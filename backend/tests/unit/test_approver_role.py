"""A commitment routed to nobody, and a draft lost over punctuation.

Generating a draft produced this and nothing else:

    A proposed commitment needs a valid approver role.

One statement in the response named an approver the server did not recognise,
and the whole draft was refused -- a paid model call, an empty version, and a
message naming neither the offending value nor the acceptable ones. The only
way to learn either was to read the source.

The cause was upstream. The prompt said a commitment "needs a named approver
role" and never said what the roles ARE, so the model was invited to write a
plausible English job title and the server rejected it for not being an enum
member. Naming a constraint without naming its values is how you set a model up
to fail.

Two changes, and the balance between them is the point: be forgiving about the
SHAPE of the answer, strict about its MEANING. "bid manager" is the same role
as "BID_MANAGER" written by someone not thinking about enums. "Head of
Delivery" is not a role at all, and accepting it would route a commitment to
nobody -- which is worse than refusing the draft, because it looks like it
worked.
"""

from __future__ import annotations

import pytest

from app.core.errors import ValidationError
from app.core.permissions import COMMITMENT_APPROVER_ROLES, RoleCode
from app.gateway import registry
from app.services.artifacts import _approver_role

# --- forgiving about shape ---------------------------------------------------


@pytest.mark.parametrize(
    "written",
    ["BID_MANAGER", "bid manager", "Bid-Manager", "  bid   manager  ", "bid_manager"],
)
def test_the_same_role_written_five_ways(written):
    assert _approver_role(written) == "BID_MANAGER"


def test_every_role_that_can_own_a_promise_survives_a_round_trip():
    """Whatever the approver set holds today, including roles added later.

    This used to loop over the whole `RoleCode` enum. It stopped being right
    when the accepted set narrowed to `COMMITMENT_APPROVER_ROLES`: an AUDITOR
    reviews what happened and undertakes nothing, so routing a promise there is
    a category error rather than a slow approval, and the door is the place to
    refuse it.
    """
    for role in sorted(COMMITMENT_APPROVER_ROLES):
        assert _approver_role(role) == role
        assert _approver_role(role.replace("_", " ").lower()) == role


def test_a_real_role_that_cannot_own_a_promise_is_still_refused():
    """The other half, and the one a widening would silently lose.

    Every excluded role is a real `RoleCode`, so nothing about its spelling
    marks it out -- only the narrowing does. Without this, relaxing the set
    back to the full enum would keep every other test in this file green.
    """
    excluded = {r.value for r in RoleCode} - COMMITMENT_APPROVER_ROLES
    assert excluded, "the narrowing has been undone; a commitment can be routed anywhere"
    for role in sorted(excluded):
        with pytest.raises(ValidationError):
            _approver_role(role)


# --- strict about meaning ----------------------------------------------------


def test_an_invented_job_title_is_refused():
    """The failure that matters. A commitment is the one statement class that
    binds the company, and an unrecognised approver is a promise with nobody
    accountable for approving it."""
    with pytest.raises(ValidationError):
        _approver_role("Head of Delivery")


@pytest.mark.parametrize("nothing", ["", "   ", None])
def test_no_role_at_all_is_refused(nothing):
    with pytest.raises(ValidationError):
        _approver_role(nothing)


def test_a_near_miss_is_not_guessed_into_a_real_role():
    """Normalising punctuation is not the same as fuzzy matching. "BID" is not
    "BID_MANAGER", and quietly deciding it was would put a commitment in front
    of an approver nobody chose."""
    with pytest.raises(ValidationError):
        _approver_role("BID")


# --- and the refusal has to be usable ---------------------------------------


def test_the_error_names_the_offending_value():
    with pytest.raises(ValidationError) as exc:
        _approver_role("Head of Delivery")
    assert "Head of Delivery" in str(exc.value)


def test_the_error_lists_what_would_have_worked():
    with pytest.raises(ValidationError) as exc:
        _approver_role("Head of Delivery")
    message = str(exc.value)
    assert "BID_MANAGER" in message and "LEGAL" in message


# --- and the model is told, rather than left to guess ------------------------


def test_the_prompt_offers_exactly_what_the_server_accepts():
    """The contract, asserted on both sides.

    An earlier version of this read the registry's SOURCE and asserted it
    contained the literal expression `sorted(r.value for r in RoleCode)`. That
    passes for a prompt built the approved way and says nothing about what the
    prompt actually offers -- and it broke the moment the expression changed,
    while a hand-typed list that happened to be correct would have failed it.
    Assert the behaviour instead: everything accepted is offered, and nothing
    offered is refused.

    Both directions cost real money when they drift. A role offered but not
    accepted fails an entire draft after the call is billed; a role accepted
    but never offered makes the narrowing decorative.
    """
    prompt = registry.get("draft_statement_generation").system_prompt
    for role in sorted(COMMITMENT_APPROVER_ROLES):
        assert role in prompt, f"{role} is accepted but never offered to the model"
    for role in sorted({r.value for r in RoleCode} - COMMITMENT_APPROVER_ROLES):
        assert role not in prompt, f"{role} is offered to the model but would be refused"


def test_the_prompt_forbids_rewording_them():
    prompt = registry.get("draft_statement_generation").system_prompt
    assert "copied " in prompt and "verbatim" in prompt
    assert "Do not invent a job title" in prompt


def test_the_prompt_version_moved():
    assert registry.get("draft_statement_generation").prompt_version != "v1"
