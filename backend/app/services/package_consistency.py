"""Do the finished documents agree with each other, and with what the tender demands?

Found by reading a package as the customer would: an assumption saying three references are
held next to a package containing two; a commercial proposal including all taxes beside an
assumption excluding them; standard terms limiting confidentiality to five years where the
tender asks for it to last for ever. No list of phrases catches the next pair, so a model
reads the documents and proposes contradictions and deviations, and code keeps only those
whose quoted words really appear in the documents (or tender) it names. The model proposes,
code decides.
"""

from __future__ import annotations

import json
import re
import uuid

import structlog
from sqlalchemy.orm import Session

from app.core.context import ExecutionContext

log = structlog.get_logger(__name__)

# Tender clauses worth setting against the bidder's own terms.
TERMS_CUE = re.compile(
    r"confidential|liabilit|indemn|penalt|liquidated|terminat|warrant|intellectual|ownership|"
    r"payment|invoice|tax|vat|duty|duties|validity|governing\s+law|jurisdiction|arbitrat|"
    r"perform(?:ance)?\s+bond|guarantee|insurance|sub-?contract|non-?disclosure|compensat|"
    r"audit|escrow|source\s+code|data\s+protection|personal\s+data",
    re.IGNORECASE,
)
MAX_DOC_CHARS = 14000
MAX_CLAUSES = 70


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[‘’“”\"'`]", "", text or "").casefold().split())


# --- rules that never vary between runs ------------------------------------------------
# The model proposes candidates; these check the clauses every bid has. A legal finding must
# not appear in one run and vanish in the next.

_SENT = re.compile(r"(?<=[.!?])\s+|\n+")
_NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}


def _sentences(text: str) -> list[str]:
    return [x.strip() for x in _SENT.split(text or "") if len(x.strip()) >= 20]


_TAX_OUT = re.compile(
    r"(?:exclud\w+|exclusive\s+of|not\s+(?:been\s+)?includ\w+|net\s+of"
    r"|added\s+by)[^.]{0,70}\b(?:tax(?:es)?|VAT|dut(?:y|ies)|levies)\b"
    r"|\b(?:tax(?:es)?|VAT|dut(?:y|ies)|levies)\b[^.]{0,70}(?:exclud\w+"
    r"|not\s+(?:been\s+)?includ\w+|added\s+by)",
    re.IGNORECASE,
)
_TAX_IN = re.compile(
    r"\b(?:inclusive\s+of|includ(?:e|es|ing|ed))\b[^.]{0,70}\b(?:tax(?:es)?|VAT|dut(?:y|ies)"
    r"|levies)\b"
    r"|\b(?:tax(?:es)?|VAT|dut(?:y|ies)|levies)\b[^.]{0,70}\binclusive\b",
    re.IGNORECASE,
)
_REF_CLAIM = re.compile(
    r"\b(one|two|three|four|five|six|\d{1,2})\s*(?:\(\d+\)\s*)?(?:current\s+|corporate\s+"
    r"|client\s+|customer\s+|written\s+)*"
    r"references?\b",
    re.IGNORECASE,
)
# Offer validity: the life of the bidder's OFFER. Three things must all be present in one clause --
# the offer (or bid, proposal, tender, quotation) tied to a validity word, a length or a start
# for it, and nothing that makes it a certificate's, a licence's or a document's validity.
_OFFER_LIFE = re.compile(
    r"\b(?:offer|bid|proposal|tender|quotation)\b[^.]{0,40}"
    r"\b(?:valid\w*|binding|remains?\s+open|open\s+for\s+acceptance|irrevocable)\b|"
    r"\bvalidity\s+(?:period\s+)?(?:of|for)\s+(?:the\s+|our\s+|this\s+|your\s+)?"
    r"(?:offer|bid|proposal|tender|quotation)\b",
    re.IGNORECASE,
)
_OFFER_LIFE_SPAN = re.compile(
    r"\b\d{1,3}[\s-]*(?:calendar\s+|working\s+|business\s+)?(?:day|week|month)s?\b|"
    r"\b(?:from|after|begins?|starts?|starting|commenc\w*|runs?|counted|effective|as\s+of)\b",
    re.IGNORECASE,
)
# A validity that belongs to something other than the offer: never an anchor for the offer's life.
_NOT_OFFER_LIFE = re.compile(
    r"\bcertificat\w*|\bcertification\w*|\blicen[cs]\w*|\bregistration\w*|\baccreditation\w*"
    r"|\bpermits?\b|\bvalidity\s+dates?\b|\bexpir\w*|\binsurance\b|\bpassports?\b"
    r"|\bvisas?\b|\bcredentials?\b",
    re.IGNORECASE,
)


