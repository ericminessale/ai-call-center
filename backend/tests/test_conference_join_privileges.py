"""Privileged conference-join shapes must come from a server-minted token.

``/api/conferences/agent-conference`` is an unauthenticated SWML webhook (only
SignalWire is supposed to call it) that harvests params from ~10 request
locations, including the raw query string of the dialled address. Every
privileged member shape — silent monitor, whisper/coach audio, barge, takeover
— is authorized by a separate endpoint that mints a ``conference_join:<token>``
Redis entry. These tests pin that the webhook honours those shapes ONLY from
such a token, so the permission gates on the observer endpoints can't be
side-stepped by dialling the public resource with your own query string.
"""
import json

import pytest
from flask import Flask

from app.api import conferences as conferences_api


class _FakeRedis:
    """Only ``get`` is exercised by the webhook."""

    def __init__(self, entries=None):
        self.entries = entries or {}

    def get(self, key):
        value = self.entries.get(key)
        return None if value is None else json.dumps(value)


@pytest.fixture()
def webhook_client(monkeypatch):
    """Bare app with the conferences blueprint; DB/base-URL stubbed out.

    Returns ``(client, set_token)`` — call ``set_token(token, payload)`` to
    stage a Redis join token, or leave it unset for the no-token case.
    """
    app = Flask(__name__)
    app.config.update(TESTING=True)
    app.register_blueprint(conferences_api.conferences_bp, url_prefix='/api/conferences')

    store = _FakeRedis()
    monkeypatch.setattr(conferences_api, 'redis_client', store)
    monkeypatch.setattr(conferences_api, 'get_base_url', lambda: 'https://demo.invalid')

    def set_token(token, payload):
        store.entries[f'conference_join:{token}'] = payload

    return app.test_client(), set_token


def _join_params(swml):
    """Pull the join_conference params out of a webhook SWML response."""
    main = swml['sections']['main']
    for step in main:
        if isinstance(step, dict) and 'join_conference' in step:
            return step['join_conference']
    return None


# ---------------------------------------------------------------------------
# Untrusted params can only produce a NORMAL join
# ---------------------------------------------------------------------------


@pytest.mark.parametrize('privileged_type', ['whisper', 'barge', 'monitor', 'escalation'])
def test_dialled_address_cannot_request_a_privileged_join(webhook_client, privileged_type):
    """The live vector: SignalWire hands us the DIALLED address in ``To``,
    query string and all, and that string is parsed into parsed_params. So
    dialling ``/public/agent-conference-swml?type=whisper&…`` is how an
    attacker-chosen join type reached the SWML builder.
    """
    client, _set_token = webhook_client

    resp = client.post(
        '/api/conferences/agent-conference',
        data={'To': (
            f'/public/agent-conference-swml?conf=interaction-abc123&agent_id=4'
            f'&type={privileged_type}&whisper_mode=true'
            f'&agent_call_sid=call-victim-leg'
        )},
    )

    assert resp.status_code == 200
    params = _join_params(resp.get_json())
    assert params is not None
    assert params['name'] == 'interaction-abc123'
    # No coach audio, and none of the silent/persistent-membership flags.
    assert 'coach' not in params
    assert params.get('muted') is not True
    # Falls through to the ordinary agent join.
    assert params['end_on_exit'] is True
    assert params['beep'] == 'onEnter'


@pytest.mark.parametrize('privileged_type', ['whisper', 'barge', 'monitor'])
def test_direct_query_string_cannot_request_a_privileged_join(
    webhook_client, privileged_type,
):
    """Belt-and-braces on the endpoint's own query string.

    Note `type` is NOT read from request.args today (the "Source 1: URL query
    params" comment in the handler describes something the code never does), so
    this passes even unfixed — it's here to keep it that way if someone ever
    wires request.args into parsed_params.
    """
    client, _set_token = webhook_client

    resp = client.post(
        '/api/conferences/agent-conference'
        f'?conf=interaction-abc123&agent_id=4&type={privileged_type}'
        '&agent_call_sid=call-victim-leg'
    )

    params = _join_params(resp.get_json())
    assert 'coach' not in params
    assert params['end_on_exit'] is True


def test_json_body_cannot_request_takeover(webhook_client):
    """Takeover ends the AI leg and bridges the caller to the customer — the
    single most damaging shape to leave reachable from raw params."""
    client, _set_token = webhook_client

    resp = client.post(
        '/api/conferences/agent-conference',
        json={
            'conf': 'interaction-abc123',
            'agent_id': 4,
            'vars': {'type': 'takeover', 'call_sid': 'call-customer'},
        },
    )

    body = resp.get_json()
    rendered = json.dumps(body)
    assert 'execute_rpc' not in rendered
    assert 'call:call-customer' not in rendered
    assert _join_params(body)['name'] == 'interaction-abc123'


def test_context_pii_is_not_played_from_the_query_string(webhook_client):
    """`context` becomes pre-join TTS of AI-collected caller details."""
    client, _set_token = webhook_client

    resp = client.post(
        '/api/conferences/agent-conference',
        json={
            'conf': 'interaction-abc123',
            'agent_id': 4,
            'vars': {'context': {'customer_name': 'Jane Doe', 'account_number': '9911'}},
        },
    )

    rendered = json.dumps(resp.get_json())
    assert 'Jane Doe' not in rendered
    assert '9911' not in rendered


# ---------------------------------------------------------------------------
# A minted token still works
# ---------------------------------------------------------------------------


def test_token_grants_whisper_coach_shape(webhook_client):
    client, set_token = webhook_client
    set_token('tok-whisper', {
        'agent_id': '7',
        'conf': 'interaction-abc123',
        'type': 'whisper',
        'agent_call_sid': 'call-agent-leg',
    })

    resp = client.post('/api/conferences/agent-conference?token=tok-whisper')

    params = _join_params(resp.get_json())
    assert params['coach'] == 'call-agent-leg'
    assert params['beep'] == 'false'
    assert params['end_on_exit'] is False


def test_token_grants_monitor_shape(webhook_client):
    client, set_token = webhook_client
    set_token('tok-monitor', {
        'agent_id': '7', 'conf': 'interaction-abc123', 'type': 'monitor',
    })

    params = _join_params(
        client.post('/api/conferences/agent-conference?token=tok-monitor').get_json()
    )
    assert params['muted'] is True
    assert params['start_on_enter'] is False


def test_token_conference_wins_over_query_string(webhook_client):
    """Otherwise a valid token for your own call could be re-pointed at
    someone else's conference with `?token=<valid>&conf=<other>`."""
    client, set_token = webhook_client
    set_token('tok-monitor', {
        'agent_id': '7', 'conf': 'interaction-mine', 'type': 'monitor',
    })

    resp = client.post(
        '/api/conferences/agent-conference'
        '?token=tok-monitor&conf=interaction-someone-else'
    )

    assert _join_params(resp.get_json())['name'] == 'interaction-mine'


def test_legacy_bare_conf_join_still_works(webhook_client):
    """No token at all is the documented legacy normal-participant join."""
    client, _set_token = webhook_client

    resp = client.post(
        '/api/conferences/agent-conference?conf=interaction-abc123&agent_id=4'
    )

    params = _join_params(resp.get_json())
    assert params['name'] == 'interaction-abc123'
    assert params['end_on_exit'] is True
    assert params['beep'] == 'onEnter'
