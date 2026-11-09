"""Pure-Python PNG reading/writing and luminance statistics for M10 lighting QA (host side, no Blender, no numpy).

Why pure Python: Blender payloads cannot import numpy and `Render Result.pixels` is empty in background (measured,
Blender 5.2.1), so renders are written as 8-bit PNG and measured here, on the host, from the exact bytes on disk.

Scope (deliberately small): 8-bit, non-interlaced PNG with color types 0 (gray), 2 (RGB), 4 (gray+alpha) and 6 (RGBA);
all five row filters (None, Sub, Up, Average, Paeth). Palette, 16-bit and Adam7 interlaced files are refused with a
code instead of being decoded approximately.

Luminance: Rec.709/sRGB primaries, Y = 0.2126 R + 0.7152 G + 0.0722 B computed on LINEAR values decoded from the
sRGB transfer function (IEC 61966-2-1). The PNG holds display-encoded values; the statistics are scene-display
linear, not scene-referred radiance (the view transform already happened inside Blender).
"""
from __future__ import annotations

from array import array
from dataclasses import dataclass
import math
from pathlib import Path
import struct
import zlib

SIGNATURE = b'\x89PNG\r\n\x1a\n'
CHANNELS = {0: 1, 2: 3, 4: 2, 6: 4}
REC709 = (0.2126, 0.7152, 0.0722)
MAX_PIXELS = 4096 * 4096


@dataclass(frozen=True)
class Image:
    width: int
    height: int
    channels: int
    pixels: bytes

    def __post_init__(self):
        if self.channels not in (1, 2, 3, 4) or self.width < 1 or self.height < 1:
            raise ValueError('PNG_IMAGE_INVALID: unsupported size or channel count')
        if len(self.pixels) != self.width * self.height * self.channels:
            raise ValueError('PNG_IMAGE_INVALID: pixel buffer does not match width*height*channels')


def _fail(code, detail):
    raise ValueError(code + ': ' + detail)


# ---- sRGB transfer ----

def srgb_to_linear(value: float) -> float:
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def linear_to_srgb(value: float) -> float:
    value = min(max(value, 0.0), 1.0)
    return value * 12.92 if value <= 0.0031308 else 1.055 * value ** (1.0 / 2.4) - 0.055


SRGB_LUT = tuple(srgb_to_linear(i / 255.0) for i in range(256))
# Pre-weighted tables: one lookup per channel instead of a multiply per pixel.
_LR = tuple(REC709[0] * v for v in SRGB_LUT)
_LG = tuple(REC709[1] * v for v in SRGB_LUT)
_LB = tuple(REC709[2] * v for v in SRGB_LUT)


def encode_8bit(linear: float) -> int:
    """The 8-bit code Blender writes for a linear value through the sRGB display transform (round half up)."""
    return int(math.floor(linear_to_srgb(linear) * 255.0 + 0.5))


# ---- decoding ----

def _paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def _unfilter(raw: bytes, width: int, height: int, bpp: int) -> bytes:
    stride = width * bpp
    if len(raw) != height * (stride + 1):
        _fail('PNG_DATA_INVALID', 'decompressed size ' + str(len(raw)) + ' != ' + str(height * (stride + 1)))
    out = bytearray(height * stride)
    prev = bytearray(stride)
    pos = 0
    for y in range(height):
        kind = raw[pos]
        line = bytearray(raw[pos + 1:pos + 1 + stride])
        pos += stride + 1
        if kind == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 255
        elif kind == 2:
            line = bytearray((x + p) & 255 for x, p in zip(line, prev))
        elif kind == 3:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 255
        elif kind == 4:
            for i in range(stride):
                if i >= bpp:
                    line[i] = (line[i] + _paeth(line[i - bpp], prev[i], prev[i - bpp])) & 255
                else:
                    line[i] = (line[i] + prev[i]) & 255
        elif kind != 0:
            _fail('PNG_FILTER_INVALID', 'row ' + str(y) + ' uses filter ' + str(kind))
        out[y * stride:(y + 1) * stride] = line
        prev = line
    return bytes(out)


