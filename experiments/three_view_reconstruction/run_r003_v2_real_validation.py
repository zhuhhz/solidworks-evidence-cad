"""R003 process boundary and sealed-evidence orchestration.

This module is deliberately fake/adapter-only. It never imports a COM module,
SolidWorksSession, benchmark evaluator, or an experimental B010 runner.
Real workers remain unbound until native evidence and ownership are reviewed.
"""

from __future__ import annotations

import builtins
from contextlib import contextmanager
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
import traceback
from typing import Any, Callable, Mapping, Sequence

from experiments.three_view_reconstruction.schemas.modeling_plan import ModelingOperation, ModelingPlan
from solidworks_main_skill.backends.solidworks_automation.r003_execution_bridge import (
    R003PreflightError, R003ExecutionConfig, _no_truth, bounded_geometry_from_plan,
    canonical_digest, execute_r003_plan, _session_ownership,
)
from solidworks_main_skill.backends.solidworks_automation.r003_real_validation import (
    GATES, compose_six_gates, validate_evidence_bundle,
)
from solidworks_main_skill.backends.solidworks_automation.r003_native_adapter import (
    R003SolidWorksSessionAdapter,
)


_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}\Z")
_PHASES = ("preflight", "connection", "modeling", "drawing", "validation",
           "cleanup", "overall")
_WORKER_MODULE = "experiments.three_view_reconstruction.run_r003_v2_real_validation"
_STAGE5A32_SECTIONS = (
    "environment", "ownership", "process_a", "generation_read_audit",
    "save_close", "process_b", "dependency", "brep", "hole_full_span",
    "face_roles", "drawing_roundtrip", "cleanup", "verdict",
)
_REAL_OPERATIONS = (
    "process_launch", "rot_attach", "new_document", "feature_operation",
    "rebuild", "save", "close_document", "reopen_document",
    "drawing_create", "drawing_rebuild", "drawing_extract", "exit_app",
    "process_exit",
)


def r003_stage5a33_timeout_config() -> dict:
    """Local first-run engineering limits, not SolidWorks API guarantees."""
    limits = {
        "process_launch": (90, "unique new SLDWORKS.exe PID observed"),
        "rot_attach": (60, "PID-qualified ROT object and GetProcessID match"),
        "new_document": (30, "native ModelDoc2 returned"),
        "feature_operation": (30, "native feature returned without error"),
        "rebuild": (60, "native rebuild and feature-error checks complete"),
        "save": (60, "save API accepts and nonempty file exists"),
        "close_document": (30, "document absent from owned session"),
        "reopen_document": (60, "fresh ModelDoc2 opened from saved path"),
        "drawing_create": (60, "native drawing and physical LEFT view exist"),
        "drawing_rebuild": (60, "native drawing rebuild completes"),
        "drawing_extract": (60, "native HLV and HLR extraction completes"),
        "exit_app": (30, "ExitApp invoked on proven-owned session"),
        "process_exit": (60, "exact owned PID disappears"),
    }
    return {name: {"timeout_s": seconds, "poll_interval_s": 0.25,
                   "success_condition": condition,
                   "rationale": "conservative first real-run local engineering safety margin"}
            for name, (seconds, condition) in limits.items()}


def validate_r003_operation_timeouts(configuration: Mapping[str, Mapping[str, Any]]) -> dict:
    """Require an auditable source and a polling success condition per wait."""
    if not isinstance(configuration, Mapping) or set(configuration) != set(_REAL_OPERATIONS):
        raise R003PreflightError("thirteen explicit R003 operation timeouts required")
    for operation, row in configuration.items():
        if not isinstance(row, Mapping) or set(row) != {
                "timeout_s", "poll_interval_s", "success_condition", "rationale"}:
            raise R003PreflightError(f"{operation}: timeout rationale incomplete")
        limit, interval = row["timeout_s"], row["poll_interval_s"]
        if (type(limit) not in (int, float) or type(interval) not in (int, float)
                or not 0 < interval < limit < float("inf")
                or not isinstance(row["success_condition"], str)
                or not row["success_condition"].strip()
                or not isinstance(row["rationale"], str) or not row["rationale"].strip()):
            raise R003PreflightError(f"{operation}: invalid timeout/poll/rationale")
    return {"status": "PASS", "policy": "LOCAL_ENGINEERING_POLICY",
            "operations": dict(configuration)}


def seal_stage5a32_bundle(run_dir: Path, sections: Mapping[str, Mapping[str, Any]], *,
                          runner_args: Sequence[str], git_commit: str | None,
                          git_dirty: bool) -> Path:
    """Seal a bounded R003 run, including failures, with immutable hashes."""
    run_dir = Path(run_dir).resolve(strict=True)
    if set(sections) != set(_STAGE5A32_SECTIONS) or (run_dir / "manifest.json").exists():
        raise R003PreflightError("complete unsealed Stage 5A.3.2 sections required")
    rows = {}
    for name in _STAGE5A32_SECTIONS:
        section = sections[name]
        if (not isinstance(section, Mapping) or section.get("status") not in
                {"PASS", "FAIL", "ERROR", "INCOMPLETE", "NOT_RUN"}
                or not isinstance(section.get("reason"), str)):
            raise R003PreflightError(f"{name} missing status/reason")
        path = run_dir / (name + ".json")
        _write_exclusive(path, dict(section))
        rows[name] = {"path": str(path), "sha256": _sha(path),
                      "size": path.stat().st_size}
    manifest = {
        "stage": "R003_V2_STAGE5A32", "timestamp": time.time(),
        "git_commit": git_commit, "git_dirty": git_dirty,
        "python_version": sys.version, "os": os.name,
        "runner_args": list(runner_args), "output_root": str(run_dir.parent),
        "evidence_files": rows,
        "final_verdict": dict(sections["verdict"]),
    }
    path = run_dir / "manifest.json"
    _write_exclusive(path, manifest)
    return path


