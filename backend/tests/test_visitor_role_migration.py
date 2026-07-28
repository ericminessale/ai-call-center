"""Data migration w3x4y5z6a7b8: demo owners 'admin' -> 'visitor' (HIGH-3).

Runs the revision's upgrade/downgrade against an in-memory SQLite users table,
the same shape test_interaction_timeline_migration.py uses. The assertions that
matter are the ones about which rows are LEFT ALONE: platform admins (NULL
workspace) and the template workspace's rows.
"""
import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def _load_migration():
    path = (
        Path(__file__).parents[1]
        / 'migrations'
        / 'versions'
        / 'w3x4y5z6a7b8_split_demo_visitor_role.py'
    )
    spec = importlib.util.spec_from_file_location('visitor_role_migration', path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


# (email, workspace_id, role) — one row per case the predicate has to separate.
_SEED = [
    ('operator@platform', None, 'admin'),        # platform operator
    ('clone@owner', None, 'admin'),              # clone-and-own admin
    ('template@ws1', 1, 'admin'),                # template workspace (excluded)
    ('owner@ws-aaaa', 7, 'admin'),               # hosted visitor's owner row
    ('extra@ws-aaaa', 7, 'admin'),               # 'admin' a visitor minted
    ('colleague@ws-aaaa', 7, 'agent'),           # untouched, wrong role
    ('sup@ws-bbbb', 9, 'supervisor'),            # untouched, wrong role
]


def _roles(connection, table):
    return {
        row.email: row.role
        for row in connection.execute(sa.select(table.c.email, table.c.role))
    }


def test_visitor_role_migration_round_trip():
    engine = sa.create_engine('sqlite://')
    metadata = sa.MetaData()
    users = sa.Table(
        'users', metadata,
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('email', sa.String(255), nullable=False),
        sa.Column('workspace_id', sa.Integer, nullable=True),
        sa.Column('role', sa.String(50), nullable=False),
    )
    metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(users.insert(), [
            {'email': email, 'workspace_id': ws, 'role': role}
            for email, ws, role in _SEED
        ])

        migration = _load_migration()
        migration.op = Operations(MigrationContext.configure(connection))

        migration.upgrade()
        after = _roles(connection, users)
        # Demoted: workspace-scoped admins outside the template workspace.
        assert after['owner@ws-aaaa'] == 'visitor'
        assert after['extra@ws-aaaa'] == 'visitor'
        # Left alone: platform admins keep the real admin role.
        assert after['operator@platform'] == 'admin'
        assert after['clone@owner'] == 'admin'
        # Left alone: the template workspace and non-admin roles.
        assert after['template@ws1'] == 'admin'
        assert after['colleague@ws-aaaa'] == 'agent'
        assert after['sup@ws-bbbb'] == 'supervisor'

        migration.downgrade()
        assert _roles(connection, users) == {
            email: role for email, _ws, role in _SEED
        }


def test_downgrade_is_a_no_op_on_a_fresh_database():
    """A DB that never had demo workspaces must survive both directions."""
    engine = sa.create_engine('sqlite://')
    metadata = sa.MetaData()
    users = sa.Table(
        'users', metadata,
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('email', sa.String(255), nullable=False),
        sa.Column('workspace_id', sa.Integer, nullable=True),
        sa.Column('role', sa.String(50), nullable=False),
    )
    metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(
            users.insert(),
            [{'email': 'solo@platform', 'workspace_id': None, 'role': 'admin'}],
        )
        migration = _load_migration()
        migration.op = Operations(MigrationContext.configure(connection))
        migration.downgrade()
        migration.upgrade()
        migration.downgrade()
        assert _roles(connection, users) == {'solo@platform': 'admin'}
