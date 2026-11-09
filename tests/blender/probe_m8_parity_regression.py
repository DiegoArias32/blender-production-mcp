"""Regression for the M8 collision parity ray step (fixed during M10 with the user's authorization).

Measured before the fix (ray step 1e-7 m): a pendant that never enters the walking body proxy was reported inside it
(14 vertex-frame pairs, 0.1431 m), and a 12-triangle box collider raised COLLISION_RAY_CAPACITY_EXCEEDED. The guard
must still catch a real penetration.

    blender -b --factory-startup --disable-autoexec --python tests/blender/probe_m8_parity_regression.py -- <out_dir>
"""
from __future__ import annotations

import json
import sys
import threading
import traceback
import types
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

REPORT = {'suite': 'm8_parity_regression', 'blender': bpy.app.version_string, 'checks': [], 'errors': []}


class LocalBridge:
    lock = threading.RLock()

    def run(self, body, params=None):
        ns = {}
        exec(build_code(body, params).rsplit('print(', 1)[0], ns)
        return ns['result']


def check(out, name, condition, data=None):
    REPORT['checks'].append({'name': name, 'passed': bool(condition), 'data': data})
    print(('PASS ' if condition else 'FAIL ') + name, flush=True)
    (out / 'm8_parity_regression.json').write_text(json.dumps(REPORT, indent=1, default=str), encoding='utf-8')


def scenario(out, label, pendant_offset_y=0.0, box_collider=False):
    """Demo C hero walking with its pendant; returns M8's technical QA after the accessory generation."""
    from blender_mcp.production.physics.service import dispatch as P, load
    folder = out / label
    manifest = FX.build_demo('C_body_face_physics', folder / 'fx', save=True)
    bpy.ops.wm.open_mainfile(filepath=manifest['blend'], load_ui=False, use_scripts=False)
    hero = manifest['actors'][0]
    rig = bpy.data.objects['Hero']
    rig.animation_data.action = bpy.data.actions['Hero_Walk']
    bpy.context.scene.frame_start, bpy.context.scene.frame_end = 1, 91
    pendant = hero['secondary'][0]
    obj = bpy.data.objects[pendant['object']]
    if pendant_offset_y:
        # Push the pendant back into the chest: a REAL penetration the guard must still report.
        bpy.context.view_layer.update()
        world = obj.matrix_world.copy()
        world.translation.y += pendant_offset_y
        obj.matrix_world = world
    colliders = list(pendant['colliders_hint'])
    if box_collider:
        mesh = bpy.data.meshes.new('ProbeBox')
        corners = [(x, y, z) for z in (1.06, 1.46) for y in (-.12, .12) for x in (-.21, .21)]
        mesh.from_pydata(corners, [], [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)])
        box = bpy.data.objects.new('ProbeBox', mesh)
        bpy.data.collections['ACTOR_Hero'].objects.link(box)
        box.parent, box.parent_type, box.parent_bone = rig, 'BONE', 'chest'
        bpy.context.view_layer.update()
        box.matrix_world = box.matrix_world.Identity(4)
        box.hide_render = True
        colliders = [box.name]
    bpy.ops.wm.save_mainfile()
    bridge = LocalBridge()
    storage = Storage(folder / 'store')
    register_open_fixture(bridge, storage, out)
    options = {'primary_objects': [hero['object'], hero['body']], 'colliders': colliders,
               'anchor_object': hero['object'], 'anchor_bone': 'chest', 'manifest': pendant['m8_manifest']}
    brief = dict(pendant['m8_brief_hint'], duration_s=3.0, frame_start=1, frame_end=91)
    ref = P('physics_plan', bridge, storage, label + '-plan', pendant['object'], None, 'pendant', brief, options, False)['physics']
    P('physics_create_candidate', bridge, storage, label + '-cand', None, ref, None, None, None, True)
    P('physics_prepare_colliders', bridge, storage, label + '-col', None, ref, None, None, None, True)
    P('physics_generate_accessory_motion', bridge, storage, label + '-gen', None, ref, None, None, None, True)
    return load(storage, ref)[2]['technical_qa']


def main():
    out = Path(sys.argv[sys.argv.index('--') + 1])
    out.mkdir(parents=True, exist_ok=True)
    try:
        qa = scenario(out, 'walk_body_proxy')
        collision = qa['collision']
        check(out, 'pendant beside the walking body proxy: no false penetration (was 14 pairs / 0.1431 m)',
              collision['passed'] and collision['metrics']['penetration_vertex_frame_pairs'] == 0, collision['metrics'])
        check(out, 'secondary motion and primary integrity still measured', qa['motion']['passed'] and qa['primary_integrity']['passed'])
        qa = scenario(out, 'walk_box_collider', box_collider=True)
        errors = qa['collision'].get('errors') or []
        check(out, 'box collider: no COLLISION_RAY_CAPACITY_EXCEEDED (was raised on 12 triangles)',
              not any('COLLISION_RAY_CAPACITY_EXCEEDED' in str(e) for e in errors), {'errors': errors, 'metrics': qa['collision']['metrics']})
        qa = scenario(out, 'real_penetration', pendant_offset_y=0.12)
        collision = qa['collision']
        check(out, 'pendant pushed 12 cm into the chest: penetration still detected (guard not weakened)',
              not collision['passed'] and collision['metrics']['penetration_vertex_frame_pairs'] > 0
              and (collision['metrics']['max_penetration_depth_m'] or 0) > 0.01, collision['metrics'])
    except Exception as exc:
        REPORT['errors'].append({'error': repr(exc), 'traceback': traceback.format_exc()})
        traceback.print_exc()
    REPORT['summary'] = {'passed': sum(c['passed'] for c in REPORT['checks']),
                         'failed': sum(not c['passed'] for c in REPORT['checks']), 'errors': len(REPORT['errors'])}
    (out / 'm8_parity_regression.json').write_text(json.dumps(REPORT, indent=1, default=str), encoding='utf-8')
    print('SUMMARY ' + json.dumps(REPORT['summary']), flush=True)


main()
