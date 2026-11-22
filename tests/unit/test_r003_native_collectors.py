"""Stage 5A.3 native-object fakes; importing/running never connects to CAD."""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
from pathlib import Path

import pytest

from experiments.three_view_reconstruction.run_r003_v2_real_validation import (
    GenerationReadAudit, validate_real_output_root,
)
from solidworks_main_skill.backends.solidworks_automation.r003_native_adapter import (
    OwnedProcessIdentity, R003OwnedNativeSession, R003SolidWorksSessionAdapter,
    SessionOwnershipUnproven,
    create_r003_physical_views, launch_r003_owned_com, prove_r003_new_instance,
    verify_owned_identity, _exact_pid_present,
)
from solidworks_main_skill.backends.solidworks_automation.r003_native_collectors import (
    NativeEvidenceUnavailable, capture_persistent_reference,
    classify_r003_hole_openings, collect_body_count, collect_bounded_r003_ownership,
    collect_feature_error,
    collect_plane_face, collect_rebuild_evidence, collect_reference_plane,
    collect_r003_brep, collect_r003_ownership, collect_signed_part_bounds, collect_sketch_support,
    collect_reopened_r003_dependencies,
    collect_r003_native_view_modes, normalize_native_view_transform,
    normalize_r003_hlv_hlr,
    resolve_persistent_reference, member, native_profile_feature,
)


def test_parameterless_com_property_result_is_not_called_twice():
    class DispatchLike:
        _oleobj_ = object()
        def __call__(self):
            raise AssertionError("default dispatch must not be invoked")
    value = DispatchLike()
    assert member(type("Owner", (), {"GetFirstDocument": value})(),
                  "GetFirstDocument") is value


def test_native_profile_enumeration_accepts_null_end_sentinel():
    class Profile:
        GetTypeName2 = "ProfileFeature"
        GetNextSubFeature = None

    profile = Profile()
    class Extrusion:
        GetParents = []
        GetFirstSubFeature = profile

    class Extension:
        def GetPersistReference3(self, entity):
            assert entity is profile
            return [1, 2, 3]

    found, reference = native_profile_feature(Extension(), Extrusion())
    assert found is profile and reference == "AQID"


def test_exact_pid_exit_probe_rejects_ambiguous_output(monkeypatch):
    import solidworks_main_skill.backends.solidworks_automation.r003_native_adapter as native
    class Result:
        returncode = 0
        stdout = "PRESENT\n"
    monkeypatch.setattr(native.subprocess, "run", lambda *a, **k: Result())
    assert _exact_pid_present(123) is True
    Result.stdout = "ABSENT\n"
    assert _exact_pid_present(123) is False
    Result.stdout = ""
    with pytest.raises(SessionOwnershipUnproven):
        _exact_pid_present(123)


class Extension:
    def GetPersistReference3(self, entity):
        return entity.ref.encode("ascii")


class Feature:
    def __init__(self, ref, error=0):
        self.ref = ref
        self.error = error

    def GetTypeName2(self):
        return "BossExtrude"

    def IsSuppressed(self):
        return False

    def GetErrorCode2(self, warning):
        warning.value = False
        return self.error


def test_instant3d_feature_requires_matching_underlying_native_family():
    class Profile(Feature):
        def GetTypeName2(self):
            return "ProfileFeature"
        GetNextSubFeature = None

    class InstantFeature(Feature):
        def __init__(self, underlying):
            super().__init__("instant-boss")
            self.underlying = underlying
            self.profile = Profile("instant-profile")

        def GetTypeName2(self):
            return "ICE"

        def GetTypeName(self):
            return self.underlying

        def GetFirstSubFeature(self):
            return self.profile

        def GetParents(self):
            return []

    adapter = object.__new__(R003SolidWorksSessionAdapter)
    adapter._part = type("Part", (), {"Extension": Extension()})()
    adapter._warning_slot_factory = lambda: type("ByRef", (), {"value": False})()
    adapter._features = {}
    adapter._feature_refs = {}
    adapter._sketch_refs = {}
    adapter._support_refs = {}
    operation = type("Operation", (), {"operation_id": "boss", "source_feature_id": "boss"})()
    result = adapter._record_feature(operation, InstantFeature("Boss"),
                                     allowed_types={"Boss", "BossExtrude", "Extrusion"})
    assert result["native_type"] == "ICE"
    assert result["underlying_native_type"] == "Boss"
    with pytest.raises(NativeEvidenceUnavailable, match="underlying: Cut"):
        adapter._record_feature(operation, InstantFeature("Cut"),
                                allowed_types={"Boss", "BossExtrude", "Extrusion"})


class PlaneSurface:
    IsPlane = True
    PlaneParams = [0, 0, 1, 0, 0, 0.005]


class Face:
    def __init__(self, ref, owner, surface=None):
        self.ref = ref
        self.owner = owner
        self.surface = surface or PlaneSurface()
        self.Normal = [0, 0, 1]
        self.edges = []

    def GetFeature(self):
        return self.owner

    def GetSurface(self):
        return self.surface

    def GetEdges(self):
        return self.edges


class Body:
    def __init__(self, faces, edges):
        self.faces = faces
        self.edges = edges

    def GetFaces(self):
        return self.faces

    def GetEdges(self):
        return self.edges


class Model:
    def __init__(self, body):
        self.Extension = Extension()
        self.body = body

    def GetBodies2(self, kind, visible):
        assert (kind, visible) == (0, False)
        return [self.body]

    def GetPartBox(self, accurate):
        assert accurate is True
        return [-.03, -.02, -.01, .03, .02, .005]

    def ForceRebuild3(self, top_only):
        assert top_only is False
        return True


