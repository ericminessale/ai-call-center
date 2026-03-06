#!/usr/bin/env python3
"""
SignalWire Call Center AI Agents
Triage agent using contexts/steps - NO problem solving, info gathering only.
AI Specialists (separate agents) are the ONLY ones that solve problems.
Includes RAG knowledge base search via pgvector.
"""

from signalwire_agents import AgentBase, AgentServer
from signalwire_agents.core.function_result import SwaigFunctionResult
import os
import json
import base64
import re
import threading
from dotenv import load_dotenv

load_dotenv()

# Configuration
BACKEND_URL = os.getenv('BACKEND_URL', 'http://backend:5000')
DATABASE_URL = os.getenv('DATABASE_URL', '')
EMBEDDING_MODEL_NAME = 'sentence-transformers/all-MiniLM-L6-v2'
EMBEDDING_DIM = 384

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

    # Create text search index if not exists
    try:
        cur.execute(f"""
            CREATE INDEX IF NOT EXISTS idx_{collection_name}_content_trgm
            ON {table_name} USING gin (content gin_trgm_ops)
        """)
    except Exception:
        pass

    conn.commit()
    cur.close()
    conn.close()

    print(f"Reindexed {total_chunks} chunks for collection '{collection_name}'", flush=True)
    return total_chunks


def get_assigned_collection(agent_id):
    """Query backend for this agent's assigned collection name."""
    import requests
    try:
        resp = requests.get(
            f"{BACKEND_URL}/api/admin/agent-assignments",
            params={'agent_id': agent_id},
            timeout=5,
        )
        if resp.ok:
            assignments = resp.json().get('assignments', [])
            if assignments:
                return assignments[0].get('collection_name')
    except Exception as e:
        print(f"Warning: Could not fetch collection assignment for {agent_id}: {e}", flush=True)
    return None


def get_active_queues():
    """Fetch active queue configs from the backend at startup.
    Falls back to hardcoded sales/support if backend is unavailable."""
    import requests
    fallback = [
        {'slug': 'sales', 'display_name': 'Sales', 'description': 'Sales inquiries and purchases', 'ai_agent_route': '/sales-ai'},
        {'slug': 'support', 'display_name': 'Support', 'description': 'Technical support and issue resolution', 'ai_agent_route': '/support-ai'},
    ]
    try:
        resp = requests.get(f"{BACKEND_URL}/api/queues/config/active", timeout=5)
        if resp.ok:
            queues = resp.json().get('queues', [])
            if queues:
                print(f"Loaded {len(queues)} active queues from backend: {[q['slug'] for q in queues]}", flush=True)
                return queues
        print("No queues returned from backend, using fallback", flush=True)
    except Exception as e:
        print(f"Warning: Could not fetch queues from backend: {e}. Using fallback.", flush=True)
    return fallback


