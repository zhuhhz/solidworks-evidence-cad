"""Opt-in bounded native evidence; never activates a server or changes Q01 routing.

Semantic IDs only link an existing execution ledger to persistent native objects.
They cannot supply measured geometry or prove a native relationship.
"""
from __future__ import annotations

from copy import deepcopy
import base64
import math
from pathlib import Path

from .r003_native_collectors import (
    NativeEvidenceUnavailable, member, capture_persistent_reference,
    resolve_persistent_reference, collect_plane_face, collect_reference_plane,
)
from .r004_execution_adapter import _invoke_com_method, from_native
from .r003_native_collectors import _vector
from .r003_execution_bridge import LINEAR_TOLERANCE_MM, AXIS_COMPONENT_TOLERANCE


def _require(condition, reason):
    if not condition:
        raise NativeEvidenceUnavailable(reason)


def _document(model, document):
    _require(isinstance(document, dict) and document.get("document_id")
             and document.get("path"), "native document binding missing")
    path = member(model, "GetPathName")  # Existing owned-document read boundary.
    _require(isinstance(path, str) and Path(path).is_absolute()
             and Path(path).resolve() == Path(document["path"]).resolve(),
             "wrong native document binding")
    return {"document_id": document["document_id"], "path": str(Path(path).resolve()),
            "source": "NATIVE_OBSERVED"}


def _resolved(extension, record, resolver):
    result = resolve_persistent_reference(extension, record, resolver=resolver)
    entity = result.pop("native_entity")
    actual = capture_persistent_reference(extension, entity, record["reference_type"])
    _require(actual["encoded"] == record["encoded"], "resolved typed identity changed")
    return entity, {**result, "source_label": "NATIVE_OBSERVED"}


def _support(extension, reference, owner_refs, resolver):
    entity, proof = _resolved(extension, reference, resolver)
    kind = reference["reference_type"]
    _require(kind in {"FACE", "PLANE"}, "unsupported native support kind")
    geometry = (collect_plane_face(entity, extension, owner_refs) if kind == "FACE"
                else collect_reference_plane(entity, extension, name="native-support"))
    # R003 helpers expose SW coordinates. The V1 consumer uses the existing R004
    # model frame. Both are retained so the measured values remain auditable.
    origin = list(from_native(geometry["origin_mm"]))
    normal = list(from_native(geometry["normal"]))
    readback = {"origin_mm": origin, "normal": normal,
                "offset_z_mm": origin[2], "raw_native": geometry,
                "source_label": "NATIVE_OBSERVED"}
    owner = ({"reference_type": "FEATURE", "encoded": geometry["owner_persistent_reference"],
              "source": "IModelDocExtension.GetPersistReference3"} if kind == "FACE" else None)
    return proof, readback, owner


def collect_feature_registry(model, ledger, *, document, resolver=None):
    """Package persistent feature/sketch/support facts, rejecting an unproved host.

    The root datum may be a native PLANE; dependent operations need a FACE owned
    by the host and a profile parent equal to that same persistent host feature.
    """
    provenance = _document(model, document)
    extension = member(model, "Extension")
    _require(ledger, "native operation ledger missing")
    ids = [row.get("source_feature_id") for row in ledger]
    _require(all(isinstance(x, str) and x for x in ids) and len(set(ids)) == len(ids),
             "duplicate/missing native source identity")
    feature_refs = {row["source_feature_id"]: row["native_feature_reference"] for row in ledger}
    _require(all(ref.get("reference_type") == "FEATURE" for ref in feature_refs.values()),
             "typed FEATURE references required")
    reverse = {ref["encoded"]: fid for fid, ref in feature_refs.items()}
    _require(len(reverse) == len(ledger), "duplicate native feature persistent identity")
    owner_refs = {fid: ref["encoded"] for fid, ref in feature_refs.items()}
    registry = {}
    for row in ledger:
        fid = row["source_feature_id"]
        feature, feature_proof = _resolved(extension, feature_refs[fid], resolver)
        sketch_ref = row["native_sketch_reference"]
        _require(sketch_ref.get("reference_type") == "SKETCH", "typed SKETCH reference required")
        profile, profile_proof = _resolved(extension, sketch_ref, resolver)
        parents = _invoke_com_method(feature, "GetParents") or []
        profile_tokens = [capture_persistent_reference(extension, p, "FEATURE")["encoded"] for p in parents]
        _require(profile_tokens.count(sketch_ref["encoded"]) == 1,
                 "operation/profile native relationship missing")
        profile_parents = _invoke_com_method(profile, "GetParents") or []
        parent_refs = [capture_persistent_reference(extension, p, "FEATURE") for p in profile_parents]
        host_ids = [reverse[p["encoded"]] for p in parent_refs if p["encoded"] in reverse]
        host_claim = row.get("host_feature_id")
        _require(host_claim is None or host_claim in feature_refs, "UNKNOWN native host")
        _require(host_ids == ([] if host_claim is None else [host_claim]),
                 "host claim without unique native relationship proof")
        support_ref = row["native_support_reference"]
        support_proof, geometry, owner = _support(extension, support_ref, owner_refs, resolver)
        if host_claim is not None:
            _require(support_ref["reference_type"] == "FACE" and owner is not None
                     and owner["encoded"] == feature_refs[host_claim]["encoded"],
                     "native support owner/host mismatch")
            _, host_proof = _resolved(extension, feature_refs[host_claim], resolver)
        else:
            _require(support_ref["reference_type"] == "PLANE"
                     and sum(p["encoded"] == support_ref["encoded"] for p in parent_refs) == 1,
                     "root datum native parent relationship missing")
            host_proof = None
        registry[fid] = {
            "native_feature_id": fid, "reference": deepcopy(feature_refs[fid]),
            "persistent_reference_resolved": True, "feature_resolution": feature_proof,
            "relationship_source": "NATIVE_PERSISTENT_FEATURE_SUPPORT",
            "host_native_feature_id": host_claim,
            "host_reference": deepcopy(feature_refs[host_claim]) if host_claim is not None else None,
            "host_reference_resolved": host_proof is not None and host_proof["resolved"],
            "profile_reference": deepcopy(sketch_ref), "profile_resolution": profile_proof,
            "native_parent_references": parent_refs,
            "support_reference": deepcopy(support_ref), "support_reference_resolved": True,
            "support_resolution": support_proof, "support_owner_reference": owner,
            "support_geometry_readback": True, "support_geometry": geometry,
            "support_plane_z_mm": geometry["offset_z_mm"],
            "support_region": {"face_reference": support_ref["encoded"], "source_label": "NATIVE_OBSERVED"},
            "document_provenance": provenance,
            "field_sources": {"reference": "NATIVE_OBSERVED", "host_relation": "DERIVED_FROM_NATIVE",
                              "support_geometry": "NATIVE_OBSERVED", "ledger_source_id": "EXECUTION"},
        }
    return registry


def collect_face_owner(extension, face, registry, *, resolver=None):
    reference = capture_persistent_reference(extension, face, "FACE")
    _, proof = _resolved(extension, reference, resolver)
    owner = _invoke_com_method(face, "GetFeature")
    _require(owner is not None, "UNKNOWN native face owner")
    owner_reference = capture_persistent_reference(extension, owner, "FEATURE")
    matches = [fid for fid, row in registry.items()
               if row["reference"]["encoded"] == owner_reference["encoded"]]
    _require(len(matches) == 1, "UNKNOWN/UNATTRIBUTED native owner")
    _, owner_proof = _resolved(extension, owner_reference, resolver)
    return {"reference": reference, "face_resolution": proof,
            "persistent_reference_resolved": True, "native_owner_feature_id": matches[0],
            "owner_reference": owner_reference, "owner_resolution": owner_proof,
            "ownership_source": "IFace2.GetFeature+IModelDocExtension.GetPersistReference3",
            "source_label": "NATIVE_OBSERVED"}


def _sub(a, b):
    return [x-y for x, y in zip(a, b)]


def _dot(a, b):
    return sum(x*y for x, y in zip(a, b))


def _unit(a):
    length = math.sqrt(_dot(a, a))
    _require(length > AXIS_COMPONENT_TOLERANCE, "zero native direction")
    return [x/length for x in a]


def _near(a, b):
    return math.dist(a, b) <= LINEAR_TOLERANCE_MM


def _model_point(raw):
    return list(from_native([x*1000 for x in _vector(raw, 3, "native XYZ")]))


def _z_axis(axis):
    axis = _unit(axis)
    _require(abs(axis[0]) <= AXIS_COMPONENT_TOLERANCE
             and abs(axis[1]) <= AXIS_COMPONENT_TOLERANCE
             and abs(abs(axis[2])-1) <= AXIS_COMPONENT_TOLERANCE,
             "UNSUPPORTED_CIRCULAR_ORIENTATION")
    return axis


def _angle(point, center):
    return math.degrees(math.atan2(point[1]-center[1], point[0]-center[0])) % 360


def _advance(a, b, direction):
    return ((b-a)*direction) % 360


def _angular_domain(start, span, direction, *, signed=False):
    # Domains are always CCW intervals; raw traversal remains in the evidence.
    low = (start if direction > 0 else start-span) % 360
    if abs(span-360) <= AXIS_COMPONENT_TOLERANCE:
        return [0.0, 360.0]
    if abs(low-360) <= AXIS_COMPONENT_TOLERANCE or abs(low) <= AXIS_COMPONENT_TOLERANCE:
        low = 0.0
    if signed and low > 180:
        low -= 360
    return [round(low, 9), round(low+span, 9)]


def _edge_identity(extension, coedge, resolver):
    edge = _invoke_com_method(coedge, "GetEdge")
    _require(edge is not None, "typed EDGE missing")
    reference = capture_persistent_reference(extension, edge, "EDGE")
    _, resolution = _resolved(extension, reference, resolver)
    return edge, reference, resolution


def _evaluate_circle(coedge, param, center, radius):
    raw = _vector(_invoke_com_method(coedge, "Evaluate2", param, 1), 6, "Evaluate2")
    point = _model_point(raw[:3])
    tangent_mm = list(from_native([x*1000 for x in raw[3:]]))
    radial = _sub(point, center)
    _require(math.sqrt(_dot(radial, radial)) > LINEAR_TOLERANCE_MM
             and math.sqrt(_dot(tangent_mm, tangent_mm)) > LINEAR_TOLERANCE_MM,
             "AMBIGUOUS_TANGENT_DIRECTION")
    ru, tu = _unit(radial), _unit(tangent_mm)
    _require(abs(math.dist(point, center)-radius) <= LINEAR_TOLERANCE_MM
             and abs(radial[2]) <= LINEAR_TOLERANCE_MM
             and abs(tu[2]) <= AXIS_COMPONENT_TOLERANCE
             and abs(_dot(ru, tu)) <= AXIS_COMPONENT_TOLERANCE,
             "NATIVE_CIRCLE_EVALUATION_MISMATCH")
    cross_z = ru[0]*tu[1]-ru[1]*tu[0]
    _require(abs(cross_z) > AXIS_COMPONENT_TOLERANCE, "AMBIGUOUS_TANGENT_DIRECTION")
    return {"parameter": param, "raw_native": raw, "point_mm": point,
            "tangent_mm": tangent_mm, "direction": 1 if cross_z > 0 else -1,
            "source_label": "NATIVE_OBSERVED"}


