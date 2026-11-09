"""Undo a rewrite that replaced a requirement with a fragment of itself.

Prompt v6 told the model to quote the block and to raise one requirement per
list item. Both instructions were written for block 26 -- three bare bullets
with no subject -- and both were then applied to every compound clause in the
document. Reconciliation matched each proposal to the requirement already
there, the wording differed, so a NEW VERSION was written and the old text
stopped being current. The requirement kept its code and lost most of its
content:

    R-003  was: the technical proposal must contain a solution overview, key
                personnel CVs, and a proposed schedule with milestones
           now: "the technical proposal should provide a proposed schedule and
                milestones, as applicable"

    R-004  was: two-page limit, fixed price, and payment terms on milestones
           now: "Payment terms and conditions on milestones should be specified."

Nine requirements lost content this way. Nothing was deleted -- the previous
wording is still there as a superseded RequirementVersion, which is the whole
reason versions exist -- but the matrix, the assessment and any draft read the
current version, so the obligation is effectively gone from the bid.

This lists every rewrite the current extractor made, with both texts, and
restores the ones you name.

Two numbers order the list: how much shorter the new text is, and how much of it
was already in the old. A rewrite that is much shorter and adds no words of its
own is what dropping half a compound clause looks like, so those sort first.
They are a reading order and nothing more -- the asymmetry that governs the rest
of this codebase applies here too, and restoring a requirement that did not need
it is visible and reversible while leaving an obligation truncated is neither.

`obligation_owner` is re-derived from the restored text rather than guessed at.
It lives on the requirement, not the version, so it was overwritten with no
history to consult -- v6's quoting put the document's own voice into the clause
("Jadwa will provide access...", "Testing will be conducted...") and the
classifier read the grammatical subject as the obligated party, flipping real
bidder work to CUSTOMER. `resolved_obligation_owner` applies the same rule
extraction uses, so this cannot disagree with the pipeline.

    python scripts/revert_fragmenting_rewrites.py                 # review every rewrite
    python scripts/revert_fragmenting_rewrites.py --restore R-003,R-004

The second form is the only one that changes anything, and it acts only on the
codes it is given. There is no "restore everything the script thinks is a
fragment", deliberately: the two numbers below are a sort order, not a verdict.
Every threshold I have chosen by eye in this codebase has been wrong at least
once -- the similarity floor, the merge bar, the autojunk fixture -- and each
time the calibrating example was one I wrote myself rather than one from the
data. So this ranks the rewrites and prints both texts, and a person decides.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

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
from app.models.enums import ObligationOwner, RequirementLifecycle  # noqa: E402
from app.services.requirements import resolved_obligation_owner  # noqa: E402

# Sorting hints, not thresholds. A rewrite is worth reading first when it is
# much shorter than what it replaced and adds no words of its own -- that is
# what dropping half a compound clause looks like. Neither number decides
# anything on its own.
SHORT = 0.85
CONTAINED = 0.85


def _words(text: str) -> list[str]:
    return [w.strip(".,;:()\"'").lower() for w in (text or "").split() if len(w) > 3]


def _containment(new: str, old: str) -> float:
    """Share of the new text's significant words already present in the old."""
    words = _words(new)
    if not words:
        return 1.0
    haystack = set(_words(old))
    return sum(1 for w in words if w in haystack) / len(words)


def _listed(flag: str) -> set[str]:
    for index, value in enumerate(sys.argv):
        if value == flag and index + 1 < len(sys.argv):
            return {c.strip().upper() for c in sys.argv[index + 1].split(",") if c.strip()}
        if value.startswith(f"{flag}="):
            return {c.strip().upper() for c in value.split("=", 1)[1].split(",") if c.strip()}
    return set()


