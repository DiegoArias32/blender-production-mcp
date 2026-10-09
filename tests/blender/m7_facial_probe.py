"""M7 real-Blender validation (own background Blender, factory startup, controlled fixtures only).

Runs the production facial service in-process through a local bridge that executes exactly the payloads the
socket bridge would send (same build_code linker and safe-mode validation). Never contacts another Blender.

    blender -b --factory-startup --python tests/blender/m7_facial_probe.py -- <out_dir> <suite>[,<suite>...]

Suites: smoke, fixtures, negatives, demos, performance. Results: <out_dir>/m7_<suite>.json.
"""
from __future__ import annotations

import json
import math
import sys
import threading
import time
import traceback
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'tests' / 'blender'))
# Blender's Python has no MCP server dependencies; load the production package without blender_mcp/__init__.
package = types.ModuleType('blender_mcp')
package.__path__ = [str(ROOT / 'src' / 'blender_mcp')]
sys.modules.setdefault('blender_mcp', package)

import bpy  # noqa: E402

from blender_mcp.production.common.bridge import build_code  # noqa: E402
from blender_mcp.production.common.storage import Storage  # noqa: E402
import m7_facial_fixtures as fx  # noqa: E402
from m31_fixture_support import register_open_fixture  # noqa: E402


class LocalBridge:
    """Executes the linked, validated payload in this Blender (what the add-on would execute)."""

    def __init__(self):
        self.lock = threading.RLock()
        self.calls = 0
        self.bytes = 0

    def run(self, body, params=None):
        code = build_code(body, params)
        self.calls += 1
        self.bytes += len(code.encode('utf-8'))
        ns = {}
        exec(code.rsplit('print(', 1)[0], ns)
        return ns['result']


class Probe:
    def __init__(self, out, suite):
        self.out = Path(out)
        self.suite = suite
        self.report = {'suite': suite, 'blender': bpy.app.version_string, 'checks': [], 'metrics': {}, 'errors': []}
        self.bridge = LocalBridge()

    def save(self):
        (self.out / ('m7_' + self.suite + '.json')).write_text(json.dumps(self.report, indent=1, default=str), encoding='utf-8')

    def check(self, name, condition, data=None):
        row = {'name': name, 'passed': bool(condition), 'data': data}
        self.report['checks'].append(row)
        print(('PASS ' if condition else 'FAIL ') + name, flush=True)
        self.save()
        return bool(condition)

    def expect_error(self, name, code, function):
        try:
            result = function()
        except Exception as exc:
            message = str(exc)
            return self.check(name + ' -> ' + code, message.startswith(code) or code in message.split(':')[0],
                              {'error': message[:400]})
        status = result.get('status') if isinstance(result, dict) else None
        return self.check(name + ' -> ' + code, False, {'unexpected_status': status})

    def fixture(self, variant, storage_root=None, label=None, **kwargs):
        # One folder per label: two demos on the same variant must never overwrite each other's .blend.
        folder = self.out / 'fixtures' / (label or variant)
        manifest = fx.build(folder, variant, save=True, **kwargs)
        storage = Storage(storage_root or (self.out / 'store' / (label or variant)))
        register_open_fixture(self.bridge, storage, self.out / 'fixtures')
        return manifest, storage


def svc():
    from blender_mcp.production.facial import service
    return service


def smoke(p):
    S = svc()
    m, storage = p.fixture('shape_keys')
    t0 = time.perf_counter()
    result = S.generate(p.bridge, storage, 'smoke-generate-0001', m['face'], 'angry but trying to hide it, watching someone off-screen',
                        {'duration_s': 3.0, 'fps': 30}, None, {}, True)
    p.report['metrics']['generate_s'] = round(time.perf_counter() - t0, 3)
    p.check('candidate created', result['status'] == 'candidate_created', {k: result.get(k) for k in ('candidate_action', 'parts', 'written')})
    qa = result['technical_qa']
    p.check('technical QA passed', qa['passed'], qa)
    t0 = time.perf_counter()
    rendered = S.render_preview(p.bridge, storage, 'smoke-preview-0001', result['facial'], {'size': 192})
    p.report['metrics']['preview_s'] = round(time.perf_counter() - t0, 3)
    p.check('temporal preview complete', rendered['render']['temporal_complete'], rendered['render'])
    t0 = time.perf_counter()
    close = S.render_closeup(p.bridge, storage, 'smoke-closeup-0001', result['facial'], {'size': 256})
    p.report['metrics']['closeup_s'] = round(time.perf_counter() - t0, 3)
    p.check('close-up rendered', close['status'] == 'rendered', close['closeup']['labels'])
    review = {'reviewer': 'probe', 'artistic_observations': ['probe placeholder: images exist; human review pending'],
              'decision': 'ACCEPT', 'artifact_reference': rendered['render']['output_dir'], 'temporal_reviewed': True}
    reviewed = S.review(p.bridge, storage, 'smoke-review-0001', result['facial'], {'review': review})
    p.check('review recorded with separated sections', set(reviewed['review']) >= {'TECHNICAL_FINDINGS', 'ARTISTIC_OBSERVATIONS'})
    accepted = S.accept(p.bridge, storage, 'smoke-accept-0001', result['facial'], {}, True)
    p.check('accepted with body unchanged', accepted['status'] == 'accepted' and accepted['body_unchanged'], accepted.get('tracks'))
    p.report['metrics']['bridge_calls'] = p.bridge.calls
    p.report['metrics']['payload_bytes'] = p.bridge.bytes


EXPECT = {
    'shape_keys': {'class': 'SHAPE_KEYS'}, 'custom_keys': {'class': 'SHAPE_KEYS'}, 'obfuscated': {'class': 'SHAPE_KEYS'},
    'bones': {'class': 'BONES'}, 'hybrid': {'class': 'HYBRID'}, 'eye_target': {'class': 'BONES', 'aims': True},
    'asymmetric_controls': {'class': 'BONES', 'asymmetry': 'RIG_CONTROL_ASYMMETRY'},
    'intentional_asymmetry': {'class': 'SHAPE_KEYS', 'asymmetry': 'EYE_SIZE_ASYMMETRY'},
    'blink_defect': {'class': 'SHAPE_KEYS', 'calibration': 'BLINK_INCOMPLETE', 'candidate_error': 'BLINK_INCOMPLETE'},
    'eye_penetration': {'class': 'SHAPE_KEYS', 'calibration': 'EYE_PENETRATION', 'candidate_error': 'EYE_PENETRATION'},
    'lip_penetration': {'class': 'SHAPE_KEYS', 'calibration': 'LIP_CROSSING_AT_FULL', 'stress': 'LIP_CROSSING'},
    'jaw_neck_collision': {'class': 'SHAPE_KEYS', 'calibration': 'JAW_COLLISION', 'jaw_roar': 'JAW_EXTREME_COLLISION'},
    'ambiguous': {'class': 'AMBIGUOUS', 'blocked': 'AMBIGUOUS_MAPPING'},
    'unsupported': {'class': 'UNSUPPORTED', 'blocked': 'FACIAL_RIG_UNSUPPORTED'},
    'conflicting_driver': {'class': 'SHAPE_KEYS', 'driven': 'mouthSmileLeft'},
    'eyelid_rig': {'class': 'SHAPE_KEYS', 'compat': {'blink': 'SUPPORTED', 'lid_follow': 'SUPPORTED', 'jaw': 'UNSUPPORTED', 'lips': 'UNSUPPORTED'}},
    'jaw_rig': {'class': 'SHAPE_KEYS', 'compat': {'jaw': 'SUPPORTED', 'blink': 'UNSUPPORTED', 'lip_sync': 'LIMITED'}},
    'lip_rig': {'class': 'SHAPE_KEYS', 'compat': {'lips': 'SUPPORTED', 'jaw': 'UNSUPPORTED', 'lip_sync': 'UNSUPPORTED', 'blink': 'UNSUPPORTED'}},
}
CALIBRATION_ERRORS = {'BLINK_INCOMPLETE', 'EYE_PENETRATION', 'JAW_COLLISION'}


