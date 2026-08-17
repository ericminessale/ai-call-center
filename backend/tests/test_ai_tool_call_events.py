"""Event Stream ``ai_tool_call`` emits.

The category was fully rendered frontend but never emitted by the backend, so
the Event Stream sat empty through the AI-tools moment (DEMO_READINESS
"Visitor-facing polish"). These tests pin the three things that can silently
break it again:

  1. the SWAIG envelope parse (platform field naming has moved before),
  2. the payload contract ``CallEventStream.tsx`` filters and renders on,
  3. that /post-prompt — the only always-on source — actually fires it, with
     no demo-mode gate, so a clone-and-own install gets it too.

The fixtures are trimmed copies of real captured payloads
(``backend/captures/postprompt.jsonl`` / ``debug-events.jsonl``).
"""

from types import SimpleNamespace

import pytest

from app.api import webhooks
from app.services import callcenter_socketio


CALL_SID = 'be64b448-7d6d-4ef1-8dcc-cd3632ee2285'


@pytest.fixture(autouse=True)
def _no_real_redis(monkeypatch):
    """Keep the cross-source dedupe off Redis unless a test asks for it.

    ``_tool_call_already_emitted`` calls ``get_redis_client()``, which tries the
    configured host and then an IP fallback — in a test container with no Redis
    that's a connect-and-retry stall, not a fast failure. Defaulting to None
    exercises the fail-open path, which is what every test here except the
    dedupe group actually wants.
    """
    import app.services.redis_service as redis_service
    monkeypatch.setattr(redis_service, 'get_redis_client', lambda: None)


def swaig_envelope(**overrides):
    """One `swaig_call` / `swaig_log` entry in the platform's live shape."""
    envelope = {
        'command_name': 'transfer_to_human',
        'command_arg': '{"customer_name":"Eric","department":"support"}',
        'epoch_time': 1782495317,
        'post_data': {
            'function': 'transfer_to_human',
            'argument': {
                'parsed': [{
                    'customer_name': 'Eric',
                    'department': 'support',
                    'urgency': 'high',
                }],
                'raw': '{"customer_name":"Eric","department":"support"}',
            },
            'call_id': CALL_SID,
            'ai_session_id': 'd99688d7-ea6f-43f9-8232-27a525714498',
            'global_data': {'call_db_id': '132'},
            'meta_data': {'call_db_id': '132'},
        },
    }
    envelope.update(overrides)
    return envelope


def fake_call(**overrides):
    # Every column/method /post-prompt reads off the row. Kept explicit rather
    # than permissive-by-default: when the handler grows a new field, these
    # tests should fail loudly instead of quietly stubbing it as None.
    call = SimpleNamespace(
        id=132,
        signalwire_call_sid=CALL_SID,
        workspace_id=4,
        status='waiting',
        assigned_agent_id=7,
        conference_name=None,
        ai_context=None,
        summary=None,
        disposition_code=None,
        agent_notes=None,
        wrap_up_source=None,
        caller_language=None,
        contact_id=None,
        update_status=lambda *a, **kw: None,
    )
    for key, value in overrides.items():
        setattr(call, key, value)
    return call


# ---------------------------------------------------------------------------
# Envelope parsing
# ---------------------------------------------------------------------------

def test_envelope_yields_tool_name_and_parsed_arguments():
    name, arguments, call_db_id, call_sid = webhooks._swaig_tool_call_fields(
        swaig_envelope()
    )

    assert name == 'transfer_to_human'
    assert arguments == {
        'customer_name': 'Eric',
        'department': 'support',
        'urgency': 'high',
    }
    assert call_db_id == '132'
    assert call_sid == CALL_SID


def test_envelope_falls_back_to_the_raw_argument_json():
    envelope = swaig_envelope()
    del envelope['post_data']['argument']['parsed']

    _name, arguments, _db_id, _sid = webhooks._swaig_tool_call_fields(envelope)

    assert arguments == {'customer_name': 'Eric', 'department': 'support'}


def test_envelope_keeps_unparseable_arguments_rather_than_dropping_them():
    envelope = swaig_envelope(command_arg='not json at all')
    del envelope['post_data']['argument']

    _name, arguments, _db_id, _sid = webhooks._swaig_tool_call_fields(envelope)

    assert arguments == {'raw': 'not json at all'}


