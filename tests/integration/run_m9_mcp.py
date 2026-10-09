"""M9 qualification over actual MCP stdio and the upstream Blender addon.

Only the coordinator runs this script. Fixtures, process handle and loopback port
are owned by this run. Visual feedback is read from explicit external files after
the reviewer has actually inspected the generated source/candidate lighting pairs.
"""
from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import traceback
from uuid import uuid4

from run_m5_mcp import Run, listening, session
from run_m8_mcp import hidden_startup
from blender_mcp.production.common.bridge import Bridge
from blender_mcp.production.common.storage import sha256
from blender_mcp.production.materials.service import backend_fingerprint, review_gate
from blender_mcp.production.materials.tools import NAMES

ROOT = Path(__file__).resolve().parents[2]
LIGHTS = ['NEUTRAL', 'SOFT', 'HARD', 'GRAZING']


def write(path, data):
    with Path(path).open('x', encoding='utf-8') as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False, default=str)


class M9Run(Run):
    def __init__(self, folder):
        super().__init__(folder)
        self.key_tools = set()
        (folder / 'calls').mkdir()

    async def call(self, name, arguments=None):
        args = deepcopy(arguments or {})
        if name in self.key_tools and 'operation_id' not in args:
            args['operation_id'] = 'm9-' + uuid4().hex
        envelope = await super().call(name, args)
        path = self.folder / 'calls' / (str(len(self.report['calls'])).zfill(4) + '-' + name + '.json')
        write(path, envelope)
        self.report['calls'][-1]['response_path'] = str(path)
        self.save()
        return envelope


def material_args(case):
    return {'object_name': case['object_name'], 'material_name': case['material_name']}


def definition(info, case_name, profile=None, channels=None, parameters=None):
    case = info['cases'][case_name]
    value = deepcopy(case['material_definition'])
    if profile:
        value['profile'] = profile
    if parameters:
        value['parameters'] = parameters
    if channels:
        for channel in channels:
            semantic = channel.upper() if channel != 'opacity' else 'ALPHA'
            row = info['texture_definitions'][semantic]
            value['channels'][channel] = {'image': row['image'], 'colorspace': row['colorspace'],
                                         'component': 'COLOR' if channel in {'base_color', 'normal', 'emission'} else 'A' if channel == 'opacity' else 'R'}
    if 'normal' in value['channels']:
        value['normal_convention'] = 'OPENGL'
    if 'height' in value['channels']:
        value['height_mode'] = 'BUMP'
    return value


async def visual_feedback(run, label, result, timeout):
    rendered = result['render']
    request = run.folder / ('review-request-' + label + '.json')
    feedback_path = run.folder / ('review-feedback-' + label + '.json')
    write(request, {'label': label, 'material': result['material'], 'render': rendered,
                    'required_fields': ['reviewer', 'observations', 'findings', 'decision',
                                        'artifact_reference', 'lights_reviewed'],
                    'feedback_path': str(feedback_path),
                    'instruction': 'Mirar los pares reales; no inferir aceptación de métricas ni rellenar por defecto.'})
    run.report['awaiting_visual_review'] = str(request)
    run.save()
    print('M9_VISUAL_REVIEW_REQUIRED=' + str(request), flush=True)
    deadline = time.monotonic() + timeout
    while not feedback_path.is_file():
        if time.monotonic() > deadline:
            raise TimeoutError('Falta revisión visual real para ' + label)
        await asyncio.sleep(1)
    review = json.loads(feedback_path.read_text(encoding='utf-8-sig'))
    review_gate(review, rendered)
    reviewed = await run.good('material_review', {'material': result['material'], 'options': review, 'apply': True})
    run.report.pop('awaiting_visual_review', None)
    run.report.setdefault('visual_reviews', {})[label] = {'request': str(request), 'feedback': str(feedback_path),
                                                        'decision': review['decision'], 'reviewed': reviewed}
    run.save()
    return review


