"""R002 bounded reasoning for one cylindrical candidate on one BASE_BLOCK.

This module consumes canonical geometry/evidence plus reviewed B009 derived
relations.  It deliberately does not call the frozen B009 shortcut that maps a
circle directly to ``Hole(through=True)``.
"""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from hashlib import sha256
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

from experiments.three_view_reconstruction.schemas.feature_evidence import FeatureEvidence
from experiments.three_view_reconstruction.schemas.feature_graph import FeatureGraph, Hole
from experiments.three_view_reconstruction.schemas.projection_graph import ProjectionGraph
from experiments.three_view_reconstruction.schemas.view_geometry import (
    Arc, Circle, LineSegment, ViewGeometry,
)
from experiments.three_view_reconstruction.validation.primitive_matcher import (
    POSITION_TOLERANCE_MM,
    RADIUS_TOLERANCE_MM,
)
from solidworks_main_skill.feature_graph_inference import (
    _axis_intervals,
    _constant_and_span,
    _identity,
)
from solidworks_main_skill.review.human_confirmation import validate_confirmation

from .base_block_reasoner import (
    BaseBlockReasoner,
    CAPABILITY_SCOPE as R001_CAPABILITY_SCOPE,
    validate_confirmation_artifact,
)


CAPABILITY_SCOPE = "R002_BASE_BLOCK_ONE_CYLINDRICAL_CANDIDATE"
SCHEMA_VERSION = "cad-reasoning-v1.r002-feature-graph-candidate/1.0"
VERSION = "cad-reasoning-v1.r002/1.0"
GENERATION_TRUTH_READS = 0
PAIR_TOLERANCE_MM = max(POSITION_TOLERANCE_MM, RADIUS_TOLERANCE_MM)

CANDIDATE_KEYS = (
    "CYLINDRICAL_ADDITIVE",
    "CYLINDRICAL_SUBTRACTIVE_FINITE_DEPTH",
    "CYLINDRICAL_SUBTRACTIVE_FULL_BODY_DEPTH",
    "CYLINDRICAL_UNRESOLVED",
)

CONTRADICTION_NAMES = (
    "BASE_PROFILE_MISMATCH",
    "VIEW_CORRESPONDENCE_FAILURE",
    "AXIS_MISMATCH",
    "DIAMETER_MISMATCH",
    "BOUNDARY_STYLE_CONTRADICTION",
    "MATERIAL_SIGN_CONTRADICTION",
    "FINITE_DEPTH_CONTRADICTED_BY_FULL_SPAN",
    "BODY_SPAN_MISMATCH",
    "TERMINATION_PRESENT",
    "UNCONSUMED_RELEVANT_EVIDENCE",
)


def _close(left: float, right: float, tolerance: float = POSITION_TOLERANCE_MM) -> bool:
    return abs(float(left) - float(right)) <= tolerance


def _contradiction(
    name: str,
    candidates: Sequence[str],
    required: Sequence[str],
    observed: Sequence[str],
    reason: str,
    *,
    blocking: bool,
) -> dict[str, Any]:
    if name not in CONTRADICTION_NAMES:
        raise ValueError("unknown R002 contradiction")
    return {
        "name": name,
        "candidates": sorted(set(candidates)),
        "required_evidence_ids": sorted(set(required)),
        "observed_evidence_ids": sorted(set(observed)),
        "reason": reason,
        "blocking": blocking,
    }


def _resolve_candidate_lifecycle(
    material_signs: set[str],
    depth_candidates: set[str],
    eliminated_by: Mapping[str, Sequence[str]],
) -> tuple[list[dict[str, Any]], str, str | None]:
    """Resolve the fixed candidate set without publishing another schema."""
    admitted = {
        "CYLINDRICAL_ADDITIVE": "ADDITIVE" in material_signs,
        "CYLINDRICAL_SUBTRACTIVE_FINITE_DEPTH": (
            "SUBTRACTIVE" in material_signs and "FINITE_DEPTH" in depth_candidates
        ),
        "CYLINDRICAL_SUBTRACTIVE_FULL_BODY_DEPTH": (
            "SUBTRACTIVE" in material_signs and "FULL_BODY_DEPTH" in depth_candidates
        ),
    }
    survivors = [key for key, value in admitted.items() if value]
    if survivors == ["CYLINDRICAL_SUBTRACTIVE_FULL_BODY_DEPTH"]:
        outcome, next_action = "RESOLVED", None
    elif survivors == ["CYLINDRICAL_SUBTRACTIVE_FINITE_DEPTH"]:
        outcome, next_action = "UNSUPPORTED", None
    elif survivors:
        outcome, next_action = "AMBIGUOUS", "ASK_HUMAN"
    else:
        outcome, next_action = "CONTRADICTORY", None

    records = []
    for key in CANDIDATE_KEYS[:-1]:
        eliminations = sorted(set(eliminated_by.get(key, ())))
        if admitted[key]:
            final = "RESOLVED" if outcome == "RESOLVED" else "SURVIVOR"
            lifecycle = ["GENERATED", "SUPPORTED", "SURVIVOR"]
            if final == "RESOLVED":
                lifecycle.append("RESOLVED")
        else:
            final = "CONTRADICTED"
            lifecycle = ["GENERATED", "SUPPORTED", "CONTRADICTED"]
        records.append({
            "candidate": key,
            "physical": True,
            "state": final,
            "lifecycle": lifecycle,
            "eliminated_by": eliminations,
        })
    if outcome == "AMBIGUOUS":
        records.append({
            "candidate": CANDIDATE_KEYS[-1],
            "physical": False,
            "state": "SURVIVOR",
            "lifecycle": ["GENERATED", "SUPPORTED", "SURVIVOR"],
            "resolution": "EVIDENCE_UNRESOLVED",
            "eliminated_by": [],
        })
    return records, outcome, next_action


