"""Reproducible M9 signatures, isolated candidates and shader feature probes.

Run a single --case in a fresh Blender --background --factory-startup process.
The local adapter executes current production runtimes through build_code and
safe mode. It does not test MCP transport, render artistic quality or open assets.
Historical probe5/probe6 regression cases are retained without embedded runtime.
"""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import traceback
import types

ROOT = Path(__file__).resolve().parents[2]
package = types.ModuleType('blender_mcp')
package.__path__ = [str(ROOT / 'src/blender_mcp')]
sys.modules['blender_mcp'] = package
sys.path.insert(0, str(ROOT / 'src'))
from blender_mcp.production.common.bridge import build_code
from blender_mcp.production.materials import candidate, graph, integrity, inspection, model


class Probe:
    def __init__(self):
        self.checks = {}
        self.qa_raw = {}
        self.qa_reports = {}
        self.payload_sizes = []

    def run(self, body, params):
        code = build_code(body, params)
        self.payload_sizes.append(len(code.encode()))
        namespace = {}
        exec(code.rsplit('print(', 1)[0], namespace)
        return namespace['result']

    def check(self, name, passed):
        self.checks[name] = bool(passed)
        if not passed:
            raise AssertionError(name)

    def rejection(self, name, call, code):
        try:
            call()
        except ValueError as error:
            self.check(name, code in str(error))
            return
        self.check(name, False)

    def status(self, params):
        return self.run(integrity.STATUS, params)

    def changed(self, name, mutate, revert, params):
        before = self.status(params)['runtime_fingerprint']
        try:
            mutate()
            after = self.status(params)['runtime_fingerprint']
        finally:
            revert()
        restored = self.status(params)['runtime_fingerprint']
        self.check(name, before != after and before == restored)


def definitions():
    base = {'channels': {
        'base_color': {'image':'Color', 'component':'COLOR', 'colorspace':'sRGB'},
        'roughness': {'image':'Data', 'component':'R', 'colorspace':'Non-Color'},
        'normal': {'image':'Normal', 'component':'COLOR', 'colorspace':'Non-Color'}},
        'normal_convention':'OPENGL'}
    variants = [base]
    for values in ({'subsurface_weight':.3}, {'transmission_weight':.8, 'ior':1.31},
                   {'alpha':.4}, {'emission_color':[.7,.2,.1,1], 'emission_strength':3}):
        variants.append(dict(deepcopy(base), parameters=values))
    for mode, engine in [('BUMP','EEVEE'), ('DISPLACEMENT','CYCLES')]:
        item = deepcopy(base)
        item['channels']['height'] = {'image':'Data', 'component':'R', 'colorspace':'Non-Color'}
        item.update(height_mode=mode, renderer=engine)
        variants.append(item)
    normalized = [model.normalize_definition(value) for value in variants]
    negatives = [
        ('normal_unknown', dict(deepcopy(normalized[0]), normal_convention='UNKNOWN'),
         'MATERIAL_NORMAL_CONVENTION_UNSUPPORTED'),
        ('directx_not_guessed', dict(deepcopy(normalized[0]), normal_convention='DIRECTX'),
         'MATERIAL_NORMAL_CONVENTION_UNSUPPORTED'),
        ('eevee_true_displacement_rejected', dict(deepcopy(normalized[-1]), renderer='EEVEE'),
         'MATERIAL_DISPLACEMENT_UNSUPPORTED')]
    return normalized, negatives


CANDIDATE_CREATE = candidate.CREATE


