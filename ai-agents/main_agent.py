#!/usr/bin/env python3
"""
SignalWire Call Center AI Agents
Triage agent using contexts/steps - NO problem solving, info gathering only.
AI Specialists (separate agents) are the ONLY ones that solve problems.
"""

from signalwire_agents import AgentBase, AgentServer
from signalwire_agents.core.function_result import SwaigFunctionResult
import os
import json
import base64
from dotenv import load_dotenv

load_dotenv()

# Configuration
BACKEND_URL = os.getenv('BACKEND_URL', 'http://backend:5000')


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
    """Dynamic config callback - captures external URL and sets post_prompt_url."""
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

    if new_global:
        agent.set_global_data(new_global)


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

        # ============================================================
        # GLOBAL PROMPT - Applies to ALL contexts
        # Just defines personality - NO problem solving instructions
        # ============================================================
        self.prompt_add_section(
            "Identity",
            "You are Sarah, a friendly and efficient customer service representative. "
            "Your ONLY job is to gather information and route calls appropriately."
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
            "Do NOT engage with the problem itself."
        )

        # Configure post_prompt for call summaries
        self.set_post_prompt("""
Summarize this call and return a JSON object with:
{
    "customer_name": "Name if provided, or null",
    "department": "sales/support/unknown",
    "reason": "Brief reason for their call",
    "outcome": "transferred_to_human/transferred_to_ai/abandoned",
    "notes": "Any important details"
}
""")

        # Define the contexts and steps
        contexts = self.define_contexts()

        # ============================================================
        # TRIAGE CONTEXT (default) - Initial greeting and routing
        # ============================================================
        triage_ctx = contexts.add_context("default")

        # Step 1: Greeting and NAME collection (REQUIRED before proceeding)
        triage_ctx.add_step("get_name") \
            .add_section("Your Task", "Greet the caller and get their name.") \
            .add_section("What to Say",
                "'Hi, thank you for calling! I'm Sarah. May I have your name please?'") \
            .add_section("IMPORTANT",
                "You MUST get their name before moving on. If they start explaining "
                "their issue, say 'I'd be happy to help with that - may I first get your name?'") \
            .set_step_criteria("Customer has clearly stated their name") \
            .set_valid_steps(["get_purpose"])

        # Step 2: Get PURPOSE (REQUIRED before routing)
        triage_ctx.add_step("get_purpose") \
            .add_section("Your Task", "Find out what they're calling about.") \
            .add_section("What to Say",
                "'Thanks [name]! Are you calling about a purchase or product inquiry, "
                "or do you need help with an existing issue?'") \
            .add_section("Listen For",
                "SALES: buying, pricing, products, interested in, purchase, plans, features, quote\n"
                "SUPPORT: problem, issue, not working, error, help with, broken, trouble, fix") \
            .add_section("Then Route",
                "Once clear:\n"
                "- Sales-related: change_context to 'sales'\n"
                "- Support-related: change_context to 'support'\n"
                "Do NOT announce the change.") \
            .set_step_criteria("Customer's need (sales or support) has been clearly identified") \
            .set_valid_contexts(["sales", "support"])

        # ============================================================
        # SALES CONTEXT - Sales info gathering (NO selling)
        # ============================================================
        sales_ctx = contexts.add_context("sales") \
            .set_isolated(True)

        # Sales context prompt
        sales_ctx.add_section("Role",
            "Continue as Sarah. Customer needs sales help. Use their name.")

        sales_ctx.add_section("REMEMBER",
            "You are TRIAGE only. Do NOT answer product questions, provide pricing, "
            "or make recommendations. Just gather info for the transfer.")

        # Sales Step 1: Brief info gathering
        sales_ctx.add_step("gather_info") \
            .add_section("Your Task", "Collect basic info for the sales team.") \
            .add_bullets("Ask These Questions (one at a time)", [
                "What product or service are you interested in?",
                "Is this for yourself or a business?"
            ]) \
            .add_section("CRITICAL",
                "Do NOT answer their questions. If they ask about features/pricing, say: "
                "'Great question - let me connect you with someone who can give you detailed information on that.'") \
            .set_step_criteria("Basic sales context collected (product interest, personal/business)") \
            .set_valid_steps(["transfer_choice"])

        # Sales Step 2: Transfer choice
        sales_ctx.add_step("transfer_choice") \
            .add_section("Your Task", "Ask how they'd like to proceed.") \
            .add_section("What to Say",
                "'I can connect you with one of our sales representatives, "
                "or if you prefer, our AI sales assistant can help you right now. "
                "Which would you prefer?'") \
            .add_section("After They Answer",
                "- Want human/representative/person: use transfer_to_human tool\n"
                "- Want AI/you/assistant: use transfer_to_ai_specialist tool\n\n"
                "Include: customer_name, reason (product interest), department='sales', "
                "urgency='medium', additional_info (business/personal)") \
            .set_step_criteria("Customer has chosen human or AI assistance")

        # ============================================================
        # SUPPORT CONTEXT - Support info gathering (NO troubleshooting)
        # ============================================================
        support_ctx = contexts.add_context("support") \
            .set_isolated(True)

        # Support context prompt
        support_ctx.add_section("Role",
            "Continue as Sarah. Customer needs support. Use their name.")

        support_ctx.add_section("CRITICAL - NO TROUBLESHOOTING",
            "You are TRIAGE only. You must NOT:\n"
            "- Ask diagnostic questions (did you try X? is Y plugged in?)\n"
            "- Suggest any fixes or workarounds\n"
            "- Attempt to solve or diagnose the problem\n\n"
            "Follow the steps IN ORDER. Do not skip steps.")

        # Support Step 1: Acknowledge and confirm issue
        # Even if they already described it, we acknowledge and confirm
        support_ctx.add_step("acknowledge_issue") \
            .add_section("Your Task", "Acknowledge what they've told you and confirm you understand.") \
            .add_section("What to Say",
                "Acknowledge their issue with empathy:\n"
                "'I understand, [brief restatement of their issue]. That sounds frustrating. "
                "Let me get you connected with someone who can help.'") \
            .add_section("IMPORTANT",
                "Do NOT ask diagnostic questions. Do NOT offer solutions.\n"
                "Just acknowledge and move to the next step.") \
            .set_step_criteria("Agent has acknowledged the customer's issue") \
            .set_valid_steps(["get_urgency"])

        # Support Step 2: Urgency (simple question)
        support_ctx.add_step("get_urgency") \
            .add_section("Your Task", "Ask ONE question about urgency.") \
            .add_section("What to Say",
                "'Is this urgent - like it's blocking your work - or is it something "
                "that can wait a bit?'") \
            .add_section("Map Their Response",
                "Blocking/urgent/critical/ASAP = 'high'\n"
                "Normal/whenever/not urgent = 'medium'\n"
                "Low priority/no rush = 'low'") \
            .set_step_criteria("Customer has indicated urgency level") \
            .set_valid_steps(["transfer_choice"])

        # Support Step 3: Transfer choice - ALWAYS ask this
        support_ctx.add_step("transfer_choice") \
            .add_section("Your Task", "Ask how they'd like to proceed. This is REQUIRED.") \
            .add_section("What to Say",
                "'I can connect you with one of our support specialists, "
                "or if you prefer, our AI support assistant can help you right now. "
                "Which would you prefer?'") \
            .add_section("After They Answer",
                "- Want human/specialist/person: use transfer_to_human tool\n"
                "- Want AI/you/assistant: use transfer_to_ai_specialist tool\n\n"
                "Include: customer_name, reason (issue description), department='support', "
                "urgency (high/medium/low), additional_info") \
            .set_step_criteria("Customer has explicitly chosen human or AI assistance")

        # ============================================================
        # TOOLS - Transfer functions only
        # ============================================================
        self.define_tool(
            name="transfer_to_human",
            description="Transfer customer to a human representative. Use when they choose to speak with a human.",
            parameters={
                "customer_name": {"type": "string", "description": "Customer's name"},
                "reason": {"type": "string", "description": "Brief description of what they need"},
                "department": {"type": "string", "description": "'sales' or 'support'"},
                "urgency": {"type": "string", "description": "'high', 'medium', or 'low'"},
                "additional_info": {"type": "string", "description": "Any other relevant context"}
            },
            handler=self.transfer_to_human
        )

        self.define_tool(
            name="transfer_to_ai_specialist",
            description="Transfer customer to AI specialist. Use when they choose AI assistance.",
            parameters={
                "customer_name": {"type": "string", "description": "Customer's name"},
                "reason": {"type": "string", "description": "Brief description of what they need"},
                "department": {"type": "string", "description": "'sales' or 'support'"},
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
        queue_url = f"{base_url}/api/queues/{department}/route?ctx={context_b64}"

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
        specialist_route = f"/{department}-ai"
        transfer_url = f"{base_url}{specialist_route}"

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

        print(f"Escalating to human support: {queue_url}", flush=True)

        result = SwaigFunctionResult(
            "I'll connect you with a support specialist who can help with that."
        )
        result.update_global_data(context_data)
        result.swml_transfer(queue_url, "", final=True)
        return result


if __name__ == '__main__':
    print('=' * 60)
    print('SignalWire AI Call Center - Triage + Specialists')
    print('=' * 60)

    server = AgentServer(host='0.0.0.0', port=8080)

    # Triage agent - info gathering ONLY
    triage = CallCenterTriageAgent()

    # Specialist agents - these actually solve problems
    sales_ai = SalesAISpecialist()
    support_ai = SupportAISpecialist()

    # Register agents
    server.register(triage, '/receptionist')
    server.register(sales_ai, '/sales-ai')
    server.register(support_ai, '/support-ai')

    username, password = triage.get_basic_auth_credentials()

    print('\nAuthentication:')
    print(f'  Username: {username}')
    print(f'  Password: {password}')
    print('\nRoutes:')
    print('  /receptionist : Triage agent (NO problem solving)')
    print('  /sales-ai     : Sales specialist (helps with sales)')
    print('  /support-ai   : Support specialist (troubleshoots issues)')
    print('\nFlow:')
    print('  1. /receptionist gets name, purpose, brief context')
    print('  2. Customer chooses human or AI')
    print('  3. Human -> queue, AI -> specialist agent')
    print('  4. ONLY specialist agents solve problems')
    print('\nStarting server...\n')

    server.run()
