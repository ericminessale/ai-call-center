# SignalWire AI Call Center

A production-ready demonstration of SignalWire's **Programmable Unified Communications (PUC)** platform — a hybrid call center where AI agents and human agents work together, built entirely on Call Fabric.

## Features

- **AI-First Call Handling** — Natural language triage replaces IVR menus. Callers speak naturally; AI routes them to the right department.
- **Configurable Queue System** — Four routing strategies (FIFO, Round-Robin, Priority, Skill-Based) with per-queue AI fallback agents, SLA thresholds, and agent skill levels.
- **Browser-Based Agent Phone** — WebRTC softphone via Call Fabric SDK. Agents go online, receive calls, and manage interactions from the browser.
- **AI-to-Human Handoff** — AI agents gather context (name, department, urgency) then transfer to the human queue. Agents see full AI context when taking calls.
- **AI Takeover** — Human agents can send active calls back to an AI specialist at any time.
- **Outbound Calling** — Click-to-call from contact profiles, with dedicated AI agents for outbound sales and support follow-ups.
- **Knowledge Base (RAG)** — Upload documents to per-agent knowledge bases backed by pgvector. AI agents query them at runtime for accurate answers.
- **Real-Time Dashboard** — Live call status, queue depth charts, call distribution analytics, and supervisor views via WebSocket.
- **Contact Management** — Automatic contact creation from inbound calls, interaction history, custom fields, and inline editing.
- **Live Transcription** — Real-time transcription display with AI-generated summaries.

## How It Works

```
Caller dials in
    │
    ▼
SignalWire Phone Number
    │  webhook: /api/swml/initial-call
    ▼
Backend creates Call record, returns SWML
    │  transfer → /receptionist
    ▼
AI Triage Agent
    │  "Hi, what's your name?"
    │  "Which department: Sales, Support, or Billing?"
    │
    ├─► "I want a human" ──► transfer_to_human()
    │       │                    │  SWML transfer → /api/queues/sales/route
    │       │                    ▼
    │       │              Queue Router
    │       │                    │
    │       │     ┌──────────────┼──────────────┐
    │       │     ▼              ▼              ▼
    │       │  Agent         No agents       > 2 min wait
    │       │  available     (hold loop)     (AI fallback)
    │       │     │
    │       │     ▼
    │       │  Socket notification → Agent accepts → Conference
    │       │
    └─► "AI can help" ──► transfer_to_ai_specialist()
            │  SWML transfer → /sales-ai or /support-ai
            ▼
        AI Specialist (with RAG knowledge base)
            │
            └─► Can escalate to human queue if needed
```

### AI Agent Routes

The AI agents service (port 8080) hosts these routes:

| Route | Agent | Purpose |
|-------|-------|---------|
| `/receptionist` | CallCenterTriageAgent | Main triage — collects name, routes to department |
| `/sales-ai` | SalesAISpecialist | AI sales specialist with product knowledge (RAG) |
| `/support-ai` | SupportAISpecialist | AI support specialist with troubleshooting docs (RAG) |
| `/outbound-sales` | OutboundSalesAgent | Proactive outbound sales calls |
| `/outbound-support` | OutboundSupportAgent | Outbound support follow-ups |

## Prerequisites

