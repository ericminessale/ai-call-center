"""Synthetic-caller test harness endpoints.

Serves the SWML for an outbound "caller bot" leg (an AI playing a customer
persona, dialed at this deployment's own inbound number by the orchestrator
in ``testing/run_scenario.py``), receives the bot's post-prompt verdict, and
exposes per-call ground truth for assertions.

Registered ONLY when ``TESTING_HARNESS_ENABLED`` is truthy (see
``app/__init__.py``) — a downloader who never sets the flag has no trace of
these routes. Auth mirrors the rest of the API surface:

  - ``/bot-swml`` and ``/verdict`` are fetched/POSTed by SignalWire, so they
    sit behind ``require_webhook_auth`` and their URLs are produced by
    ``signed_webhook_url`` — same contract as every other webhook route.
  - ``/ping``, ``/verdict/<run_id>`` and ``/call-report`` are driven by the
    orchestrator, so they hard-enforce ``require_internal_auth``.

Verdicts are Redis-only (TTL) — the harness deliberately writes NOTHING to
the product database; the product side of a test call is recorded by the
normal call pipeline, which is exactly what the assertions read back.
"""

import base64
import json
import logging
import re
from datetime import datetime

from flask import Blueprint, jsonify, request

from app.utils.webhook_auth import require_internal_auth, require_webhook_auth

logger = logging.getLogger(__name__)

harness_bp = Blueprint('testing_harness', __name__)

_VERDICT_TTL_SECONDS = 6 * 3600
_RUN_ID_RE = re.compile(r'^[A-Za-z0-9_-]{1,64}$')


def _verdict_key(run_id: str) -> str:
    return f'testing:verdict:{run_id}'


def _clean_run_id(raw) -> str | None:
    raw = (raw or '').strip()
    return raw if _RUN_ID_RE.match(raw) else None


@harness_bp.route('/ping', methods=['GET'])
@require_internal_auth
def ping():
    """Orchestrator preflight: proves the harness is enabled + auth works."""
    return jsonify({'ok': True, 'harness': 'enabled'})


@harness_bp.route('/bot-swml', methods=['GET', 'POST'])
@require_webhook_auth
def bot_swml():
    """SWML for the caller-bot leg, rendered from a b64 mission envelope.

    Query params:
        m: urlsafe-b64 JSON — {persona, post_prompt, temperature?, params?}
        run_id: correlates the leg with its verdict.

    The mission rides in the URL (same mechanism as /api/swml/initial-call's
    ``?ctx=``) so the bot needs no server-side state before the call exists.
    """
    run_id = _clean_run_id(request.args.get('run_id'))
    if run_id is None:
        return jsonify({'error': 'run_id query param is required'}), 400
    try:
        mission = json.loads(
            base64.urlsafe_b64decode((request.args.get('m') or '').encode())
        )
        persona = mission['persona']
        post_prompt = mission['post_prompt']
    except (ValueError, KeyError, TypeError) as exc:
        logger.warning("bot-swml: bad mission envelope (%s)", exc)
        return jsonify({'error': 'invalid mission envelope'}), 400

    from app.utils.url_utils import get_base_url, signed_webhook_url
    verdict_url = signed_webhook_url(
        f"{get_base_url()}/api/testing/verdict?run_id={run_id}"
    )

    params = {
        # The bot is the CALLER: stay quiet until the far end greets.
        'wait_for_user': True,
        'attention_timeout': 20000,
        # Safety net — a wedged conversation still ends and still yields a
        # verdict, rather than burning minutes until someone kills the leg.
        'inactivity_timeout': 90000,
    }
    params.update(mission.get('params') or {})

    ai_verb = {
        'prompt': {
            'text': persona,
            'temperature': mission.get('temperature', 0.6),
        },
        'post_prompt': {'text': post_prompt, 'temperature': 0.0},
        'post_prompt_url': verdict_url,
        'params': params,
    }
    # Optional multilingual voice/ASR config for the bot — a list of SWML
    # language entries ({name, code, voice}), e.g. ElevenLabs multilingual
    # voices, so scenarios can exercise language memory and mid-call
    # switching on the receiving agents.
    if mission.get('languages'):
        ai_verb['languages'] = mission['languages']
    ai_verb.update({
        'SWAIG': {
            'functions': [
                {
                    'function': 'end_call',
                    'purpose': (
                        'Hang up the phone. Use this once your mission is '
                        'complete and you have said goodbye.'
                    ),
                    'argument': {'type': 'object', 'properties': {}},
                    # Serverless: a constant data_map output carrying a
                    # hangup action — no webhook round-trip, works from a
                    # bare SWML document.
                    'data_map': {
                        'output': {
                            'response': 'The call is ending now.',
                            'action': [
                                {
                                    'SWML': {
                                        'version': '1.0.0',
                                        'sections': {'main': [{'hangup': {}}]},
                                    }
                                }
                            ],
                        }
                    },
                }
            ]
        },
    })

    document = {
        'version': '1.0.0',
        'sections': {
            'main': [
                # Both legs on tape — the async "ears check" artifact for
                # anything the structured assertions can't measure.
                {'record_call': {'format': 'mp3', 'stereo': True,
                                 'direction': 'both'}},
                {'ai': ai_verb},
                {'hangup': {}},
            ]
        },
    }
    logger.info("bot-swml: served mission for run %s", run_id)
    return jsonify(document)


