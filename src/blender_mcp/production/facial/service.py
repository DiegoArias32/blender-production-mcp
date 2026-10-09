"""M7 facial performance service: profile -> immutable plan -> layer parts -> versioned candidate -> QA ->
renders -> visual review -> refinement (<= 3) -> explicit accept / reject.

Reuses the M6 ledger (append-only, sha256 chain) in its own ``facial`` category, the M3.1 asset guard and
checkpoints. Every candidate is a NEW Action (one OBJECT slot for the armature, one KEY slot per Key
datablock); previous versions and the body Action are never modified.
"""
from __future__ import annotations

import json
import math
import time
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from ..animation.generation import store
from ..animation.generation.render import write_apng
from ..checkpoint import create_checkpoint
from ..common.asset_safety import require_mutable_asset
from ..common.storage import sha256, write_json
from ..qa.capture import _verify_images
from ..qa.png_montage import write_montage
from . import curves as curve_mod
from . import eyes as eye_mod
from . import layers as L
from . import mocap as mocap_mod
from . import profile as P
from . import resolve as R
from . import speech as speech_mod
from .model import VISEMES, create_brief, expression_dimensions, semantic_targets
from .scene_runtime import CANDIDATE_QA, CAPABILITY, HEAD_TRACK, INTEGRATION, MARK, RENDER, STATUS, WRITE

CATEGORY = 'facial'
STATES = store.FACIAL_STATES
VIEWS = ('FRONT', 'THREE_QUARTER_LEFT', 'THREE_QUARTER_RIGHT', 'PROFILE')
WRITE_CHUNK_BYTES = 120_000
QA_FRAME_LIMIT = 120
REFINE_TARGETS = {'curve_noise': 'lower', 'spike_count': 'lower', 'mbp_gap_max_mm': 'lower', 'eye_penetration_max_mm': 'lower',
                  'blink_closure_min': 'higher', 'mouth_gap_min_mm': 'higher', 'new_intersections_max': 'lower',
                  'flipped_max': 'lower', 'lipsync_timing_error_ms': 'lower'}


# ---------------------------------------------------------------- ledger and freshness

def load(storage, reference):
    return store.load(storage, reference, CATEGORY)


def record(path, state):
    store.event(path.parent, state, STATES)


def facial_set(options):
    return dict((options or {}).get('facial_set') or {})


def scene_status(bridge, object_name, set_options, candidate=None):
    return bridge.run(STATUS, {'object_name': object_name, 'options': set_options, 'candidate': candidate})


def fresh(bridge, plan, state):
    s = scene_status(bridge, plan['object_name'], plan['set_options'], state.get('candidate_action'))
    if s['rig_fingerprint'] != plan['profile']['rig_fingerprint']:
        raise ValueError('STALE_RIG_FINGERPRINT: bones, constraints, limits, drivers or shape-key data changed since the plan; plan again')
    if s['body'] != plan['body']:
        raise ValueError('BODY_ACTION_CHANGED: the body Action changed unexpectedly since the plan; nothing is written')
    if s['filepath'] != plan['filepath'] or s['stamps'] != plan['stamps']:
        raise ValueError('STALE_PROPOSAL: open file or asset stamps changed since the plan')
    if state.get('candidate_action') and s.get('candidate_fingerprint') != state.get('candidate_fingerprint'):
        raise ValueError('STALE_PROPOSAL: the facial candidate Action changed outside this plan')
    return s


def profile_summary(prof):
    usable = sorted(k for k, v in prof['mapping'].items() if v.get('usable'))
    return {'classification': prof['classification'],
            'compatibility': {k: v['status'] for k, v in prof['compatibility'].items()},
            'usable_outputs': len(usable), 'outputs': usable, 'calibration': {k: prof['calibration'].get(k) for k in ('lids', 'jaw', 'lips')},
            'calibration_findings': prof['calibration'].get('findings', []), 'blocking': prof['blocking'],
            'hybrid_conflicts': prof['hybrid']['conflicts'], 'warnings': prof['inspection_warnings'], 'timings': prof['timings'],
            'head_bone': prof['head_bone'], 'neck_bone': prof['neck_bone'], 'armature': prof['armature'],
            'face_mesh': prof['face_mesh'], 'eyes': prof['eye_objects'], 'body_action': prof['body']['action']}


def _qa_summary(qa):
    if not qa:
        return None
    return {k: qa.get(k) for k in ('passed', 'metrics', 'thresholds', 'threshold_source')} | {
        'errors': [f for f in qa.get('findings', []) if f.get('severity') == 'error'][:20],
        'warnings': [f for f in qa.get('findings', []) if f.get('severity') == 'warning'][:20]}


def public(path, plan, state):
    render = state.get('render')
    return {'facial': str(path), 'generation_id': plan['generation_id'], 'object_name': plan['object_name'],
            'state': state.get('state'), 'candidate_action': state.get('candidate_action'),
            'candidate_fingerprint': state.get('candidate_fingerprint'), 'version': state.get('version', 0),
            'parts': sorted(state.get('parts', {})), 'refinement_cycles': state.get('refinement_cycles', 0),
            'technical_qa': _qa_summary(state.get('technical_qa')),
            'render': {k: render.get(k) for k in ('status', 'output_dir', 'temporal_complete', 'candidate_action', 'animations', 'player', 'metrics')} if render else None,
            'review': state.get('review'), 'head_policy': plan['head_policy'], 'checkpoint': state.get('checkpoint'),
            'pending_refinement': bool(state.get('pending_refinement')), 'evidence_dir': str(path.parent)}


# ---------------------------------------------------------------- plan

def brief_from(intent=None, brief=None):
    if not intent and not brief:
        return create_brief('neutral')
    return create_brief(intent, brief)


def create_plan(bridge, storage, operation_id, object_name, intent=None, brief=None, options=None):
    options = dict(options or {})
    began = time.perf_counter()
    prof, raw = P.build(bridge, object_name, facial_set(options))
    normalized = brief_from(intent, brief)
    policy = options.get('head_policy', 'BODY_OWNS')
    own = P.ownership(prof, policy)
    status = scene_status(bridge, object_name, facial_set(options))
    supported = prof['classification']['type'] not in ('UNSUPPORTED', 'AMBIGUOUS') and not prof['blocking']
    data = dict(object_name=object_name, set_options=facial_set(options), options=options, profile=prof, brief=normalized,
                head_policy=policy, ownership=own, body=status['body'], filepath=status['filepath'], stamps=status['stamps'],
                rest_key_values=status['key_values'], supported=supported, no_action_created=True)
    reference, saved = store.save(storage, operation_id, data, CATEGORY, STATES)
    return reference, saved, round(time.perf_counter() - began, 3)


def plan_or_create(bridge, storage, operation_id, facial, object_name, intent, brief, options):
    if facial:
        return load(storage, facial)
    if not object_name:
        raise ValueError('FACIAL_PLAN_REQUIRED: give a facial plan reference or an object_name to plan first')
    reference, _, _ = create_plan(bridge, storage, operation_id + '-plan', object_name, intent, brief, options)
    return load(storage, reference)


# ---------------------------------------------------------------- parts

def _part_errors(parts):
    for name, part in parts.items():
        for f in (part or {}).get('findings', []):
            if f.get('severity') == 'error':
                raise ValueError(f['type'] + ': ' + name + ' ' + json.dumps({k: v for k, v in f.items() if k not in ('type', 'severity')})[:300])


def _eye_tracks(parts):
    sets = [parts[k]['tracks'] for k in ('gaze', 'saccades') if parts.get(k)]
    merged = L.merge(sets) if sets else {}
    return {k: v for k, v in merged.items() if k.startswith('eye_')}


def gaze_motion(bridge, object_name, prof, brief, options):
    """Measured body head motion and target object paths over the clip (None when nothing moves)."""
    sequence = (options or {}).get('sequence') or [{'target': brief.get('gaze') or {}}]
    objects = []
    for row in sequence:
        target = row.get('target') or {}
        if target.get('type') == 'object' or target.get('target') == 'object':
            objects.append(target['name'])
        if (target.get('type') == 'camera' or target.get('target') == 'camera') and target.get('use_scene_camera'):
            objects.append('__scene_camera__')
    if not prof['body']['action'] and not objects:
        return None
    motion = bridge.run(HEAD_TRACK, {'armature': prof['armature'], 'head_bone': prof['head_bone'], 'objects': sorted(set(objects)),
                                     'frames': list(range(brief['frame_start'], brief['frame_end'] + 1))})
    if not objects and motion['max_head_rotation_deg'] < 0.05 and motion['max_head_translation_m'] < 1e-4:
        return None
    return motion


def _scene_camera_targets(options):
    o = dict(options or {})
    if o.get('sequence'):
        rows = []
        for row in o['sequence']:
            target = dict(row.get('target') or {})
            if (target.get('type') == 'camera' or target.get('target') == 'camera') and target.get('use_scene_camera'):
                target = {'type': 'object', 'name': '__scene_camera__'}
            rows.append(dict(row, target=target))
        o['sequence'] = rows
    return o


def make_parts(kind, prof, brief, policy, parts, options=None, motion=None):
    """Regenerate one kind of part; returns {part_name: part | None (removed)}."""
    o = dict(options or {})
    usable = {k for k, v in prof['mapping'].items() if v.get('usable')}
    out = {}
    if kind == 'expression':
        out['expression'] = L.expression_part(prof, brief, o)
        out['asymmetry'] = L.asymmetry_part(prof, brief, o)
    elif kind in ('gaze', 'coordinate'):
        P.require_mutable_profile(prof, ('eyes',))
        gaze, head = L.gaze_parts(prof, brief, _scene_camera_targets(o), policy, motion)
        out['gaze'], out['head'] = gaze, head
        if o.get('lid_follow', True) and {'lid_close_L', 'lid_close_R'} <= usable:
            eye_tracks = _eye_tracks(dict(parts, gaze=gaze))
            out['lid_follow'] = L.lid_follow_part(prof, brief, eye_tracks, o.get('lid_follow_options'))
    elif kind == 'saccades':
        P.require_mutable_profile(prof, ('eyes',))
        gaze = parts.get('gaze')
        if gaze is None:
            gaze, head = L.gaze_parts(prof, brief, {}, policy, motion)
            out['gaze'], out['head'] = gaze, head
        out['saccades'] = L.saccade_part(prof, brief, gaze, o)
        if parts.get('lid_follow') or out.get('gaze'):
            if {'lid_close_L', 'lid_close_R'} <= usable:
                eye_tracks = _eye_tracks(dict(parts, gaze=gaze, saccades=out['saccades']))
                out['lid_follow'] = L.lid_follow_part(prof, brief, eye_tracks, (parts.get('lid_follow') or {}).get('params'))
    elif kind == 'lid_follow':
        if not parts.get('gaze'):
            raise ValueError('GAZE_REQUIRED: generate gaze first; lids follow the eye pitch')
        out['lid_follow'] = L.lid_follow_part(prof, brief, _eye_tracks(parts), o)
    elif kind == 'blinks':
        P.require_mutable_profile(prof, ('lids',))
        shifts = (parts.get('gaze') or {}).get('shifts', [])
        out['blinks'] = L.blinks_part(prof, brief, o, shifts, L.speech_intervals(parts))
    elif kind == 'brows':
        out['brows'] = L.brows_part(prof, brief, o)
    elif kind == 'jaw':
        if o.get('mode', 'breathing') != 'tension':
            P.require_mutable_profile(prof, ('jaw',))
        out['jaw'] = L.jaw_part(prof, brief, o)
    elif kind == 'speech':
        P.require_mutable_profile(prof, ('jaw',))
        speech, head, brows = L.speech_parts(prof, brief, o)
        out['speech'], out['speech_head'], out['speech_brows'] = speech, head, brows
    elif kind == 'micro':
        avoid = [[w[0], w[1]] for w in (parts.get('speech') or {}).get('closure_windows', [])]
        for d in (parts.get('blinks') or {}).get('details', []):
            avoid.append([d['frame'] - 2, d['frame'] + d['frames'] + 2])
        out['micro'] = L.micro_part(prof, brief, o, avoid)
    else:
        raise ValueError('UNKNOWN_FACIAL_PART: ' + str(kind))
    return out


