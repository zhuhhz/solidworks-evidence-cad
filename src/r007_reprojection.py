"""Analytic forward checks for bounded Z prisms and cylinder regions.

The projector only receives a 3D hypothesis, never the observed drawing.  P1
profile validation, exact arc extrema and angular intervals are shared tools;
this is an independent forward path, not an external/native CAD proof.

Reliable hidden-line semantics are deliberately narrower than geometry:
one rectangular host and disjoint, strictly internal full-height cylinder
cuts. Other combinations retain NOT_PROVEN even when positions agree.
"""
from __future__ import annotations

import math

from r007_geometry import (LENGTH_MM, NUMERIC_MM, bounds, validate_profile,
                           _angle_on_arc, _circle_intervals)

ROLES = ('TOP', 'FRONT', 'LEFT')
SHARED_DEPENDENCIES = ['r007_geometry.validate_profile', 'r007_geometry.bounds',
                       'r007_geometry._angle_on_arc', 'r007_geometry._circle_intervals']


def _line(a, b, style='UNRESOLVED'):
    return dict(type='LINE', start=list(a), end=list(b), line_style=style)


def _metrics(curve):
    if curve['type'] == 'LINE':
        return dict(length_mm=math.dist(curve['start'], curve['end']))
    return dict(radius_mm=curve['radius'], diameter_mm=2*curve['radius'],
                length_mm=curve['radius']*(2*math.pi if curve['type']=='CIRCLE' else abs(curve['sweep'])))


def _rectangle(x0, y0, x1, y1, style='VISIBLE'):
    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    return [_line(a, corners[(i+1) % 4], style) for i, a in enumerate(corners)]


def _is_rectangle(profile):
    segments = profile['segments']
    if len(segments) != 4 or any(s['type'] != 'LINE' for s in segments):
        return False
    x0, y0, x1, y1 = bounds(profile)
    corners = {(x0, y0), (x1, y0), (x1, y1), (x0, y1)}
    # P2 solving may leave arithmetic tails at coincident corners. Match each
    # vertex bijectively using the existing numerical epsilon, without snapping.
    matched = [{corner for corner in corners
                if math.dist(s['start'], corner) <= NUMERIC_MM} for s in segments]
    return (all(len(values) == 1 for values in matched)
            and len(set.union(*matched)) == 4
            and all(abs(s['start'][0]-s['end'][0]) <= NUMERIC_MM
                    or abs(s['start'][1]-s['end'][1]) <= NUMERIC_MM for s in segments))


def _prism_projection(primitive):
    profile = primitive['xy_profile']
    validate_profile(profile)
    z0, z1 = primitive['z_min'], primitive['z_max']
    if not (math.isfinite(z0) and math.isfinite(z1) and z1-z0 > 2*LENGTH_MM):
        raise ValueError('finite positive prism Z interval required')
    rectangular = _is_rectangle(profile)
    x0, y0, x1, y1 = bounds(profile)
    out = {role: [] for role in ROLES}
    for segment in profile['segments']:
        curve = dict(type=segment['type'], start=list(segment['start']), end=list(segment['end']),
                     line_style='VISIBLE' if primitive['type'] == 'ADDITIVE_PRISM' else 'UNRESOLVED')
        if segment['type'] == 'ARC':
            curve.update(center=list(segment['center']), radius=segment['radius'], sweep=segment['sweep'])
        out['TOP'].append(curve)
    if rectangular:
        out['FRONT'] = _rectangle(x0, z0, x1, z1)
        out['LEFT'] = _rectangle(-y1, z0, -y0, z1)
    else:
        # Project every possible boundary edge/generator. Occlusion is not
        # decided here; a missing possible edge cannot prove contradiction.
        for role, coordinate, sign in [('FRONT', 0, 1), ('LEFT', 1, -1)]:
            for segment in profile['segments']:
                values = [sign*segment[k][coordinate] for k in ('start', 'end')]
                if segment['type'] == 'ARC':
                    for angle in (0, math.pi/2, math.pi, 3*math.pi/2):
                        if _angle_on_arc(segment, angle):
                            xy = (segment['center'][0]+segment['radius']*math.cos(angle),
                                  segment['center'][1]+segment['radius']*math.sin(angle))
                            values.append(sign*xy[coordinate])
                lo, hi = min(values), max(values)
                if hi-lo > NUMERIC_MM:
                    out[role].extend([_line((lo, z), (hi, z)) for z in (z0, z1)])
                out[role].extend(_line((u, z0), (u, z1)) for u in values)
    return out, rectangular


