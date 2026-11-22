"""Q01 qualification harness. Default CLI is read-only preparation, never live.

Execution input travels through R004-A/B/C1. Sealed evidence is evaluator-only;
it is not passed to the adapter. A formal failure permanently consumes its run.
"""
from __future__ import annotations

import argparse
import ast
import base64
from datetime import datetime, timezone
from hashlib import sha256
from importlib import metadata
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys

REPOSITORY=Path(__file__).resolve().parents[2]
if str(REPOSITORY/"src") not in sys.path:
    sys.path.insert(0,str(REPOSITORY/"src"))

from solidworks_main_skill.reasoning.r004_contract import (
    ReviewedCompositionInput,EvidenceBinding,PrimitiveObservation,RegionObservation,GeometricRelation,SupportRelation,
)
from solidworks_main_skill.reasoning.r004_composition_reasoner import reason_composition
from solidworks_main_skill.reasoning.r004_planning import plan_composition
from solidworks_main_skill.backends.solidworks_automation.r004_execution_binding import _preflight
from solidworks_main_skill.backends.solidworks_automation.r004_execution_adapter import R004ExecutionAdapter, from_native, require, _vector
from solidworks_main_skill.backends.solidworks_automation.r003_native_adapter import (
    launch_r003_owned_com,R003OwnedNativeSession,_load_reviewed_skill_modules,_snapshot_solidworks_processes,
)
from solidworks_main_skill.backends.solidworks_automation.r003_native_collectors import normalize_r003_hlv_hlr
from solidworks_main_skill.backends.solidworks_automation.r003_real_validation import validate_body_count
from experiments.three_view_reconstruction.validation.primitive_matcher import match, support_difference
from solidworks_main_skill.backends.solidworks_automation.r004_drawing_comparison import (
    MODEL_PROJECTED_PRIMITIVE, SHEET_ANNOTATION, normalize_primitive, hidden_linear_spline,
)
from experiments.three_view_reconstruction.run_r003_v2_real_validation import GenerationReadAudit
from experiments.three_view_reconstruction.r004_runtime_support import RuntimeReadAudit, runtime_provenance
from experiments.three_view_reconstruction.r004_qualification_attempt import (
    attempt_cache_path, create_execution_guard, document_digest, history_view, read_document,
    reserve_attempt, validate_authorization, validate_worktree_status, verify_artifact_tree,
    sealed_manifest,
)
from experiments.three_view_reconstruction.r004_qualification_contract import (
    VERSION as QUALIFICATION_CONTRACT_VERSION, artifact_role, category_states,
    contract_preflight, verify_bundle,
)


CONFIG=REPOSITORY/"qualification/r004/q01_harness_config.json"
GATE_ORDER=("QUALIFICATION_ATTEMPT_PREFLIGHT","evidence_seal","backend_provenance","preexisting_session","CONTROLLED_LAUNCH_PREFLIGHT","CONTROLLED_LAUNCH","SESSION_BIND","pid_ownership",
    "document_creation","canonical_operations","operation_checks","rebuild","save","close","reopen",
    "reopened_rebuild","body_count","brep","persistent_references","ownership","drawing_creation",
    "physical_views","drawing_comparison","drawing_save_reopen","truth_isolation","cleanup","manifest")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def digest(path):
    return sha256(Path(path).read_bytes()).hexdigest()


def write_exclusive(path,document):
    with Path(path).open("x",encoding="utf-8",newline="\n") as file:
        json.dump(document,file,ensure_ascii=False,indent=2,allow_nan=False)
        file.write("\n")


def load_evidence(folder, *, pinned_manifest_sha256):
    folder=Path(folder).resolve(strict=True)
    manifest_path=folder/"evidence_manifest.json"
    seal=(folder/"evidence_manifest.sha256").read_text(encoding="ascii").splitlines()[0].split()
    require(len(seal)==2 and seal[1]==manifest_path.name and seal[0]==digest(manifest_path)==pinned_manifest_sha256,
            "qualification manifest detached/pinned seal failed")
    manifest=read_json(manifest_path)
    require(manifest["case_id"]=="Q01","only Q01 is authorized for this runner")
    documents={}
    names=set()
    for entry in manifest["artifacts"]:
        name=entry["relative_path"]
        require(isinstance(name,str) and Path(name).name==name and name not in names,"invalid/duplicate evidence path")
        path=(folder/name).resolve(strict=True)
        require(path.parent==folder and path.stat().st_size==entry["size"] and digest(path)==entry["sha256"],
                "qualification artifact bytes changed")
        require(entry["verification_state"]=="VERIFIED_REVIEWED_EVIDENCE","unreviewed qualification artifact")
        names.add(name)
        if path.suffix==".json":
            doc=read_json(path)
            require(doc["provenance"]["source_type"]=="INDEPENDENT_REVIEWED_GEOMETRY"
                    and doc["provenance"]["authorization_state"]=="AUTHORIZED_FOR_R004C2_QUALIFICATION",
                    "qualification source or authorization invalid")
            documents[name]=doc
    require({"reviewed_geometry.json","projection_front.json","projection_top.json","projection_left.json",
             "feature_region_correspondence.json","review_report.json","README.md"}==names,"qualification artifact set incomplete")
    require(documents["projection_left.json"]["view_identity"]=="PHYSICAL_LEFT","physical LEFT evidence required")
    return {"manifest_sha256":pinned_manifest_sha256,"documents":documents}


def verify_backend(config):
    root=Path(config["backend_root"]).resolve(strict=True)
    records=[]
    for pin in config["source_pins"]:
        path=(root/pin["relative_path"]).resolve(strict=True)
        require(path.is_relative_to(root) and digest(path)==pin["sha256"],"reviewed backend source hash drift")
        if pin["selected_symbols"]:
            tree=ast.parse(path.read_text(encoding="utf-8"))
            symbols={node.name for node in tree.body if isinstance(node,(ast.FunctionDef,ast.ClassDef))}
            require(set(pin["selected_symbols"])<=symbols,"reviewed backend symbol missing")
        records.append({**pin,"absolute_environment_path":str(path),"license":"MIT"})
    require(any(p["relative_path"]=="LICENSE" for p in records),"license pin required")
    return {"status":"PASS","backend_root":str(root),"files":records,
            "backend":"HYBRID_EXTERNAL_EXECUTION_NATIVE_VALIDATION"}


def dependency_environment():
    # Resolve installed distributions without importing COM or installing anything.
    rows=[]
    for name in ("pywin32","comtypes"):
        distribution=metadata.distribution(name)
        rows.append({"distribution":name,"version":distribution.version,
                     "installed_root":str(Path(distribution.locate_file("")).resolve(strict=True))})
    return rows


def load_execution_input(path, *, forbidden_evidence_root):
    path=Path(path).resolve(strict=True)
    require(not path.is_relative_to(Path(forbidden_evidence_root).resolve()),"evaluator files cannot be execution input")
    raw=read_json(path)
    require(raw["source_type"]=="REVIEWED_EXECUTION_INPUT" and raw["confirmation_state"]=="CONFIRMED",
            "execution input must be independently confirmed")
    values=dict(raw["reviewed_input"])
    for binding in values["evidence"]:
        payload=raw["observations"][binding["evidence_id"]]
        require(sha256(json.dumps(payload,sort_keys=True,separators=(",",":")).encode()).hexdigest()==binding["content_sha256"],
                "reviewed execution observation digest mismatch")
    for region in values["regions"]:
        payloads=[raw["observations"][eid] for eid in region["evidence_ids"]]
        require(payloads and all(p["review_state"]=="CONFIRMED" and all(p[key]==region[key]
                for key in ("region_id","shape_kind","bounds_mm","material_state")) for p in payloads),
                "reviewed region differs from its bound observation")
    for relation in values["support_relations"]:
        require(all(raw["observations"][eid]["host_region_id"] in relation["candidate_host_region_ids"]
                    for eid in relation["evidence_ids"]),"reviewed host differs from bound observation")
    conversions={"evidence":EvidenceBinding,"primitives":PrimitiveObservation,"regions":RegionObservation,
                 "geometric_relations":GeometricRelation,"support_relations":SupportRelation}
    tuple_fields={"evidence_ids","primitive_ids","bounds_mm","projection_views","candidate_host_region_ids"}
    for key,cls in conversions.items():
        values[key]=tuple(cls(**{k:tuple(v) if k in tuple_fields else v for k,v in row.items()}) for row in values[key])
    values["transport_metadata"]=tuple(tuple(row) for row in values.get("transport_metadata",()))
    reviewed=ReviewedCompositionInput(**values)
    composition=reason_composition(reviewed)
    planned=plan_composition(composition)
    _preflight(planned,composition)
    types=[row.feature_type for row in composition.feature_hypotheses]
    require(len(types)==4 and types.count("BASE_BLOCK")==1 and types.count("BOSS")==2
            and types.count("THROUGH_HOLE")==1,"Q01 qualification contract requires sibling additive plus hosted circular cut")
    hypotheses={row.feature_id:row for row in composition.feature_hypotheses}
    for row in planned.operation_requirements:
        if hypotheses[row.datum.feature_id].feature_type=="BOSS":
            require(hypotheses[row.datum.host_feature].feature_type=="BASE_BLOCK","Q01 bosses must be siblings on base")
        if hypotheses[row.datum.feature_id].feature_type=="THROUGH_HOLE":
            require(hypotheses[row.datum.host_feature].feature_type=="BOSS","Q01 hole must be boss-hosted")
    return reviewed,composition,planned


def prepare(config_path=CONFIG, *, execution_input=None, evidence_directory=None):
    config=read_json(config_path)
    folder=Path(evidence_directory or REPOSITORY/config["evidence_directory"]).resolve(strict=True)
    evidence=load_evidence(folder,pinned_manifest_sha256=config["evidence_manifest_sha256"])
    provenance=verify_backend(config)
    provenance["runtime_dependencies"]=dependency_environment()
    provenance["runtime_support"]=runtime_provenance(config)
    input_path=Path(execution_input or REPOSITORY/config["execution_input"])
    reviewed,composition,planned=load_execution_input(input_path,forbidden_evidence_root=folder.parent)
    return config,folder,input_path,reviewed,composition,planned,evidence,provenance


def owned_adapter_factory(config,run_id, *, live_authorized=False, acquisition=None, read_audit=None):
    require(live_authorized is True and os.environ.get("RUN_REAL_SOLIDWORKS")=="1","controlled launcher not authorized")
    require(not _snapshot_solidworks_processes(),"preexisting SolidWorks instance appeared before launch")
    verify_backend(config)
    require(read_audit is not None and acquisition is not None,"worker runtime boundary required")
    read_audit.configure_pywin32()
    connect_api,part_api=_load_reviewed_skill_modules(Path(config["backend_root"])/"scripts")
    verify_backend(config)  # Reject source drift between resolution and module import.
    acquisition.update(launcher_called=True,resource_acquisition_started=True,stage="CONTROLLED_LAUNCH")
    sw,identity,proof=launch_r003_owned_com(Path(config["executable_path"]),run_id=run_id,
                                         launch_timeout_s=config["launch_timeout_s"])
    acquisition.update(stage="SESSION_BIND",launch_returned=True,ownership_evidence=proof)
    def probe(pid):
        rows=[row for row in _snapshot_solidworks_processes() if row["pid"]==pid]
        if len(rows)!=1:
            return {}
        return {"pid":pid,"create_time":str(rows[0]["creation_time"]),"source":"OS_SNAPSHOT_AND_PID_ROT"}
    adapter=R004ExecutionAdapter(session_factory=lambda:R003OwnedNativeSession(sw,connect_api),identity=identity,
        identity_probe=probe,run_id=run_id,expected_major_version=config["solidworks_year"],part_api=part_api,
        exit_timeout_s=config["exit_timeout_s"])
    adapter.live_authorized=True
    adapter.ownership_evidence=proof
    return adapter


def _semantic_reference(record, kind):
    require(isinstance(record, dict) and record.get("reference_type") == kind
            and record.get("source") == "IModelDocExtension.GetPersistReference3",
            "typed native persistent reference missing")
    try:
        raw = base64.b64decode(record.get("encoded", ""), validate=True)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid native persistent reference") from exc
    require(bool(raw) and base64.b64encode(raw).decode("ascii")==record["encoded"],
            "empty/noncanonical native persistent reference")
    return record["encoded"]


