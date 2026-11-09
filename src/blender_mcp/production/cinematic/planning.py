"""Brief, narrative beats, shots, actor paths, interactions, render plans and timeline planning (pure, no Blender).

CONTRACT kept by every extension of this module:
    normalize_plan(brief, beats, shots, source_settings) -> {'brief', 'beats', 'shots', 'timeline'}
validates a brief/beat/shot set against the source scene and returns the frozen copies the plan stores. The scene's
fps is never changed: a brief at another fps is refused (CINEMATIC_FPS_MISMATCH), never silently retimed.

Every planner below is pure: planning_handlers.py reads Blender through read-only payloads and passes plain data in.
Each planned field carries one of the three provenance values of model.PROVENANCE, kept apart:
USER_REQUIREMENT (the user asked for it), INFERRED_DEFAULT (taken from the scene, the actor registry or a fixed rule)
and PROPOSED_CREATIVE_DECISION (a creative choice the planner proposes and the user may override). Numbers that are
not measured are labelled threshold_source='PROPOSAL'; render rates are used only where a measurement is cited.
Seconds become frames only through model.seconds_to_frame, whose rounding record travels with the plan.
"""
from __future__ import annotations

from copy import deepcopy
import math
from pathlib import Path

from . import model

BRIEF_SCHEMA = 'production.cinematic_brief.v1'
BEATS_SCHEMA = 'production.cinematic_beats.v1'
SHOTS_SCHEMA = 'production.cinematic_shots.v1'
PATHS_SCHEMA = 'production.cinematic_actor_paths.v1'
INTERACTIONS_SCHEMA = 'production.cinematic_interactions.v1'
RENDER_PLAN_SCHEMA = 'production.cinematic_render_plan.v1'

USER, DEFAULT, PROPOSED = model.PROVENANCE

MAX_EVENTS = 64
MAX_BEATS = 32
MAX_PATHS = 16
MAX_INTERACTIONS = 32
MAX_LISTED_ACTIONS = 32
MAX_PRESENCE = 64

EVENT_KINDS = tuple(dict.fromkeys(model.BEAT_KINDS + model.INTERACTION_KINDS))
# An event whose kind is already a beat kind keeps it (USER_REQUIREMENT). Interaction kinds map to the beat that
# usually carries them; that mapping is a planner proposal and is labelled PROPOSED_CREATIVE_DECISION.
EVENT_TO_BEAT = {'OBSERVE': 'RECOGNITION', 'GAZE': 'RECOGNITION', 'TURN_TOWARD': 'RECOGNITION', 'REACH': 'INTERACTION',
                 'TOUCH_SURFACE': 'INTERACTION', 'HOLD_PROP': 'INTERACTION', 'HANDOFF_PROP': 'INTERACTION',
                 'PREPARE_ATTACK': 'THREAT', 'REACT': 'REACTION', 'RETREAT': 'REACTION'}
FACING_KINDS = frozenset({'OBSERVE', 'GAZE', 'TURN_TOWARD', 'RECOGNITION', 'PREPARE_ATTACK', 'THREAT'})
CONTACT_KINDS = frozenset({'TOUCH_SURFACE', 'HOLD_PROP', 'HANDOFF_PROP'})
REACH_KINDS = CONTACT_KINDS | {'REACH'}
MOVING_KINDS = {'APPROACH': 'MOVE_TOWARD', 'RETREAT': 'MOVE_AWAY'}
BEAT_PURPOSE = {
    'ESTABLISHING': 'Establish the space and where every actor is before the action starts.',
    'REVEAL': 'Reveal the subject to the audience.',
    'APPROACH': 'Close the distance between the subject and its target.',
    'RECOGNITION': 'Show that one character notices the other.',
    'THREAT': 'Make the danger explicit before it resolves.',
    'REACTION': 'Show the response to the previous beat.',
    'INTERACTION': 'Physical interaction between actors or with a prop.',
    'ENDING': 'Hold the final image of the sequence.',
    'CUSTOM': 'User-defined beat.',
}
BEAT_TO_SHOT = {'ESTABLISHING': 'ESTABLISHING', 'REVEAL': 'WIDE', 'APPROACH': 'TRACKING', 'RECOGNITION': 'CLOSE_UP',
                'THREAT': 'LOW_ANGLE', 'REACTION': 'REACTION', 'INTERACTION': 'MEDIUM', 'ENDING': 'WIDE',
                'CUSTOM': 'MEDIUM'}
# (subject_region, screen_height_fraction, headroom_fraction, horizontal_position, lens_mm, height_mode,
#  camera height as a fraction of the subject height, pitch_deg (+ looks up), movement, easing).
# Every value is a PROPOSAL (cinematography convention), never a measurement; the camera tools may override it.
SHOT_DEFAULTS = {
    'ESTABLISHING': ('FULL_BODY', 0.15, 0.30, 'CENTER', 24.0, 'HIGH', 1.5, -10.0, 'STATIC', 'LINEAR'),
    'WIDE': ('FULL_BODY', 0.40, 0.15, 'CENTER', 28.0, 'EYE_LEVEL', 0.9, 0.0, 'STATIC', 'LINEAR'),
    'MEDIUM': ('UPPER_BODY', 0.75, 0.10, 'LEFT_THIRD', 50.0, 'EYE_LEVEL', 0.9, 0.0, 'STATIC', 'LINEAR'),
    'CLOSE_UP': ('HEAD', 0.60, 0.08, 'LEFT_THIRD', 85.0, 'EYE_LEVEL', 0.92, 0.0, 'STATIC', 'LINEAR'),
    'EXTREME_CLOSE_UP': ('DETAIL', 0.80, 0.0, 'CENTER', 100.0, 'EYE_LEVEL', 0.92, 0.0, 'STATIC', 'LINEAR'),
    'OVER_THE_SHOULDER': ('UPPER_BODY', 0.60, 0.10, 'RIGHT_THIRD', 50.0, 'EYE_LEVEL', 0.9, 0.0, 'STATIC', 'LINEAR'),
    'LOW_ANGLE': ('FULL_BODY', 0.60, 0.10, 'CENTER', 24.0, 'LOW', 0.25, 15.0, 'STATIC', 'LINEAR'),
    'HIGH_ANGLE': ('FULL_BODY', 0.40, 0.15, 'CENTER', 35.0, 'HIGH', 1.6, -30.0, 'STATIC', 'LINEAR'),
    'TRACKING': ('FULL_BODY', 0.50, 0.15, 'LEFT_THIRD', 35.0, 'EYE_LEVEL', 0.9, 0.0, 'TRACKING', 'EASE_IN_OUT'),
    'REACTION': ('HEAD', 0.55, 0.10, 'RIGHT_THIRD', 85.0, 'EYE_LEVEL', 0.92, 0.0, 'STATIC', 'LINEAR'),
    'INSERT': ('DETAIL', 0.80, 0.0, 'CENTER', 100.0, 'EYE_LEVEL', 0.9, -15.0, 'STATIC', 'LINEAR'),
    'CUSTOM': ('FULL_BODY', 0.50, 0.15, 'CENTER', 50.0, 'EYE_LEVEL', 0.9, 0.0, 'STATIC', 'LINEAR'),
}
SUBJECT_REGIONS = ('FULL_BODY', 'UPPER_BODY', 'HEAD', 'DETAIL')
HORIZONTAL_POSITIONS = ('LEFT_THIRD', 'CENTER', 'RIGHT_THIRD')
HEIGHT_MODES = ('GROUND', 'LOW', 'EYE_LEVEL', 'HIGH', 'OVERHEAD')
# Region height as a fraction of the actor height, used only to suggest a camera distance. PROPOSAL (not measured
# on any rig): the camera tools measure the real framing afterwards.
REGION_FRACTION = {'FULL_BODY': 1.0, 'UPPER_BODY': 0.5, 'HEAD': 0.15, 'DETAIL': 0.08}
CAMERA_DEFAULTS = {'sensor_width_mm': 36.0, 'sensor_fit': 'AUTO',
                   'source': 'bpy.data.cameras.new defaults measured in Blender 5.2.1 (docs/shot-planning.md)'}
PROPOSALS = {
    'in_place_threshold_m': 0.05,       # below this measured root travel a clip counts as in place
    'speed_tolerance_ratio': 0.10,      # requested speed may differ this much from the clip before a conflict
    'clearance_m': 0.30,                # free gap between actor footprints
    'vertical_tolerance_m': 0.05,       # height change a ground locomotion clip is assumed to absorb
    'arrival_tolerance_m': 0.10,        # APPROACH success: final distance within stop distance + this
    'facing_tolerance_deg': 15.0,       # gaze / facing success criterion
    'yaw_tolerance_deg': 0.5,           # clip heading difference below which no staging rotation is proposed
    'render_call_budget_s': 120.0,      # under the 180 s socket timeout (common/transport.py TIMEOUT_SECONDS)
    'preview_long_side_px': 320,        # preview size; matches the measured Workbench/EEVEE rates below
    'first_chunk_frames': 10,           # first render chunk when no rate is measured for the configuration
}
M3_CONTACT_DEFAULTS = {'contact_height_m': 0.005, 'contact_speed_m_s': 0.03, 'minimum_dwell_frames': 3,
                       'source': 'M3 contacts/runtime.py contact_effector defaults'}
RENDER_ENGINES = ('BLENDER_WORKBENCH', 'BLENDER_EEVEE', 'CYCLES')
# Enum identifiers measured in Blender 5.2.1 (ImageFormatSettings.media_type/file_format, FFmpegSettings.format/codec).
RENDER_FORMATS = {
    'PNG_SEQUENCE': {'media_type': 'IMAGE', 'file_format': 'PNG'},
    'EXR_SEQUENCE': {'media_type': 'IMAGE', 'file_format': 'OPEN_EXR'},
    'MP4_H264': {'media_type': 'VIDEO', 'file_format': 'FFMPEG', 'ffmpeg_format': 'MPEG4', 'ffmpeg_codec': 'H264'},
}
QUALITY_PRESETS = ('PREVIEW', 'DRAFT', 'PRODUCTION')
PRESET_SAMPLES = {
    ('BLENDER_EEVEE', 'DRAFT'): (16, PROPOSED, 'PROPOSAL: the configuration of the measured EEVEE rate'),
    ('BLENDER_EEVEE', 'PRODUCTION'): (64, DEFAULT, 'Blender 5.2.1 default eevee.taa_render_samples (measured)'),
    ('CYCLES', 'DRAFT'): (64, PROPOSED, 'PROPOSAL'),
    ('CYCLES', 'PRODUCTION'): (256, PROPOSED, 'PROPOSAL'),
}
# Only measured rates. Anything else is NOT_MEASURED; the scenes measured were simple, so a heavier scene is slower.
MEASURED_RATES = (
    {'engine': 'BLENDER_WORKBENCH', 'resolution': [256, 256], 'samples': None, 'seconds_per_image': [0.026, 0.028],
     'source': 'docs/facial-qa.md (M7 final pass: 901 images in 26.0 s; facial close-up fixture, RTX 4060)'},
    {'engine': 'BLENDER_WORKBENCH', 'resolution': [320, 180], 'samples': None, 'seconds_per_image': [0.0185, 0.0281],
     'source': 'M10 API probe bpm10-understand-api (cube + area light, Blender 5.2.1, RTX 4060)'},
    {'engine': 'BLENDER_EEVEE', 'resolution': [320, 180], 'samples': 16, 'seconds_per_image': [0.486, 0.7124],
     'source': 'M10 API probe bpm10-understand-api (cube + area light, warm renders; the first cold render at 64 spp '
               'took 1.8044 s including shader compilation)'},
)

INTENT_KEYS = ('title', 'description', 'duration_s', 'fps', 'fps_base', 'frame_start', 'actors', 'events',
               'atmosphere', 'camera_intentions', 'lighting_intentions', 'interaction_requirements',
               'timing_constraints', 'required_performances', 'open_questions')
BEAT_OVERRIDE_KEYS = ('beat_id', 'kind', 'start_s', 'end_s', 'duration_s', 'weight', 'gap_before_s', 'purpose',
                      'actors', 'required_actions', 'camera_intention', 'staging_constraints', 'transition',
                      'dependencies', 'events')
SHOT_OVERRIDE_KEYS = ('shot_id', 'type', 'beat_id', 'start_s', 'end_s', 'frame_start', 'frame_end', 'subjects',
                      'framing', 'lens_mm', 'camera_height_m', 'camera_angle_deg', 'movement', 'easing',
                      'focus_target', 'transition', 'camera_id', 'notes')
FRAMING_KEYS = ('subject_region', 'screen_height_fraction', 'headroom_fraction', 'horizontal_position', 'eye_line')
PATH_OPTION_KEYS = ('paths', 'clearance_m', 'beats', 'source_scene', 'allow_retime')
PATH_KEYS = ('actor', 'start', 'target', 'stop_distance_m', 'orientation_target', 'speed_m_s', 'departure_s',
             'arrival_s', 'action', 'event_id')
INTERACTION_OPTION_KEYS = ('interactions', 'paths', 'beats', 'source_scene')
INTERACTION_KEYS = ('event_id', 'kind', 'actors', 'time_s', 'frame', 'spatial_target', 'contact_required',
                    'action_refs', 'success_criteria', 'effector', 'duration_s', 'description')
RENDER_OPTION_KEYS = ('format', 'resolution', 'fps', 'frame_range', 'engine', 'quality_preset', 'output_path',
                      'shots', 'shot_plan', 'samples', 'source_scene')


# ---------------------------------------------------------------------------------------------------- small helpers

def _items(value, field, maximum):
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        model.fail('CINEMATIC_LIST_INVALID', field + ' must be a list')
    if len(value) > maximum:
        model.fail('CINEMATIC_CAPACITY', field + ' accepts at most ' + str(maximum) + ' items')
    return list(value)


def _dict(value, field):
    if not isinstance(value, dict):
        model.fail('CINEMATIC_OBJECT_REQUIRED', field + ' must be an object')
    return value


def _flag(value, field):
    if not isinstance(value, bool):
        model.fail('CINEMATIC_BOOLEAN_REQUIRED', field + ' must be true or false')
    return value


def _ref(value, field):
    """Actor/object/Action reference. Blender names allow more than model.NAME, so only printable text is required."""
    if not isinstance(value, str) or not value or len(value) > 63 or value.strip() != value or not value.isprintable():
        model.fail('CINEMATIC_REF_INVALID', field + ' must be a printable name of 1-63 characters')
    return value


def _refs(value, field, maximum=32):
    return [_ref(v, field) for v in _items(value, field, maximum)]


