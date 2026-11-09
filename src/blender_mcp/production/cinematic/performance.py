"""M10 multi-actor performance: staging, NLA body clips, accepted M7 faces, accepted M8 secondary and props.

Contract: docs/cinematic-architecture.md section 5; area docs: actor-staging.md, multi-character-synchronization.md,
cinematic-interactions.md. Rules every tool here keeps:
- Blender changes only through store.mutate; owned datablocks carry the M10 marks and every object-level change to a
  shared actor is returned as a `touched` row (value before) so rejection reverses it.
- Source Actions, the accepted M7 Action and M8 objects are never written: payloads compare digests before/after.
- Every measurement is an evaluated value in the candidate scene (cm_evaluated after frame_set inside
  temp_override): the original datablock can lag behind the evaluation (measured in the foundation phase).
- Each payload validates everything first and only then mutates; a failure while mutating reverses the rows it had
  already produced before re-raising, so a refused step never leaves unrecorded changes behind.
- Thresholds are labelled proposals (threshold_source PROPOSAL); no quality score is ever produced.
- Originals a step replaces (props, M8 secondary sources) are excluded when the candidate is created: source
  collections are linked whole into _SRC, so nothing here unlinks them; a step that still finds the original in the
  candidate scene refuses (CINEMATIC_PROP_NOT_EXCLUDED / CINEMATIC_SECONDARY_SOURCE_NOT_EXCLUDED).
- sync_report / facial_report / secondary_report are the quality-report providers: they re-measure the candidate as
  it is now, one bounded payload per actor or copy, and restore everything they sample.
"""
from __future__ import annotations

from . import lifecycle, model, registry, store
from .runtime import COMMON
from .service import handler

EXTRAPOLATIONS = ('NOTHING', 'HOLD', 'HOLD_FORWARD')
STAGE_ROW = frozenset({'actor_id', 'location', 'yaw_deg'})
CLIP_ROW = frozenset({'actor_id', 'action', 'slot', 'start_frame', 'action_frame_start', 'action_frame_end', 'scale',
                      'repeat', 'extrapolation', 'retime', 'carry_root'})
PROP_ROW = frozenset({'prop_id', 'object', 'placement', 'attach', 'visibility'})
ATTACH_ROW = frozenset({'actor_id', 'bone', 'frame_start', 'frame_end'})
PLACEMENT_ROW = frozenset({'location', 'rotation_deg'})
VISIBILITY_ROW = frozenset({'frame', 'visible'})
EVENT_ROW = frozenset({'frame', 'label'})
MAX_FACIAL_EVENTS = 64
MAX_CLIPS_PER_ACTOR = 32
MAX_PROPS = 16
MAX_ATTACH = 8
MAX_FRAME = 1_048_574
# Starting proposals for synthetic checks, not measured on production characters. Positions are float32 matrix
# products, so exact constructions land around 1e-7 m; the proposals leave two orders of magnitude of margin.
THRESHOLDS = {'position_m': 1e-5, 'rotation_deg': 1e-3, 'channel_value': 1e-5, 'secondary_equivalence_m': 1e-5,
              'carry_position_m': 1e-4, 'carry_yaw_deg': 1e-2,
              'prop_slide_m': 1e-5, 'prop_pop_m': 1e-5, 'root_jump_review_m': 0.05, 'root_jump_review_ratio': 4.0,
              'threshold_source': 'PROPOSAL'}
SYNTHETIC_LABEL = 'SYNTHETIC_ACCEPTANCE_FOR_MECHANISM_TEST'

