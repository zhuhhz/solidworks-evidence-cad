"""R001 fail-closed BASE_BLOCK reasoning over canonical drawing evidence."""

from __future__ import annotations

from collections import Counter
from hashlib import sha256
import json
import math
from typing import Any, Mapping, Sequence

from experiments.three_view_reconstruction.schemas.feature_evidence import FeatureEvidence
from experiments.three_view_reconstruction.schemas.feature_graph import BaseBlock, FeatureGraph
from experiments.three_view_reconstruction.schemas.projection_graph import ProjectionGraph
from experiments.three_view_reconstruction.schemas.view_geometry import ViewGeometry


CAPABILITY_SCOPE = "R001_BASE_BLOCK_ONLY"
SCHEMA_VERSION = "cad-reasoning-v1.r001-feature-graph-candidate/1.0"
VERSION = "cad-reasoning-v1.r001/1.0"
GENERATION_TRUTH_READS = 0
_TOLERANCE_MM = 0.10


def validate_confirmation_artifact(confirmation: Mapping[str, Any]) -> None:
    """Validate the shape of an artifact already returned by the human gate."""
    required = {
        "schema_version", "version", "decision", "reviewer", "corrections",
        "sources", "machine_state", "provenance",
    }
    if not isinstance(confirmation, Mapping) or set(confirmation) != required:
        raise ValueError("exact validated Human Confirmation fields are required")
    if (confirmation.get("schema_version") != "human-confirmation/0.1"
            or confirmation.get("version") != "human-confirmation-mvp/0.1"):
        raise ValueError("unsupported Human Confirmation contract")
    if confirmation.get("decision") != "CONFIRMED" or confirmation.get("corrections") != []:
        raise ValueError("R001 requires an unmodified CONFIRMED review")
    provenance = confirmation.get("provenance")
    if (not isinstance(provenance, Mapping)
            or provenance.get("decision_source") != "EXPLICIT_HUMAN_INPUT"):
        raise ValueError("Human Confirmation provenance is invalid")


def _close(left: float, right: float) -> bool:
    return abs(float(left) - float(right)) <= _TOLERANCE_MM


def _stable_feature_id(parameters: Mapping[str, float], evidence_ids: Sequence[str]) -> str:
    payload = ["BASE_BLOCK", parameters, sorted(evidence_ids)]
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return "inferred-feature." + sha256(canonical.encode()).hexdigest()[:24]


def _rectangle_dimensions(view: ViewGeometry) -> tuple[float, float]:
    lines = view.visible_segments
    if len(lines) < 4:
        raise LookupError("outer profile is incomplete")
    if len(lines) > 4:
        raise ArithmeticError("multiple or nested outer profiles are ambiguous")

    coordinates = []
    horizontal = []
    vertical = []
    for line in lines:
        values = (line.x1, line.y1, line.x2, line.y2)
        if not all(math.isfinite(float(value)) for value in values):
            raise ValueError("profile coordinates must be finite")
        if _close(line.y1, line.y2) and not _close(line.x1, line.x2):
            horizontal.append(line)
        elif _close(line.x1, line.x2) and not _close(line.y1, line.y2):
            vertical.append(line)
        else:
            raise ValueError("R001 supports only non-zero axis-aligned profile lines")
        coordinates.extend(((float(line.x1), float(line.y1)),
                            (float(line.x2), float(line.y2))))
    if len(horizontal) != 2 or len(vertical) != 2:
        raise ValueError("four lines do not form one axis-aligned rectangle")

    x_lo = min(point[0] for point in coordinates)
    x_hi = max(point[0] for point in coordinates)
    y_lo = min(point[1] for point in coordinates)
    y_hi = max(point[1] for point in coordinates)
    width, height = x_hi - x_lo, y_hi - y_lo
    if not all(math.isfinite(value) and value > 0 for value in (width, height)):
        raise ValueError("profile dimensions must be finite and positive")

    horizontal_sides = []
    for line in horizontal:
        if not (_close(min(line.x1, line.x2), x_lo)
                and _close(max(line.x1, line.x2), x_hi)):
            raise ValueError("horizontal profile edge does not span the rectangle")
        horizontal_sides.append("LOW" if _close(line.y1, y_lo) else
                                "HIGH" if _close(line.y1, y_hi) else "INTERIOR")
    vertical_sides = []
    for line in vertical:
        if not (_close(min(line.y1, line.y2), y_lo)
                and _close(max(line.y1, line.y2), y_hi)):
            raise ValueError("vertical profile edge does not span the rectangle")
        vertical_sides.append("LOW" if _close(line.x1, x_lo) else
                              "HIGH" if _close(line.x1, x_hi) else "INTERIOR")
    if sorted(horizontal_sides) != ["HIGH", "LOW"] or sorted(vertical_sides) != ["HIGH", "LOW"]:
        raise ValueError("profile does not contain each rectangle boundary exactly once")
    if (not _close(width, view.horizontal_extent)
            or not _close(height, view.vertical_extent)):
        raise ValueError("profile dimensions disagree with canonical view extents")
    return width, height


