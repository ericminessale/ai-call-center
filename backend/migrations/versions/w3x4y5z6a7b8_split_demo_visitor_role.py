"""Re-label hosted-demo workspace owners from 'admin' to 'visitor'.

Revision ID: w3x4y5z6a7b8
Revises: v2w3x4y5z6a7
Create Date: 2026-07-28 12:00:00.000000

HIGH-3. Every hosted-demo visitor used to be provisioned ``role='admin'``
(``services/workspace_provision.py``) and the ``/api/admin/*`` blueprint gate
checked only ``role == 'admin'`` — so an anonymous visitor reached the whole
admin surface minus the six platform-gated routes, scoped by the tenancy *data*
filter only, which does nothing about side-effecting endpoints. Provisioning
now mints ``role='visitor'``; this revision brings already-provisioned
workspaces in line so live visitors lose the elevated surface without waiting
for their workspace to be reaped and re-created.

DATA-only: ``users.role`` is a plain ``VARCHAR(50)``, not a Postgres ENUM, so
adding a role value needs no schema change and there is nothing to ALTER.

Scope of the UPDATE — workspace-scoped admins only:
  * ``workspace_id IS NULL`` → platform-level users: the operator and every
    clone-and-own admin. Untouched; they are the real admins.
  * ``workspace_id = 1``     → the TEMPLATE workspace (``DEFAULT_WORKSPACE_ID``).
    No code path creates users there, but excluded explicitly so a
    hand-seeded template admin can't be demoted.
  * everything else          → a hosted visitor's workspace. Both the owner row
    and any extra 'admin' a visitor minted through the previously-ungated
    ``POST /api/admin/users`` are demoted; both were over-privileged.

The downgrade is the exact inverse and restores the pre-HIGH-3 state the older
code expects (``_workspace_owner`` looked for ``role='admin'``).
"""

from alembic import op


revision = 'w3x4y5z6a7b8'
down_revision = 'v2w3x4y5z6a7'
branch_labels = None
depends_on = None


# Kept as literals rather than importing app.models.user: a migration has to
# keep describing the schema as it was at this revision even if the constants
# are later renamed.
_DEMO_OWNER_PREDICATE = (
    "workspace_id IS NOT NULL AND workspace_id <> 1"
)


def upgrade():
    op.execute(
        "UPDATE users SET role = 'visitor' "
        f"WHERE {_DEMO_OWNER_PREDICATE} AND role = 'admin'"
    )


def downgrade():
    op.execute(
        "UPDATE users SET role = 'admin' "
        f"WHERE {_DEMO_OWNER_PREDICATE} AND role = 'visitor'"
    )
