"""Agent-side caller-memory helpers — the topic-selection policy (F-09, A-5).

These pure functions decide whether the AI may raise a caller's past topic.
They had ZERO coverage: the verification audit found `_offerable_topic`
rejecting legitimate recent topics on date-format edge cases and the facts
block bypassing the gate entirely, and no test failed. Everything here runs
without a database, a network, or the SDK's serving stack.

Run from the ai-agents directory:  python -m pytest tests/ -q
"""
import os
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main_agent import (  # noqa: E402
    _CLOSED_DISPOSITIONS,
    _TOPIC_MAX_AGE_DAYS,
    _caller_greeting_hint,
    _offerable_topic,
)


def _iso(days_ago):
    return (datetime.utcnow() - timedelta(days=days_ago)).isoformat()


def _entry(**kw):
    base = {
        'reason': 'vacuum loses suction',
        'disposition': 'technical-issue',
        'ended_at': _iso(2),
    }
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# _offerable_topic — may this specific past interaction be raised?
# ---------------------------------------------------------------------------

def test_recent_open_topic_is_offerable():
    assert _offerable_topic(_entry()) == 'vacuum loses suction'


@pytest.mark.parametrize('disposition', sorted(_CLOSED_DISPOSITIONS))
def test_closed_dispositions_are_never_offerable(disposition):
    """A resolved/abandoned/spam call must not be reopened by the greeting."""
    assert _offerable_topic(_entry(disposition=disposition)) is None


def test_stale_topic_is_not_offerable():
    assert _offerable_topic(_entry(ended_at=_iso(_TOPIC_MAX_AGE_DAYS + 1))) is None


def test_topic_right_at_the_age_boundary_is_offerable():
    assert _offerable_topic(_entry(ended_at=_iso(_TOPIC_MAX_AGE_DAYS - 1))) is not None


@pytest.mark.parametrize('bad_date', [None, '', 'not-a-date', 'yesterday', 12345])
def test_unknown_age_is_never_offerable(bad_date):
    """Unknown age must fail CLOSED — we can't claim recency we can't prove."""
    assert _offerable_topic(_entry(ended_at=bad_date)) is None


def test_iso_with_microseconds_and_space_separator_still_parses():
    """The backend emits datetime.isoformat(); don't reject real formats."""
    now = datetime.utcnow() - timedelta(days=1)
    assert _offerable_topic(_entry(ended_at=now.isoformat())) is not None
    assert _offerable_topic(_entry(ended_at=now.isoformat(sep=' '))) is not None


def test_missing_reason_or_bad_shape_is_not_offerable():
    assert _offerable_topic(_entry(reason=None)) is None
    assert _offerable_topic(_entry(reason='')) is None
    assert _offerable_topic(None) is None
    assert _offerable_topic('not a dict') is None


# ---------------------------------------------------------------------------
# _caller_greeting_hint — what the greeting is allowed to say
# ---------------------------------------------------------------------------

def _contact(**kw):
    base = {'name': 'Fred', 'name_known': True, 'previous_calls': 2,
            'interaction_digest': []}
    base.update(kw)
    return base


def test_unknown_caller_yields_no_hint():
    """No hint => the greeting renders byte-identically to pre-feature text."""
    assert _caller_greeting_hint(None, None) is None
    assert _caller_greeting_hint(
        {'name': None, 'name_known': False, 'previous_calls': 0}, None,
    ) is None


def test_single_open_topic_is_offered():
    hint = _caller_greeting_hint(_contact(), _entry())
    assert hint['last_reason'] == 'vacuum loses suction'
    assert not hint.get('multiple_topics')


def test_two_distinct_open_topics_ask_an_open_question_instead():
    digest = [_entry(), _entry(reason='billing dispute')]
    hint = _caller_greeting_hint(_contact(interaction_digest=digest), _entry())
    assert hint.get('multiple_topics') is True
    assert 'last_reason' not in hint


def test_closed_and_stale_history_offers_nothing():
    digest = [
        _entry(disposition='resolved'),
        _entry(reason='old thing', ended_at=_iso(90)),
    ]
    hint = _caller_greeting_hint(
        _contact(interaction_digest=digest), _entry(disposition='resolved'),
    )
    # Still a known returning caller — just no topic to lead with.
    assert hint is not None
    assert 'last_reason' not in hint
    assert not hint.get('multiple_topics')


