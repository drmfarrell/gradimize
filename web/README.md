# Gradimize Web

React + Vite single-page app for Gradimize. It talks to the FastAPI
backend (`src/api`) over HTTP.

## Develop

Terminal 1 — backend (from repo root):

```bash
.venv/bin/python -m uvicorn src.api.main:app --reload --port 8000
```

Terminal 2 — frontend (from `web/`):

```bash
npm install   # first time only
npm run dev
```

Open http://localhost:5173. Vite proxies `/api/*` to the backend on
:8000 (stripping the `/api` prefix), so no CORS config is needed in dev.

## Build

```bash
npm run build      # typecheck + production bundle into dist/
npm run preview    # serve the built bundle
```

In production the SPA is served as static files; point it at a real API
origin by setting `VITE_API_BASE` at build time (defaults to `/api`).

## Screens

- **Dashboard** — system stats and recent schedules.
- **Schedules** — section table for a chosen schedule, with Banner/
  enrollments CSV export.
- **Finals** — solve and view finals exam schedules per semester, with
  CSV export.
