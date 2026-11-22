"""Bounded B010 FeatureGraph Candidate to existing ModelingPlan bridge."""
from __future__ import annotations

import math

from experiments.three_view_reconstruction.schemas.feature_graph import (
    BaseBlock, Boss, FeatureGraph, Hole, StraightSlot,
)
from experiments.three_view_reconstruction.schemas.modeling_plan import ModelingOperation, ModelingPlan


SCHEMA_VERSION = "b010.modeling-plan-candidate/0.1"
COORDINATE_SYSTEM = "MODEL_CENTERED_XY_MM"


def _finite(value, field, *, positive=False):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"{field} must be a finite number")
    value = float(value)
    if positive and value <= 0:
        raise ValueError(f"{field} must be positive")
    return value


def _require_keys(value, required, allowed, label):
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    missing = sorted(set(required) - set(value))
    unexpected = sorted(set(value) - set(allowed))
    if missing:
        raise ValueError(f"{label} missing required parameter: {', '.join(missing)}")
    if unexpected:
        raise ValueError(f"{label} contains unsupported fields: {', '.join(unexpected)}")


def _validate_identity(node, feature_type):
    if node.get("feature_type") != feature_type:
        raise ValueError(f"expected {feature_type} feature")
    feature_id = node.get("feature_id")
    if not isinstance(feature_id, str) or not feature_id:
        raise ValueError(f"{feature_type} requires a stable feature_id")
    if node.get("coordinate_system") != COORDINATE_SYSTEM:
        raise ValueError(f"{feature_type} requires {COORDINATE_SYSTEM}")
    evidence = node.get("source_evidence_ids")
    if not isinstance(evidence, list) or not evidence or any(not isinstance(x, str) or not x for x in evidence):
        raise ValueError(f"{feature_type} requires source evidence IDs")
    if len(evidence) != len(set(evidence)):
        raise ValueError(f"{feature_type} has duplicate source evidence IDs")
    dependencies = node.get("dependencies")
    if not isinstance(dependencies, list) or any(not isinstance(x, str) or not x for x in dependencies):
        raise ValueError(f"{feature_type} dependencies must be feature IDs")
    return feature_id


def _validate_generated_plan(graph, plan):
    """Validate the bounded B010 DAG without changing frozen B005-B007 rules."""
    nodes = graph.to_feature_nodes()
    node_by_id = {node.feature_id: node for node in nodes}
    operations = plan.operations
    operation_ids = [row.operation_id for row in operations]
    source_ids = [row.source_feature_id for row in operations]
    if (any(not value for value in operation_ids)
            or len(operation_ids) != len(set(operation_ids))):
        raise ValueError("operation IDs must be present and unique")
    if len(source_ids) != len(set(source_ids)) or set(source_ids) != set(node_by_id):
        raise ValueError("each feature must resolve to exactly one operation")
    operation_by_feature = {row.source_feature_id: row for row in operations}
    operation_id_set = set(operation_ids)
    expected_types = {"BASE_BLOCK": "base_extrude", "BOSS": "boss_extrude",
                      "THROUGH_HOLE": "cut_extrude_through_circle",
                      "STRAIGHT_SLOT": "cut_extrude_through_slot"}
    for node in nodes:
        operation = operation_by_feature[node.feature_id]
        if operation.type != expected_types[node.feature_type]:
            raise ValueError(f"unsupported operation for {node.feature_type}")
        expected = [operation_by_feature[value].operation_id for value in node.dependencies]
        if operation.depends_on_operation_ids != expected:
            raise ValueError(f"operation dependency mismatch for {node.feature_id}")
        if any(value not in operation_id_set for value in operation.depends_on_operation_ids):
            raise ValueError(f"unresolved operation dependency for {node.feature_id}")
    completed = set()
    for operation in operations:
        if not set(operation.depends_on_operation_ids) <= completed:
            raise ValueError("ModelingPlan dependency cycle or execution-order violation")
        completed.add(operation.operation_id)
    return {"status": "PASS", "operation_ids": operation_ids,
            "source_feature_ids": source_ids,
            "dependency_edges": [{"parent": dependency, "child": operation.operation_id}
                                 for operation in operations
                                 for dependency in operation.depends_on_operation_ids]}