def _semantic_native_owner(face, native_features, source_regions, semantic_by_source):
    fid = face.get("native_owner_feature_id")
    require(fid in native_features and fid in source_regions,
            "UNKNOWN/UNATTRIBUTED native owner")
    feature = native_features[fid]
    token = _semantic_reference(feature.get("reference"), "FEATURE")
    require(_semantic_reference(face.get("owner_reference"), "FEATURE") == token
            and feature.get("persistent_reference_resolved") is True
            and face.get("persistent_reference_resolved") is True,
            "unresolved/misattributed native owner reference")
    require(face.get("ownership_source") == "IFace2.GetFeature+IModelDocExtension.GetPersistReference3",
            "native face-feature provenance missing")
    require(source_regions[fid] in semantic_by_source, "dangling native source correspondence")
    return semantic_by_source[source_regions[fid]]


def _support_floor_contract(parent, child, cut, semantic_region):
    # This is the approved one-level support pattern, not recursive CSG.
    require(parent["feature_type"] == "BASE_BLOCK" and child["feature_type"] == "BOSS"
            and cut["feature_type"] == "THROUGH_HOLE", "unsupported exposed support pattern")
    require(child["host_semantic_id"] == parent["semantic_feature_id"]
            and cut["host_semantic_id"] == child["semantic_feature_id"], "support floor host mismatch")
    p, b, h = (f["bounds_mm"] for f in (parent, child, cut))
    require(all(_vector(v, 6) for v in (p, b, h)), "support floor bounds unavailable")
    require(abs(p[5]-b[4]) <= 1e-6 and abs(h[4]-p[5]) <= 1e-6
            and abs(h[5]-b[5]) <= 1e-6 and cut.get("full_span") is True,
            "terminating cut does not reach exactly the parent support plane")
    require(all(b[i] < h[i] < h[i+1] < b[i+1] for i in (0, 2)),
            "support floor cut footprint not strictly inside child")
    radius = cut["dimensions_mm"]["radius_mm"]
    require(type(radius) in (int, float) and math.isfinite(radius) and radius > 0
            and abs(h[1]-h[0]-2*radius) <= 1e-6
            and abs(h[3]-h[2]-2*radius) <= 1e-6,
            "support floor terminating footprint is not circular")
    center = [(h[i]+h[i+1])/2 for i in (0, 2)]
    require(_vector(cut["center_mm"], 3)
            and all(abs(a-b) <= 1e-6 for a,b in zip(center, cut["center_mm"][:2])),
            "support floor cut center inconsistent")
    return {"semantic_region": semantic_region, "surface_role": "EXPOSED_PARENT_SUPPORT_FLOOR",
            "semantic_owner": parent["semantic_feature_id"], "host_feature": child["semantic_feature_id"],
            "terminating_cut": cut["semantic_feature_id"], "plane_z_mm": p[5],
            "center_xy_mm": center, "radius_mm": radius,
            "allowed_native_provenance": [parent["semantic_feature_id"], cut["semantic_feature_id"]]}


def _arc_xy_bounds(center, radius, angles, *, disk=False):
    lo, hi = angles
    points = [(center[0]+radius*math.cos(math.radians(a)),
               center[1]+radius*math.sin(math.radians(a)))
              for a in [lo, hi]+[a for a in (0,90,180,270,360) if lo <= a <= hi]]
    if disk:
        points.append(tuple(center))
    return [min(p[0] for p in points), max(p[0] for p in points),
            min(p[1] for p in points), max(p[1] for p in points)]


def _partition_covers(rectangles, domain):
    # Parameter-space cells prove coverage without hiding overlaps with a union.
    require(rectangles, "missing semantic patch")
    require(all(_vector(r,4) and r[0] < r[1] and r[2] < r[3]
                and r[0] >= domain[0]-1e-6 and r[1] <= domain[1]+1e-6
                and r[2] >= domain[2]-1e-6 and r[3] <= domain[3]+1e-6
                for r in rectangles), "fragment outside analytic domain")
    xs = sorted({domain[0], domain[1], *(x for r in rectangles for x in r[:2])})
    ys = sorted({domain[2], domain[3], *(y for r in rectangles for y in r[2:])})
    for a,b in zip(xs,xs[1:]):
        for c,d in zip(ys,ys[1:]):
            if b-a <= 1e-6 or d-c <= 1e-6:
                continue
            x,y=(a+b)/2,(c+d)/2
            if domain[0] < x < domain[1] and domain[2] < y < domain[3]:
                require(sum(r[0] < x < r[1] and r[2] < y < r[3] for r in rectangles)==1,
                        "missing/overlapping/duplicate semantic fragment")


def _periodic_witness_trim(face, trim):
    """Check the independent witness source, never certify acquisition from a label.

    The upstream receipt remains acquisition proof. Retained G1 seam coordinates
    let this consumer check physical phase without inventing a witness.theta field.
    """
    witness = face.get("periodic_domain_witness")
    require(isinstance(trim, dict) and trim.get("source") == "NATIVE_PERIODIC_DOMAIN_WITNESS"
            and isinstance(witness, dict) and witness.get("source") == trim["source"]
            and witness.get("source_label") == "DERIVED_FROM_NATIVE"
            and face.get("surface_type") == witness.get("surface_type") == "CYLINDER",
            "independent periodic trim witness unavailable")
    token = _semantic_reference(face.get("reference"), "FACE")
    require(_semantic_reference(witness.get("face_reference"), "FACE") == token
            and face.get("persistent_reference_resolved") is True
            and trim.get("persistent_reference_resolved") is True
            and trim.get("face_reference") == token,
            "periodic witness FACE identity unresolved/mismatched")
    proof = face.get("face_resolution", {})
    require(isinstance(proof, dict) and _semantic_reference(proof, "FACE") == token
            and proof.get("resolved") is True and proof.get("resolved_type") == "FACE"
            and type(proof.get("resolution_error")) is int and proof["resolution_error"] == 0,
            "periodic witness FACE resolution unavailable")
    center, axis, radius, z = (witness.get(k) for k in
                              ("center_mm", "axis_model", "radius_mm", "z_span_mm"))
    require(_same_analytic(witness.get("period_deg"), 360, 1e-6)
            and _vector(center, 3) and _vector(axis, 3)
            and _same_analytic([abs(a) for a in axis], [0, 0, 1], 1e-6)
            and type(radius) in (int, float) and math.isfinite(radius) and radius > 0
            and _vector(z, 2) and z[0] < z[1]
            and _vector(face.get("center_mm"), 3) and _same_analytic(face["center_mm"], center, 1e-6)
            and _vector(face.get("axis"), 3) and _same_analytic(face["axis"], axis, 1e-6)
            and _same_analytic(face.get("radius_mm"), radius, 1e-6)
            and face.get("interior") is True,
            "periodic witness cylinder geometry inconsistent")
    require(trim.get("kind") == "CYLINDER_RECTANGLE"
            and trim.get("coordinate_convention") == "MODEL_XY_CCW_DEGREES"
            and _vector(trim.get("angular_span_deg"), 2)
            and _same_analytic(trim["angular_span_deg"], [0, 360], 1e-6)
            and _vector(trim.get("z_span_mm"), 2) and _same_analytic(trim["z_span_mm"], z, 1e-6),
            "periodic witness derived domain inconsistent")
    provenance = witness.get("same_observation_provenance", {})
    require(isinstance(provenance, dict) and provenance.get("source") == "CONTROLLED_FACE_OBSERVATION"
            and isinstance(provenance.get("observation_token"), str) and provenance["observation_token"]
            and provenance.get("face_reference") == token
            and face.get("same_observation_provenance") == provenance,
            "periodic witness same-observation provenance unavailable/mismatched")
    lineage = witness.get("native_lineage", {})
    require(isinstance(lineage, dict), "periodic witness native lineage missing")
    seam_token = _semantic_reference(lineage.get("seam_edge_reference"), "EDGE")
    circular = lineage.get("circular_boundary_references")
    usage_ids, loop_id = lineage.get("usage_ordinals"), lineage.get("loop_ordinal")
    require(isinstance(circular, list) and len(circular) == 2
            and isinstance(usage_ids, list) and len(usage_ids) == 2
            and all(type(i) is int and i >= 0 for i in usage_ids) and len(set(usage_ids)) == 2
            and type(loop_id) is int and loop_id >= 0
            and lineage.get("ordinal_source") == "OBSERVATION_ORDER_NOT_NATIVE_IDENTITY",
            "periodic witness native lineage incomplete")
    circles = {_semantic_reference(r, "EDGE") for r in circular}
    require(len(circles) == 2 and seam_token not in circles, "periodic witness boundary identity ambiguous")
    edges = face.get("edges")
    require(isinstance(edges, list) and edges and all(isinstance(e, dict) for e in edges),
            "periodic witness seam EDGE missing")
    seams = [e for e in edges if e.get("adjacent_face_references") == [token, token]]
    require(len(seams) == 1 and _semantic_reference(seams[0].get("reference"), "EDGE") == seam_token
            and seams[0].get("periodic_seam") is True
            and not any("split_boundary" in e for e in edges), "periodic witness seam relation inconsistent")
    seam = seams[0].get("seam_parameters")
    require(isinstance(seam, dict) and seam.get("source") == "NATIVE_PERIODIC_EDGE_PARAMETERS"
            and _periodic_seam_parameters_match(seam, trim), "periodic witness seam parameters inconsistent")
    raw = face.get("raw_usage", {})
    require(isinstance(raw, dict) and raw.get("source") == "NATIVE_LOOP_COEDGE_USAGE_SNAPSHOT"
            and raw.get("source_label") == "NATIVE_OBSERVED" and raw.get("surface_type") == "CYLINDER"
            and raw.get("persistent_reference_resolved") is True
            and _semantic_reference(raw.get("face_reference"), "FACE") == token
            and raw.get("face_resolution") == proof
            and _semantic_reference(raw.get("owner_reference"), "FEATURE")
                == _semantic_reference(face.get("owner_reference"), "FEATURE"),
            "periodic witness raw FACE observation inconsistent")
    loops = raw.get("loops")
    require(isinstance(loops, list) and len(loops) == 1 and isinstance(loops[0], dict)
            and type(loops[0].get("loop_ordinal")) is int and loops[0]["loop_ordinal"] == loop_id
            and loops[0].get("is_outer") is True and loops[0].get("traversal_closed") is True
            and type(loops[0].get("usage_count")) is int and loops[0]["usage_count"] == 4,
            "periodic witness raw loop incomplete")
    rows = loops[0].get("coedge_usages")
    require(isinstance(rows, list) and len(rows) == 4 and all(isinstance(r, dict) for r in rows)
            and all(type(r.get("usage_ordinal")) is int and r["usage_ordinal"] >= 0 for r in rows)
            and len({r["usage_ordinal"] for r in rows}) == 4, "periodic witness raw usages ambiguous")
    lines = []
    boundaries = []
    for row in rows:
        edge_token = _semantic_reference(row.get("edge_reference"), "EDGE")
        resolution = row.get("edge_resolution", {})
        relation = row.get("lineage", {})
        require(isinstance(resolution, dict) and _semantic_reference(resolution, "EDGE") == edge_token
                and resolution.get("resolved") is True and resolution.get("resolved_type") == "EDGE"
                and type(resolution.get("resolution_error")) is int and resolution["resolution_error"] == 0
                and row.get("edge_reference_resolved") is True
                and row.get("source_label") == "NATIVE_OBSERVED"
                and row.get("ordinal_source") == "OBSERVATION_ORDER_NOT_NATIVE_IDENTITY"
                and isinstance(relation, dict) and relation.get("loop_ordinal") == loop_id
                and _semantic_reference(relation.get("face_reference"), "FACE") == token
                and _semantic_reference(relation.get("edge_reference"), "EDGE") == edge_token,
                "periodic witness raw typed lineage unresolved/mismatched")
        if edge_token == seam_token:
            a, b = row.get("start_mm"), row.get("end_mm")
            require(row.get("curve_type") == "LINE" and row["usage_ordinal"] in usage_ids
                    and type(row.get("sense")) is bool
                    and row.get("coedge_edge_same_direction") is row["sense"]
                    and _vector(a, 3) and _vector(b, 3)
                    and _same_analytic(a[:2], b[:2], 1e-6)
                    and _same_analytic(math.dist(a[:2], center[:2]), radius, 1e-6)
                    and _same_analytic(sorted([a[2], b[2]]), z, 1e-6),
                    "periodic witness seam geometry inconsistent")
            theta = math.degrees(math.atan2(a[1]-center[1], a[0]-center[0]))
            require(abs(math.remainder(seam["angular_pair_deg"][0]-theta, 360)) <= 1e-6,
                    "periodic witness physical seam phase inconsistent")
            lines.append(row)
        else:
            circle_center = row.get("center_mm")
            require(edge_token in circles and row.get("curve_type") == "CIRCLE"
                    and row.get("full_circle") is True and _vector(row.get("angular_span_deg"), 2)
                    and _same_analytic(row["angular_span_deg"][1]-row["angular_span_deg"][0], 360, 1e-6)
                    and _same_analytic(row.get("angular_coverage_deg"), 360, 1e-6)
                    and type(row.get("direction")) is int and row["direction"] in (-1, 1)
                    and _vector(circle_center, 3) and _same_analytic(circle_center[:2], center[:2], 1e-6)
                    and _vector(row.get("axis"), 3)
                    and _same_analytic([abs(a) for a in row["axis"]], [0, 0, 1], 1e-6)
                    and _same_analytic(row.get("radius_mm"), radius, 1e-6),
                    "periodic witness circular boundary inconsistent")
            boundaries.append((edge_token, circle_center[2], row["direction"]))
    require(len(lines) == 2 and {r["usage_ordinal"] for r in lines} == set(usage_ids)
            and lines[0]["sense"] != lines[1]["sense"]
            and _same_analytic(lines[0]["start_mm"], lines[1]["end_mm"], 1e-6)
            and _same_analytic(lines[0]["end_mm"], lines[1]["start_mm"], 1e-6)
            and {e for e, _, _ in boundaries} == circles
            and len(boundaries) == 2 and boundaries[0][2] == -boundaries[1][2]
            and _same_analytic(sorted(h for _, h, _ in boundaries), z, 1e-6),
            "periodic witness two-sided usage/boundary mismatch")
    return trim


