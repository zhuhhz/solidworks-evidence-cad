"""R003 bounded, source-backed shape reasoning; no CAD execution or truth input."""

from __future__ import annotations

from collections import Counter, defaultdict
from hashlib import sha256
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

from experiments.three_view_reconstruction.schemas.feature_evidence import FeatureEvidence
from experiments.three_view_reconstruction.schemas.feature_graph import BaseBlock, Boss, FeatureGraph, Hole
from experiments.three_view_reconstruction.schemas.projection_graph import ProjectionGraph
from experiments.three_view_reconstruction.schemas.view_geometry import Arc, Circle, LineSegment, ViewGeometry
from solidworks_main_skill.review.human_confirmation import validate_confirmation


CAPABILITY_SCOPE = "R003_BOUNDED_CANONICAL_COMPOSITION"
INPUT_DOMAIN = "CONNECTED_SINGLE_SOLID_PART"
VERSION = "cad-reasoning-v1.r003/1.0"
GENERATION_TRUTH_READS = 0
POSITION_TOLERANCE_MM = 0.10
PREDICATES = (
    "P01_TWO_RECTANGULAR_PROFILE_GROUPS", "P02_THREE_VIEW_CORRESPONDENCE",
    "P03_STRICT_CENTERED_XY_CONTAINMENT", "P04_DIRECTED_Z_ADJACENCY",
    "P05_POSITIVE_COMPOSITE_Z_SPAN", "P06_ONE_FRONT_CIRCLE",
    "P07_TOP_HIDDEN_PAIR", "P08_LEFT_HIDDEN_PAIR",
    "P09_DIAMETER_CONSISTENCY", "P10_AXIS_CONSISTENCY",
    "P11_HIDDEN_LINE_QUALIFICATION", "P12_FOUR_RAW_WALL_ENDPOINTS",
    "P13_FULL_COMPOSITE_HOLE_SPAN",
)
_AXES = {"front": ("x", "y"), "top": ("x", "z"), "left": ("z", "y")}


class _GateError(ValueError):
    def __init__(self, predicate: str, outcome: str, reason: str, ids: Sequence[str] = ()):
        super().__init__(reason)
        self.predicate, self.outcome, self.ids = predicate, outcome, sorted(set(ids))


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _identity(kind: str, parameters: Mapping[str, Any], evidence_ids: Sequence[str]) -> str:
    digest = sha256(_canonical([kind, parameters, sorted(set(evidence_ids))]).encode()).hexdigest()[:24]
    return f"inferred-feature.{digest}"


def _report(domain: str | None) -> dict[str, Any]:
    return {
        "version": VERSION,
        "capability_scope": CAPABILITY_SCOPE,
        "status": "NOT_VALIDATED",
        "outcome": "NOT_VALIDATED",
        "input_domain": domain,
        "input_domain_status": "UNKNOWN",
        "artifact_validation_status": "NOT_VALIDATED",
        "predicate_results": [
            {"name": name, "status": "NOT_EVALUATED", "source_ids": [],
             "tolerance_mm": POSITION_TOLERANCE_MM, "reason": None}
            for name in PREDICATES
        ],
        "evidence_ledger": [],
        "evidence_ledger_summary": {"unconsumed_relevant": 0, "unresolved_discriminators": 0},
        "shape_status": "NOT_VALIDATED",
        "shape_survivors": [],
        "shape_exclusions": [],
        "history_status": "NOT_EVALUATED",
        "surviving_histories": [],
        "history_equivalence": None,
        "canonical_status": "BLOCKED",
        "canonical_candidates": [],
        "selected_candidate": None,
        "selection_is_original_history_claim": False,
        "feature_graph": None,
        "feature_nodes": [],
        "feature_provenance": [],
        "reviewed_inputs": {"confirmation_decision": None},
        "generation_truth_reads": GENERATION_TRUTH_READS,
        "checks": {"feature_count": 0, "unresolved_reference_count": 0,
                   "unconsumed_evidence_count": 0, "generation_truth_reads": GENERATION_TRUTH_READS},
        "ask_human": False,
        "failure_reason": None,
    }


def _fail(report: dict[str, Any], outcome: str, reason: str, *, ask: bool = False) -> tuple[None, dict]:
    report["status"] = report["outcome"] = outcome
    report["failure_reason"] = reason
    report["ask_human"] = ask
    return None, report


def _gate(report: dict[str, Any], name: str, ids: Sequence[str], check: bool,
          reason: str, *, outcome: str = "CONTRADICTORY") -> None:
    row = next(item for item in report["predicate_results"] if item["name"] == name)
    row["source_ids"] = sorted(set(str(value) for value in ids))
    if not check:
        row["status"], row["reason"] = ("NOT_VALIDATED" if outcome == "NOT_VALIDATED" else "FAIL"), reason
        raise _GateError(name, outcome, reason, row["source_ids"])
    row["status"], row["reason"] = "PASS", None


