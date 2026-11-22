"""Stage 5A.1 fake native evidence only: this module never starts SolidWorks."""

from __future__ import annotations

from dataclasses import replace
from copy import deepcopy
from pathlib import Path

import pytest

from experiments.three_view_reconstruction.schemas.modeling_plan import ModelingOperation, ModelingPlan
from experiments.three_view_reconstruction.drawing.view_orientation import canonicalize
from solidworks_main_skill.backends.solidworks_automation.r003_execution_bridge import (
    R003ExecutionConfig, R003PreflightError, assert_plan_to_sw_plane,
    canonical_digest, execute_r003_plan, validate_execution_config, validate_r003_plan,
)
from solidworks_main_skill.backends.solidworks_automation.r003_real_validation import (
    GATES, compose_six_gates, validate_body_count, validate_brep_geometry,
    validate_hole_full_span, validate_ownership, validate_physical_views,
    validate_roundtrip, validate_save_reopen, validate_strict_rebuild,
)


def plan():
    return ModelingPlan([
        ModelingOperation("base_extrude", "Front Plane",
                          {"type": "rectangle", "width_mm": 60, "height_mm": 40}, 10,
                          operation_id="op_base", source_feature_id="base", depends_on_operation_ids=[]),
        ModelingOperation("boss_extrude", "Plane_Base_Top",
                          {"type": "rectangle", "width_mm": 30, "height_mm": 20,
                           "plane_offset_mm": 10}, 5,
                          operation_id="op_boss", source_feature_id="boss",
                          depends_on_operation_ids=["op_base"]),
        ModelingOperation("cut_extrude_through_circle", "Plane_Boss_Top",
                          {"type": "circle", "diameter_mm": 5, "center_x_mm": 0,
                           "center_y_mm": 0, "plane_offset_mm": 15},
                          direction="through_all", operation_id="op_hole",
                          source_feature_id="hole", depends_on_operation_ids=["op_boss"]),
    ])


def config(root: Path, value=None):
    value = value or plan()
    d = "a" * 64
    try:
        plan_digest = canonical_digest(value.to_dict())
    except ValueError:
        plan_digest = d  # malformed plan must be rejected before session creation
    return R003ExecutionConfig("run1", root, root / "run1", "part", "drawing", 2024,
                               d, d, plan_digest, {"reviewed_source": d},
                               {key: 10.0 for key in ("preflight", "connection", "modeling",
                                                        "drawing", "validation", "cleanup", "overall")})


class FakeSession:
    connection_info = {"started_by_cad_studio": True,
                       "attached_to_existing_process": False, "process_id": 1234,
                       "ownership_proven": True, "process_create_time": "fake-start",
                       "identity_source": "fake-test"}
    sw_major_version = 2024

    def __init__(self):
        self.calls = []
        self.close_error = False

    def new_part(self):
        self.calls.append("new_part")

    def create_base(self, op):
        self.calls.append("base")
        return {"persistent_reference": "base-ref"}

    def base_plane_evidence(self, op, native):
        return ({"name": "Front Plane", "native_reference": "front-ref",
                 "offset_z_mm": 0, "normal": [0, 0, 1]},
                {"native_reference": "base-top-ref", "owner_source_feature_id": "base",
                 "offset_z_mm": 0, "normal": [0, 0, 1]})

    def create_boss(self, op):
        self.calls.append("boss")
        return {"persistent_reference": "boss-ref"}

    def boss_plane_evidence(self, op, native):
        return ({"name": "Plane_Boss_Top", "native_reference": "top-ref",
                 "offset_z_mm": 5, "normal": [0, 0, 1]},
                {"native_reference": "boss-top-ref", "owner_source_feature_id": "boss",
                 "offset_z_mm": 5, "normal": [0, 0, 1]})

    def create_hole(self, op):
        self.calls.append("hole")
        return {"persistent_reference": "hole-ref", "through_all": True}

    def initial_rebuild(self, plan, operations):
        self.calls.append("rebuild")
        return {"rebuild_result": True, "features": []}

    def save_part(self, path):
        Path(path).write_bytes(b"fake part")
        return True

    def close_part(self):
        self.calls.append("close")
        if self.close_error:
            raise RuntimeError("close failure")
        return True

    def create_physical_left_drawing(self, part_path, drawing_path):
        Path(drawing_path).write_bytes(b"fake drawing")
        return {"status": "PASS", "requested_views": ["*Front", "*Top", "*Left"],
                "child_pid": 4321, "child_exited": True}

    def quit_owned_instance(self):
        self.calls.append("quit_owned")
        return {"pid": 1234, "process_exited": True, "com_released": True}


