# SignalWire AI Call Center

A full-stack demonstration of SignalWire's Programmable Unified Communications (PUC) platform, showcasing AI-first call handling with seamless handoff to human agents. Built with Call Fabric for browser-based agent phones.

## Features

- **AI Reception** - Natural language call handling instead of IVR menus
- **Smart Routing** - Context-aware routing to sales, support, or priority queues
- **Agent Dashboard** - React-based interface with real-time call visibility
- **Browser Phone** - WebRTC-based agent phone using Call Fabric SDK
- **Live Transcription** - Real-time call transcription display
- **Queue Management** - Priority-based call queuing with wait time tracking

## How It Works

When a call comes in:

1. **Call Arrives** - SignalWire sends the call to the backend (`/api/swml/initial-call`)
2. **Immediate Visibility** - Backend creates a call record and broadcasts it via WebSocket. **The call appears in the Agent Dashboard immediately** with status "AI Active"
3. **AI Handles Call** - Backend returns SWML that routes to the AI receptionist, which determines if the caller needs Sales or Support
4. **Department Routing** - Call transfers to the appropriate department AI (Sales or Support receptionist)
5. **Resolution Options** - The department AI can either:
   - Transfer to an **AI Specialist** for automated resolution
   - Add to the **human queue** for agent assistance
6. **Human Handoff** - If queued, call status changes to "Waiting" and any available human agent can take the call via the browser phone

Agents see all active calls in real-time with live transcription, AI context, and can monitor or intervene at any point.

### AI Agent Routes

The AI agents service (port 8080) hosts these routes:

| Route | Purpose |
|-------|---------|
| `/receptionist` | Main triage - determines Sales vs Support |
| `/sales` | Sales department intake - gathers customer info |
| `/support` | Support department intake - understands the issue |
| `/sales-ai` | AI sales specialist - product knowledge, pricing |
| `/support-ai` | AI support specialist - troubleshooting, documentation |

## Prerequisites