def _interval(row: Mapping[str, Any], axis: str, gate: str) -> tuple[float, float]:
    values = row.get("coordinate_intervals", {}).get(axis)
    if (not isinstance(values, (list, tuple)) or len(values) != 2
            or any(type(value) not in (int, float) or not math.isfinite(value) for value in values)
            or values[1] - values[0] <= POSITION_TOLERANCE_MM):
        raise _GateError(gate, "NOT_VALIDATED", f"invalid {axis} interval", [str(row.get("primitive_id"))])
    return float(values[0]), float(values[1])


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= POSITION_TOLERANCE_MM


def _strict_centered(a: Mapping[str, tuple[float, float]],
                     b: Mapping[str, tuple[float, float]]) -> bool:
    return all(
        b[axis][0] - a[axis][0] > POSITION_TOLERANCE_MM
        and a[axis][1] - b[axis][1] > POSITION_TOLERANCE_MM
        and _close(sum(a[axis]) / 2, sum(b[axis]) / 2)
        for axis in ("x", "y")
    )


def _directed_adjacent(lower: tuple[float, float], upper: tuple[float, float]) -> bool:
    return (lower[1] - lower[0] > POSITION_TOLERANCE_MM
            and upper[1] - upper[0] > POSITION_TOLERANCE_MM
            and upper[0] > lower[0] and _close(lower[1], upper[0]))


def _raw_inventory(projection: ProjectionGraph) -> dict[str, tuple[str, str, Any]]:
    values: dict[str, tuple[str, str, Any]] = {}
    for name in _AXES:
        view = getattr(projection, name)
        if not isinstance(view, ViewGeometry) or view.name != name:
            raise _GateError(PREDICATES[0], "NOT_VALIDATED", "invalid canonical view")
        for field, kind in (("visible_segments", "LINE"), ("hidden_segments", "HIDDEN_LINE"),
                            ("circles", "CIRCLE"), ("arcs", "ARC"),
                            ("centerlines", "CENTERLINE")):
            for item in getattr(view, field):
                identity = item.primitive_id
                if not isinstance(identity, str) or not identity or identity in values:
                    raise _GateError(PREDICATES[0], "NOT_VALIDATED", "duplicate or empty raw ID")
                values[identity] = name, kind, item
    return values


def _rect(row: Mapping[str, Any], raw: Mapping[str, tuple[str, str, Any]]) -> dict[str, Any]:
    gate = PREDICATES[0]
    name = row.get("view")
    identity = str(row.get("primitive_id"))
    if name not in _AXES or row.get("geometry_type") != "VISIBLE_PROFILE":
        raise _GateError(gate, "NOT_VALIDATED", "invalid derived profile", [identity])
    sources = row.get("source_primitive_ids")
    if not isinstance(sources, list) or len(sources) != 4 or len(set(sources)) != 4:
        raise _GateError(gate, "NOT_VALIDATED", "rectangle requires four distinct raw edges", [identity])
    axes = _AXES[name]
    bounds = {axis: _interval(row, axis, gate) for axis in axes}
    if set(row.get("coordinate_intervals", {})) != set(axes):
        raise _GateError(gate, "NOT_VALIDATED", "profile axes mismatch", [identity])
    expected = [
        (bounds[axes[0]][0], bounds[axes[1]][0], bounds[axes[0]][1], bounds[axes[1]][0]),
        (bounds[axes[0]][1], bounds[axes[1]][0], bounds[axes[0]][1], bounds[axes[1]][1]),
        (bounds[axes[0]][1], bounds[axes[1]][1], bounds[axes[0]][0], bounds[axes[1]][1]),
        (bounds[axes[0]][0], bounds[axes[1]][1], bounds[axes[0]][0], bounds[axes[1]][0]),
    ]
    remaining = set(range(4))
    overhang: list[str] = []
    for source in sources:
        if source not in raw or raw[source][:2] != (name, "LINE"):
            raise _GateError(gate, "NOT_VALIDATED", "profile raw edge missing/wrong view/style", [identity, source])
        line = raw[source][2]
        coords = (float(line.x1), float(line.y1), float(line.x2), float(line.y2))
        matches = []
        for index in remaining:
            x1, y1, x2, y2 = expected[index]
            if _close(y1, y2):
                constant = _close(coords[1], y1) and _close(coords[3], y1)
                observed, required = sorted((coords[0], coords[2])), sorted((x1, x2))
            else:
                constant = _close(coords[0], x1) and _close(coords[2], x1)
                observed, required = sorted((coords[1], coords[3])), sorted((y1, y2))
            if constant and observed[0] <= required[0] + POSITION_TOLERANCE_MM and observed[1] >= required[1] - POSITION_TOLERANCE_MM:
                matches.append(index)
                if observed[0] < required[0] - POSITION_TOLERANCE_MM or observed[1] > required[1] + POSITION_TOLERANCE_MM:
                    overhang.append(source)
        if len(matches) != 1:
            raise _GateError(gate, "CONTRADICTORY", "raw edge does not close stated rectangle",
                             [identity, source])
        remaining.remove(matches[0])
    if remaining:
        raise _GateError(gate, "NOT_VALIDATED", "rectangle edge missing", [identity, *sources])
    return {**bounds, "_overhang": overhang}