def add_knowledge_search(agent, agent_id, fallback_collection=None):
    """Add native_vector_search skill to an agent if a collection is assigned."""
    if not DATABASE_URL:
        print(f"Warning: DATABASE_URL not set, skipping knowledge search for {agent_id}", flush=True)
        return

    collection = get_assigned_collection(agent_id) or fallback_collection
    if not collection:
        print(f"No collection assigned to {agent_id}, skipping knowledge search", flush=True)
        return

    try:
        agent.add_skill("native_vector_search", {
            "tool_name": "search_knowledge",
            "backend": "pgvector",
            "connection_string": DATABASE_URL,
            "collection_name": collection,
            "description": f"Search the knowledge base for relevant information. Use this when the customer asks questions about products, services, troubleshooting, or anything you need to look up.",
            "count": 5,
            "build_index": False,
        })
        print(f"Added knowledge search for {agent_id} -> collection '{collection}'", flush=True)
    except Exception as e:
        print(f"Warning: Failed to add knowledge search for {agent_id}: {e}", flush=True)


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

    @admin_app.get("/admin/health")
    async def admin_health():
        return {"status": "healthy", "service": "ai-agents-admin"}

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
    as global_data for outbound AI calls.
    """
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

    if base_url:
        post_prompt_url = f"{base_url}/api/webhooks/post-prompt"
        agent.set_post_prompt_url(post_prompt_url)

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
                    http_requests.post(url, json={'signalwire_sid': b_leg_sid}, timeout=5)
                    print(f"Registered AI B-leg SID: {b_leg_sid} for call {call_db_id}", flush=True)
                except Exception as e:
                    print(f"Warning: Failed to register AI leg SID: {e}", flush=True)
            threading.Thread(target=register_ai_leg, daemon=True).start()


class CallCenterTriageAgent(AgentBase):
    """
    Call Center TRIAGE Agent - Information gathering ONLY.

    This agent does NOT solve problems. It ONLY:
    1. Collects the customer's name
    2. Identifies if they need sales or support
    3. Gathers basic context info
    4. Transfers to human queue OR AI specialist

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

        # Fetch active queues from backend (dynamic at startup)
        queues = get_active_queues()
        queue_slugs = [q['slug'] for q in queues]
        self._queue_ai_map = {q['slug']: q.get('ai_agent_route', f"/{q['slug']}-ai") for q in queues}

        # ============================================================
        # GLOBAL PROMPT - Applies to ALL contexts
        # Just defines personality - NO problem solving instructions
        # ============================================================
        self.prompt_add_section(
            "Identity",
            "You are Sam, a friendly and efficient customer service representative. "
            "Your ONLY job is to quickly understand what the caller needs and route them to the right team."
        )

        self.prompt_add_section(
            "CRITICAL RESTRICTIONS",
            "You are a TRIAGE agent. You must NEVER:",
            bullets=[
                "Attempt to solve, troubleshoot, or fix any problem",
                "Provide technical advice or suggestions",
                "Answer product questions or provide pricing",
                "Diagnose issues or suggest solutions",
                "Say things like 'did you try...' or 'have you checked...'",
                "Offer workarounds or temporary fixes"
            ]
        )

        self.prompt_add_section(
            "Your Job",
            "You ONLY gather information and transfer calls. That's it. "
            "If someone describes a problem, acknowledge it and move to getting their transfer preference. "
            "Do NOT engage with the problem itself. Be brief and efficient — route calls quickly."
        )

        # Build department list for post_prompt
        dept_options = '/'.join(queue_slugs + ['unknown'])
        self.set_post_prompt(f"""
Summarize this call and return a JSON object with:
{{
    "customer_name": "Name if provided, or null",
    "department": "{dept_options}",
    "reason": "Brief reason for their call",
    "outcome": "transferred_to_human/transferred_to_ai/abandoned",
    "notes": "Any important details"
}}
""")

        # Define the contexts and steps
        contexts = self.define_contexts()

        # ============================================================
        # TRIAGE CONTEXT (default) - Initial greeting and routing
        # ============================================================
        triage_ctx = contexts.add_context("default")

        # Build routing hints from queue config
        queue_descriptions = []
        for q in queues:
            desc = q.get('description', '')
            queue_descriptions.append(f"{q['display_name'].upper()} ({q['slug']}): {desc}" if desc else f"{q['display_name'].upper()} ({q['slug']})")

        listen_for_text = "\n".join(queue_descriptions)
        route_instructions = "\n".join([f"- {q['display_name']}-related: change_context to '{q['slug']}'" for q in queues])

        # Build a natural department menu for the greeting
        dept_names = [q['display_name'] for q in queues]
        if len(dept_names) > 1:
            dept_menu = ', '.join(dept_names[:-1]) + ', or ' + dept_names[-1]
        else:
            dept_menu = dept_names[0] if dept_names else 'general assistance'

        # Step 1: Get the caller's name
        triage_ctx.add_step("get_name") \
            .add_section("Your Task",
                "Greet the caller and ask for their name. Keep it brief.") \
            .add_section("What to Say",
                "'Hi, thank you for calling! My name is Sam. May I get your name please?'") \
            .add_section("IMPORTANT",
                "You MUST get their name before moving on. If they start explaining "
                "their issue, say 'I'd love to help with that — may I first get your name?'") \
            .set_step_criteria("Customer has clearly stated their name") \
            .set_valid_steps(["route_department"])

        # Step 2: Ask which department they need — direct, not open-ended
        triage_ctx.add_step("route_department") \
            .add_section("Your Task",
                "Now that you have their name, ask which department they need. "
                "Present the available options directly.") \
            .add_section("What to Say",
                f"'Thanks [name]! Are you calling about {dept_menu}?'") \
            .add_section("Available Departments", listen_for_text) \
            .add_section("Routing",
                "As soon as they indicate a department:\n" + route_instructions + "\n"
                "Do NOT announce the routing. Just seamlessly move to the right department context.") \
            .set_step_criteria("Customer has indicated which department they need") \
            .set_valid_contexts(queue_slugs)

        # ============================================================
        # DYNAMIC QUEUE CONTEXTS - Built from configured queues
        # ============================================================
        for q in queues:
            slug = q['slug']
            display = q['display_name']

            queue_ctx = contexts.add_context(slug) \
                .set_isolated(True)

            queue_ctx.add_section("Role",
                f"Continue as Sam. Customer needs {display.lower()} help. Use their name.")

            queue_ctx.add_section("REMEMBER",
                "You are TRIAGE only. Do NOT answer questions, provide advice, "
                "or make recommendations. Quickly offer transfer options.")

            # Single step: Offer transfer choice immediately
            queue_ctx.add_step("offer_transfer") \
                .add_section("Your Task",
                    f"The caller needs {display.lower()} help. Offer them their transfer options right away.") \
                .add_section("What to Say",
                    f"'I can connect you with one of our {display.lower()} specialists, "
                    "or if you prefer, our AI assistant can help you right away. "
                    "Which would you prefer?'") \
                .add_section("After They Answer",
                    "- Want human/representative/person/agent: use transfer_to_human tool\n"
                    "- Want AI/assistant/you can help: use transfer_to_ai_specialist tool\n"
                    "- If unclear, briefly clarify then transfer\n\n"
                    f"Include: customer_name (if known), reason, department='{slug}', "
                    "urgency='medium', additional_info") \
                .add_section("CRITICAL",
                    "Do NOT answer their questions or try to solve their problem. "
                    "If they ask a question, say: "
                    "'Great question — let me connect you with someone who can help with that.' "
                    "Then ask if they want a human or AI assistant.") \
                .set_step_criteria("Customer has chosen human or AI assistance")

        # ============================================================
        # TOOLS - Transfer functions only
        # ============================================================
        dept_enum_desc = "Department: " + ", ".join([f"'{s}'" for s in queue_slugs])
        self.define_tool(
            name="transfer_to_human",
            description="Transfer customer to a human representative. Use when they choose to speak with a human.",
            parameters={
                "customer_name": {"type": "string", "description": "Customer's spoken name (NOT their phone number - only use a name they verbally provide)"},
                "reason": {"type": "string", "description": "Brief description of what they need"},
                "department": {"type": "string", "description": dept_enum_desc},
                "urgency": {"type": "string", "description": "'high', 'medium', or 'low'"},
                "additional_info": {"type": "string", "description": "Any other relevant context"}
            },
            handler=self.transfer_to_human
        )

        self.define_tool(
            name="transfer_to_ai_specialist",
            description="Transfer customer to AI specialist. Use when they choose AI assistance.",
            parameters={
                "customer_name": {"type": "string", "description": "Customer's spoken name (NOT their phone number - only use a name they verbally provide)"},
                "reason": {"type": "string", "description": "Brief description of what they need"},
                "department": {"type": "string", "description": dept_enum_desc},
                "urgency": {"type": "string", "description": "'high', 'medium', or 'low'"},
                "additional_info": {"type": "string", "description": "Any other relevant context"}
            },
            handler=self.transfer_to_ai_specialist
        )

    def _check_basic_auth(self, request) -> bool:
        """Override to disable auth - agents are behind nginx"""
        return True

    def transfer_to_human(self, args, raw_data):
        """Transfer to human representative queue"""
        customer_name = args.get("customer_name", "")
        reason = args.get("reason", "")
        department = args.get("department", "support").lower()
        urgency = args.get("urgency", "medium")
        additional_info = args.get("additional_info", "")

        base_url = get_base_url_from_global_data(raw_data)
        global_data = raw_data.get('global_data', {})

        # Get conference name and call DB ID (conference-first architecture)
        conf = global_data.get('conf', '')
        call_db_id = global_data.get('call_db_id', '')

        # Map urgency to priority
        urgency_map = {'high': 2, 'medium': 5, 'low': 8}
        priority = urgency_map.get(urgency.lower(), 5)

        context_data = {
            'customer_name': customer_name,
            'reason': reason,
            'department': department,
            'urgency': urgency,
            'priority': priority,
            'additional_info': additional_info,
            'preferred_handling': 'human',
            'source_agent': 'call_center_triage'
        }

        # Encode context as base64 JSON for URL
        context_json = json.dumps(context_data)
        context_b64 = base64.urlsafe_b64encode(context_json.encode()).decode()
        # Include conference name and call DB ID for conference-first flow
        queue_url = f"{base_url}/api/queues/{department}/route?ctx={context_b64}"
        if conf:
            queue_url += f"&conf={conf}"
        if call_db_id:
            queue_url += f"&call_db_id={call_db_id}"

        print(f"Transferring {customer_name} to human queue: {queue_url}", flush=True)
        print(f"Context data: {context_data}", flush=True)

        result = SwaigFunctionResult(
            "I'll connect you with a representative right now."
        )
        result.update_global_data(context_data)
        result.swml_transfer(queue_url, "", final=True)
        return result

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

        print(f"Transferring {customer_name} to AI specialist: {transfer_url}", flush=True)

        result = SwaigFunctionResult('')  # Silent transfer
        result.update_global_data({
            'customer_name': customer_name,
            'reason': reason,
            'department': department,
            'urgency': urgency,
            'additional_info': additional_info,
            'preferred_handling': 'ai',
            'source_agent': 'call_center_triage'
        })
        result.swml_transfer(transfer_url, "", final=True)
        return result


