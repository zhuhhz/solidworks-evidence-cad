"""Opt-in semantic slot annotations over the frozen R004 drawing boundary.

Import/construction are inert. Intent has no file loader or evaluator dependency.
The inherited executor still requires its existing qualification authorization.
"""
from dataclasses import asdict, dataclass, replace
import math

from experiments.three_view_reconstruction.validation.primitive_matcher import POSITION_TOLERANCE_MM
from .r004_drawing_comparison import normalize_primitive, SHEET_ANNOTATION
from .r004_execution_adapter import R004ExecutionAdapter, require, to_native
from .r004_execution_binding import _preflight, _vector, _dot, DatumCandidate
from .r003_execution_bridge import LINEAR_TOLERANCE_MM, AXIS_COMPONENT_TOLERANCE
from .r003_native_collectors import member


def _identity(value):
    return isinstance(value, str) and bool(value.strip())


@dataclass(frozen=True)
class SlotCenterlineIntent:
    operation_id: str
    source_feature_id: str
    host_feature_id: str
    evidence_ids: tuple[str, ...]
    major_axis: str
    feature_volume_center_mm: tuple[float, float, float]
    overall_length_mm: float
    width_mm: float
    profile_plane: DatumCandidate
    native_operation_identity: str | None = None

    def __post_init__(self):
        require(all(_identity(v) for v in (self.operation_id, self.source_feature_id, self.host_feature_id))
                and isinstance(self.evidence_ids, tuple) and bool(self.evidence_ids)
                and all(_identity(v) for v in self.evidence_ids), "slot intent provenance incomplete")
        require(isinstance(self.major_axis, str) and self.major_axis in {"X", "Y"},
                "slot intent major axis unsupported")
        require(_vector(self.feature_volume_center_mm, 3) and _vector((self.overall_length_mm, self.width_mm), 2)
                and self.overall_length_mm > self.width_mm > 0, "slot intent dimensions/center invalid")
        plane = self.profile_plane
        require(isinstance(plane, DatumCandidate) and plane.kind == "PLANAR_SUPPORT",
                "slot profile requires explicit planar support")
        require(all(_identity(v) for v in (plane.reference, plane.owner_reference,
                                          plane.support_region, plane.document_id)),
                "slot profile plane provenance incomplete")
        require(all(_vector(v, 3) for v in (plane.origin_m, plane.u, plane.v, plane.normal)),
                "slot profile plane coordinates unavailable")
        cross = (plane.u[1]*plane.v[2]-plane.u[2]*plane.v[1],
                 plane.u[2]*plane.v[0]-plane.u[0]*plane.v[2],
                 plane.u[0]*plane.v[1]-plane.u[1]*plane.v[0])
        require(all(abs(_dot(v, v)-1) <= AXIS_COMPONENT_TOLERANCE for v in (plane.u, plane.v, plane.normal))
                and abs(_dot(plane.u, plane.v)) <= AXIS_COMPONENT_TOLERANCE
                and all(abs(a-b) <= AXIS_COMPONENT_TOLERANCE for a,b in zip(cross, plane.normal)),
                "slot profile plane frame invalid")
        require(self.native_operation_identity is None or _identity(self.native_operation_identity),
                "slot intent native identity invalid")
        require(all(_vector(p, 3) for p in self.model_endpoints_mm), "slot intent endpoint overflow")

    @property
    def profile_center_mm(self):
        origin = tuple(v*1000 for v in self.profile_plane.origin_m)
        delta = tuple(a-b for a,b in zip(self.feature_volume_center_mm, origin))
        distance = _dot(delta, self.profile_plane.normal)
        return tuple(c-distance*n for c,n in zip(self.feature_volume_center_mm, self.profile_plane.normal))

    @property
    def annotation_anchor_center_mm(self):
        return self.profile_center_mm

    @property
    def model_endpoints_mm(self):
        axis = 0 if self.major_axis == "X" else 1
        return tuple(tuple(c + (sign * self.overall_length_mm / 2 if i == axis else 0)
                           for i, c in enumerate(self.annotation_anchor_center_mm)) for sign in (-1, 1))

    def to_dict(self):
        return {**asdict(self), "semantic_kind": "STRAIGHT_SLOT_OVERALL_CENTERLINE",
                "feature_type": "STRAIGHT_SLOT", "authority": "CANONICAL_OPERATION_PARAMETERS",
                "profile_center_mm": self.profile_center_mm,
                "annotation_anchor_center_mm": self.annotation_anchor_center_mm,
                "model_endpoints_mm": self.model_endpoints_mm}


