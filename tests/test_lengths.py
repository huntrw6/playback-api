"""v2.2: song length measurement against a simulated Playback (clamping seek, real-time transport)."""
import json
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from playback_api.client import PlaybackClient, Refused  # noqa: E402

IDS = [91000001, 91000002, 91000003]
REAL = {IDS[0]: 8.0, IDS[1]: 9.0, IDS[2]: 7.5}     # true lengths of the simulated songs (seconds)
CLAMP = {IDS[0]: 5.2, IDS[1]: 6.6, IDS[2]: 4.1}    # what a huge seek lands on (lower bound, as measured)


class SimPlayback:
    """Just enough of Playback: order, clamped seeks, real-time play, stop-or-continue at the end."""

    def __init__(self, client, continues=()):
        self.c, self.continues = client, set(continues)
        self.song, self.pos, self.playing, self.ver = IDS[0], 0.0, False, 4
        self.sent, self._stop, self.lock = [], False, threading.Lock()
        self.t = time.time()
        threading.Thread(target=self._run, daemon=True).start()

    def send(self, text):
        m = json.loads(text); k = next(iter(m)); b = m[k]; self.sent.append(m)
        with self.lock:
            if k == "transportPlay":
                self.playing = bool(b["playing"])
            elif k == "transportReturnToStart":
                self.pos, self.playing = 0.0, False
            elif k == "waveformSeek":
                t = b["sequenceTime"]
                self.pos = CLAMP[self.song] if t > REAL[self.song] else t
            elif k == "setlistSelectSong":
                self.song, self.pos, self.playing = b["setlistSongID"], 0.0, False
            elif k in ("transportNextSong", "transportPreviousSong"):
                i = IDS.index(self.song) + (1 if k == "transportNextSong" else -1)
                if 0 <= i < len(IDS):
                    self.song, self.pos = IDS[i], 0.0

    def close(self):
        self._stop = True

    def _run(self):
        while not self._stop:
            time.sleep(0.2)
            now = time.time()
            with self.lock:
                if self.playing:
                    self.pos += now - self.t
                    if self.pos >= REAL[self.song]:
                        i = IDS.index(self.song)
                        nxt = IDS[(i + 1) % len(IDS)]
                        self.song, self.pos = nxt, 0.0
                        self.playing = IDS[i] in self.continues  # carries on into the next song, or stops
                self.t = now
                hb = {"heartbeat": {"stateData": {"setlistSongID": self.song, "sequenceTime": self.pos,
                      "setlistCloudVersion": self.ver, "setlistState": {"ready": {}},
                      "sequencerPlayState": {"playing" if self.playing else "stopped": {}},
                      "padPlayerPlayState": {"stopped": {}}}}}
            self.c._last_msg = time.time()
            with self.c._lock:
                evs = self.c.norm.feed(hb, time.time())
            for e in evs:
                self.c._emit(e)


def make(continues=()):
    c = PlaybackClient("x", allow_control=True)
    sim = SimPlayback(c, continues)
    c._ws, c.connected = sim, True
    c.setlist, c.setlist_version = list(IDS), 4
    time.sleep(0.6)
    return c, sim


class Quick(unittest.TestCase):
    def test_quick_reads_the_clamp_and_restores(self):
        c, sim = make()
        try:
            r = c.measure_song_length(precise=False, settle=2.0)
            self.assertEqual(r["songId"], IDS[0])
            self.assertAlmostEqual(r["duration"], CLAMP[IDS[0]], 1)
            self.assertFalse(r["precise"])
            self.assertEqual(r["method"], "seek-clamp")
            self.assertEqual(c.state["songId"], IDS[0])
            self.assertEqual(c.state["position"], 0.0)
            self.assertIs(c.state["playing"], False)
            self.assertEqual(c.state["durationSource"], "measured-quick")
            self.assertNotIn({"transportFade": {"direction": 1}}, sim.sent)  # quick never touches the fade
        finally:
            sim.close()

    def test_refuses_while_playing(self):
        c, sim = make()
        try:
            c._ws.playing = True
            time.sleep(0.5)
            with self.assertRaises(Refused):
                c.measure_song_length()
        finally:
            sim.close()

    def test_refuses_precise_with_a_loop_armed(self):
        c, sim = make()
        try:
            c.norm.infinite_loop = True
            with self.assertRaises(Refused):
                c.measure_song_length(precise=True)
        finally:
            sim.close()


class Precise(unittest.TestCase):
    def test_precise_plays_the_last_seconds_and_reports_a_stop(self):
        c, sim = make()
        try:
            r = c.measure_song_length(precise=True, settle=2.0)
            self.assertTrue(r["precise"])
            self.assertLess(abs(r["duration"] - REAL[IDS[0]]), 1.1)
            self.assertEqual(r["endBehavior"], "stops")
            self.assertEqual(r["precision"], 0.5)
            self.assertEqual(c.state["songId"], IDS[0])        # restored
            self.assertIs(c.state["playing"], False)
            self.assertEqual(c.state["position"], 0.0)
            sent = [list(m)[0] + str(m[list(m)[0]]) for m in sim.sent]
            self.assertIn("transportFade{'direction': 1}", sent)   # silent while playing
            self.assertEqual(sim.sent[-1], {"transportFade": {"direction": 0}})
            self.assertEqual(c.state["durationSource"], "measured-precise")
        finally:
            sim.close()

    def test_precise_reports_a_song_that_carries_on(self):
        c, sim = make(continues=(IDS[0],))
        try:
            r = c.measure_song_length(precise=True, settle=2.0)
            self.assertEqual(r["endBehavior"], "continues")
            self.assertEqual(c.state["songId"], IDS[0])        # the next song that started was left behind
            self.assertIs(c.state["playing"], False)
        finally:
            sim.close()


class Setlist(unittest.TestCase):
    def test_walk_with_quick_measure_covers_every_song(self):
        c, sim = make()
        try:
            c.walk_setlist(settle=2.0, measure="quick")
            self.assertEqual(sorted(c.measured), sorted(IDS))
            for sid in IDS:
                self.assertAlmostEqual(c.durations[sid], CLAMP[sid], 1)
            self.assertEqual(c.state["songId"], IDS[0])
            evs = [e for e in c.events if e["type"] == "song.length"]
            self.assertEqual(len(evs), 3)
            self.assertTrue(all(e.get("walk") for e in evs))
        finally:
            sim.close()

    def test_quick_never_replaces_a_precise_value(self):
        c, sim = make()
        try:
            c._store_length({"songId": IDS[0], "duration": 8.1, "lowerBound": 5.2, "precise": True, "precision": 0.5,
                             "endBehavior": "stops", "method": "played-to-end"})
            c._store_length({"songId": IDS[0], "duration": 5.2, "lowerBound": 5.2, "precise": False, "precision": 4.0,
                             "endBehavior": None, "method": "seek-clamp"})
            self.assertEqual(c.durations[IDS[0]], 8.1)
            self.assertEqual(c.state["durationSource"], "measured-precise")
        finally:
            sim.close()

    def test_bad_measure_argument(self):
        c, sim = make()
        try:
            with self.assertRaises(ValueError):
                c.walk_setlist(measure="fast")
        finally:
            sim.close()


if __name__ == "__main__":
    unittest.main()
