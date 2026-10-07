"""v2.0: single/infinite loop, MUTE MIDI, setlist identity, packaged capture. Synthetic messages only."""
import json
import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from playback_api.capture import record  # noqa: E402
from playback_api.client import PlaybackClient  # noqa: E402
from playback_api.data import load_setlist_file  # noqa: E402
from playback_api.normalizer import Normalizer  # noqa: E402
from playback_api.replay import replay  # noqa: E402
from playback_api.server import COMMANDS  # noqa: E402

from test_capture_findings import hb, run, types  # noqa: E402
from test_client import FakeWS  # noqa: E402

SONG = 91000001
SECTIONS = {SONG: [(0.0, 1), (20.0, 2), (40.0, 3)]}


class Loops(unittest.TestCase):
    def test_single_loop_arm_and_disarm_messages(self):
        n, e = run([(0, hb(5.0, True)), (1, {"mixerLoop": {"active": True}}), (2, {"mixerLoop": {"active": False}})])
        loops = [x for x in e if x["type"] == "loop.single"]
        self.assertEqual([x["active"] for x in loops], [True, False])
        self.assertIs(n.single_loop, False)

    def test_single_loop_switches_itself_off_after_one_wrap(self):
        # Armed in section 2 (20-40 s); the heartbeat jumps back to its start; Playback sends nothing for the disarm.
        n = Normalizer(SECTIONS)
        e = []
        t = 0.0
        for pos in [21.0, 22.0]:
            e += n.feed(hb(pos, True), t); t += 1
        e += n.feed({"mixerLoop": {"active": True}}, t)
        for pos in [23.0, 24.0, 25.0, 20.1, 21.1]:  # wraps back to the section start
            e += n.feed(hb(pos, True), t); t += 1
        wraps = [x for x in e if x["type"] == "position.jump"]
        self.assertTrue(wraps and wraps[0]["likely"] == "loop")
        offs = [x for x in e if x["type"] == "loop.single" and x["active"] is False]
        self.assertEqual(len(offs), 1)
        self.assertEqual(offs[0]["reason"], "wrapped")
        self.assertIs(n.single_loop, False)

    def test_single_loop_wrap_is_recognised_without_a_section_map(self):
        n, t, e = Normalizer(), 0.0, []
        for pos in [2.0, 3.0]:
            e += n.feed(hb(pos, True), t); t += 1
        e += n.feed({"mixerLoop": {"active": True}}, t)
        for pos in [4.0, 5.0, 6.0, 0.8, 1.8, 2.8]:  # wraps back, then carries on forward
            e += n.feed(hb(pos, True), t); t += 1
        wraps = [x for x in e if x["type"] == "position.jump"]
        self.assertEqual([w["likely"] for w in wraps], ["loop"])
        self.assertEqual([x["active"] for x in e if x["type"] == "loop.single"], [True, False])

    def test_infinite_loop_wraps_are_labelled_loop(self):
        n, t, e = Normalizer(), 0.0, []
        e += n.feed(hb(1.0, True), t); t += 1
        e += n.feed({"mixerInfiniteLoop": {"active": True}}, t)
        for pos in [2.0, 3.0, 4.0, 0.9, 1.9, 2.9, 3.9, 0.8]:
            e += n.feed(hb(pos, True), t); t += 1
        self.assertEqual([x["likely"] for x in e if x["type"] == "position.jump"], ["loop", "loop"])

    def test_without_a_loop_a_backward_jump_is_not_called_a_loop(self):
        n, t, e = Normalizer(), 0.0, []
        for pos in [2.0, 3.0, 4.0, 0.9, 1.9]:
            e += n.feed(hb(pos, True), t); t += 1
        self.assertEqual([x["likely"] for x in e if x["type"] == "position.jump"], ["queued-section-jump"])

    def test_infinite_loop_keeps_single_loop_state(self):
        n = Normalizer(SECTIONS)
        n.feed(hb(21.0, True), 0)
        n.feed({"mixerInfiniteLoop": {"active": True}}, 1)
        n.feed({"mixerLoop": {"active": True}}, 2)
        t = 3.0
        for pos in [22.0, 23.0, 20.1]:
            n.feed(hb(pos, True), t); t += 1
        self.assertIs(n.single_loop, True)  # infinite loop wraps are not "the one repeat"

    def test_new_setlist_resets_single_loop(self):
        n, _ = run([(0, hb(5.0, True)), (1, {"mixerLoop": {"active": True}}),
                    (2, {"contentLoadSetlist": {"setlistData": {"setlistID": 7, "setlistName": "X"}}})])
        self.assertIsNone(n.single_loop)


class MidiMute(unittest.TestCase):
    def test_midi_mute_toggle(self):
        n, e = run([(0, hb(0)), (1, {"mixerMuteMIDI": {"active": True}}), (2, {"mixerMuteMIDI": {"active": False}})])
        self.assertEqual([x["active"] for x in e if x["type"] == "midi.mute"], [True, False])
        self.assertIs(n.midi_muted, False)


