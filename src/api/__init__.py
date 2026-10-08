"""FastAPI backend for the React web UI (``web/``).

    main        the app: middleware (token gate, CORS), router wiring, SPA static mount
    schemas     shared request/response models (newer endpoints colocate theirs)
    sse         Server-Sent-Events transport for streaming solver progress
    routes/     one router module per area — schedule, data, finals,
                projections, conflicts, recommend, system

Routers stay thin: parse/validate the request, call into ``src/core``, shape
the response. Business logic belongs in core, not here. (The TUI does NOT go
through this layer — it calls core directly.)
"""
