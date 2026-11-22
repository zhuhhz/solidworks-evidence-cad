"""B009-001/002/003 bounded FeatureGraph inference from reviewed evidence.

The runtime path has no truth, dataset, ModelingPlan, CAD, or benchmark-answer
imports. It supports one base profile with either an optional adjacent centered
boss and through hole, or one straight through slot.
"""

from copy import deepcopy
from hashlib import sha256
import json
import math
from pathlib import Path

from experiments.three_view_reconstruction.schemas.feature_graph import (
    BaseBlock, Boss, FeatureGraph, Hole, StraightSlot,
)
from experiments.three_view_reconstruction.validation.primitive_matcher import POSITION_TOLERANCE_MM
from solidworks_main_skill.review.human_confirmation import (
    _canonical,
    _read_inputs,
    validate_confirmation,
)


VERSION = "b009.reviewed-feature-graph/0.1"
SCHEMA_VERSION = "b009.feature-graph-candidate/0.1"


def _identity(kind, parameters, evidence_ids):
    payload = [kind, parameters, sorted(evidence_ids)]
    return "inferred-feature." + sha256(_canonical(payload).encode()).hexdigest()[:24]


def _hash_file(path):
    return sha256(Path(path).read_bytes()).hexdigest()


def _inventory(projection, evidence):
    primitives = {}
    for view in ("front", "top", "left"):
        for field, kind in (("visible_segments", "LINE"), ("hidden_segments", "HIDDEN_LINE"),
                            ("circles", "CIRCLE"), ("arcs", "ARC"),
                            ("centerlines", "CENTERLINE")):
            for row in projection["projection_graph"][view][field]:
                if row["primitive_id"] in primitives:
                    raise ValueError("duplicate reviewed primitive identity")
                primitives[row["primitive_id"]] = {"kind": kind, "view": view, "value": row}
    derived = {}
    for row in evidence["derived_primitives"]:
        if row["primitive_id"] in primitives or row["primitive_id"] in derived:
            raise ValueError("duplicate reviewed derived identity")
        derived[row["primitive_id"]] = row
    return primitives, derived


def _apply_corrections(projection, evidence, confirmation):
    reviewed = deepcopy(evidence)
    roles = reviewed["roles"]
    rejected_evidence, rejected_relationships = set(), set()
    primitive_types, targets = {}, set()
    missing = []
    for correction in confirmation["corrections"]:
        kind = correction["kind"]
        if kind in {"RELABEL_EVIDENCE_ROLE", "REJECT_EVIDENCE"}:
            target = ("evidence", correction["evidence_id"])
        elif kind == "REJECT_RELATIONSHIP":
            target = ("relationship", correction["relationship_id"])
        else:
            target = ("primitive", correction["primitive_id"])
        if target in targets:
            raise ValueError("conflicting corrections target the same review item")
        targets.add(target)
        if kind == "RELABEL_EVIDENCE_ROLE":
            roles[correction["evidence_id"]] = correction["role"]
        elif kind == "REJECT_EVIDENCE":
            rejected_evidence.add(correction["evidence_id"])
        elif kind == "MARK_MISSING_EVIDENCE":
            missing.append(deepcopy(correction))
        elif kind == "REJECT_RELATIONSHIP":
            rejected_relationships.add(correction["relationship_id"])
        elif kind == "RECLASSIFY_PRIMITIVE":
            primitive_types[correction["primitive_id"]] = correction["primitive_type"]
        else:
            raise ValueError("unsupported reviewed correction")
    reviewed["feature_evidence"] = [row for row in reviewed["feature_evidence"]
                                    if row["evidence_id"] not in rejected_evidence]
    reviewed["roles"] = {key: value for key, value in roles.items() if key not in rejected_evidence}
    reviewed["cross_view_relationships"] = [
        row for row in reviewed["cross_view_relationships"]
        if row["relationship_id"] not in rejected_relationships]
    return reviewed, primitive_types, missing


