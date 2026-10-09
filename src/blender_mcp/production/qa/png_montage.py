"""Small dependency-free PNG contact sheet for our 8-bit RGBA render outputs."""
from __future__ import annotations

import struct
import zlib
from pathlib import Path


def _decode(path: Path):
    raw = path.read_bytes()
    if raw[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("Invalid PNG signature")
    offset, compressed = 8, []
    width = height = None
    finished = False
    while offset < len(raw):
        if offset + 12 > len(raw):
            raise ValueError("Truncated PNG chunk")
        length = struct.unpack(">I", raw[offset:offset+4])[0]
        kind = raw[offset+4:offset+8]
        data = raw[offset+8:offset+8+length]
        crc = raw[offset+8+length:offset+12+length]
        if len(data) != length or len(crc) != 4 or struct.unpack(">I", crc)[0] != zlib.crc32(kind+data):
            raise ValueError("Invalid PNG chunk checksum")
        if kind == b"IHDR":
            if width is not None or len(data) != 13:
                raise ValueError("Invalid PNG IHDR")
            width, height, depth, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", data)
            if (depth, color, compression, filtering, interlace) != (8, 6, 0, 0, 0):
                raise ValueError("Montage requires noninterlaced 8-bit RGBA PNGs")
            if width < 1 or height < 1 or width*height > 4_000_000:
                raise ValueError("PNG dimensions exceed montage budget")
        elif kind == b"IDAT":
            compressed.append(data)
        elif kind == b"IEND":
            finished = True
        offset += 12 + length
        if finished:
            break
    if not finished or offset != len(raw) or width is None:
        raise ValueError("PNG is incomplete")
    row_bytes = width*4
    expected = height*(row_bytes+1)
    decoder = zlib.decompressobj()
    pixels = decoder.decompress(b"".join(compressed), expected+1)
    if len(pixels) != expected or not decoder.eof or decoder.unused_data:
        raise ValueError("PNG pixel data length mismatch")
    rows = []
    previous = bytearray(row_bytes)
    for y in range(height):
        start = y*(row_bytes+1)
        filtering = pixels[start]
        row = bytearray(pixels[start+1:start+1+row_bytes])
        if filtering not in range(5):
            raise ValueError("Invalid PNG row filter")
        for x in range(row_bytes):
            left = row[x-4] if x >= 4 else 0
            up = previous[x]
            upper_left = previous[x-4] if x >= 4 else 0
            if filtering == 1:
                predictor = left
            elif filtering == 2:
                predictor = up
            elif filtering == 3:
                predictor = (left+up)//2
            elif filtering == 4:
                estimate = left+up-upper_left
                distances = (abs(estimate-left), abs(estimate-up), abs(estimate-upper_left))
                predictor = (left, up, upper_left)[distances.index(min(distances))]
            else:
                predictor = 0
            row[x] = (row[x]+predictor) & 255
        rows.append(bytes(row))
        previous = row
    return width, height, rows


def _chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind+data))


def write_montage(rows: list[list[Path]], output: Path):
    """Write new PNG; inputs must be equal size, grid <= 3x2 and 24M pixels."""
    if not rows or len(rows) > 2 or not rows[0] or len(rows[0]) > 3 or any(len(row) != len(rows[0]) for row in rows):
        raise ValueError("Montage grid must be rectangular and at most 3x2")
    decoded = [[_decode(path) for path in row] for row in rows]
    width, height, _ = decoded[0][0]
    if any((w,h) != (width,height) for row in decoded for w,h,_ in row):
        raise ValueError("Montage image dimensions must match")
    full_width, full_height = width*len(rows[0]), height*len(rows)
    if full_width*full_height > 24_000_000:
        raise ValueError("Montage pixel budget exceeded")
    pixels = b"".join(b"\0"+b"".join(image[2][y] for image in row) for row in decoded for y in range(height))
    data = (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", full_width,full_height,8,6,0,0,0))
            + _chunk(b"sRGB", b"\0") + _chunk(b"IDAT", zlib.compress(pixels)) + _chunk(b"IEND", b""))
    with output.open("xb") as stream:
        stream.write(data)
    return full_width, full_height
