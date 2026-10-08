"""Replay a capture through the Normalizer.

Understands both capture formats:
  * `<iso-timestamp> <json>` lines (pr_capture.py), and
  * JSON-lines records `{"t":..., "kind":"frame", "raw":"..."}` (pb_capture.py, the passive
    long-run capture). Non-frame records (start/connect/disconnect/end) are skipped.
"""
import json
import sys
from datetime import datetime

from .normalizer import Normalizer


def _frames(path):
    with open(path) as f:
        for line in f:
            if not line.strip() or line.startswith("#"):
                continue
            if line.lstrip().startswith("{"):
                rec = json.loads(line)
                if rec.get("kind") == "frame":
                    yield rec["t"], json.loads(rec["raw"])
                continue
            ts, _, payload = line.rstrip("\n").split(" ", 2)
            yield datetime.fromisoformat(ts).timestamp(), json.loads(payload)


def replay(path: str, sections=None, volume_events: bool = False, fast_transport: bool = False):
    n = Normalizer(sections, volume_events=volume_events, fast_transport=fast_transport)
    for t, msg in _frames(path):
        for e in n.feed(msg, t):
            yield e


if __name__ == "__main__":
    t0 = None
    for e in replay(sys.argv[1], volume_events="--volume" in sys.argv):
        t0 = t0 or e["ts"]
        rest = {k: v for k, v in e.items() if k not in ("type", "ts")}
        print("%8.2f %-16s %s" % (e["ts"] - t0, e["type"], json.dumps(rest)))
