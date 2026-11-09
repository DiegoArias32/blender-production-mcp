"""Demo C upstream (own background Blender, controlled fixture): the real M7 facial and M8 secondary generations that
the M10 cinematic will integrate, each accepted only after a REAL visual review.

    blender -b --factory-startup --disable-autoexec --python tests/blender/m10_demo_c_upstream.py -- <out_dir> [review_timeout_s]

The script never writes a review. For each generation it renders the temporal preview, writes
<out>/review-request-<label>.json with the artifact to inspect and a template, then waits for
<out>/review-feedback-<label>.json written by the reviewer. ACCEPT accepts the generation; anything else rejects it.
The final .blend (Hero with an accepted M7 face track and an accepted M8 pendant bake) is saved as
<out>/demo_c_source.blend with <out>/demo_c_upstream.json.
"""
from __future__ import annotations

import json
import sys
import threading
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

DURATION_S = 3.0
FPS = 30
BODY_ACTION_SUFFIX = '_Walk'


class LocalBridge:
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


REPORT = {'schema': 'm10.demo_c_upstream.v1', 'blender': bpy.app.version_string, 'steps': [], 'errors': []}


def save(out):
    (out / 'demo_c_upstream.json').write_text(json.dumps(REPORT, indent=1, default=str), encoding='utf-8')


def step(out, name, data):
    REPORT['steps'].append({'step': name, 'data': data, 'at': time.time()})
    print('STEP ' + name, flush=True)
    save(out)


def wait_review(out, label, artifact, template, timeout):
    request = {'label': label, 'artifact': artifact, 'template': template,
               'feedback_path': str(out / ('review-feedback-' + label + '.json')),
               'instruction': 'Look at the real temporal preview (player and APNG) at normal and 0.25 speed and '
                              'write the feedback file. The script never writes a review.'}
    (out / ('review-request-' + label + '.json')).write_text(json.dumps(request, indent=1, default=str), encoding='utf-8')
    print('REVIEW_REQUEST ' + str(out / ('review-request-' + label + '.json')), flush=True)
    feedback = Path(request['feedback_path'])
    deadline = time.time() + timeout
    while not feedback.exists():
        if time.time() > deadline:
            raise TimeoutError('no visual review for ' + label + '; nothing is accepted')
        time.sleep(2)
    time.sleep(0.5)
    review = json.loads(feedback.read_text(encoding='utf-8-sig'))
    REPORT.setdefault('reviews', {})[label] = review
    return review


