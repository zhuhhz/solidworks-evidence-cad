"""Canonical B008 projection contracts exposed through the reasoning namespace."""

from experiments.three_view_reconstruction.schemas.projection_graph import ProjectionGraph
from experiments.three_view_reconstruction.schemas.view_geometry import (
    Arc,
    Circle,
    HiddenLinePair,
    LineSegment,
    ViewGeometry,
)

__all__ = [
    "Arc",
    "Circle",
    "HiddenLinePair",
    "LineSegment",
    "ProjectionGraph",
    "ViewGeometry",
]
