"""M10 negative scenarios that only had unit coverage, now in real Blender (own background process, two controlled
fixture humanoids, every call through the real tool dispatch and the safe-mode payload builder):

- camera clipping actor: the S01 camera stands 0.3 m in front of Reacher with clip_start 0.5 m -> NEAR_CLIP_CUT
  (composition) and CAMERA_CLIPPING (visibility) on S01; the control shot S02 (camera about 7 m away, clip_start
  0.1 m) has neither;
- unreachable interaction target: a REACH toward Distant, 3 m away, against the registered (measured) reach ->
  OUT_OF_REACH and status warning; a point 0.3 m in front of Reacher -> REACHABLE; contact QA of that REACH ->
  NO_CONTACT and TARGET_UNREACHED with the measured minimum distance (never zero, never UNKNOWN);
- every analysis restores the candidate; reject restores the source; the fixture file on disk is unchanged.

    blender -b --factory-startup --disable-autoexec --python tests/blender/m10_negative_probe.py -- <out_dir>
"""
from __future__ import annotations

import hashlib
import json
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

from blender_mcp.production.common.bridge import build_code  # noqa: E402
from blender_mcp.production.common.storage import Storage  # noqa: E402
from m31_fixture_support import register_open_fixture  # noqa: E402
import m10_cinematic_fixtures as FX  # noqa: E402

ROLE_BONES = (('root', 'root'), ('pelvis', 'pelvis'), ('head', 'head'), ('hand_left', ('hands', 'L')),
              ('hand_right', ('hands', 'R')), ('foot_left', ('feet', 'L')), ('foot_right', ('feet', 'R')))
REPORT = {'suite': 'm10_negative', 'blender': bpy.app.version_string, 'checks': [], 'errors': [], 'measured': {}}
OUT = None


class LocalBridge:
    """Same contract as the real bridge: every payload goes through build_code (safe mode validator)."""

    def __init__(self):
        self.calls = 0
        self.max_bytes = 0

    def run(self, body, params=None):
        code = build_code(body, params)
        self.calls += 1
        self.max_bytes = max(self.max_bytes, len(code.encode('utf-8')))
        ns = {}
        exec(code.rsplit('print(', 1)[0], ns)
        return ns['result']


def check(name, condition, data=None):
    REPORT['checks'].append({'name': name, 'passed': bool(condition), 'data': data})
    print(('PASS ' if condition else 'FAIL ') + name, flush=True)
    (OUT / 'm10_negative.json').write_text(json.dumps(REPORT, indent=1, default=str), encoding='utf-8')


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def bones_of(actor):
    return {role: (actor['bones'][ref] if isinstance(ref, str) else actor['bones'][ref[0]][ref[1]]) for role, ref in ROLE_BONES}


def build_scene(out):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.render.fps, scene.render.fps_base = FX.FPS, 1.0
    scene.frame_start, scene.frame_end = 1, 60
    reacher = FX.build_humanoid('Reacher', (0.0, 0.0, 0.0), 90.0, 'R')
    distant = FX.build_humanoid('Distant', (3.0, 0.0, 0.0), 90.0, 'R', color=(.65, .30, .12))
    path = out / 'm10_negative_fixture.blend'
    bpy.ops.wm.save_as_mainfile(filepath=str(path))
    return reacher, distant, path


def codes(report, code):
    return [f for f in report.get('findings') or [] if f['code'] == code]


