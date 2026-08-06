"""Two narrow guards from the 2026-08-05 verification audit.

1. ``build_whisper_text`` (F-14 / T-10): the pre-join whisper is what a human
   agent hears BEFORE they join, and it used to include the AI's summary only
   when no issue/reason was present. An escalation from an AI specialist
   virtually always has a reason, so the specialist's ``work_summary`` — the
   entire point of the F-14 fix — was dropped on exactly the calls it was
   built for.

2. ``scrub_embedded_credentials`` (A-1 / T-9, scrub half): SignalWire echoes
   our signed callback URLs back inside post-prompt payloads, so a raw payload
   carries the install's WEBHOOK_AUTH credential — which is also the
   INTERNAL_AUTH fallback and the ctk signing key. Persisted and served
   payloads must never contain it.
"""
import json

from app.api.conferences import build_whisper_text
from app.utils.request_logging import scrub_embedded_credentials


# ---------------------------------------------------------------------------
# build_whisper_text
# ---------------------------------------------------------------------------

def test_whisper_includes_ai_summary_even_when_a_reason_is_present():
    """The regression case: escalations carry BOTH a reason and a summary."""
    text = build_whisper_text({
        'customer_name': 'Fred',
        'issue_description': 'vacuum loses suction',
        'ai_summary': 'Tried filter clean and hose check; suction still weak. '
                      'Suggest motor inspection.',
    })

    assert 'Fred' in text
    assert 'vacuum loses suction' in text
    assert 'motor inspection' in text, 'specialist work_summary must be spoken'


def test_whisper_clamps_a_runaway_summary():
    text = build_whisper_text({
        'issue': 'thing',
        'ai_summary': 'y' * 900,
    })

    assert len(text) < 500
    assert '...' in text


def test_whisper_summary_alone_still_works():
    text = build_whisper_text({'ai_summary': 'Caller wants a refund.'})
    assert 'refund' in text


def test_whisper_empty_context_is_empty_string():
    assert build_whisper_text({}) == ''


# ---------------------------------------------------------------------------
# scrub_embedded_credentials
# ---------------------------------------------------------------------------

def test_scrub_removes_userinfo_credentials_at_any_depth():
    payload = {
        'swaig_log': [
            {'delayed_post_response': {'action': [
                {},
                {'SWML': {'sections': {'main': [
                    {'transfer': {'dest':
                     'https://wh_user:s3cr3t-44-char-password@x.ngrok.app/api/webhooks/post-prompt'}},
                ]}}},
            ]}},
        ],
    }

    out = json.dumps(scrub_embedded_credentials(payload))

    assert 's3cr3t' not in out
    assert 'wh_user' not in out
    assert '***:***@x.ngrok.app' in out
    # The URL shape — the actual debug value — survives.
    assert '/api/webhooks/post-prompt' in out


def test_scrub_leaves_ordinary_urls_and_colons_alone():
    payload = {
        'url': 'https://example.com/a/b?x=1',
        'time': '12:34:56',
        'ratio': 'a:b@c',
        'ws': 'wss://relay.example.com/ws',
    }
    assert scrub_embedded_credentials(payload) == payload


def test_scrub_does_not_merge_across_json_string_boundaries():
    """A naive character class would match from one string into the next."""
    payload = {'a': 'x://alpha', 'b': 'beta:gamma@delta'}
    assert scrub_embedded_credentials(payload) == payload


def test_scrub_tolerates_unserializable_and_empty_input():
    assert scrub_embedded_credentials(None) is None
    sentinel = object()
    # Not JSON-serializable -> returned unchanged rather than raising.
    assert scrub_embedded_credentials({'o': sentinel}) == {'o': sentinel}


def test_webhook_event_scrubs_on_read_so_old_rows_are_covered():
    from app.models.webhook_event import WebhookEvent

    event = WebhookEvent(
        event_type='post_prompt_received',
        payload={'dest': 'https://u:p@host/x'},
    )
    served = json.dumps(event.to_dict()['payload'])

    assert '://***:***@host' in served
    assert '"https://u:p@host/x"' not in served
