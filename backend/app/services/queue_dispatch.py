"""Shared agent-dispatch helper used by both /direct-inbound (immediate
dispatch when an agent is available at call arrival) and the push-dispatch
hook in queue_service (agent becomes available while a caller is waiting).

Both paths emit the same Socket.IO `call_assignment` event to the agent's
room. The agent's frontend renders the incoming-call banner; on Accept, the
frontend hits `/api/conferences/prepare-join` to stash assignment data with a
token, then dials the AGENT_CONFERENCE_RESOURCE Fabric address with that
token. SignalWire calls our /api/conferences/agent-conference webhook, which
returns SWML that joins the agent's leg into the named conference. Caller is
already in the same conference (placed there by /direct-inbound's SWML), so
both end up bridged with no additional plumbing.

Also hosts the per-caller in-conference announcement loop — periodic position
updates while the caller waits, and pre-join "agent joining" TTS the moment a
dispatch fires.

This file exists to avoid duplicating the emit code in multiple callers and
to sidestep circular imports between queue_service.py and queues.py.
"""

import json
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# In-conference TTS announcements
# ---------------------------------------------------------------------------

def play_caller_pre_join_announcement(call_sid: str) -> None:
    """Tell the caller an agent is about to join. Fired the moment we dispatch
    an agent (immediate at call arrival OR later via push-dispatch). Best-
    effort — if play_tts fails, dispatch still proceeds. Caller will hear the
    music duck and the agent's voice when they join either way.
    """
    if not call_sid:
        return
    try:
        from app.services.signalwire_api import get_signalwire_api
        get_signalwire_api().play_tts(
            call_sid,
            "An agent is joining you now. Please hold.",
        )
    except Exception as e:
        logger.warning(f"play_caller_pre_join_announcement failed for {call_sid}: {e}")


def start_position_announcement_loop(
    app, call_sid: str, queue_slug: str, interval_seconds: int = 30
) -> None:
    """Spawn an eventlet background task that periodically plays the caller's
    current queue position via play_tts. Self-terminates when the call is no
    longer in the queue zset (dispatched / hung up / abandoned).

    ``app`` must be the actual Flask app object (not the current_app proxy)
    so the background greenlet can push its own context.
    """
    from app import socketio
    socketio.start_background_task(
        _announcement_loop, app, call_sid, queue_slug, interval_seconds
    )


def _announcement_loop(app, call_sid: str, queue_slug: str, interval_seconds: int) -> None:
    """Internal — runs in an eventlet greenlet."""
    from app import socketio
    from app.services.redis_service import get_redis_client
    from app.services.signalwire_api import get_signalwire_api

    with app.app_context():
        redis = get_redis_client()
        if not redis:
            logger.warning(f"Announcement loop {call_sid}: Redis unavailable, exiting")
            return

        queue_key = f"queue:{queue_slug}"
        # Initial delay so the caller has time to actually land in the
        # conference before we try to play anything to them.
        socketio.sleep(interval_seconds)

        iteration = 0
        while True:
            iteration += 1

            # Find the caller's current position in the queue zset.
            try:
                members = redis.zrange(queue_key, 0, -1)
            except Exception as e:
                logger.warning(f"Announcement loop {call_sid}: zrange failed: {e}")
                return

            position = None
            for idx, raw in enumerate(members):
                try:
                    raw_str = raw.decode() if isinstance(raw, bytes) else raw
                    if json.loads(raw_str).get('call_id') == call_sid:
                        position = idx + 1
                        break
                except Exception:
                    continue

            if position is None:
                logger.info(
                    f"Announcement loop {call_sid} exiting — no longer in queue"
                )
                return

            try:
                get_signalwire_api().play_tts(
                    call_sid,
                    (
                        f"You are number {position} in the queue. "
                        f"Please continue holding."
                    ),
                )
            except Exception as e:
                logger.warning(
                    f"Announcement loop {call_sid} play_tts iteration {iteration} failed: {e}"
                )

            socketio.sleep(interval_seconds)


