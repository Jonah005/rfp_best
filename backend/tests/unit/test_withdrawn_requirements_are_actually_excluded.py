"""A withdrawn requirement must disappear from everything a person sees.

`Requirement.lifecycle_state` carried a WITHDRAWN value that NOTHING read. Not
the export, not the assessment, not readiness, not task generation, not the API.
A cleanup script could set it on six duplicate rows, print "withdrew 6", and
change nothing at all: the workbook still listed them, the model still spent
calls judging them, readiness still counted them toward coverage, and tasks were
still raised against them.

That is the defect this codebase keeps producing -- a column that looks
authoritative and decides nothing -- and it made a duplicate-merge script into a
no-op dressed as a fix. Reported as "50 rows become 44"; the actual answer was
50 rows become 50.

The filter now lives in one place, `requirement_scope.LIVE_REQUIREMENT`, and
this test names the functions that must apply it. A source check is a weak kind
of test, and it is the right one here: the alternative is a live database with
withdrawn rows in it, and the failure is silent everywhere else.

Deliberately NOT in this list, and each for a reason:

    requirements._find_existing   already excludes withdrawn rows separately,
                                  because a withdrawn duplicate must not be
                                  matched against on a later run
    amendments                    history has to see what was withdrawn
    audit                         hiding withdrawn rows would defeat its point
"""

from __future__ import annotations

import inspect

import pytest


def _source(dotted: str) -> str:
    module_name, _, attribute = dotted.rpartition(".")
    if module_name.endswith("_export_queries"):
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    module = __import__(module_name, fromlist=[attribute])
    return inspect.getsource(getattr(module, attribute))


# Every one of these decides what a person sees, or what the pipeline spends
# model calls on.
MUST_FILTER = [
    "app.services.assessment.assess_bid",
    "app.services.readiness.summarize",
    "app.services.tasks.generate_tasks",
]


@pytest.mark.parametrize("dotted", MUST_FILTER)
def test_the_query_excludes_withdrawn_requirements(dotted: str):
    source = _source(dotted)
    assert "LIVE_REQUIREMENT" in source, (
        f"{dotted} selects requirements without excluding withdrawn ones. A "
        "duplicate retired by scripts/merge_duplicate_requirements.py would "
        "still be assessed, counted and shown."
    )


def test_the_filter_is_one_shared_expression():
    """Not `lifecycle_state != WITHDRAWN` written out at each call site.

    Eighteen queries select requirements. Spelling the condition by hand at each
    one is how the nineteenth omits it, and how two of them end up disagreeing
    about what "live" means.
    """
    from app.models.enums import RequirementLifecycle
    from app.services.requirement_scope import LIVE_REQUIREMENT

    compiled = str(LIVE_REQUIREMENT.compile(compile_kwargs={"literal_binds": True}))
    assert "lifecycle_state" in compiled
    assert RequirementLifecycle.WITHDRAWN.value in compiled
    assert "!=" in compiled or "IS NOT" in compiled


def test_the_export_excludes_withdrawn_requirements():
    """The workbook is the artefact that gets mailed to someone who was not in
    the conversation, so a withdrawn row appearing there is the most expensive
    version of this bug."""
    import sys
    from pathlib import Path

    scripts = Path(__file__).resolve().parents[2] / "scripts"
    sys.path.insert(0, str(scripts))
    source = (scripts / "_export_queries.py").read_text(encoding="utf-8")
    marker = "select(Requirement, RequirementVersion)"
    assert marker in source
    query = source[source.index(marker) : source.index(marker) + 700]
    assert "LIVE_REQUIREMENT" in query, (
        "the requirements sheet still lists withdrawn rows"
    )
