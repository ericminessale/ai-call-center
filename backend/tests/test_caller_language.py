"""Caller-language capture + language-aware transcription (2026-08-11,
maria_language_memory row 29).

Pins the two failure modes from the Spanish synthetic call:
- Call.caller_language must be code-written, not enqueue-path-dependent:
  the internal write-through endpoint persists the tool's value in real
  time, and the post-prompt webhook back-fills from global_data / the
  post-prompt assessment when the tool's POST never landed.
- Every language value the model emits is unverified input —
  normalize_language is the single code gate, so junk ('Spanish', 'null')
  can never reach the column or the contact's durable preferred_language.
"""
import base64
import json
from datetime import datetime, timedelta

import pytest
from flask import Flask

from app import db
from app.models import Call, Contact, User, Workspace
from app.services.call_language import (
    DEFAULT_LANGUAGE,
    derive_call_language,
    normalize_language,
)


# ---------------------------------------------------------------------------
# normalize_language — the PGI gate for model-emitted language values
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('raw, expected', [
    ('es-ES', 'es-ES'),
    ('es-es', 'es-ES'),
    ('ES-es', 'es-ES'),
    ('es_ES', 'es-ES'),
    ('  en-US  ', 'en-US'),
    ('es', 'es-ES'),          # bare primary expands via the known map
    ('pt', 'pt-BR'),
    ('xx-YY', 'xx-YY'),       # shape-valid unknown pair passes through
])
def test_normalize_language_accepts_code_shapes(raw, expected):
    assert normalize_language(raw) == expected


@pytest.mark.parametrize('junk', [
    None, '', '   ', 'Spanish', 'null', 'e', 'es-E', 'es-ESP', '12-34',
    'english please', 'qq',   # unknown bare primary is refused, not guessed
    {'lang': 'es'}, 42,
])
def test_normalize_language_rejects_junk(junk):
    assert normalize_language(junk) is None


# ---------------------------------------------------------------------------
# Shared app fixture
# ---------------------------------------------------------------------------

@pytest.fixture()
def lang_app(monkeypatch):
    # Hermetic stubs for the best-effort side channels the webhook tail hits.
    import app.services.interaction_index as idx
    monkeypatch.setattr(idx, 'index_call_summary', lambda call, entry: True)

    import app.services.callcenter_socketio as cc_sio
    monkeypatch.setattr(cc_sio, 'emit_call_update', lambda call: None)

    from app.api import calls_bp, webhooks_bp
    import app.api.webhooks as webhooks_mod
    monkeypatch.setattr(
        webhooks_mod, 'capture_webhook_payload', lambda kind, data: None,
    )

    # No socket server in a bare test app — swallow the room emits.
    class _StubSio:
        @staticmethod
        def emit(*args, **kwargs):
            return None
    monkeypatch.setattr(webhooks_mod, 'socketio', _StubSio())

    flask_app = Flask(__name__)
    flask_app.config.update(
        SQLALCHEMY_DATABASE_URI='sqlite://',
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        TESTING=True,
    )
    db.init_app(flask_app)
    flask_app.register_blueprint(calls_bp, url_prefix='/api/calls')
    flask_app.register_blueprint(webhooks_bp, url_prefix='/api/webhooks')

    # Internal/webhook auth creds the routes' decorators check.
    monkeypatch.setenv('WEBHOOK_AUTH_USER', 'svc')
    monkeypatch.setenv('WEBHOOK_AUTH_PASSWORD', 'svc-secret')
    monkeypatch.delenv('WEBHOOK_AUTH_REQUIRED', raising=False)

    with flask_app.app_context():
        db.create_all()
        ws = Workspace(name='Lang WS')
        ws_other = Workspace(name='Other WS')
        db.session.add_all([ws, ws_other])
        db.session.flush()
        user = User(workspace_id=ws.id, email='agent@example.test',
                    password_hash='x', name='Agent')
        contact = Contact(workspace_id=ws.id, phone='+15555550100',
                          display_name='Maria', first_name='Maria')
        db.session.add_all([user, contact])
        db.session.commit()
        yield flask_app, ws, ws_other, user, contact
        db.session.remove()
        db.drop_all()


def _auth_header():
    token = base64.b64encode(b'svc:svc-secret').decode()
    return {'Authorization': f'Basic {token}'}


def _call(ws, user, *, contact_id=None, sid='call-lang-1', **kwargs):
    call = Call(
        workspace_id=ws.id,
        user_id=user.id,
        contact_id=contact_id,
        signalwire_call_sid=sid,
        from_number='+15555550100',
        destination='+15555550200',
        destination_type='phone',
        direction='inbound',
        handler_type='ai',
        status='ai_active',
        created_at=datetime.utcnow() - timedelta(minutes=5),
        **kwargs,
    )
    db.session.add(call)
    db.session.commit()
    return call


