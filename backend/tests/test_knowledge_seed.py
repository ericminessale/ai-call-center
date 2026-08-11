"""Template knowledge-base seed + the chunk-table bookkeeping around it.

Both KB collections shipped with zero documents in every deployment, so
``search_knowledge`` found nothing for every specialist and hosted-demo
visitors cloned two empty shells. These cover the seed that fixes that,
and specifically the parts that are easy to get wrong twice:

  - it must be ONCE-only, not "seed whenever empty" — an operator who
    deletes the example documents must not find them back after a
    restart;
  - it must not mix examples into a collection someone already wrote
    their own documents into;
  - cloned documents must NOT inherit the template's ``is_published``,
    which is the UI's "the AI can find this" badge and would be a lie
    until the clone's own chunk table exists;
  - the reaper must be able to name a workspace's chunk tables BEFORE it
    deletes the rows those names come from.
"""
import pytest
from flask import Flask

from app import db
from app.models import (
    AgentCollectionAssignment,
    Document,
    DocumentCollection,
    SystemConfig,
    Workspace,
)
from app.tenancy import DEFAULT_WORKSPACE_ID


@pytest.fixture()
def seed_app():
    app = Flask(__name__)
    app.config.update(
        SQLALCHEMY_DATABASE_URI='sqlite://',
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        TESTING=True,
    )
    db.init_app(app)
    with app.app_context():
        db.create_all()
        db.session.add(Workspace(id=DEFAULT_WORKSPACE_ID, name='Template'))
        db.session.commit()
        yield app
        db.session.remove()
        db.drop_all()


def _template_docs():
    return Document.query.filter_by(workspace_id=DEFAULT_WORKSPACE_ID).all()


def test_seed_creates_collections_documents_and_assignments(seed_app):
    from app.services.knowledge_seed import seed_template_knowledge
    from app.seeds.product_knowledge import SEED_COLLECTIONS

    result = seed_template_knowledge()

    assert result['seeded'] is True
    expected_docs = sum(len(spec['documents']) for spec in SEED_COLLECTIONS)
    assert result['documents_created'] == expected_docs
    assert len(_template_docs()) == expected_docs

    names = {
        c.name for c in DocumentCollection.query.filter_by(
            workspace_id=DEFAULT_WORKSPACE_ID).all()
    }
    assert names == {'sales_knowledge', 'support_knowledge'}

    # Assignments are what bind a specialist to its collection; without
    # them the agents fall back to a collection nobody indexed.
    agent_ids = {
        a.agent_id for a in AgentCollectionAssignment.query.filter_by(
            workspace_id=DEFAULT_WORKSPACE_ID).all()
    }
    assert agent_ids == {'sales-ai', 'outbound-sales', 'support-ai', 'outbound-support'}


def test_seeded_documents_start_unpublished(seed_app):
    """'Published' means embedded and findable. Nothing is embedded yet."""
    from app.services.knowledge_seed import seed_template_knowledge

    seed_template_knowledge()
    assert all(doc.is_published is False for doc in _template_docs())


def test_seed_is_once_only_not_seed_when_empty(seed_app):
    """A deleted example document must stay deleted across restarts."""
    from app.services.knowledge_seed import seed_template_knowledge

    seed_template_knowledge()
    for doc in _template_docs():
        db.session.delete(doc)
    db.session.commit()
    assert _template_docs() == []

    second = seed_template_knowledge()

    assert second['seeded'] is False
    assert second['reason'] == 'already_seeded'
    assert _template_docs() == []


def test_seed_skips_a_collection_that_already_has_documents(seed_app):
    """An operator's own knowledge base must not get example content mixed in."""
    from app.services.knowledge_seed import seed_template_knowledge

    mine = DocumentCollection(
        workspace_id=DEFAULT_WORKSPACE_ID,
        name='sales_knowledge',
        physical_name='sales_knowledge',
        display_name='Sales Knowledge Base',
    )
    db.session.add(mine)
    db.session.flush()
    db.session.add(Document(
        workspace_id=DEFAULT_WORKSPACE_ID,
        collection_id=mine.id,
        title='Our real product',
        content='Content the operator wrote.',
        is_published=True,
    ))
    db.session.commit()

    seed_template_knowledge()

    sales_titles = [d.title for d in Document.query.filter_by(collection_id=mine.id).all()]
    assert sales_titles == ['Our real product']
    # The other collection still gets seeded — the skip is per collection.
    support = DocumentCollection.query.filter_by(
        workspace_id=DEFAULT_WORKSPACE_ID, name='support_knowledge').one()
    assert support.documents.count() > 0


def test_seed_marker_lands_in_the_global_config_layer(seed_app):
    from app.services.knowledge_seed import SEED_MARKER_KEY, seed_template_knowledge

    seed_template_knowledge()

    marker = SystemConfig.query.filter_by(key=SEED_MARKER_KEY).one()
    assert marker.workspace_id == SystemConfig.GLOBAL_WORKSPACE_ID


def test_seed_content_never_quotes_a_price(seed_app):
    """Prices live in the shop catalog tool, which is live data. A price in
    a document is a second source of truth that goes stale silently."""
    import re

    from app.seeds.product_knowledge import SEED_COLLECTIONS

    # The free-shipping threshold is a shipping policy the catalog has no
    # concept of — the one deliberate exception.
    allowed = {'$50'}
    # Decimals only when digits follow, so a sentence-ending "$50." isn't
    # read as the price "$50.".
    price = re.compile(r'\$\d[\d,]*(?:\.\d+)?')
    for spec in SEED_COLLECTIONS:
        for title, content in spec['documents']:
            found = set(price.findall(content)) - allowed
            assert not found, f"{title} quotes a price: {sorted(found)}"


class TestChunkTableNaming:
    def test_rejects_names_that_cannot_be_an_identifier(self):
        from app.services.kb_index import chunks_table_for

        assert chunks_table_for('ws3_sales_knowledge') == 'chunks_ws3_sales_knowledge'
        assert chunks_table_for('sales; DROP TABLE users--') is None
        assert chunks_table_for('') is None
        assert chunks_table_for(None) is None

    def test_workspace_tables_include_collections_and_caller_memory(self, seed_app):
        from app.services.kb_index import workspace_chunk_tables

        ws = Workspace(id=7, name='Visitor')
        db.session.add(ws)
        db.session.add(DocumentCollection(
            workspace_id=7,
            name='sales_knowledge',
            physical_name='ws7_sales_knowledge',
            display_name='Sales Knowledge Base',
        ))
        db.session.commit()

        assert workspace_chunk_tables(7) == [
            'chunks_interactions_ws7',
            'chunks_ws7_sales_knowledge',
        ]
