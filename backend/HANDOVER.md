# BidReady — handover

Project: `D:\rfpproject` (Windows). Backend FastAPI + PostgreSQL 18 with RLS FORCE.
Test bid: Jadwa RFP, `0e20677e-9cc3-434e-ad88-c953d6676028`.

Paste this into a new session as the opening message.

---

## Constraints that do not change

- **No hardcoded credentials.** The DeepSeek key comes from `DEEPSEEK_API_KEY` only.
- **All model calls go through the model gateway**, as named call types with a
  prompt version. `detected_by` / `extracted_by` / `equivalence_by` carry
  `llm:<name>@<version>`.
- **The model never decides**: permissions, task states, approvals, evidence
  access, workflow transitions, versioning, release, audit logs, file isolation,
  mandatory blockers, identity, tenancy. Those are code. The model judges
  meaning; code records the verdict with a version stamp.
- **Uploaded documents are untrusted.** Document text must never be able to
  override platform instructions.
- Not a generic RAG chatbot. The two axes — `evidence_status` and
  `bid_readiness` — stay orthogonal and are never collapsed.

## Working rules that earned their place

1. **Measure before changing.** Every wrong diagnosis in this project has been
   corrected by pasted output, never by re-reading the code. Write a read-only
   diagnostic, run it, then change code.
2. **The asymmetry rule.** A duplicate row is visible and a person can merge it.
   A merged or dropped obligation is unrecoverable and nothing downstream shows
   it is gone. When in doubt, keep both.
3. **Fixtures come from the database, not from imagination.** Two tests once
   passed against invented text while the bug they covered was untouched.
4. **Beware the optimisation that becomes a silent skip.** Five separate bugs
   have had this shape — a numbered-clause exemption, a decomposition fallback,
   a DELETE with no tenant context, a retirement rule, a 25-character minimum
   that deleted "Working days". Any cache, exemption, threshold or default
   argument deserves the question "what does this hide when it is wrong?"
5. **Diagnostics must not reimplement their subject**, and must be updated in
   the same breath as the thing they measure. Two checkers reported pre-fix
   numbers after the fix landed because they did not know what a restatement
   was.
6. **Absence is only evidence when the reader was complete.** A model capped at
   N results per batch that returns N has not finished; its silence means
   nothing. This cost 44 real customer questions before it was caught.

## Current state (verified from exports, 14 Sep, end of day)

| | |
|---|---:|
| Requirements | 89 (extractor v9) |
| BLOCKED | 59 |
| NEEDS_CLIENT_CLARIFICATION | 21 |
| NEEDS_COMPANY_CONFIRMATION | 9 |
| DRAFTABLE / READY | 0 (re-measure: see below) |
| Distinct open customer questions | 38 |
| Duplicate questions | 0 open |
| Open COVERAGE_GAP findings | 0 |
| Stale requirement references | 0 |
| Clarification links | 417, all resolving |
| Evidence rows | 610, of which **304 hang off withdrawn documents** |
| Evidence marked not substantiated | 235 (`markers@v1`); 375 unjudged |
| Deliverables | 14 canonical, 18 linked duplicates, 0 NOT_APPLICABLE |
| Tests | **1131** expected (1130 + 1 skip without reportlab); 1123/1 verified on Windows 20 Sep before the 7 below |

Latest migration: `0024_requirement_confirmation`.
Decomposition prompt: `requirement_decomposition@v9`.

**The readiness numbers above predate two changes and are stale.** Evidence
retrieval now excludes withdrawn documents, and 235 rows are marked not
substantiated. Re-run the assessment and export before trusting any of them.

## Read this before planning

The clarification phase is finished and measured. Evidence is the live work.

**Two axes on evidence, and they are not the same question.**

    evidence_type      what KIND of material this is
    is_substantiated   whether the material exists yet   NULL = nobody judged

`NOT_SELF_EVIDENCING` downgrades whole TYPES -- a blank form, a pricing
template, a customer's old tender -- and cannot reach the case this corpus is
full of: a certification-register row that is genuinely about ISO 27001 and
reads `Certificate no.: [REPLACE] | Evidence status: NOT EVIDENCED`. Typing that
CERTIFICATE is correct and makes it read as proof; typing it FORM to force a
downgrade would be false and would drop "ISO 27001" out of every shortlist that
should surface it. Hence the second column. It is NULL by default on purpose --
a `true` default would make an unjudged row indistinguishable from a judged-good
one.

**The company pack is 14 registers, not 14 documents.** One file holds trade
licences, incorporation certificates, tax registration and signatory authority.
So the unit that carries a type is the EVIDENCE ROW, not the document version. A
document-level classifier would be confidently wrong on most rows.

**The pack labels its own status**, which is why the floor is free:

    Evidence status: NOT EVIDENCED      Value: [REPLACE FROM OFFICIAL RECORD]
    Status / owner: MISSING             Current status: [HR TO CONFIRM]
    SYNTHETIC / NOT AN OFFER            Notes: Demo value
    Status: TEMPLATE BELOW              DRAFT FOR APPROVAL

`app/services/evidence_markers.py` reads those. It is code, not a model call,
for three reasons: the downgrade direction is the safe one, a regex cannot be
argued out of a downgrade by the document it is reading (the untrusted-documents
constraint, enforced rather than requested), and a free auditable pass over 610
rows is worth having before a paid one. **It can only ever downgrade.** There is
no path through it that concludes an excerpt IS proof, and a test asserts that.

**A rule that was removed after one dry run, and must not come back.**
"Historical proposal claim; verify current status before external use" was a
marker. It matched 41 rows including the company profile and service lines --
the drafter's best material. Those are backed by a real 2015 proposal: what is
in doubt is CURRENCY, not existence. `EvidenceStatus` already separates
HISTORICAL_VERIFIED from CURRENT_VERIFIED. Age and absence are two axes;
folding one into the other is the same error as collapsing evidence_status into
bid_readiness.

## What is finished

The clarification pipeline. It went from invisible (zero links, no row ever
reported a question) through over-linking (1,317 links, every row flagged) to
correct:

