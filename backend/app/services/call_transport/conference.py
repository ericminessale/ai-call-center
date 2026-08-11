"""Conference transport — current behavior, re-homed.

This file is the canonical home for all conference-specific logic. M0 is
a re-home, not a rewrite: most public functions here delegate to existing
helpers in `queue_dispatch.py`, `signalwire_api.py`, `call_control.py`,
and `coach.py`. Subsequent passes can inline the implementations or leave
them as thin shims — whichever reads better.

The seam exists so that M1's `bridge.py` doesn't have to touch any of the
callsites — they all go through `call_transport.__init__`.
"""

import logging
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
    """Conference-mode ingress — delegates to the existing
    ``queue_dispatch.enqueue_and_build_swml`` which already implements the
    full conference lifecycle (Conference row creation, Redis enqueue,
    immediate-dispatch attempt, and caller-leg SWML: ``join_conference``
    when an agent was dispatched, otherwise a ``transfer`` into the SWML
    hold cycle that owns announcements + the hold timeout).
    """
    from app.services.queue_dispatch import enqueue_and_build_swml
    return enqueue_and_build_swml(
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
# Agent assignment notification
# ---------------------------------------------------------------------------

def notify_assigned_agent(*, call, agent, conference_name: str,
                          queue_slug: str,
                          context: Optional[Dict[str, Any]] = None) -> bool:
    """Conference-mode agent notification — delegates to the existing
    ``queue_dispatch.emit_call_assignment_to_agent`` which emits the
    Socket.IO ``call_assignment`` event. The caller-side pre-join
    announcement rides the caller's own SWML (entry greeting or hold-cycle
    join document), not REST play.
    """
    from app.services.queue_dispatch import emit_call_assignment_to_agent
    return emit_call_assignment_to_agent(
        call=call, agent=agent, conference_name=conference_name,
        queue_slug=queue_slug, context=context,
    )


# ---------------------------------------------------------------------------
# Per-call operations
# ---------------------------------------------------------------------------

def play_to_caller(call, *, tts: Optional[str] = None,
                   url: Optional[str] = None) -> Any:
    """Play TTS or media URL to the caller leg.

    Uses ``calling.play`` REST on the caller's call_id. Same in conference
    and bridge — no conference-specific routing is needed because we target
    the actual leg, not a conference participant.
    """
    from app.services.signalwire_api import get_signalwire_api
    api = get_signalwire_api()
    if tts is not None:
        return api.play_tts(call.signalwire_call_sid, tts)
    if url is not None:
        return api.play_audio(call.signalwire_call_sid, url)
    raise ValueError("play_to_caller requires either tts or url")


def hold_caller(call, *, by_agent) -> Any:
    """Conference hold = mute+deaf the agent's conference member.

    Caller stays in the conference. The hold endpoint in `call_control.py`
    has the full implementation (TTS announcement + mute_participant);
    here we're providing a stable callsite for callers that want to do this
    without going through the HTTP endpoint.

    M0 note: the existing /hold endpoint still has its own implementation
    and isn't refactored to call through here. That's M0's intentional
    minimum — see CALL_TRANSPORT.md migration plan. M1 can choose to
    consolidate.
    """
    raise NotImplementedError(
        "Conference hold currently goes through the /call-control/hold "
        "endpoint directly. Refactoring that to use this shim is M0c+."
    )


def unhold_caller(call, *, by_agent) -> Any:
    """Inverse of `hold_caller`. Same M0 note."""
    raise NotImplementedError(
        "Conference unhold currently goes through /call-control/resume."
    )


def send_dtmf(call, digits: str, *, target: str = 'caller') -> Any:
    """Conference-mode DTMF: not supported until SWML ships per-participant
    DTMF. The blocker per the dev's "in SWML but not done/exposed yet" note.

    Bridge mode (M1+) will use ``calling.send_digits`` on the appropriate
    leg directly.
    """
    raise NotImplementedError(
        f"DTMF send to {target!r} not supported in conference transport — "
        f"waiting on SWML per-participant DTMF verb."
    )


def attach_sidecar(*, call, agent, mode: str, queue_slug: str = '',
                   base_url: str = '') -> None:
    """Attach AI Coach sidecar — per-leg verb, transport-agnostic.

    Delegates to ``app.services.coach.attach_sidecar_to_call`` which already
    operates on the caller's call_id regardless of transport.
    """
    from app.services.coach import attach_sidecar_to_call
    attach_sidecar_to_call(
        call=call, agent=agent, mode=mode,
        queue_slug=queue_slug, base_url=base_url,
    )


def detach_sidecar(call) -> None:
    """Detach sidecar — per-leg, same as attach."""
    from app.services.coach import detach_sidecar_from_call
    detach_sidecar_from_call(call=call)


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------

def capabilities(call) -> set:
    """Capabilities supported on a conference-mode call.

    Hold is supported via the conference-participant mute+deaf workaround
    (not via REST `calling.hold` on a participant). DTMF send is blocked
    until SWML ships the per-participant variant.
    """
    return PER_LEG_BASE | {
        Capability.HOLD,
        Capability.UNHOLD,
        Capability.MONITOR_LISTEN,
        Capability.WHISPER,
        Capability.BARGE,
        Capability.TAKEOVER,
        Capability.TRANSFER,
        # DTMF_CALLER and DTMF_AGENT intentionally absent until SWML ships
        # per-participant DTMF in conferences. (Risk 7 unresolved.)
    }