def _trim_evidence(face):
    trim = face.get("trim_domain", {})
    if (isinstance(trim, dict) and trim.get("source") == "NATIVE_PERIODIC_DOMAIN_WITNESS"
            or "periodic_domain_witness" in face):
        return _periodic_witness_trim(face, trim)
    require(trim.get("source") == "NATIVE_ANALYTIC_TRIM_LOOPS"
            and trim.get("face_reference") == _semantic_reference(face.get("reference"), "FACE")
            and trim.get("persistent_reference_resolved") is True,
            "native analytic trim-loop evidence unavailable")
    return trim


def _periodic_angle_interfaces(faces, parameters):
    """Prove all angular interfaces from submitted domains, including absent wrap edges.

    Dictionary evidence proves consistency, not actual native XYZ acquisition.
    Identity comes exclusively from typed references; modulo equality only compares angles.
    """
    tolerance=1e-6
    def equivalent(a,b):
        return (type(a) in (int,float) and type(b) in (int,float)
                and math.isfinite(a) and math.isfinite(b)
                and abs(math.remainder((a%360)-(b%360),360))<=tolerance)
    require(len(faces)==len(parameters) and all(_vector(p,4) for p in parameters),
            "invalid ANGLE fragment domains")
    refs=[_semantic_reference(f.get("reference"),"FACE") for f in faces]
    require(len(set(refs))==len(refs),"duplicate ANGLE FACE identity")
    first=faces[0]
    for face,p in zip(faces,parameters):
        trim=_trim_evidence(face)
        require(face.get("surface_type")=="CYLINDER" and face.get("persistent_reference_resolved") is True
                and trim.get("kind")=="CYLINDER_RECTANGLE"
                and trim.get("coordinate_convention")=="MODEL_XY_CCW_DEGREES"
                and _vector(trim.get("angular_span_deg"),2) and _vector(trim.get("z_span_mm"),2)
                and _same_analytic(p,trim["angular_span_deg"]+trim["z_span_mm"],tolerance)
                and tolerance<p[1]-p[0]<=360+tolerance and p[3]-p[2]>tolerance,
                "invalid ANGLE fragment domains")
        require(_vector(face.get("center_mm"),3) and _vector(face.get("axis"),3)
                and _same_analytic([abs(a) for a in face["axis"]],[0,0,1],tolerance)
                and type(face.get("radius_mm")) in (int,float) and math.isfinite(face["radius_mm"])
                and face["radius_mm"]>0 and face.get("interior") is True
                and _same_analytic(face["center_mm"][:2],first.get("center_mm",[])[:2],tolerance)
                and _same_analytic(face["radius_mm"],first.get("radius_mm"),tolerance),
                "incompatible ANGLE analytic cylinder")
    domain=[min(p[0] for p in parameters),max(p[1] for p in parameters),
            min(p[2] for p in parameters),max(p[3] for p in parameters)]
    require(domain[1]-domain[0]<=360+tolerance,"unsupported ANGLE domain representation")
    _partition_covers(parameters,domain)
    # Domains determine the required physical interfaces before any supplied EDGE is considered.
    required=[]
    for i,left in enumerate(parameters):
        for j in range(i+1,len(parameters)):
            right=parameters[j];span=[max(left[2],right[2]),min(left[3],right[3])]
            if span[1]-span[0]<=tolerance:
                continue
            for a,b in ((left[1],right[0]),(right[1],left[0])):
                if equivalent(a,b):
                    require(left[1]-left[0]<360-tolerance and right[1]-right[0]<360-tolerance,
                            "ambiguous ANGLE interface")
                    require(_same_analytic(left[2:],right[2:],tolerance),
                            "unsupported mixed ANGLE/Z junction")
                    required.append(({refs[i],refs[j]},a%360,span))
    records={}
    for face,token in zip(faces,refs):
        seen=set()
        for edge in face.get("edges",[]):
            e=_semantic_reference(edge.get("reference"),"EDGE")
            require(e not in seen,"duplicate ANGLE EDGE identity")
            seen.add(e)
            pair=edge.get("adjacent_face_references",[]);boundary=edge.get("split_boundary",{})
            require(isinstance(pair,list) and len(pair)==2 and all(isinstance(r,str) for r in pair)
                    and token in pair,"incomplete ANGLE EDGE incidence")
            internal=len(set(pair))==2 and set(pair)<=set(refs)
            if internal:
                require(not edge.get("periodic_seam") and "seam_parameters" not in edge,
                        "distinct FACE split mislabeled as seam")
            if boundary.get("parameter")!="ANGLE":
                continue
            require(internal,"ANGLE split requires two distinct incident FACE identities")
            require(set(boundary)=={"source","parameter","coordinate","span"}
                    and boundary["source"]=="NATIVE_EDGE_PARAMETERS"
                    and _vector([boundary["coordinate"]],1) and _vector(boundary["span"],2)
                    and boundary["span"][1]-boundary["span"][0]>tolerance,
                    "invalid ANGLE split parameters")
            require(edge.get("persistent_reference_resolved",True) is True,
                    "unresolved ANGLE EDGE identity")
            records.setdefault(e,[]).append((token,set(pair),boundary))
    accounted=set()
    for entries in records.values():
        pair=entries[0][1]
        require(len(entries)==2 and {row[0] for row in entries}==pair
                and all(row[1]==pair for row in entries),"nonreciprocal ANGLE EDGE incidence")
        boundary=entries[0][2];other=entries[1][2]
        require(equivalent(boundary["coordinate"],other["coordinate"])
                and _same_analytic(boundary["span"],other["span"],tolerance),
                "inconsistent reciprocal ANGLE split parameters")
        matches=[i for i,(incident,angle,span) in enumerate(required)
                 if pair==incident and equivalent(boundary["coordinate"],angle)
                 and _same_analytic(boundary["span"],span,tolerance)]
        require(len(matches)==1,"incompatible or ambiguous ANGLE split topology")
        require(matches[0] not in accounted,"duplicate physical ANGLE interface")
        accounted.add(matches[0])
    require(len(accounted)==len(required),"missing complete periodic ANGLE interface")


def _connected_fragments(faces, parameters, *, parameter_names):
    if len(faces) == 1:
        return
    angle_fragments=(parameter_names==("ANGLE","Z")
                     and (any(p[:2]!=parameters[0][:2] for p in parameters)
                          or any(e.get("split_boundary",{}).get("parameter")=="ANGLE"
                                 for f in faces for e in f.get("edges",[]))))
    if angle_fragments:
        _periodic_angle_interfaces(faces,parameters)
    refs = [f["reference"]["encoded"] for f in faces]
    graph = {r:set() for r in refs}
    for i,face in enumerate(faces):
        for edge in face.get("edges", []):
            pair=edge.get("adjacent_face_references", [])
            if len(pair)!=2 or refs[i] not in pair:
                continue
            other=next((r for r in pair if r!=refs[i] and r in graph), None)
            if other is None:
                continue
            j=refs.index(other); left,right=parameters[i],parameters[j]
            boundary=edge.get("split_boundary", {})
            require(boundary.get("source") == "NATIVE_EDGE_PARAMETERS", "fragment topology proof missing")
            if angle_fragments and boundary.get("parameter")=="ANGLE":
                graph[refs[i]].add(other)
                continue
            matches=[]
            for axis,name in enumerate(parameter_names):
                k=2*axis; t=2*(1-axis)
                for coordinate in (left[k],left[k+1]):
                    if (abs(coordinate-right[k])<=1e-6 or abs(coordinate-right[k+1])<=1e-6):
                        lo,hi=max(left[t],right[t]),min(left[t+1],right[t+1])
                        if hi-lo>1e-6:
                            matches.append((name,coordinate,[lo,hi]))
            require(any(boundary.get("parameter")==name
                        and type(boundary.get("coordinate")) in (int,float)
                        and abs(boundary["coordinate"]-coordinate)<=1e-6
                        and _vector(boundary.get("span"),2)
                        and all(abs(a-b)<=1e-6 for a,b in zip(boundary["span"],span))
                        for name,coordinate,span in matches), "incompatible fragment split topology")
            graph[refs[i]].add(other)
    seen=set(); pending=[refs[0]]
    while pending:
        r=pending.pop()
        if r not in seen:
            seen.add(r);pending.extend(graph[r]-seen)
    require(len(seen)==len(refs), "disconnected fragments cannot normalize")


def _validate_support_floor_patches(faces, contract, native_features, source_regions, semantic_by_source):
    require(faces, "exposed support floor disk missing")
    parameters=[]; rows=[]; area=0
    for face in faces:
        owner=_semantic_native_owner(face,native_features,source_regions,semantic_by_source)
        require(owner in contract["allowed_native_provenance"], "misattributed exposed support floor")
        require(face.get("surface_type")=="PLANE" and _vector(face.get("normal"),3)
                and all(abs(a-b)<=1e-6 for a,b in zip(face["normal"],[0,0,1])),
                "exposed support floor orientation mismatch")
        trim=_trim_evidence(face)
        require(trim.get("kind")=="DISK_SECTOR" and _vector(trim.get("center_xy_mm"),2)
                and all(abs(a-b)<=.05 for a,b in zip(trim["center_xy_mm"],contract["center_xy_mm"]))
                and type(trim.get("radius_mm")) in (int,float)
                and abs(trim["radius_mm"]-contract["radius_mm"])<=.05
                and _vector(trim.get("angular_span_deg"),2), "support floor terminating-cut boundary mismatch")
        lo,hi=trim["angular_span_deg"]
        require(0<=lo<hi<=360, "invalid support floor sector")
        r=trim["radius_mm"];z=contract["plane_z_mm"]
        box=_arc_xy_bounds(trim["center_xy_mm"],r,[lo,hi],disk=True)+[z,z]
        require(_vector(face.get("bounds_mm"),6)
                and all(abs(a-b)<=.05 for a,b in zip(face["bounds_mm"],box)), "support floor plane/bounds mismatch")
        expected_area=math.pi*r*r*(hi-lo)/360
        require(type(face.get("area_mm2")) in (int,float)
                and math.isfinite(face["area_mm2"]) and abs(face["area_mm2"]-expected_area)<=.05,
                "support floor area mismatch")
        parameters.append([lo,hi,0,r]);area+=face["area_mm2"]
        rows.append({"face_reference":face["reference"],"semantic_region":contract["semantic_region"],
                     "surface_role":contract["surface_role"],"semantic_owner":contract["semantic_owner"],
                     "host_feature":contract["host_feature"],"terminating_cut":contract["terminating_cut"],
                     "native_provenance":owner,"ownership":"API_EXACT"})
    _partition_covers(parameters,[0,360,0,contract["radius_mm"]])
    require(all(_same_analytic(f["trim_domain"]["center_xy_mm"],faces[0]["trim_domain"]["center_xy_mm"],1e-6)
                and _same_analytic(f["trim_domain"]["radius_mm"],faces[0]["trim_domain"]["radius_mm"],1e-6)
                for f in faces), "support floor fragments are not the same analytic circle")
    _connected_fragments(faces,parameters,parameter_names=("ANGLE","RADIUS"))
    require(abs(area-math.pi*contract["radius_mm"]**2)<=.05,"incomplete exposed support floor coverage")
    return rows


def expected_semantic_surfaces(geometry):
    features=geometry["features"]
    require(all(f["feature_type"] in {"BASE_BLOCK","BOSS","THROUGH_HOLE"} for f in features),"unsupported qualification geometry")
    by_id={f["feature_id"]:f for f in features}
    base=next(f for f in features if f["feature_type"]=="BASE_BLOCK")
    bosses=[f for f in features if f["feature_type"]=="BOSS"]
    require(all(f["host_feature_id"]==base["feature_id"] for f in bosses),"qualification B-Rep policy currently bounded to sibling bosses")
    holes=[f for f in features if f["feature_type"]=="THROUGH_HOLE"]
    require(all(f["host_feature_id"] in {b["feature_id"] for b in bosses} for f in holes),"hosted hole expected")
    rows=[]
    volume=0
    for f in [base]+bosses:
        b=f["region_bounds_mm"]; x,y,z=(b[i+1]-b[i] for i in (0,2,4))
        volume+=x*y*z
        for axis in range(3):
            for high in (False,True):
                if f is not base and axis==2 and not high:
                    continue  # Fused contact is not an exterior face.
                box=list(b); box[2*axis]=box[2*axis+1]=b[2*axis+int(high)]
                normal=[0,0,0];normal[axis]=1 if high else -1
                area=(y*z,x*z,x*y)[axis]
                if axis==2 and high:
                    if f is base:
                        area-=sum((bb["region_bounds_mm"][1]-bb["region_bounds_mm"][0])*(bb["region_bounds_mm"][3]-bb["region_bounds_mm"][2]) for bb in bosses)
                    else:
                        area-=sum(math.pi*h["dimensions"]["diameter_mm"]**2/4 for h in holes if h["host_feature_id"]==f["feature_id"])
                rows.append({"region":f["region_id"],"surface_type":"PLANE","bounds_mm":box,
                             "normal":normal,"area_mm2":area,"allowed_owner_semantics":[f["feature_id"]]})
    for h in holes:
        b=h["region_bounds_mm"];r=h["dimensions"]["diameter_mm"]/2; depth=b[5]-b[4]
        volume-=math.pi*r*r*depth
        rows.append({"region":h["region_id"],"surface_type":"CYLINDER","bounds_mm":b,
            "center_mm":h["center_mm"][:2],"radius_mm":r,"axis":[0,0,1],"interior":True,
            "area_mm2":2*math.pi*r*depth,"allowed_owner_semantics":[h["feature_id"]]})
        # Host-through cut exposes a floor in the underlying base; it is not a
        # global through-all hole. Native oldest-owner may be base or cut.
        box=list(b);box[5]=box[4]
        rows.append({"region":h["region_id"]+":exposed-floor","surface_type":"PLANE","bounds_mm":box,
            "normal":[0,0,1],"area_mm2":math.pi*r*r,"allowed_owner_semantics":[base["feature_id"],h["feature_id"]]})
    return rows,volume


def load_semantic_evidence(folder, *, pinned_manifest_sha256, source_path,
                           pinned_source_sha256, case_id, case_revision):
    """Evaluator-only authority load; no planner, adapter, or generation consumer."""
    folder=Path(folder).resolve(strict=True)
    manifest, seal, members=sealed_manifest(folder)
    require(seal==pinned_manifest_sha256, "expected manifest pin mismatch")
    require({p.resolve() for p in folder.rglob("*") if p.is_file()}==members,
            "extra/missing expected member")
    names={p.name for p in members}
    require(names=={"manifest.json","manifest.sha256","source_binding.json","geometry.json",
        "expected_semantic_brep.json","expected_semantic_ownership.json","correspondence.json",
        "projections.json","slot_annotation_intent.json","derivation_audit.json","review_report.md"},
        "expected member domain mismatch")
    require(manifest.get("case_id")==case_id and manifest.get("case_revision")==case_revision
            and manifest.get("source_sha256")==pinned_source_sha256
            and digest(source_path)==pinned_source_sha256, "expected source/case/revision binding mismatch")
    provenance=manifest.get("provenance",{})
    require(provenance.get("source_type")=="SOURCE_DERIVED_INDEPENDENT_EVALUATOR"
            and provenance.get("case_id")==case_id and provenance.get("case_revision")==case_revision
            and provenance.get("source_sha256")==pinned_source_sha256
            and provenance.get("generation_outputs_used") is False
            and provenance.get("native_evidence_observed") is False,
            "expected evidence independent provenance unavailable")
    documents={name:read_document(folder/name) for name in ("source_binding.json","geometry.json",
        "expected_semantic_brep.json","expected_semantic_ownership.json","correspondence.json")}
    require(all(d.get("provenance")==provenance for d in documents.values()), "expected provenance mismatch")
    require(documents["source_binding.json"].get("source_sha256")==pinned_source_sha256,
            "expected source binding mismatch")
    return {"manifest":manifest,"manifest_sha256":seal,"documents":documents,
            "evidence_directory":str(folder),"source_path":str(Path(source_path).resolve(strict=True)),
            "validator_expected_evidence_reads":len(documents),
            "consumption_policy":"EXPOSED_PARENT_SUPPORT_FLOOR_V1"}


def _semantic_expected_documents(evidence):
    require(isinstance(evidence.get("evidence_directory"),str) and isinstance(evidence.get("source_path"),str),
            "sealed expected disk binding missing; inline/loose expected is not authority")
    folder=Path(evidence["evidence_directory"]).resolve(strict=True)
    current,seal,members=sealed_manifest(folder)
    require(seal==evidence["manifest_sha256"] and current==evidence["manifest"]
            and {p.resolve() for p in folder.rglob("*") if p.is_file()}==members
            and digest(evidence["source_path"])==current["source_sha256"],
            "sealed expected disk/source binding changed after load")
    require(evidence.get("consumption_policy")=="EXPOSED_PARENT_SUPPORT_FLOOR_V1"
            and document_digest(evidence["manifest"])==evidence["manifest_sha256"],
            "sealed consumption contract missing/changed")
    hashes={e["relative_path"]:e["sha256"] for e in evidence["manifest"]["artifacts"]}
    documents=evidence["documents"]
    require(set(documents)=={"source_binding.json","geometry.json","expected_semantic_brep.json",
        "expected_semantic_ownership.json","correspondence.json"}, "expected semantic document set incomplete")
    require(all(document_digest(d)==hashes.get(name) for name,d in documents.items()),
            "expected document changed after sealed load")
    return documents


def _same_analytic(left,right,tolerance=.05):
    if type(left) in (int,float) and type(right) in (int,float):
        return math.isfinite(left) and math.isfinite(right) and abs(left-right)<=tolerance
    if isinstance(left,list) and isinstance(right,list):
        return len(left)==len(right) and all(_same_analytic(a,b,tolerance) for a,b in zip(left,right))
    return type(left)==type(right) and left==right


def _footprint(feature):
    b=feature["bounds_mm"];kind=feature["feature_type"]
    if kind in {"BASE_BLOCK","BOSS"}:
        return ["RECTANGLE",*b[:4]]
    c=feature["center_mm"]
    if kind=="THROUGH_HOLE":
        return ["CIRCLE",*c[:2],feature["dimensions_mm"]["radius_mm"]]
    d=feature["dimensions_mm"]
    return ["STRAIGHT_SLOT",feature["orientation"]["major_axis"],*c[:2],d["width_mm"],d["overall_length_mm"]]


def _footprint_area(shape):
    if shape[0]=="RECTANGLE":return (shape[2]-shape[1])*(shape[4]-shape[3])
    if shape[0]=="CIRCLE":return math.pi*shape[3]**2
    width,length=shape[4:6]
    return width*(length-width)+math.pi*(width/2)**2


def _semantic_features(geometry):
    features=geometry["features"]
    by_id={f["semantic_feature_id"]:f for f in features}
    require(features and len(by_id)==len(features)
            and len({f["source_id"] for f in features})==len(features), "duplicate semantic/source feature identity")
    require(all(f["feature_type"] in {"BASE_BLOCK","BOSS","THROUGH_HOLE","STRAIGHT_SLOT"} for f in features),
            "unsupported expected feature family")
    require(sum(f["feature_type"]=="BASE_BLOCK" for f in features)==1
            and sum(f["feature_type"]=="BOSS" for f in features)<=1, "bounded support hierarchy required")
    base=next(f for f in features if f["feature_type"]=="BASE_BLOCK")
    for f in features:
        b=f["bounds_mm"]; d=f["dimensions_mm"];kind=f["feature_type"]
        require(_vector(b,6) and all(b[i]<b[i+1] for i in (0,2,4)), "invalid expected bounds")
        require(_same_analytic(f["center_mm"],[(b[i]+b[i+1])/2 for i in (0,2,4)],1e-6)
                and all(_same_analytic(d[a],b[2*i+1]-b[2*i],1e-6) for i,a in enumerate("xyz")),
                "inconsistent expected primitive dimensions")
        host=by_id.get(f["host_semantic_id"])
        require(f["orientation"]["axis"]=="Z", "unsupported expected axis")
        if kind=="BASE_BLOCK":
            require(host is None and f["host_semantic_id"] is None, "base cannot have a host")
            continue
        require(host is not None and host["feature_type"] in {"BASE_BLOCK","BOSS"}, "expected host unavailable")
        h=host["bounds_mm"]
        require(all(h[i]<b[i]<b[i+1]<h[i+1] for i in (0,2)), "expected host footprint containment failed")
        if kind=="BOSS":
            require(host is base and abs(b[4]-h[5])<=1e-6, "recursive/additive support unsupported")
        else:
            require(f.get("full_span") is True and _same_analytic(b[4:],h[4:],1e-6),
                    "cut must span its own host, not the entire part")
            if kind=="THROUGH_HOLE":
                require(_same_analytic(d["x"],d["y"],1e-6)
                        and _same_analytic(d["diameter_mm"],d["x"],1e-6)
                        and _same_analytic(d["radius_mm"]*2,d["x"],1e-6), "expected circular profile inconsistent")
            else:
                axis=f["orientation"]["major_axis"]
                require(host is base and axis in {"X","Y"}, "bounded slot host/major axis unsupported")
                width=d["y" if axis=="X" else "x"];length=d["x" if axis=="X" else "y"]
                require(length>width and _same_analytic(d["width_mm"],width,1e-6)
                        and _same_analytic(d["radius_mm"]*2,width,1e-6)
                        and _same_analytic(d["overall_length_mm"],length,1e-6)
                        and _same_analytic(d["end_center_distance_mm"],length-width,1e-6),
                        "expected slot tangent dimensions inconsistent")
        require(f["host_source_id"]==host["source_id"]
                and _same_analytic(f["support_intent"]["plane_z_mm"],h[5],1e-6), "expected support relation mismatch")
    for host in features:
        children=[f for f in features if f["host_semantic_id"]==host["semantic_feature_id"]]
        for i,a in enumerate(children):
            for b in children[i+1:]:
                require(any(a["bounds_mm"][k+1]<b["bounds_mm"][k] or b["bounds_mm"][k+1]<a["bounds_mm"][k]
                            for k in (0,2)), "overlapping bounded footprints unsupported")
    return by_id


def _semantic_surface_targets(documents):
    features=_semantic_features(documents["geometry.json"])
    surfaces=documents["expected_semantic_brep.json"]["surfaces"]
    ownership=documents["expected_semantic_ownership.json"]["entries"]
    require(documents["expected_semantic_ownership.json"].get("geometric_matching_is_ownership_proof") is False,
            "geometry cannot substitute native ownership")
    require(len({s["surface_id"] for s in surfaces})==len(surfaces)
            and len({o["surface_id"] for o in ownership})==len(ownership), "duplicate expected surface/ownership")
    owners={o["surface_id"]:o["expected_semantic_owner"] for o in ownership}
    require(set(owners)=={s["surface_id"] for s in surfaces}, "dangling/incomplete expected ownership")
    correspondence=documents["correspondence.json"]["entries"]
    require(len(correspondence)==len(features) and len({c["source_id"] for c in correspondence})==len(features),
            "source semantic correspondence incomplete")
    for c in correspondence:
        require(c["semantic_feature_id"] in features and features[c["semantic_feature_id"]]["source_id"]==c["source_id"]
                and set(c["expected_surface_ids"])=={s["surface_id"] for s in surfaces if s["source_id"]==c["source_id"]}
                and len(set(c["expected_surface_ids"]))==len(c["expected_surface_ids"]), "expected correspondence mismatch")
    targets=[];floors=[]
    for s in surfaces:
        owner=s["semantic_owner_id"];f=features.get(owner);definition=s["definition"];kind=s["surface_kind"]
        require(f is not None and s["source_id"]==f["source_id"] and owners[s["surface_id"]]==owner,
                "expected semantic owner mismatch")
        t={"semantic_region":s["surface_id"],"semantic_owner":owner,"surface_role":kind,
           "host_feature":f["host_semantic_id"],"allowed_native_provenance":[owner]}
        b=f["bounds_mm"]
        if kind=="PLANAR_MATERIAL_SURFACE":
            require(f["feature_type"] in {"BASE_BLOCK","BOSS"}, "expected material owner invalid")
            axis="XYZ".index(definition["normal_axis"]); sign=definition["normal_sign"]
            require(sign in {-1,1} and not(f["feature_type"]=="BOSS" and axis==2 and sign==-1)
                    and _same_analytic(definition["patch_bounds_mm"],b,1e-6)
                    and _same_analytic(definition["coordinate_mm"],b[2*axis+int(sign==1)],1e-6),
                    "expected material plane definition mismatch")
            children=[q for q in features.values() if q["host_semantic_id"]==owner]
            trims=children if axis==2 and sign==1 else [q for q in children if q["full_span"]] if axis==2 else []
            require(sorted(definition["trim_regions"])==sorted(q["semantic_feature_id"] for q in trims),
                    "expected material trim relation mismatch")
            uv=[k for k in range(3) if k!=axis]
            normal=[0,0,0];normal[axis]=sign
            t.update(surface_type="PLANE",normal=normal,axis=axis,coordinate_mm=definition["coordinate_mm"],
                     outer_uv_mm=[b[2*uv[0]],b[2*uv[0]+1],b[2*uv[1]],b[2*uv[1]+1]],
                     cutouts=sorted((_footprint(q) for q in trims),key=lambda q:json.dumps(q)))
            if axis==2 and sign==1:
                for child in children:
                    if child["feature_type"]=="BOSS":
                        cuts=[q for q in features.values() if q["host_semantic_id"]==child["semantic_feature_id"]]
                        require(len(cuts)<=1, "bounded terminating-cut multiplicity exceeded")
                        for cut in cuts:
                            floors.append(_support_floor_contract(f,child,cut,s["surface_id"]))
        elif kind in {"CYLINDRICAL_CUT_WALL","SEMICYLINDRICAL_CUT_WALL"}:
            require(f["feature_type"] in {"THROUGH_HOLE","STRAIGHT_SLOT"}
                    and definition["axis"]=="Z" and definition["host_semantic_id"]==f["host_semantic_id"]
                    and _same_analytic(definition["z_span_mm"],b[4:],1e-6)
                    and _same_analytic(definition["radius_mm"],f["dimensions_mm"]["radius_mm"],1e-6),
                    "expected cylinder host/span/radius mismatch")
            center=f["center_mm"][:2];angles=[0,360]
            if f["feature_type"]=="STRAIGHT_SLOT":
                require(kind=="SEMICYLINDRICAL_CUT_WALL", "slot end-wall kind mismatch")
                major="XY".index(f["orientation"]["major_axis"]);radius=definition["radius_mm"]
                positions=[b[2*major]+radius,b[2*major+1]-radius]
                matches=[i for i,p in enumerate(positions) if abs(definition["center_xy_mm"][major]-p)<=1e-6]
                require(len(matches)==1, "expected slot end location mismatch")
                side=matches[0];center=list(center);center[major]=positions[side]
                angles=([90,270] if side==0 else [-90,90]) if major==0 else ([180,360] if side==0 else [0,180])
            else:require(kind=="CYLINDRICAL_CUT_WALL" and definition.get("full_host_span") is True, "expected through-hole wall kind")
            require(_same_analytic(definition["center_xy_mm"],center,1e-6)
                    and _same_analytic(definition["angular_span_deg"],angles,1e-6), "expected analytic end-wall definition mismatch")
            t.update(surface_type="CYLINDER",center_xy_mm=center,radius_mm=definition["radius_mm"],
                     angular_span_deg=angles,z_span_mm=b[4:])
        else:
            require(kind=="PLANAR_CUT_WALL" and f["feature_type"]=="STRAIGHT_SLOT", "unsupported expected surface")
            major="XY".index(f["orientation"]["major_axis"]);axis=1-major;radius=f["dimensions_mm"]["radius_mm"]
            coordinate=definition["coordinate_mm"]
            require(definition["normal_axis"]=="XY"[axis]
                    and any(abs(coordinate-b[2*axis+i])<=1e-6 for i in (0,1))
                    and definition["host_semantic_id"]==f["host_semantic_id"]
                    and _same_analytic(definition["z_span_mm"],b[4:],1e-6), "expected slot longitudinal plane mismatch")
            span=[b[2*major]+radius,b[2*major+1]-radius]
            require(_same_analytic(definition["xy"[major]+"_span_mm"],span,1e-6), "expected slot tangent span mismatch")
            normal=[0,0,0];normal[axis]=1 if abs(coordinate-b[2*axis])<=1e-6 else -1
            t.update(surface_type="PLANE",axis=axis,normal=normal,coordinate_mm=coordinate,
                     outer_uv_mm=span+b[4:],cutouts=[])
        targets.append(t)
    for f in features.values():
        rows=[t for t in targets if t["semantic_owner"]==f["semantic_feature_id"]]
        count={"BASE_BLOCK":6,"BOSS":5,"THROUGH_HOLE":1,"STRAIGHT_SLOT":4}[f["feature_type"]]
        require(len(rows)==count and len({document_digest(t) for t in rows})==count, "expected semantic region set incomplete/duplicate")
    return features,targets,floors


def _semantic_native_features(native,source_regions,features):
    registry=native.get("native_features",{})
    by_source={f["source_id"]:fid for fid,f in features.items()}
    require(set(registry)==set(source_regions) and len(registry)==len(features)
            and set(source_regions.values())==set(by_source), "native feature/source correspondence incomplete")
    require(len({_semantic_reference(r.get("reference"),"FEATURE") for r in registry.values()})==len(registry),
            "duplicate native feature persistent identity")
    native_by_semantic={by_source[source]:fid for fid,source in source_regions.items()}
    for fid,row in registry.items():
        f=features[by_source[source_regions[fid]]];host=native_by_semantic.get(f["host_semantic_id"])
        require(row.get("native_feature_id")==fid and row.get("persistent_reference_resolved") is True
                and row.get("relationship_source")=="NATIVE_PERSISTENT_FEATURE_SUPPORT"
                and row.get("host_native_feature_id")==host, "native host identity mismatch/unresolved")
        if host is not None:
            require(_semantic_reference(row.get("host_reference"),"FEATURE")==registry[host]["reference"]["encoded"]
                    and row.get("host_reference_resolved") is True
                    and _semantic_reference(row.get("support_owner_reference"),"FEATURE")==registry[host]["reference"]["encoded"]
                    and row.get("support_reference_resolved") is True
                    and bool(_semantic_reference(row.get("support_reference"),"FACE"))
                    and _same_analytic(row.get("support_plane_z_mm"),f["support_intent"]["plane_z_mm"]),
                    "native host/support reference relation mismatch")
    return registry,by_source


def _semantic_plane_parameter(face,target):
    trim=_trim_evidence(face)
    require(trim.get("kind")=="RECTANGLE_WITH_CUTOUTS", "unsupported observed planar trim")
    outer=trim.get("outer_uv_mm");cutouts=trim.get("cutouts")
    require(_vector(outer,4) and outer[0]<outer[1] and outer[2]<outer[3]
            and isinstance(cutouts,list), "incomplete observed plane boundary")
    expected=target["cutouts"]
    require(len(cutouts)==len(expected) and all(sum(_same_analytic(a,b) for b in cutouts)==1 for a in expected),
            "observed planar trim footprint mismatch")
    if expected:require(_same_analytic(outer,target["outer_uv_mm"]), "trimmed planar fragmentation unsupported")
    uv=[a for a in range(3) if a!=target["axis"]];box=[0]*6
    box[2*target["axis"]]=box[2*target["axis"]+1]=target["coordinate_mm"]
    for i,a in enumerate(uv):box[2*a:2*a+2]=outer[2*i:2*i+2]
    require(_vector(face.get("normal"),3) and _same_analytic(face["normal"],target["normal"],1e-6)
            and _vector(face.get("bounds_mm"),6) and _same_analytic(face["bounds_mm"],box), "native plane orientation/position/span mismatch")
    area=(outer[1]-outer[0])*(outer[3]-outer[2])-sum(_footprint_area(c) for c in cutouts)
    require(area>0 and type(face.get("area_mm2")) in (int,float)
            and _same_analytic(face["area_mm2"],area), "native planar material area mismatch")
    return outer


def _semantic_cylinder_parameter(face,target):
    trim=_trim_evidence(face)
    require(trim.get("kind")=="CYLINDER_RECTANGLE" and trim.get("coordinate_convention")=="MODEL_XY_CCW_DEGREES"
            and _vector(trim.get("angular_span_deg"),2) and _vector(trim.get("z_span_mm"),2),
            "observed cylinder trim parameters missing")
    angles=trim["angular_span_deg"];z=trim["z_span_mm"]
    require(_vector(face.get("axis"),3) and abs(abs(face["axis"][2])-1)<=1e-6
            and abs(face["axis"][0])<=1e-6 and abs(face["axis"][1])<=1e-6
            and _vector(face.get("center_mm"),3) and _same_analytic(face["center_mm"][:2],target["center_xy_mm"])
            and _same_analytic(face.get("radius_mm"),target["radius_mm"])
            and face.get("interior") is True, "native cylinder radius/axis/location/interior mismatch")
    box=_arc_xy_bounds(face["center_mm"][:2],face["radius_mm"],angles)+z
    area=math.radians(angles[1]-angles[0])*face["radius_mm"]*(z[1]-z[0])
    require(area>0 and _vector(face.get("bounds_mm"),6) and _same_analytic(face["bounds_mm"],box)
            and _same_analytic(face.get("area_mm2"),area), "native cylindrical boundary/area/span mismatch")
    return angles+z


def _periodic_seam_parameters_match(seam,trim):
    # Full-period coverage has no physical phase; a seam retains its native
    # model angle. Only compare their periods, never their numerical origins.
    if not isinstance(seam,dict) or not isinstance(trim,dict):
        return False
    domain=trim.get("angular_span_deg");angles=seam.get("angular_pair_deg")
    z=trim.get("z_span_mm");seam_z=seam.get("z_span_mm")
    if not all(_vector(value,2) for value in (domain,angles,z,seam_z)):
        return False
    period=360; tolerance=1e-6
    return (_same_analytic(domain[1]-domain[0],period,tolerance)
            and _same_analytic(angles[1]-angles[0],period,tolerance)
            and abs(math.remainder((angles[1]%period)-(angles[0]%period),period))<=tolerance
            and z[0]<z[1] and seam_z[0]<seam_z[1]
            and _same_analytic(seam_z,z,tolerance))


