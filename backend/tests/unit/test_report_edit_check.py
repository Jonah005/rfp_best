"""The check that decides whether a rewritten paragraph may replace approved wording."""

from app.schemas.report_edit import EditedParagraph
from app.services.report_edit import check_edit

SOURCES = {
    "S1": "We test 25 web applications against OWASP ASVS.",
    "S2": "We will deliver the report within 8 weeks.",
}


def _p(text, refs, kind="BODY"):
    return EditedParagraph(text=text, kind=kind, source_refs=refs)


def test_a_faithful_rewrite_passes():
    paras = [
        _p("Our testing covers 25 web applications against OWASP ASVS.", ["S1"]),
        _p("We will deliver the report within 8 weeks.", ["S2"]),
    ]
    assert check_edit(SOURCES, paras, allowed_names=set()) == []


def test_a_new_figure_is_rejected():
    paras = [
        _p("Our testing covers 40 web applications against OWASP ASVS.", ["S1"]),
        _p("We will deliver the report within 8 weeks.", ["S2"]),
    ]
    assert any("'40'" in p for p in check_edit(SOURCES, paras, allowed_names=set()))


def test_a_new_name_is_rejected():
    paras = [
        _p("Our testing, led by Karim, covers 25 web applications against OWASP ASVS.", ["S1"]),
        _p("We will deliver the report within 8 weeks.", ["S2"]),
    ]
    assert any("Karim" in p for p in check_edit(SOURCES, paras, allowed_names=set()))


def test_a_dropped_statement_is_rejected():
    paras = [_p("Our testing covers 25 web applications against OWASP ASVS.", ["S1"])]
    assert "S2 was left out" in check_edit(SOURCES, paras, allowed_names=set())


def test_a_connective_line_may_not_carry_a_figure_or_a_promise():
    paras = [
        _p("Our testing covers 25 web applications against OWASP ASVS.", ["S1"]),
        _p("We will deliver the report within 8 weeks.", ["S2"]),
        _p("This section covers 12 topics.", [], "CONNECTIVE"),
        _p("We guarantee the result.", [], "CONNECTIVE"),
    ]
    problems = check_edit(SOURCES, paras, allowed_names=set())
    assert any("carries a figure" in p for p in problems)
    assert any("makes a promise" in p for p in problems)


def test_a_citation_to_something_not_shown_is_rejected():
    paras = [
        _p("Our testing covers 25 web applications against OWASP ASVS.", ["S1", "S9"]),
        _p("We will deliver the report within 8 weeks.", ["S2"]),
    ]
    assert any("S9" in p for p in check_edit(SOURCES, paras, allowed_names=set()))


def test_a_connective_line_may_orient_the_reader_and_name_what_the_sources_name():
    sources = {"S1": "Jadwa receives 25 reports.", "S2": "We test with OWASP ASVS."}
    paras = [
        _p("This section will set out how we work with Jadwa.", [], "CONNECTIVE"),
        _p("Jadwa receives 25 reports.", ["S1"]),
        _p("We test with OWASP ASVS.", ["S2"]),
    ]
    assert check_edit(sources, paras, allowed_names=set()) == []


def test_a_connective_line_may_not_promise_on_the_bidders_behalf():
    sources = {"S1": "We test with OWASP ASVS."}
    paras = [
        _p("We will deliver everything on time.", [], "CONNECTIVE"),
        _p("We test with OWASP ASVS.", ["S1"]),
    ]
    assert any("makes a promise" in p for p in check_edit(sources, paras, allowed_names=set()))


def test_a_summary_need_not_use_every_paragraph():
    paras = [_p("Our testing covers 25 web applications against OWASP ASVS.", ["S1"])]
    assert check_edit(SOURCES, paras, allowed_names=set(), require_all=False) == []
