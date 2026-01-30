# All-Conference Architecture Implementation Sketch

## Overview

Refactor so ALL calls use conferences from the start, including AI interactions. This provides:
- Uniform architecture (every call is a conference)
- Seamless takeover (agent joins existing conference)
- Consistent recording/analytics
- Same code paths for all human-customer interactions

---

## Current State Analysis

### Current `/api/swml/initial-call` (lines 172-212 in swml.py)
```python
# Current: Direct transfer to AI
swml_response = {
    "sections": {
        "main": [
            {"set": {"call_state_url": "..."}},
            "answer",
            {"record_call": {...}},
            {"live_transcribe": {...}},
            {"transfer": {"dest": f"{base_url}/receptionist"}}  # Direct to AI
        ]
    }
}
```

### Current Takeover (lines 354-517 in swml.py)
- Already HAS conference-based logic (`agent-conf-{user_id}`)
- But AI call is NOT in a conference, so `move_participant()` won't work
- Falls back to `connect: to: call:{call_sid}` (direct, not conference)

---

## Current vs Proposed Architecture

### Current (Inconsistent)
```
INBOUND → AI:
  Customer ←──direct call──→ AI Agent (SWML ai verb)
  (The AI call is NOT in a conference)

TRANSFER TO HUMAN (via queue):
  Customer ──→ per-call conference ←── Human Agent
  (Conference created at transfer time)

TAKEOVER:
  Customer ←──direct call──→ Human Agent
  (Tries conference, falls back to direct connect)
  Result: NOT a conference!
```

### Proposed (Uniform)
```
INBOUND → AI:
  Customer ──→ call-conf-{call_id} ←── AI Agent
  (Customer AND AI both in conference from start)

TRANSFER TO HUMAN:
  Customer stays in call-conf-{call_id}
  Agent joins call-conf-{call_id}
  AI leaves (or stays as observer)

TAKEOVER:
  Agent joins call-conf-{call_id}
  AI leaves
  Result: SAME conference structure as transfer!
```

---

## Call Flow Details

### 1. Inbound Call (Customer Arrives)

**Webhook: `/api/swml/initial-call`**

```
┌─────────────────────────────────────────────────────────────┐
│  1. Customer calls inbound number                           │
│  2. SignalWire hits /api/swml/initial-call                  │
│  3. Response: Put customer in conf-{call_id}                │
│  4. Conference status callback fires (customer joined)      │
│  5. Backend dials AI agent INTO the conference              │
│  6. AI agent joins, starts conversation                     │
└─────────────────────────────────────────────────────────────┘
```

**SWML Response:**
```yaml
version: 1.0.0
sections:
  main:
    - answer: {}
    - play:
        url: "say:Please hold while we connect you."
    - conference:
        name: "conf-{call_id}"
        start_on_enter: true
        end_on_exit: true  # Conference ends when customer leaves
        status_callback: "{base_url}/api/conferences/conf-{call_id}/status"
        status_callback_event: "start end join leave"
        wait_url: "{base_url}/api/swml/hold-music"  # Optional hold music
```

**Backend Logic (on conference start):**
```python
# When conference starts with customer, dial AI in
def on_conference_started(conference_name, call_id):
    # Use SignalWire API to dial AI agent into conference
    signalwire_client.calls.create(
        from_=SIGNALWIRE_NUMBER,
        to=AI_AGENT_SWML_URL,  # or resource address
        conference=conference_name,
        # Pass context so AI knows about the call
        status_callback=f"{BASE_URL}/api/calls/{call_id}/ai-status"
    )
```

---

### 2. AI Agent Joins Conference

**AI Agent SWML (main_agent.py changes):**

The AI agent needs to work differently - instead of being the primary call handler, it joins an existing conference.

**Option A: AI as SWML endpoint that joins conference**
```yaml
version: 1.0.0
sections:
  main:
    - answer: {}
    - ai:
        # Normal AI configuration
        prompt: "..."
        post_prompt_url: "{base_url}/api/swml/post-prompt"
        SWAIG:
          functions:
            - transfer_to_human
            - ...
```

