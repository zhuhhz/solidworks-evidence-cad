"""R003-only native evidence normalization. Importing this module does no COM work.

Every collector rejects unavailable native data. No expected geometry, display-name
lookup, benchmark truth, or synthetic PASS is used to fill a missing measurement.
"""

from __future__ import annotations

import base64
import math
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping

from .r003_execution_bridge import AXIS_COMPONENT_TOLERANCE


class NativeEvidenceUnavailable(RuntimeError):
    """The requested native fact could not be proven."""


def member(obj: Any, name: str, *args: Any) -> Any:
    if obj is None:
        raise NativeEvidenceUnavailable(f"{name}: native object missing")
    value = getattr(obj, name, None)
    if value is None:
        raise NativeEvidenceUnavailable(f"{name}: native member missing")
    # pywin32 CDispatch objects are callable via their default member even
    # when they are the *result* of a parameterless COM property. Calling the
    # returned object again yields DISP_E_MEMBERNOTFOUND on SW2024.
    return value(*args) if callable(value) and not hasattr(value, "_oleobj_") else value


def _vector(values: Any, length: int, label: str) -> list[float]:
    try:
        result = [float(value) for value in values]
    except (TypeError, ValueError) as exc:
        raise NativeEvidenceUnavailable(f"{label}: vector unavailable") from exc
    if len(result) != length or not all(math.isfinite(value) for value in result):
        raise NativeEvidenceUnavailable(f"{label}: invalid vector")
    return result


def capture_persistent_reference(extension: Any, entity: Any, reference_type: str) -> dict:
    if reference_type not in {"FEATURE", "FACE", "PLANE", "SKETCH", "EDGE"}:
        raise ValueError("unsupported persistent reference type")
    raw = member(extension, "GetPersistReference3", entity)
    try:
        encoded = base64.b64encode(bytes(int(value) & 0xFF for value in raw)).decode("ascii")
    except (TypeError, ValueError) as exc:
        raise NativeEvidenceUnavailable("persistent reference bytes unavailable") from exc
    if not encoded:
        raise NativeEvidenceUnavailable("empty persistent reference")
    return {"reference_type": reference_type, "encoded": encoded,
            "source": "IModelDocExtension.GetPersistReference3"}


def resolve_persistent_reference(extension: Any, record: Mapping[str, Any],
                                 *, resolver: Callable[[Any, bytes], tuple[Any, int]] | None = None) -> dict:
    """Inject the SW by-ref resolution shim; do not guess an object's native kind."""
    kind, encoded = record.get("reference_type"), record.get("encoded")
    if kind not in {"FEATURE", "FACE", "PLANE", "SKETCH", "EDGE"} or not encoded:
        raise NativeEvidenceUnavailable("typed persistent reference missing")
    try:
        entity, error = (resolver or _resolve_sw_reference)(
            extension, base64.b64decode(encoded, validate=True))
    except Exception as exc:
        raise NativeEvidenceUnavailable("persistent reference resolution failed") from exc
    if entity is None or type(error) is not int or error != 0:
        raise NativeEvidenceUnavailable(f"persistent reference unresolved: {error}")
    native_type = None
    if kind in {"FEATURE", "PLANE", "SKETCH"}:
        native_type = member(entity, "GetTypeName2")
        if not isinstance(native_type, str) or not native_type:
            raise NativeEvidenceUnavailable("resolved object is not a native feature")
        if kind == "PLANE" and native_type not in {"RefPlane", "ReferencePlane"}:
            raise NativeEvidenceUnavailable("resolved reference is not a native plane")
        if kind == "SKETCH" and native_type not in {"ProfileFeature", "3DProfileFeature", "Sketch"}:
            raise NativeEvidenceUnavailable("resolved reference is not a native sketch")
    elif kind == "FACE" and (not hasattr(entity, "GetSurface") or not hasattr(entity, "GetFeature")):
        raise NativeEvidenceUnavailable("resolved reference is not a native face")
    elif kind == "EDGE" and not hasattr(entity, "GetTwoAdjacentFaces2"):
        raise NativeEvidenceUnavailable("resolved reference is not a native edge")
    return {**record, "resolution_error": error, "resolved_type": kind,
            "native_type": native_type,
            "resolved": True, "native_entity": entity}


def _resolve_sw_reference(extension: Any, raw: bytes) -> tuple[Any, int]:
    """Lazy by-ref COM pattern copied from the local reference-plane pilot."""
    import pythoncom
    from win32com.client import VARIANT

    error = VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
    entity = member(extension, "GetObjectByPersistReference3", raw, error)
    return entity, int(error.value)


def _warning_slot():
    import pythoncom
    from win32com.client import VARIANT

    return VARIANT(pythoncom.VT_BYREF | pythoncom.VT_BOOL, False)


def collect_feature_error(feature: Any,
                          *, warning_slot_factory: Callable[[], Any] | None = None) -> dict:
    """The factory supplies a by-ref BOOL (the reference-plane pilot pattern)."""
    try:
        slot = (warning_slot_factory or _warning_slot)()
        code = member(feature, "GetErrorCode2", slot)
        if type(code) is not int:
            raise NativeEvidenceUnavailable("GetErrorCode2 returned no integer")
        warning = getattr(slot, "value", None)
        return {"api_status": "PASS", "error_code": code,
                "warning_flag": warning if type(warning) is bool else None,
                "source": "IFeature.GetErrorCode2(by-ref BOOL)"}
    except Exception as exc:
        return {"api_status": "INCOMPLETE", "error_code": None,
                "warning_flag": None, "reason": str(exc),
                "source": "IFeature.GetErrorCode2(by-ref BOOL)"}


def collect_body_count(model: Any, *, inventory_context: Mapping[str, Any] | None = None) -> dict:
    """Keep the legacy count schema; explicit offline context enables a sidecar.

    Retain the existing acquisition results, with all FACE calls before EDGE
    calls. The sidecar never scans the body again or reconstructs it from faces.
    """
    context = _body_inventory_context(model, inventory_context) if inventory_context is not None else None
    try:
        bodies = list(member(model, "GetBodies2", 0, False) or [])
        if context is not None:
            if len(bodies) != 1 or bodies[0] is None:
                raise NativeEvidenceUnavailable("inventory requires exactly one native solid body")
            _body_inventory_document(context, bodies[0])
            if context["body_kind"](bodies[0]) != "SOLID":
                raise NativeEvidenceUnavailable("unsupported native inventory body")
        face_sets = [list(member(body, "GetFaces") or []) for body in bodies]
        edge_sets = [list(member(body, "GetEdges") or []) for body in bodies]
        result = {"solid_body_count": len(bodies), "face_count": sum(map(len, face_sets)),
                  "edge_count": sum(map(len, edge_sets)), "source": "IPartDoc.GetBodies2/GetFaces/GetEdges"}
        if context is not None:
            result["typed_inventory"] = _capture_body_inventory(context, bodies[0], face_sets[0], edge_sets[0])
        return result
    except Exception as exc:
        if context is None or isinstance(exc, NativeEvidenceUnavailable):
            raise
        raise NativeEvidenceUnavailable("native body inventory acquisition failed") from exc


