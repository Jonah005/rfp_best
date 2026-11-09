from app.schemas.report_consolidate import (
    AssumptionRegister,
    ChapterAssumption,
    ChapterParagraph,
    ConsolidatedChapter,
    RegisterItem,
)
from app.services.report_consolidate import budget_for, check_chapter, check_register

SOURCES = {
    "P1": "We will run 300 concurrent sessions in JMeter before go-live.",
    "P2": "The test runs 300 concurrent sessions, using JMeter, from three load generators.",
    "P3": "We assume Jadwa provides the SMS sandbox by month 1.",
}


def chapter(paragraphs, assumptions=()):
    return ConsolidatedChapter(paragraphs=paragraphs, assumptions=list(assumptions))


def body(text, refs):
    return ChapterParagraph(kind="BODY", text=text, source_refs=refs)


def good():
    return chapter(
        [
            ChapterParagraph(kind="SUBHEADING", text="Load testing"),
            body(
                "Before go-live we run 300 concurrent sessions in JMeter from three load "
                "generators.",
                ["P1", "P2"],
            ),
        ],
        [ChapterAssumption(text="Jadwa provides the SMS sandbox by month 1.", source_refs=["P3"])],
    )


def test_a_faithful_chapter_passes():
    assert check_chapter(SOURCES, good(), budget=60, title="Testing") == []


def test_an_invented_figure_or_name_is_refused():
    bad = chapter(
        [body("We run 500 concurrent sessions in LoadRunner.", ["P1", "P2"])],
        [ChapterAssumption(text="Jadwa provides the SMS sandbox by month 1.", source_refs=["P3"])],
    )
    problems = " ".join(check_chapter(SOURCES, bad, budget=60, title="Testing"))
    assert "'500'" in problems and "LoadRunner" in problems


def test_a_dropped_figure_or_a_missing_citation_is_refused():
    dropped = chapter(
        [body("We run load tests before go-live.", ["P1", "P2"])],
        [ChapterAssumption(text="Jadwa provides the SMS sandbox by month 1.", source_refs=["P3"])],
    )
    assert any("missing" in p for p in check_chapter(SOURCES, dropped, budget=60, title="Testing"))
    uncited = chapter([body("We run 300 concurrent sessions.", [])])
    assert any(
        "cites no source" in p for p in check_chapter(SOURCES, uncited, budget=60, title="T")
    )


def test_a_chapter_over_budget_is_refused():
    long = chapter([body("We run the tests. " * 400, ["P1"])])
    assert any("budget" in p for p in check_chapter(SOURCES, long, budget=5, title="Testing"))


def test_budget_keeps_short_chapters_whole_and_shrinks_long_ones():
    assert budget_for(100) == 100
    assert budget_for(3000) == 1350


def test_the_register_may_merge_but_not_invent_or_drop():
    sources = {
        "A1": "Jadwa provides the SMS sandbox by month 1.",
        "A2": "The sandbox is ready by month 1.",
    }
    ok = AssumptionRegister(
        items=[
            RegisterItem(
                text="Jadwa provides the SMS sandbox by month 1.", source_refs=["A1", "A2"]
            )
        ]
    )
    assert check_register(sources, ok) == []
    invented = AssumptionRegister(
        items=[RegisterItem(text="Jadwa provides the sandbox by month 2.", source_refs=["A1"])]
    )
    problems = " ".join(check_register(sources, invented))
    assert "'2'" in problems and "A2 was left out" in problems