@harness_bp.route('/verdict', methods=['POST'])
@require_webhook_auth
def receive_verdict():
    """Post-prompt receiver for the bot leg — parked in Redis by run_id."""
    run_id = _clean_run_id(request.args.get('run_id'))
    if run_id is None:
        return jsonify({'error': 'run_id query param is required'}), 400
    data = request.get_json(silent=True) or {}

    post_prompt_data = data.get('post_prompt_data') or {}
    parsed = post_prompt_data.get('parsed') or []
    record = {
        'received_at': datetime.utcnow().isoformat(),
        'call_id': data.get('call_id'),
        'caller_id_num': data.get('caller_id_num'),
        'raw': post_prompt_data.get('raw'),
        'parsed': parsed[0] if parsed else None,
        'global_data': data.get('global_data') or {},
    }

    from app.services.redis_service import get_redis_client
    redis_client = get_redis_client()
    if redis_client is None:
        logger.error("verdict: Redis unavailable — verdict for %s dropped", run_id)
        return jsonify({'error': 'storage unavailable'}), 503
    redis_client.set(
        _verdict_key(run_id), json.dumps(record), ex=_VERDICT_TTL_SECONDS
    )
    logger.info(
        "verdict: stored for run %s (call %s, parsed=%s)",
        run_id, record['call_id'], record['parsed'] is not None,
    )
    return jsonify({'success': True})


@harness_bp.route('/verdict/<run_id>', methods=['GET'])
@require_internal_auth
def get_verdict(run_id):
    run_id = _clean_run_id(run_id)
    if run_id is None:
        return jsonify({'error': 'bad run_id'}), 400
    from app.services.redis_service import get_redis_client
    redis_client = get_redis_client()
    raw = redis_client.get(_verdict_key(run_id)) if redis_client else None
    if not raw:
        return jsonify({'found': False}), 404
    raw = raw.decode() if isinstance(raw, (bytes, bytearray)) else raw
    return jsonify({'found': True, 'verdict': json.loads(raw)})


@harness_bp.route('/configure-queue', methods=['POST'])
@require_internal_auth
def configure_queue():
    """Set queue routing config for a run.

    Policies are expressed in seconds of caller patience, and a scenario that
    cannot change them either waits out production timings on every call or
    tests only the default. Call 93 died on exactly that: the language wait
    was 60s, the caller bot's patience ran out first, and the notice it was
    sent to listen for played to nobody.
    """
    from app import db
    from app.models import Queue
    from app.tenancy import workspace_context

    data = request.get_json(silent=True) or {}
    raw_workspace = data.get('workspace_id')
    queue_slug = (data.get('queue_slug') or '').strip()
    if not raw_workspace or not queue_slug:
        return jsonify({'error': 'workspace_id and queue_slug are required'}), 400

    workspace_id = raw_workspace
    if not isinstance(raw_workspace, int) and not str(raw_workspace).isdigit():
        from app.services.workspace_session import resolve_workspace_id
        workspace_id = resolve_workspace_id(str(raw_workspace))
        if workspace_id is None:
            return jsonify({'error': f'unknown workspace {raw_workspace!r}'}), 404
    else:
        workspace_id = int(raw_workspace)

    with workspace_context(None):
        queue = Queue.query.filter_by(
            slug=queue_slug, workspace_id=workspace_id,
        ).first()
        if queue is None:
            return jsonify({'error': f"queue '{queue_slug}' not found"}), 404

        for field in ('language_fallback_policy', 'language_wait_seconds',
                      'max_wait_before_ai_fallback', 'routing_strategy'):
            if field in data:
                setattr(queue, field, data[field])
        db.session.commit()
        logger.info(
            "configure-queue: '%s' policy=%s wait=%ss cap=%ss",
            queue_slug, queue.language_fallback_policy,
            queue.language_wait_seconds, queue.max_wait_before_ai_fallback,
        )
        return jsonify(queue.to_dict())