def _body_inventory_context(model: Any, supplied: Mapping[str, Any]) -> dict:
    """Freeze supplied observation association before the first enumeration."""
    if not isinstance(supplied, Mapping):
        raise NativeEvidenceUnavailable("native body inventory context missing")
    context = dict(supplied)
    if (context.get("source") != "OFFLINE_NATIVE_BODY_CONTEXT" or context.get("model") is not model
            or any(context.get(key) is None for key in ("model", "extension", "document_identity", "observation_scope"))
            or not all(callable(context.get(key)) for key in ("resolver", "entity_document", "body_kind"))):
        raise NativeEvidenceUnavailable("unsupported native body inventory context")
    for entity in (model, context["extension"]):
        _body_inventory_document(context, entity)
    return context


def _body_inventory_document(context: Mapping[str, Any], entity: Any) -> None:
    try:
        if entity is not None and context["entity_document"](entity) is context["document_identity"]:
            return
    except Exception as exc:
        raise NativeEvidenceUnavailable("native inventory document association unavailable") from exc
    raise NativeEvidenceUnavailable("foreign or missing native inventory entity")


def _body_inventory_reference(reference: Any, kind: str) -> str:
    if (not isinstance(reference, Mapping) or reference.get("reference_type") != kind
            or reference.get("source") != "IModelDocExtension.GetPersistReference3"):
        raise NativeEvidenceUnavailable("wrong typed body inventory reference")
    encoded = reference.get("encoded")
    try:
        raw = base64.b64decode(encoded, validate=True) if isinstance(encoded, str) else b""
        if raw and base64.b64encode(raw).decode("ascii") == encoded:
            return encoded
    except (ValueError, TypeError):
        pass
    raise NativeEvidenceUnavailable("malformed or noncanonical body inventory reference")


def _capture_body_inventory(context: Mapping[str, Any], body: Any, faces: list, edges: list) -> dict:
    rows, captured = {}, {}
    for kind, entities in (("FACE", faces), ("EDGE", edges)):
        if not entities:
            raise NativeEvidenceUnavailable("empty native body inventory")
        items, identities = [], {}
        for entity in entities:
            _body_inventory_document(context, entity)
            reference = capture_persistent_reference(context["extension"], entity, kind)
            token = _body_inventory_reference(reference, kind)
            resolved = resolve_persistent_reference(context["extension"], reference, resolver=context["resolver"])
            native = resolved["native_entity"]
            _body_inventory_document(context, native)
            # This bounded association is offline only; it does not qualify COM
            # wrapper equality across dispatches or a save/reopen boundary.
            if native is not entity or capture_persistent_reference(context["extension"], native, kind) != reference:
                raise NativeEvidenceUnavailable("native inventory reference identity substitution")
            if token in identities:
                raise NativeEvidenceUnavailable("duplicate typed native body identity")
            identities[token] = entity
            proof = {key: value for key, value in resolved.items() if key != "native_entity"}
            items.append({"reference": dict(reference), "resolution": proof,
                          "source_body": body, "enumeration_source": f"IBody2.Get{kind.title()}s"})
        items.sort(key=lambda item: item["reference"]["encoded"])
        rows[kind] = items
        captured[kind] = tuple((item["reference"]["encoded"], identities[item["reference"]["encoded"]])
                               for item in items)
    # Only immutable token/object snapshots are shared with mutable output rows.
    # This receipt records this acquisition; it offers no authentication claim.
    receipt = MappingProxyType({"source": "CONTROLLED_NATIVE_BODY_ACQUISITION", "model": context["model"],
        "extension": context["extension"], "source_body": body, "document_identity": context["document_identity"],
        "observation_scope": context["observation_scope"], "face_count": len(faces), "edge_count": len(edges),
        "faces": captured["FACE"], "edges": captured["EDGE"]})
    inventory = {"source": "NATIVE_BODY_TYPED_INVENTORY", "status": "PASS_OFFLINE", "body_kind": "SOLID",
        "source_body": body, "document_identity": context["document_identity"],
        "observation_scope": context["observation_scope"], "face_source": "IBody2.GetFaces",
        "edge_source": "IBody2.GetEdges", "face_count": len(faces), "edge_count": len(edges),
        "faces": rows["FACE"], "edges": rows["EDGE"], "capture_record": receipt}
    validate_typed_body_inventory(inventory)
    return inventory


def validate_typed_body_inventory(inventory: Mapping[str, Any]) -> None:
    """Check a local sidecar against its original body acquisition snapshot.

    No COM calls or FACE-incidence unions occur here. This is not a serialized
    transport verifier: replacing the producer and receipt together is outside
    this local observation contract.
    """
    if not isinstance(inventory, Mapping):
        raise NativeEvidenceUnavailable("typed native body inventory missing")
    receipt = inventory.get("capture_record")
    if (not isinstance(receipt, MappingProxyType) or receipt.get("source") != "CONTROLLED_NATIVE_BODY_ACQUISITION"
            or inventory.get("source") != "NATIVE_BODY_TYPED_INVENTORY"
            or inventory.get("status") != "PASS_OFFLINE" or inventory.get("body_kind") != "SOLID"
            or any(receipt.get(key) is None for key in ("model", "extension", "source_body", "document_identity", "observation_scope"))
            or any(inventory.get(key) is not receipt.get(key) for key in ("source_body", "document_identity", "observation_scope"))):
        raise NativeEvidenceUnavailable("unsupported or inconsistent native body acquisition record")
    for kind, field in (("FACE", "faces"), ("EDGE", "edges")):
        source = f"IBody2.Get{kind.title()}s"
        count_key = f"{kind.lower()}_count"
        count, original, items = inventory.get(count_key), receipt.get(count_key), inventory.get(field)
        captured = receipt.get(field)
        if (type(count) is not int or type(original) is not int or count <= 0 or count != original
                or not isinstance(items, list) or not isinstance(captured, tuple)
                or len(items) != count or len(captured) != original
                or inventory.get(f"{kind.lower()}_source") != source):
            raise NativeEvidenceUnavailable("native body inventory count/source mismatch")
        tokens = set()
        for item in items:
            if not isinstance(item, Mapping):
                raise NativeEvidenceUnavailable("native inventory row missing")
            reference = item.get("reference")
            token = _body_inventory_reference(reference, kind)
            proof = item.get("resolution")
            if (not isinstance(proof, Mapping) or proof.get("resolved") is not True
                    or type(proof.get("resolution_error")) is not int or proof.get("resolution_error") != 0
                    or dict(proof) != {**reference, "resolution_error": 0, "resolved_type": kind,
                                      "native_type": None, "resolved": True}
                    or item.get("source_body") is not receipt["source_body"] or item.get("enumeration_source") != source):
                raise NativeEvidenceUnavailable("native inventory resolution/lineage mismatch")
            if token in tokens:
                raise NativeEvidenceUnavailable("duplicate typed native body identity")
            tokens.add(token)
        if any(entity is None for _, entity in captured) or tokens != {token for token, _ in captured}:
            raise NativeEvidenceUnavailable("native inventory differs from acquired identities")


