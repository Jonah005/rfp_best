"""Make the clarification register fit the tender in front of it.

Three jobs, each a model proposal that code checks:

* design gaps: what a bidder needs from the customer to design and price the solution that
  the tender does not say. A gap is kept only if the tender wording it quotes is really there,
  or, when the tender is silent, none of the words that would answer it appear in the tender.
* triage: drop questions the tender already answers (the quote must be in the tender) and move
  the bidder's own decisions out of the customer's list.
* work packages: group the pieces of company work that one person would do together.
"""

from __future__ import annotations

import json
import re
import uuid

import structlog
from sqlalchemy.orm import Session

from app.core.context import ExecutionContext

log = structlog.get_logger(__name__)

MAX_TENDER_CHARS = 60000


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[‘’“”\"'`]", "", text or "").casefold().split())


def tender_text(session: Session, ctx: ExecutionContext, bid_id: uuid.UUID) -> str:
    from app.services.tender_analysis import tender_blocks

    return "\n".join(
        (b.text or "").strip()
        for b in tender_blocks(session, ctx, bid_id=bid_id)
        if (b.text or "").strip()
    )


def _call(session, ctx, name, payload, gateway):
    from app.gateway.gateway import build_gateway
    from app.gateway.registry import wrap_untrusted

    model = gateway or build_gateway()
    # Every call here is at temperature zero and depends on nothing but its payload, so an
    # identical payload must give the identical answer. Caching makes that true: the register
    # built twice from the same tender and findings is the same register, not two samples.
    return model.generate_structured(
        name,
        wrap_untrusted(json.dumps(payload, ensure_ascii=False)),
        ctx=ctx,
        session=session,
        cache=True,
    )


def verified_gaps(proposed: list, tender: str, branch: str = "") -> list[dict]:
    """Keep gaps whose quote is in the tender, or whose answering words are all absent from it."""
    haystack = _norm(tender)
    kept: list[dict] = []
    seen: set[str] = set()
    for g in proposed:
        quote = _norm(g.basis_quote or "")
        terms = [_norm(t) for t in g.topic_terms if _norm(t)]
        if quote:
            if quote not in haystack:
                continue
        else:
            if not terms or any(t in haystack for t in terms):
                continue
        key = _norm(g.question)[:60]
        if key in seen:
            continue
        seen.add(key)
        kept.append(
            {
                "severity": "BLOCKING" if g.blocks != "CLARITY" else "MINOR",
                "category": g.category,
                "blocks": g.blocks,
                "impact": g.impact.strip(),
                "branch": branch,
                "question": g.question.strip(),
                "basis": (g.basis_quote or "The tender does not state this.").strip(),
                "status": "TO_ASK",
            }
        )
    return kept


def bid_headline(session, ctx, bid_id) -> str:
    """The title the bid is registered under: it often names the project type the body does not."""
    try:
        from app.services import report_extras

        d = report_extras.tender_details(session, ctx, bid_id)
        return " / ".join(x for x in (d.get("tender"), d.get("customer")) if x)[:300]
    except Exception:  # noqa: BLE001
        return ""


def scope_interpretations(
    session, ctx, tender: str, *, headline: str = "", gateway=None
) -> list[dict]:
    """Distinct readings of what the tender asks for; each needs a quote that is really there."""
    if not tender.strip():
        return []
    try:
        from app.schemas.drafting import ScopeResult

        result = _call(
            session,
            ctx,
            "tender_scope_interpretations",
            {"headline": headline, "tender": tender[:MAX_TENDER_CHARS]},
            gateway,
        )
        if not isinstance(result, ScopeResult):
            return []
    except Exception as exc:  # noqa: BLE001
        log.warning("scope_check_failed", error=str(exc)[:200])
        return []
    haystack = _norm(tender + " " + headline)
    out = [
        {"label": i.label.strip(), "quote": i.quote.strip()}
        for i in result.interpretations
        if len(_norm(i.quote)) >= 12 and _norm(i.quote) in haystack
    ]
    return out[:3]


