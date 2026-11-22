from __future__ import annotations

import builtins
from dataclasses import replace
import inspect
import io
import json
import math
import random

import pytest

from solidworks_main_skill.backends.solidworks_automation import r004_execution_binding as binding
from solidworks_main_skill.reasoning.r004_composition_reasoner import reason_composition
from solidworks_main_skill.reasoning.r004_planning import plan_composition
from test_cad_reasoning_r004_composition import _c01, _c02, _c03, _input, _region, _support


class FakeBackend:
    """An independent analytic command consumer, never native evidence or a solid modeller."""
    mode = "FAKE"

    def __init__(self, *, angle=0.0, origin=(0.0, 0.0, 0.0), candidate_fault=None,
                 feature_fault=None, precondition=None, rebuild=True, cleanup=True):
        self.angle, self.origin = angle, origin
        self.candidate_fault, self.feature_fault = candidate_fault, feature_fault
        self.precondition = precondition
        self.rebuild_result, self.cleanup_result = rebuild, cleanup
        self.calls, self.features, self.measurements, self.completed = [], {}, {}, set()
        self.closed = False

    def begin(self):
        self.calls.append("begin")
        return "fake-document"

    def datum_candidates(self, request):
        if request.host_feature_id is None:
            footprint, z = (-1.0, 1.0, -1.0, 1.0), self.origin[2]
            owner = None
        else:
            owner = self.features[request.host_feature_id].reference
            box = self.measurements[request.host_feature_id]
            footprint, z = box[:4], box[5]
        c, s = math.cos(self.angle), math.sin(self.angle)
        sign = request.normal[2]
        candidate = binding.DatumCandidate(
            reference="fake-support:" + request.token, kind=request.kind,
            support_region=request.support_region, owner_reference=owner,
            document_id="fake-document", origin_m=(0.007, -0.011, z),
            u=(c, s, 0.0), v=(-s * sign, c * sign, 0.0), normal=(0.0, 0.0, sign),
            footprint_m=tuple(footprint),
        )
        candidates = [candidate]
        if self.candidate_fault:
            candidates = self.candidate_fault(request, candidates)
        return candidates

    def check_preconditions(self, requirements, completed):
        assert set(completed) == self.completed
        values = []
        for check in requirements.preconditions:
            if check.kind == "DEPENDENCY_COMPLETED":
                value = check.reference in self.completed
            elif check.kind in {"HOST_AVAILABLE", "MATERIAL_EXISTS"}:
                value = check.reference in self.features
            else:
                value = True
            values.append(value)
        return self.precondition(values) if self.precondition else values

    def create_feature(self, command):
        self.calls.append(command.operation_id)
        assert set(command.dependencies) <= self.completed
        receipt = binding.FeatureReceipt(command.operation_id, command.source_feature_id,
                                         "fake-feature:" + command.operation_id,
                                         "fake-sketch:" + command.operation_id,
                                         command.datum.reference, "fake-document")
        if self.feature_fault:
            return self.feature_fault(command, receipt)
        # Reconstruct extents from local primitive geometry; do not copy reviewed bounds.
        def world(point):
            return tuple(command.datum.origin_m[i] + point[0] * command.datum.u[i]
                         + point[1] * command.datum.v[i] for i in range(3))
        points = [world(point) for point in command.points_m]
        radius = (command.size_m if command.profile_kind == "CIRCLE" else command.size_m / 2
                  if command.profile_kind == "STRAIGHT_SLOT" else 0)
        z = points[0][2]
        end_z = z + command.model_direction[2] * command.depth_m
        self.measurements[command.source_feature_id] = (
            min(point[0] for point in points) - radius, max(point[0] for point in points) + radius,
            min(point[1] for point in points) - radius, max(point[1] for point in points) + radius,
            min(z, end_z), max(z, end_z),
        )
        self.features[command.source_feature_id] = receipt
        self.completed.add(command.operation_id)
        return receipt

    def rebuild(self, receipts):
        self.calls.append("rebuild")
        assert {row.source_feature_id for row in receipts} == set(self.features)
        return self.rebuild_result

    def close(self):
        self.calls.append("close")
        self.closed = True
        return self.cleanup_result


def _inputs(factory=_c01):
    composition = reason_composition(factory())
    planned = plan_composition(composition)
    assert planned.status == "PLANNED"
    return planned, composition


def _run(factory=_c01, backend=None, **kwargs):
    planned, composition = _inputs(factory)
    backend = backend or FakeBackend()
    return binding.exercise_binding(planned, composition, backend, **kwargs), backend


