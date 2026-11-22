from __future__ import annotations

import builtins
from dataclasses import replace
from hashlib import sha256
import inspect
import json
import random
import re

import pytest

from solidworks_main_skill.reasoning.r004_composition_reasoner import reason_composition
from solidworks_main_skill.reasoning.r004_contract import (
    EvidenceBinding,
    GeometricRelation,
    PrimitiveObservation,
    RegionObservation,
    ReviewedCompositionInput,
    SupportRelation,
)


def _region(region_id, shape, bounds, material="PRESENT", *, full_span=False):
    return RegionObservation(
        region_id=region_id,
        shape_kind=shape,
        bounds_mm=bounds,
        material_state=material,
        evidence_ids=(f"ev:{region_id}",),
        primitive_ids=(f"primitive:{region_id}",),
        full_span=full_span,
    )


def _support(child, *hosts, equivalent=False):
    return SupportRelation(
        relation_id=f"support:{child}",
        child_region_id=child,
        candidate_host_region_ids=tuple(hosts),
        support_region_id=f"surface:{child}",
        orientation="Z+",
        evidence_ids=(f"ev:{child}",),
        history_equivalent=equivalent,
    )


def _input(regions, supports=(), **changes):
    regions = tuple(regions)
    kind = {
        "RECTANGULAR_PRISM": "RECTANGULAR_PROFILE",
        "CIRCULAR_PRISM": "CIRCLE",
        "STRAIGHT_SLOT_PRISM": "STRAIGHT_SLOT_PROFILE",
    }
    evidence = tuple(
        EvidenceBinding(f"ev:{row.region_id}", sha256(row.region_id.encode()).hexdigest())
        for row in regions
    )
    primitives = tuple(
        PrimitiveObservation(
            f"primitive:{row.region_id}", kind.get(row.shape_kind, "UNSUPPORTED"), row.region_id,
            (f"ev:{row.region_id}",),
        ) for row in regions
    )
    relations = tuple(
        GeometricRelation(
            f"geometry:{support.child_region_id}:{host}", "SUPPORTED_BY",
            support.child_region_id, host, (f"ev:{support.child_region_id}",),
        )
        for support in supports for host in support.candidate_host_region_ids
    )
    values = {
        "domain": "CONNECTED_SINGLE_SOLID_PART",
        "coordinate_convention": "MODEL_XYZ_MM_RIGHT_HANDED",
        "evidence": evidence,
        "primitives": primitives,
        "regions": regions,
        "geometric_relations": relations,
        "support_relations": tuple(supports),
        "confirmation_state": "CONFIRMED",
    }
    values.update(changes)
    return ReviewedCompositionInput(**values)


def _c01():
    regions = (
        _region("base", "RECTANGULAR_PRISM", (0, 100, 0, 60, 0, 20)),
        _region("boss-a", "RECTANGULAR_PRISM", (10, 30, 10, 30, 20, 30)),
        _region("boss-b", "RECTANGULAR_PRISM", (60, 80, 30, 50, 20, 28)),
        _region("hole-a", "CIRCULAR_PRISM", (15, 25, 15, 25, 20, 30), "ABSENT", full_span=True),
    )
    supports = (_support("boss-a", "base"), _support("boss-b", "base"),
                _support("hole-a", "boss-a"))
    return _input(regions, supports)


def _c02():
    regions = (
        _region("base", "RECTANGULAR_PRISM", (0, 120, 0, 80, 0, 20)),
        _region("boss-a", "RECTANGULAR_PRISM", (40, 80, 20, 60, 20, 35)),
        _region("hole-base", "CIRCULAR_PRISM", (10, 20, 10, 20, 0, 20), "ABSENT", full_span=True),
        _region("hole-boss", "CIRCULAR_PRISM", (52, 62, 32, 42, 20, 35), "ABSENT", full_span=True),
        _region("slot-base", "STRAIGHT_SLOT_PRISM", (85, 110, 50, 58, 0, 20), "ABSENT", full_span=True),
    )
    supports = (_support("boss-a", "base"), _support("hole-base", "base"),
                _support("hole-boss", "boss-a"), _support("slot-base", "base"))
    return _input(regions, supports)


def _c03():
    regions = (
        _region("base", "RECTANGULAR_PRISM", (0, 140, 0, 90, 0, 20)),
        _region("boss-a", "RECTANGULAR_PRISM", (10, 50, 10, 45, 20, 32)),
        _region("boss-b", "RECTANGULAR_PRISM", (85, 125, 40, 75, 20, 30)),
        _region("boss-c", "RECTANGULAR_PRISM", (20, 40, 18, 38, 32, 40)),
        _region("hole-a", "CIRCULAR_PRISM", (25, 35, 23, 33, 20, 40), "ABSENT", full_span=True),
        _region("hole-b", "CIRCULAR_PRISM", (98, 108, 53, 63, 20, 30), "ABSENT", full_span=True),
        _region("slot-a", "STRAIGHT_SLOT_PRISM", (55, 78, 65, 73, 0, 20), "ABSENT", full_span=True),
    )
    supports = (_support("boss-a", "base"), _support("boss-b", "base"),
                _support("boss-c", "boss-a"), _support("hole-a", "boss-c"),
                _support("hole-b", "boss-b"), _support("slot-a", "base"))
    return _input(regions, supports)