def test_envelope_tolerates_a_bare_tool_with_no_arguments():
    # `post_data` is absent on some platform event shapes; the tool name still
    # has to survive, because the name is what the panel renders.
    name, arguments, call_db_id, call_sid = webhooks._swaig_tool_call_fields(
        {'command_name': 'search_knowledge'}
    )

    assert (name, arguments, call_db_id, call_sid) == (
        'search_knowledge', {}, None, None,
    )


def test_native_step_navigation_is_not_reported_as_a_tool_call(monkeypatch):
    """`next_step` / `change_context` share the log but are SDK plumbing.

    They arrive flagged ``native: true`` with no post_data. A real 5-tool
    session logs 8 entries, 3 of them navigation — emitting those would bury
    the actual tool calls in the panel.
    """
    emitted = []
    monkeypatch.setattr(callcenter_socketio, 'emit_call_event',
                        lambda *a, **kw: emitted.append(a))

    for name, arg in (('next_step', '{"step":"route_department"}'),
                      ('change_context', '{"context":"support"}')):
        entry = {'command_name': name, 'command_arg': arg,
                 'epoch_time': 1781025952, 'native': True}
        assert webhooks._emit_swaig_tool_call(entry, source='post_prompt',
                                              call=fake_call()) is False

    assert emitted == []

    # native:false / absent is a real tool and must still go out.
    assert webhooks._emit_swaig_tool_call(
        swaig_envelope(native=False), source='post_prompt', call=fake_call()
    ) is True
    assert len(emitted) == 1


def test_garbage_envelopes_emit_nothing(monkeypatch):
    emitted = []
    monkeypatch.setattr(callcenter_socketio, 'emit_call_event',
                        lambda *a, **kw: emitted.append((a, kw)))

    for entry in (None, [], 'swaig_call', {}, {'post_data': {}}):
        assert webhooks._emit_swaig_tool_call(entry, source='test') is False

    assert emitted == []


# ---------------------------------------------------------------------------
# Emit payload contract (what CallEventStream.tsx consumes)
# ---------------------------------------------------------------------------

class _DedupeRedis:
    """Redis stand-in with real ``SET NX`` semantics."""

    def __init__(self):
        self.store = {}

    def set(self, key, value, **kw):
        if kw.get('nx') and key in self.store:
            return None
        self.store[key] = value
        return True


def _use_redis(monkeypatch, client):
    import app.services.redis_service as redis_service
    monkeypatch.setattr(redis_service, 'get_redis_client', lambda: client)


def test_the_same_invocation_is_emitted_once_across_both_sources(monkeypatch):
    """With DEBUG_WEBHOOK_ENABLED on, one tool call arrives twice — live from
    /debug-events and again in the /post-prompt backfill. The panel must not
    render it twice."""
    events = []
    monkeypatch.setattr(callcenter_socketio, 'emit_call_event',
                        lambda *a, **kw: events.append((a, kw)))
    _use_redis(monkeypatch, _DedupeRedis())
    call = fake_call()

    live = webhooks._emit_swaig_tool_call(
        swaig_envelope(), source='debug_events', call=call)
    backfill = webhooks._emit_swaig_tool_call(
        swaig_envelope(), source='post_prompt', call=call)

    assert live is True, 'whichever path arrives first should emit'
    assert backfill is False, 'the second path must stand down'
    assert len(events) == 1
    assert events[0][0][2]['source'] == 'debug_events'


def test_the_same_function_called_twice_is_two_events(monkeypatch):
    """Dedupe keys on the platform's epoch_time, so a genuine second call to
    the same function is NOT swallowed."""
    events = []
    monkeypatch.setattr(callcenter_socketio, 'emit_call_event',
                        lambda *a, **kw: events.append((a, kw)))
    _use_redis(monkeypatch, _DedupeRedis())
    call = fake_call()

    webhooks._emit_swaig_tool_call(
        swaig_envelope(epoch_time=1782495317), source='debug_events', call=call)
    webhooks._emit_swaig_tool_call(
        swaig_envelope(epoch_time=1782495402), source='debug_events', call=call)

    assert len(events) == 2