def verify_stage5a32_bundle(manifest_path: Path) -> dict:
    path = Path(manifest_path).resolve(strict=True)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if (manifest.get("stage") != "R003_V2_STAGE5A32"
            or set(manifest.get("evidence_files", {})) != set(_STAGE5A32_SECTIONS)):
        raise R003PreflightError("Stage 5A.3.2 manifest section set invalid")
    for name, row in manifest["evidence_files"].items():
        artifact = Path(row["path"])
        if (not _inside_run(artifact, path.parent)
                or artifact.name != name + ".json"
                or artifact.stat().st_size != row["size"]
                or _sha(artifact) != row["sha256"]):
            raise R003PreflightError("sealed Stage 5A.3.2 evidence changed")
    return manifest


def validate_real_output_root(root: Path, *, repository: Path) -> Path:
    """Create only the approved root, then reject aliases and reparse escapes."""
    raw = str(root)
    if raw.startswith(("\\\\", "//")) or re.match(r"^[A-Za-z]:(?![/\\])", raw):
        raise R003PreflightError("UNC or drive-relative output root unsupported")
    path = Path(root)
    repo = Path(repository).resolve(strict=True)
    approved = repo / "artifacts" / "r003_real"
    if not path.is_absolute() or path != approved:
        raise R003PreflightError("output root is not approved R003 artifact root")
    current = path.parent
    while True:
        if current.exists():
            info = os.lstat(current)
            attributes = getattr(info, "st_file_attributes", None)
            if stat.S_ISLNK(info.st_mode) or (attributes is not None and attributes & 0x400):
                raise R003PreflightError("output ancestor is symlink/junction/reparse point")
        if current == current.parent:
            break
        current = current.parent
    path.mkdir(parents=True, exist_ok=True)
    resolved = path.resolve(strict=True)
    if resolved != approved or not path.is_dir():
        raise R003PreflightError("output root escaped approved path")
    current = path
    while True:
        info = os.lstat(current)
        attributes = getattr(info, "st_file_attributes", None)
        if os.name == "nt" and attributes is None:
            raise R003PreflightError("Windows reparse inspection unavailable")
        if stat.S_ISLNK(info.st_mode) or (attributes is not None and attributes & 0x400):
            raise R003PreflightError("output ancestor is symlink/junction/reparse point")
        if current == current.parent:
            break
        current = current.parent
    probe = path / (".r003_write_probe_" + str(os.getpid()))
    try:
        with probe.open("xb") as stream:
            stream.write(b"R003\n")
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if probe.exists():
            probe.unlink()
    return resolved


