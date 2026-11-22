"""Canonical FeatureGraph-to-ModelingPlan strategy boundary."""

from __future__ import annotations

from typing import Protocol

from experiments.three_view_reconstruction.schemas.feature_graph import FeatureGraph
from experiments.three_view_reconstruction.schemas.modeling_plan import ModelingPlan


class ModelingStrategy(Protocol):
    """Build a canonical plan without invoking a CAD execution backend."""

    def build_plan(self, graph: FeatureGraph) -> ModelingPlan: ...


__all__ = ["ModelingStrategy"]