def _build_slot_modeling_plan(candidate, base_node, slot_node):
    """Map the bounded B010-003 X-major through slot without idealizing it."""
    base_id = _validate_identity(base_node, "BASE_BLOCK")
    slot_id = _validate_identity(slot_node, "STRAIGHT_SLOT")
    if base_id == slot_id:
        raise ValueError("feature IDs must be unique")
    if base_node["dependencies"]:
        raise ValueError("BASE_BLOCK must be the dependency root")
    if slot_node["dependencies"] != [base_id]:
        raise ValueError("STRAIGHT_SLOT must depend exactly on BASE_BLOCK")
    if set(base_node["source_evidence_ids"]) & set(slot_node["source_evidence_ids"]):
        raise ValueError("source evidence cannot be shared across features")

    base_parameters = base_node.get("parameters")
    base_fields = {"width_mm", "height_mm", "depth_mm"}
    _require_keys(base_parameters, base_fields, base_fields, "BASE_BLOCK")
    width = _finite(base_parameters["width_mm"], "width_mm", positive=True)
    height = _finite(base_parameters["height_mm"], "height_mm", positive=True)
    depth = _finite(base_parameters["depth_mm"], "depth_mm", positive=True)

    slot_parameters = slot_node.get("parameters")
    slot_fields = {"overall_length_mm", "width_mm", "radius_mm", "center_x_mm",
                   "center_y_mm", "major_axis", "through"}
    _require_keys(slot_parameters, slot_fields, slot_fields, "STRAIGHT_SLOT")
    overall_length = _finite(slot_parameters["overall_length_mm"],
                             "STRAIGHT_SLOT.overall_length_mm", positive=True)
    slot_width = _finite(slot_parameters["width_mm"], "STRAIGHT_SLOT.width_mm", positive=True)
    radius = _finite(slot_parameters["radius_mm"], "STRAIGHT_SLOT.radius_mm", positive=True)
    center_x = _finite(slot_parameters["center_x_mm"], "STRAIGHT_SLOT.center_x_mm")
    center_y = _finite(slot_parameters["center_y_mm"], "STRAIGHT_SLOT.center_y_mm")
    if slot_parameters["major_axis"] != "X":
        raise ValueError("B010-003 supports only an explicitly X-major STRAIGHT_SLOT")
    if slot_parameters["through"] is not True:
        raise ValueError("B010-003 requires through=true")
    # The frontend measures width and both end arcs independently. Preserve
    # both values while rejecting a shape that cannot be a semicircular slot.
    if abs(slot_width - 2.0 * radius) > 0.05:
        raise ValueError("STRAIGHT_SLOT width and observed end radius are inconsistent")
    center_distance = overall_length - 2.0 * radius
    if center_distance <= 0 or overall_length <= slot_width:
        raise ValueError("STRAIGHT_SLOT overall length cannot resolve two distinct end centers")
    if (abs(center_x) + overall_length / 2.0 >= width / 2.0
            or abs(center_y) + max(radius, slot_width / 2.0) >= height / 2.0):
        raise ValueError("STRAIGHT_SLOT boundary must remain inside BASE_BLOCK")

    base = BaseBlock(width, height, depth, feature_id=base_id,
                     source_evidence_ids=list(base_node["source_evidence_ids"]), dependencies=[])
    slot = StraightSlot(
        overall_length, slot_width, radius, center_x, center_y, "X", True,
        feature_id=slot_id, source_evidence_ids=list(slot_node["source_evidence_ids"]),
        dependencies=[base_id],
    )
    graph = FeatureGraph(base, slots=[slot], status="NOT_VALIDATED")
    if candidate.get("feature_graph") != graph.to_dict():
        raise ValueError("typed FeatureGraph does not match feature_nodes")
    normalized = sorted((node.to_dict() for node in graph.to_feature_nodes()),
                        key=lambda row: row["feature_id"])
    declared = sorted(candidate["feature_nodes"], key=lambda row: row["feature_id"])
    if normalized != declared:
        raise ValueError("feature_nodes do not match the typed FeatureGraph Candidate")

    base_operation_id, slot_operation_id = "op_" + base_id, "op_" + slot_id
    operations = [
        ModelingOperation(
            "base_extrude", "Front Plane",
            {"type": "rectangle", "width_mm": width, "height_mm": height}, depth,
            operation_id=base_operation_id, source_feature_id=base_id,
            depends_on_operation_ids=[],
        ),
        ModelingOperation(
            "cut_extrude_through_slot", "Front Plane",
            {"type": "straight_slot", "overall_length_mm": overall_length,
             "center_to_center_length_mm": center_distance, "width_mm": slot_width,
             "radius_mm": radius, "center_x_mm": center_x, "center_y_mm": center_y,
             "major_axis": "X", "through": True},
            direction="through_all", operation_id=slot_operation_id,
            source_feature_id=slot_id, depends_on_operation_ids=[base_operation_id],
        ),
    ]
    plan = ModelingPlan(operations)
    validation = _validate_generated_plan(graph, plan)
    report = {
        "schema_version": SCHEMA_VERSION, "status": "VALID",
        "units": {"candidate": "mm", "solidworks_api": "m",
                  "conversion": "sw_connect.mm(value)=value/1000"},
        "coordinate_system": COORDINATE_SYSTEM,
        "operation_ids": [row.operation_id for row in operations],
        "source_feature_ids": [row.source_feature_id for row in operations],
        "dependency_edges": [{"parent": base_operation_id, "child": slot_operation_id}],
        "parameter_source": "FeatureGraph Candidate feature_nodes",
        "normalization": "NONE",
        "slot_length_semantics": {
            "candidate": "overall end-to-end length",
            "solidworks_endpoints": "end-circle centers",
            "mapping": "center_to_center_length_mm = overall_length_mm - 2 * radius_mm",
            "observed_radius_preserved": True,
        },
        "validation": validation, "modeling_plan": plan.to_dict(),
    }
    return graph, plan, report


