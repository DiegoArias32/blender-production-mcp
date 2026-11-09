"""M10 imaging without Blender: PNG decode (filters 0-4), encode round trip, refusals, sRGB/Rec.709 luminance,
statistics, exact mask labelling and review artifacts."""
from __future__ import annotations

import math
import struct
import zlib

import pytest

from blender_mcp.production.cinematic import imaging
from blender_mcp.production.cinematic.imaging import Image


def _paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    return a if pa <= pb and pa <= pc else (b if pb <= pc else c)


def _filter_row(kind, row, prev, bpp):
    out = bytearray()
    for i, x in enumerate(row):
        a = row[i - bpp] if i >= bpp else 0
        b = prev[i]
        c = prev[i - bpp] if i >= bpp else 0
        predictor = {0: 0, 1: a, 2: b, 3: (a + b) >> 1, 4: _paeth(a, b, c)}[kind]
        out.append((x - predictor) & 255)
    return bytes(out)


def _png(width, height, color, pixels, kinds, extra_chunks=()):
    """Reference encoder: one filter type per row, as libpng's adaptive writer may produce."""
    bpp = imaging.CHANNELS[color]
    stride = width * bpp
    raw = bytearray()
    prev = bytes(stride)
    for y in range(height):
        row = pixels[y * stride:(y + 1) * stride]
        kind = kinds[y % len(kinds)]
        raw.append(kind)
        raw += _filter_row(kind, row, prev, bpp)
        prev = row
    body = imaging.SIGNATURE + imaging._chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, color, 0, 0, 0))
    for kind, data in extra_chunks:
        body += imaging._chunk(kind, data)
    compressed = zlib.compress(bytes(raw))
    # IDAT split in two chunks: decoders must concatenate.
    return body + imaging._chunk(b'IDAT', compressed[:5]) + imaging._chunk(b'IDAT', compressed[5:]) + imaging._chunk(b'IEND', b'')


def _pixels(width, height, channels, seed=7):
    value = seed
    out = bytearray()
    for _ in range(width * height * channels):
        value = (value * 1103515245 + 12345) & 0x7FFFFFFF
        out.append((value >> 16) & 255)
    return bytes(out)


@pytest.mark.parametrize('kind', [0, 1, 2, 3, 4])
@pytest.mark.parametrize('color', [0, 2, 4, 6])
def test_decode_every_filter_and_color_type(kind, color):
    channels = imaging.CHANNELS[color]
    pixels = _pixels(9, 5, channels, seed=kind * 11 + color)
    image = imaging.decode_png(_png(9, 5, color, pixels, [kind], extra_chunks=[(b'tEXt', b'k\x00v')]))
    assert (image.width, image.height, image.channels) == (9, 5, channels)
    assert image.pixels == pixels


def test_decode_mixed_filters_per_row():
    pixels = _pixels(17, 11, 3)
    assert imaging.decode_png(_png(17, 11, 2, pixels, [4, 1, 0, 2, 3])).pixels == pixels


@pytest.mark.parametrize('channels', [1, 2, 3, 4])
def test_encode_round_trip(channels, tmp_path):
    image = Image(13, 4, channels, _pixels(13, 4, channels, seed=channels))
    assert imaging.decode_png(imaging.encode_png(image)) == image
    path = tmp_path / 'x.png'
    assert imaging.write_png(path, image) == path.stat().st_size
    assert imaging.read_png(path) == image
    with pytest.raises(FileExistsError):
        imaging.write_png(path, image)  # artifacts are never overwritten


def test_refusals_carry_codes():
    good = _png(3, 2, 2, _pixels(3, 2, 3), [0])
    with pytest.raises(ValueError, match='PNG_SIGNATURE_INVALID'):
        imaging.decode_png(b'GIF89a' + good[6:])
    corrupt = bytearray(good)
    corrupt[-20] ^= 0xFF  # inside the IDAT/IEND tail
    with pytest.raises(ValueError, match='PNG_(CRC_MISMATCH|TRUNCATED)'):
        imaging.decode_png(bytes(corrupt))
    with pytest.raises(ValueError, match='PNG_TRUNCATED'):
        imaging.decode_png(good[:40])
    sixteen = imaging.SIGNATURE + imaging._chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 16, 2, 0, 0, 0)) + \
        imaging._chunk(b'IDAT', zlib.compress(b'\x00' + bytes(6))) + imaging._chunk(b'IEND', b'')
    with pytest.raises(ValueError, match='PNG_UNSUPPORTED'):
        imaging.decode_png(sixteen)
    interlaced = imaging.SIGNATURE + imaging._chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 1)) + \
        imaging._chunk(b'IDAT', zlib.compress(b'\x00' + bytes(3))) + imaging._chunk(b'IEND', b'')
    with pytest.raises(ValueError, match='PNG_UNSUPPORTED'):
        imaging.decode_png(interlaced)
    palette = imaging.SIGNATURE + imaging._chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 3, 0, 0, 0)) + \
        imaging._chunk(b'PLTE', bytes(3)) + imaging._chunk(b'IDAT', zlib.compress(b'\x00\x00')) + imaging._chunk(b'IEND', b'')
    with pytest.raises(ValueError, match='PNG_UNSUPPORTED'):
        imaging.decode_png(palette)
    bad_filter = imaging.SIGNATURE + imaging._chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 0, 0, 0, 0)) + \
        imaging._chunk(b'IDAT', zlib.compress(b'\x07\x10')) + imaging._chunk(b'IEND', b'')
    with pytest.raises(ValueError, match='PNG_FILTER_INVALID'):
        imaging.decode_png(bad_filter)
    short = imaging.SIGNATURE + imaging._chunk(b'IHDR', struct.pack('>IIBBBBB', 2, 2, 8, 0, 0, 0, 0)) + \
        imaging._chunk(b'IDAT', zlib.compress(b'\x00\x10')) + imaging._chunk(b'IEND', b'')
    with pytest.raises(ValueError, match='PNG_DATA_INVALID'):
        imaging.decode_png(short)


