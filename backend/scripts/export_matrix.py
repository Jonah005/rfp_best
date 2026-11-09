"""Export what the system extracted and what it concluded, as three workbooks.

    python scripts/export_matrix.py                    # newest bid, into ./exports
    python scripts/export_matrix.py --bid <uuid>       # a particular bid
    python scripts/export_matrix.py --out D:\\somewhere

Three files, because they answer three different questions and get read by
different people:

    01_tender_extract.xlsx    what we found in the customer's documents
    02_company_extract.xlsx   what we found in our own documents
    03_requirement_matrix.xlsx  what happened when the two were compared

The third is the one a bid manager lives in. The first two exist so that a
disagreement with it can be settled: if the matrix says a requirement has no
evidence, the question is always "did we not have it, or did we not find it",
and those are answered in different files.

Nothing here computes anything new. Every column is read from a row that some
part of the pipeline already wrote, so this is a view of the database rather
than a second opinion about it. Where a column needs explaining, the Legend
sheet in each workbook explains it, next to the caveats that matter -- which
matter more than usual here, because a matrix exported to Excel is exactly the
artefact that gets mailed to someone who was not in the conversation.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import date, datetime
from pathlib import Path

# Running `python scripts/export_matrix.py` puts scripts/ on sys.path, not the
# project root, so `scripts` is not an importable package and a sibling import
# fails. Add this file's own directory explicitly: it makes the script work the
# same whether it is run from the backend directory, by absolute path, or from
# anywhere else, which is what a person actually does.
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

from openpyxl import Workbook  # noqa: E402
from openpyxl.styles import Alignment, Font, PatternFill  # noqa: E402
from openpyxl.utils import get_column_letter  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.core.context import ExecutionContext  # noqa: E402
from app.db.session import apply_tenant_context, get_session_factory  # noqa: E402

HEADER_FILL = PatternFill("solid", fgColor="1F3B33")
HEADER_FONT = Font(name="Arial", size=10, bold=True, color="FFFFFF")
BODY_FONT = Font(name="Arial", size=10)
NOTE_FONT = Font(name="Arial", size=10, italic=True, color="555555")
WARN_FILL = PatternFill("solid", fgColor="FDF1EC")

# Wide enough to read, narrow enough that the sheet fits on a screen. A column
# of requirement text is capped rather than autosized: one 900-character clause
# would otherwise set the width for every row.
MAX_WIDTH = 70


def cell_value(value):
    """Whatever the database gave us, rendered as something Excel can hold."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (datetime, date)):
        return value.replace(tzinfo=None) if isinstance(value, datetime) else value
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (list, tuple, set)):
        return ", ".join(str(cell_value(v)) for v in value)
    if isinstance(value, dict):
        return "; ".join(f"{k}={cell_value(v)}" for k, v in value.items())
    if hasattr(value, "value"):  # an enum
        return value.value
    return value


def write_sheet(book: Workbook, title: str, headers: list[str], rows: list[list], note: str = ""):
    """One table, with a frozen header row and an autofilter.

    Excel truncates a sheet name at 31 characters and rejects several
    punctuation marks, so the title is trimmed here rather than raising from
    deep inside openpyxl with a message that does not name the sheet.
    """
    safe = title[:31]
    sheet = book.create_sheet(safe)
    start = 1
    if note:
        sheet.cell(row=1, column=1, value=note).font = NOTE_FONT
        start = 3

    for index, header in enumerate(headers, start=1):
        cell = sheet.cell(row=start, column=index, value=header)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(vertical="center", wrap_text=True)

    for r, row in enumerate(rows, start=start + 1):
        for c, value in enumerate(row, start=1):
            cell = sheet.cell(row=r, column=c, value=cell_value(value))
            cell.font = BODY_FONT
            cell.alignment = Alignment(vertical="top", wrap_text=True)

    for index, header in enumerate(headers, start=1):
        longest = max(
            [len(str(header))] + [len(str(cell_value(row[index - 1]))) for row in rows[:200]]
            or [10]
        )
        sheet.column_dimensions[get_column_letter(index)].width = min(
            max(12, longest + 2), MAX_WIDTH
        )

    sheet.freeze_panes = sheet.cell(row=start + 1, column=1)
    if rows:
        sheet.auto_filter.ref = f"A{start}:{get_column_letter(len(headers))}{start + len(rows)}"
    return sheet


