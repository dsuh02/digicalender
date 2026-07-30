"""
Minimal RFC 6455 WebSocket client, stdlib only.

Exists because the Samsung Tizen remote protocol is WebSocket-only and the
usual answer (`websocket-client` from pip) would break this project's no-pip
property. Scope is deliberately small: connect, send text, receive text, close.
No extensions, no continuation frames beyond reassembly, no async.

Samsung's TV presents a self-signed certificate on :8002, so verification is
off by default. That is safe here in a way it wouldn't be on the internet: the
connection is to a fixed LAN IP, and the only secret exchanged is a pairing
token that authorises volume changes on a television.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import ssl
import struct
from urllib.parse import urlparse

OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


class WSError(Exception):
    pass


class WebSocket:
    def __init__(self, url: str, timeout: float = 8.0, verify: bool = False,
                 origin: str | None = None):
        self.url = url
        self.timeout = timeout
        self._buf = b""
        u = urlparse(url)
        secure = u.scheme == "wss"
        host = u.hostname or ""
        port = u.port or (443 if secure else 80)
        path = u.path or "/"
        if u.query:
            path += "?" + u.query

        self.sock = socket.create_connection((host, port), timeout=timeout)
        if secure:
            ctx = ssl.create_default_context()
            if not verify:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            self.sock = ctx.wrap_socket(self.sock, server_hostname=host)

        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
        )
        if origin:
            req += f"Origin: {origin}\r\n"
        req += "\r\n"
        self.sock.sendall(req.encode())

        header = self._read_until(b"\r\n\r\n")
        status = header.split(b"\r\n", 1)[0].decode(errors="replace")
        if "101" not in status:
            self.close()
            raise WSError(f"handshake refused: {status}")

    # ------------------------------------------------------------ internals

    def _read_until(self, marker: bytes) -> bytes:
        while marker not in self._buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise WSError("connection closed during handshake")
            self._buf += chunk
        head, self._buf = self._buf.split(marker, 1)
        return head + marker

    def _recv_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(max(4096, n - len(self._buf)))
            if not chunk:
                raise WSError("connection closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        head = bytearray([0x80 | opcode])
        n = len(payload)
        # Client frames MUST be masked (RFC 6455 §5.3); servers drop them otherwise.
        if n < 126:
            head.append(0x80 | n)
        elif n < (1 << 16):
            head.append(0x80 | 126)
            head += struct.pack(">H", n)
        else:
            head.append(0x80 | 127)
            head += struct.pack(">Q", n)
        mask = os.urandom(4)
        head += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(head) + masked)

    def _recv_frame(self) -> tuple[int, bytes]:
        b0, b1 = self._recv_exact(2)
        fin = b0 & 0x80
        opcode = b0 & 0x0F
        masked = b1 & 0x80
        length = b1 & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._recv_exact(8))[0]
        if length > 8 << 20:
            raise WSError("frame too large")
        mask = self._recv_exact(4) if masked else b""
        payload = self._recv_exact(length) if length else b""
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        if not fin:
            # Reassemble continuations; Samsung never sends them, but a partial
            # message silently treated as whole would be a nasty bug.
            _, rest = self._recv_frame()
            payload += rest
        return opcode, payload

    # --------------------------------------------------------------- public

    def send(self, text: str) -> None:
        self._send_frame(OP_TEXT, text.encode())

    def send_json(self, obj) -> None:
        self.send(json.dumps(obj))

    def recv(self) -> str | None:
        """Next text message. Answers pings transparently; None on close."""
        while True:
            opcode, payload = self._recv_frame()
            if opcode == OP_TEXT:
                return payload.decode(errors="replace")
            if opcode == OP_PING:
                self._send_frame(OP_PONG, payload)
                continue
            if opcode == OP_CLOSE:
                return None
            # binary/pong: not used by this protocol, keep waiting

    def recv_json(self):
        raw = self.recv()
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return None

    def close(self) -> None:
        try:
            self._send_frame(OP_CLOSE, b"")
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
