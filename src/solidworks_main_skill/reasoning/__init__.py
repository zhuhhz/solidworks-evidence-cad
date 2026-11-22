"""Canonical contract adapters and isolated policies for CAD Reasoning V1."""

from experiments.three_view_reconstruction.schemas.feature_evidence import FeatureEvidence
from experiments.three_view_reconstruction.schemas.modeling_plan import (
    ModelingOperation,
    ModelingPlan,
)

from .base_block_planning import build_base_block_modeling_plan
from .base_block_reasoner import BaseBlockReasoner, CAPABILITY_SCOPE
from .cylindrical_reasoner import (
    CAPABILITY_SCOPE as R002_CAPABILITY_SCOPE,
    CONTRADICTION_NAMES as R002_CONTRADICTION_NAMES,
    CylindricalReasoner,
    reason_from_reviewed_artifacts,
)
from .r003_bounded_composition_reasoner import (
    INPUT_DOMAIN as R003_INPUT_DOMAIN,
    reason_r003_from_reviewed_artifacts,
)
from .r003_b010_integration import integrate_r003_with_b010
from .feature_graph import BaseBlock, FeatureGraph, FeatureNode
from .feature_hypothesis import FeatureHypothesis, FeatureHypothesisPolicy
from .modeling_strategy import ModelingStrategy
from .projection_graph import (
    Arc,
    Circle,
    HiddenLinePair,
    LineSegment,
    ProjectionGraph,
    ViewGeometry,
)

__all__ = [
    "Arc",
    "BaseBlock",
    "BaseBlockReasoner",
    "CAPABILITY_SCOPE",
    "Circle",
    "CylindricalReasoner",
    "FeatureEvidence",
    "FeatureGraph",
    "FeatureHypothesis",
    "FeatureHypothesisPolicy",
    "FeatureNode",
    "HiddenLinePair",
    "LineSegment",
    "ModelingOperation",
    "ModelingPlan",
    "ModelingStrategy",
    "ProjectionGraph",
    "R002_CAPABILITY_SCOPE",
    "R002_CONTRADICTION_NAMES",
    "R003_INPUT_DOMAIN",
    "reason_r003_from_reviewed_artifacts",
    "integrate_r003_with_b010",
    "reason_from_reviewed_artifacts",
    "ViewGeometry",
    "build_base_block_modeling_plan",
]
