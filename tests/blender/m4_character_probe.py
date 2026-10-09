"""Real-Blender qualification of M4 (character modeling & redesign) on generated fixtures only.

Runs in its own `blender --background --factory-startup --disable-autoexec` process with an
in-process bridge: it never connects to a user's Blender and never opens a production file.

Every production result that matters is judged independently with bpy (positions, weights, UVs,
materials, shape keys, modifiers read directly), not with the signature code under test.

Stages (comma separated with --stages):
    core       fixture inventory, inspection, regions, analyses, candidate, full edit cycle
    negatives  MASTER refusal, locked edit, stale topology, shape keys, UV/weight/material, fingerprint,
               idempotent retries, ambiguous regions, bad deformation
    demos      Demo A (humanoid, MASTER -> WORKING COPY -> candidate) and Demo B (asymmetric creature)
    perf       ~100k-150k triangle timings of the required operations

Large files stay in %TEMP%\\bpm4; the JSON report is promoted to test-artifacts/m4.
"""
import argparse
import hashlib
import json
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
PROMOTED = ROOT / 'test-artifacts' / 'm4'
STAGES = ('core', 'negatives', 'demos', 'perf')
STRESS = [{'region': 'shoulder', 'bone': 'upper_arm.L', 'axis': 'Z', 'angle_degrees': 45},
          {'region': 'shoulder', 'bone': 'upper_arm.R', 'axis': 'Z', 'angle_degrees': -45},
          {'region': 'elbow', 'bone': 'forearm.L', 'axis': 'Z', 'angle_degrees': 60},
          {'region': 'hip', 'bone': 'thigh.L', 'axis': 'X', 'angle_degrees': 40}]
DEMO_A_INTENT = ('Increase upper-body mass and improve shoulder/back transition while preserving the face and '
                 'rig dependencies')