def test_typed_persistent_feature_and_reopen_resolution():
    feature = Feature("native-feature")
    record = capture_persistent_reference(Extension(), feature, "FEATURE")
    result = resolve_persistent_reference(Extension(), record,
        resolver=lambda extension, raw: (feature, 0) if raw == b"native-feature" else (None, 1))
    assert result["resolved"] is True
    assert result["reference_type"] == "FEATURE"
    with pytest.raises(NativeEvidenceUnavailable):
        resolve_persistent_reference(Extension(), record, resolver=lambda *_: (None, 1))


def test_feature_error_failed_api_never_becomes_zero():
    slot = lambda: type("ByRef", (), {"value": False})()
    assert collect_feature_error(Feature("x", 2), warning_slot_factory=slot)["error_code"] == 2
    class Broken:
        def GetErrorCode2(self, warning):
            raise RuntimeError("COM failure")
    failed = collect_feature_error(Broken(), warning_slot_factory=slot)
    assert failed["api_status"] == "INCOMPLETE" and failed["error_code"] is None


def test_body_bounds_plane_and_strict_rebuild():
    features = {"base": Feature("base"), "boss": Feature("boss"), "hole": Feature("hole")}
    face = Face("base-top", features["base"])
    model = Model(Body([face], [object()]))
    assert collect_body_count(model)["solid_body_count"] == 1
    assert collect_signed_part_bounds(model) == [-30.0, -20.0, -10.0, 30.0, 20.0, 5.0]
    plane = collect_plane_face(face, model.Extension, {"base": "YmFzZQ=="})
    assert plane["owner_source_feature_id"] == "base" and plane["offset_z_mm"] == 5
    assert plane["native_reference"]
    face.Normal = [0, 0, -1]
    inverted = collect_plane_face(face, model.Extension, {"base": "YmFzZQ=="})
    assert inverted["normal"] == [0, 0, -1]
    assert inverted["surface_normal_raw"] == [0, 0, 1]
    slot = lambda: type("ByRef", (), {"value": False})()
    evidence = collect_rebuild_evidence(model, features,
        {key: capture_persistent_reference(model.Extension, value, "FEATURE")["encoded"]
         for key, value in features.items()}, warning_slot_factory=slot,
        resolve_sketch=lambda *_: True, resolve_plane=lambda *_: True,
        resolve_dependencies=lambda *_: True)
    assert evidence["rebuild_result"] is True
    assert len(evidence["features"]) == 3


def test_reference_plane_coordinate_conversion():
    class RefPlane:
        def GetRefPlaneParams(self):
            return [0, 0, -0.005, 1, 0, 0, 0, 0, 1]
    class RefFeature(Feature):
        def GetSpecificFeature2(self):
            return RefPlane()
    result = collect_reference_plane(RefFeature("plane"), Extension(), name="Plane_Boss_Top")
    assert result["offset_z_mm"] == 5.0


def test_reopened_sketch_dependency_uses_persistent_parent_and_support():
    base = Feature("base")
    class Profile(Feature):
        def GetTypeName2(self):
            return "ProfileFeature"
        def GetParents(self):
            return [base]
    profile = Profile("sketch")
    class Boss(Feature):
        def GetParents(self):
            return [profile]
        def GetFirstSubFeature(self):
            return None
    boss = Boss("boss")
    support = Face("base-top", base)
    model = Model(Body([support], []))
    ext = model.Extension
    sketch_ref = capture_persistent_reference(ext, profile, "SKETCH")["encoded"]
    base_ref = capture_persistent_reference(ext, base, "FEATURE")["encoded"]
    face_record = capture_persistent_reference(ext, support, "FACE")
    evidence = collect_sketch_support(model, boss, expected_sketch_ref=sketch_ref,
        expected_parent_ref=base_ref, selected_support=face_record,
        support_geometry={"normal": [0, 0, 1], "offset_z_mm": 5},
        owner_refs={"base": base_ref},
        resolver=lambda _, raw: (support, 0) if raw == b"base-top" else (None, 1))
    assert evidence["status"] == "PASS"
    assert evidence["selected_support"]["resolved"] is True
    with pytest.raises(NativeEvidenceUnavailable):
        collect_sketch_support(model, boss, expected_sketch_ref=sketch_ref,
            expected_parent_ref="other", selected_support=face_record,
            support_geometry={"normal": [0, 0, 1], "offset_z_mm": 5},
            owner_refs={"base": base_ref},
            resolver=lambda *_: (support, 0))


def test_hole_openings_require_two_native_exterior_adjacencies():
    wall = Face("wall", Feature("hole"))
    bottom = Face("bottom", Feature("base"))
    top = Face("top", Feature("boss"))
    @dataclass
    class Edge:
        ref: str
        adjacent: list

        def GetTwoAdjacentFaces2(self):
            return self.adjacent
    wall.edges = [Edge("e1", [wall, bottom]), Edge("e2", [wall, top])]
    ext = Extension()
    refs = [capture_persistent_reference(ext, face, "FACE")["encoded"] for face in (bottom, top)]
    found = classify_r003_hole_openings(wall, bottom_face_ref=refs[0],
                                       top_face_ref=refs[1], extension=ext)
    assert found["bottom_opening"] and found["top_opening"] and found["blind_cap_absent"]
    wall.edges = [Edge("e1", [wall, bottom]), Edge("e2", [wall, Face("cap", Feature("hole"))])]
    blind = classify_r003_hole_openings(wall, bottom_face_ref=refs[0],
                                       top_face_ref=refs[1], extension=ext)
    assert not blind["top_opening"] and not blind["blind_cap_absent"]