def slot_centerline_intent(operation, requirements, *, profile_planes=()):
    """Project canonical volume center onto its uniquely bound explicit support."""
    require(operation is not None and operation.type == "cut_extrude_through_slot",
            "slot intent requires a slot operation")
    require(requirements is not None and requirements.operation_id == operation.operation_id
            and requirements.datum.feature_id == operation.source_feature_id,
            "slot intent operation/requirement provenance mismatch")
    require(isinstance(profile_planes, (tuple, list)) and len(profile_planes) == 1,
            "slot profile plane missing or ambiguous")
    plane = profile_planes[0]
    require(isinstance(plane, DatumCandidate) and plane.kind == requirements.datum.kind == "PLANAR_SUPPORT"
            and plane.support_region == requirements.datum.support_region
            and operation.sketch_plane == requirements.datum.token,
            "slot profile support provenance mismatch or non-planar support")
    expected_normal = {"Z+": (0,0,1), "Z-": (0,0,-1)}.get(requirements.datum.orientation)
    require(expected_normal is not None and _vector(plane.normal, 3)
            and all(abs(a-b) <= AXIS_COMPONENT_TOLERANCE for a,b in zip(plane.normal, expected_normal)),
            "slot profile support orientation outside bounded domain")
    p = operation.profile
    require(isinstance(p, dict) and {"region_bounds_mm", "major_axis", "overall_length_mm", "width_mm"} <= p.keys(),
            "slot intent semantic parameters missing")
    bounds = p["region_bounds_mm"]
    require(_vector(bounds, 6) and all(bounds[i] < bounds[i+1] for i in (0, 2, 4)),
            "slot intent cut bounds invalid")
    intent = SlotCenterlineIntent(operation.operation_id, operation.source_feature_id,
        requirements.datum.host_feature, tuple(requirements.evidence_ids), p["major_axis"],
        tuple(bounds[i] / 2 + bounds[i+1] / 2 for i in (0, 2, 4)),
        p["overall_length_mm"], p["width_mm"], plane)
    spans = (intent.overall_length_mm, intent.width_mm) if intent.major_axis == "X" else (
        intent.width_mm, intent.overall_length_mm)
    require(all(abs(bounds[2*i+1] - bounds[2*i] - spans[i]) <= LINEAR_TOLERANCE_MM for i in (0, 1)),
            "slot intent bounds contradict semantic dimensions")
    return intent


def project_slot_intent(intent, *, view_identity, frame, model_to_sheet):
    """Transform once with the existing boundary; de-scale only for geometric policy."""
    require(isinstance(intent, SlotCenterlineIntent) and _identity(view_identity),
            "typed slot intent and view identity required")
    sheet = [model_to_sheet(to_native(tuple(v / 1000 for v in p))) for p in intent.model_endpoints_mm]
    require(all(_vector(p, 3) for p in sheet), "slot projection unavailable")
    g = normalize_primitive(dict(x1=sheet[0][0]*1000, y1=sheet[0][1]*1000,
                                 x2=sheet[1][0]*1000, y2=sheet[1][1]*1000),
                            "line", frame, source_space=SHEET_ANNOTATION)
    points = ((g["x1"], g["y1"]), (g["x2"], g["y2"]))
    length = math.dist(*points)
    require(math.isfinite(length), "slot projected length invalid")
    degenerate = length <= POSITION_TOLERANCE_MM
    return {**intent.to_dict(), "view_identity": view_identity, "sheet_endpoints_m": sheet,
            "projected_endpoints_mm": points, "projected_length_mm": length,
            "state": "DEGENERATE" if degenerate else "NONDEGENERATE",
            "annotation_policy": "NO_LINE_ANNOTATION" if degenerate else "LINE_ANNOTATION"}


def _same_line(a, b):
    # Structural roundoff only; this is not a geometry acceptance tolerance.
    return any(all(math.dist(x, y) <= 1e-12 for x, y in zip(a, points)) for points in (b, b[::-1]))