async def qualify_demo(run, args, info, label, case_name, material_definition, render_tool='material_render_multilighting', create_tool='material_create_pbr'):
    case = info['cases'][case_name]
    built = await run.good(create_tool, material_args(case) | {'definition': material_definition, 'apply': True})
    reference = built['material']
    qa = await run.good('material_quality_report', {'material': reference})
    run.check(label + '_technical_qa', qa.get('passed') is True, issues=qa.get('issues'))
    if not qa.get('passed'):
        raise AssertionError(label + ': QA técnica no válida')
    applied = await run.good('material_apply_candidate', {'material': reference, 'apply': True})
    run.check(label + '_owned_assignment', applied['original_assignments_replaced'] is False and applied['source_preserved'])
    rendered = await run.good(render_tool, {'material': reference, 'apply': True,
                             'options': {'width': args.preview_size, 'height': args.preview_size,
                                         'lighting_set': LIGHTS, 'views': ['THREE_QUARTER'], 'engine': 'BLENDER_EEVEE'}})
    run.report.setdefault('demos', {})[label] = {'material': reference, 'case': case_name,
                                               'definition': material_definition, 'qa': qa, 'render': rendered['render']}
    run.save()
    if args.smoke:
        run.check(label + '_smoke_render_only', rendered['render']['state_restored'] is True)
        return rendered
    review = await visual_feedback(run, label, rendered, args.review_timeout)
    if review['decision'] != 'ACCEPT':
        rejected = await run.good('material_reject', {'material': reference, 'apply': True,
                                  'options': {'reason': 'Revisión visual: ' + review['observations']}})
        run.report['demos'][label]['rejection'] = rejected
        raise AssertionError(label + ': revisión visual no aceptada; conservar y evaluar el fallo')
    if label == 'C_WET':
        # A finite fixture experiment tests persistent refinement limits. Every version
        # is actually rendered and reviewed; earlier ACCEPT means suitable for the
        # synthetic comparison, never a fabricated artistic rejection.
        for cycle, roughness in enumerate((.18, .14, .10), 1):
            changed = deepcopy(material_definition)
            changed.setdefault('channels', {}).pop('roughness', None)
            changed.setdefault('parameters', {})['roughness'] = roughness
            refined = await run.good('material_refine', {'material': reference, 'definition': changed,
                                    'options': {'reason': 'Experimento de fixture: comparar anchura del reflejo con roughness ' + str(roughness)},
                                    'apply': True})
            run.check('refinement_cycle_' + str(cycle), refined['refinement_cycles'] == cycle)
            rendered = await run.good('material_render_multilighting', {'material': reference, 'apply': True,
                                     'options': {'width': args.preview_size, 'height': args.preview_size,
                                                 'lighting_set': LIGHTS, 'views': ['THREE_QUARTER']}})
            review = await visual_feedback(run, label + '_refine_' + str(cycle), rendered, args.review_timeout)
            if review['decision'] != 'ACCEPT':
                raise AssertionError('La variante de refinamiento requiere decisión: ' + str(review))
        fourth = await run.call('material_refine', {'material': reference, 'definition': changed,
                                'options': {'reason': 'Prueba negativa del cuarto ciclo'}, 'apply': True})
        run.check('fourth_refinement_rejected', fourth['status'] == 'error' and 'REFINEMENT_LIMIT' in fourth.get('error_codes', []))
    accepted = await run.good('material_accept', {'material': reference, 'apply': True})
    run.check(label + '_accepted', accepted['state'] == 'ACCEPTED' and accepted['source_preserved'])
    run.report['demos'][label]['accepted'] = accepted
    run.save()
    return accepted