def _num(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _groups(provenance):
    """The three provenance values, always present and separated."""
    return {value: sorted(k for k, v in provenance.items() if v == value) for value in model.PROVENANCE}


def _hdist(a, b):
    return math.hypot(b[0] - a[0], b[1] - a[1])


def _heading(a, b):
    return math.degrees(math.atan2(b[1] - a[1], b[0] - a[0]))


def _wrap(angle):
    return (angle + 180.0) % 360.0 - 180.0


def _r(value, digits=6):
    return round(float(value), digits)


def _rv(vector):
    return [_r(v) for v in vector]


def require_brief(brief):
    if not isinstance(brief, dict) or brief.get('schema') != BRIEF_SCHEMA:
        model.fail('CINEMATIC_BRIEF_REQUIRED', 'pass the brief returned by cinematic_create_brief (schema ' + BRIEF_SCHEMA + ')')
    if brief.get('ready') is False:
        codes = sorted({str(m.get('code')) for m in brief.get('missing_prerequisites') or []
                        if isinstance(m, dict) and m.get('blocking', True)})
        model.fail('CINEMATIC_BRIEF_NOT_READY', 'resolve the missing prerequisites first: ' + (', '.join(codes) or 'unknown'))
    if brief.get('fps') is None or brief.get('duration_s') is None:
        model.fail('CINEMATIC_BRIEF_INVALID', 'the brief needs fps and duration_s')
    return brief


def brief_timeline(brief):
    """Timeline of a brief without asking Blender: the brief carries the scene fps it was checked against."""
    return timeline_for(brief, {'fps': brief.get('fps'), 'fps_base': brief.get('fps_base', 1.0)})


def actor_ids(brief):
    return [a['actor_id'] for a in (brief or {}).get('actors') or [] if isinstance(a, dict) and 'actor_id' in a]


def _frame(seconds, timeline):
    return model.seconds_to_frame(seconds, timeline['fps'], timeline['fps_base'], timeline['frame_start'])


# ------------------------------------------------------------------------------------------------- timeline/contract

def timeline_for(brief, source_settings):
    fps = model.number(brief.get('fps', source_settings['fps']), 'brief.fps', 1, 240, integer=True)
    fps_base = model.number(brief.get('fps_base', source_settings['fps_base']), 'brief.fps_base', 0.001, 10)
    if fps != source_settings['fps'] or abs(fps_base - source_settings['fps_base']) > 1e-9:
        raise ValueError('CINEMATIC_FPS_MISMATCH: brief ' + str(fps) + '/' + str(fps_base) + ' fps, scene '
                         + str(source_settings['fps']) + '/' + str(source_settings['fps_base'])
                         + '; source Actions are never retimed silently')
    duration = model.number(brief.get('duration_s'), 'brief.duration_s', 0.1, model.MAX_DURATION_S)
    frame_start = model.number(brief.get('frame_start', 1), 'brief.frame_start', -100000, 100000, integer=True)
    end_frame, conversion = model.seconds_to_frame(duration, fps, fps_base, frame_start)
    return {'fps': fps, 'fps_base': fps_base, 'frame_start': frame_start, 'frame_end': end_frame - 1,
            'duration_s': duration, 'frames': end_frame - frame_start, 'conversions': [dict(conversion, purpose='duration')]}


def validate_beats(beats, timeline, actors):
    """Structural check of a beats object (plan_beats output) against the plan timeline. Returns the beat ids."""
    rows = beats.get('beats')
    if not isinstance(rows, list) or not rows:
        model.fail('CINEMATIC_BEATS_INVALID', 'beats.beats must be a non-empty list')
    if len(rows) > MAX_BEATS:
        model.fail('CINEMATIC_CAPACITY', 'at most ' + str(MAX_BEATS) + ' beats')
    stamp = beats.get('timeline')
    if isinstance(stamp, dict):
        for key in ('fps', 'frame_start', 'frame_end'):
            if key in stamp and stamp[key] != timeline[key]:
                model.fail('CINEMATIC_BEATS_STALE', 'beats were planned for ' + key + '=' + str(stamp[key])
                           + ', the plan timeline has ' + str(timeline[key]) + '; plan the beats again')
        if 'fps_base' in stamp and abs(stamp['fps_base'] - timeline['fps_base']) > 1e-9:
            model.fail('CINEMATIC_BEATS_STALE', 'beats were planned for another fps_base')
    ids, previous_end, known = [], None, set(actors)
    for row in rows:
        _dict(row, 'beat')
        beat_id = model.name(row.get('beat_id'), 'beat.beat_id')
        if beat_id in ids:
            model.fail('CINEMATIC_BEAT_ID_DUPLICATE', beat_id)
        ids.append(beat_id)
        if 'kind' in row:
            model.choice(row['kind'], beat_id + '.kind', model.BEAT_KINDS)
        if row.get('transition') is not None:
            model.choice(row['transition'], beat_id + '.transition', model.TRANSITIONS)
        start = model.number(row.get('frame_start'), beat_id + '.frame_start', integer=True)
        end = model.number(row.get('frame_end'), beat_id + '.frame_end', integer=True)
        if not timeline['frame_start'] <= start <= end <= timeline['frame_end']:
            model.fail('CINEMATIC_BEAT_OUTSIDE_TIMELINE', beat_id)
        if previous_end is not None and start <= previous_end:
            model.fail('CINEMATIC_BEAT_OVERLAP', beat_id + ' starts at ' + str(start) + ' before the previous beat ends')
        previous_end = end
        if 'start_s' in row and _frame(row['start_s'], timeline)[0] != start:
            model.fail('CINEMATIC_BEAT_CONVERSION_MISMATCH', beat_id + ' start_s does not convert to frame_start')
        if 'end_s' in row and _frame(row['end_s'], timeline)[0] - 1 != end:
            model.fail('CINEMATIC_BEAT_CONVERSION_MISMATCH', beat_id + ' end_s does not convert to frame_end')
        unknown = [a for a in row.get('actors') or [] if a not in known]
        if unknown:
            model.fail('CINEMATIC_BEAT_ACTOR_UNKNOWN', beat_id + ' names actors outside the brief: ' + ', '.join(map(str, unknown)))
    for row in rows:
        unknown = [d for d in row.get('dependencies') or [] if d not in ids]
        if unknown:
            model.fail('CINEMATIC_BEAT_DEPENDENCY_UNKNOWN', row['beat_id'] + ' depends on ' + ', '.join(map(str, unknown)))
    return ids


def validate_shots(rows, timeline, actors, beat_ids=None):
    """Structural check of shot rows. Optional fields are checked only when present (other subsystems may pass
    minimal rows), but ids are unique, rows are ordered and never overlap, and subjects are brief actors."""
    if not isinstance(rows, list):
        model.fail('CINEMATIC_SHOTS_INVALID', 'shots.shots must be a list')
    if len(rows) > model.MAX_SHOTS:
        model.fail('CINEMATIC_CAPACITY', 'at most ' + str(model.MAX_SHOTS) + ' shots')
    seen, previous_end, known = set(), None, set(actors)
    for shot in rows:
        _dict(shot, 'shot')
        start = model.number(shot.get('frame_start'), 'shot.frame_start', integer=True)
        end = model.number(shot.get('frame_end'), 'shot.frame_end', integer=True)
        if not timeline['frame_start'] <= start <= end <= timeline['frame_end']:
            raise ValueError('CINEMATIC_SHOT_OUTSIDE_TIMELINE: ' + str(shot.get('shot_id')))
        label = str(shot.get('shot_id'))
        if shot.get('shot_id') is not None:
            model.name(shot['shot_id'], 'shot.shot_id')
            if shot['shot_id'] in seen:
                model.fail('CINEMATIC_SHOT_ID_DUPLICATE', shot['shot_id'])
            seen.add(shot['shot_id'])
        if previous_end is not None and start <= previous_end:
            model.fail('CINEMATIC_SHOT_OVERLAP', label + ' starts at ' + str(start) + ' before the previous shot ends')
        previous_end = end
        for key, options in (('type', model.SHOT_TYPES), ('movement', model.CAMERA_MOVES), ('easing', model.EASINGS),
                             ('transition', model.TRANSITIONS)):
            if shot.get(key) is not None:
                model.choice(shot[key], label + '.' + key, options)
        if shot.get('camera_id') is not None:
            model.name(shot['camera_id'], label + '.camera_id')
        unknown = [s for s in shot.get('subjects') or [] if s not in known]
        if unknown:
            model.fail('CINEMATIC_SHOT_SUBJECT_UNKNOWN', label + ' frames actors outside the brief: ' + ', '.join(map(str, unknown)))
        if beat_ids is not None and shot.get('beat_id') is not None and shot['beat_id'] not in beat_ids:
            model.fail('CINEMATIC_SHOT_BEAT_UNKNOWN', label + ' references beat ' + str(shot['beat_id']))


def normalize_plan(brief, beats, shots, source_settings):
    if not isinstance(brief, dict) or brief.get('schema') != BRIEF_SCHEMA:
        raise ValueError('CINEMATIC_BRIEF_REQUIRED: pass the brief returned by cinematic_create_brief (schema '
                         + BRIEF_SCHEMA + ')')
    if brief.get('ready') is False:
        require_brief(brief)
    timeline = timeline_for(brief, source_settings)
    actors = brief.get('actors') or []
    if not isinstance(actors, list) or any(not isinstance(a, dict) or 'actor_id' not in a for a in actors):
        raise ValueError('CINEMATIC_BRIEF_INVALID: actors must be [{actor_id, role}]')
    ids = [a['actor_id'] for a in actors]
    if len(ids) != len(set(ids)):
        raise ValueError('CINEMATIC_BRIEF_INVALID: duplicated actor_id')
    for actor in actors:
        if actor.get('role') is not None:
            model.choice(actor['role'], 'actor.role', model.ACTOR_ROLES)
    if beats is not None and (not isinstance(beats, dict) or beats.get('schema') != BEATS_SCHEMA):
        raise ValueError('CINEMATIC_BEATS_INVALID: pass the result of cinematic_plan_beats')
    if shots is not None and (not isinstance(shots, dict) or shots.get('schema') != SHOTS_SCHEMA):
        raise ValueError('CINEMATIC_SHOTS_INVALID: pass the result of cinematic_plan_shots')
    beat_ids = validate_beats(beats, timeline, ids) if beats is not None else None
    validate_shots((shots or {}).get('shots', []), timeline, ids, beat_ids)
    return {'brief': deepcopy(brief), 'beats': deepcopy(beats), 'shots': deepcopy((shots or {}).get('shots', [])),
            'timeline': timeline}


# --------------------------------------------------------------------------------------------------------- the brief

def _target_raw(value, field):
    if isinstance(value, str):
        return {'ref': _ref(value, field)}
    if isinstance(value, (list, tuple)):
        return {'type': 'POINT', 'point': model.vector(value, field)}
    data = model.closed(_dict(value, field), {'actor', 'object', 'point'})
    if len(data) != 1:
        model.fail('CINEMATIC_TARGET_INVALID', field + ' needs exactly one of actor, object, point')
    if 'point' in data:
        return {'type': 'POINT', 'point': model.vector(data['point'], field + '.point')}
    if 'actor' in data:
        return {'actor_ref': _ref(data['actor'], field + '.actor')}
    return {'type': 'OBJECT', 'object': _ref(data['object'], field + '.object')}


def _intent_actor(row, index):
    field = 'actors[' + str(index) + ']'
    row = model.closed(_dict(row, field), {'actor_id', 'object', 'role', 'label'})
    if 'actor_id' not in row and 'object' not in row:
        model.fail('CINEMATIC_ACTOR_REF_REQUIRED', field + ' needs actor_id or object')
    item = {}
    for key in ('actor_id', 'object'):
        if key in row:
            item[key] = _ref(row[key], field + '.' + key)
    if 'role' in row:
        item['role'] = model.choice(row['role'], field + '.role', model.ACTOR_ROLES)
    if 'label' in row:
        item['label'] = model.name(row['label'], field + '.label')
    return item


def _intent_event(row, index, limit):
    field = 'events[' + str(index) + ']'
    row = model.closed(_dict(row, field), {'event_id', 'kind', 'actors', 'time_s', 'target', 'description', 'action_refs'})
    event = {'kind': model.choice(row.get('kind'), field + '.kind', EVENT_KINDS),
             'actor_refs': _refs(row.get('actors'), field + '.actors', model.MAX_ACTORS),
             'action_refs': _refs(row.get('action_refs'), field + '.action_refs')}
    if 'event_id' in row:
        event['event_id'] = model.name(row['event_id'], field + '.event_id')
    if 'time_s' in row:
        event['time_s'] = model.number(row['time_s'], field + '.time_s', 0, limit)
    if 'target' in row:
        event['target'] = _target_raw(row['target'], field + '.target')
    if 'description' in row:
        event['description'] = model.text(row['description'], field + '.description', 1000)
    return event


def _intent_requirement(row, index, limit):
    field = 'interaction_requirements[' + str(index) + ']'
    row = model.closed(_dict(row, field), {'kind', 'actors', 'target', 'time_s', 'event_id', 'contact_required',
                                           'action_refs', 'effector', 'description'})
    item = {'kind': model.choice(row.get('kind'), field + '.kind', model.INTERACTION_KINDS),
            'actor_refs': _refs(row.get('actors'), field + '.actors', model.MAX_ACTORS),
            'action_refs': _refs(row.get('action_refs'), field + '.action_refs')}
    if 'target' in row:
        item['target'] = _target_raw(row['target'], field + '.target')
    if 'time_s' in row:
        item['time_s'] = model.number(row['time_s'], field + '.time_s', 0, limit)
    if 'event_id' in row:
        item['event_id'] = model.name(row['event_id'], field + '.event_id')
    if 'contact_required' in row:
        item['contact_required'] = _flag(row['contact_required'], field + '.contact_required')
    if 'effector' in row:
        item['effector'] = _ref(row['effector'], field + '.effector')
    if 'description' in row:
        item['description'] = model.text(row['description'], field + '.description', 1000)
    return item


def _intent_constraint(row, index, limit):
    field = 'timing_constraints[' + str(index) + ']'
    row = model.closed(_dict(row, field), {'event_id', 'beat_kind', 'at_s', 'not_before_s', 'not_after_s',
                                           'min_duration_s', 'max_duration_s', 'description'})
    if ('event_id' in row) == ('beat_kind' in row):
        model.fail('CINEMATIC_CONSTRAINT_INVALID', field + ' needs exactly one of event_id, beat_kind')
    item = {}
    if 'event_id' in row:
        item['event_id'] = model.name(row['event_id'], field + '.event_id')
        allowed = ('at_s', 'not_before_s', 'not_after_s')
    else:
        item['beat_kind'] = model.choice(row['beat_kind'], field + '.beat_kind', model.BEAT_KINDS)
        allowed = ('min_duration_s', 'max_duration_s')
    for key in ('at_s', 'not_before_s', 'not_after_s', 'min_duration_s', 'max_duration_s'):
        if key in row:
            if key not in allowed:
                model.fail('CINEMATIC_CONSTRAINT_INVALID', field + '.' + key + ' does not apply to this target')
            item[key] = model.number(row[key], field + '.' + key, 0, limit)
    if len(item) == 1:
        model.fail('CINEMATIC_CONSTRAINT_INVALID', field + ' sets no limit')
    if 'description' in row:
        item['description'] = model.text(row['description'], field + '.description', 500)
    return item


def _intent_camera(row, index):
    field = 'camera_intentions[' + str(index) + ']'
    if isinstance(row, str):
        return {'intent': model.text(row, field, 500), 'subject_refs': []}
    row = model.closed(_dict(row, field), {'intent', 'shot_type', 'event_id', 'beat_kind', 'movement', 'subjects'})
    item = {'intent': model.text(row.get('intent'), field + '.intent', 500),
            'subject_refs': _refs(row.get('subjects'), field + '.subjects', model.MAX_ACTORS)}
    if 'shot_type' in row:
        item['shot_type'] = model.choice(row['shot_type'], field + '.shot_type', model.SHOT_TYPES)
    if 'movement' in row:
        item['movement'] = model.choice(row['movement'], field + '.movement', model.CAMERA_MOVES)
    if 'event_id' in row:
        item['event_id'] = model.name(row['event_id'], field + '.event_id')
    if 'beat_kind' in row:
        item['beat_kind'] = model.choice(row['beat_kind'], field + '.beat_kind', model.BEAT_KINDS)
    return item


def _intent_performance(row, index):
    field = 'required_performances[' + str(index) + ']'
    row = model.closed(_dict(row, field), {'actor', 'body_actions', 'facial', 'secondary'})
    item = {'actor': _ref(row.get('actor'), field + '.actor'),
            'body_actions': _refs(row.get('body_actions'), field + '.body_actions')}
    for key in ('facial', 'secondary'):
        value = row.get(key, False)
        item[key] = value if isinstance(value, bool) else _refs(value, field + '.' + key, 8)
    return item


def normalize_intent(raw):
    """Validate the structured intention written by the client agent. The server never interprets free text: it
    only checks types, vocabularies and ranges. Unknown keys are refused."""
    if not isinstance(raw, dict) or not raw:
        model.fail('CINEMATIC_INTENT_REQUIRED', 'pass the structured user intention in brief= (duration_s, actors, events...)')
    if 'schema' in raw:
        model.fail('CINEMATIC_INTENT_INVALID', 'brief= already carries schema ' + str(raw.get('schema'))
                   + '; cinematic_create_brief takes the user intention, the other planners take its result')
    data = model.closed(raw, INTENT_KEYS)
    intent = {'_given': sorted(data)}
    if 'title' in data:
        intent['title'] = model.text(data['title'], 'title', 200)
    if 'description' in data:
        intent['description'] = model.text(data['description'], 'description', 4000)
    if 'duration_s' in data:
        intent['duration_s'] = model.number(data['duration_s'], 'duration_s', 0.1, model.MAX_DURATION_S)
    if 'fps' in data:
        intent['fps'] = model.number(data['fps'], 'fps', 1, 240, integer=True)
    if 'fps_base' in data:
        intent['fps_base'] = model.number(data['fps_base'], 'fps_base', 0.001, 10.0)
    if 'frame_start' in data:
        intent['frame_start'] = model.number(data['frame_start'], 'frame_start', -100000, 100000, integer=True)
    limit = intent.get('duration_s', model.MAX_DURATION_S)
    intent['actors'] = [_intent_actor(r, i) for i, r in enumerate(_items(data.get('actors'), 'actors', model.MAX_ACTORS))]
    intent['events'] = [_intent_event(r, i, limit) for i, r in enumerate(_items(data.get('events'), 'events', MAX_EVENTS))]
    given_ids = [e['event_id'] for e in intent['events'] if 'event_id' in e]
    if len(given_ids) != len(set(given_ids)):
        model.fail('CINEMATIC_EVENT_ID_DUPLICATE', 'event_id values must be unique')
    if 'atmosphere' in data:
        atmosphere = model.closed(_dict(data['atmosphere'], 'atmosphere'),
                                  {'mood', 'mood_preset', 'time_of_day', 'weather', 'notes'})
        intent['atmosphere'] = {k: (model.choice(v, 'atmosphere.mood_preset', model.MOOD_PRESETS) if k == 'mood_preset'
                                    else model.text(v, 'atmosphere.' + k, 500)) for k, v in atmosphere.items()}
    intent['camera_intentions'] = [_intent_camera(r, i) for i, r in
                                   enumerate(_items(data.get('camera_intentions'), 'camera_intentions', 32))]
    if 'lighting_intentions' in data:
        lighting = model.closed(_dict(data['lighting_intentions'], 'lighting_intentions'), {'preset', 'notes'})
        intent['lighting_intentions'] = {k: (model.choice(v, 'lighting_intentions.preset', model.MOOD_PRESETS)
                                             if k == 'preset' else model.text(v, 'lighting_intentions.notes', 1000))
                                         for k, v in lighting.items()}
    intent['interaction_requirements'] = [_intent_requirement(r, i, limit) for i, r in enumerate(
        _items(data.get('interaction_requirements'), 'interaction_requirements', MAX_INTERACTIONS))]
    intent['timing_constraints'] = [_intent_constraint(r, i, limit) for i, r in enumerate(
        _items(data.get('timing_constraints'), 'timing_constraints', 64))]
    intent['required_performances'] = [_intent_performance(r, i) for i, r in enumerate(
        _items(data.get('required_performances'), 'required_performances', model.MAX_ACTORS))]
    intent['open_questions'] = [model.text(q, 'open_questions', 500) for q in
                                _items(data.get('open_questions'), 'open_questions', 32)]
    return intent


def _body_rows(record):
    rows = []
    for row in (record or {}).get('body_actions') or []:
        if isinstance(row, str):
            row = {'action': row}
        if isinstance(row, dict) and isinstance(row.get('action'), str):
            rows.append(row)
    return rows


def _states(record, key):
    return [r for r in (record or {}).get(key) or [] if isinstance(r, dict)]


def _root_motion(row):
    motion = row.get('root_motion')
    if not isinstance(motion, dict):
        return None
    keep = ('horizontal_distance_m', 'duration_frames', 'direction', 'vertical_distance_m', 'bone',
            'average_speed_m_s', 'source')
    return {k: motion[k] for k in keep if k in motion}


def _locomotion(row, rate):
    """A clip usable for travel: measured root motion (registry, measured by cinematic_register_actor) with a
    horizontal travel above the in-place proposal. Returns (clip, reason_if_not)."""
    motion = row.get('root_motion') if isinstance(row.get('root_motion'), dict) else None
    if motion is None:
        return None, 'ROOT_MOTION_NOT_MEASURED'
    distance, frames = motion.get('horizontal_distance_m'), motion.get('duration_frames')
    if not _num(distance) or not _num(frames) or frames <= 0:
        return None, 'ROOT_MOTION_NOT_MEASURED'
    if distance < PROPOSALS['in_place_threshold_m']:
        return None, 'IN_PLACE'
    direction = motion.get('direction')
    if not (isinstance(direction, (list, tuple)) and len(direction) >= 2 and all(_num(v) for v in direction[:2])
            and math.hypot(direction[0], direction[1]) > 1e-9):
        direction = None
    return {'action': row['action'], 'slot': row.get('slot'), 'frame_range': row.get('frame_range'),
            'cycle_distance_m': float(distance), 'cycle_frames': float(frames),
            'speed_m_s': float(distance) / (float(frames) / rate), 'direction': list(direction[:2]) if direction else None}, None


def _measure(record, presence):
    """Bounds measured now in the open file (read-only): actor object plus its registered meshes."""
    objects = (presence or {}).get('objects') or {}
    names = [record['object']] + [m for m in record.get('meshes') or [] if isinstance(m, str)]
    rows = [(n, objects.get(n)) for n in names if objects.get(n)]
    if not rows:
        return None
    root = objects.get(record['object'])
    dims = [r['dimensions'] for _, r in rows if isinstance(r.get('dimensions'), (list, tuple)) and len(r['dimensions']) == 3]
    return {'location_m': _rv(root['location']) if root else None, 'yaw_deg': _r(root['yaw_deg']) if root else None,
            'height_m': _r(max(d[2] for d in dims)) if dims else None,
            'footprint_radius_m': _r(max(math.hypot(d[0], d[1]) / 2 for d in dims)) if dims else None,
            'frame': (presence or {}).get('frame_current'), 'objects': [n for n, _ in rows], 'source': 'SCENE_MEASURED',
            'method': 'max over the actor object and its meshes of Object.dimensions; radius = half XY diagonal'}


def presence_request(intent, records, extra_refs=()):
    """Names to look up read-only: every registered actor object and mesh, plus references that may be objects."""
    objects, actions = [], []
    for record in records:
        for name in [record.get('object')] + list(record.get('meshes') or []):
            if isinstance(name, str) and name not in objects:
                objects.append(name)
    refs = list(extra_refs)
    for event in (intent or {}).get('events') or []:
        refs.append((event.get('target') or {}).get('ref') or (event.get('target') or {}).get('object'))
        actions += event.get('action_refs') or []
    for item in (intent or {}).get('interaction_requirements') or []:
        refs.append((item.get('target') or {}).get('ref') or (item.get('target') or {}).get('object'))
        actions += item.get('action_refs') or []
    for item in (intent or {}).get('required_performances') or []:
        actions += item.get('body_actions') or []
    for ref in refs:
        if isinstance(ref, str) and ref not in objects:
            objects.append(ref)
    unique_actions = []
    for name in actions:
        if name not in unique_actions:
            unique_actions.append(name)
    return {'objects': objects[:MAX_PRESENCE], 'actions': unique_actions[:MAX_PRESENCE]}


class _Cast:
    """Actor resolution for a brief: declared actors first, then registered actors named by events."""

    def __init__(self, records, provenance, missing):
        self.records = [r for r in records if isinstance(r, dict) and isinstance(r.get('actor_id'), str)]
        self.by_id = {r['actor_id']: r for r in self.records}
        self.actors, self.index, self.unresolved = [], {}, set()
        self.provenance, self.missing = provenance, missing

    def lookup(self, ref):
        if ref in self.by_id:
            return [self.by_id[ref]]
        return [r for r in self.records if r.get('object') == ref]

    def add(self, record, role, label, origin):
        aid = record['actor_id']
        if aid in self.index.values():
            if origin == USER and any(a['actor_id'] == aid for a in self.actors):
                model.fail('CINEMATIC_ACTOR_DUPLICATE', aid + ' is declared twice')
            return aid
        role_origin = USER if role else DEFAULT
        if role is None:
            role = record.get('role') if record.get('role') in model.ACTOR_ROLES else 'CUSTOM'
        row = {'actor_id': aid, 'role': role, 'object': record['object']}
        if label:
            row['label'] = label
        self.actors.append(row)
        self.provenance['actors.' + aid] = origin
        self.provenance['actors.' + aid + '.role'] = role_origin
        for key in (aid, record['object'], label):
            if key:
                self.index[key] = aid
        return aid

    def declare(self, row):
        ref = row.get('actor_id') or row.get('object')
        matches = self.lookup(row['actor_id']) if 'actor_id' in row else self.lookup(row['object'])
        if 'actor_id' in row and matches and 'object' in row and matches[0]['object'] != row['object']:
            self.missing.append({'code': 'ACTOR_OBJECT_MISMATCH', 'ref': ref, 'blocking': True,
                                 'detail': 'actor ' + row['actor_id'] + ' is registered for ' + matches[0]['object']})
            return
        if len(matches) == 1:
            aid = self.add(matches[0], row.get('role'), row.get('label'), USER)
            for key in (row.get('actor_id'), row.get('object')):
                if key:
                    self.index[key] = aid
            return
        self.unresolved.add(ref)
        if row.get('label'):
            self.unresolved.add(row['label'])
        if matches:
            self.missing.append({'code': 'ACTOR_AMBIGUOUS', 'ref': ref, 'blocking': True,
                                 'detail': 'several registrations for this object (fingerprint changed); pass actor_id: '
                                           + ', '.join(sorted(m['actor_id'] for m in matches))})
        else:
            self.missing.append({'code': 'ACTOR_NOT_REGISTERED', 'ref': ref, 'blocking': True,
                                 'detail': 'register it with cinematic_register_actor before planning'})

    def resolve(self, ref, context):
        if ref in self.index:
            return self.index[ref]
        if ref in self.unresolved:
            return None
        matches = self.lookup(ref)
        if len(matches) == 1:
            return self.add(matches[0], None, None, DEFAULT)
        self.unresolved.add(ref)
        self.missing.append({'code': 'ACTOR_AMBIGUOUS' if matches else 'ACTOR_NOT_REGISTERED', 'ref': ref, 'blocking': True,
                             'detail': context + ' names ' + ref + (' (several registrations)' if matches else
                                                                     '; register it with cinematic_register_actor')})
        return None

    def target(self, raw, context):
        if raw is None:
            return None
        if raw.get('type') in ('POINT', 'OBJECT'):
            return dict(raw)
        ref = raw.get('ref') or raw.get('actor_ref')
        if ref in self.index or ref in self.unresolved or 'actor_ref' in raw or self.lookup(ref):
            aid = self.resolve(ref, context)
            return {'type': 'ACTOR', 'actor_id': aid} if aid else {'type': 'UNRESOLVED', 'ref': ref}
        return {'type': 'OBJECT', 'object': ref}


def _next_id(prefix, counter, used):
    while True:
        counter += 1
        candidate = prefix + format(counter, '02d')
        if candidate not in used:
            used.add(candidate)
            return candidate, counter


def build_brief(intent, info, records, presence, registry_key):
    """production.cinematic_brief.v1 from a normalized intention, the source scene and the actor registry.
    Critical story requirements are never invented: what is missing goes to missing_prerequisites/open_questions."""
    settings = info['settings']
    given = set(intent['_given'])
    prov, missing, risks = {}, [], []
    questions = list(intent['open_questions'])
    fps = intent.get('fps', settings['fps'])
    fps_base = intent.get('fps_base', settings['fps_base'])
    if fps != settings['fps'] or abs(fps_base - settings['fps_base']) > 1e-9:
        raise ValueError('CINEMATIC_FPS_MISMATCH: brief ' + str(fps) + '/' + str(fps_base) + ' fps, scene '
                         + str(settings['fps']) + '/' + str(settings['fps_base'])
                         + '; source Actions are never retimed silently')
    frame_start = intent.get('frame_start', settings['frame_start'])
    for key in ('fps', 'fps_base', 'frame_start'):
        prov[key] = USER if key in given else DEFAULT
    duration = intent.get('duration_s')
    if duration is None:
        missing.append({'code': 'DURATION_REQUIRED', 'blocking': True, 'detail': 'the sequence duration was not given'})
        questions.append('How long is the sequence (duration_s)?')
    else:
        prov['duration_s'] = USER
    cast = _Cast(records, prov, missing)
    for row in intent['actors']:
        cast.declare(row)
    used = {e['event_id'] for e in intent['events'] if 'event_id' in e}
    counter, events = 0, []
    for raw in intent['events']:
        event_id = raw.get('event_id')
        if event_id is None:
            event_id, counter = _next_id('E', counter, used)
            prov['events.' + event_id + '.event_id'] = DEFAULT
        context = 'event ' + event_id
        performers = [a for a in (cast.resolve(r, context) for r in raw['actor_refs']) if a]
        event = {'event_id': event_id, 'kind': raw['kind'], 'actors': list(dict.fromkeys(performers)),
                 'time_s': raw.get('time_s'), 'target': cast.target(raw.get('target'), context),
                 'description': raw.get('description'), 'action_refs': raw['action_refs']}
        if duration is not None and event['time_s'] is not None and event['time_s'] >= duration:
            model.fail('CINEMATIC_EVENT_OUTSIDE_DURATION', event_id + ' at ' + str(event['time_s'])
                       + ' s is not before the end of the sequence (' + str(duration) + ' s)')
        if not raw['actor_refs'] and raw['kind'] not in ('ESTABLISHING', 'ENDING', 'CUSTOM'):
            questions.append('Which actor performs ' + event_id + ' (' + raw['kind'] + ')?')
        events.append(event)
    event_ids = {e['event_id'] for e in events}
    requirements = []
    for index, raw in enumerate(intent['interaction_requirements']):
        if raw.get('event_id') is not None and raw['event_id'] not in event_ids:
            model.fail('CINEMATIC_EVENT_UNKNOWN', 'interaction_requirements[' + str(index) + '] references ' + raw['event_id'])
        context = 'interaction requirement ' + str(index)
        item = {k: v for k, v in raw.items() if k not in ('actor_refs', 'target')}
        item['actors'] = [a for a in (cast.resolve(r, context) for r in raw['actor_refs']) if a]
        item['target'] = cast.target(raw.get('target'), context)
        requirements.append(item)
    for item in intent['timing_constraints']:
        if 'event_id' in item and item['event_id'] not in event_ids:
            model.fail('CINEMATIC_EVENT_UNKNOWN', 'a timing constraint references ' + item['event_id'])
    cameras = []
    for raw in intent['camera_intentions']:
        if raw.get('event_id') is not None and raw['event_id'] not in event_ids:
            model.fail('CINEMATIC_EVENT_UNKNOWN', 'a camera intention references ' + raw['event_id'])
        item = {k: v for k, v in raw.items() if k != 'subject_refs'}
        item['subjects'] = [a for a in (cast.resolve(r, 'camera intention') for r in raw['subject_refs']) if a]
        cameras.append(item)
    # Resolve every reference before listing performances: a registered actor named only here joins the cast.
    requested = [(cast.resolve(r['actor'], 'required performance'), r) for r in intent['required_performances']]
    records_by_id = cast.by_id
    objects = (presence or {}).get('objects') or {}
    actions_found = (presence or {}).get('actions') or {}
    performances = {}
    for actor in cast.actors:
        record = records_by_id[actor['actor_id']]
        found = objects.get(record['object'])
        if found is None:
            missing.append({'code': 'ACTOR_OBJECT_MISSING', 'actor_id': actor['actor_id'], 'ref': record['object'],
                            'blocking': True, 'detail': 'the registered object is not in the open file'})
        elif not found.get('in_scene', True):
            missing.append({'code': 'ACTOR_NOT_IN_SOURCE_SCENE', 'actor_id': actor['actor_id'], 'ref': record['object'],
                            'blocking': True, 'detail': 'the object exists but is not linked to scene ' + info['scene']})
        body = _body_rows(record)
        performances[actor['actor_id']] = {
            'object': record['object'], 'type': record.get('type'), 'meshes': list(record.get('meshes') or []),
            'body_actions': [{'action': r['action'], 'slot': r.get('slot'), 'frame_range': r.get('frame_range'),
                              'root_motion': _root_motion(r)} for r in body[:MAX_LISTED_ACTIONS]],
            'body_actions_total': len(body), 'active_action': record.get('active_action'),
            'facial': [{'ref': r.get('ref'), 'state': r.get('state')} for r in _states(record, 'facial')],
            'secondary': [{'ref': r.get('ref'), 'state': r.get('state')} for r in _states(record, 'secondary')],
            'dimensions_m': record.get('dimensions_m'), 'interaction_capabilities': record.get('interaction_capabilities'),
            'limitations': record.get('limitations'), 'measured': _measure(record, presence)}
        if not body:
            risks.append({'code': 'ACTOR_WITHOUT_BODY_ACTIONS', 'actor_id': actor['actor_id'],
                          'detail': 'no registered body Action: the actor can only be staged statically'})

    def require_action(aid, action, context):
        if any(r['action'] == action for r in _body_rows(records_by_id.get(aid))):
            return
        exists = actions_found.get(action) is not None
        missing.append({'code': 'ACTION_NOT_AVAILABLE', 'actor_id': aid, 'ref': action, 'blocking': True,
                        'detail': context + (': the Action exists in the file but is not registered for this actor; '
                                             're-register the actor' if exists else
                                             ': no Action with this name exists; M10 never generates body animation')})

    for aid, request in requested:
        if aid is None:
            continue
        record = records_by_id[aid]
        for action in request['body_actions']:
            require_action(aid, action, 'required performance')
        for key, code, owner in (('facial', 'FACIAL_PERFORMANCE_NOT_ACCEPTED', 'M7'),
                                 ('secondary', 'SECONDARY_NOT_ACCEPTED', 'M8')):
            wanted = request[key]
            if not wanted:
                continue
            accepted = [r for r in _states(record, key) if r.get('state') == 'ACCEPTED']
            refs = wanted if isinstance(wanted, list) else [None]
            for ref in refs:
                if not any(ref is None or r.get('ref') == ref for r in accepted):
                    missing.append({'code': code, 'actor_id': aid, 'ref': ref, 'blocking': True,
                                    'detail': 'requested ' + key + ' needs an ACCEPTED ' + owner
                                              + ' generation registered for this actor'})
    for event in events:
        performers = event['actors']
        for action in event['action_refs']:
            if not performers:
                missing.append({'code': 'ACTION_WITHOUT_ACTOR', 'ref': action, 'blocking': True,
                                'detail': event['event_id'] + ' names an Action but no performer'})
            elif not any(any(r['action'] == action for r in _body_rows(records_by_id.get(a))) for a in performers):
                require_action(performers[0], action, event['event_id'])
    for item in requirements:
        for action in item['action_refs']:
            if item['actors'] and not any(any(r['action'] == action for r in _body_rows(records_by_id.get(a)))
                                          for a in item['actors']):
                require_action(item['actors'][0], action, 'interaction requirement')
    for row in events + requirements:
        target = row.get('target') or {}
        if target.get('type') == 'OBJECT':
            found = objects.get(target['object'])
            if found is None or not found.get('in_scene', True):
                missing.append({'code': 'TARGET_OBJECT_MISSING', 'ref': target['object'], 'blocking': True,
                                'detail': 'a target is neither a registered actor nor an object of the source scene'})
    rate = model.fps_value(fps, fps_base)
    timeline = None
    if duration is not None:
        timeline = timeline_for({'fps': fps, 'fps_base': fps_base, 'duration_s': duration, 'frame_start': frame_start},
                                {'fps': settings['fps'], 'fps_base': settings['fps_base']})
    unscheduled = [e['event_id'] for e in events if e['time_s'] is None]
    if not events:
        risks.append({'code': 'NO_STORY_EVENTS', 'detail': 'no events: plan_beats can only propose establishing/ending'})
        questions.append('What happens in the sequence (events)?')
    elif unscheduled:
        risks.append({'code': 'EVENTS_UNSCHEDULED', 'events': unscheduled,
                      'detail': 'plan_beats proposes their times (PROPOSED_CREATIVE_DECISION)'})
    for event in events:
        if event['kind'] in MOVING_KINDS:
            for aid in event['actors']:
                clips = [c for c in (_locomotion(r, rate)[0] for r in _body_rows(records_by_id.get(aid))) if c]
                if not clips:
                    risks.append({'code': 'NO_LOCOMOTION_ACTION', 'actor_id': aid, 'event_id': event['event_id'],
                                  'detail': 'no registered Action with measured root motion: plan_actor_paths will '
                                            'propose static staging with a cut'})
    if timeline is not None:
        for actor in cast.actors:
            spans = [r['frame_range'][1] - r['frame_range'][0] + 1 for r in _body_rows(records_by_id[actor['actor_id']])
                     if isinstance(r.get('frame_range'), (list, tuple)) and len(r['frame_range']) == 2
                     and all(_num(v) for v in r['frame_range'])]
            if spans and max(spans) < timeline['frames']:
                risks.append({'code': 'ACTIONS_SHORTER_THAN_TIMELINE', 'actor_id': actor['actor_id'],
                              'detail': 'longest registered Action spans ' + str(_r(max(spans), 3)) + ' frames, the '
                                        'timeline ' + str(timeline['frames']) + ': hold, repeat or another clip is needed'})
    lighting = dict(intent.get('lighting_intentions') or {})
    if 'lighting_intentions' in given:
        prov['lighting_intentions'] = USER
    elif (intent.get('atmosphere') or {}).get('mood_preset'):
        lighting = {'preset': intent['atmosphere']['mood_preset']}
        prov['lighting_intentions'] = PROPOSED
    else:
        prov['lighting_intentions'] = DEFAULT
    for key in ('title', 'description', 'atmosphere', 'camera_intentions', 'interaction_requirements',
                'timing_constraints', 'events', 'actors'):
        prov.setdefault(key, USER if key in given else DEFAULT)
    prov['available_performances'] = DEFAULT
    ready = not any(m.get('blocking', True) for m in missing)
    return {'schema': BRIEF_SCHEMA, 'title': intent.get('title'), 'description': intent.get('description'),
            'duration_s': duration, 'fps': fps, 'fps_base': fps_base, 'frame_start': frame_start,
            'actors': cast.actors, 'events': events, 'atmosphere': intent.get('atmosphere') or {},
            'camera_intentions': cameras, 'lighting_intentions': lighting, 'interaction_requirements': requirements,
            'timing_constraints': intent['timing_constraints'], 'available_performances': performances,
            'missing_prerequisites': missing, 'risks': risks, 'open_questions': list(dict.fromkeys(questions)),
            'ready': ready, 'timeline': timeline,
            'source': {'scene': info['scene'], 'fingerprint': info.get('fingerprint'), 'fps': settings['fps'],
                       'fps_base': settings['fps_base'], 'frame_start': settings['frame_start'],
                       'frame_end': settings['frame_end'], 'resolution': settings.get('resolution'),
                       'engine': settings.get('engine')},
            'actor_registry_key': registry_key, 'provenance': dict(sorted(prov.items())),
            'provenance_groups': _groups(prov)}


# ---------------------------------------------------------------------------------------------------------- beats

def _beat_override(row, field, brief, patch=False):
    data = model.closed(_dict(row, field), BEAT_OVERRIDE_KEYS)
    out, ids, events = {}, set(actor_ids(brief)), {e['event_id'] for e in brief.get('events') or []}
    if 'kind' in data:
        out['kind'] = model.choice(data['kind'], field + '.kind', model.BEAT_KINDS)
    elif not patch:
        model.fail('CINEMATIC_BEAT_KIND_REQUIRED', field + ' needs kind')
    if 'beat_id' in data:
        if patch:
            model.fail('CINEMATIC_BEAT_RENAME_UNSUPPORTED', field + ': pass a full list to rename beats')
        out['beat_id'] = model.name(data['beat_id'], field + '.beat_id')
    for key, low in (('start_s', 0), ('end_s', 0), ('duration_s', 1e-6), ('gap_before_s', 0)):
        if key in data:
            out[key] = model.number(data[key], field + '.' + key, low, model.MAX_DURATION_S)
    if 'weight' in data:
        out['weight'] = model.number(data['weight'], field + '.weight', 0.001, 1000)
    if 'purpose' in data:
        out['purpose'] = model.text(data['purpose'], field + '.purpose', 500)
    if 'camera_intention' in data:
        out['camera_intention'] = None if data['camera_intention'] is None else model.text(
            data['camera_intention'], field + '.camera_intention', 500)
    if 'transition' in data:
        out['transition'] = model.choice(data['transition'], field + '.transition', model.TRANSITIONS)
    if 'actors' in data:
        out['actors'] = _refs(data['actors'], field + '.actors', model.MAX_ACTORS)
        unknown = [a for a in out['actors'] if a not in ids]
        if unknown:
            model.fail('CINEMATIC_BEAT_ACTOR_UNKNOWN', field + ' names actors outside the brief: ' + ', '.join(unknown))
    if 'required_actions' in data:
        out['required_actions'] = _refs(data['required_actions'], field + '.required_actions')
    if 'staging_constraints' in data:
        out['staging_constraints'] = [model.text(s, field + '.staging_constraints', 300) if isinstance(s, str)
                                      else _dict(s, field + '.staging_constraints')
                                      for s in _items(data['staging_constraints'], field + '.staging_constraints', 16)]
    if 'dependencies' in data:
        out['dependencies'] = [model.name(d, field + '.dependencies') for d in _items(data['dependencies'], field, 16)]
    if 'events' in data:
        out['events'] = [model.name(e, field + '.events') for e in _items(data['events'], field + '.events', MAX_EVENTS)]
        unknown = [e for e in out['events'] if e not in events]
        if unknown:
            model.fail('CINEMATIC_EVENT_UNKNOWN', field + ' references ' + ', '.join(unknown))
    return out, {k: USER for k in out}


def _default_beat_rows(brief, add_establishing, add_ending):
    constraints = {c['event_id']: c for c in brief.get('timing_constraints') or [] if c.get('event_id')}
    rows = []
    for event in brief.get('events') or []:
        kind = event['kind'] if event['kind'] in model.BEAT_KINDS else EVENT_TO_BEAT[event['kind']]
        row = {'kind': kind, 'events': [event['event_id']]}
        prov = {'kind': USER if event['kind'] in model.BEAT_KINDS else PROPOSED, 'events': USER}
        anchor = event.get('time_s')
        if anchor is None:
            anchor = constraints.get(event['event_id'], {}).get('at_s')
        if anchor is not None:
            row['start_s'] = anchor
            prov['start_s'] = USER
        rows.append((row, prov))
    if add_establishing and (not rows or rows[0][0]['kind'] != 'ESTABLISHING') and not (
            rows and rows[0][0].get('start_s') == 0):
        rows.insert(0, ({'kind': 'ESTABLISHING', 'events': []}, {'kind': PROPOSED, 'events': DEFAULT}))
    if add_ending and (not rows or rows[-1][0]['kind'] != 'ENDING'):
        rows.append(({'kind': 'ENDING', 'events': []}, {'kind': PROPOSED, 'events': DEFAULT}))
    if rows and rows[0][0].get('start_s', 0) > 0:
        # The first beat always starts the timeline: without an establishing beat the first event happens inside
        # it (its time stays in event_schedule) instead of leaving an undeclared gap.
        rows[0][0].pop('start_s')
        rows[0][1]['start_s'] = DEFAULT
    if not rows:
        model.fail('CINEMATIC_NO_BEATS', 'the brief has no events; pass options.beats or allow the establishing/ending proposal')
    return rows


def _assign_beat_ids(rows):
    used = set()
    for index, (row, prov) in enumerate(rows):
        if 'beat_id' not in row:
            row['beat_id'] = 'B' + format(index + 1, '02d') + '_' + row['kind']
            prov.setdefault('beat_id', DEFAULT)
        if row['beat_id'] in used:
            model.fail('CINEMATIC_BEAT_ID_DUPLICATE', row['beat_id'])
        used.add(row['beat_id'])


def _solve_bounds(rows, duration):
    """Boundaries B0..Bn: beat i spans [B_i + gap_i, B_(i+1)]. User starts/ends/durations fix boundaries; the
    remaining time of each run between fixed boundaries is split by weight (equal weights unless overridden).
    Nothing overlaps; a gap exists only where gap_before_s declares it."""
    n = len(rows)
    gaps = [row.get('gap_before_s', 0.0) for row, _ in rows]
    durations = [row.get('duration_s') for row, _ in rows]
    weights = [row.get('weight', 1.0) for row, _ in rows]
    bounds, origin = [None] * (n + 1), [None] * (n + 1)

    def fix(k, value, source, why):
        if value < -1e-9 or value > duration + 1e-9:
            model.fail('CINEMATIC_BEAT_OUTSIDE_TIMELINE', why + ' places a boundary at ' + str(_r(value)) + ' s, outside 0..'
                       + str(duration) + ' s')
        if bounds[k] is not None:
            if abs(bounds[k] - value) > 1e-6:
                model.fail('CINEMATIC_BEAT_TIMES_INCONSISTENT', why + ' conflicts with another time ('
                           + str(_r(bounds[k])) + ' s vs ' + str(_r(value)) + ' s)')
            return False
        bounds[k], origin[k] = min(max(value, 0.0), duration), source
        return True

    fix(0, 0.0, DEFAULT, 'timeline start')
    fix(n, duration, DEFAULT, 'timeline end')
    for i, (row, prov) in enumerate(rows):
        if row.get('start_s') is not None:
            fix(i, row['start_s'] - gaps[i], prov.get('start_s', USER), row['beat_id'] + ' start')
        if row.get('end_s') is not None:
            fix(i + 1, row['end_s'], prov.get('end_s', USER), row['beat_id'] + ' end')
    changed = True
    while changed:
        changed = False
        for i in range(n):
            if durations[i] is None:
                continue
            if bounds[i] is not None and bounds[i + 1] is None:
                changed |= fix(i + 1, bounds[i] + gaps[i] + durations[i], USER, rows[i][0]['beat_id'] + ' duration')
            elif bounds[i + 1] is not None and bounds[i] is None:
                changed |= fix(i, bounds[i + 1] - durations[i] - gaps[i], USER, rows[i][0]['beat_id'] + ' duration')
            elif bounds[i] is not None and abs(bounds[i + 1] - bounds[i] - gaps[i] - durations[i]) > 1e-6:
                model.fail('CINEMATIC_BEAT_TIMES_INCONSISTENT', rows[i][0]['beat_id'] + ' duration does not fit its start/end')
    k = 0
    while k < n:
        j = k + 1
        while bounds[j] is None:
            j += 1
        if j > k + 1:
            span = range(k, j)
            flexible = [i for i in span if durations[i] is None]
            available = bounds[j] - bounds[k] - sum(gaps[i] for i in span) - sum(durations[i] for i in span if durations[i] is not None)
            if not flexible or available <= 1e-9:
                model.fail('CINEMATIC_BEAT_TIMES_INCONSISTENT', 'beats ' + rows[k][0]['beat_id'] + '..' + rows[j - 1][0]['beat_id']
                           + ' do not fit between ' + str(_r(bounds[k])) + ' s and ' + str(_r(bounds[j])) + ' s')
            total = sum(weights[i] for i in flexible)
            cursor = bounds[k]
            for i in range(k, j - 1):
                length = durations[i] if durations[i] is not None else available * weights[i] / total
                cursor += gaps[i] + length
                bounds[i + 1], origin[i + 1] = cursor, PROPOSED
        k = j
    for i in range(n):
        if bounds[i + 1] - (bounds[i] + gaps[i]) <= 1e-9:
            model.fail('CINEMATIC_BEAT_TIMES_INCONSISTENT', rows[i][0]['beat_id'] + ' has no duration; check the order '
                       'of the event times')
    return bounds, origin, gaps


def _staging_for(events):
    rows = []
    for event in events:
        target = event.get('target') or {}
        for aid in event.get('actors') or []:
            if event['kind'] in MOVING_KINDS and target:
                rows.append({'type': MOVING_KINDS[event['kind']], 'actor_id': aid, 'target': target})
            elif event['kind'] in FACING_KINDS and target:
                rows.append({'type': 'FACE_TARGET', 'actor_id': aid, 'target': target})
            elif event['kind'] in REACH_KINDS and target:
                rows.append({'type': 'CONTACT' if event['kind'] in CONTACT_KINDS else 'REACH_TARGET',
                             'actor_id': aid, 'target': target})
    return rows


def plan_beats(brief, options=None):
    """Narrative beats with explicit seconds and frames. Times the user gave are kept; the rest of the timeline is
    split by weight (equal by default, a PROPOSED_CREATIVE_DECISION). No overlaps, no undeclared gaps."""
    brief = require_brief(brief)
    opts = model.closed(options, {'beats', 'add_establishing', 'add_ending'})
    timeline = brief_timeline(brief)
    overrides = opts.get('beats')
    if isinstance(overrides, list):
        rows = [_beat_override(r, 'options.beats[' + str(i) + ']', brief) for i, r in
                enumerate(_items(overrides, 'options.beats', MAX_BEATS))]
        source = 'USER_OVERRIDES'
    else:
        rows = _default_beat_rows(brief, _flag(opts.get('add_establishing', True), 'add_establishing'),
                                  _flag(opts.get('add_ending', True), 'add_ending'))
        source = 'DERIVED_FROM_BRIEF'
        if overrides is not None:
            _assign_beat_ids(rows)
            index = {row['beat_id']: (row, prov) for row, prov in rows}
            for beat_id, patch in _dict(overrides, 'options.beats').items():
                if beat_id not in index:
                    model.fail('CINEMATIC_BEAT_UNKNOWN', str(beat_id) + ' is not a planned beat: ' + ', '.join(index))
                values, prov = _beat_override(patch, 'options.beats.' + beat_id, brief, patch=True)
                index[beat_id][0].update(values)
                index[beat_id][1].update(prov)
            source = 'DERIVED_WITH_PATCHES'
    if len(rows) > MAX_BEATS:
        model.fail('CINEMATIC_CAPACITY', 'at most ' + str(MAX_BEATS) + ' beats')
    _assign_beat_ids(rows)
    return _finalize_beats(rows, brief, timeline, source)


def _finalize_beats(rows, brief, timeline, source):
    bounds, origin, gaps = _solve_bounds(rows, timeline['duration_s'])
    events = {e['event_id']: e for e in brief.get('events') or []}
    requirements = {}
    for item in brief.get('interaction_requirements') or []:
        if item.get('event_id'):
            requirements.setdefault(item['event_id'], []).append(item)
    cameras = brief.get('camera_intentions') or []
    all_actors = actor_ids(brief)
    beats, conversions, provenance, gap_rows = [], [], {}, []
    n = len(rows)
    for i, (row, prov) in enumerate(rows):
        beat_id = row['beat_id']
        start, end = bounds[i] + gaps[i], bounds[i + 1]
        frame_start, record_start = _frame(start, timeline)
        end_frame, record_end = _frame(end, timeline)
        if end_frame - 1 < frame_start:
            model.fail('CINEMATIC_BEAT_TOO_SHORT', beat_id + ' rounds to less than one frame at ' + str(timeline['fps']) + ' fps')
        conversions.append(dict(record_start, purpose=beat_id + ' start'))
        if i == n - 1 or gaps[i + 1] > 0:
            conversions.append(dict(record_end, purpose=beat_id + ' end'))
        if gaps[i] > 0:
            gap_start = _frame(bounds[i], timeline)[0]
            gap_rows.append({'before_beat': beat_id, 'start_s': bounds[i], 'end_s': start, 'frame_start': gap_start,
                             'frame_end': frame_start - 1, 'frames': frame_start - gap_start, 'source': USER})
        evs = [events[e] for e in row.get('events') or [] if e in events]
        field = {}
        field['kind'] = prov.get('kind', USER)
        field['start_s'] = origin[i] if gaps[i] == 0 else USER
        field['end_s'] = origin[i + 1]
        # A user duration stays a user requirement even when its absolute placement follows a proposed split.
        if row.get('duration_s') is not None:
            field['duration_s'] = USER
        elif PROPOSED in (field['start_s'], field['end_s']):
            field['duration_s'] = PROPOSED
        else:
            field['duration_s'] = USER if USER in (field['start_s'], field['end_s']) else DEFAULT
        if 'actors' in row:
            actors, field['actors'] = row['actors'], USER
        else:
            actors = []
            for event in evs:
                actors += event.get('actors') or []
                target = event.get('target') or {}
                if target.get('type') == 'ACTOR':
                    actors.append(target['actor_id'])
            actors = list(dict.fromkeys(actors))
            field['actors'] = USER if actors else DEFAULT
            if not actors and row['kind'] in ('ESTABLISHING', 'ENDING'):
                actors, field['actors'] = list(all_actors), PROPOSED
        if 'required_actions' in row:
            required, field['required_actions'] = row['required_actions'], USER
        else:
            required = []
            for event in evs:
                required += event.get('action_refs') or []
                for item in requirements.get(event['event_id'], []):
                    required += item.get('action_refs') or []
            required = list(dict.fromkeys(required))
            field['required_actions'] = USER if required else DEFAULT
        if 'purpose' in row:
            purpose, field['purpose'] = row['purpose'], USER
        elif evs and evs[0].get('description'):
            purpose, field['purpose'] = evs[0]['description'], USER
        else:
            purpose, field['purpose'] = BEAT_PURPOSE[row['kind']], PROPOSED
        if 'camera_intention' in row:
            camera, field['camera_intention'] = row['camera_intention'], USER
        else:
            match = [c for c in cameras if c.get('event_id') in [e['event_id'] for e in evs]
                     or (c.get('event_id') is None and c.get('beat_kind') == row['kind'])]
            camera = match[0]['intent'] if match else None
            field['camera_intention'] = USER if match else DEFAULT
        if 'staging_constraints' in row:
            staging, field['staging_constraints'] = row['staging_constraints'], USER
        else:
            staging, field['staging_constraints'] = _staging_for(evs), DEFAULT
        if 'transition' in row:
            transition, field['transition'] = row['transition'], USER
        else:
            transition, field['transition'] = ('HOLD' if i == n - 1 else 'CUT'), PROPOSED
        if 'dependencies' in row:
            dependencies, field['dependencies'] = row['dependencies'], USER
        else:
            dependencies, field['dependencies'] = ([rows[i - 1][0]['beat_id']] if i else []), DEFAULT
        for key, value in field.items():
            provenance[beat_id + '.' + key] = value
        beats.append({'beat_id': beat_id, 'kind': row['kind'], 'start_s': start, 'end_s': end, 'duration_s': end - start,
                      'frame_start': frame_start, 'frame_end': end_frame - 1, 'duration_frames': end_frame - frame_start,
                      'gap_before_s': gaps[i], 'purpose': purpose, 'actors': actors, 'required_actions': required,
                      'camera_intention': camera, 'staging_constraints': staging, 'transition': transition,
                      'dependencies': dependencies, 'events': [e['event_id'] for e in evs], 'provenance': field})
    validate_beats({'beats': beats}, timeline, all_actors)
    schedule, warnings = [], []
    owner = {e: b['beat_id'] for b in beats for e in b['events']}
    by_id = {b['beat_id']: b for b in beats}
    for event in brief.get('events') or []:
        beat_id = owner.get(event['event_id'])
        if beat_id is None:
            warnings.append({'code': 'EVENT_NOT_IN_ANY_BEAT', 'event_id': event['event_id']})
            continue
        beat = by_id[beat_id]
        if event.get('time_s') is not None:
            time, origin_time = event['time_s'], USER
            if not beat['start_s'] - 1e-9 <= time < beat['end_s']:
                warnings.append({'code': 'EVENT_OUTSIDE_ITS_BEAT', 'event_id': event['event_id'], 'beat_id': beat_id})
        else:
            time, origin_time = beat['start_s'], beat['provenance']['start_s']
        frame, record = _frame(time, timeline)
        schedule.append({'event_id': event['event_id'], 'beat_id': beat_id, 'time_s': time, 'frame': frame,
                         'time_source': origin_time, 'rounding_error_frames': record['rounding_error_frames']})
    violations = []
    times = {s['event_id']: s['time_s'] for s in schedule}
    for constraint in brief.get('timing_constraints') or []:
        if 'event_id' in constraint:
            time = times.get(constraint['event_id'])
            if time is None:
                continue
            for key, ok in (('at_s', lambda t, v: abs(t - v) <= 1e-6), ('not_before_s', lambda t, v: t >= v - 1e-9),
                            ('not_after_s', lambda t, v: t <= v + 1e-9)):
                if key in constraint and not ok(time, constraint[key]):
                    violations.append({'event_id': constraint['event_id'], 'constraint': key, 'limit_s': constraint[key],
                                       'planned_s': time})
        else:
            for beat in beats:
                if beat['kind'] != constraint['beat_kind']:
                    continue
                for key, ok in (('min_duration_s', lambda d, v: d >= v - 1e-9), ('max_duration_s', lambda d, v: d <= v + 1e-9)):
                    if key in constraint and not ok(beat['duration_s'], constraint[key]):
                        violations.append({'beat_id': beat['beat_id'], 'constraint': key, 'limit_s': constraint[key],
                                           'planned_s': beat['duration_s']})
    for beat in beats:
        if beat['duration_frames'] < 2:
            warnings.append({'code': 'BEAT_SINGLE_FRAME', 'beat_id': beat['beat_id']})
    return {'schema': BEATS_SCHEMA, 'status': 'warning' if violations or warnings else 'success', 'source': source,
            'timeline': {k: timeline[k] for k in ('fps', 'fps_base', 'frame_start', 'frame_end', 'duration_s', 'frames')},
            'beats': beats, 'gaps': gap_rows, 'event_schedule': schedule, 'constraint_violations': violations,
            'timing_rule': {'rule': 'time not fixed by the user is split between unscheduled beats by weight (1.0 each '
                                    'unless overridden)', 'provenance': PROPOSED},
            'conversions': conversions, 'warnings': warnings, 'provenance_groups': _groups(provenance)}


# ---------------------------------------------------------------------------------------------------------- shots

def _sensor_height(resolution):
    """Vertical sensor size Blender uses with sensor_fit AUTO: the sensor width covers the larger image side."""
    width, height = (resolution or [1920, 1080])[:2]
    if width >= height:
        return CAMERA_DEFAULTS['sensor_width_mm'] * height / width
    return CAMERA_DEFAULTS['sensor_width_mm']


def _shot_override(row, field, brief, patch=False):
    data = model.closed(_dict(row, field), SHOT_OVERRIDE_KEYS)
    out, ids = {}, set(actor_ids(brief))
    if 'shot_id' in data:
        if patch:
            model.fail('CINEMATIC_SHOT_RENAME_UNSUPPORTED', field + ': pass a full list to rename shots')
        out['shot_id'] = model.name(data['shot_id'], field + '.shot_id')
    for key, options in (('type', model.SHOT_TYPES), ('movement', model.CAMERA_MOVES), ('easing', model.EASINGS),
                         ('transition', model.TRANSITIONS)):
        if key in data:
            out[key] = model.choice(data[key], field + '.' + key, options)
    for key in ('beat_id', 'camera_id'):
        if key in data:
            out[key] = model.name(data[key], field + '.' + key)
    for key in ('start_s', 'end_s'):
        if key in data:
            out[key] = model.number(data[key], field + '.' + key, 0, model.MAX_DURATION_S)
    for key in ('frame_start', 'frame_end'):
        if key in data:
            out[key] = model.number(data[key], field + '.' + key, -1_048_574, 1_048_574, integer=True)
    if 'lens_mm' in data:
        out['lens_mm'] = model.number(data['lens_mm'], field + '.lens_mm', 1, 5000)
    if 'camera_height_m' in data:
        out['camera_height_m'] = model.number(data['camera_height_m'], field + '.camera_height_m', -1000, 1000)
    if 'camera_angle_deg' in data:
        out['camera_angle_deg'] = model.number(data['camera_angle_deg'], field + '.camera_angle_deg', -90, 90)
    if 'subjects' in data:
        out['subjects'] = _refs(data['subjects'], field + '.subjects', model.MAX_ACTORS)
        unknown = [s for s in out['subjects'] if s not in ids]
        if unknown:
            model.fail('CINEMATIC_SHOT_SUBJECT_UNKNOWN', field + ' frames actors outside the brief: ' + ', '.join(unknown))
    if 'focus_target' in data:
        focus = data['focus_target']
        if focus is None:
            out['focus_target'] = None
        else:
            focus = model.closed(_dict(focus, field + '.focus_target'), {'actor_id', 'region', 'point'})
            if 'actor_id' in focus and focus['actor_id'] not in ids:
                model.fail('CINEMATIC_SHOT_SUBJECT_UNKNOWN', field + '.focus_target.actor_id is not a brief actor')
            item = {}
            if 'actor_id' in focus:
                item['actor_id'] = focus['actor_id']
            if 'region' in focus:
                item['region'] = model.choice(focus['region'], field + '.focus_target.region', ('HEAD', 'BODY'))
            if 'point' in focus:
                item['point'] = model.vector(focus['point'], field + '.focus_target.point')
            out['focus_target'] = item
    if 'framing' in data:
        framing = model.closed(_dict(data['framing'], field + '.framing'), FRAMING_KEYS)
        item = {}
        if 'subject_region' in framing:
            item['subject_region'] = model.choice(framing['subject_region'], field + '.framing.subject_region', SUBJECT_REGIONS)
        for key in ('screen_height_fraction', 'headroom_fraction'):
            if key in framing:
                item[key] = model.number(framing[key], field + '.framing.' + key, 0, 1)
        if 'horizontal_position' in framing:
            item['horizontal_position'] = model.choice(framing['horizontal_position'], field + '.framing.horizontal_position',
                                                       HORIZONTAL_POSITIONS)
        if 'eye_line' in framing:
            item['eye_line'] = model.choice(framing['eye_line'], field + '.framing.eye_line', ('UPPER_THIRD', 'CENTER', 'NONE'))
        out['framing'] = item
    if 'notes' in data:
        out['notes'] = model.text(data['notes'], field + '.notes', 500)
    return out


def _beat_spans(beats, timeline):
    """Frames a beat owns for shot coverage: from its start to the frame before the next beat (a declared gap is
    held by the previous beat's shot); the first span starts at the timeline start."""
    spans = []
    for i, beat in enumerate(beats):
        start = timeline['frame_start'] if i == 0 else beat['frame_start']
        end = beats[i + 1]['frame_start'] - 1 if i + 1 < len(beats) else timeline['frame_end']
        spans.append((beat['beat_id'], start, end))
    return spans


def plan_shots(brief, options=None):
    """Shots that tile the timeline exactly (contiguous, no overlap). Defaults: one shot per beat with a type
    proposed from the beat kind; framing, lens, height and angle are PROPOSALS, never forced on every type."""
    brief = require_brief(brief)
    opts = model.closed(options, {'beats', 'shots'})
    timeline = brief_timeline(brief)
    ids = actor_ids(brief)
    if opts.get('beats') is None:
        beats, beats_source = plan_beats(brief), 'COMPUTED_DEFAULT'
    else:
        beats = _dict(opts['beats'], 'options.beats')
        if beats.get('schema') != BEATS_SCHEMA:
            model.fail('CINEMATIC_BEATS_INVALID', 'options.beats must be the result of cinematic_plan_beats')
        validate_beats(beats, timeline, ids)
        beats_source = 'OPTIONS'
    beat_rows = beats['beats']
    by_beat = {b['beat_id']: b for b in beat_rows}
    spans = _beat_spans(beat_rows, timeline)
    span_of = {beat_id: (start, end) for beat_id, start, end in spans}
    overrides = opts.get('shots')
    warnings = []
    if isinstance(overrides, list):
        rows = [_shot_override(r, 'options.shots[' + str(i) + ']', brief) for i, r in
                enumerate(_items(overrides, 'options.shots', model.MAX_SHOTS))]
        provs = [{k: USER for k in row} for row in rows]
        source = 'USER_OVERRIDES'
        _resolve_shot_frames(rows, provs, span_of, timeline)
    else:
        rows, provs = [], []
        for beat_id, start, end in spans:
            rows.append({'beat_id': beat_id, 'frame_start': start, 'frame_end': end})
            provs.append({'beat_id': DEFAULT, 'frame_start': DEFAULT, 'frame_end': DEFAULT})
            if by_beat[beat_id]['frame_end'] != end:
                warnings.append({'code': 'BEAT_GAP_HELD_BY_SHOT', 'beat_id': beat_id,
                                 'detail': 'frames after the beat until the next one stay on this shot'})
        source = 'DERIVED_FROM_BEATS'
        for i, row in enumerate(rows):
            row['shot_id'] = 'S' + format(i + 1, '02d')
            provs[i]['shot_id'] = DEFAULT
        if overrides is not None:
            index = {row['shot_id']: i for i, row in enumerate(rows)}
            for shot_id, patch in _dict(overrides, 'options.shots').items():
                if shot_id not in index:
                    model.fail('CINEMATIC_SHOT_UNKNOWN', str(shot_id) + ' is not a planned shot: ' + ', '.join(index))
                values = _shot_override(patch, 'options.shots.' + shot_id, brief, patch=True)
                for key in ('frame_start', 'frame_end', 'start_s', 'end_s', 'beat_id'):
                    if key in values:
                        model.fail('CINEMATIC_SHOT_TIMES_PATCH_UNSUPPORTED', 'retime shots with a full options.shots list')
                rows[index[shot_id]].update(values)
                provs[index[shot_id]].update({k: USER for k in values})
            source = 'DERIVED_WITH_PATCHES'
    _check_coverage(rows, timeline)
    used = set()
    for i, row in enumerate(rows):
        if 'shot_id' not in row:
            row['shot_id'] = 'S' + format(i + 1, '02d')
            provs[i]['shot_id'] = DEFAULT
        if row['shot_id'] in used:
            model.fail('CINEMATIC_SHOT_ID_DUPLICATE', row['shot_id'])
        used.add(row['shot_id'])
    resolution = (brief.get('source') or {}).get('resolution') or [1920, 1080, 100]
    sensor_v = _sensor_height(resolution)
    performances = brief.get('available_performances') or {}
    shots, provenance = [], {}
    beat_last = {}
    for i, row in enumerate(rows):
        beat_last[row['beat_id']] = i
    for i, (row, prov) in enumerate(zip(rows, provs)):
        beat = by_beat[row['beat_id']]
        kind = row.get('type') or BEAT_TO_SHOT[beat['kind']]
        prov.setdefault('type', PROPOSED)
        region, screen, headroom, horizontal, lens, height_mode, height_fraction, pitch, movement, easing = SHOT_DEFAULTS[kind]
        framing = {'subject_region': region, 'screen_height_fraction': screen, 'headroom_fraction': headroom,
                   'horizontal_position': horizontal, 'eye_line': 'UPPER_THIRD' if region in ('HEAD', 'UPPER_BODY') else 'NONE'}
        framing.update(row.get('framing') or {})
        framing['threshold_source'] = 'PROPOSAL' if 'framing' not in row else 'USER_REQUIREMENT'
        prov.setdefault('framing', PROPOSED)
        subjects = row.get('subjects', beat['actors'])
        prov.setdefault('subjects', beat['provenance'].get('actors', DEFAULT))
        primary = subjects[0] if subjects else None
        measured = (performances.get(primary) or {}).get('measured') or {} if primary else {}
        subject_height = measured.get('height_m') if _num(measured.get('height_m')) and measured.get('height_m') > 0 else None
        lens_mm = row.get('lens_mm', lens)
        prov.setdefault('lens_mm', PROPOSED)
        if 'camera_height_m' in row:
            camera_height = {'mode': 'EXPLICIT', 'height_m': row['camera_height_m']}
        else:
            camera_height = {'mode': height_mode, 'fraction_of_subject_height': height_fraction,
                             'height_m': _r(height_fraction * subject_height) if subject_height else 'UNKNOWN',
                             'subject_height_source': 'SCENE_MEASURED' if subject_height else 'UNKNOWN'}
            prov['camera_height'] = PROPOSED
        distance = 'UNKNOWN'
        if subject_height and framing['screen_height_fraction'] > 0:
            region_height = REGION_FRACTION[framing['subject_region']] * subject_height
            distance = _r(region_height * lens_mm / (framing['screen_height_fraction'] * sensor_v), 4)
        if 'focus_target' in row:
            focus = row['focus_target']
        elif primary:
            focus = {'actor_id': primary, 'region': 'HEAD' if framing['subject_region'] != 'FULL_BODY' else 'BODY'}
            prov['focus_target'] = PROPOSED
        else:
            focus = None
            prov['focus_target'] = DEFAULT
        if 'transition' in row:
            transition = row['transition']
        elif beat_last[row['beat_id']] == i:
            transition = beat['transition']
            prov['transition'] = beat['provenance'].get('transition', PROPOSED)
        else:
            transition = 'CUT'
            prov['transition'] = PROPOSED
        shot = {'shot_id': row['shot_id'], 'camera_id': row.get('camera_id', 'CAM_' + row['shot_id']), 'type': kind,
                'beat_id': row['beat_id'], 'frame_start': row['frame_start'], 'frame_end': row['frame_end'],
                'duration_frames': row['frame_end'] - row['frame_start'] + 1,
                'start_s': model.frame_to_seconds(row['frame_start'], timeline['fps'], timeline['fps_base'], timeline['frame_start']),
                'end_s': model.frame_to_seconds(row['frame_end'] + 1, timeline['fps'], timeline['fps_base'], timeline['frame_start']),
                'subjects': list(subjects), 'framing': framing, 'lens_mm': lens_mm,
                'camera_height': camera_height, 'camera_angle_deg': row.get('camera_angle_deg', pitch),
                'movement': row.get('movement', movement),
                'easing': row.get('easing', easing), 'focus_target': focus, 'transition': transition,
                'suggested_distance_m': distance}
        prov.setdefault('camera_id', DEFAULT)
        prov.setdefault('camera_angle_deg', PROPOSED)
        prov.setdefault('movement', PROPOSED)
        prov.setdefault('easing', PROPOSED)
        if 'notes' in row:
            shot['notes'] = row['notes']
        shot['provenance'] = dict(sorted(prov.items()))
        for key, value in prov.items():
            provenance[row['shot_id'] + '.' + key] = value
        shots.append(shot)
    validate_shots(shots, timeline, ids, set(by_beat))
    return {'schema': SHOTS_SCHEMA, 'status': 'warning' if warnings else 'success', 'source': source,
            'beats_source': beats_source,
            'timeline': {k: timeline[k] for k in ('fps', 'fps_base', 'frame_start', 'frame_end', 'duration_s', 'frames')},
            'shots': shots, 'coverage': {'frame_start': shots[0]['frame_start'], 'frame_end': shots[-1]['frame_end'],
                                         'frames': sum(s['duration_frames'] for s in shots), 'contiguous': True},
            'threshold_source': 'PROPOSAL',
            'distance_model': {'formula': 'distance = region_height * lens_mm / (screen_height_fraction * sensor_height_mm)',
                               'region_height_fractions': REGION_FRACTION, 'region_fraction_source': 'PROPOSAL',
                               'sensor': dict(CAMERA_DEFAULTS, vertical_mm=_r(sensor_v, 4), resolution=resolution)},
            'warnings': warnings, 'provenance_groups': _groups(provenance)}


def _resolve_shot_frames(rows, provs, span_of, timeline):
    """Explicit frames or seconds win; shots that only name a beat split that beat's span evenly, in order."""
    groups = {}
    for i, row in enumerate(rows):
        explicit = 'frame_start' in row or 'frame_end' in row or 'start_s' in row or 'end_s' in row
        if ('frame_start' in row) != ('frame_end' in row) or ('start_s' in row) != ('end_s' in row) or (
                ('frame_start' in row) and ('start_s' in row)):
            model.fail('CINEMATIC_SHOT_TIMES_INVALID', 'shot ' + str(i) + ' needs frame_start+frame_end or start_s+end_s')
        if 'start_s' in row:
            row['frame_start'] = _frame(row.pop('start_s'), timeline)[0]
            row['frame_end'] = _frame(row.pop('end_s'), timeline)[0] - 1
            provs[i]['frame_start'] = provs[i]['frame_end'] = USER
        if explicit:
            if 'beat_id' not in row:
                owner = [b for b, (s, e) in span_of.items() if s <= row['frame_start'] <= e]
                row['beat_id'] = owner[0] if owner else next(iter(span_of))
                provs[i]['beat_id'] = DEFAULT
            if row['beat_id'] not in span_of:
                model.fail('CINEMATIC_SHOT_BEAT_UNKNOWN', 'shot ' + str(i) + ' references ' + row['beat_id'])
            groups.setdefault(row['beat_id'], {'explicit': [], 'implicit': []})['explicit'].append(i)
            continue
        if 'beat_id' not in row:
            model.fail('CINEMATIC_SHOT_TIMES_REQUIRED', 'shot ' + str(i) + ' needs a beat_id or explicit times')
        if row['beat_id'] not in span_of:
            model.fail('CINEMATIC_SHOT_BEAT_UNKNOWN', 'shot ' + str(i) + ' references ' + row['beat_id'])
        groups.setdefault(row['beat_id'], {'explicit': [], 'implicit': []})['implicit'].append(i)
    for beat_id, group in groups.items():
        if not group['implicit']:
            continue
        if group['explicit']:
            model.fail('CINEMATIC_SHOT_TIMES_AMBIGUOUS', beat_id + ' mixes shots with and without explicit times')
        start, end = span_of[beat_id]
        count, frames = len(group['implicit']), end - start + 1
        if frames < count:
            model.fail('CINEMATIC_SHOT_TOO_SHORT', beat_id + ' has ' + str(frames) + ' frames for ' + str(count) + ' shots')
        cursor = start
        for k, i in enumerate(group['implicit']):
            length = frames // count + (frames % count if k == count - 1 else 0)
            rows[i]['frame_start'], rows[i]['frame_end'] = cursor, cursor + length - 1
            provs[i]['frame_start'] = provs[i]['frame_end'] = PROPOSED
            cursor += length


def _check_coverage(rows, timeline):
    if not rows:
        model.fail('CINEMATIC_SHOTS_INVALID', 'no shots')
    for i, row in enumerate(rows):
        label = str(row.get('shot_id', i))
        if row['frame_end'] < row['frame_start']:
            model.fail('CINEMATIC_SHOT_TOO_SHORT', label + ' ends before it starts')
        if i == 0 and row['frame_start'] != timeline['frame_start']:
            model.fail('CINEMATIC_SHOT_COVERAGE', 'the first shot starts at ' + str(row['frame_start']) + ', the timeline at '
                       + str(timeline['frame_start']))
        if i:
            previous = rows[i - 1]['frame_end']
            if row['frame_start'] <= previous:
                model.fail('CINEMATIC_SHOT_OVERLAP', label + ' starts at ' + str(row['frame_start']) + ' before frame '
                           + str(previous) + ' ends')
            if row['frame_start'] > previous + 1:
                model.fail('CINEMATIC_SHOT_GAP', 'frames ' + str(previous + 1) + '-' + str(row['frame_start'] - 1)
                           + ' have no shot')
    if rows[-1]['frame_end'] != timeline['frame_end']:
        model.fail('CINEMATIC_SHOT_COVERAGE', 'the last shot ends at ' + str(rows[-1]['frame_end']) + ', the timeline at '
                   + str(timeline['frame_end']))


# ----------------------------------------------------------------------------------------------- cast and positions

def make_cast(brief, records):
    """Actors the spatial planners work with: the brief actors (or every registered actor without a brief), each
    with its registry record. References resolve by actor_id, object name or label."""
    by_id = {r['actor_id']: r for r in records if isinstance(r, dict) and isinstance(r.get('actor_id'), str)}
    rows = (brief or {}).get('actors') if brief else [{'actor_id': aid, 'role': r.get('role')} for aid, r in sorted(by_id.items())]
    actors, index = {}, {}
    for row in rows or []:
        record = by_id.get(row['actor_id'])
        if record is None:
            model.fail('ACTOR_MISSING', row['actor_id'] + ' from the brief is no longer registered for this asset')
        actors[row['actor_id']] = {'actor_id': row['actor_id'], 'object': record['object'],
                                   'meshes': list(record.get('meshes') or []), 'role': row.get('role'),
                                   'label': row.get('label'), 'record': record}
        for key in (row['actor_id'], record['object'], row.get('label')):
            if key:
                index[key] = row['actor_id']
    return {'actors': actors, 'index': index}


def cast_objects(cast, extra=()):
    names = []
    for actor in cast['actors'].values():
        for name in [actor['object']] + actor['meshes']:
            if isinstance(name, str) and name not in names:
                names.append(name)
    for name in extra:
        if isinstance(name, str) and name not in names and name not in cast['index']:
            names.append(name)
    return names[:MAX_PRESENCE]


def _cast_ref(cast, ref, field):
    if not isinstance(ref, str) or ref not in cast['index']:
        model.fail('CINEMATIC_ACTOR_UNKNOWN', field + ' must name a brief actor (actor_id, object or label)')
    return cast['index'][ref]


def _location(presence, name):
    row = ((presence or {}).get('objects') or {}).get(name)
    return list(row['location']) if row else None


def _radius(cast, presence, aid):
    measured = _measure(cast['actors'][aid]['record'], presence)
    return measured['footprint_radius_m'] if measured and _num(measured.get('footprint_radius_m')) else None


def _place(cast, presence, value, field):
    """A spatial reference: [x, y, z], an actor reference, an object name, or a brief target dict."""
    if isinstance(value, dict):
        kind = value.get('type')
        if kind == 'POINT':
            return {'type': 'POINT', 'position': model.vector(value.get('point'), field)}
        if kind == 'ACTOR':
            value = value.get('actor_id')
        elif kind == 'OBJECT':
            value = value.get('object')
        else:
            model.fail('CINEMATIC_TARGET_INVALID', field + ' is unresolved')
    if isinstance(value, (list, tuple)):
        return {'type': 'POINT', 'position': model.vector(value, field)}
    ref = _ref(value, field)
    if ref in cast['index']:
        aid = cast['index'][ref]
        position = _location(presence, cast['actors'][aid]['object'])
        if position is None:
            model.fail('CINEMATIC_OBJECT_MISSING', cast['actors'][aid]['object'] + ' is not in the open file')
        return {'type': 'ACTOR', 'actor_id': aid, 'position': position}
    position = _location(presence, ref)
    if position is None:
        model.fail('CINEMATIC_TARGET_OBJECT_MISSING', ref + ' is neither a brief actor nor an object of the file')
    return {'type': 'OBJECT', 'object': ref, 'position': position}


# ----------------------------------------------------------------------------------------------------- actor paths

def _default_paths(brief):
    rows = []
    for event in (brief or {}).get('events') or []:
        if event['kind'] == 'APPROACH' and event.get('actors') and (event.get('target') or {}).get('type') in (
                'ACTOR', 'OBJECT', 'POINT'):
            rows.append({'actor': event['actors'][0], 'target': event['target'], 'event_id': event['event_id']})
    return rows


def plan_paths(options, cast, presence, timeline, beats=None, brief=None):
    """Straight paths per actor that only reuse registered body Actions with measured root motion. A clip proposal
    (start frame, repeat, staging offsets) is what cinematic_stage_actors/sync_actions may apply; without a
    compatible clip the supported alternative is static staging plus a cut. Locomotion is never generated."""
    opts = model.closed(options, PATH_OPTION_KEYS)
    prov = {}
    clearance_m = model.number(opts.get('clearance_m', PROPOSALS['clearance_m']), 'clearance_m', 0, 100)
    prov['clearance_m'] = USER if 'clearance_m' in opts else PROPOSED
    allow_retime = _flag(opts.get('allow_retime', False), 'allow_retime')
    entries = opts.get('paths')
    if entries is None:
        if brief is None:
            model.fail('CINEMATIC_PATHS_REQUIRED', 'pass options.paths or a brief with APPROACH events')
        entries, source = _default_paths(brief), 'DERIVED_FROM_BRIEF'
    else:
        source = 'USER'
    entries = _items(entries, 'options.paths', MAX_PATHS)
    schedule = {row['event_id']: row for row in (beats or {}).get('event_schedule') or []}
    beat_end = {b['beat_id']: b['end_s'] for b in (beats or {}).get('beats') or []}
    boundaries = sorted({b['frame_start'] for b in (beats or {}).get('beats') or []})
    rate = model.fps_value(timeline['fps'], timeline['fps_base'])
    context = {'cast': cast, 'presence': presence, 'timeline': timeline, 'rate': rate, 'clearance_m': clearance_m,
               'allow_retime': allow_retime, 'schedule': schedule, 'boundaries': boundaries, 'beat_end': beat_end}
    paths = [_plan_path(entry, i, context) for i, entry in enumerate(entries)]
    clearance = _clearance(paths, cast, presence, timeline, clearance_m)
    missing = [m for p in paths for m in p.pop('missing')]
    statuses = {p['status'] for p in paths}
    status = 'success' if statuses <= {'PLANNED', 'NO_MOVEMENT'} and not any(p['warnings'] for p in paths) and not any(
        pair['passed'] is False for pair in clearance['pairs']) else 'warning'
    return {'schema': PATHS_SCHEMA, 'status': status, 'source': source,
            'timeline': {k: timeline[k] for k in ('fps', 'fps_base', 'frame_start', 'frame_end')},
            'paths': paths, 'clearance': clearance, 'missing_prerequisites': missing,
            'positions': {'source': 'SCENE_MEASURED', 'frame': (presence or {}).get('frame_current'),
                          'note': 'Object.matrix_world translation read without changing the frame'},
            'thresholds': {k: PROPOSALS[k] for k in ('in_place_threshold_m', 'speed_tolerance_ratio', 'clearance_m',
                                                     'vertical_tolerance_m', 'yaw_tolerance_deg')},
            'threshold_source': 'PROPOSAL', 'no_locomotion_generated': True, 'provenance': prov}


def _plan_path(entry, index, ctx):
    field = 'paths[' + str(index) + ']'
    entry = model.closed(_dict(entry, field), PATH_KEYS)
    cast, presence, timeline, rate = ctx['cast'], ctx['presence'], ctx['timeline'], ctx['rate']
    prov, warnings, missing = {}, [], []
    aid = _cast_ref(cast, entry.get('actor'), field + '.actor')
    actor = cast['actors'][aid]
    here = _location(presence, actor['object'])
    if here is None:
        model.fail('CINEMATIC_OBJECT_MISSING', actor['object'] + ' is not in the open file')
    if 'start' in entry:
        start, prov['start'] = model.vector(entry['start'], field + '.start'), USER
    else:
        start, prov['start'] = list(here), DEFAULT
    if 'target' not in entry:
        model.fail('CINEMATIC_PATH_TARGET_REQUIRED', field + ' needs target ([x, y, z], actor or object)')
    target = _place(cast, presence, entry['target'], field + '.target')
    # Targets come from options.paths or from an event target of the brief: both are user requirements.
    prov['target'] = USER
    result = {'actor_id': aid, 'object': actor['object'], 'event_id': entry.get('event_id'), 'status': 'PLANNED',
              'start': _rv(start), 'target': {k: (_rv(v) if k == 'position' else v) for k, v in target.items()},
              'clip': None, 'alternative': None, 'warnings': warnings, 'missing': missing, 'provenance': prov}
    stop = 0.0
    if target['type'] == 'ACTOR':
        if target['actor_id'] == aid:
            model.fail('CINEMATIC_PATH_TARGET_INVALID', field + ' targets its own actor')
        if 'stop_distance_m' in entry:
            stop, prov['stop_distance_m'] = model.number(entry['stop_distance_m'], field + '.stop_distance_m', 0, 1000), USER
        else:
            mine, theirs = _radius(cast, presence, aid), _radius(cast, presence, target['actor_id'])
            if mine is None or theirs is None:
                result.update(status='NEEDS_INPUT', question='stop_distance_m: the actor bounds could not be measured')
                return result
            stop, prov['stop_distance_m'] = mine + theirs + ctx['clearance_m'], DEFAULT
    elif 'stop_distance_m' in entry:
        stop, prov['stop_distance_m'] = model.number(entry['stop_distance_m'], field + '.stop_distance_m', 0, 1000), USER
    aim = target['position']
    full = _hdist(start, aim)
    if full <= stop + 1e-9:
        result.update(status='NO_MOVEMENT', target_point=_rv(start), distance_m=0.0, stop_distance_m=_r(stop))
        return result
    ux, uy = (aim[0] - start[0]) / full, (aim[1] - start[1]) / full
    z = start[2] if target['type'] == 'ACTOR' else aim[2]
    point = [aim[0] - ux * stop, aim[1] - uy * stop, z]
    distance = _hdist(start, point)
    heading = _heading(start, point)
    vertical = point[2] - start[2]
    if abs(vertical) > PROPOSALS['vertical_tolerance_m']:
        warnings.append({'code': 'VERTICAL_CHANGE', 'detail': 'the path climbs ' + str(_r(vertical, 3))
                         + ' m; a ground locomotion clip does not climb (PROPOSAL tolerance)'})
    if 'orientation_target' in entry:
        facing = _place(cast, presence, entry['orientation_target'], field + '.orientation_target')
        yaw, prov['orientation_target'] = _heading(point, facing['position']), USER
        orientation = {k: (_rv(v) if k == 'position' else v) for k, v in facing.items()}
    elif target['type'] == 'ACTOR':
        yaw, prov['orientation_target'] = _heading(point, aim), DEFAULT
        orientation = {'type': 'ACTOR', 'actor_id': target['actor_id']}
    else:
        yaw, prov['orientation_target'] = heading, DEFAULT
        orientation = {'type': 'TRAVEL_DIRECTION'}
    event_id = entry.get('event_id')
    if 'departure_s' in entry:
        departure, prov['departure_s'] = model.number(entry['departure_s'], field + '.departure_s', 0, timeline['duration_s']
                                                      if 'duration_s' in timeline else model.MAX_DURATION_S), USER
    elif event_id in ctx['schedule']:
        departure = ctx['schedule'][event_id]['time_s']
        prov['departure_s'] = ctx['schedule'][event_id]['time_source']
    else:
        departure, prov['departure_s'] = 0.0, DEFAULT
    departure_frame, departure_record = _frame(departure, timeline)
    arrival = requested = None
    if 'arrival_s' in entry:
        arrival, prov['arrival_s'] = model.number(entry['arrival_s'], field + '.arrival_s', 0, model.MAX_DURATION_S), USER
        if arrival <= departure:
            model.fail('CINEMATIC_PATH_TIMING_INVALID', field + ' arrives before it departs')
        requested = distance / (arrival - departure)
    if 'speed_m_s' in entry:
        speed = model.number(entry['speed_m_s'], field + '.speed_m_s', 0.01, 50)
        if requested is not None and abs(speed / requested - 1) > PROPOSALS['speed_tolerance_ratio']:
            model.fail('CINEMATIC_PATH_TIMING_CONFLICT', field + ' speed_m_s and arrival_s disagree')
        requested, prov['speed_m_s'] = speed, USER
    result.update(target_point=_rv(point), stop_distance_m=_r(stop), distance_m=_r(distance),
                  vertical_delta_m=_r(vertical), heading_deg=_r(heading), orientation_target=orientation,
                  arrival_yaw_deg=_r(yaw), departure_s=departure, departure_frame=departure_frame,
                  requested_speed_m_s=_r(requested) if requested is not None else None,
                  conversions=[dict(departure_record, purpose='departure')])
    rows = _body_rows(actor['record'])
    clip, reason = None, 'NO_BODY_ACTION'
    if 'action' in entry:
        name = _ref(entry['action'], field + '.action')
        row = next((r for r in rows if r['action'] == name), None)
        prov['action'] = USER
        if row is None:
            missing.append({'code': 'ACTION_NOT_AVAILABLE', 'actor_id': aid, 'ref': name, 'blocking': True,
                            'detail': 'the Action is not registered for this actor'})
            result['status'] = 'MISSING_PREREQUISITE'
            return result
        clip, reason = _locomotion(row, rate)
    else:
        evaluated = [_locomotion(r, rate) for r in rows]
        clips = [c for c, _ in evaluated if c]
        if clips:
            if requested is not None:
                clips.sort(key=lambda c: (abs(c['speed_m_s'] - requested), c['action']))
            else:
                clips.sort(key=lambda c: c['action'])
            clip, prov['action'] = clips[0], PROPOSED
        elif rows:
            # IN_PLACE (root motion measured below the proposal) and ROOT_MOTION_NOT_MEASURED are different facts.
            reasons = sorted({why for _, why in evaluated})
            reason = reasons[0] if len(reasons) == 1 else '+'.join(reasons)
    if clip is None:
        candidates = [f for f in ctx['boundaries'] if f > departure_frame]
        result['alternative'] = {'kind': 'STATIC_STAGING_WITH_CUT', 'before_cut': {'position': _rv(start)},
                                 'after_cut': {'position': _rv(point), 'yaw_deg': _r(yaw)},
                                 'cut_frame_candidates': candidates[:8],
                                 'detail': 'no compatible locomotion clip (' + reason + '); the actor holds its start '
                                           'before a cut and appears at the target after it; nothing is generated'}
        missing.append({'code': 'LOCOMOTION_ACTION_MISSING', 'actor_id': aid, 'blocking': False, 'reason': reason,
                        'detail': 'register a body Action with measured root motion to travel on screen'})
        result['status'] = 'ALTERNATIVE_REQUIRED'
        if arrival is not None:
            result.update(arrival_s=arrival, arrival_frame=_frame(min(arrival, model.MAX_DURATION_S), timeline)[0])
        return result
    native = clip['speed_m_s']
    scale = 1.0
    if requested is not None and abs(requested / native - 1) > PROPOSALS['speed_tolerance_ratio']:
        if ctx['allow_retime']:
            scale, prov['retime'] = native / requested, USER
        else:
            result['status'] = 'TIMING_CONFLICT'
            result['alternatives'] = [
                {'kind': 'NATIVE_SPEED', 'arrival_s': _r(departure + distance / native)},
                {'kind': 'EXPLICIT_RETIME', 'scale': _r(native / requested), 'requires': 'options.allow_retime=true'},
                {'kind': 'STATIC_STAGING_WITH_CUT', 'cut_frame_candidates': [f for f in ctx['boundaries'] if f > departure_frame][:8]}]
            warnings.append({'code': 'SPEED_MISMATCH', 'detail': 'requested ' + str(_r(requested, 3)) + ' m/s, clip '
                             + str(_r(native, 3)) + ' m/s measured; Actions are never retimed without allow_retime'})
    speed = native / scale
    travel = distance / speed
    planned_arrival = departure + travel
    if arrival is None or result['status'] == 'TIMING_CONFLICT':
        if arrival is not None:
            result['requested_arrival_s'] = arrival
        arrival = planned_arrival
        prov['arrival_s'] = DEFAULT
    if arrival > model.MAX_DURATION_S:
        model.fail('CINEMATIC_PATH_TOO_LONG', field + ' would arrive after ' + str(model.MAX_DURATION_S) + ' s')
    arrival_frame, arrival_record = _frame(arrival, timeline)
    if arrival_frame > timeline['frame_end']:
        warnings.append({'code': 'ARRIVAL_AFTER_TIMELINE_END', 'detail': 'arrival frame ' + str(arrival_frame)
                         + ' is after the timeline end ' + str(timeline['frame_end'])})
    beat_id = (ctx['schedule'].get(event_id) or {}).get('beat_id')
    if beat_id in ctx['beat_end'] and arrival > ctx['beat_end'][beat_id] + 1e-9:
        warnings.append({'code': 'ARRIVAL_AFTER_BEAT_END', 'beat_id': beat_id,
                         'detail': 'at the measured clip speed the actor arrives at ' + str(_r(arrival, 3)) + ' s, after '
                                   'its beat ends at ' + str(_r(ctx['beat_end'][beat_id], 3)) + ' s; lengthen the beat, '
                                   'start earlier or cut'})
    repeat = distance / clip['cycle_distance_m']
    strip_frames = repeat * clip['cycle_frames'] * scale
    frame_range = clip['frame_range'] if isinstance(clip['frame_range'], (list, tuple)) else [None, None]
    if clip['direction']:
        clip_heading = math.degrees(math.atan2(clip['direction'][1], clip['direction'][0]))
        yaw_offset = _wrap(heading - clip_heading)
        rotation = abs(yaw_offset) > PROPOSALS['yaw_tolerance_deg']
    else:
        clip_heading, yaw_offset, rotation = 'UNKNOWN', 'UNKNOWN', None
    result['clip'] = {'action': clip['action'], 'slot': clip['slot'], 'action_frame_start': frame_range[0],
                      'action_frame_end': frame_range[1], 'start_frame': departure_frame, 'repeat': _r(repeat),
                      'scale': _r(scale), 'strip_frames': _r(strip_frames, 4),
                      'end_frame_exact': _r(departure_frame + strip_frames, 4),
                      'cycle_distance_m': _r(clip['cycle_distance_m']), 'cycle_frames': clip['cycle_frames'],
                      'native_speed_m_s': _r(native), 'retime': scale != 1.0,
                      'measurement_source': 'registry body_actions.root_motion (cinematic_register_actor)',
                      'assumption': 'duration_frames is the span over which horizontal_distance_m was measured'}
    result['staging'] = {'delta_location_m': _rv([start[0] - here[0], start[1] - here[1], start[2] - here[2]]),
                         'clip_heading_deg': _r(clip_heading) if _num(clip_heading) else clip_heading,
                         'yaw_offset_deg': _r(yaw_offset) if _num(yaw_offset) else yaw_offset,
                         'requires_rotation': rotation,
                         'assumption': 'the clip root starts at the object origin; cinematic_stage_actors measures '
                                       'and refuses ROOT_MOTION_AMBIGUOUS rotations'}
    result.update(speed_m_s=_r(speed), arrival_s=arrival, arrival_frame=arrival_frame)
    result['conversions'].append(dict(arrival_record, purpose='arrival'))
    return result


def _track(path):
    if path['status'] in ('PLANNED', 'TIMING_CONFLICT') and path.get('arrival_frame') is not None:
        return (path['departure_frame'], path['arrival_frame'], path['start'], path['target_point'], 'MOVE')
    if path['status'] == 'ALTERNATIVE_REQUIRED':
        cut = (path['alternative']['cut_frame_candidates'] or [None])[0]
        if cut is not None:
            return (cut, cut, path['start'], path['target_point'], 'CUT')
    return None


def _position_at(here, tracks, frame):
    position = here
    for start, end, origin, goal, _ in tracks:
        if frame < start:
            return origin if position is here else position
        if frame >= end:
            position = goal
            continue
        u = (frame - start) / float(end - start)
        return [origin[0] + (goal[0] - origin[0]) * u, origin[1] + (goal[1] - origin[1]) * u, origin[2]]
    return position


def _clearance(paths, cast, presence, timeline, clearance_m):
    tracks = {}
    for path in paths:
        track = _track(path)
        if track:
            tracks.setdefault(path['actor_id'], []).append(track)
    for rows in tracks.values():
        rows.sort(key=lambda t: t[0])
    homes = {aid: _location(presence, a['object']) for aid, a in cast['actors'].items()}
    ids = sorted(aid for aid, here in homes.items() if here is not None)
    radii = {aid: _radius(cast, presence, aid) for aid in ids}
    first, last = timeline['frame_start'], timeline['frame_end']
    pairs = []
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            best = None
            for frame in range(first, last + 1):
                d = _hdist(_position_at(homes[a], tracks.get(a, []), frame), _position_at(homes[b], tracks.get(b, []), frame))
                if best is None or d < best[0] - 1e-12:
                    best = (d, frame)
            required = radii[a] + radii[b] + clearance_m if radii[a] is not None and radii[b] is not None else None
            pairs.append({'actors': [a, b], 'min_distance_m': _r(best[0]), 'frame': best[1],
                          'required_m': _r(required) if required is not None else 'UNKNOWN',
                          'passed': None if required is None else best[0] >= required - 1e-6})
    return {'model': 'straight line at constant speed between departure and arrival frames (cuts jump); actors '
                     'without a path stay at their measured position; XY distance between object origins',
            'clearance_m': clearance_m, 'radius_method': 'half XY diagonal of the measured bounds',
            'threshold_source': 'PROPOSAL', 'frames_checked': last - first + 1, 'pairs': pairs}


# ----------------------------------------------------------------------------------------------------- interactions

def _default_interactions(brief):
    rows, covered = [], set()
    for item in (brief or {}).get('interaction_requirements') or []:
        row = {'kind': item['kind'], 'actors': item.get('actors') or [], 'action_refs': item.get('action_refs') or []}
        for key in ('event_id', 'time_s', 'contact_required', 'effector', 'description'):
            if item.get(key) is not None:
                row[key] = item[key]
        if item.get('target') and item['target'].get('type') != 'UNRESOLVED':
            row['spatial_target'] = item['target']
        if item.get('event_id'):
            covered.add(item['event_id'])
        rows.append(row)
    for event in (brief or {}).get('events') or []:
        if event['kind'] in model.INTERACTION_KINDS and event['event_id'] not in covered and event.get('actors'):
            row = {'kind': event['kind'], 'actors': event['actors'], 'event_id': event['event_id'],
                   'action_refs': event.get('action_refs') or []}
            if event.get('description'):
                row['description'] = event['description']
            if event.get('target') and event['target'].get('type') != 'UNRESOLVED':
                row['spatial_target'] = event['target']
            rows.append(row)
    return rows


def _criteria(kind, stop):
    if kind in ('OBSERVE', 'GAZE', 'TURN_TOWARD', 'PREPARE_ATTACK'):
        return [{'metric': 'facing_error_deg', 'max': PROPOSALS['facing_tolerance_deg'], 'threshold_source': 'PROPOSAL',
                 'reference': 'actor forward axis (head for GAZE) toward the spatial target'}]
    if kind == 'APPROACH':
        return [{'metric': 'distance_to_target_m', 'max': (stop + PROPOSALS['arrival_tolerance_m']) if _num(stop) else 'UNKNOWN',
                 'threshold_source': 'PROPOSAL'}]
    if kind in REACH_KINDS:
        return [{'metric': 'effector_surface_distance_m', 'max': M3_CONTACT_DEFAULTS['contact_height_m'],
                 'speed_max_m_s': M3_CONTACT_DEFAULTS['contact_speed_m_s'],
                 'min_dwell_frames': M3_CONTACT_DEFAULTS['minimum_dwell_frames'],
                 'threshold_source': M3_CONTACT_DEFAULTS['source']}]
    return [{'metric': 'VISUAL_REVIEW', 'detail': 'no numeric criterion is proposed for ' + kind}]


def _paths_index(paths):
    if paths is None:
        return {}
    if not isinstance(paths, dict) or paths.get('schema') != PATHS_SCHEMA:
        model.fail('CINEMATIC_PATHS_INVALID', 'options.paths must be the result of cinematic_plan_actor_paths')
    index = {}
    for path in paths.get('paths') or []:
        track = _track(path)
        if track:
            index.setdefault(path['actor_id'], []).append(track)
    for rows in index.values():
        rows.sort(key=lambda t: t[0])
    return index


def plan_interactions(options, cast, presence, timeline, beats=None, brief=None):
    """Interaction events with time/frame, spatial target, contact flag, Action references and success criteria.
    Reachability uses the registered reach (interaction_capabilities.reach_m); without it the result is UNKNOWN,
    never zero. Animation is not resolved here."""
    opts = model.closed(options, INTERACTION_OPTION_KEYS)
    tracks = _paths_index(opts.get('paths'))
    schedule = {row['event_id']: row for row in (beats or {}).get('event_schedule') or []}
    entries = opts.get('interactions')
    if entries is None:
        if brief is None:
            model.fail('CINEMATIC_INTERACTIONS_REQUIRED', 'pass options.interactions or a brief')
        entries, source = _default_interactions(brief), 'DERIVED_FROM_BRIEF'
    else:
        source = 'USER'
    entries = _items(entries, 'options.interactions', MAX_INTERACTIONS)
    events = {e['event_id']: e for e in (brief or {}).get('events') or []}
    rows, missing, used = [], [], set()
    counter = 0
    for index, entry in enumerate(entries):
        field = 'interactions[' + str(index) + ']'
        # Rows derived from the brief carry the user's own requirements/events, so both sources are USER.
        origin = USER
        data = model.closed(_dict(entry, field), INTERACTION_KEYS)
        prov, warnings = {}, []
        kind = model.choice(data.get('kind'), field + '.kind', model.INTERACTION_KINDS)
        actors = [_cast_ref(cast, a, field + '.actors') for a in _items(data.get('actors'), field + '.actors', model.MAX_ACTORS)]
        if not actors:
            model.fail('CINEMATIC_INTERACTION_ACTORS_REQUIRED', field + ' needs at least one actor')
        event_id = data.get('event_id')
        if event_id is not None:
            event_id = model.name(event_id, field + '.event_id')
            if brief is not None and event_id not in events:
                model.fail('CINEMATIC_EVENT_UNKNOWN', field + ' references ' + event_id)
        else:
            event_id, counter = _next_id('I', counter, used)
            prov['event_id'] = DEFAULT
        used.add(event_id)
        time = frame = record = None
        if 'time_s' in data and 'frame' in data:
            model.fail('CINEMATIC_OPTION_CONFLICT', field + ' sets both time_s and frame')
        if 'time_s' in data:
            time, prov['time_s'] = model.number(data['time_s'], field + '.time_s', 0, model.MAX_DURATION_S), origin
        elif 'frame' in data:
            frame, prov['time_s'] = model.number(data['frame'], field + '.frame', integer=True), origin
            time = model.frame_to_seconds(frame, timeline['fps'], timeline['fps_base'], timeline['frame_start'])
        elif event_id in schedule:
            time, prov['time_s'] = schedule[event_id]['time_s'], schedule[event_id]['time_source']
        elif event_id in events and events[event_id].get('time_s') is not None:
            time, prov['time_s'] = events[event_id]['time_s'], USER
        if time is not None:
            if time < 0:
                model.fail('CINEMATIC_INTERACTION_OUTSIDE_TIMELINE', field + ' is before the timeline start')
            if frame is None:
                frame, record = _frame(time, timeline)
            if not timeline['frame_start'] <= frame <= timeline['frame_end']:
                model.fail('CINEMATIC_INTERACTION_OUTSIDE_TIMELINE', field + ' frame ' + str(frame) + ' is outside '
                           + str(timeline['frame_start']) + '-' + str(timeline['frame_end']))
        else:
            warnings.append({'code': 'UNSCHEDULED', 'detail': 'no time: pass time_s/frame or the beats with its event'})
        target = None
        raw_target = data.get('spatial_target')
        if raw_target is None and event_id in events:
            raw_target = events[event_id].get('target')
        if raw_target is not None:
            target = _place(cast, presence, raw_target, field + '.spatial_target')
            prov['spatial_target'] = origin
        contact = data.get('contact_required')
        if contact is None:
            contact, prov['contact_required'] = kind in CONTACT_KINDS, DEFAULT
        else:
            contact, prov['contact_required'] = _flag(contact, field + '.contact_required'), origin
        refs = _refs(data.get('action_refs'), field + '.action_refs')
        for ref in refs:
            if not any(any(r['action'] == ref for r in _body_rows(cast['actors'][a]['record'])) for a in actors):
                missing.append({'code': 'ACTION_NOT_AVAILABLE', 'actor_id': actors[0], 'ref': ref, 'blocking': True,
                                'detail': field + ' references an Action not registered for its actors'})
        effector = _ref(data['effector'], field + '.effector') if data.get('effector') is not None else None
        if contact and effector is None:
            warnings.append({'code': 'EFFECTOR_NOT_DECLARED', 'detail': 'contact QA needs an explicit effector bone '
                             '(M3 rule: contacts are never inferred from names)'})
        performer = actors[0]
        stop = None
        if kind == 'APPROACH' and target is not None and target['type'] == 'ACTOR':
            mine, theirs = _radius(cast, presence, performer), _radius(cast, presence, target['actor_id'])
            stop = mine + theirs + PROPOSALS['clearance_m'] if mine is not None and theirs is not None else None
        if data.get('success_criteria') is not None:
            criteria = [c if isinstance(c, dict) else {'metric': 'DECLARED', 'detail': model.text(c, field + '.success_criteria', 300)}
                        for c in _items(data['success_criteria'], field + '.success_criteria', 8)]
            prov['success_criteria'] = origin
        else:
            criteria, prov['success_criteria'] = _criteria(kind, stop), PROPOSED
        reach = _reachability(cast, presence, tracks, kind, contact, performer, target, frame)
        row = {'event_id': event_id, 'kind': kind, 'actors': actors, 'time_s': time, 'frame': frame,
               'spatial_target': None if target is None else {k: (_rv(v) if k == 'position' else v) for k, v in target.items()},
               'contact_required': contact, 'action_refs': refs, 'effector': effector, 'success_criteria': criteria,
               'reachability': reach, 'warnings': warnings, 'provenance': prov}
        if data.get('description'):
            row['description'] = model.text(data['description'], field + '.description', 1000)
        if 'duration_s' in data:
            row['duration_s'] = model.number(data['duration_s'], field + '.duration_s', 0, model.MAX_DURATION_S)
        if record is not None:
            row['conversion'] = record
        rows.append(row)
    status = 'success' if not missing and all(not r['warnings'] for r in rows) and all(
        r['reachability']['status'] != 'OUT_OF_REACH' for r in rows) else 'warning'
    return {'schema': INTERACTIONS_SCHEMA, 'status': status, 'source': source,
            'timeline': {k: timeline[k] for k in ('fps', 'fps_base', 'frame_start', 'frame_end')},
            'interactions': rows, 'missing_prerequisites': missing, 'animation_resolved': False,
            'positions_source': 'actor paths (straight-line model)' if tracks else 'SCENE_MEASURED at frame '
                                + str((presence or {}).get('frame_current')),
            'threshold_source': 'PROPOSAL', 'm3_contact_defaults': M3_CONTACT_DEFAULTS}


def _reachability(cast, presence, tracks, kind, contact, performer, target, frame):
    if kind not in REACH_KINDS and not contact:
        return {'status': 'NOT_APPLICABLE'}
    capabilities = cast['actors'][performer]['record'].get('interaction_capabilities') or {}
    reach = capabilities.get('reach_m') if isinstance(capabilities, dict) else None
    reach = reach if _num(reach) and reach > 0 else None
    if target is None:
        return {'status': 'UNKNOWN', 'reason': 'no spatial target', 'reach_m': reach if reach is not None else 'UNKNOWN'}
    here = _location(presence, cast['actors'][performer]['object'])
    if here is None:
        return {'status': 'UNKNOWN', 'reason': 'actor object not found', 'reach_m': reach if reach is not None else 'UNKNOWN'}
    position = _position_at(here, tracks.get(performer, []), frame) if frame is not None else here
    goal = target['position']
    if target['type'] == 'ACTOR':
        other = _location(presence, cast['actors'][target['actor_id']]['object'])
        goal = _position_at(other, tracks.get(target['actor_id'], []), frame) if frame is not None else other
    distance = _hdist(position, goal)
    surface = None
    if target['type'] == 'ACTOR':
        radius = _radius(cast, presence, target['actor_id'])
        surface = max(distance - radius, 0.0) if radius is not None else None
    result = {'model': 'XY distance from the actor origin to the target (minus the target footprint radius for '
                       'actors) against the registered reach; a planning check, not contact QA',
              'distance_m': _r(distance), 'surface_distance_m': _r(surface) if surface is not None else 'UNKNOWN',
              'reach_m': reach if reach is not None else 'UNKNOWN',
              'reach_source': 'registry interaction_capabilities.reach_m' if reach is not None else 'UNKNOWN',
              'position_source': 'actor paths' if tracks.get(performer) else 'SCENE_MEASURED'}
    if reach is None:
        result['status'] = 'UNKNOWN'
    else:
        result['status'] = 'REACHABLE' if (surface if surface is not None else distance) <= reach + 1e-9 else 'OUT_OF_REACH'
    return result


# ------------------------------------------------------------------------------------------------------ render plan

def _output_path(value):
    if not isinstance(value, str) or not value:
        model.fail('CINEMATIC_OUTPUT_PATH_INVALID', 'output_path must be text')
    if len(value) >= 200:
        model.fail('CINEMATIC_OUTPUT_PATH_TOO_LONG', 'output_path must stay under 200 characters (Windows MAX_PATH '
                   'includes the frame file names)')
    path = Path(value)
    if not path.is_absolute():
        model.fail('CINEMATIC_OUTPUT_PATH_NOT_ABSOLUTE', value)
    if '..' in path.parts:
        model.fail('CINEMATIC_OUTPUT_PATH_INVALID', 'output_path must not contain ..')
    if path.exists():
        model.fail('CINEMATIC_OUTPUT_EXISTS', value + ' already exists; renders never overwrite')
    return str(path)


def _estimate(engine, resolution, samples, images):
    for rate in MEASURED_RATES:
        if rate['engine'] == engine and rate['resolution'] == list(resolution) and rate['samples'] == samples:
            low, high = rate['seconds_per_image']
            per_call = max(1, int(PROPOSALS['render_call_budget_s'] // high))
            return ({'status': 'MEASURED_RATE', 'seconds': [_r(images * low, 3), _r(images * high, 3)],
                     'seconds_per_image': [low, high], 'source': rate['source'],
                     'caveat': 'measured on a simple scene: a heavier scene renders slower'},
                    {'status': 'FROM_MEASURED_RATE', 'frames_per_call': per_call, 'calls': -(-images // per_call),
                     'budget_s': PROPOSALS['render_call_budget_s'], 'budget_source': 'PROPOSAL under the 180 s socket timeout'})
    nearest = [{'resolution': r['resolution'], 'samples': r['samples'], 'seconds_per_image': r['seconds_per_image'],
                'source': r['source']} for r in MEASURED_RATES if r['engine'] == engine]
    return ({'status': 'NOT_MEASURED', 'seconds': 'NOT_MEASURED', 'measured_configurations': nearest,
             'detail': 'no measured rate for this engine/resolution/samples; nothing is extrapolated'},
            {'status': 'NOT_MEASURED', 'first_chunk_frames': PROPOSALS['first_chunk_frames'],
             'budget_s': PROPOSALS['render_call_budget_s'],
             'detail': 'render a first chunk, measure seconds per image, then size the chunks under the budget'})


def _stage(name, engine, resolution, samples, fmt, ranges, backend, scope):
    frames = sum(end - start + 1 for start, end in ranges)
    estimate, chunking = _estimate(engine, resolution, samples, frames)
    settings = {'engine': engine, 'resolution_x': resolution[0], 'resolution_y': resolution[1],
                'resolution_percentage': 100}
    settings.update(RENDER_FORMATS[fmt])
    if engine == 'CYCLES':
        settings['cycles'] = {'device': 'GPU', 'compute_device_type': 'OPTIX', 'samples': samples}
        devices = [d[0] for d in backend.get('devices') or [] if d[1] == 'OPTIX' and d[2]]
        label = 'GPU OPTIX: ' + ', '.join(devices)
    else:
        if engine == 'BLENDER_EEVEE':
            settings['eevee'] = {'taa_render_samples': samples}
        label = 'GPU rasterizer (' + str(backend.get('gpu_backend')) + ')'
    return {'stage': name, 'scope': scope, 'engine': engine, 'resolution': list(resolution), 'samples': samples,
            'format': fmt, 'frame_ranges': [list(r) for r in ranges], 'frames': frames, 'images': frames,
            'megapixels': _r(frames * resolution[0] * resolution[1] / 1e6, 3), 'estimate': estimate,
            'chunking': chunking, 'blender_settings': settings, 'backend': label}


def _ranges(frames):
    ranges = []
    for frame in sorted(set(frames)):
        if ranges and frame == ranges[-1][1] + 1:
            ranges[-1][1] = frame
        else:
            ranges.append([frame, frame])
    return ranges


def render_plan(timeline, shot_rows, options, settings, backend, reference=None):
    """A render plan, never a render. The Workbench preview of the whole timeline is always the first stage;
    EEVEE/Cycles stages are planned only with an explicit scope (frame_range or shots). Cycles requires OptiX on
    the GPU (user rule): without it the plan is refused, a CPU fallback is never planned."""
    opts = model.closed(options, RENDER_OPTION_KEYS)
    prov, warnings = {}, []
    if 'fps' in opts:
        fps = model.number(opts['fps'], 'fps', 1, 240, integer=True)
        if fps != timeline['fps']:
            model.fail('CINEMATIC_FPS_MISMATCH', 'render fps ' + str(fps) + ' differs from the timeline '
                       + str(timeline['fps']) + '; the sequence is never retimed for a render')
        prov['fps'] = USER
    else:
        prov['fps'] = DEFAULT
    preset = model.choice(opts.get('quality_preset', 'PREVIEW'), 'quality_preset', QUALITY_PRESETS)
    prov['quality_preset'] = USER if 'quality_preset' in opts else DEFAULT
    if 'engine' in opts:
        engine, prov['engine'] = model.choice(opts['engine'], 'engine', RENDER_ENGINES), USER
    else:
        engine, prov['engine'] = ('BLENDER_WORKBENCH' if preset == 'PREVIEW' else 'BLENDER_EEVEE'), DEFAULT
    if preset == 'PREVIEW' and engine != 'BLENDER_WORKBENCH':
        model.fail('CINEMATIC_RENDER_PRESET_INVALID', 'PREVIEW renders with BLENDER_WORKBENCH; use DRAFT or PRODUCTION '
                   'for ' + engine)
    expensive = engine != 'BLENDER_WORKBENCH'
    if 'frame_range' in opts and 'shots' in opts:
        model.fail('CINEMATIC_OPTION_CONFLICT', 'pass frame_range or shots, not both')
    full = [[timeline['frame_start'], timeline['frame_end']]]
    if 'frame_range' in opts:
        start, end = model.vector(opts['frame_range'], 'frame_range', 2, -1_048_574, 1_048_574)
        if int(start) != start or int(end) != end:
            model.fail('CINEMATIC_INTEGER_REQUIRED', 'frame_range must hold integers')
        start, end = int(start), int(end)
        if not timeline['frame_start'] <= start <= end <= timeline['frame_end']:
            model.fail('CINEMATIC_RANGE_INVALID', 'frame_range must lie inside ' + str(full[0]))
        ranges, prov['frame_range'] = [[start, end]], USER
    elif 'shots' in opts:
        wanted = [model.name(s, 'shots') for s in _items(opts['shots'], 'shots', model.MAX_SHOTS)]
        index = {s.get('shot_id'): s for s in shot_rows or []}
        unknown = [s for s in wanted if s not in index]
        if unknown or not wanted:
            model.fail('CINEMATIC_SHOT_UNKNOWN', 'unknown shots: ' + ', '.join(unknown or ['(none given)']))
        frames = []
        for shot_id in wanted:
            frames += list(range(index[shot_id]['frame_start'], index[shot_id]['frame_end'] + 1))
        ranges, prov['frame_range'] = _ranges(frames), USER
    else:
        ranges, prov['frame_range'] = full, DEFAULT
    if expensive and prov['frame_range'] != USER:
        model.fail('CINEMATIC_FINAL_SCOPE_REQUIRED', engine + ' renders are planned only with an explicit scope: pass '
                   'frame_range or shots (the Workbench preview of the whole timeline is planned first)')
    source_resolution = (settings or {}).get('resolution') or [1920, 1080, 100]
    width, height = source_resolution[0], source_resolution[1]
    long_side = PROPOSALS['preview_long_side_px']
    preview_resolution = ([long_side, max(1, int(round(long_side * height / width)))] if width >= height
                          else [max(1, int(round(long_side * width / height))), long_side])
    if 'resolution' in opts:
        values = model.vector(opts['resolution'], 'resolution', 2, 4, 16384)
        if any(int(v) != v for v in values):
            model.fail('CINEMATIC_INTEGER_REQUIRED', 'resolution must hold integers')
        resolution, prov['resolution'] = [int(v) for v in values], USER
    elif expensive:
        percentage = source_resolution[2] if len(source_resolution) > 2 else 100
        resolution = [int(round(width * percentage / 100.0)), int(round(height * percentage / 100.0))]
        prov['resolution'] = DEFAULT
    else:
        resolution, prov['resolution'] = preview_resolution, PROPOSED
    fmt = model.choice(opts.get('format', 'PNG_SEQUENCE'), 'format', tuple(RENDER_FORMATS))
    prov['format'] = USER if 'format' in opts else DEFAULT
    if not expensive and fmt != 'PNG_SEQUENCE':
        model.fail('CINEMATIC_PREVIEW_FORMAT', 'previews are PNG sequences: the review hashes every frame')
    if engine == 'BLENDER_WORKBENCH':
        samples = None
        if 'samples' in opts:
            model.fail('CINEMATIC_OPTION_UNSUPPORTED', 'Workbench has no sample count')
    elif 'samples' in opts:
        samples, prov['samples'] = model.number(opts['samples'], 'samples', 1, 65536, integer=True), USER
    else:
        samples, origin, note = PRESET_SAMPLES[(engine, preset)]
        prov['samples'] = origin
        warnings.append({'code': 'SAMPLES_FROM_PRESET', 'detail': str(samples) + ' samples: ' + note})
    if engine == 'CYCLES':
        compute = backend.get('compute_device_type')
        optix = [d for d in backend.get('devices') or [] if len(d) == 3 and d[1] == 'OPTIX' and d[2]]
        if compute != 'OPTIX' or not optix:
            model.fail('CINEMATIC_RENDER_OPTIX_REQUIRED', 'Cycles renders only on the GPU with OptiX (compute device '
                       + str(compute) + ', enabled OptiX devices ' + str(len(optix)) + '); enable OptiX in Preferences > '
                       'System or plan BLENDER_EEVEE; a CPU fallback is never planned')
    output = {'path': None, 'policy': 'reserved under the workspace by the render tool (new directory, never reused)'}
    if 'output_path' in opts:
        path = _output_path(opts['output_path'])
        output = {'path': path, 'policy': 'must not exist when the render starts; one subfolder per stage',
                  'stages': {'PREVIEW': str(Path(path) / 'preview')}}
        if expensive:
            output['stages']['FINAL'] = str(Path(path) / 'final')
        prov['output_path'] = USER
    else:
        prov['output_path'] = DEFAULT
    if preset == 'PREVIEW' and not expensive:
        stages = [_stage('PREVIEW', engine, resolution, None, 'PNG_SEQUENCE', ranges, backend,
                         'FULL_TIMELINE' if ranges == full else 'PARTIAL')]
        if ranges != full:
            warnings.append({'code': 'PREVIEW_PARTIAL', 'detail': 'the review requires a preview of the whole timeline'})
    else:
        stages = [_stage('PREVIEW', 'BLENDER_WORKBENCH', preview_resolution, None, 'PNG_SEQUENCE', full, backend,
                         'FULL_TIMELINE'),
                  _stage('FINAL', engine, resolution, samples, fmt, ranges, backend, 'EXPLICIT')]
        if not expensive:
            stages = stages[1:] if ranges == full else stages
    totals = [s['estimate']['seconds'] for s in stages]
    measured = all(isinstance(t, list) for t in totals)
    return {'schema': RENDER_PLAN_SCHEMA, 'status': 'plan', 'rendered': False, 'reference': reference,
            'timeline': {k: timeline[k] for k in ('fps', 'fps_base', 'frame_start', 'frame_end')},
            'quality_preset': preset, 'stages': stages,
            'workload': {'images': sum(s['images'] for s in stages),
                         'megapixels': _r(sum(s['megapixels'] for s in stages), 3),
                         'seconds': [_r(sum(t[0] for t in totals), 3), _r(sum(t[1] for t in totals), 3)] if measured
                         else 'NOT_MEASURED'},
            'output': output, 'backend': backend, 'rules': {
                'preview_first': 'a Workbench preview of the whole timeline precedes any EEVEE/Cycles stage',
                'cycles': 'GPU OptiX only; no CPU fallback', 'call_budget_s': PROPOSALS['render_call_budget_s']},
            'warnings': warnings, 'provenance': prov, 'provenance_groups': _groups(prov)}
