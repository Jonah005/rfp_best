"""The contradiction audit must describe one run, and say as active only what the report counts."""

from __future__ import annotations

import csv
import json

import pytest

from app.services import package_consistency as pc
from app.services.clarification_triage import escalate_tender_conflicts


def _f(a, b, kind="CONTRADICTION", **kw):
    return {
        "kind": kind,
        "first_source": "A",
        "first_quote": a,
        "second_source": "B",
        "second_quote": b,
        "issue": "x",
        **kw,
    }


def test_a_removed_duplicate_is_not_left_active():
    rule = _f(
        "taxes are excluded from the price", "taxes are included in the total", origin="FIXED_RULE"
    )
    model = _f("taxes are excluded from the price", "all duties are inclusive in the sum")
    audit: list = []
    pc.record(audit, rule, verdict="NOT_ADJUDICATED", active=True)
    pc.record(audit, model, verdict="CONFIRMED", active=True)
    pc.record(audit, model, verdict="DUPLICATE", active=False)
    pc.reconcile(audit, [rule])
    active = [r for r in audit if r["active"]]
    assert [r["finding_id"] for r in active] == [pc.finding_id(rule)]
    assert audit[1]["final_status"] == "SUPERSEDED"


def test_only_one_line_per_finding_is_active():
    f = _f("one sentence about payment", "another sentence about payment terms")
    audit: list = []
    pc.record(audit, f, verdict="CONFIRMED", active=True)
    pc.record(audit, f, verdict="CONFIRMED", active=True)
    pc.reconcile(audit, [f])
    assert sum(r["active"] for r in audit) == 1


def _run(package="p1"):
    f = _f("quote one is long enough", "quote two is long enough")
    audit: list = []
    pc.record(audit, f, verdict="CONFIRMED", reason="r", active=True)
    pc.record(
        audit,
        _f("rejected quote one", "rejected quote two"),
        verdict="REJECTED",
        reason="no",
        active=False,
    )
    pc.reconcile(audit, [f])
    return {"consistency_run_id": "run-1", "package_id": package, "records": audit}


def test_json_and_csv_come_from_one_run(tmp_path):
    info = pc.write_audit_files(tmp_path, "bid", _run(), "p1")
    j = json.loads((tmp_path / "contradiction-audit-bid.json").read_text())
    rows = list(csv.DictReader((tmp_path / "contradiction-audit-bid.csv").open()))
    assert len(j["records"]) == len(rows) == info["records"] == 2
    assert {r["package_id"] for r in rows} == {"p1"} and {
        r["consistency_run_id"] for r in rows
    } == {"run-1"}
    assert set(info["files"]) == {"contradiction-audit-bid.json", "contradiction-audit-bid.csv"}
    assert all(len(h) == 64 for h in info["files"].values())


def test_an_audit_from_another_package_is_refused(tmp_path):
    with pytest.raises(ValueError, match="belongs to package"):
        pc.write_audit_files(tmp_path, "bid", _run("old"), "new")


def test_no_run_is_refused(tmp_path):
    with pytest.raises(ValueError):
        pc.write_audit_files(tmp_path, "bid", {}, "p1")


DOCS = [
    ("Assumptions", "The 90-day offer validity period begins at tender opening."),
    (
        "Commercial proposal",
        "Bid validity begins at submission, while only price firmness begins at tender opening.",
    ),
    ("Technical proposal", "The offer remains binding for 90 days from tender opening."),
]


def test_offer_validity_starting_at_different_moments_is_a_contradiction():
    found = [f for f in pc.rule_findings(DOCS, []) if "validity" in f["issue"]]
    assert len(found) == 1 and found[0]["kind"] == "CONTRADICTION"


def test_price_firmness_alone_is_not_offer_validity():
    docs = [
        DOCS[0],
        ("Commercial proposal", "Prices remain firm for 90 days from tender opening."),
        DOCS[2],
    ]
    assert not [f for f in pc.rule_findings(docs, []) if "validity" in f["issue"]]


def test_the_matrix_is_not_a_bidder_statement_for_validity():
    docs = [
        DOCS[0],
        ("Requirements compliance matrix", "Offer valid 90 days from submission of the bid."),
        DOCS[2],
    ]
    assert not [f for f in pc.rule_findings(docs, []) if "validity" in f["issue"]]


def test_a_question_saying_the_tender_disagrees_with_itself_is_blocking_and_goes_to_the_customer():
    q = [
        {
            "question": "In clause 8.22, should the 90-day offer validity run from tender opening "
            "or from bid submission, as stated elsewhere in the RFP?",
            "severity": "MINOR",
            "category": "SOLUTION_DESIGN",
            "verdict": "BIDDER_DECISION",
        }
    ]
    out = escalate_tender_conflicts(q)[0]
    assert (out["severity"], out["verdict"], out["category"]) == (
        "MAJOR",
        "KEEP",
        "LEGAL_CONTRACTUAL",
    )


def test_an_ordinary_minor_question_is_left_alone():
    q = [
        {
            "question": "What format do you want the CVs in?",
            "severity": "MINOR",
            "category": "STAFFING",
            "verdict": "KEEP",
        }
    ]
    assert escalate_tender_conflicts(q)[0]["severity"] == "MINOR"


def test_validity_in_one_sentence_with_price_firmness_is_still_read():
    docs = [
        ("Assumptions", "The 90-day offer validity period runs from the date of tender opening."),
        (
            "Commercial Proposal",
            "Our bid will remain valid for 90 days from the date of submission, and our prices "
            "will be firm for 90 days from tender opening.",
        ),
    ]
    found = [f for f in pc.rule_findings(docs, []) if "validity" in f["issue"]]
    assert len(found) == 1


