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
        / 'v2w3x4y5z6a7_add_interaction_timelines.py'
    )
    spec = importlib.util.spec_from_file_location('interaction_timeline_migration', path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_interaction_timeline_migration_round_trip():
    engine = sa.create_engine('sqlite://')
    metadata = sa.MetaData()
    for name in ('workspaces', 'calls', 'queues', 'users'):
        sa.Table(name, metadata, sa.Column('id', sa.Integer, primary_key=True))
    metadata.create_all(engine)

    with engine.begin() as connection:
        migration = _load_migration()
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()

        inspector = sa.inspect(connection)
        assert {'queue_attempts', 'handling_segments'} <= set(inspector.get_table_names())
        queue_columns = {column['name'] for column in inspector.get_columns('queue_attempts')}
        assert {
            'service_started_at', 'offer_count', 'declined_offer_count',
            'accepted_at', 'exit_reason',
        } <= queue_columns
        segment_columns = {
            column['name'] for column in inspector.get_columns('handling_segments')
        }
        assert {'segment_type', 'agent_id', 'started_at', 'ended_at'} <= segment_columns

        migration.downgrade()
        assert not {
            'queue_attempts', 'handling_segments',
        } & set(sa.inspect(connection).get_table_names())
