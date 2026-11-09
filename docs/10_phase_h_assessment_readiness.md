# BidReady — Phase H: Assessment and Bid Readiness

Status: **implemented**. Sections 1–7 are the plan and acceptance contract;
section 8 records the verification performed at close.

Phases A–G are locked. Phase H is the first phase where the two halves of the
system meet: requirements extracted from the tender (Phase G) are judged against
governed company knowledge (Phase F). It reads both and writes to neither.

## 1. The rule this phase exists to protect

> `evidence_status` says what the proof shows. `bid_readiness` says what has to
> happen next. They are separate fields and they are never collapsed.

This is risk 3 from the original evaluation — historical evidence read as
current — made structurally impossible to hide. A single "status" column forces
a choice between reporting *we hold an ISO 27001 certificate* and *that
certificate expired in March*, and whichever is chosen, the other disappears.
Two axes report both, and the pair is what a reviewer acts on.

The second rule is direction of travel. An assessment reads company knowledge to
judge a requirement. It never lets that knowledge alter the requirement. Nothing
in `app/services/assessment.py` writes to `requirement` or
`requirement_version`, and the evidence query is company-side only.

The third rule is that the verdict is arithmetic. `_evidence_status` and
`_readiness` are pure functions over evidence status, expiry, approval and
requirement metadata. The model contributes retrieval candidates and prose; it
never contributes the verdict.

## 2. Scope

In: requirement assessment with evidence attachment and reasoning trace;
assessment versioning and human override; the four review matrices; the bid
readiness summary with drill-through on every figure; bid/no-bid recommendation
and recorded decision; department recommendation.

Out: task creation and assignment (Phase I/K), drafting (Phase J/L), proposal
assembly (Phase M–O). Phase H tells a bid manager what state the bid is in; it
does not act on that state.

## 3. Entities added

Migration `0005_assessment_readiness`.

| Table | Purpose |
| --- | --- |
| `requirement_assessment` | One verdict per requirement per version. `is_current` is enforced unique by a partial index, so "the current verdict" is a database guarantee. |
| `assessment_evidence` | Which evidence backed a verdict, in what role, at what rank and weight. This is what makes a verdict defensible in a review. |
| `bid_no_bid_decision` | A recorded human decision, its rationale, and the recommendation it agreed with or overrode. |

Enums added: `EvidenceStatus`, `BidReadiness`, `AssessmentState`,
`AssessmentEvidenceRole`, `BidNoBidResult`.

All three tables carry composite `(id, org_id)` foreign keys and RLS
`ENABLE` + `FORCE`, like every other tenant table.

## 4. The two axes

`EvidenceStatus` — what the proof shows:
`NONE`, `EXPIRED`, `HISTORICAL_VERIFIED`, `PARTIAL`, `CURRENT_VERIFIED`,
`CONFLICTING`.

`BidReadiness` — what must happen next:
`BLOCKED`, `NEEDS_CLIENT_CLARIFICATION`, `NEEDS_COMPANY_CONFIRMATION`,
`NEEDS_REFRESH`, `REQUIRES_SIGNATURE`, `DRAFTABLE`, `READY`, `NOT_APPLICABLE`.

The cases where they disagree are the ones that justify the design:

- Current, approved evidence on a submission form is `CURRENT_VERIFIED` +
  `REQUIRES_SIGNATURE`. Fully evidenced, not answerable.
- An expired certificate is `EXPIRED` + `NEEDS_REFRESH`, never `BLOCKED`. There
  *is* proof; it needs renewing, and calling it blocked loses that.
- Perfect evidence under an open clarification is `CURRENT_VERIFIED` +
  `NEEDS_CLIENT_CLARIFICATION`. We have the proof and still do not know what was
  asked.

Only evidence a knowledge administrator has approved for reuse is eligible.
Unapproved material may be perfectly true and must still not silently back a
customer-facing claim.

## 5. Versioning and override

Reassessment supersedes; it does not overwrite. Each run writes a new
`requirement_assessment` row with `version = previous + 1` and flips the previous
row's `is_current` to false, so the history of what the system believed, and
when, stays readable.

A human may override either axis through `PATCH`, and a reason is mandatory —
the service refuses a blank one. The row moves to `HUMAN_ADJUSTED` and records
who and why; the audit event carries the before and after.

