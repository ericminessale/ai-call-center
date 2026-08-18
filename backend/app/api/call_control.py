"""
Call Control API Blueprint
Real-time call manipulation endpoints: hold, record, play, DTMF, monitor, backup, escalate.
"""

from flask import Blueprint, request, jsonify
from app import db, socketio
from app.models.call import Call
from app.models.call_leg import CallLeg
from app.models import User
from app.models.user import SUPERVISORY_ROLES
from app.utils.decorators import require_auth, require_permission, require_role
from app.services.signalwire_api import get_signalwire_api
from app.services.redis_service import get_redis_client
from app.utils.url_utils import get_base_url, signed_tap_stream_url
from datetime import datetime
import logging
import json
import os

logger = logging.getLogger(__name__)

# Call states in which an agent is genuinely occupied. Used when deciding
# whether a Redis status that disagrees with the call being returned is stale
# drift (free the agent) or a newer real assignment (leave them busy).
ACTIVE_AGENT_CALL_STATUSES = ('assigned', 'active', 'answered', 'on_hold')


def release_agent_after_return(qs, released_agent_id, returning_sid) -> bool:
    """Put an agent back to available after their call was returned to queue,
    unless they have since been given a different live call. Returns True when
    the agent was freed.

    Two failure modes sit on either side of this. Freeing only when Redis
    still tracks THIS call meant any drift — a missed status write, a Redis
    restart, a takeover — left the agent marked busy with no call, invisible
    to dispatch for the rest of their shift. Freeing unconditionally is the
    opposite error: between two overlapping returns the agent can already have
    been dispatched a new call, and clearing that hands them a second one.

    So the DB arbitrates. A tracked call that is still live and still assigned
    to this agent is authoritative and they stay busy; anything else is stale
    and they go back to available.
    """
    agent_state = qs.get_agent_status(str(released_agent_id))
    tracked = (agent_state or {}).get('current_call_id')

    if tracked and tracked != returning_sid:
        other = Call.find_by_sid(tracked)
        if (other is not None
                and other.assigned_agent_id == released_agent_id
                and other.ended_at is None
                and other.status in ACTIVE_AGENT_CALL_STATUSES):
            logger.info(
                f"return_to_queue: agent {released_agent_id} is already on call "
                f"{tracked} (status={other.status}) — leaving them busy"
            )
            return False
        logger.warning(
            f"return_to_queue: agent {released_agent_id} Redis status tracked "
            f"stale call {tracked!r} while returning {returning_sid!r} — "
            "freeing anyway"
        )

    qs.set_agent_status(str(released_agent_id), 'available')
    return True

call_control_bp = Blueprint('call_control', __name__)


# ==================== Helpers ====================

def find_call(call_id):
    """Find a call by database ID or SignalWire call SID."""
    # Try by database ID first
    try:
        call = Call.query.get(int(call_id))
        if call:
            return call
    except (ValueError, TypeError):
        pass
    # Try by SignalWire call SID
    return Call.find_by_sid(str(call_id))


def emit_call_event(call_id, event_type, data, call_sid=None):
    """Proxy to the central emit_call_event in callcenter_socketio."""
    from app.services.callcenter_socketio import emit_call_event as _emit_event
    _emit_event(call_id, event_type, data, call_sid)


# ==================== Call Control Endpoints ====================

def _find_agent_participant(call, user_id):
    """Locate the current agent's ConferenceParticipant in the call's conference.

    Returns the ConferenceParticipant record whose call_sid we can mute/deaf,
    or None if the call isn't in a conference or the agent isn't a member yet.
    """
    if not call or not call.conference_name:
        return None
    from app.models.conference import Conference
    from app.models.conference_participant import ConferenceParticipant
    conf = Conference.get_active_by_name(call.conference_name)
    if not conf:
        return None
    return (
        db.session.query(ConferenceParticipant)
        .filter_by(
            conference_id=conf.id,
            participant_type='agent',
            participant_id=str(user_id),
            status='active',
        )
        .first()
    )


def _require_call_ownership(call, user):
    """Return (jsonify, status) tuple to abort the request, or None to allow.

    RE-AUDIT-04 (2026-06-03): endpoints that mutate call state or play
    media into the customer leg used to gate only on @require_auth,
    which let any logged-in user act on any agent's call by guessing
    the call_id. Reject unless the requester IS the assigned agent on
    the call OR holds supervisor/admin role. Apply to call control
    endpoints that fire user-facing effects (play, DTMF, return-to-queue,
    etc.).
    """
    if not call:
        return jsonify({'error': 'Call not found'}), 404
    role = getattr(user, 'role', '') or ''
    if role in SUPERVISORY_ROLES:
        return None
    if call.assigned_agent_id == user.id:
        return None
    # Initiator/attributed owner counts too — mirrors the "initiated /
    # assigned" owner definition join_call (ISO-3) and join_tap (RT-01)
    # already use. In demo mode this is what lets a phone-verified visitor
    # control their OWN inbound call (demo_verify attributes it via
    # call.user_id) even while the AI is the handling agent.
    if call.user_id == user.id:
        return None
    return jsonify({
        'error': "You don't have ownership of this call",
        'detail': (
            'Only the assigned agent (or a supervisor/admin) can mutate this '
            "call. If this is your call, your assignment may not have synced "
            "yet — refresh and try again."
        ),
    }), 403


