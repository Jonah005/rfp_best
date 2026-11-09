export const screens = {
  intake: { title: "Opportunity & tender intake", eyebrow: "Screen 1", description: "Create an opportunity, promote it to a governed bid, then upload and analyse the tender package." },
  knowledge: { title: "Company knowledge", eyebrow: "Screen 2", description: "Review the evidence-backed company graph and the proposals extracted from company documents." },
  bids: { title: "Bid dashboard", eyebrow: "Screen 3", description: "Track active bids, their deadlines, workflow state, documents and operational health." },
  clarifications: { title: "Clarifications", eyebrow: "Screen 3b", description: "Resolve the defects the analysis found in the tender: blanks, placeholders and contradictions that hold scope confidence down." },
  matrices: { title: "Requirement matrices", eyebrow: "Screen 4", description: "Inspect eligibility, technical, submission and contract requirements without collapsing evidence and readiness." },
  work: { title: "My work", eyebrow: "Screen 5", description: "See owned tasks across bids, grouped by the state that needs attention." },
  tasks: { title: "Task workspace & Copilot", eyebrow: "Screen 6", description: "Work from bounded requirements and governed evidence, then generate a typed draft through DeepSeek." },
  requests: { title: "Incoming requests", eyebrow: "Screen 7", description: "Acknowledge and resolve cross-department requests with an auditable response." },
  approvals: { title: "Approvals", eyebrow: "Screen 8", description: "Apply the ordered, role-based approval chain to submitted response artifacts." },
  readiness: { title: "Bid readiness", eyebrow: "Screen 9", description: "Keep tender scope confidence separate from operational and evidence readiness." },
  release: { title: "Release & package", eyebrow: "Screen 10", description: "Assemble real documents, run deterministic gates and release with maker-checker separation." },
  activity: { title: "Activity", eyebrow: "Screen 11", description: "Everything you have been told: requests, assignments, decisions on your drafts and milestones on your bids." },
} as const;

export type ScreenKey = keyof typeof screens;