PERF_RUNTIME = COMMON + r'''
import array
from mathutils import Matrix, Vector, Quaternion, Euler

def pf_rows(m):
    return [list(r) for r in m]

def pf_rot_error_deg(a, b):
    # 2*asin(|Ra-Rb|_F / (2*sqrt(2))) is exact for rotation matrices and, unlike acos of a quaternion dot product,
    # stays well conditioned near zero (float32 acos noise reads as ~0.02 degrees).
    ra = a.to_3x3().normalized()
    rb = b.to_3x3().normalized()
    total = 0.0
    for i in range(3):
        for j in range(3):
            total += (ra[i][j] - rb[i][j]) ** 2
    value = min(1.0, math.sqrt(total) / (2.0 * math.sqrt(2.0)))
    return math.degrees(2.0 * math.asin(value))

def pf_fcurves(action, slot):
    rows = []
    if action is None:
        return rows
    for layer in action.layers:
        for strip in layer.strips:
            if strip.type != 'KEYFRAME':
                continue
            for bag in strip.channelbags:
                if slot is None or bag.slot_handle == slot.handle:
                    for curve in bag.fcurves:
                        rows.append(curve)
    return rows

def pf_sources(idblock, include_muted):
    rows = []
    ad = idblock.animation_data
    if ad is None:
        return rows
    if ad.action is not None:
        rows.append([ad.action, ad.action_slot, None])
    for track in ad.nla_tracks:
        if track.mute and not include_muted:
            continue
        for strip in track.strips:
            if strip.action is not None:
                rows.append([strip.action, strip.action_slot, track.name])
    return rows

def pf_paths(sources):
    paths = set()
    for action, slot, track in sources:
        for curve in pf_fcurves(action, slot):
            paths.add(curve.data_path)
    return paths

def pf_channel(curve):
    return curve.data_path + '[' + str(curve.array_index) + ']'

def pf_channels(sources):
    keys = set()
    for action, slot, track in sources:
        for curve in pf_fcurves(action, slot):
            keys.add(pf_channel(curve))
    return keys

def pf_value(item, data_path, index):
    try:
        value = item.path_resolve(data_path)
    except (ValueError, AttributeError, TypeError, KeyError):
        return None
    if isinstance(value, (bool, int, float)):
        return float(value)
    try:
        return float(value[index])
    except (TypeError, IndexError, KeyError, ValueError):
        return None

def pf_custom(paths):
    # Object custom properties ('["x"]') and bone custom properties ('pose.bones["b"]["x"]').
    return sorted(p for p in paths if p.startswith('["') or (p.startswith('pose.bones["') and p.count('["') > 1))

def pf_digest(action):
    return m2_digest(m2_snapshot(action)) if action is not None else None

def pf_action_frame(f, start, afs, afe, scale, repeat):
    # Mapping measured on 5.2.1: inside a cycle a = afs + (f - start) / scale; the frame that ends a cycle starts
    # the next one, except the very end of a strip with a WHOLE repeat count, which evaluates the last action frame.
    # A fractional repeat ends inside a cycle (measured: repeat 2.75 of a 60-frame clip ends on action frame 46).
    length = (afe - afs) * scale
    local = f - start
    if length <= 0:
        return afs
    if local >= length * repeat - 1e-9 and abs(repeat - round(repeat)) < 1e-6:
        return afe
    cycle = local - math.floor(local / length + 1e-9) * length
    return afs + cycle / scale

def pf_int_frames(start, end, count):
    first = int(math.ceil(start - 1e-6))
    last = int(math.floor(end + 1e-6))
    if last < first:
        return []
    if last - first + 1 <= count:
        return list(range(first, last + 1))
    picks = set([first, last, first + 1, last - 1])
    for i in range(count):
        picks.add(first + int(round((last - first) * i / max(1, count - 1))))
    return sorted(p for p in picks if first <= p <= last)

def pf_capture_list(params, extra):
    # Evaluating the candidate scene flushes animated values into the shared originals (measured in M10 phase 0),
    # so every payload that samples time captures every actor of the plan, not only the ones it changes.
    rows = list(extra)
    for name in params.get('capture') or []:
        obj = bpy.data.objects.get(name)
        if obj is not None and obj not in rows:
            rows.append(obj)
    return rows

def pf_capture_all(params, cin, extra):
    # The same flush reaches owned objects in _CIN (keyed cameras, keyed prop constraint influence); their raw
    # channels feed the candidate fingerprint, so they are restored too.
    rows = pf_capture_list(params, extra)
    for obj in cin.all_objects:
        if obj not in rows:
            rows.append(obj)
    return rows

def pf_follows(child, parent):
    item = child.parent
    while item is not None:
        if item == parent:
            return True
        item = item.parent
    return False

def pf_bbox(scene, objects):
    low = [1e30, 1e30, 1e30]
    high = [-1e30, -1e30, -1e30]
    found = False
    for obj in objects:
        ev = cm_evaluated(scene, obj)
        for corner in ev.bound_box:
            p = ev.matrix_world @ Vector(corner)
            found = True
            for k in range(3):
                low[k] = min(low[k], p[k])
                high[k] = max(high[k], p[k])
    return [low, high] if found else None

def pf_box_gap(a, b):
    total = 0.0
    overlap = True
    for k in range(3):
        gap = max(b[0][k] - a[1][k], a[0][k] - b[1][k], 0.0)
        if gap > 0.0:
            overlap = False
        total += gap * gap
    return math.sqrt(total), overlap

def pf_mesh_local(obj, scene):
    ev = cm_evaluated(scene, obj)
    mesh = ev.to_mesh()
    try:
        values = array.array('f', [0.0]) * (len(mesh.vertices) * 3)
        mesh.vertices.foreach_get('co', values)
        return values, Matrix(ev.matrix_world)
    finally:
        ev.to_mesh_clear()

def pf_parent_frame(scene, obj):
    parent = obj.parent
    if parent is None:
        return Matrix.Identity(4)
    ev = cm_evaluated(scene, parent)
    if obj.parent_type == 'BONE' and obj.parent_bone and ev.pose is not None and ev.pose.bones.get(obj.parent_bone) is not None:
        return ev.matrix_world @ ev.pose.bones[obj.parent_bone].matrix
    return Matrix(ev.matrix_world)

def pf_mesh_parent(obj, scene):
    # M8 keeps a deforming secondary in the mesh (PC2) and a rigid accessory in an object Action on a bone-parented
    # object (measured, demo C pendant: 91 identical PC2 samples, the swing in <baked>_Motion_0). Positions in the
    # parent frame hold both, so equivalence and the playback proof read the secondary whatever carries it.
    values, matrix = pf_mesh_local(obj, scene)
    to_parent = pf_parent_frame(scene, obj).inverted() @ matrix
    out = array.array('f', [0.0]) * len(values)
    for i in range(0, len(values), 3):
        q = to_parent @ Vector((values[i], values[i + 1], values[i + 2]))
        out[i] = q.x
        out[i + 1] = q.y
        out[i + 2] = q.z
    return out, values, matrix

def pf_points(values, matrix, indices):
    rows = []
    for i in indices:
        p = matrix @ Vector((values[3 * i], values[3 * i + 1], values[3 * i + 2]))
        rows.append([p[0], p[1], p[2]])
    return rows

def pf_secondary_world(scene, rec):
    # World equivalence after the primary moved: the copy at f+delta must equal the M8 baked result at f carried by
    # the actor's present placement, A_now(f+delta) @ A_ref(f)^-1 @ ref(f) (A = actor world matrix).
    copy = bpy.data.objects.get(rec['copy'])
    actor = bpy.data.objects.get(rec['actor_object'])
    if copy is None or actor is None:
        return {'status': 'MISSING', 'copy': rec['copy']}
    worst = 0.0
    for row in rec['reference']:
        scene.frame_set(row['frame'] + rec['delta'])
        values, matrix = pf_mesh_local(copy, scene)
        now = pf_points(values, matrix, rec['indices'])
        a_now = Matrix(cm_evaluated(scene, actor).matrix_world)
        carry = a_now @ Matrix(row['actor']).inverted()
        for p, q in zip(now, row['points']):
            e = carry @ Vector(q)
            worst = max(worst, (Vector(p) - e).length)
    return {'status': 'MEASURED', 'copy': rec['copy'], 'frames': len(rec['reference']),
            'points_per_frame': len(rec['indices']), 'max_error_m': worst,
            'passed': worst <= rec['tolerance_m']}

# ------------------------------------------------------------------ staging

def pf_stage_check(row):
    obj = cm_object(row['object'], ['ARMATURE', 'MESH', 'EMPTY'])
    if obj.parent is not None or len(obj.constraints) > 0:
        cm_fail('ACTOR_STAGING_UNSUPPORTED', obj.name + ' has a parent or constraints; its delta transform would not place it predictably')
    if obj.rotation_mode == 'AXIS_ANGLE' and row['yaw_deg'] != 0:
        cm_fail('ACTOR_STAGING_UNSUPPORTED', obj.name + ' uses axis-angle rotation, which has no delta rotation channel')
    paths = pf_paths(pf_sources(obj, True))
    drivers = [d.data_path for d in obj.animation_data.drivers] if obj.animation_data else []
    delta = sorted(p for p in list(paths) + drivers if p.startswith('delta_'))
    if delta:
        cm_fail('ACTOR_DELTA_ANIMATED', obj.name + ' animates or drives ' + ', '.join(delta) + '; staging would fight it')
    if row['yaw_deg'] != 0 and 'location' in paths:
        cm_fail('ROOT_MOTION_AMBIGUOUS', obj.name + ' animates object location: a delta rotation turns the body but not its path (world = T(loc+dloc) @ R_delta @ R_anim)')
    for name in row.get('secondary_copies') or []:
        copy = bpy.data.objects.get(name)
        if copy is not None and not pf_follows(copy, obj):
            cm_fail('SECONDARY_STAGING_UNSUPPORTED', name + ' does not follow ' + obj.name + ' through its parents; restaging would detach the integrated secondary')
    return obj

def pf_sample_world(scene, objects, frames):
    rows = {}
    for f in frames:
        scene.frame_set(f)
        rows[f] = {o.name: Matrix(cm_evaluated(scene, o).matrix_world) for o in objects}
    return rows

def pf_stage(params):
    scene, src, cin = cm_candidate(params)
    checked = [[pf_stage_check(row), row] for row in params['actors']]
    if params.get('dry_run'):
        return {'dry_run': True, 'actors': [{'object': o.name, 'rotation_mode': o.rotation_mode,
                                              'delta_location': list(o.delta_location)} for o, r in checked]}
    objects = [o for o, r in checked]
    frames = params['frames']
    restore = pf_capture_all(params, cin, objects)
    captured = cm_capture(scene, restore)
    touched = []
    with bpy.context.temp_override(scene=scene):
        try:
            before = pf_sample_world(scene, objects, frames)
            for obj, row in checked:
                touched.append(cm_touch_delta(obj))
                obj.delta_location = Vector(obj.delta_location) + Vector(row['location'])
                if row['yaw_deg'] != 0:
                    turn = Matrix.Rotation(math.radians(row['yaw_deg']), 3, 'Z')
                    if obj.rotation_mode == 'QUATERNION':
                        obj.delta_rotation_quaternion = turn.to_quaternion() @ Quaternion(obj.delta_rotation_quaternion)
                    else:
                        old = Euler(obj.delta_rotation_euler, obj.rotation_mode)
                        obj.delta_rotation_euler = (turn @ old.to_matrix()).to_euler(obj.rotation_mode, old)
            after = pf_sample_world(scene, objects, frames)
            staged = []
            for obj, row in checked:
                turn = Matrix.Rotation(math.radians(row['yaw_deg']), 4, 'Z')
                worst_p = 0.0
                worst_r = 0.0
                for f in frames:
                    m0 = before[f][obj.name]
                    m1 = after[f][obj.name]
                    expected_t = m0.to_translation() + Vector(row['location'])
                    worst_p = max(worst_p, (m1.to_translation() - expected_t).length)
                    worst_r = max(worst_r, pf_rot_error_deg(m1, turn @ m0))
                staged.append({'actor_id': row['actor_id'], 'object': obj.name, 'delta_location': list(obj.delta_location),
                               'delta_rotation_euler': list(obj.delta_rotation_euler),
                               'delta_rotation_quaternion': list(obj.delta_rotation_quaternion),
                               'formula': 'world = T(loc + delta_location) @ (R_delta @ R_anim)',
                               'max_position_error_m': worst_p, 'max_rotation_error_deg': worst_r, 'frames': frames,
                               'world_start': [list(after[frames[0]][obj.name].to_translation())]})
            separation = pf_separation(scene, params['all_actors'], frames)
            secondary = [pf_secondary_world(scene, rec) for rec in params.get('secondary') or []]
        except Exception as exc:
            failed = cm_undo(touched)
            cm_restore(scene, restore, captured)
            cm_fail('CINEMATIC_STAGE_ABORTED', str(exc) + '; reversed ' + str(len(touched) - len(failed)) + ' rows, failed ' + str(failed))
        cm_restore(scene, restore, captured)
    return {'touched': touched, 'owned': {}, 'staged': staged, 'separation': separation, 'secondary': secondary,
            'fingerprints': cm_fingerprints(params)}

def pf_separation(scene, actors, frames):
    rows = []
    for actor in actors:
        # Only what the candidate scene evaluates: a registered mesh excluded at creation (an M8 secondary source)
        # would report the stale matrix of the source scene; its M10 copy stands in for it.
        objects = [cm_object(actor['object'])]
        for name in list(actor.get('meshes') or []) + list(actor.get('copies') or []):
            if scene.objects.get(name) is not None:
                objects.append(scene.objects[name])
        rows.append([actor['actor_id'], objects])
    boxes = {}
    for f in frames:
        scene.frame_set(f)
        for actor_id, objects in rows:
            boxes[(actor_id, f)] = pf_bbox(scene, objects)
    dims = {}
    for actor_id, objects in rows:
        box = boxes[(actor_id, frames[0])]
        dims[actor_id] = [box[1][k] - box[0][k] for k in range(3)] if box else None
    pairs = []
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            best = None
            for f in frames:
                a = boxes[(rows[i][0], f)]
                b = boxes[(rows[j][0], f)]
                if a is None or b is None:
                    continue
                gap, overlap = pf_box_gap(a, b)
                if best is None or gap < best['min_gap_m']:
                    best = {'actors': [rows[i][0], rows[j][0]], 'min_gap_m': gap, 'frame': f, 'bbox_overlap': overlap}
            if best is not None:
                pairs.append(best)
    return {'method': 'axis-aligned world boxes of evaluated actor objects and meshes at sampled frames',
            'dimensions_m': dims, 'pairs': pairs, 'frames': frames,
            'objects': {actor_id: [o.name for o in objects] for actor_id, objects in rows}}

# ------------------------------------------------------------------ body clips

def pf_pick_slot(action, obj, identifier):
    if identifier:
        for slot in action.slots:
            if slot.identifier == identifier:
                if slot.target_id_type not in ('OBJECT', 'UNSPECIFIED'):
                    cm_fail('SLOT_UNSUITABLE', identifier + ' targets ' + slot.target_id_type + ', not an object')
                return slot
        cm_fail('SLOT_MISSING', identifier + ' is not a slot of ' + action.name)
    objects = [s for s in action.slots if s.target_id_type == 'OBJECT']
    for slot in objects:
        if slot.identifier == 'OB' + obj.name:
            return slot
    if len(objects) == 1:
        return objects[0]
    if len(action.slots) == 1:
        return action.slots[0]
    cm_fail('SLOT_AMBIGUOUS', action.name + ' has ' + str(len(action.slots)) + ' slots and none is OB' + obj.name + '; pass slot explicitly')

def pf_clip_start(item):
    return item['start']

def pf_sync_check(row, params):
    obj = cm_object(row['object'], ['ARMATURE', 'MESH', 'EMPTY'])
    ad = obj.animation_data
    own = None
    own_carry = None
    if ad is not None:
        if ad.use_tweak_mode:
            cm_fail('NLA_TWEAK_MODE', obj.name + ' is in NLA tweak mode; leave tweak mode first (never changed automatically)')
        for track in ad.nla_tracks:
            if track.name == row['track']:
                own = track
                continue
            if track.name == row['carry_track']:
                own_carry = track
                continue
            for strip in track.strips:
                if strip.type != 'CLIP':
                    cm_fail('NLA_STRIP_UNSUPPORTED', obj.name + ' track ' + track.name + ' holds a ' + strip.type + ' strip')
    if own is not None and not row.get('replace'):
        cm_fail('CINEMATIC_NAME_COLLISION', 'NLA track ' + row['track'] + ' exists but is not recorded for this candidate')
    if own_carry is not None and not row.get('replace'):
        cm_fail('CINEMATIC_NAME_COLLISION', 'NLA track ' + row['carry_track'] + ' exists but is not recorded for this candidate')
    if row.get('root_bone') and (obj.type != 'ARMATURE' or obj.data.bones.get(row['root_bone']) is None):
        cm_fail('ROOT_BONE_MISSING', str(row['root_bone']) + ' is not a bone of ' + obj.name)
    drivers = set()
    if ad is not None:
        for d in ad.drivers:
            drivers.add(d.data_path + '[' + str(d.array_index) + ']')
    clips = []
    for index, clip in enumerate(row['clips']):
        action = bpy.data.actions.get(clip['action'])
        if action is None:
            cm_fail('CINEMATIC_ACTION_MISSING', clip['action'])
        slot = pf_pick_slot(action, obj, clip.get('slot'))
        afs = float(clip['action_frame_start']) if clip.get('action_frame_start') is not None else float(action.frame_range[0])
        afe = float(clip['action_frame_end']) if clip.get('action_frame_end') is not None else float(action.frame_range[1])
        if afe <= afs:
            cm_fail('CLIP_RANGE_INVALID', action.name + ' action range ' + str([afs, afe]))
        curves = pf_fcurves(action, slot)
        if not curves:
            cm_fail('CLIP_EMPTY', action.name + ' slot ' + slot.identifier + ' animates nothing')
        paths = set(c.data_path for c in curves)
        delta = sorted(p for p in paths if p.startswith('delta_'))
        if delta:
            cm_fail('ACTOR_DELTA_ANIMATED', action.name + ' animates ' + ', '.join(delta))
        if row.get('rotated') and 'location' in paths:
            cm_fail('ROOT_MOTION_AMBIGUOUS', action.name + ' animates object location on ' + obj.name + ', which is staged with a delta rotation')
        channels = set(pf_channel(c) for c in curves)
        clash = sorted(channels & drivers)
        if clash:
            cm_fail('NLA_DRIVER_CONFLICT', obj.name + ' drives ' + ', '.join(clash[:5]) + ' which ' + action.name + ' animates')
        length = (afe - afs) * clip['scale'] * clip['repeat']
        clips.append({'index': index, 'action': action, 'slot': slot, 'afs': afs, 'afe': afe, 'scale': clip['scale'],
                      'repeat': clip['repeat'], 'start': float(clip['start_frame']), 'end': float(clip['start_frame']) + length,
                      'extrapolation': clip['extrapolation'], 'curves': curves, 'channels': channels,
                      'custom': pf_custom(paths), 'carry_root': bool(clip.get('carry_root')), 'paths': paths})
    clips.sort(key=pf_clip_start)
    if clips and clips[0]['carry_root']:
        cm_fail('CARRY_ROOT_FIRST_CLIP', clips[0]['action'].name + ' is the first clip of ' + obj.name + '; there is no previous root to continue')
    if any(c['carry_root'] for c in clips):
        pf_carry_check(obj, row, clips, own_carry)
    for i in range(1, len(clips)):
        if clips[i]['start'] < clips[i - 1]['end'] - 1e-6:
            cm_fail('CLIP_OVERLAP', obj.name + ': ' + clips[i]['action'].name + ' starts at ' + str(clips[i]['start']) + ' before '
                    + clips[i - 1]['action'].name + ' ends at ' + str(clips[i - 1]['end']) + '; one actor plays one clip at a time')
    for rec in row.get('secondary') or []:
        ok = False
        for c in clips:
            first_cycle_end = c['start'] + (c['afe'] - c['afs'])
            if (c['action'].name == rec['primary_action'] and abs(c['scale'] - 1.0) < 1e-9
                    and abs((c['start'] - c['afs']) - rec['delta']) < 1e-6
                    and c['start'] <= rec['window'][0] + 1e-6 and rec['window'][1] <= first_cycle_end + 1e-6):
                ok = True
        if not ok:
            cm_fail('SECONDARY_OFFSET_MISMATCH', obj.name + ' carries an integrated M8 secondary at offset ' + str(rec['delta'])
                    + ': one clip of ' + str(rec['primary_action']) + ' must have scale 1, start - action_frame_start = '
                    + str(rec['delta']) + ' and cover ' + str(rec['window']) + ' in its first cycle')
    return {'object': obj, 'own': own, 'own_carry': own_carry, 'clips': clips, 'row': row}

def pf_carry_check(obj, row, clips, own_carry):
    # The carry correction is exact only for an unparented, unconstrained actor whose object transform the clips leave
    # alone (world = T(L + dloc) @ R_delta @ R @ S with L and R constant) and whose delta transform nobody else drives.
    if obj.parent is not None or len(obj.constraints) > 0:
        cm_fail('CARRY_ROOT_UNSUPPORTED', obj.name + ' has a parent or constraints; a delta correction would not place it predictably')
    if obj.rotation_mode == 'AXIS_ANGLE':
        cm_fail('CARRY_ROOT_UNSUPPORTED', obj.name + ' uses axis-angle rotation, which has no delta rotation channel')
    for c in clips:
        moved = sorted(c['paths'] & set(['location', 'rotation_euler', 'rotation_quaternion', 'rotation_axis_angle']))
        if moved:
            cm_fail('CARRY_ROOT_UNSUPPORTED', c['action'].name + ' animates the object ' + ', '.join(moved)
                    + ' of ' + obj.name + '; carry_root continues a root bone, not an animated object transform')
    sources = [s for s in pf_sources(obj, True) if s[2] != row['carry_track']]
    drivers = [d.data_path for d in obj.animation_data.drivers] if obj.animation_data else []
    delta = sorted(p for p in list(pf_paths(sources)) + drivers if p.startswith('delta_'))
    if delta:
        cm_fail('ACTOR_DELTA_ANIMATED', obj.name + ' animates or drives ' + ', '.join(delta) + '; carry_root would fight it')
    if row.get('secondary'):
        cm_fail('CARRY_ROOT_SECONDARY_UNSUPPORTED', obj.name + ' carries an integrated M8 secondary simulated against '
                'its uncorrected motion; carry_root would move the body away from that simulation')

def pf_root_matrix(scene, obj, bone):
    ev = cm_evaluated(scene, obj)
    if bone:
        return ev.matrix_world @ ev.pose.bones[bone].matrix
    return Matrix(ev.matrix_world)

def pf_yaw_between(a, b):
    # Yaw about +Z of the planar rotation that best takes b's horizontal axes onto a's (from a @ b^-1).
    r = a.to_3x3().normalized() @ b.to_3x3().normalized().transposed()
    return math.atan2(r[1][0] - r[0][1], r[0][0] + r[1][1])

def pf_carry_frames(prev, nxt):
    # Same frames as the boundary check: a frame shared by contiguous strips belongs to the earlier strip.
    f_end = int(math.floor(prev['end'] + 1e-6))
    f_next = int(math.ceil(nxt['start'] - 1e-6))
    if f_next <= f_end:
        f_next = f_end + 1
    return f_end, f_next

def pf_carry_restore(obj, track, params):
    # frame_set writes the carried value into delta_* (measured flush), so the staged base is read back from the
    # owned Action before its track is replaced.
    action = track.strips[0].action if len(track.strips) else None
    if action is not None and cm_is_owned(action, params):
        if action.get('m10_base_delta_location') is not None:
            obj.delta_location = list(action['m10_base_delta_location'])
            obj.delta_rotation_euler = list(action['m10_base_delta_rotation_euler'])
            obj.delta_rotation_quaternion = list(action['m10_base_delta_rotation_quaternion'])
    return action

def pf_carry_apply(scene, plan, params, touched, owned):
    # carry_root: a clip starts where the previous clip left the root. One world correction G = T(shift) @ Rz(yaw)
    # (horizontal shift, yaw about +Z) per carried boundary, accumulated. For an unparented actor world =
    # T(L + dloc) @ R_delta @ R @ S, so G @ world is again a delta transform: dloc' = t + Rz(L + dloc) - L and
    # R_delta' = Rz @ R_delta. It is keyed CONSTANT on an owned track above the body track; body clips never animate
    # delta_* (refused), so no channel is shared with the body strips and nothing falls back to a default between them.
    # A clip without carry_root keeps the correction accumulated so far.
    obj = plan['object']
    row = plan['row']
    clips = plan['clips']
    carried = [i for i in range(len(clips)) if clips[i]['carry_root']]
    if not carried:
        return None
    bone = row.get('root_bone')
    if bone and (obj.type != 'ARMATURE' or obj.pose is None or obj.pose.bones.get(bone) is None):
        cm_fail('ROOT_BONE_MISSING', str(bone) + ' is not a posed bone of ' + obj.name)
    quaternion = obj.rotation_mode == 'QUATERNION'
    base_loc = Vector(obj.delta_location)
    base_euler = Euler(obj.delta_rotation_euler, 'XYZ' if quaternion else obj.rotation_mode)
    base_rot = Quaternion(obj.delta_rotation_quaternion) if quaternion else base_euler.to_quaternion()
    location = Vector(obj.location)
    raw = []
    for i in carried:
        f_end, f_next = pf_carry_frames(clips[i - 1], clips[i])
        scene.frame_set(f_end)
        a = pf_root_matrix(scene, obj, bone)
        scene.frame_set(f_next)
        b = pf_root_matrix(scene, obj, bone)
        raw.append([i, f_end, f_next, a, b])
    acc = Matrix.Identity(4)
    keys = []
    rows = []
    for i, f_end, f_next, a, b in raw:
        a_now = acc @ a
        b_now = acc @ b
        yaw = pf_yaw_between(a_now, b_now)
        turn = Matrix.Rotation(yaw, 4, 'Z')
        shift = a_now.translation - (turn @ b_now).translation
        shift.z = 0.0
        acc = Matrix.Translation(shift) @ turn @ acc
        rot = acc.to_quaternion()
        dloc = acc.translation + rot @ (location + base_loc) - location
        keys.append([f_next, dloc, rot @ base_rot])
        rows.append({'clip': clips[i]['action'].name, 'frames': [f_end, f_next], 'yaw_deg': math.degrees(yaw),
                     'shift_m': [shift.x, shift.y], 'delta_location': list(dloc)})
    if plan['own_carry'] is None:
        touched.append(cm_touch_delta(obj))
        touched.append(cm_touch_track(obj, 'OBJECT', row['carry_track']))
    action = cm_new_action(params, row['carry_track'], 'root_carry')
    owned['actions'].append(action.name)
    action['m10_base_delta_location'] = list(obj.delta_location)
    action['m10_base_delta_rotation_euler'] = list(obj.delta_rotation_euler)
    action['m10_base_delta_rotation_quaternion'] = list(obj.delta_rotation_quaternion)
    slot = action.slots.new(id_type='OBJECT', name=obj.name)
    bag = action.layers.new('M10_ROOT').strips.new(type='KEYFRAME').channelbag(slot, ensure=True)
    first = int(params['timeline'][0])
    points = [[first, base_loc, base_rot]] + keys
    for index in range(3):
        pf_insert(bag, 'delta_location', index, [[q[0], q[1][index]] for q in points])
    if quaternion:
        for index in range(4):
            pf_insert(bag, 'delta_rotation_quaternion', index, [[q[0], q[2][index]] for q in points])
    else:
        eulers = [base_euler]
        for q in points[1:]:
            eulers.append(q[2].to_euler(obj.rotation_mode, eulers[-1]))
        for index in range(3):
            pf_insert(bag, 'delta_rotation_euler', index, [[points[k][0], eulers[k][index]] for k in range(len(points))])
    track = cm_new_track(obj, 'OBJECT', row['carry_track'], params)
    strip = track.strips.new('root_carry', first, action)
    if strip.action_slot != slot:
        cm_ensure_pose(obj)
        strip.action_slot = slot
    pf_set_range(strip, float(first), float(max(int(params['timeline'][1]), keys[-1][0]) + 1))
    strip.frame_start_ui = first
    strip.blend_type = 'REPLACE'
    strip.extrapolation = 'HOLD'
    worst_p = 0.0
    worst_r = 0.0
    for k in range(len(raw)):
        scene.frame_set(raw[k][1])
        a = pf_root_matrix(scene, obj, bone)
        scene.frame_set(raw[k][2])
        b = pf_root_matrix(scene, obj, bone)
        dp = Vector((a.translation.x - b.translation.x, a.translation.y - b.translation.y)).length
        dr = abs(math.degrees(pf_yaw_between(a, b)))
        rows[k]['position_error_m'] = dp
        rows[k]['yaw_error_deg'] = dr
        worst_p = max(worst_p, dp)
        worst_r = max(worst_r, dr)
    return {'track': track.name, 'action': action.name, 'root': bone or 'object', 'rotation_mode': obj.rotation_mode,
            'boundaries': rows, 'max_position_error_m': worst_p, 'max_yaw_error_deg': worst_r}

def pf_set_range(strip, afs, afe):
    if afs >= strip.action_frame_end:
        strip.action_frame_end = afe
        strip.action_frame_start = afs
    else:
        strip.action_frame_start = afs
        strip.action_frame_end = afe

def pf_sync_apply(plan, params, touched):
    obj = plan['object']
    row = plan['row']
    if plan['own_carry'] is not None:
        old = pf_carry_restore(obj, plan['own_carry'], params)
        obj.animation_data.nla_tracks.remove(plan['own_carry'])
        if old is not None and cm_is_owned(old, params):
            bpy.data.actions.remove(old)
    if plan['own'] is not None:
        obj.animation_data.nla_tracks.remove(plan['own'])
    else:
        touched.append(cm_touch_animdata(obj, 'OBJECT'))
        if obj.animation_data is not None and obj.animation_data.action is not None:
            obj.animation_data.action = None
        touched.append(cm_touch_track(obj, 'OBJECT', row['track']))
    track = cm_new_track(obj, 'OBJECT', row['track'], params)
    for clip in plan['clips']:
        name = ('clip' + str(clip['index']) + '_' + clip['action'].name)[:63]
        strip = track.strips.new(name, int(clip['start']), clip['action'])
        if strip.action_slot != clip['slot']:
            cm_ensure_pose(obj)
            strip.action_slot = clip['slot']
        pf_set_range(strip, clip['afs'], clip['afe'])
        strip.scale = clip['scale']
        strip.repeat = clip['repeat']
        strip.blend_type = 'REPLACE'
        strip.extrapolation = clip['extrapolation']
        strip.frame_start_ui = clip['start']
        suitable = [s.identifier for s in strip.action_suitable_slots]
        if strip.action_slot is None or strip.action_slot.identifier not in suitable:
            cm_fail('SLOT_UNSUITABLE', clip['slot'].identifier + ' is not suitable for ' + obj.name)
        if abs(strip.frame_start - clip['start']) > 1e-4 or abs(strip.frame_end - clip['end']) > 1e-4 \
                or abs(strip.action_frame_start - clip['afs']) > 1e-4 or abs(strip.action_frame_end - clip['afe']) > 1e-4:
            cm_fail('CLIP_PLACEMENT_MISMATCH', name + ' landed at ' + str([strip.frame_start, strip.frame_end]))
        clip['strip'] = strip
    return track

def pf_root_point(scene, obj, bone):
    ev = cm_evaluated(scene, obj)
    if bone:
        return ev.matrix_world @ ev.pose.bones[bone].head
    return ev.matrix_world.to_translation()

def pf_gap_runs(spans, start, end):
    # spans: [[clip_start, clip_end, extrapolation, action]] sorted by start. NLA rules measured in Blender 5.2.1
    # (analysis area): NOTHING leaves the frames undriven, HOLD and HOLD_FORWARD hold forward, and only the first
    # strip of a track holds backwards (HOLD). Undriven NLA channels fall back to their default value.
    runs = []
    if not spans:
        return runs
    first = spans[0]
    if first[2] != 'HOLD':
        hi = min(end, int(math.ceil(first[0] - 1e-6)) - 1)
        if hi >= start:
            runs.append({'kind': 'BEFORE_FIRST', 'frames': [start, hi], 'after': None, 'before': first[3],
                         'extrapolation': first[2]})
    for i in range(len(spans)):
        if spans[i][2] != 'NOTHING':
            continue
        last = i + 1 >= len(spans)
        lo = max(start, int(math.floor(spans[i][1] + 1e-6)) + 1)
        hi = end if last else min(end, int(math.ceil(spans[i + 1][0] - 1e-6)) - 1)
        if hi >= lo:
            runs.append({'kind': 'AFTER_LAST' if last else 'BETWEEN', 'frames': [lo, hi], 'after': spans[i][3],
                         'before': None if last else spans[i + 1][3], 'extrapolation': spans[i][2]})
    return runs

def pf_root_bones(obj, bone):
    # A declared root (options or actor registry) wins; otherwise every parentless bone read from the armature
    # data, never guessed from names.
    if bone:
        return [bone], 'DECLARED'
    if obj.type == 'ARMATURE':
        return sorted(b.name for b in obj.data.bones if b.parent is None), 'PARENTLESS_BONES'
    return [], 'OBJECT_ONLY'

def pf_transform_keys(curves, roots):
    kinds = ('location', 'rotation_euler', 'rotation_quaternion', 'rotation_axis_angle', 'scale')
    keys = set()
    for curve in curves:
        path = curve.data_path
        hit = path in kinds
        for root in roots:
            prefix = 'pose.bones["' + root + '"].'
            if path.startswith(prefix) and path[len(prefix):] in kinds:
                hit = True
        if hit:
            keys.add(pf_channel(curve))
    return keys

def pf_gap_hazards(scene, plan, params):
    # Root reset in an NLA gap (measured by the analysis area: the actor jumped 2.0 m to the origin): every frame of
    # the timeline that no clip drives, where the clips animate object or root-bone transforms, is reported with the
    # evaluated root jump between the last driven frame and the first undriven one.
    obj = plan['object']
    roots, root_source = pf_root_bones(obj, plan['row'].get('root_bone'))
    keys = set()
    for clip in plan['clips']:
        keys |= pf_transform_keys(clip['curves'], roots)
    other = set()
    ad = obj.animation_data
    if ad is not None:
        for track in ad.nla_tracks:
            if track.name != plan['row']['track'] and not track.mute:
                for strip in track.strips:
                    other |= pf_channels([[strip.action, strip.action_slot, track.name]]) & keys
        if ad.action is not None:
            other |= pf_channels([[ad.action, ad.action_slot, None]]) & keys
    out = {'roots': roots, 'root_source': root_source, 'transform_channels': len(keys),
           'also_animated_by_other_tracks': len(other), 'hazards': []}
    if not keys:
        return out
    exposed = sorted(keys)
    bone = roots[0] if roots and any(k.startswith('pose.bones') for k in exposed) else None
    spans = [[c['start'], c['end'], c['extrapolation'], c['action'].name] for c in plan['clips']]
    for run in pf_gap_runs(spans, params['timeline'][0], params['timeline'][1]):
        lo, hi = run['frames']
        if run['kind'] == 'BEFORE_FIRST':
            driven, rest = hi + 1, hi
        else:
            driven, rest = lo - 1, lo
        scene.frame_set(driven)
        p0 = Vector(pf_root_point(scene, obj, bone))
        scene.frame_set(rest)
        p1 = Vector(pf_root_point(scene, obj, bone))
        run['channels'] = len(exposed)
        run['sample'] = exposed[:5]
        run['measured_root'] = bone or 'object'
        run['frames_compared'] = [driven, rest]
        run['jump_m'] = (p1 - p0).length
        out['hazards'].append(run)
    return out

def pf_sync_verify(scene, plan, params):
    obj = plan['object']
    clips = []
    previous_end = None
    for clip in plan['clips']:
        frames = pf_int_frames(clip['start'], clip['end'], 8)
        if previous_end is not None and abs(clip['start'] - previous_end) < 1e-6:
            # A frame shared by two contiguous strips is evaluated by the earlier strip (measured 5.2.1: strips
            # [1, 11] and [11, 21] give the first strip's last frame at 11), so it is not this clip's sample.
            frames = [f for f in frames if abs(f - clip['start']) > 1e-6]
        previous_end = clip['end']
        curves = clip['curves'][:64]
        worst = 0.0
        compared = 0
        unresolved = set()
        mapping = []
        for f in frames:
            a = pf_action_frame(f, clip['start'], clip['afs'], clip['afe'], clip['scale'], clip['repeat'])
            mapping.append([f, a])
            scene.frame_set(f)
            ev = cm_evaluated(scene, obj)
            for curve in curves:
                value = pf_value(ev, curve.data_path, curve.array_index)
                if value is None:
                    unresolved.add(pf_channel(curve))
                    continue
                worst = max(worst, abs(value - curve.evaluate(a)))
                compared += 1
        strip = clip['strip']
        clips.append({'action': clip['action'].name, 'slot': clip['slot'].identifier, 'frame_start': strip.frame_start,
                      'frame_end': strip.frame_end, 'action_frame_start': strip.action_frame_start,
                      'action_frame_end': strip.action_frame_end, 'scale': strip.scale, 'repeat': strip.repeat,
                      'extrapolation': strip.extrapolation, 'blend_type': strip.blend_type,
                      'duration_frames': strip.frame_end - strip.frame_start, 'offset_frames': strip.frame_start - strip.action_frame_start,
                      'carry_root': bool(clip.get('carry_root')),
                      'mapping_samples': mapping, 'channels': len(clip['channels']), 'channels_compared': len(curves),
                      'value_comparisons': compared, 'max_channel_error': worst, 'unresolved_channels': sorted(unresolved)[:10],
                      'animated_custom_properties': clip['custom'][:10],
                      'mapping_passed': compared > 0 and worst <= params['tolerance']})
    gaps = []
    boundaries = []
    bone = plan['row'].get('root_bone')
    for i in range(1, len(plan['clips'])):
        prev = plan['clips'][i - 1]
        nxt = plan['clips'][i]
        if nxt['start'] - prev['end'] > 1e-6:
            gaps.append({'after': prev['action'].name, 'before': nxt['action'].name, 'frames': [prev['end'], nxt['start']],
                         'length_frames': nxt['start'] - prev['end'], 'fills_with': prev['extrapolation'] + ' / ' + nxt['extrapolation']})
        f_end = int(math.floor(prev['end'] + 1e-6))
        f_next = int(math.ceil(nxt['start'] - 1e-6))
        if f_next <= f_end:
            f_next = f_end + 1
        points = {}
        for f in (f_end - 1, f_end, f_next, f_next + 1):
            scene.frame_set(f)
            points[f] = Vector(pf_root_point(scene, obj, bone))
        jump = (points[f_next] - points[f_end]).length
        local = max((points[f_end] - points[f_end - 1]).length, (points[f_next + 1] - points[f_next]).length)
        review = jump > max(params['root_jump_m'], params['root_jump_ratio'] * local)
        boundaries.append({'between': [prev['action'].name, nxt['action'].name], 'frames': [f_end, f_next],
                           'root': bone or 'object', 'jump_m': jump, 'neighbour_step_m': local,
                           'finding': 'ROOT_DISCONTINUITY_REVIEW' if review else None})
    own = set()
    for clip in plan['clips']:
        own |= clip['channels']
    overlaps = []
    ad = obj.animation_data
    names = [t.name for t in ad.nla_tracks]
    for track in ad.nla_tracks:
        if track.name == plan['row']['track'] or track.mute:
            continue
        shared = set()
        for strip in track.strips:
            shared |= pf_channels([[strip.action, strip.action_slot, track.name]]) & own
        if shared:
            upper = names.index(track.name) > names.index(plan['row']['track'])
            overlaps.append({'track': track.name, 'channels': len(shared), 'sample': sorted(shared)[:5],
                             'wins': track.name if upper else plan['row']['track']})
    return {'object': obj.name, 'track': plan['row']['track'], 'clips': clips, 'gaps': gaps, 'boundaries': boundaries,
            'track_overlaps': overlaps, 'root_reset_gaps': pf_gap_hazards(scene, plan, params)}

def pf_sync(params):
    scene, src, cin = cm_candidate(params)
    plans = [pf_sync_check(row, params) for row in params['actors']]
    if params.get('dry_run'):
        return {'dry_run': True, 'actors': [{'object': p['object'].name, 'replace': p['own'] is not None,
                                              'clips': [{'action': c['action'].name, 'slot': c['slot'].identifier,
                                                         'start': c['start'], 'end': c['end']} for c in p['clips']]}
                                             for p in plans]}
    actions = {}
    for p in plans:
        for c in p['clips']:
            actions[c['action'].name] = c['action']
    digests = {name: pf_digest(a) for name, a in actions.items()}
    objects = [p['object'] for p in plans]
    for rec in params.get('secondary') or []:
        copy = bpy.data.objects.get(rec['copy'])
        if copy is not None and copy not in objects:
            objects.append(copy)
    objects = pf_capture_all(params, cin, objects)
    captured = cm_capture(scene, objects)
    touched = []
    owned = {'actions': []}
    carries = []
    try:
        for p in plans:
            pf_sync_apply(p, params, touched)
        with bpy.context.temp_override(scene=scene):
            for p in plans:
                carries.append(pf_carry_apply(scene, p, params, touched, owned))
    except Exception as exc:
        for p in plans:
            ad = p['object'].animation_data
            track = ad.nla_tracks.get(p['row']['track']) if ad else None
            if track is not None and p['own'] is None:
                ad.nla_tracks.remove(track)
        failed = cm_undo(touched)
        for name in owned['actions']:
            action = bpy.data.actions.get(name)
            if action is not None and cm_is_owned(action, params):
                bpy.data.actions.remove(action)
        cm_restore(scene, objects, captured)
        cm_fail('CINEMATIC_SYNC_ABORTED', str(exc) + '; reversed, failed rows ' + str(failed))
    with bpy.context.temp_override(scene=scene):
        try:
            verified = [pf_sync_verify(scene, p, params) for p in plans]
            for i in range(len(plans)):
                verified[i]['carry'] = carries[i]
            secondary = [pf_secondary_world(scene, rec) for rec in params.get('secondary') or []]
        finally:
            cm_restore(scene, objects, captured)
    changed = sorted(name for name, a in actions.items() if pf_digest(a) != digests[name])
    return {'touched': touched, 'owned': owned, 'actors': verified, 'secondary': secondary,
            'source_actions': {'digests_before': digests, 'changed': changed},
            'fingerprints': cm_fingerprints(params)}

# ------------------------------------------------------------------ accepted M7 face

def pf_owner(name, armature):
    if name == armature.name:
        return armature, 'OBJECT'
    key = bpy.data.shape_keys.get(name)
    if key is None:
        cm_fail('FACIAL_TRACK_MISSING', 'Key datablock ' + name + ' is gone')
    return key, 'KEY'

def pf_face_check(params):
    arm = cm_object(params['armature'], ['ARMATURE'])
    action = bpy.data.actions.get(params['action'])
    if action is None:
        cm_fail('FACIAL_CANDIDATE_MISSING', params['action'])
    if pf_digest(action) != params['fingerprint']:
        cm_fail('FACIAL_STALE', 'the accepted facial Action changed since M7 accepted it')
    owners = []
    for owner_name, track_name in params['m7_tracks']:
        owner, id_type = pf_owner(owner_name, arm)
        ad = owner.animation_data
        track = ad.nla_tracks.get(track_name) if ad else None
        if track is None or len(track.strips) != 1 or track.strips[0].action != action:
            cm_fail('FACIAL_TRACK_MISSING', track_name + ' on ' + owner_name + ' is missing or no longer plays ' + action.name)
        if ad.use_tweak_mode:
            cm_fail('NLA_TWEAK_MODE', owner_name + ' is in NLA tweak mode')
        own = ad.nla_tracks.get(params['own_track'])
        owners.append({'owner': owner, 'id_type': id_type, 'track': track, 'strip': track.strips[0], 'own': own})
    face_channels, body_channels, face_head, body_head = pf_head_channels(arm, action, owners, params['head_bones'],
                                                                         params['own_track'])
    if params['head_policy'] == 'BODY_OWNS' and face_head:
        cm_fail('HEAD_OWNERSHIP_CONFLICT', 'BODY_OWNS but the facial Action animates ' + ', '.join(face_head[:4]))
    if params['head_policy'] == 'FACE_OWNS' and body_head:
        cm_fail('HEAD_OWNERSHIP_CONFLICT', 'FACE_OWNS but the synchronized body animates ' + ', '.join(body_head[:4]))
    start = owners[0]['strip'].frame_start
    offset = params['start_frame'] - start
    reuse = abs(offset) < 1e-9 and all(o['strip'].extrapolation == params['extrapolation'] for o in owners)
    overlap = sorted(face_channels & body_channels)
    if not reuse and overlap:
        # An own FACE track lands above the body track and would invert M7's measured rule (body wins overlaps).
        cm_fail('FACIAL_BODY_CHANNEL_OVERLAP', 'the facial and body Actions both animate ' + ', '.join(overlap[:4]))
    window = [params['start_frame'], params['start_frame'] + (owners[0]['strip'].frame_end - start)]
    outside = [f for f in params.get('events') or [] if f < window[0] - 1e-6 or f > window[1] + 1e-6]
    if outside:
        cm_fail('FACIAL_EVENT_OUTSIDE_WINDOW', 'events at ' + str(outside[:5]) + ' fall outside the facial window '
                + str(window) + ' (scene frames)')
    return {'arm': arm, 'action': action, 'owners': owners, 'offset': offset, 'reuse': reuse, 'm7_start': start,
            'overlap': overlap, 'face_head': face_head, 'body_head': body_head, 'window': window}

def pf_head_channels(arm, action, owners, head_bones, own_track):
    # Face channels come from the armature slot the M7 strip plays; body channels from every unmuted source that is
    # neither an M7 face track nor the own FACE track (the synchronized BODY track included).
    arm_rows = [o for o in owners if o['id_type'] == 'OBJECT']
    face_channels = pf_channels([[action, arm_rows[0]['strip'].action_slot, None]]) if arm_rows else set()
    head_paths = ['pose.bones["' + b + '"]' for b in head_bones if b]
    face_head = sorted(c for c in face_channels if any(c.startswith(h) for h in head_paths))
    body_sources = [s for s in pf_sources(arm, False) if not (s[2] or '').startswith('M7_FACE') and s[2] != own_track]
    body_channels = pf_channels(body_sources)
    body_head = sorted(c for c in body_channels if any(c.startswith(h) for h in head_paths))
    return face_channels, body_channels, face_head, body_head

def pf_face_verify(scene, check, params, frames):
    rows = []
    arm = check['arm']
    for o in check['owners']:
        strip = o['strip'] if check['reuse'] else o['owner'].animation_data.nla_tracks.get(params['own_track']).strips[0]
        ad = o['owner'].animation_data
        names = [t.name for t in ad.nla_tracks]
        own_name = o['track'].name if check['reuse'] else params['own_track']
        above = []
        for t in ad.nla_tracks:
            if names.index(t.name) > names.index(own_name) and not t.mute:
                above.append([None, None, t.name])
                for s in t.strips:
                    above.append([s.action, s.action_slot, t.name])
        covered = pf_channels([a for a in above if a[0] is not None])
        curves = [c for c in pf_fcurves(check['action'], strip.action_slot) if pf_channel(c) not in covered][:48]
        worst = 0.0
        compared = 0
        for f in frames:
            a = pf_action_frame(f, strip.frame_start, strip.action_frame_start, strip.action_frame_end, strip.scale, strip.repeat)
            scene.frame_set(f)
            dg = cm_depsgraph(scene)
            item = arm.evaluated_get(dg) if o['id_type'] == 'OBJECT' else o['owner'].evaluated_get(dg)
            for curve in curves:
                value = pf_value(item, curve.data_path, curve.array_index)
                if value is None:
                    continue
                worst = max(worst, abs(value - curve.evaluate(a)))
                compared += 1
        rows.append({'owner': o['owner'].name, 'id_type': o['id_type'], 'track': own_name,
                     'frame_start': strip.frame_start, 'frame_end': strip.frame_end,
                     'action_frame_start': strip.action_frame_start, 'action_frame_end': strip.action_frame_end,
                     'extrapolation': strip.extrapolation, 'slot': strip.action_slot.identifier if strip.action_slot else None,
                     'channels_skipped_upper_tracks': len(covered), 'value_comparisons': compared, 'max_channel_error': worst,
                     'passed': compared > 0 and worst <= params['tolerance']})
    return rows

def pf_face(params):
    scene, src, cin = cm_candidate(params)
    check = pf_face_check(params)
    summary = {'offset_frames': check['offset'], 'm7_start': check['m7_start'], 'reuse_m7_track': check['reuse'],
               'overlapping_body_channels': check['overlap'][:10], 'head_policy': params['head_policy'],
               'window': check['window']}
    if params.get('dry_run'):
        return dict(summary, dry_run=True)
    before = pf_digest(check['action'])
    keys = [o['owner'] for o in check['owners'] if o['id_type'] == 'KEY']
    objects = pf_capture_all(params, cin, [check['arm']] + [o for o in bpy.data.objects if o.type == 'MESH' and o.data.shape_keys in keys])
    captured = cm_capture(scene, objects)
    touched = []
    try:
        for o in check['owners']:
            ad = o['owner'].animation_data
            if check['reuse']:
                if o['own'] is not None:
                    ad.nla_tracks.remove(o['own'])
                    o['track'].mute = False
                continue
            if o['own'] is not None:
                ad.nla_tracks.remove(o['own'])
            else:
                touched.append(cm_touch_track(o['owner'], o['id_type'], params['own_track']))
            track = cm_new_track(o['owner'], o['id_type'], params['own_track'], params)
            strip = track.strips.new(params['own_track'], int(params['start_frame']), check['action'])
            if strip.action_slot != o['strip'].action_slot:
                if o['id_type'] == 'OBJECT':
                    cm_ensure_pose(o['owner'])
                strip.action_slot = o['strip'].action_slot
            # Explicit range: a strip that could not auto-assign its slot is born one frame long (measured in M7).
            pf_set_range(strip, o['strip'].action_frame_start, o['strip'].action_frame_end)
            strip.blend_type = 'REPLACE'
            strip.extrapolation = params['extrapolation']
            strip.frame_start_ui = params['start_frame']
            if not o['track'].mute:
                touched.append(cm_touch_mute(o['owner'], o['id_type'], o['track']))
                o['track'].mute = True
    except Exception as exc:
        failed = cm_undo(touched)
        cm_restore(scene, objects, captured)
        cm_fail('CINEMATIC_FACE_ABORTED', str(exc) + '; reversed, failed rows ' + str(failed))
    first = check['owners'][0]['strip']
    length = first.action_frame_end - first.action_frame_start
    frames = pf_int_frames(params['start_frame'], params['start_frame'] + length, 8)
    with bpy.context.temp_override(scene=scene):
        try:
            verified = pf_face_verify(scene, check, params, frames)
        finally:
            cm_restore(scene, objects, captured)
    after = pf_digest(check['action'])
    return dict(summary, touched=touched, owned={}, tracks=verified, frames=frames,
                facial_action={'name': check['action'].name, 'unchanged': before == after},
                fingerprints=cm_fingerprints(params))

# ------------------------------------------------------------------ accepted M8 secondary (PC2)

def pf_object_digest(obj):
    mods = [[m.name, m.type, m.show_viewport, m.show_render, float(m.frame_start) if m.type == 'MESH_CACHE' else None,
             m.filepath if m.type == 'MESH_CACHE' else None] for m in obj.modifiers]
    props = sorted([k, str(obj[k])] for k in obj.keys())
    return m2_digest([obj.name, obj.data.name if obj.data else None, obj.parent.name if obj.parent else None,
                      obj.parent_bone, pf_rows(obj.matrix_basis), obj.hide_render, mods, props])

def pf_cache_modifier(obj):
    rows = [m for m in obj.modifiers if m.type == 'MESH_CACHE']
    if len(rows) != 1:
        cm_fail('SECONDARY_BACKEND_UNSUPPORTED', obj.name + ' has ' + str(len(rows)) + ' MESH_CACHE modifiers; only one PC2 playback is supported')
    mod = rows[0]
    if mod.cache_format != 'PC2' or mod.time_mode != 'FRAME' or mod.play_mode != 'SCENE':
        cm_fail('SECONDARY_BACKEND_UNSUPPORTED', 'cache playback must be PC2/FRAME/SCENE')
    if abs(mod.frame_scale - 1.0) > 1e-9:
        cm_fail('SECONDARY_RETIME_UNSUPPORTED', 'cache frame_scale is ' + str(mod.frame_scale))
    return mod

def pf_secondary_check(params):
    actor = cm_object(params['actor_object'])
    baked = cm_object(params['baked'], ['MESH'])
    scene = cm_scene(params['scene'])
    for name in (baked.name, params.get('m8_candidate')):
        if name and scene.objects.get(name) is not None:
            cm_fail('SECONDARY_M8_OBJECT_IN_CANDIDATE', name + ' (M8) is in the candidate scene; pass it in exclude_objects '
                    'of cinematic_create_candidate')
    if params.get('source_object') and scene.objects.get(params['source_object']) is not None:
        # Source collections are linked whole into _SRC, so an original can no longer be unlinked afterwards: the
        # exclusion has to be decided when the candidate is created.
        cm_fail('CINEMATIC_SECONDARY_SOURCE_NOT_EXCLUDED', params['source_object'] + ' (M8 secondary source) is still in '
                'the candidate scene; pass it in exclude_objects of cinematic_create_candidate')
    mod = pf_cache_modifier(baked)
    if abs(mod.frame_start - params['cache_start']) > 1e-6:
        cm_fail('SECONDARY_NOT_CURRENT', 'baked cache starts at ' + str(mod.frame_start) + ', the M8 record says ' + str(params['cache_start']))
    source = bpy.data.objects.get(params['source_object'])
    ad = actor.animation_data
    return {'actor': actor, 'baked': baked, 'mod': mod, 'source': source, 'follows': pf_follows(baked, actor),
            'primary_action': ad.action.name if ad is not None and ad.action is not None else None,
            'primary_slot': ad.action_slot.identifier if ad is not None and ad.action_slot is not None else None}

def pf_secondary(params):
    scene, src, cin = cm_candidate(params)
    source_scene = cm_scene(params['source_scene'])
    check = pf_secondary_check(params)
    summary = {'follows_actor': check['follows'], 'primary_action': check['primary_action'],
               'primary_slot': check['primary_slot'], 'cache_modifier': check['mod'].name}
    if params.get('dry_run'):
        return dict(summary, dry_run=True)
    baked = check['baked']
    baked_before = pf_object_digest(baked)
    cm_free_name(bpy.data.objects, params['copy'])
    touched = []
    owned = []
    try:
        copy = baked.copy()
        copy.name = params['copy']
        if copy.name != params['copy']:
            cm_fail('CINEMATIC_NAME_COLLISION', params['copy'])
        owned.append(copy)
        for key in list(copy.keys()):
            if key.startswith('production_'):
                # The copy is M10's, never M8's: M8 ownership marks would make M8 integrity adopt it.
                del copy[key]
        cin.objects.link(copy)
        cm_mark(copy, params, 'secondary_copy')
        # M8 creates <cand>_Baked hidden in render (it never activates the secondary); the M10 copy is what renders.
        copy.hide_render = False
        mod = pf_cache_modifier(copy)
        mod.frame_start = float(params['cache_start'] + params['delta'])
        ad = copy.animation_data
        if ad is not None and ad.action is not None and params['delta'] != 0:
            # A rigid accessory moves through its object Action, which the copy shares with M8's baked object: an NLA
            # strip on the owned copy shifts it by delta like the cache (the Action itself is never edited).
            action = ad.action
            slot = ad.action_slot
            ad.action = None
            track = ad.nla_tracks.new()
            track.name = 'M10_SECONDARY_MOTION'
            strip = track.strips.new('m8_motion', int(params['cache_start'] + params['delta']), action)
            if slot is not None and strip.action_slot != slot:
                strip.action_slot = slot
            pf_set_range(strip, float(params['cache_start']), float(params['cache_end']))
            strip.frame_start_ui = float(params['cache_start'] + params['delta'])
            strip.extrapolation = 'HOLD'
    except Exception as exc:
        failed = cm_undo(touched)
        for obj in owned:
            bpy.data.objects.remove(obj)
        cm_fail('CINEMATIC_SECONDARY_ABORTED', str(exc) + '; reversed, failed rows ' + str(failed))
    actor = check['actor']
    frames = list(range(int(params['cache_start']), int(params['cache_end']) + 1))
    on_source = pf_capture_list(params, [actor, baked])
    on_candidate = pf_capture_all(params, cin, [actor, copy])
    cap_source = cm_capture(source_scene, on_source)
    cap_candidate = cm_capture(scene, on_candidate)
    worst_local = 0.0
    worst_world = 0.0
    motion = 0.0
    first_values = None
    count = 0
    reference = []
    pick = set(int(round((len(frames) - 1) * i / max(1, params['reference_frames'] - 1))) for i in range(params['reference_frames']))
    try:
        for i, f in enumerate(frames):
            with bpy.context.temp_override(scene=source_scene):
                source_scene.frame_set(f)
                ref_parent, ref_values, ref_matrix = pf_mesh_parent(baked, source_scene)
                actor_ref = Matrix(cm_evaluated(source_scene, actor).matrix_world)
            with bpy.context.temp_override(scene=scene):
                scene.frame_set(f + params['delta'])
                parent_values, values, matrix = pf_mesh_parent(copy, scene)
            if len(values) != len(ref_values):
                cm_fail('SECONDARY_TOPOLOGY_CHANGED', 'copy and baked differ in vertex count')
            count = len(values) // 3
            for k in range(len(values)):
                worst_local = max(worst_local, abs(parent_values[k] - ref_parent[k]))
            # Equality alone would also hold if neither played; the motion range in the parent frame proves the
            # secondary (cache or object Action) is evaluated.
            if first_values is None:
                first_values = parent_values
            else:
                for k in range(len(values)):
                    motion = max(motion, abs(parent_values[k] - first_values[k]))
            indices = [int(round((count - 1) * j / max(1, params['points'] - 1))) for j in range(min(params['points'], count))]
            ref_points = pf_points(ref_values, ref_matrix, indices)
            if params['world_comparable']:
                for p, q in zip(pf_points(values, matrix, indices), ref_points):
                    worst_world = max(worst_world, (Vector(p) - Vector(q)).length)
            if i in pick:
                reference.append({'frame': f, 'actor': pf_rows(actor_ref), 'points': ref_points})
        outside = None
        if frames:
            with bpy.context.temp_override(scene=scene):
                scene.frame_set(frames[0] + params['delta'] - 1)
                before_values, l0, m0 = pf_mesh_parent(copy, scene)
                scene.frame_set(frames[0] + params['delta'])
                start_values, l1, m1 = pf_mesh_parent(copy, scene)
            outside = max(abs(a - b) for a, b in zip(before_values, start_values)) if len(before_values) else None
    finally:
        with bpy.context.temp_override(scene=source_scene):
            cm_restore(source_scene, on_source, cap_source)
        with bpy.context.temp_override(scene=scene):
            cm_restore(scene, on_candidate, cap_candidate)
    baked_after = pf_object_digest(baked)
    return dict(summary, touched=touched, owned={'objects': [copy.name]}, copy=copy.name,
                cache_frame_start=mod.frame_start, frames=len(frames), vertices=count,
                local_equivalence={'max_error_m': worst_local, 'frames': len(frames), 'all_vertices': True,
                                   'copy_motion_range_m': motion,
                                   'compared': 'copy at f + delta vs M8 baked at f, evaluated vertex positions in the '
                                               'parent frame (parent bone pose matrix or parent object)'},
                world_equivalence={'max_error_m': worst_world, 'points_per_frame': min(params['points'], count)}
                if params['world_comparable'] else {'status': 'PENDING_PRIMARY_OFFSET'},
                reference=reference, indices=[int(round((count - 1) * j / max(1, params['points'] - 1))) for j in range(min(params['points'], count))],
                outside_cache_first_frame_delta=outside, m8_baked_unchanged=baked_before == baked_after,
                baked_object_digest=baked_after, copy_hide_render=copy.hide_render,
                fingerprints=cm_fingerprints(params))

# ------------------------------------------------------------------ props

def pf_window_start(item):
    return item['frame_start']

def pf_bone_world(scene, arm, bone):
    ev = cm_evaluated(scene, arm)
    if bone:
        return Matrix(ev.matrix_world @ ev.pose.bones[bone].matrix)
    return Matrix(ev.matrix_world)

def pf_prop_check(row, params, scene):
    obj = cm_object(row['object'], ['MESH', 'EMPTY'])
    if cm_is_owned(obj, params) or obj.get('production_m10_owner'):
        cm_fail('PROP_IS_OWNED', obj.name + ' is an M10 datablock; give the original object')
    if scene.objects.get(obj.name) is not None:
        # Source collections are linked whole into _SRC: an original cannot be unlinked afterwards without changing
        # the source collection, so the exclusion is decided when the candidate is created.
        cm_fail('CINEMATIC_PROP_NOT_EXCLUDED', obj.name + ' is still in the candidate scene; pass it in exclude_objects '
                'of cinematic_create_candidate so only the M10 copy appears')
    if obj.library or obj.override_library or (obj.data is not None and obj.data.library):
        cm_fail('PROP_LINKED_UNSUPPORTED', obj.name + ' is linked or overridden')
    for other in bpy.data.objects:
        if other.parent == obj:
            cm_fail('PROP_HAS_CHILDREN', obj.name + ' has child ' + other.name + '; the copy would leave it behind')
    ad = obj.animation_data
    animated = ad is not None and (ad.action is not None or len(ad.nla_tracks) > 0)
    if animated and (row['attach'] or row.get('placement') or row['visibility']):
        cm_fail('PROP_ANIMATION_UNSUPPORTED', obj.name + ' is animated; placement, attach and visibility need an owned Action on the copy')
    for w in row['attach']:
        arm = cm_object(w['actor_object'])
        if w['bone'] and (arm.type != 'ARMATURE' or arm.pose.bones.get(w['bone']) is None):
            cm_fail('PROP_ATTACH_BONE_MISSING', str(w['bone']) + ' is not a bone of ' + arm.name)
    existing = bpy.data.objects.get(row['copy'])
    if existing is not None and not (cm_is_owned(existing, params) and row.get('replace')):
        cm_fail('CINEMATIC_NAME_COLLISION', row['copy'])
    return {'object': obj, 'existing': existing, 'row': row, 'animated': animated}

def pf_insert(bag, path, index, keys):
    curve = bag.fcurves.new(path, index=index)
    for frame, value in keys:
        point = curve.keyframe_points.insert(frame, value)
        point.interpolation = 'CONSTANT'
    curve.update()
    return curve

def pf_prop_build(check, params, scene, src, cin, touched, owned):
    obj = check['object']
    row = check['row']
    if check['existing'] is not None:
        old = check['existing']
        if old.animation_data is not None and old.animation_data.action is not None and cm_is_owned(old.animation_data.action, params):
            bpy.data.actions.remove(old.animation_data.action)
        bpy.data.objects.remove(old)
    placement = row.get('placement')
    # The original is excluded from the candidate: its placement is read evaluated in the source scene at that
    # scene's current frame (no frame_set there).
    original_world = Matrix(obj.evaluated_get(cm_depsgraph(cm_scene(params['source_scene']))).matrix_world)
    with bpy.context.temp_override(scene=scene):
        scene.frame_set(params['frame_start'])
        if placement:
            rot = [math.radians(v) for v in placement.get('rotation_deg') or [0.0, 0.0, 0.0]]
            p0 = Matrix.LocRotScale(Vector(placement['location']), Euler(rot, 'XYZ'), original_world.to_scale())
        else:
            p0 = original_world
        windows = sorted(row['attach'], key=pf_window_start)
        targets = {}
        for i, w in enumerate(windows):
            arm = cm_object(w['actor_object'])
            for f in (w['frame_start'], w['frame_end'], w['frame_end'] + 1):
                scene.frame_set(f)
                targets[(i, f)] = pf_bone_world(scene, arm, w['bone'])
    copy = obj.copy()
    cm_free_name(bpy.data.objects, row['copy'])
    copy.name = row['copy']
    if copy.name != row['copy']:
        cm_fail('CINEMATIC_NAME_COLLISION', row['copy'])
    owned['objects'].append(copy.name)
    cin.objects.link(copy)
    cm_mark(copy, params, 'prop_copy')
    copy.parent = None
    # The copy is owned: its inherited deltas are cleared so matrix_basis and the basis keys mean world placement.
    copy.delta_location = (0.0, 0.0, 0.0)
    copy.delta_rotation_euler = (0.0, 0.0, 0.0)
    copy.delta_rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
    copy.delta_scale = (1.0, 1.0, 1.0)
    for c in list(copy.constraints):
        copy.constraints.remove(c)
    copy.rotation_mode = 'QUATERNION'
    copy.matrix_basis = p0
    plan = []
    basis_keys = [[params['frame_start'], p0]]
    basis = p0
    inverse = None
    for i, w in enumerate(windows):
        s = w['frame_start']
        e = w['frame_end']
        contiguous = i > 0 and s == windows[i - 1]['frame_end'] + 1
        if contiguous:
            # Hand-off: the new holder must reproduce, at s, the world placement the previous holder gives at s.
            inverse = targets[(i, s)].inverted() @ targets[(i - 1, s)] @ inverse
        else:
            inverse = targets[(i, s)].inverted()
        name = 'M10_ATTACH_' + str(i)
        c = copy.constraints.new('CHILD_OF')
        c.name = name
        c.target = cm_object(w['actor_object'])
        if w['bone']:
            c.subtarget = w['bone']
        # Child Of is born with set_inverse_pending=True and would bake its own inverse at the first evaluation.
        c.set_inverse_pending = False
        c.inverse_matrix = inverse
        c.influence = 0.0
        owned['constraints'].append([copy.name, name])
        released = i + 1 >= len(windows) or windows[i + 1]['frame_start'] != e + 1
        if released:
            world = targets[(i, e)] @ inverse @ basis
            basis_keys.append([e + 1, world])
            basis = world
        plan.append({'name': name, 'window': [s, e], 'actor_object': w['actor_object'], 'bone': w['bone'],
                     'actor_id': w['actor_id'], 'handoff_from_previous': contiguous, 'released': released})
    if windows or row['visibility']:
        action = cm_new_action(params, row['action'], 'prop_animation')
        owned['actions'].append(action.name)
        slot = action.slots.new(id_type='OBJECT', name=copy.name)
        strip = action.layers.new('M10_PROP').strips.new(type='KEYFRAME')
        bag = strip.channelbag(slot, ensure=True)
        parts = [[k[0]] + list(k[1].decompose()) for k in basis_keys]
        for index in range(3):
            pf_insert(bag, 'location', index, [[p[0], p[1][index]] for p in parts])
            pf_insert(bag, 'scale', index, [[p[0], p[3][index]] for p in parts])
        for index in range(4):
            pf_insert(bag, 'rotation_quaternion', index, [[p[0], p[2][index]] for p in parts])
        for i, w in enumerate(windows):
            keys = [[w['frame_start'] - 1, 0.0], [w['frame_start'], 1.0], [w['frame_end'] + 1, 0.0]]
            pf_insert(bag, 'constraints["M10_ATTACH_' + str(i) + '"].influence', 0, keys)
        if row['visibility']:
            keys = [[v['frame'], 0.0 if v['visible'] else 1.0] for v in row['visibility']]
            pf_insert(bag, 'hide_render', 0, keys)
            pf_insert(bag, 'hide_viewport', 0, keys)
        ad = copy.animation_data_create()
        ad.action = action
        if ad.action_slot != slot:
            ad.action_slot = slot
    return copy, windows, plan

def pf_prop_verify(scene, copy, windows, plan, params):
    rows = []
    for i, w in enumerate(windows):
        arm = cm_object(w['actor_object'])
        s = w['frame_start']
        e = w['frame_end']
        frames = list(range(s - 1, e + 2))
        if len(frames) > 603:
            frames = [s - 1] + pf_int_frames(s, e, 600) + [e + 1]
        world = {}
        holder = {}
        for f in frames:
            scene.frame_set(f)
            world[f] = Matrix(cm_evaluated(scene, copy).matrix_world)
            holder[f] = pf_bone_world(scene, arm, w['bone'])
        rel0 = holder[s].inverted() @ world[s]
        slide = 0.0
        drift = 0.0
        for f in frames:
            if s <= f <= e:
                rel = holder[f].inverted() @ world[f]
                slide = max(slide, (rel.to_translation() - rel0.to_translation()).length)
                drift = max(drift, pf_rot_error_deg(rel, rel0))
        entry = {'constraint': plan[i]['name'], 'actor_id': w['actor_id'], 'bone': w['bone'], 'window': [s, e],
                 'world_at_start': list(world[s].to_translation()), 'world_at_end': list(world[e].to_translation()),
                 'hand_prop_slide_m': slide, 'hand_prop_rotation_drift_deg': drift, 'frames_measured': len(frames)}
        if plan[i]['handoff_from_previous']:
            entry['handoff_step_m'] = (world[s].to_translation() - world[s - 1].to_translation()).length
        else:
            entry['pop_in_m'] = (world[s].to_translation() - world[s - 1].to_translation()).length
        if plan[i]['released']:
            entry['pop_out_m'] = (world[e + 1].to_translation() - world[e].to_translation()).length
        entry['passed'] = slide <= params['slide_m'] and drift <= params['rotation_deg'] \
            and entry.get('pop_in_m', 0.0) <= params['pop_m'] and entry.get('pop_out_m', 0.0) <= params['pop_m']
        rows.append(entry)
    return rows

def pf_handoff_continuity(scene, copy, windows, plan):
    # At a hand-off frame h the prop must sit exactly where the previous holder would have carried it.
    rows = []
    for i, w in enumerate(windows):
        if not plan[i]['handoff_from_previous']:
            continue
        h = w['frame_start']
        prev = windows[i - 1]
        scene.frame_set(h)
        now = Matrix(cm_evaluated(scene, copy).matrix_world)
        c_prev = copy.constraints[plan[i - 1]['name']]
        holder = pf_bone_world(scene, cm_object(prev['actor_object']), prev['bone'])
        basis = Matrix(cm_evaluated(scene, copy).matrix_basis)
        expected = holder @ Matrix(c_prev.inverse_matrix) @ basis
        rows.append({'frame': h, 'from': prev['actor_id'], 'to': w['actor_id'],
                     'continuity_error_m': (now.to_translation() - expected.to_translation()).length,
                     'continuity_rotation_deg': pf_rot_error_deg(now, expected)})
    return rows

def pf_props(params):
    scene, src, cin = cm_candidate(params)
    checks = [pf_prop_check(row, params, scene) for row in params['props']]
    if params.get('dry_run'):
        return {'dry_run': True, 'props': [{'object': c['object'].name, 'animated': c['animated'],
                                             'replace': c['existing'] is not None} for c in checks]}
    actors = []
    for c in checks:
        for w in c['row']['attach']:
            arm = cm_object(w['actor_object'])
            if arm not in actors:
                actors.append(arm)
    digests = {c['object'].name: pf_object_digest(c['object']) for c in checks}
    # Copies about to be replaced are removed while building; restoring a removed object would raise.
    replaced = [c['existing'] for c in checks if c['existing'] is not None]
    actors = [o for o in pf_capture_all(params, cin, actors) if o not in replaced]
    captured = cm_capture(scene, actors)
    touched = []
    owned = {'objects': [], 'actions': [], 'constraints': []}
    built = []
    try:
        for c in checks:
            built.append([c] + list(pf_prop_build(c, params, scene, src, cin, touched, owned)))
    except Exception as exc:
        failed = cm_undo(touched)
        for name in owned['objects']:
            if bpy.data.objects.get(name) is not None:
                bpy.data.objects.remove(bpy.data.objects[name])
        for name in owned['actions']:
            if bpy.data.actions.get(name) is not None:
                bpy.data.actions.remove(bpy.data.actions[name])
        with bpy.context.temp_override(scene=scene):
            cm_restore(scene, actors, captured)
        cm_fail('CINEMATIC_PROPS_ABORTED', str(exc) + '; reversed, failed rows ' + str(failed))
    results = []
    with bpy.context.temp_override(scene=scene):
        try:
            for c, copy, windows, plan in built:
                results.append({'prop_id': c['row']['prop_id'], 'original': c['object'].name, 'copy': copy.name,
                                'shares_data_with_original': copy.data == c['object'].data,
                                'action': copy.animation_data.action.name if copy.animation_data and copy.animation_data.action else None,
                                'constraints': plan, 'attach_checks': pf_prop_verify(scene, copy, windows, plan, params),
                                'handoffs': pf_handoff_continuity(scene, copy, windows, plan),
                                'original_unchanged': pf_object_digest(c['object']) == digests[c['object'].name],
                                'original_parent': c['object'].parent.name if c['object'].parent else None})
        finally:
            cm_restore(scene, actors, captured)
    return {'touched': touched, 'owned': owned, 'props': results, 'fingerprints': cm_fingerprints(params)}

# ------------------------------------------------------------------ quality-report measurements (sampled, restored)

def pf_report_open(params):
    # Freshness is verified BEFORE the first frame_set: a candidate or source edited outside the tools is never
    # sampled (same rule as the analysis area).
    scene, src, cin = cm_candidate(params)
    source = cm_scene(params['source_scene'])
    prints = cm_fingerprints(params)
    expected = params.get('expected_fingerprints') or {}
    if expected.get('source') and prints['source'] != expected['source']:
        cm_fail('CINEMATIC_STALE_SOURCE', 'the source scene changed outside the cinematic tools; nothing was sampled')
    if expected.get('candidate') and prints['candidate'] != expected['candidate']:
        cm_fail('CINEMATIC_STALE_CANDIDATE', 'the candidate scene changed outside the cinematic tools; nothing was sampled')
    return scene, cin, source, prints

def pf_state_digest(scene, objects):
    snap = cm_capture(scene, objects)
    return m2_digest([snap['frame'], snap['subframe'], snap['camera'], snap['objects'], snap['keys']])

def pf_scene_frame(scene):
    return [scene.frame_current, scene.frame_subframe, scene.camera.name if scene.camera else None]

def pf_report_close(params, scene, source, objects, digest, source_frame, prints):
    return {'restoration': {'digest_before': digest, 'digest_after': pf_state_digest(scene, objects),
                            'source_before': source_frame, 'source_after': pf_scene_frame(source)},
            'fingerprints_before': prints, 'fingerprints_after': cm_fingerprints(params), 'filepath': bpy.data.filepath}

def pf_strip_start(strip):
    return strip.frame_start

def pf_sync_report(params):
    # Re-measures one actor's BODY track as it is now: the clips are rebuilt from the strips in Blender (not from
    # the ledger), so the host can compare them with what sync_actions recorded.
    scene, cin, source, prints = pf_report_open(params)
    actor = params['actor']
    obj = bpy.data.objects.get(actor['object'])
    ad = obj.animation_data if obj is not None else None
    track = ad.nla_tracks.get(actor['track']) if ad is not None and actor.get('track') else None
    out = {'actor_id': actor['actor_id'], 'object': actor['object'], 'track': actor.get('track'), 'status': 'MEASURED',
           'source_action_digests': {n: pf_digest(bpy.data.actions.get(n)) for n in params['actions']}}
    objects = pf_capture_all(params, cin, [obj] if obj is not None else [])
    captured = cm_capture(scene, objects)
    digest = pf_state_digest(scene, objects)
    source_frame = pf_scene_frame(source)
    if obj is None or track is None:
        out['status'] = 'OBJECT_MISSING' if obj is None else 'TRACK_MISSING'
        return dict(out, **pf_report_close(params, scene, source, objects, digest, source_frame, prints))
    strips = sorted(track.strips, key=pf_strip_start)
    clips = []
    problems = []
    for strip in strips:
        if strip.type != 'CLIP' or strip.action is None or strip.action_slot is None:
            problems.append([strip.name, strip.type])
            continue
        curves = pf_fcurves(strip.action, strip.action_slot)
        clips.append({'action': strip.action, 'slot': strip.action_slot, 'afs': strip.action_frame_start,
                      'afe': strip.action_frame_end, 'scale': strip.scale, 'repeat': strip.repeat,
                      'start': strip.frame_start, 'end': strip.frame_end, 'extrapolation': strip.extrapolation,
                      'curves': curves, 'channels': set(pf_channel(c) for c in curves),
                      'custom': pf_custom(set(c.data_path for c in curves)), 'strip': strip})
    out['strip_problems'] = problems
    out['clip_overlaps'] = [[clips[i - 1]['action'].name, clips[i]['action'].name] for i in range(1, len(clips))
                            if clips[i]['start'] < clips[i - 1]['end'] - 1e-6]
    out['track_muted'] = track.mute
    out['muted_strips'] = [s.name for s in strips if s.mute]
    if actor.get('carry_track'):
        carry = ad.nla_tracks.get(actor['carry_track'])
        out['carry_track'] = {'name': actor['carry_track'], 'present': carry is not None,
                              'muted': bool(carry.mute) if carry is not None else None,
                              'action_digest': pf_digest(carry.strips[0].action) if carry is not None and len(carry.strips) == 1 else None}
    out['blend_types'] = sorted(set(s.blend_type for s in strips))
    plan = {'object': obj, 'clips': clips, 'row': {'root_bone': actor.get('root_bone'), 'track': actor['track']}}
    with bpy.context.temp_override(scene=scene):
        try:
            out.update(pf_sync_verify(scene, plan, params))
        finally:
            cm_restore(scene, objects, captured)
    return dict(out, **pf_report_close(params, scene, source, objects, digest, source_frame, prints))

def pf_face_report(params):
    # Re-measures one integrated face: Action digest, the track that plays it (M7 reused or own), the M7 track mute
    # state recorded at integration, the strip mapping and the head/neck ownership against the current body.
    scene, cin, source, prints = pf_report_open(params)
    face = params['face']
    arm = bpy.data.objects.get(face['armature'])
    action = bpy.data.actions.get(face['action'])
    out = {'actor_id': face['actor_id'], 'mode': face['mode'], 'status': 'MEASURED', 'problems': [],
           'action_digest': pf_digest(action)}
    owners = []
    if arm is None or action is None:
        out['status'] = 'MISSING'
    else:
        reuse = face['mode'] == 'REUSED_M7_TRACK'
        for owner_name, track_name in face['m7_tracks']:
            owner = arm if owner_name == arm.name else bpy.data.shape_keys.get(owner_name)
            if owner is None:
                out['problems'].append(['FACIAL_TRACK_MISSING', owner_name + ' is gone'])
                continue
            ad = owner.animation_data
            m7 = ad.nla_tracks.get(track_name) if ad else None
            own = ad.nla_tracks.get(face['own_track']) if ad and face['own_track'] else None
            if m7 is None or len(m7.strips) != 1 or m7.strips[0].action != action:
                out['problems'].append(['FACIAL_TRACK_MISSING', track_name + ' on ' + owner_name + ' no longer plays ' + action.name])
                continue
            if m7.mute == reuse:
                out['problems'].append(['M7_TRACK_STATE_CHANGED', track_name + ' on ' + owner_name + ' mute=' + str(m7.mute)
                                        + ', recorded ' + ('unmuted (reused)' if reuse else 'muted (own track plays)')])
            if not reuse and (own is None or len(own.strips) != 1 or own.strips[0].action != action):
                out['problems'].append(['FACE_TRACK_MISSING', str(face['own_track']) + ' on ' + owner_name])
                continue
            playing = m7.strips[0] if reuse else own.strips[0]
            if abs(playing.frame_start - face['start_frame']) > 1e-4:
                out['problems'].append(['FACE_STRIP_MOVED', owner_name + ' plays from ' + str(playing.frame_start)
                                        + ', recorded ' + str(face['start_frame'])])
            owners.append({'owner': owner, 'id_type': 'OBJECT' if owner == arm else 'KEY', 'track': m7,
                           'strip': m7.strips[0], 'own': own, 'playing': playing})
    keys = [o['owner'] for o in owners if o['id_type'] == 'KEY']
    extra = ([arm] if arm is not None else []) + [o for o in bpy.data.objects if o.type == 'MESH' and o.data.shape_keys in keys]
    objects = pf_capture_all(params, cin, extra)
    captured = cm_capture(scene, objects)
    digest = pf_state_digest(scene, objects)
    source_frame = pf_scene_frame(source)
    if owners:
        playing = owners[0]['playing']
        out['window'] = [playing.frame_start, playing.frame_end]
        face_channels, body_channels, face_head, body_head = pf_head_channels(arm, action, owners, face['head_bones'],
                                                                             face['own_track'])
        names = [t.name for t in arm.animation_data.nla_tracks]
        above_body = []
        if not reuse and face['own_track'] in names:
            for t in arm.animation_data.nla_tracks:
                if t.mute or t.name == face['own_track'] or t.name.startswith('M7_FACE'):
                    continue
                shared = set()
                for s in t.strips:
                    shared |= pf_channels([[s.action, s.action_slot, t.name]]) & face_channels
                if shared and names.index(face['own_track']) > names.index(t.name):
                    above_body.append([t.name, len(shared)])
        out['head'] = {'policy': face['head_policy'], 'face_head': face_head[:6], 'body_head': body_head[:6],
                       'overlapping_channels': len(face_channels & body_channels), 'face_track_above_body': above_body}
        frames = pf_int_frames(face['start_frame'], face['start_frame'] + (playing.frame_end - playing.frame_start), 8)
        check = {'arm': arm, 'action': action, 'owners': owners, 'reuse': reuse}
        with bpy.context.temp_override(scene=scene):
            try:
                out['tracks'] = pf_face_verify(scene, check, {'own_track': face['own_track'], 'tolerance': params['tolerance']},
                                               frames)
            finally:
                cm_restore(scene, objects, captured)
        out['frames'] = frames
    return dict(out, **pf_report_close(params, scene, source, objects, digest, source_frame, prints))

def pf_secondary_report(params):
    # Re-measures one integrated M8 copy: local equivalence (copy at f + delta in the candidate vs the M8 baked
    # object at f in the source scene) on the stored reference frames, and world equivalence carried by the actor.
    scene, cin, source, prints = pf_report_open(params)
    rec = params['row']
    copy = bpy.data.objects.get(rec['copy'])
    baked = bpy.data.objects.get(rec['baked'])
    actor = bpy.data.objects.get(rec['actor_object'])
    out = {'actor_id': rec['actor_id'], 'copy': rec['copy'], 'baked': rec['baked'], 'status': 'MEASURED'}
    if copy is None or baked is None or actor is None:
        out['status'] = 'MISSING'
        out['missing'] = [n for n, o in [[rec['copy'], copy], [rec['baked'], baked], [rec['actor_object'], actor]] if o is None]
        return dict(out, **pf_report_close(params, scene, source, [], pf_state_digest(scene, []), pf_scene_frame(source), prints))
    mods = [m for m in copy.modifiers if m.type == 'MESH_CACHE']
    baked_mods = [m for m in baked.modifiers if m.type == 'MESH_CACHE']
    out.update(copy_in_cin=cin.objects.get(copy.name) is not None, copy_hide_render=copy.hide_render,
               cache_modifiers=len(mods), cache_frame_start=float(mods[0].frame_start) if len(mods) == 1 else None,
               cache_file_matches=len(mods) == 1 and len(baked_mods) == 1 and mods[0].filepath == baked_mods[0].filepath,
               baked_in_candidate=scene.objects.get(baked.name) is not None, baked_digest=pf_object_digest(baked),
               follows_actor=pf_follows(copy, actor))
    on_source = pf_capture_list(params, [actor, baked])
    on_candidate = pf_capture_all(params, cin, [actor, copy])
    cap_source = cm_capture(source, on_source)
    cap_candidate = cm_capture(scene, on_candidate)
    digest = pf_state_digest(scene, on_candidate)
    digest_source = pf_state_digest(source, on_source)
    source_frame = pf_scene_frame(source)
    worst = 0.0
    motion = 0.0
    first = None
    count = 0
    topology = True
    frames = [row['frame'] for row in rec['reference']]
    try:
        for f in frames:
            with bpy.context.temp_override(scene=source):
                source.frame_set(f)
                ref_values, l0, m0 = pf_mesh_parent(baked, source)
            with bpy.context.temp_override(scene=scene):
                scene.frame_set(f + rec['delta'])
                values, l1, m1 = pf_mesh_parent(copy, scene)
            if len(values) != len(ref_values):
                topology = False
                break
            count = len(values) // 3
            for k in range(len(values)):
                worst = max(worst, abs(values[k] - ref_values[k]))
            if first is None:
                first = values
            else:
                for k in range(len(values)):
                    motion = max(motion, abs(values[k] - first[k]))
        with bpy.context.temp_override(scene=scene):
            world = pf_secondary_world(scene, rec) if topology else {'status': 'NOT_MEASURED'}
    finally:
        with bpy.context.temp_override(scene=source):
            cm_restore(source, on_source, cap_source)
        with bpy.context.temp_override(scene=scene):
            cm_restore(scene, on_candidate, cap_candidate)
    out['local_equivalence'] = {'max_error_m': worst, 'frames': len(frames), 'vertices': count,
                                'copy_motion_range_m': motion, 'same_topology': topology,
                                'compared': 'copy at f + delta (candidate) vs M8 baked at f (source), vertex positions in '
                                            'the parent frame, all vertices'}
    out['world_equivalence'] = world
    close = pf_report_close(params, scene, source, on_candidate, digest, source_frame, prints)
    close['restoration']['source_digest_before'] = digest_source
    close['restoration']['source_digest_after'] = pf_state_digest(source, on_source)
    return dict(out, **close)
'''