def apply_parts(parts, updates):
    merged = dict(parts)
    for name, part in updates.items():
        if part is None:
            merged.pop(name, None)
        else:
            merged[name] = part
    return merged


def default_kinds(prof, brief, options):
    usable = {k for k, v in prof['mapping'].items() if v.get('usable')}
    kinds = ['expression']
    eyes_ok = all(k in usable for k in ('eye_yaw_L', 'eye_yaw_R', 'eye_pitch_L', 'eye_pitch_R'))
    if eyes_ok and options.get('gaze', {}) is not False:
        kinds.append('gaze')
        if options.get('saccades', {}) is not False:
            kinds.append('saccades')
    if brief.get('speech', {}).get('enabled'):
        kinds.append('speech')
    if {'lid_close_L', 'lid_close_R'} <= usable and options.get('blinks', {}) is not False:
        kinds.append('blinks')
    if brief.get('microexpressions') and options.get('micro', {}) is not False:
        kinds.append('micro')
    if options.get('jaw'):
        kinds.append('jaw')
    if options.get('brows'):
        kinds.append('brows')
    return kinds


def part_summary(part):
    if not part:
        return None
    tracks = part.get('tracks', {})
    return {'semantics': sorted(tracks), 'keys': sum(len(v) for v in tracks.values()), 'params': part.get('params'),
            'findings': part.get('findings', []), 'stats': part.get('stats'), 'provenance': part.get('provenance'),
            'peak': {k: round(max((abs(x) for _, x in v), default=0.0), 4) for k, v in tracks.items()}}


# ---------------------------------------------------------------- candidate

def next_version(state):
    """Versions come from the whole history: a reverted refinement keeps its Action (retained evidence), so reusing
    the reverted state's number would collide with it (measured: refinement cycles 2-3 rejected as collisions)."""
    return max([state.get('version', 0)] + [h['version'] for h in state.get('history', [])]) + 1


def candidate_name(plan, version):
    prefix = plan['options'].get('candidate_prefix', 'M7F')
    return prefix + '_' + plan['generation_id'][:8] + '_v' + str(version)


def _channel_row(c):
    flat = []
    for f, v in c['keys']:
        flat += [float(f), float(v)]
    row = {'kind': c['kind'], 'index': c['index'], 'co': flat}
    if c['kind'] == 'bone':
        row.update(bone=c['bone'], path=c['path'])
    else:
        row.update(mesh=c['mesh'], key=c['key'])
    return row


def write_candidate(bridge, plan, name, version, channels):
    rows = [_channel_row(c) for c in channels]
    meshes = sorted({c['mesh'] for c in channels if c['kind'] == 'shape_key'})
    chunks, current, size = [], [], 0
    for row in rows:
        n = len(json.dumps(row))
        if current and size + n > WRITE_CHUNK_BYTES:
            chunks.append(current)
            current, size = [], 0
        current.append(row)
        size += n
    if current:
        chunks.append(current)
    token = uuid4().hex
    totals = {'curves_written': 0, 'keys_written': 0, 'chunks': len(chunks)}
    result = None
    for index, chunk in enumerate(chunks):
        result = bridge.run(WRITE, {'candidate_name': name, 'mode': 'create' if index == 0 else 'append', 'token': token,
                                    'armature': plan['profile']['armature'], 'meshes': meshes,
                                    'needs_armature_slot': any(c['kind'] == 'bone' for c in channels),
                                    'generation_id': plan['generation_id'], 'version': version, 'channels': chunk})
        totals['curves_written'] += result['curves_written']
        totals['keys_written'] += result['keys_written']
    return dict(result, **totals)


def qa_frames(brief, parts, every=False):
    fs, fe = brief['frame_start'], brief['frame_end']
    step = 1 if every else max(1, math.ceil((fe - fs + 1) / QA_FRAME_LIMIT))
    frames = set(range(fs, fe + 1, step)) | {fe}
    events = L.event_frames(parts)
    frames |= {f for f in events['blink_peaks'] + events['mbp_centres'] if fs <= f <= fe}
    return sorted(frames), events


def geometry_findings(geo, events, thresholds):
    t = thresholds
    findings = []
    by = {r['frame']: r for r in geo['frames']}
    metrics = {'frames_sampled': len(geo['frames']), 'eye_penetration_max_mm': 0.0, 'mouth_gap_min_mm': None,
               'blink_closure_min': None, 'mbp_gap_max_mm': None, 'new_intersections_max': 0, 'collapsed_max': 0, 'flipped_max': 0}
    for f in events['blink_peaks']:
        row = by.get(f)
        if row is None:
            continue
        for side in ('L', 'R'):
            closure = row['landmarks'].get('eye_' + side, {}).get('closure')
            if closure is None:
                continue
            metrics['blink_closure_min'] = closure if metrics['blink_closure_min'] is None else min(metrics['blink_closure_min'], closure)
            if closure < t['blink_closure_min']:
                findings.append({'type': 'BLINK_INCOMPLETE', 'frame': f, 'side': side, 'closure': closure,
                                 'threshold': t['blink_closure_min'], 'severity': 'error'})
    for f in events['mbp_centres']:
        row = by.get(f)
        gap = (row or {}).get('landmarks', {}).get('mouth', {}).get('gap_mean_mm')
        if gap is None:
            continue
        metrics['mbp_gap_max_mm'] = gap if metrics['mbp_gap_max_mm'] is None else max(metrics['mbp_gap_max_mm'], gap)
        if gap > t['mbp_gap_mm']:
            findings.append({'type': 'MBP_NOT_SEALED', 'frame': f, 'gap_mm': gap, 'threshold_mm': t['mbp_gap_mm'], 'severity': 'error'})
    grouped = {}
    for row in geo['frames']:
        f = row['frame']
        for side, eye in row['eyes'].items():
            metrics['eye_penetration_max_mm'] = max(metrics['eye_penetration_max_mm'], eye['penetration_increase_mm'])
            overlap = row['overlap_new'].get('face/eye_' + side, 0)
            # Vertex depth decides; polygon-facet crossings without measurable depth are contact, not penetration
            # (measured: 1-2 polygons mid-blink on the clean fixture with 0.0 mm depth increase).
            if eye['penetration_increase_mm'] > t['eye_penetration_mm']:
                grouped.setdefault(('EYE_PENETRATION', side), []).append((f, eye['penetration_increase_mm'], overlap))
            elif overlap:
                grouped.setdefault(('EYE_SURFACE_CONTACT', side), []).append((f, overlap, overlap))
        mouth = row['landmarks'].get('mouth')
        if mouth:
            gmin = mouth['gap_min_mm']
            metrics['mouth_gap_min_mm'] = gmin if metrics['mouth_gap_min_mm'] is None else min(metrics['mouth_gap_min_mm'], gmin)
            if gmin < t['lip_crossing_mm']:
                grouped.setdefault(('LIP_CROSSING', 'mouth'), []).append((f, gmin, 0))
        for label, count in row['overlap_new'].items():
            if label.startswith('face/eye_') or count <= t['new_intersections']:
                continue
            baseline = row.get('overlap_baseline', {}).get(label, 0)
            if baseline:
                # The body-only state already intersects at this frame; the face only extends it (root cause elsewhere).
                grouped.setdefault(('MESH_INTERSECTION_EXTENDED', label), []).append((f, count, baseline))
            else:
                metrics['new_intersections_max'] = max(metrics['new_intersections_max'], count)
                grouped.setdefault(('MESH_INTERSECTION', label), []).append((f, count, count))
        metrics['collapsed_max'] = max(metrics['collapsed_max'], row['collapsed'])
        metrics['flipped_max'] = max(metrics['flipped_max'], row['flipped'])
        if row['collapsed'] > t['collapsed_faces'] or row['flipped'] > t['flipped_faces']:
            grouped.setdefault(('DEGENERATE_FACES', 'face'), []).append((f, row['collapsed'], row['flipped']))
    for (kind, where), rows in grouped.items():
        row = {'type': kind, 'where': where, 'frames': [r[0] for r in rows][:20], 'frame_count': len(rows),
               'worst': max((r[1] for r in rows), key=abs),
               'severity': 'warning' if kind in ('EYE_SURFACE_CONTACT', 'MESH_INTERSECTION_EXTENDED') else 'error'}
        if kind == 'MESH_INTERSECTION_EXTENDED':
            row['baseline_polygons'] = max(r[2] for r in rows)
            row['note'] = 'the body-only baseline already intersects at these frames; the facial layer adds polygons to it'
        findings.append(row)
    if geo.get('key_range_violations'):
        findings.append({'type': 'SHAPE_KEY_OUT_OF_RANGE', 'samples': geo['key_range_violations'][:10], 'severity': 'error'})
    if not geo.get('body_unchanged'):
        findings.append({'type': 'BODY_ACTION_CHANGED', 'severity': 'error'})
    if not geo.get('state_restored'):
        findings.append({'type': 'STATE_NOT_RESTORED', 'severity': 'error'})
    return findings, metrics


GAZE_TOLERANCE_DEG = 1.5  # proposal: measured eye direction vs planned degrees, after removing head motion


def gaze_findings(geo, combined, mapping):
    from .tracks import sample
    errors, findings = [], []
    for row in geo['frames']:
        for side, (yaw, pitch) in (row.get('gaze_deg') or {}).items():
            for axis, measured in (('yaw', yaw), ('pitch', pitch)):
                name = 'eye_' + axis + '_' + side
                if name not in combined:
                    continue
                planned = sample(combined[name], row['frame'])
                errors.append((abs(measured - planned), row['frame'], name, planned, measured))
                (lo, hi), source = eye_mod.limits_for(mapping, side, axis)
                if measured < lo - GAZE_TOLERANCE_DEG or measured > hi + GAZE_TOLERANCE_DEG:
                    findings.append({'type': 'EYE_LIMIT_EXCEEDED', 'frame': row['frame'], 'semantic': name, 'measured_deg': measured,
                                     'limits': [lo, hi], 'source': source, 'severity': 'error'})
    if not errors:
        return findings, {}
    worst = max(errors)
    metrics = {'gaze_error_max_deg': round(worst[0], 3), 'gaze_error_mean_deg': round(sum(e[0] for e in errors) / len(errors), 4),
               'gaze_samples': len(errors)}
    if worst[0] > GAZE_TOLERANCE_DEG:
        findings.append({'type': 'GAZE_MISMATCH', 'frame': worst[1], 'semantic': worst[2], 'planned_deg': round(worst[3], 3),
                         'measured_deg': worst[4], 'tolerance_deg': GAZE_TOLERANCE_DEG, 'severity': 'error'})
    return findings[:20], metrics


