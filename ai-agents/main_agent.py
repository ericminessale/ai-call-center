#!/usr/bin/env python3
"""
SignalWire Call Center AI Agents
Triage agent using contexts/steps - NO problem solving, info gathering only.
AI Specialists (separate agents) are the ONLY ones that solve problems.
Includes RAG knowledge base search via pgvector.
"""

from signalwire import AgentBase, AgentServer
from signalwire.core.function_result import FunctionResult
import os
import json
import base64
import re
import threading
from urllib.parse import quote, urlparse, urlunparse
from dotenv import load_dotenv

load_dotenv()


def _signed_webhook_url(url: str) -> str:
    """Embed WEBHOOK_AUTH credentials into a URL the platform will call back.

    The backend webhook endpoints accept HTTP Basic Auth from
    WEBHOOK_AUTH_USER/WEBHOOK_AUTH_PASSWORD. SignalWire pulls those out of
    the URL we hand it (``user:pass@host`` form) and replays them on the
    callback. If the env vars aren't set, the URL is unchanged — backend
    runs in soft mode and logs a warning instead of rejecting.
    """
    user = os.getenv('WEBHOOK_AUTH_USER')
    pw = os.getenv('WEBHOOK_AUTH_PASSWORD')
    if not user or not pw:
        return url
    parsed = urlparse(url)
    host = parsed.hostname or ''
    if parsed.port:
        host = f"{host}:{parsed.port}"
    netloc = f"{quote(user, safe='')}:{quote(pw, safe='')}@{host}"
    return urlunparse((parsed.scheme, netloc, parsed.path, parsed.params,
                       parsed.query, parsed.fragment))

def _internal_auth():
    """HTTP Basic tuple for the backend's private ``@require_internal_auth``
    routes (register-ai-leg, sentiment). Returns None when creds aren't set
    so ``requests`` sends no Authorization header (backend fail-loud handles
    the misconfig). ISO-9 (2026-07-07 pre-deploy)."""
    user = os.getenv('WEBHOOK_AUTH_USER')
    pw = os.getenv('WEBHOOK_AUTH_PASSWORD')
    return (user, pw) if (user and pw) else None


# Configuration
BACKEND_URL = os.getenv('BACKEND_URL', 'http://backend:5000')
DATABASE_URL = os.getenv('DATABASE_URL', '')
EMBEDDING_MODEL_NAME = 'sentence-transformers/all-MiniLM-L6-v2'
EMBEDDING_DIM = 384

# Defends against SQL injection where collection_name is interpolated into
# the chunks_<name> table identifier (psycopg2 can't parameterize idents).
_COLLECTION_NAME_RE = re.compile(r'^[a-zA-Z0-9_-]+$')

# Global embedding model (loaded lazily)
_embedding_model = None


def get_embedding_model():
    """Lazily load the sentence-transformers embedding model."""
    global _embedding_model
    if _embedding_model is None:
        try:
            from sentence_transformers import SentenceTransformer
            print(f"Loading embedding model: {EMBEDDING_MODEL_NAME}...", flush=True)
            _embedding_model = SentenceTransformer(EMBEDDING_MODEL_NAME)
            print("Embedding model loaded successfully.", flush=True)
        except ImportError:
            print("Warning: sentence-transformers not installed. Reindex will not work.", flush=True)
            return None
    return _embedding_model


def chunk_text(text, max_sentences=5):
    """Split text into chunks of max_sentences sentences."""
    sentences = re.split(r'(?<=[.!?])\s+', text.strip())
    chunks = []
    for i in range(0, len(sentences), max_sentences):
        chunk = ' '.join(sentences[i:i + max_sentences])
        if chunk.strip():
            chunks.append(chunk.strip())
    if not chunks and text.strip():
        chunks = [text.strip()]
    return chunks


def do_reindex(collection_name, documents, connection_string):
    """Reindex documents into pgvector.

    Creates/updates the chunks_{collection_name} table with embeddings.
    """
    import psycopg2
    from psycopg2.extras import execute_values

    model = get_embedding_model()
    if model is None:
        raise RuntimeError("Embedding model not available")

    conn = psycopg2.connect(connection_string)
    cur = conn.cursor()

    table_name = f"chunks_{collection_name}"

    # Ensure pgvector extension is enabled
    cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
    cur.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # Create chunks table if needed
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {table_name} (
            id SERIAL PRIMARY KEY,
            content TEXT NOT NULL,
            processed_content TEXT,
            embedding vector({EMBEDDING_DIM}),
            filename TEXT,
            section TEXT,
            tags JSONB DEFAULT '[]'::jsonb,
            metadata JSONB DEFAULT '{{}}'::jsonb,
            metadata_text TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)

    # Create collection_config table if needed
    cur.execute("""
        CREATE TABLE IF NOT EXISTS collection_config (
            collection_name TEXT PRIMARY KEY,
            model_name TEXT,
            embedding_dimensions INTEGER,
            chunking_strategy TEXT,
            languages JSONB,
            created_at TIMESTAMP DEFAULT NOW(),
            metadata JSONB DEFAULT '{}'::jsonb
        )
    """)

    # Clear existing chunks for this collection
    cur.execute(f"DELETE FROM {table_name}")

    # Process each document
    total_chunks = 0
    for doc in documents:
        chunks = chunk_text(doc['content'])
        if not chunks:
            continue

        # Generate embeddings for all chunks at once
        embeddings = model.encode(chunks)

        # Prepare values for bulk insert
        values = []
        for chunk_content, embedding in zip(chunks, embeddings):
            values.append((
                chunk_content,
                chunk_content.lower(),
                embedding.tolist(),
                doc['title'],
                doc['title'],
                json.dumps([]),
                json.dumps({'title': doc['title']}),
                doc['title'].lower(),
            ))

        execute_values(cur, f"""
            INSERT INTO {table_name}
            (content, processed_content, embedding, filename, section, tags, metadata, metadata_text)
            VALUES %s
        """, values, template="(%s, %s, %s::vector, %s, %s, %s::jsonb, %s::jsonb, %s)")

        total_chunks += len(chunks)

    # Update collection config
    cur.execute("""
        INSERT INTO collection_config (collection_name, model_name, embedding_dimensions, chunking_strategy)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (collection_name) DO UPDATE SET
            model_name = EXCLUDED.model_name,
            embedding_dimensions = EXCLUDED.embedding_dimensions,
            chunking_strategy = EXCLUDED.chunking_strategy
    """, (collection_name, EMBEDDING_MODEL_NAME, EMBEDDING_DIM, 'sentence'))

    # Create text search index if not exists. The pg_trgm extension may not
    # be installed on every environment — that's recoverable; vector search
    # still works without it. Log so we know.
    try:
        cur.execute(f"""
            CREATE INDEX IF NOT EXISTS idx_{collection_name}_content_trgm
            ON {table_name} USING gin (content gin_trgm_ops)
        """)
    except Exception as exc:
        print(f"trgm index creation skipped (non-fatal): {exc}", flush=True)

    conn.commit()
    cur.close()
    conn.close()

    print(f"Reindexed {total_chunks} chunks for collection '{collection_name}'", flush=True)
    return total_chunks


def do_search(collection_name, query, top_k, connection_string):
    """Vector similarity search against chunks_<collection>. Returns [] if
    the chunks table doesn't exist — that's a normal state for an unindexed
    collection, not an error."""
    import psycopg2

    if not _COLLECTION_NAME_RE.match(collection_name):
        raise ValueError(f"Invalid collection_name: {collection_name!r}")

    model = get_embedding_model()
    if model is None:
        raise RuntimeError("Embedding model not available")

    query_embedding = model.encode([query])[0].tolist()
    table_name = f"chunks_{collection_name}"

    conn = psycopg2.connect(connection_string)
    try:
        cur = conn.cursor()
        cur.execute("SELECT to_regclass(%s)", (table_name,))
        if cur.fetchone()[0] is None:
            return []
        cur.execute(
            f"SELECT content, filename, section, metadata, "
            f"1 - (embedding <=> %s::vector) AS score "
            f"FROM {table_name} "
            f"ORDER BY embedding <=> %s::vector LIMIT %s",
            (query_embedding, query_embedding, top_k),
        )
        return [
            {
                'content': row[0],
                'filename': row[1],
                'section': row[2],
                'metadata': row[3],
                'score': float(row[4]),
            }
            for row in cur.fetchall()
        ]
    finally:
        cur.close()
        conn.close()


# ---------------------------------------------------------------------------
# Knowledge-base assignment cache
#
# KB bindings resolve per request inside the dynamic-config callback (see
# attach_knowledge_search), so an admin reassignment in Settings → Agents
# applies to new calls within _KB_TTL_SECONDS — no container restart. The
# cache is stale-while-revalidate: requests never block on the backend; a
# failed refresh keeps serving the last good map and retries next window.
# ---------------------------------------------------------------------------
_KB_TTL_SECONDS = 30.0
_kb_cache = {'map': {}, 'fetched_at': 0.0}
_kb_refresh_lock = threading.Lock()
_kb_refreshing = False
_kb_last_logged = {}


