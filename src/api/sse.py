"""Server-Sent Events helper for streaming long, blocking work.

`sse_response(run)` runs `run(emit)` in a worker thread and streams every
`emit(type, **fields)` call to the client as a `data:` JSON frame, with
heartbeat comments during quiet periods. The stream ends when `run`
returns (the caller is expected to emit a terminal "done"/"error" event
of its own before that).
"""

import asyncio
import json
import threading
from typing import Callable

from fastapi.responses import StreamingResponse

# emit(event_type, **fields)
Emit = Callable[..., None]


def sse_response(run: Callable[[Emit], None]) -> StreamingResponse:
    """Wrap a blocking emit-driven function as an SSE StreamingResponse.

    Must be called from within an async request handler (it captures the
    running event loop to marshal thread → async safely).
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    DONE = object()

    def emit(event_type: str, **fields):
        loop.call_soon_threadsafe(queue.put_nowait, {"type": event_type, **fields})

    def worker():
        try:
            run(emit)
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, DONE)

    threading.Thread(target=worker, daemon=True).start()

    async def event_stream():
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=15)
            except asyncio.TimeoutError:
                yield ": ping\n\n"
                continue
            if item is DONE:
                break
            yield f"data: {json.dumps(item)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
