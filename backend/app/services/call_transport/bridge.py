"""Bridge transport — caller leg + agent leg in a two-leg bridge, no conference.

Dispatch architecture (post-2026-05-21 refactor):

The original design tried SWML ``connect: to: /private/<sub>`` at ingress
to dial the agent directly. That path hits a SignalWire JS SDK bug where
``invite.accept()`` on an inbound (SWML-connect-originated) invite never
fires ``verto.answer`` — the call appears to accept in the browser but
media never bridges. Documented in queues.py commit 816b10e: *"SDK has a
bug where connection pooling breaks inbound call answering... outbound
calls work fine."*

The fix is to never use SWML connect-to-agent. Instead, the agent leg is
always initiated as an OUTBOUND dial via REST ``calling.dial``:

  1. Caller arrives → SWML ``enter_queue`` parks them in the named queue
     (native SignalWire 2-leg-friendly hold)
  2. If an agent is available NOW, ``_push_dispatch_bridge_pickup`` fires
     a REST outbound dial to the agent's Fabric address. SDK invite from
     an outbound-originated call accepts cleanly (no bug)
  3. If no agent is available, the same dispatch runs later when an
     agent transitions to 'available' (existing push-dispatch hook in
     queue_service.set_agent_status)
  4. The dial's SWML target is ``/api/swml/queue-pickup/<slug>`` which
     does ``connect: queue:<slug>`` — pops the parked caller and bridges
     the two legs natively. End result is a 2-leg bridge, NOT a conf.

This is the canonical SignalWire pattern for "park + dispatch + bridge"
and uses native primitives (enter_queue, calling.dial, connect: queue).
Per-leg call controls (hold/play/send_digits/record on each leg) work
because the result is a real 2-leg bridge.
"""

import json
import logging
from datetime import datetime
from typing import Any, Dict, Optional

from .base import Capability, PER_LEG_BASE

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Ingress
# ---------------------------------------------------------------------------

