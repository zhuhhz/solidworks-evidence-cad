"""Deterministic visual review and confirmation for bounded B009 candidates.

Only the source PNG, B008 ProjectionGraph candidate, and B009 FeatureEvidence
candidate are inputs. Frozen truth and CAD semantics are deliberately absent.
"""

from copy import deepcopy
from hashlib import sha256
import json
import math
from pathlib import Path

from solidworks_main_skill.vision.evidence import _canonical, infer


VERSION = "human-confirmation-mvp/0.1"
SCHEMA_VERSION = "human-confirmation/0.1"
REVIEW_SCHEMA_VERSION = "human-review/0.1"
DECISIONS = {"CONFIRMED", "REJECTED", "CORRECTED"}
EVIDENCE_ROLES = {
    "VISIBLE_PROFILE_EDGE",
    "OBSERVED_CIRCLE",
    "ORTHOGONAL_HIDDEN_PAIR",
    "LINE_ARC_BOUNDARY_EDGE",
    "ARC_BOUNDARY",
    "CENTERLINE_SUPPORT",
}
PRIMITIVE_TYPES = {"LINE", "HIDDEN_LINE", "CIRCLE", "ARC", "CENTERLINE"}
CORRECTION_FIELDS = {
    "RELABEL_EVIDENCE_ROLE": {"kind", "evidence_id", "role"},
    "REJECT_EVIDENCE": {"kind", "evidence_id"},
    "MARK_MISSING_EVIDENCE": {"kind", "primitive_id", "role"},
    "REJECT_RELATIONSHIP": {"kind", "relationship_id"},
    "RECLASSIFY_PRIMITIVE": {"kind", "primitive_id", "primitive_type"},
}
COLORS = {
    "LINE": (220, 40, 40),
    "HIDDEN_LINE": (235, 125, 20),
    "CIRCLE": (25, 105, 220),
    "ARC": (145, 50, 200),
    "CENTERLINE": (15, 150, 80),
}


def _digest(data):
    return sha256(data).hexdigest()


def _load_json_bytes(data, label):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate JSON key in {label}: {key}")
            result[key] = value
        return result

    def constant(value):
        raise ValueError(f"non-finite JSON constant in {label}: {value}")

    value = json.loads(data.decode("utf-8-sig"), object_pairs_hook=pairs, parse_constant=constant)
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _json_bytes(value):
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode()


def _read_inputs(png_path, projection_path, evidence_path):
    png_path, projection_path, evidence_path = map(Path, (png_path, projection_path, evidence_path))
    png = png_path.read_bytes()
    projection_bytes = projection_path.read_bytes()
    evidence_bytes = evidence_path.read_bytes()
    projection = _load_json_bytes(projection_bytes, "ProjectionGraph candidate")
    evidence = _load_json_bytes(evidence_bytes, "FeatureEvidence candidate")
    if projection.get("image_sha256") != _digest(png):
        raise ValueError("source image hash does not match ProjectionGraph candidate")
    regenerated = infer(projection)
    if _canonical(regenerated) != _canonical(evidence):
        raise ValueError("FeatureEvidence candidate does not match deterministic inference")
    return {
        "png_path": png_path,
        "projection_path": projection_path,
        "evidence_path": evidence_path,
        "png": png,
        "projection_bytes": projection_bytes,
        "evidence_bytes": evidence_bytes,
        "projection": projection,
        "evidence": evidence,
    }


def _inverse_transform(transform, x, y):
    a, b, c, d, e, f = transform["pixel_to_view_mm"]
    determinant = a * e - b * d
    if not math.isfinite(determinant) or abs(determinant) < 1e-12:
        raise ValueError("view transform is not invertible")
    return ((e * (x - c) - b * (y - f)) / determinant,
            (-d * (x - c) + a * (y - f)) / determinant)


