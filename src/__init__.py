"""Gradimize — automated academic course scheduling (OR-Tools CP-SAT).

Four packages, each with its own ``__init__`` overview:

    core        the domain engine — ingest, demand, solve, analyze, export.
                START HERE; it's where all the scheduling logic lives.
    database    SQLModel tables + the SQLite connection. No business logic.
    api         FastAPI backend for the React web UI (``web/``).
    tui         Textual terminal UI (calls core directly, no HTTP).

Both front ends (api, tui) are deliberately thin over ``core``.
"""
