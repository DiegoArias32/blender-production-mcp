import math

import pytest

from blender_mcp.production.physics.cloth import RUNTIME,validate_settings
from blender_mcp.production.physics.qa import RUNTIME as QA_RUNTIME
from blender_mcp.safe_mode import validate_code
from blender_mcp.production.common.bridge import build_code


def test_cloth_runtime_passes_unchanged_safe_mode_with_qa_helpers():
    for entry in ('pc_setup','pc_simulate','pc_collect','pc_playback'):
        validate_code(build_code(QA_RUNTIME+RUNTIME+'\nresult='+entry+'(params)\n',{}))
    assert 'open(' not in RUNTIME
    assert 'struct.' not in RUNTIME
    assert 'ptcache.bake_from_cache' in RUNTIME
    assert 'ptcache.bake(bake=True)' not in RUNTIME


def test_real_rna_settings_are_flat_and_explicit():
    settings={'mass':.3,'quality':5,'bending_stiffness':.5,
              'use_collision':True,'collision_quality':2,'use_self_collision':False}
    assert validate_settings(settings)==settings
    assert validate_settings(settings) is not settings


@pytest.mark.parametrize('key', ['substeps','stiffness','damping','collision','invented_property'])
def test_unverified_rna_setting_rejected(key):
    with pytest.raises(ValueError,match='UNSUPPORTED_CLOTH_SETTING'):
        validate_settings({key:1})


@pytest.mark.parametrize('value', [-.1,math.inf,math.nan,True,None,'1'])
def test_invalid_material_property_rejected(value):
    with pytest.raises(ValueError):
        validate_settings({'mass':value})


def test_zero_mass_and_nonboolean_collision_rejected():
    with pytest.raises(ValueError):
        validate_settings({'mass':0})
    with pytest.raises(ValueError):
        validate_settings({'use_collision':1})


@pytest.mark.parametrize('value', [0,13,10000,5.0])
def test_solver_quality_has_bounded_integer_cost(value):
    with pytest.raises(ValueError,match='CAPACITY'):
        validate_settings({'quality':value})


def test_collision_quality_has_same_cost_guard():
    assert validate_settings({'quality':12,'collision_quality':12})
    with pytest.raises(ValueError,match='CAPACITY'):
        validate_settings({'collision_quality':13})