def channel_findings(plan, parts, combined, conflicts, report):
    brief = plan['brief']
    fps = brief['fps']
    findings = []
    protected = L.protected_windows(parts, fps)
    limits = {}
    for name in combined:
        if name.startswith('eye_'):
            limits[name] = list(eye_mod.limits_for(plan['profile']['mapping'], name[-1], name.split('_')[1])[0])
    analysis = curve_mod.analyze(combined, fps, protected, limits)
    findings += analysis['findings']
    metrics = {'spike_count': sum(r['spike_count'] for r in analysis['channels'].values()),
               'curve_noise': round(sum(r['hf_noise'] for n, r in analysis['channels'].items() if r['unit'] == 'normalized'), 6)}
    lipsync = None
    speech = parts.get('speech')
    if speech:
        lipsync = speech_mod.validate_lipsync(combined, speech['prepared'], fps, approximated=speech.get('approximations'))
        findings += lipsync['findings']
        metrics['lipsync_timing_error_ms'] = lipsync['metrics']['max_closure_timing_error_ms']
        metrics['mbp_closed'] = [lipsync['metrics']['mbp_closed'], lipsync['metrics']['mbp_segments']]
    for c in conflicts:
        findings.append({'type': 'LAYER_CONFLICT_RESOLVED', 'conflict': c['type'], 'semantic': c['semantic'], 'rule': c['rule'],
                         'frame_count': c['frame_count'], 'severity': 'info'})
    for key, count in report.get('clamped', {}).items():
        findings.append({'type': 'CONTROL_RANGE_CLAMPED', 'control': key, 'frames': count, 'severity': 'warning'})
    for name in report.get('unsupported', []):
        findings.append({'type': 'OUTPUT_UNSUPPORTED_DROPPED', 'semantic': name, 'severity': 'warning'})
    for row in report.get('shared', []):
        if row.get('asymmetry_lost_max'):
            findings.append({'type': 'BILATERAL_CONTROL_ASYMMETRY_LOST', 'control': row['control'],
                             'max_lost': row['asymmetry_lost_max'], 'severity': 'warning'})
    return findings, metrics, lipsync, {k: v for k, v in analysis['channels'].items() if v['spike_count'] or v['overshoot_frames']}


def technical_qa(bridge, plan, name, parts, combined, conflicts, report, options=None):
    o = dict(options or {})
    began = time.perf_counter()
    thresholds = dict(P.QA_THRESHOLDS, **{k: v for k, v in (o.get('thresholds') or {}).items() if k in P.QA_THRESHOLDS})
    frames, events = qa_frames(plan['brief'], parts, o.get('every_frame', False))
    geo = bridge.run(CANDIDATE_QA, {'object_name': plan['object_name'], 'options': plan['set_options'], 'candidate': name,
                                    'frames': frames, 'head_policy': plan['head_policy'], 'qa_options': o.get('geometry', {})})
    geo_findings, geo_metrics = geometry_findings(geo, events, thresholds)
    gz_findings, gz_metrics = gaze_findings(geo, combined, plan['profile']['mapping'])
    ch_findings, ch_metrics, lipsync, curve_rows = channel_findings(plan, parts, combined, conflicts, report)
    findings = geo_findings + gz_findings + ch_findings
    metrics = dict(geo_metrics, **gz_metrics, **ch_metrics)
    metrics['qa_seconds'] = round(time.perf_counter() - began, 3)
    return {'passed': not any(f['severity'] == 'error' for f in findings), 'findings': findings, 'metrics': metrics,
            'thresholds': thresholds, 'threshold_source': P.THRESHOLD_SOURCE, 'events': events, 'lipsync': lipsync,
            'curves_flagged': curve_rows, 'geometry_frames': frames if len(frames) <= 40 else frames[:40] + ['...'],
            'measured_gaze': [[r['frame'], (r.get('gaze_deg') or {}).get('L'), (r.get('gaze_deg') or {}).get('R'), r.get('head_deg')]
                              for r in geo['frames']][:300],
            'measured_lids_mouth': [[r['frame'], r['landmarks'].get('eye_L', {}).get('closure'), r['landmarks'].get('eye_R', {}).get('closure'),
                                     r['landmarks'].get('mouth', {}).get('gap_mean_mm')] for r in geo['frames']][:300],
            'body_unchanged': geo.get('body_unchanged'), 'state_restored': geo.get('state_restored'),
            'note': 'Technical measurements only (sampled frames + event frames); artistic quality needs visual review.'}


def compose_tracks(plan, parts, polish=None):
    brief = plan['brief']
    combined, conflicts = L.compose(parts, brief, plan['head_policy'])
    polish_report = None
    if polish:
        protected = L.protected_windows(parts, brief['fps'])
        combined, polish_report = curve_mod.polish(combined, brief['fps'], protected, polish.get('strength', 0.5),
                                                   polish.get('simplify_tolerance'))
    return combined, conflicts, polish_report


def build_candidate(bridge, storage, operation_id, path, plan, state, parts, reason, polish=None, qa_options=None):
    prof = plan['profile']
    P.require_mutable_profile(prof)
    _part_errors(parts)
    combined, conflicts, polish_report = compose_tracks(plan, parts, polish)
    channels, report = R.resolve(combined, prof, plan['head_policy'])
    if report['errors']:
        raise ValueError(report['errors'][0])
    if not channels:
        raise ValueError('FACIAL_NOTHING_TO_WRITE: no supported semantic output has motion')
    require_mutable_asset(bridge, storage, 'M7 facial candidate')
    version = next_version(state)
    name = candidate_name(plan, version)
    began = time.perf_counter()
    checkpoint = create_checkpoint(bridge, storage, 'Before M7 facial candidate ' + name, operation_id, 'M7 facial candidate')['manifest']
    written = write_candidate(bridge, plan, name, version, channels)
    write_s = round(time.perf_counter() - began, 3)
    qa = technical_qa(bridge, plan, name, parts, combined, conflicts, report, qa_options)
    history = list(state.get('history', [])) + [{'version': version, 'candidate': name, 'reason': reason,
                                                 'fingerprint': written['fingerprint'], 'qa_passed': qa['passed']}]
    state.update(state='FACIAL_CANDIDATE', candidate_action=name, candidate_fingerprint=written['fingerprint'], version=version,
                 parts=parts, polish=polish, technical_qa=qa, render=None, closeup=None, review=None, checkpoint=checkpoint,
                 resolve_report={k: report[k] for k in ('clamped', 'shared', 'unsupported', 'lip_seal', 'eyes') if k in report},
                 conflicts=conflicts, polish_report=polish_report, history=history,
                 written={k: written[k] for k in ('curves_written', 'keys_written', 'chunks', 'slots', 'frame_range')},
                 timings={'write_s': write_s, 'qa_s': qa['metrics']['qa_seconds']})
    record(path, state)
    return state


# ---------------------------------------------------------------- generation entry points

def generate_kind(bridge, storage, operation_id, kind, object_name=None, intent=None, brief=None, facial=None, options=None, apply=False):
    """One layer generator: proposal without a plan, or a new candidate version in the plan when apply=True."""
    options = dict(options or {})
    if not apply and not facial:
        if not object_name:
            raise ValueError('OBJECT_OR_PLAN_REQUIRED: give object_name (proposal) or a facial plan reference')
        prof, _ = P.build(bridge, object_name, facial_set(options))
        normalized = brief_from(intent, brief)
        policy = options.get('head_policy', 'BODY_OWNS')
        P.ownership(prof, policy)
        motion = gaze_motion(bridge, object_name, prof, normalized, options) if kind in ('gaze', 'coordinate', 'saccades') else None
        updates = make_parts(kind, prof, normalized, policy, {}, options, motion)
        return {'status': 'proposed', 'dry_run': True, 'no_action_created': True, 'kind': kind,
                'parts': {k: part_summary(v) for k, v in updates.items() if v}, 'profile': profile_summary(prof), 'brief': normalized}
    path, plan, state = plan_or_create(bridge, storage, operation_id, facial, object_name, intent, brief, options)
    store.mutable(state)
    if state.get('pending_refinement'):
        raise ValueError('REFINEMENT_REVIEW_REQUIRED: review the pending refinement first')
    fresh(bridge, plan, state)
    parts = dict(state.get('parts', {}))
    motion = gaze_motion(bridge, plan['object_name'], plan['profile'], plan['brief'], options) \
        if kind in ('gaze', 'coordinate', 'saccades') else None
    updates = make_parts(kind, plan['profile'], plan['brief'], plan['head_policy'], parts, options, motion)
    proposed = apply_parts(parts, updates)
    if not apply:
        combined, conflicts, _ = compose_tracks(plan, proposed, state.get('polish'))
        channels, report = R.resolve(combined, plan['profile'], plan['head_policy'])
        return dict(public(path, plan, state), status='proposed', dry_run=True, kind=kind,
                    parts={k: part_summary(v) for k, v in updates.items() if v}, conflicts=conflicts,
                    channels=len(channels), resolve_errors=report['errors'], no_action_created=True)
    state = build_candidate(bridge, storage, operation_id, path, plan, state, proposed, 'generate ' + kind, state.get('polish'),
                            options.get('qa'))
    return dict(public(path, plan, state), status='candidate_created', kind=kind,
                parts={k: part_summary(v) for k, v in updates.items() if v}, written=state['written'],
                numeric_verification='pass' if state['technical_qa']['passed'] else 'fail', visual_verification='required')


def generate(bridge, storage, operation_id, object_name=None, intent=None, brief=None, facial=None, options=None, apply=False):
    """High-level facial performance: all applicable layers from one brief. apply=False plans only."""
    options = dict(options or {})
    if facial:
        path, plan, state = load(storage, facial)
    else:
        if not object_name:
            raise ValueError('OBJECT_OR_PLAN_REQUIRED: give object_name or a facial plan reference')
        reference, _, seconds = create_plan(bridge, storage, operation_id + '-plan', object_name, intent, brief, options)
        path, plan, state = load(storage, reference)
    store.mutable(state)
    prof, normalized = plan['profile'], plan['brief']
    kinds = options.get('kinds') or default_kinds(prof, normalized, options)
    parts = dict(state.get('parts', {}))
    produced = {}
    for kind in kinds:
        kind_options = options.get(kind) if isinstance(options.get(kind), dict) else {}
        if kind == 'speech' and not kind_options.get('timeline') and not normalized.get('speech', {}).get('timeline'):
            raise ValueError('TIMING_INPUT_REQUIRED: the brief asks for speech; give options.speech.timeline with timed phonemes or visemes')
        motion = gaze_motion(bridge, plan['object_name'], prof, normalized, kind_options) \
            if kind in ('gaze', 'coordinate', 'saccades') else None
        updates = make_parts(kind, prof, normalized, plan['head_policy'], parts, kind_options, motion)
        parts = apply_parts(parts, updates)
        produced.update({k: part_summary(v) for k, v in updates.items() if v})
    if not apply:
        combined, conflicts, _ = compose_tracks(plan, parts)
        channels, report = R.resolve(combined, prof, plan['head_policy'])
        return dict(public(path, plan, state), status='planned', dry_run=True, kinds=kinds, parts=produced,
                    conflicts=conflicts, channels=len(channels), resolve_errors=report['errors'],
                    profile=profile_summary(prof), brief=normalized, no_action_created=True)
    fresh(bridge, plan, state)
    state = build_candidate(bridge, storage, operation_id, path, plan, state, parts, 'facial_generate ' + ','.join(kinds),
                            state.get('polish'), options.get('qa'))
    return dict(public(path, plan, state), status='candidate_created', kinds=kinds, parts=produced, written=state['written'],
                numeric_verification='pass' if state['technical_qa']['passed'] else 'fail', visual_verification='required',
                next_step='facial_render_preview, then facial_review with artistic observations, then facial_accept or facial_reject')


def plan_expression(bridge, storage, operation_id, object_name=None, intent=None, brief=None, facial=None, options=None, apply=False):
    options = dict(options or {})
    if facial:
        path, plan, state = load(storage, facial)
    else:
        if not object_name:
            raise ValueError('OBJECT_OR_PLAN_REQUIRED')
        reference, _, seconds = create_plan(bridge, storage, operation_id + '-plan', object_name, intent, brief, options)
        path, plan, state = load(storage, reference)
    prof, normalized = plan['profile'], plan['brief']
    dims = expression_dimensions(normalized)
    targets, reasons = semantic_targets(dims)
    usable = {k for k, v in prof['mapping'].items() if v.get('usable')}
    rows = []
    for semantic, value in sorted(targets.items()):
        entry = prof['mapping'].get(semantic, {})
        rows.append({'semantic': semantic, 'target': round(value, 4), 'from': reasons.get(semantic), 'usable': semantic in usable,
                     'confidence': entry.get('confidence'), 'control': _control_label(entry.get('control')),
                     'alternatives': entry.get('alternatives', [])[:2]})
    result = dict(public(path, plan, state), status='planned', brief=normalized,
                  dimensions={k: round(v, 4) for k, v in dims.items() if abs(v) > 1e-6}, outputs=rows,
                  unsupported=[r['semantic'] for r in rows if not r['usable']], profile=profile_summary(prof),
                  provenance=normalized['provenance'], no_action_created=True,
                  note='Emotion words select starting control dimensions (proposal); this is not psychological truth.')
    if not apply:
        return result
    created = generate_kind(bridge, storage, operation_id, 'expression', facial=str(path), options=options.get('expression'), apply=True)
    return dict(created, plan=result['outputs'], dimensions=result['dimensions'])


