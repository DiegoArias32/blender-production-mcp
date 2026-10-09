import ast

import pytest

from blender_mcp.production.common.bridge import build_code
from blender_mcp.production.physics import service,procedural,colliders,render,qa,cloth

@pytest.mark.parametrize('payload',[procedural.SAMPLES,procedural.WRITE,procedural.SOFT_SETUP,procedural.MARK,
    colliders.PREPARE,render.PAYLOAD,service.BAKE_RUNTIME+'\nresult=pb_clone(params)',
    service.BAKE_RUNTIME+'\nresult=pb_collect(params)',qa.RUNTIME+cloth.RUNTIME+'\nresult=pc_simulate(params)'],
    ids=['driver','write','soft','mark','proxy','render','clone','collect','cloth'])
def test_actual_stage_payloads_preserve_upstream_safety_budget(payload):
    code=build_code(payload,{'untrusted_label':'"; __import__("os").system("bad") #'})
    assert len(code.encode())<140000
    assert len(list(ast.walk(ast.parse(code))))<14000
    tree=ast.parse(code)
    assert not any(isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id in {'eval','exec','open','__import__'} for n in ast.walk(tree))