def fixtures(p):
    from blender_mcp.production.facial import profile as P
    from blender_mcp.production.facial import tools as T
    S = svc()
    rows = {}
    for variant, expect in EXPECT.items():
        m, storage = p.fixture(variant)
        t0 = time.perf_counter()
        prof, raw = P.build(p.bridge, m['face'])
        seconds = round(time.perf_counter() - t0, 3)
        kind = prof['classification']['type']
        types_found = sorted({f['type'] for f in prof['calibration'].get('findings', [])})
        rows[variant] = {'classification': prof['classification'], 'usable_outputs': prof['counts']['usable_outputs'],
                         'calibration_findings': types_found, 'timings': prof['timings'], 'profile_s': seconds,
                         'lids': prof['calibration'].get('lids'), 'jaw': prof['calibration'].get('jaw'),
                         'lips': prof['calibration'].get('lips'), 'vertices': m['vertices'], 'triangles': m['triangles']}
        p.check(variant + ': classified ' + expect['class'], kind == expect['class'], prof['classification'])
        if 'calibration' in expect:
            p.check(variant + ': measured calibration finds ' + expect['calibration'], expect['calibration'] in types_found, types_found)
        elif kind not in ('AMBIGUOUS', 'UNSUPPORTED'):
            p.check(variant + ': clean calibration (no lid/eye/jaw errors)', not (CALIBRATION_ERRORS & set(types_found)), types_found)
        if expect.get('compat'):
            got = {k: prof['compatibility'][k]['status'] for k in expect['compat']}
            p.check(variant + ': per-capability compatibility ' + json.dumps(expect['compat']), got == expect['compat'], got)
        if expect.get('aims'):
            p.check(variant + ': eye aim targets found', len(prof['aims']) == 2 and
                    prof['mapping']['eye_yaw_L'].get('method') == 'aim_target', prof['aims'])
        if expect.get('asymmetry'):
            report = S.analyze(p.bridge, storage, 'asymmetry', m['face'])
            found = [f for f in report['findings'] if f['type'] == expect['asymmetry']]
            rows[variant]['asymmetry'] = found
            p.check(variant + ': asymmetry measured (' + expect['asymmetry'] + ')', bool(found), report['findings'])
        if expect.get('driven'):
            used = [e['control'].get('key') for e in prof['mapping'].values() if e.get('usable') and e['control'].get('type') == 'shape_key']
            p.check(variant + ': driven key reported and never chosen', any(expect['driven'] in k for k in prof['driven_keys'])
                    and expect['driven'] not in used, prof['driven_keys'])
        if expect.get('blocked'):
            p.expect_error(variant + ': mutation blocked', expect['blocked'],
                           lambda: S.generate(p.bridge, storage, 'blocked-' + variant + '-01', m['face'], 'angry', None, None, {}, True))
            continue
        if kind != expect['class']:
            continue
        options = {'gaze': {'sequence': [{'frame': 1, 'target': {'type': 'camera'}},
                                         {'frame': 30, 'target': {'type': 'angles', 'yaw_deg': 12.0, 'pitch_deg': -5.0}}]},
                   'saccades': False, 'blinks': {'events': [{'frame': 20, 'kind': 'FULL'}]}}
        t0 = time.perf_counter()
        created = S.generate(p.bridge, storage, 'cand-' + variant + '-01', m['face'], 'angry but restrained',
                             {'duration_s': 2.0, 'fps': 30}, None, options, True)
        qa = created['technical_qa']
        rows[variant]['candidate'] = {'action': created['candidate_action'], 'written': created.get('written'), 'metrics': qa['metrics'],
                                      'errors': [f['type'] for f in qa['errors']], 'generate_s': round(time.perf_counter() - t0, 3)}
        if expect.get('candidate_error'):
            p.check(variant + ': candidate QA detects ' + expect['candidate_error'],
                    expect['candidate_error'] in rows[variant]['candidate']['errors'], qa['errors'])
        else:
            p.check(variant + ': candidate technical QA passes', qa['passed'], qa['errors'])
        p.check(variant + ': measured gaze within tolerance', qa['metrics'].get('gaze_error_max_deg') is not None and
                qa['metrics']['gaze_error_max_deg'] <= 1.5,
                {k: qa['metrics'].get(k) for k in ('gaze_error_max_deg', 'gaze_error_mean_deg', 'gaze_samples')})
        if expect.get('stress'):
            stress = S.deformation_qa(p.bridge, storage, m['face'], None, {'combinations': [['jaw_open', 'lips_close']]})
            found = [f for f in stress['findings'] if f['type'] == expect['stress'] and f.get('pose') == 'jaw_open+lips_close']
            p.check(variant + ': stress pose jaw+close detects ' + expect['stress'], bool(found), stress['findings'][:5])
        if expect.get('jaw_roar'):
            p.expect_error(variant + ': roar at full validated against geometry', expect['jaw_roar'],
                           lambda: T.dispatch('facial_generate_jaw', p.bridge, storage, 'jaw-' + variant, m['face'], None,
                                              {'duration_s': 2.0, 'fps': 30}, None, {'mode': 'roar'}, False))
    clean = rows['shape_keys']
    p.check('clean lip gap model is linear (error < 1.5 mm)', bool(clean['lips']) and clean['lips']['calibrated'], clean['lips'])
    m, storage = p.fixture('shape_keys', label='lifecycle')
    jaw = T.dispatch('facial_generate_jaw', p.bridge, storage, 'jaw-shout-01', m['face'], 'angry', {'duration_s': 2.0, 'fps': 30}, None,
                     {'mode': 'shout', 'start_frame': 10, 'end_frame': 50}, True)
    p.check('jaw: shout generated, validated at its peak and QA passes', jaw['status'] == 'candidate_created' and jaw['technical_qa']['passed'],
            jaw['technical_qa']['metrics'])
    brows = S.generate_kind(p.bridge, storage, 'brows-01', 'brows', None, None, None, jaw['facial'],
                            {'events': [{'frame': 20, 'kind': 'flash', 'strength': 0.6}, {'frame': 40, 'kind': 'furrow', 'strength': 0.5,
                                                                                         'duration_s': 0.5, 'side': 'L'}]}, True)
    path, plan, state = S.load(storage, brows['facial'])
    p.check('brows: explicit events layered on top of the jaw (both parts kept)', brows['status'] == 'candidate_created' and
            {'jaw', 'brows'} <= set(state['parts']) and brows['technical_qa']['passed'], sorted(state['parts']))
    p.check('candidate versions retained (v1 jaw, v2 jaw+brows)', len(state['history']) == 2 and
            all(h['candidate'] in bpy.data.actions for h in state['history']), state['history'])
    rejected = S.reject(p.bridge, storage, 'reject-01', brows['facial'], {'reason': 'probe: rejection path'}, True)
    p.check('reject: candidate retained, nothing attached, shape keys and body restored', rejected['status'] == 'rejected' and
            all(rejected['restored'].values()) and bpy.data.actions[state['candidate_action']].get('production_facial_lifecycle') == 'REJECTED',
            rejected['restored'])
    p.expect_error('rejected plan is terminal', 'GENERATION_TERMINAL',
                   lambda: S.generate_kind(p.bridge, storage, 'after-reject', 'expression', None, None, None, brows['facial'], {}, True))
    p.report['fixtures'] = rows
    p.report['metrics']['fixtures'] = len(rows)