def main() -> int:
    restore_codes = _listed("--restore")

    from app.models import Organization, Requirement, RequirementVersion

    call_type = get_call_type("requirement_decomposition")
    current_stamp = f"llm:{call_type.name}@{call_type.prompt_version}"
    print(f"current extractor: {current_stamp}")
    print(
        "every rewrite by that extractor is listed, shortest first.\n"
        "'shorter' and 'reused' are how the list is ordered -- they decide nothing.\n"
    )

    session = get_session_factory()()
    try:
        org = session.execute(select(Organization)).scalars().first()
        if org is None:
            print("No organization.")
            return 1
        ctx = ExecutionContext(org_id=org.id, actor_kind="SYSTEM")
        apply_tenant_context(session, ctx)

        requirements = (
            session.execute(
                select(Requirement).where(
                    Requirement.org_id == org.id,
                    Requirement.lifecycle_state != RequirementLifecycle.WITHDRAWN,
                )
            )
            .scalars()
            .all()
        )

        candidates = []
        for requirement in requirements:
            versions = (
                session.execute(
                    select(RequirementVersion)
                    .where(
                        RequirementVersion.org_id == org.id,
                        RequirementVersion.requirement_id == requirement.id,
                    )
                    .order_by(RequirementVersion.version_no)
                )
                .scalars()
                .all()
            )
            if len(versions) < 2:
                continue
            current, previous = versions[-1], versions[-2]
            if current.extracted_by != current_stamp:
                continue
            ratio = len(current.requirement_text) / max(len(previous.requirement_text), 1)
            contained = _containment(current.requirement_text, previous.requirement_text)
            candidates.append((ratio, contained, requirement, current, previous))

        examined = len(candidates)
        candidates.sort(key=lambda row: (row[0], -row[1]))

        restored = 0
        for ratio, contained, requirement, current, previous in candidates:
            owner_now = requirement.obligation_owner
            # Re-derived with the pipeline's own rule, from the text being
            # restored. Nothing here invents an owner: the rule only ever moves
            # CUSTOMER back to BIDDER, the direction that cannot lose an
            # obligation from the bid.
            owner_after = resolved_obligation_owner(
                previous.requirement_text, owner_now or ObligationOwner.BIDDER
            )
            named = requirement.code in restore_codes
            likely = ratio <= SHORT and contained >= CONTAINED
            flag = "  <== much shorter, adds nothing" if likely else ""
            print("=" * 78)
            print(
                f"{requirement.code}  shorter: {ratio:.0%} of previous   "
                f"reused: {contained:.0%} of its words{flag}"
            )
            print(f"  now (v{current.version_no}, {current.extracted_by}):")
            print(f"    {' '.join(current.requirement_text.split())}")
            print(f"  previous (v{previous.version_no}, {previous.extracted_by}):")
            print(f"    {' '.join(previous.requirement_text.split())}")
            if owner_after != owner_now:
                print(
                    f"  owner would move {owner_now.value if owner_now else None}"
                    f" -> {owner_after.value}"
                )
            if not named:
                continue
            print("  RESTORING (named on --restore)")
            restored += 1
            current.superseded_at = datetime.now(timezone.utc)
            previous.superseded_at = None
            requirement.current_version_id = previous.id
            requirement.obligation_owner = owner_after

        print("=" * 78)
        print(f"{examined} requirement(s) were rewritten by {current_stamp}.")

        missing = restore_codes - {r.code for _, _, r, _, _ in candidates}
        if missing:
            print(
                "Not restored: " + ", ".join(sorted(missing)) + "\n"
                f"  Either no such requirement, or it was not rewritten by {current_stamp}\n"
                "  and so has nothing to restore."
            )

        if not restore_codes:
            print(
                "\nNothing was changed. Read both texts in each entry above, then name the\n"
                "ones to put back:\n"
                "    python scripts/revert_fragmenting_rewrites.py --restore R-003,R-004"
            )
            return 0

        if restored == 0:
            print("Nothing to restore.")
            return 0

        session.commit()
        print(
            f"\nRestored {restored} requirement(s) to their previous wording. The v6 text is\n"
            "kept as a superseded version, so nothing is lost in either direction.\n\n"
            "Re-run the assessment: the matrix, the evidence links and the readiness\n"
            "verdicts were all computed against the fragments."
        )
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
