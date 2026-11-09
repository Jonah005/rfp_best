"""Requirement assessment and bid readiness.

Two rules shape this module.

**Direction of travel.** An assessment reads company knowledge to judge whether
a requirement can be answered. It never lets that knowledge alter the
requirement. Nothing here writes to a requirement, and the evidence query is
company-side only.

**Two axes, never collapsed.** `evidence_status` says what the proof shows;
`bid_readiness` says what has to happen next. A requirement can be fully
evidenced and still not ready, because the certificate expired or a signature is
outstanding. Collapsing them is exactly how "historical evidence read as
current" becomes invisible — risk 3 from the original evaluation.

The verdict itself is arithmetic over evidence status, validity and approval.
The model contributes retrieval candidates and a human-readable rationale, never
the verdict.
"""

from __future__ import annotations

import math
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import func, or_, select, tuple_
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.context import ExecutionContext
from app.core.errors import NotFound, ValidationError
from app.core.logging import get_logger
from app.gateway import registry
from app.gateway.gateway import build_gateway
from app.gateway.registry import wrap_untrusted
from app.models import (
    AssessmentEvidence,
    ClarificationIssue,
    Department,
    Document,
    DocumentVersion,
    Evidence,
    EvidenceLink,
    KnowledgeEntity,
    KnowledgeFact,
    KnowledgeRelation,
    Requirement,
    RequirementAssessment,
    RequirementVersion,
)
from app.models.enums import (
    ApprovalStatus,
    AssessmentEvidenceRole,
    AssessmentState,
    BidReadiness,
    ClarificationSeverity,
    ClarificationStatus,
    DepartmentCode,
    EvidenceLinkRole,
    EvidenceLinkTarget,
    EvidenceStatus,
    KnowledgeStatus,
    ObligationOwner,
    RequirementApplicability,
    RequirementCategory,
    is_self_evidencing,
)
from app.schemas.evidence_relevance import Coverage, RelevanceVerdict, RequirementFit
from app.services import audit
from app.services.requirement_scope import LIVE_REQUIREMENT

log = get_logger("assessment")

# How many evidence candidates to consider per requirement.
CANDIDATE_LIMIT = 8
# How many the database returns for scoring. Larger than CANDIDATE_LIMIT so
# ranking has something to choose between, and bounded so the work per
# requirement does not grow with the size of the corpus.
CANDIDATE_POOL = 50
# Below this share of the requirement's terms a lexical hit is not attached.
# A fraction: 0.05 means "shares at least a twentieth of the words".
MIN_ATTACH_SCORE = 0.05

# The semantic bar is separate, and far higher, because the two numbers are not
# the same kind of thing. A lexical score is a fraction of shared terms, so 0.05
# is genuinely weak. A cosine similarity from a sentence encoder is not: two
# unrelated business documents routinely sit around 0.75, so a 0.05 floor admits
# literally everything.
#
# Judging both by one threshold produced exactly that. Every requirement in a
# real bid came back with the same eight excerpts and read "current verified /
# ready" — including "proposals become the property of the customer", which no
# company document can evidence. Confident false positives are the worst failure
# this system can have: the entire point is that a claim is defensible, and a
# matrix that says everything is proven is more dangerous than one that says
# nothing is.
MIN_SEMANTIC_SIMILARITY = 0.85

# And a cap on how many semantic-only matches may attach to one requirement.
# A threshold that drifts with a model change can only mislead a little if the
# count is bounded; unbounded, it fills the matrix with confident noise.
MAX_SEMANTIC_ATTACHMENTS = 3

# The recall floor, used only when the model verifies what retrieval found.
#
# 0.85 is a guess that has to be right on its own, because nothing looks at the
# result: too high and real proof is never retrieved, too low and noise attaches
# with a "current verified" label. Verification changes what the number has to
# do. It stops being the decision and becomes the shortlist, so it can be set
# where recall is good and precision is poor, and the reader downstream throws
# the poor half away. A miss here is still unrecoverable — an excerpt that is
# not retrieved is never judged — which is why this moves down and not up.
MIN_SEMANTIC_SIMILARITY_RECALL = 0.78
# How many candidates the shortlist may carry into one verification call. Wider
# than CANDIDATE_LIMIT, because pruning is the point; bounded, because the whole
# shortlist goes into one prompt and a long prompt is a slow, expensive one.
VERIFICATION_SHORTLIST = 12
# Excerpts are truncated in the prompt: the model is judging what an excerpt is
# about, and the first paragraph settles that for anything this system stores.
VERIFICATION_EXCERPT_CHARS = 700
# Above this share of the requirement's terms, a lexical match is not a
# judgement call. MIN_ATTACH_SCORE (0.05) is the floor for being worth looking
# at; this is the bar for being obviously right, and a shortlist made entirely
# of candidates this strong is not sent to the model at all.
MIN_CONFIDENT_LEXICAL = 0.25
# ...and an absolute floor under it, because a fraction alone is not enough.
#
# Found live. "Training for 2 seats is required." has three terms over three
# characters, so ONE shared common word scores 0.33 and cleared the fraction
# bar on its own. Every excerpt in the corpus mentioning training was therefore
# "obviously right", the whole shortlist looked unambiguous, and the requirement
# was never sent to the model at all — it read "current verified / ready" on
# eight unchecked attachments, the only row in the bid still doing so.
#
# Short requirements are common in tenders and are exactly the ones a single
# word can hijack. Certainty now needs both a share of the requirement AND
# enough distinct words that the overlap cannot be one coincidence.
MIN_CONFIDENT_TERMS = 3

# --- the third retrieval path: the knowledge graph --------------------------
# How many entities a full-text hit may return before the coverage rule below
# narrows them. Bounded so a requirement full of common words cannot pull the
# whole entity table into memory to be filtered in Python.
GRAPH_ENTITY_POOL = 40
# One hop, and a bound on it. A company entity with hundreds of relations is a
# real thing (a client with many projects), and an unbounded expansion turns one
# requirement into a scan of the entire graph.
GRAPH_EXPANSION_LIMIT = 200
# What share of an ENTITY's name must appear in the requirement for the
# requirement to count as naming it. 1.0 would reject "CREST accreditation"
# against a requirement saying only "CREST"; much lower matches on one shared
# word, which is how "Information Security Policy" attaches itself to every
# requirement mentioning security.
MIN_ENTITY_NAME_COVERAGE = 0.6
# Words that carry no identity in an entity name, so they neither help a match
# nor should count against one.
_NAME_STOPWORDS = frozenset(
    {"the", "and", "for", "of", "a", "an", "ltd", "llc", "inc", "company", "certificate"}
)
# A graph hit is a hard link, not a similarity, so it is admitted on its own.
# The cap exists because extraction quality decides how good these links are,
# and a bad extraction run should degrade the matrix rather than fill it.
MAX_GRAPH_ATTACHMENTS = 4
# The score a graph hit carries into ranking. Above MIN_ATTACH_SCORE so the
# link survives the floor, below a strong semantic match so a paraphrase of the
# requirement itself still outranks a structural connection to it.
GRAPH_BASE_SCORE = 0.55

# Categories whose satisfaction is a signature or a form rather than a claim.
SIGNATURE_CATEGORIES = frozenset(
    {RequirementCategory.ADMINISTRATIVE, RequirementCategory.SUBMISSION}
)

# Which department normally owns each requirement category. A recommendation
# only: the bid manager may override, and nothing downstream assumes it.
DEPARTMENT_BY_CATEGORY: dict[RequirementCategory, DepartmentCode] = {
    RequirementCategory.TECHNICAL: DepartmentCode.SOLUTION_ARCHITECTURE,
    RequirementCategory.FUNCTIONAL: DepartmentCode.PRODUCT,
    RequirementCategory.NON_FUNCTIONAL: DepartmentCode.SOLUTION_ARCHITECTURE,
    RequirementCategory.SECURITY: DepartmentCode.SECURITY,
    RequirementCategory.IMPLEMENTATION: DepartmentCode.PMO,
    RequirementCategory.PROJECT_MANAGEMENT: DepartmentCode.PMO,
    RequirementCategory.SUPPORT: DepartmentCode.PRODUCT,
    RequirementCategory.TRAINING: DepartmentCode.PMO,
    RequirementCategory.DOCUMENTATION: DepartmentCode.QUALITY,
    RequirementCategory.STAFFING: DepartmentCode.HR,
    RequirementCategory.ELIGIBILITY: DepartmentCode.BID_MANAGEMENT,
    RequirementCategory.ADMINISTRATIVE: DepartmentCode.BID_MANAGEMENT,
    RequirementCategory.SUBMISSION: DepartmentCode.BID_MANAGEMENT,
    RequirementCategory.LEGAL: DepartmentCode.LEGAL,
    RequirementCategory.COMMERCIAL: DepartmentCode.COMMERCIAL,
    RequirementCategory.CONTRACTUAL: DepartmentCode.LEGAL,
}


@dataclass
class Candidate:
    evidence: Evidence
    score: float
    role: AssessmentEvidenceRole
    rank: int
    note: str
    #: What the model said this excerpt is, if it read it. None means it was
    #: never judged — verification off, unavailable, or the shortlist was
    #: unambiguous — and the arithmetic below then behaves exactly as it did
    #: before verification existed.
    support: RelevanceVerdict | None = None


@dataclass
class AssessmentReport:
    assessed: int = 0
    skipped_not_applicable: int = 0
    by_readiness: dict[str, int] = field(default_factory=dict)
    by_evidence_status: dict[str, int] = field(default_factory=dict)
    # How much evidence the run had to work with. Without this, a corpus that
    # is empty, unapproved, or un-embedded is indistinguishable from a matcher
    # that is failing to find things — every requirement reads "no evidence"
    # either way, and the two need completely different responses.
    evidence_pool: dict[str, int] = field(default_factory=dict)
    # What the model check did to the shortlists: how many candidates it read,
    # how many it called proof, and how many it threw out. `dropped` is the
    # number worth watching — zero means verification is not earning its cost,
    # and a very high share means retrieval is casting too wide a net.
    verification: dict[str, int] = field(default_factory=dict)
    # Tasks grouped from the requirements this run assessed. Generation happens
    # here because this is the moment the work is actually known: before the
    # assessment, a requirement has no evidence status and no readiness, so a
    # task built from it would be a title with nothing behind it. Nothing else
    # in the product triggered grouping, which left every freshly analysed bid
    # with a full matrix and an empty task board.
    tasks: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "assessed": self.assessed,
            "skipped_not_applicable": self.skipped_not_applicable,
            "by_readiness": self.by_readiness,
            "by_evidence_status": self.by_evidence_status,
            "evidence_pool": self.evidence_pool,
            "verification": self.verification,
            "tasks": self.tasks,
        }


# --- evidence retrieval for one requirement ---------------------------------