@harness_bp.route('/seed-agent', methods=['POST'])
@require_internal_auth
def seed_agent():
    """Put a human agent in a queue, available, with declared languages.

    The missing half of the harness: it could always place a CALLER, and
    never produce an agent for that caller to reach — so every scenario to
    date stops at the AI. Nothing here needs a browser. Availability is a
    Redis status and queue membership is a QueueAgentAssignment row; the
    agent desktop is just one client of the same two operations. What still
    needs a browser is the agent's AUDIO (a WebRTC leg) and anything drawn on
    their screen — but routing decisions, the language fallback, and every
    announcement the CALLER hears happen before an agent leg exists at all.

    Body:
        workspace_id: the run's demo workspace (from /api/demo/start)
        queue_slug:   queue to activate them on
        languages:    BCP-47 codes they speak, e.g. ["en-US"]
        name:         optional label, defaults to 'harness-agent'
        status:       optional, defaults to 'available'
    """
    from app import db
    from app.models import Queue, QueueAgentAssignment, User
    from app.services.queue_service import QueueService
    from app.services.redis_service import get_redis_client
    from app.tenancy import workspace_context

    data = request.get_json(silent=True) or {}
    raw_workspace = data.get('workspace_id')
    queue_slug = (data.get('queue_slug') or '').strip()
    if not raw_workspace or not queue_slug:
        return jsonify({'error': 'workspace_id and queue_slug are required'}), 400

    # /api/demo/start returns the workspace's PUBLIC id (a uuid), not the
    # integer primary key — the runner passes through whatever it was given,
    # so accept either. Querying with the uuid raised a 500 on the first live
    # run, after the call had already been provisioned and paired.
    workspace_id = raw_workspace
    if not isinstance(raw_workspace, int) and not str(raw_workspace).isdigit():
        from app.services.workspace_session import resolve_workspace_id
        workspace_id = resolve_workspace_id(str(raw_workspace))
        if workspace_id is None:
            return jsonify({
                'error': f'unknown workspace {raw_workspace!r}',
            }), 404
    else:
        workspace_id = int(raw_workspace)

    languages = data.get('languages') or ['en-US']
    name = (data.get('name') or 'harness-agent').strip()
    status = (data.get('status') or 'available').strip()
    email = f"{name}@harness.invalid"

    with workspace_context(None):
        queue = Queue.query.filter_by(
            slug=queue_slug, workspace_id=workspace_id,
        ).first()
        if queue is None:
            return jsonify({
                'error': f"queue '{queue_slug}' not found in workspace {workspace_id}",
            }), 404

        agent = User.query.filter_by(
            email=email, workspace_id=workspace_id,
        ).first()
        if agent is None:
            agent = User(email=email, name=name, workspace_id=workspace_id,
                         role='agent', is_active=True)
            agent.set_password('harness-not-a-login')
            db.session.add(agent)
        agent.languages = languages
        # Immediate dispatch skips any selected agent without an address, so
        # an agent seeded without one looks available and is never actually
        # assigned — a silent no-op that would make a scenario lie.
        agent.signalwire_address = agent.signalwire_address or f"/private/{name}"
        db.session.commit()

        assignment = QueueAgentAssignment.query.filter_by(
            queue_id=queue.id, user_id=agent.id,
        ).first()
        if assignment is None:
            assignment = QueueAgentAssignment(queue_id=queue.id, user_id=agent.id)
            db.session.add(assignment)
        assignment.is_activated = True
        assignment.skill_level = data.get('skill_level', 5)
        db.session.commit()

        qs = QueueService(get_redis_client(), workspace_id=workspace_id)
        # Rehydrate EXPLICITLY. set_agent_status only rebuilds the activation
        # sets on a TRANSITION into available (status == 'available' and
        # previous != 'available'), so seeding a second queue for an agent who
        # is already available silently did nothing: the assignment row
        # existed, the Redis set did not, and the caller routed to that queue
        # found no candidates. Two live runs died on exactly that, and this
        # endpoint's own response said so — available_agents came back []
        # for the second and third queue while reporting success.
        qs._rehydrate_queue_activations(str(agent.id))
        qs.set_agent_status(str(agent.id), status)
        qs._rehydrate_queue_activations(str(agent.id))

        logger.info(
            "seed-agent: %s (id=%s) languages=%s activated on '%s' status=%s",
            email, agent.id, languages, queue_slug, status,
        )
        return jsonify({
            'agent_id': agent.id,
            'email': email,
            'languages': languages,
            'queue_slug': queue_slug,
            'status': status,
            'available_agents': qs.get_available_agents(queue_slug),
        })


