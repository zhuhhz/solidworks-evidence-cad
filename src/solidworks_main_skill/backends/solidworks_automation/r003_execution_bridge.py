"""Bounded R003 Base/Boss/Hole execution boundary.

Native interaction is injected. Importing this module cannot connect to CAD.
The bridge produces evidence, never an engineering-success verdict.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import math
from pathlib import Path
import re
from typing import Any, Callable, Mapping

from experiments.three_view_reconstruction.schemas.modeling_plan import ModelingPlan


LINEAR_TOLERANCE_MM = 0.05  # B010-002 native geometry comparison
AXIS_COMPONENT_TOLERANCE = 1e-6  # B010-002 native Z-axis comparison
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}\Z")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}\Z")
_DENIED_KEYS = frozenset({
    "truth", "expected", "expected_geometry", "expected_body_count",
    "expected_ownership", "reference_cad", "benchmark_case", "author_history",
    "evaluator", "truth_bundle", "reference_answer",
})
_DENIED_PARTS = frozenset({
    "truth", "expected", "reference", "reference_cad", "evaluator",
    "expected.json", "bundle.json",
})


class R003PreflightError(ValueError):
    """Rejected before any SolidWorks session or document exists."""


def canonical_digest(value: Any) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                             allow_nan=False).encode("utf-8")).hexdigest()


def _no_truth(value: Any, *, key: str = "input", path_context: bool = False) -> None:
    """Reject exact semantic fields and path components, not arbitrary substrings."""
    if isinstance(value, Mapping):
        for name, child in value.items():
            if not isinstance(name, str):
                raise R003PreflightError("input keys must be strings")
            normalized = name.casefold().replace("-", "_")
            if normalized in _DENIED_KEYS:
                raise R003PreflightError(f"forbidden input field: {name}")
            _no_truth(child, key=normalized,
                      path_context=path_context or normalized.endswith(
                          ("path", "paths", "dir", "file")))
    elif isinstance(value, (list, tuple)):
        for child in value:
            _no_truth(child, key=key, path_context=path_context)
    elif isinstance(value, (str, Path)) and (path_context or isinstance(value, Path)
            or key.endswith(("path", "paths", "dir", "file"))
            or "/" in str(value) or "\\" in str(value)
            or str(value).casefold() in _DENIED_PARTS):
        parts = {part.casefold() for part in re.split(r"[/\\]+", str(value)) if part}
        if parts & _DENIED_PARTS or any(part.startswith("expected.") for part in parts):
            raise R003PreflightError("truth/reference path is forbidden")


def _positive(value: Any, label: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise R003PreflightError(f"{label} must be a finite positive mm value")
    return float(value)


def _finite(value: Any, label: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise R003PreflightError(f"{label} must be a finite mm value")
    return float(value)


def validate_r003_plan(plan: ModelingPlan, *, units: str = "mm",
                       coordinate_system: str = "MODEL_CENTERED_XY_MM") -> dict:
    if not isinstance(plan, ModelingPlan) or len(plan.operations) != 3:
        raise R003PreflightError("R003 requires exactly three ModelingPlan operations")
    if units != "mm" or coordinate_system != "MODEL_CENTERED_XY_MM":
        raise R003PreflightError("unsupported units or coordinate system")
    base, boss, hole = plan.operations
    if [op.type for op in plan.operations] != [
            "base_extrude", "boss_extrude", "cut_extrude_through_circle"]:
        raise R003PreflightError("unsupported R003 operation vocabulary/order")
    ids = [op.operation_id for op in plan.operations]
    source_ids = [op.source_feature_id for op in plan.operations]
    if (any(not isinstance(value, str) or not value.strip() for value in ids + source_ids)
            or len(set(ids)) != 3 or len(set(source_ids)) != 3):
        raise R003PreflightError("missing/duplicate operation or source feature ID")
    if ([op.depends_on_operation_ids for op in plan.operations]
            != [[], [ids[0]], [ids[1]]]):
        raise R003PreflightError("R003 dependency DAG must be Base -> Boss -> Hole")
    if (base.sketch_plane != "Front Plane" or boss.sketch_plane != "Plane_Base_Top"
            or hole.sketch_plane != "Plane_Boss_Top"
            or hole.direction != "through_all" or base.direction is not None
            or boss.direction is not None):
        raise R003PreflightError("unsupported plane or direction contract")
    for op, kind in ((base, "rectangle"), (boss, "rectangle"), (hole, "circle")):
        if not isinstance(op.profile, dict) or op.profile.get("type") != kind:
            raise R003PreflightError("unsupported profile")
    base_width = _positive(base.profile.get("width_mm"), "base width")
    base_height = _positive(base.profile.get("height_mm"), "base height")
    base_depth = _positive(base.depth_mm, "base depth")
    boss_width = _positive(boss.profile.get("width_mm"), "boss width")
    boss_height = _positive(boss.profile.get("height_mm"), "boss height")
    boss_depth = _positive(boss.depth_mm, "boss depth")
    diameter = _positive(hole.profile.get("diameter_mm"), "hole diameter")
    center = (_finite(hole.profile.get("center_x_mm"), "hole x"),
              _finite(hole.profile.get("center_y_mm"), "hole y"))
    if hole.depth_mm is not None:
        raise R003PreflightError("through-all hole must not specify a blind depth")
    base_top = _positive(boss.profile.get("plane_offset_mm"), "base top offset")
    hole_top = _positive(hole.profile.get("plane_offset_mm"), "hole plane offset")
    if (abs(base_top - base_depth) > LINEAR_TOLERANCE_MM
            or abs(hole_top - base_depth - boss_depth) > LINEAR_TOLERANCE_MM
            or boss_width >= base_width or boss_height >= base_height
            or diameter >= min(boss_width, boss_height)
            or abs(center[0]) + diameter / 2 >= boss_width / 2
            or abs(center[1]) + diameter / 2 >= boss_height / 2):
        raise R003PreflightError("unsupported R003 bounded geometry/plane offsets")
    return {"operation_ids": ids, "source_feature_ids": source_ids,
            "base_depth_mm": base_depth, "boss_depth_mm": boss_depth,
            "hole_center_mm": list(center), "hole_diameter_mm": diameter,
            "units": units, "coordinate_system": coordinate_system}


def bounded_geometry_from_plan(plan: ModelingPlan) -> dict:
    """Derive validator targets only from the accepted bounded plan."""
    details = validate_r003_plan(plan)
    base, boss, _hole = plan.operations
    bw, bh = base.profile["width_mm"], base.profile["height_mm"]
    uw, uh = boss.profile["width_mm"], boss.profile["height_mm"]
    bd, ud = details["base_depth_mm"], details["boss_depth_mm"]
    return {"base_depth_mm": bd, "boss_depth_mm": ud,
            "hole_center_mm": details["hole_center_mm"],
            "hole_diameter_mm": details["hole_diameter_mm"],
            "hole_source_feature_id": plan.operations[2].source_feature_id,
            "lower_envelope_mm": [-bw / 2, -bh / 2, -bd, bw / 2, bh / 2, 0],
            "upper_envelope_mm": [-uw / 2, -uh / 2, 0, uw / 2, uh / 2, ud],
            "overall_envelope_mm": [-bw / 2, -bh / 2, -bd, bw / 2, bh / 2, ud]}


@dataclass(frozen=True)
class R003ExecutionConfig:
    run_id: str
    artifact_root: Path
    output_dir: Path
    part_name: str
    drawing_name: str
    expected_sw_major_version: int
    feature_graph_digest: str
    reasoning_digest: str
    planning_digest: str
    source_digests: Mapping[str, str]
    timeouts: Mapping[str, float]
    session_ownership_policy: str = "OWN_NEW_INSTANCE_ONLY"
    units: str = "mm"
    coordinate_system: str = "MODEL_CENTERED_XY_MM"


_TIMEOUT_KEYS = frozenset({"preflight", "connection", "modeling", "drawing",
                           "validation", "cleanup", "overall"})


def validate_execution_config(config: R003ExecutionConfig, plan: ModelingPlan) -> dict:
    if not isinstance(config, R003ExecutionConfig):
        raise R003PreflightError("typed R003 execution config required")
    if not _RUN_ID.fullmatch(config.run_id) or not _NAME.fullmatch(config.part_name) \
            or not _NAME.fullmatch(config.drawing_name):
        raise R003PreflightError("unsafe run_id or artifact name")
    if config.session_ownership_policy != "OWN_NEW_INSTANCE_ONLY":
        raise R003PreflightError("only owned new SolidWorks sessions are supported")
    if type(config.expected_sw_major_version) is not int or config.expected_sw_major_version < 2024:
        raise R003PreflightError("expected SolidWorks major version required")
    if set(config.timeouts) != _TIMEOUT_KEYS or any(
            type(value) not in (int, float) or not math.isfinite(value) or value <= 0
            for value in config.timeouts.values()):
        raise R003PreflightError("all seven explicit positive timeouts required")
    for digest in (config.feature_graph_digest, config.reasoning_digest,
                   config.planning_digest, *config.source_digests.values()):
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise R003PreflightError("canonical SHA-256 digest required")
    if not config.source_digests:
        raise R003PreflightError("reviewed source digests required")
    _no_truth(config.source_digests)
    _no_truth({"artifact_root_path": config.artifact_root,
               "output_dir_path": config.output_dir})
    root = Path(config.artifact_root).resolve(strict=True)
    output = Path(config.output_dir)
    if output.name != config.run_id or output.exists() or output.is_symlink():
        raise R003PreflightError("run directory must be exclusive and named by run_id")
    if output.parent.resolve(strict=True) != root:
        raise R003PreflightError("output path escapes approved artifact root")
    if any((output / name).exists() for name in (
            config.part_name + ".sldprt", config.drawing_name + ".slddrw", "manifest.json")):
        raise R003PreflightError("output collision")
    _no_truth(plan.to_dict())
    details = validate_r003_plan(plan, units=config.units,
                                 coordinate_system=config.coordinate_system)
    if canonical_digest(plan.to_dict()) != config.planning_digest:
        raise R003PreflightError("ModelingPlan digest mismatch")
    return details


def assert_plan_to_sw_plane(*, plan_plane: str, expected_z_mm: float,
                            actual_plane: Mapping[str, Any],
                            base_top_face: Mapping[str, Any],
                            extrusion_direction: str,
                            plan_declared_offset_mm: float | None = None,
                            expected_direction: str = "+Z") -> dict:
    """Pure native-evidence check; a plane name is never sufficient."""
    evidence = {"plan_sketch_plane": plan_plane, "actual_sw_sketch_plane":
                actual_plane.get("name"), "expected_z_mm": expected_z_mm,
                "plan_declared_offset_mm": plan_declared_offset_mm,
                "tolerance_provenance": {"linear_mm": "B010-002:0.05",
                                         "axis_component": "B010-002:1e-6"}}
    if plan_plane not in {"Plane_Base_Top", "Plane_Boss_Top"}:
        return evidence | {"status": "FAIL", "reason": "UNSUPPORTED_PLAN_PLANE"}
    if (not actual_plane.get("native_reference") or not base_top_face.get("native_reference")
            or not base_top_face.get("owner_source_feature_id")
            or not actual_plane.get("name")):
        return evidence | {"status": "INCOMPLETE", "reason": "NATIVE_PLANE_OR_OWNER_MISSING"}
    try:
        actual_z = _finite(actual_plane.get("offset_z_mm"), "plane offset")
        face_z = _finite(base_top_face.get("offset_z_mm"), "face offset")
        normal = tuple(float(v) for v in actual_plane["normal"])
        face_normal = tuple(float(v) for v in base_top_face["normal"])
        if len(normal) != 3 or len(face_normal) != 3:
            raise ValueError("normal length")
    except (KeyError, TypeError, ValueError, R003PreflightError):
        return evidence | {"status": "INCOMPLETE", "reason": "PLANE_GEOMETRY_UNAVAILABLE"}
    delta = max(abs(actual_z - expected_z_mm), abs(face_z - expected_z_mm))
    aligned = (all(abs(normal[i] - (0.0, 0.0, 1.0)[i]) <= AXIS_COMPONENT_TOLERANCE
                   for i in range(3))
               and all(abs(face_normal[i] - (0.0, 0.0, 1.0)[i]) <= AXIS_COMPONENT_TOLERANCE
                       for i in range(3)))
    status = "PASS" if (delta <= LINEAR_TOLERANCE_MM and aligned
                        and extrusion_direction == expected_direction) else "FAIL"
    return evidence | {"status": status, "actual_z_mm": actual_z,
                       "base_top_z_mm": face_z, "offset_delta_mm": delta,
                       "normal": normal, "base_top_normal": face_normal,
                       "direction": extrusion_direction,
                       "base_native_owner": base_top_face["owner_source_feature_id"]}


def _session_ownership(session: Any) -> dict:
    connect = getattr(session, "connect", None)
    if callable(connect):
        connect()  # adapter verifies the exact owned process before new_part
    info = getattr(session, "connection_info", None)
    if not isinstance(info, Mapping):
        raise R003PreflightError("session ownership evidence unavailable")
    owned = info.get("started_by_cad_studio") is True
    attached = info.get("attached_to_existing_process") is True
    if not owned or attached or info.get("ownership_proven") is not True:
        raise R003PreflightError("attached or unproven session ownership")
    pid = info.get("process_id")
    if type(pid) is not int or pid <= 0:
        raise R003PreflightError("owned SolidWorks PID evidence required")
    create_time = info.get("process_create_time")
    if not isinstance(create_time, str) or not create_time:
        raise R003PreflightError("owned SolidWorks process creation identity required")
    return {"owned": True, "attached": False, "pid": pid,
            "process_create_time": create_time,
            "identity_source": info.get("identity_source")}


def execute_r003_plan(plan: ModelingPlan, config: R003ExecutionConfig, *,
                      session_factory: Callable[[], Any],
                      read_audit: Any = None,
                      include_drawing: bool = True) -> dict:
    """Execute through an injected bounded native adapter; no COM at import time.

    Adapter methods are `new_part`, `create_base`, `base_plane_evidence`,
    `create_boss`, `boss_plane_evidence`, `create_hole`, `initial_rebuild`,
    `save_part`, `close_part`, `create_physical_left_drawing`, and
    `quit_owned_instance`. The real adapter requires Stage 5A.2 review.
    """
    details = validate_execution_config(config, plan)  # before session_factory
    output = Path(config.output_dir)
    output.mkdir(exist_ok=False)  # atomic reservation; never clean it on failure
    part_path = output / (config.part_name + ".sldprt")
    drawing_path = output / (config.drawing_name + ".slddrw")
    result: dict[str, Any] = {
        "run_id": config.run_id, "generation_truth_reads": None,
        "generation_read_audit": {"audit_status": "INACTIVE"},
        "plan_digest": config.planning_digest,
        "feature_graph_digest": config.feature_graph_digest,
        "reasoning_digest": config.reasoning_digest,
        "planning_digest": config.planning_digest,
        "source_digests": dict(config.source_digests),
        "session": None, "operations": [], "plane_assertions": [],
        "initial_rebuild": None, "save_result": None, "close_result": None,
        "output_paths": {"part": str(part_path), "drawing": str(drawing_path)},
        "artifact_hashes": {}, "primary_status": "ERROR", "primary_reason": None,
        "cleanup_status": "NOT_RUN", "cleanup_reason": None,
    }
    session = None
    part_created = False
    try:
        session = session_factory()
        result["session"] = _session_ownership(session)  # before new_part
        observed_major = getattr(session, "sw_major_version", None)
        if observed_major != config.expected_sw_major_version:
            raise R003PreflightError("observed SolidWorks version mismatch")
        session.new_part()
        part_created = True
        base, boss, hole = plan.operations
        base_native = session.create_base(base)
        result["operations"].append({"operation_id": base.operation_id,
                                      "source_feature_id": base.source_feature_id,
                                      "native": base_native})
        base_plane, base_top_face = session.base_plane_evidence(base, base_native)
        base_assertion = assert_plan_to_sw_plane(
            plan_plane=boss.sketch_plane, expected_z_mm=0.0,
            actual_plane=base_plane, base_top_face=base_top_face,
            extrusion_direction="+Z",
            plan_declared_offset_mm=boss.profile["plane_offset_mm"])
        base_assertion["plan_to_sw_z_translation_mm"] = -details["base_depth_mm"]
        if base_assertion["status"] != "PASS" or base_top_face.get(
                "owner_source_feature_id") != base.source_feature_id:
            raise RuntimeError("PLAN_TO_SW_ADAPTER_ASSERTION: Base top/Front Plane not proven")
        result["plane_assertions"].append(base_assertion)
        boss_native = session.create_boss(boss)
        result["operations"].append({"operation_id": boss.operation_id,
                                      "source_feature_id": boss.source_feature_id,
                                      "native": boss_native})
        boss_plane, boss_top_face = session.boss_plane_evidence(boss, boss_native)
        hole_assertion = assert_plan_to_sw_plane(
            plan_plane=hole.sketch_plane, expected_z_mm=details["boss_depth_mm"],
            actual_plane=boss_plane, base_top_face=boss_top_face,
            extrusion_direction="-Z", expected_direction="-Z",
            plan_declared_offset_mm=hole.profile["plane_offset_mm"])
        hole_assertion["plan_to_sw_z_translation_mm"] = -details["base_depth_mm"]
        if hole_assertion["status"] != "PASS" or boss_top_face.get(
                "owner_source_feature_id") != boss.source_feature_id:
            raise RuntimeError("PLAN_TO_SW_ADAPTER_ASSERTION: Boss top/Hole plane not proven")
        result["plane_assertions"].append(hole_assertion)
        hole_native = session.create_hole(hole)
        result["operations"].append({"operation_id": hole.operation_id,
                                      "source_feature_id": hole.source_feature_id,
                                      "native": hole_native})
        result["initial_rebuild"] = session.initial_rebuild(plan, result["operations"])
        result["save_result"] = session.save_part(part_path)
        if result["save_result"] is not True or not part_path.is_file() or part_path.stat().st_size == 0:
            raise RuntimeError("SLDPRT save did not produce a nonempty file")
        result["close_result"] = session.close_part()
        if result["close_result"] is not True:
            raise RuntimeError("SLDPRT close failed")
        if include_drawing:
            drawing = session.create_physical_left_drawing(part_path, drawing_path)
            drawing_lifecycle_closed = (
                drawing.get("drawing_closed") is True
                and drawing.get("owned_session_pid") == result["session"]["pid"])
            legacy_fake_child_closed = (
                drawing.get("child_exited") is True
                and type(drawing.get("child_pid")) is int)
            if (drawing.get("status") != "PASS"
                    or not (drawing_lifecycle_closed or legacy_fake_child_closed)
                    or not drawing_path.is_file()
                    or drawing_path.stat().st_size == 0):
                raise RuntimeError("physical LEFT drawing creation/save failed")
            result["drawing_creation"] = drawing
        artifacts = (("part", part_path), ("drawing", drawing_path)) if include_drawing else (("part", part_path),)
        for label, path in artifacts:
            result["artifact_hashes"][label] = sha256(path.read_bytes()).hexdigest()
        result["primary_status"] = "PASS"
    except Exception as exc:  # retain partial native evidence; no fake engineering PASS
        result["primary_reason"] = f"{type(exc).__name__}: {exc}"
    finally:
        if read_audit is not None:
            audit = read_audit.evidence()
            result["generation_read_audit"] = audit
            result["generation_truth_reads"] = audit.get("generation_truth_reads")
            if (audit.get("audit_status") not in {"ACTIVE", "COMPLETE"}
                    or audit.get("denylist_violations")
                    or result["generation_truth_reads"] != 0):
                result["primary_status"] = "ERROR"
                result["primary_reason"] = "GENERATION_READ_AUDIT_INCOMPLETE_OR_VIOLATED"
        if session is not None and isinstance(result.get("session"), dict) \
                and result["session"].get("owned") is True:
            try:
                if part_created and result["close_result"] is not True:
                    session.close_part()
                cleanup = session.quit_owned_instance()
                if (not isinstance(cleanup, Mapping)
                        or cleanup.get("pid") != result["session"]["pid"]
                        or cleanup.get("process_exited") is not True
                        or cleanup.get("com_released") is not True):
                    raise RuntimeError("owned SolidWorks PID exit unconfirmed")
                result["cleanup_status"] = "PASS"
            except Exception as exc:
                result["cleanup_status"] = "ERROR"
                result["cleanup_reason"] = f"{type(exc).__name__}: {exc}"
        elif session is not None:
            result["cleanup_status"] = "NOT_RUN"
            result["cleanup_reason"] = "SESSION_NOT_PROVEN_OWNED; foreign process untouched"
    return result
