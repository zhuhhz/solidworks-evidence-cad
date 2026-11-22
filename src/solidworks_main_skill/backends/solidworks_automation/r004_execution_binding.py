"""R004-C1 binding boundary. Only injected fake backends may be exercised here.

Commands describe local sketch geometry and bounded extrusion, not native CAD
evidence. Real connection, persistent-reference acquisition and qualification
belong to the next phase and cannot be enabled through this entry point.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import math
from typing import Any

from solidworks_main_skill.reasoning.r004_contract import CompositionResult
from solidworks_main_skill.reasoning.r004_planning import PlanningResult, plan_composition

from .r003_execution_bridge import AXIS_COMPONENT_TOLERANCE, LINEAR_TOLERANCE_MM


_TOL_M = LINEAR_TOLERANCE_MM / 1000


class BindingFailure(ValueError):
    def __init__(self, code: str, reason: str):
        super().__init__(reason)
        self.code = code


@dataclass(frozen=True)
class SupportRequest:
    token: str
    kind: str
    support_region: str | None
    host_feature_id: str | None
    host_reference: str | None
    document_id: str
    plane_z_m: float
    normal: tuple[float, float, float]
    footprint_m: tuple[float, float, float, float]


@dataclass(frozen=True)
class DatumCandidate:
    reference: str
    kind: str
    support_region: str | None
    owner_reference: str | None
    document_id: str
    origin_m: tuple[float, float, float]
    u: tuple[float, float, float]
    v: tuple[float, float, float]
    normal: tuple[float, float, float]
    footprint_m: tuple[float, float, float, float]
    evidence_kind: str = "SIMULATED"


@dataclass(frozen=True)
class BoundCommand:
    operation_id: str
    source_feature_id: str
    operation_type: str
    dependencies: tuple[str, ...]
    datum: DatumCandidate
    profile_kind: str
    points_m: tuple[tuple[float, float], ...]
    size_m: float | None
    depth_m: float
    model_direction: tuple[float, float, float]
    reverse_from_sketch_normal: bool


@dataclass(frozen=True)
class FeatureReceipt:
    operation_id: str
    source_feature_id: str
    reference: str
    sketch_reference: str
    datum_reference: str
    document_id: str
    evidence_kind: str = "SIMULATED"


@dataclass(frozen=True)
class BindingResult:
    status: str
    error_code: str | None = None
    failure_reason: str | None = None
    commands: tuple[BoundCommand, ...] = ()
    receipts: tuple[FeatureReceipt, ...] = ()
    plan_digest: str | None = None
    selected_history_id: str | None = None
    history_selection_source: str | None = None
    legal_history_count: int = 0
    legal_ordering_count: int = 0
    source_evidence_ids: tuple[str, ...] = ()
    cleanup_status: str = "NOT_RUN"
    cleanup_reason: str | None = None
    engineering_status: str = "NOT_VALIDATED"
    real_qualification: str = "NOT_RUN"
    generation_truth_reads: int = 0
    real_sw_invoked: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def _require(condition: bool, code: str, reason: str) -> None:
    if not condition:
        raise BindingFailure(code, reason)


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _dot(a, b) -> float:
    return sum(x * y for x, y in zip(a, b))


def _vector(value, length: int) -> bool:
    return (isinstance(value, (tuple, list)) and len(value) == length
            and all(type(x) in (int, float) and math.isfinite(x) for x in value))


def _preflight(planned: PlanningResult, composition: CompositionResult) -> PlanningResult:
    # Reuse the planner's defensive contract instead of creating another DAG/history validator.
    canonical = plan_composition(composition)
    _require(canonical.status == "PLANNED", "INVALID_PLANNING_INPUT",
             "completed reasoning and selected legal history are required")
    _require(isinstance(planned, PlanningResult) and planned.status == "PLANNED",
             "INVALID_PLANNING_INPUT", "a completed R004 planning result is required")
    supplied = planned.to_dict()
    rows = supplied["operation_requirements"]
    supplied["operation_requirements"] = sorted(rows, key=lambda row: row["operation_id"])
    expected = canonical.to_dict()
    expected["operation_requirements"] = sorted(expected["operation_requirements"],
                                                key=lambda row: row["operation_id"])
    _require(_json(supplied) == _json(expected), "PLAN_CONTRACT_MISMATCH",
             "plan, provenance, requirements or operation order changed after planning")
    return deepcopy(canonical)


def _bind_datum(request: SupportRequest, candidates: Any) -> DatumCandidate:
    _require(isinstance(candidates, (tuple, list)), "DATUM_BINDING_FAILED",
             "backend must explicitly return datum candidates")
    matches = []
    for row in candidates:
        _require(isinstance(row, DatumCandidate) and row.evidence_kind == "SIMULATED",
                 "DATUM_BINDING_FAILED", "C1 requires typed simulated datum evidence")
        _require(isinstance(row.reference, str) and bool(row.reference.strip()) and _vector(row.origin_m, 3)
                 and all(_vector(axis, 3) for axis in (row.u, row.v, row.normal))
                 and _vector(row.footprint_m, 4), "DATUM_BINDING_FAILED",
                 "datum reference or coordinate frame is incomplete")
        cross = (row.u[1] * row.v[2] - row.u[2] * row.v[1],
                 row.u[2] * row.v[0] - row.u[0] * row.v[2],
                 row.u[0] * row.v[1] - row.u[1] * row.v[0])
        frame_ok = (all(abs(_dot(axis, axis) - 1) <= AXIS_COMPONENT_TOLERANCE
                        for axis in (row.u, row.v, row.normal))
                    and abs(_dot(row.u, row.v)) <= AXIS_COMPONENT_TOLERANCE
                    and all(abs(a - b) <= AXIS_COMPONENT_TOLERANCE
                            for a, b in zip(cross, row.normal)))
        _require(frame_ok, "DATUM_BINDING_FAILED", "datum frame must be orthonormal and right handed")
        footprint_ok = (row.footprint_m[0] <= request.footprint_m[0] + _TOL_M
                        and row.footprint_m[1] >= request.footprint_m[1] - _TOL_M
                        and row.footprint_m[2] <= request.footprint_m[2] + _TOL_M
                        and row.footprint_m[3] >= request.footprint_m[3] - _TOL_M)
        if (row.document_id == request.document_id and row.kind == request.kind
                and row.owner_reference == request.host_reference
                and row.support_region == request.support_region
                and abs(row.origin_m[2] - request.plane_z_m) <= _TOL_M
                and all(abs(a - b) <= AXIS_COMPONENT_TOLERANCE
                        for a, b in zip(row.normal, request.normal)) and footprint_ok):
            matches.append(row)
    _require(len(matches) == 1, "DATUM_BINDING_FAILED",
             f"exact owner/support/plane requires one candidate, observed {len(matches)}")
    return matches[0]


def _command(operation, datum: DatumCandidate, origin_m) -> BoundCommand:
    bounds = operation.profile["region_bounds_mm"]
    x0, x1, y0, y1, z0, z1 = bounds
    subtractive = operation.type in {"cut_extrude_through_circle", "cut_extrude_through_slot"}
    plane_z = z1 if subtractive else z0

    def local(x, y):
        world = (x / 1000 + origin_m[0], y / 1000 + origin_m[1],
                 plane_z / 1000 + origin_m[2])
        delta = tuple(a - b for a, b in zip(world, datum.origin_m))
        _require(abs(_dot(delta, datum.normal)) <= _TOL_M,
                 "DATUM_BINDING_FAILED", "profile is not on the selected support plane")
        return (_dot(delta, datum.u), _dot(delta, datum.v))

    size = None
    if operation.type in {"base_extrude", "boss_extrude"}:
        kind = "RECTANGLE"
        points = tuple(local(x, y) for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)))
    elif operation.type == "cut_extrude_through_circle":
        kind = "CIRCLE"
        points = (local((x0 + x1) / 2, (y0 + y1) / 2),)
        size = operation.profile["diameter_mm"] / 2000
    else:
        kind = "STRAIGHT_SLOT"
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        half_spacing = (operation.profile["overall_length_mm"] - operation.profile["width_mm"]) / 2
        dx, dy = (half_spacing, 0) if operation.profile["major_axis"] == "X" else (0, half_spacing)
        points = (local(cx - dx, cy - dy), local(cx + dx, cy + dy))
        # The external low-level slot input is full width, despite its radius parameter name.
        size = operation.profile["width_mm"] / 1000
    direction = (0.0, 0.0, -1.0 if subtractive else 1.0)
    return BoundCommand(operation.operation_id, operation.source_feature_id, operation.type,
                        tuple(operation.depends_on_operation_ids), datum, kind, points, size,
                        operation.depth_mm / 1000, direction, _dot(direction, datum.normal) < 0)


def exercise_binding(planned: PlanningResult, composition: CompositionResult, backend: Any,
                     *, model_origin_m=(0.0, 0.0, 0.0)) -> BindingResult:
    """Exercise binding with a fake backend; never save CAD or certify native evidence.

    Backend port: mode='FAKE'; begin()->document ID; datum_candidates(request);
    check_preconditions(requirements, completed IDs)->one explicit bool per check;
    create_feature(command)->FeatureReceipt; rebuild(receipts)->strict bool; close()->bool.
    Failed runs retain partial simulated evidence for diagnosis, never a partial PASS.
    """
    commands, receipts = [], []
    canonical = None
    digest = None
    started = False
    cleanup_status, cleanup_reason = "NOT_RUN", None
    error_code, reason = None, None
    stage_code = "INVALID_PLANNING_INPUT"
    try:
        canonical = _preflight(planned, composition)
        _require(_vector(model_origin_m, 3), "INVALID_PLANNING_INPUT", "model origin must be finite metres")
        _require(getattr(backend, "mode", None) == "FAKE", "EXECUTION_BACKEND_UNAVAILABLE",
                 "C1 permits only an explicitly injected fake backend")
        digest = sha256(_json(canonical.to_dict()).encode()).hexdigest()
        started = True  # close is required even when begin fails partway through.
        stage_code = "SESSION_OWNERSHIP_FAILED"
        document_id = backend.begin()
        _require(isinstance(document_id, str) and bool(document_id), "SESSION_OWNERSHIP_FAILED",
                 "fake backend must identify its simulation document")
        requirements = {row.operation_id: row for row in canonical.operation_requirements}
        completed, features = {}, {}
        for operation in canonical.modeling_plan.operations:
            row = requirements[operation.operation_id]
            _require(set(operation.depends_on_operation_ids) <= set(completed),
                     "HOST_NOT_AVAILABLE", "operation dependencies are incomplete")
            host = row.datum.host_feature
            _require(host is None or host in features, "HOST_NOT_AVAILABLE", "datum host was not created")
            bounds = operation.profile["region_bounds_mm"]
            cut = operation.type.startswith("cut_")
            normal = (0.0, 0.0, 1.0 if row.datum.orientation == "Z+" else -1.0)
            request = SupportRequest(row.datum.token, row.datum.kind, row.datum.support_region,
                                     host, features[host].reference if host else None, document_id,
                                     bounds[5 if cut else 4] / 1000 + model_origin_m[2], normal,
                                     tuple(bounds[i] / 1000 + model_origin_m[i // 2] for i in range(4)))
            stage_code = "DATUM_BINDING_FAILED"
            datum = _bind_datum(request, backend.datum_candidates(request))
            stage_code = "PRECONDITION_FAILED"
            checks = backend.check_preconditions(row, tuple(completed))
            _require(isinstance(checks, (tuple, list)) and len(checks) == len(row.preconditions)
                     and all(value is True for value in checks), "PRECONDITION_FAILED",
                     "all operation preconditions require explicit True evidence")
            stage_code = "DATUM_BINDING_FAILED"
            command = _command(operation, datum, model_origin_m)
            commands.append(command)
            stage_code = "FEATURE_CREATION_FAILED"
            receipt = backend.create_feature(command)
            _require(isinstance(receipt, FeatureReceipt) and receipt.evidence_kind == "SIMULATED"
                     and receipt.operation_id == operation.operation_id
                     and receipt.source_feature_id == operation.source_feature_id
                     and receipt.document_id == document_id and receipt.datum_reference == datum.reference
                     and isinstance(receipt.reference, str) and bool(receipt.reference.strip())
                     and isinstance(receipt.sketch_reference, str) and bool(receipt.sketch_reference.strip())
                     and len({receipt.reference, receipt.sketch_reference, datum.reference}) == 3
                     and receipt.reference not in {item.reference for item in receipts}
                     and receipt.sketch_reference not in {item.sketch_reference for item in receipts},
                     "FEATURE_CREATION_FAILED", "feature receipt has missing, duplicate or conflicting identity")
            receipts.append(receipt)
            completed[operation.operation_id] = receipt
            features[operation.source_feature_id] = receipt
        stage_code = "REBUILD_FAILED"
        _require(backend.rebuild(tuple(receipts)) is True, "REBUILD_FAILED",
                 "fake backend rebuild must explicitly succeed")
    except BindingFailure as exc:
        error_code, reason = exc.code, str(exc)
    except Exception as exc:
        error_code, reason = stage_code, f"{type(exc).__name__}: {exc}"
    finally:
        if started:
            try:
                _require(backend.close() is True, "CLEANUP_FAILED", "fake document closure unconfirmed")
                cleanup_status = "SIMULATED_CLOSED"
            except Exception as exc:
                cleanup_status, cleanup_reason = "FAILED", str(exc)
                if error_code is None:
                    error_code, reason = "CLEANUP_FAILED", str(exc)
    return BindingResult(
        status="SIMULATION_FAILED" if error_code else "SIMULATED",
        error_code=error_code, failure_reason=reason, commands=tuple(commands), receipts=tuple(receipts),
        plan_digest=digest, selected_history_id=canonical.selected_history_id if canonical else None,
        history_selection_source=canonical.history_selection_source if canonical else None,
        legal_history_count=canonical.legal_history_count if canonical else 0,
        legal_ordering_count=canonical.legal_ordering_count if canonical else 0,
        source_evidence_ids=tuple(sorted({value for row in canonical.operation_requirements
                                          for value in row.evidence_ids})) if canonical else (),
        cleanup_status=cleanup_status, cleanup_reason=cleanup_reason,
    )
