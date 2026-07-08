"""Callback System API — Tier 2r.

Endpoints:
    GET    /api/callbacks                  list pending callbacks (with optional queue filter)
    GET    /api/callbacks/<id>             get one
    POST   /api/callbacks                  create (agent-scheduled or programmatic)
    PUT    /api/callbacks/<id>/claim       agent claims a callback
    PUT    /api/callbacks/<id>/release     agent releases a claimed callback back to the pool
    PUT    /api/callbacks/<id>/outcome     record an outcome (success/no-answer/etc)
    POST   /api/callbacks/<id>/dial        initiate the outbound dial via SignalWire

An IVR-driven creation path (caller presses a key while waiting to be
added to the callback list) is on the roadmap (Tier 1c) but not currently
wired — this module is the agent-facing surface.
"""
from datetime import datetime
import json
import logging

from flask import Blueprint, jsonify, request

from app import db, socketio
from app.models import Call, Callback, Contact, User
from app.models.callback import CALLBACK_OUTCOMES
from app.services.signalwire_api import get_signalwire_api
from app.utils.decorators import require_auth
from app.utils.demo_config import is_demo_mode
from app.utils.url_utils import get_base_url, signed_webhook_url

logger = logging.getLogger(__name__)

callbacks_bp = Blueprint('callbacks', __name__)


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def _emit_callback_event(event: str, callback: Callback):
    """Push a real-time event to anyone watching the callback queue.

    A few events are namespaced under one channel so listeners can decide
    whether to refetch the list or surgically update a row.
    """
    try:
        socketio.emit('callback_event', {
            'event': event,
            'callback': callback.to_dict(include_contact=True),
        })
    except Exception as exc:
        logger.warning('Failed to emit callback_event %s: %s', event, exc)


def _find_callback_or_404(callback_id):
    cb = db.session.query(Callback).filter_by(id=callback_id).first()
    if not cb:
        return None, (jsonify({'error': 'Callback not found'}), 404)
    return cb, None


# -----------------------------------------------------------------------------
# List + retrieve
# -----------------------------------------------------------------------------

@callbacks_bp.route('', methods=['GET'])
@callbacks_bp.route('/', methods=['GET'])
@require_auth
def list_callbacks():
    """List pending callbacks, oldest first.

    Query params:
        queue_id     filter to one queue
        status       'pending' (default), 'claimed', 'completed', 'expired', 'all'
        mine         when true, restrict to callbacks claimed by the current user
        limit        cap, defaults to 100
    """
    queue_id = request.args.get('queue_id')
    status = (request.args.get('status') or 'pending').lower()
    mine = (request.args.get('mine') or '').lower() == 'true'
    limit = max(1, min(500, request.args.get('limit', default=100, type=int) or 100))

    now = datetime.utcnow()
    query = db.session.query(Callback)

    if status == 'pending':
        query = query.filter(
            Callback.completed_at.is_(None),
            Callback.claimed_at.is_(None),
            Callback.expires_at > now,
        )
    elif status == 'claimed':
        query = query.filter(
            Callback.completed_at.is_(None),
            Callback.claimed_at.isnot(None),
        )
    elif status == 'completed':
        query = query.filter(Callback.completed_at.isnot(None))
    elif status == 'expired':
        query = query.filter(
            Callback.completed_at.is_(None),
            Callback.expires_at <= now,
        )
    elif status == 'all':
        pass
    else:
        return jsonify({'error': f'Unknown status: {status}'}), 400

    if queue_id:
        query = query.filter(Callback.queue_id == queue_id)
    if mine:
        query = query.filter(Callback.claimed_by_agent_id == request.current_user.id)

    rows = query.order_by(Callback.requested_at.asc()).limit(limit).all()
    return jsonify({
        'callbacks': [r.to_dict(include_contact=True) for r in rows],
        'total': len(rows),
    }), 200


