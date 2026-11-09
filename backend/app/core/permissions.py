"""The permission catalogue and the role -> permission map.

One catalogue, used by the API, by exports and by AI retrieval. There is no
second place where authorization is decided.
"""

from __future__ import annotations

from enum import Enum


class Perm(str, Enum):
    # organization administration
    ORG_READ = "org:read"
    ORG_MANAGE = "org:manage"
    USER_READ = "user:read"
    USER_MANAGE = "user:manage"
    ROLE_MANAGE = "role:manage"
    DEPARTMENT_MANAGE = "department:manage"

    # knowledge
    KNOWLEDGE_READ = "knowledge:read"
    KNOWLEDGE_WRITE = "knowledge:write"
    KNOWLEDGE_APPROVE = "knowledge:approve"

    # documents and evidence
    DOCUMENT_READ = "document:read"
    DOCUMENT_UPLOAD = "document:upload"
    EVIDENCE_READ = "evidence:read"
    EVIDENCE_APPROVE = "evidence:approve"

    # bids
    OPPORTUNITY_READ = "opportunity:read"
    OPPORTUNITY_WRITE = "opportunity:write"
    BID_READ = "bid:read"
    BID_WRITE = "bid:write"
    REQUIREMENT_READ = "requirement:read"
    REQUIREMENT_WRITE = "requirement:write"
    ASSESSMENT_READ = "assessment:read"
    ASSESSMENT_WRITE = "assessment:write"

    # tasks and collaboration
    TASK_READ = "task:read"
    TASK_WRITE = "task:write"
    TASK_ASSIGN = "task:assign"
    # The bid manager's task:assign is unscoped: any task, any owner, any
    # department. That is correct for the person accountable for the bid and
    # wrong as the only way work gets staffed — they end up guessing who is free
    # in five departments they do not run. This is the delegated form, and it is
    # deliberately not a weaker version of the same power: holding it lets you
    # act as a head, and the Department row decides WHICH department. Both are
    # required, so the role alone confers nothing.
    TASK_ASSIGN_IN_DEPARTMENT = "task:assign_in_department"
    CHAT_USE = "chat:use"
    REQUEST_CREATE = "request:create"
    REQUEST_RESOLVE = "request:resolve"
    GRANT_CREATE = "grant:create"
    GRANT_REVOKE = "grant:revoke"

    # artifacts, approvals, release
    ARTIFACT_READ = "artifact:read"
    ARTIFACT_WRITE = "artifact:write"
    APPROVAL_DECIDE = "approval:decide"
    PACKAGE_READ = "package:read"
    PACKAGE_ASSEMBLE = "package:assemble"
    PACKAGE_VALIDATE = "package:validate"
    RELEASE_SUBMIT = "release:submit"
    RELEASE_APPROVE = "release:approve"
    EXCEPTION_REQUEST = "exception:request"
    EXCEPTION_APPROVE = "exception:approve"

    # restricted data classes
    PRICING_READ = "pricing:read"
    HR_RECORD_READ = "hr_record:read"
    LEGAL_PRIVILEGED_READ = "legal_privileged:read"
    PERSONAL_DATA_READ = "personal_data:read"

    # audit
    AUDIT_READ = "audit:read"


class RoleCode(str, Enum):
    ORG_ADMIN = "ORG_ADMIN"
    BID_MANAGER = "BID_MANAGER"
    SALES = "SALES"
    TECHNICAL_CONTRIBUTOR = "TECHNICAL_CONTRIBUTOR"
    SOLUTION_ARCHITECT = "SOLUTION_ARCHITECT"
    PROJECT_MANAGER = "PROJECT_MANAGER"
    SECURITY = "SECURITY"
    HR = "HR"
    COMMERCIAL = "COMMERCIAL"
    FINANCE = "FINANCE"
    LEGAL = "LEGAL"
    REVIEWER = "REVIEWER"
    EXECUTIVE_APPROVER = "EXECUTIVE_APPROVER"
    KNOWLEDGE_ADMIN = "KNOWLEDGE_ADMIN"
    # Held ALONGSIDE a department role, not instead of one: a head is a
    # contributor who also staffs their own department's work. The role says
    # "may act as a head"; `department.head_user_id` says of which one.
    DEPARTMENT_HEAD = "DEPARTMENT_HEAD"
    AUDITOR = "AUDITOR"