def _control_label(control):
    if not control:
        return None
    if control['type'] == 'shape_key':
        return control['mesh'] + ':' + control['key']
    if control['type'] == 'aim_target':
        return control['bone'] + ':aim'
    return control['bone'] + ':' + control['path'] + '[' + str(control['index']) + ']'


# ---------------------------------------------------------------- validation and analysis

def pose_from_targets(prof, targets):
    tracks = {k: [[0, v]] for k, v in targets.items() if not k.startswith(('eye_', 'head_', 'neck_'))}
    if 'lips_close' in tracks and 'jaw_open' not in tracks:
        tracks['jaw_open'] = [[0, 0.0]]
    channels, report = R.resolve(tracks, prof, 'BODY_OWNS')
    pose = {'keys': [], 'bones': []}
    for c in channels:
        value = c['keys'][0][1]
        if c['kind'] == 'shape_key':
            pose['keys'].append([c['mesh'], c['key'], value])
        else:
            pose['bones'].append([c['bone'], c['path'], c['index'], value])
    return pose, report


def measure_poses(bridge, plan_like, poses):
    return bridge.run(CAPABILITY, {'object_name': plan_like['object_name'], 'options': plan_like['set_options'], 'poses': poses})


def pose_findings(metrics, thresholds=None):
    t = dict(P.QA_THRESHOLDS, **(thresholds or {}))
    out = []
    for side, eye in metrics['eyes'].items():
        overlap = metrics['overlap_new'].get('face/eye_' + side, 0)
        if eye['penetration_increase_mm'] > t['eye_penetration_mm']:
            out.append({'type': 'EYE_PENETRATION', 'side': side, 'penetration_mm': eye['penetration_increase_mm'],
                        'new_intersections': overlap, 'severity': 'error'})
        elif overlap:
            out.append({'type': 'EYE_SURFACE_CONTACT', 'side': side, 'polygons': overlap, 'severity': 'warning'})
    mouth = metrics['landmarks'].get('mouth')
    if mouth and mouth['gap_min_mm'] < t['lip_crossing_mm']:
        out.append({'type': 'LIP_CROSSING', 'gap_min_mm': mouth['gap_min_mm'], 'threshold_mm': t['lip_crossing_mm'], 'severity': 'error'})
    for label, count in metrics['overlap_new'].items():
        if count and not label.startswith('face/eye_'):
            out.append({'type': 'MESH_INTERSECTION', 'pair': label, 'polygons': count, 'severity': 'error'})
    if metrics['collapsed'] or metrics['flipped']:
        out.append({'type': 'DEGENERATE_FACES', 'collapsed': metrics['collapsed'], 'flipped': metrics['flipped'], 'severity': 'error'})
    return out


def _plan_like(bridge, storage, object_name, facial, options):
    if facial:
        path, plan, state = load(storage, facial)
        return plan, state, path
    if not object_name:
        raise ValueError('OBJECT_OR_PLAN_REQUIRED')
    prof, raw = P.build(bridge, object_name, facial_set(options))
    return {'object_name': object_name, 'set_options': facial_set(options), 'profile': prof, 'raw': raw,
            'head_policy': (options or {}).get('head_policy', 'BODY_OWNS')}, None, None


def validate_expression(bridge, storage, object_name=None, intent=None, brief=None, facial=None, options=None):
    o = dict(options or {})
    plan, state, path = _plan_like(bridge, storage, object_name, facial, o)
    prof = plan['profile']
    normalized = plan.get('brief') or brief_from(intent, brief)
    part = L.expression_part(prof, normalized, o)
    pose, report = pose_from_targets(prof, part['targets'])
    measured = measure_poses(bridge, plan, [dict(pose, name='expression_peak')])
    metrics = measured['poses'][0]['metrics']
    findings = pose_findings(metrics, o.get('thresholds')) + part['findings']
    for key, count in report.get('clamped', {}).items():
        findings.append({'type': 'CONTROL_RANGE_CLAMPED', 'control': key, 'severity': 'warning'})
    return {'status': 'validated', 'passed': not any(f['severity'] == 'error' for f in findings), 'findings': findings,
            'targets': part['targets'], 'pose': {'keys': len(pose['keys']), 'bones': len(pose['bones'])},
            'geometry': metrics, 'restored': measured.get('restored'), 'thresholds': P.QA_THRESHOLDS,
            'threshold_source': P.THRESHOLD_SOURCE, 'no_action_created': True}


def deformation_qa(bridge, storage, object_name=None, facial=None, options=None):
    """With a plan candidate: per-frame QA. Without: every usable control at full value (stress poses)."""
    o = dict(options or {})
    plan, state, path = _plan_like(bridge, storage, object_name, facial, o)
    if state and state.get('candidate_action'):
        fresh(bridge, plan, state)
        parts = state['parts']
        combined, conflicts, _ = compose_tracks(plan, parts, state.get('polish'))
        channels, report = R.resolve(combined, plan['profile'], plan['head_policy'])
        qa = technical_qa(bridge, plan, state['candidate_action'], parts, combined, conflicts, report,
                          dict(o, every_frame=o.get('every_frame', False)))
        return {'status': 'measured', 'mode': 'candidate', 'candidate_action': state['candidate_action'], **_qa_summary(qa),
                'findings': qa['findings'], 'no_action_created': True}
    prof = plan['profile']
    poses = []
    for semantic, entry in sorted(prof['mapping'].items()):
        if not entry.get('usable') or entry['control']['type'] not in ('shape_key', 'bone'):
            continue
        keys, bones = P.control_pose(entry)
        poses.append({'name': semantic, 'keys': keys, 'bones': bones})
    for combo in o.get('combinations', []):
        keys, bones = [], []
        for semantic in combo:
            k, b = P.control_pose(prof['mapping'][semantic])
            keys += k
            bones += b
        poses.append({'name': '+'.join(combo), 'keys': keys, 'bones': bones})
    measured = measure_poses(bridge, plan, poses)
    rows, findings = [], []
    for p in measured['poses']:
        f = pose_findings(p['metrics'], o.get('thresholds'))
        for x in f:
            x['pose'] = p['name']
        findings += f
        rows.append({'pose': p['name'], 'findings': len(f), 'mouth_gap_mm': p['metrics']['landmarks'].get('mouth', {}).get('gap_mean_mm')})
    return {'status': 'measured', 'mode': 'stress_poses', 'poses': len(poses), 'rows': rows, 'findings': findings,
            'passed': not findings, 'restored': measured.get('restored'), 'landmarks': measured.get('landmarks'),
            'thresholds': P.QA_THRESHOLDS, 'threshold_source': P.THRESHOLD_SOURCE, 'no_action_created': True}


def analyze(bridge, storage, name, object_name=None, facial=None, options=None):
    """Read-only analyses built on the measured profile (transient probe, restored)."""
    o = dict(options or {})
    plan, state, path = _plan_like(bridge, storage, object_name, facial, o)
    prof = plan['profile']
    mapping = prof['mapping']
    raw = plan.get('raw')
    base = {'status': 'analyzed', 'object_name': plan['object_name'], 'no_action_created': True}
    if name == 'classify':
        return dict(base, classification=prof['classification'], hybrid=prof['hybrid'], usable_outputs=prof['counts']['usable_outputs'],
                    counts=prof['counts'], blocking=prof['blocking'], warnings=prof['inspection_warnings'], timings=prof['timings'])
    if name == 'compatibility':
        return dict(base, classification=prof['classification'], compatibility=prof['compatibility'],
                    calibration_findings=prof['calibration'].get('findings', []), viseme_support=prof['viseme_support'],
                    blocking=prof['blocking'], ownership_policies=['BODY_OWNS', 'FACE_OWNS', 'BLENDED'],
                    body_animates_head_neck=[b for b in (prof['head_bone'], prof['neck_bone']) if b in prof['body']['animated_bones']])
    if name in ('shape_keys', 'bones'):
        controls = (raw or {}).get('analysis', {}).get('controls') if raw else None
        rows = []
        for semantic, entry in sorted(mapping.items()):
            c = entry.get('control') or {}
            kind = 'shape_key' if c.get('type') == 'shape_key' else 'bone'
            if kind != ('shape_key' if name == 'shape_keys' else 'bone'):
                continue
            rows.append({'semantic': semantic, 'control': _control_label(c), 'confidence': entry.get('confidence'),
                         'usable': entry.get('usable'), 'evidence': entry.get('evidence', [])[:3],
                         'alternatives': entry.get('alternatives', [])[:3], 'deform': c.get('deform'),
                         'calibration': c.get('calibration')})
        unmapped = []
        if controls:
            used = {_control_label(e.get('control')) for e in mapping.values()}
            for key, row in controls.items():
                if (name == 'shape_keys') == (row['control']['type'] == 'shape_key') and row['confidence'] < 0.6:
                    unmapped.append({'control': key, 'best_semantic': row['semantic'], 'confidence': row['confidence']})
        extra = {}
        if name == 'shape_keys':
            extra = {'driven_keys': prof['driven_keys'], 'meshes': [{k: m.get(k) for k in ('name', 'shape_keys', 'shape_keys_relative', 'basis_offset_m', 'role')}
                                                                     for m in prof['meshes']]}
        else:
            extra = {'driven_bone_channels': prof['driven_bone_channels'],
                     'rule': 'deform bones copied by a control are never animated directly; controls are'}
        return dict(base, outputs=rows, weak_controls=unmapped[:60], **extra)
    if name == 'hybrid':
        return dict(base, classification=prof['classification']['type'], decisions=prof['hybrid']['decisions'],
                    conflicts=prof['hybrid']['conflicts'], not_driven=prof['hybrid']['not_driven'],
                    rule='one semantic output is never driven through two systems')
    if name == 'eyes':
        rows = {}
        for k in ('eye_yaw_L', 'eye_yaw_R', 'eye_pitch_L', 'eye_pitch_R'):
            entry = mapping.get(k, {})
            (lo, hi), source = eye_mod.limits_for(mapping, k[-1], k.split('_')[1])
            rows[k] = {'usable': entry.get('usable', False), 'confidence': entry.get('confidence'), 'method': entry.get('method'),
                       'control': _control_label(entry.get('control')), 'limits_deg': [round(lo, 3), round(hi, 3)], 'limits_source': source}
        centers = prof['eye_centers']
        separation = math.dist(centers['L'], centers['R']) if set(centers) == {'L', 'R'} else None
        nearest = (separation / 2) / math.tan(math.radians(eye_mod.MAX_VERGENCE_DEG / 2)) if separation else None
        findings = [{'type': 'EYE_MAPPING_LOW_CONFIDENCE', 'output': k, 'confidence': v['confidence'], 'severity': 'error'}
                    for k, v in rows.items() if not v['usable']]
        return dict(base, outputs=rows, aims=prof['aims'], eye_separation_m=round(separation, 5) if separation else None,
                    nearest_convergence_m=round(nearest, 4) if nearest else None, max_vergence_deg=eye_mod.MAX_VERGENCE_DEG,
                    findings=findings, gaze_supported=not findings,
                    note='vergence limit and default eye ranges are stated proposals; rig limits win when present')
    if name == 'eyelids':
        lids = {k: _short(mapping.get(k)) for k in ('lid_close_L', 'lid_close_R', 'lid_wide_L', 'lid_wide_R', 'lid_raise_L', 'lid_raise_R')}
        return dict(base, outputs=lids, measured=prof['calibration'].get('lids'),
                    findings=[f for f in prof['calibration'].get('findings', []) if f['type'] in ('BLINK_INCOMPLETE', 'EYE_PENETRATION', 'LID_LANDMARK_NOT_FOUND')],
                    thresholds=P.QA_THRESHOLDS, threshold_source=P.THRESHOLD_SOURCE)
    if name == 'jaw':
        jaw = {k: _short(mapping.get(k)) for k in ('jaw_open', 'jaw_forward', 'jaw_side_L', 'jaw_side_R')}
        findings = [f for f in prof['calibration'].get('findings', []) if f['type'] == 'JAW_COLLISION']
        if not jaw['jaw_open']['usable']:
            findings.append({'type': 'MISSING_JAW_CONTROL', 'severity': 'error'})
        return dict(base, outputs=jaw, measured=prof['calibration'].get('jaw'), findings=findings)
    if name == 'lips':
        names = ('lips_close', 'lip_pucker', 'lip_funnel', 'lip_press_L', 'lip_press_R', 'lip_roll_lower', 'lip_roll_upper',
                 'lip_corner_up_L', 'lip_corner_up_R', 'lip_corner_down_L', 'lip_corner_down_R', 'lip_stretch_L', 'lip_stretch_R',
                 'upper_lip_raise_L', 'upper_lip_raise_R', 'lower_lip_depress_L', 'lower_lip_depress_R')
        return dict(base, outputs={k: _short(mapping.get(k)) for k in names}, measured=prof['calibration'].get('lips'),
                    findings=[f for f in prof['calibration'].get('findings', []) if f['type'].startswith('LIP')],
                    viseme_support=prof['viseme_support'])
    if name == 'visemes':
        rows = {}
        for viseme, targets in VISEMES.items():
            pose, report = pose_from_targets(prof, {k: v for k, v in targets.items() if v > 0})
            rows[viseme] = dict(prof['viseme_support'][viseme], targets=targets,
                                controls=[p[1] + '=' + format(p[2], '.3f') for p in pose['keys']] +
                                         [p[0] + '.' + p[1] + '[' + str(p[2]) + ']=' + format(p[3], '.4f') for p in pose['bones']])
        return dict(base, visemes=rows, viseme_set=list(VISEMES),
                    note='rig-independent visemes resolved through measured controls; lips_close uses the measured gap model')
    if name == 'asymmetry':
        return dict(base, **asymmetry(prof, raw, state, plan))
    raise ValueError('UNKNOWN_ANALYSIS: ' + name)