def _reliable_domain(primitives):
    hosts = [p for p in primitives if p.get('is_host')]
    if len(hosts) != 1 or hosts[0]['type'] != 'ADDITIVE_PRISM':
        return False, ['ONE_EXPLICIT_ADDITIVE_HOST_REQUIRED']
    host = hosts[0]
    if host.get('depth_state') not in ('BOUNDED_Z_EXTENT', 'FULL_HOST_Z_SPAN'):
        return False, ['HOST_END_PLANES_NOT_PROVEN']
    if not _is_rectangle(host['xy_profile']):
        return False, ['NONRECTANGULAR_OCCLUSION_NOT_IMPLEMENTED']
    b = bounds(host['xy_profile'])
    holes = [p for p in primitives if p is not host]
    reasons = []
    for hole in holes:
        if hole['type'] != 'SUBTRACTIVE_CYLINDER' or hole.get('geometry_family') != 'CYLINDER_Z':
            reasons.append('ONLY_INTERNAL_CYLINDER_CUT_OCCLUSION_IMPLEMENTED')
            continue
        if (hole.get('depth_state') != 'FULL_HOST_Z_SPAN'
                or abs(hole['z_min']-host['z_min']) > NUMERIC_MM
                or abs(hole['z_max']-host['z_max']) > NUMERIC_MM):
            reasons.append('FULL_HOST_Z_SPAN_NOT_PROVEN')
        cx, cy = hole['center_xy']
        r = hole['radius']
        if min(cx-r-b[0], cy-r-b[1], b[2]-cx-r, b[3]-cy-r) <= 2*LENGTH_MM:
            reasons.append('CYLINDER_NOT_STRICTLY_INTERNAL_TO_HOST')
    cylinders = [p for p in holes if p.get('geometry_family') == 'CYLINDER_Z']
    for i, a in enumerate(cylinders):
        for c in cylinders[i+1:]:
            if math.dist(a['center_xy'], c['center_xy'])-a['radius']-c['radius'] <= 2*LENGTH_MM:
                reasons.append('INTERACTING_CYLINDER_OCCLUSION_NOT_IMPLEMENTED')
    return not reasons, sorted(set(reasons))


def project_candidate(candidate):
    """Project the supplied 3D analytic parameters; no source observation input."""
    primitives = candidate.get('primitives', [])
    if not primitives:
        return dict(views={role: [] for role in ROLES}, visibility_supported=False,
                    unproven_reasons=['NO_ANALYTIC_PRIMITIVES'])
    reasons, views = [], {role: [] for role in ROLES}
    try:
        reliable, reasons = _reliable_domain(primitives)
        for primitive in primitives:
            family = primitive.get('geometry_family')
            if family == 'PRISM_Z':
                if primitive['type'] not in ('ADDITIVE_PRISM', 'SUBTRACTIVE_PRISM'):
                    raise ValueError('prism material type required')
                projected, rectangular = _prism_projection(primitive)
                if primitive['type'] == 'SUBTRACTIVE_PRISM':
                    reliable = False
                    reasons.append('PRISM_BOOLEAN_VISIBILITY_NOT_IMPLEMENTED')
                for role in ROLES:
                    for row in projected[role]:
                        row['primitive_id'] = primitive['primitive_id']
                    views[role].extend(projected[role])
            elif family == 'CYLINDER_Z':
                cx, cy = primitive['center_xy']
                r, z0, z1 = primitive['radius'], primitive['z_min'], primitive['z_max']
                if not all(type(v) in (int, float) and math.isfinite(v) for v in (cx, cy, r, z0, z1)):
                    raise ValueError('finite cylinder parameters required')
                if r <= LENGTH_MM or z1-z0 <= 2*LENGTH_MM:
                    raise ValueError('positive cylinder radius/Z interval required')
                views['TOP'].append(dict(type='CIRCLE', center=[cx, cy], radius=r,
                                         line_style='VISIBLE' if reliable else 'UNRESOLVED',
                                         primitive_id=primitive['primitive_id']))
                side_style = 'HIDDEN' if reliable else 'UNRESOLVED'
                for role, mid in [('FRONT', cx), ('LEFT', -cy)]:
                    rows = [_line((u, z0), (u, z1), side_style) for u in (mid-r, mid+r)]
                    if not reliable:
                        rows.extend([_line((mid-r, z), (mid+r, z)) for z in (z0, z1)])
                    for row in rows:
                        row['primitive_id'] = primitive['primitive_id']
                    views[role].extend(rows)
            else:
                reliable = False
                reasons.append('UNSUPPORTED_ANALYTIC_PRIMITIVE')
        if not reliable:
            # Rectangle cap edges also lack proven visibility once an
            # unsupported boolean combination may occlude them.
            for rows in views.values():
                for row in rows:
                    row['line_style'] = 'UNRESOLVED'
        for role in ROLES:
            for index, row in enumerate(views[role]):
                row['projection_id'] = f'{role}-P{index+1}'
                row['metrics'] = _metrics(row)
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        return dict(views={role: [] for role in ROLES}, visibility_supported=False,
                    unproven_reasons=['ANALYTIC_PARAMETERS_INVALID: '+str(exc)])
    return dict(views=views, visibility_supported=reliable,
                unproven_reasons=sorted(set(reasons)), shared_dependencies=SHARED_DEPENDENCIES,
                coordinate_convention='TOP=(X,Y); FRONT=(X,Z); PHYSICAL_LEFT=(-Y,Z)',
                independent_scope='forward analytic parameters; shared P1 curve tools; no native CAD proof')