def offer_validity_clause(clause: str) -> bool:
    """Does this clause state how long, or from when, the bidder's offer stays open?"""
    return bool(
        _OFFER_LIFE.search(clause)
        and _OFFER_LIFE_SPAN.search(clause)
        and not _NOT_OFFER_LIFE.search(clause)
    )


_PRICEY = re.compile(r"\bpric\w*|\bfirm(?:ness)?\b|\bfees?\b", re.IGNORECASE)
_OPENING = re.compile(
    r"\b(?:tender|bid)\s+opening\b|\bopening\s+(?:date|of\s+(?:the\s+)?(?:tender|bids?))\b",
    re.IGNORECASE,
)
_SUBMISSION = re.compile(r"\b(?:submission|submitted|closing\s+date|deadline)\b", re.IGNORECASE)
_HOLD = re.compile(r"\b(?:hold|holds|held|have|has)\b", re.IGNORECASE)

# (name, pattern in the bidder's terms, pattern in a tender clause, what conflicts)
_TERM_RULES = [
    (
        "confidentiality",
        re.compile(
            r"confidential\w*[^.\n]{0,100}\b(?:\d+|one|two|three|four|five|six|seven|ten)\s+years?",
            re.IGNORECASE,
        ),
        re.compile(r"perpetuit|indefinite|without\s+(?:any\s+)?time\s+limit", re.IGNORECASE),
        "The bidder's terms time-limit confidentiality where the tender requires it to continue "
        "indefinitely.",
    ),
    (
        "liability cap",
        re.compile(
            r"liabilit\w*[^.\n]{0,80}\b(?:limited|capped|not\s+exceed)|(?:limit"
            r"|cap)\w*\s+(?:of\s+|on\s+)?(?:our\s+|its\s+|total\s+)?liabilit",
            re.IGNORECASE,
        ),
        re.compile(
            r"liable\s+for\s+(?:compensat|all|any|the\s+full)|unlimited\s+liabilit"
            r"|full\s+liabilit|shall\s+(?:compensate|indemnif)",
            re.IGNORECASE,
        ),
        "The bidder caps its liability where the tender makes it liable for compensation without "
        "a stated cap.",
    ),
    (
        "indirect loss",
        re.compile(
            r"(?:no|not)\s+liab\w+[^.\n]{0,60}indirect|exclud\w+[^.\n]{0,60}indirect"
            r"|indirect[^.\n]{0,60}exclud",
            re.IGNORECASE,
        ),
        re.compile(r"liable\s+for|compensat|indemnif|damages", re.IGNORECASE),
        "The bidder excludes indirect loss where the tender holds the bidder liable for "
        "compensation or damages.",
    ),
    (
        "warranty",
        re.compile(r"warrant\w*[^.\n]{0,100}\b\d+\s*(?:days?|weeks?|months?)", re.IGNORECASE),
        re.compile(
            r"warrant\w*[^.\n]{0,120}(?:year|month|period|lifetime|duration|life\s+of)|(?:defect"
            r"|maintenance)\s+(?:liability\s+)?period",
            re.IGNORECASE,
        ),
        "The bidder's warranty period is stated in days or months where the tender asks for a "
        "longer or different period.",
    ),
]


def _days(text: str, pattern: str) -> int | None:
    m = re.search(pattern, text, re.IGNORECASE)
    return int(m.group(1)) if m else None