def _empty_result(status, issues, inputs, confirmation, confirmation_path):
    messages = []
    for issue in issues:
        if isinstance(issue, str):
            messages.append(issue)
        elif isinstance(issue, dict) and isinstance(issue.get("reason"), str):
            messages.append(issue["reason"])
        else:
            messages.append(_canonical(issue))
    return {
        "schema_version": SCHEMA_VERSION,
        "version": VERSION,
        "status": status,
        "feature_graph": None,
        "feature_nodes": [],
        "feature_provenance": [],
        "reviewed_inputs": {
            "projection_graph_candidate_sha256": sha256(inputs["projection_bytes"]).hexdigest(),
            "feature_evidence_candidate_sha256": sha256(inputs["evidence_bytes"]).hexdigest(),
            "confirmation_sha256": _hash_file(confirmation_path),
            "confirmation_decision": confirmation["decision"],
        },
        "checks": {"feature_count": 0, "unresolved_reference_count": 0,
                   "unconsumed_evidence_count": 0,
                   "correction_count": len(confirmation["corrections"])},
        "issues": sorted(set(messages)),
    }


def _axis_intervals(profiles):
    values = {}
    for profile in profiles:
        intervals = profile.get("coordinate_intervals")
        if not isinstance(intervals, dict) or len(intervals) != 2:
            raise ValueError("reviewed outer profile has invalid coordinate intervals")
        for axis, interval in intervals.items():
            if (axis not in {"x", "y", "z"} or not isinstance(interval, list) or len(interval) != 2
                    or any(type(value) not in (int, float) or not math.isfinite(value) for value in interval)
                    or interval[0] >= interval[1]):
                raise ValueError("reviewed outer profile has invalid coordinate interval")
            values.setdefault(axis, []).append((float(interval[0]), float(interval[1])))
    if set(values) != {"x", "y", "z"} or any(len(rows) != 2 for rows in values.values()):
        raise ValueError("reviewed profiles do not establish three orthographic intervals")
    result = {}
    for axis, rows in values.items():
        if any(max(row[index] for row in rows) - min(row[index] for row in rows)
               > POSITION_TOLERANCE_MM for index in (0, 1)):
            raise ValueError("reviewed profile intervals conflict across views")
        result[axis] = tuple(sum(row[index] for row in rows) / len(rows) for index in (0, 1))
    return result


def _dimensions(intervals):
    return {axis: value[1] - value[0] for axis, value in intervals.items()}


def _reviewed_profile_groups(profile_relations, profiles, evidence_by_id, roles, primitives):
    """Bind each explicit correspondence to exactly one profile in every view."""
    profiles_by_id = {row["primitive_id"]: row for row in profiles}
    if len(profiles_by_id) != len(profiles):
        raise ValueError("duplicate reviewed profile identity")
    groups = []
    claimed_profiles = set()
    for relation in profile_relations:
        profile_ids = set(relation["source_profile_ids"])
        if len(profile_ids) != 3 or not profile_ids <= set(profiles_by_id):
            raise ValueError("profile correspondence is incomplete")
        selected = [profiles_by_id[identity] for identity in profile_ids]
        if {row["view"] for row in selected} != {"front", "top", "left"}:
            raise ValueError("profile correspondence does not cover three orthographic views")
        if claimed_profiles & profile_ids:
            raise ValueError("one profile belongs to multiple correspondence groups")
        claimed_profiles.update(profile_ids)
        evidence_ids = set(relation["source_evidence_ids"])
        if not evidence_ids or not evidence_ids <= set(evidence_by_id):
            raise ValueError("profile correspondence evidence is missing")
        profile_sources = {source for row in selected for source in row["source_primitive_ids"]}
        for evidence_id in evidence_ids:
            evidence = evidence_by_id[evidence_id]
            if (roles[evidence_id] != "VISIBLE_PROFILE_EDGE"
                    or evidence["geometry_reference"] not in profile_sources
                    or primitives[evidence["geometry_reference"]]["kind"] != "LINE"):
                raise ValueError("profile evidence does not support its explicit correspondence")
        groups.append({
            "relation": relation,
            "profiles": selected,
            "evidence_ids": evidence_ids,
            "intervals": _axis_intervals(selected),
        })
    if claimed_profiles != set(profiles_by_id):
        raise ValueError("reviewed profile remains outside explicit correspondence")
    return groups


def _contains(outer, inner):
    return outer[0] <= inner[0] + POSITION_TOLERANCE_MM and outer[1] >= inner[1] - POSITION_TOLERANCE_MM


def _centered(outer, inner):
    return abs(sum(outer) / 2 - sum(inner) / 2) <= POSITION_TOLERANCE_MM