@pytest.mark.parametrize(("factory", "count"), [(_c01, 4), (_c02, 5), (_c03, 7)])
@pytest.mark.parametrize("angle", [0, math.pi / 2, 0.371])
def test_nominal_commands_reconstruct_reviewed_extents_in_rotated_sketch_frames(factory, count, angle):
    result, backend = _run(factory, FakeBackend(angle=angle))
    assert result.status == "SIMULATED", result.failure_reason
    assert len(result.receipts) == len(result.commands) == count
    for hypothesis in reason_composition(factory()).feature_hypotheses:
        observation = next(row for row in factory().regions if row.region_id == hypothesis.region_id)
        assert backend.measurements[hypothesis.feature_id] == pytest.approx(
            tuple(value / 1000 for value in observation.bounds_mm), abs=1e-12)
    assert backend.closed
    assert result.cleanup_status == "SIMULATED_CLOSED"
    assert result.engineering_status == "NOT_VALIDATED"
    assert result.real_qualification == "NOT_RUN" and result.real_sw_invoked is False
    assert all(row.evidence_kind == "SIMULATED" for row in result.receipts)
    assert result.generation_truth_reads == 0


def test_nested_host_uses_exact_receipt_and_local_cut_range():
    result, backend = _run(_c03)
    source = reason_composition(_c03())
    ids = {row.region_id: row.feature_id for row in source.feature_hypotheses}
    commands = {row.source_feature_id: row for row in result.commands}
    nested = commands[ids["boss-c"]]
    cut = commands[ids["hole-a"]]
    assert nested.datum.owner_reference == backend.features[ids["boss-a"]].reference
    assert cut.datum.owner_reference == backend.features[ids["boss-c"]].reference
    assert cut.depth_m == 0.020
    assert cut.datum.origin_m[2] == 0.040
    assert cut.model_direction == (0, 0, -1)
    assert backend.measurements[ids["hole-a"]][4:] == pytest.approx((0.020, 0.040))
    assert cut.reverse_from_sketch_normal is True
    assert nested.reverse_from_sketch_normal is False


def test_y_major_slot_uses_full_width_and_center_spacing_not_overall_length():
    def factory():
        return _input((
            _region("plate", "RECTANGULAR_PRISM", (0, 80, 0, 90, 0, 12)),
            _region("cut", "STRAIGHT_SLOT_PRISM", (25, 33, 15, 40, 0, 12),
                    "ABSENT", full_span=True),
        ), (_support("cut", "plate"),))
    result, backend = _run(factory, FakeBackend(angle=0.91))
    assert result.status == "SIMULATED"
    slot = next(row for row in result.commands if row.profile_kind == "STRAIGHT_SLOT")
    assert slot.size_m == 0.008
    assert math.dist(*slot.points_m) == pytest.approx(0.017)
    assert backend.measurements[slot.source_feature_id] == pytest.approx((.025, .033, .015, .040, 0, .012))


def test_selected_history_metadata_and_source_evidence_preserved():
    reviewed = _c01()
    reviewed = _input(reviewed.regions, (_support("boss-a", "base"), _support("boss-b", "base"),
                                       _support("hole-a", "boss-a", "boss-b", equivalent=True)))
    ambiguous = reason_composition(reviewed)
    rejected_backend = FakeBackend()
    rejected = binding.exercise_binding(plan_composition(ambiguous), ambiguous, rejected_backend)
    assert rejected.status == "SIMULATION_FAILED" and rejected_backend.calls == []
    supported = next(row for row in ambiguous.history_candidates
                     if dict(row.host_assignments)["hole-a"] == "boss-a")
    selected = reason_composition(replace(reviewed, selected_history_id=supported.history_id))
    planned = plan_composition(selected)
    result = binding.exercise_binding(planned, selected, FakeBackend())
    assert result.status == "SIMULATED"
    assert result.selected_history_id == selected.selected_history_id
    assert result.history_selection_source == "HUMAN_SELECTED_MACHINE_CANDIDATE"
    assert result.legal_history_count == 2
    assert result.legal_ordering_count == planned.legal_ordering_count
    assert set(result.source_evidence_ids) == {value for row in planned.operation_requirements
                                             for value in row.evidence_ids}