@callbacks_bp.route('/pending-count', methods=['GET'])
@require_auth
def pending_count():
    """Lightweight count for the agent header badge.

    Optionally filtered by queue (?queue_id=sales) for queue-targeted UIs.
    """
    queue_id = request.args.get('queue_id')
    now = datetime.utcnow()
    query = db.session.query(Callback).filter(
        Callback.completed_at.is_(None),
        Callback.claimed_at.is_(None),
        Callback.expires_at > now,
    )
    if queue_id:
        query = query.filter(Callback.queue_id == queue_id)
    return jsonify({'pending': query.count()}), 200


@callbacks_bp.route('/<int:callback_id>', methods=['GET'])
@require_auth
def get_callback(callback_id):
    cb, err = _find_callback_or_404(callback_id)
    if err:
        return err
    return jsonify({'callback': cb.to_dict(include_contact=True)}), 200


@callbacks_bp.route('/for-contact/<int:contact_id>', methods=['GET'])
@require_auth
def get_pending_for_contact(contact_id):
    """Surface the most-recent pending callback for a contact (used for the
    ContactDetailView "awaiting callback" banner).

    Returns 200 with `null` when there's nothing pending — keeps the UI
    branching predictable without a 404.
    """
    cb = Callback.find_pending_for_contact(contact_id)
    return jsonify({'callback': cb.to_dict() if cb else None}), 200


# -----------------------------------------------------------------------------
# Create
# -----------------------------------------------------------------------------

@callbacks_bp.route('', methods=['POST'])
@callbacks_bp.route('/', methods=['POST'])
@require_auth
def create_callback():
    """Create a callback row.

    Body:
        phone_number (required)
        call_id        (optional — link to an existing call to snapshot context)
        contact_id     (optional)
        queue_id       (optional — which queue this callback belongs to)
        caller_name    (optional)
        reason         (optional)
        ai_context     (optional dict — snapshot of what the AI captured)
        expiry_hours   (optional, defaults to 24)
    """
    try:
        data = request.get_json() or {}
        phone_number = (data.get('phone_number') or '').strip()
        if not phone_number:
            return jsonify({'error': 'phone_number is required'}), 400

        # ISO-15 (2026-07-07 pre-deploy): caller_name/reason are visitor-typed
        # free text that surfaces in the shared callback queue. Moderate them
        # in demo mode, same as contact fields and AI-message injects.
        if is_demo_mode():
            from app.utils.moderation import is_text_acceptable
            for field in ('caller_name', 'reason'):
                val = data.get(field)
                if val:
                    ok, why = is_text_acceptable(val)
                    if not ok:
                        return jsonify({
                            'error': why,
                            'code': 'moderation_blocked',
                            'field': field,
                        }), 422

        call = None
        call_id_param = data.get('call_id')
        if call_id_param:
            # Accept either the DB id or the SignalWire call_sid for convenience.
            if isinstance(call_id_param, int) or str(call_id_param).isdigit():
                call = db.session.query(Call).filter_by(id=int(call_id_param)).first()
            if not call:
                call = Call.find_by_sid(str(call_id_param))

        if call is not None:
            # Snapshot from the call — strongest defaults.
            cb = Callback.create_from_call(
                call=call,
                queue_id=data.get('queue_id'),
                reason=data.get('reason'),
                expiry_hours=data.get('expiry_hours', 24),
            )
            # Allow caller-supplied overrides on top of the snapshot.
            if data.get('phone_number'):
                cb.phone_number = phone_number
            if data.get('caller_name'):
                cb.caller_name = data['caller_name']
            if data.get('contact_id'):
                cb.contact_id = data['contact_id']
        else:
            # Pure-API create (e.g., agent schedules a proactive callback for
            # a contact without a current call).
            ai_context = data.get('ai_context')
            cb = Callback(
                phone_number=phone_number,
                caller_name=data.get('caller_name'),
                reason=data.get('reason'),
                queue_id=data.get('queue_id'),
                contact_id=data.get('contact_id'),
                ai_context=json.dumps(ai_context) if isinstance(ai_context, dict) else ai_context,
                requested_at=datetime.utcnow(),
                expires_at=_compute_expiry(data.get('expiry_hours', 24)),
            )

        db.session.add(cb)
        db.session.commit()
        logger.info('Callback %s created (phone=%s queue=%s)', cb.id, cb.phone_number, cb.queue_id)
        _emit_callback_event('created', cb)

        return jsonify({'callback': cb.to_dict(include_contact=True)}), 201
    except Exception as exc:
        logger.error('Failed to create callback: %s', exc)
        return jsonify({'error': f'Failed to create callback: {exc}'}), 500