def collect_rebuild_evidence(model: Any, features: Mapping[str, Any],
                             expected_refs: Mapping[str, str], *,
                             warning_slot_factory: Callable[[], Any],
                             resolve_sketch: Callable[[str, Any], bool],
                             resolve_plane: Callable[[str, Any], bool],
                             resolve_dependencies: Callable[[str, Any], bool]) -> dict:
    """Collect strict rows, requiring explicit native reference checks.

    The resolution callbacks must inspect native refs, not display names. A
    caller cannot omit them and silently get `True` for required evidence.
    """
    if set(features) != set(expected_refs) or len(features) != 3:
        raise NativeEvidenceUnavailable("exact three-feature map required")
    rebuild = member(model, "ForceRebuild3", False)
    extension = member(model, "Extension")
    rows = []
    for source_id, feature in features.items():
        ref = capture_persistent_reference(extension, feature, "FEATURE")["encoded"]
        error = collect_feature_error(feature, warning_slot_factory=warning_slot_factory)
        if error["api_status"] != "PASS":
            raise NativeEvidenceUnavailable("feature error code unavailable")
        suppressed = member(feature, "IsSuppressed")
        native_type = member(feature, "GetTypeName2")
        if type(suppressed) is not bool or not native_type:
            raise NativeEvidenceUnavailable("native suppression/type unknown")
        checks = {"sketch_resolved": resolve_sketch(source_id, feature),
                  "plane_resolved": resolve_plane(source_id, feature),
                  "dependencies_resolved": resolve_dependencies(source_id, feature)}
        if any(type(value) is not bool for value in checks.values()):
            raise NativeEvidenceUnavailable("native dependency resolution not boolean")
        rows.append({"source_feature_id": source_id, "persistent_reference": ref,
                     "native_type": str(native_type), "error_code": error["error_code"],
                     "warning_flag": error["warning_flag"], "suppressed": suppressed,
                     "feature_resolved": ref == expected_refs[source_id],
                     **checks})
    return {"rebuild_result": rebuild, "features": rows,
            "source": "IModelDoc2.ForceRebuild3 + IFeature.GetErrorCode2/IsSuppressed"}


def native_profile_feature(extension: Any, feature: Any) -> tuple[Any, str]:
    """Find exactly one native ProfileFeature under an operation, never by name/order."""
    candidates = list(member(feature, "GetParents") or [])
    first = getattr(feature, "GetFirstSubFeature", None)
    child = first() if callable(first) and not hasattr(first, "_oleobj_") else first
    while child is not None:
        candidates.append(child)
        following = getattr(child, "GetNextSubFeature", None)
        child = (following() if callable(following)
                 and not hasattr(following, "_oleobj_") else following)
    profiles: dict[str, Any] = {}
    for candidate in candidates:
        if str(member(candidate, "GetTypeName2")) == "ProfileFeature":
            ref = capture_persistent_reference(extension, candidate, "SKETCH")["encoded"]
            profiles[ref] = candidate
    if len(profiles) != 1:
        raise NativeEvidenceUnavailable(f"exactly one native ProfileFeature required, found {len(profiles)}")
    ref, profile = next(iter(profiles.items()))
    return profile, ref


def collect_sketch_support(model: Any, feature: Any, *,
                           expected_sketch_ref: str,
                           expected_parent_ref: str,
                           selected_support: Mapping[str, Any],
                           support_geometry: Mapping[str, Any],
                           owner_refs: Mapping[str, str],
                           resolver: Callable[[Any, bytes], tuple[Any, int]] | None = None) -> dict:
    """Reopened sketch lineage plus the exact selected native support reference.

    For face-based sketches, the creation-time selected face reference is
    separately resolved after reopen. The profile's parent feature must also
    resolve to the intended Base/Boss feature. These independent facts form a
    bounded substitute when a direct sketch-support-face accessor is absent.
    """
    extension = member(model, "Extension")
    profile, sketch_ref = native_profile_feature(extension, feature)
    if sketch_ref != expected_sketch_ref:
        raise NativeEvidenceUnavailable("reopened profile identity changed")
    parents = list(member(profile, "GetParents") or [])
    parent_rows = [
        {"native_type": str(member(parent, "GetTypeName2")),
         "persistent_reference": capture_persistent_reference(extension, parent, "FEATURE")["encoded"]}
        for parent in parents
    ]
    matching = [row for row in parent_rows if row["persistent_reference"] == expected_parent_ref]
    if len(matching) != 1:
        raise NativeEvidenceUnavailable("intended native parent dependency unresolved")
    kind = selected_support.get("reference_type")
    if kind not in {"FACE", "PLANE"}:
        raise NativeEvidenceUnavailable("selected support must be native face or plane")
    support = resolve_persistent_reference(extension, selected_support, resolver=resolver)
    native_support = support.pop("native_entity")
    actual_plane = (collect_plane_face(native_support, extension, owner_refs)
                    if kind == "FACE" else collect_reference_plane(
                        native_support, extension, name="native-sketch-support"))
    expected_normal = support_geometry.get("normal")
    expected_z = support_geometry.get("offset_z_mm")
    if (not isinstance(expected_normal, (list, tuple)) or len(expected_normal) != 3
            or type(expected_z) not in (int, float)
            or abs(actual_plane["offset_z_mm"] - expected_z) > 0.05
            or any(abs(a - b) > 1e-6 for a, b in zip(actual_plane["normal"], expected_normal))):
        raise NativeEvidenceUnavailable("reopened sketch support plane geometry changed")
    return {"status": "PASS", "sketch_persistent_reference": sketch_ref,
            "parent_feature_references": parent_rows,
            "selected_support": support, "support_plane": actual_plane,
            "source": "ProfileFeature.GetParents + selected support persistent resolution"}


def collect_reopened_r003_dependencies(model: Any, feature_refs: Mapping[str, str],
                                       sketch_refs: Mapping[str, str],
                                       support_refs: Mapping[str, Mapping[str, Any]],
                                       support_geometry: Mapping[str, Mapping[str, Any]],
                                       ordered_source_ids: list[str], *,
                                       resolver: Callable[[Any, bytes], tuple[Any, int]]) -> dict:
    """Resolve the exact saved Base/Boss/Hole identities and native lineage."""
    ids = ordered_source_ids
    if (len(ids) != 3 or len(set(ids)) != 3 or set(ids) != set(feature_refs)
            or set(ids) != set(sketch_refs) or set(ids) != set(support_refs)
            or set(ids) != set(support_geometry)):
        raise NativeEvidenceUnavailable("complete three-operation reopen ledger required")
    extension = member(model, "Extension")
    features = {}
    lineage = {}
    for index, source_id in enumerate(ids):
        record = {"reference_type": "FEATURE", "encoded": feature_refs[source_id]}
        resolved = resolve_persistent_reference(extension, record, resolver=resolver)
        feature = resolved["native_entity"]
        features[source_id] = feature
        parent_ref = (support_refs[source_id]["encoded"] if index == 0
                      else feature_refs[ids[index - 1]])
        lineage[source_id] = collect_sketch_support(
            model, feature, expected_sketch_ref=sketch_refs[source_id],
            expected_parent_ref=parent_ref,
            selected_support=support_refs[source_id],
            support_geometry=support_geometry[source_id],
            owner_refs=feature_refs, resolver=resolver)
    return {"status": "PASS", "features": features, "lineage": lineage,
            "source": "GetObjectByPersistReference3 + ProfileFeature.GetParents + support geometry"}


