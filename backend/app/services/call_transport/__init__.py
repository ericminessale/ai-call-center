"""Call transport abstraction — see CALL_TRANSPORT.md.

Every per-call operation that *might* differ between transports goes through
this module. Anything outside `call_transport/` should NOT reference
conference primitives directly; it should call through the public API here,
which dispatches on `call.transport`.

Two transports today:
  - 'conference' — caller in interaction-{sid} conference, agent joins. The
                   current behavior. Multi-party support (whisper / barge /
                   monitor) lives here.
  - 'bridge'     — caller leg + agent leg in a regular two-leg bridge.
                   Per-leg REST verbs operate directly. No multi-party
                   without promotion to conference (not implemented in MVP).

Ingress resolves the queue's ``routing_transport`` preference and persists the
actual transport on the Call. Every later call-control operation dispatches
from that per-call value.
"""

import logging
from typing import Any, Dict, Optional

from .base import Capability  # re-export for callers


logger = logging.getLogger(__name__)

# Lazy imports inside functions to avoid circular-import problems with
# models/services at app startup. The conference impl pulls in Conference,
# Call, signalwire_api, etc. — all of which sometimes import services back.


# ---------------------------------------------------------------------------
# Ingress (called from queues.py /route + /direct-inbound)
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
    """Build the SWML that handles a caller arriving at a queue.

    Conference: join the interaction-{sid} conference when an agent is
    dispatched immediately; otherwise park the caller in the SWML hold
    cycle (announcements + hold timeout), which joins them to the
    conference at the cycle boundary after a dispatch fires.

    Bridge: park the caller with SignalWire's native ``enter_queue`` verb and
    connect the selected agent through a regular two-leg bridge.
    """
    from app.models import Queue

    # Decide transport from the queue preference. Both implementations can
    # park a waiting caller, so the selected mode remains stable for the call.
    transport = _resolve_transport(call, queue_slug)

    if transport == 'bridge':
        from . import bridge as _impl
    else:
        from . import conference as _impl

    # Persist the transport choice on the Call row so every later operation
    # routes through the right implementation.
    if call.transport != transport:
        call.transport = transport
        from app import db
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()

    # Both transports write the same operational queue timeline. Keep this
    # above the implementation fork so bridge and conference analytics have
    # identical semantics even though their SignalWire primitives differ.
    try:
        from app import db
        from app.services.interaction_timeline import record_queue_entered
        record_queue_entered(
            call,
            queue_slug,
            priority=priority,
            routing_strategy=routing_strategy,
        )
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        logger.warning(
            "Queue timeline entry failed for call %s: %s",
            call.id, exc,
        )

    return _impl.build_ingress_swml(
        call=call,
        queue_slug=queue_slug,
        context=context,
        base_url=base_url,
        routing_strategy=routing_strategy,
        caller_language=caller_language,
        agent_languages=agent_languages,
        skill_levels=skill_levels,
        priority=priority,
        start_live_transcribe=start_live_transcribe,
    )


# ---------------------------------------------------------------------------
# Agent assignment + caller-leg announcement
# ---------------------------------------------------------------------------

def notify_assigned_agent(*, call, agent, conference_name: str, queue_slug: str,
                          context: Optional[Dict[str, Any]] = None) -> bool:
    """Tell the agent's frontend they have an incoming call.

    Both transports emit the Socket.IO `call_assignment` event today (see
    Risk 3 in CALL_TRANSPORT.md). Native SDK invite is the dispatch in both
    cases; `call_assignment` is side-channel context for the banner UI.
    """
    impl = _impl_for(call)
    return impl.notify_assigned_agent(
        call=call, agent=agent, conference_name=conference_name,
        queue_slug=queue_slug, context=context,
    )


# ---------------------------------------------------------------------------
# Per-call operations (hold / play / record / sidecar / transcription)
#
# Most of these are AGN/PER-CALL — they operate on call legs the same way in
# both transports. We surface them here anyway so callers go through a single
# import path, and so M1 can override hold (and DTMF later) without callsite
# changes.
# ---------------------------------------------------------------------------