def design_gaps(session, ctx, bid_id, tender: str, *, gateway=None) -> list[dict]:
    """Design questions, one set per reading when the tender supports more than one."""
    if not tender.strip():
        return []
    readings = scope_interpretations(
        session, ctx, tender, headline=bid_headline(session, ctx, bid_id), gateway=gateway
    )
    branches = [r["label"] for r in readings] if len(readings) > 1 else [""]
    out: list[dict] = []
    if len(readings) > 1:
        letters = [f"({chr(97 + i)}) {r['label']}" for i, r in enumerate(readings)]
        both = (
            f"({chr(97 + len(readings))}) all of the above as separate workstreams, "
            "each with its own scope and deliverables"
        )
        other = f"({chr(98 + len(readings))}) another scope that ADX will define"
        evidence = " | ".join(f'"{r["quote"][:140]}"' for r in readings)
        out.append(
            {
                "severity": "BLOCKING",
                "category": "SOLUTION_DESIGN",
                "blocks": "DESIGN",
                "impact": "Nothing can be designed, sized or priced until the project type is "
                "known; each reading needs different answers.",
                "branch": "",
                "question": (
                    "The tender supports more than one reading of what is required. Please "
                    "confirm which "
                    "applies: " + "; ".join(letters + [both, other]) + "."
                ),
                "basis": "Tender wording pointing at different projects: " + evidence,
                "status": "TO_ASK",
            }
        )
    from app.schemas.drafting import DesignGapResult

    for branch in branches:
        try:
            payload = {"tender": tender[:MAX_TENDER_CHARS]}
            if branch:
                payload["interpretation"] = branch
            result = _call(session, ctx, "design_information_gaps", payload, gateway)
            if isinstance(result, DesignGapResult):
                out += verified_gaps(result.gaps, tender, branch)
        except Exception as exc:  # noqa: BLE001 - advisory; the register still builds
            log.warning("design_gaps_failed", error=str(exc)[:200])
    return out


_ADMIN = re.compile(
    r"\b(?:clarification\s+(?:approver|deadline|process|request)"
    r"|approv\w+\s+(?:of\s+)?clarifications?|"
    r"who\s+approves|question\s+deadline|deadline\s+for\s+(?:questions|clarifications)"
    r"|review\s+period|"
    r"signator\w*|point\s+of\s+contact|submission\s+(?:deadline|date|method|portal)|closing\s+date|"
    r"bid\s+opening|opening\s+date|notice\s+of\s+intention)\b",
    re.I,
)
_CERT = re.compile(r"\b(?:certif\w+|accredit\w+|ISO\s*\d+|trustworth\w*)\b", re.I)


def normalise_category(question: str, category: str) -> str:
    """The model's category, corrected where the question's own words decide it."""
    if _ADMIN.search(question or ""):
        return "SUBMISSION_ADMIN"
    if category in ("PROJECT_DELIVERY", "SOLUTION_DESIGN") and _CERT.search(question or ""):
        return "SECURITY_COMPLIANCE"
    return category


def atomize(session, ctx, questions: list[dict], *, gateway=None) -> list[dict]:
    """Split compound questions so each part can be answered, or found answered, alone."""
    if not questions:
        return questions
    try:
        from app.schemas.drafting import AtomResult

        result = _call(
            session,
            ctx,
            "question_atomize",
            {"questions": [{"id": i, "text": q["question"]} for i, q in enumerate(questions)]},
            gateway,
        )
        if not isinstance(result, AtomResult):
            return questions
    except Exception as exc:  # noqa: BLE001
        log.warning("atomize_failed", error=str(exc)[:200])
        return questions
    parts_by_id = {
        it.id: [p.strip() for p in it.parts if p and len(p.strip()) >= 10] for it in result.items
    }
    out: list[dict] = []
    for i, q in enumerate(questions):
        parts = parts_by_id.get(i) or []
        original = _norm(q["question"])
        # Each part must come from the question: guard against invented text.
        stems = {w[:5] for w in re.findall(r"[a-z]{5,}", original)}
        parts = [p for p in parts if {w[:5] for w in re.findall(r"[a-z]{5,}", _norm(p))} & stems]
        if len(parts) <= 1:
            out.append(q)
            continue
        for p in parts:
            c = dict(q)
            c["question"] = p
            c["split_from"] = q["question"]
            out.append(c)
    return out


