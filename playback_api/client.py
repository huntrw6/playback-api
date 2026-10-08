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
                 on_event: Callable[[dict], None] | None = None, history: int = 2000,
                 volume_events: bool = False, fast_transport: bool = False):
        self.host, self.port, self.allow_control = host, port, allow_control
        self.data = data  # optional SetlistData
        if data is not None and sections is None:
            sections = data.sections
        self.volume_events, self.fast_transport = volume_events, fast_transport
        self.norm = Normalizer(sections, volume_events=volume_events, fast_transport=fast_transport)
        self._last_msg = 0.0  # wall time of the last frame of any kind
        self.sections = sections or {}
        self.durations: dict[int, float] = dict(data.durations) if data else {}
        self.measured: dict[int, dict] = {}  # songId -> result of measure_song_length()
        self.names: dict[int, str | None] = dict(data.names) if data else {}
        self._seed_order = list(data.order) if data and data.order else []
        self._seed_version = data.version if data else None
        self._seed_setlist_id = getattr(data, "setlist_id", None) if data else None
        self.on_event = on_event
        self.events: deque = deque(maxlen=history)
        self.seq = 0
        self.connected = False
        self.last_error: str | None = None
        self.setlist: list[int] = []
        self.setlist_version: int | None = None
        self.setlist_id: int | None = None  # learned from contentLoadSetlist; None until a load is seen
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
            self.norm = Normalizer(self.sections, volume_events=self.volume_events, fast_transport=self.fast_transport)  # fresh state: fade unknown again
            self.connected, self.last_error = True, None
            self.setlist_id = None  # unknown again: Playback may have been restarted on another setlist
            self._emit({"type": "connection.up", "ts": time.time(), "host": self.host, "port": self.port})
            try:
                while not self._stop.is_set():
                    raw = ws.recv(timeout=5.0)
                    if raw is None:
                        # Playback can stall for minutes with the socket still open (seen around audio
                        # device changes): no frames at all, no close. Treat silence as a lost connection.
                        raise WSClosed("no heartbeat for 5s")
                    self._last_msg = time.time()
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
                        elif e["type"] == "setlist.loaded":
                            # setlistCloudVersion is per setlist (11, 3, 1, 2, 4 were all seen), so a different
                            # setlist can reuse the same version number. A load of a setlist we have not
                            # already identified invalidates any song order we are holding.
                            if e.get("setlistId") != self.setlist_id:
                                self.setlist = []
                            self.setlist_id = e.get("setlistId")
                            self._maybe_seed(self.norm.hb.get("ver") if self.norm.hb else None)
                        elif e["type"] == "state.snapshot":
                            if self.setlist and e.get("setlistVersion") != self.setlist_version:
                                self.setlist = []  # version moved while we were disconnected
                            self._maybe_seed(e.get("setlistVersion"))
                        self._emit(e)
            except (OSError, WSClosed) as e:
                self.last_error = f"{type(e).__name__}: {e}"
            finally:
                ws.close()
                self._ws = None
                if self.connected:
                    self.connected = False
                    stalled = "no heartbeat" in (self.last_error or "")
                    self._emit({"type": "connection.lost", "ts": time.time(), "error": self.last_error,
                                "reason": "stalled" if stalled else "closed"})

    # ---- state ------------------------------------------------------------
    def _maybe_seed(self, version) -> None:
        """Use the data file's song order only if Playback's setlist version still matches it."""
        if not (self._seed_order and not self.setlist and version is not None and version == self._seed_version):
            return
        # The version number alone is not an identity (it is counted per setlist). If the data file names
        # its setlist and we already know which one is open, they must agree.
        if self._seed_setlist_id and self.setlist_id and self._seed_setlist_id != self.setlist_id:
            return
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
            "duration": dur, "durationSource": self._duration_source(song), "remaining": round(dur - pos, 3) if dur is not None and pos is not None else None,
            "sectionId": sec,
            "sectionNumber": next((i + 1 for i, r in enumerate(rows) if r[1] == sec), None),
            "fadedOut": self.norm.fade,  # None = unknown (fade is not in the heartbeat)
            "setlistVersion": hb.get("ver"), "setlist": self.setlist,
            "setlistState": self.norm.setlist_state,  # ready | downloading | unsaved | midi-cues-unsaved
            "infiniteLoop": self.norm.infinite_loop,  # None = unknown until the toggle is seen
            "fastTransport": self.fast_transport,
            "singleLoop": self.norm.single_loop,      # None = unknown; clears itself after one wrap
            "midiMuted": self.norm.midi_muted,        # MUTE MIDI toggle; None = unknown
            "setlistId": self.setlist_id, "setlistName": self.norm.setlist_name,
            "muted": sorted(k for k, v in self.norm.mixer_mute.items() if v),
            "soloed": sorted(k for k, v in self.norm.mixer_solo.items() if v),
            "heartbeatAge": round(time.time() - self._last_msg, 2) if self._last_msg else None,
        }

    def _duration_source(self, song) -> str | None:
        m = self.measured.get(song)
        if m is not None and self.durations.get(song) == m["duration"]:
            return "measured-precise" if m["precise"] else "measured-quick"
        return "file" if song in self.durations else None

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
    def loop_infinite(self, active: bool) -> None: self._send({"mixerInfiniteLoop": {"active": bool(active)}})
    def loop_once(self, active: bool) -> None:
        """Single Loop button: repeat the playing section once, then it switches itself off."""
        self._send({"mixerLoop": {"active": bool(active)}})

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

    # ---- song length ---------------------------------------------------------
    HUGE_SEEK = 86400.0

    def measure_song_length(self, precise: bool = False, settle: float = 3.0) -> dict:
        r = self._measure_core(precise, settle)
        self._store_length(r)
        return r

    def _measure_core(self, precise: bool, settle: float) -> dict:
        """Length of the SELECTED song. Only runs while stopped; restores the song, position and fade.

        Quick (about 6 s): seek far past the end. Playback clamps the seek to the end of the song and the
        heartbeat reports where it landed: the start of the song's final measure, i.e. where the song ends for a service
        (operator-confirmed). Use this as the length for countdowns and plans.
        Precise (about 17 s): also play the last seconds with the tracks faded out and take the last position
        the heartbeat reports while playing; the real end is within one heartbeat (1 s) after it, so the result
        is that position + 0.5 s (+- 0.5 s). Also reports what happens at the end: "stops" (the next song is
        selected, stopped) or "continues" (the next song starts playing by itself).

        Returns {songId, duration, lowerBound, precise, precision, endBehavior, method}."""
        self._require_stopped()
        if precise and (self.norm.infinite_loop or self.norm.single_loop):
            raise Refused("refused: a loop is armed; switch it off first")
        song = self.state["songId"]
        if song is None:
            raise Refused("refused: no song selected")
        fade_was = self.norm.fade
        was_walking, self._walking = self._walking, True
        try:
            if precise and fade_was is not True:
                self.fade(True)
            self.return_to_start()
            self.wait_for(lambda s: s["position"] is not None and s["position"] < 0.01, settle)
            self.seek(self.HUGE_SEEK)
            if not self.wait_for(lambda s: (s["position"] or 0) > 0.5, settle):
                raise Refused("could not read the song length (the seek had no effect)")
            time.sleep(settle * 0.4)  # let a second heartbeat confirm the clamp
            lower = self.state["position"]
            result = {"songId": song, "lowerBound": lower, "duration": lower, "precise": False,
                      "precision": 4.0, "endBehavior": None, "method": "seek-clamp"}
            if precise:
                self.seek(max(0.0, lower - 6.0))
                self.wait_for(lambda s: abs((s["position"] or 0) - max(0.0, lower - 6.0)) < 2.0, settle)
                last, ended, nxt = None, None, None
                self.play()
                deadline = time.time() + 20.0
                while time.time() < deadline:
                    hb = self.norm.hb
                    if hb and hb["song"] == song and hb["playing"]:
                        last = hb["t"]
                    elif hb and (hb["song"] != song or not hb["playing"]) and last is not None:
                        ended, nxt = time.time(), hb
                        break
                    time.sleep(0.05)
                if last is not None and nxt is not None:
                    result.update(duration=round(last + 0.5, 2), precise=True, precision=0.5, method="played-to-end",
                                  endBehavior="continues" if nxt["song"] != song and nxt["playing"] else "stops")
            return result
        finally:
            try:
                if self.state["playing"]:
                    self.pause()
                    self.wait_for(lambda s: s["playing"] is False, settle)
                if self.state["songId"] != song:
                    self.select_song(song)
                    self.wait_for(lambda s: s["songId"] == song, settle)
                self.return_to_start()
                self.wait_for(lambda s: s["position"] is not None and s["position"] < 0.01, settle)
                if precise and fade_was is not True:
                    self.fade(False)
            finally:
                self._walking = was_walking

    def _measure_here(self, precise: bool, settle: float) -> None:
        try:
            r = self._measure_core(precise, settle)
        except Refused as e:
            self._emit({"type": "song.length.failed", "ts": time.time(), "songId": self.state["songId"], "error": str(e)})
            return
        self._store_length(r)

    def _store_length(self, r: dict) -> None:
        sid, prev = r["songId"], self.measured.get(r["songId"])
        if prev is not None and prev["precise"] and not r["precise"]:
            r = prev  # a quick lower bound never replaces a precise value
        self.measured[sid] = r
        self.durations[sid] = r["duration"]
        self._emit({"type": "song.length", "ts": time.time(), **r})

    def measure_setlist(self, precise: bool = False, settle: float = 3.0) -> dict[int, dict]:
        """Walk the setlist (this also refreshes the song order) and measure every song."""
        self.walk_setlist(settle, measure="precise" if precise else "quick")
        return dict(self.measured)

    # ---- setlist discovery ----------------------------------------------------
    def walk_setlist(self, settle: float = 3.0, measure: str | None = None) -> list[int]:
        """Read the setlist order by stepping Previous to the start then Next to the end,
        then restoring the original song. Only runs while stopped. Takes ~2 s per song.

        measure="quick" or "precise" also records each song's length on the way (see measure_song_length);
        quick adds about 6 s per song, precise about 17 s per song (and plays the last seconds of each song
        with the tracks faded out)."""
        if measure not in (None, "quick", "precise"):
            raise ValueError("measure must be None, 'quick' or 'precise'")
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
            if measure:
                self._measure_here(measure == "precise", settle)
            while step(self.next_song) and len(order) < 200:
                order.append(self.state["songId"])
                if measure:
                    self._measure_here(measure == "precise", settle)
            if original is not None and original != self.state["songId"]:
                self.select_song(original)
                self.wait_for(lambda s: s["songId"] == original, settle)
        finally:
            self._walking = False
        self.setlist, self.setlist_version = order, self.state["setlistVersion"]
        self._emit({"type": "setlist.discovered", "ts": time.time(), "songs": order, "version": self.setlist_version})
        return order
