# BidReady — Phase G: RFP Intelligence

Status: **implemented**. Sections 1-8 are the plan and acceptance contract agreed
before implementation; section 9 records the verification performed at close.

Phases A–F are locked. Phase G consumes the governed company knowledge layer
built in Phase F; it does not extend or reopen it.

## 1. The authority boundary this phase exists to protect

Phase G is where the product's central rule becomes executable code:

> Tender documents define what the customer asks for. Company documents prove
> what the bidder knows, has, did, or may safely reuse. Company evidence never
> creates tender scope.

Two of the four systemic risks from the original evaluation are Phase G's
responsibility. Risk 1 — historical company proposal scope contaminating a new
tender — is prevented structurally: requirement extraction reads only
`side = TENDER` content, and no company-side chunk is admissible to any
extraction call in this phase. Risk 4 — a contradictory tender not triggering a
strong enough gate before drafting — is the Scope Confidence gate plus the
ambiguity engine.

Risks 2 and 3 (retrieval selecting unrelated material; historical evidence read
as current) belong to Phase F's retrieval and currentness model, already built,
and to Phase H's assessment.

## 2. Scope

**In scope**

1. Opportunity creation and promotion to a Bid.
2. Tender document intake, reusing the Phase F ingestion pipeline with
   `side = TENDER` and a mandatory `bid_id`.
3. Tender document classification into `document_type`.
4. Per-clause precedence resolution producing `ClauseResolution`.
5. Tender fact extraction (customer, deadline, contacts, submission rules,
   evaluation criteria).
6. Atomic requirement extraction from **winning clauses only**, with a
   deterministic structural coverage cross-check.
7. Requirement classification: category, mandatory, scored, evaluation weight,
   risk, recommended department.
8. Conditional requirements: condition extraction, evaluation, and applicability.
9. Ambiguity engine producing `ClarificationIssue`.
10. Deliverable register.
11. Scope Confidence, computed from tender clarity only.
12. API routes and a tenant-aware analysis worker task.

**Explicitly out of scope** — these are Phase H and later, and no part of them is
to be represented as implemented: requirement-to-evidence assessment, matrices,
bid readiness, bid/no-bid decision records, department assignment execution,
task creation, copilot, artifacts, assembly, validation, release.

## 3. Entities added

| Entity | Purpose |
|---|---|
| `opportunity` | Pre-bid commercial context: customer, tender name and reference, owners, deadline, value |
| `bid` | The execution record; owns tender documents, requirements and analysis |
| `clause_resolution` | The per-clause precedence outcome: winner, competitors, method, confidence, rationale |
| `requirement` | Stable identity: code, category, mandatory/scored, applicability, thin lifecycle |
| `requirement_version` | The wording, its source locator, and the resolution it came from |
| `clarification_issue` | A detected tender defect, with both conflicting sources |
| `clarification_requirement` | Join: which requirements an issue blocks |
| `deliverable` | A required submission output |
| `deliverable_requirement` | Join: which requirements a deliverable satisfies |
| `scope_confidence` | The tender-clarity gate and its per-category detail |

`document.bid_id` gains a composite `(bid_id, org_id)` foreign key now that
`bid` exists. Phase F could not declare it because the target table did not
exist yet; leaving it unconstrained would be the one place a tender document
could point at another tenant's bid.

## 4. Model call types added

Each is registered in the gateway with its own prompt, schema and version. All
tender content is wrapped as untrusted data. No call receives company-side text.

| Call type | Returns |
|---|---|
| `precedence_hint_extraction` | Customer-stated precedence rules and affected clause references |
| `tender_fact_extraction` | Customer, reference, deadline, contacts, submission rules, evaluation criteria |
| `requirement_decomposition` | Atomic requirements with source clause and mandatory/scored signals |
| `requirement_classification` | Category, subcategory, risk, recommended department |
| `condition_extraction` | A structured condition expression for conditional requirements |
| `ambiguity_analysis` | Detected contradictions, blanks, placeholders and conflicting instructions |
| `deliverable_extraction` | Required outputs with format, filename and template signals |

## 5. Precedence resolution

Precedence is **resolved**, never assumed. Inputs per document version: issue
date, version label, document type, explicit `DocumentRelation` supersession,
affected clause references, and any customer-stated precedence rule extracted
from the tender itself.

Resolution order, highest authority first:

