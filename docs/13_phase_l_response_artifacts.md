# BidReady — Phase L: Response Artifacts, Drafting and Approval

Phase L is implemented and locked.

## Delivered behavior

- Every task owns a continuous `ResponseArtifact`; task creation creates artifact version 1.
- A working response is the current DRAFT `ArtifactVersion`, never a parallel draft object.
- Statements are typed as evidence-backed fact, tender-derived text, proposed commitment,
  assumption, or human input. Their backing rules are validated before persistence.
- The registered structured `draft_statement_generation` DeepSeek call receives only the
  task's requirements and current, approved, reusable, unexpired evidence.
- Model results are validated, provenance-tagged, and persisted through the same governed
  service used by human edits. The model never writes authoritative rows directly.
- Evidence-backed statements create `EvidenceLink` records targeting the exact artifact
  statement. Proposed commitments and assumptions enter their own review gates.
- Submission creates an ordered role-based approval chain. Rejection returns the artifact to
  rework; editing after review creates a new version; final approval makes the approved version
  immutable.
- Approved artifacts cannot complete while proposed commitments remain unapproved.
- Reviews, approval decisions, assumption decisions and material state changes are audited.

## Persistence and contract

Migration 0007 owns response artifacts, versions, statements, reviews, approvals,
commitments and assumptions, including forced RLS and tenant-safe evidence-link targets.
The implemented API covers task artifacts, versions, structured draft generation,
submission, ordered approvals, reviews, commitments and assumptions.

## Verification

`tests/integration/test_artifact_lifecycle.py` proves the continuous artifact, the immutable
approved version, commitment gating and registered structured drafting.
`tests/integration/test_statement_typing.py` covers the typing rules themselves, from both
sides of the boundary:

| Rule | Proof |
|---|---|
| Typed statements are enforced *below* the service | `test_the_typing_rules_are_enforced_below_the_service` — raw SQL inserts are rejected by `chk_tender_derived_has_locator`, `chk_commitment_linked`, `chk_assumption_linked` and `chk_human_input_attributed`. This is the test that matters when a second write path is added later and forgets the validation. |
| Tender-derived text is traceable | `test_a_tender_derived_statement_must_say_where_it_came_from` |
| There is no untyped statement | `test_an_unknown_statement_class_is_refused`, `test_a_blank_statement_is_refused` |
| A promise becomes an approvable object | `test_a_proposed_commitment_creates_a_commitment_needing_approval` |
| An assumption can be carried into the submission register | `test_an_assumption_creates_a_register_entry_that_can_be_carried` |
| A version under review cannot be edited | `test_a_version_under_review_cannot_be_edited` |
| An approved version keeps its exact text forever | `test_an_approved_version_keeps_its_text_when_a_new_draft_starts` |
| Re-saving a draft replaces rather than accumulates | `test_replacing_a_draft_does_not_leave_orphaned_statements` |
| Nothing is approved merely by being written | `test_statements_start_unapproved` |
| Tenant isolation | `test_statements_and_artifacts_do_not_cross_tenants`, `test_commitments_and_assumptions_are_scoped_to_their_bid` |

The HTTP surface is exercised end to end in `tests/integration/test_release_api.py`, which
drives draft → submit → approve entirely through the API and confirms an auditor may read the
artifact trail but not write to it.

The final repository verification is recorded in `DOCUMENTATION_AUDIT.md`.
