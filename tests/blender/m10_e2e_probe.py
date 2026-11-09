"""M10 end to end in real Blender: one demo of tests/blender/m10_cinematic_fixtures.py through the whole cinematic
pipeline, driven only by the real tools through service.dispatch (LocalBridge, no socket, no GUI, no port 9876).

    blender -b --factory-startup --disable-autoexec --python tests/blender/m10_e2e_probe.py -- <out_dir> <demo>
        [full|reject|technical] [source=<upstream_dir>] [review_timeout=<s>]

<out_dir> must not exist (or be empty): the demo .blend, the probe store, previs, preview and QA artefacts all live
there. `full` runs the pipeline and then waits for the coordinator's REAL visual review of the preview: it writes
<out_dir>/review-request-<label>.json and waits for review-feedback-<label>.json (the probe never writes a review),
then applies that decision with cinematic_review and cinematic_accept, cinematic_refine (the tool counts its three
cycles; each refined preview waits for a new review) or cinematic_reject. An accepted candidate is saved as a copy
in <out_dir>; the source .blend on disk is never written. `reject` runs the pipeline and then cinematic_reject, and
checks that the source comes back exactly. `technical` stops after the quality report (benchmarks).

Facial (M7) and secondary (M8) integration need ACCEPTED generations with a real visual review: this probe never
fabricates them. `source=<upstream_dir>` (demo C only) starts from tests/blender/m10_demo_c_upstream.py, whose .blend
holds an M7 face and an M8 pendant accepted after a real review and whose store holds those generations; without it
demo C declares those sections NOT_RUN with what is missing.

Every number in the summary is measured here (time.perf_counter, payload bytes and AST nodes of what the bridge
received, values returned by the tools, sha256 of files); thresholds belong to the tools and stay PROPOSAL.
"""
from __future__ import annotations

import ast
import contextlib
import hashlib
import io
import json
import math
import sys
import time
import traceback
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'tests' / 'blender'))
package = types.ModuleType('blender_mcp')
package.__path__ = [str(ROOT / 'src' / 'blender_mcp')]
sys.modules.setdefault('blender_mcp', package)

import bpy  # noqa: E402
from mathutils import Vector  # noqa: E402

from blender_mcp.production.common.bridge import MARKER, build_code  # noqa: E402
from blender_mcp.production.common.storage import Storage  # noqa: E402
from m31_fixture_support import register_open_fixture  # noqa: E402
import m10_cinematic_fixtures as fixtures  # noqa: E402

FPS = 30
ROLE_BONES = (('root', 'root'), ('pelvis', 'pelvis'), ('head', 'head'), ('hand_left', ('hands', 'L')),
              ('hand_right', ('hands', 'R')), ('foot_left', ('feet', 'L')), ('foot_right', ('feet', 'R')))
PREVIEW_SIZE = 480
ANALYSES = ('cinematic_analyze_composition', 'cinematic_validate_continuity', 'cinematic_analyze_visibility',
            'cinematic_analyze_actor_collisions', 'cinematic_analyze_contacts', 'cinematic_analyze_motion')
NOT_RUN_UPSTREAM = {
    'facial_timing': 'NOT_RUN: cinematic_integrate_facial needs an M7 facial generation in state ACCEPTED with a real '
                     'visual review (facial.service ledger); the demo face only has neutral ARKit shape keys. This probe '
                     'never fabricates an acceptance.',
    'secondary': 'NOT_RUN: cinematic_integrate_secondary needs an M8 generation ACCEPTED with a real review (PC2 cache, '
                 'bake_deviation.passed, cache.verify) and the source pendant plus the M8 objects excluded when the '
                 'candidate is created (exclude_objects); none exists for this demo.',
}


class Abort(Exception):
    pass


class LocalBridge:
    """Same code path as the socket bridge: build_code (safe mode), exec, the single result line parsed as strict
    JSON. Measures bytes, AST nodes and seconds of every payload."""

    def __init__(self):
        self.calls = 0
        self.bytes = 0
        self.seconds = 0.0
        self.log = []
        self._nodes = {}

    def run(self, body, params=None):
        code = build_code(body, params)
        size = len(code.encode('utf-8'))
        key = id(body)
        if key not in self._nodes:
            self._nodes[key] = sum(1 for _ in ast.walk(ast.parse(code)))
        buffer = io.StringIO()
        began = time.perf_counter()
        try:
            with contextlib.redirect_stdout(buffer):
                exec(code, {'bpy': bpy})
        finally:
            elapsed = time.perf_counter() - began
            self.calls += 1
            self.bytes += size
            self.seconds += elapsed
            self.log.append((size, self._nodes[key], elapsed))
        lines = [line[len(MARKER):] for line in buffer.getvalue().splitlines() if line.startswith(MARKER)]
        if len(lines) != 1:
            raise RuntimeError('LOCAL_BRIDGE: expected one result line, got ' + str(len(lines)))
        return json.loads(lines[0])


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def r6(value):
    return round(float(value), 6)


# ------------------------------------------------------------------------------------------- independent checks

def action_digest(action):
    """Keys, handles, interpolation, extrapolation and slots of every F-curve (independent of the tools' digest)."""
    rows = [action.name, [s.identifier for s in action.slots], [r6(v) for v in action.frame_range]]
    for layer in action.layers:
        for strip in layer.strips:
            for bag in strip.channelbags:
                for curve in sorted(bag.fcurves, key=lambda c: (c.data_path, c.array_index)):
                    rows.append([bag.slot.identifier if bag.slot else None, curve.data_path, curve.array_index,
                                 curve.extrapolation, len(curve.modifiers)])
                    for k in curve.keyframe_points:
                        rows.append([list(k.co), list(k.handle_left), list(k.handle_right), k.interpolation,
                                     k.handle_left_type, k.handle_right_type, k.easing])
    return hashlib.sha256(json.dumps(rows, default=float).encode('utf-8')).hexdigest()


def source_actions():
    return {a.name: action_digest(a) for a in bpy.data.actions if not a.get('production_m10_owner')}


def _mesh_digest(mesh):
    values = [0.0] * (len(mesh.vertices) * 3)
    mesh.vertices.foreach_get('co', values)
    rows = [len(mesh.vertices), len(mesh.polygons), [r6(v) for v in values]]
    if mesh.shape_keys:
        for block in mesh.shape_keys.key_blocks:
            data = [0.0] * (len(block.data) * 3)
            block.data.foreach_get('co', data)
            rows.append([block.name, r6(block.value), block.mute, block.relative_key.name, [r6(v) for v in data]])
    return hashlib.sha256(json.dumps(rows).encode('utf-8')).hexdigest()


def source_snapshot(scene, pose=True):
    """Independent snapshot of the user's source scene, split per object so touched actors can be told apart from
    everything else. Pose channels are evaluation results (frame_set flushes them) and are kept apart."""
    objects = {}
    for obj in sorted(scene.objects, key=lambda o: o.name):
        anim = obj.animation_data
        row = {'base': [obj.type, obj.data.name if obj.data else None, obj.parent.name if obj.parent else None,
                        obj.parent_type, obj.parent_bone, [r6(v) for v in obj.location], obj.rotation_mode,
                        [r6(v) for v in obj.rotation_euler], [r6(v) for v in obj.rotation_quaternion],
                        [r6(v) for v in obj.scale], sorted(c.name for c in obj.users_collection), obj.hide_render,
                        obj.hide_viewport, [[c.name, c.type, c.mute, r6(c.influence)] for c in obj.constraints],
                        [[m.name, m.type] for m in obj.modifiers],
                        [s.material.name if s.material else None for s in obj.material_slots]],
               'delta': [[r6(v) for v in obj.delta_location], [r6(v) for v in obj.delta_rotation_euler],
                         [r6(v) for v in obj.delta_rotation_quaternion], [r6(v) for v in obj.delta_scale]],
               'animdata': None if anim is None else [anim.action.name if anim.action else None,
                                                      anim.action_slot.identifier if anim.action_slot else None,
                                                      anim.action_extrapolation, anim.action_blend_type,
                                                      r6(anim.action_influence), anim.use_nla,
                                                      [[t.name, t.mute, [[s.name, s.action.name if s.action else None,
                                                                          r6(s.frame_start), r6(s.frame_end)]
                                                                         for s in t.strips]] for t in anim.nla_tracks]],
               'data': _mesh_digest(obj.data) if obj.type == 'MESH' else None}
        if obj.type == 'CAMERA':
            row['data'] = [r6(obj.data.lens), r6(obj.data.sensor_width), r6(obj.data.clip_start), r6(obj.data.clip_end)]
        if pose and obj.type == 'ARMATURE' and obj.pose:
            row['pose'] = {b.name: [[r6(v) for v in b.location], [r6(v) for v in b.rotation_quaternion]]
                           for b in obj.pose.bones}
        objects[obj.name] = row
    world = scene.world
    settings = [scene.frame_start, scene.frame_end, scene.render.fps, r6(scene.render.fps_base),
                scene.camera.name if scene.camera else None, scene.render.engine, scene.render.resolution_x,
                scene.render.resolution_y, [[m.name, m.frame, m.camera.name if m.camera else None] for m in scene.timeline_markers],
                world.name if world else None, scene.view_settings.view_transform, scene.view_settings.look,
                r6(scene.view_settings.exposure),
                sorted(c.name for c in scene.collection.children_recursive)]
    if world and world.use_nodes and world.node_tree:
        background = world.node_tree.nodes.get('Background')
        if background is not None:
            settings.append([r6(v) for v in background.inputs['Color'].default_value])
            settings.append(r6(background.inputs['Strength'].default_value))
    return {'settings': settings, 'objects': objects}