def main():
    global OUT
    OUT = Path(sys.argv[sys.argv.index('--') + 1])
    OUT.mkdir(parents=True, exist_ok=True)
    try:
        from blender_mcp.production.cinematic.service import dispatch
        reacher, distant, blend = build_scene(OUT)
        bridge = LocalBridge()
        storage = Storage(OUT / 'store')
        register_open_fixture(bridge, storage, OUT)
        disk_before = sha256(blend)
        counter = [0]

        def call(name, **kw):
            counter[0] += 1
            began = time.perf_counter()
            result = dispatch(name, bridge, storage, 'neg-%03d' % counter[0], **kw)
            REPORT['measured'].setdefault('elapsed_s', []).append([name, round(time.perf_counter() - began, 4)])
            return result

        ids = {}
        for actor, role in ((reacher, 'PROTAGONIST'), (distant, 'SUPPORTING')):
            ids[actor['name']] = call('cinematic_register_actor', object_name=actor['object'], options={
                'role': role, 'bones': bones_of(actor), 'body_actions': [a['name'] for a in actor['actions']]})['actor_id']
        r, d = ids['Reacher'], ids['Distant']
        intent = {'title': 'Negative probe', 'description': 'a clipping camera and a reach that cannot arrive',
                  'duration_s': 2.0, 'actors': [{'actor_id': r, 'role': 'PROTAGONIST'}, {'actor_id': d, 'role': 'SUPPORTING'}],
                  'events': [{'event_id': 'E_REACH', 'kind': 'REACH', 'actors': [r], 'target': d, 'time_s': 1.0}],
                  'atmosphere': {'mood': 'neutral', 'mood_preset': 'NEUTRAL_THREE_POINT'}}
        brief = call('cinematic_create_brief', brief=intent)['brief']
        beats = call('cinematic_plan_beats', brief=brief)
        shots = call('cinematic_plan_shots', brief=brief, options={'beats': beats, 'shots': [
            {'shot_id': 'S01', 'type': 'CLOSE_UP', 'frame_start': 1, 'frame_end': 30, 'subjects': [r]},
            {'shot_id': 'S02', 'type': 'WIDE', 'frame_start': 31, 'frame_end': 60, 'subjects': [r, d]}]})

        # ---- unreachable interaction target (planning: registered reach against the measured scene) ----
        # Reacher faces -Y (fixture forward axis), so a point 0.3 m along -Y at chest height is in front of it.
        planned = call('cinematic_plan_interaction', brief=brief, options={'beats': beats, 'interactions': [
            {'event_id': 'E_REACH', 'kind': 'REACH', 'actors': [r], 'spatial_target': d, 'effector': 'hand.R'},
            {'kind': 'REACH', 'actors': [r], 'frame': 15, 'effector': 'hand.R',
             'spatial_target': {'type': 'POINT', 'point': [0.0, -0.3, 1.2]}}]})
        far, near = planned['interactions'][0]['reachability'], planned['interactions'][1]['reachability']
        REPORT['measured']['reachability'] = {'distant': far, 'point': near}
        check('unreachable target: REACH toward an actor 3 m away -> OUT_OF_REACH against the registered reach '
              '(numeric, from the registry) and the plan status is warning',
              far.get('status') == 'OUT_OF_REACH' and isinstance(far.get('reach_m'), float)
              and far.get('reach_source') == 'registry interaction_capabilities.reach_m'
              and isinstance(far.get('surface_distance_m'), float) and far['surface_distance_m'] > far['reach_m']
              and planned['status'] == 'warning', far)
        check('control: a point 0.3 m in front of Reacher -> REACHABLE with the same reach',
              near.get('status') == 'REACHABLE' and near.get('reach_m') == far.get('reach_m') and near['distance_m'] <= near['reach_m'],
              near)
        check('interaction planning changed nothing in Blender', planned.get('blender_changed') is False)

        # ---- candidate, cameras, cuts ----
        created = call('cinematic_create_candidate', brief=brief, apply=True, options={'beats': beats, 'shots': shots})
        ref = created['cinematic']
        cams = {s['shot_id']: s['camera_id'] for s in shots['shots']}
        call('cinematic_create_camera', cinematic=ref, apply=True, options={
            'camera_id': cams['S01'], 'shot_id': 'S01', 'lens_mm': 35, 'clip_start': 0.5,
            'location': [0.0, -0.3, 1.2], 'look_at': {'point': [0.0, 0.0, 1.2]}})
        call('cinematic_create_camera', cinematic=ref, apply=True, options={
            'camera_id': cams['S02'], 'shot_id': 'S02', 'lens_mm': 35,
            'location': [1.5, -7.0, 1.4], 'look_at': {'point': [1.5, 0.0, 1.0]}})
        call('cinematic_create_cuts', cinematic=ref, apply=True, options={'from_shots': True})
        call('cinematic_manage_timeline', cinematic=ref, apply=True, options={'finalize': True})

        # ---- camera clipping actor ----
        comp = call('cinematic_analyze_composition', cinematic=ref)
        near_cut = codes(comp, 'NEAR_CLIP_CUT')
        REPORT['measured']['near_clip_cut'] = near_cut
        check('camera clipping actor: S01 camera 0.3 m in front of Reacher with clip_start 0.5 m -> NEAR_CLIP_CUT on S01',
              any(f.get('shot_id') == 'S01' and f.get('actor_id') == r for f in near_cut), near_cut)
        check('control: S02 camera about 7 m away with clip_start 0.1 m -> no NEAR_CLIP_CUT on S02',
              not any(f.get('shot_id') == 'S02' for f in near_cut), [f.get('shot_id') for f in near_cut])
        check('composition restored the candidate exactly', comp.get('state_restored') is True, comp.get('restoration'))
        vis = call('cinematic_analyze_visibility', cinematic=ref)
        clipping = codes(vis, 'CAMERA_CLIPPING')
        REPORT['measured']['camera_clipping'] = clipping
        check('camera clipping actor: visibility -> CAMERA_CLIPPING for Reacher on S01',
              any(f.get('shot_id') == 'S01' and f.get('actor_id') == r for f in clipping), clipping)
        check('control: no CAMERA_CLIPPING on S02', not any(f.get('shot_id') == 'S02' for f in clipping),
              [f.get('shot_id') for f in clipping])
        check('visibility restored the candidate exactly', vis.get('state_restored') is True, vis.get('restoration'))

        # ---- unreachable interaction target (contact QA on the real candidate) ----
        touch = {'event_id': 'E_REACH', 'kind': 'REACH', 'actor_id': r, 'frame': 30, 'frame_start': 25, 'frame_end': 35,
                 'effector': {'bone': 'hand.R'}, 'target': {'actor_id': d}, 'contact': True}
        con = call('cinematic_analyze_contacts', cinematic=ref, options={'events': [touch]})
        event = next((e for e in con.get('events') or [] if e['event_id'] == 'E_REACH'), {})
        unreached = codes(con, 'TARGET_UNREACHED')
        REPORT['measured']['contact_event'] = event
        REPORT['measured']['target_unreached'] = unreached
        measured = (unreached[0].get('measured') or {}) if unreached else {}
        check('unreachable target: contact QA -> NO_CONTACT and TARGET_UNREACHED with a measured minimum distance '
              '(> 1 m for actors 3 m apart; never zero or UNKNOWN)',
              event.get('status') == 'NO_CONTACT' and isinstance(measured.get('min_distance_m'), float)
              and measured['min_distance_m'] > 1.0, {'event': event.get('status'), 'measured': measured})
        check('contact QA restored the candidate exactly', con.get('state_restored') is True, con.get('restoration'))

        # ---- cleanup ----
        rejected = call('cinematic_reject', cinematic=ref, apply=True, options={'reason': 'negative probe rollback'})
        REPORT['measured']['reject'] = {k: rejected.get(k) for k in ('status', 'source_restored', 'removed', 'undo_failed')}
        check('reject restores the source and undoes everything', rejected.get('source_restored') is True
              and not rejected.get('undo_failed'), REPORT['measured']['reject'])
        check('fixture file on disk unchanged (sha256)', sha256(blend) == disk_before)
        REPORT['measured']['bridge'] = {'calls': bridge.calls, 'max_payload_bytes': bridge.max_bytes}
    except Exception as exc:
        REPORT['errors'].append({'error': repr(exc)[:800], 'traceback': traceback.format_exc()[-2500:]})
        print('ERROR ' + repr(exc)[:400], flush=True)
    passed = sum(c['passed'] for c in REPORT['checks'])
    REPORT['summary'] = {'passed': passed, 'failed': len(REPORT['checks']) - passed, 'errors': len(REPORT['errors'])}
    (OUT / 'm10_negative.json').write_text(json.dumps(REPORT, indent=1, default=str), encoding='utf-8')
    print('SUMMARY ' + json.dumps(REPORT['summary']), flush=True)


main()
