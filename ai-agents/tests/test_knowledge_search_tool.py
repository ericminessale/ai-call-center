"""search_knowledge registration and its relevance floor.

Two failures this covers, both of which were silent in production:

1. The tool used to be ``add_skill("native_vector_search")``. That skill
   reads its query-time embedding model from ``SearchEngine.config
   .get('embedding_model')``, but the SDK's pgvector backend publishes
   that value as ``model_name`` — so it always fell back to the SDK
   default (all-mpnet-base-v2, 768 dims) while our chunk tables are
   written by ``do_reindex`` at 384. Every single call raised "different
   vector dimensions 384 and 768" and the skill turned it into "I
   encountered an issue while searching". Nothing failed loudly; the KB
   simply never answered anything.

2. Nearest-neighbour search always returns its k nearest rows no matter
   how far away they are, so an off-topic question came back as five
   authoritative-looking excerpts and the no-results guidance could
   never fire. :data:`main_agent.KB_MIN_SCORE` is the floor that makes a
   miss possible.

These stub ``do_search`` — the retrieval quality of the real index is a
data question, not a code one. Run from the ai-agents directory:
``python -m pytest tests/ -q``
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main_agent  # noqa: E402


MISS_MESSAGE = "<<MISS>> nothing matched '{query}'"


class FakeRegistry:
    def __init__(self):
        self.tools = {}

    def define_tool(self, name, description, parameters, handler,
                    required=None, fillers=None, **kw):
        self.tools[name] = {
            'description': description,
            'parameters': parameters,
            'handler': handler,
            'required': required,
            'fillers': fillers,
        }


class FakeAgent:
    """Stands in for the per-request ephemeral agent copy."""

    def __init__(self, **attrs):
        self._registry = FakeRegistry()
        self._kb_agent_id = 'sales-ai'
        self._kb_fallback_collection = 'sales_knowledge'
        for key, value in attrs.items():
            setattr(self, key, value)

    def define_tool(self, **kwargs):
        self._registry.define_tool(**kwargs)

    @property
    def tools(self):
        return self._registry.tools


@pytest.fixture()
def stub_search(monkeypatch):
    """Replace do_search with a scripted result list."""
    calls = []

    def _install(results, raises=None):
        def fake(collection, query, count, conn, contact_id=None):
            calls.append({'collection': collection, 'query': query, 'count': count})
            if raises is not None:
                raise raises
            return results
        monkeypatch.setattr(main_agent, 'do_search', fake)
        return calls

    monkeypatch.setattr(main_agent, 'DATABASE_URL', 'postgresql://stub/stub')
    # Don't let a real backend fetch decide the collection.
    monkeypatch.setattr(main_agent, 'get_kb_collection', lambda agent_id: None)
    return _install


def _result(score, content='Battery life is about 40 hours.', section='Headphones'):
    return {'content': content, 'filename': section, 'section': section,
            'metadata': {}, 'score': score}


def _handler(agent):
    return agent.tools['search_knowledge']['handler']


def _text(result):
    return str(getattr(result, 'response', result))


def test_tool_is_registered_with_query_parameter(stub_search):
    stub_search([_result(0.7)])
    agent = FakeAgent()

    main_agent.attach_knowledge_search(agent, collection_override='sales_knowledge')

    assert 'search_knowledge' in agent.tools
    tool = agent.tools['search_knowledge']
    assert tool['required'] == ['query']
    assert 'query' in tool['parameters']
    # A KB lookup is a real wait; fillers are opt-in per tool.
    assert tool['fillers']['en-US']


def test_search_uses_the_per_call_collection_override(stub_search):
    calls = stub_search([_result(0.7)])
    agent = FakeAgent()

    main_agent.attach_knowledge_search(agent, collection_override='ws42_sales_knowledge')
    _handler(agent)({'query': 'battery life'}, {})

    assert calls[0]['collection'] == 'ws42_sales_knowledge'


def test_relevant_results_are_returned_with_their_section(stub_search):
    stub_search([_result(0.66, 'Runs about 40 hours.', 'Headphones overview')])
    agent = FakeAgent()
    main_agent.attach_knowledge_search(agent, collection_override='sales_knowledge')

    text = _text(_handler(agent)({'query': 'battery life'}, {}))

    assert 'Runs about 40 hours.' in text
    assert '[Headphones overview]' in text


def test_results_below_the_floor_are_a_miss(stub_search):
    """The whole point of the floor: an off-topic question must reach the
    no-results guidance, not five nearest-but-irrelevant excerpts."""
    stub_search([
        _result(main_agent.KB_MIN_SCORE - 0.01, 'Unrelated paragraph.'),
        _result(0.04, 'Even more unrelated.'),
    ])
    agent = FakeAgent(_kb_no_results_message=MISS_MESSAGE)
    main_agent.attach_knowledge_search(agent, collection_override='sales_knowledge')

    text = _text(_handler(agent)({'query': 'how do I file my taxes'}, {}))

    assert text.startswith('<<MISS>>')
    assert 'how do I file my taxes' in text
    assert 'Unrelated paragraph.' not in text


def test_weak_results_are_dropped_from_an_otherwise_good_hit(stub_search):
    stub_search([
        _result(0.68, 'The relevant answer.'),
        _result(0.05, 'Noise that invites fabrication.'),
    ])
    agent = FakeAgent()
    main_agent.attach_knowledge_search(agent, collection_override='sales_knowledge')

    text = _text(_handler(agent)({'query': 'battery life'}, {}))

    assert 'The relevant answer.' in text
    assert 'Noise that invites fabrication.' not in text


def test_empty_index_is_a_miss_not_an_error(stub_search):
    stub_search([])
    agent = FakeAgent(_kb_no_results_message=MISS_MESSAGE)
    main_agent.attach_knowledge_search(agent, collection_override='sales_knowledge')

    assert _text(_handler(agent)({'query': 'anything'}, {})).startswith('<<MISS>>')


def test_a_broken_index_reads_as_a_miss_to_the_caller(stub_search):
    """A dimension mismatch, a dead database, a dropped table: the model's
    next move is the same either way — offer a human, don't guess. It must
    never tell the caller our search is broken."""
    stub_search([], raises=RuntimeError('different vector dimensions 384 and 768'))
    agent = FakeAgent(_kb_no_results_message=MISS_MESSAGE)
    main_agent.attach_knowledge_search(agent, collection_override='sales_knowledge')

    text = _text(_handler(agent)({'query': 'battery life'}, {}))

    assert text.startswith('<<MISS>>')
    assert 'dimension' not in text.lower()


def test_agent_overrides_reach_the_tool(stub_search):
    """The sales specialist points catalog/pricing questions at the live
    shop tools through these two strings (commit 9d635c3)."""
    stub_search([])
    agent = FakeAgent(
        _kb_tool_description='Search company documents. NOT for prices.',
        _kb_no_results_message=MISS_MESSAGE,
    )
    main_agent.attach_knowledge_search(agent, collection_override='sales_knowledge')

    assert agent.tools['search_knowledge']['description'] == (
        'Search company documents. NOT for prices.'
    )


def test_no_tool_without_an_agent_binding(stub_search):
    stub_search([_result(0.7)])
    agent = FakeAgent()
    agent._kb_agent_id = None

    main_agent.attach_knowledge_search(agent, collection_override='sales_knowledge')

    assert agent.tools == {}


def test_blank_query_is_rejected_without_searching(stub_search):
    calls = stub_search([_result(0.7)])
    agent = FakeAgent()
    main_agent.attach_knowledge_search(agent, collection_override='sales_knowledge')

    _handler(agent)({'query': '   '}, {})

    assert calls == []