@pytest.mark.parametrize(("factory", "count"), [(_c01, 4), (_c02, 5), (_c03, 7)])
def test_nominal_variable_n_cases_infer_graph(factory, count):
    result = reason_composition(factory())

    assert result.status == "INFERRED"
    assert result.error_code is None
    assert len(result.feature_hypotheses) == count
    assert len(result.feature_graph_candidate.nodes) == count
    assert len(result.dag_edges) == count - 1
    assert result.legal_ordering_count >= 2
    assert result.feature_graph_candidate.status == "NOT_VALIDATED"
    assert result.generation_truth_reads == 0


def test_c01_has_additive_siblings_and_nested_hole_dependency():
    result = reason_composition(_c01())
    by_region = {row.region_id: row for row in result.feature_hypotheses}

    assert by_region["boss-a"].feature_type == by_region["boss-b"].feature_type == "BOSS"
    assert (by_region["boss-a"].feature_id, by_region["hole-a"].feature_id) in result.dag_edges


def test_c02_has_multiple_subtractive_candidates_and_host_differentiation():
    result = reason_composition(_c02())
    by_region = {row.region_id: row for row in result.feature_hypotheses}
    subtractive = [row for row in by_region.values() if row.material_relation == "REMOVES_MATERIAL"]
    dependencies = {node.feature_id: node.dependencies for node in result.feature_graph_candidate.nodes}

    assert len(subtractive) == 3
    assert dependencies[by_region["hole-base"].feature_id] != dependencies[by_region["hole-boss"].feature_id]


def test_c03_has_non_base_dependency_and_multiple_legal_orders():
    result = reason_composition(_c03())
    by_region = {row.region_id: row for row in result.feature_hypotheses}

    assert (by_region["boss-a"].feature_id, by_region["boss-c"].feature_id) in result.dag_edges
    assert result.legal_ordering_count > 2
    assert result.canonical_order == result.legal_orderings[0]


def test_n01_disconnected_solids_fail_closed():
    result = reason_composition(replace(_c01(), body_count=2))
    assert (result.status, result.error_code, result.feature_graph_candidate) == (
        "UNSUPPORTED", "DISCONNECTED_SOLIDS", None)


def test_n02_multiple_base_candidates_fail_closed():
    source = _c01()
    extra = _region("base-other", "RECTANGULAR_PRISM", (200, 220, 0, 20, 0, 20))
    result = reason_composition(_input((*source.regions, extra), source.support_relations))
    assert result.error_code == "MULTIPLE_BASE_CANDIDATES"
    assert result.feature_graph_candidate is None


def test_n03_cyclic_dependency_fail_closed():
    source = _c01()
    supports = (_support("boss-a", "boss-b"), _support("boss-b", "boss-a"),
                _support("hole-a", "boss-a"))
    result = reason_composition(_input(source.regions, supports))
    assert result.error_code == "CYCLIC_DEPENDENCY"
    assert result.feature_graph_candidate is None


def test_n04_ambiguous_host_fail_closed():
    source = _c01()
    supports = (_support("boss-a", "base"), _support("boss-b", "base"),
                _support("hole-a", "boss-a", "boss-b"))
    result = reason_composition(_input(source.regions, supports))
    assert (result.status, result.error_code) == ("AMBIGUOUS", "AMBIGUOUS_HOST")


def test_n05_insufficient_evidence_fail_closed():
    result = reason_composition(replace(_c01(), confirmation_state="PENDING"))
    assert result.error_code == "INSUFFICIENT_EVIDENCE"


def test_n06_unsupported_feature_family_fail_closed():
    source = _c01()
    pocket = _region("rect-cut", "RECTANGULAR_PRISM", (35, 45, 35, 45, 0, 20), "ABSENT",
                     full_span=True)
    result = reason_composition(_input((*source.regions, pocket),
                                       (*source.support_relations, _support("rect-cut", "base"))))
    assert result.error_code == "UNSUPPORTED_FEATURE_FAMILY"


def test_n07_non_axis_aligned_geometry_fail_closed():
    source = _c01()
    primitive = replace(source.primitives[0], axis_aligned=False)
    result = reason_composition(replace(source, primitives=(primitive, *source.primitives[1:])))
    assert result.error_code == "NON_AXIS_ALIGNED_GEOMETRY"


def test_n08_conflicting_evidence_fail_closed():
    source = _c01()
    regions = tuple(replace(row, material_state="CONFLICTING") if row.region_id == "boss-a" else row
                    for row in source.regions)
    result = reason_composition(replace(source, regions=regions))
    assert result.error_code == "CONFLICTING_EVIDENCE"