# -----------------------------------------------------------------------------
# Claim / release / outcome
# -----------------------------------------------------------------------------

@callbacks_bp.route('/<int:callback_id>/claim', methods=['PUT'])
@require_auth
def claim_callback(callback_id):
    """An agent takes ownership of a pending callback.

    LIFE-03 fix (2026-06-02 audit): replaces the previous check-then-act
    pattern with an atomic UPDATE...WHERE so two agents clicking Claim
    simultaneously can't both succeed. Same shape as the LIFE-01
    Take/Push-dispatch race fix in calls.py:take_queued_call. Self-claim
    stays idempotent (the WHERE allows already-claimed-by-me).
    """
    try:
        cb, err = _find_callback_or_404(callback_id)
        if err:
            return err
        if cb.completed_at is not None:
            return jsonify({'error': 'Callback already completed'}), 409
        if cb.is_expired:
            return jsonify({'error': 'Callback has expired'}), 409

        # Atomic claim. The WHERE filter accepts NULL (unclaimed) OR the
        # current user (idempotent re-claim) — anything else loses.
        from sqlalchemy import text
        from datetime import datetime
        user_id = request.current_user.id
        result = db.session.execute(
            text(
                "UPDATE callbacks SET "
                "  claimed_by_agent_id = :uid, "
                "  claimed_at = COALESCE(claimed_at, :ts) "
                "WHERE id = :id AND ("
                "  claimed_by_agent_id IS NULL OR claimed_by_agent_id = :uid"
                ") AND completed_at IS NULL "
                "RETURNING id"
            ),
            {'uid': user_id, 'ts': datetime.utcnow(), 'id': callback_id},
        )
        if not result.fetchone():
            db.session.rollback()
            # Re-fetch to log who owns it now without trusting the
            # potentially-stale in-memory cb.claimed_by_agent_id.
            current = db.session.execute(
                text("SELECT claimed_by_agent_id, completed_at FROM callbacks WHERE id = :id"),
                {'id': callback_id},
            ).fetchone()
            if current and current[1] is not None:
                return jsonify({'error': 'Callback already completed'}), 409
            return jsonify({
                'error': 'Already claimed by another agent',
                'claimed_by_agent_id': current[0] if current else None,
            }), 409

        db.session.commit()
        # Refresh so the response payload reflects the claimed state.
        db.session.refresh(cb)
        _emit_callback_event('claimed', cb)
        return jsonify({'callback': cb.to_dict(include_contact=True)}), 200
    except Exception as exc:
        logger.error('Failed to claim callback %s: %s', callback_id, exc)
        db.session.rollback()
        return jsonify({'error': f'Failed to claim callback: {exc}'}), 500


@callbacks_bp.route('/<int:callback_id>/release', methods=['PUT'])
@require_auth
def release_callback(callback_id):
    """Return a claimed callback to the pending pool."""
    try:
        cb, err = _find_callback_or_404(callback_id)
        if err:
            return err
        if cb.claimed_by_agent_id != request.current_user.id:
            return jsonify({'error': "You don't own this callback"}), 403
        cb.release()
        db.session.commit()
        _emit_callback_event('released', cb)
        return jsonify({'callback': cb.to_dict(include_contact=True)}), 200
    except Exception as exc:
        logger.error('Failed to release callback %s: %s', callback_id, exc)
        return jsonify({'error': f'Failed to release callback: {exc}'}), 500