DEMO_B_INTENT = 'Improve the torso transition without enforcing symmetry; keep the oversized arm and the hump'


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def inside(output, label, stages):
    import bpy
    sys.path.insert(0, str(ROOT / 'src'))
    sys.path.insert(0, str(ROOT / 'tests' / 'blender'))
    package = types.ModuleType('blender_mcp')
    package.__path__ = [str(ROOT / 'src/blender_mcp')]
    sys.modules['blender_mcp'] = package
    from blender_mcp.production.common.bridge import build_code, payload_bytes
    from blender_mcp.production.common.storage import Storage
    from blender_mcp.production.common.asset_safety import create_working_copy, current_asset, register_master
    from blender_mcp.production.character import service
    from blender_mcp.production.character.transaction import adjust_shape
    from blender_mcp.production.mesh.runtime import SIGNATURE
    from m31_fixture_support import register_open_fixture
    import m4_character_fixtures as fixtures

    class LocalBridge:
        def __init__(self):
            self.sizes = []

        def run(self, body, params=None):
            code = build_code(body, params)
            self.sizes.append(len(code.encode('utf-8')))
            namespace = {}
            exec(code.rsplit('print(', 1)[0], namespace)
            return namespace['result']

    bridge = LocalBridge()
    storage = Storage(output / 's')
    report = {'label': label, 'stages': stages, 'blender': bpy.app.version_string, 'python': sys.version.split()[0],
              'checks': {}, 'timings_s': {}, 'fixtures': {}, 'demos': {}, 'performance': {}, 'rejected_by_safety': []}

    def save():
        (output / 'report.json').write_text(json.dumps(report, indent=2, default=str), encoding='utf-8')

    def check(name, passed, **details):
        report['checks'][name] = dict({'passed': bool(passed)}, **details)
        save()
        print('CHECK', name, 'PASS' if passed else 'FAIL', flush=True)

    def timed(name, function):
        started = time.perf_counter()
        value = function()
        report['timings_s'][name] = round(time.perf_counter() - started, 4)
        return value

    def refused(name, function, code, record=True):
        """The call must fail with this error code; returns the message."""
        try:
            value = function()
        except Exception as exc:
            message = str(exc)
            if record:
                report['rejected_by_safety'].append({'check': name, 'error': message[:300]})
            return code in message, message[:300]
        text = json.dumps(value, default=str)
        if code in text and (value.get('status') == 'error' or not value.get('proposal', {}).get('supported', True)):
            if record:
                report['rejected_by_safety'].append({'check': name, 'error': text[:300]})
            return True, text[:300]
        return False, 'no refusal: ' + text[:300]

    def fixture(variant, folder='fixtures'):
        info = fixtures.build(output / folder, variant)
        register_open_fixture(bridge, storage, output)
        report['fixtures'][variant] = {k: info[k] for k in ('vertices', 'triangles', 'groups', 'materials', 'uv_layers', 'shape_keys')}
        return info

    # ------------------------------------------------------------------ independent bpy snapshots
    def floats(collection, attribute, width):
        values = [0.0] * (len(collection) * width)
        collection.foreach_get(attribute, values)
        return values

    def ints(collection, attribute, width):
        values = [0] * (len(collection) * width)
        collection.foreach_get(attribute, values)
        return values

    def state(name):
        obj = bpy.data.objects[name]
        me = obj.data
        groups = [g.name for g in obj.vertex_groups]
        weights = [sorted((groups[e.group], round(e.weight, 7)) for e in v.groups) for v in me.vertices]
        return {'co': floats(me.vertices, 'co', 3), 'edges': ints(me.edges, 'vertices', 2),
                'loops': ints(me.loops, 'vertex_index', 1), 'material_index': ints(me.polygons, 'material_index', 1),
                'materials': [m.name if m else None for m in me.materials], 'groups': groups, 'weights': weights,
                'uv': {layer.name: floats(layer.uv, 'vector', 2) for layer in me.uv_layers},
                'keys': {k.name: floats(k.data, 'co', 3) for k in me.shape_keys.key_blocks} if me.shape_keys else {},
                'relative': {k.name: k.relative_key.name for k in me.shape_keys.key_blocks} if me.shape_keys else {},
                'modifiers': [(m.type, m.name, m.object.name if getattr(m, 'object', None) else None, m.show_viewport) for m in obj.modifiers],
                'parent': (obj.parent.name if obj.parent else None, obj.parent_type), 'mesh': me.name,
                'custom_normal': ints(me.attributes['custom_normal'].data, 'value', 2) if 'custom_normal' in me.attributes else None}

    def displacement(a, b, indices):
        worst = 0.0
        for i in indices:
            d = ((a['co'][3 * i] - b['co'][3 * i]) ** 2 + (a['co'][3 * i + 1] - b['co'][3 * i + 1]) ** 2
                 + (a['co'][3 * i + 2] - b['co'][3 * i + 2]) ** 2) ** 0.5
            worst = max(worst, d)
        return worst

    def preserved(a, b):
        """Independent preservation verdicts between two snapshots (topology, weights, UV, materials, rig, keys)."""
        verdict = {'topology': a['edges'] == b['edges'] and a['loops'] == b['loops'],
                   'weights': a['groups'] == b['groups'] and a['weights'] == b['weights'],
                   'uv': a['uv'] == b['uv'], 'materials': a['materials'] == b['materials'] and a['material_index'] == b['material_index'],
                   'armature_modifier': a['modifiers'] == b['modifiers'], 'parent': a['parent'] == b['parent'],
                   'custom_normals_stored': a['custom_normal'] == b['custom_normal']}
        if a['keys']:
            names_equal = sorted(a['keys']) == sorted(b['keys'])
            offsets_error = 0.0
            if names_equal:
                for name, data in a['keys'].items():
                    ref_a, ref_b = a['keys'][a['relative'][name]], b['keys'][b['relative'][name]]
                    other = b['keys'][name]
                    offsets_error = max([offsets_error] + [abs((data[k] - ref_a[k]) - (other[k] - ref_b[k])) for k in range(len(data))])
            verdict['shape_key_offsets'] = names_equal and offsets_error <= 1e-6
            verdict['shape_key_offset_max_error'] = offsets_error
        return verdict

    def group_members(name, group, minimum=0.5):
        obj = bpy.data.objects[name]
        index = obj.vertex_groups[group].index
        return [v.index for v in obj.data.vertices if any(e.group == index and e.weight >= minimum for e in v.groups)]

    def world_bounds(name):
        obj = bpy.data.objects[name]
        points = [obj.matrix_world @ v.co for v in obj.data.vertices]
        return [min(p[k] for p in points) for k in range(3)], [max(p[k] for p in points) for k in range(3)]

    def signature(name):
        return bridge.run(SIGNATURE, {'object_name': name})['fingerprint']

    def checkpoints():
        folder = storage.root / 'checkpoints'
        return sorted(p.name for p in folder.iterdir()) if folder.exists() else []

    def edit_cycle(prefix, candidate, manifest, operation, shape_keys='require_unaffected'):
        options = {'regions_manifest': manifest, 'operation': operation, 'shape_keys': shape_keys}
        proposal = timed(prefix + '_propose', lambda: adjust_shape(bridge, storage, prefix + '-propose', candidate, 'propose', options))
        return proposal

    # ================================================================== core
    def stage_core():
        info = timed('build_humanoid', lambda: fixture('humanoid'))
        body = info['body']
        bpy_counts = {'vertices': len(bpy.data.objects[body].data.vertices),
                      'triangles': sum(len(p.vertices) - 2 for p in bpy.data.objects[body].data.polygons)}
        inspected = timed('inspect_humanoid', lambda: service.inspect(bridge, storage, body))
        ids = {r['region_id']: r for r in inspected['regions']}
        check('inspect_counts_match_bpy', inspected['counts']['vertices'] == bpy_counts['vertices']
              and inspected['counts']['triangles'] == bpy_counts['triangles'], counts=inspected['counts'], bpy=bpy_counts)
        low, high = world_bounds(body)
        check('inspect_dimensions_match_bpy', all(abs(inspected['dimensions_m'][k] - (high[k] - low[k])) < 1e-6 for k in range(3)),
              dimensions=inspected['dimensions_m'])
        check('inspect_asset_role_fixture', inspected['asset']['role'] == 'FIXTURE', asset=inspected['asset'])
        expected = {'FACE', 'HEAD', 'SHOULDERS', 'UPPER_BACK', 'HANDS', 'TORSO', 'LEFT_ARM', 'RIGHT_ARM', 'LEFT_LEG', 'RIGHT_LEG',
                    'LEFT_HAND', 'RIGHT_HAND', 'LEFT_FOOT', 'RIGHT_FOOT', 'LEFT_SHOULDER', 'RIGHT_SHOULDER', 'NECK', 'CHEST', 'PELVIS'}
        check('inspect_region_candidates', expected <= set(ids) and all(ids[k]['source'] == 'automatic' for k in ids),
              found=sorted(ids), missing=sorted(expected - set(ids)),
              confidence={k: ids[k]['confidence'] for k in sorted(ids)})
        check('inspect_symmetry_humanoid', inspected['symmetry']['symmetric_fraction'] >= 0.95, symmetry=inspected['symmetry'])
        check('inspect_rig_state', inspected['armatures'] == [info['rig']] and inspected['deformation']['unweighted_vertices'] == 0,
              deformation=inspected['deformation'], topology=inspected['topology_warnings'])

        defined = timed('define_regions', lambda: service.define_regions(bridge, storage, 'core-define', body))
        user = service.define_regions(bridge, storage, 'core-user', body, [
            {'region_id': 'LEFT_DELTOID', 'definition': {'method': 'bounds', 'min': [0.12, -0.2, 1.36], 'max': [0.34, 0.2, 1.56]},
             'role': 'editable', 'note': 'user box'},
            {'region_id': 'FACE', 'definition': {'method': 'vertex_group', 'name': 'FACE', 'min_weight': 0.5}, 'role': 'locked'}])
        rows = {r['region_id']: r for r in user['regions']}
        check('regions_user_overrides_automatic', rows['FACE']['source'] == 'user' and rows['FACE']['role'] == 'locked'
              and rows['LEFT_DELTOID']['source'] == 'user' and rows['HEAD']['source'] == 'automatic',
              face=rows['FACE'], deltoid=rows['LEFT_DELTOID'])
        locked = timed('lock_regions', lambda: service.lock_regions(bridge, storage, 'core-lock', defined['manifest'],
                                                                     lock=['FACE', 'HEAD', 'HANDS'], editable=['SHOULDERS', 'UPPER_BACK', 'TORSO']))
        manifest = locked['manifest']
        roles = {r['region_id']: r['role'] for r in locked['regions']}
        face_count = len(group_members(body, 'FACE'))
        check('regions_locked_and_captured', roles['FACE'] == roles['HEAD'] == 'locked' and roles['SHOULDERS'] == 'editable'
              and roles['NECK'] == 'unassigned' and next(r for r in locked['regions'] if r['region_id'] == 'FACE')['vertices'] == face_count
              and all(r['faces'] is not None for r in locked['regions']), roles=roles, face_vertices=face_count)
        validation = service.validate_locked_regions(bridge, storage, manifest)
        check('regions_validate_clean', validation['passed'], errors=validation['errors'])
        report['core_manifest'] = manifest

        silhouette = timed('silhouette_4_views', lambda: service.analyze_silhouette(bridge, storage, body, ['FRONT', 'SIDE', 'THREE_QUARTER', 'BACK'], 200, manifest))
        views = {v['view']: v for v in silhouette['views']}
        cell = views['FRONT']['cell_m']
        check('silhouette_front_matches_bounds', abs(views['FRONT']['height_m'] - (high[2] - low[2])) <= 2 * cell
              and abs(views['FRONT']['width_m'] - (high[0] - low[0])) <= 2 * cell,
              front={k: views['FRONT'][k] for k in ('height_m', 'width_m', 'area_m2', 'cell_m')}, bounds=[low, high])
        check('silhouette_side_depth_matches_bounds', abs(views['SIDE']['width_m'] - (high[1] - low[1])) <= 2 * cell,
              side_width=views['SIDE']['width_m'], depth=high[1] - low[1])
        check('silhouette_symmetric_balance', abs(views['FRONT']['left_area_fraction'] - 0.5) <= 0.02
              and abs(views['FRONT']['area_m2'] - views['BACK']['area_m2']) <= 0.01 * views['FRONT']['area_m2'],
              front_left=views['FRONT']['left_area_fraction'], back_area=views['BACK']['area_m2'], front_area=views['FRONT']['area_m2'])
        report['core_named_measurements'] = silhouette['named_measurements']

        proportions = timed('proportions', lambda: service.analyze_proportions(bridge, storage, body, 'HUMANOID', manifest))
        rig = bpy.data.objects[info['rig']]
        arm = sum(rig.data.bones[n].length for n in ('upper_arm.L', 'forearm.L', 'hand.L'))
        leg = sum(rig.data.bones[n].length for n in ('thigh.L', 'shin.L'))
        height = silhouette['named_measurements']['overall_height_m']
        values = {r['measurement']: r for r in proportions['measurements']}
        # Bone lengths are float32 in Blender: agreement is judged at 1e-6 relative, not bit-exact.
        check('proportions_bone_lengths_independent', abs(values['arm_length_to_height_LEFT']['value'] / (arm / height) - 1) < 1e-6
              and abs(values['leg_length_to_height_LEFT']['value'] / (leg / height) - 1) < 1e-6,
              measured={k: values[k]['value'] for k in values}, independent={'arm': arm / height, 'leg': leg / height})
        check('proportions_measurement_separate_from_interpretation', proportions['design_recommendations'] == []
              and all({'value', 'reference', 'deviation', 'confidence', 'interpretation'} <= set(r) for r in proportions['measurements']),
              rows=proportions['measurements'])
        creature_like = service.analyze_proportions(bridge, storage, body, 'CREATURE', manifest)
        check('proportions_creature_profile_no_reference', all(r['reference'] is None for r in creature_like['measurements']))

        asymmetry = timed('asymmetry', lambda: service.analyze_asymmetry(bridge, storage, body, manifest))
        check('asymmetry_humanoid_pairs_symmetric', asymmetry['pairs'] and all(p['classification'] == 'symmetric' for p in asymmetry['pairs']),
              pairs=[{k: p[k] for k in ('left', 'right', 'log_deviation', 'classification')} for p in asymmetry['pairs']])
        surface = timed('surface_clean', lambda: service.analyze_surface(bridge, storage, body, manifest))
        report['core_surface_clean'] = {'findings_total': surface['findings_total'], 'thresholds': surface['thresholds'],
                                        'types': sorted({f['type'] for f in surface['findings']})}
        intersections = timed('intersections_clean', lambda: service.analyze_intersections(bridge, storage, body, manifest))
        report['core_intersections_clean'] = {k: intersections[k] for k in ('overlap_pairs', 'by_region_pair', 'triangles')}
        audit = timed('audit', lambda: service.modeling_audit(bridge, storage, body, manifest))
        check('audit_structure_no_score', all({'type', 'region', 'severity', 'evidence', 'suggested_action'} <= set(f) for f in audit['findings'])
              and 'score' not in json.dumps(audit).lower().replace('no overall quality score', ''),
              findings=len(audit['findings']), types=sorted({f['type'] for f in audit['findings']}))
        before_plan = signature(body)
        plan = timed('redesign_plan', lambda: service.redesign_plan(bridge, storage, body, DEMO_A_INTENT, manifest))
        check('plan_never_mutates', signature(body) == before_plan and plan['apply'] is False and plan['mutated'] is False
              and all(s['apply'] is False for s in plan['steps']) and len(bpy.data.objects) == 2,
              steps=[{k: s.get(k) for k in ('goal', 'status', 'regions', 'estimated_displacement_m', 'risk', 'risk_reasons')} for s in plan['steps']],
              preserve=plan['preserve_requested'])

        # ---------------------------------------------------------- candidate + full edit cycle
        source_before = state(body)
        source_fp = inspected['mesh_signature']['fingerprint']
        candidate = timed('create_candidate', lambda: service.create_candidate(bridge, storage, 'core-cand', body, source_fp))
        name = candidate['candidate']
        check('candidate_created_preserving', candidate['status'] == 'success' and all(v['preserved'] for v in candidate['preservation'].values())
              and 'dependency_manifest' in candidate, preservation=candidate['preservation'])
        again = service.create_candidate(bridge, storage, 'core-cand-2', body, source_fp)
        check('candidate_retry_reuses', again['reused'] and again['candidate'] == name and
              sum(1 for o in bpy.data.objects if o.name.startswith(body + '__MESHC_')) == 1, second=again.get('warnings'))
        cand_before = state(name)
        operation = {'type': 'inflate', 'region': 'SHOULDERS', 'strength': 0.01, 'falloff': {'type': 'smooth', 'width_m': 0.04}}
        proposal = edit_cycle('core', name, manifest, operation)
        check('adjust_propose_supported', proposal['proposal']['supported'], reasons=proposal['proposal']['reasons'],
              stats=proposal['proposal']['stats'], capacity=proposal['proposal']['capacity'])
        preview = timed('adjust_preview', lambda: adjust_shape(bridge, storage, 'core-preview', mode='preview', operation=proposal['operation']))
        preview_name = preview.get('preview_object')
        check('adjust_preview_separate_object', preview.get('status') != 'error' and preview_name in bpy.data.objects
              and state(name)['co'] == cand_before['co'], status=preview.get('status'), errors=preview.get('errors'),
              metrics=preview.get('metrics'))
        applied = timed('adjust_apply', lambda: adjust_shape(bridge, storage, 'core-apply', mode='apply', operation=proposal['operation']))
        after = state(name)
        face = group_members(name, 'FACE')
        verdict = preserved(cand_before, after)
        check('adjust_apply_numeric_pass', applied.get('status') == 'warning' and applied.get('numeric_verification') == 'pass'
              and applied['regions_verification']['passed'], status=applied.get('status'), errors=applied.get('errors'),
              metrics=applied.get('metrics'))
        check('adjust_apply_independent_preservation', all(v for k, v in verdict.items() if k != 'shape_key_offset_max_error')
              and displacement(cand_before, after, face) == 0.0 and displacement(cand_before, after, range(len(after['co']) // 3)) > 0.005,
              verdict=verdict, face_max=displacement(cand_before, after, face),
              moved_max=displacement(cand_before, after, range(len(after['co']) // 3)))
        check('adjust_source_untouched', state(body) == source_before and signature(body) == source_fp)
        verified = adjust_shape(bridge, storage, 'core-verify', mode='verify', operation=proposal['operation'])
        check('adjust_verify_pass', verified.get('numeric_verification') == 'pass' and verified['regions_verification']['passed'],
              errors=verified.get('errors'))
        retry = adjust_shape(bridge, storage, 'core-apply-again', mode='apply', operation=proposal['operation'])
        meshes = sorted(m.name for m in bpy.data.meshes if '__M4E_' in m.name)
        check('adjust_apply_retry_no_duplicate', retry.get('idempotency', {}).get('replayed') is True or 'replay' in json.dumps(retry.get('idempotency')),
              meshes=meshes, idempotency=retry.get('idempotency'))
        reverted = timed('adjust_revert', lambda: adjust_shape(bridge, storage, 'core-revert', mode='revert', operation=proposal['operation']))
        check('adjust_revert_restores_positions', reverted.get('status') != 'error' and state(name)['co'] == cand_before['co']
              and bpy.data.meshes.get(applied['working_action']) is not None,
              status=reverted.get('status'), errors=reverted.get('errors'))
        # Reapply through a new proposal for the visual pass.
        proposal2 = edit_cycle('core2', name, manifest, operation)
        adjust_shape(bridge, storage, 'core2-apply', mode='apply', operation=proposal2['operation'])
        compare = timed('compare_before_after', lambda: service.compare_before_after(bridge, storage, 'core-qa', body, name, None, 384, 384, manifest))
        check('visual_equivalent_views', compare.get('status') == 'ok' and compare['state_restored'] and compare['montage']
              and len({json.dumps(f['path']) for f in compare['files']}) == 6 and all(f['exists'] and f['bytes'] > 0 for f in compare['files']),
              backend=compare.get('render_backend'), montage=compare.get('montage'), errors=compare.get('errors'),
              silhouette_delta=[{k: v['delta'] for k, v in row.items() if isinstance(v, dict) and 'delta' in v} for row in compare.get('silhouette_delta', [])])
        report['core_visual'] = {'montage': compare.get('montage'), 'output_dir': compare.get('output_dir'), 'cameras': compare.get('cameras')}
        reference = service.capture_reference_views(bridge, storage, 'core-ref', body, None, 256, 256)
        candidate_views = service.capture_candidate_views(bridge, storage, 'core-candviews', reference['reference_set'], name)
        compared = service.compare_reference(bridge, storage, 'core-compare', reference['reference_set'], name)
        check('reference_views_same_cameras', candidate_views.get('status') == 'ok' and candidate_views['cameras'] == reference['cameras']
              and compared['measurable_differences'] and compared['visual_evidence'],
              reference=reference.get('reference_set'), differences=[{'view': d['view'], 'area_delta': d['area_m2']['delta']} for d in compared['measurable_differences']])

        # ---------------------------------------------------------- fixture defects are detected where they are
        for variant, kind, region in (('shoulder_defect', {'abrupt_curvature_change', 'unexpected_bump', 'local_pinching'}, None),
                                      ('pinched_torso', {'local_pinching', 'abrupt_curvature_change', 'unexpected_bump'}, 'TORSO'),
                                      ('noisy_patch', {'noisy_surface', 'unexpected_bump', 'abrupt_curvature_change'}, None)):
            data = fixture(variant)
            found = service.analyze_surface(bridge, storage, data['body'])
            types = sorted({f['type'] for f in found['findings']})
            located = [f for f in found['findings'] if f['type'] in kind]
            report['fixtures'][variant]['surface'] = {'findings_total': found['findings_total'], 'types': types,
                                                      'located': located[:5], 'clean_total': surface['findings_total']}
            check('surface_detects_' + variant, located and found['findings_total'] > surface['findings_total']
                  and (region is None or any(f['region'] == region for f in located)),
                  types=types, first=located[:3], clean_total=surface['findings_total'])
        data = fixture('self_intersection')
        found = service.analyze_intersections(bridge, storage, data['body'])
        check('intersections_detect_folded_arm', found['overlap_pairs'] > intersections['overlap_pairs'],
              pairs=found['overlap_pairs'], clean=intersections['overlap_pairs'], by_region=found['by_region_pair'])

    # ================================================================== negatives
    def stage_negatives():
        info = fixture('humanoid', 'neg')
        body = info['body']
        defined = service.define_regions(bridge, storage, 'neg-define', body)
        locked = service.lock_regions(bridge, storage, 'neg-lock', defined['manifest'], lock=['FACE', 'HEAD'], editable=['SHOULDERS', 'UPPER_BACK'])
        manifest = locked['manifest']
        source_fp = signature(body)
        candidate = service.create_candidate(bridge, storage, 'neg-cand', body, source_fp)['candidate']
        base = state(candidate)

        ok, message = refused('locked_region_edit', lambda: adjust_shape(bridge, storage, 'neg-locked', candidate, 'propose',
                              {'regions_manifest': manifest, 'operation': {'type': 'inflate', 'region': 'FACE', 'strength': 0.01}}), 'LOCKED_REGION_EDIT')
        check('negative_locked_region_edit_refused', ok and state(candidate) == base, error=message)
        ok, message = refused('unassigned_region_edit', lambda: adjust_shape(bridge, storage, 'neg-unassigned', candidate, 'propose',
                              {'regions_manifest': manifest, 'operation': {'type': 'inflate', 'region': 'NECK', 'strength': 0.01}}), 'REGION_NOT_EDITABLE')
        check('negative_unassigned_region_refused', ok and state(candidate) == base, error=message)
        ok, message = refused('source_is_not_candidate', lambda: adjust_shape(bridge, storage, 'neg-source', body, 'propose',
                              {'regions_manifest': manifest, 'operation': {'type': 'inflate', 'region': 'SHOULDERS', 'strength': 0.01}}), 'CANDIDATE_REQUIRED')
        check('negative_direct_source_edit_refused', ok, error=message)
        ok, message = refused('wrong_source_fingerprint', lambda: service.create_candidate(bridge, storage, 'neg-fp', body, 'm521:' + '0' * 64), 'STALE_SIGNATURE')
        check('negative_wrong_source_fingerprint', ok and sum(1 for o in bpy.data.objects if '__MESHC_' in o.name) == 1, error=message)

        # A locked vertex moved outside the tools (e.g. by hand) is caught by validation.
        obj = bpy.data.objects[candidate]
        face = group_members(candidate, 'FACE')
        obj.data.vertices[face[0]].co.z += 0.0005
        result = service.validate_locked_regions(bridge, storage, manifest, candidate)
        obj.data.vertices[face[0]].co.z -= 0.0005
        check('negative_locked_move_0p5mm_detected', not result['passed'] and any('LOCKED_REGION_MODIFIED' in e for e in result['errors']),
              errors=result['errors'][:3])
        # Unexpected UV / weight / material changes on locked faces fail.
        face_poly = next(p for p in obj.data.polygons if all(v in set(face) for v in p.vertices))
        uv = obj.data.uv_layers['UVMap'].uv[face_poly.loop_start].vector.copy()
        obj.data.uv_layers['UVMap'].uv[face_poly.loop_start].vector = (uv[0] + 0.01, uv[1])
        result_uv = service.validate_locked_regions(bridge, storage, manifest, candidate)
        obj.data.uv_layers['UVMap'].uv[face_poly.loop_start].vector = uv
        old_material = face_poly.material_index
        face_poly.material_index = 2
        result_material = service.validate_locked_regions(bridge, storage, manifest, candidate)
        face_poly.material_index = old_material
        group = obj.vertex_groups['head']
        original = [e.weight for e in obj.data.vertices[face[0]].groups if e.group == group.index]
        group.add([face[0]], 0.123, 'REPLACE')
        result_weight = service.validate_locked_regions(bridge, storage, manifest, candidate)
        if original:
            group.add([face[0]], original[0], 'REPLACE')
        else:
            group.remove([face[0]])
        check('negative_locked_uv_change_detected', any('LOCKED_UV_CHANGED' in e for e in result_uv['errors']), errors=result_uv['errors'][:3])
        check('negative_locked_material_change_detected', any('LOCKED_MATERIAL_CHANGED' in e for e in result_material['errors']),
              errors=result_material['errors'][:3])
        check('negative_locked_weight_change_detected', any('LOCKED_WEIGHTS_CHANGED' in e for e in result_weight['errors']),
              errors=result_weight['errors'][:3])
        check('negative_restored_state_validates', service.validate_locked_regions(bridge, storage, manifest, candidate)['passed'])

        # An applied edit whose UVs are later changed fails numeric verification.
        operation = {'type': 'inflate', 'region': 'SHOULDERS', 'strength': 0.008}
        proposal = adjust_shape(bridge, storage, 'neg-p1', candidate, 'propose', {'regions_manifest': manifest, 'operation': operation})
        applied = adjust_shape(bridge, storage, 'neg-a1', mode='apply', operation=proposal['operation'])
        edited = bpy.data.objects[candidate].data
        loop = edited.polygons[0].loop_start
        uv = edited.uv_layers['UVMap'].uv[loop].vector.copy()
        edited.uv_layers['UVMap'].uv[loop].vector = (uv[0] + 0.05, uv[1])
        verified = adjust_shape(bridge, storage, 'neg-v1', mode='verify', operation=proposal['operation'])
        edited.uv_layers['UVMap'].uv[loop].vector = uv
        check('negative_unexpected_uv_change_fails_verify', applied.get('numeric_verification') == 'pass'
              and verified.get('numeric_verification') == 'fail' and 'UV_CHANGED' in verified.get('errors', []), errors=verified.get('errors'))
        weight_group = bpy.data.objects[candidate].vertex_groups['chest']
        previous = [e.weight for e in bpy.data.objects[candidate].data.vertices[0].groups if e.group == weight_group.index]
        weight_group.add([0], 0.5 if not previous else min(1.0, previous[0] + 0.25), 'REPLACE')
        verified_w = adjust_shape(bridge, storage, 'neg-v2', mode='verify', operation=proposal['operation'])
        if previous:
            weight_group.add([0], previous[0], 'REPLACE')
        else:
            weight_group.remove([0])
        check('negative_unexpected_weight_change_fails_verify', 'WEIGHTS_CHANGED' in verified_w.get('errors', []), errors=verified_w.get('errors'))

        # Stale proposal: the candidate changes between propose and apply.
        proposal = adjust_shape(bridge, storage, 'neg-p2', candidate, 'propose', {'regions_manifest': manifest, 'operation': operation})
        moved = bpy.data.objects[candidate].data
        index = group_members(candidate, 'SHOULDERS')[0]
        moved.vertices[index].co.x += 0.001
        ok, message = refused('stale_proposal', lambda: adjust_shape(bridge, storage, 'neg-a2', mode='apply', operation=proposal['operation']), 'STALE_PROPOSAL')
        moved.vertices[index].co.x -= 0.001
        check('negative_stale_proposal_refused', ok, error=message)

        # Stale region indices after a topology change.
        indices = service.define_regions(bridge, storage, 'neg-idx', candidate, [
            {'region_id': 'FACE_IDX', 'definition': {'method': 'indices', 'indices': face[:50],
                                                     'topology_fingerprint': bridge.run(SIGNATURE, {'object_name': candidate})['fingerprints']['topology']},
             'role': 'locked'},
            {'region_id': 'SHOULDERS', 'definition': {'method': 'vertex_group', 'name': 'SHOULDERS', 'min_weight': 0.5}, 'role': 'editable'}],
            include_automatic=False)
        import bmesh
        topo = bpy.data.objects[candidate].data
        mesh = bmesh.new()
        mesh.from_mesh(topo)
        mesh.edges.ensure_lookup_table()
        bmesh.ops.subdivide_edges(mesh, edges=[mesh.edges[0]], cuts=1)
        mesh.to_mesh(topo)
        mesh.free()
        topo.update()
        stale = service.validate_locked_regions(bridge, storage, indices['manifest'], candidate)
        check('negative_stale_indices_after_topology_change', not stale['passed'] and any('TOPOLOGY_CHANGED' in e for e in stale['errors']),
              errors=stale['errors'][:2])
        stale_proposal = adjust_shape(bridge, storage, 'neg-topo', candidate, 'propose',
                                      {'regions_manifest': indices['manifest'], 'operation': {'type': 'inflate', 'region': 'SHOULDERS', 'strength': 0.01}})
        check('negative_topology_mismatch_proposal_unsupported', not stale_proposal['proposal']['supported']
              and any('TOPOLOGY_CHANGED' in r for r in stale_proposal['proposal']['reasons']), reasons=stale_proposal['proposal']['reasons'])
        report['rejected_by_safety'].append({'check': 'topology_mismatch_proposal', 'error': stale_proposal['proposal']['reasons'][0][:300]})
        ok, message = refused('topology_mismatch_apply', lambda: adjust_shape(bridge, storage, 'neg-topo-apply', mode='apply',
                                                                              operation=stale_proposal['operation']), 'unsupported_or_ambiguous')
        check('negative_topology_mismatch_apply_refused', ok, error=message)

        # Ambiguous automatic region: a mesh without groups falls back to low-confidence height bands.
        bare = fixture('humanoid', 'bare')
        bare_obj = bpy.data.objects[bare['body']]
        bare_obj.modifiers.clear()
        bare_obj.vertex_groups.clear()
        bpy.ops.wm.save_mainfile()
        bands = service.define_regions(bridge, storage, 'neg-bands', bare['body'])
        before_bare = signature(bare['body'])
        ok, message = refused('ambiguous_region', lambda: service.lock_regions(bridge, storage, 'neg-amb', bands['manifest'], editable=['TORSO']),
                              'REGION_AMBIGUOUS')
        check('negative_ambiguous_region_not_editable', ok and signature(bare['body']) == before_bare
              and all(r['confidence'] <= 0.3 for r in bands['regions']), error=message, regions=bands['regions'])

        # Shape keys: conflict refused, propagate_delta preserves every offset, absolute keys refused.
        keys = fixture('humanoid_keys', 'keys')
        kbody = keys['body']
        kdefined = service.define_regions(bridge, storage, 'keys-define', kbody)
        klocked = service.lock_regions(bridge, storage, 'keys-lock', kdefined['manifest'], lock=['HANDS'],
                                       editable=['FACE', 'SHOULDERS', 'TORSO', 'UPPER_BACK'])
        kcand = service.create_candidate(bridge, storage, 'keys-cand', kbody, signature(kbody))['candidate']
        kbefore = state(kcand)
        conflict = adjust_shape(bridge, storage, 'keys-p1', kcand, 'propose',
                                {'regions_manifest': klocked['manifest'], 'operation': {'type': 'inflate', 'region': 'FACE', 'strength': 0.005}})
        check('negative_shape_key_conflict_refused', not conflict['proposal']['supported']
              and any('SHAPE_KEY_CONFLICT' in r for r in conflict['proposal']['reasons']), reasons=conflict['proposal']['reasons'],
              keys=conflict['proposal']['shape_keys']['keys'])
        report['rejected_by_safety'].append({'check': 'shape_key_conflict', 'error': conflict['proposal']['reasons'][0][:300]})
        propagate = adjust_shape(bridge, storage, 'keys-p2', kcand, 'propose',
                                 {'regions_manifest': klocked['manifest'], 'operation': {'type': 'inflate', 'region': 'FACE', 'strength': 0.005},
                                  'shape_keys': 'propagate_delta'})
        applied = adjust_shape(bridge, storage, 'keys-a2', mode='apply', operation=propagate['operation'])
        kafter = state(kcand)
        verdict = preserved(kbefore, kafter)
        check('shape_keys_propagate_delta_preserves_offsets', propagate['proposal']['supported'] and applied.get('numeric_verification') == 'pass'
              and verdict['shape_key_offsets'] and kafter['keys']['Basis'] == kafter['co'] and kafter['co'] != kbefore['co'],
              verdict=verdict, metrics=applied.get('metrics'))
        # UPPER_BACK (y > 0) is not moved by Smile (face) or Breath (front of the chest).
        unaffected = adjust_shape(bridge, storage, 'keys-p3', kcand, 'propose',
                                  {'regions_manifest': klocked['manifest'], 'operation': {'type': 'inflate', 'region': 'UPPER_BACK', 'strength': 0.005}})
        check('shape_keys_unaffected_region_allowed', unaffected['proposal']['supported'], reasons=unaffected['proposal']['reasons'])
        bpy.data.objects[kcand].data.shape_keys.use_relative = False
        absolute = adjust_shape(bridge, storage, 'keys-p4', kcand, 'propose',
                                {'regions_manifest': klocked['manifest'], 'operation': {'type': 'inflate', 'region': 'SHOULDERS', 'strength': 0.005},
                                 'shape_keys': 'propagate_delta'})
        bpy.data.objects[kcand].data.shape_keys.use_relative = True
        check('negative_absolute_shape_keys_refused', not absolute['proposal']['supported']
              and any('SHAPE_KEYS_ABSOLUTE_UNSUPPORTED' in r for r in absolute['proposal']['reasons']), reasons=absolute['proposal']['reasons'])
        report['rejected_by_safety'].append({'check': 'absolute_shape_keys', 'error': absolute['proposal']['reasons'][0][:300]})
        kmesh = bpy.data.objects[kcand].data
        kmesh.vertices[0].co.x += 0.002  # base mesh no longer equal to Basis
        mismatch = adjust_shape(bridge, storage, 'keys-p5', kcand, 'propose',
                                {'regions_manifest': klocked['manifest'], 'operation': {'type': 'inflate', 'region': 'SHOULDERS', 'strength': 0.005}})
        kmesh.vertices[0].co.x -= 0.002
        check('negative_basis_mismatch_refused', any('SHAPE_KEY_BASIS_MISMATCH' in r for r in mismatch['proposal']['reasons']),
              reasons=mismatch['proposal']['reasons'])

        # Custom normals + sharp edges: stored data unchanged by an edit, no recomputation.
        normals = fixture('humanoid_normals', 'normals')
        nbody = normals['body']
        ndefined = service.define_regions(bridge, storage, 'normals-define', nbody)
        nlocked = service.lock_regions(bridge, storage, 'normals-lock', ndefined['manifest'], lock=['FACE'], editable=['SHOULDERS'])
        ncand = service.create_candidate(bridge, storage, 'normals-cand', nbody, signature(nbody))['candidate']
        nbefore = state(ncand)
        sharp_before = [e.use_edge_sharp for e in bpy.data.objects[ncand].data.edges]
        nprop = adjust_shape(bridge, storage, 'normals-p', ncand, 'propose',
                             {'regions_manifest': nlocked['manifest'], 'operation': {'type': 'inflate', 'region': 'SHOULDERS', 'strength': 0.006}})
        napplied = adjust_shape(bridge, storage, 'normals-a', mode='apply', operation=nprop['operation'])
        nafter = state(ncand)
        check('normals_custom_data_preserved', napplied.get('numeric_verification') == 'pass' and nbefore['custom_normal'] is not None
              and nafter['custom_normal'] == nbefore['custom_normal']
              and [e.use_edge_sharp for e in bpy.data.objects[ncand].data.edges] == sharp_before,
              has_custom=nbefore['custom_normal'] is not None, sharp_edges=sum(sharp_before))

        # MASTER: every mutating M4 entry point refuses before a checkpoint or change.
        master = fixture('humanoid', 'master_neg')
        mbody = master['body']
        mdefined = service.define_regions(bridge, storage, 'master-define', mbody)
        mlocked = service.lock_regions(bridge, storage, 'master-lock', mdefined['manifest'], lock=['FACE'], editable=['SHOULDERS'])
        mcand = service.create_candidate(bridge, storage, 'master-cand', mbody, signature(mbody))['candidate']
        mprop = adjust_shape(bridge, storage, 'master-p', mcand, 'propose',
                             {'regions_manifest': mlocked['manifest'], 'operation': {'type': 'inflate', 'region': 'SHOULDERS', 'strength': 0.01}})
        master_path = output / 'master_files' / 'HeroMaster.blend'
        master_path.parent.mkdir(parents=True, exist_ok=True)
        bpy.ops.wm.save_as_mainfile(filepath=str(master_path))
        register_master(bridge, storage)
        digest = sha256(master_path)
        points_before = len(checkpoints())
        mstate = state(mbody)
        cstate = state(mcand)
        # Even the reuse of an existing candidate is refused on a MASTER.
        ok1, m1 = refused('master_create_candidate', lambda: service.create_candidate(bridge, storage, 'master-c2', mbody, signature(mbody)),
                          'MASTER_ASSET_READ_ONLY')
        ok2, m2 = refused('master_adjust_preview', lambda: adjust_shape(bridge, storage, 'master-prev', mode='preview', operation=mprop['operation']),
                          'MASTER_ASSET_READ_ONLY')
        ok3, m3 = refused('master_adjust_apply', lambda: adjust_shape(bridge, storage, 'master-apply', mode='apply', operation=mprop['operation']),
                          'MASTER_ASSET_READ_ONLY')
        fresh = bpy.data.objects[mbody].data
        fresh.vertices[0].co.x += 0.001
        ok4, m4 = refused('master_create_candidate_new_state', lambda: service.create_candidate(bridge, storage, 'master-c3', mbody, signature(mbody)),
                          'MASTER_ASSET_READ_ONLY')
        fresh.vertices[0].co.x -= 0.001
        ok5, m5 = refused('master_session_apply', lambda: service.redesign_session(bridge, storage, 'master-session', mbody, DEMO_A_INTENT,
                                                                                    mlocked['manifest'], True, 1), 'MASTER_ASSET_READ_ONLY')
        ok6, m6 = refused('master_deformation_qa', lambda: service.skinning_qa(bridge, storage, 'master-skin', master['rig'], mbody, mcand, STRESS),
                          'MASTER_ASSET_READ_ONLY')
        check('negative_master_mutation_blocked', ok1 and ok2 and ok3 and ok4 and ok5 and ok6 and len(checkpoints()) == points_before
              and state(mbody) == mstate and state(mcand) == cstate and sha256(master_path) == digest
              and current_asset(bridge, storage)['role'] == 'MASTER',
              errors=[m1, m2, m3, m4, m5, m6], checkpoints=len(checkpoints()) - points_before)

        # A folded face fails numeric verification (independent of the flip guard that prevents it).
        from blender_mcp.production.character.runtime import CHAR_RUNTIME
        plane = bpy.data.meshes.new('FlipBefore')
        plane.from_pydata([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)], [], [(0, 1, 2, 3)])
        folded = plane.copy()
        folded.name = 'FlipAfter'
        folded.vertices[0].co = (2.0, 2.0, 0.0)  # past the opposite corner: the quad's winding reverses
        holder = bpy.data.objects.new('FlipHolder', plane)
        bpy.context.scene.collection.objects.link(holder)
        flip = bridge.run(CHAR_RUNTIME + "\nresult = cr_verify(bpy.data.objects['FlipHolder'], bpy.data.meshes['FlipBefore'], "
                          "bpy.data.meshes['FlipAfter'], [], None, 1e-5)\n", {})
        check('negative_folded_face_fails_verification', not flip['passed'] and any('FACE_FLIPPED' in e for e in flip['errors']),
              errors=flip['errors'], flipped=flip['metrics']['flipped_faces'])
        report['rejected_by_safety'].append({'check': 'folded_face', 'error': next((e for e in flip['errors'] if 'FACE_FLIPPED' in e), '')})
        bpy.data.objects.remove(holder)

        # A bad deformation must not pass: candidate weights broken by hand (outside M4 tools).
        bad = fixture('humanoid', 'bad_deform')
        bbody = bad['body']
        bcand = service.create_candidate(bridge, storage, 'bad-cand', bbody, signature(bbody))['candidate']
        good = service.skinning_qa(bridge, storage, 'good-skin', bad['rig'], bbody, bcand, STRESS)
        cobj = bpy.data.objects[bcand]
        forearm = group_members(bcand, 'forearm.L', 0.3)
        for name in ('forearm.L', 'hand.L', 'upper_arm.L'):
            cobj.vertex_groups[name].remove(forearm)
        cobj.vertex_groups['chest'].add(forearm, 1.0, 'REPLACE')
        broken = service.skinning_qa(bridge, storage, 'bad-skin', bad['rig'], bbody, bcand, STRESS)
        check('deformation_qa_identical_candidate_passes', good['passed'], poses=good['poses'], errors=good['errors'])
        check('negative_bad_deformation_fails', not broken['passed'] and broken['regressions'], regressions=broken['regressions'][:4],
              state_restored=broken['state_restored'])
        report['rejected_by_safety'].append({'check': 'bad_deformation', 'error': (broken['errors'] or [''])[0][:300]})

    # ================================================================== demos
    def stage_demos():
        # ---------------- Demo A: MASTER -> WORKING COPY -> CANDIDATE -> edit -> verify -> accept
        demo = {}
        info = fixture('humanoid', 'demo_a')
        master_path = output / 'demo_a_master' / 'HeroMaster.blend'
        master_path.parent.mkdir(parents=True, exist_ok=True)
        bpy.ops.wm.save_as_mainfile(filepath=str(master_path))
        register_master(bridge, storage)
        master_digest = sha256(master_path)
        working = create_working_copy(bridge, storage, 'demo-a-wc')
        body, rig = info['body'], info['rig']
        demo['asset'] = {'master': str(master_path), 'working_copy': working.get('working_path') or working.get('path'),
                         'role_now': current_asset(bridge, storage)['role']}
        source_before = state(body)
        source_fp = signature(body)
        demo['inspect'] = {k: v for k, v in service.inspect(bridge, storage, body).items() if k in ('counts', 'dimensions_m', 'symmetry', 'deformation')}
        defined = service.define_regions(bridge, storage, 'demo-a-define', body)
        locked = service.lock_regions(bridge, storage, 'demo-a-lock', defined['manifest'], lock=['FACE', 'HEAD', 'HANDS'],
                                      editable=['SHOULDERS', 'UPPER_BACK', 'TORSO'])
        manifest = locked['manifest']
        audit = service.modeling_audit(bridge, storage, body, manifest)
        demo['audit'] = {'findings': len(audit['findings']), 'types': sorted({f['type'] for f in audit['findings']})}
        session = timed('demo_a_session_plan', lambda: service.redesign_session(bridge, storage, 'demo-a-s0', body, DEMO_A_INTENT, manifest))
        demo['plan'] = [{k: s.get(k) for k in ('step', 'goal', 'status', 'operation', 'estimated_displacement_m', 'affected_vertices',
                                               'locked_region_proximity_m', 'risk', 'risk_reasons')} for s in session['plan']['steps']]
        check('demo_a_session_plan_no_mutation', session['mutated'] is False and signature(body) == source_fp
              and not any('__MESHC_' in o.name for o in bpy.data.objects), steps=len(demo['plan']))
        applied_steps = []
        candidate = None
        silhouette_before = service.analyze_silhouette(bridge, storage, body, ['FRONT', 'SIDE', 'THREE_QUARTER'], 200, manifest)
        for step in [s['step'] for s in session['plan']['steps'] if s['status'] == 'proposed']:
            cycle = timed('demo_a_session_step_' + str(step), lambda: service.redesign_session(
                bridge, storage, 'demo-a-s' + str(step), body, DEMO_A_INTENT, manifest, True, step, STRESS, rig))
            candidate = cycle['candidate']['candidate']
            before_apply = state(candidate)
            operation = cycle['proposal']['operation']
            applied = adjust_shape(bridge, storage, 'demo-a-apply-' + str(step), mode='apply', operation=operation)
            after_apply = state(candidate)
            verdict = preserved(before_apply, after_apply)
            face = group_members(candidate, 'FACE') + group_members(candidate, 'HEAD') + group_members(candidate, 'HANDS')
            row = {'step': step, 'goal': next(s['goal'] for s in session['plan']['steps'] if s['step'] == step),
                   'operation': cycle['proposal'].get('operation'), 'preview_numeric': cycle['preview'].get('numeric_verification'),
                   'deformation_qa': {k: cycle['deformation_qa'].get(k) for k in ('passed', 'regressions', 'state_restored')},
                   'apply_numeric': applied.get('numeric_verification'), 'metrics': applied.get('metrics'),
                   'independent': dict(verdict, locked_max_m=displacement(before_apply, after_apply, face),
                                       moved_max_m=displacement(before_apply, after_apply, range(len(after_apply['co']) // 3))),
                   'multiview_montage': (cycle.get('multiview') or {}).get('montage')}
            applied_steps.append(row)
            check('demo_a_step_' + str(step), cycle['status'] == 'warning' and cycle['deformation_qa'].get('passed')
                  and applied.get('numeric_verification') == 'pass' and all(v for k, v in verdict.items() if k != 'shape_key_offset_max_error')
                  and applied['metrics']['flipped_faces'] == 0
                  and row['independent']['locked_max_m'] == 0.0, row=row, errors=cycle.get('errors') or applied.get('errors'))
        demo['steps'] = applied_steps
        final_qa = service.skinning_qa(bridge, storage, 'demo-a-final-skin', rig, body, candidate, STRESS)
        final_regions = service.validate_locked_regions(bridge, storage, manifest, candidate)
        compare = service.compare_before_after(bridge, storage, 'demo-a-final', body, candidate, ['FRONT', 'SIDE', 'THREE_QUARTER'], 512, 512, manifest)
        silhouette_after = service.analyze_silhouette(bridge, storage, candidate, ['FRONT', 'SIDE', 'THREE_QUARTER'], 200, manifest)
        demo['final'] = {'deformation_qa': {k: final_qa[k] for k in ('passed', 'regressions', 'poses')},
                         'locked_regions': {'passed': final_regions['passed'], 'errors': final_regions['errors']},
                         'montage': compare.get('montage'), 'silhouette_delta': compare.get('silhouette_delta'),
                         'named_before': silhouette_before['named_measurements'], 'named_after': silhouette_after['named_measurements'],
                         'render_backend': compare.get('render_backend')}
        source_after = state(body)
        check('demo_a_final', final_qa['passed'] and final_regions['passed'] and compare.get('status') == 'ok'
              and source_after == source_before and signature(body) == source_fp and sha256(master_path) == master_digest
              and len(applied_steps) >= 2, master_unchanged=sha256(master_path) == master_digest,
              regions=final_regions['errors'], qa=final_qa['errors'])
        demo['decision'] = {'accepted': bool(report['checks']['demo_a_final']['passed']), 'kept_object': candidate,
                            'source_replaced': False, 'reason': 'all numeric checks and deformation QA passed; visual review of the montage'}
        demo['originals'] = {'master_sha256_unchanged': sha256(master_path) == master_digest, 'working_copy_source_unchanged': source_after == source_before}
        report['demos']['humanoid'] = demo
        save()

        # ---------------- Demo B: asymmetric creature, oversized limb locked, symmetry never enforced
        demo = {}
        info = fixture('creature', 'demo_b')
        body, rig = info['body'], info['rig']
        source_before = state(body)
        source_fp = signature(body)
        inspected = service.inspect(bridge, storage, body)
        defined = service.define_regions(bridge, storage, 'demo-b-define', body)
        locked = service.lock_regions(bridge, storage, 'demo-b-lock', defined['manifest'], lock=['MUTATED_ARM', 'HUMP', 'FACE', 'HEAD', 'HANDS'],
                                      editable=['LEFT_SHOULDER_TORSO', 'TORSO'], intentional=['MUTATED_ARM', 'HUMP'])
        manifest = locked['manifest']
        asymmetry = service.analyze_asymmetry(bridge, storage, body, manifest)
        proportions = service.analyze_proportions(bridge, storage, body, 'CREATURE', manifest)
        demo['symmetry'] = inspected['symmetry']
        demo['asymmetry'] = {'pairs': [{k: p[k] for k in ('left', 'right', 'log_deviation', 'classification')} for p in asymmetry['pairs']],
                             'features': asymmetry['asymmetric_features']}
        demo['proportions_left_right'] = proportions['left_right']
        arm_pair = next((p for p in asymmetry['pairs'] if p['left'] == 'LEFT_ARM'), None)
        check('demo_b_asymmetry_intentional_not_corrected', arm_pair is not None and arm_pair['classification'] in ('likely_intentional', 'intentional')
              and any(f['region'] in ('MUTATED_ARM', 'HUMP') and f['classification'] == 'intentional' for f in asymmetry['asymmetric_features'])
              and all(r['reference'] is None for r in proportions['measurements']), arm=arm_pair, features=asymmetry['asymmetric_features'])
        session = service.redesign_session(bridge, storage, 'demo-b-s0', body, DEMO_B_INTENT, manifest)
        demo['plan'] = [{k: s.get(k) for k in ('step', 'goal', 'status', 'operation', 'estimated_displacement_m', 'risk')} for s in session['plan']['steps']]
        proposed = [s for s in session['plan']['steps'] if s['status'] == 'proposed']
        check('demo_b_plan_targets_torso_only', proposed and all(s['operation']['region'] in ('LEFT_SHOULDER_TORSO', 'TORSO') for s in proposed)
              and not any(s['operation']['type'] == 'scale' for s in proposed), plan=demo['plan'])
        step = proposed[0]['step']
        cycle = service.redesign_session(bridge, storage, 'demo-b-s1', body, DEMO_B_INTENT, manifest, True, step, STRESS, rig)
        candidate = cycle['candidate']['candidate']
        before_apply = state(candidate)
        applied = adjust_shape(bridge, storage, 'demo-b-apply', mode='apply', operation=cycle['proposal']['operation'])
        after_apply = state(candidate)
        arm = group_members(candidate, 'MUTATED_ARM') + group_members(candidate, 'HUMP')
        after_asym = service.analyze_asymmetry(bridge, storage, candidate, manifest)
        after_props = service.analyze_proportions(bridge, storage, candidate, 'CREATURE', manifest)
        verdict = preserved(before_apply, after_apply)
        demo['step'] = {'operation': cycle['proposal'].get('operation'), 'metrics': applied.get('metrics'),
                        'proposal_warnings': cycle['proposal'].get('warnings'),
                        'deformation_qa': {k: cycle['deformation_qa'].get(k) for k in ('passed', 'regressions')},
                        'independent': dict(verdict, locked_max_m=displacement(before_apply, after_apply, arm),
                                            moved_max_m=displacement(before_apply, after_apply, range(len(after_apply['co']) // 3))),
                        'arm_ratio_before': proportions['left_right'], 'arm_ratio_after': after_props['left_right'],
                        'mutated_arm_mismatch_before': asymmetry['regions'].get('MUTATED_ARM', {}).get('mirror_mismatch_fraction'),
                        'mutated_arm_mismatch_after': after_asym['regions'].get('MUTATED_ARM', {}).get('mirror_mismatch_fraction'),
                        'montage': (cycle.get('multiview') or {}).get('montage')}
        final_regions = service.validate_locked_regions(bridge, storage, manifest, candidate)
        check('demo_b_final', cycle['status'] == 'warning' and cycle['deformation_qa'].get('passed') and applied.get('numeric_verification') == 'pass'
              and applied['metrics']['flipped_faces'] == 0
              and final_regions['passed'] and demo['step']['independent']['locked_max_m'] == 0.0
              and demo['step']['mutated_arm_mismatch_after'] == demo['step']['mutated_arm_mismatch_before']
              and proportions['left_right'] == after_props['left_right'] and state(body) == source_before and signature(body) == source_fp,
              step=demo['step'], regions=final_regions['errors'])
        demo['decision'] = {'accepted': bool(report['checks']['demo_b_final']['passed']), 'kept_object': candidate, 'source_replaced': False}
        report['demos']['creature'] = demo
        save()

    # ================================================================== performance
    def stage_perf():
        info = timed('perf_build_dense', lambda: fixture('dense_humanoid', 'perf'))
        body = info['body']
        perf = {'vertices': info['vertices'], 'triangles': info['triangles']}
        bridge.sizes.clear()
        inspected = timed('perf_inspect', lambda: service.inspect(bridge, storage, body))
        defined = timed('perf_define_regions', lambda: service.define_regions(bridge, storage, 'perf-define', body))
        locked = timed('perf_lock_regions', lambda: service.lock_regions(bridge, storage, 'perf-lock', defined['manifest'],
                                                                         lock=['FACE', 'HEAD'], editable=['SHOULDERS', 'UPPER_BACK']))
        timed('perf_validate_locked', lambda: service.validate_locked_regions(bridge, storage, locked['manifest']))
        timed('perf_silhouette_3_views', lambda: service.analyze_silhouette(bridge, storage, body, None, 200, locked['manifest']))
        timed('perf_surface', lambda: service.analyze_surface(bridge, storage, body, locked['manifest']))
        timed('perf_asymmetry', lambda: service.analyze_asymmetry(bridge, storage, body, locked['manifest']))
        timed('perf_intersections', lambda: service.analyze_intersections(bridge, storage, body, locked['manifest']))
        candidate = timed('perf_create_candidate', lambda: service.create_candidate(bridge, storage, 'perf-cand', body,
                                                                                    inspected['mesh_signature']['fingerprint']))['candidate']
        options = {'regions_manifest': locked['manifest'], 'operation': {'type': 'inflate', 'region': 'SHOULDERS', 'strength': 0.01}}
        proposal = timed('perf_propose', lambda: adjust_shape(bridge, storage, 'perf-p', candidate, 'propose', options))
        timed('perf_preview', lambda: adjust_shape(bridge, storage, 'perf-prev', mode='preview', operation=proposal['operation']))
        applied = timed('perf_apply', lambda: adjust_shape(bridge, storage, 'perf-apply', mode='apply', operation=proposal['operation']))
        timed('perf_verify', lambda: adjust_shape(bridge, storage, 'perf-verify', mode='verify', operation=proposal['operation']))
        timed('perf_compare_before_after_512', lambda: service.compare_before_after(bridge, storage, 'perf-qa', body, candidate, None, 512, 512))
        perf['max_payload_bytes'] = max(bridge.sizes)
        perf['apply_payload_bytes'] = proposal['proposal']['capacity']['payload_bytes']
        perf['apply_numeric'] = applied.get('numeric_verification')
        perf['timings_s'] = {k: v for k, v in report['timings_s'].items() if k.startswith('perf_')}
        report['performance'] = perf
        check('perf_dense_range', 100000 <= info['triangles'] <= 150000, triangles=info['triangles'])
        check('perf_dense_apply_pass', applied.get('numeric_verification') == 'pass', errors=applied.get('errors'))
        check('perf_payloads_within_limit', perf['max_payload_bytes'] <= 200000, max_payload=perf['max_payload_bytes'])

    for name, function in (('core', stage_core), ('negatives', stage_negatives), ('demos', stage_demos), ('perf', stage_perf)):
        if name not in stages:
            continue
        try:
            function()
        except Exception as exc:
            report.setdefault('stage_exceptions', {})[name] = {'error': repr(exc)[:1000], 'traceback': traceback.format_exc()[-5000:]}
            print('STAGE_EXCEPTION', name, repr(exc)[:300], flush=True)
            save()
    report['passed'] = bool(report['checks']) and not report.get('stage_exceptions') and all(c['passed'] for c in report['checks'].values())
    save()
    print('M4_REPORT=' + str(output / 'report.json'))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--blender', required=True)
    parser.add_argument('--label', default='m4')
    parser.add_argument('--stages', default=','.join(STAGES))
    args = parser.parse_args()
    stages = [s for s in args.stages.split(',') if s]
    if any(s not in STAGES for s in stages):
        raise SystemExit('unknown stage; use ' + ','.join(STAGES))
    run_id = uuid4().hex[:8]
    output = Path(tempfile.gettempdir()) / 'bpm4' / (args.label + '-' + run_id)
    output.mkdir(parents=True)
    process = subprocess.run([args.blender, '--background', '--factory-startup', '--disable-autoexec', '--python',
                              str(Path(__file__).resolve()), '--', '--inside', str(output), args.label, ','.join(stages)],
                             capture_output=True, text=True, encoding='utf-8', errors='replace')
    (output / 'stdout.txt').write_text(process.stdout, encoding='utf-8')
    (output / 'stderr.txt').write_text(process.stderr, encoding='utf-8')
    print('\n'.join(line for line in process.stdout.splitlines() if line.startswith(('CHECK', 'M4_', 'STAGE_'))))
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
        inside(Path(sys.argv[position + 1]), sys.argv[position + 2], sys.argv[position + 3].split(','))
    else:
        main()