def test_complete_bounded_brep_rejects_extra_face_and_blind_cap():
    base, boss, hole = (Feature(name) for name in ("base", "boss", "hole"))
    class Surface:
        def __init__(self, cylinder=False):
            self.IsCylinder = cylinder
            self.IsPlane = not cylinder
            self.CylinderParams = [0, 0, 0, 0, 0, 1, .002]
    class NativeFace(Face):
        def __init__(self, ref, owner, box, cylinder=False):
            super().__init__(ref, owner, Surface(cylinder))
            self.box = box
        def GetBox(self):
            return self.box
        def FaceInSurfaceSense(self):
            return True
    class Edge:
        def __init__(self, ref, faces):
            self.ref, self.faces = ref, faces
        def GetTwoAdjacentFaces2(self):
            return self.faces
    def box(x0, y0, z0, x1, y1, z1):
        return [v / 1000 for v in (x0, y0, z0, x1, y1, z1)]
    base_faces = [
        NativeFace("base-bottom", base, box(-30, -20, -10, 30, 20, -10)),
        NativeFace("base-top", base, box(-30, -20, 0, 30, 20, 0)),
        NativeFace("base-x0", base, box(-30, -20, -10, -30, 20, 0)),
        NativeFace("base-x1", base, box(30, -20, -10, 30, 20, 0)),
        NativeFace("base-y0", base, box(-30, -20, -10, 30, -20, 0)),
        NativeFace("base-y1", base, box(-30, 20, -10, 30, 20, 0)),
    ]
    boss_faces = [
        NativeFace("boss-top", boss, box(-10, -10, 5, 10, 10, 5)),
        NativeFace("boss-x0", boss, box(-10, -10, 0, -10, 10, 5)),
        NativeFace("boss-x1", boss, box(10, -10, 0, 10, 10, 5)),
        NativeFace("boss-y0", boss, box(-10, -10, 0, 10, -10, 5)),
        NativeFace("boss-y1", boss, box(-10, 10, 0, 10, 10, 5)),
    ]
    wall = NativeFace("hole-wall", hole, box(-2, -2, -10, 2, 2, 5), True)
    wall.edges = [Edge("hole-bottom-edge", [wall, base_faces[0]]),
                  Edge("hole-top-edge", [wall, boss_faces[0]])]
    base_faces[1].edges = [Edge(f"interface-{index}", [base_faces[1], side])
                           for index, side in enumerate(boss_faces[1:])]
    model = Model(Body(base_faces + boss_faces + [wall], []))
    refs = {key: capture_persistent_reference(model.Extension, feature, "FEATURE")["encoded"]
            for key, feature in (("base", base), ("boss", boss), ("hole", hole))}
    result = collect_r003_brep(model, refs, base_id="base", boss_id="boss",
                               hole_id="hole", through_all_created=True)
    assert result["part_bounds"] == {"xmin": -30, "ymin": -20, "zmin": -10,
                                      "xmax": 30, "ymax": 20, "zmax": 5}
    assert len(result["face_accounting"]) == 12
    assert result["hole"]["bottom_opening"] and result["hole"]["top_opening"]
    assert result["hole"]["blind_cap_absent"]
    ownership = collect_bounded_r003_ownership(model, refs, base_id="base",
        boss_id="boss", hole_id="hole", interface_z_mm=0)
    assert ownership["unknown"] == ownership["unattributed"] == ownership["misattributed"] == 0
    assert ownership["total_face_count"] == 12
    model.body.faces.append(NativeFace("unattributed", Feature("other"),
                                       box(-1, -1, 0, 1, 1, 0)))
    unknown = collect_bounded_r003_ownership(model, refs, base_id="base",
        boss_id="boss", hole_id="hole", interface_z_mm=0)
    assert unknown["unattributed"] == 1 and not unknown["persistent_mapping_resolved"]
    model.body.faces.pop()
    model.body.faces.append(NativeFace("unexpected-planar", boss,
                                       box(-1, -1, 5, 1, 1, 5)))
    with pytest.raises(NativeEvidenceUnavailable):
        collect_r003_brep(model, refs, base_id="base", boss_id="boss",
                          hole_id="hole", through_all_created=True)
    model.body.faces.pop()
    model.body.faces.append(NativeFace("blind-cap", hole,
                                       box(-2, -2, -10, 2, 2, -10)))
    with pytest.raises(NativeEvidenceUnavailable):
        collect_r003_brep(model, refs, base_id="base", boss_id="boss",
                          hole_id="hole", through_all_created=True)


def test_ownership_all_faces_and_no_display_name_fallback():
    base, hole = Feature("base"), Feature("hole")
    faces = [Face("f-base", base), Face("f-hole", hole)]
    report = collect_r003_ownership(Model(Body(faces, [])),
        {"base": "YmFzZQ==", "hole": "aG9sZQ=="},
        role_for_face=lambda face, owner: "HOLE_WALL" if owner == "hole" else "BASE")
    assert report["persistent_mapping_resolved"] is True
    assert report["unknown"] == report["unattributed"] == report["misattributed"] == 0
    bad = collect_r003_ownership(Model(Body([Face("f", Feature("unknown"))], [])),
        {"base": "YmFzZQ=="}, role_for_face=lambda *_: "BASE")
    assert bad["unattributed"] == 1 and bad["persistent_mapping_resolved"] is False


