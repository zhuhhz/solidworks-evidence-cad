"""R003-only bridge from an admitted reasoning candidate to frozen B010 planning.

Planning and fake status composition never constitute CAD engineering evidence.
"""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from typing import Any, Mapping

from experiments.three_view_reconstruction.schemas.feature_graph import FeatureGraph
from experiments.three_view_reconstruction.schemas.modeling_plan import ModelingPlan
from solidworks_main_skill.b010_planning import build_modeling_plan_candidate


PLANNING_STATUSES = frozenset({"NOT_RUN", "BLOCKED", "REJECTED", "ACCEPTED", "ERROR"})
DOWNSTREAM_CHECKS = (
    "BODY_COUNT", "BREP_GEOMETRY", "FEATURE_OWNERSHIP", "REBUILD",
    "SAVE_REOPEN", "DRAWING_ROUNDTRIP",
)
DOWNSTREAM_STATUSES = frozenset({"NOT_RUN", "PASS", "FAIL"})


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: Any) -> str:
    return sha256(_canonical(value).encode("utf-8")).hexdigest()


def _reasoning_snapshot(report: Mapping[str, Any]) -> dict[str, Any]:
    names = (
        "input_domain_status", "artifact_validation_status", "shape_status",
        "history_status", "canonical_status", "selected_candidate", "outcome",
        "failure_reason", "generation_truth_reads",
    )
    return {name: deepcopy(report.get(name)) for name in names}


def _blocked_outcome(report: Mapping[str, Any], graph: FeatureGraph | None) -> str | None:
    """Classify only the R003-to-B010 admission boundary, not geometry itself."""
    if report.get("input_domain_status") != "APPLICABLE":
        return "BLOCKED_SCOPE"
    reviewed = report.get("reviewed_inputs")
    if (report.get("artifact_validation_status") != "VALID"
            or not isinstance(reviewed, Mapping)
            or reviewed.get("confirmation_decision") != "CONFIRMED"):
        return "BLOCKED_ARTIFACT"
    if report.get("shape_status") != "RESOLVED":
        return "BLOCKED_GEOMETRY"
    if report.get("history_status") != "NON_IDENTIFIABLE":
        return "BLOCKED_HISTORY"
    if (report.get("canonical_status") != "SELECTED"
            or report.get("selected_candidate") != "C_ADD"):
        return "BLOCKED_CANONICAL"
    if (not isinstance(graph, FeatureGraph) or graph.status != "NOT_VALIDATED"
            or report.get("feature_graph") != graph.to_dict()
            or report.get("status") != "INFERRED"):
        return "BLOCKED_CANONICAL"
    checks = report.get("checks")
    if (not isinstance(checks, Mapping)
            or checks.get("unresolved_reference_count") != 0
            or checks.get("unconsumed_evidence_count") != 0):
        return "BLOCKED_ARTIFACT"
    if (type(report.get("generation_truth_reads")) is not int
            or report["generation_truth_reads"] != 0
            or type(checks.get("generation_truth_reads")) is not int
            or checks["generation_truth_reads"] != 0):
        return "BLOCKED_ARTIFACT"
    return None


def _rejection_code(message: str) -> str:
    """A closed diagnostic taxonomy; planner exception text is never a state."""
    lowered = message.lower()
    for token, code in (
        ("evidence", "EVIDENCE"), ("depend", "DEPENDENCY"),
        ("feature_id", "IDENTITY"), ("feature ids", "IDENTITY"),
        ("feature_count", "FEATURE_SET"), ("exactly", "FEATURE_SET"),
        ("feature_nodes", "GRAPH_CONSISTENCY"), ("typed featuregraph", "GRAPH_CONSISTENCY"),
        ("confirmation", "REVIEW"), ("candidate", "CANDIDATE"),
        ("boss", "GEOMETRY"), ("hole", "GEOMETRY"),
        ("width", "GEOMETRY"), ("height", "GEOMETRY"), ("depth", "GEOMETRY"),
        ("diameter", "GEOMETRY"), ("center", "GEOMETRY"),
    ):
        if token in lowered:
            return "B010_REJECTED_" + code
    return "B010_REJECTED_CONTRACT"


def _fake_checks(values: Mapping[str, str] | None) -> tuple[dict[str, str], str]:
    checks = {name: "NOT_RUN" for name in DOWNSTREAM_CHECKS}
    if values is not None:
        if not isinstance(values, Mapping) or set(values) - set(DOWNSTREAM_CHECKS):
            raise ValueError("fake downstream checks contain an unsupported key")
        for name, status in values.items():
            if status not in DOWNSTREAM_STATUSES:
                raise ValueError("fake downstream status must be NOT_RUN, PASS or FAIL")
            checks[name] = status
    overall = "FAIL" if "FAIL" in checks.values() else (
        "PASS" if all(value == "PASS" for value in checks.values()) else "NOT_RUN"
    )
    return checks, overall


