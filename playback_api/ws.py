"""Minimal dependency-free RFC 6455 WebSocket client (text frames only)."""
from __future__ import annotations

import base64
import os
import socket
import struct

SUBPROTOCOL = "pr-protocol"


class WSClosed(Exception):
    pass


class WSHandshakeError(Exception):
    pass


class WebSocket:
    def __init__(self, host: str, port: int = 8080, subprotocol: str = SUBPROTOCOL, timeout: float = 5.0):
        self.host, self.port = host, port
        self.sock = socket.create_connection((host, port), timeout=timeout)
        try:
            self._handshake(host, port, subprotocol, timeout)
        except BaseException:
            self.sock.close()
            raise

    def _handshake(self, host: str, port: int, subprotocol: str, timeout: float) -> None:
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET / HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
            f"Sec-WebSocket-Protocol: {subprotocol}\r\n\r\n"
        )
        self.sock.sendall(req.encode())
        self.sock.settimeout(timeout)
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise WSHandshakeError("server closed during handshake (is 'Allow Remote Connections' on?)")
            resp += chunk
        head, _, self._buf = resp.partition(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n", 1)[0] + b" ":
            raise WSHandshakeError(head.split(b"\r\n", 1)[0].decode(errors="replace"))

    def _read(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise WSClosed("connection closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def recv(self, timeout: float | None = None) -> str | None:
        """Return the next text message, or None on timeout. Raises WSClosed."""
        self.sock.settimeout(timeout)
        parts: list[bytes] = []
        try:
            while True:
                b0, b1 = self._read(2)
                op, fin, n = b0 & 15, b0 & 0x80, b1 & 127
                if n == 126:
                    n = struct.unpack(">H", self._read(2))[0]
                elif n == 127:
                    n = struct.unpack(">Q", self._read(8))[0]
                mask = self._read(4) if b1 & 0x80 else None
                data = self._read(n) if n else b""
                if mask:
                    data = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
                if op == 8:
                    raise WSClosed("close frame")
                if op == 9:
                    self._send(10, data)
                    continue
                if op == 10:
                    continue
                if op in (0, 1):
                    parts.append(data)
                    if fin:
                        return b"".join(parts).decode("utf-8", "replace")
        except socket.timeout:
            return None

    def _send(self, op: int, data: bytes) -> None:
        m, n = os.urandom(4), len(data)
        if n < 126:
            hdr = bytes([0x80 | op, 0x80 | n])
        elif n < 65536:
            hdr = bytes([0x80 | op, 0x80 | 126]) + struct.pack(">H", n)
        else:
            hdr = bytes([0x80 | op, 0x80 | 127]) + struct.pack(">Q", n)
        self.sock.settimeout(5)
        self.sock.sendall(hdr + m + bytes(c ^ m[i % 4] for i, c in enumerate(data)))

    def send(self, text: str) -> None:
        self._send(1, text.encode())

    def close(self) -> None:
        try:
            self._send(8, b"")
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass
