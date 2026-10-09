"""Cage witnesses must reach the corresponding surface on an oblique cage."""
import ast
import math
from types import SimpleNamespace

from blender_mcp.production.bake.runtime import BAKE_RUNTIME


class Vec:
    def __init__(self, xyz=(0, 0, 0)):
        self.xyz = tuple(xyz)

    def __add__(self, other):
        return Vec(a + b for a, b in zip(self.xyz, other.xyz))

    def __sub__(self, other):
        return Vec(a - b for a, b in zip(self.xyz, other.xyz))

    def __truediv__(self, value):
        return Vec(a / value for a in self.xyz)

    def dot(self, other):
        return sum(a * b for a, b in zip(self.xyz, other.xyz))

    @property
    def length_squared(self):
        return self.dot(self)

    @property
    def length(self):
        return math.sqrt(self.length_squared)

    def normalized(self):
        return self / self.length


class Identity:
    def __matmul__(self, vector):
        return vector


class Surface:
    """Analytical ray intersection with a finite triangle in the z=0 plane."""
    def __init__(self):
        self.calls = 0

    def ray_cast(self, origin, direction, limit):
        self.calls += 1
        x, y, z = origin.xyz
        dx, dy, dz = direction.xyz
        if dz == 0:
            return (None, None, None, None)
        distance = -z / dz
        hit_x, hit_y = x + distance * dx, y + distance * dy
        if 0 <= distance <= limit and hit_x >= 0 and hit_y >= 0 and hit_x + hit_y <= 1:
            return (Vec((hit_x, hit_y, 0)), Vec((0, 0, 1)), 0, distance)
        return (None, None, None, None)


def fixture(offset):
    points = [Vec((0, 0, 0)), Vec((1, 0, 0)), Vec((0, 1, 0))]
    cage_points = [p + Vec(offset) for p in points]

    def obj(values):
        return SimpleNamespace(
            matrix_world=Identity(),
            data=SimpleNamespace(
                vertices=[SimpleNamespace(co=p) for p in values],
                polygons=[SimpleNamespace(vertices=(0, 1, 2))],
            ),
        )

    low, cage, high = obj(points), obj(cage_points), object()
    surface = Surface()
    center = sum(points, Vec()) / 3
    namespace = {
        'bpy': SimpleNamespace(context=SimpleNamespace(scene=SimpleNamespace(
            unit_settings=SimpleNamespace(scale_length=1)))),
        'Vector': Vec,
        'bake_measure': lambda *_: {'_samples': [
            {'triangle': 0, 'center': center, 'normal': Vec((0, 0, 1)), 'signed': 0}
        ]},
        'm5_geo': lambda o: {'points': cage_points if o is cage else points, 'triangles': [(0, 1, 2)]},
        'm5_bvh': lambda o: surface if o is high else SimpleNamespace(overlap=lambda _: []),
        'mg_quantiles': lambda values: {'maximum': max(values)},
    }
    node = next(n for n in ast.parse(BAKE_RUNTIME).body
                if isinstance(n, ast.FunctionDef) and n.name == 'bake_cage_report')
    exec(compile(ast.Module(body=[node], type_ignores=[]), '<cage-witness>', 'exec'), namespace)
    return namespace['bake_cage_report'], high, low, cage, surface, center


def test_oblique_cage_hits_finite_high_patch_where_face_normal_misses():
    report, high, low, cage, surface, low_center = fixture((2, 0, 1))
    cage_center = low_center + Vec((2, 0, 1))
    assert surface.ray_cast(cage_center, Vec((0, 0, -1)), 4)[0] is None
    result = report(high, low, cage, {'front_distance_m': 2, 'rear_distance_m': 2, 'max_cage_offset_m': 3})
    assert result['passed'], result['errors']
    assert result['metrics']['sampled_ray_misses'] == 0
    assert result['metrics']['undefined_ray_directions'] == 0


def test_coincident_cage_rejects_undefined_ray_without_calling_bvh():
    report, high, low, cage, surface, _ = fixture((0, 0, 0))
    result = report(high, low, cage, {'front_distance_m': 2, 'rear_distance_m': 2})
    assert not result['passed']
    assert 'CAGE_RAY_DIRECTION_UNDEFINED' in result['errors']
    assert result['metrics']['undefined_ray_directions'] == 1
    assert surface.calls == 0
