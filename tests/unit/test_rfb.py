"""RFB protocol and framebuffer conversion."""

from __future__ import annotations

import socket
import struct
import threading
import zlib

from openqvm.vm.rfb import RfbClient, keysym_for_char, write_png


class FakeRfbServer(threading.Thread):
    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(1)
        self.port = self.socket.getsockname()[1]
        self.received: list[bytes] = []
        self.error: Exception | None = None

    def run(self) -> None:
        try:
            connection, _ = self.socket.accept()
            with connection:
                connection.sendall(b"RFB 003.008\n")
                self.received.append(self._exact(connection, 12))
                connection.sendall(bytes([1, 1]))  # one security type: None
                self.received.append(self._exact(connection, 1))
                connection.sendall(struct.pack(">I", 0))
                self.received.append(self._exact(connection, 1))  # ClientInit
                pixel_format = struct.pack(
                    ">BBBBHHHBBB3x", 32, 24, 0, 1, 255, 255, 255, 16, 8, 0
                )
                name = b"Fake Display"
                connection.sendall(
                    struct.pack(">HH", 2, 2) + pixel_format + struct.pack(">I", len(name)) + name
                )
                self.received.append(self._exact(connection, 4 + 4 * 1))  # SetEncodings header + one
                self.received.append(self._exact(connection, 10))  # FramebufferUpdateRequest
                pixels = (
                    bytes([255, 0, 0, 0])      # blue
                    + bytes([0, 255, 0, 0])    # green
                    + bytes([0, 0, 255, 0])    # red
                    + bytes([255, 255, 255, 0])  # white
                )
                update = struct.pack(">BBH", 0, 0, 1) + struct.pack(">HHHHi", 0, 0, 2, 2, 0) + pixels
                connection.sendall(update)
                self.received.append(self._exact(connection, 8))  # key event
        except Exception as exc:  # pragma: no cover - helper
            self.error = exc

    def _exact(self, connection, count: int) -> bytes:
        data = b""
        while len(data) < count:
            chunk = connection.recv(count - len(data))
            if not chunk:
                break
            data += chunk
        return data

    def close(self) -> None:
        try:
            self.socket.close()
        except OSError:
            pass


def test_grab_frame_and_key_events():
    server = FakeRfbServer()
    server.start()
    client = RfbClient("127.0.0.1", server.port, timeout=5)
    try:
        client.connect()
        frame = client.grab_frame()
        client.send_key(0x41, True)
    finally:
        client.close()
    server.close()
    server.join(timeout=2)
    assert server.error is None
    assert frame.width == 2 and frame.height == 2
    pixels = [tuple(frame.rgb[index : index + 3]) for index in range(0, 12, 3)]
    assert pixels == [(0, 0, 255), (0, 255, 0), (255, 0, 0), (255, 255, 255)]


def test_keysyms():
    assert keysym_for_char("A") == ord("A")
    assert keysym_for_char("\n") == 0xFF0D
    assert keysym_for_char("é") == 0xE9
    assert keysym_for_char("😀") is None


def test_text_typing_uses_us_layout():
    from openqvm.vm.rfb import keysym_for_text_char

    assert keysym_for_text_char("a") == (ord("a"), False)
    assert keysym_for_text_char("A") == (ord("A"), False)
    assert keysym_for_text_char("7") == (ord("7"), False)
    assert keysym_for_text_char("!") == (ord("1"), True)
    assert keysym_for_text_char("~") == (ord("`"), True)
    assert keysym_for_text_char("_") == (ord("-"), True)
    assert keysym_for_text_char("{") == (ord("["), True)
    assert keysym_for_text_char("|") == (ord("\\"), True)
    assert keysym_for_text_char("=") == (ord("="), False)
    assert keysym_for_text_char("\n") == (0xFF0D, False)
    assert keysym_for_text_char("\t") == (0xFF09, False)
    assert keysym_for_text_char("é") == (0xE9, False)
    assert keysym_for_text_char("😀") is None
    assert keysym_for_text_char("ab") is None


def test_write_png(tmp_path):
    from openqvm.vm.rfb import FrameBuffer

    path = tmp_path / "frame.png"
    write_png(str(path), FrameBuffer(width=2, height=1, rgb=bytes([255, 0, 0, 0, 255, 0])))
    data = path.read_bytes()
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    assert data[12:16] == b"IHDR"
    width, height = struct.unpack(">II", data[16:24])
    assert (width, height) == (2, 1)
    assert zlib.crc32(data[12:16] + data[16:29]) & 0xFFFFFFFF == struct.unpack(">I", data[29:33])[0]
