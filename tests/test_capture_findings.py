"""Behaviour found in a 4 h passive practice capture. Synthetic messages, no private data."""
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from playback_api.client import PlaybackClient  # noqa: E402
from playback_api.normalizer import Normalizer  # noqa: E402
from playback_api.replay import replay  # noqa: E402

SONG = 91000001


def hb(t=0.0, playing=False, pad=False, song=SONG, state=None, ver=1):
    sd = {"sequenceTime": t, "setlistCloudVersion": ver,
          "setlistState": state or {"ready": {}},
          "sequencerPlayState": {"playing" if playing else "stopped": {}},
          "padPlayerPlayState": {"playing" if pad else "stopped": {}}}
    if song is not None:
        sd["setlistSongID"] = song
    return {"heartbeat": {"stateData": sd}}


def run(msgs, **kw):
    n, out = Normalizer(**kw), []
    for ts, m in msgs:
        out += n.feed(m, ts)
    return n, out


def types(evs):
    return [e["type"] for e in evs]


class MissingSongId(unittest.TestCase):
    def test_first_heartbeat_without_song(self):
        _, e = run([(0, hb(song=None))])
        self.assertEqual(types(e), ["state.snapshot"])
        self.assertIsNone(e[0]["songId"])

    def test_song_lost_then_loaded_is_selection_not_start_or_end(self):
        _, e = run([(0, hb(0)), (1, hb(song=None)), (2, hb(song=None)), (3, hb(0, song=SONG + 1))])
        self.assertEqual(types(e), ["state.snapshot", "song.cleared", "song.selected"])
        self.assertEqual(e[1]["previousSongId"], SONG)
        self.assertEqual(e[2]["reason"], "loaded")

    def test_song_lost_while_playing_does_not_emit_song_ended(self):
        _, e = run([(0, hb(120, True)), (1, hb(121, True)), (2, hb(song=None))])
        self.assertNotIn("song.ended", types(e))
        self.assertIn("song.cleared", types(e))

    def test_loaded_song_mid_play_is_not_a_start(self):
        _, e = run([(0, hb(song=None)), (1, hb(40, True, song=SONG))])
        self.assertNotIn("song.started", types(e))


class SectionNavigation(unittest.TestCase):
    def test_navigate_event_and_no_false_jump(self):
        msgs = [(0, hb(10, True)), (1, hb(11, True)),
                (1.2, {"transportNavigateToSongMapElementIndex": {"index": 3}}),
                (2, hb(60, True))]
        _, e = run(msgs)
        nav = [x for x in e if x["type"] == "section.navigate"]
        self.assertEqual([x["index"] for x in nav], [3])
        self.assertNotIn("position.jump", types(e))

    def test_burst_while_stopped_then_play_from_start_window_is_start_only_at_zero(self):
        msgs = [(0, hb(0)), (1, {"transportNavigateToSongMapElementIndex": {"index": 1}}), (1.2, hb(45)),
                (5, hb(45.2, True))]
        _, e = run(msgs)
        self.assertIn("song.resumed", types(e))
        self.assertNotIn("song.started", types(e))


class Mixer(unittest.TestCase):
    def test_mapping_ids_parsed(self):
        _, e = run([(0, {"mixerTrackMute": {"muteState": {"muted": {}}, "mappingIDs": ["TrackMute_Bus_18"]}}),
                    (1, {"mixerTrackSolo": {"soloState": {"soloed": {}}, "mappingIDs": ["TrackSolo_SongTrack_37"]}}),
                    (2, {"mixerTrackSolo": {"soloState": {"unsoloed": {}}, "mappingIDs": ["TrackSolo_SongTrack_37"]}})])
        self.assertEqual([(x["type"], x["scope"], x["number"], x["on"]) for x in e],
                         [("mixer.mute", "bus", 18, True), ("mixer.solo", "track", 37, True),
                          ("mixer.solo", "track", 37, False)])

    def test_unmute_state(self):
        n, e = run([(0, {"mixerTrackMute": {"muteState": {"unmuted": {}}, "mappingIDs": ["TrackMute_SongTrack_4"]}})])
        self.assertFalse(e[0]["on"])
        self.assertEqual(n.mixer_mute, {"track:4": False})

    def test_volume_quiet_by_default_but_tracked(self):
        msg = {"mixerTrackVolume": {"level": 0.5, "mappingIDs": ["TrackVolume_SongTrack_12"]}}
        n, e = run([(0, msg), (1, msg)])
        self.assertEqual(e, [])
        self.assertEqual(n.mixer["track:12"], 0.5)

    def test_volume_events_opt_in(self):
        msg = {"mixerTrackVolume": {"level": 0.25, "mappingIDs": ["TrackVolume_Bus_3"]}}
        _, e = run([(0, msg)], volume_events=True)
        self.assertEqual((e[0]["type"], e[0]["scope"], e[0]["number"], e[0]["level"]), ("mixer.volume", "bus", 3, 0.25))

    def test_unrecognised_mapping_id_kept_raw(self):
        _, e = run([(0, {"mixerTrackMute": {"muteState": {"muted": {}}, "mappingIDs": ["Weird_1"]}})])
        self.assertEqual(e[0]["mappingId"], "Weird_1")
        self.assertIsNone(e[0]["scope"])