def rule_findings(documents: list[tuple[str, str]], clauses: list[tuple[str, str]]) -> list[dict]:
    out: list[dict] = []
    documents = sorted(documents, key=lambda d: d[0])  # the same package reads the same way

    def add(kind, a_src, a_q, b_src, b_q, issue):
        out.append(
            {
                "kind": kind,
                "first_source": a_src,
                "first_quote": a_q[:300],
                "second_source": b_src,
                "second_quote": b_q[:300],
                "issue": issue,
                # The matter, not the sentences quoted for it: the same contradiction keeps its
                # identity when a later build happens to quote a different sentence of a document.
                "topic": f"{kind}:{issue.split(';')[0].strip()}",
                "origin": "FIXED_RULE",
            }
        )

    # A matrix restates the tender's clauses beside our status; those words are the customer's,
    # not statements by the bidder, so they cannot contradict the bidder's own documents.
    sents = [
        (name, x)
        for name, text in documents
        if not re.search(r"compliance\s+matrix|cross-reference", name, re.IGNORECASE)
        for x in _sentences(text)
    ]

    # Taxes: one document says they are excluded, another that they are included.
    outs = [(n, x) for n, x in sents if _TAX_OUT.search(x) and not _TAX_IN.search(x)]
    ins = [(n, x) for n, x in sents if _TAX_IN.search(x) and not _TAX_OUT.search(x)]
    for n1, x1 in outs[:3]:
        other = next(((n2, x2) for n2, x2 in ins if n2 != n1), None)
        if other:
            add(
                "CONTRADICTION",
                n1,
                x1,
                other[0],
                other[1],
                "One document says taxes, duties or charges are excluded from the price and "
                "another says they are included.",
            )
            break

    # References: a claim to hold N, against the references actually in the package.
    actual = sum(
        1
        for n, _ in documents
        if re.search(r"project\s+reference|reference\s+letter|testimonial", n, re.IGNORECASE)
    )
    for n, x in sents:
        if re.search(r"project\s+reference|reference\s+letter", n, re.IGNORECASE):
            continue
        m = _REF_CLAIM.search(x)
        if m and _HOLD.search(x):
            word = m.group(1).lower()
            claimed = int(word) if word.isdigit() else _NUMBER_WORDS.get(word)
            if claimed and claimed > actual:
                add(
                    "CONTRADICTION",
                    n,
                    x,
                    "Package contents",
                    f"{actual} project reference document(s) are included in this package.",
                    f"A document says {claimed} references are held or provided, but {actual} are "
                    f"in the package.",
                )
                break

    # Offer validity: when the 90 (or any) days start counting. A document that starts the offer's
    # life at the opening of the tender and one that starts it at submission describe two different
    # commitments. A price that stays firm from opening is a different matter and is not read here.
    anchors: dict[str, tuple[str, str]] = {}
    for n, x in sents:
        for clause in re.split(
            r"\bwhile\b|\bwhereas\b|\bbut\b|;|,\s+and\s+", x, flags=re.IGNORECASE
        ):
            if _PRICEY.search(clause) or not offer_validity_clause(clause):
                continue
            kind = (
                "OPENING"
                if _OPENING.search(clause)
                else "SUBMISSION"
                if _SUBMISSION.search(clause)
                else ""
            )
            if kind and kind not in anchors:
                anchors[kind] = (n, x)
    if len(anchors) == 2:
        (n1, x1), (n2, x2) = anchors["OPENING"], anchors["SUBMISSION"]
        stated_by_tender = any(
            offer_validity_clause(x) and (_OPENING.search(x) or _SUBMISSION.search(x))
            for _, c in clauses
            for x in _sentences(c)
        )
        add(
            "CONTRADICTION",
            n1,
            x1,
            n2,
            x2,
            "The documents start the offer's validity at different moments (tender opening and "
            "submission); "
            "the tender is itself unclear on this, so one position must be approved and the "
            "customer asked."
            if stated_by_tender
            else "The documents start the offer's validity at different moments (tender opening "
            "and submission).",
        )

    # The bidder's terms against the tender's clauses.
    bidder_terms = [
        (n, x)
        for n, x in sents
        if re.search(
            r"standard\s+terms|terms\s+and\s+conditions|conditions\s+of\s+supply", n, re.IGNORECASE
        )
    ]
    for _name, mine, theirs, issue in _TERM_RULES:
        a = next(((n, x) for n, x in bidder_terms if mine.search(x)), None)
        if not a:
            continue
        for label, clause in clauses:
            hit = next((x for x in _sentences(clause) if theirs.search(x)), None)
            if hit:
                add("TERMS_DEVIATION", a[0], a[1], f"Tender {label}", hit, issue)
                break

    # Bid validity: the bidder offers fewer days than the tender requires.
    my_validity = next(
        (
            (n, x, _days(x, r"valid\w*[^.\n]{0,60}?(\d{2,3})\s*days"))
            for n, x in bidder_terms
            if _days(x, r"valid\w*[^.\n]{0,60}?(\d{2,3})\s*days")
        ),
        None,
    )
    if my_validity:
        for label, clause in clauses:
            for x in _sentences(clause):
                d = _days(x, r"(?:valid\w*|firm|acceptance)[^.\n]{0,80}?(\d{2,3})\s*days")
                if d and my_validity[2] and d > my_validity[2]:
                    add(
                        "TERMS_DEVIATION",
                        my_validity[0],
                        my_validity[1],
                        f"Tender {label}",
                        x,
                        f"The bidder's offer is valid for {my_validity[2]} days; the tender "
                        f"requires {d}.",
                    )
                    break
            else:
                continue
            break
    return out


_GENERIC = {
    "shall",
    "which",
    "their",
    "there",
    "other",
    "these",
    "those",
    "would",
    "should",
    "bidder",
    "tender",
    "customer",
    "document",
    "proposal",
    "provide",
    "services",
    "service",
    "within",
    "under",
    "after",
    "before",
    "being",
    "where",
    "while",
    "about",
    "contract",
    "agreement",
    "supplier",
    "vendor",
    "charge",
    "period",
}