def integrate_r003_with_b010(
    feature_graph: FeatureGraph | None,
    reasoning_report: Mapping[str, Any],
    *,
    run_planning: bool = True,
    fake_downstream_checks: Mapping[str, str] | None = None,
) -> tuple[ModelingPlan | None, dict[str, Any]]:
    """Plan an already-reviewed R003 result without revisiting source or CAD execution.

    The optional fake checks exercise only status orchestration. They never set
    real engineering validation or alter the source-backed reasoning snapshot.
    """
    if not isinstance(reasoning_report, Mapping):
        reasoning_report = {}
    report = dict(reasoning_report)
    reasoning_snapshot = _reasoning_snapshot(report)
    result: dict[str, Any] = {
        "reasoning_status_snapshot": reasoning_snapshot,
        "canonical_candidate": reasoning_snapshot["selected_candidate"],
        "feature_graph_present": isinstance(feature_graph, FeatureGraph),
        "planning_status": "NOT_RUN",
        "outcome": "NOT_RUN",
        "b010_called": False,
        "b010_result_status": None,
        "b010_failure_reason": None,
        "b010_diagnostic": None,
        "modeling_plan_present": False,
        "engineering_validation_status": "NOT_RUN",
        "engineering_validation_not_run_reason": "REAL_SOLIDWORKS_NOT_INVOKED",
        "real_solidworks_validation": "NOT_RUN",
        "r003_engineering_pass": False,
        "generation_truth_reads": report.get("generation_truth_reads"),
        "b010_generation_truth_reads": 0,
        "ready_for_engineering_validation": False,
        "fake_downstream_checks": {name: "NOT_RUN" for name in DOWNSTREAM_CHECKS},
        "fake_downstream_status": "NOT_RUN",
        "source_reasoning_report_digest": None,
        "feature_graph_digest": None,
        "modeling_plan_digest": None,
        "input_feature_graph_unchanged": None,
        "input_reasoning_report_unchanged": None,
    }
    try:
        before_report = _canonical(reasoning_report)
        before_graph = _canonical(feature_graph.to_dict()) if isinstance(feature_graph, FeatureGraph) else None
        result["source_reasoning_report_digest"] = _digest(reasoning_report)
        result["feature_graph_digest"] = _digest(feature_graph.to_dict()) if before_graph is not None else None
    except (TypeError, ValueError, OverflowError) as exc:
        result.update(planning_status="ERROR", outcome="B010_ERROR",
                      b010_diagnostic=f"Invalid integration input: {exc}")
        return None, result

    def unchanged() -> bool:
        try:
            result["input_reasoning_report_unchanged"] = _canonical(reasoning_report) == before_report
            result["input_feature_graph_unchanged"] = (
                (_canonical(feature_graph.to_dict()) if isinstance(feature_graph, FeatureGraph) else None)
                == before_graph
            )
            return result["input_reasoning_report_unchanged"] and result["input_feature_graph_unchanged"]
        except (TypeError, ValueError, OverflowError):
            result["input_reasoning_report_unchanged"] = False
            result["input_feature_graph_unchanged"] = False
            return False

    if not run_planning:
        unchanged()
        return None, result
    try:
        blocked = _blocked_outcome(report, feature_graph)
    except (TypeError, ValueError, AttributeError, KeyError) as exc:
        result.update(planning_status="ERROR", outcome="B010_ERROR",
                      b010_diagnostic=f"Invalid integration guard input: {exc}")
        unchanged()
        return None, result
    if blocked is not None:
        result.update(planning_status="BLOCKED", outcome=blocked)
        unchanged()
        return None, result

    # The frozen planner receives its own candidate snapshot, never a mutable
    # reference to the upstream reasoning report or typed graph.
    candidate = deepcopy(report)
    result["b010_called"] = True
    try:
        normalized_graph, plan, planning_report = build_modeling_plan_candidate(candidate)
    except ValueError as exc:
        result.update(planning_status="REJECTED", outcome="B010_REJECTED",
                      b010_result_status="REJECTED",
                      b010_failure_reason=_rejection_code(str(exc)),
                      b010_diagnostic={"exception_type": "ValueError", "message": str(exc)})
        if not unchanged():
            result.update(planning_status="ERROR", outcome="B010_ERROR",
                          b010_failure_reason=None,
                          b010_diagnostic="integration input changed during B010 rejection")
        return None, result
    except Exception as exc:
        result.update(planning_status="ERROR", outcome="B010_ERROR",
                      b010_diagnostic={"exception_type": type(exc).__name__, "message": str(exc)})
        unchanged()
        return None, result

    try:
        valid = (
            isinstance(normalized_graph, FeatureGraph)
            and isinstance(plan, ModelingPlan)
            and isinstance(planning_report, Mapping)
            and planning_report.get("status") == "VALID"
            and isinstance(planning_report.get("validation"), Mapping)
            and planning_report["validation"].get("status") == "PASS"
            and normalized_graph.to_dict() == feature_graph.to_dict()
            and planning_report.get("modeling_plan") == plan.to_dict()
            and bool(plan.operations)
        )
        if not valid or not unchanged():
            raise ValueError("B010 returned inconsistent graph, plan, status or input state")
        fake_checks, fake_status = _fake_checks(fake_downstream_checks)
        plan_digest = _digest(plan.to_dict())
    except (TypeError, ValueError, AttributeError, KeyError, OverflowError) as exc:
        result.update(planning_status="ERROR", outcome="B010_ERROR",
                      b010_diagnostic={"exception_type": type(exc).__name__, "message": str(exc)})
        return None, result

    result.update(
        planning_status="ACCEPTED",
        outcome=("FAKE_ORCHESTRATION_FAIL" if fake_status == "FAIL" else
                 "FAKE_ORCHESTRATION_PASS" if fake_status == "PASS" else "PLAN_READY"),
        b010_result_status="VALID",
        modeling_plan_present=True,
        modeling_plan_digest=plan_digest,
        ready_for_engineering_validation=True,
        fake_downstream_checks=fake_checks,
        fake_downstream_status=fake_status,
    )
    return plan, result


__all__ = ["DOWNSTREAM_CHECKS", "DOWNSTREAM_STATUSES", "PLANNING_STATUSES",
           "integrate_r003_with_b010"]
