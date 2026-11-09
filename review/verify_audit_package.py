"""Refuse to complete a review package whose files do not describe one run and one package.

Checks, in order: the audit files against the package manifest (by delivered filename); the audit
rows' event identity; the validation report's register counts against the register itself; that
the manifest names no internal file; that the test-output file is present. Then writes
06-delivered-files.json covering every other file in the folder.
"""
import csv, hashlib, json, mimetypes, re, sys
from pathlib import Path

d = Path(sys.argv[1])
bad = []
man = json.loads((d / "05-package-manifest.json").read_text(encoding="utf-8"))
j = json.loads((d / "04-contradiction-audit.json").read_text(encoding="utf-8"))
rows = list(csv.DictReader((d / "04-contradiction-audit.csv").open(encoding="utf-8")))
sha = lambda p: hashlib.sha256((d / p).read_bytes()).hexdigest()

# 05 names the files as delivered, and every hash matches.
for name, digest in man["sha256"].items():
    if not (d / name).is_file():
        bad.append(f"the manifest names {name}, which is not in the package")
    elif sha(name) != digest:
        bad.append(f"{name}: hash differs from the manifest")
for need in ("04-contradiction-audit.json", "04-contradiction-audit.csv"):
    if need not in man["sha256"]:
        bad.append(f"the manifest does not list {need}")

if j["package_id"] != man["package_id"] or j["consistency_run_id"] != man["consistency_run_id"]:
    bad.append("JSON run/package differs from the manifest")
if {r["package_id"] for r in rows} != {man["package_id"]}: bad.append("CSV package differs from the manifest")
if {r["consistency_run_id"] for r in rows} != {man["consistency_run_id"]}: bad.append("CSV run differs from the manifest")
if len(rows) != len(j["records"]): bad.append("CSV and JSON record counts differ")
act_c = sorted(r["finding_id"] for r in rows if r["active"] == "True")
act_j = sorted(r["finding_id"] for r in j["records"] if r["active"])
if act_c != act_j or act_c != sorted(man["audit_active_findings"]): bad.append("active findings differ")
if len(act_c) != len(set(act_c)): bad.append("a finding is active twice")
if man["internal_issues_in_register"] != len(act_c): bad.append("register internal issues differ from the audit's active findings")

# Every audit row is its own event.
events = [r["audit_event_id"] for r in j["records"]]
if len(events) != len(set(events)): bad.append("two audit rows share one event id")
if [r["audit_event_id"] for r in rows] != events: bad.append("CSV and JSON event ids differ")
known = set(events)
for r in j["records"]:
    if r["supersedes_event_id"] and r["supersedes_event_id"] not in known:
        bad.append(f"{r['audit_event_id']} replaces an event that is not in the audit")

# The validation report and the register must state the same counts.
rep = (d / "01-validation-report.md").read_text(encoding="utf-8")
reg = (d / "02-clarification-register.md").read_text(encoding="utf-8")
if man["package_id"] not in rep: bad.append("the validation report is for a different package")
def heading(section):
    m = re.search(rf"^## {section}\..*?\((\d+)\)", reg, re.MULTILINE)
    return int(m.group(1)) if m else None
line = re.search(r"Clarification register counts: (.*)\.", rep)
claimed = {}
if line:
    for part in line.group(1).split(", "):
        label, _, n = part.rpartition(" ")
        claimed[label] = int(n)
for label, section in (("blocking", "A1"), ("optional", "A2"), ("bidder decisions", "A3"), ("answered in tender", "A4")):
    actual = heading(section)
    if actual is None or claimed.get(label) != actual:
        bad.append(f"validation report says {label} {claimed.get(label)}, the register says {actual}")
if not (d / "07-full-checks-output.txt").is_file():
    bad.append("07-full-checks-output.txt is missing: the test claim must be auditable")

print("package", man["package_id"], "run", man["consistency_run_id"], "records", len(rows), "active", len(act_c))
if bad:
    print("FAIL:", *bad, sep="\n  - ")
    sys.exit(1)

# Manifest of everything delivered. It cannot hash itself, so it says so.
mimetypes.add_type("application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx")
SELF = "06-delivered-files.json"
files = []
for p in sorted(d.iterdir()):
    if not p.is_file() or p.name == SELF:
        continue
    data = p.read_bytes()
    files.append({
        "filename": p.name,
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "mime_type": mimetypes.guess_type(p.name)[0] or "application/octet-stream",
        "designation": "authored" if p.name.startswith(("00-",)) else "generated",
    })
(d / SELF).write_text(json.dumps({
    "package_id": man["package_id"], "consistency_run_id": man["consistency_run_id"],
    "manifest_scope": "every delivered file except this manifest",
    "excluded_self": SELF,
    "file_count": len(files), "files": files}, indent=2), encoding="utf-8")
print("delivered-file manifest written:", len(files), "files (this manifest excluded by design)")
print("audit package verified: one run, one package, hashes and counts agree")