def _group(relation: Mapping[str, Any], profiles: Mapping[str, Mapping[str, Any]],
           rectangles: Mapping[str, dict]) -> dict[str, Any]:
    gate = PREDICATES[1]
    ids = relation.get("source_profile_ids")
    if (relation.get("evidence_type") != "ORTHOGONAL_PROFILE_CORRESPONDENCE"
            or not isinstance(ids, list) or len(ids) != 3 or len(set(ids)) != 3
            or any(identity not in profiles for identity in ids)):
        raise _GateError(gate, "NOT_VALIDATED", "incomplete profile correspondence",
                         [str(relation.get("relationship_id"))])
    by_view = {profiles[identity]["view"]: identity for identity in ids}
    if set(by_view) != set(_AXES):
        raise _GateError(gate, "CONTRADICTORY", "profile group must have one of each view", ids)
    coordinates: dict[str, tuple[float, float]] = {}
    for axis in ("x", "y", "z"):
        values = [rectangles[identity][axis] for identity in ids if axis in rectangles[identity]]
        if len(values) != 2 or not all(_close(values[0][i], values[1][i]) for i in (0, 1)):
            raise _GateError(gate, "CONTRADICTORY", f"{axis} cross-view intervals differ", ids)
        coordinates[axis] = tuple(sum(value[i] for value in values) / 2 for i in (0, 1))
    return {"id": str(relation["relationship_id"]), "profiles": sorted(ids),
            "by_view": by_view, "intervals": coordinates,
            "source_ids": sorted(set(relation.get("source_evidence_ids", [])))}


def _select_canonical(candidates: Sequence[Mapping[str, Any]]) -> str | None:
    """Rank only pre-admitted, shape-equivalent histories; order is irrelevant."""
    if not candidates or any(row.get("shape_equivalent") is not True for row in candidates):
        return None
    ranked = sorted(candidates, key=lambda row: (
        not row["typed_vocabulary"], not row["validated_path"], row["operation_count"],
        row["dependency_complexity"], row["name"],
    ))
    if len(ranked) > 1 and ranked[0]["name"] == ranked[1]["name"]:
        return None
    return str(ranked[0]["name"])


def _shape_state(candidates: Sequence[Mapping[str, Any]]) -> str:
    """Canonical ranking must never reduce the number of surviving shapes."""
    survivors = [row for row in candidates if row.get("status") == "SURVIVOR"]
    if not survivors:
        return "NOT_VALIDATED"
    classes = {row.get("shape_class") for row in survivors}
    return "RESOLVED" if len(classes) == 1 and None not in classes else "AMBIGUOUS"


def _ledger(raw: Mapping[str, tuple[str, str, Any]], evidence: Sequence[FeatureEvidence],
            derived: Sequence[Mapping[str, Any]], relations: Sequence[Mapping[str, Any]],
            profile_groups: Sequence[Mapping[str, Any]], pairs: Mapping[str, Mapping[str, Any]],
            artifacts: Mapping[str, str]) -> tuple[list[dict], int]:
    """One row per source identity; shared profile edges are relation provenance."""
    evidence_by_geometry: dict[str, str] = {}
    for row in evidence:
        if row.geometry_reference in evidence_by_geometry:
            raise _GateError("LEDGER", "NOT_VALIDATED", "duplicate evidence for one geometry",
                             [row.geometry_reference])
        evidence_by_geometry[row.geometry_reference] = row.evidence_id
    consumers: dict[tuple[str, str], set[str]] = defaultdict(set)
    profile_sources: dict[str, set[str]] = defaultdict(set)
    for group in profile_groups:
        for profile_id in group["profiles"]:
            profile = next(row for row in derived if row["primitive_id"] == profile_id)
            consumers[("DERIVED_PRIMITIVE", profile_id)].add(group["id"])
            for source in profile["source_primitive_ids"]:
                profile_sources[source].add(group["id"])
                consumers[("PRIMITIVE", source)].add(group["id"])
                if source in evidence_by_geometry:
                    consumers[("FEATURE_EVIDENCE", evidence_by_geometry[source])].add(group["id"])
        consumers[("RELATIONSHIP", group["id"])].add("PROFILE_CORRESPONDENCE")
    for source, groups in profile_sources.items():
        if len(groups) > 1:
            if len(groups) != 2 or raw[source][1] != "LINE":
                raise _GateError("LEDGER", "NOT_VALIDATED", "unexplained shared profile primitive", [source])
            consumers[("PRIMITIVE", source)].add("ADJACENCY_RELATION")
    for pair in pairs.values():
        pair_id = str(pair["primitive_id"])
        consumers[("DERIVED_PRIMITIVE", pair_id)].add("CYLINDRICAL_SUPPORT")
        for source in pair["source_primitive_ids"]:
            consumers[("PRIMITIVE", source)].add("CYLINDRICAL_SUPPORT")
        if pair_id in evidence_by_geometry:
            consumers[("FEATURE_EVIDENCE", evidence_by_geometry[pair_id])].add("CYLINDRICAL_SUPPORT")
    for source, (view, kind, _) in raw.items():
        if kind == "CIRCLE":
            consumers[("PRIMITIVE", source)].add("CYLINDRICAL_SUPPORT")
            if source in evidence_by_geometry:
                consumers[("FEATURE_EVIDENCE", evidence_by_geometry[source])].add("CYLINDRICAL_SUPPORT")
    for relation in relations:
        consumers[("RELATIONSHIP", str(relation["relationship_id"]))].add("REVIEWED_RELATION")
    for label in artifacts:
        consumers[("ARTIFACT", label)].add("SIX_ARTIFACT_BINDING")
    universe = (
        {("PRIMITIVE", identity) for identity in raw}
        | {("FEATURE_EVIDENCE", row.evidence_id) for row in evidence}
        | {("DERIVED_PRIMITIVE", str(row.get("primitive_id"))) for row in derived}
        | {("RELATIONSHIP", str(row.get("relationship_id"))) for row in relations}
        | {("ARTIFACT", label) for label in artifacts}
    )
    rows = [{"item_type": kind, "evidence_id": identity,
             "state": "CONSUMED" if consumers.get((kind, identity)) else "EXPLICITLY_UNRESOLVED",
             "reason": None if consumers.get((kind, identity)) else "NO_QUALIFIED_CONSUMER",
             "consumers": sorted(consumers.get((kind, identity), set()))}
            for kind, identity in sorted(universe)]
    return rows, sum(row["state"] != "CONSUMED" for row in rows)