def decode_png(data: bytes) -> Image:
    if not isinstance(data, (bytes, bytearray)) or data[:8] != SIGNATURE:
        _fail('PNG_SIGNATURE_INVALID', 'not a PNG file')
    pos = 8
    header = None
    chunks = []
    ended = False
    while pos + 12 <= len(data):
        length, kind = struct.unpack('>I4s', data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + length]
        crc = data[pos + 8 + length:pos + 12 + length]
        if len(body) != length or len(crc) != 4:
            _fail('PNG_TRUNCATED', 'chunk ' + kind.decode('latin-1') + ' is truncated')
        if struct.unpack('>I', crc)[0] != zlib.crc32(kind + body) & 0xFFFFFFFF:
            _fail('PNG_CRC_MISMATCH', 'chunk ' + kind.decode('latin-1'))
        pos += 12 + length
        if kind == b'IHDR':
            header = struct.unpack('>IIBBBBB', body)
        elif kind == b'IDAT':
            chunks.append(body)
        elif kind == b'IEND':
            ended = True
            break
        elif kind == b'PLTE':
            _fail('PNG_UNSUPPORTED', 'palette images are not supported')
    if header is None or not chunks or not ended:
        _fail('PNG_TRUNCATED', 'missing IHDR, IDAT or IEND')
    width, height, depth, color, compression, filtering, interlace = header
    if depth != 8 or color not in CHANNELS or compression or filtering or interlace:
        _fail('PNG_UNSUPPORTED', 'only 8-bit non-interlaced gray/RGB/RGBA (depth ' + str(depth) + ', color type '
              + str(color) + ', interlace ' + str(interlace) + ')')
    if width < 1 or height < 1 or width * height > MAX_PIXELS:
        _fail('PNG_SIZE_UNSUPPORTED', str(width) + 'x' + str(height))
    try:
        raw = zlib.decompress(b''.join(chunks))
    except zlib.error as exc:
        _fail('PNG_DATA_INVALID', str(exc))
    channels = CHANNELS[color]
    return Image(width, height, channels, _unfilter(raw, width, height, channels))


def read_png(path) -> Image:
    return decode_png(Path(path).read_bytes())


# ---- encoding (filter 0 only: the files are small QA artifacts, not archives) ----

def _chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack('>I', len(body)) + kind + body + struct.pack('>I', zlib.crc32(kind + body) & 0xFFFFFFFF)


def encode_png(image: Image) -> bytes:
    color = {1: 0, 2: 4, 3: 2, 4: 6}[image.channels]
    stride = image.width * image.channels
    raw = bytearray()
    for y in range(image.height):
        raw.append(0)
        raw += image.pixels[y * stride:(y + 1) * stride]
    return (SIGNATURE + _chunk(b'IHDR', struct.pack('>IIBBBBB', image.width, image.height, 8, color, 0, 0, 0))
            + _chunk(b'IDAT', zlib.compress(bytes(raw), 6)) + _chunk(b'IEND', b''))


def write_png(path, image: Image) -> int:
    data = encode_png(image)
    with Path(path).open('xb') as dest:  # never overwrite an artifact
        dest.write(data)
    return len(data)


# ---- measurements ----

def luminance(image: Image) -> array:
    """Linear Rec.709 luminance per pixel (alpha ignored: QA renders are written opaque RGB)."""
    px, c = image.pixels, image.channels
    if c >= 3:
        lr, lg, lb = _LR, _LG, _LB
        return array('d', (lr[r] + lg[g] + lb[b] for r, g, b in zip(px[0::c], px[1::c], px[2::c])))
    lut = SRGB_LUT
    return array('d', (lut[v] for v in px[0::c]))


def channel_clipped_fraction(image: Image) -> float:
    """Fraction of pixels with any color channel at code 255 (display clipping per channel)."""
    px, c = image.pixels, image.channels
    if c < 3:
        values = px[0::c]
        return sum(1 for v in values if v == 255) / len(values)
    count = sum(1 for r, g, b in zip(px[0::c], px[1::c], px[2::c]) if r == 255 or g == 255 or b == 255)
    return count / (image.width * image.height)


def _percentile(ordered, q):
    if not ordered:
        return None
    index = (len(ordered) - 1) * q
    low = int(math.floor(index))
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (index - low)


def stats(values, indices=None, clip=0.99, crush=0.01) -> dict:
    """Mean, median, p05/p95, and clipped (>= clip) / crushed (<= crush) fractions over all or selected pixels."""
    subset = values if indices is None else [values[i] for i in indices]
    n = len(subset)
    if n == 0:
        return {'pixels': 0, 'mean': None, 'median': None, 'p05': None, 'p95': None, 'clipped_fraction': None,
                'crushed_fraction': None}
    ordered = sorted(subset)
    return {'pixels': n, 'mean': round(sum(subset) / n, 6), 'median': round(_percentile(ordered, 0.5), 6),
            'p05': round(_percentile(ordered, 0.05), 6), 'p95': round(_percentile(ordered, 0.95), 6),
            'clipped_fraction': round(sum(1 for v in subset if v >= clip) / n, 6),
            'crushed_fraction': round(sum(1 for v in subset if v <= crush) / n, 6)}