def collect_signed_part_bounds(model: Any) -> list[float]:
    values = _vector(member(model, "GetPartBox", True), 6, "GetPartBox")
    # Native order is x-min,y-min,z-min,x-max,y-max,z-max. Keep it intact.
    if any(values[i] > values[i + 3] for i in range(3)):
        raise NativeEvidenceUnavailable("inverted native part bounds")
    return [round(value * 1000.0, 6) for value in values]


def _face_bounds_mm(face: Any) -> list[float]:
    bounds = _vector(member(face, "GetBox"), 6, "IFace2.GetBox")
    if any(bounds[i] > bounds[i + 3] for i in range(3)):
        raise NativeEvidenceUnavailable("inverted native face bounds")
    return [round(value * 1000.0, 6) for value in bounds]


def _union_bounds(rows: list[Mapping[str, Any]]) -> list[float]:
    if not rows:
        raise NativeEvidenceUnavailable("native region has no faces")
    boxes = [row["bounds_mm"] for row in rows]
    return [min(box[i] for box in boxes) for i in range(3)] + [
        max(box[i] for box in boxes) for i in range(3, 6)]


def classify_r003_face_role(face: Any, owner_id: str | None, *,
                            base_id: str, boss_id: str, hole_id: str,
                            interface_z_mm: float) -> str | None:
    """Role requires exact native owner and bounded surface family."""
    if owner_id is None:
        return None
    surface = member(face, "GetSurface")
    if owner_id == hole_id and bool(member(surface, "IsCylinder")):
        return "HOLE_WALL" if bool(member(face, "FaceInSurfaceSense")) else None
    if owner_id in {base_id, boss_id} and bool(member(surface, "IsPlane")):
        bounds = _face_bounds_mm(face)
        if owner_id == base_id:
            return ("BASE_INTERFACE_OR_TRANSITION" if
                    abs(bounds[2] - interface_z_mm) <= 0.05
                    and abs(bounds[5] - interface_z_mm) <= 0.05
                    else "BASE_EXTERIOR")
        return "BOSS_EXTERIOR"
    return None


def collect_r003_brep(model: Any, feature_refs: Mapping[str, str], *,
                      base_id: str, boss_id: str, hole_id: str,
                      through_all_created: bool) -> dict:
    """Strictly bounded 6 Base planes + 5 Boss planes + 1 Hole wall family.

    The plan supplies only semantic feature IDs and creation end condition.
    Every bound, surface, edge adjacency and owner comes from the final body.
    If the topology family is not observable, fail closed rather than infer it.
    """
    if (set(feature_refs) != {base_id, boss_id, hole_id}
            or len({base_id, boss_id, hole_id}) != 3):
        raise NativeEvidenceUnavailable("exact Base/Boss/Hole reference map required")
    bodies = list(member(model, "GetBodies2", 0, False) or [])
    if len(bodies) != 1:
        raise NativeEvidenceUnavailable("R003 requires exactly one final solid body")
    extension = member(model, "Extension")
    faces = list(member(bodies[0], "GetFaces") or [])
    reverse = {ref: source_id for source_id, ref in feature_refs.items()}
    rows: list[dict] = []
    for face in faces:
        ref = capture_persistent_reference(extension, face, "FACE")["encoded"]
        owner = member(face, "GetFeature")
        owner_ref = capture_persistent_reference(extension, owner, "FEATURE")["encoded"]
        owner_id = reverse.get(owner_ref)
        surface = member(face, "GetSurface")
        kind = ("CYLINDER" if bool(member(surface, "IsCylinder")) else
                "PLANE" if bool(member(surface, "IsPlane")) else "OTHER")
        rows.append({"ref": ref, "owner_ref": owner_ref, "owner_id": owner_id,
                     "kind": kind, "bounds_mm": _face_bounds_mm(face), "native_face": face})
    base = [row for row in rows if row["owner_id"] == base_id and row["kind"] == "PLANE"]
    boss = [row for row in rows if row["owner_id"] == boss_id and row["kind"] == "PLANE"]
    cylinders = [row for row in rows if row["kind"] == "CYLINDER"]
    hole_walls = [row for row in cylinders if row["owner_id"] == hole_id]
    unexpected = [row for row in rows if row not in base + boss + hole_walls]
    if (len(base) != 6 or len(boss) != 5 or len(hole_walls) != 1
            or len(rows) != 12 or unexpected):
        raise NativeEvidenceUnavailable("complete bounded 6+5+1 face accounting unavailable")
    base_bounds, boss_bounds = _union_bounds(base), _union_bounds(boss)
    overall_bounds = collect_signed_part_bounds(model)
    if (base_bounds[:2] != overall_bounds[:2]
            or base_bounds[3:5] != overall_bounds[3:5]
            or abs(base_bounds[5] - boss_bounds[2]) > 0.05
            or abs(base_bounds[2] - overall_bounds[2]) > 0.05
            or abs(boss_bounds[5] - overall_bounds[5]) > 0.05):
        raise NativeEvidenceUnavailable("native region bounds do not form bounded step")
    interface_z = base_bounds[5]
    base_top = [row for row in base if abs(row["bounds_mm"][2] - interface_z) <= 0.05
                and abs(row["bounds_mm"][5] - interface_z) <= 0.05]
    bottom = [row for row in base if abs(row["bounds_mm"][2] - overall_bounds[2]) <= 0.05
              and abs(row["bounds_mm"][5] - overall_bounds[2]) <= 0.05]
    top = [row for row in boss if abs(row["bounds_mm"][2] - overall_bounds[5]) <= 0.05
           and abs(row["bounds_mm"][5] - overall_bounds[5]) <= 0.05]
    if len(base_top) != 1 or len(bottom) != 1 or len(top) != 1:
        raise NativeEvidenceUnavailable("unique interface/bottom/top exterior planes unavailable")
    shared_edges = set()
    for edge in member(base_top[0]["native_face"], "GetEdges") or []:
        adjacent = list(member(edge, "GetTwoAdjacentFaces2") or [])
        refs = {capture_persistent_reference(extension, adjacent_face, "FACE")["encoded"]
                for adjacent_face in adjacent if adjacent_face is not None}
        if base_top[0]["ref"] in refs and any(row["ref"] in refs for row in boss):
            shared_edges.add(capture_persistent_reference(extension, edge, "EDGE")["encoded"])
    if len(shared_edges) < 4:
        raise NativeEvidenceUnavailable("native Base/Boss interface continuity unproven")
    wall = hole_walls[0]
    surface = member(wall["native_face"], "GetSurface")
    params = _vector(member(surface, "CylinderParams"), 7, "CylinderParams")
    if not bool(member(wall["native_face"], "FaceInSurfaceSense")):
        raise NativeEvidenceUnavailable("hole wall is not an interior cylinder")
    if (abs(params[3]) > 1e-6 or abs(params[4]) > 1e-6
            or abs(abs(params[5]) - 1.0) > 1e-6):
        raise NativeEvidenceUnavailable("hole cylinder axis is not model Z")
    opening = classify_r003_hole_openings(wall["native_face"],
        bottom_face_ref=bottom[0]["ref"], top_face_ref=top[0]["ref"],
        extension=extension)
    hole_bounds = wall["bounds_mm"]
    cap_faces = [row["ref"] for row in rows
                 if row["owner_id"] == hole_id and row["kind"] == "PLANE"]
    if cap_faces:
        raise NativeEvidenceUnavailable("native planar Hole cap exists")
    confirmed = (through_all_created is True and opening["bottom_opening"]
                 and opening["top_opening"] and opening["blind_cap_absent"]
                 and opening["wall_continuity"]
                 and abs(hole_bounds[2] - overall_bounds[2]) <= 0.05
                 and abs(hole_bounds[5] - overall_bounds[5]) <= 0.05)
    if not confirmed:
        raise NativeEvidenceUnavailable("native full-span hole proof incomplete")
    hole = {"through_all_created": True, "cylinder_present": True,
            "center_mm": [round(params[0] * 1000.0, 6), round(params[1] * 1000.0, 6)],
            "diameter_mm": round(abs(params[6]) * 2000.0, 6),
            "axis": params[3:6], "axial_min_z_mm": hole_bounds[2],
            "axial_max_z_mm": hole_bounds[5],
            "bottom_opening": True, "top_opening": True,
            "blind_cap_absent": True, "wall_continuity": True,
            "through_state": "confirmed", "native_owner_feature_id": hole_id,
            "wall_persistent_reference": wall["ref"],
            "edge_evidence": opening["edge_evidence"]}
    account = [{"face_ref": row["ref"], "native_owner_feature_id": row["owner_id"],
                "surface_type": row["kind"], "bounds_mm": row["bounds_mm"]}
               for row in rows]
    return {"overall_envelope_mm": overall_bounds,
            "lower_envelope_mm": base_bounds, "upper_envelope_mm": boss_bounds,
            "part_bounds": dict(zip(("xmin", "ymin", "zmin", "xmax", "ymax", "zmax"),
                                    overall_bounds)),
            "base_region": dict(zip(("xmin", "ymin", "zmin", "xmax", "ymax", "zmax"),
                                    base_bounds)),
            "boss_region": dict(zip(("xmin", "ymin", "zmin", "xmax", "ymax", "zmax"),
                                    boss_bounds)),
            "interface": {"z_mm": interface_z, "shared_native_edges": sorted(shared_edges)},
            "fused_interface": True, "cylindrical_wall_count": len(cylinders),
            "extra_geometry_count": 0, "hole": hole,
            "face_accounting": account,
            "source": "GetBodies2/GetPartBox/GetFaces/GetBox/GetFeature/GetTwoAdjacentFaces2"}


