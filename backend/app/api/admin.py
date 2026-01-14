from flask import request, jsonify
from app import db
from app.api import admin_bp
from app.models import Call, Transcription
from app.utils.decorators import require_auth
import logging

logger = logging.getLogger(__name__)


@admin_bp.route('/clear-calls', methods=['POST'])
@require_auth
def clear_calls():
    """Clear all stale calls from the database.

    This removes:
    - Calls in 'created' or 'ringing' status without ended_at (old waiting calls)
    - Calls in 'answered' status without ended_at (old active calls)
    - Associated transcriptions for deleted calls
    """
    logger.info(f"CLEAR CALLS REQUEST from user: {request.current_user.id}")

    try:
        # Find stale calls (calls without ended_at that are not truly active)
        # We'll consider calls older than 1 hour as stale
        from datetime import datetime, timedelta
        one_hour_ago = datetime.utcnow() - timedelta(hours=1)

        stale_calls = db.session.query(Call).filter(
            db.and_(
                Call.ended_at.is_(None),  # No end time
                Call.created_at < one_hour_ago,  # Older than 1 hour
                Call.status.in_(['created', 'ringing', 'answered', 'initiated'])  # Not completed
            )
        ).all()

        logger.info(f"Found {len(stale_calls)} stale calls to clean up")

        # Delete associated transcriptions first (foreign key constraint)
        deleted_transcriptions = 0
        for call in stale_calls:
            transcriptions = Transcription.query.filter_by(call_id=call.id).all()
            for t in transcriptions:
                db.session.delete(t)
                deleted_transcriptions += 1

        # Delete the stale calls
        deleted_calls = len(stale_calls)
        for call in stale_calls:
            logger.info(f"Deleting stale call: {call.id}, status={call.status}, created_at={call.created_at}")
            db.session.delete(call)

        db.session.commit()

        logger.info(f"Successfully deleted {deleted_calls} calls and {deleted_transcriptions} transcriptions")

        return jsonify({
            'success': True,
            'deleted_calls': deleted_calls,
            'deleted_transcriptions': deleted_transcriptions,
            'message': f'Cleared {deleted_calls} stale calls from database'
        }), 200

    except Exception as e:
        db.session.rollback()
        logger.error(f"Failed to clear calls: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        return jsonify({'error': f'Failed to clear calls: {str(e)}'}), 500
