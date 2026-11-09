# BidReady — Phase F: Document Ingestion & Company Knowledge (implemented and locked)

Phases E and F are locked. This records what Phase F built, the decisions taken
during implementation, and what the final closeout verification proved. Phase G
must consume this completed company-knowledge layer; it must not finish or
replace Phase F extraction.

## 1. Authentication hardening carried in with this phase

Two changes were requested before Phase F began and shipped as migration 0002.

**Globally unique normalized email.** `auth_lookup_user(email)` resolves a tenant
*from* an email before any tenant context exists, so that lookup is only well
defined if an address identifies at most one user across the deployment. The
previous `UNIQUE (org_id, email)` permitted the same address in two
organizations. Uniqueness is now enforced on `lower(email)` — the lookup compares
case-insensitively and the constraint must agree with it. The migration refuses
to run if duplicates already exist rather than silently deleting accounts, and
`create_user` turns the resulting integrity error into a clean message that does
**not** disclose which organization holds the address, since that would let an
outsider enumerate a competitor's staff list.

**SECURITY DEFINER hardening, asserted rather than assumed.** The function has
`search_path` pinned to `public, pg_temp` (without it, a caller could shadow
`app_user` with their own table earlier on the path and the function would read
that while running as a BYPASSRLS role), `PUBLIC` EXECUTE is explicitly revoked
(every role is a member of PUBLIC), and only `bidready_app` holds EXECUTE. Its
owner `bidready_auth` cannot log in and is not a superuser, so the elevated
privilege is reachable only through that one function. Six tests assert each
property against the migrated database.

## 2. Scope delivered

| Area | Delivered |
|---|---|
| Upload safety | Content-based type sniffing, size limits, macro/OLE/executable rejection, OOXML active-content inspection, pluggable malware scanning |
| Ingestion chain | `Document → DocumentVersion → DocumentBlock → DocumentChunk → Embedding`, with per-step persisted state |
| Automatic extraction | A successful COMPANY upload schedules the tenant-aware Celery pipeline; parsed chunks enter the registered DeepSeek `knowledge_extraction` structured call and are persisted as proposed graph/evidence records |
| Parsers | PDF, DOCX, XLSX, CSV, TXT/Markdown — all structure-preserving |
| Provenance | `SourceLocator` value object carried from parse through chunk to citation |
| Knowledge graph | `KnowledgeEntity` / `KnowledgeFact` / `KnowledgeRelation` / `Evidence` / `EvidenceLink` with temporal validity, currentness, approval, supersession |
| Governance | Entity resolution with a review queue, deterministic conflict detection, expiry sweep, evidence approval |
| Retrieval | Hybrid vector + full-text + structural boosts, with permission filters inside the SQL |
| API | 15 knowledge operations; 29 distinct OpenAPI paths total |
| Migration | 0003 — pgvector, RLS on 13 new tables, the polymorphic tenant guard, embedding-dimension assertion |

## 3. Decisions taken during implementation

**Table rows are chunked one-per-row, carrying their header.** A retrieved row
must be self-describing. `Client: Ministry of Interior | Sector: Government`
retrieves and cites correctly; a bare `Ministry of Interior, Government, UAE`
does not. This, plus keeping `table_name` and `heading_path` as ranking signals,
is what makes "which government VAPT references do we have?" return the
reference table rather than a paragraph in a CV. A test asserts that ordering.

**Chunks never span two clauses.** A chunk covering both 6.1 and 6.2 can cite
neither, and precedence resolution in Phase G keys on `clause_ref`. The chunker
splits on a clause change even when the token budget has room.

**Clause detection is deliberately conservative.** A false `clause_ref` is worse
than a missing one, so bare years and large integers are rejected. `2024 was a
strong year` yields no clause; `Section 4.2.3 Scope` yields `4.2.3`.

**Entity resolution never merges silently.** Auto-linking requires 0.92
similarity. `Ahmed Hassan` versus `A. Hassan` scores 0.80 and therefore creates a
`PROPOSED` entity with its candidates attached for a Knowledge Administrator.
Conflating two people's credentials is precisely the error that ends up in a
submitted proposal.

**The model proposes; governance writes.** The registered
`knowledge_extraction` call accepts bounded batches of parsed COMPANY chunks and
returns a validated structured result with local entity references and mandatory
source chunk ordinals. Deterministic application code resolves entities and
creates proposed `KnowledgeEntity`, `KnowledgeFact`, `KnowledgeRelation`, and
`Evidence` rows plus `EvidenceLink` provenance. Every model-created row is
`PROPOSED` / `PENDING_APPROVAL`, evidence is not reusable before approval, and a
review item is created under the existing governance rules. An audited
completion marker makes pipeline re-entry idempotent without treating model
output as an authoritative write.

**Conflicts are flagged, not ranked.** Two admissible facts sharing
`(entity, predicate)` with overlapping validity and different values are both set
`CONFLICTING` and queued. Non-overlapping validity is history, not contradiction,
and is left alone.