def _arc_points(primitive, transform):
    start = float(primitive["start_angle_deg"])
    end = float(primitive["end_angle_deg"])
    if primitive["sweep_direction"] == "CCW":
        sweep = (end - start) % 360
    elif primitive["sweep_direction"] == "CW":
        sweep = -((start - end) % 360)
    else:
        raise ValueError("invalid ARC sweep direction")
    if not sweep:
        raise ValueError("full or zero ARC sweep is unsupported")
    count = max(12, int(abs(sweep) / 5))
    result = []
    for index in range(count + 1):
        angle = math.radians(start + sweep * index / count)
        x = primitive["x"] + primitive["radius"] * math.cos(angle)
        y = primitive["y"] + primitive["radius"] * math.sin(angle)
        result.append(_inverse_transform(transform, x, y))
    return result


def _dashed(page, points, fill, width, dash=14, gap=9):
    for first, second in zip(points, points[1:]):
        dx, dy = second[0] - first[0], second[1] - first[1]
        length = math.hypot(dx, dy)
        if not length:
            continue
        offset = 0.0
        while offset < length:
            stop = min(offset + dash, length)
            page.draw_line((first[0] + dx * offset / length, first[1] + dy * offset / length),
                           (first[0] + dx * stop / length, first[1] + dy * stop / length),
                           color=fill, width=width, overlay=True)
            offset += dash + gap


def _primitive_inventory(projection):
    graph = projection["projection_graph"]
    transforms = projection["view_transforms"]
    inventory = {}
    families = (
        ("visible_segments", "LINE"),
        ("hidden_segments", "HIDDEN_LINE"),
        ("circles", "CIRCLE"),
        ("arcs", "ARC"),
        ("centerlines", "CENTERLINE"),
    )
    for view in ("front", "top", "left"):
        if view not in transforms:
            raise ValueError(f"missing image transform for view: {view}")
        for field, kind in families:
            for primitive in graph[view][field]:
                identity = primitive["primitive_id"]
                if identity in inventory:
                    raise ValueError("duplicate primitive identity in review input")
                inventory[identity] = {"primitive_id": identity, "primitive_type": kind,
                                       "view": view, "geometry": primitive,
                                       "transform": transforms[view]}
    return inventory


def _display_labels(inventory, evidence):
    primitive = {identity: f"P{index:02d}"
                 for index, identity in enumerate(sorted(inventory), 1)}
    evidence_labels = {row["evidence_id"]: f"E{index:02d}"
                       for index, row in enumerate(
                           sorted(evidence["feature_evidence"], key=lambda item: item["evidence_id"]), 1)}
    relationships = {row["relationship_id"]: f"R{index:02d}"
                     for index, row in enumerate(
                         sorted(evidence["cross_view_relationships"],
                                key=lambda item: item["relationship_id"]), 1)}
    return primitive, evidence_labels, relationships


def _primitive_points(item):
    geometry, transform, kind = item["geometry"], item["transform"], item["primitive_type"]
    if kind in {"LINE", "HIDDEN_LINE", "CENTERLINE"}:
        return [_inverse_transform(transform, geometry["x1"], geometry["y1"]),
                _inverse_transform(transform, geometry["x2"], geometry["y2"])]
    if kind == "CIRCLE":
        result = []
        for index in range(73):
            angle = 2 * math.pi * index / 72
            result.append(_inverse_transform(
                transform,
                geometry["x"] + geometry["diameter"] / 2 * math.cos(angle),
                geometry["y"] + geometry["diameter"] / 2 * math.sin(angle),
            ))
        return result
    return _arc_points(geometry, transform)


def _centre(points):
    return sum(point[0] for point in points) / len(points), sum(point[1] for point in points) / len(points)