def _interval_covered(lo, hi, intervals, tolerance):
    cursor = lo
    for start, end in sorted(intervals):
        if end < cursor-tolerance:
            continue
        if start > cursor+tolerance:
            return False
        cursor = max(cursor, end)
        if cursor >= hi-tolerance:
            return True
    return cursor >= hi-tolerance


def _line_covered(source, projected, tolerance):
    a, b = source['start'], source['end']
    dx, dy = b[0]-a[0], b[1]-a[1]
    length = math.hypot(dx, dy)
    if length <= NUMERIC_MM:
        return False
    ux, uy = dx/length, dy/length
    intervals = []
    for target in projected:
        if target['type'] != 'LINE':
            continue
        points = [target['start'], target['end']]
        if all(abs((p[0]-a[0])*uy-(p[1]-a[1])*ux) <= tolerance for p in points):
            intervals.append(sorted((p[0]-a[0])*ux+(p[1]-a[1])*uy for p in points))
    return _interval_covered(0, length, intervals, tolerance)


def _curve_intervals(curve):
    return [(0, 2*math.pi)] if curve['type'] == 'CIRCLE' else _circle_intervals(curve)


def _curve_covered(source, projected, tolerance):
    if source['type'] == 'LINE':
        return _line_covered(source, projected, tolerance)
    if source['type'] not in ('CIRCLE', 'ARC'):
        return False
    intervals = []
    for target in projected:
        if (target['type'] in ('CIRCLE', 'ARC')
                and math.dist(source['center'], target['center']) <= tolerance
                and abs(source['radius']-target['radius']) <= tolerance):
            intervals.extend(_curve_intervals(target))
    angular_tolerance = tolerance/source['radius']
    return all(_interval_covered(lo, hi, intervals, angular_tolerance)
               for lo, hi in _curve_intervals(source))


def _geometry_bounds(curves):
    values = []
    for curve in curves:
        if curve['type'] == 'CIRCLE':
            x, y = curve['center']
            r = curve['radius']
            values.append([x-r, y-r, x+r, y+r])
        else:
            values.append(bounds({'segments': [curve]}))
    if not values:
        return None
    return [min(v[0] for v in values), min(v[1] for v in values),
            max(v[2] for v in values), max(v[3] for v in values)]


