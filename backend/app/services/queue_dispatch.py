"""Shared agent-dispatch helper used by both /direct-inbound (immediate
dispatch when an agent is available at call arrival) and the push-dispatch
hook in queue_service (agent becomes available while a caller is waiting).

Both paths emit the same Socket.IO `call_assignment` event to the agent's
room. The agent's frontend renders the incoming-call banner; on Accept, the
frontend hits `/api/conferences/prepare-join` to stash assignment data with a
token, then dials the AGENT_CONFERENCE_RESOURCE Fabric address with that
token. SignalWire calls our /api/conferences/agent-conference webhook, which
returns SWML that joins the agent's leg into the named conference.

Also hosts the SWML hold cycle — the caller-audible half of waiting in a
queue. A caller with no agent available does NOT sit in the conference:
their leg loops through short SWML documents (position announcement + hold
music + ``transfer`` back to /api/queues/<slug>/hold), and each fetch is a
decision point: keep holding, join the conference the moment an agent was
dispatched, or — past the queue's ``max_wait_before_ai_fallback`` — commit a
Callback row and speak the callback promise before releasing the line.

Why SWML and not REST audio: this space's /api/calling/calls command
envelope silently ignores play commands (HTTP 200, no audio — proven live
2026-08-11 by the hank_hold_callback synthetic scenario), and the documented
per-call /play path 404s on live legs. There is no REST audio-injection
primitive here; SWML documents are the only thing a parked caller provably
hears, so the hold experience is built entirely out of them.

This file exists to avoid duplicating the emit code in multiple callers and
to sidestep circular imports between queue_service.py and queues.py.
"""

import json
import logging
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Hold-cycle tunables
# ---------------------------------------------------------------------------

# end_reason stamped on a call we released into the callback queue. Distinct
# from 'abandoned_in_queue' on purpose — the caller did not give up on us, we
# took them off hold — so wallboards and the call-history chip can tell the
# two apart. Also becomes the QueueAttempt.exit_reason, because
# Call.update_status feeds end_reason to close_open_queue_attempt.
END_REASON_CALLBACK_SCHEDULED = 'callback_scheduled'

# Length of the bundled hold-music segment (backend/app/assets/hold_music.wav,
# served at /api/queues/hold-music). One hold cycle ≈ announcement + this, so
# it is also the cadence of position announcements AND the worst-case lag
# between an agent being dispatched and the caller's leg reaching the
# join_conference decision point. Keep it short: a dispatched agent sits
# alone in the conference until the caller's current cycle finishes.
HOLD_MUSIC_SECONDS = 20

# Rough spoken length of the position announcement. Only used to aim the
# last cycle at the hold cap instead of overshooting by a full segment.
HOLD_ANNOUNCE_SECONDS = 5

# Seconds before the background teardown reaps a released call. The release
# SWML plays the ~9s closing announcement and then executes ``hangup``
# itself, so this only needs to comfortably outlast the audio; the REST
# end_call inside the teardown is a backstop for a leg whose hangup verb
# never ran.
RELEASE_TEARDOWN_DELAY_SECONDS = 15

# Absolute ceiling on one caller's hold, however the queue is configured.
# Only reachable when an admin has disabled the per-queue cap (set it to 0) —
# it exists so a disabled cap can still never mean "this caller cycles until
# the heartbeat keeps the watchdog away forever". Matches
# call_watchdog.STALE_MAX_AGE['waiting'].
HOLD_LOOP_CEILING_SECONDS = 2100

# Seconds between a returned-to-queue caller leaving their conference (the
# customer-leave webhook) and _return_verify checking that their leg actually
# survived the conference teardown. A surviving leg fetches /after-conference
# and heartbeats within a second or two; 15s comfortably covers webhook and
# fetch skew without leaving a dead leg's row open for long.
RETURN_VERIFY_DELAY_SECONDS = 15


# ---------------------------------------------------------------------------
# Hold-cycle SWML builders
# ---------------------------------------------------------------------------