def _semantic_edge_accounting(faces,body):
    refs={_semantic_reference(f["reference"],"FACE") for f in faces};incidence={}
    for face in faces:
        token=face["reference"]["encoded"];seen=set()
        require(face.get("edges"), "native adjacency evidence missing")
        for edge in face["edges"]:
            e=_semantic_reference(edge.get("reference"),"EDGE");pair=edge.get("adjacent_face_references",[])
            require(len(pair)==2 and set(pair)<=refs and token in pair and e not in seen,
                    "native edge adjacency incomplete/duplicate")
            seen.add(e)
            if len(set(pair))==1:
                seam=edge.get("seam_parameters",{})
                require(face["surface_type"]=="CYLINDER" and edge.get("periodic_seam") is True
                        and isinstance(seam,dict) and seam.get("source")=="NATIVE_PERIODIC_EDGE_PARAMETERS"
                        and _periodic_seam_parameters_match(seam,face.get("trim_domain")),
                        "unproven periodic native seam")
            if e not in incidence:incidence[e]=(set(pair),[])
            require(incidence[e][0]==set(pair), "inconsistent native edge adjacency")
            incidence[e][1].append(token)
    require(len(incidence)==body["edge_count"] and all(len(rows)==len(pair) and set(rows)==pair for pair,rows in incidence.values()),
            "incomplete/nonreciprocal native edge accounting")


def _validate_semantic_native_geometry(native,evidence,source_regions):
    require(native.get("evidence_kind") in {"NATIVE","SIMULATED"}, "observed evidence kind unavailable")
    docs=_semantic_expected_documents(evidence)
    features,targets,floors=_semantic_surface_targets(docs)
    registry,semantic_by_source=_semantic_native_features(native,source_regions,features)
    require(validate_body_count(native["body"])["status"]=="PASS", "one solid body required")
    faces=native["faces"]
    require(len(faces)==native["body"]["face_count"]
            and len({_semantic_reference(f["reference"],"FACE") for f in faces})==len(faces), "native face accounting/identity mismatch")
    bounds=docs["geometry.json"]["overall_bounds_mm"]
    require(_same_analytic(native.get("overall_bounds_mm"),bounds), "independent part envelope mismatch")
    volume=sum((_footprint_area(_footprint(f))*(f["bounds_mm"][5]-f["bounds_mm"][4]))
               *(-1 if f["full_span"] else 1) for f in features.values())
    require(type(native.get("volume_mm3")) in (int,float) and math.isfinite(native["volume_mm3"])
            and abs(native["volume_mm3"]-volume)<=max(.001,abs(volume)*1e-6), "independent analytic volume mismatch")
    groups={t["semantic_region"]:[] for t in targets};floor_groups=[[] for f in floors];rows=[]
    for face in faces:
        owner=_semantic_native_owner(face,registry,source_regions,semantic_by_source)
        trim=_trim_evidence(face);matches=[]
        if trim.get("kind")=="DISK_SECTOR":
            for i,floor in enumerate(floors):
                if (_same_analytic(trim.get("center_xy_mm"),floor["center_xy_mm"])
                    and _same_analytic(trim.get("radius_mm"),floor["radius_mm"])):
                    matches.append(i)
            require(len(matches)==1, "unexpected/dangling floor semantic patch")
            floor_groups[matches[0]].append(face)
            continue
        for t in targets:
            if face.get("surface_type")!=t["surface_type"] or owner not in t["allowed_native_provenance"]:
                continue
            if t["surface_type"]=="PLANE":
                if (not _vector(face.get("normal"),3) or not _same_analytic(face["normal"],t["normal"],1e-6)
                    or not _vector(face.get("bounds_mm"),6) or abs(face["bounds_mm"][2*t["axis"]]-t["coordinate_mm"])>.05):continue
                parameter=_semantic_plane_parameter(face,t)
            else:
                if (not _vector(face.get("center_mm"),3) or not _same_analytic(face["center_mm"][:2],t["center_xy_mm"])
                    or not _same_analytic(face.get("radius_mm"),t["radius_mm"])):continue
                parameter=_semantic_cylinder_parameter(face,t)
            matches.append((t,parameter))
        require(len(matches)==1, "MISATTRIBUTED/ambiguous/unexpected native semantic surface")
        t,parameter=matches[0];groups[t["semantic_region"]].append((face,parameter))
        rows.append({"face_reference":face["reference"],"semantic_region":t["semantic_region"],
                     "semantic_owner":t["semantic_owner"],"surface_role":t["surface_role"],
                     "host_feature":t["host_feature"],"native_provenance":owner,"ownership":"API_EXACT"})
    for t in targets:
        entries=groups[t["semantic_region"]];require(entries,"missing expected semantic surface")
        observed,parameters=zip(*entries)
        domain=t["outer_uv_mm"] if t["surface_type"]=="PLANE" else t["angular_span_deg"]+t["z_span_mm"]
        _partition_covers(parameters,domain)
        _connected_fragments(observed,parameters,parameter_names=("U","V") if t["surface_type"]=="PLANE" else ("ANGLE","Z"))
        require(len({f["native_owner_feature_id"] for f in observed})==1, "fragment native owner mismatch")
        if t["surface_type"]=="CYLINDER":
            first=observed[0]
            require(all(_same_analytic(f["radius_mm"],first["radius_mm"],1e-6)
                        and _same_analytic(f["center_mm"][:2],first["center_mm"][:2],1e-6)
                        and _same_analytic([abs(v) for v in f["axis"]],[abs(v) for v in first["axis"]],1e-6)
                        for f in observed), "fragments are not the same analytic cylinder")
            area=math.radians(domain[1]-domain[0])*t["radius_mm"]*(domain[3]-domain[2])
        else:
            area=(domain[1]-domain[0])*(domain[3]-domain[2])-sum(_footprint_area(c) for c in t["cutouts"])
        require(abs(sum(f["area_mm2"] for f in observed)-area)<=.05, "normalized semantic area mismatch")
    for floor,observed in zip(floors,floor_groups):
        rows.extend(_validate_support_floor_patches(observed,floor,registry,source_regions,semantic_by_source))
    _semantic_edge_accounting(faces,native["body"])
    require(len(rows)==len(faces), "unattributed native surface")
    rows.sort(key=lambda r:r["face_reference"]["encoded"])
    return {"status":"PASS","validation_domain":"BOUNDED_SEMANTIC_SURFACES_WITH_SUPPORT_FLOOR_V1",
            "evidence_kind":native.get("evidence_kind"),"engineering_status":"NOT_VALIDATED",
            "semantic_region_count":len(targets),"native_face_count":len(faces),
            "semantic_regions":sorted(groups),"expected_volume_mm3":volume,"rows":rows,
            "UNKNOWN":0,"UNATTRIBUTED":0,"MISATTRIBUTED":0,
            "validator_expected_evidence_reads":evidence["validator_expected_evidence_reads"]}


def validate_native_geometry(native,geometry,source_regions, *, expected_evidence=None):
    if expected_evidence is not None:
        require(geometry is None, "planner/legacy geometry cannot supply sealed evaluator expected truth")
        return _validate_semantic_native_geometry(native,expected_evidence,source_regions)
    require(validate_body_count(native["body"])["status"]=="PASS","one solid body required")
    expected,volume=expected_semantic_surfaces(geometry)
    semantic_by_region={f["region_id"]:f["feature_id"] for f in geometry["features"]}
    owners={fid:semantic_by_region.get(region) for fid,region in source_regions.items()}
    require(all(owners.values()) and len(set(owners.values()))==len(owners),"source semantic correspondence incomplete")
    require(abs(native["volume_mm3"]-volume)<=max(.001,abs(volume)*1e-6),"independent analytic material volume mismatch")
    actual=native["faces"]
    require(len(actual)==native["body"]["face_count"]==len(expected),"complete semantic surface accounting failed")
    require(len({f["reference"]["encoded"] for f in actual})==len(actual),"duplicate native face identity")
    matched=set();rows=[]
    for face in actual:
        require(_vector(face["bounds_mm"],6) and type(face["area_mm2"]) in (int,float)
                and math.isfinite(face["area_mm2"]) and face["area_mm2"]>0,"incomplete native surface metrics")
        if face["surface_type"]=="PLANE":
            require(_vector(face["normal"],3),"incomplete native plane normal")
        elif face["surface_type"]=="CYLINDER":
            require(_vector(face["axis"],3) and _vector(face["center_mm"],3)
                    and type(face["radius_mm"]) in (int,float) and math.isfinite(face["radius_mm"]),"incomplete native cylinder")
        owner=owners.get(face["native_owner_feature_id"])
        require(owner is not None and face["owner_reference"]["encoded"] and face["persistent_reference_resolved"] is True,
                "UNKNOWN/UNATTRIBUTED/unresolved native owner")
        matches=[]
        for i,region in enumerate(expected):
            if face["surface_type"]!=region["surface_type"] or owner not in region["allowed_owner_semantics"]:
                continue
            checks=[all(abs(a-b)<=.05 for a,b in zip(face["bounds_mm"],region["bounds_mm"])),
                    abs(face["area_mm2"]-region["area_mm2"])<=.05]
            if region["surface_type"]=="PLANE":
                checks.append(all(abs(a-b)<=1e-6 for a,b in zip(face["normal"],region["normal"])))
            else:
                checks.extend([face["interior"] is True,abs(face["radius_mm"]-region["radius_mm"])<=.05,
                    all(abs(a-b)<=.05 for a,b in zip(face["center_mm"][:2],region["center_mm"])),
                    abs(abs(face["axis"][2])-1)<=1e-6 and abs(face["axis"][0])<=1e-6 and abs(face["axis"][1])<=1e-6])
            if all(checks): matches.append(i)
        require(len(matches)==1 and matches[0] not in matched,"MISATTRIBUTED/ambiguous/unexpected semantic surface")
        matched.add(matches[0])
        rows.append({"face_reference":face["reference"],"native_owner_feature_id":face["native_owner_feature_id"],
                     "semantic_region":expected[matches[0]]["region"],"ownership":"API_EXACT"})
    face_refs={f["reference"]["encoded"] for f in actual}
    edge_incidence={}
    for face in actual:
        require(face["edges"],"native adjacency evidence missing")
        for e in face["edges"]:
            require(len(e["adjacent_face_references"])==len(set(e["adjacent_face_references"]))==2 and set(e["adjacent_face_references"])<=face_refs
                    and face["reference"]["encoded"] in e["adjacent_face_references"],"native final-body adjacency incomplete")
            token=e["reference"]["encoded"]
            pair=set(e["adjacent_face_references"])
            if token not in edge_incidence: edge_incidence[token]=(pair,[])
            require(edge_incidence[token][0]==pair,"inconsistent native edge adjacency")
            edge_incidence[token][1].append(face["reference"]["encoded"])
    require(len(edge_incidence)==native["body"]["edge_count"],"complete native edge accounting failed")
    require(all(len(rows)==2 and set(rows)==pair for pair,rows in edge_incidence.values()),"nonreciprocal native edge incidence")
    return {"status":"PASS","expected_surface_count_from_reviewed_geometry":len(expected),"expected_volume_mm3":volume,
            "rows":rows,"UNKNOWN":0,"UNATTRIBUTED":0,"MISATTRIBUTED":0}


