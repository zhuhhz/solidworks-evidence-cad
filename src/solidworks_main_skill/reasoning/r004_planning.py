"""Backend-neutral planning for an already selected R004 composition history."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import math
import re

from experiments.three_view_reconstruction.schemas.feature_graph import MODEL_COORDINATE_SYSTEM
from experiments.three_view_reconstruction.schemas.modeling_plan import ModelingOperation, ModelingPlan

from .r004_composition_reasoner import (
    INPUT_DOMAIN, MAX_FEATURES, MAX_LEGAL_ORDERINGS, SUPPORTED_FEATURE_TYPES,
)
from .r004_contract import CompositionResult, DatumRequirement, NormalizedObservation


_OPERATIONS = {
    "BASE_BLOCK": "base_extrude",
    "BOSS": "boss_extrude",
    "THROUGH_HOLE": "cut_extrude_through_circle",
    "STRAIGHT_SLOT": "cut_extrude_through_slot",
}
_MATERIAL = {
    "BASE_BLOCK": "ROOT_MATERIAL",
    "BOSS": "ADDS_MATERIAL",
    "THROUGH_HOLE": "REMOVES_MATERIAL",
    "STRAIGHT_SLOT": "REMOVES_MATERIAL",
}
_PARAMETERS = {
    "BASE_BLOCK": {"width_mm", "height_mm", "depth_mm"},
    "BOSS": {"width_mm", "height_mm", "depth_mm", "center_x_mm", "center_y_mm",
             "base_offset_mm", "centered"},
    "THROUGH_HOLE": {"diameter_mm", "center_x_mm", "center_y_mm", "axis", "through"},
    "STRAIGHT_SLOT": {"overall_length_mm", "width_mm", "radius_mm", "center_x_mm",
                      "center_y_mm", "major_axis", "axis", "through"},
}
_NATIVE_REFERENCE = re.compile(r"(?:front|top|right)\s*plane|plane\d+|face<?\d+>?|selectbyid2", re.I)


@dataclass(frozen=True)
class PlannedDatum:
    feature_id: str
    token: str
    kind: str
    host_feature: str | None
    support_region: str | None
    orientation: str


@dataclass(frozen=True)
class OperationPrecondition:
    kind: str
    reference: str
    expected: str


@dataclass(frozen=True)
class OperationRequirements:
    operation_id: str
    datum: PlannedDatum
    evidence_ids: tuple[str, ...]
    preconditions: tuple[OperationPrecondition, ...]


@dataclass(frozen=True)
class PlanningResult:
    status: str
    error_code: str | None = None
    failure_reason: str | None = None
    modeling_plan: ModelingPlan | None = None
    operation_requirements: tuple[OperationRequirements, ...] = ()
    selected_history_id: str | None = None
    history_selection_source: str | None = None
    legal_history_count: int = 0
    legal_ordering_count: int = 0
    generation_truth_reads: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


class _PlanningError(ValueError):
    def __init__(self, code: str, reason: str):
        super().__init__(reason)
        self.code = code


def _require(condition: bool, code: str, reason: str) -> None:
    if not condition:
        raise _PlanningError(code, reason)


def _positive(parameters: dict, key: str) -> None:
    value = parameters.get(key)
    _require(type(value) in (int, float) and math.isfinite(value) and value > 0,
             "OUT_OF_DOMAIN_PLAN", f"{key} must be finite and positive")


def _validate_geometry(feature_type: str, parameters: dict) -> None:
    _require(isinstance(parameters, dict), "OUT_OF_DOMAIN_PLAN", "feature parameters are required")
    _require(set(parameters) == _PARAMETERS[feature_type], "OUT_OF_DOMAIN_PLAN",
             "feature geometry has missing or unsupported parameters")
    for key, value in parameters.items():
        if key.endswith("_mm"):
            _require(type(value) in (int, float) and math.isfinite(value),
                     "OUT_OF_DOMAIN_PLAN", "geometry parameters must be finite numbers")
    if feature_type in {"BASE_BLOCK", "BOSS"}:
        for key in ("width_mm", "height_mm", "depth_mm"):
            _positive(parameters, key)
    elif feature_type == "THROUGH_HOLE":
        _positive(parameters, "diameter_mm")
    else:
        _positive(parameters, "overall_length_mm")
        _positive(parameters, "width_mm")
        _require(parameters["overall_length_mm"] > parameters["width_mm"],
                 "OUT_OF_DOMAIN_PLAN", "slot length must exceed width")
        _require(parameters["major_axis"] in {"X", "Y"}
                 and math.isclose(parameters["radius_mm"] * 2, parameters["width_mm"], abs_tol=1e-6),
                 "OUT_OF_DOMAIN_PLAN", "slot axis or radius contradicts its profile")
    if feature_type == "BOSS":
        _require(type(parameters["centered"]) is bool, "OUT_OF_DOMAIN_PLAN",
                 "boss centering state must be explicit")
    if feature_type in {"THROUGH_HOLE", "STRAIGHT_SLOT"}:
        _require(parameters.get("through") is True and parameters.get("axis") == "Z",
                 "OUT_OF_DOMAIN_PLAN", "only through Z cuts are supported")


def _ordered(nodes: dict, dependencies: dict[str, set[str]], order: tuple[str, ...]) -> bool:
    if len(order) != len(nodes) or set(order) != set(nodes):
        return False
    completed: set[str] = set()
    for identity in order:
        if not dependencies[identity] <= completed:
            return False
        completed.add(identity)
    return True


def _validate_graph(result: CompositionResult) -> tuple[dict, dict[str, set[str]], str]:
    graph = result.feature_graph_candidate
    _require(graph is not None and graph.status == "NOT_VALIDATED", "OUT_OF_DOMAIN_PLAN",
             "an unvalidated R004 feature graph candidate is required")
    nodes = {node.feature_id: node for node in graph.nodes}
    _require(0 < len(graph.nodes) <= MAX_FEATURES and len(nodes) == len(graph.nodes),
             "OUT_OF_DOMAIN_PLAN", "bounded unique feature nodes are required")
    _require(all(node.feature_id and node.feature_type in SUPPORTED_FEATURE_TYPES
                 and node.coordinate_system == MODEL_COORDINATE_SYSTEM for node in graph.nodes),
             "OUT_OF_DOMAIN_PLAN", "feature graph is outside the bounded model domain")
    dependencies = {node.feature_id: set(node.dependencies) for node in graph.nodes}
    for node in graph.nodes:
        _require(len(dependencies[node.feature_id]) == len(node.dependencies)
                 and node.feature_id not in dependencies[node.feature_id],
                 "INVALID_DEPENDENCY_REFERENCE", "invalid or repeated feature dependency")
        _require(dependencies[node.feature_id] <= set(nodes), "INVALID_DEPENDENCY_REFERENCE",
                 "dependency refers to an absent feature")
        _validate_geometry(node.feature_type, node.parameters)
        _require(bool(node.source_evidence_ids) and all(node.source_evidence_ids),
                 "OUT_OF_DOMAIN_PLAN", "every feature needs bound evidence")
    edges = {(parent, child) for child, parents in dependencies.items() for parent in parents}
    _require(len(edges) == len(graph.dag_edges) and edges == set(graph.dag_edges)
             and edges == set(result.dag_edges), "INVALID_DEPENDENCY_REFERENCE",
             "DAG edges and feature dependencies disagree")
    # A cycle is reported before root/reachability failures so it cannot be mistaken for a missing base.
    remaining = set(nodes)
    while remaining:
        ready = {identity for identity in remaining if not dependencies[identity] & remaining}
        _require(bool(ready), "CYCLIC_DEPENDENCY", "feature dependency graph is cyclic")
        remaining -= ready
    roots = [identity for identity, node in nodes.items() if node.feature_type == "BASE_BLOCK"]
    _require(len(roots) == 1 and not dependencies[roots[0]]
             and all(len(dependencies[identity]) == 1 for identity in nodes if identity != roots[0]),
             "OUT_OF_DOMAIN_PLAN", "one root and one host per non-base feature are required")
    reachable = {roots[0]}
    while True:
        next_reachable = reachable | {identity for identity, parents in dependencies.items()
                                      if parents <= reachable}
        if next_reachable == reachable:
            break
        reachable = next_reachable
    _require(reachable == set(nodes), "OUT_OF_DOMAIN_PLAN", "some features are unreachable")
    _require(tuple(graph.canonical_order) == tuple(result.canonical_order), "OUT_OF_DOMAIN_PLAN",
             "candidate and reasoning canonical orders disagree")
    orders = tuple(tuple(order) for order in result.legal_orderings)
    _require(bool(orders) and len(orders) <= MAX_LEGAL_ORDERINGS
             and len(orders) == len(set(orders))
             and all(_ordered(nodes, dependencies, order) for order in orders)
             and tuple(result.canonical_order) in orders,
             "OUT_OF_DOMAIN_PLAN", "legal ordering collection is incomplete or invalid")
    return nodes, dependencies, roots[0]


def _validate_history(result: CompositionResult, nodes: dict, dependencies: dict) -> str:
    histories = {row.history_id: row for row in result.history_candidates}
    _require(bool(histories) and len(histories) == len(result.history_candidates)
             and result.selected_history_id in histories,
             "UNRESOLVED_HISTORY", "a machine-generated legal history must be selected")
    _require(result.history_status == ("MULTIPLE_LEGAL_HISTORIES" if len(histories) > 1
                                       else "UNIQUE_LEGAL_HISTORY"),
             "UNRESOLVED_HISTORY", "history status and candidate count disagree")
    hypotheses = {row.feature_id: row for row in result.feature_hypotheses}
    _require(len(hypotheses) == len(nodes) and set(hypotheses) == set(nodes),
             "OUT_OF_DOMAIN_PLAN", "feature hypotheses do not match graph nodes")
    regions = {row.region_id: row for row in hypotheses.values()}
    _require(len(regions) == len(nodes), "OUT_OF_DOMAIN_PLAN", "region identities are not unique")
    for feature_id, hypothesis in hypotheses.items():
        _require(hypothesis.feature_type == nodes[feature_id].feature_type
                 and hypothesis.material_relation == _MATERIAL[hypothesis.feature_type]
                 and dict(hypothesis.geometry_summary) == nodes[feature_id].parameters
                 and set(hypothesis.evidence_ids) == set(nodes[feature_id].source_evidence_ids),
                 "CONFLICTING_PRECONDITIONS", "hypothesis and graph material or geometry disagree")
    base_regions = {row.region_id for row in hypotheses.values() if row.feature_type == "BASE_BLOCK"}
    for history in histories.values():
        assignments = dict(history.host_assignments)
        _require(len(assignments) == len(history.host_assignments), "UNRESOLVED_HISTORY",
                 "history repeats a child assignment")
        _require(set(assignments) == set(regions) - base_regions
                 and set(assignments.values()) <= set(regions), "MISSING_HOST",
                 "history contains an absent child or host")
        _require(all(regions[host].material_relation in {"ROOT_MATERIAL", "ADDS_MATERIAL"}
                     for host in assignments.values()), "UNRESOLVED_HISTORY",
                 "history assigns a host without material")
        pairs = tuple(sorted(assignments.items()))
        encoded = json.dumps(pairs, sort_keys=True, separators=(",", ":"), allow_nan=False)
        _require(history.history_id == "history." + sha256(encoded.encode()).hexdigest()[:24],
                 "UNRESOLVED_HISTORY", "history identity is not machine generated")
        parent_by_child = {child: host for child, host in pairs}
        visited = set(base_regions)
        while True:
            next_visited = visited | {child for child, host in parent_by_child.items() if host in visited}
            if next_visited == visited:
                break
            visited = next_visited
        _require(visited == set(regions), "UNRESOLVED_HISTORY",
                 "history is not a connected acyclic dependency tree")
    selected = histories[result.selected_history_id]
    history_edges = {(regions[host].feature_id, regions[child].feature_id)
                     for child, host in selected.host_assignments}
    graph_edges = {(parent, child) for child, parents in dependencies.items() for parent in parents}
    _require(history_edges == graph_edges, "MISSING_HOST",
             "selected history and dependency DAG disagree")
    return "HUMAN_SELECTED_MACHINE_CANDIDATE" if len(histories) > 1 else "UNIQUE_MACHINE_CANDIDATE"


def _validate_datums(result: CompositionResult, nodes: dict, dependencies: dict,
                     base_id: str) -> dict[str, DatumRequirement]:
    datums: dict[str, DatumRequirement] = {}
    for row in result.datum_requirements:
        _require(row.feature_id in nodes and row.feature_id != base_id,
                 "OUT_OF_DOMAIN_PLAN", "datum targets an invalid feature")
        if row.feature_id in datums:
            old = datums[row.feature_id]
            code = ("CONFLICTING_PRECONDITIONS" if old.host_feature == row.host_feature
                    and old.support_region == row.support_region else "AMBIGUOUS_EXECUTION_BINDING")
            raise _PlanningError(code, "more than one datum requirement targets a feature")
        _require(row.required_datum_kind == "PLANAR_SUPPORT", "UNSUPPORTED_DATUM_KIND",
                 "datum kind is not supported by the bounded plan")
        _require(bool(row.support_region), "MISSING_DATUM", "support region is absent")
        _require(isinstance(row.support_region, str) and not _NATIVE_REFERENCE.search(row.support_region),
                 "AMBIGUOUS_EXECUTION_BINDING", "support must be an abstract region reference")
        _require(row.host_feature in nodes, "MISSING_HOST", "datum host feature is absent")
        _require(row.orientation in {"Z+", "Z-"}, "AMBIGUOUS_EXECUTION_BINDING",
                 "datum orientation is not resolvable in the bounded plan")
        _require(row.host_feature in dependencies[row.feature_id], "DATUM_NOT_AVAILABLE_YET",
                 "datum host is not a completed dependency of its feature")
        _require(nodes[row.host_feature].feature_type in {"BASE_BLOCK", "BOSS"},
                 "CONFLICTING_PRECONDITIONS", "datum host does not provide material support")
        datums[row.feature_id] = row
    _require(set(datums) == set(nodes) - {base_id}, "MISSING_DATUM",
             "every non-base feature needs one abstract datum")
    return datums


def _validate_observations(result: CompositionResult, nodes: dict) -> dict[str, NormalizedObservation]:
    observations = {row.region_id: row for row in result.normalized_observations}
    regions = {row.region_id for row in result.feature_hypotheses}
    _require(set(observations) == regions and len(observations) == len(result.normalized_observations),
             "OUT_OF_DOMAIN_PLAN", "normalized observation coverage is incomplete")
    by_feature = {}
    for hypothesis in result.feature_hypotheses:
        row = observations[hypothesis.region_id]
        bounds = row.relative_bounds_mm
        _require(len(bounds) == 6 and all(type(value) in (int, float) and math.isfinite(value)
                                         for value in bounds)
                 and all(bounds[index] < bounds[index + 1] for index in (0, 2, 4)),
                 "OUT_OF_DOMAIN_PLAN", "normalized region bounds are invalid")
        subtractive = hypothesis.material_relation == "REMOVES_MATERIAL"
        shape = {"BASE_BLOCK": "RECTANGULAR_PRISM", "BOSS": "RECTANGULAR_PRISM",
                 "THROUGH_HOLE": "CIRCULAR_PRISM", "STRAIGHT_SLOT": "STRAIGHT_SLOT_PRISM"}
        _require(row.material_state == ("ABSENT" if subtractive else "PRESENT")
                 and row.shape_kind == shape[hypothesis.feature_type] and row.axis == "Z"
                 and (not subtractive or row.full_span is True), "CONFLICTING_PRECONDITIONS",
                 "normalized observation contradicts the material operation")
        parameters = nodes[hypothesis.feature_id].parameters
        width, height, depth = (bounds[index + 1] - bounds[index] for index in (0, 2, 4))
        dimensions = ({"diameter_mm": width} if hypothesis.feature_type == "THROUGH_HOLE"
                      else {"overall_length_mm": max(width, height), "width_mm": min(width, height)}
                      if hypothesis.feature_type == "STRAIGHT_SLOT"
                      else {"width_mm": width, "height_mm": height, "depth_mm": depth})
        _require(all(math.isclose(parameters[key], value, abs_tol=1e-6)
                     for key, value in dimensions.items()), "CONFLICTING_PRECONDITIONS",
                 "operation dimensions contradict the reviewed region")
        by_feature[hypothesis.feature_id] = row
    return by_feature


def plan_composition(result: CompositionResult) -> PlanningResult:
    """Translate a completed R004-A result; never infer an authoring history or execute CAD."""
    try:
        _require(isinstance(result, CompositionResult), "OUT_OF_DOMAIN_PLAN",
                 "R004-A CompositionResult is required")
        _require(result.status == "INFERRED" and result.error_code is None
                 and result.generation_truth_reads == 0, "UNRESOLVED_HISTORY",
                 "completed R004-A reasoning with zero evaluator reads is required")
        nodes, dependencies, base_id = _validate_graph(result)
        selection_source = _validate_history(result, nodes, dependencies)
        datums = _validate_datums(result, nodes, dependencies, base_id)
        observations = _validate_observations(result, nodes)
        operations: list[ModelingOperation] = []
        requirements: list[OperationRequirements] = []
        completed: set[str] = set()
        for feature_id in result.canonical_order:
            node = nodes[feature_id]
            parents = sorted(dependencies[feature_id])
            _require(set(parents) <= completed, "DATUM_NOT_AVAILABLE_YET",
                     "canonical order schedules a feature before its host")
            operation_id = "op_" + feature_id
            if feature_id == base_id:
                datum = PlannedDatum(feature_id, "datum:model_xy", "GLOBAL_REFERENCE",
                                     None, None, "Z+")
            else:
                row = datums[feature_id]
                datum = PlannedDatum(feature_id, "datum:feature:" + feature_id,
                                     row.required_datum_kind, row.host_feature,
                                     row.support_region, row.orientation)
            profile = {key: node.parameters[key] for key in sorted(node.parameters)
                       if key not in {"depth_mm", "through"}}
            bounds = observations[feature_id].relative_bounds_mm
            profile["region_bounds_mm"] = tuple(bounds)
            profile["coordinate_frame"] = "BASE_MIN_XYZ_MM"
            subtractive = node.feature_type in {"THROUGH_HOLE", "STRAIGHT_SLOT"}
            # A through cut spans the reviewed region, not every material interval in the body.
            if subtractive:
                profile["extent_scope"] = "REVIEWED_REGION"
            operation = ModelingOperation(
                type=_OPERATIONS[node.feature_type], sketch_plane=datum.token, profile=profile,
                depth_mm=bounds[5] - bounds[4] if subtractive else node.parameters["depth_mm"],
                direction="THROUGH_REGION_Z" if subtractive
                else "POSITIVE_Z",
                operation_id=operation_id, source_feature_id=feature_id,
                depends_on_operation_ids=["op_" + parent for parent in parents],
            )
            checks = [OperationPrecondition("DOMAIN_VALID", INPUT_DOMAIN, "CONFIRMED"),
                      OperationPrecondition("TOPOLOGY_VALID", "PRISMATIC", "CONFIRMED"),
                      OperationPrecondition("DATUM_AVAILABLE", datum.token, "AVAILABLE")]
            for parent in parents:
                checks.append(OperationPrecondition("DEPENDENCY_COMPLETED", "op_" + parent,
                                                    "COMPLETED"))
            if feature_id != base_id:
                checks.extend((
                    OperationPrecondition("HOST_AVAILABLE", datum.host_feature, "AVAILABLE"),
                    OperationPrecondition("SUPPORT_REGION_AVAILABLE", datum.support_region, "AVAILABLE"),
                    OperationPrecondition("MATERIAL_EXISTS", datum.host_feature, "PRESENT"),
                ))
            requirements.append(OperationRequirements(operation_id, datum,
                                                      tuple(sorted(set(node.source_evidence_ids))),
                                                      tuple(sorted(checks, key=lambda row:
                                                                   (row.kind, row.reference, row.expected)))))
            operations.append(operation)
            completed.add(feature_id)
        return PlanningResult(
            status="PLANNED", modeling_plan=ModelingPlan(operations),
            operation_requirements=tuple(requirements),
            selected_history_id=result.selected_history_id,
            history_selection_source=selection_source,
            legal_history_count=len(result.history_candidates),
            legal_ordering_count=len(result.legal_orderings),
        )
    except _PlanningError as exc:
        return PlanningResult(status="REJECTED", error_code=exc.code, failure_reason=str(exc))
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        return PlanningResult(status="REJECTED", error_code="OUT_OF_DOMAIN_PLAN",
                              failure_reason=f"invalid planning input: {exc}")


__all__ = ["OperationPrecondition", "OperationRequirements", "PlannedDatum", "PlanningResult",
           "plan_composition"]
