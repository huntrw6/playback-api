"""Tests for finding Playback: a fake Playback WebSocket server on loopback stands in for the real app."""
import base64
import hashlib
import json
import os
import socket
import struct
import sys
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from playback_api import find  # noqa: E402

GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
HB = json.dumps({"heartbeat": {"stateData": {"setlistSongID": 91000001, "sequenceTime": 0,
                 "sequencerPlayState": {"stopped": {}}, "padPlayerPlayState": {"stopped": {}},
                 "setlistCloudVersion": 11, "setlistState": {}}}}).encode()


class FakeServer:
    """kind='playback' speaks pr-protocol and sends heartbeats; 'http' answers plain HTTP; 'ws' is a WebSocket
    that never sends a Playback heartbeat."""
    def __init__(self, bind, kind="playback", port=0):
        self.kind = kind
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((bind, port))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.stop = False
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while not self.stop:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _serve(self, c):
        try:
            req = b""
            while b"\r\n\r\n" not in req:
                chunk = c.recv(4096)
                if not chunk:
                    return
                req += chunk
            if self.kind == "http":
                c.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nhi")
                return
            key = [l.split(b": ")[1] for l in req.split(b"\r\n") if l.lower().startswith(b"sec-websocket-key")][0]
            acc = base64.b64encode(hashlib.sha1(key + GUID).digest())
            c.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      b"Sec-WebSocket-Accept: " + acc + b"\r\nSec-WebSocket-Protocol: pr-protocol\r\n\r\n")
            body = HB if self.kind == "playback" else b'{"hello":1}'
            for _ in range(5):
                c.sendall(bytes([0x81, len(body)]) if len(body) < 126 else bytes([0x81, 126]) + struct.pack(">H", len(body)))
                c.sendall(body)
                threading.Event().wait(0.3)
        except OSError:
            pass
        finally:
            c.close()

    def close(self):
        self.stop = True
        self.sock.close()


class Probe(unittest.TestCase):
    def test_real_playback_identified(self):
        s = FakeServer("127.0.0.1")
        try:
            r = find.probe("127.0.0.1", s.port, timeout=2)
            self.assertEqual((r["songId"], r["setlistVersion"], r["playing"]), (91000001, 11, False))
        finally:
            s.close()

    def test_other_services_ignored(self):
        for kind in ("http", "ws"):
            s = FakeServer("127.0.0.1", kind)
            try:
                self.assertIsNone(find.probe("127.0.0.1", s.port, timeout=1.2), kind)
            finally:
                s.close()

    def test_closed_port(self):
        s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close()
        self.assertIsNone(find.probe("127.0.0.1", p, timeout=1))


class Find(unittest.TestCase):
    def setUp(self):
        self._cache = find.CACHE_PATH
        find.CACHE_PATH = os.path.join(os.environ.get("TMPDIR", "/tmp"), "pbapi-test-cache-%d.json" % os.getpid())

    def tearDown(self):
        try:
            os.remove(find.CACHE_PATH)
        except OSError:
            pass
        find.CACHE_PATH = self._cache

    def test_local_first(self):
        s = FakeServer("127.0.0.1")
        try:
            r = find.find_playback(s.port, scan=True)
            self.assertEqual([(x["host"], x["source"]) for x in r], [("127.0.0.1", "this computer")])
        finally:
            s.close()

    def test_no_scan_unless_asked(self):
        remote = FakeServer("127.0.0.2")  # another "device"; nothing on 127.0.0.1 at that port
        msgs = []
        try:
            self.assertEqual(find.find_playback(remote.port, scan=False, subnets=["127.0.0.0/29"], log=msgs.append), [])
            self.assertEqual(msgs, [])
        finally:
            remote.close()

    def test_scan_finds_other_device_and_skips_impostors(self):
        remote = FakeServer("127.0.0.2")
        imp = FakeServer("127.0.0.3", "http", port=remote.port)
        imp2 = FakeServer("127.0.0.4", "ws", port=remote.port)
        try:
            r = find.find_playback(remote.port, scan=True, subnets=["127.0.0.0/29"])
            self.assertEqual([(x["host"], x["source"]) for x in r], [("127.0.0.2", "network scan")])
            # remembered: the next run finds it without scanning
            r2 = find.find_playback(remote.port, scan=True, subnets=["10.255.255.0/30"])
            self.assertEqual([(x["host"], x["source"]) for x in r2], [("127.0.0.2", "last known host")])
        finally:
            for x in (remote, imp, imp2):
                x.close()

    def test_find_all(self):
        a = FakeServer("127.0.0.2")
        b = FakeServer("127.0.0.5", port=a.port)
        try:
            r = find.find_playback(a.port, scan=True, find_all=True, subnets=["127.0.0.0/29"])
            self.assertEqual(sorted(x["host"] for x in r), ["127.0.0.2", "127.0.0.5"])
        finally:
            a.close(); b.close()


class Networks(unittest.TestCase):
    LINUX = "1: lo    inet 127.0.0.1/8 scope host lo\n2: eth0    inet 192.168.4.21/24 brd 192.168.4.255 scope global eth0\n" \
            "3: tun0    inet 10.8.0.2/32 scope global tun0\n4: docker0    inet 172.17.0.1/16 scope global docker0"
    MAC = "lo0: inet 127.0.0.1 netmask 0xff000000\nen0: inet 192.168.1.50 netmask 0xffffff00 broadcast 192.168.1.255\n" \
          "en1: inet 169.254.7.7 netmask 0xffff0000\nutun3: inet 100.64.0.5 --> 100.64.0.5 netmask 0xffffffff"

    def test_linux(self):
        nets = [str(n) for n in find.usable_networks(find.parse_interfaces(self.LINUX))]
        self.assertEqual(nets, ["192.168.4.0/24", "172.17.0.0/24"])  # loopback and /32 tunnel dropped; /16 narrowed

    def test_mac(self):
        nets = [str(n) for n in find.usable_networks(find.parse_interfaces(self.MAC))]
        self.assertEqual(nets, ["192.168.1.0/24"])  # loopback, link-local and point-to-point dropped


class Cli(unittest.TestCase):
    def test_flags_before_or_after_command(self):
        import contextlib, io
        from playback_api import cli
        for argv in (["--port", "9", "find"], ["find", "--port", "9"], ["--port", "9", "find", "--scan"],
                     ["find", "--scan", "--subnet", "127.0.0.0/30", "--port", "9"]):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(argv), 2, argv)  # parsed fine; nothing is listening on port 9

    def test_state_without_host_says_how_to_scan(self):
        import contextlib, io
        from playback_api import cli
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            cli.main(["state", "--port", "9"])
        self.assertIn("--scan", err.getvalue())


if __name__ == "__main__":
    unittest.main()