def main():
    args = sys.argv[sys.argv.index('--') + 1:]
    out = Path(args[0])
    timeout = float(args[1]) if len(args) > 1 else 7200.0
    out.mkdir(parents=True, exist_ok=True)
    try:
        manifest = FX.build_demo('C_body_face_physics', out / 'fixture', save=True)
        bpy.ops.wm.open_mainfile(filepath=manifest['blend'], load_ui=False, use_scripts=False)
        hero = manifest['actors'][0]
        rig = bpy.data.objects[hero['object']]
        walk = bpy.data.actions[hero['name'] + BODY_ACTION_SUFFIX]
        idle = bpy.data.actions[hero['name'] + '_Idle']
        # Measured: with the walk active the M7 profile probe classifies this face AMBIGUOUS (8 of 41 usable outputs;
        # SHAPE_KEYS 0.97 with the idle). The face is profiled and generated on the idle body, as M7 was designed; the
        # walk is activated afterwards for M8, whose pendant must swing with it.
        rig.animation_data_create().action = idle
        scene = bpy.context.scene
        scene.frame_start, scene.frame_end = 1, 1 + round(FPS * DURATION_S)
        source = out / 'demo_c_source.blend'
        bpy.ops.wm.save_as_mainfile(filepath=str(source))
        bridge = LocalBridge()
        storage = Storage(out / 'store')
        register_open_fixture(bridge, storage, out)
        step(out, 'fixture', {'blend': str(source), 'hero': hero['object'], 'body_action_m7': idle.name, 'body_action_m8': walk.name,
                              'frame_range': [scene.frame_start, scene.frame_end]})

        # ---- M7: facial performance on the walking hero (body owns head/neck: the walk sways the head)
        from blender_mcp.production.facial import service as S
        face = hero['face']['objects']
        facial_set = {'meshes': [face['face'], face['neck'], face['mouth_interior']], 'eyes': [face['eye_L'], face['eye_R']]}
        generated = S.generate(bridge, storage, 'demoC-m7-generate', face['face'], 'scared, glancing around while walking',
                               {'duration_s': DURATION_S, 'fps': FPS}, None,
                               {'head_policy': 'BODY_OWNS', 'facial_set': facial_set, 'saccades': {'seed': 5},
                                'blinks': {'events': [{'frame': 18, 'kind': 'FULL'}, {'frame': 52, 'kind': 'FULL'}, {'frame': 80, 'kind': 'SLOW'}]}}, True)
        facial = generated['facial']
        step(out, 'm7_generate', {'facial': facial, 'candidate': generated.get('candidate_action'),
                                  'technical_qa_passed': generated['technical_qa']['passed'],
                                  'errors': generated['technical_qa'].get('errors')})
        integration = S.integrate(bridge, storage, 'demoC-m7-integrate', facial)
        preview = S.render_preview(bridge, storage, 'demoC-m7-preview', facial, {'size': 320})
        render = preview.get('render', preview)
        step(out, 'm7_preview', {'output_dir': render['output_dir'], 'player': render.get('player'),
                                 'temporal_complete': render.get('temporal_complete'),
                                 'integration_passed': integration['integration']['passed']})
        template = {'reviewer': '', 'artistic_observations': [], 'decision': 'ACCEPT|REJECT|REFINE',
                    'artifact_reference': render['output_dir'], 'temporal_reviewed': False,
                    'views_reviewed': [], 'speeds_reviewed': []}
        review = wait_review(out, 'm7', {'player': render.get('player'), 'output_dir': render['output_dir']}, template, timeout)
        S.review(bridge, storage, 'demoC-m7-review', facial, {'review': review})
        if review.get('decision') == 'ACCEPT':
            accepted = S.accept(bridge, storage, 'demoC-m7-accept', facial, apply=True)
            step(out, 'm7_accept', {'status': accepted.get('status'), 'tracks': accepted.get('tracks')})
        else:
            S.reject(bridge, storage, 'demoC-m7-reject', facial, apply=True)
            step(out, 'm7_rejected', {'decision': review.get('decision')})
            raise RuntimeError('M7 not accepted by the reviewer; Demo C cannot integrate a face')
        bpy.ops.wm.save_mainfile()

        # ---- M8: pendant secondary motion driven by the walk (planned after the face track exists)
        rig.animation_data.action = walk
        bpy.ops.wm.save_mainfile()
        from blender_mcp.production.physics.service import dispatch as P
        pendant = hero['secondary'][0]
        # The real body copy is the collider (M8's parity ray-step defect was fixed with authorization, see
        # tests/blender/probe_m8_parity_regression.py).
        options = {'primary_objects': [hero['object'], hero['body']], 'colliders': list(pendant['colliders_hint']),
                   'anchor_object': hero['object'], 'anchor_bone': 'chest', 'manifest': pendant['m8_manifest']}
        brief = dict(pendant['m8_brief_hint'], duration_s=DURATION_S, frame_start=1, frame_end=1 + round(FPS * DURATION_S))
        intent = 'Pendant swinging from the chest while the hero walks; preserve the body and facial performance'
        planned = P('physics_plan', bridge, storage, 'demoC-m8-plan', pendant['object'], None, intent, brief, options, False)
        ref = planned['physics']
        P('physics_create_candidate', bridge, storage, 'demoC-m8-candidate', None, ref, None, None, None, True)
        P('physics_prepare_colliders', bridge, storage, 'demoC-m8-colliders', None, ref, None, None, None, True)
        P('physics_analyze_accessory', bridge, storage, 'demoC-m8-analyze', None, ref, None, None, None, False)
        simulated = P('physics_generate_accessory_motion', bridge, storage, 'demoC-m8-generate', None, ref, None, None, None, True)
        qa = simulated.get('technical_qa') or {}
        step(out, 'm8_generate', {'physics': ref, 'state': simulated.get('state'), 'technical_qa_passed': qa.get('passed'),
                                  'collision': (qa.get('collision') or {}).get('metrics'), 'motion': (qa.get('motion') or {}).get('metrics')})
        if qa.get('passed') is not True:
            raise RuntimeError('M8 technical QA failed: ' + json.dumps((qa.get('collision') or {}).get('errors')))
        P('physics_cache_plan', bridge, storage, 'demoC-m8-cacheplan', None, ref, None, None, None, False)
        P('physics_bake_cache', bridge, storage, 'demoC-m8-cache', None, ref, None, None, None, True)
        baked = P('physics_bake_secondary_motion', bridge, storage, 'demoC-m8-bake', None, ref, None, None, None, True)
        step(out, 'm8_bake', {'bake_deviation_passed': (baked.get('bake_deviation') or {}).get('passed')})
        settings = {'width': 320, 'height': 320, 'views': ['FRONT', 'SIDE', 'THREE_QUARTER']}
        P('physics_render_preview', bridge, storage, 'demoC-m8-preview', None, ref, None, None, settings, False)
        compared = P('physics_compare_before_after', bridge, storage, 'demoC-m8-compare', None, ref, None, None, settings, False)
        render = compared.get('render') or compared
        step(out, 'm8_preview', {'output_dir': render.get('output_dir'), 'player': render.get('player')})
        template = {'reviewer': '', 'observations': [], 'findings': [], 'decision': 'ACCEPT|REJECT|REFINE',
                    'artifact_reference': render.get('output_dir'), 'temporal_reviewed': False,
                    'views_reviewed': [], 'speeds_reviewed': [], 'technical_findings': [], 'artistic_observations': []}
        review = wait_review(out, 'm8', {'player': render.get('player'), 'output_dir': render.get('output_dir')}, template, timeout)
        P('physics_review', bridge, storage, 'demoC-m8-review', None, ref, None, None, {'review': review}, False)
        if review.get('decision') == 'ACCEPT':
            accepted = P('physics_accept', bridge, storage, 'demoC-m8-accept', None, ref, None, None, None, True)
            step(out, 'm8_accept', {'state': accepted.get('state')})
        else:
            P('physics_reject', bridge, storage, 'demoC-m8-reject', None, ref, None, None, None, True)
            step(out, 'm8_rejected', {'decision': review.get('decision')})
            raise RuntimeError('M8 not accepted by the reviewer')
        bpy.ops.wm.save_mainfile()
        REPORT.update(status='ready', source_blend=bpy.data.filepath, store=str(storage.root), facial=facial,
                      physics=ref, hero=hero, bridge_calls=bridge.calls, payload_bytes=bridge.bytes)
    except Exception as exc:
        REPORT['errors'].append({'error': repr(exc), 'traceback': traceback.format_exc()})
        REPORT['status'] = 'failed'
        traceback.print_exc()
    save(out)
    print('DEMO_C_UPSTREAM ' + REPORT.get('status', 'unknown'), flush=True)


main()
