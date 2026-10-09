"""M5 transfer/game-normal qualification in a separate factory Blender; never opens user assets."""
from pathlib import Path
import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import traceback
import types
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]


def inside(output):
    import bpy
    sys.path.insert(0, str(ROOT / 'src'))
    package = types.ModuleType('blender_mcp')
    package.__path__ = [str(ROOT / 'src/blender_mcp')]
    sys.modules['blender_mcp'] = package
    from blender_mcp.production.common.bridge import build_code
    from blender_mcp.production.common.storage import Storage
    from blender_mcp.production.common import m5
    from blender_mcp.production.common.asset_safety import register_master
    from blender_mcp.production.transfer import service as transfer
    from blender_mcp.production.game_mesh import service as game

    class LocalBridge:
        def __init__(self):
            self.sizes = []
        def run(self, body, params=None):
            code = build_code(body, params)
            self.sizes.append(len(code.encode()))
            namespace = {}
            exec(code.rsplit('print(', 1)[0], namespace)
            return namespace['result']

    bridge = LocalBridge()
    storage = Storage(output / 's')
    report = {'blender': bpy.app.version_string, 'checks': {}, 'metrics': {}, 'fixtures': {}}
    def save():
        (output / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf8')
    def check(name, passed, **details):
        report['checks'][name] = {'passed': bool(passed), **details}
        save()
        print('CHECK', name, 'PASS' if passed else 'FAIL', flush=True)
    def refused(name, function, code):
        try:
            result = function()
            text = json.dumps(result)
            failed = not result.get('proposal', {}).get('supported', True) or result.get('status') == 'error'
            check(name, code in text and failed, result=result)
        except Exception as exc:
            check(name, code in str(exc), error=str(exc))
    def mesh(name, nx, ny, rig):
        vertices = [(2*x/(nx-1)-1, .4*y/(ny-1)-.2, .05) for y in range(ny) for x in range(nx)]
        faces = [(y*nx+x, y*nx+x+1, (y+1)*nx+x+1, (y+1)*nx+x) for y in range(ny-1) for x in range(nx-1)]
        data = bpy.data.meshes.new(name+'Data')
        data.from_pydata(vertices, [], faces)
        data.update()
        obj = bpy.data.objects.new(name, data)
        bpy.context.scene.collection.objects.link(obj)
        modifier = obj.modifiers.new('FixtureArmature', 'ARMATURE')
        modifier.object = rig
        a, b = obj.vertex_groups.new(name='J0'), obj.vertex_groups.new(name='J1')
        for vertex in data.vertices:
            t = (vertex.co.x+1)/2
            if t < 1:
                a.add([vertex.index], 1-t, 'REPLACE')
            if t > 0:
                b.add([vertex.index], t, 'REPLACE')
        uv = data.uv_layers.new(name='GameUV')
        for loop in data.loops:
            vertex = data.vertices[loop.vertex_index]
            uv.uv[loop.index].vector = ((vertex.co.x+1)/2*.8+.1, (vertex.co.y+.2)/.4*.8+.1)
        return obj

    bpy.ops.wm.read_factory_settings(use_empty=True)
    armature = bpy.data.armatures.new('TransferFixtureRigData')
    rig = bpy.data.objects.new('TransferFixtureRig', armature)
    bpy.context.scene.collection.objects.link(rig)
    bpy.context.view_layer.objects.active = rig
    rig.select_set(True)
    bpy.ops.object.mode_set(mode='EDIT')
    for i in range(3):
        bone = armature.edit_bones.new('J'+str(i))
        bone.head, bone.tail = (i-1, 0, 0), (i, 0, 0)
        if i:
            bone.parent = armature.edit_bones['J'+str(i-1)]
    bpy.ops.object.mode_set(mode='OBJECT')
    high, low = mesh('TransferHigh', 21, 11, rig), mesh('TransferLow', 5, 3, rig)
    low.vertex_groups['J0'].add(list(range(len(low.data.vertices))), 1, 'REPLACE')
    low.vertex_groups['J1'].remove(list(range(len(low.data.vertices))))
    for name in ('LeftSurface', 'RightSurface'):
        high.data.materials.append(bpy.data.materials.new(name))
    for polygon in high.data.polygons:
        polygon.material_index = int(polygon.center.x > 0)
    color = high.data.color_attributes.new(name='RegionColor', type='FLOAT_COLOR', domain='POINT')
    for vertex in high.data.vertices:
        color.data[vertex.index].color = ((vertex.co.x+1)/2, (vertex.co.y+.2)/.4, .25, 1)
    corner_color = high.data.color_attributes.new(name='CornerColor', type='FLOAT_COLOR', domain='CORNER')
    for loop in high.data.loops:
        x = (high.data.vertices[loop.vertex_index].co.x+1)/2
        corner_color.data[loop.index].color = (x, 0.5, 0.75, 1)
    face_attribute = high.data.attributes.new(name='SemanticID', type='INT', domain='FACE')
    for polygon in high.data.polygons:
        face_attribute.data[polygon.index].value = 4 + int(polygon.center.x > 0)
    high['game_region'] = 'explicit_arm_fixture'
    high.data['semantic_pipeline'] = 'controlled_m5'
    report['fixtures'] = {'rig': rig.name, 'high': high.name, 'low': low.name, 'high_triangles': 400, 'low_triangles': 16,
                          'covers': ['weighted_source', 'shoulder', 'elbow', 'knee', 'multiple_materials', 'color_attributes', 'single_uv_tile', 'tangent_defect', 'shape_keys']}
    before_high = m5.signature(bridge, high.name)
    before_low = m5.signature(bridge, low.name)
    initial = m5.create_relationship(bridge, storage, 'fixture-relation', high.name, before_high['fingerprint'], {'target_object': low.name})
    relationship = initial['relationship']
    candidate = bpy.data.objects[initial['candidate']]
    check('candidate_object_mesh_separation', candidate is not low and candidate.data is not low.data and initial['source_intact'])
    check('changed_topology_fixture', len(high.data.vertices) != len(candidate.data.vertices))
    settings = {'groups': ['J0', 'J1'], 'influence_limit': 2}
    planned = transfer.transfer(bridge, storage, 'weight-proposal', 'weights', relationship, 'propose', settings)
    check('weight_surface_proposal', planned['proposal']['supported'], proposal=planned['proposal'])
    applied = transfer.transfer(bridge, storage, 'weight-apply', 'weights', mode='apply', operation=planned['operation'])
    check('weight_apply_verified', applied['numeric_verification'] == 'pass', metrics=applied['metrics'])
    weighted = bpy.data.objects[applied['candidate']]
    max_error = 0
    for vertex in weighted.data.vertices:
        t = (vertex.co.x+1)/2
        values = {weighted.vertex_groups[g.group].name: g.weight for g in vertex.groups}
        max_error = max(max_error, abs(values.get('J0', 0)-(1-t)), abs(values.get('J1', 0)-t))
    check('independent_analytic_weight_result', max_error < 1e-6, maximum_weight_error=max_error)
    check('source_and_previous_low_preserved', m5.signature(bridge, high.name) == before_high and m5.signature(bridge, low.name) == before_low)
    verified = transfer.transfer(bridge, storage, 'weight-verify', 'weights', mode='verify', operation=planned['operation'])
    check('weight_verify_remeasured', verified['numeric_verification'] == 'pass')
    qa = transfer.validate_weights(bridge, storage, applied['relationship'], settings)
    check('weight_qa_normalized', qa['passed'] and qa['maximum_influences'] == 2 and qa['unweighted_vertices'] == 0, metrics=qa)
    report['metrics']['weights'] = qa
    stress = [{'region': 'shoulder', 'bone': 'J0', 'axis': 'Z', 'angle_degrees': 25},
              {'region': 'elbow', 'bone': 'J1', 'axis': 'Z', 'angle_degrees': 35},
              {'region': 'knee', 'bone': 'J0', 'axis': 'Y', 'angle_degrees': 15}]
    stress_settings = dict(settings, stress_poses=stress, deformation_error_m=.03, deformation_fraction_tolerance=.01)
    stress_qa = transfer.validate_weights(bridge, storage, applied['relationship'], stress_settings, 'stress-qa')
    check('deformation_stress_same_pose_surfaces', stress_qa['passed'] and stress_qa['deformation_qa']['state_restored'], metrics=stress_qa['deformation_qa'])
    report['metrics']['deformation'] = stress_qa['deformation_qa']
    regions = transfer.deformation_regions(bridge, storage, applied['relationship'], {'regions': {'shoulder': ['J0'], 'elbow': ['J1'], 'knee': ['J0']}, 'minimum_region_vertices': 6}, 'region-analysis')
    check('deformation_regions_explicit', regions['passed'] and regions['region_count'] == 3, metrics=regions)
    bad = weighted.copy()
    bad.data = weighted.data.copy()
    bad.name = 'BadDeformationWeights'
    bpy.context.scene.collection.objects.link(bad)
    bad.vertex_groups['J0'].add(list(range(len(bad.data.vertices))), 1, 'REPLACE')
    bad.vertex_groups['J1'].remove(list(range(len(bad.data.vertices))))
    bad_relation = m5.create_relationship(bridge, storage, 'bad-deform-relation', high.name, m5.signature(bridge, high.name)['fingerprint'], {'target_object': bad.name})
    bad_stress = transfer.validate_weights(bridge, storage, bad_relation['relationship'], stress_settings, 'bad-deform-stress')
    check('deformation_regression_rejected', not bad_stress['passed'] and not bad_stress['deformation_qa']['passed'] and bad_stress['deformation_qa']['state_restored'], metrics=bad_stress['deformation_qa'])
    # The installed tessellator changes this bent quad's diagonal at Y=90 degrees. Authored
    # polygon connectivity is unchanged and identical weighted copies must still pass stress QA.
    quad = mesh('NonplanarQuadTessellationFixture', 2, 2, rig)
    quad.data.vertices[3].co.z = .25
    quad.data.update()
    from blender_mcp.production.transfer.runtime import TRANSFER_RUNTIME
    diagonal = bridge.run(TRANSFER_RUNTIME + '''
obj=m5_obj(params['object_name'])
reference=mt_evaluated(obj)
bone=mt_rig(obj).pose.bones['J1']
original_mode=bone.rotation_mode
original_quaternion=bone.rotation_quaternion.copy()
try:
    bone.rotation_mode='QUATERNION'
    bone.rotation_quaternion=Quaternion(Vector((0,1,0)),math.radians(90))
    bpy.context.view_layer.update()
    posed=mt_evaluated(obj)
    measured=mt_deform_metrics(reference,posed,params['options'])
    changed=dict(posed,polygons=[(0,1,2,3)])
    rejected=False
    try:
        mt_deform_metrics(reference,changed,params['options'])
    except ValueError as exc:
        rejected='DYNAMIC_TOPOLOGY_DEFORMATION_UNSUPPORTED' in str(exc)
    result={'reference_triangles':reference['triangles'],'posed_triangles':posed['triangles'],
            'polygons_preserved':reference['polygons']==posed['polygons'],
            'authored_topology_change_rejected':rejected,'metrics':measured}
finally:
    bone.rotation_mode=original_mode
    bone.rotation_quaternion=original_quaternion
    bpy.context.view_layer.update()
''', {'object_name': quad.name, 'options': transfer.options()})
    quad_relation = m5.create_relationship(bridge, storage, 'quad-tessellation-relation', quad.name,
                                         m5.signature(bridge, quad.name)['fingerprint'])
    quad_stress = transfer.validate_weights(bridge, storage, quad_relation['relationship'],
            dict(settings, stress_poses=[{'region': 'elbow', 'bone': 'J1', 'axis': 'Y', 'angle_degrees': 90}]), 'quad-tessellation-stress')
    check('quad_tessellation_changes_without_false_topology_failure',
          diagonal['reference_triangles'] != diagonal['posed_triangles'] and diagonal['polygons_preserved'] and
          quad_stress['passed'] and quad_stress['deformation_qa']['state_restored'], diagonal=diagonal, stress=quad_stress['deformation_qa'])
    check('actual_authored_topology_change_still_rejected', diagonal['authored_topology_change_rejected'])
    protected_relation = m5.create_relationship(bridge, storage, 'protected-quad-relation', quad.name,
        m5.signature(bridge, quad.name)['fingerprint'], {'metadata': {'regions': [{'name': 'LockedQuad',
        'role': 'locked', 'protected': True, 'tolerance_m': 1e-5,
        'definition': {'method': 'bounds', 'min': [-2,-1,-1], 'max': [2,1,1]}}]}})
    refused('protected_quad_diagonal_surface_change_rejected', lambda: game.prepare(bridge, storage,
            'protected-quad-alternate', 'triangulation', protected_relation['relationship'], 'propose',
            {'quad_method': 'FIXED_ALTERNATE'}), 'PROTECTED_REGION_MODIFIED')
    protected_plan = game.prepare(bridge, storage, 'protected-quad-fixed', 'triangulation',
                                 protected_relation['relationship'], 'propose', {'quad_method': 'FIXED'})
    protected_apply = game.prepare(bridge, storage, 'protected-quad-fixed-apply', 'triangulation',
                                  mode='apply', operation=protected_plan['operation'])
    protection = protected_apply['metrics']['protected_regions']
    check('protected_quad_installed_diagonal_preserved', protected_apply['numeric_verification'] == 'pass' and
          protection['passed'] and protection['metrics']['regions'][0]['surface_samples'] > 0, metrics=protection)
    bad_weight = transfer.validate_weights(bridge, storage, relationship, dict(settings, side_pairs=[{'positive': 'J0', 'negative': 'J1'}], leakage_weight_limit=.1))
    check('bad_weight_side_leakage_rejected', not bad_weight['passed'] and bad_weight['left_right_leakage_vertices'] > 0)
    refused('influence_limit_rejected', lambda: transfer.validate_weights(bridge, storage, applied['relationship'], dict(settings, influence_limit=1)), 'EXCESSIVE_INFLUENCES')

    attr_options = {'attributes': ['RegionColor', 'CornerColor', 'SemanticID'], 'custom_properties': {'object': ['game_region'], 'mesh': ['semantic_pipeline']}}
    attr_plan = transfer.transfer(bridge, storage, 'attr-plan', 'attributes', applied['relationship'], 'propose', attr_options)
    attr_apply = transfer.transfer(bridge, storage, 'attr-apply', 'attributes', mode='apply', operation=attr_plan['operation'])
    check('attribute_transfer_verified', attr_apply['numeric_verification'] == 'pass', metrics=attr_apply['metrics'])
    attributed = bpy.data.objects[attr_apply['candidate']]
    independently_correct = all(abs(attributed.data.color_attributes['RegionColor'].data[v.index].color[0]-(v.co.x+1)/2) < 1e-6 for v in attributed.data.vertices)
    check('independent_point_color_interpolation', independently_correct)
    check('material_semantic_regions', all(p.material_index == int(p.center.x > 0) for p in attributed.data.polygons) and [m.name for m in attributed.data.materials] == ['LeftSurface', 'RightSurface'])
    check('explicit_custom_properties', attributed['game_region'] == 'explicit_arm_fixture' and attributed.data['semantic_pipeline'] == 'controlled_m5')
    refused('arbitrary_attribute_refused', lambda: transfer.transfer(bridge, storage, 'bad-attr', 'attributes', attr_apply['relationship'], 'analyze', {'attributes': ['position']}), 'INTERNAL_ATTRIBUTE_REFUSED')
    # A single quad spanning both materials must not silently choose its centre's semantic ID.
    crossing = mesh('CrossingMaterialBoundary', 2, 2, rig)
    crossing_relation = m5.create_relationship(bridge, storage, 'crossing-relation', high.name, m5.signature(bridge, high.name)['fingerprint'], {'target_object': crossing.name})
    refused('crossed_material_boundary_rejected', lambda: transfer.transfer(bridge, storage, 'crossing-attr', 'attributes', crossing_relation['relationship'], 'propose', {'materials': True}), 'MATERIAL_BOUNDARY_CROSSED')

    normal_plan = game.prepare(bridge, storage, 'norm-plan', 'normals', attr_apply['relationship'], 'propose')
    normal_apply = game.prepare(bridge, storage, 'norm-apply', 'normals', mode='apply', operation=normal_plan['operation'])
    check('normals_validated', normal_apply['numeric_verification'] == 'pass', metrics=normal_apply['metrics'])
    tri_qa = game.validate(bridge, storage, normal_apply['relationship'], 'triangulation', {'stress_poses': stress}, 'tri-stress-validation')
    check('triangulation_deterministic_two_copies', tri_qa['passed'] and tri_qa['deterministic'] and tri_qa['triangles'] == 16, metrics=tri_qa)
    tri_plan = game.prepare(bridge, storage, 'tri-plan', 'triangulation', normal_apply['relationship'], 'propose', {'stress_poses': stress})
    tri_apply = game.prepare(bridge, storage, 'tri-apply', 'triangulation', mode='apply', operation=tri_plan['operation'])
    check('triangulated_evaluation_copy', tri_apply['numeric_verification'] == 'pass' and len(bpy.data.objects[normal_apply['candidate']].data.polygons) == 8 and len(bpy.data.objects[tri_apply['candidate']].data.polygons) == 16, metrics=tri_apply['metrics'])
    check('triangulation_explicit_deformation_qa', tri_apply['metrics']['deformation_qa']['passed'] and tri_apply['metrics']['deformation_qa']['state_restored'])
    tangent = game.validate(bridge, storage, tri_apply['relationship'], 'tangents')
    check('actual_blender_tangent_basis', tangent['passed'] and tangent['maximum_normal_tangent_dot'] < 1e-6, metrics=tangent)
    report['metrics']['tangents'] = tangent
    no_uv = bpy.data.objects[tri_apply['candidate']]
    uv_values = [tuple(v.vector) for v in no_uv.data.uv_layers['GameUV'].uv]
    for v in no_uv.data.uv_layers['GameUV'].uv:
        v.vector = (0, 0)
    bad_tangent = game.validate(bridge, storage, tri_apply['relationship'], 'tangents')
    check('degenerate_uv_tangent_rejected', not bad_tangent['passed'] and 'TANGENT_DEGENERATE_UV' in ';'.join(bad_tangent['errors']))
    for v, value in zip(no_uv.data.uv_layers['GameUV'].uv, uv_values):
        v.vector = value
    custom = bpy.data.objects[normal_apply['candidate']]
    custom.data.normals_split_custom_set([(0, 0, 1)]*len(custom.data.loops))
    custom_relation = m5.create_relationship(bridge, storage, 'custom-relation', high.name, m5.signature(bridge, high.name)['fingerprint'], {'target_object': custom.name})
    refused('custom_normal_rebuild_requires_confirmation', lambda: game.prepare(bridge, storage, 'bad-normal', 'normals', custom_relation['relationship'], 'propose', {'custom_normals': 'rebuild'}), 'CUSTOM_NORMAL_REBUILD_REQUIRES_CONFIRMATION')
    custom_plan = game.prepare(bridge, storage, 'custom-plan', 'normals', custom_relation['relationship'], 'propose')
    custom_apply = game.prepare(bridge, storage, 'custom-apply', 'normals', mode='apply', operation=custom_plan['operation'])
    check('custom_normals_preserved', custom_apply['numeric_verification'] == 'pass' and bpy.data.objects[custom_apply['candidate']].data.has_custom_normals)

    high.shape_key_add(name='Basis')
    morph = high.shape_key_add(name='ControlledBulge')
    for item in morph.data:
        item.co.z += .02*max(0, 1-abs(item.co.x))
    morph_before = [list(item.co) for item in morph.data]
    morph_relation = m5.create_relationship(bridge, storage, 'morph-relation', high.name, m5.signature(bridge, high.name)['fingerprint'], {'target_object': low.name})
    morph_report = transfer.analyze_morphs(bridge, storage, morph_relation['relationship'])
    check('morph_loss_reported_honestly', not morph_report['transfer_feasible'] and morph_report['decision'] == 'MANUAL_OR_LATER_PIPELINE_REQUIRED' and morph_report['key_count'] == 2, metrics=morph_report)
    morph_plan = transfer.transfer(bridge, storage, 'morph-plan', 'morphs', morph_relation['relationship'], 'propose')
    refused('morph_transfer_safely_rejected', lambda: transfer.transfer(bridge, storage, 'morph-apply', 'morphs', mode='apply', operation=morph_plan['operation']), 'MANUAL_OR_LATER_PIPELINE_REQUIRED')
    check('morph_source_intact', [list(item.co) for item in high.data.shape_keys.key_blocks['ControlledBulge'].data] == morph_before and len(high.data.shape_keys.key_blocks) == 2)
    exact_morph = m5.create_relationship(bridge, storage, 'exact-morph-relation', high.name, m5.signature(bridge, high.name)['fingerprint'])
    morph_tri = game.validate(bridge, storage, exact_morph['relationship'], 'triangulation')
    check('triangulation_exact_morph_preservation', morph_tri['passed'] and morph_tri['morphs_preserved_exactly'], metrics=morph_tri)
    driver = high.data.shape_keys.key_blocks['ControlledBulge'].driver_add('value').driver
    driver.type, driver.expression = 'SCRIPTED', '0.0'
    from blender_mcp.production.transfer.runtime import TRANSFER_RUNTIME
    animated_report = bridge.run(TRANSFER_RUNTIME + '\nobj=m5_obj(params["object_name"])\nresult=mt_morph_analysis(obj,obj)', {'object_name': high.name})
    check('animated_morph_dependencies_safely_rejected', not animated_report['transfer_feasible'] and not animated_report['animated_dependencies_supported'] and animated_report['decision'] == 'MANUAL_OR_LATER_PIPELINE_REQUIRED')

    fixture_path = output/'weighted-attribute-fixture.blend'
    bpy.ops.wm.save_as_mainfile(filepath=str(fixture_path))
    register_master(bridge, storage)
    refused('master_mutation_rejected', lambda: game.prepare(bridge, storage, 'master-apply', 'normals', mode='apply', operation=custom_plan['operation']), 'MASTER_ASSET_READ_ONLY')
    report['fixture_file'] = str(fixture_path)
    report['fixture_sha256'] = hashlib.sha256(fixture_path.read_bytes()).hexdigest()
    report['maximum_payload_bytes'] = max(bridge.sizes)
    report['passed'] = all(row['passed'] for row in report['checks'].values())
    save()
    print('REPORT', output/'report.json', flush=True)
    if not report['passed']:
        raise RuntimeError('M5 transfer/game-mesh probe failed')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--inside', action='store_true')
    parser.add_argument('--output')
    parser.add_argument('--blender', default=r'C:\Program Files\Blender Foundation\Blender 5.2\blender.exe')
    args = parser.parse_args(sys.argv[sys.argv.index('--')+1:] if '--' in sys.argv else None)
    if args.inside:
        output = Path(args.output)
        try:
            inside(output)
        except BaseException:
            (output/'error.txt').write_text(traceback.format_exc(), encoding='utf8')
            raise
    else:
        output = ROOT/'test-artifacts'/('m5-transfer-'+uuid4().hex[:8])
        output.mkdir(parents=True)
        command = [args.blender, '--background', '--factory-startup', '--disable-autoexec', '--python', str(Path(__file__).resolve()), '--', '--inside', '--output', str(output)]
        with (output/'blender.log').open('w', encoding='utf8') as log:
            result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT)
        print(output/'report.json')
        if (output/'error.txt').exists():
            print((output/'error.txt').read_text())
        raise SystemExit(result.returncode or (1 if not (output/'report.json').exists() or not json.loads((output/'report.json').read_text()).get('passed') else 0))

if __name__ == '__main__':
    main()
