"""Job state has to survive crossing a process boundary.

The copilot answers over two HTTP requests: a POST that starts the work and a
GET that streams it. Behind more than one API replica those land on different
processes. A process-local store means the GET can reach a replica that has
never heard of the job, so the stream hangs on work that is running fine next
door -- and it fails the moment a second replica exists, not gradually under
load.

Each test below runs the two halves through *separate store objects*, which is
the cheapest faithful stand-in for two replicas.
"""

from __future__ import annotations

import uuid

import pytest

from app.services.jobs import InProcessJobStore, RedisJobStore


@pytest.fixture
def redis_stores(monkeypatch):
    """Two RedisJobStore objects over one shared fake server: two 'replicas'."""
    import fakeredis

    server = fakeredis.FakeServer()

    def make() -> RedisJobStore:
        store = RedisJobStore.__new__(RedisJobStore)
        store._redis = fakeredis.FakeRedis(server=server, decode_responses=True)
        store._ttl = 60
        return store

    return make(), make()


ORG = uuid.uuid4()


def test_a_second_replica_can_read_a_job_it_did_not_start(redis_stores):
    """The whole point. Replica A starts the work; replica B serves the stream."""
    replica_a, replica_b = redis_stores

    job = replica_a.create(org_id=ORG, user_id=None, kind="copilot_ask")
    replica_a.start(job)
    replica_a.emit(job, "status", {"state": "building_context"})

    seen_by_b = replica_b.get(job.id, org_id=ORG)
    assert seen_by_b is not None, "replica B could not see the job at all"
    assert seen_by_b.state == "RUNNING"
    assert replica_b.events(seen_by_b) == [
        {"event": "status", "data": {"state": "building_context"}}
    ]


def test_the_answer_reaches_a_replica_that_joined_late(redis_stores):
    """A client connecting after the work finished still gets the whole sequence."""
    replica_a, replica_b = redis_stores

    job = replica_a.create(org_id=ORG, user_id=None, kind="copilot_ask")
    replica_a.start(job)
    replica_a.emit(job, "status", {"state": "building_context"})
    replica_a.finish(job, {"message_id": "abc", "content": "Yes, ISO 27001."})

    late = replica_b.get(job.id, org_id=ORG)
    assert late is not None
    assert late.state == "DONE"
    assert late.result == {"message_id": "abc", "content": "Yes, ISO 27001."}
    # Every event replays, in order, ending with the terminal one.
    names = [item["event"] for item in replica_b.events(late)]
    assert names == ["status", "done"]


def test_a_failure_is_visible_across_replicas(redis_stores):
    replica_a, replica_b = redis_stores
    job = replica_a.create(org_id=ORG, user_id=None, kind="copilot_ask")
    replica_a.fail(job, "ProviderError: upstream refused")

    seen = replica_b.get(job.id, org_id=ORG)
    assert seen is not None
    assert seen.state == "FAILED"
    assert "upstream refused" in (seen.error or "")
    assert replica_b.events(seen)[-1]["event"] == "error"


def test_a_job_id_is_scoped_to_its_tenant(redis_stores):
    """A job id is a bearer token for whatever the job produced."""
    replica_a, replica_b = redis_stores
    job = replica_a.create(org_id=ORG, user_id=None, kind="copilot_ask")
    replica_a.finish(job, {"content": "tenant A's answer"})

    assert replica_b.get(job.id, org_id=uuid.uuid4()) is None


def test_waiting_returns_when_another_replica_finishes_the_work(redis_stores):
    replica_a, replica_b = redis_stores
    job = replica_a.create(org_id=ORG, user_id=None, kind="copilot_ask")
    replica_a.start(job)

    watcher = replica_b.get(job.id, org_id=ORG)
    assert watcher is not None
    assert replica_b.wait(watcher, 0.2) is False, "should not report done while running"

    replica_a.finish(job, {"content": "done"})
    assert replica_b.wait(watcher, 1.0) is True
    assert watcher.state == "DONE"


def test_the_in_process_store_is_explicitly_single_process():
    """Kept for single-process runs, and honest about what it cannot do.

    This test documents the limitation rather than pretending it away: two
    in-process stores share nothing, which is exactly why Redis is the default
    when it is available.
    """
    replica_a, replica_b = InProcessJobStore(), InProcessJobStore()
    job = replica_a.create(org_id=ORG, user_id=None, kind="copilot_ask")
    replica_a.finish(job, {"content": "x"})

    assert replica_a.get(job.id, org_id=ORG) is not None
    assert replica_b.get(job.id, org_id=ORG) is None


def test_the_in_process_store_still_works_for_one_process():
    store = InProcessJobStore()
    job = store.create(org_id=ORG, user_id=None, kind="copilot_ask")
    store.start(job)
    store.emit(job, "status", {"state": "building_context"})
    store.finish(job, {"content": "answer"})

    assert store.events(job)[0]["event"] == "status"
    assert store.events(job)[-1]["event"] == "done"
    assert store.wait(job, 0.1) is True