def compare_snapshots(before, after, touched_objects=()):
    """Sections that differ. For touched actors the delta and animation stack differences are recorded changes
    (reversible rows), listed apart from unexpected ones."""
    unexpected, recorded = [], []
    if before['settings'] != after['settings']:
        unexpected.append('scene settings')
    for name in sorted(set(before['objects']) | set(after['objects'])):
        a, b = before['objects'].get(name), after['objects'].get(name)
        if a is None or b is None:
            unexpected.append(name + ': ' + ('added' if a is None else 'removed'))
            continue
        for section in ('base', 'delta', 'animdata', 'data'):
            if a[section] != b[section]:
                (recorded if name in touched_objects and section in ('delta', 'animdata') else unexpected).append(
                    name + ': ' + section)
    return unexpected, recorded


def pose_differences(before, after):
    rows = []
    for name, row in before['objects'].items():
        other = after['objects'].get(name) or {}
        for bone, values in (row.get('pose') or {}).items():
            if (other.get('pose') or {}).get(bone) != values:
                rows.append(name + ':' + bone)
    return rows


# ------------------------------------------------------------------------------------------------- the pipeline

class Pipeline:
    def __init__(self, out, kind, mode, manifest, bridge, storage):
        self.out, self.kind, self.mode, self.manifest = out, kind, mode, manifest
        self.bridge, self.storage = bridge, storage
        self.ref = None
        self.scene_name = None
        self.ids = {}
        self.records = {}
        self.stages = []
        self.results = {}
        self.adjustments = []
        self.counter = 0
        self.runtime = None

    def dispatch(self, name, **kw):
        from blender_mcp.production.cinematic.service import dispatch
        self.counter += 1
        return dispatch(name, self.bridge, self.storage, 'e2e-' + format(self.counter, '03d') + '-' + name[10:30], **kw)

    def call(self, name, options=None, apply=False, **kw):
        return self.dispatch(name, cinematic=self.ref, options=options, apply=apply, **kw)

    def step(self, name, function, critical=True):
        start = len(self.bridge.log)
        began = time.perf_counter()
        row = {'stage': name}
        try:
            result = function()
            row['ok'] = True
        except Exception as exc:
            result = None
            row.update(ok=False, error=str(exc)[:1500], traceback=traceback.format_exc()[-3000:])
        row['seconds'] = round(time.perf_counter() - began, 4)
        calls = self.bridge.log[start:]
        row.update(bridge_calls=len(calls), payload_bytes=sum(c[0] for c in calls),
                   max_payload_bytes=max((c[0] for c in calls), default=0),
                   max_payload_ast_nodes=max((c[1] for c in calls), default=0),
                   bridge_seconds=round(sum(c[2] for c in calls), 4))
        if isinstance(result, dict):
            row['status'] = result.get('status')
            for key in ('warnings', 'errors'):
                values = result.get(key)
                if isinstance(values, list) and values:
                    row[key] = [(v.get('code') if isinstance(v, dict) else str(v)[:160]) for v in values][:12]
            if row['ok'] and result.get('status') == 'error':
                # A tool that applied a change but whose own verification failed is a failed stage, never OK.
                row.update(ok=False, error='TOOL_STATUS_ERROR: ' + ', '.join(row.get('errors') or ['status error']))
        self.stages.append(row)
        print(('OK   ' if row['ok'] else 'FAIL ') + name + ' ' + str(row['seconds']) + ' s'
              + ('' if row['ok'] else ' :: ' + row['error'][:400]), flush=True)
        write_summary(self)
        if not row['ok'] and critical:
            raise Abort(name + ': ' + row['error'][:500])
        self.results[name] = result
        return result

    # ---- helpers the demos use
    def actor(self, name):
        return next(a for a in self.manifest['actors'] if a['name'] == name)

    def sample(self, frames, function):
        """Probe-side measurement in the candidate (directing decisions, never QA): one frame_set per frame inside the
        candidate override, every candidate object captured and restored with the runtime's own cm_capture/cm_restore
        so the recorded fingerprints stay valid (verified by the next tool's freshness check)."""
        scene = bpy.data.scenes[self.scene_name]
        objects = list(scene.objects)
        rows = []
        state = self.runtime['cm_capture'](scene, objects)
        try:
            with bpy.context.temp_override(scene=scene, view_layer=scene.view_layers[0]):
                for frame in frames:
                    scene.frame_set(frame)
                    depsgraph = bpy.context.evaluated_depsgraph_get()
                    rows.append(function(scene, depsgraph, frame))
        finally:
            with bpy.context.temp_override(scene=scene, view_layer=scene.view_layers[0]):
                self.runtime['cm_restore'](scene, objects, state)
        return rows


def bone_point(depsgraph, object_name, bone, where='TAIL'):
    obj = bpy.data.objects[object_name].evaluated_get(depsgraph)
    pose_bone = obj.pose.bones[bone]
    local = pose_bone.tail if where == 'TAIL' else pose_bone.head if where == 'HEAD' else pose_bone.head.lerp(pose_bone.tail, .6)
    return obj.matrix_world @ local


def registered_bones(actor):
    bones = {}
    for role, ref in ROLE_BONES:
        value = actor['bones'][ref] if isinstance(ref, str) else actor['bones'][ref[0]][ref[1]]
        bones[role] = value
    return bones


def finding_rows(result, limit=16):
    """Compact WARNING/ERROR rows (code, shot, actor, frames, measured) so the summary says where, not only what."""
    rows = []
    for row in (result or {}).get('findings') or []:
        if isinstance(row, dict) and str(row.get('severity')).upper() in ('ERROR', 'WARNING'):
            rows.append({k: row.get(k) for k in ('code', 'severity', 'shot', 'shot_id', 'actor', 'actor_id', 'frames',
                                                 'count', 'measured', 'threshold') if row.get(k) is not None})
    return json.loads(json.dumps(rows[:limit], default=str)[:6000]) if len(json.dumps(rows[:limit], default=str)) <= 6000         else [{k: r.get(k) for k in ('code', 'severity', 'shot', 'shot_id', 'actor', 'actor_id', 'frames')} for r in rows[:limit]]


def finding_codes(result, limit=40):
    codes = {}
    for row in (result or {}).get('findings') or []:
        if isinstance(row, dict):
            key = str(row.get('code')) + ':' + str(row.get('severity'))
            codes[key] = codes.get(key, 0) + int(row.get('count') or 1)
    return dict(list(sorted(codes.items()))[:limit])


# ------------------------------------------------------------------------------------------------- demo direction
# Every camera position, timing and staging below is a directing decision of this probe (PROPOSAL), written down so
# the coordinator can judge it in the real review. Positions in metres, world space; actors face -Y at rest.

def head(aid):
    return {'actor_id': aid, 'bone': 'head'}


def demo_A(P):
    cid = P.ids['Creature']
    survivor = 'ENV_SourceCamera'  # no survivor actor in this demo: the creature looks at the camera position
    return {
        'intent': {'title': 'Creature reveal', 'description': 'The creature emerges from the darkness of the cave, '
                   'approaches, looks at the survivor (the camera) and prepares to attack.', 'duration_s': 15.0,
                   'actors': [{'actor_id': cid, 'role': 'CREATURE'}],
                   'events': [{'event_id': 'E_REVEAL', 'kind': 'REVEAL', 'actors': [cid], 'time_s': 2.0,
                               'action_refs': ['Creature_Emerge'], 'description': 'emerges from the darkness'},
                              {'event_id': 'E_APPROACH', 'kind': 'APPROACH', 'actors': [cid], 'time_s': 4.0,
                               'target': [0.0, 2.5, 0.0], 'description': 'leaves the cave mouth towards the survivor'},
                              {'event_id': 'E_LOOK', 'kind': 'OBSERVE', 'actors': [cid], 'time_s': 7.5, 'target': survivor,
                               'action_refs': ['Creature_Stare'], 'description': 'looks at the survivor / camera'},
                              {'event_id': 'E_ATTACK', 'kind': 'PREPARE_ATTACK', 'actors': [cid], 'time_s': 11.0,
                               'target': survivor, 'action_refs': ['Creature_AttackPrep'],
                               'description': 'rears up and prepares the attack'}],
                   'atmosphere': {'mood': 'dread', 'mood_preset': 'LOW_KEY', 'time_of_day': 'night'},
                   'lighting_intentions': {'preset': 'LOW_KEY', 'notes': 'cave interior stays dark'}},
        'paths': [{'actor': cid, 'target': [0.0, 2.5, 0.0], 'departure_s': 2.0, 'event_id': 'E_APPROACH',
                   'action': 'Creature_Emerge'}],
        'interactions': True,
        # Stare and AttackPrep are in place: carry_root starts them where Emerge left the root (measured in
        # bpm10-e2e-4: without it the root jumped 4.0 m back between frames 221 and 226, local step 0.025 m).
        'clips': lambda P: [dict(P.path_clip(cid), extrapolation='HOLD'),
                            {'actor_id': cid, 'action': 'Creature_Stare', 'start_frame': 226, 'action_frame_start': 1,
                             'action_frame_end': 91, 'extrapolation': 'HOLD_FORWARD', 'carry_root': True},
                            {'actor_id': cid, 'action': 'Creature_AttackPrep', 'start_frame': 331,
                             'action_frame_start': 1, 'action_frame_end': 46, 'extrapolation': 'HOLD_FORWARD',
                             'carry_root': True}],
        'staging': lambda P: [{'actor_id': cid, 'location': P.path_staging(cid), 'yaw_deg': 0.0}],
        'cameras': [
            {'shot': 'S01', 'location': [6.0, -6.0, 3.2], 'look_at': {'point': [0.0, 5.0, 1.8]}, 'lens': 24, 'move': 'STATIC'},
            {'shot': 'S02', 'location': [0.0, -5.5, 1.5], 'look_at': head(cid), 'lens': 28, 'move': 'DOLLY',
             'params': {'distance_m': 1.0}, 'easing': 'EASE_IN_OUT', 'target': head(cid)},
            {'shot': 'S03', 'location': [2.2, -1.0, 1.7], 'look_at': head(cid), 'lens': 32, 'move': 'TRACKING',
             'params': {'look_at_target': False}, 'target': {'actor_id': cid}},
            # From S04 on the creature is where the approach left it: cameras are placed from its measured head.
            {'shot': 'S04', 'relative_to': {'actor_id': cid, 'bone': 'head', 'frame': 226}, 'offset': [1.6, -3.2, -0.2],
             'look_at': head(cid), 'lens': 45, 'move': 'STATIC', 'focus': head(cid), 'fstop': 2.8},
            {'shot': 'S05', 'relative_to': {'actor_id': cid, 'bone': 'head', 'frame': 331}, 'offset': [1.0, -3.6, -2.2],
             'look_at': head(cid), 'lens': 24, 'move': 'ORBIT', 'params': {'angle_deg': -20.0},
             'target': {'actor_id': cid}, 'focus': head(cid), 'fstop': 4.0},
            {'shot': 'S06', 'relative_to': {'actor_id': cid, 'bone': 'head', 'frame': 391}, 'offset': [-3.0, -6.5, -1.0],
             'look_at': head(cid), 'lens': 30, 'move': 'DOLLY', 'params': {'distance_m': -1.5}, 'easing': 'EASE_OUT',
             'target': head(cid)}],
        'lighting': {'preset': 'LOW_KEY', 'key_subject': cid, 'shot': 'S02'},
    }