# Which roles can OWN a promise the company is making.
#
# A PROPOSED_COMMITMENT is routed to the role that would stand behind it, and
# the drafting model picks that role. It was picking from the whole enum, which
# offered it four choices that are wrong by construction: an AUDITOR reviews
# what happened and never undertakes anything, a KNOWLEDGE_ADMIN curates the
# library, an ORG_ADMIN administers the tenant, and a REVIEWER checks somebody
# else's work rather than owning it. DEPARTMENT_HEAD is excluded for a
# different reason -- it says "may act as a head", not of what, so a commitment
# routed there names no one in particular.
#
# This is a modelling decision, not a permission calculation: it narrows what a
# commitment MEANS, and it is made here, in code, once. The model is still not
# told which of these roles a given customer has staffed -- trimming the answer
# to the org chart WOULD be a permission calculation, and it would also hide
# the gap that `commitment_blockage` exists to report.
#
# The narrowing matters because the alternative is chasing instances. A fixture
# cast was extended to cover PROJECT_MANAGER and SOLUTION_ARCHITECT after one
# run routed to those; the next run routed to LEGAL. Every role on this list
# can turn up, so the list is the thing to close over -- in the prompt, in
# validation, and in any organization that intends to bid.
COMMITMENT_APPROVER_ROLES: frozenset[str] = frozenset(
    {
        RoleCode.BID_MANAGER.value,
        RoleCode.SALES.value,
        RoleCode.TECHNICAL_CONTRIBUTOR.value,
        RoleCode.SOLUTION_ARCHITECT.value,
        RoleCode.PROJECT_MANAGER.value,
        RoleCode.SECURITY.value,
        RoleCode.HR.value,
        RoleCode.COMMERCIAL.value,
        RoleCode.FINANCE.value,
        RoleCode.LEGAL.value,
        RoleCode.EXECUTIVE_APPROVER.value,
    }
)


_CONTRIBUTOR_BASE = {
    Perm.KNOWLEDGE_READ,
    Perm.DOCUMENT_READ,
    Perm.EVIDENCE_READ,
    Perm.BID_READ,
    Perm.REQUIREMENT_READ,
    Perm.ASSESSMENT_READ,
    Perm.TASK_READ,
    Perm.TASK_WRITE,
    Perm.CHAT_USE,
    Perm.REQUEST_CREATE,
    Perm.ARTIFACT_READ,
    Perm.ARTIFACT_WRITE,
}

