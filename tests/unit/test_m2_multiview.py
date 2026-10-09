"""Multiview budgets, safe templates, artifact integrity and failure semantics."""
from pathlib import Path
import struct
from unittest.mock import patch
import zlib

import pytest

from blender_mcp.production.common.bridge import build_code
from blender_mcp.production.common.storage import Storage
from blender_mcp.production.qa import multiview
from blender_mcp.production.qa.png_montage import _decode, write_montage


def png(width=16, height=16, color=(15, 25, 40, 255)):
    def chunk(kind, data):
        return struct.pack(">I",len(data))+kind+data+struct.pack(">I",zlib.crc32(kind+data))
    return (b"\x89PNG\r\n\x1a\n"+chunk(b"IHDR",struct.pack(">IIBBBBB",width,height,8,6,0,0,0))
            +chunk(b"IDAT",zlib.compress((b"\0"+bytes(color)*width)*height))+chunk(b"IEND",b""))


class ArtifactBridge:
    def __init__(self, *, failure=None):
        self.calls = []
        self.failure = failure

    def run(self, body, params):
        build_code(body,params)
        self.calls.append((body,params))
        if body == multiview._PREFLIGHT:
            if self.failure == "preflight":
                raise ValueError("Active NLA/tweak mode is not supported")
            return {"object":params["object_name"],"frame_start":1,"frame_end":9,"fps":24,"fps_base":1.0,
                    "actions":[dict(s, action=s["action"] or "Original", slot="OBRig") for s in params["selections"]]}
        if self.failure == "transport":
            raise TimeoutError("Blender transport timed out")
        timing = {"frames":params["frames"],"fps":24,"fps_base":1.0}
        result = {"status":"ok","before":{"action":"Original"},"after":{"action":"Original"},
                  "state_restored":True,"files":[],"series":[],"cameras":{},"errors":[],"warnings":[],
                  "timing":timing,"metrics":{}}
        for view in params["views"]:
            result["cameras"][view] = {"location":[1,2,3],"ortho_scale":2}
        for selection in params["actions"]:
            for view in params["views"]:
                series = {"label":selection["label"],"view":view,"files":[],"status":"ok",
                          "camera":result["cameras"][view],"timing":timing,
                          "resolution":[params["width"],params["height"]]}
                for frame in params["frames"]:
                    path = Path(params["directories"][selection["label"]][view])/f"frame_{frame:06d}.png"
                    if self.failure != "missing_file":
                        with path.open("xb") as stream:
                            stream.write(png(params["width"],params["height"]))
                    item = {"path":str(path),"frame":frame}
                    series["files"].append(item)
                    result["files"].append(item)
                result["series"].append(series)
        if self.failure == "wrong_frame":
            result["series"][0]["files"][0]["frame"] = 1234
        if self.failure == "wrong_camera":
            result["series"][0]["camera"] = {"location":[10,20,30],"ortho_scale":100}
        return result


@pytest.fixture
def checkpoint():
    with patch.object(multiview,"create_checkpoint",return_value={"manifest":"safe/manifest.json"}) as stub:
        yield stub


def test_multiview_payloads_pass_upstream_safe_mode():
    assert build_code(multiview._PREFLIGHT,{})
    assert build_code(multiview._RENDER,{})


def test_comparison_uses_shared_cameras_frames_and_verified_pngs(tmp_path,checkpoint):
    bridge = ArtifactBridge()
    result = multiview.compare_before_after(bridge,Storage(tmp_path),"op","Rig","Original","Corrected",
                                            {"width":16,"height":16})
    assert result["status"] == "ok" and result["comparable"]
    assert result["timing"]["frames"] == [1,5,9]
    assert len(result["files"]) == 18
    assert result["state_restored"]
    assert result["checkpoint"]["manifest"] == "safe/manifest.json"
    assert Path(result["output_dir"],"before","FRONT").is_dir()
    assert Path(result["output_dir"],"after","THREE_QUARTER").is_dir()
    assert _decode(Path(result["montage"]["path"]))[:2] == (48,32)
    assert result["montage"]["rows"] == ["before","after"]
    checkpoint.assert_called_once()