def demo_B(P):
    """Task scenario: A approaches B; B notices A, turns towards A and reacts. One Walk cycle covers 2.2 m, so A is
    staged 1.2 m further back, faces B and stops 1.0 m from it. carry_root keeps each actor where its previous clip
    left it: A's position after the walk, B's yaw after the turn (without it the React and Idle clips, which key the
    root at rest, would snap B back). Timing, staging and cameras are PROPOSAL."""
    a, b = P.ids['HumanA'], P.ids['HumanB']
    shots = {'schema': 'production.cinematic_shots.v1', 'shots': [
        {'shot_id': 'S01', 'type': 'WIDE', 'frame_start': 1, 'frame_end': 75, 'subjects': [a, b]},
        {'shot_id': 'S02', 'type': 'MEDIUM', 'frame_start': 76, 'frame_end': 112, 'subjects': [b]},
        {'shot_id': 'S03', 'type': 'MEDIUM', 'frame_start': 113, 'frame_end': 165, 'subjects': [a, b]},
        {'shot_id': 'S04', 'type': 'CLOSE_UP', 'frame_start': 166, 'frame_end': 225, 'subjects': [a]},
        {'shot_id': 'S05', 'type': 'WIDE', 'frame_start': 226, 'frame_end': 300, 'subjects': [a, b]}]}
    return {
        'intent': {'title': 'Two characters', 'description': 'A approaches B; B notices A, turns towards A and reacts.',
                   'duration_s': 10.0, 'actors': [{'actor_id': a, 'role': 'PROTAGONIST'}, {'actor_id': b, 'role': 'SUPPORTING'}],
                   'events': [{'event_id': 'E_APPROACH', 'kind': 'APPROACH', 'actors': [a], 'target': b, 'time_s': 1.0,
                               'action_refs': ['HumanA_Walk']},
                              {'event_id': 'E_NOTICE', 'kind': 'OBSERVE', 'actors': [b], 'target': a, 'time_s': 2.2},
                              {'event_id': 'E_TURN', 'kind': 'TURN_TOWARD', 'actors': [b], 'target': a, 'time_s': 2.5,
                               'action_refs': ['HumanB_Turn']},
                              {'event_id': 'E_REACT', 'kind': 'REACT', 'actors': [b], 'target': a, 'time_s': 3.733,
                               'action_refs': ['HumanB_React']}],
                   'atmosphere': {'mood': 'tense', 'mood_preset': 'NEUTRAL_THREE_POINT'}},
        'shots_list': shots,
        'interaction_rows': interactions_B,
        'staging': lambda P: [{'actor_id': a, 'location': [-1.2, 0.0, 0.0], 'yaw_deg': 90.0},
                              {'actor_id': b, 'location': [0.0, 0.0, 0.0], 'yaw_deg': 0.0}],
        'clips': lambda P: [
            {'actor_id': a, 'action': 'HumanA_Idle', 'start_frame': 1, 'action_frame_start': 1, 'action_frame_end': 31,
             'extrapolation': 'HOLD'},
            {'actor_id': a, 'action': 'HumanA_Walk', 'start_frame': 31, 'action_frame_start': 1, 'action_frame_end': 81,
             'extrapolation': 'HOLD_FORWARD'},
            {'actor_id': a, 'action': 'HumanA_Idle', 'start_frame': 112, 'action_frame_start': 1, 'action_frame_end': 61,
             'repeat': 3.1, 'extrapolation': 'HOLD_FORWARD', 'carry_root': True},
            {'actor_id': b, 'action': 'HumanB_Idle', 'start_frame': 1, 'action_frame_start': 1, 'action_frame_end': 61,
             'repeat': 1.25, 'extrapolation': 'HOLD'},
            {'actor_id': b, 'action': 'HumanB_Turn', 'start_frame': 76, 'action_frame_start': 1, 'action_frame_end': 37,
             'extrapolation': 'HOLD_FORWARD'},
            {'actor_id': b, 'action': 'HumanB_React', 'start_frame': 113, 'action_frame_start': 1, 'action_frame_end': 31,
             'extrapolation': 'HOLD_FORWARD', 'carry_root': True},
            {'actor_id': b, 'action': 'HumanB_Idle', 'start_frame': 144, 'action_frame_start': 1, 'action_frame_end': 61,
             'repeat': 2.6, 'extrapolation': 'HOLD_FORWARD', 'carry_root': True}],
        'cameras': [
            {'shot': 'S01', 'location': [0.0, -6.5, 2.0], 'look_at': {'point': [-0.6, 0.0, 1.0]}, 'lens': 28, 'move': 'STATIC'},
            {'shot': 'S02', 'relative_to': {'actor_id': b, 'bone': 'head', 'frame': 76}, 'offset': [-0.6, -2.4, -0.1],
             'look_at': head(b), 'lens': 40, 'move': 'STATIC', 'focus': head(b), 'fstop': 2.8},
            {'shot': 'S03', 'location': [0.5, -3.2, 1.5], 'look_at': {'point': [0.5, 0.0, 1.35]}, 'lens': 35, 'move': 'STATIC'},
            {'shot': 'S04', 'relative_to': {'actor_id': a, 'bone': 'head', 'frame': 166}, 'offset': [0.8, -1.5, 0.0],
             'look_at': head(a), 'lens': 50, 'move': 'STATIC', 'focus': head(a), 'fstop': 2.8},
            {'shot': 'S05', 'location': [-2.4, -5.2, 1.8], 'look_at': {'point': [0.5, 0.0, 1.0]}, 'lens': 28, 'move': 'CRANE',
             'params': {'height_m': 0.6}, 'easing': 'EASE_IN_OUT', 'target': {'point': [0.5, 0.0, 1.0]}}],
        'lighting': {'preset': 'NEUTRAL_THREE_POINT', 'key_subject': a, 'shot': 'S01'},
    }


def interactions_B(P):
    """Analysis-format events: the gaze is measurable only with the head bone's local forward axis, measured here on
    the rest matrix (rest^-1 @ armature -Y, the fixture's facing), never guessed."""
    a, b = P.ids['HumanA'], P.ids['HumanB']
    return [{'event_id': 'E_APPROACH', 'kind': 'APPROACH', 'actor_id': a, 'frame': 31, 'frame_end': 111,
             'target': {'actor_id': b}},
            {'event_id': 'E_REACT', 'kind': 'REACT', 'actor_id': b, 'frame': 113, 'frame_end': 143},
            {'event_id': 'E_LOOK', 'kind': 'GAZE', 'actor_id': b, 'frame': 144, 'frame_end': 225,
             'target': {'actor_id': a, 'bone': 'head'}, 'head_bone': 'head', 'forward_axis': head_forward_axis(P, 'HumanB')}]


def head_forward_axis(P, name, bone='head'):
    rig = bpy.data.objects[P.actor(name)['object']]
    local = rig.data.bones[bone].matrix_local.to_3x3().inverted() @ Vector((0.0, -1.0, 0.0))
    return [r6(v) for v in local.normalized()]


def demo_C(P):
    h = P.ids['Hero']
    return {
        'intent': {'title': 'Body, face and physics', 'description': 'The hero walks towards the camera and looks at it.',
                   'duration_s': 6.0, 'actors': [{'actor_id': h, 'role': 'PROTAGONIST'}],
                   'events': [{'event_id': 'E_WALK', 'kind': 'APPROACH', 'actors': [h], 'time_s': 1.0,
                               'target': [0.0, -2.2, 0.0], 'action_refs': ['Hero_Walk']},
                              {'event_id': 'E_LOOK', 'kind': 'GAZE', 'actors': [h], 'time_s': 4.0, 'target': [0.0, -6.0, 1.6]}],
                   'atmosphere': {'mood': 'calm', 'mood_preset': 'HIGH_KEY'},
                   'open_questions': ['facial performance (M7) and pendant secondary (M8) need accepted generations']},
        'paths': [{'actor': h, 'target': [0.0, -2.2, 0.0], 'departure_s': 1.0, 'event_id': 'E_WALK', 'action': 'Hero_Walk'}],
        'interactions': True,
        'staging': lambda P: [{'actor_id': h, 'location': P.path_staging(h), 'yaw_deg': 0.0}],
        'clips': lambda P: [dict(P.path_clip(h), extrapolation='HOLD')],
        'cameras': [
            {'shot': 'S01', 'location': [3.5, -6.0, 2.4], 'look_at': {'point': [0.0, -1.0, 1.0]}, 'lens': 28, 'move': 'STATIC'},
            {'shot': 'S02', 'location': [1.6, -2.4, 1.5], 'look_at': head(h), 'lens': 35, 'move': 'TRACKING',
             'params': {'look_at_target': False}, 'target': {'actor_id': h}},
            {'shot': 'S03', 'location': [0.3, -3.7, 1.6], 'look_at': head(h), 'lens': 85, 'move': 'STATIC',
             'focus': head(h), 'fstop': 2.8},
            {'shot': 'S04', 'location': [-2.2, -5.2, 1.3], 'look_at': head(h), 'lens': 35, 'move': 'DOLLY',
             'params': {'distance_m': -0.8}, 'easing': 'EASE_IN_OUT', 'target': head(h)}],
        'lighting': {'preset': 'HIGH_KEY', 'key_subject': h, 'shot': 'S03'},
        'upstream_not_run': ['facial_timing', 'secondary'],
    }