# Evidence a requirement may draw on at all: approved for reuse, and in a state
# that still says something. Applied inside every retrieval query, so an
# unapproved excerpt is never fetched, let alone ranked.
def _admissible():
    """What may be considered as evidence at all.

    The third predicate was missing, and its absence was measurable. The
    company corpus holds 610 evidence rows over 28 document records for 14
    files: one live record and one WITHDRAWN record per file, 304 of the 610
    rows hanging off the withdrawn half. Every shortlist was drawing from a
    pool that was close to half retracted material, and nothing downstream
    showed it -- a withdrawn duplicate ranks exactly as well as its live twin
    and reads identically in the drafter's context.

    `Document.withdrawn_at.is_(None)` was already applied in `precedence.py`,
    `requirements.py`, `tender_analysis.py` and the knowledge API. This is the
    fifth place that needs it and the one that did not have it: the same
    "fix the pattern, not the instance" lesson that let 25 dangling requirement
    codes reappear a day after they were fixed, because one of two scripts
    doing the same thing was looked at.

    A correlated subquery rather than a join, because `_admissible()` returns
    predicates that six different queries splice into their own WHERE clause,
    and a join would change the shape of each of them. It correlates on
    `Evidence.org_id`, so the tenant boundary travels with the predicate
    instead of depending on each caller to re-state it.
    """
    live_source = (
        select(DocumentVersion.id)
        .join(
            Document,
            (Document.id == DocumentVersion.document_id)
            & (Document.org_id == DocumentVersion.org_id),
        )
        .where(
            Document.withdrawn_at.is_(None),
            DocumentVersion.org_id == Evidence.org_id,
        )
    )
    return (
        Evidence.approval_status == ApprovalStatus.APPROVED,
        Evidence.status.in_(
            [
                KnowledgeStatus.CURRENT_VERIFIED,
                KnowledgeStatus.HISTORICAL_VERIFIED,
                KnowledgeStatus.EXPIRED,
                KnowledgeStatus.CONFLICTING,
            ]
        ),
        Evidence.source_document_version_id.in_(live_source),
    )


def _lexical_candidates(session: Session, ctx: ExecutionContext, *, terms: list[str]):
    """Full-text selection: exact words, bounded and ranked in the database.

    This is what keeps identifiers findable — a standard's number or a product
    name is a literal string, and no embedding recovers it as reliably.
    """
    safe_terms = [re.sub(r"[^a-z0-9]", "", term) for term in terms]
    safe_terms = [term for term in safe_terms if term]
    if not safe_terms:
        return []
    tsquery = func.to_tsquery("simple", " | ".join(safe_terms))
    relevance = func.ts_rank(Evidence.excerpt_tsv, tsquery)
    return list(
        session.execute(
            select(Evidence)
            .where(
                Evidence.org_id == ctx.org_id,
                *_admissible(),
                Evidence.excerpt_tsv.op("@@")(tsquery),
            )
            .order_by(relevance.desc())
            .limit(CANDIDATE_POOL)
        ).scalars()
    )


def _semantic_candidates(
    session: Session, ctx: ExecutionContext, *, text_query: str
) -> list[tuple[Evidence, float]]:
    """Nearest evidence by meaning, as (evidence, similarity in 0..1).

    Returns nothing rather than raising when no embedder is configured or no
    evidence has been embedded yet: semantic matching is an improvement on the
    lexical path, never a precondition for it, and a corpus mid-backfill must
    still assess.
    """
    try:
        # A requirement is the query side of the pair; the excerpts are
        # passages. The e5 family encodes the two differently and comparing
        # across the wrong convention is not the space the model was trained in.
        vector = build_gateway().embed([text_query], kind="query")[0]
    except Exception:  # noqa: BLE001 - a missing embedder must not fail assessment
        log.warning("semantic_matching_unavailable", reason="no embedding provider")
        return []

    if get_settings().vector_backend == "portable":
        return _portable_semantic_candidates(session, ctx, vector=vector)

    distance = Evidence.excerpt_vector.cosine_distance(vector)
    rows = session.execute(
        select(Evidence, distance.label("distance"))
        .where(
            Evidence.org_id == ctx.org_id,
            *_admissible(),
            Evidence.excerpt_vector.is_not(None),
        )
        .order_by(distance)
        .limit(CANDIDATE_POOL)
    ).all()
    return [(row[0], _similarity(row[1])) for row in rows]


def _portable_semantic_candidates(
    session: Session, ctx: ExecutionContext, *, vector: list[float]
) -> list[tuple[Evidence, float]]:
    """Cosine in Python over a real[] column, for databases without pgvector.

    Same embeddings and same ordering; what is missing is the index, so this is
    a scan. The admissibility predicate still runs in the query — the permission
    boundary does not move just because the arithmetic did.
    """
    rows = list(
        session.execute(
            select(Evidence).where(
                Evidence.org_id == ctx.org_id,
                *_admissible(),
                Evidence.excerpt_vector.is_not(None),
            )
        ).scalars()
    )
    scored = []
    for evidence in rows:
        stored = evidence.excerpt_vector
        if not stored:
            continue
        scored.append((evidence, _cosine(vector, list(stored))))
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored[:CANDIDATE_POOL]


def _similarity(distance: float | None) -> float:
    """Cosine distance to a 0..1 similarity, clamped.

    Distance runs 0..2; anything past 1 is pointing away from the query and is
    reported as no similarity rather than as a negative score, which would sort
    below unrelated evidence but read as if it were a match.
    """
    if distance is None:
        return 0.0
    return max(0.0, min(1.0, 1.0 - float(distance)))


def _cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return max(0.0, min(1.0, dot / (left_norm * right_norm)))


def names_entity(label: str, present: set[str]) -> bool:
    """Does a requirement containing `present` actually NAME this entity?

    The direction of this test is the whole precision argument for graph
    expansion, so it is a function on its own rather than a line inside a query.

    Ordinary retrieval asks how much of the REQUIREMENT a text covers, which is
    generous on purpose. This asks the reverse: is the ENTITY's whole name in the
    requirement? "CREST" must be there; "ISO/IEC 27001" needs its parts. Ask it
    the generous way round and an entity called "Information Security Policy"
    attaches itself to every requirement containing the word "security", which
    would make the graph path worse than useless — it would be confidently,
    structurally wrong.
    """
    name_tokens = [t for t in _tokens(label) if t not in _NAME_STOPWORDS]
    if not name_tokens:
        return False
    covered = sum(1 for token in name_tokens if token in present)
    return covered / len(name_tokens) >= MIN_ENTITY_NAME_COVERAGE


def _graph_admissible(model):
    """The same governance rule as evidence, applied to graph rows.

    A relation nobody approved is a claim, not a fact, and traversing it would
    let unreviewed extraction output route real evidence to a requirement.
    """
    return (
        model.approval_status == ApprovalStatus.APPROVED,
        model.status.in_(
            [
                KnowledgeStatus.CURRENT_VERIFIED,
                KnowledgeStatus.HISTORICAL_VERIFIED,
                KnowledgeStatus.EXPIRED,
                KnowledgeStatus.CONFLICTING,
            ]
        ),
    )


def _named_entities(
    session: Session, ctx: ExecutionContext, *, terms: list[str], text_query: str
) -> list[KnowledgeEntity]:
    """Entities the requirement actually names.

    The direction of the match is what makes this safe. Ordinary retrieval asks
    "how much of the requirement does this text cover", which is generous by
    design. Here the question is reversed: is the ENTITY's whole name present in
    the requirement? "CREST" must appear; "ISO/IEC 27001" needs its parts. An
    entity called "Information Security Policy" therefore does not attach itself
    to every requirement containing the word "security", which is exactly what a
    forgiving match would do and what would make graph expansion worthless.
    """
    safe_terms = [re.sub(r"[^a-z0-9]", "", term) for term in terms]
    safe_terms = [term for term in safe_terms if term]
    if not safe_terms:
        return []
    tsquery = func.to_tsquery("simple", " | ".join(safe_terms))
    rows = list(
        session.execute(
            select(KnowledgeEntity)
            .where(
                KnowledgeEntity.org_id == ctx.org_id,
                *_graph_admissible(KnowledgeEntity),
                KnowledgeEntity.merged_into_entity_id.is_(None),
                KnowledgeEntity.name_tsv.op("@@")(tsquery),
            )
            .limit(GRAPH_ENTITY_POOL)
        ).scalars()
    )

    present = set(_tokens(text_query))
    named: list[KnowledgeEntity] = []
    for entity in rows:
        labels = [entity.canonical_name, *(entity.aliases or [])]
        if any(names_entity(label, present) for label in labels):
            named.append(entity)
    return named


def _graph_candidates(
    session: Session, ctx: ExecutionContext, *, terms: list[str], text_query: str
) -> list[tuple[Evidence, str]]:
    """Evidence reached through the knowledge graph, with the path that found it.

    Embeddings and full text both work on the requirement's words. The graph
    works on what the company *is*: extraction has already resolved a pile of
    documents into typed entities, so a requirement naming CREST can walk from
    the accreditation to the certificate that proves it, to the body that issued
    it, to the projects delivered under it — none of which need share vocabulary
    with the requirement.

    One hop, deliberately. Two hops from a company entity reaches most of the
    company, and "connected to something connected to the subject" is not a
    reason to put an excerpt in front of a customer.

    Returns (evidence, why) pairs. The `why` is the point: unlike a similarity
    score, a graph hit can say which entity connected it, and a reviewer can
    check that claim.
    """
    entities = _named_entities(session, ctx, terms=terms, text_query=text_query)
    if not entities:
        return []

    reason_by_entity = {
        entity.id: f"the requirement names {entity.canonical_name} "
        f"({entity.entity_type.value.replace('_', ' ').lower()})"
        for entity in entities
    }
    entity_ids = set(reason_by_entity)

    # One hop. A relation carries the reason with it, so evidence reached
    # through a neighbour can still say how it got there.
    relations = list(
        session.execute(
            select(KnowledgeRelation)
            .where(
                KnowledgeRelation.org_id == ctx.org_id,
                *_graph_admissible(KnowledgeRelation),
                or_(
                    KnowledgeRelation.subject_entity_id.in_(entity_ids),
                    KnowledgeRelation.object_entity_id.in_(entity_ids),
                ),
            )
            .limit(GRAPH_EXPANSION_LIMIT)
        ).scalars()
    )

    targets: dict[tuple[EvidenceLinkTarget, uuid.UUID], str] = {
        (EvidenceLinkTarget.KNOWLEDGE_ENTITY, entity_id): reason
        for entity_id, reason in reason_by_entity.items()
    }
    for relation in relations:
        anchor = (
            relation.subject_entity_id
            if relation.subject_entity_id in entity_ids
            else relation.object_entity_id
        )
        reason = reason_by_entity.get(anchor, "the knowledge graph")
        targets[(EvidenceLinkTarget.KNOWLEDGE_RELATION, relation.id)] = reason
        for neighbour in (relation.subject_entity_id, relation.object_entity_id):
            if neighbour not in entity_ids:
                targets.setdefault((EvidenceLinkTarget.KNOWLEDGE_ENTITY, neighbour), reason)

    facts = list(
        session.execute(
            select(KnowledgeFact.id, KnowledgeFact.entity_id).where(
                KnowledgeFact.org_id == ctx.org_id,
                *_graph_admissible(KnowledgeFact),
                KnowledgeFact.entity_id.in_(entity_ids),
            )
        )
    )
    for fact_id, entity_id in facts:
        targets[(EvidenceLinkTarget.KNOWLEDGE_FACT, fact_id)] = reason_by_entity.get(
            entity_id, "the knowledge graph"
        )

    if not targets:
        return []

    # The evidence predicate is unchanged: graph expansion decides what to look
    # for, never what may be seen. Unapproved evidence stays unreachable.
    rows = session.execute(
        select(Evidence, EvidenceLink.target_type, EvidenceLink.target_id)
        .join(EvidenceLink, EvidenceLink.evidence_id == Evidence.id)
        .where(
            Evidence.org_id == ctx.org_id,
            *_admissible(),
            EvidenceLink.org_id == ctx.org_id,
            EvidenceLink.link_role.in_(
                [EvidenceLinkRole.SUPPORTS, EvidenceLinkRole.PARTIALLY_SUPPORTS]
            ),
            tuple_(EvidenceLink.target_type, EvidenceLink.target_id).in_(list(targets)),
        )
        .limit(CANDIDATE_POOL)
    ).all()

    found: dict[uuid.UUID, tuple[Evidence, str]] = {}
    for evidence, target_type, target_id in rows:
        if evidence.id in found:
            continue
        found[evidence.id] = (evidence, targets[(target_type, target_id)])
    return list(found.values())


