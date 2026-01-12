# SignalWire Call Center - Development Roadmap

## Overview

This document tracks known issues, planned features, and architectural decisions for the SignalWire AI Call Center project.

---

## Architecture Decisions

### Call Flow Entry Point (CONFIRMED)

**Current Approach:** Phone number POSTs to `/api/swml/initial-call`, backend creates call record, returns SWML that transfers to AI agent.

**Status:** This is the correct approach for our use case.

**Rationale:**
- Call record exists in database before AI interaction begins
- Dashboard shows call immediately with "AI Active" status
- Allows pre-routing logic (VIP detection, blocked callers, etc.)
- Clean separation: backend owns call lifecycle, AI agents own conversations
- Alternative (AI agent notifying backend) would delay dashboard visibility and add coupling

**Alternative Considered:** Having phone number route directly to AI agent, with agent's `dynamic_config_callback` or `on_swml_request` notifying the backend. Rejected because it would delay call visibility in dashboard.

---

## Priority 1: Core Functionality

### 1.1 Caller Data Import to AI Agent
**Status:** Not implemented
**Priority:** HIGH
**Complexity:** Medium

**Goal:** When a call arrives, AI agent should have access to existing customer data from our database (name, account info, previous calls, preferences).

**Technical Approach:**
```python
# In ai-agents/main_agent.py - use dynamic_config_callback
def configure_agent_dynamically(self, query_params, body_params, headers, agent):
    caller_id = body_params.get('caller_id_num')

    # Query backend API for customer data
    response = requests.get(f"{BACKEND_URL}/api/customers/lookup?phone={caller_id}")

    if response.status_code == 200:
        customer = response.json()
        agent.set_global_data({
            "customer_id": customer.get('id'),
            "customer_name": customer.get('name'),
            "account_type": customer.get('tier'),
            "previous_calls": customer.get('call_count'),
            "known_customer": True
        })
        # Adjust prompt for known customer
        agent.prompt_add_section("customer_context", f"""
        This is a returning customer named {customer.get('name')}.
        Account type: {customer.get('tier')}
        Previous calls: {customer.get('call_count')}
        Do NOT ask for their name - you already know it.
        """)
    else:
        agent.set_global_data({"known_customer": False})
        # First-time caller prompt
        agent.prompt_add_section("customer_context", """
        This appears to be a first-time caller.
        Gather their name and create a positive first impression.
        """)
```

**Backend Work Required:**
- [ ] Create `/api/customers/lookup?phone=` endpoint
- [ ] Customer database model (or extend User model)
- [ ] Return customer data including call history

**AI Agent Work Required:**
- [ ] Add `set_dynamic_config_callback()` to each agent
- [ ] Query backend for customer data
- [ ] Adjust prompts based on known vs new customer
- [ ] Pass customer data in global_data for downstream agents

---

### 1.2 AI Agent Transfers (Not Working)
**Status:** Broken
**Priority:** HIGH
**Complexity:** Medium

**Issue:** Transfer functions in AI agents are defined but not executing properly. Calls don't actually transfer between agents or to queues.

**Investigation Needed:**
- [ ] Test transfer SWML syntax is correct
- [ ] Verify `swaig_allow_swml: true` is set
- [ ] Check if transfers work between AI agents (/receptionist → /sales)
- [ ] Check if transfers to backend queue endpoints work
- [ ] Review SignalWire documentation for transfer action format

**Current Transfer Code (main_agent.py):**
```python
result.add_action('transfer', {'to': route})
```

**May Need:**
```python
result.add_action('SWML', {
    'sections': {
        'main': [{
            'transfer': {'dest': route}
        }]
    }
})
```

---

### 1.3 Transfer to Human Agent
**Status:** Not implemented end-to-end
**Priority:** HIGH
**Complexity:** High

**Goal:** When AI decides human is needed, transfer call to available human agent in dashboard.

**Flow:**
```
AI Agent → "transfer_to_human" function → Backend queue check →
  If agent available: Transfer to agent's Call Fabric address
  If no agent: Offer callback/hold options
```

**Components Needed:**

