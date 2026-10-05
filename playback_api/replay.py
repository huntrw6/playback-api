"""Replay a capture log (pr_capture.py format) through the Normalizer."""
import json
import sys
from datetime import datetime

from .normalizer import Normalizer


def replay(path: str, sections=None):
    n = Normalizer(sections)
    for line in open(path):
        if line.startswith("#"):
            continue
        ts, _, payload = line.rstrip("\n").split(" ", 2)
        t = datetime.fromisoformat(ts).timestamp()
        for e in n.feed(json.loads(payload), t):
            yield e


if __name__ == "__main__":
    t0 = None
    for e in replay(sys.argv[1]):
        t0 = t0 or e["ts"]
        rest = {k: v for k, v in e.items() if k not in ("type", "ts")}
        print("%8.2f %-16s %s" % (e["ts"] - t0, e["type"], json.dumps(rest)))