STAGE = PERF_RUNTIME + '\nresult = pf_stage(params)\n'
SYNC = PERF_RUNTIME + '\nresult = pf_sync(params)\n'
FACE = PERF_RUNTIME + '\nresult = pf_face(params)\n'
SECONDARY = PERF_RUNTIME + '\nresult = pf_secondary(params)\n'
PROPS = PERF_RUNTIME + '\nresult = pf_props(params)\n'
SYNC_REPORT = PERF_RUNTIME + '\nresult = pf_sync_report(params)\n'
FACE_REPORT = PERF_RUNTIME + '\nresult = pf_face_report(params)\n'
SECONDARY_REPORT = PERF_RUNTIME + '\nresult = pf_secondary_report(params)\n'
PAYLOADS = {'stage': STAGE, 'sync': SYNC, 'face': FACE, 'secondary': SECONDARY, 'props': PROPS,
            'sync_report': SYNC_REPORT, 'face_report': FACE_REPORT, 'secondary_report': SECONDARY_REPORT}


# ====================================================================== host side (pure validation, testable)

def _frame(value, field):
    return model.number(value, field, -MAX_FRAME, MAX_FRAME, integer=True)


def _rows(options, key, allowed, maximum):
    rows = options.get(key)
    if not isinstance(rows, list) or not rows:
        model.fail('CINEMATIC_OPTION_REQUIRED', key + ' must be a non-empty list')
    if len(rows) > maximum:
        model.fail('CINEMATIC_CAPACITY', key + ' accepts at most ' + str(maximum) + ' rows')
    for row in rows:
        if not isinstance(row, dict):
            model.fail('CINEMATIC_OPTION_INVALID', key + ' rows must be objects')
        model.closed(row, allowed)
    return rows


