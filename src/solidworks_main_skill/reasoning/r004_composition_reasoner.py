"""Bounded N-feature composition reasoning over reviewed structured evidence."""

from __future__ import annotations

from hashlib import sha256
from itertools import product
import json
import math
import re
from typing import Any, Iterable, Mapping

from experiments.three_view_reconstruction.schemas.feature_graph import FeatureNode

from .r004_contract import (
    CompositionResult,
    DatumRequirement,
    FeatureGraphCandidate,
    FeatureHypothesis,
    HistoryCandidate,
    NormalizedObservation,
    RegionObservation,
    ReviewedCompositionInput,
    SupportRelation,
)


CAPABILITY_SCOPE = "BOUNDED_N_FEATURE_COMPOSITION_V1"
INPUT_DOMAIN = "CONNECTED_SINGLE_SOLID_PART"
COORDINATE_CONVENTION = "MODEL_XYZ_MM_RIGHT_HANDED"
GENERATION_TRUTH_READS = 0
SUPPORTED_FEATURE_TYPES = ("BASE_BLOCK", "BOSS", "STRAIGHT_SLOT", "THROUGH_HOLE")
MAX_FEATURES = 8
MAX_LEGAL_ORDERINGS = 10_000
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_RELATION_TYPES = {"ADJACENT", "CONTAINED_BY", "INTERSECTS", "SUPPORTED_BY"}
_SHAPE_TO_PRIMITIVE = {
    "RECTANGULAR_PRISM": "RECTANGULAR_PROFILE",
    "CIRCULAR_PRISM": "CIRCLE",
    "STRAIGHT_SLOT_PRISM": "STRAIGHT_SLOT_PROFILE",
}


class _ReasoningError(ValueError):
    def __init__(self, code: str, reason: str, *, status: str = "NOT_VALIDATED"):
        super().__init__(reason)
        self.code = code
        self.status = status


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _identity(prefix: str, value: Any) -> str:
    return f"{prefix}.{sha256(_canonical(value).encode()).hexdigest()[:24]}"


def _clean(value: float) -> float:
    rounded = round(float(value), 9)
    return 0.0 if rounded == -0.0 else rounded


def _bounds(region: RegionObservation) -> tuple[float, float, float, float, float, float]:
    values = region.bounds_mm
    if (len(values) != 6 or any(type(value) not in (int, float) or not math.isfinite(value)
                                for value in values)):
        raise _ReasoningError("INSUFFICIENT_EVIDENCE", f"invalid bounds for {region.region_id}")
    xmin, xmax, ymin, ymax, zmin, zmax = map(float, values)
    if xmin >= xmax or ymin >= ymax or zmin >= zmax:
        raise _ReasoningError("CONFLICTING_EVIDENCE", f"non-positive bounds for {region.region_id}")
    return xmin, xmax, ymin, ymax, zmin, zmax


def _relative_bounds(region: RegionObservation, base: RegionObservation) -> tuple[float, ...]:
    xmin, xmax, ymin, ymax, zmin, zmax = _bounds(region)
    bxmin, _, bymin, _, bzmin, _ = _bounds(base)
    return tuple(_clean(value) for value in (
        xmin - bxmin, xmax - bxmin, ymin - bymin, ymax - bymin, zmin - bzmin, zmax - bzmin,
    ))


def _has_cycle(nodes: Iterable[str], assignments: Mapping[str, str]) -> bool:
    state = {node: 0 for node in nodes}

    def visit(node: str) -> bool:
        if state[node] == 1:
            return True
        if state[node] == 2:
            return False
        state[node] = 1
        host = assignments.get(node)
        if host is not None and visit(host):
            return True
        state[node] = 2
        return False

    return any(visit(node) for node in sorted(state) if state[node] == 0)


def _reachable(root: str, nodes: Iterable[str], assignments: Mapping[str, str]) -> bool:
    children: dict[str, set[str]] = {node: set() for node in nodes}
    for child, host in assignments.items():
        children[host].add(child)
    seen, pending = set(), [root]
    while pending:
        node = pending.pop()
        if node in seen:
            continue
        seen.add(node)
        pending.extend(sorted(children[node], reverse=True))
    return seen == set(nodes)