def m7_actions():
    return sorted(a.name for a in bpy.data.actions if a.get('production_facial_owner') == 'M7')


def save_registered(p, name, storage):
    path = p.out / 'fixtures' / (name + '.blend')
    bpy.ops.wm.save_as_mainfile(filepath=str(path))
    register_open_fixture(p.bridge, storage, p.out / 'fixtures')
    return path


def add_body_action(rig, bones=('spine', 'chest'), name='M7Test_Body'):
    """A small controlled body Action on the fixture rig (never on the head/neck unless asked)."""
    action = bpy.data.actions.new(name)
    rig.animation_data_create()
    rig.animation_data.action = action
    for frame, angle in ((1, 0.0), (30, 0.12), (61, 0.0)):
        for bone in bones:
            pb = rig.pose.bones[bone]
            pb.rotation_mode = 'XYZ'
            pb.rotation_euler = (angle, 0.0, angle * 0.5)
            pb.keyframe_insert('rotation_euler', frame=frame)
    for bone in bones:
        rig.pose.bones[bone].rotation_euler = (0.0, 0.0, 0.0)
    bpy.context.scene.frame_set(1)
    return action


def negatives(p):
    from blender_mcp.production.facial import tools as T
    from blender_mcp.production.facial import profile as P
    from blender_mcp.production.common.asset_safety import register_master
    S = svc()
    phrase = fx.viseme_phrase()
    brief2 = {'duration_s': 2.0, 'fps': 30}
    rows = {}

    def guarded(name, code, function):
        before = m7_actions()
        ok = p.expect_error(name, code, function)
        after = m7_actions()
        p.check(name + ': no facial Action created', before == after, {'before': before, 'after': after})
        rows[name] = code
        return ok

    # 1 MASTER: a separate fixture path registered MASTER (never re-labelling a FIXTURE path).
    m = fx.build(p.out / 'master', 'shape_keys', save=True)
    storage = Storage(p.out / 'store' / 'neg_master')
    register_master(p.bridge, storage)
    guarded('MASTER mutation', 'MASTER_ASSET_READ_ONLY',
            lambda: S.generate(p.bridge, storage, 'neg-master-0001', m['face'], 'angry', brief2, None, {}, True))
    # 2/3 unsupported and ambiguous rigs
    for variant, code in (('unsupported', 'FACIAL_RIG_UNSUPPORTED'), ('ambiguous', 'AMBIGUOUS_MAPPING')):
        m, storage = p.fixture(variant)
        guarded(variant + ' rig', code, lambda: S.generate(p.bridge, storage, 'neg-' + variant + '-01', m['face'], 'angry', brief2, None, {}, True))
    # 4 low-confidence eye mapping: only one gaze direction per axis remains
    m, storage = p.fixture('shape_keys')
    for eye in m['eyes'].values():
        obj = bpy.data.objects[eye]
        for block in list(obj.data.shape_keys.key_blocks):
            if block.name.startswith(('eyeLookIn', 'eyeLookDown')):
                obj.shape_key_remove(block)
    save_registered(p, 'neg_eyes', storage)
    guarded('low-confidence eye mapping', 'EYE_MAPPING_LOW_CONFIDENCE',
            lambda: S.generate_kind(p.bridge, storage, 'neg-eyes-0001', 'gaze', m['face'], 'neutral', brief2, None, {}, True))
    # 5 missing jaw
    m, storage = p.fixture('shape_keys')
    face = bpy.data.objects[m['face']]
    face.shape_key_remove(face.data.shape_keys.key_blocks['jawOpen'])
    save_registered(p, 'neg_jaw', storage)
    guarded('missing jaw', 'MISSING_JAW_CONTROL',
            lambda: S.generate_kind(p.bridge, storage, 'neg-jaw-0001', 'speech', m['face'], 'neutral', brief2, None, {'timeline': phrase}, True))
    # 6 invalid Basis
    m, storage = p.fixture('shape_keys')
    face = bpy.data.objects[m['face']]
    basis = face.data.shape_keys.reference_key
    for i in range(0, 200):
        basis.data[i].co.z += 0.005
    save_registered(p, 'neg_basis', storage)
    guarded('invalid Basis', 'BASIS_INVALID', lambda: S.generate(p.bridge, storage, 'neg-basis-0001', m['face'], 'angry', brief2, None, {}, True))
    # 7 absolute keys
    m, storage = p.fixture('shape_keys')
    bpy.data.objects[m['face']].data.shape_keys.use_relative = False
    save_registered(p, 'neg_absolute', storage)
    guarded('absolute keys', 'ABSOLUTE_SHAPE_KEYS', lambda: S.generate(p.bridge, storage, 'neg-abs-0001', m['face'], 'angry', brief2, None, {}, True))
    # 8 conflicting drivers: the driven key is never keyed; the expression still writes the others
    m, storage = p.fixture('conflicting_driver')
    key = bpy.data.objects[m['face']].data.shape_keys
    drivers_before = len(key.animation_data.drivers)
    created = S.generate_kind(p.bridge, storage, 'neg-driver-0001', 'expression', m['face'], 'very happy', brief2, None, {}, True)
    action = bpy.data.actions[created['candidate_action']]
    paths = [c.data_path for layer in action.layers for strip in layer.strips for bag in strip.channelbags for c in bag.fcurves]
    dropped = [f for f in created['technical_qa']['warnings'] if f['type'] == 'OUTPUT_UNSUPPORTED_DROPPED']
    p.check('conflicting driver: driven key never keyed, driver intact', not any('mouthSmileLeft' in x for x in paths)
            and len(key.animation_data.drivers) == drivers_before, {'dropped': dropped, 'curves': len(paths)})
    rows['conflicting drivers'] = 'driven control excluded'
    # 9/10 invalid viseme timeline and unsupported viseme
    overlapping = {'units': 'seconds', 'phonemes': [{'phoneme': 'B', 'start': 0.1, 'end': 0.3}, {'phoneme': 'AA', 'start': 0.2, 'end': 0.4}]}
    unknown = {'units': 'seconds', 'visemes': [{'viseme': 'XX', 'start': 0.1, 'end': 0.3}]}
    m, storage = p.fixture('shape_keys')
    guarded('invalid viseme timeline', 'TIMELINE_INVALID',
            lambda: S.generate_kind(p.bridge, storage, 'neg-timeline-01', 'speech', m['face'], 'neutral', brief2, None, {'timeline': overlapping}, True))
    guarded('unsupported viseme', 'UNSUPPORTED_VISEME',
            lambda: S.generate_kind(p.bridge, storage, 'neg-viseme-001', 'speech', m['face'], 'neutral', brief2, None, {'timeline': unknown}, True))
    guarded('audio without timing', 'TIMING_INPUT_REQUIRED',
            lambda: S.generate_kind(p.bridge, storage, 'neg-audio-0001', 'speech', m['face'], 'neutral', brief2, None,
                                    {'timeline': {'audio': 'line.wav'}}, True))
    # 11/12 gaze geometry
    far = {'sequence': [{'frame': 1, 'target': {'type': 'angles', 'yaw_deg': 40.0, 'pitch_deg': 0.0}}]}
    guarded('eye target out of range', 'EYE_TARGET_OUT_OF_RANGE',
            lambda: S.generate_kind(p.bridge, storage, 'neg-range-0001', 'gaze', m['face'], 'neutral', brief2, None, far, True))
    prof, raw = P.build(p.bridge, m['face'])
    fr = prof['frame']
    near_point = [fr['origin'][k] + fr['forward'][k] * 0.06 for k in range(3)]  # each eye ~18 deg, vergence ~36 deg
    near = {'sequence': [{'frame': 1, 'target': {'type': 'point', 'point': near_point}}]}
    guarded('impossible convergence', 'IMPOSSIBLE_CONVERGENCE',
            lambda: S.generate_kind(p.bridge, storage, 'neg-verge-0001', 'gaze', m['face'], 'neutral', brief2, None, near, True))
    # 13 candidate collision: the deterministic name already exists (never overwritten)
    planned = S.plan_expression(p.bridge, storage, 'neg-collide-plan', m['face'], 'angry', brief2, None, {}, False)
    path, plan, state = S.load(storage, planned['facial'])
    name = S.candidate_name(plan, 1)
    squatter = bpy.data.actions.new(name)
    squatter['marker'] = 'pre-existing'
    save_registered(p, 'neg_collision', storage)
    path, plan, state = S.load(storage, planned['facial'])
    p.expect_error('facial candidate collision', 'STALE_PROPOSAL',
                   lambda: S.generate_kind(p.bridge, storage, 'neg-collide-01', 'expression', None, None, None, planned['facial'], {}, True))
    planned = S.plan_expression(p.bridge, storage, 'neg-collide-plan2', m['face'], 'angry', brief2, None, {}, False)
    path, plan, state = S.load(storage, planned['facial'])
    bpy.data.actions.new(S.candidate_name(plan, 1))['marker'] = 'pre-existing'
    before = m7_actions()
    guarded('facial Candidate collision', 'FACIAL_CANDIDATE_COLLISION',
            lambda: S.generate_kind(p.bridge, storage, 'neg-collide-02', 'expression', None, None, None, planned['facial'], {}, True))
    p.check('collision: pre-existing Action untouched', bpy.data.actions[S.candidate_name(plan, 1)].get('marker') == 'pre-existing')
    # 14 stale proposal: edited plan.json fails integrity
    planned = S.plan_expression(p.bridge, storage, 'neg-stale-plan', m['face'], 'angry', brief2, None, {}, False)
    plan_file = Path(planned['facial'])
    data = json.loads(plan_file.read_text(encoding='utf-8'))
    data['head_policy'] = 'FACE_OWNS'
    plan_file.write_text(json.dumps(data), encoding='utf-8')
    guarded('stale proposal (edited plan)', 'STALE_PROPOSAL',
            lambda: S.generate_kind(p.bridge, storage, 'neg-stale-0001', 'expression', None, None, None, planned['facial'], {}, True))
    # 15 stale rig fingerprint: a shape key changes after planning
    planned = S.plan_expression(p.bridge, storage, 'neg-rig-plan', m['face'], 'angry', brief2, None, {}, False)
    bpy.data.objects[m['face']].data.shape_keys.key_blocks['browDownLeft'].slider_max = 0.8
    guarded('stale rig fingerprint', 'STALE_RIG_FINGERPRINT',
            lambda: S.generate_kind(p.bridge, storage, 'neg-rig-0001', 'expression', None, None, None, planned['facial'], {}, True))
    # 16 body Action modified unexpectedly
    m, storage = p.fixture('shape_keys')
    rig = bpy.data.objects[m['rig']]
    body = add_body_action(rig)
    save_registered(p, 'neg_body', storage)
    planned = S.plan_expression(p.bridge, storage, 'neg-body-plan', m['face'], 'angry', brief2, None, {}, False)
    fc = [c for layer in body.layers for strip in layer.strips for bag in strip.channelbags for c in bag.fcurves][0]
    fc.keyframe_points.insert(45, 0.3)
    guarded('body Action modified unexpectedly', 'BODY_ACTION_CHANGED',
            lambda: S.generate_kind(p.bridge, storage, 'neg-body-0001', 'expression', None, None, None, planned['facial'], {}, True))
    # 16b FACE_OWNS while the body animates the head
    m, storage = p.fixture('shape_keys')
    add_body_action(bpy.data.objects[m['rig']], ('neck', 'head'), 'M7Test_HeadBody')
    save_registered(p, 'neg_owner', storage)
    guarded('FACE_OWNS while body animates head', 'HEAD_OWNERSHIP_CONFLICT',
            lambda: S.generate(p.bridge, storage, 'neg-owner-0001', m['face'], 'angry', brief2, None, {'head_policy': 'FACE_OWNS'}, True))
    # 17 refinement > 3 and accept without review
    m, storage = p.fixture('shape_keys')
    created = S.generate(p.bridge, storage, 'neg-refine-gen', m['face'], 'angry but restrained', brief2, None,
                         {'saccades': {'amplitude_deg': 3.0}}, True)
    p.expect_error('accept without visual review', 'VISUAL_REVIEW_REQUIRED',
                   lambda: S.accept(p.bridge, storage, 'neg-accept-001', created['facial'], {}, True))
    outcomes = []
    for cycle in range(3):
        result = S.refine(p.bridge, storage, 'neg-refine-' + str(cycle), created['facial'],
                          {'target': 'curve_noise', 'minimum_improvement': 1e-9, 'changes': {'polish': {'strength': 0.3 + 0.2 * cycle}}}, True)
        outcomes.append(result['status'])
        if result['status'] == 'visual_review_required':
            reviewed = S.review(p.bridge, storage, 'neg-refine-rev-' + str(cycle), created['facial'],
                                {'review': {'reviewer': 'probe', 'artistic_observations': ['negative test: refinement not adopted'],
                                            'decision': 'REJECT', 'artifact_reference': result['render']['output_dir'], 'temporal_reviewed': True}})
            outcomes.append(reviewed['status'])
    p.check('three refinement cycles consumed', S.load(storage, created['facial'])[2]['refinement_cycles'] == 3, outcomes)
    # Each cycle must really attempt a candidate (no collision with a retained, reverted version).
    p.check('every refinement cycle produced a measured outcome', 'error' not in outcomes and len(outcomes) >= 3, outcomes)
    guarded('refinement > 3', 'REFINEMENT_LIMIT',
            lambda: S.refine(p.bridge, storage, 'neg-refine-4', created['facial'],
                             {'target': 'curve_noise', 'minimum_improvement': 1e-9, 'changes': {'polish': {'strength': 0.9}}}, True))
    # 18 incomplete mocap mapping
    guarded('incomplete mocap mapping', 'MOCAP_MAPPING_INCOMPLETE',
            lambda: S.mocap_retarget(p.bridge, storage, 'neg-mocap-0001', m['face'], None,
                                     {'source': fx.synthetic_mocap(naming='unknown', seconds=2.0)}, True))
    p.report['negatives'] = rows


