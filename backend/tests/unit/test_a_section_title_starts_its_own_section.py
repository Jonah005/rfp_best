"""An unnumbered section title has to end the previous clause's segment.

Measured on the live tender: Integration 4 (SMS) and Business 4 (Menu) both
carried the reference "Business Requirements / 4". Everything under Integration
Requirements and Technical Requirements kept the name of the section BEFORE it,
because those titles are unnumbered -- and only a numbered line started a new
segment. The title was swallowed by the preceding clause, so it never became a
heading, and the glued blob ("2. Menu Website should adopt a drop down based
menu. Integration Requirements") was short enough to be filed as one.

One reference for two unrelated obligations is the failure that
`clause_identity` exists to prevent, arriving one layer earlier.
"""

from __future__ import annotations

from app.ingestion.parsers import _page_segments, looks_like_heading, looks_like_section_title


def test_a_bare_section_title_is_recognised():
    for line in ("Integration Requirements", "Technical Requirements", "EVALUATION FACTORS"):
        assert looks_like_section_title(line), line


def test_a_sentence_or_a_numbered_clause_is_not_a_section_title():
    for line in (
        "The portal should support SSL configuration.",
        "3. SSL Support",
        "the vendor is required to configure IIS",
        "Jadwa will provide SMS service connection details to send validation SMS",
    ):
        assert not looks_like_section_title(line), line


def test_a_title_after_a_clause_starts_a_new_segment():
    page = "\n".join(
        [
            "2. Menu",
            "Website should adopt a drop down based menu.",
            "Integration Requirements",
            "1. Al-Elm Services",
            "The buyer manages the subscription.",
        ]
    )
    segments = _page_segments(page, set())
    assert segments[0].startswith("2. Menu")
    assert segments[1] == "Integration Requirements"
    assert segments[2].startswith("1. Al-Elm Services")


def test_a_glued_blob_is_never_filed_as_a_heading():
    assert not looks_like_heading(
        "2. Menu Website should adopt a drop down based menu. Integration Requirements"
    )
    assert looks_like_heading("Integration Requirements")