def test_two_calls_invoking_the_same_tool_do_not_collide(monkeypatch):
    """The key includes the call, so concurrent callers don't suppress each
    other even on an identical function + timestamp."""
    events = []
    monkeypatch.setattr(callcenter_socketio, 'emit_call_event',
                        lambda *a, **kw: events.append((a, kw)))
    _use_redis(monkeypatch, _DedupeRedis())

    for sid in ('sid-caller-one', 'sid-caller-two'):
        webhooks._emit_swaig_tool_call(
            swaig_envelope(), source='debug_events',
            call=fake_call(signalwire_call_sid=sid))

    assert len(events) == 2


def test_dedupe_fails_open_without_redis(monkeypatch):
    """No Redis: emit. A duplicate row is cosmetic; a dropped tool call makes
    the Event Stream look broken, which is the bug being fixed."""
    events = []
    monkeypatch.setattr(callcenter_socketio, 'emit_call_event',
                        lambda *a, **kw: events.append((a, kw)))
    _use_redis(monkeypatch, None)
    call = fake_call()

    for source in ('debug_events', 'post_prompt'):
        assert webhooks._emit_swaig_tool_call(
            swaig_envelope(), source=source, call=call) is True
    assert len(events) == 2


def test_dedupe_fails_open_when_redis_errors(monkeypatch):
    class _Broken:
        def set(self, *a, **kw):
            raise RuntimeError('redis down')

    events = []
    monkeypatch.setattr(callcenter_socketio, 'emit_call_event',
                        lambda *a, **kw: events.append((a, kw)))
    _use_redis(monkeypatch, _Broken())

    assert webhooks._emit_swaig_tool_call(
        swaig_envelope(), source='debug_events', call=fake_call()) is True
    assert len(events) == 1


def test_an_envelope_without_epoch_time_is_never_deduped(monkeypatch):
    """Unknown shape → emit rather than key on something we can't trust."""
    events = []
    monkeypatch.setattr(callcenter_socketio, 'emit_call_event',
                        lambda *a, **kw: events.append((a, kw)))
    _use_redis(monkeypatch, _DedupeRedis())
    call = fake_call()

    envelope = swaig_envelope()
    envelope.pop('epoch_time')
    for source in ('debug_events', 'post_prompt'):
        webhooks._emit_swaig_tool_call(envelope, source=source, call=call)
    assert len(events) == 2


def test_emit_uses_the_resolved_rows_ids_not_the_payloads(monkeypatch):
    events = []
    monkeypatch.setattr(callcenter_socketio, 'emit_call_event',
                        lambda *a, **kw: events.append((a, kw)))

    # A stale/relabelled sid in the payload must not win over the real row.
    call = fake_call(id=999, signalwire_call_sid='authoritative-sid')
    assert webhooks._emit_swaig_tool_call(
        swaig_envelope(), source='post_prompt', call=call
    ) is True

    (call_id, event_type, data, call_sid), _kwargs = events[0]
    assert call_id == 999
    assert call_sid == 'authoritative-sid'
    assert event_type == 'ai_tool_call'
    # `function_name` is the exact field CallEventStream.tsx renders.
    assert data['function_name'] == 'transfer_to_human'
    assert data['arguments']['department'] == 'support'
    assert data['source'] == 'post_prompt'


def test_event_carries_both_ids_so_the_panel_filter_matches(monkeypatch):
    """The panel is mounted with the SignalWire sid but producers key by DB id.

    ContactDetailView passes ``effectiveCallSid`` as *both* CallEventStream
    props, so an event that carried only the DB id was dropped client-side.
    """
    emissions = []
    monkeypatch.setattr(callcenter_socketio, 'Call', SimpleNamespace(
        query=SimpleNamespace(get=lambda _id: fake_call()),
        find_by_sid=lambda _sid: fake_call(),
    ))
    monkeypatch.setattr(callcenter_socketio.socketio, 'emit',
                        lambda *a, **kw: emissions.append((a, kw)))

    callcenter_socketio.emit_ai_tool_call(
        132, 'search_knowledge', arguments={'query': 'router reset'},
        call_sid=CALL_SID, source='debug_events',
    )

    rooms = {kwargs.get('room') for _args, kwargs in emissions}
    assert CALL_SID in rooms          # call-specific room
    assert 'ws:4' in rooms            # the owning workspace's room

    event = emissions[0][0][1]
    assert event['call_id'] == 132
    assert event['call_sid'] == CALL_SID
    assert event['event_type'] == 'ai_tool_call'
    assert event['data'] == {
        'function_name': 'search_knowledge',
        'arguments': {'query': 'router reset'},
        'source': 'debug_events',
    }
    assert event['timestamp']