@call_control_bp.route('/<call_id>/hold', methods=['POST'])
@require_auth
def hold_call(call_id):
    """Hold the call — NOT YET IMPLEMENTED.

    RE-AUDIT-01 (2026-06-03): the previous "leave-conference" workaround
    was fundamentally broken in two ways the re-audit caught:
      1. The interaction conference is created with ``end_on_exit: true``
         (conferences.py:528), so the moment the agent left, SignalWire
         ENDED the conference — disconnecting the caller, not holding
         them.
      2. The participant-leave webhook's ``is_hold_leave`` guard was
         unreachable because ``ConferenceParticipant.get_active_by_call_sid``
         filters ``status='active'`` and hold set the row to ``on_hold``
         FIRST.
      3. Frontend swallowed ``cf.hangup()`` failures and flipped the UI
         to "on hold" while the agent was still bridged — a privacy
         failure (customer keeps hearing the agent's mic after the
         "Please hold" TTS).
    Rather than fix three layered bugs against a feature SignalWire's
    own platform doesn't fully support yet, deferred. Joins the same
    "waiting on platform support" bucket as LIFE-02 transfer (the dev
    said participant-level mute/hold is "still being fleshed out").
    Frontend Hold button removed; this endpoint returns 501 so any
    stale client that still tries it gets a clear answer.
    """
    return jsonify({
        'error': 'Hold not implemented',
        'detail': (
            'Conference-participant hold is not yet supported by the '
            'SignalWire REST surface. The previous workaround (agent '
            'leaves the conference) was broken — end_on_exit:true on '
            'the interaction conference disconnected the caller. '
            'Deferred until the platform exposes participant-level '
            'hold. See RE-AUDIT-01 in REMEDIATION_2026-06-02.md.'
        ),
    }), 501


@call_control_bp.route('/<call_id>/unhold', methods=['POST'])
@require_auth
def unhold_call(call_id):
    """Resume a held call — NOT YET IMPLEMENTED. Counterpart to hold_call;
    same RE-AUDIT-01 disablement rationale."""
    return jsonify({
        'error': 'Unhold not implemented',
        'detail': 'Hold is currently disabled — see /hold for the rationale.',
    }), 501


# Codes accepted for the mandatory `reason` field on return-to-queue.
# Per the 2p spec — keeps the action from being used as "get rid of this
# caller" by forcing the agent to pick a category that supervisors can
# audit. Kept tight; new categories require a code change + UI update.
RETURN_REASON_CODES = (
    'wrong-queue',       # caller belongs to a different queue
    'taking-break',      # agent stepping away (lunch / EOD)
    'cannot-resolve',    # agent can't solve this; needs different skill
    'caller-request',    # caller asked for someone else
    'other',             # falls back to other w/ free-text note in body
)


