"""Q02 case-local first attempt and retries; the frozen Q01 contract is unchanged.

Only identity, sealed records and gate requirements live here. No engineering
answers, CAD launcher or executor are part of this contract.
"""
from pathlib import Path
import re

from experiments.three_view_reconstruction import r004_qualification_attempt as a
from experiments.three_view_reconstruction.r004_qualification_contract import (
    MANIFEST, SEAL, REQUIRED_ROLES,
    artifact_role, category_states, check_gate_transitions,
)

VERSION = "R004-Q02-V2-A1-V1"
PROVENANCE_DIGESTS = (
    "c2a_evidence_manifest_sha256", "backend_provenance_digest", "runtime_support_provenance_digest",
    "generation_input_sha256", "canonical_plan_digest", "project_history_digest", "environment_preflight_digest",
)
PARENT_FIELDS = ("parent_attempt", "parent_run_id", "parent_manifest_sha256")
PASS_ROLES = REQUIRED_ROLES | {"PREFLIGHT", "NATIVE_PART", "NATIVE_DRAWING", "NATIVE_EVIDENCE",
    "BREP_VALIDATION", "OWNERSHIP_VALIDATION", "DRAWING_INITIAL", "DRAWING_NATIVE", "DRAWING_VALIDATION", "TRUTH_AUDIT"}


def revision_policy():
    """G0 eligibility policy; these fields are not historical attempt outcomes."""
    return {"case_id":"Q02", "active_qualification_revision":"V2",
            "V1":{"real_attempt":"NONE", "end_to_end_provenance":"BLOCKED",
                  "attempt_eligibility":"BLOCKED", "superseded_for_future_qualification":True}}