@harness_bp.route('/call-report', methods=['GET'])
@require_internal_auth
def call_report():
    """Ground truth for one test call: Call row + transcript + contact.

    Query params:
        phone: the bot's caller number (E.164-ish; normalized here)
        since: ISO timestamp — only consider calls created after it, so two
               missions in one run can't read each other's call.

    Platform-scoped on purpose (the harness must find the call whichever
    demo workspace the pairing landed it in) — this route is internal-auth'd
    and enabled only on operator deployments, mirroring /api/internal/*.
    """
    from app.models import Call, Contact
    from app.models.transcription import Transcription
    from app.tenancy import workspace_context
    from app import db

    phone = (request.args.get('phone') or '').strip()
    norm = Contact.normalize_phone(phone)
    if not norm:
        return jsonify({'error': 'phone query param is required'}), 400
    since = None
    raw_since = (request.args.get('since') or '').strip()
    if raw_since:
        try:
            since = datetime.fromisoformat(raw_since.replace('Z', '+00:00'))
            since = since.replace(tzinfo=None)  # DB stores naive UTC
        except ValueError:
            return jsonify({'error': 'since must be ISO-8601'}), 400

    with workspace_context(None):
        query = Call.query.filter(Call.from_number.in_([phone, norm]))
        if since is not None:
            query = query.filter(Call.created_at > since)
        # EARLIEST call after `since`, not latest: the runner passes the
        # dial moment as `since`, and under concurrent activity from the
        # same number the newest row can belong to someone else's call
        # (observed 2026-08-10 when a parallel session's verification call
        # interleaved with a scenario run).
        call = query.order_by(Call.created_at.asc()).first()
        if call is None:
            return jsonify({'found': False}), 404

        rows = (
            db.session.query(Transcription)
            .filter_by(call_id=call.id)
            .order_by(Transcription.sequence_number.asc())
            .all()
        )
        contact = (
            db.session.get(Contact, call.contact_id) if call.contact_id else None
        )

        report = {
            'found': True,
            'call': {
                'id': call.id,
                'sid': call.signalwire_call_sid,
                'workspace_id': call.workspace_id,
                'contact_id': call.contact_id,
                'direction': call.direction,
                'handler_type': call.handler_type,
                'ai_agent_name': call.ai_agent_name,
                'status': call.status,
                'end_reason': call.end_reason,
                'queue_id': call.queue_id,
                'caller_language': call.caller_language,
                # The language-fallback decision. Absent from this report, an
                # assertion on it resolves to None and reads as a product
                # failure when it is really a harness gap — which is exactly
                # what happened on the first sofia run.
                'needs_translation': call.needs_translation,
                'assigned_agent_id': call.assigned_agent_id,
                'duration': call.duration,
                'summary': call.summary,
                'disposition_code': call.disposition_code,
                'agent_notes': call.agent_notes,
                'ai_context': call.ai_context_dict,
                'created_at': call.created_at.isoformat() if call.created_at else None,
                'ended_at': call.ended_at.isoformat() if call.ended_at else None,
            },
            'transcript': [
                {
                    'sequence': r.sequence_number,
                    'speaker': r.speaker,
                    'text': r.transcript,
                    'is_final': r.is_final,
                }
                for r in rows
            ],
            'contact': None,
        }

        # AI tool invocations, as persisted for Call Detail. This is the only
        # ground truth for them that outlives the call: they are broadcast on
        # the live Event Stream, but the always-on producer (/post-prompt
        # swaig_log) fires as the AI session ends, so on an AI-only call the
        # panel is already gone. A scenario asserting on these is asserting
        # exactly what a human would see after hanging up.
        from app.models import WebhookEvent
        tool_events = (
            WebhookEvent.query
            .filter_by(call_id=call.id, event_type='ai_tool_call')
            .order_by(WebhookEvent.created_at.asc())
            .all()
        )
        report['tool_calls'] = [
            {
                'function_name': (e.payload or {}).get('function_name'),
                'arguments': (e.payload or {}).get('arguments') or {},
                'source': (e.payload or {}).get('source'),
                'ai_session_id': (e.payload or {}).get('ai_session_id'),
                'response_excerpt': (e.payload or {}).get('response_excerpt'),
                'at': e.created_at.isoformat() if e.created_at else None,
            }
            for e in tool_events
            if isinstance(e.payload, dict)
        ]
        # Joined for the harness's string ops (contains/contains_any) — the
        # assertion vocabulary has no list-membership operator.
        # Name, arguments AND result in one assertable string. A scenario
        # that can only see the tool NAME cannot tell "looked up the product
        # the caller asked about and was told we don't sell it" from "looked
        # up something else" — and that distinction is the whole point of
        # asserting on a catalog lookup.
        report['tool_calls_text'] = ' | '.join(
            '{}({}) -> {}'.format(
                t['function_name'] or '',
                json.dumps(t['arguments'], sort_keys=True),
                t['response_excerpt'] or '',
            )
            for t in report['tool_calls']
        )
        report['tool_call_names'] = ','.join(
            t['function_name'] or '' for t in report['tool_calls']
        )
        # ONE Call row spans several AI sessions: triage hands off to a
        # specialist, and each fires its own post_prompt. A bare "any tool
        # persisted" check is therefore satisfiable by the early triage
        # session alone — which is NOT evidence that the final session's
        # backfill landed, and the final session is the one whose post_prompt
        # races the panel teardown. Counting distinct sessions is what lets a
        # scenario tell those apart.
        sessions = []
        for t in report['tool_calls']:
            sid = t['ai_session_id']
            if sid and sid not in sessions:
                sessions.append(sid)
        report['tool_call_sessions'] = sessions
        report['tool_call_session_count'] = len(sessions)
        # Names from the LAST session that persisted anything. Read with
        # tool_call_session_count: on its own it cannot prove the terminal
        # session persisted (if that session wrote nothing, this reports the
        # previous one instead) — the count is what exposes the difference.
        last_session = sessions[-1] if sessions else None
        report['tool_call_last_session_names'] = ','.join(
            t['function_name'] or '' for t in report['tool_calls']
            if last_session and t['ai_session_id'] == last_session
        )
        if contact is not None:
            # Queue-scenario ground truth: the durable promise minted by a
            # hold timeout (or an explicit callback request) for this caller.
            from app.models.callback import Callback
            pending_cb = Callback.find_pending_for_contact(contact.id)
            report['pending_callback'] = (
                {
                    'reason': pending_cb.reason,
                    'requested_at': (
                        pending_cb.requested_at.isoformat()
                        if pending_cb.requested_at else None
                    ),
                }
                if pending_cb is not None else None
            )
            report['callback_count'] = (
                db.session.query(Callback)
                .filter_by(contact_id=contact.id)
                .count()
            )
            report['contact'] = {
                'id': contact.id,
                'phone': contact.phone,
                'first_name': contact.first_name,
                'last_name': contact.last_name,
                'display_name': contact.display_name,
                'computed_display_name': contact.computed_display_name,
                'total_calls': contact.total_calls,
                'preferred_language': contact.preferred_language,
                'interaction_digest': contact.interaction_digest_list,
                'last_interaction_at': (
                    contact.last_interaction_at.isoformat()
                    if contact.last_interaction_at else None
                ),
            }
    return jsonify(report)


