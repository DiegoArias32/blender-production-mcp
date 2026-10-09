"""Fresh owned Blender process used by host M8 probes. No listening port."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import traceback

import bpy

REPO = Path(__file__).resolve().parents[2]


def proxy_fixture(output,kind,fps,duration):
    if not bpy.app.background or bpy.data.filepath:
        raise RuntimeError('Proxy fixture requires unsaved headless factory-startup')
    output.mkdir(parents=True,exist_ok=False)
    for obj in tuple(bpy.context.scene.objects):
        bpy.data.objects.remove(obj,do_unlink=True)
    scene = bpy.context.scene
    scene.name = 'M8_ControlledProxy_'+kind
    scene.unit_settings.system,scene.unit_settings.scale_length = 'METRIC',1.0
    scene.frame_start,scene.frame_end = 1,1+round(fps*duration)
    scene.render.fps,scene.render.fps_base = fps,1.0
    operators = {}
    if kind == 'proxy_cube':
        operator = bpy.ops.mesh.primitive_cube_add
        selected = ('size',)
        supplied = {'size':2.0,'location':(0,0,1)}
    else:
        operator = bpy.ops.mesh.primitive_uv_sphere_add
        selected = ('segments','ring_count','radius')
        supplied = {'segments':16,'ring_count':8,'radius':1.0,'location':(0,0,1)}
    rna = operator.get_rna_type()
    for name in selected:
        prop = rna.properties.get(name)
        if prop is None:
            raise RuntimeError('INSTALLED_PRIMITIVE_PARAMETER_UNSUPPORTED: '+name)
        operators[name] = {'type':prop.type,'default':prop.default,'supplied':supplied[name]}
    operator(**supplied)
    collider = bpy.context.object
    collider.name = 'M8_StaticColliderSource'
    collider['fixture_only'] = True
    data = bpy.data.meshes.new('M8_SecondarySource_Data')
    data.from_pydata([(-.1,-.2,3),(.1,-.2,3),(0,.1,3)],[],[(0,1,2)])
    data.update()
    secondary = bpy.data.objects.new('M8_SecondarySource',data)
    scene.collection.objects.link(secondary)
    secondary['fixture_only'] = True
    secondary.vertex_groups.new(name='ExplicitPins').add([0],1.0,'REPLACE')
    target = output/'proxy-fixture.blend'
    bpy.ops.wm.save_as_mainfile(filepath=str(target))
    return {'schema':'m8.controlled_proxy_fixture.v1','fixture_only':True,'variant':kind,
            'source_path':str(target),'source_object':secondary.name,'colliders':[collider.name],
            'primary_objects':[secondary.name,collider.name],'pin_group':'ExplicitPins',
            'source_collider_vertices':len(collider.data.vertices),'operator_rna':operators,
            'blender':bpy.app.version_string,'frame_range':[scene.frame_start,scene.frame_end],
            'manifest':{'object':secondary.name,'classification':'CLOTH'}}


args = sys.argv[sys.argv.index('--')+1:]
output,fps,duration,variant = Path(args[0]),int(args[1]),float(args[2]),args[3]
if variant in ('proxy_cube','proxy_sphere'):
    fixture = proxy_fixture(output/'fixtures',variant,fps,duration)
else:
    spec = importlib.util.spec_from_file_location('m8_pipe_fixtures',REPO/'tests/blender/fixtures_m8.py')
    fixtures = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixtures)
    fixture = fixtures.build(output/'fixtures',variant,fps,duration)
print('__M8_PIPE_READY__='+json.dumps(fixture,allow_nan=False),flush=True)
for line in sys.stdin:
    request = json.loads(line)
    if request.get('quit'):
        break
    captured = io.StringIO()
    try:
        with contextlib.redirect_stdout(captured):
            exec(request['code'],{})
        answer = {'output':captured.getvalue()}
    except Exception as exc:
        answer = {'error':str(exc),'traceback':traceback.format_exc(),'output':captured.getvalue()}
    print('__M8_PIPE_REPLY__='+json.dumps(answer,allow_nan=False),flush=True)