def _short(entry):
    entry = entry or {}
    return {'usable': entry.get('usable', False), 'confidence': entry.get('confidence'), 'control': _control_label(entry.get('control'))}


def asymmetry(prof, raw, state, plan):
    rows, findings = [], []
    if raw:
        stats = {}
        for p in raw['probe'].get('key_probes', []):
            stats[p['mesh'] + ':' + p['key']] = (p['stats'].get('max_displacement_m') or 0.0)
        for p in raw['probe'].get('bone_probes', []):
            stats[p['bone'] + ':' + p['path'] + '[' + str(p['index']) + ']' + ('+' if p['amount'] > 0 else '-')] = \
                (p['stats'].get('max_displacement_m') or 0.0)
        for semantic in sorted(prof['mapping']):
            if not semantic.endswith('_L'):
                continue
            twin = semantic[:-2] + '_R'
            a, b = prof['mapping'][semantic], prof['mapping'].get(twin)
            if not b or not a.get('usable') or not b.get('usable') or semantic.startswith('eye_'):
                continue
            ka = _probe_key(a['control'])
            kb = _probe_key(b['control'])
            da, db = stats.get(ka), stats.get(kb)
            if not da or not db:
                continue
            ratio = min(da, db) / max(da, db)
            row = {'pair': [semantic, twin], 'max_displacement_mm': [round(da * 1000, 3), round(db * 1000, 3)], 'ratio': round(ratio, 4)}
            if a['control'].get('limit') and b['control'].get('limit'):
                row['limits'] = [a['control']['limit'], b['control']['limit']]
            rows.append(row)
            if ratio < 0.85:
                findings.append({'type': 'RIG_CONTROL_ASYMMETRY', 'pair': [semantic, twin], 'ratio': round(ratio, 4),
                                 'severity': 'info', 'note': 'measured; preserved (never corrected automatically)'})
        eyes = raw['inspection'].get('eyes', [])
        if len(eyes) == 2:
            r = min(eyes[0]['radius_m'], eyes[1]['radius_m']) / max(eyes[0]['radius_m'], eyes[1]['radius_m'])
            rows.append({'pair': ['eye_L', 'eye_R'], 'radius_mm': [round(eyes[0]['radius_m'] * 1000, 3), round(eyes[1]['radius_m'] * 1000, 3)],
                         'ratio': round(r, 4)})
            if r < 0.95:
                findings.append({'type': 'EYE_SIZE_ASYMMETRY', 'ratio': round(r, 4), 'severity': 'info',
                                 'note': 'asset asymmetry (possibly intentional); never normalized'})
    performance = None
    if state and state.get('parts'):
        combined, _, _ = compose_tracks(plan, state['parts'], state.get('polish'))
        declared = {r['area'] + ':' + r['side'] for r in (state['parts'].get('asymmetry') or {}).get('params', {}).get('entries', [])}
        diffs = []
        for name in sorted(combined):
            if not name.endswith('_L') or name.startswith('eye_'):
                continue
            twin = name[:-2] + '_R'
            if twin not in combined:
                continue
            worst = max((abs(sample_value - _sample(combined[twin], f)) for f, sample_value in combined[name]), default=0.0)
            if worst > 0.05:
                diffs.append({'pair': [name, twin], 'max_difference': round(worst, 4)})
        performance = {'declared': sorted(declared), 'differences': diffs,
                       'blink_asymmetry': (state['parts'].get('blinks') or {}).get('params', {}).get('asymmetry')}
    return {'rig': rows, 'findings': findings, 'performance': performance,
            'rule': 'asymmetry is measured and reported; intentional asymmetry needs purpose, strength and timing'}


def _sample(track, f):
    from .tracks import sample
    return sample(track, f)


def _probe_key(control):
    if control['type'] == 'shape_key':
        return control['mesh'] + ':' + control['key']
    return control['bone'] + ':' + control['path'] + '[' + str(control['index']) + ']' + ('+' if control['full'] > 0 else '-')


# ---------------------------------------------------------------- jaw extreme validation

def validate_jaw_part(bridge, plan_like, part):
    pose, report = pose_from_targets(plan_like['profile'], part['peak'])
    measured = measure_poses(bridge, plan_like, [dict(pose, name='jaw_peak')])
    metrics = measured['poses'][0]['metrics']
    return pose_findings(metrics), metrics


# ---------------------------------------------------------------- speech helpers (pure)

def speech_context(storage, facial, options):
    o = dict(options or {})
    if facial:
        path, plan, state = load(storage, facial)
        return plan['brief'], plan['profile'].get('viseme_support')
    fps = o.get('fps', 30)
    duration = o.get('duration_s')
    timeline = o.get('timeline') or {}
    if duration is None:
        ends = [r.get('end', 0) for r in (timeline.get('phonemes') or timeline.get('visemes') or [])]
        duration = (max(ends) + 0.3) if ends and timeline.get('units', 'seconds') == 'seconds' else 3.0
    brief = create_brief('neutral', {'duration_s': min(15.0, max(0.5, duration)), 'fps': fps})
    return brief, None


def prepare_speech(storage, facial=None, options=None):
    o = dict(options or {})
    brief, support = speech_context(storage, facial, o)
    prepared = speech_mod.prepare(o.get('timeline'), brief['fps'], brief['frame_start'], brief['frame_end'])
    rows = [{k: (round(v, 3) if isinstance(v, float) else v) for k, v in s.items()} for s in prepared['segments']]
    out = {'status': 'prepared', 'segments': rows[:400], 'segment_count': prepared['segment_count'],
           'visemes_used': prepared['visemes_used'], 'speech_frames': [prepared['speech_start'], prepared['speech_end']],
           'words': prepared['words'][:100], 'energy_used_for': 'jaw amplitude only (x0.8..x1.2)' if prepared['energy'] else None,
           'fps': brief['fps'], 'frame_range': [brief['frame_start'], brief['frame_end']], 'no_action_created': True}
    if support:
        out['rig_support'] = {v: support[v]['status'] for v in prepared['visemes_used']}
    return out


def coarticulation(storage, facial=None, options=None):
    o = dict(options or {})
    brief, support = speech_context(storage, facial, o)
    prepared = speech_mod.prepare(o.get('timeline'), brief['fps'], brief['frame_start'], brief['frame_end'])
    fps = brief['fps']
    report = {}
    for label, hard in (('coarticulated', False), ('pose_to_pose', True)):
        tracks, approximations = speech_mod.coarticulate(prepared, fps, brief['frame_start'], brief['frame_end'],
                                                         o.get('overlap', 0.5), support, hard)
        check = speech_mod.validate_lipsync(tracks, prepared, fps)
        report[label] = {'max_rate_per_s': check['metrics']['max_rate_per_s'], 'mbp_closed': check['metrics']['mbp_closed'],
                         'mbp_segments': check['metrics']['mbp_segments'],
                         'max_closure_timing_error_ms': check['metrics']['max_closure_timing_error_ms'],
                         'findings': [f['type'] for f in check['findings']]}
    return {'status': 'measured', 'overlap': o.get('overlap', 0.5), 'comparison': report, 'fps': fps,
            'model': 'dominance functions (Cohen & Massaro 1993); closures M_B_P/F_V/W_Q are dominant',
            'no_action_created': True}


# ---------------------------------------------------------------- mocap

def mocap_source(options):
    source = (options or {}).get('source')
    if source is None:
        raise ValueError('MOCAP_SOURCE_REQUIRED: options.source = {fps, frame_start, channels} or a .json/.csv path')
    return mocap_mod.load(source)


def resample(tracks, src_fps, dst_fps, frame_start):
    if src_fps == dst_fps:
        return tracks
    from .tracks import sample
    out = {}
    for name, track in tracks.items():
        n = len(track)
        duration = (n - 1) / src_fps
        frames = int(math.floor(duration * dst_fps))
        src = [[(f - frame_start) / src_fps, v] for f, v in track]
        out[name] = [[frame_start + i, round(sample(src, i / dst_fps), 6)] for i in range(frames + 1)]
    return out


def mocap_map(bridge, storage, object_name=None, facial=None, options=None):
    o = dict(options or {})
    plan, state, path = _plan_like(bridge, storage, object_name, facial, o)
    mocap = mocap_source(o)
    result = mocap_mod.map_channels(mocap, plan['profile']['mapping'], o.get('mirror', False), o.get('eye_look_full_deg', 25.0))
    return {'status': 'blocked' if result['blocked'] else 'mapped', **result,
            'note': 'channel names map through normalized tokens and the measured semantic model; eye-look full angle is an assumption',
            'no_action_created': True}


