"""Minimal RFB (VNC) client used for the embedded guest display.

QEMU exposes its display as a VNC server bound to loopback.  OpenQVM speaks just
enough RFB 3.8 (security type None, Raw encoding) to render the guest framebuffer
inside the application and to forward keyboard and pointer input.

The module has no Qt dependency, which makes the protocol testable headlessly and
allows the end-to-end test to save a real screenshot of a booted guest.
"""

from __future__ import annotations

import socket
import struct
import threading
import time
import zlib
from dataclasses import dataclass


class RfbError(Exception):
    pass


@dataclass(frozen=True)
class PixelFormat:
    bits_per_pixel: int
    depth: int
    big_endian: bool
    true_colour: bool
    red_max: int
    green_max: int
    blue_max: int
    red_shift: int
    green_shift: int
    blue_shift: int

    @property
    def bytes_per_pixel(self) -> int:
        return self.bits_per_pixel // 8


@dataclass(frozen=True)
class Rectangle:
    x: int
    y: int
    width: int
    height: int
    rgb: bytes


@dataclass(frozen=True)
class FrameBuffer:
    width: int
    height: int
    rgb: bytes
    name: str = ""


@dataclass(frozen=True)
class ServerCutText:
    text: str


class RfbClient:
    VERSION = b"RFB 003.008\n"

    def __init__(self, host: str, port: int, *, timeout: float = 15.0) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self._socket: socket.socket | None = None
        self.width = 0
        self.height = 0
        self.pixel_format: PixelFormat | None = None
        self.desktop_name = ""
        self._canvas = bytearray()
        self._send_lock = threading.Lock()

    # -- connection -----------------------------------------------------------

    def connect(self, *, timeout: float | None = None) -> None:
        limit = timeout if timeout is not None else self.timeout
        self._socket = socket.create_connection((self.host, self.port), timeout=limit)
        self._socket.settimeout(limit)
        version = self._read_exact(12)
        if not version.startswith(b"RFB "):
            self.close()
            raise RfbError("The VNC endpoint did not send an RFB greeting.")
        self._send(self.VERSION)
        count = self._read_exact(1)[0]
        if count == 0:
            length = struct.unpack(">I", self._read_exact(4))[0]
            reason = self._read_exact(length).decode("utf-8", errors="replace")
            self.close()
            raise RfbError(f"The VNC server refused the connection: {reason}")
        types = self._read_exact(count)
        if 1 not in types:
            self.close()
            raise RfbError(
                "The VNC server requires authentication. OpenQVM uses loopback-only VNC without a password."
            )
        self._send(b"\x01")  # security type None
        result = struct.unpack(">I", self._read_exact(4))[0]
        if result != 0:
            self.close()
            raise RfbError("The VNC security handshake failed.")
        self._send(b"\x01")  # shared session
        header = self._read_exact(4)
        self.width, self.height = struct.unpack(">HH", header)
        raw_format = self._read_exact(16)
        (
            bits_per_pixel,
            depth,
            big_endian,
            true_colour,
            red_max,
            green_max,
            blue_max,
            red_shift,
            green_shift,
            blue_shift,
        ) = struct.unpack(">BBBBHHHBBB3x", raw_format)
        self.pixel_format = PixelFormat(
            bits_per_pixel=bits_per_pixel,
            depth=depth,
            big_endian=bool(big_endian),
            true_colour=bool(true_colour),
            red_max=red_max,
            green_max=green_max,
            blue_max=blue_max,
            red_shift=red_shift,
            green_shift=green_shift,
            blue_shift=blue_shift,
        )
        if not self.pixel_format.true_colour:
            self.close()
            raise RfbError("The VNC server uses an indexed colour map, which is not supported.")
        name_length = struct.unpack(">I", self._read_exact(4))[0]
        self.desktop_name = self._read_exact(name_length).decode("utf-8", errors="replace")
        self._canvas = bytearray(self.width * self.height * 3)
        self.set_encodings([0])

    def close(self) -> None:
        if self._socket is not None:
            try:
                self._socket.close()
            except OSError:
                pass
            self._socket = None

    def __enter__(self) -> RfbClient:
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        self.close()

    # -- messages -------------------------------------------------------------

    def set_encodings(self, encodings: list[int]) -> None:
        payload = struct.pack(">BBH", 2, 0, len(encodings)) + b"".join(
            struct.pack(">i", encoding) for encoding in encodings
        )
        self._send(payload)

    def request_update(self, *, incremental: bool = True) -> None:
        self._send(struct.pack(">BBHHHH", 3, 1 if incremental else 0, 0, 0, self.width, self.height))

    def framebuffer_update(self, *, incremental: bool = False) -> list[Rectangle]:
        self.request_update(incremental=incremental)
        while True:
            message_type = self._read_exact(1)[0]
            if message_type == 0:
                rectangles = self._read_framebuffer_update()
                self._apply(rectangles)
                return rectangles
            if message_type == 1:
                self._read_colour_map()
                continue
            if message_type == 2:
                continue
            if message_type == 3:
                self._read_server_cut_text()
                continue
            raise RfbError(f"Unsupported RFB message type {message_type}.")

    def read_message(self) -> list[Rectangle] | ServerCutText | None:
        """Read one server message; framebuffer updates are applied to the canvas."""

        message_type = self._read_exact(1)[0]
        if message_type == 0:
            rectangles = self._read_framebuffer_update()
            self._apply(rectangles)
            return rectangles
        if message_type == 1:
            self._read_colour_map()
            return None
        if message_type == 2:
            return None
        if message_type == 3:
            return self._read_server_cut_text()
        raise RfbError(f"Unsupported RFB message type {message_type}.")

    def _read_framebuffer_update(self) -> list[Rectangle]:
        self._read_exact(1)  # padding
        count = struct.unpack(">H", self._read_exact(2))[0]
        if self.pixel_format is None:
            raise RfbError("The RFB session is not initialized.")
        rectangles: list[Rectangle] = []
        for _ in range(count):
            x, y, width, height, encoding = struct.unpack(">HHHHi", self._read_exact(12))
            if encoding != 0:
                raise RfbError(f"Unsupported RFB rectangle encoding {encoding}.")
            pixel_bytes = self.pixel_format.bytes_per_pixel
            data = self._read_exact(width * height * pixel_bytes)
            rectangles.append(Rectangle(x, y, width, height, self._to_rgb(data, width, height)))
        return rectangles

    def _read_colour_map(self) -> None:
        self._read_exact(1)
        count = struct.unpack(">H", self._read_exact(2))[0]
        self._read_exact(count * 6)

    def _read_server_cut_text(self) -> ServerCutText:
        self._read_exact(3)
        length = struct.unpack(">I", self._read_exact(4))[0]
        text = self._read_exact(length).decode("latin-1", errors="replace")
        return ServerCutText(text)

    def send_pointer(self, button_mask: int, x: int, y: int) -> None:
        x = max(0, min(int(x), max(0, self.width - 1)))
        y = max(0, min(int(y), max(0, self.height - 1)))
        self._send(struct.pack(">BBHH", 5, button_mask & 0xFF, x, y))

    def send_key(self, keysym: int, down: bool) -> None:
        self._send(struct.pack(">BBHi", 4, 1 if down else 0, 0, keysym))

    def send_cut_text(self, text: str) -> None:
        data = text.encode("latin-1", errors="replace")
        self._send(struct.pack(">BxxxI", 6, len(data)) + data)

    # -- canvas ---------------------------------------------------------------

    def _apply(self, rectangles: list[Rectangle]) -> None:
        for rect in rectangles:
            if rect.x + rect.width > self.width or rect.y + rect.height > self.height:
                raise RfbError("The VNC server sent a rectangle outside the framebuffer.")
            row_bytes = rect.width * 3
            for row in range(rect.height):
                source = row * row_bytes
                target = ((rect.y + row) * self.width + rect.x) * 3
                self._canvas[target : target + row_bytes] = rect.rgb[source : source + row_bytes]

    @property
    def canvas(self) -> bytes:
        return bytes(self._canvas)

    def snapshot(self) -> FrameBuffer:
        """Return the last fully updated framebuffer."""

        return FrameBuffer(
            width=self.width,
            height=self.height,
            rgb=self.canvas,
            name=self.desktop_name,
        )

    def grab_frame(self, *, timeout: float | None = None) -> FrameBuffer:
        """Request a full update and return a complete frame (used for screenshots)."""

        if self._socket is None:
            self.connect(timeout=timeout)
        self.framebuffer_update(incremental=False)
        return self.snapshot()

    # -- pixel conversion -----------------------------------------------------

    def _to_rgb(self, data: bytes, width: int, height: int) -> bytes:
        fmt = self.pixel_format
        assert fmt is not None
        if fmt.bits_per_pixel == 32 and fmt.red_max == 255 and fmt.green_max == 255 and fmt.blue_max == 255:
            if fmt.red_shift == 16 and fmt.green_shift == 8 and fmt.blue_shift == 0 and not fmt.big_endian:
                # QEMU's native BGRX layout: copy planes instead of looping per pixel.
                rgb = bytearray(width * height * 3)
                rgb[0::3] = data[2::4]
                rgb[1::3] = data[1::4]
                rgb[2::3] = data[0::4]
                return bytes(rgb)
            if fmt.red_shift == 0 and fmt.green_shift == 8 and fmt.blue_shift == 16 and not fmt.big_endian:
                rgb = bytearray(width * height * 3)
                rgb[0::3] = data[0::4]
                rgb[1::3] = data[1::4]
                rgb[2::3] = data[2::4]
                return bytes(rgb)

        pixel_count = width * height
        pixel_bytes = fmt.bytes_per_pixel
        rgb = bytearray(pixel_count * 3)
        byteorder = "big" if fmt.big_endian else "little"

        def scale(value: int, maximum: int) -> int:
            if maximum <= 0:
                return 0
            return (value * 255 + maximum // 2) // maximum

        for index in range(pixel_count):
            raw = data[index * pixel_bytes : (index + 1) * pixel_bytes]
            value = int.from_bytes(raw, byteorder)
            rgb[index * 3] = scale((value >> fmt.red_shift) & fmt.red_max, fmt.red_max)
            rgb[index * 3 + 1] = scale((value >> fmt.green_shift) & fmt.green_max, fmt.green_max)
            rgb[index * 3 + 2] = scale((value >> fmt.blue_shift) & fmt.blue_max, fmt.blue_max)
        return bytes(rgb)

    # -- socket helpers -------------------------------------------------------

    def _send(self, payload: bytes) -> None:
        if self._socket is None:
            raise RfbError("The RFB client is not connected.")
        try:
            with self._send_lock:
                if self._socket is None:
                    raise RfbError("The RFB client is not connected.")
                self._socket.sendall(payload)
        except OSError as exc:
            raise RfbError(f"The VNC connection was lost: {exc}") from exc

    def _read_exact(self, count: int) -> bytes:
        if self._socket is None:
            raise RfbError("The RFB client is not connected.")
        chunks = bytearray()
        deadline = time.monotonic() + self.timeout
        while len(chunks) < count:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RfbError("Timed out waiting for the VNC server.")
            self._socket.settimeout(max(0.1, min(5.0, remaining)))
            try:
                chunk = self._socket.recv(count - len(chunks))
            except TimeoutError:
                continue
            except OSError as exc:
                raise RfbError(f"The VNC connection was lost: {exc}") from exc
            if not chunk:
                raise RfbError("The VNC server closed the connection.")
            chunks.extend(chunk)
        return bytes(chunks)


# -- X11 keysyms --------------------------------------------------------------

SPECIAL_KEYSYMS: dict[str, int] = {
    "escape": 0xFF1B,
    "tab": 0xFF09,
    "backspace": 0xFF08,
    "return": 0xFF0D,
    "enter": 0xFF0D,
    "insert": 0xFF63,
    "delete": 0xFFFF,
    "home": 0xFF50,
    "end": 0xFF57,
    "pageup": 0xFF55,
    "pagedown": 0xFF56,
    "left": 0xFF51,
    "up": 0xFF52,
    "right": 0xFF53,
    "down": 0xFF54,
    "shift": 0xFFE1,
    "control": 0xFFE3,
    "alt": 0xFFE9,
    "meta": 0xFFEB,
    "super": 0xFFEB,
    "capslock": 0xFFE5,
    "pause": 0xFF13,
    "print": 0xFF61,
}
for _index in range(1, 13):
    SPECIAL_KEYSYMS[f"f{_index}"] = 0xFFBE + _index - 1


def keysym_for_char(character: str) -> int | None:
    if len(character) != 1:
        return None
    code = ord(character)
    if 0x20 <= code <= 0x7E:
        return code
    if code == 0x0A:
        return 0xFF0D
    if code == 0x09:
        return 0xFF09
    # Latin-1 characters use their Unicode code point as the keysym.
    if 0xA0 <= code <= 0xFF:
        return code
    return None


# Characters that require Shift on a US keyboard, mapped to their base keysym.
_SHIFTED_CHARACTERS = {
    "!": "1",
    "@": "2",
    "#": "3",
    "$": "4",
    "%": "5",
    "^": "6",
    "&": "7",
    "*": "8",
    "(": "9",
    ")": "0",
    "_": "-",
    "+": "=",
    "{": "[",
    "}": "]",
    "|": "\\",
    ":": ";",
    '"': "'",
    "<": ",",
    ">": ".",
    "?": "/",
    "~": "`",
}


def keysym_for_text_char(character: str) -> tuple[int, bool] | None:
    """Map a character to ``(keysym, needs_shift)`` for typing into a guest.

    The mapping assumes a US keyboard layout, which is the layout QEMU's VNC
    server uses for keysym translation.  Returns ``None`` for characters that
    cannot be produced this way.
    """

    if len(character) != 1:
        return None
    if character == "\n" or character == "\r":
        return SPECIAL_KEYSYMS["return"], False
    if character == "\t":
        return SPECIAL_KEYSYMS["tab"], False

    shifted = _SHIFTED_CHARACTERS.get(character)
    if shifted is not None:
        return ord(shifted), True

    if "a" <= character <= "z" or "0" <= character <= "9":
        return ord(character), False
    if "A" <= character <= "Z":
        # QEMU's VNC server maps letter keysyms to their shifted keycode, so the
        # uppercase keysym must be sent as-is; adding Shift would cancel it.
        return ord(character), False
    if character == " ":
        return ord(" "), False
    if character in "-=[]\\;',./`":
        return ord(character), False
    if 0xA0 <= ord(character) <= 0xFF:
        return ord(character), False
    return None


def type_text(sender, text: str) -> tuple[int, int]:
    """Type *text* through any object exposing ``send_key(keysym, down)``.

    Returns ``(typed, unsupported)`` counts.  Used by the console widget and by
    the end-to-end test so both exercise the same key mapping.
    """

    shift = SPECIAL_KEYSYMS["shift"]
    typed = 0
    unsupported = 0
    for character in text:
        mapping = keysym_for_text_char(character)
        if mapping is None:
            unsupported += 1
            continue
        keysym, needs_shift = mapping
        if needs_shift:
            sender.send_key(shift, True)
        sender.send_key(keysym, True)
        sender.send_key(keysym, False)
        if needs_shift:
            sender.send_key(shift, False)
        typed += 1
    return typed, unsupported


def write_png(path: str, frame: FrameBuffer) -> None:
    """Write an RGB framebuffer as a PNG without external dependencies."""

    import os

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    stride = frame.width * 3
    raw = bytearray()
    for row in range(frame.height):
        raw.append(0)
        raw.extend(frame.rgb[row * stride : (row + 1) * stride])
    header = struct.pack(">IIBBBBB", frame.width, frame.height, 8, 2, 0, 0, 0)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + chunk(b"IEND", b"")
    )
    with open(os.fspath(path), "wb") as handle:
        handle.write(png)
