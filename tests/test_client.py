"""Offline tests for song/section addressing, guards and the HTTP layer using a fake Playback."""
import json
import os
import sys
import threading
import time
import unittest
import urllib.request
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from playback_api.client import PlaybackClient, ControlDisabled, Refused, UnknownSong  # noqa: E402
from playback_api.data import load_setlist_file  # noqa: E402
from playback_api.server import make_server  # noqa: E402

DATA = os.path.join(os.path.dirname(__file__), "..", "data", "example-setlist.json")
IDS = [91000001, 91000002, 91000003, 91000004, 91000005]


class FakeWS:
    def __init__(self): self.sent = []
    def send(self, text): self.sent.append(json.loads(text))
    def close(self): pass


def make(control=True, playing=False, seeded=True, version=11):
    data = load_setlist_file(DATA)
    c = PlaybackClient("x", allow_control=control, data=data)
    c._ws, c.connected = FakeWS(), True
    c.norm.feed({"heartbeat": {"stateData": {"setlistSongID": IDS[0], "sequenceTime": 0, "setlistCloudVersion": version,
        "setlistState": {}, "sequencerPlayState": {"playing" if playing else "stopped": {}},
        "padPlayerPlayState": {"stopped": {}}}}}, time.time())
    if seeded:
        c._maybe_seed(version)
    return c


class Addressing(unittest.TestCase):
    def test_numbers_and_ids(self):
        c = make()
        self.assertEqual([c.resolve_song(n) for n in (1, 2, 3, 4, 5)], IDS)
        self.assertEqual(c.resolve_song("3"), IDS[2])
        self.assertEqual(c.resolve_song(IDS[3]), IDS[3])

    def test_bad_refs(self):
        c = make()
        for bad in (0, 6, 99, "x", 12345678901):
            with self.assertRaises(UnknownSong):
                c.resolve_song(bad)

    def test_select_by_number_sends_real_id(self):
        c = make()
        c.select_song(3)
        self.assertEqual(c._ws.sent[-1], {"setlistSelectSong": {"setlistSongID": IDS[2]}})

    def test_section_number_in_song(self):
        c = make()
        c.jump_to_section(2, song=2)
        self.assertEqual(c._ws.sent[-1], {"waveformDoubleTap": {"setlistSongSectionID": 92000677}})
        c.jump_to_section(92000652)
        self.assertEqual(c._ws.sent[-1]["waveformDoubleTap"]["setlistSongSectionID"], 92000652)
        with self.assertRaises(UnknownSong):
            c.jump_to_section(99, song=1)

    def test_seed_ignored_when_version_differs(self):
        c = make(version=12)
        self.assertEqual(c.setlist, [])
        c2 = make(control=False, version=12)
        with self.assertRaises(UnknownSong):
            c2.resolve_song(2)

    def test_state_has_numbers_and_remaining(self):
        s = make().state
        self.assertEqual((s["songNumber"], s["songCount"], s["duration"], s["remaining"]), (1, 5, 309.0, 309.0))
        self.assertEqual(s["sectionNumber"], 1)


class Guards(unittest.TestCase):
    def test_read_only(self):
        with self.assertRaises(ControlDisabled):
            make(control=False).play()

    def test_no_song_change_while_playing(self):
        c = make(playing=True)
        for fn in (lambda: c.select_song(2), c.next_song, c.previous_song):
            with self.assertRaises(Refused):
                fn()
        self.assertEqual(c._ws.sent, [])

    def test_own_commands_update_fade(self):
        c = make()
        self.assertIsNone(c.state["fadedOut"])
        c.fade(True)
        self.assertTrue(c.state["fadedOut"])
        c.fade(False)
        self.assertFalse(c.state["fadedOut"])


class Http(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = make()
        cls.srv = make_server(cls.c, "127.0.0.1", 0)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def call(self, path, body=None):
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path),
                                     data=None if body is None else json.dumps(body).encode(),
                                     method="GET" if body is None else "POST")
        try:
            with urllib.request.urlopen(req, timeout=3) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_endpoints(self):
        st, s = self.call("/state")
        self.assertEqual((st, s["songNumber"]), (200, 1))
        st, sl = self.call("/setlist")
        self.assertEqual([x["number"] for x in sl["songs"]], [1, 2, 3, 4, 5])
        self.assertEqual(sl["songs"][4]["duration"], 608.0)
        self.assertEqual(sl["songs"][0]["sections"][1]["number"], 2)

    def test_commands(self):
        self.assertEqual(self.call("/command/select-song", {"song": 4})[0], 200)
        self.assertEqual(self.c._ws.sent[-1]["setlistSelectSong"]["setlistSongID"], IDS[3])
        self.assertEqual(self.call("/command/select-song", {"song": 9})[0], 409)
        self.assertEqual(self.call("/command/seek", {})[0], 400)
        self.assertEqual(self.call("/command/nope", {})[0], 404)
        self.assertEqual(self.call("/command/section", {"section": 3, "song": 1})[0], 200)


if __name__ == "__main__":
    unittest.main()