class R004SlotDrawingAdapter(R004ExecutionAdapter):
    """Explicit adapter selection opts in; frozen Q01/E1 adapter stays untouched."""
    slot_drawing_intents = ()

    def execute(self, planned, composition, *, evidence_digests):
        canonical = _preflight(planned, composition)
        requirements = {r.operation_id: r for r in canonical.operation_requirements}
        result = super().execute(planned, composition, evidence_digests=evidence_digests)
        bound = []
        for op in canonical.modeling_plan.operations:
            if op.type != "cut_extrude_through_slot":
                continue
            commands = [c for c in self.commands if c.operation_id == op.operation_id
                        and c.source_feature_id == op.source_feature_id]
            require(len(commands) == 1 and commands[0].profile_kind == "STRAIGHT_SLOT",
                    "slot drawing bound profile command unavailable or ambiguous")
            intent = slot_centerline_intent(op, requirements[op.operation_id],
                                           profile_planes=(commands[0].datum,))
            rows = [r for r in self.ledger if r["operation_id"] == intent.operation_id
                    and r["source_feature_id"] == intent.source_feature_id]
            require(len(rows) == 1 and rows[0]["feature_type"] == "STRAIGHT_SLOT"
                    and rows[0]["host_feature_id"] == intent.host_feature_id,
                    "slot drawing native operation binding unavailable")
            ref = rows[0].get("native_feature_reference", {})
            require(ref.get("reference_type") == "FEATURE" and _identity(ref.get("encoded")),
                    "slot drawing persistent operation identity unavailable")
            bound.append(replace(intent, native_operation_identity=ref["encoded"]))
        self.slot_drawing_intents = tuple(bound)
        self.diagnostics["slot_drawing_intents"] = [i.to_dict() for i in bound]
        return result

    def _insert_centerlines(self, drawing, view, role, native):
        if not self.slot_drawing_intents:
            return super()._insert_centerlines(drawing, view, role, native)
        transform = member(view, "ModelToViewTransform")
        raw = member(transform, "ArrayData")
        ratio = member(view, "ScaleRatio")
        require(_vector(raw, 16) and _vector(ratio, 2) and all(v > 0 for v in ratio),
                "slot drawing view frame unavailable")
        frame = {"scale": ratio[0] / ratio[1], "native_transform": {"translation": raw[9:12]},
                 "sheet_position": member(view, "Position")}
        projected = [project_slot_intent(i, view_identity=role, frame=frame,
                     model_to_sheet=lambda p: self._math_point(p, transform)) for i in self.slot_drawing_intents]
        # Validate all semantics/projections before any annotation is created.
        super()._insert_centerlines(drawing, view, role, native)
        self.diagnostics.setdefault("slot_drawing_projections", {})[role] = projected
        drawable = [r for r in projected if r["state"] == "NONDEGENERATE"]
        if not drawable:
            return
        sketch = member(view, "GetSketch")
        require(sketch is not None, "slot drawing annotation sketch unavailable")
        inverse = member(member(sketch, "ModelToSketchTransform"), "Inverse")
        existing = []
        for segment in member(sketch, "GetSketchSegments") or []:
            if member(segment, "ConstructionGeometry") is True and member(segment, "GetType") == 0:
                points = []
                for name in ("GetStartPoint2", "GetEndPoint2"):
                    point = member(segment, name)
                    points.append(self._math_point([member(point, k) for k in ("X", "Y", "Z")], inverse)[:2])
                require(all(_vector(p, 2) for p in points), "existing annotation endpoints unavailable")
                existing.append(points)
        # Same reviewed boundary as the inherited cylinder annotation writer.
        from .r004_execution_adapter import _invoke_com_method
        require(_invoke_com_method(drawing, "ActivateView", str(member(view, "Name"))) is True,
                "slot drawing view activation failed")
        manager = member(drawing, "SketchManager")
        _invoke_com_method(manager, "InsertSketch", True)
        active = member(manager, "ActiveSketch")
        require(active is not None, "slot drawing active sketch unavailable")
        sheet_to_sketch = member(active, "ModelToSketchTransform")
        for row in drawable:
            sheet = row["sheet_endpoints_m"]
            if any(_same_line([p[:2] for p in sheet], previous) for previous in existing):
                row["emission"] = "ALREADY_PRESENT"
                continue
            local = [self._math_point(p, sheet_to_sketch) for p in sheet]
            require(all(_vector(p, 3) for p in local), "slot drawing sketch transform unavailable")
            require(_invoke_com_method(manager, "CreateCenterLine", *local[0], *local[1]) is not None,
                    "slot overall centerline creation failed")
            existing.append([p[:2] for p in sheet])
            row["emission"] = "CREATED"
        _invoke_com_method(manager, "InsertSketch", True)