class VerificationUnavailable(Exception):
    """The model could not check this shortlist; fall back to thresholds."""


class RelevanceVerifier:
    """Ask the model whether retrieval's shortlist actually answers the requirement.

    This exists because the alternative is a magic number. A cosine floor has to
    separate proof from noise using one scalar, with nothing downstream to catch
    it when it is wrong, and it is wrong in both directions at once: unrelated
    business prose sits around 0.75, real paraphrase sometimes lower. Reading the
    two texts is the thing that actually decides the question, and reading is
    what this model is for.

    What it is allowed to do is deliberately small:

    * It sees only excerpts the permission-filtered query already returned, so
      it can prune and reorder, never introduce.
    * It never sees, and is told not to consider, approval, validity or
      readiness. Those remain arithmetic over the database: an excerpt the model
      loves is still EXPIRED if its date says so.
    * Every verdict is written into the assessment's reasoning trace and into
      each attachment's note, so a reviewer sees which attachments a model
      accepted and why.
    * A failure is not an error. The verifier latches off for the rest of the
      run and retrieval reverts to the strict thresholds, because "the vendor is
      down" must never be the reason a bid cannot be assessed.

    One call per requirement is the honest cost: a sixty-requirement bid is
    sixty calls. The shortlist for a requirement goes in one call rather than
    one call per pair, which is the difference between minutes and an hour.
    """

    def __init__(self) -> None:
        self._enabled = get_settings().assessment_llm_verification
        # The comparison for the requirement currently being assessed. Cleared
        # by `begin_requirement`, so a requirement whose verification did not
        # run can never inherit the previous requirement's reading — which
        # would attach a plausible, specific, and completely wrong gap.
        self.last_fit: RequirementFit | None = None
        #: What the model said about each attached excerpt, by evidence id.
        #: Carried into the Candidate so the verdict reaches the arithmetic and
        #: not only the prose — see `_evidence_status`.
        self.last_verdicts: dict[uuid.UUID, RelevanceVerdict] = {}
        self.disagreements = 0
        # Cost accounting. `calls` is what was actually paid for; the other two
        # are what was avoided, and they are reported because an optimisation
        # nobody can see is an optimisation nobody maintains.
        self.calls = 0
        self.cache_hits = 0
        self.skipped_requirements = 0
        self.checked = 0
        self.supported = 0
        self.partial = 0
        self.dropped = 0
        self.unjudged = 0
        self.failures = 0

    @property
    def available(self) -> bool:
        return self._enabled

    def begin_requirement(self) -> None:
        self.last_fit = None
        self.last_verdicts = {}

    def counters(self) -> dict[str, int]:
        return {
            "calls": self.calls,
            "cache_hits": self.cache_hits,
            "skipped_requirements": self.skipped_requirements,
            "disagreements": self.disagreements,
            "checked": self.checked,
            "supported": self.supported,
            "partial": self.partial,
            "dropped": self.dropped,
            "unjudged": self.unjudged,
            "failures": self.failures,
        }

    def prune(
        self,
        session: Session,
        ctx: ExecutionContext,
        *,
        requirement_text: str,
        shortlist: list[tuple[float, Evidence, str]],
    ) -> list[tuple[float, Evidence, str]]:
        if not shortlist:
            return shortlist

        prompt = self._prompt(requirement_text, shortlist)
        gateway = build_gateway()
        try:
            # Cached: this call type is a deterministic read at temperature
            # zero, so an identical requirement and shortlist has an identical
            # answer. Assessment is re-run every time more evidence is approved,
            # and without this every re-run re-pays for every requirement that
            # did not change.
            result = gateway.generate_structured(
                "evidence_relevance", prompt, ctx=ctx, session=session, cache=True
            )
        except Exception as exc:  # noqa: BLE001 - any provider failure degrades, never fails
            self.failures += 1
            self._enabled = False
            log.warning(
                "evidence_verification_unavailable",
                reason=type(exc).__name__,
                detail=str(exc)[:200],
            )
            raise VerificationUnavailable from exc

        if gateway.last_call_cached:
            self.cache_hits += 1
        else:
            self.calls += 1

        verdicts = {
            v.candidate: v
            for v in result.verdicts
            # Out of range means the model answered about something it was not
            # shown. Discarded rather than repaired: there is no way to know
            # which excerpt it meant.
            if 1 <= v.candidate <= len(shortlist)
        }
        if not verdicts:
            # An empty reply is not "nothing is relevant" — it is a reply that
            # judged nothing, and treating it as a rejection would silently
            # empty the matrix.
            self.failures += 1
            self._enabled = False
            log.warning("evidence_verification_empty", candidates=len(shortlist))
            raise VerificationUnavailable

        kept: list[tuple[int, float, Evidence, str]] = []
        for index, (score, evidence, note) in enumerate(shortlist, start=1):
            self.checked += 1
            verdict = verdicts.get(index)
            if verdict is None:
                # Kept, and said so. Dropping evidence because the model forgot
                # to mention it would lose real proof for a formatting slip.
                self.unjudged += 1
                kept.append((2, score, evidence, f"{note} Not checked by the model."))
                continue

            reason = verdict.reason.strip()
            if verdict.verdict is RelevanceVerdict.UNRELATED:
                self.dropped += 1
                continue
            self.last_verdicts[evidence.id] = verdict.verdict
            if verdict.verdict is RelevanceVerdict.SUPPORTS:
                self.supported += 1
                # "Supports" is a statement about meaning. Whether the excerpt
                # may be relied on is decided elsewhere (`is_substantiated`),
                # and the note must not contradict that column on the same row.
                if evidence.is_substantiated is False:
                    tier, label = (
                        1,
                        "Model check: relevant to this requirement, but inadmissible as proof",
                    )
                else:
                    tier, label = 0, "Model check: supports this requirement"
            else:
                self.partial += 1
                tier, label = 1, "Model check: related, not proof on its own"
            kept.append(
                (tier, score, evidence, f"{note} {label}{f' — {reason}' if reason else ''}.")
            )

        # Tier first, then retrieval score: what the model calls proof outranks
        # what it calls related, whatever the cosine said.
        kept.sort(key=lambda item: (item[0], -item[1]))
        # Only kept when something survived. A comparison written against a
        # shortlist that was then entirely rejected describes evidence this
        # assessment does not cite.
        self.last_fit = result.fit if kept else None
        return [(score, evidence, note) for _, score, evidence, note in kept]

    def _prompt(self, requirement_text: str, shortlist: list[tuple[float, Evidence, str]]) -> str:
        lines = [
            "REQUIREMENT (tender text):",
            wrap_untrusted(requirement_text.strip()),
            "",
            f"SHORTLIST ({len(shortlist)} company evidence excerpts). Return a verdict "
            "for every number below, then compare the two sides in `fit`.",
        ]
        for index, (_, evidence, _) in enumerate(shortlist, start=1):
            excerpt = (evidence.excerpt or "").strip()[:VERIFICATION_EXCERPT_CHARS]
            lines.append(f"\n[{index}]")
            lines.append(wrap_untrusted(excerpt))
        return "\n".join(lines)


