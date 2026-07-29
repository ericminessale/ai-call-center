"""Bulk DELETE is NOT workspace-scoped — code must not rely on it being.

``tenancy._apply_workspace_criteria`` deliberately rewrites SELECTs only, so
the demo-reset wipe isn't silently narrowed to one tenant. The consequence is
that any ``Model.query.delete()`` on a WorkspaceScoped model spans every
workspace. ``PUT /api/admin/agent-assignments`` did exactly that to clear
before re-inserting, so one hosted workspace saving its KB bindings deleted
every other workspace's.

These tests pin both halves: the hazard (bulk delete crosses tenants) and the
replacement idiom the endpoint now uses (fetch under scope, then delete rows).
"""
import pytest
from flask import Flask

from app import db
from app.models import AgentCollectionAssignment, DocumentCollection, Workspace
from app.tenancy import workspace_context


@pytest.fixture()
def two_workspaces():
    app = Flask(__name__)
    app.config.update(
        SQLALCHEMY_DATABASE_URI='sqlite://',
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        TESTING=True,
    )
    db.init_app(app)
    with app.app_context():
        db.create_all()

        first = Workspace(name='Tenant A')
        second = Workspace(name='Tenant B')
        db.session.add_all([first, second])
        db.session.flush()

        for ws, slug in ((first, 'a'), (second, 'b')):
            collection = DocumentCollection(
                workspace_id=ws.id,
                name='product-kb',
                display_name='Product KB',
                physical_name=f'ws{ws.id}_product-kb',
            )
            db.session.add(collection)
            db.session.flush()
            db.session.add(AgentCollectionAssignment(
                workspace_id=ws.id,
                agent_id=f'sales-ai-{slug}',
                collection_id=collection.id,
            ))
        db.session.commit()

        yield first.id, second.id
        db.session.remove()
        db.drop_all()


def _all_assignment_workspaces():
    with workspace_context(None):
        return sorted(
            row.workspace_id for row in AgentCollectionAssignment.query.all()
        )


def test_bulk_delete_ignores_the_workspace_scope(two_workspaces):
    """Documents WHY the endpoint can't use .query.delete()."""
    _first_id, second_id = two_workspaces

    with workspace_context(second_id):
        # The scoped SELECT sees one row...
        assert AgentCollectionAssignment.query.count() == 1
        # ...but the bulk DELETE reaches both.
        assert AgentCollectionAssignment.query.delete() == 2
        db.session.rollback()

    assert _all_assignment_workspaces() == sorted(two_workspaces)


def test_fetch_then_delete_stays_inside_the_workspace(two_workspaces):
    """The idiom update_agent_assignments now uses."""
    first_id, second_id = two_workspaces

    with workspace_context(second_id):
        for row in AgentCollectionAssignment.query.all():
            db.session.delete(row)
        db.session.commit()

    assert _all_assignment_workspaces() == [first_id]


def test_platform_scope_still_clears_everything(two_workspaces):
    """A clone-and-own admin has no workspace context, and the same code must
    still behave as a full wipe for them."""
    with workspace_context(None):
        for row in AgentCollectionAssignment.query.all():
            db.session.delete(row)
        db.session.commit()

    assert _all_assignment_workspaces() == []


def test_scoped_query_hides_another_workspaces_collection(two_workspaces):
    """The collection-exists check must not be satisfiable cross-tenant."""
    first_id, second_id = two_workspaces
    with workspace_context(first_id):
        foreign_id = DocumentCollection.query.one().id

    with workspace_context(second_id):
        assert DocumentCollection.query.filter_by(id=foreign_id).first() is None