class GenerationReadAudit:
    """Process-A Python file-read ledger; C/native I/O remains unsupported."""

    def __init__(self, *, allowed_roots: Sequence[Path], forbidden_roots: Sequence[Path],
                 phase: str = "generation"):
        self.allowed = tuple(Path(p).resolve(strict=True) for p in allowed_roots)
        self.forbidden = tuple(Path(p).resolve(strict=True) for p in forbidden_roots)
        self.reads: list[str] = []
        self.denied: list[str] = []
        self.truth_reads: list[str] = []
        self.is_active = False
        self.completed = False
        self.phase = phase
        self.events: list[dict] = []
        self._fd_paths: dict[int, tuple[Path, tuple[int, int]]] = {}

    def record(self, file: Any, mode: str) -> None:
        if not ("r" in mode or "+" in mode):
            return
        if type(file) is int and file in self._fd_paths:
            mapped, identity = self._fd_paths[file]
            observed = os.fstat(file)
            if (observed.st_dev, observed.st_ino) == identity:
                file = mapped
        if type(file) is int:
            try:
                is_pipe = stat.S_ISFIFO(os.fstat(file).st_mode)
            except OSError:
                is_pipe = False
            if is_pipe:
                self.events.append({"operation": "read", "source": "SUBPROCESS_PIPE",
                                    "category": "ALLOWED_READ", "timestamp": time.time(),
                                    "phase": self.phase, "allowed": True,
                                    "reason": "anonymous IPC pipe, not a filesystem truth read"})
                return
        if not isinstance(file, (str, bytes, os.PathLike)):
            self.denied.append("NON_PATH_READ")
            self.events.append({"operation": "read", "source": "NON_PATH_READ",
                                "category": "FORBIDDEN_GENERATION_READ",
                                "timestamp": time.time(), "phase": self.phase,
                                "allowed": False, "reason": "unattributable file descriptor",
                                "descriptor": file if type(file) is int else None,
                                "call_stack": [f"{row.filename}:{row.lineno}:{row.name}"
                                               for row in traceback.extract_stack(limit=8)]})
            raise R003PreflightError("unattributable generation file read")
        path = Path(os.fsdecode(file)).resolve(strict=True)
        text_parts = {part.casefold() for part in path.parts}
        forbidden_name = bool(text_parts & {
            "truth", "expected", "evaluator", "reference_answer", "datasets",
            "benchmark", "benchmarks", "expected_geometry", "expected_ownership",
        }) \
            or path.name.casefold() == "expected.json"
        allowed = any(path.is_relative_to(root) for root in self.allowed)
        forbidden_root = any(path.is_relative_to(root) for root in self.forbidden)
        denied = forbidden_name or forbidden_root or not allowed
        category = ("TRUTH_READ" if forbidden_name or forbidden_root else
                    "FORBIDDEN_GENERATION_READ" if not allowed else "ALLOWED_READ")
        self.events.append({"operation": "read", "source": str(path),
                            "category": category, "timestamp": time.time(),
                            "phase": self.phase, "allowed": not denied,
                            "reason": ("truth/reference/evaluator path" if category == "TRUTH_READ"
                                       else "outside approved roots" if denied else "approved root")})
        if denied:
            self.denied.append(str(path))
            if forbidden_name or forbidden_root:
                self.truth_reads.append(str(path))
            raise R003PreflightError("generation read outside approved non-truth roots")
        self.reads.append(str(path))

    def evidence(self) -> dict:
        return {"generation_read_ledger": list(self.reads),
                "events": list(self.events),
                "generation_truth_reads": len(self.truth_reads),
                "denylist_violations": list(self.denied),
                "audit_status": ("ACTIVE" if self.is_active else
                                 "COMPLETE" if self.completed else "INACTIVE"),
                "audit_scope": "APPLICATION_CONTROLLED_GENERATION_READS: Python builtins.open/io.open/os.open; SolidWorks internal I/O excluded"}

    def _register_opened_file(self, file: Any, opened: Any) -> Any:
        if isinstance(file, (str, bytes, os.PathLike)):
            try:
                fd = opened.fileno()
                observed = os.fstat(fd)
                self._fd_paths[fd] = (Path(os.fsdecode(file)).resolve(strict=True),
                                      (observed.st_dev, observed.st_ino))
            except (AttributeError, OSError, ValueError):
                pass
        return opened

    @contextmanager
    def active(self):
        if self.is_active:
            raise R003PreflightError("nested generation read audit unsupported")
        original_builtin, original_io, original_os = builtins.open, io.open, os.open

        def audited_open(file, mode="r", *args, **kwargs):
            self.record(file, mode)
            return self._register_opened_file(
                file, original_builtin(file, mode, *args, **kwargs))

        def audited_io_open(file, mode="r", *args, **kwargs):
            self.record(file, mode)
            return self._register_opened_file(
                file, original_io(file, mode, *args, **kwargs))

        def audited_os_open(file, flags, *args, **kwargs):
            if flags & (os.O_WRONLY | os.O_RDWR) != os.O_WRONLY:
                self.record(file, "r")
            fd = original_os(file, flags, *args, **kwargs)
            if isinstance(file, (str, bytes, os.PathLike)):
                stat_result = os.fstat(fd)
                self._fd_paths[fd] = (Path(os.fsdecode(file)).resolve(strict=True),
                                      (stat_result.st_dev, stat_result.st_ino))
            return fd

        self.is_active = True
        builtins.open, io.open, os.open = audited_open, audited_io_open, audited_os_open
        try:
            yield self
        finally:
            builtins.open, io.open, os.open = original_builtin, original_io, original_os
            self.is_active = False
            self.completed = True


def execute_stage5a32_process_a(*,
                                load_reviewed_inputs: Callable[[], tuple[ModelingPlan, R003ExecutionConfig]],
                                owned_session_factory: Callable[[R003ExecutionConfig], Any],
                                read_audit: GenerationReadAudit,
                                allow_unit_fake: bool = False,
                                operation_timeouts: Mapping[str, Mapping[str, Any]] | None = None) -> dict:
    """Process-A callable: all Python input reads occur under the audit.

    The factory must return a proven-owned native adapter; a fake adapter is
    useful only for unit testing this boundary and is never real evidence.
    """
    if not isinstance(read_audit, GenerationReadAudit) or read_audit.is_active:
        raise R003PreflightError("fresh generation read audit required")
    if not allow_unit_fake:
        validate_r003_operation_timeouts(operation_timeouts)
    def checked_factory(config):
        session = owned_session_factory(config)
        if not allow_unit_fake and (
                not isinstance(session, R003SolidWorksSessionAdapter)
                or getattr(session, "ownership_evidence", {}).get("ownership_proven") is not True):
            raise R003PreflightError("Process A requires proven native owned adapter")
        return session

    with read_audit.active():
        plan, config = load_reviewed_inputs()
        if not allow_unit_fake:
            validate_real_output_root(config.artifact_root,
                                      repository=Path(__file__).resolve().parents[2])
        report = execute_r003_plan(plan, config,
                                   session_factory=lambda: checked_factory(config),
                                   read_audit=read_audit)
    audit = read_audit.evidence()
    report["generation_read_audit"] = audit
    report["generation_truth_reads"] = audit["generation_truth_reads"]
    report["evidence_mode"] = "UNIT_FAKE" if allow_unit_fake else "REAL_NATIVE_CANDIDATE"
    if (audit["audit_status"] != "COMPLETE" or audit["denylist_violations"]
            or audit["generation_truth_reads"] != 0):
        report["primary_status"] = "ERROR"
        report["primary_reason"] = "GENERATION_READ_AUDIT_INCOMPLETE_OR_VIOLATED"
    return report


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"),
                       allow_nan=False) + "\n").encode("utf-8")