def preservation(probe):
    import bpy
    check = probe.check
    rejection = probe.rejection
    changed = probe.changed
    m9_status = probe.status
    m9_make_candidate = lambda params, endpoint=CANDIDATE_CREATE: probe.run(endpoint, params)
    m9_build_shader = lambda params: probe.run(graph.BUILD, params)
    SOCKETS = model.SOCKETS
    DEFINITION = model.normalize_definition({'category':'METAL', 'parameters':{'metallic':.7},
        'channels':{'roughness':{'image':'M9_Image','component':'R','colorspace':'Non-Color'},
                    'ao':{'image':'M9_Image','component':'G','colorspace':'Non-Color'}}})
    obj = bpy.data.objects['Cube']
    mat = bpy.data.materials.new('M9_Source')
    mat.use_nodes = True
    obj.data.materials.clear()
    obj.data.materials.append(mat)
    image = bpy.data.images.new('M9_Image', width=8, height=8)
    image.pixels = [0.25, 0.5, 0.75, 1.0] * 64
    nodes = mat.node_tree.nodes
    shader = next((n for n in nodes if n.type == 'BSDF_PRINCIPLED'))
    params = {'object_name': obj.name, 'material_name': mat.name, 'options': {'image_names': [image.name]}}
    obj.shape_key_add(name='Basis')
    shape = obj.shape_key_add(name='Expression')
    shape.data[0].co.z += 0.1
    weights = obj.vertex_groups.new(name='Bone')
    weights.add([0, 1, 2], 0.8, 'REPLACE')
    obj.keyframe_insert(data_path='location', frame=1)
    obj.keyframe_insert(data_path='location', frame=10)
    action = obj.animation_data.action
    track = obj.animation_data.nla_tracks.new()
    track.name = 'PreserveNLA'
    strip = track.strips.new('PreserveBody', 1, action)
    bpy.ops.object.armature_add()
    rig = bpy.context.object
    rig.name = 'PreserveRig'
    modifier = obj.modifiers.new('PreserveArmature', 'ARMATURE')
    modifier.object = rig
    initial = m9_status(params)
    check('stable_snapshot', initial == m9_status(params))
    original = shader.inputs['Roughness'].default_value
    changed('shader_value_signed', lambda: setattr(shader.inputs['Roughness'], 'default_value', 0.123), lambda: setattr(shader.inputs['Roughness'], 'default_value', original), params)
    px = list(image.pixels)
    changed('cached_pixels_signed', lambda: image.pixels.__setitem__(0, 0.9), lambda: image.pixels.__setitem__(slice(None), px), params)
    uv = obj.data.uv_layers.active.data[0]
    old = list(uv.uv)
    changed('uv_signed', lambda: setattr(uv, 'uv', [0.123, 0.234]), lambda: setattr(uv, 'uv', old), params)
    polygon = obj.data.polygons[0]
    old = polygon.use_smooth
    changed('smooth_signed', lambda: setattr(polygon, 'use_smooth', not old), lambda: setattr(polygon, 'use_smooth', old), params)
    old = shape.data[0].co.z
    changed('shape_coordinates_signed', lambda: setattr(shape.data[0].co, 'z', old + 0.05), lambda: setattr(shape.data[0].co, 'z', old), params)
    curve = next(iter(action.layers[0].strips[0].channelbags[0].fcurves))
    old = curve.keyframe_points[0].co.y
    changed('action_keys_signed', lambda: setattr(curve.keyframe_points[0].co, 'y', old + 0.2), lambda: setattr(curve.keyframe_points[0].co, 'y', old), params)
    old = strip.influence
    changed('nla_signed', lambda: setattr(strip, 'influence', 0.5), lambda: setattr(strip, 'influence', old), params)
    pose = rig.pose.bones[0]
    old = pose.ik_stretch
    changed('rig_pose_configuration_signed', lambda: setattr(pose, 'ik_stretch', 0.25), lambda: setattr(pose, 'ik_stretch', old), params)
    old = weights.weight(0)
    changed('weights_signed', lambda: weights.add([0], 0.3, 'REPLACE'), lambda: weights.add([0], old, 'REPLACE'), params)
    group = bpy.data.node_groups.new('M9_Group', 'ShaderNodeTree')
    group.interface.new_socket(name='Color', in_out='OUTPUT', socket_type='NodeSocketColor')
    ramp = group.nodes.new('ShaderNodeValToRGB')
    out = group.nodes.new('NodeGroupOutput')
    group.links.new(ramp.outputs['Color'], out.inputs['Color'])
    node = nodes.new('ShaderNodeGroup')
    node.node_tree = group
    mat.node_tree.links.new(node.outputs['Color'], shader.inputs['Base Color'])
    old = list(ramp.color_ramp.elements[0].color)
    changed('nested_ramp_color_signed', lambda: setattr(ramp.color_ramp.elements[0], 'color', [0.1, 0.2, 0.3, 1]), lambda: setattr(ramp.color_ramp.elements[0], 'color', old), params)
    initial = m9_status(params)
    params.update(expected_status=initial, candidate_name='M9_TestCandidate', generation_id='a' * 32, version=1)
    creation = m9_make_candidate(params)
    candidate = bpy.data.objects[creation['candidate_object']]
    cmat = bpy.data.materials[creation['candidate_material']]
    check('source_preserved_after_copy', m9_status(params)['runtime_fingerprint'] == initial['runtime_fingerprint'])
    check('isolated_ids', candidate != obj and candidate.data != obj.data and (cmat != mat))
    check('source_slots_unchanged', obj.material_slots[0].material == mat)
    check('geometry_preserved', creation['candidate_status']['geometry_fingerprint'] == initial['geometry_fingerprint'])
    bparams = dict(params, candidate_object=candidate.name, candidate_material=cmat.name, expected_candidate=creation['candidate_status'], definition=DEFINITION, socket_names=SOCKETS)
    result = m9_build_shader(bparams)
    check('source_preserved_after_build', m9_status(params)['runtime_fingerprint'] == initial['runtime_fingerprint'])
    check('principled_graph_built', len([n for n in cmat.node_tree.nodes if n.type == 'BSDF_PRINCIPLED']) == 1)
    tex = next((n for n in cmat.node_tree.nodes if n.type == 'TEX_IMAGE'))
    check('image_isolated', tex.image != image and image.colorspace_settings.name != 'Non-Color')
    check('candidate_colorspace', tex.image.colorspace_settings.name == 'Non-Color')
    check('cached_pixel_copy_preserved', list(tex.image.pixels) == list(image.pixels))
    check('ao_independent', not any((l.from_node.get('production_channel') == 'ao' and l.to_socket.name == 'Base Color' for l in cmat.node_tree.links)))
    check('source_group_unchanged', ramp.color_ramp.elements[0].color[:] == tuple(old))
    check('candidate_geometry_unchanged', result['candidate_status']['geometry_fingerprint'] == initial['geometry_fingerprint'])
    bparams['expected_candidate'] = result['candidate_status']
    retry = m9_build_shader(bparams)
    check('build_retry_idempotent', retry['idempotent'])
    try:
        m9_make_candidate(params)
    except ValueError as e:
        check('duplicate_candidate_rejected', 'MATERIAL_CANDIDATE_EXISTS' in str(e))
    else:
        check('duplicate_candidate_rejected', False)
    bpy.context.scene['production_asset'] = json.dumps({'role': 'MASTER'})
    params['expected_status'] = m9_status(params)
    before_counts = (len(bpy.data.objects), len(bpy.data.materials))
    try:
        m9_make_candidate(dict(params, candidate_name='Blocked', generation_id='b' * 32))
    except ValueError as e:
        check('master_rejected_before_copy', 'MASTER_ASSET_READ_ONLY' in str(e) and before_counts == (len(bpy.data.objects), len(bpy.data.materials)))
    else:
        check('master_rejected_before_copy', False)


