/**
 * The company-side filing system, as a bid team would describe it.
 *
 * Twelve groups, each mapping to the `DocumentType` labels the API accepts. The
 * grouping is for the person doing the filing; the label is what the system
 * reasons with. Two distinctions carry real weight and are worth not losing in
 * the UI:
 *
 * - A certificate has an expiry and a policy does not, so only the first can go
 *   stale and flip the requirements resting on it to NEEDS_REFRESH.
 * - A past tender is what a customer once asked for. Only the response says
 *   what this company actually offered. Filing them as one kind would let a
 *   customer's wish list be mined as this company's capability.
 */

export type CompanyDocumentGroup = {
  group: string;
  hint: string;
  types: { value: string; label: string }[];
};

export const COMPANY_DOCUMENT_GROUPS: CompanyDocumentGroup[] = [
  {
    group: "1. Company profile",
    hint: "Who the company is: services, industries, offices, history.",
    types: [
      { value: "COMPANY_PROFILE", label: "Company profile" },
      { value: "CAPABILITY_STATEMENT", label: "Capability statement" },
      { value: "ORG_STRUCTURE", label: "Organization structure" },
    ],
  },
  {
    group: "2. Legal and registration",
    hint: "Tenders ask for these by name, so they are filed by name.",
    types: [
      { value: "TRADE_LICENCE", label: "Trade licence" },
      { value: "INCORPORATION_CERTIFICATE", label: "Certificate of incorporation" },
      { value: "TAX_REGISTRATION", label: "VAT / tax registration" },
      { value: "OWNERSHIP_DOCUMENT", label: "Ownership document" },
      { value: "SIGNATORY_AUTHORITY", label: "Authorized-signatory document" },
      { value: "INSURANCE_CERTIFICATE", label: "Insurance certificate" },
      { value: "LEGAL_DOC", label: "Other legal document" },
    ],
  },
  {
    group: "3. Certifications and accreditations",
    hint: "Company-level. Expiry dates are extracted and tracked.",
    types: [
      { value: "CERTIFICATE", label: "ISO or similar certificate" },
      { value: "ACCREDITATION", label: "Accreditation (CREST and similar)" },
      { value: "PARTNER_CERTIFICATE", label: "Vendor / technology partner certificate" },
    ],
  },
  {
    group: "4. Employee CVs",
    hint: "Roles, experience, skills, employment history.",
    types: [{ value: "CV", label: "Employee CV" }],
  },
  {
    group: "5. Individual professional certificates",
    hint: "Person-level, with certificate number and dates.",
    types: [
      { value: "PROFESSIONAL_CERTIFICATE", label: "Professional certificate" },
    ],
  },
  {
    group: "6. Company policies",
    hint: "Security, privacy, quality, continuity, HSE, HR screening, ethics, ESG.",
    types: [
      { value: "POLICY", label: "Policy" },
      { value: "SECURITY_DOC", label: "Security documentation" },
    ],
  },
  {
    group: "7. Support and service",
    hint: "How the company supports what it sells.",
    types: [
      { value: "SUPPORT_DOC", label: "Support model" },
      { value: "SLA_DOC", label: "SLA definitions" },
      { value: "ESCALATION_MATRIX", label: "Escalation matrix" },
      { value: "WARRANTY_TERMS", label: "Maintenance / warranty terms" },
    ],
  },
  {
    group: "8. Historical bid files",
    hint:
      "A past tender shows what the customer asked for. Only the response shows " +
      "what this company offered — upload both, and link the response to its tender.",
    types: [
      { value: "PREVIOUS_TENDER", label: "Previous RFP / tender (what was asked)" },
      { value: "PREVIOUS_PROPOSAL", label: "Our technical proposal (what we offered)" },
      { value: "PREVIOUS_COMMERCIAL_RESPONSE", label: "Our commercial response" },
      { value: "PREVIOUS_CLARIFICATION", label: "Clarification responses / revisions" },
    ],
  },
  {
    group: "9. Project proof",
    hint: "Supporting proof that verifies references extracted from proposals.",
    types: [
      { value: "CONTRACT", label: "Contract or contract pages" },
      { value: "PURCHASE_ORDER", label: "Purchase order" },
      { value: "COMPLETION_CERTIFICATE", label: "Completion / acceptance certificate" },
      { value: "REFERENCE_LETTER", label: "Customer reference letter" },
      { value: "AWARD_NOTICE", label: "Award notice" },
      { value: "PROJECT_REFERENCE", label: "Project reference" },
      { value: "CASE_STUDY", label: "Case study" },
    ],
  },
  {
    group: "10. Commercial and financial",
    hint: "Commonly requested financial evidence.",
    types: [
      { value: "FINANCIAL_STATEMENT", label: "Audited financial statement" },
      { value: "BANK_LETTER", label: "Bank / financial-capability letter" },
      { value: "PRICING_TEMPLATE", label: "Pricing template" },
      { value: "COMMERCIAL_DOC", label: "Standard commercial assumptions" },
    ],
  },
  {
    group: "11. Rate cards",
    hint: "Role, service or product rates, with currency, region and dates.",
    types: [{ value: "RATE_CARD", label: "Rate card" }],
  },
  {
    group: "12. Other reusable material",
    hint: "Anything approved for reuse in a bid.",
    types: [
      { value: "PRODUCT_DOC", label: "Product datasheet / brochure" },
      { value: "METHODOLOGY", label: "Methodology" },
      { value: "ARCHITECTURE", label: "Architecture document" },
      { value: "COMPLETED_QUESTIONNAIRE", label: "Completed security questionnaire" },
      { value: "STANDARD_FORM", label: "Standard form" },
      { value: "PARTNER_DOC", label: "Partner documentation" },
      { value: "OTHER", label: "Other" },
    ],
  },
];

/** Types whose meaning depends on the tender they answered. */
export const NEEDS_TENDER_LINK = new Set([
  "PREVIOUS_PROPOSAL",
  "PREVIOUS_COMMERCIAL_RESPONSE",
  "PREVIOUS_CLARIFICATION",
]);

/** The folder name to use inside a ZIP so a group files itself. */
export const GROUP_FOLDER_HINT: Record<string, string> = {
  "1. Company profile": "Company Profile",
  "2. Legal and registration": "Legal & Registration",
  "3. Certifications and accreditations": "Certifications",
  "4. Employee CVs": "CVs",
  "5. Individual professional certificates": "Professional Certificates",
  "6. Company policies": "Policies",
  "7. Support and service": "Support",
  "8. Historical bid files": "Previous Tenders  /  Our Technical Proposals",
  "9. Project proof": "Project Proof",
  "10. Commercial and financial": "Financial",
  "11. Rate cards": "Rate Cards",
  "12. Other reusable material": "Product",
};

export const TYPE_LABELS: Record<string, string> = Object.fromEntries(
  COMPANY_DOCUMENT_GROUPS.flatMap((group) =>
    group.types.map((type) => [type.value, type.label]),
  ),
);