def test_a_finding_rejected_this_run_is_not_carried_back_in():
    f = _f(
        "Our bid will remain valid from submission here",
        "The validity period runs from tender opening here",
    )
    audit: list = []
    pc.record(audit, f, verdict="REJECTED", reason="different subject", active=False)
    rejected_now = {r["finding_id"] for r in audit if r["verdict"] == "REJECTED"}
    assert pc.finding_id(f) in rejected_now


CERTIFICATE_SENTENCE = (
    "We will submit our current professional and government certifications, together with "
    "their validity dates, as part of our bid submission so that ADX can verify our standing."
)


def test_certificate_validity_is_never_an_offer_validity_anchor():
    assert not pc.offer_validity_clause(CERTIFICATE_SENTENCE)
    for text in (
        "The licence is valid for 12 months from the date of bid submission.",
        "Our registration validity runs from the date of submission.",
        "The insurance policy expires 90 days after tender opening.",
        "Document validity dates are shown in the bid submission annex.",
    ):
        assert not pc.offer_validity_clause(text), text


def test_a_certificate_sentence_cannot_become_the_quoted_evidence_of_a_validity_finding():
    docs = [
        ("Technical proposal", CERTIFICATE_SENTENCE),
        ("Assumptions", "The 90-day offer validity period runs from the date of tender opening."),
        (
            "Commercial proposal",
            "Our bid will remain valid for 90 days from the date of submission.",
        ),
    ]
    found = [f for f in pc.rule_findings(docs, []) if "validity" in f["issue"]]
    assert len(found) == 1
    quoted = f"{found[0]['first_quote']} {found[0]['second_quote']}"
    assert "certifications" not in quoted
    assert "offer validity period" in quoted and "bid will remain valid" in quoted


def test_real_offer_validity_statements_are_still_recognised():
    for text in (
        "The 90-day offer validity period begins at tender opening.",
        "Bid validity begins at submission.",
        "This proposal remains valid for 90 days, as the tender requires.",
        "The offer remains binding for 90 days from tender opening.",
        "The tender requires the offer to be open for acceptance for 120 days.",
    ):
        assert pc.offer_validity_clause(text), text


def test_every_audit_row_has_its_own_event_id_and_names_the_row_it_replaces(tmp_path):
    import json

    run = {
        "consistency_run_id": "run-1",
        "package_id": "pkg-1",
        "records": [
            {"finding_id": "F-1", "active": False, "final_status": "PROPOSED"},
            {"finding_id": "F-2", "active": True, "final_status": "ACTIVE"},
            {"finding_id": "F-1", "active": True, "final_status": "ACTIVE"},
        ],
    }
    info = pc.write_audit_files(tmp_path, "bid", run, "pkg-1")
    rows = json.loads((tmp_path / "contradiction-audit-bid.json").read_text())["records"]
    ids = [r["audit_event_id"] for r in rows]
    assert len(set(ids)) == 3
    assert rows[0]["supersedes_event_id"] == ""
    assert rows[2]["supersedes_event_id"] == ids[0], "the later F-1 row replaces the earlier one"
    assert info["active_findings"] == ["F-1", "F-2"]


def test_the_validation_report_is_written_from_the_registers_own_counts():
    from types import SimpleNamespace

    from app.services import clarification_register as cr

    def finding(code, waivability):
        return SimpleNamespace(check_code=code, waivability=SimpleNamespace(value=waivability))

    findings = [
        finding("TENDER_INCOMPLETE", "NON_WAIVABLE"),
        finding("TERMS_DEVIATION", "WAIVABLE"),
        finding("TERMS_DEVIATION", "WAIVABLE"),
    ]
    data = {
        "package_id": "pkg-9",
        "validation_counts": cr._validation_counts(findings),
        "counts": {"blocking": 45, "optional": 39, "answered": 9},
    }
    text = cr.render_validation_report(
        data, {"consistency_run_id": "run-9", "records": 20, "active_findings": ["F-1"]}
    )
    assert "Non-waivable (1): TENDER_INCOMPLETE 1." in text
    assert "Waivable (2): TERMS_DEVIATION 2." in text
    assert "blocking 45, optional 39" in text and "answered in tender 9" in text
    assert "pkg-9" in text and "run-9" in text


TENDER_OPEN = "The offer remains binding for 90 days from the date of tender opening."
AT_SUBMISSION = "Our bid will remain valid for 90 days from the date of submission."
ASSUMED_OPEN = "The 90-day offer validity period runs from the date of tender opening."


def test_a_rule_finding_keeps_its_identity_when_another_sentence_is_quoted():
    docs_a = [
        ("Technical proposal", TENDER_OPEN),
        ("Commercial proposal", AT_SUBMISSION),
    ]
    docs_b = [
        ("Assumptions", ASSUMED_OPEN),
        ("Commercial proposal", AT_SUBMISSION),
    ]

    def validity(docs):
        found = [f for f in pc.rule_findings(docs, []) if "validity" in f["issue"]]
        assert len(found) == 1
        return found[0]

    a, b = validity(docs_a), validity(docs_b)
    assert a["first_quote"] != b["first_quote"], "the two builds quote different sentences"
    assert pc.finding_id(a) == pc.finding_id(b), "but it is one matter with one identity"


def test_documents_are_read_in_one_order_whatever_order_they_arrive_in():
    docs = [
        ("Technical proposal", TENDER_OPEN),
        ("Assumptions", ASSUMED_OPEN),
        ("Commercial proposal", AT_SUBMISSION),
    ]
    forward = pc.rule_findings(docs, [])
    backward = pc.rule_findings(list(reversed(docs)), [])
    assert [f["first_quote"] for f in forward] == [f["first_quote"] for f in backward]
