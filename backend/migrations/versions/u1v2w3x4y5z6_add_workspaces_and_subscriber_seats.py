"""Multi-tenancy Phase 1: workspaces + subscriber_seats + workspace_id spine.

Revision ID: u1v2w3x4y5z6
Revises: t0u1v2w3x4y5
Create Date: 2026-07-08 12:00:00.000000

The spine of the per-visitor-workspace refactor (MULTI_TENANCY_DESIGN.md §3):

1. ``workspaces`` — the tenancy root. A DEFAULT workspace (id=1) is created
   here and every existing tenant row is backfilled onto it. Clone-and-own
   deployments treat it as THE workspace; the hosted demo (TENANCY_MODE)
   treats it as the never-leased TEMPLATE the provisioner clones from.
2. ``subscriber_seats`` — the SignalWire subscriber pool, decoupled from
   User rows. Existing demo-persona credentials are copied in and the
   persona users retired (is_active=false); services/seat_pool.py
   re-resolves any fabricated '/private/agent-<id>' addresses at boot.
3. ``workspace_id`` columns on tenant tables, backfilled to the default
   workspace. NOT NULL where every row must belong somewhere (calls,
   contacts, queues, callbacks, documents, collections, assignments, mcp
   configs); nullable where NULL is meaningful (users = platform-level
   user; webhook_events = platform log line) or derivation can lag
   (conferences, transcriptions, call_legs, conference_participants).
4. Uniqueness re-scoped per workspace: users.email (COALESCE expression
   index so NULL-workspace platform users still collide), contacts.phone,
   queues.slug, document_collections.name (+ new globally-unique
   physical_name for chunk-table/search identity), agent_collection_
   assignments. system_config's PK becomes (workspace_id, key) with the
   pre-existing rows re-keyed as workspace 0 = global defaults.

NOT mirrored into scripts/init.sql — on a fresh volume init.sql stamps
e5f6a7b8c9d0 and entrypoint.sh's `flask db upgrade` applies everything
after it, including this revision (DEPLOY-H1 mechanics). Mirroring would
double-create the tables.
"""
import uuid

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'u1v2w3x4y5z6'
down_revision = 't0u1v2w3x4y5'
branch_labels = None
depends_on = None


# Tables whose workspace_id ends NOT NULL after backfill.
_NOT_NULL_TABLES = (
    'calls',
    'contacts',
    'queues',
    'callbacks',
    'document_collections',
    'documents',
    'agent_collection_assignments',
    'mcp_gateway_configs',
)

# Tables whose workspace_id stays nullable (NULL is meaningful or the
# value is derived opportunistically at flush time).
_NULLABLE_TABLES = (
    'conferences',
    'webhook_events',
    'transcriptions',
    'call_legs',
    'conference_participants',
)