async def scenario(run, args, info):
    inventory = await run.client.list_tools()
    listed = {t.name: t for t in inventory.tools}
    baseline = json.loads((ROOT / 'tests/fixtures/m9-baseline-tools.json').read_text())['tools']
    run.check('M8_inventory_retained_M9_added', set(listed) == set(baseline) | {'production.' + name for name in NAMES},
              total=len(listed), inherited=len(baseline), new=len(NAMES))
    run.key_tools = {name.removeprefix('production.') for name, tool in listed.items()
                     if 'operation_id' in tool.inputSchema.get('properties', {})}
    run.report['tool_inventory'] = sorted(listed)
    run.report['backend_fingerprint'] = backend_fingerprint()
    await run.good('asset_register_fixture', {'path': info['source_file']})
    asset = await run.good('asset_status')
    run.check('controlled_fixture_role', asset['asset']['role'] == 'FIXTURE')
    run.report['scene_audit'] = await run.good('scene_audit')
    metal = info['cases']['metallic']
    for name in ('material_inspect', 'material_analyze_nodes', 'material_compatibility_report',
                 'material_analyze_pbr', 'material_validate_pbr', 'texture_inspect',
                 'texture_validate_channels', 'texture_validate_colorspace', 'texture_dependency_report',
                 'material_validate_height', 'material_validate_uv', 'material_validate_graph', 'material_performance_report'):
        checked = await run.good(name, material_args(metal))
        run.check(name + '_positive_fixture', checked.get('passed') is True, issues=checked.get('issues'))
    normal_case = info['cases']['normal_map']
    for name in ('material_validate_normals', 'material_validate_tangent_workflow'):
        checked = await run.good(name, material_args(normal_case) | {'options': {'normal_convention': 'OPENGL'}})
        run.check(name + '_positive_fixture', checked.get('passed') is True, issues=checked.get('issues'))
    for profile in ('human_skin', 'creature_skin', 'fabric', 'leather', 'metal', 'painted_metal', 'rubber',
                    'bone', 'ice', 'snow', 'wet_surface', 'solid_ice', 'frosted_ice', 'compacted_snow',
                    'wet_fabric', 'wet_skin', 'wet_metal', 'wet_creature'):
        p = await run.good('material_create_profile', {'profile': profile})
        v = await run.good('material_validate_profile', {'profile': profile})
        run.check('profile_' + profile, v.get('passed') is True and p.get('provenance') == 'proposed_starting_configuration')
    proposed = await run.good('material_create_candidate', material_args(metal) |
                              {'definition': definition(info, 'metallic', 'metal'), 'apply': False})
    create_args = {'material': proposed['material'], 'apply': True, 'operation_id': 'm9-idempotent-candidate-' + uuid4().hex[:12]}
    one = await run.call('material_create_candidate', create_args)
    two = await run.call('material_create_candidate', create_args)
    run.check('candidate_idempotent_retry', one['status'] != 'error' and one['execution_id'] == two['execution_id']
              and one['data']['candidate'] == two['data']['candidate'])
    if one['status'] == 'error':
        raise AssertionError('Candidata: ' + str(one['errors']))
    built = await run.good('material_build_shader', {'material': proposed['material'], 'apply': True})
    await run.good('material_status', {'material': proposed['material']})
    qa = await run.good('material_quality_report', {'material': proposed['material']})
    run.check('candidate_built_graph_valid', qa.get('passed') is True, qa=qa)
    if not qa.get('passed'):
        raise AssertionError('Candidata construida no pasa QA: ' + str(qa.get('issues')))
    await run.good('material_reject', {'material': proposed['material'], 'apply': True,
                   'options': {'reason': 'Candidata técnica de idempotencia; retirada conservando sus artefactos.'}})
    await qualify_demo(run, args, info, 'A_PBR', 'metallic',
                        definition(info, 'metallic', 'metal', ['metallic', 'normal', 'ao', 'height']))
    if args.smoke:
        return
    feature_case = info['cases']['dielectric']
    feature = await run.good('material_create_pbr', material_args(feature_case) | {'apply': True})
    feature_ref = feature['material']
    await run.good('material_apply_profile', {'material': feature_ref, 'profile': 'creature_skin', 'apply': True})
    for name, values in (
        ('material_configure_sss', {'subsurface_weight': .2, 'subsurface_scale': .01}),
        ('material_configure_transmission', {'transmission_weight': .25, 'ior': 1.4, 'roughness': .3}),
        ('material_configure_opacity', {'alpha': .6}),
        ('material_configure_emission', {'emission_color': [.1, .2, .8, 1.], 'emission_strength': 1.5}),
    ):
        configured = await run.good(name, {'material': feature_ref, 'options': {'parameters': values}, 'apply': True})
        report = await run.good('material_quality_report', {'material': feature_ref})
        run.check(name + '_technical', configured['source_preserved'] and report.get('passed') is True,
                  issues=report.get('issues'))
    await run.good('material_reject', {'material': feature_ref, 'apply': True,
                   'options': {'reason': 'Prueba técnica de configuraciones; no es una propuesta artística aceptada.'}})
    skin_def = definition(info, 'skin_sss', 'human_skin', ['normal', 'height'], {'subsurface_scale': .02})
    await qualify_demo(run, args, info, 'B_ORGANIC', 'skin_sss', skin_def, 'material_render_preview')
    for label, case_name, profile in (('C_ICE', 'ice_transmission', 'frosted_ice'),
                                      ('C_SNOW', 'snow', 'compacted_snow'), ('C_WET', 'wet_surface', 'wet_surface')):
        await qualify_demo(run, args, info, label, case_name, definition(info, case_name, profile),
                            'material_compare_before_after')
    for case_name in ('multi_udim', 'single_material_multi_udim', 'multiple_materials'):
        case = info['cases'][case_name]
        result = None
        for name in ('material_validate_uv', 'material_validate_udim', 'material_analyze_texture_sets', 'texture_dependency_report'):
            result = await run.good(name, material_args(case) | {'definition': case['material_definition']})
            run.check('D_' + case_name + '_' + name, result.get('passed') is True, issues=result.get('issues'))
        handoff = await run.good('material_create_handoff', material_args(case) |
                                {'definition': case['material_definition'], 'apply': True,
                                 'options': {'expected_maps': ['BaseColor', 'Normal', 'Roughness'],
                                             'mesh_maps_expected': ['Normal', 'AO', 'Thickness']}})
        content = json.loads(Path(handoff['handoff']).read_text(encoding='utf-8'))
        run.check('D_' + case_name + '_neutral_handoff', content['application'] == 'neutral'
                  and content['bake_readiness'] == 'UNKNOWN' and content['exports'] == [], handoff=handoff)
        if case_name == 'single_material_multi_udim':
            run.check('D_single_material_five_tiles', len(content['material_mapping']) == 1 and len(content['UDIMs']) == 5)
        run.report.setdefault('handoffs', {})[case_name] = handoff
    expected = {
        'missing_image': {'SHADER_IMAGE_MISSING'},
        'roughness_wrong_colorspace': {'TEXTURE_DATA_COLORSPACE'},
        'normal_broken_wiring': {'NORMAL_IMAGE_DIRECT_CONNECTION'},
        'missing_udim_tile': {'UDIM_TILE_MISSING', 'TEXTURE_UDIM_TILE_MISSING'},
        'unsupported_shader': {'SHADER_FEATURE_UNSUPPORTED'},
        'invalid_graph': {'SHADER_OUTPUT_DISCONNECTED'},
    }
    for case_name, wanted in expected.items():
        result = await run.good('material_quality_report', material_args(info['cases'][case_name]))
        codes = {i['code'] for i in result.get('issues', [])}
        run.check('E_' + case_name, bool(wanted & codes) and result.get('passed') is False, codes=sorted(codes))
    await stale_negative(run, args, info)
    await master_negative(run, args, info)
    run.report['core_tool_calls'] = sorted({c['tool'].removeprefix('production.') for c in run.report['calls']} & NAMES)
    missing = NAMES - set(run.report['core_tool_calls'])
    run.check('all_M9_tools_exercised', not missing, missing=sorted(missing))