def test_bounded_plan_and_fake_dispatch(tmp_path):
    value = plan()
    assert validate_r003_plan(value)["operation_ids"] == ["op_base", "op_boss", "op_hole"]
    session = FakeSession()
    report = execute_r003_plan(value, config(tmp_path, value), session_factory=lambda: session)
    assert report["primary_status"] == "PASS"
    assert report["generation_truth_reads"] is None
    assert report["generation_read_audit"]["audit_status"] == "INACTIVE"
    assert report["cleanup_status"] == "PASS"
    assert [row["status"] for row in report["plane_assertions"]] == ["PASS", "PASS"]
    assert [row["plan_declared_offset_mm"] for row in report["plane_assertions"]] == [10, 15]
    assert [row["plan_to_sw_z_translation_mm"] for row in report["plane_assertions"]] == [-10, -10]
    assert session.calls[:4] == ["new_part", "base", "boss", "hole"]
    assert "engineering_success" not in report


def test_part_stage_finishes_without_drawing_before_process_b(tmp_path):
    class PartOnly(FakeSession):
        def create_physical_left_drawing(self, *_):
            raise AssertionError("Drawing must follow reopened Part Process B")

    session = PartOnly()
    report = execute_r003_plan(plan(), config(tmp_path), session_factory=lambda: session,
                               include_drawing=False)
    assert report["primary_status"] == "PASS"
    assert report["cleanup_status"] == "PASS"
    assert report["close_result"] is True
    assert set(report["artifact_hashes"]) == {"part"}
    assert not Path(report["output_paths"]["drawing"]).exists()


@pytest.mark.parametrize("mutation", [
    lambda p: p.operations.append(p.operations[-1]),
    lambda p: setattr(p.operations[2], "type", "unknown"),
    lambda p: setattr(p.operations[2], "depends_on_operation_ids", []),
    lambda p: setattr(p.operations[0], "depth_mm", float("nan")),
    lambda p: setattr(p.operations[2], "direction", "blind"),
])
def test_invalid_plan_never_creates_session(tmp_path, mutation):
    value = plan()
    mutation(value)
    calls = []
    with pytest.raises(R003PreflightError):
        execute_r003_plan(value, config(tmp_path, value), session_factory=lambda: calls.append(1))
    assert calls == []
    assert not (tmp_path / "run1").exists()


@pytest.mark.parametrize("key,path", [
    ("source_path", "truth/bundle.json"),
    ("projection_path", "expected.json"),
    ("cad_path", "reference/reference.sldprt"),
])
def test_truth_path_rejected_before_session(tmp_path, key, path):
    value = plan()
    conf = replace(config(tmp_path, value), source_digests={key: path})
    calls = []
    with pytest.raises(R003PreflightError):
        execute_r003_plan(value, conf, session_factory=lambda: calls.append(1))
    assert calls == []


def test_plane_assertion_is_geometric_and_native():
    actual = {"name": "Front Plane", "native_reference": "p", "offset_z_mm": 0,
              "normal": [0, 0, 1]}
    face = {"native_reference": "f", "owner_source_feature_id": "base",
            "offset_z_mm": 0, "normal": [0, 0, 1]}
    def check(a=actual, f=face, direction="+Z"):
        return assert_plan_to_sw_plane(plan_plane="Plane_Base_Top", expected_z_mm=0,
                                       actual_plane=a, base_top_face=f,
                                       extrusion_direction=direction)["status"]
    assert check() == "PASS"
    assert check(a=actual | {"offset_z_mm": 1}) == "FAIL"
    assert check(a=actual | {"normal": [0, 0, -1]}) == "FAIL"
    assert check(f=face | {"owner_source_feature_id": None}) == "INCOMPLETE"
    assert check(direction="-Z") == "FAIL"


def test_wrong_boss_top_plane_fails_before_hole(tmp_path):
    class WrongTop(FakeSession):
        def boss_plane_evidence(self, op, native):
            plane, face = super().boss_plane_evidence(op, native)
            return plane | {"offset_z_mm": 6}, face
    session = WrongTop()
    report = execute_r003_plan(plan(), config(tmp_path), session_factory=lambda: session)
    assert report["primary_status"] == "ERROR"
    assert "PLAN_TO_SW_ADAPTER_ASSERTION" in report["primary_reason"]
    assert "hole" not in session.calls


