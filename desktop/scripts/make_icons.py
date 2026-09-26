"""Generate Dobot's app icons without any image library.

Tauri validates that ``bundle.icon`` paths exist at build time, so this keeps the repository
self-contained: run ``python scripts/make_icons.py`` from ``desktop/`` to regenerate the PNG/ICO set.

The mark: Dobot's dot — a blue sphere with a soft highlight, on transparency.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "src-tauri" / "icons"
SIZES = {"32x32.png": 32, "128x128.png": 128, "128x128@2x.png": 256, "icon.png": 512}


def _pixel(x: int, y: int, size: int) -> tuple[int, int, int, int]:
    """Return RGBA for a pixel: a shaded sphere with an anti-aliased edge."""
    cx = cy = (size - 1) / 2
    radius = size * 0.44
    dx, dy = x - cx, y - cy
    distance = (dx * dx + dy * dy) ** 0.5
    if distance > radius + 1:
        return (0, 0, 0, 0)

    # Base colour, lit from the upper-left.
    light = max(0.0, 1.0 - ((dx + radius * 0.45) ** 2 + (dy + radius * 0.45) ** 2) ** 0.5 / (radius * 2.1))
    red = int(40 + 120 * light)
    green = int(110 + 120 * light)
    blue = int(200 + 55 * light)
    alpha = 255 if distance <= radius else int(255 * (radius + 1 - distance))
    return (min(red, 255), min(green, 255), min(blue, 255), max(alpha, 0))


def _png(size: int) -> bytes:
    raw = bytearray()
    for y in range(size):
        raw.append(0)  # filter type: none
        for x in range(size):
            raw.extend(_pixel(x, y, size))

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


def _ico(png: bytes, size: int = 256) -> bytes:
    """ICO container holding a single PNG frame (supported on Windows Vista and later)."""
    header = struct.pack("<HHH", 0, 1, 1)
    dimension = 0 if size >= 256 else size
    entry = struct.pack("<BBBBHHII", dimension, dimension, 0, 0, 1, 32, len(png), 22)
    return header + entry + png


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, size in SIZES.items():
        (OUT / name).write_bytes(_png(size))
        print("wrote", OUT / name)
    (OUT / "icon.ico").write_bytes(_ico(_png(256)))
    print("wrote", OUT / "icon.ico")


if __name__ == "__main__":
    main()