def own_fixture_payload(port, fixture, body, params=None):
    """Sequential defect injection in this run's fixture; never a second MCP server."""
    prefix = r'''
import bpy
if bpy.data.filepath.replace('\\','/') != params['fixture'].replace('\\','/'):
    raise ValueError('OWN_FIXTURE_REQUIRED: sólo la fixture creada por este runner')
'''
    bridge = Bridge(port=port)
    try:
        return bridge.run(prefix + body, dict(params or {}, fixture=str(fixture)))
    finally:
        bridge.close()


async def stale_negative(run, args, info):
    plan = await run.good('material_create_pbr', material_args(info['cases']['dielectric']) | {'apply': True})
    candidate = plan['candidate']
    changed = own_fixture_payload(args.port, info['source_file'], r'''
material = bpy.data.materials.get(params['candidate_material'])
if not material or material.get('production_m9_owned') is not True:
    raise ValueError('OWN_CANDIDATE_REQUIRED')
before = list(material.diffuse_color)
material.diffuse_color = (0.123,0.234,0.345,1)
result = {'before':before,'after':list(material.diffuse_color),'owned_fixture_only':True}
''', candidate)
    stale = await run.call('material_status', {'material': plan['material']})
    run.check('E_stale_candidate_rejected', stale['status'] == 'error'
              and 'MATERIAL_STALE_CANDIDATE' in stale.get('error_codes', []), injection=changed)


