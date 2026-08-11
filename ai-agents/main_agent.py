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
import hashlib
import hmac
import re
import threading
import time
from urllib.parse import quote, urlparse, urlunparse
from dotenv import load_dotenv

load_dotenv()


# CRITICAL-1 Phase 2 / HIGH-4. These agent routes are PUBLIC — nginx's
# auth_basic is commented out and the SDK's own check is disabled — so whatever
# a rendered SWML contains is readable by anyone who GETs /receptionist. Putting
# WEBHOOK_AUTH in the callback URLs therefore published the install's webhook
# password. This is the leak, and this helper is where it happened.
#
# Token mode replaces the credential with an HMAC bound to the callback's own
# PATH, signed with INTERNAL_AUTH_PASSWORD — which never appears in any
# rendered document. Must stay byte-compatible with the backend's
# app/utils/url_utils.py: the backend VERIFIES what we mint here, so the
# message format, truncation and parameter names are a shared contract.
WEBHOOK_TOKEN_PARAM = '_wt'
WEBHOOK_TOKEN_EXPIRY_PARAM = '_wexp'
WEBHOOK_URL_TOKEN_TTL_SECONDS = 12 * 60 * 60


def _webhook_token_signing_secret():
    """INTERNAL_AUTH_PASSWORD only — no fallback, matching the backend.

    Falling back to WEBHOOK_AUTH_PASSWORD would sign the token with the exact
    credential this change removes from the URL, so a leaked SWML would let
    anyone forge tokens. No secret → no token mode.
    """
    return os.getenv('INTERNAL_AUTH_PASSWORD') or None


def _basic_auth_webhook_url(url: str) -> str:
    """Legacy scheme: WEBHOOK_AUTH creds in the ``user:pass@host``."""
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


def _signed_webhook_url(url: str) -> str:
    """Authenticate a URL the platform will call back into the backend.

    ``WEBHOOK_URL_AUTH=token`` switches to the path-bound HMAC; anything else
    keeps today's credentials-in-URL. Default is unchanged on purpose — the
    backend accepts BOTH schemes, so this flips (and rolls back) without a
    flag-day, but only a live PSTN call proves the callbacks still land.

    If the env isn't set for whichever scheme is selected, the URL is returned
    unchanged / degraded to Basic — the backend's soft mode logs rather than
    rejects.
    """
    if os.getenv('WEBHOOK_URL_AUTH', '').strip().lower() != 'token':
        return _basic_auth_webhook_url(url)

    secret = _webhook_token_signing_secret()
    if not secret:
        print(
            '[agent] WEBHOOK_URL_AUTH=token ignored: INTERNAL_AUTH_PASSWORD is '
            'not set, so there is no secret safe to sign callback tokens with. '
            'Falling back to credentials-in-URL.',
            flush=True,
        )
        return _basic_auth_webhook_url(url)

    parsed = urlparse(url)
    expires_at = int(time.time()) + WEBHOOK_URL_TOKEN_TTL_SECONDS
    # Path only, and the same message string the backend signs — see
    # url_utils._webhook_token_path / webhook_url_token.
    message = f'webhook-url:{parsed.path or "/"}:{expires_at}'.encode()
    token = hmac.new(
        secret.encode(), message, hashlib.sha256,
    ).hexdigest()[:32]
    extra = f'{WEBHOOK_TOKEN_EXPIRY_PARAM}={expires_at}&{WEBHOOK_TOKEN_PARAM}={token}'
    query = f'{parsed.query}&{extra}' if parsed.query else extra
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path,
                       parsed.params, query, parsed.fragment))

def _internal_auth():
    """HTTP Basic tuple for the backend's private ``@require_internal_auth``
    routes (call-context, agent-assignments, mcp-gateways, register-ai-leg,
    sentiment). Returns None when creds aren't set so ``requests`` sends no
    Authorization header (backend fail-loud handles the misconfig). ISO-9
    (2026-07-07 pre-deploy).

    Reads the segregated INTERNAL_AUTH_* secret first, falling back to
    WEBHOOK_AUTH_* — must mirror the backend's
    ``webhook_auth._expected_internal_credentials`` exactly, since backend and
    agents share one docker-compose env: if the operator rotates to a distinct
    INTERNAL_AUTH secret, both sides pick it up together and internal calls
    keep authenticating."""
    user = os.getenv('INTERNAL_AUTH_USER') or os.getenv('WEBHOOK_AUTH_USER')
    pw = os.getenv('INTERNAL_AUTH_PASSWORD') or os.getenv('WEBHOOK_AUTH_PASSWORD')
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


def _ensure_chunks_table(cur, table_name):
    """Extensions + chunks table DDL shared by full reindex and the
    single-document interaction upsert (R5)."""
    cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
    cur.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
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

    _ensure_chunks_table(cur, table_name)

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