_TENDER_DISAGREES = re.compile(
    r"as\s+stated\s+elsewhere|elsewhere\s+in\s+the\s+(?:rfp|tender)|conflicts?\s+with\s+clause|"
    r"(?:inconsisten\w+|contradict\w+)\s+(?:with|between)\s+(?:clause|section|the\s+(?:rfp"
    r"|tender))",
    re.I,
)
_VALIDITY = re.compile(
    r"\b(?:offer|bid|proposal|tender)\s+(?:validity|is\s+valid|remains?\s+(?:valid|binding))|"
    r"validity\s+(?:period\s+)?of\s+(?:the\s+)?(?:offer|bid|proposal|tender)"
    r"|\b\d+-?\s*day\s+(?:offer|bid)\s+validity",
    re.I,
)


def question_id(question: str, clause: str = "") -> str:
    """A question's identity: the same words about the same clause always get the same id."""
    import hashlib

    words = re.sub(r"[^a-z0-9 ]+", " ", _norm(question))
    return "Q-" + hashlib.sha256(f"{' '.join(words.split())}|{clause}".encode()).hexdigest()[:10]


_FILLER = frozenset(
    "clause clauses please confirm what which that this does must should shall would could "
    "there their about whether required require requires need needs have been with from into "
    "than also each other under over such many much more most only the and for are all may "
    "they not can any how who when where".split()
)


def _topic(text: str) -> set[str]:
    """The content words of a question or sentence, plurals folded, filler left out."""
    out: set[str] = set()
    for w in re.findall(r"[a-z]{3,}", _norm(text)):
        if w in _FILLER:
            continue
        if w.endswith("ies") and len(w) > 4:
            w = w[:-3] + "y"
        elif w.endswith(("xes", "ses", "ches", "shes", "zes")) and len(w) > 4:
            w = w[:-2]
        elif w.endswith("s") and not w.endswith("ss") and len(w) > 3:
            w = w[:-1]
        out.add(w[:5])
    return out


def _overlap(a: set[str], b: set[str]) -> float:
    return len(a & b) / min(len(a), len(b)) if a and b else 0.0


_ASKS_FOR_A_FACT = re.compile(
    r"^\s*(?:for\s+clauses?\s+[\d.()a-z]+,\s*)?(?:how\s+(?:many|much|long)|what\s+(?:is|are)\s+the\s+"
    r"(?:number|quantity|currency|duration|period)|which\s+currency|in\s+which\s+currency)"
    # A question that quotes the tender's own number and then asks to confirm the count.
    r"|\bspecif\w+\s+(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)\b"
    r".*\bconfirm\b.*\b(?:exact|number|count|quantity)\b",
    re.I | re.S,
)
# Where a compound question leaves the tender's statement and starts questioning it.
_LEAD_CLAUSE = re.compile(r",\s*(?:while|but|whereas)\b|\s[—–]\s|;\s", re.I)
_STATES_A_VALUE = re.compile(
    r"\d|\b(?:one|two|three|four|five|six|seven|eight|nine|ten|twelve)\b|"
    r"\b(?:AED|USD|EUR|SAR|dirhams?|dollars?)\b",
    re.I,
)


_CURRENCY = re.compile(r"\b(?:AED|USD|EUR|SAR|dirhams?|dollars?)\b", re.I)