def collect_plane_face(face: Any, extension: Any, owner_refs: Mapping[str, str]) -> dict:
    """Read a planar IFace2 in model coordinates, without a face-name heuristic."""
    surface = member(face, "GetSurface")
    if member(surface, "IsPlane") is not True:
        raise NativeEvidenceUnavailable("selected native face is not planar")
    params = _vector(member(surface, "PlaneParams"), 6, "PlaneParams")
    surface_normal = params[:3]
    length = math.sqrt(sum(value * value for value in surface_normal))
    if length <= 0:
        raise NativeEvidenceUnavailable("zero plane normal")
    surface_normal = [value / length for value in surface_normal]
    # PlaneParams is the underlying surface orientation; it can disagree with
    # the outward face normal. IFace2.Normal is the native face-side authority.
    normal = _vector(member(face, "Normal"), 3, "IFace2.Normal")
    face_length = math.sqrt(sum(value * value for value in normal))
    if face_length <= 0:
        raise NativeEvidenceUnavailable("zero face normal")
    normal = [value / face_length for value in normal]
    if abs(abs(sum(a * b for a, b in zip(normal, surface_normal))) - 1.0) > 1e-6:
        raise NativeEvidenceUnavailable("face/surface normal not parallel")
    origin_mm = [value * 1000.0 for value in params[3:6]]
    owner = member(face, "GetFeature")
    owner_ref = capture_persistent_reference(extension, owner, "FEATURE")["encoded"]
    reverse = {ref: source_id for source_id, ref in owner_refs.items()}
    if owner_ref not in reverse:
        raise NativeEvidenceUnavailable("plane face owner not mapped to source feature")
    return {"native_reference": capture_persistent_reference(extension, face, "FACE")["encoded"],
            "owner_source_feature_id": reverse[owner_ref],
            "owner_persistent_reference": owner_ref,
            "origin_mm": origin_mm, "normal": normal,
            "surface_normal_raw": surface_normal,
            "offset_z_mm": origin_mm[2],
            "signed_plane_offset_mm": -sum(n * x for n, x in zip(normal, origin_mm)),
            "source": "IFace2.GetSurface/PlaneParams/GetFeature"}


def collect_reference_plane(feature: Any, extension: Any, *, name: str) -> dict:
    """Normalize the pilot's GetRefPlaneParams coordinate conversion."""
    specific = member(feature, "GetSpecificFeature2")
    params = _vector(member(specific, "GetRefPlaneParams"), 9, "GetRefPlaneParams")
    q, x, normal = params[:3], params[3:6], params[6:9]
    xlen = math.sqrt(sum(v * v for v in x))
    nlen = math.sqrt(sum(v * v for v in normal))
    if xlen <= 0 or nlen <= 0:
        raise NativeEvidenceUnavailable("reference-plane basis invalid")
    x = [v / xlen for v in x]
    normal = [v / nlen for v in normal]
    y = [normal[1] * x[2] - normal[2] * x[1],
         normal[2] * x[0] - normal[0] * x[2],
         normal[0] * x[1] - normal[1] * x[0]]
    origin_mm = [-1000.0 * (q[0] * x[i] + q[1] * y[i] + q[2] * normal[i])
                 for i in range(3)]
    return {"name": name,
            "native_reference": capture_persistent_reference(extension, feature, "PLANE")["encoded"],
            "origin_mm": origin_mm, "normal": normal,
            "offset_z_mm": origin_mm[2],
            "signed_plane_offset_mm": -sum(n * v for n, v in zip(normal, origin_mm)),
            "source": "IRefPlane.GetRefPlaneParams + native feature reference"}


