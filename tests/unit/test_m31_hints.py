"""AUD-016: MCP hints must describe what each tool's Blender payload actually does.

Every tool is called with a recording bridge; the first payload it would send is linked exactly as
the real bridge links it and searched for state changes. A tool advertised as readOnlyHint=true must
send none; a tool that changes the frame, Action assignment or pose transiently must say so.
"""
import asyncio
import inspect
import re
import threading

import pytest

from blender_mcp.production import server
from blender_mcp.production.common.payload import link_payload
from blender_mcp.production.common.storage import Storage

FRAME = re.compile(r'frame_set\(|m3_set_frame\(|m2_set_frame\(')
ASSIGN = re.compile(r'animation_data\.action\s*=(?!=)|m2_assign\(|m3_assign\(|mv_assign\(|\.action_slot\s*=(?!=)')
POSE = re.compile(r'\.rotation_quaternion\s*=(?!=)|\.location\s*=(?!=)|pose_position\s*=(?!=)')
ARGUMENTS = {'object_name': 'Rig', 'armature': 'Rig', 'action_name': 'A', 'before_action': 'A', 'after_action': 'B',
             'checkpoint': 'x', 'description': 'd', 'name': 'N', 'options': {}, 'operation_id': 'op-00000001'}


class Captured(Exception):
    pass


class Recorder:
    def __init__(self):
        self.bodies = []
        self.lock = threading.RLock()

    def run(self, body, params=None):
        self.bodies.append(body)
        raise Captured()


def first_payload_effects(name, tmp_path, monkeypatch):
    function = server.mcp._tool_manager.get_tool(name).fn.__wrapped__
    parameters = inspect.signature(function).parameters
    kwargs = {k: v for k, v in ARGUMENTS.items() if k in parameters}
    for key, parameter in parameters.items():
        if parameter.default is inspect.Parameter.empty and key not in kwargs and not key.startswith('_'):
            kwargs[key] = 'X'
    recorder = Recorder()
    monkeypatch.setattr(server, '_bridge', recorder)
    monkeypatch.setattr(server, '_storage', Storage(tmp_path))
    if name in {'production.physics_status','production.physics_verify_primary_integrity',
                'production.physics_analyze_motion','production.physics_analyze_penetration',
                'production.physics_analyze_stability','production.physics_collision_report'}:
        # These high-level probes require an immutable scoped physics plan. Supply
        # that prerequisite so the observer reaches the actual restoring payload.
        from blender_mcp.production.animation.generation import store
        from blender_mcp.production.physics.service import backend_fingerprint, STATES
        reference,_=store.save(server._storage,'hint-probe',{'backend_fingerprint':backend_fingerprint(),
            'params':{'source_object':'Rig'},'object_name':'Rig'},'physics',STATES)
        kwargs['physics']=reference
    try:
        function(**kwargs, _operation_id='probe')
    except Exception:
        pass
    effects = set()
    for body in recorder.bodies:
        linked = link_payload(body)
        effects |= {label for label, pattern in (('frame', FRAME), ('assign', ASSIGN), ('pose', POSE)) if pattern.search(linked)}
    return effects, len(recorder.bodies)


TOOLS = sorted(asyncio.run(server.mcp.list_tools()), key=lambda t: t.name)


@pytest.mark.parametrize('listed', [t for t in TOOLS if t.annotations.readOnlyHint], ids=lambda t: t.name)
def test_read_only_tools_send_no_state_change(listed, tmp_path, monkeypatch):
    effects, _ = first_payload_effects(listed.name, tmp_path, monkeypatch)
    assert effects == set(), listed.name + ' is advertised read-only but its payload changes ' + str(effects)


@pytest.mark.parametrize('name', sorted(server.TRANSIENT_READERS))
def test_transient_readers_are_not_advertised_read_only(name, tmp_path, monkeypatch):
    listed = next(t for t in TOOLS if t.name == name)
    effects, payloads = first_payload_effects(name, tmp_path, monkeypatch)
    assert payloads and effects, name + ' no longer changes state; move it back to the read-only set'
    assert listed.annotations.readOnlyHint is False and listed.annotations.idempotentHint is True
    assert listed.meta['production']['scene_effects'] == 'transient_restored'
    assert 'operation_id' not in listed.inputSchema['properties']


def test_dual_backend_tools_label_their_schema(monkeypatch):
    from blender_mcp.production.animation import tools as animation_tools
    import blender_mcp.production.contacts.service as contact_service
    monkeypatch.setattr(animation_tools, 'run_analysis', lambda *a, **k: {'status': 'success'})
    monkeypatch.setattr(contact_service, 'analyze_contacts', lambda *a, **k: {'status': 'success'})
    monkeypatch.setattr(contact_service, 'analyze_limbs', lambda *a, **k: {'status': 'success'})
    monkeypatch.setattr(server, '_bridge', Recorder())
    contacts = server.mcp._tool_manager.get_tool('production.animation_analyze_contacts').fn.__wrapped__
    motion = server.mcp._tool_manager.get_tool('production.animation_analyze_motion_quality').fn.__wrapped__
    assert contacts('Rig', None, _operation_id='x')['schema'] == 'production.contacts.ground_plane.v1'
    assert contacts('Rig', {'feet': [], 'surfaces': []}, _operation_id='x')['schema'] == 'production.contacts.surfaces.v1'
    assert motion('Rig', None, _operation_id='x')['schema'] == 'production.motion_quality.rig.v1'
    assert motion('Rig', {'chains': []}, _operation_id='x')['schema'] == 'production.motion_quality.limb_chains.v1'
