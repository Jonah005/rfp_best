"""Give a development user a role the app has no screen to assign.

The app can list roles but not assign them, so a role nobody holds (REVIEWER) leaves
any review step that needs it unanswerable. This is a development helper only.

    python scripts/grant_dev_role.py arch@acme-cyber.example.com REVIEWER          # show
    python scripts/grant_dev_role.py arch@acme-cyber.example.com REVIEWER --apply  # do it
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

os.environ.setdefault(
    "BIDREADY_DATABASE_URL",
    os.environ.get("BIDREADY_LOCAL_DATABASE_URL")
    or "postgresql+psycopg://bidready_app:bidready-local@localhost:5432/bidready",
)

from _vector_backend import align_vector_backend  # noqa: E402

align_vector_backend()

from sqlalchemy import select  # noqa: E402

from app.core.context import ExecutionContext  # noqa: E402
from app.db.session import apply_tenant_context, get_session_factory  # noqa: E402


def main() -> int:
    from app.models import Organization, Role, User, UserRole

    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) != 2:
        print(__doc__)
        return 1
    email, role_code = args[0].lower(), args[1].upper()
    apply = "--apply" in sys.argv
    session = get_session_factory()()
    try:
        org = session.execute(select(Organization)).scalars().first()
        if org is None:
            print("No organization.")
            return 1
        ctx = ExecutionContext(org_id=org.id, actor_kind="SYSTEM")
        apply_tenant_context(session, ctx)
        user = session.execute(
            select(User).where(User.org_id == org.id, User.email == email)
        ).scalar_one_or_none()
        role = session.execute(
            select(Role).where(Role.org_id == org.id, Role.code == role_code)
        ).scalar_one_or_none()
        if user is None or role is None:
            print(f"Not found: user={user is not None} role={role is not None}")
            return 1
        held = session.execute(
            select(UserRole).where(UserRole.user_id == user.id, UserRole.role_id == role.id)
        ).scalar_one_or_none()
        if held is not None:
            print(f"{email} already holds {role_code}.")
            return 0
        print(f"Would give {email} the {role_code} role.")
        if not apply:
            print("Nothing changed. Re-run with --apply.")
            return 0
        session.add(UserRole(org_id=org.id, user_id=user.id, role_id=role.id))
        session.commit()
        print("Done.")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