def test_view_transform_direct_native_basis_rejects_right_as_left():
    class Transform:
        ArrayData = [0., 0., -1., 0., 1., 0., 1., 0., 0.,
                     0., 0., 0., 1., 0., 0., 0.]
    class View:
        ModelToViewTransform = Transform()
        Position = [0.04, 0.12]
        ScaleRatio = [1, 2]
        def GetOrientationName(self):
            return "*Left"
    row = normalize_native_view_transform(View(), expected_role="LEFT",
        model_reference="part.sldprt", projection_standard="THIRD_ANGLE")
    assert row["normal"] == [-1, 0, 0] and row["model_to_view_transform"][:3] == [0, 0, 1]
    assert row["native_transform"]["axis_mapping"]["model_x_in_view"] == [0, 0, -1]
    View.ModelToViewTransform.ArrayData = [1., 0., 0., 0., 1., 0., 0., 0., 1.,
                                           0., 0., 0., 1., 0., 0., 0.]
    with pytest.raises(NativeEvidenceUnavailable):
        normalize_native_view_transform(View(), expected_role="LEFT",
            model_reference="part.sldprt", projection_standard="THIRD_ANGLE")


def test_injected_session_identity_blocks_unknown_before_factory():
    identity = OwnedProcessIdentity(123, "start-A", "launcher", "run1", "OWNED_NEW_INSTANCE")
    called = []
    adapter = R003SolidWorksSessionAdapter.from_factory(lambda: called.append(1),
        identity=identity, identity_probe=lambda _: {"pid": 123, "create_time": "start-B", "source": "launcher"},
        run_id="run1", expected_major_version=2024)
    with pytest.raises(SessionOwnershipUnproven):
        adapter.new_part()
    assert called == []
    assert not verify_owned_identity(identity, {"pid": 123, "create_time": "start-B",
                                                "source": "launcher"}, "run1")


def test_new_instance_ownership_all_required_counterexamples(tmp_path):
    executable = str(tmp_path / "SLDWORKS.exe")
    old = {"pid": 11, "creation_time": 90.0, "executable_path": executable}
    fresh = {"pid": 22, "creation_time": 101.0, "executable_path": executable}
    def proof(before, after, com, observed=None):
        return prove_r003_new_instance(before, after, launch_started_at=100.0,
                                       com_pid=com,
                                       observed_after_bind=observed or fresh)
    assert proof([], [fresh], 22)["ownership_proven"] is True  # A
    assert proof([old], [old, fresh], 11)["ownership_proven"] is False  # B
    assert proof([old], [old], 11)["ownership_proven"] is False  # C
    second = {"pid": 33, "creation_time": 101.1, "executable_path": executable}
    assert proof([old], [old, fresh, second], 22)["ownership_proven"] is False  # D
    assert proof([], [fresh], 33)["ownership_proven"] is False  # E
    assert proof([], [{**fresh, "creation_time": None}], 22)["ownership_proven"] is False  # F
    assert proof([], [fresh], 22, {"pid": None})["ownership_proven"] is False  # G
    rounded = {**fresh, "creation_time": 100.000}
    assert prove_r003_new_instance([], [rounded], launch_started_at=100.0005,
                                   com_pid=22, observed_after_bind=rounded)["ownership_proven"] is True
    assert prove_r003_new_instance([], [rounded], launch_started_at=100.002,
                                   com_pid=22, observed_after_bind=rounded)["ownership_proven"] is False


def test_controlled_launcher_binds_only_new_pid_fake(tmp_path):
    executable = tmp_path / "SLDWORKS.exe"
    executable.write_bytes(b"fake executable never run")
    old = {"pid": 11, "creation_time": 90.0, "executable_path": str(executable)}
    fresh = {"pid": 22, "creation_time": 101.0, "executable_path": str(executable)}
    from itertools import chain, repeat
    snapshots = chain([[old]], repeat([old, fresh]))
    class FakeCom:
        def GetProcessID(self):
            return 22
    sw, identity, evidence = launch_r003_owned_com(
        executable, run_id="r003-run", launch_timeout_s=1,
        snapshot=lambda: next(snapshots),
        start_process=lambda argv: type("Process", (), {"pid": 22})(),
        bind_pid=lambda pid: FakeCom(), clock=lambda: 100.0,
        monotonic=iter(i * 0.01 for i in range(100)).__next__, pause=lambda _: None)
    assert sw.GetProcessID() == identity.pid == 22
    assert evidence["ownership_proven"] is True


