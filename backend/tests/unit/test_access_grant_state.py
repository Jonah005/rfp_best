"""Correction 2: grant state is derived from expires_at/revoked_at, live."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from app.models import AccessGrant
from app.models.enums import GrantPermission, GrantResourceType, GrantState


def _grant(**overrides) -> AccessGrant:
    base = dict(
        org_id=uuid.uuid4(),
        grantee_user_id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        resource_type=GrantResourceType.EVIDENCE,
        resource_id=uuid.uuid4(),
        permission=GrantPermission.READ,
        reason="test",
        granted_by=uuid.uuid4(),
        granted_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    )
    base.update(overrides)
    return AccessGrant(**base)


def test_active_before_expiry():
    assert _grant().effective_state() is GrantState.ACTIVE


def test_expired_after_expiry_without_any_job_running():
    grant = _grant(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    # No sweep has run; the grant is nonetheless dead.
    assert grant.materialized_state is None
    assert grant.effective_state() is GrantState.EXPIRED
    assert grant.is_active() is False


def test_revocation_beats_a_future_expiry():
    grant = _grant(
        expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        revoked_at=datetime.now(timezone.utc),
    )
    assert grant.effective_state() is GrantState.REVOKED


def test_stale_materialized_state_is_ignored():
    """A stale cache must never resurrect a dead grant."""
    grant = _grant(expires_at=datetime.now(timezone.utc) - timedelta(hours=1))
    grant.materialized_state = GrantState.ACTIVE  # deliberately wrong
    assert grant.effective_state() is GrantState.EXPIRED


def test_expiry_boundary_is_exclusive():
    now = datetime.now(timezone.utc)
    assert _grant(expires_at=now).effective_state(now=now) is GrantState.EXPIRED