@callbacks_bp.route('/<int:callback_id>/outcome', methods=['PUT'])
@require_auth
def record_outcome(callback_id):
    """Record an outcome and optionally re-open for retry."""
    try:
        data = request.get_json() or {}
        outcome = data.get('outcome')
        notes = data.get('notes')
        retry = bool(data.get('retry', False))

        if outcome not in CALLBACK_OUTCOMES:
            return jsonify({
                'error': f'Unknown outcome: {outcome}',
                'valid_outcomes': list(CALLBACK_OUTCOMES),
            }), 400

        cb, err = _find_callback_or_404(callback_id)
        if err:
            return err

        # ISO-15 (2026-07-07 pre-deploy): ownership gate — without it any agent
        # could close any pending callback and drain the shared queue. Require
        # the requester to be the claiming agent (or a supervisor/admin),
        # mirroring release_callback.
        role = request.current_user.role or ''
        if role not in ('admin', 'supervisor') and cb.claimed_by_agent_id != request.current_user.id:
            return jsonify({
                'error': "You don't own this callback",
                'detail': 'Claim it first, or ask a supervisor to record the outcome.',
            }), 403

        # ISO-15: notes are visitor-typed free text — moderate in demo mode.
        if notes and is_demo_mode():
            from app.utils.moderation import is_text_acceptable
            ok, why = is_text_acceptable(notes)
            if not ok:
                return jsonify({
                    'error': why, 'code': 'moderation_blocked', 'field': 'notes',
                }), 422

        cb.complete(outcome, notes=notes)
        # If the agent wants to retry (e.g. no-answer / voicemail), bump
        # attempts and clear claim so the row goes back to pending.
        if retry and outcome in ('no-answer', 'voicemail'):
            # We complete first so a record of the attempt exists, then
            # spawn a fresh row that points at the same call/contact.
            db.session.flush()
            retry_row = Callback(
                call_id=cb.call_id,
                contact_id=cb.contact_id,
                queue_id=cb.queue_id,
                phone_number=cb.phone_number,
                caller_name=cb.caller_name,
                reason=cb.reason,
                ai_context=cb.ai_context,
                requested_at=datetime.utcnow(),
                expires_at=_compute_expiry(24),
                attempts=cb.attempts + 1,
            )
            db.session.add(retry_row)
            db.session.commit()
            _emit_callback_event('completed', cb)
            _emit_callback_event('created', retry_row)
            return jsonify({
                'callback': cb.to_dict(include_contact=True),
                'retry': retry_row.to_dict(include_contact=True),
            }), 200

        db.session.commit()
        _emit_callback_event('completed', cb)
        return jsonify({'callback': cb.to_dict(include_contact=True)}), 200
    except Exception as exc:
        logger.error('Failed to record outcome for callback %s: %s', callback_id, exc)
        return jsonify({'error': f'Failed to record outcome: {exc}'}), 500


# -----------------------------------------------------------------------------
# Dial
# -----------------------------------------------------------------------------

