"""HTTP + Server-Sent Events front end (stdlib only).

GET  /state                 current state (JSON)
GET  /events?since=N        recent events (JSON array, seq > N)
GET  /stream                Server-Sent Events, one normalized event per message
GET  /setlist               setlist order, numbers, durations, names and section maps
POST /command/<name>        control (only with --allow-control); JSON body for arguments

Songs and sections can be addressed by real ID or by 1-based number:
  {"song": 3}  or  {"song": 91000003}      {"section": 4, "song": 2}  or  {"section": 92000690}
"""
from __future__ import annotations

import json
import queue
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .client import ControlDisabled, PlaybackClient, Refused


def _song(b):
    return b["song"] if "song" in b else b["songId"]


def _section(b):
    return b["section"] if "section" in b else b["sectionId"]


COMMANDS = {
    "play": lambda c, b: c.play(), "pause": lambda c, b: c.pause(),
    "return-to-start": lambda c, b: c.return_to_start(),
    "seek": lambda c, b: c.seek(b["seconds"]),
    "section": lambda c, b: c.jump_to_section(_section(b), b.get("song")),
    "pad": lambda c, b: c.pad(b["on"]), "fade": lambda c, b: c.fade(b["out"]),
    "loop-infinite": lambda c, b: c.loop_infinite(b["active"]),
    "loop-once": lambda c, b: c.loop_once(b["active"]),
    "loop-section": lambda c, b: c.loop_section(_section(b), b["active"], b.get("song")),
    "select-song": lambda c, b: c.select_song(_song(b)),
    "next-song": lambda c, b: c.next_song(), "previous-song": lambda c, b: c.previous_song(),
    "walk-setlist": lambda c, b: c.walk_setlist(measure=b.get("measure")),
    "measure-song": lambda c, b: c.measure_song_length(bool(b.get("precise"))),
    "measure-setlist": lambda c, b: c.measure_setlist(bool(b.get("precise"))),
}


def make_server(client: PlaybackClient, bind: str = "127.0.0.1", port: int = 8787) -> ThreadingHTTPServer:
    subs: list = []
    prev_cb = client.on_event

    def fan_out(ev: dict) -> None:
        if prev_cb:
            prev_cb(ev)
        for q in list(subs):
            try:
                q.put_nowait(ev)
            except queue.Full:
                pass
    client.on_event = fan_out

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def _json(self, code: int, obj) -> None:
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path, _, qs = self.path.partition("?")
            if path == "/state":
                self._json(200, client.state)
            elif path == "/events":
                since = int(dict(p.split("=") for p in qs.split("&") if "=" in p).get("since", 0))
                self._json(200, [e for e in list(client.events) if e["seq"] > since])
            elif path == "/setlist":
                songs = []
                for i, sid in enumerate(client.setlist, 1):
                    rows = client.sections.get(sid, [])
                    songs.append({"number": i, "id": sid, "name": client.names.get(sid),
                                  "duration": client.durations.get(sid),
                                  "durationSource": client._duration_source(sid),
                                  "sections": [{"number": n, "id": r[1], "start": r[0]} for n, r in enumerate(rows, 1)]})
                self._json(200, {"version": client.setlist_version, "songs": songs})
            elif path == "/stream":
                q: queue.Queue = queue.Queue(maxsize=1000)
                subs.append(q)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                try:
                    self.wfile.write(b"event: state\ndata: " + json.dumps(client.state).encode() + b"\n\n")
                    self.wfile.flush()
                    while True:
                        try:
                            ev = q.get(timeout=15)
                            self.wfile.write(("event: %s\ndata: %s\n\n" % (ev["type"], json.dumps(ev))).encode())
                        except queue.Empty:
                            self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    subs.remove(q)
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):
            if not self.path.startswith("/command/"):
                return self._json(404, {"error": "not found"})
            name = self.path[len("/command/"):]
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
                if name not in COMMANDS:
                    return self._json(404, {"error": "unknown command", "commands": sorted(COMMANDS)})
                result = COMMANDS[name](client, body)
                self._json(200, {"ok": True, "result": result})
            except ControlDisabled as e:
                self._json(403, {"ok": False, "error": str(e)})
            except Refused as e:
                self._json(409, {"ok": False, "error": str(e)})
            except (KeyError, ValueError, TypeError) as e:
                self._json(400, {"ok": False, "error": "bad arguments: %s" % e})

    return ThreadingHTTPServer((bind, port), H)
