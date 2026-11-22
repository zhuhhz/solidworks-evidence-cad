"""Immutable reviewed-input and result contracts for bounded R004 reasoning."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from experiments.three_view_reconstruction.schemas.feature_graph import FeatureNode


@dataclass(frozen=True)
class EvidenceBinding:
    evidence_id: str
    content_sha256: str
    evidence_kind: str = "REVIEWED_OBSERVATION"


@dataclass(frozen=True)
class PrimitiveObservation:
    primitive_id: str
    primitive_kind: str
    region_id: str
    evidence_ids: tuple[str, ...]
    projection_views: tuple[str, ...] = ("front", "left", "top")
    axis_aligned: bool = True


@dataclass(frozen=True)
class RegionObservation:
    region_id: str
    shape_kind: str
    bounds_mm: tuple[float, float, float, float, float, float]
    material_state: str
    evidence_ids: tuple[str, ...]
    primitive_ids: tuple[str, ...]
    full_span: bool = False
    axis: str = "Z"


@dataclass(frozen=True)
class GeometricRelation:
    relation_id: str
    relation_type: str
    source_region_id: str
    target_region_id: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class SupportRelation:
    relation_id: str
    child_region_id: str
    candidate_host_region_ids: tuple[str, ...]
    support_region_id: str
    orientation: str
    evidence_ids: tuple[str, ...]
    history_equivalent: bool = False


@dataclass(frozen=True)
class ReviewedCompositionInput:
    domain: str
    coordinate_convention: str
    evidence: tuple[EvidenceBinding, ...]
    primitives: tuple[PrimitiveObservation, ...]
    regions: tuple[RegionObservation, ...]
    geometric_relations: tuple[GeometricRelation, ...]
    support_relations: tuple[SupportRelation, ...]
    confirmation_state: str
    body_count: int = 1
    topology_kind: str = "PRISMATIC"
    selected_history_id: str | None = None
    transport_metadata: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class NormalizedObservation:
    region_id: str
    shape_kind: str
    material_state: str
    relative_bounds_mm: tuple[float, float, float, float, float, float]
    full_span: bool
    axis: str


@dataclass(frozen=True)
class FeatureHypothesis:
    feature_id: str
    region_id: str
    feature_type: str
    geometry_summary: tuple[tuple[str, Any], ...]
    evidence_ids: tuple[str, ...]
    support_evidence_ids: tuple[str, ...]
    material_relation: str
    candidate_host_region_ids: tuple[str, ...]
    provenance: tuple[str, ...]


@dataclass(frozen=True)
class DatumRequirement:
    feature_id: str
    required_datum_kind: str
    host_feature: str
    support_region: str
    orientation: str


@dataclass(frozen=True)
class HistoryCandidate:
    history_id: str
    host_assignments: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class FeatureGraphCandidate:
    nodes: tuple[FeatureNode, ...]
    dag_edges: tuple[tuple[str, str], ...]
    canonical_order: tuple[str, ...]
    status: str = "NOT_VALIDATED"

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": [node.to_dict() for node in self.nodes],
            "dag_edges": [list(edge) for edge in self.dag_edges],
            "canonical_order": list(self.canonical_order),
            "status": self.status,
        }


@dataclass(frozen=True)
class CompositionResult:
    status: str
    error_code: str | None
    failure_reason: str | None
    normalized_observations: tuple[NormalizedObservation, ...] = ()
    feature_hypotheses: tuple[FeatureHypothesis, ...] = ()
    history_status: str = "NOT_EVALUATED"
    history_candidates: tuple[HistoryCandidate, ...] = ()
    selected_history_id: str | None = None
    dag_edges: tuple[tuple[str, str], ...] = ()
    legal_orderings: tuple[tuple[str, ...], ...] = ()
    canonical_order: tuple[str, ...] = ()
    datum_requirements: tuple[DatumRequirement, ...] = ()
    feature_graph_candidate: FeatureGraphCandidate | None = None
    generation_truth_reads: int = 0

    @property
    def legal_ordering_count(self) -> int:
        return len(self.legal_orderings)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["legal_ordering_count"] = self.legal_ordering_count
        if self.feature_graph_candidate is not None:
            value["feature_graph_candidate"] = self.feature_graph_candidate.to_dict()
        return value


__all__ = [
    "CompositionResult",
    "DatumRequirement",
    "EvidenceBinding",
    "FeatureGraphCandidate",
    "FeatureHypothesis",
    "GeometricRelation",
    "HistoryCandidate",
    "NormalizedObservation",
    "PrimitiveObservation",
    "RegionObservation",
    "ReviewedCompositionInput",
    "SupportRelation",
]