# The two release announcements. Wording is load-bearing: the synthetic
# scenarios assert the caller actually HEARD "call you back" phrasing, and
# the promise variant must only ever play once a Callback row is committed.
# Deliberately NO "you can hang up now": the old REST flow said that to
# cover its 9s settle-sleep before end_call, but here ``hangup`` executes
# the moment the audio ends — inviting the caller to hang up first just
# races them against us and makes the release read as an abandon.
RELEASE_MESSAGE_PROMISE = (
    "Thanks for your patience. Rather than keep you holding, we've "
    "added you to our callback list, and one of our specialists "
    "will call you back on this number. Thank you for calling. "
    "Goodbye."
)
RELEASE_MESSAGE_NO_PROMISE = (
    "We're sorry — our specialists are all still busy, and we "
    "haven't been able to connect you. Please try your call again "
    "a little later. Goodbye."
)

PRE_JOIN_ANNOUNCEMENT = "An agent is joining you now. Please hold."

# First audio of a renewed hold after an agent returned the caller to the
# queue. Rides the after-conference SWML document — the only channel a
# post-conference leg provably hears on this space; the REST TTS the old
# return flow attempted was a silent no-op for every caller.
RETURN_TO_QUEUE_ANNOUNCEMENT = (
    "Let me connect you with someone better suited. "
    "Please hold for just a moment."
)


def _swml_doc(main: list) -> Dict[str, Any]:
    return {"version": "1.0.0", "sections": {"main": main}}


def hold_cycle_url(base_url: str, queue_slug: str, call_sid: str,
                   cycle: int) -> str:
    """Signed URL for one iteration of the hold cycle. The token binds the
    path only, so the per-cycle query params don't invalidate it."""
    from app.utils.url_utils import signed_webhook_url
    return signed_webhook_url(
        f"{base_url}/api/queues/{queue_slug}/hold"
        f"?call_sid={call_sid}&n={cycle}"
    )


def _hold_music_url(base_url: str) -> str:
    # Public media fetch — SignalWire's player does not carry webhook auth,
    # and the asset is not sensitive.
    return f"{base_url}/api/queues/hold-music"


def after_conference_url(base_url: str, call_sid: str) -> str:
    """Signed URL a caller's leg fetches when its interaction conference ends
    under it (the agent's member joins with ``end_on_exit``, so the agent
    leaving — return-to-queue, end of call, browser death — ends the
    conference and the caller's script resumes). No queue slug in the path:
    the decision reads the call's live ``queue_id``, which return-to-queue
    may have retargeted while the caller sat in the conference."""
    from app.utils.url_utils import signed_webhook_url
    return signed_webhook_url(
        f"{base_url}/api/queues/after-conference?call_sid={call_sid}"
    )


def _release_swml(promised: bool) -> Dict[str, Any]:
    """The final document of a released hold: closing announcement, then the
    leg hangs itself up. Playing the message from SWML (not REST) is the
    whole point — hangup runs only after the audio finished, so the caller
    hears the entire promise instead of a syllable and a click."""
    message = RELEASE_MESSAGE_PROMISE if promised else RELEASE_MESSAGE_NO_PROMISE
    return _swml_doc([
        {"play": {"url": f"say:{message}"}},
        "hangup",
    ])


def _join_conference_swml(call, base_url: str) -> Dict[str, Any]:
    """Document for a caller whose agent was dispatched while they cycled:
    announce, then join the interaction conference the agent is (about to
    be) in. Mirrors the entry SWML's dispatched branch — same conference
    name, same post-conference decision fetch.

    The verb AFTER ``join_conference`` runs when the conference ends with
    this leg still alive — which happens whenever the agent leaves first,
    because the agent's member joins with ``end_on_exit``. That boundary is
    a decision point exactly like a hold-cycle fetch: ``transfer`` to
    /after-conference, whose answer re-queues a returned caller (the ONLY
    audible path for the return-to-queue announcement on this space) or
    hangs up a finished one, which is all the old inline ``hangup`` did."""
    conference_name = (
        call.conference_name or f"interaction-{call.signalwire_call_sid}"
    )
    return _swml_doc([
        {"play": {"url": f"say:{PRE_JOIN_ANNOUNCEMENT}"}},
        {"join_conference": {"name": conference_name, "end_on_exit": False}},
        {"transfer": {
            "dest": after_conference_url(base_url, call.signalwire_call_sid)
        }},
    ])