def actor_row(plan, actor_id):
    for row in plan.get('actors') or []:
        if row['actor_id'] == actor_id:
            return row
    model.fail('ACTOR_NOT_IN_CINEMATIC', str(actor_id) + ' is not an actor of this cinematic plan')


def short_id(actor_id):
    return str(actor_id)[:8]


def normalize_staging(options, plan):
    rows = _rows(options, 'actors', STAGE_ROW, model.MAX_ACTORS)
    seen = set()
    result = []
    for row in rows:
        actor = actor_row(plan, row.get('actor_id'))
        if actor['actor_id'] in seen:
            model.fail('ACTOR_DUPLICATED', actor['actor_id'] + ' appears twice')
        seen.add(actor['actor_id'])
        location = model.vector(row.get('location', [0.0, 0.0, 0.0]), 'location', 3, -10000, 10000)
        yaw = model.number(row.get('yaw_deg', 0.0), 'yaw_deg', -360, 360)
        result.append({'actor_id': actor['actor_id'], 'object': actor['object'], 'location': location, 'yaw_deg': yaw})
    return result


def normalize_clips(options, plan):
    rows = _rows(options, 'clips', CLIP_ROW, model.MAX_ACTORS * MAX_CLIPS_PER_ACTOR)
    roots = options.get('root_bones') or {}
    if not isinstance(roots, dict):
        model.fail('CINEMATIC_OPTION_INVALID', 'root_bones must map actor_id to a bone name')
    actors = {}
    for row in rows:
        actor = actor_row(plan, row.get('actor_id'))
        if not isinstance(row.get('action'), str) or not row['action']:
            model.fail('CINEMATIC_ACTION_REQUIRED', 'each clip names a source Action')
        scale = model.number(row.get('scale', 1.0), 'scale', 0.01, 100)
        retime = row.get('retime', False)
        if not isinstance(retime, bool):
            model.fail('CINEMATIC_OPTION_INVALID', 'retime must be a boolean')
        if abs(scale - 1.0) > 1e-12 and not retime:
            model.fail('CLIP_RETIME_REQUIRED', row['action'] + ': scale ' + str(scale) + ' changes timing; pass retime=true to accept it explicitly')
        carry = row.get('carry_root', False)
        if not isinstance(carry, bool):
            model.fail('CINEMATIC_OPTION_INVALID', 'carry_root must be a boolean')
        clip = {'action': row['action'], 'slot': row.get('slot'), 'start_frame': _frame(row.get('start_frame'), 'start_frame'),
                'action_frame_start': None, 'action_frame_end': None, 'scale': scale,
                'repeat': model.number(row.get('repeat', 1.0), 'repeat', 0.1, 1000),
                'extrapolation': model.choice(row.get('extrapolation', 'NOTHING'), 'extrapolation', EXTRAPOLATIONS),
                'retime': retime, 'carry_root': carry}
        if clip['slot'] is not None and (not isinstance(clip['slot'], str) or not clip['slot']):
            model.fail('CINEMATIC_OPTION_INVALID', 'slot is a slot identifier such as OBRig')
        for key in ('action_frame_start', 'action_frame_end'):
            if row.get(key) is not None:
                clip[key] = model.number(row[key], key, -MAX_FRAME, MAX_FRAME)
        if clip['action_frame_start'] is not None and clip['action_frame_end'] is not None \
                and clip['action_frame_end'] <= clip['action_frame_start']:
            model.fail('CLIP_RANGE_INVALID', row['action'] + ': action_frame_end must exceed action_frame_start')
        entry = actors.setdefault(actor['actor_id'], {'actor_id': actor['actor_id'], 'object': actor['object'], 'clips': []})
        entry['clips'].append(clip)
        if len(entry['clips']) > MAX_CLIPS_PER_ACTOR:
            model.fail('CINEMATIC_CAPACITY', 'at most ' + str(MAX_CLIPS_PER_ACTOR) + ' clips per actor')
    for actor_id, bone in roots.items():
        if actor_id not in actors:
            model.fail('CINEMATIC_OPTION_INVALID', 'root_bones names ' + str(actor_id) + ', which has no clip in this call')
        actors[actor_id]['root_bone'] = model.name(bone, 'root_bones.' + actor_id)
    for entry in actors.values():
        first = min(entry['clips'], key=lambda c: c['start_frame'])
        if first['carry_root']:
            model.fail('CARRY_ROOT_FIRST_CLIP', entry['actor_id'] + ': ' + first['action'] + ' is the first clip; '
                       'there is no previous root to continue')
        explicit = all(c['action_frame_start'] is not None and c['action_frame_end'] is not None for c in entry['clips'])
        if explicit:
            spans = sorted((c['start_frame'], c['start_frame'] + (c['action_frame_end'] - c['action_frame_start']) * c['scale']
                            * c['repeat'], c['action']) for c in entry['clips'])
            for (s0, e0, a0), (s1, e1, a1) in zip(spans, spans[1:]):
                if s1 < e0 - 1e-6:
                    model.fail('CLIP_OVERLAP', entry['actor_id'] + ': ' + a1 + ' starts before ' + a0 + ' ends')
    return list(actors.values())


