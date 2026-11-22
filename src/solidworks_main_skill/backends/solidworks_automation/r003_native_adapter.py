"""Bounded R003 session adapter; no CAD connection occurs at import or init.

The upstream SolidWorksSession attaches first. A production launcher must supply
independent, externally verifiable proof of a newly created process. Without it
this adapter refuses to create any document. The launcher is intentionally
injected; this module never calls Dispatch or GetActiveObject itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import importlib
import json
import os
import subprocess
import sys
import time
from typing import Any, Callable, Mapping

from .r003_native_collectors import (
    NativeEvidenceUnavailable, capture_persistent_reference, collect_feature_error,
    collect_plane_face, collect_rebuild_evidence, collect_reference_plane,
    collect_sketch_support, collect_reopened_r003_dependencies,
    collect_body_count, collect_r003_brep, collect_bounded_r003_ownership,
    collect_r003_native_view_modes, normalize_native_view_transform,
    normalize_r003_hlv_hlr,
    member, native_profile_feature,
    _resolve_sw_reference as _native_reference_resolver,
    _warning_slot as _native_warning_slot,
)


class SessionOwnershipUnproven(RuntimeError):
    pass


def _empty_variant():
    import pythoncom
    from win32com.client import VARIANT
    return VARIANT(pythoncom.VT_EMPTY, None)


@dataclass(frozen=True)
class OwnedProcessIdentity:
    pid: int
    create_time: str
    source: str
    run_id: str
    ownership_status: str


def verify_owned_identity(identity: OwnedProcessIdentity,
                          observed: Mapping[str, Any], run_id: str) -> bool:
    """A PID alone is never sufficient, including after process-ID reuse."""
    return (isinstance(identity, OwnedProcessIdentity)
            and type(identity.pid) is int and identity.pid > 0
            and bool(identity.create_time) and bool(identity.source)
            and identity.run_id == run_id
            and identity.ownership_status == "OWNED_NEW_INSTANCE"
            and observed.get("pid") == identity.pid
            and observed.get("create_time") == identity.create_time
            and observed.get("source") == identity.source)


def prove_r003_new_instance(pre_launch: list[Mapping[str, Any]],
                            post_launch: list[Mapping[str, Any]], *,
                            launch_started_at: float, com_pid: int,
                            observed_after_bind: Mapping[str, Any]) -> dict:
    """Require one new OS process and a COM pointer to precisely that PID.

    Times are UTC epoch seconds; the same PID with a changed creation time is
    not accepted. This function has no process, COM, or filesystem side effects.
    """
    evidence = {"pre_launch_processes": [dict(row) for row in pre_launch],
                "post_launch_processes": [dict(row) for row in post_launch],
                "launch_started_at": launch_started_at,
                "com_process_id": com_pid, "ownership_proven": False,
                "ownership_failure_reason": None}
    try:
        if not isinstance(launch_started_at, (int, float)) or launch_started_at <= 0:
            raise SessionOwnershipUnproven("launch timestamp unavailable")
        pre_pids = {row["pid"] for row in pre_launch}
        candidates = [row for row in post_launch if row.get("pid") not in pre_pids]
        evidence["candidate_new_processes"] = [dict(row) for row in candidates]
        if len(candidates) != 1:
            raise SessionOwnershipUnproven("exactly one new SLDWORKS.exe PID required")
        selected = candidates[0]
        pid, created, executable = (selected.get("pid"), selected.get("creation_time"),
                                    selected.get("executable_path"))
        # The PowerShell snapshot stores milliseconds; time.time keeps finer
        # precision. A same-millisecond new PID must not fail as "predating".
        if (type(pid) is not int or pid <= 0 or type(created) not in (int, float)
                or created + 0.001 < launch_started_at or not isinstance(executable, str)
                or Path(executable).name.casefold() != "sldworks.exe"):
            raise SessionOwnershipUnproven("new process identity incomplete or predates launch")
        if com_pid != pid:
            raise SessionOwnershipUnproven("COM pointer bound to another SolidWorks PID")
        if (observed_after_bind.get("pid") != pid
                or observed_after_bind.get("creation_time") != created
                or observed_after_bind.get("executable_path", "").casefold() != executable.casefold()):
            raise SessionOwnershipUnproven("new PID exited or changed during COM binding")
        evidence.update({"selected_process_id": pid, "process_creation_time": created,
                         "executable_path": executable, "ownership_proven": True})
    except (KeyError, TypeError, SessionOwnershipUnproven) as exc:
        evidence["ownership_failure_reason"] = str(exc)
    return evidence


def _snapshot_solidworks_processes() -> list[dict]:
    """Read-only OS snapshot; no SolidWorks COM acquisition.

    Launch-time metadata may be temporarily unavailable. Only a complete,
    stable candidate can become owned; a missing PID makes the snapshot unusable.
    """
    if os.name != "nt":
        raise SessionOwnershipUnproven("Windows process snapshot required")
    command = ("$ErrorActionPreference='Stop'; "
               "$rows=@(Get-Process -Name SLDWORKS -ErrorAction SilentlyContinue | "
               "ForEach-Object { $created=$null; $path=$null; "
               "try {$created=([DateTimeOffset]$_.StartTime.ToUniversalTime()).ToUnixTimeMilliseconds()/1000.0} catch {}; "
               "try {$path=[string]$_.Path} catch {}; "
               "[pscustomobject]@{pid=[int]$_.Id; creation_time=$created; executable_path=$path} }); "
               "ConvertTo-Json -InputObject $rows -Compress -Depth 3")
    completed = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                               shell=False, capture_output=True, text=True, timeout=15, check=False)
    if completed.returncode != 0:
        raise SessionOwnershipUnproven("OS process snapshot unavailable: " + completed.stderr.strip())
    rows = json.loads(completed.stdout)
    if not isinstance(rows, list) or any(not isinstance(row, dict)
                                         or type(row.get("pid")) is not int or row["pid"] <= 0
                                         for row in rows):
        raise SessionOwnershipUnproven("OS process snapshot lacks valid PID identity")
    return rows


def _exact_pid_present(pid: int) -> bool:
    """Observe exact Windows PID existence after ExitApp, not image-name state."""
    if os.name != "nt" or type(pid) is not int or pid <= 0:
        raise SessionOwnershipUnproven("exact Windows PID required")
    command = ("$p=Get-Process -Id " + str(pid) + " -ErrorAction SilentlyContinue; "
               "if ($null -eq $p) {'ABSENT'} else {'PRESENT'}")
    completed = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive",
                                "-Command", command], shell=False,
                               capture_output=True, text=True, timeout=15, check=False)
    value = completed.stdout.strip()
    if completed.returncode != 0 or value not in {"ABSENT", "PRESENT"}:
        raise SessionOwnershipUnproven("exact PID exit observation unavailable")
    return value == "PRESENT"


def _bind_r003_rot_pid(pid: int) -> Any:
    """Bind only the PID-qualified SOLIDWORKS ROT moniker, never GetActiveObject."""
    import pythoncom
    from win32com.client import Dispatch

    rot = pythoncom.GetRunningObjectTable()
    context = pythoncom.CreateBindCtx(0)
    matches = []
    for moniker in rot.EnumRunning():
        try:
            display = moniker.GetDisplayName(context, None)
        except Exception:
            continue
        if display.casefold() == f"solidworks_pid_{pid}".casefold():
            matches.append(moniker)
    if len(matches) != 1:
        raise SessionOwnershipUnproven("unique PID-qualified SOLIDWORKS ROT moniker unavailable")
    # ROT.GetObject supplies IUnknown; pywin32 Dispatch requires IDispatch.
    # Keep the PID-qualified moniker restriction before this interface cast.
    sw = Dispatch(rot.GetObject(matches[0]).QueryInterface(pythoncom.IID_IDispatch))
    if int(member(sw, "GetProcessID")) != pid:
        raise SessionOwnershipUnproven("ROT COM object PID mismatch")
    return sw


def launch_r003_owned_com(executable_path: Path, *, run_id: str,
                          launch_timeout_s: float,
                          snapshot: Callable[[], list[Mapping[str, Any]]] = _snapshot_solidworks_processes,
                          start_process: Callable[[list[str]], Any] | None = None,
                          bind_pid: Callable[[int], Any] = _bind_r003_rot_pid,
                          clock: Callable[[], float] = time.time,
                          monotonic: Callable[[], float] = time.monotonic,
                          pause: Callable[[float], Any] = time.sleep) -> tuple[Any, OwnedProcessIdentity, dict]:
    """Launch a distinct process, then bind its PID-qualified ROT object.

    No general COM activation is attempted. An unproven spawned process is
    *not* exited or terminated here because ownership has not been proved.
    """
    executable = Path(executable_path)
    if (not executable.is_absolute() or not executable.is_file()
            or executable.name.casefold() != "sldworks.exe"
            or type(launch_timeout_s) not in (int, float) or launch_timeout_s <= 0):
        raise SessionOwnershipUnproven("invalid controlled-launch executable or timeout")
    before = snapshot()
    launched_at = clock()
    starter = start_process or (lambda argv: subprocess.Popen(argv, shell=False))
    process = starter([str(executable)])
    launched_pid = getattr(process, "pid", None)
    if type(launched_pid) is not int or launched_pid <= 0:
        raise SessionOwnershipUnproven("controlled launcher returned no PID")
    deadline = monotonic() + launch_timeout_s
    identity_deadline = None
    stable_sample = None
    pre_pids = {row["pid"] for row in before}
    timeline = [{"timestamp": launched_at, "state": "NOT_STARTED",
                 "launch_snapshot_initial": [dict(row) for row in before]},
                {"timestamp": clock(), "state": "LAUNCHED", "candidate_pid": launched_pid}]
    evidence: dict = {"pre_launch_processes": [dict(row) for row in before],
                      "launcher_pid": launched_pid, "launch_started_at": launched_at,
                      "process_identity_timeline": timeline, "ownership_proven": False}

    def fail(reason: str):
        timeline.append({"timestamp": clock(), "state": "FAILED", "reason": reason})
        evidence["ownership_failure_reason"] = reason
        failure = SessionOwnershipUnproven(reason)
        failure.evidence = evidence
        raise failure

    while monotonic() < deadline:
        try:
            after = snapshot()
        except Exception as exc:
            fail(f"OS process snapshot unavailable: {exc}")
        candidates = [row for row in after if row["pid"] not in pre_pids]
        if len(candidates) > 1:
            fail("second new SolidWorks PID appeared")
        if not candidates:
            if identity_deadline is not None:
                fail("candidate PID disappeared during identity stabilization")
            pause(0.25)
            continue
        row = candidates[0]
        if row["pid"] != launched_pid:
            fail("new SolidWorks PID differs from controlled launcher PID")
        if identity_deadline is None:
            identity_deadline = min(deadline, monotonic() + 30.0)
        created, path = row.get("creation_time"), row.get("executable_path")
        complete = type(created) in (int, float) and bool(path)
        timeline.append({"timestamp": clock(), "state": "PID_DETECTED_UNVERIFIED",
                         "candidate_pid": launched_pid, "creation_time": created,
                         "executable_path": path, "metadata_complete": complete})
        if type(created) in (int, float) and created + 0.001 < launched_at:
            fail("candidate creation time predates controlled launch")
        if path and Path(path).resolve() != executable.resolve():
            fail("candidate executable path mismatch")
        if complete:
            sample = (launched_pid, created, str(Path(path).resolve()))
            if stable_sample == sample:
                timeline.append({"timestamp": clock(), "state": "IDENTITY_COMPLETE",
                                 "candidate_pid": launched_pid, "creation_time": created,
                                 "executable_path": path, "metadata_complete": True})
                break
            if stable_sample is not None and stable_sample != sample:
                fail("candidate process identity changed during stabilization")
            stable_sample = sample
        if monotonic() >= identity_deadline:
            fail("candidate process identity metadata unavailable before stabilization deadline")
        pause(0.25)
    else:
        fail("controlled launch timed out before stable process identity")

    # ROT may register after OS identity; never use COM to substitute for it.
    while monotonic() < deadline:
        current = snapshot()
        new_rows = [item for item in current if item["pid"] not in pre_pids]
        if len(new_rows) != 1 or new_rows[0].get("pid") != launched_pid:
            fail("candidate disappeared or another new PID appeared before COM binding")
        if (new_rows[0].get("creation_time") != created
                or str(Path(new_rows[0].get("executable_path") or "").resolve()) != sample[2]):
            fail("candidate process identity changed before COM binding")
        try:
            sw = bind_pid(launched_pid)
        except SessionOwnershipUnproven as exc:
            if "unavailable" not in str(exc):
                fail(str(exc))
            pause(0.25)
            continue
        except Exception as exc:
            fail(f"PID-qualified ROT binding failed: {exc}")
        timeline.append({"timestamp": clock(), "state": "COM_BOUND",
                         "candidate_pid": launched_pid})
        try:
            com_pid = int(member(sw, "GetProcessID"))
        except Exception as exc:
            fail(f"COM process ID unavailable: {exc}")
        observed = snapshot()
        new_observed = [item for item in observed if item["pid"] not in pre_pids]
        if len(new_observed) != 1:
            fail("candidate disappeared or another new PID appeared after COM binding")
        proof = prove_r003_new_instance(before, after, launch_started_at=launched_at,
                                        com_pid=com_pid, observed_after_bind=new_observed[0])
        evidence.update(proof)
        evidence.update({"launch_finished_at": clock(), "connection_timestamp": clock(),
                         "launcher_pid": launched_pid, "process_identity_timeline": timeline})
        if not proof["ownership_proven"]:
            fail(proof["ownership_failure_reason"] or "new process ownership unproven")
        timeline.append({"timestamp": clock(), "state": "OWNED", "candidate_pid": launched_pid,
                         "creation_time": created, "executable_path": path,
                         "metadata_complete": True, "com_pid": com_pid})
        identity = OwnedProcessIdentity(launched_pid, str(created),
                                        "OS_SNAPSHOT_AND_PID_ROT", run_id, "OWNED_NEW_INSTANCE")
        return sw, identity, evidence
    fail("PID-qualified ROT binding timed out")


class R003OwnedNativeSession:
    """R003-only façade over reviewed external document helpers and owned COM."""

    def __init__(self, sw: Any, connect_api: Any):
        self.sw = sw
        self._connect_api = connect_api
        self.connection_info = {"binding": "PID_QUALIFIED_ROT"}

    def new_part(self, template_path=None):
        return self._connect_api.new_document(self.sw, doc_type="part",
                                               template_path=template_path)

    def new_drawing(self, template_path=None):
        return self._connect_api.new_document(self.sw, doc_type="drawing",
                                               template_path=template_path)

    def open(self, file_path, *, read_only=False, silent=False):
        return self._connect_api.open_document(self.sw, str(file_path),
                                                read_only=read_only, silent=silent)

    def save(self, model, path):
        return self._connect_api.save_document(model, file_path=str(path))

    def _open_titles(self) -> list[str]:
        # GetDocuments is not exposed by some 2024 out-of-process dispatches;
        # the documented GetFirstDocument/GetNext chain remains available.
        count = int(member(self.sw, "GetDocumentCount"))
        first = getattr(self.sw, "GetFirstDocument", None)
        document = first() if callable(first) and not hasattr(first, "_oleobj_") else first
        titles = []
        while document is not None:
            titles.append(str(member(document, "GetTitle")))
            next_document = getattr(document, "GetNext", None)
            document = (next_document() if callable(next_document)
                        and not hasattr(next_document, "_oleobj_") else next_document)
            if len(titles) > count:
                raise SessionOwnershipUnproven("document enumeration exceeds native count")
        if len(titles) != count:
            raise SessionOwnershipUnproven("native document count/enumeration mismatch")
        return titles

    def close(self, *, title):
        if title not in self._open_titles():
            return False
        member(self.sw, "CloseDoc", title)
        return title not in self._open_titles()

    def r003_quit_proven_owned(self, identity: OwnedProcessIdentity):
        if (identity.ownership_status != "OWNED_NEW_INSTANCE"
                or int(member(self.sw, "GetProcessID")) != identity.pid
                or self._open_titles()):
            return False
        member(self.sw, "ExitApp")
        return True  # supervisor still must observe exact PID disappearance


def _load_reviewed_skill_modules(scripts_root: Path) -> tuple[Any, Any]:
    root = Path(scripts_root).resolve(strict=True)
    if (root.name != "scripts" or not (root / "sw_connect.py").is_file()
            or not (root / "sw_part.py").is_file()):
        raise SessionOwnershipUnproven("reviewed external SolidWorks skill scripts missing")
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    connect_api = importlib.import_module("sw_connect")
    part_api = importlib.import_module("sw_part")
    if any(Path(module.__file__).resolve().parent != root
           for module in (connect_api, part_api)):
        raise SessionOwnershipUnproven("external skill import resolved to unexpected location")
    return connect_api, part_api


def create_r003_owned_adapter(*, executable_path: Path, scripts_root: Path,
                              run_id: str, expected_major_version: int,
                              launch_timeout_s: float,
                              exit_timeout_s: float) -> tuple["R003SolidWorksSessionAdapter", dict]:
    """Production entry. Importing this module never launches or binds COM."""
    connect_api, part_api = _load_reviewed_skill_modules(scripts_root)
    sw, identity, ownership = launch_r003_owned_com(
        executable_path, run_id=run_id, launch_timeout_s=launch_timeout_s)

    def probe(pid: int) -> dict:
        matches = [row for row in _snapshot_solidworks_processes() if row["pid"] == pid]
        if len(matches) != 1:
            return {}
        return {"pid": pid, "create_time": str(matches[0]["creation_time"]),
                "source": "OS_SNAPSHOT_AND_PID_ROT"}

    adapter = R003SolidWorksSessionAdapter.from_factory(
        lambda: R003OwnedNativeSession(sw, connect_api), identity=identity,
        identity_probe=probe, run_id=run_id,
        expected_major_version=expected_major_version, part_api=part_api,
        exit_timeout_s=exit_timeout_s)
    adapter.ownership_evidence = ownership
    return adapter, ownership


def create_r003_physical_views(drawing: Any, part: Any, part_path: str, *,
                               empty_variant_factory: Callable[[], Any]) -> dict:
    """Initialize native view cache, remove every temporary view, then create
    explicitly named FRONT/TOP/LEFT native views. No RIGHT relabeling.

    Mirrors the proven STEP API sequence, not its runner/process orchestration.
    The caller must save, reopen, and independently measure frames afterward.
    """
    from experiments.three_view_reconstruction.drawing.view_orientation import canonicalize
    import pythoncom
    import re

    def identity(obj: Any) -> str:
        raw = repr(obj._oleobj_.QueryInterface(pythoncom.IID_IUnknown))
        match = re.search(r"with obj at (0x[0-9A-Fa-f]+)", raw)
        if match is None:
            raise NativeEvidenceUnavailable("native drawing view COM identity unavailable")
        return match.group(1).casefold()

    if not bool(member(drawing, "Create3rdAngleViews2", str(part_path))):
        raise NativeEvidenceUnavailable("native model-view cache initialization failed")
    sheet = member(drawing, "GetFirstView")
    current = getattr(sheet, "GetNextView", None)
    current = current() if callable(current) and not hasattr(current, "_oleobj_") else current
    temporary_views = []
    while current is not None:
        temporary_views.append(current)
        following = getattr(current, "GetNextView", None)
        current = following() if callable(following) and not hasattr(following, "_oleobj_") else following
    temporary_names = [str(member(view, "Name")) for view in temporary_views]
    if len(temporary_names) != 3 or len(set(temporary_names)) != 3:
        raise NativeEvidenceUnavailable("native cache did not create three unique temporary views")
    model_names = list(member(part, "GetModelViewNames") or [])
    names_by_role: dict[str, list[str]] = {"FRONT": [], "TOP": [], "LEFT": []}
    for name in model_names:
        try:
            role = canonicalize(str(name)).canonical_role.value
        except (TypeError, ValueError):
            continue
        if role in names_by_role:
            names_by_role[role].append(str(name))
    if any(len(names) != 1 for names in names_by_role.values()):
        raise NativeEvidenceUnavailable("unique native FRONT/TOP/LEFT model views unavailable")
    for view, name in zip(temporary_views, temporary_names):
        target_identity = identity(view)
        member(drawing, "ClearSelection2", True)
        feature = member(drawing, "FeatureByName", name)
        if feature is None or identity(member(feature, "GetSpecificFeature")) != target_identity:
            raise NativeEvidenceUnavailable("temporary view feature identity mismatch")
        if not bool(member(feature, "Select2", False, 0)):
            raise NativeEvidenceUnavailable("temporary native view selection failed")
        manager = member(drawing, "SelectionManager")
        if int(member(manager, "GetSelectedObjectCount2", -1)) != 1:
            raise NativeEvidenceUnavailable("temporary view selection count mismatch")
        selected = member(manager, "GetSelectedObject6", 1, -1)
        selected_identity = identity(selected)
        if selected_identity != target_identity:
            specific = getattr(selected, "GetSpecificFeature", None)
            selected_view = (specific() if callable(specific) and not hasattr(specific, "_oleobj_")
                             else specific)
            if selected_view is None or identity(selected_view) != target_identity:
                raise NativeEvidenceUnavailable("selected temporary view identity mismatch")
        member(drawing, "EditDelete")
        remaining = []
        root = member(drawing, "GetFirstView")
        following = getattr(root, "GetNextView", None)
        current = following() if callable(following) and not hasattr(following, "_oleobj_") else following
        while current is not None:
            remaining.append(identity(current))
            following = getattr(current, "GetNextView", None)
            current = following() if callable(following) and not hasattr(following, "_oleobj_") else following
        if target_identity in remaining or len(remaining) != len(temporary_names) - temporary_names.index(name) - 1:
            raise NativeEvidenceUnavailable("exact temporary view deletion unproven")
    placement = {"FRONT": (0.145, 0.12), "TOP": (0.145, 0.22),
                 "LEFT": (0.045, 0.12)}
    created = {}
    for role in ("FRONT", "TOP", "LEFT"):
        x, y = placement[role]
        view = member(drawing, "CreateDrawViewFromModelView3", str(part_path),
                      names_by_role[role][0], x, y, 0.0)
        if view is None:
            raise NativeEvidenceUnavailable(f"native {role} drawing view creation failed")
        member(view, "Position")  # assert native view exposes placement
        created[role] = {"native_model_view_name": names_by_role[role][0],
                         "native_drawing_view_name": str(member(view, "Name")),
                         "position_m": [x, y], "native_view": view}
    member(drawing, "ForceRebuild3", False)
    return {"views": created, "temporary_view_names_deleted": temporary_names,
            "source": "IDrawingDoc.CreateDrawViewFromModelView3 native FRONT/TOP/LEFT"}


class R003SolidWorksSessionAdapter:
    """Normalize only lifecycle/ownership of a proven-owned injected session.

    The injected `part_api` is the existing external sw_part module (or a fake
    with exactly those functions). Native support faces are selected by exact
    feature owner and measured plane; names are never acceptance authority.
    """

    def __init__(self, *, session_factory: Callable[[], Any],
                 identity: OwnedProcessIdentity,
                 identity_probe: Callable[[int], Mapping[str, Any]],
                 run_id: str, expected_major_version: int,
                 part_api: Any = None,
                 warning_slot_factory: Callable[[], Any] | None = None,
                 reference_resolver: Callable[[Any, bytes], tuple[Any, int]] | None = None,
                 empty_variant_factory: Callable[[], Any] | None = None,
                 exit_timeout_s: float = 0):
        self._factory = session_factory
        self._identity = identity
        self._probe = identity_probe
        self._run_id = run_id
        self._expected_major = expected_major_version
        self._part_api = part_api
        self._warning_slot_factory = warning_slot_factory
        self._reference_resolver = reference_resolver
        self._empty_variant_factory = empty_variant_factory or _empty_variant
        self._exit_timeout_s = exit_timeout_s
        self._session = None
        self._part = None
        self._owned_titles: set[str] = set()
        self._features: dict[str, Any] = {}
        self._feature_refs: dict[str, str] = {}
        self._sketch_refs: dict[str, str] = {}
        self._support_refs: dict[str, dict] = {}
        self._support_geometry: dict[str, dict] = {}
        self._support_faces: dict[str, Any] = {}
        self.connection_info: dict[str, Any] | None = None
        self.sw_major_version: int | None = None

    @classmethod
    def from_factory(cls, factory: Callable[[], Any], *,
                     identity: OwnedProcessIdentity,
                     identity_probe: Callable[[int], Mapping[str, Any]],
                     run_id: str, expected_major_version: int,
                     part_api: Any = None,
                     warning_slot_factory: Callable[[], Any] | None = None,
                     reference_resolver: Callable[[Any, bytes], tuple[Any, int]] | None = None,
                     empty_variant_factory: Callable[[], Any] | None = None,
                     exit_timeout_s: float = 0):
        return cls(session_factory=factory, identity=identity,
                   identity_probe=identity_probe, run_id=run_id,
                   expected_major_version=expected_major_version,
                   part_api=part_api, warning_slot_factory=warning_slot_factory,
                   reference_resolver=reference_resolver,
                   empty_variant_factory=empty_variant_factory,
                   exit_timeout_s=exit_timeout_s)

    def connect(self) -> None:
        if self._session is not None:
            return
        if not verify_owned_identity(self._identity, self._probe(self._identity.pid), self._run_id):
            raise SessionOwnershipUnproven("owned process identity not proven before session construction")
        session = self._factory()
        info = getattr(session, "connection_info", None)
        if not isinstance(info, Mapping):
            raise SessionOwnershipUnproven("underlying connection metadata unavailable")
        # An external, run-owned launcher may create SW first. The upstream
        # GetActiveObject then attaches to *that owned PID* and reports False.
        # The independent creation-time proof and native PID equality below,
        # never the upstream `Dispatch` flag, establish ownership.
        if not verify_owned_identity(self._identity, self._probe(self._identity.pid), self._run_id):
            raise SessionOwnershipUnproven("process identity changed during session construction")
        try:
            native_pid = int(member(session.sw, "GetProcessID"))
            revision = str(member(session.sw, "RevisionNumber"))
            # sw_connect.get_sw_version: revision major 32 -> year 2024.
            major = int(revision.split(".")[0]) - 8 + 2000
        except Exception as exc:
            raise SessionOwnershipUnproven("native PID/version unavailable") from exc
        if native_pid != self._identity.pid or major != self._expected_major:
            raise SessionOwnershipUnproven("native PID/version mismatch")
        self._session = session
        self.sw_major_version = major
        self.connection_info = {
            "started_by_cad_studio": True,
            "attached_to_existing_process": False,
            "process_id": native_pid,
            "process_create_time": self._identity.create_time,
            "ownership_proven": True,
            "identity_source": self._identity.source,
            "run_id": self._run_id,
        }

    def new_part(self, template_path: str | None = None) -> Any:
        self.connect()
        self._part = self._session.new_part(template_path=template_path)
        if self._part is None:
            raise NativeEvidenceUnavailable("new part returned no document")
        title = str(member(self._part, "GetTitle"))
        self._owned_titles.add(title)
        return self._part

    def save_part(self, path: Path) -> bool:
        if self._part is None:
            raise NativeEvidenceUnavailable("no run-owned part")
        old_title = str(member(self._part, "GetTitle"))
        if old_title not in self._owned_titles:
            raise SessionOwnershipUnproven("save forbidden for non-owned document")
        result = self._session.save(self._part, str(path))
        if result is not True or not Path(path).is_file() or Path(path).stat().st_size <= 0:
            return False
        if Path(str(member(self._part, "GetPathName"))).resolve() != Path(path).resolve():
            raise SessionOwnershipUnproven("saved document path does not match run artifact")
        new_title = str(member(self._part, "GetTitle"))
        self._owned_titles.remove(old_title)
        self._owned_titles.add(new_title)
        return True

    def close_part(self) -> bool:
        if self._part is None:
            return True
        title = str(member(self._part, "GetTitle"))
        if title not in self._owned_titles:
            raise SessionOwnershipUnproven("attempt to close non-owned document")
        result = self._session.close(title=title)
        if result is not True:
            return False
        self._owned_titles.remove(title)
        self._part = None
        return True

    def quit_owned_instance(self) -> dict:
        cleanup_started_at = time.time()
        if not self.connection_info or self.connection_info.get("ownership_proven") is not True:
            raise SessionOwnershipUnproven("quit forbidden without proven ownership")
        if self._owned_titles:
            raise SessionOwnershipUnproven("run-owned documents not closed")
        if not verify_owned_identity(self._identity, self._probe(self._identity.pid), self._run_id):
            raise SessionOwnershipUnproven("PID identity changed; do not quit")
        # The upstream method refuses to quit a session that attached to an
        # instance launched externally by this run. An explicit owned-quit
        # capability is required; never fall back to killing by image name.
        quit_owned = getattr(self._session, "r003_quit_proven_owned", None)
        if quit_owned is None:
            return {"pid": self._identity.pid, "process_exited": False,
                    "com_released": False, "reason": "owned quit capability unavailable"}
        requested = quit_owned(self._identity)
        del quit_owned  # release bound session/COM reference before OS exit polling
        if requested is not True:
            return {"pid": self._identity.pid, "process_exited": False,
                    "com_released": False, "exit_app_called": False,
                    "cleanup_started_at": cleanup_started_at,
                    "cleanup_finished_at": time.time(),
                    "reason": "quit not acknowledged"}
        # No synthetic exit confirmation. The supervisor must observe exact
        # identity disappearance and release its own COM references.
        self._part = None
        self._session = None
        self._factory = None
        self._features.clear()
        self._support_faces.clear()
        deadline = time.monotonic() + self._exit_timeout_s
        if self._identity.source == "OS_SNAPSHOT_AND_PID_ROT":
            present = _exact_pid_present(self._identity.pid)
            while present and time.monotonic() < deadline:
                time.sleep(0.25)
                present = _exact_pid_present(self._identity.pid)
            exited = not present
        else:  # injected unit-session probe, never production ownership proof
            observed = self._probe(self._identity.pid)
            while (verify_owned_identity(self._identity, observed, self._run_id)
                   and time.monotonic() < deadline):
                time.sleep(0.1)
                observed = self._probe(self._identity.pid)
            exited = not verify_owned_identity(self._identity, observed, self._run_id)
        return {"pid": self._identity.pid, "process_exited": exited,
                "com_released": exited, "exit_app_called": True,
                "cleanup_started_at": cleanup_started_at,
                "cleanup_finished_at": time.time(),
                "reason": "observed exact owned PID exit" if exited
                else "owned PID still alive"}

    def _api(self):
        if self._part_api is None or self._part is None:
            raise NativeEvidenceUnavailable("native sw_part binding or run-owned part unavailable")
        return self._part_api

    def _record_feature(self, operation: Any, feature: Any, *, allowed_types: set[str]) -> dict:
        if feature is None:
            raise NativeEvidenceUnavailable("native feature creation returned None")
        native_type = str(member(feature, "GetTypeName2"))
        # Instant3D can expose ICE through GetTypeName2; GetTypeName reveals
        # the underlying feature family and must still match this operation.
        underlying_type = str(member(feature, "GetTypeName")) if native_type == "ICE" else native_type
        if underlying_type not in allowed_types:
            raise NativeEvidenceUnavailable(
                f"unexpected native feature type: {native_type} (underlying: {underlying_type})")
        error = collect_feature_error(feature, warning_slot_factory=self._warning_slot_factory)
        if error["api_status"] != "PASS" or error["error_code"] != 0:
            raise NativeEvidenceUnavailable("native feature error code nonzero/unknown")
        if bool(member(feature, "IsSuppressed")):
            raise NativeEvidenceUnavailable("native feature suppressed")
        extension = member(self._part, "Extension")
        feature_ref = capture_persistent_reference(extension, feature, "FEATURE")["encoded"]
        _profile, sketch_ref = native_profile_feature(extension, feature)
        source_id = operation.source_feature_id
        self._features[source_id] = feature
        self._feature_refs[source_id] = feature_ref
        self._sketch_refs[source_id] = sketch_ref
        return {"operation_id": operation.operation_id, "source_feature_id": source_id,
                "persistent_reference": feature_ref, "sketch_reference": sketch_ref,
                "sketch_plane_reference": self._support_refs.get(source_id, {}).get("encoded"),
                "native_type": native_type, "underlying_native_type": underlying_type,
                "api_error_code": 0,
                "postcondition_status": "NATIVE_CREATED_NOT_ENGINEERING_VALIDATED"}

    def _unique_top_face(self, owner_id: str, z_mm: float) -> tuple[Any, dict]:
        extension = member(self._part, "Extension")
        matches = []
        observed_faces = []
        for body in member(self._part, "GetBodies2", 0, False) or []:
            for face in member(body, "GetFaces") or []:
                try:
                    evidence = collect_plane_face(face, extension, self._feature_refs)
                except NativeEvidenceUnavailable as exc:
                    observed_faces.append({"status": "UNAVAILABLE", "reason": str(exc)})
                    continue
                observed_faces.append({"status": "COLLECTED", "evidence": evidence})
                if (evidence["owner_source_feature_id"] == owner_id
                        and abs(evidence["offset_z_mm"] - z_mm) <= 0.05
                        and all(abs(a - b) <= 1e-6
                                for a, b in zip(evidence["normal"], (0.0, 0.0, 1.0)))):
                    matches.append((face, evidence))
        if len(matches) != 1:
            failure = NativeEvidenceUnavailable(
                f"unique native {owner_id} top face unavailable: {len(matches)}")
            failure.evidence = {"owner_id": owner_id, "target_z_mm": z_mm,
                                "observed_faces": observed_faces}
            raise failure
        return matches[0]

    def _select_face_sketch(self, face: Any) -> None:
        self._api()
        member(self._part, "ClearSelection2", True)
        if not bool(member(face, "Select2", False, 0)):
            raise NativeEvidenceUnavailable("exact native support face selection failed")
        member(member(self._part, "SketchManager"), "InsertSketch", True)

    def create_base(self, operation):
        api = self._api()
        if operation.type != "base_extrude":
            raise NativeEvidenceUnavailable("unsupported Base operation")
        with api.sketch(self._part, "Front Plane") as sketch_name:
            api.sketch_rectangle(self._part, 0, 0,
                                 operation.profile["width_mm"] / 1000.0,
                                 operation.profile["height_mm"] / 1000.0)
        feature = api.extrude_boss(self._part, sketch_name,
                                   operation.depth_mm / 1000.0,
                                   direction=True, merge=True)
        result = self._record_feature(operation, feature,
                                      allowed_types={"Extrusion", "Boss", "BossExtrude"})
        extension = member(self._part, "Extension")
        profile, _ = native_profile_feature(extension, feature)
        planes = [parent for parent in member(profile, "GetParents") or []
                  if str(member(parent, "GetTypeName2")) == "RefPlane"]
        if len(planes) != 1:
            raise NativeEvidenceUnavailable("Base sketch has no unique native reference plane parent")
        self._support_refs[operation.source_feature_id] = capture_persistent_reference(
            extension, planes[0], "PLANE")
        self._support_geometry[operation.source_feature_id] = collect_reference_plane(
            planes[0], extension, name="native-base-sketch-plane")
        self._support_faces["front_plane"] = planes[0]
        result["sketch_plane_reference"] = self._support_refs[operation.source_feature_id]["encoded"]
        return result

    def base_plane_evidence(self, operation, native):
        self._api()
        front = self._support_faces.get("front_plane")
        if front is None:
            raise NativeEvidenceUnavailable("native Front sketch plane not captured")
        extension = member(self._part, "Extension")
        plane = collect_reference_plane(front, extension, name="Front Plane")
        face, top = self._unique_top_face(operation.source_feature_id, 0.0)
        self._support_faces["base_top"] = face
        return plane, top

    def create_boss(self, operation):
        api = self._api()
        if operation.type != "boss_extrude" or "base_top" not in self._support_faces:
            raise NativeEvidenceUnavailable("unsupported Boss or Base support unavailable")
        face = self._support_faces["base_top"]
        extension = member(self._part, "Extension")
        self._support_refs[operation.source_feature_id] = capture_persistent_reference(
            extension, face, "FACE")
        self._support_geometry[operation.source_feature_id] = collect_plane_face(
            face, extension, self._feature_refs)
        self._select_face_sketch(face)
        api.sketch_rectangle(self._part, 0, 0,
                             operation.profile["width_mm"] / 1000.0,
                             operation.profile["height_mm"] / 1000.0)
        sketch_name = api.current_sketch_name(self._part)
        api.end_sketch(self._part)
        feature = api.extrude_boss(self._part, sketch_name,
                                   operation.depth_mm / 1000.0,
                                   direction=False, merge=True)
        result = self._record_feature(operation, feature,
                                      allowed_types={"Extrusion", "Boss", "BossExtrude"})
        base_id = next(iter(self._feature_refs))
        collect_sketch_support(self._part, feature,
            expected_sketch_ref=result["sketch_reference"],
            expected_parent_ref=self._feature_refs[base_id],
            selected_support=self._support_refs[operation.source_feature_id],
            support_geometry=self._support_geometry[operation.source_feature_id],
            owner_refs=self._feature_refs,
            resolver=self._reference_resolver)
        return result

    def boss_plane_evidence(self, operation, native):
        self._api()
        face, top = self._unique_top_face(operation.source_feature_id,
                                          float(operation.depth_mm))
        self._support_faces["boss_top"] = face
        # This is an actual native planar face, not an invented RefPlane.
        plane = {**top, "name": "Plane_Boss_Top", "support_kind": "FACE"}
        return plane, top

    def create_hole(self, operation):
        api = self._api()
        if (operation.type != "cut_extrude_through_circle"
                or operation.direction != "through_all"
                or "boss_top" not in self._support_faces):
            raise NativeEvidenceUnavailable("unsupported Hole or Boss support unavailable")
        face = self._support_faces["boss_top"]
        extension = member(self._part, "Extension")
        self._support_refs[operation.source_feature_id] = capture_persistent_reference(
            extension, face, "FACE")
        self._support_geometry[operation.source_feature_id] = collect_plane_face(
            face, extension, self._feature_refs)
        self._select_face_sketch(face)
        api.sketch_circle(self._part,
                          operation.profile["center_x_mm"] / 1000.0,
                          operation.profile["center_y_mm"] / 1000.0,
                          operation.profile["diameter_mm"] / 2000.0)
        sketch_name = api.current_sketch_name(self._part)
        api.end_sketch(self._part)
        feature = api.extrude_cut(self._part, sketch_name, 0,
                                  direction=True)
        result = self._record_feature(operation, feature,
                                      allowed_types={"Cut", "CutExtrude"})
        boss_id = list(self._feature_refs)[-2]
        collect_sketch_support(self._part, feature,
            expected_sketch_ref=result["sketch_reference"],
            expected_parent_ref=self._feature_refs[boss_id],
            selected_support=self._support_refs[operation.source_feature_id],
            support_geometry=self._support_geometry[operation.source_feature_id],
            owner_refs=self._feature_refs,
            resolver=self._reference_resolver)
        result["through_all_requested"] = True
        return result

    def initial_rebuild(self, plan, operations):
        self._api()
        ids = [operation.source_feature_id for operation in plan.operations]
        if set(ids) != set(self._features):
            raise NativeEvidenceUnavailable("created feature set incomplete")

        def lineage(source_id, feature):
            index = ids.index(source_id)
            parent_ref = (self._support_refs[source_id]["encoded"] if index == 0
                          else self._feature_refs[ids[index - 1]])
            return collect_sketch_support(self._part, feature,
                expected_sketch_ref=self._sketch_refs[source_id],
                expected_parent_ref=parent_ref,
                selected_support=self._support_refs[source_id],
                support_geometry=self._support_geometry[source_id],
                owner_refs=self._feature_refs,
                resolver=self._reference_resolver)

        report = collect_rebuild_evidence(
            self._part, self._features, self._feature_refs,
            warning_slot_factory=self._warning_slot_factory,
            resolve_sketch=lambda source_id, feature: bool(lineage(source_id, feature)["sketch_persistent_reference"]),
            resolve_plane=lambda source_id, feature: bool(lineage(source_id, feature)["selected_support"]),
            resolve_dependencies=lambda source_id, feature: lineage(source_id, feature)["status"] == "PASS")
        report["native_ledger"] = {
            "feature_refs": dict(self._feature_refs),
            "sketch_refs": dict(self._sketch_refs),
            "support_refs": dict(self._support_refs),
            "support_geometry": dict(self._support_geometry),
            "ordered_source_ids": ids,
        }
        return report

    def reopen_native_part_and_collect(self, part_path: Path, ledger: Mapping[str, Any],
                                       *, through_all_created: bool) -> dict:
        """Process B only: fresh disk-opened document, never Process A objects."""
        self.connect()
        if self._part is not None or not Path(part_path).is_file():
            raise NativeEvidenceUnavailable("Process B needs a fresh saved part")
        model = self._session.open(str(part_path), read_only=True, silent=True)
        if model is None:
            raise NativeEvidenceUnavailable("native part reopen returned no document")
        native_path = str(member(model, "GetPathName"))
        if Path(native_path).resolve() != Path(part_path).resolve():
            raise NativeEvidenceUnavailable("reopened part path differs from sealed part")
        self._part = model
        self._owned_titles.add(str(member(model, "GetTitle")))
        refs = ledger["feature_refs"]
        ids = ledger["ordered_source_ids"]
        reopened = collect_reopened_r003_dependencies(
            model, refs, ledger["sketch_refs"], ledger["support_refs"],
            ledger["support_geometry"], ids,
            resolver=self._reference_resolver or _native_reference_resolver)
        lineage = reopened["lineage"]
        rebuild = collect_rebuild_evidence(
            model, reopened["features"], refs,
            warning_slot_factory=self._warning_slot_factory or _native_warning_slot,
            resolve_sketch=lambda source_id, feature: lineage[source_id]["status"] == "PASS",
            resolve_plane=lambda source_id, feature: lineage[source_id]["selected_support"]["resolved"],
            resolve_dependencies=lambda source_id, feature: bool(
                lineage[source_id]["parent_feature_references"]))
        brep = collect_r003_brep(model, refs, base_id=ids[0], boss_id=ids[1],
                                 hole_id=ids[2], through_all_created=through_all_created)
        ownership = collect_bounded_r003_ownership(
            model, refs, base_id=ids[0], boss_id=ids[1], hole_id=ids[2],
            interface_z_mm=brep["interface"]["z_mm"])
        return {"reopen_status": "PASS", "reopened_exact_path": True,
                "native_path": native_path, "dependency": lineage,
                "reopened_rebuild": rebuild, "body": collect_body_count(model),
                "brep": brep, "ownership": ownership,
                "source": "fresh disk-opened IModelDoc2 native collection"}

    def reopen_native_drawing_and_collect(self, drawing_path: Path, part_path: Path, *,
                                          reviewed_projection_graph_digest: str) -> dict:
        """Reopen saved drawing and extract same-frame native HLR/HLV views."""
        self.connect()
        if not Path(drawing_path).is_file() or not Path(part_path).is_file():
            raise NativeEvidenceUnavailable("saved part/drawing files required")
        drawing = self._session.open(str(drawing_path), read_only=True, silent=True)
        if drawing is None or Path(str(member(drawing, "GetPathName"))).resolve() != Path(drawing_path).resolve():
            raise NativeEvidenceUnavailable("native drawing reopen returned no document")
        title = str(member(drawing, "GetTitle"))
        self._owned_titles.add(title)
        sheet = member(drawing, "GetCurrentSheet")
        sheet_properties = list(member(sheet, "GetProperties2") or [])
        if len(sheet_properties) < 5 or float(sheet_properties[4]) != 0.0:
            raise NativeEvidenceUnavailable("reopened sheet is not native third-angle")
        sheet_view = member(drawing, "GetFirstView")
        current = getattr(sheet_view, "GetNextView", None)
        current = current() if callable(current) and not hasattr(current, "_oleobj_") else current
        hlr, hlv, frames = {}, {}, {}
        import pythoncom
        import re

        def identity_token(obj):
            raw = repr(obj._oleobj_.QueryInterface(pythoncom.IID_IUnknown))
            match = re.search(r"with obj at (0x[0-9A-Fa-f]+)", raw)
            if match is None:
                raise NativeEvidenceUnavailable("reopened view COM identity unavailable")
            return match.group(1).casefold()

        while current is not None:
            orientation_identity = str(member(current, "GetOrientationName"))
            from experiments.three_view_reconstruction.drawing.view_orientation import canonicalize
            try:
                role = canonicalize(orientation_identity).canonical_role.value
            except (TypeError, ValueError) as exc:
                raise NativeEvidenceUnavailable("reopened drawing native orientation unknown") from exc
            if role not in {"FRONT", "TOP", "LEFT"} or role in frames:
                raise NativeEvidenceUnavailable("unexpected/duplicate physical drawing view")
            view_name = str(member(current, "Name"))
            feature = member(drawing, "FeatureByName", view_name)
            view_identity = identity_token(current)
            if feature is None or identity_token(member(feature, "GetSpecificFeature")) != view_identity:
                raise NativeEvidenceUnavailable("reopened view-to-feature identity mismatch")
            referenced = str(member(current, "GetReferencedModelName"))
            referenced_document = member(current, "ReferencedDocument")
            native_path = str(member(referenced_document, "GetPathName"))
            if (Path(referenced).name.casefold() != Path(part_path).name.casefold()
                    or Path(native_path).resolve() != Path(part_path).resolve()):
                raise NativeEvidenceUnavailable("reopened view model reference mismatch")
            frame = normalize_native_view_transform(
                current, expected_role=role,
                model_reference=str(part_path), projection_standard="THIRD_ANGLE")
            frame["native_view_identity"] = view_identity
            frame["feature_specific_view_identity"] = identity_token(member(feature, "GetSpecificFeature"))
            frame["referenced_model_path"] = native_path
            hidden, all_edges = collect_r003_native_view_modes(
                drawing, current, role=role, model_path=str(part_path))
            hlr[role], hlv[role], frames[role] = hidden, all_edges, frame
            following = getattr(current, "GetNextView", None)
            current = following() if callable(following) and not hasattr(following, "_oleobj_") else following
        if set(frames) != {"FRONT", "TOP", "LEFT"}:
            raise NativeEvidenceUnavailable("reopened physical FRONT/TOP/LEFT set incomplete")
        primitives = normalize_r003_hlv_hlr(
            hlr, hlv, reviewed_projection_graph_digest=reviewed_projection_graph_digest)
        if self._session.close(title=title) is not True:
            raise NativeEvidenceUnavailable("reopened drawing close failed")
        self._owned_titles.remove(title)
        return {"status": "PASS", "native_views": frames,
                "hlr_views": hlr, "hlv_views": hlv,
                "generated_views": primitives,
                "source": "reopened IDrawingDoc physical HLR/HLV extraction"}

    def create_physical_left_drawing(self, part_path, drawing_path):
        self.connect()
        if not Path(part_path).is_file() or Path(drawing_path).exists():
            raise NativeEvidenceUnavailable("saved part missing or drawing target already exists")
        part = self._session.open(str(part_path), read_only=True, silent=True)
        if part is None:
            raise NativeEvidenceUnavailable("saved part did not reopen for drawing")
        part_title = str(member(part, "GetTitle"))
        self._owned_titles.add(part_title)
        drawing = None
        drawing_title = None
        try:
            drawing = self._session.new_drawing()
            if drawing is None:
                raise NativeEvidenceUnavailable("new drawing returned no document")
            drawing_title = str(member(drawing, "GetTitle"))
            self._owned_titles.add(drawing_title)
            views = create_r003_physical_views(drawing, part, str(part_path),
                                               empty_variant_factory=self._empty_variant_factory)
            saved = self._session.save(drawing, str(drawing_path)) is True
            if not saved or not Path(drawing_path).is_file() or Path(drawing_path).stat().st_size == 0:
                raise NativeEvidenceUnavailable("physical drawing save unproven")
            if Path(str(member(drawing, "GetPathName"))).resolve() != Path(drawing_path).resolve():
                raise SessionOwnershipUnproven("saved drawing path does not match run artifact")
            self._owned_titles.remove(drawing_title)
            drawing_title = str(member(drawing, "GetTitle"))
            self._owned_titles.add(drawing_title)
        finally:
            for document, title in ((drawing, drawing_title), (part, part_title)):
                if document is not None and title in self._owned_titles:
                    actual_title = str(member(document, "GetTitle"))
                    if actual_title == title and self._session.close(title=title) is True:
                        self._owned_titles.remove(title)
        if drawing_title in self._owned_titles or part_title in self._owned_titles:
            raise NativeEvidenceUnavailable("run-owned drawing/source-part close unproven")
        return {"status": "PASS", "drawing_closed": True,
                "owned_session_pid": self._identity.pid,
                "requested_views": ["*Front", "*Top", "*Left"],
                "temporary_views_deleted": views["temporary_view_names_deleted"],
                "source": "owned-session native drawing lifecycle"}