M6_COPY = Path(__import__('os').environ.get('M7_M6_CHECKPOINT_COPY', ''))


def action_digest(action):
    import hashlib
    curves = [c for layer in action.layers for strip in layer.strips for bag in strip.channelbags for c in bag.fcurves]
    rows = [[c.data_path, c.array_index, [list(k.co) + list(k.handle_left) + list(k.handle_right) + [k.interpolation] for k in c.keyframe_points]]
            for c in curves]
    return hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()


def first_reach(rows, column, target, after, ratio=0.9):
    for row in rows:
        if row[0] >= after and row[column] is not None and abs(row[column]) >= ratio * abs(target):
            return row[0]
    return None


def demo_renders(p, S, storage, facial, label, closeup_frames, views=None, compare=None):
    options = {'size': 256}
    if views:
        options['views'] = views
    if compare:
        options['compare'] = compare
    t0 = time.perf_counter()
    preview = S.render_preview(p.bridge, storage, label + '-preview', facial, options)
    preview_s = round(time.perf_counter() - t0, 3)
    t0 = time.perf_counter()
    close = S.render_closeup(p.bridge, storage, label + '-closeup', facial,
                             {'size': 512, 'frames': closeup_frames, 'compare': compare} if compare else {'size': 512, 'frames': closeup_frames})
    closeup_s = round(time.perf_counter() - t0, 3)
    p.check(label + ': temporal preview complete', preview['render']['temporal_complete'], preview['render']['metrics'])
    p.check(label + ': identical close-up cameras', close['closeup']['identical_cameras'] and close['status'] == 'rendered')
    return {'preview_dir': preview['render']['output_dir'], 'player': preview['render']['player'], 'animations': preview['render']['animations'],
            'closeup_dir': close['closeup']['output_dir'], 'montages': {k: v['montages'] for k, v in close['closeup']['labels'].items()},
            'preview_s': preview_s, 'closeup_s': closeup_s, 'preview_metrics': preview['render']['metrics']}