def write_legend(book: Workbook, lines: list[tuple[str, str]], caveats: list[str]):
    """What the columns mean, and what the sheet does not say.

    A workbook leaves the room. The person who opens it next was not here for
    the conversation where someone explained that two status columns are not
    one status column, and they will read the friendlier-looking one.
    """
    sheet = book.create_sheet("Legend", 0)
    sheet.cell(row=1, column=1, value="What this workbook contains").font = Font(
        name="Arial", size=12, bold=True
    )
    row = 3
    for term, meaning in lines:
        sheet.cell(row=row, column=1, value=term).font = Font(name="Arial", size=10, bold=True)
        sheet.cell(row=row, column=2, value=meaning).font = BODY_FONT
        sheet.cell(row=row, column=2).alignment = Alignment(wrap_text=True, vertical="top")
        row += 1

    row += 1
    sheet.cell(row=row, column=1, value="Read this before acting on the numbers").font = Font(
        name="Arial", size=11, bold=True
    )
    row += 1
    for caveat in caveats:
        cell = sheet.cell(row=row, column=1, value=caveat)
        cell.font = BODY_FONT
        cell.alignment = Alignment(wrap_text=True, vertical="top")
        cell.fill = WARN_FILL
        sheet.merge_cells(start_row=row, start_column=1, end_row=row, end_column=2)
        row += 1

    sheet.column_dimensions["A"].width = 34
    sheet.column_dimensions["B"].width = 96
    return sheet


def finish(book: Workbook, path: Path) -> Path:
    """openpyxl creates a default empty sheet; drop it before saving."""
    if "Sheet" in book.sheetnames:
        del book["Sheet"]
    path.parent.mkdir(parents=True, exist_ok=True)
    book.save(path)
    return path


# --- the three workbooks ------------------------------------------------------


def build_tender(session, ctx, bid, rows) -> Workbook:
    book = Workbook()
    write_legend(
        book,
        [
            ("Bid", "The opportunity and the tender's own stated facts."),
            ("Requirements", "Every requirement extracted from the tender, with its text."),
            (
                "Clarifications",
                "Defects found IN THE TENDER: blanks, placeholders, contradictions.",
            ),
            ("Deliverables", "What must be submitted, in what format, by when."),
            ("Clause precedence", "Where two documents disagreed, which one won and why."),
            ("Tender documents", "What was uploaded, parsed and indexed."),
        ],
        [
            "Obligation owner matters. A requirement owned by the CUSTOMER describes what they "
            "will do; it can never be evidenced by us and is not a gap (its readiness reads "
            "CUSTOMER_OBLIGATION). One exception is deliberate: a CUSTOMER clause that hands "
            "something over and says what it is for (access, credentials, a subscription) "
            "usually hides bidder work that was not split into a row of its own. It keeps a "
            "task and a readiness for a person to confirm, and appears in Gaps.",
            "Applicability is not the same as importance. A NOT_APPLICABLE requirement was ruled "
            "out for a stated reason, recorded in the applicability reason column.",
            "A requirement with no text has no current version row. That is an extraction "
            "failure worth investigating, not an empty requirement.",
        ],
    )
    write_sheet(book, "Bid", rows["bid_headers"], rows["bid"])
    write_sheet(book, "Requirements", rows["req_headers"], rows["requirements"])
    write_sheet(book, "Clarifications", rows["clar_headers"], rows["clarifications"])
    write_sheet(book, "Deliverables", rows["deliv_headers"], rows["deliverables"])
    write_sheet(book, "Clause precedence", rows["clause_headers"], rows["clauses"])
    write_sheet(book, "Tender documents", rows["tdoc_headers"], rows["tender_documents"])
    return book


def build_company(session, ctx, rows) -> Workbook:
    book = Workbook()
    write_legend(
        book,
        [
            ("Documents", "Company documents, their classified type and indexing state."),
            ("Evidence", "Every extracted excerpt, with its approval and currentness."),
            ("Entities", "People, certifications, products and projects in the knowledge graph."),
            ("Facts", "Attributes attached to an entity, with validity dates."),
            ("Relations", "How two entities are connected."),
            ("People links", "Which knowledge-graph person maps to which account."),
        ],
        [
            "PROVES CAPABILITY = no marks a blank form, a template, a questionnaire or a previous "
            "TENDER. Such an item shows the shape of an answer, not that the company has done "
            "anything, and it must never be cited as proof of a capability.",
            "Company knowledge is org-wide and deliberately not per-bid: reuse is the asset. "
            "Tender text is per-bid and never crosses between bids.",
            "APPROVED and CURRENT_VERIFIED are different axes. Approval is a human decision about "
            "reuse; status is about whether the material is still in date.",
        ],
    )
    write_sheet(book, "Documents", rows["cdoc_headers"], rows["company_documents"])
    write_sheet(book, "Evidence", rows["ev_headers"], rows["evidence"])
    write_sheet(book, "Entities", rows["ent_headers"], rows["entities"])
    write_sheet(book, "Facts", rows["fact_headers"], rows["facts"])
    write_sheet(book, "Relations", rows["rel_headers"], rows["relations"])
    write_sheet(book, "People links", rows["plink_headers"], rows["person_links"])
    return book