# ---------------------------------------------------------------------------
# Unified queue onboarding — used by both /direct-inbound and /route
# ---------------------------------------------------------------------------

def enqueue_and_build_swml(
    *,
    call,
    queue_slug: str,
    context: Dict[str, Any],
    base_url: str,
    routing_strategy: str = 'round_robin',
    caller_language: str = 'en-US',
    agent_languages: Optional[Dict[str, Any]] = None,
    skill_levels: Optional[Dict[str, Any]] = None,
    priority: int = 5,
    start_live_transcribe: bool = True,
) -> Dict[str, Any]:
    """Single queue-onboarding entry point. Both /direct-inbound (PSTN caller
    arrives directly at a phone number) and /route (AI agent transfers a
    caller to the queue) call this. They differ only in what context they've
    collected and pass in; the queue lifecycle from here on is identical.

    Side effects (all best-effort, none of them fatal to returning SWML):
      - Ensures Conference DB row exists for ``interaction-<call_sid>``
      - Enqueues the call in Redis ``queue:<slug>`` (no-op if already there)
      - Emits ``queue_update action='added'`` so dashboards refresh
      - If an agent is available right now, dispatches them: marks Call as
        assigned, sets their Redis status to busy, removes from queue zset,
        emits ``call_assignment`` to the agent's room (which also fires the
        caller pre-join TTS announcement)
      - Otherwise, starts the periodic position-announcement greenlet

    Returns: SWML dict for the caller's leg. ALWAYS ends with
        join_conference(interaction-<call_sid>) + hangup-on-exit.
    """
    from datetime import datetime
    from flask import current_app
    from app import db, socketio
    from app.models import Conference, User
    from app.services.queue_service import QueueService
    from app.services.redis_service import get_redis_client
    from app.utils.url_utils import signed_webhook_url

    call_sid = call.signalwire_call_sid
    conference_name = f"interaction-{call_sid}"

    # Conference DB row — ensures ConferenceParticipants / supervisor monitoring
    # / call-leg tracking have a row to attach to. Owner is filled later if a
    # dispatch happens below; otherwise stays NULL until push-dispatch later.
    try:
        conf = Conference.get_active_by_name(conference_name)
        if not conf:
            conf = Conference.create_interaction_conference(
                call_id=call_sid,
                queue_id=queue_slug,
                agent_user_id=None,
            )
    except Exception as e:
        logger.warning(f"enqueue_and_build_swml: Conference row creation failed (non-fatal): {e}")
        conf = None

    if call.conference_name != conference_name:
        call.conference_name = conference_name
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()

    # Enqueue in Redis.
    qs = QueueService(get_redis_client())
    queue_position = 1
    try:
        queue_result = qs.enqueue_call(
            call_id=call_sid,
            queue_id=queue_slug,
            priority=priority,
            context=context,
            caller_info={
                'number': call.from_number,
                'name': context.get('customer_name'),
            },
        )
        if isinstance(queue_result, dict):
            queue_position = queue_result.get('position', 1)
    except Exception as e:
        logger.warning(f"enqueue_and_build_swml: enqueue failed (non-fatal): {e}")

    # Emit queue_update so the dashboard Queue tab + counts update.
    try:
        socketio.emit('queue_update', {
            'call': call.to_dict(include_contact=True),
            'queue_id': queue_slug,
            'action': 'added',
        })
    except Exception as e:
        logger.warning(f"enqueue_and_build_swml: queue_update emit failed: {e}")

    # Default greeting (no-agent path). Switches below if we dispatch now.
    greeting = (
        f"say:All of our specialists are currently helping other customers. "
        f"You are number {queue_position} in the queue. Please hold."
    )
    agent_dispatched = False

    # Immediate-dispatch check. Same selection used by push-dispatch later if
    # no agent is available now — single code path.
    try:
        available_agents = qs.get_available_agents(queue_slug)
        if available_agents:
            agent_id_str = qs.select_agent(
                queue_slug=queue_slug,
                routing_strategy=routing_strategy,
                available_agents=available_agents,
                skill_levels=skill_levels or {},
                call_priority=priority,
                caller_language=caller_language,
                agent_languages=agent_languages or {},
            )
            if agent_id_str:
                try:
                    selected_user = User.query.filter_by(id=int(agent_id_str)).first()
                except (ValueError, TypeError):
                    selected_user = User.query.filter_by(email=agent_id_str).first()
                if selected_user and selected_user.signalwire_address:
                    # Atomic claim — same race fix as push-dispatch. Two
                    # immediate-dispatch paths can fire in parallel (two
                    # concurrent inbound calls arriving while several agents
                    # are available); without the WHERE clause both would
                    # write to assigned_agent_id and one of the banner
                    # recipients would later get rejected on Take.
                    from sqlalchemy import text
                    claim = db.session.execute(
                        text(
                            "UPDATE calls "
                            "SET assigned_agent_id = :uid, assigned_at = :ts, status = 'assigned' "
                            "WHERE id = :id AND assigned_agent_id IS NULL "
                            "RETURNING id"
                        ),
                        {'uid': selected_user.id, 'ts': datetime.utcnow(), 'id': call.id},
                    )
                    if claim.fetchone():
                        if conf:
                            conf.owner_user_id = selected_user.id
                        try:
                            db.session.commit()
                            db.session.refresh(call)
                        except Exception:
                            db.session.rollback()
                        try:
                            qs.set_agent_status(
                                str(selected_user.id), 'busy', current_call_id=call_sid
                            )
                        except Exception as e:
                            logger.warning(f"enqueue_and_build_swml: agent busy failed: {e}")
                        try:
                            qs.remove_call_from_all_queues(call_sid)
                        except Exception as e:
                            logger.warning(f"enqueue_and_build_swml: dequeue after assign failed: {e}")
                        emit_call_assignment_to_agent(
                            call=call,
                            agent=selected_user,
                            conference_name=conference_name,
                            queue_slug=queue_slug,
                            context=context,
                        )
                        greeting = (
                            "say:Connecting you to an agent now. "
                            "They'll be joining you shortly, please hold."
                        )
                        agent_dispatched = True
                    else:
                        # Lost race — another path claimed the call. Fall
                        # through to the announcement-loop branch so this
                        # caller gets a normal queue experience while the
                        # winning path runs its own dispatch sequence.
                        db.session.rollback()
                        logger.info(
                            f"enqueue_and_build_swml: lost claim race on "
                            f"call {call_sid} — another worker assigned. "
                            f"Falling through to queue hold."
                        )
    except Exception as e:
        logger.warning(f"enqueue_and_build_swml: immediate-dispatch check failed (non-fatal): {e}")

    # Start the announcement loop only when the caller is actually going to
    # wait. If we dispatched, the pre-join TTS already fired and a second
    # loop would clash with it.
    if not agent_dispatched:
        try:
            start_position_announcement_loop(
                current_app._get_current_object(), call_sid, queue_slug
            )
        except Exception as e:
            logger.warning(f"enqueue_and_build_swml: announcement loop failed to start: {e}")

    # Caller-side SWML — same structure for /direct-inbound and /route.
    main_section: list = [
        {
            "set": {
                "call_state_url": signed_webhook_url(f"{base_url}/api/webhooks/call-status"),
                "call_state_events": "created,ringing,answered,ended",
            }
        },
        "answer",
    ]
    if start_live_transcribe:
        # Persists across the conference join so transcripts continue to flow
        # to /api/webhooks/transcription throughout the call.
        main_section.append({
            "live_transcribe": {
                "action": {
                    "start": {
                        "webhook": signed_webhook_url(f"{base_url}/api/webhooks/transcription"),
                        "lang": "en-US",
                        "live_events": True,
                        "ai_summary": True,
                        # Steer the end-of-session summary toward a CRM wrap-up note.
                        # Delivered on teardown as conversation_summary in the
                        # calling.ai.transcribe.conversation_log event -> wrap-up notes
                        # (handled in webhooks._apply_ai_wrapup_summary). This is the
                        # conference/human leg, so it captures the human conversation.
                        # CODE-8 (2026-07-07): this is now the canonical copy of the
                        # wrap-up summary prompt — the old AI-leg copy in api/swml.py
                        # was deleted. The only other copy is the REST manual-start
                        # path in services/signalwire_api.py; keep the two in sync.
                        "ai_summary_prompt": (
                            "Summarize this call for an agent's CRM wrap-up in 2-4 "
                            "sentences of plain prose (no headings or lists). Cover ONLY "
                            "what actually happened: why the caller reached out, what was "
                            "discussed or done, and how the call ended. Do NOT invent or "
                            "assume outcomes, next steps, follow-ups, or appointments that "
                            "were not explicitly stated in the conversation. If the call "
                            "was brief, unresolved, or the caller was unclear, say so "
                            "plainly rather than inferring a resolution."
                        ),
                        "direction": ["remote-caller", "local-caller"],
                    }
                }
            }
        })
    main_section.append({"play": {"url": greeting}})
    main_section.append({
        "join_conference": {
            "name": conference_name,
            "end_on_exit": False,
        }
    })
    main_section.append("hangup")

    return {"version": "1.0.0", "sections": {"main": main_section}}