The AI still uses the `ai` verb, but it's participating IN a conference. The conference mixing handles the audio.

**Option B: AI joins via RELAY room (more complex)**
```python
# AI uses join_conference() in its response
result.join_conference("conf-{call_id}")
```

**Recommendation:** Option A - keep the AI using the `ai` verb, but dial it INTO the conference. SignalWire handles the audio mixing.

---

### 3. Transfer to Human

**When AI calls `transfer_to_human()`:**

```
┌─────────────────────────────────────────────────────────────┐
│  1. AI calls transfer_to_human(queue="support")             │
│  2. SWAIG function returns SWML to leave conference         │
│  3. Backend notified of transfer request                    │
│  4. Backend finds available agent                           │
│  5. Option A: Move customer to agent's conference           │
│     Option B: Add agent to customer's conference            │
│  6. AI leg hangs up (leaves conference)                     │
│  7. Customer and agent now in same conference               │
└─────────────────────────────────────────────────────────────┘
```

**Two sub-options for where the call happens:**

**Option A: Customer moves to Agent's conference**
- Agent has persistent `agent-conf-{user_id}`
- Customer transferred from `conf-{call_id}` to `agent-conf-{user_id}`
- Matches "hot seat" model
- Agent stays in their room, customers come to them

**Option B: Agent joins Customer's conference**
- Customer stays in `conf-{call_id}`
- Agent dials into `conf-{call_id}`
- Simpler, no movement needed
- Conference is per-call, not per-agent

**Recommendation:** Option B is simpler for MVP. Option A is better for "hot seat" but more complex.

---

### 4. Takeover (Agent Takes AI Call)

**This is where the architecture really shines:**

```
┌─────────────────────────────────────────────────────────────┐
│  BEFORE:                                                    │
│  conf-{call_id}                                             │
│    ├── Customer (in conference)                             │
│    └── AI Agent (in conference, handling call)              │
│                                                             │
│  TAKEOVER ACTION:                                           │
│  1. Agent clicks "Take Over" in dashboard                   │
│  2. POST /api/calls/{call_id}/takeover                      │
│  3. Backend: Add agent to conf-{call_id}                    │
│  4. Backend: Signal AI to leave (or just hang up AI leg)    │
│                                                             │
│  AFTER:                                                     │
│  conf-{call_id}                                             │
│    ├── Customer (still in conference)                       │
│    └── Human Agent (joined conference)                      │
│    └── AI Agent (left/disconnected)                         │
└─────────────────────────────────────────────────────────────┘
```

**Backend takeover logic:**
```python
@calls_bp.route('/<call_sid>/takeover', methods=['POST'])
def takeover_call(call_sid):
    agent_id = request.json.get('agent_id')

    # Get the conference name for this call
    call = Call.query.filter_by(signalwire_call_id=call_sid).first()
    conference_name = f"conf-{call.id}"

    # Add agent to the conference
    # Agent dials in via Call Fabric (frontend handles this)
    # OR backend dials agent in:
    dial_agent_to_conference(agent_id, conference_name)

    # Hang up the AI leg
    ai_leg = CallLeg.query.filter_by(
        call_id=call.id,
        leg_type='ai'
    ).first()
    if ai_leg:
        signalwire_client.calls.hangup(ai_leg.signalwire_sid)

    return {"success": True, "conference": conference_name}
```

---

## Implementation Checklist

### Phase 1: Backend Conference Infrastructure

- [ ] **Modify `/api/swml/initial-call`**
  - Return conference SWML instead of direct AI
  - Include status callbacks

- [ ] **Add conference status webhook**
  - `/api/conferences/<name>/status`
  - On "start" event: dial AI into conference
  - On "join" event: track participants
  - On "leave" event: handle cleanup

- [ ] **Add AI dial-in logic**
  - Function to dial AI agent into a conference
  - Pass call context to AI (caller info, etc.)

- [ ] **Modify takeover endpoint**
  - Join agent to existing conference
  - Hang up AI leg

