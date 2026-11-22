"""Frozen Q01 attempt artifact contract. No CAD or evaluator input is read here."""
from __future__ import annotations

from pathlib import Path

from experiments.three_view_reconstruction.r004_qualification_attempt import (
    AttemptStateError, digest, document_digest, need, read_document,
)


VERSION = "R004-Q01-A2-V1"
MANIFEST = "manifest.json"
SEAL = "manifest.sha256"
CATEGORIES = tuple("ABCDEFGHIJKLMN")
STATES = {"PRESENT", "NOT_REACHED", "NOT_APPLICABLE"}
REQUIRED_ROLES = {"AUTHORIZATION", "RESERVATION_COPY", "ENVIRONMENT", "SOURCE_PROVENANCE",
                  "EXECUTION_PLAN", "EXECUTION_LEDGER", "CLEANUP", "FINAL_REPORT"}
ROLES = {
    "authorization.json": ("AUTHORIZATION", "A"),
    "reservation.json": ("RESERVATION_COPY", "A"),
    "source_provenance.json": ("SOURCE_PROVENANCE", "C"),
    "environment.json": ("ENVIRONMENT", "B"),
    "preflight.json": ("PREFLIGHT", "B"),
    "execution_plan.json": ("EXECUTION_PLAN", "D"),
    "execution_ledger.json": ("EXECUTION_LEDGER", "E"),
    "native_evidence.json": ("NATIVE_EVIDENCE", "I"),
    "brep_validation.json": ("BREP_VALIDATION", "H"),
    "ownership_validation.json": ("OWNERSHIP_VALIDATION", "I"),
    "drawing_initial_native_evidence.json": ("DRAWING_INITIAL", "J"),
    "drawing_native_evidence.json": ("DRAWING_NATIVE", "J"),
    "drawing_validation.json": ("DRAWING_VALIDATION", "J"),
    "truth_isolation.json": ("TRUTH_AUDIT", "K"),
    "cleanup.json": ("CLEANUP", "L"),
    "final_report.json": ("FINAL_REPORT", "M"),
    "part.sldprt": ("NATIVE_PART", "G"),
    "drawing.slddrw": ("NATIVE_DRAWING", "J"),
}
ROLE_CATEGORIES = {role: category for role, category in ROLES.values()}
ROLE_CATEGORIES["RUNTIME_CACHE_MEMBER"] = "K"


def artifact_role(relative_path):
    # Canonical names on Windows may retain native extension capitalization.
    # Match the entire controlled relative name, never an arbitrary extension.
    canonical = relative_path.casefold()
    if canonical in ROLES:
        return ROLES[canonical]
    if relative_path.startswith("attempt-") and "-runtime_cache/" in relative_path:
        return "RUNTIME_CACHE_MEMBER", "K"
    # Preserve unknown bytes in the sealed list, then reject the bundle.
    return "UNCLASSIFIED", "K"


def contract_preflight(writer_roles=ROLES, *, version=VERSION):
    need(version == VERSION and {role for role, _ in writer_roles.values()} >= REQUIRED_ROLES,
         "QUALIFICATION_CONTRACT_MISMATCH", "writer capability")
    need(MANIFEST == "manifest.json" and SEAL == "manifest.sha256",
         "QUALIFICATION_CONTRACT_MISMATCH", "canonical manifest")
    return "PASS"


def category_states(report, roles):
    gates = report["gates"]
    reached = lambda name: gates.get(name, "NOT_REACHED") != "NOT_REACHED"
    present = lambda name: name in roles
    state = lambda yes: "PRESENT" if yes else "NOT_REACHED"
    return {
        "A": "PRESENT", "B": state(present("ENVIRONMENT")),
        "C": state(present("SOURCE_PROVENANCE")), "D": state(present("EXECUTION_PLAN")),
        "E": state(reached("canonical_operations")), "F": state(reached("SESSION_BIND")),
        "G": state(reached("save") or reached("reopen")), "H": state(reached("brep")),
        "I": state(reached("ownership")), "J": state(reached("drawing_creation")),
        "K": state(present("TRUTH_AUDIT")), "L": state(present("CLEANUP")),
        "M": state(present("FINAL_REPORT")), "N": "PRESENT",
    }