def _reason_canonical(
    projection: ProjectionGraph,
    evidence: Sequence[FeatureEvidence],
    derived: Sequence[Mapping[str, Any]],
    relationships: Sequence[Mapping[str, Any]],
    roles: Mapping[str, str],
    confirmation: Mapping[str, Any],
    detections: Sequence[Mapping[str, Any]],
    artifact_hashes: Mapping[str, str],
    *,
    input_domain: str | None,
    canonical_candidates: Sequence[Mapping[str, Any]] | None = None,
) -> tuple[FeatureGraph | None, dict[str, Any]]:
    """Pure bounded reasoning; caller must have validated artifact byte bindings."""
    report = _report(input_domain)
    if input_domain is None or input_domain == "UNKNOWN":
        return _fail(report, "NOT_VALIDATED", "explicit bounded input domain is required", ask=True)
    if input_domain != INPUT_DOMAIN:
        report["input_domain_status"] = "NOT_APPLICABLE"
        report["canonical_status"] = "NOT_APPLICABLE"
        return _fail(report, "UNSUPPORTED", "input is outside connected-single-solid part domain")
    report["input_domain_status"] = "APPLICABLE"
    if not isinstance(confirmation, Mapping) or confirmation.get("decision") != "CONFIRMED" or confirmation.get("corrections") != []:
        return _fail(report, "NOT_VALIDATED", "unmodified confirmed review is required", ask=True)
    report["reviewed_inputs"] = {"confirmation_decision": "CONFIRMED",
                                  "confirmation_schema_version": confirmation.get("schema_version")}
    if set(artifact_hashes) != {"image", "projection_graph_candidate", "feature_evidence_candidate",
                                "review_overlay", "review", "confirmation"}:
        return _fail(report, "NOT_VALIDATED", "six bound artifacts are required", ask=True)
    report["artifact_validation_status"] = "VALID"
    try:
        if (not isinstance(projection, ProjectionGraph) or projection.projection != "third_angle"
                or projection.coordinate_convention.get("units") != "mm"
                or projection.feature_evidence is not None or projection.feature_evidence_records
                or projection.expected_features is not None
                or any(projection.coordinate_convention.get(name) != value for name, value in
                       (("front", "X/Y"), ("top", "X/Z"), ("left", "Z/Y")))):
            raise _GateError(PREDICATES[0], "NOT_VALIDATED", "unsupported canonical projection")
        raw = _raw_inventory(projection)
        evidence_by_id = {row.evidence_id: row for row in evidence}
        if (len(evidence_by_id) != len(evidence) or set(roles) != set(evidence_by_id)
                or any(row.feature_id is not None for row in evidence)):
            raise _GateError(PREDICATES[0], "NOT_VALIDATED", "invalid unowned source evidence")
        derived_by_id = {str(row.get("primitive_id")): row for row in derived}
        relation_by_id = {str(row.get("relationship_id")): row for row in relationships}
        if (len(derived_by_id) != len(derived) or len(relation_by_id) != len(relationships)
                or "None" in derived_by_id or "None" in relation_by_id):
            raise _GateError(PREDICATES[0], "NOT_VALIDATED", "invalid derived identity")
        profiles = {identity: row for identity, row in derived_by_id.items()
                    if row.get("geometry_type") == "VISIBLE_PROFILE"}
        profile_relations = [row for row in relationships
                             if row.get("evidence_type") == "ORTHOGONAL_PROFILE_CORRESPONDENCE"]
        _gate(report, PREDICATES[0], [*profiles, *(str(row.get("relationship_id")) for row in profile_relations)],
              len(profiles) == 6 and len(profile_relations) == 2,
              "exactly two complete rectangular three-view groups are required", outcome="NOT_VALIDATED")
        rectangles = {identity: _rect(row, raw) for identity, row in profiles.items()}
        groups = [_group(row, profiles, rectangles) for row in profile_relations]
        evidence_by_reference = {row.geometry_reference: row for row in evidence}
        for group in groups:
            source_ids = {source for profile_id in group["profiles"]
                          for source in profiles[profile_id]["source_primitive_ids"]}
            expected_ids = {evidence_by_reference[source].evidence_id for source in source_ids
                            if source in evidence_by_reference}
            if (len(expected_ids) != len(source_ids)
                    or expected_ids != set(group["source_ids"])
                    or any(roles[evidence_by_reference[source].evidence_id] != "VISIBLE_PROFILE_EDGE"
                           for source in source_ids)):
                raise _GateError(PREDICATES[1], "NOT_VALIDATED",
                                 "profile relation does not bind exact raw edge evidence",
                                 [group["id"], *source_ids])
        _gate(report, PREDICATES[1], [group["id"] for group in groups],
              len({identity for group in groups for identity in group["profiles"]}) == 6,
              "profile groups overlap or do not cover all profiles")
        lower, upper = sorted(groups, key=lambda group: (group["intervals"]["z"][0], group["id"]))
        a, b = lower["intervals"], upper["intervals"]
        _gate(report, PREDICATES[2], lower["profiles"] + upper["profiles"],
              _strict_centered(a, b),
              "upper XY profile is not strictly centered and contained")
        _gate(report, PREDICATES[3], lower["profiles"] + upper["profiles"],
              _directed_adjacent(a["z"], b["z"]),
              "directed Z intervals have a gap, overlap or reversed order")
        source_groups: dict[str, set[str]] = defaultdict(set)
        for group in groups:
            for profile_id in group["profiles"]:
                for source in profiles[profile_id]["source_primitive_ids"]:
                    source_groups[source].add(group["id"])
        overhang_ids = {source for rectangle in rectangles.values() for source in rectangle["_overhang"]}
        if any(len(source_groups[source]) != 2 or raw[source][0] not in {"top", "left"}
               for source in overhang_ids):
            raise _GateError(PREDICATES[3], "CONTRADICTORY",
                             "overhanging profile edge lacks a shared Z interface", sorted(overhang_ids))
        composite = (a["z"][0], b["z"][1])
        _gate(report, PREDICATES[4], lower["profiles"] + upper["profiles"],
              composite[1] - composite[0] > 2 * POSITION_TOLERANCE_MM
              and abs(composite[0]) <= POSITION_TOLERANCE_MM,
              "composite Z span is not positive")
        circles = [(identity, item) for identity, item in raw.items() if item[1] == "CIRCLE"]
        _gate(report, PREDICATES[5], [identity for identity, _ in circles],
              len(circles) == 1 and circles[0][1][0] == "front",
              "exactly one actual FRONT circle is required", outcome="NOT_VALIDATED")
        circle_id, circle = circles[0][0], circles[0][1][2]
        pairs: dict[str, Mapping[str, Any]] = {}
        for name, predicate in (("top", PREDICATES[6]), ("left", PREDICATES[7])):
            matches = [row for row in derived if row.get("geometry_type") == "HIDDEN_LINE_PAIR"
                       and row.get("view") == name]
            pair_ids = [str(row.get("primitive_id")) for row in matches]
            valid = (len(matches) == 1 and isinstance(matches[0].get("source_primitive_ids"), list)
                     and len(matches[0]["source_primitive_ids"]) == 2
                     and len(set(matches[0]["source_primitive_ids"])) == 2
                     and matches[0].get("relationship") == "PARALLEL_FULL_SPAN_MATCHED_TO_BOUNDARY_EXTREMA")
            _gate(report, predicate, pair_ids, valid, f"one qualified {name.upper()} hidden pair is required",
                  outcome="NOT_VALIDATED")
            pairs[name] = matches[0]
        _gate(report, PREDICATES[8], [circle_id, *upper["profiles"],
                                     *[str(row["primitive_id"]) for row in pairs.values()]],
              all(len(row.get("offsets_mm", [])) == 2 and
                  _close(abs(float(row["offsets_mm"][1]) - float(row["offsets_mm"][0])),
                         float(circle.diameter)) for row in pairs.values())
              and math.isfinite(float(circle.diameter)) and float(circle.diameter) > 0
              and all(b[axis][0] + POSITION_TOLERANCE_MM < center - float(circle.diameter) / 2
                      and center + float(circle.diameter) / 2 < b[axis][1] - POSITION_TOLERANCE_MM
                      for axis, center in (("x", float(circle.x)), ("y", float(circle.y)))),
              "circle diameter/terminal support differs from hidden-pair geometry")
        _gate(report, PREDICATES[9], [circle_id, *[str(row["primitive_id"]) for row in pairs.values()]],
              _close(sum(pairs["top"]["offsets_mm"]) / 2, circle.x)
              and _close(sum(pairs["left"]["offsets_mm"]) / 2, circle.y)
              and pairs["top"].get("shared_coordinate") == "X"
              and pairs["left"].get("shared_coordinate") == "Y",
              "circle and pair axes/centers differ")
        detection_by_id = {str(row.get("primitive_id")): row for row in detections}
        wall_ids = [source for pair in pairs.values() for source in pair["source_primitive_ids"]]
        _gate(report, PREDICATES[10], wall_ids,
              len(detection_by_id) == len(detections) and set(detection_by_id) == set(raw)
              and all(source in raw and raw[source][0] == name and raw[source][1] == "HIDDEN_LINE"
                  and source in detection_by_id and detection_by_id[source].get("primitive_type") == "HIDDEN_LINE"
                  for name, pair in pairs.items() for source in pair["source_primitive_ids"]),
              "hidden wall classification or view is not source-qualified", outcome="NOT_VALIDATED")
        _gate(report, PREDICATES[11], wall_ids,
              len(wall_ids) == 4 and len(set(wall_ids)) == 4 and
              all(math.isfinite(value) for source in wall_ids for value in
                  (raw[source][2].x1, raw[source][2].y1, raw[source][2].x2, raw[source][2].y2)),
              "four distinct finite raw wall endpoints are required", outcome="NOT_VALIDATED")
        spans = []
        for name, pair in pairs.items():
            for source in pair["source_primitive_ids"]:
                line = raw[source][2]
                if name == "top":
                    constant = _close(line.x1, line.x2)
                    span = sorted((line.y1, line.y2))
                else:
                    constant = _close(line.y1, line.y2)
                    span = sorted((line.x1, line.x2))
                spans.append({"raw_id": source, "view": name, "endpoints_mm": span})
                if not constant or not all(_close(span[index], composite[index]) for index in (0, 1)):
                    _gate(report, PREDICATES[12], wall_ids + lower["profiles"] + upper["profiles"],
                          False, "raw hidden walls do not span complete A+B Z interval")
        _gate(report, PREDICATES[12], wall_ids + lower["profiles"] + upper["profiles"],
              True, "raw hidden walls do not span complete A+B Z interval")
        if len([row for row in derived if row.get("geometry_type") == "HIDDEN_LINE_PAIR"]) != 2:
            raise _GateError("LEDGER", "NOT_VALIDATED", "unexpected extra hidden pair")
        if (len(relationships) != 3 or len([row for row in relationships if
                                             row.get("evidence_type") == "CIRCLE_ORTHOGONAL_HIDDEN_PAIR_SUPPORT"]) != 1):
            raise _GateError("LEDGER", "NOT_VALIDATED", "incomplete cylinder support relation")
        support_relation = next(row for row in relationships if
                                row.get("evidence_type") == "CIRCLE_ORTHOGONAL_HIDDEN_PAIR_SUPPORT")
        hole_references = {circle_id, *(str(pair["primitive_id"]) for pair in pairs.values())}
        hole_evidence_ids = {row.evidence_id for row in evidence if row.geometry_reference in hole_references}
        if (set(support_relation.get("source_evidence_ids", [])) != hole_evidence_ids
                or len(hole_evidence_ids) != 3
                or any(roles[row.evidence_id] != ("OBSERVED_CIRCLE" if row.geometry_reference == circle_id
                                                       else "ORTHOGONAL_HIDDEN_PAIR")
                       for row in evidence if row.geometry_reference in hole_references)):
            raise _GateError("LEDGER", "NOT_VALIDATED",
                             "cylinder support relation does not bind exact circle/pairs",
                             [str(support_relation["relationship_id"]), *hole_references])
        ledger, unconsumed = _ledger(raw, evidence, derived, relationships, groups, pairs, artifact_hashes)
        report["evidence_ledger"] = ledger
        report["evidence_ledger_summary"]["unconsumed_relevant"] = unconsumed
        report["checks"]["unconsumed_evidence_count"] = unconsumed
        if unconsumed:
            raise _GateError("LEDGER", "NOT_VALIDATED", "relevant source evidence remains unresolved",
                             [row["evidence_id"] for row in ledger if row["state"] != "CONSUMED"])
        report["shape_survivors"] = [{"name": "FUSED_SINGLE_SOLID", "shape_class": "FUSED_SINGLE_SOLID",
                                      "status": "SURVIVOR",
                                      "source_ids": sorted(set(wall_ids + lower["profiles"] + upper["profiles"] + [circle_id]))}]
        report["shape_exclusions"] = [{"name": "TOUCHING_INDEPENDENT_BODIES",
                                       "status": "OUTSIDE_SUPPORTED_INPUT_DOMAIN",
                                       "excluded_by": "PROJECT_POLICY_SINGLE_SOLID",
                                       "contradicted_by_drawing": False},
                                      {"name": "CYLINDRICAL_ADDITIVE",
                                       "status": "CONTRADICTED_BY_EVIDENCE",
                                       "reason": "INTERNAL_CIRCLE_AND_COMPLEMENTARY_HIDDEN_WALLS",
                                       "source_ids": sorted([circle_id, *wall_ids])},
                                      {"name": "CYLINDRICAL_FINITE_DEPTH",
                                       "status": "CONTRADICTED_BY_EVIDENCE",
                                       "reason": "POSITIVE_FULL_COMPOSITE_SPAN",
                                       "source_ids": sorted(wall_ids)}]
        report["shape_status"] = _shape_state(report["shape_survivors"])
        if report["shape_status"] != "RESOLVED":
            return _fail(report, "AMBIGUOUS", "multiple in-domain final shape classes survive", ask=True)
        basis = sorted({item for row in report["predicate_results"] for item in row["source_ids"]})
        report["history_equivalence"] = {
            "status": "BOUNDED_EQUIVALENT", "identity": "(T\\P)\\V=(L∪U)\\V",
            "guard_predicates": list(PREDICATES), "source_ids": basis,
            "composite_z_mm": list(composite), "raw_wall_spans": spans,
            "input_domain_qualification": "PROJECT_POLICY",
        }
        report["history_status"] = "NON_IDENTIFIABLE"
        report["surviving_histories"] = [
            {"name": "H_ADD", "historically_possible": True},
            {"name": "H_CUT", "historically_possible": True, "contradicted_by_drawing": False},
        ]
        candidates = list(canonical_candidates) if canonical_candidates is not None else [
            {"name": "C_ADD", "shape_equivalent": True, "typed_vocabulary": True,
             "validated_path": True, "operation_count": 3, "dependency_complexity": 2},
            {"name": "C_CUT", "shape_equivalent": True, "typed_vocabulary": False,
             "validated_path": False, "operation_count": 4, "dependency_complexity": 3},
        ]
        report["canonical_candidates"] = sorted((dict(row) for row in candidates), key=lambda row: row["name"])
        selected = _select_canonical(candidates)
        if selected != "C_ADD":
            return _fail(report, "NOT_VALIDATED", "bounded canonical policy unavailable or tied", ask=True)
        report["selected_candidate"], report["canonical_status"] = selected, "SELECTED"
        base_evidence = sorted(set(lower["source_ids"]))
        boss_evidence = sorted(set(upper["source_ids"]))
        hole_evidence = sorted(hole_evidence_ids)
        if not base_evidence or not boss_evidence or not hole_evidence:
            return _fail(report, "NOT_VALIDATED", "feature evidence partition is empty")
        base_params = {"width_mm": a["x"][1] - a["x"][0],
                       "height_mm": a["y"][1] - a["y"][0],
                       "depth_mm": a["z"][1] - a["z"][0]}
        boss_params = {"width_mm": b["x"][1] - b["x"][0],
                       "height_mm": b["y"][1] - b["y"][0],
                       "depth_mm": b["z"][1] - b["z"][0], "centered": True}
        hole_params = {"diameter_mm": float(circle.diameter),
                       "center_x_mm": float(circle.x) - sum(a["x"]) / 2,
                       "center_y_mm": float(circle.y) - sum(a["y"]) / 2,
                       "axis": "Z", "through": True}
        base_id = _identity("BASE_BLOCK", base_params, base_evidence)
        boss_id = _identity("BOSS", boss_params, boss_evidence)
        hole_id = _identity("THROUGH_HOLE", hole_params, hole_evidence)
        base = BaseBlock(base_params["width_mm"], base_params["height_mm"], base_params["depth_mm"],
                         feature_id=base_id, source_evidence_ids=base_evidence, dependencies=[])
        boss = Boss(boss_params["width_mm"], boss_params["height_mm"], boss_params["depth_mm"], True,
                    feature_id=boss_id, source_evidence_ids=boss_evidence, dependencies=[base_id])
        hole = Hole(hole_params["diameter_mm"], hole_params["center_x_mm"],
                    hole_params["center_y_mm"], "Z", True, feature_id=hole_id,
                    source_evidence_ids=hole_evidence, dependencies=[boss_id])
        graph = FeatureGraph(base, holes=[hole], bosses=[boss], status="NOT_VALIDATED")
        report.update(status="INFERRED", outcome="SELECTED", feature_graph=graph.to_dict(),
                      feature_nodes=sorted((node.to_dict() for node in graph.to_feature_nodes()),
                                           key=lambda row: row["feature_id"]),
                      feature_provenance=sorted([
                          {"feature_id": base_id, "selection_source": "CANONICAL_POLICY",
                           "source_evidence_ids": base_evidence, "source_relationship_ids": [lower["id"]]},
                          {"feature_id": boss_id, "selection_source": "CANONICAL_POLICY",
                           "source_evidence_ids": boss_evidence, "source_relationship_ids": [upper["id"]]},
                          {"feature_id": hole_id, "selection_source": "CANONICAL_POLICY",
                           "source_evidence_ids": hole_evidence,
                           "source_relationship_ids": [str(support_relation["relationship_id"])]},
                      ], key=lambda row: row["feature_id"]))
        report["checks"].update(feature_count=3, physical_survivor_count=1,
                                mandatory_gate_failures=0, blocking_contradiction_count=0)
        return graph, report
    except _GateError as exc:
        row = next((item for item in report["predicate_results"] if item["name"] == exc.predicate), None)
        if row is not None:
            row["status"] = "NOT_VALIDATED" if exc.outcome == "NOT_VALIDATED" else "FAIL"
            row["reason"], row["source_ids"] = str(exc), exc.ids
        report["checks"]["mandatory_gate_failures"] = 1
        return _fail(report, exc.outcome, str(exc), ask=exc.outcome in {"NOT_VALIDATED", "AMBIGUOUS"})
    except (TypeError, ValueError, KeyError, OverflowError) as exc:
        report["checks"]["mandatory_gate_failures"] = 1
        return _fail(report, "NOT_VALIDATED", f"invalid source-backed input: {exc}", ask=True)