def build_matrix(book_rows) -> Workbook:
    book = Workbook()
    write_legend(
        book,
        [
            ("Matrix", "One row per requirement: what it asks, and where we stand."),
            (
                "Requirement x Evidence",
                "Which evidence was attached to which requirement, and how.",
            ),
            (
                "Coverage summary",
                "Counts by category and readiness. Formulas, so they follow edits.",
            ),
            ("Gaps", "Mandatory, ours to answer, and not ready. The work list."),
            ("Tasks", "Who owns which requirements."),
            ("Requests", "Cross-department requests raised and how they resolved."),
            ("Drafted statements", "What was written, typed by what kind of claim it makes."),
        ],
        [
            "EVIDENCE STATUS and BID READINESS are two different questions and are never "
            "collapsed. Evidence status asks what we hold. Readiness asks whether we can answer. "
            "A requirement can hold perfect evidence and still be BLOCKED because a signature is "
            "missing, and it can be DRAFTABLE on partial evidence.",
            "DRAFTABLE means a statement can be WRITTEN, not that the claim behind it is proven. "
            "The evidence may be unjudged -- nobody has said yet whether it proves the capability "
            "-- and unjudged counts as usable, deliberately, so that 600 excerpts do not each "
            "need a model call before anyone can start. Read PROVES CAPABILITY on the Requirement "
            "x Evidence sheet before a draft on one of these rows is sent out as a factual claim.",
            "NEEDS_CLIENT_CLARIFICATION is not a refusal to draft. Nothing in drafting reads "
            "this column: the row can be written now, from what is known. It says a MAJOR "
            "question about this clause has been found and not yet sent, so the answer must not "
            "be treated as final until the customer replies. A MINOR question never holds a row "
            "-- it stays visible in the count and in the rationale.",
            "Only the current assessment is exported. Superseded versions stay in the database; "
            "including them would multiply every requirement by its assessment history.",
            "A CUSTOMER_OBLIGATION row in Gaps would be a bug: a clause that only describes what "
            "the customer will do is not ours to evidence. A CUSTOMER-owned row that hands "
            "something over and is NOT CUSTOMER_OBLIGATION is in Gaps on purpose: the bidder "
            "has integration or configuration work in it.",
            "PROPOSED_COMMITMENT statements are promises the company has not yet made. They need "
            "a named approver before release and are not facts about the company.",
        ],
    )
    write_sheet(book, "Matrix", book_rows["matrix_headers"], book_rows["matrix"])
    write_sheet(
        book,
        "Requirement x Evidence",
        book_rows["rxe_headers"],
        book_rows["rxe"],
        note="Each row is one attachment. A requirement with no rows here had nothing attached.",
    )
    write_coverage(book, book_rows["categories"], len(book_rows["matrix"]))
    write_sheet(
        book,
        "Gaps",
        book_rows["matrix_headers"],
        book_rows["gaps"],
        note="Mandatory, ours to answer, and not READY. Includes a customer clause that hands something over (the bidder uses it); excludes clauses that only bind the customer. Sorted by category.",
    )
    write_sheet(book, "Tasks", book_rows["task_headers"], book_rows["tasks"])
    write_sheet(book, "Requests", book_rows["request_headers"], book_rows["requests"])
    write_sheet(book, "Drafted statements", book_rows["stmt_headers"], book_rows["statements"])
    return book


# Readiness values, in the order a reader wants to scan them: worst first.
READINESS = [
    "BLOCKED",
    "NEEDS_CLIENT_CLARIFICATION",
    "NEEDS_COMPANY_CONFIRMATION",
    "REQUIRES_SIGNATURE",
    "NEEDS_REFRESH",
    "DRAFTABLE",
    "READY",
    "NOT_APPLICABLE",
    "CUSTOMER_OBLIGATION",
]