def build_modeling_plan_candidate(candidate):
    """Validate a reviewed B010-001/002/003 graph and map its exact mm values.

    No truth, benchmark file, CAD file, or evaluator result is accepted by this
    API. The returned plan uses the already-established backend operations.
    """
    if not isinstance(candidate, dict) or candidate.get("status") != "INFERRED":
        raise ValueError("FeatureGraph Candidate must have status INFERRED")
    reviewed = candidate.get("reviewed_inputs")
    if not isinstance(reviewed, dict) or reviewed.get("confirmation_decision") not in {"CONFIRMED", "CORRECTED"}:
        raise ValueError("valid Human Confirmation is required")
    checks = candidate.get("checks") or {}
    if checks.get("unresolved_reference_count") != 0 or checks.get("unconsumed_evidence_count") != 0:
        raise ValueError("FeatureGraph Candidate has unresolved or unconsumed evidence")
    nodes = candidate.get("feature_nodes")
    if not isinstance(nodes, list):
        raise ValueError("FeatureGraph Candidate feature_nodes are required")
    by_type = {}
    for node in nodes:
        if not isinstance(node, dict) or node.get("feature_type") in by_type:
            raise ValueError("FeatureGraph Candidate contains duplicate or invalid feature types")
        by_type[node.get("feature_type")] = node
    feature_types = set(by_type)
    # Early B010-001 unit candidates predate the explicit feature_count field.
    # Preserve that validated two-feature contract when the field is absent.
    declared_count = checks.get("feature_count", 2)
    two_feature_cases = ({"BASE_BLOCK", "THROUGH_HOLE"}, {"BASE_BLOCK", "STRAIGHT_SLOT"})
    if declared_count == 2 and feature_types not in two_feature_cases:
        if "STRAIGHT_SLOT" in feature_types:
            raise ValueError("B010-003 supports exactly BASE_BLOCK and STRAIGHT_SLOT")
        raise ValueError("B010-001 supports exactly BASE_BLOCK and THROUGH_HOLE")
    if declared_count == 3 and feature_types != {"BASE_BLOCK", "BOSS", "THROUGH_HOLE"}:
        raise ValueError("B010-002 supports exactly BASE_BLOCK, BOSS and THROUGH_HOLE")
    if declared_count not in {2, 3}:
        raise ValueError("FeatureGraph Candidate feature_count must be 2 or 3")

    if feature_types == {"BASE_BLOCK", "STRAIGHT_SLOT"}:
        return _build_slot_modeling_plan(candidate, by_type["BASE_BLOCK"],
                                         by_type["STRAIGHT_SLOT"])

    base_node, hole_node = by_type["BASE_BLOCK"], by_type["THROUGH_HOLE"]
    boss_node = by_type.get("BOSS")
    base_id = _validate_identity(base_node, "BASE_BLOCK")
    boss_id = _validate_identity(boss_node, "BOSS") if boss_node else None
    hole_id = _validate_identity(hole_node, "THROUGH_HOLE")
    feature_ids = [base_id, *([boss_id] if boss_id else []), hole_id]
    if len(feature_ids) != len(set(feature_ids)):
        raise ValueError("feature IDs must be unique")
    if base_node["dependencies"]:
        raise ValueError("BASE_BLOCK must be the dependency root")
    if boss_node:
        if boss_node["dependencies"] != [base_id]:
            raise ValueError("BOSS must depend exactly on BASE_BLOCK")
        if hole_node["dependencies"] != [boss_id]:
            raise ValueError("THROUGH_HOLE must depend exactly on BOSS")
    elif hole_node["dependencies"] != [base_id]:
        raise ValueError("THROUGH_HOLE must depend exactly on BASE_BLOCK")
    base_evidence = set(base_node["source_evidence_ids"])
    hole_evidence = set(hole_node["source_evidence_ids"])
    boss_evidence = set(boss_node["source_evidence_ids"]) if boss_node else set()
    # B009-002 intentionally shares explicit adjacent-profile relationship
    # evidence between Base and Boss. Hole evidence remains independently owned.
    if hole_evidence & (base_evidence | boss_evidence):
        message = ("THROUGH_HOLE evidence cannot be shared across features" if boss_node
                   else "source evidence cannot be shared across features")
        raise ValueError(message)

    base_parameters = base_node.get("parameters")
    _require_keys(base_parameters, {"width_mm", "height_mm", "depth_mm"},
                  {"width_mm", "height_mm", "depth_mm"}, "BASE_BLOCK")
    width = _finite(base_parameters["width_mm"], "width_mm", positive=True)
    height = _finite(base_parameters["height_mm"], "height_mm", positive=True)
    depth = _finite(base_parameters["depth_mm"], "depth_mm", positive=True)

    boss = None
    if boss_node:
        boss_parameters = boss_node.get("parameters")
        boss_fields = {"width_mm", "height_mm", "depth_mm", "centered"}
        _require_keys(boss_parameters, boss_fields, boss_fields, "BOSS")
        boss_width = _finite(boss_parameters["width_mm"], "BOSS.width_mm", positive=True)
        boss_height = _finite(boss_parameters["height_mm"], "BOSS.height_mm", positive=True)
        boss_depth = _finite(boss_parameters["depth_mm"], "BOSS.depth_mm", positive=True)
        if boss_parameters["centered"] is not True:
            raise ValueError("B010-002 requires an explicitly centered BOSS")
        if boss_width >= width or boss_height >= height:
            raise ValueError("BOSS profile must lie strictly inside BASE_BLOCK")
        boss = Boss(boss_width, boss_height, boss_depth, True, feature_id=boss_id,
                    source_evidence_ids=list(boss_node["source_evidence_ids"]),
                    dependencies=[base_id])

    hole_parameters = hole_node.get("parameters")
    hole_fields = {"diameter_mm", "center_x_mm", "center_y_mm", "axis", "through"}
    _require_keys(hole_parameters, hole_fields, hole_fields, "THROUGH_HOLE")
    diameter = _finite(hole_parameters["diameter_mm"], "diameter_mm", positive=True)
    center_x = _finite(hole_parameters["center_x_mm"], "center_x_mm")
    center_y = _finite(hole_parameters["center_y_mm"], "center_y_mm")
    if hole_parameters["axis"] != "Z":
        raise ValueError("B010-001 supports only a Z-axis through hole")
    if hole_parameters["through"] is not True:
        raise ValueError("B010-001 requires through=true")
    support_width = boss.width if boss else width
    support_height = boss.height if boss else height
    if diameter >= min(support_width, support_height):
        raise ValueError("through-hole diameter must fit inside its terminal support profile")
    radius = diameter / 2.0
    if (abs(center_x) + radius >= support_width / 2.0
            or abs(center_y) + radius >= support_height / 2.0):
        raise ValueError("through-hole boundary must remain inside its terminal support profile")

    base = BaseBlock(width, height, depth, feature_id=base_id,
                     source_evidence_ids=list(base_node["source_evidence_ids"]), dependencies=[])
    hole = Hole(diameter, center_x, center_y, "Z", True, feature_id=hole_id,
                source_evidence_ids=list(hole_node["source_evidence_ids"]),
                dependencies=[boss_id or base_id])
    graph = FeatureGraph(base, holes=[hole], bosses=[boss] if boss else [], status="NOT_VALIDATED")
    if candidate.get("feature_graph") != graph.to_dict():
        raise ValueError("typed FeatureGraph does not match feature_nodes")
    normalized = sorted((node.to_dict() for node in graph.to_feature_nodes()),
                        key=lambda row: row["feature_id"])
    declared = sorted(nodes, key=lambda row: row["feature_id"])
    if normalized != declared:
        raise ValueError("feature_nodes do not match the typed FeatureGraph Candidate")

    base_operation_id = "op_" + base_id
    boss_operation_id = "op_" + boss_id if boss_id else None
    hole_operation_id = "op_" + hole_id
    operations = [
        ModelingOperation(
            "base_extrude", "Front Plane",
            {"type": "rectangle", "width_mm": width, "height_mm": height}, depth,
            operation_id=base_operation_id, source_feature_id=base_id,
            depends_on_operation_ids=[],
        ),
    ]
    if boss:
        operations.append(ModelingOperation(
            "boss_extrude", "Plane_Base_Top",
            {"type": "rectangle", "width_mm": boss.width, "height_mm": boss.height,
             "plane_offset_mm": depth}, boss.depth,
            operation_id=boss_operation_id, source_feature_id=boss_id,
            depends_on_operation_ids=[base_operation_id],
        ))
    operations.append(ModelingOperation(
            "cut_extrude_through_circle", "Plane_Boss_Top" if boss else "Front Plane",
            {"type": "circle", "diameter_mm": diameter, "center_x_mm": center_x,
             "center_y_mm": center_y,
             "plane_offset_mm": depth + (boss.depth if boss else 0.0)},
            direction="through_all", operation_id=hole_operation_id,
            source_feature_id=hole_id,
            depends_on_operation_ids=[boss_operation_id or base_operation_id],
        ))
    plan = ModelingPlan(operations)
    validation = _validate_generated_plan(graph, plan)
    dependency_edges = [{"parent": dependency, "child": operation.operation_id}
                        for operation in operations for dependency in operation.depends_on_operation_ids]
    report = {
        "schema_version": SCHEMA_VERSION,
        "status": "VALID",
        "units": {"candidate": "mm", "solidworks_api": "m", "conversion": "sw_connect.mm(value)=value/1000"},
        "coordinate_system": COORDINATE_SYSTEM,
        "operation_ids": [operation.operation_id for operation in operations],
        "source_feature_ids": [operation.source_feature_id for operation in operations],
        "dependency_edges": dependency_edges,
        "parameter_source": "FeatureGraph Candidate feature_nodes",
        "normalization": "NONE",
        "validation": validation,
        "modeling_plan": plan.to_dict(),
    }
    return graph, plan, report
