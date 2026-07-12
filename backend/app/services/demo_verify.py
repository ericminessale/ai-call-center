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
visitor's dashboard shows a random 6-digit code and asks them to TEXT it to
the demo number; the inbound-SMS webhook (``/api/webhooks/sms-inbound``)
matches the code and binds the sender's number. Receiving an MO message requires no
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
import secrets
from typing import Optional

from app.services.redis_service import get_redis_client

logger = logging.getLogger(__name__)


def _binding_ttl_seconds() -> int:
    """Verify-binding idle window. Same knob + default the old persona
    lease used (``DEMO_LEASE_TTL_SECONDS``, 300s, clamped [60, 3600]) —
    bindings are refreshed by the workspace heartbeat, so a live tab keeps
    them alive indefinitely. Phase 4 re-keys bindings number→workspace and
    stretches the TTL to the workspace lifetime.
    """
    import os
    raw = os.getenv('DEMO_LEASE_TTL_SECONDS', '300').strip()
    try:
        n = int(raw)
    except ValueError:
        n = 300
    return max(60, min(n, 3600))


def _lease_ttl_seconds() -> int:
    # Internal alias kept for the pre-tenancy call sites below.
    return _binding_ttl_seconds()


def has_active_lease(user_id: int) -> bool:
    """Liveness for a visitor user — post-tenancy this means "the user's
    workspace session is alive" (see workspace_session.user_workspace_alive).
    Name kept from the persona-lease era for the call sites below."""
    try:
        from app.services.workspace_session import user_workspace_alive
        return user_workspace_alive(user_id)
    except Exception:
        return False


# Code lives as long as a binding idle-window — long enough for the visitor
# to place the call, short enough that a stale code on screen expires with
# the session. Reuses the binding TTL so the two never diverge.
_CODE_TTL = _binding_ttl_seconds


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


def _outbound_key(persona_id: int) -> str:
    """Outbound-cap counter, re-keyed ``ws:{id}:outbound`` (§4.3/§8.2).

    The cap is now per WORKSPACE, not per user — a visitor's invited
    colleagues share the visitor's hourly budget instead of multiplying
    it. Falls back to a user-keyed name only when the user row can't be
    resolved (shouldn't happen for live visitors), so a lookup blip
    degrades to the old per-user behavior rather than an uncapped dial.
    The workspace reaper's ``ws:{id}:*`` pattern delete clears it, and
    :func:`clear_bindings` still deletes it explicitly.
    """
    try:
        from app.models import User
        from app.services.ws_rooms import ws_key
        from app.tenancy import workspace_context
        with workspace_context(None):
            user = User.query.get(int(persona_id))
        if user is not None:
            return ws_key(user.workspace_id, 'outbound')
    except Exception:
        pass
    return f'demo:outbound:{int(persona_id)}'


def _gen_code(redis_client) -> Optional[str]:
    """Allocate a random 6-digit code not currently mapped to another persona.

    DEMO-SEC-06: the code is the ONLY thing binding an SMS sender to a
    session (the shared demo number carries no other context), so it must
    be unguessable — a predictable code lets a stranger bind THEIR phone
    to someone else's session. 6 digits of secrets-grade randomness
    against a handful of live codes, plus the per-sender attempt cap in
    the sms-inbound webhook, is plenty.
    """
    for _ in range(12):
        code = str(secrets.randbelow(900000) + 100000)
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
    # visitor, so one who verifies while already on a call gets privacy
    # immediately (the inbound-attribution hook only covers NEW calls).
    # Kept in Phase 1: unverified inbound still reaches the floor until the
    # Phase 4 verify-first rejection lands — only then is this sweep dead
    # code (nothing pre-verification will exist to re-parent).
    # Best-effort — pairing succeeds regardless.
    try:
        from app import db
        from app.models import (
            Call,
            CallLeg,
            Callback,
            ConferenceParticipant,
            Transcription,
            User,
            WebhookEvent,
        )
        visitor = User.query.get(persona_id)
        live = (
            Call.query
            .filter(Call.from_number.in_([norm, number]))
            .filter(Call.status.in_(['initiated', 'ringing', 'answered', 'ai_active', 'waiting', 'assigned', 'active']))
            .all()
        )
        for call in live:
            call.user_id = persona_id
            # Tenancy: visibility now rides on workspace_id — re-parent the
            # call into the visitor's workspace too, or it stays quarantined
            # in the default workspace where the visitor can't see it. The
            # call's pre-verification children were stamped from the call's
            # OLD workspace, so they must follow or the visitor gets a call
            # with no transcript/legs in their scoped views. (Contacts are
            # deliberately NOT moved: phone is per-workspace unique and the
            # ws-1 contact may be shared by other quarantined calls —
            # contact attribution is Phase 4's verify-first rework.)
            if visitor is not None and visitor.workspace_id is not None:
                old_ws_id = call.workspace_id
                call.workspace_id = visitor.workspace_id
                for child in (Transcription, CallLeg, WebhookEvent,
                              Callback, ConferenceParticipant):
                    (
                        child.query
                        .filter_by(call_id=call.id)
                        .update({'workspace_id': visitor.workspace_id},
                                synchronize_session=False)
                    )
                # Conference rows key by the SignalWire sid STRING, not the
                # DB id, so the children loop above misses them — and
                # conference-keyed emits (conference_ended,
                # participant_moved) target workspace_room(conference.
                # workspace_id), so a row left in quarantine would emit to
                # the wrong room for the rest of the call.
                from app.models import Conference as _Conference
                (
                    _Conference.query
                    .filter_by(call_id=call.signalwire_call_sid)
                    .update({'workspace_id': visitor.workspace_id},
                            synchronize_session=False)
                )
                # Phase 3: queue state is workspace-keyed. A caller who was
                # parked BEFORE verifying sits in the OLD (quarantine)
                # workspace's zset, where the visitor's dispatch will never
                # find them — move the entry to the new workspace's queue,
                # preserving priority/context (enqueue_call keeps the
                # original SLA clock from Call.created_at).
                if call.status in ('waiting', 'assigned') and call.queue_id:
                    try:
                        import json as _json
                        from app.services.queue_service import QueueService
                        qs_new = QueueService(
                            redis_client, workspace_id=visitor.workspace_id
                        )
                        removed = qs_new.remove_call_from_all_queues(
                            call.signalwire_call_sid
                        )
                        if removed and call.status == 'waiting':
                            try:
                                ctx = _json.loads(call.ai_context) if call.ai_context else {}
                            except (TypeError, ValueError):
                                ctx = {}
                            qs_new.enqueue_call(
                                call_id=call.signalwire_call_sid,
                                queue_id=call.queue_id,
                                priority=ctx.get('priority', 5),
                                context=ctx,
                                caller_info={'number': call.from_number, 'name': None},
                            )
                            logger.info(
                                "demo_verify: moved queued call %s from ws %s "
                                "to ws %s queue '%s'",
                                call.signalwire_call_sid, old_ws_id,
                                visitor.workspace_id, call.queue_id,
                            )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "demo_verify: queued-call workspace move failed "
                            "for %s: %s", call.signalwire_call_sid, exc,
                        )
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
    """Extend the verified-number binding TTL to match a fresh heartbeat.

    Called from the workspace heartbeat (``/api/demo/heartbeat``) so the
    number stays verified for as long as the visitor keeps their session
    alive.
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
    # DEMO-SEC-07: the outbound-cap counter is persona-keyed too — left
    # behind, the next visitor to lease this persona inherits the previous
    # visitor's remaining call budget.
    redis_client.delete(_outbound_key(persona_id))


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
            key = _outbound_key(persona_id)
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