@call_control_bp.route('/<call_id>/return-to-queue', methods=['POST'])
@require_auth
@require_permission('can_return_to_queue')
def return_call_to_queue(call_id):
    """Bounce an accepted call back to queue routing (Tier 2p).

    Fills the gap between Transfer (pick a specific target) and Hangup
    (end the call). Use cases: agent realised wrong queue, stepping away,
    can't resolve, caller asked for someone else.

    Flow:
      1. End the agent's CallLeg with reason='returned_to_queue' (this is
         a real handoff — different from hold which preserves the leg).
      2. Mark Call.status='waiting', clear assigned_agent_id, increment
         return_count, save last_return_reason.
      3. Free the agent's Redis status from busy → available.
      4. Re-enqueue the call in the original queue's zset with original
         priority + preserved ai_context (so the next agent doesn't have
         to re-triage).
      5. Tell the frontend to SDK-hangup, same pattern as Hold. The
         agent's conference member carries end_on_exit, so their exit ends
         the conference; the caller's leg then resumes its own SWML and
         fetches /api/queues/after-conference, which speaks the handoff
         announcement and drops them into the SWML hold cycle (position
         announcements, re-dispatch, hold cap). That fetch is the ONLY
         channel the caller provably hears — the REST TTS this flow used
         to attempt was a silent no-op on this space (verified live
         2026-08-11), and the old post-conference inline ``hangup``
         disconnected the returned caller outright. The waiting state
         MUST be committed before this response returns: the sdk_hangup
         it triggers is what makes the caller's leg fetch the decision.
      6. Soft-cap at 2 returns — if this would be the third return, refuse
         and tell the agent to escalate to a supervisor instead.

    SLA: the caller's wait clock continues from original call-received
    time — no reset. Per the spec, their wait is their wait regardless
    of how many agents touched them.

    Request body: ``{reason: str, target_queue_slug?: str, note?: str}``.
    reason must be in RETURN_REASON_CODES. target_queue_slug defaults to
    the call's current queue_id (most common — return to same queue, just
    a different agent). note is free-text, only stored when reason='other'.
    """
    call = find_call(call_id)
    if not call:
        return jsonify({'error': 'Call not found'}), 404
    if not call.conference_name:
        return jsonify({'error': 'Call is not in a conference — return not supported'}), 400

    # RE-AUDIT-04 (2026-06-03): ownership check. Previously @require_auth +
    # @require_permission('can_return_to_queue') passed — but any agent with
    # that permission could return any other agent's call by guessing the
    # call_id. Now reject unless the requester IS the assigned agent (or a
    # supervisor/admin). Pairs with the same gate on /play and /dtmf.
    owner_check = _require_call_ownership(call, request.current_user)
    if owner_check:
        return owner_check

    data = request.get_json() or {}
    reason = (data.get('reason') or '').strip()
    target_queue_slug = (data.get('target_queue_slug') or call.queue_id or '').strip()
    note = (data.get('note') or '').strip() or None

    if reason not in RETURN_REASON_CODES:
        return jsonify({
            'error': f'reason must be one of {list(RETURN_REASON_CODES)}'
        }), 400
    if not target_queue_slug:
        return jsonify({'error': 'target_queue_slug required (no queue on the call to default to)'}), 400

    # Soft cap — third return on the same call escalates instead of
    # recycling. Prevents bouncing-loops where multiple agents punt the
    # same caller back-and-forth.
    if (call.return_count or 0) >= 2:
        return jsonify({
            'error': (
                'Soft cap reached — this call has already been returned twice. '
                'Escalate to supervisor instead of returning to the queue again.'
            ),
            'return_count': call.return_count,
            'must_escalate': True,
        }), 409

    user = request.current_user

    try:
        # No REST announcement here — it never worked (silent no-op on this
        # space). The caller hears the handoff from their own SWML: when the
        # agent's SDK hangup ends the conference, the caller's leg fetches
        # /api/queues/after-conference, which speaks the announcement and
        # re-enters the hold cycle. Everything committed below must land
        # BEFORE this request returns, because the response is what triggers
        # that hangup.

        # 1. Mark the agent's CallLeg as completed — this is a real
        # handoff, not a hold pause. Reason captures the intent for
        # supervisor-side reporting.
        agent_leg = CallLeg.query.filter_by(
            call_id=call.id,
            user_id=user.id,
            status='active',
        ).first()
        # Also catch an on_hold leg in case the agent returns FROM hold.
        if not agent_leg:
            agent_leg = CallLeg.query.filter_by(
                call_id=call.id,
                user_id=user.id,
                status='on_hold',
            ).first()
        if agent_leg:
            agent_leg.end_leg(reason=f'returned_to_queue:{reason}')

        # 2. Mark agent's ConferenceParticipant as 'left'. (No on_hold
        # bypass — this leave SHOULD be a teardown.)
        agent_participant = _find_agent_participant(call, user.id)
        if agent_participant:
            agent_participant.leave()

        # 3. Reset call back to 'waiting'. Increment counter, save reason.
        # IMPORTANT: ai_context stays — the next agent sees the same
        # collected context, no re-triage. answered_at also stays —
        # SLA clock is original-to-now per the 2p spec.
        try:
            context = json.loads(call.ai_context) if call.ai_context else {}
        except Exception:
            context = {}
        from app.services.interaction_timeline import best_effort, record_return_to_queue
        best_effort(
            record_return_to_queue,
            call, target_queue_slug,
            reason=reason,
            priority=context.get('priority', 5),
        )

        # WHO to release. `user` is the requester, which is not always the
        # agent who held the call — a supervisor can return someone else's.
        # Freeing the requester in that case leaves the real agent busy with
        # no call, which is the very state this step exists to prevent.
        released_agent_id = call.assigned_agent_id or user.id

        call.status = 'waiting'
        call.assigned_agent_id = None
        call.assigned_at = None
        call.handler_type = None
        call.queue_id = target_queue_slug  # may differ from original
        call.return_count = (call.return_count or 0) + 1
        call.last_return_reason = (
            f'{reason}: {note}' if (reason == 'other' and note) else reason
        )
        db.session.commit()

        # 4. Free the agent's Redis status.
        try:
            from app.services.queue_service import QueueService
            qs = QueueService(get_redis_client(), workspace_id=call.workspace_id)
            release_agent_after_return(
                qs, released_agent_id, call.signalwire_call_sid,
            )
            # 5. Re-enqueue. Preserves AI-collected priority + context for
            # the next agent.
            qs.enqueue_call(
                call_id=call.signalwire_call_sid,
                queue_id=target_queue_slug,
                priority=context.get('priority', 5),
                context=context,
                caller_info={'number': call.from_number, 'name': None},
            )
        except Exception as e:
            logger.error(f"return_to_queue {call_id}: queue re-enqueue failed: {e}")
            # Don't bail — agent is already off the call. Manual recovery
            # is fine; better than leaving the agent stuck busy.

        # 6. Notify dashboards. queue_update fires the assignment banner
        # for whoever's next, AND clears it from the supervisor's
        # active-calls view since we're back to waiting.
        from app.services.callcenter_socketio import emit_call_update
        from app.services.ws_rooms import workspace_room
        emit_call_update(call)
        socketio.emit('queue_update', {
            'call': call.to_dict(include_contact=True),
            'queue_id': target_queue_slug,
            'action': 'added',
            'return_count': call.return_count,
            'last_return_reason': call.last_return_reason,
        }, room=workspace_room(call.workspace_id))
        emit_call_event(call.id, 'return_to_queue', {
            'agent': user.email,
            'reason': reason,
            'target_queue_slug': target_queue_slug,
            'return_count': call.return_count,
            'note': note,
        }, call.signalwire_call_sid)

        logger.info(
            f"Call {call.id} returned to queue '{target_queue_slug}' by "
            f"agent {user.id} (reason={reason}, return_count={call.return_count})"
        )

        return jsonify({
            'success': True,
            'call_id': call.id,
            'status': 'waiting',
            'queue_id': target_queue_slug,
            'return_count': call.return_count,
            'frontend_action': 'sdk_hangup',
        }), 200
    except Exception as e:
        logger.error(f"Failed to return call {call_id} to queue: {str(e)}")
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


@call_control_bp.route('/<call_id>/play', methods=['POST'])
@require_auth
def play_into_call(call_id):
    """Play audio or TTS into an active call — NOT SUPPORTED on this space.

    2026-08-11: every REST audio-injection shape was tested live against a
    held leg with transcript verification, and none produce audio. The
    ``calling.play`` command envelope returns 200 and plays nothing (a 200
    from /api/calling/calls means "call exists", not "command executed" —
    unknown commands are silently ignored) and the documented per-call
    ``/play`` path 404s. So for its entire life this endpoint reported
    success while the caller heard nothing — the agent-facing TTS
    soundboard it powered was removed from the frontend in the same commit.

    Audio a caller actually hears rides an SWML document their leg fetches
    (the hold-cycle / after-conference pattern in
    ``queue_dispatch``) or the ``ai`` verb on AI-handled calls. Neither fits
    an arbitrary speak-now-into-a-live-bridged-call feature, so this is
    501, not a workaround — same honest disposition as /hold (RE-AUDIT-01)
    and queue transfer (LIFE-02).
    """
    return jsonify({
        'error': 'Play-into-call not supported',
        'detail': (
            'REST audio injection into a live call leg is non-functional '
            'on this SignalWire space (verified live 2026-08-11): '
            'calling.play variants return 200 without producing audio and '
            'the per-call /play path 404s. Caller-audible audio must ride '
            'an SWML document the leg fetches — see the queue hold cycle — '
            'or the AI verb on AI-handled calls.'
        ),
    }), 501


