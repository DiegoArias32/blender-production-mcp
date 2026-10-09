"""Blender templates for the topology-preserving shape-edit cycle (M4, phase 12).

The candidate mesh object is the sandbox; the source/master is never touched. An edit never changes
the candidate's mesh datablock in place: PREVIEW builds an edited copy on a separate preview object,
APPLY assigns an edited copy to the candidate and keeps the previous datablock (fake user), and REVERT
reassigns it. The displacement field is recomputed from the frozen proposal parameters and must match
the proposal's digest, so no per-vertex data crosses the socket.
"""
from .runtime import CHAR_RUNTIME

EDIT_RUNTIME = CHAR_RUNTIME + r'''
def ce_candidate(name):
    obj = cr_object(name)
    if bpy.context.mode != 'OBJECT':
        raise ValueError('MODE_NOT_SUPPORTED: shape edits require Object mode; the mode is never changed automatically')
    if obj.library or obj.data.library or obj.override_library:
        raise ValueError('LINKED_MESH_UNSUPPORTED: linked or overridden meshes cannot be edited here')
    raw = obj.get(MS_MARKER)
    marker = json.loads(raw) if isinstance(raw, str) else None
    if marker is None or not marker.get('source_fingerprint'):
        raise ValueError('CANDIDATE_REQUIRED: ' + obj.name + ' is not a candidate mesh; create one with production.character_create_candidate')
    return obj, marker

def ce_positions_digest(me):
    return m2_digest(ms_floats(me.vertices, 'co', 3))

def ce_analyze(params):
    obj, marker = ce_candidate(params['object_name'])
    field = cr_field(obj, params['operation'], params['editable'], params['locked'])
    safety = cr_shape_key_safety(obj.data, field['edited'], params['shape_keys'])
    signature = ms_signature(obj)
    return {'candidate': obj.name, 'mesh': obj.data.name, 'marker': marker,
            'signature_fingerprint': signature['fingerprint'], 'topology_fingerprint': signature['fingerprints']['topology'],
            'field_digest': field['digest'], 'stats': field['stats'], 'shape_keys': safety}
'''

READ = EDIT_RUNTIME + r'''
result = ce_analyze(params)
'''

PREFLIGHT = EDIT_RUNTIME + r'''
obj, marker = ce_candidate(params['object_name'])
result = {'matches': ms_signature(obj)['fingerprint'] == params['signature_fingerprint'], 'mesh': obj.data.name}
'''

EXECUTE = EDIT_RUNTIME + r'''
obj, marker = ce_candidate(params['object_name'])
if ms_signature(obj)['fingerprint'] != params['signature_fingerprint']:
    raise ValueError('STALE_PROPOSAL: the candidate mesh changed after the proposal; analyze again')
field = cr_field(obj, params['operation'], params['editable'], params['locked'])
if field['digest'] != params['field_digest']:
    raise ValueError('STALE_PROPOSAL: the recomputed displacement differs from the proposal; analyze again')
safety = cr_shape_key_safety(obj.data, field['edited'], params['shape_keys'])
if not safety['safe']:
    raise ValueError(safety['reasons'][0])
before = obj.data
base = before.name.split('__M4E_')[0]
edited = before.copy()
edited.name = base + '__M4E_' + params['suffix']
cr_apply_field(edited, field['field'])
verification = cr_verify(obj, before, edited, params['locked'], params['editable'], params['tolerance_m'])
preview_object = None
if params['mode'] == 'preview':
    preview = obj.copy()
    preview.data = edited
    preview.name = obj.name + '__M4P_' + params['suffix']
    for collection in obj.users_collection:
        collection.objects.link(preview)
    preview[MS_MARKER] = json.dumps({'preview_of': obj.name, 'source_object': marker.get('source_object'),
                                     'source_fingerprint': None, 'operation_id': params['operation_id']})
    preview.hide_render = True
    preview_object = preview.name
elif verification['passed']:
    before.use_fake_user = True
    obj.data = edited
else:
    edited.use_fake_user = True
passed = verification['passed']
metrics = dict(verification['metrics'])
for key, value in field['stats'].items():
    metrics['field_' + key] = value
result = {'candidate': obj.name, 'working_action': edited.name, 'previous_mesh': before.name, 'preview_object': preview_object,
          'working_fingerprint': ce_positions_digest(edited), 'active_action': obj.data.name,
          'verification': verification, 'shape_keys': safety,
          'metrics': metrics,
          'numeric_verification': 'pass' if passed else 'fail', 'visual_verification': 'required',
          'status': 'warning' if passed else 'error', 'errors': verification['errors'],
          'warnings': ['Numeric preservation checks passed; the shape change itself needs visual review.'] if passed else []}
'''

CHECK = EDIT_RUNTIME + r'''
obj, marker = ce_candidate(params['object_name'])
previous = bpy.data.meshes.get(params['previous_mesh'])
applied = bpy.data.meshes.get(params['working_action'])
if previous is None or applied is None:
    raise ValueError('CANDIDATE_MISSING: the previous or edited mesh datablock is missing; recover the checkpoint')
if ce_positions_digest(applied) != params['working_fingerprint']:
    raise ValueError('STALE_PROPOSAL: the edited mesh changed after the edit; automatic operation refused')
if params['mode'] == 'revert':
    if obj.data not in (previous, applied):
        raise ValueError('UNRELATED_MESH_ACTIVE: the candidate uses another mesh datablock; automatic revert refused')
    obj.data = previous
    applied.use_fake_user = True
    result = {'reverted': True, 'active_action': previous.name, 'candidate_preserved': applied.name, 'status': 'warning',
              'numeric_verification': params.get('numeric_verification'), 'visual_verification': 'required',
              'metrics': {}, 'warnings': [], 'errors': []}
else:
    if params.get('event_mode') == 'apply' and obj.data != applied:
        raise ValueError('STALE_PROPOSAL: the candidate no longer uses the applied mesh')
    verification = cr_verify(obj, previous, applied, params['locked'], params['editable'], params['tolerance_m'])
    result = {'verification': verification, 'metrics': verification['metrics'], 'active_action': obj.data.name,
              'numeric_verification': 'pass' if verification['passed'] else 'fail', 'visual_verification': 'required',
              'status': 'warning' if verification['passed'] else 'error', 'errors': verification['errors'], 'warnings': []}
'''

RECONCILE = EDIT_RUNTIME + r'''
obj = bpy.data.objects.get(params['object_name'])
prefix = params['candidate_name']
found = [m for m in bpy.data.meshes if prefix and (m.name == prefix or m.name.startswith(prefix + '.'))]
result = {'object_found': obj is not None, 'candidates': sorted(m.name for m in found),
          'active_action': obj.data.name if obj is not None and obj.type == 'MESH' else None,
          'working_fingerprint': ce_positions_digest(found[0]) if len(found) == 1 else None}
'''
