"""The fixture must not quietly become a second Jadwa.

The Cybranytech pack exists for one reason: to be evidence, where the Jadwa
company pack is a register OF evidence to be collected. That distinction is the
whole value of the fixture, and it is exactly the kind of property that decays
silently -- somebody adds a row with a `[REPLACE]` field, the pack stops
proving anything, and the end-to-end run starts reporting BLOCKED everywhere
for a reason nobody traces back to the fixture.

So the property is asserted rather than assumed. If these fail, the fixture is
broken, not the product.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services import evidence_markers

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "cybranytech"
COMPANY = sorted((FIXTURE / "company").glob("*.md"))


def test_the_fixture_exists() -> None:
    """A fixture directory that quietly disappeared would make every test below
    pass by iterating over nothing -- the empty-parametrize trap."""
    assert COMPANY, f"no company documents under {FIXTURE / 'company'}"
    assert len(COMPANY) >= 7
    assert (FIXTURE / "tender" / "RFP_Cybersecurity_Assessment.md").exists()
    assert (FIXTURE / "EXPECTED.md").exists()


@pytest.mark.parametrize("path", COMPANY, ids=lambda p: p.name)
def test_the_fixture_is_actually_evidence(path: Path) -> None:
    """Zero placeholder markers. A document in this pack that reads as a
    request for evidence would be downgraded by `markers@v1`, and the bid it
    feeds could never reach DRAFTABLE -- which would look like a product defect
    and would be a fixture defect."""
    hits = evidence_markers.find(path.read_text(encoding="utf-8"))
    assert not hits, f"{path.name} reads as a placeholder: {hits}"


def test_the_expired_certificate_is_still_evidence() -> None:
    """The ISO document is the fixture's most important row and the easiest to
    get wrong.

    It must be complete, verified evidence that happens to have EXPIRED. If it
    were written as a placeholder it would be downgraded on the substantiation
    axis, and the test would pass for the wrong reason -- proving that missing
    evidence blocks a bid, which we already knew, instead of proving that an
    expiry demotes readiness while evidence status stays verified.
    """
    text = (FIXTURE / "company" / "02_iso27001.md").read_text(encoding="utf-8")
    assert not evidence_markers.find(text)
    assert "Expiry date" in text
    assert "IS-664120" in text


def test_the_mobile_gap_is_genuinely_absent() -> None:
    """Clause 3.3 asks for a mobile testing methodology and the pack must not
    contain one. If somebody helpfully adds mobile coverage, the fixture stops
    exercising the needs-human-input path that the walkthrough's SEC-03 is
    built on."""
    methodology = (FIXTURE / "company" / "06_vapt_methodology.md").read_text(encoding="utf-8")
    lowered = methodology.lower()
    assert "mobile" not in lowered
    assert "masvs" not in lowered
    tender = (FIXTURE / "tender" / "RFP_Cybersecurity_Assessment.md").read_text(encoding="utf-8")
    assert "mobile application testing methodology" in tender.lower()
