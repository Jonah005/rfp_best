"""What a model may return for the diagram calls.

The structure of a diagram itself is validated by `app.services.diagram_spec`,
so it is carried here as plain data: a model that produced a bad structure gets
the reason back, not a parse failure in the middle of a response.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.drafting import DraftStatementProposal


class MissingInformation(BaseModel):
    what: str = Field(min_length=1, max_length=400)
    why: str = Field(default="", max_length=400)
    # Requirements that already ask for this, by code. Empty means nothing on
    # the list asks for it yet and a new question would be needed.
    requirement_codes: list[str] = Field(default_factory=list, max_length=12)
    # Worded as a question the customer or a colleague could actually answer.
    suggested_question: str | None = Field(default=None, max_length=500)


class NeedProposal(BaseModel):
    diagram_type: str
    why: str = Field(max_length=500)
    requirement_codes: list[str] = Field(default_factory=list, max_length=30)
    enough_information: bool
    missing: list[MissingInformation] = Field(default_factory=list, max_length=8)


class NotNeeded(BaseModel):
    diagram_type: str
    reason: str = Field(max_length=300)


class DiagramPlanResult(BaseModel):
    needs: list[NeedProposal] = Field(default_factory=list, max_length=8)
    not_needed: list[NotNeeded] = Field(default_factory=list, max_length=8)


class DiagramDraftResult(BaseModel):
    enough_information: bool
    missing: list[MissingInformation] = Field(default_factory=list, max_length=8)
    title: str | None = Field(default=None, max_length=200)
    spec: dict[str, Any] | None = None
    requirement_codes: list[str] = Field(default_factory=list, max_length=30)
    # Detail diagrams that could follow when this one is kept at overview level.
    detail_diagrams: list[str] = Field(default_factory=list, max_length=4)


class CouldNot(BaseModel):
    request: str = Field(max_length=400)
    reason: str = Field(max_length=400)


class DiagramRevisionResult(BaseModel):
    spec: dict[str, Any] | None = None
    could_not: list[CouldNot] = Field(default_factory=list, max_length=8)
    missing: list[MissingInformation] = Field(default_factory=list, max_length=8)


class Suggestion(BaseModel):
    severity: Literal["IMPORTANT", "SUGGESTION", "NOTE"] = "SUGGESTION"
    text: str = Field(min_length=1, max_length=500)
    boxes: list[str] = Field(default_factory=list, max_length=8)
    requirement_codes: list[str] = Field(default_factory=list, max_length=12)
    # True when acting on it needs information the sources do not contain.
    needs_information: bool = False


class DiagramReviewResult(BaseModel):
    suggestions: list[Suggestion] = Field(default_factory=list, max_length=15)
    verdict: str = Field(default="", max_length=400)


class StyleIntentResult(BaseModel):
    style: dict[str, Any] = Field(default_factory=dict)
    understood: str = Field(default="", max_length=300)
    could_not: list[str] = Field(default_factory=list, max_length=6)


class DiagramImageReading(BaseModel):
    """What a vision model read from a picture of a diagram."""

    is_a_diagram: bool
    diagram_type: str | None = None
    title: str | None = Field(default=None, max_length=200)
    spec: dict[str, Any] | None = None
    # Text or parts the model could not read with confidence, so a person can check.
    unclear: list[str] = Field(default_factory=list, max_length=15)


class DiagramStep(BaseModel):
    """One move in the plan the router lays out for a message."""

    action: Literal[
        "WHICH_NEEDED",
        "CREATE",
        "CHANGE",
        "RESTYLE",
        "REVIEW",
        "APPLY_SUGGESTIONS",
        "NOT_NEEDED",
        "RECORD_QUESTIONS",
        "REFRESH",
        "SHOW",
        "WRITE_UP",
        "WRITE_REQUIREMENTS",
        "LEAVE_OUT",
        "INCLUDE",
        "REMOVE",
    ]
    diagram_type: str | None = None
    # A reference from the list shown (D1, D2 ...), or LAST for the diagram an
    # earlier step of this same plan made or changed. Null when not said.
    target: str | None = Field(default=None, max_length=8)
    # Several diagrams at once (WRITE_UP, LEAVE_OUT, INCLUDE): "the flow and the architecture".
    targets: list[str] = Field(default_factory=list, max_length=8)
    # WRITE_UP only: they said all of the diagrams, so do not ask which.
    all_diagrams: bool = False
    # The wish, in the person's words: what to change, how it should look, a note.
    instruction: str | None = Field(default=None, max_length=2000)
    suggestion_numbers: list[int] = Field(default_factory=list, max_length=15)
    all_suggestions: bool = False
    reason: str | None = Field(default=None, max_length=500)


class DiagramIntentResult(BaseModel):
    """The router's reading of a message in the task chat.

    The model only decides whether diagrams are involved and lays out the moves.
    Doing them -- and whether the person may -- is decided by the platform.
    """

    wants_diagram: bool = False
    steps: list[DiagramStep] = Field(default_factory=list, max_length=4)


class WriteupResult(BaseModel):
    """One section of the response: typed statements explaining a diagram.

    The same typed proposals the first-draft call returns, so the same rules apply
    to them afterwards. The model writes; the platform checks every ref and decides
    what each sentence is allowed to claim.
    """

    statements: list[DraftStatementProposal] = Field(min_length=1, max_length=45)
