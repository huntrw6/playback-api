"""PlaybackClient: connection, reconnect, state, guarded commands, setlist walk."""
from __future__ import annotations

import json
import threading
import time
from collections import deque
from typing import Callable

from .normalizer import Normalizer
from .ws import WebSocket, WSClosed, WSHandshakeError


class ControlDisabled(Exception):
    """Control commands are off (read-only mode)."""


class Refused(Exception):
    """A safety rule blocked the command (e.g. song change while playing)."""


class UnknownSong(Refused):
    """A song reference (id or number) could not be resolved."""


NUMBER_LIMIT = 10_000  # integers below this are song/section *numbers*; real Playback IDs are ~10^7+


class PlaybackClient:
    def __init__(self, host: str, port: int = 8080, *, allow_control: bool = False,
                 sections: dict[int, list[tuple[float, int]]] | None = None,
                 data=None,
                 on_event: Callable[[dict], None] | None = None, history: int = 2000):
        self.host, self.port, self.allow_control = host, port, allow_control
        self.data = data  # optional SetlistData
        if data is not None and sections is None:
            sections = data.sections
        self.norm = Normalizer(sections)
        self.sections = sections or {}
        self.durations: dict[int, float] = dict(data.durations) if data else {}
        self.names: dict[int, str | None] = dict(data.names) if data else {}
        self._seed_order = list(data.order) if data and data.order else []
        self._seed_version = data.version if data else None
        self.on_event = on_event
        self.events: deque = deque(maxlen=history)
        self.seq = 0
        self.connected = False
        self.last_error: str | None = None
        self.setlist: list[int] = []
        self.setlist_version: int | None = None
        self._ws: WebSocket | None = None
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._walking = False

    # ---- lifecycle ------------------------------------------------------
    def start(self) -> "PlaybackClient":
        self._thread = threading.Thread(target=self._run, daemon=True, name="playback-api")
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._ws:
            self._ws.close()

    def _emit(self, ev: dict) -> None:
        with self._lock:
            self.seq += 1
            ev["seq"] = self.seq
            if self._walking:
                ev["walk"] = True  # produced by our own setlist walk, not an operator
            self.events.append(ev)
        if self.on_event:
            try:
                self.on_event(ev)
            except Exception:
                pass

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                ws = WebSocket(self.host, self.port)
            except (OSError, WSHandshakeError) as e:
                self.last_error = f"{type(e).__name__}: {e}"
                if self.connected:
                    self.connected = False
                    self._emit({"type": "connection.lost", "ts": time.time(), "error": self.last_error})
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 15.0)
                continue
            backoff = 1.0
            self._ws = ws
            self.norm = Normalizer(self.sections)  # fresh state: fade is unknown again
            self.connected, self.last_error = True, None
            self._emit({"type": "connection.up", "ts": time.time(), "host": self.host, "port": self.port})
            try:
                while not self._stop.is_set():
                    raw = ws.recv(timeout=5.0)
                    if raw is None:
                        raise WSClosed("no heartbeat for 5s")
                    try:
                        msg = json.loads(raw)
                    except ValueError:
                        continue
                    ts = time.time()
                    with self._lock:
                        evs = self.norm.feed(msg, ts)
                    for e in evs:
                        if e["type"] == "setlist.changed":
                            self.setlist = []  # order is stale until re-walked
                        elif e["type"] == "state.snapshot":
                            self._maybe_seed(e.get("setlistVersion"))
                        self._emit(e)
            except (OSError, WSClosed) as e:
                self.last_error = f"{type(e).__name__}: {e}"
            finally:
                ws.close()
                self._ws = None
                if self.connected:
                    self.connected = False
                    self._emit({"type": "connection.lost", "ts": time.time(), "error": self.last_error})

    # ---- state ------------------------------------------------------------
    def _maybe_seed(self, version) -> None:
        """Use the data file's song order only if Playback's setlist version still matches it."""
        if self._seed_order and not self.setlist and version is not None and version == self._seed_version:
            self.setlist, self.setlist_version = list(self._seed_order), version

    def song_number(self, song_id: int | None) -> int | None:
        return self.setlist.index(song_id) + 1 if song_id in self.setlist else None

    @property
    def state(self) -> dict:
        hb = self.norm.hb or {}
        song = hb.get("song")
        sec = self.norm._section_at(song, hb.get("t", 0)) if song is not None else None
        dur = self.durations.get(song)
        pos = hb.get("t")
        rows = self.sections.get(song, [])
        return {
            "connected": self.connected, "lastError": self.last_error,
            "songId": song, "songNumber": self.song_number(song), "songName": self.names.get(song),
            "songCount": len(self.setlist) or None,
            "position": pos, "playing": hb.get("playing"), "pad": hb.get("pad"),
            "duration": dur, "remaining": round(dur - pos, 3) if dur is not None and pos is not None else None,
            "sectionId": sec,
            "sectionNumber": next((i + 1 for i, r in enumerate(rows) if r[1] == sec), None),
            "fadedOut": self.norm.fade,  # None = unknown (fade is not in the heartbeat)
            "setlistVersion": hb.get("ver"), "setlist": self.setlist,
        }

    def wait_for(self, pred: Callable[[dict], bool], timeout: float = 3.0) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            if pred(self.state):
                return True
            time.sleep(0.05)
        return False

    # ---- commands -----------------------------------------------------------
    def _send(self, msg: dict) -> None:
        if not self.allow_control:
            raise ControlDisabled("control is disabled (start with allow_control=True / --allow-control)")
        if not self._ws or not self.connected:
            raise Refused("not connected")
        self._ws.send(json.dumps(msg, separators=(",", ":")))
        # Playback broadcasts commands to *other* clients but never echoes them back to the
        # sender, so feed our own command through the normalizer to keep events/fade state right.
        with self._lock:
            evs = self.norm.feed(msg, time.time())
        for e in evs:
            e["source"] = "api"
            self._emit(e)

    def _require_stopped(self) -> None:
        if self.state["playing"] is not False:
            raise Refused("refused: transport is playing (or state unknown); stop first")

    def play(self) -> None: self._send({"transportPlay": {"playing": True}})
    def pause(self) -> None: self._send({"transportPlay": {"playing": False}})
    def return_to_start(self) -> None: self._send({"transportReturnToStart": {}})
    def seek(self, seconds: float) -> None: self._send({"waveformSeek": {"sequenceTime": float(seconds)}})
    def jump_to_section(self, ref: int, song: int | str | None = None) -> None:
        """ref is a real section ID, or a 1-based section number within `song` (default: current song)."""
        self._send({"waveformDoubleTap": {"setlistSongSectionID": self.resolve_section(ref, song)}})

    def pad(self, on: bool) -> None: self._send({"transportPad": {"playing": bool(on)}})
    def fade(self, out: bool) -> None: self._send({"transportFade": {"direction": 1 if out else 0}})
    def loop_section(self, ref: int, active: bool, song: int | str | None = None) -> None:
        self._send({"waveformLoop": {"setlistSongSectionID": self.resolve_section(ref, song), "active": bool(active)}})

    # ---- song / section references ------------------------------------------
    def resolve_song(self, ref) -> int:
        """Accept a song ID (e.g. 91000002) or a 1-based song number (e.g. 3). Numbers need the setlist
        order: from the data file (if its version matches), a previous walk, or an automatic walk."""
        try:
            n = int(str(ref).strip())
        except ValueError:
            raise UnknownSong("not a song id or number: %r" % (ref,))
        if n >= NUMBER_LIMIT:
            if self.setlist and n not in self.setlist:
                raise UnknownSong("song id %d is not in the discovered setlist %s" % (n, self.setlist))
            return n
        if not self.setlist:
            if not self.allow_control:
                raise UnknownSong("setlist not discovered yet: run walk-setlist, or load a data file")
            self.walk_setlist()
        if not 1 <= n <= len(self.setlist):
            raise UnknownSong("song number %d out of range 1..%d" % (n, len(self.setlist)))
        return self.setlist[n - 1]

    def resolve_section(self, ref, song=None) -> int:
        n = int(ref)
        if n >= NUMBER_LIMIT:
            return n
        sid = self.resolve_song(song) if song is not None else self.state["songId"]
        rows = self.sections.get(sid)
        if not rows:
            raise UnknownSong("no section map for song %s; pass a real section id or run discover-sections" % sid)
        if not 1 <= n <= len(rows):
            raise UnknownSong("section number %d out of range 1..%d for song %s" % (n, len(rows), sid))
        return rows[n - 1][1]

    def select_song(self, ref) -> None:
        self._require_stopped()
        self._send({"setlistSelectSong": {"setlistSongID": self.resolve_song(ref)}})

    def next_song(self) -> None:
        self._require_stopped()
        self._send({"transportNextSong": {}})

    def previous_song(self) -> None:
        self._require_stopped()
        self._send({"transportPreviousSong": {}})

    # ---- setlist discovery ----------------------------------------------------
    def walk_setlist(self, settle: float = 3.0) -> list[int]:
        """Read the setlist order by stepping Previous to the start then Next to the end,
        then restoring the original song. Only runs while stopped. Takes ~2 s per song."""
        self._require_stopped()
        original = self.state["songId"]
        self._walking = True
        try:
            def step(fn) -> bool:
                before = self.state["songId"]
                fn()
                return self.wait_for(lambda s: s["songId"] != before, settle)
            guard = 0
            while step(self.previous_song) and (guard := guard + 1) < 200:
                pass
            order = [self.state["songId"]]
            while step(self.next_song) and len(order) < 200:
                order.append(self.state["songId"])
            if original is not None and original != self.state["songId"]:
                self.select_song(original)
                self.wait_for(lambda s: s["songId"] == original, settle)
        finally:
            self._walking = False
        self.setlist, self.setlist_version = order, self.state["setlistVersion"]
        self._emit({"type": "setlist.discovered", "ts": time.time(), "songs": order, "version": self.setlist_version})
        return order
