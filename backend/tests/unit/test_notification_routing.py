"""Who gets told, and who does not.

Notification bugs are quiet. Nothing throws when a message is routed to the
wrong person -- the wrong person just sees something they should not, or the
right person sits waiting for a bell that never rings and concludes the
governed path does not work. So the routing rules are asserted directly, at the
level where they are actually decided, rather than inferred from a screenshot.

Three rules carry the weight:

  * Nobody is notified of their own action. Nadia answering a request must not
    put "your request was answered" in Nadia's own list.
  * A department is its members, and a department with no members is nobody --
    a request raised into an empty room is a real condition, not a routing bug
    to paper over. Legal, in the seeded org, has no members at all.
  * A notification carries a title, a kind and a place to go. It never carries
    the answer, the evidence, or anything else that a permission check exists
    to guard, because a feed is the one surface nobody thinks to audit.
"""

from __future__ import annotations

import uuid

from app.core.context import ExecutionContext
from app.services import notifications


class _Session:
    """Enough Session for `notify`: it adds rows and flushes."""

    def __init__(self) -> None:
        self.added: list = []

    def add(self, row) -> None:
        self.added.append(row)

    def flush(self) -> None:
        pass


def _ctx(actor: uuid.UUID | None = None) -> ExecutionContext:
    return ExecutionContext(org_id=uuid.uuid4(), actor_user_id=actor)


def test_the_actor_is_never_told_about_their_own_action():
    """A feed full of your own work is a feed people stop reading, and the
    unread badge stops meaning anything the moment it counts your own clicks."""
    nadia = uuid.uuid4()
    sara = uuid.uuid4()
    session = _Session()

    notifications.notify(
        session,
        _ctx(nadia),
        recipients=[sara, nadia],
        kind="request.answered",
        title="Your request was answered",
        screen="tasks",
    )

    recipients = [row.recipient_user_id for row in session.added]
    assert recipients == [sara]


def test_one_person_named_twice_is_told_once():
    """Someone in two departments must not get the same request twice."""
    sara = uuid.uuid4()
    session = _Session()

    notifications.notify(
        session,
        _ctx(uuid.uuid4()),
        recipients=[sara, sara, sara],
        kind="request.created",
        title="A new request",
        screen="requests",
    )

    assert len(session.added) == 1


def test_a_department_with_no_members_is_told_nothing():
    """Legal exists in the seeded org and has no accounts in it. Raising a
    request there is addressing an empty room -- which the product should be
    able to show, not silently reroute to somebody else."""
    session = _Session()

    notifications.notify(
        session,
        _ctx(uuid.uuid4()),
        recipients=[],
        kind="request.created",
        title="A new request",
        screen="requests",
    )

    assert session.added == []


def test_a_missing_recipient_is_skipped_rather_than_stored():
    """An unassigned task has no owner. Notifying `None` would write a row
    addressed to nobody that no query would ever return and no one could clear."""
    session = _Session()

    notifications.notify(
        session,
        _ctx(uuid.uuid4()),
        recipients=[None],  # type: ignore[list-item]
        kind="task.blocked",
        title="A task is blocked",
        screen="tasks",
    )

    assert session.added == []


def test_a_notification_records_where_to_go():
    """A bell that tells you something happened and not where is a bell that
    makes you search. The screen key and the record to focus are the payload."""
    sara = uuid.uuid4()
    bid = uuid.uuid4()
    task = uuid.uuid4()
    session = _Session()

    notifications.notify(
        session,
        _ctx(uuid.uuid4()),
        recipients=[sara],
        kind="request.answered",
        title="Your request was answered",
        screen="tasks",
        bid_id=bid,
        focus_id=task,
    )

    row = session.added[0]
    assert (row.screen, row.bid_id, row.focus_id) == ("tasks", bid, task)


def test_a_request_notification_carries_the_question_not_the_answer():
    """The requester is told their request was answered and sent to the task,
    where the answer arrives as authorized context. Putting the resolution text
    in the notification itself would route material around the request that
    governs it -- and the feed has no permission check of its own."""
    import inspect

    from app.services import requests as request_service

    source = inspect.getsource(request_service._notify_requester)
    assert "request.request_text" in source
    assert "resolution_text" not in source
    assert "shared_evidence_ids" not in source


def test_every_resolution_tells_the_requester():
    """Six ways a request can end, and a person waiting on each of them. An
    unnotified resolution is the exact failure this was built to remove."""
    import inspect

    from app.services import requests as request_service

    for name in ("acknowledge", "answer", "share_evidence", "grant_access", "reject"):
        source = inspect.getsource(getattr(request_service, name))
        assert "_notify_requester" in source, f"{name} resolves a request and tells nobody"


def test_raising_a_request_tells_the_department_asked():
    import inspect

    from app.services import requests as request_service

    assert "_notify_department" in inspect.getsource(request_service.create_request)


def test_a_feed_cannot_be_read_for_somebody_else():
    """There is no recipient parameter, anywhere. A feed that took one would let
    anyone read anyone's notifications by editing a query string."""
    import inspect

    for fn in (notifications.for_user, notifications.unread_count, notifications.mark_all_read):
        parameters = inspect.signature(fn).parameters
        assert "recipient_user_id" not in parameters
        assert "user_id" not in parameters


def test_an_unauthenticated_context_has_an_empty_feed():
    """A system context -- the assessment runner, a scheduled sweep -- has no
    person behind it, and must not fall through to reading everyone's."""
    assert notifications.for_user(_Session(), _ctx(None)) == []  # type: ignore[arg-type]
