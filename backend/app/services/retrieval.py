"""Hybrid retrieval over chunks and the knowledge graph.

The single most security-sensitive component in the product: it is the one path
from a question to the corpus, and it serves both the UI search screen and (from
Phase J) the task Copilot. Three properties are non-negotiable.

**Filters run inside the SQL, never after ranking.** Tenant, permission,
classification and scope predicates are part of the statement that touches the
vector index. Filtering a ranked list afterwards leaks through result counts and
is one refactor away from not happening at all.

**One permission implementation.** Candidate admissibility is decided by the
same `PermissionEvaluator` the API uses, so what the assistant can cite can
never drift from what the user can open.

**Nothing admissible, nothing returned.** When a query finds no permitted
candidates the service says which department owns the material instead of
silently returning an empty list, so the caller can offer the cross-department
request flow rather than a dead end.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field

from sqlalchemy import Select, and_, func, literal, or_, select, text
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.core.config import get_settings
from app.core.context import ExecutionContext
from app.core.logging import get_logger
from app.core.permissions import CLASS_PERMISSION, Perm
from app.models import AccessGrant, DocumentChunk, KnowledgeEntity, KnowledgeFact
from app.models.enums import (
    ConfidentialityClass,
    DocumentSide,
    GrantResourceType,
    KnowledgeStatus,
)
from app.services.permission_evaluator import active_grant_clause

# Classes anyone with basic read access may see. Anything else needs either a
# role permission or a live task-scoped grant.
OPEN_CLASSES = (ConfidentialityClass.PUBLIC, ConfidentialityClass.INTERNAL)

# Which classification each restricted permission unlocks.
log = get_logger("retrieval")

_STOP_WORDS = {
    "that", "this", "with", "from", "will", "have", "which", "their", "what", "when",
    "where", "does", "shall", "must", "should", "would", "could", "about", "into", "your",
    "than", "then", "them", "they", "been", "being", "also", "such", "each", "more",
}

# Set once the embedding model has failed to load; see `_vector_candidates`.
_EMBEDDER_UNUSABLE = False

# information_schema's name for each column type, and what it implies. A
# pgvector column is `vector`; the portable fallback is a real[] array, which
# postgres reports as `_float4`.
_BACKEND_BY_UDT = {"vector": "pgvector", "_float4": "portable", "_float8": "portable"}
_BACKEND_CACHE: dict[str, str] = {}


def _detect_vector_backend(session: Session) -> str | None:
    """Read the embedding column's type. Cached per database, None if unknown.

    Cheap enough to be worth doing rather than trusting a flag -- one query per
    database URL for the life of the process. A failure here is not fatal: the
    caller falls back to the configured value, which is exactly the behaviour
    that existed before, so a database this cannot introspect is no worse off.
    """
    bind = session.get_bind()
    key = str(getattr(bind, "url", "unknown"))
    cached = _BACKEND_CACHE.get(key)
    if cached:
        return cached
    try:
        udt = session.execute(
            text(
                "SELECT udt_name FROM information_schema.columns "
                "WHERE table_name = 'embedding' AND column_name = 'vector'"
            )
        ).scalar_one_or_none()
    except Exception:  # noqa: BLE001 - detection is best-effort by design
        return None
    backend = _BACKEND_BY_UDT.get(udt or "")
    if backend:
        _BACKEND_CACHE[key] = backend
    return backend


_PERMISSION_FOR_CLASS: dict[ConfidentialityClass, Perm] = {
    ConfidentialityClass.RESTRICTED: Perm.PRICING_READ,
    ConfidentialityClass.PRIVILEGED: Perm.LEGAL_PRIVILEGED_READ,
}

# Metadata boosts. A "customer references" query should surface the reference
# TABLE, not a paragraph in a CV that happens to mention a client -- which is
# only possible because ingestion preserved table names and heading paths.
TABLE_BOOST = 0.15
HEADING_MATCH_BOOST = 0.12
CLAUSE_MATCH_BOOST = 0.10
RRF_K = 60


@dataclass
class RetrievalScope:
    """The bounds a task places on retrieval.

    Broader than "evidence already attached to this task", far narrower than
    "the whole company database".
    """

    task_id: uuid.UUID | None = None
    sides: tuple[DocumentSide, ...] = (DocumentSide.COMPANY,)
    # A CEILING a task may impose, not a default restriction. Left at the top of
    # the scale, admissibility is decided purely by what the caller is entitled
    # to see; a lower value lets a task deliberately narrow below that. Capping
    # by default would hide content a user IS entitled to and make them believe
    # the organization does not hold it -- a worse failure than showing it.
    max_confidentiality: ConfidentialityClass = ConfidentialityClass.PRIVILEGED
    bid_ids: tuple[uuid.UUID, ...] = ()
    document_version_ids: tuple[uuid.UUID, ...] = ()

    def describe(self) -> dict:
        return {
            "task_id": str(self.task_id) if self.task_id else None,
            "sides": [s.value for s in self.sides],
            "max_confidentiality": self.max_confidentiality.value,
        }


@dataclass
class Candidate:
    chunk_id: uuid.UUID
    document_version_id: uuid.UUID
    text: str
    score: float
    vector_rank: int | None = None
    text_rank: int | None = None
    heading_path: list[str] = field(default_factory=list)
    clause_ref: str | None = None
    table_name: str | None = None
    page_from: int | None = None
    page_to: int | None = None
    why_matched: list[str] = field(default_factory=list)

    def source_locator(self) -> dict:
        from app.schemas.source_locator import SourceLocator

        return SourceLocator(
            document_version_id=self.document_version_id,
            heading_path=self.heading_path,
            clause_ref=self.clause_ref,
            table_name=self.table_name,
            page_from=self.page_from,
            page_to=self.page_to,
        ).as_json()


@dataclass
class RetrievalResult:
    candidates: list[Candidate]
    filters_applied: dict
    admissible_before_ranking: int
    out_of_scope: bool = False
    owning_departments: list[str] = field(default_factory=list)


class RetrievalService:
    def __init__(self, session: Session, ctx: ExecutionContext) -> None:
        self.session = session
        self.ctx = ctx

    # -- the permission predicate ------------------------------------------
    def _admissibility_clause(self, scope: RetrievalScope):
        """A SQL predicate expressing everything this caller may read.

        Composed into the candidate query itself so an inadmissible chunk is
        never a candidate, rather than being dropped after scoring.
        """
        clauses = [DocumentChunk.org_id == self.ctx.org_id]

        if scope.sides:
            clauses.append(DocumentChunk.side.in_(list(scope.sides)))
        if scope.bid_ids:
            # "Belongs to these bids, or to no bid at all."
            #
            # A plain `IN (versions of these bids)` was wrong in the other
            # direction: company documents carry no bid_id, so naming a bid
            # silently hid the entire reusable knowledge base and the search
            # returned only tender text. The asymmetry is deliberate and is the
            # whole point -- company knowledge is org-wide because reuse across
            # bids is the asset; tender text belongs to exactly one bid and must
            # never appear while working another.
            clauses.append(
                or_(
                    DocumentChunk.document_version_id.in_(
                        self._version_ids_for_bids(scope.bid_ids)
                    ),
                    DocumentChunk.document_version_id.in_(self._version_ids_without_bid()),
                )
            )
        if scope.document_version_ids:
            clauses.append(DocumentChunk.document_version_id.in_(list(scope.document_version_ids)))

        # A company document that has been replaced by a newer version, or withdrawn,
        # no longer speaks for the company. Without this, uploading the real version of
        # a document left the old one searchable and the old claims kept reaching drafts.
        from app.models import Document

        live_company_versions = select(Document.current_version_id).where(
            Document.org_id == self.ctx.org_id,
            Document.side == DocumentSide.COMPANY,
            Document.withdrawn_at.is_(None),
            Document.current_version_id.is_not(None),
        )
        clauses.append(
            or_(
                DocumentChunk.side != DocumentSide.COMPANY,
                DocumentChunk.document_version_id.in_(live_company_versions),
            )
        )

        # Open classes require only basic read access.
        permitted_classes: list[ConfidentialityClass] = []
        if Perm.KNOWLEDGE_READ.value in self.ctx.permissions or (
            Perm.DOCUMENT_READ.value in self.ctx.permissions
        ):
            permitted_classes.extend(OPEN_CLASSES)
            permitted_classes.append(ConfidentialityClass.CONFIDENTIAL)

        # Restricted classes need the matching role permission.
        for klass, needed in _PERMISSION_FOR_CLASS.items():
            if needed.value in self.ctx.permissions:
                permitted_classes.append(klass)
        for klass_name, needed in CLASS_PERMISSION.items():
            if needed.value in self.ctx.permissions and klass_name == "PERSONAL_DATA":
                permitted_classes.append(ConfidentialityClass.RESTRICTED)

        allowed = [
            c
            for c in dict.fromkeys(permitted_classes)
            if _class_rank(c) <= _class_rank(scope.max_confidentiality)
        ]

        class_clause: ColumnElement[bool] = (
            DocumentChunk.confidentiality_class.in_(allowed) if allowed else literal(False)
        )

        # ... or a live, task-scoped grant over the chunk's document version.
        if scope.task_id is not None and self.ctx.actor_user_id is not None:
            granted_versions = select(AccessGrant.resource_id).where(
                AccessGrant.org_id == self.ctx.org_id,
                AccessGrant.grantee_user_id == self.ctx.actor_user_id,
                AccessGrant.task_id == scope.task_id,
                AccessGrant.resource_type == GrantResourceType.DOCUMENT_VERSION,
                active_grant_clause(),
            )
            class_clause = or_(
                class_clause,
                DocumentChunk.document_version_id.in_(granted_versions),
            )

        clauses.append(class_clause)
        return and_(*clauses)

    def _version_ids_for_bids(self, bid_ids) -> Select:
        from app.models import Document, DocumentVersion

        return (
            select(DocumentVersion.id)
            .join(Document, Document.id == DocumentVersion.document_id)
            .where(Document.org_id == self.ctx.org_id, Document.bid_id.in_(list(bid_ids)))
        )

    def _version_ids_without_bid(self) -> Select:
        """Documents belonging to no bid -- the company's own corpus.

        These are the reusable half: certifications, CVs, past performance,
        policies. They are org-wide on purpose, so a bid filter must not
        exclude them.
        """
        from app.models import Document, DocumentVersion

        return (
            select(DocumentVersion.id)
            .join(Document, Document.id == DocumentVersion.document_id)
            .where(Document.org_id == self.ctx.org_id, Document.bid_id.is_(None))
        )

    # -- candidate generation ----------------------------------------------
    def search(
        self,
        query: str,
        *,
        scope: RetrievalScope | None = None,
        limit: int = 10,
        candidate_pool: int = 60,
        embedder=None,
    ) -> RetrievalResult:
        scope = scope or RetrievalScope()
        admissible = self._admissibility_clause(scope)

        total_admissible = self.session.execute(
            select(func.count()).select_from(DocumentChunk).where(admissible)
        ).scalar_one()

        if total_admissible == 0:
            return RetrievalResult(
                candidates=[],
                filters_applied=self._filters_summary(scope),
                admissible_before_ranking=0,
                out_of_scope=True,
                owning_departments=self._likely_owners(scope),
            )

        text_hits = self._text_candidates(query, admissible, candidate_pool)
        vector_hits = self._vector_candidates(query, admissible, candidate_pool, embedder)

        fused = self._fuse(query, text_hits, vector_hits)
        fused.sort(key=lambda c: c.score, reverse=True)
        return RetrievalResult(
            candidates=fused[:limit],
            filters_applied=self._filters_summary(scope),
            admissible_before_ranking=total_admissible,
        )

    def _text_candidates(self, query: str, admissible, pool: int) -> list[Candidate]:
        rows = self._text_rows(func.plainto_tsquery("simple", query), admissible, pool)
        if not rows:
            # A question put in full sentences rarely has every word in one passage.
            # When nothing matches all of its words, match on any of its distinctive
            # words and let the ranking put the best passages first.
            words = {
                w for w in re.findall(r"[a-z0-9]{4,}", query.lower()) if w not in _STOP_WORDS
            }
            if words:
                rows = self._text_rows(
                    func.to_tsquery("simple", " | ".join(sorted(words))), admissible, pool
                )
        return [
            _to_candidate(row[0], text_rank=position, why=["keyword match"])
            for position, row in enumerate(rows, start=1)
        ]

    def _text_rows(self, ts_query, admissible, pool: int):
        rank = func.ts_rank(DocumentChunk.text_tsv, ts_query)
        stmt = (
            select(DocumentChunk, rank.label("rank"))
            .where(admissible, DocumentChunk.text_tsv.op("@@")(ts_query))
            .order_by(rank.desc())
            .limit(pool)
        )
        return self.session.execute(stmt).all()

    def _vector_candidates(self, query: str, admissible, pool: int, embedder) -> list[Candidate]:
        provider = embedder
        if provider is None:
            from app.gateway.gateway import build_gateway

            provider = build_gateway().embeddings
        if provider is None:
            return []

        from app.models import Embedding

        global _EMBEDDER_UNUSABLE
        if _EMBEDDER_UNUSABLE:
            return []
        try:
            vector = provider.embed_one(query)
        except (OSError, MemoryError, RuntimeError, ImportError) as exc:
            # The model cannot be loaded here (Windows "paging file is too small",
            # out of memory, a missing library). Keyword search still answers, so a
            # machine that cannot hold the model degrades to fewer matches instead
            # of failing every feature that searches. Remembered, so the slow load
            # is not retried on every request until the server restarts.
            _EMBEDDER_UNUSABLE = True
            log.warning("embedder_unusable_keyword_search_only", error=str(exc)[:200])
            return []

        # Only vectors from the active model. A partially backfilled corpus
        # degrades to fewer candidates, never to nonsense distances.
        same_model = (
            admissible,
            Embedding.org_id == self.ctx.org_id,
            Embedding.model_id == provider.model_id,
        )

        if self._vector_backend() == "portable":
            return self._portable_vector_candidates(vector, same_model, pool)

        distance = Embedding.vector.cosine_distance(vector)
        stmt = (
            select(DocumentChunk, distance.label("distance"))
            .join(Embedding, Embedding.chunk_id == DocumentChunk.id)
            .where(*same_model)
            .order_by(distance)
            .limit(pool)
        )
        out: list[Candidate] = []
        for position, row in enumerate(self.session.execute(stmt).all(), start=1):
            out.append(_to_candidate(row[0], vector_rank=position, why=["semantic match"]))
        return out

    def _vector_backend(self) -> str:
        """Which distance operator this database can actually evaluate.

        The setting alone was not safe. It defaults to "pgvector", and the only
        thing that corrected it was an environment variable the local launcher
        exported into the process it started -- so anything NOT started that way
        (a script, a worker, a deployment where the variable was forgotten) ran
        the indexed path against a `real[]` column and failed at query time with
        `operator does not exist: real[] <=> unknown`. A configuration flag that
        must agree with the schema, with nothing checking that it does, is a
        trap rather than a setting.

        So ask the schema, which cannot be wrong: pgvector's column type is
        `vector`, and the portable fallback is a `real[]` (`_float4`). The
        configured value stays as the answer when detection cannot run, which
        keeps this working against a database the app cannot introspect.
        """
        detected = _detect_vector_backend(self.session)
        configured = get_settings().vector_backend
        if detected and detected != configured:
            # Worth saying once: the deployment believes something the database
            # does not, and somebody should reconcile them.
            log.info("vector_backend_mismatch", configured=configured, detected=detected)
        return detected or configured

    def _portable_vector_candidates(self, vector, admissible_clauses, pool: int):
        """Cosine ranking over a real[] column, without pgvector.

        The same embeddings and the same ordering pgvector would produce; what
        is missing is the HNSW index, so this is a scan. Crucially the
        admissibility predicate is still applied *in the query*, so an
        inadmissible chunk is never fetched, let alone ranked -- the permission
        boundary does not move just because the arithmetic did.

        This suits modest corpora. Point the deployment at a database with
        pgvector and the indexed path returns with no other change.
        """
        from app.models import Embedding

        rows = self.session.execute(
            select(DocumentChunk, Embedding.vector)
            .join(Embedding, Embedding.chunk_id == DocumentChunk.id)
            .where(*admissible_clauses)
        ).all()
        if not rows:
            return []

        import math

        query_norm = math.sqrt(sum(component * component for component in vector))
        if query_norm == 0:
            return []

        scored: list[tuple[float, DocumentChunk]] = []
        for chunk, stored in rows:
            if not stored:
                continue
            norm = math.sqrt(sum(component * component for component in stored))
            if norm == 0:
                continue
            dot = sum(a * b for a, b in zip(vector, stored, strict=False))
            scored.append((dot / (norm * query_norm), chunk))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [
            _to_candidate(chunk, vector_rank=position, why=["semantic match"])
            for position, (_similarity, chunk) in enumerate(scored[:pool], start=1)
        ]

    def _fuse(
        self, query: str, text_hits: list[Candidate], vector_hits: list[Candidate]
    ) -> list[Candidate]:
        """Reciprocal rank fusion plus structural boosts."""
        merged: dict[uuid.UUID, Candidate] = {}
        for hit in text_hits + vector_hits:
            existing = merged.get(hit.chunk_id)
            if existing is None:
                merged[hit.chunk_id] = hit
            else:
                existing.text_rank = existing.text_rank or hit.text_rank
                existing.vector_rank = existing.vector_rank or hit.vector_rank
                for reason in hit.why_matched:
                    if reason not in existing.why_matched:
                        existing.why_matched.append(reason)

        terms = {t for t in query.lower().split() if len(t) > 2}
        for candidate in merged.values():
            score = 0.0
            if candidate.text_rank:
                score += 1.0 / (RRF_K + candidate.text_rank)
            if candidate.vector_rank:
                score += 1.0 / (RRF_K + candidate.vector_rank)
            if candidate.text_rank and candidate.vector_rank:
                candidate.why_matched.append("matched on both keyword and meaning")

            if candidate.table_name:
                haystack = candidate.table_name.lower()
                if terms & set(haystack.split()):
                    score += TABLE_BOOST
                    candidate.why_matched.append(f"table '{candidate.table_name}'")
            if candidate.heading_path:
                heading = " ".join(candidate.heading_path).lower()
                if terms & set(heading.split()):
                    score += HEADING_MATCH_BOOST
                    candidate.why_matched.append(f"section '{candidate.heading_path[-1]}'")
            if candidate.clause_ref and candidate.clause_ref.lower() in query.lower():
                score += CLAUSE_MATCH_BOOST
                candidate.why_matched.append(f"clause {candidate.clause_ref}")

            candidate.score = score
        return list(merged.values())

    def _filters_summary(self, scope: RetrievalScope) -> dict:
        return {
            "org_id": str(self.ctx.org_id),
            "scope": scope.describe(),
            "permissions_considered": sorted(
                p for p in self.ctx.permissions if p.endswith(":read")
            ),
        }

    def _likely_owners(self, scope: RetrievalScope) -> list[str]:
        """Who to ask when nothing is admissible."""
        owners = []
        if Perm.HR_RECORD_READ.value not in self.ctx.permissions:
            owners.append("HR")
        if Perm.PRICING_READ.value not in self.ctx.permissions:
            owners.append("COMMERCIAL")
        if Perm.LEGAL_PRIVILEGED_READ.value not in self.ctx.permissions:
            owners.append("LEGAL")
        return owners

    # -- structured graph search -------------------------------------------
    def search_knowledge(
        self,
        query: str,
        *,
        entity_types=None,
        predicates=None,
        valid_on=None,
        limit: int = 20,
    ) -> list[dict]:
        """Query the knowledge graph directly, for questions the graph answers
        better than prose ("which people hold OSCP?")."""
        stmt = select(KnowledgeEntity).where(
            KnowledgeEntity.org_id == self.ctx.org_id,
            KnowledgeEntity.merged_into_entity_id.is_(None),
            KnowledgeEntity.status.in_(
                [
                    KnowledgeStatus.CURRENT_VERIFIED,
                    KnowledgeStatus.HISTORICAL_VERIFIED,
                    KnowledgeStatus.CONFLICTING,
                ]
            ),
        )
        if entity_types:
            stmt = stmt.where(KnowledgeEntity.entity_type.in_(list(entity_types)))
        if query.strip():
            ts = func.plainto_tsquery("simple", query)
            stmt = stmt.where(
                or_(
                    KnowledgeEntity.name_tsv.op("@@")(ts),
                    KnowledgeEntity.canonical_name.ilike(f"%{query}%"),
                )
            )
        entities = list(self.session.execute(stmt.limit(limit)).scalars().all())

        results: list[dict] = []
        for entity in entities:
            facts = (
                self.session.execute(
                    select(KnowledgeFact).where(
                        KnowledgeFact.org_id == self.ctx.org_id,
                        KnowledgeFact.entity_id == entity.id,
                        KnowledgeFact.status != KnowledgeStatus.REJECTED,
                    )
                )
                .scalars()
                .all()
            )
            results.append(
                {
                    "entity_id": str(entity.id),
                    "entity_type": entity.entity_type.value,
                    "canonical_name": entity.canonical_name,
                    "status": entity.status.value,
                    "conflicting": entity.status is KnowledgeStatus.CONFLICTING,
                    "facts": [
                        {
                            "predicate": f.predicate,
                            "value": f.value_text
                            or (float(f.value_number) if f.value_number is not None else None)
                            or (str(f.value_date) if f.value_date else None),
                            "status": f.status.value,
                            "valid_to": str(f.valid_to) if f.valid_to else None,
                        }
                        for f in facts
                    ],
                }
            )
        return results


def _class_rank(klass: ConfidentialityClass) -> int:
    order = {
        ConfidentialityClass.PUBLIC: 0,
        ConfidentialityClass.INTERNAL: 1,
        ConfidentialityClass.CONFIDENTIAL: 2,
        ConfidentialityClass.RESTRICTED: 3,
        ConfidentialityClass.PRIVILEGED: 4,
    }
    return order[klass]


def _to_candidate(
    chunk: DocumentChunk,
    *,
    text_rank: int | None = None,
    vector_rank: int | None = None,
    why: list[str] | None = None,
) -> Candidate:
    return Candidate(
        chunk_id=chunk.id,
        document_version_id=chunk.document_version_id,
        text=chunk.text,
        score=0.0,
        text_rank=text_rank,
        vector_rank=vector_rank,
        heading_path=list(chunk.heading_path or []),
        clause_ref=chunk.clause_ref,
        table_name=chunk.table_name,
        page_from=chunk.page_from,
        page_to=chunk.page_to,
        why_matched=list(why or []),
    )