def normalize_props(options, plan, timeline):
    rows = _rows(options, 'props', PROP_ROW, MAX_PROPS)
    actor_objects = set()
    for actor in plan.get('actors') or []:
        actor_objects.add(actor['object'])
        actor_objects.update(actor.get('meshes') or [])
    seen = set()
    result = []
    for row in rows:
        prop_id = model.name(row.get('prop_id'), 'prop_id')
        if len(prop_id) > 24 or ' ' in prop_id:
            model.fail('CINEMATIC_NAME_INVALID', 'prop_id must be at most 24 characters without spaces')
        if prop_id in seen:
            model.fail('PROP_DUPLICATED', prop_id)
        seen.add(prop_id)
        obj = row.get('object')
        if not isinstance(obj, str) or not obj:
            model.fail('CINEMATIC_OPTION_INVALID', 'object names the original prop')
        if obj in actor_objects:
            model.fail('PROP_IS_ACTOR', obj + ' belongs to a registered actor')
        placement = None
        if row.get('placement') is not None:
            p = model.closed(row['placement'], PLACEMENT_ROW)
            placement = {'location': model.vector(p.get('location'), 'placement.location', 3, -10000, 10000),
                         'rotation_deg': model.vector(p.get('rotation_deg', [0.0, 0.0, 0.0]), 'placement.rotation_deg', 3, -360, 360)}
        attach = []
        windows = row.get('attach') or []
        if not isinstance(windows, list) or len(windows) > MAX_ATTACH:
            model.fail('CINEMATIC_OPTION_INVALID', 'attach is a list of at most ' + str(MAX_ATTACH) + ' windows')
        for w in windows:
            if not isinstance(w, dict):
                model.fail('CINEMATIC_OPTION_INVALID', 'attach rows are objects')
            model.closed(w, ATTACH_ROW)
            actor = actor_row(plan, w.get('actor_id'))
            bone = w.get('bone') or ''
            if bone:
                model.name(bone, 'attach.bone')
            start = _frame(w.get('frame_start'), 'attach.frame_start')
            end = _frame(w.get('frame_end'), 'attach.frame_end')
            if end < start:
                model.fail('PROP_ATTACH_WINDOW_INVALID', prop_id + ': frame_end before frame_start')
            attach.append({'actor_id': actor['actor_id'], 'actor_object': actor['object'], 'bone': bone,
                           'frame_start': start, 'frame_end': end})
        attach.sort(key=lambda w: w['frame_start'])
        for a, b in zip(attach, attach[1:]):
            if b['frame_start'] <= a['frame_end']:
                model.fail('PROP_ATTACH_OVERLAP', prop_id + ': windows ' + str([a['frame_start'], a['frame_end']]) + ' and '
                           + str([b['frame_start'], b['frame_end']]) + ' overlap; a hand-off starts the frame after the previous ends')
        if attach and attach[0]['frame_start'] <= timeline['frame_start']:
            model.fail('PROP_ATTACH_WINDOW_INVALID', prop_id + ': the first attach must start after the timeline start '
                       + str(timeline['frame_start']) + ' so the prop has a free placement to hold')
        visibility = []
        for v in row.get('visibility') or []:
            if not isinstance(v, dict):
                model.fail('CINEMATIC_OPTION_INVALID', 'visibility rows are objects')
            model.closed(v, VISIBILITY_ROW)
            if not isinstance(v.get('visible'), bool):
                model.fail('CINEMATIC_OPTION_INVALID', 'visibility.visible must be a boolean')
            visibility.append({'frame': _frame(v.get('frame'), 'visibility.frame'), 'visible': v['visible']})
        result.append({'prop_id': prop_id, 'object': obj, 'placement': placement, 'attach': attach,
                       'visibility': sorted(visibility, key=lambda v: v['frame'])})
    return result