def stated_facts(questions: list[dict], tender: str) -> list[dict]:
    """Settle by rule the questions whose answer the tender states as a plain fact.

    A question that asks for a quantity, a length or a currency is answered when one sentence of
    the tender is about the same things and gives a number or a currency. This does not depend on
    a model's verdict, so the same tender gives the same classification every time.
    """
    sentences = [x.strip() for x in re.split(r"(?<=[.;])\s+|\n+", tender) if len(x.strip()) >= 20]
    topics = [(x, _topic(x)) for x in sentences if _STATES_A_VALUE.search(x)]
    for q in questions:
        if q.get("verdict") == "ANSWERED_IN_TENDER" or not _ASKS_FOR_A_FACT.search(
            q.get("question", "")
        ):
            continue
        # A compound question states the tender's wording first and doubts it afterwards: the
        # statement is what the tender is searched for.
        lead = _LEAD_CLAUSE.split(q["question"], maxsplit=1)[0]
        asked = _topic(lead if re.search(r"\bspecif\w+\s+\S", lead, re.I) else q["question"])
        currency = bool(re.search(r"currenc", q["question"], re.I))
        if currency:
            asked.discard("curre")  # the sentence names a currency; it does not say "currency"
        candidates = [(x, t) for x, t in topics if not currency or _CURRENCY.search(x)]
        best = max(((_overlap(asked, t), x) for x, t in candidates), default=(0.0, ""))
        need = 2 if currency else 3
        if len(asked) >= need and best[0] >= 0.75 and len(asked & _topic(best[1])) >= need:
            q["verdict"] = "ANSWERED_IN_TENDER"
            q["answer_quote"] = best[1][:300]
    return questions


_ASKS_WHICH_ITEMS = re.compile(
    r"^\s*(?:what|which)\s+(?:are\s+the\s+)?(?:applicable|relevant|specific|required)?\s*"
    r"(?:taxes|tax\s+types|rates|levies|duties|charges|fees|standards|certifications?)\b|"
    r"^\s*what\s+are\s+the\s+applicable\b",
    re.I,
)
_SPECIFIC = re.compile(r"\d|%|\b[A-Z]{2,}\b")


def demote_unspecific_answers(questions: list[dict]) -> list[dict]:
    """A question asking WHICH items or rates apply is not answered by a sentence naming none.

    "The total is inclusive of all applicable taxes" settles how the price is stated; it does not
    say which taxes, or at what rate. Such an answer stays a question, with what the tender does
    say kept beside it.
    """
    for q in questions:
        quote = q.get("answer_quote") or ""
        if (
            q.get("verdict") == "ANSWERED_IN_TENDER"
            and _ASKS_WHICH_ITEMS.search(q.get("question", ""))
            and not _SPECIFIC.search(quote)
        ):
            q["verdict"] = "KEEP"
            q["partial_answer"] = quote
            q["still_missing"] = "the specific items or rates, which the tender does not name"
            q.pop("answer_quote", None)
    return questions


def reconcile_sections(
    blocking: list[dict], optional: list[dict], bidder: list[dict], answered: list[dict]
) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    """One matter, one place.

    Near-identical questions within a section collapse into the first. A question still open
    that the tender already answers (a verified answer sits in the answered section) is
    moved there: the same matter cannot be both asked and answered.
    """

    def collapse(items: list[dict]) -> list[dict]:
        kept: list[tuple[set[str], dict]] = []
        for q in items:
            mine = _topic(q.get("question", ""))
            twin = next(
                (
                    k
                    for t, k in kept
                    if len(mine & t) >= 2
                    and _overlap(mine, t) >= 0.8
                    and len(mine & t) / len(mine | t) >= 0.5
                ),
                None,
            )
            if twin is None:
                kept.append((mine, q))
            else:
                twin.setdefault("also_asked_as", []).append(q.get("question", ""))
        return [k for _, k in kept]

    def same_answer_twice(items: list[dict]) -> list[dict]:
        kept: list[tuple[set[str], dict]] = []
        for q in items:
            mine = _topic(q.get("question", ""))
            twin = next(
                (
                    k
                    for t, k in kept
                    if k.get("answer_quote") == q.get("answer_quote")
                    and len(mine & t) >= 3
                    and _overlap(mine, t) >= 0.5
                ),
                None,
            )
            if twin is None:
                kept.append((mine, q))
            else:
                twin.setdefault("also_asked_as", []).append(q.get("question", ""))
        return [k for _, k in kept]

    answered = same_answer_twice(collapse(answered))
    answers = [(_topic(a["question"] + " " + a.get("answer_quote", "")), a) for a in answered]
    open_sections = []
    for section in (blocking, optional, bidder):
        remaining: list[dict] = []
        for q in collapse(section):
            if q.get("tender_conflict") or q.get("blocks") == "DESIGN":
                remaining.append(q)
                continue
            mine = _topic(q.get("question", ""))
            match = next(
                (a for t, a in answers if len(mine & t) >= 3 and _overlap(mine, t) >= 0.75), None
            )
            if match is None:
                remaining.append(q)
            else:
                moved = {**q, "verdict": "ANSWERED_IN_TENDER"}
                moved["answer_quote"] = match.get("answer_quote", "")
                answered.append(moved)
        open_sections.append(remaining)
    return (
        open_sections[0],
        open_sections[1],
        open_sections[2],
        same_answer_twice(collapse(answered)),
    )


