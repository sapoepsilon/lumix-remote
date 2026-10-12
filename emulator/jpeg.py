"""Tiny pure-Python baseline JPEG encoder (no Pillow needed).

Every 8x8 block is a flat colour: only the DC coefficient is coded and every
AC run is an immediate EOB. That keeps encoding a 640x360 frame to a few
milliseconds while still producing a real, decodable JFIF-less baseline JPEG
(SOI, DQT, SOF0, DHT, SOS, EOI) like the S5's live view frames, which also
start with SOI + DQT.

Images are described as a grid of RGB cells, one cell per 8x8 block.
"""

from __future__ import annotations

import struct
from typing import Dict, List, Optional, Sequence, Tuple

RGB = Tuple[int, int, int]

# Standard (Annex K) DC Huffman tables: code lengths per category 0..11.
_DC_LUMA_BITS = [0, 1, 5, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0]
_DC_CHROMA_BITS = [0, 3, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0]
_DC_VALS = list(range(12))
# AC table with a single symbol (EOB = 0x00) coded as "0".
_AC_BITS = [1] + [0] * 15
_AC_VALS = [0x00]


def _huff_codes(bits: Sequence[int], vals: Sequence[int]) -> Dict[int, str]:
    codes: Dict[int, str] = {}
    code = 0
    k = 0
    for length in range(1, 17):
        for _ in range(bits[length - 1]):
            codes[vals[k]] = format(code, "0%db" % length)
            code += 1
            k += 1
        code <<= 1
    return codes


def _dc_table(bits: Sequence[int]) -> Dict[int, str]:
    """diff -> huffman(category) + magnitude bits + EOB('0')."""
    codes = _huff_codes(bits, _DC_VALS)
    out: Dict[int, str] = {}
    for diff in range(-510, 511):
        a = abs(diff)
        cat = a.bit_length()
        if cat == 0:
            mag = ""
        elif diff > 0:
            mag = format(diff, "0%db" % cat)
        else:
            mag = format((diff - 1) & ((1 << cat) - 1), "0%db" % cat)
        out[diff] = codes[cat] + mag + "0"
    return out


_DC_Y = _dc_table(_DC_LUMA_BITS)
_DC_C = _dc_table(_DC_CHROMA_BITS)


def _segment(marker: int, payload: bytes) -> bytes:
    return struct.pack(">HH", 0xFF00 | marker, len(payload) + 2) + payload


def _dqt() -> bytes:
    # DC quantiser 8 -> quantised DC == (block mean - 128). AC values are irrelevant.
    t = bytes([8] + [16] * 63)
    return _segment(0xDB, b"\x00" + t + b"\x01" + t)


def _dht() -> bytes:
    p = b""
    for tc_th, bits, vals in ((0x00, _DC_LUMA_BITS, _DC_VALS), (0x10, _AC_BITS, _AC_VALS),
                              (0x01, _DC_CHROMA_BITS, _DC_VALS), (0x11, _AC_BITS, _AC_VALS)):
        p += bytes([tc_th]) + bytes(bits) + bytes(vals)
    return _segment(0xC4, p)


_DQT = _dqt()
_DHT = _dht()


def _ycc(c: RGB) -> Tuple[int, int, int]:
    r, g, b = c
    y = 0.299 * r + 0.587 * g + 0.114 * b - 128
    cb = -0.168736 * r - 0.331264 * g + 0.5 * b
    cr = 0.5 * r - 0.418688 * g - 0.081312 * b
    return (max(-128, min(127, int(round(y)))), max(-128, min(127, int(round(cb)))),
            max(-128, min(127, int(round(cr)))))


