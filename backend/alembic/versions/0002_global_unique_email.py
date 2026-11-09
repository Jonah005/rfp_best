"""Authentication hardening: globally unique normalized email.

`auth_lookup_user(email)` resolves a tenant FROM an email, before any tenant
context exists. That lookup is only well defined if an address identifies at
most one user across the whole deployment. The previous `UNIQUE (org_id, email)`
permitted the same address in two organizations, which would have made the
lookup ambiguous -- and "pick one" is not an acceptable answer to "who is
signing in".

The identity model already gives each user exactly one Organization, so making
the address globally unique costs nothing today. Uniqueness is enforced on
`lower(email)` so that casing cannot be used to create a duplicate: the lookup
compares case-insensitively, and the constraint must agree with it.

If a future release genuinely needs one human in several organizations, the
answer is a separate person/membership model, not a weakened login lookup.

Revision ID: 0002_global_unique_email
"""

from __future__ import annotations

from alembic import op

revision = "0002_global_unique_email"
down_revision = "0001_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Fail loudly rather than silently dropping data if duplicates already
    # exist. A migration that quietly deletes user accounts is worse than one
    # that stops and asks.
    op.execute(
        """
        DO $$
        DECLARE dup_count integer;
        BEGIN
          SELECT count(*) INTO dup_count FROM (
            SELECT lower(email) FROM app_user GROUP BY lower(email) HAVING count(*) > 1
          ) d;
          IF dup_count > 0 THEN
            RAISE EXCEPTION
              'Cannot enforce globally unique email: % address(es) appear in more than '
              'one organization. Resolve these before upgrading.', dup_count;
          END IF;
        END $$
        """
    )

    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_app_user_email_lower ON app_user (lower(email))"
    )

    # Now redundant: subsumed by the global constraint above.
    op.execute("ALTER TABLE app_user DROP CONSTRAINT IF EXISTS uq_app_user_org_email")

    # Re-assert the SECURITY DEFINER hardening as an explicit, auditable step
    # rather than relying on it having been done correctly in 0001.
    op.execute("ALTER FUNCTION auth_lookup_user(text) SET search_path = public, pg_temp")
    op.execute("REVOKE ALL ON FUNCTION auth_lookup_user(text) FROM PUBLIC")
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bidready_app') THEN
            GRANT EXECUTE ON FUNCTION auth_lookup_user(text) TO bidready_app;
          END IF;
        END $$
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_app_user_email_lower")
    op.execute("ALTER TABLE app_user ADD CONSTRAINT uq_app_user_org_email UNIQUE (org_id, email)")
