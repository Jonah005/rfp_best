"""Foundation background tasks.

Each one goes through a domain service. None of them touches a tenant table
directly, and none of them bypasses audit.
"""

from __future__ import annotations

from app.core.logging import get_logger
from app.services import access_grants
from app.workers.celery_app import tenant_task

log = get_logger("worker.tasks")


@tenant_task("app.workers.tasks.sweep_grant_states")
def sweep_grant_states(ctx, session) -> dict:
    """Materialize grant states for listing screens.

    This is a convenience only. Authorization always recomputes effective state
    from expires_at/revoked_at, so a grant is never live because this job has
    not run yet, and never dead because it has.
    """
    changed = access_grants.materialize_states(session, ctx)
    return {"org_id": str(ctx.org_id), "updated": changed}


@tenant_task("app.workers.tasks.revoke_task_grants")
def revoke_task_grants(ctx, session, *, task_id: str, reason: str) -> dict:
    """Called when a task completes or its bid closes."""
    import uuid

    count = access_grants.revoke_for_task(session, ctx, task_id=uuid.UUID(task_id), reason=reason)
    return {"revoked": count}


@tenant_task("app.workers.tasks.health_probe")
def health_probe(ctx, session) -> dict:
    """Proves the worker path end to end: broker, tenant context, database."""
    from app.db.session import current_tenant

    return {"tenant_in_transaction": str(current_tenant(session))}


@tenant_task("app.workers.tasks.parse_and_index_document")
def parse_and_index_document(ctx, session, *, version_id: str) -> dict:
    """Parse, chunk, embed, and extract governed company knowledge.

    Runs in a worker because parsing a large PDF and embedding a few hundred
    chunks takes far longer than a request should. It goes through the same
    ingestion service the API would call, so RLS, audit and state guards apply
    identically.
    """
    import uuid

    from app.services import ingestion

    version = ingestion.parse_and_index(session, ctx, version_id=uuid.UUID(version_id))
    return {
        "version_id": str(version.id),
        "state": version.state.value,
        "blocks": version.block_count,
        "chunks": version.chunk_count,
    }


@tenant_task("app.workers.tasks.expire_stale_knowledge")
def expire_stale_knowledge(ctx, session) -> dict:
    """Flip facts, relations and evidence past their validity window to EXPIRED.

    This is what makes an expired certificate automatically invalidate any claim
    that rests on it, rather than relying on someone remembering.
    """
    from app.services import knowledge

    return knowledge.expire_stale(session, ctx)


@tenant_task("app.workers.tasks.analyze_tender")
def analyze_tender(ctx, session, *, bid_id: str) -> dict:
    """Run the full tender-understanding pass for a bid.

    Precedence, facts, requirements, deliverables, ambiguity and the scope
    gate are many bounded model calls over a whole tender package, so this
    belongs in a worker. It goes through the same domain services the API route
    uses, so RLS, audit and the tender/company authority boundary apply
    identically.
    """
    import uuid

    from app.services import tender_analysis

    result = tender_analysis.analyze_bid(session, ctx, bid_id=uuid.UUID(bid_id))
    return result.as_dict()
