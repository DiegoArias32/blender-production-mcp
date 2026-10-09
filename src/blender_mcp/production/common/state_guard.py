"""Centralized Blender global-state preconditions (M3.1, audit AUD-004/AUD-007/AUD-023).

Global UI state must never define mathematical validation. Every template that evaluates
animation or mutates data declares the state it requires; the same state participates in
proposal fingerprints so a change after proposal makes the proposal stale.

Error messages start with an UPPER_SNAKE code so the MCP envelope can expose a structured
precondition code (see ``server.precondition_code``).
"""

STATE_RUNTIME = r'''
PROD_EVALUATION_MODES = ('OBJECT', 'POSE')

def prod_fail(code, message):
    raise ValueError(code + ': ' + message)

def prod_require_pose_position(obj, role='target'):
    # Rest Position evaluates every pose as the bind pose: sampling, baking or verifying in it
    # compares rest against rest and certifies nothing.
    if obj is not None and obj.type == 'ARMATURE' and obj.data.pose_position != 'POSE':
        prod_fail('POSE_POSITION_REQUIRED', 'armature "' + obj.name + '" (' + role + ') is evaluated in '
                  + obj.data.pose_position + ' position; switch it to Pose Position before animation evaluation or mutation')

def prod_require_mode(allowed=PROD_EVALUATION_MODES):
    mode = bpy.context.mode
    if mode not in allowed:
        prod_fail('MODE_NOT_SUPPORTED', 'Blender is in ' + mode + ' mode; this capability requires one of '
                  + ', '.join(allowed) + ' and never changes mode automatically')

def prod_require_evaluable(objects, roles=None):
    prod_require_mode()
    for index, obj in enumerate(objects):
        prod_require_pose_position(obj, roles[index] if roles else 'target')

def prod_global_state(objects):
    """State that changes evaluation without touching Action data; part of every proposal fingerprint."""
    scene = bpy.context.scene
    units = scene.unit_settings
    state = {'scene': scene.name, 'view_layer': bpy.context.view_layer.name,
             'fps': scene.render.fps, 'fps_base': scene.render.fps_base,
             'frame_range': [scene.frame_start, scene.frame_end],
             'units': {'system': units.system, 'scale_length': units.scale_length, 'length_unit': units.length_unit},
             'objects': {}}
    for obj in objects:
        if obj is None:
            continue
        row = {'type': obj.type, 'hide_viewport': obj.hide_viewport, 'matrix_world': [list(r) for r in obj.matrix_world]}
        if obj.type == 'ARMATURE':
            row['pose_position'] = obj.data.pose_position
            row['ik_solver'] = obj.pose.ik_solver
            row['local_location'] = sorted(b.name for b in obj.data.bones if not b.use_local_location)
        row['modifiers'] = [[m.name, m.type, m.show_viewport] for m in obj.modifiers]
        state['objects'][obj.name] = row
    return state

def prod_transient_capture():
    """Execution context a tool may touch transiently: restored even when the operation fails."""
    scene = bpy.context.scene
    layer = bpy.context.view_layer
    return {'frame': scene.frame_current, 'subframe': scene.frame_subframe, 'mode': bpy.context.mode,
            'active': layer.objects.active.name if layer.objects.active else None,
            'selected': sorted(o.name for o in scene.objects if o.select_get()),
            'pose_positions': {o.name: o.data.pose_position for o in scene.objects if o.type == 'ARMATURE'}}

def prod_transient_restore(state):
    scene = bpy.context.scene
    for name, value in state['pose_positions'].items():
        obj = scene.objects.get(name)
        if obj is not None and obj.data.pose_position != value:
            obj.data.pose_position = value
    scene.frame_set(state['frame'], subframe=state['subframe'])

def prod_transient_diff(before, after):
    return sorted(key for key in before if before[key] != after.get(key))
'''
