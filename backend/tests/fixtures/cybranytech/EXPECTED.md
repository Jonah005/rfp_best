# Cybranytech — the fixture bid, and what it is supposed to prove

The Jadwa bid cannot reach DRAFTABLE and that is the correct answer for it: its
company pack is a register OF evidence to be collected, and 235 of its rows say
so in their own text. Measured on that bid, 42 requirements retrieve nothing,
30 have material the relevance judge correctly rejects, and 0 have complete
proof of anything.

The consequence is that **stages 4 to 12 of the product have never run.** Task
assignment, task-scoped chat, cross-department requests, approval into response
artifacts, assembly, cross-section validation, release blocking and outcome
learning are all modelled and coded, and no input has ever reached them. Four
test files out of 772 touch that half of the system.

This fixture exists to reach them. It is a fictional company with genuinely
substantiated documents, and a fictional tender written so that **every
requirement has a known correct answer before the pipeline runs.** That is the
difference between a test and another exploration: a result that surprises us
here is a defect, not a discovery.

## The company

Cybranytech FZ-LLC, invented for this fixture. Seven documents under
`company/`, all of which read as evidence rather than as a request for evidence
-- asserted by `test_the_fixture_is_actually_evidence`, which runs the
placeholder markers over them and requires zero hits. If that test ever fails,
the fixture has drifted into being a second Jadwa and proves nothing.

## The tender

`tender/RFP_Cybersecurity_Assessment.md`, 14 obligations, written against the
company pack so that each exercises one path deliberately.

## What each requirement is for

| Tender clause | Company evidence | Expected outcome | The path it exercises |
|---|---|---|---|
| 2.1 accreditation | `01_accreditation.md`, valid to Apr 2027 | **DRAFTABLE** | The happy path. If this row is not draftable, nothing in the product's second half can run at all. |
| 2.3 two OSCP testers | two CVs, both OSCP, no expiry | **DRAFTABLE** | Evidence assembled from two documents for one requirement. |
| 3.1 web methodology | `06_vapt_methodology.md` | **DRAFTABLE** | Narrative evidence rather than a certificate. |
| 3.2 API methodology | same document | **DRAFTABLE** | Two requirements drawing on one document without collapsing into one row. |
| 2.2 ISO 27001 | `02_iso27001.md`, **expired 14 Mar 2025** | **NEEDS_REFRESH** | The orthogonality of the two axes. Evidence is complete and verified; readiness is not. A system that reports this DRAFTABLE has collapsed the axes -- the exact failure the schema exists to prevent. |
| 2.4 three GCC references | one project only | **BLOCKED**, partial | Quantity shortfall. The evidence is real and insufficient, which is different from absent. |
| 3.3 mobile methodology | nothing -- deliberately absent | **BLOCKED**, no evidence | The SEC-03 gap from the walkthrough. Should produce a task needing human input, not a fabricated answer. |
| 3.4 eight-week schedule | nothing; it is bid-specific | **BLOCKED** → proposed commitment | Nothing in company knowledge can satisfy this. It must become a Commitment approved by a person, never evidence. |
| 3.5 nominate a Project Manager | no PM in the pack | **BLOCKED** | Needs a person to nominate. Exercises assignment rather than retrieval. |
| 4.1 daily rate per role | `07_rate_card.md`, RESTRICTED | **BLOCKED** for a security user | The cross-department path. A tester must not see rates; the answer has to come through a request to Commercial. |
| 4.2 no pricing in the technical proposal | n/a | release check | Must block release if a rate reaches the technical document. The AED 18,000 case from the walkthrough. |
| 5.1 authorised signature | n/a | submission action | Satisfied by doing, not by evidencing. |
| 5.2 signed NDA | n/a | submission action | Same. |
| 1.1 scope quantities | n/a | new commitment | A customer instruction, not something to evidence. |

## BLOCKED is a destination, not a dead end

Read the table above carefully: the BLOCKED rows are not the disappointing
ones. They are the rows that become somebody's job, and half this fixture
exists to prove that routing fires.

Verified in the code rather than assumed, because it is the property the whole
product rests on:

  * `assess_bid` loops every requirement. One row with no evidence produces one
    BLOCKED row; the other 88 are assessed normally. Nothing short-circuits.
  * A blocked requirement makes its task BLOCKED -- "a covered requirement is
    blocked: nothing attached proves it" -- and the state machine allows
    BLOCKED -> IN_PROGRESS. A person takes it and it moves. Tasks a person has
    already taken are never recomputed by a later assessment.
  * **`drafting` has no readiness gate.** A contributor can generate a draft for
    a requirement with no evidence at all. What the model cannot do is misclaim
    the sentence: EVIDENCE_BACKED_FACT validates every citation against the
    authorised source set and raises on anything outside it, so an unevidenced
    answer comes back as PROPOSED_COMMITMENT with a required approver role, or
    as an ASSUMPTION. That is the mobile-methodology and eight-week-schedule
    path, and it already exists.
  * Release blocks last, and only five findings can never be waived:
    TENANT_LEAKAGE, PERMISSION_VIOLATION, MALWARE_DETECTED, CORRUPTED_ARTIFACT,
    RESTRICTED_DISCLOSURE -- all safety failures. Everything else, including
    MANDATORY_UNANSWERED and UNSUPPORTED_CLAIM, takes a ReleaseException with a
    written justification, requested by one person and approved by another.

So a company that holds some of what a tender asks for and not the rest is the
NORMAL case, not a failure mode. The Jadwa bid is the existing proof: it has
almost no evidence and still produced 89 assessed requirements, 32 tasks and
three complete workbooks without stopping.

## What "working" means here

Two gates, in order.

**Gate one.** At least one requirement reaches DRAFTABLE, and clause 2.2 reaches
NEEDS_REFRESH rather than DRAFTABLE. The first proves the draftable path exists
at all; the second proves expiry still demotes it. Until gate one passes, no
later stage is reachable and nothing else on this page can be measured.

**Gate two.** Walk the stages the Jadwa bid has never reached, in the order the
product actually runs them, and record what breaks:

1. tasks carry an owner and a department
2. a draft statement is generated for a draftable requirement, cited to evidence
3. a person approves it into a versioned response artifact
4. a requirement with restricted evidence raises a cross-department request
5. answering that request unblocks the task
6. approved artifacts assemble into proposal sections
7. validation catches a contradiction between two separately approved sections
8. release blocks on a rate appearing in the technical proposal

My expectation, stated before running so it can be wrong: several of these
fail. They were built and locked without an input that reached them.

## Deliberately not in this fixture

No amendment, and no won-bid outcome. Those are stages 9 and 10 of the
walkthrough and they only mean anything once 1 to 8 hold. Adding them now would
produce more findings than can be acted on, which is how the last several
rounds went.
