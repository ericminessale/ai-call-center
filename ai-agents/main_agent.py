#!/usr/bin/env python3
"""
SignalWire Call Center AI Agents
Based on the multi-agent server example from signalwire-agents
"""

from signalwire_agents import AgentBase, AgentServer
from signalwire_agents.core.function_result import SwaigFunctionResult
import os
from dotenv import load_dotenv
import requests
from typing import Dict, Any, Optional

load_dotenv()

# Configuration
BACKEND_URL = os.getenv('BACKEND_URL', 'http://backend:5000')

# The signalwire-agents SDK will automatically handle authentication
# It checks for SWML_BASIC_AUTH_USER and SWML_BASIC_AUTH_PASSWORD env vars
# If not set, it will auto-generate credentials and display them

class BasicReceptionist(AgentBase):
    """Main receptionist - determines department routing"""

    def __init__(self):
        super().__init__(
            name="BasicReceptionist",
            route="/receptionist",
            auto_answer=True
        )

        # Build the prompt using the POM structure
        self.prompt_add_section(
            "Role",
            "You are the main receptionist for our call center. "
            "Your job is to greet callers warmly, understand their needs, "
            "and route them to the appropriate department (Sales or Support)."
        )

        self.prompt_add_section(
            "Guidelines",
            "Follow these guidelines:",
            bullets=[
                "Greet callers warmly and professionally",
                "Ask how you can help them today",
                "Determine if they need Sales or Support",
                "Transfer them to the appropriate department",
                "If unclear, ask clarifying questions"
            ]
        )

        # Define tools using define_tool method
        self.define_tool(
            name="transfer_to_department",
            description="Transfer call to Sales or Support department",
            parameters={
                "department": {"type": "string", "description": "Department name (sales or support)"},
                "customer_name": {"type": "string", "description": "Customer's name if provided"},
                "reason": {"type": "string", "description": "Reason for the call"}
            },
            handler=self.transfer_to_department
        )

    def _check_basic_auth(self, request) -> bool:
        """Override to disable authentication - agents are behind nginx"""
        return True

    def transfer_to_department(self, args, raw_data):
        """Transfer call to Sales or Support department"""
        department = args.get("department", "")
        customer_name = args.get("customer_name", "")
        reason = args.get("reason", "")

        route = '/sales' if 'sales' in department.lower() else '/support'

        result = SwaigFunctionResult(
            f'I understand you need help with {department}. Let me transfer you to our {department} department.'
        )
        result.add_action('transfer', {'to': route})
        result.set_global_data({
            'customer_name': customer_name,
            'initial_request': reason,
            'from_receptionist': True
        })

        return result

class SalesReceptionist(AgentBase):
    """Sales department receptionist"""

    def __init__(self):
        super().__init__(
            name="SalesReceptionist",
            route="/sales",
            auto_answer=True
        )

        self.prompt_add_section(
            "Role",
            "You are the Sales department receptionist. "
            "Welcome customers, gather information about their interests, "
            "and route them to either an AI specialist or human agent."
        )

        self.prompt_add_section(
            "Process",
            "Follow this process:",
            bullets=[
                "Welcome the customer to sales",
                "Ask about their product/service interests",
                "Gather relevant information",
                "Ask if they prefer AI or human assistance",
                "Route them appropriately"
            ]
        )

        self.define_tool(
            name="save_sales_info",
            description="Save sales inquiry information",
            parameters={
                "customer_name": {"type": "string", "description": "Customer name"},
                "interest": {"type": "string", "description": "What they're interested in"},
                "company": {"type": "string", "description": "Company name (optional)"},
                "budget": {"type": "string", "description": "Budget range (optional)"}
            },
            handler=self.save_sales_info
        )

        self.define_tool(
            name="route_to_agent",
            description="Route to AI specialist or human queue",
            parameters={
                "agent_type": {"type": "string", "description": "Type of agent (ai or human)"}
            },
            handler=self.route_to_agent
        )

    def _check_basic_auth(self, request) -> bool:
        """Override to disable authentication - agents are behind nginx"""
        return True

    def save_sales_info(self, args, raw_data):
        """Save sales inquiry information"""
        customer_name = args.get("customer_name", "")
        interest = args.get("interest", "")
        company = args.get("company", "")
        budget = args.get("budget", "")

        result = SwaigFunctionResult(
            f'Thank you {customer_name}. I have your information about {interest}. '
            'Would you prefer our AI sales specialist or a human representative?'
        )
        result.set_global_data({
            'customer_name': customer_name,
            'interest': interest,
            'company': company,
            'budget': budget,
            'department': 'sales'
        })
        return result

    def route_to_agent(self, args, raw_data):
        """Route to AI specialist or human queue"""
        agent_type = args.get("agent_type", "")

        if 'ai' in agent_type.lower() or 'specialist' in agent_type.lower():
            result = SwaigFunctionResult(
                'Great! Our AI sales specialist has access to all our product information. Connecting you now...'
            )
            result.add_action('transfer', {'to': '/sales-ai'})
        else:
            # Call backend to add to queue
            try:
                requests.post(f"{BACKEND_URL}/api/queues/sales/route", json={'priority': 5})
            except:
                pass

            result = SwaigFunctionResult(
                'I\'ll add you to our queue for the next available human sales representative.'
            )
            result.add_action('queue', {'name': 'sales'})

        return result