def _circular_trim(coedge, edge, curve, row):
    raw_circle = _vector(member(curve, "CircleParams"), 7, "CircleParams")
    center = _model_point(raw_circle[:3])
    axis = _z_axis(from_native(raw_circle[3:6]))
    radius = raw_circle[6]*1000
    _require(radius > LINEAR_TOLERANCE_MM, "invalid native circle radius")
    data = _invoke_com_method(edge, "GetCurveParams3")  # GetCurve has already generated the curve.
    sense = member(data, "Sense")
    cp_start_raw = _vector(member(data, "StartPoint"), 3, "CurveParamData.StartPoint")
    cp_end_raw = _vector(member(data, "EndPoint"), 3, "CurveParamData.EndPoint")
    limits = _vector([member(data, "UMinValue"), member(data, "UMaxValue")], 2, "native interval")
    kind = member(data, "CurveType")
    _require(type(sense) is bool and type(kind) is int and kind == 3002,
             "native circular CurveParamData type/sense mismatch")
    lo, hi = limits
    _require(hi-lo > AXIS_COMPONENT_TOLERANCE, "AMBIGUOUS_CLOSED_CIRCULAR_EDGE")
    cp_start, cp_end = _model_point(cp_start_raw), _model_point(cp_end_raw)
    curve_coedge_same = sense == row["coedge_edge_same_direction"]
    a, b = (cp_start, cp_end) if curve_coedge_same else (cp_end, cp_start)
    _require(_near(a, row["start_mm"]) and _near(b, row["end_mm"]),
             "CURVE_EDGE_COEDGE_SENSE_CONTRADICTION")
    params = row["coedge_parameters"][6:8]
    paired = [lo, hi] if curve_coedge_same else [hi, lo]
    # Accept only an explicitly common native interval, never an assumed phase or
    # a guessed mapping between two different parameter representations.
    _require(all(abs(x-y) <= AXIS_COMPONENT_TOLERANCE for x, y in zip(params, paired)),
             "COEDGE_PARAMETER_INTERVAL_INCONSISTENCY")
    first = _evaluate_circle(coedge, params[0], center, radius)
    _require(_near(first["point_mm"], row["start_mm"]), "COEDGE_PARAMETER_POINT_INCONSISTENCY")
    direction = first["direction"]
    samples = [_evaluate_circle(coedge, lo+(hi-lo)*f, center, radius)
               for f in (0.0, 0.25, 0.5, 0.75, 1.0)]
    _require(all(s["direction"] == direction for s in samples), "CONTRADICTORY_NATIVE_TANGENT")
    _require(_near(samples[0]["point_mm"], cp_start)
             and _near(samples[-1]["point_mm"], cp_end), "EDGE_PARAMETER_POINT_INCONSISTENCY")
    parameter_traversal = direction if curve_coedge_same else -direction
    angles = [_angle(s["point_mm"], center) for s in samples]
    advances = [_advance(a, b, parameter_traversal) for a, b in zip(angles, angles[1:])]
    _require(all(a > AXIS_COMPONENT_TOLERANCE for a in advances), "AMBIGUOUS_CLOSED_CIRCULAR_EDGE")
    observed_coverage = sum(advances)
    closed = _near(cp_start, cp_end) and _near(row["start_mm"], row["end_mm"])
    if closed:
        _require(abs(observed_coverage-360) <= AXIS_COMPONENT_TOLERANCE,
                 "AMBIGUOUS_CLOSED_CIRCULAR_EDGE")
        _require(not _near(samples[2]["point_mm"], first["point_mm"]),
                 "AMBIGUOUS_CLOSED_CIRCULAR_EDGE")
        span = 360.0
    else:
        span = _advance(_angle(row["start_mm"], center), _angle(row["end_mm"], center), direction)
        _require(AXIS_COMPONENT_TOLERANCE < span < 360-AXIS_COMPONENT_TOLERANCE
                 and abs(span-observed_coverage) <= AXIS_COMPONENT_TOLERANCE,
                 "CONTRADICTORY_NATIVE_CIRCULAR_INTERVAL")
    row.update(circle_params_native=raw_circle, center_mm=center, axis=list(axis), radius_mm=radius,
               curve_edge_same_direction=sense,
               curve_parameter_data={"Sense": sense, "StartPoint": cp_start_raw,
                   "EndPoint": cp_end_raw, "UMinValue": lo, "UMaxValue": hi, "CurveType": kind},
               evaluations=[first, *samples], full_circle=closed,
               model_xy_traversal_direction="MODEL_XY_CCW" if direction > 0 else "MODEL_XY_CW",
               direction=direction, angular_coverage_deg=span,
               angular_span_deg=_angular_domain(_angle(row["start_mm"], center), span, direction))


def _collect_trim_coedge(extension, coedge, resolver):
    edge, reference, resolution = _edge_identity(extension, coedge, resolver)
    curve = _invoke_com_method(edge, "GetCurve")
    _require(curve is not None, "UNSUPPORTED_TRIM_CURVE")
    sense = _invoke_com_method(coedge, "GetSense")
    _require(type(sense) is bool, "unknown coedge sense")
    params = _vector(_invoke_com_method(coedge, "GetCurveParams"), 10, "GetCurveParams")
    row = {"edge_reference": reference, "edge_resolution": resolution,
           "coedge_edge_same_direction": sense, "coedge_parameters": params,
           "start_mm": _model_point(params[:3]), "end_mm": _model_point(params[3:6]),
           "source_label": "NATIVE_OBSERVED"}
    line = _invoke_com_method(curve, "IsLine")
    circle = _invoke_com_method(curve, "IsCircle")
    _require(type(line) is bool and type(circle) is bool and line != circle, "UNSUPPORTED_TRIM_CURVE")
    if line:
        raw = _vector(member(curve, "LineParams"), 6, "LineParams")
        origin, unit = _model_point(raw[:3]), _unit(from_native(raw[3:]))
        delta = _sub(row["end_mm"], row["start_mm"])
        _require(math.dist(row["start_mm"], row["end_mm"]) > LINEAR_TOLERANCE_MM,
                 "ZERO_LENGTH_LINE")
        for point in (row["start_mm"], row["end_mm"]):
            v = _sub(point, origin)
            residual = _sub(v, [x*_dot(v, unit) for x in unit])
            _require(math.sqrt(_dot(residual, residual)) <= LINEAR_TOLERANCE_MM,
                     "NATIVE_LINE_ENDPOINT_MISMATCH")
        _require(abs(abs(_dot(_unit(delta), unit))-1) <= AXIS_COMPONENT_TOLERANCE,
                 "NATIVE_LINE_DIRECTION_MISMATCH")
        row.update(curve_type="LINE", line_params_native=raw)
    else:
        row["curve_type"] = "CIRCLE"
        _circular_trim(coedge, edge, curve, row)
    return row


def _collect_raw_trim_loop_usages(extension, face, resolver):
    # Yield one acquisition at a time so ordinary normalization retains its
    # original failure boundary before touching the next loop.
    loops = list(_invoke_com_method(face, "GetLoops") or [])
    _require(loops, "missing native trim loops")
    for loop in loops:
        outer = _invoke_com_method(loop, "IsOuter")
        _require(type(outer) is bool, "unknown native IsOuter")
        coedges = list(_invoke_com_method(loop, "GetCoEdges") or [])
        _require(coedges, "OPEN_OR_AMBIGUOUS_TRIM_LOOP")
        rows = [_collect_trim_coedge(extension, c, resolver) for c in coedges]
        yield outer, coedges, rows


def _next_trim_coedge_usage(extension, coedge, resolver):
    nxt = _invoke_com_method(coedge, "GetNext")
    _, ref, _ = _edge_identity(extension, nxt, resolver)
    return nxt, ref


def _record_trim_edge_geometry(reuse, row):
    identity = row["edge_reference"]["encoded"]
    shape = [row["start_mm"], row["end_mm"]]
    prior = reuse.get(identity)
    if prior is not None:
        _require(prior["curve_type"] == row["curve_type"] and (
                 all(_near(a,b) for a,b in zip(prior["points"],shape))
                 or all(_near(a,b) for a,b in zip(prior["points"],reversed(shape)))),
                 "INCONSISTENT_REUSED_EDGE")
        if row["curve_type"] == "CIRCLE":
            _require(_near(prior["center_mm"],row["center_mm"])
                     and abs(prior["radius_mm"]-row["radius_mm"]) <= LINEAR_TOLERANCE_MM
                     and all(abs(a-b) <= AXIS_COMPONENT_TOLERANCE for a,b in
                             zip(prior["angular_span_deg"],row["angular_span_deg"])),
                     "INCONSISTENT_REUSED_EDGE")
    reuse[identity] = {"points":shape,"curve_type":row["curve_type"],
                      **{k:row[k] for k in ("center_mm","radius_mm","angular_span_deg") if k in row}}


def _trim_loops(extension, face, resolver):
    result, reuse = [], {}
    for outer, coedges, rows in _collect_raw_trim_loop_usages(extension, face, resolver):
        tokens = [r["edge_reference"]["encoded"] for r in rows]
        _require(len(set(tokens)) == len(tokens), "DUPLICATE_EDGE_CONTOUR")
        by_ref = dict(zip(tokens, rows))
        for coedge, row in zip(coedges, rows):
            _, ref = _next_trim_coedge_usage(extension, coedge, resolver)
            token = ref["encoded"]
            row["next_edge_reference"] = ref
            _require(token in by_ref and _near(row["end_mm"], by_ref[token]["start_mm"]),
                     "OPEN_OR_AMBIGUOUS_TRIM_LOOP")
            _record_trim_edge_geometry(reuse, row)
        chain, current = [], min(tokens)
        while current not in chain:
            chain.append(current)
            current = by_ref[current]["next_edge_reference"]["encoded"]
        _require(current == chain[0] and len(chain) == len(rows), "DISCONNECTED_COEDGE_CHAIN")
        result.append({"is_outer": outer, "coedges": [by_ref[t] for t in chain],
                       "input_edge_references": tokens, "closure_proven": True,
                       "source_label": "NATIVE_OBSERVED"})
    _require(sum(loop["is_outer"] for loop in result) == 1, "MISSING_OR_MULTIPLE_OUTER_LOOPS")
    return result


def collect_raw_coedge_usages(extension, face, registry, *, native_face_record, resolver=None):
    """Opt-in observation snapshot, independent of ordinary trim acceptance.

    Surface metadata comes from the existing native FACE record, bound to the
    independently resolved FACE/owner. No additional surface/API acquisition.
    Object references associate occurrences only within this call; if GetNext
    wrappers cannot be associated unambiguously, fail rather than match geometry.
    """
    owner = collect_face_owner(extension, face, registry, resolver=resolver)
    _require(isinstance(native_face_record, dict), "native face observation missing")
    observed_ref = native_face_record.get("reference", {})
    observed_owner = native_face_record.get("owner_reference", {})
    kind = native_face_record.get("surface_type")
    _require(isinstance(observed_ref, dict) and isinstance(observed_owner, dict)
             and observed_ref.get("reference_type") == "FACE"
             and observed_ref.get("encoded") == owner["reference"]["encoded"]
             and native_face_record.get("persistent_reference_resolved") is True
             and observed_owner.get("reference_type") == "FEATURE"
             and observed_owner.get("encoded") == owner["owner_reference"]["encoded"]
             and kind in {"PLANE", "CYLINDER"}, "native face observation binding mismatch")
    loops, reuse = [], {}
    for loop_ordinal, (outer, coedges, rows) in enumerate(
            _collect_raw_trim_loop_usages(extension, face, resolver)):
        # No persistent COEDGE family is assumed. These local handles only prove
        # distinct occurrences and traversal inside this retained observation.
        positions = {id(c): i for i, c in enumerate(coedges)}
        _require(len(positions) == len(coedges), "DUPLICATE_COEDGE_OCCURRENCE")
        next_positions = []
        for coedge, row in zip(coedges, rows):
            nxt, ref = _next_trim_coedge_usage(extension, coedge, resolver)
            index = positions.get(id(nxt))
            _require(index is not None and coedges[index] is nxt, "UNKNOWN_COEDGE_OCCURRENCE")
            _require(ref == rows[index]["edge_reference"]
                     and _near(row["end_mm"], rows[index]["start_mm"]),
                     "OPEN_OR_AMBIGUOUS_TRIM_LOOP")
            _record_trim_edge_geometry(reuse, row)
            next_positions.append(index)
            row.update(next_edge_reference=ref)
        ordered, current = [], 0
        while current not in ordered:
            ordered.append(current)
            current = next_positions[current]
        _require(current == ordered[0] and len(ordered) == len(rows),
                 "DISCONNECTED_COEDGE_USAGE_CHAIN")
        usages = []
        for index in ordered:
            row = deepcopy(rows[index])
            row.update(usage_ordinal=index, next_usage_ordinal=next_positions[index],
                       sense=row["coedge_edge_same_direction"],
                       edge_reference_resolved=row["edge_resolution"]["resolved"],
                       ordinal_source="OBSERVATION_ORDER_NOT_NATIVE_IDENTITY",
                       lineage={"face_reference":deepcopy(owner["reference"]),
                                "loop_ordinal":loop_ordinal,
                                "edge_reference":deepcopy(row["edge_reference"]),
                                "source":"ILoop2.GetCoEdges/ICoEdge.GetEdge/GetNext"})
            usages.append(row)
        loops.append({"is_outer":outer, "loop_ordinal":loop_ordinal,
                      "ordinal_source":"OBSERVATION_ORDER_NOT_NATIVE_IDENTITY",
                      "usage_count":len(usages), "coedge_usages":usages,
                      "traversal_closed":True, "source_label":"NATIVE_OBSERVED"})
    return {"face_reference":deepcopy(owner["reference"]),
            "persistent_reference_resolved":True, "face_resolution":owner["face_resolution"],
            "owner_reference":owner["owner_reference"], "surface_type":kind, "loops":loops,
            "source":"NATIVE_LOOP_COEDGE_USAGE_SNAPSHOT", "source_label":"NATIVE_OBSERVED",
            "field_sources":{"ordinal":"OBSERVATION_ORDER_NOT_NATIVE_IDENTITY",
                             "native_parameters":"NATIVE_OBSERVED",
                             "model_geometry":"DERIVED_FROM_NATIVE",
                             "surface_type":"EXISTING_NATIVE_FACE_RECORD"}}


