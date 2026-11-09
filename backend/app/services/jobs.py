"""Job state for streamed answers.

Why this exists: a copilot answer takes seconds, and a request that blocks for
seconds behind a load balancer is a request that gets cut. So `POST` accepts the
question, returns `202` with a job id, and the client streams the result from a
separate `GET`.

**Why it is not just a dict.** Those are two separate HTTP requests, and behind
more than one API replica they land on different processes. A process-local
store means the `GET` can reach a replica that has never heard of the job, so
the stream hangs on a job that is running perfectly well next door. That is not
a degradation under load; it breaks the moment a second replica exists.

So there are two backends:

* `RedisJobStore` -- shared state and a pub/sub channel per job. The default
  whenever Redis is configured, and what a horizontally scaled deployment uses.
* `InProcessJobStore` -- the original dict, kept for single-process runs
  (`run_local.py`, tests) where Redis would be ceremony for nothing.

Neither is durable, and neither needs to be: the message and its retrieval trace
are committed to PostgreSQL before the job is marked done, so a lost stream
costs the live update and never the answer.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any


def _text(value: object) -> str:
    """Redis values arrive as bytes or str depending on client configuration."""
    return value.decode() if isinstance(value, bytes) else str(value)


MAX_JOBS = 512
# In-flight state only; the durable record is the chat message in PostgreSQL.
JOB_TTL_SECONDS = 3600


@dataclass
class Job:
    id: uuid.UUID
    org_id: uuid.UUID
    user_id: uuid.UUID | None
    kind: str
    state: str = "PENDING"  # PENDING | RUNNING | DONE | FAILED
    events: list[dict] = field(default_factory=list)
    result: dict | None = None
    error: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    store: Any = field(default=None, repr=False)
    _done: threading.Event = field(default_factory=threading.Event, repr=False)

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": str(self.id),
            "kind": self.kind,
            "state": self.state,
            "created_at": self.created_at.isoformat(),
            "error": self.error,
        }


class InProcessJobStore:
    """Single-process job state. Correct only when there is one API process."""

    def __init__(self, max_jobs: int = MAX_JOBS) -> None:
        self._jobs: OrderedDict[uuid.UUID, Job] = OrderedDict()
        self._lock = threading.Lock()
        self._max = max_jobs

    def create(self, *, org_id: uuid.UUID, user_id: uuid.UUID | None, kind: str) -> Job:
        job = Job(id=uuid.uuid4(), org_id=org_id, user_id=user_id, kind=kind, store=self)
        with self._lock:
            self._jobs[job.id] = job
            while len(self._jobs) > self._max:
                self._jobs.popitem(last=False)
        return job

    def get(self, job_id: uuid.UUID, *, org_id: uuid.UUID) -> Job | None:
        """Scoped by tenant on read.

        A job id is a bearer token for whatever the job produced, so the store
        checks the organization rather than trusting the id to be unguessable.
        """
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None or job.org_id != org_id:
            return None
        return job

    def events(self, job: Job) -> list[dict]:
        with self._lock:
            return list(job.events)

    def emit(self, job: Job, event: str, data: dict) -> None:
        with self._lock:
            job.events.append({"event": event, "data": data})

    def start(self, job: Job) -> None:
        job.state = "RUNNING"

    def finish(self, job: Job, result: dict) -> None:
        job.result = result
        job.state = "DONE"
        self.emit(job, "done", result)
        job._done.set()

    def fail(self, job: Job, error: str) -> None:
        job.error = error
        job.state = "FAILED"
        self.emit(job, "error", {"detail": error})
        job._done.set()

    def wait(self, job: Job, timeout: float) -> bool:
        return job._done.wait(timeout)


class RedisJobStore:
    """Job state shared across API replicas.

    State lives in a hash per job and events in a Redis list, so a replica that
    did not start the job can still replay everything emitted so far and then
    follow along. Keys expire, because this is in-flight state: the durable
    record is the chat message in PostgreSQL.
    """

    def __init__(self, redis_url: str, *, ttl_seconds: int = JOB_TTL_SECONDS) -> None:
        import redis

        self._redis = redis.Redis.from_url(redis_url, decode_responses=True)
        self._ttl = ttl_seconds

    def _key(self, job_id: uuid.UUID) -> str:
        return f"bidready:job:{job_id}"

    def _events_key(self, job_id: uuid.UUID) -> str:
        return f"bidready:job:{job_id}:events"

    def create(self, *, org_id: uuid.UUID, user_id: uuid.UUID | None, kind: str) -> Job:
        job = Job(id=uuid.uuid4(), org_id=org_id, user_id=user_id, kind=kind, store=self)
        self._redis.hset(
            self._key(job.id),
            mapping={
                "org_id": str(org_id),
                "user_id": str(user_id) if user_id else "",
                "kind": kind,
                "state": job.state,
                "created_at": job.created_at.isoformat(),
                "error": "",
                "result": "",
            },
        )
        self._redis.expire(self._key(job.id), self._ttl)
        return job

    def get(self, job_id: uuid.UUID, *, org_id: uuid.UUID) -> Job | None:
        # Decoded explicitly rather than relying on decode_responses, so a
        # client configured elsewhere in bytes mode cannot silently break this.
        raw = {_text(k): _text(v) for k, v in self._redis.hgetall(self._key(job_id)).items()}
        if not raw or raw.get("org_id") != str(org_id):
            return None
        job = Job(
            id=job_id,
            org_id=org_id,
            user_id=uuid.UUID(raw["user_id"]) if raw.get("user_id") else None,
            kind=raw.get("kind", ""),
            state=raw.get("state", "PENDING"),
            error=raw.get("error") or None,
            created_at=datetime.fromisoformat(raw["created_at"]),
            store=self,
        )
        if raw.get("result"):
            job.result = json.loads(raw["result"])
        return job

    def events(self, job: Job) -> list[dict]:
        return [
            json.loads(_text(item))
            for item in self._redis.lrange(self._events_key(job.id), 0, -1)
        ]

    def emit(self, job: Job, event: str, data: dict) -> None:
        self._redis.rpush(self._events_key(job.id), json.dumps({"event": event, "data": data}))
        self._redis.expire(self._events_key(job.id), self._ttl)

    def start(self, job: Job) -> None:
        job.state = "RUNNING"
        self._redis.hset(self._key(job.id), "state", "RUNNING")

    def finish(self, job: Job, result: dict) -> None:
        job.result, job.state = result, "DONE"
        self._redis.hset(
            self._key(job.id), mapping={"state": "DONE", "result": json.dumps(result)}
        )
        self.emit(job, "done", result)

    def fail(self, job: Job, error: str) -> None:
        job.error, job.state = error, "FAILED"
        self._redis.hset(self._key(job.id), mapping={"state": "FAILED", "error": error})
        self.emit(job, "error", {"detail": error})

    def wait(self, job: Job, timeout: float) -> bool:
        """Poll for terminal state.

        Polling rather than blocking pub/sub because the reader may have joined
        after the event was published; the event list is the source of truth and
        this only paces the loop.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            raw_state = self._redis.hget(self._key(job.id), "state")
            state = _text(raw_state) if raw_state is not None else ""
            if state in ("DONE", "FAILED"):
                job.state = state
                return True
            time.sleep(min(0.1, timeout))
        return False


@lru_cache(maxsize=1)
def get_store() -> InProcessJobStore | RedisJobStore:
    """Redis when it is configured and reachable, otherwise the local store.

    Falling back rather than failing keeps `run_local.py` and the tests working
    with no Redis, while a deployment that sets a Redis URL gets the shared
    store without further configuration. The choice is logged, because "why did
    my stream hang behind the load balancer" should be answerable from the logs.
    """
    from app.core.config import get_settings
    from app.core.logging import get_logger

    log = get_logger("jobs")
    settings = get_settings()
    if settings.job_store == "memory":
        log.info("jobs.store", backend="memory", note="single process only")
        return InProcessJobStore()
    try:
        store = RedisJobStore(settings.redis_url)
        store._redis.ping()
        log.info("jobs.store", backend="redis")
        return store
    except Exception as exc:  # noqa: BLE001 - reported, then degraded
        log.warning("jobs.store_fallback", error=str(exc), backend="memory")
        return InProcessJobStore()
