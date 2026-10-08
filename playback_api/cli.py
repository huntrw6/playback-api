"""CLI: python -m playback_api <command>"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from .client import PlaybackClient, Refused
from .data import load_setlist_file


def _resolve_host(a) -> tuple:
    """Pick the Playback to talk to: explicit --host / $PLAYBACK_HOST, else this computer, else (if --scan)
    the network. Returns (host, port) or exits with a helpful message."""
    from .find import find_playback
    host = a.host or os.environ.get("PLAYBACK_HOST")
    if host:
        return host, a.port
    found = find_playback(a.port, scan=a.scan, subnets=a.subnet or None,
                          log=lambda m: print(m, file=sys.stderr, flush=True))
    if not found:
        print("Playback was not found on this computer%s.\n"
              "Check that Playback is running with 'Allow Remote Connections' on, then either pass --host <address>%s."
              % ("" if not a.scan else " or on the networks scanned", "" if a.scan else ", or add --scan to search the network"),
              file=sys.stderr)
        raise SystemExit(2)
    if len(found) > 1:
        print("Found several Playbacks, using the first (pass --host to choose): %s"
              % ", ".join(f["host"] for f in found), file=sys.stderr)
    f = found[0]
    print("Using Playback at %s:%d (%s)" % (f["host"], f["port"], f["source"]), file=sys.stderr, flush=True)
    return f["host"], f["port"]


def _client(a, control: bool = False) -> PlaybackClient:
    data = load_setlist_file(a.setlist_file) if a.setlist_file else None
    host, port = _resolve_host(a)
    c = PlaybackClient(host, port, allow_control=control or getattr(a, "allow_control", False), data=data,
                       fast_transport=getattr(a, "fast", False))
    return c.start()


def main(argv=None) -> int:
    def common(parser, suppress):
        d = (lambda v: argparse.SUPPRESS) if suppress else (lambda v: v)
        parser.add_argument("--host", default=d(None),
                            help="Playback's address. Omit to look on this computer ($PLAYBACK_HOST also works)")
        parser.add_argument("--port", type=int, default=d(8080))
        parser.add_argument("--scan", action="store_true", default=d(False),
                            help="if Playback is not on this computer, search the connected network for it")
        parser.add_argument("--subnet", action="append", metavar="CIDR", default=d(None),
                            help="network to scan instead of the attached ones, e.g. 192.168.1.0/24 (repeatable; implies --scan)")
        parser.add_argument("--setlist-file", default=d(None),
                            help="JSON with song order, durations, names and section maps (see data/)")

    p = argparse.ArgumentParser(prog="playback_api", description="Unofficial Playback API (read-only by default)")
    common(p, False)
    shared = argparse.ArgumentParser(add_help=False)  # lets the same flags go before OR after the command
    common(shared, True)
    sub = p.add_subparsers(dest="cmd", required=True)

    fd = sub.add_parser("find", parents=[shared], help="locate Playback (this computer first, then the network with --scan) and exit")
    fd.add_argument("--all", action="store_true", help="report every Playback found, not just the first")
    fd.add_argument("--json", action="store_true", help="machine-readable output")
    li = sub.add_parser("listen", parents=[shared], help="print normalized events as JSON lines")
    li.add_argument("--fast", action="store_true", help="announce play/pause/stop from the command (about 30 ms) instead of waiting for the next heartbeat (up to 1 s)")
    li.add_argument("--volume-events", action="store_true", help="also emit mixer.volume for every fader message (very chatty)")
    rp = sub.add_parser("replay", help="replay a capture file offline and print normalized events")
    rp.add_argument("file")
    rp.add_argument("--volume-events", action="store_true")
    rp.add_argument("--fast", action="store_true", help="replay with fast transport events")
    cp = sub.add_parser("capture", parents=[shared], help="passively record every message to a file (sends nothing)")
    cp.add_argument("file")
    cp.add_argument("--hours", type=float, default=1.0)
    sub.add_parser("state", parents=[shared], help="print current state once")
    s = sub.add_parser("serve", parents=[shared], help="HTTP + SSE server")
    s.add_argument("--bind", default="127.0.0.1")
    s.add_argument("--http-port", type=int, default=8787)
    s.add_argument("--allow-control", action="store_true", help="enable POST /command/* (off by default)")
    s.add_argument("--fast", action="store_true", help="announce play/pause/stop from the command (about 30 ms) instead of the next heartbeat (up to 1 s)")
    w = sub.add_parser("walk-setlist", parents=[shared], help="discover setlist order (needs control; transport must be stopped)")
    w.add_argument("--measure", choices=["quick", "precise"], help="also measure every song's length")
    ms = sub.add_parser("measure-songs", parents=[shared], help="measure every song's length (needs control; transport must be stopped)")
    ms.add_argument("--precise", action="store_true", help="also play the last seconds of each song, silently, for +-0.5 s (about 17 s per song)")
    d = sub.add_parser("discover-sections", parents=[shared], help="find section IDs for a song (needs control)")
    d.add_argument("song", help="song id or number")
    d.add_argument("id_lo", type=int)
    d.add_argument("id_hi", type=int)
    sel = sub.add_parser("select", parents=[shared], help="select a song by id or number (needs control; transport must be stopped)")
    sel.add_argument("song")
    go = sub.add_parser("play", parents=[shared], help="optionally select a song by id/number, then play (needs control)")
    go.add_argument("song", nargs="?")
    sub.add_parser("pause", parents=[shared], help="pause (needs control)")
    a = p.parse_args(argv)
    if a.subnet:
        a.scan = True

    if a.cmd == "replay":
        from .replay import replay
        t0 = None
        for e in replay(a.file, volume_events=a.volume_events, fast_transport=a.fast):
            t0 = t0 or e["ts"]
            print(json.dumps(dict(e, ts=round(e["ts"] - t0, 3))))
        return 0

    if a.cmd == "capture":
        from .capture import record
        host, port = _resolve_host(a)
        print("recording %s:%d for %.1f h -> %s (Ctrl+C to stop)" % (host, port, a.hours, a.file), file=sys.stderr, flush=True)
        try:
            n = record(a.file, a.hours, host, port)
        except KeyboardInterrupt:
            return 0
        print("%d frames" % n, file=sys.stderr)
        return 0

    if a.cmd == "find":
        from .find import find_playback
        if a.host or os.environ.get("PLAYBACK_HOST"):
            from .find import probe
            h = a.host or os.environ["PLAYBACK_HOST"]
            r = probe(h, a.port)
            found = [dict(r, source="--host")] if r else []
        else:
            found = find_playback(a.port, scan=a.scan, find_all=a.all, subnets=a.subnet or None,
                                  log=lambda m: print(m, file=sys.stderr, flush=True))
        if a.json:
            print(json.dumps(found))
        elif found:
            for f in found:
                print("%s:%d  (%s)  song %s, %s" % (f["host"], f["port"], f["source"], f["songId"],
                                                   "playing" if f["playing"] else "stopped"))
        else:
            print("Playback not found%s." % ("" if a.scan else " on this computer (add --scan to search the network)"),
                  file=sys.stderr)
        return 0 if found else 2

    if a.cmd == "listen":
        c = _client(a)
        c.volume_events = a.volume_events
        c.norm.volume_events = a.volume_events
        c.on_event = lambda e: print(json.dumps(e), flush=True)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            return 0
    needs_control = a.cmd in ("walk-setlist", "measure-songs", "discover-sections", "select", "play", "pause")
    c = _client(a, control=needs_control)
    if not c.wait_for(lambda s: s["songId"] is not None, 8):
        print("could not connect: %s\n(is Playback running with 'Allow Remote Connections' on?)" % c.last_error, file=sys.stderr)
        return 2
    time.sleep(0.3)
    try:
        if a.cmd == "state":
            print(json.dumps(c.state, indent=2))
        elif a.cmd == "serve":
            from .server import make_server
            c.allow_control = a.allow_control
            srv = make_server(c, a.bind, a.http_port)
            print("serving on http://%s:%d (control %s)" % (a.bind, a.http_port, "ON" if a.allow_control else "off"), flush=True)
            srv.serve_forever()
        elif a.cmd == "walk-setlist":
            print(json.dumps(c.walk_setlist(measure=a.measure)))
        elif a.cmd == "measure-songs":
            res = c.measure_setlist(precise=a.precise)
            print(json.dumps([dict(number=c.song_number(k), **v) for k, v in res.items()], indent=2))
        elif a.cmd == "discover-sections":
            from .discovery import discover_sections
            print(json.dumps(discover_sections(c, c.resolve_song(a.song), a.id_lo, a.id_hi)))
        elif a.cmd == "select":
            c.select_song(a.song)
            c.wait_for(lambda s: s["songId"] == c.resolve_song(a.song), 4)
            print(json.dumps(c.state, indent=2))
        elif a.cmd == "play":
            if a.song:
                c.select_song(a.song)
                c.wait_for(lambda s: s["songId"] == c.resolve_song(a.song), 4)
            c.play()
            time.sleep(1.5)
            print(json.dumps(c.state, indent=2))
        elif a.cmd == "pause":
            c.pause()
            time.sleep(1.5)
            print(json.dumps(c.state, indent=2))
    except Refused as e:
        print("refused: %s" % e, file=sys.stderr)
        return 3
    finally:
        c.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