def mocap_retarget(bridge, storage, operation_id, object_name=None, facial=None, options=None, apply=False):
    o = dict(options or {})
    mocap = mocap_source(o)
    seconds = mocap['frames'] / mocap['fps']
    if facial:
        path, plan, state = load(storage, facial)
    else:
        if not object_name:
            raise ValueError('OBJECT_OR_PLAN_REQUIRED')
        fps = mocap['fps'] if mocap['fps'] in (24, 25, 30, 48, 50, 60) else 30
        reference, _, _ = create_plan(bridge, storage, operation_id + '-plan', object_name, 'neutral',
                                      {'duration_s': round(min(15.0, max(0.5, (mocap['frames'] - 1) / mocap['fps'])), 4), 'fps': fps,
                                       'microexpressions': False}, o)
        path, plan, state = load(storage, reference)
    store.mutable(state)
    brief = plan['brief']
    mapped = mocap_mod.map_channels(mocap, plan['profile']['mapping'], o.get('mirror', False), o.get('eye_look_full_deg', 25.0))
    if mapped['blocked']:
        raise ValueError(mapped['reason'] + ' (mapped ' + str(mapped['mapped_ratio']) + '; unmapped ' + str(len(mapped['unmapped'])) + ')')
    neutral = mocap_mod.neutral_offsets(mocap, o.get('neutral_frames', 10), o.get('neutral'))
    tracks = mocap_mod.retarget_tracks(mocap, mapped, brief['frame_start'], neutral)
    tracks = resample(tracks, mocap['fps'], brief['fps'], brief['frame_start'])
    last = max(f for t in tracks.values() for f, _ in t)
    if last > brief['frame_end'] + 1e-6:
        raise ValueError('MOCAP_LONGER_THAN_CLIP: mocap ends at frame ' + str(last) + ', clip at ' + str(brief['frame_end']) + ' (never trimmed silently)')
    part = {'tracks': tracks, 'params': {'source': mocap['source'], 'fps': mocap['fps'], 'frames': mocap['frames'],
            'mirror': o.get('mirror', False), 'neutral_frames': o.get('neutral_frames', 10), 'mapped_ratio': mapped['mapped_ratio'],
            'unmapped': mapped['unmapped'], 'cleanup': None}, 'findings': [],
            'provenance': 'generic channel mocap retargeted through the semantic model (raw, before cleanup)'}
    summary = {'channels_mapped': sum(1 for r in mapped['channels'].values() if r['usable']), 'semantic_tracks': len(tracks),
               'keys': sum(len(t) for t in tracks.values()), 'seconds': round(seconds, 3), 'unmapped': mapped['unmapped']}
    if not apply:
        return dict(public(path, plan, state), status='proposed', dry_run=True, retarget=summary, no_action_created=True)
    fresh(bridge, plan, state)
    parts = {k: v for k, v in state.get('parts', {}).items() if k in (o.get('keep_parts') or [])}
    parts['mocap'] = part
    state['mocap_raw'] = {'tracks_keys': summary['keys'], 'source': mocap['source']}
    state = build_candidate(bridge, storage, operation_id, path, plan, state, parts, 'mocap retarget (raw)', None, o.get('qa'))
    return dict(public(path, plan, state), status='candidate_created', retarget=summary, written=state['written'],
                numeric_verification='pass' if state['technical_qa']['passed'] else 'fail', visual_verification='required')


def mocap_cleanup(bridge, storage, operation_id, facial=None, options=None, apply=False):
    o = dict(options or {})
    if not facial:
        raise ValueError('FACIAL_PLAN_REQUIRED: retarget mocap into a facial plan first')
    path, plan, state = load(storage, facial)
    store.mutable(state)
    part = (state.get('parts') or {}).get('mocap')
    if not part:
        raise ValueError('MOCAP_PART_REQUIRED: facial_mocap_retarget first')
    raw = part['params'].get('raw_tracks') or part['tracks']
    cleaned, report = mocap_mod.cleanup(raw, plan['brief']['fps'], o.get('cleanup'))
    summary = {'keys_before': report['keys_before'], 'keys_after': report['keys_after'],
               'noise_reduction_ratio': report['noise_reduction_ratio'], 'by_unit': report['by_unit'],
               'noise_reduction_definition': report['noise_reduction_definition'],
               'channels': {k: {kk: v[kk] for kk in ('keys_before', 'keys_after', 'hf_noise_before', 'hf_noise_after', 'max_deviation', 'mean_deviation', 'unit')}
                            for k, v in report['channels'].items()}}
    if not apply:
        return dict(public(path, plan, state), status='proposed', dry_run=True, cleanup=summary, no_action_created=True)
    fresh(bridge, plan, state)
    before = {'candidate': state.get('candidate_action'), 'qa': _qa_summary(state.get('technical_qa'))}
    new_part = {'tracks': cleaned, 'params': dict(part['params'], cleanup=report['options'], raw_tracks=raw),
                'findings': [], 'provenance': 'mocap cleanup (fast motion, blinks and closures protected)'}
    parts = dict(state['parts'], mocap=new_part)
    state = build_candidate(bridge, storage, operation_id, path, plan, state, parts, 'mocap cleanup', state.get('polish'), o.get('qa'))
    return dict(public(path, plan, state), status='candidate_created', cleanup=summary, before=before,
                after={'candidate': state['candidate_action'], 'qa': _qa_summary(state['technical_qa'])},
                numeric_verification='pass' if state['technical_qa']['passed'] else 'fail', visual_verification='required')


# ---------------------------------------------------------------- curves and layers

def analyze_curves(storage, facial, options=None):
    path, plan, state = load(storage, facial)
    if not state.get('parts'):
        raise ValueError('FACIAL_PARTS_REQUIRED: generate a layer first')
    combined, conflicts, _ = compose_tracks(plan, state['parts'], state.get('polish'))
    protected = L.protected_windows(state['parts'], plan['brief']['fps'])
    limits = {n: list(eye_mod.limits_for(plan['profile']['mapping'], n[-1], n.split('_')[1])[0]) for n in combined if n.startswith('eye_')}
    analysis = curve_mod.analyze(combined, plan['brief']['fps'], protected, limits)
    return {'status': 'analyzed', 'findings': analysis['findings'], 'thresholds': analysis['thresholds'],
            'channels': {k: v for k, v in analysis['channels'].items() if v['spike_count'] or v['overshoot_frames'] or v['holds']},
            'protected_windows': {k: len(v) for k, v in protected.items()}, 'no_action_created': True}


def polish_curves(bridge, storage, operation_id, facial, options=None, apply=False):
    o = dict(options or {})
    path, plan, state = load(storage, facial)
    store.mutable(state)
    if not state.get('parts'):
        raise ValueError('FACIAL_PARTS_REQUIRED: generate a layer first')
    polish = {'strength': o.get('strength', 0.5), 'simplify_tolerance': o.get('simplify_tolerance')}
    if not 0 < polish['strength'] <= 1:
        raise ValueError('POLISH_STRENGTH_INVALID: (0, 1]')
    combined, conflicts, report = compose_tracks(plan, state['parts'], polish)
    protected_dev = max((r['protected_max_deviation'] for r in report.values()), default=0.0)
    summary = {'keys_before': sum(r['keys_before'] for r in report.values()), 'keys_after': sum(r['keys_after'] for r in report.values()),
               'max_deviation': max((r['max_deviation'] for r in report.values()), default=0.0),
               'protected_max_deviation': protected_dev,
               'protected': 'blinks, bilabial/labiodental closures and saccade frames are not smoothed'}
    if protected_dev > 1e-6:
        raise ValueError('POLISH_TOUCHED_PROTECTED_WINDOW: ' + str(protected_dev))
    if not apply:
        return dict(public(path, plan, state), status='proposed', dry_run=True, polish=summary, no_action_created=True)
    fresh(bridge, plan, state)
    state = build_candidate(bridge, storage, operation_id, path, plan, state, state['parts'], 'polish curves', polish, o.get('qa'))
    return dict(public(path, plan, state), status='candidate_created', polish=summary,
                numeric_verification='pass' if state['technical_qa']['passed'] else 'fail', visual_verification='required')


def layer_conflicts(storage, facial):
    path, plan, state = load(storage, facial)
    parts = state.get('parts', {})
    combined, conflicts, _ = compose_tracks(plan, parts, state.get('polish'))
    channels, report = R.resolve(combined, plan['profile'], plan['head_policy'])
    layers = {}
    for name in parts:
        layers.setdefault(L.PART_LAYER[name], []).append(name)
    return {'status': 'analyzed', 'layers': layers, 'conflicts': conflicts, 'resolve_errors': report['errors'],
            'shared_controls': report['shared'], 'clamped': report['clamped'], 'unsupported': report['unsupported'],
            'ownership': plan['ownership'], 'blender_mapping': {
                'storage': 'one candidate Action per version: OBJECT slot (facial bones) + KEY slots (shape keys); one layer, one strip',
                'evaluation': 'NLA track on the armature and on each Key datablock; the body Action stays active and unchanged',
                'logical_layers': 'combined host-side by documented rules; their parts live in the facial ledger'},
            'no_action_created': True}


# ---------------------------------------------------------------- integration with the body

def integrate(bridge, storage, operation_id, facial, options=None):
    path, plan, state = load(storage, facial)
    store.mutable(state)
    fresh(bridge, plan, state)
    if not state.get('candidate_action'):
        raise ValueError('CANDIDATE_REQUIRED: generate a facial candidate first')
    brief = plan['brief']
    step = max(1, math.ceil((brief['frame_end'] - brief['frame_start'] + 1) / 60))
    frames = sorted(set(range(brief['frame_start'], brief['frame_end'] + 1, step)) | {brief['frame_end']})
    result = bridge.run(INTEGRATION, {'object_name': plan['object_name'], 'options': plan['set_options'],
                                      'candidate': state['candidate_action'], 'frames': frames, 'head_policy': plan['head_policy']})
    head_ok = plan['head_policy'] != 'BODY_OWNS' or result['max_head_neck_matrix_change'] <= 1e-6
    passed = result['body_action_unchanged'] and result['max_body_matrix_change'] <= 1e-6 and head_ok
    state['integration'] = dict(result, passed=passed, head_policy=plan['head_policy'], ownership=plan['ownership'])
    record(path, state)
    return dict(public(path, plan, state), status='integrated' if passed else 'error', integration=state['integration'],
                errors=[] if passed else ['BODY_CHANGED_BY_FACIAL_LAYER: ' + json.dumps({k: result[k] for k in ('body_action_unchanged', 'max_body_matrix_change', 'max_head_neck_matrix_change')})])


# ---------------------------------------------------------------- renders

def cameras(profile, views, options=None):
    o = dict(options or {})
    fr = profile['frame']
    origin, left, up, forward = fr['origin'], fr['left'], fr['up'], fr['forward']
    iod = fr['iod_m']
    # Face close-up: brows (+1.1 IOD above the eyes) to chin (about 0.9 IOD below the mouth), 15 % margin.
    top, bottom = 1.1, fr['mouth_z'] - 0.9
    center = (top + bottom) / 2
    lens = float(o.get('lens_mm', 85.0))
    sensor = 24.0
    height = (top - bottom) * iod * float(o.get('margin', 1.15))
    distance = height / 2 / math.tan(math.atan(sensor / 2 / lens))
    target = [origin[k] + up[k] * center * iod for k in range(3)]
    c45 = math.sqrt(0.5)

    def norm(v):
        n = math.sqrt(sum(x * x for x in v))
        return [x / n for x in v]
    directions = {'FRONT': forward, 'THREE_QUARTER_LEFT': norm([forward[k] * c45 + left[k] * c45 for k in range(3)]),
                  'THREE_QUARTER_RIGHT': norm([forward[k] * c45 - left[k] * c45 for k in range(3)]), 'PROFILE': left}
    for v in views:
        if v not in directions:
            raise ValueError('VIEW_INVALID: ' + ', '.join(VIEWS))
    rows = [{'view': v, 'direction': [round(x, 6) for x in directions[v]], 'target': [round(x, 6) for x in target],
             'distance_m': round(distance, 6)} for v in views]
    return rows, {'lens_mm': lens, 'sensor_mm': sensor, 'clip': [round(distance * 0.05, 6), round(distance * 10, 6)]}


