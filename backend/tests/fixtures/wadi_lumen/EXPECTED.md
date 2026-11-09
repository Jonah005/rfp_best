# Wadi Lumen -- a mediocre vendor against the real Jadwa tender

**Wadi Lumen Software Co. is fictional.** Its commercial registration, office,
staff, clients and projects are invented for testing. It has no connection to
the company behind the Jadwa bid, and none of these files may be uploaded to
that company's organization. `scripts/seed_mediocre_vendor.py` runs it in its
own organization, `wadi-lumen-fixture`.

The tender is real and is NOT in this folder: it is confidential by its own
terms, so the script reads it from wherever it already sits (`--tender`).

## What the vendor is

A 23-person Riyadh .NET web shop. Good at bilingual portals on C# and SQL
Server, has built one investor account-opening portal (2019), writes test
cases, trains users. Has never built SMS or banking integrations, touched
Oracle once, load-tested to 150 users, gives a 30-day warranty, and no longer
supports Windows Server 2012 -- and says so.

The pack is deliberately written the way a real, ordinary company writes: no
gaps are flagged as gaps, some limits are stated plainly, and the relevance
judge has to READ a sentence like "We have not deployed to Windows Server 2012
since 2018" rather than match the words "Windows Server 2012".

## Expectations

| Tender asks for | Group | Why |
|---|---|---|
| C#, SQL Server, English and Arabic, responsive, cross-browser, SSL, IIS, XSS, test cases, roles, knowledge transfer, prior experience | **Answerable** -- must not be BLOCKED | covered in plain words |
| Windows 2012, 3-month pen-test fixes, SMS, B2B bank integration, Dokmee, Google Analytics, signed NDA | **Not answerable** -- must never be READY or DRAFTABLE | silent, or says it cannot |
| Oracle, 300 concurrent users, load test results, data encryption, user manual, key-personnel CVs, bilingual signed PDFs | **Judgement** -- reported only | half-covered; this is what a reviewer is for |

The two "says it cannot" rows (Windows 2012, the three-month warranty) are the
interesting ones. The words match the clause; the meaning is the opposite. A
matcher that counts overlap will call them proof. A reader should not.
