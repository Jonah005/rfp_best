/**
 * Which seeded account holds a given permission.
 *
 * A 403 in this system almost never means "you cannot do this" — it means
 * "this is somebody else's job", because the roles are separated on purpose.
 * The useful thing to show is therefore not the permission name but the person
 * who has it, and naming the wrong role is worse than naming none: it sends
 * someone to an account that will refuse them for the same reason.
 */

type Holder = { role: string; account: string; why: string };

export const PERMISSION_HOLDERS: Record<string, Holder> = {
  "evidence:approve": {
    role: "KNOWLEDGE_ADMIN",
    account: "knowledge@acme-cyber.example.com",
    why: "approving company evidence for reuse",
  },
  "knowledge:approve": {
    role: "KNOWLEDGE_ADMIN",
    account: "knowledge@acme-cyber.example.com",
    why: "resolving the knowledge review queue",
  },
  "knowledge:write": {
    role: "KNOWLEDGE_ADMIN",
    account: "knowledge@acme-cyber.example.com",
    why: "editing the company knowledge graph",
  },
  "release:approve": {
    role: "EXECUTIVE_APPROVER",
    account: "the executive approver",
    why: "releasing a package (maker-checker)",
  },
  "release:write": {
    role: "BID_MANAGER",
    account: "sara.k@acme-cyber.example.com",
    why: "assembling a release package",
  },
  "user:write": {
    role: "ORG_ADMIN",
    account: "admin@acme-cyber.example.com",
    why: "administering users and roles",
  },
  "role:write": {
    role: "ORG_ADMIN",
    account: "admin@acme-cyber.example.com",
    why: "administering users and roles",
  },
  "audit:read": {
    role: "ORG_ADMIN",
    account: "admin@acme-cyber.example.com",
    why: "reading the audit log",
  },
};

const DEFAULT_HOLDER: Holder = {
  role: "BID_MANAGER",
  account: "sara.k@acme-cyber.example.com",
  why: "most bid work",
};

/** Turn "Missing required permission(s): evidence:approve" into an instruction. */
export function explainPermissionError(message: string): string {
  const match = message.match(/permission\(s\):\s*([a-z_:]+)/i);
  const permission = match?.[1];
  const holder = (permission && PERMISSION_HOLDERS[permission]) || DEFAULT_HOLDER;
  const named = permission ? `“${permission}”` : "That permission";
  return (
    `${named} belongs to ${holder.role}, not to this account — the roles are ` +
    `separated on purpose. Sign in as ${holder.account} for ${holder.why}.`
  );
}

/**
 * Say who holds a permission, before anything is attempted.
 *
 * `explainPermissionError` above answers the question after a 403 has come
 * back. This answers the same question from the permission alone, so a control
 * can say it at the point of the click instead of waiting for the server to
 * refuse. Both read the one table, so the two can never name different people
 * for the same permission.
 */
export function explainMissingPermission(permission: string): string {
  const holder = PERMISSION_HOLDERS[permission] || DEFAULT_HOLDER;
  return (
    `“${permission}” belongs to ${holder.role}, not to this account — the roles ` +
    `are separated on purpose. Sign in as ${holder.account} for ${holder.why}.`
  );
}