def demo_D(P):
    s, l = P.ids['Speaker'], P.ids['Listener']
    yaw_s = math.degrees(math.atan2(0.4, 1.2)) + 90.0   # -Y rotated to face the listener
    yaw_l = yaw_s - 180.0
    shots = {'schema': 'production.cinematic_shots.v1', 'shots': [
        {'shot_id': 'S01', 'type': 'WIDE', 'frame_start': 1, 'frame_end': 45, 'subjects': [s, l]},
        {'shot_id': 'S02', 'type': 'MEDIUM', 'frame_start': 46, 'frame_end': 100, 'subjects': [s]},
        {'shot_id': 'S03', 'type': 'CLOSE_UP', 'frame_start': 101, 'frame_end': 150, 'subjects': [s]},
        {'shot_id': 'S04', 'type': 'REACTION', 'frame_start': 151, 'frame_end': 195, 'subjects': [l]},
        {'shot_id': 'S05', 'type': 'WIDE', 'frame_start': 196, 'frame_end': 240, 'subjects': [s, l]}]}
    return {
        'intent': {'title': 'Editorial dialogue', 'description': 'The speaker talks to the listener, who reacts.',
                   'duration_s': 8.0, 'actors': [{'actor_id': s, 'role': 'PROTAGONIST'}, {'actor_id': l, 'role': 'SUPPORTING'}],
                   'events': [{'event_id': 'E_TALK', 'kind': 'GAZE', 'actors': [s], 'target': l, 'time_s': 1.5},
                              {'event_id': 'E_REACT', 'kind': 'REACT', 'actors': [l], 'target': s, 'time_s': 5.0,
                               'action_refs': ['Listener_React']}],
                   'atmosphere': {'mood': 'intimate', 'mood_preset': 'RIM_DRAMATIC'},
                   'camera_intentions': ['coverage: wide, medium, close-up of the speaker, reaction of the listener']},
        'shots_list': shots,
        'interactions': True,
        'staging': lambda P: [{'actor_id': s, 'location': [0.0, 0.0, 0.0], 'yaw_deg': yaw_s},
                              {'actor_id': l, 'location': [0.0, 0.0, 0.0], 'yaw_deg': yaw_l}],
        'clips': lambda P: [
            {'actor_id': s, 'action': 'Speaker_Idle', 'start_frame': 1, 'action_frame_start': 1, 'action_frame_end': 61,
             'repeat': 4.0, 'extrapolation': 'HOLD'},
            {'actor_id': l, 'action': 'Listener_Idle', 'start_frame': 1, 'action_frame_start': 1, 'action_frame_end': 61,
             'repeat': 2.5, 'extrapolation': 'HOLD'},
            {'actor_id': l, 'action': 'Listener_React', 'start_frame': 151, 'action_frame_start': 1, 'action_frame_end': 31,
             'extrapolation': 'HOLD'},
            {'actor_id': l, 'action': 'Listener_Idle', 'start_frame': 181, 'action_frame_start': 1, 'action_frame_end': 61,
             'repeat': 1.0, 'extrapolation': 'HOLD_FORWARD'}],
        'cameras': [
            # Reviewed in bpm10-e2e-5: S01 left the actors small in the cave and S02 showed the speaker's back; S02
            # is now over the listener's shoulder, on the speaker's face side.
            {'shot': 'S01', 'location': [0.0, -3.4, 1.5], 'look_at': {'point': [0.0, 0.2, 1.2]}, 'lens': 28, 'move': 'STATIC'},
            {'shot': 'S02', 'location': [1.15, 0.15, 1.6], 'look_at': head(s), 'lens': 40, 'move': 'DOLLY',
             'params': {'distance_m': 0.3}, 'easing': 'EASE_IN_OUT', 'target': head(s)},
            {'shot': 'S03', 'location': [0.5, -0.55, 1.6], 'look_at': head(s), 'lens': 85, 'move': 'STATIC',
             'focus': head(s), 'fstop': 2.0},
            {'shot': 'S04', 'location': [-0.5, -0.8, 1.6], 'look_at': head(l), 'lens': 85, 'move': 'STATIC',
             'focus': head(l), 'fstop': 2.0},
            {'shot': 'S05', 'location': [-2.0, -4.2, 1.7], 'look_at': {'point': [0.0, 0.2, 1.1]}, 'lens': 28, 'move': 'PAN',
             'params': {'angle_deg': 6.0}, 'easing': 'EASE_IN_OUT'}],
        'lighting': {'preset': 'RIM_DRAMATIC', 'key_subject': s, 'shot': 'S03'},
        'upstream_not_run': ['facial_timing'],
        # Two faced actors over 240 frames measured 3,697,920 vertex-frames (bpm10-e2e-5), above the declared
        # 3,000,000 collision capacity: every second frame is sampled, explicitly, in the tool and in the report.
        'analysis_options': {'cinematic_analyze_actor_collisions': {'step': 2}},
        'quality_options': {'staging': {'step': 2}},
    }


def demo_E(P):
    """The giver offers the lantern, the taker reaches, takes it at the first contact and both lower their arms. Reviewed
    in bpm10-e2e-5: with both arms left extended the hand-over did not read and the giver's hand stayed inside the
    lantern. The hold windows are declared as HOLD_PROP events (a held prop touches its hand by design). Timing,
    staging and cameras are PROPOSAL."""
    g, t = P.ids['Giver'], P.ids['Taker']
    handoff = 104   # the taker's reach is extended from 100; the hands meet here after the alignment below
    shots = {'schema': 'production.cinematic_shots.v1', 'shots': [
        {'shot_id': 'S01', 'type': 'WIDE', 'frame_start': 1, 'frame_end': 60, 'subjects': [g, t]},
        {'shot_id': 'S02', 'type': 'MEDIUM', 'frame_start': 61, 'frame_end': 95, 'subjects': [g]},
        {'shot_id': 'S03', 'type': 'INSERT', 'frame_start': 96, 'frame_end': 125, 'subjects': [g, t]},
        {'shot_id': 'S04', 'type': 'OVER_THE_SHOULDER', 'frame_start': 126, 'frame_end': 175, 'subjects': [t]},
        {'shot_id': 'S05', 'type': 'WIDE', 'frame_start': 176, 'frame_end': 240, 'subjects': [g, t]}]}
    return {
        'intent': {'title': 'Lantern handoff', 'description': 'The giver offers the lantern, the taker reaches and takes '
                   'it; both lower their arms, the lantern now in the taker\'s hand.',
                   'duration_s': 8.0, 'actors': [{'actor_id': g, 'role': 'PROTAGONIST'}, {'actor_id': t, 'role': 'SUPPORTING'}],
                   'events': [{'event_id': 'E_OFFER', 'kind': 'HOLD_PROP', 'actors': [g], 'target': 'Lantern', 'time_s': 1.0},
                              {'event_id': 'E_REACH', 'kind': 'REACH', 'actors': [t], 'target': g, 'time_s': 2.5,
                               'action_refs': ['Taker_Reach']},
                              {'event_id': 'E_HANDOFF', 'kind': 'HANDOFF_PROP', 'actors': [t, g], 'target': 'Lantern',
                               'time_s': (handoff - 1) / FPS}],
                   'atmosphere': {'mood': 'warm', 'mood_preset': 'NEUTRAL_THREE_POINT'}},
        'shots_list': shots,
        'interaction_rows': interactions_E,
        'exclude': ['Lantern'],
        'handoff': handoff,
        'staging': lambda P: [{'actor_id': g, 'location': [0.0, 0.0, 0.0], 'yaw_deg': 90.0},
                              {'actor_id': t, 'location': [-0.4, -0.5, 0.0], 'yaw_deg': -90.0}],
        'clips': lambda P: [
            {'actor_id': g, 'action': 'Giver_Idle', 'start_frame': 1, 'action_frame_start': 1, 'action_frame_end': 61,
             'extrapolation': 'HOLD'},
            {'actor_id': g, 'action': 'Giver_Reach', 'start_frame': 61, 'action_frame_start': 1, 'action_frame_end': 41,
             'extrapolation': 'HOLD_FORWARD'},
            {'actor_id': g, 'action': 'Giver_Lower', 'start_frame': handoff + 4, 'action_frame_start': 1,
             'action_frame_end': 33, 'extrapolation': 'HOLD_FORWARD'},
            {'actor_id': t, 'action': 'Taker_Idle', 'start_frame': 1, 'action_frame_start': 1, 'action_frame_end': 61,
             'repeat': 1.25, 'extrapolation': 'HOLD'},
            {'actor_id': t, 'action': 'Taker_Reach', 'start_frame': 76, 'action_frame_start': 1, 'action_frame_end': 41,
             'extrapolation': 'HOLD_FORWARD'},
            {'actor_id': t, 'action': 'Taker_Lower', 'start_frame': 120, 'action_frame_start': 1, 'action_frame_end': 33,
             'extrapolation': 'HOLD_FORWARD'}],
        'choreography': choreography_E,
        'cameras': [
            {'shot': 'S01', 'location': [0.0, -5.0, 1.9], 'look_at': {'point': [-0.2, -0.25, 1.0]}, 'lens': 35, 'move': 'STATIC'},
            # Reviewed in bpm10-e2e-5 (second take): aimed at the head, S02 lost the offered lantern and S04 the lantern
            # in the taker's lowered hand; S05 looked along the line between the actors and overlapped them.
            {'shot': 'S02', 'relative_to': {'actor_id': g, 'bone': 'head', 'frame': 61}, 'offset': [1.3, -1.6, -0.3],
             'look_at': {'actor_id': g, 'bone': 'chest'}, 'lens': 32, 'move': 'STATIC', 'focus': head(g), 'fstop': 2.8},
            {'shot': 'S03', 'relative_to': {'object': '@prop:lantern', 'frame': 96}, 'offset': [0.2, -1.2, 0.15],
             'look_at': {'object': '@prop:lantern'}, 'lens': 45, 'move': 'STATIC',
             'focus': {'object': '@prop:lantern'}, 'fstop': 4.0},
            {'shot': 'S04', 'relative_to': {'actor_id': g, 'bone': 'head', 'frame': 126}, 'offset': [-0.7, -0.45, 0.05],
             'look_at': {'actor_id': t, 'bone': 'spine'}, 'lens': 32, 'move': 'STATIC', 'focus': head(t), 'fstop': 2.8},
            {'shot': 'S05', 'location': [-0.3, -4.6, 1.6], 'look_at': {'point': [-0.3, -0.25, 1.0]}, 'lens': 28, 'move': 'ORBIT',
             'params': {'angle_deg': 12.0}, 'easing': 'EASE_IN_OUT', 'target': {'point': [-0.3, -0.25, 1.0]}}],
        'lighting': {'preset': 'NEUTRAL_THREE_POINT', 'key_subject': g, 'shot': 'S01'},
    }