def encode_cells(cells: List[List[RGB]], comment: bytes = b"", pad_to: int = 0) -> bytes:
    """Encode a grid (rows of RGB cells, each cell = one 8x8 block) as baseline JPEG.

    ``comment`` goes in a COM segment; ``pad_to`` grows that COM segment so the
    whole file is about that many bytes (live view frames are 22-31 KB)."""
    h = len(cells)
    w = len(cells[0]) if h else 0
    bits: List[str] = []
    py = pcb = pcr = 0
    cache: Dict[RGB, Tuple[int, int, int]] = {}
    for row in cells:
        for c in row:
            ycc = cache.get(c)
            if ycc is None:
                ycc = cache[c] = _ycc(c)
            y, cb, cr = ycc
            bits.append(_DC_Y[y - py])
            bits.append(_DC_C[cb - pcb])
            bits.append(_DC_C[cr - pcr])
            py, pcb, pcr = y, cb, cr
    s = "".join(bits)
    s += "1" * (-len(s) % 8)
    scan = int(s, 2).to_bytes(len(s) // 8, "big") if s else b""
    scan = scan.replace(b"\xff", b"\xff\x00")
    sof = _segment(0xC0, struct.pack(">BHHB", 8, h * 8, w * 8, 3)
                   + b"\x01\x11\x00" + b"\x02\x11\x01" + b"\x03\x11\x01")
    sos = _segment(0xDA, b"\x03" + b"\x01\x00" + b"\x02\x11" + b"\x03\x11" + b"\x00\x3f\x00")
    body = _DQT + sof + _DHT + sos + scan + b"\xff\xd9"
    com = b""
    if comment or pad_to:
        need = max(0, pad_to - (2 + len(body) + 4 + len(comment)))
        payload = comment + b" " * min(need, 65533 - len(comment))
        com = _segment(0xFE, payload[:65533])
    return b"\xff\xd8" + body[:len(_DQT)] + com + body[len(_DQT):]


# ---------------------------------------------------------------- drawing
_FONT = {  # 3x5 digits
    "0": ["111", "101", "101", "101", "111"], "1": ["010", "110", "010", "010", "111"],
    "2": ["111", "001", "111", "100", "111"], "3": ["111", "001", "111", "001", "111"],
    "4": ["101", "101", "111", "001", "001"], "5": ["111", "100", "111", "001", "111"],
    "6": ["111", "100", "111", "101", "111"], "7": ["111", "001", "010", "010", "010"],
    "8": ["111", "101", "111", "101", "111"], "9": ["111", "101", "111", "001", "111"],
    ":": ["000", "010", "000", "010", "000"], "-": ["000", "000", "111", "000", "000"],
    " ": ["000", "000", "000", "000", "000"],
}


def draw_text(cells: List[List[RGB]], x: int, y: int, text: str, color: RGB, scale: int = 1) -> None:
    for ch in text:
        glyph = _FONT.get(ch, _FONT[" "])
        for gy, line in enumerate(glyph):
            for gx, on in enumerate(line):
                if on != "1":
                    continue
                for sy in range(scale):
                    for sx in range(scale):
                        yy, xx = y + gy * scale + sy, x + gx * scale + sx
                        if 0 <= yy < len(cells) and 0 <= xx < len(cells[0]):
                            cells[yy][xx] = color
        x += 4 * scale


def hue_rgb(h: float, s: float = 0.55, v: float = 0.8) -> RGB:
    i = int(h * 6) % 6
    f = h * 6 - int(h * 6)
    p, q, t = v * (1 - s), v * (1 - f * s), v * (1 - (1 - f) * s)
    r, g, b = [(v, t, p), (q, v, p), (p, v, t), (p, q, v), (t, p, v), (v, p, q)][i]
    return int(r * 255), int(g * 255), int(b * 255)


def picture(w: int, h: int, hue: float, label: str, phase: float = 0.0) -> List[List[RGB]]:
    """A recognisable test card: hue gradient, a moving bar, a label in digits."""
    cells = []
    for yy in range(h):
        v = 0.35 + 0.45 * yy / max(1, h)
        row = [hue_rgb((hue + 0.15 * x / max(1, w)) % 1.0, 0.5, v) for x in range(w)]
        cells.append(row)
    bar = int(phase * w) % max(1, w)
    for yy in range(h):
        for dx in range(2):
            cells[yy][(bar + dx) % w] = (235, 235, 235)
    scale = max(1, min(w // (len(label) * 4 + 2), h // 7))
    draw_text(cells, 1, 1, label, (255, 255, 255), scale)
    return cells


def still_jpeg(w_cells: int, h_cells: int, hue: float, label: str) -> bytes:
    return encode_cells(picture(w_cells, h_cells, hue, label, phase=0.5))


class FrameCache:
    """Live-view frames (640x360 = 80x45 blocks), cached per loop position."""

    def __init__(self, hue: float, loop: int = 100, w: int = 80, h: int = 45):
        self.hue, self.loop, self.w, self.h = hue, loop, w, h
        self._cells: Dict[int, List[List[RGB]]] = {}

    def frame(self, seq: int, stamp: str, pad_to: int = 23500) -> bytes:
        k = seq % self.loop
        cells = self._cells.get(k)
        if cells is None:
            cells = picture(self.w, self.h, self.hue, "", phase=k / self.loop)
            self._cells[k] = cells
        full = [row[:] for row in cells]     # the cached picture stays clean
        draw_text(full, 2, 2, stamp, (255, 255, 255), 1)
        return encode_cells(full, comment=b"lumix-emulator seq=%d" % seq, pad_to=pad_to)


def jpeg_size(data: bytes) -> Optional[Tuple[int, int]]:
    """(width, height) from the SOF0 segment, for tests."""
    i = 2
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        length = struct.unpack(">H", data[i + 2:i + 4])[0]
        if marker == 0xC0:
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return w, h
        i += 2 + length
    return None

