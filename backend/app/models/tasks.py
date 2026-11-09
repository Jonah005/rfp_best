"""Tasks, conversations and cross-department requests.

Three design rules are visible in the schema here.

**Task isolation.** A task carries its own `retrieval_scope`. Two contributors
working the same bid see different context because their tasks say so, not
because the application remembers to filter. The scope is stored, but it is
re-evaluated against live permissions on every message rather than trusted from
cache — see `app/services/context_builder.py`.

**One task, many conversations.** A conversation belongs to a `(task, user)`
pair, so a contributor's working dialogue is private to them. A shared task
thread would leak one person's exploration into another's context, which is the
same leak as a shared retrieval scope wearing a different hat.

**A request is not a grant.** `cross_department_request` records what was asked
and what was decided. Sharing evidence or granting access creates `access_grant`
rows; answering creates none. The request never carries authority itself.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ENUM as PGEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin, tenant_fk, uuid_pk
from app.models.enums import (
    ChatRole,
    ParticipantRole,
    RequestState,
    RequestType,
    TaskDependencyType,
    TaskPriority,
    TaskState,
)


def _pg_enum(python_enum, name: str) -> PGEnum:
    return PGEnum(
        python_enum,
        name=name,
        values_callable=lambda e: [m.value for m in e],
        create_type=True,
    )


class Task(Base, TenantMixin, TimestampMixin):
    """A unit of contributor work, with its own permission-bounded context."""

    __tablename__ = "task"

    id: Mapped[uuid.UUID] = uuid_pk()
    bid_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    department_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    # The single accountable owner. Contributors, reviewers, approvers and
    # observers live in task_participant; conflating them loses accountability.
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    priority: Mapped[TaskPriority] = mapped_column(
        _pg_enum(TaskPriority, "task_priority"), nullable=False
    )
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    state: Mapped[TaskState] = mapped_column(_pg_enum(TaskState, "task_state"), nullable=False)
    # {departments[], categories[], entity_types[], max_confidentiality,
    #  extra_resource_ids[]} -- what this task may ever see, before live
    #  permission evaluation narrows it further.
    retrieval_scope: Mapped[dict] = mapped_column(JSONB, nullable=False)
    blocker_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    grouping_rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    state_changed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    state_changed_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    # When the AI last decided which gaps in the tender concern this task. NULL
    # means never; the first time the task is opened it is asked, then cached.
    gaps_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # The version of the check that produced the saved answer. When the check improves, a
    # task checked with an older one is checked again the next time it is opened.
    gaps_check_version: Mapped[str | None] = mapped_column(String(16), nullable=True)

    __table_args__ = (
        UniqueConstraint("id", "org_id", name="uq_task_id_org"),
        UniqueConstraint("bid_id", "code", name="uq_task_bid_code"),
        tenant_fk("bid_id", "bid", ondelete="CASCADE"),
        tenant_fk("department_id", "department", name="fk_task_department_org"),
        tenant_fk("owner_user_id", "app_user", name="fk_task_owner_org"),
        Index("ix_task_owner", "org_id", "owner_user_id", "state"),
        Index("ix_task_department", "org_id", "bid_id", "department_id", "state"),
    )


class TaskEvidence(Base, TenantMixin, TimestampMixin):
    """A company document a contributor uploaded from inside a task.

    The upload itself is ordinary company knowledge -- it is parsed and
    extracted like any other document and its excerpts wait for a knowledge
    administrator. This row records WHERE it came from, so that once approved
    those excerpts are offered to this task's draft first, and so the task can
    say "waiting on approval of the methodology you uploaded" rather than going
    quiet. Keyed on the document version because extraction runs after upload:
    the evidence rows do not exist yet when the contributor presses Upload.
    """

    __tablename__ = "task_evidence"

    id: Mapped[uuid.UUID] = uuid_pk()
    task_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    document_version_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    added_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("id", "org_id", name="uq_task_evidence_id_org"),
        UniqueConstraint("task_id", "document_version_id", name="uq_task_evidence"),
        tenant_fk("task_id", "task", ondelete="CASCADE"),
        tenant_fk("document_version_id", "document_version", ondelete="CASCADE"),
        Index("ix_task_evidence_task", "org_id", "task_id"),
    )


class TaskRequirement(Base, TenantMixin, TimestampMixin):
    """Which requirements a task covers. The join carries the tenant too."""

    __tablename__ = "task_requirement"

    id: Mapped[uuid.UUID] = uuid_pk()
    task_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    requirement_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    coverage_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("id", "org_id", name="uq_task_requirement_id_org"),
        UniqueConstraint("task_id", "requirement_id", name="uq_task_requirement"),
        tenant_fk("task_id", "task", ondelete="CASCADE"),
        tenant_fk("requirement_id", "requirement", ondelete="CASCADE"),
        Index("ix_task_requirement_reverse", "org_id", "requirement_id"),
    )


class TaskGap(Base, TenantMixin, TimestampMixin):
    """A gap in the tender (a clarification issue) that concerns a task.

    `source` is who decided: "model" or "person". `attached` False is a recorded
    refusal -- a person removed it, so the AI does not put it back.
    """

    __tablename__ = "task_gap"

    id: Mapped[uuid.UUID] = uuid_pk()
    task_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    clarification_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    attached: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    # In the company's formal voice: what the RFP leaves out, and why this task
    # needs it filled.
    missing: Mapped[str | None] = mapped_column(Text, nullable=True)
    why_needed: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Which piece of its requirement this gap is about, in a few words, as the AI read it
    # from the requirement's own wording ("final agreement generation").
    part: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # When the AI judged this gap to say the same thing as another one on the task, the
    # other one's id. The gap stays; it is shown folded under the other. `keep_separate`
    # is a person saying they are different: the AI does not fold it again.
    duplicate_of: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    keep_separate: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    __table_args__ = (
        UniqueConstraint("id", "org_id", name="uq_task_gap_id_org"),
        UniqueConstraint("task_id", "clarification_id", name="uq_task_gap"),
        tenant_fk("task_id", "task", ondelete="CASCADE"),
        tenant_fk("clarification_id", "clarification_issue", ondelete="CASCADE"),
        Index("ix_task_gap_task", "org_id", "task_id"),
    )


class RequirementPosition(Base, TenantMixin, TimestampMixin):
    """What the company decided to do about a requirement it holds no evidence for.

    One row per (task, requirement). `status`: "proposed" (the AI suggested wording;
    nothing decided), "submitted" (a person approved this wording as the company's
    answer), "ignored" (a person set the requirement aside) or "restored" (a person
    undid either). The AI's suggestions stay on the row, so a decision can always be
    traced to what was proposed and who chose.
    """

    __tablename__ = "requirement_position"

    id: Mapped[uuid.UUID] = uuid_pk()
    task_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    requirement_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False)
    # Requirements decided together share a group.
    group_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    # "model" when the AI's wording went in as drafted, "person" once changed or written.
    source: Mapped[str | None] = mapped_column(String(16), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # What the AI proposed, as it proposed it, and the prompt that produced it.
    proposed: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    proposed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    chosen_option: Mapped[int | None] = mapped_column(Integer, nullable=True)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("id", "org_id", name="uq_requirement_position_id_org"),
        UniqueConstraint("task_id", "requirement_id", name="uq_requirement_position"),
        tenant_fk("task_id", "task", ondelete="CASCADE"),
        tenant_fk("requirement_id", "requirement", ondelete="CASCADE"),
        tenant_fk("decided_by", "app_user", name="fk_requirement_position_decider_org"),
        Index("ix_requirement_position_task", "org_id", "task_id", "status"),
    )


class TaskDependency(Base, TenantMixin, TimestampMixin):
    __tablename__ = "task_dependency"

    id: Mapped[uuid.UUID] = uuid_pk()
    from_task_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    to_task_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    dependency_type: Mapped[TaskDependencyType] = mapped_column(
        _pg_enum(TaskDependencyType, "task_dependency_type"), nullable=False
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("id", "org_id", name="uq_task_dependency_id_org"),
        UniqueConstraint(
            "from_task_id", "to_task_id", "dependency_type", name="uq_task_dependency"
        ),
        tenant_fk("from_task_id", "task", ondelete="CASCADE", name="fk_task_dep_from_org"),
        tenant_fk("to_task_id", "task", ondelete="CASCADE", name="fk_task_dep_to_org"),
        Index("ix_task_dependency_to", "org_id", "to_task_id"),
    )


class TaskParticipant(Base, TenantMixin, TimestampMixin):
    """Who else is on a task, and in what role.

    Removal is recorded, not deleted: who could see a task last month is a
    question an auditor is entitled to ask.
    """

    __tablename__ = "task_participant"

    id: Mapped[uuid.UUID] = uuid_pk()
    task_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    participant_role: Mapped[ParticipantRole] = mapped_column(
        _pg_enum(ParticipantRole, "participant_role"), nullable=False
    )
    # `created_at` from TimestampMixin is when the participant was added.
    added_by: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    removed_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("id", "org_id", name="uq_task_participant_id_org"),
        UniqueConstraint("task_id", "user_id", "participant_role", name="uq_task_participant"),
        tenant_fk("task_id", "task", ondelete="CASCADE"),
        tenant_fk("user_id", "app_user", ondelete="CASCADE"),
        Index("ix_task_participant_user", "org_id", "user_id"),
        Index("ix_task_participant_task", "org_id", "task_id"),
    )


class TaskConversation(Base, TenantMixin, TimestampMixin):
    """One dialogue per (task, user). Never shared.

    Rows are created explicitly -- by the conversation endpoint, or by the
    assignment service when a user becomes owner or participant. No GET creates
    one, because a read that writes makes conversation counts meaningless and
    lets a permission check be sidestepped by simply looking.
    """

    __tablename__ = "task_conversation"

    id: Mapped[uuid.UUID] = uuid_pk()
    task_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    title: Mapped[str | None] = mapped_column(String(300), nullable=True)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    message_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    __table_args__ = (
        UniqueConstraint("id", "org_id", name="uq_task_conversation_id_org"),
        UniqueConstraint("task_id", "user_id", name="uq_task_conversation_task_user"),
        tenant_fk("task_id", "task", ondelete="CASCADE"),
        tenant_fk("user_id", "app_user", name="fk_task_conversation_user_org"),
        Index("ix_task_conversation_user", "org_id", "user_id", "last_message_at"),
    )


class ChatMessage(Base, TenantMixin):
    """A turn in a task conversation.

    `retrieval_trace` records the query, the filters applied and the candidates
    that survived. It is what makes a suspected leak investigable after the
    fact instead of merely deniable.
    """

    __tablename__ = "chat_message"

    id: Mapped[uuid.UUID] = uuid_pk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    role: Mapped[ChatRole] = mapped_column(_pg_enum(ChatRole, "chat_role"), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    cited_evidence_ids: Mapped[list[uuid.UUID] | None] = mapped_column(
        ARRAY(PGUUID(as_uuid=True)), nullable=True
    )
    cited_chunk_ids: Mapped[list[uuid.UUID] | None] = mapped_column(
        ARRAY(PGUUID(as_uuid=True)), nullable=True
    )
    # Cross-department answers, cited as [R#]. A third column rather than a
    # third meaning for one of the others: a corpus chunk, an approved evidence
    # item and a colleague's written answer are three different kinds of
    # backing, and a reviewer asking "which claims rest on somebody's word
    # rather than on a document" needs to be able to ask it in SQL.
    cited_request_ids: Mapped[list[uuid.UUID] | None] = mapped_column(
        ARRAY(PGUUID(as_uuid=True)), nullable=True
    )
    retrieval_trace: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # What the person was looking at when this turn was said: NULL for the task as
    # a whole, otherwise a key naming the requirements or the section. A thread is
    # the turns that share a key, so talk about one thing never bleeds into another.
    focus_key: Mapped[str | None] = mapped_column(String(300), nullable=True)
    focus: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    token_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    model_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default="now()", nullable=False
    )

    __table_args__ = (
        UniqueConstraint("id", "org_id", name="uq_chat_message_id_org"),
        tenant_fk("conversation_id", "task_conversation", ondelete="CASCADE"),
        Index("ix_chat_message_thread", "org_id", "conversation_id", "created_at"),
    )


class ConversationSummary(Base, TenantMixin, TimestampMixin):
    """A rollup of a contiguous message range, so long threads stay affordable.

    The range covered is explicit. A summary that cannot say which messages it
    replaces cannot be safely re-expanded or re-generated.
    """

    __tablename__ = "conversation_summary"

    id: Mapped[uuid.UUID] = uuid_pk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    covers_from_message_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    covers_to_message_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        UniqueConstraint("id", "org_id", name="uq_conversation_summary_id_org"),
        tenant_fk("conversation_id", "task_conversation", ondelete="CASCADE"),
        Index("ix_conversation_summary_thread", "org_id", "conversation_id"),
    )


class CrossDepartmentRequest(Base, TenantMixin, TimestampMixin):
    """An ask across a department boundary, and what was decided about it.

    The three request types are offered in least-privilege order. Answering
    creates no grant at all; sharing evidence creates one narrow task-scoped
    grant per item; granting access creates the broader task-scoped grant that
    was asked for. The request row is the record of the decision, never the
    authority itself.
    """

    __tablename__ = "cross_department_request"

    id: Mapped[uuid.UUID] = uuid_pk()
    bid_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    origin_task_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    requester_user_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    requester_department_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    target_department_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    target_user_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    request_type: Mapped[RequestType] = mapped_column(
        _pg_enum(RequestType, "request_type"), nullable=False
    )
    request_text: Mapped[str] = mapped_column(Text, nullable=False)
    business_reason: Mapped[str] = mapped_column(Text, nullable=False)
    requested_resource_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    requested_resource_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    state: Mapped[RequestState] = mapped_column(
        _pg_enum(RequestState, "request_state"), nullable=False
    )
    resolution_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    shared_evidence_ids: Mapped[list[uuid.UUID] | None] = mapped_column(
        ARRAY(PGUUID(as_uuid=True)), nullable=True
    )
    access_grant_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    # Set when the request was raised from a gap in the tender, so the answer can
    # come back to that gap.
    clarification_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    # Set when the person who asked has dealt with the reply, so it leaves their to-do list.
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("id", "org_id", name="uq_cross_department_request_id_org"),
        tenant_fk("clarification_id", "clarification_issue", name="fk_request_clarification_org"),
        tenant_fk("bid_id", "bid", ondelete="CASCADE"),
        tenant_fk("origin_task_id", "task", ondelete="CASCADE"),
        tenant_fk("requester_user_id", "app_user", name="fk_request_requester_org"),
        tenant_fk("target_user_id", "app_user", name="fk_request_target_user_org"),
        tenant_fk("requester_department_id", "department", name="fk_request_requester_dept_org"),
        tenant_fk("target_department_id", "department", name="fk_request_target_dept_org"),
        tenant_fk("access_grant_id", "access_grant", name="fk_request_grant_org"),
        Index("ix_request_inbox", "org_id", "target_department_id", "state"),
        Index("ix_request_origin", "org_id", "origin_task_id"),
    )


class RequirementRequest(Base, TenantMixin, TimestampMixin):
    """Which requirements a cross-department request is about."""

    __tablename__ = "requirement_request"

    id: Mapped[uuid.UUID] = uuid_pk()
    request_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    requirement_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("id", "org_id", name="uq_requirement_request_id_org"),
        UniqueConstraint("request_id", "requirement_id", name="uq_requirement_request"),
        ForeignKeyConstraint(
            ["request_id", "org_id"],
            ["cross_department_request.id", "cross_department_request.org_id"],
            ondelete="CASCADE",
            name="fk_requirement_request_request_org",
        ),
        tenant_fk("requirement_id", "requirement", ondelete="CASCADE"),
    )


class DraftHold(Base, TenantMixin, TimestampMixin):
    """A part of the response draft that is waiting on something from another department.

    The writer does not have to leave the part empty: whatever they wrote stays, and the
    hold keeps it marked ("fix this when the answer comes") until a person says it is
    fixed. Kept apart from the draft's own versions on purpose -- a new version of the
    draft must not wipe the reminder, and clearing it must not make a new version.

    A hold is on a requirement (the sentences that answer it) or on a named section.
    `state` is "open" or "fixed".
    """

    __tablename__ = "draft_hold"

    id: Mapped[uuid.UUID] = uuid_pk()
    task_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    requirement_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    section: Mapped[str | None] = mapped_column(String(200), nullable=True)
    department_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    request_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    clarification_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    state: Mapped[str] = mapped_column(String(8), nullable=False, default="open")
    raised_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    fixed_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    fixed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("id", "org_id", name="uq_draft_hold_id_org"),
        CheckConstraint(
            "requirement_id IS NOT NULL OR section IS NOT NULL", name="chk_draft_hold_target"
        ),
        tenant_fk("task_id", "task", ondelete="CASCADE"),
        tenant_fk("requirement_id", "requirement", ondelete="CASCADE"),
        Index("ix_draft_hold_task", "org_id", "task_id", "state"),
    )