def _plane_frame(surface):
    params = _vector(member(surface, "PlaneParams"), 6, "PlaneParams")
    normal, origin = _unit(from_native(params[:3])), _model_point(params[3:])
    axes = [i for i in range(3) if abs(abs(normal[i])-1) <= AXIS_COMPONENT_TOLERANCE]
    _require(len(axes) == 1 and all(abs(normal[j]) <= AXIS_COMPONENT_TOLERANCE
             for j in range(3) if j != axes[0]), "AMBIGUOUS_PLANE_BASIS")
    axis = axes[0]
    uv = [i for i in range(3) if i != axis]
    anchor = [0.0,0.0,0.0]; anchor[axis] = origin[axis]
    u = [int(i == uv[0]) for i in range(3)]
    v = [int(i == uv[1]) for i in range(3)]
    cross = [u[1]*v[2]-u[2]*v[1],u[2]*v[0]-u[0]*v[2],u[0]*v[1]-u[1]*v[0]]
    return {"origin_mm": anchor, "normal": cross, "observed_normal": normal,
            "u_axis": u, "v_axis": v, "axis": axis, "uv_axes": uv,
            "raw_native": params, "source_label": "DERIVED_FROM_NATIVE"}


def _rectangle(rows, uv):
    _require(len(rows) == 4 and all(r["curve_type"] == "LINE" for r in rows), "INVALID_RECTANGLE")
    points = [[r["start_mm"][i] for i in uv] for r in rows]
    vectors = [_sub(points[(i+1)%4], p) for i, p in enumerate(points)]
    units = [_unit(v) for v in vectors]
    _require(all(abs(_dot(units[i],units[(i+1)%4])) <= AXIS_COMPONENT_TOLERANCE for i in range(4))
             and all(abs(_dot(units[i],units[i+2])+1) <= AXIS_COMPONENT_TOLERANCE for i in range(2)),
             "INVALID_RECTANGLE")
    _require(all(any(abs(abs(x)-1) <= AXIS_COMPONENT_TOLERANCE for x in unit) for unit in units),
             "UNSUPPORTED_ROTATED_RECTANGLE")
    domain = [min(p[0] for p in points),max(p[0] for p in points),
              min(p[1] for p in points),max(p[1] for p in points)]
    corners = [[x,y] for x in domain[:2] for y in domain[2:]]
    _require(all(sum(_near(p,c) for p in points) == 1 for c in corners)
             and domain[1]-domain[0] > LINEAR_TOLERANCE_MM
             and domain[3]-domain[2] > LINEAR_TOLERANCE_MM, "INVALID_RECTANGLE")
    return domain


def _circle_chain(rows, *, full):
    _require(rows and all(r["curve_type"] == "CIRCLE" for r in rows), "INVALID_CIRCULAR_CONTOUR")
    first = rows[0]
    _require(all(_near(r["center_mm"], first["center_mm"])
                 and abs(r["radius_mm"]-first["radius_mm"]) <= LINEAR_TOLERANCE_MM
                 and r["direction"] == first["direction"] for r in rows), "INCONSISTENT_CIRCLE_CONTOUR")
    _require(all(_near(a["end_mm"], b["start_mm"]) for a,b in zip(rows,rows[1:])),
             "INCOMPLETE_CIRCULAR_COVERAGE")
    span = sum(r["angular_coverage_deg"] for r in rows)
    if full:
        _require(_near(rows[-1]["end_mm"], first["start_mm"])
                 and abs(span-360) <= AXIS_COMPONENT_TOLERANCE, "INCOMPLETE_CIRCULAR_COVERAGE")
    else:
        _require(AXIS_COMPONENT_TOLERANCE < span < 360-AXIS_COMPONENT_TOLERANCE,
                 "AMBIGUOUS_ANGULAR_DOMAIN")
    return first["center_mm"], first["radius_mm"], _angular_domain(
        _angle(first["start_mm"], first["center_mm"]), span, first["direction"])


def _slot_cutout(rows):
    lines = [r for r in rows if r["curve_type"] == "LINE"]
    arcs = [r for r in rows if r["curve_type"] == "CIRCLE"]
    _require(len(rows) == 4 and len(lines) == len(arcs) == 2, "UNSUPPORTED_INNER_CONTOUR")
    a,b = arcs; radius = a["radius_mm"]
    _require(abs(radius-b["radius_mm"]) <= LINEAR_TOLERANCE_MM
             and all(abs(r["angular_coverage_deg"]-180) <= AXIS_COMPONENT_TOLERANCE for r in arcs)
             and a["direction"] == b["direction"], "INVALID_SLOT_CONTOUR")
    delta = _sub(b["center_mm"],a["center_mm"])
    unit = _unit(delta)
    axis = next((i for i in (0,1) if abs(abs(unit[i])-1) <= AXIS_COMPONENT_TOLERANCE), None)
    _require(axis is not None and abs(unit[2]) <= AXIS_COMPONENT_TOLERANCE, "INVALID_SLOT_CONTOUR")
    side = 1-axis
    for r in arcs:
        _require(all(abs(p[axis]-r["center_mm"][axis]) <= LINEAR_TOLERANCE_MM
                     and abs(abs(p[side]-r["center_mm"][side])-radius) <= LINEAR_TOLERANCE_MM
                     for p in (r["start_mm"],r["end_mm"])), "INVALID_SLOT_CONTOUR")
        midpoint = r["evaluations"][3]["point_mm"]
        outward = r["center_mm"][axis]-(b if r is a else a)["center_mm"][axis]
        _require((midpoint[axis]-r["center_mm"][axis])*outward > 0, "INVALID_SLOT_CONTOUR")
    _require(all(abs(abs(_unit(_sub(r["end_mm"],r["start_mm"]))[axis])-1)
                 <= AXIS_COMPONENT_TOLERANCE for r in lines), "INVALID_SLOT_CONTOUR")
    center = [(x+y)/2 for x,y in zip(a["center_mm"],b["center_mm"])]
    return ["STRAIGHT_SLOT", "X" if axis == 0 else "Y", *center[:2],2*radius,math.dist(a["center_mm"],b["center_mm"])+2*radius]


def _planar_domain(loops, frame):
    axis, uv = frame["axis"], frame["uv_axes"]
    for loop in loops:
        for row in loop["coedges"]:
            points = [row["start_mm"],row["end_mm"]]
            points += [s["point_mm"] for s in row.get("evaluations",[])]
            _require(all(abs(p[axis]-frame["origin_mm"][axis]) <= LINEAR_TOLERANCE_MM for p in points),
                     "OFF_PLANE_TRIM_POINT")
            row["start_uv_mm"] = [row["start_mm"][i] for i in uv]
            row["end_uv_mm"] = [row["end_mm"][i] for i in uv]
    outer = next(loop["coedges"] for loop in loops if loop["is_outer"])
    inner = [loop["coedges"] for loop in loops if not loop["is_outer"]]
    if all(r["curve_type"] == "LINE" for r in outer):
        domain, cutouts = _rectangle(outer,uv), []
        for rows in inner:
            if all(r["curve_type"] == "LINE" for r in rows):
                shape = ["RECTANGLE", *_rectangle(rows,uv)]
                extent = shape[1:]
            else:
                _require(uv == [0,1], "UNSUPPORTED_CIRCULAR_ORIENTATION")
                if all(r["curve_type"] == "CIRCLE" for r in rows):
                    center,radius,_ = _circle_chain(rows,full=True)
                    shape = ["CIRCLE", *center[:2],radius]
                    extent = [center[0]-radius,center[0]+radius,center[1]-radius,center[1]+radius]
                else:
                    shape = _slot_cutout(rows)
                    _,major,x,y,w,length = shape
                    dx,dy = (length/2,w/2) if major == "X" else (w/2,length/2)
                    extent = [x-dx,x+dx,y-dy,y+dy]
            _require(all(domain[i]+LINEAR_TOLERANCE_MM < extent[i] if i%2 == 0
                         else extent[i] < domain[i]-LINEAR_TOLERANCE_MM for i in range(4)),
                     "CUTOUT_OUTSIDE_OUTER_CONTOUR")
            for _,prior in cutouts:
                _require(extent[1] < prior[0] or prior[1] < extent[0]
                         or extent[3] < prior[2] or prior[3] < extent[2], "UNSUPPORTED_OVERLAPPING_CUTOUT_EXTENTS")
            cutouts.append((shape,extent))
        return {"kind":"RECTANGLE_WITH_CUTOUTS", "outer_uv_mm":domain,
                "cutouts":sorted([shape for shape,_ in cutouts],key=repr)}
    _require(not inner and uv == [0,1], "UNSUPPORTED_PLANAR_TRIM")
    # Rotate by topology to make an arc run contiguous, regardless of start edge.
    split = next((i for i,r in enumerate(outer) if r["curve_type"] == "LINE"),None)
    if split is None:
        center,radius,angles = _circle_chain(outer,full=True)
        coverage = "FULL_DISK"
    else:
        ordered = outer[split:]+outer[:split]
        lines = [r for r in ordered if r["curve_type"] == "LINE"]
        arcs = [r for r in ordered if r["curve_type"] == "CIRCLE"]
        _require(len(lines) == 2 and len(arcs) == len(outer)-2, "UNSUPPORTED_PLANAR_TRIM")
        center,radius,angles = _circle_chain(arcs,full=False)
        _require(all(_near(r["start_mm"],center) or _near(r["end_mm"],center) for r in lines),
                 "INVALID_SECTOR_RADIAL_LINES")
        _require(angles[1] <= 360+AXIS_COMPONENT_TOLERANCE, "UNSUPPORTED_WRAPPED_DISK_SECTOR")
        coverage = "PARTIAL_SECTOR"
    return {"kind":"DISK_SECTOR", "center_xy_mm":center[:2], "radius_mm":radius,
            "angular_span_deg":angles, "coverage":coverage}


def _cylinder_domain(loops, surface):
    raw = _vector(member(surface, "CylinderParams"), 7, "CylinderParams")
    axis, center, radius = _z_axis(from_native(raw[3:6])), _model_point(raw[:3]),raw[6]*1000
    _require(radius > LINEAR_TOLERANCE_MM, "invalid native cylinder radius")
    circles = [r for loop in loops for r in loop["coedges"] if r["curve_type"] == "CIRCLE"]
    lines = [r for loop in loops for r in loop["coedges"] if r["curve_type"] == "LINE"]
    _require(circles and all(_near(r["center_mm"][:2],center[:2])
             and abs(r["radius_mm"]-radius) <= LINEAR_TOLERANCE_MM for r in circles),
             "CYLINDER_TRIM_ANALYTIC_MISMATCH")
    zs = sorted({round(r["center_mm"][2],9) for r in circles})
    _require(len(zs) == 2 and zs[1]-zs[0] > LINEAR_TOLERANCE_MM, "UNSUPPORTED_CYLINDER_CONTOUR")
    if not lines:
        _require(len(loops) == 2, "UNSUPPORTED_CYLINDER_CONTOUR")
        for loop in loops:
            _circle_chain(loop["coedges"],full=True)
        angles = [0.0,360.0]
    else:
        _require(len(loops) == 1 and len(lines) == 2 and len(circles) == 2, "UNSUPPORTED_CYLINDER_CONTOUR")
        angles = circles[0]["angular_span_deg"]
        _require(all(abs(a-b) <= AXIS_COMPONENT_TOLERANCE for a,b in zip(angles,circles[1]["angular_span_deg"]))
                 and circles[0]["direction"] == -circles[1]["direction"], "AMBIGUOUS_ANGULAR_DOMAIN")
        for row in lines:
            a,b = row["start_mm"],row["end_mm"]
            _require(_near(a[:2],b[:2]) and abs(math.dist(a[:2],center[:2])-radius) <= LINEAR_TOLERANCE_MM
                     and _near(sorted([a[2],b[2]]),zs), "INVALID_CYLINDER_AXIAL_LINE")
        if angles[0] > 180:
            angles = [x-360 for x in angles]
    return {"kind":"CYLINDER_RECTANGLE", "coordinate_convention":"MODEL_XY_CCW_DEGREES",
            "angular_span_deg":angles, "z_span_mm":zs}, {
            "center_mm":center, "axis":axis, "radius_mm":radius, "raw_native":raw}


