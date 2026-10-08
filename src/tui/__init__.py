"""Textual terminal UI. Talks to ``src/core`` directly — no HTTP, no API layer.

    app             the Textual App: mounts the screens and holds solver settings
    screens/        one screen per view — dashboard, schedule, finals,
                    data_browser, constraints, settings

Like the web routers, screens stay thin: they render and delegate solving,
validation, export, etc. to ``src/core``. Any logic shared with the web UI
belongs in core so the two front ends can't drift.
"""