# ---------------------------------------------------------------------------
# derive_call_language — one derivation for every live_transcribe start site
# ---------------------------------------------------------------------------

def test_derive_prefers_per_call_fact(lang_app):
    _fa, ws, _wso, user, contact = lang_app
    call = _call(ws, user, contact_id=contact.id, caller_language='fr-FR')
    contact.preferred_language = 'es-ES'
    db.session.commit()
    assert derive_call_language(call) == 'fr-FR'


def test_derive_falls_back_to_contact_documented_language(lang_app):
    _fa, ws, _wso, user, contact = lang_app
    contact.preferred_language = 'es-ES'
    db.session.commit()
    call = _call(ws, user, contact_id=contact.id)
    assert derive_call_language(call) == 'es-ES'


def test_derive_refuses_cross_workspace_contact(lang_app):
    """F-02 shape: a mis-bound contact must not leak its language."""
    _fa, ws, ws_other, user, _contact = lang_app
    foreign = Contact(workspace_id=ws_other.id, phone='+15555550999',
                      display_name='Foreign', preferred_language='de-DE')
    db.session.add(foreign)
    db.session.commit()
    call = _call(ws, user, contact_id=foreign.id)
    assert derive_call_language(call) == DEFAULT_LANGUAGE


def test_derive_defaults_when_nothing_known(lang_app):
    _fa, ws, _wso, user, _contact = lang_app
    call = _call(ws, user)
    assert derive_call_language(call) == DEFAULT_LANGUAGE


def test_restart_is_disabled_by_default(lang_app, monkeypatch):
    """Live finding (run 20260811-130839): stop+start against a leg running
    the ai verb kills transcription delivery and the call with it. The real
    restart function must therefore no-op unless explicitly enabled."""
    _fa, ws, _wso, user, _contact = lang_app
    call = _call(ws, user, sid='call-restart-default')

    from app.services import call_language as cl_real
    api_calls = []
    import app.services.signalwire_api as sw
    monkeypatch.setattr(
        sw, 'get_signalwire_api',
        lambda: api_calls.append('constructed'),
    )

    assert cl_real.restart_ai_leg_transcription(call, 'http://test') is False
    assert api_calls == []


# ---------------------------------------------------------------------------
# POST /api/calls/<id>/caller-language — the tool's write-through
# ---------------------------------------------------------------------------

def _post_language(flask_app, call, language, headers=None):
    return flask_app.test_client().post(
        f'/api/calls/{call.id}/caller-language',
        json={'language': language},
        headers=headers if headers is not None else _auth_header(),
    )


def test_write_through_requires_internal_auth(lang_app):
    flask_app, ws, _wso, user, _contact = lang_app
    call = _call(ws, user)
    resp = _post_language(flask_app, call, 'es-ES', headers={})
    assert resp.status_code == 401
    assert Call.query.get(call.id).caller_language is None


def test_write_through_persists_normalized_and_restarts(lang_app, monkeypatch):
    flask_app, ws, _wso, user, _contact = lang_app
    call = _call(ws, user)

    restarts = []
    import app.services.call_language as cl
    monkeypatch.setattr(
        cl, 'restart_ai_leg_transcription',
        lambda c, base_url: restarts.append(c.id) or True,
    )
    import app.api.calls as calls_mod
    monkeypatch.setattr(calls_mod, 'get_base_url', lambda: 'http://test')

    resp = _post_language(flask_app, call, 'es-es')

    assert resp.status_code == 200
    assert resp.get_json()['caller_language'] == 'es-ES'
    assert Call.query.get(call.id).caller_language == 'es-ES'
    # en-US was the effective transcription language → restart needed
    assert restarts == [call.id]


def test_write_through_skips_restart_when_language_already_effective(
        lang_app, monkeypatch):
    """Known es-ES caller confirmed as es-ES: no pointless session gap."""
    flask_app, ws, _wso, user, contact = lang_app
    contact.preferred_language = 'es-ES'
    db.session.commit()
    call = _call(ws, user, contact_id=contact.id)

    restarts = []
    import app.services.call_language as cl
    monkeypatch.setattr(
        cl, 'restart_ai_leg_transcription',
        lambda c, base_url: restarts.append(c.id) or True,
    )
    import app.api.calls as calls_mod
    monkeypatch.setattr(calls_mod, 'get_base_url', lambda: 'http://test')

    resp = _post_language(flask_app, call, 'es-ES')

    assert resp.status_code == 200
    assert Call.query.get(call.id).caller_language == 'es-ES'
    assert restarts == []


def test_write_through_rejects_model_junk(lang_app):
    flask_app, ws, _wso, user, _contact = lang_app
    call = _call(ws, user)
    resp = _post_language(flask_app, call, 'Spanish')
    assert resp.status_code == 400
    assert Call.query.get(call.id).caller_language is None


