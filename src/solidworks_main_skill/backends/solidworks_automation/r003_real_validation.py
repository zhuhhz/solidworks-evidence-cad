"""R003-only, source-backed six-gate validation over sealed native evidence.

All functions are pure. Evidence collectors and CAD sessions belong to a later
reviewed native adapter, not to this module or its unit tests.
"""

from __future__ import annotations

import math
from typing import Any, Mapping

from experiments.three_view_reconstruction.drawing.view_orientation import canonicalize
from experiments.three_view_reconstruction.validation.primitive_matcher import match

from .r003_execution_bridge import AXIS_COMPONENT_TOLERANCE, LINEAR_TOLERANCE_MM


GATES = ("REBUILD", "BODY_COUNT", "BREP_GEOMETRY", "FEATURE_OWNERSHIP",
         "SAVE_REOPEN", "DRAWING_ROUNDTRIP")
STATUSES = frozenset({"PASS", "FAIL", "INCOMPLETE", "ERROR", "NOT_RUN"})
_NORMALS = {"FRONT": (0.0, 0.0, 1.0), "TOP": (0.0, 1.0, 0.0),
            "LEFT": (-1.0, 0.0, 0.0)}


def _result(status: str, reason: str, **evidence: Any) -> dict:
    return {"status": status, "reason": reason, **evidence}


def _number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _close(a: Any, b: Any, tolerance: float = LINEAR_TOLERANCE_MM) -> bool:
    return _number(a) and _number(b) and abs(a - b) <= tolerance


def _vector(actual: Any, expected: tuple[float, ...], tolerance: float) -> bool:
    return (isinstance(actual, (list, tuple)) and len(actual) == len(expected)
            and all(_close(a, b, tolerance) for a, b in zip(actual, expected)))


