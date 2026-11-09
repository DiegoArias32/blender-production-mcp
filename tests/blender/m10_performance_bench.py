"""M10 workloads only (own background Blender, LocalBridge, no socket): 1/2/3 simple actors x short (3 s) and 15 s x
30/60 FPS. Per configuration it times shot planning, candidate creation, staging, clip synchronization, camera
setup, timeline (cuts + finalize), scene validation (integrity + composition, continuity, visibility, collisions) and
the Workbench temporal preview, and records the largest tool response (JSON bytes), payload sizes and the process
peak working set. Lighting validation (EEVEE) and the quality report are not part of this benchmark.

    blender -b --factory-startup --disable-autoexec --python tests/blender/m10_performance_bench.py -- <out_dir> [configs]

Every number is measured here (time.perf_counter, len(json.dumps(result)), build_code sizes, GetProcessMemoryInfo);
nothing is extrapolated. Timings depend on this machine and on what else uses the GPU: run it alone.
"""
from __future__ import annotations

import ctypes
import json
import math
import sys
import time
import traceback
import types
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'tests' / 'blender'))
package = types.ModuleType('blender_mcp')
package.__path__ = [str(ROOT / 'src' / 'blender_mcp')]
sys.modules.setdefault('blender_mcp', package)

import bpy  # noqa: E402

from blender_mcp.production.common.bridge import build_code  # noqa: E402
from blender_mcp.production.common.storage import Storage  # noqa: E402
from m31_fixture_support import register_open_fixture  # noqa: E402
import m10_cinematic_fixtures as FX  # noqa: E402

ROLE_BONES = (('root', 'root'), ('pelvis', 'pelvis'), ('head', 'head'), ('hand_left', ('hands', 'L')),
              ('hand_right', ('hands', 'R')), ('foot_left', ('feet', 'L')), ('foot_right', ('feet', 'R')))
COLLISION_CAPACITY = 3_000_000


class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD), ('PeakWorkingSetSize', ctypes.c_size_t),
                ('WorkingSetSize', ctypes.c_size_t), ('QuotaPeakPagedPoolUsage', ctypes.c_size_t),
                ('QuotaPagedPoolUsage', ctypes.c_size_t), ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t),
                ('QuotaNonPagedPoolUsage', ctypes.c_size_t), ('PagefileUsage', ctypes.c_size_t),
                ('PeakPagefileUsage', ctypes.c_size_t)]


def memory_mb():
    # Typed signatures: without them the pseudo-handle (-1) is passed as a 32-bit int on 64-bit Windows and the call
    # fails (measured in the first run: every reading was None).
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    psapi = ctypes.WinDLL('psapi', use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESS_MEMORY_COUNTERS), wintypes.DWORD]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    counters = PROCESS_MEMORY_COUNTERS()
    counters.cb = ctypes.sizeof(counters)
    if not psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
        return {'error': ctypes.get_last_error()}
    return {'working_set_mb': round(counters.WorkingSetSize / 2 ** 20, 1),
            'peak_working_set_mb': round(counters.PeakWorkingSetSize / 2 ** 20, 1)}


class LocalBridge:
    def __init__(self):
        self.log = []

    def run(self, body, params=None):
        code = build_code(body, params)
        ns = {}
        began = time.perf_counter()
        exec(code.rsplit('print(', 1)[0], ns)
        self.log.append((len(code.encode('utf-8')), time.perf_counter() - began))
        return ns['result']


def bones_of(actor):
    return {role: (actor['bones'][ref] if isinstance(ref, str) else actor['bones'][ref[0]][ref[1]]) for role, ref in ROLE_BONES}


def build_scene(out, actors, duration, fps):
    """A controlled scene of `actors` simple humanoids side by side (Idle only), saved under out/."""
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.render.fps, scene.render.fps_base = fps, 1.0
    scene.frame_start, scene.frame_end = 1, int(round(duration * fps))
    rows = []
    for i in range(actors):
        rows.append(FX.build_humanoid('Bench' + str(i), ((i - (actors - 1) / 2.0) * 1.4, 0.0, 0.0), 90.0, 'R'))
    blend = out / ('bench.blend')
    bpy.ops.wm.save_as_mainfile(filepath=str(blend))
    return rows, blend


def measure(result):
    return len(json.dumps(result, default=str).encode('utf-8'))