async def master_negative(run, args, info):
    master = run.folder / 'synthetic-master-negative.blend'
    own_fixture_payload(args.port, info['source_file'], r'''
outcome = bpy.ops.wm.save_as_mainfile(filepath=params['target'],copy=True,relative_remap=True)
if 'FINISHED' not in outcome:
    raise RuntimeError('MASTER_FIXTURE_COPY_FAILED')
outcome = bpy.ops.wm.open_mainfile(filepath=params['target'],load_ui=False,use_scripts=False)
if 'FINISHED' not in outcome:
    raise RuntimeError('MASTER_FIXTURE_OPEN_FAILED')
for scene in bpy.data.scenes:
    scene.blendermcp_port = params['port']
result = {'path':bpy.data.filepath}
''', {'target': str(master), 'port': args.port})
    original = sha256(master)
    await run.good('asset_register_master', {'confirm_master': True})
    before = len(list((run.folder / 's/checkpoints').glob('*')))
    denied = await run.call('material_create_pbr', material_args(info['cases']['dielectric']) | {'apply': True})
    after = len(list((run.folder / 's/checkpoints').glob('*')))
    run.check('MASTER_rejected_before_checkpoint', denied['status'] == 'error'
              and 'MASTER_ASSET_READ_ONLY' in denied.get('error_codes', []) and before == after,
              before=before, after=after)
    run.check('MASTER_file_unchanged', sha256(master) == original)


