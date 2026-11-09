"""Host-side transport of external per-loop UV arrays into Blender.

Safe mode keeps ``open``/``numpy`` out of Blender payloads and the upstream socket parses JSON with
quadratic cost (measured 4.2 MB -> 18.5 s), so millions of UVs cannot travel as JSON. The host reads
the caller's ``.npy`` without pickle, validates it, and writes an uncompressed float32 OpenEXR
"carrier" that Blender loads through the permitted ``bpy.data.images.load`` path. Two loops travel in
each RGBA pixel (R=u0, G=v0, B=u1, A=v1); the Blender side reads the pixels back bit-exact and only
then writes the new UV layer.
"""
from __future__ import annotations

import ast
from array import array
import hashlib
import math
from pathlib import Path
import struct
import sys

NPY_MAGIC = b"\x93NUMPY"
MAX_LOOPS = 60_000_000
CARRIER_WIDTH = 4096
# EXR pixel type 2 = FLOAT (32-bit IEEE); one scanline per block because compression is NONE.
_FLOAT = 2


def _header_bytes(path: Path, stream) -> tuple[dict, int]:
    magic = stream.read(6)
    if magic != NPY_MAGIC:
        raise ValueError("UV_SOURCE_FORMAT: " + path.name + " is not a .npy file (missing \\x93NUMPY)")
    major, _minor = stream.read(2)
    if major == 1:
        (length,) = struct.unpack("<H", stream.read(2))
    elif major in (2, 3):
        (length,) = struct.unpack("<I", stream.read(4))
    else:
        raise ValueError("UV_SOURCE_FORMAT: unsupported .npy version " + str(major))
    if length > 65_536:
        raise ValueError("UV_SOURCE_FORMAT: .npy header too large")
    text = stream.read(length).decode("latin1")
    # literal_eval only builds literals: no pickle, no object arrays, no code.
    header = ast.literal_eval(text)
    if not isinstance(header, dict) or set(header) != {"descr", "fortran_order", "shape"}:
        raise ValueError("UV_SOURCE_FORMAT: unexpected .npy header " + text.strip())
    return header, stream.tell()


def read_uv_array(path: str | Path, expected_loops: int | None = None) -> tuple[array, dict]:
    """Return (float32 array [u0, v0, u1, v1, ...], provenance). Rejects anything ambiguous."""
    source = Path(path)
    if not source.is_absolute() or source.suffix.lower() != ".npy":
        raise ValueError("UV_SOURCE_PATH: an absolute path to a .npy file is required")
    if not source.is_file() or source.is_symlink():
        raise ValueError("UV_SOURCE_PATH: file not found or is a symlink: " + str(source))
    with source.open("rb") as stream:
        header, offset = _header_bytes(source, stream)
        descr, shape = header["descr"], header["shape"]
        if header["fortran_order"] is not False:
            raise ValueError("UV_SOURCE_LAYOUT: fortran_order arrays are not accepted; save C-ordered (N, 2)")
        if descr not in ("<f4", "<f8"):
            raise ValueError("UV_SOURCE_DTYPE: dtype must be little-endian float32/float64, got " + repr(descr))
        if not (isinstance(shape, tuple) and len(shape) == 2 and shape[1] == 2 and isinstance(shape[0], int)):
            raise ValueError("UV_SOURCE_SHAPE: shape must be (loops, 2), got " + repr(shape))
        loops = shape[0]
        if not 1 <= loops <= MAX_LOOPS:
            raise ValueError("UV_SOURCE_CAPACITY: 1.." + str(MAX_LOOPS) + " loops supported, got " + str(loops))
        if expected_loops is not None and loops != expected_loops:
            raise ValueError("UV_LOOP_COUNT_MISMATCH: file has " + str(loops) + " loops, mesh has " + str(expected_loops))
        width = 4 if descr == "<f4" else 8
        payload = stream.read()
    if len(payload) != loops * 2 * width:
        raise ValueError("UV_SOURCE_TRUNCATED: expected " + str(loops * 2 * width) + " data bytes, found " + str(len(payload)))
    values = array("f" if width == 4 else "d")
    values.frombytes(payload)
    if sys.byteorder != "little":
        values.byteswap()
    if width == 8:
        doubles = values
        values = array("f", doubles)
        # float64 -> float32 is the precision Blender stores; report how much was lost.
        rounding = max((abs(a - b) for a, b in zip(doubles, values)), default=0.0)
    else:
        rounding = 0.0
    nonfinite = sum(1 for v in values if not math.isfinite(v))
    if nonfinite:
        raise ValueError("UV_SOURCE_NONFINITE: " + str(nonfinite) + " NaN/Inf values")
    digest = hashlib.sha256()
    with source.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    u = values[0::2]
    v = values[1::2]
    info = {"path": str(source), "bytes": source.stat().st_size, "sha256": digest.hexdigest(),
            "dtype": descr, "shape": [loops, 2], "loops": loops, "header_offset": offset,
            "float64_to_float32_max_rounding": rounding,
            "bounds": {"u_min": min(u), "u_max": max(u), "v_min": min(v), "v_max": max(v)}}
    return values, info