def collect_face_trim(extension, face, registry, *, resolver=None):
    """Opt-in producer only: preserve each native FACE; no fragment/lifecycle classification.

    Ordinary precise edges supply analytic proof. Unsupported parameter encodings,
    unsupported footprints and missing directed geometry fail before a domain is emitted.
    """
    owner = collect_face_owner(extension,face,registry,resolver=resolver)
    uv_bounds = _vector(_invoke_com_method(face, "GetUVBounds"),4,"GetUVBounds")
    _require(uv_bounds[0] < uv_bounds[1] and uv_bounds[2] < uv_bounds[3], "invalid native surface parameter bounds")
    surface = member(face,"GetSurface")
    loops = _trim_loops(extension,face,resolver)
    if member(surface,"IsPlane") is True:
        frame = _plane_frame(surface)
        domain = _planar_domain(loops,frame)
        geometry = {"plane_frame":frame}
        kind = "PLANE"
    elif member(surface,"IsCylinder") is True:
        domain,geometry = _cylinder_domain(loops,surface)
        sense = member(face,"FaceInSurfaceSense")
        _require(type(sense) is bool, "unknown native surface sense")
        geometry["interior"] = sense
        kind = "CYLINDER"
    else:
        raise NativeEvidenceUnavailable("UNSUPPORTED_NATIVE_SURFACE")
    lineage = sorted(r["edge_reference"]["encoded"] for loop in loops for r in loop["coedges"])
    domain.update(source="NATIVE_ANALYTIC_TRIM_LOOPS",face_reference=owner["reference"]["encoded"],
                  persistent_reference_resolved=True, native_edge_lineage=lineage,
                  source_label="DERIVED_FROM_NATIVE")
    return {**owner, "surface_type":kind, "surface_geometry":geometry,
            "face_trim_raw":{"uv_bounds":uv_bounds,"loops":loops,"source_label":"NATIVE_OBSERVED"},
            "trim_domain":domain, "field_sources":{"raw":"NATIVE_OBSERVED",
                "model_coordinate_conversion":"DERIVED_FROM_NATIVE","uv":"DERIVED_FROM_NATIVE",
                "trim_domain":"DERIVED_FROM_NATIVE"}}


def _n1c_snapshot(payload):
    """Compare retained capture values independently of presentation order."""
    result=deepcopy(payload)
    if not isinstance(result,dict):
        return result
    if "edges" in result:
        rows=result["edges"]
        _require(isinstance(rows,list) and all(isinstance(r,dict) for r in rows),
                 "INVALID_OBSERVATION_RECEIPT")
        result["edges"]=sorted(rows,key=lambda r:str(r.get("reference",{}).get("encoded")))
    if "loops" in result:
        loops=result["loops"]
        _require(isinstance(loops,list) and all(isinstance(r,dict) for r in loops),
                 "INVALID_OBSERVATION_RECEIPT")
        for loop in loops:
            rows=loop.get("coedge_usages",[])
            _require(isinstance(rows,list) and all(isinstance(r,dict) for r in rows),
                     "INVALID_OBSERVATION_RECEIPT")
            loop["coedge_usages"]=sorted(rows,key=lambda r:str(r.get("usage_ordinal")))
        result["loops"]=sorted(loops,key=lambda r:str(r.get("loop_ordinal")))
    return result


def _n1c_reference(record,kind):
    _require(isinstance(record,dict) and record.get("reference_type")==kind
             and record.get("source")=="IModelDocExtension.GetPersistReference3",
             "UNRESOLVED_OR_WRONG_TYPED_REFERENCE")
    encoded=record.get("encoded")
    try:
        raw=base64.b64decode(encoded,validate=True)
        valid=bool(raw) and base64.b64encode(raw).decode("ascii")==encoded
    except (TypeError,ValueError):
        valid=False
    _require(valid,"UNRESOLVED_OR_WRONG_TYPED_REFERENCE")
    return encoded


def _n1c_vector(value,length):
    _require(isinstance(value,(list,tuple)) and len(value)==length
             and all(type(x) in (int,float) and math.isfinite(x) for x in value),
             "NONFINITE_OR_INCOMPLETE_NATIVE_EVIDENCE")
    return list(value)


def _n1c_equal(a,b):
    if isinstance(a,(list,tuple)) and isinstance(b,(list,tuple)):
        return len(a)==len(b) and all(_n1c_equal(x,y) for x,y in zip(a,b))
    return (type(a) in (int,float) and type(b) in (int,float)
            and math.isfinite(a) and math.isfinite(b) and abs(a-b)<=AXIS_COMPONENT_TOLERANCE)


def _n1c_resolution(proof,reference,kind):
    _require(isinstance(proof,dict) and proof.get("resolved") is True
             and proof.get("resolved_type")==kind and proof.get("resolution_error")==0
             and _n1c_reference(proof,kind)==_n1c_reference(reference,kind),
             "UNRESOLVED_OR_WRONG_TYPED_REFERENCE")


def _n1c_observation(observation):
    """Require an acquisition receipt; this function never issues one.

    Retained object associations are local observation lineage, never native
    ownership or persistence. Legacy independent snapshots have no such proof.
    """
    _require(isinstance(observation,dict),"SAME_OBSERVATION_PROVENANCE_UNAVAILABLE")
    receipt=observation.get("capture_record",{})
    _require(isinstance(receipt,dict) and receipt.get("source")=="CONTROLLED_FACE_OBSERVATION"
             and observation.get("scope") is not None
             and receipt.get("scope") is observation["scope"]
             and isinstance(receipt.get("observation_token"),str) and receipt["observation_token"],
             "SAME_OBSERVATION_PROVENANCE_UNAVAILABLE")
    members=receipt.get("members",{});values=receipt.get("values",{})
    _require(isinstance(members,dict) and isinstance(values,dict),"INVALID_OBSERVATION_RECEIPT")
    for role in ("native_face","raw_usage","normalized_face"):
        _require(role in observation and role in members and role in values
                 and members[role] is observation[role]
                 and _n1c_snapshot(values[role])==_n1c_snapshot(observation[role]),
                 "CROSS_OBSERVATION_OR_MUTATED_EVIDENCE")
    native=observation["native_face"];raw=observation["raw_usage"]
    _require(isinstance(native,dict) and isinstance(raw,dict),"INCOMPLETE_NATIVE_OBSERVATION")
    face=_n1c_reference(native.get("reference"),"FACE")
    _require(_n1c_reference(raw.get("face_reference"),"FACE")==face
             and _n1c_reference(receipt.get("face_reference"),"FACE")==face
             and _n1c_reference(native.get("owner_reference"),"FEATURE")
                 ==_n1c_reference(raw.get("owner_reference"),"FEATURE")
             and native.get("persistent_reference_resolved") is True
             and raw.get("persistent_reference_resolved") is True
             and raw.get("source")=="NATIVE_LOOP_COEDGE_USAGE_SNAPSHOT"
             and raw.get("source_label")=="NATIVE_OBSERVED"
             and native.get("surface_type")==raw.get("surface_type"),"INCOMPLETE_NATIVE_OBSERVATION")
    _n1c_resolution(raw.get("face_resolution"),raw["face_reference"],"FACE")
    loops=raw.get("loops")
    _require(isinstance(loops,list) and loops,"OPEN_RAW_LOOP")
    all_rows=[];edge_rows={};loop_indices=set()
    for loop in loops:
        _require(isinstance(loop,dict),"OPEN_RAW_LOOP")
        rows=loop.get("coedge_usages");index=loop.get("loop_ordinal")
        _require(type(index) is int and index>=0 and index not in loop_indices
                 and type(loop.get("is_outer")) is bool and loop.get("traversal_closed") is True
                 and isinstance(rows,list) and rows and loop.get("usage_count")==len(rows),"OPEN_RAW_LOOP")
        loop_indices.add(index);by_usage={}
        for row in rows:
            _require(isinstance(row,dict),"AMBIGUOUS_SEAM_TRAVERSAL")
            ordinal=row.get("usage_ordinal")
            _require(type(ordinal) is int and ordinal>=0 and ordinal not in by_usage
                     and row.get("ordinal_source")=="OBSERVATION_ORDER_NOT_NATIVE_IDENTITY"
                     and row.get("source_label")=="NATIVE_OBSERVED"
                     and type(row.get("sense")) is bool
                     and row["sense"]==row.get("coedge_edge_same_direction"),"AMBIGUOUS_SEAM_TRAVERSAL")
            edge=_n1c_reference(row.get("edge_reference"),"EDGE")
            _n1c_resolution(row.get("edge_resolution"),row["edge_reference"],"EDGE")
            _require(row.get("edge_reference_resolved") is True,"UNRESOLVED_OR_WRONG_TYPED_REFERENCE")
            lineage=row.get("lineage",{})
            _require(isinstance(lineage,dict) and lineage.get("loop_ordinal")==index
                     and _n1c_reference(lineage.get("face_reference"),"FACE")==face
                     and _n1c_reference(lineage.get("edge_reference"),"EDGE")==edge,
                     "SAME_OBSERVATION_PROVENANCE_UNAVAILABLE")
            params=_n1c_vector(row.get("coedge_parameters"),10)
            a=_n1c_vector(row.get("start_mm"),3);b=_n1c_vector(row.get("end_mm"),3)
            _require(_n1c_equal(a,_model_point(params[:3])) and _n1c_equal(b,_model_point(params[3:6])),
                     "NATIVE_PARAMETER_POINT_MISMATCH")
            by_usage[ordinal]=row;edge_rows.setdefault(edge,[]).append(row);all_rows.append(row)
        for row in rows:
            nxt=by_usage.get(row.get("next_usage_ordinal"))
            _require(nxt is not None and _n1c_reference(row.get("next_edge_reference"),"EDGE")
                     ==_n1c_reference(nxt["edge_reference"],"EDGE")
                     and _n1c_equal(row["end_mm"],nxt["start_mm"]),"OPEN_RAW_LOOP")
        visited=set();current=min(by_usage)
        while current not in visited:
            visited.add(current);current=by_usage[current]["next_usage_ordinal"]
        _require(current==min(by_usage) and len(visited)==len(rows),"AMBIGUOUS_SEAM_TRAVERSAL")
    retained=receipt.get("usage_rows")
    _require(isinstance(retained,(list,tuple)) and len(retained)==len(all_rows)
             and len({id(r) for r in all_rows})==len(all_rows)
             and all(sum(r is original for original in retained)==1 for r in all_rows),
             "FABRICATED_OR_FOREIGN_COEDGE_USAGE")
    return native,raw,edge_rows


def _n1c_line(row):
    _require(row.get("curve_type")=="LINE","UNSUPPORTED_SPLIT_BOUNDARY")
    raw=_n1c_vector(row.get("line_params_native"),6)
    origin=_model_point(raw[:3]);unit=_unit(from_native(raw[3:]))
    a=_n1c_vector(row.get("start_mm"),3);b=_n1c_vector(row.get("end_mm"),3)
    _require(math.dist(a,b)>AXIS_COMPONENT_TOLERANCE,"ZERO_SPLIT_SPAN")
    for point in (a,b):
        delta=_sub(point,origin)
        _require(_n1c_equal(delta,[v*_dot(delta,unit) for v in unit]),"NONCONSTANT_NATIVE_LINE")
    return a,b,unit


def _n1c_cylinder(native):
    _require(native.get("surface_type")=="CYLINDER","UNSUPPORTED_ANALYTIC_SURFACE")
    center=_n1c_vector(native.get("center_mm"),3);axis=_z_axis(_n1c_vector(native.get("axis"),3))
    radius=native.get("radius_mm")
    _require(type(radius) in (int,float) and math.isfinite(radius) and radius>LINEAR_TOLERANCE_MM
             and type(native.get("interior")) is bool,"INCOMPLETE_CYLINDER_GEOMETRY")
    return center,list(axis),radius


