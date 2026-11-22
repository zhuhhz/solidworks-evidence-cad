"""Analytic R007 LINE/ARC geometry; input coordinates are never repaired.

Millimetres and radians.  1e-8 mm is arithmetic consistency tolerance, not a
license to snap coordinates.  The inherited half input quantum is 0.0005 mm;
points within it of a boundary are BOUNDARY, and independent boundaries must
have strictly more than two half quanta of clearance.  Full circles use two
or more arcs.  Adjacent edges may meet only at their declared common vertex.
"""
import math

LENGTH_MM = 0.0005
NUMERIC_MM = 1e-8
ANGLE_RAD = 1e-12
SWEEP_MIN_RAD = 1e-9
MIN_CLEARANCE_MM = 2 * LENGTH_MM
TAU = 2 * math.pi


def need(condition, message):
    if not condition:
        raise ValueError(message)


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def point(value):
    need(isinstance(value, (list, tuple)) and len(value) == 2
         and all(_number(v) for v in value), 'invalid finite XY point')
    return tuple(value)


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1])


def _cross(a, b):
    return a[0] * b[1] - a[1] * b[0]


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1]


def _arc_start(s):
    return math.atan2(s['start'][1] - s['center'][1],
                      s['start'][0] - s['center'][0])


def _arc_delta(s, theta):
    return ((theta - _arc_start(s)) * (1 if s['sweep'] > 0 else -1)) % TAU


def _angle_on_arc(s, theta):
    delta = _arc_delta(s, theta)
    tolerance = ANGLE_RAD + NUMERIC_MM / s['radius']
    return delta <= abs(s['sweep']) + tolerance or TAU - delta <= tolerance


def _arc_point(s, theta):
    c, r = s['center'], s['radius']
    return (c[0] + r * math.cos(theta), c[1] + r * math.sin(theta))


def _point_distance(p, s):
    a, b = s['start'], s['end']
    if s['type'] == 'LINE':
        d = _sub(b, a)
        t = max(0.0, min(1.0, _dot(_sub(p, a), d) / _dot(d, d)))
        return math.dist(p, (a[0] + t * d[0], a[1] + t * d[1]))
    c = s['center']
    length = math.dist(c, p)
    if length == 0:
        return s['radius']
    theta = math.atan2(p[1] - c[1], p[0] - c[0])
    return (abs(length - s['radius']) if _angle_on_arc(s, theta)
            else min(math.dist(p, a), math.dist(p, b)))


def on(s, p):
    """Arithmetic contact predicate, separate from input-quantum ambiguity."""
    return _point_distance(p, s) <= NUMERIC_MM


def _unique(points):
    result = []
    for p in points:
        if not any(math.dist(p, q) <= NUMERIC_MM for q in result):
            result.append(p)
    return result


def _circle_intervals(s):
    start = (_arc_start(s) + (s['sweep'] if s['sweep'] < 0 else 0)) % TAU
    end = start + abs(s['sweep'])
    return [(start, end)] if end <= TAU else [(start, TAU), (0, end - TAU)]


def _contacts(a, b):
    """Return finite contact representatives and whether intervals overlap."""
    if a['type'] == 'ARC' and b['type'] == 'LINE':
        return _contacts(b, a)
    if a['type'] == b['type'] == 'LINE':
        p, q = a['start'], b['start']
        d, e = _sub(a['end'], p), _sub(b['end'], q)
        den = _cross(d, e)
        if den == 0:
            if abs(_cross(_sub(q, p), d)) / math.hypot(*d) > NUMERIC_MM:
                return [], False
            aa = _dot(d, d)
            t0 = _dot(_sub(q, p), d) / aa
            t1 = _dot(_sub(b['end'], p), d) / aa
            lo, hi = max(0.0, min(t0, t1)), min(1.0, max(t0, t1))
            if hi < lo - NUMERIC_MM / math.sqrt(aa):
                return [], False
            pts = [(p[0] + t * d[0], p[1] + t * d[1]) for t in (lo, hi)]
            return _unique(pts), (hi - lo) * math.sqrt(aa) > NUMERIC_MM
        t = _cross(_sub(q, p), e) / den
        p_hit = (p[0] + t * d[0], p[1] + t * d[1])
        return ([p_hit] if on(a, p_hit) and on(b, p_hit) else []), False
    if a['type'] == 'LINE':
        p, d = a['start'], _sub(a['end'], a['start'])
        aa = _dot(d, d)
        t = -_dot(_sub(p, b['center']), d) / aa
        foot = (p[0] + t * d[0], p[1] + t * d[1])
        distance = math.dist(foot, b['center'])
        if distance > b['radius'] + NUMERIC_MM:
            return [], False
        offset = math.sqrt(max(0.0, b['radius'] ** 2 - distance ** 2) / aa)
        pts = [(p[0] + u * d[0], p[1] + u * d[1]) for u in (t - offset, t + offset)]
    else:
        p, q, r, s = a['center'], b['center'], a['radius'], b['radius']
        dist = math.dist(p, q)
        if dist <= NUMERIC_MM:
            if abs(r - s) > NUMERIC_MM:
                return [], False
            pts = [v for v in (a['start'], a['end'], b['start'], b['end'])
                   if on(a, v) and on(b, v)]
            overlap = False
            for lo_a, hi_a in _circle_intervals(a):
                for lo_b, hi_b in _circle_intervals(b):
                    lo, hi = max(lo_a, lo_b), min(hi_a, hi_b)
                    if (hi - lo) * min(r, s) > NUMERIC_MM:
                        overlap = True
                        pts.append(_arc_point(a, (lo + hi) / 2))
            return _unique(pts), overlap
        if dist > r + s + NUMERIC_MM or dist < abs(r - s) - NUMERIC_MM:
            return [], False
        along = (r * r - s * s + dist * dist) / (2 * dist)
        height = math.sqrt(max(0.0, r * r - along * along))
        dx, dy = (q[0] - p[0]) / dist, (q[1] - p[1]) / dist
        pts = [(p[0] + along * dx - sign * height * dy,
                p[1] + along * dy + sign * height * dx) for sign in (-1, 1)]
    return _unique([p for p in pts if on(a, p) and on(b, p)]), False