@pytest.mark.parametrize("case,should_pass", [
    ("immediate", True), ("delayed", True), ("missing", False),
    ("disappeared", False), ("second_pid", False),
    ("creation_mismatch", False), ("path_mismatch", False),
    ("com_pid_mismatch", False),
])
def test_controlled_launcher_identity_stabilization_cases(tmp_path, case, should_pass):
    from itertools import chain, repeat

    executable = tmp_path / "SLDWORKS.exe"
    executable.write_bytes(b"fake executable never run")
    full = {"pid": 22, "creation_time": 100.1, "executable_path": str(executable)}
    partial = {"pid": 22, "creation_time": None, "executable_path": None}
    wrong_time = {**full, "creation_time": 99.0}
    wrong_path = {**full, "executable_path": str(tmp_path / "another" / "SLDWORKS.exe")}
    second = {"pid": 33, "creation_time": 100.1, "executable_path": str(executable)}
    sequences = {
        "immediate": [[full], [full], [full], [full]],
        "delayed": [[partial], [partial], [full], [full], [full], [full]],
        "missing": [[partial]],
        "disappeared": [[partial], []],
        "second_pid": [[partial], [partial, second]],
        "creation_mismatch": [[wrong_time]],
        "path_mismatch": [[wrong_path]],
        "com_pid_mismatch": [[full], [full], [full], [full]],
    }
    rows = sequences[case]
    snapshots = chain([[]], rows, repeat(rows[-1]))
    ticks = iter(i * 0.01 for i in range(10000))

    class FakeCom:
        def GetProcessID(self):
            return 33 if case == "com_pid_mismatch" else 22

    kwargs = dict(run_id="r003-run", launch_timeout_s=1,
                  snapshot=lambda: next(snapshots),
                  start_process=lambda argv: type("Process", (), {"pid": 22})(),
                  bind_pid=lambda pid: FakeCom(), clock=lambda: 100.0,
                  monotonic=lambda: next(ticks), pause=lambda _: None)
    if should_pass:
        _, identity, evidence = launch_r003_owned_com(executable, **kwargs)
        assert identity.pid == 22 and evidence["ownership_proven"] is True
        states = [row["state"] for row in evidence["process_identity_timeline"]]
        assert states[-3:] == ["IDENTITY_COMPLETE", "COM_BOUND", "OWNED"]
        if case == "delayed":
            assert any(row.get("metadata_complete") is False for row in
                       evidence["process_identity_timeline"])
    else:
        with pytest.raises(SessionOwnershipUnproven) as caught:
            launch_r003_owned_com(executable, **kwargs)
        assert caught.value.evidence["ownership_proven"] is False
        assert caught.value.evidence["process_identity_timeline"][-1]["state"] == "FAILED"


def test_injected_owned_session_normalizes_lifecycle_without_real_com(tmp_path):
    identity = OwnedProcessIdentity(123, "start-A", "launcher", "run1", "OWNED_NEW_INSTANCE")
    state = {"alive": True, "quit": 0}
    class NativeApp:
        def GetProcessID(self):
            return 123
        def RevisionNumber(self):
            return "32.0.0"
    class NativePart:
        path = ""
        def GetTitle(self):
            return Path(self.path).name if self.path else "Part1"
        def GetPathName(self):
            return self.path
    class InjectedSession:
        sw = NativeApp()
        connection_info = {"started_by_cad_studio": False}
        def new_part(self, template_path=None):
            return NativePart()
        def save(self, model, path):
            Path(path).write_bytes(b"injected native save")
            model.path = path
            return True
        def close(self, title):
            return title == "part.sldprt"
        def r003_quit_proven_owned(self, record):
            state["quit"] += 1
            state["alive"] = False
            return True
    def probe(_):
        return {"pid": 123 if state["alive"] else None,
                "create_time": "start-A" if state["alive"] else None,
                "source": "launcher"}
    adapter = R003SolidWorksSessionAdapter.from_factory(InjectedSession,
        identity=identity, identity_probe=probe, run_id="run1", expected_major_version=2024)
    assert adapter.new_part() is not None
    assert adapter.connection_info["ownership_proven"] is True
    assert adapter.sw_major_version == 2024
    assert adapter.save_part(tmp_path / "part.sldprt")
    assert adapter.close_part()
    assert adapter.quit_owned_instance()["process_exited"] is True
    assert state["quit"] == 1


def test_owned_quit_acknowledged_but_pid_alive_is_not_cleanup_pass():
    identity = OwnedProcessIdentity(123, "start-A", "launcher", "run1", "OWNED_NEW_INSTANCE")
    class App:
        def GetProcessID(self):
            return 123
        def RevisionNumber(self):
            return "32.0"
    class Session:
        sw = App()
        connection_info = {}
        def r003_quit_proven_owned(self, record):
            return True
    adapter = R003SolidWorksSessionAdapter.from_factory(Session, identity=identity,
        identity_probe=lambda _: {"pid": 123, "create_time": "start-A", "source": "launcher"},
        run_id="run1", expected_major_version=2024)
    adapter.connect()
    report = adapter.quit_owned_instance()
    assert report["process_exited"] is False
    assert report["com_released"] is False
    assert report["reason"] == "owned PID still alive"


def test_native_owned_session_never_exits_borrowed_or_open_document():
    state = {"documents": [], "exit_calls": 0}
    class App:
        def GetProcessID(self):
            return 123
        def GetDocumentCount(self):
            return len(state["documents"])
        def GetFirstDocument(self):
            return state["documents"][0] if state["documents"] else None
        def ExitApp(self):
            state["exit_calls"] += 1
    session = R003OwnedNativeSession(App(), object())
    borrowed = OwnedProcessIdentity(123, "start", "launcher", "run", "UNPROVEN")
    owned = OwnedProcessIdentity(123, "start", "launcher", "run", "OWNED_NEW_INSTANCE")
    assert session.r003_quit_proven_owned(borrowed) is False
    state["documents"] = [type("Doc", (), {"GetTitle": lambda self: "user.sldprt",
                                           "GetNext": lambda self: None})()]
    assert session.r003_quit_proven_owned(owned) is False
    assert state["exit_calls"] == 0
    state["documents"] = []
    assert session.r003_quit_proven_owned(owned) is True
    assert state["exit_calls"] == 1


