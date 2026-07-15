"""
Phone-number verification for hosted-demo workspaces (§6.2, re-keyed Phase 4).

Binds an anonymous visitor's WORKSPACE to a real phone number they control.
The binding is the telephony attribution invariant (§10.1, verify-first):
inbound calls on the shared DIDs are accepted ONLY from verified numbers and
land in the bound workspace; outbound may dial ONLY that same number. Total
telephony surface per workspace = the visitor calling themselves.

Verification method: an **inbound pairing code**, not outbound OTP. The
visitor's dashboard shows a random 6-digit code and asks them to TEXT it to
the demo number; the inbound-SMS webhook (``/api/webhooks/sms-inbound``)
matches the code and binds the sender's number. Receiving an MO message
requires no messaging campaign (10DLC/A2P gates outbound application
traffic, and we never reply), it costs nothing to operate, and the SMS
sender number is a stronger possession proof than voice caller-ID.

Storage layout in Redis (workspace INT id; values carry the code-creator's
user id so inbound attribution can stamp ``Call.user_id``):

    demo:verify:code:<CODE>     → "<ws_id>:<user_id>"  (one-time, code TTL)
    demo:verify:number:<E164>   → "<ws_id>:<user_id>"  (binding, workspace TTL)
    ws:<id>:verify_code         → "<CODE>"             (reverse, for display/clear)
    ws:<id>:verify_number       → "<E164>"             (reverse)

Binding TTL = the workspace TTL (7 days by default, §6.2 — decoupled from
the old 5-minute persona lease), refreshed by the demo heartbeat. The
number is also mirrored into ``workspaces.verified_number`` for recovery /
operator visibility. The reverse keys live under ``ws:{id}:`` so the
workspace reaper's pattern delete clears them; :func:`clear_bindings`
clears both directions explicitly on release/reap.

``get_workspace_for_number`` additionally confirms the workspace row is
still live, so a binding that outlives its workspace never grants access.

(Pre-Phase-4 this module was keyed by the visitor's user id and included a
retroactive call-attribution sweep for "verify while already on a call".
Verify-first rejection means no pre-verification calls can exist, so the
sweep is gone; old ``demo:verify:persona_*`` keys are inert residue.)
"""

from __future__ import annotations

import logging
import re
import secrets
from typing import Optional, Tuple

from app.services.redis_service import get_redis_client

logger = logging.getLogger(__name__)


def _binding_ttl_seconds() -> int:
    """Verified-number binding lifetime = the workspace lifetime (§6.2).

    Refreshed on the demo heartbeat and re-asserted on every re-pair, so a
    live workspace's binding never lapses before the workspace does.
    """
    from app.services.workspace_session import workspace_ttl_seconds
    return workspace_ttl_seconds()


# Codes are one-shot, secrets-grade random, and attempt-capped at the SMS
# webhook — an hour on screen is convenience, not risk. (Decoupled from the
# binding TTL now that bindings live as long as the workspace.)
_CODE_TTL_SECONDS = 3600


def _workspace_live(workspace_id) -> bool:
    """Fail-closed liveness for a workspace int id."""
    try:
        from app.models import Workspace
        from app import db
        ws = db.session.get(Workspace, int(workspace_id))
        return bool(ws is not None and ws.is_live())
    except Exception:
        return False


def _norm(number: Optional[str]) -> Optional[str]:
    """Canonical E.164-ish form for keying/compare: leading '+' + digits only.

    Accepts the shapes SignalWire and the frontend hand us ('+1 555 123 4567',
    '+15551234567', '15551234567') and collapses them to '+15551234567'.
    Returns None for anything without at least a plausible national number.
    """
    if not number:
        return None
    digits = re.sub(r'[^0-9]', '', str(number))
    if len(digits) < 7:  # too short to be a real dialable number
        return None
    # If it already had a leading +, or looks like it needs a country code,
    # just prefix '+'. We don't guess country codes — exact-match semantics
    # mean both sides normalize the same way, which is all we need.
    return '+' + digits


def mask_number(number: Optional[str]) -> Optional[str]:
    """Human-facing masked form, e.g. '••• ••• 4567'. Last 4 shown."""
    norm = _norm(number)
    if not norm:
        return None
    tail = norm[-4:]
    return f'••• ••• {tail}'


def _code_key(code: str) -> str:
    return f'demo:verify:code:{code}'


def _number_key(norm_number: str) -> str:
    return f'demo:verify:number:{norm_number}'


