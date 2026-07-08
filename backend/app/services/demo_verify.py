"""
Phone-number verification for hosted-demo visitors (2026-07-07).

Gives the shared demo the one thing it structurally lacked: a link between
an anonymous browser visitor (their leased ``demo_agent`` persona) and a real
phone number they control. Once a number is verified, the pre-deploy
ownership checks already in place light up automatically — inbound calls from
that number are attributed to the persona (``call.user_id``), so other
visitors can't join the room, read the transcript, steer the AI, or take it
over, and the visitor is allowed to dial *their own* number outbound.

Verification method: an **inbound pairing code**, not outbound OTP. The
visitor's dashboard shows a 4-digit code and asks them to TEXT it to the demo
number; the inbound-SMS webhook (``/api/webhooks/sms-inbound``) matches the
code and binds the sender's number. Receiving an MO message requires no
messaging campaign (10DLC/A2P gates outbound application traffic, and we
never reply), it costs nothing to operate, and the SMS sender number is a
stronger possession proof than voice caller-ID.

Everything is **lease-scoped**: personas are recycled between visitors, so
all bindings carry the lease TTL, are refreshed on heartbeat, and are cleared
on release / nightly reset. Storage layout in Redis:

    demo:verify:code:<CODE>            → "<persona_id>"   (one-time, code TTL)
    demo:verify:persona_code:<pid>     → "<CODE>"         (reverse, for display/clear)
    demo:verify:number:<E164>          → "<persona_id>"   (verified binding, lease TTL)
    demo:verify:persona_number:<pid>   → "<E164>"         (reverse)

The ``number:*`` binding is authoritative for attribution + the outbound
own-number gate. ``get_persona_for_number`` additionally confirms the persona
still holds a live lease, so a number binding that outlived its lease (belt +
suspenders on the TTL) never grants access.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from app.services.demo_lease import (
    _lease_ttl_seconds,
    has_active_lease,
)
from app.services.redis_service import get_redis_client

logger = logging.getLogger(__name__)

# Code lives as long as a lease idle-window — long enough for the visitor to
# place the call, short enough that a stale code on screen expires with the
# session. Reuses the lease TTL so the two never diverge.
_CODE_TTL = _lease_ttl_seconds


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
    """Human-facing masked form, e.g. '+1 ••• ••• 4567'. Last 4 shown."""
    norm = _norm(number)
    if not norm:
        return None
    tail = norm[-4:]
    return f'••• ••• {tail}'


def _code_key(code: str) -> str:
    return f'demo:verify:code:{code}'


def _persona_code_key(persona_id: int) -> str:
    return f'demo:verify:persona_code:{int(persona_id)}'


def _number_key(norm_number: str) -> str:
    return f'demo:verify:number:{norm_number}'


def _persona_number_key(persona_id: int) -> str:
    return f'demo:verify:persona_number:{int(persona_id)}'


def _gen_code(redis_client) -> Optional[str]:
    """Allocate a 4-digit code not currently mapped to another persona.

    Uses INCR on a rotating counter mapped into the 1000–9999 space so we
    don't need Math.random-style entropy (and don't collide in a tight loop).
    Falls back through a few candidates if a code is somehow live.
    """
    for _ in range(12):
        seq = redis_client.incr('demo:verify:code_seq')
        code = str(1000 + (int(seq) % 9000))
        if not redis_client.exists(_code_key(code)):
            return code
    return None


def generate_pairing_code(persona_id: int) -> Optional[str]:
    """Create (or replace) the pairing code for a persona. Returns the code.

    Idempotent-ish: replacing an existing code invalidates the previous one so
    only one code is ever live per persona.
    """
    redis_client = get_redis_client()
    if redis_client is None:
        return None
    persona_id = int(persona_id)
    ttl = _CODE_TTL()

    # Drop any prior code for this persona so it can't still pair.
    prior = redis_client.get(_persona_code_key(persona_id))
    if prior:
        redis_client.delete(_code_key(_as_str(prior)))

    code = _gen_code(redis_client)
    if code is None:
        logger.warning("demo_verify: could not allocate a free pairing code")
        return None
    redis_client.set(_code_key(code), str(persona_id), ex=ttl)
    redis_client.set(_persona_code_key(persona_id), code, ex=ttl)
    return code


def _as_str(v) -> str:
    return v.decode() if isinstance(v, (bytes, bytearray)) else str(v)


PAIR_OK = 'PAIRED'
PAIR_INVALID = 'INVALID_CODE'
PAIR_NUMBER_TAKEN = 'NUMBER_TAKEN'
PAIR_NO_NUMBER = 'NO_NUMBER'


def pair_number(code: str, number: str) -> dict:
    """Bind a verified phone number to the persona that owns ``code``.

    Called from the inbound-SMS webhook when the visitor texts their pairing
    code to the demo number. Returns a dict with a ``status`` key:
      - PAIRED       → {status, persona_id, masked}
      - INVALID_CODE → code unknown/expired
      - NUMBER_TAKEN → number already verified by a different live persona
      - NO_NUMBER    → sender number missing/unusable

    Side effects on PAIRED: writes both number bindings (lease TTL), consumes
    the one-time code, and retroactively attributes any currently-live call
    from that number to the persona (covers "verify while already on a call").
    """
    redis_client = get_redis_client()
    if redis_client is None:
        return {'status': PAIR_INVALID}

    norm = _norm(number)
    if not norm:
        return {'status': PAIR_NO_NUMBER}

    code = (code or '').strip()
    raw_pid = redis_client.get(_code_key(code))
    if raw_pid is None:
        return {'status': PAIR_INVALID}
    try:
        persona_id = int(_as_str(raw_pid))
    except (TypeError, ValueError):
        return {'status': PAIR_INVALID}

    # One number ↔ one active persona. If this number is already bound to a
    # DIFFERENT persona that still holds a live lease, refuse — otherwise the
    # binding is stale (lease gone) and we take it over.
    existing_owner = redis_client.get(_number_key(norm))
    if existing_owner is not None:
        try:
            existing_pid = int(_as_str(existing_owner))
        except (TypeError, ValueError):
            existing_pid = None
        if existing_pid and existing_pid != persona_id and has_active_lease(existing_pid):
            return {'status': PAIR_NUMBER_TAKEN}

    ttl = _lease_ttl_seconds()
    redis_client.set(_number_key(norm), str(persona_id), ex=ttl)
    redis_client.set(_persona_number_key(persona_id), norm, ex=ttl)
    # One-time code: consume it so it can't be replayed.
    redis_client.delete(_code_key(code))
    redis_client.delete(_persona_code_key(persona_id))

    # Retroactively attribute any currently-live call from this number to the
    # persona, so a visitor who verifies while already on a call gets privacy
    # immediately (the inbound-attribution hook only covers NEW calls).
    # Best-effort — pairing succeeds regardless.
    try:
        from app import db
        from app.models import Call
        live = (
            Call.query
            .filter(Call.from_number.in_([norm, number]))
            .filter(Call.status.in_(['initiated', 'ringing', 'answered', 'ai_active', 'waiting', 'assigned', 'active']))
            .all()
        )
        for call in live:
            call.user_id = persona_id
        if live:
            db.session.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("demo_verify: retroactive call attribution failed: %s", exc)

    logger.info(
        "demo_verify: paired number %s → persona %s",
        mask_number(norm), persona_id,
    )
    return {'status': PAIR_OK, 'persona_id': persona_id, 'masked': mask_number(norm)}


def get_verified_number(persona_id: int) -> Optional[str]:
    """Normalized verified number for a persona, or None."""
    redis_client = get_redis_client()
    if redis_client is None:
        return None
    raw = redis_client.get(_persona_number_key(int(persona_id)))
    return _as_str(raw) if raw is not None else None


def get_persona_for_number(number: str) -> Optional[int]:
    """Persona that verified ``number`` AND still holds a live lease, else None.

    This is the authoritative read for inbound attribution + the outbound
    own-number gate: a binding whose lease has lapsed grants nothing.
    """
    redis_client = get_redis_client()
    if redis_client is None:
        return None
    norm = _norm(number)
    if not norm:
        return None
    raw = redis_client.get(_number_key(norm))
    if raw is None:
        return None
    try:
        persona_id = int(_as_str(raw))
    except (TypeError, ValueError):
        return None
    if not has_active_lease(persona_id):
        return None
    return persona_id


def is_number_verified_for_persona(persona_id: int, number: str) -> bool:
    """True iff ``number`` is the live verified number for this exact persona.

    Used by the demo outbound gate: a persona may dial ONLY its own verified
    number.
    """
    norm = _norm(number)
    if not norm:
        return False
    verified = get_verified_number(persona_id)
    return verified is not None and verified == norm


def refresh_bindings(persona_id: int) -> None:
    """Extend the verified-number binding TTL to match a fresh lease heartbeat.

    Called from ``demo_lease.heartbeat_lease`` so the number stays verified for
    as long as the visitor keeps their session alive.
    """
    redis_client = get_redis_client()
    if redis_client is None:
        return
    persona_id = int(persona_id)
    ttl = _lease_ttl_seconds()
    norm = get_verified_number(persona_id)
    if norm:
        redis_client.expire(_number_key(norm), ttl)
        redis_client.expire(_persona_number_key(persona_id), ttl)
    code = redis_client.get(_persona_code_key(persona_id))
    if code:
        redis_client.expire(_code_key(_as_str(code)), ttl)
        redis_client.expire(_persona_code_key(persona_id), ttl)


def clear_bindings(persona_id: int) -> None:
    """Delete all verify state for a persona. Called on lease release so the
    recycled persona starts clean for the next visitor."""
    redis_client = get_redis_client()
    if redis_client is None:
        return
    persona_id = int(persona_id)
    norm = redis_client.get(_persona_number_key(persona_id))
    if norm:
        redis_client.delete(_number_key(_as_str(norm)))
    code = redis_client.get(_persona_code_key(persona_id))
    if code:
        redis_client.delete(_code_key(_as_str(code)))
    redis_client.delete(_persona_number_key(persona_id))
    redis_client.delete(_persona_code_key(persona_id))


# Per-persona outbound cap in demo mode — calls cost money, and even
# own-number dialing shouldn't be unbounded.
_OUTBOUND_CAP = 5
_OUTBOUND_WINDOW = 3600


def demo_outbound_denial(persona_id: int, destination: Optional[str]) -> Optional[tuple]:
    """Demo outbound policy. Returns None if this persona may place this
    outbound call, else ``(error_dict, http_status)``.

    Rule: in demo mode a persona may dial ONLY its own verified phone number,
    and only up to a per-hour cap. Everything else stays blocked. Callers
    invoke this only when ``is_demo_mode()`` is true.
    """
    if not is_number_verified_for_persona(persona_id, destination):
        if get_verified_number(persona_id) is None:
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
            key = f'demo:outbound:{int(persona_id)}'
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


def verify_status(persona_id: int) -> dict:
    """Frontend status payload: current code (if any) + verified number (masked)."""
    redis_client = get_redis_client()
    if redis_client is None:
        return {'verified': False, 'code': None, 'masked_number': None}
    persona_id = int(persona_id)
    code = redis_client.get(_persona_code_key(persona_id))
    norm = get_verified_number(persona_id)
    return {
        'verified': norm is not None,
        'code': _as_str(code) if code else None,
        'masked_number': mask_number(norm) if norm else None,
    }