**The retrieval scope ceiling defaults to the top of the scale.** An earlier
draft defaulted `max_confidentiality` to `CONFIDENTIAL`, which hid content users
were entitled to and would have made them believe the organization did not hold
it. Entitlement now decides what a user *may* see; scope is a ceiling a task
imposes to narrow *below* that deliberately — a security task drafting a
methodology has no business pulling salary bands into context even if its owner
could open them. Both directions are tested.

**Binary content sniffing was hardened after a test failure.** C0 control bytes
are valid UTF-8, so a naive `decode()` check passed binary payloads through as
`text/plain`. Content with NUL bytes or a high proportion of control characters
is now classified as binary and refused.

**`evidence_link` is guarded by a trigger, not a composite key.** Its target is
polymorphic (fact, relation, entity, and later assessment and statement), so no
composite foreign key can express the tenant constraint. A `BEFORE INSERT OR
UPDATE` trigger asserts the target exists and shares the tenant, and rejects
target types whose tables do not yet exist rather than accepting them unchecked.

## 4. What the tests prove

At the Phase F lock checkpoint, 157 tests were green with warnings treated as
errors against real PostgreSQL 18.1 with native pgvector 0.8.1. The complete
declared runtime and development dependency set was installed; dependency
integrity, Ruff, mypy, and Celery task registration passed with no skips or
dependency errors. After the later Phase G-J work landed, the entire expanded
repository was revalidated against a fresh database on that same real stack:
**311 tests passed, 0 skipped**, Ruff passed, mypy reported no issues in 81
source files, and Celery loaded all six application tasks including
`parse_and_index_document`. This later run re-proves the locked Phase F behavior
inside the current application rather than replacing the 157-test historical
checkpoint. The deployment target remains the locked PostgreSQL 16 baseline.

*Ingestion* — the full chain produces blocks, chunks and one embedding per chunk;
provenance survives from file to chunk and every chunk points back at its blocks;
table rows keep their table name and columns; `confidentiality_class` and `side`
are denormalized onto chunks so retrieval can filter inside the vector query; an
infected upload never reaches a parser and stores nothing; company documents
cannot be attached to a bid and tender documents must be; a second version does
not replace the first; ingestion is audited; re-indexing does not duplicate
embeddings.

*Safety* — type is determined from content not filename; a declared MIME cannot
override the content; macro-enabled and executable formats are refused; an OOXML
container smuggling `vbaProject.bin` or an embedded `.exe` is rejected; EICAR is
caught; `NoOpScanner` refuses to construct in production; binary content with
control bytes is not mistaken for text, while tabs, newlines and Arabic text
remain text.

*Knowledge* — an indexed COMPANY document automatically invokes the registered
structured extraction call and creates proposed entities, facts, relations,
evidence, links, and review items; source locators identify the exact document
version and chunk region; one cited chunk produces one shared provenance row;
re-entering the pipeline neither recalls the model nor duplicates records;
identical names link, similar names never merge silently; merging
moves facts and leaves a forwarding pointer; cross-type merges are refused;
contradictions are flagged and queued; sequential validity is not a conflict; the
expiry sweep invalidates facts and evidence past their window; expired evidence
cannot be approved for reuse; unapproved evidence does not count as support; an
`evidence_link` cannot cross tenants; knowledge does not leak across tenants.

*Retrieval* — the reference table outranks a CV for a references query; every hit
carries a resolvable citation and an explanation; keyword and semantic paths both
contribute; restricted content is not a **candidate** without permission (asserted
against the admissibility clause itself, not just the ranked list); the matching
role permission opens it; a live task-scoped grant opens exactly one document and
does not follow the user to another task; a revoked grant stops working
immediately and an expired one never admits content; retrieval never crosses
tenants; a dead end names the owning department so the caller can offer the
request flow; a caller with no read permission sees nothing; scope narrows by
side and can narrow below entitlement; only the active embedding model is
queried.

## 5. Deliberately not built by Phase F at its lock checkpoint

Phase F itself did not build tender-side intelligence: precedence resolution,
requirement extraction, ambiguity analysis and scope confidence belonged to
Phase G and have since been implemented there. The `document.side` distinction
and `DocumentRelation` were the Phase F handoff points that Phase G consumed.
Automatic COMPANY-document knowledge extraction remains owned and complete in
Phase F; later phases do not defer or duplicate it.

## 6. Historical handoff: Phase G — RFP Intelligence

At Phase F lock, the next unit was tender upload and classification,
per-clause precedence resolution
(`ClauseResolution`), atomic requirement extraction with a structural coverage
cross-check, conditional requirements, the ambiguity engine, the deliverable
register, and the tender-only Scope Confidence gate. Phase G has since shipped
and remains locked. It starts from review-governed company knowledge and may
retrieve accepted/current evidence, but company content must never create
tender scope.
