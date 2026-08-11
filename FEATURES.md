# Features

A comprehensive look at what this AI Call Center does today.

This is a reference implementation built on **SignalWire's Programmable Unified Communications** platform. It replaces the traditional CCaaS stack (Twilio Flex + TaskRouter, Five9, Genesys, etc.) with a single runtime where AI agents and human agents share the same infrastructure, the same call state, and the same tools.

---

## Overview

- **AI-first call handling.** Inbound callers reach an AI receptionist that gathers name, preferred language, and intent, then routes to a specialist — AI or human — with full context carried forward.
- **Hybrid routing.** Four queue strategies (FIFO, Round-Robin, Priority-Based, Skill-Based) with per-queue SLA thresholds, automatic callback enrollment when waits exceed the cap, and language-matched agent preference.
- **One runtime for everything.** Calls are stateful objects on SignalWire's control plane — no external state machines, no middleware reassembly, no separate "AI provider" and "telephony provider."
- **Browser-based agent desktop.** WebRTC softphone, real-time transcription, sentiment tracking, live event stream, and in-call controls — all in a dark-themed React UI designed for dense operator use.

---

## Call handling & routing

### Inbound flow

1. A SignalWire phone number receives a call.
2. The number routes to an SWML webhook handler that returns the receptionist AI agent's SWML document.
3. The AI receptionist greets the caller, detects their language from their first utterance, and calls a `set_caller_language` tool silently to persist it.
4. The receptionist determines the caller's intent (sales, support, billing) and offers a choice between an AI specialist or a human agent.
5. Depending on the choice:
   - **AI specialist**: transferred to a department-specific agent (Sales AI / Support AI) with preserved context.
   - **Human**: transferred into a queue and routed to an available agent using either a multi-party conference or SignalWire's native `enter_queue` + two-leg bridge transport, selected per queue.

### Phone number management

- List and route every phone number on the SignalWire account from an admin tab.
- Per-number routing to the call center's entry-point webhook with one click.
- Backend uses the documented `call_handler='relay_script'` + `call_relay_script_url` pair, which materializes a proper **SWML Webhook** Fabric resource — not a legacy cXML Webhook.
- URLs rotate automatically when the ngrok tunnel rotates in dev (see "Infrastructure").

### Queue routing

Four strategies, selectable per queue:

| Strategy | Behavior |
|---|---|
| **FIFO** | Earliest-idle agent wins |
| **Round-Robin** | Cycles through eligible agents |
| **Priority-Based** | Call priority weighs into selection |
| **Skill-Based** | Routes to the agent with the highest configured skill level for that queue |

Every strategy runs **after** a language-preference pass: the router first narrows to agents whose language profile matches the caller's preferred language, then applies the selected strategy. If no language match exists, the router falls back to the full available pool and flags the call for live translation on conference join.

Additional per-queue knobs:

- **SLA threshold** — time the caller can wait before the queue is considered at risk.
- **Max wait cap → callback enrollment** — after this threshold, the still-waiting caller is auto-enrolled in the callback queue, hears an announcement, and the call ends. (The config field is named `max_wait_before_ai_fallback` for historical reasons, but it does NOT hand the caller to an AI agent — redirecting a live waiting leg to new SWML isn't possible; see `queue_dispatch._offer_callback_and_release`.)
- **Default priority** — seeds the priority attribute on new calls into this queue.
- **AI agent route** — which AI agent handles the fallback path.
- **Per-agent skill levels** — 0–10, used by skill-based routing.

### Direct-to-queue skip

Phone numbers can bypass the AI triage entirely and land callers directly in a specific queue's routing flow. Useful for published VIP lines or department-specific direct dials.

### Callback queue

Agent-facing callback management for callers who'd rather not wait:

- Schedule a callback for a contact (agent-initiated or programmatic via API).
- Pending callbacks are a shared pool — agents claim one, release it back, or dial it with one click.
- Outcome tracking per attempt (success, no-answer, etc.) with real-time Socket.IO updates to everyone watching the pool.
- Callback dials run answering-machine detection — voicemail gets a short message, not a stranded AI pitch.

The caller-initiated IVR path ("press 1 for a callback and keep your place in line") is planned but not yet wired.

### Conference-based bridging

Human agents join calls via **Call Fabric conferences**, not direct phone-to-phone bridges. This means:

- The caller and agent are both members of the same conference room.
- Agents can be added or removed without disturbing the caller.
- Multi-agent flows (backup, supervisor join, takeover) all use the conference as the stable anchor.
- Audio operations (mute, deaf, play) work at the member level.

---

## AI agents & knowledge

### Agent roster

Five AI agents ship out-of-box; all are Python-defined using the SignalWire Agents SDK:

| Agent | Route | Role |
|---|---|---|
| **Receptionist** | `/receptionist` | Triage entry point — name, language, intent, routing |
| **Sales AI Specialist** | `/sales-ai` | Product questions, qualifying, demo offers — RAG-enabled |
| **Support AI Specialist** | `/support-ai` | Troubleshooting, account help — RAG-enabled |
| **Outbound Sales Agent** | `/outbound-sales` | Proactive outbound sales follow-up |
| **Outbound Support Agent** | `/outbound-support` | Outbound support check-ins |

### Multilingual support

- AI agents are configured with English, Spanish, and French out-of-box — STT, TTS, and LLM all language-native.
- The receptionist auto-detects the caller's language from their first utterance and responds in kind.
- Caller language preference is stored on the call record and flows with the call through transfers.

### Knowledge base (RAG)

- Per-agent document collections backed by PostgreSQL + **pgvector**.
- **Seeded out-of-box** — the sales and support collections ship with example documents (product specs, comparisons, policies, troubleshooting guides) for the same fictional shop the bundled DemoShop MCP gateway sells, so the specialists can answer real questions on the first call. First boot embeds them automatically; no manual reindex step. Replace them with your own content in Settings → Knowledge Base — the seed runs once and never overwrites or restores what you change.
- Deliberately no prices in the seeded documents: what we sell, what it costs and what's in stock come from the live catalog tool, so the documents can't go stale against it.
- Upload documents via the admin UI; trigger reindex to regenerate embeddings.
- AI agents query their bound collection at runtime via SWAIG function calls.
- Assignments are dynamic — reindex a collection and agents pick up new content on the next conversation, no restart needed.
- The human-agent **Factbook** search (manual, from-transcript, auto, and coach lookups) derives its collection from the call's queue — queue → AI agent route → collection assignment — so a support call searches the support KB, not a hardcoded default.

### Context preservation

- Caller name, intent, language, and any additional data collected by one agent are passed through to the next via SWML `global_data`.
- Context survives AI-to-AI transfers (receptionist → sales-AI), AI-to-human transfers (hands off to agent with full background), and human-to-AI handoffs (agent can re-hand a call back to an AI specialist).
- When a human agent picks up a transferred call, a **pre-join TTS whisper** speaks the collected context into their ear *before* they enter the conference — they walk in knowing who's on the other end and why.
- **Returning-caller memory (2026-08-04).** Inbound calls now resolve the caller's contact server-side and inject a tiered memory block into the AI (`/internal/call-context` → `global_data` + a "Known Caller" prompt section): known callers are greeted by confirmation ("Am I speaking with Fred?") instead of interrogation, and after they confirm, the AI may offer the last call's topic once — recent, non-spam topics only. Caller ID is treated as a hint, never verification; `Contact.notes`/`custom_fields` are deliberately excluded from the payload.
- **AI-only calls learn.** The post-prompt now lifts confirmed name/company/language onto the Contact with the same no-clobber rules as queue transfers (shared `contact_enrichment` service) — an AI-resolved call finally updates durable customer identity. AI prose never touches `Contact.notes` (human-curated).
- **Callback dials open with context.** A dialed callback re-injects the snapshotted context (name, reason, callback flag) so the AI opens by *returning* the call about the stated reason rather than asking who they are.
- **Escalations carry the specialist's work.** `escalate_to_human` now captures a current `work_summary` (steps tried, results, next step) at transfer time; it renders in the AgentContextCard and pre-join whisper, so callers stop repeating themselves to the human.
- **Multi-agent summaries compose.** Each AI session's assessment is kept under `ai_context.session_summaries`; prose summary/notes append labeled sections instead of first-write-wins, and the latest AI disposition wins unless a human saved one. Contact call counts increment once per call (creation-site only) and reconcile from truth at call end.
- **Interaction digest (2026-08-04).** Every contact carries a rolling, token-bounded digest of their last 3 terminal calls (reason, outcome, one-line summary), regenerated at call end. One producer, three consumers: the AI's Known Caller context, the agent desktop's ring-time banner ("Returning · last: vacuum suction · 2d ago"), and — by construction — the future chat kernel.
- **Language memory (2026-08-05).** `Contact.preferred_language` is a durable, human-settable language for a caller (the AI seeds it from a call while empty and never overwrites a human's value). On a later call the agent **opens in that language** — the per-request language list is reordered so the right voice speaks first — then offers English in one short phrase, because a phone number is not a person: if the caller answers in English the AI switches immediately and re-stamps the call's language. Applies only when the documented language is non-English *and* one the agent actually speaks; anything else opens in English exactly as before. Backfilled from existing call history, so callers who already told us once benefit on their next call.
- **Caller-history search (2026-08-04).** AI specialists on calls with an identified returning caller get a `search_caller_history` tool over a per-workspace pgvector index of past-call summaries, hard-filtered server-side to that contact. For "I called months ago about..." moments the digest can't hold; the AI is instructed to search before making the caller re-explain.

### Mid-call AI message injection

Admins viewing an active AI call can send a system message that's injected into the AI's prompt context mid-conversation — useful for steering the AI mid-flow without interrupting the caller.

### Sentiment reporting

AI agents report sentiment events (score, reason, timestamp) during the call. The sentiment is displayed in real time on the agent/supervisor UI and persisted for historical review.

---

## Agent desktop

### Online presence

Agents toggle between Available / Busy / After-Call / Break / Offline. The Call Fabric SDK initializes on Available; queue routing only considers agents in the Available state.

When a call ends, the agent drops into After-Call automatically with a **60-second ACW countdown** shown in the status pill — it auto-returns them to Available at zero. Manually selecting After-Call is deliberate and never expires.

### Queue opt-in

Agents choose which queues they're currently taking calls from via a header dropdown. Opt-in state is per-session; an agent can be a member of multiple queues but only active in a subset at any time.

### Contacts

- Automatic contact creation on first inbound call from a phone number.
- 360-degree view: interaction history, call timeline, custom fields, last-interaction timestamp, VIP status.
- Inline contact editing — name, company, notes, account tier.

### Live call view

When a call is active, the contact detail pane becomes the call workspace. Surfaces include:

- **AI triage summary** — what the receptionist collected before the agent joined.
- **Call controls** (below).
- **Event stream** — color-coded timeline of every call-lifecycle event (transfer, handoff, recording start, etc.) in real time.
- **Transcription feed** — AI-agent utterances streamed live via WebSocket.
- **Sentiment indicator** — current emotional tone, updated as the AI observes shifts.
- **Conference participants** — live list of everyone in the conference with type badges (caller / agent / AI / supervisor).
- **Coach panel** — real-time AI assist for the human agent (see "Real-time agent assist" below).

### Active Calls / Queue / Supervisor tabs

Agents navigate between focused views:

- **Contacts** — default landing; contact list + detail.
- **Active Calls** — all in-flight calls across the center, filterable by AI / human / queue.
- **Queue** — waiting calls in routing order.
- **Supervisor** (admin + supervisor only) — floor overview with queue-depth charts, call distribution donut, and per-call monitoring.
- **Settings** (admin only) — every configuration surface.

---

## In-call controls

### Self actions

- **Mute / Unmute** — WebRTC-level microphone toggle. UI state dispatches to either the SDK's `audioMute` or the outbound shim's `mute` so it works correctly regardless of which call shape is active.

### Participant actions

All of the following operate on calls the agent is *on*:

- **Hold** — conference-aware. Plays a "please hold" TTS announcement to the caller, then mutes and deafens the agent's conference member (audio cut both directions). Caller stays connected to the conference.
- **Record** — on-demand manual recording start/stop. AI calls auto-record via SWML by default; this control is for pausing/resuming the human-side flow. Button state hydrates from the backend so it reflects reality on mount.
- **TTS** — speak a synthesized message into the call. Useful for canned greetings or reading back data hands-free.
- **DTMF keypad** — send touch-tone digits when a caller has been transferred to an external IVR that expects keypad input.
- **Live Translate toggle** — start or stop bidirectional real-time speech translation mid-call. Language picker lets the agent change the source or target pair without tearing down the session (stop + restart internally, since `live_translate` has no `update` action).
- **Request Backup** — queues another agent into the current conference. Uses the queue routing system to find an available agent.
- **Escalate to Supervisor** — adds a supervisor to the conference with optional whisper (coach-only audio).
- **Return to Queue** — bounce the call back to a queue (same or different) with a reason code. Return count is tracked per call for analytics; the SLA clock is *not* reset (the caller's wait is their wait), and a soft cap of 2 returns forces escalation instead of infinite hot-potato.

### Observer actions (for calls you're NOT on)

Observer controls surface in the **Supervisor** tab and in the **Active Calls** list for calls the viewer isn't participating in. They are permission-gated separately from role.

- **Listen** — silent monitoring. For AI calls, uses SignalWire's `tap` to stream audio to a WebSocket-backed AudioMonitor component in the viewer's browser. For human calls, does a silent conference join.
- **Whisper** — coach an agent mid-call with one-way audio. Joins the conference with SignalWire's `coach` member shape: the supervisor hears the whole room but is heard only by the agent's leg. Gated on `can_whisper`. (Agent-initiated whisper also exists via "Escalate to Supervisor".)
- **Barge** — insert yourself into the call with full audio. Silent-entry full-participant conference join; leaving never tears the call down. Gated on `can_barge`.

### Transfer to AI specialist

A human agent on a call can hand the call back to an AI specialist mid-conversation — the AI picks up the context and continues. Useful when a specialist agent needs to consult specific knowledge or run a structured flow before returning to a human.

---

## Live Translate (multilingual call bridging)

When a caller's language doesn't match any available agent's profile, the system auto-starts real-time speech translation on conference join. Five layered pieces share the same SWML `live_translate` primitive:

1. **Per-agent language profile** — agents declare what languages they speak in their user settings.
2. **Caller language capture** — receptionist detects from first utterance, persists to the call record.
3. **Router preference** — queue router prefers agents whose profile matches the caller's language.
4. **Auto-start on handoff** — when the selected agent doesn't match, translation starts automatically as the conference is joined. The caller speaks in their language; the agent hears English (or their primary language). Both directions translated, speech-to-text-to-translate-to-TTS, in real time.
5. **Agent control** — in-call toggle + language picker to start, stop, or swap the active pair on demand.

Ten languages configured by default: English (US), Spanish, French, German, Italian, Portuguese (Brazil), Japanese, Chinese (Mandarin), Korean, Arabic.

---

## Real-time agent assist (Coach)

A per-call AI copilot for the human agent, built on SignalWire's `ai_sidecar` — the coach hears the live call audio and pushes help into the agent's UI without ever speaking on the call.

> **Off by default.** The Coach is gated behind the `COACH_ENABLED` env flag and disabled in hosted demo mode (pre-release platform verb, per-minute billing). The KB Factbook below is independent of that flag and always available.

- **Modes**: off / on-request / auto, selectable per agent. Auto mode volunteers suggestions as the conversation develops; on-request answers only when asked.
- **Coaching suggestions** stream into the Coach panel in real time as the sidecar observes the call.
- **Ask Coach** — the agent types a question mid-call and gets an answer grounded in the live conversation context.
- **KB Factbook** — knowledge-base lookups surfaced alongside the conversation; in auto mode, the caller's latest utterances trigger a debounced KB search so relevant facts appear before the agent asks.
- Permission-gated via `can_use_coach`; coach tone/behavior preset configurable by admins.

Full design notes in `AGENT_ASSIST.md`.

---

## Post-call review & wrap-up

When the call ends, the call detail view becomes the review workspace:

- **Wrap-up panel** — disposition code + free-text agent notes. The AI's post-prompt report auto-fills both as a starting point, marked with a "Captured by AI" badge (`wrap_up_source`); the agent's edits take over from there.
- **Recording playback** — inline audio player plus one-click download, straight from the call record.
- **Sentiment arc** — a per-call timeline of sentiment segments showing how the caller's tone moved across the conversation, alongside the overall score.
- **End-reason classification** — every call gets a deterministic technical ending (abandoned-in-queue, missed, caller-hangup, agent-hangup, premature-disconnect, failed, completed) distinct from the agent's business disposition. Drives the status chips in call history.
- **Full transcript + event timeline** — the complete record of what was said and what happened, in one place.

---

## Supervisor & observer surfaces

### Supervisor tab

- Live list of every active call with visual indicators for AI-vs-human handling, sentiment, VIP status, and "needs attention" flags (sentiment-negative or duration-long).
- Queue depth bar chart and call distribution donut, rendered in real time.
- **SLA wallboard** — per-queue service level vs. threshold, abandon rate, offered/answered counts (24h), and longest current wait, pushed live over Socket.IO.
- **Agent scorecards** — per-agent table over a 24h/7d window: live presence, calls handled, average handle time, total talk time, average sentiment, and returns-to-queue. Supervisor/admin only.
- **Measured interaction history** — each continuous queue stay records entry, offers, declines, acceptance, exit reason, transport, and the original SLA clock across returns. AI, human, hold, and future consultation time have separate handling-segment types, so scorecards no longer infer human talk time from one overloaded call timestamp.
- **Legacy-safe rollout** — timeline-backed calls are measured from queue attempts and handling segments; older calls continue through a non-overlapping compatibility calculation, avoiding both blank dashboards and double-counting.
- Per-call monitoring entry — click to drill in and observe.

### Multi-agent conferencing modes

The conference infrastructure supports three join modes, selectable per-participant:

- **Monitor** — silent observer, no audio in either direction.
- **Backup** — full participant, added as a second handler.
- **Escalation** — supervisor joins with optional whisper (one-way audio to a specific agent).

Each mode is backed by SWML `join_conference` with the appropriate `muted` and `deaf` flags.

---

## Admin & configuration

The Settings tab is the complete configuration surface. Admin-only.

### Phone Numbers

- List every number on the SignalWire account with current routing status.
- One-click assign/unassign to the call center entry-point webhook.
- Automatic `swml_webhook` Fabric resource management — the correct modern routing, not the Twilio-compat cXML path.

### Queues

- Create, edit, and delete queues.
- Configure routing strategy, SLA threshold, AI agent route, default priority, max wait cap (auto-callback enrollment).
- Assign agents to queues with per-queue skill levels (0–10) used by skill-based routing.

### AI Agents

- Each AI agent visible with its route and current knowledge-base binding.
- Change a knowledge-base assignment without touching code — agents query the binding at runtime.

### Knowledge Base

- Document Collections: create, edit, delete.
- Documents within a collection: create, edit, delete — seeded with example content on first boot.
- Reindex ("Publish changes") to regenerate embeddings after content changes. The **Published** badge means a document is embedded and findable by the AI, not merely saved.

### External Tools (MCP Gateway integrations)

Bridge customer-owned MCP (Model Context Protocol) servers into agents. Each row is one configured connection to an MCP Gateway service that fronts one or more MCP servers; the tools the gateway exposes become SWAIG functions on the bound agents.

- Per-gateway: name, description, gateway URL, auth (basic / bearer token / none), optional services allowlist.
- Per-agent binding — pick which agents (receptionist, sales-ai, support-ai, outbound-sales, outbound-support) should load each gateway.
- Connection test — probes the gateway's `/services` endpoint with the configured credentials and lists the services + tools it exposes inline. Confirms the connection is live before committing to the binding.
- Encrypted at rest — passwords and bearer tokens stored via the same Fernet helper as other secrets.
- Attached **per request** via the SDK's `mcp_gateway` skill from the call's workspace config — no code changes, no rebuild, no restart; gateway/binding changes apply to the next call.

**Bundled "DemoShop" gateway** ships in docker-compose so the feature works the moment the cloner runs `docker-compose up`. A small SQLite-backed e-commerce backend exposes six tools: `find_customer_by_phone`, `get_order`, `list_recent_orders`, `track_shipment`, `start_return`, `check_inventory`. Pre-seeded customers, products, and orders. Pre-bound to the sales + support AI specialists. The cloner sees a working "Test" button on first login. Replace with a real gateway when ready (see `demo-mcp/README.md`).

The pitch: every CCaaS says "we integrate with Salesforce." That took them 18 months and breaks when Salesforce changes. We say: paste your gateway URL. Your AI has whatever tools you exposed, in production, today.

### Webhook Event Log

Every inbound SignalWire webhook is logged as a `WebhookEvent` row and browsable from a dedicated Settings tab — filterable by event type, paginated, with expandable raw payloads. When a call misbehaves, the answer to "what did SignalWire actually send us?" is one click away instead of a grep through container logs.

### User Management

- List every user account with role, languages, subscriber link status, creation date.
- Inline quick-edit: role dropdown, languages preview (edit in detail modal).
- **Edit modal** for deeper configuration:
  - Role change (with self-edit safety)
  - Language multi-select
  - Hierarchical permissions with "Listen" as a parent category and per-scope children (AI calls / Human calls); standalone flags for Whisper, Barge, Control recording.
  - Visible "overridden" badges when a permission diverges from the role default.
  - Per-flag reset to role default, or whole-set reset.
  - Subscriber summary (read-only).
  - Delete user with stacked confirmation modal.
- Admin-scoped endpoint: only admins can edit other users; any authenticated user can edit their own languages via a self-serve endpoint.

---

## Security & access control

### Four roles

- **Admin** — full access to every configuration surface; can edit any user.
- **Supervisor** — sees the Supervisor tab and observer actions on other users' calls; no admin configuration access.
- **Agent** — participates in calls; default observer permissions are off.
- **Visitor** (hosted demo only) — an anonymous demo persona: workspace-scoped admin surface with the destructive/platform routes carved out via `@require_full_admin`. Included in `SUPERVISORY_ROLES`, so supervisor surfaces also admit it in demo mode.

### Gating layers

- **Frontend tab visibility** — Settings admin-only, Supervisor tab for admin + supervisor, other tabs for all roles. URL-path guard redirects direct-URL access.
- **Backend blueprint-level gate** — every `/api/admin/*` route requires admin role via a single blueprint-before-request hook.
- **Per-capability permission flags** — `can_listen_ai_calls`, `can_listen_human_calls`, `can_whisper`, `can_barge`, `can_control_recording`. Resolved per-user as role defaults merged with per-user overrides.
- **`@require_permission` decorator** — enforces flags on specific endpoints (e.g., observer `monitor/start` gated on the appropriate listen flag based on the call's handler type).

### Audit safety

- Admins cannot demote themselves to a non-admin role (prevents lockout).
- Admins cannot delete themselves.
- Permission overrides are stored separately from role defaults so a "revert to defaults" action is always available.

---

## Recording, transcription, sentiment

### Recording

- AI calls auto-record via SWML.
- Human calls support on-demand recording start/stop.
- Recording URL stored on the Call record; inline playback + download in the post-call review view.
- Status endpoint lets the UI hydrate the button on mount so it reflects the real recording state.

### Transcription

- Real-time utterance stream from AI agents, relayed to the UI via WebSocket.
- Full transcript saved to the Call record.
- AI-generated post-call summary via the `post_prompt` mechanism.

### Sentiment

- AI agents report sentiment events (score, reason, timestamp) during calls.
- Live sentiment indicator on the agent UI.
- Persisted for historical review and for supervisor "needs attention" flags.
- Post-call **sentiment arc** — per-call timeline of sentiment segments in the review view.

---

## Observability

- **Event stream per call** — real-time feed of every lifecycle event (transfer, recording, monitor start/stop, translate start/stop, hold, TTS injection, agent join/leave) with color-coded types.
- **Socket.IO push updates** — contact updates, call updates, queue updates, sentiment updates, authenticated events — all streamed to the frontend in real time.
- **Structured backend logs** — every SignalWire REST call logs its full request and response bodies. Errors include call IDs, user IDs, and SignalWire status codes for quick triage.

---

## Platform & infrastructure

### Stack

- **Frontend**: React 18 + TypeScript + Vite + Tailwind CSS + Radix UI primitives + Framer Motion + Zustand + TanStack Query.
- **Backend**: Flask 3 + SQLAlchemy + Alembic + Flask-SocketIO + Gunicorn (eventlet workers).
- **AI Agents**: Python SignalWire Agents SDK, FastAPI-backed, running as a separate service.
- **Storage**: PostgreSQL 15 with pgvector (contacts, calls, queues, knowledge-base embeddings).
- **Cache/pub-sub**: Redis 7 (call-control state, queue agents, session tokens).
- **Reverse proxy**: Nginx (routes `/api/*`, `/socket.io/*`, `/ws/tap-stream/*`, and the agent-service routes to the right container).

### SignalWire integration

- **Call Fabric** for all voice — phone numbers, SIP endpoints, subscribers, conferences, routing resources all addressable through one API.
- **SWML** (SignalWire Markup Language) for declarative call-flow definition, in use across every entry point.
- **Agents SDK** for AI agent definitions, SWAIG function tool-calling, Prompt Object Model, multilingual support.
- **REST SDK** (`signalwire-sdk`) for platform management — phone number configuration, subscriber CRUD, Fabric resource management, DataSphere access (`services/signalwire_client.py`). Call control (`services/signalwire_api.py`) drives the Calling API over direct HTTP.
- **Fabric resource auto-sync** — a backend service keeps managed SWML Webhook resources pointed at the current external URL so the call center works through ngrok tunnel rotation without manual dashboard edits.

### Deployment

- **Docker Compose** orchestrates the full stack locally: Postgres, Redis, Flask backend, Vite frontend, AI agents, Nginx.
- **Database migrations** via Alembic.
- **Environment-based configuration** — a single `.env` file with SignalWire credentials, external URL, and feature flags.

### Developer experience

- **ngrok auto-integration** (`scripts/dev/start_ngrok.bat`) — one command spins up a tunnel, writes the URL into `.env`, recreates the backend containers, and syncs the Fabric resource URL so inbound webhooks resolve to the fresh tunnel.
- **Swaig-test CLI** for testing AI agents locally without deployment.
- **Alembic migrations** apply cleanly on container boot.

---

## What's planned but not yet shipped

The items below are on the roadmap but not yet live. Listed here for completeness so this document stays honest about scope. (Audited against the code 2026-07-16.)

- **Warm transfer with briefing** — agent-to-agent handoff with spoken context. The previous transfer endpoint was removed after an audit found it desynced DB state from the actual SignalWire call (LIFE-02); the replacement path via conference `move_participant` plus a consult-then-handoff flow is designed but not wired. Return-to-Queue and Request-Backup cover the interim.
- **IVR callback opt-in** — "press 1 for a callback" while waiting. The agent-facing callback queue (schedule / claim / dial / outcomes) is live; the caller-initiated IVR entry path is not.
- **Transcript-synced recording scrubbing** — the inline player and download shipped; click-a-transcript-line-to-seek has not.
- **Named routing profiles** — per-number routing modes (AI triage / AI specialist / direct-to-queue) shipped; reusable named profiles beyond that have not.
- **Per-agent prompt editor** — KB bindings and external tools are already UI-editable; prompts intentionally stay in code (see "Architecture principle"), so this remains an open design question rather than a commitment.
- **Post-call survey** via the SDK's survey prefab.
- **Outbound campaigns** — list upload, power dialer. (Answering-machine detection itself shipped 2026-06-09: outbound AI calls and callback dials run `detect_machine` — voicemail gets a short message after the beep and a clean hangup instead of the AI pitch.)
- **Video escalation.**
- **Visual SWML Builder** — drag-and-drop IVR flow editor. Deferred until the Tier 2 CCaaS gaps close.

Each item has a design note in the internal roadmap. Ship order and prioritization are intentional — the architecture has to stay coherent as we extend it.

---

## Architecture principle

**Agents stay in code. Everything they query at runtime is configurable via UI without restart.**

This is the invariant. Knowledge bases, queue config, routing rules, phone number assignments, user permissions, language profiles, MCP server configs (when they land) — all editable from the admin UI. The AI agent code itself defines *what tools exist*; the data they operate over is yours to shape without redeploying anything.

That's what makes "platform you build on" real instead of a tagline.