def _cycle_swml(base_url: str, queue_slug: str, call_sid: str, cycle: int,
                position: Optional[int], waited: Optional[int],
                cap: Optional[int]) -> Dict[str, Any]:
    """One hold iteration: (announcement +) music, then transfer back to the
    hold endpoint for the next decision. Cycle 1 skips the announcement —
    the entry SWML's greeting said the position seconds ago."""
    urls = []
    announce = cycle > 1 and position is not None
    if announce:
        urls.append(
            f"say:You are number {position} in the queue. "
            f"Please continue holding."
        )

    # Land the release near the cap instead of a full segment past it: when
    # less than one normal cycle remains, pad with silence sized to the
    # remainder rather than starting another 20s music segment.
    remaining = None
    if cap is not None and waited is not None:
        remaining = max(0, cap - waited)
    announce_len = HOLD_ANNOUNCE_SECONDS if announce else 0
    if remaining is not None and remaining < announce_len + HOLD_MUSIC_SECONDS:
        urls.append(f"silence:{max(2, remaining - announce_len)}")
    else:
        urls.append(_hold_music_url(base_url))

    return _swml_doc([
        {"play": {"urls": urls}},
        {"transfer": {
            "dest": hold_cycle_url(base_url, queue_slug, call_sid, cycle + 1)
        }},
    ])


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