def validate_authorization(document, *, attempt_number, current_head, history, case_revision=None):
    a.need(case_revision == "V2" and isinstance(document, dict) and
           document.get("case_revision") == case_revision, "ATTEMPT_IDENTITY_CONFLICT", "authorization revision")
    a.need(isinstance(document, dict) and document.get("authorization_kind") == a.AUTHORIZATION_KIND and
           isinstance(document.get("authorization_id"), str) and bool(document["authorization_id"].strip()),
           "ATTEMPT_NOT_AUTHORIZED")
    a.need(document.get("case_id") == "Q02" and type(attempt_number) is int and
           1 <= attempt_number <= 9999 and type(document.get("attempt_number")) is int and
           document["attempt_number"] == attempt_number, "ATTEMPT_IDENTITY_CONFLICT")
    a.need(document.get("qualification_contract_version") == VERSION, "QUALIFICATION_CONTRACT_MISMATCH")
    a.need(isinstance(current_head, str) and re.fullmatch(r"[0-9a-f]{40}", current_head) and
           document.get("authorized_head") == current_head, "AUTHORIZED_HEAD_MISMATCH")
    a.need(document.get("remediation_commit") == current_head, "ATTEMPT_IDENTITY_CONFLICT", "source HEAD")
    a.need(type(document.get("MAX_FORMAL_INVOCATIONS")) is int and document["MAX_FORMAL_INVOCATIONS"] == 1 and
           type(document.get("MAX_REAL_SW_RUNS")) is int and document["MAX_REAL_SW_RUNS"] == 1,
           "ATTEMPT_NOT_AUTHORIZED", "explicit single invocation budgets")
    a.need(attempt_number not in history, "ATTEMPT_ALREADY_CONSUMED")
    if attempt_number == 1:
        a.need(not history and all(key in document and document[key] is None for key in PARENT_FIELDS),
               "PARENT_ATTEMPT_MISMATCH", "first attempt has no case-local parent")
    else:
        parent = history.get(attempt_number - 1)
        a.need(all(row.get("case_id") == "Q02" and row.get("case_revision") == case_revision
                   for row in history.values()), "PARENT_ATTEMPT_MISMATCH", "revision-local history")
        a.need(set(history) == set(range(1, attempt_number)) and parent is not None and
               parent.get("case_id") == "Q02" and parent.get("case_revision") == case_revision and
               parent.get("attempt_number") == attempt_number - 1 and
               parent.get("HISTORICAL_RECORD_STATE") == "SEALED_FAIL_ATTEMPT" and
               parent.get("HISTORICAL_RECORD_INTEGRITY") == "PASS" and parent.get("status") in {"FAIL", "FAIL_PRE_LAUNCH"} and
               parent.get("evidence_kind") == "CONTROLLED_QUALIFICATION" and
               isinstance(parent.get("backend"), str) and bool(parent["backend"]) and
               parent["backend"] not in {"SYNTHETIC", "SIMULATED"} and
               document.get("parent_attempt") == attempt_number - 1 and
               document.get("parent_run_id") == parent.get("run_id"), "PARENT_ATTEMPT_MISMATCH")
        a.need(document.get("parent_manifest_sha256") == parent.get("manifest_sha256") and
               isinstance(parent.get("manifest_sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", parent["manifest_sha256"]),
               "PARENT_MANIFEST_MISMATCH")
    return a.document_digest(document)


def _record(folder, reservation_path, guard_path, history, *, case_revision):
    """Prove immutable identity/provenance/outcome before bundle policy classification."""
    from experiments.three_view_reconstruction.run_r004_q01_real_qualification import GATE_ORDER
    folder = Path(folder).resolve(strict=True)
    manifest, manifest_hash, files = a.sealed_manifest(folder)
    reservation = a.read_document(reservation_path)
    a.need(case_revision == "V2" and reservation.get("case_revision") == case_revision,
           "INVALID_HISTORY_RECORD", "reservation revision")
    a.need(Path(guard_path).is_file(), "INVALID_HISTORY_RECORD", "execution guard absent")
    guard = a.read_document(guard_path)
    a.verify_guard_document(guard, reservation, a.digest(reservation_path))
    number = reservation.get("attempt_number")
    a.need(reservation.get("case_id") == "Q02" and type(number) is int and 1 <= number <= 9999,
           "INVALID_HISTORY_RECORD", "case identity")
    root = folder.parent
    a.need(Path(reservation_path).resolve() ==
           a.attempt_path(root, "formal_run_reservations", number, case_id="Q02", case_revision=case_revision).resolve() and
           Path(guard_path).resolve() == a.attempt_path(root, "formal_execution_started", number,
                                                     case_id="Q02", case_revision=case_revision).resolve(),
           "INVALID_HISTORY_RECORD", "case-local records")
    a.need(manifest.get("qualification_contract_version") == reservation.get("qualification_contract_version") ==
           guard.get("qualification_contract_version") == VERSION, "INVALID_HISTORY_RECORD", "contract binding")
    identity = {key: reservation[key] for key in ("case_id", "case_revision", "attempt_number", "run_id", "authorized_head", "authorization_digest")}
    identity["reservation_digest"] = a.digest(reservation_path)
    a.need(folder.name == reservation["run_id"] and manifest.get("attempt_identity") == identity and
           all(manifest.get(key) == value for key, value in identity.items() if key != "authorized_head") and
           manifest.get("HEAD") == reservation.get("current_head") == reservation["authorized_head"] ==
           reservation.get("remediation_commit") == reservation.get("attempt_governance_remediation_commit"),
           "INVALID_HISTORY_RECORD", "HEAD or manifest identity")
    a.need(all(type(reservation.get(key)) is int and reservation[key] == 1 for key in
           ("MAX_FORMAL_INVOCATIONS", "MAX_REAL_SW_RUNS", "max_formal_invocations_for_attempt",
            "max_real_sw_runs_for_attempt", "formal_invocation_count_for_attempt")) and
           type(reservation.get("real_sw_run_count_for_attempt")) is int and reservation["real_sw_run_count_for_attempt"] == 0 and
           reservation.get("created_at") and guard.get("created_at"), "INVALID_HISTORY_RECORD", "budget/timestamp")
    a.need({p.resolve() for p in folder.rglob("*") if p.is_file()} == files,
           "INVALID_HISTORY_RECORD", "unsealed member")
    by_role = {}
    for entry in manifest["artifacts"]:
        role = entry.get("role")
        a.need(role not in by_role or role == "RUNTIME_CACHE_MEMBER", "INVALID_HISTORY_RECORD", "duplicate role")
        by_role[role] = folder / entry["relative_path"]
    a.need(REQUIRED_ROLES <= set(by_role), "INVALID_HISTORY_RECORD", "mandatory record missing")
    auth = a.read_document(by_role["AUTHORIZATION"])
    a.need(validate_authorization(auth, attempt_number=number, current_head=reservation["current_head"], history=history,
                                  case_revision=case_revision) ==
           reservation["authorization_digest"] and a.read_document(by_role["RESERVATION_COPY"]) == reservation and
           all(auth.get(key) == reservation.get(key) for key in PARENT_FIELDS + ("authorization_id", "authorization_kind")) and
           reservation.get("parent_failure_run_id") == reservation.get("parent_run_id") and
           reservation.get("parent_failure_manifest_sha256") == reservation.get("parent_manifest_sha256"),
           "INVALID_HISTORY_RECORD", "authorization or parent binding")
    source = a.read_document(by_role["SOURCE_PROVENANCE"])
    a.need(source.get("attempt_identity") == identity and
           source.get("attempt_governance_remediation_commit") == reservation["current_head"] and
           source.get("runtime_remediation_commit") == reservation.get("runtime_remediation_commit") and
           re.fullmatch(r"[0-9a-f]{40}", str(reservation.get("runtime_remediation_commit"))) and
           isinstance(source.get("backend"), str) and bool(source["backend"]) and isinstance(source.get("files"), list) and
           isinstance(source.get("runtime_support"), dict), "INVALID_HISTORY_RECORD", "source identity")
    for key in PROVENANCE_DIGESTS:
        value = reservation.get(key)
        a.need(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) and
               value == manifest.get(key) == source.get(key), "INVALID_HISTORY_RECORD", "provenance: " + key)
    a.need(a.document_digest({"backend":source["backend"], "files":source["files"]}) == reservation["backend_provenance_digest"] and
           a.document_digest(source["runtime_support"]) == reservation["runtime_support_provenance_digest"],
           "INVALID_HISTORY_RECORD", "source digest")
    a.need(all(isinstance(entry, dict) and isinstance(entry.get("relative_path"), str) and
               bool(entry["relative_path"]) and re.fullmatch(r"[0-9a-f]{64}", str(entry.get("sha256")))
               for entry in source["files"]), "INVALID_HISTORY_RECORD", "source file pins")
    report = a.read_document(by_role["FINAL_REPORT"])
    a.need(report.get("attempt_identity") == identity and report.get("status") in {"PASS", "FAIL"} and
           type(report.get("ENGINEERING_PASS")) is bool and report["ENGINEERING_PASS"] == (report["status"] == "PASS") and
           manifest.get("final_state") == report["status"] and manifest.get("engineering_status") == report.get("engineering_status") and
           (report.get("engineering_status") == "PASS") == report["ENGINEERING_PASS"],
           "INVALID_HISTORY_RECORD", "outcome identity")
    real = report.get("REAL_SW_RUN_COUNT")
    a.need(type(real) is int and real in {0, 1} and type(report.get("formal_attempt_count")) is int and
           report["formal_attempt_count"] == 1 and type(report.get("REAL_SW_INVOKED")) is bool and
           report["REAL_SW_INVOKED"] == (real == 1), "INVALID_HISTORY_RECORD", "run budget")
    a.need(report.get("gate_order") == list(GATE_ORDER) and isinstance(report.get("gates"), dict) and
           set(GATE_ORDER) <= set(report["gates"]) and
           set(report["gates"].values()) <= {"PASS", "FAIL", "NOT_REACHED", "NOT_APPLICABLE"},
           "INVALID_HISTORY_RECORD", "required gates")
    check_gate_transitions(report)
    gates = report["gates"]
    a.need(real != 0 or gates["CONTROLLED_LAUNCH"] in {"FAIL", "NOT_REACHED", "NOT_APPLICABLE"},
           "INVALID_HISTORY_RECORD", "pre-launch stage")
    for role, gate in (("CLEANUP", "cleanup"), ("BREP_VALIDATION", "brep"),
                       ("OWNERSHIP_VALIDATION", "ownership"), ("DRAWING_VALIDATION", "drawing_comparison")):
        if role in by_role:
            section = a.read_document(by_role[role])
            a.need(not (section.get("status") == "FAIL" and gates[gate] == "PASS"),
                   "INVALID_HISTORY_RECORD", "sealed gate contradiction")
            a.need(gates[gate] != "PASS" or section.get("status") == "PASS",
                   "INVALID_HISTORY_RECORD", "PASS gate has no independent result")
    if report["ENGINEERING_PASS"]:
        a.need(real == 1 and all(gates[key] == "PASS" for key in GATE_ORDER) and
               type(report.get("generation_truth_reads")) is int and report["generation_truth_reads"] == 0 and
               type(report.get("generation_denylist_violations")) is int and report["generation_denylist_violations"] == 0 and
               not report.get("failure_reason") and
               report.get("FIRST_FAILED_GATE") is None, "INVALID_HISTORY_RECORD", "PASS contradicts gates")
    else:
        a.need(isinstance(report.get("failure_reason"), str) and bool(report["failure_reason"].strip()),
               "INVALID_HISTORY_RECORD", "FAIL has no reason")
        failed = report.get("FIRST_FAILED_GATE")
        a.need(failed is None or gates.get(failed) == "FAIL", "INVALID_HISTORY_RECORD", "failed gate")
    return manifest, manifest_hash, files, report, set(by_role), source