def _candidates_for(
    session: Session,
    ctx: ExecutionContext,
    *,
    requirement: Requirement,
    text_query: str,
    verifier: RelevanceVerifier | None = None,
) -> list[Candidate]:
    """Find governed company evidence that might answer a requirement.

    Only evidence a knowledge administrator has approved for reuse is eligible.
    Unapproved material may be perfectly true and still must not silently back a
    claim in a customer-facing document.

    With a verifier, this runs in two stages: retrieval casts a wider net and
    the model prunes it. Without one, the thresholds are the decision. The
    stages are ordered this way round on purpose — the model only ever sees
    excerpts the permission-filtered query already returned, so verification can
    remove and reorder but can never introduce evidence.
    """
    terms = [t for t in _tokens(text_query) if len(t) > 3]
    if not terms:
        return []

    verifying = verifier is not None and verifier.available
    semantic_floor = MIN_SEMANTIC_SIMILARITY_RECALL if verifying else MIN_SEMANTIC_SIMILARITY
    semantic_budget = VERIFICATION_SHORTLIST if verifying else MAX_SEMANTIC_ATTACHMENTS
    shortlist_size = VERIFICATION_SHORTLIST if verifying else CANDIDATE_LIMIT

    # Three retrieval paths, because they fail in different directions.
    #
    # Lexical finds exact terms — a standard's number, a product name, a clause
    # reference — and is blind to paraphrase. Semantic finds meaning and is
    # vague about identifiers. A requirement saying "firms must be Industry
    # Recognized/Certified" shares no word with a certificate saying "certified
    # to ISO/IEC 27001", so lexical alone never retrieved it; a requirement
    # naming "ISO 27001" specifically wants the row containing that string.
    #
    # The union is taken before scoring so neither path can hide a candidate
    # from the other, and each candidate keeps the reason it was found.
    # A third path, on a different axis again. Lexical and semantic both read
    # the requirement's words; the graph reads what extraction already resolved
    # the company to BE. A requirement naming CREST walks from the accreditation
    # to the certificate that proves it — no shared vocabulary required, and the
    # connection is a stored link a reviewer can check rather than a number.
    lexical = _lexical_candidates(session, ctx, terms=terms)
    semantic = _semantic_candidates(session, ctx, text_query=text_query)
    graph = _graph_candidates(session, ctx, terms=terms, text_query=text_query)
    graph_reasons = {evidence.id: reason for evidence, reason in graph}

    scored: list[tuple[float, Evidence, str]] = []
    seen: set[uuid.UUID] = set()
    # Candidates whose admission needs no second opinion — see `_needs_reading`.
    certain: set[uuid.UUID] = set()
    semantic_attached = 0
    for evidence, similarity in semantic:
        haystack = set(_tokens(evidence.excerpt))
        overlap = sum(1 for term in terms if term in haystack)
        lexical_score = overlap / len(terms) if terms else 0.0

        # Each path is judged by its own rule, because the two scores are not
        # comparable. A candidate found semantically must clear the semantic
        # bar; one that also shares words can still qualify lexically.
        lexical_ok = lexical_score >= MIN_ATTACH_SCORE
        semantic_ok = similarity >= semantic_floor and semantic_attached < semantic_budget
        if not (lexical_ok or semantic_ok):
            continue
        if semantic_ok and not lexical_ok:
            semantic_attached += 1

        seen.add(evidence.id)
        # The better of the two readings, not their average: a certificate that
        # answers the requirement in different words is not half a match.
        score = max(lexical_score, similarity)
        note = (
            f"Matched {overlap} of {len(terms)} requirement terms."
            if lexical_ok and lexical_score >= similarity
            else f"Semantic match ({similarity:.0%} similar); shares "
            f"{overlap} of {len(terms)} terms."
        )
        if _lexically_certain(overlap, len(terms)) or similarity >= MIN_SEMANTIC_SIMILARITY:
            certain.add(evidence.id)
        scored.append((score, evidence, note))

    for evidence in lexical:
        if evidence.id in seen:
            continue
        haystack = set(_tokens(evidence.excerpt))
        overlap = sum(1 for term in terms if term in haystack)
        if not overlap:
            continue
        seen.add(evidence.id)
        if _lexically_certain(overlap, len(terms)):
            certain.add(evidence.id)
        scored.append(
            (
                overlap / len(terms),
                evidence,
                f"Matched {overlap} of {len(terms)} requirement terms.",
            )
        )

    # Graph hits last, and merged rather than appended. An excerpt that both
    # matched the words and hangs off a named entity is the strongest kind of
    # candidate there is, and it would be perverse to report it twice or to let
    # the weaker of its two readings decide its rank.
    graph_attached = 0
    for index, (score, evidence, note) in enumerate(scored):
        reason = graph_reasons.pop(evidence.id, None)
        if reason is not None:
            certain.add(evidence.id)
            scored[index] = (
                max(score, GRAPH_BASE_SCORE),
                evidence,
                f"{note} Also linked in the knowledge graph: {reason}.",
            )
    for evidence, reason in graph:
        if evidence.id not in graph_reasons or evidence.id in seen:
            continue
        if graph_attached >= MAX_GRAPH_ATTACHMENTS:
            break
        graph_attached += 1
        seen.add(evidence.id)
        certain.add(evidence.id)
        haystack = set(_tokens(evidence.excerpt))
        overlap = sum(1 for term in terms if term in haystack)
        scored.append(
            (
                max(GRAPH_BASE_SCORE, overlap / len(terms)),
                evidence,
                f"Reached through the knowledge graph: {reason}.",
            )
        )

    scored.sort(key=lambda item: item[0], reverse=True)
    shortlist = [item for item in scored[:shortlist_size] if item[0] >= MIN_ATTACH_SCORE]

    if verifier is not None and verifying and not _needs_reading(shortlist, certain):
        # Nothing here is a judgement call: every candidate is a hard lexical
        # match, a graph link, or well clear of the strict similarity floor, and
        # a reader would agree with all of them. Paying for a call to be told so
        # is the cheapest waste in the system to remove, and on a bid with a
        # well-extracted corpus it removes a large share of the calls.
        verifier.skipped_requirements += 1
        return _rank(shortlist[:CANDIDATE_LIMIT])

    if verifying:
        assert verifier is not None
        try:
            shortlist = verifier.prune(
                session, ctx, requirement_text=text_query, shortlist=shortlist
            )
        except VerificationUnavailable:
            # The verifier has latched itself off, so this retries on the strict
            # thresholds — a narrower net than the one just cast. Re-running
            # retrieval is cheaper than trying to reconstruct what the strict
            # path would have admitted from a shortlist built for a wider one.
            return _candidates_for(
                session, ctx, requirement=requirement, text_query=text_query, verifier=verifier
            )

    return _rank(
        shortlist[:CANDIDATE_LIMIT],
        verifier.last_verdicts if verifier is not None else None,
    )


def _lexically_certain(overlap: int, term_count: int) -> bool:
    """Is this match strong enough that a reader would only confirm it?

    Both conditions, not either. The fraction says the excerpt covers a real
    share of the requirement; the absolute count says the overlap is too wide to
    be one word appearing by chance. A three-term requirement therefore cannot
    be certified by a single common noun, which is exactly how an unchecked row
    reached "ready" on the live bid.
    """
    if term_count == 0:
        return False
    return overlap >= MIN_CONFIDENT_TERMS and overlap / term_count >= MIN_CONFIDENT_LEXICAL


def _needs_reading(shortlist: list[tuple[float, Evidence, str]], certain: set[uuid.UUID]) -> bool:
    """Is there anything here a threshold cannot settle on its own?

    The model earns its cost on ambiguity: a candidate admitted by the widened
    recall floor that would not have cleared the strict rule. Those are the ones
    that used to attach as confident nonsense, and reading them is the whole
    point of the verification stage.

    A shortlist with none of them is a different situation. Every candidate is
    either a hard lexical match, a stored graph link, or well clear of the strict
    similarity floor — cases the thresholds already get right, and where the
    model would be paid to agree. Skipping those is not a shortcut around the
    reading; it is declining to buy a reading of something already legible.

    An empty shortlist needs nothing either: there is nothing to judge, and the
    requirement reports no evidence whichever way it is reached.

    One exception overrides all of that. A blank form or a template shares its
    vocabulary with the requirement it was designed to answer -- a "CV Update
    Form: Software Tester" is wall-to-wall personnel nouns -- so it clears the
    lexical rule more easily than the real evidence does, and "certain" is
    exactly the wrong word for it. Those are read whatever the score says. It
    costs a model call; admitting a blank form to a mandatory requirement
    unread costs a claim nobody can substantiate.
    """
    # There are two ways to be that exception and the type answers only one.
    # A certification-register row typed CERTIFICATE is self-evidencing by kind
    # while reading "Certificate no.: [REPLACE] | Evidence status: NOT
    # EVIDENCED", and it clears the lexical rule against a certification
    # requirement for exactly the vocabulary reason a blank CV form does.
    #
    # `is False` rather than falsy: NULL means nobody has judged this row, and
    # an unjudged row is not a placeholder. Treating the two alike would buy a
    # model call for all 600 excerpts the day the column landed.
    if any(
        not is_self_evidencing(evidence.evidence_type) or evidence.is_substantiated is False
        for _, evidence, _ in shortlist
    ):
        return True
    return any(evidence.id not in certain for _, evidence, _ in shortlist)


def _rank(
    shortlist: list[tuple[float, Evidence, str]],
    verdicts: dict[uuid.UUID, RelevanceVerdict] | None = None,
) -> list[Candidate]:
    """Turn a ranked shortlist into candidates, assigning roles by position.

    Roles are assigned last, after any pruning: if the model rejects what
    retrieval ranked first, the promotion has to follow, or an assessment cites
    a PRIMARY that is no longer attached.
    """
    verdicts = verdicts or {}
    return [
        Candidate(
            evidence=evidence,
            score=round(score, 4),
            role=AssessmentEvidenceRole.PRIMARY if rank == 1 else AssessmentEvidenceRole.SUPPORTING,
            rank=rank,
            note=note,
            support=verdicts.get(evidence.id),
        )
        for rank, (score, evidence, note) in enumerate(shortlist, start=1)
    ]


def _tokens(text: str) -> list[str]:
    """Words, split on anything that is not alphanumeric.

    Splitting on whitespace alone left "Recognized/Certified" and "ISO/IEC" as
    single tokens that could never match anything — tender language is full of
    slashed pairs and hyphenated terms, and each half is a real word worth
    matching on.
    """
    return [token for token in re.split(r"[^a-z0-9]+", (text or "").lower()) if token]


# --- the verdict ------------------------------------------------------------


#: How a computed evidence status reads in the model's coverage vocabulary, so
#: the two can be compared at all. EXPIRED and HISTORICAL_VERIFIED both map to
#: "covered", because they are statements about dates, not about whether the
#: material answers the ask — the model was never asked about currency and its
#: silence on it is not a disagreement.
_COVERAGE_OF_STATUS: dict[EvidenceStatus, Coverage] = {
    EvidenceStatus.CURRENT_VERIFIED: Coverage.FULLY_COVERED,
    EvidenceStatus.HISTORICAL_VERIFIED: Coverage.FULLY_COVERED,
    EvidenceStatus.EXPIRED: Coverage.FULLY_COVERED,
    EvidenceStatus.PARTIAL: Coverage.PARTIALLY_COVERED,
    EvidenceStatus.CONFLICTING: Coverage.PARTIALLY_COVERED,
    EvidenceStatus.NONE: Coverage.NOT_COVERED,
}


#: Readiness states a reading may never move. Each is already a harder call to
#: action than anything the coverage verdict can produce: not applicable is a
#: scope decision, blocked means nothing was found at all, an open clarification
#: is a question outstanding with the customer, and needs-confirmation is
#: already where a NOT_COVERED reading would send it.
_READINESS_FLOOR = frozenset(
    {
        BidReadiness.NOT_APPLICABLE,
        BidReadiness.CUSTOMER_OBLIGATION,
        BidReadiness.BLOCKED,
        BidReadiness.NEEDS_CLIENT_CLARIFICATION,
        BidReadiness.NEEDS_COMPANY_CONFIRMATION,
    }
)


def _dissent(fit: RequirementFit | None, computed: EvidenceStatus) -> str | None:
    """What the model would say differently, when it disagrees.

    The computed status wins — it is arithmetic over approval and dates, and a
    reader's opinion does not expire a certificate. But a disagreement is the
    most informative thing in the run: the arithmetic says "three approved
    excerpts are attached, therefore covered", and the model says "all three are
    about a different service". One of them is wrong, and the only way anybody
    finds out is if the disagreement is on the screen instead of averaged away.
    """
    if fit is None:
        return None
    if fit.coverage is _COVERAGE_OF_STATUS.get(computed):
        return None
    spoken = fit.coverage.value.replace("_", " ").lower()
    detail = fit.gap.strip() or fit.offered.strip()
    return f"The model reads the attached evidence as {spoken}" + (f": {detail}" if detail else ".")


