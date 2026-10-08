"""Schedule routes, split by concern into submodules.

The former single 900-line ``schedule.py`` is now a package. Each submodule
owns one concern and its own ``APIRouter``; this ``__init__`` combines them
into the ``router`` that ``main.py`` mounts, and re-exports ``get_session`` so
``from src.api.routes.schedule import router, get_session`` keeps working.
"""

from fastapi import APIRouter

from src.database.connection import get_session  # noqa: F401 — re-exported for tests

from . import analysis, crud, exports, pins, solve

router = APIRouter()
for _m in (crud, solve, pins, analysis, exports):
    router.include_router(_m.router)