def _review_model(inputs, overlay_hash):
    projection, evidence = inputs["projection"], inputs["evidence"]
    inventory = _primitive_inventory(projection)
    primitive_labels, evidence_labels, relationship_labels = _display_labels(inventory, evidence)
    primitives = [{**{key: item[key] for key in ("primitive_id", "primitive_type", "view")},
                   "display_label": primitive_labels[item["primitive_id"]]}
                  for item in sorted(inventory.values(), key=lambda row: row["primitive_id"])]
    evidence_items = []
    for row in evidence["feature_evidence"]:
        evidence_items.append({
            "evidence_id": row["evidence_id"],
            "display_label": evidence_labels[row["evidence_id"]],
            "geometry_reference": row["geometry_reference"],
            "geometry_type": row["geometry_type"],
            "role": evidence["roles"][row["evidence_id"]],
            "view": row["view"],
        })
    relationships = [{"relationship_id": row["relationship_id"],
                      "display_label": relationship_labels[row["relationship_id"]],
                      "relationship_type": row["evidence_type"],
                      "source_evidence_ids": sorted(row["source_evidence_ids"])}
                     for row in evidence["cross_view_relationships"]]
    issue_markers = []
    for layer, rows in (("PROJECTION_GRAPH", projection.get("issues", [])),
                        ("FEATURE_EVIDENCE", evidence.get("issues", []))):
        for row in rows:
            issue_markers.append({"layer": layer, "classification": "AMBIGUOUS_OR_UNSUPPORTED",
                                  "detail": deepcopy(row)})
    return {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "version": VERSION,
        "sources": {
            "image": {"reference": inputs["png_path"].name, "sha256": _digest(inputs["png"])},
            "projection_graph_candidate": {
                "reference": inputs["projection_path"].name,
                "sha256": _digest(inputs["projection_bytes"]),
            },
            "feature_evidence_candidate": {
                "reference": inputs["evidence_path"].name,
                "sha256": _digest(inputs["evidence_bytes"]),
            },
        },
        "overlay": {"reference": "review-overlay.png", "sha256": overlay_hash},
        "machine_state": {"projection": projection["status"], "evidence": evidence["status"]},
        "primitives": primitives,
        "feature_evidence": sorted(evidence_items, key=lambda row: row["evidence_id"]),
        "derived_relationships": sorted(relationships, key=lambda row: row["relationship_id"]),
        "issue_markers": issue_markers,
        "provenance": {
            "generator": "solidworks_main_skill.review",
            "inputs": "PNG_PLUS_PROJECTION_GRAPH_PLUS_FEATURE_EVIDENCE",
            "feature_ownership": "NOT_INFERRED_BY_SCOPE",
            "truth_access": "PROHIBITED",
        },
    }


