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

# Cosine-similarity floor for search_knowledge results. Nearest-neighbour
# search ALWAYS returns its k nearest rows, however far away they are — so
# without a floor an off-topic question ("what time do you close?", "how do
# I file my taxes?") comes back as five confident-looking knowledge-base
# excerpts, under a preamble telling the model to answer from them. That is
# fabrication fuel, and it also means the no-results guidance — which tells
# the model to offer a human instead of guessing — could never fire.
#
# Measured against the seeded product KB (2026-08-11, all-MiniLM-L6-v2):
# on-topic questions scored 0.49-0.69, clearly off-topic ones 0.05-0.31.
# The gap is real but not wide, so this sits nearer the noise: a marginal
# excerpt the model can ignore costs less than wrongly telling a caller we
# have nothing. Re-measure if the embedding model changes — the scale of
# these numbers is model-specific, not universal.
KB_MIN_SCORE = 0.30

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
# Returned when a call DOES name a workspace but we could not reach the
# backend to find out which. Distinct from None ("no tenant context asked
# for"), because the two must not be handled the same way: None means run on
# the template config, and doing that for a real tenant call binds the DEFAULT
# workspace's KB collection and MCP gateways — i.e. serves one workspace's
# data, and its gateway credentials, to another. A transient backend blip is
# exactly when that would happen.
_CTX_UNAVAILABLE = object()

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
            payload = _CTX_UNAVAILABLE
    except Exception as e:
        print(f"Warning: call-context fetch failed for call {call_db_id}: {e}", flush=True)
        payload = _CTX_UNAVAILABLE
    with _ctx_cache_lock:
        if len(_ctx_cache) > 512:
            _ctx_cache.clear()
        _ctx_cache[key] = (payload, time.time())
    return payload


