#!/usr/bin/env python3
"""
SignalWire Call Center AI Agents
Refactored to use contexts/steps for structured flow
"""

from signalwire_agents import AgentBase, AgentServer
from signalwire_agents.core.function_result import SwaigFunctionResult
import os
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
    from urllib.parse import urlparse

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


class CallCenterAgent(AgentBase):
    """
    Main Call Center Agent using contexts/steps for structured flow.

    Flow:
    1. Default context: Greet, get name, determine sales vs support
    2. Sales context: Brief intake, offer human or AI choice
    3. Support context: Brief intake, offer human or AI choice

    Then routes to specialist agents or human queue.
    """

    def __init__(self):
        super().__init__(
            name="CallCenterAgent",
            route="/receptionist",
            auto_answer=True
        )

        self.set_dynamic_config_callback(capture_base_url)

        # Base prompt that applies globally
        self.prompt_add_section(
            "Base Instructions",
            "You are Sarah, a friendly customer service representative. "
            "Follow the structured workflow to help customers efficiently."
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
        # DEFAULT CONTEXT - Initial greeting and triage
        # ============================================================
        default_ctx = contexts.add_context("default")

        # Step 1: Greeting and name collection
        default_ctx.add_step("greeting") \
            .add_section("Current Task", "Greet the caller warmly and get their name") \
            .add_bullets("What to do", [
                "Say: 'Hi, thank you for calling! I'm Sarah, how can I help you today?'",
                "If they explain their issue first, acknowledge it then ask for their name",
                "Get their name before moving on - this is required"
            ]) \
            .set_step_criteria("Customer has provided their name") \
            .set_valid_steps(["determine_need"])

        # Step 2: Determine if sales or support
        default_ctx.add_step("determine_need") \
            .add_section("Current Task", "Understand what they need help with") \
            .add_bullets("Listen for clues", [
                "SALES: buying, pricing, products, interested in, purchase, plans, features",
                "SUPPORT: problem, issue, not working, error, help with, broken, trouble"
            ]) \
            .add_section("Navigation",
                "Once you understand their need:\n"
                "- If SALES related: change_context to 'sales'\n"
                "- If SUPPORT related: change_context to 'support'\n\n"
                "Do NOT announce the change - just continue naturally.") \
            .set_step_criteria("Customer's need (sales or support) has been identified") \
            .set_valid_contexts(["sales", "support"])

        # ============================================================
        # SALES CONTEXT - Sales-specific intake
        # ============================================================
        sales_ctx = contexts.add_context("sales") \
            .set_isolated(True)

        # Sales context prompt (applies to all steps in this context)
        sales_ctx.add_section("Role",
            "You are continuing the conversation as Sarah. The customer needs sales help. "
            "Do NOT re-introduce yourself. Use their name from the conversation.")

        # Sales Step 1: Brief detail gathering
        sales_ctx.add_step("gather_details") \
            .add_section("Current Task", "Quickly gather 1-2 relevant details") \
            .add_bullets("Ask about", [
                "What product or service interests them",
                "Company name if relevant (B2B)"
            ]) \
            .add_section("Important", "Keep this brief - just 1-2 questions max, then move to offer_choice. Remember what they tell you for the transfer.") \
            .set_step_criteria("Basic sales details gathered") \
            .set_valid_steps(["offer_choice"])

        # Sales Step 2: Offer the choice
        sales_ctx.add_step("offer_choice") \
            .add_section("CRITICAL TASK",
                "You MUST ask this question - do not skip it:\n\n"
                "'Would you like to speak with one of our sales representatives, "
                "or would you like me to assist you?'") \
            .add_section("After they answer",
                "- If they want human/representative: use transfer_to_human tool\n"
                "- If they want you/AI to help: use transfer_to_ai_specialist tool\n\n"
                "Include all collected info: customer_name, reason, department='sales', urgency, additional_info") \
            .set_step_criteria("Customer has chosen human or AI assistance")

        # ============================================================
        # SUPPORT CONTEXT - Support-specific intake
        # ============================================================
        support_ctx = contexts.add_context("support") \
            .set_isolated(True)

        # Support context prompt
        support_ctx.add_section("Role",
            "You are continuing the conversation as Sarah. The customer needs technical support. "
            "Do NOT re-introduce yourself. Use their name from the conversation.")

        # Support Step 1: Brief detail gathering
        support_ctx.add_step("gather_details") \
            .add_section("Current Task", "Quickly gather 1-2 relevant details about the issue") \
            .add_bullets("Ask about", [
                "Any error messages they're seeing",
                "How urgent/critical is this issue (high/medium/low)"
            ]) \
            .add_section("Important", "Keep this brief - just 1-2 questions max, then move to offer_choice. Remember what they tell you for the transfer.") \
            .set_step_criteria("Basic issue details gathered") \
            .set_valid_steps(["offer_choice"])

        # Support Step 2: Offer the choice
        support_ctx.add_step("offer_choice") \
            .add_section("CRITICAL TASK",
                "You MUST ask this question - do not skip it:\n\n"
                "'Would you like to speak with a support specialist, "
                "or would you like me to help you troubleshoot?'") \
            .add_section("After they answer",
                "- If they want human/specialist: use transfer_to_human tool\n"
                "- If they want you/AI to help: use transfer_to_ai_specialist tool\n\n"
                "Include all collected info: customer_name, reason, department='support', urgency, additional_info (like error messages)") \
            .set_step_criteria("Customer has chosen human or AI assistance")

        # ============================================================
        # TOOLS - Only for final transfer (context switching is automatic)
        # ============================================================

        # Tool to transfer to human queue or AI specialist
        self.define_tool(
            name="transfer_to_human",
            description="Transfer customer to a human representative. Call this when they choose to speak with a human.",
            parameters={
                "customer_name": {"type": "string", "description": "Customer's name"},
                "reason": {"type": "string", "description": "What they need help with"},
                "department": {"type": "string", "description": "'sales' or 'support'"},
                "urgency": {"type": "string", "description": "How urgent (high/medium/low)"},
                "additional_info": {"type": "string", "description": "Any other relevant details (error message, product interest, etc.)"}
            },
            handler=self.transfer_to_human
        )

        # Tool to transfer to AI specialist
        self.define_tool(
            name="transfer_to_ai_specialist",
            description="Transfer customer to AI specialist for further assistance. Call this when they choose AI help.",
            parameters={
                "customer_name": {"type": "string", "description": "Customer's name"},
                "reason": {"type": "string", "description": "What they need help with"},
                "department": {"type": "string", "description": "'sales' or 'support'"},
                "urgency": {"type": "string", "description": "How urgent (high/medium/low)"},
                "additional_info": {"type": "string", "description": "Any other relevant details (error message, product interest, etc.)"}
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
        queue_url = f"{base_url}/api/queues/{department}/route"

        print(f"Transferring {customer_name} to human queue: {queue_url}", flush=True)

        # Map urgency to priority
        urgency_map = {'high': 2, 'medium': 5, 'low': 8}
        priority = urgency_map.get(urgency.lower(), 5)

        result = SwaigFunctionResult(
            "I'll connect you with a representative right now."
        )
        result.update_global_data({
            'customer_name': customer_name,
            'reason': reason,
            'department': department,
            'urgency': urgency,
            'priority': priority,
            'additional_info': additional_info,
            'preferred_handling': 'human'
        })
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
    """AI Sales Specialist - handles actual sales conversations"""

    def __init__(self):
        super().__init__(
            name="SalesAISpecialist",
            route="/sales-ai",
            auto_answer=True
        )

        # Speak first after transfer
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
            "You are an AI sales specialist continuing the conversation. "
            "The customer chose to get AI assistance for their sales inquiry."
        )

        self.prompt_add_section(
            "Customer Context",
            "Customer name: ${global_data.customer_name}\n"
            "Interest: ${global_data.reason}\n"
            "Company: ${global_data.company}\n\n"
            "Address them by name and help with their inquiry."
        )

        self.prompt_add_section(
            "Your Job",
            "Help the customer with their sales questions:",
            bullets=[
                "Answer product and pricing questions",
                "Explain features and benefits",
                "Make recommendations based on their needs",
                "If they need something you can't help with, offer to connect to human"
            ]
        )

        self.define_tool(
            name="escalate_to_human",
            description="Escalate to human sales rep if needed",
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
        queue_url = f"{base_url}/api/queues/sales/route"

        result = SwaigFunctionResult(
            "I'll connect you with a sales representative who can help with that."
        )
        result.update_global_data({
            'escalation_reason': reason,
            'escalated_from': 'sales_ai_specialist'
        })
        result.swml_transfer(queue_url, "", final=True)
        return result


class SupportAISpecialist(AgentBase):
    """AI Support Specialist - handles actual troubleshooting"""

    def __init__(self):
        super().__init__(
            name="SupportAISpecialist",
            route="/support-ai",
            auto_answer=True
        )

        # Speak first after transfer
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
            "You are an AI support specialist continuing the conversation. "
            "The customer chose to get AI assistance for troubleshooting."
        )

        self.prompt_add_section(
            "Customer Context",
            "Customer name: ${global_data.customer_name}\n"
            "Issue: ${global_data.reason}\n"
            "Error message: ${global_data.error_message}\n"
            "Urgency: ${global_data.urgency}\n\n"
            "Address them by name and help resolve their issue."
        )

        self.prompt_add_section(
            "Your Job",
            "Help troubleshoot and resolve their issue:",
            bullets=[
                "Diagnose the problem systematically",
                "Walk through troubleshooting steps",
                "Provide clear instructions",
                "If you can't resolve it, offer to connect to human specialist"
            ]
        )

        self.define_tool(
            name="escalate_to_human",
            description="Escalate to human support if needed",
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
        queue_url = f"{base_url}/api/queues/support/route"

        result = SwaigFunctionResult(
            "I'll connect you with a support specialist who can help with that."
        )
        result.update_global_data({
            'escalation_reason': reason,
            'escalated_from': 'support_ai_specialist'
        })
        result.swml_transfer(queue_url, "", final=True)
        return result


if __name__ == '__main__':
    print('=' * 60)
    print('SignalWire AI Call Center - Contexts/Steps Architecture')
    print('=' * 60)

    server = AgentServer(host='0.0.0.0', port=8080)

    # Main agent with contexts/steps
    call_center = CallCenterAgent()

    # Specialist agents (only reached after customer chooses AI)
    sales_ai = SalesAISpecialist()
    support_ai = SupportAISpecialist()

    # Register agents
    server.register(call_center, '/receptionist')
    server.register(sales_ai, '/sales-ai')
    server.register(support_ai, '/support-ai')

    username, password = call_center.get_basic_auth_credentials()

    print('\nAuthentication Credentials:')
    print(f'  Username: {username}')
    print(f'  Password: {password}')
    print('\nRegistered Routes:')
    print('  - /receptionist : Main agent (contexts: default, sales, support)')
    print('  - /sales-ai     : AI Sales specialist')
    print('  - /support-ai   : AI Support specialist')
    print('\nFlow:')
    print('  1. /receptionist handles greeting + triage')
    print('  2. Context switches to sales or support')
    print('  3. Customer chooses human or AI')
    print('  4. Routes to queue or specialist agent')
    print('\nStarting server...\n')

    server.run()
