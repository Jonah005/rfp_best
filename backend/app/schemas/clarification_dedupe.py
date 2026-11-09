"""Contract for judging whether two clarification questions are one question.

Character similarity cannot do this. "What is the required scope, format and
duration of the source code and development knowledge transfer?" and "What are
the required IP/source ownership terms and the expected format and duration of
the knowledge transfer?" share few enough words to score low and are answered by
the same reply from the customer. Meanwhile two genuinely different questions
about one clause -- what "final release" means, and how proposals are weighted
-- can score high because they quote the same lead-in.

So the judgement is the model's and the record is the code's, the same division
every other detector here uses: the verdict is stored with a version stamp
(`equivalence_by`), a restatement points at what it restates, and nothing is
deleted.

The model never decides what happens next. It answers one question -- would a
single reply from the customer answer both of these -- and the service decides
which row survives, which links are removed, and what the matrix shows.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


class QuestionGroup(BaseModel):
    """One question, and the other phrasings of it in this set."""

    # Index into the numbered list the caller supplied. The clearest and most
    # complete phrasing, because it is the one a person will send.
    canonical_index: int = Field(ge=0)
    restatement_indexes: list[int] = Field(default_factory=list)
    # Why these are the same question, in the model's own words. Stored on the
    # restatement so a reviewer who disagrees can see the reasoning rather than
    # just the verdict.
    reason: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def _canonical_is_not_its_own_restatement(self) -> QuestionGroup:
        if self.canonical_index in self.restatement_indexes:
            raise ValueError(
                "canonical_index must not appear in restatement_indexes; a question "
                "cannot restate itself"
            )
        if len(set(self.restatement_indexes)) != len(self.restatement_indexes):
            raise ValueError("restatement_indexes must not repeat an index")
        return self


class QuestionDedupeResult(BaseModel):
    """Groups of two or more. Anything not mentioned stands on its own.

    Deliberately not "for each question, say which one it duplicates": that
    phrasing invites a chain -- A restates B, B restates C -- and leaves the
    service to resolve it. Groups make the canonical explicit.
    """

    groups: list[QuestionGroup] = Field(default_factory=list)

    @model_validator(mode="after")
    def _each_question_appears_once(self) -> QuestionDedupeResult:
        seen: set[int] = set()
        for group in self.groups:
            for index in [group.canonical_index, *group.restatement_indexes]:
                if index in seen:
                    raise ValueError(
                        f"question {index} appears in more than one group; each "
                        "question belongs to at most one"
                    )
                seen.add(index)
        return self