def _profile_semantics(groups):
    """Identify base/boss from explicit adjacent extrusion intervals, never size/order."""
    if len(groups) == 1:
        return groups[0], None
    if len(groups) != 2:
        raise ValueError("bounded inference supports one base and at most one boss profile")
    ordered = sorted(groups, key=lambda row: (row["intervals"]["z"][0],
                                               row["relation"]["relationship_id"]))
    base, boss = ordered
    if abs(base["intervals"]["z"][1] - boss["intervals"]["z"][0]) > POSITION_TOLERANCE_MM:
        raise ValueError("Base/Boss extrusion intervals are not explicitly adjacent")
    if not all(_contains(base["intervals"][axis], boss["intervals"][axis])
               for axis in ("x", "y")):
        raise ValueError("Boss profile is not supported by the base footprint")
    if not all(_centered(base["intervals"][axis], boss["intervals"][axis])
               for axis in ("x", "y")):
        raise ValueError("existing Boss schema cannot represent the reviewed relative placement")
    return base, boss


def _constant_and_span(line):
    dx = abs(float(line["x2"]) - float(line["x1"]))
    dy = abs(float(line["y2"]) - float(line["y1"]))
    if dx > POSITION_TOLERANCE_MM and dy <= POSITION_TOLERANCE_MM:
        return "X", (float(line["y1"]) + float(line["y2"])) / 2, sorted(
            (float(line["x1"]), float(line["x2"])))
    if dy > POSITION_TOLERANCE_MM and dx <= POSITION_TOLERANCE_MM:
        return "Y", (float(line["x1"]) + float(line["x2"])) / 2, sorted(
            (float(line["y1"]), float(line["y2"])))
    raise ValueError("reviewed support line is not axis aligned")