def _compare_drawing_view(native, generated, source, role, *, require_roundtrip, diagnostics):
    frame=native["native_views"][role]
    axes=frame["transform_axes"]
    require(list(from_native(axes["right"]))==source["frame"]["right"]
            and list(from_native(axes["up"]))==source["frame"]["up"]
            and frame["sheet_first_angle_native_readback"] is True,"native physical view source-axis mismatch")
    require(not require_roundtrip or native["drawing_save_reopen"] is True,"native drawing roundtrip missing")
    origin=source["frame"]["origin_mm"]
    shift=[-sum(origin[i]*source["frame"][key][i] for i in range(3)) for key in ("right","up")]
    diagnostics.update(native_frame=frame, source_spaces=[MODEL_PROJECTED_PRIMITIVE,SHEET_ANNOTATION],
                       evaluator_origin_shift_mm=shift, groups={}, linear_spline_mappings=[])
    def coords(g,kind,space):
        converted=normalize_primitive(g,kind,frame,source_space=space)
        # This origin belongs only to evaluator correspondence, not generation.
        for key in (("x1","y1","x2","y2") if kind=="line" else ("x","y")):
            converted[key]+=shift[0 if key.startswith("x") else 1]
        return converted
    centers=[]
    for row in native["centerlines"][role]:
        require(row.get("primitive_source_space",SHEET_ANNOTATION)==SHEET_ANNOTATION,"annotation source space mismatch")
        a,b=row["sheet_endpoints_m"]
        centers.append(coords({"x1":a[0]*1000,"y1":a[1]*1000,"x2":b[0]*1000,"y2":b[1]*1000},"line",SHEET_ANNOTATION))
    for mode in ("hlr_views","hlv_views"):
        require(native[mode][role].get("primitive_source_space",MODEL_PROJECTED_PRIMITIVE)==MODEL_PROJECTED_PRIMITIVE,
                "model primitive source space mismatch")
    require(not generated[role]["visible_splines"],"visible spline comparison unsupported")
    visible=[coords(g,"line",MODEL_PROJECTED_PRIMITIVE) for g in generated[role]["visible_lines"]]
    included=[c for c in centers if match([c],visible,"line")["recall"]==1.0]
    if included:
        subtraction=support_difference(included,visible)
        require(subtraction["status"]=="PASS","native annotation support not stable")
        visible=[r["geometry"] for r in subtraction["candidates"]]
    hidden=[coords(g,"line",MODEL_PROJECTED_PRIMITIVE) for g in generated[role]["hidden_lines"]]
    for candidate in generated[role]["hidden_splines"]:
        mapping=hidden_linear_spline(candidate,frame)
        geometry=mapping["geometry"]
        for key in ("x1","y1","x2","y2"):
            geometry[key]+=shift[0 if key.startswith("x") else 1]
        diagnostics["linear_spline_mappings"].append(mapping)
        hidden.append(geometry)
    groups={"LINE":visible,"HIDDEN_LINE":hidden,
            "CIRCLE":[coords(g,"circle",MODEL_PROJECTED_PRIMITIVE) for g in generated[role]["circles"]],
            "ARC":[coords(g,"arc",MODEL_PROJECTED_PRIMITIVE) for g in generated[role]["arcs"]],"CENTERLINE":centers}
    raw_groups={"LINE":generated[role]["visible_lines"],
                "HIDDEN_LINE":{"lines":generated[role]["hidden_lines"],"splines":generated[role]["hidden_splines"]},
                "CIRCLE":generated[role]["circles"],"ARC":generated[role]["arcs"],
                "CENTERLINE":native["centerlines"][role]}
    results={}
    for kind,actual in groups.items():
        wanted=[p["geometry"] for p in source["primitives"] if p["primitive_type"]==kind]
        # Keep inputs even when the matcher itself raises, before it returns a result.
        group={"view":role,"matcher_group":kind,
            "primitive_source_space":SHEET_ANNOTATION if kind=="CENTERLINE" else MODEL_PROJECTED_PRIMITIVE,
            "raw_primitives":raw_groups[kind],"normalized_primitives":actual,
            "expected_count":len(wanted),"actual_count":len(actual),"matcher":{"status":"NOT_RUN"},
            "failure_reason":None}
        diagnostics["groups"][kind]=group
        try:
            result=match(wanted,actual,"circle" if kind=="CIRCLE" else "arc" if kind=="ARC" else "line")
        except Exception as exc:
            group.update(matcher={"status":"FAIL","exception_type":type(exc).__name__},
                         failure_reason=f"{type(exc).__name__}: {exc}")
            raise
        group.update(matcher=result,
            failure_reason=None if result["status"]=="PASS" else f"{role} {kind} sealed projection mismatch")
        require(result["status"]=="PASS",f"{role} {kind} sealed projection mismatch")
        results[kind]=result
    return results


def validate_drawing(native,evidence, *, require_roundtrip=True):
    diagnostics={"status":"RUNNING","views":{}}
    try:
        generated=normalize_r003_hlv_hlr(native["hlr_views"],native["hlv_views"],
                                       reviewed_projection_graph_digest=evidence["manifest_sha256"])
        results={}
        for role in ("FRONT","TOP","LEFT"):
            view_diagnostics={};diagnostics["views"][role]=view_diagnostics
            results[role]=_compare_drawing_view(native,generated,evidence["documents"][f"projection_{role.lower()}.json"],
                role,require_roundtrip=require_roundtrip,diagnostics=view_diagnostics)
        diagnostics["status"]="PASS"
        return {"status":"PASS","views":results,"sealed_manifest_sha256":evidence["manifest_sha256"],
                "comparison_diagnostics":diagnostics}
    except Exception as exc:
        diagnostics.update(status="FAIL",failure_reason=f"{type(exc).__name__}: {exc}")
        exc.drawing_diagnostics={"status":"FAIL","comparison_diagnostics":diagnostics}
        raise


def seal_output(folder, *, simulated=False, attempt_identity=None, contract_records=None):
    entries=[]
    for path in sorted(folder.rglob("*")):
        require(not path.is_symlink() and not path.is_junction() and path.resolve().is_relative_to(folder.resolve()),
                "artifact sealing path escape")
        if path.is_file():
            rel=path.relative_to(folder).as_posix()
            entry={"relative_path":rel,"size":path.stat().st_size,"sha256":digest(path)}
            if contract_records is not None:
                entry["role"],entry["semantic_category"]=artifact_role(rel)
            entries.append(entry)
    manifest={"artifacts":entries,"algorithm":"SHA256",
        "evidence_kind":"SIMULATED" if simulated else "QUALIFICATION_ARTIFACT_MANIFEST",
        "engineering_status":"NOT_VALIDATED","attempt_identity":attempt_identity}
    if contract_records is not None:
        report=read_json(folder/"final_report.json")
        reservation=read_document(contract_records["reservation_path"])
        manifest["engineering_status"]=report["engineering_status"]
        manifest.update({"qualification_contract_version":QUALIFICATION_CONTRACT_VERSION,
            "case_id":reservation["case_id"],"attempt_number":reservation["attempt_number"],
            "run_id":reservation["run_id"],"HEAD":reservation["current_head"],
            "final_state":report["status"],"reservation_digest":digest(contract_records["reservation_path"]),
            "authorization_digest":reservation["authorization_digest"],
            "c2a_evidence_manifest_sha256":reservation["c2a_evidence_manifest_sha256"],
            "backend_provenance_digest":reservation["backend_provenance_digest"],
            "runtime_support_provenance_digest":reservation["runtime_support_provenance_digest"],
            "semantic_categories":category_states(report,{e["role"] for e in entries})})
    write_exclusive(folder/"manifest.json",manifest)
    (folder/"manifest.sha256").write_text(digest(folder/"manifest.json")+"  manifest.json\n",encoding="ascii")
    if contract_records is not None:
        verify_bundle(folder,contract_records["reservation_path"],contract_records["guard_path"])


def run_formal(*,authorization,run_id,attempt_number,config_path=CONFIG,execution_input=None,
               adapter_factory=owned_adapter_factory,process_snapshot=_snapshot_solidworks_processes):
    require(os.environ.get("RUN_REAL_SOLIDWORKS")=="1","formal Q01 run requires explicit real gate")
    document=read_json(authorization) if isinstance(authorization,(str,Path)) else authorization
    require(isinstance(document,dict),"formal authorization document required")
    head=subprocess.check_output(["git","rev-parse","HEAD"],cwd=REPOSITORY,text=True).strip()
    harness_config=read_json(config_path)
    root=REPOSITORY/"artifacts/r004_real"
    history,allowed_files=history_view(root,"Q01",legacy_pin=harness_config["legacy_attempt_1"])
    verify_artifact_tree(root,"Q01",allowed_files)
    validate_authorization(document,case_id="Q01",attempt_number=attempt_number,current_head=head,history=history)
    status=subprocess.check_output(["git","status","--porcelain=v1","-uall"],cwd=REPOSITORY,text=True)
    tracked=subprocess.check_output(["git","diff","HEAD","--name-only"],cwd=REPOSITORY,text=True)
    validate_worktree_status(status,tracked,allowed_files,REPOSITORY)
    config,folder,input_path,reviewed,composition,planned,evidence,provenance=prepare(config_path,execution_input=execution_input)
    contract_preflight(version=config["qualification_contract_version"])
    require(not process_snapshot(),"preexisting SolidWorks session ambiguity; do not attach or close")
    binding={"runtime_remediation_commit":config["runtime_remediation_commit"],
        "c2a_evidence_manifest_sha256":evidence["manifest_sha256"],
        "backend_provenance_digest":document_digest({"backend":provenance["backend"],"files":provenance["files"]}),
        "runtime_support_provenance_digest":document_digest(provenance["runtime_support"])}
    provenance.update(binding,qualification_contract_version=QUALIFICATION_CONTRACT_VERSION,
                      attempt_governance_remediation_commit=head)
    output,reservation_path,reservation_digest=reserve_attempt(root,document,run_id,current_head=head,history=history,
                                                                  provenance=binding)
    return _run_prepared((config,folder,input_path,reviewed,composition,planned,evidence,provenance),output,
                         run_id=run_id,adapter_factory=adapter_factory,authorization=document,
                         reservation_path=reservation_path,reservation_digest=reservation_digest)


def cleanup_without_adapter(acquisition):
    if acquisition.get("launcher_called") is False and acquisition.get("resource_acquisition_started") is False:
        return {"status":"NOT_REQUIRED","applicability":"NOT_REQUIRED",
                "reason":"launcher not called; no owned resources acquired"}
    return {"status":"FAIL","applicability":"UNKNOWN","reason":"resource acquisition/owned PID unresolved"}


