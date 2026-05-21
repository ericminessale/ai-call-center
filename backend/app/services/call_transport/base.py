"""Call transport — shared types.

`Capability` is the truth table the UI gates on. Add a new capability here
and you have to declare which transports support it via the
`capabilities(call)` method on each implementation. That's the lint rule
this design enforces: a feature can't ship with "supported in conference
but I forgot bridge" because the capability set tells the UI what to hide.
"""

from enum import Enum


class Capability(str, Enum):
    """Per-call capabilities, transport-specific.

    Values are strings so they serialize cleanly across the Socket.IO /
    JSON wire to the frontend.
    """

    # Audio control on the caller leg
    HOLD = 'hold'
    UNHOLD = 'unhold'

    # DTMF — direction-aware. Caller-leg DTMF works in bridge; conference
    # variant pending in SWML.
    SEND_DTMF_CALLER = 'send_dtmf_caller'
    SEND_DTMF_AGENT = 'send_dtmf_agent'

    # Recording — per-leg works in both transports today. Conference also
    # offers a conference-level recording surface.
    RECORD_START = 'record_start'
    RECORD_STOP = 'record_stop'

    # Multi-party observation. Conference natively supports these via mute/
    # deaf flags on extra participants. Bridge has to promote to conference
    # before any of these light up.
    MONITOR_LISTEN = 'monitor_listen'
    WHISPER = 'whisper'
    BARGE = 'barge'

    # Lifecycle handoffs.
    TAKEOVER = 'takeover'      # AI → human takeover mid-call
    TRANSFER = 'transfer'      # human → another human or queue

    # Per-leg verbs that work everywhere.
    LIVE_TRANSLATE = 'live_translate'
    SIDECAR_COACH = 'sidecar_coach'


# Common capability sets — modules can extend.

PER_LEG_BASE = {
    Capability.RECORD_START,
    Capability.RECORD_STOP,
    Capability.LIVE_TRANSLATE,
    Capability.SIDECAR_COACH,
}
