"""What counts as a live requirement.

`Requirement.lifecycle_state` had a WITHDRAWN value that nothing read. A cleanup
could set it on six duplicate rows, report success, and change nothing: the
export still listed them, the assessment still judged them, readiness still
counted them, and tasks were still generated for them. The column looked
authoritative and decided nothing -- the same defect this codebase has produced
in five other places, and the reason a duplicate-merge script was briefly a
no-op dressed as a fix.

It is one expression rather than a repeated `!=` so that a nineteenth query
cannot quietly omit it, and so the guard test has one name to look for.

Not applied everywhere on purpose. `_find_existing` must ignore withdrawn rows
when matching (it already does, separately), amendment history must still see
them, and an audit trail that hid them would defeat its own point. The rule is
narrower than "always": anything that decides what a PERSON sees, or what the
pipeline spends work on, filters here.
"""

from __future__ import annotations

from app.models import Requirement
from app.models.enums import RequirementLifecycle

# Usable directly in a .where(): `.where(*BID_SCOPE(ctx, bid_id), LIVE_REQUIREMENT)`
LIVE_REQUIREMENT = Requirement.lifecycle_state != RequirementLifecycle.WITHDRAWN
