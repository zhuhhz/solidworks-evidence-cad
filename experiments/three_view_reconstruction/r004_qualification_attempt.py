"""Attempt-local Q01 qualification state, derived from immutable evidence.

This module never launches CAD. Legacy attempt 1 is read only. An exclusive
reservation consumes one formal invocation even when launch later fails.
"""
from __future__ import annotations

from hashlib import sha256
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re


MAX_FORMAL_INVOCATIONS = 1
MAX_REAL_SW_RUNS = 1
AUTHORIZATION_KIND = "GPT_MAIN_EXPLICIT_QUALIFICATION_ATTEMPT"


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class AttemptStateError(ValueError):
    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.gate = "QUALIFICATION_ATTEMPT_PREFLIGHT"
        super().__init__(f"{code}: {detail}" if detail else code)


def need(condition, code, detail=""):
    if not condition:
        raise AttemptStateError(code, detail)


def digest(path):
    return sha256(Path(path).read_bytes()).hexdigest()


def document_digest(document):
    return sha256(json.dumps(document, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()


def read_document(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_exclusive(path, document):
    with Path(path).open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(document, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def case_root(artifact_root, case_id):
    need(isinstance(case_id, str) and re.fullmatch(r"Q[0-9]{2}", case_id), "ATTEMPT_IDENTITY_CONFLICT", "case ID")
    base = Path(artifact_root).absolute()
    need(not base.is_symlink() and not base.is_junction() and
         not base.parent.is_symlink() and not base.parent.is_junction(),
         "UNAPPROVED_WORKTREE_DIRTINESS", "artifact root reparse")
    root = base.resolve() / case_id.lower()
    need(not root.is_symlink() and not root.is_junction(),
         "UNAPPROVED_WORKTREE_DIRTINESS", "case root reparse")
    return root


def revision_root(artifact_root, case_id, case_revision):
    """An explicit revision adds a namespace; it never changes the case root."""
    need(case_id == "Q02" and case_revision == "V2", "ATTEMPT_IDENTITY_CONFLICT", "case revision")
    root = case_root(artifact_root, case_id)
    for name in ("revisions", "v2"):
        root = root / name
        need(not root.is_symlink() and not root.is_junction(), "UNAPPROVED_WORKTREE_DIRTINESS", "revision reparse")
    return root


def bootstrap_path(artifact_root, *, case_id, case_revision):
    root = revision_root(artifact_root, case_id, case_revision) / "bootstrap"
    need(not root.is_symlink() and not root.is_junction(), "UNAPPROVED_WORKTREE_DIRTINESS", "bootstrap reparse")
    return root


def attempt_root(artifact_root, case_id, *, case_revision=None):
    if case_revision is None:
        return case_root(artifact_root, case_id)
    root = revision_root(artifact_root, case_id, case_revision) / "attempts"
    need(not root.is_symlink() and not root.is_junction(), "UNAPPROVED_WORKTREE_DIRTINESS", "attempt reparse")
    return root


def attempt_path(root, kind, number, *, case_id="Q01", case_revision=None):
    minimum = 1 if case_id == "Q02" else 2
    need(case_id in {"Q01", "Q02"} and type(number) is int and minimum <= number <= 9999,
         "ATTEMPT_IDENTITY_CONFLICT", "attempt number")
    if case_revision is not None:
        root = Path(root)
        need(case_id == "Q02" and case_revision == "V2" and
             kind in {"formal_run_reservations", "formal_execution_started"} and
             tuple(p.name for p in (root, root.parent, root.parent.parent, root.parent.parent.parent)) ==
             ("attempts", "v2", "revisions", "q02"), "ATTEMPT_IDENTITY_CONFLICT", "revision namespace")
        need(all(not p.is_symlink() and not p.is_junction() for p in
                 (root, root.parent, root.parent.parent, root.parent.parent.parent, root / kind)),
             "UNAPPROVED_WORKTREE_DIRTINESS", "revision namespace reparse")
    elif case_id == "Q02":
        need(Path(root).name == "q02", "ATTEMPT_IDENTITY_CONFLICT", "case namespace")
    return root / kind / f"attempt-{number:04d}.json"


def attempt_cache_path(output, number, *, case_id="Q01"):
    need(case_id in {"Q01", "Q02"} and type(number) is int and
         (1 if case_id == "Q02" else 2) <= number <= 9999,
         "ATTEMPT_IDENTITY_CONFLICT", "cache attempt number")
    return Path(output) / f"attempt-{number:04d}-runtime_cache"


def sealed_manifest(folder, *, legacy=False):
    name = "qualification_manifest.json" if legacy else "manifest.json"
    manifest = folder / name
    seal = folder / (name.removesuffix(".json") + ".sha256")
    need(manifest.is_file() and seal.is_file(), "PARENT_MANIFEST_MISMATCH", "manifest or detached seal absent")
    words = seal.read_text(encoding="ascii").split()
    need(len(words) == 2 and words == [digest(manifest), name], "PARENT_MANIFEST_MISMATCH", "detached seal")
    body = read_document(manifest)
    members = {manifest.resolve(), seal.resolve()}
    for entry in body["artifacts"]:
        name = entry["relative_path"]
        need(isinstance(name, str) and name and "\\" not in name and
             not Path(name).is_absolute() and ".." not in Path(name).parts,
             "PARENT_MANIFEST_MISMATCH", "artifact member path")
        raw = folder / name
        try:
            path = raw.resolve(strict=True)
        except OSError as exc:
            raise AttemptStateError("PARENT_MANIFEST_MISMATCH", "artifact member absent") from exc
        need(not raw.is_symlink() and not raw.is_junction() and
             path.is_relative_to(folder.resolve()) and path.is_file() and
             path.stat().st_size == entry["size"] and digest(path) == entry["sha256"] and path not in members,
             "PARENT_MANIFEST_MISMATCH", "artifact member changed")
        members.add(path)
    return body, digest(manifest), members


def _historical_record(run, manifest, files, reservation_path, guard_path, history):
    """Prove the record before separately inspecting its qualification contract."""
    from experiments.three_view_reconstruction.r004_qualification_contract import (
        REQUIRED_ROLES, VERSION, check_gate_transitions, verify_bundle,
    )
    from experiments.three_view_reconstruction.run_r004_q01_real_qualification import GATE_ORDER
    reservation = read_document(reservation_path)
    need(guard_path.is_file(), "INVALID_HISTORY_RECORD", "sealed execution guard absent")
    guard = read_document(guard_path)
    verify_guard_document(guard, reservation, digest(reservation_path))
    need(manifest.get("qualification_contract_version") == reservation.get("qualification_contract_version") == VERSION and
         all(type(reservation.get(key)) is int and reservation[key] == 1 for key in
             ("MAX_FORMAL_INVOCATIONS", "MAX_REAL_SW_RUNS", "max_formal_invocations_for_attempt", "max_real_sw_runs_for_attempt")) and
         isinstance(reservation.get("created_at"), str) and bool(reservation["created_at"]) and
         isinstance(guard.get("created_at"), str) and bool(guard["created_at"]),
         "INVALID_HISTORY_RECORD", "historical version/budget/timestamp binding")
    identity = manifest.get("attempt_identity", {})
    expected = {key: reservation[key] for key in ("case_id", "attempt_number", "run_id", "authorization_digest", "authorized_head")}
    expected["reservation_digest"] = digest(reservation_path)
    need(identity == expected, "PARENT_MANIFEST_MISMATCH", "attempt manifest identity")
    for key in ("case_id", "attempt_number", "run_id", "authorization_digest", "reservation_digest"):
        need(manifest.get(key) == expected[key], "INVALID_HISTORY_RECORD", "manifest binding: " + key)
    head = reservation.get("current_head")
    need(isinstance(head, str) and re.fullmatch(r"[0-9a-f]{40}", head) and
         head == reservation.get("authorized_head") == reservation.get("remediation_commit") == manifest.get("HEAD"),
         "INVALID_HISTORY_RECORD", "historical head binding")
    need(reservation.get("attempt_governance_remediation_commit") == head and
         reservation.get("parent_failure_run_id") == reservation.get("parent_run_id") and
         reservation.get("parent_failure_manifest_sha256") == reservation.get("parent_manifest_sha256"),
         "INVALID_HISTORY_RECORD", "historical lineage")
    need({p.resolve() for p in run.rglob("*") if p.is_file()} == files,
         "INVALID_HISTORY_RECORD", "unsealed historical member")
    by_role = {}
    for entry in manifest["artifacts"]:
        role = entry.get("role")
        if role in REQUIRED_ROLES:
            need(role not in by_role, "INVALID_HISTORY_RECORD", "duplicate historical role: " + role)
            by_role[role] = run / entry["relative_path"]
    need(set(by_role) >= REQUIRED_ROLES, "INVALID_HISTORY_RECORD", "mandatory historical record absent")
    auth = read_document(by_role["AUTHORIZATION"])
    need(validate_authorization(auth, case_id=reservation["case_id"], attempt_number=reservation["attempt_number"],
         current_head=head, history=history) == reservation["authorization_digest"] and
         auth.get("authorization_kind") == reservation.get("authorization_kind") and
         auth.get("authorization_id") == reservation.get("authorization_id") and
         read_document(by_role["RESERVATION_COPY"]) == reservation,
         "INVALID_HISTORY_RECORD", "historical authorization/reservation")
    source = read_document(by_role["SOURCE_PROVENANCE"])
    need(source.get("attempt_identity") == identity and
         source.get("attempt_governance_remediation_commit") == head and
         isinstance(source.get("backend"), str) and bool(source["backend"]) and
         isinstance(source.get("files"), list) and isinstance(source.get("runtime_support"), dict) and
         all(isinstance(entry, dict) and isinstance(entry.get("relative_path"), str) and bool(entry["relative_path"]) and
             isinstance(entry.get("sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
             for entry in source["files"]),
         "INVALID_HISTORY_RECORD", "historical source identity")
    for key in ("c2a_evidence_manifest_sha256", "backend_provenance_digest", "runtime_support_provenance_digest"):
        value = reservation.get(key)
        need(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) and
             manifest.get(key) == source.get(key) == value,
             "INVALID_HISTORY_RECORD", "historical provenance: " + key)
    remediation = reservation.get("runtime_remediation_commit")
    need(isinstance(remediation, str) and re.fullmatch(r"[0-9a-f]{40}", remediation) and
         source.get("runtime_remediation_commit") == remediation and
         document_digest({"backend":source.get("backend"), "files":source.get("files")}) == reservation["backend_provenance_digest"] and
         document_digest(source.get("runtime_support")) == reservation["runtime_support_provenance_digest"],
         "INVALID_HISTORY_RECORD", "historical source/runtime provenance")
    report = read_document(by_role["FINAL_REPORT"])
    need(report.get("attempt_identity") == identity and manifest.get("final_state") == report.get("status") and
         report.get("status") in {"PASS", "FAIL"} and type(report.get("ENGINEERING_PASS")) is bool and
         (report["ENGINEERING_PASS"] is True) == (report["status"] == "PASS") and
         not (report["status"] == "FAIL" and report.get("engineering_status") == "PASS"),
         "INVALID_HISTORY_RECORD", "sealed outcome disagreement or absent")
    real = report.get("Q01_REAL_RUN_COUNT")
    need(type(real) is int and real in (0, 1), "INVALID_HISTORY_RECORD", "historical stage unknown")
    need("REAL_SW_INVOKED" not in report or
         (type(report["REAL_SW_INVOKED"]) is bool and report["REAL_SW_INVOKED"] == (real == 1)),
         "INVALID_HISTORY_RECORD", "sealed real invocation disagreement")
    need("formal_attempt_count" not in report or
         (type(report["formal_attempt_count"]) is int and report["formal_attempt_count"] == 1),
         "INVALID_HISTORY_RECORD", "sealed budget disagreement")
    check_gate_transitions(report)
    gates = report["gates"]
    need(report.get("gate_order") == list(GATE_ORDER) and set(GATE_ORDER) <= set(gates) and
         set(gates.values()) <= {"PASS", "FAIL", "NOT_REACHED", "NOT_APPLICABLE"},
         "INVALID_HISTORY_RECORD", "historical hard gates")
    need(real != 0 or gates.get("CONTROLLED_LAUNCH") in {"FAIL", "NOT_REACHED", "NOT_APPLICABLE"},
         "INVALID_HISTORY_RECORD", "pre-launch stage disagreement")
    # Independent sealed status cannot contradict the report, even for failed nodes.
    for role, gate in (("CLEANUP", "cleanup"), ("BREP_VALIDATION", "brep"),
                       ("OWNERSHIP_VALIDATION", "ownership"), ("DRAWING_VALIDATION", "drawing_comparison")):
        entries = [entry for entry in manifest["artifacts"] if entry.get("role") == role]
        for entry in entries:
            section = read_document(run / entry["relative_path"])
            need(not (section.get("status") == "FAIL" and gates.get(gate) == "PASS"),
                 "INVALID_HISTORY_RECORD", "sealed gate contradiction: " + gate)
            if role == "CLEANUP" and "CLEANUP_STATUS" in report:
                need(report["CLEANUP_STATUS"] == section.get("status"),
                     "INVALID_HISTORY_RECORD", "cleanup outcome disagreement")
    contract = "PASS"
    contract_failure = None
    try:
        verify_bundle(run, reservation_path, guard_path)
    except AttemptStateError as exc:
        # Identity, bytes, provenance, mandatory records and outcomes were already
        # proved independently. Retain this contract failure as a separate result.
        need(exc.code == "QUALIFICATION_CONTRACT_MISMATCH", "INVALID_HISTORY_RECORD", str(exc))
        contract, contract_failure = "FAIL", str(exc)
    if report["ENGINEERING_PASS"]:
        need(contract == "PASS" and real == 1 and set(GATE_ORDER) <= set(gates) and
             all(gates[name] == "PASS" for name in GATE_ORDER) and report.get("generation_truth_reads") == 0 and
             not report.get("failure_reason") and report.get("FIRST_FAILED_GATE") is None,
             "INVALID_HISTORY_RECORD", "recorded PASS contradicted by hard gates")
    else:
        need(isinstance(report.get("failure_reason"), str) and bool(report["failure_reason"].strip()) and
             ("FAIL" in gates.values() or contract == "FAIL"),
             "INVALID_HISTORY_RECORD", "recorded FAIL lacks sealed failure evidence")
        if report.get("FIRST_FAILED_GATE") is not None:
            need(gates.get(report["FIRST_FAILED_GATE"]) == "FAIL",
                 "INVALID_HISTORY_RECORD", "first failed gate disagreement")
    return report, {"HISTORICAL_RECORD_STATE": "SEALED_PASS_ATTEMPT" if report["ENGINEERING_PASS"] else "SEALED_FAIL_ATTEMPT",
        "HISTORICAL_RECORD_INTEGRITY": "PASS", "HISTORICAL_OUTCOME_CONSISTENCY": "PASS",
        "HISTORICAL_QUALIFICATION_OUTCOME": "PASS" if report["ENGINEERING_PASS"] else "PRE_LAUNCH_FAIL" if real == 0 else "FAIL",
        "HISTORICAL_STAGE": "REAL_RUN_PASS" if report["ENGINEERING_PASS"] else "PRE_LAUNCH_FAIL" if real == 0 else "REAL_RUN_FAIL",
        "HISTORICAL_BUNDLE_CONTRACT_VALIDITY": contract,
        "historical_bundle_contract_failure": contract_failure, "historical_failure_reason": report.get("failure_reason")}


def history_view(artifact_root, case_id, *, legacy_pin=None, case_revision=None):
    """Invalid records raise a classified error; no corrupt node is skipped."""
    try:
        return _history_view(artifact_root, case_id, legacy_pin=legacy_pin, case_revision=case_revision)
    except Exception as exc:
        error = exc if isinstance(exc, AttemptStateError) else AttemptStateError("INVALID_HISTORY_RECORD", str(exc))
        error.history_record_state = "INVALID_HISTORY_RECORD"
        error.historical_record_integrity = "FAIL"
        error.future_qualification = "BLOCKED"
        if error is exc: raise
        raise error from exc


def historical_preflight(artifact_root, case_id, *, legacy_pin=None, case_revision=None):
    """Read-only history gate; does not reserve, authorize, guard or launch."""
    try:
        rows, allowed = history_view(artifact_root, case_id, legacy_pin=legacy_pin, case_revision=case_revision)
        need(all(row["HISTORICAL_RECORD_STATE"] in {"SEALED_PASS_ATTEMPT", "SEALED_FAIL_ATTEMPT"}
                 for row in rows.values()), "INVALID_HISTORY_RECORD", "unsealed consumed attempt")
        verify_artifact_tree(artifact_root, case_id, allowed, case_revision=case_revision)
        return {"HISTORICAL_CHAIN_INTEGRITY":"PASS", "HISTORICAL_OUTCOME_CONSISTENCY":"PASS", "attempts":rows}
    except Exception as exc:
        error = exc if isinstance(exc, AttemptStateError) else AttemptStateError("INVALID_HISTORY_RECORD", str(exc))
        error.history_record_state = "INVALID_HISTORY_RECORD"
        error.historical_record_integrity = "FAIL"
        error.future_qualification = "BLOCKED"
        if error is exc: raise
        raise error from exc


def _history_view(artifact_root, case_id, *, legacy_pin=None, case_revision=None):
    """Return verified attempt rows and the exact files allowed by git status."""
    root = attempt_root(artifact_root, case_id, case_revision=case_revision)
    if case_id == "Q02":
        from experiments.three_view_reconstruction.r004_q02_qualification_contract import read_history
        need(legacy_pin is None, "PARENT_ATTEMPT_MISMATCH", "Q02 has no legacy Q01 parent")
        return read_history(root, case_revision=case_revision)
    rows = {}
    allowed = set()
    legacy_res = root / "formal_run_reservation.json"
    legacy_guard = root / "formal_execution_started.json"
    need(legacy_pin is None or (legacy_res.is_file() and legacy_guard.is_file()),
         "PARENT_ATTEMPT_MISMATCH", "pinned legacy attempt absent")
    if legacy_res.exists() or legacy_guard.exists():
        need(legacy_res.is_file() and legacy_guard.is_file(), "ATTEMPT_IDENTITY_CONFLICT", "partial legacy state")
        reservation, guard = read_document(legacy_res), read_document(legacy_guard)
        need(reservation.get("case") == case_id and reservation.get("formal_run_count") == 1 and
             reservation.get("MAX_FORMAL_RUNS_PER_CASE") == 1 and
             guard.get("run_id") == reservation.get("run_id") and guard.get("formal_run_count") == 1,
             "ATTEMPT_IDENTITY_CONFLICT", "legacy reservation/guard disagreement")
        folder = root / reservation["run_id"]
        need(folder.is_dir() and not folder.is_symlink() and not folder.is_junction() and
             folder.resolve().parent == root.resolve(), "ATTEMPT_IDENTITY_CONFLICT", "legacy run path")
        manifest, manifest_hash, files = sealed_manifest(folder, legacy=True)
        need(manifest.get("run_id") == reservation["run_id"] and manifest.get("case") == case_id,
             "PARENT_MANIFEST_MISMATCH", "legacy identity")
        if legacy_pin is not None:
            need(legacy_pin.get("run_id") == reservation["run_id"] and
                 legacy_pin.get("manifest_sha256") == manifest_hash and
                 legacy_pin.get("reservation_sha256") == digest(legacy_res) and
                 legacy_pin.get("execution_guard_sha256") == digest(legacy_guard),
                 "PARENT_MANIFEST_MISMATCH", "authoritative legacy pin")
        guards = manifest.get("parent_guard_hashes", {})
        need(guards.get(legacy_res.name) == digest(legacy_res) and
             guards.get(legacy_guard.name) == digest(legacy_guard),
             "PARENT_MANIFEST_MISMATCH", "legacy parent guard hash")
        outcome_path = folder / "c2b1_verified_outcome.json"
        need(outcome_path.resolve() in files, "PARENT_MANIFEST_MISMATCH", "legacy outcome absent")
        outcome = read_document(outcome_path)
        need(outcome.get("formal_runner_invocations") == 1 and outcome.get("Q01_REAL_RUN_COUNT") == 0 and
             outcome.get("FIRST_FAILED_GATE") == "controlled_launch",
             "ATTEMPT_IDENTITY_CONFLICT", "legacy outcome")
        need(outcome.get("ENGINEERING_PASS", False) is False and
             outcome.get("Q01_ENGINEERING_PASS", "NO") == "NO" and
             outcome.get("REAL_SW_INVOKED", False) is False and
             manifest.get("Q01_REAL_RUN_COUNT", 0) == 0 and manifest.get("formal_budget_consumed", 1) == 1,
             "INVALID_HISTORY_RECORD", "legacy pre-launch outcome contradiction")
        rows[1] = {"case_id": case_id, "attempt_number": 1, "run_id": reservation["run_id"],
                   "formal_invocation_count": 1, "real_sw_run_count": 0,
                   "status": "FAIL_PRE_LAUNCH", "manifest_sha256": manifest_hash,
                   "reservation_digest": digest(legacy_res), "legacy": True}
        rows[1].update(HISTORICAL_RECORD_STATE="SEALED_FAIL_ATTEMPT", HISTORICAL_RECORD_INTEGRITY="PASS",
                       HISTORICAL_OUTCOME_CONSISTENCY="PASS", HISTORICAL_QUALIFICATION_OUTCOME="PRE_LAUNCH_FAIL",
                       HISTORICAL_STAGE="PRE_LAUNCH_FAIL", HISTORICAL_BUNDLE_CONTRACT_VALIDITY="NOT_APPLICABLE",
                       historical_failure_reason=outcome.get("FAILURE_MESSAGE", outcome["FIRST_FAILED_GATE"]))
        allowed.update(files | {legacy_res.resolve(), legacy_guard.resolve()})
    namespace = root / "formal_run_reservations"
    if namespace.exists():
        need(namespace.is_dir() and not namespace.is_symlink() and not namespace.is_junction(),
             "ATTEMPT_IDENTITY_CONFLICT", "reservation namespace")
        for path in sorted(namespace.iterdir()):
            match = re.fullmatch(r"attempt-([0-9]{4}).json", path.name)
            need(match is not None and path.is_file() and not path.is_symlink(),
                 "ATTEMPT_IDENTITY_CONFLICT", "unknown reservation member")
            number = int(match.group(1))
            need(number >= 2 and number not in rows and number - 1 in rows,
                 "ATTEMPT_IDENTITY_CONFLICT", "noncontiguous attempt history")
            reservation = read_document(path)
            need(reservation.get("case_id") == case_id and reservation.get("attempt_number") == number and
                 reservation.get("formal_invocation_count_for_attempt") == 1 and
                 reservation.get("MAX_FORMAL_INVOCATIONS") == 1 and reservation.get("MAX_REAL_SW_RUNS") == 1 and
                 reservation.get("parent_attempt") == number - 1 and
                 reservation.get("parent_run_id") == rows[number - 1]["run_id"] and
                 reservation.get("parent_manifest_sha256") == rows[number - 1]["manifest_sha256"],
                 "ATTEMPT_IDENTITY_CONFLICT", "reservation content")
            run = root / reservation["run_id"]
            need(run.is_dir() and not run.is_symlink() and not run.is_junction() and
                 run.resolve().parent == root.resolve(), "ATTEMPT_IDENTITY_CONFLICT", "run identity")
            if (run / "manifest.json").exists():
                manifest, manifest_hash, files = sealed_manifest(run)
                report, classification = _historical_record(run, manifest, files, path,
                    attempt_path(root, "formal_execution_started", number), rows)
            else:
                manifest_hash, files = None, set()
                report, classification = {}, {"HISTORICAL_RECORD_STATE":"INVALID_HISTORY_RECORD",
                    "HISTORICAL_RECORD_INTEGRITY":"FAIL", "HISTORICAL_OUTCOME_CONSISTENCY":"NOT_VALIDATED",
                    "HISTORICAL_QUALIFICATION_OUTCOME":"UNKNOWN", "HISTORICAL_BUNDLE_CONTRACT_VALIDITY":"NOT_APPLICABLE"}
            guard_path = attempt_path(root, "formal_execution_started", number)
            if guard_path.exists():
                guard = read_document(guard_path)
                verify_guard_document(guard, reservation, digest(path))
                allowed.add(guard_path.resolve())
            real = report.get("Q01_REAL_RUN_COUNT")
            need(real in (0, 1, None) and type(real) is not bool, "ATTEMPT_IDENTITY_CONFLICT", "real run count")
            rows[number] = {"case_id": case_id, "attempt_number": number, "run_id": reservation["run_id"],
                            "formal_invocation_count": 1, "real_sw_run_count": real,
                            "status": "PASS" if report.get("ENGINEERING_PASS") is True else
                                      "FAIL" if report else "UNSEALED",
                            "manifest_sha256": manifest_hash, "reservation_digest": digest(path), "legacy": False}
            rows[number].update(classification)
            allowed.update(files | {path.resolve()})
    guard_namespace = root / "formal_execution_started"
    if guard_namespace.exists():
        need(guard_namespace.is_dir() and not guard_namespace.is_symlink() and not guard_namespace.is_junction() and
             {p.resolve() for p in guard_namespace.iterdir()} <= allowed,
             "EXECUTION_GUARD_ALREADY_EXISTS", "orphan/unknown guard")
    return rows, allowed


def verify_artifact_tree(artifact_root, case_id, allowed_files, *, case_revision=None):
    root = attempt_root(artifact_root, case_id, case_revision=case_revision)
    if not root.exists():
        return
    need(not root.is_symlink() and not root.is_junction(), "UNAPPROVED_WORKTREE_DIRTINESS", "artifact root")
    for path in root.rglob("*"):
        need(not path.is_symlink() and not path.is_junction() and
             (not path.is_file() or path.resolve() in allowed_files),
             "UNAPPROVED_WORKTREE_DIRTINESS", "unknown artifact member")


def validate_authorization(document, *, case_id, attempt_number, current_head, history, case_revision=None):
    need(isinstance(case_id, str) and re.fullmatch(r"Q[0-9]{2}", case_id),
         "ATTEMPT_IDENTITY_CONFLICT", "case ID")
    if case_id == "Q02":
        from experiments.three_view_reconstruction.r004_q02_qualification_contract import validate_authorization as validate_q02
        return validate_q02(document, attempt_number=attempt_number, current_head=current_head, history=history,
                            case_revision=case_revision)
    need(case_revision is None, "ATTEMPT_IDENTITY_CONFLICT", "legacy case revision")
    need(isinstance(document, dict) and document.get("authorization_kind") == AUTHORIZATION_KIND and
         isinstance(document.get("authorization_id"), str) and bool(document["authorization_id"].strip()),
         "ATTEMPT_NOT_AUTHORIZED")
    need(document.get("case_id") == case_id and document.get("attempt_number") == attempt_number and
         type(attempt_number) is int and 2 <= attempt_number <= 9999,
         "ATTEMPT_IDENTITY_CONFLICT")
    need(document.get("authorized_head") == current_head, "AUTHORIZED_HEAD_MISMATCH")
    need(document.get("remediation_commit") == current_head, "ATTEMPT_IDENTITY_CONFLICT", "remediation commit")
    parent = history.get(attempt_number - 1)
    need(parent is not None and parent["status"] in {"FAIL_PRE_LAUNCH", "FAIL"} and
         document.get("parent_attempt") == attempt_number - 1 and
         document.get("parent_run_id") == parent["run_id"], "PARENT_ATTEMPT_MISMATCH")
    need(document.get("parent_manifest_sha256") == parent["manifest_sha256"], "PARENT_MANIFEST_MISMATCH")
    need(attempt_number not in history, "ATTEMPT_ALREADY_CONSUMED")
    return document_digest(document)


def validate_worktree_status(status, tracked_changes, allowed_files, repository):
    need(not tracked_changes.strip(), "UNAPPROVED_WORKTREE_DIRTINESS", "tracked diff")
    repository = Path(repository).resolve()
    for line in status.splitlines():
        need(line.startswith("?? ") and not line[3:].startswith('"'),
             "UNAPPROVED_WORKTREE_DIRTINESS", "unexpected Git status")
        rel = line[3:].replace("\\", "/")
        need(rel.startswith("artifacts/r004_real/") and ".." not in Path(rel).parts and
             (repository / rel).resolve() in allowed_files,
             "UNAPPROVED_WORKTREE_DIRTINESS", rel)


def reserve_attempt(artifact_root, document, run_id, *, current_head, history, provenance):
    from experiments.three_view_reconstruction.r004_qualification_contract import VERSION
    case_id, number = document["case_id"], document["attempt_number"]
    case_revision = document.get("case_revision") if case_id == "Q02" else None
    if case_id == "Q02":
        from experiments.three_view_reconstruction.r004_q02_qualification_contract import VERSION
    auth_digest = validate_authorization(document, case_id=case_id, attempt_number=number,
                                         current_head=current_head, history=history, case_revision=case_revision)
    need(isinstance(run_id, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", run_id),
         "ATTEMPT_IDENTITY_CONFLICT", "run ID")
    if case_id == "Q02":
        need(re.fullmatch(r"r004-q02-[A-Za-z0-9_-]{1,65}", run_id),
             "ATTEMPT_IDENTITY_CONFLICT", "Q02 run namespace")
    root = attempt_root(artifact_root, case_id, case_revision=case_revision)
    reservation_path = attempt_path(root, "formal_run_reservations", number, case_id=case_id, case_revision=case_revision)
    guard_path = attempt_path(root, "formal_execution_started", number, case_id=case_id, case_revision=case_revision)
    need(not reservation_path.exists(), "RESERVATION_ALREADY_EXISTS")
    need(not guard_path.exists(), "EXECUTION_GUARD_ALREADY_EXISTS")
    need(root.is_dir() and not (root / run_id).exists() and
         run_id not in {row["run_id"] for row in history.values()},
         "ATTEMPT_IDENTITY_CONFLICT", "duplicate run ID")
    required = ("runtime_remediation_commit", "c2a_evidence_manifest_sha256",
                "backend_provenance_digest", "runtime_support_provenance_digest")
    if case_id == "Q02":
        required += ("generation_input_sha256", "canonical_plan_digest", "project_history_digest",
                     "environment_preflight_digest")
    need(isinstance(provenance, dict) and all(isinstance(provenance.get(key), str) and
         bool(provenance[key]) for key in required),
         "QUALIFICATION_CONTRACT_MISMATCH", "reservation provenance")
    need(re.fullmatch(r"[0-9a-f]{40}", provenance["runtime_remediation_commit"]) is not None and
         all(re.fullmatch(r"[0-9a-f]{64}", provenance[key]) is not None for key in required[1:]),
         "QUALIFICATION_CONTRACT_MISMATCH", "provenance digest format")
    parent = history.get(number - 1, {})
    reservation_path.parent.mkdir(exist_ok=True)
    reservation = {"qualification_contract_version": VERSION,
                   "case_id": case_id, "attempt_number": number, "run_id": run_id,
                   "authorization_kind": document["authorization_kind"], "authorization_id": document["authorization_id"],
                   "authorization_digest": auth_digest, "authorized_head": current_head, "current_head": current_head,
                   "remediation_commit": document["remediation_commit"],
                   "parent_attempt": number - 1 if parent else None, "parent_run_id": parent.get("run_id"),
                   "parent_manifest_sha256": parent.get("manifest_sha256"),
                   "parent_failure_run_id": parent.get("run_id"),
                   "parent_failure_manifest_sha256": parent.get("manifest_sha256"),
                   "runtime_remediation_commit": provenance["runtime_remediation_commit"],
                   "attempt_governance_remediation_commit": current_head,
                   "c2a_evidence_manifest_sha256": provenance["c2a_evidence_manifest_sha256"],
                   "backend_provenance_digest": provenance["backend_provenance_digest"],
                   "runtime_support_provenance_digest": provenance["runtime_support_provenance_digest"],
                   "created_at": utc_now(),
                   "MAX_FORMAL_INVOCATIONS": MAX_FORMAL_INVOCATIONS, "MAX_REAL_SW_RUNS": MAX_REAL_SW_RUNS,
                   "max_formal_invocations_for_attempt": MAX_FORMAL_INVOCATIONS,
                   "max_real_sw_runs_for_attempt": MAX_REAL_SW_RUNS,
                   "formal_invocation_count_for_attempt": 1, "real_sw_run_count_for_attempt": 0,
                   "state": "RESERVED_BEFORE_LAUNCH"}
    if case_id == "Q02":
        reservation["case_revision"] = case_revision
        reservation.update({key: provenance[key] for key in required[4:]})
    try:
        write_exclusive(reservation_path, reservation)
    except FileExistsError as exc:
        raise AttemptStateError("RESERVATION_ALREADY_EXISTS") from exc
    output = root / run_id
    output.mkdir(exist_ok=False)
    return output, reservation_path, digest(reservation_path)


def verify_guard_document(guard, reservation, reservation_digest):
    if reservation["case_id"] == "Q02":
        need(reservation.get("case_revision") == guard.get("case_revision") == "V2",
             "ATTEMPT_IDENTITY_CONFLICT", "guard revision binding")
    need(guard.get("case_id") == reservation["case_id"] and
         guard.get("qualification_contract_version") == reservation.get("qualification_contract_version") and
         guard.get("attempt_number") == reservation["attempt_number"] and
         guard.get("run_id") == reservation["run_id"] and
         guard.get("authorized_head") == reservation["authorized_head"] and
         guard.get("current_head") == reservation["current_head"] and
         guard.get("authorization_digest") == reservation["authorization_digest"] and
         guard.get("reservation_digest") == reservation_digest,
         "ATTEMPT_IDENTITY_CONFLICT", "execution guard binding")


def create_execution_guard(reservation_path, *, document, current_head, expected_run_id,
                           expected_reservation_digest):
    reservation_path = Path(reservation_path).resolve(strict=True)
    reservation = read_document(reservation_path)
    root = reservation_path.parent.parent
    case_revision = reservation.get("case_revision") if reservation["case_id"] == "Q02" else None
    if reservation["case_id"] == "Q02":
        need(document.get("case_revision") == case_revision == "V2",
             "ATTEMPT_IDENTITY_CONFLICT", "authorization revision binding")
    need(reservation_path == attempt_path(root, "formal_run_reservations", reservation["attempt_number"],
                                        case_id=reservation["case_id"], case_revision=case_revision),
         "ATTEMPT_IDENTITY_CONFLICT", "reservation namespace")
    need(reservation.get("authorized_head") == current_head,
         "AUTHORIZED_HEAD_MISMATCH")
    need(reservation.get("run_id") == expected_run_id and digest(reservation_path) == expected_reservation_digest,
         "ATTEMPT_IDENTITY_CONFLICT", "reservation changed after creation")
    need(document_digest(document) == reservation.get("authorization_digest") and
         document.get("case_id") == reservation["case_id"] and
         document.get("attempt_number") == reservation["attempt_number"] and
         document.get("parent_run_id") == reservation["parent_run_id"],
         "ATTEMPT_NOT_AUTHORIZED", "authorization changed after reservation")
    if reservation["case_id"] == "Q02":
        from experiments.three_view_reconstruction.r004_q02_qualification_contract import VERSION
        need(document.get("qualification_contract_version") == reservation["qualification_contract_version"] == VERSION,
             "QUALIFICATION_CONTRACT_MISMATCH")
        need(reservation.get("state") == "RESERVED_BEFORE_LAUNCH" and
             reservation.get("formal_invocation_count_for_attempt") == 1 and
             reservation.get("real_sw_run_count_for_attempt") == 0,
             "ATTEMPT_IDENTITY_CONFLICT", "consumed invocation budget")
        need(reservation.get("current_head") == current_head == document.get("authorized_head") and
             reservation.get("remediation_commit") == document.get("remediation_commit") == current_head and
             all(type(reservation.get(key)) is int and reservation[key] == 1 and
                 type(document.get(key)) is int and document[key] == 1
                 for key in ("MAX_FORMAL_INVOCATIONS", "MAX_REAL_SW_RUNS")),
             "ATTEMPT_NOT_AUTHORIZED", "HEAD/budget binding")
    path = attempt_path(root, "formal_execution_started", reservation["attempt_number"],
                        case_id=reservation["case_id"], case_revision=case_revision)
    need(not path.exists(), "EXECUTION_GUARD_ALREADY_EXISTS")
    path.parent.mkdir(exist_ok=True)
    guard = {"qualification_contract_version": reservation["qualification_contract_version"],
             "case_id": reservation["case_id"], "attempt_number": reservation["attempt_number"],
             "run_id": reservation["run_id"], "authorized_head": current_head,
             "current_head": current_head, "created_at": utc_now(),
             "authorization_digest": reservation["authorization_digest"],
             "reservation_digest": digest(reservation_path)}
    if reservation["case_id"] == "Q02":
        guard["case_revision"] = case_revision
    try:
        write_exclusive(path, guard)
    except FileExistsError as exc:
        raise AttemptStateError("EXECUTION_GUARD_ALREADY_EXISTS") from exc
    return path