def collect_r003_ownership(model: Any, feature_refs: Mapping[str, str],
                           *, role_for_face: Callable[[Any, str], str | None]) -> dict:
    """All-face native attribution; caller supplies bounded geometric role policy."""
    extension = member(model, "Extension")
    reverse = {ref: source_id for source_id, ref in feature_refs.items()}
    if len(reverse) != len(feature_refs) or not feature_refs:
        raise NativeEvidenceUnavailable("feature reference map incomplete")
    rows = []
    unknown = unattributed = misattributed = 0
    for body in member(model, "GetBodies2", 0, False) or []:
        for face in member(body, "GetFaces") or []:
            try:
                face_ref = capture_persistent_reference(extension, face, "FACE")["encoded"]
                native_owner = member(face, "GetFeature")
                owner_ref = capture_persistent_reference(extension, native_owner, "FEATURE")["encoded"]
                owner_id = reverse.get(owner_ref)
            except NativeEvidenceUnavailable:
                face_ref = owner_ref = owner_id = None
            if face_ref is None:
                unknown += 1
            if owner_id is None:
                unattributed += 1
            role = role_for_face(face, owner_id) if owner_id else None
            source_id = owner_id if role is not None else None
            if role is None:
                misattributed += 1
            rows.append({"persistent_reference": face_ref,
                         "owner_persistent_reference": owner_ref,
                         "native_owner_feature_id": owner_id,
                         "source_feature_id": source_id, "role": role,
                         "ownership": "API_EXACT" if owner_id and role else "OWNERSHIP_UNRESOLVED"})
    return {"unknown": unknown, "unattributed": unattributed,
            "misattributed": misattributed, "total_face_count": len(rows),
            "persistent_mapping_resolved": len(rows) > 0 and not (unknown or unattributed or misattributed),
            "feature_references": dict(feature_refs), "rows": rows,
            "source": "IFace2.GetFeature + GetPersistReference3"}


def collect_bounded_r003_ownership(model: Any, feature_refs: Mapping[str, str], *,
                                   base_id: str, boss_id: str, hole_id: str,
                                   interface_z_mm: float) -> dict:
    """R003-specific whole-body role accounting; no B001 role fallback."""
    if set(feature_refs) != {base_id, boss_id, hole_id}:
        raise NativeEvidenceUnavailable("exact three native feature refs required")
    bodies = list(member(model, "GetBodies2", 0, False) or [])
    if len(bodies) != 1:
        raise NativeEvidenceUnavailable("R003 ownership requires one final solid body")
    report = collect_r003_ownership(model, feature_refs,
        role_for_face=lambda face, owner: classify_r003_face_role(
            face, owner, base_id=base_id, boss_id=boss_id, hole_id=hole_id,
            interface_z_mm=interface_z_mm))
    native_count = len(list(member(bodies[0], "GetFaces") or []))
    if report["total_face_count"] != native_count:
        raise NativeEvidenceUnavailable("native final-face accounting incomplete")
    return report


def classify_r003_hole_openings(wall: Any, *, bottom_face_ref: str,
                                top_face_ref: str, extension: Any) -> dict:
    """Require two actual wall edges adjacent to the two exterior planes.

    `GetTwoAdjacentFaces2` is used elsewhere in the repo's native F011/F015
    probes. A missing adjacency is UNKNOWN, never an inferred opening.
    """
    if not bottom_face_ref or not top_face_ref or bottom_face_ref == top_face_ref:
        raise NativeEvidenceUnavailable("two distinct exterior planes required")
    wall_ref = capture_persistent_reference(extension, wall, "FACE")["encoded"]
    found = set()
    edge_rows = []
    for edge in member(wall, "GetEdges") or []:
        adjacent = list(member(edge, "GetTwoAdjacentFaces2") or [])
        refs = {capture_persistent_reference(extension, face, "FACE")["encoded"]
                for face in adjacent if face is not None}
        if wall_ref not in refs:
            raise NativeEvidenceUnavailable("wall edge lacks native wall adjacency")
        plane_refs = refs & {bottom_face_ref, top_face_ref}
        found.update(plane_refs)
        edge_rows.append({"edge_ref": capture_persistent_reference(extension, edge, "EDGE")["encoded"],
                          "adjacent_face_refs": sorted(refs)})
    return {"bottom_opening": bottom_face_ref in found,
            "top_opening": top_face_ref in found,
            "blind_cap_absent": bool(edge_rows) and all(
                row["adjacent_face_refs"] == sorted([wall_ref, exterior])
                for row in edge_rows for exterior in row["adjacent_face_refs"]
                if exterior in {bottom_face_ref, top_face_ref})
                and len(edge_rows) == 2 and found == {bottom_face_ref, top_face_ref},
            "wall_continuity": len(edge_rows) == 2,
            "edge_evidence": edge_rows,
            "source": "IFace2.GetEdges + IEdge.GetTwoAdjacentFaces2"}


def normalize_native_view_transform(view: Any, *, expected_role: str,
                                    model_reference: str,
                                    projection_standard: str) -> dict:
    """Validate the native 16-value ModelToViewTransform without MathPoint COM.

    SOLIDWORKS stores the images of model X/Y/Z as three row axis sets.
    The downstream contract stores view-coordinate components as rows, so its
    3x3 representation is the transpose of those *documented* axis sets.
    """
    from experiments.three_view_reconstruction.drawing.view_orientation import canonicalize

    identity = str(member(view, "GetOrientationName"))
    if not identity:
        raise NativeEvidenceUnavailable("native orientation identity unavailable")
    if expected_role not in {"FRONT", "TOP", "LEFT"}:
        raise NativeEvidenceUnavailable("unsupported expected physical view role")
    try:
        if canonicalize(identity).canonical_role.value != expected_role:
            raise NativeEvidenceUnavailable("native orientation metadata contradicts expected role")
        frame = canonicalize("*" + expected_role)
    except (TypeError, ValueError) as exc:
        raise NativeEvidenceUnavailable("native orientation metadata unavailable") from exc
    transform = member(view, "ModelToViewTransform")
    raw = member(transform, "ArrayData")
    if (not isinstance(raw, (list, tuple)) or len(raw) != 16
            or any(type(value) not in (int, float) or not math.isfinite(value)
                   for value in raw)):
        raise NativeEvidenceUnavailable("native MathTransform must contain 16 finite numbers")
    values = [float(value) for value in raw]
    axes = [values[0:3], values[3:6], values[6:9]]
    columns = [[axes[row][col] for row in range(3)] for col in range(3)]
    norm = lambda vector: math.sqrt(sum(value * value for value in vector))
    pairs = ((0, 1), (0, 2), (1, 2))
    row_norms = [norm(axis) for axis in axes]
    column_norms = [norm(column) for column in columns]
    row_dots = [sum(axes[i][k] * axes[j][k] for k in range(3)) for i, j in pairs]
    column_dots = [sum(columns[i][k] * columns[j][k] for k in range(3)) for i, j in pairs]
    cross = [axes[0][1] * axes[1][2] - axes[0][2] * axes[1][1],
             axes[0][2] * axes[1][0] - axes[0][0] * axes[1][2],
             axes[0][0] * axes[1][1] - axes[0][1] * axes[1][0]]
    determinant = sum(cross[k] * axes[2][k] for k in range(3))
    tolerance = AXIS_COMPONENT_TOLERANCE
    if (any(abs(value - 1.0) > tolerance for value in row_norms + column_norms)
            or any(abs(value) > tolerance for value in row_dots + column_dots)
            or abs(abs(determinant) - 1.0) > tolerance
            or any(abs(cross[k] - determinant * axes[2][k]) > tolerance for k in range(3))
            or values[12] == 0.0):
        raise NativeEvidenceUnavailable("native MathTransform rotation/scale invalid")
    expected_mapping = [[frame.right[index], frame.up[index], frame.normal[index]]
                        for index in range(3)]
    if any(abs(axes[i][j] - expected_mapping[i][j]) > tolerance
           for i in range(3) for j in range(3)):
        raise NativeEvidenceUnavailable("native MathTransform contradicts physical view axis mapping")
    position = _vector(member(view, "Position"), 2, "view sheet position")
    scale_values = list(member(view, "ScaleRatio"))
    if len(scale_values) != 2 or scale_values[1] == 0:
        raise NativeEvidenceUnavailable("native view scale unavailable")
    scale = float(scale_values[0]) / float(scale_values[1])
    if not math.isfinite(scale) or scale <= 0:
        raise NativeEvidenceUnavailable("invalid native view scale")
    if not model_reference or projection_standard != "THIRD_ANGLE":
        raise NativeEvidenceUnavailable("model/projection provenance unavailable")
    native_transform = {"api_source": "IView.ModelToViewTransform", "raw_array": values,
                        "array_length": 16, "rotation": axes, "translation": values[9:12],
                        "scale": values[12], "unused_homogeneous_components": values[13:16],
                        "row_norms": row_norms, "column_norms": column_norms,
                        "row_dot_products": row_dots, "column_dot_products": column_dots,
                        "cross_product_norm": norm(cross), "determinant": determinant,
                        "rotation_convention": "SOLIDWORKS_ROW_AXIS_SETS",
                        "axis_mapping": {"model_x_in_view": axes[0], "model_y_in_view": axes[1],
                                         "model_z_in_view": axes[2]},
                        "expected_axis_mapping": expected_mapping,
                        "mathpoint_basis_path": "RETIRED_FROM_NORMATIVE_VALIDATION"}
    return {"native_identity": identity, "normal": list(frame.normal),
            "model_to_view_transform": [columns[i][j] for i in range(3) for j in range(3)],
            "transform_axes": {"right": list(frame.right), "up": list(frame.up),
                               "normal": list(frame.normal)},
            "projection_direction": list(frame.normal), "scale": scale,
            "sheet_position": position, "model_reference": model_reference,
            "projection_standard": projection_standard, "native_transform": native_transform,
            "source": "IView.ModelToViewTransform.ArrayData direct rotation validation"}


