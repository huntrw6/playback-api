"""v2.1: fast transport, select-while-playing, Next/Previous request. Synthetic messages only."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from playback_api.normalizer import Normalizer  # noqa: E402

from test_capture_findings import hb  # noqa: E402

A, B = 91000001, 91000002


def hbs(pos, playing, song=A):
    m = hb(pos, playing)
    m["heartbeat"]["stateData"]["setlistSongID"] = song
    return m


def types(e):
    return [x["type"] for x in e]


class Fast(unittest.TestCase):
    def mk(self, fast=True):
        n = Normalizer(fast_transport=fast)
        n.feed(hbs(0.0, False), 0.0)
        return n

    def test_off_by_default_play_waits_for_the_heartbeat(self):
        n = self.mk(fast=False)
        self.assertEqual(n.feed({"transportPlay": {"playing": True}}, 1.0), [])
        self.assertEqual(types(n.feed(hbs(0.4, True), 1.5)), ["song.started"])

    def test_play_is_announced_at_once_and_the_heartbeat_does_not_repeat_it(self):
        n = self.mk()
        e = n.feed({"transportPlay": {"playing": True}}, 1.0)
        self.assertEqual(types(e), ["song.started"])
        self.assertTrue(e[0]["provisional"])
        self.assertEqual(e[0]["songId"], A)
        self.assertEqual(n.feed(hbs(0.6, True), 1.6), [])        # confirmation is silent
        self.assertEqual(n.feed(hbs(1.6, True), 2.6), [])
        self.assertEqual(types(n.feed({"transportPlay": {"playing": False}}, 3.0)), ["song.paused"])
        self.assertEqual(n.feed(hbs(2.9, False), 3.4), [])

    def test_resume_is_not_a_start(self):
        n = self.mk()
        n.feed({"transportPlay": {"playing": True}}, 1.0)
        n.feed(hbs(20.0, True), 1.4)
        n.feed({"transportPlay": {"playing": False}}, 21.0)
        n.feed(hbs(40.0, False), 21.2)
        self.assertEqual(types(n.feed({"transportPlay": {"playing": True}}, 22.0)), ["song.resumed"])

    def test_play_while_already_playing_is_ignored(self):
        n = self.mk()
        n.feed({"transportPlay": {"playing": True}}, 1.0)
        self.assertEqual(n.feed({"transportPlay": {"playing": True}}, 1.2), [])

    def test_return_to_start_while_playing_is_stopped_once(self):
        n = self.mk()
        n.feed({"transportPlay": {"playing": True}}, 1.0)
        n.feed(hbs(1.5, True), 1.5)
        e = n.feed({"transportReturnToStart": {}}, 5.0)
        self.assertEqual(types(e), ["song.stopped"])
        self.assertEqual(e[0]["reason"], "return-to-start")
        self.assertEqual(n.feed(hbs(0.0, False), 5.4), [])

    def test_unconfirmed_command_is_reported_as_reverted(self):
        n = self.mk()
        n.feed({"transportPlay": {"playing": True}}, 1.0)
        self.assertEqual(n.feed(hbs(0.0, False), 1.5), [])        # not confirmed yet, wait
        self.assertEqual(n.feed(hbs(0.0, False), 2.5), [])
        e = n.feed(hbs(0.0, False), 4.2)
        self.assertEqual(types(e), ["transport.reverted"])
        self.assertIs(e[0]["playing"], False)
        self.assertEqual(n.feed(hbs(0.0, False), 5.2), [])

    def test_a_late_start_after_a_revert_is_still_reported(self):
        n = self.mk()
        n.feed({"transportPlay": {"playing": True}}, 1.0)
        for t in (1.5, 2.5, 4.2):
            n.feed(hbs(0.0, False), t)
        self.assertEqual(types(n.feed(hbs(0.5, True), 5.2)), ["song.started"])

    def test_pad_and_other_events_are_not_swallowed(self):
        n = self.mk()
        n.feed({"transportPlay": {"playing": True}}, 1.0)
        m = hbs(0.6, True)
        m["heartbeat"]["stateData"]["padPlayerPlayState"] = {"playing": {}}
        self.assertEqual(types(n.feed(m, 1.6)), ["pad.on"])


class SelectWhilePlaying(unittest.TestCase):
    def play(self, n):
        n.feed(hbs(0.0, False), 0.0)
        n.feed(hbs(3.0, True), 3.0)
        n.feed(hbs(4.0, True), 4.0)

    def test_heartbeat_path_reports_the_old_song_stopped(self):
        n = Normalizer()
        self.play(n)
        e = n.feed({"setlistSelectSong": {"setlistSongID": B}}, 5.0)
        self.assertEqual(types(e), ["song.selected"])
        e = n.feed(hbs(0.0, False, B), 5.5)
        self.assertEqual(types(e), ["song.stopped"])
        self.assertEqual((e[0]["songId"], e[0]["reason"]), (A, "song-selected"))

    def test_fast_path_reports_it_at_once_and_once(self):
        n = Normalizer(fast_transport=True)
        self.play(n)
        e = n.feed({"setlistSelectSong": {"setlistSongID": B}}, 5.0)
        self.assertEqual(types(e), ["song.selected", "song.stopped"])
        self.assertTrue(e[1]["provisional"])
        self.assertEqual(n.feed(hbs(0.0, False, B), 5.5), [])

    def test_natural_end_is_still_an_auto_advance(self):
        n = Normalizer()
        self.play(n)
        e = n.feed(hbs(0.0, True, B), 5.0)
        self.assertEqual(types(e), ["song.ended", "song.changed", "song.started"])

    def test_selecting_while_stopped_is_just_a_selection(self):
        n = Normalizer(fast_transport=True)
        n.feed(hbs(0.0, False), 0.0)
        self.assertEqual(types(n.feed({"setlistSelectSong": {"setlistSongID": B}}, 1.0)), ["song.selected"])


class NavigateRequested(unittest.TestCase):
    def test_next_and_previous_are_named(self):
        n = Normalizer()
        n.feed(hbs(0.0, False), 0.0)
        a = n.feed({"transportNextSong": {}}, 1.0)
        b = n.feed({"transportPreviousSong": {}}, 2.0)
        self.assertEqual([(x["type"], x["direction"]) for x in a + b],
                         [("navigate.requested", "next"), ("navigate.requested", "previous")])


if __name__ == "__main__":
    unittest.main()


class StateField(unittest.TestCase):
    def test_client_reports_fast_mode(self):
        from playback_api.client import PlaybackClient
        self.assertIs(PlaybackClient("x", 1, fast_transport=True).state["fastTransport"], True)
        self.assertIs(PlaybackClient("x", 1).state["fastTransport"], False)
        self.assertTrue(PlaybackClient("x", 1, fast_transport=True).norm.fast_transport)