def _render(bridge, plan, candidate, frames, views, size, folder, label, operation_id, options, follow_head):
    cams, settings = cameras(plan['profile'], views, options)
    dirs = {}
    for view in views:
        directory = folder / label / view
        directory.mkdir(parents=True)
        if len(str(directory / 'frame_000001.png')) >= 260:
            raise ValueError('OUTPUT_PATH_TOO_LONG: use a shorter production artifact store')
        dirs[view] = str(directory)
    params = dict(settings, object_name=plan['object_name'], options=plan['set_options'], candidate=candidate, cameras=cams,
                  frames=frames, width=size, height=size, directories=dirs, operation_id=operation_id + '_' + label,
                  head_bone=plan['profile']['head_bone'], follow_head=follow_head, head_policy=plan['head_policy'])
    write_json(folder / label / 'intent.json', params)
    result = bridge.run(RENDER, params)
    if result.get('status') != 'ok':
        raise ValueError('RENDER_FAILED: ' + json.dumps(result.get('errors', []))[:400])
    for series in result['series']:
        checked = _verify_images({'status': 'ok', 'files': series['files'], 'errors': []}, Path(dirs[series['view']]))
        if checked['status'] != 'ok' or [f['frame'] for f in series['files']] != frames:
            raise ValueError('RENDER_ARTIFACT_INVALID: incomplete sequence for ' + series['view'])
    return result, dirs, cams, settings


def _selection(plan, state, which):
    if which in (None, 'current'):
        return state.get('candidate_action'), 'candidate'
    if which == 'none':
        return None, 'body_only'
    for row in state.get('history', []):
        if row['candidate'] == which or 'v' + str(row['version']) == which:
            return row['candidate'], 'v' + str(row['version'])
    raise ValueError('COMPARE_TARGET_UNKNOWN: use none, current or a version from this plan history')


