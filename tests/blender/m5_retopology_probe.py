"""M5 retopology real-Blender qualification. Own headless process, generated fixtures only."""
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


def inside(output):
    import bpy
    from mathutils import Vector
    sys.path.insert(0, str(ROOT / 'src'))
    sys.path.insert(0, str(ROOT / 'tests/blender'))
    package = types.ModuleType('blender_mcp'); package.__path__ = [str(ROOT / 'src/blender_mcp')]
    sys.modules['blender_mcp'] = package
    from blender_mcp.production.common.bridge import build_code
    from blender_mcp.production.common.storage import Storage
    from blender_mcp.production.common.asset_safety import register_master
    from blender_mcp.production.common import m5
    from blender_mcp.production.retopology import service
    from blender_mcp.production.retopology.runtime import RETOPOLOGY_RUNTIME
    from m31_fixture_support import register_open_fixture
    import m4_character_fixtures as fixtures
    import m5_fixtures

    class LocalBridge:
        sizes = []
        def run(self, body, params=None):
            code = build_code(body, params or {})
            self.sizes.append(len(code.encode()))
            namespace = {}
            exec(code.rsplit('print(', 1)[0], namespace)
            return namespace['result']

    bridge = LocalBridge(); storage = Storage(output / 's')
    report = {'blender': bpy.app.version_string, 'checks': {}, 'demos': {}, 'timings_s': {}, 'fixtures': {}}
    def save():
        (output / 'report.json').write_text(json.dumps(report, indent=2, default=str), encoding='utf-8')
    def check(name, passed, **details):
        report['checks'][name] = {'passed': bool(passed), **details}; save()
        print('CHECK', name, 'PASS' if passed else 'FAIL', flush=True)
    def timed(name, fn):
        start = time.perf_counter(); result = fn(); report['timings_s'][name] = time.perf_counter() - start; return result
    def rejected(name, fn, expected):
        try:
            data = fn(); text = json.dumps(data)
            passed = expected in text and (data.get('status') == 'error' or not data.get('proposal', data).get('supported', True))
        except Exception as exc:
            text = str(exc); passed = expected in text
        check(name, passed, expected=expected, result=text[:600])
    def region(name, group, **extra):
        return dict(name=name, definition={'method': 'vertex_group', 'name': group}, **extra)
    regions = [region('FACE', 'FACE', protected=True, priority=8), region('HANDS', 'HANDS', protected=True, priority=6),
               region('SHOULDERS', 'SHOULDERS', deformation=True, priority=6),
               region('BACK_FLAT', 'UPPER_BACK', priority=1)]

    try:
        for variant in ('humanoid', 'creature'):
            info = m5_fixtures.build_character(output / 'fixtures', variant, levels=3)
            register_open_fixture(bridge, storage, output)
            report['fixtures'][variant] = info
            settings = info['options']
            source = m5.signature(bridge, info['body'])
            audit = timed(variant + '_audit', lambda: service.analyze(bridge, info['body'], settings))
            check(variant + '_closed_topology', audit['passed'], audit=audit['metrics'])
            density = timed(variant + '_density', lambda: service.analyze(bridge, info['body'], settings, 'density'))
            check(variant + '_world_density', density['surface_area_m2'] > 0 and len(density['regions']) >= 4, density=density)
            priorities = service.analyze(bridge, info['body'], settings, 'priority')
            check(variant + '_priority_protects', priorities['priority']['protected_vertices'] > 0,
                  priority=priorities['priority'])
            budget = service.analyze(bridge, info['body'], settings, 'budget')
            check(variant + '_budget', budget['supported'], budget=budget)
            plan = service.analyze(bridge, info['body'], settings, 'plan')
            check(variant + '_explicit_strategy', plan['supported'] and plan['strategy'] == 'GUIDED_DECIMATION', plan=plan)
            candidate = service.create_candidate(bridge, storage, variant + '-candidate', info['body'], source['fingerprint'],
                                                 {'regions': settings['regions']})
            relation = candidate['relationship']; previous = candidate['candidate']
            check(variant + '_candidate_dependencies', candidate['source_intact'] and len(candidate['signature']['uv_layers']) == 2,
                  candidate=candidate)
            proposed = service.correction(bridge, storage, variant + '-proposal', relation, 'propose', settings)
            check(variant + '_proposal_supported', proposed['proposal']['supported'], proposal=proposed['proposal'])
            applied = timed(variant + '_reduction', lambda: service.correction(bridge, storage, variant + '-apply',
                                                                             mode='apply', operation=proposed['operation']))
            check(variant + '_reduction_verified', applied['numeric_verification'] == 'pass', result=applied)
            if applied['numeric_verification'] != 'pass':
                continue
            low = applied['candidate']; relationship = applied['relationship']
            counts = applied['after']['triangles']
            check(variant + '_global_target', settings['target_triangles'] * .95 <= counts <= settings['target_triangles'] * 1.05,
                  triangles=counts)
            check(variant + '_source_unchanged', m5.signature(bridge, info['body'])['fingerprint'] == source['fingerprint'])
            check(variant + '_previous_low_retained', bpy.data.objects.get(previous) is not None)
            verified = service.correction(bridge, storage, variant + '-verify', mode='verify', operation=proposed['operation'])
            check(variant + '_fresh_verify', verified['numeric_verification'] == 'pass')
            projection = service.correction(bridge, storage, variant + '-projection', relationship, 'propose', settings, kind='project')
            projected = service.correction(bridge, storage, variant + '-projectapply', mode='apply',
                                           operation=projection['operation'], kind='project')
            check(variant + '_projection_verified', projected['numeric_verification'] == 'pass', result=projected)
            selected = projected if projected['numeric_verification'] == 'pass' else applied
            compare = service.compare(bridge, storage, selected['relationship'], settings)
            silhouette = service.compare(bridge, storage, selected['relationship'], settings, 'silhouette')
            check(variant + '_surface_fidelity', compare['passed'], metrics=compare['metrics'])
            check(variant + '_silhouette_fidelity', silhouette['passed'], metrics=silhouette['metrics'])
            protected = selected['metrics']['protected_regions']
            check(variant + '_protected_exact', all(row['max_vertex_displacement_m'] <= row['tolerance_m']
                                                    and row['max_weight_error'] <= 1e-5 for row in protected), regions=protected)
            replay = service.correction(bridge, storage, variant + '-applyretry', mode='apply', operation=proposed['operation'])
            check(variant + '_idempotent_apply', replay['candidate'] == low and replay['idempotency']['replayed'])
            after_density = service.analyze(bridge, selected['candidate'], settings, 'density')
            source_density = {row['region']: row for row in density['regions']}
            target_density = {row['region']: row for row in after_density['regions']}
            priority_ratio = target_density['FACE']['triangles'] / source_density['FACE']['triangles']
            flat_ratio = target_density['BACK_FLAT']['triangles'] / source_density['BACK_FLAT']['triangles']
            check(variant + '_priority_allocation_verified', priority_ratio >= flat_ratio,
                  protected_face_retention=priority_ratio, flat_retention=flat_ratio)
            report['demos'][variant] = {'source': info['body'], 'rig': info['rig'], 'low': selected['candidate'],
                                        'relationship': selected['relationship'], 'before_triangles': info['triangles'],
                                        'after_triangles': selected['after']['triangles'], 'metrics': selected['metrics'],
                                        'reduction_percent': 100 * (1 - selected['after']['triangles'] / info['triangles']),
                                        'fixture': str(output / 'fixtures' / (variant + '_final.blend')),
                                        'regions': settings['regions'], 'options': settings,
                                        'density_before': density['regions'], 'density_after': after_density['regions']}
            bpy.ops.wm.save_as_mainfile(filepath=report['demos'][variant]['fixture'])
            register_open_fixture(bridge, storage, output)
            if variant == 'humanoid':
                baseline_protection = service.correction(bridge, storage, 'empty-region-override', relationship,
                    'analyze', dict(settings, regions=[]))
                check('empty_options_cannot_drop_protection', baseline_protection['proposal']['priority']['protected_vertices'] > 0)
                downgraded = [dict(row, protected=False, deformation=False) for row in settings['regions']]
                rejected('protected_override_refused', lambda: service.correction(bridge, storage, 'unprotect-options',
                    relationship, 'analyze', dict(settings, regions=downgraded)), 'PROTECTED_REGION_OVERRIDE_REJECTED')
                rejected('protected_region_edit_refused', lambda: service.correction(bridge, storage, 'locked-propose',
                    relationship, 'analyze', dict(settings, region='FACE')), 'PROTECTED_REGION_EDIT_REJECTED')
                rejected('impossible_protected_budget', lambda: service.correction(bridge, storage, 'floor-propose',
                    relationship, 'analyze', dict(settings, target_triangles=2, maximum_triangles=2, minimum_triangles=1)),
                    'BUDGET_IMPOSSIBLE_PROTECTED_REGIONS')
                rejected('wrong_high_fingerprint', lambda: service.create_candidate(bridge, storage, 'bad-source',
                    info['body'], 'not-the-source'), 'STALE_SIGNATURE')
                original = bpy.data.objects[info['body']].data.vertices[0].co.copy()
                bpy.data.objects[info['body']].data.vertices[0].co.x += .001
                rejected('stale_high_relationship', lambda: service.compare(bridge, storage, relationship),
                         'STALE_HIGH_LOW_RELATIONSHIP')
                bpy.data.objects[info['body']].data.vertices[0].co = original
                bpy.data.objects[low].data.vertices[0].co.x += .001
                rejected('stale_low_relationship', lambda: service.compare(bridge, storage, relationship),
                         'STALE_HIGH_LOW_RELATIONSHIP')

        # Explicit LOCAL_RETOPOLOGY on one region; all outside vertices are protected and checked.
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.mesh.primitive_uv_sphere_add(segments=64, ring_count=32, radius=.10)
        sphere = bpy.context.object; sphere.name = 'LocalSphereHigh'
        group = sphere.vertex_groups.new(name='LOW_REGION')
        group.add([v.index for v in sphere.data.vertices if v.co.z < -.005], 1, 'REPLACE')
        source = m5.signature(bridge, sphere.name)
        local_settings = {'regions': [region('LOW', 'LOW_REGION', role='editable', priority=1)],
                          'region': 'LOW', 'target_reduction': .2, 'strategy': 'LOCAL_RETOPOLOGY',
                          'max_distance_m': .002, 'silhouette_resolution': 64, 'samples': 1024,
                          'maximum_silhouette_distance_m': .01, 'minimum_silhouette_iou': .95}
        local_candidate = service.create_candidate(bridge, storage, 'local-candidate', sphere.name, source['fingerprint'],
                                                    {'regions': local_settings['regions']})
        local_proposal = service.correction(bridge, storage, 'local-proposal', local_candidate['relationship'], 'propose', local_settings)
        local_applied = service.correction(bridge, storage, 'local-apply', mode='apply', operation=local_proposal['operation'])
        check('local_optimization_or_safe_budget_rejection', local_applied['numeric_verification'] == 'pass' or
              ('LOCAL_TRIANGLE_BUDGET_NOT_MET' in str(local_applied['errors']) and
               local_applied['relationship'] == local_candidate['relationship']), result=local_applied)
        check('local_outside_region_preserved', all(row['passed'] for row in local_applied['metrics']['outside_editable_region']),
              result=local_applied['metrics']['outside_editable_region'])
        density_rules = [dict(local_settings['regions'][0], minimum_density_per_m2=1e12)]
        density_proposal = service.correction(bridge, storage, 'density-impossible-propose', local_candidate['relationship'],
                                              'propose', dict(local_settings, regions=density_rules))
        density_applied = service.correction(bridge, storage, 'density-impossible-apply', mode='apply', operation=density_proposal['operation'])
        check('minimum_density_failure_keeps_previous_low', density_applied['numeric_verification'] == 'fail' and
              'MINIMUM_REGION_DENSITY_NOT_MET' in str(density_applied['errors']) and
              density_applied['relationship'] == local_candidate['relationship'])
        bad_low = sphere.copy(); bad_low.data = sphere.data.copy(); bad_low.name = 'ExcessivelyReducedMass'
        bpy.context.scene.collection.objects.link(bad_low); bad_low.scale = (.65,.65,.65)
        bad_pair = service.create_candidate(bridge,storage,'bad-fidelity-pair',sphere.name,source['fingerprint'],
                                            {'target_object':bad_low.name})
        bad_surface = service.compare(bridge,storage,bad_pair['relationship'],local_settings)
        bad_silhouette = service.compare(bridge,storage,bad_pair['relationship'],local_settings,'silhouette')
        check('excessive_surface_deviation_rejected', not bad_surface['passed'] and
              'SURFACE_DEVIATION_EXCEEDED' in bad_surface['errors'], result=bad_surface)
        check('excessive_silhouette_deviation_rejected', not bad_silhouette['passed'] and
              any('SILHOUETTE_' in error for error in bad_silhouette['errors']), result=bad_silhouette)

        info = fixtures.build(output / 'fixtures', 'humanoid_keys', levels=1)
        register_open_fixture(bridge, storage, output)
        source = m5.signature(bridge, info['body'])
        candidate = service.create_candidate(bridge, storage, 'morph-candidate', info['body'], source['fingerprint'])
        rejected('morph_topology_change_refused', lambda: service.correction(bridge, storage, 'morph-propose',
            candidate['relationship'], 'analyze', {'target_triangles': int(info['triangles'] * .5)}),
            'MORPH_TOPOLOGY_CHANGE_REQUIRES_MANUAL_OR_LATER_PIPELINE')
        register_master(bridge, Storage(output / 'master-registry'))
        rejected('MASTER_candidate_refused', lambda: service.create_candidate(bridge, Storage(output / 'master-registry'),
            'master-candidate', info['body'], source['fingerprint']), 'MASTER_ASSET_READ_ONLY')

        # Controlled analytical defects, independently introduced through bpy outside production tools.
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.mesh.primitive_cube_add()
        obj = bpy.context.object; obj.name = 'DefectCube'
        import bmesh
        bm = bmesh.new(); bm.from_mesh(obj.data); bm.faces.ensure_lookup_table()
        bmesh.ops.delete(bm, geom=[bm.faces[0]], context='FACES'); bm.to_mesh(obj.data); bm.free()
        hole = service.analyze(bridge, obj.name)
        check('hole_detected', not hole['passed'] and hole['metrics']['boundary_edges'] == 4)
        mesh = bpy.data.meshes.new('SliverMesh')
        mesh.from_pydata([(0, 0, 0), (1, 0, 0), (.5, .000001, 0)], [], [(0, 1, 2)])
        sliver = bpy.data.objects.new('Sliver', mesh); bpy.context.scene.collection.objects.link(sliver)
        result = service.analyze(bridge, sliver.name, {'allow_boundary': True})
        check('sliver_detected', result['metrics']['sliver_triangles'] == 1 and not result['passed'])
        mesh2 = bpy.data.meshes.new('NonManifoldMesh')
        mesh2.from_pydata([(0, 0, 0), (1, 0, 0), (.5, 1, 0), (.5, -1, 0), (.5, 0, 1)], [],
                         [(0, 1, 2), (1, 0, 3), (0, 1, 4)])
        nonmanifold = bpy.data.objects.new('NonManifold', mesh2); bpy.context.scene.collection.objects.link(nonmanifold)
        audit = service.analyze(bridge, nonmanifold.name, {'allow_boundary': True})
        check('nonmanifold_detected', audit['metrics']['non_manifold_edges'] == 1 and not audit['passed'])
        protected_group = obj.vertex_groups.new(name='PROTECTED')
        protected_group.add(list(range(len(obj.data.vertices))),1,'REPLACE')
        uv = obj.data.uv_layers.new(name='ExistingUV')
        for loop in obj.data.loops: uv.uv[loop.index].vector = (.2,.3)
        a = bpy.data.materials.new('ProtectedMaterialA'); b = bpy.data.materials.new('ProtectedMaterialB')
        obj.data.materials.append(a); obj.data.materials.append(b)
        copied = obj.copy(); copied.data=obj.data.copy(); copied.name='ChangedProtected'; bpy.context.scene.collection.objects.link(copied)
        definition = {'regions': [region('PROTECTED','PROTECTED',protected=True)]}
        copied.data.uv_layers['ExistingUV'].uv[0].vector.x += .1
        result = bridge.run(RETOPOLOGY_RUNTIME + '\nresult=rt_protected(m5_obj(params["high"]),m5_obj(params["low"]),params["options"])',
                            {'high':obj.name,'low':copied.name,'options':definition})
        check('protected_UV_change_detected', not result['passed'] and result['metrics']['regions'][0]['uv_mismatch_vertices'] > 0)
        copied.data.uv_layers['ExistingUV'].uv[0].vector.x -= .1
        copied.data.polygons[0].material_index=1
        result = bridge.run(RETOPOLOGY_RUNTIME + '\nresult=rt_protected(m5_obj(params["high"]),m5_obj(params["low"]),params["options"])',
                            {'high':obj.name,'low':copied.name,'options':definition})
        check('protected_material_change_detected', not result['passed'] and result['metrics']['regions'][0]['material_mismatch_vertices'] > 0)
        # Moving no vertex is insufficient: a changed diagonal alters a warped protected quad.
        warped_mesh = bpy.data.meshes.new('WarpedProtectedMesh')
        warped_mesh.from_pydata([(0,0,0),(1,0,0),(1,1,.1),(0,1,0)],[],[(0,1,2,3)])
        warped = bpy.data.objects.new('WarpedProtected',warped_mesh); bpy.context.scene.collection.objects.link(warped)
        group = warped.vertex_groups.new(name='PROTECTED'); group.add([0,1,2,3],1,'REPLACE')
        warped_mesh.calc_loop_triangles()
        diagonal = set(warped_mesh.loop_triangles[0].vertices) & set(warped_mesh.loop_triangles[1].vertices)
        opposite = [(0,1,3),(1,2,3)] if diagonal == {0,2} else [(0,1,2),(0,2,3)]
        changed_mesh = bpy.data.meshes.new('ChangedWarpedMesh')
        changed_mesh.from_pydata([tuple(v.co) for v in warped_mesh.vertices],[],opposite)
        changed = warped.copy(); changed.data=changed_mesh; changed.name='ChangedWarped'; bpy.context.scene.collection.objects.link(changed)
        changed.vertex_groups.new(name='PROTECTED').add([0,1,2,3],1,'REPLACE')
        result = bridge.run(RETOPOLOGY_RUNTIME + '\nresult=rt_protected(m5_obj(params["high"]),m5_obj(params["low"]),params["options"])',
                            {'high':warped.name,'low':changed.name,'options':definition})
        row = result['metrics']['regions'][0]
        check('protected_interior_change_without_vertex_motion_detected', not result['passed']
              and row['max_vertex_displacement_m'] == 0 and row['max_weight_error'] == 0
              and row['max_surface_deviation_m'] > row['tolerance_m'], metrics=row)
        preserved = warped.copy(); preserved.data=warped_mesh.copy(); preserved.name='PreservedWarped'; bpy.context.scene.collection.objects.link(preserved)
        result = bridge.run(RETOPOLOGY_RUNTIME + '\nlow=m5_obj(params["low"])\ncount=rt_exact_protected_triangulation(low,[0]*len(low.data.vertices))\nresult=rt_protected(m5_obj(params["high"]),low,params["options"])\nresult["triangulated_quads"]=count',
                            {'high':warped.name,'low':preserved.name,'options':definition})
        check('protected_installed_tessellation_preserved', result['passed'] and result['triangulated_quads'] == 1,
              metrics=result['metrics'])
        # Two coplanar-near surfaces with equally plausible normals: projection must reject ambiguity.
        mesh3 = bpy.data.meshes.new('NearbyHighMesh')
        mesh3.from_pydata([(0,0,0),(1,0,0),(0,1,0),(0,0,.0001),(1,0,.0001),(0,1,.0001)], [], [(0,1,2),(3,4,5)])
        high = bpy.data.objects.new('NearbyHigh',mesh3); bpy.context.scene.collection.objects.link(high)
        lowmesh = bpy.data.meshes.new('NearbyLowMesh'); lowmesh.from_pydata([(0,0,.00005),(1,0,.00005),(0,1,.00005)],[],[(0,1,2)])
        low = bpy.data.objects.new('NearbyLow',lowmesh); bpy.context.scene.collection.objects.link(low)
        relation = {'metadata': {}}
        rejected('unrelated_nearby_surface_rejected', lambda: bridge.run(RETOPOLOGY_RUNTIME + '\nresult={"rows":m5_correspondence(m5_obj(params["high"]),m5_obj(params["low"]),params["options"])}',
            {'high':high.name,'low':low.name,'options':{'max_distance_m':.001,'ambiguity_distance_m':.0001}}),
            'AMBIGUOUS_SURFACE_CORRESPONDENCE')
        report['maximum_payload_bytes'] = max(bridge.sizes)
        report['passed'] = all(row['passed'] for row in report['checks'].values())
        save()
    except BaseException:
        report['exception'] = traceback.format_exc(); report['passed'] = False; save(); raise