- [ ] **Modify transfer logic**
  - Either move customer or add agent to conference
  - Hang up AI leg

### Phase 2: AI Agent Changes

- [ ] **Modify main_agent.py**
  - AI may need to be aware it's in a conference
  - Transfer function returns "leave conference" action
  - May need to handle being dialed in vs being primary

- [ ] **Update transfer_to_human function**
  - Return action to leave conference
  - Notify backend of transfer request

### Phase 3: Frontend Changes

- [ ] **Modify takeover flow**
  - Instead of complex SWML connect, just join conference
  - `client.dial({ to: conference_address })`

- [ ] **Update call state tracking**
  - Track conference name for active calls
  - Show conference participants

### Phase 4: Database Changes

- [ ] **Add conference tracking to calls table**
  - `conference_name` column
  - `conference_status` column

- [ ] **Conference participants table** (may already exist)
  - Track who's in each conference

---

## Key Files to Modify

### Backend
```
backend/app/api/swml.py          # Initial call → conference
backend/app/api/conferences.py   # Conference status webhooks
backend/app/api/calls.py         # Takeover endpoint changes
backend/app/services/signalwire_api.py  # Dial AI into conference
```

### AI Agents
```
ai-agents/main_agent.py          # Transfer returns leave action
```

### Frontend
```
frontend/src/contexts/CallFabricContext.tsx  # Join conference for takeover
frontend/src/components/callcenter/ActiveCallsList.tsx  # Takeover button
```

---

## Verified Approaches (Confirmed)

### 1. Conference + AI Verb — SUPPORTED ✅
You can have AI inside a conference. The AI is dialed into the conference via a separate SWML call:

```yaml
sections:
  main:
    - answer: {}
    - conference:
        name: call-conf-123
        start_on_enter: false
        end_on_exit: false
    - ai:
        voice: joanna
        prompt: "Hello, how can I help you?"
        post_prompt_url: ...
```

**Note:** Conference blocks, so the `ai` verb runs AFTER the conference ends (when AI leaves).
To avoid issues, dial AI into conference from a separate SWML URL.

### 2. Best Way to Dial AI Into Conference — Voice API ✅

Use SignalWire Voice API to create a call to a SWML endpoint:

```python
client.calls.create(
    from_="+1YOURNUM",
    to="https://yourapp.com/api/swml/ai-conference-join?conf=call-conf-123",
)
```

Benefits:
- Keeps AI modular
- Can dial into any conference dynamically
- Easy to reuse same AI agent for multiple customers

### 3. Recording — Conference-Level ✅

Use conference-level recording for:
- Unified recording for whole call flow (customer + AI + human)
- One file per interaction
- Easier analytics and summaries

```yaml
- record:
    type: "audio"
    direction: "both"
    name: "call-conf-{id}"
```

### 4. Conference Naming — `call-conf-{call_id}` ✅

Using `call-conf-{call_id}` for per-call conferences.

### 5. Token-Based Access (Future Enhancement)

SignalWire supports:
- JWT tokens that grant scoped permissions to conferences
- Call addresses that can only be joined via specific API/ACL logic
- "Invite-only" or "one-time-use" conference flows

Can implement later for enhanced security.

---

## SignalWire API for Conference Operations

Based on SDK exploration, key methods:

```python
# Dial a new participant into existing conference
client.calls.create(
    from_="+1...",
    to="swml_endpoint_or_number",
    conference="conf-name",  # Join this conference
    ...
)

# List conference participants
participants = client.conferences.list_participants("conf-name")

# Kick participant from conference
client.conferences.kick_participant("conf-name", participant_id)

# Update participant (mute, hold, etc.)
client.conferences.update_participant("conf-name", participant_id, muted=True)
```

---

## Estimated Effort

1. **Backend conference infrastructure**: Medium
2. **AI agent changes**: Low-Medium
3. **Frontend takeover changes**: Low (simpler than current)
4. **Testing & debugging**: Medium

The takeover flow actually becomes SIMPLER because instead of complex SWML/connect logic, you just join a conference.

---

## Concrete Implementation Details