def rebuild_rows():
    return {"rebuild_result": True, "features": [
        {"source_feature_id": name, "persistent_reference": name + "-ref",
         "native_type": kind, "error_code": 0, "suppressed": False,
         "feature_resolved": True, "sketch_resolved": True, "plane_resolved": True,
         "dependencies_resolved": True}
        for name, kind in (("base", "Boss"), ("boss", "Boss"), ("hole", "Cut"))]}


def test_strict_rebuild_identity_and_fail_closed():
    first, reopened = rebuild_rows(), rebuild_rows()
    ids = ["base", "boss", "hole"]
    refs = {name: name + "-ref" for name in ids}
    def check(a=first, b=reopened):
        return validate_strict_rebuild(a, b, ids, refs)["status"]
    assert check() == "PASS"
    reopened["features"][2]["error_code"] = 3
    assert check() == "FAIL"
    reopened = rebuild_rows()
    reopened["features"][1]["suppressed"] = True
    assert check(b=reopened) == "FAIL"
    reopened = rebuild_rows()
    reopened["features"][1]["plane_resolved"] = False
    assert check(b=reopened) == "FAIL"
    reopened = rebuild_rows()
    reopened["features"][2]["persistent_reference"] = "wrong-owner"
    reopened["features"][2]["display_name"] = "Hole"
    assert check(b=reopened) == "FAIL"
    assert check(b=None) == "NOT_RUN"
    assert validate_strict_rebuild(first, rebuild_rows(), ids)["status"] == "INCOMPLETE"


def geometry_evidence():
    target = {"base_depth_mm": 10, "boss_depth_mm": 5, "hole_center_mm": [0, 0],
              "hole_diameter_mm": 5, "hole_source_feature_id": "hole",
              "lower_envelope_mm": [-30, -20, -10, 30, 20, 0],
              "upper_envelope_mm": [-15, -10, 0, 15, 10, 5],
              "overall_envelope_mm": [-30, -20, -10, 30, 20, 5]}
    hole = {"through_all_created": True, "cylinder_present": True,
            "center_mm": [0, 0], "diameter_mm": 5, "axis": [0, 0, 1],
            "axial_min_z_mm": -10, "axial_max_z_mm": 5,
            "bottom_opening": True, "top_opening": True,
            "blind_cap_absent": True, "through_state": "confirmed",
            "native_owner_feature_id": "hole"}
    brep = {"overall_envelope_mm": target["overall_envelope_mm"],
            "lower_envelope_mm": target["lower_envelope_mm"],
            "upper_envelope_mm": target["upper_envelope_mm"],
            "fused_interface": True, "cylindrical_wall_count": 1,
            "extra_geometry_count": 0, "hole": hole}
    body = {"solid_body_count": 1, "face_count": 12, "edge_count": 25}
    ownership = {"unknown": 0, "unattributed": 0, "misattributed": 0,
                 "persistent_mapping_resolved": True,
                 "total_face_count": 12,
                 "feature_references": {name: name + "-feature-ref"
                                        for name in ("base", "boss", "hole")},
                 "rows": [{"source_feature_id": name, "native_owner_feature_id": name,
                           "ownership": "API_EXACT", "persistent_reference": f"face-{index}-ref",
                           "owner_persistent_reference": name + "-feature-ref",
                           "role": "HOLE_WALL" if name == "hole" else "PLANAR"}
                          for index, name in enumerate(["base"] * 6 + ["boss"] * 5 + ["hole"])]}
    return target, brep, body, ownership