def test_workspace_is_resolved_from_the_sid_when_only_the_sid_is_known(
    monkeypatch,
):
    """Otherwise sid-only producers land in the default workspace room, which
    in hosted mode is platform operators — the owning visitor sees nothing."""
    emissions = []
    monkeypatch.setattr(callcenter_socketio, 'Call', SimpleNamespace(
        query=SimpleNamespace(get=lambda _id: None),
        find_by_sid=lambda sid: fake_call() if sid == CALL_SID else None,
    ))
    monkeypatch.setattr(callcenter_socketio.socketio, 'emit',
                        lambda *a, **kw: emissions.append((a, kw)))

    callcenter_socketio.emit_ai_tool_call(
        None, 'report_sentiment', call_sid=CALL_SID,
    )

    assert 'ws:4' in {kwargs.get('room') for _args, kwargs in emissions}


# ---------------------------------------------------------------------------
# /post-prompt — the always-on source
# ---------------------------------------------------------------------------

class _CallQuery:
    """The exact SQLAlchemy chain /post-prompt uses to resolve the call:
    ``db.session.query(Call).filter_by(...).with_for_update().first()``.

    F-07 put the row lock on that lookup, so a stub that only answers
    ``Call.find_by_sid`` is no longer enough — the handler raises before it
    ever reaches the swaig_log block.
    """

    def __init__(self, call):
        self._call = call

    def filter_by(self, **_kwargs):
        return self

    def with_for_update(self):
        return self

    def first(self):
        return self._call


def _run_post_prompt(monkeypatch, payload, call, trace=None, persisted=None):
    """Drive the real handler, stubbing only persistence and transport.

    Calls ``__wrapped__`` to skip @require_webhook_auth so the test doesn't
    depend on WEBHOOK_AUTH env state.

    Pass ``trace`` to capture the ORDER of socket emits (the tool events have
    to precede the terminal call_update, or the panel they render in is gone
    before they arrive), and ``persisted`` to capture WebhookEvent rows.
    """
    from flask import Flask

    events = []

    def _log_event(**kwargs):
        if persisted is not None:
            persisted.append(kwargs)

    monkeypatch.setattr(webhooks, 'capture_webhook_payload',
                        lambda *a, **kw: None)
    monkeypatch.setattr(webhooks, 'Call',
                        SimpleNamespace(find_by_sid=lambda _sid: call))
    monkeypatch.setattr(webhooks, 'db', SimpleNamespace(
        session=SimpleNamespace(commit=lambda: None, rollback=lambda: None,
                                query=lambda _model: _CallQuery(call))))
    monkeypatch.setattr(webhooks, 'WebhookEvent',
                        SimpleNamespace(log_event=_log_event))
    # Caller-memory finalization runs just before the emit block and is not
    # itself under test — it would drive the whole digest/index/stats path
    # against a SimpleNamespace.
    import app.services.contact_enrichment as contact_enrichment
    monkeypatch.setattr(contact_enrichment, 'finalize_call_memory',
                        lambda _call: None)
    monkeypatch.setattr(webhooks, 'socketio',
                        SimpleNamespace(emit=lambda *a, **kw: None))
    def _emit_call_update(_call):
        if trace is not None:
            trace.append('call_update')

    def _emit_call_event(*args, **_kwargs):
        events.append(args)
        if trace is not None:
            trace.append(args[1])

    monkeypatch.setattr(callcenter_socketio, 'emit_call_update',
                        _emit_call_update)
    monkeypatch.setattr(callcenter_socketio, 'emit_call_event',
                        _emit_call_event)

    app = Flask(__name__)
    with app.test_request_context('/api/webhooks/post-prompt', json=payload):
        response = webhooks.post_prompt.__wrapped__()

    return response, events


def test_tool_events_precede_the_terminal_call_update(monkeypatch):
    """Ordering is the whole feature on the default path.

    /post-prompt is the only always-on producer and it fires as the AI session
    ends. For an AI-only call this handler has already set status='ended', so
    the call_update it emits makes the desktop drop the call from activeCalls
    — which unmounts CallEventStream. Emitted after that update, every tool
    event arrives at a component that no longer exists.
    """
    trace = []
    _response, events = _run_post_prompt(
        monkeypatch,
        {'call_id': CALL_SID, 'swaig_log': [swaig_envelope()]},
        fake_call(status='ended'),
        trace=trace,
    )

    assert len(events) == 1
    assert trace.index('ai_tool_call') < trace.index('call_update')