def _attribute(name: str, kind: str, value: bytes) -> bytes:
    return name.encode() + b"\0" + kind.encode() + b"\0" + struct.pack("<i", len(value)) + value


def write_carrier_exr(path: str | Path, values: array) -> dict:
    """Write [u0, v0, u1, v1, ...] as an uncompressed float32 RGBA OpenEXR, two loops per pixel.

    Blender's pixel buffer starts at the bottom row while EXR scanline 0 is the top row, so data row r
    is written to scanline height-1-r. The Blender side verifies the round trip; it never trusts this."""
    if values.typecode != "f":
        raise ValueError("carrier needs float32 values")
    if len(values) % 2:
        raise ValueError("carrier needs an even number of floats")
    target = Path(path)
    if target.exists():
        raise FileExistsError("refusing to overwrite carrier " + str(target))
    loops = len(values) // 2
    pixels = (loops + 1) // 2
    width = min(CARRIER_WIDTH, pixels)
    height = (pixels + width - 1) // width
    padded = array("f", values)
    padded.extend(array("f", bytes(4 * (width * height * 4 - len(values)))))
    channels = b"".join(name + b"\0" + struct.pack("<iB3xii", _FLOAT, 0, 1, 1) for name in (b"A", b"B", b"G", b"R")) + b"\0"
    box = struct.pack("<iiii", 0, 0, width - 1, height - 1)
    header = (b"\x76\x2f\x31\x01" + struct.pack("<i", 2)
              + _attribute("channels", "chlist", channels)
              + _attribute("compression", "compression", b"\0")
              + _attribute("dataWindow", "box2i", box)
              + _attribute("displayWindow", "box2i", box)
              + _attribute("lineOrder", "lineOrder", b"\0")
              + _attribute("pixelAspectRatio", "float", struct.pack("<f", 1.0))
              + _attribute("screenWindowCenter", "v2f", struct.pack("<ff", 0.0, 0.0))
              + _attribute("screenWindowWidth", "float", struct.pack("<f", 1.0))
              + b"\0")
    line_bytes = width * 4 * 4
    block = 8 + line_bytes
    first = len(header) + 8 * height
    offsets = struct.pack("<" + str(height) + "Q", *(first + y * block for y in range(height)))
    digest = hashlib.sha256()
    with target.open("xb") as out:
        for part in (header, offsets):
            out.write(part)
            digest.update(part)
        for y in range(height):
            row = height - 1 - y
            start = row * width * 4
            line = padded[start:start + width * 4]
            data = b"".join(line[c::4].tobytes() for c in (3, 2, 1, 0))
            chunk = struct.pack("<ii", y, line_bytes) + data
            out.write(chunk)
            digest.update(chunk)
    return {"path": str(target), "width": width, "height": height, "loops": loops,
            "pixels": width * height, "bytes": target.stat().st_size, "sha256": digest.hexdigest()}