def validate_strict_rebuild(initial: Mapping[str, Any] | None,
                            reopened: Mapping[str, Any] | None,
                            required_feature_ids: list[str],
                            expected_native_refs: Mapping[str, str] | None = None) -> dict:
    """Feature identity is source ID plus persistent ref, never display name."""
    if initial is None or reopened is None:
        return _result("NOT_RUN", "INITIAL_OR_REOPENED_REBUILD_NOT_RUN")
    if len(required_feature_ids) != 3 or len(set(required_feature_ids)) != 3:
        return _result("ERROR", "INVALID_REQUIRED_FEATURE_SET")
    if (not isinstance(expected_native_refs, Mapping)
            or set(expected_native_refs) != set(required_feature_ids)
            or any(not isinstance(value, str) or not value for value in expected_native_refs.values())):
        return _result("INCOMPLETE", "CREATION_PERSISTENT_MAPPING_MISSING")
    phase_rows = {}
    for phase, evidence in (("initial", initial), ("reopened", reopened)):
        if not isinstance(evidence, Mapping):
            return _result("ERROR", "MALFORMED_REBUILD_EVIDENCE")
        if evidence.get("exception"):
            return _result("ERROR", phase.upper() + "_REBUILD_EXCEPTION")
        rebuild = evidence.get("rebuild_result")
        if rebuild is False:
            return _result("FAIL", phase.upper() + "_REBUILD_FALSE")
        if rebuild is not True:
            return _result("INCOMPLETE", phase.upper() + "_REBUILD_RESULT_MISSING")
        rows = evidence.get("features")
        if not isinstance(rows, list):
            return _result("INCOMPLETE", phase.upper() + "_FEATURE_ROWS_MISSING")
        indexed = {}
        for row in rows:
            if not isinstance(row, Mapping):
                return _result("ERROR", phase.upper() + "_MALFORMED_FEATURE_ROW")
            feature_id = row.get("source_feature_id")
            if feature_id in indexed:
                return _result("FAIL", phase.upper() + "_DUPLICATE_FEATURE_ID")
            indexed[feature_id] = row
            if row.get("error_code") not in (0, None):
                return _result("FAIL", phase.upper() + "_NATIVE_FEATURE_ERROR", feature_id=feature_id)
            if row.get("error_code") is None or row.get("suppressed") is None:
                return _result("INCOMPLETE", phase.upper() + "_TREE_FEATURE_STATE_MISSING",
                               feature_id=feature_id)
            if row.get("failed") is True:
                return _result("FAIL", phase.upper() + "_UNEXPECTED_FAILED_FEATURE", feature_id=feature_id)
        if any(feature_id not in indexed for feature_id in required_feature_ids):
            return _result("FAIL", phase.upper() + "_REQUIRED_FEATURE_MISSING")
        for feature_id in required_feature_ids:
            row = indexed[feature_id]
            if row.get("error_code") is None or row.get("suppressed") is None:
                return _result("INCOMPLETE", phase.upper() + "_FEATURE_STATE_MISSING", feature_id=feature_id)
            if row["error_code"] != 0 or row["suppressed"] is True:
                return _result("FAIL", phase.upper() + "_FEATURE_FAILED", feature_id=feature_id)
            if row["suppressed"] is not False:
                return _result("INCOMPLETE", phase.upper() + "_SUPPRESSION_UNKNOWN")
            if not row.get("persistent_reference") or not row.get("native_type"):
                return _result("INCOMPLETE", phase.upper() + "_NATIVE_IDENTITY_MISSING")
            if row["persistent_reference"] != expected_native_refs[feature_id]:
                return _result("FAIL", phase.upper() + "_WRONG_PERSISTENT_OWNER",
                               feature_id=feature_id)
            for key in ("feature_resolved", "sketch_resolved", "plane_resolved",
                        "dependencies_resolved"):
                if row.get(key) is False:
                    return _result("FAIL", phase.upper() + "_" + key.upper() + "_FALSE")
                if row.get(key) is not True:
                    return _result("INCOMPLETE", phase.upper() + "_" + key.upper() + "_UNKNOWN")
        phase_rows[phase] = indexed
    for feature_id in required_feature_ids:
        first, second = phase_rows["initial"][feature_id], phase_rows["reopened"][feature_id]
        if first["persistent_reference"] != second["persistent_reference"] \
                or first["native_type"] != second["native_type"]:
            return _result("FAIL", "PERSISTENT_FEATURE_IDENTITY_CHANGED", feature_id=feature_id)
    return _result("PASS", "STRICT_INITIAL_AND_REOPENED_REBUILD", feature_ids=required_feature_ids)


def validate_body_count(reopened: Mapping[str, Any] | None) -> dict:
    if reopened is None:
        return _result("NOT_RUN", "REOPENED_BODY_EVIDENCE_NOT_RUN")
    fields = (reopened.get("solid_body_count"), reopened.get("face_count"),
              reopened.get("edge_count"))
    if any(type(value) is not int or value < 0 for value in fields):
        return _result("INCOMPLETE", "BODY_TOPOLOGY_COUNTS_MISSING")
    return _result("PASS" if fields[0] == 1 and fields[1] > 0 and fields[2] > 0 else "FAIL",
                   "REOPENED_SINGLE_SOLID_TOPOLOGY", counts=list(fields))