def interactions_E(P):
    """Analysis-format events on the owned lantern copy (known only after manage_props): who holds it when, the grab
    and the hand-over. The giver's hold window runs until its lowering hand has left the lantern."""
    g, t = P.ids['Giver'], P.ids['Taker']
    h = P.spec['handoff']
    lantern = {'object': P.prop_objects['lantern']}
    hand = {'bone': 'hand.R', 'end': 'TAIL'}
    return [{'event_id': 'E_HOLD_GIVER', 'kind': 'HOLD_PROP', 'actor_id': g, 'frame': 2, 'frame_end': h + 10,
             'effector': hand, 'target': lantern, 'contact': True},
            {'event_id': 'E_GRAB', 'kind': 'REACH', 'actor_id': t, 'frame': h - 14, 'frame_end': h,
             'effector': hand, 'target': lantern, 'contact': True},
            {'event_id': 'E_HANDOFF', 'kind': 'HANDOFF_PROP', 'actor_id': t, 'frame': h, 'frame_end': h,
             'effector': hand, 'target': lantern, 'contact': True},
            {'event_id': 'E_HOLD_TAKER', 'kind': 'HOLD_PROP', 'actor_id': t, 'frame': h, 'frame_end': 240,
             'effector': hand, 'target': lantern, 'contact': True}]


def choreography_E(P, spec):
    """Measured staging of the handoff (directing, not QA): both right hands reach forward along X; the taker is moved
    in XY so its hand tail meets the giver's hand tail at the handoff frame (same rig, same reach phase, so the height
    already matches), minus a 6 cm side offset so the two hands do not overlap. The lantern starts in the giver's right
    hand grip (placement), then CHILD_OF to the giver and hand-off to the taker. Before/after recorded."""
    g, t = P.ids['Giver'], P.ids['Taker']
    giver, taker = P.actor('Giver')['object'], P.actor('Taker')['object']
    h = spec['handoff']
    rows = P.sample([2, h], lambda scene, dg, f: {
        'giver_grip': list(bone_point(dg, giver, 'hand.R', 'GRIP')), 'giver_tail': list(bone_point(dg, giver, 'hand.R')),
        'taker_tail': list(bone_point(dg, taker, 'hand.R'))})
    grip_start = rows[0]['giver_grip']
    giver_tail, taker_tail = Vector(rows[1]['giver_tail']), Vector(rows[1]['taker_tail'])
    side = Vector((0.0, 0.06, 0.0))
    correction = (giver_tail + side) - taker_tail
    before = {'frame': h, 'giver_hand_tail': [r6(v) for v in giver_tail], 'taker_hand_tail': [r6(v) for v in taker_tail],
              'distance_m': r6((giver_tail - taker_tail).length), 'height_difference_m': r6(taker_tail.z - giver_tail.z)}
    P.step('stage_actors (handoff alignment)', lambda: P.call('cinematic_stage_actors', {'actors': [
        {'actor_id': t, 'location': [r6(correction.x), r6(correction.y), 0.0], 'yaw_deg': 0.0}]}, apply=True))
    after_rows = P.sample([h], lambda scene, dg, f: {'giver_tail': list(bone_point(dg, giver, 'hand.R')),
                                                     'taker_tail': list(bone_point(dg, taker, 'hand.R'))})
    a, b = Vector(after_rows[0]['giver_tail']), Vector(after_rows[0]['taker_tail'])
    P.adjustments.append({'kind': 'E_HANDOFF_ALIGNMENT', 'tool': 'cinematic_stage_actors', 'before': before,
                          'correction_m': [r6(correction.x), r6(correction.y), 0.0],
                          'after': {'giver_hand_tail': [r6(v) for v in a], 'taker_hand_tail': [r6(v) for v in b],
                                    'distance_m': r6((a - b).length), 'height_difference_m': r6(b.z - a.z)},
                          'measured_by': 'probe-side evaluation in the candidate (capture/restore), not a QA tool'})
    P.prop_rows = [{'prop_id': 'lantern', 'object': 'Lantern',
                    'placement': {'location': [r6(v) for v in grip_start], 'rotation_deg': [0.0, 0.0, 0.0]},
                    'attach': [{'actor_id': g, 'bone': 'hand.R', 'frame_start': 2, 'frame_end': h - 1},
                               {'actor_id': t, 'bone': 'hand.R', 'frame_start': h, 'frame_end': 240}]}]


def demo_C_upstream(P):
    """Demo C on the upstream source: walk, face and pendant play exactly as M7 and M8 accepted them (offset 0; the
    M8 cache covers frames 1-91), so the cinematic lasts 3 s. Camera and timing choices are PROPOSAL."""
    h = P.ids['Hero']
    up = P.upstream
    shots = {'schema': 'production.cinematic_shots.v1', 'shots': [
        {'shot_id': 'S01', 'type': 'WIDE', 'frame_start': 1, 'frame_end': 30, 'subjects': [h]},
        {'shot_id': 'S02', 'type': 'MEDIUM', 'frame_start': 31, 'frame_end': 60, 'subjects': [h]},
        {'shot_id': 'S03', 'type': 'CLOSE_UP', 'frame_start': 61, 'frame_end': 90, 'subjects': [h]}]}
    return {
        'intent': {'title': 'Body, face and physics',
                   'description': 'The scared hero walks towards the camera glancing around; the pendant swings with '
                                  'the walk and keeps moving for a moment after he stops.',
                   'duration_s': 3.0, 'actors': [{'actor_id': h, 'role': 'PROTAGONIST'}],
                   'events': [{'event_id': 'E_WALK', 'kind': 'APPROACH', 'actors': [h], 'time_s': 0.0,
                               'target': [0.0, -2.2, 0.0], 'action_refs': ['Hero_Walk'],
                               'description': 'walks towards the camera, scared, glancing around'},
                              {'event_id': 'E_STOP', 'kind': 'ENDING', 'actors': [h], 'time_s': 2.7,
                               'description': 'stops; the pendant settles'}],
                   'required_performances': [{'actor': h, 'body_actions': ['Hero_Walk'], 'facial': True,
                                              'secondary': True}],
                   'atmosphere': {'mood': 'tense', 'mood_preset': 'NEUTRAL_THREE_POINT'}},
        'shots_list': shots,
        'exclude': up['exclude'],
        'secondary': {'actor_id': h, 'physics': up['physics'], 'offset_frames': 0, 'window': [1, 90]},
        # The M8 primary is Hero_Walk from frame 1: same clip, scale 1, offset 0 (sync enforces it).
        'clips': lambda P: [{'actor_id': h, 'action': 'Hero_Walk', 'start_frame': 1, 'action_frame_start': 1,
                             'action_frame_end': 91, 'extrapolation': 'HOLD'}],
        # The three blinks M7 was asked for: the face must be visible on them.
        'facial': {'actor_id': h, 'facial': up['facial'], 'start_frame': 1,
                   'events': [{'frame': 18, 'label': 'blink'}, {'frame': 52, 'label': 'blink'},
                              {'frame': 80, 'label': 'slow blink'}]},
        'cameras': [
            # Reviewed in bpm10-e2e-4: at 28 mm S01 left the hero small and the pendant unreadable; at 85 mm S03
            # lost the pendant from frame ~76 as the hero approached.
            {'shot': 'S01', 'location': [3.0, -5.6, 1.9], 'look_at': {'point': [0.0, -0.6, 1.0]}, 'lens': 35,
             'move': 'STATIC'},
            {'shot': 'S02', 'location': [1.3, -2.9, 1.45], 'look_at': head(h), 'lens': 35, 'move': 'TRACKING',
             'params': {'look_at_target': False}, 'target': {'actor_id': h}},
            {'shot': 'S03', 'location': [0.3, -4.4, 1.62], 'look_at': head(h), 'lens': 65, 'move': 'STATIC',
             'focus': head(h), 'fstop': 4.0}],
        'lighting': {'preset': 'NEUTRAL_THREE_POINT', 'key_subject': h, 'shot': 'S03'},
    }