def compare_views(projected, source_views, *, tolerance_mm=LENGTH_MM):
    """Compare finite loci in canonical model-coordinate views.

    source_views maps physical role to LINE/ARC/CIRCLE observations. A missing
    hidden line is unobserved evidence, never proof that a hole is absent.
    """
    if (type(tolerance_mm) not in (int, float) or not math.isfinite(tolerance_mm)
            or tolerance_mm <= 0 or tolerance_mm > LENGTH_MM):
        raise ValueError('reprojection tolerance must be positive and no looser than P1 quantum')
    comparisons, contradictions, unobserved, untested = [], [], [], []
    complete = True
    for role in ROLES:
        expected = projected['views'].get(role, [])
        observed = source_views.get(role)
        if observed is None:
            complete = False
            untested.append(dict(view_kind=role, reason='SOURCE_VIEW_MISSING'))
            continue
        observed = [e for e in observed if e.get('line_style') != 'CENTERLINE']
        for entity in source_views[role]:
            if entity.get('line_style') == 'CENTERLINE':
                untested.append(dict(view_kind=role, source_entity_id=entity.get('entity_id', entity.get('id')),
                                     reason='CENTERLINE_EXCLUDED_FROM_SOLID_BOUNDARY_COMPARISON'))
        for entity in observed:
            geometry = _curve_covered(entity, expected, tolerance_mm)
            row = dict(view_kind=role, source_entity_id=entity.get('entity_id', entity.get('id')),
                       geometry_status='PASS' if geometry else 'FAIL',
                       geometry_test='FINITE_LINE_INTERVAL_OR_CIRCULAR_ANGULAR_COVERAGE')
            row['source_reference_ids'] = entity.get('source_reference_ids', [])
            row['source_metrics'] = _metrics(entity)
            if geometry and projected['visibility_supported']:
                same_style = [e for e in expected if e['line_style'] == entity['line_style']]
                visible_ok = _curve_covered(entity, same_style, tolerance_mm)
                row['visibility_status'] = 'PASS' if visible_ok else 'FAIL'
                if not visible_ok:
                    contradictions.append(dict(**row, reason='LINE_STYLE_DISAGREES_WITH_BOUNDED_OCCLUSION'))
            else:
                row['visibility_status'] = 'NOT_PROVEN'
            if not geometry:
                contradictions.append(dict(**row, reason='FINITE_SOURCE_GEOMETRY_NOT_PROJECTED'))
            comparisons.append(row)
        for entity in expected:
            if not _curve_covered(entity, observed, tolerance_mm):
                complete = False
                unobserved.append(dict(view_kind=role, projection_id=entity.get('projection_id'),
                                       line_style=entity['line_style'], reason='PROJECTED_CURVE_NOT_OBSERVED'))
        source_bounds, projected_bounds = _geometry_bounds(observed), _geometry_bounds(expected)
        range_ok = (source_bounds is not None and projected_bounds is not None
                    and all(abs(a-b) <= tolerance_mm for a,b in zip(source_bounds,projected_bounds)))
        if not range_ok:
            complete = False
        comparisons.append(dict(view_kind=role, comparison='COORDINATE_RANGES',
                                source_bounds_mm=source_bounds, projected_bounds_mm=projected_bounds,
                                geometry_status='PASS' if range_ok else 'NOT_PROVEN'))
    geometry_failed = any(c.get('geometry_status') == 'FAIL' for c in contradictions)
    visibility_failed = any(c.get('visibility_status') == 'FAIL' for c in contradictions)
    geometry_status = 'FAIL' if geometry_failed else ('PASS' if complete and all(projected['views'].values()) else 'NOT_PROVEN')
    visibility_status = ('FAIL' if visibility_failed else 'PASS'
        if projected['visibility_supported'] and geometry_status == 'PASS' else 'NOT_PROVEN')
    full = geometry_status == visibility_status == 'PASS'
    return dict(status='FULL_BOUNDED_REPROJECTION_PASS' if full else 'FAIL'
                if geometry_failed or visibility_failed else 'NOT_PROVEN',
                geometry_status=geometry_status, visibility_status=visibility_status,
                GEOMETRY_REPROJECTION_PASS=geometry_status == 'PASS',
                VISIBILITY_REPROJECTION_PASS=visibility_status == 'PASS',
                FULL_BOUNDED_REPROJECTION_PASS=full, NOT_PROVEN=not full and not (geometry_failed or visibility_failed),
                comparisons=comparisons, contradiction_evidence=contradictions,
                unobserved_projected_objects=unobserved, uncompared_source_objects=untested,
                unproven_reasons=projected.get('unproven_reasons', []),
                tolerance_mm=tolerance_mm, shared_dependencies=SHARED_DEPENDENCIES,
                independence='separate analytic forward path sharing P1 curve geometry; no native CAD qualification')


def evaluate_reprojection(candidate, normalized_views, *, tolerance_mm=LENGTH_MM):
    """Evaluate reviewed P1-style normalized source curves, without filling gaps."""
    source = {}
    for view in normalized_views:
        role = view['view_kind']
        if role in source:
            raise ValueError('duplicate physical source view in reprojection')
        source[role] = view.get('canonical_geometry_entities', view['geometry_entities'])
    projected = project_candidate(candidate)
    result = compare_views(projected, source, tolerance_mm=tolerance_mm)
    result['candidate_id'] = candidate['candidate_id']
    result['projection'] = projected
    result['source_view_ids'] = [v['view_id'] for v in normalized_views]
    conditional = []
    for view in normalized_views:
        if view.get('p2_status') not in (None, 'RESOLVED_BOUNDED'):
            conditional.append(dict(view_id=view['view_id'], reason='SOURCE_2D_GEOMETRY_NOT_RESOLVED',
                                    p2_status=view['p2_status']))
        for entity in source[view['view_kind']]:
            if entity.get('review_state') in ('UNKNOWN', 'INFERRED', 'UNRESOLVED', 'CONTRADICTED'):
                conditional.append(dict(view_id=view['view_id'],source_entity_id=entity['entity_id'],
                    reason='SOURCE_CURVE_REQUIRES_REVIEW',review_state=entity['review_state']))
    result['conditional_source_evidence'] = conditional
    if conditional:
        result['FULL_BOUNDED_REPROJECTION_PASS'] = False
        result['VISIBILITY_REPROJECTION_PASS'] = False
        if result['visibility_status'] == 'PASS':
            result['visibility_status'] = 'NOT_PROVEN'
        if result['status'] != 'FAIL':
            result['status'] = 'NOT_PROVEN'
            result['NOT_PROVEN'] = True
    return result