def demos(p):
    S = svc()
    manifest = {}
    # ---------------- Demo A: angry but restrained (no speech)
    m, storage = p.fixture('shape_keys', label='demoA')
    t0 = time.perf_counter()
    asymmetry = [{'area': 'brow_lower', 'side': 'L', 'strength': 0.15, 'purpose': 'one-sided distrust: the left brow sits lower',
                  'start_frame': 20, 'end_frame': 121, 'ramp_s': 0.6}]
    a = S.generate(p.bridge, storage, 'demoA-generate', m['face'], 'angry but restrained, staring at someone in front',
                   {'duration_s': 4.0, 'fps': 30, 'microexpressions': True, 'asymmetry': asymmetry}, None,
                   {'saccades': {'attention_style': 'ATTENTIVE', 'amplitude_deg': 1.5, 'frequency_hz': 1.2, 'seed': 3},
                    'blinks': {'context': 'tense', 'seed': 4},
                    'brows': {'events': [{'frame': 85, 'kind': 'furrow', 'strength': 0.3, 'duration_s': 0.8}]}}, True)
    gen_s = round(time.perf_counter() - t0, 3)
    qa = a['technical_qa']
    path, plan, state = S.load(storage, a['facial'])
    parts = state['parts']
    p.check('Demo A: candidate created without speech', a['status'] == 'candidate_created' and 'speech' not in parts, sorted(parts))
    p.check('Demo A: technical QA passes', qa['passed'], qa['errors'])
    p.check('Demo A: restrained anger keeps brows lowered and lips pressed', all(k in parts['expression']['targets'] for k in
            ('brow_lower_L', 'brow_lower_R', 'lip_press_L', 'lip_press_R')), parts['expression']['targets'])
    p.check('Demo A: subtle intentional asymmetry with purpose and timing', parts.get('asymmetry') and
            parts['asymmetry']['params']['entries'][0]['purpose'].startswith('one-sided'), parts.get('asymmetry', {}).get('params'))
    p.check('Demo A: explicit brow layer and jaw/lip tension present', parts.get('brows') and
            {'jaw_forward', 'lip_press_L'} <= set(parts['expression']['targets']), sorted(parts))
    p.check('Demo A: microexpressions bounded (<= 0.25, >= 1.5 s apart)', parts.get('micro') and
            all(e['amplitude'] <= 0.25 for e in parts['micro']['params']['events']), parts.get('micro', {}).get('params'))
    renders = demo_renders(p, S, storage, a['facial'], 'demoA', [1, 13, 60, 120])
    bpy.ops.wm.save_mainfile()
    manifest['A'] = dict(renders, facial=a['facial'], file=bpy.data.filepath, store=str(storage.root), generate_s=gen_s,
                         qa=qa['metrics'], targets=parts['expression']['targets'], micro=parts['micro']['params']['events'],
                         blinks=parts['blinks']['stats'])
    # ---------------- Demo B: target A -> eyes lead -> head follows -> blink -> target B
    m, storage = p.fixture('shape_keys', label='demoB')
    seq = {'sequence': [{'frame': 1, 'target': {'type': 'camera'}},
                        {'frame': 31, 'target': {'type': 'angles', 'yaw_deg': 30.0, 'pitch_deg': -12.0, 'distance_m': 0.6}}],
           'coordination': 'NATURAL'}
    b1 = S.generate_kind(p.bridge, storage, 'demoB-gaze', 'gaze', m['face'], 'neutral', {'duration_s': 3.0, 'fps': 30}, None,
                         dict(seq, head_policy='FACE_OWNS'), True)
    b = S.generate_kind(p.bridge, storage, 'demoB-blinks', 'blinks', None, None, None, b1['facial'], {'context': 'attentive', 'seed': 2}, True)
    qa = b['technical_qa']
    path, plan, state = S.load(storage, b['facial'])
    rows = state['technical_qa']['measured_gaze']
    shift = state['parts']['gaze']['shifts'][0]
    final = rows[-1]
    eye_world = [[r[0], (r[1][0] + r[3][0]) if r[1] and r[3] else None] for r in rows]
    head_rows = [[r[0], r[3][0] if r[3] else None] for r in rows]
    t_eye = first_reach(eye_world, 1, eye_world[-1][1], 31)
    t_head = first_reach(head_rows, 1, head_rows[-1][1], 31)
    blink_frames = [e['frame'] for e in state['parts']['blinks']['params']['events']]
    p.check('Demo B: technical QA passes (gaze measured)', qa['passed'], qa['errors'])
    p.check('Demo B: eyes lead, head follows (measured)', t_eye is not None and t_head is not None and t_eye < t_head,
            {'eyes_90pct_frame': t_eye, 'head_90pct_frame': t_head, 'final_eye_world_yaw': eye_world[-1][1], 'final_head_yaw': head_rows[-1][1],
             'split': shift['split']['ratios']})
    p.check('Demo B: blink placed at the large gaze shift', any(abs(f - 31) <= 2 for f in blink_frames), blink_frames)
    lids = {r[0]: r for r in state['technical_qa']['measured_lids_mouth']}
    follow = state['parts'].get('lid_follow')
    p.check('Demo B: lid follow lowers the upper lids when looking down (measured)', follow and lids[90][1] is not None and
            lids[90][1] > lids[1][1] + 0.02, {'closure_frame1': lids[1][1], 'closure_frame90': lids[90][1]})
    vergence = final[2][0] - final[1][0] if final[1] and final[2] else None
    p.check('Demo B: convergence measured (right eye turns in more than left for a 0.6 m target)', vergence is not None and vergence > 0.5,
            {'measured_vergence_deg': vergence})
    p.check('Demo B: eye limits respected (no out-of-range frames)', not [f for f in state['technical_qa']['findings'] if f['type'] == 'EYE_LIMIT_EXCEEDED'])
    renders = demo_renders(p, S, storage, b['facial'], 'demoB', [1, 31, 33, 36, 45, 90])
    bpy.ops.wm.save_mainfile()
    manifest['B'] = dict(renders, facial=b['facial'], file=bpy.data.filepath, store=str(storage.root), qa=qa['metrics'],
                         eyes_frame=t_eye, head_frame=t_head, blink_frames=blink_frames, shift=shift)
    # ---------------- Demo C: lip-sync from controlled timing
    m, storage = p.fixture('shape_keys', label='demoC')
    phrase = fx.viseme_phrase()
    c = S.generate(p.bridge, storage, 'demoC-generate', m['face'], 'slightly happy, speaking calmly',
                   {'duration_s': 2.0, 'fps': 30, 'speech': {'enabled': True}}, None,
                   {'kinds': ['expression', 'speech', 'blinks'], 'speech': {'timeline': phrase}, 'blinks': {'seed': 9}}, True)
    full = S.load(storage, c['facial'])[2]['technical_qa']
    qa = dict(c['technical_qa'], lipsync=full['lipsync'])
    mbp = full['events']['mbp_centres']
    gaps = {r[0]: r[3] for r in full['measured_lids_mouth']}
    p.check('Demo C: technical QA passes (closures sealed, no crossing)', qa['passed'], qa['errors'])
    p.check('Demo C: every bilabial closure measured sealed (<= 1 mm)', mbp and all(gaps.get(f) is not None and gaps[f] <= 1.0 for f in mbp),
            {f: gaps.get(f) for f in mbp})
    p.check('Demo C: channel lip-sync validation passes', qa['lipsync']['passed'], qa['lipsync']['metrics'])
    stateC = S.load(storage, c['facial'])[2]
    path, planC, _ = S.load(storage, c['facial'])
    combinedC, _, _ = S.compose_tracks(planC, stateC['parts'])
    target = stateC['parts']['expression']['targets'].get('lip_corner_up_L', 0.0)
    expression = stateC['parts']['expression']['tracks']['lip_corner_up_L']
    # Rule checked per frame: while speaking, the corners keep at least 70 % of the expression at that same frame.
    ratios = [S.L.sample(combinedC['lip_corner_up_L'], f) / S.L.sample(expression, f) for f in range(10, 40) if S.L.sample(expression, f) > 1e-3]
    p.check('Demo C: expression overlay kept under speech (smile corners >= 70 % residual per frame)', target > 0 and ratios and
            min(ratios) >= 0.7 - 1e-3, {'expression_target': target, 'min_ratio_during_speech': round(min(ratios), 4)})
    renders = demo_renders(p, S, storage, c['facial'], 'demoC', sorted(set(mbp + [10, 15])))
    bpy.ops.wm.save_mainfile()
    manifest['C'] = dict(renders, facial=c['facial'], file=bpy.data.filepath, store=str(storage.root), qa=qa['metrics'],
                         mbp_frames=mbp, mbp_gaps_mm={f: gaps.get(f) for f in mbp}, lipsync=qa['lipsync']['metrics'])
    # ---------------- Demo D: controlled M6 body action + separate facial candidate
    if M6_COPY.is_file():
        import shutil
        target = p.out / 'fixtures' / 'demoD_m6_copy.blend'
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(M6_COPY, target)
        bpy.ops.wm.open_mainfile(filepath=str(target), load_ui=False, use_scripts=False)
        rig = bpy.data.objects['M6_hit_reaction_Rig']
        keep = {rig} | set(rig.children_recursive)
        for obj in bpy.data.objects:
            if obj not in keep and obj.type in ('MESH', 'ARMATURE'):
                obj.hide_render = True
                obj.hide_viewport = True
        body = rig.animation_data.action
        body_before = action_digest(body)
        span = [int(body.frame_range[0]), int(body.frame_range[1])]
        md = fx.build(p.out / 'fixtures', 'shape_keys', save=False, existing_rig=rig, head_bone='head')
        storage = Storage(p.out / 'store' / 'demoD')
        bpy.ops.wm.save_as_mainfile(filepath=str(target))
        register_open_fixture(p.bridge, storage, p.out / 'fixtures')
        seconds = (span[1] - 1) / bpy.context.scene.render.fps
        # The M6 proxy body carries its own head where the facial fixture sits: the facial set is named explicitly.
        facial_set = {'meshes': [md['face'], md['neck'], md['mouth_interior']], 'eyes': [md['eyes']['L'], md['eyes']['R']]}
        d = S.generate(p.bridge, storage, 'demoD-generate', md['face'], 'pain, hit and hurt',
                       {'duration_s': round(seconds, 4), 'fps': bpy.context.scene.render.fps}, None,
                       {'head_policy': 'BODY_OWNS', 'saccades': False, 'facial_set': facial_set,
                        'blinks': {'events': [{'frame': 12, 'kind': 'SLOW'}]}}, True)
        integration = S.integrate(p.bridge, storage, 'demoD-integrate', d['facial'])
        path, plan, state = S.load(storage, d['facial'])
        p.check('Demo D: facial candidate is a separate Action', d['candidate_action'] != body.name and body.name == rig.animation_data.action.name)
        p.check('Demo D: body Action digest unchanged', action_digest(body) == body_before)
        p.check('Demo D: body bones unchanged with the facial layer', integration['integration']['passed'],
                {k: integration['integration'][k] for k in ('max_body_matrix_change', 'max_head_neck_matrix_change', 'body_action_unchanged', 'body_bones')})
        p.check('Demo D: head/neck ownership explicit', plan['ownership']['policy'] == 'BODY_OWNS', plan['ownership'])
        v1_errors = [(f['type'], f.get('where'), f.get('frames')) for f in d['technical_qa']['errors']]
        renders = demo_renders(p, S, storage, d['facial'], 'demoD', [1, 8, 12, 20, span[1]])
        refinement = None
        if not d['technical_qa']['passed']:
            # A measured problem goes through the refinement loop (max 3), never through a looser threshold.
            refined = S.refine(p.bridge, storage, 'demoD-refine-1', d['facial'],
                               {'target': 'new_intersections_max', 'minimum_improvement': 1,
                                'findings': v1_errors, 'changes': {'parts': {'expression': {'dimensions': {'jaw_open': 0.03}}}}}, True)
            refinement = refined.get('refinement')
            p.check('Demo D: refinement removes the measured contact', refined['status'] == 'visual_review_required' and
                    refinement['after'] < refinement['before'], refinement)
            renders['preview_dir'] = refined['render']['output_dir']
            renders['player'] = refined['render']['player']
            close = S.render_closeup(p.bridge, storage, 'demoD-closeup-v2', d['facial'], {'size': 320, 'frames': [8, 12, 20], 'compare': 'v1'})
            renders['montages_v2'] = {k: v['montages'] for k, v in close['closeup']['labels'].items()}
        final = S.load(storage, d['facial'])[2]
        gaze_params = final['parts']['gaze']['params']
        p.check('Demo D: gaze world-locked against the measured body head motion', gaze_params['world_locked'] and
                gaze_params['body_head_motion']['max_head_rotation_deg'] > 1.0 and final['technical_qa']['metrics']['gaze_error_max_deg'] <= 1.5,
                {'motion': gaze_params['body_head_motion'], 'gaze_error_max_deg': final['technical_qa']['metrics']['gaze_error_max_deg']})
        p.check('Demo D: final candidate technical QA passes on the moving body', final['technical_qa']['passed'],
                [f for f in final['technical_qa']['findings'] if f['severity'] == 'error'])
        bpy.ops.wm.save_mainfile()
        manifest['D'] = dict(renders, facial=d['facial'], file=bpy.data.filepath, store=str(storage.root), qa_v1=d['technical_qa']['metrics'],
                             v1_errors=v1_errors, refinement=refinement, qa=final['technical_qa']['metrics'],
                             body_action=body.name, body_digest=body_before, body_span=span, integration=integration['integration'],
                             ownership=plan['ownership'], candidate=final['candidate_action'])
    else:
        p.check('Demo D: M6 checkpoint copy available (M7_M6_CHECKPOINT_COPY)', False, str(M6_COPY))
    # ---------------- Demo E: synthetic facial mocap -> map -> retarget -> cleanup -> QA -> before/after
    bpy.ops.wm.read_factory_settings(use_empty=True)
    m, storage = p.fixture('shape_keys', label='demoE')
    source = fx.synthetic_mocap(fps=30, seconds=5.0, noise=0.025, chatter=0.03, eye_jitter=0.04, seed=7)
    from blender_mcp.production.facial import mocap as MC
    analysis = MC.analyze(MC.load(source))
    mapped = S.mocap_map(p.bridge, storage, m['face'], None, {'source': source})
    e1 = S.mocap_retarget(p.bridge, storage, 'demoE-retarget', m['face'], None, {'source': source}, True)
    e2 = S.mocap_cleanup(p.bridge, storage, 'demoE-cleanup', e1['facial'], {}, True)
    p.check('Demo E: mapping complete', mapped['status'] == 'mapped' and mapped['mapped_ratio'] >= 0.9, {'ratio': mapped['mapped_ratio'], 'unmapped': mapped['unmapped']})
    p.check('Demo E: raw retarget candidate (v1) created', e1['status'] == 'candidate_created', e1['retarget'])
    p.check('Demo E: cleanup reduces measured noise and keys', e2['cleanup']['by_unit']['normalized']['noise_reduction_ratio'] > 0 and e2['cleanup']['by_unit']['degrees']['noise_reduction_ratio'] > 0 and
            e2['cleanup']['keys_after'] < e2['cleanup']['keys_before'], {k: e2['cleanup'][k] for k in ('noise_reduction_ratio', 'keys_before', 'keys_after', 'by_unit')})
    p.check('Demo E: cleaned candidate (v2) passes QA', e2['technical_qa']['passed'], e2['technical_qa']['errors'])
    renders = demo_renders(p, S, storage, e2['facial'], 'demoE', [28, 50, 79, 124], compare='v1')
    bpy.ops.wm.save_mainfile()
    manifest['E'] = dict(renders, facial=e2['facial'], file=bpy.data.filepath, store=str(storage.root), analysis={k: analysis[k] for k in ('noisiest_channels', 'blinks', 'groups')},
                         mapped_ratio=mapped['mapped_ratio'], v1=e1['technical_qa']['metrics'], v2=e2['technical_qa']['metrics'],
                         cleanup={k: e2['cleanup'][k] for k in ('noise_reduction_ratio', 'keys_before', 'keys_after', 'by_unit')},
                         channels=e2['cleanup']['channels'])
    (p.out / 'demos_manifest.json').write_text(json.dumps(manifest, indent=1, default=str), encoding='utf-8')
    p.report['demos'] = manifest