def _render_overlay(inputs):
    try:
        import pymupdf
    except ImportError as exc:
        raise RuntimeError("PyMuPDF is required to render the review overlay") from exc

    projection, evidence = inputs["projection"], inputs["evidence"]
    source = pymupdf.Pixmap(inputs["png"])
    if source.n != 1 or source.alpha or [source.width, source.height] != projection["image_size_px"]:
        raise ValueError("review source must be the candidate 8-bit grayscale image")
    document = pymupdf.open()
    page = document.new_page(width=source.width, height=source.height)
    page.insert_image(page.rect, stream=inputs["png"])
    inventory = _primitive_inventory(projection)
    primitive_labels, evidence_labels, relationship_labels = _display_labels(inventory, evidence)
    derived = {row["primitive_id"]: row for row in evidence["derived_primitives"]}
    evidence_by_geometry = {}
    evidence_by_id = {}
    for row in evidence["feature_evidence"]:
        evidence_by_id[row["evidence_id"]] = row
        evidence_by_geometry.setdefault(row["geometry_reference"], []).append(row)

    points_by_reference = {}
    for identity, item in sorted(inventory.items()):
        points = _primitive_points(item)
        points_by_reference[identity] = points
        kind, color = item["primitive_type"], COLORS[item["primitive_type"]]
        color = tuple(channel / 255 for channel in color)
        if kind in {"HIDDEN_LINE", "CENTERLINE"}:
            _dashed(page, points, color, 4, dash=18 if kind == "HIDDEN_LINE" else 28,
                    gap=10 if kind == "HIDDEN_LINE" else 8)
        else:
            page.draw_polyline(points, color=color, width=4, overlay=True)
        centre = _centre(points)
        linked = sorted(evidence_labels[row["evidence_id"]]
                        for row in evidence_by_geometry.get(identity, []))
        label = primitive_labels[identity] + ("/" + "/".join(linked) if linked else "")
        width = pymupdf.get_text_length(label, fontname="helv", fontsize=18)
        box = pymupdf.Rect(centre[0] - width / 2 - 3, centre[1] - 11,
                           centre[0] + width / 2 + 3, centre[1] + 10)
        page.draw_rect(box, fill=(1, 1, .90), color=None, overlay=True)
        page.insert_text((centre[0] - width / 2, centre[1] + 6), label,
                         color=color, fontsize=18, fontname="helv", overlay=True)

    for identity, row in derived.items():
        source_points = [point for source_id in row["source_primitive_ids"]
                         for point in points_by_reference[source_id]]
        points_by_reference[identity] = source_points
        centre = _centre(source_points)
        evidence_rows = evidence_by_geometry.get(identity, [])
        if evidence_rows:
            text = "/".join(sorted(evidence_labels[item["evidence_id"]]
                                   for item in evidence_rows))
            width = pymupdf.get_text_length(text, fontname="helv", fontsize=18)
            page.insert_text((centre[0] - width / 2, centre[1] + 6), text,
                             color=(.31, .16, .59), fontsize=18, fontname="helv", overlay=True)

    for relationship in evidence["cross_view_relationships"]:
        centres = []
        for evidence_id in relationship["source_evidence_ids"]:
            reference = evidence_by_id[evidence_id]["geometry_reference"]
            centres.append(_centre(points_by_reference[reference]))
        if len(centres) > 1:
            ordered = sorted(centres)
            _dashed(page, ordered, (205 / 255, 20 / 255, 155 / 255), 3, dash=10, gap=10)
        position = _centre(centres)
        text = relationship_labels[relationship["relationship_id"]]
        width = pymupdf.get_text_length(text, fontname="helv", fontsize=18)
        page.insert_text((position[0] - width / 2, position[1] + 6), text,
                         color=(205 / 255, 20 / 255, 155 / 255), fontsize=18,
                         fontname="helv", overlay=True)

    panel = pymupdf.Rect(20, 20, source.width - 20, 520)
    page.draw_rect(panel, fill=(1, 1, 1), color=(.1, .1, .1), width=2, overlay=True)
    page.insert_text((38, 52), "HUMAN EVIDENCE REVIEW", color=(0, 0, 0),
                     fontsize=22, fontname="helv", overlay=True)
    page.draw_line((40, 79), (105, 79), color=(0, 0, 0), width=4, overlay=True)
    page.insert_text((120, 85), "ORIGINAL: source drawing", color=(0, 0, 0),
                     fontsize=15, fontname="helv", overlay=True)
    legend = (("LINE", "solid profile"), ("HIDDEN_LINE", "hidden line"),
              ("CIRCLE", "circle"), ("ARC", "arc"), ("CENTERLINE", "centerline"))
    for index, (kind, label) in enumerate(legend):
        y = 98 + index * 26
        color = tuple(channel / 255 for channel in COLORS[kind])
        page.draw_line((40, y + 8), (105, y + 8), color=color, width=5, overlay=True)
        page.insert_text((120, y + 14), f"{kind}: {label}", color=color,
                         fontsize=15, fontname="helv", overlay=True)
    page.insert_text((38, 256), "MAGENTA: derived relationship", color=(205 / 255, 20 / 255, 155 / 255),
                     fontsize=15, fontname="helv", overlay=True)
    page.insert_text((38, 284), "P=primitive  E=evidence  R=relationship", color=(0, 0, 0),
                     fontsize=15, fontname="helv", overlay=True)
    page.insert_text((38, 312), "Human decision is stored separately", color=(0, 0, 0),
                     fontsize=15, fontname="helv", overlay=True)

    entries = []
    for identity, item in sorted(inventory.items()):
        linked = sorted(evidence_by_geometry.get(identity, []), key=lambda row: row["evidence_id"])
        suffix = " ".join(
            f"{evidence_labels[row['evidence_id']]}={evidence['roles'][row['evidence_id']]}"
            for row in linked)
        entries.append(f"{primitive_labels[identity]} {item['primitive_type']} {item['view']} "
                       f"{identity[-8:]} {suffix}".rstrip())
    for identity, row in sorted(derived.items()):
        for evidence_row in sorted(evidence_by_geometry.get(identity, []), key=lambda item: item["evidence_id"]):
            entries.append(f"{evidence_labels[evidence_row['evidence_id']]} {row['geometry_type']} "
                           f"{identity[-8:]} {evidence['roles'][evidence_row['evidence_id']]}")
    for relationship in sorted(evidence["cross_view_relationships"], key=lambda row: row["relationship_id"]):
        entries.append(f"{relationship_labels[relationship['relationship_id']]} "
                       f"{relationship['evidence_type']} {relationship['relationship_id'][-8:]}")
    columns = 3
    rows = math.ceil(len(entries) / columns)
    for index, entry in enumerate(entries):
        column, row = divmod(index, rows)
        page.insert_text((610 + column * 950, 55 + row * 19), entry, color=(.08, .08, .08),
                         fontsize=12, fontname="helv", overlay=True)
    issues = projection.get("issues", []) + evidence.get("issues", [])
    if issues:
        page.draw_rect((30, 455, 1000, 505), fill=(1, .88, .88), color=(.82, 0, 0),
                       width=3, overlay=True)
        page.insert_text((45, 488), f"AMBIGUOUS / UNSUPPORTED: {len(issues)} item(s)",
                         color=(.75, 0, 0), fontsize=22, fontname="helv", overlay=True)

    pixmap = page.get_pixmap(matrix=pymupdf.Matrix(1, 1), colorspace=pymupdf.csRGB, alpha=False)
    pixmap.set_dpi(300, 300)
    result = pixmap.tobytes("png")
    document.close()
    return result