def test_n09_out_of_domain_topology_fail_closed():
    result = reason_composition(replace(_c01(), topology_kind="FREEFORM_SURFACE"))
    assert result.error_code == "OUT_OF_DOMAIN_TOPOLOGY"


def test_n10_multiple_legal_histories_require_confirmation_then_accept_generated_choice():
    source = _c01()
    supports = (_support("boss-a", "base"), _support("boss-b", "base"),
                _support("hole-a", "boss-a", "boss-b", equivalent=True))
    ambiguous_input = _input(source.regions, supports)
    ambiguous = reason_composition(ambiguous_input)

    assert ambiguous.status == "AMBIGUOUS"
    assert ambiguous.error_code == "HISTORY_AMBIGUITY_REQUIRES_CONFIRMATION"
    assert ambiguous.history_status == "MULTIPLE_LEGAL_HISTORIES"
    assert len(ambiguous.history_candidates) == 2
    assert ambiguous.feature_graph_candidate is None

    selected = reason_composition(replace(
        ambiguous_input, selected_history_id=ambiguous.history_candidates[0].history_id))
    assert selected.status == "INFERRED"
    assert selected.history_status == "MULTIPLE_LEGAL_HISTORIES"


@pytest.mark.parametrize("factory", [_c01, _c02, _c03])
def test_determinism_twenty_repetitions(factory):
    expected = json.dumps(reason_composition(factory()).to_dict(), sort_keys=True)
    assert all(json.dumps(reason_composition(factory()).to_dict(), sort_keys=True) == expected
               for _ in range(20))


def _permuted(source, seed):
    randomizer = random.Random(seed)

    def shuffled(values):
        values = list(values)
        randomizer.shuffle(values)
        return tuple(values)

    supports = shuffled(
        replace(row, candidate_host_region_ids=shuffled(row.candidate_host_region_ids))
        for row in source.support_relations
    )
    return replace(
        source,
        evidence=shuffled(source.evidence),
        primitives=shuffled(source.primitives),
        regions=shuffled(source.regions),
        geometric_relations=shuffled(source.geometric_relations),
        support_relations=supports,
    )


@pytest.mark.parametrize("factory", [_c01, _c02, _c03])
def test_input_permutation_invariance_twenty_permutations(factory):
    source = factory()
    expected = json.dumps(reason_composition(source).to_dict(), sort_keys=True)
    assert all(json.dumps(reason_composition(_permuted(source, seed)).to_dict(), sort_keys=True) == expected
               for seed in range(20))


@pytest.mark.parametrize("factory", [_c01, _c02, _c03])
def test_translation_metamorphic_equivalence(factory):
    source = factory()
    dx, dy, dz = 137.0, -42.0, 19.0
    translated_regions = tuple(
        replace(row, bounds_mm=(
            row.bounds_mm[0] + dx, row.bounds_mm[1] + dx,
            row.bounds_mm[2] + dy, row.bounds_mm[3] + dy,
            row.bounds_mm[4] + dz, row.bounds_mm[5] + dz,
        )) for row in source.regions
    )
    original = reason_composition(source)
    translated = reason_composition(replace(source, regions=translated_regions))

    assert translated.normalized_observations == original.normalized_observations
    assert translated.feature_hypotheses == original.feature_hypotheses
    assert translated.dag_edges == original.dag_edges
    assert translated.legal_orderings == original.legal_orderings
    assert translated.feature_graph_candidate.to_dict() == original.feature_graph_candidate.to_dict()


def test_truth_isolation_does_not_open_expected_file(tmp_path, monkeypatch):
    expected = tmp_path / "expected.json"
    expected.write_text('{"forbidden": true}', encoding="utf-8")
    original_open = builtins.open

    def guarded_open(file, *args, **kwargs):
        if str(file).endswith("expected.json"):
            raise AssertionError("runtime attempted evaluator read")
        return original_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)
    result = reason_composition(replace(_c01(), transport_metadata=(("path", str(expected)),)))
    assert result.status == "INFERRED"
    assert result.generation_truth_reads == 0


def test_runtime_has_no_case_specific_decision_branches():
    import solidworks_main_skill.reasoning.r004_composition_reasoner as runtime

    source = inspect.getsource(runtime).lower()
    forbidden = ("r003", "r004-c01", "r004-c02", "r004-c03", "case-000", "b010-002",
                 "expected.json", "golden", "known_answer")
    assert not {token for token in forbidden if token in source}
    assert not re.search(r"feature_count\s*==\s*[456]", source)


def test_datum_requirements_are_abstract_and_host_bound():
    result = reason_composition(_c03())
    by_id = {row.feature_id for row in result.feature_hypotheses}

    assert len(result.datum_requirements) == 6
    assert all(row.required_datum_kind == "PLANAR_SUPPORT" for row in result.datum_requirements)
    assert all(row.host_feature in by_id for row in result.datum_requirements)
    assert all("Plane" not in row.support_region and "Top" not in row.support_region
               for row in result.datum_requirements)