def _empty_result(
    outcome: str,
    confirmation: Mapping[str, Any] | None,
    issues: Sequence[str],
    *,
    contradictions: Sequence[Mapping[str, Any]] = (),
    candidates: Sequence[Mapping[str, Any]] = (),
    evidence_ledger: Sequence[Mapping[str, Any]] = (),
    unconsumed: int = 0,
    next_action: str | None = None,
) -> dict[str, Any]:
    blocking = sum(bool(row.get("blocking")) for row in contradictions)
    return {
        "schema_version": SCHEMA_VERSION,
        "version": VERSION,
        "capability_scope": CAPABILITY_SCOPE,
        "status": outcome,
        "outcome": outcome,
        "next_action": next_action,
        "feature_graph": None,
        "feature_nodes": [],
        "feature_provenance": [],
        "derived_relations": [],
        "candidates": list(candidates),
        "contradictions": sorted(
            (dict(row) for row in contradictions), key=lambda row: row["name"]),
        "evidence_ledger": sorted(
            (dict(row) for row in evidence_ledger),
            key=lambda row: (row["item_type"], row["evidence_id"]),
        ),
        "reviewed_inputs": {
            "confirmation_decision": confirmation.get("decision")
            if isinstance(confirmation, Mapping) else None,
            "confirmation_schema_version": confirmation.get("schema_version")
            if isinstance(confirmation, Mapping) else None,
        },
        "checks": {
            "feature_count": 0,
            "physical_survivor_count": sum(
                row.get("physical") is True and row.get("state") in {"SURVIVOR", "RESOLVED"}
                for row in candidates
            ),
            "mandatory_gate_failures": 1 if outcome in {"NOT_VALIDATED", "UNSUPPORTED"} else 0,
            "unresolved_reference_count": blocking,
            "unconsumed_evidence_count": unconsumed,
            "elimination_contradiction_count": sum(
                row.get("name") in {
                    "MATERIAL_SIGN_CONTRADICTION",
                    "FINITE_DEPTH_CONTRADICTED_BY_FULL_SPAN",
                    "BODY_SPAN_MISMATCH",
                    "TERMINATION_PRESENT",
                } for row in contradictions),
            "blocking_contradiction_count": blocking,
            "generation_truth_reads": GENERATION_TRUTH_READS,
        },
        "issues": sorted(set(issues)),
    }


def _view_primitives(projection: ProjectionGraph) -> dict[str, tuple[str, str, Any]]:
    result: dict[str, tuple[str, str, Any]] = {}
    families = (
        ("visible_segments", "LINE"),
        ("hidden_segments", "HIDDEN_LINE"),
        ("circles", "CIRCLE"),
        ("arcs", "ARC"),
        ("centerlines", "CENTERLINE"),
    )
    for view_name in ("front", "top", "left"):
        view = getattr(projection, view_name)
        for field, kind in families:
            for primitive in getattr(view, field):
                identity = primitive.primitive_id
                if not isinstance(identity, str) or not identity or identity in result:
                    raise ValueError("duplicate or empty primitive identity")
                result[identity] = (view_name, kind, primitive)
    return result


def _base_projection(projection: ProjectionGraph, source_ids: set[str]) -> ProjectionGraph:
    def filtered(name: str) -> ViewGeometry:
        view = getattr(projection, name)
        lines = [line for line in view.visible_segments if line.primitive_id in source_ids]
        return ViewGeometry(name, view.horizontal_extent, view.vertical_extent,
                            visible_segments=lines)

    return ProjectionGraph(
        projection.projection,
        filtered("front"),
        filtered("top"),
        filtered("left"),
        deepcopy(projection.coordinate_convention),
    )


def _terminal_sources(
    projection: ProjectionGraph,
    base_sources: set[str],
    pair_sources: set[str],
    pairs: Mapping[str, Mapping[str, Any]],
    z_interval: tuple[float, float],
) -> set[str]:
    terminals: set[str] = set()
    for view_name, expected_orientation in (("top", "X"), ("left", "Y")):
        pair = pairs[view_name]
        offsets = sorted(float(value) for value in pair["offsets_mm"])
        view = getattr(projection, view_name)
        for line in (*view.visible_segments, *view.hidden_segments):
            identity = line.primitive_id
            if identity in base_sources or identity in pair_sources:
                continue
            try:
                orientation, constant, span = _constant_and_span(line.__dict__)
            except ValueError:
                continue
            if (orientation == expected_orientation
                    and all(_close(span[index], offsets[index]) for index in (0, 1))
                    and z_interval[0] + POSITION_TOLERANCE_MM < constant
                    < z_interval[1] - POSITION_TOLERANCE_MM):
                terminals.add(identity)
    return terminals


def _ledger(
    primitives: Mapping[str, tuple[str, str, Any]],
    evidence: Sequence[FeatureEvidence],
    derived: Sequence[Mapping[str, Any]],
    relationships: Sequence[Mapping[str, Any]],
    consumers: Mapping[tuple[str, str], set[str]],
    unresolved: set[tuple[str, str]],
) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
    universe = (
        [("PRIMITIVE", identity) for identity in primitives]
        + [("FEATURE_EVIDENCE", row.evidence_id) for row in evidence]
        + [("DERIVED_PRIMITIVE", str(row.get("primitive_id"))) for row in derived]
        + [("RELATIONSHIP", str(row.get("relationship_id"))) for row in relationships]
    )
    rows = []
    unconsumed = []
    for key in sorted(set(universe)):
        used_by = sorted(consumers.get(key, set()))
        if used_by:
            state = "CONSUMED"
        elif key in unresolved:
            state = "EXPLICITLY_UNRESOLVED"
        else:
            state = "UNCONSUMED"
            unconsumed.append(key)
        rows.append({
            "item_type": key[0],
            "evidence_id": key[1],
            "state": state,
            "consumers": used_by,
        })
    return rows, unconsumed