def attach_knowledge_search(agent, collection_override=None):
    """Register search_knowledge on the per-request ephemeral agent.

    Called from the dynamic-config callback, which the SDK runs on a fresh
    ephemeral copy for BOTH SWML renders and SWAIG executions — so the tool
    is registered with the current collection at execution time too.

    The tool must NOT also be registered at boot: _create_ephemeral_copy
    re-loads boot registrations into each copy first, so a boot-registered
    search_knowledge would make this define_tool raise "already exists" —
    caught below — leaving every call bound to the boot collection.

    Agents opt in by setting ``_kb_agent_id`` (assignment slug) and
    optionally ``_kb_fallback_collection`` in __init__.
    ``collection_override`` is the per-call tenant assignment from
    call-context (physical collection name); without it the template
    cache / fallback applies.

    This used to be ``add_skill("native_vector_search")``, and that skill
    could not answer a single question against our collections. It picks
    the query-time embedding model from ``SearchEngine.config
    .get('embedding_model')``, but the SDK's own pgvector backend
    publishes that value under the key ``model_name`` — so the lookup
    always returned None and the query fell through to the SDK default,
    all-mpnet-base-v2 at 768 dimensions. Our chunk tables are written by
    do_reindex with all-MiniLM-L6-v2 at 384. Every call raised
    ``different vector dimensions 384 and 768`` inside the skill, which
    swallowed it into "I encountered an issue while searching".

    So we query through :func:`do_search` instead — the same function that
    already backs search_caller_history, using the same model that wrote
    the index. That also removes the hidden dependency on an SDK default
    matching ours, which is what made this silent in the first place.
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

    def _miss(query):
        # str.format on an agent-authored template: a stray brace in an
        # override would raise mid-call and surface to the caller as a
        # failed tool. The unformatted text is still usable guidance.
        try:
            return FunctionResult(no_results_message.format(query=query))
        except Exception:
            return FunctionResult(no_results_message)

    def _handle_search_knowledge(args, raw_data):
        query = (args.get('query') or '').strip()
        if not query:
            return FunctionResult("Please provide a search query.")
        try:
            results = do_search(collection, query, 5, DATABASE_URL)
        except Exception as exc:
            print(f"search_knowledge failed for {collection}: {exc}", flush=True)
            # Same shape as a miss on purpose: the model's next move should
            # be to offer a human, not to tell the caller our database is
            # down. An empty index and a broken index look identical to the
            # caller, and both are our problem, not theirs.
            return _miss(query)
        results = [r for r in results if r.get('score', 0) >= KB_MIN_SCORE]
        if not results:
            return _miss(query)
        lines = []
        for r in results:
            section = r.get('section') or r.get('filename') or ''
            prefix = f"[{section}] " if section else ""
            lines.append(f"- {prefix}{r['content']}")
        return FunctionResult(
            "Knowledge base results (answer from these — do not invent "
            "details they don't contain):\n" + "\n".join(lines)
        )

    try:
        agent.define_tool(
            name="search_knowledge",
            description=description,
            parameters={
                "query": {
                    "type": "string",
                    "description": "What to look up, in the caller's own terms",
                }
            },
            handler=_handle_search_knowledge,
            required=["query"],
            # Per-tool fillers (filler policy: opt-in only — no language-level
            # function_fillers anywhere). A KB lookup is a real 1-2s wait where
            # the persona plausibly speaks. define_tool takes language-keyed
            # fillers, unlike the @AgentBase.tool decorator's bare list.
            fillers={"en-US": [
                "Let me check on that for you.",
                "One moment while I look that up.",
            ]},
        )
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


def _resolve_mcp_entries(agent_id, call_ctx):
    """Which gateway rows apply to this agent on this call, and where from."""
    if call_ctx is not None and call_ctx.get('mcp_gateways') is not None:
        return [
            g for g in call_ctx['mcp_gateways']
            if agent_id in (g.get('bound_agent_ids') or [])
        ], f"workspace {call_ctx.get('workspace_id')}"
    return get_template_mcp_gateways(agent_id), 'template'


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
        return []

    entries, source = _resolve_mcp_entries(agent_id, call_ctx)

    import time
    # Only the gateways that actually came up. Callers (preload_mcp_context)
    # must not re-probe a gateway whose health check just failed or that the
    # negative cache is deliberately skipping — that turns one timeout per
    # window back into two per render.
    attached = []
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
            # The SDK only understands object-shaped services; a bare string
            # in services_filter makes register_tools raise and takes the
            # whole gateway down with it. Normalize on a COPY so the stored
            # config (and the cache key derived from it) stays as configured.
            skill_config = dict(config)
            normalized_services = _normalized_services(config)
            if normalized_services:
                skill_config['services'] = normalized_services
            agent.add_skill("mcp_gateway", skill_config)
            attached.append(entry)
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
    return attached


# ---------------------------------------------------------------------------
# Preloaded reference data — putting ground truth IN the prompt, because
# having a tool available is not the same as the model choosing to call it.
#
# Live call 78 (2026-08-17): asked "what products do you offer and what do
# they cost", the sales specialist answered "the smart home hub for two
# hundred dollars, wireless earbuds for one hundred fifty dollars, and a
# fitness tracker for seventy five dollars" — WITHOUT calling any catalog
# tool. Two of those products have never existed; the third's price was
# wrong. It then repeated the invented $200 even after find_product returned
# a different product and price, because once a number is in the
# conversation the model defends it.
#
# Typed tool returns are necessary but not sufficient: they only help if the
# model calls the tool BEFORE committing to an answer. The catalog is small,
# it's the single most common sales question, and it is authoritative — so it
# belongs in the context, where there is no gap to invent into.
#
# Opt-in per agent (``_preload_mcp_tool``) rather than assumed, because
# DemoShop is the bundled demo: a cloner points these agents at their own MCP
# server and names their own catalog tool, or sets nothing and loses nothing.
# ---------------------------------------------------------------------------

_PRELOAD_TTL_SECONDS = 60
_preload_cache: dict = {}
_preload_inflight: dict = {}
_preload_lock = threading.Lock()


def _preload_cache_key(entry, config, tool_name):
    """Cache identity for one preload result.

    This process serves every workspace, so the key has to name the TENANT
    CONFIGURATION, not just the URL. Two workspaces can point at the same
    hosted gateway with different credentials and get different catalogs;
    keying on (url, tool) alone served whichever rendered first to both — one
    tenant's product list inside another tenant's prompt.

    Credentials are hashed rather than stored: this dict is process-global and
    long-lived, and a cache key is not a place to keep a password.
    """
    secret = f"{config.get('auth_user') or ''}:" \
             f"{config.get('auth_password') or ''}:" \
             f"{config.get('auth_token') or ''}"
    return (
        entry.get('id'),
        entry.get('name'),
        config.get('gateway_url'),
        json.dumps(_normalized_services(config), sort_keys=True),
        hashlib.sha256(secret.encode()).hexdigest()[:16],
        tool_name,
    )


def _gateway_auth(config):
    """(auth tuple, headers) for a gateway config."""
    headers = {'Content-Type': 'application/json'}
    if config.get('auth_token'):
        headers['Authorization'] = f"Bearer {config['auth_token']}"
        return None, headers
    if config.get('auth_user') is not None:
        return (config.get('auth_user') or '',
                config.get('auth_password') or ''), headers
    return None, headers


def _normalized_services(config):
    """``services`` as a list of ``{"name", "tools"}`` dicts.

    McpGatewayConfig.services_filter stores bare strings OR objects, but the
    SDK skill only handles the object form — register_tools calls
    ``service_config.get("name")`` on each entry, so a bare string raises
    AttributeError, the whole gateway is caught as "failed to attach", and the
    agent silently loses both its MCP tools and this preload. Normalizing
    once, here, is what keeps a documented config shape from disabling the
    feature it configures.
    """
    normalized = []
    for service in config.get('services') or []:
        if isinstance(service, str) and service:
            normalized.append({'name': service, 'tools': '*'})
        elif isinstance(service, dict) and service.get('name'):
            entry = dict(service)
            entry.setdefault('tools', '*')
            normalized.append(entry)
    return normalized


def _service_exposes_tool(service, tool_name):
    """Whether the ADMIN's filter lets this service offer ``tool_name``.

    The gateway exposing a tool is not permission to call it: ``{"name":
    "catalog", "tools": ["lookup_order"]}`` is an operator deciding this agent
    may not use anything else. Preloading list_products anyway would walk
    straight past that decision.
    """
    tools = service.get('tools', '*')
    if tools == '*' or tools is None:
        return True
    if isinstance(tools, str):
        return tools == tool_name
    return tool_name in tools


def _configured_service_names(config):
    return [s['name'] for s in _normalized_services(config)]


def _resolve_service_for_tool(config, tool_name):
    """Which service on this gateway exposes ``tool_name``.

    One configured service is taken at its word. Otherwise ask the gateway,
    because assuming 'demoshop' silently disables preloading for every cloner
    who runs their own MCP server under any other name.
    """
    import requests

    services = _normalized_services(config)
    if services:
        # Only services the operator's filter actually permits this tool on.
        permitted = [s for s in services if _service_exposes_tool(s, tool_name)]
        if not permitted:
            return None
        if len(permitted) == 1:
            return permitted[0]['name']
        names = [s['name'] for s in permitted]
    else:
        names = []

    gateway_url = (config.get('gateway_url') or '').rstrip('/')
    auth, headers = _gateway_auth(config)
    timeout = config.get('request_timeout', 5)
    if not names:
        # Nothing configured: ask the gateway which services exist. Discovery
        # answers "which service owns this tool", never "may I use it" — that
        # question was already answered above.
        resp = requests.get(f"{gateway_url}/services", auth=auth,
                            headers=headers, timeout=timeout)
        resp.raise_for_status()
        listing = resp.json()
        names = list(listing.keys()) if isinstance(listing, dict) else [
            s.get('name') for s in listing if isinstance(s, dict)
        ]

    for name in names:
        if not name:
            continue
        resp = requests.get(f"{gateway_url}/services/{name}/tools", auth=auth,
                            headers=headers, timeout=timeout)
        if not resp.ok:
            continue
        payload = resp.json()
        tools = payload.get('tools', payload) if isinstance(payload, dict) else payload
        exposed = {
            t.get('name') for t in tools if isinstance(t, dict)
        } if isinstance(tools, list) else set()
        if tool_name in exposed:
            return name
    return None


def _call_mcp_tool(config, tool_name, arguments=None, service=None):
    """Invoke one tool on an MCP gateway over its HTTP API.

    Reads the same config the SDK skill gets (McpGatewayConfig.to_skill_config):
    gateway_url, auth_user/auth_password or auth_token, services, and the
    deliberately short request_timeout that keeps a dead gateway from stalling
    a SWML render.
    """
    import requests

    gateway_url = (config.get('gateway_url') or '').rstrip('/')
    if not gateway_url:
        return None
    if service is None:
        service = _resolve_service_for_tool(config, tool_name)
    if not service:
        return None

    auth, headers = _gateway_auth(config)
    resp = requests.post(
        f"{gateway_url}/services/{service}/call",
        json={
            'tool': tool_name,
            'arguments': arguments or {},
            # Read-only and shared; the gateway keys sessions by this.
            'session_id': 'agent-context-preload',
        },
        auth=auth,
        headers=headers,
        timeout=config.get('request_timeout', 5),
    )
    resp.raise_for_status()
    payload = resp.json()
    result = payload.get('result', payload)
    if isinstance(result, str):
        result = json.loads(result)
    return result


def _format_catalog_section(catalog) -> str:
    """The catalog as the model should read it: closed, priced, quotable."""
    products = catalog.get('products') or []
    if not products:
        return ''
    most_popular = (catalog.get('most_popular') or {}).get('name')
    lines = []
    for product in products:
        line = f"- {product.get('name')} — {product.get('price')}"
        availability = product.get('availability')
        if availability:
            line += f" ({availability})"
        if most_popular and product.get('name') == most_popular:
            line += "  ← most popular / best seller"
        lines.append(line)
    return (
        "This is the COMPLETE live catalog — every product this company "
        "sells, with its current price:\n\n"
        + "\n".join(lines)
        + "\n\nQuote these prices exactly as written. Never name, describe, "
        "or price a product that is not on this list — not when listing "
        "options, not when suggesting alternatives, not when offering the "
        "caller a choice. If someone asks about anything else, tell them we "
        "don't carry it and name the nearest thing we do sell. Use "
        "find_product for stock or details on a specific item."
    )


def preload_mcp_context(agent, entries):
    """Inject an agent's declared reference tool output into its prompt.

    Best-effort throughout: this is an optional head start, never a
    prerequisite. A gateway that is slow, down, or answering in a shape we
    don't understand costs the call the section and nothing else — the model
    still has every tool it always had.
    """
    tool_name = getattr(agent, '_preload_mcp_tool', None)
    if not tool_name or not entries:
        return False

    for entry in entries:
        config = entry.get('config') or {}
        cache_key = _preload_cache_key(entry, config, tool_name)
        section = _preloaded_section(cache_key, config, tool_name)
        if section:
            agent.prompt_add_section(
                getattr(agent, '_preload_section_title', 'Product Catalog'),
                section,
            )
            return True
    return False


def _preloaded_section(cache_key, config, tool_name):
    """The cached section for this key, fetching it once across threads.

    Single-flight: the lock alone covered lookup and storage but not the
    gateway request between them, so every concurrent miss made its own call.
    """
    now = time.time()
    with _preload_lock:
        cached = _preload_cache.get(cache_key)
        if cached and now - cached[0] < _PRELOAD_TTL_SECONDS:
            return cached[1]
        leader = _preload_inflight.get(cache_key)
        owner = leader is None
        if owner:
            leader = threading.Event()
            _preload_inflight[cache_key] = leader

    if not owner:
        # Bounded, because a wedged leader must not hold up a SWML render.
        leader.wait(timeout=config.get('request_timeout', 5) + 1)
        with _preload_lock:
            cached = _preload_cache.get(cache_key)
        # On timeout: take whatever is there, stale or not, and otherwise go
        # without. Starting our own fetch here would make every follower a
        # second owner and reintroduce the pile-up single-flight exists to
        # prevent — and discovery can legitimately outlast one request_timeout
        # since it may issue several sequential requests before the call.
        return cached[1] if cached else ''

    # Owner. try/finally, not try/except: the formatter runs on whatever the
    # tool returned, and a custom preload tool answering with an unexpected
    # shape raised THERE, outside the handler, leaving the event unset and the
    # key in _preload_inflight forever. Every later render then waited the
    # full timeout and repeated the failure.
    section = ''
    try:
        section = _format_catalog_section(_call_mcp_tool(config, tool_name) or {})
    except Exception as e:
        print(
            f"Catalog preload skipped for {_PRELOAD_TTL_SECONDS}s "
            f"({tool_name} via {config.get('gateway_url')!r}): {e}",
            flush=True,
        )
        section = ''
    finally:
        with _preload_lock:
            # Cache the failure as well as the success, so a dead gateway
            # costs one attempt per window rather than one per render.
            _preload_cache[cache_key] = (now, section)
            _preload_inflight.pop(cache_key, None)
        leader.set()
    return section


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
    # Fail CLOSED when this call named a workspace we could not resolve. The
    # template config is the default workspace's real KB collection and real
    # MCP gateways (credentials included), so falling back to it during a
    # backend blip hands one tenant's data to another. No tenant data at all
    # is a degraded call; the wrong tenant's data is an incident.
    tenant_unavailable = tenant_ctx is _CTX_UNAVAILABLE
    if tenant_unavailable:
        tenant_ctx = None
        print(
            f"call-context unavailable for call {call_db_id} — running "
            "without KB or external tools rather than on template config",
            flush=True,
        )

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
    if not tenant_unavailable:
        attach_knowledge_search(agent, collection_override=kb_override)

    # MCP gateway skills — per-request since Phase 4 (boot registration
    # removed; see attach_mcp_gateways for the duplicate-skip rationale).
    mcp_entries = (
        [] if tenant_unavailable else attach_mcp_gateways(agent, tenant_ctx)
    )

    # ...and, for agents that declare one, that gateway's reference data
    # straight into the prompt. Having the catalog tool available did not
    # stop the specialist inventing products and prices — see
    # preload_mcp_context.
    try:
        preload_mcp_context(agent, mcp_entries)
    except Exception as e:
        print(f"Warning: context preload failed (non-fatal): {e}", flush=True)

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
    'call: what the caller wanted, how it ended, any recommended follow-up", '
    # Safety net for calls where set_caller_language never fired: the
    # post-prompt webhook seeds Call.caller_language from this (normalized
    # + shape-validated in code) only when the column is still empty.
    '"caller_language": "BCP-47 code of the language the CALLER mainly '
    'spoke, e.g. en-US, es-ES, fr-FR"'
)


# Department intake, asked through the SDK's native gather_info system rather
# than as hand-rolled prompt questions. gather_info presents one question at a
# time via step-instruction re-injection and writes typed answers into
# global_data, producing ZERO tool_call/tool_result entries in LLM-visible
# history — which is why it beats asking through tools when intake is more than
# one field.
#
# TWO questions per department, deliberately. Pre-queue intake is where call
# centers lose callers, so this asks only what triage does not already know:
# name, language, department and a free-text reason are captured upstream, and
# re-asking any of them is what the old single "what do you need help with"
# step was already trying to avoid.
#
# Keyed by queue slug with a generic fallback, so a deployment that invents its
# own queue still gets sane intake. Making these editable per queue in the
# admin UI is the natural next step (same shape as the runtime queue refresh
# noted as AI-06-future) — it needs a backend field to read them from first.
_DEPARTMENT_INTAKE = {
    'sales': [
        {'key': 'interest',
         'question': "What product or service are you interested in?"},
        {'key': 'existing_customer',
         'question': "Are you already a customer with us?",
         'type': 'boolean'},
    ],
    'support': [
        {'key': 'product',
         'question': "Which product or service is this about?"},
        {'key': 'issue_summary',
         'question': "In one sentence, what is happening?"},
    ],
    'billing': [
        # confirm=True makes the model read the answer back and get explicit
        # agreement before submitting. Worth the extra turn on an identifier:
        # long digit strings are exactly what ASR gets wrong, and a wrong
        # account number sends the agent to the wrong record.
        {'key': 'account_ref',
         'question': "What is the account or invoice number on the bill?",
         'confirm': True},
        {'key': 'billing_issue',
         'question': "What about the charge looks wrong?"},
    ],
}

_DEFAULT_INTAKE = [
    {'key': 'details', 'question': "Briefly, what do you need help with?"},
]

# Unlocked on EVERY intake question, and the most important line here.
# gather mode forcibly deactivates all of the step's other functions —
# change_context and next_step included — so a caller who says "just get me a
# person" mid-intake cannot be given one unless the transfer tools are named
# per question. That is the same trap just fixed in the greeting step, except
# enforced by the runtime instead of by a prompt, which no wording could
# talk its way out of.
_INTAKE_ESCAPES = ["transfer_to_human", "transfer_to_ai_specialist"]


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
    # A caller asking for a person outranks identifying them. Whichever
    # variant above is in play, an explicit request for a human - or a
    # refusal to give a name - has to be able to LEAVE this step:
    # transfer_to_human does not exist until offer_transfer, three steps
    # away, so a greeting criteria the caller will not satisfy is a caller
    # who can never reach a person at all. Live run 2026-08-19: a caller
    # asked eleven times and was asked for their name every time, and the
    # model was not being stubborn - it had no tool to comply with.
    #
    # Relaxing the PROMPT here cannot cause a spurious transfer: whether
    # the request was real is decided in code, by _human_request_evidence
    # inside transfer_to_human, which routes to the AI specialist instead
    # when that evidence is ABSENT.
    greeting_criteria += (
        " - OR the caller has asked to speak with a person, or has declined"
        " to give their name. Identity is preferred, never required."
        " OR you have already asked twice without getting a usable answer -"
        " in that case route with whatever you have rather than asking again.")
    triage_ctx.add_step("greeting") \
        .add_section("Goal", greeting_goal) \
        .add_section("Routing From Here",
            "The moment you know which department fits — whether the caller volunteered it "
            "with their name, described a need that clearly belongs somewhere, or confirmed "
            "a topic you offered — call route_to_department with the department and a short "
            "note of what they need. Never tell a caller you'll connect them without calling "
            "it: saying 'let me connect you' does nothing on its own.\n"
            "Departments:\n" + dept_list_text) \
        .add_section("If You Are Getting Nowhere",
            "Some callers are distracted, hard to hear, or answer with something "
            "that is not an answer. Ask at most TWICE. After that, stop asking and "
            "call route_to_department with your best reading of what they need"
            + (f" (use '{queues[0]['slug']}' if you have nothing to go on)"
               if queues else "")
            + ". Repeating the same question is never the right move: a caller who "
            "cannot get past you is worse off than one routed imperfectly, and a "
            "live run left a hesitant caller in this step for the whole call while "
            "another heard the identical sentence more than forty times.") \
        .add_section("If They Ask For A Person",
            "Being asked for a person outranks getting a name. Ask once more "
            "at most, then route regardless: call route_to_department with the "
            "department that best fits whatever they have told you"
            + (f" (use '{queues[0]['slug']}' if nothing else fits)" if queues else "")
            + ", and leave the name out if you do not have it. Never make a "
            "connection conditional on a name - a caller who has asked twice "
            "must be routed. The same applies to anyone who declines to give "
            "their name.") \
        .set_step_criteria(greeting_criteria) \
        .set_valid_steps(["route_department"]) \
        .set_functions(["report_sentiment", "set_caller_language", "route_to_department"])

    # Step 2: Determine department
    triage_ctx.add_step("route_department") \
        .add_section("Goal",
            "Figure out which department the caller needs. If they already told you during "
            "the greeting, route them immediately — no need to ask again.") \
        .add_section("If You Need to Ask",
            f"Ask naturally which area they need help with: {dept_menu}.") \
        .add_section("Departments", dept_list_text) \
        .add_section("Routing",
            "Once you know the department, call route_to_department with the department and "
            "a short note of what the caller needs. That call is what moves them — never "
            "announce a connection you haven't started, and never leave a caller waiting on "
            "a promise.") \
        .set_step_criteria("Customer has indicated which department they need") \
        .set_valid_contexts(queue_slugs) \
        .set_functions(["report_sentiment", "set_caller_language", "route_to_department"])

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

        # Step 1: a thin landing step, and the reason it exists.
        # route_to_department FORCES this context with swml_change_context,
        # which lands on the context's first step. On the first live run the
        # gather sitting on THAT step never engaged: the model improvised its
        # own question from the step text instead of being handed the gather's
        # question, and global_data['intake'] came back empty. Gather mode is
        # documented to begin "when the AI enters a step with gather_info", so
        # a forced context change appears not to count as that entry.
        #
        # Landing on a pass-through step and letting NORMAL advancement enter
        # the gather costs the caller nothing: skip_user_turn and
        # skip_to_next_step move on without a turn of their own.
        queue_ctx.add_step("gather_reason") \
            .add_section("Goal",
                f"You are now handling a {display.lower()} call. Continue "
                "straight on to taking the caller's details.") \
            .set_skip_user_turn(True) \
            .set_skip_to_next_step(True) \
            .set_valid_steps(["intake"]) \
            .set_functions(["report_sentiment", "route_to_department",
                            "set_caller_language"] + _INTAKE_ESCAPES)

        # Step 2: structured department intake (SDK gather_info), entered by
        # ordinary advancement from the landing step above.
        intake = _DEPARTMENT_INTAKE.get(slug, _DEFAULT_INTAKE)
        gather_step = queue_ctx.add_step("intake")
        gather_step.add_section("Goal",
            f"Take a few quick {display.lower()} details, then hand the caller "
            "on. Ask only the questions you are given, one at a time.")
        gather_step.set_gather_info(
            output_key='intake',
            completion_action='next_step',
            prompt=(
                f"You are taking a few {display.lower()} details before handing "
                "the caller on. Keep it brief and conversational. If the caller "
                "ALREADY told you an answer earlier in this call, submit that "
                "answer instead of asking the question again. If they ask for a "
                "person at any point, stop asking and transfer them - the "
                "person at any point, stop asking and transfer them - the "
                "details are a convenience, never a toll gate. And if the "
                "caller cannot or will not answer a question after TWO "
                "attempts, submit 'not provided' as the answer and move on: "
                "asking a third time strands callers who simply do not have "
                "the information, which a live run proved by leaving one at "
                "'nowhere' after the same question was put to them "
                "repeatedly."))
        for question in intake:
            gather_step.add_gather_question(
                key=question['key'],
                question=question['question'],
                type=question.get('type', 'string'),
                confirm=question.get('confirm', False),
                functions=_INTAKE_ESCAPES)
        gather_step.set_valid_steps(["offer_transfer"])
        gather_step.set_functions(["report_sentiment", "route_to_department",
                                   "set_caller_language"] + _INTAKE_ESCAPES)

        # Step 2: Offer transfer choice
        # The exact words the caller hears, lifted out of the chained call so
        # the quoting stays legible. An earlier inlined version rendered as
        #   ... - that might mean a short wait - or ""would you like ...
        # and the model, handed a malformed sentence, went straight back to
        # paraphrasing: it asked "Would you like to speak with someone on our
        # support team?" - the first clause only, dropping the alternative
        # entirely - and used "human specialist" and "AI assistant" elsewhere
        # in the same call. Mandating wording only works if the wording is
        # well-formed.
        choice_question = (
            f"Would you like to speak with someone on our {display.lower()} "
            "team - that might mean a short wait - or would you like our "
            "automated assistant to start helping you right now?"
        )

        # Criterion is the MODEL's action, not the caller's. It used to read
        # "Customer has chosen human or AI assistance", which a caller who
        # never chose could not satisfy - so the step had no exit and the
        # model filled the time by promising a connection it was not making:
        # fifteen consecutive turns of "I'm still connecting you to the
        # billing department" with no transfer tool ever called. Kept terse on
        # purpose - the model evaluates this string to decide advancement, so
        # rationale belongs in this comment rather than in the criterion.
        queue_ctx.add_step("offer_transfer") \
            .add_section("Goal",
                "Ask the caller which they want, in these exact words, "
                "changing nothing:\n"
                f'  "{choice_question}"\n'
                "Do not paraphrase it, and do not fall back on the phrases "
                "'human specialist' or 'AI assistant'. Naming the options "
                "that way tells a first-time caller nothing, and describing "
                "them loosely was not enough - the model paraphrased straight "
                "back into the jargon and live callers said so in as many "
                "words: \"I didn't understand the difference between human "
                "and AI help\". A caller who picks by guessing is a misroute "
                "that has not happened yet. Never offer just one of the two - "
                "a caller saying 'yes' to a single option is how misroutes "
                "happen.") \
            .add_section("Handling Questions",
                "If they ask you a question about their issue, acknowledge it and "
                "let them know a specialist can help with that. Then offer the transfer options.") \
            .add_section("If They Do Not Choose",
                "Offer the choice ONCE. If the caller's next turn does not "
                "clearly name one of the two options - for ANY reason at all, "
                "including silence, a noise, a non-answer, a question back, or "
                "something you simply cannot make out - call "
                "transfer_to_ai_specialist immediately. Do not re-ask and do "
                "not wait for a better answer: the assistant can help at once "
                "and can still escalate to a person later, so it is the "
                "recoverable choice. Only use transfer_to_human when they "
                "clearly asked for a person.\n"
                "This used to enumerate the ways a caller might fail to pick "
                "(repeats the question, says a bare 'yes', gets cut off). "
                "Enumerating never covers enough: a hesitant caller answering "
                "in 'hmm' and 'sorry?' matched none of the listed cases, so "
                "the model kept waiting for a choice that was never coming.") \
            .add_section("Never Narrate A Transfer You Have Not Made",
                "Telling the caller you are connecting them is not connecting "
                "them. A live call ended with fifteen consecutive turns of "
                "\"I'm still connecting you to the billing department, please "
                "hold on just a little longer\" while no transfer tool had been "
                "called at all - the caller waited out the whole call for "
                "something that was never happening. Fabricated progress is "
                "worse than an apology: say nothing about connecting until the "
                "tool call is going out in the same breath.") \
            .add_section("Transferring",
                "Once they choose:\n"
                "- Someone on the team: use the transfer_to_human tool\n"
                "- The automated assistant: use the transfer_to_ai_specialist tool\n\n"
                f"Always include: customer_name, reason, department='{slug}', "
                "urgency, additional_info") \
            .set_step_criteria(
                "You have called transfer_to_human or transfer_to_ai_specialist") \
            .set_valid_steps([]) \
            .set_functions(["transfer_to_human", "transfer_to_ai_specialist",
                            "report_sentiment", "route_to_department",
                            "set_caller_language"])


# ---------------------------------------------------------------------------
# "Did the caller actually ask for a person?" — checked in code, from the
# platform's own transcript, because the prompt version of this rule does not
# hold.
#
# The offer_transfer step already tells the model, in as many words, that a
# caller who gets "cut off mid-answer" wants the AI assistant and that
# transfer_to_human is only for someone who "clearly asked for a person". On a
# live call (2026-08-17) Sam asked "human specialist or the AI assistant?", the
# caller began "I would prefer" — three words, no choice in them yet — and Sam
# answered "It sounds like you prefer to speak with human specialist" and
# transferred. The caller said "No." twice on the way to the hold queue. The
# call ended as a callback and he never reached the specialist.
#
# So the rule moves into the tool. `swaig_post_conversation` puts the real
# conversation in every tool's post_data, which means this check reads what the
# caller SAID rather than what the model concluded they meant.
# ---------------------------------------------------------------------------

# This agent answers in English, Spanish and French (see add_language), so the
# gate has to read all three — an English-only word list silently forces every
# Spanish caller who asks for "una persona" to the AI.
_HUMAN_TERMS = frozenset({
    # English
    'human', 'humans', 'person', 'people', 'agent', 'agents',
    'representative', 'representatives', 'rep', 'someone', 'somebody',
    'operator', 'operators',
    # The word the offer itself uses for the human option: "a human sales
    # SPECIALIST, or our AI assistant". A caller echoing the label back —
    # "the specialist, please" — has named an option, and reading that as
    # naming nobody overrode an explicit choice with an AI transfer.
    'specialist', 'specialists',
    # Deliberately NOT 'live'/'actual'/'real'. Those are adjectives, not
    # people, and standing alone they turned ordinary product questions
    # ("can you check live availability?", "is that the real price") into
    # option-naming turns. Every phrasing that matters carries a noun too:
    # "a real person", "an actual human".
    # Spanish
    'persona', 'personas', 'humano', 'humana', 'humanos', 'agente',
    'agentes', 'representante', 'representantes', 'alguien', 'operador',
    'operadora', 'especialista', 'especialistas',
    # French
    'humain', 'humaine', 'humains', 'personne', 'personnes', 'conseiller',
    'conseillere', 'conseillers', 'representant', 'representants',
    'quelquun', 'operateur', 'specialiste', 'specialistes',
})

# Naming the machine is a request for a person only when it's REJECTED — "I
# don't want a robot" wants a human, "the AI is fine" does not. Polarity does
# that work, so these live here rather than in the human list. 'ia' is the
# Spanish and French abbreviation and is what those callers actually say.
_AI_TERMS = frozenset({
    'ai', 'ia', 'bot', 'robot', 'machine', 'computer', 'automated',
    'assistant', 'asistente', 'automatico', 'automatica', 'maquina',
    'artificial', 'inteligencia', 'artificielle', 'virtuel', 'virtuelle',
    'ordinateur', 'automate',
})

_NEGATORS = frozenset({
    'not', 'no', 'never', 'nor', 'neither',
    # Contractions, post-fold (the apostrophe is gone by the time we look).
    'dont', 'doesnt', 'didnt', 'cant', 'cannot', 'wont', 'wouldnt',
    'shouldnt', 'couldnt', 'isnt', 'arent', 'wasnt', 'werent', 'havent',
    'hasnt', 'hadnt', 'aint',
    'sin', 'nunca', 'tampoco',
    'pas', 'ne', 'non', 'sans', 'jamais', 'aucun', 'aucune',
})

# Word-initial elision: l'agent, d'un, qu'il. Folding the apostrophe away
# glues these into "lagent", which matches nothing. English contractions are
# the opposite case — "don't" must become "dont" to read as a negator — and
# the two are told apart by position: the elided article is its own word.
_ELISION = re.compile(r"\b(l|d|j|n|m|t|s|c|qu)'")


def _fold(text: str) -> str:
    """Lowercase, strip accents, split elisions, drop punctuation."""
    import unicodedata
    decomposed = unicodedata.normalize('NFKD', text.lower())
    stripped = ''.join(c for c in decomposed if not unicodedata.combining(c))
    stripped = _ELISION.sub(r' ', stripped)
    for apostrophe in ("'", '’', 'ʼ', '`'):
        stripped = stripped.replace(apostrophe, '')
    return ''.join(c if c.isalnum() else ' ' for c in stripped)


def _last_caller_utterance(raw_data) -> str:
    """The caller's most recent words per the platform's own call_log."""
    if not isinstance(raw_data, dict):
        return ''
    log = raw_data.get('call_log') or raw_data.get('raw_call_log') or []
    if not isinstance(log, list):
        return ''
    for entry in reversed(log):
        if not isinstance(entry, dict) or entry.get('role') != 'user':
            continue
        content = entry.get('content')
        if isinstance(content, str) and content.strip():
            return content.strip()
    return ''


def _human_request_evidence(raw_data) -> str:
    """Did the caller's last turn name an option at all?

    Returns 'CONFIRMED' (the turn talks about a person — leave the model's
    routing alone), 'ABSENT' (no choice was expressed — apply the documented
    default and route to the AI), or 'UNAVAILABLE' (no transcript; defer).

    This deliberately does NOT decide WHICH option the caller picked. Earlier
    versions tried, and review found five different ways for keyword-and-
    polarity parsing to get that wrong — comparisons ("the AI over a human"),
    corrections ("I wanted a human before, but..."), post-target negation ("a
    human isn't what I want"), explanations that mention the rejected option
    ("get me a person, the bot is useless"), and every phrasing in three
    languages that nobody had enumerated yet. Each fix bought one sentence and
    left the shape of the problem intact, because deciding intent from free
    speech is not a job a word list can hold.

    So the gate answers only the question it was built for and can answer.
    The bug it exists to stop was a caller who said "I would prefer" — cut off
    mid-answer, no option named — being routed to a hold queue on a guess. A
    turn with no option in it is that case, and the offer_transfer step already
    documents the answer: the AI assistant, which is recoverable, rather than
    a queue, which is not.

    When the caller DOES name a person, the model's reading stands. That is
    strictly better than what this code was doing: it was overriding the model
    with a worse parser, and the override could itself produce the expensive
    misroute. If enforcing an explicit choice turns out to matter, the answer
    is a structured one (a DTMF digit, or an enum the tool validates), not
    more parsing.
    """
    utterance = _last_caller_utterance(raw_data)
    if not utterance:
        return 'UNAVAILABLE'

    tokens = set(_fold(utterance).split())
    if tokens & _HUMAN_TERMS:
        return 'CONFIRMED'
    if tokens & _AI_TERMS:
        # Only the machine was named. With no negator anywhere in the turn
        # that reads as choosing it ("the AI is fine"); with one it may be a
        # rejection ("not the AI"), and telling those apart is precisely the
        # judgement this gate stopped making — so it defers instead.
        return 'CONFIRMED' if tokens & _NEGATORS else 'ABSENT'
    return 'ABSENT'


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
        # Only forward a language we actually LEARNED (set_caller_language
        # wrote it into global_data). Defaulting 'en-US' here presented a
        # guess as fact — /route persisted it onto Call.caller_language and
        # from there it seeped into Contact.preferred_language.
        if global_data.get('caller_language'):
            context_data.setdefault(
                'caller_language', global_data['caller_language']
            )

        # Structured intake gathered by gather_info lands in global_data under
        # the step's output_key. Forward it so the agent screen-pop and the AI
        # specialist actually receive what the caller was asked for: gathering
        # details and then dropping them wastes the caller's time twice, once
        # answering and again when whoever picks up asks the same thing.
        gathered = global_data.get('intake')
        if isinstance(gathered, dict) and gathered:
            context_data.setdefault('intake', gathered)

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

    @AgentBase.tool(
        name="set_caller_language",
        description=(
            "Silently record the language the caller is speaking (BCP-47 code). "
            "Call this as soon as you detect or confirm their language, and call "
            "it AGAIN any time the caller switches languages mid-call. "
            "Used to route them to language-matched agents and to transcribe "
            "the call in the right language."
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
        """Record the caller's language: in global_data (flows with the call
        through transfers) AND via a write-through POST to the backend.

        The write-through is the PGI half (base class so ALL agents have it —
        a caller can reveal or switch language during a specialist session
        too): the backend persists Call.caller_language the moment the tool
        fires — the column used to depend on the human-enqueue path copying
        global_data, which AI-only calls never run — and restarts live
        transcription in the new language, since a live_transcribe session
        is pinned to the single lang it was started with.
        """
        language = (args.get("language") or "en-US").strip()

        global_data = raw_data.get('global_data', {})
        call_db_id = global_data.get('call_db_id')
        if call_db_id:
            def post_language():
                try:
                    import requests as http_requests
                    backend_url = os.getenv('BACKEND_URL', 'http://backend:5000')
                    url = f"{backend_url}/api/calls/{call_db_id}/caller-language"
                    http_requests.post(url, json={'language': language},
                                       auth=_internal_auth(), timeout=5)
                except Exception as exc:
                    # Non-fatal: the post-prompt seeder still back-fills the
                    # column from global_data at session end; only the
                    # mid-call transcription restart is lost.
                    print(f"caller-language POST failed (non-fatal): {exc}", flush=True)
            threading.Thread(target=post_language, daemon=True).start()

        # Return a NON-EMPTY result. An empty FunctionResult("") tells the engine
        # the turn has nothing to say, so it ends the turn and the AI's next line
        # (e.g. "Which department?") is generated but never spoken — the call
        # stalls into dead air. report_sentiment (the other silent tool) returns
        # "ok" for exactly this reason; mirror it. "ok" is a function return to
        # the model, not spoken to the caller.
        result = FunctionResult("ok")
        result.update_global_data({"caller_language": language})
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
            # 1500ms, ABOVE the platform default of 1000. It was 800 - below the
            # default - which told the platform a caller had finished speaking
            # sooner than stock, and unrehearsed callers do not talk in clean
            # bursts. Observed 2026-08-20: a caller answering the human-or-AI
            # question got as far as "A real per-" before the AI resumed over
            # them; the fragment named no option, so _human_request_evidence
            # correctly read ABSENT and sent them to the AI specialist. The code
            # was right and the caller still ended up in the wrong place.
            #
            # The trade is real: every turn in triage now waits ~0.7s longer.
            # Worth it here specifically because triage asks the one question
            # whose misreading is IRREVERSIBLE - a misrouted caller waits on hold
            # and may leave as a callback. The specialists keep their own snappier
            # timeouts, where a misheard turn is just re-asked.
            "end_of_speech_timeout": 1500,
            "ai_volume": 0,
            "enable_text_normalization": "both",
            # Puts call_log/raw_call_log in every SWAIG tool's post_data, so a
            # tool can check what the caller ACTUALLY said instead of trusting
            # the model's reading of it. transfer_to_human depends on this —
            # see _caller_asked_for_a_human.
            "swaig_post_conversation": True,
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
            "and move toward getting them connected. Words alone never connect anyone: only the routing "
            "and transfer tools do. Never say you're connecting or transferring a caller unless you are "
            "invoking the tool that does it right then."
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
                "Call it silently — no fillers, no acknowledgment to the caller",
                "If the caller SWITCHES languages mid-call, follow them and call set_caller_language again with the new code",
            ]
        )

        # Everything queue-shaped (contexts, routing map, hints, post-prompt
        # enum) builds through the same function the per-request callback
        # uses with tenant queues.
        configure_triage_queues(self, queues)

        # Tools registered via @AgentBase.tool() decorators below

    # set_caller_language lives on CallCenterAgent (base) — every agent can
    # record a language switch, not just triage.

    def _require_caller_language(self, raw_data):
        """One-shot PGI gate for the triage exit tools: no caller leaves
        triage without a recorded language.

        The greeting step_criteria ASKS the model to call
        set_caller_language, but criteria are advisory and the model
        provably skips them (maria_language_memory rows 29 and 47: a
        Spanish-only call where the tool never fired and the transcript
        stayed garbled en-US start to finish). Routing/transfer are the
        structural chokepoints every handled call passes through, so gate
        them: the first exit attempt without a language bounces with a
        prescriptive error telling the model to record the language and
        retry. ONE bounce only — a model that still refuses must not be
        able to strand the caller, so the second attempt passes (the
        post-prompt back-fill then still fixes the record, at the cost of
        this one call's transcription language).
        """
        global_data = raw_data.get('global_data', {}) or {}
        if global_data.get('caller_language') or global_data.get('language_gate_bounced'):
            return None
        result = FunctionResult(
            "LANGUAGE_NOT_SET: silently call set_caller_language with the "
            "BCP-47 code of the language this caller has been speaking "
            "(e.g. 'en-US', 'es-ES', 'fr-FR'), then call this tool again "
            "with the same arguments."
        )
        result.update_global_data({'language_gate_bounced': True})
        return result

    @AgentBase.tool(
        name="route_to_department",
        description=(
            "Move the caller into the flow for the department that handles what they "
            "need. Call it the MOMENT the department is clear — whether the caller "
            "stated it, described a need that obviously belongs there (like a pricing "
            "question belonging to sales), or confirmed a topic you offered — even if "
            "the usual intake questions were skipped. Never tell a caller you'll "
            "connect or transfer them without calling this. NOT for reaching a "
            "specialist: from inside a department flow, use transfer_to_human or "
            "transfer_to_ai_specialist instead."
        ),
        parameters={
            "department": {
                "type": "string",
                "description": "Department slug from the Departments list (e.g., 'sales', 'support')",
            },
            "reason": {
                "type": "string",
                "description": (
                    "What the caller needs, briefly, in their own words "
                    "(e.g., 'price of the most popular product')"
                ),
            },
        },
        # No fillers — the department shift is the same persona, one continuous
        # person (see the filler policy note in __init__); the change_context
        # internal fillers cover any gap.
    )
    def route_to_department(self, args, raw_data):
        """Code-enforced routing into a queue context (PGI: prompts propose,
        code decides).

        Step/context advancement is otherwise model-driven against
        step_criteria, and the transfer tools exist only inside the queue
        contexts. A caller whose conversation shape skips the intake ritual
        (e.g. a recognized returning caller whose topic was confirmed by the
        greeting, then pressed for an answer) used to strand the model in
        greeting/route_department — verbally promising a transfer it could
        not execute from there. This tool makes routing an action: validate
        the department against the live queue map, then FORCE the context
        change with swml_change_context (which bypasses valid_contexts and
        lands on the queue's first step). The result text rides the
        conversation into the new context, so the queue steps know the
        caller's request without re-asking.
        """
        gate = self._require_caller_language(raw_data)
        if gate is not None:
            return gate

        global_data = raw_data.get('global_data', {})
        queue_map = getattr(self, '_queue_ai_map', {}) or {}
        requested = (args.get('department') or '').strip().lower()
        reason = (args.get('reason') or '').strip()

        # Tolerate display-name-ish input ("Sales", "technical support desk").
        slug = requested if requested in queue_map else next(
            (cand for cand in queue_map if cand in requested), None)

        if not slug:
            options = ', '.join(sorted(queue_map)) or 'none configured'
            return FunctionResult(
                f"UNKNOWN_DEPARTMENT: '{requested}' is not one of our departments. "
                f"Valid departments: {options}. Pick the one that fits what the "
                "caller needs and call route_to_department again."
            )

        if global_data.get('routed_department') == slug:
            # Re-routing to the SAME department would reset the queue flow to
            # its first step; bounce the model forward instead. A different
            # department passes through — that's a legitimate correction.
            return FunctionResult(
                f"ALREADY_ROUTED: the caller is already in the {slug} flow. "
                "Offer the connection options, or execute their choice with "
                "transfer_to_human / transfer_to_ai_specialist."
            )

        if reason:
            guidance = (
                "They have already explained what they need — do NOT ask again. "
                "Go straight to the connection choice, BOTH options in ONE short "
                "question: a human specialist, or the AI assistant who can answer "
                "immediately. If their answer is unclear, interrupted, or they "
                "just repeat their question, use transfer_to_ai_specialist."
            )
        else:
            guidance = (
                "Ask briefly what they need help with, then offer the "
                "connection choice — both options in one short question."
            )
        result = FunctionResult(
            f"ROUTED to {slug}. The caller's request: "
            f"{reason or 'not stated yet'}. {guidance}"
        )
        result.update_global_data({
            'routed_department': slug,
            'department': slug,
            'reason': reason,
        })
        result.swml_change_context(slug)
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
        gate = self._require_caller_language(raw_data)
        if gate is not None:
            return gate

        # The caller has to have asked for a person. Sending someone to the
        # human queue on a guess costs them the conversation: they wait on
        # hold, and past the cap the call ends as a callback. Routing to the
        # AI assistant instead costs nothing that can't be undone — it answers
        # immediately and can still escalate_to_human at any point. Given that
        # asymmetry, an unclear answer resolves to the AI, which is exactly
        # what the offer_transfer step already documents.
        if _human_request_evidence(raw_data) == 'ABSENT':
            print(
                "transfer_to_human: no explicit request for a person in the "
                f"caller's last turn ({_last_caller_utterance(raw_data)!r}) — "
                "routing to the AI specialist instead",
                flush=True,
            )
            return self.transfer_to_ai_specialist(args, raw_data)

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
        gate = self._require_caller_language(raw_data)
        if gate is not None:
            return gate
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
            "(mcp_demoshop_list_products) and answer from its data. If they "
            "named a specific product, call mcp_demoshop_find_product with "
            "that name — it confirms the price or tells you we don't carry "
            "it, and either answer is better than a transfer. Escalate only "
            "after the catalog tools have failed to answer; a document miss "
            "alone is not a reason to hand the call to a human."
        )
        self._mcp_agent_id = 'sales-ai'  # MCP gateways attach per-request (callback), not at boot
        # The catalog rides IN the prompt, not just behind a tool. "What do
        # you sell and what does it cost" is the most common question on this
        # line, and a model that has to elect a tool call to answer it will
        # sometimes answer from nothing instead — with prices.
        self._preload_mcp_tool = 'list_products'
        self._preload_section_title = 'Product Catalog'
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
                "When the caller names a particular product, look it up with "
                "mcp_demoshop_find_product before you say anything about it — that tool "
                "also tells you when we don't carry it, which is a real answer worth "
                "giving, not a reason to transfer",
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