### CHANGE 1: `/api/swml/initial-call` - Put Customer in Conference

**File:** `backend/app/api/swml.py` (lines 172-212)

**BEFORE:**
```python
swml_response = {
    "version": "1.0.0",
    "sections": {
        "main": [
            {"set": {"call_state_url": f"{base_url}/api/webhooks/call-status", ...}},
            "answer",
            {"record_call": {...}},
            {"live_transcribe": {...}},
            {"transfer": {"dest": f"{base_url}/receptionist"}}  # Direct to AI
        ]
    }
}
```

**AFTER:**
```python
# Generate conference name for this call
conference_name = f"call-conf-{call.id}"

swml_response = {
    "version": "1.0.0",
    "sections": {
        "main": [
            {"set": {"call_state_url": f"{base_url}/api/webhooks/call-status", ...}},
            "answer",
            {"record_call": {...}},
            {"live_transcribe": {...}},
            # Put customer in conference instead of direct AI transfer
            {
                "conference": {
                    "name": conference_name,
                    "start_on_enter": True,
                    "end_on_exit": True,  # Conference ends when customer hangs up
                    "beep": False,
                    "wait_url": f"say:Please hold while we connect you.",
                    "status_url": f"{base_url}/api/conferences/{conference_name}/status",
                    "status_events": ["start", "end", "join", "leave"]
                }
            }
        ]
    }
}

# Also store conference name in call record
call.conference_name = conference_name
db.session.commit()

# Backend will dial AI in when conference starts (via status callback)
```

### CHANGE 2: Conference Status Webhook - Dial AI Into Conference

**File:** `backend/app/api/conferences.py` (new or extend existing)

```python
@conferences_bp.route('/<conference_name>/status', methods=['POST'])
def conference_status(conference_name):
    """Handle conference status callbacks from SignalWire."""
    data = request.get_json() or request.form.to_dict()
    event = data.get('StatusCallbackEvent') or data.get('event')

    logger.info(f"Conference {conference_name} event: {event}")

    if event == 'start':
        # Conference started, customer is waiting
        # Dial AI agent into the conference
        dial_ai_into_conference(conference_name)

    elif event == 'join':
        participant = data.get('CallSid') or data.get('call_id')
        logger.info(f"Participant {participant} joined {conference_name}")
        # Track participant in database

    elif event == 'leave':
        participant = data.get('CallSid') or data.get('call_id')
        logger.info(f"Participant {participant} left {conference_name}")
        # If AI left and no human, customer is alone - handle accordingly

    elif event == 'end':
        logger.info(f"Conference {conference_name} ended")
        # Clean up

    return '', 200


def dial_ai_into_conference(conference_name):
    """Dial AI agent into an existing conference."""
    # Extract call_id from conference name
    call_id = conference_name.replace('call-conf-', '')
    call = Call.query.get(call_id)

    if not call:
        logger.error(f"Call not found for conference {conference_name}")
        return

    # Use SignalWire API to create a new call leg that joins the conference
    base_url = get_base_url()

    # Option A: Create a call to an SWML endpoint that joins conference
    swml_url = f"{base_url}/api/swml/ai-conference-join?conf={conference_name}&call_id={call_id}"

    sw_api = SignalWireAPI()
    sw_api.create_call(
        to=swml_url,
        from_=SIGNALWIRE_NUMBER,
        # Or use: conference=conference_name directly if API supports it
    )

    logger.info(f"Dialed AI into conference {conference_name}")
```

### CHANGE 3: New SWML Endpoint - AI Joins Conference

**File:** `backend/app/api/swml.py` (add new endpoint)

The AI is dialed into the conference as a separate call. The SWML joins the conference,
then runs the AI. When the AI/conference interaction ends, the AI leg hangs up.