def build_review(png_path, projection_path, evidence_path):
    """Return deterministic overlay bytes and its truth-independent review model."""
    inputs = _read_inputs(png_path, projection_path, evidence_path)
    overlay = _render_overlay(inputs)
    return overlay, _review_model(inputs, _digest(overlay))


def prepare_review(png_path, projection_path, evidence_path, output_dir):
    """Create a review overlay and description without changing machine inputs."""
    output_dir = Path(output_dir)
    overlay_path, review_path = output_dir / "review-overlay.png", output_dir / "review.json"
    if overlay_path.exists() or review_path.exists():
        raise FileExistsError("review output already exists")
    overlay, review = build_review(png_path, projection_path, evidence_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    overlay_path.write_bytes(overlay)
    with review_path.open("xb") as stream:
        stream.write(_json_bytes(review))
    return review


def _validate_review(png_path, projection_path, evidence_path, overlay_path, review_path):
    overlay_path, review_path = Path(overlay_path), Path(review_path)
    actual_overlay, actual_review = build_review(png_path, projection_path, evidence_path)
    saved_overlay = overlay_path.read_bytes()
    saved_review_bytes = review_path.read_bytes()
    saved_review = _load_json_bytes(saved_review_bytes, "human review")
    if saved_overlay != actual_overlay or _digest(saved_overlay) != saved_review.get("overlay", {}).get("sha256"):
        raise ValueError("review overlay cannot be reproduced or its hash mismatches")
    if _canonical(saved_review) != _canonical(actual_review):
        raise ValueError("human review artifact does not match current machine inputs")
    return _read_inputs(png_path, projection_path, evidence_path), saved_review, saved_review_bytes


def _normalize_corrections(corrections, inputs):
    if not isinstance(corrections, list):
        raise ValueError("corrections must be a JSON array")
    evidence = inputs["evidence"]
    evidence_ids = set(evidence.get("roles", {}))
    relationship_ids = {row["relationship_id"] for row in evidence["cross_view_relationships"]}
    inventory = _primitive_inventory(inputs["projection"])
    evidenced_primitives = {row["geometry_reference"] for row in evidence["feature_evidence"]}
    normalized = []
    for correction in corrections:
        if not isinstance(correction, dict) or correction.get("kind") not in CORRECTION_FIELDS:
            raise ValueError("unknown correction kind")
        kind = correction["kind"]
        if set(correction) != CORRECTION_FIELDS[kind]:
            raise ValueError(f"invalid fields for correction kind: {kind}")
        if kind in {"RELABEL_EVIDENCE_ROLE", "REJECT_EVIDENCE"}:
            if correction["evidence_id"] not in evidence_ids:
                raise ValueError("correction references unknown evidence ID")
        if kind == "RELABEL_EVIDENCE_ROLE":
            if correction["role"] not in EVIDENCE_ROLES:
                raise ValueError("correction uses unsupported evidence role")
        elif kind == "MARK_MISSING_EVIDENCE":
            if correction["primitive_id"] not in inventory:
                raise ValueError("correction references unknown primitive ID")
            if correction["primitive_id"] in evidenced_primitives:
                raise ValueError("missing-evidence correction references existing evidence")
            if correction["role"] not in EVIDENCE_ROLES:
                raise ValueError("correction uses unsupported evidence role")
        elif kind == "REJECT_RELATIONSHIP":
            if correction["relationship_id"] not in relationship_ids:
                raise ValueError("correction references unknown relationship ID")
        elif kind == "RECLASSIFY_PRIMITIVE":
            primitive_id = correction["primitive_id"]
            if primitive_id not in inventory:
                raise ValueError("correction references unknown primitive ID")
            if correction["primitive_type"] not in PRIMITIVE_TYPES:
                raise ValueError("correction uses unsupported primitive classification")
        normalized.append(deepcopy(correction))
    normalized.sort(key=_canonical)
    if len({_canonical(row) for row in normalized}) != len(normalized):
        raise ValueError("duplicate correction")
    return normalized


def _confirmation_value(inputs, review, review_bytes, decision, reviewer, corrections):
    if decision not in DECISIONS:
        raise ValueError("invalid human decision")
    if reviewer is not None and (not isinstance(reviewer, str) or not reviewer.strip() or len(reviewer) > 128):
        raise ValueError("reviewer identifier must be a non-empty string of at most 128 characters")
    corrections = _normalize_corrections(corrections, inputs)
    if (decision == "CORRECTED") != bool(corrections):
        raise ValueError("CORRECTED requires corrections and other decisions forbid them")
    sources = deepcopy(review["sources"])
    sources["review_overlay"] = deepcopy(review["overlay"])
    sources["review"] = {"reference": "review.json", "sha256": _digest(review_bytes)}
    return {
        "schema_version": SCHEMA_VERSION,
        "version": VERSION,
        "decision": decision,
        "reviewer": reviewer.strip() if reviewer is not None else None,
        "corrections": corrections,
        "sources": sources,
        "machine_state": deepcopy(review["machine_state"]),
        "provenance": {
            "generator": "solidworks_main_skill.review",
            "decision_source": "EXPLICIT_HUMAN_INPUT",
            "machine_validation_separate": True,
            "feature_ownership": "NOT_INFERRED_BY_SCOPE",
        },
    }


def create_confirmation(png_path, projection_path, evidence_path, overlay_path, review_path,
                        decision, output_path, *, reviewer=None, corrections=None):
    """Persist one explicit decision as a new immutable confirmation artifact."""
    output_path = Path(output_path)
    if output_path.exists():
        raise FileExistsError("confirmation output already exists")
    inputs, review, review_bytes = _validate_review(
        png_path, projection_path, evidence_path, overlay_path, review_path)
    value = _confirmation_value(
        inputs, review, review_bytes, decision, reviewer, [] if corrections is None else corrections)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("xb") as stream:
        stream.write(_json_bytes(value))
    return value


def validate_confirmation(png_path, projection_path, evidence_path, overlay_path, review_path,
                          confirmation_path):
    """Fail closed unless all sources and the deterministic overlay still match."""
    inputs, review, review_bytes = _validate_review(
        png_path, projection_path, evidence_path, overlay_path, review_path)
    value = _load_json_bytes(Path(confirmation_path).read_bytes(), "human confirmation")
    expected_fields = {"schema_version", "version", "decision", "reviewer", "corrections",
                       "sources", "machine_state", "provenance"}
    if set(value) != expected_fields or value.get("schema_version") != SCHEMA_VERSION or value.get("version") != VERSION:
        raise ValueError("invalid human confirmation schema")
    expected = _confirmation_value(inputs, review, review_bytes, value["decision"],
                                   value["reviewer"], value["corrections"])
    if _canonical(value) != _canonical(expected):
        raise ValueError("confirmation artifact does not match current review inputs")
    return value