def _write_exclusive(path: Path, value: Any) -> None:
    with path.open("xb") as stream:
        stream.write(_json_bytes(value))


def _sha(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_digest(path: Path) -> str:
    return canonical_digest(json.loads(path.read_text(encoding="utf-8")))


def reserve_run_directory(artifact_root: Path, run_id: str) -> Path:
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise R003PreflightError("invalid run_id")
    root = Path(artifact_root).resolve(strict=True)
    _no_truth({"artifact_root_path": root})
    run_dir = root / run_id
    if run_dir.exists() or run_dir.is_symlink() or run_dir.parent.resolve(strict=True) != root:
        raise R003PreflightError("output collision or path escape")
    run_dir.mkdir(exist_ok=False)
    return run_dir


def _inside_run(path: Path, run_dir: Path) -> bool:
    try:
        return path.resolve(strict=True).is_relative_to(run_dir.resolve(strict=True))
    except (OSError, ValueError):
        return False


def seal_generation_manifest(run_dir: Path, evidence: Mapping[str, Any]) -> Path:
    """Seal closed artifacts; fake files use the same integrity contract."""
    required = ("run_id", "process_id", "generation_truth_reads", "source_digests",
                "reasoning_digest", "feature_graph_digest", "plan_digest",
                "execution_evidence_path", "part_path", "drawing_path",
                "reasoning_path", "feature_graph_path", "plan_path", "source_artifact_paths",
                "session_ownership", "cleanup_result", "primary_status", "primary_reason")
    if any(key not in evidence for key in required):
        raise R003PreflightError("generation manifest field missing")
    if evidence["generation_truth_reads"] != 0:
        raise R003PreflightError("generation truth reads not zero")
    _no_truth(evidence)
    if evidence["run_id"] != run_dir.name or type(evidence["process_id"]) is not int:
        raise R003PreflightError("manifest run/process identity mismatch")
    source_paths = evidence["source_artifact_paths"]
    if not isinstance(source_paths, Mapping) or set(source_paths) != set(evidence["source_digests"]):
        raise R003PreflightError("source artifact/digest mapping mismatch")
    files = {}
    for label, key in (("execution_evidence", "execution_evidence_path"),
                       ("part", "part_path"), ("drawing", "drawing_path"),
                       ("reasoning", "reasoning_path"),
                       ("feature_graph", "feature_graph_path"), ("plan", "plan_path")):
        path = Path(evidence[key])
        if not path.is_file() or not _inside_run(path, run_dir) or path.stat().st_size <= 0:
            raise R003PreflightError("missing, empty or out-of-run artifact")
        files[label] = {"path": str(path.resolve(strict=True)),
                        "sha256": _sha(path), "size": path.stat().st_size}
    for label, path_value in source_paths.items():
        path = Path(path_value)
        if not path.is_file() or not _inside_run(path, run_dir) or path.stat().st_size <= 0:
            raise R003PreflightError("source artifact missing or outside run")
        files["source:" + label] = {"path": str(path.resolve(strict=True)),
                                    "sha256": _sha(path), "size": path.stat().st_size}
        if files["source:" + label]["sha256"] != evidence["source_digests"][label]:
            raise R003PreflightError("reviewed source digest mismatch")
    for label, digest_key in (("reasoning", "reasoning_digest"),
                              ("feature_graph", "feature_graph_digest"),
                              ("plan", "plan_digest")):
        if _json_digest(Path(files[label]["path"])) != evidence[digest_key]:
            raise R003PreflightError("canonical provenance digest mismatch")
    manifest = {key: evidence[key] for key in required
                if not key.endswith("_path") and not key.endswith("_paths")}
    manifest["files"] = files
    manifest["evidence_mode"] = evidence.get("evidence_mode", "FAKE")
    if manifest["evidence_mode"] not in {"FAKE", "REAL_NATIVE"}:
        raise R003PreflightError("unsupported generation evidence mode")
    if manifest["evidence_mode"] == "REAL_NATIVE":
        audit = evidence.get("generation_read_audit")
        ownership = evidence.get("session_ownership", {})
        cleanup = evidence.get("cleanup_result", {})
        if (not isinstance(audit, Mapping) or audit.get("audit_status") != "COMPLETE"
                or audit.get("generation_truth_reads") != 0
                or audit.get("denylist_violations")
                or ownership.get("ownership_proven") is not True
                or not ownership.get("process_create_time")
                or cleanup.get("status") != "PASS"
                or cleanup.get("process_exit_observed") is not True
                or evidence.get("primary_status") != "PASS"):
            raise R003PreflightError("real generation ownership/audit/cleanup proof incomplete")
        manifest["generation_read_audit"] = dict(audit)
    path = run_dir / "manifest.json"
    _write_exclusive(path, manifest)
    return path


def verify_generation_manifest(manifest_path: Path) -> dict:
    path = Path(manifest_path).resolve(strict=True)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("run_id") != path.parent.name or manifest.get("generation_truth_reads") != 0:
        raise R003PreflightError("manifest identity or truth count mismatch")
    if manifest.get("evidence_mode") not in {"FAKE", "REAL_NATIVE"}:
        raise R003PreflightError("unsupported evidence mode")
    ownership = manifest.get("session_ownership")
    if (not isinstance(ownership, dict) or ownership.get("owned") is not True
            or ownership.get("attached") is not False
            or type(ownership.get("pid")) is not int):
        raise R003PreflightError("generation session ownership unproven")
    if manifest["evidence_mode"] == "REAL_NATIVE":
        audit = manifest.get("generation_read_audit")
        if (ownership.get("ownership_proven") is not True
                or not ownership.get("process_create_time")
                or not isinstance(audit, dict)
                or audit.get("audit_status") != "COMPLETE"
                or audit.get("generation_truth_reads") != 0
                or audit.get("denylist_violations")
                or manifest.get("cleanup_result", {}).get("process_exit_observed") is not True):
            raise R003PreflightError("real generation seal proof incomplete")
    for label in ("reasoning_digest", "feature_graph_digest", "plan_digest"):
        if not isinstance(manifest.get(label), str) or not re.fullmatch(
                r"[0-9a-f]{64}", manifest[label]):
            raise R003PreflightError("manifest provenance digest invalid")
    source_digests = manifest.get("source_digests")
    if not isinstance(source_digests, dict) or not source_digests or any(
            not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
            for digest in source_digests.values()):
        raise R003PreflightError("source digest invalid")
    _no_truth(manifest)
    expected_file_labels = {"execution_evidence", "part", "drawing", "reasoning",
                            "feature_graph", "plan"} | {
                                "source:" + label for label in source_digests}
    if set(manifest.get("files", {})) != expected_file_labels:
        raise R003PreflightError("sealed file set mismatch")
    for label in expected_file_labels:
        row = manifest.get("files", {}).get(label)
        if not isinstance(row, dict):
            raise R003PreflightError("manifest artifact row missing")
        artifact = Path(row["path"])
        if not _inside_run(artifact, path.parent) or artifact.stat().st_size != row["size"] \
                or _sha(artifact) != row["sha256"]:
            raise R003PreflightError("sealed artifact changed or escaped run")
    for label, digest_key in (("reasoning", "reasoning_digest"),
                              ("feature_graph", "feature_graph_digest"),
                              ("plan", "plan_digest")):
        if _json_digest(Path(manifest["files"][label]["path"])) != manifest[digest_key]:
            raise R003PreflightError("canonical provenance digest changed")
    for label, digest in source_digests.items():
        if manifest["files"]["source:" + label]["sha256"] != digest:
            raise R003PreflightError("reviewed source digest changed")
    return manifest


def execute_stage5a32_process_b(manifest_path: Path, *,
                                owned_session_factory: Callable[[dict], Any],
                                source_views: Mapping[str, Any],
                                allow_unit_fake: bool = False) -> dict:
    """Fresh-session native Process B; sealed Process A objects are never reused."""
    manifest = verify_generation_manifest(manifest_path)
    if manifest.get("evidence_mode") != "REAL_NATIVE":
        raise R003PreflightError("Process B requires sealed real-native Process A")
    projection_row = manifest["files"].get("source:projection")
    if projection_row is None:
        raise R003PreflightError("reviewed projection artifact not sealed")
    projection_digest = _json_digest(Path(projection_row["path"]))
    if source_views.get("reviewed_projection_graph_digest") != projection_digest:
        raise R003PreflightError("source view provenance differs from sealed ProjectionGraph")
    execution = json.loads(Path(manifest["files"]["execution_evidence"]["path"]).read_text(encoding="utf-8"))
    initial = execution.get("initial_rebuild")
    if not isinstance(initial, Mapping) or not isinstance(initial.get("native_ledger"), Mapping):
        raise R003PreflightError("sealed native generation ledger missing")
    ledger = initial["native_ledger"]
    plan_data = json.loads(Path(manifest["files"]["plan"]["path"]).read_text(encoding="utf-8"))
    plan = ModelingPlan([ModelingOperation(**row) for row in plan_data["operations"]])
    part_path = Path(manifest["files"]["part"]["path"])
    drawing_path = Path(manifest["files"]["drawing"]["path"])
    report = {"primary_status": "ERROR", "primary_reason": None,
              "part_collection": None, "drawing_collection": None,
              "gates": None, "composition": None, "cleanup": None}
    adapter = None
    try:
        adapter = owned_session_factory(manifest)
        if not allow_unit_fake and (
                not isinstance(adapter, R003SolidWorksSessionAdapter)
                or getattr(adapter, "ownership_evidence", {}).get("ownership_proven") is not True):
            raise R003PreflightError("Process B requires independently proven owned native adapter")
        validation_session = _session_ownership(adapter)
        # Isolation is the closed-then-reopened ModelDoc2 boundary. A second
        # owned PID is permitted, but not required by the R003 input contract.
        report["validation_session"] = validation_session
        operations = execution.get("operations")
        through_requested = (isinstance(operations, list) and len(operations) == 3
                             and operations[2].get("native", {}).get("through_all_requested") is True)
        native_part = adapter.reopen_native_part_and_collect(
            part_path, ledger, through_all_created=through_requested)
        report["part_collection"] = native_part
        if adapter.close_part() is not True:
            raise R003PreflightError("Process B reopened part did not close")
        native_drawing = adapter.reopen_native_drawing_and_collect(
            drawing_path, part_path, reviewed_projection_graph_digest=projection_digest)
        report["drawing_collection"] = native_drawing
        if _sha(part_path) != manifest["files"]["part"]["sha256"] \
                or _sha(drawing_path) != manifest["files"]["drawing"]["sha256"]:
            raise R003PreflightError("sealed native files changed during reopen collection")
        native = {
            "initial_rebuild": initial,
            "reopened_rebuild": native_part["reopened_rebuild"],
            "required_feature_ids": ledger["ordered_source_ids"],
            "expected_native_refs": ledger["feature_refs"],
            "body": native_part["body"], "brep": native_part["brep"],
            "plan_geometry": bounded_geometry_from_plan(plan),
            "ownership": native_part["ownership"],
            "save_reopen": {
                "save_success": execution.get("save_result") is True,
                "close_success": execution.get("close_result") is True,
                "reopen_success": native_part["reopen_status"] == "PASS",
                "file_size": manifest["files"]["part"]["size"],
                "sealed_sha256": manifest["files"]["part"]["sha256"],
                "reopened_exact_path": native_part["reopened_exact_path"],
                "sealed_hash_unchanged": True},
            "source_views": dict(source_views),
            "generated_views": native_drawing["generated_views"],
            "native_views": native_drawing["native_views"],
        }
        validated = validate_evidence_bundle(native)
        report.update({"gates": validated["gates"],
                       "composition": validated["composition"],
                       "primary_status": validated["composition"]["status"],
                       "primary_reason": validated["composition"]["reason"]})
    except Exception as exc:
        report["primary_reason"] = f"{type(exc).__name__}: {exc}"
    finally:
        if adapter is not None and report.get("validation_session", {}).get("owned") is True:
            try:
                adapter.close_part()
                cleanup = adapter.quit_owned_instance()
                report["cleanup"] = cleanup
                if (cleanup.get("process_exited") is not True
                        or cleanup.get("com_released") is not True):
                    report["primary_status"] = "ERROR"
                    report["primary_reason"] = "PROCESS_B_OWNED_PID_EXIT_UNPROVEN"
            except Exception as exc:
                report["cleanup"] = {"status": "ERROR", "reason": str(exc)}
                report["primary_status"] = "ERROR"
        elif adapter is not None:
            report["cleanup"] = {"status": "NOT_RUN",
                                 "reason": "unproven/borrowed session untouched"}
        verify_generation_manifest(manifest_path)
    return report


def _validate_timeouts(timeouts: Mapping[str, float]) -> None:
    if set(timeouts) != set(_PHASES) or any(
            type(value) not in (int, float) or value <= 0 or value == float("inf")
            or value != value for value in timeouts.values()):
        raise R003PreflightError("all seven explicit positive timeouts required")


def terminate_verified_owned_pid(pid: int, ledger: Mapping[str, Any], run_id: str,
                                 terminator: Callable[[int], Any],
                                 identity_verifier: Callable[[int, Mapping[str, Any]], Mapping[str, Any]]) -> bool:
    """Never kill a process without exact run/PID/ownership evidence."""
    if (type(pid) is not int or pid <= 0 or ledger.get("run_id") != run_id
            or ledger.get("session_owned_by_run") is not True
            or ledger.get("attached_to_existing_process") is not False
            or ledger.get("sw_pid") != pid
            or not isinstance(ledger.get("process_create_time"), str)
            or not ledger["process_create_time"]
            or not isinstance(ledger.get("identity_source"), str)
            or not ledger["identity_source"]):
        return False
    observed = identity_verifier(pid, ledger)
    if (not isinstance(observed, Mapping) or observed.get("pid") != pid
            or observed.get("process_create_time") != ledger["process_create_time"]
            or observed.get("identity_source") != ledger["identity_source"]):
        return False
    terminator(pid)
    return True


def _timeout_cleanup(run_dir: Path, worker: Mapping[str, Any], *,
                     pid_terminator: Callable[[int], Any] | None,
                     identity_verifier: Callable[[int, Mapping[str, Any]], Mapping[str, Any]] | None) -> dict:
    if worker.get("worker_exited") is not True:
        return {"status": "ERROR", "reason": "PYTHON_WORKER_EXIT_UNCONFIRMED"}
    ledger_path = run_dir / "owned_process.json"
    if not ledger_path.exists():
        # In this fake-only harness no SW PID is launched. A future native
        # worker must emit the ledger before connection can create a process.
        return {"status": "PASS", "reason": "FAKE_WORKER_ONLY"}
    try:
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        pid = ledger.get("sw_pid")
        if pid_terminator is None or identity_verifier is None or not terminate_verified_owned_pid(
                pid, ledger, run_dir.name, pid_terminator, identity_verifier):
            return {"status": "ERROR", "reason": "OWNED_SW_PID_UNVERIFIED_NOT_KILLED"}
        return {"status": "PASS", "reason": "EXACT_OWNED_PID_TERMINATED"}
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return {"status": "ERROR", "reason": f"OWNED_SW_CLEANUP_ERROR: {exc}"}


def _run_worker(argv: Sequence[str], timeout: float, *, runner: Callable | None = None,
                phase_file: Path | None = None,
                phase_timeouts: Mapping[str, float] | None = None) -> dict:
    if not isinstance(argv, (list, tuple)) or not argv \
            or not all(isinstance(value, str) and value for value in argv):
        raise R003PreflightError("worker argv must be an explicit nonempty list")
    _no_truth({"worker_argv_path": list(argv)})
    if runner is None:
        process = subprocess.Popen(list(argv), shell=False, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        started = time.monotonic()
        current_phase = None
        phase_started = started
        while process.poll() is None:
            now = time.monotonic()
            timeout_reason = "WORKER_TIMEOUT" if now - started >= timeout else None
            if phase_file is not None and phase_file.is_file():
                try:
                    state = json.loads(phase_file.read_text(encoding="utf-8"))
                    phase = state.get("phase")
                    if phase in _PHASES and phase != current_phase:
                        current_phase, phase_started = phase, now
                except (OSError, ValueError, TypeError):
                    pass  # malformed heartbeat never extends the overall bound
            if (timeout_reason is None and phase_timeouts is not None
                    and current_phase in phase_timeouts
                    and now - phase_started >= phase_timeouts[current_phase]):
                timeout_reason = current_phase.upper() + "_TIMEOUT"
            if timeout_reason:
                process.terminate()  # exact run-owned Python worker PID
                try:
                    process.communicate(timeout=min(phase_timeouts.get("cleanup", 1.0)
                                                    if phase_timeouts else 1.0, timeout))
                except subprocess.TimeoutExpired:
                    process.kill()  # exact already-created child, never by image name
                    process.communicate()
                return {"status": "ERROR", "reason": timeout_reason,
                        "worker_pid": process.pid, "worker_exited": process.poll() is not None}
            time.sleep(0.02)
        stdout, stderr = process.communicate()
        return {"status": "PASS" if process.returncode == 0 else "ERROR",
                "reason": "WORKER_EXIT", "exit_code": process.returncode,
                "stdout": stdout, "stderr": stderr,
                "worker_pid": process.pid, "worker_exited": True}
    try:
        completed = runner(list(argv), shell=False, timeout=timeout,
                           capture_output=True, text=True, check=False)
    except subprocess.TimeoutExpired as exc:
        return {"status": "ERROR", "reason": "WORKER_TIMEOUT", "diagnostic": str(exc),
                "worker_exited": True}
    return {"status": "PASS" if completed.returncode == 0 else "ERROR",
            "reason": "WORKER_EXIT", "exit_code": completed.returncode,
            "stdout": completed.stdout, "stderr": completed.stderr}


def _trusted_fake_command(argv: Sequence[str], verb: str, run_dir: Path) -> bool:
    if len(argv) != 6 or list(argv[:4]) != [sys.executable, "-m", _WORKER_MODULE, verb]:
        return False
    if verb == "generate-fake":
        return Path(argv[4]).resolve() == run_dir.resolve() \
            and _inside_run(Path(argv[5]), run_dir)
    return Path(argv[4]).resolve() == (run_dir / "manifest.json").resolve() \
        and _inside_run(Path(argv[5]), run_dir)


def orchestrate_fake_run(run_dir: Path, *, generation_argv: Sequence[str],
                         validation_argv: Sequence[str], timeouts: Mapping[str, float],
                         runner: Callable | None = None,
                         pid_terminator: Callable[[int], Any] | None = None,
                         identity_verifier: Callable[[int, Mapping[str, Any]], Mapping[str, Any]] | None = None,
                         optional_evaluator_argv: Sequence[str] | None = None) -> dict:
    """Separate workers and seal checks. Never upgrades fake PASS to real PASS."""
    _validate_timeouts(timeouts)
    run_dir = Path(run_dir)
    if (not _RUN_ID.fullmatch(run_dir.name) or run_dir.is_symlink()
            or not run_dir.parent.is_dir()
            or (run_dir.exists() and not run_dir.is_dir())):
        raise R003PreflightError("unsafe run target")
    # The production execution bridge, not the parent harness, atomically
    # creates a new directory. Fake fixtures may already reserve theirs.
    if runner is None and (not _trusted_fake_command(generation_argv, "generate-fake", run_dir)
                           or not _trusted_fake_command(validation_argv, "validate-fake", run_dir)):
        raise R003PreflightError("only reviewed fake worker commands are runnable before Stage 5A.2")
    if runner is None and optional_evaluator_argv is not None:
        raise R003PreflightError("benchmark evaluator is an interface placeholder before Stage 5A.2")
    result = {"generation": None, "validation": None, "evaluation": None,
              "simulated_composer_status": "NOT_RUN", "r003_engineering_pass": False,
              "real_solidworks_validation": "NOT_RUN", "primary_status": "ERROR",
              "primary_reason": None, "cleanup_status": "NOT_RUN", "cleanup_reason": None}
    started = time.monotonic()
    first = _run_worker(generation_argv, timeouts["overall"],
                        runner=runner, phase_file=Path(run_dir) / "generation_phase.json",
                        phase_timeouts=timeouts)
    result["generation"] = first
    if first["status"] != "PASS":
        result["primary_reason"] = "GENERATION_" + first["reason"]
        if first["reason"].endswith("TIMEOUT"):
            cleanup = _timeout_cleanup(Path(run_dir), first,
                                       pid_terminator=pid_terminator,
                                       identity_verifier=identity_verifier)
            result["cleanup_status"] = cleanup["status"]
            result["cleanup_reason"] = cleanup["reason"]
        return result
    try:
        manifest = verify_generation_manifest(Path(run_dir) / "manifest.json")
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        result["primary_reason"] = "GENERATION_SEAL_INVALID: " + str(exc)
        return result
    if manifest.get("cleanup_result", {}).get("status") != "PASS":
        result["primary_status"] = manifest.get("primary_status", "ERROR")
        result["primary_reason"] = manifest.get("primary_reason")
        result["cleanup_status"] = "ERROR"
        result["cleanup_reason"] = manifest.get("cleanup_result", {}).get("reason", "UNKNOWN")
        return result
    if manifest.get("primary_status") != "PASS":
        result["primary_status"] = manifest.get("primary_status", "ERROR")
        result["primary_reason"] = manifest.get("primary_reason")
        result["cleanup_status"] = "PASS"
        return result
    result["cleanup_status"] = "PASS"
    # The caller supplies an explicit *separate* Python interpreter command.
    # The worker itself must validate and write `validation_verdict.json`.
    remaining = max(0.001, timeouts["overall"] - (time.monotonic() - started))
    second = _run_worker(validation_argv, min(timeouts["validation"], remaining),
                         runner=runner, phase_file=Path(run_dir) / "validation_phase.json",
                         phase_timeouts=timeouts)
    result["validation"] = second
    if second["status"] != "PASS":
        result["primary_reason"] = "VALIDATION_" + second["reason"]
        return result
    try:
        verify_generation_manifest(Path(run_dir) / "manifest.json")
        verdict_path = Path(run_dir) / "validation_verdict.json"
        verdict = json.loads(verdict_path.read_text(encoding="utf-8"))
        if verdict.get("run_id") != manifest["run_id"] or verdict.get("process_id") == manifest["process_id"]:
            raise R003PreflightError("generation/validation process identity not separated")
        composed = compose_six_gates(verdict["gates"])
        result["simulated_composer_status"] = composed["status"]
        result["primary_status"] = composed["status"]
        result["primary_reason"] = composed["reason"]
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        result["primary_reason"] = "VALIDATION_SEAL_INVALID: " + str(exc)
        return result
    if optional_evaluator_argv is not None:
        # Separate process; its result cannot change the sealed engineering verdict.
        sealed_verdict_hash = _sha(verdict_path)
        result["evaluation"] = _run_worker(optional_evaluator_argv,
                                            min(timeouts["validation"], max(
                                                0.001, timeouts["overall"] -
                                                (time.monotonic() - started))), runner=runner)
        try:
            verify_generation_manifest(Path(run_dir) / "manifest.json")
            unchanged = _sha(verdict_path) == sealed_verdict_hash
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            unchanged = False
        if not unchanged:
            result["primary_status"] = "ERROR"
            result["primary_reason"] = "OPTIONAL_EVALUATOR_MUTATED_SEALED_EVIDENCE"
            result["simulated_composer_status"] = "ERROR"
    return result


def assert_generation_isolation(imported_modules: Sequence[str],
                                opened_paths: Sequence[str | Path]) -> None:
    forbidden_modules = ("evaluator", "run_b010_002", "truth")
    if any(any(token in name.casefold().split(".") for token in forbidden_modules)
           for name in imported_modules):
        raise R003PreflightError("generation imported evaluator/benchmark truth module")
    for path in opened_paths:
        _no_truth({"opened_file_path": Path(path)})


def generate_fake_worker(run_dir: Path, evidence_path: Path) -> Path:
    """Fake Process A: seal already-created fake files in a fresh interpreter."""
    if not _inside_run(Path(evidence_path), Path(run_dir)):
        raise R003PreflightError("generation evidence path outside run")
    assert_generation_isolation(tuple(sys.modules), [evidence_path])
    evidence = json.loads(Path(evidence_path).read_text(encoding="utf-8"))
    evidence["process_id"] = os.getpid()
    evidence["evidence_mode"] = "FAKE"
    return seal_generation_manifest(run_dir, evidence)


def validate_fake_worker(manifest_path: Path, gate_evidence_path: Path) -> Path:
    """Fake Process B entry: validates hashes before/after composing supplied gates."""
    manifest = verify_generation_manifest(manifest_path)
    path = Path(gate_evidence_path).resolve(strict=True)
    if not _inside_run(path, Path(manifest_path).parent):
        raise R003PreflightError("gate evidence outside run")
    supplied = json.loads(path.read_text(encoding="utf-8"))
    _no_truth(supplied)
    native = supplied.get("native_evidence")
    if not isinstance(native, dict) or not isinstance(native.get("save_reopen"), dict):
        raise R003PreflightError("native save/reopen evidence missing")
    if (native["save_reopen"].get("sealed_sha256")
            != manifest["files"]["part"]["sha256"]
            or native["save_reopen"].get("file_size")
            != manifest["files"]["part"]["size"]):
        raise R003PreflightError("native save evidence disagrees with sealed part")
    plan_data = json.loads(Path(manifest["files"]["plan"]["path"]).read_text(encoding="utf-8"))
    plan = ModelingPlan([ModelingOperation(**row) for row in plan_data["operations"]])
    if native.get("plan_geometry") != bounded_geometry_from_plan(plan):
        raise R003PreflightError("native comparison targets differ from sealed ModelingPlan")
    result = validate_evidence_bundle(native)
    gates, composed = result["gates"], result["composition"]
    verify_generation_manifest(manifest_path)
    output = Path(manifest_path).parent / "validation_verdict.json"
    _write_exclusive(output, {"run_id": manifest["run_id"], "process_id": os.getpid(),
                              "gates": gates, "simulated_composer_status": composed["status"],
                              "r003_engineering_pass": False,
                              "real_solidworks_validation": "NOT_RUN"})
    return output


def _main() -> int:
    if len(sys.argv) == 4 and sys.argv[1] == "generate-fake":
        generate_fake_worker(Path(sys.argv[2]), Path(sys.argv[3]))
        return 0
    if len(sys.argv) == 4 and sys.argv[1] == "validate-fake":
        validate_fake_worker(Path(sys.argv[2]), Path(sys.argv[3]))
        return 0
    print("Only validate-fake is available before Stage 5A.2", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
