"""Passive, receive-only recorder: stores every frame Playback sends, verbatim, as JSON lines.

Sends nothing (no commands, no pings of its own) and reconnects by itself, so it can run through a whole
rehearsal. The output replays offline with `playback-api replay FILE`.
Record kinds: start | connect | frame (raw text) | disconnect | error | end.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

from .ws import WebSocket, WSClosed


def record(path: str, hours: float, host: str = "127.0.0.1", port: int = 8080, stop=None) -> int:
    """Blocking. Returns the number of frames written. `stop` is an optional threading.Event."""
    deadline = time.time() + hours * 3600
    frames = 0

    def now():
        t = time.time()
        return t, datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="milliseconds")

    with open(path, "a", buffering=1, encoding="utf-8") as f:
        def emit(kind, **kw):
            t, iso = now()
            f.write(json.dumps({"t": t, "iso": iso, "kind": kind, **kw}, separators=(",", ":")) + "\n")

        emit("start", host=host, port=port, hours=hours, pid=os.getpid())
        backoff = 1.0
        while time.time() < deadline and not (stop and stop.is_set()):
            ws = None
            try:
                ws = WebSocket(host, port, timeout=5.0)
                emit("connect")
                backoff = 1.0
                while time.time() < deadline and not (stop and stop.is_set()):
                    raw = ws.recv(timeout=5.0)
                    if raw is None:
                        continue  # idle, keep listening
                    frames += 1
                    emit("frame", raw=raw)
            except WSClosed:
                emit("disconnect", reason="closed by server")
            except Exception as e:  # noqa: BLE001 - keep recording through anything
                emit("error", error="%s: %s" % (type(e).__name__, e))
            finally:
                if ws is not None:
                    try:
                        ws.sock.close()  # close the socket only; never send a close frame
                    except Exception:
                        pass
            if time.time() < deadline and not (stop and stop.is_set()):
                time.sleep(backoff)
                backoff = min(backoff * 2, 10.0)
        emit("end", frames=frames)
    return frames
