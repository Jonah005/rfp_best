"""Connect the people in the CVs to the people with accounts.

A staffing requirement asks for named personnel with their qualifications. The
corpus knows the people — extraction turns a CV pack into PERSON entities with
their certifications attached. The organization knows the accounts. Nothing
connected the two, so a staffing answer could name nobody and a task about an
employee could not be routed to them.

The two populations are separate on purpose and stay separate. `app_user` rows
are identities, created only by seeding or an org admin; PERSON entities are
evidence, extracted from untrusted uploads. If extraction could create or claim
an account, uploading a CV would mint an identity. This table is the bridge, and
it is built to carry as little as possible:

* A link grants nothing — no role, no permission, no access to any evidence.
  Nothing in the permission evaluator reads this table. It records a fact.
* A matcher proposes; a knowledge administrator decides. A PROPOSED row is
  treated as absent by everything except the review list.
* A rejection is stored, not forgotten, so the next run does not propose the
  same wrong pair and turn a reviewer's decision into a recurring chore.

Revision ID: 0014_person_link
"""

from __future__ import annotations

from alembic import op
from app.db.base import Base

revision = "0014_person_link"
down_revision = "0013_obligation_owner"
branch_labels = None
depends_on = None

APP_ROLE = "bidready_app"
TABLE = "person_link"


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'person_link_status') THEN
            CREATE TYPE person_link_status AS ENUM ('PROPOSED', 'CONFIRMED', 'REJECTED');
          END IF;
        END $$
        """
    )
    Base.metadata.create_all(bind=op.get_bind(), tables=[Base.metadata.tables[TABLE]])

    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON {TABLE}
          USING      (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid)
          WITH CHECK (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid)
        """
    )
    op.execute(
        f"""
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
            GRANT SELECT, INSERT, UPDATE, DELETE ON {TABLE} TO {APP_ROLE};
          END IF;
        END $$
        """
    )


def downgrade() -> None:
    op.execute(f"DROP TABLE IF EXISTS {TABLE} CASCADE")
    op.execute("DROP TYPE IF EXISTS person_link_status")