class SalesAISpecialist(AgentBase):
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

        self.set_params({
            "wait_for_user": False,
            "end_of_speech_timeout": 1000
        })

        self.set_dynamic_config_callback(capture_base_url)

        # Add knowledge base search (pgvector RAG)
        add_knowledge_search(self, 'sales-ai', fallback_collection='sales_knowledge')

        self.set_post_prompt("""
Summarize this sales consultation and return a JSON object with:
{
    "customer_name": "Name if provided, or null",
    "company": "Company name if provided, or null",
    "products_discussed": ["List of products/services discussed"],
    "recommendations_made": ["Products/solutions recommended"],
    "next_steps": "Recommended next steps",
    "lead_score": "1-10 (1=hot, 10=cold)",
    "outcome": "sale/quote_requested/follow_up_needed/lost"
}
""")

        self.prompt_add_section(
            "Role",
            "You are Alex, an AI sales specialist. The customer chose to speak with an AI assistant "
            "for help with their sales inquiry."
        )

        self.prompt_add_section(
            "Customer Context",
            "Customer name: ${global_data.customer_name}\n"
            "Interest: ${global_data.reason}\n"
            "Additional info: ${global_data.additional_info}\n\n"
            "Greet them by name and continue the conversation."
        )

        self.prompt_add_section(
            "What You CAN Do",
            "You are empowered to help with:",
            bullets=[
                "Answer questions about products and services",
                "Explain features, benefits, and use cases",
                "Provide general pricing guidance",
                "Make recommendations based on their needs",
                "Help them understand which solution fits best"
            ]
        )

        self.prompt_add_section(
            "Escalation",
            "If they want to proceed with a purchase, get a custom quote, "
            "or speak with a human, use the escalate_to_human tool."
        )

        self.define_tool(
            name="escalate_to_human",
            description="Connect to human sales rep for purchases, quotes, or complex needs",
            parameters={
                "reason": {"type": "string", "description": "Reason for escalation"}
            },
            handler=self.escalate_to_human
        )

    def _check_basic_auth(self, request) -> bool:
        return True

    def escalate_to_human(self, args, raw_data):
        """Escalate to human sales"""
        reason = args.get("reason", "")
        base_url = get_base_url_from_global_data(raw_data)
        global_data = raw_data.get('global_data', {})

        # Get conference name and call DB ID (conference-first architecture)
        conf = global_data.get('conf', '')
        call_db_id = global_data.get('call_db_id', '')

        context_data = {
            'customer_name': global_data.get('customer_name', ''),
            'reason': global_data.get('reason', ''),
            'department': 'sales',
            'urgency': global_data.get('urgency', 'medium'),
            'priority': global_data.get('priority', 5),
            'additional_info': global_data.get('additional_info', ''),
            'escalation_reason': reason,
            'escalated_from': 'sales_ai_specialist',
            'preferred_handling': 'human',
            'source_agent': 'sales_ai_specialist'
        }

        context_json = json.dumps(context_data)
        context_b64 = base64.urlsafe_b64encode(context_json.encode()).decode()
        queue_url = f"{base_url}/api/queues/sales/route?ctx={context_b64}"
        if conf:
            queue_url += f"&conf={conf}"
        if call_db_id:
            queue_url += f"&call_db_id={call_db_id}"

        print(f"Escalating to human sales: {queue_url}", flush=True)

        result = SwaigFunctionResult(
            "I'll connect you with a sales representative who can help with that."
        )
        result.update_global_data(context_data)
        result.swml_transfer(queue_url, "", final=True)
        return result