def test_write_through_defers_to_human_live_translate(lang_app, monkeypatch):
    flask_app, ws, _wso, user, _contact = lang_app
    call = _call(ws, user, caller_language='pt-BR', needs_translation=True)

    restarts = []
    import app.services.call_language as cl
    monkeypatch.setattr(
        cl, 'restart_ai_leg_transcription',
        lambda c, base_url: restarts.append(c.id) or True,
    )

    resp = _post_language(flask_app, call, 'es-ES')

    assert resp.status_code == 200
    assert Call.query.get(call.id).caller_language == 'pt-BR'
    assert restarts == []


# ---------------------------------------------------------------------------
# /api/webhooks/post-prompt — the back-fill safety net
# ---------------------------------------------------------------------------

def _post_prompt(flask_app, call, *, global_data=None, parsed=None):
    payload = {
        'call_id': call.signalwire_call_sid,
        'app_name': 'test app',
        'global_data': global_data or {},
        'post_prompt_data': {
            'raw': 'summary text',
            'parsed': [parsed] if parsed is not None else [],
        },
    }
    return flask_app.test_client().post(
        '/api/webhooks/post-prompt',
        data=json.dumps(payload),
        content_type='application/json',
        headers=_auth_header(),
    )


def test_post_prompt_seeds_column_from_tool_written_global_data(lang_app):
    flask_app, ws, _wso, user, contact = lang_app
    call = _call(ws, user, contact_id=contact.id)

    resp = _post_prompt(flask_app, call,
                        global_data={'caller_language': 'es-ES'})

    assert resp.status_code == 200
    assert Call.query.get(call.id).caller_language == 'es-ES'
    # …and the contact's durable language seeds from the same signal.
    assert Contact.query.get(contact.id).preferred_language == 'es-ES'


def test_post_prompt_falls_back_to_validated_assessment(lang_app):
    flask_app, ws, _wso, user, _contact = lang_app
    call = _call(ws, user, sid='call-lang-2')

    resp = _post_prompt(flask_app, call,
                        parsed={'caller_language': 'es-ES'})

    assert resp.status_code == 200
    assert Call.query.get(call.id).caller_language == 'es-ES'


def test_post_prompt_never_persists_junk_assessment(lang_app):
    flask_app, ws, _wso, user, contact = lang_app
    call = _call(ws, user, contact_id=contact.id, sid='call-lang-3')

    resp = _post_prompt(flask_app, call,
                        parsed={'caller_language': 'Spanish'})

    assert resp.status_code == 200
    assert Call.query.get(call.id).caller_language is None
    assert Contact.query.get(contact.id).preferred_language is None


def test_post_prompt_backfill_restarts_live_transcription(lang_app, monkeypatch):
    """AI→AI chain: the triage session's post-prompt lands while the
    specialist is still mid-conversation — the back-fill must flip live
    transcription to the learned language for the rest of the call."""
    flask_app, ws, _wso, user, _contact = lang_app
    call = _call(ws, user, sid='call-lang-5')

    restarts = []
    import app.services.call_language as cl
    monkeypatch.setattr(
        cl, 'restart_ai_leg_transcription',
        lambda c, base_url: restarts.append(c.id) or True,
    )
    import app.utils.url_utils as uu
    monkeypatch.setattr(uu, 'get_base_url', lambda: 'http://test')

    # transferred_to_ai outcome leaves the call ai_active (live)
    resp = _post_prompt(flask_app, call,
                        global_data={'caller_language': 'es-ES'},
                        parsed={'outcome': 'transferred_to_ai'})

    assert resp.status_code == 200
    assert Call.query.get(call.id).caller_language == 'es-ES'
    assert restarts == [call.id]


def test_post_prompt_backfill_skips_restart_when_call_just_closed(
        lang_app, monkeypatch):
    flask_app, ws, _wso, user, _contact = lang_app
    call = _call(ws, user, sid='call-lang-6')

    restarts = []
    import app.services.call_language as cl
    monkeypatch.setattr(
        cl, 'restart_ai_leg_transcription',
        lambda c, base_url: restarts.append(c.id) or True,
    )

    # Empty outcome + no assigned agent/conference → the handler itself
    # closes the call; a closed call must not get a fresh transcribe session.
    resp = _post_prompt(flask_app, call,
                        global_data={'caller_language': 'es-ES'})

    assert resp.status_code == 200
    assert Call.query.get(call.id).caller_language == 'es-ES'
    assert restarts == []


def test_post_prompt_does_not_overwrite_existing_column(lang_app):
    flask_app, ws, _wso, user, _contact = lang_app
    call = _call(ws, user, sid='call-lang-4', caller_language='en-US')

    resp = _post_prompt(flask_app, call,
                        global_data={'caller_language': 'es-ES'})

    assert resp.status_code == 200
    assert Call.query.get(call.id).caller_language == 'en-US'