**Backend:**
- [ ] `/api/queues/available-agents?department=` - Check agent availability
- [ ] `/api/queues/route-to-human` - SWML endpoint that routes to agent
- [ ] Agent status tracking (available/busy/offline)
- [ ] WebSocket notification to agent when call is routed

**AI Agent:**
- [ ] `transfer_to_human(department)` SWAIG function
- [ ] Check availability before transferring
- [ ] Handle "no agents available" gracefully
- [ ] Pass all context in global_data

**Frontend:**
- [ ] Agent status toggle (Go Online/Offline)
- [ ] Incoming call notification
- [ ] Call Fabric integration for receiving calls

**Call Fabric:**
- [ ] Agent registered as subscriber
- [ ] Transfer target format: `sip:{agent_email}@{space}.signalwire.com` or `/private/{subscriber_id}`

---

### 1.4 Call Fabric Token Endpoint (Broken)
**Status:** Returns 422 error
**Priority:** HIGH
**Complexity:** Medium

**Issue:** `/api/fabric/token` endpoint fails, preventing browser phone from working.

**File:** `backend/app/api/fabric.py`

**Investigation:**
- [ ] Check what SignalWire API expects for subscriber token
- [ ] Verify subscriber exists in SignalWire
- [ ] Check authentication parameters

---

## Priority 2: Outbound AI Calls

### 2.1 Outbound Call with AI Agent
**Status:** Not implemented
**Priority:** MEDIUM
**Complexity:** High

**Goal:** Place outbound calls where an AI agent handles the conversation, with goals and customer context pre-loaded.

**Technical Approach:**

**Initiating the call (Backend):**
```python
def create_outbound_ai_call(customer_id, goal, talking_points):
    # 1. Fetch customer data
    customer = db.get_customer(customer_id)

    # 2. Create call via SignalWire API
    # The SWML URL should include customer context as query params or in a session
    swml_url = f"{BASE_URL}/api/swml/outbound-ai?customer_id={customer_id}&goal={goal}"

    response = signalwire.calls.create(
        to=customer.phone,
        from_=SIGNALWIRE_NUMBER,
        url=swml_url,
        call_state_url=f"{BASE_URL}/api/webhooks/call-status"
    )
```

**SWML endpoint for outbound AI:**
```python
@swml_bp.route('/outbound-ai', methods=['POST'])
def outbound_ai_call():
    customer_id = request.args.get('customer_id')
    goal = request.args.get('goal')

    # Fetch customer data
    customer = get_customer(customer_id)

    # Return SWML that routes to outbound AI agent with context
    return {
        "version": "1.0.0",
        "sections": {
            "main": [{
                "ai": {
                    "url": f"{AGENTS_URL}/outbound-sales",
                    "params": {
                        "global_data": {
                            "customer_name": customer.name,
                            "customer_id": customer.id,
                            "call_goal": goal,
                            "call_direction": "outbound"
                        }
                    }
                }
            }]
        }
    }
```

**New AI Agent (outbound-sales):**
```python
class OutboundSalesAgent(AgentBase):
    def __init__(self):
        super().__init__(name="OutboundSalesAgent", route="/outbound-sales")

        self.prompt_add_section("main", """
        You are making an outbound sales call.
        Customer: ${global_data.customer_name}
        Goal: ${global_data.call_goal}

        Be professional and respect if they're not interested.
        """)
```

**UI Considerations:**

Option A: **Contact-Centric UI**
- Replace call list with contact/customer list
- Each contact shows: name, phone, last call, status
- Click contact → see history, place call (human or AI)
- Incoming calls associate with existing contacts

Option B: **Separate Outbound Campaign UI**
- Keep current call-centric list for active calls
- Add "Campaigns" section for outbound AI calls
- Create campaign: select contacts, set goal, launch
- View campaign results

Option C: **Unified with Call Purpose Toggle**
- When placing call, choose: "Call as Agent" or "Send AI"
- If AI, specify goal and context
- Same call list shows all calls with AI/Human indicator

**Recommendation:** Start with Option C (simplest), evolve to Option A if contact management becomes important.

---

