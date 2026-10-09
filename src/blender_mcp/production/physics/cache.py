"""Portable, exclusive physics caches. Blender evaluates; the host writes PC2.

No filesystem primitive is added to the upstream Blender script allowlist.
Coordinates are float32 mesh-local XYZ, sampled on every integer clip frame.
"""
from __future__ import annotations

import array
import math
import json
from pathlib import Path
import struct
import sys

from ..common.storage import sha256, write_json
from ..common.operations import fingerprint

MAX_CACHE_BYTES = 256 * 1024 * 1024


def write_pc2(storage, operation_id, frames, dependencies, frame_start):
    if type(frame_start) is not int:
        raise ValueError('CACHE_TIME_INVALID: integer start frame')
    if sys.byteorder != 'little':
        raise ValueError('CACHE_ENDIAN_UNSUPPORTED: float32 payload requires little endian')
    if not frames or not 1 <= len(frames) <= 601:
        raise ValueError('CACHE_FRAME_CAPACITY: 1..601 complete samples required')
    count = frames[0]['vertex_count']
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 20000:
        raise ValueError('CACHE_VERTEX_CAPACITY: 1..20000 vertices')
    estimated = 32 + count * 12 * len(frames)
    if estimated > MAX_CACHE_BYTES:
        raise ValueError('CACHE_CAPACITY: portable cache would exceed 256 MiB')
    chunks = []
    for index, sample in enumerate(frames):
        if type(sample['frame']) is not int or sample['frame'] != frame_start + index or sample['vertex_count'] != count:
            raise ValueError('CACHE_TOPOLOGY_OR_TIME_CHANGED: full consecutive frames and fixed topology required')
        raw = bytes.fromhex(sample['hex'])
        if len(raw) != count * 12:
            raise ValueError('CACHE_PAYLOAD_INVALID: sample byte count differs')
        values = array.array('f'); values.frombytes(raw)
        if not all(math.isfinite(v) for v in values):
            raise ValueError('CACHE_NONFINITE: sample has NaN or infinity')
        chunks.append(raw)
    folder = storage.reserve('physics-cache', operation_id)
    path = folder / 'motion.pc2'
    with path.open('xb') as stream:
        stream.write(struct.pack('<12siiffi', b'POINTCACHE2\0', 1, count, float(frame_start), 1.0, len(frames)))
        for raw in chunks:
            stream.write(raw)
    record = {'schema': 'production.physics_cache.v1', 'representation': 'PC2', 'status': 'BAKED',
              'path': str(path), 'manifest': str(folder / 'manifest.json'), 'sha256': sha256(path),
              'frame_start': frame_start, 'frame_end': frame_start + len(frames) - 1,
              'frames': len(frames), 'vertices': count, 'bytes': path.stat().st_size,
              'dependencies': dependencies, 'dependencies_fingerprint': fingerprint(dependencies),
              'coordinates': 'float32 little-endian mesh local XYZ',
              'playback': {'frame_start': float(frame_start), 'frame_scale': 1.0, 'time_mode': 'FRAME', 'play_mode': 'SCENE'}}
    write_json(folder / 'manifest.json', record)
    return record


def verify(storage, cache, dependencies=None):
    path = Path(cache['path']).resolve(strict=True)
    if not path.is_relative_to(storage.root / 'physics-cache') or path.suffix != '.pc2':
        raise ValueError('INVALID_CACHE_REFERENCE: cache must belong to this physics store')
    manifest = Path(cache['manifest']).resolve(strict=True)
    if manifest.parent != path.parent or manifest.name != 'manifest.json':
        raise ValueError('INVALID_CACHE_REFERENCE: wrong cache manifest')
    recorded=json.loads(manifest.read_text(encoding='utf-8'))
    if any(recorded.get(k)!=cache.get(k) for k in ('path','sha256','bytes','frame_start','frame_end','dependencies_fingerprint')):
        raise ValueError('STALE_CACHE: manifest differs from the immutable recorded artifact')
    if sha256(path) != cache['sha256'] or path.stat().st_size != cache['bytes']:
        raise ValueError('STALE_CACHE: cache content changed; never reuse it')
    if dependencies is not None and fingerprint(dependencies) != cache['dependencies_fingerprint']:
        raise ValueError('STALE_CACHE: primary motion, geometry, settings, colliders or context changed')
    return {'status': 'FRESH', 'path': str(path), 'bytes': path.stat().st_size, 'sha256': cache['sha256']}