def test_multiview_single_action_never_claims_before_after(tmp_path,checkpoint):
    result = multiview.render_multiview(ArtifactBridge(),Storage(tmp_path),"op","Rig",
                                       {"width":16,"height":16,"max_frames":1})
    assert result["status"] == "ok" and not result["comparable"]
    assert len(result["files"]) == 3
    assert result["montage"]["rows"] == ["preview"]


def test_repeat_operation_id_gets_distinct_output_directories(tmp_path,checkpoint):
    storage = Storage(tmp_path)
    args = (ArtifactBridge(),storage,"same-op","Rig")
    first = multiview.render_multiview(*args,{"width":16,"height":16})
    original = Path(first["montage"]["path"]).read_bytes()
    second = multiview.render_multiview(*args,{"width":16,"height":16})
    assert first["output_dir"] != second["output_dir"]
    assert Path(first["montage"]["path"]).read_bytes() == original


@pytest.mark.parametrize("options",[{"unknown":True},{"views":["TOP"]},{"views":["FRONT","FRONT"]},
                                      {"width":True},{"height":3000},{"step":0},{"max_frames":30},
                                      {"margin":float("nan")},{"quality":"cpu_cycles"},{"montage":"yes"},
                                      {"after_slot":15}])
def test_invalid_options_do_not_reach_blender(tmp_path,checkpoint,options):
    bridge = ArtifactBridge()
    with pytest.raises(ValueError):
        multiview.render_multiview(bridge,Storage(tmp_path),"op","Rig",options)
    assert not bridge.calls
    checkpoint.assert_not_called()


@pytest.mark.parametrize("options",[{"frame_start":9,"frame_end":1},{"step":1,"max_frames":3},
                                      {"frame_start":1,"frame_end":24,"max_frames":24,"step":1,
                                       "width":1920,"height":1920}])
def test_frame_and_pixel_budget_refused_before_checkpoint(tmp_path,checkpoint,options):
    with pytest.raises(ValueError):
        multiview.render_multiview(ArtifactBridge(),Storage(tmp_path),"op","Rig",options)
    checkpoint.assert_not_called()


def test_nla_refusal_occurs_before_checkpoint(tmp_path,checkpoint):
    with pytest.raises(ValueError,match="NLA"):
        multiview.render_multiview(ArtifactBridge(failure="preflight"),Storage(tmp_path),"op","Rig")
    checkpoint.assert_not_called()


def test_transport_timeout_is_unverified_and_not_retried(tmp_path,checkpoint):
    bridge = ArtifactBridge(failure="transport")
    result = multiview.render_multiview(bridge,Storage(tmp_path),"op","Rig")
    assert result["status"] == "error" and result["state_restored"] is None
    assert not result["comparable"] and result["checkpoint"]
    assert len(bridge.calls) == 2
    assert result["errors"][0]["type"] == "TimeoutError"
    assert Path(result["output_dir"],"intent.json").is_file()


@pytest.mark.parametrize("failure",["missing_file","wrong_frame","wrong_camera"])
def test_artifact_failure_never_claims_comparable(tmp_path,checkpoint,failure):
    result = multiview.compare_before_after(ArtifactBridge(failure=failure),Storage(tmp_path),"op","Rig",
                                            "Original","Corrected",{"width":16,"height":16})
    assert result["status"] == "error" and not result["comparable"]
    assert result["errors"]


def test_montage_preserves_pixels_and_refuses_overwrite(tmp_path):
    first,second,output = (tmp_path/name for name in ("first.png","second.png","out.png"))
    first.write_bytes(png(color=(255,0,0,255)))
    second.write_bytes(png(color=(0,0,255,255)))
    assert write_montage([[first,second]],output) == (32,16)
    _,_,rows = _decode(output)
    assert rows[0][:64] == bytes((255,0,0,255))*16
    assert rows[0][64:] == bytes((0,0,255,255))*16
    original = output.read_bytes()
    with pytest.raises(FileExistsError):
        write_montage([[second,first]],output)
    assert output.read_bytes() == original


def test_montage_rejects_corrupt_png(tmp_path):
    source = tmp_path/"corrupt.png"
    source.write_bytes(png()[:-5])
    with pytest.raises(ValueError):
        write_montage([[source]],tmp_path/"out.png")
    assert not (tmp_path/"out.png").exists()