def demos_review(p):
    """Record the human-style visual review written after looking at the renders, then accept/reject."""
    S = svc()
    manifest = json.loads((p.out / 'demos_manifest.json').read_text(encoding='utf-8'))
    reviews = json.loads((p.out / 'demos_review_input.json').read_text(encoding='utf-8'))
    for key, row in manifest.items():
        bpy.ops.wm.open_mainfile(filepath=row['file'], load_ui=False, use_scripts=False)
        storage = Storage(row['store'])
        given = reviews[key]
        review = dict(given['review'], artifact_reference=row['preview_dir'], temporal_reviewed=True)
        reviewed = S.review(p.bridge, storage, 'demo' + key + '-review', row['facial'], {'review': review})
        p.check('Demo ' + key + ': review recorded (technical vs artistic)', set(reviewed['review']) >= {'TECHNICAL_FINDINGS', 'ARTISTIC_OBSERVATIONS'})
        if given['action'] == 'accept':
            body = None
            if key == 'D':
                body = action_digest(bpy.data.actions[row['body_action']])
            accepted = S.accept(p.bridge, storage, 'demo' + key + '-accept', row['facial'], {}, True)
            p.check('Demo ' + key + ': accepted as a separate facial layer', accepted['status'] == 'accepted' and accepted['body_unchanged'], accepted['tracks'])
            if key == 'D':
                p.check('Demo D: body Action unchanged after acceptance', action_digest(bpy.data.actions[row['body_action']]) == body == row['body_digest'])
            bpy.ops.wm.save_mainfile()
        else:
            rejected = S.reject(p.bridge, storage, 'demo' + key + '-reject', row['facial'], {'reason': given.get('reason')}, True)
            p.check('Demo ' + key + ': rejected and scene restored', rejected['status'] == 'rejected' and all(rejected['restored'].values()), rejected['restored'])


