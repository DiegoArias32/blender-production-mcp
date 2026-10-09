from blender_mcp.production.common.bridge import build_code
from blender_mcp.production.materials.inspection import INSPECT
from blender_mcp.safe_mode import MAX_CODE_BYTES


def test_installed_safe_mode_accepts_complete_linked_inspector():
    code=build_code(INSPECT,{'object_name':'Fixture','material_name':'Material','options':{}})
    assert len(code.encode())<MAX_CODE_BYTES
    assert 'reload(' not in code and 'bpy.ops' not in code and 'frame_set(' not in code


def test_untrusted_name_is_json_data_never_code():
    dangerous='name\"); __import__(\"os\").system(\"x\"); #'
    code=build_code(INSPECT,{'object_name':dangerous,'material_name':'M','options':{}})
    assert 'json.loads(' in code