def intersections(a, b):
    """Contact representatives; an overlap includes an interior representative."""
    return _contacts(a, b)[0]


def _curve_clearance(a, b):
    contacts, overlap = _contacts(a, b)
    if contacts or overlap:
        return 0.0
    values = [_point_distance(p, b) for p in (a['start'], a['end'])]
    values += [_point_distance(p, a) for p in (b['start'], b['end'])]
    if a['type'] == 'ARC' and b['type'] == 'LINE':
        a, b = b, a
    if a['type'] == 'LINE' and b['type'] == 'ARC':
        d = _sub(a['end'], a['start'])
        length = math.hypot(*d)
        for sign in (-1, 1):
            p = (b['center'][0] - sign * b['radius'] * d[1] / length,
                 b['center'][1] + sign * b['radius'] * d[0] / length)
            if on(b, p):
                values.append(_point_distance(p, a))
    elif a['type'] == b['type'] == 'ARC':
        dist = math.dist(a['center'], b['center'])
        if dist > 0:
            u = tuple((b['center'][i] - a['center'][i]) / dist for i in (0, 1))
            for sign_a in (-1, 1):
                p = tuple(a['center'][i] + sign_a * a['radius'] * u[i] for i in (0, 1))
                for sign_b in (-1, 1):
                    q = tuple(b['center'][i] + sign_b * b['radius'] * u[i] for i in (0, 1))
                    if on(a, p) and on(b, q):
                        values.append(math.dist(p, q))
    return min(values)


def bounds(profile):
    pts = [s[k] for s in profile['segments'] for k in ('start', 'end')]
    for s in profile['segments']:
        if s['type'] == 'ARC':
            pts += [_arc_point(s, theta) for theta in (0, math.pi / 2, math.pi, 3 * math.pi / 2)
                    if _angle_on_arc(s, theta)]
    return [min(p[0] for p in pts), min(p[1] for p in pts),
            max(p[0] for p in pts), max(p[1] for p in pts)]


def validate_profile(profile):
    need(isinstance(profile, dict), 'profile must be an object')
    need(isinstance(profile.get('profile_id'), str) and bool(profile['profile_id'].strip()),
         'profile identity')
    segments = profile.get('segments')
    need(isinstance(segments, list) and len(segments) >= 2, 'ordered segments required')
    for s in segments:
        need(isinstance(s, dict) and s.get('type') in ('LINE', 'ARC'), 'segment type')
        a, b = point(s.get('start')), point(s.get('end'))
        need(math.dist(a, b) > LENGTH_MM, 'degenerate edge')
        if s['type'] == 'ARC':
            c = point(s.get('center'))
            r, w = s.get('radius'), s.get('sweep')
            need(_number(r) and r > LENGTH_MM, 'invalid arc radius')
            need(_number(w) and SWEEP_MIN_RAD < abs(w) < TAU - SWEEP_MIN_RAD, 'invalid arc sweep')
            need(abs(math.dist(a, c) - r) <= NUMERIC_MM
                 and abs(math.dist(b, c) - r) <= NUMERIC_MM, 'arc endpoint/radius disagreement')
            target = _arc_point(s, _arc_start(s) + w)
            need(math.dist(target, b) <= NUMERIC_MM, 'arc signed sweep/endpoint disagreement')
    for i, s in enumerate(segments):
        need(math.dist(s['end'], segments[(i + 1) % len(segments)]['start']) <= NUMERIC_MM,
             'disconnected/open profile; coordinates are not snapped')
    for i, a in enumerate(segments):
        for j in range(i + 1, len(segments)):
            allowed = []
            if j == i + 1:
                allowed.append(a['end'])
            if i == 0 and j == len(segments) - 1:
                allowed.append(a['start'])
            contacts, overlap = _contacts(a, segments[j])
            need(not overlap and all(any(math.dist(p, v) <= NUMERIC_MM for v in allowed)
                                     for p in contacts), 'self intersection/tangency/overlap')
            if not allowed:
                need(_curve_clearance(a, segments[j]) > MIN_CLEARANCE_MM,
                     'nonadjacent self clearance unresolved at input quantum')
    # A deterministic origin reduces cancellation without changing coordinates;
    # fsum gives the same signed area for every cyclic representation.
    origin = min(tuple(s[k]) for s in segments for k in ('start', 'end'))
    areas, lengths = [], []
    for s in segments:
        a, b = _sub(s['start'], origin), _sub(s['end'], origin)
        if s['type'] == 'LINE':
            areas.append(_cross(a, b) / 2)
            lengths.append(math.dist(a, b))
        else:
            c = _sub(s['center'], origin)
            areas.append((_cross(c, _sub(b, a)) + s['radius'] ** 2 * s['sweep']) / 2)
            lengths.append(s['radius'] * abs(s['sweep']))
    signed_area, perimeter = math.fsum(areas), math.fsum(lengths)
    need(abs(signed_area) > perimeter * LENGTH_MM, 'zero/unresolved profile area')
    return dict(signed_area_mm2=signed_area, area_mm2=abs(signed_area),
                perimeter_mm=perimeter, orientation='CCW' if signed_area > 0 else 'CW',
                bounds_mm=bounds(profile))


