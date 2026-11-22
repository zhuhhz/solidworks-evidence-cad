"""Explicit right-handed orthogonal sketch frames in unchanged global XYZ.

These transforms describe reviewed source geometry.  The executor must acquire
and compare the actual native sketch transform separately; declaring a frame
does not prove that SolidWorks used it.
"""
from __future__ import annotations

import math

NUMERIC_MM = 1e-8
VECTOR_TOLERANCE = 1e-12
CARDINALS = ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0),
             (0, 0, 1), (0, 0, -1))


def need(condition, message):
    if not condition:
        raise ValueError(message)


def vector(value, label='XYZ vector'):
    need(isinstance(value, (list, tuple)) and len(value) == 3
         and all(type(x) in (int, float) and math.isfinite(x) for x in value),
         label + ' must contain three finite numbers')
    return tuple(value)


def dot(a, b):
    return math.fsum(x * y for x, y in zip(a, b))


def cross(a, b):
    return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2],
            a[0]*b[1]-a[1]*b[0])


def cardinal(value, label='direction'):
    result = vector(value, label)
    need(result in CARDINALS, label + ' must be one signed Cartesian unit axis')
    return result


def validate_frame(frame):
    need(isinstance(frame, dict)
         and set(frame) == {'origin_mm', 'u', 'v', 'normal'},
         'sketch frame fields must be origin_mm, u, v, normal')
    origin = vector(frame['origin_mm'], 'frame origin')
    need(all(abs(x) <= 100000 for x in origin), 'frame origin exceeds coordinate bound')
    u, v, normal = [cardinal(frame[k], 'frame '+k) for k in ('u', 'v', 'normal')]
    need(dot(u, v) == dot(u, normal) == dot(v, normal) == 0,
         'sketch frame axes must be orthogonal')
    need(cross(u, v) == normal, 'sketch frame is mirrored: u cross v must equal normal')
    return frame


def canonical_frame(normal, origin_mm=(0, 0, 0)):
    """Return a proper frame for each supported signed orthogonal normal."""
    normal = cardinal(normal, 'frame normal')
    choices = {
        (0, 0, 1): ((1, 0, 0), (0, 1, 0)),
        (0, 0, -1): ((-1, 0, 0), (0, 1, 0)),
        (0, -1, 0): ((1, 0, 0), (0, 0, 1)),
        (0, 1, 0): ((-1, 0, 0), (0, 0, 1)),
        (1, 0, 0): ((0, 1, 0), (0, 0, 1)),
        (-1, 0, 0): ((0, -1, 0), (0, 0, 1)),
    }
    u, v = choices[normal]
    frame = dict(origin_mm=list(vector(origin_mm, 'frame origin')),
                 u=list(u), v=list(v), normal=list(normal))
    return validate_frame(frame)


def to_world(point2, frame):
    validate_frame(frame)
    need(isinstance(point2, (list, tuple)) and len(point2) == 2
         and all(type(x) in (int, float) and math.isfinite(x) for x in point2),
         'finite local UV point required')
    return [math.fsum((frame['origin_mm'][i], point2[0]*frame['u'][i],
                       point2[1]*frame['v'][i])) for i in range(3)]


def to_local(point3, frame):
    validate_frame(frame)
    point3 = vector(point3, 'model point')
    delta = tuple(point3[i]-frame['origin_mm'][i] for i in range(3))
    need(abs(dot(delta, frame['normal'])) <= NUMERIC_MM,
         'model point is off the declared sketch plane')
    return [dot(delta, frame['u']), dot(delta, frame['v'])]
