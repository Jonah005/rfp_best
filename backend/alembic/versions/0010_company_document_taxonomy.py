"""Give the company side a filing system a bid team would recognise.

The company-side document types were a rough sketch — fifteen buckets that
covered the idea without matching how anyone actually files bid material. This
revision replaces them with the taxonomy the product brief specifies, and the
distinctions it adds are not cosmetic:

* A **certificate** carries an issue and expiry date; a **policy** does not.
  Only the first can go stale, and staleness is what flips a requirement to
  NEEDS_REFRESH rather than leaving it reading as current.
* **PREVIOUS_TENDER** and **PREVIOUS_PROPOSAL** are separate types because an
  old RFP records what a customer once asked for, while the response records
  what this company actually offered. Filing both as "old bid" would let the
  customer's wish list be mined as though it were this company's capability —
  the single most expensive mistake available in this subsystem.
* Legal registration documents are split by kind (trade licence, incorporation,
  tax registration, signatory authority) because tenders ask for them by name,
  and a checklist cannot tick "LEGAL_DOC".

**Why ADD VALUE and not a type rebuild.** Existing rows already carry the old
labels, all of which are retained, so there is nothing to migrate — only new
labels to admit. `ALTER TYPE ... ADD VALUE` runs inside Alembic's transaction on
PostgreSQL 12 and above; the only restriction is that a label added in a
transaction cannot be *used* in that same transaction, and this revision only
declares labels, it writes no rows with them. (Forcing an AUTOCOMMIT isolation
level here does not work: Alembic has already begun a transaction on the
connection, and SQLAlchemy refuses to change isolation on a live one.)

`IF NOT EXISTS` keeps it re-runnable: a database created from live metadata
already has every label the model defines by the time this revision executes,
which is the same hazard revision 0009 documents.

Revision ID: 0010_company_document_taxonomy
"""

from __future__ import annotations

from alembic import op

revision = "0010_company_document_taxonomy"
down_revision = "0009_scale_evidence_search"
branch_labels = None
depends_on = None

NEW_TYPES = (
    # 1. Company profile
    "CAPABILITY_STATEMENT",
    "ORG_STRUCTURE",
    # 2. Legal and registration
    "TRADE_LICENCE",
    "INCORPORATION_CERTIFICATE",
    "TAX_REGISTRATION",
    "OWNERSHIP_DOCUMENT",
    "SIGNATORY_AUTHORITY",
    "INSURANCE_CERTIFICATE",
    # 3. Certifications and accreditations
    "ACCREDITATION",
    "PARTNER_CERTIFICATE",
    # 5. Individual professional certificates
    "PROFESSIONAL_CERTIFICATE",
    # 7. Support and service
    "SLA_DOC",
    "ESCALATION_MATRIX",
    "WARRANTY_TERMS",
    # 8. Historical bid files
    "PREVIOUS_TENDER",
    "PREVIOUS_COMMERCIAL_RESPONSE",
    "PREVIOUS_CLARIFICATION",
    # 9. Project proof
    "CONTRACT",
    "PURCHASE_ORDER",
    "COMPLETION_CERTIFICATE",
    "REFERENCE_LETTER",
    "AWARD_NOTICE",
    # 10. Commercial and financial
    "FINANCIAL_STATEMENT",
    "BANK_LETTER",
    "PRICING_TEMPLATE",
    # 12. Other reusable material
    "COMPLETED_QUESTIONNAIRE",
    "STANDARD_FORM",
    "PARTNER_DOC",
)


def upgrade() -> None:
    for label in NEW_TYPES:
        op.execute(f"ALTER TYPE document_type ADD VALUE IF NOT EXISTS '{label}'")

    # A company document's kind is now a filing decision a person makes, so the
    # response that answered a past tender can be linked to that tender. Without
    # the link the pair is two unrelated files and the "what did we actually
    # offer" question cannot be answered from the "what was asked" document.
    op.execute(
        """
        ALTER TABLE document
          ADD COLUMN IF NOT EXISTS responds_to_document_id uuid
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_document_responds_to
          ON document (org_id, responds_to_document_id)
        """
    )


def downgrade() -> None:
    # PostgreSQL cannot remove a value from an enum type. Dropping the labels
    # would mean rebuilding the type and rewriting every column that uses it,
    # which risks live data to undo an additive change. The link column goes;
    # the labels stay.
    op.execute("DROP INDEX IF EXISTS ix_document_responds_to")
    op.execute("ALTER TABLE document DROP COLUMN IF EXISTS responds_to_document_id")
