"""Job-lock 409 contract: endpoints must reject requests while
src/core/job_lock.py holds the process-wide single-optimization slot.

The SSE generate/re-solve endpoints' 409 behavior is covered alongside their
streaming tests in test_api_schedule_stream.py; this file covers the
remaining real guarded endpoint (/data/import/{kind}) and documents a
CODE_REVIEW.md discrepancy discovered while writing these tests — see the
docstring on the "no_longer_exists" tests below.
"""

import io

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select

from src.api.routes.data import get_session as data_get_session
from src.api.routes.data import router as data_router
from src.api.routes.projections import get_session as projections_get_session
from src.api.routes.projections import router as projections_router
from src.core import job_lock
from src.database.models import Room

ROOMS_CSV = b"id,building,number,capacity,room_type\nR1,Hall,101,30,LECTURE\n"


@pytest.fixture(autouse=True)
def _job_lock_safety_net():
    job_lock.release()
    yield
    job_lock.release()


@pytest.fixture
def data_client(db_manager):
    app = FastAPI()
    app.include_router(data_router)

    def override_session():
        with db_manager.get_session() as session:
            yield session

    app.dependency_overrides[data_get_session] = override_session
    return TestClient(app)


def test_data_import_409_when_job_lock_held_and_writes_nothing(data_client, db_manager):
    assert job_lock.try_acquire("generate", "busy") is True
    try:
        resp = data_client.post(
            "/data/import/rooms",
            files={"file": ("rooms.csv", io.BytesIO(ROOMS_CSV), "text/csv")},
        )
        assert resp.status_code == 409
        assert "optimization is running" in resp.json()["detail"]
    finally:
        job_lock.release()

    with db_manager.get_session() as session:
        assert session.exec(select(Room)).all() == [], (
            "the lock check must short-circuit before any row is written"
        )


def test_data_import_holds_lock_for_the_whole_import(data_client, monkeypatch):
    """Regression: the import must HOLD the optimization slot while it runs,
    not merely pre-check it. A bare status() read left a window where a solve
    could start right after the check and race the wipe+reload; the fix
    acquires the slot and releases it in finally.
    """
    import src.api.routes.data as data_module

    seen = {}
    real_import = data_module.import_data_file

    def spy(session, kind, content, replace=True):
        seen["locked_during_import"] = job_lock.status()["running"]
        return real_import(session, kind, content, replace=replace)

    monkeypatch.setattr(data_module, "import_data_file", spy)

    resp = data_client.post(
        "/data/import/rooms",
        files={"file": ("rooms.csv", io.BytesIO(ROOMS_CSV), "text/csv")},
    )
    assert resp.status_code == 200
    assert seen["locked_during_import"] is True, (
        "the slot must be held during the import, not just pre-checked"
    )
    assert job_lock.status()["running"] is False, "slot released after import"


def test_data_import_succeeds_once_lock_is_free(data_client, db_manager):
    """Sanity companion to the 409 test above: the identical request succeeds
    once the lock is free, proving the 409 was about the lock and not some
    other request problem."""
    resp = data_client.post(
        "/data/import/rooms",
        files={"file": ("rooms.csv", io.BytesIO(ROOMS_CSV), "text/csv")},
    )
    assert resp.status_code == 200
    assert resp.json()["rows_loaded"] == 1


# ─────────────────────────────────────────────────────────────────
# Stale CODE_REVIEW.md claim (P0.3): "/data/import/{kind}",
# "/projections/generate-demand", and "DELETE /projections/generated-data"
# return 409 while an optimization holds job_lock.
#
# The two projections routes no longer exist: the P3 dead-code pass (commit
# 77fae71, "P3: delete verified-dead code") removed both
# `POST /projections/generate-demand` and `DELETE /projections/generated-data`
# ("nothing in web/src calls them; TUI doesn't use HTTP"), but the P0.3
# checklist text above was never revised to match, and
# `_reject_if_optimizing()` in src/api/routes/projections.py — the helper
# that used to guard them — is now unreferenced dead code. There is nothing
# left to acquire-lock-and-assert-409 against.
#
# The route-ordering comment directly above `delete_projection` ("the
# parametrized "/{projection_id}" route must come LAST... it would shadow
# [/generated-data]") is now stale too, for the same reason: there is no
# /generated-data route left to shadow. It still describes a real Starlette
# routing hazard for the surviving `/{projection_id}` route, just not the
# one named in the comment. That hazard is exactly what these two tests
# pin down instead of the 404 one might naively expect:
#   - POST /projections/generate-demand -> 405: no route's (path, method)
#     pair matches, but the path SHAPE matches DELETE /{projection_id}, so
#     Starlette reports 405 (method not allowed) rather than 404.
#   - DELETE /projections/generated-data -> 422: this one matches
#     /{projection_id} on both path shape and method, so FastAPI proceeds
#     to coerce "generated-data" to int and fails validation.
# ─────────────────────────────────────────────────────────────────


@pytest.fixture
def projections_client(db_manager):
    app = FastAPI()
    app.include_router(projections_router)

    def override_session():
        with db_manager.get_session() as session:
            yield session

    app.dependency_overrides[projections_get_session] = override_session
    return TestClient(app)


def test_projections_generate_demand_route_no_longer_exists(projections_client):
    resp = projections_client.post("/projections/generate-demand")
    assert resp.status_code == 405  # path shape matches DELETE /{projection_id}, wrong method


def test_projections_generated_data_delete_route_no_longer_exists(projections_client):
    resp = projections_client.delete("/projections/generated-data")
    assert resp.status_code == 422  # swallowed by /{projection_id}; "generated-data" isn't an int