def validate_hole_full_span(hole: Mapping[str, Any] | None,
                            plan_geometry: Mapping[str, Any],
                            body: Mapping[str, Any], ownership: Mapping[str, Any]) -> dict:
    if not isinstance(hole, Mapping):
        return _result("INCOMPLETE", "REOPENED_HOLE_EVIDENCE_MISSING")
    required = ("through_all_created", "cylinder_present", "center_mm", "diameter_mm",
                "axis", "axial_min_z_mm", "axial_max_z_mm", "bottom_opening",
                "top_opening", "blind_cap_absent", "through_state", "native_owner_feature_id")
    if any(key not in hole for key in required):
        return _result("INCOMPLETE", "HOLE_TOPOLOGY_EVIDENCE_MISSING")
    if hole["through_state"] == "unknown":
        return _result("INCOMPLETE", "THROUGH_STATE_UNKNOWN")
    if body.get("solid_body_count") != 1:
        return _result("FAIL", "HOLE_NOT_IN_SINGLE_SOLID")
    expected_id = plan_geometry.get("hole_source_feature_id")
    if (not expected_id or hole["native_owner_feature_id"] != expected_id
            or ownership.get("persistent_mapping_resolved") is not True):
        return _result("FAIL", "HOLE_NATIVE_OWNER_UNPROVEN")
    lower = -plan_geometry["base_depth_mm"]
    upper = plan_geometry["boss_depth_mm"]
    center = plan_geometry["hole_center_mm"]
    checks = {
        "creation_through_all": hole["through_all_created"] is True,
        "cylinder": hole["cylinder_present"] is True,
        "center": isinstance(hole["center_mm"], (list, tuple)) and len(hole["center_mm"]) == 2
                  and all(_close(a, b) for a, b in zip(hole["center_mm"], center)),
        "diameter": _close(hole["diameter_mm"], plan_geometry["hole_diameter_mm"]),
        "axis": (_vector(hole["axis"], (0.0, 0.0, 1.0), AXIS_COMPONENT_TOLERANCE)
                 or _vector(hole["axis"], (0.0, 0.0, -1.0), AXIS_COMPONENT_TOLERANCE)),
        "full_min": _close(hole["axial_min_z_mm"], lower),
        "full_max": _close(hole["axial_max_z_mm"], upper),
        "bottom_open": hole["bottom_opening"] is True,
        "top_open": hole["top_opening"] is True,
        "no_blind_cap": hole["blind_cap_absent"] is True,
        "through_confirmed": hole["through_state"] == "confirmed",
    }
    return _result("PASS" if all(checks.values()) else "FAIL",
                   "FULL_COMPOSITE_NATIVE_HOLE", checks=checks)


def validate_brep_geometry(reopened: Mapping[str, Any] | None,
                           plan_geometry: Mapping[str, Any],
                           body: Mapping[str, Any], ownership: Mapping[str, Any]) -> dict:
    if not isinstance(reopened, Mapping):
        return _result("NOT_RUN", "REOPENED_BREP_NOT_RUN")
    required = ("overall_envelope_mm", "lower_envelope_mm", "upper_envelope_mm",
                "fused_interface", "cylindrical_wall_count", "extra_geometry_count", "hole")
    if any(key not in reopened for key in required):
        return _result("INCOMPLETE", "BREP_MEASUREMENTS_MISSING")
    lower = plan_geometry.get("lower_envelope_mm")
    upper = plan_geometry.get("upper_envelope_mm")
    overall = plan_geometry.get("overall_envelope_mm")
    if any(not isinstance(value, (list, tuple)) or len(value) != 6
           for value in (lower, upper, overall)):
        return _result("ERROR", "PLAN_GEOMETRY_UNAVAILABLE")
    checks = {
        "overall": _vector(reopened["overall_envelope_mm"], tuple(overall), LINEAR_TOLERANCE_MM),
        "lower": _vector(reopened["lower_envelope_mm"], tuple(lower), LINEAR_TOLERANCE_MM),
        "upper": _vector(reopened["upper_envelope_mm"], tuple(upper), LINEAR_TOLERANCE_MM),
        "fused": reopened["fused_interface"] is True,
        "one_cylinder": reopened["cylindrical_wall_count"] == 1,
        "no_extra": reopened["extra_geometry_count"] == 0,
        "centered": _close(upper[0] + upper[3], 0) and _close(upper[1] + upper[4], 0),
    }
    hole_gate = validate_hole_full_span(reopened["hole"], plan_geometry, body, ownership)
    if hole_gate["status"] in {"INCOMPLETE", "ERROR", "NOT_RUN"}:
        return _result(hole_gate["status"], "HOLE_FULL_SPAN_" + hole_gate["reason"],
                       checks=checks, hole=hole_gate)
    checks["hole_full_span"] = hole_gate["status"] == "PASS"
    return _result("PASS" if all(checks.values()) else "FAIL", "BOUNDED_R003_BREP",
                   checks=checks, hole=hole_gate)


