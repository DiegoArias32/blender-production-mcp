"""Small host adapters; geometry/pose evaluation stays inside Blender."""
from __future__ import annotations
from ..common.m3_transaction import BASE_RUNTIME, transaction
from .runtime import CONTACT_RUNTIME
from .correction_runtime import FOOT_RUNTIME

OPTION_NAMES={'frame_start','frame_end','step','sample_step','feet','surfaces','chains','bone','point','generic',
    'surface_orientation','support_direction','ambiguity_margin_m','max_penetration_m','minimum_contact_samples',
    'query_distance_m','minimum_up_normal','contact_height_m','normal_speed_m_s','contact_speed_m_s','minimum_dwell_frames',
    'offset','limit','detail','allow_stretch','stretch_tolerance_ratio','joint_step_degrees','bend_plane_step_degrees','context_ratio',
    'confirm_contacts','blend_frames','phase_blend_frames','position_tolerance_m','angular_tolerance_degrees','protected_tolerance_m',
    'protected_angular_tolerance_degrees','protected_scale_tolerance',
    'max_acceleration_increase_m_s2','max_velocity_increase_m_s','max_angular_acceleration_increase_deg_s2',
    'max_joint_step_increase_degrees','max_bend_plane_step_increase_degrees','max_knee_trajectory_change_m',
    'stretch_tolerance_m','sliding_tolerance_m'}


def validate_options(options):
    from ..animation.corrections import _validate_options
    options={} if options is None else options
    _validate_options(options)
    if set(options)-OPTION_NAMES: raise ValueError('Unknown contact options: '+', '.join(sorted(set(options)-OPTION_NAMES)))
    for key in ('confirm_contacts','allow_stretch','generic'):
        if key in options and type(options[key]) is not bool: raise ValueError(key+' must be boolean')
    if options.get('detail','summary') not in ('summary','samples'): raise ValueError('detail must be summary or samples')
    if options.get('surface_orientation','two_sided') not in ('two_sided','as_modeled'): raise ValueError('surface_orientation must be two_sided or as_modeled')
    direction=options.get('support_direction',[0,0,1])
    if not isinstance(direction,list) or len(direction)!=3 or any(isinstance(v,bool) or not isinstance(v,(int,float)) for v in direction): raise ValueError('support_direction must be three numbers')
    if 'minimum_contact_samples' in options and (type(options['minimum_contact_samples']) is not int or not 2<=options['minimum_contact_samples']<=1000): raise ValueError('minimum_contact_samples must be an integer 2..1000')
    if not .125<=float(options.get('sample_step',.25))<=1: raise ValueError('sample_step must be .125..1 frames')
    if not .25<=float(options.get('phase_blend_frames',1))<=10: raise ValueError('phase_blend_frames must be .25..10')
    if not .25<=float(options.get('blend_frames',3))<=30: raise ValueError('blend_frames must be .25..30')
    return options


def _run(bridge,object_name,options,expression):
    return bridge.run(BASE_RUNTIME+CONTACT_RUNTIME+"\nobj=m2_object(params['object_name'])\noptions=params['options']\nresult="+expression+'\n',
        {'object_name':object_name,'options':validate_options(options)})


def analyze_contacts(bridge,object_name,options=None):
    return _run(bridge,object_name,options,'contact_summary(contact_sample(obj,options),options)')


def analyze_limbs(bridge,object_name,options=None):
    return _run(bridge,object_name,options,'contact_limb_report(obj,options)')


def analyze_effector(bridge,object_name,options=None):
    return _run(bridge,object_name,options,'contact_effector(obj,options)')


def detect_surface(bridge,object_name,options=None):
    body=BASE_RUNTIME+CONTACT_RUNTIME+r'''
obj=m2_object(params['object_name']);options=params['options']
bone=obj.pose.bones.get(options.get('bone','')) if obj.type=='ARMATURE' else None
if bone is None: raise ValueError('Explicit effector bone required')
prod_require_evaluable([obj],['effector armature'])
evaluated=obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
point=evaluated.matrix_world@evaluated.pose.bones[bone.name].matrix@Vector(options.get('point',[0,0,0]))*bpy.context.scene.unit_settings.scale_length
hit=contact_query(point,contact_static_surfaces(options.get('surfaces',[])),options,options.get('generic',False))
result={'effector':bone.name,'frame':bpy.context.scene.frame_current+bpy.context.scene.frame_subframe,'contact':hit,
    'warnings':['A nearest surface is a candidate, not proof of contact intent. Static evaluated meshes only.']}
'''
    return bridge.run(body,{'object_name':object_name,'options':validate_options(options)})


def fix_contacts(bridge,storage,operation_id,object_name=None,mode='analyze',options=None,operation=None):
    return transaction(bridge,storage,operation_id,'advanced_foot_sliding',FOOT_RUNTIME,object_name,mode,validate_options(options),operation)


def register(tool,resources):
    @tool('production.animation_analyze_foot_phase',read_only=True)
    def phases(object_name:str,options:dict|None=None,*,_operation_id='')->dict:
        """Heel/sole/toe phases against explicit static surfaces; geometric heuristics with physical metrics."""
        return analyze_contacts(resources()[0],object_name,options)

    @tool('production.animation_detect_contact_surface',read_only=True)
    def surface(object_name:str,options:dict|None=None,*,_operation_id='')->dict:
        """Nearest evaluated static mesh surface at an explicit local effector marker."""
        return detect_surface(resources()[0],object_name,options)

    @tool('production.animation_analyze_effector_contact',read_only=True)
    def effector(object_name:str,options:dict|None=None,*,_operation_id='')->dict:
        """Generic effector dwell/contact/sliding/penetration estimates; proximity alone is insufficient."""
        return analyze_effector(resources()[0],object_name,options)

    @tool('production.animation_detect_limb_pops',read_only=True)
    def pops(object_name:str,options:dict|None=None,*,_operation_id='')->dict:
        """Explicit upper/lower/effector chains; contextual angle/velocity/acceleration/extension events."""
        return analyze_limbs(resources()[0],object_name,options)

    @tool('production.animation_detect_limb_stretch',read_only=True)
    def stretch(object_name:str,options:dict|None=None,*,_operation_id='')->dict:
        """Evaluated lengths versus rest lengths; reports configured intentional stretch separately."""
        return analyze_limbs(resources()[0],object_name,options)

    @tool('production.animation_analyze_bezier',read_only=True)
    def bezier(object_name:str,options:dict|None=None,*,_operation_id='')->dict:
        """Handle overshoot and contextual tangent findings; fast motion is not automatically a spike."""
        from ..animation.bezier import analyze_bezier
        return analyze_bezier(resources()[0],object_name,options)

    @tool('production.animation_repair_bezier')
    def repair(object_name:str|None=None,mode:str='analyze',options:dict|None=None,operation:str|None=None,*,_operation_id='')->dict:
        """Reviewed-channel handle repair on duplicate; immutable proposal and dense motion/derivative bounds."""
        from ..animation.bezier import repair_bezier
        bridge,storage=resources()
        return repair_bezier(bridge,storage,_operation_id,object_name,mode,options,operation)