def _ws_code_key(workspace_id: int) -> str:
    from app.services.ws_rooms import ws_key
    return ws_key(workspace_id, 'verify_code')


def _ws_number_key(workspace_id: int) -> str:
    from app.services.ws_rooms import ws_key
    return ws_key(workspace_id, 'verify_number')


def _outbound_key(workspace_id: int) -> str:
    """Outbound-cap counter, ``ws:{id}:outbound`` (§4.3/§8.2) — per
    WORKSPACE, so a visitor's invited colleagues share one hourly budget.
    Cleared by :func:`clear_bindings` and by the reaper's pattern delete."""
    from app.services.ws_rooms import ws_key
    return ws_key(workspace_id, 'outbound')


def _pack(workspace_id: int, user_id: int) -> str:
    return f'{int(workspace_id)}:{int(user_id)}'


def _unpack(raw) -> Optional[Tuple[int, int]]:
    """Parse a "<ws_id>:<user_id>" binding value. None on any legacy/garbage
    value (old persona-keyed bindings were bare user ids — treated as unbound
    rather than misattributed)."""
    try:
        s = _as_str(raw)
        ws_part, user_part = s.split(':', 1)
        return int(ws_part), int(user_part)
    except (TypeError, ValueError, AttributeError):
        return None


def _gen_code(redis_client) -> Optional[str]:
    """Allocate a random 6-digit code not currently mapped to a workspace.

    DEMO-SEC-06: the code is the ONLY thing binding an SMS sender to a
    workspace (the shared demo number carries no other context), so it must
    be unguessable — a predictable code lets a stranger bind THEIR phone
    to someone else's workspace. 6 digits of secrets-grade randomness
    against a handful of live codes, plus the per-sender attempt cap in
    the sms-inbound webhook, is plenty.
    """
    for _ in range(12):
        code = str(secrets.randbelow(900000) + 100000)
        if not redis_client.exists(_code_key(code)):
            return code
    return None


def generate_pairing_code(workspace_id: int, user_id: int) -> Optional[str]:
    """Create (or replace) the pairing code for a workspace. Returns the code.

    ``user_id`` is the requesting visitor — it rides in the binding value so
    inbound attribution can stamp ``Call.user_id`` with a real member of the
    workspace. Replacing an existing code invalidates the previous one so
    only one code is ever live per workspace.
    """
    redis_client = get_redis_client()
    if redis_client is None:
        return None
    workspace_id = int(workspace_id)

    # Drop any prior code for this workspace so it can't still pair.
    prior = redis_client.get(_ws_code_key(workspace_id))
    if prior:
        redis_client.delete(_code_key(_as_str(prior)))

    code = _gen_code(redis_client)
    if code is None:
        logger.warning("demo_verify: could not allocate a free pairing code")
        return None
    redis_client.set(_code_key(code), _pack(workspace_id, user_id), ex=_CODE_TTL_SECONDS)
    redis_client.set(_ws_code_key(workspace_id), code, ex=_CODE_TTL_SECONDS)
    return code


def _as_str(v) -> str:
    return v.decode() if isinstance(v, (bytes, bytearray)) else str(v)


PAIR_OK = 'PAIRED'
PAIR_INVALID = 'INVALID_CODE'
PAIR_NUMBER_TAKEN = 'NUMBER_TAKEN'
PAIR_NO_NUMBER = 'NO_NUMBER'

# NOTE (Phase 5, security): an SMS "re-claim" flow was prototyped here —
# texting a fresh workspace's code from a number already bound to a
# different live workspace would move that older workspace onto the new
# browser's cookie. It was REVERTED before shipping: the pairing code is a
# bearer secret shown on-screen, so an attacker could create a workspace,
# read its code, and social-engineer a victim into texting it — handing the
# attacker the victim's workspace (KB, contacts, call history, verified
# number). A single inbound SMS cannot prove the code's browser and the
# sender's phone belong to the same person, so cross-browser transfer is
# fundamentally unsafe. A number already bound to a live workspace stays
# NUMBER_TAKEN; the 7-day session cookie remains the lost-session recovery
# path. See MULTI_TENANCY_DESIGN.md "Phase 5 deviations".


