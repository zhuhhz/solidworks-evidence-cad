"""Future hypothesis-generation boundary using canonical B008/B009 types."""

from __future__ import annotations

from typing import Protocol, Sequence

from experiments.three_view_reconstruction.schemas.feature_evidence import FeatureEvidence
from experiments.three_view_reconstruction.schemas.feature_graph import FeatureHypothesis
from experiments.three_view_reconstruction.schemas.projection_graph import ProjectionGraph


class FeatureHypothesisPolicy(Protocol):
    """Generate canonical hypotheses without prescribing an implementation."""

    def generate(
        self,
        graph: ProjectionGraph,
        evidence: Sequence[FeatureEvidence],
    ) -> list[FeatureHypothesis]: ...


__all__ = ["FeatureHypothesis", "FeatureHypothesisPolicy"]