## Priority 3: AI Agent Improvements

### 3.1 Agent Prompt Refinement
**Status:** Rough drafts only
**Priority:** MEDIUM
**Complexity:** Low

**Agents Needing Work:**
- [ ] BasicReceptionist - Improve routing logic
- [ ] SalesReceptionist - Better qualification questions
- [ ] SupportReceptionist - Severity detection, urgency handling
- [ ] SalesAISpecialist - Product knowledge, objection handling
- [ ] SupportAISpecialist - Troubleshooting flows, KB integration

**For Each Agent:**
- [ ] Review and improve system prompt
- [ ] Add appropriate skills (datetime, search, etc.)
- [ ] Test transfer functions
- [ ] Add proper error handling

---

### 3.2 Knowledge Base Integration
**Status:** Not implemented
**Priority:** MEDIUM
**Complexity:** Medium

**Goal:** AI specialists should have access to product/support knowledge bases.

**Options:**
1. **DataSphere** - SignalWire's hosted vector search
2. **native_vector_search** skill - Local vector search
3. **Custom SWAIG function** - Query your own API

**Work Required:**
- [ ] Decide on knowledge base approach
- [ ] Create/import knowledge content
- [ ] Integrate with SalesAISpecialist and SupportAISpecialist

---

## Priority 4: Dashboard & UI

### 4.1 Call List Improvements
**Status:** Basic implementation exists
**Priority:** LOW
**Complexity:** Medium

**Ideas:**
- [ ] Show customer name (not just phone) when known
- [ ] Add call purpose/goal indicator
- [ ] Show AI vs Human handling clearly
- [ ] Filter by department/queue
- [ ] Sort by priority/wait time

---

### 4.2 Outbound Call UI
**Status:** Only basic dialer exists
**Priority:** MEDIUM (after outbound AI works)
**Complexity:** Medium

**Needed:**
- [ ] Choose call type: Human or AI
- [ ] For AI: Set goal, select context
- [ ] For AI: View live transcription during call
- [ ] Campaign management (if doing Option B above)

---

## Known Bugs

### Critical
- [ ] Call Fabric token endpoint returns 422 (blocks browser phone)
- [ ] AI agent transfers not executing

### Medium
- [ ] User model ID type mismatch (needs VARCHAR(36) UUID)
- [ ] Auth token refresh issues (occasional 401 errors)

### Low
- [ ] Transcription webhook may not be receiving all events
- [ ] Some frontend components missing loading states

---

## Technical Debt

- [ ] Add proper database migrations (Flask-Migrate)
- [ ] Improve error handling in frontend (toast notifications)
- [ ] Add logging configuration (reduce verbosity in production)
- [ ] Add rate limiting to API endpoints
- [ ] Input validation/sanitization (XSS, injection prevention)
- [ ] API documentation (OpenAPI/Swagger)

---

## Research Needed

### Call Fabric Subscriber Registration
- How to programmatically create subscribers?
- How to get subscriber token for browser SDK?
- What's the exact transfer target format?

### AI Agent Transfer Syntax
- Correct SWML format for transfers within AI context
- How to pass global_data through transfers
- How to transfer to external SIP endpoints

### Outbound Call Best Practices
- Rate limiting for outbound campaigns
- Handling voicemail detection
- Compliance (TCPA, consent tracking)

---

## Completed Items

### Phase 1
- [x] Agent Dashboard UI with call lists
- [x] Supervisor Dashboard with agent monitoring
- [x] WebSocket real-time updates
- [x] Database schema for calls, transcriptions
- [x] 5 AI agents (basic implementation)
- [x] System message injection to AI

### Phase 2
- [x] AI intervention panel for supervisors
- [x] Browser phone UI (needs backend fix)
- [x] Call detail view with tabs

---

## Version History

| Version | Date | Changes |
|---------|------|---------|
| 0.1.0 | Initial | Basic project structure, AI agents |
| 0.2.0 | Nov 2024 | Agent/Supervisor dashboards, AI intervention |
| 0.3.0 | TBD | Caller data import, working transfers |

---

**Last Updated:** 2024-12-29