def stops(after, before, eps=1e-4) -> float:
    """Signed change in photographic stops; eps keeps black frames finite (definition, not a measurement)."""
    return math.log2((after + eps) / (before + eps))


def palette_codes(palette, tolerance=2):
    """Lookup {(r, g, b): label} for linear mask colors as Blender writes them through the Standard sRGB transform.
    Label -1 is the black background; tolerance absorbs +-1 code rounding differences."""
    table = {}
    entries = [(-1, (0.0, 0.0, 0.0))] + list(enumerate(palette))
    for label, color in entries:
        code = [encode_8bit(v) for v in color[:3]]
        for dr in range(-tolerance, tolerance + 1):
            for dg in range(-tolerance, tolerance + 1):
                for db in range(-tolerance, tolerance + 1):
                    key = (code[0] + dr, code[1] + dg, code[2] + db)
                    if key in table and table[key] != label:
                        _fail('MASK_PALETTE_AMBIGUOUS', 'palette colors closer than the tolerance')
                    table[key] = label
    return table


def label_mask(image: Image, palette, tolerance=2) -> tuple[list, dict]:
    """Per-pixel label: palette index, -1 background (black), -2 unmatched (antialiasing/dither would cause it)."""
    if image.channels < 3:
        _fail('MASK_CHANNELS_INVALID', 'masks are RGB')
    table = palette_codes(palette, tolerance)
    px, c = image.pixels, image.channels
    labels = [table.get(key, -2) for key in zip(px[0::c], px[1::c], px[2::c])]
    counts = {}
    for label in labels:
        counts[label] = counts.get(label, 0) + 1
    return labels, counts


# ---- review artifacts ----

def thumbnail(image: Image, width: int) -> Image:
    """Nearest-neighbour downscale to RGB (no filtering: the review sees the measured pixels)."""
    width = max(1, min(width, image.width))
    height = max(1, int(round(image.height * width / image.width)))
    c = image.channels
    out = bytearray(width * height * 3)
    for y in range(height):
        sy = min(image.height - 1, y * image.height // height)
        for x in range(width):
            sx = min(image.width - 1, x * image.width // width)
            i = (sy * image.width + sx) * c
            o = (y * width + x) * 3
            if c >= 3:
                out[o:o + 3] = image.pixels[i:i + 3]
            else:
                out[o:o + 3] = bytes((image.pixels[i],)) * 3
    return Image(width, height, 3, bytes(out))


def contact_sheet(images, columns=4, gap=2, background=(32, 32, 32)) -> Image:
    if not images:
        _fail('CONTACT_SHEET_EMPTY', 'no images')
    columns = max(1, min(columns, len(images)))
    rows = (len(images) + columns - 1) // columns
    cell_w = max(i.width for i in images)
    cell_h = max(i.height for i in images)
    width = columns * cell_w + (columns + 1) * gap
    height = rows * cell_h + (rows + 1) * gap
    out = bytearray(bytes(background) * (width * height))
    for n, image in enumerate(images):
        if image.channels != 3:
            _fail('CONTACT_SHEET_CHANNELS', 'thumbnails must be RGB')
        ox = gap + (n % columns) * (cell_w + gap)
        oy = gap + (n // columns) * (cell_h + gap)
        for y in range(image.height):
            start = ((oy + y) * width + ox) * 3
            out[start:start + image.width * 3] = image.pixels[y * image.width * 3:(y + 1) * image.width * 3]
    return Image(width, height, 3, bytes(out))


def exposure_false_color(image: Image, values, clip=0.99, crush=0.01) -> Image:
    """Red where luminance >= clip, blue where <= crush, display-encoded gray elsewhere."""
    out = bytearray(len(values) * 3)
    for i, v in enumerate(values):
        if v >= clip:
            out[i * 3:i * 3 + 3] = b'\xff\x00\x00'
        elif v <= crush:
            out[i * 3:i * 3 + 3] = b'\x00\x00\xff'
        else:
            g = encode_8bit(v)
            out[i * 3:i * 3 + 3] = bytes((g, g, g))
    return Image(image.width, image.height, 3, bytes(out))
