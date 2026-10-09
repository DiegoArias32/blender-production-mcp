"""Four finite M8-heavy-cloth workloads; no GUI, socket, external asset or new dependency."""
from copy import deepcopy
from datetime import datetime,timezone
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import traceback
import uuid

REPO=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(REPO/'src'))
from blender_mcp.production.common.bridge import build_code,MARKER
from blender_mcp.production.common.storage import Storage,sha256
from blender_mcp.production.common.asset_safety import register_fixture
from blender_mcp.production.physics import service

OUT=Path(tempfile.gettempdir())/('bpm8perf-'+uuid.uuid4().hex[:8])
OUT.mkdir(exist_ok=False)
REPORT={'schema':'m8.heavy_cloth_performance.v1','output_directory':str(OUT),'workloads':[],'errors':[],
        'scope':'Four finite cloth625 workloads; preview/render benchmark belongs to coordinator',
        'backend_fingerprint':service.backend_fingerprint(),'preview_measurement':'NOT_RUN_IN_THIS_BENCHMARK',
        'cpu_benchmark':True,'no_socket':True,'no_production_assets':True}


def save():
    (OUT/'report.json').write_text(json.dumps(REPORT,indent=2,allow_nan=False),encoding='utf-8')


class Memory(ctypes.Structure):
    _fields_=[('cb',wintypes.DWORD),('PageFaultCount',wintypes.DWORD),
              ('PeakWorkingSetSize',ctypes.c_size_t),('WorkingSetSize',ctypes.c_size_t),
              ('QuotaPeakPagedPoolUsage',ctypes.c_size_t),('QuotaPagedPoolUsage',ctypes.c_size_t),
              ('QuotaPeakNonPagedPoolUsage',ctypes.c_size_t),('QuotaNonPagedPoolUsage',ctypes.c_size_t),
              ('PagefileUsage',ctypes.c_size_t),('PeakPagefileUsage',ctypes.c_size_t)]


def process_memory(pid):
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.OpenProcess.restype=wintypes.HANDLE
    handle=kernel.OpenProcess(0x1000|0x0010,False,pid)
    if not handle:
        return {'status':'UNAVAILABLE','error':ctypes.get_last_error()}
    try:
        counters=Memory();counters.cb=ctypes.sizeof(counters)
        psapi=ctypes.WinDLL('psapi',use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes=[wintypes.HANDLE,ctypes.POINTER(Memory),wintypes.DWORD]
        if not psapi.GetProcessMemoryInfo(handle,ctypes.byref(counters),counters.cb):
            return {'status':'UNAVAILABLE','error':ctypes.get_last_error()}
        return {'status':'MEASURED','working_set_bytes':counters.WorkingSetSize,
                'peak_working_set_bytes':counters.PeakWorkingSetSize,
                'pagefile_usage_bytes':counters.PagefileUsage}
    finally:
        kernel.CloseHandle.argtypes=[wintypes.HANDLE];kernel.CloseHandle(handle)


from m8_pipe_harness import PipeBridge


def timed(entry,name,fn):
    began=time.perf_counter();value=fn();entry[name+'_seconds']=time.perf_counter()-began
    return value


for fps,duration in ((30,2),(30,4),(60,2),(60,4)):
    label='heavy625_'+str(fps)+'fps_'+str(duration)+'s'
    folder=OUT/label;folder.mkdir()
    entry={'workload':label,'fps':fps,'duration_s':duration,'expected_vertices':625,
           'preview_seconds':None,'preview_status':'NOT_RUN_IN_THIS_BENCHMARK'}
    bridge=None
    try:
        bridge=PipeBridge(folder,fps,duration);fixture=bridge.fixture
        entry.update(blender_pid=bridge.process.pid,blender=fixture['blender'],vertices=fixture['source_vertices'],
                     frame_range=fixture['frame_range'],source_path=fixture['source_path'])
        storage=Storage(folder/'store')
        register_fixture(bridge,storage,fixture['source_path'],[str(folder/'fixtures')])
        source_hash=sha256(Path(fixture['source_path']))
        options={k:fixture[k] for k in ('manifest','primary_objects','colliders','pin_group','anchor_object','anchor_bone')}
        options['manifest']=fixture['manifest']
        planned=timed(entry,'plan',lambda:service.plan(bridge,storage,label+'-plan',fixture['source_object'],None,fixture['brief'],options))
        path,plan,state=service.load(storage,planned['physics'])
        state=timed(entry,'setup',lambda:service.setup_cloth(bridge,storage,path,plan,state,label+'-setup'))
        state=timed(entry,'simulate_and_qualify',lambda:service.simulate_cloth(bridge,storage,path,plan,state,label+'-simulate'))
        entry.update(technical_qa=deepcopy(state.get('technical_qa')),simulation=deepcopy(state.get('simulation')),
                     memory_after_simulation=process_memory(bridge.process.pid))
        if not state.get('technical_qa',{}).get('passed'):
            raise RuntimeError('M8_HEAVY_CLOTH_QUALIFICATION_FAILED')
        collision=timed(entry,'collision_qa',lambda:service.analyze(bridge,plan,state,'collision'))
        entry['collision_qa']=collision
        state=timed(entry,'cache',lambda:service.bake_cache(bridge,storage,path,plan,state,label+'-cache'))
        entry.update(cache=deepcopy(state.get('cache')),memory_after_cache=process_memory(bridge.process.pid),
                     source_sha256_before=source_hash,source_sha256_after=sha256(Path(fixture['source_path'])))
        entry['source_file_unchanged']=entry['source_sha256_before']==entry['source_sha256_after']
        entry['passed']=bool(entry['technical_qa']['passed'] and entry['collision_qa']['passed'] and entry['source_file_unchanged'])
    except Exception as exc:
        entry.update(passed=False,error=str(exc),traceback=traceback.format_exc())
        REPORT['errors'].append({'workload':label,'error':str(exc)})
    finally:
        if bridge:bridge.close()
        REPORT['workloads'].append(entry);save()
        print('M8_PERFORMANCE',label,entry.get('passed'),entry.get('error',''),flush=True)
REPORT['passed']=len(REPORT['workloads'])==4 and all(w.get('passed') for w in REPORT['workloads'])
REPORT['backend_unchanged']=REPORT['backend_fingerprint']==service.backend_fingerprint();save()
print('M8_PERFORMANCE_REPORT',str(OUT/'report.json'),REPORT['passed'],flush=True)