def _evidence_status(candidates: list[Candidate], *, today: date) -> EvidenceStatus:
    """Arithmetic over what the evidence actually is.

    Two properties of an attachment decide this, and both are facts about the
    attachment rather than opinions about the requirement: whether the excerpt
    is still in date and approved, and whether it is proof of the ask or merely
    related to it. The second only exists once something has read the pair, so
    it is absent — and ignored — whenever verification did not run.

    The first live run made the case for including it. Across a real 58
    requirement bid the model judged 684 candidates and called exactly TWO of
    them proof; 66 were "related, not proof on its own". Counting those 66 as
    proof produced twenty rows reading "current verified / ready" whose own
    rationale said, in the next sentence, that nothing attached actually
    answered the requirement. A matrix cannot say "ready" and "no evidence of
    this" about the same row and be useful to anybody.
    """
    if not candidates:
        return EvidenceStatus.NONE

    # A requirement whose shortlist was read, and where nothing in it was
    # called proof, has partial evidence at best — whatever the dates say. The
    # excerpts stay attached, because "here is the closest material we hold" is
    # worth showing a drafter; they just stop counting as a claim's backing.
    judged = [c for c in candidates if c.support is not None]
    proven = [c for c in judged if c.support is RelevanceVerdict.SUPPORTS]
    related_only = bool(judged) and not proven

    live, historical, expired, conflicting = [], [], [], []
    for candidate in candidates:
        evidence = candidate.evidence
        if evidence.status is KnowledgeStatus.CONFLICTING:
            conflicting.append(candidate)
        elif evidence.expiry_date is not None and evidence.expiry_date < today:
            expired.append(candidate)
        elif evidence.status is KnowledgeStatus.HISTORICAL_VERIFIED:
            historical.append(candidate)
        elif evidence.status is KnowledgeStatus.EXPIRED:
            expired.append(candidate)
        else:
            live.append(candidate)

    if conflicting:
        return EvidenceStatus.CONFLICTING
    if live and (expired or historical):
        return EvidenceStatus.PARTIAL
    if live:
        return EvidenceStatus.PARTIAL if related_only else EvidenceStatus.CURRENT_VERIFIED
    if historical:
        # Real proof, but of the past. Never reported as current.
        return EvidenceStatus.HISTORICAL_VERIFIED
    if expired:
        return EvidenceStatus.EXPIRED
    return EvidenceStatus.NONE


# Categories whose requirements are agreed to rather than proven. Keeping them
# out of the evidence-gap count is what stops a real gap being lost in a list of
# contract boilerplate.
ACCEPTANCE_CATEGORIES = frozenset({RequirementCategory.CONTRACTUAL, RequirementCategory.LEGAL})


# A customer who hands something over -- access, credentials, a service, a
# subscription -- and names what it is FOR. The extractor is told to split
# "Jadwa will provide X to send Y" into the customer's provision and the
# bidder's use of it, and on the live Jadwa tender it did not: R-033 (the Core
# OMS access) carried "create client account", "create beneficiary accounts" and
# "create electronic trading account" inside a row owned by the customer, and
# R-037 and R-042 did the same with SMS and email. Excusing those rows as
# CUSTOMER_OBLIGATION removed the tender's largest integration from every list a
# person reads.
#
# The instruction is in the prompt and the model did not follow it, so the
# control lives here, and it errs in the safe direction: a clause like this is
# never excused, it is sent to a person to confirm whether the bidder has work
# in it. A rights clause ("Jadwa may cancel", "Jadwa will rate proposals on")
# hands nothing over and is still excused.
_HANDS_SOMETHING_OVER = re.compile(
    r"\b(?:will|shall|to|can|may)\s+(?:provide|supply|grant|give|manage|allow|"
    r"make\s+available|arrange)\b|\b(?:access|credentials|connection\s+details|"
    r"subscription)\b",
    re.IGNORECASE,
)
_NAMES_A_PURPOSE = re.compile(
    r"\bfor\s+(?:the\s+)?following\b|\bpurposes?\b|\bused?\s+for\b|"
    r"\bintended\s+use\b|\bso\s+that\b|\bin\s+order\s+to\b|"
    r"\b(?:provide|supply|grant|give|manage)\b[^.]{0,120}\bto\s+(?:send|create|check|verify|"
    r"upload|access|get|generate|process|use)\b",
    re.IGNORECASE,
)


# How a bidder row that USES a handover refers to it: "Using the Core OMS access
# Jadwa provides, the solution shall ...". Recognised by the customer's act being
# named, not by the phrase "Using the", because the model's wording varies.
_REFERS_TO_A_HANDOVER = re.compile(
    r"\b(?:provides?|provided|manages?|managed|supplies|supplied|grants?|granted)\b",
    re.IGNORECASE,
)


def bidder_work_already_extracted(
    session: Session, ctx: ExecutionContext, *, requirement: Requirement, clause_ref: str | None
) -> bool:
    """Has the bidder's use of this handover been extracted as its own requirement?

    The handover guard exists for the case where it has not. After the
    decomposition split R-033 into the customer's access plus five bidder rows,
    the guard kept flagging the customer row as "bidder work was not extracted",
    which was false, and put the same work in Gaps twice.

    A sibling counts only if it is in the SAME clause, is not the customer's,
    and itself refers to something being provided or managed -- the engine and
    Arabic-support rows share the SMS clause and say nothing of the handover, so
    they must not switch the guard off for a clause that was not split.
    """
    if not clause_ref:
        return False
    rows = session.execute(
        select(RequirementVersion.requirement_text)
        .join(Requirement, Requirement.current_version_id == RequirementVersion.id)
        .where(
            Requirement.org_id == ctx.org_id,
            Requirement.bid_id == requirement.bid_id,
            Requirement.id != requirement.id,
            Requirement.obligation_owner != ObligationOwner.CUSTOMER,
            RequirementVersion.clause_ref == clause_ref,
            LIVE_REQUIREMENT,
        )
    ).scalars()
    return any(_REFERS_TO_A_HANDOVER.search(text or "") for text in rows)


def customer_hands_something_over(text: str | None) -> bool:
    """Does this clause give the bidder something and say what to do with it?"""
    body = text or ""
    return bool(_HANDS_SOMETHING_OVER.search(body) and _NAMES_A_PURPOSE.search(body))


def _obligation_owner(requirement: Requirement) -> ObligationOwner:
    """Read the label defensively.

    A requirement extracted before the column existed, or one whose value is
    missing for any other reason, is the bidder's. The default lives here as
    well as in the database because this is the decision it protects.
    """
    return getattr(requirement, "obligation_owner", None) or ObligationOwner.BIDDER


def _nothing_attached_is_proof(candidates: list[Candidate]) -> bool:
    """Was the shortlist read, and did none of it turn out to be proof?

    `_evidence_status` already knows this and folds it into PARTIAL, which is
    right for the evidence axis: material that is merely related is not nothing,
    and a drafter should see the closest thing the company holds. It is wrong
    for the readiness axis, where PARTIAL then means two different situations at
    once -- "some proof, plus some stale material" and "nothing here backs the
    claim" -- and only the first can be drafted from.

    Measured on the live bid: 41 rows read DRAFTABLE, and for 40 of them the
    model had judged every single attachment "related, not proof on its own".
    198 attachments, 5 called proof. The verdict existed on every row and the
    readiness rule never asked for it, so the matrix invited a drafter to write
    a positive answer backed by material the system itself had said does not
    answer the requirement.

    Unjudged candidates are not counted either way: `support is None` means
    verification did not run, and absence of a check is not a finding.
    """
    judged = [c for c in candidates if c.support is not None]
    return bool(judged) and not any(c.support is RelevanceVerdict.SUPPORTS for c in judged)


def distinct_sources(candidates: list[Candidate]) -> int:
    """How many separate documents this requirement can actually draw on.

    The counting rule needs a number of THINGS, and excerpts are not things:
    one project reference produces several rows, and counting rows would let a
    single project satisfy a demand for three. The source document is the
    nearest honest unit of "a separate item the company holds" -- a certificate,
    a CV, a reference letter is one document each -- and it is a proxy, stated
    here rather than hidden: one document describing three projects counts as
    one, and undercounts.

    Undercounting is the direction to be wrong in. The rule only ever
    downgrades; a downgraded row is visible, carries its count and its noun in
    the rationale, and a person can override it through `adjust`. What it
    prevents is a proposal claiming three reference projects and showing one.

    Generous about WHICH candidates count: everything the reader did not throw
    out. UNRELATED means retrieval was wrong and the excerpt says nothing about
    this requirement, so it cannot be one of the three. Anything else -- proof,
    partial proof, or never read at all -- is material a drafter could point at,
    and a shortfall should be claimed only when even that generous count falls
    short.
    """
    return len(
        {
            candidate.evidence.source_document_version_id
            for candidate in candidates
            if candidate.support is not RelevanceVerdict.UNRELATED
        }
    )


def quantity_shortfall(
    version: RequirementVersion | None, candidates: list[Candidate]
) -> tuple[int, int, str] | None:
    """(have, need, noun) when the tender asks for more than the pack holds.

    The whole of the counting rule, and deliberately a pure function of two
    inputs so it can be tested without a database, a model or a bid.

    Returns None when there is nothing to say: no count was read, the count was
    read and there is none, or the company holds enough. `required_count` being
    NULL means "not judged", never "none required", and both lead here to the
    same silence -- a requirement nobody has read for quantity is left exactly
    as the product left it before this rule existed.
    """
    if version is None or not version.required_count:
        return None
    need = int(version.required_count)
    have = distinct_sources(candidates)
    if have >= need:
        return None
    return (have, need, (version.counted_noun or "").strip() or "of these")


