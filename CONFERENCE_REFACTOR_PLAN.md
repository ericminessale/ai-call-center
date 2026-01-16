# Conference Refactor Plan: Per-Interaction Conferences

## Problem with Current Implementation

**Current flow (WRONG):**
```
Agent goes "available"
  → Dials CXML resource → Joins agent-conf-{user_id}
  → Sits idle in conference waiting for customers
Customer routed
  → Joined INTO agent's existing conference
```

**Issues:**
- Agents consume resources sitting idle in conferences
- Conference exists even when no calls
- Not how boss intended it based on cf-cc-mini example

## Proposed Implementation

**New flow (CORRECT):**
```
Agent goes "available"
  → Goes online via Call Fabric (client.online())
  → NO conference joined - just ready to receive calls

Customer calls → AI handles
  ↓
Customer wants human → Backend finds available agent
  ↓
Backend creates interaction conference: interaction-{call_id}
  ↓
Customer joins interaction conference (via SWML)
  ↓
Backend DIALS agent into same conference (via CXML resource)
  ↓
Both in conference → can manipulate legs independently
  ↓
Call ends → conference cleaned up
```

## Benefits

1. **No idle conferences** - Created only when needed
2. **Flexible leg handling** - Can move customer to AI, agent to another room
3. **Simpler agent state** - Online/offline, not in/out of conference
4. **Matches cf-cc-mini pattern** - Participants dialed INTO conferences

---

## Changes Required

### 1. Frontend: CallFabricContext.tsx

**Remove:**
- `joinAgentConference()` function
- `joinAgentConferenceRef`
- `agentConferenceRef`
- Auto-join conference when going available

**Keep:**
- `goOnline()` / `goOffline()` - agent availability
- `client.online()` with inbound call handlers

**Change `goOnline()`:**
```typescript
// BEFORE: Goes online AND joins conference
const goOnline = async () => {
  await client.online({...handlers});
  await joinAgentConference(); // REMOVE THIS
};

// AFTER: Just goes online
const goOnline = async () => {
  await client.online({...handlers});
  // Agent is now available to be dialed into conferences
};
```

### 2. Backend: queues.py - route_call_to_queue()

**Change from:**
```python
# Current: Use agent's persistent conference
conference_name = f"agent-conf-{selected_user.id}"
conference = Conference.get_or_create_agent_conference(selected_user.id)
```

**Change to:**
```python
# New: Create per-interaction conference
conference_name = f"interaction-{call_id}"
conference = Conference.create_interaction_conference(call_id, queue_id)

# Join customer to conference
customer_swml = {
    "join_conference": {"name": conference_name}
}

# Dial agent into same conference (via API call)
dial_agent_into_conference(selected_user, conference_name, call_context)
```

### 3. Backend: New function to dial agent

```python
def dial_agent_into_conference(user, conference_name, context):
    """
    Dial an agent into a conference using SignalWire API.
    Agent's Call Fabric client receives inbound call.
    """
    # Option 1: Use Call Fabric address
    if user.signalwire_address:
        # Dial the agent's fabric address with conference param
        # The agent's client.online() handler receives this
        pass

    # Option 2: Use CXML resource that joins conference
    # Similar to cf-cc-mini's /agent endpoint
    cxml_url = f"{base_url}/api/conferences/join?conf={conference_name}"
    # Initiate call to agent that executes this CXML
```

### 4. Backend: conferences.py - Update CXML endpoint

**Change from:**
```python
@conferences_bp.route('/agent-conference', methods=['POST'])
def agent_conference():
    # Returns CXML joining agent-conf-{agent_id}
    conference_name = f"agent-conf-{agent_id}"
```

**Change to:**
```python
@conferences_bp.route('/join-conference', methods=['POST'])
def join_conference():
    """
    CXML endpoint that joins caller to specified conference.
    Called when agent is dialed into an interaction.
    """
    conference_name = request.args.get('conf')  # interaction-{call_id}

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Dial>
    <Conference statusCallback="{callback_url}">{conference_name}</Conference>
  </Dial>
</Response>"""
```

### 5. Backend: Conference model changes

**Add:**
```python
@classmethod
def create_interaction_conference(cls, call_id, queue_id=None):
    """Create a conference for a specific customer interaction."""
    conference_name = f'interaction-{call_id}'
    conference = cls(
        conference_name=conference_name,
        conference_type='interaction',
        queue_id=queue_id,
        status='active',
        created_at=datetime.utcnow()
    )
    db.session.add(conference)
    db.session.commit()
    return conference
```

**Keep for cleanup:**
```python
def end_conference(self):
    """Mark conference as ended."""
    self.status = 'ended'
    self.ended_at = datetime.utcnow()
    db.session.commit()
```

### 6. Frontend: Remove conference state from StatusSelector

**Remove:**
- Any UI showing "In Conference" state
- Conference join/leave logic tied to status changes

**Keep:**
- Online/Offline/Break status toggle
- Status updates to backend

---

## Implementation Order

### Phase 1: Backend (Non-Breaking)
1. Add `create_interaction_conference()` to Conference model
2. Add `/join-conference` CXML endpoint (new, doesn't replace old)
3. Add `dial_agent_into_conference()` utility function

### Phase 2: Backend (Update Queue Routing)
4. Update `route_call_to_queue()` to use interaction conferences
5. Update to dial agent instead of expecting them in conference

### Phase 3: Frontend
6. Remove `joinAgentConference()` from CallFabricContext
7. Update `goOnline()` to NOT join conference
8. Remove conference-related UI state

### Phase 4: Cleanup
9. Remove old `/agent-conference` endpoint (once confirmed working)
10. Remove `get_or_create_agent_conference()` from model
11. Update environment variables documentation

---

## Conference Naming Convention

| Conference Type | Name Pattern | When Created | When Destroyed |
|----------------|--------------|--------------|----------------|
| AI Interaction | `ai-{call_id}` | Customer calls AI | Customer transfers out |
| Human Interaction | `interaction-{call_id}` | Customer routed to human | Call ends |
| Hold/Queue | `hold-{queue_id}-{call_id}` | Customer waiting | Agent picks up |

---

## Testing Checklist

- [ ] Agent can go online without joining a conference
- [ ] Customer call creates interaction conference
- [ ] Agent is dialed into conference when assigned
- [ ] Both parties can hear each other
- [ ] Agent can be moved to different conference
- [ ] Customer can be moved to AI conference
- [ ] Conference cleaned up when call ends
- [ ] Multiple concurrent interactions work

---

## Rollback Strategy

Keep feature flag:
```python
USE_INTERACTION_CONFERENCES = os.getenv('USE_INTERACTION_CONFERENCES', 'false').lower() == 'true'
```

If issues, set to `false` to use old per-agent conference model.
