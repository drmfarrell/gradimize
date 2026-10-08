"""Persistence layer: SQLModel table definitions and the SQLite connection.

    models      every table (courses, rooms, faculty, students, demand,
                schedules, sections, meetings, finals) + the shared enums,
                grouped by section-comment dividers in one navigable file
    connection  DB path policy, the per-path engine cache, SQLite pragmas
                (WAL, busy_timeout), and the ``get_session`` FastAPI dependency
    queries     a few read helpers over the models

Kept intentionally thin: no business logic lives here. Anything that reasons
about the domain (grade thresholds, urgency, scheduling) belongs in
``src/core``.
"""
