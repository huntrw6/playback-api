"""Turn Playback's raw `pr-protocol` messages into normalized events.

Pure logic, no I/O: feed it parsed JSON messages with a timestamp and it returns
a list of event dicts. That makes it replayable against captured logs.

Event types
-----------
state.snapshot   first heartbeat after connecting (full state)
setlist.changed  setlistCloudVersion changed (re-walk the setlist)
song.selected    a song became the selected one (reason: select|navigation)
song.started     playback began from the top of a song (reason: play|auto-advance)
song.resumed     playback began from a non-start position
song.paused      transport stopped mid-song
song.stopped     transport stopped and position reset (reason: return-to-start|ended)
song.ended       playing song reached its end (followed by song.changed)
song.changed     automatic transition to another song (continuesPlaying tells the mode)
seek             position jumped by a seek command
section.jump     a section was jumped to (sectionId; startTime if the section map is known)
section.entered  playback entered a section (needs a section map)
section.loop     section loop toggled
position.jump    position moved while playing with no command (queued section jump, or loop wrap)
fade.out/in      fade command seen (state is NOT in the heartbeat; unknown after reconnect)
pad.requested    pad button command seen
pad.on/off       pad state actually changed (follows the command by ~5-7 s)
message.unknown  a message type this version does not know
"""
from __future__ import annotations

import bisect
from typing import Any

START_WINDOW = 2.0  # seconds: a play at position < this counts as "from the start"
SELECT_WINDOW = 5.0  # seconds: heartbeat song change within this of a select msg is that select
JUMP_TOLERANCE = 1.5  # seconds of unexplained position drift that counts as a jump


def _first_key(msg: dict) -> str:
    return next(iter(msg))


