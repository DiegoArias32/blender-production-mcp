"""Real-Blender qualification of the M3.1 mesh safety foundation (gates P, Q, R).

A generated fixture (never a production asset) of about 150k triangles carries every dependency the
candidate must preserve: armature parent + modifier, vertex groups, shape keys with a driver, two UV
layers across UDIM tiles, custom normals, two material slots, face sets and custom properties.

Checks use bpy directly (not the signature code) wherever they judge the production result:
- signature: timing and response size at ~150k triangles;
- candidate: independent comparison of every declared dependency and data-block separation;
- regions: locked-region edits of 1 mm, weight edits, new shape keys and topology edits must fail;
  editable-region edits must pass.

Large files stay in %TEMP%\\bpm31; only the JSON report is promoted.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import types
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
PROMOTED = ROOT / 'test-artifacts' / 'm3.1-mesh'


def build_fixture(bpy, folder, grid):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    armature = bpy.data.armatures.new('RigData')
    rig = bpy.data.objects.new('Rig', armature)
    bpy.context.scene.collection.objects.link(rig)
    bpy.context.view_layer.objects.active = rig
    bpy.ops.object.mode_set(mode='EDIT')
    for name, head, tail in (('Root', (0, 0, 0), (0, 0, 0.5)), ('Head', (0, 0, 0.5), (0, 0, 1.0))):
        bone = armature.edit_bones.new(name)
        bone.head, bone.tail = head, tail
    armature.edit_bones['Head'].parent = armature.edit_bones['Root']
    bpy.ops.object.mode_set(mode='OBJECT')

    bpy.ops.mesh.primitive_grid_add(x_subdivisions=grid, y_subdivisions=grid, size=2)
    body = bpy.context.object
    body.name = 'Body'
    me = body.data
    me.name = 'BodyMesh'
    body.parent = rig
    modifier = body.modifiers.new('Armature', 'ARMATURE')
    modifier.object = rig
    count = len(me.vertices)
    face = [v.index for v in me.vertices if v.co.y > 0.6]
    torso = [v.index for v in me.vertices if v.co.y < -0.2]
    body.vertex_groups.new(name='Head').add(face, 1.0, 'REPLACE')
    body.vertex_groups.new(name='Root').add(torso, 0.75, 'REPLACE')
    body.vertex_groups.new(name='Face').add(face, 1.0, 'REPLACE')
    body.vertex_groups.new(name='Torso').add(torso, 1.0, 'REPLACE')
    body.shape_key_add(name='Basis')
    smile = body.shape_key_add(name='Smile')
    for index in face[:200]:
        smile.data[index].co.z += 0.01
    driver = smile.driver_add('value').driver
    driver.type = 'AVERAGE'
    variable = driver.variables.new()
    variable.type = 'TRANSFORMS'
    variable.targets[0].id = rig
    variable.targets[0].bone_target = 'Head'
    variable.targets[0].transform_type = 'LOC_Z'
    second = me.uv_layers.new(name='UDIM')
    values = [0.0] * (2 * len(me.loops))
    second.uv.foreach_get('vector', values)
    starts = [0] * len(me.polygons)
    me.polygons.foreach_get('loop_start', starts)
    for polygon, start in enumerate(starts):
        if polygon % 2:
            for corner in range(start, start + me.polygons[polygon].loop_total):
                values[2 * corner] += 1.0  # odd faces on tile 1002
    second.uv.foreach_set('vector', values)
    normals = [(0.0, 0.2, 0.98) for _ in range(count)]
    me.normals_split_custom_set_from_vertices(normals)
    for name in ('Skin', 'Metal'):
        body.data.materials.append(bpy.data.materials.new(name))
    indices = [polygon % 2 for polygon in range(len(me.polygons))]
    me.polygons.foreach_set('material_index', indices)
    face_sets = me.attributes.new('.sculpt_face_set', 'INT', 'FACE')
    centers = [0.0] * (3 * len(me.polygons))
    me.polygons.foreach_get('center', centers)
    face_sets.data.foreach_set('value', [1 if centers[3 * p + 1] > 0.6 else 2 for p in range(len(me.polygons))])
    body['asset_note'] = 'generated M3.1 fixture'
    me['lod'] = 0
    path = folder / 'mesh_fixture.blend'
    bpy.ops.wm.save_as_mainfile(filepath=str(path))
    return path, {'vertices': count, 'face': len(face), 'torso': len(torso)}


def inside(output, label, grid):
    import bpy
    sys.path.insert(0, str(ROOT / 'src'))
    package = types.ModuleType('blender_mcp')
    package.__path__ = [str(ROOT / 'src/blender_mcp')]
    sys.modules['blender_mcp'] = package
    from blender_mcp.production.common.bridge import build_code
    from blender_mcp.production.common.storage import Storage
    from blender_mcp.production.common.asset_safety import register_fixture
    from blender_mcp.production.mesh import service
    from blender_mcp.production.mesh.runtime import SIGNATURE

    class LocalBridge:
        def run(self, body, params=None):
            namespace = {}
            exec(build_code(body, params).rsplit('print(', 1)[0], namespace)
            return namespace['result']

    bridge = LocalBridge()
    storage = Storage(output / 's')
    report = {'label': label, 'blender': bpy.app.version_string, 'python': sys.version.split()[0], 'checks': {},
              'timings_s': {}}

    def save():
        (output / 'report.json').write_text(json.dumps(report, indent=2, default=str), encoding='utf-8')

    def check(name, passed, **details):
        report['checks'][name] = {'passed': bool(passed), **details}
        save()
        print('CHECK', name, 'PASS' if passed else 'FAIL', flush=True)

    def timed(name, function):
        started = time.perf_counter()
        value = function()
        report['timings_s'][name] = round(time.perf_counter() - started, 4)
        return value

    try:
        path, fixture = timed('build_fixture', lambda: build_fixture(bpy, output, grid))
        register_fixture(bridge, storage, str(path))
        body = bpy.data.objects['Body']
        me = body.data
        report['fixture'] = dict(fixture, triangles=sum(p.loop_total - 2 for p in me.polygons), path=str(path))

        # ---------------------------------------------------------------- signature (phase 18)
        raw = timed('signature_raw_1', lambda: bridge.run(SIGNATURE, {'object_name': 'Body'}))
        timed('signature_raw_2', lambda: bridge.run(SIGNATURE, {'object_name': 'Body'}))
        report['signature_response_bytes'] = len(json.dumps(raw))
        signed = timed('mesh_signature_tool_path', lambda: service.mesh_signature(bridge, storage, 'sig', 'Body'))
        report['compact_summary_bytes'] = len(json.dumps(signed['signature']))
        summary = signed['signature']
        check('signature_counts', summary['counts']['vertices'] == len(me.vertices)
              and summary['counts']['triangles'] == report['fixture']['triangles'], counts=summary['counts'])
        check('signature_udim_tiles', summary['uv_layers'][1]['udim_tiles'] == [1001, 1002], uv=summary['uv_layers'])
        check('signature_sections', set(summary['fingerprints']) == {'topology', 'geometry', 'shape_keys', 'vertex_groups',
              'uv_layers', 'normals', 'materials', 'modifiers', 'relationships', 'transform', 'custom_properties', 'attributes'},
              normals=summary['normals'], armature_modifiers=summary['armature_modifiers'])
        again = bridge.run(SIGNATURE, {'object_name': 'Body'})
        check('signature_deterministic', again['fingerprint'] == raw['fingerprint'])
        me.vertices[0].co.x += 1e-4
        moved = bridge.run(SIGNATURE, {'object_name': 'Body'})
        me.vertices[0].co.x -= 1e-4
        changed = sorted(k for k in raw['fingerprints'] if moved['fingerprints'][k] != raw['fingerprints'][k])
        check('signature_detects_0p1mm_vertex_move', changed == ['geometry'], changed_sections=changed)
        body.vertex_groups['Face'].add([0], 0.5, 'REPLACE')
        weighted = bridge.run(SIGNATURE, {'object_name': 'Body'})
        body.vertex_groups['Face'].remove([0])
        check('signature_detects_weight_edit', weighted['fingerprints']['vertex_groups'] != raw['fingerprints']['vertex_groups'])
        bpy.context.view_layer.objects.active = body
        bpy.ops.object.mode_set(mode='EDIT')
        try:
            bridge.run(SIGNATURE, {'object_name': 'Body'})
            check('signature_refuses_edit_mode', False)
        except ValueError as exc:
            check('signature_refuses_edit_mode', 'MODE_NOT_SUPPORTED' in str(exc), error=str(exc)[:200])
        finally:
            bpy.ops.object.mode_set(mode='OBJECT')
        restored = bridge.run(SIGNATURE, {'object_name': 'Body'})
        check('signature_restored_after_undo_edits', restored['fingerprint'] == raw['fingerprint'],
              changed_sections=sorted(k for k in raw['fingerprints'] if restored['fingerprints'][k] != raw['fingerprints'][k]),
              attributes_before=raw['sections']['attributes']['attributes'], attributes_after=restored['sections']['attributes']['attributes'],
              normals_before={k: raw['sections']['normals'][k] for k in ('domain', 'has_custom_normals', 'sharp')},
              normals_after={k: restored['sections']['normals'][k] for k in ('domain', 'has_custom_normals', 'sharp')})
        source_fp = raw['fingerprint']

        # ---------------------------------------------------------------- candidate (phase 19)
        objects_before = len(bpy.data.objects)
        created = timed('candidate_create', lambda: service.create_candidate(bridge, storage, 'cand', 'Body', source_fp))
        candidate = bpy.data.objects[created['candidate']]
        check('candidate_preservation_reported', created['status'] == 'success' and all(v['preserved'] for v in created['preservation'].values()),
              preservation=created['preservation'], not_verified=created['not_verified'])
        cme = candidate.data

        def floats(collection, attribute, width):
            values = [0.0] * (len(collection) * width)
            collection.foreach_get(attribute, values)
            return values
        independent = {
            'separate_object_and_mesh': candidate != body and cme != me,
            'separate_shape_key_block': cme.shape_keys is not None and cme.shape_keys != me.shape_keys,
            'parent': candidate.parent == body.parent and candidate.parent_type == body.parent_type,
            'armature_modifier': [(m.type, m.object) for m in candidate.modifiers] == [(m.type, m.object) for m in body.modifiers],
            'vertex_group_names': [g.name for g in candidate.vertex_groups] == [g.name for g in body.vertex_groups],
            'weights': [[(g.group, g.weight) for g in v.groups] for v in cme.vertices] == [[(g.group, g.weight) for g in v.groups] for v in me.vertices],
            'shape_keys': [k.name for k in cme.shape_keys.key_blocks] == [k.name for k in me.shape_keys.key_blocks]
                          and all(floats(a.data, 'co', 3) == floats(b.data, 'co', 3) for a, b in zip(cme.shape_keys.key_blocks, me.shape_keys.key_blocks)),
            'shape_key_driver': bool(cme.shape_keys.animation_data and len(cme.shape_keys.animation_data.drivers) == 1),
            'materials': [s.material for s in candidate.material_slots] == [s.material for s in body.material_slots],
            'uv_layers': [l.name for l in cme.uv_layers] == [l.name for l in me.uv_layers]
                         and all(floats(a.uv, 'vector', 2) == floats(b.uv, 'vector', 2) for a, b in zip(cme.uv_layers, me.uv_layers)),
            'custom_normals': cme.has_custom_normals and floats(cme.corner_normals, 'vector', 3) == floats(me.corner_normals, 'vector', 3),
            'custom_properties': candidate.get('asset_note') == body.get('asset_note') and cme.get('lod') == me.get('lod'),
            'hidden_from_render_only': candidate.hide_render and not body.hide_render,
        }
        check('candidate_independent_dependency_check', all(independent.values()), details=independent)
        check('candidate_source_intact', created['source_intact']
              and bridge.run(SIGNATURE, {'object_name': 'Body'})['fingerprint'] == source_fp)
        reused = service.create_candidate(bridge, storage, 'cand2', 'Body', source_fp)
        check('candidate_create_is_idempotent', reused['reused'] and reused['candidate'] == candidate.name
              and len(bpy.data.objects) == objects_before + 1, objects_before=objects_before, objects_after=len(bpy.data.objects))
        try:
            service.create_candidate(bridge, storage, 'stale', 'Body', 'm521:0:0:0')
            check('candidate_stale_signature_refused', False)
        except ValueError as exc:
            check('candidate_stale_signature_refused', 'STALE_SIGNATURE' in str(exc), error=str(exc)[:200])

        # ---------------------------------------------------------------- regions (phase 20)
        regions = [{'name': 'Face', 'role': 'locked', 'definition': {'method': 'vertex_group', 'name': 'Face'}, 'tolerance_m': 1e-5},
                   {'name': 'FaceSet', 'role': 'locked', 'definition': {'method': 'face_set', 'ids': [1]}, 'tolerance_m': 1e-5},
                   {'name': 'Torso', 'role': 'editable', 'definition': {'method': 'vertex_group', 'name': 'Torso'}}]
        defined = timed('region_define', lambda: service.define_regions(bridge, storage, 'reg', 'Body', regions))
        manifest = defined['manifest']
        report['region_manifest_bytes'] = os.path.getsize(manifest)
        check('regions_defined', [r['vertices'] for r in defined['regions']][0] == fixture['face'], regions=defined['regions'])
        clean = timed('region_verify_candidate', lambda: service.verify_regions(bridge, storage, manifest, candidate.name))
        check('regions_pass_on_untouched_candidate', clean['passed'], regions=clean['regions'])

        def scenario(name, mutate, undo, expect_pass, code=None):
            mutate()
            try:
                result = service.verify_regions(bridge, storage, manifest, candidate.name)
            finally:
                undo()
            failures = result['errors']
            ok = result['passed'] is expect_pass and (code is None or any(f.startswith(code) for f in failures))
            check(name, ok, verification_passed=result['passed'], errors=failures,
                  regions=[{k: r.get(k) for k in ('name', 'role', 'max_displacement_m', 'worst_vertex', 'failures')} for r in result['regions']])

        locked_vertex = next(v.index for v in cme.vertices if v.co.y > 0.8)
        editable_vertex = next(v.index for v in cme.vertices if v.co.y < -0.5)
        basis = cme.shape_keys.key_blocks['Basis']

        def move(index, dz):
            def run():
                cme.vertices[index].co.z += dz
                basis.data[index].co.z += dz
            return run
        scenario('editable_edit_5cm_passes', move(editable_vertex, 0.05), move(editable_vertex, -0.05), True)
        scenario('locked_edit_1mm_fails', move(locked_vertex, 0.001), move(locked_vertex, -0.001), False, 'LOCKED_REGION_MODIFIED')
        scenario('locked_weight_edit_fails', lambda: candidate.vertex_groups['Head'].add([locked_vertex], 0.5, 'REPLACE'),
                 lambda: candidate.vertex_groups['Head'].add([locked_vertex], 1.0, 'REPLACE'), False, 'LOCKED_WEIGHTS_CHANGED')

        def add_key():
            key = candidate.shape_key_add(name='Bulge', from_mix=False)
            key.data[locked_vertex].co.z += 0.002

        def remove_key():
            candidate.shape_key_remove(cme.shape_keys.key_blocks['Bulge'])
        scenario('new_shape_key_on_locked_fails', add_key, remove_key, False, 'NEW_SHAPE_KEY_MOVES_LOCKED_REGION')
        check('candidate_back_to_clean_state', service.verify_regions(bridge, storage, manifest, candidate.name)['passed'])

        import bmesh
        edited = bmesh.new()
        edited.from_mesh(cme)
        edited.verts.ensure_lookup_table()
        bmesh.ops.delete(edited, geom=[edited.verts[editable_vertex]], context='VERTS')
        edited.to_mesh(cme)
        edited.free()
        topology = service.verify_regions(bridge, storage, manifest, candidate.name)
        check('topology_change_fails_locked_regions', not topology['passed']
              and any(f.startswith('TOPOLOGY_CHANGED') or f.startswith('REGION_UNDEFINED') for f in topology['errors']),
              errors=topology['errors'])
        verified = service.verify_candidate(bridge, storage, 'Body', candidate.name, manifest)
        check('candidate_verify_reports_regions_and_source', verified['status'] == 'error' and verified['source_unchanged_since_candidate'],
              differences=verified['differences'], errors=verified['errors'][:5])
        check('source_intact_after_all_candidate_edits', bridge.run(SIGNATURE, {'object_name': 'Body'})['fingerprint'] == source_fp)
    except Exception as exc:
        report['exception'] = repr(exc)
        report['traceback'] = traceback.format_exc()[-4000:]
    report['passed'] = bool(report['checks']) and 'exception' not in report and all(c['passed'] for c in report['checks'].values())
    save()
    print('M31_MESH_REPORT=' + str(output / 'report.json'))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--blender', required=True)
    parser.add_argument('--label', default='mesh')
    parser.add_argument('--grid', type=int, default=276, help='grid subdivisions; 276 gives 151250 triangles')
    args = parser.parse_args()
    run_id = uuid4().hex[:8]
    output = Path(tempfile.gettempdir()) / 'bpm31' / (args.label + '-' + run_id)
    output.mkdir(parents=True)
    process = subprocess.run([args.blender, '--background', '--factory-startup', '--disable-autoexec', '--python',
                              str(Path(__file__).resolve()), '--', '--inside', str(output), args.label, str(args.grid)],
                             capture_output=True, text=True, encoding='utf-8', errors='replace')
    (output / 'stdout.txt').write_text(process.stdout, encoding='utf-8')
    (output / 'stderr.txt').write_text(process.stderr, encoding='utf-8')
    print('\n'.join(line for line in process.stdout.splitlines() if line.startswith(('CHECK', 'M31_'))))
    report_path = output / 'report.json'
    if not report_path.exists():
        print(process.stdout[-4000:])
        print(process.stderr[-4000:])
        raise SystemExit(1)
    PROMOTED.mkdir(parents=True, exist_ok=True)
    promoted = PROMOTED / (args.label + '-' + run_id + '.json')
    shutil.copyfile(report_path, promoted)
    print('PROMOTED=' + str(promoted))
    print('ARTIFACTS=' + str(output))
    report = json.loads(report_path.read_text(encoding='utf-8'))
    raise SystemExit(0 if report.get('passed') else 1)


if __name__ == '__main__':
    if '--inside' in sys.argv:
        position = sys.argv.index('--inside')
        inside(Path(sys.argv[position + 1]), sys.argv[position + 2], int(sys.argv[position + 3]))
    else:
        main()