@call_control_bp.route('/<call_id>/record/start', methods=['POST'])
@require_auth
@require_permission('can_control_recording')
def start_recording(call_id):
    """Start recording an active call."""
    call = find_call(call_id)
    if not call:
        return jsonify({'error': 'Call not found'}), 404

    # ISO-5 (2026-07-07 pre-deploy): ownership gate — RE-AUDIT-04 added
    # this to play/DTMF/return-to-queue but skipped the recording/translate
    # siblings, so any user with can_control_recording could start/stop
    # recording on another visitor's call by id.
    owner_check = _require_call_ownership(call, request.current_user)
    if owner_check:
        return owner_check

    try:
        sw_api = get_signalwire_api()
        result = sw_api.start_recording(call.signalwire_call_sid)

        # Store control_id in Redis for stopping later
        control_id = result.get('control_id')
        if control_id:
            redis_client = get_redis_client()
            redis_client.set(f'recording:{call.id}', control_id, ex=7200)  # 2 hour TTL

        emit_call_event(call.id, 'record', {
            'action': 'start',
            'agent': request.current_user.email,
            'control_id': control_id
        }, call.signalwire_call_sid)

        # Notify frontend of recording state
        from app.services.callcenter_socketio import emit_call_update
        emit_call_update(call)

        return jsonify({
            'success': True,
            'call_id': call.id,
            'recording': True,
            'control_id': control_id,
            'result': result
        }), 200
    except Exception as e:
        logger.error(f"Failed to start recording for call {call_id}: {str(e)}")
        return jsonify({'error': str(e)}), 500


@call_control_bp.route('/<call_id>/record/status', methods=['GET'])
@require_auth
def recording_status(call_id):
    """Return whether this call currently has an active manual recording.

    Reports the presence of the `recording:{call_id}` Redis key set by
    start_recording. Does not know about default SWML-level recording — the
    UI treats absence of a key as "not under manual control."
    """
    call = find_call(call_id)
    if not call:
        return jsonify({'error': 'Call not found'}), 404
    redis_client = get_redis_client()
    control_id = redis_client.get(f'recording:{call.id}') if redis_client else None
    return jsonify({
        'active': bool(control_id),
        'control_id': control_id,
        'recording_url': call.recording_url,
    }), 200


@call_control_bp.route('/<call_id>/record/stop', methods=['POST'])
@require_auth
@require_permission('can_control_recording')
def stop_recording(call_id):
    """Stop recording an active call."""
    call = find_call(call_id)
    if not call:
        return jsonify({'error': 'Call not found'}), 404

    # ISO-5: ownership gate (see start_recording).
    owner_check = _require_call_ownership(call, request.current_user)
    if owner_check:
        return owner_check

    try:
        # Get control_id from Redis
        redis_client = get_redis_client()
        control_id = redis_client.get(f'recording:{call.id}')

        sw_api = get_signalwire_api()
        result = sw_api.stop_recording(call.signalwire_call_sid, control_id)

        # Clean up Redis
        redis_client.delete(f'recording:{call.id}')

        emit_call_event(call.id, 'record', {
            'action': 'stop',
            'agent': request.current_user.email
        }, call.signalwire_call_sid)

        from app.services.callcenter_socketio import emit_call_update
        emit_call_update(call)

        return jsonify({'success': True, 'call_id': call.id, 'recording': False, 'result': result}), 200
    except Exception as e:
        logger.error(f"Failed to stop recording for call {call_id}: {str(e)}")
        return jsonify({'error': str(e)}), 500


# ==================== Live Translate Endpoints ====================

@call_control_bp.route('/<call_id>/translate/start', methods=['POST'])
@require_auth
def start_translate(call_id):
    """Start (or change) bidirectional live_translate on a call's customer leg.

    Body: { "from_lang": "es-ES", "to_lang": "en-US" }
    If translation is already active, this updates the language pair without restarting.
    """
    call = find_call(call_id)
    if not call:
        return jsonify({'error': 'Call not found'}), 404

    # ISO-5: ownership gate — live_translate plays audio into the customer
    # leg, so restrict to the assigned agent (or supervisor/admin).
    owner_check = _require_call_ownership(call, request.current_user)
    if owner_check:
        return owner_check

    data = request.get_json() or {}
    from_lang = (data.get('from_lang') or call.caller_language or 'en-US').strip()
    to_lang = (data.get('to_lang') or 'en-US').strip()

    if from_lang == to_lang:
        return jsonify({'error': 'from_lang and to_lang must differ'}), 400

    redis_client = get_redis_client()
    already_active = redis_client.get(f'translate:{call.id}') if redis_client else None

    try:
        sw_api = get_signalwire_api()
        if already_active:
            # live_translate has no `update` action per the SWML docs — only
            # start, stop, summarize, inject. To change languages mid-call we
            # stop the existing session and start a fresh one with the new pair.
            # Brief audio gap is acceptable; a silent no-op from an invented
            # `update` action is not.
            try:
                sw_api.stop_live_translate(call.signalwire_call_sid)
            except Exception as stop_err:
                # If stop fails because no session actually exists server-side
                # (Redis out of sync), log and proceed — start will error clearly
                # if the session is genuinely alive.
                logger.warning(
                    f"stop_live_translate before restart failed on call {call.id}: {stop_err}"
                )
            action = 'updated'
        else:
            action = 'started'

        result = sw_api.start_live_translate(
            call.signalwire_call_sid,
            from_lang=from_lang,
            to_lang=to_lang,
        )

        # Mark translation as on so the UI + future toggles know the state
        if redis_client:
            redis_client.setex(f'translate:{call.id}',
                               7200,
                               json.dumps({'from_lang': from_lang, 'to_lang': to_lang}))

        # Persist on the Call so other agents (takeovers, supervisors) see it
        call.caller_language = from_lang
        call.needs_translation = True
        db.session.commit()

        emit_call_event(call.id, 'translate', {
            'action': action,
            'from_lang': from_lang,
            'to_lang': to_lang,
            'agent': request.current_user.email,
        }, call.signalwire_call_sid)

        return jsonify({
            'success': True,
            'call_id': call.id,
            'action': action,
            'from_lang': from_lang,
            'to_lang': to_lang,
            'result': result,
        }), 200
    except Exception as e:
        logger.error(f"Failed to start translate for call {call_id}: {str(e)}")
        return jsonify({'error': str(e)}), 500


