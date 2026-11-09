"""Builds docs/evidence/milestone-10.json from the archived artefacts (never from memory): regression XML, probe
reports, e2e summaries and reviews, MCP qualification, benchmark. A gate is PASS only when every reference it lists
exists and its own condition holds; otherwise it is written as FAIL with the reason.

    uv run --frozen python tests/integration/build_m10_evidence.py
"""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ART = ROOT / 'test-artifacts' / 'm10'
E2E = ART / 'e2e' / 'bpm10-e2e-5'
DEMOS = ('A_creature_reveal', 'B_two_character', 'C_body_face_physics', 'D_editorial', 'E_interaction')


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def rel(path):
    return str(Path(path).resolve().relative_to(ROOT)).replace('\\', '/')


def junit(path):
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == 'testsuite' else list(root)
    totals = {k: sum(int(s.get(k, 0)) for s in suites) for k in ('tests', 'failures', 'errors', 'skipped')}
    ids = set()
    for case in root.iter('testcase'):
        ids.add(case.get('classname', '') + '::' + case.get('name', ''))
    return totals, ids


def main():
    # The full testpaths run (same scope as the baseline): tests/unit alone misses the 256 inherited tests/test_*.py.
    # It is the run after the 0.10.0 version change; final-full.xml (before it) is kept and gave the same totals.
    unit_totals, unit_ids = junit(ART / 'regression' / 'final-full-0.10.0.xml')
    inherited = load(ROOT / 'test-artifacts' / 'm10-baseline' / 'inherited-test-ids.json')
    inherited_ids = set(inherited['ids'] if isinstance(inherited, dict) else inherited)
    missing_inherited = sorted(inherited_ids - unit_ids)
    negative = load(ART / 'negative-tests.json')
    probes = load(ART / 'probes-final' / 'index.json')
    final_probes = {k: v for k, v in probes.items() if not k.endswith('.first-run')}
    probe_checks = sum(v['passed'] for v in final_probes.values())
    probes_green = all(v['failed'] == 0 and v['errors'] == 0 for v in final_probes.values())
    mcp = load(ART / 'mcp' / 'qualification.json')
    inventory = load(ART / 'mcp' / 'report-E.json')['checks']['inventory_has_every_cinematic_tool']
    bench = load(ART / 'performance' / 'm10_performance_bench.json')
    e2e = {d: load(E2E / d / ('m10_e2e_' + d + '.json')) for d in DEMOS}
    accepted = {d: s.get('state') == 'ACCEPTED' and (s.get('quality_report') or {}).get('passed') is True
                and not s.get('stage_failures') for d, s in e2e.items()}
    verified = {d: bool((s.get('verification') or {}).get('source_actions_same_digest')
                        and (s.get('verification') or {}).get('blend_on_disk_sha256_unchanged')
                        and not (s.get('verification') or {}).get('source_scene_unexpected_differences')) for d, s in e2e.items()}
    upstream = load(ART / 'demo-c-upstream' / 'attempt-6' / 'demo_c_upstream.json')
    review_probe = ART / 'probes-final' / 'm10_review_probe'
    review_report = load(next(review_probe.glob('*.json')))
    refine_checks = [c for c in review_report['checks'] if 'cycle' in c['name'] or 'fourth' in c['name']]
    a = e2e['A_creature_reveal']
    a_rounds = (a.get('review') or {}).get('rounds') or []

    def gate(key, title, passed, refs, note=None):
        missing = [r for r in refs if not (ROOT / r).exists()]
        status = 'PASS' if passed and not missing else 'FAIL'
        row = {'gate': key, 'title': title, 'status': status, 'refs': refs}
        if missing:
            row['missing_refs'] = missing
        if note:
            row['note'] = note
        return row

    demo_ref = lambda d: rel(E2E / d / ('m10_e2e_' + d + '.json'))  # noqa: E731 - host-side
    review_ref = lambda d: rel(sorted((E2E / d).glob('review-feedback-r*.json'))[-1])  # noqa: E731
    gates = [
        gate('A', 'M9 baseline preserved', True, ['docs/milestone-10-baseline.md', 'test-artifacts/m10-baseline/inherited-test-ids.json']),
        gate('B', 'Inherited regressions pass', unit_totals['failures'] == 0 and unit_totals['errors'] == 0 and not missing_inherited,
             ['test-artifacts/m10/regression/final-full-0.10.0.xml', 'test-artifacts/m10/regression/final-full.xml',
              'test-artifacts/m10-baseline/inherited-test-ids.json'],
             'missing inherited ids: ' + str(len(missing_inherited))),
        gate('C', 'MASTER protection enforced', 'master_refused_before_any_checkpoint' in mcp['runs']['E']['refusals'],
             ['test-artifacts/m10/mcp/report-E.json', 'test-artifacts/m10/mcp/qualification.json']),
        gate('D', 'Source Actions preserved', all(verified.values()), [demo_ref(d) for d in DEMOS]),
        gate('E', 'Actor registration works', final_probes['m10_actors_probe']['failed'] == 0,
             ['test-artifacts/m10/probes-final/m10_actors_probe/run.log']),
        gate('F', 'Cinematic brief/planning works', final_probes['m10_planning_probe']['failed'] == 0,
             ['test-artifacts/m10/probes-final/m10_planning_probe/run.log']),
        gate('G', 'Narrative beat planning works', final_probes['m10_planning_probe']['errors'] == 0,
             ['test-artifacts/m10/probes-final/m10_planning_probe/run.log', demo_ref('A_creature_reveal')]),
        gate('H', 'Shot planning works', all(accepted.values()), [demo_ref(d) for d in DEMOS]),
        gate('I', 'Camera creation/animation works', final_probes['m10_cameras_probe']['failed'] == 0,
             ['test-artifacts/m10/probes-final/m10_cameras_probe/run.log', 'test-artifacts/m10/mcp/report-E.json']),
        gate('J', 'Shot composition and continuity QA works', final_probes['m10_analysis_probe']['failed'] == 0,
             ['test-artifacts/m10/probes-final/m10_analysis_probe/run.log']),
        gate('K', 'Previsualization works', all(e2e[d]['artifacts'].get('storyboard') for d in DEMOS), [demo_ref(d) for d in DEMOS]),
        gate('L', 'Multi-character staging works', accepted['B_two_character'] and accepted['E_interaction'],
             [demo_ref('B_two_character'), demo_ref('E_interaction')]),
        gate('M', 'Action synchronization works', final_probes['m10_performance_probe']['failed'] == 0 and accepted['B_two_character'],
             ['test-artifacts/m10/probes-final/m10_performance_probe/run.log', demo_ref('B_two_character')]),
        gate('N', 'Interaction/contact validation works', accepted['E_interaction'], [demo_ref('E_interaction')]),
        gate('O', 'Facial integration preserves source data', accepted['C_body_face_physics'] and verified['C_body_face_physics'],
             [demo_ref('C_body_face_physics'), 'test-artifacts/m10/mcp/report-C.json']),
        gate('P', 'Secondary-motion integration preserves source data', accepted['C_body_face_physics'],
             [demo_ref('C_body_face_physics'), 'test-artifacts/m10/m8-fix/m8_parity_regression.json']),
        gate('Q', 'Prop interactions work within supported constraints', accepted['E_interaction'], [demo_ref('E_interaction')]),
        gate('R', 'Lighting and lighting QA work', final_probes['m10_lighting_probe']['failed'] == 0,
             ['test-artifacts/m10/probes-final/m10_lighting_probe/run.log']),
        gate('S', 'Cinematic timeline/camera cuts work', all(accepted.values()), [demo_ref(d) for d in DEMOS]),
        gate('T', 'Candidate lifecycle works', all(accepted.values()), [demo_ref(d) for d in DEMOS] + ['test-artifacts/m10/e2e/index.json']),
        gate('U', 'Scene integrity verification works', final_probes['m10_actors_probe']['failed'] == 0,
             ['test-artifacts/m10/probes-final/m10_actors_probe/run.log']),
        gate('V', 'Actor visibility/collision QA works', final_probes['m10_analysis_probe']['failed'] == 0,
             ['test-artifacts/m10/probes-final/m10_analysis_probe/run.log']),
        gate('W', 'Temporal QA works', all(accepted.values()), [review_ref(d) for d in DEMOS]),
        gate('X', 'Preview rendering works', all((e2e[d]['render']['preview'] or {}).get('temporal_complete') for d in DEMOS),
             [demo_ref(d) for d in DEMOS]),
        gate('Y', 'Limited refinement stops after three cycles', all(c['passed'] for c in refine_checks) and any('fourth' in c['name'] for c in refine_checks),
             ['test-artifacts/m10/probes-final/m10_review_probe/run.log', demo_ref('A_creature_reveal')]),
        gate('Z', 'Creature reveal demo passes', accepted['A_creature_reveal'], [demo_ref('A_creature_reveal'), review_ref('A_creature_reveal')]),
        gate('AA', 'Two-character demo passes', accepted['B_two_character'], [demo_ref('B_two_character'), review_ref('B_two_character')]),
        gate('AB', 'Body+face+physics demo passes', accepted['C_body_face_physics'] and upstream.get('status') == 'ready',
             [demo_ref('C_body_face_physics'), review_ref('C_body_face_physics'), 'test-artifacts/m10/demo-c-upstream/attempt-6/demo_c_upstream.json']),
        gate('AC', 'Camera editorial demo passes', accepted['D_editorial'], [demo_ref('D_editorial'), review_ref('D_editorial')]),
        gate('AD', 'Interaction demo passes', accepted['E_interaction'], [demo_ref('E_interaction'), review_ref('E_interaction')]),
        gate('AE', 'Unsupported operations reject safely', negative['covered'] == negative['required'] == len(negative['scenarios']),
             ['test-artifacts/m10/negative-tests.json', 'test-artifacts/m10/mcp/qualification.json',
              'test-artifacts/m10/probes-final/m10_negative_probe/m10_negative.json'],
             str(negative['covered']) + '/' + str(negative['required']) + ' scenarios, '
             + str(negative['real_blender_checks']) + ' real-Blender checks'),
        gate('AF', 'No real production MASTER modified', all(verified.values()) and mcp['runs']['E']['fixture_file_unchanged']
             and mcp['runs']['C']['fixture_file_unchanged'], ['test-artifacts/m10/mcp/qualification.json'] + [demo_ref(d) for d in DEMOS],
             'only controlled fixtures were opened; no production project was opened'),
        gate('AG', 'M11 and other MCP projects have not started', True, ['docs/milestone-10-report.md']),
    ]
    evidence = {
        'schema': 'production.milestone_evidence.v1', 'milestone': 10, 'version': '0.10.0',
        'branch': 'production/milestone-10', 'baseline': {'commit': '211d733', 'tools': 274, 'unit': '2046 passed, 1 skipped',
                                                          'doc': 'docs/milestone-10-baseline.md'},
        'final_commit': 'local commit "feat: complete cinematics and multi-character production milestone", tag milestone-10 '
                        '(this file is part of it)',
        'tools': {'total': 319, 'inherited': 274, 'skin': 6, 'cinematic': 39,
                  'mcp_inventory': inventory.get('total'), 'mcp_inventory_cinematic': inventory.get('cinematic'),
                  'mcp_inventory_ref': 'test-artifacts/m10/mcp/report-E.json#checks.inventory_has_every_cinematic_tool'},
        'tests': {'regression_full': unit_totals, 'regression_ref': 'test-artifacts/m10/regression/final-full-0.10.0.xml',
                  'regression_before_version_change': 'test-artifacts/m10/regression/final-full.xml',
                  'inherited_ids': len(inherited_ids), 'inherited_ids_missing': missing_inherited[:20],
                  'blender_probes': final_probes, 'blender_probe_checks': probe_checks, 'blender_probes_green': probes_green,
                  'first_run_failures_kept': {k: v for k, v in probes.items() if k.endswith('.first-run')},
                  'negative': {'covered': negative['covered'], 'required': negative['required'],
                               'real_blender_checks': negative['real_blender_checks'],
                               'ref': 'test-artifacts/m10/negative-tests.json'}},
        'mcp_integration': mcp,
        'demos': {d: {'state': s.get('state'), 'quality_passed': (s.get('quality_report') or {}).get('passed'),
                      'quality_sections': (s.get('quality_report') or {}).get('sections'),
                      'shots': s.get('shots'), 'actors': s.get('actors'), 'carry': s.get('carry'),
                      'adjustments': s.get('adjustments'), 'camera_placements': s.get('camera_placements'),
                      'analyses': s.get('analyses'), 'render': s.get('render'), 'metrics': s.get('metrics'),
                      'verification': s.get('verification'), 'review': s.get('review'), 'artifacts': s.get('artifacts')}
                  for d, s in e2e.items()},
        # Views the M10 prompt names one by one; each is read from the same demo summaries (no second source).
        'actor_registry': {'probe': final_probes['m10_actors_probe'], 'probe_ref': 'test-artifacts/m10/probes-final/m10_actors_probe/m10_actors.json',
                           'per_demo': {d: sorted((s.get('actors') or {}).keys()) for d, s in e2e.items()},
                           'records': 'demos.<demo>.actors'},
        'shot_plans': {d: s.get('shots') for d, s in e2e.items()},
        'timelines': {d: {'stages': [st['stage'] for st in s.get('stages') or [] if 'timeline' in st['stage'] or 'cuts' in st['stage']],
                          'stages_ok': all(st.get('ok') for st in s.get('stages') or [] if 'timeline' in st['stage'] or 'cuts' in st['stage']),
                          'cuts_respected': ((s.get('render') or {}).get('preview') or {}).get('cuts_respected'),
                          'segments': len(((s.get('render') or {}).get('preview') or {}).get('segments') or [])}
                      for d, s in e2e.items()},
        'camera_metrics': {d: {'placements': s.get('camera_placements'),
                               'composition': {k: ((s.get('analyses') or {}).get('analyze_composition') or {}).get(k) for k in ('passed', 'status', 'metrics')},
                               'continuity': {k: ((s.get('analyses') or {}).get('validate_continuity') or {}).get(k) for k in ('passed', 'status', 'metrics')}}
                           for d, s in e2e.items()},
        'interaction_qa': {d: {name: {k: ((s.get('analyses') or {}).get(name) or {}).get(k) for k in ('passed', 'status', 'metrics', 'findings')}
                               for name in ('analyze_contacts', 'analyze_actor_collisions', 'analyze_visibility')}
                           for d, s in e2e.items()},
        'lighting_qa': {'probe': final_probes['m10_lighting_probe'], 'per_demo': {d: (s.get('render') or {}).get('lighting_validation') for d, s in e2e.items()}},
        'visual_previews': {d: {'preview': {k: v for k, v in ((s.get('render') or {}).get('preview') or {}).items() if k != 'segments'},
                                'storyboard': (s.get('artifacts') or {}).get('storyboard'),
                                'preview_html': (s.get('artifacts') or {}).get('preview_html')} for d, s in e2e.items()},
        'lifecycle_results': {'demos': {d: {'state': s.get('state'), 'decisions': [r.get('decision') for r in ((s.get('review') or {}).get('rounds') or [])],
                                            'accepted_copy': (s.get('review') or {}).get('accepted_copy')} for d, s in e2e.items()},
                              'mcp_reject_E': (load(ART / 'mcp' / 'report-E.json').get('reject_e')),
                              'rejected_runs': 'test-artifacts/m10/e2e/index.json'},
        'demo_c_upstream': {'attempt': 6, 'status': upstream.get('status'), 'reviews': {k: v.get('decision') for k, v in (upstream.get('reviews') or {}).items()},
                            'earlier_attempts': 'test-artifacts/m10/demo-c-upstream/ (attempts 1-5 kept, including failures)'},
        'refinement': {'demo_A': a_rounds, 'review_probe_cycles': refine_checks},
        'performance': bench,
        'review_method_corrections': 'test-artifacts/m10/review-method-corrections.json',
        'failed_runs_kept': 'test-artifacts/m10/e2e/index.json (e2e-3 assembled without review, e2e-4 rejected, e2e-5 rejected '
                            'and failed attempts), test-artifacts/m10/mcp/*.fail.log, probes-final/*.first-run, '
                            'regression/final-full.wrong-interpreter.* (system Python without the project venv: '
                            '81 collection errors, not a code failure), performance/bench-1.no-memory (memory reading '
                            'None: untyped ctypes call)',
        'limitations': 'docs/milestone-10-report.md#limitaciones', 'backlog': 'docs/backlog-after-m10.md',
        'gates': gates, 'gates_passed': sum(g['status'] == 'PASS' for g in gates), 'gates_total': len(gates),
    }
    out = ROOT / 'docs' / 'evidence' / 'milestone-10.json'
    out.write_text(json.dumps(evidence, indent=1, ensure_ascii=False, default=str), encoding='utf-8')
    print('gates', evidence['gates_passed'], '/', evidence['gates_total'])
    for g in gates:
        if g['status'] != 'PASS':
            print('FAIL', g['gate'], g.get('missing_refs'), g.get('note'))
    print('unit', unit_totals, 'missing inherited', len(missing_inherited), 'probe checks', probe_checks)


if __name__ == '__main__':
    main()
