"""The human-vs-AI routing gate.

Live failure this exists for (fred_returning_caller, call 76, 2026-08-17):

    [ai]     Would you like to speak with a human specialist or the AI
             assistant who can answer your question right away?
    [caller] I would prefer
    [ai]     ...It sounds like you prefer to speak with human specialist.
    [caller] No.

Sam committed an irreversible routing decision on a three-word fragment that
contained no choice yet, then ignored the correction. Fred spent the call on
hold and left as a callback, never reaching the specialist.

The offer_transfer step's prompt ALREADY says a caller "cut off mid-answer"
wants the AI assistant and that transfer_to_human is only for someone who
"clearly asked for a person" — and the model did the opposite anyway. That is
the PGI point: a rule the model can violate is a proposal. So the rule reads
the platform's own call_log instead of the model's interpretation of it.

Direction of failure is deliberate. Routing to the AI when the caller wanted a
human is recoverable — the specialist can escalate_to_human mid-conversation.
Routing to a human when they didn't ask parks them on hold and, past the cap,
ends the call as a callback. So ambiguity resolves to the AI, and a missing
call_log resolves to obeying the model.
"""

import os
import sys

import pytest

AGENTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if AGENTS_DIR not in sys.path:
    sys.path.insert(0, AGENTS_DIR)

main_agent = pytest.importorskip(
    'main_agent',
    reason='needs the signalwire SDK; runs in the agents image',
)


def call_log(*turns):
    """A call_log in the platform's shape: [{'role': ..., 'content': ...}]."""
    return {'call_log': [
        {'role': role, 'content': content} for role, content in turns
    ]}


OFFER = (
    'Would you like to speak with a human specialist or the AI assistant '
    'who can answer your question right away?'
)


def test_the_fragment_that_lost_the_call_is_not_a_request_for_a_human():
    """'I would prefer' — the caller was still forming the sentence."""
    raw = call_log(
        ('assistant', OFFER),
        ('user', 'I would prefer'),
    )

    assert main_agent._human_request_evidence(raw) == 'ABSENT'


@pytest.mark.parametrize('said', [
    'I would prefer to speak to a human',
    "Can I talk to a person please",
    'Put me through to an agent',
    'I want a real representative',
    "I don't want to talk to a robot",
    'Is there someone I can speak with',
    'operator',
])
def test_a_genuine_request_for_a_person_still_routes_to_a_human(said):
    """The gate must not eat legitimate transfers — including the ones phrased
    by naming the machine rather than the human."""
    raw = call_log(('assistant', OFFER), ('user', said))

    assert main_agent._human_request_evidence(raw) == 'CONFIRMED', said


@pytest.mark.parametrize('said', [
    'The AI is fine',
    'whatever is fastest',
    'yes',
    'um',
    'I just need pricing',
])
def test_anything_that_is_not_a_request_for_a_person_reads_as_absent(said):
    raw = call_log(('assistant', OFFER), ('user', said))

    assert main_agent._human_request_evidence(raw) == 'ABSENT', said


def test_only_the_callers_words_count_not_the_agents():
    """The offer itself contains 'human specialist'. Reading the wrong role
    would make every caller look like they asked for a person — the gate would
    pass exactly when it needs to fire."""
    raw = call_log(
        ('assistant', OFFER),
        ('user', 'I would prefer'),
    )

    assert main_agent._last_caller_utterance(raw) == 'I would prefer'
    assert main_agent._human_request_evidence(raw) == 'ABSENT'


def test_the_most_recent_caller_turn_wins():
    """A caller who asks for a human after first saying something else."""
    raw = call_log(
        ('user', 'I have a question about pricing'),
        ('assistant', OFFER),
        ('user', 'Actually put me through to a person'),
    )

    assert main_agent._human_request_evidence(raw) == 'CONFIRMED'


@pytest.mark.parametrize('raw', [
    {},
    {'call_log': []},
    {'call_log': 'not-a-list'},
    {'call_log': [{'role': 'assistant', 'content': OFFER}]},
    None,
])
def test_a_missing_conversation_defers_to_the_model(raw):
    """UNAVAILABLE, never ABSENT. If swaig_post_conversation isn't honoured on
    some deployment, the gate must go quiet rather than reroute every single
    request for a human."""
    assert main_agent._human_request_evidence(raw) == 'UNAVAILABLE'


def test_raw_call_log_is_accepted_as_a_fallback():
    """call_log can shrink after consolidation; raw_call_log is preserved."""
    raw = {'raw_call_log': [
        {'role': 'assistant', 'content': OFFER},
        {'role': 'user', 'content': 'get me a human'},
    ]}

    assert main_agent._human_request_evidence(raw) == 'CONFIRMED'