1. `EXPLICIT_SUPERSESSION` — a `DocumentRelation` naming the clause.
2. `CUSTOMER_STATED_RULE` — the tender's own precedence statement.
3. `ISSUE_DATE` — the later issue date wins.
4. `VERSION_LABEL` — a higher version label wins.
5. `DOCUMENT_TYPE_DEFAULT` — the weakest signal, used only when nothing else
   discriminates.

Every resolution records its method, confidence and rationale. A resolution
below the confidence threshold, or one where the top two candidates are
indistinguishable, does not silently pick a winner: it raises a
`PRECEDENCE_UNRESOLVED` clarification issue and the affected requirements are
flagged for client clarification.

A later clarification can therefore beat an earlier amendment on one clause
while the amendment still governs another — which is exactly the case the
hardcoded `amendment > clarification > original` ordering got wrong.

## 6. Structural coverage cross-check

Requirement recall is the worst failure mode in the product, so extraction is
never a single model pass. After decomposition, a deterministic pass walks every
clause-bearing block of every winning document version and asserts that each one
either produced at least one requirement or carries an explicit
no-requirement decision. Uncovered clauses are reported as a coverage gap and
counted against Scope Confidence; they are never silently dropped.

## 7. Scope Confidence — tender clarity only

Inputs, all measurements of the tender:

- clause coverage ratio from the cross-check;
- unresolved `clause_resolution` count;
- blocking and major `clarification_issue` counts;
- contradiction count;
- proportion of requirements whose source text is a placeholder or blank;
- presence of the tender's own structural essentials (deadline, submission
  instructions, evaluation criteria).

Company evidence availability is **not** an input and must not become one. That
question is about us, not about the tender, and belongs to Phase H readiness.
Conflating them would let a well-understood tender we are poorly positioned for
masquerade as an unclear tender, and would block drafting for the wrong reason.

`HIGH` permits detailed drafting; `MEDIUM` permits conditional drafting with
recorded assumptions; `LOW` blocks detailed solution generation and directs
effort at clarification questions instead.

## 8. Acceptance criteria

Phase G is complete when all of the following hold, each backed by a test.

**Authority boundary**

1. Requirement extraction reads only `side = TENDER` chunks; a company document
   in the same organization contributes nothing to any requirement.
2. A company-side document cannot be attached to a bid, and a tender document
   cannot exist without one.
3. No Phase G model call receives company-side content.

**Precedence**

4. An explicit supersession beats a later issue date.
5. A clarification issued after an amendment wins that clause, while the
   amendment still wins a clause the clarification does not touch.
6. Two indistinguishable candidates produce no winner and raise a
   `PRECEDENCE_UNRESOLVED` clarification issue.
7. A human override records actor, reason and supersedes the prior resolution.

**Requirements**

8. Requirements are extracted only from winning clause text.
9. A superseded clause produces no requirement.
10. The coverage cross-check detects a clause block that produced nothing.
11. An amendment changing wording creates version N+1, marks N superseded, and
    leaves the requirement itself `CONFIRMED` with the same identity and code.
12. A conditional requirement whose condition evaluates false is
    `NOT_APPLICABLE` and is excluded from gap counts.
13. Malformed or out-of-batch model output is rejected and nothing is written.

**Ambiguity and scope confidence**

14. Contradictory quantities in two documents produce a clarification issue
    citing both sources.
15. Scope Confidence is unchanged when company evidence is added or removed.
16. `LOW` confidence blocks detailed drafting and records the blocking reasons.

**Platform invariants**

17. Every new table has forced RLS and rejects a cross-tenant relationship.
18. Every state change writes an audit event in the same transaction.
19. Analysis runs in a tenant-aware worker through domain services.
20. Full suite, Ruff, and mypy pass; `openapi.json` regenerated.

## 9. Verification

### 9.1 Result

- **217 tests pass**, warnings treated as errors, zero skips (159 before this
  phase; 58 added).
- Ruff clean across `app`, `tests` and `alembic`; migrations were previously
  outside the lint target and are now inside it.
- mypy clean across 65 source files.
- Migration chain `0001 → 0002 → 0003 → 0004` applies to a fresh database and
  downgrades to base leaving only `alembic_version`.
- 37 tables, 34 with forced RLS; `openapi.json` regenerated at 49 paths and 55
  operations.
- All six Celery tasks register at worker startup, including
  `analyze_tender`.

### 9.2 Acceptance criteria, and where each is proven

