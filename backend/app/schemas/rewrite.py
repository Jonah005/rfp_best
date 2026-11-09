"""What a model returns when asked to reword a piece of the draft."""

from __future__ import annotations

from pydantic import BaseModel, Field


class RewriteResult(BaseModel):
    """One replacement per piece asked for, in the order asked."""

    replacements: list[str] = Field(min_length=1, max_length=40)


class ExplainResult(BaseModel):
    """A model's explanation of a picture or a drawn figure, for the person to use or not."""

    text: str = Field(min_length=1, max_length=3000)
    # Things in the picture that could not be made out, said rather than guessed.
    unclear: list[str] = Field(default_factory=list, max_length=8)
