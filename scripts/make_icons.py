"""Draw the PWA icons (SPEC §7): a puck on the brand blue, stdlib only.

    uv run python -m scripts.make_icons

Writes ``src/fha/web/static/icons/{icon-192,icon-512,apple-touch-icon}.png``.
A tiny PNG writer (zlib + struct) keeps image libraries out of the project.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "src" / "fha" / "web" / "static" / "icons"
SIZES = {"icon-192.png": 192, "icon-512.png": 512, "apple-touch-icon.png": 180}
BLUE = (11, 61, 145)
WHITE = (255, 255, 255)
BLACK = (27, 31, 36)
GREY = (70, 76, 84)


def png(width: int, height: int, rows: list[bytes]) -> bytes:
    """An 8-bit RGB PNG from ``rows`` of ``width * 3`` bytes."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    raw = b"".join(b"\x00" + row for row in rows)  # filter type 0 per row
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def puck(size: int) -> bytes:
    """A black puck (an ellipse with a grey rim band) centred on blue, full bleed."""
    cx, cy = size / 2, size * 0.54
    rx, ry = size * 0.34, size * 0.17
    top = cy - size * 0.09  # the puck's top face is a second ellipse, raised
    rows = []
    for y in range(size):
        row = bytearray()
        for x in range(size):
            dx = (x + 0.5 - cx) / rx
            face = (dx * dx + ((y + 0.5 - top) / ry) ** 2) <= 1
            side = (dx * dx + ((y + 0.5 - cy) / ry) ** 2) <= 1 or (abs(dx) <= 1 and top <= y <= cy)
            colour = BLUE
            if side:
                colour = GREY
            if face:
                colour = BLACK
            if face and ((dx * dx + ((y + 0.5 - top) / ry) ** 2) > 0.8):
                colour = WHITE if y < top else colour  # a highlight on the rim's far edge
            row.extend(colour)
        rows.append(bytes(row))
    return png(size, size, rows)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, size in SIZES.items():
        (OUT / name).write_bytes(puck(size))
        print(f"wrote {OUT / name} ({size}x{size})")


if __name__ == "__main__":
    main()