@callbacks_bp.route('/<int:callback_id>/dial', methods=['POST'])
@require_auth
def dial_callback(callback_id):
    """Initiate the outbound call to fulfil the callback.

    In DEMO_MODE this is gated to the persona's own verified number (phone
    verification) with a per-hour cap — a visitor can dial a callback they
    scheduled to THEIR verified number, but the demo can't fan out real calls
    to arbitrary numbers. Unverified / mismatched numbers get 403 so the UI
    can prompt to verify.
    """
    try:
        cb, err = _find_callback_or_404(callback_id)
        if err:
            return err
        if cb.completed_at is not None:
            return jsonify({'error': 'Callback already completed'}), 409
        if cb.is_expired:
            return jsonify({'error': 'Callback has expired'}), 409

        # Demo outbound gate — own verified number only, capped.
        if is_demo_mode():
            from app.services.demo_verify import demo_outbound_denial
            denial = demo_outbound_denial(request.current_user.id, cb.phone_number)
            if denial:
                return jsonify(denial[0]), denial[1]

        # Auto-claim on dial — UX shortcut so the agent doesn't have to
        # explicitly click claim before dial. Atomic so a race with another
        # agent's claim doesn't let both proceed to the outbound dial
        # (LIFE-03 followup: the auto-claim used to be the same vulnerable
        # check-then-act).
        from sqlalchemy import text
        from datetime import datetime
        user_id = request.current_user.id
        claim = db.session.execute(
            text(
                "UPDATE callbacks SET "
                "  claimed_by_agent_id = :uid, "
                "  claimed_at = COALESCE(claimed_at, :ts) "
                "WHERE id = :id AND ("
                "  claimed_by_agent_id IS NULL OR claimed_by_agent_id = :uid"
                ") AND completed_at IS NULL "
                "RETURNING id"
            ),
            {'uid': user_id, 'ts': datetime.utcnow(), 'id': callback_id},
        )
        if not claim.fetchone():
            db.session.rollback()
            current = db.session.execute(
                text("SELECT claimed_by_agent_id FROM callbacks WHERE id = :id"),
                {'id': callback_id},
            ).fetchone()
            return jsonify({
                'error': 'Already claimed by another agent',
                'claimed_by_agent_id': current[0] if current else None,
            }), 409
        db.session.refresh(cb)

        # Initiate the outbound call. We dial through the standard
        # initial-call SWML so the agent's CRM context is preserved and
        # transcription / recording kick in just like an inbound. amd=1
        # adds answering-machine detection so the pipeline doesn't run
        # against a voicemail greeting (machine → message + hangup).
        sw_api = get_signalwire_api()
        base_url = get_base_url()
        swml_url = f"{base_url}/api/swml/initial-call?amd=1"
        status_callback = signed_webhook_url(f"{base_url}/api/webhooks/call-status")

        sw_call = sw_api.create_call(
            to=cb.phone_number,
            swml_url=swml_url,
            status_callback=status_callback,
        )
        new_call_id = sw_call.sid if hasattr(sw_call, 'sid') else str(sw_call.get('call_id', ''))

        # Record a Call row for the outbound leg, linked back to the
        # originating callback for reporting.
        outbound_call = Call(
            user_id=request.current_user.id,
            signalwire_call_sid=new_call_id,
            from_number=None,
            destination=cb.phone_number,
            destination_type='phone',
            direction='outbound',
            handler_type='human',
            status='initiated',
            queue_id=cb.queue_id,
            contact_id=cb.contact_id,
            ai_context=cb.ai_context,  # carry the captured context forward
            transcription_active=True,
        )
        db.session.add(outbound_call)
        db.session.flush()

        cb.attempts += 1
        db.session.commit()

        logger.info(
            'Callback %s dialled by agent %s → call %s (sw_call_id=%s)',
            cb.id, request.current_user.id, outbound_call.id, new_call_id,
        )

        _emit_callback_event('dialled', cb)
        return jsonify({
            'callback': cb.to_dict(include_contact=True),
            'call_id': new_call_id,
            'outbound_call_db_id': outbound_call.id,
        }), 200
    except Exception as exc:
        logger.error('Failed to dial callback %s: %s', callback_id, exc)
        return jsonify({'error': f'Failed to dial callback: {exc}'}), 500


# -----------------------------------------------------------------------------
# Internal helpers
# -----------------------------------------------------------------------------

def _compute_expiry(hours):
    from datetime import timedelta
    try:
        h = int(hours)
    except (TypeError, ValueError):
        h = 24
    return datetime.utcnow() + timedelta(hours=h)
