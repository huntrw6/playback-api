"""Offline tests: replay real captures through the Normalizer and check the events."""
import json
import os
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from playback_api.normalizer import Normalizer  # noqa: E402

HERE = os.path.dirname(__file__)


def run(msgs, sections=None):
    n, out = Normalizer(sections), []
    for ts, m in msgs:
        out += n.feed(m, ts)
    return out


def hb(song, t, playing, pad=False, ver=11):
    return {"heartbeat": {"stateData": {"setlistSongID": song, "sequenceTime": t, "setlistCloudVersion": ver,
            "setlistState": {}, "sequencerPlayState": {"playing" if playing else "stopped": {}},
            "padPlayerPlayState": {"playing" if pad else "stopped": {}}}}}


def types(evs):
    return [e["type"] for e in evs]


class Rules(unittest.TestCase):
    def test_start_vs_resume(self):
        e = run([(0, hb(1, 0, False)), (1, {"setlistSelectSong": {"setlistSongID": 1}}), (2, hb(1, 0.4, True)),
                 (3, hb(1, 9, False)), (4, hb(1, 9.1, True))])
        self.assertEqual(types(e), ["state.snapshot", "song.selected", "song.started", "song.paused", "song.resumed"])

    def test_return_to_start_then_play_is_started(self):
        e = run([(0, hb(1, 50, False)), (1, {"transportReturnToStart": {}}), (1.1, hb(1, 0, False)), (5, hb(1, 0.3, True))])
        self.assertIn("song.started", types(e))

    def test_play_after_seek_is_resume(self):
        e = run([(0, hb(1, 0, False)), (1, {"waveformSeek": {"sequenceTime": 100}}), (1.1, hb(1, 100, False)), (5, hb(1, 100.2, True))])
        self.assertIn("song.resumed", types(e))
        self.assertNotIn("song.started", types(e))

    def test_auto_advance_stop_and_keep_playing(self):
        stop = run([(0, hb(1, 300, True)), (1, hb(1, 301, True)), (2, hb(2, 0, False))])
        self.assertEqual(types(stop), ["state.snapshot", "song.ended", "song.changed"])
        self.assertFalse([e for e in stop if e["type"] == "song.changed"][0]["continuesPlaying"])
        go = run([(0, hb(1, 300, True)), (1, hb(2, 0.4, True))])
        self.assertTrue([e for e in go if e["type"] == "song.changed"][0]["continuesPlaying"])
        self.assertIn("song.started", types(go))

    def test_navigation_while_stopped_is_selected(self):
        e = run([(0, hb(1, 0, False)), (1, hb(2, 0, False))])
        self.assertEqual([x["reason"] for x in e if x["type"] == "song.selected"], ["navigation"])

    def test_fade_is_message_only(self):
        n = Normalizer()
        self.assertIsNone(n.fade)
        n.feed({"transportFade": {"direction": 1}}, 0)
        self.assertTrue(n.fade)

    def test_loop_wrap_detected(self):
        secs = {1: [(0.0, 10), (12.4, 11), (20.7, 12)]}
        e = run([(0, hb(1, 18.5, True)), (1, hb(1, 19.5, True)), (2, hb(1, 20.5, True)), (3, hb(1, 12.6, True)), (4, hb(1, 13.6, True))], secs)
        j = [x for x in e if x["type"] == "position.jump"]
        self.assertEqual(len(j), 1)
        self.assertEqual(j[0]["likely"], "loop")

    def test_setlist_version_change(self):
        e = run([(0, hb(1, 0, False, ver=11)), (1, hb(1, 0, False, ver=12))])
        self.assertIn("setlist.changed", types(e))

    def test_unknown_message(self):
        self.assertEqual(types(run([(0, {"somethingNew": {"x": 1}})])), ["message.unknown"])


class RealCapture(unittest.TestCase):
    """Replays the operator-driven session (955 frames) and checks what the operator did."""
    path = os.path.join(HERE, "fixtures", "operator_session.jsonl")

    @unittest.skipUnless(os.path.exists(path), "fixture missing")
    def test_operator_session(self):
        out = []
        n = Normalizer()
        for line in open(self.path):
            if line.startswith("#"):
                continue
            ts, _, p = line.rstrip("\n").split(" ", 2)
            out += n.feed(json.loads(p), datetime.fromisoformat(ts).timestamp())
        c = {}
        for e in out:
            c[e["type"]] = c.get(e["type"], 0) + 1
        self.assertEqual(c["song.changed"], 2)           # two natural ends
        self.assertEqual([e["continuesPlaying"] for e in out if e["type"] == "song.changed"], [False, True])
        self.assertEqual(c["fade.out"], 2)
        self.assertEqual(c["fade.in"], 2)
        self.assertGreaterEqual(c["song.started"], 8)
        self.assertEqual(c["song.stopped"], 3)           # three Return-to-Start presses (after pause)


if __name__ == "__main__":
    unittest.main()
