"""Per-call cost estimation at published list rates (IMP-01).

The point is transparency: SignalWire's bundled pricing is simple enough
to compute from the Call row alone — AI runtime is one all-in per-minute
rate, with voice transport, conferencing, and recording as separate
published line items. Rates live in SystemConfig (``pricing.*``) so the
math is shown, not hidden; defaults below are published list rates as of
2026-06 (signalwire.com/pricing).

Always an ESTIMATE: durations are prorated to the second, and per-span
attribution (translate/sidecar windows, AI→human handoff splits) is out
of scope for v1 — a call is priced by its final handler_type.
"""

import time
from typing import Optional

_RATE_DEFAULTS = {
    'ai_runtime_per_min': 0.16,       # AI agent runtime, all-in STT+LLM+TTS
    'voice_inbound_per_min': 0.0066,  # local PSTN (10DLC) inbound
    'voice_outbound_per_min': 0.008,  # local PSTN (10DLC) outbound
    'webrtc_per_min': 0.003,          # browser agent leg
    'conference_per_participant_min': 0.0018,
    'recording_per_min': 0.002,
    'did_monthly': 0.50,              # per-number monthly, day-summary line only
}

# Tenancy: pricing.* resolves per workspace now (SystemConfig layering),
# so the 30s cache is keyed by the current workspace id — one entry per
# tenant, plus the None/global entry for unscoped contexts. Bounded reset
# guards against unbounded tenant churn.
_cache: dict = {}
_TTL_SECONDS = 30.0
_CACHE_MAX_ENTRIES = 1024


def get_rates() -> dict:
    """Current rates: SystemConfig ``pricing.*`` overrides over list-rate
    defaults, resolved for the current workspace (global fallback).
    Cached ~30s per workspace so Call.to_dict can call this per row
    without a DB read per key per call."""
    try:
        from app.tenancy import current_workspace_id
        cache_key = current_workspace_id()
    except Exception:
        cache_key = None

    now = time.monotonic()
    hit = _cache.get(cache_key)
    if hit is not None and now - hit['at'] < _TTL_SECONDS:
        return hit['rates']

    rates = {}
    try:
        from app.models.system_config import SystemConfig
        for name, default in _RATE_DEFAULTS.items():
            raw = SystemConfig.get(f'pricing.{name}')
            try:
                rates[name] = float(raw) if raw is not None else default
            except (TypeError, ValueError):
                rates[name] = default
    except Exception:
        rates = dict(_RATE_DEFAULTS)

    if len(_cache) >= _CACHE_MAX_ENTRIES:
        _cache.clear()
    _cache[cache_key] = {'rates': rates, 'at': now}
    return rates


def estimate_call_cost(call) -> Optional[dict]:
    """Estimated platform cost for one call, with line items.

    Returns None for calls that never carried audio (no duration).
    """
    duration = getattr(call, 'duration', None)
    if not duration or duration <= 0:
        return None

    rates = get_rates()
    minutes = duration / 60.0
    lines = []

    if (call.direction or 'outbound') == 'inbound':
        lines.append(('Voice — PSTN inbound', rates['voice_inbound_per_min'] * minutes))
    else:
        lines.append(('Voice — PSTN outbound', rates['voice_outbound_per_min'] * minutes))

    if call.handler_type == 'ai':
        lines.append(('AI agent runtime', rates['ai_runtime_per_min'] * minutes))
    else:
        lines.append(('Agent leg — WebRTC', rates['webrtc_per_min'] * minutes))
        # Conference transport mixes caller + agent legs in a conference.
        if (call.transport or 'conference') == 'conference':
            lines.append((
                'Conference audio — 2 participants',
                rates['conference_per_participant_min'] * 2 * minutes,
            ))

    if call.recording_url:
        lines.append(('Call recording', rates['recording_per_min'] * minutes))

    total = sum(amount for _, amount in lines)
    return {
        'total': round(total, 4),
        'currency': 'USD',
        'minutes': round(minutes, 2),
        'handler_type': call.handler_type,
        'lines': [
            {'label': label, 'amount': round(amount, 4)}
            for label, amount in lines
        ],
        'disclaimer': 'Estimated at published list rates',
    }


def estimate_cost_total(call) -> Optional[float]:
    """Just the number, for embedding in Call.to_dict. None when n/a."""
    estimate = estimate_call_cost(call)
    return estimate['total'] if estimate else None
