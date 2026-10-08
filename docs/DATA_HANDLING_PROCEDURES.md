# Gradimize — Data Handling & Security Procedures

**Prepared by:** Martilias Farrell, PhD, Department of Natural and Applied Sciences · **Date:** July 15 2026 · *rev. July 28, 2026*
**Status:** Project technical document. Reviewed by the University's policy office (July 2026), which confirmed that **no new University policy is required** — the data handling and security controls described here are already addressed by existing university policies and procedures. This document implements those existing requirements for the Gradimize project; it does not supersede or substitute for them.

> **The key point up front:** the scheduling server never holds a name, email, or
> real student ID. Every student record is keyed to an irreversible pseudonym, and
> the re-identification key never leaves the Registrar/IT office. Even a full compromise of
> the server would therefore expose no directly identifiable student data — the
> worst case is pseudonymized academic records. We nonetheless do **not** claim the
> data is exempt from FERPA, and we protect it as if it were identifiable (§ 4, § 5).

## 1. Purpose

Gradimize is a course-scheduling optimizer operated on university infrastructure by
university employees. It builds and evaluates section schedules; it does not make
decisions about individual students, and no *identified* individual-level output leaves
the system (enrollment exports are keyed to pseudonyms only, like everything else).
This document defines exactly what data it receives, in what form, and under what controls.

## 2. Key design decision: the scheduling server never receives identifiers

Gradimize's algorithms need to know *that* two demands belong to the same student —
never *who* the student is. Accordingly:

- **Student names, emails, phone numbers, addresses, DOB, and SSN are not requested,
  not transmitted, and cannot be stored** — the import pipeline unconditionally drops
  these columns before any row is read, so even a raw extract delivered by mistake
  cannot land a student identifier in the database (enforced in code, covered by an
  automated test). Faculty names are ordinary directory information and are kept.
- **Student IDs are pseudonymized before delivery.** The data owner (Registrar/IT) runs
  the provided `scripts/deidentify.py` tool on the raw extracts. It replaces every
  Banner ID with a keyed one-way pseudonym (HMAC-SHA256). The secret key — and the
  optional pseudonym-to-ID mapping — **remain with the Registrar/IT office**. Without
  the key, pseudonyms cannot be reversed.
- Pseudonyms are consistent across files and refreshes, so records still link, but the
  scheduling server holds no means of re-identification.

This is verified by an automated test suite (`tests/test_deidentify.py`) demonstrating
that no identifier survives into the delivered files and that the de-identified files
import cleanly.

## 3. Data elements requested, with purpose

| Element | Why the optimizer needs it |
|---|---|
| Pseudonymous student key | Links one student's demands so their courses don't collide |
| Class standing, credits earned | Urgency: seniors needing required courses are prioritized |
| Major / concentration / minor, catalog year | Determines which degree requirements apply |
| Course history (course, grade, term) | Prerequisite eligibility (min-grade rules) and remaining requirements |
| Current registrations (course/section per pseudonym) | Real demand for the schedule back-test; finals grid from real enrollments |
| Catalog, prerequisites, degree requirements, semester guides | Curriculum structure (contains no student data) |
| Faculty assignments/availability, rooms, time grid | Operational scheduling inputs (directory/facility data) |

**Explicitly excluded:** name, email, phone, address, DOB, SSN, financial aid, health,
disability, disciplinary, immigration, and GPA-as-such (only per-course grades needed
for prerequisite rules are included, tied to pseudonyms only).

## 4. Legal basis

1. **FERPA school-official exception** (34 CFR § 99.31(a)(1)): the operators are
   university employees performing an institutional function (course scheduling) with a
   legitimate educational interest.
2. **Data minimization as defense-in-depth:** because identifiers are removed and the
   re-identification key never leaves the Registrar/IT office, the data on the scheduling server
   is pseudonymized. We deliberately do *not* claim it is fully anonymous under
   § 99.31(b) — small course cohorts could in principle be re-identified by someone with
   deep institutional knowledge — which is why the server is additionally protected by
   the technical controls in § 5, and access is limited as if it were identifiable data.

## 5. Technical & physical safeguards (implemented, verifiable)

| Control | Implementation |
|---|---|
| Physical | Single on-prem server in a locked room; no cloud storage or third parties |
| Server access | Physical console, or SSH with key-based authentication only — password login over the network is disabled in the SSH daemon. There is no remote path onto the server that accepts just a password |
| Minimal exposure | The system is fully operable from the terminal over SSH (import, generation, review, exports). The web interface is optional; when it is not running, the application listens on no network ports at all |
| Network | LAN-only; no public exposure |
| Transport encryption | When the optional web interface is served: HTTPS/TLS only (`serve.sh --secure`); plain HTTP refused |
| Access control | When served: a per-deployment access token is required on every API request; unauthenticated requests rejected (401) |
| Data minimization | Identifier columns dropped at import even if accidentally delivered |
| Source-control isolation | Student data can never enter version control: the data directory is excluded from git and the live database is kept outside the repository entirely, on a dedicated data drive; the code repository is private |
| Backups | Nightly automated database snapshot to a separate internal drive in the same server (30-copy rolling retention). Backups hold the same pseudonymized-only data, never leave the machine, and are deleted on the same schedule as the primary (§ 7) |
| Auditability | All schedule outputs versioned; application code under version control |

## 6. Handling workflow

1. Registrar/IT produces the standard extracts (per `exports/Gradimize_Data_Request.md`).
2. Registrar/IT runs `python scripts/deidentify.py RAW_DIR OUT_DIR` on a machine they
   control. The key file and any mapping stay with them.
3. Only `OUT_DIR` (de-identified) is delivered to the Gradimize server, by USB drive or
   campus network copy.
4. Raw extracts on the delivery medium are deleted after transfer.
5. Refreshes repeat steps 1–4 with the same key file so records stay linked.

## 7. Retention & disposal

De-identified data is retained only while scheduling work for the covered terms is
active, and deleted (database and any backup copies) within 30 days of a written
request from the Registrar/IT or at project end. The deletion is confirmed in writing.

## 8. Roles

| Role | Person/office | Responsibility |
|---|---|---|
| Data owner | Registrar/IT | Approves extracts, holds de-identification key |
| System operator | Martilias Farrell, PhD, Dept. of Natural & Applied Sciences | Runs Gradimize, maintains controls |
| Policy review | University policy office | Reviewed July 2026 — no new University policy required (see Status, above) |

## 9. Incident response

Suspected compromise of the server or media: operator disconnects the machine from the
network, notifies the Registrar and IT security the same day, and provides the server
logs. Because the server holds no identifiers or key, exposure is limited to
pseudonymized academic records.

---
*Questions about anything described here: Martilias Farrell, PhD*