class SupportAISpecialist(AgentBase):
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

        self.set_params({
            "wait_for_user": False,
            "end_of_speech_timeout": 1000
        })

        self.set_dynamic_config_callback(capture_base_url)

        # Add knowledge base search (pgvector RAG)
        add_knowledge_search(self, 'support-ai', fallback_collection='support_knowledge')

        self.set_post_prompt("""
Summarize this support consultation and return a JSON object with:
{
    "customer_name": "Name if provided, or null",
    "issue_summary": "Brief description of the issue",
    "troubleshooting_steps": ["Steps attempted during the call"],
    "resolution": "How resolved, or null if unresolved",
    "resolved": true/false,
    "escalation_reason": "Why escalated, or null",
    "customer_satisfaction": "1-5 based on conversation"
}
""")

        self.prompt_add_section(
            "Role",
            "You are Jordan, an AI support specialist. The customer chose to speak with an AI assistant "
            "to help troubleshoot their issue."
        )

        self.prompt_add_section(
            "Customer Context",
            "Customer name: ${global_data.customer_name}\n"
            "Issue: ${global_data.reason}\n"
            "Urgency: ${global_data.urgency}\n"
            "Additional info: ${global_data.additional_info}\n\n"
            "Greet them by name and let them know you're here to help solve their problem."
        )

        self.prompt_add_section(
            "What You CAN Do",
            "You are empowered to:",
            bullets=[
                "Ask diagnostic questions to understand the problem",
                "Walk through troubleshooting steps systematically",
                "Suggest solutions and workarounds",
                "Provide technical guidance and instructions",
                "Help them resolve the issue"
            ]
        )

        self.prompt_add_section(
            "Troubleshooting Approach",
            "Start with the basics and work up:",
            bullets=[
                "Confirm you understand the issue",
                "Ask clarifying questions if needed",
                "Start with simple/common fixes first",
                "Walk through steps clearly, one at a time",
                "Confirm each step works before moving on",
                "If stuck after 3-4 attempts, offer human escalation"
            ]
        )

        self.prompt_add_section(
            "Escalation",
            "If you can't resolve the issue after reasonable troubleshooting, "
            "or if they request a human, use the escalate_to_human tool."
        )

        self.define_tool(
            name="escalate_to_human",
            description="Connect to human support for complex issues or by request",
            parameters={
                "reason": {"type": "string", "description": "Reason for escalation"}
            },
            handler=self.escalate_to_human
        )

    def _check_basic_auth(self, request) -> bool:
        return True

    def escalate_to_human(self, args, raw_data):
        """Escalate to human support"""
        reason = args.get("reason", "")
        base_url = get_base_url_from_global_data(raw_data)
        global_data = raw_data.get('global_data', {})

        # Get conference name and call DB ID (conference-first architecture)
        conf = global_data.get('conf', '')
        call_db_id = global_data.get('call_db_id', '')

        context_data = {
            'customer_name': global_data.get('customer_name', ''),
            'reason': global_data.get('reason', ''),
            'department': 'support',
            'urgency': global_data.get('urgency', 'medium'),
            'priority': global_data.get('priority', 5),
            'additional_info': global_data.get('additional_info', ''),
            'escalation_reason': reason,
            'escalated_from': 'support_ai_specialist',
            'preferred_handling': 'human',
            'source_agent': 'support_ai_specialist'
        }

        context_json = json.dumps(context_data)
        context_b64 = base64.urlsafe_b64encode(context_json.encode()).decode()
        queue_url = f"{base_url}/api/queues/support/route?ctx={context_b64}"
        if conf:
            queue_url += f"&conf={conf}"
        if call_db_id:
            queue_url += f"&call_db_id={call_db_id}"

        print(f"Escalating to human support: {queue_url}", flush=True)

        result = SwaigFunctionResult(
            "I'll connect you with a support specialist who can help with that."
        )
        result.update_global_data(context_data)
        result.swml_transfer(queue_url, "", final=True)
        return result


