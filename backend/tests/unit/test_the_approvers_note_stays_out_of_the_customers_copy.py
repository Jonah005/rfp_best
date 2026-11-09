from app.services.drafting import COMMITMENT_NOTE, strip_commitment_note


def test_the_note_is_removed_and_the_sentence_is_kept():
    text = f"We will fix findings in three months. {COMMITMENT_NOTE}"
    assert strip_commitment_note(text) == "We will fix findings in three months."


def test_a_sentence_without_the_note_is_unchanged():
    assert strip_commitment_note("We will test.") == "We will test."