def check_gate_transitions(report):
    order = [name for name in report["gate_order"] if name not in {"cleanup", "manifest"}]
    gates = report["gates"]
    unreachable = False
    for name in order:
        status = gates.get(name)
        need(status in {"PASS", "FAIL", "NOT_REACHED", "NOT_APPLICABLE"},
             "QUALIFICATION_CONTRACT_MISMATCH", f"invalid gate state: {name}")
        need(not (unreachable and status in {"PASS", "FAIL"}),
             "QUALIFICATION_CONTRACT_MISMATCH", "invalid NOT_REACHED transition")
        unreachable |= status in {"FAIL", "NOT_REACHED"}


def verify_bundle(folder, reservation_path, guard_path):
    """Check a sealed V1 bundle, with attempt records kept in their own namespace."""
    folder = Path(folder).resolve(strict=True)
    manifest_path, seal_path = folder / MANIFEST, folder / SEAL
    need(manifest_path.is_file() and seal_path.is_file() and
         seal_path.read_text(encoding="ascii").split() == [digest(manifest_path), MANIFEST],
         "QUALIFICATION_CONTRACT_MISMATCH", "manifest detached seal")
    manifest = read_document(manifest_path)
    reservation, guard = read_document(reservation_path), read_document(guard_path)
    need(manifest.get("qualification_contract_version") == VERSION and
         reservation.get("qualification_contract_version") == VERSION and
         guard.get("qualification_contract_version") == VERSION,
         "QUALIFICATION_CONTRACT_MISMATCH", "contract version")
    for key in ("case_id", "attempt_number", "run_id"):
        need(manifest.get(key) == reservation.get(key) == guard.get(key),
             "QUALIFICATION_CONTRACT_MISMATCH", key)
    need(type(reservation.get("attempt_number")) is int and reservation["attempt_number"] >= 2,
         "QUALIFICATION_CONTRACT_MISMATCH", "attempt number")
    need(manifest.get("HEAD") == reservation.get("current_head") == guard.get("current_head") and
         manifest.get("reservation_digest") == guard.get("reservation_digest") == digest(reservation_path) and
         manifest.get("authorization_digest") == reservation.get("authorization_digest") == guard.get("authorization_digest"),
         "QUALIFICATION_CONTRACT_MISMATCH", "attempt binding")
    need(manifest.get("reservation_digest") != manifest.get("authorization_digest"),
         "QUALIFICATION_CONTRACT_MISMATCH", "digest roles")
    for field in ("c2a_evidence_manifest_sha256", "backend_provenance_digest",
                  "runtime_support_provenance_digest"):
        need(isinstance(reservation.get(field), str) and len(reservation[field]) == 64 and
             manifest.get(field) == reservation[field],
             "QUALIFICATION_CONTRACT_MISMATCH", field)
    need(reservation.get("MAX_FORMAL_INVOCATIONS") == reservation.get("MAX_REAL_SW_RUNS") == 1 and
         reservation.get("max_formal_invocations_for_attempt") == 1 and
         reservation.get("max_real_sw_runs_for_attempt") == 1 and
         guard.get("created_at") and reservation.get("created_at"),
         "QUALIFICATION_CONTRACT_MISMATCH", "attempt budget or timestamp")
    need(reservation.get("parent_run_id") == reservation.get("parent_failure_run_id") and
         reservation.get("parent_manifest_sha256") == reservation.get("parent_failure_manifest_sha256") and
         reservation.get("attempt_governance_remediation_commit") == reservation.get("current_head"),
         "QUALIFICATION_CONTRACT_MISMATCH", "lineage")
    entries = manifest.get("artifacts")
    need(isinstance(entries, list), "QUALIFICATION_CONTRACT_MISMATCH", "artifact list")
    observed, roles = set(), set()
    for entry in entries:
        rel = entry.get("relative_path")
        need(isinstance(rel, str) and rel and "\\" not in rel and
             not Path(rel).is_absolute() and ".." not in Path(rel).parts,
             "QUALIFICATION_CONTRACT_MISMATCH", "artifact path")
        path = folder / rel
        need(path.is_file() and not path.is_symlink() and not path.is_junction() and
             path.resolve().is_relative_to(folder) and rel not in observed and
             path.stat().st_size == entry.get("size") and digest(path) == entry.get("sha256"),
             "QUALIFICATION_CONTRACT_MISMATCH", f"artifact hash/size: {rel}")
        need(entry.get("role") in ROLE_CATEGORIES and
             entry.get("semantic_category") == ROLE_CATEGORIES.get(entry.get("role")),
             "QUALIFICATION_CONTRACT_MISMATCH", f"artifact role: {rel}")
        observed.add(rel); roles.add(entry["role"])
    actual = {p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file()}
    need(actual == observed | {MANIFEST, SEAL} and roles >= REQUIRED_ROLES,
         "QUALIFICATION_CONTRACT_MISMATCH", "unexpected artifact or missing role")
    auth = next(read_document(folder / e["relative_path"]) for e in entries if e["role"] == "AUTHORIZATION")
    source = next(read_document(folder / e["relative_path"]) for e in entries if e["role"] == "SOURCE_PROVENANCE")
    report = next(read_document(folder / e["relative_path"]) for e in entries if e["role"] == "FINAL_REPORT")
    need(document_digest(auth) == reservation["authorization_digest"] and
         auth.get("case_id") == reservation["case_id"] and
         auth.get("attempt_number") == reservation["attempt_number"] and
         auth.get("authorized_head") == reservation["current_head"] and
         auth.get("parent_run_id") == reservation["parent_failure_run_id"] and
         auth.get("parent_manifest_sha256") == reservation["parent_failure_manifest_sha256"] and
         source.get("c2a_evidence_manifest_sha256") == reservation["c2a_evidence_manifest_sha256"] and
         source.get("backend_provenance_digest") == reservation["backend_provenance_digest"] and
         source.get("runtime_support_provenance_digest") == reservation["runtime_support_provenance_digest"] and
         source.get("runtime_remediation_commit") == reservation["runtime_remediation_commit"] and
         source.get("attempt_governance_remediation_commit") == reservation["current_head"] and
         document_digest({"backend":source.get("backend"),"files":source.get("files")}) ==
             reservation["backend_provenance_digest"] and
         document_digest(source.get("runtime_support")) ==
             reservation["runtime_support_provenance_digest"],
         "QUALIFICATION_CONTRACT_MISMATCH", "provenance chain")
    check_gate_transitions(report)
    if report.get("evidence_kind") != "SYNTHETIC":
        need(report["gates"].get("save") != "PASS" or "NATIVE_PART" in roles,
             "QUALIFICATION_CONTRACT_MISMATCH", "saved part absent")
        need(report["gates"].get("drawing_save_reopen") != "PASS" or "NATIVE_DRAWING" in roles,
             "QUALIFICATION_CONTRACT_MISMATCH", "saved drawing absent")
    categories = manifest.get("semantic_categories")
    need(isinstance(categories, dict) and set(categories) == set(CATEGORIES) and
         set(categories.values()) <= STATES and
         categories == category_states(report, roles),
         "QUALIFICATION_CONTRACT_MISMATCH", "semantic category or NOT_REACHED state")
    need(manifest.get("final_state") == report.get("status") and
         manifest.get("attempt_identity") == report.get("attempt_identity"),
         "QUALIFICATION_CONTRACT_MISMATCH", "final state")
    return "PASS"
