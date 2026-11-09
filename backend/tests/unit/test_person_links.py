"""A CV describes a person; an account is an identity. Connecting them safely.

Two populations of "people" exist in this system and they are separate on
purpose. `app_user` rows sign in, hold roles and own tasks, and are created only
by seeding or an org admin. PERSON entities are evidence, extracted from
uploaded CVs. Uploaded documents are untrusted input, so if extraction could
create or claim an account, uploading a CV would mint an identity.

The link between them is therefore built to carry as little as possible: a
matcher proposes, a person decides, and the result is a stated fact that grants
nothing. These cases are about that shape — what the matcher must refuse to
guess, and what a proposal is not allowed to do on its own.
"""

from __future__ import annotations

import uuid

import pytest

from app.core.errors import ValidationError
from app.models.enums import PersonLinkStatus
from app.services import person_links
from app.services.person_links import (
    EMAIL_LOCAL,
    EXACT_NAME,
    SURNAME_INITIAL,
    _match_one,
    normalise,
)


class _User:
    def __init__(self, email, display_name):
        self.id = uuid.uuid4()
        self.email = email
        self.display_name = display_name
        self.is_active = True


# The seeded organization, as it actually is.
ACCOUNTS = [
    _User("sara.k@acme-cyber.example.com", "Sara K."),
    _User("ahmed.h@acme-cyber.example.com", "Ahmed Hassan"),
    _User("nadia.r@acme-cyber.example.com", "Nadia R."),
    _User("leila.m@acme-cyber.example.com", "Leila M."),
]


# --- what the matcher should find -------------------------------------------


def test_an_exact_name_matches():
    match = _match_one("Ahmed Hassan", ACCOUNTS)
    assert match.user is ACCOUNTS[1]
    assert match.method == EXACT_NAME


def test_case_and_spacing_do_not_matter():
    assert _match_one("  ahmed   hassan ", ACCOUNTS).user is ACCOUNTS[1]


def test_accents_are_folded():
    """The same person is written both ways across a CV, an HR export and an
    email address; a match that depends on which one you read is not a match."""
    accounts = [_User("jose.p@example.com", "José Pérez")]
    assert _match_one("Jose Perez", accounts).user is accounts[0]


def test_an_email_built_from_the_name_matches():
    """ "Ahmed H." in a CV against ahmed.h@ — the account's own address is the
    strongest evidence available short of an exact name."""
    match = _match_one("Ahmed H.", ACCOUNTS)
    assert match.user is ACCOUNTS[1]
    assert match.method == EMAIL_LOCAL


def test_a_full_name_matches_an_abbreviated_account():
    """The common real case: HR writes "Sara Khalifa", the account says "Sara K.\""""
    match = _match_one("Sara Khalifa", ACCOUNTS)
    assert match.user is ACCOUNTS[0]
    assert match.method == SURNAME_INITIAL
    # And it is flagged as the weakest kind, because it is a guess about a
    # surname prefix rather than a fact about a string.
    assert match.confidence is not None and match.confidence < 0.9
    assert "check this one" in match.note


def test_a_name_particle_does_not_break_the_surname():
    accounts = [_User("k.vandenberg@example.com", "Klaas van den Berg")]
    assert _match_one("Klaas van den Berg", accounts).user is accounts[0]


# --- what it must refuse to guess -------------------------------------------


def test_an_unknown_person_matches_nothing():
    """Most people in a CV pack have no account, and that is not a failure."""
    match = _match_one("Fatima Al Mansouri", ACCOUNTS)
    assert match.user is None
    assert "No account matches" in match.note


def test_two_accounts_with_the_same_name_are_not_guessed_between():
    """A coin toss presented as a finding is worse than an admitted gap."""
    accounts = [
        _User("a.hassan@example.com", "Ahmed Hassan"),
        _User("ahmed.hassan@example.com", "Ahmed Hassan"),
    ]
    match = _match_one("Ahmed Hassan", accounts)
    assert match.user is None
    assert "a person must choose" in match.note


def test_two_surname_candidates_are_not_guessed_between():
    accounts = [_User("s.k@example.com", "Sara K."), _User("s.kh@example.com", "Sami Kh.")]
    match = _match_one("Sara Khalifa", accounts)
    # Sami has a different first initial, so this must still resolve to Sara.
    assert match.user is accounts[0]

    ambiguous = [_User("s.k@example.com", "Sara K."), _User("s.kh@example.com", "Sara Kh.")]
    assert _match_one("Sara Khalifa", ambiguous).user is None


def test_an_initial_alone_is_not_a_name():
    """ "K." must not match every account whose surname starts with K."""
    assert _match_one("K.", ACCOUNTS).user is None


def test_an_empty_name_matches_nothing():
    assert _match_one("", ACCOUNTS).user is None
    assert normalise("") == []


# --- what a link is, and is not ---------------------------------------------


def test_bulk_confirmation_requires_a_confidence_floor():
    """ "Approve everything" is how a review becomes a rubber stamp.

    An exact name against a nine-person organization is not a decision anybody
    learns from, so sweeping those up is reasonable. A surname-and-initial guess
    is exactly the kind of match a person should look at, and it must not ride
    along with the certain ones.
    """

    class _Session:
        def execute(self, *_a, **_k):
            raise AssertionError("must refuse before touching the database")

    with pytest.raises(ValidationError) as exc:
        person_links.confirm_all(_Session(), object(), min_confidence=0)
    assert "one at a time" in str(exc.value)


def test_the_surname_guess_sits_below_the_bulk_threshold():
    """The rule above only works if the confidences are actually separated."""
    assert person_links.CONFIDENCE[SURNAME_INITIAL] < 0.9
    assert person_links.CONFIDENCE[EXACT_NAME] >= 0.9
    assert person_links.CONFIDENCE[EMAIL_LOCAL] >= 0.9


def test_only_a_confirmed_link_is_readable_downstream():
    """A PROPOSED row is a suggestion. Nothing may act on a match nobody has
    confirmed, so the one read path other code is given filters on CONFIRMED."""
    import inspect

    source = inspect.getsource(person_links.confirmed_account_for)
    assert "PersonLinkStatus.CONFIRMED" in source


def test_a_link_confers_no_permission():
    """The security property, asserted rather than assumed.

    If the permission evaluator ever reads this table, uploading a CV becomes a
    path to access — which is the exact reason the two populations are separate.
    """
    import inspect

    from app.services import permission_evaluator

    assert "PersonLink" not in inspect.getsource(permission_evaluator)
    assert "person_link" not in inspect.getsource(permission_evaluator)


def test_extraction_never_creates_an_account():
    """The boundary that makes all of this necessary."""
    import inspect

    from app.services import knowledge_extraction

    source = inspect.getsource(knowledge_extraction)
    assert "User(" not in source
    assert "app_user" not in source


def test_a_rejection_is_a_recorded_state():
    """Otherwise the next run proposes the same wrong pair and a reviewer's
    decision decays into a recurring chore."""
    assert PersonLinkStatus.REJECTED.value == "REJECTED"
    import inspect

    source = inspect.getsource(person_links.propose_links)
    assert "is not PersonLinkStatus.PROPOSED" in source