def _n1c_circle(row,center,axis,radius):
    _require(row.get("curve_type")=="CIRCLE","UNSUPPORTED_SPLIT_BOUNDARY")
    raw=_n1c_vector(row.get("circle_params_native"),7)
    c=_n1c_vector(row.get("center_mm"),3);r=row.get("radius_mm")
    circle_axis=_z_axis(_n1c_vector(row.get("axis"),3))
    _require(_n1c_equal(c,_model_point(raw[:3])) and _n1c_equal(c[:2],center[:2])
             and _n1c_equal(r,raw[6]*1000) and _n1c_equal(r,radius)
             and _n1c_equal(circle_axis,_z_axis(from_native(raw[3:6]))),"INCOMPATIBLE_CIRCULAR_BOUNDARIES")
    angles=_n1c_vector(row.get("angular_span_deg"),2);coverage=row.get("angular_coverage_deg")
    _require(_n1c_equal(angles[1]-angles[0],coverage)
             and AXIS_COMPONENT_TOLERANCE<coverage<=360+AXIS_COMPONENT_TOLERANCE
             and row.get("direction") in (-1,1) and type(row.get("full_circle")) is bool,
             "UNPROVEN_PERIODIC_WITNESS")
    points=[row["start_mm"],row["end_mm"]]
    samples=row.get("evaluations")
    _require(isinstance(samples,list) and len(samples)>=5,"UNPROVEN_PERIODIC_WITNESS")
    for sample in samples:
        _require(isinstance(sample,dict),"UNPROVEN_PERIODIC_WITNESS")
        point=_n1c_vector(sample.get("point_mm"),3);tangent=_n1c_vector(sample.get("tangent_mm"),3)
        radial=_sub(point,c)
        _require(_n1c_equal(point[2],c[2]) and _n1c_equal(math.dist(point,c),radius)
                 and abs(tangent[2])<=AXIS_COMPONENT_TOLERANCE
                 and abs(_dot(_unit(radial),_unit(tangent)))<=AXIS_COMPONENT_TOLERANCE
                 and sample.get("direction")==row["direction"],"INCOMPATIBLE_CIRCULAR_BOUNDARIES")
        points.append(point)
    _require(all(_n1c_equal(p[2],c[2]) and _n1c_equal(math.dist(p,c),radius) for p in points),
             "NONCONSTANT_Z_CIRCLE")
    return c,angles


def _n1c_same_edge(a,b):
    _require(a.get("curve_type")==b.get("curve_type")
             and _n1c_equal(a["start_mm"],b["end_mm"])
             and _n1c_equal(a["end_mm"],b["start_mm"]),"INCOMPATIBLE_SHARED_EDGE_GEOMETRY")
    if a["curve_type"]=="LINE":
        _,_,u=_n1c_line(a);_,_,v=_n1c_line(b)
        _require(_n1c_equal(u,v) and _n1c_equal(a["line_params_native"],b["line_params_native"]),
                 "INCOMPATIBLE_SHARED_EDGE_GEOMETRY")
    else:
        _require(_n1c_equal(a.get("center_mm"),b.get("center_mm"))
                 and _n1c_equal(a.get("radius_mm"),b.get("radius_mm"))
                 and a.get("direction")==-b.get("direction",0),"INCOMPATIBLE_SHARED_EDGE_GEOMETRY")
    _require(a["sense"]!=b["sense"],"AMBIGUOUS_SEAM_TRAVERSAL")


def derive_periodic_domain_witness(observation):
    """Opt-in phase-free proof from one retained acquisition, never N1B PASS."""
    native,raw,rows=_n1c_observation(observation)
    center,axis,radius=_n1c_cylinder(native)
    loops=raw["loops"]
    _require(len(loops)==1 and loops[0]["is_outer"],"UNPROVEN_PERIODIC_WITNESS")
    usages=loops[0]["coedge_usages"]
    lines=[r for r in usages if r.get("curve_type")=="LINE"]
    circles=[r for r in usages if r.get("curve_type")=="CIRCLE"]
    _require(len(usages)==4 and len(lines)==len(circles)==2
             and _n1c_reference(lines[0]["edge_reference"],"EDGE")
                 ==_n1c_reference(lines[1]["edge_reference"],"EDGE")
             and lines[0]["usage_ordinal"]!=lines[1]["usage_ordinal"],"UNPROVEN_PERIODIC_WITNESS")
    _n1c_same_edge(lines[0],lines[1])
    a,b,unit=_n1c_line(lines[0])
    _require(_n1c_equal(a[:2],b[:2]) and abs(abs(unit[2])-1)<=AXIS_COMPONENT_TOLERANCE
             and _n1c_equal(math.dist(a[:2],center[:2]),radius),"AMBIGUOUS_PERIODIC_SEAM")
    zs=sorted([a[2],b[2]])
    _require(zs[1]-zs[0]>AXIS_COMPONENT_TOLERANCE,"ZERO_SPLIT_SPAN")
    heights=[]
    for row in circles:
        c,angles=_n1c_circle(row,center,axis,radius);heights.append(c[2])
        _require(row["full_circle"] is True and _n1c_equal(angles[1]-angles[0],360)
                 and _n1c_equal(row["start_mm"][:2],a[:2]),"UNPROVEN_PERIODIC_WITNESS")
    _require(_n1c_equal(sorted(heights),zs) and circles[0]["direction"]==-circles[1]["direction"],
             "AMBIGUOUS_PERIODIC_SEAM")
    seam=_n1c_reference(lines[0]["edge_reference"],"EDGE");face=native["reference"]["encoded"]
    edges=native.get("edges",[])
    _require(isinstance(edges,list) and len(edges)==1
             and _n1c_reference(edges[0].get("reference"),"EDGE")==seam
             and edges[0].get("adjacent_face_references")==[face,face],"NONRECIPROCAL_EDGE_INCIDENCE")
    receipt=observation["capture_record"]
    return {"source":"NATIVE_PERIODIC_DOMAIN_WITNESS","source_label":"DERIVED_FROM_NATIVE",
            "face_reference":deepcopy(native["reference"]),"surface_type":"CYLINDER","period_deg":360.0,
            "z_span_mm":zs,"center_mm":center,"axis_model":axis,"radius_mm":radius,
            "same_observation_provenance":{"source":receipt["source"],
                "observation_token":receipt["observation_token"],"face_reference":face},
            "native_lineage":{"seam_edge_reference":deepcopy(lines[0]["edge_reference"]),
                "circular_boundary_references":sorted([deepcopy(r["edge_reference"]) for r in circles],key=lambda r:r["encoded"]),
                "loop_ordinal":loops[0]["loop_ordinal"],
                "usage_ordinals":sorted(r["usage_ordinal"] for r in lines),
                "ordinal_source":"OBSERVATION_ORDER_NOT_NATIVE_IDENTITY"}}


def _n1c_domain(observation,edge):
    normalized=observation.get("normalized_face")
    _require(isinstance(normalized,dict),"NORMALIZED_SPLIT_DOMAIN_UNAVAILABLE")
    native=observation["native_face"];trim=normalized.get("trim_domain",{})
    _require(_n1c_reference(normalized.get("reference"),"FACE")==native["reference"]["encoded"]
             and normalized.get("surface_type")==native["surface_type"]
             and isinstance(trim,dict) and trim.get("source")=="NATIVE_ANALYTIC_TRIM_LOOPS"
             and trim.get("face_reference")==native["reference"]["encoded"]
             and trim.get("persistent_reference_resolved") is True
             and edge in trim.get("native_edge_lineage",[]),"NORMALIZED_SPLIT_DOMAIN_UNAVAILABLE")
    rows=[r for loop in normalized.get("face_trim_raw",{}).get("loops",[]) for r in loop.get("coedges",[])
          if _n1c_reference(r.get("edge_reference"),"EDGE")==edge]
    _require(len(rows)==1,"AMBIGUOUS_SPLIT_BOUNDARY")
    raw_rows=[r for loop in observation["raw_usage"]["loops"] for r in loop["coedge_usages"]
              if r["edge_reference"]["encoded"]==edge]
    _require(len(raw_rows)==1 and rows[0]["curve_type"]==raw_rows[0]["curve_type"]
             and _n1c_equal(rows[0]["start_mm"],raw_rows[0]["start_mm"])
             and _n1c_equal(rows[0]["end_mm"],raw_rows[0]["end_mm"]),"AMBIGUOUS_SPLIT_BOUNDARY")
    return normalized,trim


def _n1c_boundary(a,b,axis,coordinate,span):
    a=_n1c_vector(a,4);b=_n1c_vector(b,4);k=2*axis;t=2*(1-axis)
    _require(all(row[i+1]-row[i]>AXIS_COMPONENT_TOLERANCE for row in (a,b) for i in (0,2)),
             "AMBIGUOUS_SPLIT_BOUNDARY")
    touching=((_n1c_equal(a[k+1],b[k]) and _n1c_equal(coordinate,a[k+1]) and _n1c_equal(coordinate,b[k]))
              or (_n1c_equal(b[k+1],a[k]) and _n1c_equal(coordinate,b[k+1]) and _n1c_equal(coordinate,a[k])))
    overlap=[max(a[t],b[t]),min(a[t+1],b[t+1])]
    _require(touching and overlap[1]-overlap[0]>AXIS_COMPONENT_TOLERANCE
             and _n1c_equal(span,overlap),"AMBIGUOUS_SPLIT_BOUNDARY")


def _n1c_angle_boundary(a,b,coordinate,span):
    """A physical axial EDGE must meet one distinct-FACE angular interface."""
    a=_n1c_vector(a,4);b=_n1c_vector(b,4)
    coordinate=_n1c_vector([coordinate],1)[0];span=_n1c_vector(span,2)
    _require(all(AXIS_COMPONENT_TOLERANCE<r[1]-r[0]<360-AXIS_COMPONENT_TOLERANCE
                 and r[3]-r[2]>AXIS_COMPONENT_TOLERANCE for r in (a,b)),
             "AMBIGUOUS_SPLIT_BOUNDARY")
    def equivalent(x,y):
        return abs(math.remainder((x%360)-(y%360),360))<=AXIS_COMPONENT_TOLERANCE
    relations=[(x,y) for x,y in ((a[1],b[0]),(b[1],a[0]))
               if equivalent(x,y) and equivalent(coordinate,x) and equivalent(coordinate,y)]
    overlap=[max(a[2],b[2]),min(a[3],b[3])]
    _require(len(relations)==1 and overlap[1]-overlap[0]>AXIS_COMPONENT_TOLERANCE
             and _n1c_equal(span,overlap),"AMBIGUOUS_SPLIT_BOUNDARY")