def normalize_events(value):
    """options.events of integrate_facial: [{frame (scene frame, integer), label?}]; the window is checked in Blender."""
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_FACIAL_EVENTS:
        model.fail('CINEMATIC_OPTION_INVALID', 'events is a list of at most ' + str(MAX_FACIAL_EVENTS) + ' {frame, label} rows')
    rows = []
    for row in value:
        if not isinstance(row, dict):
            model.fail('CINEMATIC_OPTION_INVALID', 'events rows are objects')
        model.closed(row, EVENT_ROW)
        label = row.get('label')
        rows.append({'frame': _frame(row.get('frame'), 'events.frame'),
                     'label': model.text(label, 'events.label', 80) if label is not None else None})
    return sorted(rows, key=lambda r: r['frame'])


def facial_gate(fplan, fstate, plan, timeline):
    """Pure M7 acceptance gate (the Blender half compares fingerprints and tracks)."""
    if fstate.get('state') != 'ACCEPTED':
        model.fail('FACIAL_NOT_ACCEPTED', 'M7 performance is ' + str(fstate.get('state')) + '; only ACCEPTED faces are integrated')
    if fstate.get('pending_refinement') or fstate.get('uncertain_operation'):
        model.fail('FACIAL_NOT_ACCEPTED', 'M7 performance has a pending refinement or an uncertain operation')
    fps = (fplan.get('brief') or {}).get('fps')
    if fps != timeline['fps'] or abs(float(timeline.get('fps_base', 1.0)) - 1.0) > 1e-9:
        model.fail('FACIAL_FPS_MISMATCH', 'M7 brief at ' + str(fps) + ' fps, cinematic at ' + str(timeline['fps']) + '/'
                   + str(timeline.get('fps_base', 1.0)) + '; lip-sync is never retimed silently')
    policy = fplan.get('head_policy')
    if policy not in ('BODY_OWNS', 'FACE_OWNS'):
        model.fail('FACIAL_HEAD_POLICY_UNSUPPORTED', str(policy) + ': BLENDED needs the body Action active in COMBINE, '
                   'which NLA synchronization removes; declared limit')
    candidate = fstate.get('candidate_action')
    tracks = fstate.get('accepted_tracks') or []
    if not candidate or not fstate.get('candidate_fingerprint') or not tracks:
        model.fail('FACIAL_TRACK_MISSING', 'the M7 ledger has no accepted candidate and tracks')
    return {'candidate': candidate, 'fingerprint': fstate['candidate_fingerprint'], 'tracks': [list(t) for t in tracks],
            'policy': policy, 'armature': (fplan.get('profile') or {}).get('armature'),
            'head_bones': [(fplan.get('profile') or {}).get('head_bone'), (fplan.get('profile') or {}).get('neck_bone')]}


def secondary_gate(pplan, pstate, timeline, delta, window):
    """Pure part of the M8 acceptance gate; freshness (Blender) and cache.verify (host) run in the handler."""
    if pstate.get('state') != 'ACCEPTED':
        model.fail('SECONDARY_NOT_ACCEPTED', 'M8 generation is ' + str(pstate.get('state')) + '; only ACCEPTED secondary is integrated')
    if pstate.get('pending_refinement') or pstate.get('uncertain_operation'):
        model.fail('SECONDARY_NOT_ACCEPTED', 'M8 generation has a pending refinement or an uncertain operation')
    cache = pstate.get('cache') or {}
    if cache.get('representation') == 'SECONDARY_ACTION':
        model.fail('SECONDARY_BACKEND_UNSUPPORTED', 'armature SECONDARY_ACTION representation: its record holds digests, '
                   'not reusable motion, and offsetting it would need the rig copy M10 does not make; declared limit')
    if cache.get('representation') != 'PC2':
        model.fail('SECONDARY_BACKEND_UNSUPPORTED', 'only PC2 mesh caches are integrated')
    if not (pstate.get('bake_deviation') or {}).get('passed'):
        model.fail('SECONDARY_BAKE_NOT_EQUIVALENT', 'M8 bake equivalence did not pass')
    baked = (pstate.get('baked_candidate') or {}).get('candidate_object')
    if not baked:
        model.fail('SECONDARY_BAKED_MISSING', 'the M8 ledger has no baked candidate')
    fps = (pplan.get('brief') or {}).get('fps')
    if fps != timeline['fps'] or abs(float(timeline.get('fps_base', 1.0)) - 1.0) > 1e-9:
        model.fail('SECONDARY_FPS_MISMATCH', 'M8 brief at ' + str(fps) + ' fps, cinematic at ' + str(timeline['fps']))
    start = cache.get('frame_start')
    end = cache.get('frame_end')
    if not isinstance(start, int) or not isinstance(end, int):
        model.fail('SECONDARY_BACKEND_UNSUPPORTED', 'cache record lacks integer frame_start/frame_end')
    if window[0] < start + delta or window[1] > end + delta:
        model.fail('SECONDARY_WINDOW_OUTSIDE_CACHE', 'window ' + str(window) + ' leaves the cached clip shifted by '
                   + str(delta) + ': ' + str([start + delta, end + delta]) + '; the secondary does not exist outside it')
    primary = list((pplan.get('params') or {}).get('primary_objects') or (pplan.get('source_status') or {}).get('primary_objects') or [])
    return {'baked': baked, 'cache_start': start, 'cache_end': end, 'source_object': pplan.get('object_name'),
            'primary_objects': primary, 'm8_candidate': (pstate.get('candidate') or {}).get('candidate_object')}


# ====================================================================== handlers

def _load(ctx, req):
    path, plan, state = store.load(ctx.storage, req.cinematic)
    store.require_state(state, model.ASSEMBLY_STATES)
    if not state.get('assembly'):
        model.fail('CINEMATIC_CANDIDATE_REQUIRED', 'create the candidate scene first')
    return path, plan, state


def _timeline(plan, state):
    return state.get('timeline') or plan['timeline']


def _sample_frames(timeline, count=5):
    start, end = int(timeline['frame_start']), int(timeline['frame_end'])
    if end <= start:
        return [start]
    return sorted(set(start + int(round((end - start) * i / (count - 1))) for i in range(count)))


def _params(plan, state, extra):
    params = store.assembly_params(plan, state, extra)
    params['capture'] = lifecycle.actor_objects(plan.get('actors') or [])
    return params


def _actor_entry(state, actor):
    actors = state.setdefault('actors', {})
    return actors.setdefault(actor['actor_id'], {'object': actor['object']})


def _proposal(ctx, plan, state, path, body, params, label):
    data = ctx.bridge.run(body, dict(params, dry_run=True))
    return {'status': 'proposal', 'apply': False, 'step': label, 'checks': data, 'thresholds': THRESHOLDS,
            'next': 'call again with apply=true to change the candidate scene', **store.public(path, plan, state)}


def _secondary_records(state, actor_ids, with_clips_only=False):
    rows = []
    for actor_id in actor_ids:
        entry = (state.get('actors') or {}).get(actor_id) or {}
        if with_clips_only and not entry.get('clips'):
            continue
        for rec in entry.get('secondary') or []:
            rows.append({'actor_object': entry.get('object'), 'copy': rec['copy'], 'delta': rec['delta'],
                         'reference': rec['reference'], 'indices': rec['indices'],
                         'tolerance_m': THRESHOLDS['secondary_equivalence_m'], 'primary_action': rec['primary_action'],
                         'window': _requested_window(rec)})
    return rows


def _requested_window(rec):
    # 'window' of a stored secondary row is the cached scene window (motion and timeline read it); the window the
    # caller asked to cover is 'requested_window'.
    return rec.get('requested_window') or rec['window']


def _finish(path, plan, new, result, status_errors, payload_key, extra, warnings=()):
    store.record(path, new)
    errors = list(status_errors)
    return {'status': 'error' if errors else 'success', 'apply': True, 'errors': errors, 'warnings': list(warnings),
            payload_key: extra, 'thresholds': THRESHOLDS, 'touched_rows': len(result.get('touched') or []),
            **store.public(path, plan, new)}


def _root_bone(ctx, plan, actor_id, explicit):
    """Root bone for boundaries and gap checks: options first, then the actor registry; never guessed from names."""
    if explicit:
        return explicit, 'OPTIONS'
    try:
        record = registry.find(ctx.storage, plan.get('actor_registry_key'), actor_id)
    except (ValueError, OSError, TypeError):
        return None, 'NONE'
    bone = (record.get('bones') or {}).get('root') if isinstance(record.get('bones'), dict) else None
    if isinstance(bone, str) and bone and bone != 'UNKNOWN':
        return bone, 'REGISTRY'
    return None, 'NONE'


def gap_warnings(actor_id, verified):
    """ROOT_RESET_IN_NLA_GAP and ROOT_DISCONTINUITY_REVIEW rows (warnings, never errors) from a measured actor row."""
    rows = []
    gaps = verified.get('root_reset_gaps') or {}
    for hazard in gaps.get('hazards') or []:
        rows.append({'code': 'ROOT_RESET_IN_NLA_GAP', 'severity': 'WARNING', 'actor_id': actor_id,
                     'frames': hazard['frames'], 'gap': hazard['kind'], 'after': hazard.get('after'),
                     'before': hazard.get('before'), 'extrapolation': hazard['extrapolation'],
                     'measured': {'jump_m': hazard['jump_m'], 'frames_compared': hazard['frames_compared'],
                                  'root': hazard['measured_root'], 'transform_channels': hazard['channels']},
                     'note': 'no clip drives these frames: NLA-animated transform channels fall back to their default '
                             'value (measured in Blender 5.2.1: the actor jumped 2.0 m to the origin); fill the gap with '
                             'a clip or use HOLD/HOLD_FORWARD extrapolation'})
    for boundary in verified.get('boundaries') or []:
        if boundary.get('finding') == 'ROOT_DISCONTINUITY_REVIEW':
            rows.append({'code': 'ROOT_DISCONTINUITY_REVIEW', 'severity': 'WARNING', 'actor_id': actor_id,
                         'frames': boundary['frames'], 'between': boundary['between'],
                         'measured': {'jump_m': boundary['jump_m'], 'neighbour_step_m': boundary['neighbour_step_m'],
                                      'root': boundary['root']},
                         'threshold': {'root_jump_review_m': THRESHOLDS['root_jump_review_m'],
                                       'root_jump_review_ratio': THRESHOLDS['root_jump_review_ratio']},
                         'threshold_source': 'PROPOSAL'})
    return rows


@handler('cinematic_stage_actors')
def stage_actors(ctx, req):
    opts = model.closed(req.options, {'actors'})
    path, plan, state = _load(ctx, req)
    rows = normalize_staging(opts, plan)
    timeline = _timeline(plan, state)
    for row in rows:
        entry = (state.get('actors') or {}).get(row['actor_id']) or {}
        row['secondary_copies'] = [rec['copy'] for rec in entry.get('secondary') or []]
    all_actors = [{'actor_id': a['actor_id'], 'object': a['object'], 'meshes': a.get('meshes') or [],
                   'copies': [rec['copy'] for rec in ((state.get('actors') or {}).get(a['actor_id']) or {}).get('secondary') or []]}
                  for a in plan['actors']]
    params = _params(plan, state, {'actors': rows, 'frames': _sample_frames(timeline), 'all_actors': all_actors,
                                                 'secondary': _secondary_records(state, [r['actor_id'] for r in rows], True)})
    if not req.apply:
        return _proposal(ctx, plan, state, path, STAGE, params, 'stage_actors')
    new, result = store.mutate(ctx, path, plan, state, 'stage_actors', STAGE, params)
    errors = []
    for staged in result['staged']:
        actor = actor_row(plan, staged['actor_id'])
        entry = _actor_entry(new, actor)
        previous = entry.get('staging') or {}
        entry['staging'] = {'delta_location': staged['delta_location'], 'delta_rotation_euler': staged['delta_rotation_euler'],
                            'delta_rotation_quaternion': staged['delta_rotation_quaternion'],
                            'yaw_deg_total': (previous.get('yaw_deg_total') or 0.0) + next(r['yaw_deg'] for r in rows if r['actor_id'] == staged['actor_id']),
                            'rotated': bool(previous.get('rotated')) or any(r['yaw_deg'] != 0 for r in rows if r['actor_id'] == staged['actor_id']),
                            'max_position_error_m': staged['max_position_error_m'],
                            'max_rotation_error_deg': staged['max_rotation_error_deg'], 'formula': staged['formula']}
        if staged['max_position_error_m'] > THRESHOLDS['position_m'] or staged['max_rotation_error_deg'] > THRESHOLDS['rotation_deg']:
            errors.append({'code': 'STAGING_VERIFICATION_FAILED', 'actor_id': staged['actor_id'],
                           'message': 'evaluated world placement differs from the measured delta formula'})
    for rec in result.get('secondary') or []:
        if rec.get('status') == 'MEASURED' and not rec['passed']:
            errors.append({'code': 'SECONDARY_WORLD_EQUIVALENCE_FAILED', 'copy': rec['copy'], 'max_error_m': rec['max_error_m']})
    return _finish(path, plan, new, result, errors, 'staging',
                   {'actors': result['staged'], 'separation': result['separation'], 'secondary': result.get('secondary')})


@handler('cinematic_sync_actions')
def sync_actions(ctx, req):
    opts = model.closed(req.options, {'clips', 'root_bones'})
    path, plan, state = _load(ctx, req)
    actors = normalize_clips(opts, plan)
    timeline = _timeline(plan, state)
    rows = []
    for actor in actors:
        entry = (state.get('actors') or {}).get(actor['actor_id']) or {}
        secondary = [{'primary_action': rec['primary_action'], 'delta': rec['delta'], 'window': _requested_window(rec)}
                     for rec in entry.get('secondary') or []]
        root_bone, root_source = _root_bone(ctx, plan, actor['actor_id'], actor.get('root_bone'))
        rows.append({'actor_id': actor['actor_id'], 'object': actor['object'], 'clips': actor['clips'],
                     'root_bone': root_bone, 'root_source': root_source,
                     'rotated': bool((entry.get('staging') or {}).get('rotated')),
                     'replace': bool(entry.get('clips')), 'secondary': secondary,
                     'track': model.owned_name(plan['generation_id'], state['assembly']['version'], 'BODY_' + short_id(actor['actor_id'])),
                     'carry_track': model.owned_name(plan['generation_id'], state['assembly']['version'], 'ROOT_' + short_id(actor['actor_id']))})
    params = _params(plan, state, {'actors': rows, 'tolerance': THRESHOLDS['channel_value'],
                                   'root_jump_m': THRESHOLDS['root_jump_review_m'],
                                   'root_jump_ratio': THRESHOLDS['root_jump_review_ratio'],
                                   'timeline': [int(timeline['frame_start']), int(timeline['frame_end'])],
                                   'secondary': _secondary_records(state, [a['actor_id'] for a in actors])})
    if not req.apply:
        return _proposal(ctx, plan, state, path, SYNC, params, 'sync_actions')
    new, result = store.mutate(ctx, path, plan, state, 'sync_actions', SYNC, params)
    errors = []
    warnings = []
    if result['source_actions']['changed']:
        errors.append({'code': 'SOURCE_ACTION_CHANGED', 'actions': result['source_actions']['changed']})
    digests = result['source_actions']['digests_before']
    for row, verified in zip(rows, result['actors']):
        actor = actor_row(plan, row['actor_id'])
        entry = _actor_entry(new, actor)
        entry['body_track'] = row['track']
        entry['root_bone'] = row['root_bone']
        entry['root_source'] = row['root_source']
        entry['clips'] = [{k: c[k] for k in ('action', 'slot', 'frame_start', 'frame_end', 'action_frame_start',
                                              'action_frame_end', 'scale', 'repeat', 'extrapolation', 'offset_frames',
                                              'duration_frames', 'max_channel_error', 'mapping_passed', 'carry_root')}
                          for c in verified['clips']]
        # Digests of the source Actions as they were before this sync: the quality report compares them again.
        entry['source_action_digests'] = {c['action']: digests.get(c['action']) for c in verified['clips']}
        entry['clip_findings'] = {'gaps': verified['gaps'], 'boundaries': verified['boundaries'],
                                  'track_overlaps': verified['track_overlaps'],
                                  'root_reset_gaps': verified['root_reset_gaps']}
        entry['carry'] = verified.get('carry')
        carry = verified.get('carry')
        if carry and (carry['max_position_error_m'] > THRESHOLDS['carry_position_m']
                      or carry['max_yaw_error_deg'] > THRESHOLDS['carry_yaw_deg']):
            errors.append({'code': 'CARRY_ROOT_MISMATCH', 'actor_id': row['actor_id'],
                           'max_position_error_m': carry['max_position_error_m'],
                           'max_yaw_error_deg': carry['max_yaw_error_deg']})
        warnings += gap_warnings(row['actor_id'], verified)
        for c in verified['clips']:
            if not c['mapping_passed']:
                errors.append({'code': 'CLIP_MAPPING_FAILED', 'actor_id': row['actor_id'], 'action': c['action'],
                               'max_channel_error': c['max_channel_error'], 'value_comparisons': c['value_comparisons']})
    for rec in result.get('secondary') or []:
        if rec.get('status') == 'MEASURED' and not rec['passed']:
            errors.append({'code': 'SECONDARY_WORLD_EQUIVALENCE_FAILED', 'copy': rec['copy'], 'max_error_m': rec['max_error_m']})
    for rec, measured in zip(_secondary_records(state, [a['actor_id'] for a in actors]), result.get('secondary') or []):
        for actor in actors:
            entry = new['actors'][actor['actor_id']]
            for stored in entry.get('secondary') or []:
                if stored['copy'] == rec['copy']:
                    stored['world_check'] = measured
    return _finish(path, plan, new, result, errors, 'sync',
                   {'actors': result['actors'], 'secondary': result.get('secondary'),
                    'source_actions_unchanged': not result['source_actions']['changed']}, warnings)