@pytest.mark.parametrize("fault", [
    lambda request, rows: [],
    lambda request, rows: rows + rows,
    lambda request, rows: [replace(rows[0], owner_reference="wrong-host")],
    lambda request, rows: [replace(rows[0], document_id="foreign-document")],
    lambda request, rows: [replace(rows[0], support_region="unbound-region")],
    lambda request, rows: [replace(rows[0], origin_m=(0, 0, request.plane_z_m + .01))],
    lambda request, rows: [replace(rows[0], normal=(0, 0, -1))],
    lambda request, rows: [replace(rows[0], u=(2, 0, 0))],
    lambda request, rows: [replace(rows[0], footprint_m=(0, .001, 0, .001))],
    lambda request, rows: [replace(rows[0], evidence_kind="NATIVE")],
])
def test_datum_rejection_never_creates_feature(fault):
    result, backend = _run(backend=FakeBackend(candidate_fault=fault))
    assert result.error_code == "DATUM_BINDING_FAILED"
    assert not result.commands and not result.receipts
    assert backend.calls == ["begin", "close"]


@pytest.mark.parametrize("fault", [None, False, 1, "UNKNOWN"])
def test_precondition_requires_explicit_true(fault):
    result, backend = _run(backend=FakeBackend(precondition=lambda values: [fault] * len(values)))
    assert result.error_code == "PRECONDITION_FAILED"
    assert not result.commands and backend.closed


@pytest.mark.parametrize("fault", [
    lambda command, row: None,
    lambda command, row: replace(row, source_feature_id="other-feature"),
    lambda command, row: replace(row, document_id="foreign-document"),
    lambda command, row: replace(row, datum_reference="other-support"),
    lambda command, row: replace(row, reference=""),
    lambda command, row: replace(row, evidence_kind="NATIVE"),
])
def test_invalid_feature_receipt_stops_sequence_and_cleans_up(fault):
    result, backend = _run(backend=FakeBackend(feature_fault=fault))
    assert result.error_code == "FEATURE_CREATION_FAILED"
    assert len(result.commands) == 1 and not result.receipts
    assert len(backend.calls) == 3 and backend.closed


def test_duplicate_feature_identity_rejected():
    backend = FakeBackend()
    original = backend.create_feature
    def duplicate(command):
        receipt = original(command)
        receipt = replace(receipt, reference="same-feature")
        backend.features[command.source_feature_id] = receipt
        return receipt
    backend.create_feature = duplicate
    result, _ = _run(backend=backend)
    assert result.error_code == "FEATURE_CREATION_FAILED"
    assert len(result.receipts) == 1 and len(result.commands) == 2


@pytest.mark.parametrize("value", [False, None, 1])
def test_rebuild_failure_keeps_only_simulated_diagnostic_evidence(value):
    result, backend = _run(backend=FakeBackend(rebuild=value))
    assert result.status == "SIMULATION_FAILED" and result.error_code == "REBUILD_FAILED"
    assert len(result.receipts) == 4 and backend.closed
    assert result.engineering_status == "NOT_VALIDATED"


def test_cleanup_failure_cannot_be_hidden_by_success_or_primary_failure():
    result, _ = _run(backend=FakeBackend(cleanup=False))
    assert result.error_code == "CLEANUP_FAILED" and result.cleanup_status == "FAILED"
    result, _ = _run(backend=FakeBackend(rebuild=False, cleanup=False))
    assert result.error_code == "REBUILD_FAILED" and result.cleanup_status == "FAILED"
    assert result.cleanup_reason


def test_real_backend_and_mutated_plan_rejected_before_any_backend_call():
    planned, composition = _inputs()
    backend = FakeBackend()
    backend.mode = "REAL"
    result = binding.exercise_binding(planned, composition, backend)
    assert result.error_code == "EXECUTION_BACKEND_UNAVAILABLE" and not backend.calls
    planned.modeling_plan.operations[-1].depth_mm = 999
    backend = FakeBackend()
    result = binding.exercise_binding(planned, composition, backend)
    assert result.error_code == "PLAN_CONTRACT_MISMATCH" and not backend.calls


@pytest.mark.parametrize("factory", [_c01, _c02, _c03])
def test_twenty_repeats_and_collection_permutations_are_deterministic(factory):
    planned, composition = _inputs(factory)
    expected = binding.exercise_binding(planned, composition, FakeBackend()).to_dict()
    assert expected["status"] == "SIMULATED"
    for seed in range(20):
        rng = random.Random(seed)
        nodes, edges, requirements = (list(composition.feature_graph_candidate.nodes),
                                      list(composition.dag_edges), list(planned.operation_requirements))
        for collection in (nodes, edges, requirements):
            rng.shuffle(collection)
        permuted = replace(composition, dag_edges=tuple(edges), feature_graph_candidate=replace(
            composition.feature_graph_candidate, nodes=tuple(nodes), dag_edges=tuple(edges)))
        actual = binding.exercise_binding(replace(planned, operation_requirements=tuple(requirements)),
                                          permuted, FakeBackend()).to_dict()
        assert json.dumps(actual, sort_keys=True) == json.dumps(expected, sort_keys=True)


