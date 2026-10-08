# Gradimize — Security Posture Summary

*One page for reviewers. Every claim below is testable on the running system; the
commands shown were executed against the hardened server on 2026-07-15.*

## What this system is
A course-scheduling optimizer on a single on-premises server (locked room), used by
university staff on the campus LAN. Single-writer application; a handful of viewers.

> **Why the stakes are structurally low:** the server never receives names, emails,
> or real student IDs — records arrive already pseudonymized, and the key to reverse
> the pseudonyms stays with the Registrar/IT office. Even a full compromise would expose only
> de-identified academic records. The controls below exist on top of that floor, and
> the data is still handled as FERPA-protected (not claimed exempt — see
> DATA_HANDLING_PROCEDURES.md § 4).

## Server access
- **Two ways in, both strong:** the physical console (locked room, 114A), or SSH with
  key-based authentication only. Password and keyboard-interactive login are disabled
  in the SSH daemon (`PasswordAuthentication no`, `KbdInteractiveAuthentication no`)
  — there is no remote *login* path onto this server that accepts just a password.
- **One administrative account.** The host has a single user account (the operator's).
- **The web interface is optional.** Every major function — data import, schedule
  generation, review, conflict analysis, finals scheduling, exports — runs in the
  terminal interface over that same SSH session. When the web server is not running
  (the default), the application listens on **no network ports at all**; the controls
  in the next two sections apply only when browser access is deliberately started
  for viewers.

## Data
- **No direct identifiers.** Names/emails/etc. are never requested; student IDs arrive
  pseudonymized by Registrar/IT (`scripts/deidentify.py`, HMAC-SHA256; key stays with
  Registrar/IT). The import pipeline drops identifier columns defensively.
- Holdings: pseudonymized academic records (standing, major, course history,
  registrations) + non-personal curriculum/facility data.
- **Data isolated from source control.** The live database and any imported
  extracts are never committed. `.gitignore` blocks the entire `data/` directory
  by default (only synthetic sample files are tracked), app-generated exports
  (schedule/enrollment CSVs) are likewise ignored, and the live database is
  kept entirely outside the repository (`GRADIMIZE_DATA_DIR` points at a
  dedicated data drive) — so no git operation can capture or push student
  data even by accident. The source repository is private.
- **Nightly backups, same custody.** An automated nightly snapshot of the
  database lands on a second internal drive in the same machine (30-copy
  rolling retention). Backups contain the same pseudonymized-only data, never
  leave the server, and are deleted on the same schedule as the primary
  (DATA_HANDLING_PROCEDURES.md § 7).

## Network & transport (when the web interface is served)
- **LAN only.** No ports exposed off-campus.
- **Host firewall (ufw) active, default-deny inbound.** SSH (22) and the Gradimize
  HTTPS port (8443) are explicitly allowed; the OS print service has been disabled
  outright.
- **LAN-facing service exists only in hardened mode.** The development mode binds
  to localhost and is unreachable from the network; every browser-facing deployment
  is `./serve.sh --secure`.
- **HTTPS only**: `./serve.sh --secure` runs a single TLS endpoint (`:8443`);
  plain HTTP connections are refused.
  - Verify: `curl http://SERVER:8443/api/health` → connection error;
    `curl -k https://SERVER:8443/api/health` → `{"status":"healthy"}`.
- Certificate: self-signed with published SHA-256 fingerprint pending an internal-CA
  certificate from campus IT (recommended; drop-in replacement at `certs/`).

## Authentication & authorization (when the web interface is served)
- Every API request requires the access token (`Authorization: Bearer …`);
  unauthenticated → HTTP 401. Verify:
  `curl -k https://SERVER:8443/api/data/stats` → `401`.
- Token generated per deployment (`data/access_token.txt`, mode 600), distributed
  out-of-band, rotatable by deleting the file and restarting.

## Application hardening (selected)
- Security headers on every response (nosniff, frame-deny, referrer-policy).
- Upload size caps (413 beyond 50 MB); imports transactional (a bad file cannot
  destroy existing data); server errors return generic messages (no stack traces).
- SQLite in WAL mode.

## Known limitations (stated plainly)
- Single shared token, not per-user accounts — acceptable for the current 1-editor
  pilot; per-user auth + audit log planned before multi-department use.
- Self-signed certificate until campus IT issues one (browser shows a one-time
  warning; fingerprint verification documented).
- Pseudonymized ≠ anonymous for very small cohorts; handled as if identifiable
  (see DATA_HANDLING_PROCEDURES.md § 4).

## Verification suite
`pytest tests/` (116 tests) includes de-identification guarantees
(`tests/test_deidentify.py`): no identifier survives to delivered files; pseudonyms
are deterministic per key and unlinkable across keys; de-identified extracts import
with zero errors; file formats the tool cannot de-identify (e.g. `.xlsx`) are
refused rather than passed through.