# Statuses in which a dispatched call is worth answering. Mirrors
# call_control.ACTIVE_AGENT_CALL_STATUSES plus 'connecting', which is where a
# push-dispatched call sits between selection and the caller's next hold cycle.
_ANSWERABLE_STATUSES = ('assigned', 'connecting', 'active', 'answered', 'on_hold')


@harness_bp.route('/answer-as-agent', methods=['POST'])
@require_internal_auth
def answer_as_agent():
    """Put a SYNTHETIC AGENT on a dispatched call - no browser, no WebRTC.

    The agent desktop's audio leg is not special. It is whatever executes the
    SWML that ``/api/conferences/agent-conference`` serves for a
    ``conference_join`` token, and that document is just::

        ["answer", {"join_conference": {"name": <conf>, ...}}]

    Nothing in it is browser-shaped. Verified live 2026-08-19: a REST-created
    call dialled at the Fabric address fetched and ran that document
    (``POST /api/conferences/agent-conference 200`` in the nginx access log).

    So a synthetic agent is the same two steps the real desktop takes:

      1. ``POST /api/conferences/prepare-join``  as the agent, with a real JWT
      2. dial ``<AGENT_CONFERENCE_RESOURCE>?token=<token>``

    Step 1 goes over HTTP to this same backend instead of re-implementing the
    token mint, so that endpoint's own authorization actually runs - including
    the pinning that ignores a body-supplied ``agent_id``. Re-implementing it
    here would test the harness rather than the product.

    The leg joins SILENT: its origin is a PSTN number with no media source.
    That still covers the conference bridge, assignment state, return-to-queue,
    backup/escalate, the supervisor shapes and wrap-up. Giving the synthetic
    agent a VOICE is a separate job - ``/api/conferences/ai/<name>/join``
    already joins a talking AI into a conference and is the intended route.

    Note ``join_conference.end_on_exit`` is true: when this leg hangs up, the
    conference ends. Teardown order matters in a scenario.

    Body:
        workspace_id: the run's demo workspace (public uuid or integer pk)
        name:         harness agent label, as passed to /seed-agent
        wait_seconds: how long to wait for a dispatch (default 90, cap 100 -
                      gunicorn's request timeout is 120)
        call_sid:     optional; answer this specific call rather than
                      whichever one is currently assigned
    """
    import os
    import time

    import requests as _requests

    from app import db
    from app.models import Call, User
    from app.tenancy import workspace_context
    from app.utils.jwt_utils import generate_tokens

    data = request.get_json(silent=True) or {}
    raw_workspace = data.get('workspace_id')
    if not raw_workspace:
        return jsonify({'error': 'workspace_id is required'}), 400

    # Same dual-shape acceptance as /seed-agent: /api/demo/start hands the
    # runner a public uuid, not the integer pk.
    workspace_id = raw_workspace
    if not isinstance(raw_workspace, int) and not str(raw_workspace).isdigit():
        from app.services.workspace_session import resolve_workspace_id
        workspace_id = resolve_workspace_id(str(raw_workspace))
        if workspace_id is None:
            return jsonify({'error': f'unknown workspace {raw_workspace!r}'}), 404
    else:
        workspace_id = int(raw_workspace)

    name = (data.get('name') or 'harness-agent').strip()
    email = (data.get('email') or f'{name}@harness.invalid').strip()
    # This endpoint mints a JWT, which is a real credential. It may only ever
    # do so for an agent the harness itself created - never an operator's
    # account, whatever the body asks for.
    if not email.endswith('@harness.invalid'):
        return jsonify({
            'error': 'refusing to mint a token for a non-harness user',
        }), 403

    try:
        wait_seconds = min(int(data.get('wait_seconds') or 90), 100)
    except (TypeError, ValueError):
        wait_seconds = 90
    want_sid = (data.get('call_sid') or '').strip() or None

    with workspace_context(None):
        agent = User.query.filter_by(
            email=email, workspace_id=workspace_id,
        ).first()
        if agent is None:
            return jsonify({
                'error': f'no harness agent {email!r} in workspace {workspace_id}',
            }), 404

        agent_id = agent.id
        workspace = agent.workspace
        extra_claims = (
            {'wsid': workspace.public_id} if workspace is not None else None
        )

        # Wait for the dispatch. A push-dispatched caller is only assigned when
        # their hold cycle comes round, so this legitimately takes tens of
        # seconds - polling here keeps that wait off the runner.
        deadline = time.time() + wait_seconds
        call = None
        while True:
            db.session.expire_all()
            query = Call.query.filter(
                Call.assigned_agent_id == agent_id,
                Call.conference_name.isnot(None),
                Call.status.in_(_ANSWERABLE_STATUSES),
            )
            if want_sid:
                query = query.filter(Call.signalwire_call_sid == want_sid)
            call = query.order_by(Call.id.desc()).first()
            if call is not None or time.time() >= deadline:
                break
            time.sleep(2)

        if call is None:
            # 200, not an error: "nobody was dispatched to me" is a result a
            # scenario may want to assert on, not a harness malfunction.
            return jsonify({
                'answered': False,
                'reason': f'no call dispatched to this agent within {wait_seconds}s',
                'agent_id': agent_id,
            })

        conference_name = call.conference_name
        call_db_id = call.id
        call_sid = call.signalwire_call_sid
        tokens = generate_tokens(agent_id, extra_claims=extra_claims)

    base = os.getenv('BACKEND_INTERNAL_URL', 'http://localhost:5000')

    try:
        prepared = _requests.post(
            f'{base}/api/conferences/prepare-join',
            timeout=20,
            headers={'Authorization': f"Bearer {tokens['access_token']}"},
            json={'conference_name': conference_name, 'call_id': str(call_db_id)},
        )
    except _requests.RequestException as exc:
        return jsonify({'answered': False,
                        'reason': f'prepare-join unreachable: {exc}'}), 502
    if prepared.status_code >= 300:
        return jsonify({
            'answered': False,
            'reason': f'prepare-join HTTP {prepared.status_code}: '
                      f'{prepared.text[:200]}',
        }), 502

    dial_address = (prepared.json() or {}).get('dial_address')
    if not dial_address:
        return jsonify({'answered': False,
                        'reason': 'prepare-join returned no dial_address'}), 502

    space = os.getenv('SIGNALWIRE_SPACE')
    project = os.getenv('SIGNALWIRE_PROJECT_ID')
    api_token = os.getenv('SIGNALWIRE_API_TOKEN')
    from_number = (os.getenv('SIGNALWIRE_FROM_NUMBER')
                   or os.getenv('SIGNALWIRE_PHONE_NUMBER'))
    if not all([space, project, api_token, from_number]):
        return jsonify({
            'answered': False,
            'reason': 'SignalWire credentials or from-number not configured',
        }), 500

    # `url` is NOT what drives this leg - the Fabric resource supplies the
    # document. But the Calling API refuses a dial carrying neither inline SWML
    # nor a webhook url (422 inline_swml_or_swml_webhook_required) and then
    # ignores what it was given: the 200 response echoes ``url: null``. The
    # placeholder points at a real SWML document rather than a dead URL, so if
    # that behaviour ever changes the failure is a legible out-of-service
    # message instead of a silent dead leg.
    placeholder_swml = f'{base}/api/swml/out-of-service'

    try:
        dialed = _requests.post(
            f'https://{space}/api/calling/calls',
            auth=(project, api_token), timeout=30,
            json={'command': 'dial', 'params': {
                'from': from_number,
                'to': dial_address,
                'url': placeholder_swml,
            }},
        )
    except _requests.RequestException as exc:
        return jsonify({'answered': False,
                        'reason': f'dial failed: {exc}'}), 502
    if dialed.status_code >= 300:
        return jsonify({
            'answered': False,
            'reason': f'dial HTTP {dialed.status_code}: {dialed.text[:200]}',
        }), 502

    body = dialed.json() if dialed.content else {}
    leg_id = body.get('id') or body.get('call_id')
    logger.info(
        'answer-as-agent: agent %s dialled into conference %s as leg %s',
        agent_id, conference_name, leg_id,
    )
    return jsonify({
        'answered': True,
        'agent_id': agent_id,
        'agent_leg_id': leg_id,
        'call_sid': call_sid,
        'call_db_id': call_db_id,
        'conference_name': conference_name,
    })
