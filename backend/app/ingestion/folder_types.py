"""Work out what kind of document a folder inside an archive holds.

Bulk intake only saves work if the archive's own structure does the filing, so
a folder name is read as the filing decision. The matching is deliberately
forgiving about how people actually name folders — "CVs", "3. Certifications",
"Legal & Registration" — and deliberately strict about what it will conclude:
an unrecognised folder yields nothing rather than a guess.

Filing a document as the wrong kind is worse than filing it as unknown. An
unknown document is visible on the review list and can be corrected; a
confidently mislabelled one is not, and in one direction the mistake is
expensive — a past customer's tender filed as a proposal becomes, to every
downstream reader, something this company offered.
"""

from __future__ import annotations

import re

from app.models.enums import DocumentType

# Order in this tuple is not what resolves a tie — see `document_type_for_folder`,
# which prefers the phrase appearing earliest in the folder name and, at the same
# position, the longer one. That keeps "Certifications & Accreditations" filed by
# its leading word rather than by whichever pattern happened to be listed first,
# and still lets "professional certificate" beat "certificate".
FOLDER_PATTERNS: tuple[tuple[str, DocumentType], ...] = (
    # 8. Historical bid files — the pair that must never be conflated.
    ("previous tender", DocumentType.PREVIOUS_TENDER),
    ("past tender", DocumentType.PREVIOUS_TENDER),
    ("previous rfp", DocumentType.PREVIOUS_TENDER),
    ("past rfp", DocumentType.PREVIOUS_TENDER),
    ("old rfp", DocumentType.PREVIOUS_TENDER),
    ("commercial response", DocumentType.PREVIOUS_COMMERCIAL_RESPONSE),
    ("previous commercial", DocumentType.PREVIOUS_COMMERCIAL_RESPONSE),
    ("previous clarification", DocumentType.PREVIOUS_CLARIFICATION),
    ("clarification response", DocumentType.PREVIOUS_CLARIFICATION),
    ("previous proposal", DocumentType.PREVIOUS_PROPOSAL),
    ("past proposal", DocumentType.PREVIOUS_PROPOSAL),
    ("technical proposal", DocumentType.PREVIOUS_PROPOSAL),
    ("submitted response", DocumentType.PREVIOUS_PROPOSAL),
    # 2. Legal and registration
    ("trade licence", DocumentType.TRADE_LICENCE),
    ("trade license", DocumentType.TRADE_LICENCE),
    ("incorporation", DocumentType.INCORPORATION_CERTIFICATE),
    ("tax registration", DocumentType.TAX_REGISTRATION),
    ("vat", DocumentType.TAX_REGISTRATION),
    ("ownership", DocumentType.OWNERSHIP_DOCUMENT),
    ("signatory", DocumentType.SIGNATORY_AUTHORITY),
    ("power of attorney", DocumentType.SIGNATORY_AUTHORITY),
    ("insurance", DocumentType.INSURANCE_CERTIFICATE),
    ("legal", DocumentType.LEGAL_DOC),
    # 5 before 3: a personal certificate is not a company accreditation.
    ("professional certificate", DocumentType.PROFESSIONAL_CERTIFICATE),
    ("individual certificate", DocumentType.PROFESSIONAL_CERTIFICATE),
    ("staff certificate", DocumentType.PROFESSIONAL_CERTIFICATE),
    # 3. Certifications and accreditations
    ("partner certificate", DocumentType.PARTNER_CERTIFICATE),
    ("vendor certificate", DocumentType.PARTNER_CERTIFICATE),
    ("accreditation", DocumentType.ACCREDITATION),
    ("certification", DocumentType.CERTIFICATE),
    ("certificate", DocumentType.CERTIFICATE),
    ("iso", DocumentType.CERTIFICATE),
    # 9. Project proof — before the looser "reference" match.
    ("reference letter", DocumentType.REFERENCE_LETTER),
    ("completion certificate", DocumentType.COMPLETION_CERTIFICATE),
    ("acceptance certificate", DocumentType.COMPLETION_CERTIFICATE),
    ("purchase order", DocumentType.PURCHASE_ORDER),
    ("award", DocumentType.AWARD_NOTICE),
    ("contract", DocumentType.CONTRACT),
    ("case stud", DocumentType.CASE_STUDY),
    ("project reference", DocumentType.PROJECT_REFERENCE),
    ("reference", DocumentType.PROJECT_REFERENCE),
    # 4. People
    ("cv", DocumentType.CV),
    ("resume", DocumentType.CV),
    ("curriculum", DocumentType.CV),
    # 6. Policies
    ("policy", DocumentType.POLICY),
    ("policies", DocumentType.POLICY),
    # 7. Support and service
    ("escalation", DocumentType.ESCALATION_MATRIX),
    ("sla", DocumentType.SLA_DOC),
    ("warranty", DocumentType.WARRANTY_TERMS),
    ("maintenance", DocumentType.WARRANTY_TERMS),
    ("support", DocumentType.SUPPORT_DOC),
    # 10/11. Commercial and financial
    ("financial statement", DocumentType.FINANCIAL_STATEMENT),
    ("audited", DocumentType.FINANCIAL_STATEMENT),
    ("bank letter", DocumentType.BANK_LETTER),
    ("rate card", DocumentType.RATE_CARD),
    ("pricing", DocumentType.PRICING_TEMPLATE),
    ("commercial", DocumentType.COMMERCIAL_DOC),
    # 1. Company profile
    ("capability statement", DocumentType.CAPABILITY_STATEMENT),
    ("org structure", DocumentType.ORG_STRUCTURE),
    ("organisation structure", DocumentType.ORG_STRUCTURE),
    ("organization structure", DocumentType.ORG_STRUCTURE),
    ("org chart", DocumentType.ORG_STRUCTURE),
    ("company profile", DocumentType.COMPANY_PROFILE),
    ("profile", DocumentType.COMPANY_PROFILE),
    # 12. Other reusable material
    ("questionnaire", DocumentType.COMPLETED_QUESTIONNAIRE),
    ("methodolog", DocumentType.METHODOLOGY),
    ("architecture", DocumentType.ARCHITECTURE),
    ("datasheet", DocumentType.PRODUCT_DOC),
    ("brochure", DocumentType.PRODUCT_DOC),
    ("product", DocumentType.PRODUCT_DOC),
    ("security", DocumentType.SECURITY_DOC),
    ("standard form", DocumentType.STANDARD_FORM),
    ("partner", DocumentType.PARTNER_DOC),
)

