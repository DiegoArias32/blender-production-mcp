"""Evidence-driven M8 inspection, conservative classification and API support."""
from __future__ import annotations

import math

from .integrity import RUNTIME as INTEGRITY_RUNTIME


RUNTIME = INTEGRITY_RUNTIME + r'''
def px_inspect(params):
    source=px_object(params['source_object']);options=params.get('options',{})
    deps=px_dependencies([source]);prod_require_evaluable(deps);state=px_capture(deps)
    try:
        bpy.context.scene.frame_set(bpy.context.scene.frame_start)
        dimensions=[float(v) for v in source.dimensions]
        geometry={'vertices':0,'edges':0,'faces':0,'boundary_edges':0,'nonmanifold_edges':0,
            'components':0,'dimensions':dimensions,'thin_ratio':None,'length_ratio':None}
        groups=[];features=[]
        if source.type=='MESH':
            data=source.data
            if len(data.vertices)>100000 or len(data.polygons)>250000:
                px_fail('PHYSICS_CAPACITY','inspection geometry budget')
            incidence={tuple(sorted(e.vertices)):0 for e in data.edges}
            for polygon in data.polygons:
                vertices=list(polygon.vertices)
                for a,b in zip(vertices,vertices[1:]+vertices[:1]):
                    key=tuple(sorted((a,b)));incidence[key]=incidence.get(key,0)+1
            boundary=[edge for edge,count in incidence.items() if count==1]
            adjacency={v.index:[] for v in data.vertices}
            for edge in data.edges:
                a,b=edge.vertices;adjacency[a].append(b);adjacency[b].append(a)
            seen=set();components=0
            for vertex in adjacency:
                if vertex in seen:
                    continue
                components+=1;queue=[vertex]
                while queue:
                    current=queue.pop()
                    if current in seen:
                        continue
                    seen.add(current);queue.extend(adjacency[current])
            ordered=sorted(dimensions)
            geometry.update(vertices=len(data.vertices),edges=len(data.edges),faces=len(data.polygons),
                boundary_edges=len(boundary),nonmanifold_edges=sum(count!=2 for count in incidence.values()),
                loose_edges=sum(count==0 for count in incidence.values()),components=components,
                thin_ratio=ordered[0]/ordered[-1] if ordered[-1]>0 else None,
                length_ratio=ordered[-1]/ordered[-2] if ordered[-2]>0 else None)
            boundary_vertices={v for edge in boundary for v in edge}
            for group in source.vertex_groups:
                selected=[v.index for v in data.vertices if any(g.group==group.index and g.weight>0 for g in v.groups)]
                weights=[g.weight for v in data.vertices for g in v.groups if g.group==group.index and g.weight>0]
                groups.append({'name':group.name,'index':group.index,'weighted_vertices':len(selected),
                    'coverage':len(selected)/max(1,len(data.vertices)),
                    'boundary_vertices':sum(v in boundary_vertices for v in selected),
                    'min_weight':min(weights) if weights else None,'max_weight':max(weights) if weights else None})
            if geometry['faces'] and geometry['boundary_edges']:
                features.append('OPEN_SURFACE')
            if geometry['faces'] and geometry['boundary_edges']==0 and geometry['nonmanifold_edges']==0:
                features.append('CLOSED_VOLUME')
            if geometry['thin_ratio'] is not None and geometry['thin_ratio']<.08:
                features.append('THIN_GEOMETRY')
            if not geometry['faces'] and geometry['edges'] and geometry['components']==1:
                features.append('CONNECTED_EDGE_STRUCTURE')
            if any(0<g['coverage']<.4 and g['boundary_vertices']>0 for g in groups):
                features.append('PARTIAL_BOUNDARY_GROUP')
        modifiers=[{'name':m.name,'type':m.type,'viewport':m.show_viewport,
            'armature':m.object.name if m.type=='ARMATURE' and m.object else None} for m in source.modifiers]
        if any(m['armature'] for m in modifiers):
            features.append('ARMATURE_BOUND')
        chain=options.get('chain')
        if chain:
            rig=px_object(chain['armature']);names=chain.get('bones',[])
            if rig.type!='ARMATURE' or not names or any(not rig.pose.bones.get(n) for n in names):
                px_fail('PHYSICS_CHAIN_INVALID','inspection requires existing explicit bones')
            features.append('EXPLICIT_BONE_CHAIN')
        try:
            status=px_status(dict(params,chain=chain) if chain else params)
            source_supported=True;unsupported=[]
        except ValueError as error:
            status=None;source_supported=False;unsupported=[str(error)]
        return {'object':source.name,'object_type':source.type,'source_object':source.name,
            'geometry':geometry,'vertex_groups':groups,'features':features,
            'hierarchy':{'parent':source.parent.name if source.parent else None,'parent_type':source.parent_type,
                'parent_bone':source.parent_bone,'children':sorted(o.name for o in source.children)},
            'constraints':[{'name':c.name,'type':c.type} for c in source.constraints],
            'modifiers':modifiers,'rig_relationship':sorted({m['armature'] for m in modifiers if m['armature']}),
            'chain':chain,'source_supported':source_supported,'unsupported_reasons':unsupported,
            'source_status':status,
            'api':{'version':list(bpy.app.version),'cloth':bpy.types.ClothModifier.bl_rna.identifier=='ClothModifier',
                'self_collision':'use_self_collision' in bpy.types.ClothCollisionSettings.bl_rna.properties,
                'point_cache_bake':'is_baked' in bpy.types.PointCache.bl_rna.properties,
                'action_slots':bpy.types.ActionSlot.bl_rna.identifier=='ActionSlot',
                'native_hair':False},
            'collision_requirements':['explicit collider manifest','penetration QA','attachment QA'],
            'state_restored':True}
    finally:
        px_restore(deps,state)
'''

