"""The human-vs-AI routing gate.

Live failure this exists for (fred_returning_caller, call 76, 2026-08-17):

    [ai]     Would you like to speak with a human specialist or the AI
             assistant who can answer your question right away?
    [caller] I would prefer
    [ai]     ...It sounds like you prefer to speak with human specialist.
    [caller] No.

Sam committed an irreversible routing decision on a three-word fragment that
named no option yet, then ignored the correction. Fred spent the call on hold
and left as a callback, never reaching the specialist.

WHAT THIS GATE DOES, AND WHAT IT GAVE UP
----------------------------------------
It answers one question: did the caller's turn name an option at all? It does
NOT decide which option they picked.

Earlier versions did try. Review found five distinct ways for keyword-and-
polarity parsing to get that wrong: comparisons ("the AI over a human"),
corrections ("I wanted a human before, but use the AI now"), post-target
negation ("a human isn't what I want"), explanations naming the rejected
option ("get me a person, the bot is useless"), and unenumerated phrasings
across three languages. Each fix bought one sentence and left the shape of the
problem standing -- and a wrong answer here produces the expensive misroute the
gate was built to prevent, so the parser was capable of causing the very bug it
was guarding against.

The narrow version cannot do that. A turn naming a person is left to the
model, which is where that judgement lived before this code existed. A turn
naming nothing gets the documented default -- the AI assistant, recoverable via
escalate_to_human, rather than a queue, which is not.

If enforcing an explicit choice ever matters, the answer is a structured one
(a DTMF digit, or an enum the tool validates), not more parsing.
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


# ---------------------------------------------------------------------------
# The case the gate exists for: no option named.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('said', [
    'I would prefer',            # the exact fragment that lost call 76
    'I would',
    'yes',
    'okay sure',
    'um',
    'whatever is fastest',
    'I just need pricing',
])
def test_a_turn_that_names_no_option_gets_the_documented_default(said):
    raw = call_log(('assistant', OFFER), ('user', said))

    assert main_agent._human_request_evidence(raw) == 'ABSENT', said


@pytest.mark.parametrize('said', [
    'The AI is fine',
    'the assistant please',
    'Prefiero la IA',
])
def test_naming_only_the_machine_reads_as_choosing_it(said):
    """No negator anywhere in the turn, so there is nothing to weigh."""
    raw = call_log(('assistant', OFFER), ('user', said))

    assert main_agent._human_request_evidence(raw) == 'ABSENT', said


# ---------------------------------------------------------------------------
# The case the gate stays out of: a person was named.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('said', [
    # Plain requests.
    'I would prefer to speak to a human',
    'Can I talk to a person please',
    'Put me through to an agent',
    'Is there someone I can speak with',
    'operator',
    # Spanish and French -- the agent answers in all three languages.
    'Quiero hablar con una persona',
    'Prefiero un agente humano',
    'Me puede pasar con alguien por favor',
    'Je prefere un conseiller humain',
    "Je veux parler a l'agent",
    "Passez-moi quelqu'un s'il vous plait",
    # Phrasings that broke every previous parser. The gate no longer rules on
    # them either way -- it declines to override the model, which is correct
    # and, unlike its earlier guesses, cannot misroute anyone by itself.
    "A human isn't what I want",
    "The human option doesn't work for me",
    'Get me a person because the bot is useless',
    'I prefer the AI over a human',
    'I wanted a human before, but use the AI now',
    'I do not want a human, use the AI',
])
def test_a_turn_that_names_a_person_is_left_to_the_model(said):
    raw = call_log(('assistant', OFFER), ('user', said))

    assert main_agent._human_request_evidence(raw) == 'CONFIRMED', said


@pytest.mark.parametrize('said', [
    "I don't want to talk to a robot",
    'not the AI please',
    'No quiero la IA',
    "Je ne veux pas de l'IA",
])
def test_a_negated_machine_is_not_treated_as_choosing_it(said):
    """"not the AI" names only the machine, but the negator means it may well
    be a rejection -- the exact judgement this gate stopped making. It defers
    rather than guessing, so refusing the machine can never be read as
    choosing it."""
    raw = call_log(('assistant', OFFER), ('user', said))

    assert main_agent._human_request_evidence(raw) == 'CONFIRMED', said


# ---------------------------------------------------------------------------
# Reading the right turn, and failing safe.
# ---------------------------------------------------------------------------

def test_only_the_callers_words_count_not_the_agents():
    """The offer itself contains 'human specialist'. Reading the wrong role
    would make every caller look like they named a person -- the gate would go
    quiet exactly when it needs to fire."""
    raw = call_log(('assistant', OFFER), ('user', 'I would prefer'))

    assert main_agent._last_caller_utterance(raw) == 'I would prefer'
    assert main_agent._human_request_evidence(raw) == 'ABSENT'


def test_the_most_recent_caller_turn_wins():
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


def test_folding_still_handles_elision_and_contractions():
    """French elision must split (l'agent -> l agent) while English
    contractions must fuse (don't -> dont), or the vocabulary matches nothing
    in one language or the negator list misses in the other."""
    assert 'agent' in main_agent._fold("l'agent").split()
    assert 'dont' in main_agent._fold("I don't").split()
    assert 'quelquun' in main_agent._fold("quelqu'un").split()


# ---------------------------------------------------------------------------
# Round five: the presence vocabulary has to match the labels the OFFER uses,
# and must not treat adjectives as people.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('said', [
    'The specialist, please',      # the exact label the offer uses
    'the human specialist',
    'quiero un especialista',
    'le specialiste',
])
def test_the_offered_label_counts_as_naming_an_option(said):
    """The offer says "a human sales SPECIALIST, or our AI assistant". A
    caller echoing that back named an option; reading it as naming nobody
    overrode an explicit choice with an AI transfer."""
    raw = call_log(('assistant', OFFER), ('user', said))

    assert main_agent._human_request_evidence(raw) == 'CONFIRMED', said


@pytest.mark.parametrize('said', [
    'Can you check live availability?',
    'the actual price please',
    'is that the real price',
])
def test_bare_adjectives_are_not_people(said):
    """'live', 'actual' and 'real' standing alone turned ordinary product
    questions into option-naming turns."""
    raw = call_log(('assistant', OFFER), ('user', said))

    assert main_agent._human_request_evidence(raw) == 'ABSENT', said


@pytest.mark.parametrize('said', ['a real person please', 'an actual human'])
def test_those_adjectives_still_work_with_their_noun(said):
    raw = call_log(('assistant', OFFER), ('user', said))

    assert main_agent._human_request_evidence(raw) == 'CONFIRMED', said