def normalize_r003_hlv_hlr(hlr: Mapping[str, Mapping[str, Any]],
                           hlv: Mapping[str, Mapping[str, Any]], *,
                           reviewed_projection_graph_digest: str) -> dict:
    """Physical-role HLV-minus-HLR primitive adapter; never LEFT->RIGHT.

    The caller must supply native extractions sharing one *fixed* drawing
    coordinate frame. The generic extractor's independently shifted bounding
    boxes are not sufficient until that alignment is supplied/proven.
    """
    from experiments.three_view_reconstruction.drawing.view_orientation import canonicalize
    from experiments.three_view_reconstruction.validation.primitive_matcher import (
        match, spline_difference, support_difference,
    )

    roles = {"FRONT", "TOP", "LEFT"}
    if set(hlr) != roles or set(hlv) != roles:
        raise NativeEvidenceUnavailable("physical FRONT/TOP/LEFT extraction required")
    if not isinstance(reviewed_projection_graph_digest, str) or len(reviewed_projection_graph_digest) != 64:
        raise NativeEvidenceUnavailable("reviewed ProjectionGraph digest unavailable")
    result: dict[str, Any] = {"reviewed_projection_graph_digest": reviewed_projection_graph_digest}
    for role in ("FRONT", "TOP", "LEFT"):
        hidden_row, all_row = hlr[role], hlv[role]
        for row in (hidden_row, all_row):
            try:
                native_role = canonicalize(row["native_identity"]).canonical_role.value
            except (KeyError, ValueError, TypeError) as exc:
                raise NativeEvidenceUnavailable("native drawing identity unavailable") from exc
            if native_role != role:
                raise NativeEvidenceUnavailable("physical view role mismatch")
            for key in ("lines", "circles", "arcs", "frame_origin_mm", "scale", "sheet_position"):
                if key not in row:
                    raise NativeEvidenceUnavailable(f"{role} {key} extraction missing")
            if row.get("unclassified_polylines"):
                raise NativeEvidenceUnavailable("unclassified projected primitives")
            if not isinstance(row.get("native_splines", []), list):
                raise NativeEvidenceUnavailable("native spline evidence unavailable")
        if (hidden_row["frame_origin_mm"] != all_row["frame_origin_mm"]
                or hidden_row["scale"] != all_row["scale"]
                or hidden_row["sheet_position"] != all_row["sheet_position"]):
            raise NativeEvidenceUnavailable("HLR/HLV coordinate frame not stable")
        if hidden_row["arcs"] != all_row["arcs"]:
            raise NativeEvidenceUnavailable("hidden native arcs are not yet classified")
        difference = support_difference(hidden_row["lines"], all_row["lines"])
        if difference["status"] != "PASS":
            raise NativeEvidenceUnavailable("HLR visible support changed in HLV")
        try:
            spline_diff = spline_difference(hidden_row.get("native_splines", []),
                                            all_row.get("native_splines", []))
        except (KeyError, TypeError, ValueError) as exc:
            raise NativeEvidenceUnavailable("malformed or unsupported native spline") from exc
        if spline_diff["status"] != "PASS":
            raise NativeEvidenceUnavailable("ambiguous or missing native spline match")
        visible_circles = hidden_row["circles"]
        hidden_circles = [circle for circle in all_row["circles"]
                          if not any(match([circle], [candidate], "circle")["status"] == "PASS"
                                     for candidate in visible_circles)]
        result[role] = {
            "visible_lines": list(hidden_row["lines"]),
            "hidden_lines": [row["geometry"] for row in difference["candidates"]],
            "visible_splines": list(hidden_row.get("native_splines", [])),
            "hidden_splines": spline_diff["candidates"],
            "spline_matching": spline_diff,
            "native_hlr_evidence": hidden_row,
            "circles": list(visible_circles) + hidden_circles,
            "arcs": list(hidden_row["arcs"]),
            "unresolved_discriminating_primitives": 0,
            "native_identity": hidden_row["native_identity"],
            "source": "physical-role HLV_MINUS_HLR native extraction",
        }
    return result