def render_closeup(bridge, storage, operation_id, facial, options=None):
    o = dict(options or {})
    path, plan, state = load(storage, facial)
    store.mutable(state)
    fresh(bridge, plan, state)
    if not state.get('candidate_action'):
        raise ValueError('CANDIDATE_REQUIRED')
    brief = plan['brief']
    events = L.event_frames(state['parts'])
    default = [brief['frame_start'], (brief['frame_start'] + brief['frame_end']) // 2, brief['frame_end']]
    default += events['blink_peaks'][:1] + events['mbp_centres'][:1]
    frames = sorted(set(o.get('frames') or default))
    if len(frames) > 12 or any(not brief['frame_start'] <= f <= brief['frame_end'] for f in frames):
        raise ValueError('CLOSEUP_FRAMES_INVALID: up to 12 frames inside the clip')
    views = list(o.get('views') or VIEWS)
    size = int(o.get('size', 384))
    if not 128 <= size <= 1024:
        raise ValueError('SIZE_INVALID: 128..1024')
    follow = o.get('follow_head', bool(plan['body']['action']))
    folder = storage.reserve('facial-qa', operation_id)
    began = time.perf_counter()
    labels = [_selection(plan, state, 'current')]
    if o.get('compare'):
        labels.append(_selection(plan, state, o['compare']))
    outputs = {}
    for candidate, label in labels:
        result, dirs, cams, settings = _render(bridge, plan, candidate, frames, views, size, folder, label, operation_id, o, follow)
        montages = []
        if len(views) == 4:
            for f in frames:
                grid = [[Path(dirs[v]) / ('frame_' + format(f, '06d') + '.png') for v in views[:2]],
                        [Path(dirs[v]) / ('frame_' + format(f, '06d') + '.png') for v in views[2:]]]
                target = folder / label / ('montage_' + format(f, '06d') + '.png')
                write_montage(grid, target)
                montages.append(str(target))
        outputs[label] = {'candidate': candidate, 'images': len(result['files']), 'montages': montages,
                          'render_backend': result.get('render_backend'), 'state_restored': result.get('state_restored')}
    closeup = {'status': 'ok', 'output_dir': str(folder), 'frames': frames, 'views': views, 'size': size,
               'cameras': cams, 'camera_settings': settings, 'identical_cameras': True, 'follow_head': follow, 'labels': outputs,
               'elapsed_s': round(time.perf_counter() - began, 3), 'candidate_action': state['candidate_action']}
    write_json(folder / 'closeup.json', closeup)
    state['closeup'] = {k: closeup[k] for k in ('output_dir', 'frames', 'views', 'candidate_action', 'elapsed_s')}
    record(path, state)
    return dict(public(path, plan, state), status='rendered', closeup=closeup)


def render_preview(bridge, storage, operation_id, facial, options=None, pending=False):
    o = dict(options or {})
    path, plan, state = load(storage, facial)
    store.mutable(state)
    fresh(bridge, plan, state)
    if not state.get('candidate_action'):
        raise ValueError('CANDIDATE_REQUIRED')
    if state.get('pending_refinement') and not pending:
        raise ValueError('REFINEMENT_REVIEW_REQUIRED: the pending refinement already has its render; review it')
    result = temporal_preview(bridge, storage, operation_id, plan, state, o)
    state['render'] = result
    state['review'] = None
    record(path, state)
    return dict(public(path, plan, state), status='rendered' if result['temporal_complete'] else 'error')


def temporal_preview(bridge, storage, operation_id, plan, state, o):
    brief = plan['brief']
    frames = list(range(brief['frame_start'], brief['frame_end'] + 1))
    views = list(o.get('views') or ['FRONT', 'THREE_QUARTER_LEFT'])
    size = int(o.get('size', 256))
    labels = [_selection(plan, state, 'current')]
    if o.get('compare'):
        labels.append(_selection(plan, state, o['compare']))
    images = len(frames) * len(views) * len(labels)
    if len(frames) > 901 or images > 1810 or images * size * size > 200_000_000:
        raise ValueError('CAPACITY_EXCEEDED: facial temporal preview exceeds 901 frames/view, 1810 images or 200 Mpixel')
    follow = o.get('follow_head', bool(plan['body']['action']))
    checkpoint = create_checkpoint(bridge, storage, 'Before M7 facial temporal preview', operation_id, 'M7 facial preview')['manifest']
    folder = storage.reserve('facial-qa', operation_id)
    began = time.perf_counter()
    animations = []
    series = {}
    backend = None
    for candidate, label in labels:
        result, dirs, cams, settings = _render(bridge, plan, candidate, frames, views, size, folder, label, operation_id, o, follow)
        backend = result.get('render_backend')
        for s in result['series']:
            target = folder / label / (s['view'] + '.png')
            write_apng(s['files'], target, brief['fps'])
            animations.append({'label': label, 'view': s['view'], 'path': str(target), 'sha256': sha256(target),
                               'frames': len(frames), 'fps': brief['fps']})
        series[label] = {'candidate': candidate, 'views': views}
    player = write_player(folder, frames, brief['fps'], labels, views)
    elapsed = round(time.perf_counter() - began, 3)
    result = {'status': 'ok', 'output_dir': str(folder), 'temporal_complete': len(animations) == len(views) * len(labels),
              'candidate_action': state['candidate_action'], 'animations': animations, 'player': player, 'labels': series,
              'cameras': cams, 'camera_settings': settings, 'identical_cameras': True, 'follow_head': follow,
              'render_backend': backend, 'checkpoint': checkpoint,
              'metrics': {'frames': len(frames), 'views': len(views), 'images': images, 'elapsed_s': elapsed,
                          'seconds_per_image': round(elapsed / max(1, images), 4)}}
    write_json(folder / 'result.json', result)
    return result


def write_player(folder, frames, fps, labels, views):
    """Local HTML player: real frame timing at normal speed and slow motion (1/2, 1/4), frame stepping."""
    names = [label for _, label in labels]
    html = ['<!doctype html><meta charset="utf-8"><title>M7 facial preview</title>',
            '<style>body{background:#1d1d1f;color:#eee;font:15px sans-serif;margin:16px}figure{display:inline-block;margin:4px}'
            'img{width:300px;image-rendering:auto}figcaption{font-size:12px;color:#bbb}button,select,input{font:15px sans-serif;margin:6px;padding:6px}</style>',
            '<h1 style="font-size:19px">M7 — Revisión facial temporal</h1>',
            '<button id="play">Pausar</button><button id="prev">Anterior</button><button id="next">Siguiente</button>',
            '<label>Frame <input id="frame" type="number"></label>',
            '<label>Velocidad <select id="speed"><option value="1">Normal</option><option value="0.5">Lenta (1/2)</option>'
            '<option value="0.25">Muy lenta (1/4)</option></select></label><p id="position"></p>']
    for label in names:
        html.append('<div><h2 style="font-size:16px">' + label + '</h2>')
        for view in views:
            html.append('<figure><img data-dir="' + label + '/' + view + '"><figcaption>' + view + '</figcaption></figure>')
        html.append('</div>')
    html.append('<script>const frames=' + json.dumps(frames) + ',fps=' + json.dumps(fps) + r''';let at=0,playing=true,last=null,elapsed=0;
const input=document.querySelector('#frame'),button=document.querySelector('#play');input.min=frames[0];input.max=frames[frames.length-1];
function show(){input.value=frames[at];document.querySelector('#position').textContent='Frame '+frames[at]+' · '+((frames[at]-frames[0])/fps).toFixed(3)+' s';
document.querySelectorAll('img').forEach(img=>img.src=img.dataset.dir+'/frame_'+String(frames[at]).padStart(6,'0')+'.png');}
function pause(){playing=false;last=null;elapsed=0;button.textContent='Reproducir';}
button.onclick=()=>{playing=!playing;last=null;elapsed=0;button.textContent=playing?'Pausar':'Reproducir';};
document.querySelector('#prev').onclick=()=>{pause();at=Math.max(0,at-1);show();};
document.querySelector('#next').onclick=()=>{pause();at=Math.min(frames.length-1,at+1);show();};
input.oninput=()=>{pause();at=Math.max(0,frames.indexOf(Number(input.value)));show();};
function tick(now){const dt=last===null?0:Math.max(0,now-last);last=now;
if(playing){elapsed+=dt*Number(document.querySelector('#speed').value);const steps=Math.floor(elapsed*fps/1000);
if(steps){at=(at+steps)%frames.length;elapsed-=steps*1000/fps;show();}}else{elapsed=0;}requestAnimationFrame(tick);}
show();requestAnimationFrame(tick);</script>''')
    target = folder / 'preview.html'
    target.write_text('\n'.join(html), encoding='utf-8')
    return str(target)


# ---------------------------------------------------------------- review, refine, accept, reject

def review_gate(review, render, candidate):
    if not render or render.get('status') != 'ok' or not render.get('temporal_complete'):
        raise ValueError('TEMPORAL_REVIEW_REQUIRED: render the full temporal facial preview first')
    if render.get('candidate_action') != candidate:
        raise ValueError('VISUAL_REVIEW_STALE: the preview shows another candidate')
    fields = {'reviewer', 'artistic_observations', 'decision', 'artifact_reference', 'temporal_reviewed'}
    if not isinstance(review, dict) or not fields <= set(review):
        raise ValueError('VISUAL_REVIEW_REQUIRED: reviewer, artistic_observations, decision, artifact_reference, temporal_reviewed')
    if review['artifact_reference'] != render['output_dir'] or review['temporal_reviewed'] is not True:
        raise ValueError('VISUAL_REVIEW_STALE: review must reference the current full temporal artifact')
    if review['decision'] not in ('ACCEPT', 'REJECT', 'REFINE') or not review['reviewer'] or not review['artistic_observations']:
        raise ValueError('VISUAL_REVIEW_INVALID: record artistic observations and an explicit decision')
    if any(k in review for k in ('score', 'aaa_score', 'quality_score', 'rating')):
        raise ValueError('VISUAL_REVIEW_INVALID: no quality score; technical findings and artistic observations stay separate')
    return review


def review_report(state, review):
    qa = state.get('technical_qa') or {}
    report = {'TECHNICAL_FINDINGS': {'passed': qa.get('passed'), 'metrics': qa.get('metrics'),
                                     'errors': [f for f in qa.get('findings', []) if f.get('severity') == 'error'],
                                     'warnings': [f for f in qa.get('findings', []) if f.get('severity') == 'warning'][:30],
                                     'source': 'measured by production tools'},
              'ARTISTIC_OBSERVATIONS': review['artistic_observations'], 'decision': review['decision'],
              'reviewer': review['reviewer'], 'artifact_reference': review['artifact_reference'],
              'views_reviewed': review.get('views_reviewed'), 'speeds_reviewed': review.get('speeds_reviewed'),
              'candidate_action': state.get('candidate_action'),
              'no_score': 'No AAA or quality score is produced; measurements and artistic judgement are separate.'}
    folder = Path(review['artifact_reference'])
    write_json(folder / 'review.json', report)
    lines = ['# Revisión facial', '', '## TECHNICAL FINDINGS', '', 'passed: ' + str(qa.get('passed')), '']
    for f in report['TECHNICAL_FINDINGS']['errors'] + report['TECHNICAL_FINDINGS']['warnings']:
        lines.append('- ' + f.get('type', '?') + ' ' + json.dumps({k: v for k, v in f.items() if k not in ('type',)})[:200])
    lines += ['', '## ARTISTIC OBSERVATIONS', '']
    obs = review['artistic_observations']
    lines += ['- ' + o for o in (obs if isinstance(obs, list) else [obs])]
    lines += ['', 'Decision: ' + review['decision'] + ' (' + review['reviewer'] + ')', '', report['no_score']]
    (folder / 'review.md').write_text('\n'.join(lines), encoding='utf-8')
    return report


def review(bridge, storage, operation_id, facial, options=None):
    o = dict(options or {})
    path, plan, state = load(storage, facial)
    store.mutable(state)
    fresh(bridge, plan, state)
    pending = state.get('pending_refinement')
    visual = review_gate(o.get('review'), state.get('render'), state.get('candidate_action'))
    report = review_report(state, visual)
    if pending:
        keep = visual['decision'] == 'ACCEPT'
        cycles = state['refinement_cycles']
        if not keep:
            previous = pending['previous']
            for key in ('candidate_action', 'candidate_fingerprint', 'version', 'parts', 'polish', 'technical_qa', 'render', 'review',
                        'resolve_report', 'conflicts', 'written', 'state'):
                state[key] = previous.get(key)
        state.update(refinement_cycles=cycles, pending_refinement=None,
                     last_refinement=dict(pending['summary'], visual_review=report['decision'], kept=keep))
        if keep:
            state.update(state='REVIEWED', review=report)
        record(path, state)
        return dict(public(path, plan, state), status='refined' if keep else 'reverted', review=report)
    state.update(state='REVIEWED', review=report)
    record(path, state)
    return dict(public(path, plan, state), status='reviewed', review=report)


def refine(bridge, storage, operation_id, facial, options=None, apply=False):
    o = dict(options or {})
    path, plan, state = load(storage, facial)
    store.mutable(state)
    if state.get('pending_refinement'):
        raise ValueError('REFINEMENT_REVIEW_REQUIRED: review the pending refinement (facial_review) first')
    store.refinement_gate(state)
    fresh(bridge, plan, state)
    if not state.get('candidate_action'):
        raise ValueError('CANDIDATE_REQUIRED')
    target = o.get('target')
    if target not in REFINE_TARGETS:
        raise ValueError('REFINEMENT_TARGET_UNSUPPORTED: measured targets ' + ', '.join(sorted(REFINE_TARGETS)))
    changes = o.get('changes') or {}
    if not changes.get('parts') and not changes.get('polish'):
        raise ValueError('REFINEMENT_CHANGES_REQUIRED: changes.parts {kind: options} and/or changes.polish')
    before = state['technical_qa']['metrics'].get(target)
    if not apply:
        return dict(public(path, plan, state), status='proposed', target=target, before=before, changes=changes,
                    remaining_cycles=3 - state.get('refinement_cycles', 0))
    minimum = o.get('minimum_improvement')
    if isinstance(minimum, bool) or not isinstance(minimum, (int, float)) or not math.isfinite(minimum) or minimum <= 0:
        raise ValueError('INVALID_IMPROVEMENT_THRESHOLD: declare a positive finite material improvement')
    previous = deepcopy(state)
    state['refinement_cycles'] = state.get('refinement_cycles', 0) + 1
    record(path, state)  # the cycle is consumed even if the attempt fails
    cycles = state['refinement_cycles']
    parts = dict(state['parts'])
    try:
        for kind, kind_options in (changes.get('parts') or {}).items():
            motion = gaze_motion(bridge, plan['object_name'], plan['profile'], plan['brief'], kind_options) \
                if kind in ('gaze', 'coordinate', 'saccades') else None
            parts = apply_parts(parts, make_parts(kind, plan['profile'], plan['brief'], plan['head_policy'], parts, kind_options, motion))
        polish = changes.get('polish', state.get('polish'))
        state = build_candidate(bridge, storage, operation_id, path, plan, state, parts, 'refinement ' + str(cycles), polish, o.get('qa'))
    except Exception as exc:
        previous.update(refinement_cycles=cycles, last_refinement={'target': target, 'outcome': 'safe_rejection', 'error': str(exc)[:300]})
        record(path, previous)
        return dict(public(path, plan, previous), status='error', errors=[str(exc)[:300]])
    after = state['technical_qa']['metrics'].get(target)
    direction = REFINE_TARGETS[target]
    improved = (isinstance(before, (int, float)) and isinstance(after, (int, float)) and state['technical_qa']['passed'] and
                ((before - after) if direction == 'lower' else (after - before)) >= minimum)
    summary = {'target': target, 'before': before, 'after': after, 'minimum_improvement': minimum, 'improved': improved,
               'candidate': state['candidate_action'], 'cycle': cycles}
    if not improved:
        bridge.run(MARK, {'object_name': plan['object_name'], 'options': plan['set_options'], 'candidate': state['candidate_action'],
                          'fingerprint': state['candidate_fingerprint'], 'body': plan['body'], 'lifecycle': 'REFINEMENT_REVERTED',
                          'qa_result': 'PASS' if state['technical_qa']['passed'] else 'FAIL', 'head_policy': plan['head_policy']})
        history = state['history']
        previous.update(refinement_cycles=cycles, last_refinement=dict(summary, reverted=True), history=history)
        record(path, previous)
        return dict(public(path, plan, previous), status='reverted', refinement=summary)
    render = temporal_preview(bridge, storage, operation_id + '-visual', plan, state, o.get('render') or {})
    state['render'] = render
    state['review'] = None
    state['pending_refinement'] = {'summary': summary, 'previous': {k: previous.get(k) for k in (
        'candidate_action', 'candidate_fingerprint', 'version', 'parts', 'polish', 'technical_qa', 'render', 'review',
        'resolve_report', 'conflicts', 'written', 'state')}}
    record(path, state)
    return dict(public(path, plan, state), status='visual_review_required', refinement=summary)


def accept(bridge, storage, operation_id, facial, options=None, apply=False):
    path, plan, state = load(storage, facial)
    store.mutable(state)
    fresh(bridge, plan, state)
    if not state.get('candidate_action'):
        raise ValueError('CANDIDATE_REQUIRED')
    if state.get('pending_refinement'):
        raise ValueError('REFINEMENT_REVIEW_REQUIRED')
    rev = state.get('review')
    render = state.get('render') or {}
    if not rev or rev['decision'] != 'ACCEPT' or rev['artifact_reference'] != render.get('output_dir') or \
            render.get('candidate_action') != state['candidate_action']:
        raise ValueError('VISUAL_REVIEW_REQUIRED: an ACCEPT review of the current full temporal preview is required')
    if not state['technical_qa']['passed']:
        raise ValueError('QA_FAILED: technical QA has errors; refine or reject')
    if not apply:
        return dict(public(path, plan, state), status='proposed', dry_run=True,
                    plan='persist the candidate as NLA track(s) "M7_FACE <candidate>" on the armature and Key datablocks; '
                         'the body Action stays active and unchanged')
    require_mutable_asset(bridge, storage, 'M7 facial accept')
    brief = plan['brief']
    step = max(1, math.ceil((brief['frame_end'] - brief['frame_start'] + 1) / 60))
    frames = sorted(set(range(brief['frame_start'], brief['frame_end'] + 1, step)) | {brief['frame_end']})
    integration = bridge.run(INTEGRATION, {'object_name': plan['object_name'], 'options': plan['set_options'],
                                           'candidate': state['candidate_action'], 'frames': frames, 'head_policy': plan['head_policy']})
    if not integration['body_action_unchanged'] or integration['max_body_matrix_change'] > 1e-6:
        raise ValueError('BODY_CHANGED_BY_FACIAL_LAYER: ' + json.dumps({k: integration[k] for k in ('body_action_unchanged', 'max_body_matrix_change')}))
    checkpoint = create_checkpoint(bridge, storage, 'Before accepting M7 facial candidate', operation_id, 'M7 facial accept')['manifest']
    mark = bridge.run(MARK, {'object_name': plan['object_name'], 'options': plan['set_options'], 'candidate': state['candidate_action'],
                             'fingerprint': state['candidate_fingerprint'], 'body': plan['body'], 'lifecycle': 'ACCEPTED',
                             'qa_result': 'PASS', 'head_policy': plan['head_policy']})
    if not mark['body_unchanged']:
        raise ValueError('BODY_ACTION_CHANGED during acceptance')
    state.update(state='ACCEPTED', candidate_fingerprint=mark['fingerprint'], accepted_tracks=mark['tracks'], checkpoint=checkpoint,
                 integration=dict(integration, passed=True))
    record(path, state)
    return dict(public(path, plan, state), status='accepted', tracks=mark['tracks'], body_unchanged=True,
                note='accepted facial layer stays separate from the body Action (NLA strips of the candidate Action)')


def reject(bridge, storage, operation_id, facial, options=None, apply=False):
    o = dict(options or {})
    path, plan, state = load(storage, facial)
    store.mutable(state)
    if not apply:
        return dict(public(path, plan, state), status='proposed', proposed_state='REJECTED', dry_run=True)
    require_mutable_asset(bridge, storage, 'M7 facial reject')
    checkpoint = create_checkpoint(bridge, storage, 'Before rejecting M7 facial candidate (retained)', operation_id, 'M7 facial reject')['manifest']
    mark = None
    if state.get('candidate_action'):
        fresh(bridge, plan, state)
        mark = bridge.run(MARK, {'object_name': plan['object_name'], 'options': plan['set_options'], 'candidate': state['candidate_action'],
                                 'fingerprint': state['candidate_fingerprint'], 'body': plan['body'], 'lifecycle': 'REJECTED',
                                 'qa_result': 'PASS' if (state.get('technical_qa') or {}).get('passed') else 'FAIL',
                                 'head_policy': plan['head_policy']})
    s = scene_status(bridge, plan['object_name'], plan['set_options'], state.get('candidate_action'))
    restored = {'no_facial_tracks': not any(state.get('candidate_action', '#') in t for t in s['facial_tracks']),
                'shape_key_values_as_planned': s['key_values'] == plan['rest_key_values'], 'body_unchanged': s['body'] == plan['body']}
    state.update(state='REJECTED', rejection=o.get('reason') or 'explicit rejection', checkpoint=checkpoint, restored=restored,
                 candidate_fingerprint=(mark or {}).get('fingerprint', state.get('candidate_fingerprint')))
    record(path, state)
    return dict(public(path, plan, state), status='rejected', preserved=True, restored=restored,
                note='candidate Action retained with lifecycle REJECTED; nothing attached to the character')
