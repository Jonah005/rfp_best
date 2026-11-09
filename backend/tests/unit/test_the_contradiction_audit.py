"""Every proposed contradiction leaves a line in the audit, whether it stays or goes."""

from __future__ import annotations

from types import SimpleNamespace

from app.schemas.drafting import AdjudicatedFinding, AdjudicationResult
from app.services import package_consistency as pc
from app.services.storage import InMemoryStorage

TAX_A = "Our grand total is stated exclusive of any taxes or charges ADX is required by law to add."
TAX_B = (
    "The Total fee is to be inclusive of all taxes, duties, charges and levies "
    "payable under local law."
)
CAP = "Liability is limited to one hundred percent of the fees paid under this agreement."
CLAUSE = (
    "The Supplier shall be liable for appropriate damages arising from a breach of this agreement."
)


def _finding(
    kind="CONTRADICTION",
    a=TAX_A,
    b=TAX_B,
    issue="Taxes excluded and included.",
    origin="MODEL_PROPOSED",
):
    return {
        "kind": kind,
        "first_source": "Assumptions",
        "first_quote": a,
        "second_source": "Commercial Proposal" if kind == "CONTRADICTION" else "Tender clause 4",
        "second_quote": b,
        "issue": issue,
        "origin": origin,
    }


class _Reviewer:
    def __init__(self, verdicts=None, fail=False):
        self.verdicts, self.fail, self.calls = verdicts or [], fail, 0

    def generate_structured(self, name, text, ctx=None, session=None):
        self.calls += 1
        assert name == "consistency_adjudication"
        if self.fail:
            raise RuntimeError("provider down")
        return AdjudicationResult(
            verdicts=[AdjudicatedFinding(id=i, verdict=v, reason=r) for i, v, r in self.verdicts]
        )


def _run(found, reviewer, audit):
    docs = [("Assumptions", TAX_A + " " + CAP), ("Commercial Proposal", TAX_B)]
    clauses = [("clause 4", CLAUSE)]
    return pc.adjudicate(reviewer, None, None, found, docs, clauses, audit=audit)


def test_a_rejected_finding_is_dropped_but_its_reason_is_kept():
    audit: list = []
    found = [
        _finding(),
        _finding("TERMS_DEVIATION", CAP, CLAUSE, "Liability capped.", "FIXED_RULE"),
    ]
    kept = _run(
        found,
        _Reviewer(
            [(0, "CONFIRMED", "same fee, cannot both be true"), (1, "REJECTED", "different things")]
        ),
        audit,
    )
    assert [f["kind"] for f in kept] == ["CONTRADICTION"]
    by_id = {r["finding_id"]: r for r in audit}
    rejected = by_id[pc.finding_id(found[1])]
    assert rejected["verdict"] == "REJECTED" and rejected["active"] is False
    assert rejected["reason"] == "different things"
    assert rejected["origin"] == "FIXED_RULE"
    assert rejected["first_quote"] == CAP and rejected["second_quote"] == CLAUSE
    confirmed = by_id[pc.finding_id(found[0])]
    assert confirmed["verdict"] == "CONFIRMED" and confirmed["active"] is True
    for record in audit:
        assert record["decided_at"]
        assert "adjudicator_prompt_version" in record


def test_an_ambiguous_deviation_is_kept_and_marked_possible():
    audit: list = []
    found = [
        _finding("TERMS_DEVIATION", CAP, CLAUSE, "The bidder caps its liability.", "FIXED_RULE")
    ]
    kept = _run(
        found, _Reviewer([(0, "AMBIGUOUS", "the clause only says appropriate damages")]), audit
    )
    assert len(kept) == 1
    assert kept[0]["ambiguous"] is True
    assert kept[0]["issue"].startswith("Possible deviation")
    assert audit[0]["verdict"] == "AMBIGUOUS" and audit[0]["active"] is True


def test_when_the_second_reviewer_cannot_run_every_finding_stands_and_says_so():
    audit: list = []
    kept = _run([_finding()], _Reviewer(fail=True), audit)
    assert len(kept) == 1 and kept[0]["unadjudicated"] is True
    assert audit[0]["verdict"] == "UNADJUDICATED" and audit[0]["active"] is True