def _stems(text: str) -> set[str]:
    return {w[:5] for w in re.findall(r"[a-z]{5,}", (text or "").lower()) if w not in _GENERIC}


def _about_issue(quote: str, issue: str) -> bool:
    """A model finding is kept only when each quote shares a distinctive word with the issue it
    claims, so a warranty sentence cannot be offered as evidence of a liability conflict."""
    wanted = _stems(issue)
    return not wanted or bool(wanted & _stems(quote))


def _turned_away(audit, p, why: str) -> None:
    if audit is None:
        return
    f = {
        "kind": getattr(p, "kind", ""),
        "issue": getattr(p, "issue", ""),
        "first_source": "",
        "first_quote": getattr(p, "first_quote", ""),
        "second_source": "",
        "second_quote": getattr(p, "second_quote", ""),
        "origin": "MODEL_PROPOSED",
    }
    record(audit, f, verdict="FAILED_CHECK", reason=why, active=False)


def verify(
    proposed: list,
    documents: list[tuple[str, str]],
    clauses: list[tuple[str, str]],
    audit: list | None = None,
) -> list[dict]:
    """Keep the findings whose quotes can be found, in the sources they belong to."""
    sources = [(name, _norm(text), False) for name, text in documents]
    sources += [(f"Tender {label}", _norm(text), True) for label, text in clauses]
    kept: list[dict] = []
    seen: set[tuple[str, ...]] = set()
    for p in proposed:
        qa, qb = _norm(p.first_quote), _norm(p.second_quote)
        if len(qa) < 12 or len(qb) < 12 or qa == qb:
            _turned_away(audit, p, "a quotation is too short or the two quotations are the same")
            continue
        if not (_about_issue(p.first_quote, p.issue) and _about_issue(p.second_quote, p.issue)):
            _turned_away(audit, p, "a quotation shares no distinctive word with the issue claimed")
            continue
        a = next((s for s in sources if qa in s[1]), None)
        b = next((s for s in sources if qb in s[1] and s[0] != (a[0] if a else None)), None)
        if a is None or b is None:
            _turned_away(
                audit, p, "a quotation was not found word for word in the documents or tender"
            )
            continue
        if p.kind == "TERMS_DEVIATION" and a[2] == b[2]:
            _turned_away(audit, p, "a deviation must set the bidder's words against the tender's")
            continue  # a deviation sets the bidder's words against the tender's
        if p.kind == "CONTRADICTION" and all(
            re.search(r"project\s+reference", src[0], re.IGNORECASE) for src in (a, b)
        ):
            _turned_away(audit, p, "two project references describe two different projects")
            continue  # two references describe two different projects
        if p.kind == "CONTRADICTION" and (a[2] and b[2]):
            _turned_away(
                audit,
                p,
                "the tender disagreeing with itself is a clarification, not a contradiction in "
                "our documents",
            )
            continue  # the tender disagreeing with itself is a clarification, not ours
        key = tuple(sorted((qa[:60], qb[:60])))
        if key in seen:
            _turned_away(audit, p, "the same pair of statements was already proposed")
            continue
        seen.add(key)
        # Bidder's side first, so the message reads "our words vs theirs".
        if a[2] and not b[2]:
            a, b, p_a, p_b = b, a, p.second_quote, p.first_quote
        else:
            p_a, p_b = p.first_quote, p.second_quote
        kept.append(
            {
                "kind": p.kind,
                "first_source": a[0],
                "first_quote": p_a.strip()[:300],
                "second_source": b[0],
                "second_quote": p_b.strip()[:300],
                "issue": p.issue.strip()[:400],
                "origin": "MODEL_PROPOSED",
            }
        )
    return kept[:25]


def _context(
    documents: list[tuple[str, str]], clauses: list[tuple[str, str]], source: str, quote: str
) -> str:
    """The line a quote sits in, with the line before it, so a reviewer sees the whole row."""
    text = next((t for n, t in documents if n == source), None)
    if text is None:
        text = next((t for r, t in clauses if f"Tender {r}" == source), "")
    lines = (text or "").splitlines()
    want = _norm(quote)[:60]
    for i, line in enumerate(lines):
        if want and want in _norm(line):
            return "\n".join(lines[max(0, i - 1) : i + 1])[:900]
    return quote


def _core(quote: str) -> str:
    """A quote without the register-row prefix ("AS-25 | Commercial | ") a table adds."""
    import re

    return _norm(re.sub(r"^\s*[A-Za-z]{1,5}-\d+\s*\|\s*[^|]{0,40}\|\s*", "", quote or ""))[:60]


