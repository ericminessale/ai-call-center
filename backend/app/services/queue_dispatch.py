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
updates while the caller waits, pre-join "agent joining" TTS the moment a
dispatch fires, and the hold timeout that bounds the wait (see
``_offer_callback_and_release``).

This file exists to avoid duplicating the emit code in multiple callers and
to sidestep circular imports between queue_service.py and queues.py.
"""

import json
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Hold-timeout tunables
# ---------------------------------------------------------------------------

# end_reason stamped on a call we released into the callback queue. Distinct
# from 'abandoned_in_queue' on purpose — the caller did not give up on us, we
# took them off hold — so wallboards and the call-history chip can tell the
# two apart. Also becomes the QueueAttempt.exit_reason, because
# Call.update_status feeds end_reason to close_open_queue_attempt.
END_REASON_CALLBACK_SCHEDULED = 'callback_scheduled'

# Seconds to let the closing announcement play before we drop the caller's
# leg. calling.play returns when SignalWire ACCEPTS the command, not when the
# audio finishes, so ending the call immediately would cut the caller off
# mid-sentence — they'd hear a syllable and a hangup instead of the promise
# we just made them.
CALLBACK_ANNOUNCE_SETTLE_SECONDS = 9

# Consecutive play_tts failures that end the loop. A leg that has gone away
# without us being told (carrier drop, stale zset entry) fails every single
# announcement; previously those failures were logged and ignored forever.
MAX_CONSECUTIVE_TTS_FAILURES = 3

# Absolute ceiling on one caller's announcement loop, however the queue is
# configured. Only reachable when an admin has disabled the per-queue cap
# (set it to 0) — it exists so a disabled cap can still never mean "this
# greenlet runs until the process restarts". Matches
# call_watchdog.STALE_MAX_AGE['waiting']: past that point the watchdog has
# reaped the Call row, so announcing into it is provably pointless.
HOLD_LOOP_CEILING_SECONDS = 2100


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
    app, call_sid: str, queue_slug: str, interval_seconds: int = 30,
    workspace_id=None,
) -> None:
    """Spawn an eventlet background task that periodically plays the caller's
    current queue position via play_tts. Self-terminates when the call is no
    longer in the queue zset (dispatched / hung up / abandoned).

    ``app`` must be the actual Flask app object (not the current_app proxy)
    so the background greenlet can push its own context. ``workspace_id``
    picks which workspace's queue zset to watch (queue keys are
    ``ws:{id}:queue:{slug}`` — slugs repeat across workspaces).
    """
    from app import socketio
    socketio.start_background_task(
        _announcement_loop, app, call_sid, queue_slug, interval_seconds,
        workspace_id,
    )


def _queue_hold_cap_seconds(queue_slug: str, workspace_id) -> Optional[int]:
    """The queue's ``max_wait_before_ai_fallback``, or None when unbounded.

    Read fresh on every announcement rather than captured once, so an admin
    editing the queue in Settings takes effect on callers who are ALREADY
    holding — that is the whole point of the setting being live config.

    Returns None when the cap is disabled (admin set it to 0 or NULL) or the
    queue row has gone away; in both cases only HOLD_LOOP_CEILING_SECONDS
    bounds the loop.

    Filters ``workspace_id`` explicitly instead of leaning on the ORM
    auto-scope: this runs in a background greenlet with no request context,
    so ``current_workspace_id()`` is None and nothing would be filtered —
    and queue slugs repeat across workspaces (§8.2), so an unfiltered
    ``filter_by(slug=...)`` would read some other tenant's setting.
    """
    from app.models import Queue

    try:
        queue = Queue.query.filter_by(
            slug=queue_slug, workspace_id=workspace_id,
        ).first()
    except Exception as e:
        logger.warning(
            f"Hold cap lookup failed for queue '{queue_slug}' "
            f"(workspace {workspace_id}): {e}"
        )
        return None

    if queue is None:
        return None
    cap = queue.max_wait_before_ai_fallback
    if cap is None or int(cap) <= 0:
        return None
    return int(cap)


def _waited_seconds(entry: Optional[Dict[str, Any]]) -> Optional[int]:
    """How long this caller has been waiting, per their queue entry.

    Keys off the entry's ``enqueued_at``, which despite the name is the
    call's ORIGINAL arrival time, preserved across re-enqueues and
    returns-to-queue (LIFE-06 in ``queue_service.enqueue_call``). So this is
    the same clock the SLA math and the agent desktop's countdown already
    use — "their wait is their wait, regardless of how many agents touched
    the call" — and the hold timeout can't be reset by bouncing a caller
    between agents.

    None when the entry has no parseable timestamp; callers treat that as
    "wait unknown" and leave the caller holding rather than guess.
    """
    from datetime import datetime

    if not entry:
        return None
    raw = entry.get('enqueued_at')
    if not raw:
        return None
    try:
        enqueued_at = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    return max(0, int((datetime.utcnow() - enqueued_at).total_seconds()))


def _offer_callback_and_release(
    call_sid: str, queue_slug: str, workspace_id, waited_seconds: int,
    cap_seconds: int,
) -> None:
    """Hold timeout: put the caller on the callback list and free the line.

    Runs when a waiting caller passes their queue's
    ``max_wait_before_ai_fallback``. Enrolling them in the callback queue
    (``models/callback.py`` + ``api/callbacks.py``, already agent-facing)
    turns an unbounded hold into a promise somebody can actually keep: the
    row lands on the callback board, an agent claims and dials it, and the
    outcome vocabulary already covers "caller said no thanks" (`declined`).

    Deliberately NOT an AI hand-back, despite the column name. Sending the
    caller's leg back to ``queue.ai_agent_route`` means re-pointing a leg
    that is mid-``join_conference`` at new SWML, and the only primitive here
    for that (``signalwire_api.update_call``) is dead code whose payload
    shape doesn't match any command this file's other methods use — it looks
    modelled on Twilio's compat API, not ``/api/calling/calls``. Shipping
    live-call surgery on that is how LIFE-02 desynced DB state from
    SignalWire state and got the transfer path 501'd. Every primitive used
    below is one this repo already exercises on real calls.

    Order matters and is chosen so the caller is never lied to:
      1. Atomically claim the call, losing to an agent who just took it.
      2. Create + COMMIT the callback row, so the promise is durable.
      3. Only then announce it.
      4. Dequeue before the settle sleep, so no dispatch lands on a leg we
         are about to drop.
      5. Hang up, then run the standard end-of-call cleanup.

    Best-effort throughout: a failure after the claim still releases the
    caller, because leaving them on hold forever is the bug being fixed.
    """
    from datetime import datetime

    from sqlalchemy import text

    from app import db, socketio
    from app.models import Call, Callback
    from app.services.call_watchdog import reap_call
    from app.services.queue_service import QueueService
    from app.services.redis_service import get_redis_client
    from app.services.signalwire_api import get_signalwire_api
    from app.tenancy import workspace_context

    # Pin the workspace so the auto-scope, the flush-time stamper and the
    # per-workspace callback cap all resolve to this caller's tenant — the
    # documented mechanism for background jobs (tenancy.py:18-26).
    with workspace_context(workspace_id):
        call = Call.find_by_sid(call_sid)
        if call is None:
            logger.warning(
                f"Hold timeout {call_sid}: no Call row — dequeuing only"
            )
            try:
                QueueService(
                    get_redis_client(), workspace_id=workspace_id,
                ).remove_call_from_all_queues(call_sid)
            except Exception as e:
                logger.warning(f"Hold timeout {call_sid}: dequeue failed: {e}")
            return

        # (1) Compare-and-set on end_reason. Whoever moves it off NULL owns
        # the teardown, and the extra predicates lose to an agent who was
        # dispatched between our cap check and now (push-dispatch fires from
        # another greenlet the moment an agent goes available) or to a
        # hangup/watchdog reap that already ended the call. Same shape as
        # the assigned_agent_id claim in enqueue_and_build_swml.
        try:
            claim = db.session.execute(
                text(
                    "UPDATE calls SET end_reason = :reason "
                    "WHERE id = :id AND end_reason IS NULL "
                    "AND ended_at IS NULL AND assigned_agent_id IS NULL "
                    "RETURNING id"
                ),
                {'reason': END_REASON_CALLBACK_SCHEDULED, 'id': call.id},
            )
            claimed = claim.fetchone() is not None
            if claimed:
                db.session.commit()
            else:
                db.session.rollback()
        except Exception as e:
            db.session.rollback()
            logger.warning(f"Hold timeout {call_sid}: claim failed: {e}")
            return

        if not claimed:
            logger.info(
                f"Hold timeout {call_sid}: lost the claim — the call was "
                f"assigned or already ended. Leaving it alone."
            )
            return
        db.session.refresh(call)

        # (2) Only promise a callback we can actually place. No number to
        # dial (SIP/web origin, withheld caller ID) or the workspace is at
        # its callback cap → release honestly with no promise attached.
        to_number = (call.from_number or '').strip()
        dialable = bool(to_number) and (
            to_number.startswith('+') or to_number.isdigit()
        )

        callback = None
        blocked_reason = None
        if not dialable:
            blocked_reason = f'no dialable caller number ({call.from_number!r})'
        else:
            from app.utils.workspace_caps import cap_denial
            capped = cap_denial('callbacks')
            if capped:
                blocked_reason = f'workspace callback cap reached ({capped[0].get("cap")})'

        if blocked_reason is None:
            # Idempotence: a re-enqueued caller starts a fresh announcement
            # greenlet, and two greenlets for one call must not mint two
            # promises. Reuse any live pending row instead.
            try:
                callback = (
                    db.session.query(Callback)
                    .filter(
                        Callback.call_id == call.id,
                        Callback.completed_at.is_(None),
                        Callback.expires_at > datetime.utcnow(),
                    )
                    .order_by(Callback.requested_at.desc())
                    .first()
                )
            except Exception as e:
                logger.warning(
                    f"Hold timeout {call_sid}: existing-callback lookup failed: {e}"
                )
                callback = None

            if callback is None:
                try:
                    # create_from_call snapshots the AI-collected reason,
                    # caller name and context, so the agent who dials back
                    # picks up the thread instead of re-triaging. The
                    # system-side fact goes in notes, NOT reason — reason is
                    # the caller's own words and overwriting it would throw
                    # away the triage.
                    callback = Callback.create_from_call(call, queue_id=queue_slug)
                    callback.phone_number = to_number
                    callback.notes = (
                        f'Auto-enrolled by the queue hold timeout: waited '
                        f'{waited_seconds}s in "{queue_slug}", past the queue\'s '
                        f'{cap_seconds}s maximum, with no agent available.'
                    )
                    db.session.add(callback)
                    db.session.commit()
                except Exception as e:
                    db.session.rollback()
                    callback = None
                    blocked_reason = f'callback row create failed: {e}'
                    logger.error(f"Hold timeout {call_sid}: {blocked_reason}")
                else:
                    try:
                        # Canonical emitter — reused rather than duplicated so
                        # the payload shape can't drift from the REST paths.
                        from app.api.callbacks import _emit_callback_event
                        _emit_callback_event('created', callback)
                    except Exception as e:
                        logger.warning(
                            f"Hold timeout {call_sid}: callback_event emit failed: {e}"
                        )

        # (3) Tell the caller what just happened. Only promise the callback
        # when there is a row backing it.
        if callback is not None:
            message = (
                "Thanks for your patience. Rather than keep you holding, we've "
                "added you to our callback list, and one of our specialists "
                "will call you back on this number. You can hang up now. "
                "Goodbye."
            )
        else:
            message = (
                "We're sorry — our specialists are all still busy, and we "
                "haven't been able to connect you. Please try your call again "
                "a little later. Goodbye."
            )
        try:
            get_signalwire_api().play_tts(call_sid, message)
        except Exception as e:
            logger.warning(
                f"Hold timeout {call_sid}: closing announcement failed "
                f"(releasing anyway): {e}"
            )

        # (4) Out of the queue before the settle sleep — a caller we are
        # about to hang up on must not be dispatched to an agent.
        try:
            QueueService(
                get_redis_client(), workspace_id=workspace_id,
            ).remove_call_from_all_queues(call_sid)
        except Exception as e:
            logger.warning(f"Hold timeout {call_sid}: dequeue failed: {e}")

        socketio.sleep(CALLBACK_ANNOUNCE_SETTLE_SECONDS)

        # (5) Drop the leg, then run the same end-of-call cleanup the
        # /call-status webhook and the watchdog run: legs closed, conference
        # ended, agent released, dashboards told. end_reason is already
        # stamped, so reap_call keeps 'callback_scheduled' rather than
        # computing 'abandoned_in_queue'.
        try:
            get_signalwire_api().end_call(call_sid)
        except Exception as e:
            logger.warning(
                f"Hold timeout {call_sid}: end_call failed (cleaning up "
                f"anyway; the watchdog is the backstop): {e}"
            )
        try:
            reap_call(call)
        except Exception as e:
            logger.error(f"Hold timeout {call_sid}: cleanup failed: {e}")

        if callback is not None:
            logger.warning(
                f"[hold_timeout] {call_sid} waited {waited_seconds}s in "
                f"'{queue_slug}' (cap {cap_seconds}s) → callback {callback.id} "
                f"for {to_number}, line released"
            )
        else:
            logger.warning(
                f"[hold_timeout] {call_sid} waited {waited_seconds}s in "
                f"'{queue_slug}' (cap {cap_seconds}s) → released with NO "
                f"callback: {blocked_reason}"
            )


def _announcement_loop(
    app, call_sid: str, queue_slug: str, interval_seconds: int, workspace_id=None
) -> None:
    """Internal — runs in an eventlet greenlet."""
    from app import socketio
    from app.services.redis_service import get_redis_client
    from app.services.signalwire_api import get_signalwire_api
    from app.services.ws_rooms import ws_key

    with app.app_context():
        redis = get_redis_client()
        if not redis:
            logger.warning(f"Announcement loop {call_sid}: Redis unavailable, exiting")
            return

        queue_key = ws_key(workspace_id, f"queue:{queue_slug}")
        # Initial delay so the caller has time to actually land in the
        # conference before we try to play anything to them. Clamped to the
        # queue's hold cap so a cap shorter than the announcement interval
        # still fires roughly on time instead of a full interval late.
        cap_seconds = _queue_hold_cap_seconds(queue_slug, workspace_id)
        socketio.sleep(
            interval_seconds if cap_seconds is None
            else max(1, min(interval_seconds, cap_seconds))
        )

        iteration = 0
        tts_failures = 0
        # Sum of the delays we've waited through. Only used for the
        # disabled-cap backstop, so intended-sleep accounting is precise
        # enough and keeps the loop testable without faking a clock.
        elapsed_seconds = 0
        while True:
            iteration += 1

            # Find the caller's current position in the queue zset.
            try:
                members = redis.zrange(queue_key, 0, -1)
            except Exception as e:
                logger.warning(f"Announcement loop {call_sid}: zrange failed: {e}")
                return

            position = None
            entry = None
            for idx, raw in enumerate(members):
                try:
                    raw_str = raw.decode() if isinstance(raw, bytes) else raw
                    parsed = json.loads(raw_str)
                    if parsed.get('call_id') == call_sid:
                        position = idx + 1
                        entry = parsed
                        break
                except Exception:
                    continue

            if position is None:
                logger.info(
                    f"Announcement loop {call_sid} exiting — no longer in queue"
                )
                return

            # Hold timeout. Checked BEFORE the position announcement so we
            # never tell someone "please continue holding" and then take
            # them off hold in the same breath.
            cap_seconds = _queue_hold_cap_seconds(queue_slug, workspace_id)
            waited = _waited_seconds(entry)
            if cap_seconds is not None and waited is not None and waited >= cap_seconds:
                _offer_callback_and_release(
                    call_sid, queue_slug, workspace_id, waited, cap_seconds,
                )
                return

            # Backstop for a cap the admin disabled (0) — and for a stale
            # zset entry whose call is long gone but whose play_tts keeps
            # being accepted.
            if elapsed_seconds >= HOLD_LOOP_CEILING_SECONDS:
                logger.warning(
                    f"Announcement loop {call_sid} exiting — hit the "
                    f"{HOLD_LOOP_CEILING_SECONDS}s hard ceiling after "
                    f"{iteration} announcements (queue '{queue_slug}' has no "
                    f"max-wait configured)"
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
                tts_failures = 0
            except Exception as e:
                tts_failures += 1
                logger.warning(
                    f"Announcement loop {call_sid} play_tts iteration {iteration} failed: {e}"
                )
                if tts_failures >= MAX_CONSECUTIVE_TTS_FAILURES:
                    logger.warning(
                        f"Announcement loop {call_sid} exiting — "
                        f"{tts_failures} consecutive play_tts failures, the "
                        f"leg is almost certainly gone"
                    )
                    return

            # Land the next pass on the cap rather than one interval past it.
            sleep_for = interval_seconds
            if cap_seconds is not None and waited is not None:
                sleep_for = max(1, min(interval_seconds, cap_seconds - waited))
            elapsed_seconds += sleep_for
            socketio.sleep(sleep_for)


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
      - Otherwise, starts the periodic position-announcement greenlet, which
        also enforces the queue's ``max_wait_before_ai_fallback`` — see
        ``_offer_callback_and_release``

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

    # Enqueue in Redis — keyed to the call's workspace (§8.2).
    qs = QueueService(get_redis_client(), workspace_id=call.workspace_id)
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

    # Emit queue_update so the workspace's Queue tab + counts update.
    try:
        from app.services.ws_rooms import workspace_room
        socketio.emit('queue_update', {
            'call': call.to_dict(include_contact=True),
            'queue_id': queue_slug,
            'action': 'added',
        }, room=workspace_room(call.workspace_id))
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
                    claim_at = datetime.utcnow()
                    claim = db.session.execute(
                        text(
                            "UPDATE calls "
                            "SET assigned_agent_id = :uid, assigned_at = :ts, status = 'assigned' "
                            "WHERE id = :id AND assigned_agent_id IS NULL "
                            "RETURNING id"
                        ),
                        {'uid': selected_user.id, 'ts': claim_at, 'id': call.id},
                    )
                    if claim.fetchone():
                        if conf:
                            conf.owner_user_id = selected_user.id
                        try:
                            from app.services.interaction_timeline import best_effort, record_queue_offered
                            best_effort(record_queue_offered, call, selected_user.id, claim_at)
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
    # loop would clash with it. The loop owns the hold timeout too, so this
    # is also what bounds the wait for a caller nobody picks up.
    if not agent_dispatched:
        try:
            start_position_announcement_loop(
                current_app._get_current_object(), call_sid, queue_slug,
                workspace_id=call.workspace_id,
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
        #
        # lang: the caller's known language (threaded in by /route from
        # global_data, by /direct-inbound from the call/contact). Only
        # matters on the /direct-inbound path in practice — when an AI agent
        # transfers here a session is already running on this leg and the
        # re-start below is a no-op.
        from app.services.call_language import normalize_language
        main_section.append({
            "live_transcribe": {
                "action": {
                    "start": {
                        "webhook": signed_webhook_url(f"{base_url}/api/webhooks/transcription"),
                        "lang": normalize_language(caller_language) or 'en-US',
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
