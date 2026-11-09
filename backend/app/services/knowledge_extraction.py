"""Automatic company-document knowledge extraction and governed persistence."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.context import ExecutionContext
from app.core.errors import NotFound, ValidationError
from app.core.logging import get_logger
from app.gateway.gateway import ModelGateway, build_gateway
from app.gateway.registry import get as get_call_type
from app.gateway.registry import wrap_untrusted
from app.models import (
    AuditEvent,
    Document,
    DocumentBlock,
    DocumentChunk,
    DocumentClassification,
    DocumentVersion,
    Evidence,
    KnowledgeEntity,
)
from app.models.enums import (
    DocumentSide,
    DocumentType,
    EvidenceLinkRole,
    EvidenceLinkTarget,
)
from app.schemas.knowledge_extraction import KnowledgeExtractionResult
from app.schemas.source_locator import SourceLocator
from app.services import audit, knowledge

log = get_logger("knowledge.extraction")

MAX_BATCH_CHUNKS = 20
MAX_BATCH_CHARACTERS = 24_000


@dataclass(frozen=True)
class ExtractionCounts:
    entities: int = 0
    facts: int = 0
    relations: int = 0
    evidence: int = 0
    links: int = 0
    model_calls: int = 0
    skipped: bool = False

    def as_dict(self) -> dict[str, int | bool]:
        return {
            "entities": self.entities,
            "facts": self.facts,
            "relations": self.relations,
            "evidence": self.evidence,
            "links": self.links,
            "model_calls": self.model_calls,
            "skipped": self.skipped,
        }


def extract_company_knowledge(
    session: Session,
    ctx: ExecutionContext,
    *,
    version_id: uuid.UUID,
    gateway: ModelGateway | None = None,
) -> ExtractionCounts:
    """Extract proposed graph records from an indexed COMPANY document."""
    version = session.get(DocumentVersion, version_id)
    if version is None or version.org_id != ctx.org_id:
        raise NotFound("Document version not found.")
    document = session.get(Document, version.document_id)
    if document is None or document.org_id != ctx.org_id:
        raise NotFound("Document not found.")
    if document.side is not DocumentSide.COMPANY:
        raise ValidationError("Company knowledge extraction accepts COMPANY documents only.")

    completed = session.execute(
        select(AuditEvent.id).where(
            AuditEvent.org_id == ctx.org_id,
            AuditEvent.action == "knowledge.extraction_completed",
            AuditEvent.entity_type == "document_version",
            AuditEvent.entity_id == version.id,
        )
    ).scalar_one_or_none()
    if completed is not None:
        return ExtractionCounts(skipped=True)

    chunks = list(
        session.execute(
            select(DocumentChunk)
            .where(
                DocumentChunk.org_id == ctx.org_id,
                DocumentChunk.document_version_id == version.id,
            )
            .order_by(DocumentChunk.ordinal)
        )
        .scalars()
        .all()
    )
    if not chunks:
        raise ValidationError("A company document must be parsed into chunks before extraction.")

    block_ids = {block_id for chunk in chunks for block_id in chunk.block_ids}
    blocks = {
        block.id: block
        for block in session.execute(
            select(DocumentBlock).where(
                DocumentBlock.org_id == ctx.org_id, DocumentBlock.id.in_(block_ids)
            )
        )
        .scalars()
        .all()
    }
    chunk_by_ordinal = {chunk.ordinal: chunk for chunk in chunks}
    document_type = (
        session.execute(
            select(DocumentClassification.document_type).where(
                DocumentClassification.org_id == ctx.org_id,
                DocumentClassification.document_version_id == version.id,
            )
        ).scalar_one_or_none()
        or DocumentType.OTHER
    )

    model_gateway = gateway or build_gateway()
    call_type = get_call_type("knowledge_extraction")
    extracted_by = f"llm:{call_type.name}@{call_type.prompt_version}"

    evidence_by_ordinal: dict[int, Evidence] = {}
    entity_by_key: dict[tuple[str, str], KnowledgeEntity] = {}
    fact_fingerprints: set[tuple] = set()
    relation_fingerprints: set[tuple] = set()
    link_fingerprints: set[tuple[uuid.UUID, EvidenceLinkTarget, uuid.UUID, str]] = set()
    totals = {"entities": 0, "facts": 0, "relations": 0, "evidence": 0, "links": 0}
    model_calls = 0

    for batch in _batches(chunks):
        payload = {
            "document": {
                "document_version_id": str(version.id),
                "title": document.title,
                "filename": document.original_filename,
                "language": document.language,
                "document_type": document_type.value,
            },
            "chunks": [
                {
                    "ordinal": chunk.ordinal,
                    "heading_path": chunk.heading_path or [],
                    "clause_ref": chunk.clause_ref,
                    "table_name": chunk.table_name,
                    "page_from": chunk.page_from,
                    "page_to": chunk.page_to,
                    "text": chunk.text,
                }
                for chunk in batch
            ],
        }
        result = model_gateway.generate_structured(
            "knowledge_extraction",
            wrap_untrusted(json.dumps(payload, ensure_ascii=False, default=str)),
            ctx=ctx,
            session=session,
        )
        if not isinstance(result, KnowledgeExtractionResult):
            raise ValidationError("knowledge_extraction returned an unexpected schema.")
        model_calls += 1
        allowed_ordinals = {chunk.ordinal for chunk in batch}
        _validate_source_ordinals(result, allowed_ordinals)

        local_entities: dict[str, KnowledgeEntity] = {}
        for entity_proposal in result.entities:
            key = (
                entity_proposal.entity_type.value,
                entity_proposal.canonical_name.casefold().strip(),
            )
            entity = entity_by_key.get(key)
            if entity is None:
                entity, created = knowledge.resolve_or_create_entity(
                    session,
                    ctx,
                    entity_type=entity_proposal.entity_type,
                    name=entity_proposal.canonical_name,
                    classification=document.confidentiality_class,
                    attributes=entity_proposal.attributes,
                    extracted_by=extracted_by,
                )
                if entity_proposal.aliases:
                    entity.aliases = sorted({*(entity.aliases or []), *entity_proposal.aliases})
                entity_by_key[key] = entity
                totals["entities"] += int(created)
            local_entities[entity_proposal.ref] = entity
            totals["links"] += _link_sources(
                session,
                ctx,
                entity_proposal.source_chunk_ordinals,
                target_type=EvidenceLinkTarget.KNOWLEDGE_ENTITY,
                target_id=entity.id,
                confidence=entity_proposal.confidence,
                document=document,
                version=version,
                document_type=document_type,
                chunk_by_ordinal=chunk_by_ordinal,
                blocks=blocks,
                evidence_by_ordinal=evidence_by_ordinal,
                link_fingerprints=link_fingerprints,
                totals=totals,
            )

        for fact_proposal in result.facts:
            entity = local_entities[fact_proposal.entity_ref]
            fact_fingerprint = (
                entity.id,
                fact_proposal.predicate,
                fact_proposal.value_text,
                fact_proposal.value_number,
                fact_proposal.value_date,
                json.dumps(fact_proposal.value_json, sort_keys=True)
                if fact_proposal.value_json
                else None,
                fact_proposal.unit,
                fact_proposal.valid_from,
                fact_proposal.valid_to,
            )
            if fact_fingerprint in fact_fingerprints:
                continue
            fact_fingerprints.add(fact_fingerprint)
            evidence_ids = _evidence_ids(
                session,
                ctx,
                fact_proposal.source_chunk_ordinals,
                document=document,
                version=version,
                document_type=document_type,
                chunk_by_ordinal=chunk_by_ordinal,
                blocks=blocks,
                evidence_by_ordinal=evidence_by_ordinal,
                totals=totals,
            )
            fact = knowledge.add_fact(
                session,
                ctx,
                entity_id=entity.id,
                predicate=fact_proposal.predicate,
                value_text=fact_proposal.value_text,
                value_number=fact_proposal.value_number,
                value_date=fact_proposal.value_date,
                value_json=fact_proposal.value_json,
                unit=fact_proposal.unit,
                valid_from=fact_proposal.valid_from,
                valid_to=fact_proposal.valid_to,
                evidence_ids=[],
                extracted_by=extracted_by,
                confidence=fact_proposal.confidence,
            )
            totals["facts"] += 1
            for evidence_id in evidence_ids:
                totals["links"] += _link_once(
                    session,
                    ctx,
                    evidence_id=evidence_id,
                    target_type=EvidenceLinkTarget.KNOWLEDGE_FACT,
                    target_id=fact.id,
                    link_role=fact_proposal.link_role,
                    confidence=fact_proposal.confidence,
                    fingerprints=link_fingerprints,
                )

        for relation_proposal in result.relations:
            subject = local_entities[relation_proposal.subject_ref]
            object_ = local_entities[relation_proposal.object_ref]
            if subject.id == object_.id:
                # The model named one thing twice (two spellings that resolved to one
                # entity). A relation to itself says nothing; it must not fail the document.
                continue
            relation_fingerprint = (
                subject.id,
                relation_proposal.predicate.value,
                object_.id,
                json.dumps(relation_proposal.attributes, sort_keys=True)
                if relation_proposal.attributes
                else None,
                relation_proposal.valid_from,
                relation_proposal.valid_to,
            )
            if relation_fingerprint in relation_fingerprints:
                continue
            relation_fingerprints.add(relation_fingerprint)
            evidence_ids = _evidence_ids(
                session,
                ctx,
                relation_proposal.source_chunk_ordinals,
                document=document,
                version=version,
                document_type=document_type,
                chunk_by_ordinal=chunk_by_ordinal,
                blocks=blocks,
                evidence_by_ordinal=evidence_by_ordinal,
                totals=totals,
            )
            relation = knowledge.add_relation(
                session,
                ctx,
                subject_entity_id=subject.id,
                predicate=relation_proposal.predicate,
                object_entity_id=object_.id,
                attributes=relation_proposal.attributes,
                valid_from=relation_proposal.valid_from,
                valid_to=relation_proposal.valid_to,
                evidence_ids=[],
                extracted_by=extracted_by,
                confidence=relation_proposal.confidence,
            )
            totals["relations"] += 1
            for evidence_id in evidence_ids:
                totals["links"] += _link_once(
                    session,
                    ctx,
                    evidence_id=evidence_id,
                    target_type=EvidenceLinkTarget.KNOWLEDGE_RELATION,
                    target_id=relation.id,
                    link_role=relation_proposal.link_role,
                    confidence=relation_proposal.confidence,
                    fingerprints=link_fingerprints,
                )

    audit.record(
        session,
        ctx,
        action="knowledge.extraction_completed",
        entity_type="document_version",
        entity_id=version.id,
        after={**totals, "model_calls": model_calls, "call_type": extracted_by},
    )
    # Logged as well as audited. The audit row is the record of what happened;
    # the log is what someone reads when a whole corpus assesses as "no
    # evidence" and the question is whether extraction produced any.
    log.info(
        "knowledge_extracted",
        document_version_id=str(version.id),
        model_calls=model_calls,
        **totals,
    )
    return ExtractionCounts(
        entities=totals["entities"],
        facts=totals["facts"],
        relations=totals["relations"],
        evidence=totals["evidence"],
        links=totals["links"],
        model_calls=model_calls,
    )


def _batches(chunks: list[DocumentChunk]) -> list[list[DocumentChunk]]:
    batches: list[list[DocumentChunk]] = []
    current: list[DocumentChunk] = []
    characters = 0
    for chunk in chunks:
        if current and (
            len(current) >= MAX_BATCH_CHUNKS or characters + len(chunk.text) > MAX_BATCH_CHARACTERS
        ):
            batches.append(current)
            current = []
            characters = 0
        current.append(chunk)
        characters += len(chunk.text)
    if current:
        batches.append(current)
    return batches


def _validate_source_ordinals(result: KnowledgeExtractionResult, allowed: set[int]) -> None:
    used = {ordinal for entity in result.entities for ordinal in entity.source_chunk_ordinals}
    used.update(ordinal for fact in result.facts for ordinal in fact.source_chunk_ordinals)
    used.update(
        ordinal for relation in result.relations for ordinal in relation.source_chunk_ordinals
    )
    invalid = used - allowed
    if invalid:
        values = ", ".join(str(value) for value in sorted(invalid))
        raise ValidationError(
            f"knowledge_extraction cited chunk ordinals outside its input batch: {values}"
        )


def _locator(chunk: DocumentChunk, blocks: dict[uuid.UUID, DocumentBlock]) -> dict:
    first_block = next(
        (blocks.get(block_id) for block_id in chunk.block_ids if block_id in blocks), None
    )
    return SourceLocator(
        document_version_id=chunk.document_version_id,
        heading_path=chunk.heading_path or [],
        clause_ref=chunk.clause_ref,
        table_name=chunk.table_name,
        row_index=first_block.row_index if first_block else None,
        col_index=first_block.col_index if first_block else None,
        page_from=chunk.page_from,
        page_to=chunk.page_to,
        char_start=chunk.char_start,
        char_end=chunk.char_end,
    ).as_json()


def _evidence_ids(
    session: Session,
    ctx: ExecutionContext,
    ordinals: list[int],
    *,
    document: Document,
    version: DocumentVersion,
    document_type: DocumentType,
    chunk_by_ordinal: dict[int, DocumentChunk],
    blocks: dict[uuid.UUID, DocumentBlock],
    evidence_by_ordinal: dict[int, Evidence],
    totals: dict[str, int],
) -> list[uuid.UUID]:
    ids: list[uuid.UUID] = []
    for ordinal in sorted(set(ordinals)):
        evidence = evidence_by_ordinal.get(ordinal)
        if evidence is None:
            chunk = chunk_by_ordinal[ordinal]
            evidence = knowledge.create_evidence(
                session,
                ctx,
                source_document_version_id=version.id,
                source_locator=_locator(chunk, blocks),
                excerpt=chunk.text,
                evidence_type=document_type,
                confidentiality_class=document.confidentiality_class,
                approved_for_reuse=False,
                proposed=True,
            )
            evidence_by_ordinal[ordinal] = evidence
            totals["evidence"] += 1
        ids.append(evidence.id)
    return ids


def _link_sources(
    session: Session,
    ctx: ExecutionContext,
    ordinals: list[int],
    *,
    target_type: EvidenceLinkTarget,
    target_id: uuid.UUID,
    confidence: float,
    document: Document,
    version: DocumentVersion,
    document_type: DocumentType,
    chunk_by_ordinal: dict[int, DocumentChunk],
    blocks: dict[uuid.UUID, DocumentBlock],
    evidence_by_ordinal: dict[int, Evidence],
    link_fingerprints: set[tuple[uuid.UUID, EvidenceLinkTarget, uuid.UUID, str]],
    totals: dict[str, int],
) -> int:
    written = 0
    for evidence_id in _evidence_ids(
        session,
        ctx,
        ordinals,
        document=document,
        version=version,
        document_type=document_type,
        chunk_by_ordinal=chunk_by_ordinal,
        blocks=blocks,
        evidence_by_ordinal=evidence_by_ordinal,
        totals=totals,
    ):
        written += _link_once(
            session,
            ctx,
            evidence_id=evidence_id,
            target_type=target_type,
            target_id=target_id,
            link_role=EvidenceLinkRole.SUPPORTS,
            confidence=confidence,
            fingerprints=link_fingerprints,
        )
    return written


def _link_once(
    session: Session,
    ctx: ExecutionContext,
    *,
    evidence_id: uuid.UUID,
    target_type: EvidenceLinkTarget,
    target_id: uuid.UUID,
    link_role: EvidenceLinkRole,
    confidence: float,
    fingerprints: set[tuple[uuid.UUID, EvidenceLinkTarget, uuid.UUID, str]],
) -> int:
    fingerprint = (evidence_id, target_type, target_id, link_role.value)
    if fingerprint in fingerprints:
        return 0
    fingerprints.add(fingerprint)
    knowledge.link_evidence(
        session,
        ctx,
        evidence_id=evidence_id,
        target_type=target_type,
        target_id=target_id,
        link_role=link_role,
        confidence=confidence,
    )
    return 1
