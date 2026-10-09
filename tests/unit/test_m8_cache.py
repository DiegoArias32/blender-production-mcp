import array
import json
import struct

import pytest

from blender_mcp.production.common.storage import Storage
from blender_mcp.production.physics.cache import write_pc2, verify


def samples(start=1):
    return [{'frame':start+i,'vertex_count':1,'hex':array.array('f',[0.,float(i),0.]).tobytes().hex()} for i in range(3)]


def test_pc2_exact_header_and_geometry_roundtrip_without_overwrite(tmp_path):
    store=Storage(tmp_path);dependencies={'body':'stable','face':'stable'}
    first=write_pc2(store,'cache-one',samples(-2),dependencies,-2)
    second=write_pc2(store,'cache-one',samples(-2),dependencies,-2)
    assert first['path']!=second['path']
    raw=__import__('pathlib').Path(first['path']).read_bytes()
    assert struct.unpack('<12siiffi',raw[:32])==(b'POINTCACHE2\0',1,1,-2.,1.,3)
    assert raw[32:]==b''.join(bytes.fromhex(s['hex']) for s in samples(-2))
    assert verify(store,first,dependencies)['status']=='FRESH'


@pytest.mark.parametrize('change', ['geometry','dependencies','manifest'])
def test_changed_cache_inputs_never_reuse_artifact(tmp_path,change):
    store=Storage(tmp_path);entry=write_pc2(store,'guard-test',samples(),{'body':'A'},1)
    from pathlib import Path
    dependencies={'body':'A'}
    if change=='geometry':Path(entry['path']).write_bytes(b'corrupt')
    elif change=='dependencies':dependencies={'body':'B'}
    else:
        path=Path(entry['manifest']);data=json.loads(path.read_text());data['frame_end']=999;path.write_text(json.dumps(data))
    with pytest.raises(ValueError,match='STALE_CACHE'):verify(store,entry,dependencies)


@pytest.mark.parametrize('change', ['gap','topology','nan','truncated','bool_frame'])
def test_invalid_samples_reject_before_reservation(tmp_path,change):
    store=Storage(tmp_path);frames=samples()
    if change=='gap':frames[1]['frame']=5
    elif change=='topology':frames[1]['vertex_count']=2
    elif change=='nan':frames[1]['hex']=array.array('f',[0.,float('nan'),0.]).tobytes().hex()
    elif change=='truncated':frames[1]['hex']='00'
    else:frames[0]['frame']=True
    with pytest.raises(ValueError):write_pc2(store,'invalid-samples',frames,{},1)
    assert not (tmp_path/'physics-cache').exists()


def test_cache_reference_outside_store_is_refused(tmp_path):
    other=Storage(tmp_path/'other');entry=write_pc2(other,'outside-test',samples(),{},1)
    with pytest.raises(ValueError,match='INVALID_CACHE_REFERENCE'):verify(Storage(tmp_path/'actual'),entry)