@call_control_bp.route('/<call_id>/translate/stop', methods=['POST'])
@require_auth
def stop_translate(call_id):
    """Stop live_translate on a call's customer leg."""
    call = find_call(call_id)
    if not call:
        return jsonify({'error': 'Call not found'}), 404

    # ISO-5: ownership gate (see start_translate).
    owner_check = _require_call_ownership(call, request.current_user)
    if owner_check:
        return owner_check

    try:
        sw_api = get_signalwire_api()
        result = sw_api.stop_live_translate(call.signalwire_call_sid)

        redis_client = get_redis_client()
        if redis_client:
            redis_client.delete(f'translate:{call.id}')

        call.needs_translation = False
        db.session.commit()

        emit_call_event(call.id, 'translate', {
            'action': 'stopped',
            'agent': request.current_user.email,
        }, call.signalwire_call_sid)

        return jsonify({'success': True, 'call_id': call.id, 'result': result}), 200
    except Exception as e:
        logger.error(f"Failed to stop translate for call {call_id}: {str(e)}")
        return jsonify({'error': str(e)}), 500


@call_control_bp.route('/<call_id>/translate/status', methods=['GET'])
@require_auth
def translate_status(call_id):
    """Get current translation state for a call."""
    call = find_call(call_id)
    if not call:
        return jsonify({'error': 'Call not found'}), 404

    redis_client = get_redis_client()
    raw = redis_client.get(f'translate:{call.id}') if redis_client else None
    state = json.loads(raw) if raw else None

    return jsonify({
        'active': state is not None,
        'from_lang': state.get('from_lang') if state else None,
        'to_lang': state.get('to_lang') if state else None,
        'caller_language': call.caller_language,
        'needs_translation': call.needs_translation,
    }), 200


@call_control_bp.route('/<call_id>/dtmf', methods=['POST'])
@require_auth
def send_dtmf(call_id):
    """Send DTMF tones into an active call.

    RE-AUDIT-04 (2026-06-03): ownership gate. DTMF tones are audible
    on the customer's line and can interact with IVR menus the agent
    might transfer to — cross-agent abuse is real. Restricted to the
    assigned agent + supervisor/admin.
    """
    call = find_call(call_id)
    if not call:
        return jsonify({'error': 'Call not found'}), 404

    owner_check = _require_call_ownership(call, request.current_user)
    if owner_check:
        return owner_check

    data = request.get_json() or {}
    digits = data.get('digits', '')

    # Validate digits
    import re
    if not re.match(r'^[0-9*#]+$', digits):
        return jsonify({'error': 'Invalid DTMF digits. Only 0-9, *, # are allowed.'}), 400

    try:
        sw_api = get_signalwire_api()
        result = sw_api.send_dtmf(call.signalwire_call_sid, digits)

        emit_call_event(call.id, 'dtmf', {
            'digits': digits,
            'agent': request.current_user.email
        }, call.signalwire_call_sid)

        return jsonify({'success': True, 'call_id': call.id, 'digits': digits, 'result': result}), 200
    except Exception as e:
        logger.error(f"Failed to send DTMF to call {call_id}: {str(e)}")
        return jsonify({'error': str(e)}), 500


# ==================== Monitoring Endpoints ====================