def _fetch_kb_assignments():
    """GET the full agent→collection map from the backend's internal API.

    Uses the same WEBHOOK_AUTH service credentials as the MCP gateway fetch
    (the old admin-surface endpoint required a user JWT, so the agents' boot
    fetch always 401'd and fell back). Returns None on any failure so callers
    keep the previous map instead of blanking a working one.
    """
    import requests
    auth_user = os.getenv('WEBHOOK_AUTH_USER')
    auth_password = os.getenv('WEBHOOK_AUTH_PASSWORD')
    auth = (auth_user, auth_password) if (auth_user and auth_password) else None
    try:
        resp = requests.get(
            f"{BACKEND_URL}/api/internal/agent-assignments",
            auth=auth,
            timeout=5,
        )
        if resp.ok:
            assignments = resp.json().get('assignments', [])
            return {
                a['agent_id']: a['collection_name']
                for a in assignments
                if a.get('agent_id') and a.get('collection_name')
            }
        print(f"Warning: agent-assignments fetch returned HTTP {resp.status_code}", flush=True)
    except Exception as e:
        print(f"Warning: agent-assignments fetch failed: {e}", flush=True)
    return None


def _refresh_kb_cache_async():
    """Kick a single-flight background refresh of the assignment cache."""
    global _kb_refreshing
    with _kb_refresh_lock:
        if _kb_refreshing:
            return
        _kb_refreshing = True

    def worker():
        global _kb_refreshing
        import time
        try:
            fresh = _fetch_kb_assignments()
            if fresh is not None:
                _kb_cache['map'] = fresh
            # Stamp even on failure so a down backend is retried once per
            # TTL window instead of on every request.
            _kb_cache['fetched_at'] = time.time()
        finally:
            with _kb_refresh_lock:
                _kb_refreshing = False

    threading.Thread(target=worker, daemon=True).start()


def get_kb_collection(agent_id):
    """Resolve an agent's assigned collection from the cache (never blocks)."""
    import time
    if time.time() - _kb_cache['fetched_at'] > _KB_TTL_SECONDS:
        _refresh_kb_cache_async()
    return _kb_cache['map'].get(agent_id)


def prime_kb_assignments():
    """Synchronously load assignments at boot so first calls bind correctly."""
    import time
    for attempt in range(3):
        fresh = _fetch_kb_assignments()
        if fresh is not None:
            _kb_cache['map'] = fresh
            _kb_cache['fetched_at'] = time.time()
            print(f"KB assignments primed: {fresh or '(none — fallbacks in effect)'}", flush=True)
            return
        time.sleep(2 * (attempt + 1))
    print(
        "Warning: KB assignments could not be primed; fallback collections "
        "serve until a background refresh succeeds.",
        flush=True,
    )


def get_active_queues():
    """Fetch active queue configs from the backend at startup.

    AI-06 fix (2026-06-02 audit): the previous single-shot 5s GET would
    silently fall back to hardcoded sales/support whenever the backend
    was momentarily slow or starting up — and since this only runs once
    at ai-agents process boot, the triage agent's contexts would be
    permanently mis-seeded until an operator manually restarted the
    container. In a docker-compose cold-start the backend often takes
    10-15s to be ready (postgres healthchecks, migrations), reliably
    triggering the fallback path.

    Now retries with exponential backoff up to ~30s total. Empty-queues
    response is treated as a hit, not a miss (an admin may have
    legitimately disabled every queue — falling back to a hardcoded
    sales+support would be wrong in that case).

    Future work: add a runtime refresh path that subscribes to the
    backend's ``queue_config_changed`` Socket.IO event so adding a new
    queue takes effect without an ai-agents restart. Tracked as
    AI-06-future in REMEDIATION.
    """
    import requests
    import time
    fallback = [
        {'slug': 'sales', 'display_name': 'Sales', 'description': 'Sales inquiries and purchases', 'ai_agent_route': '/sales-ai'},
        {'slug': 'support', 'display_name': 'Support', 'description': 'Technical support and issue resolution', 'ai_agent_route': '/support-ai'},
    ]
    # Six attempts with 1s, 2s, 4s, 8s, 16s waits between them — caps
    # at ~31s of patience, which covers a normal docker-compose cold
    # start with margin to spare while still surfacing a true outage.
    delays = [0, 1, 2, 4, 8, 16]
    last_error = None
    for attempt, delay in enumerate(delays):
        if delay:
            time.sleep(delay)
        try:
            resp = requests.get(f"{BACKEND_URL}/api/queues/config/active", timeout=5)
            if resp.ok:
                queues = resp.json().get('queues', [])
                # Empty list IS a valid response — an admin may have
                # legitimately disabled every queue. Don't paper over
                # that with the hardcoded fallback.
                print(
                    f"Loaded {len(queues)} active queues from backend "
                    f"(attempt {attempt + 1}/{len(delays)}): "
                    f"{[q['slug'] for q in queues]}",
                    flush=True,
                )
                return queues
            last_error = f"HTTP {resp.status_code}"
        except Exception as e:
            last_error = str(e)
        print(
            f"queues/config/active fetch attempt {attempt + 1}/{len(delays)} "
            f"failed: {last_error}; retrying in {delays[attempt + 1] if attempt + 1 < len(delays) else 'no more'}s",
            flush=True,
        )

    print(
        f"WARNING: All {len(delays)} attempts to fetch queues failed "
        f"(last error: {last_error}). Falling back to hardcoded "
        f"sales/support — operator should investigate the backend.",
        flush=True,
    )
    return fallback


def attach_knowledge_search(agent):
    """Attach native_vector_search to the per-request ephemeral agent.

    Called from the dynamic-config callback, which the SDK runs on a fresh
    ephemeral copy for BOTH SWML renders and SWAIG executions — so the tool
    is registered with the current collection at execution time too.

    The skill must NOT also be attached at boot: _create_ephemeral_copy
    re-loads boot skills into each copy first, and with a duplicate tool
    name the stale boot binding wins (add_skill treats the re-registration
    as an expected duplicate and keeps the first one).

    Agents opt in by setting ``_kb_agent_id`` (assignment slug) and
    optionally ``_kb_fallback_collection`` in __init__.
    """
    agent_id = getattr(agent, '_kb_agent_id', None)
    if not agent_id:
        return
    if not DATABASE_URL:
        print(f"Warning: DATABASE_URL not set, skipping knowledge search for {agent_id}", flush=True)
        return

    collection = get_kb_collection(agent_id) or getattr(agent, '_kb_fallback_collection', None)
    if not collection:
        return

    try:
        agent.add_skill("native_vector_search", {
            "tool_name": "search_knowledge",
            "backend": "pgvector",
            "connection_string": DATABASE_URL,
            "collection_name": collection,
            "description": "Search the knowledge base for relevant information. Use this when the customer asks questions about products, services, troubleshooting, or anything you need to look up.",
            "count": 5,
            "build_index": False,
            # Per-tool fillers (filler policy: opt-in only — no language-level
            # function_fillers anywhere). A KB lookup is a real 1-2s wait where
            # the persona plausibly speaks; SkillBase merges swaig_fields into
            # the tool definition it registers.
            "swaig_fields": {
                "fillers": [
                    "Let me check on that for you.",
                    "One moment while I look that up.",
                ],
            },
        })
        # Runs on every request — only log when the binding actually changes.
        if _kb_last_logged.get(agent_id) != collection:
            _kb_last_logged[agent_id] = collection
            print(f"Knowledge search for {agent_id} -> collection '{collection}'", flush=True)
    except Exception as e:
        print(f"Warning: Failed to attach knowledge search for {agent_id}: {e}", flush=True)


