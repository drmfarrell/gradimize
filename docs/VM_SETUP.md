# Gradimize VM Setup

Runbook for deploying Gradimize on an IT-managed VM. Goal: bare VM → running
service in one pass. Pair this with `docs/DATA_HANDLING_PROCEDURES.md` and
`docs/SECURITY_POSTURE.md` before any real data is involved.

## 1. VM specification

| Resource | Requested | Minimum |
|---|---|---|
| vCPU | 16 | 8 |
| RAM | 32 GB | 16 GB |
| Disk | 50 GB (thin-provisioned fine) | 20 GB |
| OS | Ubuntu 24.04 LTS (or similar) | — |
| Runtime | Python 3.12 + venv module; Node 20+ & npm (web UI only); openssl | Python 3.11 |
| Network | LAN-only; no inbound internet exposure | — |

The solver is CPU-bound (OR-Tools CP-SAT). The auto worker count is
`cores − 4` (`src/core/solver_util.py`), so on a machine with ≤ 8 cores set
`num_workers` explicitly in `config/constraints.yaml` (e.g. `6`) instead of
leaving it at `0` (auto).

## 2. Getting the code

**Path A — clone from the repository** (needs network/credential access):

```bash
git clone git@github.com:drmfarrell/gradimize.git /opt/gradimize
```

**Path B — offline git bundle** (no repo access needed on the VM):

On the workstation: `scripts/make_bundle.sh` → produces
`../gradimize-YYYYMMDD.bundle`. Copy that one file to the VM, then:

```bash
git clone gradimize-YYYYMMDD.bundle /opt/gradimize
cd /opt/gradimize && git remote remove origin
```

## 3. Install

```bash
cd /opt/gradimize
scripts/bootstrap.sh --locked
```

Bootstrap is idempotent and does, in order:

- creates `.venv` on Python 3.12 (3.11 accepted) and installs the app
  (`--locked` = pinned versions from `requirements-lock.txt`);
- builds the web UI (`npm ci && npm run build` in `web/`) — skipped with
  `--no-web` or when npm is absent (the TUI still works);
- generates a self-signed TLS cert into `certs/` if none exists;
- seeds the synthetic sample database if the DB is empty and runs
  `scripts/doctor.py` as a smoke check.

Add `--dev` to also install the test tooling (`pytest`, `ruff`, …).

## 4. Running

**Interactive (screen/tmux):**

```bash
screen -S gradimize ./serve.sh --secure
```

Serves HTTPS on **:8443**. The API token is `$GRADIMIZE_TOKEN` if set,
otherwise generated into `data/access_token.txt` (mode 0600). The self-signed
cert produces a one-time browser warning — expected on a LAN deployment.
The TUI runs with `./start.sh` (synthetic sample data; `--fresh` reloads it).

**As a service:** use the template at `deploy/gradimize.service` — copy to
`/etc/systemd/system/`, edit `User=`/paths, `systemctl daemon-reload`,
`systemctl enable --now gradimize`. Install steps are in the template header.

## 5. Health checks

```bash
.venv/bin/python scripts/doctor.py            # after any data delivery
.venv/bin/python scripts/doctor.py --skip-solver-checks   # quick check
```

Exit codes: 0 = OK, 1 = warnings only, 2 = failures (fix before generating
schedules). Default output is redacted to counts and safe to paste into email.

## 5b. When something goes wrong (getting remote help)

This VM has no AI tooling on it — support works by copy-paste. One command
collects everything a remote helper (e.g. a Claude session on another
machine) needs:

```bash
./bugreport.sh
```

It writes a single markdown file (path is printed) containing versions, the
git commit, solver config, database row counts, redacted health checks, and
the tails of the application log and the newest run log. **Every section is
student-data-free by design** — counts, codes, and shapes only — so the whole
file is safe to paste into a chat.

Where problems get recorded:

- `logs/gradimize.log` — application log; unhandled crashes land here with
  full tracebacks (TUI included), and TUI operation failures are logged here
  even when the UI only shows a short status message.
- `logs/run-*.jsonl` — one structured, FERPA-safe log per import/solve/doctor
  run.
- `scripts/run_report.py` — student-free content report for a generated
  schedule, for "why does the schedule look like this" questions.

For a schedule-content question, share the run report; for anything else,
share the bug report. If the app won't start at all, run
`./bugreport.sh --no-doctor` (skips the database checks).

## 6. Data governance

- The repository ships **only synthetic sample data** (`data/sample/`) and
  tiny hand-written format examples. Real institutional data is delivered
  separately, lives under `GRADIMIZE_DATA_DIR` **outside the repo tree**, and
  is never committed (`.gitignore` enforces this).
- **Before real data is loaded on this VM:** if IT administers the host, IT
  administrators gain access to the pseudonymized data. The access table in
  `docs/DATA_HANDLING_PROCEDURES.md` must be updated to list them first —
  they become a listed party.
- Once real data is present, debugging works from the redacted run logs
  (`logs/run-*.jsonl`) **only** — never from the database or CSVs directly.

## 7. Ports & firewall

| Port | Use | Exposure |
|---|---|---|
| 8443/tcp | HTTPS web UI + API (`serve.sh --secure`, systemd unit) | LAN only |
| 8000, 5173 | Dev mode only (`serve.sh` without `--secure`) | loopback only |
