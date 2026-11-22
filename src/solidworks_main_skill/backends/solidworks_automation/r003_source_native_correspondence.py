"""Bounded LEFT source/native drawing comparison; never retypes native splines."""

from __future__ import annotations

from copy import deepcopy
import math
from typing import Any, Mapping

from experiments.three_view_reconstruction.drawing.view_coordinate_transform import normalize_geometry
from experiments.three_view_reconstruction.validation.primitive_matcher import (
    POSITION_TOLERANCE_MM, match,
)


def _axis(actual: Any, expected: tuple[int, int, int]) -> bool:
    return (isinstance(actual, (list, tuple)) and len(actual) == 3
            and all(type(value) in (int, float) and math.isfinite(value)
                    and abs(value - target) <= 1e-6
                    for value, target in zip(actual, expected)))


def _source_frame(projection: Mapping[str, Any]) -> dict:
    graph = projection["projection_graph"]
    convention = graph["coordinate_convention"]
    left = projection["view_transforms"]["left"]
    orientation = left["orientation"]
    matrix = left["pixel_to_view_mm"]
    dpi = float(projection["dpi"])
    scale_numerator = float(projection["scale_numerator"])
    scale_denominator = float(projection["scale_denominator"])
    if not all(math.isfinite(value) and value > 0
               for value in (dpi, scale_numerator, scale_denominator)):
        raise ValueError("SOURCE_LEFT_SCALE_INVALID")
    expected_mm_per_px = 25.4 / dpi * scale_denominator / scale_numerator
    extent = (float(graph["left"]["horizontal_extent"]),
              float(graph["left"]["vertical_extent"]))
    if (convention.get("units") != "mm" or convention.get("origin") != "VIEW_BOTTOM_LEFT"
            or convention.get("left") != "Z/Y"
            or orientation.get("canonical_role") != "LEFT"
            or orientation.get("projection_type") != "THIRD_ANGLE"
            or orientation.get("mirror_state") is not False
            or not _axis(orientation.get("right"), (0, 0, 1))
            or not _axis(orientation.get("up"), (0, 1, 0))
            or not _axis(orientation.get("normal"), (-1, 0, 0))
            or len(matrix) != 6 or any(not math.isfinite(float(value)) for value in matrix)
            or matrix[0] <= 0 or matrix[4] >= 0 or matrix[1] != 0 or matrix[3] != 0
            or not all(math.isfinite(value) and value > 0
                       for value in (dpi, scale_numerator, scale_denominator))
            or abs(matrix[0] - expected_mm_per_px) > 1e-9
            or abs(matrix[4] + expected_mm_per_px) > 1e-9
            or not all(math.isfinite(value) and value > 0 for value in extent)):
        raise ValueError("SOURCE_LEFT_FRAME_INVALID")
    return {"status": "PASS", "frame": "CANONICAL_LEFT_2D", "axes": ["+Z", "+Y"],
            "unit": "mm", "origin": "SOURCE_VIEW_BOTTOM_LEFT",
            "source_to_canonical": [1, 0, 0, 0, 1, 0],
            "pixel_y_inversion_already_applied_by_source": True,
            "scale_mm_per_pixel_from_independent_metadata": expected_mm_per_px,
            "pixel_to_view_mm": list(matrix), "extent_mm": list(extent)}


def _native_frame(native_frame: Mapping[str, Any], hlr: Mapping[str, Any]) -> dict:
    axes = native_frame["transform_axes"]
    if (not _axis(axes.get("right"), (0, 0, 1))
            or not _axis(axes.get("up"), (0, 1, 0))
            or not _axis(axes.get("normal"), (-1, 0, 0))
            or hlr.get("native_identity") not in ("*左视", "*Left")
            or native_frame.get("native_identity") != hlr.get("native_identity")
            or not isinstance(hlr.get("lines"), list) or not hlr["lines"]
            or not isinstance(hlr.get("scale"), (int, float))
            or not math.isfinite(hlr["scale"]) or hlr["scale"] <= 0
            or native_frame.get("scale") != hlr["scale"]):
        raise ValueError("NATIVE_LEFT_FRAME_INVALID")
    lines, circles, arcs, origin = normalize_geometry(
        hlr["lines"], hlr["circles"], hlr["arcs"])
    if not all(math.isfinite(value) for value in origin):
        raise ValueError("NATIVE_LEFT_ORIGIN_INVALID")
    extent = [max(value for line in lines for value in (line["x1"], line["x2"])),
              max(value for line in lines for value in (line["y1"], line["y2"]))]
    return {"status": "PASS", "frame": "CANONICAL_LEFT_2D", "axes": ["+Z", "+Y"],
            "unit": "mm", "origin": "NATIVE_HLR_VIEW_ENVELOPE_MIN",
            "native_to_canonical": [1, 0, -origin[0], 0, 1, -origin[1]],
            "origin_removed_mm": list(origin), "extent_mm": extent,
            "view_scale_recorded_not_reapplied": hlr["scale"],
            "visible_lines": lines, "visible_circles": circles, "visible_arcs": arcs}


