from __future__ import annotations

import builtins
from dataclasses import replace
import inspect
import io
import json
import random

import pytest

from solidworks_main_skill.reasoning.r004_composition_reasoner import reason_composition
from solidworks_main_skill.reasoning.r004_contract import EvidenceBinding
from solidworks_main_skill.reasoning.r004_planning import plan_composition
from test_cad_reasoning_r004_composition import _c01, _c02, _c03, _input, _support


def _result(factory=_c01):
    result = reason_composition(factory())
    assert result.status == "INFERRED"
    return result


def _nodes_changed(result, nodes, edges=None):
    graph = replace(result.feature_graph_candidate, nodes=tuple(nodes),
                    dag_edges=result.dag_edges if edges is None else tuple(edges))
    return replace(result, feature_graph_candidate=graph,
                   dag_edges=result.dag_edges if edges is None else tuple(edges))


def _assert_rejected(result, code):
    planned = plan_composition(result)
    assert (planned.status, planned.error_code) == ("REJECTED", code)
    assert planned.modeling_plan is None
    assert planned.operation_requirements == ()


@pytest.mark.parametrize(("factory", "count"), [(_c01, 4), (_c02, 5), (_c03, 7)])
def test_nominal_variable_n_plan_and_contract(factory, count):
    source = _result(factory)
    planned = plan_composition(source)
    assert (planned.status, planned.error_code) == ("PLANNED", None)
    assert len(planned.modeling_plan.operations) == count
    assert len(planned.operation_requirements) == count
    assert [row.source_feature_id for row in planned.modeling_plan.operations] == list(source.canonical_order)
    assert planned.selected_history_id == source.selected_history_id
    assert planned.legal_ordering_count == source.legal_ordering_count
    assert planned.legal_history_count == len(source.history_candidates)
    assert planned.history_selection_source == "UNIQUE_MACHINE_CANDIDATE"
    assert planned.generation_truth_reads == 0
    for operation, requirements in zip(planned.modeling_plan.operations,
                                       planned.operation_requirements):
        assert requirements.operation_id == operation.operation_id
        assert operation.sketch_plane == requirements.datum.token
        assert operation.sketch_plane.startswith("datum:")
        assert requirements.evidence_ids
        assert {"DOMAIN_VALID", "TOPOLOGY_VALID", "DATUM_AVAILABLE"} <= {
            condition.kind for condition in requirements.preconditions}
    assert not any(value in json.dumps(planned.to_dict()) for value in
                   ("Front Plane", "Top Plane", "Right Plane", "Plane2", "Face1", "SelectByID2"))


def test_nested_host_same_type_siblings_and_legal_order_preserved():
    source = _result(_c03)
    planned = plan_composition(source)
    by_region = {row.region_id: row.feature_id for row in source.feature_hypotheses}
    by_feature = {row.source_feature_id: row for row in planned.modeling_plan.operations}
    by_requirement = {row.datum.feature_id: row for row in planned.operation_requirements}
    assert by_feature[by_region["boss-c"]].depends_on_operation_ids == ["op_" + by_region["boss-a"]]
    assert by_feature[by_region["hole-a"]].depends_on_operation_ids == ["op_" + by_region["boss-c"]]
    assert by_feature[by_region["boss-b"]].type == by_feature[by_region["boss-c"]].type
    assert by_requirement[by_region["hole-a"]].datum.host_feature == by_region["boss-c"]
    assert planned.legal_ordering_count > 2
    cut = by_feature[by_region["hole-a"]]
    assert cut.profile["region_bounds_mm"][4:] == (20.0, 40.0)
    assert cut.profile["extent_scope"] == "REVIEWED_REGION"
    assert cut.depth_mm == 20.0
    assert cut.direction == "THROUGH_REGION_Z"


def test_multiple_histories_need_r004_a_selection():
    source = _c01()
    supports = (_support("boss-a", "base"), _support("boss-b", "base"),
                _support("hole-a", "boss-a", "boss-b", equivalent=True))
    reviewed = _input(source.regions, supports)
    ambiguous = reason_composition(reviewed)
    _assert_rejected(ambiguous, "UNRESOLVED_HISTORY")
    selected = reason_composition(replace(reviewed, selected_history_id=ambiguous.history_candidates[1].history_id))
    planned = plan_composition(selected)
    assert planned.status == "PLANNED"
    assert planned.selected_history_id == selected.selected_history_id
    assert planned.history_selection_source == "HUMAN_SELECTED_MACHINE_CANDIDATE"
    assert planned.legal_history_count == 2