@call_control_bp.route('/<call_id>/monitor/start', methods=['POST'])
@require_auth
def start_monitor(call_id):
    """Start monitoring an active call (audio tap or silent conference join).

    For AI calls (non-conference): uses SignalWire tap to stream audio via WebSocket.
    For human calls (conference-based): prepares a silent conference join.

    Permission: `can_listen_ai_calls` or `can_listen_human_calls` depending on
    who is handling the call. Agents must never be able to silently observe
    arbitrary calls they're not on; gated here before any SignalWire RPC.
    """
    call = find_call(call_id)
    if not call:
        return jsonify({'error': 'Call not found'}), 404

    user = request.current_user

    # Pick the right permission based on what kind of call this is.
    # A human-handled conference call requires `can_listen_human_calls`;
    # anything else (AI-driven, tap-based) requires `can_listen_ai_calls`.
    is_human_call = bool(call.conference_name and call.handler_type == 'human')
    required_flag = 'can_listen_human_calls' if is_human_call else 'can_listen_ai_calls'
    if not user.has_permission(required_flag):
        return jsonify({
            'error': 'Missing required permissions',
            'required_permissions': [required_flag],
            'missing_permissions': [required_flag],
            'call_type': 'human' if is_human_call else 'ai',
        }), 403

    # (Persona self-scope gone with the shared floor: the call lookup runs
    # under the tenancy auto-filter, so another workspace's call already
    # 404s before this point.)

    redis_client = get_redis_client()

    try:
        if call.conference_name and call.handler_type == 'human':
            # Conference-based call: prepare silent join
            import uuid
            token = str(uuid.uuid4())
            redis_data = {
                'agent_id': str(user.id),
                'conf': call.conference_name,
                'call_id': str(call.id),
                'type': 'monitor',
                'muted': 'true',
                'beep': 'false',
            }
            redis_client.set(f'conference_join:{token}', json.dumps(redis_data), ex=300)

            base_url = get_base_url()
            dial_address = f"{base_url}/api/conferences/agent-conference?token={token}"

            emit_call_event(call.id, 'monitor', {
                'action': 'start',
                'monitor_type': 'conference_silent_join',
                'agent': user.email
            }, call.signalwire_call_sid)

            return jsonify({
                'success': True,
                'monitor_type': 'conference',
                'dial_address': dial_address,
                'token': token,
                'conference_name': call.conference_name,
            }), 200
        else:
            # AI call or non-conference: use tap
            base_url = get_base_url()
            ws_url = base_url.replace('http://', 'ws://').replace('https://', 'wss://')
            # RE-AUDIT-03 fix (2026-06-03): the tap URL used to embed
            # ``call.id`` (DB int), so tap_relay's per-frame Socket.IO
            # emits used ``room=f'tap:{db_id}'``. But the join_tap
            # consumer handler keys the room off ``signalwire_call_sid``
            # (the only identifier the frontend has at room-join time).
            # Room-key mismatch → no supervisor ever received audio,
            # because no socket was ever in ``tap:{db_id}``. Listen was
            # silently dead for everyone. Use the sid in the URL so the
            # producer's emit room matches what join_tap joins.
            tap_uri = signed_tap_stream_url(ws_url, call.signalwire_call_sid)

            sw_api = get_signalwire_api()
            result = sw_api.tap_call(call.signalwire_call_sid, tap_uri, direction='both')

            # Store tap control_id (keyed by DB id for back-compat with
            # the stop endpoint's lookup pattern).
            control_id = result.get('control_id')
            if control_id:
                redis_client.set(f'tap:{call.id}:{user.id}', control_id, ex=7200)

            emit_call_event(call.id, 'monitor', {
                'action': 'start',
                'monitor_type': 'tap',
                'agent': user.email,
                'control_id': control_id
            }, call.signalwire_call_sid)

            return jsonify({
                'success': True,
                'monitor_type': 'tap',
                'control_id': control_id,
                'result': result,
            }), 200

    except Exception as e:
        logger.error(f"Failed to start monitoring call {call_id}: {str(e)}")
        return jsonify({'error': str(e)}), 500


@call_control_bp.route('/<call_id>/monitor/stop', methods=['POST'])
@require_auth
def stop_monitor(call_id):
    """Stop monitoring an active call.

    Permission-wise this is the tear-down half of start_monitor. We allow it
    whenever the user has EITHER listen permission — if they got in, they
    must be able to get out even if their role was narrowed mid-session.
    """
    call = find_call(call_id)
    if not call:
        return jsonify({'error': 'Call not found'}), 404

    user = request.current_user
    if not (user.has_permission('can_listen_ai_calls')
            or user.has_permission('can_listen_human_calls')):
        return jsonify({
            'error': 'Missing required permissions',
            'required_permissions': ['can_listen_ai_calls', 'can_listen_human_calls'],
        }), 403

    # (Persona self-scope gone — see start_monitor.)

    redis_client = get_redis_client()

    try:
        # Check for active tap
        control_id = redis_client.get(f'tap:{call.id}:{user.id}')
        if control_id:
            sw_api = get_signalwire_api()
            result = sw_api.stop_tap(call.signalwire_call_sid, control_id)
            redis_client.delete(f'tap:{call.id}:{user.id}')

            emit_call_event(call.id, 'monitor', {
                'action': 'stop',
                'monitor_type': 'tap',
                'agent': user.email
            }, call.signalwire_call_sid)

            return jsonify({'success': True, 'monitor_type': 'tap', 'result': result}), 200

        # For conference monitor, the agent just hangs up their Call Fabric connection
        emit_call_event(call.id, 'monitor', {
            'action': 'stop',
            'agent': user.email
        }, call.signalwire_call_sid)

        return jsonify({'success': True, 'monitor_type': 'conference', 'message': 'Disconnect your Call Fabric client to stop monitoring'}), 200

    except Exception as e:
        logger.error(f"Failed to stop monitoring call {call_id}: {str(e)}")
        return jsonify({'error': str(e)}), 500


# ==================== Multi-Agent Conferencing Endpoints ====================