def _readiness(
    requirement: Requirement,
    evidence_status: EvidenceStatus,
    *,
    has_open_clarification: bool,
    nothing_is_proof: bool = False,
    proven: bool = False,
    customer_hands_over: bool = False,
) -> tuple[BidReadiness, str]:
    """What has to happen next, given what the evidence shows."""
    if requirement.applicability is RequirementApplicability.NOT_APPLICABLE:
        return BidReadiness.NOT_APPLICABLE, "The requirement's condition does not apply."

    readiness, rationale = _readiness_from_evidence(
        requirement,
        evidence_status,
        nothing_is_proof=nothing_is_proof,
        proven=proven,
        customer_hands_over=customer_hands_over,
    )
    if not has_open_clarification:
        return readiness, rationale

    # An open question DEMOTES a row that would otherwise be answerable. It does
    # not overrule a row that is stuck for a worse reason.
    #
    # Checking it first, as this did, made the question outrank everything:
    # BLOCKED fell from 27 to 9 and NEEDS_CLIENT_CLARIFICATION rose to 92 of 104
    # rows on the live bid -- not because evidence improved, but because those
    # rows acquired a question and the question won. A row with no proof and an
    # open question is not waiting on the customer; it is waiting on the
    # customer AND on us, and the state worth showing is the one naming the work
    # we own.
    #
    # So the evidence verdict is computed first, and the question yields only to
    # the states that mean WE STILL OWE PROOF: BLOCKED, and NEEDS_REFRESH where
    # the only support has expired. Those name work nobody else can do for us,
    # and they must not be hidden behind a question that has not even been sent.
    #
    # Everything else gives way to the question, and the boundary is deliberate.
    # NEEDS_COMPANY_CONFIRMATION on a customer-owned clause or a term to accept
    # is a "someone should read this" note, not a missing proof -- when such a
    # row also carries an open question, the question is the actionable thing.
    # Narrowing this to READY and DRAFTABLE alone got that case wrong.
    if readiness not in (BidReadiness.BLOCKED, BidReadiness.NEEDS_REFRESH):
        return (
            BidReadiness.NEEDS_CLIENT_CLARIFICATION,
            # Not "has been put to the customer". The live bid had 221 open
            # clarifications, zero requests and zero customer responses, while
            # every one of these rows claimed the question had been asked. The
            # system DETECTS a question; a person sends it. Saying otherwise
            # invites someone to wait for an answer nobody requested.
            # And what it does NOT mean. A review read this state as the system
            # refusing to draft, because the label reads like a stop and
            # nothing said otherwise. Nothing in drafting consults readiness:
            # the row can be written now, on what is known, and the question
            # has to be answered before that answer is final.
            "An open clarification about this requirement has been identified and "
            "not yet sent to the customer. A draft can still be written from what "
            "is known; the answer is what must not be treated as final until the "
            "customer replies.",
        )
    return (
        readiness,
        rationale + " An open clarification about this requirement is also outstanding.",
    )


def _everything_attached_is_disqualified(candidates: list[Candidate]) -> bool:
    """Is every attachment something that cannot prove anything?

    Different from `_nothing_attached_is_proof`, which asks what the MODEL made
    of the shortlist. This asks what the excerpts ARE: a blank form, a register
    row that records the proof as missing, an excerpt from a document that
    calls itself fictional. `is_substantiated is False` is written by code and
    by people, never inferred from silence, so an unjudged row is not counted
    here.

    Twelve requirements read DRAFTABLE on a shortlist where every single item
    was marked "the document states of itself that it is fictional material".
    DRAFTABLE says there is something to write from. There was not -- there was
    something to READ, which is a different thing, and what happens next is
    that a person decides whether the company can claim this at all.
    """
    return bool(candidates) and all(c.evidence.is_substantiated is False for c in candidates)


def _something_attached_is_proof(candidates: list[Candidate]) -> bool:
    """Did anything on the shortlist get a positive verdict?

    The mirror of `_nothing_attached_is_proof`, and not its negation: that one
    answers "was it read and found wanting", this one answers "was it read and
    found sufficient". Both are false for an unread shortlist, which is the
    state this distinction exists to name.

    An excerpt its own document disowns is never proof, whatever the reader
    said about the sentence -- see `demonstration_documents`.
    """
    return any(
        c.support is RelevanceVerdict.SUPPORTS and c.evidence.is_substantiated is not False
        for c in candidates
    )


def _readiness_from_evidence(
    requirement: Requirement,
    evidence_status: EvidenceStatus,
    *,
    nothing_is_proof: bool = False,
    proven: bool = False,
    customer_hands_over: bool = False,
) -> tuple[BidReadiness, str]:
    """Where the requirement stands on the evidence alone, clarifications aside.

    Split out so "we have a question" and "we have nothing to answer with" stay
    separable. They are different problems with different owners, and collapsing
    them is what let a question hide a missing proof.
    """
    if requirement.applicability is RequirementApplicability.CONDITIONAL_PENDING:
        return (
            BidReadiness.NEEDS_COMPANY_CONFIRMATION,
            "The requirement is conditional and its condition has not been evaluated.",
        )

    if evidence_status is EvidenceStatus.CONFLICTING:
        return (
            BidReadiness.NEEDS_COMPANY_CONFIRMATION,
            "Supporting evidence contradicts itself and must be resolved before use.",
        )
    if evidence_status is EvidenceStatus.EXPIRED:
        return (
            BidReadiness.NEEDS_REFRESH,
            "The only supporting evidence has expired and must be renewed.",
        )
    if evidence_status is EvidenceStatus.HISTORICAL_VERIFIED:
        return (
            BidReadiness.NEEDS_REFRESH,
            "Evidence proves this was true historically; currency must be confirmed.",
        )
    if evidence_status is EvidenceStatus.PARTIAL:
        # PARTIAL because material was attached and none of it is proof is not
        # a gap to fill while drafting -- there is nothing yet to draft from.
        # Treated exactly as if nothing had been found, because in the sense
        # that matters nothing was: the excerpts stay attached and visible, and
        # they simply stop counting as a claim's backing.
        if nothing_is_proof:
            return _readiness_without_evidence(
                requirement,
                found="Related material is attached, but the model judged none of it "
                "proof of this requirement",
                customer_hands_over=customer_hands_over,
            )
        return (
            BidReadiness.DRAFTABLE,
            "Some current evidence exists; the gaps must be filled during drafting.",
        )
    if evidence_status is EvidenceStatus.NONE:
        return _readiness_without_evidence(
            requirement,
            found="No supporting evidence was found",
            customer_hands_over=customer_hands_over,
        )

    # CURRENT_VERIFIED from here.
    if requirement.category in SIGNATURE_CATEGORIES:
        return (
            BidReadiness.REQUIRES_SIGNATURE,
            "Evidence is current; the requirement is satisfied by a signed submission item.",
        )
    # READY is the only state that claims a drafter has nothing left to
    # resolve, so it asks for a positive judgement rather than the absence of a
    # negative one.
    #
    # R-019 went READY on seven excerpts that nobody and nothing had judged:
    # current, approved, the right type, and never read for whether they
    # actually prove bilingual delivery. "Current and approved" is a statement
    # about an excerpt's lifecycle -- is it ours, is it in date, did someone
    # let it into the library -- and it was standing in for a statement about
    # its content.
    #
    # Unjudged is not demoted to nothing: the row is DRAFTABLE, which is true.
    # There is something to write from, and what is missing is the reading that
    # would let the claim be made without qualification.
    if not proven:
        return (
            BidReadiness.DRAFTABLE,
            "Current, approved evidence is attached, but none of it has been judged "
            "proof of this requirement. A draft can be written from it; the claim "
            "cannot be made unqualified until the evidence is read.",
        )
    return BidReadiness.READY, "Current, approved evidence supports this requirement."


def _readiness_without_evidence(
    requirement: Requirement, *, found: str, customer_hands_over: bool = False
) -> tuple[BidReadiness, str]:
    """Where a requirement stands when nothing attached proves it.

    Shared by "nothing was found" and "material was found and none of it is
    proof", because the bid is in the same position either way, and the reasons
    a missing proof is acceptable -- a clause binding the customer, a term
    accepted rather than evidenced -- do not depend on which of the two
    happened. `found` says which, so the rationale stays truthful about what is
    attached.
    """
    # A clause that binds the CUSTOMER cannot be evidenced by the bidder at
    # all -- "the Buyer may waive any minor non-conformity", "the Authority
    # shall be under no obligation to accept the lowest offer". Eight such rows
    # sat at BLOCKED on the first real bid, burying the four requirements that
    # genuinely put it at risk. Note it and move on; it is still a term someone
    # should read, which is why it lands on a person rather than disappearing.
    #
    # Applied only to a clause the extractor positively labelled CUSTOMER.
    # Everything else, including anything it was unsure about, stays where it
    # was: an obligation wrongly excused is a no-bid risk removed from the red
    # list, and that error must be impossible to make by accident.
    if _obligation_owner(requirement) is ObligationOwner.CUSTOMER:
        if customer_hands_over:
            return (
                BidReadiness.NEEDS_COMPANY_CONFIRMATION,
                "The customer hands something over and the clause says what it is for. "
                "Using it is normally the bidder's work, and it was not extracted as a "
                "requirement of its own: confirm what the company must build or do with it.",
            )
        return (
            BidReadiness.NEEDS_COMPANY_CONFIRMATION,
            "This clause binds the customer, not the bidder: there is nothing to "
            "evidence, but somebody should read and accept it.",
        )
    # A term the bidder accepts is not blocked by missing proof. "Proposals
    # become the property of the customer" cannot be evidenced by any company
    # document, and reporting it as BLOCKED buries the requirements that
    # genuinely lack proof under boilerplate no evidence could ever satisfy.
    # What it needs is a decision to accept, not a document.
    if requirement.category in ACCEPTANCE_CATEGORIES:
        return (
            BidReadiness.NEEDS_COMPANY_CONFIRMATION,
            "A term to accept rather than evidence: it needs a decision, not a document.",
        )
    if requirement.is_mandatory:
        # Still blocked, but say why accurately. A submission mechanic -- notify
        # by a date, use the specified format, keep prices out of the technical
        # envelope -- is not waiting on a document nobody uploaded; it is
        # waiting on somebody doing it.
        if requirement.category in SIGNATURE_CATEGORIES:
            return (
                BidReadiness.BLOCKED,
                "A submission action that has not been done: it is satisfied by "
                "doing and attaching it, not by evidence from the corpus.",
            )
        return (BidReadiness.BLOCKED, f"A mandatory requirement, and {found.lower()}.")
    return (
        BidReadiness.NEEDS_COMPANY_CONFIRMATION,
        f"{found}; a contributor must supply proof.",
    )


def holds_the_row(question: str | None, severity) -> bool:
    """Does this open finding put the row in NEEDS_CLIENT_CLARIFICATION?

    Only a question the customer can answer (`recommended_question`), and only
    when it is not MINOR. Measured on the Wadi Lumen fixture against the real
    Jadwa tender: with questions linked correctly, 22 of 28 rows waiting on the
    customer had current, approved proof, and most of what held them was MINOR
    -- "what acceptance criteria apply to the knowledge transfer?", "which
    browser versions?". Good things to ask; nothing a drafter must wait for.
    The detector returns its cap of 12 on every batch, so it pads with exactly
    these. A MINOR question stays visible on the row (it is counted in
    `open_clarification` and named in the rationale); it just does not hold it.
    """
    if not (question or "").strip():
        return False
    return str(getattr(severity, "value", severity)) != ClarificationSeverity.MINOR.value