def _history_candidates(
    region_ids: set[str], base_region_id: str, supports: tuple[SupportRelation, ...]
) -> tuple[HistoryCandidate, ...]:
    choices = [tuple(sorted(set(row.candidate_host_region_ids))) for row in supports]
    legal: list[HistoryCandidate] = []
    cyclic = False
    for selected in product(*choices):
        assignments = {row.child_region_id: host for row, host in zip(supports, selected)}
        if _has_cycle(region_ids, assignments):
            cyclic = True
            continue
        if not _reachable(base_region_id, region_ids, assignments):
            continue
        pairs = tuple(sorted(assignments.items()))
        legal.append(HistoryCandidate(_identity("history", pairs), pairs))
    unique = {row.history_id: row for row in legal}
    if not unique:
        code = "CYCLIC_DEPENDENCY" if cyclic else "OUT_OF_DOMAIN_TOPOLOGY"
        raise _ReasoningError(code, "no connected acyclic dependency history survives")
    return tuple(unique[key] for key in sorted(unique))


def _feature_type(region: RegionObservation, base_region_id: str) -> str:
    if region.region_id == base_region_id:
        return "BASE_BLOCK"
    if region.material_state == "PRESENT" and region.shape_kind == "RECTANGULAR_PRISM":
        return "BOSS"
    if region.material_state == "ABSENT" and region.shape_kind == "CIRCULAR_PRISM":
        if not region.full_span:
            raise _ReasoningError("INSUFFICIENT_EVIDENCE", "circular cut lacks full-span evidence")
        return "THROUGH_HOLE"
    if region.material_state == "ABSENT" and region.shape_kind == "STRAIGHT_SLOT_PRISM":
        if not region.full_span:
            raise _ReasoningError("INSUFFICIENT_EVIDENCE", "slot cut lacks full-span evidence")
        return "STRAIGHT_SLOT"
    raise _ReasoningError("UNSUPPORTED_FEATURE_FAMILY", f"unsupported material/shape pair for {region.region_id}",
                          status="UNSUPPORTED")


def _geometry(feature_type: str, relative: tuple[float, ...], base: RegionObservation,
              region: RegionObservation) -> dict[str, Any]:
    xmin, xmax, ymin, ymax, zmin, zmax = relative
    width, height, depth = xmax - xmin, ymax - ymin, zmax - zmin
    if feature_type == "BASE_BLOCK":
        return {"width_mm": _clean(width), "height_mm": _clean(height), "depth_mm": _clean(depth)}
    if feature_type == "BOSS":
        base_bounds = _relative_bounds(base, base)
        bx = (base_bounds[0] + base_bounds[1]) / 2
        by = (base_bounds[2] + base_bounds[3]) / 2
        return {
            "width_mm": _clean(width), "height_mm": _clean(height), "depth_mm": _clean(depth),
            "center_x_mm": _clean((xmin + xmax) / 2), "center_y_mm": _clean((ymin + ymax) / 2),
            "base_offset_mm": _clean(zmin),
            "centered": _clean((xmin + xmax) / 2) == _clean(bx)
            and _clean((ymin + ymax) / 2) == _clean(by),
        }
    if region.axis != "Z":
        raise _ReasoningError("UNSUPPORTED_FEATURE_FAMILY", "V1 hole/slot observations require axis Z",
                              status="UNSUPPORTED")
    if feature_type == "THROUGH_HOLE":
        if abs(width - height) > 1e-6:
            raise _ReasoningError("CONFLICTING_EVIDENCE", "circular bounds do not resolve one diameter")
        return {
            "diameter_mm": _clean((width + height) / 2),
            "center_x_mm": _clean((xmin + xmax) / 2), "center_y_mm": _clean((ymin + ymax) / 2),
            "axis": region.axis, "through": True,
        }
    major_axis = "X" if width >= height else "Y"
    overall, slot_width = (width, height) if major_axis == "X" else (height, width)
    if overall <= slot_width:
        raise _ReasoningError("CONFLICTING_EVIDENCE", "slot major extent must exceed its width")
    return {
        "overall_length_mm": _clean(overall), "width_mm": _clean(slot_width),
        "radius_mm": _clean(slot_width / 2), "center_x_mm": _clean((xmin + xmax) / 2),
        "center_y_mm": _clean((ymin + ymax) / 2), "major_axis": major_axis,
        "axis": region.axis, "through": True,
    }