@handler('cinematic_integrate_facial')
def integrate_facial(ctx, req):
    opts = model.closed(req.options, {'actor_id', 'facial', 'start_frame', 'extrapolation', 'events'})
    path, plan, state = _load(ctx, req)
    actor = actor_row(plan, opts.get('actor_id'))
    start = _frame(opts.get('start_frame'), 'start_frame')
    extrapolation = model.choice(opts.get('extrapolation', 'NOTHING'), 'extrapolation', EXTRAPOLATIONS)
    events = normalize_events(opts.get('events'))
    from ..facial import service as facial_service
    try:
        fpath, fplan, fstate = facial_service.load(ctx.storage, opts.get('facial'))
    except (ValueError, OSError, KeyError, TypeError) as exc:
        raise ValueError('FACIAL_REFERENCE_INVALID: ' + str(exc)[:300]) from exc
    timeline = _timeline(plan, state)
    gate = facial_gate(fplan, fstate, plan, timeline)
    if gate['armature'] != actor['object']:
        model.fail('FACIAL_ACTOR_MISMATCH', 'M7 plan drives ' + str(gate['armature']) + ', the actor is ' + actor['object'])
    try:
        status = facial_service.scene_status(ctx.bridge, fplan['object_name'], fplan['set_options'], gate['candidate'])
    except Exception as exc:  # a Blender-side refusal means the accepted face is not current
        raise ValueError('FACIAL_STALE: ' + str(exc)[:300]) from exc
    if status.get('candidate_fingerprint') != gate['fingerprint']:
        model.fail('FACIAL_STALE', 'the accepted facial Action changed after M7 accepted it')
    if 'M7_FACE ' + gate['candidate'] not in (status.get('facial_tracks') or []):
        model.fail('FACIAL_TRACK_MISSING', "the accepted 'M7_FACE " + gate['candidate'] + "' track is not on the rig")
    version = state['assembly']['version']
    params = _params(plan, state, {
        'armature': actor['object'], 'action': gate['candidate'], 'fingerprint': gate['fingerprint'],
        'm7_tracks': gate['tracks'], 'start_frame': start, 'extrapolation': extrapolation,
        'own_track': model.owned_name(plan['generation_id'], version, 'FACE_' + short_id(actor['actor_id'])),
        'head_policy': gate['policy'], 'head_bones': [b for b in gate['head_bones'] if b], 'tolerance': THRESHOLDS['channel_value'],
        'events': [e['frame'] for e in events]})
    if not req.apply:
        return _proposal(ctx, plan, state, path, FACE, params, 'integrate_facial')
    new, result = store.mutate(ctx, path, plan, state, 'integrate_facial', FACE, params)
    errors = []
    warnings = []
    if not result['facial_action']['unchanged']:
        errors.append({'code': 'FACIAL_ACTION_CHANGED', 'action': result['facial_action']['name']})
    for row in result['tracks']:
        if not row['passed']:
            errors.append({'code': 'FACIAL_MAPPING_FAILED', 'owner': row['owner'], 'max_channel_error': row['max_channel_error']})
    entry = _actor_entry(new, actor)
    reuse = result['reuse_m7_track']
    entry['facial'] = {'ref': str(fpath), 'generation_id': fplan.get('generation_id'), 'candidate': gate['candidate'],
                       'fingerprint': gate['fingerprint'], 'armature': actor['object'], 'm7_tracks': gate['tracks'],
                       'head_bones': params['head_bones'], 'window': result['window'],
                       'start_frame': start, 'offset_frames': result['offset_frames'], 'extrapolation': extrapolation,
                       'mode': 'REUSED_M7_TRACK' if reuse else 'OWN_TRACKS',
                       'own_track': None if reuse else params['own_track'],
                       'head_policy': gate['policy'], 'tracks': [{k: r[k] for k in ('owner', 'track', 'frame_start', 'frame_end',
                                                                                      'max_channel_error', 'passed')} for r in result['tracks']],
                       'events': len(events),
                       'acceptance_label': fstate.get('acceptance_label') or (fstate.get('review') or {}).get('label')}
    # Visibility analysis reads state['facial_events'] (FACE_HIDDEN_AT_EVENT); a new integration replaces this actor's.
    new['facial_events'] = [e for e in new.get('facial_events') or [] if e.get('actor_id') != actor['actor_id']] + [
        {'actor_id': actor['actor_id'], 'frame': e['frame'], 'label': e['label']} for e in events]
    if not events:
        warnings.append({'code': 'FACIAL_EVENTS_NOT_DECLARED', 'severity': 'WARNING', 'actor_id': actor['actor_id'],
                         'note': 'no options.events: the visibility analysis cannot check that the face is visible at '
                                 'its important frames'})
    return _finish(path, plan, new, result, errors, 'facial',
                   {k: result[k] for k in ('offset_frames', 'm7_start', 'reuse_m7_track', 'overlapping_body_channels',
                                           'tracks', 'frames', 'facial_action', 'window')}
                   | {'events': [e for e in new['facial_events'] if e['actor_id'] == actor['actor_id']]}, warnings)


@handler('cinematic_integrate_secondary')
def integrate_secondary(ctx, req):
    opts = model.closed(req.options, {'actor_id', 'physics', 'offset_frames', 'window'})
    path, plan, state = _load(ctx, req)
    actor = actor_row(plan, opts.get('actor_id'))
    delta = model.number(opts.get('offset_frames'), 'offset_frames', -100000, 100000, integer=True)
    timeline = _timeline(plan, state)
    window = opts.get('window') or [timeline['frame_start'], timeline['frame_end']]
    if not isinstance(window, list) or len(window) != 2:
        model.fail('CINEMATIC_OPTION_INVALID', 'window is [frame_start, frame_end]')
    window = [_frame(window[0], 'window[0]'), _frame(window[1], 'window[1]')]
    if window[1] < window[0]:
        model.fail('CINEMATIC_OPTION_INVALID', 'window ends before it starts')
    entry = (state.get('actors') or {}).get(actor['actor_id']) or {}
    rows = [r for r in (state.get('assembly') or {}).get('touched') or []
            if r.get('object') == actor['object'] or r.get('id_name') == actor['object']]
    if entry.get('staging') or entry.get('clips') or rows:
        # The M8 gate re-evaluates the primary as M8 signed it; M10 staging/NLA on the shared actor makes it stale
        # by construction, so the gate can only run before M10 touches the actor.
        model.fail('SECONDARY_GATE_ORDER', actor['actor_id'] + ' already has M10 staging, clips or NLA changes; integrate '
                   'the accepted secondary first, then stage and synchronize (sync then enforces the offset)')
    from ..physics import cache as physics_cache
    from ..physics import service as physics_service
    try:
        ppath, pplan, pstate = physics_service.load(ctx.storage, opts.get('physics'))
    except (ValueError, OSError, KeyError, TypeError) as exc:
        raise ValueError('SECONDARY_REFERENCE_INVALID: ' + str(exc)[:300]) from exc
    gate = secondary_gate(pplan, pstate, timeline, delta, window)
    if actor['object'] not in gate['primary_objects']:
        model.fail('SECONDARY_ACTOR_MISMATCH', 'M8 primary objects ' + str(gate['primary_objects']) + ' do not include ' + actor['object'])
    try:
        physics_service.fresh(ctx.bridge, ctx.storage, pplan, pstate)
        physics_cache.verify(ctx.storage, pstate['cache'], physics_service.cache_dependencies(pplan, pstate))
    except Exception as exc:  # RuntimeError from the bridge is as stale as a ValueError from the host (M8 pitfall)
        raise ValueError('SECONDARY_NOT_CURRENT: ' + str(exc)[:400]) from exc
    version = state['assembly']['version']
    copy = model.owned_name(plan['generation_id'], version, 'SEC_' + short_id(actor['actor_id']) + '_' + pplan['generation_id'][:6])
    params = _params(plan, state, {
        'actor_object': actor['object'], 'baked': gate['baked'], 'source_object': gate['source_object'], 'copy': copy,
        'm8_candidate': gate['m8_candidate'], 'cache_start': gate['cache_start'], 'cache_end': gate['cache_end'],
        'delta': delta, 'points': 4, 'reference_frames': 16, 'world_comparable': delta == 0})
    if not req.apply:
        return _proposal(ctx, plan, state, path, SECONDARY, params, 'integrate_secondary')
    probe = ctx.bridge.run(SECONDARY, dict(params, dry_run=True))
    params['world_comparable'] = delta == 0 or not probe['follows_actor']
    new, result = store.mutate(ctx, path, plan, state, 'integrate_secondary', SECONDARY, params)
    errors = []
    if result['local_equivalence']['max_error_m'] > THRESHOLDS['secondary_equivalence_m']:
        errors.append({'code': 'SECONDARY_EQUIVALENCE_FAILED', 'max_error_m': result['local_equivalence']['max_error_m']})
    if result['local_equivalence']['copy_motion_range_m'] <= 0.0:
        # Two caches that never play are also equal: equivalence only counts when the playback visibly moves.
        errors.append({'code': 'SECONDARY_PLAYBACK_UNPROVEN', 'message': 'the copy did not move across the cached clip'})
    world = result['world_equivalence']
    if world.get('max_error_m') is not None and world['max_error_m'] > THRESHOLDS['secondary_equivalence_m']:
        errors.append({'code': 'SECONDARY_WORLD_EQUIVALENCE_FAILED', 'max_error_m': world['max_error_m']})
    if not result['m8_baked_unchanged']:
        errors.append({'code': 'M8_OBJECT_CHANGED', 'object': gate['baked']})
    if result['copy_hide_render']:
        errors.append({'code': 'SECONDARY_COPY_HIDDEN', 'copy': result['copy']})
    entry = _actor_entry(new, actor)
    entry.setdefault('secondary', []).append({
        # 'object' and 'window' (scene frames actually cached) are what motion and timeline QA read.
        'object': result['copy'], 'window': [gate['cache_start'] + delta, gate['cache_end'] + delta],
        'requested_window': window, 'ref': str(ppath), 'generation_id': pplan['generation_id'], 'copy': result['copy'],
        'delta': delta, 'baked': gate['baked'], 'baked_object_digest': result['baked_object_digest'],
        'cache': {'sha256': pstate['cache'].get('sha256'), 'frame_start': gate['cache_start'], 'frame_end': gate['cache_end'],
                  'dependencies_fingerprint': pstate['cache'].get('dependencies_fingerprint')},
        'baked_fingerprint': pstate.get('baked_fingerprint'), 'source_fingerprint': (pplan.get('source_status') or {}).get('fingerprint'),
        'primary_action': result['primary_action'], 'primary_slot': result['primary_slot'],
        'required_primary_offset': delta, 'follows_actor': result['follows_actor'],
        'local_equivalence': result['local_equivalence'], 'world_equivalence': world,
        'reference': result['reference'], 'indices': result['indices'],
        'acceptance_label': pstate.get('acceptance_label') or (pstate.get('review') or {}).get('label')})
    return _finish(path, plan, new, result, errors, 'secondary',
                   {k: result[k] for k in ('copy', 'cache_frame_start', 'frames', 'vertices', 'local_equivalence',
                                           'world_equivalence', 'follows_actor', 'primary_action',
                                           'outside_cache_first_frame_delta', 'm8_baked_unchanged', 'copy_hide_render')}
                   | {'gate': {'state': pstate.get('state'), 'fresh': True, 'cache_verified': True,
                               'bake_deviation_passed': True, 'delta': delta, 'window': window},
                      'cached_window': [gate['cache_start'] + delta, gate['cache_end'] + delta],
                      'required_primary_offset': delta,
                      'next': 'stage/synchronize this actor; sync_actions requires a clip of ' + str(result['primary_action'])
                              + ' with start - action_frame_start = ' + str(delta) + ' and re-measures world equivalence'})


@handler('cinematic_manage_props')
def manage_props(ctx, req):
    opts = model.closed(req.options, {'props'})
    path, plan, state = _load(ctx, req)
    timeline = _timeline(plan, state)
    rows = normalize_props(opts, plan, timeline)
    version = state['assembly']['version']
    existing = state.get('props') or {}
    for row in rows:
        row['copy'] = model.owned_name(plan['generation_id'], version, 'PROP_' + row['prop_id'])
        row['action'] = model.owned_name(plan['generation_id'], version, 'PROPACT_' + row['prop_id'])
        row['replace'] = row['prop_id'] in existing
        if row['replace'] and existing[row['prop_id']].get('original') != row['object']:
            model.fail('PROP_DUPLICATED', row['prop_id'] + ' already copies ' + str(existing[row['prop_id']].get('original')))
    params = _params(plan, state, {'props': rows, 'frame_start': int(timeline['frame_start']),
                                                 'slide_m': THRESHOLDS['prop_slide_m'], 'pop_m': THRESHOLDS['prop_pop_m'],
                                                 'rotation_deg': THRESHOLDS['rotation_deg']})
    if not req.apply:
        return _proposal(ctx, plan, state, path, PROPS, params, 'manage_props')
    new, result = store.mutate(ctx, path, plan, state, 'manage_props', PROPS, params)
    errors = []
    props = new.setdefault('props', {})
    for row, measured in zip(rows, result['props']):
        props[row['prop_id']] = {'original': row['object'], 'copy': measured['copy'], 'action': measured['action'],
                                 'attach': row['attach'], 'placement': row['placement'], 'visibility': row['visibility'],
                                 'checks': [{k: c[k] for k in ('constraint', 'actor_id', 'window', 'hand_prop_slide_m',
                                                               'passed')} for c in measured['attach_checks']],
                                 'handoffs': measured['handoffs'], 'original_unchanged': measured['original_unchanged']}
        for w in row['attach']:
            actor = actor_row(plan, w['actor_id'])
            linked = _actor_entry(new, actor).setdefault('props', [])
            if row['prop_id'] not in linked:
                linked.append(row['prop_id'])
        for c in measured['attach_checks']:
            if not c['passed']:
                errors.append({'code': 'PROP_ATTACH_VERIFICATION_FAILED', 'prop_id': row['prop_id'], 'window': c['window'],
                               'slide_m': c['hand_prop_slide_m']})
        for h in measured['handoffs']:
            if h['continuity_error_m'] > THRESHOLDS['prop_pop_m']:
                errors.append({'code': 'PROP_HANDOFF_DISCONTINUITY', 'prop_id': row['prop_id'], 'frame': h['frame'],
                               'continuity_error_m': h['continuity_error_m']})
        if not measured['original_unchanged']:
            errors.append({'code': 'PROP_ORIGINAL_CHANGED', 'prop_id': row['prop_id']})
    return _finish(path, plan, new, result, errors, 'props', {'props': result['props']})


# ====================================================================== quality-report providers (review.py looks them up)
#
# Each one measures the candidate as it is NOW (never re-reads what was recorded when the step was applied), one
# bridge call per actor or copy so a payload stays bounded, and restores frame, channels and fingerprints. The shape
# is {'status': PASS|FAIL|NOT_APPLICABLE, 'passed': bool|None, 'findings', 'metrics' (flat scalars),
# 'state_restored', ...}; findings carry severity ERROR|WARNING|INFO and never a score.

REPORT_LIMITATIONS = {
    'sync': ['clips are rebuilt from the strips of the recorded BODY track; strips on other tracks are only reported '
             'as channel overlaps', 'mapping compares up to 64 channels and 8 integer frames per clip, no subframes',
             'root continuity is a point (root bone head or object origin), not the whole silhouette'],
    'facial': ['mapping compares up to 48 channels per owner and 8 integer frames; channels driven by upper unmuted '
               'tracks are skipped and counted', 'head/neck ownership is checked on animated channels, not on the '
               'evaluated pose'],
    'secondary': ['equivalence is measured on the 16 reference frames stored at integration, not on every cached frame',
                  'world equivalence uses 4 vertices per frame carried by the actor world matrix; it assumes the copy '
                  'follows the actor rigidly through its parent chain'],
}


def _finding(code, severity, **fields):
    row = {'code': code, 'severity': severity}
    row.update({k: v for k, v in fields.items() if v is not None})
    return row


def _not_applicable(kind, reason):
    return {'schema': 'production.cinematic_' + kind + '_report.v1', 'kind': kind, 'status': 'NOT_APPLICABLE',
            'passed': None, 'findings': [], 'metrics': {}, 'state_restored': True, 'reason': reason,
            'measured': False}


def restoration_of(result):
    """Restoration verdict of one report payload: frame/channels digest, source frame and fingerprints."""
    r = result['restoration']
    row = {'state_digest_equal': r['digest_before'] == r['digest_after'],
           'source_frame_and_camera_equal': r['source_before'] == r['source_after'],
           'fingerprints_equal': result['fingerprints_before'] == result['fingerprints_after']}
    if 'source_digest_before' in r:
        row['source_state_digest_equal'] = r['source_digest_before'] == r['source_digest_after']
    return row


def envelope(kind, findings, metrics, restorations, details):
    """Final report. Not restored, or any ERROR finding, is FAIL; warnings never fail a report."""
    restored = all(all(r.values()) for r in restorations)
    findings = list(findings)
    if not restored:
        findings.append(_finding('CINEMATIC_STATE_NOT_RESTORED', 'ERROR',
                                 measured=[r for r in restorations if not all(r.values())][:4],
                                 note='frame, channels or fingerprints differ after the measurement; inspect the '
                                      'candidate before any further step'))
    errors = [f for f in findings if f['severity'] == 'ERROR']
    passed = restored and not errors
    counts = {}
    for f in findings:
        counts[f['code']] = counts.get(f['code'], 0) + 1
    return {'schema': 'production.cinematic_' + kind + '_report.v1', 'kind': kind, 'status': 'PASS' if passed else 'FAIL',
            'passed': passed, 'findings': findings[:200], 'findings_by_code': dict(sorted(counts.items())),
            'metrics': metrics, 'state_restored': restored, 'restoration': restorations, 'details': details,
            'thresholds': THRESHOLDS, 'threshold_source': 'PROPOSAL', 'measured': True,
            'limitations': REPORT_LIMITATIONS[kind], 'visual_verification': 'required'}


def _report_params(plan, state, extra):
    params = _params(plan, state, extra)
    # The payload refuses to sample when the scenes moved since the last recorded step (checked before frame_set).
    params['expected_fingerprints'] = state.get('fingerprints')
    return params


def _close(a, b, tolerance=1e-4):
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
        return abs(a - b) <= tolerance
    return a == b


CLIP_FIELDS = ('action', 'frame_start', 'frame_end', 'action_frame_start', 'action_frame_end', 'scale', 'repeat',
               'extrapolation')