- **Docker Desktop** - [Install Docker](https://docs.docker.com/get-docker/)
- **SignalWire Account** - [Sign up free](https://signalwire.com)
- **ngrok** - For local development webhooks - [Install ngrok](https://ngrok.com/download)
- **Phone Number** - At least one SignalWire phone number

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
# Required - from SignalWire Dashboard
SIGNALWIRE_SPACE=yourspace.signalwire.com
SIGNALWIRE_PROJECT_ID=your-project-id
SIGNALWIRE_API_TOKEN=PTxxxxxxxxxxxxxxxxxxxxxxxx
SIGNALWIRE_PHONE_NUMBER=+1234567890

# Required - generate these
SUBSCRIBER_PASSWORD_KEY=<generate-fernet-key>
JWT_SECRET_KEY=<generate-random-string>

# Frontend must match your SignalWire space
VITE_SIGNALWIRE_HOST=yourspace.signalwire.com
```

**Generate the Fernet key:**
```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

### 3. Start ngrok (Required for Webhooks)

SignalWire needs to reach your local services. In a separate terminal:

```bash
ngrok http 80
```

This exposes the nginx proxy which routes `/api/*` to the backend and `/receptionist` etc. to the AI agents.

Copy the HTTPS URL (e.g., `https://abc123.ngrok.io`)

### 4. Start Services

```bash
# Start all containers
docker-compose up -d

# Watch logs (optional)
docker-compose logs -f

# Check all services are healthy
docker-compose ps
```

### 5. Initialize Database

```bash
# Run migrations
docker-compose exec backend flask db upgrade
```

### 6. Configure SignalWire Phone Number

In your [SignalWire Dashboard](https://signalwire.com/signin):

1. Go to **Phone Numbers** > Select your number
2. Set **Handle Calls Using** to **SWML Script**
3. Set **When a Call Comes In**:
   - **Primary Script URL**: `https://YOUR-NGROK-URL.ngrok.io/api/swml/initial-call`
4. Set **Status Callback URL**: `https://YOUR-NGROK-URL.ngrok.io/api/webhooks/call-status`

The backend dynamically generates the SWML that routes calls to the AI agents.

### 7. Create Call Fabric Subscriber (For Agent Phone)

```bash
# Create a subscriber for the agent to make/receive calls
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

Save the returned `subscriber_id` - you'll use this when registering in the app.

### 8. Access the Application

| Service | URL |
|---------|-----|
| Agent Dashboard | http://localhost:3000 |
| Backend API | http://localhost:5000 |
| AI Agents | http://localhost:8080 |

### 9. Register and Login

1. Open http://localhost:3000
2. Click "Register" and create an account
3. Login with your credentials
4. Go to Settings > Enter your Call Fabric subscriber credentials
5. Click "Go Online" to start receiving calls

## Project Structure

```
signalwire-call-center/
├── ai-agents/              # Python AI agents (SignalWire Agents SDK)
│   ├── main_agent.py       # 5 agents: receptionist, sales, support, specialists
│   ├── requirements.txt
│   └── Dockerfile
├── backend/                # Flask REST API + WebSocket
│   ├── app/
│   │   ├── api/            # REST endpoints (swml.py, webhooks.py, calls.py)
│   │   ├── models/         # SQLAlchemy models (Call, User, Transcription)
│   │   └── services/       # Business logic
│   ├── migrations/         # Alembic database migrations
│   └── Dockerfile
├── frontend/               # React + TypeScript dashboard
│   ├── src/
│   │   ├── components/     # UI components (agent/, supervisor/, callcenter/)
│   │   ├── hooks/          # useCallFabric, useSocket
│   │   ├── pages/          # AgentDashboard, SupervisorDashboard
│   │   └── stores/         # Zustand state management
│   └── Dockerfile
├── nginx/                  # Reverse proxy config
├── docker-compose.yml
├── .env.example
└── README.md
```

## Docker Services

| Container | Port | Purpose |
|-----------|------|---------|
| `callcenter-frontend` | 3000 | React dashboard |
| `callcenter-backend` | 5000 | Flask API + WebSocket |
| `callcenter-agents` | 8080 | Python AI agents |
| `callcenter-postgres` | 5432 | PostgreSQL database |
| `callcenter-redis` | 6379 | Caching and pub/sub |
| `callcenter-nginx` | 80/443 | Reverse proxy |

## Common Commands

```bash
# Start/stop services
docker-compose up -d
docker-compose down

# View logs
docker-compose logs -f backend
docker-compose logs -f ai-agents

# Restart a service
docker-compose restart backend

# Rebuild after code changes
docker-compose up -d --build

# Reset everything (including database)
docker-compose down -v
docker-compose up -d

# Database shell
docker-compose exec postgres psql -U ccuser -d callcenter

# Backend shell
docker-compose exec backend bash
```

## Troubleshooting

### Calls not appearing in dashboard
- Check ngrok is running and URL is correct in SignalWire dashboard
- Check backend logs: `docker-compose logs -f backend`
- Verify `/api/swml/initial-call` is being called (look for "INITIAL-CALL ENDPOINT HIT!")

### AI agents not responding
- Check agents are healthy: `docker-compose ps ai-agents`
- Check agent logs: `docker-compose logs ai-agents`
- Test health endpoint: `curl http://localhost:8080/health`

### Call Fabric phone not working
- Check browser console for WebRTC errors
- Ensure `VITE_SIGNALWIRE_HOST` matches your SignalWire space
- Verify subscriber credentials are correct in Settings
- The SDK needs ~10 seconds to initialize the connection pool

### Database errors
```bash
# Reset database
docker-compose down -v
docker-compose up -d
docker-compose exec backend flask db upgrade
```

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `SIGNALWIRE_SPACE` | Yes | Your SignalWire space (e.g., `myspace.signalwire.com`) |
| `SIGNALWIRE_PROJECT_ID` | Yes | Project ID from dashboard |
| `SIGNALWIRE_API_TOKEN` | Yes | API token (starts with `PT`) |
| `SIGNALWIRE_PHONE_NUMBER` | Yes | Your SignalWire phone number |
| `SUBSCRIBER_PASSWORD_KEY` | Yes | Fernet key for encrypting subscriber passwords |
| `VITE_SIGNALWIRE_HOST` | Yes | Same as SIGNALWIRE_SPACE (for frontend) |
| `JWT_SECRET_KEY` | Yes | Secret for JWT tokens |
| `SWML_BASIC_AUTH_USER` | No | HTTP Basic Auth user for webhooks (default: `agent`) |
| `SWML_BASIC_AUTH_PASSWORD` | No | HTTP Basic Auth password (default: `agent123`) |

## Development Without Docker

**Backend:**
```bash
cd backend
pip install -r requirements.txt
export DATABASE_URL=postgresql://user:pass@localhost:5432/callcenter
flask db upgrade
flask run
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

MIT License - See LICENSE file for details.