_LEADING_INDEX = re.compile(r"^\s*\d+[\.\)]?\s*")


def normalise(folder: str) -> str:
    """Strip the decoration people put on folder names.

    "3. Certifications & Accreditations" and "certifications_accreditations"
    describe the same shelf and must reach the same answer.
    """
    name = folder.replace("\\", "/").split("/")[-1]
    name = _LEADING_INDEX.sub("", name)
    name = name.replace("_", " ").replace("-", " ").replace("&", " ")
    return re.sub(r"\s+", " ", name).strip().lower()


def document_type_for_folder(folder: str) -> DocumentType | None:
    """Match the deepest folder first, then walk outwards.

    A file in ``Historical/Previous Tenders/2023`` should be filed by the
    "Previous Tenders" part, not by "Historical" — the more specific shelf is
    the one nearer the file. Returns None when nothing matches, which leaves
    the document unclassified and visible for a person to correct.
    """
    if not folder:
        return None
    segments = [segment for segment in folder.replace("\\", "/").split("/") if segment]
    for segment in reversed(segments):
        name = normalise(segment)
        if not name:
            continue
        best: tuple[int, int, DocumentType] | None = None
        for phrase, document_type in FOLDER_PATTERNS:
            position = name.find(phrase)
            if position < 0:
                continue
            # Earliest match wins; at equal position the longer phrase wins,
            # so a specific kind is never shadowed by a substring of itself.
            candidate = (position, -len(phrase), document_type)
            if best is None or candidate[:2] < best[:2]:
                best = candidate
        if best is not None:
            return best[2]
    return None