def _all_topological_orders(
    node_ids: tuple[str, ...], parents: Mapping[str, set[str]], sort_keys: Mapping[str, tuple[Any, ...]]
) -> tuple[tuple[str, ...], ...]:
    results: list[tuple[str, ...]] = []

    def walk(order: tuple[str, ...], remaining: frozenset[str]) -> None:
        if len(results) > MAX_LEGAL_ORDERINGS:
            raise _ReasoningError("ORDERING_LIMIT_EXCEEDED", "bounded legal-ordering limit exceeded",
                                  status="UNSUPPORTED")
        if not remaining:
            results.append(order)
            return
        ready = sorted((node for node in remaining if parents[node].isdisjoint(remaining)),
                       key=lambda node: sort_keys[node])
        if not ready:
            raise _ReasoningError("CYCLIC_DEPENDENCY", "dependency graph has no topological ordering")
        for node in ready:
            walk((*order, node), remaining - {node})

    walk((), frozenset(node_ids))
    results.sort(key=lambda order: tuple(sort_keys[node] for node in order))
    return tuple(results)


def _validate_and_index(value: ReviewedCompositionInput) -> tuple[dict[str, RegionObservation],
                                                                    dict[str, Any], dict[str, Any]]:
    if value.domain != INPUT_DOMAIN:
        code = "DISCONNECTED_SOLIDS" if value.body_count > 1 else "OUT_OF_DOMAIN_TOPOLOGY"
        raise _ReasoningError(code, "input is outside the connected-single-solid domain",
                              status="UNSUPPORTED")
    if value.body_count != 1:
        code = "DISCONNECTED_SOLIDS" if value.body_count > 1 else "OUT_OF_DOMAIN_TOPOLOGY"
        raise _ReasoningError(code, "exactly one solid body is required", status="UNSUPPORTED")
    if value.topology_kind != "PRISMATIC" or value.coordinate_convention != COORDINATE_CONVENTION:
        raise _ReasoningError("OUT_OF_DOMAIN_TOPOLOGY", "prismatic axis-aligned model coordinates are required",
                              status="UNSUPPORTED")
    if value.confirmation_state != "CONFIRMED":
        raise _ReasoningError("INSUFFICIENT_EVIDENCE", "reviewed CONFIRMED evidence is required",
                              status="AMBIGUOUS")

    evidence = {row.evidence_id: row for row in value.evidence}
    if len(evidence) != len(value.evidence) or not evidence:
        raise _ReasoningError("INSUFFICIENT_EVIDENCE", "evidence IDs must be unique and non-empty")
    if any(not row.evidence_id or not _SHA256.fullmatch(row.content_sha256) for row in evidence.values()):
        raise _ReasoningError("INSUFFICIENT_EVIDENCE", "every evidence ID requires a SHA-256 content binding")
    regions = {row.region_id: row for row in value.regions}
    primitives = {row.primitive_id: row for row in value.primitives}
    if len(regions) != len(value.regions) or len(primitives) != len(value.primitives) or not regions:
        raise _ReasoningError("INSUFFICIENT_EVIDENCE", "region and primitive IDs must be unique and non-empty")
    if len(regions) > MAX_FEATURES:
        raise _ReasoningError("OUT_OF_DOMAIN_TOPOLOGY", "feature count exceeds the bounded V1 domain",
                              status="UNSUPPORTED")

    referenced: set[str] = set()

    def require_evidence(ids: Iterable[str], owner: str) -> None:
        values = tuple(ids)
        if not values or any(identity not in evidence for identity in values):
            raise _ReasoningError("INSUFFICIENT_EVIDENCE", f"unbound evidence reference on {owner}")
        referenced.update(values)

    for primitive in primitives.values():
        if primitive.region_id not in regions:
            raise _ReasoningError("CONFLICTING_EVIDENCE", "primitive references an unknown region")
        if not primitive.axis_aligned:
            raise _ReasoningError("NON_AXIS_ALIGNED_GEOMETRY", "all primitives must be axis aligned",
                                  status="UNSUPPORTED")
        if set(primitive.projection_views) != {"front", "left", "top"}:
            raise _ReasoningError("INSUFFICIENT_EVIDENCE", "three-view primitive support is incomplete")
        require_evidence(primitive.evidence_ids, primitive.primitive_id)
    for region in regions.values():
        _bounds(region)
        if region.material_state not in {"PRESENT", "ABSENT"}:
            raise _ReasoningError("CONFLICTING_EVIDENCE", "material state must resolve presence or absence")
        if region.shape_kind not in _SHAPE_TO_PRIMITIVE:
            raise _ReasoningError("UNSUPPORTED_FEATURE_FAMILY", f"unsupported shape {region.shape_kind}",
                                  status="UNSUPPORTED")
        require_evidence(region.evidence_ids, region.region_id)
        if not region.primitive_ids:
            raise _ReasoningError("INSUFFICIENT_EVIDENCE", "each region requires primitive support")
        for primitive_id in region.primitive_ids:
            primitive = primitives.get(primitive_id)
            if (primitive is None or primitive.region_id != region.region_id
                    or primitive.primitive_kind != _SHAPE_TO_PRIMITIVE[region.shape_kind]):
                raise _ReasoningError("CONFLICTING_EVIDENCE", "region/primitive support is inconsistent")
    relation_by_pair: dict[tuple[str, str], list[Any]] = {}
    relation_ids: set[str] = set()
    for relation in value.geometric_relations:
        if not relation.relation_id or relation.relation_id in relation_ids:
            raise _ReasoningError("CONFLICTING_EVIDENCE", "geometric relation IDs must be unique")
        relation_ids.add(relation.relation_id)
        if (relation.relation_type not in _RELATION_TYPES
                or relation.source_region_id not in regions or relation.target_region_id not in regions
                or relation.source_region_id == relation.target_region_id):
            raise _ReasoningError("CONFLICTING_EVIDENCE", "invalid geometric relation")
        require_evidence(relation.evidence_ids, relation.relation_id)
        relation_by_pair.setdefault((relation.source_region_id, relation.target_region_id), []).append(relation)

    support_by_child: dict[str, SupportRelation] = {}
    support_ids: set[str] = set()
    for support in value.support_relations:
        if not support.relation_id or support.relation_id in support_ids:
            raise _ReasoningError("CONFLICTING_EVIDENCE", "support relation IDs must be unique")
        support_ids.add(support.relation_id)
        if support.child_region_id in support_by_child:
            raise _ReasoningError("CONFLICTING_EVIDENCE", "a region has multiple support declarations")
        hosts = tuple(sorted(set(support.candidate_host_region_ids)))
        if (support.child_region_id not in regions or not hosts
                or any(host not in regions or host == support.child_region_id for host in hosts)):
            raise _ReasoningError("CONFLICTING_EVIDENCE", "support relation has invalid references")
        if len(hosts) > 1 and not support.history_equivalent:
            raise _ReasoningError("AMBIGUOUS_HOST", "multiple hosts lack shape-equivalent history evidence",
                                  status="AMBIGUOUS")
        require_evidence(support.evidence_ids, support.relation_id)
        for host in hosts:
            if regions[host].material_state != "PRESENT":
                raise _ReasoningError("CONFLICTING_EVIDENCE", "feature host must denote existing material")
            if not relation_by_pair.get((support.child_region_id, host)):
                raise _ReasoningError("INSUFFICIENT_EVIDENCE", "host lacks geometric support evidence")
        support_by_child[support.child_region_id] = support
    if referenced != set(evidence):
        raise _ReasoningError("INSUFFICIENT_EVIDENCE", "reviewed evidence remains unconsumed")
    return regions, primitives, support_by_child