def test_tool_calls_are_persisted_for_call_detail(monkeypatch):
    """A socket event nobody is mounted to receive is not observability.

    The persisted row is what Call Detail reads back after hangup, so it has
    to carry enough to render without the live event: name, arguments, and
    which producer it came from.
    """
    persisted = []
    _response, _events = _run_post_prompt(
        monkeypatch,
        {'call_id': CALL_SID, 'swaig_log': [swaig_envelope()]},
        fake_call(),
        persisted=persisted,
    )

    tool_rows = [row for row in persisted
                 if row.get('event_type') == 'ai_tool_call']
    assert len(tool_rows) == 1
    assert tool_rows[0]['call_id'] == 132
    assert tool_rows[0]['payload']['function_name'] == 'transfer_to_human'
    assert tool_rows[0]['payload']['arguments']['urgency'] == 'high'
    assert tool_rows[0]['payload']['source'] == 'post_prompt'


def test_persist_is_skipped_when_the_call_row_never_resolved(monkeypatch):
    """The fallback id comes from the model's own global_data.

    WebhookEvent.call_id is an FK and log_event commits, so writing an
    unresolvable id would raise at flush and poison the session for the rest
    of post_prompt. Dropping the row is the cheaper failure — but the live
    event still goes out.
    """
    persisted = []
    events = []
    # The /debug-events shape: a tool call can reach us before the Call row
    # exists, leaving only the id the model reported in global_data.
    monkeypatch.setattr(webhooks, 'Call',
                        SimpleNamespace(find_by_sid=lambda _sid: None))
    monkeypatch.setattr(webhooks, 'WebhookEvent', SimpleNamespace(
        log_event=lambda **kw: persisted.append(kw)))
    monkeypatch.setattr(callcenter_socketio, 'emit_call_event',
                        lambda *a, **kw: events.append(a))

    assert webhooks._emit_swaig_tool_call(
        swaig_envelope(), source='debug_events',
    ) is True

    assert persisted == []
    assert len(events) == 1, 'the live event must still go out'


def test_every_event_carries_a_unique_id_for_room_dedupe(monkeypatch):
    """A viewer sits in BOTH the call room and its workspace room, so the one
    logical event is delivered twice. Consumers dedupe on this id.
    """
    emissions = []
    monkeypatch.setattr(callcenter_socketio.socketio, 'emit',
                        lambda *a, **kw: emissions.append((a, kw)))
    monkeypatch.setattr(callcenter_socketio, 'Call', SimpleNamespace(
        query=SimpleNamespace(get=lambda _id: fake_call()),
        find_by_sid=lambda _sid: fake_call(),
    ))

    callcenter_socketio.emit_call_event(
        132, 'ai_tool_call', {'function_name': 'x'}, CALL_SID,
    )

    payloads = [args[1] for args, _kwargs in emissions]
    assert len(payloads) == 2, 'expected the call-room and workspace-room emits'
    # Same logical event -> ONE id, so the receiver can drop the echo.
    assert payloads[0]['event_id'] == payloads[1]['event_id']
    assert payloads[0]['event_id']

    # ...and a genuinely separate invocation must not collide with it.
    emissions.clear()
    callcenter_socketio.emit_call_event(
        132, 'ai_tool_call', {'function_name': 'x'}, CALL_SID,
    )
    assert emissions[0][0][1]['event_id'] != payloads[0]['event_id']


