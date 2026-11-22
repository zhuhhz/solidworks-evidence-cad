"""R001 adapter from one canonical BASE_BLOCK graph to one canonical plan."""

from __future__ import annotations

from typing import Any, Mapping

from experiments.three_view_reconstruction.schemas.feature_graph import FeatureGraph
from experiments.three_view_reconstruction.schemas.modeling_plan import (
    ModelingOperation,
    ModelingPlan,
)

from .base_block_reasoner import (
    CAPABILITY_SCOPE,
    GENERATION_TRUTH_READS,
    validate_confirmation_artifact,
)


SCHEMA_VERSION = "cad-reasoning-v1.r001-modeling-plan-candidate/1.0"


def build_base_block_modeling_plan(
    graph: FeatureGraph,
    reasoning_result: Mapping[str, Any],
    confirmation: Mapping[str, Any],
) -> tuple[ModelingPlan, dict[str, Any]]:
    """Build one backend-neutral base extrusion after all R001 gates pass."""
    if not isinstance(graph, FeatureGraph):
        raise ValueError("canonical FeatureGraph is required")
    validate_confirmation_artifact(confirmation)
    if (not isinstance(reasoning_result, Mapping)
            or reasoning_result.get("status") != "INFERRED"
            or reasoning_result.get("capability_scope") != CAPABILITY_SCOPE):
        raise ValueError("INFERRED R001 reasoning result is required")
    checks = reasoning_result.get("checks")
    if (not isinstance(checks, Mapping) or checks.get("feature_count") != 1
            or checks.get("unresolved_reference_count") != 0
            or checks.get("unconsumed_evidence_count") != 0
            or checks.get("generation_truth_reads") != GENERATION_TRUTH_READS):
        raise ValueError("R001 completeness and truth-isolation gates must pass")
    if reasoning_result.get("feature_graph") != graph.to_dict():
        raise ValueError("R001 result does not match the canonical FeatureGraph")
    if graph.holes or graph.bosses or graph.slots or graph.hypotheses:
        raise ValueError("R001 permits exactly one BASE_BLOCK and no other features")

    base = graph.base_block
    node = base.to_feature_node()
    if (node.feature_type != "BASE_BLOCK" or node.dependencies
            or not node.feature_id or not node.source_evidence_ids):
        raise ValueError("canonical BASE_BLOCK identity and evidence are required")
    operation_id = "op_" + node.feature_id
    operation = ModelingOperation(
        "base_extrude",
        "Front Plane",
        {
            "type": "rectangle",
            "width_mm": base.width,
            "height_mm": base.height,
        },
        depth_mm=base.depth,
        operation_id=operation_id,
        source_feature_id=node.feature_id,
        depends_on_operation_ids=[],
    )
    plan = ModelingPlan([operation])
    report = {
        "schema_version": SCHEMA_VERSION,
        "status": "VALID",
        "capability_scope": CAPABILITY_SCOPE,
        "operation_ids": [operation_id],
        "source_feature_ids": [node.feature_id],
        "dependency_edges": [],
        "generation_truth_reads": GENERATION_TRUTH_READS,
        "backend_execution": "NOT_REQUESTED",
        "modeling_plan": plan.to_dict(),
    }
    return plan, report


__all__ = ["build_base_block_modeling_plan"]
