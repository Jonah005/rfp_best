"""Document ingestion orchestration.

The pipeline, in order, with a persisted state at every step so a failure is
visible rather than silent:

    UPLOADED -> SCANNING -> PARSING -> PARSED -> INDEXED -> ACTIVE
                  |            |
             QUARANTINED   PARSE_FAILED

Everything here writes through tenant-aware services and records audit events,
including when it runs inside a Celery worker.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.context import ExecutionContext
from app.core.errors import NotFound, ValidationError
from app.core.logging import get_logger
from app.ingestion.chunker import chunk_blocks
from app.ingestion.parsers import parse_document
from app.ingestion.safety import MalwareScanner, build_scanner, validate_upload
from app.models import (
    Document,
    DocumentBlock,
    DocumentChunk,
    DocumentClassification,
    DocumentVersion,
    Embedding,
)
from app.models.enums import (
    ConfidentialityClass,
    DocumentSide,
    DocumentType,
    DocumentVersionState,
)
from app.services import audit
from app.services.storage import ObjectStorage, get_storage

log = get_logger("ingestion")


def register_upload(
    session: Session,
    ctx: ExecutionContext,
    *,
    data: bytes,
    filename: str,
    side: DocumentSide,
    confidentiality_class: ConfidentialityClass,
    title: str | None = None,
    bid_id: uuid.UUID | None = None,
    language: str = "en",
    declared_mime: str | None = None,
    scanner: MalwareScanner | None = None,
    storage: ObjectStorage | None = None,
    document_id: uuid.UUID | None = None,
    document_type: DocumentType | None = None,
    responds_to_document_id: uuid.UUID | None = None,
) -> DocumentVersion:
    """Validate, scan, store, and record a new document version.

    Runs synchronously because nothing after this point should happen if the
    file is unsafe: a quarantined file must never reach a parser, and the
    caller deserves an immediate answer rather than a job id.
    """
    if side is DocumentSide.TENDER and bid_id is None:
        raise ValidationError("Tender-side documents must belong to a bid.")
    if side is DocumentSide.COMPANY and bid_id is not None:
        raise ValidationError(
            "Company knowledge is organization-wide and must not be attached to a bid."
        )

    scanner = scanner or build_scanner()
    storage = storage or get_storage()

    validated = validate_upload(data, filename, scanner=scanner, declared_mime=declared_mime)

    document = _resolve_document(
        session,
        ctx,
        document_id=document_id,
        side=side,
        bid_id=bid_id,
        title=title or filename,
        filename=filename,
        language=language,
        confidentiality_class=confidentiality_class,
        responds_to_document_id=responds_to_document_id,
    )

    version_no = (
        session.execute(
            select(DocumentVersion.version_no)
            .where(DocumentVersion.document_id == document.id)
            .order_by(DocumentVersion.version_no.desc())
            .limit(1)
        ).scalar()
        or 0
    ) + 1

    storage_key = ObjectStorage.tenant_key(
        ctx.org_id, "documents", str(document.id), f"v{version_no}", filename
    )
    storage.put(storage_key, data, content_type=validated.mime_type)

    version = DocumentVersion(
        org_id=ctx.org_id,
        document_id=document.id,
        version_no=version_no,
        storage_key=storage_key,
        sha256=validated.sha256,
        byte_size=validated.byte_size,
        mime_type=validated.mime_type,
        received_at=datetime.now(timezone.utc),
        state=DocumentVersionState.SCANNING,
        scan_result=validated.scan.as_json(),
    )
    session.add(version)
    session.flush()

    document.current_version_id = version.id
    version.state = DocumentVersionState.UPLOADED

    # A stated type is a human filing decision and outranks anything a
    # classifier would infer, so it is recorded as such. It matters more on the
    # company side than the tender side: whether a file is a past tender or the
    # response to one is not reliably visible in the text, and getting it wrong
    # would let a customer's old wish list be mined as this company's own
    # capability.
    if document_type is not None:
        classification = session.execute(
            select(DocumentClassification).where(
                DocumentClassification.org_id == ctx.org_id,
                DocumentClassification.document_version_id == version.id,
            )
        ).scalar_one_or_none()
        if classification is None:
            classification = DocumentClassification(
                org_id=ctx.org_id,
                document_version_id=version.id,
                document_type=document_type,
                confidence=1.0,
                classified_by=f"human:{ctx.actor_user_id}",
            )
            session.add(classification)
        else:
            classification.document_type = document_type
            classification.classified_by = f"human:{ctx.actor_user_id}"
            classification.confidence = 1.0
        session.flush()

    audit.record(
        session,
        ctx,
        action="document.uploaded",
        entity_type="document_version",
        entity_id=version.id,
        bid_id=bid_id,
        after={
            "document_id": str(document.id),
            "version_no": version_no,
            "filename": filename,
            "mime_type": validated.mime_type,
            "sha256": validated.sha256,
            "byte_size": validated.byte_size,
            "scanner": validated.scan.scanner,
        },
    )
    return version


def _resolve_document(
    session: Session,
    ctx: ExecutionContext,
    *,
    document_id: uuid.UUID | None,
    side: DocumentSide,
    bid_id: uuid.UUID | None,
    title: str,
    filename: str,
    language: str,
    confidentiality_class: ConfidentialityClass,
    responds_to_document_id: uuid.UUID | None = None,
) -> Document:
    if document_id is not None:
        document = session.get(Document, document_id)
        if document is None or document.org_id != ctx.org_id:
            raise NotFound("Document not found.")
        if document.side is not side:
            raise ValidationError("A new version cannot change a document's side.")
        return document

    document = Document(
        org_id=ctx.org_id,
        side=side,
        bid_id=bid_id,
        title=title,
        original_filename=filename,
        language=language,
        confidentiality_class=confidentiality_class,
        owner_user_id=ctx.actor_user_id,
        responds_to_document_id=responds_to_document_id,
    )
    session.add(document)
    session.flush()
    return document


def _discard_parse_output(
    session: Session, ctx: ExecutionContext, *, version: DocumentVersion
) -> None:
    """Remove the blocks and chunks of a previous parse, in dependency order.

    Embeddings reference chunks and chunks reference blocks, so the deletes run
    innermost first. Anything derived from those blocks downstream — clause
    resolutions, requirements — is left alone deliberately: re-running the
    analysis pass rebuilds it, and silently deleting a reviewer's confirmed
    requirements because a document was re-parsed would be a far worse
    surprise than a stale one.
    """
    chunk_ids = list(
        session.execute(
            select(DocumentChunk.id).where(
                DocumentChunk.org_id == ctx.org_id,
                DocumentChunk.document_version_id == version.id,
            )
        ).scalars()
    )
    if chunk_ids:
        session.execute(
            delete(Embedding).where(
                Embedding.org_id == ctx.org_id, Embedding.chunk_id.in_(chunk_ids)
            )
        )
    session.execute(
        delete(DocumentChunk).where(
            DocumentChunk.org_id == ctx.org_id,
            DocumentChunk.document_version_id == version.id,
        )
    )
    session.execute(
        delete(DocumentBlock).where(
            DocumentBlock.org_id == ctx.org_id,
            DocumentBlock.document_version_id == version.id,
        )
    )
    version.block_count = None
    version.chunk_count = None
    version.parse_error = None
    version.state = DocumentVersionState.SCANNING
    session.flush()
    audit.record(
        session,
        ctx,
        action="document.reparse_requested",
        entity_type="document_version",
        entity_id=version.id,
    )


def parse_and_index(
    session: Session,
    ctx: ExecutionContext,
    *,
    version_id: uuid.UUID,
    storage: ObjectStorage | None = None,
    embedder=None,
    knowledge_gateway=None,
    force: bool = False,
) -> DocumentVersion:
    """Parse, chunk, embed, and extract governed company knowledge.

    `force` re-parses a version that is already indexed, discarding its blocks
    and chunks first. Parsing is not a one-time event in practice: an
    improvement to the parser leaves every document already in the system
    holding the output of the old one, and without a way to re-run it the only
    remedy is to upload the file again and live with a duplicate.
    """
    # Locked for the duration: blocks and chunks are keyed by (version,
    # ordinal), so two parses of the same version racing each other both insert
    # ordinal 0 and one dies on the unique index. That is not hypothetical — a
    # re-parse requested while the upload's own parse was still in flight hit
    # exactly this, and the symptom was misleading: the discard step found
    # nothing to delete because the first parse had not committed yet, and the
    # failure then looked like the delete was broken rather than a race.
    version = session.get(DocumentVersion, version_id, with_for_update=True)
    if version is None or version.org_id != ctx.org_id:
        raise NotFound("Document version not found.")
    document = session.get(Document, version.document_id)
    if document is None or document.org_id != ctx.org_id:
        raise NotFound("Document not found.")
    if force:
        _discard_parse_output(session, ctx, version=version)
    if not force and version.state in (DocumentVersionState.INDEXED, DocumentVersionState.ACTIVE):
        if document.side is DocumentSide.COMPANY:
            from app.services.knowledge_extraction import extract_company_knowledge

            extract_company_knowledge(
                session, ctx, version_id=version.id, gateway=knowledge_gateway
            )
        return version

    storage = storage or get_storage()
    version.state = DocumentVersionState.PARSING
    session.flush()

    try:
        data = storage.get(version.storage_key)
        parsed = parse_document(data, document.original_filename, version.mime_type)
    except Exception as exc:
        version.state = DocumentVersionState.PARSE_FAILED
        version.parse_error = str(exc)[:2000]
        session.flush()
        audit.record(
            session,
            ctx,
            action="document.parse_failed",
            entity_type="document_version",
            entity_id=version.id,
            reason=str(exc)[:500],
        )
        raise

    block_rows: list[DocumentBlock] = []
    for parsed_block in parsed.blocks:
        block_rows.append(
            DocumentBlock(
                org_id=ctx.org_id,
                document_version_id=version.id,
                block_type=parsed_block.block_type,
                ordinal=parsed_block.ordinal,
                text=parsed_block.text,
                heading_path=parsed_block.heading_path or None,
                clause_ref=parsed_block.clause_ref,
                table_name=parsed_block.table_name,
                row_index=parsed_block.row_index,
                col_index=parsed_block.col_index,
                page_from=parsed_block.page_from,
                page_to=parsed_block.page_to,
                char_start=parsed_block.char_start,
                char_end=parsed_block.char_end,
                language=parsed_block.language,
            )
        )
    session.add_all(block_rows)
    session.flush()

    ordinal_to_id = {row.ordinal: row.id for row in block_rows}

    assembled = chunk_blocks(parsed.blocks)
    chunk_rows: list[DocumentChunk] = []
    for index, chunk in enumerate(assembled):
        chunk_rows.append(
            DocumentChunk(
                org_id=ctx.org_id,
                document_version_id=version.id,
                ordinal=index,
                text=chunk.text,
                token_count=chunk.token_count,
                block_ids=[ordinal_to_id[o] for o in chunk.block_ordinals if o in ordinal_to_id],
                heading_path=chunk.heading_path or None,
                clause_ref=chunk.clause_ref,
                table_name=chunk.table_name,
                page_from=chunk.page_from,
                page_to=chunk.page_to,
                char_start=chunk.char_start,
                char_end=chunk.char_end,
                language=chunk.language,
                # Denormalized so retrieval can filter inside the vector query.
                confidentiality_class=document.confidentiality_class,
                side=document.side,
            )
        )
    session.add_all(chunk_rows)
    session.flush()

    version.page_count = parsed.page_count
    version.block_count = len(block_rows)
    version.chunk_count = len(chunk_rows)
    version.state = DocumentVersionState.PARSED
    session.flush()

    embedded = embed_chunks(session, ctx, version_id=version.id, embedder=embedder)

    extracted: dict[str, int | bool] | None = None
    if document.side is DocumentSide.COMPANY:
        # Filing before extraction, because every excerpt inherits the
        # document's type: classify afterwards and the excerpts are already
        # stamped OTHER. A type a person stated at upload is left alone.
        from app.services import document_classification
        from app.services.knowledge_extraction import extract_company_knowledge

        document_classification.classify_version(
            session, ctx, version_id=version.id, gateway=knowledge_gateway
        )
        extracted = extract_company_knowledge(
            session, ctx, version_id=version.id, gateway=knowledge_gateway
        ).as_dict()
        # After extraction, because it marks the excerpts extraction just made.
        #
        # A document that declares itself fictional, specimen or demonstration
        # material makes none of its excerpts proof -- of anything, not only of
        # the paragraph the declaration sits in. See
        # `demonstration_documents`: a test pack whose every file opened with
        # "FICTIONAL TEST FIXTURE" had one excerpt marked and the rest left
        # unjudged, and a requirement went READY on seven of them.
        from app.services import demonstration_documents

        extracted["demonstration_excerpts"] = demonstration_documents.mark_version(
            session, ctx, version_id=version.id
        )

    version.state = DocumentVersionState.INDEXED
    session.flush()

    audit.record(
        session,
        ctx,
        action="document.indexed",
        entity_type="document_version",
        entity_id=version.id,
        bid_id=document.bid_id,
        after={
            "blocks": len(block_rows),
            "chunks": len(chunk_rows),
            "embeddings": embedded,
            "pages": parsed.page_count,
            # Which pages had no text layer and were read by OCR. A reviewer
            # judging a surprising excerpt needs to know it came from a machine
            # reading a photograph, and this is the only durable record of it.
            "ocr_pages": parsed.ocr_pages,
            "knowledge_extraction": extracted,
        },
    )
    return version


def embed_chunks(
    session: Session,
    ctx: ExecutionContext,
    *,
    version_id: uuid.UUID,
    embedder=None,
    batch_size: int = 32,
) -> int:
    """Embed every chunk of a version that lacks a vector for the active model."""
    from app.gateway.gateway import build_gateway

    provider = embedder
    if provider is None:
        provider = build_gateway().embeddings
    if provider is None:
        raise ValidationError("No embedding provider is configured.")
    provider.assert_dimension()

    existing = set(
        session.execute(
            select(Embedding.chunk_id).where(
                Embedding.org_id == ctx.org_id, Embedding.model_id == provider.model_id
            )
        )
        .scalars()
        .all()
    )
    chunks = (
        session.execute(
            select(DocumentChunk)
            .where(
                DocumentChunk.org_id == ctx.org_id,
                DocumentChunk.document_version_id == version_id,
            )
            .order_by(DocumentChunk.ordinal)
        )
        .scalars()
        .all()
    )
    pending = [c for c in chunks if c.id not in existing]

    written = 0
    now = datetime.now(timezone.utc)
    for start in range(0, len(pending), batch_size):
        batch = pending[start : start + batch_size]
        vectors = provider.embed([c.text for c in batch])
        for chunk, vector in zip(batch, vectors, strict=True):
            session.add(
                Embedding(
                    org_id=ctx.org_id,
                    chunk_id=chunk.id,
                    model_id=provider.model_id,
                    dim=len(vector),
                    vector=vector,
                    created_at=now,
                )
            )
            written += 1
        session.flush()
    return written


def activate_version(
    session: Session, ctx: ExecutionContext, *, version_id: uuid.UUID
) -> DocumentVersion:
    version = session.get(DocumentVersion, version_id)
    if version is None or version.org_id != ctx.org_id:
        raise NotFound("Document version not found.")
    if version.state is not DocumentVersionState.INDEXED:
        raise ValidationError(
            f"A version must be INDEXED before activation; this one is {version.state.value}."
        )
    before = version.state.value
    version.state = DocumentVersionState.ACTIVE
    session.flush()
    audit.record(
        session,
        ctx,
        action="document.activated",
        entity_type="document_version",
        entity_id=version.id,
        before={"state": before},
        after={"state": version.state.value},
    )
    return version