def upgrade():
    # ------------------------------------------------------------------
    # 1. workspaces
    # ------------------------------------------------------------------
    op.create_table(
        'workspaces',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('public_id', sa.String(36), nullable=False),
        sa.Column('name', sa.String(200), nullable=False, server_default='My Call Center'),
        sa.Column('status', sa.String(20), nullable=False, server_default='active'),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.Column('last_active_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.Column('expires_at', sa.DateTime(), nullable=True),
        sa.Column('session_token_hash', sa.String(64), nullable=True),
        sa.Column('verified_number', sa.String(32), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_workspaces_public_id', 'workspaces', ['public_id'], unique=True)
    op.create_index('ix_workspaces_session_token_hash', 'workspaces', ['session_token_hash'], unique=True)
    op.create_index('ix_workspaces_verified_number', 'workspaces', ['verified_number'])
    op.create_index('ix_workspaces_status', 'workspaces', ['status'])

    # The default workspace. expires_at stays NULL = never reaped.
    default_public_id = str(uuid.uuid4())
    op.execute(
        "INSERT INTO workspaces (id, public_id, name, status) "
        f"VALUES (1, '{default_public_id}', 'My Call Center', 'active')"
    )
    op.execute("SELECT setval('workspaces_id_seq', (SELECT MAX(id) FROM workspaces))")

    # ------------------------------------------------------------------
    # 2. subscriber_seats (+ adopt demo-persona credentials)
    # ------------------------------------------------------------------
    op.create_table(
        'subscriber_seats',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('email', sa.String(255), nullable=False),
        sa.Column('display_name', sa.String(255), nullable=True),
        sa.Column('signalwire_subscriber_id', sa.String(100), nullable=True),
        sa.Column('signalwire_username', sa.String(100), nullable=True),
        sa.Column('signalwire_password_encrypted', sa.String(500), nullable=True),
        sa.Column('signalwire_address', sa.String(255), nullable=True),
        sa.Column('provisioned_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.Column('leased_by_user_id', sa.Integer(), nullable=True),
        sa.Column('leased_at', sa.DateTime(), nullable=True),
        sa.Column('lease_expires_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['leased_by_user_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('email', name='uq_subscriber_seats_email'),
    )
    op.create_index(
        'ix_subscriber_seats_signalwire_subscriber_id',
        'subscriber_seats', ['signalwire_subscriber_id'], unique=True,
    )

    # Adopt the demo personas' pre-provisioned SignalWire subscribers into
    # the seat pool, then retire the persona rows — the persona lease model
    # is gone (rows are kept inactive for FK integrity on old volumes;
    # fresh volumes never create them since demo_seed.py is deleted).
    op.execute(
        """
        INSERT INTO subscriber_seats (
            email, display_name, signalwire_subscriber_id, signalwire_username,
            signalwire_password_encrypted, signalwire_address, provisioned_at
        )
        SELECT email, name, signalwire_subscriber_id, signalwire_username,
               signalwire_password_encrypted, signalwire_address,
               fabric_subscriber_created_at
          FROM users
         WHERE role = 'demo_agent'
           AND signalwire_subscriber_id IS NOT NULL
        """
    )
    op.execute("UPDATE users SET is_active = false WHERE role = 'demo_agent'")

    # ------------------------------------------------------------------
    # 3. workspace_id columns + backfill to the default workspace
    # ------------------------------------------------------------------
    for table in _NOT_NULL_TABLES + _NULLABLE_TABLES:
        op.add_column(table, sa.Column('workspace_id', sa.Integer(), nullable=True))
        op.execute(f"UPDATE {table} SET workspace_id = 1")
        op.create_foreign_key(
            f'fk_{table}_workspace_id', table, 'workspaces',
            ['workspace_id'], ['id'],
        )
    for table in _NOT_NULL_TABLES:
        op.alter_column(table, 'workspace_id', nullable=False)

    # users: nullable — NULL means platform-level (operator admin,
    # clone-and-own users). Existing users deliberately stay NULL so
    # clone-and-own token/filter behavior is unchanged.
    op.add_column('users', sa.Column('workspace_id', sa.Integer(), nullable=True))
    op.create_foreign_key(
        'fk_users_workspace_id', 'users', 'workspaces', ['workspace_id'], ['id'],
    )
    op.create_index('ix_users_workspace_id', 'users', ['workspace_id'])

    # Hot-path indexes (list filters, emit routing, callback board).
    op.create_index('ix_calls_workspace_id', 'calls', ['workspace_id'])
    op.create_index('ix_callbacks_workspace_id', 'callbacks', ['workspace_id'])
    op.create_index('ix_conferences_workspace_id', 'conferences', ['workspace_id'])
    op.create_index('ix_webhook_events_workspace_id', 'webhook_events', ['workspace_id'])

    # ------------------------------------------------------------------
    # 4. uniqueness re-scoped per workspace
    # ------------------------------------------------------------------
    # users.email: global unique index → per-workspace expression index.
    # COALESCE(workspace_id, 0) keeps platform users (NULL workspace)
    # mutually unique too (plain composite uniques don't compare NULLs).
    op.drop_index('ix_users_email', table_name='users')
    op.create_index('ix_users_email', 'users', ['email'])
    op.execute(
        'CREATE UNIQUE INDEX uq_users_workspace_email '
        'ON users (COALESCE(workspace_id, 0), email)'
    )

    # contacts.phone: global unique index → (workspace_id, phone).
    op.drop_index('ix_contacts_phone', table_name='contacts')
    op.create_index('ix_contacts_phone', 'contacts', ['phone'])
    op.create_unique_constraint(
        'uq_contacts_workspace_phone', 'contacts', ['workspace_id', 'phone'],
    )

    # queues.slug: global unique constraint → (workspace_id, slug).
    op.drop_constraint('queues_slug_key', 'queues', type_='unique')
    op.create_unique_constraint(
        'uq_queues_workspace_slug', 'queues', ['workspace_id', 'slug'],
    )

    # document_collections.name: global unique → (workspace_id, name),
    # plus physical_name as the globally-unique search/chunk identity
    # (= name for existing rows; provisioner writes ws{ID}_{name} clones).
    op.add_column('document_collections', sa.Column('physical_name', sa.String(150), nullable=True))
    op.execute('UPDATE document_collections SET physical_name = name')
    op.alter_column('document_collections', 'physical_name', nullable=False)
    op.drop_constraint('document_collections_name_key', 'document_collections', type_='unique')
    op.create_unique_constraint(
        'uq_document_collections_workspace_name',
        'document_collections', ['workspace_id', 'name'],
    )
    op.create_unique_constraint(
        'uq_document_collections_physical_name',
        'document_collections', ['physical_name'],
    )

    # agent_collection_assignments: (agent_id, collection_id) →
    # (workspace_id, agent_id, collection_id).
    op.drop_constraint(
        'agent_collection_assignments_agent_id_collection_id_key',
        'agent_collection_assignments', type_='unique',
    )
    op.create_unique_constraint(
        'uq_agent_collection_assignments_workspace',
        'agent_collection_assignments',
        ['workspace_id', 'agent_id', 'collection_id'],
    )

    # system_config: PK key → (workspace_id, key). Existing rows land at
    # workspace 0 via the server default = the GLOBAL platform defaults
    # that per-workspace rows layer over (copy-on-write). workspace 0 is
    # not a workspaces row on purpose — no FK here.
    op.add_column(
        'system_config',
        sa.Column('workspace_id', sa.Integer(), nullable=False, server_default='0'),
    )
    op.execute('ALTER TABLE system_config DROP CONSTRAINT system_config_pkey')
    op.create_primary_key('system_config_pkey', 'system_config', ['workspace_id', 'key'])


def downgrade():
    # The pre-tenancy schema has GLOBAL uniques (contacts.phone, queues.slug,
    # document_collections.name, users.email). Visitor workspaces hold clones
    # that duplicate the default workspace's values, so restoring those
    # constraints fails unless visitor data is purged first. Downgrading
    # MEANS collapsing to the default workspace's world — delete everything
    # outside it (children first), clearing user references along the way.
    # _doomed_calls must cover EVERY link from a surviving (workspace-1) call
    # to a visitor-owned row, or the later deletes hit FK violations: calls
    # quarantined to workspace 1 by the flush stamper (webhook inbound with
    # the platform system user) can still carry a visitor assigned_agent_id
    # (queue dispatch) or a visitor-workspace contact_id.
    op.execute(
        """
        CREATE TEMPORARY TABLE _doomed_calls AS
        SELECT id FROM calls
         WHERE workspace_id <> 1
            OR user_id IN (SELECT id FROM users WHERE workspace_id IS NOT NULL)
            OR assigned_agent_id IN (SELECT id FROM users WHERE workspace_id IS NOT NULL)
            OR contact_id IN (SELECT id FROM contacts WHERE workspace_id <> 1)
        """
    )
    # Same family: quarantined ws-1 call_legs / conference_participants can
    # reference a visitor-owned conference — doom those conferences up front
    # so the child deletes can name them.
    op.execute(
        """
        CREATE TEMPORARY TABLE _doomed_conferences AS
        SELECT id FROM conferences
         WHERE (workspace_id IS NOT NULL AND workspace_id <> 1)
            OR owner_user_id IN (SELECT id FROM users WHERE workspace_id IS NOT NULL)
        """
    )
    for stmt in (
        "DELETE FROM transcriptions WHERE workspace_id <> 1 OR call_id IN (SELECT id FROM _doomed_calls)",
        "DELETE FROM call_legs WHERE workspace_id <> 1 OR call_id IN (SELECT id FROM _doomed_calls)"
        " OR user_id IN (SELECT id FROM users WHERE workspace_id IS NOT NULL)"
        " OR conference_id IN (SELECT id FROM _doomed_conferences)",
        "DELETE FROM conference_participants WHERE workspace_id <> 1 OR call_id IN (SELECT id FROM _doomed_calls)"
        " OR conference_id IN (SELECT id FROM _doomed_conferences)",
        "DELETE FROM webhook_events WHERE (workspace_id IS NOT NULL AND workspace_id <> 1)"
        " OR call_id IN (SELECT id FROM _doomed_calls)",
        "DELETE FROM callbacks WHERE workspace_id <> 1 OR call_id IN (SELECT id FROM _doomed_calls)"
        " OR claimed_by_agent_id IN (SELECT id FROM users WHERE workspace_id IS NOT NULL)",
        "DELETE FROM conferences WHERE id IN (SELECT id FROM _doomed_conferences)",
        "DELETE FROM calls WHERE id IN (SELECT id FROM _doomed_calls)",
        "DELETE FROM contacts WHERE workspace_id <> 1",
        "DELETE FROM queue_agent_assignments WHERE queue_id IN (SELECT id FROM queues WHERE workspace_id <> 1)"
        " OR user_id IN (SELECT id FROM users WHERE workspace_id IS NOT NULL)",
        "DELETE FROM queues WHERE workspace_id <> 1",
        "DELETE FROM documents WHERE workspace_id <> 1",
        "DELETE FROM agent_collection_assignments WHERE workspace_id <> 1",
        "DELETE FROM document_collections WHERE workspace_id <> 1",
        "DELETE FROM mcp_gateway_configs WHERE workspace_id <> 1",
        "UPDATE system_config SET updated_by = NULL"
        " WHERE updated_by IN (SELECT id FROM users WHERE workspace_id IS NOT NULL)",
        "UPDATE subscriber_seats SET leased_by_user_id = NULL",
        "DELETE FROM users WHERE workspace_id IS NOT NULL",
        "DROP TABLE _doomed_calls",
        "DROP TABLE _doomed_conferences",
    ):
        op.execute(stmt)

    # system_config PK back to key-only. Per-workspace layers would collide
    # on key — keep only the global (workspace 0) rows.
    op.execute('DELETE FROM system_config WHERE workspace_id <> 0')
    op.execute('ALTER TABLE system_config DROP CONSTRAINT system_config_pkey')
    op.create_primary_key('system_config_pkey', 'system_config', ['key'])
    op.drop_column('system_config', 'workspace_id')

    op.drop_constraint(
        'uq_agent_collection_assignments_workspace',
        'agent_collection_assignments', type_='unique',
    )
    op.create_unique_constraint(
        'agent_collection_assignments_agent_id_collection_id_key',
        'agent_collection_assignments', ['agent_id', 'collection_id'],
    )

    op.drop_constraint('uq_document_collections_physical_name', 'document_collections', type_='unique')
    op.drop_constraint('uq_document_collections_workspace_name', 'document_collections', type_='unique')
    op.create_unique_constraint('document_collections_name_key', 'document_collections', ['name'])
    op.drop_column('document_collections', 'physical_name')

    op.drop_constraint('uq_queues_workspace_slug', 'queues', type_='unique')
    op.create_unique_constraint('queues_slug_key', 'queues', ['slug'])

    op.drop_constraint('uq_contacts_workspace_phone', 'contacts', type_='unique')
    op.drop_index('ix_contacts_phone', table_name='contacts')
    op.create_index('ix_contacts_phone', 'contacts', ['phone'], unique=True)

    op.execute('DROP INDEX uq_users_workspace_email')
    op.drop_index('ix_users_email', table_name='users')
    op.create_index('ix_users_email', 'users', ['email'], unique=True)

    op.drop_index('ix_webhook_events_workspace_id', table_name='webhook_events')
    op.drop_index('ix_conferences_workspace_id', table_name='conferences')
    op.drop_index('ix_callbacks_workspace_id', table_name='callbacks')
    op.drop_index('ix_calls_workspace_id', table_name='calls')

    op.drop_index('ix_users_workspace_id', table_name='users')
    op.drop_constraint('fk_users_workspace_id', 'users', type_='foreignkey')
    op.drop_column('users', 'workspace_id')

    for table in _NOT_NULL_TABLES + _NULLABLE_TABLES:
        op.drop_constraint(f'fk_{table}_workspace_id', table, type_='foreignkey')
        op.drop_column(table, 'workspace_id')

    op.execute("UPDATE users SET is_active = true WHERE role = 'demo_agent'")
    op.drop_index('ix_subscriber_seats_signalwire_subscriber_id', table_name='subscriber_seats')
    op.drop_table('subscriber_seats')

    op.drop_index('ix_workspaces_status', table_name='workspaces')
    op.drop_index('ix_workspaces_verified_number', table_name='workspaces')
    op.drop_index('ix_workspaces_session_token_hash', table_name='workspaces')
    op.drop_index('ix_workspaces_public_id', table_name='workspaces')
    op.drop_table('workspaces')
