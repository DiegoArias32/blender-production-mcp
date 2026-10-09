"""Preview failure recovery covers every object whose animation is evaluated.

Real rendered success and manual pose/KEY preservation live in the background
probe. This test exercises recovery after the first frame change, without bpy.
"""
import ast
from copy import deepcopy
from types import SimpleNamespace as NS

import pytest

from blender_mcp.production.physics import render


@pytest.mark.parametrize('order',[('A','B','C'),('C','A','B'),('B','C','A')])
def test_preview_failure_restores_owned_and_render_source_dependency_rna(order):
    generation='a'*32
    class Obj(dict):
        def __init__(self,name,owned=False):
            super().__init__(production_m8_owned=owned,production_generation_id=generation)
            self.name=name;self.type='ARMATURE';self.hide_render=True
            self.rna={'manual_pose':.63};self.dependency=None
        def evaluated_get(self,graph):return self
    source,primary,owned=[Obj(prefix+'_Scope',index==2) for index,prefix in enumerate(order)]
    extra=Obj('RenderSource');dependency=Obj('ExternalDependency')
    extra.dependency=dependency
    objects={o.name:o for o in (source,primary,owned,extra,dependency)}
    before={name:deepcopy(o.rna) for name,o in objects.items()}
    shading=NS(light='OLD',color_type='MATERIAL',background_type='WORLD',background_color=(0,0,0),
               show_shadows=False,show_cavity=False)
    scene=NS(frame_current=9,objects=[],camera=None,timeline_markers=[],
             render=NS(engine='ORIGINAL',film_transparent=True),display=NS(shading=shading))
    def frame_set(frame):
        scene.frame_current=frame
        for obj in objects.values():obj.rna['manual_pose']=frame/10
    scene.frame_set=frame_set
    context=NS(scene=scene,evaluated_depsgraph_get=lambda:None)
    captured=[]
    def dependencies(seeds):
        closure={o.name:o for o in seeds}
        for obj in seeds:
            if obj.dependency:closure[obj.dependency.name]=obj.dependency
        return [closure[name] for name in sorted(closure)]
    def capture(deps):
        captured.append([o.name for o in deps])
        return {'frame':scene.frame_current,'rna':{o.name:deepcopy(o.rna) for o in deps}}
    def restore(deps,state):
        scene.frame_set(state['frame'])
        for obj in deps:obj.rna=deepcopy(state['rna'][obj.name])
    def fail(code,detail):raise ValueError(code+': '+detail)
    namespace={'bpy':NS(context=context),'px_selection':lambda p:(source,[primary],[source,primary]),
        'px_object':lambda name:objects[name],'px_dependencies':dependencies,'px_capture':capture,'px_restore':restore,
        'prod_require_evaluable':lambda deps:None,'px_validate_objects':lambda deps:None,
        'px_fail':fail,'qa_state':lambda s:{'frame':s.frame_current},
        'qa_restore':lambda s,state,camera:s.frame_set(state['frame'])}
    function=next(node for node in ast.parse(render.RUNTIME).body if isinstance(node,ast.FunctionDef) and node.name=='pr_render')
    exec(compile(ast.Module(body=[function],type_ignores=[]),'<actual pr_render>','exec'),namespace)
    with pytest.raises(ValueError,match='PHYSICS_PREVIEW_GEOMETRY_REQUIRED'):
        namespace['pr_render']({'owned_objects':[owned.name],'render_candidate':owned.name,
            'source_render_objects':[extra.name],'generation_id':generation,'frames':[1,2,3]})
    assert captured[0]==sorted(objects)
    assert scene.frame_current==9
    assert {name:o.rna for name,o in objects.items()}==before