def test_body_brep_hole_ownership_and_save_reopen():
    target, brep, body, owner = geometry_evidence()
    assert validate_body_count(body)["status"] == "PASS"
    assert validate_body_count(body | {"solid_body_count": 2})["status"] == "FAIL"
    assert validate_hole_full_span(brep["hole"], target, body, owner)["status"] == "PASS"
    assert validate_brep_geometry(brep, target, body, owner)["status"] == "PASS"
    assert validate_brep_geometry(brep | {"extra_geometry_count": 1}, target, body, owner)["status"] == "FAIL"
    for change in ({"top_opening": False}, {"blind_cap_absent": False},
                   {"axial_max_z_mm": 4}, {"native_owner_feature_id": "unknown"},
                   {"through_all_created": False}):
        assert validate_hole_full_span(brep["hole"] | change, target, body, owner)["status"] == "FAIL"
    assert validate_hole_full_span(brep["hole"] | {"through_state": "unknown"},
                                   target, body, owner)["status"] == "INCOMPLETE"
    assert validate_ownership(owner, ["base", "boss", "hole"])["status"] == "PASS"
    assert validate_ownership(owner | {"unknown": 1}, ["base", "boss", "hole"])["status"] == "FAIL"
    wrong_owner = deepcopy(owner)
    wrong_owner["rows"][-1]["display_name"] = "Hole"
    wrong_owner["rows"][-1]["owner_persistent_reference"] = "base-feature-ref"
    assert validate_ownership(wrong_owner, ["base", "boss", "hole"])["status"] == "FAIL"
    save = {"save_success": True, "close_success": True, "reopen_success": True,
            "file_size": 100, "sealed_sha256": "a" * 64,
            "reopened_exact_path": True, "sealed_hash_unchanged": True}
    passed = {"status": "PASS"}
    assert validate_save_reopen(save, passed, passed, passed)["status"] == "PASS"
    assert validate_save_reopen(save | {"reopen_success": False}, passed, passed, passed)["status"] == "FAIL"


def views():
    return {role: {"native_identity": "*" + role.title(), "normal": normal,
                   "model_to_view_transform": [*canonicalize("*" + role.title()).right,
                                               *canonicalize("*" + role.title()).up,
                                               *canonicalize("*" + role.title()).normal,
                                               0, 0, 0, 1, 0, 0, 0],
                   "transform_axes": {"normal": normal,
                                      "up": canonicalize("*" + role.title()).up,
                                      "right": canonicalize("*" + role.title()).right},
                   "projection_direction": normal, "scale": 1.0,
                   "sheet_position": {"FRONT": [0.15, 0.12],
                                      "TOP": [0.15, 0.22], "LEFT": [0.05, 0.12]}[role],
                   "model_reference": "same.sldprt", "projection_standard": "THIRD_ANGLE"}
            for role, normal in (("FRONT", [0, 0, 1]),
                                 ("TOP", [0, 1, 0]), ("LEFT", [-1, 0, 0]))}


def primitives():
    rows = {role: {"visible_lines": [{"x1": 0, "y1": 0, "x2": 10, "y2": 0}],
                   "hidden_lines": [], "circles": [], "arcs": [],
                   "unresolved_discriminating_primitives": 0}
            for role in ("FRONT", "TOP", "LEFT")}
    rows["LEFT"]["hidden_lines"] = [{"x1": 2, "y1": 0, "x2": 2, "y2": 5}]
    rows["TOP"]["circles"] = [{"x": 0, "y": 0, "diameter": 5}]
    rows["reviewed_projection_graph_digest"] = "a" * 64
    return rows


def test_physical_left_roundtrip_and_six_gate_composer():
    native = views()
    source = primitives()
    assert validate_physical_views(native)["status"] == "PASS"
    assert validate_roundtrip(source, primitives(), native)["status"] == "PASS"
    right = views()
    right["LEFT"]["native_identity"] = "*Right"
    assert validate_physical_views(right)["status"] == "FAIL"
    wrong_normal = views()
    wrong_normal["LEFT"]["normal"] = [1, 0, 0]
    assert validate_physical_views(wrong_normal)["status"] == "FAIL"
    mismatched = primitives()
    mismatched["LEFT"]["hidden_lines"] = [{"x1": 0, "y1": 0, "x2": 1, "y2": 0}]
    assert validate_roundtrip(source, mismatched, native)["status"] == "FAIL"
    mismatched = primitives()
    mismatched["FRONT"]["circles"] = [{"x": 0, "y": 0, "diameter": 4}]
    assert validate_roundtrip(source, mismatched, native)["status"] == "FAIL"
    gates = {name: {"status": "PASS"} for name in GATES}
    assert compose_six_gates(gates)["r003_engineering_pass"] is True
    for name in GATES:
        for status in ("FAIL", "ERROR", "INCOMPLETE", "NOT_RUN"):
            changed = gates | {name: {"status": status}}
            assert compose_six_gates(changed)["r003_engineering_pass"] is False