- Links are **exact** — every finding reaches precisely the requirements of the
  blocks it cites; measured, not assumed.
- Fragmented requirements were coalesced (129 → 104), which removed the fan-out
  at its source: a block held nine rows where it holds three.
- Duplicate questions are judged by `question_equivalence@v1` — "would one reply
  from the customer answer both?" — and marked, not deleted.
- An open question **demotes** a row rather than overruling it. It yields to
  BLOCKED and NEEDS_REFRESH, the states meaning we still owe proof.
- Rationales say a question "has been identified and not yet sent", because no
  request record exists.

## Fix the pattern, not the instance -- it happened again

`Document.withdrawn_at.is_(None)` was applied in `precedence.py`,
`requirements.py`, `tender_analysis.py` and the knowledge API.
`assessment._admissible()` is the fifth place that needs it and did not have it,
so **every evidence shortlist drew from a pool that was ~50% retracted
material** -- 304 of 610 rows. A withdrawn duplicate scored as well as its live
twin and rendered identically to a drafter. Fixed, with the predicate asserted
against compiled SQL rather than against the function's source.

**A wrong call worth recording.** The 28 document records for 14 files were
called a probable ingestion bug and put at the top of the backlog. They are not.
The withdrawal reason says so in the export: *"Superseded by a newer upload of
the same document."* Each file was uploaded twice and
`retire_duplicate_documents.py` correctly retired the older record. The
duplication was right; only the code reading past it was wrong. The lesson is
the project's own first rule -- the answer was one column away in the data, and
a diagnostic that printed the withdrawal REASON instead of a count would have
settled it immediately.

## Lesson that cost two rounds: fix the pattern, not the instance

Withdrawing a requirement without moving its clarification links produced 25
dangling requirement codes in an export. It was fixed in
`coalesce_enumerated_fragments.py`, and `retire_superseded_extractions.py` --
which does the same thing -- was not looked at, so the identical failure
reappeared a day later with 130 occurrences. The move helper now lives in the
retire script and is imported by the repair script rather than written twice.

**Any script that withdraws a requirement must move its clarification links and
record `superseded_by_requirement_id`.** The same applies to deliverables, which
have their own join table.

## Done: obligations the extractor dropped

Five obligations are in the tender and absent from the matrix:

```
Back Office Checker                    role 6 of 8
Working days                           a timing parameter
"Get applicant related more information"   Al-Elm purpose 2 of 2, missing from R-018
"Develop engine to prompty send SMS"   missing from R-115
"Qoute Separate"                       the separate-pricing instruction
```

`scripts/check_uncovered_obligations.py` finds them two ways: a span check with
a coverage threshold, and a **word diff** with no threshold at all — the words a
block uses that no requirement from it repeats. Trust the word diff; the span
check has been mis-calibrated three times.

**The pattern**: every one of these sits in a clause shaped *lead-in + bullets*,
where the model produced ONE requirement carrying the first bullet and dropped
the rest. It is the exact mirror of the v6 fragmentation bug (one row per
bullet, lead-in copied onto each). The same clause shape breaks both ways.

All five are now extracted, and by the extractor rather than by hand:
Back Office Checker is in R-010, Working days in R-011, the second Al-Elm
purpose in R-018, the SMS engine is R-157.

`scripts/preview_decomposition.py` is the harness that made this safe -- it runs
the registered prompt against stored blocks and prints KEEPS / NEW / ABSORBED /
LOST per block, writing nothing. **Use it before every prompt change.** It
caught v7 emitting subjectless rows and v8 attributing bidder duties to the
customer, neither of which would have been visible until an export.

Three prompt versions in one sitting (v7, v8, v9) is the honest record. v7 and
v8 each fixed one half of the lead-in/bullets problem and broke the other, and
the oscillation is evidence of a structural limit: **one prompt cannot reliably
hold both "group these items" and "split that one, with its own actor".** The
architecture that would work is the one already used for `obligation_owner` --
let decomposition split generously, then a second small call decides grouping
and actor per item with its own version stamp. That is a design change and it is
the right next move on extraction quality, not a tenth prompt revision.