class CylindricalReasoner:
    """Resolve the single source-qualified R002 through-hole pattern."""

    def _reason_canonical(
        self,
        projection: ProjectionGraph,
        evidence: Sequence[FeatureEvidence],
        confirmation: Mapping[str, Any],
        *,
        derived_primitives: Sequence[Mapping[str, Any]],
        cross_view_relationships: Sequence[Mapping[str, Any]],
        evidence_roles: Mapping[str, str],
        capability_scope: str,
    ) -> tuple[FeatureGraph | None, dict[str, Any]]:
        if capability_scope != CAPABILITY_SCOPE:
            return None, _empty_result(
                "UNSUPPORTED", confirmation, ["unsupported capability scope"])
        if not isinstance(projection, ProjectionGraph):
            return None, _empty_result(
                "NOT_VALIDATED", confirmation, ["canonical ProjectionGraph is required"])
        try:
            validate_confirmation_artifact(confirmation)
        except (TypeError, ValueError) as exc:
            return None, _empty_result("NOT_VALIDATED", confirmation, [str(exc)])
        machine_state = confirmation.get("machine_state")
        if (not isinstance(machine_state, Mapping)
                or machine_state.get("projection") != "EXTRACTED"
                or machine_state.get("evidence") != "INFERRED"):
            return None, _empty_result(
                "NOT_VALIDATED", confirmation,
                ["reviewed complete projection and line semantics are required"])
        if (projection.projection != "third_angle"
                or not isinstance(projection.coordinate_convention, dict)
                or projection.coordinate_convention.get("units") != "mm"
                or projection.coordinate_convention.get("front") != "X/Y"
                or projection.coordinate_convention.get("top") != "X/Z"
                or projection.coordinate_convention.get("left") != "Z/Y"):
            return None, _empty_result(
                "UNSUPPORTED", confirmation, ["unsupported projection coordinate convention"])
        if (projection.feature_evidence is not None or projection.feature_evidence_records
                or projection.expected_features is not None):
            return None, _empty_result(
                "NOT_VALIDATED", confirmation,
                ["R002 accepts geometry-only canonical ProjectionGraph input"])
        if (not isinstance(evidence, Sequence) or isinstance(evidence, (str, bytes))
                or not all(isinstance(row, FeatureEvidence) for row in evidence)):
            return None, _empty_result(
                "NOT_VALIDATED", confirmation, ["canonical FeatureEvidence sequence is required"])
        if (not isinstance(derived_primitives, Sequence)
                or isinstance(derived_primitives, (str, bytes))
                or not isinstance(cross_view_relationships, Sequence)
                or isinstance(cross_view_relationships, (str, bytes))
                or not isinstance(evidence_roles, Mapping)):
            return None, _empty_result(
                "NOT_VALIDATED", confirmation, ["reviewed B009 derived evidence is required"])

        try:
            primitives = _view_primitives(projection)
        except (TypeError, ValueError) as exc:
            return None, _empty_result("NOT_VALIDATED", confirmation, [str(exc)])
        if any(not math.isfinite(float(value)) or float(value) <= 0 for view in
               (projection.front, projection.top, projection.left)
               for value in (view.horizontal_extent, view.vertical_extent)):
            return None, _empty_result(
                "NOT_VALIDATED", confirmation, ["view extents must be finite and positive"])

        evidence_by_id = {row.evidence_id: row for row in evidence}
        if len(evidence_by_id) != len(evidence):
            return None, _empty_result(
                "NOT_VALIDATED", confirmation, ["evidence IDs must be unique"])
        if set(evidence_roles) != set(evidence_by_id):
            return None, _empty_result(
                "NOT_VALIDATED", confirmation, ["every evidence ID requires exactly one role"])
        if any(row.feature_id is not None for row in evidence):
            return None, _empty_result(
                "NOT_VALIDATED", confirmation, ["input evidence must not pre-assign feature ownership"])

        derived_by_id = {str(row.get("primitive_id")): row for row in derived_primitives}
        relation_by_id = {str(row.get("relationship_id")): row
                          for row in cross_view_relationships}
        if (len(derived_by_id) != len(derived_primitives)
                or len(relation_by_id) != len(cross_view_relationships)
                or "None" in derived_by_id or "None" in relation_by_id):
            return None, _empty_result(
                "NOT_VALIDATED", confirmation, ["derived and relationship IDs must be unique"])

        circles = [(identity, item) for identity, item in primitives.items() if item[1] == "CIRCLE"]
        arcs = [identity for identity, item in primitives.items() if item[1] == "ARC"]
        profiles = [row for row in derived_primitives if row.get("geometry_type") == "VISIBLE_PROFILE"]
        profile_relations = [row for row in cross_view_relationships
                             if row.get("evidence_type") == "ORTHOGONAL_PROFILE_CORRESPONDENCE"]
        if len(circles) > 1 or len(profiles) > 3 or len(profile_relations) > 1:
            return None, _empty_result(
                "UNSUPPORTED", confirmation, ["explicit second feature is outside R002"])
        if arcs:
            return None, _empty_result(
                "UNSUPPORTED", confirmation, ["arc or curved feature geometry is outside R002"])
        if len(circles) != 1:
            return None, _empty_result(
                "NOT_VALIDATED", confirmation, ["exactly one cylindrical circle is required"])
        circle_id, (circle_view, _circle_kind, circle) = circles[0]
        if circle_view != "front":
            return None, _empty_result(
                "UNSUPPORTED", confirmation, ["R002 supports only a front-view Z-axis circle"])
        circle_rows = [row for row in evidence
                       if row.geometry_type == "CIRCLE"
                       or evidence_roles[row.evidence_id] == "OBSERVED_CIRCLE"]
        if not circle_rows:
            return None, _empty_result(
                "NOT_VALIDATED", confirmation, ["front circle evidence is missing"])
        if (len(circle_rows) != 1 or circle_rows[0].view != "front"
                or circle_rows[0].geometry_type != "CIRCLE"
                or circle_rows[0].geometry_reference != circle_id):
            observed = [row.evidence_id for row in circle_rows]
            observed.extend(row.geometry_reference for row in circle_rows)
            contradiction = _contradiction(
                "VIEW_CORRESPONDENCE_FAILURE", CANDIDATE_KEYS[:-1],
                [circle_id, *sorted(row.evidence_id for row in circle_rows)], observed,
                "circle evidence must bind the unique actual front circle", blocking=True)
            return None, _empty_result(
                "CONTRADICTORY", confirmation, [contradiction["reason"]],
                contradictions=[contradiction])
        if len(profiles) != 3 or len(profile_relations) != 1:
            return None, _empty_result(
                "NOT_VALIDATED", confirmation, ["one three-view base profile group is required"])

        profile_relation = profile_relations[0]
        profile_ids = {str(row.get("primitive_id")) for row in profiles}
        if (set(profile_relation.get("source_profile_ids", ())) != profile_ids
                or {row.get("view") for row in profiles} != {"front", "top", "left"}):
            contradiction = _contradiction(
                "BASE_PROFILE_MISMATCH", CANDIDATE_KEYS[:-1],
                sorted(profile_ids | {str(profile_relation["relationship_id"])}),
                [*profile_relation.get("source_profile_ids", ()),
                 str(profile_relation["relationship_id"])],
                "base profiles do not form one three-view correspondence", blocking=True)
            return None, _empty_result(
                "CONTRADICTORY", confirmation, [contradiction["reason"]],
                contradictions=[contradiction])
        base_sources = {str(source) for row in profiles
                        for source in row.get("source_primitive_ids", ())}
        base_evidence = [row for row in evidence if row.geometry_reference in base_sources]
        base_evidence_ids = {row.evidence_id for row in base_evidence}
        if set(profile_relation.get("source_evidence_ids", ())) != base_evidence_ids:
            contradiction = _contradiction(
                "BASE_PROFILE_MISMATCH", CANDIDATE_KEYS[:-1],
                sorted(base_evidence_ids | {str(profile_relation["relationship_id"])}),
                [*profile_relation.get("source_evidence_ids", ()),
                 str(profile_relation["relationship_id"])],
                "base profile relationship does not reference the exact profile evidence",
                blocking=True)
            return None, _empty_result(
                "CONTRADICTORY", confirmation, [contradiction["reason"]],
                contradictions=[contradiction])
        incorrectly_styled_base = sorted(
            row.evidence_id for row in base_evidence
            if evidence_roles[row.evidence_id] != "VISIBLE_PROFILE_EDGE")
        if incorrectly_styled_base:
            contradiction = _contradiction(
                "BOUNDARY_STYLE_CONTRADICTION", CANDIDATE_KEYS[:-1],
                sorted(base_evidence_ids | base_sources
                       | {str(profile_relation["relationship_id"])}),
                incorrectly_styled_base,
                "base contour evidence is not classified as visible profile geometry",
                blocking=True)
            return None, _empty_result(
                "CONTRADICTORY", confirmation, [contradiction["reason"]],
                contradictions=[contradiction])
        try:
            base_graph, base_result = BaseBlockReasoner().reason(
                _base_projection(projection, base_sources), base_evidence, confirmation,
                capability_scope=R001_CAPABILITY_SCOPE)
        except (TypeError, ValueError) as exc:
            return None, _empty_result("NOT_VALIDATED", confirmation, [str(exc)])
        if base_graph is None or base_result.get("status") != "INFERRED":
            if base_result.get("status") == "AMBIGUOUS":
                contradiction = _contradiction(
                    "BASE_PROFILE_MISMATCH", CANDIDATE_KEYS[:-1], sorted(base_sources),
                    sorted(base_sources), "R001 base profiles do not correspond", blocking=True)
                return None, _empty_result(
                    "CONTRADICTORY", confirmation, [contradiction["reason"]],
                    contradictions=[contradiction])
            return None, _empty_result(
                "NOT_VALIDATED", confirmation, base_result.get("issues", ["R001 base failed"]))
        try:
            intervals = _axis_intervals(profiles)
        except (TypeError, ValueError) as exc:
            contradiction = _contradiction(
                "BASE_PROFILE_MISMATCH", CANDIDATE_KEYS[:-1], sorted(profile_ids),
                sorted(profile_ids), str(exc), blocking=True)
            return None, _empty_result(
                "CONTRADICTORY", confirmation, [str(exc)], contradictions=[contradiction])
        base = base_graph.base_block
        if not (_close(intervals["x"][1] - intervals["x"][0], base.width)
                and _close(intervals["y"][1] - intervals["y"][0], base.height)
                and _close(intervals["z"][1] - intervals["z"][0], base.depth)):
            contradiction = _contradiction(
                "BASE_PROFILE_MISMATCH", CANDIDATE_KEYS[:-1], sorted(profile_ids),
                sorted(profile_ids), "profile intervals disagree with the R001 base", blocking=True)
            return None, _empty_result(
                "CONTRADICTORY", confirmation, [contradiction["reason"]],
                contradictions=[contradiction])

        circle_evidence = [row for row in evidence
                           if row.geometry_type == "CIRCLE"
                           or evidence_roles[row.evidence_id] == "OBSERVED_CIRCLE"]
        pairs = [row for row in derived_primitives if row.get("geometry_type") == "HIDDEN_LINE_PAIR"]
        pair_by_view = {str(row.get("view")): row for row in pairs}
        if len(circle_evidence) != 1 or len(pair_by_view) < 2:
            return None, _empty_result(
                "NOT_VALIDATED", confirmation,
                ["circle and two orthogonal hidden-pair observations are mandatory"])
        if len(pairs) != 2 or set(pair_by_view) != {"top", "left"}:
            return None, _empty_result(
                "NOT_VALIDATED", confirmation,
                ["exactly one top and one left hidden pair are required"])
        pair_evidence = [row for row in evidence
                         if row.geometry_type == "HIDDEN_LINE_PAIR"
                         or evidence_roles[row.evidence_id] == "ORTHOGONAL_HIDDEN_PAIR"]
        if len(pair_evidence) < 2:
            return None, _empty_result(
                "NOT_VALIDATED", confirmation,
                ["top and left hidden-pair evidence are required"])
        pair_rows = {row.view: row for row in pair_evidence}
        circle_row = circle_evidence[0]
        binding_ids = [circle_row.evidence_id, circle_id]
        binding_ids.extend(str(pair_by_view[view]["primitive_id"]) for view in ("top", "left"))
        binding_ids.extend(row.evidence_id for row in pair_evidence)
        binding_ids.extend(row.geometry_reference for row in pair_evidence)
        wrongly_styled_sources: list[str] = []
        binding_ok = (
            circle_row.view == "front" and circle_row.geometry_type == "CIRCLE"
            and circle_row.geometry_reference == circle_id
            and primitives.get(circle_id, (None, None))[0:2] == ("front", "CIRCLE")
            and len(pair_evidence) == 2 and len(pair_rows) == 2
            and set(pair_rows) == {"top", "left"}
        )
        if binding_ok:
            for view_name in ("top", "left"):
                row, pair = pair_rows[view_name], pair_by_view[view_name]
                sources = pair.get("source_primitive_ids", ())
                binding_ids.extend(str(source) for source in sources)
                if (row.geometry_type != "HIDDEN_LINE_PAIR"
                        or row.geometry_reference != pair["primitive_id"]
                        or pair.get("view") != view_name
                        or len(sources) != 2 or len(set(sources)) != 2
                        or any(primitives.get(source, (None, None))[0] != view_name
                               for source in sources)):
                    binding_ok = False
                wrongly_styled_sources.extend(
                    str(source) for source in sources
                    if primitives.get(source, (None, None))[0] == view_name
                    and primitives[source][1] != "HIDDEN_LINE")
        if not binding_ok:
            contradiction = _contradiction(
                "VIEW_CORRESPONDENCE_FAILURE", CANDIDATE_KEYS[:-1],
                sorted(set(binding_ids)), sorted(set(binding_ids)),
                "circle and hidden-pair evidence must bind exact same-view primitives",
                blocking=True)
            return None, _empty_result(
                "CONTRADICTORY", confirmation, [contradiction["reason"]],
                contradictions=[contradiction])
        if wrongly_styled_sources:
            contradiction = _contradiction(
                "BOUNDARY_STYLE_CONTRADICTION", CANDIDATE_KEYS[:-1],
                sorted(set(binding_ids)), sorted(set(wrongly_styled_sources)),
                "complementary boundary source is not hidden", blocking=True)
            return None, _empty_result(
                "CONTRADICTORY", confirmation, [contradiction["reason"]],
                contradictions=[contradiction])
        style_role_failures = sorted(
            [circle_evidence[0].evidence_id]
            if evidence_roles[circle_evidence[0].evidence_id] != "OBSERVED_CIRCLE" else [])
        style_role_failures.extend(sorted(
            row.evidence_id for row in pair_evidence
            if evidence_roles[row.evidence_id] != "ORTHOGONAL_HIDDEN_PAIR"))
        if style_role_failures:
            contradiction = _contradiction(
                "BOUNDARY_STYLE_CONTRADICTION", CANDIDATE_KEYS[:-1],
                [circle_evidence[0].evidence_id,
                 *(row.evidence_id for row in pair_evidence)],
                style_role_failures,
                "circle or complementary pair evidence has an incompatible reviewed role",
                blocking=True)
            return None, _empty_result(
                "CONTRADICTORY", confirmation, [contradiction["reason"]],
                contradictions=[contradiction])

        support_relations = [row for row in cross_view_relationships
                             if row.get("evidence_type") ==
                             "CIRCLE_ORTHOGONAL_HIDDEN_PAIR_SUPPORT"]
        if len(support_relations) != 1:
            return None, _empty_result(
                "NOT_VALIDATED", confirmation,
                ["one explicit cylindrical cross-view relationship is required"])
        support_relation = support_relations[0]
        support_evidence_ids = {circle_evidence[0].evidence_id,
                                *(row.evidence_id for row in pair_evidence)}
        relation_sources = support_relation.get("source_evidence_ids", ())
        if (not isinstance(relation_sources, (list, tuple))
                or len(relation_sources) != len(support_evidence_ids)
                or set(relation_sources) != support_evidence_ids
                or support_relation.get("relationship") !=
                "SHARED_CENTER_AND_EQUAL_HALF_SEPARATION"):
            contradiction = _contradiction(
                "VIEW_CORRESPONDENCE_FAILURE", CANDIDATE_KEYS[:-1],
                sorted(support_evidence_ids | {str(support_relation["relationship_id"])}),
                [*support_relation.get("source_evidence_ids", ()),
                 str(support_relation["relationship_id"])],
                "cylindrical relationship does not bind the exact observations", blocking=True)
            return None, _empty_result(
                "CONTRADICTORY", confirmation, [contradiction["reason"]],
                contradictions=[contradiction])

        contradictions: list[dict[str, Any]] = []
        consumers: dict[tuple[str, str], set[str]] = defaultdict(set)
        unresolved: set[tuple[str, str]] = set()
        for source in base_sources:
            consumers[("PRIMITIVE", source)].update({"BODY_EXTENT_CORRESPONDENCE", "R001_BASE_BLOCK"})
        for row in base_evidence:
            consumers[("FEATURE_EVIDENCE", row.evidence_id)].update(
                {"BODY_EXTENT_CORRESPONDENCE", "R001_BASE_BLOCK"})
        for identity in profile_ids:
            consumers[("DERIVED_PRIMITIVE", identity)].add("BODY_EXTENT_CORRESPONDENCE")
        consumers[("RELATIONSHIP", str(profile_relation["relationship_id"]))].add(
            "BODY_EXTENT_CORRESPONDENCE")
        consumers[("PRIMITIVE", circle_id)].update({"LOOP_CLOSURE", "LOOP_TOPOLOGY"})
        consumers[("FEATURE_EVIDENCE", circle_evidence[0].evidence_id)].update(
            {"CYLINDRICAL_AXIS_CORRESPONDENCE", "DIAMETER_CORRESPONDENCE", "LOOP_CONTINUITY"})
        consumers[("RELATIONSHIP", str(support_relation["relationship_id"]))].add(
            "PROJECTED_BOUNDARY_CORRESPONDENCE")

        circle_radius = float(circle.diameter) / 2.0
        circle_bounds = {
            "x": (float(circle.x) - circle_radius, float(circle.x) + circle_radius),
            "y": (float(circle.y) - circle_radius, float(circle.y) + circle_radius),
        }
        if (not math.isfinite(circle_radius) or circle_radius <= 0
                or not all(intervals[axis][0] + POSITION_TOLERANCE_MM < circle_bounds[axis][0]
                            and circle_bounds[axis][1]
                            < intervals[axis][1] - POSITION_TOLERANCE_MM
                            for axis in ("x", "y"))):
            contradiction = _contradiction(
                "MATERIAL_SIGN_CONTRADICTION", CANDIDATE_KEYS[:-1],
                [circle_evidence[0].evidence_id, *sorted(profile_ids),
                 *sorted(base_evidence_ids), str(profile_relation["relationship_id"])],
                [circle_evidence[0].evidence_id, *sorted(profile_ids),
                 *sorted(base_evidence_ids), str(profile_relation["relationship_id"])],
                "front circle is not a strictly internal loop", blocking=True)
            contradictions.append(contradiction)

        pair_sources: set[str] = set()
        axis_mismatch: set[str] = set()
        diameter_mismatch: set[str] = set()
        correspondence_failure: set[str] = set()
        style_failure: set[str] = set()
        span_failure: set[str] = set()
        expected = {"top": ("X", float(circle.x), circle_bounds["x"], "Y"),
                    "left": ("Y", float(circle.y), circle_bounds["y"], "X")}
        evidence_for_pair = pair_rows
        for view_name in ("top", "left"):
            pair = pair_by_view[view_name]
            pair_id = str(pair["primitive_id"])
            pair_row = evidence_for_pair.get(view_name)
            source_ids = [str(value) for value in pair.get("source_primitive_ids", ())]
            offsets = pair.get("offsets_mm")
            shared_axis, expected_center, expected_offsets, expected_orientation = expected[view_name]
            required = [circle_evidence[0].evidence_id, pair_row.evidence_id]
            if (pair.get("shared_coordinate") != shared_axis
                    or not isinstance(offsets, list) or len(offsets) != 2
                    or len(source_ids) != 2 or len(set(source_ids)) != 2):
                correspondence_failure.update(required)
                continue
            offsets = sorted(float(value) for value in offsets)
            midpoint = sum(offsets) / 2.0
            if not _close(midpoint, expected_center):
                axis_mismatch.update(required)
            if not _close(offsets[1] - offsets[0], float(circle.diameter),
                          2.0 * PAIR_TOLERANCE_MM):
                diameter_mismatch.update(required)
            if any(not _close(offsets[index], expected_offsets[index], PAIR_TOLERANCE_MM)
                   for index in (0, 1)):
                if _close(midpoint, expected_center):
                    diameter_mismatch.update(required)
                else:
                    axis_mismatch.update(required)
            constants = []
            for source in source_ids:
                item = primitives.get(source)
                if item is None:
                    correspondence_failure.add(source)
                    continue
                actual_view, kind, line = item
                if actual_view != view_name:
                    correspondence_failure.add(source)
                if kind != "HIDDEN_LINE":
                    style_failure.add(source)
                try:
                    orientation, constant, span = _constant_and_span(line.__dict__)
                except ValueError:
                    correspondence_failure.add(source)
                    continue
                if orientation != expected_orientation:
                    correspondence_failure.add(source)
                constants.append(constant)
                if any(not _close(span[index], intervals["z"][index]) for index in (0, 1)):
                    span_failure.add(source)
                pair_sources.add(source)
                consumers[("PRIMITIVE", source)].update(
                    {"PROJECTED_BOUNDARY_CORRESPONDENCE", "FULL_SPAN_CANDIDATE"})
            if len(constants) == 2 and any(
                    not _close(sorted(constants)[index], offsets[index]) for index in (0, 1)):
                correspondence_failure.update(source_ids)
            consumers[("DERIVED_PRIMITIVE", pair_id)].update(
                {"CYLINDRICAL_AXIS_CORRESPONDENCE", "DIAMETER_CORRESPONDENCE",
                 "FULL_SPAN_CANDIDATE", "LOOP_CONTINUITY"})
            consumers[("FEATURE_EVIDENCE", pair_row.evidence_id)].update(
                {"CYLINDRICAL_AXIS_CORRESPONDENCE", "DIAMETER_CORRESPONDENCE",
                 "FULL_SPAN_CANDIDATE", "LOOP_CONTINUITY"})

        geometric_required = sorted(
            support_evidence_ids | pair_sources
            | {str(row["primitive_id"]) for row in pairs}
            | {str(support_relation["relationship_id"])})
        if correspondence_failure:
            contradictions.append(_contradiction(
                "VIEW_CORRESPONDENCE_FAILURE", CANDIDATE_KEYS[:-1], geometric_required,
                sorted(correspondence_failure),
                "pair sources do not resolve to the declared projected boundaries", blocking=True))
        if style_failure:
            contradictions.append(_contradiction(
                "BOUNDARY_STYLE_CONTRADICTION", CANDIDATE_KEYS[:-1], geometric_required,
                sorted(style_failure), "cylindrical complementary boundaries are not hidden",
                blocking=True))
        if axis_mismatch:
            contradictions.append(_contradiction(
                "AXIS_MISMATCH", CANDIDATE_KEYS[:-1], geometric_required,
                sorted(axis_mismatch), "orthogonal pair midpoint disagrees with the circle axis",
                blocking=True))
        if diameter_mismatch:
            contradictions.append(_contradiction(
                "DIAMETER_MISMATCH", CANDIDATE_KEYS[:-1], geometric_required,
                sorted(diameter_mismatch), "orthogonal pair separation disagrees with circle diameter",
                blocking=True))

        blocking_geometry = any(row["blocking"] for row in contradictions)
        material_signs = {"ADDITIVE", "SUBTRACTIVE"}
        depth_candidates = {"FINITE_DEPTH", "FULL_BODY_DEPTH"}
        eliminated: dict[str, list[str]] = defaultdict(list)

        terminals = _terminal_sources(
            projection, base_sources, pair_sources, pair_by_view, intervals["z"])
        evidence_by_reference = defaultdict(list)
        for row in evidence:
            evidence_by_reference[row.geometry_reference].append(row.evidence_id)
        for source in terminals:
            consumers[("PRIMITIVE", source)].add("TERMINATION_PRESENT")
            for evidence_id in evidence_by_reference[source]:
                consumers[("FEATURE_EVIDENCE", evidence_id)].add("TERMINATION_PRESENT")

        for identity, (_view, kind, _primitive) in primitives.items():
            if kind == "CENTERLINE":
                unresolved.add(("PRIMITIVE", identity))
                for evidence_id in evidence_by_reference[identity]:
                    unresolved.add(("FEATURE_EVIDENCE", evidence_id))
        ledger, unconsumed_items = _ledger(
            primitives, evidence, derived_primitives, cross_view_relationships,
            consumers, unresolved)
        if unconsumed_items:
            observed = [identity for _kind, identity in unconsumed_items]
            contradictions.append(_contradiction(
                "UNCONSUMED_RELEVANT_EVIDENCE", CANDIDATE_KEYS[:-1],
                geometric_required + sorted(profile_ids)
                + [str(profile_relation["relationship_id"])], observed,
                "relevant geometry or relation remains outside the bounded explanation",
                blocking=False))

        # Material sign may resolve only after every relevant observation has
        # an auditable consumer.  Depth elimination likewise follows, rather
        # than preceding, the bounded subtractive decision.
        if not blocking_geometry and not unconsumed_items:
            material_signs = {"SUBTRACTIVE"}
            eliminated["CYLINDRICAL_ADDITIVE"].append("MATERIAL_SIGN_CONTRADICTION")
            qualified_sources = sorted(
                set(geometric_required) | profile_ids | base_evidence_ids
                | {str(profile_relation["relationship_id"])})
            contradictions.append(_contradiction(
                "MATERIAL_SIGN_CONTRADICTION", ["CYLINDRICAL_ADDITIVE"],
                qualified_sources, qualified_sources,
                "bounded internal-solid and complementary-hidden pattern rejects additive matter",
                blocking=False))
            if terminals and not span_failure:
                eliminated["CYLINDRICAL_SUBTRACTIVE_FINITE_DEPTH"].append(
                    "FINITE_DEPTH_CONTRADICTED_BY_FULL_SPAN")
                contradictions.append(_contradiction(
                    "FINITE_DEPTH_CONTRADICTED_BY_FULL_SPAN",
                    ["CYLINDRICAL_SUBTRACTIVE_FINITE_DEPTH"], qualified_sources,
                    sorted(pair_sources),
                    "both orthogonal boundary pairs positively span the complete base depth",
                    blocking=False))
                depth_candidates = set()
                eliminated["CYLINDRICAL_SUBTRACTIVE_FULL_BODY_DEPTH"].append(
                    "TERMINATION_PRESENT")
                contradictions.append(_contradiction(
                    "TERMINATION_PRESENT", ["CYLINDRICAL_SUBTRACTIVE_FULL_BODY_DEPTH"],
                    qualified_sources, sorted(terminals | {
                        evidence_id for source in terminals
                        for evidence_id in evidence_by_reference[source]}),
                    "interior terminal contradicts positively full-span walls", blocking=True))
            elif terminals:
                depth_candidates = {"FINITE_DEPTH"}
                eliminated["CYLINDRICAL_SUBTRACTIVE_FULL_BODY_DEPTH"].append(
                    "TERMINATION_PRESENT")
                contradictions.append(_contradiction(
                    "TERMINATION_PRESENT", ["CYLINDRICAL_SUBTRACTIVE_FULL_BODY_DEPTH"],
                    qualified_sources, sorted(terminals | {
                        evidence_id for source in terminals
                        for evidence_id in evidence_by_reference[source]}),
                    "positive interior terminal boundary rejects full-body depth", blocking=False))
            elif span_failure:
                depth_candidates = set()
                eliminated["CYLINDRICAL_SUBTRACTIVE_FULL_BODY_DEPTH"].append(
                    "BODY_SPAN_MISMATCH")
                contradictions.append(_contradiction(
                    "BODY_SPAN_MISMATCH", ["CYLINDRICAL_SUBTRACTIVE_FULL_BODY_DEPTH"],
                    qualified_sources, sorted(span_failure),
                    "hidden boundary endpoints do not span the complete base depth", blocking=False))
            else:
                depth_candidates = {"FULL_BODY_DEPTH"}
                eliminated["CYLINDRICAL_SUBTRACTIVE_FINITE_DEPTH"].append(
                    "FINITE_DEPTH_CONTRADICTED_BY_FULL_SPAN")
                contradictions.append(_contradiction(
                    "FINITE_DEPTH_CONTRADICTED_BY_FULL_SPAN",
                    ["CYLINDRICAL_SUBTRACTIVE_FINITE_DEPTH"], qualified_sources,
                    sorted(pair_sources),
                    "both orthogonal boundary pairs positively span the complete base depth",
                    blocking=False))

        blocking_geometry = any(row["blocking"] for row in contradictions)
        if blocking_geometry:
            eliminated = defaultdict(list, {
                key: sorted({row["name"] for row in contradictions if row["blocking"]})
                for key in CANDIDATE_KEYS[:-1]
            })
            material_signs, depth_candidates = set(), set()
        candidates, outcome, next_action = _resolve_candidate_lifecycle(
            material_signs, depth_candidates, eliminated)
        if blocking_geometry:
            outcome, next_action = "CONTRADICTORY", None
        if unconsumed_items:
            outcome, next_action = "NOT_VALIDATED", None
        elif span_failure and not terminals and not blocking_geometry:
            outcome, next_action = "NOT_VALIDATED", "ASK_HUMAN"
            finite = next(row for row in candidates if row["candidate"] ==
                          "CYLINDRICAL_SUBTRACTIVE_FINITE_DEPTH")
            finite.update(state="UNRESOLVED", lifecycle=["GENERATED"])
            candidates.append({
                "candidate": CANDIDATE_KEYS[-1], "physical": False,
                "state": "SURVIVOR", "lifecycle": ["GENERATED", "SUPPORTED", "SURVIVOR"],
                "resolution": "EVIDENCE_UNRESOLVED", "eliminated_by": [],
            })

        if outcome != "RESOLVED":
            result = _empty_result(
                outcome, confirmation,
                [row["reason"] for row in contradictions],
                contradictions=contradictions,
                candidates=candidates,
                evidence_ledger=ledger,
                unconsumed=len(unconsumed_items),
                next_action=next_action,
            )
            result["derived_relations"] = [
                "BODY_EXTENT_CORRESPONDENCE",
                "CYLINDRICAL_AXIS_CORRESPONDENCE",
                "DIAMETER_CORRESPONDENCE",
                "PROJECTED_BOUNDARY_CORRESPONDENCE",
                "FULL_SPAN_CANDIDATE",
                "LOOP_CLOSURE",
                "LOOP_TOPOLOGY",
                "LOOP_CONTINUITY",
            ]
            return None, result

        hole_evidence_ids = sorted(support_evidence_ids)
        center_x = float(circle.x) - sum(intervals["x"]) / 2.0
        center_y = float(circle.y) - sum(intervals["y"]) / 2.0
        parameters = {
            "diameter_mm": float(circle.diameter),
            "center_x_mm": center_x,
            "center_y_mm": center_y,
            "axis": "Z",
            "through": True,
        }
        hole_id = _identity("THROUGH_HOLE", parameters, hole_evidence_ids)
        hole = Hole(
            float(circle.diameter), center_x, center_y, "Z", True,
            feature_id=hole_id,
            source_evidence_ids=hole_evidence_ids,
            dependencies=[base.feature_id],
        )
        graph = FeatureGraph(base, holes=[hole], status="NOT_VALIDATED")
        nodes = graph.to_feature_nodes()
        result = _empty_result(
            "RESOLVED", confirmation, [], contradictions=contradictions,
            candidates=candidates, evidence_ledger=ledger, unconsumed=0)
        # Preserve the frozen B010 candidate status while exposing the R002
        # machine outcome separately as RESOLVED.
        result.update(
            status="INFERRED",
            outcome="RESOLVED",
            feature_graph=graph.to_dict(),
            feature_nodes=sorted(
                (node.to_dict() for node in nodes), key=lambda row: row["feature_id"]),
            feature_provenance=sorted([
                {
                    "feature_id": base.feature_id,
                    "rule": "R001_CANONICAL_BASE_BLOCK",
                    "source_evidence_ids": sorted(base.source_evidence_ids),
                    "source_relationship_ids": [str(profile_relation["relationship_id"])],
                },
                {
                    "feature_id": hole.feature_id,
                    "rule": "R002_BOUNDED_SUBTRACTIVE_FULL_BODY_CYLINDER",
                    "source_evidence_ids": hole_evidence_ids,
                    "source_relationship_ids": [str(support_relation["relationship_id"])],
                },
            ], key=lambda row: row["feature_id"]),
            derived_relations=[
                "BODY_EXTENT_CORRESPONDENCE",
                "CYLINDRICAL_AXIS_CORRESPONDENCE",
                "DIAMETER_CORRESPONDENCE",
                "PROJECTED_BOUNDARY_CORRESPONDENCE",
                "FULL_SPAN_CANDIDATE",
                "LOOP_CLOSURE",
                "LOOP_TOPOLOGY",
                "LOOP_CONTINUITY",
            ],
        )
        result["checks"].update(
            feature_count=2,
            physical_survivor_count=1,
            mandatory_gate_failures=0,
            unresolved_reference_count=0,
            unconsumed_evidence_count=0,
            elimination_contradiction_count=2,
            blocking_contradiction_count=0,
            base_profile_count=3,
            circle_count=1,
            raw_hidden_line_count=4,
            hidden_pair_count=2,
            generation_truth_reads=GENERATION_TRUTH_READS,
        )
        return graph, result