@call_control_bp.route('/<call_id>/request-backup', methods=['POST'])
@require_auth
def request_backup(call_id):
    """Request a backup agent to join the current call's conference.

    Finds an available agent from the queue (excluding the requesting agent),
    and sends them a call assignment notification.
    """
    call = find_call(call_id)
    if not call:
        return jsonify({'error': 'Call not found'}), 404

    # ISO-5: ownership gate — request_backup pulls another agent into THIS
    # call's conference and fires a call assignment. Without ownership a
    # persona could drag agents into any conference and spam assignments.
    owner_check = _require_call_ownership(call, request.current_user)
    if owner_check:
        return owner_check

    if not call.conference_name:
        return jsonify({'error': 'Call must be in a conference to request backup'}), 400

    data = request.get_json() or {}
    queue_slug = data.get('queue_id') or call.queue_id or 'support'

    try:
        redis_client = get_redis_client()
        from app.services.queue_service import QueueService
        queue_service = QueueService(redis_client, workspace_id=call.workspace_id)

        # Get available agents excluding the requesting agent
        available = queue_service.get_available_agents(queue_slug)
        available = [a for a in available if str(a) != str(request.current_user.id)]

        if not available:
            return jsonify({'error': 'No agents available for backup'}), 404

        # Get queue for routing strategy
        from app.models.queue import Queue
        queue = Queue.query.filter_by(slug=queue_slug).first()
        strategy = queue.routing_strategy if queue else 'round_robin'
        skill_levels = {}
        if queue:
            from app.models.queue import QueueAgentAssignment
            assignments = QueueAgentAssignment.query.filter_by(queue_id=queue.id).all()
            skill_levels = {str(a.user_id): a.skill_level for a in assignments}

        # Match backup agent to the original caller's language when known
        agent_languages = queue_service.get_languages_for_agents(available)

        selected_agent_id = queue_service.select_agent(
            queue_slug, strategy, available, skill_levels,
            caller_language=call.caller_language,
            agent_languages=agent_languages,
        )

        if not selected_agent_id:
            return jsonify({'error': 'No suitable agent found'}), 404

        selected_user = User.query.get(int(selected_agent_id))
        if not selected_user:
            return jsonify({'error': 'Selected agent not found'}), 404

        # Create a backup leg record
        max_leg = db.session.query(db.func.max(CallLeg.leg_number)).filter_by(call_id=call.id).scalar() or 0
        backup_leg = CallLeg(
            call_id=call.id,
            leg_type='backup',
            leg_number=max_leg + 1,
            user_id=selected_user.id,
            status='connecting',
            conference_name=call.conference_name,
        )
        db.session.add(backup_leg)
        db.session.commit()

        # Emit assignment to the selected agent
        assignment_data = {
            'type': 'backup',
            'call': call.to_dict(include_contact=True),
            'requesting_agent': {
                'id': request.current_user.id,
                'name': request.current_user.name or request.current_user.email,
                'email': request.current_user.email,
            },
            'conference_name': call.conference_name,
            'leg_id': backup_leg.id,
            'call_db_id': call.id,
        }
        socketio.emit('call_assignment', assignment_data, room=str(selected_agent_id))

        emit_call_event(call.id, 'conference', {
            'action': 'backup_requested',
            'requesting_agent': request.current_user.email,
            'selected_agent': selected_user.email,
        }, call.signalwire_call_sid)

        return jsonify({
            'success': True,
            'selected_agent_id': selected_user.id,
            'selected_agent_name': selected_user.name or selected_user.email,
            'leg_id': backup_leg.id,
        }), 200

    except Exception as e:
        logger.error(f"Failed to request backup for call {call_id}: {str(e)}")
        return jsonify({'error': str(e)}), 500


@call_control_bp.route('/<call_id>/escalate', methods=['POST'])
@require_auth
def escalate_to_supervisor(call_id):
    """Escalate an active call to a supervisor.

    Finds an available supervisor/admin and sends them a call assignment notification.
    Supports whisper mode where the supervisor can only speak to the agent (not the customer).
    """
    call = find_call(call_id)
    if not call:
        return jsonify({'error': 'Call not found'}), 404

    # (Persona self-scope gone with the shared floor: cross-workspace
    # call ids don't resolve under the tenancy auto-filter, so escalation
    # can't be reached for another tenant's call.)

    if not call.conference_name:
        return jsonify({'error': 'Call must be in a conference to escalate'}), 400

    data = request.get_json() or {}
    whisper_mode = data.get('whisper', False)

    # Whisper mode = supervisor speaks one-way to the requesting agent.
    # Gated by the requesting user's `can_whisper` flag (it's the user opting
    # into the coach-audio shape). Non-whisper escalation = full participant
    # supervisor join and remains available to any authenticated agent.
    if whisper_mode and not request.current_user.has_permission('can_whisper'):
        return jsonify({
            'error': 'Missing required permissions',
            'required_permissions': ['can_whisper'],
            'missing_permissions': ['can_whisper'],
        }), 403

    try:
        # Find available supervisors and admins. Not widened to
        # SUPERVISORY_ROLES: this looks for someone OTHER than the caller
        # (User.id != current_user.id below), and a hosted workspace's only
        # 'visitor' is always the caller — adding the role would change nothing.
        candidates = User.query.filter(
            User.role.in_(['supervisor', 'admin']),
            User.is_active == True,
            User.id != request.current_user.id
        ).all()

        if not candidates:
            return jsonify({'error': 'No supervisors found in the system'}), 404

        # Check availability via Redis
        redis_client = get_redis_client()
        from app.services.queue_service import QueueService
        queue_service = QueueService(redis_client)
        available_ids = queue_service.get_available_agents()

        available_supervisors = [s for s in candidates if str(s.id) in available_ids]

        if not available_supervisors:
            # Fall back to all supervisors if none are "available" (they might not use queue system)
            available_supervisors = candidates

        # Pick first available
        supervisor = available_supervisors[0]

        # Create escalation leg
        max_leg = db.session.query(db.func.max(CallLeg.leg_number)).filter_by(call_id=call.id).scalar() or 0
        esc_leg = CallLeg(
            call_id=call.id,
            leg_type='supervisor',
            leg_number=max_leg + 1,
            user_id=supervisor.id,
            status='connecting',
            conference_name=call.conference_name,
        )
        db.session.add(esc_leg)
        db.session.commit()

        # Find the requesting agent's call SID for coach mode
        agent_call_sid = None
        if whisper_mode:
            agent_legs = CallLeg.query.filter_by(
                call_id=call.id,
                user_id=request.current_user.id,
                status='active'
            ).first()
            if agent_legs and agent_legs.signalwire_sid:
                agent_call_sid = agent_legs.signalwire_sid

        # Emit assignment to supervisor
        assignment_data = {
            'type': 'escalation',
            'call': call.to_dict(include_contact=True),
            'requesting_agent': {
                'id': request.current_user.id,
                'name': request.current_user.name or request.current_user.email,
                'email': request.current_user.email,
            },
            'conference_name': call.conference_name,
            'leg_id': esc_leg.id,
            'call_db_id': call.id,
            'whisper_mode': whisper_mode,
            'agent_call_sid': agent_call_sid,
        }
        socketio.emit('call_assignment', assignment_data, room=str(supervisor.id))

        emit_call_event(call.id, 'conference', {
            'action': 'escalation_requested',
            'requesting_agent': request.current_user.email,
            'supervisor': supervisor.email,
            'whisper_mode': whisper_mode,
        }, call.signalwire_call_sid)

        return jsonify({
            'success': True,
            'supervisor_id': supervisor.id,
            'supervisor_name': supervisor.name or supervisor.email,
            'leg_id': esc_leg.id,
            'whisper_mode': whisper_mode,
        }), 200

    except Exception as e:
        logger.error(f"Failed to escalate call {call_id}: {str(e)}")
        return jsonify({'error': str(e)}), 500