# ============================================================
# OUTBOUND AI AGENTS
# Used when an agent sends an AI agent to call a customer.
# These receive CRM context (name, company, tier, notes) via
# global_data, set from the ?ctx= query param.
# ============================================================

class OutboundSalesAgent(AgentBase):
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

        self.set_params({
            "wait_for_user": False,
            "end_of_speech_timeout": 1000
        })

        self.set_dynamic_config_callback(capture_base_url)

        # Add knowledge base search (pgvector RAG)
        add_knowledge_search(self, 'outbound-sales', fallback_collection='sales_knowledge')

        self.set_post_prompt("""
Summarize this outbound sales call and return a JSON object with:
{
    "customer_name": "Name of customer called",
    "company": "Company name if known",
    "products_discussed": ["List of products/services discussed"],
    "customer_interest_level": "high/medium/low/none",
    "next_steps": "Recommended follow-up actions",
    "outcome": "interested/callback_requested/not_interested/no_answer/voicemail"
}
""")

        self.prompt_add_section(
            "Role",
            "You are Alex, a friendly and professional AI sales representative making an outbound call. "
            "You are calling on behalf of the company to connect with this customer."
        )

        self.prompt_add_section(
            "Customer Information",
            "Customer name: ${global_data.contact_name}\n"
            "Company: ${global_data.company}\n"
            "Account tier: ${global_data.account_tier}\n"
            "VIP customer: ${global_data.is_vip}\n"
            "Previous interactions: ${global_data.total_calls} calls\n"
            "Notes: ${global_data.notes}\n"
            "Special instructions: ${global_data.additional_context}"
        )

        self.prompt_add_section(
            "Call Approach",
            "You are making a proactive outbound call. Guidelines:",
            bullets=[
                "Introduce yourself and the company right away",
                "Address the customer by name",
                "If there are special instructions, follow them",
                "Be respectful of their time — ask if now is a good moment",
                "If they're a VIP or enterprise customer, acknowledge their importance",
                "Tailor your pitch based on their account tier and history",
                "If they have notes from previous interactions, reference them naturally"
            ]
        )

        self.prompt_add_section(
            "What You CAN Do",
            "You are empowered to:",
            bullets=[
                "Discuss products, services, features, and pricing",
                "Make personalized recommendations",
                "Schedule follow-up calls or demos",
                "Answer questions about the company's offerings",
                "Offer promotional deals if appropriate"
            ]
        )

        self.prompt_add_section(
            "Escalation",
            "If they want to speak with a human representative, "
            "or need help beyond sales, use the transfer_to_human tool."
        )

        self.define_tool(
            name="transfer_to_human",
            description="Connect to a human sales representative when the customer requests it",
            parameters={
                "reason": {"type": "string", "description": "Reason for transfer"}
            },
            handler=self.transfer_to_human
        )

    def _check_basic_auth(self, request) -> bool:
        return True

    def transfer_to_human(self, args, raw_data):
        """Transfer to human sales rep"""
        reason = args.get("reason", "")
        base_url = get_base_url_from_global_data(raw_data)
        global_data = raw_data.get('global_data', {})

        # Get conference name if available (conference-first architecture)
        conf = global_data.get('conf', '')
        call_db_id = global_data.get('call_db_id', '')

        context_data = {
            'customer_name': global_data.get('contact_name', ''),
            'company': global_data.get('company', ''),
            'department': 'sales',
            'reason': reason,
            'urgency': 'medium',
            'priority': 5,
            'additional_info': global_data.get('additional_context', ''),
            'escalated_from': 'outbound_sales_agent',
            'preferred_handling': 'human',
            'source_agent': 'outbound_sales_agent'
        }

        context_json = json.dumps(context_data)
        context_b64 = base64.urlsafe_b64encode(context_json.encode()).decode()
        queue_url = f"{base_url}/api/queues/sales/route?ctx={context_b64}"
        if conf:
            queue_url += f"&conf={conf}"
        if call_db_id:
            queue_url += f"&call_db_id={call_db_id}"

        print(f"Outbound sales transferring to human: {queue_url}", flush=True)

        result = SwaigFunctionResult(
            "I'll connect you with a sales representative right away."
        )
        result.update_global_data(context_data)
        result.swml_transfer(queue_url, "", final=True)
        return result