class SupportReceptionist(AgentBase):
    """Support department receptionist"""

    def __init__(self):
        super().__init__(
            name="SupportReceptionist",
            route="/support",
            auto_answer=True
        )

        self.prompt_add_section(
            "Role",
            "You are the Support department receptionist. "
            "Help customers by understanding their issues, "
            "determining urgency, and routing to appropriate assistance."
        )

        self.prompt_add_section(
            "Process",
            "Support process:",
            bullets=[
                "Welcome the customer to support",
                "Understand the nature of their issue",
                "Determine urgency level",
                "Ask if they prefer AI or human support",
                "Route them appropriately"
            ]
        )

        self.define_tool(
            name="save_support_info",
            description="Save support issue information",
            parameters={
                "issue_description": {"type": "string", "description": "Description of the issue"},
                "urgency": {"type": "string", "description": "Urgency level (low/medium/high)"},
                "error_message": {"type": "string", "description": "Any error messages (optional)"}
            },
            handler=self.save_support_info
        )

        self.define_tool(
            name="route_to_agent",
            description="Route to AI specialist or human queue",
            parameters={
                "agent_type": {"type": "string", "description": "Type of agent (ai or human)"}
            },
            handler=self.route_to_agent
        )

    def _check_basic_auth(self, request) -> bool:
        """Override to disable authentication - agents are behind nginx"""
        return True

    def save_support_info(self, args, raw_data):
        """Save support issue information"""
        issue_description = args.get("issue_description", "")
        urgency = args.get("urgency", "medium")
        error_message = args.get("error_message", "")

        urgency_map = {'low': 10, 'medium': 5, 'high': 1}
        priority = urgency_map.get(urgency.lower(), 5)

        result = SwaigFunctionResult(
            f'I understand you\'re experiencing issues with {issue_description}. '
            'Would you like our AI support specialist or a human agent?'
        )
        result.set_global_data({
            'issue': issue_description,
            'urgency': urgency,
            'error_message': error_message,
            'priority': priority,
            'department': 'support'
        })
        return result

    def route_to_agent(self, args, raw_data):
        """Route to AI specialist or human queue"""
        agent_type = args.get("agent_type", "")

        if 'ai' in agent_type.lower() or 'specialist' in agent_type.lower():
            result = SwaigFunctionResult(
                'Our AI support specialist can help troubleshoot right away. Connecting you now...'
            )
            result.add_action('transfer', {'to': '/support-ai'})
        else:
            result = SwaigFunctionResult(
                'I\'ll connect you with a human support representative.'
            )
            result.add_action('queue', {'name': 'support'})

        return result

class SalesAISpecialist(AgentBase):
    """AI Sales Specialist"""

    def __init__(self):
        super().__init__(
            name="SalesAISpecialist",
            route="/sales-ai",
            auto_answer=True
        )

        self.prompt_add_section(
            "Role",
            "You are an AI sales specialist with deep product knowledge. "
            "Help customers understand our offerings and make purchasing decisions."
        )

        self.prompt_add_section(
            "Capabilities",
            "You can:",
            bullets=[
                "Answer detailed product questions",
                "Explain features and benefits",
                "Discuss pricing options",
                "Compare different solutions",
                "Help choose the right product"
            ]
        )

        self.prompt_add_section(
            "Escalation",
            "Escalate to human for:",
            bullets=[
                "Contract negotiation",
                "Custom enterprise pricing",
                "Complex technical requirements",
                "Special arrangements"
            ]
        )

        self.define_tool(
            name="search_products",
            description="Search product knowledge base",
            parameters={
                "query": {"type": "string", "description": "Search query"}
            },
            handler=self.search_products
        )

        self.define_tool(
            name="escalate_to_human",
            description="Escalate to human sales representative",
            parameters={
                "reason": {"type": "string", "description": "Reason for escalation"}
            },
            handler=self.escalate_to_human
        )

    def _check_basic_auth(self, request) -> bool:
        """Override to disable authentication - agents are behind nginx"""
        return True

    def search_products(self, args, raw_data):
        """Search product knowledge base"""
        query = args.get("query", "")

        # In production, this would query a real knowledge base
        return SwaigFunctionResult(
            f'I found information about {query}. [Product details would appear here]'
        )

    def escalate_to_human(self, args, raw_data):
        """Escalate complex sales to human"""
        reason = args.get("reason", "")

        result = SwaigFunctionResult(
            f'For {reason}, I\'ll connect you with a human sales representative.'
        )
        result.add_action('queue', {'name': 'sales'})
        result.set_global_data({
            'escalation_reason': reason,
            'needs_senior_rep': True
        })
        return result