def test_negative_matrix_missing_host():
    source = _result()
    history = source.history_candidates[0]
    pairs = list(history.host_assignments)
    pairs[0] = (pairs[0][0], "absent-host")
    _assert_rejected(replace(source, history_candidates=(replace(history, host_assignments=tuple(pairs)),)),
                     "MISSING_HOST")


def test_negative_matrix_missing_datum():
    source = _result()
    _assert_rejected(replace(source, datum_requirements=source.datum_requirements[1:]),
                     "MISSING_DATUM")


def test_negative_matrix_unresolved_history():
    source = _result()
    _assert_rejected(replace(source, selected_history_id=None), "UNRESOLVED_HISTORY")


def test_negative_matrix_cyclic_dependency():
    source = _result()
    nodes = list(source.feature_graph_candidate.nodes)
    base = next(node for node in nodes if node.feature_type == "BASE_BLOCK")
    child = next(node for node in nodes if base.feature_id in node.dependencies)
    nodes = [replace(node, dependencies=[child.feature_id]) if node.feature_id == base.feature_id
             else node for node in nodes]
    _assert_rejected(_nodes_changed(source, nodes, (*source.dag_edges, (child.feature_id, base.feature_id))),
                     "CYCLIC_DEPENDENCY")


def test_negative_matrix_invalid_dependency_reference():
    source = _result()
    nodes = [replace(node, dependencies=["missing-feature"]) if node.feature_type == "BOSS" else node
             for node in source.feature_graph_candidate.nodes]
    _assert_rejected(_nodes_changed(source, nodes), "INVALID_DEPENDENCY_REFERENCE")


def test_negative_matrix_datum_not_available_yet():
    source = _result()
    by_region = {row.region_id: row.feature_id for row in source.feature_hypotheses}
    datums = tuple(replace(row, host_feature=by_region["boss-b"])
                   if row.feature_id == by_region["hole-a"] else row
                   for row in source.datum_requirements)
    _assert_rejected(replace(source, datum_requirements=datums), "DATUM_NOT_AVAILABLE_YET")


def test_negative_matrix_unsupported_datum_kind():
    source = _result()
    datums = (replace(source.datum_requirements[0], required_datum_kind="SPHERICAL_SUPPORT"),
              *source.datum_requirements[1:])
    _assert_rejected(replace(source, datum_requirements=datums), "UNSUPPORTED_DATUM_KIND")


def test_negative_matrix_conflicting_preconditions():
    source = _result()
    hypothesis = replace(source.feature_hypotheses[0], material_relation="REMOVES_MATERIAL")
    _assert_rejected(replace(source, feature_hypotheses=(hypothesis, *source.feature_hypotheses[1:])),
                     "CONFLICTING_PRECONDITIONS")


def test_negative_matrix_out_of_domain_plan():
    source = _result()
    nodes = [replace(node, feature_type="POCKET") if node.feature_type == "BOSS" else node
             for node in source.feature_graph_candidate.nodes]
    _assert_rejected(_nodes_changed(source, nodes), "OUT_OF_DOMAIN_PLAN")


def test_negative_matrix_ambiguous_execution_binding():
    source = _result()
    second = replace(source.datum_requirements[0], support_region="another-support")
    _assert_rejected(replace(source, datum_requirements=(*source.datum_requirements, second)),
                     "AMBIGUOUS_EXECUTION_BINDING")


@pytest.mark.parametrize("reference", ["Front Plane", "surface:Plane2", "region:Face<1>"])
def test_concrete_cad_reference_cannot_enter_abstract_plan(reference):
    source = _result()
    datums = (replace(source.datum_requirements[0], support_region=reference),
              *source.datum_requirements[1:])
    _assert_rejected(replace(source, datum_requirements=datums), "AMBIGUOUS_EXECUTION_BINDING")


def test_abstract_region_identifier_does_not_require_a_fixture_prefix():
    source = _result()
    datums = (replace(source.datum_requirements[0], support_region="lug-support-surface"),
              *source.datum_requirements[1:])
    assert plan_composition(replace(source, datum_requirements=datums)).status == "PLANNED"