class OutboundSupportAgent(AgentBase):
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

        self.set_params({
            "wait_for_user": False,
            "end_of_speech_timeout": 1000
        })

        self.set_dynamic_config_callback(capture_base_url)

        # Add knowledge base search (pgvector RAG)
        add_knowledge_search(self, 'outbound-support', fallback_collection='support_knowledge')

        self.set_post_prompt("""
Summarize this outbound support call and return a JSON object with:
{
    "customer_name": "Name of customer called",
    "company": "Company name if known",
    "issue_discussed": "What was discussed",
    "resolution": "How it was resolved, or null",
    "resolved": true/false,
    "follow_up_needed": true/false,
    "outcome": "resolved/escalated/callback_requested/no_answer/voicemail"
}
""")

        self.prompt_add_section(
            "Role",
            "You are Jordan, a friendly and professional AI support specialist making an outbound call. "
            "You are proactively reaching out to help this customer."
        )

        self.prompt_add_section(
            "Customer Information",
            "Customer name: ${global_data.contact_name}\n"
            "Company: ${global_data.company}\n"
            "Account tier: ${global_data.account_tier}\n"
            "VIP customer: ${global_data.is_vip}\n"
            "Previous interactions: ${global_data.total_calls} calls\n"
            "Notes: ${global_data.notes}\n"
            "Special instructions: ${global_data.additional_context}"
        )

        self.prompt_add_section(
            "Call Approach",
            "You are making a proactive outbound support call. Guidelines:",
            bullets=[
                "Introduce yourself and explain why you're calling",
                "Address the customer by name",
                "If there are special instructions, follow them",
                "Be respectful of their time — ask if now is a good moment",
                "If they're a VIP or enterprise customer, prioritize their experience",
                "Reference their history and any notes from previous interactions",
                "Be empathetic and solution-oriented"
            ]
        )

        self.prompt_add_section(
            "What You CAN Do",
            "You are empowered to:",
            bullets=[
                "Troubleshoot and diagnose technical issues",
                "Walk through solutions step by step",
                "Provide product guidance and best practices",
                "Schedule follow-up calls if more time is needed",
                "Escalate to human support for complex issues"
            ]
        )

        self.prompt_add_section(
            "Escalation",
            "If you can't resolve their issue, or they want to speak with a human, "
            "use the transfer_to_human tool."
        )

        self.define_tool(
            name="transfer_to_human",
            description="Connect to a human support specialist when needed",
            parameters={
                "reason": {"type": "string", "description": "Reason for transfer"}
            },
            handler=self.transfer_to_human
        )

    def _check_basic_auth(self, request) -> bool:
        return True

    def transfer_to_human(self, args, raw_data):
        """Transfer to human support"""
        reason = args.get("reason", "")
        base_url = get_base_url_from_global_data(raw_data)
        global_data = raw_data.get('global_data', {})

        # Get conference name if available (conference-first architecture)
        conf = global_data.get('conf', '')
        call_db_id = global_data.get('call_db_id', '')

        context_data = {
            'customer_name': global_data.get('contact_name', ''),
            'company': global_data.get('company', ''),
            'department': 'support',
            'reason': reason,
            'urgency': 'medium',
            'priority': 5,
            'additional_info': global_data.get('additional_context', ''),
            'escalated_from': 'outbound_support_agent',
            'preferred_handling': 'human',
            'source_agent': 'outbound_support_agent'
        }

        context_json = json.dumps(context_data)
        context_b64 = base64.urlsafe_b64encode(context_json.encode()).decode()
        queue_url = f"{base_url}/api/queues/support/route?ctx={context_b64}"
        if conf:
            queue_url += f"&conf={conf}"
        if call_db_id:
            queue_url += f"&call_db_id={call_db_id}"

        print(f"Outbound support transferring to human: {queue_url}", flush=True)

        result = SwaigFunctionResult(
            "I'll connect you with a support specialist right away."
        )
        result.update_global_data(context_data)
        result.swml_transfer(queue_url, "", final=True)
        return result


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
    print(f'\nDatabase: {DATABASE_URL[:50]}...' if DATABASE_URL else '\nDatabase: NOT CONFIGURED')
    print('\nStarting agent server on port 8080...\n')

    server.run()