def upstream_source(folder):
    """Open the upstream demo C .blend (never saved here) and list what the cinematic replaces: the pendant source,
    whose retimed copy stands in for it, and every M8-owned object (candidate, baked result, colliders)."""
    report = json.loads((folder / 'demo_c_upstream.json').read_text(encoding='utf-8'))
    if report.get('status') != 'ready':
        raise RuntimeError('M10_E2E_UPSTREAM_NOT_READY: ' + str(report.get('status')))
    blend = Path(report['source_blend'])
    bpy.ops.wm.open_mainfile(filepath=str(blend), load_ui=False, use_scripts=False)
    scene = bpy.context.scene
    hero = report['hero']
    pendant = hero['secondary'][0]['object']
    exclude = [pendant] + sorted(o.name for o in scene.objects if o.get('production_m8_owned') and o.name != pendant)
    manifest = {'blend': str(blend), 'sha256': sha256_file(blend), 'bytes': blend.stat().st_size, 'scene': scene.name,
                'frame_range': [scene.frame_start, scene.frame_end], 'duration_source': 'UPSTREAM (M7 + M8 accepted)',
                'actors': [hero]}
    return manifest, {'folder': str(folder), 'facial': report['facial'], 'physics': report['physics'],
                      'exclude': exclude, 'reviews': {k: v.get('decision') for k, v in (report.get('reviews') or {}).items()}}


DEMOS = {'A_creature_reveal': demo_A, 'B_two_character': demo_B, 'C_body_face_physics': demo_C,
         'D_editorial': demo_D, 'E_interaction': demo_E}


# --------------------------------------------------------------------------------------------- pipeline stages

def run_pipeline(P):
    from blender_mcp.production.cinematic import store
    manifest = P.manifest
    # 1. register_actor with the manifest's explicit bones (never inferred from names)
    for actor in manifest['actors']:
        options = {'role': actor['role'], 'bones': registered_bones(actor),
                   'body_actions': [a['name'] for a in actor['actions']]}
        if P.upstream:
            options.update(facial=[P.upstream['facial']], secondary=[P.upstream['physics']])
        result = P.step('register_actor ' + actor['name'], lambda actor=actor, options=options: P.dispatch(
            'cinematic_register_actor', object_name=actor['object'], options=options))
        P.ids[actor['name']] = result['actor_id']
        P.records[actor['name']] = {'actor_id': result['actor_id'], 'warnings': [w.get('code') for w in result['warnings']],
                                    'body_actions': [{k: row.get(k) for k in ('action', 'digest', 'root_motion')}
                                                     for row in (result['actor'].get('body_actions') or [])]}
    spec = (demo_C_upstream if P.upstream else DEMOS[P.kind])(P)
    P.spec = spec
    P.spec_summary = {k: v for k, v in spec.items() if not callable(v) and k not in ('cameras',)}
    # 2. brief -> beats -> shots
    brief_result = P.step('create_brief', lambda: P.dispatch('cinematic_create_brief', brief=spec['intent']))
    if not brief_result['ready']:
        raise Abort('BRIEF_NOT_READY: ' + json.dumps(brief_result['summary']))
    brief = brief_result['brief']
    beats = P.step('plan_beats', lambda: P.dispatch('cinematic_plan_beats', brief=brief))
    if spec.get('shots_list'):
        shot_options = {'beats': beats, 'shots': spec['shots_list']['shots']}
    else:
        shot_options = {'beats': beats}
        if spec.get('shots_patch'):
            shot_options['shots'] = spec['shots_patch']
    shots = P.step('plan_shots', lambda: P.dispatch('cinematic_plan_shots', brief=brief, options=shot_options))
    P.shots = shots['shots']
    paths = None
    if spec.get('paths'):
        paths = P.step('plan_actor_paths', lambda: P.dispatch('cinematic_plan_actor_paths', brief=brief,
                                                              options={'beats': beats, 'paths': spec['paths']}))
        P.paths = {row['actor_id']: row for row in paths['paths']}
    interactions = None
    if spec.get('interactions'):
        options = {'beats': beats}
        if paths:
            options['paths'] = paths
        interactions = P.step('plan_interaction', lambda: P.dispatch('cinematic_plan_interaction', brief=brief,
                                                                     options=options))
    # 3. candidate (originals of moved props excluded at creation: they cannot leave _SRC afterwards)
    created = P.step('create_candidate', lambda: P.dispatch(
        'cinematic_create_candidate', brief=brief, apply=True,
        options={'beats': beats, 'shots': shots, 'exclude_objects': spec.get('exclude') or []}))
    P.ref, P.scene_name = created['cinematic'], created['scene']
    _, plan, _ = store.load(P.storage, P.ref)
    P.plan_source_fingerprint = plan['source']['fingerprint']
    # 4. accepted M8 secondary (its gate runs before M10 stages or synchronizes the actor), staging, clips, props,
    # accepted M7 face
    if spec.get('secondary'):
        P.step('integrate_secondary', lambda: P.call('cinematic_integrate_secondary', spec['secondary'], apply=True))
    if spec.get('staging'):
        P.step('stage_actors', lambda: P.call('cinematic_stage_actors', {'actors': spec['staging'](P)}, apply=True))
    sync = P.step('sync_actions', lambda: P.call('cinematic_sync_actions', {'clips': spec['clips'](P)}, apply=True))
    P.results['sync_warnings'] = [{k: w.get(k) for k in ('code', 'actor_id', 'frames', 'gap')} for w in sync.get('warnings') or []]
    if spec.get('choreography'):
        spec['choreography'](P, spec)
    if getattr(P, 'prop_rows', None):
        P.step('manage_props', lambda: P.call('cinematic_manage_props', {'props': P.prop_rows}, apply=True))
        _, _, state = store.load(P.storage, P.ref)
        P.prop_objects = {prop_id: row['copy'] for prop_id, row in (state.get('props') or {}).items()}
    if spec.get('facial'):
        P.step('integrate_facial', lambda: P.call('cinematic_integrate_facial', spec['facial'], apply=True))
    # 5. cameras per shot, cuts, focus
    names = {aid: name for name, aid in P.ids.items()}
    P.camera_placements = []
    for row in spec['cameras']:
        shot = next(s for s in P.shots if s['shot_id'] == row['shot'])
        camera_id = shot['camera_id']
        location = row.get('location')
        if row.get('relative_to'):
            # Directing measurement, not QA: the bone (or object) where the shot starts, plus the written offset.
            rel = row['relative_to']
            if rel.get('object'):
                obj_name = resolve_target(P, {'object': rel['object']})['object']
                point = P.sample([rel['frame']], lambda scene, dg, f, o=obj_name: list(
                    bpy.data.objects[o].evaluated_get(dg).matrix_world.translation))[0]
            else:
                obj_name = P.actor(names[rel['actor_id']])['object']
                point = P.sample([rel['frame']], lambda scene, dg, f, o=obj_name, b=rel['bone']: list(bone_point(dg, o, b, 'GRIP')))[0]
            location = [r6(point[i] + row['offset'][i]) for i in range(3)]
            P.camera_placements.append({'shot': row['shot'], 'measured_point': [r6(v) for v in point],
                                        'offset': row['offset'], 'location': location})
        options = {'camera_id': camera_id, 'shot_id': row['shot'], 'lens_mm': row['lens'], 'location': location,
                   'look_at': resolve_target(P, row['look_at']), 'clip_start': 0.05}
        P.step('create_camera ' + camera_id, lambda options=options: P.call('cinematic_create_camera', options, apply=True))
        P.step('configure_camera ' + camera_id, lambda camera_id=camera_id, row=row: P.call(
            'cinematic_configure_camera', {'camera_id': camera_id, 'dof': {'use_dof': bool(row.get('focus')),
                                                                          'aperture_fstop': row.get('fstop', 5.6)}},
            apply=True))
        animate = {'camera_id': camera_id, 'shot_id': row['shot'], 'move': row['move']}
        for key in ('params', 'easing'):
            if row.get(key):
                animate[key] = row[key]
        if row.get('target'):
            animate['target'] = resolve_target(P, row['target'])
        P.step('animate_camera ' + camera_id + ' ' + row['move'],
               lambda animate=animate: P.call('cinematic_animate_camera', animate, apply=True))
    P.step('create_cuts', lambda: P.call('cinematic_create_cuts', {'from_shots': True}, apply=True))
    for row in spec['cameras']:
        if row.get('focus'):
            shot = next(s for s in P.shots if s['shot_id'] == row['shot'])
            P.step('plan_focus ' + shot['camera_id'], lambda shot=shot, row=row: P.call('cinematic_plan_focus', {
                'camera_id': shot['camera_id'], 'shot_id': shot['shot_id'], 'aperture_fstop': row.get('fstop', 2.8),
                'focus': [{'frame': shot['frame_start'], 'target': resolve_target(P, row['focus'])}]}, apply=True))
    # 6. lighting
    lighting = spec['lighting']
    planned = P.step('plan_lighting', lambda: P.call('cinematic_plan_lighting', {
        'preset': lighting['preset'], 'key_subject': lighting['key_subject'], 'shot': lighting['shot']}))
    P.step('setup_lighting', lambda: P.call('cinematic_setup_lighting', {'plan': planned['plan']}, apply=True))
    # 7. timeline: interactions, then finalize -> ASSEMBLED
    if spec.get('interaction_rows'):  # built here: they may name owned copies created by manage_props
        interactions = spec['interaction_rows'](P)
        P.results['interaction_rows'] = interactions
    if interactions is not None:
        P.step('manage_timeline (interactions)', lambda: P.call('cinematic_manage_timeline',
                                                                {'interactions': interactions}, apply=True))
    finalize(P)
    # 8. QA, previs, preview, quality report
    run_analyses(P, 'analyses')
    adjust_collisions(P)
    P.step('validate_lighting', lambda: P.call('cinematic_validate_lighting', {}, apply=True), critical=False)
    P.step('generate_previs', lambda: P.call('cinematic_generate_previs', {'size': PREVIEW_SIZE}))
    P.step('render_preview', lambda: P.call('cinematic_render_preview', {'size': PREVIEW_SIZE}))
    quality_options = {'section_options': spec['quality_options']} if spec.get('quality_options') else None
    P.step('quality_report', lambda: P.call('cinematic_quality_report', quality_options))