- **Docker Desktop** — [Install Docker](https://docs.docker.com/get-docker/)
- **SignalWire Account** — [Sign up free](https://signalwire.com)
- **ngrok** — For local development webhooks — [Install ngrok](https://ngrok.com/download)
- **Phone Number** — At least one SignalWire phone number

## Quick Start

### 1. Clone and Configure

```bash
git clone <repository-url>
cd signalwire-call-center

# Copy environment template
cp .env.example .env
```

### 2. Edit `.env` with Your Credentials

Get these from your [SignalWire Dashboard](https://signalwire.com/signin):

```bash
# Required — from SignalWire Dashboard
SIGNALWIRE_SPACE=yourspace.signalwire.com
SIGNALWIRE_PROJECT_ID=your-project-id
SIGNALWIRE_API_TOKEN=PTxxxxxxxxxxxxxxxxxxxxxxxx
SIGNALWIRE_PHONE_NUMBER=+1234567890

# Required — generate these
SUBSCRIBER_PASSWORD_KEY=<generate-fernet-key>
JWT_SECRET_KEY=<generate-random-string>

# Required — frontend must match your SignalWire space
VITE_SIGNALWIRE_HOST=yourspace.signalwire.com
```

**Generate the Fernet key:**
```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

### 3. Start ngrok

SignalWire needs to reach your local services. In a separate terminal:

```bash
ngrok http 80
```

Copy the HTTPS URL (e.g., `https://abc123.ngrok-free.app`) and set it in `.env`:

```bash
# Both should be your ngrok URL
EXTERNAL_URL=https://abc123.ngrok-free.app
AGENT_BASE_URL=https://abc123.ngrok-free.app
```

> **Why both?** `EXTERNAL_URL` is used by the backend to construct webhook callback URLs. `AGENT_BASE_URL` is used by AI agents to build transfer URLs between agents. Since nginx routes everything, they should be the same ngrok URL.

### 4. Start Services

```bash
docker-compose up -d

# Verify all services are healthy
docker-compose ps

# Watch logs (optional)
docker-compose logs -f
```

### 5. Create the SWML Resource (Required for Agent Phone)

The agent's browser phone needs a SignalWire resource that returns SWML for joining conferences. This is what connects the agent to customer calls.

1. Go to your [SignalWire Dashboard](https://signalwire.com/signin) > **Resources**
2. Click **Add New** > **Script** > **SWML Script**
3. Set the **Request URL** to:
   ```
   https://YOUR-NGROK-URL/api/conferences/agent-conference
   ```
4. Save and copy the assigned address (e.g., `/public/agent-conference-swml`)
5. Add to your `.env`:
   ```bash
   AGENT_CONFERENCE_RESOURCE=/public/agent-conference-swml
   ```
6. Restart the backend to pick up the change:
   ```bash
   docker-compose restart backend
   ```

> **What this does:** When an agent dials into a conference via Call Fabric, SignalWire hits this resource URL. The backend returns SWML that joins the agent to the correct interaction conference.

### 6. Configure SignalWire Phone Number

In your [SignalWire Dashboard](https://signalwire.com/signin):

1. Go to **Phone Numbers** > Select your number
2. Set **Handle Calls Using** to **a SWML Script**
3. Set **When a Call Comes In** to:
   ```
   https://YOUR-NGROK-URL/api/swml/initial-call
   ```
4. Set **Status Callback URL** to:
   ```
   https://YOUR-NGROK-URL/api/webhooks/call-status
   ```

### Direct-to-Queue Routing (Optional)

If you want a phone number to skip AI entirely and route callers straight to a human agent queue, set the number's webhook to:

```
https://YOUR-NGROK-URL/api/queues/{queue-slug}/direct-inbound
```

Replace `{queue-slug}` with any active queue slug (e.g., `sales`, `support`, `billing`). Callers hear a brief hold message and enter the queue immediately. This uses the same agent assignment, conference, and hold loop logic as AI-routed calls — just without the triage step.

### 7. Create a Call Fabric Subscriber

Agents need a Call Fabric subscriber to make/receive calls via the browser:

```bash
curl -X POST "https://YOUR-SPACE.signalwire.com/api/fabric/subscribers" \
  -u "YOUR-PROJECT-ID:YOUR-API-TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "first_name": "Agent",
    "last_name": "One",
    "display_name": "Agent One",
    "job_title": "Support Agent",
    "email": "agent@example.com"
  }'
```

Save the returned `subscriber_id` — you'll enter this when configuring your account in the app.

### 8. Access the Application

| Service | URL | Purpose |
|---------|-----|---------|
| Agent Dashboard | http://localhost:3000 | Main UI — register, login, manage calls |
| Backend API | http://localhost:5000 | REST API + WebSocket |
| AI Agents | http://localhost:8080 | AI agent endpoints |
| Health Check | http://localhost:8080/health | AI agents health status |

### 9. Register, Login, and Go Online

1. Open **http://localhost:3000**
2. Click **Register** and create an account
3. Login with your credentials
4. Go to **Settings** (gear icon in right panel):
   - Enter your **Call Fabric subscriber ID** and **password**
   - Assign yourself to queues (Sales, Support, Billing)
5. Click the status toggle in the header to go **Available**
6. Call your SignalWire number from any phone — the AI will triage the call and route it to you

## Architecture

### Request Flow

```
                     Internet
                        │
                        ▼
┌─────────────────── ngrok ───────────────────┐
│                       │                      │
│    ┌──────────── nginx (port 80) ──────────┐ │
│    │                  │                     │ │
│    │   /api/*         │    /receptionist    │ │
│    │   /socket.io     │    /sales-ai        │ │
│    │       │          │    /support-ai       │ │
│    │       ▼          │    /outbound-*       │ │
│    │   backend        │        │            │ │
│    │   (Flask)        │        ▼            │ │
│    │   port 5000      │    ai-agents        │ │
│    │       │          │    (Python)          │ │
│    │       │          │    port 8080         │ │
│    │       ▼          │        │            │ │
│    │   ┌───────┐      │        │            │ │
│    │   │ Redis │◄─────┼────────┘            │ │
│    │   └───────┘      │                     │ │
│    │   ┌──────────┐   │                     │ │
│    │   │ Postgres │◄──┼────────┘            │ │
│    │   │ (pgvector)│  │                     │ │
│    │   └──────────┘   │                     │ │
│    └──────────────────┘                     │ │
│                                              │
│    frontend (React + Vite) ─── port 3000     │
└──────────────────────────────────────────────┘
```

### Key Technologies

| Component | Technology | Purpose |
|-----------|-----------|---------|
| Backend | Flask + Gunicorn | REST API, SWML generation, call orchestration |
| Frontend | React + TypeScript + Vite | Agent desktop, contact management, admin |
| AI Agents | SignalWire Agents SDK (Python) | Conversational AI with SWAIG functions |
| Database | PostgreSQL + pgvector | Data storage + vector search for RAG |
| Cache | Redis | Agent status, queue state, pub/sub |
| Proxy | nginx | Unified routing for all services |
| Real-time | Socket.IO | Live call updates, queue events, transcription |
| Browser Phone | SignalWire Call Fabric SDK | WebRTC calling from the browser |

## Project Structure

```
signalwire-call-center/
├── ai-agents/                 # Python AI agents
│   ├── main_agent.py          # All agents: triage, specialists, outbound
│   ├── requirements.txt
│   └── Dockerfile
├── backend/                   # Flask backend
│   ├── app/
│   │   ├── api/
│   │   │   ├── swml.py        # Initial call SWML generation
│   │   │   ├── conferences.py # Agent conference SWML resource
│   │   │   ├── queues.py      # Queue routing + agent assignment
│   │   │   ├── calls.py       # Call management + outbound
│   │   │   ├── admin.py       # Admin API (queues, KB, phone config)
│   │   │   ├── ai_control.py  # AI takeover / handback
│   │   │   └── webhooks.py    # SignalWire status callbacks
│   │   ├── models/            # SQLAlchemy models
│   │   │   ├── call.py        # Call, CallLeg, Conference
│   │   │   ├── user.py        # User (with subscriber fields)
│   │   │   ├── contact.py     # Contact + custom fields
│   │   │   └── queue.py       # Queue + QueueAgentAssignment
│   │   ├── services/
│   │   │   ├── queue_service.py       # Redis queue + routing strategies
│   │   │   ├── callcenter_socketio.py # Real-time events
│   │   │   └── redis_service.py       # Redis client helpers
│   │   └── utils/
│   │       ├── url_utils.py   # EXTERNAL_URL resolution
│   │       └── jwt_utils.py   # JWT auth
│   ├── migrations/            # Alembic database migrations
│   └── Dockerfile
├── frontend/                  # React + TypeScript
│   ├── src/
│   │   ├── pages/
│   │   │   ├── UnifiedAgentDesktop.tsx  # Main agent workspace
│   │   │   ├── Admin.tsx               # Admin settings
│   │   │   ├── Login.tsx / Register.tsx
│   │   ├── components/
│   │   │   ├── unified/       # Dashboard panels, queue list, charts
│   │   │   ├── contacts/      # Contact detail, interaction history
│   │   │   └── shared/        # Reusable components
│   │   ├── contexts/
│   │   │   ├── CallFabricContext.tsx  # Call Fabric SDK state
│   │   │   └── SocketContext.tsx      # WebSocket connection
│   │   └── stores/            # Zustand state management
│   └── Dockerfile
├── nginx/
│   └── nginx.conf             # Reverse proxy routing
├── scripts/
│   └── init.sql               # Database schema initialization
├── docker-compose.yml
├── .env.example
└── README.md
```

## Docker Services

| Container | Port | Purpose |
|-----------|------|---------|
| `callcenter-nginx` | 80 | Reverse proxy — all traffic enters here |
| `callcenter-frontend` | 3000 | React agent dashboard (Vite dev server) |
| `callcenter-backend` | 5000 | Flask API + Socket.IO |
| `callcenter-agents` | 8080 | Python AI agents |
| `callcenter-postgres` | 5432 | PostgreSQL with pgvector |
| `callcenter-redis` | 6379 | Agent status, queues, pub/sub |

## Common Commands

```bash
# Start / stop
docker-compose up -d
docker-compose down

# View logs
docker-compose logs -f backend
docker-compose logs -f ai-agents

# Restart a single service (picks up code changes via volume mounts)
docker-compose restart backend

# Full rebuild (needed after dependency changes)
docker-compose up -d --build

# Reset everything (database, Redis, all state)
docker-compose down -v
docker-compose up -d

# Database shell
docker-compose exec postgres psql -U ccuser -d callcenter

# Redis shell
docker-compose exec redis redis-cli

# Backend shell
docker-compose exec backend bash
```

## Environment Variables

### Required

| Variable | Description |
|----------|-------------|
| `SIGNALWIRE_SPACE` | Your SignalWire space (e.g., `yourspace.signalwire.com`) |
| `SIGNALWIRE_PROJECT_ID` | Project ID from SignalWire Dashboard |
| `SIGNALWIRE_API_TOKEN` | API token (starts with `PT`) |
| `SIGNALWIRE_PHONE_NUMBER` | Your SignalWire phone number (E.164) |
| `SUBSCRIBER_PASSWORD_KEY` | Fernet key for encrypting subscriber passwords |
| `JWT_SECRET_KEY` | Secret for JWT authentication tokens |
| `VITE_SIGNALWIRE_HOST` | SignalWire space hostname (for frontend SDK) |
| `EXTERNAL_URL` | Your ngrok URL — used for webhook callbacks |
| `AGENT_BASE_URL` | Your ngrok URL — used for AI agent transfers |
| `AGENT_CONFERENCE_RESOURCE` | SWML resource address from SignalWire Dashboard (e.g., `/public/agent-conference-swml`) |

### Optional

| Variable | Default | Description |
|----------|---------|-------------|
| `SWML_BASIC_AUTH_USER` | `agent` | HTTP Basic Auth user for AI agent webhooks |
| `SWML_BASIC_AUTH_PASSWORD` | `agent123` | HTTP Basic Auth password |
| `DEMO_MODE` | `true` | Enables demo features |
| `ENABLE_CALL_RECORDING` | `true` | Record calls as MP3 |
| `ENABLE_TRANSCRIPTION` | `true` | Live transcription |
| `ENABLE_AI_SUMMARY` | `true` | AI-generated call summaries |

## Troubleshooting

### Calls not reaching the app
- Verify ngrok is running and your `EXTERNAL_URL` matches the current ngrok URL
- Check that your SignalWire phone number's webhook URL points to `https://YOUR-NGROK-URL/api/swml/initial-call`
- Check backend logs: `docker-compose logs -f backend`

### Agent phone not working (no calls / "ending" immediately)
- Verify the **SWML resource** is set up in SignalWire Dashboard (see Step 5 above)
- Ensure the resource's Request URL points to your **current** ngrok URL: `https://YOUR-NGROK-URL/api/conferences/agent-conference`
- Check `AGENT_CONFERENCE_RESOURCE` in `.env` matches the resource address in the dashboard
- Check `VITE_SIGNALWIRE_HOST` in `.env` matches your SignalWire space
- Check browser console for WebRTC errors — the SDK needs ~10 seconds to initialize

### AI agent says "no agents available"
- Make sure you're in **Available** status (green toggle in the header)
- After outbound calls or takeovers, your status auto-transitions to **After-Call** — click back to **Available**
- Check Redis state: `docker-compose exec redis redis-cli SMEMBERS agents:available`
- If stale data, flush it: `docker-compose exec redis redis-cli FLUSHDB` and restart backend

### AI agents not responding
- Check agent health: `curl http://localhost:8080/health`
- Check logs: `docker-compose logs ai-agents`
- Verify `AGENT_BASE_URL` in `.env` is your current ngrok URL

### ngrok URL changed
When ngrok restarts, you get a new URL. Update **all three places**:
1. `.env` — update `EXTERNAL_URL` and `AGENT_BASE_URL`
2. SignalWire Dashboard — update the phone number webhook URL
3. SignalWire Dashboard — update the SWML resource Request URL
4. Restart backend: `docker-compose restart backend`

### Database errors
```bash
# Reset database completely
docker-compose down -v
docker-compose up -d
```

## Development Without Docker

**Backend:**
```bash
cd backend
pip install -r requirements.txt
export DATABASE_URL=postgresql://ccuser:changeme@localhost:5432/callcenter
export REDIS_URL=redis://localhost:6379/0
flask db upgrade
gunicorn --workers 4 --threads 100 --bind 0.0.0.0:5000 wsgi:app
```

**Frontend:**
```bash
cd frontend
npm install
npm run dev
```

**AI Agents:**
```bash
cd ai-agents
pip install -r requirements.txt
python main_agent.py
```

## Resources

- [SignalWire Documentation](https://developer.signalwire.com)
- [SignalWire Agents SDK](https://github.com/signalwire/signalwire-agents)
- [SWML Reference](https://developer.signalwire.com/sdks/reference/swml/overview)
- [Call Fabric SDK](https://developer.signalwire.com/sdks/reference/browser-sdk/00-getting-started)

## License

MIT License — See LICENSE file for details.