def finding_id(f: dict) -> str:
    """Stable across builds: a fixed rule's finding is its matter; a model-proposed one is the
    pair of statements it sets against each other."""
    import hashlib

    if f.get("topic"):
        key = "topic|" + str(f["topic"])
    else:
        key = "|".join(sorted((_core(f.get("first_quote", "")), _core(f.get("second_quote", "")))))
    return "F-" + hashlib.sha1(key.encode(), usedforsecurity=False).hexdigest()[:10]


def _versions() -> dict:
    """The prompt versions that produced and judged the findings."""
    try:
        from app.gateway import registry

        return {
            "proposer_prompt_version": registry.get("package_consistency_review").prompt_version,
            "adjudicator_prompt_version": registry.get("consistency_adjudication").prompt_version,
        }
    except Exception:  # noqa: BLE001 - the audit does not depend on the registry
        return {}


def record(audit: list | None, f: dict, *, verdict: str, reason: str = "", active: bool) -> None:
    """One line in the contradiction audit: what was proposed, what became of it, and why."""
    if audit is None:
        return
    from datetime import datetime, timezone

    audit.append(
        {
            "finding_id": finding_id(f),
            "kind": f.get("kind", ""),
            "issue": f.get("issue", ""),
            "topic": f.get("topic", ""),
            "first_source": f.get("first_source", ""),
            "first_quote": f.get("first_quote", ""),
            "second_source": f.get("second_source", ""),
            "second_quote": f.get("second_quote", ""),
            "origin": f.get("origin")
            or ("CARRIED_OVER" if f.get("carried_over") else "MODEL_PROPOSED"),
            "carried_over": bool(f.get("carried_over")),
            "verdict": verdict,
            "reason": reason,
            "active": active,
            "decided_at": datetime.now(timezone.utc).isoformat(),
            **_versions(),
        }
    )


def adjudicate(
    model, ctx, session, found: list[dict], documents, clauses, audit: list | None = None
) -> list[dict]:
    """A second, sceptical read of the model-proposed findings (never the fixed rules).

    Keeps a finding only when the reviewer confirms it concerns the same entity and moment and
    cannot both be true; if the reviewer cannot run, the findings stand, marked unadjudicated.
    """
    from app.gateway.registry import wrap_untrusted
    from app.schemas.drafting import AdjudicationResult

    if not found:
        return found
    items = [
        {
            "id": i,
            "kind": f["kind"],
            "issue": f["issue"],
            "first": {
                "source": f["first_source"],
                "quote": f["first_quote"],
                "context": _context(documents, clauses, f["first_source"], f["first_quote"]),
            },
            "second": {
                "source": f["second_source"],
                "quote": f["second_quote"],
                "context": _context(documents, clauses, f["second_source"], f["second_quote"]),
            },
        }
        for i, f in enumerate(found)
    ]
    try:
        result = model.generate_structured(
            "consistency_adjudication",
            wrap_untrusted(json.dumps({"items": items}, ensure_ascii=False)),
            ctx=ctx,
            session=session,
        )
        if not isinstance(result, AdjudicationResult):
            raise TypeError("unexpected result")
    except Exception as exc:  # noqa: BLE001
        log.warning("consistency_adjudication_failed", error=str(exc)[:200])
        for f in found:
            record(
                audit,
                f,
                verdict="UNADJUDICATED",
                reason="the second reviewer could not run; the finding stands",
                active=True,
            )
        return [{**f, "unadjudicated": True} for f in found]
    verdict = {v.id: v for v in result.verdicts}
    out = []
    for i, f in enumerate(found):
        v = verdict.get(i)
        if v is None:
            record(
                audit,
                f,
                verdict="UNADJUDICATED",
                reason="the second reviewer returned no verdict; the finding stands",
                active=True,
            )
            out.append({**f, "unadjudicated": True})
        elif v.verdict == "CONFIRMED":
            record(audit, f, verdict="CONFIRMED", reason=v.reason, active=True)
            out.append(
                {
                    **f,
                    "adjudication": "CONFIRMED",
                    "ambiguous": False,
                    "issue": f["issue"].replace(
                        "Possible deviation, tender wording is general: ", ""
                    ),
                }
            )
        elif v.verdict == "AMBIGUOUS":
            record(audit, f, verdict="AMBIGUOUS", reason=v.reason, active=True)
            out.append(
                {
                    **f,
                    "ambiguous": True,
                    "adjudication": "AMBIGUOUS",
                    "adjudication_reason": v.reason,
                    "issue": (
                        f["issue"]
                        if f["issue"].startswith("Possible deviation")
                        else ("Possible deviation, tender wording is general: " + f["issue"])[:400]
                    )
                    if f.get("kind") == "TERMS_DEVIATION"
                    else f["issue"],
                }
            )
        else:
            record(audit, f, verdict="REJECTED", reason=v.reason, active=False)
    return out


