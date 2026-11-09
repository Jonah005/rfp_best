"""Who a missing piece of information should be requested from.

After the copilot answers, `department_routing` reads the question and the
answer and proposes whether another department is needed, which, and what to
ask. Everything it returns is checked here against what exists:

- a code the company does not have is dropped (it once suggested
  "Procurement / Vendor Management" to a company with no such department);
- the asking task's own department is never suggested to itself;
- at most two suggestions survive, and a question is trimmed to size;
- if the call fails, the keyword rule that preceded it decides instead.

The result only decides whether request buttons are OFFERED and how they are
pre-filled. Sending a request remains a person's click.
"""

from __future__ import annotations

import json
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.context import ExecutionContext
from app.core.logging import get_logger
from app.models import Department, UserDepartment
from app.models.enums import DepartmentCode

log = get_logger("routing")

DEFAULT_RESPONSIBILITIES: dict[DepartmentCode, str] = {
    DepartmentCode.SECURITY: "security services, testing methodology, certifications, "
    "security controls, tools used in engagements",
    DepartmentCode.HR: "staff records, CVs, qualifications, headcount, hiring",
    DepartmentCode.PMO: "project plans, schedules, delivery method, testing and "
    "training plans, past project delivery records",
    DepartmentCode.SALES: "customer relationships, past customers, references, account history",
    DepartmentCode.COMMERCIAL: "pricing, rates, payment terms, commercial assumptions, "
    "supplier and third-party costs",
    DepartmentCode.FINANCE: "financial statements, bank guarantees, insurance, tax, "
    "company registration and financial standing",
    DepartmentCode.LEGAL: "contracts, licences and licence terms, NDAs, liability, "
    "intellectual property, legal positions on customer terms",
    DepartmentCode.BID_MANAGEMENT: "bid coordination, submission format, deadlines, "
    "signatures, forms and customer correspondence",
    DepartmentCode.PRODUCT: "the company's products and platforms, features, roadmap, "
    "product documentation",
    DepartmentCode.SOLUTION_ARCHITECTURE: "solution design, architecture, integrations, "
    "hosting, third-party components used in a solution",
    DepartmentCode.QUALITY: "quality management, ISO certification, documentation "
    "standards, audits",
}

MAX_SUGGESTIONS = 2

_GAP = re.compile(
    r"missing|gap|not (?:yet )?(?:established|evidenced|covered)"
    r"|cross-department request|who to ask",
    re.IGNORECASE,
)


def responsibilities_of(department: Department) -> str:
    return (department.responsibilities or "").strip() or DEFAULT_RESPONSIBILITIES.get(
        department.code, ""
    )


def departments(session: Session, ctx: ExecutionContext) -> list[Department]:
    """Departments with at least one person in them: the only places to send a question."""
    return list(
        session.execute(
            select(Department)
            .where(
                Department.org_id == ctx.org_id,
                Department.id.in_(
                    select(UserDepartment.department_id).where(UserDepartment.org_id == ctx.org_id)
                ),
            )
            .order_by(Department.name)
        ).scalars()
    )


def validate(proposed, *, rows: list[Department], own_department_id) -> list[dict]:
    """Keep only suggestions that name a real department other than the asker's."""
    by_code = {d.code.value: d for d in rows}
    out: list[dict] = []
    for item in proposed or []:
        code = (getattr(item, "code", "") or "").strip().upper()
        department = by_code.get(code)
        if department is None or department.id == own_department_id:
            continue
        if any(o["code"] == code for o in out):
            continue
        out.append(
            {
                "code": code,
                "id": str(department.id),
                "name": department.name,
                "question": " ".join((getattr(item, "question", "") or "").split())[:600],
                "why": " ".join((getattr(item, "reason", "") or "").split())[:300],
                "need": " ".join((getattr(item, "need", "") or "").split())[:400],
            }
        )
        if len(out) == MAX_SUGGESTIONS:
            break
    return out


def _fallback(answer_text: str, suggested: list[dict]) -> dict:
    needed = bool(_GAP.search(answer_text or "")) and bool(suggested)
    return {"needed": needed, "departments": suggested if needed else [], "source": "rules"}


def route(
    session: Session,
    ctx: ExecutionContext,
    *,
    task,
    question: str,
    answer_text: str,
    requirement_block: str,
    fallback_departments: list[dict],
    gateway,
) -> dict:
    """Decide whether to offer a cross-department request, and pre-fill it."""
    from app.gateway.registry import wrap_untrusted
    from app.schemas.department_routing import DepartmentRoutingResult

    rows = departments(session, ctx)
    own = next((d for d in rows if d.id == task.department_id), None)
    payload = {
        "task": f"{task.code}: {task.title}",
        "task_department": own.code.value if own else None,
        "requirements": (requirement_block or "")[:4000],
        "QUESTION": (question or "")[:2000],
        "ASSISTANT_ANSWER": (answer_text or "")[:6000],
        "DEPARTMENTS": [
            {"code": d.code.value, "name": d.name, "responsible_for": responsibilities_of(d)}
            for d in rows
        ],
    }
    try:
        result = gateway.generate_structured(
            "department_routing",
            wrap_untrusted(json.dumps(payload, ensure_ascii=False)),
            ctx=ctx,
            session=session,
            cache=True,
        )
    except Exception as exc:  # noqa: BLE001 - routing degrades to the rules, never fails an answer
        log.info("department_routing_unavailable", reason=type(exc).__name__)
        return _fallback(answer_text, fallback_departments)
    if not isinstance(result, DepartmentRoutingResult):
        return _fallback(answer_text, fallback_departments)

    chosen = validate(result.departments, rows=rows, own_department_id=task.department_id)
    needed = bool(result.needs_other_department and chosen)
    if result.needs_other_department and not chosen:
        # It said "ask someone" and named nobody real: say so in the trace, and
        # let the rules offer what they can rather than leaving a dead end.
        fallback = _fallback(answer_text, fallback_departments)
        fallback["source"] = "rules_after_invalid_model_choice"
        return fallback
    return {"needed": needed, "departments": chosen if needed else [], "source": "model"}


__all__ = ["DEFAULT_RESPONSIBILITIES", "responsibilities_of", "route", "validate"]