def _run_prepared(prepared,output, *, run_id,adapter_factory,simulated=False,authorization=None,
                  reservation_path=None,reservation_digest=None):
    config,folder,input_path,reviewed,composition,planned,evidence,provenance=prepared
    output=Path(output).resolve(strict=True)
    if not simulated:
        require(isinstance(authorization,dict) and os.environ.get("RUN_REAL_SOLIDWORKS")=="1",
                "private formal boundary requires real gate and authorization")
        require(output==REPOSITORY/"artifacts/r004_real/q01"/run_id,"formal output outside permanent budget root")
        require(reservation_path is not None,"attempt-local reservation required")
        reservation=read_document(reservation_path)
        require(reservation["run_id"]==run_id and reservation["attempt_number"]==authorization["attempt_number"],
                "reservation/authorization identity conflict")
        observed_head=subprocess.check_output(["git","rev-parse","HEAD"],cwd=REPOSITORY,text=True).strip()
        guard_path=create_execution_guard(reservation_path,document=authorization,current_head=observed_head,
                                          expected_run_id=run_id,expected_reservation_digest=reservation_digest)
    attempt_identity=({"case_id":authorization["case_id"],"attempt_number":authorization["attempt_number"],
                       "run_id":run_id,"authorization_digest":reservation["authorization_digest"],
                       "reservation_digest":digest(reservation_path),"authorized_head":reservation["authorized_head"]}
                      if not simulated else {"case_id":"Q01","attempt_number":0,"run_id":run_id,
                                              "authorization_kind":"SYNTHETIC_SIMULATION"})
    provenance={**provenance,"attempt_identity":attempt_identity}
    report={"status":"SIMULATED" if simulated else "FAIL","ENGINEERING_PASS":False,
        "engineering_status":"NOT_VALIDATED","Q01_REAL_RUN_COUNT":0,"formal_attempt_count":0 if simulated else 1,"REAL_SW_INVOKED":False,
        "FALLBACK_USED":False,"gates":{key:"NOT_REACHED" for key in GATE_ORDER},"generation_truth_reads":None,
        "gate_order":GATE_ORDER,"attempt_identity":attempt_identity,
        "QUALIFICATION_CONTRACT_PREFLIGHT":"NOT_APPLICABLE" if simulated else "PASS"}
    adapter=None
    drawing_steps=None
    native={"status":"NOT_RUN"}; brep={"status":"NOT_RUN"}; drawing={"status":"NOT_RUN"}; cleanup={"status":"NOT_RUN"}
    acquisition={"launcher_called":False,"resource_acquisition_started":False,"stage":"CONTROLLED_LAUNCH_PREFLIGHT"}
    cache_root=output/"runtime_cache" if simulated else attempt_cache_path(output,attempt_identity["attempt_number"])
    audit=RuntimeReadAudit(runtime=provenance["runtime_support"],cache_root=cache_root,write_roots=[output],allowed_roots=[REPOSITORY/"src",
        *[REPOSITORY/"experiments/three_view_reconstruction"/name for name in ("schemas","drawing","validation")],
        input_path,output,*[Path(config["backend_root"])/pin["relative_path"] for pin in config["source_pins"]]],forbidden_roots=[folder.parent])
    current_gate="CONTROLLED_LAUNCH_PREFLIGHT"
    def passed(*names):
        for name in names:
            report["gates"][name]="PASS"
            report["LAST_SUCCESSFUL_GATE"]=name
    passed("QUALIFICATION_ATTEMPT_PREFLIGHT","evidence_seal","backend_provenance","preexisting_session")
    try:
        # Persist the attempt binding before runtime bootstrap can fail. A sealed
        # pre-launch failure must still be readable as the parent of a new attempt.
        if not simulated:
            write_exclusive(output/"authorization.json",authorization)
            write_exclusive(output/"reservation.json",reservation)
        write_exclusive(output/"source_provenance.json",provenance)
        write_exclusive(output/"environment.json",{"harness_head":subprocess.check_output(["git","rev-parse","HEAD"],cwd=REPOSITORY,text=True).strip(),
            "python":sys.version,"timestamp_utc":datetime.now(timezone.utc).isoformat(),
            "attempt_identity":attempt_identity})
        write_exclusive(output/"execution_plan.json",planned.to_dict())
        audit.bootstrap()
        write_exclusive(output/"preflight.json",{"evidence_seal":"PASS","backend_provenance":"PASS",
            "QUALIFICATION_CONTRACT_PREFLIGHT":"NOT_APPLICABLE" if simulated else "PASS",
            "preexisting_processes":[],"formal_budget_for_attempt":1,"attempt_identity":attempt_identity})
        with audit.active():
            if adapter_factory is owned_adapter_factory:
                adapter=adapter_factory(config,run_id,live_authorized=not simulated,acquisition=acquisition,read_audit=audit)
            else:
                # An injected factory can only prove no acquisition in simulation.
                if not simulated: acquisition.update(launcher_called=None,resource_acquisition_started=None)
                adapter=adapter_factory(config,run_id,live_authorized=not simulated)
            require(adapter.simulated is simulated,"formal/simulated adapter mode mismatch")
            report["REAL_SW_INVOKED"]=not simulated
            report["Q01_REAL_RUN_COUNT"]=0 if simulated else 1
            passed("CONTROLLED_LAUNCH_PREFLIGHT","CONTROLLED_LAUNCH")
            current_gate="SESSION_BIND"
            if not simulated: adapter.connect()
            passed("SESSION_BIND")
            current_gate="pid_ownership"
            require(adapter.ownership_evidence["ownership_proven"] is True,"owned launch not proved")
            passed("pid_ownership")
            current_gate="canonical_operations"
            execution=adapter.execute(planned,composition,evidence_digests={e.evidence_id:e.content_sha256 for e in reviewed.evidence})
            passed("document_creation","canonical_operations","operation_checks","rebuild")
            current_gate="save"
            part_path=output/("simulated_part.json" if simulated else "part.sldprt")
            require(adapter.save_part(part_path) is True,"part save failed")
            passed("save")
            saved_digest=digest(part_path)
            current_gate="close"
            require(adapter.close_part() is True,"part close failed")
            passed("close")
            current_gate="reopen"
            adapter.reopen_part(part_path)
            passed("reopen","reopened_rebuild")
            require(digest(part_path)==saved_digest,"saved part changed during reopen")
            native=adapter.collect_native()
        require(native["evidence_kind"]==("SIMULATED" if simulated else "NATIVE"),"native/simulated evidence mismatch")
        regions={h.feature_id:h.region_id for h in composition.feature_hypotheses}
        current_gate="brep"
        require(validate_body_count(native["body"])["status"]=="PASS","body count failed")
        passed("body_count")
        brep=validate_native_geometry(native,evidence["documents"]["reviewed_geometry.json"],regions)
        passed("brep","persistent_references","ownership")
        current_gate="drawing_creation"
        with audit.active():
            drawing_steps=adapter.drawing_roundtrip(part_path,output/("simulated_drawing.json" if simulated else "drawing.slddrw"),native)
            initial_drawing=next(drawing_steps)
        passed("drawing_creation")
        write_exclusive(output/"drawing_initial_native_evidence.json",initial_drawing)
        current_gate="drawing_comparison"
        initial_validation=validate_drawing(initial_drawing,evidence,require_roundtrip=False)
        passed("physical_views","drawing_comparison")
        current_gate="drawing_save_reopen"
        with audit.active():
            drawing_native=next(drawing_steps)
        write_exclusive(output/"drawing_native_evidence.json",drawing_native)
        drawing=validate_drawing(drawing_native,evidence)
        drawing["initial_comparison"]=initial_validation
        passed("drawing_save_reopen")
        current_gate="truth_isolation"
        audit.validate_cache_members()
        isolation=audit.evidence()
        require(isolation["generation_truth_reads"]==0 and not isolation["denylist_violations"],"generation truth isolation violated")
        report["generation_truth_reads"]=0
        passed("truth_isolation")
    except Exception as exc:
        if current_gate=="CONTROLLED_LAUNCH_PREFLIGHT":
            current_gate=acquisition["stage"]
            if current_gate!="CONTROLLED_LAUNCH_PREFLIGHT":passed("CONTROLLED_LAUNCH_PREFLIGHT")
            if acquisition.get("launch_returned") is True:passed("CONTROLLED_LAUNCH")
        report["FIRST_FAILED_GATE"]=current_gate
        report["gates"][current_gate]="FAIL"
        report["failure_reason"]=f"{type(exc).__name__}: {exc}"
        if hasattr(exc,"drawing_diagnostics"):drawing=exc.drawing_diagnostics
        report["ownership_attempt"]=getattr(exc,"evidence",None)
        if not simulated and acquisition.get("launch_returned") is True:
            report["REAL_SW_INVOKED"]=True
            report["Q01_REAL_RUN_COUNT"]=1
        if not simulated and report["ownership_attempt"] and report["ownership_attempt"].get("launcher_pid"):
            report["REAL_SW_INVOKED"]=True
            report["Q01_REAL_RUN_COUNT"]=1
        elif not simulated and acquisition["launcher_called"] is not False and not acquisition.get("launch_returned"):
            report["REAL_SW_INVOKED"]=None
            report["Q01_REAL_RUN_COUNT"]=None
    finally:
        finalization={"drawing_steps_close_reached":"NOT_REACHED","generator_close_completed":"NOT_REACHED",
                      "generator_native_document_closure":"NOT_RECORDED",
                      "generator_close_proves_native_document_closure":False}
        if adapter is not None and isinstance(getattr(adapter,"diagnostics",None),dict):
            adapter.diagnostics["finalization"]=finalization
        if drawing_steps is not None:
            finalization["drawing_steps_close_reached"]="YES"
            try:
                drawing_steps.close()
                finalization["generator_close_completed"]="YES"
            except Exception as exc:
                finalization.update(generator_close_completed="NO",
                    exception={"type":type(exc).__name__,"message":str(exc)})
                raise
        report["generation_truth_reads"]=audit.evidence()["generation_truth_reads"]
        if adapter is not None:
            report["native_diagnostics"]=getattr(adapter,"diagnostics",None)
            report["gates"].update(getattr(adapter,"execution_gates",{}))
            report["gates"].update(getattr(adapter,"drawing_gates",{}))
            try:
                cleanup=adapter.cleanup_documents()
            except Exception as exc:
                cleanup={"status":"FAIL","reason":str(exc)}
            adapter_diagnostics=getattr(adapter,"diagnostics",None)
            cleanup["diagnostics"]={"finalization":finalization,
                "documents":adapter_diagnostics.get("cleanup",{}) if isinstance(adapter_diagnostics,dict) else {}}
            cleanup["applicability"]="REQUIRED"
            cleanup.setdefault("status","PASS" if cleanup.get("process_exited") is True and cleanup.get("com_released") is True else "FAIL")
        else:
            cleanup=cleanup_without_adapter(acquisition)
        report["resource_acquisition"]=acquisition
        report["CLEANUP_APPLICABILITY"]=cleanup["applicability"]
        report["CLEANUP_STATUS"]=cleanup["status"]
        success=(not report.get("failure_reason") and cleanup.get("process_exited") is True
                 and cleanup.get("com_released") is True and report["generation_truth_reads"]==0)
        report.update(status="SIMULATED" if simulated else "PASS" if success else "FAIL",
                      ENGINEERING_PASS=success and not simulated,simulation_checks_passed=success if simulated else None,
                      engineering_status="NOT_VALIDATED" if simulated or not success else "PASS")
        if not success and not report.get("failure_reason"):
            report["failure_reason"]="exact owned-session cleanup unconfirmed"
        report["gates"]["cleanup"]="NOT_REQUIRED" if cleanup["applicability"]=="NOT_REQUIRED" else "PASS" if cleanup.get("process_exited") is True and cleanup.get("com_released") is True else "FAIL"
        if report["gates"]["cleanup"]=="NOT_REQUIRED":
            report["gates"]["cleanup"]="NOT_APPLICABLE"
        report["gates"]["manifest"]="PASS"  # Any sealing exception prevents returning this report.
        sections={"execution_ledger.json":{"ledger":adapter.ledger if adapter else [],"engineering_status":"NOT_VALIDATED",
            "native_diagnostics":getattr(adapter,"diagnostics",None) if adapter else None},
            "native_evidence.json":native,"brep_validation.json":brep,
            "ownership_validation.json":{k:brep.get(k) for k in ("status","UNKNOWN","UNATTRIBUTED","MISATTRIBUTED","rows")},
            "drawing_validation.json":drawing,"truth_isolation.json":audit.evidence(),"cleanup.json":cleanup,"final_report.json":report}
        if simulated:
            # Every standalone simulation artifact is explicitly distinguishable
            # from native evidence, including preflight/environment/provenance.
            for path in output.glob("*.json"):
                document=read_json(path)
                document.update(evidence_kind="SIMULATED",engineering_status="NOT_VALIDATED")
                path.write_text(json.dumps(document,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
        for name,section in sections.items():
            if simulated: section={**section,"evidence_kind":"SIMULATED","engineering_status":"NOT_VALIDATED"}
            write_exclusive(output/name,section)
        seal_output(output,simulated=simulated,attempt_identity=attempt_identity,
                    contract_records=None if simulated else {"reservation_path":reservation_path,"guard_path":guard_path})
    return output,report


def main():
    parser=argparse.ArgumentParser(description="R004 Q01 qualification harness；默认只读 preparation")
    parser.add_argument("--config",type=Path,default=CONFIG)
    parser.add_argument("--execution-input",type=Path)
    parser.add_argument("--formal-authorization")
    parser.add_argument("--run-id")
    parser.add_argument("--attempt-number",type=int)
    args=parser.parse_args()
    if args.formal_authorization is None:
        prepared=prepare(args.config,execution_input=args.execution_input)
        print(json.dumps({"status":"PREPARED_NOT_EXECUTED","operations":len(prepared[5].modeling_plan.operations),
                          "REAL_SW_INVOKED":False,"Q01_REAL_RUN_COUNT":0},ensure_ascii=False))
        return 0
    folder,report=run_formal(authorization=args.formal_authorization,run_id=args.run_id,
                           attempt_number=args.attempt_number,config_path=args.config,
                           execution_input=args.execution_input)
    print(str(folder))
    return 0 if report["ENGINEERING_PASS"] else 1


if __name__=="__main__":
    raise SystemExit(main())