def play_to_caller(call, *, tts: Optional[str] = None, url: Optional[str] = None) -> Any:
    """Play TTS or a media URL to the caller leg. Same in both transports."""
    impl = _impl_for(call)
    return impl.play_to_caller(call, tts=tts, url=url)


def hold_caller(call, *, by_agent) -> Any:
    """Place the caller on hold.

    Conference: mute+deaf the agent's conference member (caller stays in the
    conference; can play hold music to remaining members).

    Bridge (M1+): `calling.hold` REST on the caller leg.
    """
    impl = _impl_for(call)
    return impl.hold_caller(call, by_agent=by_agent)


def unhold_caller(call, *, by_agent) -> Any:
    """Resume from hold. Inverse of `hold_caller`."""
    impl = _impl_for(call)
    return impl.unhold_caller(call, by_agent=by_agent)


def send_dtmf(call, digits: str, *, target: str = 'caller') -> Any:
    """Send DTMF digits into a call leg. `target` is 'caller' or 'agent'.

    Bridge: `calling.send_digits` REST on the chosen leg.
    Conference: not supported until SWML ships per-participant DTMF.
    """
    impl = _impl_for(call)
    return impl.send_dtmf(call, digits, target=target)


def attach_sidecar(call, agent, *, mode: str, queue_slug: str = '',
                   base_url: str = '') -> None:
    """Attach the AI Coach sidecar to the caller leg with the given mode.

    Per-leg verb — same in both transports.
    """
    impl = _impl_for(call)
    return impl.attach_sidecar(
        call=call, agent=agent, mode=mode,
        queue_slug=queue_slug, base_url=base_url,
    )


def detach_sidecar(call) -> None:
    """Detach the sidecar from the caller leg. Per-leg, same in both."""
    impl = _impl_for(call)
    return impl.detach_sidecar(call)


# ---------------------------------------------------------------------------
# Capabilities — for UI gating
# ---------------------------------------------------------------------------

def capabilities(call) -> set:
    """Return the set of `Capability` values supported for this call.

    UI surfaces consume this via the `useCallCapabilities(call)` hook on the
    frontend (added in M2). Buttons whose capability isn't in the set hide.
    """
    impl = _impl_for(call)
    return impl.capabilities(call)


# ---------------------------------------------------------------------------
# Internal: dispatch helpers
# ---------------------------------------------------------------------------

def _impl_for(call):
    """Pick the concrete transport implementation for this Call.

    `call.transport` is set once at ingress (in `build_ingress_swml`) and never
    changes. Defaults to 'conference' for any call that somehow lacks the
    column (e.g. backfill, manual DB inserts).
    """
    transport = getattr(call, 'transport', None) or 'conference'
    if transport == 'bridge':
        # M1 will register bridge.py. Until then, fall back to conference so
        # an accidental 'bridge' value can't break a live call.
        try:
            from . import bridge as _bridge  # noqa: F401
            return _bridge
        except ImportError:
            from . import conference as _conf
            return _conf
    from . import conference as _conf
    return _conf


def _resolve_transport(call, queue_slug: str) -> str:
    """Decide what transport this call should use, at ingress time.

    Reads the queue's ``routing_transport`` preference. Two transports:
      - 'conference' (default) — caller parked in interaction-{sid}
        conference, agent joins via WebRTC join_conference.
      - 'bridge' — SWML ``connect`` dials the agent directly. Two legs in
        a bridge, no conference.

    Risk 1 fallback (no agent available → use conference parking) is
    NOT handled here — it happens inside ``bridge.build_ingress_swml``
    which delegates back to conference when ``select_agent`` returns
    empty. Keeping the fallback inside bridge.py means this resolver
    stays pure data lookup; the runtime decision lives next to the
    agent selection code that actually knows the state.
    """
    if not queue_slug:
        return 'conference'
    try:
        from app.models import Queue
        q = Queue.query.filter_by(slug=queue_slug, is_active=True).first()
        if q and (q.routing_transport or 'conference') == 'bridge':
            return 'bridge'
    except Exception:
        # If the queue lookup fails (DB hiccup at ingress time), be safe and
        # use conference — that's the well-tested path.
        pass
    return 'conference'