def sync_findings(measured, entry):
    """Pure: findings of one re-measured actor against the rows sync_actions recorded."""
    aid = measured['actor_id']
    if measured['status'] != 'MEASURED':
        return [_finding('SYNC_' + measured['status'], 'ERROR', actor_id=aid, track=measured.get('track'),
                         note='the recorded BODY track of this actor cannot be measured')]
    findings = []
    recorded = sorted(entry.get('clips') or [], key=lambda c: c['frame_start'])
    now = sorted(measured.get('clips') or [], key=lambda c: c['frame_start'])
    if len(recorded) != len(now):
        findings.append(_finding('CLIP_PLACEMENT_CHANGED', 'ERROR', actor_id=aid,
                                 measured={'recorded_clips': len(recorded), 'clips_now': len(now)}))
    else:
        for old, new in zip(recorded, now):
            moved = [k for k in CLIP_FIELDS if not _close(old.get(k), new.get(k))]
            if moved:
                findings.append(_finding('CLIP_PLACEMENT_CHANGED', 'ERROR', actor_id=aid, action=new.get('action'),
                                         measured={k: [old.get(k), new.get(k)] for k in moved}))
    for clip in now:
        if not clip['mapping_passed']:
            findings.append(_finding('CLIP_MAPPING_FAILED', 'ERROR', actor_id=aid, action=clip['action'],
                                     measured={'max_channel_error': clip['max_channel_error'],
                                               'value_comparisons': clip['value_comparisons']},
                                     threshold={'channel_value': THRESHOLDS['channel_value']},
                                     threshold_source='PROPOSAL'))
    for problem in measured.get('strip_problems') or []:
        findings.append(_finding('NLA_STRIP_UNSUPPORTED', 'ERROR', actor_id=aid, measured=problem))
    for pair in measured.get('clip_overlaps') or []:
        findings.append(_finding('CLIP_OVERLAP', 'ERROR', actor_id=aid, measured=pair))
    if measured.get('track_muted') or measured.get('muted_strips'):
        findings.append(_finding('SYNC_TRACK_MUTED', 'ERROR', actor_id=aid,
                                 measured={'track_muted': measured.get('track_muted'), 'muted_strips': measured.get('muted_strips')}))
    baseline = entry.get('source_action_digests') or {}
    current = measured.get('source_action_digests') or {}
    if not baseline:
        findings.append(_finding('SOURCE_ACTION_BASELINE_MISSING', 'WARNING', actor_id=aid,
                                 note='no digest was recorded at sync time; the source Actions cannot be compared'))
    changed = sorted(n for n, d in baseline.items() if current.get(n) != d)
    if changed:
        findings.append(_finding('SOURCE_ACTION_CHANGED', 'ERROR', actor_id=aid, measured={'actions': changed}))
    for row in measured.get('track_overlaps') or []:
        findings.append(_finding('TRACK_CHANNEL_OVERLAP', 'INFO', actor_id=aid, measured=row))
    carry = measured.get('carry_track')
    if carry and (not carry['present'] or carry['muted']):
        findings.append(_finding('CARRY_TRACK_CHANGED', 'ERROR', actor_id=aid, measured=carry,
                                 note='the root-continuity track recorded by sync_actions is missing or muted'))
    findings += gap_warnings(aid, measured)
    return findings


def sync_report(ctx, plan, state, options=None):
    """Quality-report provider 'sync': re-measures every synchronized actor (time mapping on sampled frames, root
    continuity at clip boundaries, gaps and root reset in NLA gaps, overlaps, source Action digests)."""
    model.closed(options, set())
    rows = [(actor_id, entry) for actor_id, entry in sorted((state.get('actors') or {}).items())
            if isinstance(entry, dict) and entry.get('clips')]
    if not rows:
        return _not_applicable('sync', 'no actor has synchronized clips in this candidate')
    store.fresh(ctx.bridge, ctx.storage, plan, state)
    timeline = _timeline(plan, state)
    findings, restorations, details = [], [], []
    for actor_id, entry in rows:
        actions = sorted(set((entry.get('source_action_digests') or {}).keys()) | {c['action'] for c in entry['clips']})
        params = _report_params(plan, state, {
            'actor': {'actor_id': actor_id, 'object': entry['object'], 'track': entry.get('body_track'),
                      'root_bone': entry.get('root_bone'), 'carry_track': (entry.get('carry') or {}).get('track')},
            'actions': actions, 'tolerance': THRESHOLDS['channel_value'], 'root_jump_m': THRESHOLDS['root_jump_review_m'],
            'root_jump_ratio': THRESHOLDS['root_jump_review_ratio'],
            'timeline': [int(timeline['frame_start']), int(timeline['frame_end'])]})
        measured = ctx.bridge.run(SYNC_REPORT, params)
        restorations.append(restoration_of(measured))
        findings += sync_findings(measured, entry)
        details.append({k: measured.get(k) for k in ('actor_id', 'object', 'track', 'status', 'clips', 'gaps',
                                                      'boundaries', 'track_overlaps', 'root_reset_gaps')})
    clips = [c for d in details for c in d.get('clips') or []]
    hazards = [h for d in details for h in ((d.get('root_reset_gaps') or {}).get('hazards') or [])]
    jumps = [b['jump_m'] for d in details for b in d.get('boundaries') or []]
    metrics = {'actors': len(rows), 'clips': len(clips),
               'value_comparisons': sum(c['value_comparisons'] for c in clips),
               'max_channel_error': max((c['max_channel_error'] for c in clips), default=None),
               'gaps_between_clips': sum(len(d.get('gaps') or []) for d in details),
               'root_reset_gaps': len(hazards), 'max_root_reset_jump_m': max((h['jump_m'] for h in hazards), default=None),
               'max_boundary_jump_m': max(jumps, default=None),
               'source_actions_checked': sum(len(e.get('source_action_digests') or {}) for _, e in rows),
               'source_actions_changed': sum(len(f['measured']['actions']) for f in findings
                                             if f['code'] == 'SOURCE_ACTION_CHANGED')}
    return envelope('sync', findings, metrics, restorations, details)


def facial_findings(measured, facial, ledger, events):
    """Pure: findings of one re-measured face against what integrate_facial recorded and the M7 ledger now."""
    aid = measured['actor_id']
    findings = []
    if ledger.get('error'):
        findings.append(_finding('FACIAL_REFERENCE_INVALID', 'ERROR', actor_id=aid, note=ledger['error']))
    elif ledger.get('state') != 'ACCEPTED':
        findings.append(_finding('FACIAL_NOT_ACCEPTED', 'ERROR', actor_id=aid, measured={'m7_state': ledger.get('state')}))
    elif not ledger.get('fingerprint_matches'):
        findings.append(_finding('FACIAL_STALE', 'ERROR', actor_id=aid,
                                 note='the M7 ledger fingerprint differs from the one integrated'))
    if measured['status'] != 'MEASURED':
        return findings + [_finding('FACIAL_TRACK_MISSING', 'ERROR', actor_id=aid,
                                    note='the facial Action or the armature is gone')]
    if measured.get('action_digest') != facial.get('fingerprint'):
        findings.append(_finding('FACIAL_ACTION_CHANGED', 'ERROR', actor_id=aid, action=facial.get('candidate')))
    for code, detail in measured.get('problems') or []:
        findings.append(_finding(code, 'ERROR', actor_id=aid, note=detail))
    if not measured.get('problems') and not measured.get('tracks'):
        # Nothing was compared (no recorded M7 track to follow): an empty measurement is never a pass.
        findings.append(_finding('FACIAL_NOT_MEASURED', 'ERROR', actor_id=aid))
    for row in measured.get('tracks') or []:
        if not row['passed']:
            findings.append(_finding('FACIAL_MAPPING_FAILED', 'ERROR', actor_id=aid, owner=row['owner'],
                                     measured={'max_channel_error': row['max_channel_error'],
                                               'value_comparisons': row['value_comparisons']},
                                     threshold={'channel_value': THRESHOLDS['channel_value']},
                                     threshold_source='PROPOSAL'))
    head = measured.get('head') or {}
    if head.get('policy') == 'BODY_OWNS' and head.get('face_head'):
        findings.append(_finding('HEAD_OWNERSHIP_CONFLICT', 'ERROR', actor_id=aid,
                                 measured={'policy': 'BODY_OWNS', 'facial_head_channels': head['face_head']}))
    if head.get('policy') == 'FACE_OWNS' and head.get('body_head'):
        findings.append(_finding('HEAD_OWNERSHIP_CONFLICT', 'ERROR', actor_id=aid,
                                 measured={'policy': 'FACE_OWNS', 'body_head_channels': head['body_head']}))
    if head.get('face_track_above_body'):
        findings.append(_finding('FACIAL_BODY_CHANNEL_OVERLAP', 'ERROR', actor_id=aid,
                                 measured={'tracks': head['face_track_above_body']},
                                 note='the own FACE track sits above a body track that animates the same channels; '
                                      'M7 measured that the body must win them'))
    elif head.get('overlapping_channels'):
        findings.append(_finding('FACIAL_BODY_CHANNEL_OVERLAP_BODY_WINS', 'INFO', actor_id=aid,
                                 measured={'channels': head['overlapping_channels']}))
    window = measured.get('window')
    if not events:
        findings.append(_finding('FACIAL_EVENTS_NOT_DECLARED', 'WARNING', actor_id=aid,
                                 note='no facial events: visibility cannot check the face at its important frames'))
    elif window:
        outside = [e['frame'] for e in events if e['frame'] < window[0] - 1e-6 or e['frame'] > window[1] + 1e-6]
        if outside:
            findings.append(_finding('FACIAL_EVENT_OUTSIDE_WINDOW', 'ERROR', actor_id=aid,
                                     measured={'frames': outside[:10], 'window': window}))
    return findings


def facial_report(ctx, plan, state, options=None):
    """Quality-report provider 'facial_timing': re-measures every integrated face (facial Action digest, strip
    mapping, M7 track muted or reused as recorded, head/neck policy against the current body)."""
    model.closed(options, set())
    rows = [(actor_id, entry['facial']) for actor_id, entry in sorted((state.get('actors') or {}).items())
            if isinstance(entry, dict) and entry.get('facial')]
    if not rows:
        return _not_applicable('facial', 'no actor has an integrated M7 face in this candidate')
    store.fresh(ctx.bridge, ctx.storage, plan, state)
    from ..facial import service as facial_service
    findings, restorations, details = [], [], []
    for actor_id, facial in rows:
        try:
            _, _, fstate = facial_service.load(ctx.storage, facial['ref'])
            ledger = {'state': fstate.get('state'),
                      'fingerprint_matches': fstate.get('candidate_fingerprint') == facial.get('fingerprint')}
        except (ValueError, OSError, KeyError, TypeError) as exc:
            ledger = {'error': str(exc)[:300]}
        actor = actor_row(plan, actor_id)
        params = _report_params(plan, state, {
            'face': {'actor_id': actor_id, 'armature': facial.get('armature') or actor['object'],
                     'action': facial['candidate'], 'mode': facial['mode'], 'own_track': facial.get('own_track'),
                     'm7_tracks': facial.get('m7_tracks') or [], 'start_frame': facial['start_frame'],
                     'head_policy': facial['head_policy'], 'head_bones': facial.get('head_bones') or []},
            'tolerance': THRESHOLDS['channel_value']})
        measured = ctx.bridge.run(FACE_REPORT, params)
        restorations.append(restoration_of(measured))
        events = [e for e in state.get('facial_events') or [] if e.get('actor_id') == actor_id]
        findings += facial_findings(measured, facial, ledger, events)
        details.append({k: measured.get(k) for k in ('actor_id', 'mode', 'status', 'window', 'tracks', 'head',
                                                      'problems', 'frames')} | {'m7_ledger': ledger, 'events': len(events)})
    tracks = [t for d in details for t in d.get('tracks') or []]
    metrics = {'faces': len(rows), 'tracks': len(tracks), 'value_comparisons': sum(t['value_comparisons'] for t in tracks),
               'max_channel_error': max((t['max_channel_error'] for t in tracks), default=None),
               'own_track_faces': sum(1 for _, f in rows if f['mode'] == 'OWN_TRACKS'),
               'reused_m7_faces': sum(1 for _, f in rows if f['mode'] == 'REUSED_M7_TRACK'),
               'events': len([e for e in state.get('facial_events') or [] if e.get('actor_id') in dict(rows)])}
    return envelope('facial', findings, metrics, restorations, details)


def secondary_findings(measured, rec, ledger, synced):
    """Pure: findings of one re-measured M8 copy against what integrate_secondary recorded and the M8 ledger now."""
    aid = measured['actor_id']
    tolerance = THRESHOLDS['secondary_equivalence_m']
    findings = []
    if ledger.get('error'):
        findings.append(_finding('SECONDARY_REFERENCE_INVALID', 'ERROR', actor_id=aid, note=ledger['error']))
    else:
        if ledger.get('state') != 'ACCEPTED':
            findings.append(_finding('SECONDARY_NOT_ACCEPTED', 'ERROR', actor_id=aid, measured={'m8_state': ledger.get('state')}))
        if ledger.get('cache_error'):
            findings.append(_finding('SECONDARY_CACHE_NOT_CURRENT', 'ERROR', actor_id=aid, note=ledger['cache_error']))
        elif not ledger.get('cache_sha_matches'):
            findings.append(_finding('SECONDARY_CACHE_NOT_CURRENT', 'ERROR', actor_id=aid,
                                     note='the M8 cache differs from the one integrated'))
    if measured['status'] != 'MEASURED':
        return findings + [_finding('SECONDARY_COPY_MISSING', 'ERROR', actor_id=aid, measured={'missing': measured.get('missing')})]
    expected_start = float(rec['cache']['frame_start'] + rec['delta'])
    if measured.get('cache_frame_start') is None or abs(measured['cache_frame_start'] - expected_start) > 1e-6:
        findings.append(_finding('SECONDARY_CACHE_OFFSET_CHANGED', 'ERROR', actor_id=aid,
                                 measured={'cache_frame_start': measured.get('cache_frame_start'), 'expected': expected_start}))
    if not measured.get('cache_file_matches'):
        findings.append(_finding('SECONDARY_CACHE_FILE_CHANGED', 'ERROR', actor_id=aid))
    if not measured.get('copy_in_cin'):
        findings.append(_finding('SECONDARY_COPY_NOT_OWNED', 'ERROR', actor_id=aid, copy=measured['copy']))
    if measured.get('copy_hide_render'):
        findings.append(_finding('SECONDARY_COPY_HIDDEN', 'ERROR', actor_id=aid, copy=measured['copy'],
                                 note='the M10 copy is hidden in render; the secondary would not appear'))
    if measured.get('baked_in_candidate'):
        findings.append(_finding('SECONDARY_M8_OBJECT_IN_CANDIDATE', 'ERROR', actor_id=aid, copy=measured['baked']))
    if rec.get('baked_object_digest') and measured.get('baked_digest') != rec['baked_object_digest']:
        findings.append(_finding('M8_OBJECT_CHANGED', 'ERROR', actor_id=aid, copy=measured['baked']))
    local = measured.get('local_equivalence') or {}
    if not local.get('same_topology', True):
        findings.append(_finding('SECONDARY_TOPOLOGY_CHANGED', 'ERROR', actor_id=aid))
    elif local.get('max_error_m') is None or local['max_error_m'] > tolerance:
        findings.append(_finding('SECONDARY_EQUIVALENCE_FAILED', 'ERROR', actor_id=aid,
                                 measured={'max_error_m': local.get('max_error_m'), 'frames': local.get('frames')},
                                 threshold={'secondary_equivalence_m': tolerance}, threshold_source='PROPOSAL'))
    if local.get('copy_motion_range_m') is not None and local['copy_motion_range_m'] <= 0.0:
        findings.append(_finding('SECONDARY_PLAYBACK_UNPROVEN', 'ERROR', actor_id=aid,
                                 note='the copy did not move across the sampled frames'))
    world = measured.get('world_equivalence') or {}
    if world.get('status') == 'MEASURED' and world['max_error_m'] > tolerance:
        findings.append(_finding('SECONDARY_WORLD_EQUIVALENCE_FAILED', 'ERROR', actor_id=aid,
                                 measured={'max_error_m': world['max_error_m'], 'frames': world['frames']},
                                 threshold={'secondary_equivalence_m': tolerance}, threshold_source='PROPOSAL'))
    elif world.get('status') != 'MEASURED':
        findings.append(_finding('SECONDARY_WORLD_EQUIVALENCE_NOT_MEASURED', 'ERROR', actor_id=aid, measured=world))
    if rec['delta'] != 0 and measured.get('follows_actor') and not synced:
        findings.append(_finding('SECONDARY_PRIMARY_NOT_SYNCHRONIZED', 'WARNING', actor_id=aid,
                                 measured={'delta': rec['delta']},
                                 note='the copy plays the cache shifted by delta but no clip of the actor was synchronized '
                                      'with that offset; the world equivalence above is what decides'))
    return findings


def secondary_report(ctx, plan, state, options=None):
    """Quality-report provider 'secondary': re-measures every M10 copy of an accepted M8 secondary (equivalence of the
    copy at f + delta against the M8 baked object at f on sampled frames, in mesh space and carried by the actor)."""
    model.closed(options, set())
    rows = [(actor_id, entry, rec) for actor_id, entry in sorted((state.get('actors') or {}).items()) if isinstance(entry, dict)
            for rec in entry.get('secondary') or []]
    if not rows:
        return _not_applicable('secondary', 'no actor has an integrated M8 secondary in this candidate')
    store.fresh(ctx.bridge, ctx.storage, plan, state)
    from ..physics import cache as physics_cache
    from ..physics import service as physics_service
    findings, restorations, details = [], [], []
    for actor_id, entry, rec in rows:
        ledger = {}
        baked = rec.get('baked')
        try:
            _, pplan, pstate = physics_service.load(ctx.storage, rec['ref'])
            ledger['state'] = pstate.get('state')
            baked = baked or (pstate.get('baked_candidate') or {}).get('candidate_object')
            try:
                physics_cache.verify(ctx.storage, pstate['cache'], physics_service.cache_dependencies(pplan, pstate))
                ledger['cache_sha_matches'] = pstate['cache'].get('sha256') == rec['cache'].get('sha256')
            except (ValueError, OSError, KeyError, TypeError) as exc:
                ledger['cache_error'] = str(exc)[:300]
        except (ValueError, OSError, KeyError, TypeError) as exc:
            ledger = {'error': str(exc)[:300]}
        params = _report_params(plan, state, {
            'row': {'actor_id': actor_id, 'actor_object': entry['object'], 'copy': rec['copy'], 'baked': baked,
                    'delta': rec['delta'], 'reference': rec['reference'], 'indices': rec['indices'],
                    'tolerance_m': THRESHOLDS['secondary_equivalence_m']}})
        measured = ctx.bridge.run(SECONDARY_REPORT, params)
        restorations.append(restoration_of(measured))
        findings += secondary_findings(measured, rec, ledger, bool(entry.get('clips')))
        details.append({k: measured.get(k) for k in ('actor_id', 'copy', 'baked', 'status', 'cache_frame_start',
                                                      'copy_hide_render', 'follows_actor', 'local_equivalence',
                                                      'world_equivalence')} | {'m8_ledger': ledger, 'window': rec.get('window')})
    local = [d['local_equivalence'] for d in details if d.get('local_equivalence')]
    world = [d['world_equivalence'] for d in details if (d.get('world_equivalence') or {}).get('status') == 'MEASURED']
    metrics = {'copies': len(rows), 'frames_compared': sum(r['frames'] for r in local),
               'max_local_error_m': max((r['max_error_m'] for r in local), default=None),
               'min_copy_motion_range_m': min((r['copy_motion_range_m'] for r in local), default=None),
               'max_world_error_m': max((r['max_error_m'] for r in world), default=None)}
    return envelope('secondary', findings, metrics, restorations, details)


def payload_budget(name, params=None):
    """(bytes, ast_nodes) of a payload as the bridge would send it; used by the unit tests."""
    import ast
    from ..common.bridge import build_code, payload_bytes
    body = PAYLOADS[name]
    code = build_code(body, params or {})
    return payload_bytes(body, params or {}), sum(1 for _ in ast.walk(ast.parse(code)))


__all__ = ['normalize_staging', 'normalize_clips', 'normalize_props', 'normalize_events', 'facial_gate', 'secondary_gate',
           'payload_budget', 'sync_report', 'facial_report', 'secondary_report', 'sync_findings', 'facial_findings',
           'secondary_findings', 'gap_warnings', 'envelope', 'restoration_of', 'THRESHOLDS', 'PAYLOADS']