def _n1c_split(left,right,edge,a,b):
    first,da=_n1c_domain(left,edge);second,db=_n1c_domain(right,edge)
    _n1c_same_edge(a,b)
    kind=first["surface_type"]
    _require(kind==second["surface_type"],"INCOMPATIBLE_ANALYTIC_SURFACE")
    if kind=="PLANE":
        fa=first.get("surface_geometry",{}).get("plane_frame",{})
        fb=second.get("surface_geometry",{}).get("plane_frame",{})
        _require(all(_n1c_equal(fa.get(key),fb.get(key)) for key in
                     ("origin_mm","normal","observed_normal","u_axis","v_axis"))
                 and da.get("kind")==db.get("kind")=="RECTANGLE_WITH_CUTOUTS",
                 "INCOMPATIBLE_PLANAR_FRAME")
        p,q,_=_n1c_line(a)
        origin=_n1c_vector(fa.get("origin_mm"),3)
        u=_n1c_vector(fa.get("u_axis"),3);v=_n1c_vector(fa.get("v_axis"),3)
        for observation,frame in ((left,fa),(right,fb)):
            native=observation["native_face"]
            normal=_unit(_n1c_vector(native.get("normal"),3))
            native_origin=_n1c_vector(native.get("origin_mm"),3)
            plane_axis=frame.get("axis")
            _require(type(plane_axis) is int and plane_axis in (0,1,2)
                     and frame.get("uv_axes")==[i for i in range(3) if i!=plane_axis]
                     and _n1c_equal(normal,frame.get("observed_normal"))
                     and abs(abs(normal[plane_axis])-1)<=AXIS_COMPONENT_TOLERANCE
                     and all(abs(normal[i])<=AXIS_COMPONENT_TOLERANCE for i in range(3) if i!=plane_axis)
                     and _n1c_equal(native_origin[plane_axis],origin[plane_axis])
                     and all(abs(_dot(_sub(point,origin),normal))<=AXIS_COMPONENT_TOLERANCE
                             for point in (p,q)),"INCOMPATIBLE_PLANAR_FRAME")
        points=[[_dot(_sub(point,origin),u),_dot(_sub(point,origin),v)] for point in (p,q)]
        candidates=[i for i in (0,1) if _n1c_equal(points[0][i],points[1][i])
                    and abs(points[1][1-i]-points[0][1-i])>AXIS_COMPONENT_TOLERANCE]
        _require(len(candidates)==1,"UNSUPPORTED_SPLIT_BOUNDARY")
        axis=candidates[0];coordinate=(points[0][axis]+points[1][axis])/2
        span=sorted([points[0][1-axis],points[1][1-axis]])
        _n1c_boundary(da.get("outer_uv_mm"),db.get("outer_uv_mm"),axis,coordinate,span)
        parameter=("U","V")[axis]
    elif kind=="CYLINDER":
        ca,aa,ra=_n1c_cylinder(left["native_face"]);cb,ab,rb=_n1c_cylinder(right["native_face"])
        _require(_n1c_equal(ca,cb) and _n1c_equal(aa,ab) and _n1c_equal(ra,rb)
                 and left["native_face"]["interior"]==right["native_face"]["interior"]
                 and da.get("kind")==db.get("kind")=="CYLINDER_RECTANGLE"
                 and da.get("coordinate_convention")==db.get("coordinate_convention")=="MODEL_XY_CCW_DEGREES",
                 "INCOMPATIBLE_CYLINDER_GEOMETRY")
        for normalized,center,axis,radius in ((first,ca,aa,ra),(second,cb,ab,rb)):
            geometry=normalized.get("surface_geometry",{})
            _require(isinstance(geometry,dict) and _n1c_equal(geometry.get("center_mm"),center)
                     and _n1c_equal(geometry.get("axis"),axis)
                     and _n1c_equal(geometry.get("radius_mm"),radius),"INCOMPATIBLE_CYLINDER_GEOMETRY")
        domains=[_n1c_vector(d.get("angular_span_deg"),2)+_n1c_vector(d.get("z_span_mm"),2) for d in (da,db)]
        if a["curve_type"]=="LINE":
            p,q,unit=_n1c_line(a)
            _require(_n1c_equal(p[:2],q[:2]) and abs(abs(unit[2])-1)<=AXIS_COMPONENT_TOLERANCE
                     and _n1c_equal(math.dist(p[:2],ca[:2]),ra),"NONCONSTANT_ANGLE_LINE")
            parameter="ANGLE";axis=0;coordinate=round(_angle(p,ca),9)%360;span=sorted([p[2],q[2]])
            _n1c_angle_boundary(*domains,coordinate,span)
        elif a["curve_type"]=="CIRCLE":
            c,angles=_n1c_circle(a,ca,aa,ra);_n1c_circle(b,cb,ab,rb)
            overlap=[max(domains[0][0],domains[1][0]),min(domains[0][1],domains[1][1])]
            shift=360*round((overlap[0]-angles[0])/360)
            span=[round(x+shift,9) for x in angles]
            parameter="Z";axis=1;coordinate=c[2]
        else:
            raise NativeEvidenceUnavailable("UNSUPPORTED_SPLIT_BOUNDARY")
        if parameter!="ANGLE":
            _n1c_boundary(*domains,axis,coordinate,span)
    else:
        raise NativeEvidenceUnavailable("UNSUPPORTED_ANALYTIC_SURFACE")
    return {"source":"NATIVE_EDGE_PARAMETERS","parameter":parameter,"coordinate":coordinate,"span":span}


def derive_split_and_seam_evidence(observations):
    """Classify explicitly supplied bounded native relations, not a full body.

    Perimeter loops still prove domains; candidate adjacency rows identify the
    relations to qualify. Complete body accounting and lifecycle belong later.
    """
    _require(isinstance(observations,(list,tuple)) and observations,"INCOMPLETE_NATIVE_OBSERVATION")
    by_face={};facts={};relations={};output={}
    for observation in observations:
        native,raw,rows=_n1c_observation(observation);face=native["reference"]["encoded"]
        _require(face not in by_face,"DUPLICATE_NATIVE_FACE")
        by_face[face]=observation;facts[face]=rows;output[face]=deepcopy(native)
        if observation["normalized_face"] is not None:
            output[face]["trim_domain"]=deepcopy(observation["normalized_face"].get("trim_domain"))
    for face,observation in by_face.items():
        edges=observation["native_face"].get("edges")
        _require(isinstance(edges,list) and edges,"NATIVE_ADJACENCY_UNAVAILABLE")
        seen=set()
        for row in edges:
            _require(isinstance(row,dict),"NATIVE_ADJACENCY_UNAVAILABLE")
            edge=_n1c_reference(row.get("reference"),"EDGE");pair=row.get("adjacent_face_references")
            _require(edge not in seen and edge in facts[face] and isinstance(pair,list) and len(pair)==2
                     and all(isinstance(p,str) and p in by_face for p in pair) and face in pair,
                     "NONRECIPROCAL_EDGE_INCIDENCE")
            seen.add(edge);relations.setdefault(edge,[]).append((face,row))
    for edge,entries in relations.items():
        pair=entries[0][1]["adjacent_face_references"];incident=set(pair)
        _require(all(sorted(row["adjacent_face_references"])==sorted(pair) for _,row in entries)
                 and {f for f,_ in entries}==incident and len(entries)==len(incident)
                 and {f for f,rows in facts.items() if edge in rows}==incident,
                 "NONRECIPROCAL_EDGE_INCIDENCE")
        if len(incident)==1:
            face=pair[0];observation=by_face[face]
            witness=derive_periodic_domain_witness(observation)
            rows=facts[face][edge]
            _require(len(rows)==2,"UNPROVEN_PERIODIC_WITNESS")
            theta=round(_angle(rows[0]["start_mm"],witness["center_mm"]),9)
            proof={"source":"NATIVE_PERIODIC_EDGE_PARAMETERS","angular_pair_deg":[theta,theta+360],
                   "z_span_mm":deepcopy(witness["z_span_mm"])}
            output[face]["periodic_domain_witness"]=witness
            additions={"periodic_seam":True,"seam_parameters":proof}
        else:
            left,right=sorted(incident)
            _require(len(facts[left][edge])==len(facts[right][edge])==1,"AMBIGUOUS_SPLIT_BOUNDARY")
            additions={"split_boundary":_n1c_split(by_face[left],by_face[right],edge,
                                                  facts[left][edge][0],facts[right][edge][0])}
        for face,_ in entries:
            row=next(r for r in output[face]["edges"] if r["reference"]["encoded"]==edge)
            _require(not any(key in row for key in ("split_boundary","periodic_seam","seam_parameters")),
                     "PRECLASSIFIED_NATIVE_EVIDENCE")
            row.update(deepcopy(additions))
    for face in output.values():face["edges"].sort(key=lambda r:r["reference"]["encoded"])
    return [output[key] for key in sorted(output)]


def _n1d_context(context,stage):
    _require(isinstance(context,dict) and context.get("stage")==stage
             and all(context.get(key) is not None for key in
                     ("document_identity","generation","extension"))
             and all(callable(context.get(key)) for key in
                     ("resolver","entity_document","read_relations")),"INCOMPLETE_LIFECYCLE_CONTEXT")


def _n1d_document(context,entity):
    _require(entity is not None and context["entity_document"](entity) is context["document_identity"],
             "WRONG_DOCUMENT_REFERENCE")


def _n1d_resolved(context,reference,objects):
    _require(isinstance(reference,dict),"MISSING_PRE_SAVE_REFERENCE")
    kind=reference.get("reference_type")
    _require(kind in {"FACE","EDGE","FEATURE","PLANE","SKETCH"},"WRONG_TYPED_LIFECYCLE_REFERENCE")
    encoded=_n1c_reference(reference,kind)
    entity,proof=_resolved(context["extension"],reference,context["resolver"])
    _n1d_document(context,entity)
    key=(kind,encoded)
    _require(key not in objects or objects[key] is entity,"AMBIGUOUS_PERSISTENT_REFERENCE")
    objects[key]=entity
    return entity,proof


def _n1d_relations(context,category,entity,reference):
    """Read bounded native relationships; no entity is selected from geometry."""
    facts=context["read_relations"](category,entity)
    _require(isinstance(facts,dict),"NATIVE_LIFECYCLE_RELATION_UNAVAILABLE")
    if category=="FACE":
        owner=facts.get("owner_reference")
        _n1c_reference(owner,"FEATURE")
        return {"owner_reference":deepcopy(owner)}
    if category=="EDGE":
        faces=facts.get("adjacent_faces")
        _require(isinstance(faces,(list,tuple)) and len(faces)==2,"NATIVE_EDGE_INCIDENCE_UNAVAILABLE")
        refs=[]
        for face in faces:
            _n1d_document(context,face)
            refs.append(capture_persistent_reference(context["extension"],face,"FACE"))
        return {"adjacent_face_references":sorted(refs,key=lambda r:r["encoded"])}
    support=facts.get("support_reference");kind=reference["reference_type"]
    _require(_n1c_reference(support,kind)==reference["encoded"]
             and facts.get("support_geometry_readback") is True,"NATIVE_SUPPORT_RELATION_UNAVAILABLE")
    profile=facts.get("profile_reference");_n1c_reference(profile,"SKETCH")
    owner=facts.get("support_owner_reference")
    if kind=="FACE":
        host=_n1c_reference(owner,"FEATURE")
    else:
        _require(owner is None,"WRONG_DATUM_OWNER_RELATION")
        host=reference["encoded"]
    parents=facts.get("native_parent_references")
    _require(isinstance(parents,list) and parents,"NATIVE_SUPPORT_PARENT_UNAVAILABLE")
    for parent in parents:
        _n1c_reference(parent,"FEATURE")
    _require(sum(p["encoded"]==host for p in parents)==1,"NATIVE_SUPPORT_PARENT_MISMATCH")
    return {"support_reference":deepcopy(support),"support_owner_reference":deepcopy(owner),
            "profile_reference":deepcopy(profile),
            "native_parent_references":sorted(deepcopy(parents),key=lambda r:r["encoded"]),
            "support_geometry_readback":True}


def _n1d_relation_references(row):
    facts=row["relations"]
    if row["category"]=="FACE":
        return [facts["owner_reference"]]
    if row["category"]=="EDGE":
        return facts["adjacent_face_references"]
    refs=[facts["support_reference"],facts["profile_reference"],*facts["native_parent_references"]]
    if facts["support_owner_reference"] is not None:refs.append(facts["support_owner_reference"])
    return refs


def _n1d_row_key(row):
    _require(isinstance(row,dict) and row.get("category") in {"FACE","EDGE","SUPPORT"},
             "INCOMPLETE_PRE_SAVE_SNAPSHOT")
    ref=row.get("reference")
    _require(isinstance(ref,dict),"MISSING_PRE_SAVE_REFERENCE")
    kind=ref.get("reference_type")
    _require(kind in ({"FACE"} if row["category"]=="FACE" else
                      {"EDGE"} if row["category"]=="EDGE" else {"FACE","PLANE"}),
             "WRONG_TYPED_LIFECYCLE_REFERENCE")
    return row["category"],kind,_n1c_reference(ref,kind)


def capture_native_lifecycle_snapshot(context,entries):
    """Retain old refs at FINAL_PRE_SAVE through the approved capture abstraction.

    Retained Python instances prove only the offline acquisition/boundary. Native
    identity remains the typed persistent reference, never those instances.
    """
    _n1d_context(context,"FINAL_PRE_SAVE")
    _require(isinstance(entries,(list,tuple)) and entries,"INCOMPLETE_PRE_SAVE_SNAPSHOT")
    records=[];objects={};seen=set()
    for entry in entries:
        _require(isinstance(entry,dict),"INCOMPLETE_PRE_SAVE_SNAPSHOT")
        category=entry.get("category");kind=entry.get("reference_type")
        _require(category in {"FACE","EDGE","SUPPORT"} and kind in
                 ({"FACE"} if category=="FACE" else {"EDGE"} if category=="EDGE" else {"FACE","PLANE"}),
                 "WRONG_TYPED_LIFECYCLE_REFERENCE")
        entity=entry.get("native_entity");_n1d_document(context,entity)
        ref=capture_persistent_reference(context["extension"],entity,kind)
        row={"category":category,"reference":ref}
        key=_n1d_row_key(row)
        _require(key not in seen,"DUPLICATE_PERSISTENT_REFERENCE")
        seen.add(key)
        resolved,proof=_n1d_resolved(context,ref,objects)
        _require(resolved is entity,"AMBIGUOUS_PRE_SAVE_NATIVE_OBJECT")
        row.update(persistent_reference_bytes=base64.b64decode(ref["encoded"],validate=True),
                   native_type=proof.get("native_type") or kind,resolution=proof,
                   persistent_reference_resolved=True,
                   relations=_n1d_relations(context,category,entity,ref))
        for relation in _n1d_relation_references(row):_n1d_resolved(context,relation,objects)
        records.append(row)
    _require({r["category"] for r in records}=={"FACE","EDGE","SUPPORT"},"INCOMPLETE_PRE_SAVE_CATEGORIES")
    records.sort(key=_n1d_row_key)
    receipt={"source":"CONTROLLED_PRE_SAVE_LIFECYCLE_CAPTURE","records":records,
             "values":deepcopy(records),"objects":objects,
             "document_identity":context["document_identity"],"generation":context["generation"],
             "extension":context["extension"]}
    return {"stage":"FINAL_PRE_SAVE","status":"PASS_OFFLINE",
            "source":"OFFLINE_NATIVE_SHAPED_LIFECYCLE_CONTRACT",
            "document_identity":context["document_identity"],"generation":context["generation"],
            "records":records,"capture_record":receipt}


