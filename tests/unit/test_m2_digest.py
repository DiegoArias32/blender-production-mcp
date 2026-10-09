import hashlib
import json
import pytest

from blender_mcp.production.common.digest import m2_sha256,DIGEST_RUNTIME,runtime_digest
from blender_mcp.production.common.operations import fingerprint
from blender_mcp.production.common.bridge import build_code


@pytest.mark.parametrize('payload',[b'',b'abc',b'a'*55,b'a'*56,b'a'*63,b'a'*64,b'a'*65,
                                    bytes(range(256)),b'abc'*10000,'Español 骨架'.encode()])
def test_blender_digest_matches_host_sha256(payload):
    assert m2_sha256(payload)==hashlib.sha256(payload).hexdigest()


def test_canonical_content_digest_roundtrips_through_json():
    data={'unicode':'acción','numbers':[1.234e-8,-0.0,99], 'empty':{},'flags':[True,None]}
    scope={}
    exec(build_code(DIGEST_RUNTIME+'\nresult={"digest":m2_digest(params)}',data),scope)
    # M3.1: runtime fingerprints use the fast integer digest on both sides of the socket.
    assert scope['result']['digest']==runtime_digest(data)


def test_snapshots_are_excluded_from_mutation_transport():
    from blender_mcp.production.animation.corrections import _runtime_params
    values={'source_snapshot':{'huge':True},'working_snapshot':{'foo':2},'context':{'rig':3},'source_fingerprint':'abc'}
    compact=_runtime_params(values)
    assert set(compact)=={'working_fingerprint','context_fingerprint','source_fingerprint'}
    assert compact['working_fingerprint']==runtime_digest(values['working_snapshot'])
