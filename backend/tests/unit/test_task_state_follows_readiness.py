"""A task must not still say "ready to draft" after its requirements block.

`seed_initial_states` runs after every assessment, and considered only tasks
still in NOT_STARTED. That made it a one-time seeding despite its schedule: once
a task had any state at all, no later assessment could revise it.

It went unnoticed while readiness only ever improved. Then the evidence rule
tightened -- material the model called "related, not proof" stopped counting --
and 38 requirements moved to BLOCKED in a single run. PMO-01 and QUA-01 kept
reading AI_READY_FOR_REVIEW, whose stored reason is "every covered requirement
has usable evidence; a draft can be proposed", while every requirement behind
them said the opposite. The matrix and the task board disagreed about the same
work, and the board is the sheet a contributor actually opens.

The rule now revisits every state the system assigned, and no state a person
did. That boundary is the whole design: a contributor's IN_PROGRESS is a fact
about what they are doing, not a conclusion to recompute from evidence.
"""

from __future__ import annotations

from app.models.enums import TaskState
from app.services.tasks import ALLOWED_TRANSITIONS, SYSTEM_ASSIGNED_STATES


def test_every_state_the_system_assigns_is_revisitable():
    """The four the rule itself can produce, and only those."""
    assert SYSTEM_ASSIGNED_STATES == {
        TaskState.NOT_STARTED,
        TaskState.AI_READY_FOR_REVIEW,
        TaskState.NEEDS_INPUT,
        TaskState.BLOCKED,
    }


def test_a_task_a_person_has_taken_is_never_recomputed():
    """Re-deriving these from evidence would overwrite somebody's work with an
    opinion. A task in progress, awaiting approval, approved or completed stays
    exactly where its owner put it, whatever the assessment now says."""
    for state in (
        TaskState.IN_PROGRESS,
        TaskState.WAITING_FOR_DEPENDENCY,
        TaskState.WAITING_FOR_ACCESS,
        TaskState.READY_FOR_APPROVAL,
        TaskState.REWORK_REQUIRED,
        TaskState.APPROVED,
        TaskState.COMPLETED,
        TaskState.REVALIDATION_REQUIRED,
    ):
        assert state not in SYSTEM_ASSIGNED_STATES, state


def test_readiness_can_move_a_task_back_from_ready():
    """The transition this bug needed and the machine refused.

    A task cannot be corrected into a state the guard rejects, so widening the
    rule without widening the machine would have swapped a stale value for a
    raised exception in the middle of an assessment.
    """
    assert TaskState.BLOCKED in ALLOWED_TRANSITIONS[TaskState.AI_READY_FOR_REVIEW]
    assert TaskState.NEEDS_INPUT in ALLOWED_TRANSITIONS[TaskState.AI_READY_FOR_REVIEW]


def test_readiness_can_also_move_a_task_forward_again():
    """Both directions, because evidence arrives as well as evaporating. A
    blocked task whose evidence is later uploaded must be able to become
    reviewable without a person nudging it by hand."""
    assert TaskState.AI_READY_FOR_REVIEW in ALLOWED_TRANSITIONS[TaskState.BLOCKED]
    assert TaskState.AI_READY_FOR_REVIEW in ALLOWED_TRANSITIONS[TaskState.NEEDS_INPUT]
    assert TaskState.NEEDS_INPUT in ALLOWED_TRANSITIONS[TaskState.BLOCKED]


def test_the_derivation_reads_every_covered_requirement():
    """One blocked requirement blocks the task, whatever the others say.

    Stated against the source because the rule is a three-branch conditional
    over a list, and the failure being guarded is someone rewriting it as "most
    requirements are fine" -- which is how a task comes to invite a draft for a
    requirement nothing supports.
    """
    import inspect

    from app.services import tasks

    source = inspect.getsource(tasks.seed_initial_states)
    assert "if BidReadiness.BLOCKED in readiness:" in source
    blocked_at = source.index("if BidReadiness.BLOCKED in readiness:")
    ready_at = source.index("elif all(")
    assert blocked_at < ready_at, "a single blocker must be checked before 'all are ready'"
    assert "all(r in (BidReadiness.READY, BidReadiness.DRAFTABLE) for r in readiness)" in source
