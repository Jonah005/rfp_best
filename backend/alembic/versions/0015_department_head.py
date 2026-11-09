"""Name a head for a department, so staffing can stop going through one person.

`task:assign` was a single unscoped permission: whoever held it could put any
account on any task, and everyone who did not hold it could staff nothing. On a
real bid that makes the bid manager the bottleneck for every department's
staffing decision, and the workaround people actually reach for is worse than
the problem -- hand out `task:assign`, and every holder can route any task, with
its evidence, to any account in the organization.

This adds the fact that a scoped permission needs to be checkable:
`department.head_user_id`. The role `DEPARTMENT_HEAD` says an actor may act as a
head; this column says of WHICH department. Both must be true, and so must a
third thing the service checks -- that the proposed owner is a member of that
department. A head who can assign outside their own department can reach any
account in the organization, which is the same hole reopened one step down.

Nullable, and null means nobody: a department without a named head behaves
exactly as it did before this revision, with the bid manager assigning. Nothing
is granted by this column alone.

The foreign key is the tenant-aware composite (head_user_id, org_id) ->
app_user (id, org_id), so a head cannot be an account from another tenant. It is
ON DELETE SET NULL rather than RESTRICT: removing a person should not be blocked
by a department pointing at them, and the safe resting state for a missing head
is "no head" -- which falls back to the bid manager.

Revision ID: 0015_department_head
"""

from __future__ import annotations

from alembic import op

revision = "0015_department_head"
down_revision = "0014_person_link"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotent for the same reason as 0009 and 0013: revision 0004 builds its
    # tables from live SQLAlchemy metadata, so a database created from empty
    # already has this column by the time this revision runs, while one migrated
    # before the model changed does not. Both must work.
    op.execute("ALTER TABLE department ADD COLUMN IF NOT EXISTS head_user_id uuid")
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'fk_department_head_org'
          ) THEN
            ALTER TABLE department
              ADD CONSTRAINT fk_department_head_org
              FOREIGN KEY (head_user_id, org_id)
              REFERENCES app_user (id, org_id)
              ON DELETE SET NULL;
          END IF;
        END $$
        """
    )
    # The assignment check reads it by department, and the "which departments do
    # I head" list reads it by actor. Both are covered by (org_id, head_user_id).
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_department_head
          ON department (org_id, head_user_id)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_department_head")
    op.execute("ALTER TABLE department DROP CONSTRAINT IF EXISTS fk_department_head_org")
    op.execute("ALTER TABLE department DROP COLUMN IF EXISTS head_user_id")
