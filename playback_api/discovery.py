"""Find section IDs for a song without any account access.

Playback ignores waveformDoubleTap for IDs that are not sections of the *selected* song, so
a burst of candidate IDs followed by "did the position move?" tells us whether the burst
contained a valid ID. Bisecting finds it; then neighbours are walked to get the block.
Requires control enabled and the transport stopped. Moves the position; restores to 0.
"""
from __future__ import annotations

import time

from .client import PlaybackClient

BATCH = 5000  # 5000 frames in one burst worked; 50000 stalls the socket


def _burst(c: PlaybackClient, ids, wait: float = 1.6) -> tuple[bool, float]:
    c.seek(50.0)
    c.wait_for(lambda s: abs((s["position"] or 0) - 50.0) < 0.5, 2)
    before = c.state["position"]
    for i in ids:
        c.jump_to_section(i)
    time.sleep(wait)
    now = c.state["position"]
    return now != before, now


def _bisect(c: PlaybackClient, lo: int, hi: int) -> int:
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if _burst(c, range(lo, mid))[0]:
            hi = mid
        else:
            lo = mid
    return lo


def discover_sections(c: PlaybackClient, song_id: int, id_lo: int, id_hi: int) -> list[tuple[float, int]]:
    """Return [(startTime, sectionId)] for song_id, searching candidate IDs in [id_lo, id_hi)."""
    c.select_song(song_id)
    c.wait_for(lambda s: s["songId"] == song_id, 3)
    a, hit = id_lo, None
    while a < id_hi and hit is None:
        b = min(a + BATCH, id_hi)
        if _burst(c, range(a, b))[0]:
            hit = _bisect(c, a, b)
        a = b
    if hit is None:
        return []
    found: dict[int, float] = {}

    def probe(i: int) -> bool:
        ok, t = _burst(c, [i], 1.2)
        if ok:
            found[i] = t
        return ok

    i = hit
    while probe(i):
        i -= 1
    i = hit + 1
    miss = 0
    while miss < 3:
        miss = 0 if probe(i) else miss + 1
        i += 1
    c.return_to_start()
    return sorted((t, sid) for sid, t in found.items())