def validate_ownership(native: Mapping[str, Any] | None,
                       source_feature_ids: list[str], expected_face_count: int | None = None) -> dict:
    if not isinstance(native, Mapping):
        return _result("NOT_RUN", "NATIVE_OWNERSHIP_NOT_RUN")
    for key in ("unknown", "unattributed", "misattributed"):
        value = native.get(key)
        if type(value) is not int or value < 0:
            return _result("INCOMPLETE", "OWNERSHIP_COUNT_MISSING")
        if value != 0:
            return _result("FAIL", "NATIVE_OWNERSHIP_" + key.upper())
    if native.get("persistent_mapping_resolved") is not True:
        return _result("FAIL", "PERSISTENT_MAPPING_UNRESOLVED")
    rows = native.get("rows")
    if not isinstance(rows, list) or not rows:
        return _result("INCOMPLETE", "NATIVE_OWNER_ROWS_MISSING")
    total = native.get("total_face_count")
    if type(total) is not int or total <= 0 or len(rows) != total:
        return _result("INCOMPLETE", "NATIVE_FACE_COVERAGE_INCOMPLETE")
    if expected_face_count is not None and total != expected_face_count:
        return _result("FAIL", "OWNERSHIP_BODY_FACE_COUNT_MISMATCH")
    feature_refs = native.get("feature_references")
    if (not isinstance(feature_refs, Mapping)
            or set(feature_refs) != set(source_feature_ids)
            or any(not isinstance(value, str) or not value for value in feature_refs.values())):
        return _result("INCOMPLETE", "NATIVE_FEATURE_REFERENCE_MAP_MISSING")
    face_refs = [row.get("persistent_reference") for row in rows]
    if any(not isinstance(value, str) or not value for value in face_refs) \
            or len(set(face_refs)) != total:
        return _result("FAIL", "FACE_PERSISTENT_REFERENCES_NOT_UNIQUE")
    if any(row.get("ownership") != "API_EXACT" or not row.get("persistent_reference")
           or row.get("native_owner_feature_id") not in source_feature_ids
           or row.get("owner_persistent_reference") != feature_refs[row["native_owner_feature_id"]]
           for row in rows):
        return _result("FAIL", "NATIVE_FACE_OWNER_UNRESOLVED")
    for feature_id in source_feature_ids:
        matching = [row for row in rows if row.get("source_feature_id") == feature_id]
        if not matching:
            return _result("FAIL", "REQUIRED_FEATURE_OWNER_ABSENT", feature_id=feature_id)
        if any(row.get("ownership") != "API_EXACT" or not row.get("persistent_reference")
               or row.get("native_owner_feature_id") != feature_id for row in matching):
            return _result("FAIL", "NON_NATIVE_OR_WRONG_OWNER", feature_id=feature_id)
    hole_id = source_feature_ids[-1]
    if not any(row.get("source_feature_id") == hole_id and row.get("role") == "HOLE_WALL"
               for row in rows):
        return _result("FAIL", "HOLE_WALL_OWNER_ABSENT")
    return _result("PASS", "EXACT_NATIVE_FACE_OWNERSHIP")


