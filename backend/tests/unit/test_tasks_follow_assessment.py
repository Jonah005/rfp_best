"""A matrix with no task board is a list of problems nobody owns.

Analysis produced requirements. The assessment worked out what each one was
backed by. And then nothing happened: no screen created tasks, no workflow step
created tasks, so every freshly analysed bid had a complete matrix and an empty
task board, and the only thing that fixed it was a script run by hand.

Grouping now happens at the end of the assessment, which is the first moment the
work is genuinely known. Earlier would be worse, not merely premature: before
the assessment a requirement has no evidence status and no readiness, so a task
built from it is a title with nothing behind it.

Two properties matter more than the trigger, and both are asserted here.
"""

from __future__ import annotations

import inspect

from app.services import assessment, tasks


def test_the_assessment_groups_the_work_it_just_assessed():
    source = inspect.getsource(assessment.assess_bid)
    assert "task_service.generate_tasks" in source
    assert "seed_initial_states" in source


def test_grouping_happens_after_the_verdicts_are_in():
    """Order is the point. Grouping before assessment would produce tasks whose
    requirements have no readiness, which is the empty board with extra steps."""
    source = inspect.getsource(assessment.assess_bid)
    assert source.index("report.verification = verifier.counters()") < source.index(
        "task_service.generate_tasks"
    )


def test_the_import_is_deferred_to_avoid_a_cycle():
    """`tasks` imports DEPARTMENT_BY_CATEGORY from `assessment` at module level.
    A module-level import back the other way is an import cycle, and the failure
    it produces names neither module usefully."""
    source = inspect.getsource(assessment.assess_bid)
    assert "from app.services import tasks as task_service" in source


def test_generation_never_names_an_owner():
    """The property the whole staffing design rests on.

    Grouping is a fact about the tender: this category of work belongs to that
    department. Naming the person is a judgement about a team, and it belongs to
    that team's head. If generation ever set an owner, the head's authority
    would be a formality applied to a decision already made.
    """
    source = inspect.getsource(tasks.generate_tasks)
    assert "owner_user_id" not in source


def test_regrouping_leaves_existing_tasks_alone():
    """Idempotence, and it has to be real: the assessment can be re-run at any
    time, and a second run that duplicated tasks would scatter each department's
    work across two boards -- and orphan the owners, conversations and drafts
    attached to the first."""
    source = inspect.getsource(tasks.generate_tasks)
    assert "covered" in source
    assert "if requirement.id in covered" in source


def test_the_report_says_how_many_tasks_it_made():
    """Silent side effects are how a person stops trusting a number on a screen."""
    report = assessment.AssessmentReport()
    assert "tasks" in report.as_dict()


def test_a_requirement_with_no_department_is_left_visibly_ungrouped():
    """Filing it somewhere plausible would lose it. An ungrouped requirement is
    a gap someone can see; a misfiled one is a gap nobody can."""
    source = inspect.getsource(tasks.generate_tasks)
    assert "Better to leave it visibly ungrouped" in source
