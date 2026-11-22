"""Pure native-coordinate helpers; no evaluator files or CAD activation."""
from copy import deepcopy
import math

from .r003_native_collectors import NativeEvidenceUnavailable


MODEL_PROJECTED_PRIMITIVE = "MODEL_PROJECTED_PRIMITIVE"
SHEET_ANNOTATION = "SHEET_ANNOTATION"


def _numbers(values, count, label):
    if (not isinstance(values, (list, tuple)) or len(values) != count
            or any(type(v) not in (int, float) or not math.isfinite(v) for v in values)):
        raise NativeEvidenceUnavailable(f"{label}: finite native coordinates required")
    return values


def normalize_primitive(geometry, kind, frame, *, source_space):
    """Model projections retain model size; annotations retain sheet scaling."""
    scale = frame.get("scale")
    if type(scale) not in (int, float) or not math.isfinite(scale) or scale <= 0:
        raise NativeEvidenceUnavailable("native drawing scale invalid")
    translation = _numbers(frame.get("native_transform", {}).get("translation"), 3, "translation")
    position = _numbers(frame.get("sheet_position"), 2, "sheet position")
    if source_space == MODEL_PROJECTED_PRIMITIVE:
        offsets = [(position[i] - translation[i]) * 1000 / scale for i in range(2)]
        factor = 1.0
    elif source_space == SHEET_ANNOTATION:
        offsets = [-translation[i] * 1000 / scale for i in range(2)]
        factor = 1 / scale
    else:
        raise NativeEvidenceUnavailable("unknown primitive coordinate space")
    keys = ("x1", "y1", "x2", "y2") if kind == "line" else ("x", "y")
    if kind not in {"line", "circle", "arc"}:
        raise NativeEvidenceUnavailable("unsupported drawing primitive")
    _numbers([geometry.get(k) for k in keys], len(keys), "primitive")
    result = dict(geometry)
    for k in keys:
        result[k] = geometry[k] * factor + offsets[0 if k.startswith("x") else 1]
    if kind != "line":
        key = "diameter" if kind == "circle" else "radius"
        value = geometry.get(key)
        _numbers([value], 1, key)
        if value <= 0:
            raise NativeEvidenceUnavailable("nonpositive native curve size")
        result[key] = value * factor
    _numbers([result[k] for k in keys], len(keys), "normalized primitive")
    return result


def hidden_linear_spline(candidate, frame):
    """Accept only the exact bounded two-control-point linear native structure."""
    raw = candidate.get("geometry", {}).get("native_evidence", {})
    if (candidate.get("geometry_type") != "SPLINE" or candidate.get("semantic") != "HIDDEN"
            or raw.get("raw_kind") != 3 or raw.get("structural_validity") != "PASS"
            or type(raw.get("degree")) is not int or raw["degree"] != 1
            or type(raw.get("rational")) is not int or raw["rational"] != 0
            or raw.get("num_dims") != 3 or raw.get("num_control_points") != 2
            or raw.get("num_knots") != 4 or raw.get("raw_geometry_count") != 15
            or raw.get("coordinate_unit") != "m"):
        raise NativeEvidenceUnavailable("unproven hidden bounded linear spline")
    knots = _numbers(raw.get("knots"), 4, "linear spline knots")
    if not (knots[0] == knots[1] < knots[2] == knots[3]):
        raise NativeEvidenceUnavailable("non-clamped linear spline knots")
    control = raw.get("control_points")
    if not isinstance(control, list) or len(control) != 2:
        raise NativeEvidenceUnavailable("linear spline control count")
    a, b = [_numbers(p, 3, "linear spline control") for p in control]
    _numbers(raw.get("line_attributes"), 6, "linear spline attributes")
    samples = raw.get("sample_points")
    if (not isinstance(samples, list) or len(samples) < 2
            or len(samples) != raw.get("num_sample_points")):
        raise NativeEvidenceUnavailable("linear spline samples unavailable")
    delta = [b[i] - a[i] for i in range(3)]
    length_sq = sum(v*v for v in delta)
    if length_sq <= 0 or delta[0]*delta[0] + delta[1]*delta[1] <= 0:
        raise NativeEvidenceUnavailable("degenerate projected linear spline")
    parameters = []
    for point in samples:
        point = _numbers(point, 3, "linear spline sample")
        t = sum((point[i]-a[i])*delta[i] for i in range(3)) / length_sq
        # Structural roundoff allowance, not a drawing acceptance threshold.
        if not (-1e-12 <= t <= 1+1e-12) or any(
                abs(point[i] - (a[i]+t*delta[i])) > 1e-12 for i in range(3)):
            raise NativeEvidenceUnavailable("nonlinear or unbounded spline sample")
        parameters.append(t)
    if abs(parameters[0]) > 1e-12 or abs(parameters[-1]-1) > 1e-12:
        raise NativeEvidenceUnavailable("linear spline endpoint samples mismatch")
    geometry = normalize_primitive({"x1": a[0]*1000, "y1": a[1]*1000,
                                    "x2": b[0]*1000, "y2": b[1]*1000},
                                   "line", frame, source_space=MODEL_PROJECTED_PRIMITIVE)
    return {"geometry": geometry, "source_visibility": "HIDDEN",
            "comparison_semantic": "LINEAR_SPLINE_AS_HIDDEN_LINE",
            "original_native_primitive_type": "SPLINE", "native_evidence": deepcopy(raw)}