def _drop_repeats(findings: list[dict], audit: list | None = None) -> list[dict]:
    """The same pair of sources disagreeing over one sentence is one finding, however the
    other side was worded."""
    kept: list[dict] = []
    for f in findings:
        quotes = {_core(f.get("first_quote", "")), _core(f.get("second_quote", ""))}
        sources = frozenset((f.get("first_source"), f.get("second_source")))
        stems = _stems(f.get("issue", ""))

        def _repeat(k: dict, quotes=quotes, sources=sources, stems=stems) -> bool:
            shared = quotes & {_core(k.get("first_quote", "")), _core(k.get("second_quote", ""))}
            if not shared:
                return False
            if sources == frozenset((k.get("first_source"), k.get("second_source"))):
                return True
            # One sentence set against two different statements of the same matter (the
            # same issue worded twice) is still one finding.
            return len(stems & _stems(k.get("issue", ""))) >= 2

        if any(_repeat(k) for k in kept):
            record(
                audit,
                f,
                verdict="DUPLICATE",
                reason="the same sources disagree over the same sentence as another finding",
                active=False,
            )
            continue
        kept.append(f)
    return kept


def _merge(rules: list[dict], proposed: list[dict], audit: list | None = None) -> list[dict]:
    """Rules first; a model finding about a sentence a rule already reported adds nothing."""
    seen = {_norm(r["first_quote"])[:60] for r in rules} | {
        _norm(r["second_quote"])[:60] for r in rules
    }
    extra = []
    for p in proposed:
        if _norm(p["first_quote"])[:60] not in seen and _norm(p["second_quote"])[:60] not in seen:
            extra.append(p)
        else:
            record(
                audit,
                p,
                verdict="DUPLICATE",
                reason="a fixed rule already reported this sentence",
                active=False,
            )
    return (rules + extra)[:30]


def carry_over(
    prior: list[dict],
    documents: list[tuple[str, str]],
    clauses: list[tuple[str, str]],
    current: list[dict],
) -> list[dict]:
    """Findings reported on an earlier run stay while both of their quotes are still in the
    documents (or the tender): a different model answer must not silently drop a real issue."""
    sources = [_norm(t) for _n, t in documents] + [_norm(t) for _r, t in clauses]
    have = {_norm(c["first_quote"])[:60] for c in current} | {
        _norm(c["second_quote"])[:60] for c in current
    }
    topics = {c["topic"] for c in current if c.get("topic")}
    kept: list[dict] = []
    for f in prior:
        qa, qb = _norm(f.get("first_quote", "")), _norm(f.get("second_quote", ""))
        if f.get("topic") and f["topic"] in topics:
            continue  # the same matter is already reported, whichever sentences it quotes
        if len(qa) < 12 or len(qb) < 12 or qa[:60] in have or qb[:60] in have:
            continue
        if any(qa in s for s in sources) and any(qb in s for s in sources):
            kept.append({**f, "carried_over": True, "origin": "CARRIED_OVER"})
            have.add(qa[:60])
            have.add(qb[:60])
    return kept