`mark_stale` flags a verdict for recomputation (after an amendment, say) without
discarding it. `STALE` means "this was our verdict and it may no longer hold",
which is more useful to a reviewer than an empty field.

## 6. Readiness, and the rule that every number opens

Every figure on the readiness screen has a drill-through route returning the
requirements behind it. A number a user cannot open is a number they cannot act
on, and an unopenable percentage is exactly the kind of figure that gets quoted
in a management meeting and later turns out to have been wrong.

`NOT_APPLICABLE` requirements are removed from the coverage denominator
entirely: a conditional requirement that does not apply is not a gap to chase,
and leaving it in the denominator permanently caps coverage below 100%.

The four matrices — eligibility, technical, submission, contract register —
partition requirements by category and carry each row's clause citation and
evidence ids.

## 7. Bid / no-bid

`recommend_bid_no_bid` is a recommendation, not a decision. It aggregates
coverage, critical blockers, unready mandatory requirements, open and blocking
clarifications, blocked eligibility requirements and Phase G's Scope Confidence
into a RED / AMBER / GREEN with the factors that produced it.

Management decides. `record_bid_no_bid` stores the human result alongside the
recommendation it agreed with or overrode, and the audit event flags
`overrode_recommendation` explicitly — a human overriding a RED recommendation
is the decision most likely to be revisited after an outcome.

## 8. Verification

### 8.1 Result

- 259 tests pass (up from 217 at Phase G close). Ruff and mypy clean.
- Migration chain 0001→0005 applies and downgrades cleanly against a fresh
  database, as the owner, with RLS asserted as the constrained app role.
- OpenAPI surface: 62 paths (up from 49).

### 8.2 Where each rule is proven

| Rule | Proof |
| --- | --- |
| The two axes never collapse | `tests/unit/test_assessment_axes.py` — expired and historical evidence never read as current; submission + current evidence still requires a signature; an open clarification outranks perfect evidence. |
| Every evidence status maps to a readiness | `test_every_evidence_status_maps_to_a_readiness` — no status may fall through to an implicit default. |
| Only governed evidence counts | `test_unapproved_evidence_never_backs_an_assessment` — pending evidence yields `NONE`, not "we found something". |
| Reassessment supersedes | `test_reassessment_supersedes_rather_than_overwrites`, plus `test_only_one_assessment_per_requirement_can_be_current`, which proves the partial unique index rather than trusting service discipline. |
| Override is auditable | `test_a_human_override_records_who_and_why` — blank reasons refused, before/after captured in `audit_event`. |
| Every number opens | `test_every_dashboard_number_opens_into_its_requirements` — each drill-through returns exactly the count the summary reported; an unknown metric is a problem document, not a crash. |
| Human may overrule the recommendation | `test_a_human_may_overrule_the_recommendation_and_it_is_recorded` — `overrode_recommendation` is true in the audit event. |
| Tenant isolation | `test_an_assessment_cannot_be_read_across_tenants`, run as the constrained role. |
| The HTTP surface is complete | `tests/integration/test_assessment_api.py` — the full picture is reachable without a database client, RBAC refuses `assessment:write` to an auditor, and another tenant's token sees nothing. |

### 8.3 Decisions taken during implementation

**Candidate retrieval is lexical for now.** `_candidates_for` scores evidence by
term overlap. This is deliberately simple: Phase H's contract is about what the
engine concludes *from* a candidate set, and the retrieval that builds that set
is Phase F's concern and will be replaced by the embedding path. The tests are
written so that improving retrieval cannot silently change what the assessment
arithmetic asserts.

**Department recommendation never overwrites.** `recommend_departments` only
fills `recommended_department_id` where it is null, so re-running it cannot undo
a bid manager's own choice. It is a suggestion, and nothing downstream assumes
it.

### 8.4 Defect found and fixed outside the phase scope

The integration fixture `seeded_orgs` did not depend on `app_engine`, the
fixture that builds the schema. Nothing had failed yet because pytest happened
to order them favourably, but a new test module requesting `seeded_orgs` before
anything requested `app_engine` failed with `relation "organization" does not
exist`. Fixed by naming the dependency. The shared API fixtures (`api`,
`seeded_users`, `api_login`) moved from `test_api.py` into `conftest.py` at the
same time, so more than one module can use them.

## 9. Next: Phase I + K — Tasks, participants and cross-department dependencies