ROLE_PERMISSIONS: dict[RoleCode, set[Perm]] = {
    RoleCode.ORG_ADMIN: {
        Perm.ORG_READ,
        Perm.ORG_MANAGE,
        Perm.USER_READ,
        Perm.USER_MANAGE,
        Perm.ROLE_MANAGE,
        Perm.DEPARTMENT_MANAGE,
        Perm.AUDIT_READ,
        Perm.GRANT_REVOKE,
        Perm.KNOWLEDGE_READ,
        Perm.BID_READ,
    },
    RoleCode.BID_MANAGER: _CONTRIBUTOR_BASE
    | {
        Perm.OPPORTUNITY_READ,
        Perm.OPPORTUNITY_WRITE,
        Perm.BID_WRITE,
        Perm.DOCUMENT_UPLOAD,
        Perm.REQUIREMENT_WRITE,
        Perm.ASSESSMENT_WRITE,
        Perm.TASK_ASSIGN,
        Perm.REQUEST_RESOLVE,
        Perm.APPROVAL_DECIDE,
        Perm.PACKAGE_READ,
        Perm.PACKAGE_ASSEMBLE,
        Perm.PACKAGE_VALIDATE,
        Perm.RELEASE_SUBMIT,
        Perm.EXCEPTION_REQUEST,
        Perm.GRANT_REVOKE,
    },
    # Every department a task can ask for help must be able to ANSWER in words.
    # Sales, Technical, Solution Architecture, PMO and Security were offered in the
    # "Ask another department" list but held no `request:resolve`, so each question
    # sent to them sat pending and the asking task waited forever. Answering is
    # not granting: handing over documents still needs `grant:create`.
    RoleCode.SALES: _CONTRIBUTOR_BASE
    | {
        Perm.OPPORTUNITY_READ,
        Perm.OPPORTUNITY_WRITE,
        Perm.DOCUMENT_UPLOAD,
        Perm.REQUEST_RESOLVE,
    },
    RoleCode.TECHNICAL_CONTRIBUTOR: _CONTRIBUTOR_BASE | {Perm.REQUEST_RESOLVE},
    RoleCode.SOLUTION_ARCHITECT: _CONTRIBUTOR_BASE | {Perm.REQUEST_RESOLVE},
    RoleCode.PROJECT_MANAGER: _CONTRIBUTOR_BASE | {Perm.REQUEST_RESOLVE},
    RoleCode.SECURITY: _CONTRIBUTOR_BASE | {Perm.DOCUMENT_UPLOAD, Perm.REQUEST_RESOLVE},
    RoleCode.HR: _CONTRIBUTOR_BASE
    | {
        Perm.HR_RECORD_READ,
        Perm.PERSONAL_DATA_READ,
        Perm.REQUEST_RESOLVE,
        Perm.GRANT_CREATE,
        Perm.GRANT_REVOKE,
        Perm.DOCUMENT_UPLOAD,
    },
    RoleCode.COMMERCIAL: _CONTRIBUTOR_BASE
    | {
        Perm.PRICING_READ,
        Perm.REQUEST_RESOLVE,
        Perm.GRANT_CREATE,
        Perm.GRANT_REVOKE,
    },
    RoleCode.FINANCE: _CONTRIBUTOR_BASE | {Perm.PRICING_READ, Perm.REQUEST_RESOLVE},
    RoleCode.LEGAL: _CONTRIBUTOR_BASE
    | {
        Perm.LEGAL_PRIVILEGED_READ,
        Perm.REQUEST_RESOLVE,
        Perm.GRANT_CREATE,
        Perm.GRANT_REVOKE,
    },
    RoleCode.REVIEWER: {
        Perm.BID_READ,
        Perm.REQUIREMENT_READ,
        Perm.ASSESSMENT_READ,
        Perm.TASK_READ,
        Perm.ARTIFACT_READ,
        Perm.APPROVAL_DECIDE,
        Perm.KNOWLEDGE_READ,
        Perm.DOCUMENT_READ,
        Perm.EVIDENCE_READ,
    },
    RoleCode.EXECUTIVE_APPROVER: {
        Perm.BID_READ,
        Perm.ARTIFACT_READ,
        Perm.APPROVAL_DECIDE,
        Perm.PACKAGE_READ,
        Perm.RELEASE_APPROVE,
        Perm.EXCEPTION_APPROVE,
        Perm.ASSESSMENT_READ,
        Perm.REQUIREMENT_READ,
    },
    RoleCode.KNOWLEDGE_ADMIN: {
        Perm.KNOWLEDGE_READ,
        Perm.KNOWLEDGE_WRITE,
        Perm.KNOWLEDGE_APPROVE,
        Perm.DOCUMENT_READ,
        Perm.DOCUMENT_UPLOAD,
        Perm.EVIDENCE_READ,
        Perm.EVIDENCE_APPROVE,
    },
    # Just the one capability, plus what it takes to use it: see the tasks, see
    # the people. A head is a contributor first — their own department role
    # carries everything else — so this role adds staffing authority and nothing
    # that could be mistaken for approval or evidence authority.
    RoleCode.DEPARTMENT_HEAD: {
        Perm.TASK_ASSIGN_IN_DEPARTMENT,
        Perm.TASK_READ,
        Perm.USER_READ,
    },
    # Auditor sees everything and changes nothing. No write permission appears here.
    RoleCode.AUDITOR: {
        Perm.AUDIT_READ,
        Perm.ORG_READ,
        Perm.USER_READ,
        Perm.KNOWLEDGE_READ,
        Perm.DOCUMENT_READ,
        Perm.EVIDENCE_READ,
        Perm.BID_READ,
        Perm.REQUIREMENT_READ,
        Perm.ASSESSMENT_READ,
        Perm.TASK_READ,
        Perm.ARTIFACT_READ,
        Perm.PACKAGE_READ,
    },
}

# Restricted object classes and the permission that opens them without a grant.
CLASS_PERMISSION: dict[str, Perm] = {
    "PRICING": Perm.PRICING_READ,
    "HR_RECORD": Perm.HR_RECORD_READ,
    "LEGAL_PRIVILEGED": Perm.LEGAL_PRIVILEGED_READ,
    "PERSONAL_DATA": Perm.PERSONAL_DATA_READ,
}

ALL_PERMISSIONS: list[Perm] = list(Perm)