def test_process_b_reopens_disk_document_not_process_a_object(tmp_path, monkeypatch):
    import solidworks_main_skill.backends.solidworks_automation.r003_native_adapter as adapter_module

    part_path = tmp_path / "saved.sldprt"
    part_path.write_bytes(b"sealed fake model")
    class App:
        def GetProcessID(self):
            return 123
        def RevisionNumber(self):
            return "32.0"
    class Reopened:
        def GetTitle(self):
            return "saved.sldprt"
        def GetPathName(self):
            return str(part_path)
    reopened_document = Reopened()
    class Session:
        sw = App()
        connection_info = {}
        def open(self, path, *, read_only, silent):
            assert Path(path) == part_path and read_only and silent
            return reopened_document
    identity = OwnedProcessIdentity(123, "start-A", "launcher", "run1", "OWNED_NEW_INSTANCE")
    adapter = R003SolidWorksSessionAdapter.from_factory(Session, identity=identity,
        identity_probe=lambda _: {"pid": 123, "create_time": "start-A", "source": "launcher"},
        run_id="run1", expected_major_version=2024)
    ledger = {"feature_refs": {"base": "a", "boss": "b", "hole": "c"},
              "sketch_refs": {}, "support_refs": {}, "support_geometry": {},
              "ordered_source_ids": ["base", "boss", "hole"]}
    monkeypatch.setattr(adapter_module, "collect_reopened_r003_dependencies",
        lambda model, *args, **kwargs: {"features": {}, "lineage": {}} if model is reopened_document else None)
    monkeypatch.setattr(adapter_module, "collect_rebuild_evidence", lambda *args, **kwargs: {"features": []})
    monkeypatch.setattr(adapter_module, "collect_r003_brep", lambda *args, **kwargs: {"interface": {"z_mm": 0}})
    monkeypatch.setattr(adapter_module, "collect_bounded_r003_ownership", lambda *args, **kwargs: {"unknown": 0})
    monkeypatch.setattr(adapter_module, "collect_body_count", lambda *args, **kwargs: {"solid_body_count": 1})
    result = adapter.reopen_native_part_and_collect(part_path, ledger, through_all_created=True)
    assert result["reopen_status"] == "PASS" and adapter._part is reopened_document
    with pytest.raises(NativeEvidenceUnavailable):
        adapter.reopen_native_part_and_collect(part_path, ledger, through_all_created=True)


def test_bounded_native_base_boss_hole_binding_with_fake_native_objects():
    from test_r003_real_validation_bridge import plan

    class RefPlane(Feature):
        def GetTypeName2(self):
            return "RefPlane"
        def GetSpecificFeature2(self):
            return self
        def GetRefPlaneParams(self):
            return [0, 0, 0, 1, 0, 0, 0, 0, 1]

    class Profile(Feature):
        def __init__(self, ref, parent):
            super().__init__(ref)
            self.parent = parent
        def GetTypeName2(self):
            return "ProfileFeature"
        def GetParents(self):
            return [self.parent]

    class Extrusion(Feature):
        def __init__(self, ref, profile, kind="BossExtrude"):
            super().__init__(ref)
            self.profile = profile
            self.kind = kind
        def GetTypeName2(self):
            return self.kind
        def GetParents(self):
            return [self.profile]
        def GetFirstSubFeature(self):
            return None

    class NativeFace(Face):
        def __init__(self, ref, owner, z):
            super().__init__(ref, owner)
            self.surface = type("Surface", (), {"IsPlane": True,
                "PlaneParams": [0, 0, 1, 0, 0, z / 1000.0]})()
        def Select2(self, append, mark):
            model.selected = self
            return True

    class NativeModel(Model):
        def __init__(self):
            super().__init__(Body([], [object()]))
            self.front = RefPlane("front")
            self.selected = None
            self.SketchManager = type("SketchManager", (),
                {"InsertSketch": lambda self, start: True})()
        def ClearSelection2(self, clear):
            return True

    model = NativeModel()
    objects = {"front": model.front}
    class Api:
        @contextmanager
        def sketch(self, part, plane):
            assert plane == "Front Plane"
            yield "BaseSketch"
        def sketch_rectangle(self, part, cx, cy, width, height):
            assert width > 0 and height > 0
        def sketch_circle(self, part, x, y, radius):
            assert radius > 0
        def current_sketch_name(self, part):
            return "FaceSketch"
        def end_sketch(self, part):
            return "FaceSketch"
        def extrude_boss(self, part, sketch, depth, *, direction, merge):
            if not objects.get("base"):
                assert direction is True  # real Front-plane probe: -Z base interval
                feature = Extrusion("base", Profile("base-sketch", part.front))
                objects["base"] = feature
                face = NativeFace("base-top", feature, 0)
                part.body.faces.append(face)
                objects["base-top"] = face
            else:
                assert part.selected is objects["base-top"]
                assert direction is False  # real face-support probe: outward +Z boss
                feature = Extrusion("boss", Profile("boss-sketch", objects["base"]))
                objects["boss"] = feature
                face = NativeFace("boss-top", feature, 5)
                part.body.faces.append(face)
                objects["boss-top"] = face
            return feature
        def extrude_cut(self, part, sketch, depth, *, direction):
            assert part.selected is objects["boss-top"] and depth == 0
            assert direction is True  # native ThroughAll creation from Boss top
            feature = Extrusion("hole", Profile("hole-sketch", objects["boss"]), "CutExtrude")
            objects["hole"] = feature
            return feature

    class App:
        def GetProcessID(self):
            return 123
        def RevisionNumber(self):
            return "32.0"
    class Session:
        sw = App()
        connection_info = {"started_by_cad_studio": False}
        def new_part(self, template_path=None):
            return model
    model.GetTitle = lambda: "R003Part"
    identity = OwnedProcessIdentity(123, "start-A", "launcher", "run1", "OWNED_NEW_INSTANCE")
    adapter = R003SolidWorksSessionAdapter.from_factory(Session, identity=identity,
        identity_probe=lambda _: {"pid": 123, "create_time": "start-A", "source": "launcher"},
        run_id="run1", expected_major_version=2024, part_api=Api(),
        warning_slot_factory=lambda: type("ByRef", (), {"value": False})(),
        reference_resolver=lambda _, raw: (objects.get(raw.decode("ascii")), 0))
    adapter.new_part()
    base, boss, hole = plan().operations
    base_native = adapter.create_base(base)
    base_plane, base_top = adapter.base_plane_evidence(base, base_native)
    assert base_plane["offset_z_mm"] == base_top["offset_z_mm"] == 0
    boss_native = adapter.create_boss(boss)
    boss_plane, boss_top = adapter.boss_plane_evidence(boss, boss_native)
    assert boss_plane["offset_z_mm"] == boss_top["offset_z_mm"] == 5
    hole_native = adapter.create_hole(hole)
    assert hole_native["through_all_requested"] is True
    assert [row["persistent_reference"] for row in (base_native, boss_native, hole_native)]
    rebuilt = adapter.initial_rebuild(plan(), [base_native, boss_native, hole_native])
    assert rebuilt["rebuild_result"] is True
    assert all(row["dependencies_resolved"] for row in rebuilt["features"])
    reopened = collect_reopened_r003_dependencies(model, adapter._feature_refs,
        adapter._sketch_refs, adapter._support_refs, adapter._support_geometry,
        [op.source_feature_id for op in plan().operations],
        resolver=lambda _, raw: (objects.get(raw.decode("ascii")), 0))
    assert reopened["status"] == "PASS"
    assert len(reopened["lineage"]) == 3