def _slot_candidate(inputs, confirmation, confirmation_path, reviewed, primitives, derived,
                    evidence_by_id, relationships, base_group, base, base_id, provenance_specs,
                    groups):
    closed = [row for row in relationships if row["evidence_type"] == "CLOSED_LINE_ARC_BOUNDARY"]
    support = [row for row in relationships
               if row["evidence_type"] == "LINE_ARC_BOUNDARY_ORTHOGONAL_SUPPORT"]
    boundaries = [row for row in derived.values() if row["geometry_type"] == "LINE_ARC_PROFILE"]
    if len(closed) != 1 or len(support) != 1 or len(boundaries) != 1:
        return _empty_result("AMBIGUOUS", ["one explicit closed LINE/ARC slot support chain is required"],
                             inputs, confirmation, confirmation_path)
    boundary, closed_relation, support_relation = boundaries[0], closed[0], support[0]
    if (closed_relation.get("source_boundary_id") != boundary["primitive_id"]
            or closed_relation.get("relationship") != "ENDPOINT_CLOSED_AND_TANGENT"
            or support_relation.get("relationship") != "SHARED_EXTREMA_AND_CENTERLINES"):
        return _empty_result("AMBIGUOUS", ["reviewed LINE/ARC closure is inconsistent"],
                             inputs, confirmation, confirmation_path)
    line_ids, arc_ids = set(boundary["line_primitive_ids"]), set(boundary["arc_primitive_ids"])
    if (len(line_ids) != 2 or len(arc_ids) != 2
            or line_ids | arc_ids != set(boundary["source_primitive_ids"])):
        return _empty_result("AMBIGUOUS", ["slot boundary requires exactly two LINE and two ARC primitives"],
                             inputs, confirmation, confirmation_path)
    boundary_evidence_ids = set(closed_relation["source_evidence_ids"])
    support_evidence_ids = set(support_relation["source_evidence_ids"])
    if (not boundary_evidence_ids or not support_evidence_ids
            or not support_evidence_ids <= set(evidence_by_id)
            or boundary_evidence_ids - support_evidence_ids):
        return _empty_result("AMBIGUOUS", ["slot evidence references are incomplete"],
                             inputs, confirmation, confirmation_path)
    role_rows = {}
    for evidence_id in support_evidence_ids:
        role_rows.setdefault(reviewed["roles"][evidence_id], []).append(evidence_by_id[evidence_id])
    expected_counts = {"LINE_ARC_BOUNDARY_EDGE": 2, "ARC_BOUNDARY": 2,
                       "CENTERLINE_SUPPORT": 3, "ORTHOGONAL_HIDDEN_PAIR": 2}
    if {role: len(rows) for role, rows in role_rows.items()} != expected_counts:
        return _empty_result("AMBIGUOUS", ["slot evidence roles are incomplete or ambiguous"],
                             inputs, confirmation, confirmation_path)
    if boundary_evidence_ids != {
            row["evidence_id"] for role in ("LINE_ARC_BOUNDARY_EDGE", "ARC_BOUNDARY")
            for row in role_rows[role]}:
        return _empty_result("AMBIGUOUS", ["closed boundary evidence ownership is inconsistent"],
                             inputs, confirmation, confirmation_path)
    for row in role_rows["LINE_ARC_BOUNDARY_EDGE"]:
        if row["geometry_reference"] not in line_ids or primitives[row["geometry_reference"]]["kind"] != "LINE":
            return _empty_result("AMBIGUOUS", ["slot LINE evidence is inconsistent"],
                                 inputs, confirmation, confirmation_path)
    for row in role_rows["ARC_BOUNDARY"]:
        if row["geometry_reference"] not in arc_ids or primitives[row["geometry_reference"]]["kind"] != "ARC":
            return _empty_result("AMBIGUOUS", ["slot ARC evidence is inconsistent"],
                                 inputs, confirmation, confirmation_path)

    lines = [primitives[identity]["value"] for identity in sorted(line_ids)]
    line_geometry = [_constant_and_span(line) for line in lines]
    if len({row[0] for row in line_geometry}) != 1:
        return _empty_result("AMBIGUOUS", ["slot boundary LINE primitives are not parallel"],
                             inputs, confirmation, confirmation_path)
    major_axis = line_geometry[0][0]
    intervals = boundary.get("coordinate_intervals")
    if not isinstance(intervals, dict) or set(intervals) != {"x", "y"}:
        return _empty_result("AMBIGUOUS", ["slot boundary intervals are missing"],
                             inputs, confirmation, confirmation_path)
    major_key, minor_key = ("x", "y") if major_axis == "X" else ("y", "x")
    overall_length = float(intervals[major_key][1]) - float(intervals[major_key][0])
    width = float(intervals[minor_key][1]) - float(intervals[minor_key][0])
    arcs = [primitives[identity]["value"] for identity in sorted(arc_ids)]
    radii = [float(arc["radius"]) for arc in arcs]
    if (any(not math.isfinite(value) or value <= 0 for value in (overall_length, width, *radii))
            or max(radii) - min(radii) > POSITION_TOLERANCE_MM
            or abs(2 * sum(radii) / len(radii) - width) > POSITION_TOLERANCE_MM):
        return _empty_result("AMBIGUOUS", ["observed slot radius and width are inconsistent"],
                             inputs, confirmation, confirmation_path)
    radius = sum(radii) / len(radii)
    center_view = {axis: sum(map(float, interval)) / 2 for axis, interval in intervals.items()}
    center_x = center_view["x"] - sum(base_group["intervals"]["x"]) / 2
    center_y = center_view["y"] - sum(base_group["intervals"]["y"]) / 2

    pairs = [derived.get(row["geometry_reference"])
             for row in role_rows["ORTHOGONAL_HIDDEN_PAIR"]]
    if (any(pair is None or pair["geometry_type"] != "HIDDEN_LINE_PAIR"
            or pair["relationship"] != "PARALLEL_FULL_SPAN_MATCHED_TO_BOUNDARY_EXTREMA"
            for pair in pairs) or {pair["view"] for pair in pairs} != {"top", "left"}):
        return _empty_result("AMBIGUOUS", ["orthogonal hidden support is incomplete"],
                             inputs, confirmation, confirmation_path)
    expected_derived = {boundary["primitive_id"], *(pair["primitive_id"] for pair in pairs)}
    if set(support_relation.get("source_derived_ids", [])) != expected_derived:
        return _empty_result("AMBIGUOUS", ["slot orthogonal support references are inconsistent"],
                             inputs, confirmation, confirmation_path)
    for pair in pairs:
        expected_offsets = intervals["x" if pair["view"] == "top" else "y"]
        if any(abs(float(pair["offsets_mm"][index]) - float(expected_offsets[index]))
               > POSITION_TOLERANCE_MM for index in (0, 1)):
            return _empty_result("AMBIGUOUS", ["hidden support does not match slot extrema"],
                                 inputs, confirmation, confirmation_path)
        coordinate = "y" if pair["view"] == "top" else "x"
        for source in pair["source_primitive_ids"]:
            line = primitives.get(source)
            if line is None or line["kind"] != "HIDDEN_LINE":
                return _empty_result("AMBIGUOUS", ["hidden support source is missing"],
                                     inputs, confirmation, confirmation_path)
            span = sorted((float(line["value"][coordinate + "1"]),
                           float(line["value"][coordinate + "2"])))
            if any(abs(span[index] - base_group["intervals"]["z"][index])
                   > POSITION_TOLERANCE_MM for index in (0, 1)):
                return _empty_result("AMBIGUOUS", ["slot hidden support is not full depth"],
                                     inputs, confirmation, confirmation_path)

    centerlines = {}
    for row in role_rows["CENTERLINE_SUPPORT"]:
        item = primitives.get(row["geometry_reference"])
        if item is None or item["kind"] != "CENTERLINE" or item["view"] in centerlines:
            return _empty_result("AMBIGUOUS", ["slot centerline support is inconsistent"],
                                 inputs, confirmation, confirmation_path)
        centerlines[item["view"]] = item["value"]
    if set(centerlines) != {"front", "top", "left"}:
        return _empty_result("AMBIGUOUS", ["slot centerline views are incomplete"],
                             inputs, confirmation, confirmation_path)
    if major_axis != "X":
        return _empty_result("UNSUPPORTED_FEATURE",
                             ["bounded B009-003 rule currently supports an observed X-axis slot"],
                             inputs, confirmation, confirmation_path)
    center_checks = {"front": ("X", center_view["y"], intervals["x"]),
                     "top": ("Y", center_view["x"], base_group["intervals"]["z"]),
                     "left": ("X", center_view["y"], base_group["intervals"]["z"])}
    for view, line in centerlines.items():
        orientation, constant, span = _constant_and_span(line)
        expected_orientation, expected_constant, expected_span = center_checks[view]
        if (orientation != expected_orientation
                or abs(constant - expected_constant) > POSITION_TOLERANCE_MM
                or span[0] > float(expected_span[0]) + POSITION_TOLERANCE_MM
                or span[1] < float(expected_span[1]) - POSITION_TOLERANCE_MM):
            return _empty_result("AMBIGUOUS", ["slot centerline does not establish a common center"],
                                 inputs, confirmation, confirmation_path)

    parameters = {"overall_length_mm": overall_length, "width_mm": width,
                  "radius_mm": radius, "center_x_mm": center_x, "center_y_mm": center_y,
                  "major_axis": major_axis, "through": True}
    slot_id = _identity("STRAIGHT_SLOT", parameters, support_evidence_ids)
    slot = StraightSlot(overall_length, width, radius, center_x, center_y, major_axis, True,
                        feature_id=slot_id, source_evidence_ids=sorted(support_evidence_ids),
                        dependencies=[base_id])
    graph = FeatureGraph(base, slots=[slot], status="NOT_VALIDATED")
    provenance_specs[slot_id] = {
        "rule": "REVIEWED_CLOSED_LINE_ARC_AND_FULL_DEPTH_ORTHOGONAL_SUPPORT_TO_STRAIGHT_SLOT",
        "relationship_ids": [closed_relation["relationship_id"],
                             support_relation["relationship_id"]],
    }
    used = support_evidence_ids | {identity for group in groups for identity in group["evidence_ids"]}
    return _graph_result("INFERRED", [], graph, provenance_specs, inputs, confirmation,
                         confirmation_path, set(evidence_by_id) - used)


