"""FastAPI application for Gradimize.

Serving modes:
- Dev: uvicorn on :8000 + Vite dev server on :5173 proxying /api.
- Hardened (serve.sh --secure): single uvicorn process over HTTPS serving
  both the API (under /api) and the built web bundle (web/dist), with an
  access token required when GRADIMIZE_TOKEN is set.
"""

import logging
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from src.api.routes import (
    conflicts,
    data,
    finals,
    projections,
    recommend,
    schedule,
    system,
)
from src.database.connection import create_tables, get_engine

logger = logging.getLogger(__name__)

WEB_DIST = Path(__file__).resolve().parents[2] / "web" / "dist"

# Shared access token. Empty/unset -> auth disabled (local dev, tests).
ACCESS_TOKEN = os.environ.get("GRADIMIZE_TOKEN", "").strip()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    from src.core.logsetup import init_logging
    logfile = init_logging(app="api")
    logger.info("Starting Gradimize API... (file log: %s)", logfile)
    engine = get_engine()
    create_tables(engine)
    logger.info("Database tables initialized")
    if ACCESS_TOKEN:
        logger.info("Access-token auth is ENABLED")
    else:
        logger.warning("Access-token auth is DISABLED (set GRADIMIZE_TOKEN to enable)")
    yield
    logger.info("Shutting down Gradimize API...")


app = FastAPI(
    title="Gradimize API",
    description="Academic scheduling engine using constraint programming",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS: browser traffic reaches this API through the Vite proxy in dev or
# same-origin static serving in hardened mode; this is only for local tooling.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def require_access_token(request, call_next):
    """Shared-token gate for every API route when GRADIMIZE_TOKEN is set.

    The token is accepted as `Authorization: Bearer <token>` or, for
    browser flows that can't set headers (EventSource streams, <a href>
    CSV downloads), as a `?token=` query parameter. Static assets and
    /api/health stay open so the login screen itself can load.
    """
    path = request.url.path
    needs_auth = (
        ACCESS_TOKEN
        and path.startswith("/api")
        and path not in ("/api/health", "/api")
        and request.method != "OPTIONS"
    )
    if needs_auth:
        auth = request.headers.get("authorization", "")
        presented = auth[7:] if auth.lower().startswith("bearer ") else (
            request.query_params.get("token", "")
        )
        if not secrets.compare_digest(presented, ACCESS_TOKEN):
            return JSONResponse(
                status_code=401,
                content={"detail": "Missing or invalid access token"},
            )
    return await call_next(request)


# Registered AFTER the token gate on purpose: the last-added middleware is
# outermost in Starlette, so the headers get stamped on every response —
# including the 401s the gate short-circuits without calling further in.
@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


# All API routes live under /api: the Vite dev proxy forwards /api verbatim,
# and hardened mode serves the SPA and API from one origin with no path
# collisions (e.g. the SPA route /finals/3 vs the API's /finals/{id}).
for router in (
    schedule.router,
    data.router,
    conflicts.router,
    projections.router,
    recommend.router,
    finals.router,
    system.router,
):
    app.include_router(router, prefix="/api")


@app.get("/api")
async def api_root():
    """API root endpoint."""
    return {
        "name": "Gradimize API",
        "version": "0.1.0",
        "description": "Academic scheduling engine",
        "auth_required": bool(ACCESS_TOKEN),
    }


@app.get("/api/health")
async def health_check():
    """Health check endpoint (always unauthenticated)."""
    return {"status": "healthy"}


class SPAStaticFiles(StaticFiles):
    """Static files with SPA fallback: unknown paths serve index.html so
    client-side routes like /schedules/3 survive a page refresh."""

    async def get_response(self, path: str, scope) -> Response:
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as e:
            if e.status_code == 404:
                return await super().get_response("index.html", scope)
            raise


if WEB_DIST.is_dir():
    app.mount("/", SPAStaticFiles(directory=WEB_DIST, html=True), name="web")
    logger.info("Serving web UI from %s", WEB_DIST)


def run_server(
    host: str = "127.0.0.1",
    port: int = 8000,
    reload: bool = False,
):
    """Run the API server (dev mode)."""
    uvicorn.run(
        "src.api.main:app",
        host=host,
        port=port,
        reload=reload,
    )


if __name__ == "__main__":
    run_server(reload=True)