# ==================== Observer actions (whisper / barge) ====================
#
# Supervisor-INITIATED joins to a call the supervisor is NOT on — the
# other half of the observer surface next to monitor/start (Listen).
# Unlike escalate (agent asks, supervisor gets an assignment), here the
# observer acts directly: we mint the same Redis conference_join token
# prepare_conference_join uses, and the observer's browser dials the
# returned resource address via Call Fabric. The agent-conference SWML
# webhook (conferences.py) shapes the join from token['type']:
#   whisper → join_conference with coach=<agent leg SID>: the supervisor
#             hears the room but is heard ONLY by the agent.
#   barge   → full-participant join, silent entry (no beep).
# Conference-based (human-handled) calls only; AI calls have takeover.

def _mint_observer_join(call, user, mode, agent_call_sid=None):
    """Store observer-join params in Redis and return the dial payload."""
    import uuid
    token = str(uuid.uuid4())
    token_data = {
        'agent_id': str(user.id),
        'conf': call.conference_name,
        'call_id': str(call.id),
        'type': mode,
    }
    if agent_call_sid:
        token_data['agent_call_sid'] = agent_call_sid
    redis_client = get_redis_client()
    redis_client.setex(f'conference_join:{token}', 300, json.dumps(token_data))

    resource_address = os.getenv('AGENT_CONFERENCE_RESOURCE', '/public/agent-conference-swml')
    return {
        'success': True,
        'mode': mode,
        'token': token,
        'dial_address': f"{resource_address}?token={token}",
        'conference_name': call.conference_name,
    }


@call_control_bp.route('/<call_id>/observe/whisper', methods=['POST'])
@require_auth
@require_permission('can_whisper')
def observe_whisper(call_id):
    """Supervisor-initiated whisper: coach the agent on a call you're not on.

    Joins the call's conference with SignalWire's ``coach`` member shape —
    the supervisor hears everything, but their audio reaches only the
    agent's leg. Returns a ``dial_address`` the caller's browser dials via
    Call Fabric; hanging that call up ends the whisper.
    """
    call = find_call(call_id)
    if not call:
        return jsonify({'error': 'Call not found'}), 404
    if not call.conference_name:
        return jsonify({'error': 'Whisper requires a conference-based call'}), 400

    # Coach targeting needs the agent's live leg SID. Prefer the primary
    # human agent leg; a backup agent's leg is an acceptable stand-in.
    target_leg = (
        CallLeg.query.filter_by(call_id=call.id, status='active')
        .filter(CallLeg.leg_type.in_(['human_agent', 'backup']))
        .filter(CallLeg.signalwire_sid.isnot(None))
        .order_by(CallLeg.leg_number.asc())
        .first()
    )
    if not target_leg:
        return jsonify({
            'error': 'No active human agent leg to whisper to',
        }), 409

    try:
        payload = _mint_observer_join(
            call, request.current_user, 'whisper', target_leg.signalwire_sid)
        emit_call_event(call.id, 'conference', {
            'action': 'whisper_started',
            'supervisor': request.current_user.email,
        }, call.signalwire_call_sid)
        return jsonify(payload), 200
    except Exception as e:
        logger.error(f"Failed to prepare whisper for call {call_id}: {str(e)}")
        return jsonify({'error': str(e)}), 500


@call_control_bp.route('/<call_id>/observe/barge', methods=['POST'])
@require_auth
@require_permission('can_barge')
def observe_barge(call_id):
    """Supervisor-initiated barge: join a call you're not on with full audio.

    Full-participant conference join with silent entry (no beep). Returns a
    ``dial_address`` the caller's browser dials via Call Fabric; hanging
    that call up leaves the conference.
    """
    call = find_call(call_id)
    if not call:
        return jsonify({'error': 'Call not found'}), 404
    if not call.conference_name:
        return jsonify({'error': 'Barge requires a conference-based call'}), 400

    try:
        payload = _mint_observer_join(call, request.current_user, 'barge')
        emit_call_event(call.id, 'conference', {
            'action': 'barge_started',
            'supervisor': request.current_user.email,
        }, call.signalwire_call_sid)
        return jsonify(payload), 200
    except Exception as e:
        logger.error(f"Failed to prepare barge for call {call_id}: {str(e)}")
        return jsonify({'error': str(e)}), 500