def review(
    session: Session,
    ctx: ExecutionContext,
    bid_id: uuid.UUID,
    documents: list[tuple[str, str]],
    *,
    gateway=None,
    prior: list[dict] | None = None,
    audit: list | None = None,
) -> list[dict] | None:
    """Verified findings, or None when the check could not run.

    Every proposal, from the model or a fixed rule, ends as a line in `audit` whether it
    stays or goes, so a wrong verdict by the second reviewer leaves a trace.
    """
    from app.gateway.gateway import build_gateway
    from app.gateway.registry import wrap_untrusted
    from app.schemas.drafting import ConsistencyResult
    from app.services.tender_analysis import tender_blocks

    documents = [(n, t) for n, t in documents if t and t.strip()]
    if len(documents) < 1:
        return []
    clauses = [
        (f"clause {i + 1}", (b.text or "").strip())
        for i, b in enumerate(tender_blocks(session, ctx, bid_id=bid_id))
        if TERMS_CUE.search(b.text or "")
    ][:MAX_CLAUSES]
    payload = {
        "documents": [{"name": n, "text": t[:MAX_DOC_CHARS]} for n, t in documents],
        "tender_clauses": [{"ref": r, "text": t[:700]} for r, t in clauses],
    }
    rules = rule_findings(documents, clauses)

    model = None
    proposed: list[dict] = []
    try:
        model = gateway or build_gateway()
        result = model.generate_structured(
            "package_consistency_review",
            wrap_untrusted(json.dumps(payload, ensure_ascii=False)),
            ctx=ctx,
            session=session,
        )
        if isinstance(result, ConsistencyResult):
            proposed = verify(result.findings, documents, [(r, t) for r, t in clauses], audit=audit)
    except Exception as exc:  # noqa: BLE001 - the rules still stand when the model is down
        log.warning("consistency_review_failed", error=str(exc)[:200])

    # Objective rules (a tax conflict, a reference count) stand on their own. A terms rule
    # compares wording with a clause and is a judgement, so it is read by the second reviewer
    # like a model proposal; if that reviewer cannot run, the rule finding stands.
    objective = [r for r in rules if r["kind"] != "TERMS_DEVIATION"]
    for r in objective:
        record(
            audit,
            r,
            verdict="NOT_ADJUDICATED",
            reason="objective rule; no judgement involved",
            active=True,
        )
    judged = [r for r in rules if r["kind"] == "TERMS_DEVIATION"] + proposed
    if model is not None:
        judged = adjudicate(model, ctx, session, judged, documents, clauses, audit=audit)
    else:
        for f in judged:
            record(
                audit,
                f,
                verdict="UNADJUDICATED",
                reason="no reviewer available; the finding stands",
                active=True,
            )
        judged = [{**f, "unadjudicated": True} for f in judged]
    kept_rules = objective + [f for f in judged if f.get("origin") == "FIXED_RULE"]
    kept_proposed = [f for f in judged if f.get("origin") != "FIXED_RULE"]
    found = _merge(kept_rules, kept_proposed, audit=audit)

    # What the second reviewer rejected a moment ago, on these same documents, is not revived
    # because an earlier run once reported it.
    rejected_now = {r["finding_id"] for r in (audit or []) if r.get("verdict") == "REJECTED"}
    revived = list(
        {finding_id(f): f for f in (prior or []) if finding_id(f) in rejected_now}.values()
    )
    for f in revived:
        record(
            audit,
            {**f, "carried_over": True},
            verdict="REJECTED",
            reason="the second reviewer rejected this finding earlier in this same run",
            active=False,
        )
    prior = [f for f in (prior or []) if finding_id(f) not in rejected_now]
    carried = carry_over(prior, documents, clauses, found)
    if carried and model is not None:
        # An earlier finding stays only if the sceptical reviewer still confirms it; a wrong
        # one must not live on because it was reported once.
        carried = adjudicate(model, ctx, session, carried, documents, clauses, audit=audit)
    out = _drop_repeats(found + carried, audit=audit)[:35]
    reconcile(audit, out)
    return [{**f, "finding_id": finding_id(f)} for f in out]


_STANDING = {"CONFIRMED", "AMBIGUOUS", "NOT_ADJUDICATED", "UNADJUDICATED"}


def reconcile(audit: list | None, final: list[dict]) -> None:
    """Give every audit line its final state once merging and de-duplication are done.

    A line is active only if its verdict let the finding stand AND that finding is in the
    final list. A finding that stood at the reviewer but was later folded into another one
    is marked SUPERSEDED, so the audit never shows as active what the report does not count.
    """
    if audit is None:
        return
    kept = {finding_id(f) for f in final}
    for rec in audit:
        stood = rec.get("verdict") in _STANDING
        if stood and rec.get("finding_id") in kept:
            rec["active"], rec["final_status"] = True, "ACTIVE"
        elif stood:
            rec["active"], rec["final_status"] = False, "SUPERSEDED"
            rec["reason"] = (
                rec.get("reason") or ""
            ) + " | folded into another finding about the same sentence"
        else:
            rec["active"] = False
            rec["final_status"] = "REMOVED"
    # One finding id, one active line (a rule and a model line can name the same pair).
    seen: set[str] = set()
    for rec in audit:
        if rec.get("active"):
            if rec["finding_id"] in seen:
                rec["active"], rec["final_status"] = False, "SUPERSEDED"
            seen.add(rec["finding_id"])


def save_audit(
    storage,
    ctx: ExecutionContext,
    bid_id: uuid.UUID,
    package_id,
    audit: list[dict],
    coverage: list[dict] | None = None,
) -> None:
    """Append this run's audit to the bid's contradiction audit (the last ten runs are kept)."""
    from datetime import datetime, timezone

    key = f"org/{ctx.org_id}/audit/{bid_id}/consistency-audit.json"
    runs: list = []
    try:
        if storage.exists(key):
            runs = json.loads(storage.get(key)).get("runs", [])
    except Exception:  # noqa: BLE001 - a damaged file starts again
        runs = []
    import uuid as _uuid

    runs.append(
        {
            "consistency_run_id": str(_uuid.uuid4()),
            "package_id": str(package_id),
            "active_finding_ids": sorted({r["finding_id"] for r in audit if r.get("active")}),
            "at": datetime.now(timezone.utc).isoformat(),
            "documents_read": coverage or [],
            "records": audit,
        }
    )
    storage.put(
        key, json.dumps({"runs": runs[-10:]}, default=str).encode(), content_type="application/json"
    )