def reason_from_reviewed_artifacts(
    png_path: str | Path,
    projection_candidate_path: str | Path,
    evidence_candidate_path: str | Path,
    overlay_path: str | Path,
    review_path: str | Path,
    confirmation_path: str | Path,
) -> tuple[FeatureGraph | None, dict[str, Any]]:
    """Enter R002 only from the exact inputs accepted by the frozen human gate."""
    try:
        confirmation = validate_confirmation(
            png_path, projection_candidate_path, evidence_candidate_path,
            overlay_path, review_path, confirmation_path,
        )
        if confirmation["decision"] != "CONFIRMED" or confirmation["corrections"]:
            return None, _empty_result(
                "NOT_VALIDATED", confirmation, ["unmodified CONFIRMED review is required"])

        sources = confirmation["sources"]
        artifact_paths = (
            ("image", png_path),
            ("projection_graph_candidate", projection_candidate_path),
            ("feature_evidence_candidate", evidence_candidate_path),
            ("review_overlay", overlay_path),
            ("review", review_path),
        )
        snapshots = {}
        for label, path in artifact_paths:
            payload = Path(path).read_bytes()
            if sha256(payload).hexdigest() != sources[label]["sha256"]:
                raise ValueError(f"reviewed {label} changed after validation")
            snapshots[label] = payload
        saved_confirmation = json.loads(Path(confirmation_path).read_bytes().decode("utf-8-sig"))
        if saved_confirmation != confirmation:
            raise ValueError("confirmation changed after validation")

        projection_candidate = json.loads(
            snapshots["projection_graph_candidate"].decode("utf-8-sig"))
        evidence_candidate = json.loads(
            snapshots["feature_evidence_candidate"].decode("utf-8-sig"))
        graph_data = projection_candidate["projection_graph"]

        # The frozen validator already checked these exact bytes and B009 regeneration.
        def view(name: str) -> ViewGeometry:
            row = graph_data[name]
            return ViewGeometry(
                name=row["name"],
                horizontal_extent=row["horizontal_extent"],
                vertical_extent=row["vertical_extent"],
                circles=[Circle(**item) for item in row["circles"]],
                visible_segments=[LineSegment(**item) for item in row["visible_segments"]],
                hidden_segments=[LineSegment(**item) for item in row["hidden_segments"]],
                arcs=[Arc(**item) for item in row["arcs"]],
                centerlines=[LineSegment(**item) for item in row["centerlines"]],
            )

        projection = ProjectionGraph(
            graph_data["projection"], view("front"), view("top"), view("left"),
            graph_data["coordinate_convention"],
        )
        circle_count = sum(len(getattr(projection, name).circles)
                           for name in ("front", "top", "left"))
        if circle_count > 1:
            result = _empty_result(
                "UNSUPPORTED", confirmation, ["explicit second feature is outside R002"])
        elif (projection_candidate["status"] != "EXTRACTED"
              or evidence_candidate["status"] != "INFERRED"):
            result = _empty_result(
                "NOT_VALIDATED", confirmation,
                ["reviewed B008/B009 evidence is incomplete"], next_action="ASK_HUMAN")
        else:
            evidence = [FeatureEvidence(**row)
                        for row in evidence_candidate["feature_evidence"]]
            graph, result = CylindricalReasoner()._reason_canonical(
                projection, evidence, confirmation,
                derived_primitives=evidence_candidate["derived_primitives"],
                cross_view_relationships=evidence_candidate["cross_view_relationships"],
                evidence_roles=evidence_candidate["roles"],
                capability_scope=CAPABILITY_SCOPE,
            )
            result["checks"]["runtime_source_artifact_reads"] = (
                "FROZEN_VALIDATOR_PLUS_6_SNAPSHOTS")
            return graph, result
        result["checks"]["runtime_source_artifact_reads"] = (
            "FROZEN_VALIDATOR_PLUS_6_SNAPSHOTS")
        return None, result
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        return None, _empty_result("NOT_VALIDATED", None, [str(exc)])


__all__ = [
    "CAPABILITY_SCOPE",
    "CONTRADICTION_NAMES",
    "CylindricalReasoner",
    "GENERATION_TRUTH_READS",
    "reason_from_reviewed_artifacts",
]