def merge_branch_variants(questions: list[dict], *, floor: float = 0.5) -> list[dict]:
    """Fold the same question asked under two scope readings into one.

    A question that differs only by which reading of the scope it was generated for is one
    question to the customer. Variants merge when they share a category and most of their
    content words; the kept question loses its branch and notes the others it covers.
    """

    def stems(text: str) -> set[str]:
        return {w[:5] for w in re.findall(r"[a-z]{4,}", _norm(text))}

    out: list[dict] = []
    for q in questions:
        mine = stems(q.get("question", ""))
        twin = None
        if q.get("branch"):
            for kept in out:
                if (
                    kept.get("branch")
                    and kept["branch"] != q["branch"]
                    and kept.get("category") == q.get("category")
                ):
                    theirs = stems(kept.get("question", ""))
                    union = mine | theirs
                    if union and len(mine & theirs) / len(union) >= floor:
                        twin = kept
                        break
        if twin is None:
            out.append(q)
            continue
        also = twin.setdefault("also_branches", [twin["branch"]])
        if q["branch"] not in also:
            also.append(q["branch"])
    for q in out:
        if q.get("also_branches"):
            q["branch"] = ""
            q["question"] = q["question"].rstrip() + (
                " (This applies under each reading: " + "; ".join(q["also_branches"]) + ".)"
            )
    return out


def escalate_tender_conflicts(questions: list[dict]) -> list[dict]:
    """A tender that disagrees with itself is the customer's to settle, and nothing can be
    submitted until it has. Such a question is blocking and goes to the customer, never to
    the bidder's own decisions, whatever severity or verdict it was given earlier."""
    for q in questions:
        text = q.get("question") or ""
        if _TENDER_DISAGREES.search(text) or (
            _VALIDITY.search(text) and re.search(r"\bor\b", text, re.I)
        ):
            q["severity"] = "MAJOR"
            q["verdict"] = "KEEP"
            q.pop("internal", None)
            if _VALIDITY.search(text) or q.get("category") in (None, "", "SOLUTION_DESIGN"):
                q["category"] = "LEGAL_CONTRACTUAL"
            q["tender_conflict"] = True
    return questions


def _regroup(questions: list[dict]) -> list[dict]:
    """After the per-part verdicts, parts still open go back into one question; parts already
    answered, or for the bidder to settle, stand alone."""
    questions = escalate_tender_conflicts(questions)
    out: list[dict] = []
    open_parts: dict[str, list[dict]] = {}
    slot: dict[str, int] = {}
    for q in questions:
        origin = q.get("split_from")
        if origin and q.get("verdict") == "KEEP":
            if origin not in open_parts:
                open_parts[origin] = []
                slot[origin] = len(out)
                out.append(q)  # placeholder, replaced below
            open_parts[origin].append(q)
        else:
            out.append(q)
    for origin, parts in open_parts.items():
        total = sum(1 for q in questions if q.get("split_from") == origin)
        merged = dict(parts[0])
        if len(parts) == total:
            merged["question"] = origin
        else:
            merged["question"] = " ".join(p["question"].rstrip() for p in parts)
            merged["partial_answer"] = "part of this question is already answered in the tender"
            merged["still_missing"] = "the remaining parts as worded"
        merged.pop("split_from", None)
        out[slot[origin]] = merged
    return out