def write_coverage(book: Workbook, categories: list[str], matrix_rows: int):
    """Counts by category and readiness, as formulas over the Matrix sheet.

    Formulas rather than numbers computed here, so that a bid manager who
    reclassifies a row on the Matrix sheet sees this sheet follow. A summary
    that silently disagrees with the sheet it summarises is worse than no
    summary, and hardcoding the totals guarantees it eventually will.
    """
    sheet = book.create_sheet("Coverage summary")
    last = matrix_rows + 1  # header occupies row 1 of Matrix

    sheet.cell(row=1, column=1, value="Requirements by category and readiness").font = Font(
        name="Arial", size=12, bold=True
    )
    sheet.cell(
        row=2,
        column=1,
        value="Counts read from the Matrix sheet. Edit a readiness there and these follow.",
    ).font = NOTE_FONT

    header_row = 4
    sheet.cell(row=header_row, column=1, value="Category").font = HEADER_FONT
    sheet.cell(row=header_row, column=1).fill = HEADER_FILL
    for index, readiness in enumerate(READINESS, start=2):
        cell = sheet.cell(row=header_row, column=index, value=readiness)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(wrap_text=True, vertical="center")
    total_col = len(READINESS) + 2
    cell = sheet.cell(row=header_row, column=total_col, value="Total")
    cell.font = HEADER_FONT
    cell.fill = HEADER_FILL

    # Column B on Matrix is the category; column G is bid readiness. Both are
    # fixed by MATRIX_HEADERS below -- change that order and change these.
    for r, category in enumerate(categories, start=header_row + 1):
        sheet.cell(row=r, column=1, value=category).font = BODY_FONT
        for c, readiness in enumerate(READINESS, start=2):
            letter = get_column_letter(c)
            sheet.cell(
                row=r,
                column=c,
                value=(
                    f"=COUNTIFS(Matrix!$B$2:$B${last},$A{r},"
                    f"Matrix!$G$2:$G${last},{letter}${header_row})"
                ),
            ).font = BODY_FONT
        sheet.cell(
            row=r,
            column=total_col,
            value=f"=SUM(B{r}:{get_column_letter(total_col - 1)}{r})",
        ).font = Font(name="Arial", size=10, bold=True)

    total_row = header_row + len(categories) + 1
    sheet.cell(row=total_row, column=1, value="Total").font = Font(name="Arial", size=10, bold=True)
    for c in range(2, total_col + 1):
        letter = get_column_letter(c)
        sheet.cell(
            row=total_row,
            column=c,
            value=f"=SUM({letter}{header_row + 1}:{letter}{total_row - 1})",
        ).font = Font(name="Arial", size=10, bold=True)

    sheet.column_dimensions["A"].width = 26
    for c in range(2, total_col + 1):
        sheet.column_dimensions[get_column_letter(c)].width = 16
    return sheet


def main() -> int:
    args = sys.argv[1:]
    bid_arg = None
    out = Path("exports")
    for index, arg in enumerate(args):
        if arg == "--bid" and index + 1 < len(args):
            bid_arg = args[index + 1]
        if arg == "--out" and index + 1 < len(args):
            out = Path(args[index + 1])

    from app.models import Bid, Organization

    session = get_session_factory()()
    try:
        # Every organization, not the first one.
        #
        # This took `select(Organization).first()` -- no ORDER BY -- and looked
        # for the bid inside it. With one organization that was the only answer;
        # once the fixture orgs existed beside the working one, `--bid` for a
        # fixture bid said "No bid found" about a bid that plainly existed,
        # because RLS hid it from the org that happened to come first. The bid
        # is looked for in each organization in turn, under that organization's
        # own tenant context, so RLS is still what decides visibility.
        orgs = [
            (row.id, row.name)
            for row in session.execute(select(Organization).order_by(Organization.name)).scalars()
        ]
        if not orgs:
            print("No organization. Seed the database first.")
            return 1

        # Plain values, not ORM rows: the rollback between organizations expires
        # every loaded object, and reloading one under the next tenant's context
        # would read it through the wrong RLS policy.
        chosen: tuple | None = None  # (created_at, bid_id, ctx, org_name)
        for org_id, name in orgs:
            candidate_ctx = ExecutionContext(org_id=org_id, actor_kind="SYSTEM")
            apply_tenant_context(session, candidate_ctx)
            if bid_arg:
                found = session.get(Bid, uuid.UUID(bid_arg))
            else:
                found = (
                    session.execute(select(Bid).order_by(Bid.created_at.desc()).limit(1))
                    .scalars()
                    .first()
                )
            if found is not None and (chosen is None or found.created_at > chosen[0]):
                chosen = (found.created_at, found.id, candidate_ctx, name)
            session.rollback()
            if bid_arg and chosen is not None:
                break
        if chosen is None:
            print("No bid found in any organization.")
            return 1
        _, bid_id, ctx, org_name = chosen
        # Re-enter the owning tenant: the search rolled back between orgs.
        apply_tenant_context(session, ctx)
        bid = session.get(Bid, bid_id)
        print(f"organization {org_name}")

        from _export_queries import collect

        print(f"bid {bid.id}")
        data = collect(session, ctx, bid)
        print(
            f"  requirements {len(data['requirements'])}"
            f"  evidence {len(data['evidence'])}"
            f"  attachments {len(data['rxe'])}"
        )

        one = finish(build_tender(session, ctx, bid, data), out / "01_tender_extract.xlsx")
        two = finish(build_company(session, ctx, data), out / "02_company_extract.xlsx")
        three = finish(build_matrix(data), out / "03_requirement_matrix.xlsx")
        for path in (one, two, three):
            print(f"  wrote {path.resolve()}")
        print(
            "\nOpen the third one first. Excel recalculates the Coverage summary on open;\n"
            "if it shows zeros, press Ctrl+Alt+F9."
        )
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