class _DrawingOle:
    def __init__(self, owner):
        self.owner = owner

    def QueryInterface(self, _iid):
        class Unknown:
            def __init__(self, owner):
                self.owner = owner
            def __repr__(self):
                return f"<PyIUnknown at 0x1 with obj at 0x{id(self.owner):016X}>"
        return Unknown(self.owner)


class _DrawingView:
    def __init__(self, drawing, name):
        self.drawing, self.Name, self.Position = drawing, name, [0, 0]
        self._oleobj_ = _DrawingOle(self)

    def GetNextView(self):
        if self is self.drawing.sheet:
            return self.drawing.temporary[0] if self.drawing.temporary else None
        index = self.drawing.temporary.index(self)
        return self.drawing.temporary[index + 1] if index + 1 < len(self.drawing.temporary) else None


class _DrawingFeature:
    def __init__(self, drawing, view):
        self.drawing, self.view = drawing, view

    def GetSpecificFeature(self):
        return self.view

    def Select2(self, append, mark):
        assert append is False and mark == 0
        self.drawing.selected = self.view
        return True


class _DrawingSelectionManager:
    def __init__(self, drawing):
        self.drawing = drawing

    def GetSelectedObjectCount2(self, mark):
        return int(self.drawing.selected is not None)

    def GetSelectedObject6(self, index, mark):
        assert index == 1
        return self.drawing.selected


class _DrawingForNativeViews:
    def __init__(self, names):
        self.temporary = [_DrawingView(self, name) for name in names]
        self.sheet = _DrawingView(self, "sheet")
        self.selected = None
        self.SelectionManager = _DrawingSelectionManager(self)
        self.created = []
        self.path = ""

    def GetTitle(self):
        return "drawing.slddrw"

    def GetPathName(self):
        return self.path

    def Create3rdAngleViews2(self, path):
        return True

    def GetFirstView(self):
        return self.sheet

    def ClearSelection2(self, clear):
        self.selected = None

    def FeatureByName(self, name):
        return next((_DrawingFeature(self, view) for view in self.temporary if view.Name == name), None)

    def EditDelete(self):
        self.temporary.remove(self.selected)
        self.selected = None

    def CreateDrawViewFromModelView3(self, path, native, x, y, angle):
        self.created.append(native)
        return _DrawingView(self, native)

    def ForceRebuild3(self, top_only):
        return True


def test_native_drawing_creation_requests_physical_left_not_right():
    class Part:
        def GetModelViewNames(self):
            return ["*Front", "*Top", "*Left", "*Right"]
    drawing = _DrawingForNativeViews(["front-auto", "top-auto", "right-auto"])
    result = create_r003_physical_views(drawing, Part(), "part.sldprt",
                                        empty_variant_factory=lambda: object())
    assert drawing.created == ["*Front", "*Top", "*Left"]
    assert drawing.temporary == []
    assert result["views"]["LEFT"]["native_model_view_name"] == "*Left"