def do_search(collection_name, query, top_k, connection_string, contact_id=None):
    """Vector similarity search against chunks_<collection>. Returns [] if
    the chunks table doesn't exist — that's a normal state for an unindexed
    collection, not an error.

    ``contact_id`` (R5) restricts results to rows whose metadata carries that
    contact — the HARD tenant/person filter for caller-history search. It is
    always server-derived (never model/caller input) at the call sites.
    """
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
        where_sql = ""
        params = [query_embedding]
        if contact_id is not None:
            where_sql = "WHERE metadata->>'contact_id' = %s "
            params.append(str(contact_id))
        params.extend([query_embedding, top_k])
        cur.execute(
            f"SELECT content, filename, section, metadata, "
            f"1 - (embedding <=> %s::vector) AS score "
            f"FROM {table_name} "
            f"{where_sql}"
            f"ORDER BY embedding <=> %s::vector LIMIT %s",
            tuple(params),
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

    Uses the same segregated internal service credentials as the MCP gateway
    fetch (the old admin-surface endpoint required a user JWT, so the agents'
    boot fetch always 401'd and fell back). Returns None on any failure so
    callers keep the previous map instead of blanking a working one.
    """
    import requests
    auth = _internal_auth()
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


# ---------------------------------------------------------------------------
# Per-call tenant config (§7.1 — Phase 4)
#
# The backend appends ?call_db_id={id} to every agent URL it hands the
# platform. GET /api/internal/call-context resolves that call's WORKSPACE
# server-side (agent routes are public, so nothing tenant-shaped is ever
# trusted from the URL itself) and returns the workspace's queues, KB
# assignments, MCP gateways and agent config in one payload. The dynamic-
# config callback shapes the ephemeral agent from it; requests without a
# call_db_id (health checks, direct pokes) run on the boot/template config,
# which carries no tenant data.
# ---------------------------------------------------------------------------
_CTX_TTL_SECONDS = 30.0
_ctx_cache = {}  # call_db_id(str) -> (payload_or_None, fetched_at)
_ctx_cache_lock = threading.Lock()


def fetch_call_context(call_db_id, ctk=None):
    """Tenant config for one call, cached per call_db_id for the TTL.

    Synchronous with a short timeout — worst case one internal HTTP call
    per call per 30s window; every SWAIG request for the same call reuses
    the cache. Failures negative-cache so a down backend costs one timeout
    per window, not per request. Returns None → template config.

    ``ctk`` is the backend-minted call-context token (§7.1); call-context
    403s without it, so a caller who hits a public agent route with a
    forged call_db_id gets template config, not a tenant's.
    """
    import time
    import requests
    if not call_db_id:
        return None
    # Cache key includes the token: an unsigned/forged request must NOT be
    # served a payload cached by a legitimate signed request for the same
    # (currently-active, enumerable) call_db_id — that would bypass the
    # backend's confused-deputy check within the TTL window.
    key = f"{call_db_id}:{ctk or ''}"
    with _ctx_cache_lock:
        hit = _ctx_cache.get(key)
        if hit and time.time() - hit[1] < _CTX_TTL_SECONDS:
            return hit[0]
    payload = None
    try:
        resp = requests.get(
            f"{BACKEND_URL}/api/internal/call-context",
            params={'call_db_id': str(call_db_id), 'ctk': ctk or ''},
            auth=_internal_auth(),
            timeout=3,
        )
        if resp.ok:
            payload = resp.json()
        elif resp.status_code == 403:
            # Forged/unsigned call_db_id — do NOT cache (a legit signed
            # retry for the same id should still resolve). Log the call id
            # only — the cache key embeds the signed ctk (F-17).
            print(f"Warning: call-context for call {call_db_id} rejected (403) — serving template config", flush=True)
            return None
        else:
            print(f"Warning: call-context for call {call_db_id} returned HTTP {resp.status_code}", flush=True)
    except Exception as e:
        print(f"Warning: call-context fetch failed for call {call_db_id}: {e}", flush=True)
    with _ctx_cache_lock:
        if len(_ctx_cache) > 512:
            _ctx_cache.clear()
        _ctx_cache[key] = (payload, time.time())
    return payload


def attach_knowledge_search(agent, collection_override=None):
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
    ``collection_override`` is the per-call tenant assignment from
    call-context (physical collection name); without it the template
    cache / fallback applies.
    """
    agent_id = getattr(agent, '_kb_agent_id', None)
    if not agent_id:
        return
    if not DATABASE_URL:
        print(f"Warning: DATABASE_URL not set, skipping knowledge search for {agent_id}", flush=True)
        return

    collection = (
        collection_override
        or get_kb_collection(agent_id)
        or getattr(agent, '_kb_fallback_collection', None)
    )
    if not collection:
        return

    # Tool description and empty-result text are the LLM's routing table for
    # this tool, so agents may override both (e.g. the sales specialist points
    # catalog/pricing questions at the live shop tools instead). The defaults
    # keep the historic behavior, except the miss message: "No information
    # found" left the model free to improvise — several 2026-08-10 harness
    # calls turned it into "I can't access pricing" or a fabricated product.
    # A miss must prescribe the next move, never just report the miss.
    description = getattr(agent, '_kb_tool_description', None) or (
        "Search the knowledge base for relevant information. Use this when "
        "the customer asks questions about products, services, "
        "troubleshooting, or anything you need to look up."
    )
    no_results_message = getattr(agent, '_kb_no_results_message', None) or (
        "No knowledge-base entry matched '{query}'. Tell the caller you "
        "don't have that detail on hand — never say you 'can't access' "
        "information — and offer to connect them with a specialist who can "
        "help. Do not invent an answer."
    )

    try:
        agent.add_skill("native_vector_search", {
            "tool_name": "search_knowledge",
            "backend": "pgvector",
            "connection_string": DATABASE_URL,
            "collection_name": collection,
            "description": description,
            "no_results_message": no_results_message,
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


# ---------------------------------------------------------------------------
# MCP gateway skills — per-request since Phase 4 (§7.2).
#
# Boot registration is GONE: _create_ephemeral_copy re-loads boot skills
# into every per-request copy first, and a duplicate instance key makes the
# stale boot gateway win over the tenant's (the same duplicate-skip trap
# the KB skill dodged by being callback-only). Gateways now attach in the
# dynamic-config callback: from the call-context payload (the call's
# workspace's rows) when a call_db_id is present, else from this template
# cache (default-workspace rows — identical to the old boot behavior).
# ---------------------------------------------------------------------------
_MCP_TTL_SECONDS = 30.0
_mcp_cache = {'map': {}, 'fetched_at': 0.0}  # agent_id -> [gateway entries]
_mcp_refresh_lock = threading.Lock()
_mcp_refreshing = False
_mcp_last_logged = {}
# Negative cache of gateway_urls whose skill setup() failed (unreachable
# endpoint), so a dead gateway is re-probed at most once per window rather
# than on every SWML render / SWAIG execution.
_mcp_setup_failures = {}  # gateway_url -> failed_at (epoch seconds)
_mcp_fail_lock = threading.Lock()
_MCP_FAIL_TTL_SECONDS = 60.0


def _fetch_mcp_gateways(agent_id):
    """GET the template (default-workspace) gateway list for one slug."""
    import requests as http_requests
    try:
        resp = http_requests.get(
            f"{BACKEND_URL}/api/internal/mcp-gateways",
            params={'agent_id': agent_id},
            auth=_internal_auth(),
            timeout=5,
        )
        resp.raise_for_status()
        return resp.json().get('gateways') or []
    except Exception as e:
        print(f"Warning: Could not fetch MCP gateways for {agent_id}: {e}", flush=True)
        return None


_MCP_AGENT_SLUGS = (
    'receptionist', 'sales-ai', 'support-ai', 'outbound-sales', 'outbound-support',
)


def prime_mcp_gateways():
    """Synchronously load the template gateway map at boot (mirrors
    prime_kb_assignments) so the first calls don't block on HTTP."""
    import time
    fresh = {}
    for slug in _MCP_AGENT_SLUGS:
        entries = _fetch_mcp_gateways(slug)
        if entries is not None:
            fresh[slug] = entries
    _mcp_cache['map'] = fresh
    _mcp_cache['fetched_at'] = time.time()
    print(
        "MCP template gateways primed: "
        + ", ".join(f"{k}={len(v)}" for k, v in fresh.items()),
        flush=True,
    )


def _refresh_mcp_cache_async():
    """Single-flight background refresh of the template gateway map."""
    global _mcp_refreshing
    with _mcp_refresh_lock:
        if _mcp_refreshing:
            return
        _mcp_refreshing = True

    def worker():
        global _mcp_refreshing
        import time
        try:
            fresh = {}
            ok = True
            for slug in _MCP_AGENT_SLUGS:
                entries = _fetch_mcp_gateways(slug)
                if entries is None:
                    ok = False
                    break
                fresh[slug] = entries
            if ok:
                _mcp_cache['map'] = fresh
            _mcp_cache['fetched_at'] = time.time()
        finally:
            with _mcp_refresh_lock:
                _mcp_refreshing = False

    threading.Thread(target=worker, daemon=True).start()


def get_template_mcp_gateways(agent_id):
    """Template gateway entries for a slug (stale-while-revalidate)."""
    import time
    if time.time() - _mcp_cache['fetched_at'] > _MCP_TTL_SECONDS:
        _refresh_mcp_cache_async()
    return _mcp_cache['map'].get(agent_id, [])


def attach_mcp_gateways(agent, call_ctx=None):
    """Register the agent's MCP gateway skills on the ephemeral copy.

    Customer-supplied external tool integrations: each gateway entry
    carries a URL, credentials, and which agent slugs should load it. One
    ``mcp_gateway`` skill instance per gateway; the skill handles tool
    discovery, session lifecycle, and bridging MCP calls to SWAIG
    functions. Failures are non-fatal per gateway.

    ``call_ctx`` is the call-context payload — its ``mcp_gateways`` list
    is the call's WORKSPACE's rows (a tenant can unbind the shared
    DemoShop or add their own gateway, §7.4). Without it the template
    cache serves the default workspace's rows.
    """
    agent_id = getattr(agent, '_mcp_agent_id', None)
    if not agent_id:
        return

    if call_ctx is not None and call_ctx.get('mcp_gateways') is not None:
        entries = [
            g for g in call_ctx['mcp_gateways']
            if agent_id in (g.get('bound_agent_ids') or [])
        ]
        source = f"workspace {call_ctx.get('workspace_id')}"
    else:
        entries = get_template_mcp_gateways(agent_id)
        source = 'template'

    import time
    for entry in entries:
        name = entry.get('name', '<unnamed>')
        config = entry.get('config') or {}
        gateway_url = config.get('gateway_url')
        # Negative cache (I1 regression fix): the skill's setup() does a
        # synchronous health GET against the gateway. Since Phase 4 moved
        # attach from boot into this per-request callback, an unreachable
        # gateway would otherwise stall EVERY render/execution (config now
        # emits a 5s request_timeout, but 5s × every call is still bad).
        # Skip a gateway whose setup failed within the last window so one
        # timeout per window replaces one per request — matching the old
        # boot behavior where a dead gateway just meant "no MCP tools".
        with _mcp_fail_lock:
            failed_at = _mcp_setup_failures.get(gateway_url)
        if failed_at and time.time() - failed_at < _MCP_FAIL_TTL_SECONDS:
            continue
        try:
            agent.add_skill("mcp_gateway", config)
            with _mcp_fail_lock:
                _mcp_setup_failures.pop(gateway_url, None)
            log_key = (agent_id, name, source)
            if _mcp_last_logged.get(log_key) != gateway_url:
                _mcp_last_logged[log_key] = gateway_url
                print(
                    f"Attached MCP gateway '{name}' to {agent_id} [{source}] "
                    f"(url={gateway_url!r})",
                    flush=True,
                )
        except Exception as e:
            # One bad gateway should not poison the agent. Negative-cache it
            # so the next calls don't repeat the (timeout-bounded) probe.
            with _mcp_fail_lock:
                _mcp_setup_failures[gateway_url] = time.time()
            print(
                f"Warning: failed to add MCP gateway '{name}' to {agent_id} "
                f"(negative-cached {_MCP_FAIL_TTL_SECONDS}s): {e}",
                flush=True,
            )


def _admin_request_authorized(request) -> bool:
    """HTTP Basic gate for the admin API (F-01,
    CONTEXT_MEMORY_VERIFICATION_AUDIT 2026-08-04).

    Same trust principal as the backend's ``require_internal_auth``: the
    segregated INTERNAL_AUTH service credentials (WEBHOOK_AUTH fallback,
    mirroring ``_internal_auth()``). FAIL-CLOSED: unconfigured credentials
    reject every request rather than leaving the reindex/search/interaction
    surface open — these endpoints can now read and write caller memory,
    not just product KB.
    """
    import base64 as _b64

    expected_user = os.getenv('INTERNAL_AUTH_USER') or os.getenv('WEBHOOK_AUTH_USER')
    expected_pw = os.getenv('INTERNAL_AUTH_PASSWORD') or os.getenv('WEBHOOK_AUTH_PASSWORD')
    if not expected_user or not expected_pw:
        print(
            '[admin_api] rejecting request: INTERNAL_AUTH_*/WEBHOOK_AUTH_* not '
            'configured — admin API is fail-closed.',
            flush=True,
        )
        return False
    header = request.headers.get('authorization') or ''
    if not header.lower().startswith('basic '):
        return False
    try:
        decoded = _b64.b64decode(header[6:].strip()).decode('utf-8')
        user, _, pw = decoded.partition(':')
        # Compare as BYTES: hmac.compare_digest raises TypeError on str with
        # any non-ASCII character, which would turn a wrong-credential 401
        # into a 500 — and would break every admin call outright if the
        # operator chose a non-ASCII password.
        return (
            hmac.compare_digest(user.encode('utf-8'), expected_user.encode('utf-8'))
            and hmac.compare_digest(pw.encode('utf-8'), expected_pw.encode('utf-8'))
        )
    except Exception:
        return False


def start_admin_api():
    """Start a lightweight FastAPI server for admin operations (reindex)."""
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse
    import uvicorn

    admin_app = FastAPI(title="AI Agents Admin API")

    def _reject_unauthorized(request):
        """None when authorized; a 401 response otherwise. /admin/health
        stays open for container healthchecks — it returns no data."""
        if _admin_request_authorized(request):
            return None
        return JSONResponse(
            {'error': 'unauthorized'},
            status_code=401,
            headers={'WWW-Authenticate': 'Basic realm="ai-agents-admin"'},
        )

    @admin_app.post("/reindex")
    async def reindex(request: Request):
        denied = _reject_unauthorized(request)
        if denied is not None:
            return denied
        try:
            data = await request.json()
            collection_name = data.get('collection_name')
            documents = data.get('documents', [])

            if not collection_name:
                return JSONResponse({'error': 'collection_name is required'}, status_code=400)
            # §7.3: the collection name is interpolated into SQL identifiers
            # (chunks_{name}) — apply the same guard do_search has. Without
            # it this unauthenticated port is a SQL-identifier injection.
            if not _COLLECTION_NAME_RE.match(collection_name):
                return JSONResponse({'error': 'invalid collection_name'}, status_code=400)
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
        denied = _reject_unauthorized(request)
        if denied is not None:
            return denied
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

    @admin_app.post("/index-interaction")
    async def index_interaction(request: Request):
        """Single-document upsert into an interaction-history collection (R5).

        Auth-gated like every admin endpoint (F-01): this one WRITES caller
        memory that later renders into prompts — poisoning it is prompt
        injection with a paper trail.

        Called by the backend at call end with one summary doc per call,
        keyed by metadata.call_id (re-posting the same call replaces its
        row). Unlike /reindex this never drops the collection — it's an
        incremental writer for the per-workspace caller-history index.
        """
        denied = _reject_unauthorized(request)
        if denied is not None:
            return denied
        try:
            data = await request.json()
            collection_name = data.get('collection_name')
            content = (data.get('content') or '').strip()
            metadata = data.get('metadata') or {}

            if not collection_name or not _COLLECTION_NAME_RE.match(collection_name):
                return JSONResponse({'error': 'invalid collection_name'}, status_code=400)
            if not content:
                return JSONResponse({'error': 'content is required'}, status_code=400)
            if not str(metadata.get('call_id') or '').strip():
                return JSONResponse({'error': 'metadata.call_id is required'}, status_code=400)
            if not str(metadata.get('contact_id') or '').strip():
                return JSONResponse({'error': 'metadata.contact_id is required'}, status_code=400)
            if not DATABASE_URL:
                return JSONResponse({'error': 'DATABASE_URL not configured'}, status_code=500)

            model = get_embedding_model()
            if model is None:
                return JSONResponse({'error': 'embedding model unavailable'}, status_code=503)

            import json as _json

            import psycopg2
            embedding = model.encode([content])[0].tolist()
            table_name = f"chunks_{collection_name}"
            conn = psycopg2.connect(DATABASE_URL)
            try:
                cur = conn.cursor()
                # DDL in its own transaction: table, then the partial unique
                # expression index that makes the write below a REAL upsert
                # (F-08 — DELETE+INSERT raced under concurrent finalizers).
                _ensure_chunks_table(cur, table_name)
                index_name = f"uq_{table_name}_call_id"
                cur.execute("SELECT to_regclass(%s)", (index_name,))
                if cur.fetchone()[0] is None:
                    # ONE TIME per collection: rows written before the index
                    # existed may already be duplicated by call_id, which
                    # would make CREATE UNIQUE INDEX fail. Collapse them
                    # (newest id wins) and then create the index. Gated on
                    # index absence so the O(n^2)-ish self-join never runs
                    # on the steady-state path — this endpoint fires on
                    # every call end.
                    cur.execute(
                        f"DELETE FROM {table_name} a USING {table_name} b "
                        f"WHERE a.id < b.id "
                        f"AND a.metadata->>'call_id' IS NOT NULL "
                        f"AND a.metadata->>'call_id' = b.metadata->>'call_id'"
                    )
                    cur.execute(
                        f"CREATE UNIQUE INDEX IF NOT EXISTS {index_name} "
                        f"ON {table_name} (((metadata->>'call_id'))) "
                        f"WHERE metadata->>'call_id' IS NOT NULL"
                    )
                conn.commit()

                cur.execute(
                    f"INSERT INTO {table_name} "
                    f"(content, processed_content, embedding, filename, section, "
                    f"tags, metadata, metadata_text) "
                    f"VALUES (%s, %s, %s::vector, %s, %s, %s::jsonb, %s::jsonb, %s) "
                    f"ON CONFLICT ((metadata->>'call_id')) "
                    f"WHERE metadata->>'call_id' IS NOT NULL "
                    f"DO UPDATE SET "
                    f"content = EXCLUDED.content, "
                    f"processed_content = EXCLUDED.processed_content, "
                    f"embedding = EXCLUDED.embedding, "
                    f"metadata = EXCLUDED.metadata, "
                    f"metadata_text = EXCLUDED.metadata_text, "
                    f"created_at = NOW()",
                    (
                        content,
                        content.lower(),
                        embedding,
                        f"call-{metadata['call_id']}",
                        'interaction',
                        _json.dumps([]),
                        _json.dumps({k: str(v) for k, v in metadata.items()}),
                        content.lower(),
                    ),
                )
                conn.commit()
            finally:
                conn.close()
            return JSONResponse({'success': True, 'collection_name': collection_name})
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
    as global_data for outbound AI calls, and shapes the ephemeral agent for
    the CALL'S WORKSPACE (§7.1/§7.2): tenant queues rebuild the triage
    contexts (fixes AI-06's boot-frozen queue list as a side effect), the KB
    skill binds to the workspace's assigned collection, MCP gateway skills
    attach from the workspace's rows, and tenant branding lands as a prompt
    section. No call_db_id (health checks, direct pokes) → inert template
    config, no tenant data.
    """
    # Per-call tenant config — resolved SERVER-SIDE from the Call row via
    # the internal call-context endpoint (agent routes are public; a URL
    # workspace param would be forgeable).
    #
    # call_db_id is on the query string for the SWML RENDER, but SWAIG tool
    # EXECUTIONS POST to the function webhook and carry it in global_data
    # instead (the render stashed it there via set_global_data). The
    # callback runs on the ephemeral copy for BOTH, and KB/MCP bind here —
    # so resolve from either source or a tenant's tool call (KB search)
    # would silently fall back to the template workspace's collection.
    call_db_id = query_params.get('call_db_id')
    ctk = query_params.get('ctk')
    if not call_db_id:
        gd = body_params.get('global_data') or {}
        call_db_id = gd.get('call_db_id')
        ctk = ctk or gd.get('ctk')
    tenant_ctx = fetch_call_context(call_db_id, ctk)

    # Caller memory (R1, CONTEXT_AUDIT_2026-08-04): the backend's tiered
    # contact block. Only inbound agents opt in via _inbound_caller_memory —
    # the outbound agents already receive richer, human-vetted context via
    # ?ctx= and must not get a second, competing section.
    caller_mem_enabled = bool(getattr(agent, '_inbound_caller_memory', False))
    mem_contact = (tenant_ctx or {}).get('contact') if caller_mem_enabled else None
    mem_last = (tenant_ctx or {}).get('last_interaction') if caller_mem_enabled else None
    mem_callback = (tenant_ctx or {}).get('open_callback') if caller_mem_enabled else None
    mem_direction = (tenant_ctx or {}).get('direction')
    # Vet topics ONCE and reuse (A-5): the triage greeting step and the
    # "Known Caller" facts block must not disagree about which past topic may
    # be raised. Specialists have no greeting step at all, so for them this
    # vetted result is the ONLY thing standing between a closed/stale topic
    # and the caller hearing about it.
    caller_hint = (
        _caller_greeting_hint(mem_contact, mem_last, mem_callback, mem_direction)
        if caller_mem_enabled else None
    )
    # Language memory: open in the caller's documented language when we speak
    # it. Must run BEFORE the greeting is built so the hint can carry it.
    #
    # Gated on caller_hint being present — i.e. only when the prompt will ALSO
    # carry the instruction. Reordering the voice list without instructing the
    # model would have it speak English text through a Spanish voice, which is
    # worse than not doing this at all.
    opening_language = (
        _open_in_documented_language(agent, mem_contact, mem_last)
        if (caller_mem_enabled and caller_hint is not None) else None
    )
    if opening_language:
        caller_hint['opening_language'] = opening_language

    # Bind KB search to the workspace's assigned collection (falls back to
    # the template cache / hardcoded fallback without tenant context). Runs
    # on the ephemeral copy, so each request reflects assignments within
    # the TTL.
    kb_override = None
    if tenant_ctx is not None:
        kb_override = (tenant_ctx.get('kb_assignments') or {}).get(
            getattr(agent, '_kb_agent_id', None)
        )
    attach_knowledge_search(agent, collection_override=kb_override)

    # MCP gateway skills — per-request since Phase 4 (boot registration
    # removed; see attach_mcp_gateways for the duplicate-skip rationale).
    attach_mcp_gateways(agent, tenant_ctx)

    # Triage only: rebuild the queue-shaped config (contexts, routing map,
    # hints, post-prompt enum) from the workspace's queues.
    if getattr(agent, '_is_triage', False) and tenant_ctx is not None \
            and tenant_ctx.get('queues') is not None:
        try:
            configure_triage_queues(agent, tenant_ctx['queues'], caller=caller_hint)
        except Exception as e:
            # A failed rebuild leaves the deep-copied template contexts in
            # place — degraded (template queues) but functional.
            print(f"Warning: triage queue rebuild failed: {e}", flush=True)

    # Tenant branding (v1: company name only — §7.2). New section name, so
    # it can't collide with the boot sections deep-copied into this copy.
    company_name = ((tenant_ctx or {}).get('agent_config') or {}).get('company_name')
    if company_name:
        try:
            agent.prompt_add_section(
                "Company",
                f"You work for {company_name}. When the company name comes up, "
                f"it is {company_name} — never any other name."
            )
        except Exception as e:
            print(f"Warning: company-name section failed: {e}", flush=True)

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

    # Reuse the render-or-execution resolved id (query param on render,
    # global_data on SWAIG execution) so it stays in global_data either way.
    # ctk rides alongside so SWAIG executions and downstream specialist
    # transfers can re-present the backend-minted call-context token.
    if call_db_id:
        new_global['call_db_id'] = call_db_id
        if ctk:
            new_global['ctk'] = ctk
        print(f"Call DB ID: {call_db_id}", flush=True)

    # Read context from query params (outbound AI calls + callback dials).
    # Values-level logging removed on purpose (R1 redaction prereq): this
    # payload now routinely carries contact names/notes — keys only.
    ctx_param = query_params.get('ctx')
    if ctx_param:
        try:
            ctx_data = json.loads(base64.urlsafe_b64decode(ctx_param).decode())
            print(f"Received context param with keys: {sorted(ctx_data.keys())}", flush=True)
            new_global.update(ctx_data)
        except Exception as e:
            # F-13: loud — a malformed envelope means a producer bug, and
            # silently dropping it is how context loss hides.
            print(f"ERROR: rejected malformed ctx param (continuing without context): {e}", flush=True)

    # Caller-memory injection (R1): structured facts into global_data plus
    # one behavioral prompt section. setdefault so explicit ?ctx= values
    # (applied just above) win on collision. Unknown caller → mem_contact is
    # None → the render is identical to pre-R1 output.
    if mem_contact and (mem_contact.get('previous_calls') or mem_contact.get('name_known')):
        mem_keys = {
            'returning_caller': bool(mem_contact.get('previous_calls')),
            'previous_calls': mem_contact.get('previous_calls') or 0,
            'is_vip': bool(mem_contact.get('is_vip')),
        }
        # contact_id rides along for tools that need a hard server-side
        # filter (R5's search_caller_history) — backend-derived, never
        # caller-supplied.
        if mem_contact.get('id'):
            mem_keys['contact_id'] = mem_contact['id']
        if mem_contact.get('name_known') and mem_contact.get('name'):
            mem_keys['contact_name'] = mem_contact['name']
        if mem_contact.get('company'):
            mem_keys['contact_company'] = mem_contact['company']
        if mem_contact.get('account_tier'):
            mem_keys['account_tier'] = mem_contact['account_tier']
        if mem_last:
            if mem_last.get('reason'):
                mem_keys['last_call_reason'] = mem_last['reason']
            if mem_last.get('disposition'):
                mem_keys['last_call_disposition'] = mem_last['disposition']
            if mem_last.get('summary_short'):
                mem_keys['last_call_summary'] = mem_last['summary_short']
            if mem_last.get('caller_language'):
                mem_keys['known_caller_language'] = mem_last['caller_language']
        if mem_callback and mem_callback.get('reason'):
            mem_keys['open_callback_reason'] = mem_callback['reason']
        for key, value in mem_keys.items():
            new_global.setdefault(key, value)

        try:
            facts = []
            if mem_keys.get('contact_name'):
                facts.append(f"Name on file: {mem_keys['contact_name']}")
            if mem_keys.get('contact_company'):
                facts.append(f"Company: {mem_keys['contact_company']}")
            if mem_keys.get('account_tier'):
                facts.append(
                    f"Account tier: {mem_keys['account_tier']}"
                    + (" (VIP)" if mem_keys.get('is_vip') else "")
                )
            facts.append(f"Previous calls with us: {mem_keys['previous_calls']}")
            # A-5: every history line says explicitly whether it may be
            # RAISED or is BACKGROUND ONLY, decided by the same
            # _offerable_topic gate the greeting uses (recent + still open).
            # Without this the facts block was an unconditional licence to
            # bring up any topic at any age — and for specialists, which have
            # no greeting step, it was the whole policy.
            vetted_topic = (caller_hint or {}).get('last_reason')
            multiple_topics = bool((caller_hint or {}).get('multiple_topics'))
            if mem_keys.get('last_call_reason'):
                line = f"Last call was about: {mem_keys['last_call_reason']}"
                if mem_keys.get('last_call_disposition'):
                    line += f" (outcome: {mem_keys['last_call_disposition']})"
                raisable = (
                    vetted_topic
                    and mem_keys['last_call_reason'].strip().lower()
                    == str(vetted_topic).strip().lower()
                )
                line += (
                    " [may be raised once, as a question]" if raisable
                    else " [BACKGROUND ONLY — closed or old; do not raise it]"
                )
                facts.append(line)
            if mem_keys.get('last_call_summary'):
                facts.append(
                    f"Last call notes (background only): "
                    f"{mem_keys['last_call_summary']}"
                )
            # R4: earlier interactions beyond the latest one (digest is
            # newest-first; entry 0 duplicates the last-call lines above).
            # These are never offer-eligible — at most one topic may ever be
            # raised, and that one is decided above.
            for entry in (mem_contact.get('interaction_digest') or [])[1:3]:
                if not isinstance(entry, dict):
                    continue
                topic = entry.get('reason') or entry.get('summary')
                if not topic:
                    continue
                line = f"Earlier interaction: {topic}"
                if entry.get('disposition'):
                    line += f" (outcome: {entry['disposition']})"
                if entry.get('ended_at'):
                    line += f" — {str(entry['ended_at'])[:10]}"
                facts.append(line + " [BACKGROUND ONLY — do not raise it]")
            if mem_keys.get('open_callback_reason'):
                # F-06 completion: the greeting withholds this until identity
                # is confirmed, but the fact itself was stated plainly here —
                # so the model could still volunteer it to whoever answered.
                facts.append(
                    "They have a pending callback request about: "
                    f"{mem_keys['open_callback_reason']} "
                    "[do NOT say this until the right person has confirmed "
                    "their identity]"
                )
            # R5: the caller-history search tool registers only for
            # specialists (they have _kb_agent_id; triage doesn't need it),
            # only for identified returning callers, and only when the
            # workspace is known — the collection and contact filter are
            # both server-derived, never model input.
            history_tool = bool(
                getattr(agent, '_kb_agent_id', None)
                and mem_contact.get('id')
                # F-16: returning callers only, as documented — a known
                # contact on their first call has no history to search.
                and mem_contact.get('previous_calls')
                and DATABASE_URL
                and (tenant_ctx or {}).get('workspace_id') is not None
            )
            section_bullets = []
            if not getattr(agent, '_is_triage', False):
                # F-10: on a live transfer the receptionist may have already
                # confirmed (or corrected) the caller's name this call — that
                # beats the records below, and re-confirming reads robotic.
                section_bullets.append(
                    "If the receptionist already confirmed this caller during this "
                    "call (customer_name is set in your Customer Context), that "
                    "confirmation STANDS — do not re-confirm identity, and prefer "
                    "the name the caller gave THIS call over the name on file here"
                )
            section_bullets.append(
                "Confirm identity as a question before relying on any of this; "
                "if it's someone else, ignore these records entirely"
            )
            # A-5: the offer licence now matches what was actually vetted.
            if multiple_topics:
                section_bullets.append(
                    "They have more than one recent open topic with us — after they "
                    "confirm, ask an OPEN question ('What can I help with today?'). "
                    "Do NOT name a past topic; guessing between them is worse than asking"
                )
            elif vetted_topic:
                section_bullets.append(
                    f"After they confirm, you may ask ONCE whether this is about "
                    f"{vetted_topic} or something new — offer it as a question, never assert it"
                )
            else:
                section_bullets.append(
                    "Do NOT raise any past topic proactively — nothing here is recent "
                    "and open enough to lead with. Ask what they need today; the lines "
                    "above are only for recognising what they bring up themselves"
                )
            section_bullets += [
                "If they redirect, drop the history immediately and follow their lead",
                "Weave facts in naturally; never recite them or mention records, systems, or caller ID",
                "Never treat any of this as identity verification, and never volunteer it to an unconfirmed caller",
            ]
            if history_tool:
                section_bullets.append(
                    "If they reference a past interaction that isn't listed above, "
                    "use search_caller_history before asking them to re-explain"
                )
            if opening_language:
                # Specialists have no greeting step, so this bullet is their
                # only instruction for the language opening.
                section_bullets.append(
                    f"This caller's documented language is {opening_language} — open in "
                    f"{opening_language}, then immediately offer English in one short "
                    "phrase, because a phone number is not a person. If they answer in "
                    "English (or ask for it), switch to English at once and call "
                    "set_caller_language('en-US')"
                )
            agent.prompt_add_section(
                "Known Caller",
                body=(
                    "Caller ID matched this phone number to existing records. "
                    "These are HINTS from past interactions — the caller's "
                    "identity is NOT verified:\n- " + "\n- ".join(facts)
                ),
                bullets=section_bullets,
            )
        except Exception as e:
            print(f"Warning: known-caller section failed: {e}", flush=True)
            history_tool = False

        if history_tool:
            _hist_collection = f"interactions_ws{tenant_ctx['workspace_id']}"
            _hist_contact_id = mem_contact['id']

            def _handle_search_caller_history(args, raw_data):
                query = (args.get('query') or '').strip()
                if not query:
                    return FunctionResult("Please provide a search query.")
                try:
                    results = do_search(
                        _hist_collection, query, 3, DATABASE_URL,
                        contact_id=_hist_contact_id,
                    )
                except Exception as exc:
                    print(f"search_caller_history failed: {exc}", flush=True)
                    return FunctionResult(
                        "History search is unavailable right now."
                    )
                if not results:
                    return FunctionResult(
                        "No past-interaction records matched that query."
                    )
                lines = []
                for r in results:
                    meta = r.get('metadata') or {}
                    stamp = str(meta.get('ended_at') or '')[:10]
                    lines.append(f"- [{stamp}] {r['content']}")
                return FunctionResult(
                    "Records from this caller's past interactions (weave in "
                    "naturally — never recite verbatim):\n" + "\n".join(lines)
                )

            try:
                agent.define_tool(
                    name="search_caller_history",
                    description=(
                        "Search THIS caller's past interactions with us. Use when "
                        "they reference a previous call, ticket, or promise that "
                        "isn't in your provided context — instead of making them "
                        "re-explain. Do not use for product or policy questions "
                        "(use search_knowledge for those)."
                    ),
                    parameters={
                        "query": {
                            "type": "string",
                            "description": (
                                "What to look for in their history, e.g. "
                                "'vacuum suction troubleshooting steps tried'"
                            ),
                        }
                    },
                    handler=_handle_search_caller_history,
                    required=["query"],
                    # define_tool takes language-keyed fillers (unlike the
                    # @AgentBase.tool decorator's bare list).
                    fillers={"en-US": [
                        "Let me check your history.",
                        "One moment while I look that up.",
                    ]},
                )
            except Exception as e:
                print(
                    f"Warning: search_caller_history registration failed: {e}",
                    flush=True,
                )

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


# Dispositions whose topic must NEVER lead a greeting (F-09: closed or
# non-conversations — proactively reopening them reads wrong).
_CLOSED_DISPOSITIONS = {'resolved', 'no-answer', 'wrong-number', 'spam', 'abandoned'}
_TOPIC_MAX_AGE_DAYS = 14


def _offerable_topic(entry):
    """The reason from one digest/last-interaction entry IF the greeting may
    proactively offer it: has a reason, isn't a closed outcome, and is
    verifiably recent — an unparseable/missing date means unknown age, and
    unknown age never leads (F-09)."""
    if not isinstance(entry, dict):
        return None
    reason = entry.get('reason')
    if not reason:
        return None
    if (entry.get('disposition') or '') in _CLOSED_DISPOSITIONS:
        return None
    ended = entry.get('ended_at')
    if not ended:
        return None
    try:
        from datetime import datetime
        if (datetime.utcnow() - datetime.fromisoformat(str(ended))).days > _TOPIC_MAX_AGE_DAYS:
            return None
    except (ValueError, TypeError):
        return None
    return reason


def _open_in_documented_language(agent, mem_contact, mem_last):
    """Make a returning caller's documented language the one the AI OPENS in.

    The rendered SWML carries ``languages`` in list order and the platform
    treats the first entry as the opening language, so this reorders the
    ephemeral copy's list — the same per-request, deep-copied structure the
    KB/MCP/prompt shaping already mutates, so it cannot leak across calls.

    Returns the language's display name (e.g. ``"Spanish"``) when a switch was
    made, else None. Deliberately no-ops when:
      * there is no documented language, or it is English — English is already
        the default, and a bilingual opening for an English speaker is noise;
      * the agent does not actually SPEAK that language. A voice/language the
        agent was never configured with would render as a bad voice id (a
        runtime voice_error) or silence, which is far worse than opening in
        English. Only the languages added via add_language() are eligible.
    """
    code = (mem_contact or {}).get('preferred_language') \
        or (mem_last or {}).get('caller_language')
    code = (code or '').strip()
    if not code or code.lower().startswith('en'):
        return None
    languages = getattr(agent, '_languages', None)
    if not languages:
        return None
    # Match on the full tag first, then the primary subtag ('es' == 'es-MX'),
    # so a caller recorded as es-MX still opens in the agent's es-ES voice.
    primary = code.split('-')[0].lower()
    match = next(
        (
            lang for lang in languages
            if str(lang.get('code', '')).lower() == code.lower()
        ),
        None,
    ) or next(
        (
            lang for lang in languages
            if str(lang.get('code', '')).split('-')[0].lower() == primary
        ),
        None,
    )
    if match is None or languages[0] is match:
        return match.get('name') if match is not None else None
    languages.remove(match)
    languages.insert(0, match)
    return match.get('name')


def _caller_greeting_hint(contact, last, open_callback=None, direction=None):
    """Compact hint configure_triage_queues uses to shape the greeting step.

    Returns None for unknown callers, so the greeting stays exactly as it
    was before R1. Topic selection (F-09): only recent, OPEN topics are
    offerable; exactly one candidate → offer it as a question; two or more
    distinct candidates → flag multiple_topics so the greeting asks an open
    question instead of guessing.
    """
    if not contact:
        return None
    hint = {
        'name': contact.get('name') if contact.get('name_known') else None,
        'previous_calls': contact.get('previous_calls') or 0,
    }
    candidates = []
    if last:
        candidates.append(last)
    candidates.extend(
        e for e in (contact.get('interaction_digest') or []) if isinstance(e, dict)
    )
    topics, seen = [], set()
    for entry in candidates:
        reason = _offerable_topic(entry)
        if reason and reason.strip().lower() not in seen:
            seen.add(reason.strip().lower())
            topics.append(reason)
    if len(topics) == 1:
        hint['last_reason'] = topics[0]
    elif len(topics) >= 2:
        hint['multiple_topics'] = True
    if open_callback and open_callback.get('reason'):
        hint['callback_reason'] = open_callback['reason']
        # outbound + pending callback = we are dialing THEM back;
        # inbound + pending callback = they called before we got to it.
        hint['callback_dialed'] = direction == 'outbound'
    if hint['name'] or hint['previous_calls'] or hint.get('callback_reason'):
        return hint
    return None


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


def configure_triage_queues(agent, queues, caller=None):
    """(Re)build everything queue-shaped on a triage agent.

    ``caller`` is the optional _caller_greeting_hint dict (R1): when present
    the greeting step confirms the known caller instead of interrogating
    them, and callback dials open by returning the call. Boot renders and
    unknown callers pass None and get the original greeting verbatim.

    Contexts, the slug→AI-route transfer map, speech hints, and the
    post-prompt department enum all derive from the queue list. Runs at
    BOOT on the persistent agent (template queues from get_active_queues)
    and PER REQUEST on the ephemeral copy with the call's workspace's
    queues from call-context (§7.2) — which is also the AI-06 fix: queue
    config changes apply to the next call, no container restart.

    On the ephemeral copy the deep-copied boot ContextBuilder is discarded
    and rebuilt fresh (add_context raises on duplicates, so re-adding onto
    the copied builder would crash).
    """
    # Defensive de-dup: duplicate slugs (or a slug literally named
    # 'default') would raise in add_context and kill the render. Keep
    # first occurrence, order preserved.
    seen = set()
    clean = []
    for q in queues or []:
        slug = q.get('slug')
        if not slug or slug == 'default' or slug in seen:
            continue
        seen.add(slug)
        clean.append(q)
    queues = clean

    queue_slugs = [q['slug'] for q in queues]
    agent._queue_ai_map = {
        q['slug']: q.get('ai_agent_route') or f"/{q['slug']}-ai" for q in queues
    }

    # Speech recognition hints. add_hints only appends, and the ephemeral
    # copy already carries the boot queue hints (deep-copied from the
    # persistent agent), so a naive re-add would duplicate every queue hint
    # on every render. Add, then de-dup _hints in place (order-preserving)
    # so the rendered SWML matches the boot shape exactly.
    agent.add_hints([q['display_name'] for q in queues] + queue_slugs)
    try:
        seen_h = set()
        deduped = []
        for h in agent._hints:
            marker = h if isinstance(h, str) else repr(h)
            if marker in seen_h:
                continue
            seen_h.add(marker)
            deduped.append(h)
        agent._hints = deduped
    except Exception:
        pass

    # Department enum for the post_prompt summary
    dept_options = '/'.join(queue_slugs + ['unknown'])
    agent.set_post_prompt(
        f'Summarize this call as a JSON object: {{"customer_name": "name or null", '
        f'"department": "{dept_options}", '
        '"reason": "brief reason for call", '
        '"outcome": "transferred_to_human/transferred_to_ai/abandoned", '
        '"notes": "any important details", '
        f'{WRAP_UP_POST_PROMPT_FIELDS}}}'
    )
    agent.set_post_prompt_llm_params(temperature=0.1, top_p=0.9)

    # Fresh ContextBuilder — discard whatever the agent had (boot: nothing;
    # ephemeral copy: the deep-copied template contexts).
    agent._contexts_builder = None
    agent._contexts_defined = False
    contexts = agent.define_contexts()

    # ============================================================
    # TRIAGE CONTEXT (default) - Greeting and routing
    # ============================================================
    triage_ctx = contexts.add_context("default")

    # Build department info for step prompts
    queue_descriptions = []
    for q in queues:
        desc = q.get('description', '')
        queue_descriptions.append(
            f"{q['display_name']} ({q['slug']}): {desc}" if desc
            else f"{q['display_name']} ({q['slug']})"
        )

    dept_list_text = "\n".join(queue_descriptions)
    route_instructions = "\n".join(
        [f"- {q['display_name']}-related: switch to the '{q['slug']}' context" for q in queues]
    )

    dept_names = [q['display_name'] for q in queues]
    if len(dept_names) > 1:
        dept_menu = ', '.join(dept_names[:-1]) + ' or ' + dept_names[-1]
    else:
        dept_menu = dept_names[0] if dept_names else 'general assistance'

    # Step 1: Greet and get name (also detects caller's language).
    # Known callers (R1) get a confirm-not-interrogate variant; callback
    # dials open by returning the call. Unknown callers get the original
    # greeting text verbatim.
    greeting_goal = (
        "Welcome the caller and get their name. Introduce yourself as Sam. "
        "Be warm but brief — this should take one exchange. "
        "Detect their language from their first words and respond in kind.")
    greeting_criteria = (
        "The customer has stated their name and you have called set_caller_language")
    # Language memory: the caller's documented language is now the voice this
    # call OPENS in (the agent reordered its language list). Say the first line
    # in that language, then offer English in one short phrase — the number is
    # not the person, and a wrong guess here is a comprehension failure, not
    # merely an awkward one. Composed as a suffix so it works with every
    # greeting variant below (unknown / known name / callback).
    lang = (caller or {}).get('opening_language')
    language_clause = (
        (
            f" IMPORTANT — LANGUAGE: this caller's records say they speak {lang}, and "
            f"you are opening in {lang}. Say your greeting in {lang} first, then add ONE "
            f"short English phrase offering English (e.g. 'or English, if you prefer?'). "
            f"If they reply in English or ask for English, switch immediately, finish the "
            f"rest of the call in English, and call set_caller_language('en-US'). If they "
            f"continue in {lang}, stay in {lang} and call set_caller_language with that "
            f"language's code. Never make them ask twice."
        ) if lang else ""
    )
    if caller and caller.get('callback_reason') and caller.get('callback_dialed'):
        # F-06: confirm the PERSON before disclosing WHY we're calling — a
        # callback reason can be sensitive, and whoever answers the phone is
        # not necessarily who requested the call.
        who = (
            f" {caller['name']}" if caller.get('name')
            else " the person who requested a callback from us"
        )
        greeting_goal = (
            "This is a callback WE are placing at the customer's request. "
            "Introduce yourself as Sam from the company, and FIRST confirm you "
            f"have reached{who} — BEFORE saying anything about why you're calling. "
            "Only once the right person has confirmed, tell them you're returning "
            f"their call about: {caller['callback_reason']}, and check it's a good "
            "time. If it's someone else or they won't confirm, do NOT state the "
            "reason for the call — just say you'll try again later. If it's a bad "
            "time, apologize briefly and offer to call back. "
            "Detect their language from their first words and respond in kind.")
        greeting_criteria = (
            "You have confirmed you reached the right person and you have called "
            "set_caller_language")
    elif caller and caller.get('name'):
        topic = caller.get('last_reason') or caller.get('callback_reason')
        if caller.get('multiple_topics'):
            # F-09: several plausible open topics — never guess one.
            offer = (
                " They have more than one recent open topic with us — once they "
                "confirm, ask an open question like 'What can I help with today?' "
                "instead of guessing which topic they're calling about.")
        elif topic:
            offer = (
                f" Once they confirm, you may ask ONCE whether they're calling about "
                f"{topic} or something new — offer it as a question, never assume.")
        else:
            offer = ""
        greeting_goal = (
            f"Welcome the caller back. Caller ID suggests this may be {caller['name']}, "
            "but that is unverified — confirm with a short, warm question like "
            f"'Am I speaking with {caller['name']}?' instead of asking for their name cold."
            f"{offer} If it turns out to be someone else, just welcome them and ask their "
            "name as usual. Introduce yourself as Sam. "
            "Detect their language from their first words and respond in kind.")
        greeting_criteria = (
            "The caller's identity is confirmed or corrected and you have called "
            "set_caller_language")
    # Applies to whichever variant was selected above (empty when there is no
    # documented non-English language we speak).
    greeting_goal += language_clause
    triage_ctx.add_step("greeting") \
        .add_section("Goal", greeting_goal) \
        .add_section("Handling Eager Callers",
            "If the caller gives you their name AND mentions what they need in the same breath, "
            "great — note both. You can skip asking about the department in the next step.") \
        .set_step_criteria(greeting_criteria) \
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
        self._mcp_agent_id = 'receptionist'  # MCP gateways attach per-request (callback), not at boot

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

        # Fetch active queues from backend (dynamic at startup). These are
        # the TEMPLATE queues — per-call tenant queues rebuild this whole
        # block on the ephemeral copy via configure_triage_queues (§7.2).
        self._is_triage = True
        self._inbound_caller_memory = True  # R1: consume call-context contact block
        self.add_hints(["SignalWire"])  # brand hint, queue-independent
        queues = get_active_queues()

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

        # Everything queue-shaped (contexts, routing map, hints, post-prompt
        # enum) builds through the same function the per-request callback
        # uses with tenant queues.
        configure_triage_queues(self, queues)

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
        # Forward the backend-minted call-context token (§7.1) so the
        # specialist resolves THIS workspace's config. The agent only
        # forwards what it received at render — it never mints tokens.
        ctk = global_data.get('ctk', '')

        specialist_route = self._queue_ai_map.get(department, f"/{department}-ai")
        transfer_url = f"{base_url}{specialist_route}"
        params = []
        if conf:
            params.append(f"conf={conf}")
        if call_db_id:
            params.append(f"call_db_id={call_db_id}")
        if ctk:
            params.append(f"ctk={ctk}")
        if params:
            transfer_url += '?' + '&'.join(params)

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
        # Catalog facts — what we sell, prices, stock, the most popular
        # product — live in the shop tools (mcp_demoshop_*), not the document
        # KB. Route selection there, and make a KB miss redirect instead of
        # letting the model announce it "can't access pricing" (the
        # 2026-08-10 fred_returning_caller flap).
        self._kb_tool_description = (
            "Search company documents for product specs, feature details, "
            "policies, or comparisons. NOT for what we sell, prices, stock, "
            "or which product is most popular — the shop catalog tools "
            "answer those with live data."
        )
        self._kb_no_results_message = (
            "No document matched '{query}'. Do NOT tell the caller you can't "
            "access information. If they asked what we sell, which product "
            "is most popular, or a price, call the product catalog tool "
            "(mcp_demoshop_list_products) and answer from its data. For "
            "anything else, say you don't have that detail on hand and "
            "offer to connect a human sales rep."
        )
        self._mcp_agent_id = 'sales-ai'  # MCP gateways attach per-request (callback), not at boot
        self._inbound_caller_memory = True  # R1: consume call-context contact block

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
                "When you give a price, name the product and its price together in your very "
                "first sentence — callers often jump in after one sentence, and that first "
                "sentence must stand alone as a complete answer",
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
                "If the caller asks a direct factual question — a price, what we sell, "
                "what's most popular, availability — answer it first, in one short "
                "sentence, before any discovery questions. A researching caller should "
                "never have to wait through qualifying questions for a simple fact",
                "What we sell, prices, stock, and 'most popular' come from the product "
                "catalog tool (mcp_demoshop_list_products) — answer from its live data, "
                "never from memory or guesswork",
                "Beyond the quick facts, understand their situation — what problem are "
                "they trying to solve?",
                "Ask about their current setup, team size, or use case to tailor your "
                "recommendations — after their question is answered, one question at a time",
                "Match their needs to specific products or features",
                "Use the search_knowledge tool for document details the catalog doesn't cover"
            ]
        )

        self.prompt_add_section(
            "When to Escalate",
            body="Use the escalate_to_human tool when:",
            bullets=[
                "They're ready to make a purchase or need a formal quote",
                "They need custom pricing or contract terms",
                "They specifically ask to speak with a person",
                "The question is about billing or account-specific details you can't access",
                "Always fill work_summary with what you've covered so far — the rep reads it before joining and the customer should never have to repeat themselves"
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
            "work_summary": {
                "type": "string",
                "description": (
                    "2-4 sentences FOR THE HUMAN REP taking over, current as of right now: "
                    "what the customer needs, what you already discussed or recommended and "
                    "how they responded, and the logical next step. They should not have to "
                    "re-ask anything you already covered."
                ),
            },
        },
        required=["reason", "work_summary"],
        fillers=["Let me get a sales representative for you.", "One moment."],
    )
    def escalate_to_human(self, args, raw_data):
        """Sales specialist's hand-off back to a human rep."""
        global_data = raw_data.get('global_data', {})
        work_summary = args.get("work_summary", "")
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
                # R7 (G9): the specialist's CURRENT work, captured at transfer
                # time. ai_summary is the key AgentContextCard + the pre-join
                # whisper already render — no frontend change needed.
                'work_summary': work_summary,
                'ai_summary': work_summary,
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
        self._mcp_agent_id = 'support-ai'  # MCP gateways attach per-request (callback), not at boot
        self._inbound_caller_memory = True  # R1: consume call-context contact block

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
                "Always fill work_summary with the steps already tried and their results — the specialist reads it before joining and must never repeat a step",
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
            "work_summary": {
                "type": "string",
                "description": (
                    "2-4 sentences FOR THE HUMAN SPECIALIST taking over, current as of "
                    "right now: the issue, every troubleshooting step already attempted "
                    "and its result, and your proposed next step. They should never "
                    "repeat a step you already tried."
                ),
            },
        },
        required=["reason", "work_summary"],
        fillers=["Let me get a support specialist for you.", "One moment."],
    )
    def escalate_to_human(self, args, raw_data):
        """Support specialist's hand-off back to a human rep."""
        global_data = raw_data.get('global_data', {})
        work_summary = args.get("work_summary", "")
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
                # R7 (G9): the specialist's CURRENT work, captured at transfer
                # time. ai_summary is the key AgentContextCard + the pre-join
                # whisper already render — no frontend change needed.
                'work_summary': work_summary,
                'ai_summary': work_summary,
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
        self._mcp_agent_id = 'outbound-sales'  # MCP gateways attach per-request (callback), not at boot

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
        self._mcp_agent_id = 'outbound-support'  # MCP gateways attach per-request (callback), not at boot

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

    # Prime the KB assignment + template-MCP caches now that the backend is
    # reachable (the triage agent's get_active_queues just blocked on it) so
    # the very first calls bind to admin-assigned collections and gateways
    # instead of fallbacks.
    prime_kb_assignments()
    prime_mcp_gateways()

    # Register agents
    server.register(triage, '/receptionist')
    server.register(sales_ai, '/sales-ai')
    server.register(support_ai, '/support-ai')
    server.register(outbound_sales, '/outbound-sales')
    server.register(outbound_support, '/outbound-support')

    print('\nAuthentication:')
    print('  Basic Auth: configured')
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
    print('\nDatabase: configured' if DATABASE_URL else '\nDatabase: NOT CONFIGURED')
    print('\nStarting agent server on port 8080...\n')

    server.run()