class Normalizer:
    def __init__(self, sections: dict[int, list[tuple[float, int]]] | None = None):
        # sections: songId -> sorted [(startTime, sectionId)]
        self.sections = {k: sorted(v) for k, v in (sections or {}).items()}
        self.hb: dict | None = None
        self.fade: bool | None = None  # None = unknown, True = faded out
        self._armed = True  # next play counts as a "start"
        self._sel: tuple[int, float] | None = None
        self._ret_ts = -1e9
        self._cmd_ts = -1e9  # last seek / section-jump message (suppresses position.jump)
        self._hb_ts: float | None = None
        self._section: int | None = None

    # ---- public -------------------------------------------------------
    def feed(self, msg: dict, ts: float) -> list[dict]:
        kind = _first_key(msg)
        body = msg[kind]
        ev: list[dict] = []

        def emit(t: str, **kw: Any) -> None:
            ev.append({"type": t, "ts": ts, **kw})

        if kind == "heartbeat":
            self._heartbeat(body["stateData"], ts, emit)
        elif kind == "setlistSelectSong":
            sid = body["setlistSongID"]
            self._sel = (sid, ts)
            self._armed = True
            emit("song.selected", songId=sid, reason="select")
        elif kind == "transportReturnToStart":
            self._ret_ts = ts
            self._armed = True
        elif kind == "waveformSeek":
            self._cmd_ts = ts
            t = body["sequenceTime"]
            self._armed = t < START_WINDOW
            emit("seek", to=t, songId=self.song)
        elif kind == "waveformDoubleTap":
            self._cmd_ts = ts
            sec = body["setlistSongSectionID"]
            start = self._section_start(sec)
            self._armed = start is not None and start < START_WINDOW
            emit("section.jump", songId=self.song, sectionId=sec, startTime=start)
        elif kind == "transportFade":
            out = body.get("direction") == 1
            self.fade = out
            emit("fade.out" if out else "fade.in", songId=self.song)
        elif kind == "transportPad":
            emit("pad.requested", on=bool(body.get("playing")), songId=self.song)
        elif kind == "waveformLoop":
            emit("section.loop", songId=self.song, sectionId=body.get("setlistSongSectionID"), active=body.get("active"))
        elif kind == "transportPlay":
            pass  # the heartbeat edge carries the position, so we classify there
        else:
            emit("message.unknown", kind=kind, body=body)
        return ev

    @property
    def song(self) -> int | None:
        return self.hb["song"] if self.hb else None

    # ---- internals ----------------------------------------------------
    def _section_start(self, sec: int) -> float | None:
        for rows in self.sections.values():
            for start, sid in rows:
                if sid == sec:
                    return start
        return None

    def _section_at(self, song: int, t: float) -> int | None:
        rows = self.sections.get(song)
        if not rows:
            return None
        i = bisect.bisect_right([r[0] for r in rows], t) - 1
        return rows[i][1] if i >= 0 else None

    def _heartbeat(self, sd: dict, ts: float, emit) -> None:
        cur = {
            "song": sd["setlistSongID"],
            "t": sd["sequenceTime"],
            "playing": "playing" in sd["sequencerPlayState"],
            "pad": "playing" in sd["padPlayerPlayState"],
            "ver": sd.get("setlistCloudVersion"),
        }
        prev, self.hb = self.hb, cur
        prev_ts, self._hb_ts = self._hb_ts, ts
        if prev is None:
            self._armed = cur["t"] < START_WINDOW
            self._section = self._section_at(cur["song"], cur["t"])
            emit("state.snapshot", songId=cur["song"], position=cur["t"], playing=cur["playing"],
                 pad=cur["pad"], setlistVersion=cur["ver"], fade=None)
            return

        if cur["ver"] != prev["ver"]:
            emit("setlist.changed", version=cur["ver"], previous=prev["ver"])

        changed = cur["song"] != prev["song"]
        if changed:
            by_select = self._sel and self._sel[0] == cur["song"] and ts - self._sel[1] < SELECT_WINDOW
            self._sel = None
            if by_select:
                pass  # song.selected already emitted from the message
            elif prev["playing"]:
                emit("song.ended", songId=prev["song"], endedAt=prev["t"])
                emit("song.changed", fromSongId=prev["song"], toSongId=cur["song"],
                     reason="auto-advance", continuesPlaying=cur["playing"])
            else:
                emit("song.selected", songId=cur["song"], reason="navigation")
            self._armed = True
            self._section = None

        if cur["playing"] and (not prev["playing"] or changed):
            start = (changed and prev["playing"]) or (self._armed and cur["t"] < START_WINDOW)
            emit("song.started" if start else "song.resumed", songId=cur["song"], position=cur["t"],
                 reason="auto-advance" if (changed and prev["playing"]) else "play")
            self._armed = False
        elif not cur["playing"] and prev["playing"] and not changed:
            if cur["t"] == 0 and ts - self._ret_ts < 3:
                emit("song.stopped", songId=cur["song"], reason="return-to-start")
            else:
                emit("song.paused", songId=cur["song"], position=cur["t"])
        elif not cur["playing"] and not prev["playing"] and cur["t"] == 0 and prev["t"] > 0 and not changed:
            emit("song.stopped", songId=cur["song"], reason="return-to-start")

        if cur["pad"] != prev["pad"]:
            emit("pad.on" if cur["pad"] else "pad.off", songId=cur["song"])

        # Position moved by something other than normal playback while playing:
        # a queued section jump (applies at the next boundary) or a loop wrap.
        if cur["playing"] and prev["playing"] and not changed and prev_ts is not None:
            drift = cur["t"] - (prev["t"] + (ts - prev_ts))
            if abs(drift) > JUMP_TOLERANCE and ts - self._cmd_ts > 2.0:
                landing = self._section_at(cur["song"], cur["t"])
                at_start = landing is not None and abs(cur["t"] - self._section_start(landing)) < 1.5
                emit("position.jump", songId=cur["song"], fromPosition=prev["t"], toPosition=cur["t"],
                     direction="back" if drift < 0 else "forward", sectionId=landing if at_start else None,
                     likely="loop" if drift < 0 and at_start else "queued-section-jump")

        sec = self._section_at(cur["song"], cur["t"])
        if sec != self._section:
            if cur["playing"] and sec is not None:
                emit("section.entered", songId=cur["song"], sectionId=sec, position=cur["t"])
            self._section = sec