def features(probe):
    import bpy
    check = probe.check
    rejection = probe.rejection
    changed = probe.changed
    m9_status = probe.status
    m9_make_candidate = lambda params, endpoint=CANDIDATE_CREATE: probe.run(endpoint, params)
    m9_build_shader = lambda params: probe.run(graph.BUILD, params)
    SOCKETS = model.SOCKETS
    DEFINITIONS, NEGATIVES = definitions()
    qa_raw = probe.qa_raw
    mat9_material = lambda material, obj, options: inspection.inspect(probe, obj, material, options)
    obj = bpy.data.objects['Cube']
    mat = bpy.data.materials.new('Source')
    mat.use_nodes = True
    obj.data.materials.clear()
    obj.data.materials.append(mat)
    for name, color in [('Color', [0.25, 0.5, 0.75, 1]), ('Normal', [0.5, 0.5, 1, 1]), ('Data', [0.6, 0.6, 0.6, 1])]:
        image = bpy.data.images.new(name, width=8, height=8)
        image.pixels = color * 64
    for index, definition in enumerate(DEFINITIONS):
        params = {'object_name': obj.name, 'material_name': mat.name, 'options': {'image_names': sorted({v['image'] for v in definition['channels'].values()})}, 'candidate_name': 'Candidate' + str(index), 'generation_id': format(index + 1, '032x'), 'version': 1}
        params['expected_status'] = m9_status(params)
        created = m9_make_candidate(params)
        bp = dict(params, **{k: created[k] for k in ['candidate_object', 'candidate_material']}, definition=definition, socket_names=SOCKETS, expected_candidate=created['candidate_status'])
        result = m9_build_shader(bp)
        cmat = bpy.data.materials[created['candidate_material']]
        check('source_' + str(index), result['source_preserved'])
        for node in cmat.node_tree.nodes:
            if node.type == 'TEX_IMAGE':
                check('pixels_' + str(index) + '_' + node.name, list(node.image.pixels) == list(bpy.data.images[node.image['production_m9_source_image']].pixels))
        qa_raw[str(index)] = mat9_material(cmat.name, created['candidate_object'], {'image_names': [], 'uv': {}})
        check('geometry_' + str(index), result['candidate_status']['geometry_fingerprint'] == params['expected_status']['geometry_fingerprint'])
        if definition['height_mode'] == 'DISPLACEMENT':
            check('true_displacement', cmat.displacement_method == 'DISPLACEMENT' and any((l.to_socket.name == 'Displacement' for l in cmat.node_tree.links)))
        if definition['parameters']['transmission_weight'] > 0:
            check('refraction_flag', cmat.use_raytrace_refraction)
        if 'normal' in definition['channels']:
            check('normal_wiring', any((n.type == 'NORMAL_MAP' for n in cmat.node_tree.nodes)))
    params = {'object_name': obj.name, 'material_name': mat.name, 'options': {}, 'candidate_name': 'Negative', 'generation_id': 'f' * 32, 'version': 1}
    params['expected_status'] = m9_status(params)
    created = m9_make_candidate(params)
    for label, definition, expected in NEGATIVES:
        cp = {'object_name': created['candidate_object'], 'material_name': created['candidate_material'], 'options': {}}
        bp = dict(params, **{k: created[k] for k in ['candidate_object', 'candidate_material']}, definition=definition, socket_names=SOCKETS, expected_candidate=m9_status(cp))
        before_counts = (len(bpy.data.materials), len(bpy.data.images))
        before_graph = m9_status(cp)['material_fingerprint']
        rejection(label, lambda: m9_build_shader(bp), expected)
        check(label + '_no_graph_change', before_graph == m9_status(cp)['material_fingerprint'] and before_counts == (len(bpy.data.materials), len(bpy.data.images)))
    cmat = bpy.data.materials[created['candidate_material']]
    shader = next((n for n in cmat.node_tree.nodes if n.type == 'BSDF_PRINCIPLED'))
    old = shader.inputs['Roughness'].default_value
    cp = {'object_name': created['candidate_object'], 'material_name': created['candidate_material'], 'options': {}}
    prior = m9_status(cp)
    shader.inputs['Roughness'].default_value = 0.123
    bp = dict(params, **{k: created[k] for k in ['candidate_object', 'candidate_material']}, definition=DEFINITIONS[0], socket_names=SOCKETS, expected_candidate=prior)
    rejection('candidate_tamper', lambda: m9_build_shader(bp), 'MATERIAL_STALE_CANDIDATE')
    shader.inputs['Roughness'].default_value = old
    mat.keyframe_insert(data_path='diffuse_color', frame=1)
    params['expected_status'] = m9_status(params)
    rejection('animated_material_rejected', lambda: m9_make_candidate(dict(params, candidate_name='Animated', generation_id='e' * 32)), 'MATERIAL_ANIMATED_SHADER_UNSUPPORTED')
    mat.animation_data_clear()
    driver = mat.driver_add('diffuse_color', 0)
    rejection('material_driver_rejected', lambda: m9_status(params), 'MATERIAL_DRIVER_UNSUPPORTED')
    mat.driver_remove('diffuse_color', 0)
    mod = obj.modifiers.new('NativeCloth', 'CLOTH')
    rejection('native_cache_unsupported_honest', lambda: m9_status(params), 'MATERIAL_PHYSICS_DEPENDENCY_UNSUPPORTED')
    obj.modifiers.remove(mod)
    for index, definition in enumerate(DEFINITIONS):
        result = inspection.report('material_quality_report', qa_raw[str(index)], definition)
        probe.qa_reports[str(index)] = result
        if index < 6:
            check('technical_qa_' + str(index), result['passed'])
        else:
            codes = {item['code'] for item in result['blocking_unknown']}
            check('displacement_not_falsely_certified', not result['passed'] and not result['errors']
                  and codes == {'DISPLACEMENT_GEOMETRY_UNCERTIFIED'})


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    parser.add_argument('--case', required=True, choices=('preservation', 'features'))
    args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    probe = Probe()
    errors = []
    try:
        {'preservation':preservation, 'features':features}[args.case](probe)
    except Exception as error:
        errors.append({'error':str(error), 'traceback':traceback.format_exc()})
    import bpy
    files = [ROOT/'src/blender_mcp/production/materials'/name for name in
             ('candidate.py','graph.py','integrity.py','inspection.py','model.py','qa.py','textures.py','uv.py')]
    report = {'case':args.case, 'scope':'local safe-mode bpy; no MCP transport/render/production asset',
        'status':'PASS' if probe.checks and all(probe.checks.values()) and not errors else 'FAIL',
        'blender':bpy.app.version_string, 'checks':probe.checks, 'errors':errors,
        'max_payload_bytes':max(probe.payload_sizes, default=0), 'qa_raw':probe.qa_raw,
        'qa_reports':probe.qa_reports,
        'source_sha256':{str(path.relative_to(ROOT)):hashlib.sha256(path.read_bytes()).hexdigest() for path in files}}
    (output/'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print('M9_INTEGRITY', json.dumps({'case':args.case, 'status':report['status'],
          'checks':len(probe.checks), 'errors':errors}))