def test_owned_adapter_drawing_full_fake_lifecycle(tmp_path):
    class Part:
        def GetTitle(self):
            return "part.sldprt"
        def GetModelViewNames(self):
            return ["*Front", "*Top", "*Left", "*Right"]
    state = {"alive": True, "closed": []}
    drawing = _DrawingForNativeViews(["auto-f", "auto-t", "auto-r"])
    class App:
        def GetProcessID(self):
            return 123
        def RevisionNumber(self):
            return "32.0"
    class Session:
        sw = App()
        connection_info = {}
        def open(self, path, *, read_only, silent):
            assert read_only and silent
            return Part()
        def new_drawing(self):
            return drawing
        def save(self, model, path):
            Path(path).write_bytes(b"fake drawing")
            model.path = path
            return True
        def close(self, *, title):
            state["closed"].append(title)
            return True
        def r003_quit_proven_owned(self, identity):
            state["alive"] = False
            return True
    identity = OwnedProcessIdentity(123, "start-A", "launcher", "run1", "OWNED_NEW_INSTANCE")
    adapter = R003SolidWorksSessionAdapter.from_factory(Session, identity=identity,
        identity_probe=lambda _: {"pid": 123 if state["alive"] else None,
                                  "create_time": "start-A" if state["alive"] else None,
                                  "source": "launcher"},
        run_id="run1", expected_major_version=2024,
        empty_variant_factory=lambda: object())
    part_path, drawing_path = tmp_path / "part.sldprt", tmp_path / "drawing.slddrw"
    part_path.write_bytes(b"fake part")
    result = adapter.create_physical_left_drawing(part_path, drawing_path)
    assert result["status"] == "PASS" and result["drawing_closed"]
    assert drawing.created == ["*Front", "*Top", "*Left"]
    assert state["closed"] == ["drawing.slddrw", "part.sldprt"]
    assert adapter.quit_owned_instance()["process_exited"] is True


def test_physical_role_hlv_hlr_extraction_requires_stable_frame():
    names = {"FRONT": "*Front", "TOP": "*Top", "LEFT": "*Left"}
    line = {"x1": 0.0, "y1": 0.0, "x2": 10.0, "y2": 0.0}
    hidden = {"x1": 0.0, "y1": 5.0, "x2": 10.0, "y2": 5.0}
    def row(name):
        return {"native_identity": name, "lines": [line], "circles": [], "arcs": [],
                "frame_origin_mm": [0, 0], "scale": 1.0,
                "sheet_position": [0.1, 0.1], "unclassified_polylines": []}
    hlr = {role: row(name) for role, name in names.items()}
    hlv = {role: row(name) for role, name in names.items()}
    hlv["LEFT"]["lines"].append(hidden)
    result = normalize_r003_hlv_hlr(hlr, hlv,
        reviewed_projection_graph_digest="a" * 64)
    assert result["LEFT"]["hidden_lines"]
    assert result["LEFT"]["native_identity"] == "*Left"
    hlv["LEFT"]["frame_origin_mm"] = [1, 0]
    with pytest.raises(NativeEvidenceUnavailable):
        normalize_r003_hlv_hlr(hlr, hlv, reviewed_projection_graph_digest="a" * 64)


def test_reopened_native_hlr_hlv_lines_are_extracted_not_simulated():
    visible = [0, 0, 0, 0, 0, 0, 0, 0, 2, 0, 0, 0, .01, 0, 0]
    hidden = [0, 0, 0, 0, 0, 0, 0, 0, 2, 0, .005, 0, .01, .005, 0]
    class View:
        ScaleRatio = [1, 2]
        Position = [.04, .12]
        ReferencedDocument = type("Referenced", (), {"GetPathName": lambda self: "part.sldprt"})()
        def __init__(self):
            self.mode = None
        def GetOrientationName(self):
            return "*Left"
        def GetReferencedModelName(self):
            return "part.sldprt"
        def SetDisplayMode3(self, use_parent, mode, facetted, edges):
            self.mode = mode
            return True
        def GetPolyLinesAndCurves(self, crosshatch):
            assert crosshatch == 1
            return visible if self.mode == 2 else visible + hidden
    class Drawing:
        def ForceRebuild3(self, top_only):
            return True
    hlr, hlv = collect_r003_native_view_modes(Drawing(), View(), role="LEFT",
                                               model_path="part.sldprt")
    assert len(hlr["lines"]) == 1 and len(hlv["lines"]) == 2
    assert hlr["lines"][0]["x2"] == 10.0
    assert hlv["mode"] == "HLV"
    class Right(View):
        def GetOrientationName(self):
            return "*Right"
    with pytest.raises(NativeEvidenceUnavailable):
        collect_r003_native_view_modes(Drawing(), Right(), role="LEFT",
                                       model_path="part.sldprt")


def test_python_read_audit_and_output_root_fail_closed(tmp_path):
    allowed = tmp_path / "reviewed"
    allowed.mkdir()
    source = allowed / "source.json"
    source.write_text("{}")
    truth = allowed / "truth"
    truth.mkdir()
    (truth / "answer.json").write_text("{}")
    audit = GenerationReadAudit(allowed_roots=[allowed], forbidden_roots=[truth])
    assert audit.evidence()["audit_status"] == "INACTIVE"
    with audit.active():
        assert source.read_text() == "{}"
        with pytest.raises(Exception):
            (truth / "answer.json").read_text()
    assert len(audit.reads) == 1 and len(audit.denied) == 1
    assert audit.evidence()["audit_status"] == "COMPLETE"
    assert audit.evidence()["generation_truth_reads"] == 1
    outside = tmp_path / "ordinary.txt"
    outside.write_text("ordinary")
    with audit.active():
        with pytest.raises(Exception):
            outside.read_text()
    assert len(audit.denied) == 2
    assert audit.evidence()["generation_truth_reads"] == 1
    assert [row["category"] for row in audit.evidence()["events"]] == [
        "ALLOWED_READ", "TRUTH_READ", "FORBIDDEN_GENERATION_READ"]
    with pytest.raises(Exception):
        validate_real_output_root(Path(r"C:relative"), repository=tmp_path)
    with pytest.raises(Exception):
        validate_real_output_root(Path(r"\\server\share"), repository=tmp_path)