def _pending_callback_for(call):
    """The call's live pending Callback row, if any. 'Live' means not
    completed and not expired — the same definition the callback board uses.
    None on lookup failure (callers treat that as 'no promise to speak of')."""
    from datetime import datetime

    from app import db
    from app.models import Callback

    try:
        return (
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
        logger.warning(f"Pending-callback lookup failed for call {call.id}: {e}")
        return None


def _claim_hold_release(
    call, queue_slug: str, workspace_id, waited_seconds: int, cap_seconds: int,
) -> Tuple[bool, Optional[Any]]:
    """Hold timeout: claim the call and make the callback promise durable.

    Runs when a waiting caller passes their queue's
    ``max_wait_before_ai_fallback``. Enrolling them in the callback queue
    (``models/callback.py`` + ``api/callbacks.py``, already agent-facing)
    turns an unbounded hold into a promise somebody can actually keep: the
    row lands on the callback board, an agent claims and dials it, and the
    outcome vocabulary already covers "caller said no thanks" (`declined`).

    Deliberately NOT an AI hand-back, despite the column name — there is no
    working primitive to re-point a live leg at an AI route from REST (see
    signalwire_api.update_call), and LIFE-02 is what shipping that anyway
    looks like. The hold CYCLE is what makes this announcement possible at
    all: the caller's leg fetches its next document from us, so the promise
    rides back as SWML instead of a REST play that provably never sounds.

    Ordering — the caller is never lied to:
      1. Atomically claim the call, losing to an agent who just took it.
      2. Create + COMMIT the callback row, so the promise is durable.
      3. Dequeue, so no dispatch lands on a leg we are about to release.
    Only then does the hold endpoint serve the release SWML that actually
    speaks the promise (and hangs up after it finished playing).

    Returns ``(claimed, callback)``: ``claimed`` False means someone else
    (dispatch, hangup, another cycle) owns the call — serve their outcome
    instead. ``callback`` None with ``claimed`` True means release WITHOUT a
    promise (no dialable number / workspace cap / row create failed).

    Caller must hold ``workspace_context(workspace_id)`` — the auto-scope,
    the flush-time stamper and the per-workspace callback cap all key off it.
    """
    from sqlalchemy import text

    from app import db
    from app.services.queue_service import QueueService
    from app.services.redis_service import get_redis_client

    call_sid = call.signalwire_call_sid

    # (1) Compare-and-set on end_reason. Whoever moves it off NULL owns the
    # teardown, and the extra predicates lose to an agent who was dispatched
    # between the cap check and now (push-dispatch fires from another
    # greenlet the moment an agent goes available) or to a hangup/watchdog
    # reap that already ended the call. Same shape as the assigned_agent_id
    # claim in enqueue_and_build_swml.
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
        return False, None

    if not claimed:
        logger.info(
            f"Hold timeout {call_sid}: lost the claim — the call was "
            f"assigned or already ended. Leaving it alone."
        )
        return False, None
    db.session.refresh(call)

    # (2) Only promise a callback we can actually place. No number to dial
    # (SIP/web origin, withheld caller ID) or the workspace is at its
    # callback cap → release honestly with no promise attached.
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
        # Idempotence: a redelivered release document or a re-enqueued
        # caller must not mint two live promises for one call. Reuse any
        # pending row instead.
        callback = _pending_callback_for(call)

        if callback is None:
            try:
                # create_from_call snapshots the AI-collected reason,
                # caller name and context, so the agent who dials back
                # picks up the thread instead of re-triaging. The
                # system-side fact goes in notes, NOT reason — reason is
                # the caller's own words and overwriting it would throw
                # away the triage.
                from app.models import Callback
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

    # (3) Out of the queue before the goodbye plays — a caller we are about
    # to release must not be dispatched to an agent.
    try:
        QueueService(
            get_redis_client(), workspace_id=workspace_id,
        ).remove_call_from_all_queues(call_sid)
    except Exception as e:
        logger.warning(f"Hold timeout {call_sid}: dequeue failed: {e}")

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
    return True, callback


def _schedule_release_teardown(call_sid: str, workspace_id) -> None:
    """Arrange the DB/Redis teardown for a call whose release SWML was just
    served. Runs delayed in a greenlet because the leg is still playing the
    closing announcement — and SWML-parked legs deliver NO call-state
    webhook (see the /api/webhooks/call-heartbeat docstring), so if we
    don't close the row ourselves nobody will until the watchdog."""
    from flask import current_app

    from app import socketio

    app = current_app._get_current_object()
    socketio.start_background_task(_release_teardown, app, call_sid, workspace_id)


def _release_teardown(app, call_sid: str, workspace_id) -> None:
    """Internal — runs in a greenlet ~RELEASE_TEARDOWN_DELAY_SECONDS after
    the release document went out. By then the announcement has played and
    the document's own ``hangup`` has dropped the leg; end_call is only a
    backstop for a leg whose hangup verb never ran, so an error from it is
    the EXPECTED outcome, not a failure. reap_call keeps a pre-stamped
    'callback_scheduled' end_reason and emits the same events the webhook
    'ended' branch would."""
    from app import socketio
    from app.models import Call
    from app.services.call_watchdog import reap_call
    from app.services.signalwire_api import get_signalwire_api
    from app.tenancy import workspace_context

    with app.app_context():
        socketio.sleep(RELEASE_TEARDOWN_DELAY_SECONDS)
        with workspace_context(workspace_id):
            call = Call.find_by_sid(call_sid)
            if call is None:
                return
            if call.ended_at is not None or call.status in Call.TERMINAL_STATUSES:
                return
            try:
                get_signalwire_api().end_call(call_sid)
            except Exception as e:
                logger.info(
                    f"Release teardown {call_sid}: end_call backstop errored "
                    f"(leg normally already hung itself up): {e}"
                )
            try:
                reap_call(call)
            except Exception as e:
                logger.error(f"Release teardown {call_sid}: cleanup failed: {e}")


def _queue_entry_for(redis, workspace_id, queue_slug: str, call_sid: str):
    """(position, entry) for this caller in the queue zset, or (None, None)
    when they are not queued. Raises on a Redis read failure — the caller
    must treat that as 'unknown', NOT 'gone' (releasing a live caller over
    a Redis blip would be the old bug with a new face)."""
    from app.services.ws_rooms import ws_key

    members = redis.zrange(ws_key(workspace_id, f"queue:{queue_slug}"), 0, -1)
    for idx, raw in enumerate(members):
        try:
            raw_str = raw.decode() if isinstance(raw, bytes) else raw
            parsed = json.loads(raw_str)
            if parsed.get('call_id') == call_sid:
                return idx + 1, parsed
        except Exception:
            continue
    return None, None


def hold_cycle_swml(
    call_sid: str, queue_slug: str, cycle: int, base_url: str,
) -> Dict[str, Any]:
    """Decide and build the next SWML document for a caller parked on hold.

    This is the whole hold state machine. The caller's leg fetches it via
    the ``transfer`` at the end of the entry SWML (cycle 1) and of every
    cycle document after that, so each fetch is a decision point observed at
    most one music segment after the state changed:

      - release already claimed        → speak it again (redelivery) + hangup
      - call ended                     → hangup
      - agent dispatched               → pre-join announcement + join_conference
      - past the queue's hold cap      → claim, mint the Callback, promise, hangup
      - past the hard ceiling          → honest no-promise release
      - otherwise                      → position announcement + music + transfer

    Checked in that order so we never announce "please continue holding" on
    the same pass that takes the caller off hold. Any unexpected error in
    the HTTP wrapper (api/queues.py) degrades to a polite hangup rather
    than dead air.
    """
    from app import db
    from app.models import Call
    from app.services.redis_service import get_redis_client
    from app.tenancy import workspace_context

    call = Call.find_by_sid(call_sid) if call_sid else None
    if call is None:
        logger.warning(f"Hold cycle: unknown call {call_sid!r} — hanging up")
        return _swml_doc(["hangup"])

    workspace_id = call.workspace_id
    with workspace_context(workspace_id):
        # Redelivery of an already-claimed release (previous response lost
        # mid-flight, or a crash between claim and respond): the durable
        # state decides the wording, and the teardown is (re)scheduled —
        # it no-ops on an already-ended call.
        if call.end_reason == END_REASON_CALLBACK_SCHEDULED:
            _schedule_release_teardown(call_sid, workspace_id)
            return _release_swml(_pending_callback_for(call) is not None)

        if call.ended_at is not None or call.status in Call.TERMINAL_STATUSES:
            return _swml_doc(["hangup"])

        # Dispatched while cycling → join the conference the agent was sent
        # to. Both dispatch paths commit assigned_agent_id BEFORE dequeuing,
        # so a caller observed neither assigned nor queued below is
        # genuinely out of routing, not mid-handoff.
        if call.assigned_agent_id:
            logger.info(
                f"Hold cycle {call_sid}: agent {call.assigned_agent_id} "
                f"dispatched — sending caller into the conference"
            )
            return _join_conference_swml(call, base_url)

        redis = get_redis_client()
        position = entry = None
        queue_read_ok = False
        if redis:
            try:
                position, entry = _queue_entry_for(
                    redis, workspace_id, queue_slug, call_sid,
                )
                queue_read_ok = True
            except Exception as e:
                logger.warning(f"Hold cycle {call_sid}: queue read failed: {e}")

        if not queue_read_ok:
            # Redis unavailable: keep the caller alive on plain music and
            # retry the decision next cycle. position=None suppresses the
            # announcement (we don't know it).
            return _cycle_swml(
                base_url, queue_slug, call_sid, cycle, None, None, None,
            )

        if position is None:
            # Not queued, not assigned, not ended — the queue entry
            # evaporated under a live leg (admin cleared the queue, watchdog
            # swept a sibling, stale re-enqueue). Release honestly instead
            # of cycling music forever on a call routing has forgotten.
            logger.warning(
                f"Hold cycle {call_sid}: no longer queued in '{queue_slug}' "
                f"and not assigned — releasing with no promise"
            )
            _schedule_release_teardown(call_sid, workspace_id)
            return _release_swml(False)

        cap = _queue_hold_cap_seconds(queue_slug, workspace_id)
        waited = _waited_seconds(entry)

        if cap is not None and waited is not None and waited >= cap:
            claimed, callback = _claim_hold_release(
                call, queue_slug, workspace_id, waited, cap,
            )
            if claimed:
                _schedule_release_teardown(call_sid, workspace_id)
                return _release_swml(callback is not None)
            # Lost the claim — a dispatch or teardown won between the cap
            # check and now. Serve whatever the winner decided.
            db.session.refresh(call)
            if call.assigned_agent_id:
                return _join_conference_swml(call, base_url)
            if call.end_reason == END_REASON_CALLBACK_SCHEDULED:
                _schedule_release_teardown(call_sid, workspace_id)
                return _release_swml(_pending_callback_for(call) is not None)
            return _swml_doc(["hangup"])

        if waited is not None and waited >= HOLD_LOOP_CEILING_SECONDS:
            # Cap disabled by the admin; the hard ceiling still bounds the
            # hold. Dequeue first so no dispatch lands during the goodbye.
            # No claim and no promise: a disabled cap must not silently
            # start scheduling callbacks.
            try:
                from app.services.queue_service import QueueService
                QueueService(
                    redis, workspace_id=workspace_id,
                ).remove_call_from_all_queues(call_sid)
            except Exception as e:
                logger.warning(
                    f"Hold cycle {call_sid}: ceiling dequeue failed: {e}"
                )
            logger.warning(
                f"Hold cycle {call_sid}: hit the {HOLD_LOOP_CEILING_SECONDS}s "
                f"ceiling (queue '{queue_slug}' has no max-wait cap) — "
                f"releasing with no promise"
            )
            _schedule_release_teardown(call_sid, workspace_id)
            return _release_swml(False)

        # Normal iteration. The fetch itself proves the leg is alive — feed
        # the watchdog's heartbeat fast-skip (same key the legacy bridge
        # hold loop used) so a long-but-live hold isn't reaped mid-music.
        try:
            redis.set(f"call_heartbeat:{call_sid}", '1', ex=90)
        except Exception:
            pass
        return _cycle_swml(
            base_url, queue_slug, call_sid, cycle, position, waited, cap,
        )


def after_conference_swml(call_sid: str, base_url: str) -> Dict[str, Any]:
    """Decide the fate of a caller leg whose interaction conference just
    ended under it.

    The agent's conference member joins with ``end_on_exit``, so the agent
    leaving ends the conference and the caller's script resumes at the
    ``transfer`` that fetches this. Two ways that happens with the caller
    still alive:

      - Return-to-queue: call_control committed status='waiting', cleared
        the assignment and re-enqueued BEFORE telling the agent's browser
        to hang up, so by the time this fetch arrives the durable state
        already says "back in the queue". Speak the handoff announcement
        and ``transfer`` into the SWML hold cycle — the machine that owns
        position announcements, re-dispatch and the hold cap. This is the
        only architecture in which the caller HEARS the announcement: the
        old flow's REST TTS was a proven silent no-op, and worse, the old
        inline ``hangup`` after ``join_conference`` disconnected the
        returned caller outright the moment the agent left.

      - End of call where the agent's leg dropped first (agent clicked
        End, or their browser died): the caller is done. Hang up — exactly
        what the old inline verb did.

    A caller re-taken by another agent between the return commit and this
    fetch (status 'assigned') joins the new agent's conference directly,
    same as the hold cycle's dispatched branch. Everything else defaults
    to hangup: this endpoint must never strand a leg in silence or re-join
    a conference nobody is coming back to.
    """
    from app.models import Call
    from app.services.redis_service import get_redis_client
    from app.tenancy import workspace_context

    call = Call.find_by_sid(call_sid) if call_sid else None
    if call is None:
        logger.warning(
            f"After-conference: unknown call {call_sid!r} — hanging up"
        )
        return _swml_doc(["hangup"])

    with workspace_context(call.workspace_id):
        if (
            call.ended_at is not None
            or call.status in Call.TERMINAL_STATUSES
            or call.end_reason is not None
        ):
            return _swml_doc(["hangup"])

        if call.status == 'waiting' and call.assigned_agent_id is None:
            queue_slug = (call.queue_id or '').strip()
            if not queue_slug:
                logger.warning(
                    f"After-conference {call_sid}: returned to queue but no "
                    f"queue to hold in — hanging up"
                )
                return _swml_doc(["hangup"])
            # The fetch itself proves the leg survived the conference
            # teardown — heartbeat immediately so _return_verify and the
            # watchdog both know. (A vanished queue entry is NOT checked
            # here: the hold cycle's first fetch releases honestly in that
            # case, with wording we'd only duplicate.)
            redis = get_redis_client()
            if redis:
                try:
                    redis.set(f"call_heartbeat:{call_sid}", '1', ex=90)
                except Exception:
                    pass
            logger.info(
                f"After-conference {call_sid}: returned to queue "
                f"'{queue_slug}' — announcing and entering the hold cycle"
            )
            return _swml_doc([
                {"play": {"url": f"say:{RETURN_TO_QUEUE_ANNOUNCEMENT}"}},
                {"transfer": {
                    "dest": hold_cycle_url(base_url, queue_slug, call_sid, 1)
                }},
            ])

        if call.status == 'assigned' and call.assigned_agent_id:
            # Re-taken between the return commit and this fetch. Same
            # conference name, fresh instance — the new agent's leg joins
            # it on Accept.
            return _join_conference_swml(call, base_url)

        # 'active' with an agent = normal end where the agent's leg dropped
        # first; anything unrecognised gets the same safe teardown.
        return _swml_doc(["hangup"])


def schedule_return_verify(call_sid: str, workspace_id) -> None:
    """Arrange the survival check for a returned-to-queue caller whose
    customer-leave event just arrived. The conference dying under the
    caller is BY DESIGN on that path — their leg should be fetching
    /after-conference right now — but if SignalWire tore the leg down with
    the conference instead, nothing else will ever close the row
    (SWML-parked legs deliver no call-state webhook), so verify shortly
    and reap only if the leg never checked in."""
    from flask import current_app

    from app import socketio

    app = current_app._get_current_object()
    socketio.start_background_task(_return_verify, app, call_sid, workspace_id)


def _return_verify(app, call_sid: str, workspace_id) -> None:
    """Internal — runs ~RETURN_VERIFY_DELAY_SECONDS after a returned-to-queue
    caller left their conference. A surviving leg has heartbeated (the
    /after-conference fetch and every hold cycle set ``call_heartbeat``) or
    the call has progressed (re-assigned / active / terminal); absent all
    of those, the leg died with the conference: dequeue, backstop-end the
    leg, and let ``reap_call`` classify the row (abandoned_in_queue)."""
    from app import socketio
    from app.models import Call
    from app.services.call_watchdog import reap_call
    from app.services.queue_service import QueueService
    from app.services.redis_service import get_redis_client
    from app.services.signalwire_api import get_signalwire_api
    from app.tenancy import workspace_context

    with app.app_context():
        socketio.sleep(RETURN_VERIFY_DELAY_SECONDS)
        with workspace_context(workspace_id):
            call = Call.find_by_sid(call_sid)
            if call is None:
                return
            if call.ended_at is not None or call.status in Call.TERMINAL_STATUSES:
                return
            if call.status != 'waiting' or call.assigned_agent_id is not None:
                # Progressed — re-taken or otherwise moved on. Not ours.
                return
            redis = get_redis_client()
            if redis is None:
                return
            try:
                heartbeat = redis.get(f"call_heartbeat:{call_sid}")
            except Exception as e:
                # Unknown is not gone — never reap a live caller over a
                # Redis blip. The watchdog's sweep owns the long tail.
                logger.warning(
                    f"Return verify {call_sid}: heartbeat read failed: {e}"
                )
                return
            if heartbeat:
                return  # leg alive in the hold cycle
            logger.warning(
                f"Return verify {call_sid}: leg never re-entered the hold "
                f"cycle after its conference ended — closing the row"
            )
            try:
                QueueService(
                    redis, workspace_id=workspace_id,
                ).remove_call_from_all_queues(call_sid)
            except Exception as e:
                logger.warning(f"Return verify {call_sid}: dequeue failed: {e}")
            try:
                get_signalwire_api().end_call(call_sid)
            except Exception as e:
                logger.info(
                    f"Return verify {call_sid}: end_call backstop errored "
                    f"(leg normally already gone): {e}"
                )
            try:
                reap_call(call)
            except Exception as e:
                logger.error(f"Return verify {call_sid}: cleanup failed: {e}")


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
        emits ``call_assignment`` to the agent's room

    Returns: SWML dict for the caller's leg. Ends with
        join_conference(interaction-<call_sid>) + a ``transfer`` to
        /after-conference (the post-conference decision — re-queue or
        hangup, see ``after_conference_swml``) when an agent was
        dispatched, or a ``transfer`` into the SWML hold cycle otherwise —
        the cycle (``hold_cycle_swml``) owns position announcements, the
        ``max_wait_before_ai_fallback`` timeout and the eventual conference
        join. See ``_claim_hold_release``.
    """
    from datetime import datetime
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
                            # Language-preference fallback -> live
                            # translation. Shared with the delayed
                            # push-dispatch path so a caller who WAITS for an
                            # agent gets the same treatment as one dispatched
                            # on arrival; that is the common case here, since
                            # waiting is what happens when no matching agent
                            # was free.
                            from app.services.call_language import (
                                flag_translation_if_mismatched,
                            )
                            flag_translation_if_mismatched(call, selected_user)
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
    if agent_dispatched:
        # Agent already on the way — park the caller in the conference the
        # agent's WebRTC leg joins on Accept. When the conference later ends
        # with this leg still alive (the agent's member joins with
        # end_on_exit, so their exit ends it), the script resumes at the
        # transfer: /after-conference re-queues a returned caller or hangs
        # up a finished one — see after_conference_swml.
        main_section.append({
            "join_conference": {
                "name": conference_name,
                "end_on_exit": False,
            }
        })
        main_section.append({
            "transfer": {
                "dest": after_conference_url(base_url, call_sid)
            }
        })
    else:
        # Nobody available: enter the SWML hold cycle instead of the
        # conference. Each cycle plays the position + music and transfers
        # back to /api/queues/<slug>/hold, whose response is the next
        # decision (keep holding / join_conference once dispatched /
        # callback promise at the hold cap). This is the only architecture
        # in which the caller actually HEARS any of that — REST play into
        # a parked leg is a proven silent no-op on this space.
        main_section.append({
            "transfer": {
                "dest": hold_cycle_url(base_url, queue_slug, call_sid, 1)
            }
        })

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

    # No caller-side announcement from here: REST play into a parked leg is
    # a proven silent no-op on this space. The caller hears "an agent is
    # joining you now" from their own SWML — the entry greeting on immediate
    # dispatch, or the hold cycle's join document on push-dispatch (served
    # at their next cycle boundary, ≤ one music segment away).

    # AI Coach (sidecar) attach is NOT done here. The sidecar bills per-minute,
    # so we don't pre-attach speculatively at dispatch — the agent decides
    # per-call via the Coach panel mode picker, which hits
    # POST /api/calls/<sid>/coach/attach to start it. Admin gates this with
    # the `can_use_coach` permission flag.

    return True
