"""Full temporal M6 previews using the inherited restoring multiview payload.

M2/M3 public multiview bounds are unchanged. M6 has its own explicit budget:
601 frames/view, 1830 images, 200 million pixels, three views and 10 seconds.
"""
from __future__ import annotations

import struct
import time
import zlib
import math
import re
from pathlib import Path

from ...qa.multiview import _PREFLIGHT, _RENDER, _options
from ...qa.capture import _verify_images
from ...checkpoint import create_checkpoint
from ...common.storage import write_json, sha256


def settings_for_temporal(options=None):
    values = dict(options or {})
    if set(values) & {'frame_start', 'frame_end', 'step', 'max_frames', 'montage'}:
        raise ValueError('TEMPORAL_WINDOW_FIXED: M6 reviews every frame of the complete brief; use inherited QA for still samples')
    custom = values.pop('custom_views', [])
    settings = _options(dict(width=values.pop('width', 256), height=values.pop('height', 256),
                             max_frames=1, montage=False, **values))
    if set(settings['views']) != {'FRONT', 'SIDE', 'THREE_QUARTER'}:
        raise ValueError('THREE_VIEWS_REQUIRED: full temporal QA requires FRONT, SIDE and THREE_QUARTER')
    if not isinstance(custom, list) or len(custom) > 2:
        raise ValueError('CUSTOM_VIEW_CAPACITY: at most two additional explicit camera directions')
    directions = {}
    for view in custom:
        if not isinstance(view, dict) or set(view) != {'name', 'direction'}:
            raise ValueError('CUSTOM_VIEW_INVALID: provide name and direction')
        name, vector = view['name'], view['direction']
        if not isinstance(name, str) or not re.fullmatch(r'CUSTOM_[A-Z][A-Z0-9_]{0,31}', name) or name in directions:
            raise ValueError('CUSTOM_VIEW_INVALID: unique CUSTOM_ name required')
        if (not isinstance(vector, list) or len(vector) != 3 or
                any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in vector) or
                sum(x*x for x in vector) <= 1e-12):
            raise ValueError('CUSTOM_VIEW_INVALID: finite nonzero three-vector required')
        directions[name] = vector
    settings['views'] += list(directions)
    settings['custom_view_directions'] = directions
    return settings


# Keep the inherited restoring renderer intact; this adapter adds explicit
# directions to its existing camera-selection expression only.
_TEMPORAL_RENDER = _RENDER.replace('directions[view]',
    "params.get('custom_view_directions', {}).get(view, directions.get(view))")

# Validate the actual selected Action before checkpoint or camera mutations.
# A full brief preview must never silently omit authored keys outside it.
_TEMPORAL_PREFLIGHT = _PREFLIGHT + r'''
import math
if action is None:
    raise ValueError('CANDIDATE_MISSING: temporal QA requires a retained Action')
if hasattr(action,'is_action_layered') and action.is_action_layered:
    curves=[curve for layer in action.layers for strip in layer.strips
            for bag in strip.channelbags if slot and bag.slot_handle==slot.handle for curve in bag.fcurves]
else:
    curves=list(action.fcurves)
minimum=float('inf');maximum=float('-inf');count=0
for curve in curves:
    for point in curve.keyframe_points:
        frame=float(point.co.x)
        if not math.isfinite(frame) or frame<params['brief_start']-1e-6 or frame>params['brief_end']+1e-6:
            raise ValueError('TEMPORAL_ACTION_WINDOW_MISMATCH: authored keys outside the complete brief')
        minimum=min(minimum,frame);maximum=max(maximum,frame);count+=1
if count==0:
    raise ValueError('TEMPORAL_ACTION_WINDOW_MISMATCH: no authored candidate keys')
result['action_keyframe_range']=[minimum,maximum]
result['action_keyframe_count']=count
'''


def _chunk(kind, value):
    return struct.pack('>I', len(value)) + kind + value + struct.pack('>I', zlib.crc32(kind+value) & 0xffffffff)


def write_apng(files, output, fps):
    """Losslessly package Blender PNG frames; never alter rendered pixels."""
    chunks = []
    ihdr = None
    sequence = 0
    for index, item in enumerate(files):
        raw = Path(item['path']).read_bytes()
        if raw[:8] != b'\x89PNG\r\n\x1a\n':
            raise ValueError('PNG_REQUIRED: invalid temporal frame')
        offset, ids, auxiliaries = 8, [], []
        header = None
        while offset < len(raw):
            length = struct.unpack('>I', raw[offset:offset+4])[0]
            kind = raw[offset+4:offset+8]
            data = raw[offset+8:offset+8+length]
            if kind == b'IHDR': header = data
            elif kind == b'IDAT': ids.append(data)
            elif kind not in {b'IEND', b'acTL', b'fcTL', b'fdAT'}: auxiliaries.append((kind, data))
            offset += 12+length
        if index == 0:
            ihdr = header
            chunks.append(_chunk(b'IHDR', header))
            chunks.extend(_chunk(k, v) for k, v in auxiliaries)
            chunks.append(_chunk(b'acTL', struct.pack('>II', len(files), 0)))
        elif header != ihdr:
            raise ValueError('FRAME_FORMAT_CHANGED: APNG dimensions/color format differ')
        width, height = struct.unpack('>II', ihdr[:8])
        control = struct.pack('>IIIIIHHBB', sequence, width, height, 0, 0, 1, round(fps), 0, 0)
        chunks.append(_chunk(b'fcTL', control)); sequence += 1
        for data in ids:
            if index == 0: chunks.append(_chunk(b'IDAT', data))
            else:
                chunks.append(_chunk(b'fdAT', struct.pack('>I', sequence)+data)); sequence += 1
    chunks.append(_chunk(b'IEND', b''))
    with Path(output).open('xb') as target:
        target.write(b'\x89PNG\r\n\x1a\n' + b''.join(chunks))