def test_incomplete_geometry_is_rejected_even_if_hypothesis_matches():
    source = _result()
    node = next(node for node in source.feature_graph_candidate.nodes if node.feature_type == "THROUGH_HOLE")
    parameters = dict(node.parameters)
    parameters.pop("center_x_mm")
    changed = _nodes_changed(source, (replace(row, parameters=parameters)
                                     if row.feature_id == node.feature_id else row
                                     for row in source.feature_graph_candidate.nodes))
    hypotheses = tuple(replace(row, geometry_summary=tuple(sorted(parameters.items())))
                       if row.feature_id == node.feature_id else row for row in source.feature_hypotheses)
    _assert_rejected(replace(changed, feature_hypotheses=hypotheses), "OUT_OF_DOMAIN_PLAN")


@pytest.mark.parametrize("factory", [_c01, _c02, _c03])
def test_twenty_repetitions_are_byte_equivalent(factory):
    expected = json.dumps(plan_composition(_result(factory)).to_dict(), sort_keys=True)
    assert all(json.dumps(plan_composition(_result(factory)).to_dict(), sort_keys=True) == expected
               for _ in range(20))


@pytest.mark.parametrize("factory", [_c01, _c02, _c03])
def test_twenty_permutations_are_invariant(factory):
    # Multiple bound evidence IDs make the provenance permutation check substantive.
    reviewed = factory()
    extras = tuple(EvidenceBinding("extra:" + row.region_id, "a" * 64) for row in reviewed.regions)
    reviewed = replace(reviewed, evidence=(*reviewed.evidence, *extras), regions=tuple(
        replace(row, evidence_ids=(*row.evidence_ids, "extra:" + row.region_id)) for row in reviewed.regions))
    source = reason_composition(reviewed)
    assert source.status == "INFERRED"
    assert all(len(node.source_evidence_ids) >= 2 for node in source.feature_graph_candidate.nodes)
    expected = json.dumps(plan_composition(source).to_dict(), sort_keys=True)
    for seed in range(20):
        rng = random.Random(seed)
        def shuffled(values):
            values = list(values)
            rng.shuffle(values)
            return tuple(values)
        nodes = shuffled(replace(node, source_evidence_ids=list(shuffled(node.source_evidence_ids)))
                         for node in source.feature_graph_candidate.nodes)
        permuted = replace(source,
                           feature_graph_candidate=replace(source.feature_graph_candidate, nodes=nodes,
                                                           dag_edges=shuffled(source.dag_edges)),
                           dag_edges=shuffled(source.dag_edges),
                           datum_requirements=shuffled(source.datum_requirements),
                           legal_orderings=shuffled(source.legal_orderings))
        assert json.dumps(plan_composition(permuted).to_dict(), sort_keys=True) == expected


@pytest.mark.parametrize("factory", [_c01, _c02, _c03])
def test_whole_model_translation_is_plan_equivalent(factory):
    reviewed = factory()
    translated = replace(reviewed, regions=tuple(replace(row, bounds_mm=(
        row.bounds_mm[0] + 137, row.bounds_mm[1] + 137,
        row.bounds_mm[2] - 42, row.bounds_mm[3] - 42,
        row.bounds_mm[4] + 19, row.bounds_mm[5] + 19,
    )) for row in reviewed.regions))
    left = plan_composition(reason_composition(reviewed))
    right = plan_composition(reason_composition(translated))
    assert left.status == right.status == "PLANNED"
    assert left.to_dict() == right.to_dict()


def test_planner_performs_no_file_reads(monkeypatch):
    source = _result()
    def guarded_open(*args, **kwargs):
        raise AssertionError("planner attempted a file read")
    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(io, "open", guarded_open)
    assert plan_composition(source).status == "PLANNED"
    assert plan_composition(source).generation_truth_reads == 0


def test_production_source_has_no_case_or_evaluator_branch():
    from solidworks_main_skill.reasoning import r004_planning
    source = inspect.getsource(r004_planning).lower()
    forbidden = ("r004-c01", "r004-c02", "r004-c03", "r003", "b010-002",
                 "expected.json", "golden", "known_answer")
    assert not any(token in source for token in forbidden)
    assert source.count("truth") == source.count("generation_truth_reads")