def add_mcp_gateways(agent, agent_id):
    """Load every admin-configured MCP Gateway bound to this agent slug.

    Customer-supplied external tool integrations: each McpGatewayConfig
    row in the backend tells us a gateway URL, credentials, and which
    agent slugs should load it. We pull the matching configs from the
    backend's internal endpoint and register one ``mcp_gateway`` skill
    instance per gateway. The skill itself handles tool discovery,
    session lifecycle, and bridging MCP calls to SWAIG functions.

    Failures are non-fatal — if the backend isn't reachable or auth is
    misconfigured, we log and skip MCP rather than blocking agent boot.
    """
    backend_url = os.getenv('BACKEND_URL', 'http://backend:5000')
    auth_user = os.getenv('WEBHOOK_AUTH_USER')
    auth_password = os.getenv('WEBHOOK_AUTH_PASSWORD')
    auth = (auth_user, auth_password) if (auth_user and auth_password) else None

    try:
        import requests as http_requests
        resp = http_requests.get(
            f"{backend_url}/api/internal/mcp-gateways",
            params={'agent_id': agent_id},
            auth=auth,
            timeout=5,
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as e:
        print(f"Warning: Could not fetch MCP gateways for {agent_id}: {e}", flush=True)
        return

    gateways = payload.get('gateways') or []
    if not gateways:
        print(f"No MCP gateways bound to {agent_id}", flush=True)
        return

    for entry in gateways:
        name = entry.get('name', '<unnamed>')
        config = entry.get('config') or {}
        try:
            agent.add_skill("mcp_gateway", config)
            print(
                f"Added MCP gateway '{name}' to {agent_id} "
                f"(url={config.get('gateway_url')!r})",
                flush=True,
            )
        except Exception as e:
            # One bad gateway should not poison the agent. Log + continue.
            print(
                f"Warning: failed to add MCP gateway '{name}' to {agent_id}: {e}",
                flush=True,
            )


def start_admin_api():
    """Start a lightweight FastAPI server for admin operations (reindex)."""
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse
    import uvicorn

    admin_app = FastAPI(title="AI Agents Admin API")

    @admin_app.post("/reindex")
    async def reindex(request: Request):
        try:
            data = await request.json()
            collection_name = data.get('collection_name')
            documents = data.get('documents', [])

            if not collection_name:
                return JSONResponse({'error': 'collection_name is required'}, status_code=400)
            if not documents:
                return JSONResponse({'error': 'No documents provided'}, status_code=400)
            if not DATABASE_URL:
                return JSONResponse({'error': 'DATABASE_URL not configured'}, status_code=500)

            chunks_indexed = do_reindex(collection_name, documents, DATABASE_URL)

            return JSONResponse({
                'success': True,
                'collection_name': collection_name,
                'documents_processed': len(documents),
                'chunks_indexed': chunks_indexed,
            })
        except Exception as e:
            import traceback
            traceback.print_exc()
            return JSONResponse({'error': str(e)}, status_code=500)

    @admin_app.post("/search")
    async def search(request: Request):
        try:
            data = await request.json()
            collection_name = data.get('collection_name')
            query = (data.get('query') or '').strip()
            top_k = data.get('top_k', 5)

            if not collection_name:
                return JSONResponse({'error': 'collection_name is required'}, status_code=400)
            if not query:
                return JSONResponse({'error': 'query is required'}, status_code=400)
            if not isinstance(top_k, int) or top_k < 1 or top_k > 50:
                top_k = 5
            if not DATABASE_URL:
                return JSONResponse({'error': 'DATABASE_URL not configured'}, status_code=500)

            results = do_search(collection_name, query, top_k, DATABASE_URL)
            return JSONResponse({
                'success': True,
                'collection_name': collection_name,
                'query': query,
                'results': results,
            })
        except ValueError as e:
            return JSONResponse({'error': str(e)}, status_code=400)
        except Exception as e:
            import traceback
            traceback.print_exc()
            return JSONResponse({'error': str(e)}, status_code=500)

    @admin_app.get("/admin/health")
    async def admin_health():
        return {"status": "healthy", "service": "ai-agents-admin"}

    # Pre-warm the embedding model so the first /reindex or /search call doesn't
    # eat the 10–30s cold-load. Without this, first request hits the Flask
    # proxy's 10s timeout and returns 503 even though /search succeeds.
    try:
        get_embedding_model()
    except Exception as e:
        print(f"[admin_api] Pre-warm of embedding model failed (non-fatal): {e}", flush=True)

    uvicorn.run(admin_app, host="0.0.0.0", port=8081, log_level="info")


def get_base_url_from_global_data(raw_data: dict) -> str:
    """Get the base URL from global_data (set during initial request)."""
    global_data = raw_data.get('global_data', {})
    if global_data.get('agent_base_url'):
        return global_data['agent_base_url']

    env_url = os.getenv('AGENT_BASE_URL')
    if env_url and not env_url.startswith('http://ai-agents'):
        return env_url.rstrip('/')

    print("Warning: Could not determine agent base URL", flush=True)
    return 'http://ai-agents:8080'


def capture_base_url(query_params, body_params, headers, agent):
    """Dynamic config callback - captures external URL and sets post_prompt_url.

    Also reads 'ctx' query param (base64-encoded JSON) to inject customer context
    as global_data for outbound AI calls, and binds the agent's knowledge-base
    skill to its currently assigned collection (live reassignment, no restart).
    """
    # Bind KB search to the current admin-assigned collection. Runs on the
    # ephemeral copy, so each request reflects assignments within the TTL.
    attach_knowledge_search(agent)

    existing_global = body_params.get('global_data', {})
    new_global = {}
    base_url = None

    forwarded_host = headers.get('x-forwarded-host') or headers.get('X-Forwarded-Host')
    forwarded_proto = headers.get('x-forwarded-proto') or headers.get('X-Forwarded-Proto') or 'https'

    if forwarded_host:
        if 'ngrok' in forwarded_host:
            forwarded_proto = 'https'
        base_url = f"{forwarded_proto}://{forwarded_host}"
        print(f"Detected base URL: {base_url}", flush=True)
        new_global['agent_base_url'] = base_url
    else:
        host = headers.get('host') or headers.get('Host')
        if host and not host.startswith('ai-agents') and not host.startswith('localhost'):
            base_url = f"https://{host}"
            new_global['agent_base_url'] = base_url
        else:
            env_url = os.getenv('AGENT_BASE_URL')
            if env_url and not env_url.startswith('http://ai-agents'):
                base_url = env_url.rstrip('/')
                new_global['agent_base_url'] = base_url

    # Wire the post-prompt summary to the backend's consumer
    # (/api/webhooks/post-prompt auto-fills disposition/agent_notes/summary
    # and transitions call state). The agent and backend share one external
    # origin behind nginx — the same base_url every other webhook uses — so
    # the prior "ngrok only exposes 8080" concern no longer applies. Signed
    # with WEBHOOK_AUTH creds the backend validates (soft no-op if unset).
    if base_url:
        agent.set_post_prompt_url(
            _signed_webhook_url(f"{base_url}/api/webhooks/post-prompt")
        )

        # Debug telemetry (opt-in via DEBUG_WEBHOOK_ENABLED). We set the SWML
        # params DIRECTLY instead of calling self.enable_debug_events() on
        # purpose: the SDK's enable path (agent_base.py ~1064) re-points
        # debug_webhook_url at the agent's OWN /debug_events endpoint at render
        # time, which would clobber this and send events to the agent instead
        # of the backend. The platform only needs these two params in the AI
        # verb, so setting them here routes debug events through nginx to the
        # backend's /api/webhooks/debug-events capture — exactly like the
        # post-prompt above. Keep self.enable_debug_events(...) commented out
        # while using this. Level 2 is high volume (~dozens of POSTs/call);
        # enable only while debugging a specific call, then flip the flag off.
        if os.getenv('DEBUG_WEBHOOK_ENABLED', 'false').strip().lower() == 'true':
            try:
                debug_level = int(os.getenv('DEBUG_WEBHOOK_LEVEL', '2'))
            except ValueError:
                debug_level = 2
            agent.set_params({
                "debug_webhook_url": _signed_webhook_url(
                    f"{base_url}/api/webhooks/debug-events"),
                "debug_webhook_level": debug_level,
            })
            print(f"Debug webhook enabled (level {debug_level}) -> "
                  f"{base_url}/api/webhooks/debug-events", flush=True)

    # Read conference name and call DB ID from query params (conference-first architecture)
    conf_param = query_params.get('conf')
    if conf_param:
        new_global['conf'] = conf_param
        print(f"Conference name from query param: {conf_param}", flush=True)

    call_db_id = query_params.get('call_db_id')
    if call_db_id:
        new_global['call_db_id'] = call_db_id
        print(f"Call DB ID from query param: {call_db_id}", flush=True)

    # Read context from query params (for outbound AI calls)
    ctx_param = query_params.get('ctx')
    if ctx_param:
        try:
            ctx_data = json.loads(base64.urlsafe_b64decode(ctx_param).decode())
            print(f"Received outbound context: {ctx_data}", flush=True)
            new_global.update(ctx_data)
        except Exception as e:
            print(f"Warning: Failed to decode ctx param: {e}", flush=True)

    if new_global:
        agent.set_global_data(new_global)

    # Register AI leg's B-leg call SID with backend (for takeover support)
    # Do this in a background thread to avoid blocking the AI agent startup
    if call_db_id and base_url:
        call_data = body_params.get('call', {})
        b_leg_sid = call_data.get('call_id')
        if b_leg_sid:
            def register_ai_leg():
                try:
                    import requests as http_requests
                    backend_url = os.getenv('BACKEND_URL', 'http://backend:5000')
                    url = f"{backend_url}/api/calls/{call_db_id}/register-ai-leg"
                    http_requests.post(url, json={'signalwire_sid': b_leg_sid},
                                       auth=_internal_auth(), timeout=5)
                    print(f"Registered AI B-leg SID: {b_leg_sid} for call {call_db_id}", flush=True)
                except Exception as e:
                    print(f"Warning: Failed to register AI leg SID: {e}", flush=True)
            threading.Thread(target=register_ai_leg, daemon=True).start()


def add_sentiment_tool(agent):
    """Add the report_sentiment global tool and prompt to any agent.

    Uses define_tool with a local handler that POSTs to the backend over the
    Docker network. skip_fillers is not yet supported via define_tool (SDK gap).
    """

    def _handle_report_sentiment(args, raw_data):
        score = args.get('score', 0.0)
        reason = args.get('reason', '')
        global_data = raw_data.get('global_data', {})
        call_db_id = global_data.get('call_db_id')

        if call_db_id:
            def post_sentiment():
                try:
                    import requests as http_requests
                    backend_url = os.getenv('BACKEND_URL', 'http://backend:5000')
                    url = f"{backend_url}/api/calls/{call_db_id}/sentiment"
                    http_requests.post(url, json={'score': score, 'reason': reason},
                                       auth=_internal_auth(), timeout=5)
                except Exception as exc:
                    # Non-fatal: sentiment is decorative, never block the call.
                    print(f"sentiment POST failed (non-fatal): {exc}", flush=True)
            threading.Thread(target=post_sentiment, daemon=True).start()

        return FunctionResult("ok")

    agent.define_tool(
        name="report_sentiment",
        description=(
            "Silently report a change in customer sentiment. Only call this when "
            "you detect a meaningful shift — not for every utterance."
        ),
        parameters={
            "score": {
                "type": "number",
                "description": (
                    "Sentiment score from -1.0 to 1.0. "
                    "-1.0 = extremely negative, 0.0 = neutral, 1.0 = extremely positive"
                )
            },
            "reason": {
                "type": "string",
                "description": "Brief note on what triggered the change (e.g. 'customer frustrated about wait time')"
            }
        },
        handler=_handle_report_sentiment,
        skip_fillers=True
    )

    agent.prompt_add_section(
        "Sentiment Tracking",
        body=(
            "You have a silent tool called report_sentiment. Use it when you detect a "
            "meaningful shift in the customer's emotional state — frustration, relief, "
            "confusion, satisfaction, anger, gratitude, etc."
        ),
        bullets=[
            "Only report changes — if the customer stays in the same emotional state, do not call it again",
            "Call it silently with no filler words, no acknowledgment to the caller",
            "It must never interrupt or alter the conversation flow in any way",
            "Score guide: -1.0 extremely negative, 0.0 neutral, 1.0 extremely positive"
        ]
    )


# Shared post_prompt wrap-up fields appended to every agent's per-call
# JSON summary. The post-prompt webhook persists these into
# calls.disposition_code + calls.agent_notes so the wrap-up panel in the
# contact detail view shows them pre-filled (the human can still edit).
# Valid disposition codes are mirrored from backend DISPOSITION_CODES in
# backend/app/api/calls.py — keep in sync if either list changes.
WRAP_UP_POST_PROMPT_FIELDS = (
    '"disposition": "one of: resolved|transferred|callback-scheduled|escalated'
    '|sales-opportunity|technical-issue|no-answer|wrong-number|spam|abandoned'
    '|other — pick the best fit for the call\'s BUSINESS outcome", '
    '"post_mortem": "2-3 sentence assessment for the agent reviewing this '
    'call: what the caller wanted, how it ended, any recommended follow-up"'
)


class CallCenterAgent(AgentBase):
    """Project-wide base for every agent class in this file.

    Centralizes the bits that were previously duplicated across all five
    agents — the basic-auth bypass, and the queue-transfer helper that
    every ``transfer_to_human`` / ``escalate_to_human`` tool ends up
    calling. Per-agent tools just describe what's specific (department,
    spoken response, source label, extra context fields).
    """

    def _check_basic_auth(self, request) -> bool:
        # Agents are only reachable via nginx, which has already terminated
        # the basic-auth layer SignalWire was configured with. Skipping the
        # SDK's inner check avoids a double prompt without removing real auth.
        return True

    def _transfer_to_human_queue(
        self,
        *,
        department: str,
        spoken_response: str,
        context_data: dict,
        raw_data: dict,
    ) -> FunctionResult:
        """Hand the caller off to ``/api/queues/<department>/route``.

        The caller passes the agent-specific bits (``customer_name``,
        ``reason``, ``source_agent``, etc.) in ``context_data``; this
        method merges in the standard fields, base64-encodes, builds the
        URL with conf/call_db_id wiring, signs it with WEBHOOK_AUTH
        creds, and returns a ready-to-return ``FunctionResult``.
        """
        base_url = get_base_url_from_global_data(raw_data)
        global_data = raw_data.get('global_data', {})
        conf = global_data.get('conf', '')
        call_db_id = global_data.get('call_db_id', '')

        # Standard fields every queue transfer includes.
        context_data.setdefault('department', department)
        context_data.setdefault('preferred_handling', 'human')
        context_data.setdefault(
            'caller_language', global_data.get('caller_language', 'en-US')
        )

        context_b64 = base64.urlsafe_b64encode(
            json.dumps(context_data).encode()
        ).decode()
        queue_url = f"{base_url}/api/queues/{department}/route?ctx={context_b64}"
        if conf:
            queue_url += f"&conf={conf}"
        if call_db_id:
            queue_url += f"&call_db_id={call_db_id}"

        result = FunctionResult(spoken_response, post_process=True)
        result.update_global_data(context_data)
        result.action.append({
            "SWML": {
                "version": "1.0.0",
                "sections": {
                    "main": [
                        {"transfer": {"dest": _signed_webhook_url(queue_url)}}
                    ]
                },
            },
            "transfer": "true",
        })
        return result


class CallCenterTriageAgent(CallCenterAgent):
    """
    Call Center TRIAGE Agent - Information gathering ONLY.

    This agent does NOT solve problems. It ONLY:
    1. Collects the customer's name
    2. Identifies if they need sales or support
    3. Offers transfer to human queue OR AI specialist

    The AI Specialists (SalesAISpecialist, SupportAISpecialist) are the ONLY
    agents that actually help solve problems or answer questions.
    """

    def __init__(self):
        super().__init__(
            name="CallCenterTriageAgent",
            route="/receptionist",
            auto_answer=True
        )

        self.set_dynamic_config_callback(capture_base_url)
        add_sentiment_tool(self)
        add_mcp_gateways(self, 'receptionist')

        # Voice and speech configuration
        # Multiple languages so the receptionist can greet/converse in the caller's
        # language while we capture the preference for routing + live_translate.
        # Filler policy (2026-06-10): NO language-level function_fillers on any
        # agent. They fire on EVERY tool call — including silent-by-design tools
        # (set_caller_language, report_sentiment) — and they announce internal
        # moves the caller must never perceive: the triage→department context
        # shift is the same persona, same voice, one continuous person. Fillers
        # are opt-in per tool: overt handoffs (transfer_*, escalate_*) declare
        # their own; everything else is silent.
        # VOICE ENGINE WORKAROUND (2026-06-11): the platform's openai TTS
        # engine intermittently dies mid-call (generated turns render with
        # audio_latency=0; voice_config_error; see voice-repro/ bug report).
        # Pinned via isolation ladder: bare agent on openai.alloy dies by
        # turn ~9; identical agent on rime.spore ran 20 turns clean. All
        # English paths move to rime until the platform fix; es/fr keep
        # openai.alloy (rarely exercised; rime multilingual ids unvalidated
        # — a bad voice id is a runtime voice_error, the symptom we're
        # escaping). Revert when the platform closes the bug.
        self.add_language("English", "en-US", "rime.spore")
        self.add_language("Spanish", "es-ES", "openai.alloy")
        self.add_language("French", "fr-FR", "openai.alloy")
        self.set_prompt_llm_params(
            temperature=0.4, top_p=0.9,
            barge_confidence=0.6, frequency_penalty=0.2)
        self.set_params({
            "end_of_speech_timeout": 800,
            "ai_volume": 0,
            "enable_text_normalization": "both",
        })
        # Observability: debug telemetry is wired through the BACKEND instead —
        # set DEBUG_WEBHOOK_ENABLED=true and see capture_base_url(), which points
        # debug_webhook_url at /api/webhooks/debug-events. Do NOT uncomment the
        # line below: enable_debug_events() re-points debug_webhook_url at this
        # agent's own endpoint at render time and would override that capture.
        # self.enable_debug_events(level=2)

        # Internal fillers for step/context transitions (common-mistakes.md #31)
        self.add_internal_filler("next_step", "en-US", [
            "One moment...", "Bear with me...",
        ])
        # change_context must NOT reveal routing — "right team" reads as a
        # handoff, but the department shift is the same persona and voice.
        # Neutral, one-person acknowledgments only.
        self.add_internal_filler("change_context", "en-US", [
            "One moment...", "Sure, one second...",
        ])

        # Fetch active queues from backend (dynamic at startup)
        queues = get_active_queues()
        queue_slugs = [q['slug'] for q in queues]
        self._queue_ai_map = {q['slug']: q.get('ai_agent_route', f"/{q['slug']}-ai") for q in queues}

        # Speech recognition hints
        hint_words = ["SignalWire"] + [q['display_name'] for q in queues] + queue_slugs
        self.add_hints(hint_words)

        # ============================================================
        # GLOBAL PROMPT - Personality and role boundaries
        # ============================================================
        self.prompt_add_section(
            "Role",
            "You are Sam, a warm and efficient call center receptionist. "
            "You speak in short, natural sentences. You sound like a real person — not a script. "
            "You are genuinely friendly but you keep things moving."
        )

        self.prompt_add_section(
            "Voice Style",
            body="This is a voice call. Optimize everything for spoken conversation:",
            bullets=[
                "Keep sentences under 15 words when possible",
                "Ask one question at a time — never stack multiple questions",
                "Use everyday language, not corporate-speak",
                "When acknowledging something, keep it to a few words before moving on"
            ]
        )

        self.prompt_add_section(
            "Boundaries",
            "Your only job is to greet callers, learn their name, figure out which department they need, "
            "and connect them. All questions, troubleshooting, and advice are handled by the specialists "
            "you transfer to. If a caller asks you a question or describes a problem, acknowledge it briefly "
            "and move toward getting them connected."
        )

        self.prompt_add_section(
            "Language Detection",
            body=(
                "You can converse in English, Spanish, and French. From the caller's very first words, "
                "detect which language they're speaking and respond in that same language. As soon as "
                "you've confirmed which language they prefer, silently call set_caller_language with "
                "the BCP-47 code so we can route them to a matching agent."
            ),
            bullets=[
                "If the caller starts in English, call set_caller_language('en-US') silently",
                "If the caller starts in Spanish, switch to Spanish and call set_caller_language('es-ES')",
                "If the caller starts in French, switch to French and call set_caller_language('fr-FR')",
                "If you can't tell, just ask once: 'Which language do you prefer — English, Spanish, or French?'",
                "Call this tool ONCE per call, no fillers, no acknowledgment to the caller",
            ]
        )

        # Build department list for post_prompt
        dept_options = '/'.join(queue_slugs + ['unknown'])
        self.set_post_prompt(
            f'Summarize this call as a JSON object: {{"customer_name": "name or null", '
            f'"department": "{dept_options}", '
            '"reason": "brief reason for call", '
            '"outcome": "transferred_to_human/transferred_to_ai/abandoned", '
            '"notes": "any important details", '
            f'{WRAP_UP_POST_PROMPT_FIELDS}}}'
        )
        self.set_post_prompt_llm_params(temperature=0.1, top_p=0.9)

        # Define the contexts and steps
        contexts = self.define_contexts()

        # ============================================================
        # TRIAGE CONTEXT (default) - Greeting and routing
        # ============================================================
        triage_ctx = contexts.add_context("default")

        # Build department info for step prompts
        queue_descriptions = []
        for q in queues:
            desc = q.get('description', '')
            queue_descriptions.append(f"{q['display_name']} ({q['slug']}): {desc}" if desc else f"{q['display_name']} ({q['slug']})")

        dept_list_text = "\n".join(queue_descriptions)
        route_instructions = "\n".join([f"- {q['display_name']}-related: switch to the '{q['slug']}' context" for q in queues])

        dept_names = [q['display_name'] for q in queues]
        if len(dept_names) > 1:
            dept_menu = ', '.join(dept_names[:-1]) + ' or ' + dept_names[-1]
        else:
            dept_menu = dept_names[0] if dept_names else 'general assistance'

        # Step 1: Greet and get name (also detects caller's language)
        triage_ctx.add_step("greeting") \
            .add_section("Goal",
                "Welcome the caller and get their name. Introduce yourself as Sam. "
                "Be warm but brief — this should take one exchange. "
                "Detect their language from their first words and respond in kind.") \
            .add_section("Handling Eager Callers",
                "If the caller gives you their name AND mentions what they need in the same breath, "
                "great — note both. You can skip asking about the department in the next step.") \
            .set_step_criteria("The customer has stated their name and you have called set_caller_language") \
            .set_valid_steps(["route_department"]) \
            .set_functions(["report_sentiment", "set_caller_language"])

        # Step 2: Determine department
        triage_ctx.add_step("route_department") \
            .add_section("Goal",
                "Figure out which department the caller needs. If they already told you during "
                "the greeting, route them immediately — no need to ask again.") \
            .add_section("If You Need to Ask",
                f"Ask naturally which area they need help with: {dept_menu}.") \
            .add_section("Departments", dept_list_text) \
            .add_section("Routing",
                "Once you know the department, move to that context seamlessly:\n" + route_instructions) \
            .set_step_criteria("Customer has indicated which department they need") \
            .set_valid_contexts(queue_slugs) \
            .set_functions(["report_sentiment", "set_caller_language"])

        # ============================================================
        # DYNAMIC QUEUE CONTEXTS - One per configured queue
        # ============================================================
        for q in queues:
            slug = q['slug']
            display = q['display_name']

            queue_ctx = contexts.add_context(slug) \
                .set_consolidate(True)

            queue_ctx.add_section("Context",
                f"The caller needs {display.lower()} help. You still have their name and "
                "what they told you from the greeting. Use it naturally.")

            # Step 1: Ask what they need help with
            queue_ctx.add_step("gather_reason") \
                .add_section("Goal",
                    f"Briefly ask what they need help with so you can pass useful context "
                    "to the specialist. One question is enough — don't interrogate them.") \
                .add_section("If They Already Told You",
                    "If the caller already described their issue during the greeting, "
                    "you have what you need. Move on to offering transfer options.") \
                .set_step_criteria("You have a basic understanding of what the caller needs help with") \
                .set_valid_steps(["offer_transfer"]) \
                .set_functions(["report_sentiment"])

            # Step 2: Offer transfer choice
            queue_ctx.add_step("offer_transfer") \
                .add_section("Goal",
                    f"Offer to connect them with a {display.lower()} specialist, "
                    "or let them know our AI assistant can help right away. Let them choose.") \
                .add_section("Handling Questions",
                    "If they ask you a question about their issue, acknowledge it and "
                    "let them know a specialist can help with that. Then offer the transfer options.") \
                .add_section("Transferring",
                    "Once they choose:\n"
                    "- Human specialist: use the transfer_to_human tool\n"
                    "- AI assistant: use the transfer_to_ai_specialist tool\n\n"
                    f"Always include: customer_name, reason, department='{slug}', urgency, additional_info") \
                .set_step_criteria("Customer has chosen human or AI assistance") \
                .set_valid_steps([]) \
                .set_functions(["transfer_to_human", "transfer_to_ai_specialist", "report_sentiment"])

        # Tools registered via @AgentBase.tool() decorators below

    @AgentBase.tool(
        name="set_caller_language",
        description=(
            "Silently record the caller's preferred language (BCP-47 code). "
            "Call this once after detecting or confirming what language the caller speaks. "
            "Used by routing to prefer language-matched agents."
        ),
        parameters={
            "language": {
                "type": "string",
                "description": "BCP-47 code: 'en-US', 'es-ES', 'fr-FR', 'de-DE', 'pt-BR', etc.",
            }
        },
        # No fillers — must be silent so it doesn't interrupt the conversation
    )
    def set_caller_language(self, args, raw_data):
        """Persist caller's language preference into global_data so it flows with the call."""
        language = (args.get("language") or "en-US").strip()
        # Return a NON-EMPTY result. An empty FunctionResult("") tells the engine
        # the turn has nothing to say, so it ends the turn and the AI's next line
        # (e.g. "Which department?") is generated but never spoken — the call
        # stalls into dead air. report_sentiment (the other silent tool) returns
        # "ok" for exactly this reason; mirror it. "ok" is a function return to
        # the model, not spoken to the caller.
        result = FunctionResult("ok")
        result.update_global_data({"caller_language": language})
        return result

    @AgentBase.tool(
        name="transfer_to_human",
        description=(
            "Connect the caller to a human representative in the department they need. "
            "Use this when the caller says they want to talk to a person."
        ),
        parameters={
            "customer_name": {"type": "string", "description": "The caller's name as they said it"},
            "reason": {"type": "string", "description": "Brief summary of what they need help with"},
            "department": {"type": "string", "description": "Which department (e.g., 'sales', 'support')"},
            "urgency": {"type": "string", "description": "'high', 'medium', or 'low'"},
            "additional_info": {"type": "string", "description": "Any other relevant details from the conversation"},
        },
        fillers=["I'm connecting you now.", "One moment, I'll get you to the right person."],
    )
    def transfer_to_human(self, args, raw_data):
        """Triage agent's transfer — department comes from caller intent."""
        urgency = args.get("urgency", "medium")
        urgency_map = {'high': 2, 'medium': 5, 'low': 8}
        return self._transfer_to_human_queue(
            department=args.get("department", "support").lower(),
            spoken_response="I'll connect you with a representative right now.",
            context_data={
                'customer_name': args.get("customer_name", ""),
                'reason': args.get("reason", ""),
                'urgency': urgency,
                'priority': urgency_map.get(urgency.lower(), 5),
                'additional_info': args.get("additional_info", ""),
                'source_agent': 'call_center_triage',
            },
            raw_data=raw_data,
        )

    @AgentBase.tool(
        name="transfer_to_ai_specialist",
        description=(
            "Connect the caller to our AI assistant for their department. "
            "Use this when the caller says they'd like help from the AI assistant."
        ),
        parameters={
            "customer_name": {"type": "string", "description": "The caller's name as they said it"},
            "reason": {"type": "string", "description": "Brief summary of what they need help with"},
            "department": {"type": "string", "description": "Which department (e.g., 'sales', 'support')"},
            "urgency": {"type": "string", "description": "'high', 'medium', or 'low'"},
            "additional_info": {"type": "string", "description": "Any other relevant details from the conversation"},
        },
        fillers=["Let me connect you with our AI assistant.", "One moment."],
    )
    def transfer_to_ai_specialist(self, args, raw_data):
        """Transfer to AI specialist agent"""
        customer_name = args.get("customer_name", "")
        reason = args.get("reason", "")
        department = args.get("department", "support").lower()
        urgency = args.get("urgency", "medium")
        additional_info = args.get("additional_info", "")

        base_url = get_base_url_from_global_data(raw_data)
        global_data = raw_data.get('global_data', {})

        # Pass conference name and call DB ID to specialist (conference-first architecture)
        conf = global_data.get('conf', '')
        call_db_id = global_data.get('call_db_id', '')

        specialist_route = self._queue_ai_map.get(department, f"/{department}-ai")
        transfer_url = f"{base_url}{specialist_route}"
        if conf:
            transfer_url += f"?conf={conf}"
        if call_db_id:
            transfer_url += f"&call_db_id={call_db_id}" if '?' in transfer_url else f"?call_db_id={call_db_id}"

        result = FunctionResult('')  # Silent transfer
        result.update_global_data({
            'customer_name': customer_name,
            'reason': reason,
            'department': department,
            'urgency': urgency,
            'additional_info': additional_info,
            'preferred_handling': 'ai',
            'source_agent': 'call_center_triage',
            'caller_language': global_data.get('caller_language', 'en-US'),
        })
        result.action.append({
            "SWML": {
                "version": "1.0.0",
                "sections": {"main": [{"transfer": {"dest": transfer_url}}]}
            },
            "transfer": "true"
        })
        return result


class SalesAISpecialist(CallCenterAgent):
    """
    AI Sales Specialist - This agent DOES help with sales inquiries.
    Only reached after customer explicitly chooses AI assistance.
    Has RAG knowledge base search via pgvector.
    """

    def __init__(self):
        super().__init__(
            name="SalesAISpecialist",
            route="/sales-ai",
            auto_answer=True
        )

        # Voice and speech configuration
        # Persona voices: Sam=alloy (triage), Alex=echo (sales), Jordan=shimmer
        # (support) — distinct voices make the triage→specialist handoff audible.
        # OpenAI voice ids pass through to the platform; validate new ids on a
        # live call before a demo (a bad id surfaces as voice_error at runtime).
        # rime.spore: openai engine workaround (see triage note). Distinct
        # persona voices (IMP-13) restored once more rime ids are validated.
        self.add_language("English", "en-US", "rime.spore",
            speech_fillers=["Good question, let me think about that.", "Hmm, let me consider that."])
        # function_fillers removed — fillers are opt-in per tool (see policy in
        # BasicReceptionist); search_knowledge carries its own via swaig_fields.
        self.set_prompt_llm_params(
            temperature=0.5, top_p=0.9,
            barge_confidence=0.5, frequency_penalty=0.1)
        self.set_params({
            "wait_for_user": False,
            "end_of_speech_timeout": 1000,
            "enable_text_normalization": "both",
        })
        # Observability: debug telemetry is wired through the BACKEND instead —
        # set DEBUG_WEBHOOK_ENABLED=true and see capture_base_url(), which points
        # debug_webhook_url at /api/webhooks/debug-events. Do NOT uncomment the
        # line below: enable_debug_events() re-points debug_webhook_url at this
        # agent's own endpoint at render time and would override that capture.
        # self.enable_debug_events(level=2)
        self.add_hints(["SignalWire", "pricing", "enterprise", "demo", "trial", "API",
                        "platform", "integration", "SDK", "CPaaS", "UCaaS"])

        self.set_dynamic_config_callback(capture_base_url)
        add_sentiment_tool(self)

        # KB search binds per request in capture_base_url — admin reassignment
        # applies to new calls without a restart.
        self._kb_agent_id = 'sales-ai'
        self._kb_fallback_collection = 'sales_knowledge'
        add_mcp_gateways(self, 'sales-ai')

        self.set_post_prompt(
            'Summarize this sales consultation as a JSON object: '
            '{"customer_name": "name or null", "company": "company or null", '
            '"products_discussed": ["list"], "recommendations_made": ["list"], '
            '"next_steps": "recommended next steps", '
            '"lead_score": "1-10 where 1 is hot", '
            '"outcome": "sale/quote_requested/follow_up_needed/lost", '
            f'{WRAP_UP_POST_PROMPT_FIELDS}}}'
        )
        self.set_post_prompt_llm_params(temperature=0.1, top_p=0.9)

        self.prompt_add_section(
            "Role",
            "You are Alex, a consultative AI sales specialist. You listen first and recommend second. "
            "You ask discovery questions to understand what the customer actually needs before suggesting solutions. "
            "You are knowledgeable, genuine, and never pushy."
        )

        self.prompt_add_section(
            "Voice Style",
            body="This is a voice call. Optimize for spoken conversation:",
            bullets=[
                "Keep responses to one or two sentences unless explaining something specific",
                "Lead with the answer, then add detail if needed",
                "Never list more than three things at once — offer to go deeper on any of them",
                "Ask one question at a time"
            ]
        )

        self.prompt_add_section(
            "Customer Context",
            "Customer name: ${global_data.customer_name}\n"
            "What they're interested in: ${global_data.reason}\n"
            "Additional context: ${global_data.additional_info}\n\n"
            "The customer was just transferred from our receptionist. Greet them by name and "
            "pick up where they left off — don't re-ask what they already told Sam."
        )

        self.prompt_add_section(
            "Approach",
            body="How to handle the conversation:",
            bullets=[
                "Start by understanding their situation — what problem are they trying to solve?",
                "Ask about their current setup, team size, or use case to tailor your recommendations",
                "Match their needs to specific products or features",
                "Give honest, straightforward pricing guidance when asked",
                "Use the search_knowledge tool to find specific product details when needed"
            ]
        )

        self.prompt_add_section(
            "When to Escalate",
            body="Use the escalate_to_human tool when:",
            bullets=[
                "They're ready to make a purchase or need a formal quote",
                "They need custom pricing or contract terms",
                "They specifically ask to speak with a person",
                "The question is about billing or account-specific details you can't access"
            ]
        )

        # Tool registered via @AgentBase.tool() decorator below

    @AgentBase.tool(
        name="escalate_to_human",
        description=(
            "Connect the caller to a human sales representative. Use when they want to make "
            "a purchase, need a formal quote, request custom pricing, or ask to speak with a person."
        ),
        parameters={
            "reason": {"type": "string", "description": "Why the caller needs a human rep"},
        },
        fillers=["Let me get a sales representative for you.", "One moment."],
    )
    def escalate_to_human(self, args, raw_data):
        """Sales specialist's hand-off back to a human rep."""
        global_data = raw_data.get('global_data', {})
        return self._transfer_to_human_queue(
            department='sales',
            spoken_response="I'll connect you with a sales representative who can help with that.",
            context_data={
                'customer_name': global_data.get('customer_name', ''),
                'reason': global_data.get('reason', ''),
                'urgency': global_data.get('urgency', 'medium'),
                'priority': global_data.get('priority', 5),
                'additional_info': global_data.get('additional_info', ''),
                'escalation_reason': args.get("reason", ""),
                'escalated_from': 'sales_ai_specialist',
                'source_agent': 'sales_ai_specialist',
            },
            raw_data=raw_data,
        )


class SupportAISpecialist(CallCenterAgent):
    """
    AI Support Specialist - This agent DOES troubleshoot and solve problems.
    Only reached after customer explicitly chooses AI assistance.
    Has RAG knowledge base search via pgvector.
    """

    def __init__(self):
        super().__init__(
            name="SupportAISpecialist",
            route="/support-ai",
            auto_answer=True
        )

        # Voice and speech configuration
        # Jordan's persona voice — see SalesAISpecialist for the voice map.
        # rime.spore: openai engine workaround (see triage note).
        self.add_language("English", "en-US", "rime.spore",
            speech_fillers=["Let me think about that.", "Good question."])
        # function_fillers removed — fillers are opt-in per tool (see policy in
        # BasicReceptionist); search_knowledge carries its own via swaig_fields.
        self.set_prompt_llm_params(
            temperature=0.3, top_p=0.9,
            barge_confidence=0.5, frequency_penalty=0.2)
        self.set_params({
            "wait_for_user": False,
            "end_of_speech_timeout": 1000,
            "enable_text_normalization": "both",
        })
        # Observability: debug telemetry is wired through the BACKEND instead —
        # set DEBUG_WEBHOOK_ENABLED=true and see capture_base_url(), which points
        # debug_webhook_url at /api/webhooks/debug-events. Do NOT uncomment the
        # line below: enable_debug_events() re-points debug_webhook_url at this
        # agent's own endpoint at render time and would override that capture.
        # self.enable_debug_events(level=2)
        self.add_hints(["SignalWire", "error", "restart", "configuration", "API", "log",
                        "debug", "timeout", "connection", "webhook", "SDK"])

        self.set_dynamic_config_callback(capture_base_url)
        add_sentiment_tool(self)

        # KB search binds per request in capture_base_url (see SalesAISpecialist).
        self._kb_agent_id = 'support-ai'
        self._kb_fallback_collection = 'support_knowledge'
        add_mcp_gateways(self, 'support-ai')

        self.set_post_prompt(
            'Summarize this support consultation as a JSON object: '
            '{"customer_name": "name or null", '
            '"issue_summary": "brief description", '
            '"troubleshooting_steps": ["steps attempted"], '
            '"resolution": "how resolved or null", '
            '"resolved": true or false, '
            '"escalation_reason": "why escalated or null", '
            '"customer_satisfaction": "1-5 based on tone", '
            f'{WRAP_UP_POST_PROMPT_FIELDS}}}'
        )
        self.set_post_prompt_llm_params(temperature=0.1, top_p=0.9)

        self.prompt_add_section(
            "Role",
            "You are Jordan, a patient and methodical AI support specialist. "
            "You confirm you understand a problem before jumping to solutions. "
            "You are calm, empathetic, and you explain things in plain language."
        )

        self.prompt_add_section(
            "Voice Style",
            body="This is a voice call. Keep it natural:",
            bullets=[
                "Give one instruction at a time — wait for confirmation before the next step",
                "Explain technical terms simply when you use them",
                "Keep responses short — if an explanation is long, break it into parts",
                "Check in after each step: a quick 'did that work?' goes a long way"
            ]
        )

        self.prompt_add_section(
            "Customer Context",
            "Customer name: ${global_data.customer_name}\n"
            "Reported issue: ${global_data.reason}\n"
            "Urgency: ${global_data.urgency}\n"
            "Additional context: ${global_data.additional_info}\n\n"
            "The customer was just transferred from our receptionist. Greet them by name, "
            "briefly acknowledge what they told Sam, and start helping."
        )

        self.prompt_add_section(
            "Approach",
            body="How to troubleshoot effectively:",
            bullets=[
                "First, confirm you understand the issue by restating it briefly in your own words",
                "Ask what they've already tried so you don't repeat steps",
                "Start with the simplest, most common fix",
                "Walk through one step at a time — confirm it worked before moving on",
                "Use the search_knowledge tool to look up solutions when needed"
            ]
        )

        self.prompt_add_section(
            "When to Escalate",
            body="Use the escalate_to_human tool when:",
            bullets=[
                "The customer asks to speak with a person — escalate IMMEDIATELY, do not try to troubleshoot first",
                "You've tried two or three approaches and the issue persists",
                "The issue requires account access or admin-level changes you can't make",
            ]
        )

        # Tool registered via @AgentBase.tool() decorator below

    @AgentBase.tool(
        name="escalate_to_human",
        description=(
            "Connect the caller to a human support specialist. Use IMMEDIATELY when "
            "the caller asks for a person. Also use when the issue needs account access "
            "or you've tried multiple fixes without success."
        ),
        parameters={
            "reason": {"type": "string", "description": "Why the caller needs a human specialist"},
        },
        fillers=["Let me get a support specialist for you.", "One moment."],
    )
    def escalate_to_human(self, args, raw_data):
        """Support specialist's hand-off back to a human rep."""
        global_data = raw_data.get('global_data', {})
        return self._transfer_to_human_queue(
            department='support',
            spoken_response="I'll connect you with a support specialist who can help with that.",
            context_data={
                'customer_name': global_data.get('customer_name', ''),
                'reason': global_data.get('reason', ''),
                'urgency': global_data.get('urgency', 'medium'),
                'priority': global_data.get('priority', 5),
                'additional_info': global_data.get('additional_info', ''),
                'escalation_reason': args.get("reason", ""),
                'escalated_from': 'support_ai_specialist',
                'source_agent': 'support_ai_specialist',
            },
            raw_data=raw_data,
        )


# ============================================================
# OUTBOUND AI AGENTS
# Used when an agent sends an AI agent to call a customer.
# These receive CRM context (name, company, tier, notes) via
# global_data, set from the ?ctx= query param.
# ============================================================

class OutboundSalesAgent(CallCenterAgent):
    """
    Outbound AI Sales Agent - proactively calls customers for sales outreach.
    Receives customer context from the call center dashboard.
    Has RAG knowledge base search via pgvector.
    """

    def __init__(self):
        super().__init__(
            name="OutboundSalesAgent",
            route="/outbound-sales",
            auto_answer=True
        )

        # Voice and speech configuration
        # Outbound sales is the same "Alex" persona as SalesAISpecialist —
        # keep the voice identical so the persona is consistent either direction.
        # rime.spore: openai engine workaround (see triage note).
        self.add_language("English", "en-US", "rime.spore",
            speech_fillers=["That's a great question.", "Let me think about that."])
        # function_fillers removed — fillers are opt-in per tool (filler policy).
        self.set_prompt_llm_params(
            temperature=0.5, top_p=0.9,
            barge_confidence=0.5, frequency_penalty=0.1)
        self.set_params({
            "wait_for_user": False,
            "end_of_speech_timeout": 1000,
            "enable_text_normalization": "both",
        })
        # Observability: debug telemetry is wired through the BACKEND instead —
        # set DEBUG_WEBHOOK_ENABLED=true and see capture_base_url(), which points
        # debug_webhook_url at /api/webhooks/debug-events. Do NOT uncomment the
        # line below: enable_debug_events() re-points debug_webhook_url at this
        # agent's own endpoint at render time and would override that capture.
        # self.enable_debug_events(level=2)
        self.add_hints(["SignalWire", "pricing", "enterprise", "demo", "trial", "API",
                        "platform", "integration"])

        self.set_dynamic_config_callback(capture_base_url)
        add_sentiment_tool(self)

        # KB search binds per request in capture_base_url (see SalesAISpecialist).
        self._kb_agent_id = 'outbound-sales'
        self._kb_fallback_collection = 'sales_knowledge'
        add_mcp_gateways(self, 'outbound-sales')

        self.set_post_prompt(
            'Summarize this outbound sales call as a JSON object: '
            '{"customer_name": "name", "company": "company or null", '
            '"products_discussed": ["list"], '
            '"customer_interest_level": "high/medium/low/none", '
            '"next_steps": "recommended follow-up", '
            '"outcome": "interested/callback_requested/not_interested/no_answer/voicemail", '
            f'{WRAP_UP_POST_PROMPT_FIELDS}}}'
        )
        self.set_post_prompt_llm_params(temperature=0.1, top_p=0.9)

        self.prompt_add_section(
            "Role",
            "You are Alex, a warm and professional AI sales representative making an outbound call. "
            "You are genuine, respectful of people's time, and never pushy. "
            "You get to the point quickly and listen more than you talk."
        )

        self.prompt_add_section(
            "Voice Style",
            body="This is an outbound voice call — first impressions matter:",
            bullets=[
                "Introduce yourself and the company in your first sentence",
                "Ask if now is a good time before diving in",
                "Keep it conversational — this is a phone call, not a pitch deck",
                "If they say it's not a good time, respect that and offer to call back"
            ]
        )

        self.prompt_add_section(
            "Customer Context",
            "Customer name: ${global_data.contact_name}\n"
            "Company: ${global_data.company}\n"
            "Account tier: ${global_data.account_tier}\n"
            "VIP: ${global_data.is_vip}\n"
            "Previous calls: ${global_data.total_calls}\n"
            "Notes: ${global_data.notes}\n"
            "Instructions: ${global_data.additional_context}\n\n"
            "Use this context to personalize the conversation. Don't read it back to them — "
            "weave it in naturally. Follow any special instructions."
        )

        self.prompt_add_section(
            "Approach",
            body="How to handle the call:",
            bullets=[
                "Tailor your conversation to their tier and history",
                "For VIP or enterprise customers, acknowledge their relationship with the company",
                "Ask discovery questions to understand their current needs",
                "Match solutions to their specific situation",
                "Use search_knowledge to find relevant product details when needed"
            ]
        )

        self.prompt_add_section(
            "When to Transfer",
            "Use the transfer_to_human tool if they want to speak with a person, "
            "are ready to purchase, or need help outside of sales."
        )

        # Tool registered via @AgentBase.tool() decorator below

    @AgentBase.tool(
        name="transfer_to_human",
        description=(
            "Connect the caller to a human sales representative. Use when they want to "
            "speak with a person, are ready to purchase, or need help outside of sales."
        ),
        parameters={
            "reason": {"type": "string", "description": "Why the caller needs a human rep"},
        },
        fillers=["Let me connect you with a sales representative.", "One moment."],
    )
    def transfer_to_human(self, args, raw_data):
        """Outbound sales agent's hand-off to a human rep."""
        global_data = raw_data.get('global_data', {})
        return self._transfer_to_human_queue(
            department='sales',
            spoken_response="I'll connect you with a sales representative right away.",
            context_data={
                'customer_name': global_data.get('contact_name', ''),
                'company': global_data.get('company', ''),
                'reason': args.get("reason", ""),
                'urgency': 'medium',
                'priority': 5,
                'additional_info': global_data.get('additional_context', ''),
                'escalated_from': 'outbound_sales_agent',
                'source_agent': 'outbound_sales_agent',
            },
            raw_data=raw_data,
        )


class OutboundSupportAgent(CallCenterAgent):
    """
    Outbound AI Support Agent - proactively calls customers for support follow-ups.
    Receives customer context from the call center dashboard.
    Has RAG knowledge base search via pgvector.
    """

    def __init__(self):
        super().__init__(
            name="OutboundSupportAgent",
            route="/outbound-support",
            auto_answer=True
        )

        # Voice and speech configuration
        # Outbound support is the same "Jordan" persona as SupportAISpecialist.
        # rime.spore: openai engine workaround (see triage note).
        self.add_language("English", "en-US", "rime.spore",
            speech_fillers=["Let me think about that.", "Good question."])
        # function_fillers removed — fillers are opt-in per tool (filler policy).
        self.set_prompt_llm_params(
            temperature=0.3, top_p=0.9,
            barge_confidence=0.5, frequency_penalty=0.2)
        self.set_params({
            "wait_for_user": False,
            "end_of_speech_timeout": 1000,
            "enable_text_normalization": "both",
        })
        # Observability: debug telemetry is wired through the BACKEND instead —
        # set DEBUG_WEBHOOK_ENABLED=true and see capture_base_url(), which points
        # debug_webhook_url at /api/webhooks/debug-events. Do NOT uncomment the
        # line below: enable_debug_events() re-points debug_webhook_url at this
        # agent's own endpoint at render time and would override that capture.
        # self.enable_debug_events(level=2)
        self.add_hints(["SignalWire", "error", "restart", "configuration", "API", "log",
                        "debug", "timeout", "connection", "webhook"])

        self.set_dynamic_config_callback(capture_base_url)
        add_sentiment_tool(self)

        # KB search binds per request in capture_base_url (see SalesAISpecialist).
        self._kb_agent_id = 'outbound-support'
        self._kb_fallback_collection = 'support_knowledge'
        add_mcp_gateways(self, 'outbound-support')

        self.set_post_prompt(
            'Summarize this outbound support call as a JSON object: '
            '{"customer_name": "name", "company": "company or null", '
            '"issue_discussed": "what was discussed", '
            '"resolution": "how resolved or null", '
            '"resolved": true or false, '
            '"follow_up_needed": true or false, '
            '"outcome": "resolved/escalated/callback_requested/no_answer/voicemail", '
            f'{WRAP_UP_POST_PROMPT_FIELDS}}}'
        )
        self.set_post_prompt_llm_params(temperature=0.1, top_p=0.9)

        self.prompt_add_section(
            "Role",
            "You are Jordan, a patient and empathetic AI support specialist making a proactive follow-up call. "
            "You genuinely care about helping the customer resolve their issue. "
            "You explain things clearly and never rush."
        )

        self.prompt_add_section(
            "Voice Style",
            body="This is an outbound voice call — be considerate:",
            bullets=[
                "Introduce yourself and explain why you're calling right away",
                "Ask if it's a good time to talk",
                "Give one instruction at a time during troubleshooting",
                "Check in after each step before moving on"
            ]
        )

        self.prompt_add_section(
            "Customer Context",
            "Customer name: ${global_data.contact_name}\n"
            "Company: ${global_data.company}\n"
            "Account tier: ${global_data.account_tier}\n"
            "VIP: ${global_data.is_vip}\n"
            "Previous calls: ${global_data.total_calls}\n"
            "Notes: ${global_data.notes}\n"
            "Instructions: ${global_data.additional_context}\n\n"
            "Use this context to personalize the call. Reference their history naturally. "
            "Follow any special instructions. Prioritize VIP and enterprise customers."
        )

        self.prompt_add_section(
            "Approach",
            body="How to handle the call:",
            bullets=[
                "Explain the reason for your call clearly",
                "Ask about their current situation and what they need help with",
                "Start with the simplest fix and work up from there",
                "Use search_knowledge to look up solutions when needed",
                "If you can't resolve it in a reasonable time, offer to connect them with a specialist"
            ]
        )

        self.prompt_add_section(
            "When to Transfer",
            "Use the transfer_to_human tool if the issue needs account-level access, "
            "you've tried multiple approaches without success, or they request a human."
        )

        # Tool registered via @AgentBase.tool() decorator below

    @AgentBase.tool(
        name="transfer_to_human",
        description=(
            "Connect the caller to a human support specialist. Use when the issue needs "
            "account-level access, multiple fixes haven't worked, or they request a person."
        ),
        parameters={
            "reason": {"type": "string", "description": "Why the caller needs a human specialist"},
        },
        fillers=["Let me connect you with a support specialist.", "One moment."],
    )
    def transfer_to_human(self, args, raw_data):
        """Outbound support agent's hand-off to a human rep."""
        global_data = raw_data.get('global_data', {})
        return self._transfer_to_human_queue(
            department='support',
            spoken_response="I'll connect you with a support specialist right away.",
            context_data={
                'customer_name': global_data.get('contact_name', ''),
                'company': global_data.get('company', ''),
                'reason': args.get("reason", ""),
                'urgency': 'medium',
                'priority': 5,
                'additional_info': global_data.get('additional_context', ''),
                'escalated_from': 'outbound_support_agent',
                'source_agent': 'outbound_support_agent',
            },
            raw_data=raw_data,
        )


if __name__ == '__main__':
    print('=' * 60)
    print('SignalWire AI Call Center - Triage + Specialists + Outbound')
    print('  with RAG Knowledge Base (pgvector)')
    print('=' * 60)

    # Start admin API server (reindex endpoint) in background thread
    print('\nStarting admin API on port 8081...', flush=True)
    admin_thread = threading.Thread(target=start_admin_api, daemon=True)
    admin_thread.start()

    server = AgentServer(host='0.0.0.0', port=8080)

    # Triage agent - info gathering ONLY
    triage = CallCenterTriageAgent()

    # Specialist agents - these actually solve problems (inbound transfer targets)
    # Each gets RAG knowledge search from their assigned pgvector collection
    sales_ai = SalesAISpecialist()
    support_ai = SupportAISpecialist()

    # Outbound agents - proactive calls with CRM context
    # Also get RAG knowledge search
    outbound_sales = OutboundSalesAgent()
    outbound_support = OutboundSupportAgent()

    # Prime the KB assignment cache now that the backend is reachable (the
    # triage agent's get_active_queues just blocked on it) so the very first
    # calls bind to admin-assigned collections instead of fallbacks.
    prime_kb_assignments()

    # Register agents
    server.register(triage, '/receptionist')
    server.register(sales_ai, '/sales-ai')
    server.register(support_ai, '/support-ai')
    server.register(outbound_sales, '/outbound-sales')
    server.register(outbound_support, '/outbound-support')

    username, password = triage.get_basic_auth_credentials()

    print('\nAuthentication:')
    print(f'  Username: {username}')
    print(f'  Password: {password}')
    print('\nRoutes (Inbound):')
    print('  /receptionist : Triage agent (NO problem solving)')
    print('  /sales-ai     : Sales specialist (helps with sales + RAG)')
    print('  /support-ai   : Support specialist (troubleshoots + RAG)')
    print('\nRoutes (Outbound):')
    print('  /outbound-sales   : Outbound sales (proactive + RAG)')
    print('  /outbound-support : Outbound support (proactive + RAG)')
    print('\nAdmin API:')
    print('  POST /reindex : Reindex documents into pgvector (port 8081)')
    print('  POST /search  : Vector similarity search (port 8081)')
    print(f'\nDatabase: {DATABASE_URL[:50]}...' if DATABASE_URL else '\nDatabase: NOT CONFIGURED')
    print('\nStarting agent server on port 8080...\n')

    server.run()