def triage(session, ctx, questions: list[dict], tender: str, *, gateway=None) -> list[dict]:
    """Annotate each question with verdict KEEP / ANSWERED_IN_TENDER / BIDDER_DECISION."""
    for q in questions:
        q["verdict"] = "KEEP"
        if q.get("category"):
            q["category"] = normalise_category(q["question"], q["category"])
    if not questions or not tender.strip():
        return questions
    questions = atomize(session, ctx, questions, gateway=gateway)
    try:
        from app.schemas.drafting import TriageResult

        payload = {
            "questions": [{"id": i, "text": q["question"]} for i, q in enumerate(questions)],
            "tender": tender[:MAX_TENDER_CHARS],
        }
        result = _call(session, ctx, "clarification_triage", payload, gateway)
        if not isinstance(result, TriageResult):
            return _regroup(questions)
    except Exception as exc:  # noqa: BLE001
        log.warning("triage_failed", error=str(exc)[:200])
        return _regroup(questions)
    haystack = _norm(tender)
    claimed: dict[int, str] = {}
    for v in result.verdicts:
        if not 0 <= v.id < len(questions):
            continue
        q = questions[v.id]
        if v.verdict == "ANSWERED_IN_TENDER":
            if v.quote and len(_norm(v.quote)) >= 12 and _norm(v.quote) in haystack:
                claimed[v.id] = v.quote.strip()
        elif v.verdict == "BIDDER_DECISION" and q.get("severity") in ("MINOR", "INFO"):
            q["verdict"] = "BIDDER_DECISION"
        elif v.verdict == "INTERNAL_CHECK":
            q["verdict"] = "BIDDER_DECISION"
            q["internal"] = True
    # A quote that is in the tender is not necessarily an answer. Ask whether it answers every
    # material part of the question; anything less stays a question, noting what is missing.
    if claimed:
        try:
            from app.schemas.drafting import EntailmentResult

            check = _call(
                session,
                ctx,
                "answer_entailment",
                {
                    "items": [
                        {"id": i, "question": questions[i]["question"], "quote": quote}
                        for i, quote in claimed.items()
                    ]
                },
                gateway,
            )
            full = {v.id: v for v in check.verdicts} if isinstance(check, EntailmentResult) else {}
        except Exception as exc:  # noqa: BLE001
            log.warning("entailment_failed", error=str(exc)[:200])
            full = {}
        for i, quote in claimed.items():
            entailed = full.get(i)
            if entailed is not None and entailed.answers_fully:
                questions[i]["verdict"], questions[i]["answer_quote"] = "ANSWERED_IN_TENDER", quote
            elif entailed is not None:
                questions[i]["partial_answer"] = quote
                questions[i]["still_missing"] = entailed.missing_part or ""
    return _regroup(questions)


def work_packages(session, ctx, items: list[str], *, gateway=None) -> list[dict]:
    """[{name, items: [indexes]}] covering every item exactly once."""
    if not items:
        return []
    packages: list[dict] = []
    try:
        from app.schemas.drafting import WorkPackageResult

        result = _call(
            session,
            ctx,
            "work_package_grouping",
            {"items": [{"id": i, "text": t[:200]} for i, t in enumerate(items)]},
            gateway,
        )
        if isinstance(result, WorkPackageResult):
            taken: set[int] = set()
            for p in result.packages:
                ids = [i for i in dict.fromkeys(p.items) if 0 <= i < len(items) and i not in taken]
                if ids:
                    taken.update(ids)
                    packages.append({"name": p.name.strip(), "items": ids})
            packages += [
                {"name": items[i], "items": [i]} for i in range(len(items)) if i not in taken
            ]
            return packages
    except Exception as exc:  # noqa: BLE001
        log.warning("work_packages_failed", error=str(exc)[:200])
    return [{"name": t, "items": [i]} for i, t in enumerate(items)]