def load_audit(storage, ctx: ExecutionContext, bid_id: uuid.UUID) -> list[dict]:
    key = f"org/{ctx.org_id}/audit/{bid_id}/consistency-audit.json"
    try:
        return json.loads(storage.get(key)).get("runs", []) if storage.exists(key) else []
    except Exception:  # noqa: BLE001
        return []


AUDIT_FIELDS = [
    "consistency_run_id",
    "package_id",
    "audit_event_id",
    "supersedes_event_id",
    "finding_id",
    "topic",
    "kind",
    "origin",
    "carried_over",
    "verdict",
    "final_status",
    "active",
    "reason",
    "issue",
    "first_source",
    "first_quote",
    "second_source",
    "second_quote",
    "proposer_prompt_version",
    "adjudicator_prompt_version",
    "decided_at",
]


def _with_event_identity(records: list[dict]) -> list[dict]:
    """Give every audit row its own id and say which earlier row of the same finding it replaces.

    A finding keeps one id through its life, so one id can appear on several rows (for example a
    proposed row and the later row that removed it). Each row is a separate event; a downstream
    system keys on `audit_event_id`, and `supersedes_event_id` links an event to the previous event
    of the same finding in the same run.
    """
    import hashlib

    last_for: dict[str, str] = {}
    out = []
    for position, r in enumerate(records, start=1):
        basis = f"{r.get('consistency_run_id')}|{position}|{r.get('finding_id')}"
        event = "E-" + hashlib.sha256(basis.encode()).hexdigest()[:12]
        fid = str(r.get("finding_id"))
        out.append({**r, "audit_event_id": event, "supersedes_event_id": last_for.get(fid, "")})
        last_for[fid] = event
    return out


def write_audit_files(folder, bid_id, run: dict, package_id) -> dict:
    """Write the JSON and CSV of ONE run, the CSV generated from the same in-memory record,
    and check they agree. Raises ValueError rather than leave two files that describe
    different runs. Returns the hashes and counts for the package manifest."""
    import csv
    import hashlib
    from pathlib import Path

    if not run:
        raise ValueError("there is no consistency run to export")
    if str(run.get("package_id")) != str(package_id):
        raise ValueError(
            f"the latest consistency run belongs to package {run.get('package_id')}, not "
            f"{package_id}; "
            "validate the package again before exporting"
        )
    folder = Path(folder)
    run_id, pkg = run["consistency_run_id"], str(run["package_id"])
    records = _with_event_identity(
        [{**r, "consistency_run_id": run_id, "package_id": pkg} for r in run.get("records", [])]
    )
    doc = {**run, "records": records}
    jpath = folder / f"contradiction-audit-{bid_id}.json"
    cpath = folder / f"contradiction-audit-{bid_id}.csv"
    jpath.write_text(json.dumps(doc, indent=2, default=str), encoding="utf-8")
    with cpath.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=AUDIT_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in records:
            w.writerow(r)
    # Read both files back and compare what a reviewer would compare.
    back_j = json.loads(jpath.read_text(encoding="utf-8"))
    back_c = list(csv.DictReader(cpath.open(encoding="utf-8")))
    act_j = sorted(r["finding_id"] for r in back_j["records"] if r["active"])
    act_c = sorted(r["finding_id"] for r in back_c if r["active"] == "True")
    if len(back_j["records"]) != len(back_c) or act_j != act_c:
        raise ValueError("the contradiction audit JSON and CSV disagree")
    events = [r["audit_event_id"] for r in back_j["records"]]
    if len(events) != len(set(events)):
        raise ValueError("two audit rows share one event id")
    if len(act_j) != len(set(act_j)):
        raise ValueError("the same finding is active twice in the contradiction audit")
    if {r["package_id"] for r in back_c} != {pkg} or {r["consistency_run_id"] for r in back_c} != {
        run_id
    }:
        raise ValueError("the contradiction audit files do not share one run and package")
    sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()  # noqa: E731
    return {
        "consistency_run_id": run_id,
        "package_id": pkg,
        "records": len(records),
        "active_findings": act_j,
        "files": {jpath.name: sha(jpath), cpath.name: sha(cpath)},
    }