INSPECT = RUNTIME + "\nresult=px_inspect(params)\n"

CLASSIFICATIONS = frozenset({'RIGID', 'CLOTH', 'HAIR', 'SPRING_BONE', 'DANGLING_ACCESSORY',
    'SOFT_REGION', 'CHAIN_LIKE', 'CABLE_LIKE', 'STATIC', 'UNSUPPORTED', 'AMBIGUOUS'})


def inspect(bridge, object_name: str, options: dict | None = None) -> dict:
    options = dict(options or {})
    params = {'source_object': object_name, 'options': options}
    for key in ('primary_objects', 'colliders', 'anchor_object', 'chain'):
        if key in options:
            params[key] = options[key]
    return bridge.run(INSPECT, params)


def _entry(summary: dict, manifest: dict) -> dict:
    if 'objects' in manifest:
        entries = manifest['objects']
        if isinstance(entries, dict):
            return dict(entries.get(summary.get('object'), {}))
        if isinstance(entries, list):
            matches = [r for r in entries if isinstance(r, dict) and r.get('object') == summary.get('object')]
            if len(matches) > 1:
                raise ValueError('PHYSICS_MANIFEST_INVALID: duplicate object entries')
            return dict(matches[0]) if matches else {}
        raise ValueError('PHYSICS_MANIFEST_INVALID: objects must be a map or list')
    return dict(manifest)


def classify(summary: dict, manifest: dict | None = None) -> dict:
    """Compute confidence on the server; caller confidence can only lower it."""
    entry = _entry(summary, manifest or {})
    declared = entry.get('classification', entry.get('type', entry.get('candidate_type')))
    if declared is not None and declared not in CLASSIFICATIONS:
        raise ValueError('PHYSICS_CLASSIFICATION_INVALID: unknown classification')
    features = set(summary.get('features', []))
    evidence = []
    proposal = 'AMBIGUOUS'; confidence = .0; reasons = []
    supported = summary.get('source_supported', True)
    if not supported:
        proposal = 'UNSUPPORTED'; confidence = 1.
        reasons = list(summary.get('unsupported_reasons', ['Source dependencies need an adapter']))
    elif declared:
        proposal = declared; confidence = .95
        evidence.append({'kind': 'USER_MANIFEST', 'classification': declared})
    elif {'OPEN_SURFACE', 'THIN_GEOMETRY', 'PARTIAL_BOUNDARY_GROUP'} <= features:
        proposal = 'CLOTH'; confidence = .83
        evidence.extend({'kind': 'GEOMETRY_OR_ATTACHMENT', 'feature': f} for f in sorted(features))
    elif 'EXPLICIT_BONE_CHAIN' in features:
        proposal = 'SPRING_BONE'; confidence = .88
        evidence.append({'kind': 'EXPLICIT_CHAIN', 'chain': summary.get('chain')})
    elif 'CONNECTED_EDGE_STRUCTURE' in features:
        proposal = 'CHAIN_LIKE'; confidence = .62
        evidence.append({'kind': 'GEOMETRY', 'feature': 'CONNECTED_EDGE_STRUCTURE'})
    elif 'OPEN_SURFACE' in features and 'THIN_GEOMETRY' in features:
        proposal = 'CLOTH'; confidence = .65
        evidence.append({'kind': 'GEOMETRY', 'features': ['OPEN_SURFACE', 'THIN_GEOMETRY']})
    elif summary.get('hierarchy', {}).get('parent_bone') and 'CLOSED_VOLUME' in features:
        proposal = 'DANGLING_ACCESSORY'; confidence = .85
        evidence.append({'kind': 'ATTACHMENT', 'bone': summary['hierarchy']['parent_bone']})
    else:
        reasons.append('Geometry and relationships do not identify one supported secondary subsystem')
    name = str(summary.get('object', '')).lower()
    hints = [hint for hint in ('cloth', 'coat', 'hair', 'strap', 'chain', 'cable', 'pouch', 'soft') if hint in name]
    if hints:
        evidence.append({'kind': 'NAME_SECONDARY_ONLY', 'hints': hints})
    caller_confidence = entry.get('confidence')
    if caller_confidence is not None:
        if isinstance(caller_confidence, bool) or not isinstance(caller_confidence, (int, float)) or not math.isfinite(caller_confidence) or not 0 <= caller_confidence <= 1:
            raise ValueError('PHYSICS_CONFIDENCE_INVALID: finite confidence in [0,1] required')
        confidence = min(confidence, caller_confidence)
    if proposal == 'SOFT_REGION':
        mask = entry.get('authorized_mask', entry.get('region_mask'))
        valid_mask = isinstance(mask, str) and bool(mask.strip())
        valid_mask = valid_mask or (isinstance(mask, list) and bool(mask) and all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in mask))
        if entry.get('authorized') is not True or not valid_mask:
            reasons.append('SOFT_REGION requires an explicitly authorized nonempty region mask')
            confidence = min(confidence, .4)
    if proposal in ('HAIR', 'SPRING_BONE', 'CHAIN_LIKE', 'CABLE_LIKE') and not (entry.get('chain') or summary.get('chain')):
        reasons.append('The supported bone backend requires an explicit armature and ordered bone chain')
        confidence = min(confidence, .6)
    blocked = confidence < .8 or proposal in ('STATIC', 'RIGID', 'UNSUPPORTED', 'AMBIGUOUS')
    result_type = 'AMBIGUOUS' if confidence < .8 and proposal not in ('UNSUPPORTED', 'STATIC') else proposal
    return {'object': summary.get('object', summary.get('source_object')), 'classification': result_type,
        'candidate_type': result_type, 'proposed_classification': proposal, 'confidence': confidence,
        'automatic_setup_allowed': not blocked, 'evidence': evidence, 'reasons': reasons,
        'attachment': summary.get('hierarchy', {}), 'rig_relationship': summary.get('rig_relationship', []),
        'geometry': summary.get('geometry', {}), 'collision_requirements': summary.get('collision_requirements', []),
        'manifest': entry}


