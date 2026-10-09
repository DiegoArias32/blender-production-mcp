"""Character review renders with identical cameras (M4, phases 21 and 23).

One temporary orthographic camera, the same framing, resolution and neutral Workbench look for every
object and view: single grey colour, fixed studio light, no shadows, no cavity, no outline, so a
lighting or material difference can never read as a geometry change. Objects are rendered one at a
time (all other renderable objects are hidden temporarily). Every scene setting, visibility flag and
the camera are restored in `finally`. Workbench rasterizes on the GPU; the backend is reported.
"""
from ..qa.capture import _STATE

RENDER = _STATE + r'''
import math
from mathutils import Vector, Quaternion

CR_DIRECTIONS = {'FRONT': (0.0, -1.0, 0.0), 'BACK': (0.0, 1.0, 0.0), 'SIDE': (1.0, 0.0, 0.0), 'THREE_QUARTER': (1.0, -1.0, 0.0)}
RENDERABLE = ('MESH', 'CURVE', 'SURFACE', 'META', 'FONT', 'CURVES', 'POINTCLOUD', 'VOLUME', 'GREASEPENCIL')

def cr_safe(name):
    return ''.join(c if c.isalnum() or c in '-_' else '_' for c in name)

scene = bpy.context.scene
if bpy.context.mode != 'OBJECT':
    raise ValueError('MODE_NOT_SUPPORTED: character renders require Object mode')
targets = [bpy.data.objects.get(name) for name in params['objects']]
if any(t is None or t.type != 'MESH' for t in targets):
    raise ValueError('MESH_REQUIRED: every rendered object must be an existing mesh')
before = qa_state(scene)
camera = scene.camera
engine = scene.render.engine
shading = scene.display.shading
shade = {'light': shading.light, 'color_type': shading.color_type, 'single_color': list(shading.single_color),
         'background_type': shading.background_type, 'background_color': list(shading.background_color),
         'show_shadows': shading.show_shadows, 'show_cavity': shading.show_cavity,
         'show_object_outline': shading.show_object_outline}
visibility = {o.name: o.hide_render for o in scene.objects}
camera_data = None
camera_object = None
result = {'status': 'ok', 'files': [], 'cameras': {}, 'errors': [], 'before': before,
          'look': 'Workbench, single grey colour, studio light, no shadows/cavity/outline, orthographic',
          'warnings': ['Renders are geometry review images, not final shading.']}
try:
    scene.render.engine = 'BLENDER_WORKBENCH'
    shading.light = 'STUDIO'
    shading.color_type = 'SINGLE'
    shading.single_color = (0.8, 0.8, 0.8)
    shading.background_type = 'VIEWPORT'
    shading.background_color = (0.05, 0.05, 0.05)
    shading.show_shadows = False
    shading.show_cavity = False
    shading.show_object_outline = False
    result['render_backend'] = qa_render_backend(scene, False)
    qa_configure(scene, params['width'], params['height'])
    cameras = params.get('cameras')
    if not cameras:
        corners = []
        for target in targets:
            for corner in target.bound_box:
                corners.append(target.matrix_world @ Vector(corner))
        center = Vector((0.0, 0.0, 0.0))
        for corner in corners:
            center += corner
        center /= len(corners)
        radius = max((corner - center).length for corner in corners)
        if radius <= 1e-9:
            raise ValueError('Degenerate bounds; framing is undefined')
        cameras = {}
        for view in params['views']:
            direction = Vector(CR_DIRECTIONS[view]).normalized()
            rotation = (-direction).to_track_quat('-Z', 'Y')
            cameras[view] = {'location': list(center + direction * radius * 4.0), 'rotation_quaternion': list(rotation),
                             'ortho_scale': 2.0 * radius * params['margin'], 'clip_start': radius * 0.01,
                             'clip_end': radius * 20.0}
    camera_data = bpy.data.cameras.new('ProductionCharacterQA_' + params['operation_id'])
    camera_data.type = 'ORTHO'
    camera_object = bpy.data.objects.new('ProductionCharacterQA_' + params['operation_id'], camera_data)
    scene.collection.objects.link(camera_object)
    for target in targets:
        for item in scene.objects:
            if item.type in RENDERABLE:
                item.hide_render = item.name != target.name
        for view in params['views']:
            definition = cameras[view]
            camera_object.location = definition['location']
            camera_object.rotation_mode = 'QUATERNION'
            camera_object.rotation_quaternion = definition['rotation_quaternion']
            camera_data.ortho_scale = definition['ortho_scale']
            camera_data.clip_start = definition['clip_start']
            camera_data.clip_end = definition['clip_end']
            scene.camera = camera_object
            bpy.context.view_layer.update()
            path = params['output_dir'] + '/' + cr_safe(target.name) + '_' + view + '.png'
            scene.render.filepath = path
            outcome = bpy.ops.render.render(write_still=True)
            if 'FINISHED' not in outcome:
                raise RuntimeError('Render did not finish: ' + str(outcome))
            result['files'].append({'object': target.name, 'view': view, 'path': path})
    result['cameras'] = cameras
except Exception as exc:
    result['status'] = 'error'
    result['errors'].append({'type': str(type(exc)), 'message': str(exc)})
finally:
    for item in scene.objects:
        if item.name in visibility:
            item.hide_render = visibility[item.name]
    scene.render.engine = engine
    shading.light = shade['light']
    shading.color_type = shade['color_type']
    shading.single_color = shade['single_color']
    shading.background_type = shade['background_type']
    shading.background_color = shade['background_color']
    shading.show_shadows = shade['show_shadows']
    shading.show_cavity = shade['show_cavity']
    shading.show_object_outline = shade['show_object_outline']
    qa_restore(scene, before, camera)
    if camera_object is not None:
        bpy.data.objects.remove(camera_object, do_unlink=True)
    if camera_data is not None:
        bpy.data.cameras.remove(camera_data)
result['after'] = qa_state(scene)
result['state_restored'] = (result['before'] == result['after'] and scene.render.engine == engine
                            and all(o.hide_render == visibility[o.name] for o in scene.objects if o.name in visibility))
if not result['state_restored']:
    result['status'] = 'error'
    result['errors'].append({'type': 'StateRestorationError', 'message': 'Scene state differs after character render'})
'''
