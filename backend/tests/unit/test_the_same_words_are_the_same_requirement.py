"""A full stop is not a new requirement.

A re-run opened nine "this requirement was re-extracted and now reads
differently" items, MAJOR, on a bid whose source had not changed. Several were a
final period, a stray space or a capital letter. A drift item is meant to make
somebody check that a code still means what it meant; spending that on
punctuation trains people to ignore the item that matters.
"""

from __future__ import annotations

from app.services import requirements as r


def test_spacing_case_and_punctuation_are_not_a_change():
    assert r.same_wording(
        "Develop engine to prompty send SMS", "Develop engine to prompty send SMS."
    )
    assert r.same_wording("Ensure  Arabic SMS is supported", "ensure arabic sms is supported")
    assert r.same_wording("  The site  must be fast ;", "The site must be fast")


def test_a_different_word_is_a_change():
    assert not r.same_wording(
        "Proposals must remain valid for 90 days", "Proposals must remain valid for 60 days"
    )


def test_a_dropped_section_label_is_benign():
    assert r.is_benign_rewording(
        "SSL Support: The portal should support SSL confgiuration.",
        "The portal should support SSL confgiuration.",
    )


def test_a_narrowing_is_not_benign():
    """The row used to carry two obligations and now carries a fragment of one."""
    before = (
        "Tradenet Core OMS System: Jadwa will provide access to Core OMS system for the "
        "following purpose: Check client already exsist; Create client account by adding all "
        "collected information"
    )
    after = "Jadwa will provide access to Core OMS system"
    assert not r.is_benign_rewording(before, after)


def test_an_unrelated_rewrite_is_not_benign():
    assert not r.is_benign_rewording(
        "responses to clarifications would be annexed to the contract",
        "the Firm must prepare the number of copies as indicated",
    )


def test_an_empty_text_is_never_benign():
    assert not r.is_benign_rewording("", "anything")
    assert not r.is_benign_rewording(None, None)
