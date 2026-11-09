"""Print the exact text sent to the model for one task and one question.

"Is this hardcoded?" is the right question to ask of any system that answers in
confident prose, and it deserves a better answer than "trust me". So this
assembles the prompt the same way `copilot.ask` does -- same function, same
database, same permission checks -- and prints it instead of sending it.

    python scripts/show_prompt.py COM-01 "holding prices firm for 90 days"

What you get is three things, in the order the model receives them:

    1. The SYSTEM PROMPT -- fixed rules from the gateway registry. This part IS
       written by hand, and it is the only part that is. It contains no facts
       about your company, your bid, or any requirement.
    2. The AUTHORIZED CONTEXT -- assembled per question from your database:
       this task's requirements, evidence approved against them, answers from
       other departments, and whatever the retrieval query matched.
    3. The QUESTION.

Everything factual in an answer has to come from (2), and (2) is built from
rows. Change a row and the prompt changes; delete the evidence and the model
has nothing to say. That is the claim, and this prints the evidence for it.

Nothing is sent anywhere and nothing is written. No model call is made, so this
costs nothing and can be run as often as you like.
"""

from __future__ import annotations

import os
import sys

# The launcher exports the working URL only into the process it starts, so a
# script run separately has to be told. These are the local-development
# credentials `run_local.py` itself creates.
os.environ.setdefault(
    "BIDREADY_DATABASE_URL",
    os.environ.get("BIDREADY_LOCAL_DATABASE_URL")
    or "postgresql+psycopg://bidready_app:bidready-local@localhost:5432/bidready",
)

# Before any app import: `settings.vector_backend` chooses the embedding column
# type and is resolved once at import. run_local.py sets it; a script run
# directly does not, so the ORM builds pgvector columns against a real[] schema
# and dies on "malformed array literal" or "'list' object has no attribute
# 'split'". Both have cost a debugging round. Detected from the live schema.
from _vector_backend import align_vector_backend  # noqa: E402

align_vector_backend()

from sqlalchemy import select  # noqa: E402

from app.core.context import ExecutionContext  # noqa: E402
from app.db.session import apply_tenant_context, get_session_factory  # noqa: E402
from app.gateway.registry import get as get_call_type  # noqa: E402
from app.models import Organization, Task, User  # noqa: E402
from app.services import context_builder, identity  # noqa: E402

RULE = "=" * 78


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    task_code, question = sys.argv[1], " ".join(sys.argv[2:])

    session = get_session_factory()()
    try:
        org = session.execute(select(Organization)).scalars().first()
        if org is None:
            print("No organization. Seed the database first.")
            return 1

        # A system context to find the task, then the OWNER's context to build
        # the prompt -- because what the copilot may see depends on who is
        # asking, and building it as an administrator would prove nothing.
        finder = ExecutionContext(org_id=org.id, actor_kind="SYSTEM")
        apply_tenant_context(session, finder)

        # Task codes are unique per BID, not per organization: every bid has its
        # own BID-01. Asking for one code across three bids used to raise
        # MultipleResultsFound and print a SQLAlchemy stack trace, which tells
        # the reader nothing about what they did wrong or what to type instead.
        matches = (
            session.execute(
                select(Task)
                .where(Task.org_id == org.id, Task.code == task_code)
                .order_by(Task.created_at.desc())
            )
            .scalars()
            .all()
        )
        if not matches:
            codes = session.execute(select(Task.code).where(Task.org_id == org.id)).scalars().all()
            print(f"No task {task_code}. Try one of: {', '.join(sorted(set(codes))) or '(none)'}")
            return 1
        task = matches[0]
        if len(matches) > 1:
            print(
                f"note: {len(matches)} bids have a task {task_code}. Using the most recent "
                f"(bid {task.bid_id}). Pass a bid id as the third argument to choose "
                "another.\n"
            )
        if task.owner_user_id is None:
            print(f"{task_code} has no owner, so there is nobody to build a context as.")
            return 1

        owner = session.get(User, task.owner_user_id)
        ctx = ExecutionContext(
            org_id=org.id,
            actor_user_id=owner.id,
            permissions=frozenset(identity.effective_permissions(session, owner)),
            role_codes=frozenset(identity.role_codes(session, owner)),
            department_ids=frozenset(identity.department_ids(session, owner)),
        )
        apply_tenant_context(session, ctx)

        print(f"task     {task.code} — {task.title}")
        print(f"asked by {owner.display_name} <{owner.email}>")
        print(f"question {question}\n")

        # Semantic retrieval needs the local embedding model, which lives in
        # whichever interpreter runs the app. Run this script with a different
        # one and the import fails -- so fall back to lexical-only rather than
        # dying, and say so, because a prompt assembled from three of its four
        # sources is still worth reading and mislabelling it would not be.
        lexical_only = False
        try:
            built = context_builder.build(
                session, ctx, task_id=task.id, query=question, user_id=owner.id
            )
        except ModuleNotFoundError as missing:
            lexical_only = True
            print(f"note: {missing.name} is not installed in this interpreter.")
            print("      Semantic search skipped; lexical search and attached evidence still run.")
            print("      For the full picture, run this with the same python that runs the app.\n")
            from app.services.retrieval import RetrievalService

            original = RetrievalService._vector_candidates
            RetrievalService._vector_candidates = lambda *a, **k: []
            try:
                built = context_builder.build(
                    session, ctx, task_id=task.id, query=question, user_id=owner.id
                )
            finally:
                RetrievalService._vector_candidates = original

        call_type = get_call_type("task_copilot")
        print(RULE)
        print(f"SYSTEM PROMPT  (registry: task_copilot {call_type.prompt_version})")
        print("the only hand-written part, and it states no facts")
        print(RULE)
        print(call_type.system_prompt)

        print("\n" + RULE)
        print("USER MESSAGE  (assembled from your database, per question)")
        print(RULE)
        print(built.prompt_block())
        print(f"\nQUESTION: {question}")

        print("\n" + RULE)
        print("WHERE THAT CAME FROM")
        print(RULE)
        trace = built.trace
        print(f"  admissible chunks before ranking : {trace['admissible_before_ranking']}")
        print(f"  filters applied                  : {trace['filters_applied']}")
        print(
            f"  retrieval matches                : {len(built.sources)}"
            + ("  (lexical only)" if lexical_only else "")
        )
        print(f"  evidence lines attached           : {len(built.evidence_block.splitlines())}")
        print(f"  answers from other departments    : {len(built.resolution_block.splitlines())}")
        print(f"  would refuse                      : {built.out_of_scope} ({built.refusal_kind})")
        print(
            "\nNothing was sent to any model. Delete the evidence rows behind [E1] and\n"
            "re-run: the block empties, and the copilot has nothing left to answer with."
        )
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