def assess_requirement(
    session: Session,
    ctx: ExecutionContext,
    *,
    requirement: Requirement,
    today: date | None = None,
    verifier: RelevanceVerifier | None = None,
) -> RequirementAssessment:
    moment = today or date.today()
    # A bid-wide run passes one verifier so a provider outage latches off once
    # rather than being rediscovered on every requirement.
    verifier = verifier if verifier is not None else RelevanceVerifier()
    verifier.begin_requirement()
    checked_before = verifier.checked
    version = (
        session.get(RequirementVersion, requirement.current_version_id)
        if requirement.current_version_id
        else None
    )
    text_query = version.requirement_text if version else ""

    # A clause binding the customer is not proven by anything the company holds,
    # so nothing is attached to it: evidence "supporting" a term the bidder does
    # not owe is noise on a row that is not the bidder's to work.
    hands_over = _obligation_owner(
        requirement
    ) is ObligationOwner.CUSTOMER and customer_hands_something_over(text_query)
    # A handover whose bidder half was extracted as its own requirement is no
    # longer hiding anything: the customer row is context and the work is
    # counted on the rows that state it.
    split_out = hands_over and bidder_work_already_extracted(
        session,
        ctx,
        requirement=requirement,
        clause_ref=getattr(version, "clause_ref", None),
    )
    binds_only_the_customer = (
        _obligation_owner(requirement) is ObligationOwner.CUSTOMER and not hands_over or split_out
    )
    if (
        requirement.applicability is RequirementApplicability.NOT_APPLICABLE
        or binds_only_the_customer
    ):
        candidates: list[Candidate] = []
    else:
        candidates = _candidates_for(
            session, ctx, requirement=requirement, text_query=text_query, verifier=verifier
        )

    # Two counts, because "this finding concerns this requirement" and "we are
    # waiting on the customer" are different questions and were the same query.
    #
    # Anything linked to a requirement made it read NEEDS_CLIENT_CLARIFICATION,
    # so a detector could either be visible or be honest, not both. The identity
    # drift detector chose invisible, and says so in its own comment: linking it
    # once put 51 of 59 rows into "an open clarification blocks a reliable
    # answer" over notes about our own re-extraction, which is nothing anyone
    # could ask the customer. The coverage and ambiguity detectors then followed
    # the same path, and the result is 918 clarifications of which zero reach
    # the row they concern.
    #
    # The distinguishing fact is already stored: `recommended_question`. A
    # finding that carries one is a question that can be put to the customer and
    # answered; a finding without one is an internal note about our own
    # processing. Only the first can be what a requirement is waiting on.
    linked_open = (
        select(
            ClarificationIssue.id,
            ClarificationIssue.recommended_question,
            ClarificationIssue.severity,
        )
        .join(
            _clarification_requirement_table(),
            _clarification_requirement_table().c.clarification_id == ClarificationIssue.id,
        )
        .where(
            ClarificationIssue.org_id == ctx.org_id,
            _clarification_requirement_table().c.requirement_id == requirement.id,
            ClarificationIssue.status.in_(
                [ClarificationStatus.OPEN, ClarificationStatus.SENT_TO_CLIENT]
            ),
        )
    )
    linked = session.execute(linked_open).all()
    open_clarification = len(linked)
    awaiting_customer = sum(
        1 for _id, question, severity in linked if holds_the_row(question, severity)
    )
    minor_questions = sum(
        1
        for _id, question, severity in linked
        if (question or "").strip() and not holds_the_row(question, severity)
    )

    evidence_status = _evidence_status(candidates, today=moment)
    readiness, rationale = _readiness(
        requirement,
        evidence_status,
        # A gap in the tender does not change what the company can prove. It is
        # named on the row (below) and the owner records an assumption for it.
        has_open_clarification=False,
        nothing_is_proof=_nothing_attached_is_proof(candidates),
        proven=_something_attached_is_proof(candidates),
        customer_hands_over=hands_over and not split_out,
    )
    # Owner first, then everything else. Only a clause the extractor positively
    # labelled CUSTOMER lands here (see `_obligation_owner`): anything it was
    # unsure about stays the bidder's, so a bidder duty cannot be excused by
    # accident. An open question about a customer clause still lives in the
    # clarification register and is still sent; it just does not make this row
    # wait, because nothing the bidder does depends on the answer.
    if requirement.applicability is RequirementApplicability.ACTIVE and binds_only_the_customer:
        readiness = BidReadiness.CUSTOMER_OBLIGATION
        rationale = (
            "This clause binds the customer, not the bidder: there is nothing to "
            "evidence, confirm or draft. It is recorded so it can be read."
            if not split_out
            else "The customer hands something over here. What the bidder does with it is "
            "stated in its own requirement(s) in the same clause, which carry the work; "
            "this row is recorded so the dependency can be read."
        )

    # The comparison is appended to the computed rationale, never substituted
    # for it. A reader sees what the system concluded and, after it, what the
    # model saw — in that order, so the sentence that carries authority is the
    # one that computed the verdict.
    gaps = awaiting_customer + minor_questions
    if gaps:
        rationale = (
            f"{rationale} {gaps} unresolved gap(s) in the tender concern this requirement; "
            "the owning department should record the assumption the response will rest on."
        )

    fit = verifier.last_fit
    dissent = _dissent(fit, evidence_status)
    if dissent:
        verifier.disagreements += 1
        rationale = f"{rationale} {dissent}"
    elif fit is not None and fit.gap.strip():
        rationale = f"{rationale} Gap noted: {fit.gap.strip()}"

    # A reading of NOT_COVERED moves the requirement to a person, and this is
    # the axis it belongs on. `evidence_status` stays a statement about the
    # attached excerpts — their approval, their dates, whether any is proof.
    # `bid_readiness` is the "what happens next" axis, and when the material on
    # file does not answer the ask, what happens next is that somebody decides
    # whether the company can claim this at all. The two are not being
    # collapsed: the row still reports honestly what evidence it holds.
    #
    # A clarification already outranks everything here, so it is left alone —
    # an unanswered question to the customer is a harder blocker than a gap in
    # our own material, and overwriting it would hide the more urgent one.
    if fit is not None and readiness not in _READINESS_FLOOR:
        if fit.coverage is Coverage.NOT_COVERED:
            readiness = BidReadiness.NEEDS_COMPANY_CONFIRMATION
        elif fit.coverage is Coverage.PARTIALLY_COVERED and readiness is BidReadiness.READY:
            # Milder than NOT_COVERED and wrong in the same direction. R-033 on
            # the live bid held one excerpt the model called proof, so the
            # arithmetic said CURRENT_VERIFIED and the row read "ready" — while
            # the reading said the excerpt anchors validity to proposal
            # submission rather than to tender opening, which is a different
            # commitment. "Ready" claims a drafter has nothing left to resolve.
            # DRAFTABLE says what is true: there is something to write from, and
            # a gap to close while writing.
            readiness = BidReadiness.DRAFTABLE

    # Counting, last, and after every reading has had its say.
    #
    # R-006 is the case this exists for. The tender asks for three comparable
    # GCC projects, the pack holds one, and the row read DRAFTABLE with "Some
    # current evidence exists; the gaps must be filled during drafting". Each
    # clause of that is correct and the conclusion is false: two of the three
    # projects do not exist, and drafting will not produce them.
    #
    # It runs last because it is not an opinion to be weighed against the
    # others. The reader's job was to say what the excerpts ARE, and it did that
    # correctly -- one completed project IS partial proof of a demand for three,
    # which is exactly why teaching the relevance prompt to say otherwise made
    # its verdicts flap. The shortfall is arithmetic over the answer, and
    # arithmetic does not negotiate with a judgement.
    #
    # `_READINESS_FLOOR` is respected for the same reason the coverage
    # adjustment respects it: an unanswered question to the customer outranks a
    # gap in our own material, and overwriting it would hide the more urgent
    # problem behind the less urgent one.
    # Nothing admissible is attached, whatever the arithmetic said.
    #
    # Runs beside the shortfall rule and for the same reason: it is a fact
    # about what the pack holds, not an opinion to be weighed. `_READINESS_FLOOR`
    # is respected so an open customer question still outranks it.
    # REQUIRES_SIGNATURE is exempt, and the exemption is not an edge case.
    #
    # That state does not mean "the evidence proves this". It means the
    # requirement is satisfied by someone signing the submission, and no
    # excerpt was ever going to be the thing that satisfies it. Overriding it
    # sent R-009 -- proposals must be signed by an authorised representative --
    # to NEEDS_COMPANY_CONFIRMATION, which asks a person to decide whether the
    # company can claim something it does not need to claim.
    _NOT_WAITING_ON_PROOF = _READINESS_FLOOR | {BidReadiness.REQUIRES_SIGNATURE}
    if _everything_attached_is_disqualified(candidates) and readiness not in _NOT_WAITING_ON_PROOF:
        readiness = BidReadiness.NEEDS_COMPANY_CONFIRMATION
        rationale = (
            f"{rationale} Every attached excerpt is marked as unable to prove anything "
            "-- a blank form, a register of what is missing, or a document that says of "
            "itself that it is not real. Somebody must decide whether the company can "
            "claim this at all."
        )

    shortfall = quantity_shortfall(version, candidates)
    if shortfall is not None and readiness not in _READINESS_FLOOR:
        have, need, noun = shortfall
        readiness = BidReadiness.BLOCKED
        rationale = (
            f"{rationale} The tender asks for {need} {noun} and the approved pack "
            f"evidences {have}. This is a shortfall in what the company holds, not a "
            "gap that can be closed while drafting."
        )

    previous = session.execute(
        select(RequirementAssessment).where(
            RequirementAssessment.org_id == ctx.org_id,
            RequirementAssessment.requirement_id == requirement.id,
            RequirementAssessment.is_current.is_(True),
        )
    ).scalar_one_or_none()

    next_version = (previous.version + 1) if previous else 1
    if previous is not None:
        previous.is_current = False
        session.flush()

    assessment = RequirementAssessment(
        org_id=ctx.org_id,
        requirement_id=requirement.id,
        requirement_version_id=requirement.current_version_id,
        bid_id=requirement.bid_id,
        evidence_status=evidence_status,
        bid_readiness=readiness,
        rationale=rationale,
        assessment_state=AssessmentState.COMPUTED,
        version=next_version,
        is_current=True,
        computed_at=datetime.now(timezone.utc),
        computed_by="assessment_engine@v1",
        reasoning_trace={
            "candidates": [
                {
                    "evidence_id": str(c.evidence.id),
                    "score": c.score,
                    "rank": c.rank,
                    "status": c.evidence.status.value,
                    "expiry_date": c.evidence.expiry_date.isoformat()
                    if c.evidence.expiry_date
                    else None,
                }
                for c in candidates
            ],
            "open_clarifications": open_clarification,
            # Which reader decided this shortlist. Named and versioned like
            # every other model output, so a change of prompt is visible in the
            # history rather than appearing as the evidence having changed.
            "verified_by": (
                f"llm:evidence_relevance@{registry.get('evidence_relevance').prompt_version}"
                if verifier.checked > checked_before
                else None
            ),
            # The side-by-side reading, stored whole. It is the answer to "why
            # does this row say that", and reconstructing it later from the
            # attachments is not possible — the gap in particular is a statement
            # about what is ABSENT, and nothing else in the record holds it.
            "fit": fit.model_dump(mode="json") if fit is not None else None,
            "model_dissents": bool(dissent),
        },
    )
    session.add(assessment)
    session.flush()

    for candidate in candidates:
        session.add(
            AssessmentEvidence(
                org_id=ctx.org_id,
                assessment_id=assessment.id,
                evidence_id=candidate.evidence.id,
                role=candidate.role,
                weight=candidate.score,
                contribution_note=candidate.note,
                retrieval_rank=candidate.rank,
                rerank_score=candidate.score,
            )
        )
    session.flush()
    return assessment


