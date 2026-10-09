"""Execute the actual generated player script under Node with controlled RAF time.

The clock oracle uses wall time, never the number of RAF callbacks. These tests
do not certify browser image decoding, display refresh or artistic quality.
"""
from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
from unittest.mock import Mock

import pytest

from blender_mcp.production.animation.generation import render


@pytest.fixture
def node():
    executable = shutil.which("node")
    if executable is None:
        pytest.skip("Node.js unavailable; actual generated JavaScript clock was not tested")
    return executable


def execute_player(node, tmp_path, fps, speed, times, pause=False, pause_without_callbacks=False):
    frames = list(range(1, 602))
    render.write_player({"output_dir": str(tmp_path),
                         "timing": {"frames": frames, "fps": fps, "fps_base": 1.},
                         "animations": [{"view": view} for view in ("FRONT", "SIDE", "THREE_QUARTER")]})
    html = (tmp_path / "preview.html").read_text(encoding="utf-8")
    script = re.search(r"<script>(.*?)</script>", html, re.DOTALL).group(1)
    expected = [frames[math.floor(t * fps * speed / 1000) % len(frames)] for t in times]
    harness = r'''
const vm=require('node:vm'), assert=require('node:assert/strict');
const source=SOURCE, times=TIMES, expected=EXPECTED;
const elements={
 '#frame':{value:0}, '#play':{textContent:''}, '#prev':{}, '#next':{},
 '#position':{textContent:''}, '#speed':{value:String(SPEED)}
};
const images=['FRONT','SIDE','THREE_QUARTER'].map(view=>({dataset:{view},src:''}));
let pending=null;
const context=vm.createContext({document:{
 querySelector(selector){assert.ok(selector in elements);return elements[selector];},
 querySelectorAll(selector){assert.equal(selector,'img');return images;}
},requestAnimationFrame(callback){assert.equal(pending,null);pending=callback;}});
vm.runInContext(source,context);
function advance(now){const callback=pending;assert.equal(typeof callback,'function');pending=null;callback(now);}
const observed=[];
for(let i=0;i<times.length;i++){
 advance(times[i]);
 const frame=Number(elements['#frame'].value);observed.push(frame);
 assert.equal(frame,expected[i],JSON.stringify({fps:FPS,speed:SPEED,time:times[i],frame,expected:expected[i]}));
 images.forEach(image=>assert.equal(image.src,image.dataset.view+'/frame_'+String(frame).padStart(6,'0')+'.png'));
}
if(PAUSE){
 const before=Number(elements['#frame'].value);
 elements['#play'].onclick();
 if(!PAUSE_WITHOUT_CALLBACKS){advance(times[times.length-1]+4000);}
 assert.equal(Number(elements['#frame'].value),before,'paused wall time must not advance frames');
 elements['#play'].onclick();
 advance(times[times.length-1]+4010);
 assert.equal(Number(elements['#frame'].value),before,'resume must not catch up paused time');
 advance(times[times.length-1]+5010);
 assert.equal(Number(elements['#frame'].value),(before-1+Math.floor(1010*FPS*SPEED/1000))%601+1);
}
process.stdout.write(JSON.stringify({observed,sourceExecuted:true,images:images.length}));
'''
    replacements = {
        "SOURCE": json.dumps(script), "TIMES": json.dumps(times), "EXPECTED": json.dumps(expected),
        "SPEED": json.dumps(speed), "FPS": json.dumps(fps), "PAUSE": json.dumps(pause),
        "PAUSE_WITHOUT_CALLBACKS": json.dumps(pause_without_callbacks),
    }
    # Replace distinct uppercase placeholders only in the harness, before JS
    # source is inserted; no template operation touches the production script.
    harness = re.sub(r"\b(SOURCE|TIMES|EXPECTED|SPEED|FPS|PAUSE|PAUSE_WITHOUT_CALLBACKS)\b",
                     lambda match: replacements[match.group(0)], harness)
    completed = subprocess.run([node, "-"], input=harness, text=True, capture_output=True,
                               encoding="utf-8", errors="strict", timeout=10)
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["sourceExecuted"] is True
    assert result["observed"] == expected
    return result


@pytest.mark.parametrize("fps", [30, 60])
@pytest.mark.parametrize("speed", [1., .25])
@pytest.mark.parametrize("schedule", ["raf_60hz", "dropped_callbacks", "long_tab_lag"])
def test_actual_player_uses_elapsed_time_despite_refresh_and_missing_frames(node, tmp_path, fps, speed, schedule):
    if schedule == "raf_60hz":
        # Sample just past exact frame boundaries to avoid asking floating-point
        # arithmetic to distinguish an infinitesimal 1e-13 ms boundary error.
        times = [0.] + [index * 1000 / 60 + .125 for index in range(1, 301)]
    elif schedule == "dropped_callbacks":
        times = [0., 16.8, 50.1, 166.8, 1000.1, 5000.1]
    else:
        times = [0., 16.8, 1000.1, 60_000.1, 100_000.1]
    execute_player(node, tmp_path, fps, speed, times)


@pytest.mark.parametrize("fps", [30, 60])
def test_actual_player_pause_does_not_accumulate_paused_time(node, tmp_path, fps):
    execute_player(node, tmp_path, fps, 1., [0., 1010.1], pause=True)


@pytest.mark.parametrize("fps", [30, 60])
def test_actual_player_paused_tab_with_no_raf_resumes_without_time_jump(node, tmp_path, fps):
    execute_player(node, tmp_path, fps, 1., [0., 1010.1], pause=True, pause_without_callbacks=True)


@pytest.mark.parametrize("option", [
    {"frame_start": 2}, {"frame_end": 4}, {"step": 2}, {"max_frames": 1}, {"montage": True},
])
def test_partial_temporal_window_rejects_before_bridge_or_checkpoint(monkeypatch, option):
    bridge = Mock()
    checkpoint = Mock(side_effect=AssertionError("Checkpoint must not be created for an invalid render window"))
    monkeypatch.setattr(render, "create_checkpoint", checkpoint)
    with pytest.raises(ValueError, match="TEMPORAL_WINDOW_FIXED"):
        render.temporal(bridge, None, "invalid-window", {"brief": {}}, "Candidate", option)
    bridge.run.assert_not_called()
    checkpoint.assert_not_called()


@pytest.mark.parametrize("custom,marker", [
    ([{"name": "CUSTOM_REAR", "direction": [0, 1, 0]}] * 2, "CUSTOM_VIEW_INVALID"),
    ([{"name": "CUSTOM_A", "direction": [0, 1, 0]},
      {"name": "CUSTOM_B", "direction": [1, 0, 0]},
      {"name": "CUSTOM_C", "direction": [0, -1, 0]}], "CUSTOM_VIEW_CAPACITY"),
])
def test_duplicate_or_excess_custom_views_reject_before_blender(monkeypatch, custom, marker):
    bridge = Mock()
    checkpoint = Mock()
    monkeypatch.setattr(render, "create_checkpoint", checkpoint)
    with pytest.raises(ValueError, match=marker):
        render.temporal(bridge, None, "invalid-views", {"brief": {}}, "Candidate", {"custom_views": custom})
    bridge.run.assert_not_called()
    checkpoint.assert_not_called()
