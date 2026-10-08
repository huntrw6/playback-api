"""Turn Playback's raw `pr-protocol` messages into normalized events.

fast_transport (v2.1, off by default)
-------------------------------------
Playback relays a play/pause/return/select command to every listener within tens of milliseconds, but the
state only shows up in the once-per-second heartbeat (median 0.5 s later, up to 1 s). With fast_transport=True
the command itself produces song.started / song.resumed / song.paused / song.stopped at once, marked
`provisional: true`. The heartbeat that follows confirms it silently (no duplicate). If the heartbeat has not
shown the expected state after ~3 s a `transport.reverted` event reports the real state. Commands that would
change nothing (play while already playing) are ignored.

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
section.navigate the operator stepped to a section-map element (index); position lands at the next boundary
setlist.state    heartbeat setlistState changed (ready | downloading | unsaved | midi-cues-unsaved)
setlist.loaded   a setlist was loaded on the Playback computer (setlistId, isDemo)
setlist.updated  setlist content metadata was refreshed (counts only)
song.cleared     the heartbeat lost its song (setlist empty or loading); songId is None until one is selected
song.transition.requested  setlistSelectSongTransition seen (songIndex, raw transition code; meaning unconfirmed)
navigate.requested  Next/Previous song pressed (direction); the heartbeat then shows the new song
transport.reverted  fast_transport only: a provisional start/pause/stop was not confirmed by the heartbeat (playing = actual state)
loop.infinite    the infinite-loop toggle changed (active)
loop.single      the single-Loop button was armed/disarmed (active); disarms itself after one wrap (reason: wrapped)
midi.mute        the MUTE MIDI toggle changed (active)
mixer.mute       track or bus mute changed (scope, number, on)
mixer.solo       track solo changed (scope, number, on)
mixer.volume     track or bus fader moved (only with volume_events=True; the latest levels are always in Normalizer.mixer)
audio.device.changed  Playback saw an audio device change (Playback can stall for minutes around these)
pad.requested    pad button command seen
pad.on/off       pad state actually changed (follows the command by ~5-7 s)
message.unknown  a message type this version does not know
"""
from __future__ import annotations

import bisect
import re
from typing import Any

START_WINDOW = 2.0  # seconds: a play at position < this counts as "from the start"
SELECT_WINDOW = 5.0  # seconds: heartbeat song change within this of a select msg is that select
JUMP_TOLERANCE = 1.5  # seconds of unexplained position drift that counts as a jump


def _first_key(msg: dict) -> str:
    return next(iter(msg))


_MAPPING = re.compile(r"^Track(?P<kind>Volume|Mute|Solo)_(?P<scope>SongTrack|Bus)_(?P<n>\d+)$")


def _parse_mapping(mapping_id: str) -> dict:
    """'TrackMute_Bus_18' -> {scope: 'bus', number: 18}. Unrecognised ids keep the raw string."""
    m = _MAPPING.match(mapping_id)
    if not m:
        return {"scope": None, "number": None, "mappingId": mapping_id}
    return {"scope": "track" if m["scope"] == "SongTrack" else "bus", "number": int(m["n"])}


def _setlist_state(raw: Any) -> tuple[str, str | None]:
    """Heartbeat setlistState -> (name, detail). Seen: ready, downloadingContent,
    changed{_0:{setlistUnsaved|midiCuesNotSaved}}."""
    if not isinstance(raw, dict) or not raw:
        return "unknown", None
    name = next(iter(raw))
    if name == "changed":
        inner = raw[name].get("_0") if isinstance(raw[name], dict) else None
        detail = next(iter(inner)) if isinstance(inner, dict) and inner else None
        return ("unsaved" if detail == "setlistUnsaved" else "midi-cues-unsaved" if detail == "midiCuesNotSaved" else "changed"), detail
    return ("downloading" if name == "downloadingContent" else name), None