def run_config(out, actors, duration, fps):
    from blender_mcp.production.cinematic.service import dispatch
    folder = out / ('a%d_%ds_%dfps' % (actors, duration, fps))
    folder.mkdir(parents=True, exist_ok=True)
    rows, blend = build_scene(folder, actors, duration, fps)
    bridge = LocalBridge()
    storage = Storage(folder / 'store')
    register_open_fixture(bridge, storage, folder)
    row = {'actors': actors, 'duration_s': duration, 'fps': fps, 'frames': int(round(duration * fps)), 'stages': {},
           'responses_bytes_max': 0, 'errors': []}
    counter = [0]

    def call(name, stage, **kw):
        counter[0] += 1
        start = len(bridge.log)
        began = time.perf_counter()
        result = dispatch(name, bridge, storage, 'bench-%03d' % counter[0], **kw)
        spent = time.perf_counter() - began
        size = measure(result)
        entry = row['stages'].setdefault(stage, {'seconds': 0.0, 'calls': 0, 'bridge_calls': 0, 'max_payload_bytes': 0,
                                                 'max_response_bytes': 0})
        entry['seconds'] = round(entry['seconds'] + spent, 4)
        entry['calls'] += 1
        entry['bridge_calls'] += len(bridge.log) - start
        entry['max_payload_bytes'] = max([entry['max_payload_bytes']] + [b for b, _ in bridge.log[start:]])
        entry['max_response_bytes'] = max(entry['max_response_bytes'], size)
        row['responses_bytes_max'] = max(row['responses_bytes_max'], size)
        return result

    try:
        ids = []
        for actor in rows:
            ids.append(call('cinematic_register_actor', 'register', object_name=actor['object'],
                            options={'role': 'PROTAGONIST' if not ids else 'SUPPORTING', 'bones': bones_of(actor),
                                     'body_actions': [a['name'] for a in actor['actions']]})['actor_id'])
        intent = {'title': 'Bench', 'description': 'performance workload', 'duration_s': float(duration),
                  'actors': [{'actor_id': a, 'role': 'PROTAGONIST' if i == 0 else 'SUPPORTING'} for i, a in enumerate(ids)],
                  'events': [{'event_id': 'E%d' % i, 'kind': 'OBSERVE', 'actors': [ids[i % len(ids)]],
                              'time_s': round(duration * (i + 1) / 4.0, 3)} for i in range(3)],
                  'atmosphere': {'mood': 'neutral', 'mood_preset': 'NEUTRAL_THREE_POINT'}}
        brief = call('cinematic_create_brief', 'shot_planning', brief=intent)['brief']
        beats = call('cinematic_plan_beats', 'shot_planning', brief=brief)
        shots = call('cinematic_plan_shots', 'shot_planning', brief=brief, options={'beats': beats})
        created = call('cinematic_create_candidate', 'candidate', brief=brief, apply=True,
                       options={'beats': beats, 'shots': shots})
        ref = created['cinematic']
        call('cinematic_stage_actors', 'staging', cinematic=ref, apply=True,
             options={'actors': [{'actor_id': a, 'location': [0.0, 0.0, 0.0], 'yaw_deg': 0.0} for a in ids]})
        frames = row['frames']
        clips = [{'actor_id': a, 'action': r['name'] + '_Idle', 'start_frame': 1, 'action_frame_start': 1,
                  'action_frame_end': 61, 'repeat': max(1.0, round(frames / 60.0, 3)), 'extrapolation': 'HOLD'}
                 for a, r in zip(ids, rows)]
        call('cinematic_sync_actions', 'actor_sync', cinematic=ref, apply=True, options={'clips': clips})
        for i, shot in enumerate(shots['shots']):
            camera = shot['camera_id']
            call('cinematic_create_camera', 'camera_setup', cinematic=ref, apply=True, options={
                'camera_id': camera, 'shot_id': shot['shot_id'], 'lens_mm': 35, 'location': [0.6 * i, -6.0, 1.6],
                'look_at': {'point': [0.0, 0.0, 1.0]}})
            call('cinematic_animate_camera', 'camera_setup', cinematic=ref, apply=True, options={
                'camera_id': camera, 'shot_id': shot['shot_id'], 'move': 'STATIC'})
        call('cinematic_create_cuts', 'timeline', cinematic=ref, apply=True, options={'from_shots': True})
        call('cinematic_manage_timeline', 'timeline', cinematic=ref, apply=True, options={'finalize': True})
        call('cinematic_verify_integrity', 'scene_validation', cinematic=ref)
        for name in ('cinematic_analyze_composition', 'cinematic_validate_continuity', 'cinematic_analyze_visibility'):
            call(name, 'scene_validation', cinematic=ref)
        vertices = sum(len(o.data.vertices) for o in bpy.data.objects if o.type == 'MESH')
        step = max(1, math.ceil(vertices * frames / COLLISION_CAPACITY))
        row['collision_step'] = {'step': step, 'mesh_vertices': vertices, 'source': 'declared capacity 3,000,000 vertex-frames'}
        call('cinematic_analyze_actor_collisions', 'scene_validation', cinematic=ref, options={'step': step} if step > 1 else None)
        preview = call('cinematic_render_preview', 'preview', cinematic=ref, options={'size': 320})
        row['preview'] = {k: preview.get(k) for k in ('temporal_complete', 'frames', 'render_backend')}
        row['preview']['metrics'] = preview.get('metrics')
    except Exception as exc:
        row['errors'].append({'error': repr(exc)[:600], 'traceback': traceback.format_exc()[-1500:]})
    row['memory'] = memory_mb()
    row['total_seconds'] = round(sum(s['seconds'] for s in row['stages'].values()), 3)
    return row


def main():
    args = sys.argv[sys.argv.index('--') + 1:]
    out = Path(args[0])
    out.mkdir(parents=True, exist_ok=True)
    wanted = args[1].split(',') if len(args) > 1 else None
    report = {'suite': 'm10_performance_bench', 'blender': bpy.app.version_string, 'gpu': None, 'configs': []}
    try:
        import gpu
        report['gpu'] = gpu.platform.renderer_get()
    except Exception as exc:
        report['gpu'] = 'unavailable: ' + repr(exc)[:120]
    for actors in (1, 2, 3):
        for duration in (3, 15):
            for fps in (30, 60):
                key = 'a%d_%ds_%dfps' % (actors, duration, fps)
                if wanted and key not in wanted:
                    continue
                began = time.perf_counter()
                row = run_config(out, actors, duration, fps)
                row['wall_seconds'] = round(time.perf_counter() - began, 3)
                report['configs'].append(row)
                (out / 'm10_performance_bench.json').write_text(json.dumps(report, indent=1, default=str), encoding='utf-8')
                print('CONFIG ' + key + ' ' + str(row['total_seconds']) + ' s errors=' + str(len(row['errors'])), flush=True)
    print('BENCH_DONE', flush=True)


main()