def resolve_native_lifecycle_snapshot(snapshot,context,boundary):
    """Resolve retained PRE-SAVE references; never execute save/close/reopen.

    This opt-in contract requires an explicit resolver and bounded document
    identity reader. There is no default real transport or geometric rescue.
    """
    _n1d_context(context,"POST_REOPEN")
    _require(isinstance(snapshot,dict) and snapshot.get("stage")=="FINAL_PRE_SAVE",
             "INCOMPLETE_PRE_SAVE_SNAPSHOT")
    receipt=snapshot.get("capture_record")
    _require(isinstance(receipt,dict) and receipt.get("source")=="CONTROLLED_PRE_SAVE_LIFECYCLE_CAPTURE"
             and all(receipt.get(key) is not None for key in
                     ("document_identity","generation","extension")),
             "PRE_SAVE_CAPTURE_UNAVAILABLE")
    records=snapshot.get("records")
    _require(isinstance(records,list) and records and records is receipt.get("records")
             and sorted(records,key=_n1d_row_key)==receipt.get("values")
             and snapshot.get("document_identity") is receipt.get("document_identity")
             and snapshot.get("generation") is receipt.get("generation"),"TAMPERED_PRE_SAVE_SNAPSHOT")
    _require(isinstance(boundary,dict) and boundary.get("source")=="OFFLINE_NATIVE_SHAPED_LIFECYCLE_BOUNDARY"
             and boundary.get("document_identity") is receipt["document_identity"]
             and context["document_identity"] is receipt["document_identity"],"WRONG_DOCUMENT_REFERENCE")
    _require(boundary.get("events")==["SAVE","CLOSE","REOPEN"]
             and boundary.get("pre_generation") is receipt["generation"]
             and boundary.get("post_generation") is context["generation"]
             and context["generation"] is not receipt["generation"]
             and boundary.get("pre_extension") is receipt["extension"]
             and boundary.get("post_extension") is context["extension"]
             and context["extension"] is not receipt["extension"],"MISSING_LIFECYCLE_BOUNDARY")
    prior_objects=receipt.get("objects")
    _require(isinstance(prior_objects,dict) and prior_objects,"PRE_SAVE_CAPTURE_UNAVAILABLE")
    output=[];objects={}
    resolved_rows={}
    # Resolve all required old primary tokens before reading current relations.
    # A changed incident/support token cannot preempt the old-token proof.
    for row in sorted(records,key=_n1d_row_key):
        ref=row["reference"]
        _require(row["persistent_reference_bytes"]==base64.b64decode(ref["encoded"],validate=True),
                 "TAMPERED_PRE_SAVE_REFERENCE")
        resolved_rows[_n1d_row_key(row)]=_n1d_resolved(context,ref,objects)
    for row in sorted(records,key=_n1d_row_key):
        ref=row["reference"]
        entity,proof=resolved_rows[_n1d_row_key(row)]
        facts=_n1d_relations(context,row["category"],entity,ref)
        _require(facts==row["relations"],"POST_REOPEN_NATIVE_RELATION_CHANGED")
        # Every resolution input, including related objects, comes from PRE_SAVE.
        # Newly measured relation refs are compared, never substituted as inputs.
        for relation in _n1d_relation_references(row):_n1d_resolved(context,relation,objects)
        output.append({**deepcopy(row),"relations":facts,"resolution":proof})
    _require(set(objects)==set(prior_objects),"INCOMPLETE_LIFECYCLE_OBJECT_RESOLUTION")
    _require(all(entity is not prior_objects[key] for key,entity in objects.items()),
             "PRE_SAVE_OBJECT_REUSED_ACROSS_REOPEN")
    return {"stage":"POST_REOPEN","status":"PASS_OFFLINE",
            "source":"OFFLINE_NATIVE_SHAPED_LIFECYCLE_CONTRACT",
            "document_identity":context["document_identity"],"generation":context["generation"],
            "records":output,"pre_save_reference_reused":True,
            "lifecycle_boundary_proof":list(boundary["events"]),
            "real_persistent_reference_lifecycle":"NOT_VERIFIED"}


def _n1e_begin_transport(adapter,context):
    """Enter an explicit offline observation before any native acquisition."""
    from .r003_native_collectors import _body_inventory_context
    _require(adapter.simulated is True and getattr(adapter,"_identity",None) is not None,
             "OFFLINE_CONTROLLED_SESSION_REQUIRED")
    _require(isinstance(context,dict) and set(context)=={"source","model","extension","document_identity",
             "observation_scope","resolver","entity_document","body_kind"},"INVALID_CONTROLLED_CONTEXT")
    context=_body_inventory_context(adapter._part,context)
    return {"source":"CONTROLLED_R004_NATIVE_ACQUISITION","model":adapter._part,
            "extension":context["extension"],"document_identity":context["document_identity"],
            "scope":context["observation_scope"],"session_identity":adapter._identity,"members":{},"context":context}


def _n1e_retain_incidence(transport,face,record,edge,adjacent,row):
    """Retain objects at existing adapter sites; never create a second scanner."""
    inventory=transport["inventory"];receipt=inventory["capture_record"]
    faces=dict(receipt["faces"]);edges=dict(receipt["edges"])
    token=_n1c_reference(record["reference"],"FACE");e=_n1c_reference(row["reference"],"EDGE")
    _require(faces.get(token) is face and edges.get(e) is edge,"ACQUIRED_NATIVE_IDENTITY_SUBSTITUTION")
    _require(len(adjacent)==2 and len(row["adjacent_face_references"])==2
             and all(faces.get(t) is entity for t,entity in zip(row["adjacent_face_references"],adjacent)),
             "FOREIGN_NATIVE_EDGE_ADJACENCY")
    binding=transport["members"].setdefault(token,{"face":face,"record":record,"edges":{}})
    _require(binding["face"] is face and binding["record"] is record and e not in binding["edges"],
             "DUPLICATE_CONTRADICTORY_NATIVE_INCIDENCE")
    binding["edges"][e]=(edge,tuple(adjacent),row)
    proof=next(r["resolution"] for r in inventory["edges"] if r["reference"]["encoded"]==e)
    row.update(edge_resolution=deepcopy(proof),edge_reference_resolved=True,source_label="NATIVE_OBSERVED")


def _n1e_finish_transport(transport,native):
    from .r003_native_collectors import validate_typed_body_inventory
    validate_typed_body_inventory(transport["inventory"])
    origin=transport["inventory"]["capture_record"]
    _require(transport["model"] is origin["model"] and transport["extension"] is origin["extension"]
             and transport["document_identity"] is origin["document_identity"]
             and transport["scope"] is origin["observation_scope"],"INVENTORY_TRANSPORT_CONTEXT_CHANGED")
    _require(set(transport["members"])=={t for t,_ in transport["inventory"]["capture_record"]["faces"]},
             "INCOMPLETE_ACQUIRED_FACE_TRANSPORT")
    _n1e_accounting(native,transport["inventory"],classified=False)
    bindings=tuple((token,b["face"],b["record"],tuple((e,*objects) for e,objects in sorted(b["edges"].items())))
                   for token,b in sorted(transport["members"].items()))
    transport.update(native=native,values=deepcopy(native),object_bindings=bindings)
    return transport


def _n1e_accounting(native,inventory,*,classified=True):
    """Independent inventory is an input; incidence is only a comparison side."""
    from .r003_native_collectors import validate_typed_body_inventory
    validate_typed_body_inventory(inventory)
    face_set={_n1c_reference(r["reference"],"FACE") for r in inventory["faces"]}
    edge_set={_n1c_reference(r["reference"],"EDGE") for r in inventory["edges"]}
    faces=native.get("faces");body=native.get("body",{})
    _require(isinstance(faces,list) and len(faces)==len(face_set)
             and {_n1c_reference(f["reference"],"FACE") for f in faces}==face_set,"INCOMPLETE_BODY_FACE_ACCOUNTING")
    _require(type(body.get("solid_body_count")) is int and body["solid_body_count"]==1
             and type(body.get("face_count")) is int and body["face_count"]==len(face_set)
             and type(body.get("edge_count")) is int and body["edge_count"]==len(edge_set),"BODY_COUNT_IDENTITY_MISMATCH")
    incidence={}
    for face in faces:
        f=_n1c_reference(face["reference"],"FACE");seen=set()
        _require(face.get("persistent_reference_resolved") is True and face.get("edges"),"INCOMPLETE_FACE_INCIDENCE")
        for row in face["edges"]:
            e=_n1c_reference(row.get("reference"),"EDGE");pair=row.get("adjacent_face_references")
            _require(isinstance(pair,list) and len(pair)==2 and set(pair)<=face_set and f in pair
                     and e in edge_set and e not in seen,"NONRECIPROCAL_COMPLETE_INCIDENCE")
            _n1c_resolution(row.get("edge_resolution"),row["reference"],"EDGE")
            _require(row.get("edge_reference_resolved") is True,"UNRESOLVED_COMPLETE_EDGE")
            seen.add(e);previous,used=incidence.setdefault(e,(sorted(pair),[]))
            _require(previous==sorted(pair),"CONTRADICTORY_COMPLETE_EDGE")
            used.append(f)
            if classified and len(set(pair))==1:
                _require(face.get("surface_type")=="CYLINDER" and row.get("periodic_seam") is True
                         and row.get("seam_parameters",{}).get("source")=="NATIVE_PERIODIC_EDGE_PARAMETERS",
                         "UNPROVEN_COMPLETE_SEAM")
    _require(set(incidence)==edge_set and all(len(used)==len(set(pair)) and set(used)==set(pair)
             for pair,used in incidence.values()),"INCOMPLETE_OR_NONRECIPROCAL_BODY_EDGE_ACCOUNTING")


def _n1e_canonical_ledger(extension,ledger,resolver):
    output=deepcopy(ledger)
    for row in output:
        for key,kind in (("native_feature_reference","FEATURE"),("native_sketch_reference","SKETCH")):
            _require(row.get(key,{}).get("reference_type")==kind,"WRONG_LEDGER_REFERENCE_KIND")
        for key in ("native_feature_reference","native_sketch_reference","native_support_reference"):
            reference=row[key]
            entity,_=_resolved(extension,reference,resolver)
            row[key]=capture_persistent_reference(extension,entity,reference["reference_type"])
            _n1c_reference(row[key],reference["reference_type"])
    return output


def _n1e_candidate_relation(a,b):
    # Geometry tests choose bounded classification inputs, never native identity.
    if a["surface_type"]!=b["surface_type"] or a["owner_reference"]!=b["owner_reference"]:
        return False
    if a["surface_type"]=="PLANE":
        axis=next((i for i,x in enumerate(a["normal"]) if abs(abs(x)-1)<=AXIS_COMPONENT_TOLERANCE),None)
        return (axis is not None and _n1c_equal(a["normal"],b["normal"])
                and _n1c_equal(a["bounds_mm"][2*axis],b["bounds_mm"][2*axis]))
    return (a["surface_type"]=="CYLINDER" and _n1c_equal(a["center_mm"],b["center_mm"])
            and _n1c_equal(a["axis"],b["axis"]) and _n1c_equal(a["radius_mm"],b["radius_mm"])
            and a["interior"]==b["interior"])