class SetlistIdentity(unittest.TestCase):
    def test_load_carries_id_and_name(self):
        n, e = run([(0, hb(0)), (1, {"contentLoadSetlist": {"setlistData": {"setlistID": 93000001, "setlistName": "Example Service", "isDemo": False}}})])
        ev = [x for x in e if x["type"] == "setlist.loaded"][0]
        self.assertEqual(ev["setlistId"], 93000001)
        self.assertNotIn("Example Service", json.dumps(e))  # the name is state only, never in the event stream
        self.assertEqual((n.setlist_id, n.setlist_name), (93000001, "Example Service"))

    def _client(self, data=None):
        c = PlaybackClient("x", 1, allow_control=True, data=data)
        c._ws = FakeWS()
        return c

    def test_same_version_different_setlist_does_not_reuse_order(self):
        d = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump({"setlistId": 100, "setlistVersion": 3, "songs": [{"id": 1, "position": 1}, {"id": 2, "position": 2}]}, d)
        d.close()
        try:
            data = load_setlist_file(d.name)
            self.assertEqual(data.setlist_id, 100)
            c = self._client(data)
            c.setlist_id = 200  # a different setlist is open
            c._maybe_seed(3)
            self.assertEqual(c.setlist, [])
            c.setlist_id = 100
            c._maybe_seed(3)
            self.assertEqual(c.setlist, [1, 2])
        finally:
            os.unlink(d.name)

    def test_file_without_setlist_id_still_seeds_by_version(self):
        d = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump({"setlistVersion": 3, "songs": [{"id": 1, "position": 1}]}, d)
        d.close()
        try:
            c = self._client(load_setlist_file(d.name))
            c._maybe_seed(3)
            self.assertEqual(c.setlist, [1])
        finally:
            os.unlink(d.name)

    def test_state_exposes_new_fields(self):
        c = self._client()
        c.norm.feed(hb(0), 0)
        c.norm.feed({"mixerLoop": {"active": True}}, 1)
        c.norm.feed({"mixerMuteMIDI": {"active": True}}, 2)
        s = c.state
        self.assertIs(s["singleLoop"], True)
        self.assertIs(s["midiMuted"], True)
        self.assertIn("setlistId", s)


class Commands(unittest.TestCase):
    def test_loop_commands_send_the_observed_messages(self):
        c = PlaybackClient("x", 1, allow_control=True)
        c._ws, c.connected = FakeWS(), True
        c.norm.feed(hb(0), 0)
        c.loop_once(True)
        self.assertEqual(c._ws.sent[-1], {"mixerLoop": {"active": True}})
        c.loop_infinite(False)
        self.assertEqual(c._ws.sent[-1], {"mixerInfiniteLoop": {"active": False}})
        COMMANDS["loop-once"](c, {"active": False})
        self.assertEqual(c._ws.sent[-1], {"mixerLoop": {"active": False}})

    def test_loop_commands_need_control(self):
        c = PlaybackClient("x", 1)
        c._ws, c.connected = FakeWS(), True
        with self.assertRaises(Exception):
            c.loop_once(True)


class Capture(unittest.TestCase):
    def test_record_then_replay(self):
        from test_find import FakeServer
        srv = FakeServer("127.0.0.1")
        out = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False)
        out.close()
        stop = threading.Event()
        try:
            t = threading.Thread(target=record, args=(out.name, 1.0, "127.0.0.1", srv.port, stop), daemon=True)
            t.start()
            time.sleep(1.6)
            stop.set()
            t.join(5)
            kinds = [json.loads(l)["kind"] for l in open(out.name)]
            self.assertEqual(kinds[0], "start")
            self.assertIn("connect", kinds)
            self.assertGreaterEqual(kinds.count("frame"), 2)
            self.assertEqual(kinds[-1], "end")
            self.assertEqual(types(list(replay(out.name)))[0], "state.snapshot")
        finally:
            srv.close()
            os.unlink(out.name)

    def test_replay_reads_the_recorder_format(self):
        f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
        for rec in ({"t": 1.0, "kind": "start"}, {"t": 1.1, "kind": "connect"},
                    {"t": 1.2, "kind": "frame", "raw": json.dumps(hb(0))},
                    {"t": 2.2, "kind": "frame", "raw": json.dumps({"mixerLoop": {"active": True}})},
                    {"t": 3.0, "kind": "disconnect"}):
            f.write(json.dumps(rec) + "\n")
        f.close()
        try:
            self.assertEqual(types(list(replay(f.name))), ["state.snapshot", "loop.single"])
        finally:
            os.unlink(f.name)


if __name__ == "__main__":
    unittest.main()