def classify_point(profile, query):
    """Exact circular winding, with explicit input-quantum boundary ambiguity.

    Split an arc at the line through its center and query.  Each resulting
    arc lies in a half-plane whose boundary contains the query, so its change
    of argument cannot wrap by more than pi.  Summing signed atan2 changes is
    analytic, and avoids ray/vertex counting or a polyline approximation.
    """
    p = point(query)
    if any(_point_distance(p, s) <= LENGTH_MM for s in profile['segments']):
        return 'BOUNDARY'
    contributions = []
    for s in profile['segments']:
        if s['type'] == 'LINE':
            a, b = _sub(s['start'], p), _sub(s['end'], p)
            contributions.append(math.atan2(_cross(a, b), _dot(a, b)))
            continue
        c = s['center']
        if math.dist(c, p) <= NUMERIC_MM:
            contributions.append(s['sweep'])
            continue
        theta = math.atan2(p[1] - c[1], p[0] - c[0])
        cuts = [0.0, abs(s['sweep'])]
        for direction in (theta, theta + math.pi):
            delta = _arc_delta(s, direction)
            if ANGLE_RAD < delta < abs(s['sweep']) - ANGLE_RAD:
                cuts.append(delta)
        cuts.sort()
        sign = 1 if s['sweep'] > 0 else -1
        vertices = [s['start']] + [_arc_point(s, _arc_start(s) + sign * t)
                                  for t in cuts[1:-1]] + [s['end']]
        for i, (start, end) in enumerate(zip(vertices, vertices[1:])):
            a, b = _sub(start, p), _sub(end, p)
            angle = math.atan2(_cross(a, b), _dot(a, b))
            if abs(abs(angle) - math.pi) <= ANGLE_RAD:
                # Exact antipodal vectors lose traversal sign in signed zero.
                # The circular midpoint identifies which half-plane was used.
                mid = _sub(_arc_point(s, _arc_start(s) + sign * (cuts[i] + cuts[i + 1]) / 2), p)
                angle = math.copysign(abs(angle), _cross(a, mid))
            contributions.append(angle)
    winding_angle = math.fsum(contributions)
    return 'INSIDE' if abs(winding_angle) > math.pi else 'OUTSIDE'


def boundary_clearance(a, b):
    return min(_curve_clearance(x, y) for x in a['segments'] for y in b['segments'])


def _separated(a, b):
    for x in a['segments']:
        for y in b['segments']:
            contacts, overlap = _contacts(x, y)
            need(not contacts and not overlap, 'profile boundaries intersect/touch/overlap')
    need(boundary_clearance(a, b) > MIN_CLEARANCE_MM,
         'profile clearance unresolved at input quantum')


def _vertex_classes(host, guest):
    # Every supplied vertex is classified: no first-segment representative,
    # no segment-list rotation, and no chosen interior point can hide a fault.
    return {classify_point(host, s[k]) for s in guest['segments'] for k in ('start', 'end')}


def contains(base, cut):
    validate_profile(base)
    validate_profile(cut)
    _separated(base, cut)
    need(_vertex_classes(base, cut) == {'INSIDE'}, 'cut not strictly inside base material')
    return True


def independent(a, b):
    validate_profile(a)
    validate_profile(b)
    _separated(a, b)
    need(_vertex_classes(a, b) == {'OUTSIDE'} and _vertex_classes(b, a) == {'OUTSIDE'},
         'nested/overlapping cut profiles unsupported')
    return True