def test_a_missing_verdict_is_not_a_rejection():
    audit: list = []
    kept = _run(
        [_finding(), _finding(a=CAP, b=CLAUSE, issue="Liability.")],
        _Reviewer([(0, "CONFIRMED", "ok")]),
        audit,
    )
    assert len(kept) == 2
    assert {r["verdict"] for r in audit} == {"CONFIRMED", "UNADJUDICATED"}


def test_a_proposal_the_quote_check_turns_away_is_recorded():
    audit: list = []
    proposed = [
        SimpleNamespace(
            kind="CONTRADICTION",
            issue="Taxes excluded and included.",
            first_quote="All taxes are payable by a third party, as nowhere stated.",
            second_quote=TAX_B,
        )
    ]
    kept = pc.verify(proposed, [("Commercial Proposal", TAX_B)], [], audit=audit)
    assert kept == []
    assert audit[0]["verdict"] == "FAILED_CHECK" and audit[0]["active"] is False
    assert "word for word" in audit[0]["reason"]


def test_a_repeat_with_a_table_row_prefix_is_a_recorded_duplicate():
    audit: list = []
    first = _finding()
    again = _finding(a="AS-25 | Commercial | " + TAX_A, b=TAX_B)
    kept = pc._drop_repeats([first, again], audit=audit)
    assert len(kept) == 1
    assert audit[-1]["verdict"] == "DUPLICATE" and audit[-1]["active"] is False


def test_the_finding_id_ignores_row_prefixes_and_order():
    plain = _finding()
    prefixed = _finding(a="AS-25 | Commercial | " + TAX_A)
    swapped = _finding(a=TAX_B, b=TAX_A)
    assert pc.finding_id(plain) == pc.finding_id(prefixed) == pc.finding_id(swapped)
    assert pc.finding_id(plain) != pc.finding_id(_finding(a=CAP, b=CLAUSE))


def test_the_audit_is_stored_per_bid_and_keeps_the_last_ten_runs():
    storage = InMemoryStorage()
    ctx = SimpleNamespace(org_id="org-1")
    for n in range(12):
        audit: list = []
        pc.record(audit, _finding(), verdict="CONFIRMED", reason=f"run {n}", active=True)
        pc.save_audit(storage, ctx, "bid-1", f"package-{n}", audit)
    runs = pc.load_audit(storage, ctx, "bid-1")
    assert len(runs) == 10
    assert runs[-1]["package_id"] == "package-11"
    assert runs[-1]["records"][0]["reason"] == "run 11"
    assert pc.load_audit(storage, ctx, "bid-2") == []


def test_one_sentence_against_two_wordings_of_the_same_matter_is_one_finding():
    audit: list = []
    rule = _finding(origin="FIXED_RULE")
    model = _finding(
        a=TAX_A,
        b="The Total fee we submit will be inclusive of all applicable taxes, duties, charges and "
        "levies.",
        issue="The commercial proposal says taxes are included while the assumptions say taxes "
        "are excluded.",
    )
    model["second_source"] = "Commercial Proposal (another wording)"
    kept = pc._drop_repeats([rule, model], audit=audit)
    assert len(kept) == 1 and kept[0]["origin"] == "FIXED_RULE"
    assert audit[-1]["verdict"] == "DUPLICATE"


def test_two_different_matters_sharing_a_sentence_stay_two_findings():
    first = _finding()
    other = _finding(
        "TERMS_DEVIATION", TAX_A, CLAUSE, "The assumptions say something about damages."
    )
    assert len(pc._drop_repeats([first, other])) == 2


def test_a_matrix_quoting_the_tender_is_not_a_bidder_statement():
    matrix = (
        "Requirements compliance matrix",
        "R-153 | The Total fee is inclusive of all taxes, duties, charges and levies.",
    )
    assumptions = ("Assumptions", "AS-25 | Commercial | " + TAX_A)
    assert [
        f for f in pc.rule_findings([assumptions, matrix], []) if f["kind"] == "CONTRADICTION"
    ] == []
    proposal = ("Commercial Proposal", TAX_B)
    found = [
        f
        for f in pc.rule_findings([assumptions, matrix, proposal], [])
        if f["kind"] == "CONTRADICTION"
    ]
    assert len(found) == 1 and found[0]["second_source"] == "Commercial Proposal"
