"""Deterministic source/native correspondence for physical orthographic views.

Frames are fixed from reviewed ProjectionGraph and native transform metadata
before matching. No fixture truth, fitted transform, or evaluator input is read.
"""

from __future__ import annotations

import math
from typing import Any, Mapping

from experiments.three_view_reconstruction.drawing.view_coordinate_transform import normalize_geometry
from experiments.three_view_reconstruction.drawing.view_orientation import canonicalize
from experiments.three_view_reconstruction.validation.primitive_matcher import (
    POSITION_TOLERANCE_MM, match,
)

from .r003_source_native_correspondence import _axis, _spline_support


FRAMES = {
    "FRONT": {"axes": ("+X", "+Y"), "coordinate": "X/Y", "source_y_sign": 1,
              "right": (1, 0, 0), "up": (0, 1, 0), "normal": (0, 0, 1)},
    "TOP": {"axes": ("+X", "-Z"), "coordinate": "X/Z", "source_y_sign": -1,
            "right": (1, 0, 0), "up": (0, 0, -1), "normal": (0, 1, 0)},
    "LEFT": {"axes": ("+Z", "+Y"), "coordinate": "Z/Y", "source_y_sign": 1,
             "right": (0, 0, 1), "up": (0, 1, 0), "normal": (-1, 0, 0)},
}


def source_frame(projection: Mapping[str, Any], role: str) -> dict:
    contract = FRAMES[role]
    key = role.lower()
    graph = projection["projection_graph"]
    convention = graph["coordinate_convention"]
    orientation = projection["view_transforms"][key]["orientation"]
    matrix = projection["view_transforms"][key]["pixel_to_view_mm"]
    dpi = float(projection["dpi"])
    numerator = float(projection["scale_numerator"])
    denominator = float(projection["scale_denominator"])
    if not all(math.isfinite(value) and value > 0 for value in (dpi, numerator, denominator)):
        raise ValueError("SOURCE_SCALE_INVALID")
    mm_per_pixel = 25.4 / dpi * denominator / numerator
    extent = [float(graph[key]["horizontal_extent"]),
              float(graph[key]["vertical_extent"])]
    if (convention.get("units") != "mm" or convention.get("origin") != "VIEW_BOTTOM_LEFT"
            or convention.get(key) != contract["coordinate"]
            or orientation.get("canonical_role") != role
            or orientation.get("projection_type") != "THIRD_ANGLE"
            or orientation.get("mirror_state") is not False
            or any(not _axis(orientation.get(axis), contract[axis])
                   for axis in ("right", "up", "normal"))
            or len(matrix) != 6 or any(not math.isfinite(float(value)) for value in matrix)
            or matrix[0] <= 0 or matrix[4] >= 0 or matrix[1] != 0 or matrix[3] != 0
            or abs(matrix[0] - mm_per_pixel) > 1e-9
            or abs(matrix[4] + mm_per_pixel) > 1e-9
            or not all(math.isfinite(value) and value > 0 for value in extent)):
        raise ValueError("SOURCE_FRAME_INVALID")
    sign = contract["source_y_sign"]
    return {"status": "PASS", "frame": f"CANONICAL_{role}_2D",
            "axes": list(contract["axes"]), "unit": "mm",
            "origin": "SOURCE_VIEW_BOTTOM_LEFT",
            "source_to_canonical": [1, 0, 0, 0, sign,
                                    extent[1] if sign < 0 else 0],
            "pixel_y_inversion_already_applied_by_source": True,
            "scale_mm_per_pixel_from_independent_metadata": mm_per_pixel,
            "pixel_to_view_mm": list(matrix), "extent_mm": extent}