def validate_save_reopen(evidence: Mapping[str, Any] | None,
                         rebuild: Mapping[str, Any], brep: Mapping[str, Any],
                         ownership: Mapping[str, Any]) -> dict:
    if not isinstance(evidence, Mapping):
        return _result("NOT_RUN", "SAVE_REOPEN_NOT_RUN")
    if evidence.get("save_success") is False or evidence.get("close_success") is False \
            or evidence.get("reopen_success") is False:
        return _result("FAIL", "SAVE_CLOSE_OR_REOPEN_FAILED")
    required = ("save_success", "close_success", "reopen_success", "file_size",
                "sealed_sha256", "reopened_exact_path", "sealed_hash_unchanged")
    if any(key not in evidence for key in required):
        return _result("INCOMPLETE", "SAVE_REOPEN_EVIDENCE_MISSING")
    checks = {
        "save": evidence["save_success"] is True,
        "close": evidence["close_success"] is True,
        "reopen": evidence["reopen_success"] is True,
        "nonempty": type(evidence["file_size"]) is int and evidence["file_size"] > 0,
        "hash": isinstance(evidence["sealed_sha256"], str)
                and len(evidence["sealed_sha256"]) == 64 and evidence["sealed_hash_unchanged"] is True,
        "exact_file": evidence["reopened_exact_path"] is True,
        "rebuild": rebuild.get("status") == "PASS",
        "geometry": brep.get("status") == "PASS",
        "ownership": ownership.get("status") == "PASS",
    }
    return _result("PASS" if all(checks.values()) else "FAIL", "SEALED_NATIVE_SAVE_REOPEN",
                   checks=checks)


def validate_physical_views(views: Mapping[str, Any] | None) -> dict:
    if not isinstance(views, Mapping):
        return _result("NOT_RUN", "DRAWING_VIEWS_NOT_RUN")
    if set(views) != set(_NORMALS):
        return _result("FAIL", "PHYSICAL_FRONT_TOP_LEFT_REQUIRED")
    positions = {}
    for role, expected_normal in _NORMALS.items():
        row = views[role]
        if not isinstance(row, Mapping):
            return _result("ERROR", "MALFORMED_VIEW_EVIDENCE")
        required = ("native_identity", "normal", "model_to_view_transform",
                    "transform_axes", "projection_direction", "scale", "sheet_position",
                    "model_reference", "projection_standard")
        if any(key not in row for key in required):
            return _result("INCOMPLETE", role + "_FRAME_EVIDENCE_MISSING")
        try:
            native_role = canonicalize(row["native_identity"]).canonical_role.value
        except (ValueError, TypeError):
            return _result("FAIL", role + "_NATIVE_VIEW_IDENTITY_INVALID")
        if native_role != role or not _vector(row["normal"], expected_normal,
                                               AXIS_COMPONENT_TOLERANCE):
            return _result("FAIL", role + "_PHYSICAL_NORMAL_OR_IDENTITY_MISMATCH")
        transform = row["model_to_view_transform"]
        axes = row["transform_axes"]
        frame = canonicalize(row["native_identity"])
        # The adapter must normalize SW ArrayData into row-major world-to-view
        # rotation rows before providing this field. Stage 5A.2 reviews the
        # actual API layout; an arbitrary nonempty transform is not evidence.
        expected_rows = (frame.right, frame.up, frame.normal)
        rotation_valid = (isinstance(transform, (list, tuple)) and len(transform) >= 9
                          and all(_vector(transform[i * 3:i * 3 + 3], expected_rows[i],
                                          AXIS_COMPONENT_TOLERANCE) for i in range(3)))
        if (not isinstance(transform, (list, tuple)) or len(transform) < 9
                or not all(_number(value) for value in transform)
                or not isinstance(axes, Mapping)
                or not rotation_valid
                or not _vector(axes.get("normal"), expected_normal, AXIS_COMPONENT_TOLERANCE)
                or not _vector(axes.get("up"), frame.up, AXIS_COMPONENT_TOLERANCE)
                or not _vector(axes.get("right"), frame.right, AXIS_COMPONENT_TOLERANCE)
                or not _vector(row["projection_direction"], expected_normal,
                               AXIS_COMPONENT_TOLERANCE)):
            return _result("FAIL", role + "_TRANSFORM_OR_PROJECTION_MISMATCH")
        if (not _number(row["scale"]) or row["scale"] <= 0
                or not isinstance(row["model_reference"], str) or not row["model_reference"]
                or row["projection_standard"] != "THIRD_ANGLE"
                or not isinstance(row["sheet_position"], (list, tuple))
                or len(row["sheet_position"]) != 2
                or not all(_number(v) for v in row["sheet_position"])):
            return _result("FAIL", role + "_SCALE_REFERENCE_OR_PROJECTION_INVALID")
        positions[role] = row["sheet_position"]
    if not (positions["LEFT"][0] < positions["FRONT"][0]
            and positions["TOP"][1] > positions["FRONT"][1]):
        return _result("FAIL", "THIRD_ANGLE_PLACEMENT_MISMATCH")
    if len({views[role]["model_reference"] for role in views}) != 1:
        return _result("FAIL", "VIEWS_REFERENCE_DIFFERENT_MODELS")
    return _result("PASS", "PHYSICAL_FRONT_TOP_LEFT_CONFIRMED",
                   physical_left_view_confirmed=True)