# ---------------------------------------------------------------------------
# Call assignment dispatch
# ---------------------------------------------------------------------------


def emit_call_assignment_to_agent(
    *,
    call,
    agent,
    conference_name: str,
    queue_slug: str,
    context: Optional[Dict[str, Any]] = None,
) -> bool:
    """Notify an agent's frontend about an incoming call assignment.

    Returns True if the event was emitted, False if preconditions weren't met
    (missing agent, missing call, etc.) — callers can use this to decide
    whether to fall back to "no agent available" SWML.

    Args:
        call: Call ORM row. Must have signalwire_call_sid, id, from_number,
              contact_id.
        agent: User ORM row representing the dispatched agent.
        conference_name: SignalWire conference the agent should join, e.g.
            ``interaction-<call_sid>``. Caller is (or will be) in this same
            conference.
        queue_slug: the queue this dispatch came from, surfaced in the
            agent's UI for context.
        context: optional AI-collected or routing context to surface in the
            agent's incoming-call banner.
    """
    from app import socketio  # local import — avoids circular at module load

    if not agent or not call:
        logger.warning(
            "emit_call_assignment_to_agent skipped: missing agent or call "
            f"(agent={bool(agent)}, call={bool(call)})"
        )
        return False

    context = context or {}

    payload = {
        'call_id': call.signalwire_call_sid,
        'call_db_id': call.id,
        'caller_number': call.from_number,
        'queue_id': queue_slug,
        'context': context,
        'agent_id': agent.id,
        'agent_name': agent.name or agent.email,
        'conference_name': conference_name,
        'agent_call_sid': None,  # server-initiated dial-out pattern not used
        'customer_info': {
            'phone': call.from_number,
            'name': context.get('customer_name'),
            'contact_id': call.contact_id,
        },
    }

    socketio.emit('call_assignment', payload, room=str(agent.id))
    logger.info(
        f"Dispatched call {call.signalwire_call_sid} → agent {agent.id} "
        f"({agent.email}) via conference {conference_name}"
    )

    # Tell the caller an agent is about to join. Fires now (when the agent
    # sees the banner) rather than at conference participantJoined because we
    # don't currently subscribe to SignalWire's conference events. Slight
    # timing risk if the agent dawdles before accepting, but the wording
    # ("joining you now") absorbs a few seconds gracefully.
    play_caller_pre_join_announcement(call.signalwire_call_sid)

    # AI Coach (sidecar) attach is NOT done here. The sidecar bills per-minute,
    # so we don't pre-attach speculatively at dispatch — the agent decides
    # per-call via the Coach panel mode picker, which hits
    # POST /api/calls/<sid>/coach/attach to start it. Admin gates this with
    # the `can_use_coach` permission flag.

    return True