def _source_primitives(source: Mapping[str, Any], frame: Mapping[str, Any]) -> dict:
    """Apply only the predeclared source-axis sign and reviewed view extent."""
    sign = frame["source_to_canonical"][4]
    offset = frame["source_to_canonical"][5]
    def line(row):
        return {"x1": float(row["x1"]), "y1": sign * float(row["y1"]) + offset,
                "x2": float(row["x2"]), "y2": sign * float(row["y2"]) + offset}
    def circle(row):
        return {"x": float(row["x"]), "y": sign * float(row["y"]) + offset,
                "diameter": float(row["diameter"])}
    def arc(row):
        result = {**row, "x": float(row["x"]), "y": sign * float(row["y"]) + offset}
        if sign < 0:
            result["start_angle_deg"] = (-float(row["start_angle_deg"])) % 360
            result["end_angle_deg"] = (-float(row["end_angle_deg"])) % 360
            result["sweep_direction"] = "CW" if row["sweep_direction"] == "CCW" else "CCW"
        return result
    return {"visible_segments": [line(row) for row in source["visible_segments"]],
            "hidden_segments": [line(row) for row in source["hidden_segments"]],
            "circles": [circle(row) for row in source["circles"]],
            "arcs": [arc(row) for row in source["arcs"]]}


def native_frame(frame: Mapping[str, Any], hlr: Mapping[str, Any], role: str) -> dict:
    contract = FRAMES[role]
    axes = frame["transform_axes"]
    try:
        physical = canonicalize(hlr["native_identity"]).canonical_role.value
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("NATIVE_ORIENTATION_INVALID") from exc
    if (physical != role or frame.get("native_identity") != hlr.get("native_identity")
            or any(not _axis(axes.get(axis), contract[axis])
                   for axis in ("right", "up", "normal"))
            or not isinstance(hlr.get("lines"), list) or not hlr["lines"]
            or not isinstance(hlr.get("scale"), (int, float))
            or not math.isfinite(hlr["scale"]) or hlr["scale"] <= 0
            or frame.get("scale") != hlr["scale"]
            or frame.get("projection_standard") != "THIRD_ANGLE"
            or not frame.get("model_reference")):
        raise ValueError("NATIVE_FRAME_INVALID")
    lines, circles, arcs, origin = normalize_geometry(
        hlr["lines"], hlr["circles"], hlr["arcs"])
    if not all(math.isfinite(value) for value in origin):
        raise ValueError("NATIVE_ORIGIN_INVALID")
    xs = [value for line in lines for value in (line["x1"], line["x2"])]
    ys = [value for line in lines for value in (line["y1"], line["y2"])]
    xs += [row["x"] + row["diameter"] / 2 for row in circles]
    ys += [row["y"] + row["diameter"] / 2 for row in circles]
    xs += [row["x"] + row["radius"] for row in arcs]
    ys += [row["y"] + row["radius"] for row in arcs]
    return {"status": "PASS", "frame": f"CANONICAL_{role}_2D",
            "axes": list(contract["axes"]), "unit": "mm",
            "origin": "NATIVE_HLR_VIEW_ENVELOPE_MIN",
            "native_to_canonical": [1, 0, -origin[0], 0, 1, -origin[1]],
            "origin_removed_mm": list(origin), "extent_mm": [max(xs), max(ys)],
            "view_scale_recorded_not_reapplied": hlr["scale"],
            "visible_lines": lines, "visible_circles": circles,
            "visible_arcs": arcs}