Known and accepted: the SMS block yields terse rows (R-155 "To send validation
SMS"). "Qoute Separate" is still dropped -- and it is NOT a parser problem, the
words are in the block text and the model discards them, because the obligation
test does not recognise a pricing instruction as an obligation.

## Gate one passed: the draftable path works (and is 10/10 since 17 Sep)

15 Sep. `scripts/seed_fixture_bid.py` runs one bid end to end against a company
whose documents ARE evidence (`tests/fixtures/cybranytech/`), and it reaches
DRAFTABLE. That had never happened before in this project.

| | |
|---|---|
| accreditation, two OSCP testers, web methodology, API methodology | **answerable** |
| ISO 27001, expired 14 Mar 2025 | **NEEDS_REFRESH**, evidence status EXPIRED |
| three GCC references (one exists), mobile methodology (absent), eight-week schedule, nominate a PM | **BLOCKED** |

The ISO row is the one worth keeping: evidence complete and verified, readiness
demoted for age, neither axis collapsed into the other. That is the design
holding under a real input rather than in a docstring.

## Gate two passed: task to approved artifact, 17 Sep

`scripts/walk_gate_two.py` now runs all eight stages green. This is the first
time in the project that a requirement has gone from an assessed task to an
immutable, approved artifact version, acting throughout as real users built the
same way `app/api/deps.py` builds them -- no service called with a synthetic
god-context.

```
1. tasks exist for the bid                     6 task(s)
2. a department head assigns an owner          BID-01 -> Sara K.
3. the owner takes the task                    NOT_STARTED -> IN_PROGRESS
4. a response artifact exists for the task
5. a draft is generated, cited to evidence     11 statements, all four classes
6. the draft is submitted for approval         awaiting REVIEWER
7. proposed commitments approved by a role     1 commitment approved
8. a reviewer approves it                      artifact state is APPROVED
```

The walk is re-runnable and does NOT reset state. Anything a previous run
completed is reported as progress, and the thing it must never do is rewind:
see the stage-3 defect below.

## What gate two cost, and what it was worth

Three defects, and only one of them was in the product.

**1. A commitment routed to nobody stalls in silence.** A PROPOSED_COMMITMENT is
the one statement class no evidence can support -- it is a promise the company
has not made yet -- so a named role has to accept it personally.
`decide_commitment` requires `required_approver_role in ctx.role_codes`, which
is right. But the drafting model chooses that role, and it can choose one the
customer has not staffed. Then nobody can approve it, no approver was ever
notified, and the artifact is stopped for good. All the reviewer saw was:

    Every proposed commitment must be approved before the artifact can pass.

True, and useless. It named neither the commitments nor the roles, so a QUEUE
(somebody holds the role and has not decided) and an ORPHAN (nobody holds it,
ever) were indistinguishable. `artifacts.commitment_blockage` now names both,
and says which of the two you are looking at. It does **not** re-route
anything: code may report that a promise has no possible approver, but changing
who may bind the company is a person's decision and the audit log's record.

**2. Fix the pattern, not the instance -- the third and fourth time.** The
first run routed promises to PROJECT_MANAGER and SOLUTION_ARCHITECT, which the
fixture cast did not staff. Two people were added to the cast. The next run,
walking a different task, routed an NDA clause to LEGAL and stalled identically.
Adding LEGAL would have bought exactly one more run.

The pattern was that the prompt offered the model **all seventeen** `RoleCode`
values, so any of them could turn up. Closed over properly:

- `COMMITMENT_APPROVER_ROLES` in `app/core/permissions.py` -- the eleven roles
  that can own a promise at all. The six excluded are wrong by construction,
  not slow: an AUDITOR reviews what happened and undertakes nothing, a REVIEWER
  checks somebody else's work rather than owning it, DEPARTMENT_HEAD says "may
  act as a head" without saying of what.
- `_approver_role` validates against that set, so AUDITOR is refused at the door
  with a message listing what would have worked.
- `draft_statement_generation` bumped to **v4**, offering the same set, with a
  contract test asserting both directions. A role offered but not accepted fails
  a whole draft AFTER the call is billed; a role accepted but never offered
  makes the narrowing decorative.
- `_fixture_cast.ensure()` staffs all eleven and **asserts it against the
  database** at setup, exiting with the missing role names. The gap was knowable
  before a single model call was paid for, and the walk was discovering it three
  stages later, twice.

What is deliberately NOT narrowed is the customer's org chart. The model is
never told which of those roles a given organization has staffed -- a model
trimming its answer to who happens to be employed is doing a permission
calculation, and it would hide the very gap `commitment_blockage` reports.

**3. The harness framed the product.** Stage 8 failed with *"a task cannot move
from IN_PROGRESS to APPROVED"*. The product was right at every step. `submit`
moves the task to READY_FOR_APPROVAL; REWORK is a real path, so
READY_FOR_APPROVAL -> IN_PROGRESS is legal; and stage 3 was transitioning to
IN_PROGRESS *unconditionally*, so a second run quietly dragged an
already-submitted task backwards and then blamed stage 8 for the hole. Taking a
task somebody has already taken is not a re-run of that step, it is asking for
rework. Third time this walk needed the same re-runnability branch, after
stages 5 and 6.

**A test that was worse than no test.** `test_the_prompt_enumerates_the_roles_from_the_enum`
asserted that the registry's SOURCE contained the literal string
`sorted(r.value for r in RoleCode)`. It passed for a prompt built the approved
way while saying nothing about what the prompt offered, and broke the moment the
expression changed. This is the source-grepping anti-pattern a test was deleted
for earlier in the same project, written again hours later. It now asserts
behaviour. **Grep the source in a test and you are testing the implementation's
spelling.**

## 17 Sep: the suite is green, and two of the seven were real

`python -m pytest -q -p no:randomly` -> **1100 passing, 0 failing.** Run the
WHOLE suite; running `tests\unit` alone is how seven failures stayed invisible.

Five of the seven were stale assertions. Two were live defects, and both had
been reported by a red test for as long as the test had been red.

**Explicit supersession had never fired, once.** `collect_candidates` groups
clauses by the QUALIFIED reference ("4.2 Testing scope / 4.2") -- the fix for
the Jadwa collapse where a bare "3" gathered three unrelated obligations into
one contest. `DocumentRelation.clause_refs` holds BARE references, because that
is what an amendment says about itself. The resolver looked the qualified key up
in the bare-keyed map, matched nothing, and fell through to issue-date ordering
in silence. An amendment issued BEFORE the document it amends -- the ordinary
case, since the consolidated RFP is re-issued later -- therefore lost to the
document it was supposed to replace. `_is_superseded` now reconciles the two
spellings per candidate. `override()` had the same mismatch and a worse
consequence: it stored the caller's spelling as the key, so a bare override and
a qualified one could coexist as two CURRENT resolutions for one clause. It now
canonicalises, and refuses an ambiguous bare ref instead of guessing.

**An amendment created a duplicate requirement instead of a new version.**
`_requirements_from_block` scoped candidates to `source_document_version_id ==
block.document_version_id`. When an amendment wins a clause the winning block
moves into the amendment's version while the requirement's current version still
points at the RFP's, so nothing matched and every proposal looked new. The bid
ended up holding R-001 "Test 25 applications" and R-002 "Test 35 applications",
both live, both on clause 4.2, one of them retracted by the customer. That is
what `code`, `version_no` and `superseded_at` exist to prevent, and it made the
whole amendment lifecycle a duplicate factory. The qualified clause reference
now bridges the two versions.

**The lesson is about the red tests, not the bugs.** Six tests reported a live
defect for an unknown length of time, and because the suite was run in subsets
and these were known-red, they were read as a flaky stub and left. A red test
nobody reads is worse than no test: it converts a finding into background noise.

## Done: quantity is counted, and R-006 is no longer a known red

The last gate-one red. Three GCC reference projects required, one in the pack,
row read DRAFTABLE with "the gaps must be filled during drafting" -- true of a
wording gap, false of two projects that do not exist.

Split into a reading and an arithmetic, because that is the project's own rule:

  * `requirement_quantity@v1` (batched, cached, reached only by clauses that
    contain a numeral) answers ONE question per clause -- does this demand a
    number of separate things the bidder must show? It says NO to durations,
    ages, scope, money and standard names, and NO when unsure.
  * Migration **0023** stores `required_count`, `counted_noun` and `quantity_by`
    on `requirement_version`, because the number is a property of the WORDING;
    an amendment changing "three" to "two" is a new version. NULL means NOT
    JUDGED, never "none required" -- treating NULL as zero would block an entire
    live matrix the day this shipped.
  * `assessment.quantity_shortfall` counts distinct SOURCE DOCUMENTS among
    candidates the reader did not throw out, and blocks when that falls short.

### Measured on the live fixture, 17 Sep

```
quantity_review_completed  calls=1  carried_a_number=6
                           counting_obligations=2  settled_without_a_call=11
```

Of 17 requirements, 11 carried no numeral and cost nothing. Six reached the
model in ONE call, and it called exactly two of them counting obligations --
R-006 (three comparable projects) and R-005 (two OSCP testers). The other four
numerals are the eight-week schedule, the three-year window, the 25 applications
and the ISO/IEC 27001 in a standard's name, and it said NO to all of them.

Exactly one row moved: BLOCKED 10 -> 11, DRAFTABLE 6 -> 5, and the R-006 task
went NEEDS_INPUT -> BLOCKED. **R-005 stayed DRAFTABLE**, which is the control
that matters -- two CVs are two distinct sources against a demand for two, so
the rule saw a satisfied count and said nothing. A shortfall rule that also
fired on R-005 would have looked equally "successful" on R-006 and been useless.

**Gate one is now 10 of 10 with no known red**, for the first time in the
project.

The relevance prompt was NOT taught to call one project "not partial proof".
That was tried and made the verdicts flap: the one reference genuinely IS
partial proof, and the model was being asked to encode an arithmetic result in a
word that means something else.

Counting documents is a proxy, stated in the code rather than hidden: one
document describing three projects counts as one, and undercounts. That is the
direction to be wrong in -- the rule only downgrades, the row carries its count
and noun in the rationale, and `adjust` overrides it.

## Done: a rule that was unwaivable and could never fire

`RESTRICTED_DISCLOSURE` sat in `release.NON_WAIVABLE` since the ruleset was
written and **nothing ever raised it**. A grep found one hit: its own
declaration. The code claimed "this can never be waived" about a finding that
could not occur, and every release passed the check by having nothing to check.

The fixture tender says "Pricing must not appear anywhere in the technical
proposal" and the rate card is classified RESTRICTED. Nothing stopped a drafter
citing it, a reviewer approving, and the day rates leaving with the technical
submission -- which disqualifies a bid outright under most public procurement
rules.

`validate_package` now raises it on the confidentiality CLASS of the cited
evidence, not on the words in the rendered answer: prose-scanning for a rate
misses "five thousand dirhams" and fires on a page number, while the class is a
decision a person recorded at ingestion. CONFIDENTIAL is deliberately NOT
blocked -- it is the ordinary class for policies, methodologies and CVs, and a
control that fires on every release is one people learn to click past. Only
RESTRICTED and PRIVILEGED (`release.UNRELEASABLE`).

Verified by removing the rule and watching the three load-bearing cases fail.
**A new test that passes on its first run has not been shown to test anything.**

## Gate two: twelve stages

Stages 9-12 added: a cross-department request answered by the department that
was asked (the ANSWER travels, no access is granted), the proposal structure,
assembly, and the disclosure check. Assembly REFUSES while any mandatory section
lacks an approved artifact, which on the fixture is the ordinary case -- five of
six tasks untouched -- so that refusal is recorded as a pass with the open count.

**Four stages have now needed the same re-runnability branch** (3, 5, 6, 8),
each added separately after the walk reported the product broken for having
already done the work. Stage 3's version was the expensive one: it rewound a
submitted task and stage 8 was blamed for the hole. `_already_done` now asks the
question once, and any new stage should call it rather than grow a fifth copy.

## Suite state, and seven failures nobody was watching

`python -m pytest -q -p no:randomly` -> **1050 passing, 7 failing**. The seven
predate this work and are unrelated to it:

- `test_copilot.py::test_out_of_scope_offers_the_three_requests_in_least_privilege_order`
  -- retrieval trace says `not_permitted`, the test expects `out_of_scope`.
- six in `test_tender_intelligence.py` -- `StopIteration`, a stubbed provider
  running out of canned responses.

They were invisible because the suite was being run as subsets ("786 unit tests
passing" was never the whole suite). **Run the whole thing.** One stale
assertion in `test_artifact_lifecycle.py` was fixed on the way past: it pinned
`llm:draft_statement_generation@v1` and had sat red through the v2 and v3 bumps
saying nothing about either; it now derives the version from the registry, so it
still fails if the stamp stops matching the call actually made.

## What the fixture caught on its first two runs

**Gate one failed the first time for a reason in the fixture, not the product.**
`approved=0, embedded=0`: `_admissible()` requires APPROVED and extraction
records PROPOSED, so all 18 excerpts existed and none were candidates. The
Knowledge Administrator step -- stage 1 of the product's own walkthrough, "the
knowledge administrator later verifies it" -- had simply not been modelled. A
company that has never approved its corpus genuinely cannot bid from it.

**Then it caught a real false positive, which is what it was built for.**
`evidence_relevance` judged the web/API methodology PARTIAL proof of a MOBILE
methodology requirement, and testers' CVs proof of nominating a PROJECT MANAGER.
PARTIAL becomes DRAFTABLE, so both would have been drafted as capability the
company never documented -- the most damaging output this system can produce.

v2 already forbade subject-adjacency ("holding ISO 27001 does not support a
requirement for CREST"). That is same subject, different THING. This is the
same thing at a different SCOPE, and the rule did not reach it. **v3** adds:
where a requirement names a platform, standard, role or quantity, an excerpt
about a different one is UNRELATED, not PARTIAL. Re-running the fixture moved
all three rows to BLOCKED without disturbing the four that passed.

v3 also states the 400-character budget for `fit.offered`, which the schema
enforces and the prompt never mentioned -- it overran on four requirements in
one run, burned three repair attempts and degraded the verifier for the rest of
the bid.

**The fixture is the harness.** The v7/v8/v9 decomposition oscillation happened
because there was no way to measure a prompt change before shipping it. There
is now: change a prompt, re-run, see which rows move.

## What the fixture deliberately does not claim

Expectations are SETS of acceptable readiness, not single values. Two rows
swapped between runs -- API methodology DRAFTABLE then READY, rate card READY
then DRAFTABLE -- because both sit on the PARTIAL/CURRENT_VERIFIED seam, which
depends on a model judging coverage partial or full. Pinning that makes the
fixture fail on noise, and a red line that cries wolf gets skipped.

The sets never straddle the distinction the product exists to make: answerable
(DRAFTABLE or READY), needs renewal (NEEDS_REFRESH), not answerable (BLOCKED).
**A row expected BLOCKED can never be accepted as DRAFTABLE or READY** -- that
is the false-positive direction, and it is asserted exactly.

Also not covered: the permission path. The script assesses as SYSTEM, which
applies no permission filter, so the RESTRICTED rate card is legitimately
visible. Testing it needs a scoped user.

## The one red line still failing: quantity is not a relevance question

`scripts/seed_fixture_bid.py` reports 9 of 10. The tenth is "provide three
comparable projects" against a pack holding one, and it reads DRAFTABLE --
"the gaps must be filled during drafting", which is false. Nobody drafts their
way to two more delivered projects. The company would write "three comparable
GCC projects" over one, which is the exact output this system exists to prevent.

**It is left failing rather than widened to accept DRAFTABLE.** A red line
switched off the first time it fires is not a red line.

The diagnosis, and it is a design finding rather than a tuning problem.
`evidence_relevance` v3 first tried to fix this in the prompt -- "one completed
project is not partial proof of THREE" -- and that sentence was wrong. One
project IS partial proof of three in any ordinary reading, so the instruction
asked the model to mislabel, and it complied inconsistently: the row read
BLOCKED, BLOCKED, DRAFTABLE across three runs. **An instruction that fights the
meaning of its own label produces a flapping verdict, and a flapping verdict on
the answerable/blocked boundary is worse than a wrong one that holds still,
because nobody can tell which runs to trust.** The clause was removed; the
scope rule it shipped with was measured and stays.

Where it belongs: **counting is code's job, not the model's.** "Three projects",
"at least two OSCP testers", "eight weeks" are consequences of counting, and the
project's own division says the model judges meaning while code holds
consequences. The relevance judge should say PARTIAL and record the shortfall in
`fit.gap`; something deterministic should then read the required quantity off
the requirement and decide that a shortfall is a gap DRAFTING CANNOT FILL --
which is NEEDS_COMPANY_CONFIRMATION, "somebody decides whether the company can
claim this at all", not DRAFTABLE.

Note that the two-OSCP-testers row passes today only because the pack happens to
hold exactly two. It is not evidence that counting works.

## Why nothing is DRAFTABLE -- measured, and not what this file said

For several revisions this handover asserted "until the evidence classifier
exists nothing reaches DRAFTABLE". **That is wrong.** The assessment was re-run
after the withdrawn-document fix and the matrix says:

| | |
|---|---:|
| Requirements with NO evidence retrieved at all | 42 |
| Requirements where material is attached and the relevance judge says none is proof | 30 |
| Requirements whose evidence is "verified" -- and described by the model as PARTIAL | 3 |
| Requirements with complete proof of anything | **0** |

`evidence_classification` would change none of those numbers. It decides what
KIND of material an excerpt is. Typing `Certificate no.: [REPLACE] | Evidence
status: NOT EVIDENCED` as CERTIFICATE does not make it a certificate, and the
relevance judge already rejects it correctly.

**0 DRAFTABLE is the right answer for this document pack.** The pack is a
register OF evidence to be collected, not evidence: 235 of its rows say so in
their own words, and its manifest says "synthetic/template fields must be
replaced". The pipeline is reporting, accurately, that this bid cannot be
substantiated from these files.

That reframes the acceptance test. "Reach DRAFTABLE on the Jadwa bid with this
pack" is not achievable and never was. The test has to be: **given a company
pack containing at least one real, substantiated document, does a matching
requirement reach DRAFTABLE?** Nothing in the repo currently proves that path
works end to end, because no input has ever exercised it.

## What the withdrawn fix actually did

Also worth recording honestly: the fix cut the admissible pool from 502 rows to
198 -- 304 APPROVED excerpts hanging off superseded uploads stopped being
candidates -- and **readiness did not move by one row**, attachments stayed at
125. The withdrawn duplicates were ranking, and their live twins were ranking
identically beside them, so removing them changed no outcome. The fix was still
right (a shortlist that shows the same excerpt twice, half of it retracted, is
lying to the drafter) but it was not a blocker. The prediction here that
readiness might get WORSE was wrong.

## Small real defect found in the same pass

Three requirements carry `Evidence status = CURRENT_VERIFIED` while their own
rationale says the model read the evidence as *partially* covered (R-004, R-096,
R-142). Those should be PARTIAL. The label and the rationale disagree, and the
label is the optimistic one.

## Done: withdrawing a requirement is one operation

Found by accident. `check_before_rerun.py` was written to decide whether to
re-assess Jadwa, and printed a number nobody had asked for:

    current assessments: 158        live requirements: 89

`is_current` integrity held -- exactly one per requirement -- and that is what
made it dangerous. The extra 69 assessed WITHDRAWN requirements and went on
claiming to be current:

    state                          all   live
    BLOCKED                         81     59
    NEEDS_CLIENT_CLARIFICATION      43     21
    NEEDS_COMPANY_CONFIRMATION      33      9
    DRAFTABLE                        1      0

Every number a person had SEEN was right, because the export joins to the
requirement and filters. The trap was for whatever queried `is_current` next
without knowing it had to -- and the first thing that did reported BLOCKED as 81
against a true 59. Note the last row: the only DRAFTABLE row in the matrix was a
retired one. Unfiltered, the bid looked like it had one answerable requirement.

**Withdrawing is three facts**, and no caller did all three:

  1. lifecycle and successor;
  2. clarification links, which must follow the clause onto the survivors;
  3. the assessment, which must stop claiming to be current.

Fact 2 was fixed in `coalesce_enumerated_fragments.py` and not in
`retire_superseded_extractions.py` (the 130-occurrence recurrence already
recorded above). `merge_duplicate_requirements.py` did NEITHER -- two call sites
setting the column alone, no successor, no links -- a third instance nobody had
found. Fact 3 was fixed nowhere.

`requirements.withdraw()` now does all three and audits the counts. All five
call sites go through it. The only remaining direct assignment is in
`replay_r055.py`, which SIMULATES a withdrawal and rolls back; calling the
service there would move real links and write a real audit row for something
that never happened, so it is commented rather than changed.

Backfilled with `scripts/repair_withdrawn_assessments.py --apply` (69 rows,
nothing deleted, `is_current` cleared). Verified: 89 current for 89 live, every
diff zero.

## Measured: Jadwa does NOT need re-assessing for today's work

`scripts/check_before_rerun.py`, read-only, before spending anything:

  * **0** superseding relations on the tender, **0** contested clauses. Every
    clause has one candidate, so `_decide` returns early. The supersession fix
    is correct and INERT here -- Jadwa's duplication came from re-uploading
    files, not from amendments naming clauses.
  * 15 of 89 requirements carry a numeral, and **0** of them are in a state a
    shortfall could downgrade. Only one row was DRAFTABLE and it was retired.
    The quantity rule cannot move anything on this bid.

So a re-run would cost 89 requirements of `evidence_relevance` and provably
change nothing. **Do not re-run Jadwa to pick up 17 Sep's changes.** Re-run it
when the evidence corpus changes, which is what the quality backlog is about.

## A third source-grepping test, converted

`test_the_sweep_cross_checks_what_the_prompt_produced` asserted that one string
appeared at a lower CHARACTER OFFSET than another in
`retire_superseded_extractions.py`. Moving the withdrawal into the service
changed nothing about the rule and broke it instantly; a rearrangement that
broke the rule while leaving both strings in place would have passed. The
decision is now `sweep_verdict()` -- UNREAD / SKIP / RETIRE -- with a test per
outcome.

Three such tests have now been converted and a fourth was written and deleted on
the same day. **If a test reads code as text, it is testing the
implementation's spelling.**

## Done: confirmation is a gate, not a label (0024)

`RequirementLifecycle.CONFIRMED` was written by one function and read by
nothing, so the model's reading of a clause was the one link in the chain no
person approved. Now:

  * `decide_approval` refuses the FINAL approval of an artifact that answers a
    requirement not in CONFIRMED (withdrawn ones excluded), naming the codes.
    Rejection is never blocked. See `artifacts.unconfirmed_requirements`.
  * `requirement.confirmed_by` / `confirmed_at` record who and when. The
    LIFECYCLE is the fact; the columns are attribution. Legacy CONFIRMED rows
    with NULL attribution still pass -- an early draft keyed on `confirmed_at`
    and disagreed with itself.
  * `confirm_requirements(..., requirement_ids=[...])` confirms a chosen subset.
    `[]` confirms nothing; `None` means the whole bid. The API takes the same
    `requirement_ids` body field.
  * Nothing was backfilled. All 89 Jadwa requirements are unconfirmed, so no
    artifact on that bid can be approved until somebody confirms what it answers.

Tests: `tests/integration/test_an_unconfirmed_clause_cannot_be_answered.py`.

## Done: the integration suite actually runs (20 Sep)

"1114 passing" was 838 plus **285 integration tests silently skipped** --
`pgserver` was not installed, and it crashes on Windows anyway. Fixed so it
cannot recur unnoticed:

  * `tests/conftest.py` prints a red **INTEGRATION TESTS DID NOT RUN** block
    (with Windows instructions) whenever they skip.
  * `pgserver` is a POSIX-only dev dependency. On Windows, point the suite at a
    real server: `python scripts\create_test_database.py` creates
    `bidready_test`.
  * The vector backend is chosen in `tests/conftest.py` at IMPORT time (pytest
    imports every model during collection); no pgvector -> portable backend.
  * The fixture always `ALTER`s `bidready_app` (password and NOSUPERUSER ...
    NOBYPASSRLS). Roles are cluster-wide, so set `BIDREADY_TEST_APP_PASSWORD`
    to the password the dev role already has, or every script loses its login.
  * The suite **drops and rebuilds the `public` schema** of its database on
    every run (the setup is not idempotent; the second run produced 271
    `DuplicateObject` errors). It refuses any database whose name lacks "test",
    and `bidready` is refused outright.
  * The one skip is `reportlab` not being installed (`pip install reportlab`).

Windows, per terminal:

```
set BIDREADY_TEST_APP_PASSWORD=<the bidready_app password from .env>
set BIDREADY_TEST_DATABASE_URL=postgresql://postgres:<owner password>@localhost:5432/bidready_test
python -m pytest -q -p no:randomly
```

Noted, not chased: `tests/unit/test_worker_contract.py` uses the DB fixture, so
the unit/integration directory split is not what it claims; the vector-backend
probe is duplicated between `scripts/_vector_backend.py` and `tests/conftest.py`.

## Done: upload to DRAFTABLE, in the suite (20 Sep)

`tests/integration/test_a_real_document_reaches_draftable.py`. Every earlier
assessment test planted APPROVED evidence rows by hand; this one uploads the
cybranytech company files and runs the production chain -- parse, chunk, embed,
`knowledge_extraction`, `approve_evidence_batch`, `assess_bid`,
`evidence_relevance`, readiness. Only the model is scripted (a reader that calls
an excerpt proof if it carries the scheme ref / certificate number).

  * accreditation, read as proof, full coverage -> **READY**, cites `01_accreditation.md`
  * same proof, reader names a gap -> **DRAFTABLE** (the coverage demotion)
  * same file read as "related" only -> **not** answerable
  * expired ISO certificate -> **NEEDS_REFRESH**
  * mobile methodology (absent) -> **BLOCKED**
  * no administrator approval -> everything **BLOCKED**

Verified by removal: dropping the coverage demotion, the related-only rule, or
the approval filter each fails exactly one test. Expiry has THREE independent
guards (date check, EXPIRED status, approval filing lapsed rows as
HISTORICAL_VERIFIED); the test fails only with all three removed.

**Correction.** This file's backlog said "nothing in the repo proves the
draftable path end to end". `seed_fixture_bid.py` had proved it on 15 Sep with
the live model; what was missing was a guard in the suite. And the first draft
of this test asserted "PARTIAL means DRAFTABLE" -- the pre-fix rule. The product
refused it, correctly.

## Done: the whole workflow, end to end (21 Sep)

Every step of the product walkthrough now exists in code, API and UI. Suite:
**1182 passed** (fresh DB). Migration **0025** (`task_evidence`) -- restart
`run_local.py` so alembic applies it, and rebuild the frontend.

- **Work packages** -- `task_grouping@v1` proposes packages per department;
  `tasks.packages_from` (code) partitions and drops/keeps; fallback is one task
  per category. Owners are still assigned by the department head (deliberate).
- **AI pre-drafts** -- `assess_bid` drafts every AI_READY_FOR_REVIEW task whose
  v1 is empty (`drafting.prepare_ready_drafts`), each in a savepoint.
- **My work** -- `GET /me/dashboard`: ready for approval / needs my input /
  waiting on another department / completed, plus approvals and incoming requests.
- **Typed facts wait** -- `add_fact(approved=)`: a fact from someone without
  KNOWLEDGE_APPROVE goes to the review queue.
- **Evidence from inside a task** -- `POST/GET /tasks/{id}/evidence`. Uploads are
  company knowledge, NOT draft sources until approved; the owner is notified
  (`task.evidence_approved`) and can redraft.
- **Amendments** -- `extract_requirements` now calls `amendments.propagate` for
  superseding documents automatically and notifies affected task owners.
- **Customer structure + two submissions** -- `proposal_structure@v1` reads the
  tender's layout; sections are bound to TECHNICAL or COMMERCIAL documents;
  fallback is one section per answer split by department/category.
- **Review vs submission** -- REVIEW shows `[Evidence: ...]`,
  `[Proposed commitment - approved by ROLE]`, `[Provided by a contributor]`;
  SUBMISSION is clean.
- **Release checks** -- adds SECTION_WITHOUT_CONTENT, PRICE_IN_TECHNICAL
  (non-waivable), UNRESOLVED_TASK, OPEN_CUSTOMER_QUESTION, REQUIREMENT_UNANSWERED,
  DELIVERABLE_NOT_READY, SIGNATURE_REQUIRED, CONSISTENCY_NOT_CHECKED.
- **Consistency** -- `cross_section_consistency@v1`; each contradiction opens one
  "Resolve ... conflict" task (fingerprinted, never twice). `POST /bids/{id}/consistency-check`.
- **Trace / timeline / dashboard / letter** -- `GET /statements/{id}/trace`,
  `GET /tasks/{id}/timeline`, `GET /bids/{id}/dashboard`,
  `GET|POST /bids/{id}/clarification-letter[/send]` (docx, major questions first).
- **Won bid -> knowledge** -- WON proposes an `OUTCOME_AWARD` candidate; approving
  it creates the PROJECT entity with customer/award facts.
- **UI** -- My work screen, grant-access option on requests, proposed-commitment
  approvals, task evidence + history panels in the copilot, bid dashboard in
  readiness, letter buttons in clarifications, consistency check in release.

## Done: who to ask is decided by a routing call (22 Sep)

After each copilot answer, `department_routing@v1` (separate structured call)
says whether another department is needed, which (max 2), the question to send
and why. `app/services/routing.py` keeps only real department codes, never the
asker's own, and falls back to the old keyword rule if the call fails. The
result pre-fills the "Ask ..." request dialog; a person still sends it.
Each department has a `responsibilities` line (migration **0026**), editable by
an org admin on the Company knowledge screen (`PATCH
/admin/departments/{id}/responsibilities`); blank = built-in default per code.
Also this day: analysis refuses while documents are still being read; assessment
refuses with no requirements; My work shows the bid and unowned packages;
copilot prompt v5 lists real departments. Suite: 1189 passed.

## Done: the four extraction and governance defects the export showed (23 Sep)

Measured against the live export of bid 0e20677e and the tender itself.

- **Every excerpt typed OTHER (610 of them).** `evidence_type` is stamped from
  the document's type, and nothing ever set that type unless a person picked one
  at upload -- so the prompt rule "a blank form is not proof" could never fire.
  New call type `document_classification@v1` plus
  `app/services/document_classification.py`: files a company document at
  ingestion (before extraction, so excerpts inherit the right type), never
  overwrites a human filing, drops an invented type to OTHER, and restamps
  excerpts. Backfill: `POST /knowledge/documents/classify-unclassified`, and a
  button on Company knowledge.
- **One clause reference for two clauses.** Integration 4 (SMS) and Business 4
  (Menu) both read "Business Requirements / 4": an unnumbered section title was
  swallowed by the preceding clause's segment, so it never became a heading, and
  the glued blob was short enough to be filed as one. `_page_segments` now
  breaks on a bare section title (`looks_like_section_title`) and
  `looks_like_heading` rejects anything containing a sentence break.
- **"(Qoute Separate)" dropped.** `requirements.priced_separately(block)` reads
  the marker from the clause and its heading, spelling-tolerant; new column
  `requirement.priced_separately` (**0027**), shown in the API and the export.
- **Closures with no reason.** 47 coverage gaps closed silently.
  `clarification_issue.resolution_note` (**0028**) is written at every closure,
  and the coverage check names the requirements that now account for the clause.
- **Findings nobody can ask.** 90 of 131 open findings had no question.
  `ambiguity_analysis@v4` must carry a question the customer could answer, or
  not raise the issue; the screen marks the rest as internal notes and counts
  what is actually askable.

Suite: 1211 passed.

## Done: a scorecard, and the context the model was missing (23 Sep)

**`scripts/score_extraction.py`** -- one tender, counted, so two documents can
be compared instead of argued about: blocks and how many carry a clause ref,
OCR pages (every later figure is a floor when this is non-zero), sections and
the largest section's share, eligible blocks vs blocks read vs blocks nothing
accounted for, requirements per obliging block, requirements with no clause
ref, clause refs shared by unrelated blocks, open findings vs findings that can
actually be put to the customer, and closures with no reason. `--json` for a
file. Two integration tests pin it against a real extraction run.

**Context the model was not given** (audited this session):

- **Matrix rows.** A TABLE_ROW arrived with `table_name` (the enclosing
  heading) and no column names -- the header sits in the TABLE block that opens
  the table, usually an earlier batch. `requirements._table_headers` now sends
  `table_header`, and `requirement_decomposition@v10` says how to read a row
  against it: the obligation is the requirement cell, the bidder's response
  columns are not defects, a header or totals row imposes nothing. This is the
  shape the Jadwa tender never had and most Gulf tenders do.
- **What an evidence item is, at drafting time.** The copilot has been told each
  source's document type for two versions; `draft_statement_generation` never
  was, so a blank form could back an EVIDENCE_BACKED_FACT in customer-facing
  text. Each source now carries `document_type`, `can_prove_capability` and
  `expires`, and **v5** forbids citing a non-self-evidencing item as proof.

Also fixed: `test_customer_neutral` compared prompt versions as strings, so
"v10" < "v4" -- it would have failed on the tenth version of any prompt.

Suite: 1216 passed.

## Backlog, in the order that actually blocks a bid response

1. **A real company document, on the real bid.** The PATH is now proven and
   guarded (see "Done: upload to DRAFTABLE" above); what nobody has done is
   upload one genuinely signed document of the company's own -- a reference
   letter, a current certificate -- and assess Jadwa against it. Needs a file
   only the company has. Expect it to move a row only if the tender asks for
   what that document proves.
2. **`CURRENT_VERIFIED` vs a partial rationale** -- R-004, R-096, R-142 claim
   verified evidence while the model calls it partial. Small, and it is the
   optimistic direction, which is the wrong one to be wrong in.
3. **`evidence_classification@v1`** -- type the 375 unjudged rows. Worth doing
   for shortlist quality and for the copilot's context, but it is NOT the
   DRAFTABLE blocker and should stop being described as one.
3. **Deliverable data capture** -- owner, deadline, format, filename, blank on
   all 32. Dedup is done (14 canonical, 18 linked duplicates).
4. **Run the workflow on Jadwa** -- the code exists (see above); nobody has yet
   re-assessed Jadwa to generate packages and drafts, or sent a request.
5. **The historical axis** -- `effective_date` and HISTORICAL_VERIFIED are
   unreachable. The whole company pack derives from a 2015 proposal and nothing
   downstream says so. Deliberately NOT folded into `is_substantiated`.

## Also open, not started

- **Integration namespace** — ~20 integration clauses filed under
  `Business Requirements / N`, colliding with genuine business clauses on the
  same numbers. 16 requirements carry no clause ref at all. Probably the same
  parser/heading cause.
- **Deliverables** — 30 rows for about 12 real deliverables (five
  financial-proposal variants, three CV variants). Needs the coalescing
  treatment requirements got, plus owner/deadline/format/filename, all blank.
- **No requests, no drafted statements, 33 unassigned tasks.** Nothing has been
  sent to a department or a customer.
- **Company corpus** — 28 document records for 14 files, all classifications
  blank, all 610 evidence records typed `OTHER`, entities/facts/relations all
  still PROPOSED. No document classifier exists.
- **Evidence precision** — 144 attachments, 3 judged supporting. R-004's
  supporting attachment contradicts its own rationale.
- **Export presentation** — closed coverage findings keep their original "must
  be reviewed manually" text with no resolution note; the reason is in the audit
  trail only. `Same question as` renders canonical question TEXT rather than a
  stable id (the database stores `restates_clarification_id` correctly).

## Commands

```
cd D:\rfpproject\backend
python -m pytest tests\unit -q

python scripts\check_uncovered_obligations.py     # dropped obligations
python scripts\check_clarification_links.py       # link / question health
python scripts\check_crowded_blocks.py --min 8    # block vs its requirements
python scripts\dedupe_questions.py                # dry run, no model calls
python scripts\check_document_classification.py   # what the corpus is filed as
python scripts\check_evidence_units.py --per 4    # what one classifiable unit looks like
python scripts\mark_unsubstantiated_evidence.py   # the free marker floor
python scripts\close_false_coverage_gaps.py
python scripts\coalesce_enumerated_fragments.py
python scripts\repair_withdrawn_links.py
python scripts\export_matrix.py --bid 0e20677e-9cc3-434e-ad88-c953d6676028

python -m pytest -q -p no:randomly          # THE WHOLE SUITE; needs the two `set` lines above
python scripts\seed_fixture_bid.py --reset  # gate one, from an empty org
python scripts\walk_gate_two.py             # gate two, eight stages
```

The two fixture scripts are the harness for any prompt change. `--reset` wipes
the fixture organization and rebuilds it, and it is needed after any change to
`_fixture_cast.py`. Neither needs `run_local.py` running -- scripts import the
app fresh each time.

Every mutating script is dry-run by default and needs `--apply` (or named
codes). `run_local.py` runs `alembic upgrade head` itself with owner
credentials. **Restart it after any change under `app/`** — a stale process
silently produced two identical exports during one session. `backend\.env` holds
a stale DB password, so hand-run alembic fails; use `run_local.py`.

Import a new migration file directly before trusting it (`python -c "import
importlib.util…"`): nothing in the unit suite executes migrations, and
`sa.dialects.postgresql` does not resolve from `import sqlalchemy as sa`.

## Repo

Git initialised, remote `Jonah005/rfp_system`, **not yet pushed**. `.gitignore`
excludes the company document pack, `.tools/`, venvs, caches and logs. Secret
scan clean; both `.env.example` files are tracked deliberately with
`DEEPSEEK_API_KEY=` empty.