class Normalizer:
    def __init__(self, sections: dict[int, list[tuple[float, int]]] | None = None, *,
                 volume_events: bool = False, fast_transport: bool = False):
        # sections: songId -> sorted [(startTime, sectionId)]
        # volume_events: emit mixer.volume for every fader message (hundreds per minute while a
        # fader moves). Off by default; the latest levels are always kept in self.mixer.
        self.volume_events = volume_events
        self.fast_transport = fast_transport
        self._expected: bool | None = None  # playing state a provisional event promised (fast_transport)
        self._prov: list[str] = []          # provisional event types waiting for their heartbeat
        self._prov_ts = 0.0
        self._prov_hbs = 0
        self._armed_before = True
        self._armed_before_next = True
        self.mixer: dict[str, float] = {}  # "track:12" / "bus:3" -> level
        self.mixer_mute: dict[str, bool] = {}
        self.mixer_solo: dict[str, bool] = {}
        self.infinite_loop: bool | None = None  # None = unknown until the toggle is seen
        self.setlist_state: str | None = None
        self.sections = {k: sorted(v) for k, v in (sections or {}).items()}
        self.hb: dict | None = None
        self.fade: bool | None = None  # None = unknown, True = faded out
        self.single_loop: bool | None = None  # None = unknown until the toggle is seen
        self.midi_muted: bool | None = None
        self.setlist_id: int | None = None  # only known if the load happened while we were listening
        self.setlist_name: str | None = None
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
            n0 = len(ev)
            self._heartbeat(body["stateData"], ts, emit)
            if self._prov:
                mine = ev[n0:]
                del ev[n0:]
                ev.extend(self._settle_provisional(self.hb, ts, mine, emit))
        elif kind == "setlistSelectSong":
            sid = body["setlistSongID"]
            self._sel = (sid, ts)
            self._armed = True
            emit("song.selected", songId=sid, reason="select")
            # Selecting another song while one plays makes Playback stop the transport (measured live).
            if self.fast_transport and self.hb and sid != self.hb["song"] and self._playing_now():
                self._provisional(emit, ts, "song.stopped", False, songId=self.hb["song"],
                                  position=self.hb["t"], reason="song-selected")
        elif kind == "transportReturnToStart":
            self._ret_ts = ts
            self._armed = True
            if self.fast_transport and self.hb and (self._playing_now() or self.hb["t"] > 0):
                self._provisional(emit, ts, "song.stopped", False, songId=self.hb["song"],
                                  position=0.0, reason="return-to-start")
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
            # The heartbeat edge carries the authoritative position, so by default we classify there.
            want = bool(body.get("playing"))
            self._armed_before_next = self._armed
            if self.fast_transport and self.hb and want != self._playing_now() and self.hb["song"] is not None:
                if want:
                    start = self._armed and self.hb["t"] < START_WINDOW
                    self._armed = False
                    self._provisional(emit, ts, "song.started" if start else "song.resumed", True,
                                      songId=self.hb["song"], position=self.hb["t"], reason="play")
                else:
                    self._provisional(emit, ts, "song.paused", False, songId=self.hb["song"], position=self.hb["t"])
        elif kind in ("transportNextSong", "transportPreviousSong"):
            emit("navigate.requested", direction="next" if kind == "transportNextSong" else "previous")
        elif kind == "transportNavigateToSongMapElementIndex":
            # Operator stepped through section-map elements (bursts of index 0..N). The position
            # then moves by itself, so suppress position.jump and let the start window decide
            # whether the next play is a start or a resume.
            self._cmd_ts = ts
            self._armed = True
            emit("section.navigate", songId=self.song, index=body.get("index"))
        elif kind == "mixerInfiniteLoop":
            self.infinite_loop = bool(body.get("active"))
            emit("loop.infinite", active=self.infinite_loop)
        elif kind == "mixerLoop":
            self.single_loop = bool(body.get("active"))
            emit("loop.single", active=self.single_loop, songId=self.song)
        elif kind == "mixerMuteMIDI":
            self.midi_muted = bool(body.get("active"))
            emit("midi.mute", active=self.midi_muted)
        elif kind == "setlistSelectSongTransition":
            emit("song.transition.requested", songIndex=body.get("songIndex"), transition=body.get("transition"))
        elif kind == "contentLoadSetlist":
            data = body.get("setlistData") or {}
            # The name can identify a private service plan, so it is kept in state only (and shown only by
            # state / the HTTP /state route), never put in the event stream that callers may log or forward.
            self.setlist_id, self.setlist_name = data.get("setlistID"), data.get("setlistName")
            self.single_loop = None  # a new setlist resets transient toggles we cannot read back
            emit("setlist.loaded", setlistId=self.setlist_id, isDemo=data.get("isDemo"))
        elif kind == "contentUpdateSetlist":
            emit("setlist.updated", rentals=len(body.get("rentalData") or []),
                 modularClick=len(body.get("modularClickSongData") or []))
        elif kind == "audioDeviceChanged":
            emit("audio.device.changed")
        elif kind in ("mixerTrackMute", "mixerTrackSolo", "mixerTrackVolume"):
            self._mixer(kind, body, emit)
        else:
            emit("message.unknown", kind=kind, body=body)
        return ev

    def _playing_now(self) -> bool:
        """Best known transport state: what a provisional event promised, else the last heartbeat."""
        if self._expected is not None:
            return self._expected
        return bool(self.hb and self.hb["playing"])

    def _provisional(self, emit, ts: float, etype: str, expected: bool, **kw: Any) -> None:
        if not self._prov:
            self._prov_ts, self._prov_hbs = ts, 0
            self._armed_before = self._armed_before_next
        self._prov.append(etype)
        self._expected = expected
        emit(etype, provisional=True, **kw)

    def _settle_provisional(self, cur: dict, ts: float, events: list[dict], emit) -> list[dict]:
        """Called with the events a heartbeat produced. Drops those the provisional events already announced."""
        if not self._prov:
            return events
        if cur["playing"] == self._expected:
            same = {"song.started": "play", "song.resumed": "play"}
            pending = [same.get(t, t) for t in self._prov]  # a start and a resume are one promise: "playing"
            kept = []
            for e in events:
                k = same.get(e["type"], e["type"])
                if k in pending:
                    pending.remove(k)  # already announced, one for one
                else:
                    kept.append(e)
            self._prov, self._expected = [], None
            return kept
        self._prov_hbs += 1
        if self._prov_hbs >= 2 and ts - self._prov_ts >= 3.0:
            self._prov, self._expected = [], None
            self._armed = self._armed_before  # a later real start must still count as a start
            emit("transport.reverted", playing=cur["playing"], songId=cur["song"], position=cur["t"])
        return events

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

    def _section_at(self, song: int | None, t: float) -> int | None:
        rows = self.sections.get(song) if song is not None else None
        if not rows:
            return None
        i = bisect.bisect_right([r[0] for r in rows], t) - 1
        return rows[i][1] if i >= 0 else None

    def _mixer(self, kind: str, body: dict, emit) -> None:
        for mid in body.get("mappingIDs") or []:
            who = _parse_mapping(mid)
            key = "%s:%s" % (who["scope"], who["number"]) if who["scope"] else mid
            if kind == "mixerTrackVolume":
                self.mixer[key] = body.get("level")
                if self.volume_events:
                    emit("mixer.volume", level=body.get("level"), **who)
            elif kind == "mixerTrackMute":
                on = "muted" in (body.get("muteState") or {})
                self.mixer_mute[key] = on
                emit("mixer.mute", on=on, **who)
            else:
                on = "soloed" in (body.get("soloState") or {})
                self.mixer_solo[key] = on
                emit("mixer.solo", on=on, **who)

    def _heartbeat(self, sd: dict, ts: float, emit) -> None:
        sl_state, sl_detail = _setlist_state(sd.get("setlistState"))
        if sl_state != self.setlist_state:
            if self.setlist_state is not None:  # the snapshot already carries the first value
                emit("setlist.state", state=sl_state, detail=sl_detail, previous=self.setlist_state)
            self.setlist_state = sl_state
        cur = {
            "song": sd.get("setlistSongID"),  # absent while a setlist is empty/loading
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
                 pad=cur["pad"], setlistVersion=cur["ver"], fade=None, setlistState=sl_state)
            return

        if cur["ver"] != prev["ver"]:
            emit("setlist.changed", version=cur["ver"], previous=prev["ver"])

        changed = cur["song"] != prev["song"]
        by_select = False
        if changed and cur["song"] is None:
            # Setlist empty/loading: the heartbeat drops the song id. Not an auto-advance; do not
            # classify it as an ended song, and do not arm a start.
            emit("song.cleared", previousSongId=prev["song"])
            self._sel, self._section = None, None
            if cur["pad"] != prev["pad"]:
                emit("pad.on" if cur["pad"] else "pad.off", songId=None)
            return
        if changed and prev["song"] is None:
            # A song appeared (setlist finished loading). That is a selection, never a start/end.
            self._sel = None
            emit("song.selected", songId=cur["song"], reason="loaded")
            self._armed = cur["t"] < START_WINDOW
            self._section = None
            changed = False
            prev = dict(prev, song=cur["song"], playing=False, t=cur["t"])
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

        if changed and by_select and prev["playing"] and not cur["playing"]:
            # Selecting another song while one plays stops the transport (measured on a live Playback): the
            # OLD song stopped. Without the select message this would be a natural end, handled above.
            emit("song.stopped", songId=prev["song"], reason="song-selected", position=prev["t"])
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
                # A backward jump with a loop armed is that loop wrapping; no section map is needed to say so.
                looping = drift < 0 and bool(self.infinite_loop or self.single_loop)
                likely = "loop" if (drift < 0 and at_start) or looping else "queued-section-jump"
                emit("position.jump", songId=cur["song"], fromPosition=prev["t"], toPosition=cur["t"],
                     direction="back" if drift < 0 else "forward", sectionId=landing if at_start else None,
                     likely=likely)
                if drift < 0 and self.single_loop and not self.infinite_loop:
                    # Documented: the Loop button repeats the section once, then switches itself off.
                    # Playback sends no message for that, so the disarm is inferred from the wrap.
                    self.single_loop = False
                    emit("loop.single", active=False, reason="wrapped", songId=cur["song"])

        sec = self._section_at(cur["song"], cur["t"])
        if sec != self._section:
            if cur["playing"] and sec is not None:
                emit("section.entered", songId=cur["song"], sectionId=sec, position=cur["t"])
            self._section = sec