def _hidden(source_lines: list[dict], generated: Mapping[str, Any], origin: tuple[float, float]) -> dict:
    candidates = []
    for row in generated.get("hidden_lines", []):
        if row.get("geometry_type") != "LINE" or row.get("source") != "HLV_MINUS_HLR":
            raise ValueError("UNSUPPORTED_HIDDEN_LINE_PROVENANCE")
        geometry = row["geometry"]
        line = {key: float(geometry[key]) - origin[0 if key.startswith("x") else 1]
                for key in ("x1", "y1", "x2", "y2")}
        candidates.append({"native_type": "LINE", "support": [line]})
    for row in generated.get("hidden_splines", []):
        spline = _spline_support(row, origin)
        candidates.append({"native_type": "SPLINE", "support":
                           spline["comparison_geometry"]["segments_for_existing_matcher"],
                           "spline_evidence": spline})
    pairings = [[j for j, candidate in enumerate(candidates)
                 if match([line], candidate["support"], "line")["status"] == "PASS"]
                for line in source_lines]
    reverse = [[i for i, matches in enumerate(pairings) if j in matches]
               for j in range(len(candidates))]
    passed = (len(source_lines) == len(candidates)
              and all(len(row) == 1 for row in pairings)
              and all(len(row) == 1 for row in reverse))
    return {"status": "PASS" if passed else "FAIL",
            "source_count": len(source_lines), "native_count": len(candidates),
            "native_line_count": sum(row["native_type"] == "LINE" for row in candidates),
            "native_spline_count": sum(row["native_type"] == "SPLINE" for row in candidates),
            "matched_pairs": [[i, row[0]] for i, row in enumerate(pairings) if len(row) == 1],
            "unmatched_source": [i for i, row in enumerate(pairings) if not row],
            "unmatched_native": [j for j, row in enumerate(reverse) if not row],
            "ambiguous_source": [i for i, row in enumerate(pairings) if len(row) > 1],
            "ambiguous_native": [j for j, row in enumerate(reverse) if len(row) > 1],
            "native_types": [row["native_type"] for row in candidates],
            "spline_comparison_evidence": [row["spline_evidence"] for row in candidates
                                           if row["native_type"] == "SPLINE"]}


def compare_view(projection: Mapping[str, Any], hlr: Mapping[str, Any],
                 generated: Mapping[str, Any], frame: Mapping[str, Any], role: str) -> dict:
    """Resolve fixed frames first; then compare all visible and hidden supports."""
    source_description = source_frame(projection, role)
    native_description = native_frame(frame, hlr, role)
    source = _source_primitives(projection["projection_graph"][role.lower()],
                                source_description)
    if (hlr.get("unclassified_polylines") or hlr.get("native_splines")
            or generated.get("visible_splines")
            or generated.get("circles") != hlr.get("circles")
            or generated.get("arcs") != hlr.get("arcs")
            or generated.get("unresolved_discriminating_primitives") != 0):
        raise ValueError("UNSUPPORTED_NATIVE_CORRESPONDENCE_INPUT")
    if any(abs(a - b) > POSITION_TOLERANCE_MM for a, b in zip(
            source_description["extent_mm"], native_description["extent_mm"])):
        raise ValueError("SOURCE_NATIVE_EXTENT_CONTRADICTION")
    visible = {"lines": match(source["visible_segments"], native_description["visible_lines"], "line"),
               "circles": match(source["circles"], native_description["visible_circles"], "circle"),
               "arcs": match(source["arcs"], native_description["visible_arcs"], "arc")}
    hidden = _hidden(source["hidden_segments"], generated,
                     tuple(native_description["origin_removed_mm"]))
    passed = all(row["status"] == "PASS" for row in visible.values()) and hidden["status"] == "PASS"
    return {"status": "PASS" if passed else "FAIL", "role": role,
            "source_frame": source_description, "native_frame": native_description,
            "visible": visible, "hidden": hidden,
            "transform_derived_from_match_result": False}


def compare_three_views(projection: Mapping[str, Any], hlr: Mapping[str, Mapping[str, Any]],
                        generated: Mapping[str, Mapping[str, Any]],
                        frames: Mapping[str, Mapping[str, Any]]) -> dict:
    if any(not set(FRAMES).issubset(mapping) for mapping in (hlr, generated, frames)):
        raise ValueError("THREE_PHYSICAL_VIEWS_REQUIRED")
    results = {role: compare_view(projection, hlr[role], generated[role], frames[role], role)
               for role in ("FRONT", "TOP", "LEFT")}
    return {"status": "PASS" if all(row["status"] == "PASS" for row in results.values())
            else "FAIL", "views": results, "fit_to_expected_operations": 0}
