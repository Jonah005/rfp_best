# BidReady — Phases M–P: Assembly, Validation, Release and Learning

Phases M–P are implemented and locked.

## Assembly

- A bid has a versioned proposal structure and ordered sections.
- Approved artifact versions map explicitly into sections; mandatory coverage is checked before
  assembly.
- Assembly produces real DOCX bytes, stores them in tenant-namespaced object storage, records a
  SHA-256 digest and byte size, and preserves the artifact-version generation trace.

## Package validation and release

- Package creation snapshots generated document versions into ordered items and an immutable
  manifest containing filename, version, size, digest, classification, approval and destination.
- Validation is deterministic and versioned. It checks missing files, corruption, unanswered
  artifacts, placeholders, unapproved commitments and evidence-backed claims without current,
  approved, reusable, unexpired evidence.
- Security and integrity findings such as corrupted artifacts are non-waivable. Other findings
  require an explicit, scoped, audited exception decision.
- Release uses maker-checker separation: the submitter cannot approve the same package.
- Released packages are immutable and may only transition to archived.

## Amendments and learning

- Amendment impact records the source document version, affected requirements, stale
  assessments, tasks moved to revalidation and artifacts marked for revalidation.
- Outcomes capture result, scores, award value, competitor, reason and evaluator feedback.
- Feedback creates governed knowledge-update candidates. Human review—not the model—decides
  whether a candidate becomes canonical company knowledge.

## Persistence and verification

Migration 0008 owns proposal structure, generated documents, packages, manifests, validation,
release exceptions, amendment impacts, outcomes and learning candidates with forced RLS.

`tests/integration/test_release_lifecycle.py` proves real DOCX assembly, checksums and
maker-checker release. `tests/integration/test_release_gates.py` covers the refusals:

| Gate | Proof |
|---|---|
| Waivability is data, not a code path | `test_a_non_waivable_finding_has_no_exception_path_even_through_sql` — the service refuses, and so does the `release_exception_waivable_guard` trigger when the insert is attempted in raw SQL |
| A placeholder blocks release but may be waived on the record | `test_a_placeholder_blocks_release_and_is_waivable` |
| Assembly is not approval | `test_an_unapproved_artifact_blocks_the_package` |
| A blocked package cannot be submitted | `test_a_blocked_package_cannot_be_submitted_for_release` |
| Requesting an exception is not receiving one | `test_a_waivable_finding_clears_only_when_the_exception_is_approved`, `test_a_rejected_exception_leaves_the_finding_open` |
| A released package is frozen | `test_a_released_package_is_frozen` |
| Approval follows submission | `test_an_unsubmitted_package_cannot_be_approved`, `test_only_a_released_package_can_be_archived` |
| Tenant isolation of the release trail | `test_findings_and_exceptions_do_not_cross_tenants` |

`tests/integration/test_amendment_and_learning.py` covers Phase P:

| Rule | Proof |
|---|---|
| An amendment makes approved work visibly stale | `test_an_amendment_makes_approved_work_visibly_stale` — assessment, task and approved artifact all move, found through the requirement's stable identity |
| STALE keeps the verdict it had | `test_a_stale_assessment_keeps_the_verdict_it_had` |
| Re-propagation is idempotent | `test_propagating_the_same_amendment_twice_is_idempotent` |
| A company document cannot drive a cascade | `test_a_company_document_cannot_drive_an_amendment_cascade` — the authority boundary at the last place it could be crossed |
| Feedback becomes a candidate, never a fact | `test_evaluator_feedback_becomes_a_candidate_not_a_fact`, `test_a_candidate_is_decided_once_and_a_rejection_writes_nothing` |
| The system invents no lesson | `test_an_outcome_without_feedback_proposes_nothing` |
| An outcome is recorded once | `test_an_outcome_is_recorded_once` |

`tests/integration/test_release_api.py` walks one bid from blank draft to released package over
HTTP only, and pins the end-of-line separations: a bid manager may submit a release but is
refused `release:approve`, a released package answers 409 rather than silently revalidating, and
the governed-learning queue is refused to whoever recorded the outcome.

Final repository gates are recorded in `DOCUMENTATION_AUDIT.md`.