def test_post_prompt_emits_one_ai_tool_call_per_swaig_log_entry(monkeypatch):
    payload = {
        'call_id': CALL_SID,
        'app_name': 'swml app',
        'global_data': {'call_db_id': '132'},
        # Interleaved exactly as a real session logs it: the SDK's native
        # navigation entries sit between the model's own tool calls.
        'swaig_log': [
            swaig_envelope(
                command_name='set_caller_language',
                post_data={
                    'function': 'set_caller_language',
                    'argument': {'parsed': [{'language': 'en-US'}]},
                    'call_id': CALL_SID,
                    'global_data': {'call_db_id': '132'},
                },
            ),
            {'command_name': 'next_step', 'native': True,
             'command_arg': '{"step":"route_department"}'},
            {'command_name': 'change_context', 'native': True,
             'command_arg': '{"context":"support"}'},
            swaig_envelope(),
        ],
    }

    response, events = _run_post_prompt(monkeypatch, payload, fake_call())

    assert response[1] == 200
    assert [event[1] for event in events] == ['ai_tool_call', 'ai_tool_call']
    assert [event[2]['function_name'] for event in events] == [
        'set_caller_language', 'transfer_to_human',
    ]
    # Order matters — the panel renders the stream chronologically.
    assert events[1][2]['arguments']['urgency'] == 'high'
    assert all(event[3] == CALL_SID for event in events)


def test_post_prompt_without_a_swaig_log_emits_nothing(monkeypatch):
    response, events = _run_post_prompt(
        monkeypatch, {'call_id': CALL_SID}, fake_call()
    )

    assert response[1] == 200
    assert events == []


def test_post_prompt_emit_survives_a_malformed_swaig_log(monkeypatch):
    """A bad entry must not cost the wrap-up persistence its 200."""
    payload = {
        'call_id': CALL_SID,
        'swaig_log': ['not-a-dict', swaig_envelope()],
    }

    response, events = _run_post_prompt(monkeypatch, payload, fake_call())

    assert response[1] == 200
    assert len(events) == 1


def test_ai_tool_call_is_not_gated_on_demo_mode(monkeypatch):
    """Clone-and-own installs (demo mode off — the default) must emit too.

    See the two-deployment-shapes rule: demo mode is operator-only, so gating
    this on it would ship the feature to nobody who clones the repo.
    """
    from app.utils import demo_config

    monkeypatch.delenv('TENANCY_MODE', raising=False)
    monkeypatch.delenv('DEMO_MODE', raising=False)
    assert demo_config.is_demo_mode() is False

    _response, events = _run_post_prompt(
        monkeypatch,
        {'call_id': CALL_SID, 'swaig_log': [swaig_envelope()]},
        fake_call(),
    )

    assert len(events) == 1


# ---------------------------------------------------------------------------
# /debug-events — the live (opt-in) source
# ---------------------------------------------------------------------------

def test_debug_events_emits_on_a_swaig_call_and_names_the_event(monkeypatch):
    from flask import Flask

    events = []
    logged = []
    monkeypatch.setattr(webhooks, 'capture_webhook_payload',
                        lambda *a, **kw: None)
    monkeypatch.setattr(webhooks, 'Call',
                        SimpleNamespace(find_by_sid=lambda _sid: fake_call()))
    monkeypatch.setattr(callcenter_socketio, 'emit_call_event',
                        lambda *a, **kw: events.append(a))
    monkeypatch.setattr(webhooks.logger, 'info',
                        lambda msg, *a: logged.append(msg % a if a else msg))

    payload = {
        'call_info': {'call_id': CALL_SID},
        'swaig_call': swaig_envelope(),
    }

    app = Flask(__name__)
    with app.test_request_context('/api/webhooks/debug-events', json=payload):
        response = webhooks.debug_events.__wrapped__()

    assert response[1] == 200
    assert len(events) == 1
    assert events[0][1] == 'ai_tool_call'
    assert events[0][2]['function_name'] == 'transfer_to_human'
    assert events[0][2]['source'] == 'debug_events'
    # The event name is the payload key, not the absent 'label'/'action' field
    # this handler used to read (it logged a constant "unknown").
    assert any('swaig_call' in line for line in logged)


def test_debug_events_ignores_non_tool_telemetry(monkeypatch):
    from flask import Flask

    events = []
    monkeypatch.setattr(webhooks, 'capture_webhook_payload',
                        lambda *a, **kw: None)
    monkeypatch.setattr(callcenter_socketio, 'emit_call_event',
                        lambda *a, **kw: events.append(a))

    payload = {
        'call_info': {'call_id': CALL_SID},
        'llm_response': {'content': 'hello'},
    }

    app = Flask(__name__)
    with app.test_request_context('/api/webhooks/debug-events', json=payload):
        response = webhooks.debug_events.__wrapped__()

    assert response[1] == 200
    assert events == []