def validate_roundtrip(source: Mapping[str, Any] | None,
                       generated: Mapping[str, Any] | None,
                       views: Mapping[str, Any] | None) -> dict:
    frames = validate_physical_views(views)
    if frames["status"] != "PASS":
        return _result(frames["status"], "VIEW_FRAME_" + frames["reason"], frames=frames)
    if not isinstance(source, Mapping) or not isinstance(generated, Mapping):
        return _result("INCOMPLETE", "SOURCE_OR_GENERATED_PRIMITIVES_MISSING")
    digest = source.get("reviewed_projection_graph_digest")
    if (not isinstance(digest, str) or len(digest) != 64
            or generated.get("reviewed_projection_graph_digest") != digest):
        return _result("INCOMPLETE", "REVIEWED_PROJECTION_PROVENANCE_MISSING")
    if (not any(source.get(role, {}).get("visible_lines") for role in _NORMALS)
            or not any(source.get(role, {}).get("circles") for role in _NORMALS)
            or not any(source.get(role, {}).get("hidden_lines") for role in _NORMALS)):
        return _result("INCOMPLETE", "SOURCE_PROFILE_OR_HOLE_SUPPORT_MISSING")
    if "comparison_projection" in source:
        from .r003_three_view_correspondence import compare_three_views
        if (not isinstance(source["comparison_projection"], Mapping)
                or any(not isinstance(generated.get(role), Mapping)
                       or not isinstance(generated[role].get("native_hlr_evidence"), Mapping)
                       for role in ("FRONT", "TOP", "LEFT"))):
            return _result("INCOMPLETE", "THREE_VIEW_NATIVE_HLR_EVIDENCE_MISSING")
        try:
            correspondence = compare_three_views(
                source["comparison_projection"],
                {role: generated[role]["native_hlr_evidence"]
                 for role in ("FRONT", "TOP", "LEFT")},
                generated, views)
        except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
            return _result("INCOMPLETE", "THREE_VIEW_SOURCE_NATIVE_FRAME_OR_SUPPORT_INVALID",
                           diagnostic=str(exc))
        if correspondence["status"] != "PASS":
            return _result("FAIL", "THREE_VIEW_SOURCE_NATIVE_SUPPORT_MISMATCH",
                           reports=correspondence["views"], frames=frames)
        return _result("PASS", "SOURCE_BACKED_PHYSICAL_THREE_VIEW_ROUNDTRIP",
                       reports=correspondence["views"], frames=frames)
    reports = {}
    for role in ("FRONT", "TOP", "LEFT"):
        source_row, generated_row = source.get(role), generated.get(role)
        if not isinstance(source_row, Mapping) or not isinstance(generated_row, Mapping):
            return _result("INCOMPLETE", role + "_PRIMITIVES_MISSING")
        if source_row.get("unresolved_discriminating_primitives") != 0 \
                or generated_row.get("unresolved_discriminating_primitives") != 0:
            return _result("FAIL", role + "_DISCRIMINATING_PRIMITIVE_UNRESOLVED")
        if generated_row.get("visible_splines") or generated_row.get("hidden_splines"):
            return _result("INCOMPLETE", role + "_SOURCE_NATIVE_EVIDENCE_REQUIRED")
        reports[role] = {}
        for kind in ("visible_lines", "hidden_lines", "circles", "arcs"):
            expected, actual = source_row.get(kind), generated_row.get(kind)
            if not isinstance(expected, list) or not isinstance(actual, list):
                return _result("INCOMPLETE", role + "_" + kind.upper() + "_MISSING")
            try:
                report = match(expected, actual, {"visible_lines": "line",
                                                 "hidden_lines": "line",
                                                 "circles": "circle", "arcs": "arc"}[kind])
            except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
                return _result("ERROR", "PRIMITIVE_MATCHER_ERROR", diagnostic=str(exc))
            reports[role][kind] = report
            if report["status"] != "PASS":
                return _result("FAIL", role + "_" + kind.upper() + "_MISMATCH",
                               reports=reports, frames=frames)
    return _result("PASS", "SOURCE_BACKED_PHYSICAL_ROUNDTRIP",
                   reports=reports, frames=frames)