class OtherMessages(unittest.TestCase):
    def test_infinite_loop(self):
        n, e = run([(0, {"mixerInfiniteLoop": {"active": True}}), (1, {"mixerInfiniteLoop": {"active": False}})])
        self.assertEqual([x["active"] for x in e], [True, False])
        self.assertIs(n.infinite_loop, False)

    def test_setlist_loaded_and_updated_carry_no_names(self):
        _, e = run([(0, {"contentLoadSetlist": {"setlistData": {"setlistID": 7, "setlistName": "Private", "isDemo": False}}}),
                    (1, {"contentUpdateSetlist": {"rentalData": [{}, {}], "modularClickSongData": [{}]}})])
        self.assertEqual(e[0]["setlistId"], 7)
        self.assertNotIn("Private", json.dumps(e))
        self.assertEqual((e[1]["rentals"], e[1]["modularClick"]), (2, 1))

    def test_audio_device_and_transition(self):
        _, e = run([(0, {"audioDeviceChanged": {}}), (1, {"setlistSelectSongTransition": {"songIndex": 2, "transition": 4}})])
        self.assertEqual(types(e), ["audio.device.changed", "song.transition.requested"])
        self.assertEqual((e[1]["songIndex"], e[1]["transition"]), (2, 4))

    def test_nothing_in_the_capture_is_unknown_any_more(self):
        kinds = ["contentLoadSetlist", "contentUpdateSetlist", "mixerTrackVolume", "mixerTrackMute", "mixerTrackSolo",
                 "mixerInfiniteLoop", "transportNavigateToSongMapElementIndex", "setlistSelectSongTransition",
                 "audioDeviceChanged"]
        for k in kinds:
            _, e = run([(0, {k: {}})])
            self.assertNotIn("message.unknown", types(e), k)


class SetlistState(unittest.TestCase):
    def test_states_and_changes(self):
        _, e = run([(0, hb(0)),
                    (1, hb(0, state={"downloadingContent": {}})),
                    (2, hb(0, state={"changed": {"_0": {"setlistUnsaved": {}}}})),
                    (3, hb(0, state={"changed": {"_0": {"midiCuesNotSaved": {}}}})),
                    (4, hb(0, state={"ready": {}})),
                    (5, hb(0, state={"ready": {}}))])
        st = [x for x in e if x["type"] == "setlist.state"]
        self.assertEqual([x["state"] for x in st], ["downloading", "unsaved", "midi-cues-unsaved", "ready"])
        self.assertEqual(e[0]["setlistState"], "ready")


class ReplayFormats(unittest.TestCase):
    def test_json_lines_capture_format(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            for rec in ({"t": 1, "iso": "x", "kind": "start"}, {"t": 2, "kind": "connect"},
                        {"t": 3, "kind": "frame", "raw": json.dumps(hb(0))},
                        {"t": 4, "kind": "frame", "raw": json.dumps({"mixerInfiniteLoop": {"active": True}})},
                        {"t": 5, "kind": "end"}):
                f.write(json.dumps(rec) + "\n")
        try:
            self.assertEqual(types(list(replay(f.name))), ["state.snapshot", "loop.infinite"])
        finally:
            os.unlink(f.name)


class ClientState(unittest.TestCase):
    def test_state_exposes_new_fields(self):
        c = PlaybackClient("x")
        c.norm.feed(hb(0), time.time())
        c.norm.feed({"mixerTrackMute": {"muteState": {"muted": {}}, "mappingIDs": ["TrackMute_Bus_2"]}}, time.time())
        c.norm.feed({"mixerInfiniteLoop": {"active": True}}, time.time())
        s = c.state
        self.assertEqual((s["setlistState"], s["infiniteLoop"], s["muted"], s["soloed"]), ("ready", True, ["bus:2"], []))
        self.assertIsNone(s["heartbeatAge"])

    def test_state_with_no_song(self):
        c = PlaybackClient("x")
        c.norm.feed(hb(song=None), time.time())
        s = c.state
        self.assertIsNone(s["songId"])
        self.assertIsNone(s["sectionId"])


class Stall(unittest.TestCase):
    """Playback can go silent for minutes with the socket open; that must surface as a lost connection."""

    def test_silence_is_reported_as_stalled_then_reconnects(self):
        import playback_api.client as client_mod

        frames = [json.dumps(hb(0)), None]  # one heartbeat, then 5 s of silence

        class Silent:
            def __init__(self, *a, **k):
                self.sent = []

            def recv(self, timeout=None):
                return frames.pop(0) if frames else None

            def send(self, t):
                pass

            def close(self):
                pass

        real = client_mod.WebSocket
        client_mod.WebSocket = Silent
        events = []
        c = PlaybackClient("x", on_event=events.append)
        try:
            c.start()
            deadline = time.time() + 5
            while time.time() < deadline and not any(e["type"] == "connection.lost" for e in events):
                time.sleep(0.02)
        finally:
            c.stop()
            client_mod.WebSocket = real
        lost = [e for e in events if e["type"] == "connection.lost"]
        self.assertTrue(lost)
        self.assertEqual(lost[0]["reason"], "stalled")
        self.assertGreaterEqual(sum(e["type"] == "connection.up" for e in events), 1)


if __name__ == "__main__":
    unittest.main()
