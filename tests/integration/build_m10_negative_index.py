"""Builds test-artifacts/m10/negative-tests.json: the 20 required M10 rejection/diagnostic scenarios, each mapped to
checks that ran in real Blender (own background probes or the MCP qualification on an own Blender at 9877). Every
entry is read back from the archived report (exact check name, its passed flag and its data); a name that is not in
the report counts as missing, never as a pass. A scenario is covered only when all of its listed checks passed.

    uv run --frozen python tests/integration/build_m10_negative_index.py
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ART = ROOT / 'test-artifacts' / 'm10'
PROBES = ART / 'probes-final'
MCP = ART / 'mcp'


def P(probe, name):
    return ('probe', probe, name)


def M(run, name):
    return ('mcp', run, name)


SCENARIOS = [
    ('MASTER mutation attempt', ['MASTER_ASSET_READ_ONLY'], [
        P('m10_foundation_probe', 'MASTER refuses candidate creation -> MASTER_ASSET_READ_ONLY'),
        P('m10_foundation_probe', 'MASTER refusal created no scene'),
        M('E', 'master_refused_before_any_checkpoint'), M('E', 'master_file_unchanged')]),
    ('missing actor', ['ACTOR_OBJECT_MISSING', 'ACTOR_NOT_IN_PLAN'], [
        P('m10_actors_probe', 'missing object -> ACTOR_OBJECT_MISSING'),
        M('E', 'refusal_create_camera_ACTOR_NOT_IN_PLAN')]),
    ('stale body Action', ['STALE_SOURCE', 'ACTOR_STALE'], [
        P('m10_cameras_probe', 'finalize refuses a moved source key: fresh STALE_SOURCE and an integrity gate in one list'),
        P('m10_foundation_probe', 'actor edited after registration is refused at planning -> ACTOR_STALE')]),
    ('stale facial Action', ['FACIAL_STALE'], [M('C', 'refusal_integrate_facial_FACIAL_STALE')]),
    ('stale physics cache', ['SECONDARY_NOT_CURRENT', 'PHYSICS_STALE_BAKED_CANDIDATE'], [
        M('C', 'refusal_integrate_secondary_SECONDARY_NOT_CURRENT')]),
    ('invalid camera target', ['CAMERA_TARGET_REQUIRED', 'CAMERA_TARGET_MISSING'], [
        P('m10_cameras_probe', 'ORBIT without target -> CAMERA_TARGET_REQUIRED'),
        P('m10_cameras_probe', 'focus target that is not in the candidate -> CAMERA_TARGET_MISSING')]),
    ('conflicting Action ownership', ['NLA_DRIVER_CONFLICT', 'CLIP_OVERLAP', 'SLOT_AMBIGUOUS', 'CAMERA_ANIMATION_OVERLAP'], [
        P('m10_performance_probe', 'a clip whose channel is driven on the actor -> NLA_DRIVER_CONFLICT'),
        P('m10_performance_probe', 'two clips of one actor overlapping in time -> CLIP_OVERLAP'),
        P('m10_performance_probe', 'an Action with two object slots and none for the actor -> SLOT_AMBIGUOUS'),
        P('m10_cameras_probe', 'overlapping animation without replace -> CAMERA_ANIMATION_OVERLAP'),
        M('E', 'refusal_sync_actions_CLIP_OVERLAP')]),
    ('incompatible NLA state', ['NLA_TWEAK_MODE'], [
        P('m10_performance_probe', 'sync an actor in NLA tweak mode -> NLA_TWEAK_MODE')]),
    ('duplicate operation_id', ['IDEMPOTENT_REPLAY', 'IDEMPOTENCY_KEY_CONFLICT'], [
        M('E', 'duplicate_operation_id_replays_without_running_again'),
        M('E', 'reused_operation_id_with_other_inputs_is_refused')]),
    ('ambiguous root motion', ['ROOT_MOTION_AMBIGUOUS'], [
        P('m10_performance_probe', 'rotate an actor whose Action animates object location -> ROOT_MOTION_AMBIGUOUS')]),
    ('invalid shot duration', ['CUT_TOO_SHORT', 'CINEMATIC_SHOT_OVERLAP'], [
        P('m10_cameras_probe', 'shot shorter than the proposed minimum -> CUT_TOO_SHORT'),
        P('m10_planning_probe', 'overlapping shots refused by normalize_plan -> CINEMATIC_SHOT_OVERLAP')]),
    ('camera clipping actor', ['NEAR_CLIP_CUT', 'CAMERA_CLIPPING', 'CAMERA_CLIP_INVALID'], [
        P('m10_negative_probe', 'camera clipping actor: S01 camera 0.3 m in front of Reacher with clip_start 0.5 m -> '
                                'NEAR_CLIP_CUT on S01'),
        P('m10_negative_probe', 'control: S02 camera about 7 m away with clip_start 0.1 m -> no NEAR_CLIP_CUT on S02'),
        P('m10_negative_probe', 'camera clipping actor: visibility -> CAMERA_CLIPPING for Reacher on S01'),
        P('m10_negative_probe', 'control: no CAMERA_CLIPPING on S02'),
        P('m10_cameras_probe', 'clip_start above clip_end -> CAMERA_CLIP_INVALID')]),
    ('lost subject visibility', ['EXCESSIVE_OCCLUSION'], [
        P('m10_analysis_probe', 'visibility: the wall occludes A in shot 2 -> EXCESSIVE_OCCLUSION by Wall (ENVIRONMENT)')]),
    ('missing prop', ['CINEMATIC_OBJECT_MISSING', 'CINEMATIC_PROP_NOT_EXCLUDED'], [
        M('E', 'refusal_manage_props_MISSING'),
        P('m10_performance_probe', 'prop whose original was not excluded at candidate creation -> CINEMATIC_PROP_NOT_EXCLUDED')]),
    ('unreachable interaction target', ['OUT_OF_REACH', 'TARGET_UNREACHED'], [
        P('m10_negative_probe', 'unreachable target: REACH toward an actor 3 m away -> OUT_OF_REACH against the registered '
                                'reach (numeric, from the registry) and the plan status is warning'),
        P('m10_negative_probe', 'control: a point 0.3 m in front of Reacher -> REACHABLE with the same reach'),
        P('m10_negative_probe', 'unreachable target: contact QA -> NO_CONTACT and TARGET_UNREACHED with a measured minimum '
                                'distance (> 1 m for actors 3 m apart; never zero or UNKNOWN)')]),
    ('excessive actor penetration', ['ACCIDENTAL_COLLISION', 'PENETRATION'], [
        P('m10_analysis_probe', 'collisions: bodies interpenetrating at 40-45 -> ACCIDENTAL_COLLISION (ERROR, depth > 0.02 m)'),
        P('probe_m8_parity_regression', 'pendant pushed 12 cm into the chest: penetration still detected (guard not weakened)')]),
    ('broken camera cut timing', ['CUT_OUTSIDE_RANGE', 'CUT_ORDER_INVALID', 'CUT_FIRST_NOT_AT_START', 'TIMELINE_CUTS_OUTSIDE'], [
        P('m10_cameras_probe', 'cut outside the timeline -> CUT_OUTSIDE_RANGE'),
        P('m10_cameras_probe', 'cuts out of order -> CUT_ORDER_INVALID'),
        P('m10_cameras_probe', 'first cut not at frame_start -> CUT_FIRST_NOT_AT_START'),
        P('m10_cameras_probe', 'range that would orphan cuts -> TIMELINE_CUTS_OUTSIDE'),
        P('m10_cameras_probe', 'deleted camera -> broken cut detected (marker camera None, camera missing)')]),
    ('stale cinematic candidate', ['CINEMATIC_STALE_CANDIDATE'], [
        P('m10_analysis_probe', 'a candidate edited outside the tools is refused before any frame_set -> CINEMATIC_STALE_CANDIDATE'),
        P('m10_cameras_probe', 'external deletion makes the candidate stale -> CINEMATIC_STALE_CANDIDATE'),
        P('m10_review_probe', 'review after an external candidate edit -> CINEMATIC_STALE_CANDIDATE'),
        M('E', 'refusal_create_cuts_CINEMATIC_STALE_CANDIDATE')]),
    ('invalid frame-rate conversion', ['CINEMATIC_FPS_MISMATCH', 'FACIAL_FPS_MISMATCH', 'CLIP_RETIME_REQUIRED'], [
        P('m10_foundation_probe', 'brief at another fps is refused -> CINEMATIC_FPS_MISMATCH'),
        P('m10_planning_probe', 'brief at another fps -> CINEMATIC_FPS_MISMATCH'),
        P('m10_performance_probe', 'M7 performance at another fps -> FACIAL_FPS_MISMATCH'),
        P('m10_performance_probe', 'a scaled clip without explicit retime -> CLIP_RETIME_REQUIRED')]),
    ('unsupported render backend', ['CINEMATIC_RENDER_OPTIX_REQUIRED'], [
        P('m10_planning_probe', 'Cycles in a factory process without OptiX -> CINEMATIC_RENDER_OPTIX_REQUIRED'),
        M('E', 'refusal_render_plan_CINEMATIC_RENDER_OPTIX_REQUIRED')]),
]


def rel(path):
    return str(Path(path).resolve().relative_to(ROOT)).replace('\\', '/')


def probe_checks(probe):
    report = next((PROBES / probe).glob('*.json'))
    return rel(report), {c['name']: c for c in json.loads(report.read_text(encoding='utf-8'))['checks']}


def mcp_checks(run):
    report = MCP / ('report-' + run + '.json')
    return rel(report), json.loads(report.read_text(encoding='utf-8'))['checks']


def excerpt(value):
    text = json.dumps(value, default=str)
    return text if len(text) <= 400 else text[:400] + '...'


def main():
    cache = {}
    rows = []
    for scenario, codes, refs in SCENARIOS:
        evidence = []
        for kind, source, name in refs:
            if (kind, source) not in cache:
                cache[(kind, source)] = probe_checks(source) if kind == 'probe' else mcp_checks(source)
            path, checks = cache[(kind, source)]
            found = checks.get(name)
            evidence.append({'kind': 'BLENDER_PROBE' if kind == 'probe' else 'MCP_QUALIFICATION', 'report': path,
                             'check': name, 'found': found is not None,
                             'passed': bool(found and found.get('passed')),
                             'data': excerpt((found or {}).get('data') if kind == 'probe' else
                                             {k: v for k, v in (found or {}).items() if k != 'passed'})})
        rows.append({'scenario': scenario, 'expected_codes': codes, 'evidence': evidence,
                     'covered': bool(evidence) and all(e['found'] and e['passed'] for e in evidence)})
    out = {'schema': 'production.m10_negative_tests.v1', 'required': 20, 'scenarios': rows,
           'covered': sum(r['covered'] for r in rows), 'real_blender_checks': sum(len(r['evidence']) for r in rows),
           'note': 'Safe rejection counts as correct behaviour (M10 prompt, phase 40). Every check ran in real Blender: '
                   'own background probes (tests/blender/m10_*_probe.py) or the MCP qualification on an own Blender at 9877 '
                   '(tests/integration/run_m10_mcp.py). Unit tests of the same codes are not counted here.'}
    target = ART / 'negative-tests.json'
    target.write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding='utf-8')
    print('covered', out['covered'], '/', len(rows), 'checks', out['real_blender_checks'])
    for row in rows:
        if not row['covered']:
            print('NOT COVERED', row['scenario'], [(e['check'], e['found'], e['passed']) for e in row['evidence']])


if __name__ == '__main__':
    main()