def _bundle_policy(manifest, report, roles):
    for entry in manifest["artifacts"]:
        a.need((entry.get("role"), entry.get("semantic_category")) == artifact_role(entry["relative_path"]) and
               entry.get("role") != "UNCLASSIFIED", "QUALIFICATION_CONTRACT_MISMATCH", "artifact role")
    a.need(manifest.get("semantic_categories") == category_states(report, roles),
           "QUALIFICATION_CONTRACT_MISMATCH", "semantic categories")
    a.need(report["gates"].get("save") != "PASS" or "NATIVE_PART" in roles,
           "QUALIFICATION_CONTRACT_MISMATCH", "saved native part absent")
    a.need(report["gates"].get("drawing_save_reopen") != "PASS" or "NATIVE_DRAWING" in roles,
           "QUALIFICATION_CONTRACT_MISMATCH", "saved native drawing absent")
    if report["ENGINEERING_PASS"]:
        a.need(PASS_ROLES <= roles, "QUALIFICATION_CONTRACT_MISMATCH", "required real evidence roles")
        a.need(report.get("evidence_kind") not in {"SYNTHETIC", "SIMULATED"},
               "QUALIFICATION_CONTRACT_MISMATCH", "synthetic evidence is not qualification")


def verify_bundle(folder, reservation_path, guard_path, *, history, case_revision=None):
    manifest, _, _, report, roles, _ = _record(folder, reservation_path, guard_path, history, case_revision=case_revision)
    _bundle_policy(manifest, report, roles)
    a.need(report["ENGINEERING_PASS"] or "FAIL" in report["gates"].values(),
           "QUALIFICATION_CONTRACT_MISMATCH", "failure evidence absent")
    return "PASS"