def pair_number(code: str, number: str) -> dict:
    """Bind a verified phone number to the workspace that owns ``code``.

    Called from the inbound-SMS webhook when the visitor texts their pairing
    code to the demo number. Returns a dict with a ``status`` key:
      - PAIRED       → {status, workspace_id, user_id, masked}
      - INVALID_CODE → code unknown/expired
      - NUMBER_TAKEN → number already verified by a different LIVE workspace
      - NO_NUMBER    → sender number missing/unusable

    Side effects on PAIRED: writes both number bindings (workspace TTL),
    consumes the one-time code, and mirrors the number into
    ``workspaces.verified_number``. One number ↔ one live workspace (§10.6):
    a binding held by an expired workspace is silently claimable.
    """
    redis_client = get_redis_client()
    if redis_client is None:
        return {'status': PAIR_INVALID}

    norm = _norm(number)
    if not norm:
        return {'status': PAIR_NO_NUMBER}

    code = (code or '').strip()
    binding = _unpack(redis_client.get(_code_key(code)))
    if binding is None:
        return {'status': PAIR_INVALID}
    workspace_id, user_id = binding
    if not _workspace_live(workspace_id):
        # A code can't outlive its workspace by much (1h TTL), but never
        # bind into a released/expired workspace.
        return {'status': PAIR_INVALID}

    # One number ↔ one live workspace. If this number is already bound to a
    # DIFFERENT workspace that is still live, refuse — otherwise the binding
    # is stale (workspace gone) and we take it over. We deliberately do NOT
    # transfer the existing workspace to this code's browser: the code is a
    # bearer secret and the SMS can't prove the two belong to one person
    # (see the reverted re-claim note above).
    existing = _unpack(redis_client.get(_number_key(norm)))
    if existing is not None:
        existing_ws, _existing_uid = existing
        if existing_ws != workspace_id and _workspace_live(existing_ws):
            return {'status': PAIR_NUMBER_TAKEN}

    ttl = _binding_ttl_seconds()
    redis_client.set(_number_key(norm), _pack(workspace_id, user_id), ex=ttl)
    redis_client.set(_ws_number_key(workspace_id), norm, ex=ttl)
    # One-time code: consume it so it can't be replayed.
    redis_client.delete(_code_key(code))
    redis_client.delete(_ws_code_key(workspace_id))

    # Durable mirror for recovery / operator visibility (§6.2). Best-effort —
    # the Redis binding is authoritative.
    try:
        from app import db
        from app.models import Workspace
        ws = db.session.get(Workspace, workspace_id)
        if ws is not None:
            ws.verified_number = norm
            db.session.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("demo_verify: verified_number mirror write failed: %s", exc)

    try:
        from app.services.demo_telemetry import bump_daily
        bump_daily('ws_verified')
    except Exception:
        pass

    logger.info(
        "demo_verify: paired number %s → workspace %s (user %s)",
        mask_number(norm), workspace_id, user_id,
    )
    return {
        'status': PAIR_OK,
        'workspace_id': workspace_id,
        'user_id': user_id,
        'masked': mask_number(norm),
    }


def get_verified_number(workspace_id) -> Optional[str]:
    """Normalized verified number for a workspace, or None."""
    if workspace_id is None:
        return None
    redis_client = get_redis_client()
    if redis_client is None:
        return None
    raw = redis_client.get(_ws_number_key(int(workspace_id)))
    return _as_str(raw) if raw else None


def get_workspace_for_number(number: str) -> Optional[Tuple[int, int]]:
    """``(workspace_id, user_id)`` bound to a verified number, or None.

    THE inbound-attribution lookup (§6.1). Confirms the workspace row is
    still live — a binding that outlived its workspace never grants access
    (fail closed).
    """
    norm = _norm(number)
    if not norm:
        return None
    redis_client = get_redis_client()
    if redis_client is None:
        return None
    binding = _unpack(redis_client.get(_number_key(norm)))
    if binding is None:
        return None
    workspace_id, user_id = binding
    if not _workspace_live(workspace_id):
        return None
    return workspace_id, user_id


def is_number_verified_for_workspace(workspace_id, number: str) -> bool:
    """True iff ``number`` is this workspace's verified number."""
    if workspace_id is None:
        return False
    norm = _norm(number)
    if not norm:
        return False
    return get_verified_number(workspace_id) == norm


def refresh_bindings(workspace_id) -> None:
    """Re-assert binding TTLs on activity (demo heartbeat)."""
    if workspace_id is None:
        return
    redis_client = get_redis_client()
    if redis_client is None:
        return
    workspace_id = int(workspace_id)
    ttl = _binding_ttl_seconds()
    norm = redis_client.get(_ws_number_key(workspace_id))
    if norm:
        redis_client.expire(_number_key(_as_str(norm)), ttl)
        redis_client.expire(_ws_number_key(workspace_id), ttl)
    code = redis_client.get(_ws_code_key(workspace_id))
    if code:
        redis_client.expire(_code_key(_as_str(code)), _CODE_TTL_SECONDS)
        redis_client.expire(_ws_code_key(workspace_id), _CODE_TTL_SECONDS)