def resolve_target(P, value):
    if isinstance(value, dict) and isinstance(value.get('object'), str) and value['object'].startswith('@prop:'):
        return dict(value, object=P.prop_objects[value['object'][6:]])
    return value


def finalize(P, label='manage_timeline (finalize)'):
    result = P.step(label, lambda: P.call('cinematic_manage_timeline', {'finalize': True}, apply=True))
    P.results['assembled'] = {k: (result.get(k) if not isinstance(result.get(k), dict) else None)
                              for k in ('status', 'state')}
    return result


def run_analyses(P, label):
    rows = {}
    options = (getattr(P, 'spec', None) or {}).get('analysis_options') or {}
    for name in ANALYSES:
        result = P.step(label + ' ' + name[10:], lambda name=name: P.call(name, options.get(name)), critical=False)
        if result is not None:
            rows[name[10:]] = {'passed': result.get('passed'), 'status': result.get('status'),
                               'state_restored': result.get('state_restored'), 'metrics': result.get('metrics'),
                               'findings': finding_codes(result), 'finding_rows': finding_rows(result)}
    P.results[label + '_summary'] = rows
    return rows


def adjust_collisions(P):
    """A measured ACCIDENTAL_COLLISION is a staging defect: move the second actor of the worst pair away along the
    pair axis by the measured depth plus 5 cm (PROPOSAL), re-finalize and measure again. Before/after recorded."""
    collisions = P.results.get('analyses analyze_actor_collisions') or {}
    accidental = [f for f in collisions.get('findings') or [] if f.get('code') == 'ACCIDENTAL_COLLISION']
    if not accidental:
        return
    worst = max(accidental, key=lambda f: ((f.get('measured') or {}).get('max_depth_m') or 0.0))
    pair = worst.get('actor') or (worst.get('measured') or {}).get('pair')
    names = {aid: name for name, aid in P.ids.items()}
    ids = [p for p in (pair if isinstance(pair, list) else str(pair).split('|')) if p in names]
    if len(ids) != 2:
        P.adjustments.append({'kind': 'COLLISION', 'skipped': 'pair not resolvable: ' + str(pair)[:200]})
        return
    depth = float((worst.get('measured') or {}).get('max_depth_m') or 0.0)
    first, second = P.actor(names[ids[0]])['object'], P.actor(names[ids[1]])['object']
    rows = P.sample([1], lambda scene, dg, f: [list(bpy.data.objects[first].evaluated_get(dg).matrix_world.translation),
                                              list(bpy.data.objects[second].evaluated_get(dg).matrix_world.translation)])
    a, b = Vector(rows[0][0]), Vector(rows[0][1])
    axis = Vector((b.x - a.x, b.y - a.y, 0.0)).normalized()
    move = axis * (depth + 0.05)
    before = {'finding': {k: worst.get(k) for k in ('code', 'severity', 'frames', 'measured')},
              'metrics': collisions.get('metrics')}
    P.step('stage_actors (collision fix)', lambda: P.call('cinematic_stage_actors', {'actors': [
        {'actor_id': ids[1], 'location': [r6(move.x), r6(move.y), 0.0], 'yaw_deg': 0.0}]}, apply=True))
    finalize(P, 'manage_timeline (finalize after collision fix)')
    options = ((getattr(P, 'spec', None) or {}).get('analysis_options') or {}).get('cinematic_analyze_actor_collisions')
    after = P.step('analyze_actor_collisions (after fix)', lambda: P.call('cinematic_analyze_actor_collisions', options),
                   critical=False)
    P.adjustments.append({'kind': 'COLLISION', 'tool': 'cinematic_stage_actors', 'actor_moved': names[ids[1]],
                          'move_m': [r6(move.x), r6(move.y), 0.0], 'before': before,
                          'after': {'passed': (after or {}).get('passed'), 'metrics': (after or {}).get('metrics'),
                                    'findings': finding_codes(after)}})
    run_analyses(P, 'analyses after collision fix')


def path_clip(P, aid):
    clip = P.paths[aid]['clip']
    return {'actor_id': aid, 'action': clip['action'], 'start_frame': clip['start_frame'],
            'action_frame_start': clip['action_frame_start'], 'action_frame_end': clip['action_frame_end'],
            'repeat': clip['repeat'], 'scale': clip['scale']}


def path_staging(P, aid):
    return [r6(v) for v in P.paths[aid]['staging']['delta_location_m']]


Pipeline.path_clip = path_clip
Pipeline.path_staging = path_staging


# ------------------------------------------------------------------------------------------- real visual review

def wait_review(P, label, artifact):
    request = {'label': label, 'demo': P.kind, 'cinematic': P.ref, 'artifact': artifact,
               'feedback_path': str(P.out / ('review-feedback-' + label + '.json')),
               'instruction': 'Watch the real preview (player at 1 and 0.25, every shot) and write the feedback file: '
                              'the cinematic_review document, plus an optional "refine" object (cinematic_refine '
                              'options) when the decision is REFINE. The probe never writes a review.'}
    path = P.out / ('review-request-' + label + '.json')
    path.write_text(json.dumps(request, indent=1, default=str), encoding='utf-8')
    print('REVIEW_REQUEST ' + str(path), flush=True)
    feedback = Path(request['feedback_path'])
    deadline = time.time() + P.review_timeout
    while not feedback.exists():
        if time.time() > deadline:
            raise Abort('REVIEW_TIMEOUT: no visual review for ' + label + '; nothing is accepted')
        time.sleep(2)
    time.sleep(0.5)
    return json.loads(feedback.read_text(encoding='utf-8-sig'))


def review_and_decide(P):
    """The reviewer's decision drives the tools; the tools keep their own gates (stale review, cycle limit, quality
    report of the current candidate)."""
    preview = P.results.get('render_preview') or {}
    artifact = {k: preview.get(k) for k in ('player', 'output_dir', 'review_template')}
    artifact.update(quality_report=(P.results.get('quality_report') or {}).get('report_md'),
                    storyboard=(P.results.get('generate_previs') or {}).get('storyboard'))
    rounds = []
    P.summary['review'] = {'rounds': rounds}
    for index in range(1, 8):
        label = 'r' + str(index)
        feedback = wait_review(P, label, artifact)
        refine = feedback.pop('refine', None)
        decision = feedback.get('decision')
        reviewed = P.step('cinematic_review ' + label, lambda: P.call('cinematic_review', {'review': feedback}))
        row = {'label': label, 'decision': decision, 'status': reviewed.get('status'),
               'refinement': reviewed.get('refinement')}
        rounds.append(row)
        if decision == 'ACCEPT':
            if reviewed.get('status') == 'refined':  # a kept refinement changed the candidate: report it again
                # The EEVEE lighting validation is bound to the candidate fingerprint (LIGHTING_VALIDATION_STALE
                # after any change, measured on demo A): validate again before the report.
                P.step('validate_lighting (after refinement)', lambda: P.call('cinematic_validate_lighting', {}, apply=True))
                quality_options = {'section_options': P.spec['quality_options']} if P.spec.get('quality_options') else None
                P.step('quality_report (after refinement)', lambda: P.call('cinematic_quality_report', quality_options))
            accepted = P.step('cinematic_accept', lambda: P.call('cinematic_accept', apply=True))
            copy = P.out / ('accepted_' + P.kind + '.blend')
            bpy.ops.wm.save_as_mainfile(filepath=str(copy), copy=True)
            P.summary['review'].update(decision='ACCEPT', acceptance=accepted.get('acceptance'),
                                       accepted_copy={'path': str(copy), 'sha256': sha256_file(copy)})
            return
        if decision == 'REFINE' and refine:
            refined = P.step('cinematic_refine ' + label, lambda: P.call('cinematic_refine', refine, apply=True),
                             critical=False)
            row['refine'] = {'request': refine, 'status': (refined or {}).get('status'),
                             'refinement': (refined or {}).get('refinement'),
                             'remaining_cycles': (refined or {}).get('remaining_cycles')}
            if refined and refined.get('status') == 'visual_review_required':
                artifact = dict(refined['preview'], quality_report=None)
            continue  # reverted or refused: the unchanged candidate is reviewed again
        P.step('cinematic_reject (review)', lambda: P.call('cinematic_reject', {'reason': 'REVIEW_' + str(decision)},
                                                           apply=True))
        P.summary['review']['decision'] = decision
        return
    raise Abort('REVIEW_ROUNDS_EXHAUSTED')


# --------------------------------------------------------------------------------------------------- summary

def write_summary(P):
    summary = getattr(P, 'summary', {})
    summary['stages'] = P.stages
    (P.out / ('m10_e2e_' + P.kind + ('_reject' if P.mode == 'reject' else '') + '.json')).write_text(
        json.dumps(summary, indent=1, default=str), encoding='utf-8')