def main():
    if '--inside' in sys.argv:
        output = Path(sys.argv[sys.argv.index('--inside') + 1]); inside(output); return
    parser = argparse.ArgumentParser()
    parser.add_argument('--blender', default=r'C:\Program Files\Blender Foundation\Blender 5.2\blender.exe')
    args = parser.parse_args()
    label = 'm5r-' + uuid4().hex[:8]
    output = Path(tempfile.gettempdir()) / 'bpm5r' / label; output.mkdir(parents=True)
    process = subprocess.run([args.blender, '--background', '--factory-startup', '--disable-autoexec', '--python',
                              str(Path(__file__).resolve()), '--', '--inside', str(output)], capture_output=True, text=True)
    target = ROOT / 'test-artifacts/m5-retopology' / label; target.mkdir(parents=True)
    (target / 'blender.log').write_text(process.stdout + process.stderr, encoding='utf8')
    if (output / 'report.json').exists():
        shutil.copy2(output / 'report.json', target / 'report.json')
        report = json.loads((target / 'report.json').read_text())
        print(json.dumps({'report': str(target / 'report.json'), 'artifact_root': str(output), 'passed': report['passed'],
                          'checks': len(report['checks']), 'failed': [k for k,v in report['checks'].items() if not v['passed']],
                          'exception': report.get('exception')}, indent=2))
        if not report['passed']: sys.exit(1)
    else:
        print(process.stdout + process.stderr); sys.exit(1)


if __name__ == '__main__': main()