def _empty_result(
    status: str,
    issues: Sequence[str],
    confirmation: Mapping[str, Any] | None,
    *,
    unconsumed: int = 0,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "version": VERSION,
        "capability_scope": CAPABILITY_SCOPE,
        "status": status,
        "feature_graph": None,
        "feature_nodes": [],
        "feature_provenance": [],
        "reviewed_inputs": {
            "confirmation_decision": confirmation.get("decision")
            if isinstance(confirmation, Mapping) else None,
            "confirmation_schema_version": confirmation.get("schema_version")
            if isinstance(confirmation, Mapping) else None,
        },
        "checks": {
            "feature_count": 0,
            "unresolved_reference_count": 0,
            "unconsumed_evidence_count": unconsumed,
            "generation_truth_reads": GENERATION_TRUTH_READS,
        },
        "issues": sorted(set(issues)),
    }


class BaseBlockReasoner:
    """Infer exactly one rectangular BASE_BLOCK or return an explicit failure state."""

    def reason(
        self,
        projection: ProjectionGraph,
        evidence: Sequence[FeatureEvidence],
        confirmation: Mapping[str, Any],
        *,
        capability_scope: str,
    ) -> tuple[FeatureGraph | None, dict[str, Any]]:
        if capability_scope != CAPABILITY_SCOPE:
            return None, _empty_result(
                "UNSUPPORTED_FEATURE", ["unsupported capability scope"], confirmation)
        if not isinstance(projection, ProjectionGraph):
            return None, _empty_result(
                "FAIL", ["canonical ProjectionGraph is required"], confirmation)
        try:
            validate_confirmation_artifact(confirmation)
        except (TypeError, ValueError) as exc:
            return None, _empty_result("FAIL", [str(exc)], confirmation)

        views = {"front": projection.front, "top": projection.top, "left": projection.left}
        if (projection.projection != "third_angle"
                or any(not isinstance(view, ViewGeometry) or view.name != name
                       for name, view in views.items())):
            return None, _empty_result(
                "NOT_VALIDATED", ["exactly three supported orthographic views are required"],
                confirmation)
        convention = projection.coordinate_convention
        if (not isinstance(convention, dict) or convention.get("units") != "mm"
                or convention.get("front") != "X/Y" or convention.get("top") != "X/Z"
                or convention.get("left") != "Z/Y"):
            return None, _empty_result(
                "UNSUPPORTED_FEATURE", ["unsupported projection coordinate convention"],
                confirmation)
        if (projection.feature_evidence is not None
                or projection.feature_evidence_records
                or projection.expected_features is not None):
            return None, _empty_result(
                "FAIL", ["R001 accepts geometry-only canonical ProjectionGraph input"],
                confirmation)

        visible_by_id: dict[str, tuple[str, Any]] = {}
        all_primitive_ids: set[str] = set()
        for view_name, view in views.items():
            unsupported = (
                len(view.circles) + len(view.arcs) + len(view.hidden_segments)
                + len(view.hidden_line_pairs)
            )
            if unsupported:
                return None, _empty_result(
                    "UNSUPPORTED_FEATURE",
                    ["circle, arc, or hidden-feature geometry is outside R001"],
                    confirmation,
                    unconsumed=unsupported,
                )
            if view.centerlines:
                return None, _empty_result(
                    "NOT_VALIDATED", ["unconsumed centerline geometry remains"],
                    confirmation, unconsumed=len(view.centerlines))
            for line in view.visible_segments:
                primitive_id = line.primitive_id
                if not isinstance(primitive_id, str) or not primitive_id:
                    return None, _empty_result(
                        "FAIL", ["every profile primitive requires a stable ID"], confirmation)
                if primitive_id in all_primitive_ids:
                    return None, _empty_result(
                        "FAIL", ["every primitive reference must resolve exactly once"],
                        confirmation)
                all_primitive_ids.add(primitive_id)
                visible_by_id[primitive_id] = (view_name, line)

        if not isinstance(evidence, Sequence) or isinstance(evidence, (str, bytes)):
            return None, _empty_result("FAIL", ["canonical FeatureEvidence sequence required"], confirmation)
        evidence_ids: set[str] = set()
        reference_counts: Counter[str] = Counter()
        unconsumed = 0
        for row in evidence:
            if not isinstance(row, FeatureEvidence):
                return None, _empty_result(
                    "FAIL", ["canonical FeatureEvidence is required"], confirmation)
            if row.evidence_id in evidence_ids:
                return None, _empty_result(
                    "FAIL", ["evidence IDs must be unique"], confirmation, unconsumed=1)
            evidence_ids.add(row.evidence_id)
            if row.geometry_type in {"CIRCLE", "ARC", "HIDDEN_LINE", "HIDDEN_LINE_PAIR"}:
                return None, _empty_result(
                    "UNSUPPORTED_FEATURE", ["feature contamination is outside R001"],
                    confirmation, unconsumed=1)
            if (row.geometry_type != "LINE" or row.feature_id is not None
                    or row.geometry_reference not in visible_by_id
                    or visible_by_id[row.geometry_reference][0] != row.view):
                unconsumed += 1
                continue
            reference_counts[row.geometry_reference] += 1

        duplicate_references = sorted(
            primitive_id for primitive_id, count in reference_counts.items() if count != 1)
        if duplicate_references:
            return None, _empty_result(
                "FAIL", ["every evidence reference must resolve exactly once"],
                confirmation, unconsumed=unconsumed + len(duplicate_references))
        missing_evidence = sorted(set(visible_by_id) - set(reference_counts))
        if missing_evidence or unconsumed:
            return None, _empty_result(
                "NOT_VALIDATED", ["profile evidence is missing or remains unconsumed"],
                confirmation, unconsumed=unconsumed + len(missing_evidence))

        try:
            front_width, front_height = _rectangle_dimensions(projection.front)
            top_width, top_depth = _rectangle_dimensions(projection.top)
            left_depth, left_height = _rectangle_dimensions(projection.left)
        except LookupError as exc:
            return None, _empty_result("NOT_VALIDATED", [str(exc)], confirmation)
        except ArithmeticError as exc:
            return None, _empty_result("AMBIGUOUS", [str(exc)], confirmation)
        except (TypeError, ValueError) as exc:
            return None, _empty_result("FAIL", [str(exc)], confirmation)
        if not (_close(front_width, top_width)
                and _close(front_height, left_height)
                and _close(top_depth, left_depth)):
            return None, _empty_result(
                "AMBIGUOUS", ["orthographic outer profiles do not correspond uniquely"],
                confirmation)

        parameters = {
            "width_mm": float(front_width),
            "height_mm": float(front_height),
            "depth_mm": float(top_depth),
        }
        ordered_evidence = sorted(evidence_ids)
        feature_id = _stable_feature_id(parameters, ordered_evidence)
        base = BaseBlock(
            parameters["width_mm"], parameters["height_mm"], parameters["depth_mm"],
            feature_id=feature_id, source_evidence_ids=ordered_evidence, dependencies=[])
        graph = FeatureGraph(base_block=base, status="NOT_VALIDATED")
        node = base.to_feature_node()
        result = _empty_result("INFERRED", [], confirmation)
        result.update(
            feature_graph=graph.to_dict(),
            feature_nodes=[node.to_dict()],
            feature_provenance=[{
                "feature_id": feature_id,
                "rule": "ONE_ORTHOGONAL_OUTER_PROFILE_TO_BASE_BLOCK",
                "source_evidence_ids": ordered_evidence,
                "source": "REVIEWED_VISUAL_EVIDENCE",
                "confirmation_decision": confirmation["decision"],
            }],
        )
        result["checks"].update(
            feature_count=1,
            complete_orthographic_view_count=3,
            outer_profile_count=1,
        )
        return graph, result


__all__ = [
    "BaseBlockReasoner",
    "CAPABILITY_SCOPE",
    "GENERATION_TRUTH_READS",
    "validate_confirmation_artifact",
]