def verification(P, before, touched_objects, rejected=None):
    scene = bpy.data.scenes[P.manifest['scene']]
    after_actions = source_actions()
    after = source_snapshot(scene)
    unexpected, recorded = compare_snapshots(before['snapshot'], after, touched_objects)
    status = None
    if P.ref:
        from blender_mcp.production.cinematic import store
        path, plan, state = store.load(P.storage, P.ref)
        if state.get('state') != 'REJECTED':
            observed = store.status(P.bridge, plan, state)
            status = {'source_equals_ledger': observed['source']['fingerprint'] == (state.get('fingerprints') or {}).get('source'),
                      'candidate_equals_ledger': (observed.get('candidate') or {}).get('fingerprint')
                      == (state.get('fingerprints') or {}).get('candidate'),
                      'source_equals_plan': observed['source']['fingerprint'] == plan['source']['fingerprint']}
        else:
            status = {'source_equals_plan': P.runtime['cm_source_fingerprint'](scene)['fingerprint']
                      == plan['source']['fingerprint']}
    blend = Path(P.manifest['blend'])
    result = {
        'source_actions_same_digest': after_actions == before['actions'],
        'source_actions_changed': sorted(k for k in before['actions'] if after_actions.get(k) != before['actions'][k]),
        'source_actions_count': len(before['actions']),
        'blend_on_disk_sha256_unchanged': sha256_file(blend) == P.manifest['sha256'],
        'blend_sha256': P.manifest['sha256'],
        'source_scene_unexpected_differences': unexpected,
        'source_scene_recorded_differences_on_touched_actors': recorded,
        'tool_fingerprints': status,
        'pose_channels_restored': not pose_differences(before['snapshot'], after),
        'pose_channel_differences': pose_differences(before['snapshot'], after)[:20],
    }
    leftovers = sorted(set(o.name for o in bpy.data.objects if o.get('production_m10_owner'))
                       | set(s.name for s in bpy.data.scenes if s.get('production_m10_owner'))
                       | set(a.name for a in bpy.data.actions if a.get('production_m10_owner')))
    result['m10_datablocks_remaining'] = len(leftovers)
    if rejected is not None:
        result['m10_datablocks_remaining_names'] = leftovers[:20]
    return result


def main():
    args = sys.argv[sys.argv.index('--') + 1:]
    out, kind = Path(args[0]), args[1]
    mode = args[2] if len(args) > 2 else 'full'
    extra = dict(a.split('=', 1) for a in args[3:] if '=' in a)
    upstream = Path(extra['source']) if extra.get('source') else None
    if kind not in DEMOS or mode not in ('full', 'reject', 'technical') or set(extra) - {'source', 'review_timeout'}:
        raise SystemExit('usage: -- <out_dir> <demo> [full|reject|technical] [source=<dir>] [review_timeout=<s>]')
    if upstream is not None and kind != 'C_body_face_physics':
        raise SystemExit('M10_E2E_SOURCE_ONLY_FOR_DEMO_C')
    if out.exists() and any(out.iterdir()):
        raise SystemExit('M10_E2E_OUTPUT_NOT_EMPTY: ' + str(out))
    out.mkdir(parents=True, exist_ok=True)
    began = time.perf_counter()
    bridge = LocalBridge()
    # The upstream store holds the accepted M7/M8 generations the cinematic integrates.
    storage = Storage((upstream or out) / 'store')
    P = Pipeline(out, kind, mode, None, bridge, storage)
    P.upstream = None
    P.review_timeout = float(extra.get('review_timeout', 10800))
    P.summary = {'suite': 'm10_e2e', 'demo': kind, 'mode': mode, 'blender': bpy.app.version_string, 'errors': [],
                 'source': 'upstream' if upstream else 'fixture'}
    try:
        from blender_mcp.production.cinematic.runtime import COMMON
        # Same prelude as every payload (compose_code): the shared runtime expects json in its namespace.
        P.runtime_source = 'import json\n' + COMMON + '\n'
        P.runtime = {}
        exec(compile(P.runtime_source, 'cm_runtime', 'exec'), P.runtime)
        if upstream is None:
            manifest = fixtures.build_demo(kind, out / 'fixture')
        else:
            manifest, P.upstream = upstream_source(upstream)
            P.summary['upstream'] = P.upstream
        P.manifest = manifest
        P.summary['fixture'] = {'blend': manifest['blend'], 'sha256': manifest['sha256'], 'bytes': manifest['bytes'],
                                'scene': manifest['scene'], 'frame_range': manifest['frame_range'],
                                'duration_source': manifest['duration_source']}
        scene = bpy.data.scenes[manifest['scene']]
        before = {'actions': source_actions(), 'snapshot': source_snapshot(scene)}
        if upstream is None:  # the upstream run registered its own .blend in its store
            register_open_fixture(bridge, storage, out)
        try:
            run_pipeline(P)
            if mode == 'full':
                review_and_decide(P)
        except Abort as exc:
            P.summary['errors'].append({'abort': str(exc)})
        from blender_mcp.production.cinematic import store
        touched, owned, state = [], {}, {}
        if P.ref:
            path, plan, state = store.load(storage, P.ref)
            touched = (state.get('assembly') or {}).get('touched') or []
            owned = (state.get('assembly') or {}).get('owned') or {}
        rejected = None
        if mode == 'reject' and P.ref:
            rejected = P.step('reject', lambda: P.call('cinematic_reject', {'reason': 'E2E_REJECT_TEST'}, apply=True),
                              critical=False)
            path, plan, state = store.load(storage, P.ref)
        touched_objects = sorted({row.get('object') or row.get('id_name') for row in touched})
        quality = P.results.get('quality_report') or {}
        preview = P.results.get('render_preview') or {}
        previs = P.results.get('generate_previs') or {}
        lighting = P.results.get('validate_lighting') or {}
        sections = {s['section']: {k: s.get(k) for k in ('status', 'mandatory', 'provider', 'error_count', 'reason')}
                    for s in quality.get('sections') or []}
        for section, reason in NOT_RUN_UPSTREAM.items():
            if not P.upstream and section in ((getattr(P, 'spec', None) or {}).get('upstream_not_run') or []):
                sections.setdefault(section, {})['upstream'] = reason
        P.summary.update({
            'cinematic': P.ref, 'candidate_scene': P.scene_name, 'state': state.get('state'),
            'actors': P.records, 'shots': [{k: s.get(k) for k in ('shot_id', 'camera_id', 'type', 'frame_start', 'frame_end')}
                                           for s in getattr(P, 'shots', [])],
            'artifacts': {'preview_html': preview.get('player'), 'preview_dir': preview.get('output_dir'),
                          'frames_dir': (str(Path(preview['output_dir']) / 'frames') if preview.get('output_dir') else None),
                          'review_template': preview.get('review_template'),
                          'storyboard': previs.get('storyboard'), 'previs_dir': previs.get('output_dir'),
                          'quality_report': quality.get('report'), 'quality_report_md': quality.get('report_md'),
                          'lighting_report': lighting.get('report')},
            'quality_report': {'passed': quality.get('passed'), 'sections': sections,
                               'failing_sections': quality.get('failing_sections'),
                               'mandatory_not_passed': quality.get('mandatory_not_passed'),
                               'technical_findings': (quality.get('technical_findings') or [])[:25]},
            'analyses': P.results.get('analyses_summary'),
            'analyses_after_adjustment': P.results.get('analyses after collision fix_summary'),
            'sync_warnings': P.results.get('sync_warnings'),
            'adjustments': P.adjustments,
            'camera_placements': getattr(P, 'camera_placements', None),
            'carry': {aid: (entry.get('carry') or None) for aid, entry in (state.get('actors') or {}).items()},
            'render': {'preview': {k: preview.get(k) for k in ('temporal_complete', 'frames', 'cuts_respected',
                                                                'state_restored', 'render_backend', 'metrics', 'segments')},
                       'previs': {k: previs.get(k) for k in ('metrics', 'render_backend', 'warnings')},
                       'lighting_validation': {k: lighting.get(k) for k in ('status', 'passed', 'metrics', 'timing',
                                                                             'render_backend', 'error_count', 'warning_count')}},
            'touched': {'count': len(touched), 'kinds': sorted({row.get('kind') for row in touched}),
                        'objects': touched_objects},
            'owned': {k: len(v) for k, v in owned.items()},
            'owned_names': {k: v[:12] for k, v in owned.items()},
            'metrics': {'total_seconds': round(time.perf_counter() - began, 3), 'bridge_calls': bridge.calls,
                        'payload_bytes': bridge.bytes, 'max_payload_bytes': max((c[0] for c in bridge.log), default=0),
                        'max_payload_ast_nodes': max((c[1] for c in bridge.log), default=0),
                        'bridge_seconds': round(bridge.seconds, 3),
                        'payload_limit_bytes': 140000, 'payload_limit_ast_nodes': 14000},
        })
        if rejected is not None:
            P.summary['reject'] = {k: rejected.get(k) for k in ('status', 'source_restored', 'removed', 'undo_failed')}
        P.summary['verification'] = verification(P, before, touched_objects, rejected)
    except Exception as exc:
        P.summary['errors'].append({'error': repr(exc), 'traceback': traceback.format_exc()})
        traceback.print_exc()
    P.summary['stage_failures'] = [s['stage'] for s in P.stages if not s['ok']]
    write_summary(P)
    print('SUMMARY ' + json.dumps({'demo': kind, 'mode': mode, 'state': P.summary.get('state'),
                                   'quality_passed': (P.summary.get('quality_report') or {}).get('passed'),
                                   'stage_failures': P.summary['stage_failures'], 'errors': len(P.summary['errors'])}),
          flush=True)


main()