def build_ingress_swml(
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
    """Bridge-mode ingress: park caller in ``enter_queue``, always dispatch via outbound dial.

    Single code path regardless of whether an agent is online at ingress:

      1. Enqueue in Redis + emit ``queue_update added`` for the dashboard
      2. Return SWML that puts the caller in SignalWire's native ``enter_queue``
      3. If an available agent exists right now, immediately fire the
         outbound-dial dispatch (``_push_dispatch_bridge_pickup``) — same
         dispatch the agent-becomes-available status hook already uses
      4. Outbound dial reaches the agent's WebRTC → SDK invite → Accept
         works (outbound-originated invites don't hit the inbound-accept
         SDK bug) → queue-pickup SWML runs on agent's leg → ``connect:
         queue:<slug>`` pops the parked caller → 2-leg bridge established

    See the module docstring for the SDK bug context. The agent-at-ingress
    "fast path" that returned ``connect: to: /private/<sub>`` SWML has been
    removed — it never worked through to a live media bridge.
    """
    from app.models import User
    from app.services.queue_service import QueueService
    from app.services.redis_service import get_redis_client
    from app.services.callcenter_socketio import emit_call_update

    call_sid = call.signalwire_call_sid
    qs = QueueService(get_redis_client(), workspace_id=call.workspace_id)

    # --- 1. Enqueue + announce to dashboard ---
    try:
        queue_result = qs.enqueue_call(
            call_id=call_sid,
            queue_id=queue_slug,
            priority=priority,
            context=context,
            caller_info={
                'number': call.from_number,
                'name': context.get('customer_name') if context else None,
            },
        )
        logger.info(
            f"Bridge ingress: enqueued {call_sid} in '{queue_slug}' → "
            f"position {queue_result.get('position', '?') if isinstance(queue_result, dict) else '?'}"
        )
    except Exception as e:
        logger.warning(f"Bridge ingress: enqueue failed (non-fatal): {e}")

    try:
        from app import socketio
        from app.services.ws_rooms import workspace_room
        socketio.emit('queue_update', {
            'call': call.to_dict(include_contact=True),
            'queue_id': queue_slug,
            'action': 'added',
        }, room=workspace_room(call.workspace_id))
    except Exception as e:
        logger.warning(f"Bridge ingress: queue_update emit failed: {e}")

    try:
        emit_call_update(call)
    except Exception as e:
        logger.warning(f"Bridge ingress: emit_call_update failed: {e}")

    # --- 2. Trigger immediate dispatch if an agent is available ---
    # _push_dispatch_bridge_pickup checks signalwire_address + agent status,
    # marks agent busy, and fires the outbound dial. We call it directly
    # (not via the status-transition hook) so that an already-available
    # agent at ingress time gets dispatched right away — no need to wait
    # for them to flip status. Best-effort; failures shouldn't stop the
    # SWML from returning (caller still gets queue parking, push-dispatch
    # will pick them up on the next available transition).
    try:
        available_agents = qs.get_available_agents(queue_slug)
        # Bridge transport gets the same language policy as conference. It
        # was reaching select_agent with the default (fallback allowed), so a
        # queue set to wait_only or wait_then_translate held out on the
        # conference path and connected a mismatched agent instantly here —
        # the same rule producing opposite behaviour depending on transport.
        from app.models import Queue as QueueModel
        from app.services.call_language import language_fallback_allowed
        queue_row = QueueModel.query.filter_by(
            slug=queue_slug, workspace_id=call.workspace_id,
        ).first()
        allow_fallback = language_fallback_allowed(queue_row, waited_seconds=0)
        if not agent_languages and available_agents:
            # Same gap as the conference path: an empty map makes
            # select_agent skip language preference altogether.
            try:
                agent_languages = qs.get_languages_for_agents(available_agents)
            except Exception:
                agent_languages = {}
        for candidate_id in (available_agents or []):
            # Reuse the same routing strategy the conference path uses.
            chosen = qs.select_agent(
                queue_slug=queue_slug,
                routing_strategy=routing_strategy,
                available_agents=[candidate_id],
                skill_levels=skill_levels or {},
                call_priority=priority,
                caller_language=caller_language,
                agent_languages=agent_languages or {},
                allow_language_fallback=allow_fallback,
            )
            if not chosen:
                continue
            try:
                user = User.query.filter_by(id=int(chosen)).first()
            except (ValueError, TypeError):
                user = User.query.filter_by(email=chosen).first()
            if not user or not user.signalwire_address:
                continue
            # Skip recently-declined pairings (decline cooldown — see
            # queue_service.mark_decline / has_recently_declined).
            if qs.has_recently_declined(str(user.id), call_sid):
                continue
            if qs._push_dispatch_bridge_pickup(str(user.id), queue_slug):
                logger.info(
                    f"Bridge ingress: immediate dispatch of {call_sid} to "
                    f"agent {user.id} via outbound dial"
                )
                break
    except Exception as e:
        logger.warning(f"Bridge ingress: immediate dispatch failed (non-fatal): {e}")

    # --- 3. SWML: park the caller in the native queue ---
    # `enter_queue` is the SignalWire-native primitive for 2-leg call
    # parking. Combined with the agent-side ``connect: queue:<slug>`` (see
    # /api/swml/queue-pickup), this provides matchmaking + bridging without
    # needing a conference. No wait_url specified → SignalWire plays its
    # default hold media (avoids the wait_url polling spam we hit before).
    # transfer_after_bridge is required by the schema; we point it at an
    # endpoint that returns a simple hangup SWML so the call ends cleanly
    # when the bridge completes.
    from app.utils.url_utils import signed_webhook_url
    transfer_after_bridge_url = signed_webhook_url(f"{base_url}/api/swml/end-call")
    status_url = signed_webhook_url(f"{base_url}/api/webhooks/queue-status")

    main_section: list = [
        {"play": "say:Thanks for calling. Please hold while we connect you with an agent."},
        {
            "enter_queue": {
                "queue_name": queue_slug,
                "transfer_after_bridge": transfer_after_bridge_url,
                "status_url": status_url,
                "wait_time": 1800,  # 30-minute max hold
            }
        },
        "hangup",
    ]

    logger.info(
        f"Bridge ingress: call {call_sid} parked in queue '{queue_slug}' "
        f"(dispatch via outbound dial)"
    )
    return {"version": "1.0.0", "sections": {"main": main_section}}


# ---------------------------------------------------------------------------
# Agent notification
# ---------------------------------------------------------------------------

def notify_assigned_agent(*, call, agent, conference_name: str,
                          queue_slug: str,
                          context: Optional[Dict[str, Any]] = None) -> bool:
    """Emit the Socket.IO ``call_assignment`` event for UI parity.

    The SWML ``connect`` already triggers the agent's SDK invite — that's
    the real dispatch. This event just hands the banner UI the queue /
    caller / context metadata it expects (Risk 3 Option A).

    Returns True on emit. Best-effort: failures here don't break dispatch.
    """
    from app import socketio
    if not agent or not call:
        logger.warning(
            "Bridge notify_assigned_agent skipped: missing agent or call"
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
        # Bridge mode has no conference; include empty string so the existing
        # frontend handler doesn't choke on a missing key.
        'conference_name': '',
        'agent_call_sid': None,
        'customer_info': {
            'phone': call.from_number,
            'name': context.get('customer_name'),
            'contact_id': call.contact_id,
        },
        # Signal to the frontend that this assignment is bridge-mode, in case
        # any UI affordance wants to act on it (e.g. hide "Listen" preemptively
        # without waiting for the call to materialize).
        'transport': 'bridge',
    }
    try:
        socketio.emit('call_assignment', payload, room=str(agent.id))
        logger.info(
            f"Bridge dispatched call {call.signalwire_call_sid} → "
            f"agent {agent.id} ({agent.email}) via SDK invite"
        )
        return True
    except Exception as e:
        logger.warning(f"Bridge notify_assigned_agent emit failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Per-call operations
# ---------------------------------------------------------------------------

def hold_caller(call, *, by_agent) -> Any:
    """Bridge hold = REST ``calling.hold`` on the caller's leg.

    Risk 7 from CALL_TRANSPORT.md — synthetic probe was inconclusive.
    First live bridge call will tell us if this works as-is or if SignalWire
    rejects the command on a non-AI leg. Error from `_call_command` is
    logged and bubbles up; the /hold endpoint catches it and reports
    cleanly to the operator.
    """
    from app.services.signalwire_api import get_signalwire_api
    api = get_signalwire_api()
    return api.hold_call(call.signalwire_call_sid)


def unhold_caller(call, *, by_agent) -> Any:
    """Bridge unhold = REST ``calling.unhold`` on the caller's leg."""
    from app.services.signalwire_api import get_signalwire_api
    api = get_signalwire_api()
    return api.unhold_call(call.signalwire_call_sid)


def send_dtmf(call, digits: str, *, target: str = 'caller') -> Any:
    """Bridge DTMF: ``calling.send_digits`` REST on the chosen leg.

    `target='caller'` sends to the caller leg (e.g. for navigating an IVR
    after a transfer). `target='agent'` would target the agent leg — we'd
    need to look up the agent leg's call_sid from CallLeg rows; not in M1
    MVP.
    """
    from app.services.signalwire_api import get_signalwire_api
    api = get_signalwire_api()
    if target == 'caller':
        return api.send_digits(call.signalwire_call_sid, digits)
    raise NotImplementedError(
        f"DTMF target={target!r} not implemented in M1 (only 'caller')"
    )


def attach_sidecar(*, call, agent, mode: str, queue_slug: str = '',
                   base_url: str = '') -> None:
    """Same as conference — sidecar operates on the caller's call_id."""
    from app.services.coach import attach_sidecar_to_call
    attach_sidecar_to_call(
        call=call, agent=agent, mode=mode,
        queue_slug=queue_slug, base_url=base_url,
    )


def detach_sidecar(call) -> None:
    """Same as conference."""
    from app.services.coach import detach_sidecar_from_call
    detach_sidecar_from_call(call=call)


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------

def capabilities(call) -> set:
    """Capabilities supported on a bridge-mode call.

    Per-leg verbs (hold/unhold, DTMF to caller, recording, transcription,
    sidecar) work directly. Multi-party (listen/whisper/barge/takeover/
    transfer) requires promote-to-conference, which is M4 — not in MVP, so
    those capabilities are intentionally absent.
    """
    return PER_LEG_BASE | {
        Capability.HOLD,
        Capability.UNHOLD,
        Capability.SEND_DTMF_CALLER,
        # SEND_DTMF_AGENT, MONITOR_LISTEN, WHISPER, BARGE, TAKEOVER, TRANSFER
        # all defer to M4 (promote-to-conference) or out-of-scope.
    }
