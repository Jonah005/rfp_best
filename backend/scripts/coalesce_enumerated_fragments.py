"""Put a bulleted list back together into the one obligation it always was.

Block 46 of the tender is one paragraph holding clause 14 and an unnumbered
"Evaluation Factors" list. It produced nine requirements:

    R-143  Jadwa conducts penetration test, all vulnerabilities found within
           3 months from final release should be fixed with no extra cost.
    R-144  Jadwa will rate proposals based on the following factors:
           Responsiveness to the requirements set forth in this RFP
    R-145  Jadwa will rate proposals based on the following factors:
           Technical approach , discussion and proposal
    R-146  ... Relevant past performance/experience
    R-147  ... Samples of work
    R-148  ... Cost, including an assessment of total cost of ownership
    R-149  ... Comprehensive proposal quality
    R-150  ... Fair comparision matrix on Jadwa criteria

R-144 to R-150 are not seven obligations. They are one sentence, split at its
bullets, with the lead-in copied onto each fragment. The same thing happened to
the role list in block 21 (R-010, R-103-R-108: "We intend to have multiple role
and responsibilities such as ...") and to the screen list in block 8216-9413.

This is what makes the clarification matrix unreadable, and the mechanism is
worth being precise about, because the linking was measured and is exact. A
clarification cites a BLOCK. A requirement records the span of the block it came
from -- not its own sentence -- so the finest join the data allows is "every
requirement from this block". Nine fragments in a block therefore means every
question about that block lands on nine rows: R-143 to R-150 each carry 32 open
findings, the same 22 questions eight times over. The fan-out is not a bug in
the join. It is the fragment count, seen from downstream.

Fragmenting also breaks the owner. R-103 to R-106 are BIDDER and R-010, R-107,
R-108 are CUSTOMER -- identical sentences, arbitrarily different owners --
because the classifier is reading a tail like "Compliance (For review of
application and approval)" with no verb in it. One row carrying the whole
sentence has something to classify.

## What a group has to look like

Deliberately narrow, and checkable by reading rather than by trusting a number:

    same block, and every member begins with the same run of words,
    that run being at least MIN_PREFIX characters and at least HALF of
    the shortest member.

That signature is what "the lead-in was copied onto each bullet" looks like in
storage, and it is hard for two genuinely different obligations to satisfy: they
would have to agree word for word for forty characters and then diverge.

## Why this merges rather than withdraws

`merge_duplicate_requirements.py` keeps one row and withdraws the rest, which is
right for restatements -- three wordings of one obligation, any of which carries
the whole thing. It is wrong here. Keeping R-144 and withdrawing R-145 to R-150
would delete six evaluation factors from the bid, which is precisely the
unrecoverable direction. So the survivor is given a NEW VERSION whose text is
the lead-in followed by every tail in document order. The merged text is a
superset of all of them; nothing that was in the matrix leaves it.

The withdrawn rows keep their versions, assessments and audit trail, and record
the survivor in `superseded_by_requirement_id`, so a merge can be read back and
argued with.

    python scripts/coalesce_enumerated_fragments.py            # show the groups
    python scripts/coalesce_enumerated_fragments.py --merge R-144,R-010

The second form is the only one that changes anything, and it acts only on the
groups whose survivor is named. There is no "merge everything it found": the
rule above is a way of surfacing candidates, not a verdict on them. Read the
merged text in the dry run first -- it is printed in full, because the one
question worth asking is whether that sentence is the obligation.
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
from app.models.enums import ObligationOwner, RequirementLifecycle  # noqa: E402
from app.services import requirements as requirement_service  # noqa: E402
from app.services.requirements import resolved_obligation_owner  # noqa: E402

STAMP = "script:coalesce_enumerated_fragments@v1"

# A copied lead-in is long. Forty characters is about seven words, which is more
# agreement than two unrelated obligations reach by accident, and the second
# test stops a long clause from swallowing a short one that merely starts the
# same way.
MIN_PREFIX = 40
HALF = 0.5


def _flat(text: str | None) -> str:
    return " ".join((text or "").split())


def _word_prefix(a: str, b: str) -> str:
    """The leading words the two share, cut at a word boundary.

    Character-level would happily stop mid-word and call "Compliance" and
    "Complete access" a 7-character agreement.

    Trailing punctuation is ignored when comparing, and kept in what is
    returned. The first version compared the raw tokens, so "system" and
    "system," disagreed and the shared run stopped one word early -- which put
    that word at the head of every tail and produced

        ... create a new design for this system; system, they may use ...

    The same thing happened to "area" and to "approach." against "approach:".
    A comma is not where two fragments of one sentence stop agreeing.
    """
    left, right = a.split(" "), b.split(" ")
    shared: list[str] = []
    for x, y in zip(left, right):
        if x.rstrip(".,;:") != y.rstrip(".,;:"):
            break
        shared.append(x)
    return " ".join(shared)


def _qualifies(prefix: str, members: list[str]) -> bool:
    if len(prefix) < MIN_PREFIX:
        return False
    shortest = min(len(m) for m in members)
    return len(prefix) >= HALF * shortest


def _listed(flag: str) -> set[str]:
    for index, value in enumerate(sys.argv):
        if value == flag and index + 1 < len(sys.argv):
            return {c.strip().upper() for c in sys.argv[index + 1].split(",") if c.strip()}
        if value.startswith(f"{flag}="):
            return {c.strip().upper() for c in value.split("=", 1)[1].split(",") if c.strip()}
    return set()


def _move_clarification_links(session, org_id, *, drop: list, survivor) -> int:
    """Point a withdrawn requirement's clarifications at the one that replaced it.

    Withdrawing without this leaves the join table addressing rows that no
    longer appear anywhere. The first run of this script did exactly that: 25
    requirements were merged away and an export then referenced 129 requirement
    codes against 104 existing ones, with clarifications citing R-145 to R-150
    in a workbook that stops at R-144.

    Deleting the links instead would be the wrong repair. The question was
    raised about that clause and the clause is still in the bid -- it is now
    part of the survivor -- so the link moves rather than disappears.

    A link the survivor already holds is dropped rather than moved: the join is
    unique on (clarification, requirement), and inserting a second would fail
    the constraint in the middle of a merge.
    """
    from app.models import ClarificationRequirement

    if not drop:
        return 0
    existing = {
        row.clarification_id
        for row in session.execute(
            select(ClarificationRequirement).where(
                ClarificationRequirement.org_id == org_id,
                ClarificationRequirement.requirement_id == survivor,
            )
        ).scalars()
    }
    moved = 0
    for link in session.execute(
        select(ClarificationRequirement).where(
            ClarificationRequirement.org_id == org_id,
            ClarificationRequirement.requirement_id.in_(drop),
        )
    ).scalars():
        if link.clarification_id in existing:
            session.delete(link)
            continue
        link.requirement_id = survivor
        existing.add(link.clarification_id)
        moved += 1
    session.flush()
    return moved


def _group(block_members: list[tuple]) -> list[list[tuple]]:
    """Connected components over "shares a qualifying lead-in"."""
    n = len(block_members)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        for j in range(i + 1, n):
            a, b = _flat(block_members[i][2]), _flat(block_members[j][2])
            if _qualifies(_word_prefix(a, b), [a, b]):
                parent[find(i)] = find(j)

    buckets: dict[int, list[tuple]] = {}
    for i, member in enumerate(block_members):
        buckets.setdefault(find(i), []).append(member)
    return [g for g in buckets.values() if len(g) > 1]


def main() -> int:
    merge_codes = _listed("--merge")

    from app.models import (
        DocumentBlock,
        Organization,
        Requirement,
        RequirementVersion,
    )

    session = get_session_factory()()
    try:
        org = session.execute(select(Organization)).scalars().first()
        if org is None:
            print("No organization.")
            return 1
        ctx = ExecutionContext(org_id=org.id, actor_kind="SYSTEM")
        apply_tenant_context(session, ctx)

        rows = session.execute(
            select(Requirement, RequirementVersion)
            .join(RequirementVersion, RequirementVersion.id == Requirement.current_version_id)
            .where(
                Requirement.org_id == org.id,
                Requirement.lifecycle_state.notin_(
                    [RequirementLifecycle.WITHDRAWN, RequirementLifecycle.SUPERSEDED]
                ),
            )
            .order_by(Requirement.code)
        ).all()

        blocks = {
            (b.document_version_id, b.char_start, b.char_end): b
            for b in session.execute(
                select(DocumentBlock).where(DocumentBlock.org_id == org.id)
            ).scalars()
        }

        per_block: dict[tuple, list[tuple]] = {}
        for req, version in rows:
            locator = version.source_locator or {}
            key = (
                version.source_document_version_id,
                locator.get("char_start"),
                locator.get("char_end"),
            )
            per_block.setdefault(key, []).append((req, version, version.requirement_text))

        merged_groups = 0
        found = 0
        for key, members in sorted(per_block.items(), key=lambda kv: kv[0][1] or 0):
            for group in _group(members):
                found += 1
                texts = [_flat(m[2]) for m in group]
                prefix = texts[0]
                for text in texts[1:]:
                    prefix = _word_prefix(prefix, text)

                block = blocks.get(key)
                # Document order, not code order: the codes were handed out in
                # the order the model emitted the bullets, which is not always
                # the order they appear on the page, and the merged sentence
                # should read the way the tender reads.
                # Cut the tail by WORD COUNT, never by len(prefix).
                #
                # The shared run is spelled with the first member's punctuation,
                # so it can be a character shorter or longer than the same words
                # in another member: slicing "...following design area: Page
                # design" at 95 characters left the tail "n". Words are what was
                # actually compared, so words are what must be dropped.
                prefix_words = len(prefix.split(" "))

                def _tail(text: str) -> str:
                    return " ".join(text.split(" ")[prefix_words:]).strip(" .;:,")

                def _where(text: str) -> int:
                    tail = _tail(text)
                    if block is not None and tail:
                        at = (block.text or "").find(tail[:40])
                        if at >= 0:
                            return at
                    return 10**9

                ordered = sorted(zip(group, texts), key=lambda pair: _where(pair[1]))
                tails = []
                for _member, text in ordered:
                    tail = _tail(text)
                    if tail and tail not in tails:
                        tails.append(tail)
                # The lead-in keeps its own punctuation. Forcing a colon in
                # produced "...following design: area Page design" on a group
                # whose shared run stopped mid-phrase; the tender's own
                # punctuation is right more often than a rule about it.
                merged_text = prefix.rstrip() + " " + "; ".join(tails)

                survivor = min((m[0] for m in group), key=lambda r: r.code)
                drop = [m[0] for m in group if m[0].id != survivor.id]
                owner_now = survivor.obligation_owner
                owner_after = resolved_obligation_owner(
                    merged_text, owner_now or ObligationOwner.BIDDER
                )
                # If any fragment was the bidder's, the merged clause is the
                # bidder's.
                #
                # Block 13658-13954 is the case that demands this. Its lead-in
                # is "Jadwa will provide SMS service connection details", so the
                # survivor is CUSTOMER -- but two of the four bullets under it
                # are "Ensure Arabic SMS is supported" and "Easy to re-configure
                # the SMS service credentials if changed", which are work for
                # the bidder and were labelled BIDDER. Merging under the
                # lead-in's owner would file both under a customer-owned row,
                # and the Gaps sheet excludes customer-owned rows -- so two real
                # obligations would leave the bid silently, which is the one
                # direction this codebase never takes.
                #
                # This only ever moves toward BIDDER, the same asymmetry
                # `resolved_obligation_owner` is built on: a wrongly-included
                # obligation is a cluttered list, a wrongly-excluded one is a
                # missed commitment.
                if any(m[0].obligation_owner is ObligationOwner.BIDDER for m in group):
                    owner_after = ObligationOwner.BIDDER
                owners = sorted(
                    {(m[0].obligation_owner.value if m[0].obligation_owner else "?") for m in group}
                )

                print("=" * 92)
                print(f"block {key[1]}-{key[2]}   {len(group)} fragments -> 1")
                print(
                    f"  survivor: {survivor.code}   withdraw: "
                    f"{', '.join(r.code for r in sorted(drop, key=lambda x: x.code))}"
                )
                print(f"  shared lead-in ({len(prefix)} chars): {prefix}")
                if len(owners) > 1:
                    print(f"  owners currently disagree across the fragments: {', '.join(owners)}")
                if owner_after != owner_now:
                    print(
                        f"  owner would move "
                        f"{owner_now.value if owner_now else None} -> {owner_after.value}"
                    )
                print("  merged text:")
                print(f"    {merged_text}")

                if survivor.code not in merge_codes:
                    continue

                print("  MERGING (survivor named on --merge)")
                current = next(m[1] for m in group if m[0].id == survivor.id)
                next_no = (
                    session.execute(
                        select(RequirementVersion.version_no)
                        .where(
                            RequirementVersion.org_id == org.id,
                            RequirementVersion.requirement_id == survivor.id,
                        )
                        .order_by(RequirementVersion.version_no.desc())
                        .limit(1)
                    ).scalar_one()
                    + 1
                )
                version = RequirementVersion(
                    org_id=org.id,
                    requirement_id=survivor.id,
                    version_no=next_no,
                    requirement_text=merged_text,
                    normalized_requirement=merged_text,
                    source_document_version_id=current.source_document_version_id,
                    source_locator=current.source_locator,
                    clause_ref=current.clause_ref,
                    clause_resolution_id=current.clause_resolution_id,
                    change_reason=(
                        "Coalesced "
                        + ", ".join(r.code for r in sorted(drop, key=lambda x: x.code))
                        + " back into the single enumerated clause they were split from."
                    ),
                    extracted_by=STAMP,
                )
                session.add(version)
                session.flush()
                current.superseded_at = datetime.now(timezone.utc)
                survivor.current_version_id = version.id
                survivor.obligation_owner = owner_after
                # One call, because withdrawing is three facts: the lifecycle
                # and successor, the clarification links, and the assessment
                # that must stop claiming to be current. This script had the
                # first two right and the third wrong -- as did every other
                # caller -- which is how the live bid came to hold 158 current
                # assessments against 89 live requirements.
                for requirement in drop:
                    requirement_service.withdraw(
                        session,
                        ctx,
                        requirement=requirement,
                        successors=[survivor.id],
                        reason="Enumerated fragment of a single obligation.",
                    )
                merged_groups += 1

        print("=" * 92)
        print(f"{found} group(s) of fragments found.")

        if not merge_codes:
            print(
                "\nNothing was changed. Read each merged text above -- the question is\n"
                "whether that sentence is the obligation -- then name the survivors:\n"
                "    python scripts/coalesce_enumerated_fragments.py --merge R-144,R-010"
            )
            return 0

        missing = merge_codes - {
            min((m[0] for m in g), key=lambda r: r.code).code
            for members in per_block.values()
            for g in _group(members)
        }
        if missing:
            print("Not merged: " + ", ".join(sorted(missing)))
            print("  Either no such requirement, or it is not the lowest code in a group.")

        if merged_groups == 0:
            print("Nothing to merge.")
            return 0

        session.commit()
        print(
            f"\nMerged {merged_groups} group(s). The fragments are WITHDRAWN, not deleted, and\n"
            "each records the survivor in superseded_by_requirement_id.\n\n"
            "Re-run the assessment. Every count downstream was computed against the\n"
            "fragments: the matrix rows, the clarification links (a question now reaches\n"
            "fewer rows because the block holds fewer requirements), the readiness\n"
            "verdicts and the task states."
        )
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