def _n1e_observation(scope,native,raw,normalized):
    members={"native_face":native,"raw_usage":raw,"normalized_face":normalized}
    receipt={"source":"CONTROLLED_FACE_OBSERVATION","scope":scope,
             "observation_token":str(id(scope)),"face_reference":deepcopy(native["reference"]),
             "members":members,"values":deepcopy(members),
             "usage_rows":[row for loop in raw["loops"] for row in loop["coedge_usages"]]}
    return {"scope":scope,**members,"capture_record":receipt}


def _n1e_supported_domains(faces):
    # Complete integration qualifies ANGLE and Z separately. A real kernel may
    # emit mixed partitions; retain a failure until that representation is qualified.
    cylinders=[f for f in faces if f["surface_type"]=="CYLINDER"]
    for i,left in enumerate(cylinders):
        for right in cylinders[i+1:]:
            if _n1e_candidate_relation(left,right):
                a,b=left["trim_domain"],right["trim_domain"]
                _require(_n1c_equal(a["angular_span_deg"],b["angular_span_deg"])
                         or _n1c_equal(a["z_span_mm"],b["z_span_mm"]),
                         "UNSUPPORTED_MIXED_ANGLE_Z")


def collect_complete_native_observation(adapter,*,inventory_context,document,generation,read_relations):
    """Capture complete offline evidence; no oracle or validator enters here."""
    _require(generation is not None and callable(read_relations),"INCOMPLETE_CONTROLLED_LIFECYCLE_CONTEXT")
    _require(isinstance(document,dict) and set(document)=={"document_id","path"},
             "INVALID_CONTROLLED_DOCUMENT")
    acquired=adapter.collect_native(observation_context=inventory_context)
    transport=acquired.pop("_observation")
    inventory_context=transport["context"]
    extension=transport["extension"];resolver=inventory_context["resolver"]
    ledger=_n1e_canonical_ledger(extension,adapter.ledger,resolver)
    registry=collect_feature_registry(adapter._part,ledger,document=document,resolver=resolver)
    native=deepcopy(acquired);native["native_features"]=registry
    inventory=transport["inventory"];scope=transport["scope"]
    by_face={f["reference"]["encoded"]:f for f in native["faces"]}
    retained={};normalized={};raws={}
    for token in sorted(by_face):
        face=transport["members"][token]["face"];record=by_face[token]
        _require(record["surface_type"] in {"PLANE","CYLINDER"},"UNSUPPORTED_COMPLETE_NATIVE_SURFACE")
        owner=collect_face_owner(extension,face,registry,resolver=resolver)
        _require(owner["reference"]==record["reference"] and owner["owner_reference"]==record["owner_reference"]
                 and owner["native_owner_feature_id"]==record["native_owner_feature_id"],"NATIVE_OWNER_TRANSPORT_CHANGED")
        record.update(owner)
        raw=collect_raw_coedge_usages(extension,face,registry,native_face_record=record,resolver=resolver)
        raw_tokens=[r["edge_reference"]["encoded"] for loop in raw["loops"] for r in loop["coedge_usages"]]
        _require(set(raw_tokens)=={r["reference"]["encoded"] for r in record["edges"]},"RAW_COMPLETE_INCIDENCE_MISMATCH")
        seam=any(len(set(r["adjacent_face_references"]))==1 for r in record["edges"])
        trim=None if seam else collect_face_trim(extension,face,registry,resolver=resolver)
        if trim is not None:
            record.update(trim_domain=deepcopy(trim["trim_domain"]),face_trim_raw=deepcopy(trim["face_trim_raw"]),
                          surface_geometry=deepcopy(trim["surface_geometry"]))
        record["raw_usage"]=raw;raws[token]=raw;normalized[token]=trim
        retained[token]={"native_face":record,"raw_usage":raw,"normalized_face":trim,"owner":owner}
    _n1e_supported_domains([r for r in by_face.values() if r.get("trim_domain")])
    candidates=[]
    for token,record in sorted(by_face.items()):
        edges=[]
        for row in record["edges"]:
            pair=row["adjacent_face_references"]
            if len(set(pair))==1 or _n1e_candidate_relation(by_face[pair[0]],by_face[pair[1]]):
                edges.append(deepcopy(row))
        if edges:
            projection=deepcopy(record);projection["edges"]=edges
            candidates.append(_n1e_observation(scope,projection,raws[token],normalized[token]))
    classified=derive_split_and_seam_evidence(candidates) if candidates else []
    for projection in classified:
        token=projection["reference"]["encoded"];record=by_face[token]
        authoritative={r["reference"]["encoded"]:r for r in record["edges"]}
        for edge in projection["edges"]:
            target=authoritative[edge["reference"]["encoded"]]
            for key in ("split_boundary","periodic_seam","seam_parameters"):
                if key in edge:target[key]=deepcopy(edge[key])
        if "periodic_domain_witness" in projection:
            witness=projection["periodic_domain_witness"]
            record.update(periodic_domain_witness=deepcopy(witness),
                          same_observation_provenance=deepcopy(witness["same_observation_provenance"]),
                          trim_domain={"source":witness["source"],"kind":"CYLINDER_RECTANGLE",
                            "coordinate_convention":"MODEL_XY_CCW_DEGREES","angular_span_deg":[0.0,witness["period_deg"]],
                            "z_span_mm":deepcopy(witness["z_span_mm"]),"face_reference":token,
                            "persistent_reference_resolved":record["persistent_reference_resolved"]})
    native["faces"].sort(key=lambda f:f["reference"]["encoded"])
    for face in native["faces"]:
        face["edges"].sort(key=lambda r:r["reference"]["encoded"])
        _require(face.get("trim_domain"),"INCOMPLETE_COMPLETE_FACE_DOMAIN")
    _n1e_accounting(native,inventory)
    entries=[{"category":kind,"reference_type":kind,"native_entity":entity}
             for kind,field in (("FACE","faces"),("EDGE","edges"))
             for _,entity in inventory["capture_record"][field]]
    supports={}
    for row in registry.values():
        ref=row["support_reference"];entity,_=_resolved(extension,ref,resolver)
        supports[(ref["reference_type"],ref["encoded"])]=entity
    entries.extend({"category":"SUPPORT","reference_type":kind,"native_entity":entity}
                   for (kind,_),entity in sorted(supports.items()))
    context={"stage":"FINAL_PRE_SAVE","document_identity":transport["document_identity"],"generation":generation,
             "extension":extension,"resolver":resolver,"entity_document":inventory_context["entity_document"],
             "read_relations":read_relations}
    snapshot=capture_native_lifecycle_snapshot(context,entries)
    for row in snapshot["records"]:
        if row["category"]=="FACE":
            _require(row["relations"]["owner_reference"]==by_face[row["reference"]["encoded"]]["owner_reference"],
                     "LIFECYCLE_OWNER_CAPTURE_CHANGED")
        elif row["category"]=="EDGE":
            e=row["reference"]["encoded"]
            face=next(f for f in native["faces"] if any(r["reference"]["encoded"]==e for r in f["edges"]))
            edge=next(r for r in face["edges"] if r["reference"]["encoded"]==e)
            _require(sorted(r["encoded"] for r in row["relations"]["adjacent_face_references"])
                     ==sorted(edge["adjacent_face_references"]),"LIFECYCLE_EDGE_CAPTURE_CHANGED")
        else:
            matching=[r for r in registry.values() if r["support_reference"]==row["reference"]]
            keys=("support_reference","support_owner_reference","profile_reference",
                  "native_parent_references","support_geometry_readback")
            expected=[{key:r[key] for key in keys} for r in matching]
            _require(expected and all(item==expected[0] for item in expected)
                     and row["relations"]==expected[0],"AMBIGUOUS_OR_CHANGED_LIFECYCLE_SUPPORT")
    observation={"native":native,"body_inventory":inventory,"transport":transport,
                 "face_observations":candidates,"lifecycle_snapshot":snapshot}
    observation["capture_record"]={"source":"CONTROLLED_COMPLETE_NATIVE_OBSERVATION","native":native,
        "values":deepcopy(native),"inventory":inventory,"transport":transport,"scope":scope,
        "session_identity":adapter._identity,"members":retained,
        "member_values":deepcopy(retained),"candidates":candidates,"candidate_values":deepcopy(classified),
        "lifecycle_snapshot":snapshot,"registry":registry,
        "transport_anchors":tuple(transport[key] for key in
              ("model","extension","document_identity","scope","session_identity","inventory","members","object_bindings")),
        "member_objects":tuple((token,tuple(members.items()),tuple(row for loop in raws[token]["loops"]
                                for row in loop["coedge_usages"])) for token,members in sorted(retained.items()))}
    return observation


def resolve_complete_native_observation(observation,post_context,boundary):
    """Validate retained pre-save evidence and resolve old refs in a new graph."""
    _require(isinstance(observation,dict),"COMPLETE_OBSERVATION_MISSING")
    receipt=observation.get("capture_record",{})
    native=observation.get("native");transport=observation.get("transport")
    _require(receipt.get("source")=="CONTROLLED_COMPLETE_NATIVE_OBSERVATION" and native is receipt.get("native")
             and native==receipt.get("values") and transport is receipt.get("transport")
             and observation.get("body_inventory") is receipt.get("inventory")
             and observation.get("face_observations") is receipt.get("candidates")
             and observation.get("lifecycle_snapshot") is receipt.get("lifecycle_snapshot"),
             "CROSS_OBSERVATION_OR_MUTATED_COMPLETE_EVIDENCE")
    _require(transport["scope"] is receipt["scope"] and transport["session_identity"] is receipt["session_identity"]
             and {k:v for k,v in transport["native"].items() if k!="_observation"}==transport["values"],
             "CROSS_OBSERVATION_TRANSPORT")
    keys=("model","extension","document_identity","scope","session_identity","inventory","members","object_bindings")
    _require(all(transport.get(key) is original for key,original in zip(keys,receipt["transport_anchors"])),
             "ACQUISITION_TRANSPORT_OBJECT_SUBSTITUTION")
    inventory=observation["body_inventory"];origin=inventory["capture_record"]
    _require(transport["model"] is origin["model"] and transport["extension"] is origin["extension"]
             and transport["document_identity"] is origin["document_identity"]
             and transport["scope"] is origin["observation_scope"],"INVENTORY_TRANSPORT_CONTEXT_CHANGED")
    for token,face,record,edges in transport["object_bindings"]:
        member=transport["members"].get(token,{})
        _require(member.get("face") is face and member.get("record") is record
                 and len(member.get("edges",{}))==len(edges),"NATIVE_TRANSPORT_MEMBER_SUBSTITUTION")
        for e,entity,pair,row in edges:
            actual=member["edges"].get(e,())
            _require(len(actual)==3 and actual[0] is entity and actual[2] is row
                     and len(actual[1])==len(pair) and all(a is b for a,b in zip(actual[1],pair)),
                     "NATIVE_TRANSPORT_EDGE_SUBSTITUTION")
    _require(receipt["members"]==receipt["member_values"],"MUTATED_NATIVE_PRODUCER_OUTPUT")
    by_face={f["reference"]["encoded"]:f for f in native["faces"]}
    _require(native["native_features"] is receipt["registry"] and set(by_face)==set(receipt["members"]),
             "NATIVE_REGISTRY_OR_FACE_SUBSTITUTION")
    for token,objects,usage_rows in receipt["member_objects"]:
        members=receipt["members"][token]
        _require(all(members.get(role) is original for role,original in objects)
                 and by_face[token] is members["native_face"]
                 and by_face[token]["raw_usage"] is members["raw_usage"],"CROSS_OBSERVATION_MEMBER_SUBSTITUTION")
        current=[row for loop in members["raw_usage"]["loops"] for row in loop["coedge_usages"]]
        _require(len(current)==len(usage_rows) and len({id(row) for row in current})==len(current)
                 and all(sum(row is original for original in usage_rows)==1 for row in current),
                 "FABRICATED_COMPLETE_COEDGE_USAGE")
    candidates=observation["face_observations"]
    for candidate in candidates:
        _require(candidate["scope"] is receipt["scope"],"CROSS_OBSERVATION_CANDIDATE")
        _n1c_observation(candidate)
    _require((derive_split_and_seam_evidence(candidates) if candidates else [])==receipt["candidate_values"],
             "MUTATED_CLASSIFICATION_OUTPUT")
    _n1e_accounting(native,observation["body_inventory"])
    lifecycle=resolve_native_lifecycle_snapshot(observation["lifecycle_snapshot"],post_context,boundary)
    return {"native":native,"lifecycle":lifecycle,"source_regions":{fid:fid for fid in native["native_features"]}}