async def main_async(args, folder, info, run):
    with (folder / 'mcp-stderr.log').open('w', encoding='utf-8') as log:
        async with session(folder, args.port, log) as client:
            run.client = client
            if args.qualification_fixes:
                # Requalify only the two harness contracts corrected after a completed
                # visual/demo run. Earlier failures remain unmodified and explicitly scoped.
                run.report['backend_fingerprint'] = backend_fingerprint()
                run.report['tool_inventory'] = sorted(t.name for t in (await client.list_tools()).tools)
                await run.good('asset_register_fixture', {'path': info['source_file']})
                for name in ('material_validate_normals', 'material_validate_tangent_workflow'):
                    checked = await run.good(name, material_args(info['cases']['normal_map']) |
                                             {'options': {'normal_convention': 'OPENGL'}})
                    run.check(name + '_positive_fixture', checked.get('passed') is True, issues=checked.get('issues'))
                await master_negative(run, args, info)
            else:
                await scenario(run, args, info)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--blender', default=r'C:\Program Files\Blender Foundation\Blender 5.2\blender.exe')
    parser.add_argument('--port', type=int, default=9877)
    parser.add_argument('--preview-size', type=int, default=256)
    parser.add_argument('--review-timeout', type=float, default=3600)
    parser.add_argument('--smoke', action='store_true')
    parser.add_argument('--qualification-fixes', action='store_true',
                        help='Sólo contratos de inspección normal y registro MASTER sintético; no vuelve a ejecutar demos visuales')
    args = parser.parse_args()
    if args.port == 9876 or not 1024 <= args.port <= 65535 or listening(args.port):
        raise SystemExit('Puerto dedicado inválido u ocupado; no se contacta ni detiene una instancia ajena.')
    if not 64 <= args.preview_size <= 512 or args.review_timeout <= 0:
        raise SystemExit('Capacidad o tiempo de revisión inválido.')
    folder = Path(tempfile.gettempdir()) / ('bpm9-' + uuid4().hex[:8])
    folder.mkdir()
    run = M9Run(folder)
    blender = None
    info = None
    phase = 'BUILD_FIXTURES'
    try:
        build_script = folder / 'build.py'
        build_script.write_text('import sys\nfrom pathlib import Path\nsys.path.insert(0, ' +
                                repr(str(ROOT / 'tests/blender')) + ')\nimport fixtures_m9\nfixtures_m9.build(' +
                                repr(str(folder / 'f')) + ')\n', encoding='utf-8')
        built = subprocess.run([args.blender, '--background', '--factory-startup', '--disable-autoexec',
                                '--python', str(build_script)], capture_output=True, text=True,
                               encoding='utf-8', errors='replace', timeout=180, startupinfo=hidden_startup())
        (folder / 'fixture-build.log').write_text(built.stdout + '\n' + built.stderr, encoding='utf-8')
        if built.returncode or not (folder / 'f/fixtures.json').exists():
            raise RuntimeError('Falló la construcción de fixtures; consultar fixture-build.log')
        info = json.loads((folder / 'f/fixtures.json').read_text(encoding='utf-8'))
        phase = 'OWN_BLENDER_START'
        with (folder / 'blender-stdout.log').open('w', encoding='utf-8') as log:
            blender = subprocess.Popen([args.blender, '--factory-startup', '--disable-autoexec', info['source_file'],
                                         '--python', str(ROOT / 'tests/integration/m4_own_blender_start.py'),
                                         '--', str(args.port)], stdout=log, stderr=subprocess.STDOUT,
                                        startupinfo=hidden_startup())
            deadline = time.monotonic() + 90
            while not listening(args.port):
                if time.monotonic() > deadline or blender.poll() is not None:
                    raise RuntimeError('No arrancó el addon en el Blender propio.')
                time.sleep(.5)
            phase = 'MCP_QUALIFICATION'
            asyncio.run(main_async(args, folder, info, run))
    except Exception as exc:
        run.report.update(exception=repr(exc), failure_phase=phase, traceback=traceback.format_exc())
        run.check('runner_failure_' + phase, False, error=repr(exc))
    finally:
        if blender is not None and blender.poll() is None:
            blender.terminate()
            try:
                blender.wait(timeout=30)
            except subprocess.TimeoutExpired:
                blender.kill()
                blender.wait(timeout=30)
    if info:
        original = sha256(Path(info['source_file']))
        run.check('source_blend_unchanged', original == info['source_sha256'])
        run.check('source_backup_unchanged', sha256(Path(info['source_backup'])) == info['backup_sha256'])
        run.check('texture_files_unchanged', all(sha256(Path(row['path'])) == row['sha256'] for row in info['files']))
        run.report['fixture_metadata'] = info
    run.report.update(smoke_only=args.smoke, blender_port=args.port,
                      qualification_scope='NORMAL_AND_MASTER_HARNESS_FIXES' if args.qualification_fixes else 'SMOKE' if args.smoke else 'FULL',
                      owned_blender_pid=blender.pid if blender else None, user_port_9876_untouched=True,
                      backend_fingerprint_after=backend_fingerprint(), workdir=str(folder))
    run.check('backend_unchanged_during_qualification',
              run.report.get('backend_fingerprint') == run.report['backend_fingerprint_after'])
    run.report['status'] = 'pass' if 'exception' not in run.report and all(r['passed'] for r in run.report['checks'].values()) else 'fail'
    run.save()
    archive = ROOT / 'test-artifacts/m9' / folder.name
    archive.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(folder / 'report.json', archive / 'report.json')
    print('M9_MCP_REPORT=' + str(archive / 'report.json') + ' workdir=' + str(folder) + ' status=' + run.report['status'], flush=True)
    raise SystemExit(0 if run.report['status'] == 'pass' else 1)


if __name__ == '__main__':
    main()