def _candidate(inputs, confirmation, confirmation_path):
    projection, evidence = inputs["projection"], inputs["evidence"]
    if confirmation["decision"] == "REJECTED":
        raise ValueError("REJECTED human confirmation blocks FeatureGraph inference")
    if confirmation["decision"] not in {"CONFIRMED", "CORRECTED"}:
        raise ValueError("reviewed evidence requires CONFIRMED or CORRECTED decision")
    if evidence["status"] != "INFERRED":
        return _empty_result("UNSUPPORTED_FEATURE", evidence.get("issues", ["evidence is unsupported"]),
                             inputs, confirmation, confirmation_path)
    reviewed, type_corrections, missing = _apply_corrections(projection, evidence, confirmation)
    if missing:
        return _empty_result("NOT_VALIDATED", ["human marked missing evidence"],
                             inputs, confirmation, confirmation_path)
    primitives, derived = _inventory(projection, reviewed)
    for primitive_id, corrected_type in type_corrections.items():
        primitives[primitive_id]["kind"] = corrected_type
    evidence_by_id = {row["evidence_id"]: row for row in reviewed["feature_evidence"]}
    if len(evidence_by_id) != len(reviewed["feature_evidence"]):
        raise ValueError("duplicate reviewed evidence identity")
    relationships = reviewed["cross_view_relationships"]
    relationship_by_id = {row["relationship_id"]: row for row in relationships}
    if len(relationship_by_id) != len(relationships):
        raise ValueError("duplicate reviewed relationship identity")
    slot_mode = any(row["evidence_type"] == "LINE_ARC_BOUNDARY_ORTHOGONAL_SUPPORT"
                    for row in relationships)
    allowed_roles = {"VISIBLE_PROFILE_EDGE", "ORTHOGONAL_HIDDEN_PAIR"}
    allowed_roles.update(
        {"LINE_ARC_BOUNDARY_EDGE", "ARC_BOUNDARY", "CENTERLINE_SUPPORT"}
        if slot_mode else {"OBSERVED_CIRCLE"})
    unsupported_roles = {reviewed["roles"][identity] for identity in evidence_by_id} - allowed_roles
    if unsupported_roles:
        return _empty_result("UNSUPPORTED_FEATURE",
                             ["bounded B009-001/002/003 rules do not support roles: "
                              + ",".join(sorted(unsupported_roles))],
                             inputs, confirmation, confirmation_path)

    profile_relations = [row for row in relationships
                         if row["evidence_type"] == "ORTHOGONAL_PROFILE_CORRESPONDENCE"]
    profiles = [row for row in derived.values() if row["geometry_type"] == "VISIBLE_PROFILE"]
    try:
        groups = _reviewed_profile_groups(profile_relations, profiles, evidence_by_id,
                                           reviewed["roles"], primitives)
        base_group, boss_group = _profile_semantics(groups)
    except ValueError as exc:
        state = "AMBIGUOUS" if len(profile_relations) <= 2 else "UNSUPPORTED_FEATURE"
        return _empty_result(state, [str(exc)], inputs, confirmation, confirmation_path)

    base_dimensions = _dimensions(base_group["intervals"])
    base_evidence_ids = base_group["evidence_ids"]
    base_parameters = {"width_mm": base_dimensions["x"], "height_mm": base_dimensions["y"],
                       "depth_mm": base_dimensions["z"]}
    base_id = _identity("BASE_BLOCK", base_parameters, base_evidence_ids)
    base = BaseBlock(base_dimensions["x"], base_dimensions["y"], base_dimensions["z"], feature_id=base_id,
                     source_evidence_ids=sorted(base_evidence_ids), dependencies=[])
    boss = None
    profile_specs = {
        base_id: {
            "rule": "ORTHOGONAL_OUTER_PROFILE_TO_BASE_BLOCK",
            "relationship_ids": [base_group["relation"]["relationship_id"]],
        }
    }
    feature_context_id = base_id
    if boss_group is not None:
        boss_dimensions = _dimensions(boss_group["intervals"])
        boss_evidence_ids = boss_group["evidence_ids"]
        boss_parameters = {"width_mm": boss_dimensions["x"], "height_mm": boss_dimensions["y"],
                           "depth_mm": boss_dimensions["z"], "centered": True}
        boss_id = _identity("BOSS", boss_parameters, boss_evidence_ids)
        boss = Boss(boss_dimensions["x"], boss_dimensions["y"], boss_dimensions["z"], True,
                    feature_id=boss_id, source_evidence_ids=sorted(boss_evidence_ids),
                    dependencies=[base_id])
        feature_context_id = boss_id
        profile_specs[boss_id] = {
            "rule": "ADJACENT_CENTERED_ORTHOGONAL_PROFILE_TO_BOSS",
            "relationship_ids": [boss_group["relation"]["relationship_id"]],
        }

    slot_support_relations = [
        row for row in relationships
        if row["evidence_type"] == "LINE_ARC_BOUNDARY_ORTHOGONAL_SUPPORT"]
    if slot_support_relations:
        if boss is not None or any(row["evidence_type"] == "CIRCLE_ORTHOGONAL_HIDDEN_PAIR_SUPPORT"
                                   for row in relationships):
            return _empty_result("UNSUPPORTED_FEATURE",
                                 ["bounded slot inference does not support mixed feature ownership"],
                                 inputs, confirmation, confirmation_path)
        return _slot_candidate(inputs, confirmation, confirmation_path, reviewed, primitives,
                               derived, evidence_by_id, relationships, base_group, base, base_id,
                               profile_specs, groups)

    hole_relations = [row for row in relationships
                      if row["evidence_type"] == "CIRCLE_ORTHOGONAL_HIDDEN_PAIR_SUPPORT"]
    if len(hole_relations) != 1:
        graph = FeatureGraph(base, bosses=[boss] if boss else [], status="AMBIGUOUS")
        return _graph_result("AMBIGUOUS", ["one through-hole support relationship is required"],
                             graph, profile_specs, inputs, confirmation, confirmation_path,
                             set(evidence_by_id) - {identity for group in groups
                                                   for identity in group["evidence_ids"]})
    hole_relation = hole_relations[0]
    hole_evidence_ids = set(hole_relation["source_evidence_ids"])
    if not hole_evidence_ids or not hole_evidence_ids <= set(evidence_by_id):
        graph = FeatureGraph(base, bosses=[boss] if boss else [], status="AMBIGUOUS")
        return _graph_result("AMBIGUOUS", ["through-hole evidence is missing"], graph,
                             profile_specs, inputs, confirmation, confirmation_path,
                             set(evidence_by_id) - {identity for group in groups
                                                   for identity in group["evidence_ids"]})
    role_rows = {}
    for evidence_id in hole_evidence_ids:
        role_rows.setdefault(reviewed["roles"][evidence_id], []).append(evidence_by_id[evidence_id])
    if (set(role_rows) != {"OBSERVED_CIRCLE", "ORTHOGONAL_HIDDEN_PAIR"}
            or len(role_rows["OBSERVED_CIRCLE"]) != 1 or len(role_rows["ORTHOGONAL_HIDDEN_PAIR"]) != 2):
        graph = FeatureGraph(base, bosses=[boss] if boss else [], status="AMBIGUOUS")
        return _graph_result("AMBIGUOUS", ["through-hole evidence roles are incomplete"], graph,
                             profile_specs, inputs, confirmation, confirmation_path,
                             set(evidence_by_id) - {identity for group in groups
                                                   for identity in group["evidence_ids"]})
    circle_evidence = role_rows["OBSERVED_CIRCLE"][0]
    circle_item = primitives.get(circle_evidence["geometry_reference"])
    if circle_item is None or circle_item["kind"] != "CIRCLE" or circle_item["view"] != "front":
        return _empty_result("UNSUPPORTED_FEATURE", ["bounded rule requires one front-view circle"],
                             inputs, confirmation, confirmation_path)
    for row in role_rows["ORTHOGONAL_HIDDEN_PAIR"]:
        pair = derived.get(row["geometry_reference"])
        if (pair is None or pair["geometry_type"] != "HIDDEN_LINE_PAIR"
                or pair["view"] not in {"top", "left"}
                or pair["relationship"] != "PARALLEL_FULL_SPAN_MATCHED_TO_BOUNDARY_EXTREMA"
                or any(source not in primitives or primitives[source]["kind"] != "HIDDEN_LINE"
                       for source in pair["source_primitive_ids"])):
            return _empty_result("AMBIGUOUS", ["orthogonal hidden-line support is incomplete"],
                                 inputs, confirmation, confirmation_path)
    if {row["view"] for row in role_rows["ORTHOGONAL_HIDDEN_PAIR"]} != {"top", "left"}:
        return _empty_result("AMBIGUOUS", ["orthogonal hidden-line views are incomplete"],
                             inputs, confirmation, confirmation_path)
    total_depth = (base_group["intervals"]["z"][0],
                   (boss_group or base_group)["intervals"]["z"][1])
    for row in role_rows["ORTHOGONAL_HIDDEN_PAIR"]:
        pair = derived[row["geometry_reference"]]
        coordinate = "y" if pair["view"] == "top" else "x"
        for source in pair["source_primitive_ids"]:
            line = primitives[source]["value"]
            span = sorted((float(line[coordinate + "1"]), float(line[coordinate + "2"])))
            if any(abs(span[index] - total_depth[index]) > POSITION_TOLERANCE_MM
                   for index in (0, 1)):
                return _empty_result("AMBIGUOUS",
                                     ["hidden-line support does not span the reviewed feature stack"],
                                     inputs, confirmation, confirmation_path)
    circle = circle_item["value"]
    if boss_group is not None:
        radius = float(circle["diameter"]) / 2
        circle_bounds = {
            "x": (float(circle["x"]) - radius, float(circle["x"]) + radius),
            "y": (float(circle["y"]) - radius, float(circle["y"]) + radius),
        }
        if not all(_contains(boss_group["intervals"][axis], circle_bounds[axis])
                   for axis in ("x", "y")):
            return _empty_result("AMBIGUOUS",
                                 ["through-hole ownership is not enclosed by the terminal Boss profile"],
                                 inputs, confirmation, confirmation_path)
    # ProjectionGraph uses a lower-left view origin; FeatureGraph uses the
    # existing MODEL_CENTERED_XY_MM coordinate contract.
    centre_x = float(circle["x"]) - sum(base_group["intervals"]["x"]) / 2
    centre_y = float(circle["y"]) - sum(base_group["intervals"]["y"]) / 2
    hole_parameters = {"diameter_mm": float(circle["diameter"]), "center_x_mm": centre_x,
                       "center_y_mm": centre_y, "axis": "Z", "through": True}
    hole_id = _identity("THROUGH_HOLE", hole_parameters, hole_evidence_ids)
    hole = Hole(float(circle["diameter"]), centre_x, centre_y, "Z", True,
                feature_id=hole_id, source_evidence_ids=sorted(hole_evidence_ids),
                dependencies=[feature_context_id])
    graph = FeatureGraph(base, holes=[hole], bosses=[boss] if boss else [], status="NOT_VALIDATED")
    profile_specs[hole_id] = {
        "rule": "CIRCLE_AND_ORTHOGONAL_HIDDEN_SUPPORT_TO_THROUGH_HOLE",
        "relationship_ids": [hole_relation["relationship_id"]],
    }
    used = hole_evidence_ids | {identity for group in groups for identity in group["evidence_ids"]}
    return _graph_result("INFERRED", [], graph, profile_specs, inputs, confirmation,
                         confirmation_path, set(evidence_by_id) - used)