def test_explicit_model_translation_is_preserved_through_local_sketch_conversion():
    origin = (.137, -.042, .019)
    result, backend = _run(_c03, FakeBackend(angle=.47, origin=origin), model_origin_m=origin)
    assert result.status == "SIMULATED"
    source = reason_composition(_c03())
    for hypothesis in source.feature_hypotheses:
        region = next(row for row in _c03().regions if row.region_id == hypothesis.region_id)
        assert backend.measurements[hypothesis.feature_id] == pytest.approx(tuple(
            value / 1000 + origin[i // 2] for i, value in enumerate(region.bounds_mm)))


def test_no_file_reads_and_no_live_connection_imports(monkeypatch):
    planned, composition = _inputs()
    def denied(*args, **kwargs):
        raise AssertionError("binding attempted a file read")
    monkeypatch.setattr(builtins, "open", denied)
    monkeypatch.setattr(io, "open", denied)
    assert binding.exercise_binding(planned, composition, FakeBackend()).status == "SIMULATED"
    source = inspect.getsource(binding).lower()
    for token in ("r004-c01", "r004-c02", "r004-c03", "expected.json", "golden", "known_answer",
                  "win32com", "pythoncom", "getactiveobject", "dispatch(", "subprocess", "cad1000"):
        assert token not in source
    assert source.count("truth") == source.count("generation_truth_reads")


def test_selected_history_with_incompatible_physical_host_fails_closed():
    reviewed = _c01()
    reviewed = _input(reviewed.regions, (_support("boss-a", "base"), _support("boss-b", "base"),
                                       _support("hole-a", "boss-a", "boss-b", equivalent=True)))
    ambiguous = reason_composition(reviewed)
    incompatible = next(row for row in ambiguous.history_candidates
                        if dict(row.host_assignments)["hole-a"] == "boss-b")
    selected = reason_composition(replace(reviewed, selected_history_id=incompatible.history_id))
    backend = FakeBackend()
    result = binding.exercise_binding(plan_composition(selected), selected, backend)
    assert result.error_code == "DATUM_BINDING_FAILED"
    assert result.selected_history_id == incompatible.history_id
    assert result.legal_history_count == 2 and backend.closed
    assert all(row.profile_kind != "CIRCLE" for row in result.commands)
    assert result.engineering_status == "NOT_VALIDATED"


@pytest.mark.parametrize(("method", "code", "receipts"), [
    ("begin", "SESSION_OWNERSHIP_FAILED", 0),
    ("datum_candidates", "DATUM_BINDING_FAILED", 0),
    ("check_preconditions", "PRECONDITION_FAILED", 0),
    ("create_feature", "FEATURE_CREATION_FAILED", 0),
    ("rebuild", "REBUILD_FAILED", 4),
    ("close", "CLEANUP_FAILED", 4),
])
def test_backend_exceptions_preserve_stage_and_attempt_cleanup(method, code, receipts):
    backend = FakeBackend()
    calls = []
    def fail(*args):
        calls.append(method)
        raise RuntimeError("injected failure")
    setattr(backend, method, fail)
    result, _ = _run(backend=backend)
    assert result.status == "SIMULATION_FAILED" and result.error_code == code
    assert len(result.receipts) == receipts and calls == [method]
    assert result.cleanup_status == ("FAILED" if method == "close" else "SIMULATED_CLOSED")
    assert result.real_qualification == "NOT_RUN"


@pytest.mark.parametrize("field", ["reference", "sketch_reference"])
@pytest.mark.parametrize("value", [17, "   "])
def test_receipt_references_require_nonblank_strings(field, value):
    result, backend = _run(backend=FakeBackend(feature_fault=lambda command, row: replace(row, **{field: value})))
    assert result.error_code == "FEATURE_CREATION_FAILED" and backend.closed


def test_duplicate_sketch_identity_rejected():
    backend = FakeBackend()
    original = backend.create_feature
    def duplicate(command):
        receipt = replace(original(command), sketch_reference="same-sketch")
        backend.features[command.source_feature_id] = receipt
        return receipt
    backend.create_feature = duplicate
    result, _ = _run(backend=backend)
    assert result.error_code == "FEATURE_CREATION_FAILED" and len(result.receipts) == 1
