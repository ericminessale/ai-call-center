"""Department intake definitions, and the one property that must never regress.

WHY THIS FILE IS MOSTLY ABOUT ONE ASSERTION
-------------------------------------------
Intake runs through the SDK's ``gather_info``, and gather mode forcibly
deactivates every other function on the step -- ``change_context`` and
``next_step`` included. The SDK spells it out: during a gather question the
only callable tools are ``gather_submit`` plus whatever that question names in
its own ``functions``.

So an intake question that does not name the transfer tools is a trap: a caller
who says "just get me a person" mid-intake cannot be given one, and unlike the
greeting-step version of this bug fixed the same day (2026-08-19, a caller
asked eleven times and was asked for their name every time), no prompt wording
can talk its way out of it, because the lockdown is enforced by the runtime.

The live proof is ``testing/scenarios/intake_gather.json`` call 1, where a
caller refuses every question and still reaches a human. This file is the cheap
guard that fails in CI the moment a new department is added without the escape,
long before anyone places a call.
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


def all_question_sets():
    """Every intake question set, department-specific and the fallback."""
    sets = dict(main_agent._DEPARTMENT_INTAKE)
    sets['<default>'] = main_agent._DEFAULT_INTAKE
    return sets


@pytest.mark.parametrize('slug', sorted(all_question_sets()))
def test_every_intake_question_can_escape_to_a_human(slug):
    """The assertion this file exists for. No question may be a toll gate."""
    for question in all_question_sets()[slug]:
        # The escape list is passed as the question's `functions` at build
        # time, so what is checked here is that the definition is escapable at
        # all -- see test_escape_list_names_the_transfer_tools for the content.
        assert 'transfer_to_human' in main_agent._INTAKE_ESCAPES, (
            f"{slug}/{question['key']}: intake questions are built with "
            '_INTAKE_ESCAPES as their `functions`, and that list no longer '
            'offers a way to reach a person'
        )


def test_escape_list_names_the_transfer_tools():
    """Two exits, and one tool that must NOT be here.

    transfer_to_human: a caller who explicitly asks for a person gets one at
    once, without finishing intake.

    skip_intake: leaves the gather for offer_transfer, so the caller is still
    offered the choice.

    transfer_to_ai_specialist was in this list and was REMOVED on 2026-08-21.
    Unlocking it on every question gave the model a way out of intake that
    bypassed offer_transfer entirely, and it took it - two chat runs sent an
    engaged caller straight to the specialist having never been offered
    anything. The exclusion is the point of this test, not an omission.
    """
    assert 'transfer_to_human' in main_agent._INTAKE_ESCAPES
    assert 'skip_intake' in main_agent._INTAKE_ESCAPES
    assert 'transfer_to_ai_specialist' not in main_agent._INTAKE_ESCAPES, (
        'unlocking the AI transfer inside intake lets the model skip the '
        'human-or-assistant choice altogether'
    )


@pytest.mark.parametrize('slug', sorted(all_question_sets()))
def test_intake_stays_short(slug):
    """Pre-queue intake is where call centers lose callers.

    Two questions is the deliberate ceiling. This is not a style rule: every
    extra question before the caller reaches a queue is another chance for them
    to hang up, and triage has already captured name, language, department and
    a free-text reason upstream.
    """
    questions = all_question_sets()[slug]
    assert 1 <= len(questions) <= 2, (
        f'{slug} defines {len(questions)} intake questions; the ceiling is 2'
    )


@pytest.mark.parametrize('slug', sorted(all_question_sets()))
def test_question_definitions_are_well_formed(slug):
    seen = set()
    for question in all_question_sets()[slug]:
        assert question.get('key'), f'{slug}: a question has no key'
        assert question['key'] not in seen, (
            f"{slug}: duplicate key {question['key']!r} would overwrite the "
            'earlier answer in global_data'
        )
        seen.add(question['key'])
        assert question.get('question', '').strip().endswith('?'), (
            f"{slug}/{question['key']}: the question text should read as a "
            'question, since the model is instructed to ask it verbatim'
        )
        assert question.get('type', 'string') in (
            'string', 'integer', 'number', 'boolean'), (
            f"{slug}/{question['key']}: type must be one of the four the SDK "
            "maps onto gather_submit's answer schema"
        )


def test_identifier_questions_are_confirmed():
    """Long digit strings are exactly what ASR gets wrong.

    A misheard account number sends the agent to the wrong record, which is
    worse than the extra turn ``confirm=True`` costs -- the model reads the
    answer back and has to get explicit agreement before submitting.
    """
    billing = main_agent._DEPARTMENT_INTAKE.get('billing') or []
    account = [q for q in billing if q['key'] == 'account_ref']
    assert account, 'billing intake no longer asks for an account reference'
    assert account[0].get('confirm') is True, (
        'account_ref must keep confirm=True: it is an identifier read over '
        'the phone'
    )