def clear_bindings(workspace_id) -> None:
    """Delete all verify state for a workspace. Called on release/reap so
    the number is immediately claimable by the visitor's next workspace."""
    if workspace_id is None:
        return
    redis_client = get_redis_client()
    if redis_client is None:
        return
    workspace_id = int(workspace_id)
    norm = redis_client.get(_ws_number_key(workspace_id))
    if norm:
        redis_client.delete(_number_key(_as_str(norm)))
    code = redis_client.get(_ws_code_key(workspace_id))
    if code:
        redis_client.delete(_code_key(_as_str(code)))
    redis_client.delete(_ws_number_key(workspace_id))
    redis_client.delete(_ws_code_key(workspace_id))
    # DEMO-SEC-07 lineage: the outbound-cap counter must not survive into
    # the number's next binding.
    redis_client.delete(_outbound_key(workspace_id))


# Per-workspace outbound cap in demo mode — calls cost money, and even
# own-number dialing shouldn't be unbounded.
_OUTBOUND_CAP = 5
_OUTBOUND_WINDOW = 3600


def demo_outbound_denial(workspace_id, destination: Optional[str]) -> Optional[tuple]:
    """Demo outbound policy. Returns None if this workspace may place this
    outbound call, else ``(error_dict, http_status)``.

    Rule: in demo mode a workspace may dial ONLY its own verified phone
    number, and only up to a per-hour cap. Everything else stays blocked.
    Callers invoke this only when ``is_demo_mode()`` is true.
    """
    if not is_number_verified_for_workspace(workspace_id, destination):
        if get_verified_number(workspace_id) is None:
            return ({
                'error': 'Verify your phone number first, then you can have the demo call you.',
                'code': 'demo_verify_required',
            }, 403)
        return ({
            'error': 'In the demo you can only place calls to your own verified number.',
            'code': 'demo_blocked',
        }, 403)

    redis_client = get_redis_client()
    if redis_client is not None:
        try:
            key = _outbound_key(int(workspace_id))
            n = redis_client.incr(key)
            if n == 1:
                redis_client.expire(key, _OUTBOUND_WINDOW)
            if n > _OUTBOUND_CAP:
                return ({
                    'error': 'Demo call limit reached for now — try again in a bit.',
                    'code': 'rate_limited',
                }, 429)
        except Exception:
            pass
    return None


def verify_status(workspace_id) -> dict:
    """Frontend status payload: current code (if any) + verified number (masked)."""
    redis_client = get_redis_client()
    code = None
    if redis_client is not None and workspace_id is not None:
        raw = redis_client.get(_ws_code_key(int(workspace_id)))
        code = _as_str(raw) if raw else None
    verified = get_verified_number(workspace_id)
    return {
        'verified': verified is not None,
        'code': code,
        'masked_number': mask_number(verified),
    }


# ---------------------------------------------------------------------------
# Verify-first inbound gate (§6.1 / §10.1)
# ---------------------------------------------------------------------------

REJECTED_COUNTER_KEY = 'demo:inbound:rejected'


def unverified_reject_swml() -> dict:
    """Polite reject SWML for inbound calls from numbers with no live
    workspace binding. No Call/Contact rows are created for these."""
    return {
        'version': '1.0.0',
        'sections': {
            'main': [
                'answer',
                {
                    'play': {
                        'urls': [
                            'say:This number is not linked to a demo workspace. '
                            'Start a demo on the website and text your pairing '
                            'code to link your phone. Goodbye.'
                        ]
                    }
                },
                'hangup',
            ]
        },
    }


def note_rejected_inbound(from_number: Optional[str], entry_point: str) -> None:
    """Telemetry for verify-first rejections: one platform-scoped webhook
    event log line + a Redis counter. Best-effort."""
    try:
        redis_client = get_redis_client()
        if redis_client is not None:
            redis_client.incr(REJECTED_COUNTER_KEY)
    except Exception:
        pass
    try:
        from app.services.demo_telemetry import bump_daily
        bump_daily('inbound_rejected')
    except Exception:
        pass
    try:
        from app.models import WebhookEvent
        WebhookEvent.log_event(
            event_type='inbound_rejected_unverified',
            payload={'from': mask_number(from_number), 'entry_point': entry_point},
            call_id=None,
        )
    except Exception:
        pass