| # | Criterion | Test |
|---|---|---|
| 1 | Extraction reads tender-side content only | `test_company_content_is_never_eligible_for_requirement_extraction` |
| 2 | Company documents cannot join a bid; tender documents must | `test_a_company_document_cannot_be_attached_to_a_bid`, plus the `ck_document_side_bid` check asserted in the migration harness |
| 3 | No Phase G prompt carries company text | `test_tender_prompts_never_carry_company_text` |
| 4 | Explicit supersession beats a later issue date | `test_explicit_supersession_beats_a_later_issue_date`, `test_explicit_supersession_overrides_dates` |
| 5 | A later clarification and an earlier amendment each win their own clause | `test_a_clarification_and_an_amendment_each_win_their_own_clause` |
| 6 | Indistinguishable candidates raise `PRECEDENCE_UNRESOLVED` | `test_indistinguishable_candidates_produce_no_winner`, `test_an_unresolvable_clause_raises_a_blocking_clarification` |
| 7 | Overrides record actor and reason and supersede | `test_a_human_override_supersedes_and_is_audited` |
| 8 | Requirements come only from winning clause text | `test_requirements_are_extracted_only_from_winning_clauses` |
| 9 | A superseded clause produces no requirement | same test: every clause-4.2 version points at the amendment |
| 10 | The coverage check catches an unreported block | `test_the_coverage_check_catches_a_block_the_model_ignored` |
| 11 | An amendment adds a version and keeps identity | `test_an_amendment_adds_a_version_and_keeps_the_requirement_identity` |
| 12 | A false condition yields `NOT_APPLICABLE`, excluded from the active set | `test_a_false_condition_marks_a_requirement_not_applicable` |
| 13 | Out-of-batch model output is rejected, nothing written | `test_out_of_batch_ordinals_are_rejected_and_nothing_is_written` |
| 14 | Contradictions cite both sources | `test_a_contradiction_cites_both_sources` |
| 15 | Scope Confidence is unchanged by company evidence | `test_scope_confidence_ignores_company_knowledge`, `test_grading_never_reads_company_knowledge` |
| 16 | `LOW` blocks detailed drafting and records reasons | `test_scope_confidence.py` grading suite |
| 17 | New tables have forced RLS and reject cross-tenant rows | migration harness, `test_tender_intelligence_does_not_leak_across_tenants` |
| 18 | State changes are audited in the same transaction | `test_the_full_pass_produces_a_scope_confidence_and_audits_itself`, `test_bid_state_transitions_are_guarded_and_audited` |
| 19 | Analysis runs in a tenant-aware worker | `analyze_tender` registration test; the task goes through `tender_analysis.analyze_bid` |
| 20 | Suite, Ruff, mypy pass; OpenAPI regenerated | above |

### 9.3 Decisions taken during implementation

**Precedence declines rather than guesses.** When no signal discriminates, the
resolver writes a resolution with no winner and raises a blocking clarification.
Requirement extraction then skips that clause entirely, because a requirement
built on an unresolved clause would look answered while resting on text that may
not govern.

**Document type is a tie-breaker of last resort, not the rule.** It carries the
lowest confidence of any method, and a type-only decision is asserted in test to
stay below the threshold at which the product would treat it as settled.

**The `document.bid_id` foreign key was added now.** Phase F could not declare it
because `bid` did not exist. Leaving it would have been the one place a tender
document could reference another tenant's bid. A `CHECK` was added alongside it
so the tender/company boundary is a schema guarantee rather than a service
convention, and the migration refuses to run if any orphaned row exists.

**Conditions that cannot be evaluated stay pending.** Guessing `ACTIVE` invents
work; guessing `NOT_APPLICABLE` hides an obligation. A non-numeric comparison
returns undecided rather than false, for the same reason.

**Clarification findings are fingerprinted.** Re-running analysis updates an
existing issue instead of appending a duplicate, and an issue a human has already
sent to the client is never silently rewritten by a later automatic run.

### 9.4 Defect found and fixed outside the phase scope

Alembic's `set_main_option` stores values in a `ConfigParser` that performs `%`
interpolation, so any connection string containing a literal `%` — a
percent-encoded socket path, or simply a password containing `%` after URL
encoding — raised at read time. `escape_for_alembic_config` now escapes the
value in `alembic/env.py` and in the migration test harness, with a regression
test in `tests/unit/test_security.py`.

## 10. Next: Phase H — Assessment and Bid Readiness

Requirement-to-evidence assessment (`RequirementAssessment` plus
`AssessmentEvidence`), eligibility and technical compliance matrices, the
submission checklist, the two-dimension readiness model, bid/no-bid support, and
department assignment recommendations. It consumes Phase F's governed knowledge
and Phase G's requirement register; it is the first phase where the two sides
meet, and the authority boundary matters most there.