def _clarification_requirement_table():
    from app.models import ClarificationRequirement

    return ClarificationRequirement.__table__


def _evidence_pool(session: Session, ctx: ExecutionContext) -> dict[str, int]:
    """What the matcher actually has available, in three numbers.

    `total` is everything extracted; `approved` is what may back a claim at
    all; `embedded` is what semantic matching can reach. A gap between them
    points at a specific, different remedy: extract more, approve what is
    there, or run the embedding backfill.
    """
    total = session.execute(
        select(func.count()).select_from(Evidence).where(Evidence.org_id == ctx.org_id)
    ).scalar_one()
    approved = session.execute(
        select(func.count())
        .select_from(Evidence)
        .where(Evidence.org_id == ctx.org_id, *_admissible())
    ).scalar_one()
    embedded = session.execute(
        select(func.count())
        .select_from(Evidence)
        .where(
            Evidence.org_id == ctx.org_id,
            *_admissible(),
            Evidence.excerpt_vector.is_not(None),
        )
    ).scalar_one()
    return {"total": total, "approved": approved, "embedded": embedded}


def assess_bid(
    session: Session, ctx: ExecutionContext, *, bid_id: uuid.UUID, today: date | None = None
) -> AssessmentReport:
    rows = (
        session.execute(
            select(Requirement)
            .where(
                Requirement.org_id == ctx.org_id,
                Requirement.bid_id == bid_id,
                LIVE_REQUIREMENT,
            )
            .order_by(Requirement.code)
        )
        .scalars()
        .all()
    )
    if not rows:
        # Seen live: "Requirement assessment completed." over an empty matrix,
        # because the tender analysis had never produced a requirement to assess.
        from app.core.errors import StateConflict

        raise StateConflict(
            "This bid has no requirements yet, so there is nothing to assess. "
            "Run the tender analysis on the Tender package screen first; "
            "requirements come from that step."
        )
    report = AssessmentReport(evidence_pool=_evidence_pool(session, ctx))
    # Reported before the loop and logged once, so a run against an empty or
    # unapproved corpus says so plainly instead of producing 58 identical
    # "no supporting evidence" verdicts that look like a broken matcher.
    log.info("assessment_started", bid_id=str(bid_id), **report.evidence_pool)
    # One verifier for the whole bid: its counters are the run's summary, and a
    # provider failure switches it off once rather than being retried per
    # requirement.
    verifier = RelevanceVerifier()
    for requirement in rows:
        assessment = assess_requirement(
            session, ctx, requirement=requirement, today=today, verifier=verifier
        )
        report.assessed += 1
        if assessment.bid_readiness is BidReadiness.NOT_APPLICABLE:
            report.skipped_not_applicable += 1
        report.by_readiness[assessment.bid_readiness.value] = (
            report.by_readiness.get(assessment.bid_readiness.value, 0) + 1
        )
        report.by_evidence_status[assessment.evidence_status.value] = (
            report.by_evidence_status.get(assessment.evidence_status.value, 0) + 1
        )

    report.verification = verifier.counters()

    # Group the assessed requirements into department tasks. Idempotent: any
    # requirement already covered by a task is skipped, so re-running after an
    # amendment tops the board up and leaves existing tasks -- with their
    # owners, conversations and drafts -- untouched.
    #
    # Deliberately no owner is chosen. Grouping is a fact about the tender
    # (which department owns this category of work); naming the person is a
    # judgement about a team, and belongs to that department's head.
    from app.services import tasks as task_service

    created = task_service.generate_tasks(session, ctx, bid_id=bid_id)
    seeded = task_service.seed_initial_states(session, ctx, bid_id=bid_id)
    # A task the evidence says is ready opens with a draft already in it.
    from app.services import drafting

    prepared = drafting.prepare_ready_drafts(session, ctx, bid_id=bid_id)
    report.tasks = {"created": len(created), "initial_states": seeded, "drafts": prepared}

    log.info(
        "assessment_finished",
        bid_id=str(bid_id),
        tasks_created=len(created),
        **report.verification,
    )

    audit.record(
        session,
        ctx,
        action="assessment.completed",
        entity_type="bid",
        entity_id=bid_id,
        bid_id=bid_id,
        after=report.as_dict(),
    )
    return report


def adjust(
    session: Session,
    ctx: ExecutionContext,
    *,
    requirement_id: uuid.UUID,
    evidence_status: EvidenceStatus | None = None,
    bid_readiness: BidReadiness | None = None,
    reason: str,
) -> RequirementAssessment:
    """A human overrides the computed verdict. Reason is mandatory."""
    if not (reason or "").strip():
        raise ValidationError("Adjusting an assessment requires a reason.")
    assessment = current_assessment(session, ctx, requirement_id=requirement_id)
    if assessment is None:
        raise NotFound("This requirement has not been assessed yet.")

    before = {
        "evidence_status": assessment.evidence_status.value,
        "bid_readiness": assessment.bid_readiness.value,
    }
    if evidence_status is not None:
        assessment.evidence_status = evidence_status
    if bid_readiness is not None:
        assessment.bid_readiness = bid_readiness
    assessment.assessment_state = AssessmentState.HUMAN_ADJUSTED
    assessment.human_adjusted_by = ctx.actor_user_id
    assessment.human_adjustment_reason = reason
    session.flush()

    audit.record(
        session,
        ctx,
        action="assessment.adjusted",
        entity_type="requirement_assessment",
        entity_id=assessment.id,
        bid_id=assessment.bid_id,
        before=before,
        after={
            "evidence_status": assessment.evidence_status.value,
            "bid_readiness": assessment.bid_readiness.value,
        },
        reason=reason,
    )
    return assessment


def mark_stale(
    session: Session,
    ctx: ExecutionContext,
    *,
    requirement_ids: list[uuid.UUID],
    reason: str,
) -> int:
    """Flag assessments for recomputation without discarding the prior verdict."""
    if not requirement_ids:
        return 0
    rows = (
        session.execute(
            select(RequirementAssessment).where(
                RequirementAssessment.org_id == ctx.org_id,
                RequirementAssessment.requirement_id.in_(requirement_ids),
                RequirementAssessment.is_current.is_(True),
            )
        )
        .scalars()
        .all()
    )
    for assessment in rows:
        assessment.assessment_state = AssessmentState.STALE
        assessment.stale_reason = reason
    session.flush()
    return len(rows)


def current_assessment(
    session: Session, ctx: ExecutionContext, *, requirement_id: uuid.UUID
) -> RequirementAssessment | None:
    return session.execute(
        select(RequirementAssessment).where(
            RequirementAssessment.org_id == ctx.org_id,
            RequirementAssessment.requirement_id == requirement_id,
            RequirementAssessment.is_current.is_(True),
        )
    ).scalar_one_or_none()


def assessment_history(
    session: Session, ctx: ExecutionContext, *, requirement_id: uuid.UUID
) -> list[RequirementAssessment]:
    return list(
        session.execute(
            select(RequirementAssessment)
            .where(
                RequirementAssessment.org_id == ctx.org_id,
                RequirementAssessment.requirement_id == requirement_id,
            )
            .order_by(RequirementAssessment.version)
        )
        .scalars()
        .all()
    )


def evidence_for(
    session: Session, ctx: ExecutionContext, *, assessment_id: uuid.UUID
) -> list[tuple[AssessmentEvidence, Evidence]]:
    rows = session.execute(
        select(AssessmentEvidence, Evidence)
        .join(Evidence, Evidence.id == AssessmentEvidence.evidence_id)
        .where(
            AssessmentEvidence.org_id == ctx.org_id,
            AssessmentEvidence.assessment_id == assessment_id,
        )
        .order_by(AssessmentEvidence.retrieval_rank)
    ).all()
    return [(link, evidence) for link, evidence in rows]


def attach_evidence(
    session: Session,
    ctx: ExecutionContext,
    *,
    requirement_id: uuid.UUID,
    evidence_id: uuid.UUID,
    role: AssessmentEvidenceRole = AssessmentEvidenceRole.SUPPORTING,
    note: str | None = None,
) -> AssessmentEvidence:
    assessment = current_assessment(session, ctx, requirement_id=requirement_id)
    if assessment is None:
        raise NotFound("This requirement has not been assessed yet.")
    existing = session.execute(
        select(AssessmentEvidence).where(
            AssessmentEvidence.org_id == ctx.org_id,
            AssessmentEvidence.assessment_id == assessment.id,
            AssessmentEvidence.evidence_id == evidence_id,
            AssessmentEvidence.role == role,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    link = AssessmentEvidence(
        org_id=ctx.org_id,
        assessment_id=assessment.id,
        evidence_id=evidence_id,
        role=role,
        contribution_note=note or "Attached by a contributor.",
    )
    session.add(link)
    session.flush()
    audit.record(
        session,
        ctx,
        action="assessment.evidence_attached",
        entity_type="requirement_assessment",
        entity_id=assessment.id,
        bid_id=assessment.bid_id,
        after={"evidence_id": str(evidence_id), "role": role.value},
    )
    return link


# --- department recommendation ----------------------------------------------


def recommend_departments(session: Session, ctx: ExecutionContext, *, bid_id: uuid.UUID) -> int:
    """Suggest an owning department per requirement. A suggestion only."""
    departments = {
        d.code: d
        for d in session.execute(select(Department).where(Department.org_id == ctx.org_id))
        .scalars()
        .all()
    }
    rows = (
        session.execute(
            select(Requirement).where(
                Requirement.org_id == ctx.org_id,
                Requirement.bid_id == bid_id,
                Requirement.recommended_department_id.is_(None),
            )
        )
        .scalars()
        .all()
    )
    from app.services import category_routing

    route_table = category_routing.routes(session, ctx)
    assigned = 0
    for requirement in rows:
        code = route_table.get(requirement.category)
        department = departments.get(code) if code else None
        if department is None:
            continue
        requirement.recommended_department_id = department.id
        assigned += 1
    session.flush()
    if assigned:
        audit.record(
            session,
            ctx,
            action="assessment.departments_recommended",
            entity_type="bid",
            entity_id=bid_id,
            bid_id=bid_id,
            after={"assigned": assigned},
        )
    return assigned
