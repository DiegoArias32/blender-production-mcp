"""Small deterministic, wholly synthetic material fixtures for Blender 5.2+.

Run only in a dedicated factory-startup process. This module never loads assets.
The build function refuses existing output, and writes a byte-identical source
backup plus content hashes. No material fixture is an artistic quality claim.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil

VARIANTS = (
    'metallic','dielectric','fabric','skin_sss','ice_transmission','snow','wet_surface',
    'emission','opacity','normal_map','height_bump','missing_image',
    'roughness_wrong_colorspace','normal_broken_wiring','single_uv','multi_udim',
    'single_material_multi_udim','multiple_materials','invalid_graph','source_preservation',
    'candidate_material','unsupported_shader','missing_udim_tile',
)


def _digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _png(folder, name, semantic, size=32, tile=None):
    import bpy
    import math
    suffix = '.'+str(tile) if tile is not None else ''
    path=folder/(name+suffix+'.png')
    if path.exists():
        raise FileExistsError(path)
    image=bpy.data.images.new('M9_'+name+suffix,width=size,height=size,alpha=True)
    # Set metadata before touching pixels: changing generated image color space
    # after foreach_set discards its unsaved buffer in the installed Blender.
    image.colorspace_settings.name='sRGB' if semantic=='BASE_COLOR' else 'Non-Color'
    image.filepath_raw=str(path);image.file_format='PNG'
    data=[]
    for y in range(size):
        for x in range(size):
            wave=.5+.5*math.sin(x*.8)*math.cos(y*.65)
            if semantic=='BASE_COLOR':
                offset=((tile or 1001)-1001)*.002
                c=(.28+.25*x/size+offset,.1+.15*y/size,.06+.15*wave+offset,1.)
            elif semantic=='NORMAL':
                nx=.14*math.sin(x*.8);ny=.14*math.cos(y*.8)
                c=(.5+.5*nx,.5+.5*ny,.5+.5*math.sqrt(1-nx*nx-ny*ny),1.)
            elif semantic=='ROUGHNESS':c=(.25+.45*wave,)*3+(1.,)
            elif semantic=='METALLIC':c=(.85,)*3+(1.,)
            elif semantic=='AO':c=(.65+.35*wave,)*3+(1.,)
            elif semantic=='HEIGHT':c=(.45+.1*wave,)*3+(1.,)
            elif semantic=='ALPHA':c=(1.,1.,1.,.25+.7*(x/size))
            else:c=(wave,wave,wave,1.)
            data.extend(c)
    image.pixels.foreach_set(data)
    image.update();image.save()
    # Load from disk to test real texture dependencies rather than generated buffers.
    bpy.data.images.remove(image)
    image=bpy.data.images.load(str(path),check_existing=False)
    image.colorspace_settings.name='sRGB' if semantic=='BASE_COLOR' else 'Non-Color'
    # The synthetic channel library includes currently unassigned AO/metallic/
    # opacity maps. Keep those declared dependencies after save/reopen as well.
    image.use_fake_user=True
    loaded=list(image.pixels)
    minimum=[min(loaded[c::4]) for c in range(4)];maximum=[max(loaded[c::4]) for c in range(4)]
    dynamic=[maximum[c]-minimum[c] for c in range(4)]
    valid=len(loaded)==size*size*4 and max(maximum[:3])>0
    if semantic=='NORMAL':valid=valid and minimum[2]>.9 and .35<minimum[0]<.6 and dynamic[0]>.1 and dynamic[1]>.1
    if semantic=='ROUGHNESS':valid=valid and dynamic[0]>.3
    if semantic=='BASE_COLOR':valid=valid and dynamic[0]>.1 and maximum[0]!=maximum[1]
    if semantic=='HEIGHT':valid=valid and dynamic[0]>.08
    if semantic=='ALPHA':valid=valid and dynamic[3]>.5
    if not valid:raise AssertionError('M9_TEXTURE_PIXEL_VALIDATION_FAILED: '+semantic+' '+str((minimum,maximum)))
    return image,{'semantic':semantic,'image':image.name,'path':str(path),'colorspace':image.colorspace_settings.name,
                  'width':size,'height':size,'sha256':_digest(path),'normal_convention':'OPENGL' if semantic=='NORMAL' else None,
                  'pixel_validation':{'passed':True,'min_rgba':minimum,'max_rgba':maximum,'dynamic_range_rgba':dynamic}}


def _material(name):
    import bpy
    material=bpy.data.materials.new(name);material.use_nodes=True
    material['m9_fixture']=True
    return material,material.node_tree.nodes.get('Principled BSDF')


def _texture(material,image,name):
    node=material.node_tree.nodes.new('ShaderNodeTexImage');node.image=image
    node.label=name;node.name='M9_'+name
    return node


def _sphere(name,material,location):
    import bpy
    bpy.ops.mesh.primitive_uv_sphere_add(segments=24,ring_count=12,radius=1.,location=location)
    obj=bpy.context.object;obj.name=name;obj.data.name=name+'_Mesh'
    obj.data.materials.append(material)
    for face in obj.data.polygons:face.use_smooth=True
    obj['m9_fixture']=True
    return obj


def _udim_object(name,material,location,tiles):
    import bpy
    mesh=bpy.data.meshes.new(name+'_Mesh')
    verts=[];faces=[]
    for index,tile in enumerate(tiles):
        x=index*1.1
        verts.extend([(x,0,0),(x+1,0,0),(x+1,0,1),(x,0,1)])
        faces.append(tuple(range(index*4,index*4+4)))
    mesh.from_pydata(verts,[],faces);mesh.update()
    obj=bpy.data.objects.new(name,mesh);bpy.context.scene.collection.objects.link(obj);obj.location=location
    mesh.materials.append(material);uv=mesh.uv_layers.new(name='UVMap')
    for face,tile in zip(mesh.polygons,tiles):
        offset=tile-1001;u=offset%10;v=offset//10
        for loop,co in zip(face.loop_indices,((u+.05,v+.05),(u+.95,v+.05),(u+.95,v+.95),(u+.05,v+.95))):
            uv.data[loop].uv=co
    obj['m9_fixture']=True
    return obj


def _source_preservation(obj,folder):
    import bpy
    obj['m9_original_property']='must remain unchanged'
    obj.shape_key_add(name='Basis');key=obj.shape_key_add(name='M9_SourceShape')
    for point in key.data:point.co.z*=1.08
    key.value=.3
    key.keyframe_insert('value',frame=1);key.value=.55;key.keyframe_insert('value',frame=12)
    obj.location.x+=.2;obj.keyframe_insert('location',frame=1)
    obj.location.x+=.1;obj.keyframe_insert('location',frame=12)
    rigdata=bpy.data.armatures.new('M9_PreservedRigData');rig=bpy.data.objects.new('M9_PreservedRig',rigdata)
    bpy.context.scene.collection.objects.link(rig)
    bpy.ops.object.select_all(action='DESELECT');rig.select_set(True);bpy.context.view_layer.objects.active=rig
    bpy.ops.object.mode_set(mode='EDIT');bone=rigdata.edit_bones.new('ExplicitRoot');bone.head=(0,0,0);bone.tail=(0,0,1)
    bpy.ops.object.mode_set(mode='OBJECT')
    obj.vertex_groups.new(name='ExplicitRoot').add(list(range(len(obj.data.vertices))),1.,'REPLACE')
    mod=obj.modifiers.new('M9_PreservedBinding','ARMATURE');mod.object=rig
    cloth=obj.modifiers.new('M9_PreservedPhysics','CLOTH');cloth.show_viewport=False;cloth.show_render=False
    cloth.point_cache.frame_start=1;cloth.point_cache.frame_end=12
    cache=folder/'preserved-cache-sentinel.bin';cache.write_bytes(b'M9_SYNTHETIC_PHYSICS_CACHE_SENTINEL\x00\x01')
    obj['m9_cache_path']=str(cache)
    bpy.context.scene.frame_set(5,subframe=.25)
    obj.location.x+=.17;key.value=.63
    bpy.ops.object.select_all(action='DESELECT');obj.select_set(True);bpy.context.view_layer.objects.active=obj
    return {'rig':rig.name,'shape_key':key.name,'object_action':obj.animation_data.action.name,
            'shape_key_action':obj.data.shape_keys.animation_data.action.name,
            'physics_modifier':cloth.name,'cache_path':str(cache),'cache_sha256':_digest(cache),
            'cache_note':'synthetic sentinel; not a baked dynamics simulation'}


def build(output, variant='all', save=True):
    import bpy
    folder=Path(output).resolve()
    if folder.exists() and any(folder.iterdir()):
        raise FileExistsError('M9 fixture output must be new or empty: '+str(folder))
    if variant!='all' and variant not in VARIANTS:
        raise ValueError('Unknown M9 fixture variant: '+variant)
    if bpy.data.filepath:
        raise ValueError('Factory-startup unsaved scene required; existing .blend cannot be a fixture target')
    folder.mkdir(parents=True,exist_ok=True);textures=folder/'textures';textures.mkdir()
    for obj in list(bpy.context.scene.objects):bpy.data.objects.remove(obj,do_unlink=True)
    scene=bpy.context.scene;scene.name='M9_ControlledFixtures';scene.unit_settings.system='METRIC';scene.unit_settings.scale_length=1.
    scene.render.engine='BLENDER_EEVEE';scene.render.fps=30;scene.frame_start=1;scene.frame_end=12
    definitions={};images={}
    for semantic in ('BASE_COLOR','ROUGHNESS','METALLIC','NORMAL','AO','HEIGHT','ALPHA'):
        image,definition=_png(textures,'fixture_'+semantic.lower(),semantic)
        images[semantic]=image;definitions[semantic]=definition
    cases={}
    variants=VARIANTS if variant=='all' else (variant,)
    for index,case in enumerate(variants):
        material,bsdf=_material('M9_'+case+'_Material');material['m9_variant']=case
        bsdf.inputs['Base Color'].default_value=(.28,.12,.055,1.);bsdf.inputs['Roughness'].default_value=.42
        row={'object_name':'M9_'+case,'material_name':material.name,'texture_definitions':[],
             'expected_issues':[],'notes':[]}
        location=((index%6)*4.,(index//6)*4.,0)
        if case in ('multi_udim','single_material_multi_udim','missing_udim_tile'):
            tiles=(1001,1002,1011,1012,1021) if case=='single_material_multi_udim' else (1001,1002,1011)
            actual=tiles[:-1] if case=='missing_udim_tile' else tiles
            tiledefs=[]
            for tile in actual:
                image,definition=_png(textures,case,'BASE_COLOR',tile=tile);tiledefs.append(definition)
                bpy.data.images.remove(image)
            image=bpy.data.images.load(tiledefs[0]['path'],check_existing=False);image.source='TILED'
            image.filepath=str(textures/(case+'.<UDIM>.png'))
            for tile in actual:
                if tile!=1001:image.tiles.new(tile)
            image.reload();image.colorspace_settings.name='sRGB'
            node=_texture(material,image,'BASE_COLOR');material.node_tree.links.new(node.outputs['Color'],bsdf.inputs['Base Color'])
            obj=_udim_object(row['object_name'],material,location,tiles)
            row.update(uv_tiles=list(tiles),image_tiles=list(actual),texture_definitions=[{'semantic':'BASE_COLOR','image':image.name,
                'path':image.filepath,'colorspace':'sRGB','tiles':tiledefs}],single_material=True)
            if case=='missing_udim_tile':row['expected_issues'].append('MISSING_UDIM_TILE')
        else:
            obj=_sphere(row['object_name'],material,location)
        if case in ('metallic','candidate_material'):
            bsdf.inputs['Metallic'].default_value=.85
        elif case=='fabric':bsdf.inputs['Roughness'].default_value=.85
        elif case=='skin_sss':
            bsdf.inputs['Base Color'].default_value=(.42,.14,.09,1.)
            bsdf.inputs['Subsurface Weight'].default_value=.22;bsdf.inputs['Subsurface Scale'].default_value=.03
        elif case=='ice_transmission':
            bsdf.inputs['Base Color'].default_value=(.3,.52,.64,1.)
            bsdf.inputs['Transmission Weight'].default_value=.78;bsdf.inputs['IOR'].default_value=1.31
            bsdf.inputs['Roughness'].default_value=.16
            material.use_raytrace_refraction=True
        elif case=='snow':
            bsdf.inputs['Base Color'].default_value=(.78,.84,.9,1.)
            bsdf.inputs['Roughness'].default_value=.78;bsdf.inputs['Subsurface Weight'].default_value=.13
        elif case=='wet_surface':
            bsdf.inputs['Coat Weight'].default_value=.65;bsdf.inputs['Coat Roughness'].default_value=.08
        elif case=='emission':
            bsdf.inputs['Emission Color'].default_value=(.08,.3,.8,1.);bsdf.inputs['Emission Strength'].default_value=2.
        elif case=='opacity':
            bsdf.inputs['Alpha'].default_value=.45;material.surface_render_method='DITHERED'
        elif case=='missing_image':
            node=_texture(material,None,'BASE_COLOR');material.node_tree.links.new(node.outputs['Color'],bsdf.inputs['Base Color'])
            row['expected_issues'].append('MISSING_IMAGE')
        elif case=='roughness_wrong_colorspace':
            bad=images['ROUGHNESS'].copy();bad.name='M9_WrongRoughnessSpace';bad.colorspace_settings.name='sRGB'
            node=_texture(material,bad,'ROUGHNESS');material.node_tree.links.new(node.outputs['Color'],bsdf.inputs['Roughness'])
            row['texture_definitions']=[dict(definitions['ROUGHNESS'],image=bad.name,colorspace='sRGB')]
            row['expected_issues'].append('COLORSPACE_MISMATCH')
        elif case in ('normal_map','normal_broken_wiring'):
            node=_texture(material,images['NORMAL'],'NORMAL');row['texture_definitions']=[definitions['NORMAL']]
            if case=='normal_map':
                normal=material.node_tree.nodes.new('ShaderNodeNormalMap');normal.space='TANGENT';normal.uv_map=obj.data.uv_layers.active.name
                normal['normal_convention']='OPENGL';material.node_tree.links.new(node.outputs['Color'],normal.inputs['Color'])
                material.node_tree.links.new(normal.outputs['Normal'],bsdf.inputs['Normal'])
            else:
                material.node_tree.links.new(node.outputs['Color'],bsdf.inputs['Normal']);row['expected_issues'].append('NORMAL_WIRING_INVALID')
        elif case=='height_bump':
            node=_texture(material,images['HEIGHT'],'HEIGHT');bump=material.node_tree.nodes.new('ShaderNodeBump')
            bump.inputs['Distance'].default_value=.02;material.node_tree.links.new(node.outputs['Color'],bump.inputs['Height'])
            material.node_tree.links.new(bump.outputs['Normal'],bsdf.inputs['Normal']);row['texture_definitions']=[definitions['HEIGHT']]
        elif case=='multiple_materials':
            other,p=_material('M9_SecondaryAssignedMaterial');p.inputs['Base Color'].default_value=(.02,.18,.4,1.)
            obj.data.materials.append(other)
            for face in obj.data.polygons:face.material_index=face.index%2
            row['additional_materials']=[other.name]
        elif case=='invalid_graph':
            for link in list(material.node_tree.links):material.node_tree.links.remove(link)
            row['expected_issues'].append('OUTPUT_DISCONNECTED')
        elif case=='unsupported_shader':
            output=material.node_tree.nodes.get('Material Output')
            for link in list(material.node_tree.links):material.node_tree.links.remove(link)
            hair=material.node_tree.nodes.new('ShaderNodeBsdfHairPrincipled')
            material.node_tree.links.new(hair.outputs[0],output.inputs['Surface'])
            row['expected_issues'].append('UNSUPPORTED_SHADER');row['notes'].append('Hair BSDF is not a qualified M9 Principled surface builder')
        elif case=='source_preservation':row['preservation']=_source_preservation(obj,folder)
        if case in ('metallic','skin_sss','candidate_material'):
            for semantic,socket in (('BASE_COLOR','Base Color'),('ROUGHNESS','Roughness')):
                node=_texture(material,images[semantic],semantic);material.node_tree.links.new(node.outputs['Color'],bsdf.inputs[socket])
                row['texture_definitions'].append(definitions[semantic])
        if case=='candidate_material':material['m9_fixture_candidate']=True
        channels={}
        for definition in row['texture_definitions']:
            semantic=definition['semantic'].lower()
            if semantic=='alpha':semantic='opacity'
            channels[semantic]={'image':definition['image'],'component':'COLOR' if semantic in ('base_color','normal','emission') else 'R',
                                'colorspace':definition['colorspace']}
        row['material_definition']={'channels':channels,'normal_convention':'OPENGL' if 'normal' in channels else 'UNKNOWN',
                                    'height_mode':'BUMP' if 'height' in channels else 'NONE','renderer':'EEVEE'}
        cases[case]=row
    metadata={'schema_version':1,'fixture':'M9_MATERIALS_PBR','variant':variant,'cases':cases,
              'objects':[r['object_name'] for r in cases.values()],
              'materials':[r['material_name'] for r in cases.values()],
              'texture_definitions':definitions,'blender_version':bpy.app.version_string,
              'production_assets_used':False,'source_file':None,'source_backup':None}
    if save:
        source=folder/'m9-fixtures.blend'
        bpy.ops.wm.save_as_mainfile(filepath=str(source),check_existing=False)
        backup=folder/'m9-fixtures-source-backup.blend';shutil.copyfile(source,backup)
        metadata.update(source_file=str(source),source_backup=str(backup),source_sha256=_digest(source),backup_sha256=_digest(backup))
    metadata['files']=[{'path':str(p),'sha256':_digest(p),'bytes':p.stat().st_size} for p in textures.iterdir() if p.is_file()]
    with (folder/'fixtures.json').open('x',encoding='utf-8') as stream:json.dump(metadata,stream,indent=2)
    return metadata


if __name__=='__main__':
    import sys
    args=sys.argv[sys.argv.index('--')+1:]
    print('__M9_FIXTURE__='+json.dumps(build(args[0],args[1] if len(args)>1 else 'all'),separators=(',',':')))