```python
@swml_bp.route('/ai-conference-join', methods=['POST'])
def ai_conference_join():
    """SWML endpoint for AI to join a conference.

    This gets dialed by the backend when a conference starts.
    The SWML joins the conference and runs the AI inside it.
    When the AI ends (transfer/hangup), this leg leaves the conference.
    """
    conference_name = request.args.get('conf')
    call_id = request.args.get('call_id')

    base_url = get_base_url()

    # VERIFIED APPROACH: Conference + AI verb in sequence
    # The AI runs INSIDE the conference context
    swml_response = {
        "version": "1.0.0",
        "sections": {
            "main": [
                "answer",
                {
                    "conference": {
                        "name": conference_name,
                        "start_on_enter": False,  # Customer already started it
                        "end_on_exit": False,     # Don't end when AI leaves
                        "beep": False
                    }
                },
                # AI runs inside conference context
                {
                    "ai": {
                        "voice": "en-US-Neural2-F",
                        "prompt": {
                            "text": "You are a helpful receptionist...",
                            # Full prompt here, or use post_prompt_url
                        },
                        "post_prompt_url": f"{base_url}/api/swml/post-prompt",
                        "SWAIG": {
                            "functions": [
                                {
                                    "function": "transfer_to_human",
                                    "web_hook_url": f"{base_url}/api/queues/transfer"
                                }
                            ]
                        }
                    }
                }
            ]
        }
    }

    return jsonify(swml_response)
```

**OR: Redirect to existing AI agent with conference context**

```python
@swml_bp.route('/ai-conference-join', methods=['POST'])
def ai_conference_join():
    """Redirect to AI agent, passing conference name."""
    conference_name = request.args.get('conf')
    call_id = request.args.get('call_id')
    base_url = get_base_url()

    # Just transfer to the AI agent, passing conference as param
    # The AI agent will handle joining the conference
    swml_response = {
        "version": "1.0.0",
        "sections": {
            "main": [
                "answer",
                {
                    "transfer": {
                        "dest": f"{base_url}/receptionist?conf={conference_name}&call_id={call_id}"
                    }
                }
            ]
        }
    }

    return jsonify(swml_response)
```

### CHANGE 4: AI Agent - Handle Conference Context

**File:** `ai-agents/main_agent.py`

**VERIFIED APPROACH:** The AI agent checks for `conf` query param and wraps its
SWML in a conference join:

```python
class Receptionist(AgentBase):
    def get_swml(self):
        """Override to add conference join if conf param present."""
        # Get conference name from request
        from flask import request
        conference_name = request.args.get('conf')

        # Build base AI SWML
        base_swml = super().get_swml()

        if conference_name:
            # Wrap in conference join
            return {
                "version": "1.0.0",
                "sections": {
                    "main": [
                        {
                            "conference": {
                                "name": conference_name,
                                "start_on_enter": False,
                                "end_on_exit": False,
                                "beep": False
                            }
                        },
                        # Then the AI section from base_swml
                        base_swml["sections"]["main"][0]  # The "ai" block
                    ]
                }
            }

        return base_swml
```

**Alternative: Backend handles conference, AI is simple**
If we use the first approach in CHANGE 3 (AI SWML inline), the AI agent
doesn't need to change at all - conference join is handled in the
`/ai-conference-join` endpoint.

### CHANGE 5: Takeover - Join Existing Conference

**File:** `backend/app/api/swml.py` (modify takeover_swml)

**BEFORE:** Complex logic trying to move participants between conferences.

**AFTER:** Much simpler - just join the call's conference:
```python
@swml_bp.route('/takeover/<token>', methods=['POST'])
def takeover_swml(token):
    # ... validate token ...

    # Get the call's conference name
    call = Call.query.filter_by(id=call_id).first()
    conference_name = call.conference_name  # e.g., "call-conf-123"

    # Hang up the AI leg (it will leave the conference)
    ai_leg = CallLeg.get_active_leg(call.id, leg_type='ai_agent')
    if ai_leg:
        sw_api.hangup_call(ai_leg.signalwire_sid)

    # Agent joins the SAME conference the customer is in
    swml_response = {
        "version": "1.0.0",
        "sections": {
            "main": [
                "answer",
                {
                    "conference": {
                        "name": conference_name,
                        "start_on_enter": False,  # Customer already there
                        "end_on_exit": True,      # End when agent leaves
                        "beep": False
                    }
                }
            ]
        }
    }

    return jsonify(swml_response)
```