def compatibility(summary: dict, classification: dict | None = None, api: dict | None = None) -> dict:
    """Report inspected backends and boundaries; presence is distinct from readiness."""
    kind = (classification or {}).get('classification', 'AMBIGUOUS')
    source_ok = summary.get('source_supported', True)
    # Defaults describe the installed 5.2.1 RNA probe, not arbitrary Blender versions.
    probed = api if api is not None else summary.get('api', {'version': [5, 2, 1], 'cloth': True, 'self_collision': True,
        'point_cache_bake': True, 'action_slots': True, 'native_hair': False})
    def capability(supported, reason, limited=False):
        return {'status': 'LIMITED' if supported and limited else 'SUPPORTED' if supported else 'UNSUPPORTED_BY_CURRENT_BLENDER_API',
            'reason': reason}
    caps = {
        'cloth': capability(source_ok and bool(probed.get('cloth')), 'Native ClothModifier settings were inspected; mesh source, explicit pins and valid scale are required'),
        'self_collision': capability(source_ok and bool(probed.get('self_collision')), 'Native ClothCollisionSettings.use_self_collision; enabled only by an explicit cost/stability plan'),
        'hair_secondary': capability(source_ok and bool(probed.get('action_slots')), 'Explicit bone-chain backend preserves primary Actions/NLA; native curves/groom simulation has no qualified adapter', True),
        'spring_accessory': capability(source_ok and bool(probed.get('action_slots')), 'Separate bounded spring motion on owned candidates; explicit anchor/chain required'),
        'soft_region': capability(source_ok and bool(probed.get('action_slots')), 'Explicit authorized vertex mask and hard displacement limit; procedural secondary mesh backend', True),
        'cache_bake': capability(source_ok and bool(probed.get('point_cache_bake')), 'Native point-cache bake for cloth; sampled artifact cache for procedural backends'),
        'secondary_bake': capability(source_ok and bool(probed.get('action_slots')), 'Separate owned secondary Action or mesh cache; sampled simulation/bake equivalence required'),
        'native_hair': capability(False, 'No qualified native hair dynamics API adapter; existing groom is never replaced'),
    }
    return {'object': summary.get('object'), 'classification': kind, 'blender_api': probed,
        'capabilities': caps, 'automatic_setup_allowed': bool((classification or {}).get('automatic_setup_allowed')) and source_ok,
        'limitations': ['No primary Action rewriting', 'No arbitrary drivers, F-Curve modifiers or linked/override dependencies',
            'No native curves groom overhaul', 'Profiles are starting parameters, not universally calibrated physics']}