def temporal(bridge, storage, operation_id, plan, action_name, options=None):
    began = time.perf_counter()
    brief = plan['brief']
    settings = settings_for_temporal(options)
    start, end = brief['frame_start'], brief['frame_end']
    frames = list(range(start, end+1))
    images = len(frames)*len(settings['views'])
    if len(frames) > 601 or images > 1830 or images*settings['width']*settings['height'] > 200_000_000:
        raise ValueError('CAPACITY_EXCEEDED: M6 temporal preview exceeds frame/image/pixel budget')
    selections = [{'label': 'candidate', 'action': action_name, 'slot': settings['action_slot']}]
    preflight = bridge.run(_TEMPORAL_PREFLIGHT, {'object_name': plan['object_name'], 'selections': selections,
                                               'brief_start': start, 'brief_end': end})
    if preflight['fps']/preflight['fps_base'] != brief['fps']:
        raise ValueError('STALE_RIG: scene FPS differs from animation brief')
    checkpoint = create_checkpoint(bridge, storage, 'Before M6 temporal preview', operation_id, 'M6 temporal QA')['manifest']
    folder = storage.reserve('generation-qa', operation_id)
    dirs = {'candidate': {}}
    for view in settings['views']:
        directory = folder / view
        directory.mkdir()
        if len(str(directory / 'frame_000001.png')) >= 260:
            raise ValueError('OUTPUT_PATH_TOO_LONG: use a shorter production artifact store')
        dirs['candidate'][view] = str(directory)
    params = dict(settings, object_name=plan['object_name'], operation_id=operation_id,
                  actions=preflight['actions'], frames=frames, step=1, directories=dirs)
    write_json(folder / 'intent.json', params)
    result = bridge.run(_TEMPORAL_RENDER, params)
    result.update(output_dir=str(folder), checkpoint=checkpoint, temporal_complete=False,
                  candidate_action=action_name, animations=[])
    if result.get('status') == 'ok' and result.get('state_restored'):
        for series in result['series']:
            verified = _verify_images({'status': 'ok', 'files': series['files'], 'errors': []}, Path(dirs['candidate'][series['view']]))
            if verified['status'] != 'ok' or [f['frame'] for f in series['files']] != frames:
                raise ValueError('TEMPORAL_ARTIFACT_INVALID: incomplete temporal sequence')
            animation = folder / (series['view'] + '.png')
            write_apng(series['files'], animation, brief['fps'])
            result['animations'].append({'view': series['view'], 'path': str(animation), 'sha256': sha256(animation),
                                         'frames': len(frames), 'fps': brief['fps']})
        result['temporal_complete'] = len(result['series']) == len(settings['views']) and len(result['files']) == images
    result.setdefault('metrics', {})['elapsed_ms_m6'] = round((time.perf_counter()-began)*1000, 2)
    write_json(folder / 'result.json', result)
    # A local browser can play real frame timing with no external resources.
    write_player(result)
    return result


def write_player(result):
    import json
    folder = Path(result['output_dir'])
    frames = result['timing']['frames']
    fps = result['timing']['fps']/result['timing']['fps_base']
    html = '''<!doctype html><meta charset="utf-8"><title>M6 temporal QA</title>
<style>body{background:#222;color:#eee;font:16px sans-serif}img{width:32%}h1{font-size:20px}button,input,select{font:16px sans-serif;margin:8px;padding:8px}</style>
<h1>M6 — Revisión temporal de la candidata</h1>
<button id="play">Pausar</button><button id="prev">Anterior</button><button id="next">Siguiente</button>
<label>Frame <input id="frame" type="number"></label>
<label>Velocidad <select id="speed"><option value="1">Normal</option><option value="0.25">Un cuarto</option></select></label>
<p id="position"></p>'''
    for row in result['animations']:
        html += '<img alt="'+row['view']+'" data-view="'+row['view']+'">'
    html += '<script>const frames='+json.dumps(frames)+',fps='+json.dumps(fps)+''';let at=0,playing=true,last=null,elapsed=0;
const input=document.querySelector('#frame'),button=document.querySelector('#play');
input.min=frames[0];input.max=frames[frames.length-1];
function show(){input.value=frames[at];document.querySelector('#position').textContent='Frame '+frames[at]+' · '+((frames[at]-frames[0])/fps).toFixed(3)+' s';
document.querySelectorAll('img').forEach(img=>img.src=img.dataset.view+'/frame_'+String(frames[at]).padStart(6,'0')+'.png');}
function pause(){playing=false;last=null;elapsed=0;button.textContent='Reproducir';}
button.onclick=()=>{playing=!playing;last=null;elapsed=0;button.textContent=playing?'Pausar':'Reproducir';};
document.querySelector('#prev').onclick=()=>{pause();at=Math.max(0,at-1);show();};
document.querySelector('#next').onclick=()=>{pause();at=Math.min(frames.length-1,at+1);show();};
input.oninput=()=>{pause();at=Math.max(0,frames.indexOf(Number(input.value)));show();};
function tick(now){const dt=last===null?0:Math.max(0,now-last);last=now;
if(playing){elapsed+=dt*Number(document.querySelector('#speed').value);const steps=Math.floor(elapsed*fps/1000);
if(steps){at=(at+steps)%frames.length;elapsed-=steps*1000/fps;show();}}else{elapsed=0;}
requestAnimationFrame(tick);}show();requestAnimationFrame(tick);
</script>'''
    (folder/'preview.html').write_text(html, encoding='utf-8')