def read_history(root, *, case_revision=None):
    """Q02 starts at modern attempt 1. Q01 legacy records cannot supply a parent."""
    if case_revision is not None:
        a.attempt_path(root, "formal_run_reservations", 1, case_id="Q02", case_revision=case_revision)
    a.need(not (root / "formal_run_reservation.json").exists() and not (root / "formal_execution_started.json").exists(),
           "INVALID_HISTORY_RECORD", "Q02 cannot import legacy Q01")
    rows, allowed = {}, set()
    namespace = root / "formal_run_reservations"
    if namespace.exists():
        a.need(namespace.is_dir() and not namespace.is_symlink() and not namespace.is_junction(),
               "INVALID_HISTORY_RECORD", "reservation namespace")
        for path in sorted(namespace.iterdir()):
            match = re.fullmatch(r"attempt-([0-9]{4}).json", path.name)
            a.need(match is not None and path.is_file() and not path.is_symlink() and not path.is_junction(),
                   "INVALID_HISTORY_RECORD", "unknown reservation")
            number = int(match.group(1))
            a.need(number == len(rows) + 1, "INVALID_HISTORY_RECORD", "sequence gap")
            reservation = a.read_document(path)
            a.need(reservation.get("attempt_number") == number and isinstance(reservation.get("run_id"), str) and
                   re.fullmatch(r"r004-q02-[A-Za-z0-9_-]{1,65}", reservation["run_id"]),
                   "INVALID_HISTORY_RECORD", "run identity")
            run = root / reservation["run_id"]
            guard = a.attempt_path(root, "formal_execution_started", number, case_id="Q02", case_revision=case_revision)
            manifest, sha, files, report, roles, source = _record(run, path, guard, rows, case_revision=case_revision)
            contract, reason = "PASS", None
            try:
                _bundle_policy(manifest, report, roles)
            except a.AttemptStateError as exc:
                a.need(not report["ENGINEERING_PASS"] and exc.code == "QUALIFICATION_CONTRACT_MISMATCH",
                       "INVALID_HISTORY_RECORD", "PASS with invalid bundle")
                contract, reason = "FAIL", str(exc)
            a.need(report["ENGINEERING_PASS"] or "FAIL" in report["gates"].values() or contract == "FAIL",
                   "INVALID_HISTORY_RECORD", "failure evidence absent")
            real = report["REAL_SW_RUN_COUNT"]
            rows[number] = {"case_id":"Q02", "case_revision":case_revision, "attempt_number":number, "run_id":reservation["run_id"],
                "evidence_kind":report.get("evidence_kind"), "backend":source["backend"],
                "status":report["status"], "manifest_sha256":sha, "reservation_digest":a.digest(path), "legacy":False,
                "formal_invocation_count":1, "real_sw_run_count":real,
                "HISTORICAL_RECORD_STATE":"SEALED_PASS_ATTEMPT" if report["ENGINEERING_PASS"] else "SEALED_FAIL_ATTEMPT",
                "HISTORICAL_RECORD_INTEGRITY":"PASS", "HISTORICAL_OUTCOME_CONSISTENCY":"PASS",
                "HISTORICAL_QUALIFICATION_OUTCOME":"PASS" if report["ENGINEERING_PASS"] else "FAIL" if real else "PRE_LAUNCH_FAIL",
                "HISTORICAL_STAGE":"REAL_RUN_PASS" if report["ENGINEERING_PASS"] else "REAL_RUN_FAIL" if real else "PRE_LAUNCH_FAIL",
                "HISTORICAL_BUNDLE_CONTRACT_VALIDITY":contract, "historical_bundle_contract_failure":reason,
                "historical_failure_reason":report.get("failure_reason")}
            allowed.update(files | {path.resolve(), guard.resolve()})
    guards = root / "formal_execution_started"
    if guards.exists():
        a.need(guards.is_dir() and not guards.is_symlink() and not guards.is_junction() and
               {p.resolve() for p in guards.iterdir()} <= allowed, "INVALID_HISTORY_RECORD", "orphan guard")
    return rows, allowed