### CHANGE 6: Transfer to Human - Same Pattern

**File:** `backend/app/api/queues.py` or AI agent's transfer function

When AI transfers to human queue:
1. AI notifies backend: "transfer to support queue"
2. Backend finds available agent
3. Agent joins the existing `call-conf-{call_id}`
4. AI leaves (or backend hangs up AI leg)

The customer NEVER moves conferences. The conference is per-call, not per-agent.

```python
# In queue assignment logic
def assign_call_to_agent(call_id, agent_id):
    call = Call.query.get(call_id)
    conference_name = call.conference_name  # "call-conf-{call_id}"

    # Hang up AI leg
    ai_leg = CallLeg.get_active_leg(call.id, leg_type='ai_agent')
    if ai_leg:
        sw_api.hangup_call(ai_leg.signalwire_sid)

    # Notify agent to dial into this conference
    socketio.emit('call_assigned', {
        'call_id': call.id,
        'conference_name': conference_name,
        # Agent's frontend will dial into this conference
    }, room=f'agent_{agent_id}')
```

### CHANGE 7: Frontend - Dial Conference Address

**File:** `frontend/src/contexts/CallFabricContext.tsx`

When agent accepts a call (takeover or from queue):
```typescript
const acceptCall = async (callId: number, conferenceName: string) => {
    // Dial the conference directly via Call Fabric
    // SignalWire SWML address for conference join
    const swmlUrl = `${API_BASE_URL}/api/swml/agent-conference-join?conf=${conferenceName}`;

    // Or if SignalWire supports dialing conference addresses directly:
    // const destination = `/conf/${conferenceName}`;

    const call = await client.dial({
        to: swmlUrl,
        // or: to: destination
    });

    await call.start();
};
```

---

## Database Changes Needed

### Add to `calls` table:
```python
# In backend/app/models/call.py
conference_name = db.Column(db.String(255), nullable=True)
```

### Migration:
```python
# backend/migrations/versions/xxx_add_conference_name_to_calls.py
def upgrade():
    op.add_column('calls', sa.Column('conference_name', sa.String(255), nullable=True))

def downgrade():
    op.drop_column('calls', 'conference_name')
```

---

## Simplified Call Flow Summary

```
1. INBOUND CALL
   └─> SignalWire calls /api/swml/initial-call
   └─> Response: Put customer in "call-conf-{call_id}"
   └─> Conference status callback: "start"
   └─> Backend dials AI into same conference

2. AI HANDLES CALL
   └─> Customer and AI both in "call-conf-{call_id}"
   └─> Normal AI interaction

3a. TRANSFER TO HUMAN (via queue)
    └─> AI calls transfer_to_human()
    └─> Backend hangs up AI leg
    └─> Backend finds available agent
    └─> Agent dials into "call-conf-{call_id}"
    └─> Customer and agent now in same conference

3b. TAKEOVER (agent takes AI call)
    └─> Agent clicks "Take Over"
    └─> Backend hangs up AI leg
    └─> Agent dials into "call-conf-{call_id}"
    └─> Customer and agent now in same conference

4. CALL ENDS
   └─> Customer or agent hangs up
   └─> Conference ends
   └─> Cleanup
```

---

## Key Insight: Per-Call vs Per-Agent Conferences

This design uses **per-call conferences** (`call-conf-{call_id}`):
- Customer stays in same conference for entire call
- AI and humans come and go
- Simpler: no moving participants between conferences

Alternative (not recommended for MVP): **per-agent conferences**:
- Agent has `agent-conf-{user_id}`
- Customer moves to agent's conference
- More complex, but supports "hot seat" where agent stays connected

**Recommendation:** Start with per-call conferences. If "hot seat" is needed later, can refactor.

---

## Next Steps

1. Review this sketch and confirm approach
2. Decide on open questions above
3. Start with Phase 1: Backend conference infrastructure
4. Test with simple flow: customer → conference → AI joins
5. Add transfer and takeover