class SupportAISpecialist(AgentBase):
    """AI Support Specialist"""

    def __init__(self):
        super().__init__(
            name="SupportAISpecialist",
            route="/support-ai",
            auto_answer=True
        )

        self.prompt_add_section(
            "Role",
            "You are an AI support specialist trained to troubleshoot issues. "
            "Help customers resolve problems efficiently."
        )

        self.prompt_add_section(
            "Capabilities",
            "Support capabilities:",
            bullets=[
                "Diagnose common problems",
                "Walk through troubleshooting steps",
                "Check system status",
                "Provide workarounds",
                "Access documentation"
            ]
        )

        self.define_tool(
            name="check_system_status",
            description="Check service status",
            parameters={
                "service": {"type": "string", "description": "Service name"}
            },
            handler=self.check_system_status
        )

        self.define_tool(
            name="search_knowledge_base",
            description="Search support knowledge base",
            parameters={
                "query": {"type": "string", "description": "Search query"}
            },
            handler=self.search_knowledge_base
        )

        self.define_tool(
            name="escalate_to_human",
            description="Escalate to human support",
            parameters={
                "reason": {"type": "string", "description": "Reason for escalation"},
                "ticket_id": {"type": "string", "description": "Ticket ID (optional)"}
            },
            handler=self.escalate_to_human
        )

    def _check_basic_auth(self, request) -> bool:
        """Override to disable authentication - agents are behind nginx"""
        return True

    def check_system_status(self, args, raw_data):
        """Check status of a service"""
        service = args.get("service", "")

        return SwaigFunctionResult(
            f'The {service} service is currently operational with no known issues.'
        )

    def search_knowledge_base(self, args, raw_data):
        """Search support knowledge base"""
        query = args.get("query", "")

        return SwaigFunctionResult(
            f'I found articles about {query}. Let me walk you through the solution...'
        )

    def escalate_to_human(self, args, raw_data):
        """Escalate unresolved issues to human support"""
        reason = args.get("reason", "")
        ticket_id = args.get("ticket_id", None)

        result = SwaigFunctionResult(
            'I\'ll connect you with a human support representative for further assistance.'
        )
        result.add_action('queue', {'name': 'support'})
        result.set_global_data({
            'escalation_reason': reason,
            'ticket_id': ticket_id,
            'attempted_solutions': True
        })
        return result


if __name__ == '__main__':
    print('='*60)
    print('SignalWire AI Call Center - Starting')
    print('='*60)

    # Create the AgentServer (authentication via env vars SWML_BASIC_AUTH_USER/PASSWORD)
    server = AgentServer(host='0.0.0.0', port=8080)

    # Initialize and register all agents
    receptionist = BasicReceptionist()
    sales = SalesReceptionist()
    support = SupportReceptionist()
    sales_ai = SalesAISpecialist()
    support_ai = SupportAISpecialist()

    # Register agents with the server
    server.register(receptionist, '/receptionist')
    server.register(sales, '/sales')
    server.register(support, '/support')
    server.register(sales_ai, '/sales-ai')
    server.register(support_ai, '/support-ai')

    # Get auth credentials from the first agent
    username, password = receptionist.get_basic_auth_credentials()

    print('\n📡 Authentication Credentials:')
    print(f'  Username: {username}')
    print(f'  Password: {password}')
    print('\n🔗 SignalWire Webhook URL format:')
    print(f'  https://{username}:{password}@your-ngrok-url.ngrok.io/receptionist')

    print('\n📍 Registered Routes:')
    print('  - /receptionist: Main receptionist')
    print('  - /sales       : Sales department')
    print('  - /support     : Support department')
    print('  - /sales-ai    : AI Sales specialist')
    print('  - /support-ai  : AI Support specialist')
    print('\n🔧 Backend URL:', BACKEND_URL)
    print('🌐 Server Address: http://0.0.0.0:8080')
    print('\nStarting server...\n')

    # Start the server using the SDK's proper method
    server.run()