def _spline_support(candidate: Mapping[str, Any], origin: tuple[float, float]) -> dict:
    if candidate.get("geometry_type") != "SPLINE":
        raise ValueError("UNSUPPORTED_NATIVE_TYPE")
    geometry = candidate["geometry"]
    native = geometry["native_evidence"]
    if (geometry.get("native_type") != "SPLINE" or native.get("raw_kind") != 3
            or native.get("structural_validity") != "PASS"
            or native.get("degree") != 1 or native.get("rational") != 0
            or native.get("num_dims") != 3 or native.get("num_control_points") != 2
            or native.get("coordinate_unit") != "m"):
        raise ValueError("UNSUPPORTED_NATIVE_SPLINE")
    points = native.get("sample_points")
    controls = native.get("control_points")
    if (not isinstance(points, list) or len(points) < 2
            or len(points) != native.get("num_sample_points")
            or not isinstance(controls, list) or len(controls) != 2
            or any(not isinstance(point, list) or len(point) != 3 for point in points + controls)
            or not all(type(value) in (int, float) and math.isfinite(value)
                       for point in points + controls for value in point)):
        raise ValueError("MALFORMED_NATIVE_SPLINE_SUPPORT")
    if (min(math.dist(points[0], controls[0]), math.dist(points[0], controls[1])) * 1000
            > POSITION_TOLERANCE_MM
            or min(math.dist(points[-1], controls[0]), math.dist(points[-1], controls[1])) * 1000
            > POSITION_TOLERANCE_MM):
        raise ValueError("NATIVE_SPLINE_SAMPLE_ENDPOINT_MISMATCH")
    canonical = [[point[0] * 1000 - origin[0], point[1] * 1000 - origin[1]]
                 for point in points]
    segments = [{"x1": a[0], "y1": a[1], "x2": b[0], "y2": b[1]}
                for a, b in zip(canonical, canonical[1:])]
    if any(math.dist(a, b) <= 1e-9 for a, b in zip(canonical, canonical[1:])):
        raise ValueError("DEGENERATE_NATIVE_SPLINE_SUPPORT")
    return {"native_type": "SPLINE", "native_evidence": deepcopy(native),
            "comparison_geometry": {"type": "CURVE_SUPPORT", "points_mm": canonical,
                                    "segments_for_existing_matcher": segments}}


def compare_left_source_native(projection: Mapping[str, Any],
                               hlr: Mapping[str, Any], generated_left: Mapping[str, Any],
                               native_frame: Mapping[str, Any]) -> dict:
    """Both transforms are fixed before any primitive matching or IoU computation."""
    source_frame = _source_frame(projection)
    native = _native_frame(native_frame, hlr)
    source = projection["projection_graph"]["left"]
    if (hlr.get("unclassified_polylines") or generated_left.get("hidden_lines")
            or generated_left.get("visible_splines")
            or generated_left.get("circles") != hlr.get("circles")
            or generated_left.get("arcs") != hlr.get("arcs")
            or generated_left.get("unresolved_discriminating_primitives") != 0):
        raise ValueError("UNSUPPORTED_LEFT_NATIVE_CORRESPONDENCE_INPUT")
    if any(abs(a - b) > POSITION_TOLERANCE_MM for a, b in zip(
            source_frame["extent_mm"], native["extent_mm"])):
        raise ValueError("SOURCE_NATIVE_LEFT_EXTENT_CONTRADICTION")
    visible = {"lines": match(source["visible_segments"], native["visible_lines"], "line"),
               "circles": match(source["circles"], native["visible_circles"], "circle"),
               "arcs": match(source["arcs"], native["visible_arcs"], "arc")}
    if any(row["status"] != "PASS" for row in visible.values()):
        return {"status": "FAIL", "source_frame": source_frame,
                "native_frame": native, "visible": visible, "hidden": {"status": "NOT_RUN"}}
    origin = tuple(native["origin_removed_mm"])
    splines = [_spline_support(row, origin) for row in generated_left.get("hidden_splines", [])]
    source_hidden = source["hidden_segments"]
    pairings = [[j for j, spline in enumerate(splines)
                 if match([line], spline["comparison_geometry"]["segments_for_existing_matcher"],
                          "line")["status"] == "PASS"]
                for line in source_hidden]
    reverse = [[i for i, candidates in enumerate(pairings) if j in candidates]
               for j in range(len(splines))]
    hidden_status = ("PASS" if len(source_hidden) == len(splines)
                     and all(len(candidates) == 1 for candidates in pairings)
                     and all(len(candidates) == 1 for candidates in reverse) else "FAIL")
    hidden = {"status": hidden_status, "source_count": len(source_hidden),
              "native_spline_count": len(splines), "matched_pairs":
              [[i, candidates[0]] for i, candidates in enumerate(pairings)
               if len(candidates) == 1],
              "unmatched_source": [i for i, candidates in enumerate(pairings) if not candidates],
              "unmatched_native": [j for j, candidates in enumerate(reverse) if not candidates],
              "ambiguous_source": [i for i, candidates in enumerate(pairings) if len(candidates) > 1],
              "ambiguous_native": [j for j, candidates in enumerate(reverse) if len(candidates) > 1],
              "type_exact": not splines, "geometric_support_match": hidden_status,
              "spline_comparison_evidence": splines}
    return {"status": hidden_status, "source_frame": source_frame,
            "native_frame": native, "visible": visible, "hidden": hidden,
            "transform_derived_from_match_result": False}