def test_srgb_transfer_and_rec709_luminance():
    assert imaging.SRGB_LUT[0] == 0.0 and imaging.SRGB_LUT[255] == 1.0
    assert abs(imaging.SRGB_LUT[128] - 0.2158605) < 1e-6
    for code in range(256):
        assert imaging.encode_8bit(imaging.SRGB_LUT[code]) == code  # the two directions are exact inverses
    # Blender wrote (0.2, 0.4, 0.6) as (124, 170, 203) through the Standard transform (measured, docs).
    assert [imaging.encode_8bit(v) for v in (0.2, 0.4, 0.6)] == [124, 170, 203]
    image = Image(4, 1, 3, bytes([255, 255, 255, 255, 0, 0, 0, 255, 0, 0, 0, 255]))
    y = list(imaging.luminance(image))
    assert y == pytest.approx([1.0, 0.2126, 0.7152, 0.0722], abs=1e-12)
    gray = Image(2, 1, 2, bytes([128, 0, 255, 255]))  # gray+alpha: alpha ignored
    assert list(imaging.luminance(gray)) == pytest.approx([0.2158605, 1.0], abs=1e-6)


def test_statistics_fractions_and_percentiles():
    values = [0.0, 0.005, 0.2, 0.4, 0.995, 1.0]
    s = imaging.stats(values, clip=0.99, crush=0.01)
    assert s['pixels'] == 6 and s['mean'] == pytest.approx(sum(values) / 6, abs=1e-6)
    assert s['median'] == pytest.approx(0.3) and s['clipped_fraction'] == pytest.approx(2 / 6, abs=1e-6)
    assert s['crushed_fraction'] == pytest.approx(2 / 6, abs=1e-6)
    sub = imaging.stats(values, [2, 3])
    assert sub['pixels'] == 2 and sub['mean'] == pytest.approx(0.3) and sub['clipped_fraction'] == 0
    empty = imaging.stats(values, [])
    assert empty['pixels'] == 0 and empty['mean'] is None
    assert imaging.stops(0.4, 0.2, eps=0) == pytest.approx(1.0)
    assert math.isfinite(imaging.stops(0.0, 0.0))
    clipped = Image(2, 1, 3, bytes([255, 10, 10, 254, 254, 254]))
    assert imaging.channel_clipped_fraction(clipped) == 0.5


def test_mask_labels_are_exact_and_count_unmatched():
    palette = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [1.0, 0.5, 0.0]]
    orange = imaging.encode_8bit(0.5)
    pixels = bytes([0, 0, 0, 255, 0, 0, 0, 255, 0, 255, orange, 0, 255, orange + 1, 0, 128, 128, 128])
    labels, counts = imaging.label_mask(Image(6, 1, 3, pixels), palette)
    assert labels == [-1, 0, 1, 2, 2, -2]
    assert counts == {-1: 1, 0: 1, 1: 1, 2: 2, -2: 1}
    with pytest.raises(ValueError, match='MASK_PALETTE_AMBIGUOUS'):
        imaging.palette_codes([[1.0, 0.0, 0.0], [1.0, 0.0, 0.0005]])  # codes 0 and 2: within +-2
    with pytest.raises(ValueError, match='MASK_CHANNELS_INVALID'):
        imaging.label_mask(Image(1, 1, 1, b'\x00'), palette)


def test_review_artifacts():
    image = Image(8, 4, 3, _pixels(8, 4, 3))
    thumb = imaging.thumbnail(image, 4)
    assert (thumb.width, thumb.height, thumb.channels) == (4, 2, 3)
    assert thumb.pixels[:3] == image.pixels[:3]
    sheet = imaging.contact_sheet([thumb, thumb, thumb], columns=2, gap=1)
    assert (sheet.width, sheet.height) == (2 * 4 + 3, 2 * 2 + 3)
    values = [1.0, 0.0, 0.5, 0.2]
    false = imaging.exposure_false_color(Image(4, 1, 3, bytes(12)), values)
    assert false.pixels[:6] == b'\xff\x00\x00\x00\x00\xff'
    assert false.pixels[6] == imaging.encode_8bit(0.5)
    with pytest.raises(ValueError, match='CONTACT_SHEET_EMPTY'):
        imaging.contact_sheet([])
    with pytest.raises(ValueError, match='PNG_IMAGE_INVALID'):
        Image(2, 2, 3, b'\x00')