def compose_six_gates(gates: Mapping[str, Mapping[str, Any]]) -> dict:
    if not isinstance(gates, Mapping) or set(gates) != set(GATES):
        return _result("ERROR", "SIX_EXACT_GATES_REQUIRED", r003_engineering_pass=False)
    statuses = []
    for name in GATES:
        row = gates[name]
        if not isinstance(row, Mapping) or row.get("status") not in STATUSES:
            return _result("ERROR", name + "_INVALID_GATE_STATUS", r003_engineering_pass=False)
        statuses.append(row["status"])
    if "ERROR" in statuses:
        status = "ERROR"
    elif "FAIL" in statuses:
        status = "FAIL"
    elif "INCOMPLETE" in statuses or "NOT_RUN" in statuses:
        status = "INCOMPLETE"
    else:
        status = "PASS"
    return _result(status, "ALL_SIX_GATES_PASS" if status == "PASS" else "SIX_GATE_NONPASS",
                   gate_statuses=dict(zip(GATES, statuses)),
                   r003_engineering_pass=status == "PASS")


def validate_evidence_bundle(bundle: Mapping[str, Any]) -> dict:
    """R003 Process B boundary: source-backed native evidence -> six gates.

    A fake bundle exercises logic only. The harness must keep its verdict
    marked FAKE until a reviewed native collector and Stage 5A.2 gate exist.
    """
    if not isinstance(bundle, Mapping):
        return {"gates": {}, "composition": _result("ERROR", "BUNDLE_MISSING",
                                                     r003_engineering_pass=False)}
    required = ("initial_rebuild", "reopened_rebuild", "required_feature_ids",
                "expected_native_refs", "body", "brep", "plan_geometry", "ownership",
                "save_reopen", "source_views", "generated_views", "native_views")
    if any(key not in bundle for key in required):
        return {"gates": {}, "composition": _result("INCOMPLETE", "BUNDLE_FIELD_MISSING",
                                                     r003_engineering_pass=False)}
    rebuild = validate_strict_rebuild(bundle["initial_rebuild"], bundle["reopened_rebuild"],
                                      bundle["required_feature_ids"], bundle["expected_native_refs"])
    body = validate_body_count(bundle["body"])
    ownership = validate_ownership(bundle["ownership"], bundle["required_feature_ids"],
                                   bundle["body"].get("face_count"))
    brep = validate_brep_geometry(bundle["brep"], bundle["plan_geometry"],
                                  bundle["body"], bundle["ownership"])
    save = validate_save_reopen(bundle["save_reopen"], rebuild, brep, ownership)
    drawing = validate_roundtrip(bundle["source_views"], bundle["generated_views"],
                                 bundle["native_views"])
    gates = {"REBUILD": rebuild, "BODY_COUNT": body, "BREP_GEOMETRY": brep,
             "FEATURE_OWNERSHIP": ownership, "SAVE_REOPEN": save,
             "DRAWING_ROUNDTRIP": drawing}
    return {"gates": gates, "composition": compose_six_gates(gates)}
