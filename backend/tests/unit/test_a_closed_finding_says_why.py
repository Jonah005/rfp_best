"""Closing a finding without saying why reads as dropping it.

The live bid closed all 47 coverage gaps and recorded nothing on any of them:
each one still said "must be reviewed manually before drafting" while its
status said closed. A reader could not tell a resolved gap from a lost one.
"""

from __future__ import annotations

from app.gateway.registry import get
from app.schemas.tender import ClarificationOut


def test_the_wire_shape_carries_the_reason():
    assert "resolution_note" in ClarificationOut.model_fields


def test_a_finding_must_come_with_a_question_for_the_customer():
    prompt = get("ambiguity_analysis").system_prompt
    assert "MUST carry `recommended_question`" in prompt
    assert "do not raise the issue at all" in prompt
    # That the version is STAMPED is the load-bearing fact -- it is what
    # retires the previous run's findings. Pinning the number instead made a
    # later prompt improvement fail a test about a different rule, which is a
    # test teaching the wrong lesson: do not change the prompt.
    assert get("ambiguity_analysis").prompt_version.startswith("v")