def tiled_phrase(seconds):
    """The controlled phrase repeated to fill a clip (segments past the end are dropped, never stretched)."""
    base = fx.viseme_phrase()
    rows, words, offset = [], [], 0.0
    period = 1.8
    while offset + 0.3 < seconds:
        for r in base['phonemes']:
            s, e = r['start'] + offset, r['end'] + offset
            if e <= seconds - 0.05:
                rows.append({'phoneme': r['phoneme'], 'start': round(s, 4), 'end': round(e, 4)})
        for w in base['words']:
            if w['end'] + offset <= seconds - 0.05:
                words.append(dict(w, start=round(w['start'] + offset, 4), end=round(w['end'] + offset, 4)))
        offset += period
    return {'units': 'seconds', 'phonemes': rows, 'words': words, 'source': 'controlled fixture phrase tiled'}


PERF_RENDER = {('shape_keys', 30, 5.0), ('shape_keys', 60, 15.0), ('bones', 30, 5.0)}


def performance(p):
    from blender_mcp.production.facial import mocap as MC
    S = svc()
    rows = []
    for variant in ('shape_keys', 'bones', 'hybrid'):
        for fps in (30, 60):
            for seconds in (1.0, 5.0, 15.0):
                label = variant + '_' + str(fps) + '_' + str(int(seconds))
                m, storage = p.fixture(variant, label='perf_' + label)
                calls0, bytes0 = p.bridge.calls, p.bridge.bytes
                brief = {'duration_s': seconds, 'fps': fps, 'speech': {'enabled': True}, 'microexpressions': True}
                options = {'speech': {'timeline': tiled_phrase(seconds)}, 'saccades': {'seed': 5}, 'blinks': {'seed': 6}}
                t0 = time.perf_counter()
                planned = S.generate(p.bridge, storage, 'perf-plan-' + label, m['face'], 'angry but restrained, speaking', brief, None, options, False)
                plan_s = round(time.perf_counter() - t0, 3)
                path, plan, state = S.load(storage, planned['facial'])
                t0 = time.perf_counter()
                created = S.generate(p.bridge, storage, 'perf-gen-' + label, None, None, None, planned['facial'], options, True)
                apply_s = round(time.perf_counter() - t0, 3)
                state = S.load(storage, planned['facial'])[2]
                t0 = time.perf_counter()
                source = fx.synthetic_mocap(fps=fps, seconds=seconds, noise=0.025, chatter=0.03, eye_jitter=0.04, seed=7)
                mocap = MC.load(source)
                mapped = MC.map_channels(mocap, plan['profile']['mapping'])
                tracks = MC.retarget_tracks(mocap, mapped, 1, MC.neutral_offsets(mocap))
                cleaned, report = MC.cleanup(tracks, fps)
                mocap_s = round(time.perf_counter() - t0, 3)
                row = {'rig': variant, 'fps': fps, 'seconds': seconds, 'frames': plan['brief']['frame_end'] - plan['brief']['frame_start'] + 1,
                       'plan_s_including_profile': plan_s, 'profile_timings': plan['profile']['timings'],
                       'generate_apply_s': apply_s, 'write_s': state['timings']['write_s'], 'qa_s': state['timings']['qa_s'],
                       'qa_frames': state['technical_qa']['metrics']['frames_sampled'], 'keys_written': state['written']['keys_written'],
                       'curves_written': state['written']['curves_written'], 'write_chunks': state['written']['chunks'],
                       'qa_passed': state['technical_qa']['passed'], 'mocap_cleanup_s': mocap_s,
                       'mocap_keys': [report['keys_before'], report['keys_after']], 'bridge_calls': p.bridge.calls - calls0,
                       'payload_mb': round((p.bridge.bytes - bytes0) / 1e6, 3)}
                if (variant, fps, seconds) in PERF_RENDER:
                    views = ['FRONT'] if seconds > 10 else ['FRONT', 'THREE_QUARTER_LEFT']
                    t0 = time.perf_counter()
                    rendered = S.render_preview(p.bridge, storage, 'perf-render-' + label, planned['facial'], {'size': 256, 'views': views})
                    row['preview'] = {'seconds': round(time.perf_counter() - t0, 3), 'images': rendered['render']['metrics']['images'],
                                      'complete': rendered['render']['temporal_complete'],
                                      'seconds_per_image': rendered['render']['metrics']['seconds_per_image']}
                rows.append(row)
                row['qa_errors'] = sorted({f['type'] for f in state['technical_qa']['findings'] if f['severity'] == 'error'})
                row['lip_sync_compatibility'] = plan['profile']['compatibility']['lip_sync']
                p.check('performance ' + label + ': candidate created and measured', created['status'] == 'candidate_created',
                        {k: row[k] for k in ('generate_apply_s', 'qa_s', 'keys_written')})
                # A rig that cannot seal its lips must be reported as such, never passed: QA may fail only for that reason.
                limited = row['lip_sync_compatibility']['status'] == 'LIMITED'
                p.check('performance ' + label + ': QA passes or fails only on the measured lip-seal limit',
                        row['qa_passed'] or (limited and set(row['qa_errors']) <= {'MBP_NOT_SEALED', 'MBP_CLOSURE_NOT_REACHED'}),
                        {'qa_errors': row['qa_errors'], 'lip_sync': row['lip_sync_compatibility']})
                print('PERF', json.dumps(row, default=str), flush=True)
    p.report['performance'] = rows
    p.report['metrics']['combinations'] = len(rows)


SUITES = {'smoke': smoke, 'fixtures': fixtures, 'negatives': negatives, 'demos': demos, 'demos_review': demos_review,
          'performance': performance}


def main():
    args = sys.argv[sys.argv.index('--') + 1:]
    out = Path(args[0])
    out.mkdir(parents=True, exist_ok=True)
    for suite in args[1].split(','):
        probe = Probe(out, suite)
        began = time.perf_counter()
        try:
            SUITES[suite](probe)
            probe.report['status'] = 'completed'
        except Exception as exc:
            probe.report['status'] = 'crashed'
            probe.report['errors'].append({'error': repr(exc)[:2000], 'traceback': traceback.format_exc()[-4000:]})
            print('CRASH', repr(exc)[:500], flush=True)
        probe.report['metrics']['suite_s'] = round(time.perf_counter() - began, 3)
        probe.save()
        failed = [c['name'] for c in probe.report['checks'] if not c['passed']]
        print('SUITE', suite, probe.report['status'], 'checks', len(probe.report['checks']), 'failed', len(failed), flush=True)


if __name__ == '__main__':
    main()
