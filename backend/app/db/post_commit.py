"""Run work only after the request's transaction has actually committed.

Why this exists
---------------
Both upload routes used to enqueue their parse/index job with FastAPI's
``BackgroundTasks``. The comment on that code claimed background tasks run
after yield-dependency teardown, so the row would be committed before a worker
could load it. That is not what happens. Starlette runs background tasks from
inside ``Response.__call__``::

    await response(scope, receive, send)   # fastapi/routing.py
      -> await self.background()           # starlette/responses.py

which is *inside* the route's dependency exit stack. The ``get_db`` generator
has not resumed past its ``yield`` yet, so ``session.commit()`` has not run.

The failure that follows is quiet and destructive. The worker opens its own
session, cannot see the uncommitted ``document_version`` row, and raises
``NotFound``. With ``task_eager_propagates`` on (local single-process mode)
that exception propagates back through the response, ``get_db`` takes its
``except`` branch and rolls the transaction back — so the upload the caller was
just told succeeded, with a 201 and a document id, disappears entirely.

With a real broker the failure is a race rather than a certainty: the worker
usually loses to the commit, and occasionally does not. A job enqueued before
its data is visible is wrong either way.

So: register the enqueue as an ``after_commit`` hook on the session. It fires
once, only if the transaction actually committed, and never if it rolled back.
A hook that raises is logged and swallowed, because by then the data is safely
committed and the caller's request succeeded — losing the follow-up job is
recoverable, turning a committed write into a 500 is not.
"""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.logging import get_logger

log = get_logger("db.post_commit")


def run_after_commit(session: Session, work: Callable[[], None], *, label: str) -> None:
    """Call `work` once, immediately after `session` commits.

    Nothing runs if the transaction rolls back, which is the whole point: the
    hook is how "this row now exists" becomes a fact other processes can rely
    on rather than an assumption about teardown ordering.
    """

    # A flag, not `event.remove`: SQLAlchemy dispatches `after_commit` by
    # iterating its listener collection, and removing a listener from inside
    # that loop raises "deque mutated during iteration" out of `session.commit`
    # — which turns a successful write into a 500. The listener therefore stays
    # registered and simply declines to run twice. Sessions here are per
    # request, so nothing accumulates.
    fired = False

    def _fire(_session: Session) -> None:
        nonlocal fired
        if fired:
            return
        fired = True
        try:
            work()
        except Exception:
            # The write is committed. Losing the follow-up job is a recoverable
            # backlog problem; re-raising here would surface as a 500 on a
            # request that already succeeded.
            log.exception("post_commit_hook_failed", hook=label)

    event.listen(session, "after_commit", _fire)