def test_same_topic_repeated_is_one_topic_not_two():
    digest = [_entry(), _entry(reason='Vacuum Loses Suction  ')]
    hint = _caller_greeting_hint(_contact(interaction_digest=digest), _entry())
    assert hint.get('last_reason') == 'vacuum loses suction'
    assert not hint.get('multiple_topics')


def test_callback_direction_decides_who_called_whom():
    cb = {'reason': 'warranty question', 'requested_at': _iso(1)}
    outbound = _caller_greeting_hint(_contact(), None, cb, 'outbound')
    inbound = _caller_greeting_hint(_contact(), None, cb, 'inbound')
    assert outbound['callback_dialed'] is True
    assert inbound['callback_dialed'] is False
    assert outbound['callback_reason'] == 'warranty question'


def test_first_time_caller_with_a_name_still_hints():
    """A known contact on their first call gets recognition but no history."""
    hint = _caller_greeting_hint(_contact(previous_calls=0), None)
    assert hint is not None
    assert hint['name'] == 'Fred'
    assert 'last_reason' not in hint


# ---------------------------------------------------------------------------
# _open_in_documented_language — which language the call OPENS in
# ---------------------------------------------------------------------------

class _FakeAgent:
    """Just the attribute the helper touches, in add_language()'s shape."""

    def __init__(self):
        self._languages = [
            {'name': 'English', 'code': 'en-US', 'voice': 'rime.spore'},
            {'name': 'Spanish', 'code': 'es-ES', 'voice': 'openai.alloy'},
            {'name': 'French', 'code': 'fr-FR', 'voice': 'openai.alloy'},
        ]

    @property
    def codes(self):
        return [lang['code'] for lang in self._languages]


def test_documented_spanish_opens_in_spanish():
    from main_agent import _open_in_documented_language
    agent = _FakeAgent()

    name = _open_in_documented_language(agent, {'preferred_language': 'es-ES'}, None)

    assert name == 'Spanish'
    assert agent.codes[0] == 'es-ES', 'the opening language must be first'


def test_regional_variant_matches_the_configured_voice():
    """A caller recorded as es-MX should still open in the agent's es-ES voice."""
    from main_agent import _open_in_documented_language
    agent = _FakeAgent()

    assert _open_in_documented_language(agent, {'preferred_language': 'es-MX'}, None) == 'Spanish'
    assert agent.codes[0] == 'es-ES'


def test_english_and_missing_language_never_reorder():
    from main_agent import _open_in_documented_language
    for value in ('en-US', 'en-GB', '', None):
        agent = _FakeAgent()
        assert _open_in_documented_language(agent, {'preferred_language': value}, None) is None
        assert agent.codes[0] == 'en-US'


def test_language_the_agent_does_not_speak_is_ignored():
    """Reordering to an unconfigured language would render a bad voice id."""
    from main_agent import _open_in_documented_language
    agent = _FakeAgent()

    assert _open_in_documented_language(agent, {'preferred_language': 'de-DE'}, None) is None
    assert agent.codes[0] == 'en-US'


def test_contact_level_language_beats_last_call_language():
    """A human-set contact language outranks whatever the last call used."""
    from main_agent import _open_in_documented_language
    agent = _FakeAgent()

    name = _open_in_documented_language(
        agent, {'preferred_language': 'fr-FR'}, {'caller_language': 'es-ES'},
    )

    assert name == 'French'
    assert agent.codes[0] == 'fr-FR'


def test_falls_back_to_last_call_language_when_contact_has_none():
    from main_agent import _open_in_documented_language
    agent = _FakeAgent()

    assert _open_in_documented_language(agent, {}, {'caller_language': 'es-ES'}) == 'Spanish'
    assert agent.codes[0] == 'es-ES'


def test_reorder_is_idempotent_and_preserves_the_other_languages():
    from main_agent import _open_in_documented_language
    agent = _FakeAgent()

    _open_in_documented_language(agent, {'preferred_language': 'es-ES'}, None)
    _open_in_documented_language(agent, {'preferred_language': 'es-ES'}, None)

    assert agent.codes == ['es-ES', 'en-US', 'fr-FR']
    assert len(agent._languages) == 3