def _graph_result(status, issues, graph, provenance_specs, inputs, confirmation,
                  confirmation_path, unconsumed):
    nodes = graph.to_feature_nodes()
    provenance = []
    for node in nodes:
        spec = provenance_specs[node.feature_id]
        provenance.append({
            "feature_id": node.feature_id,
            "rule": spec["rule"],
            "source_evidence_ids": sorted(node.source_evidence_ids),
            "source_relationship_ids": sorted(spec["relationship_ids"]),
            "source": "REVIEWED_VISUAL_EVIDENCE",
            "confirmation_decision": confirmation["decision"],
        })
    result = _empty_result(status, issues, inputs, confirmation, confirmation_path)
    result.update(feature_graph=graph.to_dict(),
                  feature_nodes=sorted((node.to_dict() for node in nodes), key=lambda row: row["feature_id"]),
                  feature_provenance=sorted(provenance, key=lambda row: row["feature_id"]))
    result["checks"].update(feature_count=len(nodes),
                            unconsumed_evidence_count=len(unconsumed))
    if status == "INFERRED" and unconsumed:
        result["status"] = "NOT_VALIDATED"
        result["issues"] = ["reviewed evidence remains unconsumed"]
    return result


def infer_feature_graph(png_path, projection_path, evidence_path, overlay_path, review_path,
                        confirmation_path):
    """Validate the human gate, then infer a bounded B009-001/002/003 candidate."""
    confirmation = validate_confirmation(
        png_path, projection_path, evidence_path, overlay_path, review_path, confirmation_path)
    inputs = _read_inputs(png_path, projection_path, evidence_path)
    return _candidate(inputs, confirmation, confirmation_path)


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--png", required=True)
    parser.add_argument("--projection", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--overlay", required=True)
    parser.add_argument("--review", required=True)
    parser.add_argument("--confirmation", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        result = infer_feature_graph(args.png, args.projection, args.evidence, args.overlay,
                                     args.review, args.confirmation)
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(result, sort_keys=True, indent=2, allow_nan=False) + "\n")
    except (ValueError, OSError, TypeError, KeyError) as exc:
        parser.exit(2, str(exc) + "\n")


if __name__ == "__main__":
    main()