# --- who owns what (deterministic) ----------------------------------------------------------
OWNER_RULES = (
    (
        re.compile(
            r"conflicts?\s+of\s+interest|declaration|undertaking|power\s+of\s+attorney"
            r"|authori[sz]ation",
            re.I,
        ),
        "Legal / Compliance",
    ),
    (
        re.compile(r"performance\s+bond|bank\s+guarantee|advance\s+payment|\bbond\b", re.I),
        "Finance / Legal",
    ),
    (
        re.compile(
            r"soft[\s-]*cop|\bCDs?\b|envelope|sealed|stamped|\bcopies\b|pages\s+numbered"
            r"|loosely\s+bound",
            re.I,
        ),
        "Bid coordinator",
    ),
    (re.compile(r"sub-?contract", re.I), "Procurement / Legal"),
    (
        re.compile(r"company\s+(?:profile|overview)|brochure|corporate|footprint", re.I),
        "Corporate / Marketing",
    ),
    (
        re.compile(r"\bterms\b|liabilit|legal|confidential|warrant|indemn", re.I),
        "Legal / Compliance",
    ),
    (
        re.compile(r"price|pricing|commercial|financial|payment|tax|cost|fee|BOQ", re.I),
        "Commercial / Finance",
    ),
    (
        re.compile(
            r"\bCVs?\b|resume|staff|visa|training\s+cert|organi[sz]ational\s+chart|screening|team",
            re.I,
        ),
        "Delivery lead",
    ),
    (
        re.compile(
            r"architecture|methodology|solution|technical|project\s+plan|schedule|milestone"
            r"|specification|scope|deliverables",
            re.I,
        ),
        "Solution architect",
    ),
    (
        re.compile(r"reference|testimonial|customer\s+list|similar\s+assignment", re.I),
        "Bid manager",
    ),
)


def owner_of(text: str) -> str:
    for pattern, who in OWNER_RULES:
        if pattern.search(text or ""):
            return who
    return "Bid manager"


_RULES = (
    (
        "SUBMISSION_ADMIN",
        re.compile(
            r"\b(?:issue\s+date|template|footer|final\s+issued|reference\s+number|formal\s+title"
            r"|page\s+count|clarification\s+(?:approver|deadline|process|request)"
            r"|approv\w+\s+(?:of\s+)?clarifications?|question\s+deadline"
            r"|deadline\s+for\s+questions|review\s+period|signator\w*|authori[sz]ed\s+signator\w*"
            r"|point\s+of\s+contact|submission\s+(?:deadline|date|method|portal))\b",
            re.I,
        ),
    ),
    (
        "ELIGIBILITY",
        re.compile(
            r"\b(?:footprint|regional\s+presence|local\s+(?:presence|office)|offices?\s+in"
            r"|presence\s+in|middle\s+east)\b",
            re.I,
        ),
    ),
    (
        "SECURITY_COMPLIANCE",
        re.compile(
            r"\b(?:trustworth\w*|certif\w*\s+(?:required|needed|held)|security\s+certif\w*"
            r"|ISO\s*27001|SOC\s*2)\b",
            re.I,
        ),
    ),
    (
        "LEGAL_CONTRACTUAL",
        re.compile(r"sub-?contract|sub-?supplier|third[\s-]party\s+(?:approval|supplier)", re.I),
    ),
    (
        "LEGAL_CONTRACTUAL",
        re.compile(
            r"arbitrat|governing\s+law|jurisdiction|liabilit|indemn|penalt|damages|terminat"
            r"|force\s+majeure|prevails|contract\b|conflicts?\s+of\s+interest|binding",
            re.I,
        ),
    ),
    (
        "COMMERCIAL",
        re.compile(
            r"\b(?:price|pricing|cost|fee|currency|tax|payment|invoice|BOQ|bill\s+of|budget"
            r"|itemi[sz]ation)\b",
            re.I,
        ),
    ),
    (
        "STAFFING",
        re.compile(
            r"\b(?:staff|CVs?|resume|team|personnel|consultants?|time\s+commitment|replacement)\b",
            re.I,
        ),
    ),
    (
        "ELIGIBILITY",
        re.compile(
            r"\b(?:eligib|reference|experience|qualif|registration|recency|current)\b", re.I
        ),
    ),
    (
        "SECURITY_COMPLIANCE",
        re.compile(
            r"\b(?:security|ethical|standard|certif|complian|classification|screening|audit)\b",
            re.I,
        ),
    ),
    (
        "PROJECT_DELIVERY",
        re.compile(
            r"\b(?:schedule|duration|phase|milestone|deliver|training|knowledge\s+transfer"
            r"|acceptance|go[\s-]*live|sign-?off|approver|presentation)\b",
            re.I,
        ),
    ),
)


def category_of(text: str) -> str:
    for name, pattern in _RULES:
        if pattern.search(text or ""):
            return name
    return "SOLUTION_DESIGN"
