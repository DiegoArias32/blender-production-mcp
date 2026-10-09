"""M5 generated character fixtures, deliberately closed and independent of production assets.

The inherited M4 creature Skin branch contains open/non-manifold junctions.  M5 does not repair that
unrelated historical fixture.  It starts from the closed humanoid connectivity and analytically
changes the right arm and back mass, with matching rest-bone endpoints and explicit region groups.
"""
import math
from pathlib import Path
import bpy
from mathutils import Vector
import m4_character_fixtures as m4

REGIONS = [
    {'name': 'FACE', 'definition': {'method': 'vertex_group', 'name': 'FACE'}, 'protected': True, 'priority': 8},
    {'name': 'HANDS', 'definition': {'method': 'vertex_group', 'name': 'HANDS'}, 'protected': True, 'priority': 6},
    {'name': 'SHOULDERS', 'definition': {'method': 'vertex_group', 'name': 'SHOULDERS'}, 'deformation': True, 'priority': 6},
    {'name': 'BACK_FLAT', 'definition': {'method': 'vertex_group', 'name': 'UPPER_BACK'}, 'priority': 1},
]
RETO_OPTIONS = {'max_distance_m': .005, 'max_normal_angle_degrees': 50,
                'maximum_silhouette_distance_m': .025, 'minimum_silhouette_iou': .97,
                'silhouette_resolution': 96, 'samples': 2048}


def creature_arm(point):
    p = point.copy()
    if p.x < -.20 and p.z > 1.15:
        t = max(0.0, min(1.0, (-p.x - .20) / .16))
        smooth = t * t * (3 - 2 * t)
        p.x = -.20 - (-p.x - .20) * (1 + .25 * smooth)
        p.y *= 1 + .9 * smooth
        p.z = 1.45 + (p.z - 1.45) * (1 + .9 * smooth)
    return p


def build_character(output, variant='humanoid', levels=3):
    if variant not in ('humanoid', 'creature'):
        raise ValueError('Controlled character fixture variant required')
    output = Path(output)
    info = m4.build(output / 'generated-base', 'humanoid', levels=levels)
    body, rig = bpy.data.objects[info['body']], bpy.data.objects[info['rig']]
    if variant == 'creature':
        for vertex in body.data.vertices:
            p = creature_arm(vertex.co)
            if p.y > .025 and -.02 < p.x < .22 and 1.18 < p.z < 1.55:
                dx, dz = (p.x - .10) / .14, (p.z - 1.38) / .18
                p.y += .075 * math.exp(-(dx * dx + dz * dz) * 2)
            vertex.co = p
        body.data.update()
        bpy.context.view_layer.objects.active = rig
        bpy.ops.object.mode_set(mode='EDIT')
        for bone in rig.data.edit_bones:
            bone.head = creature_arm(bone.head)
            bone.tail = creature_arm(bone.tail)
        bpy.ops.object.mode_set(mode='OBJECT')
        names = set(bone.name for bone in rig.data.bones)
        for group in list(body.vertex_groups):
            if group.name not in names: body.vertex_groups.remove(group)
        m4.region_groups(body, 'creature')
        body.name = 'M5CreatureHigh'; rig.name = 'M5CreatureRig'
    else:
        body.name = 'M5HumanoidHigh'; rig.name = 'M5HumanoidRig'
    regions = list(REGIONS)
    if variant == 'creature':
        regions += [
            {'name': 'INTENTIONAL_MASS', 'definition': {'method': 'vertex_group', 'name': 'MUTATED_ARM'}, 'protected': True, 'priority': 8},
            {'name': 'HUMP', 'definition': {'method': 'vertex_group', 'name': 'HUMP'}, 'protected': True, 'priority': 8},
        ]
    body['m5_fixture_variant'] = variant
    output.mkdir(parents=True, exist_ok=True)
    path = output / (variant + '.blend')
    bpy.ops.wm.save_as_mainfile(filepath=str(path))
    count = sum(p.loop_total - 2 for p in body.data.polygons)
    return {'path': str(path), 'body': body.name, 'rig': rig.name, 'variant': variant,
            'vertices': len(body.data.vertices), 'triangles': count, 'regions': regions,
            'options': dict(RETO_OPTIONS, regions=regions, target_triangles=int(count * .70)),
            'materials': [m.name for m in body.data.materials], 'uv_layers': [layer.name for layer in body.data.uv_layers]}