def _r003_native_projected_primitives(raw: Any) -> dict:
    """Decode only bounded native line/circle/arc polyline records.

    Layout follows IView.GetPolyLinesAndCurves and the repository's frozen
    drawing extractor. Unknown/partial records are retained as unclassified
    and therefore block roundtrip validation.
    """
    try:
        values = _vector(raw, len(raw), "GetPolyLinesAndCurves")
    except TypeError as exc:
        raise NativeEvidenceUnavailable("native projection records unavailable") from exc
    lines, circles, arcs, native_splines, unclassified = [], [], [], [], []
    index = 0
    raw_count = 0
    while index < len(values):
        if index + 2 > len(values):
            raise NativeEvidenceUnavailable("truncated native projection record")
        kind, geometry_count = int(values[index]), int(values[index + 1])
        if kind == 3 and (values[index] != 3 or values[index + 1] != geometry_count):
            raise NativeEvidenceUnavailable("malformed native spline header")
        index += 2
        if geometry_count < 0 or index + geometry_count + 7 > len(values):
            raise NativeEvidenceUnavailable("invalid native projection record length")
        geometry = values[index:index + geometry_count]
        line_attributes = values[index + geometry_count:index + geometry_count + 6]
        index += geometry_count + 6  # geometry + six line attributes
        point_count = int(values[index])
        if kind == 3 and values[index] != point_count:
            raise NativeEvidenceUnavailable("malformed native spline sample count")
        index += 1
        if point_count < 0 or index + point_count * 3 > len(values):
            raise NativeEvidenceUnavailable("invalid native projection point count")
        sample_xyz = [values[index + 3 * i:index + 3 * i + 3]
                      for i in range(point_count)]
        points = [(values[index + 3 * i] * 1000.0,
                   values[index + 3 * i + 1] * 1000.0)
                  for i in range(point_count)]
        index += point_count * 3
        raw_count += 1
        if kind == 0 and len(points) == 2:
            lines.append({"x1": points[0][0], "y1": points[0][1],
                          "x2": points[1][0], "y2": points[1][1]})
        elif kind == 1 and len(geometry) >= 9 and len(points) >= 8:
            cx, cy = geometry[0] * 1000.0, geometry[1] * 1000.0
            radii = [math.hypot(x - cx, y - cy) for x, y in points]
            radius = sum(radii) / len(radii)
            if radius <= 0 or max(abs(value - radius) for value in radii) > 0.05:
                unclassified.append({"kind": kind, "points": points})
            elif math.dist(points[0], points[-1]) <= 0.02:
                circles.append({"x": cx, "y": cy, "diameter": radius * 2})
            else:
                angles = [math.degrees(math.atan2(y - cy, x - cx)) % 360
                          for x, y in points]
                signed = sum(((b - a + 180) % 360) - 180
                             for a, b in zip(angles, angles[1:]))
                arcs.append({"x": cx, "y": cy, "radius": radius,
                             "start_angle_deg": angles[0],
                             "end_angle_deg": angles[-1],
                             "sweep_direction": "CCW" if signed >= 0 else "CW"})
        elif kind == 3:
            def exact_int(value: float, label: str) -> int:
                if value != int(value):
                    raise NativeEvidenceUnavailable(f"malformed native spline {label}")
                return int(value)

            if len(geometry) < 5:
                raise NativeEvidenceUnavailable("malformed native spline geometry header")
            degree, rational, dims, control_count, knot_count = (
                exact_int(value, label) for value, label in zip(
                    geometry[:5], ("degree", "rational", "dimensions",
                                   "control count", "knot count")))
            if (degree < 0 or rational not in (0, 1) or
                    dims != (4 if rational else 3) or control_count <= 0 or knot_count <= 0 or
                    len(geometry) != 5 + control_count * dims + knot_count):
                raise NativeEvidenceUnavailable("malformed native spline geometry length/fields")
            control_flat = geometry[5:5 + control_count * dims]
            native_splines.append({
                "raw_kind": 3, "raw_geometry_count": geometry_count,
                "degree": degree, "rational": rational, "num_dims": dims,
                "num_control_points": control_count, "num_knots": knot_count,
                "control_points": [control_flat[i:i + dims]
                                   for i in range(0, len(control_flat), dims)],
                "knots": geometry[5 + control_count * dims:],
                "line_attributes": line_attributes,
                "num_sample_points": point_count, "sample_points": sample_xyz,
                "coordinate_unit": "m", "structural_validity": "PASS",
            })
        else:
            unclassified.append({"kind": kind, "points": points})
    return {"lines": lines, "circles": circles, "arcs": arcs,
            "native_splines": native_splines,
            "unclassified_polylines": unclassified, "raw_count": raw_count}


def r003_native_completeness(row: Mapping[str, Any]) -> dict:
    """Native-only gate; spline evidence is retained, never coerced to line."""
    counts = {name: len(row[name]) for name in (
        "lines", "circles", "arcs", "native_splines", "unclassified_polylines")}
    classified = sum(counts.values())
    raw_count = row["raw_count"]
    supported = sum(counts[name] for name in ("lines", "circles", "arcs", "native_splines"))
    return {"raw_count": raw_count, "parsed_count": classified,
            "classified_count": classified, "normalized_count": classified,
            "line_count": counts["lines"], "circle_count": counts["circles"],
            "arc_count": counts["arcs"], "ellipse_count": 0,
            "spline_count": counts["native_splines"],
            "unknown_count": counts["unclassified_polylines"], "malformed_count": 0,
            "supported_native_count": supported,
            "status": "PASS" if raw_count == classified and supported > 0 and
            counts["unclassified_polylines"] == 0 else "FAIL"}


def collect_r003_native_view_modes(drawing: Any, view: Any, *,
                                   role: str, model_path: str) -> tuple[dict, dict]:
    """Extract HLR/HLV from the same reopened native view and fixed frame."""
    from experiments.three_view_reconstruction.drawing.view_orientation import canonicalize

    if role not in {"FRONT", "TOP", "LEFT"}:
        raise NativeEvidenceUnavailable("unsupported R003 physical view role")
    identity = str(member(view, "GetOrientationName"))
    if not identity or canonicalize(identity).canonical_role.value != role:
        raise NativeEvidenceUnavailable("native physical view identity unavailable/mismatched")
    referenced = str(member(view, "GetReferencedModelName"))
    if Path(referenced).name.casefold() != Path(model_path).name.casefold():
        raise NativeEvidenceUnavailable("reopened view references another model")
    referenced_document = member(view, "ReferencedDocument")
    native_path = str(member(referenced_document, "GetPathName"))
    if not native_path or Path(native_path).resolve() != Path(model_path).resolve():
        raise NativeEvidenceUnavailable("reopened view model path identity unproven")
    rows = []
    for mode, label in ((2, "HLR"), (1, "HLV")):
        if not bool(member(view, "SetDisplayMode3", False, mode, False, True)):
            raise NativeEvidenceUnavailable(f"native {label} display mode rejected")
        member(drawing, "ForceRebuild3", False)
        if str(member(view, "GetOrientationName")) != identity:
            raise NativeEvidenceUnavailable("physical view identity changed across HLR/HLV")
        ratio = _vector(member(view, "ScaleRatio"), 2, "drawing view scale")
        if ratio[1] == 0:
            raise NativeEvidenceUnavailable("drawing view scale denominator zero")
        raw = member(view, "GetPolyLinesAndCurves", 1)
        row = _r003_native_projected_primitives(raw)
        row.update({"native_identity": identity, "frame_origin_mm": [0.0, 0.0],
                    "scale": ratio[0] / ratio[1],
                    "sheet_position": _vector(member(view, "Position"), 2,
                                               "drawing view sheet position"),
                    "mode": label,
                    "native_display_mode_request": mode,
                    "raw_polyline_values": list(raw), "raw_coordinate_unit": "m",
                    "source": "IView.SetDisplayMode3 + GetPolyLinesAndCurves"})
        rows.append(row)
    if any(rows[0][key] != rows[1][key]
           for key in ("native_identity", "frame_origin_mm", "scale", "sheet_position")):
        raise NativeEvidenceUnavailable("HLR/HLV native frame changed")
    return rows[0], rows[1]