def reason_composition(value: ReviewedCompositionInput) -> CompositionResult:
    """Infer a bounded N-feature DAG without claiming engineering validation."""
    try:
        regions, primitives, support_by_child = _validate_and_index(value)
        child_ids = set(support_by_child)
        base_candidates = sorted(
            region.region_id for region in regions.values()
            if region.material_state == "PRESENT" and region.shape_kind == "RECTANGULAR_PRISM"
            and region.region_id not in child_ids
        )
        if len(base_candidates) != 1:
            probe = {child: sorted(set(row.candidate_host_region_ids))[0]
                     for child, row in support_by_child.items()}
            if _has_cycle(set(regions), probe):
                raise _ReasoningError("CYCLIC_DEPENDENCY", "support evidence contains a dependency cycle")
            raise _ReasoningError("MULTIPLE_BASE_CANDIDATES", "exactly one material root/base is required",
                                  status="AMBIGUOUS")
        base_region_id = base_candidates[0]
        if set(regions) - {base_region_id} != child_ids:
            raise _ReasoningError("INSUFFICIENT_EVIDENCE", "every non-base region requires host support")
        supports = tuple(sorted(support_by_child.values(), key=lambda row: row.child_region_id))
        histories = _history_candidates(set(regions), base_region_id, supports)
        if len(histories) > 1:
            selected = next((row for row in histories if row.history_id == value.selected_history_id), None)
            if selected is None:
                return CompositionResult(
                    status="AMBIGUOUS", error_code="HISTORY_AMBIGUITY_REQUIRES_CONFIRMATION",
                    failure_reason="multiple shape-equivalent legal histories require an explicit human selection",
                    history_status="MULTIPLE_LEGAL_HISTORIES", history_candidates=histories,
                    generation_truth_reads=GENERATION_TRUTH_READS,
                )
        else:
            selected = histories[0]
            if value.selected_history_id not in {None, selected.history_id}:
                raise _ReasoningError("HISTORY_AMBIGUITY_REQUIRES_CONFIRMATION",
                                      "selected history is not machine generated", status="AMBIGUOUS")

        base = regions[base_region_id]
        normalized = tuple(sorted((
            NormalizedObservation(region.region_id, region.shape_kind, region.material_state,
                                  _relative_bounds(region, base), region.full_span, region.axis)
            for region in regions.values()
        ), key=lambda row: row.region_id))
        normalized_by_id = {row.region_id: row for row in normalized}
        support_evidence = {row.child_region_id: tuple(sorted(set(row.evidence_ids))) for row in supports}
        relation_evidence: dict[tuple[str, str], set[str]] = {}
        for row in value.geometric_relations:
            relation_evidence.setdefault((row.source_region_id, row.target_region_id), set()).update(row.evidence_ids)
        assignments = dict(selected.host_assignments)
        hypotheses: list[FeatureHypothesis] = []
        for region_id in sorted(regions):
            region = regions[region_id]
            feature_type = _feature_type(region, base_region_id)
            geometry = _geometry(feature_type, normalized_by_id[region_id].relative_bounds_mm, base, region)
            primitive_evidence = {
                evidence_id for primitive_id in region.primitive_ids
                for evidence_id in primitives[primitive_id].evidence_ids
            }
            host = assignments.get(region_id)
            geometric_evidence = relation_evidence.get((region_id, host), set()) if host else set()
            evidence_ids = tuple(sorted(set(region.evidence_ids) | primitive_evidence
                                        | set(support_evidence.get(region_id, ())) | geometric_evidence))
            feature_id = _identity("inferred-feature", [feature_type, geometry, evidence_ids])
            hypotheses.append(FeatureHypothesis(
                feature_id=feature_id, region_id=region_id, feature_type=feature_type,
                geometry_summary=tuple(sorted(geometry.items())), evidence_ids=evidence_ids,
                support_evidence_ids=support_evidence.get(region_id, ()),
                material_relation="ROOT_MATERIAL" if feature_type == "BASE_BLOCK"
                else ("ADDS_MATERIAL" if feature_type == "BOSS" else "REMOVES_MATERIAL"),
                candidate_host_region_ids=tuple(sorted(set(support_by_child[region_id].candidate_host_region_ids)))
                if region_id in support_by_child else (),
                provenance=("GEOMETRIC_RELATION", "PRIMITIVE_OBSERVATION", "REGION_OBSERVATION",
                            "SUPPORT_RELATION") if host else ("PRIMITIVE_OBSERVATION", "REGION_OBSERVATION"),
            ))
        hypotheses_tuple = tuple(sorted(hypotheses, key=lambda row: row.feature_id))
        by_region = {row.region_id: row for row in hypotheses_tuple}
        parents: dict[str, set[str]] = {row.feature_id: set() for row in hypotheses_tuple}
        dag_edges = []
        for child_region, host_region in selected.host_assignments:
            child, host = by_region[child_region].feature_id, by_region[host_region].feature_id
            parents[child].add(host)
            dag_edges.append((host, child))
        dag_edges_tuple = tuple(sorted(dag_edges))
        operation_rank = {"BASE_BLOCK": 0, "BOSS": 1, "STRAIGHT_SLOT": 2, "THROUGH_HOLE": 2}
        sort_keys = {
            row.feature_id: (operation_rank[row.feature_type], _canonical(row.geometry_summary),
                             row.evidence_ids, row.feature_id)
            for row in hypotheses_tuple
        }
        orders = _all_topological_orders(tuple(parents), parents, sort_keys)
        canonical_order = orders[0]
        node_by_id = {
            row.feature_id: FeatureNode(
                feature_id=row.feature_id, feature_type=row.feature_type,
                parameters=dict(row.geometry_summary), source_evidence_ids=list(row.evidence_ids),
                dependencies=sorted(parents[row.feature_id]),
            ) for row in hypotheses_tuple
        }
        nodes = tuple(node_by_id[identity] for identity in canonical_order)
        datums = tuple(sorted((
            DatumRequirement(
                feature_id=by_region[child].feature_id, required_datum_kind="PLANAR_SUPPORT",
                host_feature=by_region[host].feature_id,
                support_region=support_by_child[child].support_region_id,
                orientation=support_by_child[child].orientation,
            ) for child, host in selected.host_assignments
        ), key=lambda row: row.feature_id))
        candidate = FeatureGraphCandidate(nodes, dag_edges_tuple, canonical_order)
        return CompositionResult(
            status="INFERRED", error_code=None, failure_reason=None,
            normalized_observations=normalized, feature_hypotheses=hypotheses_tuple,
            history_status="MULTIPLE_LEGAL_HISTORIES" if len(histories) > 1 else "UNIQUE_LEGAL_HISTORY",
            history_candidates=histories, selected_history_id=selected.history_id,
            dag_edges=dag_edges_tuple, legal_orderings=orders, canonical_order=canonical_order,
            datum_requirements=datums, feature_graph_candidate=candidate,
            generation_truth_reads=GENERATION_TRUTH_READS,
        )
    except _ReasoningError as exc:
        return CompositionResult(
            status=exc.status, error_code=exc.code, failure_reason=str(exc),
            generation_truth_reads=GENERATION_TRUTH_READS,
        )
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        return CompositionResult(
            status="NOT_VALIDATED", error_code="INSUFFICIENT_EVIDENCE",
            failure_reason=f"invalid reviewed composition input: {exc}",
            generation_truth_reads=GENERATION_TRUTH_READS,
        )


__all__ = [
    "CAPABILITY_SCOPE",
    "COORDINATE_CONVENTION",
    "GENERATION_TRUTH_READS",
    "INPUT_DOMAIN",
    "SUPPORTED_FEATURE_TYPES",
    "reason_composition",
]