def reason_r003_from_reviewed_artifacts(
    source_png_path: str | Path,
    projection_graph_path: str | Path,
    feature_evidence_path: str | Path,
    overlay_path: str | Path,
    review_path: str | Path,
    confirmation_path: str | Path,
    *,
    input_domain: str | None,
) -> tuple[FeatureGraph | None, dict[str, Any]]:
    """Public six-artifact entry; domain authority is an explicit caller argument."""
    report = _report(input_domain)
    if input_domain is None or input_domain == "UNKNOWN":
        return _fail(report, "NOT_VALIDATED", "explicit bounded input domain is required", ask=True)
    if input_domain != INPUT_DOMAIN:
        report["input_domain_status"] = "NOT_APPLICABLE"
        report["canonical_status"] = "NOT_APPLICABLE"
        return _fail(report, "UNSUPPORTED", "input is outside connected-single-solid part domain")
    report["input_domain_status"] = "APPLICABLE"
    paths = {"image": source_png_path, "projection_graph_candidate": projection_graph_path,
             "feature_evidence_candidate": feature_evidence_path, "review_overlay": overlay_path,
             "review": review_path, "confirmation": confirmation_path}
    try:
        confirmation = validate_confirmation(*paths.values())
        if confirmation.get("decision") != "CONFIRMED" or confirmation.get("corrections") != []:
            return _fail(report, "NOT_VALIDATED", "unmodified CONFIRMED review is required", ask=True)
        snapshots = {}
        for label, path in paths.items():
            payload = Path(path).read_bytes()
            if label == "confirmation":
                if json.loads(payload.decode("utf-8-sig")) != confirmation:
                    raise ValueError("confirmation changed after validation")
            elif sha256(payload).hexdigest() != confirmation["sources"][label]["sha256"]:
                raise ValueError(f"reviewed {label} changed after validation")
            snapshots[label] = payload
        pg_candidate = json.loads(snapshots["projection_graph_candidate"].decode("utf-8-sig"))
        fe_candidate = json.loads(snapshots["feature_evidence_candidate"].decode("utf-8-sig"))
        if pg_candidate.get("status") != "EXTRACTED" or fe_candidate.get("status") != "INFERRED":
            return _fail(report, "NOT_VALIDATED", "reviewed B008/B009 candidates are incomplete", ask=True)
        graph_data = pg_candidate["projection_graph"]

        def view(name: str) -> ViewGeometry:
            row = graph_data[name]
            return ViewGeometry(
                name=row["name"], horizontal_extent=row["horizontal_extent"],
                vertical_extent=row["vertical_extent"],
                circles=[Circle(**item) for item in row["circles"]],
                hidden_line_pairs=[],
                visible_segments=[LineSegment(**item) for item in row["visible_segments"]],
                hidden_segments=[LineSegment(**item) for item in row["hidden_segments"]],
                arcs=[Arc(**item) for item in row["arcs"]],
                centerlines=[LineSegment(**item) for item in row["centerlines"]],
            )

        projection = ProjectionGraph(graph_data["projection"], view("front"), view("top"),
                                     view("left"), graph_data["coordinate_convention"])
        evidence = [FeatureEvidence(**row) for row in fe_candidate["feature_evidence"]]
        hashes = {label: sha256(payload).hexdigest() for label, payload in snapshots.items()}
        return _reason_canonical(
            projection, evidence, fe_candidate["derived_primitives"],
            fe_candidate["cross_view_relationships"], fe_candidate["roles"], confirmation,
            pg_candidate["detections"], hashes, input_domain=input_domain,
        )
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        return _fail(report, "NOT_VALIDATED", f"reviewed artifact validation failed: {exc}", ask=True)


__all__ = ["CAPABILITY_SCOPE", "INPUT_DOMAIN", "PREDICATES", "reason_r003_from_reviewed_artifacts"]
