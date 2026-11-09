"use client";

import { Fragment, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { Gated, useCan } from "@/components/gate";
import { explainMissingPermission } from "@/lib/permission-roles";
import {
  COMPANY_DOCUMENT_GROUPS,
  GROUP_FOLDER_HINT,
  NEEDS_TENDER_LINK,
  TYPE_LABELS,
} from "@/lib/company-documents";
import { ClassifyDocuments, DepartmentResponsibilities } from "./extras";

/**
 * Screen 2 — the company side of the system.
 *
 * Everything so far has been about what the tender demands. This is the other
 * half: what the company can actually prove. Until documents land here, every
 * requirement assesses as having no evidence, and the readiness picture is
 * arithmetic over an empty set.
 *
 * The flow is deliberately not "upload and trust":
 *
 *   upload      a company document, ingested exactly like a tender document
 *   extract     entities, facts and relations, each tied to an excerpt
 *   approve     a human confirms the evidence before it can back a claim
 *   review      anything the extractor was unsure of, resolved by a person
 *
 * The approval step is the point of the whole subsystem. Extracted knowledge
 * arrives PENDING_APPROVAL and cannot be reused until someone with
 * knowledge:approve says it is real — which is why a claim in a submitted
 * proposal can always be traced to a person who vouched for it.
 */

type Action = <T>(
  label: string,
  work: () => Promise<T>,
  describe?: (result: T) => string,
) => Promise<void>;

type Entity = {
  id: string;
  entity_type: string;
  canonical_name: string;
  classification: string;
  status: string;
  approval_status: string;
};
type Fact = {
  id: string;
  entity_id: string;
  predicate: string;
  value_text: string | null;
  value_number: number | null;
  value_date: string | null;
  unit: string | null;
  valid_to: string | null;
  status: string;
  approval_status: string;
};
type Evidence = {
  id: string;
  excerpt: string;
  evidence_type: string;
  confidentiality_class: string;
  approval_status: string;
  status: string;
  expiry_date: string | null;
  approved_for_reuse: boolean;
};
type ReviewItem = {
  id: string;
  kind: string;
  subject_type: string;
  subject_id: string;
  detail: Record<string, unknown> | null;
};

const TABS = [
  { key: "checklist", label: "What to provide", hint: "Every kind, and whether it is on file" },
  { key: "documents", label: "Filed documents", hint: "What is on file, and as what" },
  { key: "evidence", label: "Evidence", hint: "Excerpts that can back a claim" },
  { key: "entities", label: "Entities", hint: "Certifications, people, products, projects" },
  { key: "review", label: "Review queue", hint: "What the extractor could not decide alone" },
  { key: "people", label: "People", hint: "Which colleague each CV describes" },
] as const;

/**
 * One extracted person, and the account the matcher thinks they hold.
 *
 * `status` is the whole point: PROPOSED means a string matcher had an idea and
 * nothing more. Only a CONFIRMED row states a fact, and even then it grants
 * nothing — no role, no permission, no access to evidence.
 */
type PersonLink = {
  link_id: string;
  entity_id: string;
  person: string;
  status: string;
  method: string;
  confidence: number | null;
  note: string | null;
  account_id: string | null;
  account_email: string | null;
  account_name: string | null;
};

type ArchiveResult = {
  filed: number;
  skipped: number;
  unclassified: number;
  documents: { file: string; document_type: string | null }[];
  rejected: { file: string; reason: string }[];
};

type CompanyDoc = {
  id: string;
  title: string;
  original_filename: string;
  document_type?: string | null;
  responds_to_document_id?: string | null;
};

export function Knowledge({ action }: { action: Action }) {
  const [tab, setTab] = useState<(typeof TABS)[number]["key"]>("evidence");
  const [file, setFile] = useState<File | null>(null);
  const [docType, setDocType] = useState("");
  const [respondsTo, setRespondsTo] = useState("");
  const [archive, setArchive] = useState<File | null>(null);
  const [archiveResult, setArchiveResult] = useState<ArchiveResult | null>(null);

  const documents = useQuery({
    queryKey: ["knowledge", "documents"],
    queryFn: () => api<CompanyDoc[]>("/knowledge/documents"),
  });
  // Only a past tender can be the thing a response answered.
  const pastTenders = (documents.data ?? []).filter(
    (doc) => doc.document_type === "PREVIOUS_TENDER",
  );

  const evidence = useQuery({
    queryKey: ["knowledge", "evidence"],
    queryFn: () => api<Evidence[]>("/knowledge/evidence"),
  });
  const entities = useQuery({
    queryKey: ["knowledge", "entities"],
    queryFn: () => api<Entity[]>("/knowledge/entities"),
  });
  // There is no facts list endpoint — facts belong to an entity and are
  // returned with it, which is the right shape: a predicate with no subject is
  // not a fact about anything.
  const [openEntity, setOpenEntity] = useState<string | null>(null);
  const entityDetail = useQuery({
    queryKey: ["knowledge", "entity", openEntity],
    queryFn: () => api<{ facts?: Fact[] }>(`/knowledge/entities/${openEntity}`),
    enabled: Boolean(openEntity),
  });
  const people = useQuery({
    queryKey: ["knowledge", "people"],
    queryFn: () => api<PersonLink[]>("/knowledge/people"),
    retry: false,
  });
  // Guarded by knowledge:approve. Firing it for an account that cannot hold
  // that permission produced a 403 in the server log on every single page load
  // of this screen, which is noise that hides real failures — and the tab it
  // feeds could only ever render an error for those accounts anyway.
  const mayReview = useCan("knowledge:approve");
  const review = useQuery({
    queryKey: ["knowledge", "review"],
    queryFn: () => api<ReviewItem[]>("/knowledge/review-queue"),
    retry: false,
    enabled: mayReview !== false,
  });

  // "Not usable" rather than "not approved". Evidence carries two axes, and
  // counting only the approval one hid a corpus that was fully approved and
  // still inadmissible because extraction left its status at PROPOSED — the
  // button vanished at exactly the moment it was needed.
  const pending = (evidence.data ?? []).filter(
    (row) =>
      row.approval_status !== "APPROVED" ||
      row.status === "PROPOSED" ||
      row.status === "NEEDS_CONFIRMATION",
  ).length;

  return (
    <>
      <DepartmentResponsibilities />
      <ClassifyDocuments />
      <section className="panel">
        <div className="panel-head">
          <div>
            <p className="kicker">STEP 1 · INGEST</p>
            <h2>Add a company document</h2>
          </div>
          <div className="panel-actions">
            <span className="pill">Provenance required</span>
          </div>
        </div>
        <form
          className="upload-stack"
          onSubmit={(event) => {
            event.preventDefault();
            if (!file || !docType) return;
            const uploadForm = event.currentTarget;
            action("Company document upload", async () => {
              const body = new FormData();
              body.append("file", file);
              body.append("confidentiality_class", "INTERNAL");
              body.append("language", "en");
              body.append("document_type", docType);
              // A response filed without its tender is just prose. The link is
              // what lets "what did we commit to" be answered from "what was
              // asked", so it is captured at upload rather than reconstructed.
              if (respondsTo) body.append("responds_to_document_id", respondsTo);
              await api("/knowledge/documents", { method: "POST", body });
              setFile(null);
              setRespondsTo("");
              uploadForm.reset();
            });
          }}
        >
          <div className="upload-row">
            <input
              type="file"
              accept=".pdf,.docx,.xlsx,.pptx,.csv,.txt,.md"
              onChange={(event) => setFile(event.target.files?.[0] ?? null)}
            />
            <select
              value={docType}
              onChange={(event) => {
                setDocType(event.target.value);
                if (!NEEDS_TENDER_LINK.has(event.target.value)) setRespondsTo("");
              }}
              required
              aria-label="Document kind"
            >
              <option value="">Choose the kind of document…</option>
              {COMPANY_DOCUMENT_GROUPS.map((group) => (
                <optgroup key={group.group} label={group.group} title={group.hint}>
                  {group.types.map((type) => (
                    <option key={type.value} value={type.value}>
                      {type.label}
                    </option>
                  ))}
                </optgroup>
              ))}
            </select>
            <button disabled={!file || !docType}>Upload document</button>
          </div>

          {NEEDS_TENDER_LINK.has(docType) && (
            <div className="upload-row">
              <select
                value={respondsTo}
                onChange={(event) => setRespondsTo(event.target.value)}
                aria-label="Which past tender this answered"
              >
                <option value="">Which past tender did this answer? (optional)</option>
                {pastTenders.map((doc) => (
                  <option key={doc.id} value={doc.id}>
                    {doc.title || doc.original_filename}
                  </option>
                ))}
              </select>
              <span className="hint">
                {pastTenders.length === 0
                  ? "No past tenders filed yet — upload the tender first to link them."
                  : "Linking lets the system tell what was asked from what you offered."}
              </span>
            </div>
          )}
        </form>
        <p className="hint block">
          These are <b>company</b> documents: they never define requirements, they only
          prove things. Choosing the kind is a filing decision, not a guess — it is
          recorded as yours, and it decides what the extractor looks for. A
          certificate is read for its expiry date; a policy is not. Extraction runs
          automatically and everything it produces arrives unapproved.
        </p>

        <div className="panel-head bulk-head">
          <div>
            <p className="kicker">OR ADD MANY AT ONCE</p>
            <h2>Upload a ZIP</h2>
          </div>
        </div>
        <form
          className="upload-row"
          onSubmit={(event) => {
            event.preventDefault();
            if (!archive) return;
            const uploadForm = event.currentTarget;
            action("Archive upload", async () => {
              const body = new FormData();
              body.append("file", archive);
              body.append("confidentiality_class", "INTERNAL");
              body.append("language", "en");
              setArchiveResult(
                await api<ArchiveResult>("/knowledge/documents/bulk", {
                  method: "POST",
                  body,
                }),
              );
              setArchive(null);
              uploadForm.reset();
            });
          }}
        >
          <input
            type="file"
            accept=".zip"
            onChange={(event) => setArchive(event.target.files?.[0] ?? null)}
          />
          <button disabled={!archive}>Upload archive</button>
        </form>
        <p className="hint block">
          Put each kind in its own folder and the archive files itself — <b>CVs/</b>,{" "}
          <b>Policies/</b>, <b>Certifications/</b>, <b>Previous Tenders/</b>,{" "}
          <b>Our Technical Proposals/</b>. A folder that matches nothing leaves its
          documents unclassified rather than guessed at, and they appear on the filed
          list for you to correct. One unusable file does not sink the archive: it is
          reported by name and the rest are still filed. Nested ZIPs are refused.
          The <b>What to provide</b> tab lists every folder name.
        </p>
        {archiveResult && <ArchiveSummary result={archiveResult} />}
      </section>

      <section className="panel">
        <div className="panel-head">
          <div>
            <p className="kicker">
              STEP 2 · REVIEW AND APPROVE
              {pending > 0 ? ` · ${pending} AWAITING APPROVAL` : ""}
            </p>
            <h2>Company knowledge</h2>
          </div>
          <div className="tabs">
            {TABS.map((item) => (
              <button
                key={item.key}
                className={item.key === tab ? "tab active" : "tab"}
                onClick={() => setTab(item.key)}
                title={item.hint}
              >
                {item.label}
              </button>
            ))}
          </div>
        </div>

        {tab === "checklist" && <Checklist documents={documents.data ?? []} onGoToFiled={() => setTab("documents")} />}
        {tab === "documents" && <DocumentTable rows={documents} action={action} />}
        {tab === "evidence" && (
          <EvidenceTable rows={evidence} action={action} pending={pending} />
        )}
        {tab === "entities" && (
          <EntityTable
            rows={entities}
            open={openEntity}
            onOpen={setOpenEntity}
            facts={entityDetail.data?.facts ?? []}
          />
        )}
        {tab === "review" && <ReviewTable rows={review} action={action} />}
        {tab === "people" && <PeopleTable rows={people} action={action} />}
      </section>
    </>
  );
}

/** What the system expects, and whether it has it.
 *
 *  Written as a checklist rather than prose because the question a person
 *  actually has is "what have I not given it yet", and a list of 41 document
 *  kinds with no state against them answers that no better than the dropdown
 *  already did. The folder column is what makes a bulk upload self-filing.
 */
function Checklist({
  documents,
  onGoToFiled,
}: {
  documents: CompanyDoc[];
  onGoToFiled: () => void;
}) {
  const filed = new Set(
    documents.map((doc) => doc.document_type).filter((type): type is string => Boolean(type)),
  );
  const total = COMPANY_DOCUMENT_GROUPS.reduce(
    (sum, group) => sum + group.types.length,
    0,
  );
  const have = COMPANY_DOCUMENT_GROUPS.reduce(
    (sum, group) => sum + group.types.filter((type) => filed.has(type.value)).length,
    0,
  );
  // "Not provided" is measured by kind, so a corpus that is fully uploaded but
  // unclassified reads as entirely missing. That is technically true and
  // practically misleading, so the count of filed-but-unclassified documents is
  // stated rather than left to be inferred from a wall of "not provided".
  const unclassified = documents.filter((doc) => !doc.document_type).length;
  return (
    <>
      {unclassified > 0 && (
        <p className="hint block warn">
          <b>
            {unclassified} document{unclassified === 1 ? " is" : "s are"} filed with no
            kind
          </b>
          , so nothing below counts them yet. Their folder names matched nothing on
          this list. Set the kind on the{" "}
          <button className="linklike" onClick={onGoToFiled}>
            Filed documents
          </button>{" "}
          tab — no need to upload them again.
        </p>
      )}
      <p className="hint block">
        {have} of {total} document kinds are on file. Nothing here is individually
        mandatory — what a given tender demands is decided by its own requirements —
        but a kind you never provide can never back a claim. Put files in a folder
        named as shown and a ZIP upload files them without you choosing anything.
      </p>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Document kind</th>
              <th>ZIP folder</th>
              <th>On file</th>
            </tr>
          </thead>
          <tbody>
            {COMPANY_DOCUMENT_GROUPS.map((group) => (
              <Fragment key={group.group}>
                <tr className="group-row">
                  <td colSpan={3}>
                    <b>{group.group}</b>
                    <span className="hint"> {group.hint}</span>
                  </td>
                </tr>
                {group.types.map((type, index) => (
                  <tr key={type.value}>
                    <td>{type.label}</td>
                    <td className="folder">
                      {index === 0 ? GROUP_FOLDER_HINT[group.group] ?? "" : ""}
                    </td>
                    <td>
                      {filed.has(type.value) ? (
                        <span className="sev sev-ok">on file</span>
                      ) : (
                        <span className="hint">not provided</span>
                      )}
                    </td>
                  </tr>
                ))}
              </Fragment>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

/** What the archive actually did, named file by file.
 *
 *  A bulk upload that reports only a count hides the two things worth knowing:
 *  which files were refused, and which landed unclassified because their folder
 *  matched nothing. Both are correctable, but only if they are visible.
 */
function ArchiveSummary({ result }: { result: ArchiveResult }) {
  return (
    <div className="archive-summary">
      <div className="analysis-figures">
        <div>
          <small>Filed</small>
          <strong>{result.filed}</strong>
        </div>
        <div>
          <small>Unclassified</small>
          <strong>{result.unclassified}</strong>
        </div>
        <div>
          <small>Refused</small>
          <strong>{result.skipped}</strong>
        </div>
      </div>
      {result.unclassified > 0 && (
        <p className="hint block">
          {result.unclassified} file{result.unclassified === 1 ? "" : "s"} landed with
          no kind, because the folder matched nothing on the checklist. They are
          ingested and searchable, but a kind is what tells the extractor what to look
          for — set it from the filed list, or re-upload them under a named folder.
        </p>
      )}
      {result.rejected.length > 0 && (
        <ul className="issue-list">
          {result.rejected.map((item) => (
            <li key={item.file}>
              <div className="issue-head">
                <span className="sev sev-major">REFUSED</span>
                <span className="issue-status">{item.file}</span>
              </div>
              <p className="issue-body">{item.reason}</p>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function DocumentTable({
  rows,
  action,
}: {
  rows: { data?: CompanyDoc[]; isLoading: boolean; error: Error | null };
  action: Action;
}) {
  if (rows.isLoading) return <Empty text="Loading filed documents…" />;
  if (rows.error) return <Empty text={rows.error.message} />;
  const data = rows.data ?? [];
  if (data.length === 0)
    return <Empty text="Nothing filed yet. Upload a company document above." />;
  const byId = new Map(data.map((doc) => [doc.id, doc]));
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>File</th>
            <th>Filed as</th>
            <th>Answers</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {data.map((doc) => (
            <tr key={doc.id}>
              <td>{doc.title || doc.original_filename}</td>
              <td>
                {doc.document_type ? (
                  TYPE_LABELS[doc.document_type] ?? doc.document_type
                ) : (
                  <span className="hint">not classified</span>
                )}
              </td>
              <td>
                {doc.responds_to_document_id
                  ? byId.get(doc.responds_to_document_id)?.title ??
                    byId.get(doc.responds_to_document_id)?.original_filename ??
                    "a past tender"
                  : "—"}
              </td>
              <td className="row-action">
                {/* Correcting in place, rather than re-uploading: a duplicate
                    corpus is a high price for fixing a label. */}
                <select
                  defaultValue=""
                  aria-label={`Set the kind for ${doc.original_filename}`}
                  onChange={(event) => {
                    const value = event.target.value;
                    if (!value) return;
                    action("Document filing", () =>
                      api(`/knowledge/documents/${doc.id}/classify`, {
                        method: "POST",
                        body: JSON.stringify({ document_type: value }),
                      }),
                    );
                  }}
                >
                  <option value="">{doc.document_type ? "Change kind…" : "Set kind…"}</option>
                  {COMPANY_DOCUMENT_GROUPS.map((group) => (
                    <optgroup key={group.group} label={group.group}>
                      {group.types.map((type) => (
                        <option key={type.value} value={type.value}>
                          {type.label}
                        </option>
                      ))}
                    </optgroup>
                  ))}
                </select>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function EvidenceTable({
  rows,
  action,
  pending,
}: {
  rows: { data?: Evidence[]; isLoading: boolean; error: Error | null };
  action: Action;
  pending: number;
}) {
  if (rows.isLoading) return <Empty text="Loading evidence…" />;
  if (rows.error) return <Empty text={rows.error.message} />;
  const data = rows.data ?? [];
  if (data.length === 0)
    return (
      <Empty text="No evidence yet. Upload a company document above — extraction produces evidence automatically." />
    );
  return (
    <>
      <div className="bulk-approve">
        <p className="hint">
          <b>Semantic matching needs each excerpt embedded.</b> Evidence extracted
          before the vector column existed has none, so a requirement can only find
          it by shared words. Run this once after installing the embedding model;
          it reports how many remain, so repeat until zero.
        </p>
        <button
          className="ghost"
          onClick={() =>
            action(
              "Evidence embedding",
              () =>
                api<{ embedded: number; remaining: number }>(
                  "/knowledge/evidence/backfill-vectors",
                  { method: "POST" },
                ),
              // The endpoint is bounded per call and returns `remaining` for
              // exactly this reason. Reporting only "completed" left the one
              // number that says whether to press again unread — and a run
              // that embedded nothing at all looked identical to one that
              // finished the corpus.
              (result) =>
                result.remaining === 0
                  ? `${result.embedded} embedded. Every excerpt now has a vector.`
                  : `${result.embedded} embedded, ${result.remaining} still without a vector — run it again.`,
            )
          }
        >
          Embed evidence for semantic search
        </button>
      </div>
      {pending > 0 && (
        <div className="bulk-approve">
          <p className="hint">
            <b>
              {pending} excerpt{pending === 1 ? "" : "s"} are not usable yet.
            </b>{" "}
            Only approved evidence is matched against tender requirements, so nothing
            below can back a claim yet. Read enough of them to be comfortable, then
            approve the set — a claim in a submitted proposal traces back to whoever
            approved the excerpt behind it. Approving is the{" "}
            <b>Knowledge Admin&rsquo;s</b> job, not the bid manager&rsquo;s: sign in as{" "}
            <code>knowledge@acme-cyber.example.com</code>.
          </p>
          {/* The paragraph above already says whose job this is, and it was on
              screen when a bid manager clicked the button anyway and got a 403
              from the server. Prose next to a live control does not stop a
              click; a disabled control does. */}
          <Gated need="evidence:approve">
            <button
              onClick={() =>
                action(
                  "Bulk evidence approval",
                  () =>
                    api<{ approved: number; skipped: { id: string; reason: string }[] }>(
                      "/knowledge/evidence/approve-batch",
                      {
                        method: "POST",
                        body: JSON.stringify({
                          reason: "Reviewed and approved in bulk from the evidence list.",
                        }),
                      },
                    ),
                  // Skipped rows are the point of reporting this. Expired
                  // evidence cannot be approved for reuse and is passed over
                  // rather than failing the batch, so a silent "completed"
                  // hides the rows that did not make it in.
                  (result) =>
                    result.skipped.length === 0
                      ? `${result.approved} excerpts approved and now matchable.`
                      : `${result.approved} approved, ${result.skipped.length} skipped as expired — re-verify those before they can back a claim.`,
                )
              }
            >
              Approve all {pending} for reuse
            </button>
          </Gated>
        </div>
      )}
      <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Excerpt</th>
            <th>Type</th>
            <th>Approval</th>
            <th>Verified</th>
            <th>Expires</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {data.map((row) => (
            <tr key={row.id}>
              <td className="excerpt">{row.excerpt}</td>
              <td>{row.evidence_type}</td>
              <td>
                <span
                  className={
                    row.approval_status === "APPROVED" ? "sev sev-ok" : "sev sev-major"
                  }
                >
                  {row.approval_status.replaceAll("_", " ")}
                </span>
              </td>
              {/* Approval and verification are separate: evidence can be
                  approved and still unusable while its status is PROPOSED. */}
              <td>
                <span
                  className={
                    row.status === "CURRENT_VERIFIED" || row.status === "HISTORICAL_VERIFIED"
                      ? "sev sev-ok"
                      : "sev sev-major"
                  }
                >
                  {(row.status ?? "—").replaceAll("_", " ")}
                </span>
              </td>
              <td>{row.expiry_date ?? "—"}</td>
              <td className="row-action">
                {row.approval_status !== "APPROVED" && (
                  <button
                    className="ghost"
                    onClick={() =>
                      // Approving is what makes an excerpt usable as proof.
                      // Until then it exists but cannot back a claim.
                      action("Evidence approval", () =>
                        api(`/knowledge/evidence/${row.id}/approve`, { method: "POST" }),
                      )
                    }
                  >
                    Approve for reuse
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      </div>
    </>
  );
}

function EntityTable({
  rows,
  open,
  onOpen,
  facts,
}: {
  rows: { data?: Entity[]; isLoading: boolean; error: Error | null };
  open: string | null;
  onOpen: (id: string | null) => void;
  facts: Fact[];
}) {
  if (rows.isLoading) return <Empty text="Loading entities…" />;
  if (rows.error) return <Empty text={rows.error.message} />;
  const data = rows.data ?? [];
  if (data.length === 0) return <Empty text="No entities extracted yet." />;
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Name</th>
            <th>Type</th>
            <th>Classification</th>
            <th>Status</th>
            <th>Approval</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {data.map((row) => (
            <Fragment key={row.id}>
              <tr>
                <td>{row.canonical_name}</td>
                <td>{row.entity_type}</td>
                <td>{row.classification}</td>
                <td>{row.status}</td>
                <td>{row.approval_status.replaceAll("_", " ")}</td>
                <td className="row-action">
                  <button
                    className="ghost"
                    onClick={() => onOpen(open === row.id ? null : row.id)}
                  >
                    {open === row.id ? "Hide facts" : "Facts"}
                  </button>
                </td>
              </tr>
              {open === row.id && (
                <tr>
                  <td colSpan={6}>
                    <FactList facts={facts} />
                  </td>
                </tr>
              )}
            </Fragment>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function FactList({ facts }: { facts: Fact[] }) {
  if (facts.length === 0) return <p className="hint">No facts recorded for this entity.</p>;
  return (
    <table className="inner">
      <thead>
        <tr>
          <th>Predicate</th>
          <th>Value</th>
          <th>Valid to</th>
          <th>Approval</th>
        </tr>
      </thead>
      <tbody>
        {facts.map((fact) => (
          <tr key={fact.id}>
            <td>{fact.predicate}</td>
            <td>
              {fact.value_text ??
                (fact.value_number !== null && fact.value_number !== undefined
                  ? `${fact.value_number}${fact.unit ? ` ${fact.unit}` : ""}`
                  : fact.value_date) ??
                "—"}
            </td>
            {/* A fact past its validity window is not proof of anything now. */}
            <td>{fact.valid_to ?? "—"}</td>
            <td>{fact.approval_status.replaceAll("_", " ")}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function ReviewTable({
  rows,
  action,
}: {
  rows: { data?: ReviewItem[]; isLoading: boolean; error: Error | null };
  action: Action;
}) {
  const [resolution, setResolution] = useState<Record<string, string>>({});
  // Checked before the empty case below, and deliberately so. The query is now
  // disabled for accounts without the permission, and a disabled query has no
  // data and no error — so falling through would have reached "Nothing
  // waiting. The extractor resolved everything it saw," which is not a gentler
  // way of saying the same thing. It is the opposite claim, and it would be
  // read as a clean queue by exactly the people who cannot see the queue.
  if (useCan("knowledge:approve") === false)
    return <Empty text={explainMissingPermission("knowledge:approve")} />;
  if (rows.isLoading) return <Empty text="Loading the review queue…" />;
  if (rows.error)
    return (
      <Empty text="The review queue needs the knowledge:approve permission — sign in as the knowledge admin to work it." />
    );
  const data = rows.data ?? [];
  if (data.length === 0)
    return <Empty text="Nothing waiting. The extractor resolved everything it saw." />;
  return (
    <ul className="issue-list">
      {data.map((item) => (
        <li key={item.id}>
          <div className="issue-head">
            <span className="pill">{item.kind.replaceAll("_", " ")}</span>
            <span className="issue-status">{item.subject_type}</span>
          </div>
          {item.detail && (
            <p className="issue-body">{JSON.stringify(item.detail).slice(0, 400)}</p>
          )}
          <form
            className="answer-form"
            onSubmit={(event) => {
              event.preventDefault();
              const text = (resolution[item.id] ?? "").trim();
              if (text.length < 3) return;
              action("Review resolution", () =>
                api(`/knowledge/review-queue/${item.id}/resolve`, {
                  method: "POST",
                  body: JSON.stringify({ resolution: text }),
                }),
              );
            }}
          >
            <label>
              How you resolved it, for the record
              <textarea
                rows={2}
                value={resolution[item.id] ?? ""}
                onChange={(event) =>
                  setResolution({ ...resolution, [item.id]: event.target.value })
                }
                required
                minLength={3}
              />
            </label>
            <div className="issue-actions">
              <button disabled={(resolution[item.id] ?? "").trim().length < 3}>
                Resolve
              </button>
            </div>
          </form>
        </li>
      ))}
    </ul>
  );
}

function Empty({ text }: { text: string }) {
  return (
    <div className="empty">
      <span>◌</span>
      <p>{text}</p>
    </div>
  );
}


/**
 * The review list for CV-to-account matches.
 *
 * Two things this screen has to make obvious. A suggestion is not a decision —
 * a PROPOSED row has done nothing until somebody says yes. And a confirmed link
 * is a statement of fact, not a grant: it lets a staffing answer name a real
 * colleague and lets a bid manager route a task to the right person, and it
 * confers no access of any kind.
 *
 * Bulk confirmation is offered only for the certain matches. An exact name
 * against a small organization is not a decision anybody learns from; a
 * surname-and-initial guess is exactly the kind of thing that should cost a
 * click, so it is left out of the sweep on purpose.
 */
function PeopleTable({
  rows,
  action,
}: {
  rows: { data?: PersonLink[]; isLoading: boolean; error: unknown };
  action: Action;
}) {
  if (rows.isLoading) return <Empty text="Loading people…" />;
  if (rows.error)
    return <Empty text="You do not have permission to see the people list." />;

  const links = rows.data ?? [];
  const proposed = links.filter((r) => r.status === "PROPOSED");
  const certain = proposed.filter((r) => (r.confidence ?? 0) >= 0.9 && r.account_id);
  const unmatched = proposed.filter((r) => !r.account_id);

  return (
    <>
      <div className="toolbar">
        <button
          className="secondary"
          onClick={() =>
            action("Match people to accounts", () =>
              api("/knowledge/people/match", { method: "POST", body: "{}" }),
            )
          }
        >
          Find matches
        </button>
        {certain.length > 0 && (
          <button
            onClick={() =>
              action(`Confirm ${certain.length} certain match(es)`, () =>
                api("/knowledge/people/confirm-all", {
                  method: "POST",
                  body: JSON.stringify({ min_confidence: 0.9 }),
                }),
              )
            }
          >
            Confirm {certain.length} certain
          </button>
        )}
        <span className="spacer" />
        <span className="pill">{links.length} person/people</span>
      </div>

      <p className="hint block">
        A confirmed link says <b>this CV describes this colleague</b>. It grants no
        access, no role and no permission — it lets a staffing answer name a real
        person, and lets work about them be routed to the right account.
        {unmatched.length > 0 && (
          <>
            {" "}
            <b>{unmatched.length} could not be matched</b> and need an account
            chosen by hand.
          </>
        )}
      </p>

      {!links.length ? (
        <Empty text="No people extracted yet. Upload CVs, then choose Find matches." />
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Person (from the CV)</th>
                <th>Account</th>
                <th>Matched on</th>
                <th>Status</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {links.map((row) => (
                <tr key={row.link_id}>
                  <td>{row.person}</td>
                  <td>
                    {row.account_email ? (
                      <>
                        {row.account_name}
                        <br />
                        <span className="muted">{row.account_email}</span>
                      </>
                    ) : (
                      <span className="muted">—</span>
                    )}
                  </td>
                  <td>
                    <span className="muted">
                      {row.method.replace(/_/g, " ")}
                      {row.confidence != null && ` · ${Math.round(row.confidence * 100)}%`}
                    </span>
                    {row.note && (
                      <>
                        <br />
                        <span className="muted">{row.note}</span>
                      </>
                    )}
                  </td>
                  <td>
                    <span className={row.status === "CONFIRMED" ? "pill" : "pill warn"}>
                      {row.status.toLowerCase()}
                    </span>
                  </td>
                  <td>
                    {row.status === "PROPOSED" && (
                      <>
                        {row.account_id && (
                          <button
                            className="small"
                            onClick={() =>
                              action(`Confirm ${row.person}`, () =>
                                api(`/knowledge/people/${row.link_id}`, {
                                  method: "POST",
                                  body: JSON.stringify({ confirm: true }),
                                }),
                              )
                            }
                          >
                            Confirm
                          </button>
                        )}{" "}
                        <button
                          className="small ghost"
                          onClick={() =>
                            action(`Reject the match for ${row.person}`, () =>
                              api(`/knowledge/people/${row.link_id}`, {
                                method: "POST",
                                body: JSON.stringify({ confirm: false }),
                              }),
                            )
                          }
                        >
                          Not this person
                        </button>
                      </>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
