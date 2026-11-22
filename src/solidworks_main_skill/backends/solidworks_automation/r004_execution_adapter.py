"""Bounded external primitive adapter over a proven-owned native session.

Import and construction are inert. Activation belongs to the qualification
runner; this module never reads evaluator files or resolves an external path.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import base64
from hashlib import sha256
import itertools
import inspect
import math
from pathlib import Path
import re

from .r004_execution_binding import DatumCandidate, SupportRequest, _command, _preflight, _vector, _dot
from .r003_execution_bridge import canonical_digest, LINEAR_TOLERANCE_MM, AXIS_COMPONENT_TOLERANCE
from .r003_native_adapter import R003SolidWorksSessionAdapter, R003OwnedNativeSession, SessionOwnershipUnproven, verify_owned_identity, create_r003_physical_views
from .r004_cleanup_diagnostics import document_inventory, inventory_title_state, observe_cleanup
from .r004_document_lifecycle_diagnostics import (
    lifecycle_event, cleanup_entry_identity, observe_close_call, record_cleanup_close,
)
from .r003_native_collectors import (
    NativeEvidenceUnavailable, member, capture_persistent_reference, resolve_persistent_reference,
    native_profile_feature, collect_feature_error, collect_plane_face, collect_reference_plane,
    collect_body_count, collect_signed_part_bounds, collect_r003_native_view_modes,
    normalize_native_view_transform, normalize_r003_hlv_hlr,
)


def require(condition, reason):
    if not condition:
        raise NativeEvidenceUnavailable(reason)


def _drawing_title_after_model_views(adapter, drawing, token, original_title):
    """SW can rename an unsaved drawing when its first model view is inserted."""
    title = member(drawing, "GetTitle")
    require(isinstance(title, str) and bool(title), "native drawing title unavailable")
    if title != original_title:
        require(title not in adapter._owned_titles, "automatic drawing title collides with another owned document")
        lifecycle_event(adapter, "TITLE_AFTER_MODEL_VIEWS", token, document=drawing,
                        previous_native_title=original_title)
        # Retain the old alias until a native close proves this logical
        # document absent; cleanup already verifies that historical lineage.
        adapter._owned_titles.add(title)
        lifecycle_event(adapter, "ALIAS_ADD", token, title=title)
    return title


def _slot_arguments(command):
    """Map bound end centres/full width to the audited SW2024 slot contract."""
    datum=command.datum
    require(command.operation_type=="cut_extrude_through_slot"
            and command.profile_kind=="STRAIGHT_SLOT","slot requires a bounded cut command")
    require(len(command.points_m)==2 and all(_vector(p,2) for p in command.points_m)
            and _vector((command.size_m,command.depth_m),2)
            and command.size_m>0 and command.depth_m>0,"slot dimensions must be finite and positive")
    require(datum.reference and datum.owner_reference and datum.support_region
            and datum.document_id and datum.kind=="PLANAR_SUPPORT","slot host/support unavailable")
    require(all(_vector(p,3) for p in (datum.origin_m,datum.u,datum.v,datum.normal))
            and _vector(datum.footprint_m,4),"slot sketch frame unavailable")
    cross=(datum.u[1]*datum.v[2]-datum.u[2]*datum.v[1],
           datum.u[2]*datum.v[0]-datum.u[0]*datum.v[2],
           datum.u[0]*datum.v[1]-datum.u[1]*datum.v[0])
    require(all(abs(_dot(axis,axis)-1)<=AXIS_COMPONENT_TOLERANCE
                for axis in (datum.u,datum.v,datum.normal))
            and abs(_dot(datum.u,datum.v))<=AXIS_COMPONENT_TOLERANCE
            and all(abs(a-b)<=AXIS_COMPONENT_TOLERANCE for a,b in zip(cross,datum.normal)),
            "slot sketch frame must be orthonormal and right handed")
    a,b=command.points_m
    spacing=math.dist(a,b)
    require(math.isfinite(spacing) and spacing>0,"slot overall length must exceed width")
    delta=tuple((b[0]-a[0])*datum.u[i]+(b[1]-a[1])*datum.v[i] for i in range(3))
    major=tuple(x/spacing for x in delta)
    require(any(all(abs(x-y)<=AXIS_COMPONENT_TOLERANCE for x,y in zip(major,axis))
                for axis in ((1,0,0),(0,1,0))),"slot model major axis must be X or Y")
    require(_vector(command.model_direction,3)
            and all(abs(a-b)<=AXIS_COMPONENT_TOLERANCE for a,b in zip(command.model_direction,(0,0,-1)))
            and all(abs(a-b)<=AXIS_COMPONENT_TOLERANCE for a,b in zip(datum.normal,(0,0,1)))
            and _dot(command.model_direction,datum.normal)<0,"slot cut must enter the host along -Z")
    radius=command.size_m/2
    tol=LINEAR_TOLERANCE_MM/1000
    for point in command.points_m:
        world=tuple(datum.origin_m[i]+point[0]*datum.u[i]+point[1]*datum.v[i] for i in range(3))
        require(all(math.isfinite(x) for x in world)
                and all(datum.footprint_m[2*i]-tol<=world[i]-radius
                        and world[i]+radius<=datum.footprint_m[2*i+1]+tol for i in (0,1)),
                "slot profile exceeds bound support footprint")
    # SlotLengthType is an integer enum, not a dimension. The pinned external
    # sketch_slot puts a floating width in this position; do not rely on coercion.
    return (0,0,command.size_m,*a,0.0,*b,0.0,0.0,0.0,0.0,1,False)


# SW2024 sldworks.tlb (LIBID 83A33D31..., version 32.0), independently
# confirmed against installed SolidWorks.Interop.sldworks.IFeature metadata.
# IFeature is dual: pywin32's useIID can safely wrap its IDispatch base without
# a second QI to the object's default automation interface (possibly ISketch).
_IFEATURE_IID = "{83A33D38-27C5-11CE-BFD4-00400513BB57}"


def _native_sketch_to_feature(native_sketch, diagnostic):
    import pythoncom
    import pywintypes
    from win32com.client import dynamic
    diagnostic.update(mechanism="QUERYINTERFACE",interface_iid=_IFEATURE_IID,
                      wrapper_strategy="DUAL_IFEATURE_AS_IDISPATCH",status="FAIL")
    try:
        ole=_local_attribute(native_sketch,"_oleobj_")
        require(native_sketch is not None and ole is not None,"native sketch COM interface missing")
        # The second argument selects the Python wrapper for an unregistered
        # dual IID; it does not change the requested native IFeature interface.
        interface=ole.QueryInterface(pywintypes.IID(_IFEATURE_IID),pythoncom.IID_IDispatch)
        require(interface is not None,"IFeature QueryInterface returned None")
        diagnostic["interface_python_type"]=f"{type(interface).__module__}.{type(interface).__qualname__}"
        if not isinstance(interface,pythoncom.TypeIIDs[pythoncom.IID_IDispatch]):
            interface=interface.QueryInterface(pythoncom.IID_IDispatch)
            require(interface is not None,"IFeature automation interface missing")
        feature=dynamic.Dispatch(interface)  # No makepy, EnsureDispatch or gen_py.
        require(_local_attribute(feature,"_oleobj_") is not None,"IFeature dispatch wrapper missing")
        kind=_invoke_com_method(feature,"GetTypeName2",diagnostic=diagnostic.setdefault("feature_type_method",{}))
        require(kind=="ProfileFeature","converted interface is not a 2D sketch Feature")
        diagnostic.update(status="PASS",native_feature_type=kind,
                          iunknown_diagnostic=runtime_pointer_diagnostic(feature))
        return feature
    except Exception as exc:
        diagnostic["error"]=f"{type(exc).__name__}: {exc}"
        raise NativeEvidenceUnavailable("native sketch to IFeature conversion failed") from exc


@dataclass
class _NativeSketchHandoff:
    native_sketch: object
    native_feature: object
    sketch_persistent_reference: bytes
    feature_persistent_reference: bytes | None
    document: object
    session_identity: object
    run_id: str
    support_reference: str
    capture_stage: str
    capture_source: str
    diagnostic: dict
    operation_id: str | None = None


def _double_safearray_point(values):
    """Supply the three doubles required by IMathUtility.CreatePoint."""
    coordinates=list(values)
    require(len(coordinates)==3,"CreatePoint requires three coordinates")
    try:
        coordinates=[float(value) for value in coordinates]
    except (TypeError,ValueError,OverflowError) as exc:
        raise NativeEvidenceUnavailable("CreatePoint coordinate is not a double") from exc
    require(all(math.isfinite(value) for value in coordinates),"CreatePoint coordinates must be finite")
    import pythoncom
    from win32com.client import VARIANT
    return VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8,coordinates)


def _math_object_diagnostic(row, label, obj):
    row[label+"_is_none"]=obj is None
    row[label+"_python_type"]=f"{type(obj).__module__}.{type(obj).__qualname__}"
    row[label+"_has_oleobj"]=hasattr(obj,"_oleobj_")
    try:
        row[label+"_repr"]=repr(obj)[:256]  # Diagnostic only; addresses are never identity.
    except Exception as exc:
        row[label+"_repr_error"]=f"{type(exc).__name__}: {exc}"


def _math_array_diagnostic(row, label, value=None, error=None):
    row[label+"_available"]=False
    row[label+"_raw"]=None
    row[label+"_length"]=None
    row[label+"_finite"]=False
    if error is not None:
        row[label+"_error"]=f"{type(error).__name__}: {error}"
        return
    try:
        numbers=[float(x) for x in value]
        row[label+"_length"]=len(numbers)
        row[label+"_finite"]=all(math.isfinite(x) for x in numbers)
        if row[label+"_finite"]:
            row[label+"_raw"]=numbers
            row[label+"_available"]=True
        else:
            row[label+"_error"]="non-finite ArrayData"
    except (TypeError,ValueError,OverflowError) as exc:
        row[label+"_error"]=f"{type(exc).__name__}: {exc}"


def _local_attribute(obj, name, default=None):
    # Bypass __getattr__: inspecting stored dispatch metadata must not issue COM calls.
    try:
        return object.__getattribute__(obj,name)
    except Exception:
        return default


def _dispatch_metadata(owner, name):
    row={"typeinfo_status":"UNKNOWN","generated_wrapper_used":"UNKNOWN"}
    try:
        module=type(owner).__module__
        if module.startswith("win32com.gen_py."):
            row["generated_wrapper_used"]="YES"
        elif module in ("win32com.client","win32com.client.dynamic"):
            row["generated_wrapper_used"]="NO"
        lazy=_local_attribute(owner,"_lazydata_")
        row["lazydata_present"]=lazy is not None
        if isinstance(lazy,tuple) and lazy and lazy[0] is not None:
            row["typeinfo_status"]="AVAILABLE"
        ole=_local_attribute(owner,"_olerepr_")
        row["olerepr_present"]=ole is not None
        for key in ("mapFuncs","propMap","propMapGet"):
            mapping=_local_attribute(ole,key)
            entry={"member_present":"UNKNOWN"}
            row[key]=entry
            if not isinstance(mapping,dict):
                entry["metadata_status"]="NOT_AVAILABLE"
                continue
            entry["member_present"]="YES" if name in mapping else "NO"
            value=mapping.get(name)
            if value is None:
                continue
            entry["dispid"]=_local_attribute(value,"dispid")
            desc=_local_attribute(value,"desc")
            entry["funcdesc_derived"]="NO"
            if desc is not None:
                # pywin32 FUNCDESC has invkind, args and rettype; VARDESC does not.
                kind=_local_attribute(desc,"invkind")
                if kind is not None:
                    entry.update(funcdesc_derived="YES",invoke_kind=kind,
                                 argument_count=len(_local_attribute(desc,"args",())),
                                 return_description=repr(_local_attribute(desc,"rettype"))[:256])
                    row["typeinfo_status"]="AVAILABLE"
    except Exception as exc:
        row["metadata_error"]=f"{type(exc).__name__}: {exc}"
    return row


def _resolution_object_diagnostic(row, label, obj):
    try:
        row[label+"_python_type"]=f"{type(obj).__module__}.{type(obj).__qualname__}"
        row[label+"_is_none"]=obj is None
        absent=object()
        row[label+"_has_oleobj"]=_local_attribute(obj,"_oleobj_",absent) is not absent
        row[label+"_repr"]=repr(obj)[:256]
    except Exception as exc:
        row[label+"_metadata_error"]=f"{type(exc).__name__}: {exc}"


def _diagnostic_member(obj, name, row, *args):
    """Resolve once and preserve frozen member() dispatch, including ignored args."""
    row.update(member_name=name,argument_count=len(args),
               argument_python_types=[f"{type(a).__module__}.{type(a).__qualname__}" for a in args],
               argument_vartype=[_local_attribute(a,"varianttype") for a in args],
               arguments_applied="UNKNOWN",branch_taken="RESOLUTION_UNKNOWN")
    _resolution_object_diagnostic(row,"owner",obj)
    row["dispatch_metadata_before"]=_dispatch_metadata(obj,name)
    try:
        if obj is None:
            raise NativeEvidenceUnavailable(f"{name}: native object missing")
        resolved=getattr(obj,name,None)  # The only resolution of this member.
        _resolution_object_diagnostic(row,"resolved",resolved)
        row["dispatch_metadata"]=_dispatch_metadata(obj,name)
        if resolved is None:
            raise NativeEvidenceUnavailable(f"{name}: native member missing")
        is_callable=callable(resolved)
        row["resolved_callable"]=is_callable
        # Match the frozen short circuit exactly; never force a COM default call.
        call=is_callable and not hasattr(resolved,"_oleobj_")
        if is_callable:
            row["resolved_has_oleobj"]=not call
        row["branch_taken"]="CALL_CALLABLE" if call else "RETURN_RESOLVED_OBJECT"
        row["arguments_applied"]="YES" if call else "NO" if args else "NOT_APPLICABLE"
        result=resolved(*args) if call else resolved
        _resolution_object_diagnostic(row,"result",result)
        if name=="ArrayData":
            # Do not consume an iterator or coerce an arbitrary native result.
            # Frozen member() returns it unchanged to the production observer.
            row.update(result_value_finite=False,result_value=None)
            try:
                numeric=type(result) in (list,tuple) and all(type(x) in (int,float) for x in result)
                finite=numeric and all(math.isfinite(x) for x in result)
                row["result_value_finite"]=finite
                row["result_value"]=list(result) if finite else None
            except Exception as exc:
                row["result_value_error"]=f"{type(exc).__name__}: {exc}"
        return result
    except Exception as exc:
        row["error"]=f"{type(exc).__name__}: {exc}"
        raise


def _getchildren_record_error(row, key, exc):
    try:
        message=str(exc)
    except Exception:
        message="NOT_RECORDED"
    row[key]=f"{type(exc).__name__}: {message}"


def _getchildren_wrapper_metadata(feature):
    """Inspect stored Python metadata only; never resolve a business member."""
    absent=object()
    ole=_local_attribute(feature,"_oleobj_",absent)
    representation=_local_attribute(feature,"_olerepr_",absent)
    lazy=_local_attribute(feature,"_lazydata_",absent)
    row={"python_type":f"{type(feature).__module__}.{type(feature).__qualname__}",
         "module":type(feature).__module__,"has_oleobj":ole is not absent,
         "oleobj_python_type":None if ole is absent else f"{type(ole).__module__}.{type(ole).__qualname__}",
         "has_olerepr":representation is not absent,"has_lazydata":lazy is not absent,
         "repr":object.__repr__(feature)[:256],
         "FLAG_AS_METHOD_AVAILABLE":"YES" if _local_attribute(feature,"_FlagAsMethod",absent) is not absent else "NO"}
    row["olerepr_python_type"]=None if representation is absent else f"{type(representation).__module__}.{type(representation).__qualname__}"
    default=_local_attribute(representation,"defaultDispatchName")
    row["defaultDispatchName"]=default if type(default) is str else None
    for name in ("mapFuncs","propMap","propMapGet","propMapPut"):
        mapping=_local_attribute(representation,name)
        # Custom mapping protocols could run code or trigger lazy generation.
        row[name+"_keys"]=sorted(key for key in mapping if type(key) is str) if type(mapping) is dict else None
        row["GETCHILDREN_"+name.upper()+"_PRESENT"]=("YES" if "GetChildren" in mapping else "NO") if type(mapping) is dict else "UNKNOWN"
    row["lazydata_python_type"]=None if lazy is absent else f"{type(lazy).__module__}.{type(lazy).__qualname__}"
    row["lazydata_contains_getchildren"]=("YES" if "GetChildren" in lazy else "NO") if type(lazy) is dict else "UNKNOWN"
    return row


def _getchildren_interface_probe(feature):
    """One best-effort IFeature QI, after production access; never use its result."""
    row={"QI_ATTEMPTED":"NO","QI_STATUS":"NOT_AVAILABLE","QI_INVOCATION_COUNT":0,
         "interface_iid":_IFEATURE_IID,"normalized_wrapper_status":"NOT_AVAILABLE"}
    try:
        ole=_local_attribute(feature,"_oleobj_")
        query=_local_attribute(ole,"QueryInterface")
        if not callable(query):
            return row
        import pythoncom
        import pywintypes
        from win32com.client import dynamic
        row.update(QI_ATTEMPTED="YES",QI_INVOCATION_COUNT=1)
        # useIID chooses the Python wrapper for the same dual IFeature pointer.
        # It does not request a second native interface or replace production.
        interface=query(pywintypes.IID(_IFEATURE_IID),pythoncom.IID_IDispatch)
        row["returned_python_type"]=f"{type(interface).__module__}.{type(interface).__qualname__}"
        row["QI_STATUS"]="PASS" if interface is not None else "FAIL"
        row["returned_wrapper_kind"]=("PyIDispatch" if isinstance(interface,pythoncom.TypeIIDs[pythoncom.IID_IDispatch])
                                      else "PyIUnknown" if isinstance(interface,pythoncom.TypeIIDs[pythoncom.IID_IUnknown])
                                      else "OTHER")
        if isinstance(interface,pythoncom.TypeIIDs[pythoncom.IID_IDispatch]):
            normalized=dynamic.Dispatch(interface)  # Metadata only; no makepy.
            row["normalized_metadata"]=_getchildren_wrapper_metadata(normalized)
            row["normalized_wrapper_status"]="PASS"
    except Exception as exc:
        row["QI_STATUS"]="FAIL" if "returned_python_type" not in row else row["QI_STATUS"]
        _getchildren_record_error(row,"error",exc)
    return row


def _classify_getchildren_diagnostics(row):
    """Classify observed metadata; never claim that a conversion fixes execution."""
    probe=row.get("interface_probe",{})
    original=row.get("operation_feature_metadata",{})
    normalized=probe.get("normalized_metadata",{})
    mapped=normalized.get("GETCHILDREN_MAPFUNCS_PRESENT","UNKNOWN")
    missing=row.get("resolved_is_none") is True
    classification="INSUFFICIENT"
    if probe.get("QI_STATUS")=="FAIL":
        classification="QI_FAILED"
    elif probe.get("normalized_wrapper_status")=="PASS":
        if missing and mapped in ("YES","NO"):
            classification=("ORIGINAL_MISSING_NORMALIZED_METHOD_MAPPED" if mapped=="YES"
                            else "ORIGINAL_MISSING_NORMALIZED_STILL_UNMAPPED")
        else:
            keys=("GETCHILDREN_MAPFUNCS_PRESENT","GETCHILDREN_PROPMAP_PRESENT",
                  "GETCHILDREN_PROPMAPGET_PRESENT","GETCHILDREN_PROPMAPPUT_PRESENT")
            shape=("python_type","module","has_oleobj","oleobj_python_type","has_olerepr",
                   "has_lazydata","defaultDispatchName","FLAG_AS_METHOD_AVAILABLE",
                   "mapFuncs_keys","propMap_keys","propMapGet_keys","propMapPut_keys")
            if (all(original.get(k) in ("YES","NO") and original.get(k)==normalized.get(k) for k in keys)
                    and all(original.get(k)==normalized.get(k) for k in shape)):
                classification="ORIGINAL_AND_NORMALIZED_EQUIVALENT"
    row["IFEATURE_NORMALIZATION_DIAGNOSTIC_CLASSIFICATION"]=classification
    resolution="CASE_F_OTHER"
    if row.get("branch_taken")=="CALL_CALLABLE":
        resolution="CASE_C_ORIGINAL_GETATTR_CALLABLE"
    elif row.get("branch_taken")=="RETURN_RESOLVED_OBJECT" and row.get("resolved_has_oleobj") is True:
        resolution="CASE_D_ORIGINAL_GETATTR_RESOLVED_COM_OBJECT"
    elif probe.get("QI_STATUS")=="FAIL":
        resolution="CASE_E_QI_FAILED"
    elif missing and mapped in ("YES","NO"):
        resolution=("CASE_A_ORIGINAL_GETATTR_NONE_QI_METHOD_MAPPED" if mapped=="YES"
                    else "CASE_B_ORIGINAL_GETATTR_NONE_QI_STILL_UNMAPPED")
    row["GETCHILDREN_RESOLUTION_CLASSIFICATION"]=resolution


def _getchildren_value_metadata(row, label, value):
    """Describe the existing value without invoking its repr or attribute protocol."""
    row[label+"_python_type"]=f"{type(value).__module__}.{type(value).__qualname__}"
    row[label+"_module"]=type(value).__module__
    row[label+"_is_none"]=value is None
    row[label+"_callable"]=callable(value)
    absent=object()
    row[label+"_has_oleobj"]=inspect.getattr_static(value,"_oleobj_",absent) is not absent
    row[label+"_repr"]=object.__repr__(value)[:256]


def _observe_getchildren(feature, row):
    """Retain the A12-DX observation contract for historical compatibility tests."""
    row.update(observation_policy="OBSERVE_EXISTING_PATH_ONLY",
               GETCHILDREN_STATIC_CONTRACT="CONFIRMED",static_interface_iid=_IFEATURE_IID,
               static_member_kind="METHOD",static_argument_count=0,static_dispid=44,
               static_return="VARIANT/Object children array",FLAG_AS_METHOD_CALLED="NO",
               NORMALIZED_FEATURE_USED_FOR_PRODUCTION="NO",POST_DX_GETATTR_COUNT=0,
               argument_count=0,invocation_attempted="NO",invocation_count=0)
    try:
        metadata=_getchildren_wrapper_metadata(feature)
        row["operation_feature_metadata"]=metadata
        row.update({"operation_feature_"+key:metadata[key] for key in (
            "python_type","module","has_oleobj","oleobj_python_type","has_olerepr","has_lazydata","repr")})
        for key,value in metadata.items():
            if key.startswith("GETCHILDREN_") or key=="FLAG_AS_METHOD_AVAILABLE":
                row[key]=value
    except Exception as exc:
        _getchildren_record_error(row,"metadata_error",exc)
    try:
        if feature is None:
            raise NativeEvidenceUnavailable("GetChildren: native object missing")
        row["POST_DX_GETATTR_COUNT"]=1
        resolved=getattr(feature,"GetChildren",None)
        try:
            _getchildren_value_metadata(row,"resolved",resolved)
        except Exception as exc:
            _getchildren_record_error(row,"resolved_metadata_error",exc)
        if resolved is None:
            row["branch_taken"]="MISSING_NONE"
            raise NativeEvidenceUnavailable("GetChildren: native member missing")
        # Keep the frozen short circuit, even though the static contract is METHOD.
        call=callable(resolved) and not hasattr(resolved,"_oleobj_")
        if callable(resolved):
            row["resolved_has_oleobj"]=not call
        row["branch_taken"]="CALL_CALLABLE" if call else "RETURN_RESOLVED_OBJECT"
        row["argument_count"]=0
        result=resolved() if call else resolved
        try:
            _getchildren_value_metadata(row,"result",result)
        except Exception as exc:
            _getchildren_record_error(row,"result_metadata_error",exc)
        return result  # The exact production result, never the QI object.
    except Exception as exc:
        row["exception_type"]=f"{type(exc).__module__}.{type(exc).__qualname__}"
        _getchildren_record_error(row,"exception_message",exc)
        raise
    finally:
        # The existing boundary sets this branch immediately before its call.
        row["invocation_attempted"]="YES" if row.get("branch_taken")=="CALL_CALLABLE" else "NO"
        row["invocation_count"]=1 if row["invocation_attempted"]=="YES" else 0
        if row.get("resolved_is_none") is True:
            row["branch_taken"]="MISSING_NONE"
        elif "branch_taken" not in row:
            row["branch_taken"]="OTHER"
        # Diagnostics must not mask the original return/exception. The probe
        # runs afterwards and cannot influence which production branch executed.
        try:
            row["interface_probe"]=_getchildren_interface_probe(feature)
            _classify_getchildren_diagnostics(row)
        except Exception as exc:
            _getchildren_record_error(row,"diagnostic_error",exc)


def _invoke_getchildren(feature, row):
    """Enumerate current direct children; a successful API empty result is valid."""
    row.update(observation_policy="R004_NAMED_METHOD_BOUNDARY",
               GETCHILDREN_STATIC_CONTRACT="CONFIRMED",static_interface_iid=_IFEATURE_IID,
               static_member_kind="METHOD",static_argument_count=0,static_dispid=44,
               static_return="VARIANT/Object children array",
               GETCHILDREN_EVIDENCE_REQUIREMENT="REQUIRED_ENGINEERING_EVIDENCE",
               NORMALIZED_FEATURE_USED_FOR_PRODUCTION="NO",IFEATURE_QI_ROLE="DIAGNOSTIC_ONLY",
               GETCHILDREN_EMPTY_RESULT_FALLBACK="NO",GETCHILDREN_SUCCESSFUL_EMPTY_NORMALIZATION="NO",
               GETCHILDREN_RESULT_NORMALIZATION="NOT_APPLICABLE",
               GETCHILDREN_EMPTY_RESULT_NORMALIZATION="NOT_APPLICABLE",
               GETCHILDREN_ENUMERATION_COMPLETED="NO",
               CHILD_EVIDENCE_TEMPORAL_SEMANTICS="CREATION_TIME_SNAPSHOT",
               CHILD_EVIDENCE_ENGINEERING_PURPOSE="CURRENT_STATE_ENUMERATION")
    try:
        metadata=_getchildren_wrapper_metadata(feature)
        row["operation_feature_metadata"]=metadata
        row.update({"operation_feature_"+key:metadata[key] for key in (
            "python_type","module","has_oleobj","oleobj_python_type","has_olerepr","has_lazydata","repr")})
        row.update({key:value for key,value in metadata.items()
                    if key.startswith("GETCHILDREN_") or key=="FLAG_AS_METHOD_AVAILABLE"})
    except Exception as exc:
        _getchildren_record_error(row,"metadata_error",exc)
    invocation=row["named_invocation"]={}
    classification=None
    try:
        # Diagnostics share the helper's resolution; no pre-bind business getattr.
        result=_invoke_com_method(feature,"GetChildren",diagnostic=invocation,require_result=False)
        classification="BINDING_FAILED"
        require(invocation.get("NAMED_METHOD_TARGET_CONFIRMED")=="YES"
                and invocation.get("resolved_callable") is True
                and invocation.get("POST_BIND_GETATTR_COUNT")==1,
                "GetChildren: successful named binding unproven")
        classification="INVOCATION_FAILED"
        require(invocation.get("METHOD_INVOCATION_COUNT")==invocation.get("invocation_count")==1
                and invocation.get("branch_taken")=="EXPLICIT_METHOD_CALL" and "error" not in invocation,
                "GetChildren: successful single invocation unproven")
        classification="MALFORMED_RESULT"
        # Official Empty can become None in pywin32. Normalize only after the
        # named call returned successfully, never resolution or invocation errors.
        if result is None:
            result=()
            row.update(GETCHILDREN_RESULT_NORMALIZATION="SUCCESSFUL_NONE_TO_EMPTY_CHILD_SET",
                       GETCHILDREN_EMPTY_RESULT_NORMALIZATION="SUCCESSFUL_NONE_TO_EMPTY_CHILD_SET",
                       GETCHILDREN_SUCCESSFUL_EMPTY_NORMALIZATION="YES")
            classification="SUCCESSFUL_EMPTY_RESULT_NORMALIZED"
        else:
            require(isinstance(result,(tuple,list)),"GetChildren: malformed child collection")
            row["GETCHILDREN_RESULT_NORMALIZATION"]="NOT_REQUIRED"
            classification="SUCCESSFUL_NONEMPTY_COLLECTION" if len(result) else "SUCCESSFUL_EMPTY_COLLECTION"
        row.update(GETCHILDREN_RESULT_CLASSIFICATION=classification,
                   GETCHILDREN_NORMALIZED_RESULT_TYPE=f"{type(result).__module__}.{type(result).__qualname__}",
                   GETCHILDREN_NORMALIZED_CHILD_COUNT=len(result),DIRECT_CHILD_COUNT_AT_SNAPSHOT=len(result),
                   GETCHILDREN_ENUMERATION_COMPLETED="YES")
        row["GETCHILDREN_BINDING_CLASSIFICATION"]=(
            "NAMED_BINDING_REQUIRED_AND_APPLIED" if invocation["METHOD_FLAGGED"]=="YES"
            else "EXISTING_METHOD_MAPPING_USED")
        return result
    except Exception as exc:
        classification=classification if classification is not None else (
            "INVOCATION_FAILED" if invocation.get("METHOD_INVOCATION_COUNT")==1 else "BINDING_FAILED")
        row["GETCHILDREN_RESULT_CLASSIFICATION"]=classification
        row["GETCHILDREN_BINDING_CLASSIFICATION"]=(
            "RESULT_INVALID" if classification=="MALFORMED_RESULT" else classification)
        row["exception_type"]=f"{type(exc).__module__}.{type(exc).__qualname__}"
        _getchildren_record_error(row,"exception_message",exc)
        raise
    finally:
        row["GETCHILDREN_RAW_RESULT_TYPE"]=invocation.get("result_python_type","NOT_RECORDED")
        row["GETCHILDREN_RAW_RESULT_IS_NONE"]=(
            "YES" if invocation["result_is_none"] else "NO") if "result_is_none" in invocation else "NOT_RECORDED"
        row.update({key:value for key,value in invocation.items()
                    if key.startswith(("resolved_","result_")) or key in (
                        "branch_taken","argument_count","invocation_count")})
        for target,source in (
            ("METHOD_NAME","METHOD_NAME"),("METHOD_BINDING_STRATEGY","METHOD_BINDING_STRATEGY"),
            ("METHOD_ALREADY_MAPPED","METHOD_ALREADY_MAPPED"),("METHOD_FLAGGED","METHOD_FLAGGED"),
            ("PRE_FLAG_GETATTR_PERFORMED","PRE_FLAG_GETATTR_PERFORMED"),
            ("POST_BIND_GETATTR_COUNT","POST_BIND_GETATTR_COUNT"),
            ("NAMED_TARGET_CONFIRMED","NAMED_METHOD_TARGET_CONFIRMED"),
            ("INVOCATION_COUNT","METHOD_INVOCATION_COUNT"),("ARGUMENTS_APPLIED","ARGUMENTS_APPLIED"),
            ("RESULT_TYPE","result_python_type"),("RESULT_IS_NONE","result_is_none")):
            row["GETCHILDREN_"+target]=invocation.get(source)
        row["POST_DX_GETATTR_COUNT"]=invocation.get("POST_BIND_GETATTR_COUNT",0)
        row["invocation_attempted"]="YES" if invocation.get("METHOD_INVOCATION_COUNT")==1 else "NO"
        row["FLAG_AS_METHOD_CALLED"]=invocation.get("METHOD_FLAGGED","NO")
        try:
            row["post_bind_operation_feature_metadata"]=_getchildren_wrapper_metadata(feature)
            row["interface_probe"]=_getchildren_interface_probe(feature)
            _classify_getchildren_diagnostics(row)
        except Exception as exc:
            _getchildren_record_error(row,"diagnostic_error",exc)


def _invoke_com_method(obj, name, *args, diagnostic=None, require_result=False):
    """Bind an audited named method before resolution; never call a dispatch result."""
    row=diagnostic if diagnostic is not None else {}
    row.update(member_name=name,argument_count=len(args),
               argument_python_types=[f"{type(a).__module__}.{type(a).__qualname__}" for a in args],
               argument_vartype=[_local_attribute(a,"varianttype") for a in args],
               arguments_applied="UNKNOWN",branch_taken="RESOLUTION_UNKNOWN",
               METHOD_NAME=name,METHOD_BINDING_STRATEGY="BINDING_UNKNOWN",
               METHOD_ALREADY_MAPPED="UNKNOWN",METHOD_FLAGGED="NO",METHOD_DISPID=None,
               NAMED_METHOD_TARGET_CONFIRMED="NO",PRE_FLAG_GETATTR_PERFORMED="NO",
               POST_BIND_GETATTR_COUNT=0,METHOD_INVOCATION_COUNT=0,invocation_count=0,
               ARGUMENTS_APPLIED="UNKNOWN")
    _resolution_object_diagnostic(row,"owner",obj)
    row["dispatch_metadata_before"]=_dispatch_metadata(obj,name)
    row["METHOD_MAP_BEFORE"]=dict(row["dispatch_metadata_before"].get("mapFuncs",{}))
    try:
        require(obj is not None,f"{name}: native object missing")
        mapping=_local_attribute(_local_attribute(obj,"_olerepr_"),"mapFuncs")
        flag=_local_attribute(obj,"_FlagAsMethod")
        mapped=isinstance(mapping,dict) and name in mapping
        row["METHOD_ALREADY_MAPPED"]="YES" if mapped else "NO" if isinstance(mapping,dict) else "UNKNOWN"
        if mapped:
            row["METHOD_BINDING_STRATEGY"]="EXISTING_METHOD_MAP"
        elif flag is not None:
            row["METHOD_BINDING_STRATEGY"]="FLAG_AS_METHOD_THEN_RESOLVE"
            require(callable(flag) and isinstance(mapping,dict),f"{name}: native method binding unavailable")
            flag(name)  # Known METHOD contract; no property resolution before binding.
            row["METHOD_FLAGGED"]="YES"
            require(name in mapping,f"{name}: native named method map entry missing")
        else:
            row["METHOD_BINDING_STRATEGY"]="PYTHON_OR_GENERATED_CALLABLE"
        row["METHOD_MAP_AFTER"]=dict(_dispatch_metadata(obj,name).get("mapFuncs",{}))
        row["METHOD_DISPID"]=row["METHOD_MAP_AFTER"].get("dispid")
        row["POST_BIND_GETATTR_COUNT"]=1
        resolved=getattr(obj,name,None)  # Resolve once; diagnostics and invocation share it.
        _resolution_object_diagnostic(row,"resolved",resolved)
        row["dispatch_metadata"]=_dispatch_metadata(obj,name)
        require(resolved is not None,f"{name}: native member missing")
        row["resolved_callable"]=callable(resolved)
        require(row["resolved_callable"],f"{name}: native method not callable")
        if row["resolved_has_oleobj"]:
            from win32com.client.dynamic import CDispatch
            require(not isinstance(resolved,CDispatch),f"{name}: resolved dispatch is not a named method")
        row.update(NAMED_METHOD_TARGET_CONFIRMED="YES",branch_taken="EXPLICIT_METHOD_CALL",
                   arguments_applied="YES",ARGUMENTS_APPLIED="YES",METHOD_INVOCATION_COUNT=1,invocation_count=1)
        result=resolved(*args)
        _resolution_object_diagnostic(row,"result",result)
        require(not require_result or result is not None,f"{name}: native method returned None")
        return result
    except Exception as exc:
        row["METHOD_MAP_AFTER"]=dict(_dispatch_metadata(obj,name).get("mapFuncs",{}))
        row["METHOD_DISPID"]=row["METHOD_MAP_AFTER"].get("dispid")
        row["error"]=f"{type(exc).__name__}: {exc}"
        raise


def _createpoint_classification(boundary):
    row=boundary["createpoint_resolution"]
    applied=row["arguments_applied"]
    boundary["CREATEPOINT_ARGUMENTS_APPLIED"]=applied if applied in ("YES","NO") else "UNKNOWN"
    classification="OTHER"
    if row["branch_taken"]=="RESOLUTION_UNKNOWN":
        classification="RESOLUTION_UNKNOWN"
    elif row["branch_taken"] in ("CALL_CALLABLE","EXPLICIT_METHOD_CALL") and row["argument_count"]:
        classification="METHOD_CALL_WITH_ARGUMENTS"
    elif row["resolved_has_oleobj"] and applied=="NO":
        classification="RESOLVED_COM_OBJECT_WITH_ARGUMENTS_NOT_APPLIED"
    boundary["CREATEPOINT_RESOLUTION_CLASSIFICATION"]=classification
    boundary["CREATEPOINT_TYPEINFO_STATUS"]=row.get("dispatch_metadata",{}).get("typeinfo_status","UNKNOWN")
    boundary["GENERATED_WRAPPER_USED"]=row.get("dispatch_metadata",{}).get("generated_wrapper_used","UNKNOWN")


def _mathpoint_scalar_probe(source, name, args, requested_length):
    valid_key="CONVERT_TO_VECTOR_PROBE_VALID" if name=="ConvertToVector" else "SUBTRACT_PROBE_VALID"
    row={"evidence_class":"BEST_EFFORT_DIAGNOSTIC","requested_vector_length":requested_length,
         valid_key:"NO","GET_LENGTH_PROBE_VALID":"NO",
         "observed_length":None,"absolute_difference":None,"scalar_finite":False,
         "raw_scalar_value":None,"raw_scalar_repr":None,"scalar_python_type":"UNKNOWN"}
    observed_key="observed_convert_to_vector_length" if name=="ConvertToVector" else "observed_subtract_length"
    row[observed_key]=None
    if name=="Subtract":row["requested_distance"]=requested_length
    operation=row["operation_resolution"]={}
    length=row["get_length_resolution"]={}
    try:
        vector=_diagnostic_member(source,name,operation,*args)
        _resolution_object_diagnostic(row,"returned_vector",vector)
        if operation["branch_taken"]=="CALL_CALLABLE" and vector is not None:
            row[valid_key]="YES"
        scalar=_diagnostic_member(vector,"GetLength",length)
        row["scalar_python_type"]=f"{type(scalar).__module__}.{type(scalar).__qualname__}"
        row["raw_scalar_repr"]=repr(scalar)[:256]
        # No object coercion/default COM invocation is allowed to manufacture a scalar.
        if type(scalar) in (int,float):
            row["scalar_finite"]=math.isfinite(scalar)
            row["raw_scalar_value"]=scalar if row["scalar_finite"] else None
            if row["scalar_finite"] and scalar>=0:
                row["observed_length"]=scalar
                row[observed_key]=scalar
                row["absolute_difference"]=abs(scalar-requested_length)
                if length["branch_taken"]=="CALL_CALLABLE":
                    row["GET_LENGTH_PROBE_VALID"]="YES"
    except Exception as exc:
        row["error"]=f"{type(exc).__name__}: {exc}"
    return row


def _mathpoint_internal_state(boundary):
    boundary.update(MATHPOINT_INTERNAL_STATE="UNKNOWN",ARRAYDATA_OBSERVATION_PATH="UNKNOWN",
                    CREATEPOINT_OR_INVOCATION_PATH="UNKNOWN")
    a,b=boundary["convert_to_vector_probe"],boundary["subtract_probe"]
    if (boundary["source_point_arraydata_raw"]!=[0,0,0] or
        a.get("CONVERT_TO_VECTOR_PROBE_VALID")!="YES" or b.get("SUBTRACT_PROBE_VALID")!="YES" or
        a.get("GET_LENGTH_PROBE_VALID")!="YES" or b.get("GET_LENGTH_PROBE_VALID")!="YES"):
        return
    near=lambda x,y:abs(x-y)<=AXIS_COMPONENT_TOLERANCE
    if (a["requested_vector_length"]>AXIS_COMPONENT_TOLERANCE and
        b["requested_vector_length"]>AXIS_COMPONENT_TOLERANCE and
        near(a["observed_length"],a["requested_vector_length"]) and
        near(b["observed_length"],b["requested_vector_length"])):
        boundary.update(MATHPOINT_INTERNAL_STATE="NONZERO",ARRAYDATA_OBSERVATION_PATH="SUSPECT")
    elif near(a["observed_length"],0) and near(b["observed_length"],0):
        boundary.update(MATHPOINT_INTERNAL_STATE="CONSISTENT_WITH_ZERO",CREATEPOINT_OR_INVOCATION_PATH="SUSPECT")


def next_native(entity,name):
    value=getattr(entity,name,None)
    return value() if callable(value) and not hasattr(value,"_oleobj_") else value


MAX_REFERENCE_FEATURE_SCAN_NODES=256  # Safety budget, never an expected feature count.


def runtime_pointer_diagnostic(feature):
    """Best-effort raw COM address; its short lifetime cannot prove node identity."""
    if not hasattr(feature,"_oleobj_"):
        return None,"COM wrapper unavailable"
    try:
        import pythoncom
        raw=repr(feature._oleobj_.QueryInterface(pythoncom.IID_IUnknown))
        match=re.search(r"with obj at (0x[0-9A-Fa-f]+)",raw)
        if match is None:
            return None,"canonical IUnknown pointer unavailable"
        return match.group(1).casefold(),None
    except Exception as exc:
        return None,f"{type(exc).__name__}: {exc}"


def to_native(xyz):
    return (xyz[0], xyz[2], -xyz[1])


def from_native(xyz):
    return (xyz[0], -xyz[2], xyz[1])


def source_bounds(native_box):
    require(_vector(native_box,6), "native bounds incomplete")
    corners=[from_native(p) for p in itertools.product(*[(native_box[i],native_box[i+3]) for i in range(3)])]
    return [value for i in range(3) for value in (min(p[i] for p in corners),max(p[i] for p in corners))]


class R004ExecutionAdapter(R003SolidWorksSessionAdapter):
    """Reuse lifecycle only; fixed three-operation execution/validation is excluded."""

    def __init__(self, *, simulated=False, **kwargs):
        super().__init__(**kwargs)
        self.simulated=simulated
        self.live_authorized=False
        self.ledger=[]
        self.commands=[]
        self.execution_gates={}
        self.drawing_gates={}
        self._owned_sketch_cache_keys=set()
        self._preparation_sketch=None
        self._native_sketch_handoff=None
        self._part_doc_token=None
        self.diagnostics={"evidence_class":"BEST_EFFORT_DIAGNOSTIC_EVIDENCE",
                          "native_feature_chains":[],"sketch_basis":[],"document":None,"session":None,
                          "backend_boundary":{"document_backend_reached":False,"modeling_primitive_reached":False},
                          "geometry":{"part_document_created":False,"sketch_created":False,
                                      "feature_created":False,"body_created":None,"modeling_primitive_called":False}}

    def connect(self):
        if not self.simulated and not self.live_authorized:
            raise SessionOwnershipUnproven("live boundary requires qualification-runner authorization")
        if not self.simulated and self._identity.source != "OS_SNAPSHOT_AND_PID_ROT":
            raise SessionOwnershipUnproven("production requires OS identity and PID-qualified ROT")
        try:
            super().connect()
        except Exception as exc:
            self.diagnostics["session"]={"pid":self._identity.pid,
                "process_creation_time":self._identity.create_time,"com_bound_pid":None,
                "actual_sw_version":"UNKNOWN","revision_number_raw":None,
                "parsed_sw_year":None,"version_query_error":f"{type(exc).__name__}: {exc}"}
            raise
        if self.diagnostics["session"] is None:
            row={"pid":self._identity.pid,"process_creation_time":self._identity.create_time,
                 "com_bound_pid":(self.connection_info or {}).get("process_id"),
                 "actual_sw_version":"UNKNOWN","revision_number_raw":None,
                 "parsed_sw_year":self.sw_major_version}
            try:
                row["revision_number_raw"]=str(member(self._session.sw,"RevisionNumber"))
                row["actual_sw_version"]=row["revision_number_raw"]
            except Exception as exc:
                row["version_query_error"]=f"{type(exc).__name__}: {exc}"
            self.diagnostics["session"]=row

    def _api(self):
        self._verify_boundary()
        return super()._api()

    def _verify_boundary(self):
        require(self.simulated or self.live_authorized,"live call boundary not authorized")
        require(verify_owned_identity(self._identity,self._probe(self._identity.pid),self._run_id),
                "owned process identity changed")

    def new_part(self,template_path=None):
        self._verify_boundary()
        part=super().new_part(template_path=template_path)
        self.diagnostics["backend_boundary"]["document_backend_reached"]=True
        self.diagnostics["geometry"]["part_document_created"]=True
        self.diagnostics["document"]=self._document_diagnostic()
        self._part_doc_token=lifecycle_event(self,"REGISTER",kind="PART",document=part,
                                             title=self.diagnostics["document"]["title"])
        lifecycle_event(self,"ALIAS_ADD",self._part_doc_token,title=self.diagnostics["document"]["title"])
        return part

    def _document_diagnostic(self):
        row={"document_created":True,"document_type":None,"title":None,"path":None,
             "saved":False,"body_count":None,"feature_count":None,"query_errors":{}}
        for key,name,args in (("document_type","GetType",()),("title","GetTitle",()),
                              ("path","GetPathName",()),("body_count","GetBodies2",(0,False))):
            try:
                value=(_invoke_com_method(self._part,"GetBodies2",*args) if args
                       else member(self._part,name))
                row[key]=len(value or []) if key=="body_count" else value
            except Exception as exc:
                row["query_errors"][key]=f"{type(exc).__name__}: {exc}"
        row["saved"]=bool(row["path"])
        if isinstance(row["body_count"],int):
            self.diagnostics["geometry"]["body_created"]=row["body_count"]>0
        return row

    @staticmethod
    def _optional_native(feature,name):
        try:
            return str(member(feature,name)),None
        except Exception as exc:
            return None,f"{type(exc).__name__}: {exc}"

    def _trace_global_reference(self,ext,feature,request):
        trace={"datum_token":request.token,"traversal_progress_guard":"MAX_REFERENCE_FEATURE_SCAN_NODES",
               "engineering_persistent_identity":"IModelDocExtension.GetPersistReference3",
               "max_scan_nodes":MAX_REFERENCE_FEATURE_SCAN_NODES,"nodes":[],"termination_reason":None,
               "visited_count":0,"visited_node_count":0,
               "unique_persistent_reference_count":0,"candidate_count":0,
               "persistent_ref_duplicate_count":0,"persistent_ref_duplicate_events":[],
               "runtime_pointer_authoritative_for_traversal_identity":"NO",
               "runtime_pointer_authoritative_for_engineering_identity":"NO",
               "runtime_pointer_duplicate_count":0,"runtime_pointer_duplicate_events":[],
               "runtime_cycle_proven":"NO",
               "scan_budget_exhausted":"NO"}
        self.diagnostics["native_feature_chains"].append(trace)
        seen_persistent={}
        seen_runtime_pointers={}
        candidate_refs={}
        candidates=[]
        while feature is not None:
            index=len(trace["nodes"])
            if index>=MAX_REFERENCE_FEATURE_SCAN_NODES:
                trace["termination_reason"]="SCAN_LIMIT_EXCEEDED"
                trace["scan_budget_exhausted"]="YES"
                require(False,"REFERENCE_FEATURE_SCAN_LIMIT_EXCEEDED")
            row={"sequence_index":index,"feature_name":None,"feature_type_name":None,
                 "python_wrapper_type":f"{type(feature).__module__}.{type(feature).__qualname__}",
                 "python_wrapper_identity":id(feature),"com_wrapper_type":type(feature).__name__,
                 "persistent_reference_available":False,"persistent_reference_base64":None,
                 "persistent_reference_sha256":None,"native_identity_source":"IModelDocExtension.GetPersistReference3",
                 "previous_node_index":index-1 if index else None,
                 "transition":"GET_NEXT_FEATURE" if index else "FIRST_FEATURE",
                 "next_feature_returned_none":None,"node_category":None,
                 "persistent_ref_duplicate":False,"persistent_ref_duplicate_of_index":None,
                 "runtime_pointer_observed":None,"runtime_pointer_available":False,
                 "runtime_pointer_duplicate":False,"runtime_pointer_first_seen_index":None,
                 "runtime_pointer_authoritative":"NO",
                 "node_decision":"CONTINUE","exception_at_node":None}
            trace["nodes"].append(row)
            trace["visited_count"]=index+1
            trace["visited_node_count"]=index+1
            try:
                kind=str(member(feature,"GetTypeName2"))
                row["feature_type_name"]=kind
            except Exception as exc:
                row["exception_at_node"]=f"{type(exc).__name__}: {exc}"
                trace["termination_reason"]="FEATURE_TYPE_ERROR"
                raise
            row["node_category"]=("REFERENCE_CANDIDATE" if kind in {"RefPlane","ReferencePlane"}
                                  else "TRAVERSAL_ONLY")
            row["feature_name"],row["feature_name_error"]=self._optional_native(feature,"Name")
            runtime_pointer,runtime_error=runtime_pointer_diagnostic(feature)
            row["runtime_pointer_available"]=runtime_pointer is not None
            row["runtime_pointer_observed"]=runtime_pointer
            row["runtime_pointer_error"]=runtime_error
            if runtime_pointer is not None:
                if runtime_pointer in seen_runtime_pointers:
                    row["runtime_pointer_duplicate"]=True
                    row["runtime_pointer_first_seen_index"]=seen_runtime_pointers[runtime_pointer]
                    trace["runtime_pointer_duplicate_count"]+=1
                    trace["runtime_pointer_duplicate_events"].append({
                        "first_seen_index":seen_runtime_pointers[runtime_pointer],
                        "duplicate_index":index,"raw_pointer":runtime_pointer})
                else:
                    seen_runtime_pointers[runtime_pointer]=index
            try:
                token=capture_persistent_reference(ext,feature,"FEATURE")["encoded"]
            except Exception as exc:
                row["persistent_reference_error"]=f"{type(exc).__name__}: {exc}"
                if row["node_category"]=="REFERENCE_CANDIDATE":
                    row["exception_at_node"]=row["persistent_reference_error"]
                    trace["termination_reason"]="CANDIDATE_PERSISTENT_REFERENCE_ERROR"
                    raise
                token=None
            if token is not None:
                row["persistent_reference_available"]=True
                row["persistent_reference_base64"]=token
                row["persistent_reference_sha256"]=sha256(token.encode("ascii")).hexdigest()
                if token in seen_persistent:
                    row["persistent_ref_duplicate"]=True
                    row["persistent_ref_duplicate_of_index"]=seen_persistent[token]
                    trace["persistent_ref_duplicate_count"]+=1
                    trace["persistent_ref_duplicate_events"].append({"first_seen_index":seen_persistent[token],
                        "duplicate_index":index,"persistent_reference_base64":token,
                        "first_node_metadata":dict(trace["nodes"][seen_persistent[token]]),
                        "duplicate_node_metadata":dict(row)})
                else:
                    seen_persistent[token]=index
                trace["unique_persistent_reference_count"]=len(seen_persistent)
            try:
                if row["node_category"]=="REFERENCE_CANDIDATE":
                    measured=collect_reference_plane(feature,ext,name="measured-native-reference")
                    origin=from_native([v/1000 for v in measured["origin_mm"]])
                    normal=from_native(measured["normal"])
                    if abs(origin[2]-request.plane_z_m)<=LINEAR_TOLERANCE_MM/1000 and abs(abs(normal[2])-1)<=AXIS_COMPONENT_TOLERANCE:
                        candidate_ref=measured["native_reference"]
                        if candidate_ref in candidate_refs:
                            row["node_decision"]="FAIL"
                            row["exception_at_node"]="NativeEvidenceUnavailable: REFERENCE_CANDIDATE_IDENTITY_COLLISION"
                            trace["termination_reason"]="REFERENCE_CANDIDATE_IDENTITY_COLLISION"
                            raise NativeEvidenceUnavailable("REFERENCE_CANDIDATE_IDENTITY_COLLISION")
                        candidate_refs[candidate_ref]=index
                        candidates.append((feature,{"reference_type":"PLANE","encoded":measured["native_reference"]},measured))
                        trace["candidate_count"]=len(candidates)
            except Exception as exc:
                row["exception_at_node"]=f"{type(exc).__name__}: {exc}"
                trace["termination_reason"]=trace["termination_reason"] or "REFERENCE_PLANE_EVIDENCE_ERROR"
                raise
            try:
                feature=next_native(feature,"GetNextFeature")
            except Exception as exc:
                row["exception_at_node"]=f"{type(exc).__name__}: {exc}"
                trace["termination_reason"]="GET_NEXT_FEATURE_ERROR"
                raise
            row["next_feature_returned_none"]=feature is None
        trace["termination_reason"]="GET_NEXT_FEATURE_NONE"
        if self.diagnostics["document"] is not None:
            self.diagnostics["document"]["feature_count"]=trace["visited_count"]
        return candidates

    def save_part(self,path):
        self._verify_boundary()
        lifecycle_event(self,"TITLE_BEFORE_SAVE",self._part_doc_token,document=self._part)
        old_title=self.diagnostics.get("document_lifecycle",{}).get("documents",{}).get(self._part_doc_token,{}).get("title")
        saved=super().save_part(path)
        lifecycle_event(self,"SAVE_RENAME",self._part_doc_token,document=self._part,save_return=saved)
        if saved is True:
            lifecycle_event(self,"ALIAS_REMOVE",self._part_doc_token,title=old_title,
                            registry_operation="remove",observation_stage="AFTER_FROZEN_SAVE_RETURN")
            title=self.diagnostics.get("document_lifecycle",{}).get("documents",{}).get(self._part_doc_token,{}).get("title")
            lifecycle_event(self,"ALIAS_ADD",self._part_doc_token,title=title,
                            observation_stage="AFTER_FROZEN_SAVE_RETURN")
        lifecycle_event(self,"SAVE",self._part_doc_token,document=self._part,save_return=saved)
        if saved and self.diagnostics["document"] is not None:
            self.diagnostics["document"].update(saved=True,path=str(path))
            title,error=self._optional_native(self._part,"GetTitle")
            if error is None:self.diagnostics["document"]["title"]=title
            else:self.diagnostics["document"]["query_errors"]["title_after_save"]=error
        return saved

    def _resolve(self, record):
        return resolve_persistent_reference(member(self._part,"Extension"),record,
                                            resolver=self._reference_resolver)["native_entity"]

    def _same(self,a,b,kind="FEATURE"):
        ext=member(self._part,"Extension")
        # Compare current native identities after resolving historical bytes.
        return capture_persistent_reference(ext,a,kind)["encoded"]==capture_persistent_reference(ext,b,kind)["encoded"]

    def _current_refs(self):
        ext=member(self._part,"Extension")
        return {fid:capture_persistent_reference(ext,self._resolve({"reference_type":"FEATURE","encoded":ref}),"FEATURE")["encoded"]
                for fid,ref in self._feature_refs.items()}

    def support_candidates(self,request):
        self._api()
        ext=member(self._part,"Extension")
        candidates=[]
        if request.host_feature_id is None:
            try:
                feature=member(self._part,"FirstFeature")
            except Exception as exc:
                self.diagnostics["native_feature_chains"].append({"datum_token":request.token,"nodes":[],
                    "termination_reason":"FIRST_FEATURE_ERROR","exception":f"{type(exc).__name__}: {exc}"})
                raise
            candidates=self._trace_global_reference(ext,feature,request)
        else:
            # Resolve the host first; only its exact native owner can supply the support.
            host=self._resolve({"reference_type":"FEATURE","encoded":request.host_reference})
            refs=self._current_refs()
            for body in _invoke_com_method(self._part,"GetBodies2",0,False) or []:
                for face in member(body,"GetFaces") or []:
                    surface=member(face,"GetSurface")
                    if member(surface,"IsPlane") is not True or not self._same(member(face,"GetFeature"),host):
                        continue
                    measured=collect_plane_face(face,ext,refs)
                    origin=from_native([v/1000 for v in measured["origin_mm"]])
                    normal=from_native(measured["normal"])
                    box=source_bounds([v for v in member(face,"GetBox")])
                    footprint=(box[0],box[1],box[2],box[3])
                    inside=all((footprint[i] <= request.footprint_m[i]+LINEAR_TOLERANCE_MM/1000 if i%2==0
                                else footprint[i] >= request.footprint_m[i]-LINEAR_TOLERANCE_MM/1000) for i in range(4))
                    if (inside and abs(origin[2]-request.plane_z_m)<=LINEAR_TOLERANCE_MM/1000
                            and all(abs(a-b)<=AXIS_COMPONENT_TOLERANCE for a,b in zip(normal,request.normal))):
                        # Identity was already established by GetFeature. These
                        # samples verify material on that face, never rank faces
                        # or assign ownership by geometric proximity.
                        x0,x1,y0,y1=request.footprint_m
                        samples=[]
                        for x,y in itertools.product((x0,(x0+x1)/2,x1),(y0,(y0+y1)/2,y1)):
                            point=to_native((x,y,request.plane_z_m))
                            closest=_invoke_com_method(face,"GetClosestPointOn",*point)
                            require(_vector(closest,5),"trimmed support membership unavailable")
                            require(all(abs(a-b)<=LINEAR_TOLERANCE_MM/1000 for a,b in zip(closest[:3],point)),
                                    "requested support contains absent material")
                            samples.append({"native_point_m":list(point),"on_exact_face":True})
                        measured["trimmed_membership_samples"]=samples
                        candidates.append((face,{"reference_type":"FACE","encoded":measured["native_reference"]},measured))
        return candidates

    def _sketch_frame(self, diagnostic=None):
        if diagnostic is None:
            diagnostic={"GLOBAL_REFERENCE_CANDIDATE_EVIDENCE":None}
            self.diagnostics["sketch_basis"].append(diagnostic)
        basis=diagnostic.setdefault("ACTIVE_SKETCH_BASIS_EVIDENCE",{})
        basis.update(stage="SKETCH_BASIS_VALIDATION",sketch_created=self.diagnostics["geometry"]["sketch_created"],
                     sketch_active=False,transform_source="ISketch.ModelToSketchTransform",
                     transform_direction="SKETCH_TO_MODEL",inverse_requested="YES",transform_available=False,
                     native_coordinate_convention="SOLIDWORKS_MODEL_XYZ",
                     logical_mapping="native (x,y,z) -> logical (x,-z,y)",
                     modeling_primitive_called=self.diagnostics["geometry"]["modeling_primitive_called"],
                     sketch_role="PREPARATION_SKETCH",failed_components=["UNKNOWN"])
        sketch=member(member(self._part,"SketchManager"),"ActiveSketch")
        basis["sketch_active"]=sketch is not None
        require(sketch is not None,"active native sketch absent")
        try:
            basis["sketch_reference_identity"]=capture_persistent_reference(
                member(self._part,"Extension"),sketch,"SKETCH")["encoded"]
        except Exception as exc:
            basis["sketch_reference_identity"]=None
            basis["sketch_reference_error"]=f"{type(exc).__name__}: {exc}"
        model_to_sketch=member(sketch,"ModelToSketchTransform")
        transform=member(model_to_sketch,"Inverse")
        basis["transform_available"]=True
        for key,value in (("raw_array",model_to_sketch),("inverse_array",transform)):
            try:
                raw=member(value,"ArrayData")
                numbers=[float(x) for x in raw]
                if not all(math.isfinite(x) for x in numbers):
                    raise ValueError("non-finite transform array")
                basis[key]=numbers
                basis[key+"_length"]=len(numbers)
                basis[key+"_available"]=True
            except Exception as exc:
                basis[key]=None
                basis[key+"_length"]=None
                basis[key+"_available"]=False
                basis[key+"_error"]=f"{type(exc).__name__}: {exc}"
        basis["TRANSFORM_ARRAY_AVAILABLE"]=("YES" if basis["raw_array_available"] and
                                               basis["inverse_array_available"] else "NO")
        # IMathTransform ArrayData uses slot 12 for uniform scale; retain both
        # raw arrays so a reviewer can check the native convention independently.
        if basis["raw_array_length"]==16 and basis["inverse_array_length"]==16:
            basis["reported_scale"]={"model_to_sketch":basis["raw_array"][12],
                                     "sketch_to_model":basis["inverse_array"][12]}
        utility=member(self._session.sw,"GetMathUtility")
        points=[]
        native_points=[]
        source_origin=None
        for index,p in enumerate(((0,0,0),(1,0,0),(0,1,0))):
            basis[f"sketch_point_{index}_input"]=list(p)
            requested=list(p)
            boundary={"requested_input":requested,"method_used":"MultiplyTransform",
                      "transform_python_type":f"{type(transform).__module__}.{type(transform).__qualname__}",
                      "transform_has_oleobj":hasattr(transform,"_oleobj_")}
            basis.setdefault("math_point_boundaries",[]).append(boundary)
            argument=_double_safearray_point(requested)
            boundary.update(createpoint_argument_python_type=type(argument).__name__,
                createpoint_argument_repr=repr(argument),createpoint_argument_length=len(argument.value),
                createpoint_argument_values=list(argument.value),
                createpoint_element_python_types=[type(x).__name__ for x in argument.value],
                createpoint_explicit_typed_variant=True,createpoint_varianttype=argument.varianttype)
            resolution=boundary["createpoint_resolution"]={}
            try:
                source_point=_invoke_com_method(utility,"CreatePoint",argument,
                                                diagnostic=resolution,require_result=True)
            finally:
                _createpoint_classification(boundary)
            _math_object_diagnostic(boundary,"source_point",source_point)
            source_before=None
            try:
                source_before=_diagnostic_member(source_point,"ArrayData",
                    boundary.setdefault("source_arraydata_resolution",{}))
                _math_array_diagnostic(boundary,"source_point_arraydata",source_before)
            except Exception as exc:
                _math_array_diagnostic(boundary,"source_point_arraydata",error=exc)
            boundary["source_array_before_transform"]=boundary["source_point_arraydata_raw"]
            boundary["source_point_matches_requested_input"]=(boundary["source_point_arraydata_raw"]==requested
                if boundary["source_point_arraydata_available"] and boundary["source_point_arraydata_length"]==3 else None)
            boundary["CREATEPOINT_INPUT_RESULT_MISMATCH"]=(boundary["source_point_matches_requested_input"] is False)
            boundary["ARRAYDATA_TYPEINFO_STATUS"]=boundary.get("source_arraydata_resolution",{}).get(
                "dispatch_metadata",{}).get("typeinfo_status","UNKNOWN")
            boundary["convert_to_vector_probe"]=_mathpoint_scalar_probe(
                source_point,"ConvertToVector",(),math.sqrt(sum(x*x for x in requested)))
            boundary["CONVERT_TO_VECTOR_PROBE_VALID"]=boundary["convert_to_vector_probe"]["CONVERT_TO_VECTOR_PROBE_VALID"]
            if index==0:
                source_origin=source_point
                boundary["subtract_probe"]={"SUBTRACT_PROBE_VALID":"NO","status":"NOT_APPLICABLE_ORIGIN"}
            else:
                boundary["subtract_probe"]=_mathpoint_scalar_probe(
                    source_point,"Subtract",(source_origin,),math.dist(requested,(0,0,0)))
            boundary["SUBTRACT_PROBE_VALID"]=boundary["subtract_probe"]["SUBTRACT_PROBE_VALID"]
            _mathpoint_internal_state(boundary)
            require(_vector(source_before,3),"CreatePoint source ArrayData unavailable")
            returned_point=_invoke_com_method(source_point,"MultiplyTransform",transform,
                                             diagnostic=boundary.setdefault("multiplytransform_resolution",{}),
                                             require_result=True)
            _math_object_diagnostic(boundary,"returned_point",returned_point)
            boundary["source_is_returned_point"]=source_point is returned_point
            try:
                source_after=_diagnostic_member(source_point,"ArrayData",
                    boundary.setdefault("source_after_arraydata_resolution",{}))
                _math_array_diagnostic(boundary,"source_array_after_transform",source_after)
            except Exception as exc:
                _math_array_diagnostic(boundary,"source_array_after_transform",error=exc)
            try:
                native=_diagnostic_member(returned_point,"ArrayData",
                    boundary.setdefault("returned_arraydata_resolution",{}))
            except Exception as exc:
                _math_array_diagnostic(boundary,"returned_point_arraydata",error=exc)
                raise
            _math_array_diagnostic(boundary,"returned_point_arraydata",native)
            boundary["returned_array_after_transform"]=boundary["returned_point_arraydata_raw"]
            if _vector(native,3):
                basis[f"P{index}_native"]=list(native)
            else:
                basis[f"P{index}_native"]=None
                basis["failed_components"]=["NON_FINITE" if isinstance(native,(tuple,list)) and
                                            len(native)==3 else "UNKNOWN"]
            require(_vector(native,3),"native sketch transform unavailable")
            native_points.append(native)
            points.append(from_native(native))
            basis[f"P{index}_logical"]=list(points[-1])
        origin=points[0]
        u=tuple(a-b for a,b in zip(points[1],origin)); v=tuple(a-b for a,b in zip(points[2],origin))
        normal=(u[1]*v[2]-u[2]*v[1],u[2]*v[0]-u[0]*v[2],u[0]*v[1]-u[1]*v[0])
        nu=tuple(a-b for a,b in zip(native_points[1],native_points[0]))
        nv=tuple(a-b for a,b in zip(native_points[2],native_points[0]))
        nn=(nu[1]*nv[2]-nu[2]*nv[1],nu[2]*nv[0]-nu[0]*nv[2],nu[0]*nv[1]-nu[1]*nv[0])
        finite_basis=all(math.isfinite(x) for axis in (u,v,normal,nu,nv,nn) for x in axis)
        basis.update(u_raw=list(u) if finite_basis else None,v_raw=list(v) if finite_basis else None,
                     normal_raw=list(normal) if finite_basis else None,
                     u_native_raw=list(nu) if finite_basis else None,
                     v_native_raw=list(nv) if finite_basis else None,
                     normal_native_raw=list(nn) if finite_basis else None,
                     ORTHONORMAL_TOLERANCE=AXIS_COMPONENT_TOLERANCE)
        if finite_basis:
            u2,v2,n2=_dot(u,u),_dot(v,v),_dot(normal,normal)
            uv,un,vn=_dot(u,v),_dot(u,normal),_dot(v,normal)
            cross_dot=_dot(normal,normal)
            native_u2,native_v2,native_n2=_dot(nu,nu),_dot(nv,nv),_dot(nn,nn)
            values=(u2,v2,n2,uv,un,vn,cross_dot,native_u2,native_v2,native_n2,_dot(nu,nv))
            if all(math.isfinite(x) for x in values):
                residuals={"u_norm_sq":u2,"v_norm_sq":v2,"normal_norm_sq":n2,
                    "dot_uv":uv,"dot_un":un,"dot_vn":vn,"cross_uv":list(normal),
                    "cross_uv_dot_normal":cross_dot,"det_basis":cross_dot,
                    "unit_u_residual":abs(u2-1),"unit_v_residual":abs(v2-1),
                    "unit_normal_residual":abs(n2-1),"orthogonality_uv_residual":abs(uv),
                    "orthogonality_un_residual":abs(un),"orthogonality_vn_residual":abs(vn)}
                basis["logical_residuals"]=residuals
                basis["native_residuals"]={"u_norm_sq":native_u2,"v_norm_sq":native_v2,
                                           "normal_norm_sq":native_n2,"dot_uv":_dot(nu,nv)}
                failed=[]
                if u2==0:failed.append("ZERO_U")
                if v2==0:failed.append("ZERO_V")
                for code,residual in (("NON_UNIT_U",abs(u2-1)),("NON_UNIT_V",abs(v2-1)),
                                      ("U_V_NOT_ORTHOGONAL",abs(uv)),("NON_UNIT_NORMAL",abs(n2-1))):
                    if residual>AXIS_COMPONENT_TOLERANCE:failed.append(code)
                basis["failed_components"]=failed
                basis["failure_component"]=("MULTIPLE_COMPONENTS" if len(failed)>1 else
                                             failed[0] if failed else "PASS")
        if basis["failed_components"]==["UNKNOWN"]:
            basis["failed_components"]=["NON_FINITE"]
            basis["failure_component"]="NON_FINITE"
        require(all(abs(_dot(a,a)-1)<=AXIS_COMPONENT_TOLERANCE for a in (u,v,normal))
                and abs(_dot(u,v))<=AXIS_COMPONENT_TOLERANCE,"sketch basis is not orthonormal")
        # Preserve exactly the object/reference already measured in this frame.
        # Runtime objects stay outside the serializable diagnostic containers.
        self._preparation_sketch=(self._part,sketch,basis["sketch_reference_identity"])
        return origin,u,v,normal

    def bind_sketch(self, request):
        self._preparation_sketch=None
        self._native_sketch_handoff=None
        candidates=self.support_candidates(request)
        require(len(candidates)==1,f"exact native support requires one candidate, found {len(candidates)}")
        entity,record,measured=candidates[0]
        diagnostic={"source_datum_identity":request.token,
                    "GLOBAL_REFERENCE_CANDIDATE_EVIDENCE":{
                        "source_candidate_feature_type":str(member(entity,"GetTypeName2")) if request.host_feature_id is None else "FACE",
                        "source_candidate_persistent_reference":record["encoded"],
                        "source_candidate_reference_type":record["reference_type"],
                        "diagnostic_name":self._optional_native(entity,"Name")[0],
                        "collected_origin_mm":measured.get("origin_mm"),
                        "collected_normal":measured.get("normal"),
                        "collector_source":measured.get("source"),
                        "candidate_count":len(candidates),"candidate_unique":True,
                        "semantic_orientation_result":"PASS"}}
        if request.host_feature_id is None:
            try:
                raw=member(member(entity,"GetSpecificFeature2"),"GetRefPlaneParams")
                if not _vector(raw,9):raise ValueError("invalid/non-finite reference plane params")
                diagnostic["GLOBAL_REFERENCE_CANDIDATE_EVIDENCE"].update(
                    ref_plane_params_raw=list(raw),collected_x_direction_raw=list(raw[3:6]))
            except Exception as exc:
                diagnostic["GLOBAL_REFERENCE_CANDIDATE_EVIDENCE"]["ref_plane_params_error"]=f"{type(exc).__name__}: {exc}"
        self.diagnostics["sketch_basis"].append(diagnostic)
        _invoke_com_method(self._part,"ClearSelection2",True)
        require(_invoke_com_method(entity,"Select2",False,0) is True,"native support selection failed")
        diagnostic["SKETCH_INSERT_CALLED"]=False
        diagnostic["SKETCH_CREATED"]=False
        diagnostic["MODELING_PRIMITIVE_CALLED"]=self.diagnostics["geometry"]["modeling_primitive_called"]
        self.diagnostics["geometry"]["sketch_created"]=None
        diagnostic["SKETCH_INSERT_CALLED"]=True
        _invoke_com_method(member(self._part,"SketchManager"),"InsertSketch",True)
        self.diagnostics["geometry"]["sketch_created"]=True
        diagnostic["SKETCH_CREATED"]=True
        origin,u,v,normal=self._sketch_frame(diagnostic)
        require(abs(origin[2]-request.plane_z_m)<=LINEAR_TOLERANCE_MM/1000
                and abs(abs(normal[2])-1)<=AXIS_COMPONENT_TOLERANCE,"actual sketch plane contradicts support")
        self._capture_sketch_handoff(record,diagnostic)
        self._support_refs[request.token]=record
        self._support_geometry[request.token]=measured
        return DatumCandidate(record["encoded"],request.kind,request.support_region,request.host_reference,
                              request.document_id,tuple(origin),u,v,normal,request.footprint_m,
                              "SIMULATED" if self.simulated else "NATIVE"),record

    def _capture_sketch_handoff(self, support, diagnostic):
        self._verify_boundary()
        require(self._preparation_sketch is not None,"measured native sketch unavailable")
        document,sketch,encoded=self._preparation_sketch
        require(document is self._part and encoded,"measured sketch persistent identity unavailable")
        reference=base64.b64decode(encoded,validate=True)
        require(bool(reference),"empty measured sketch identity")
        row=diagnostic.setdefault("NATIVE_SKETCH_HANDOFF",{})
        row.update(capture_stage="BEFORE_END_SKETCH",capture_source="MEASURED_ACTIVE_SKETCH",
                   sketch_persistent_reference=encoded,document_id=self._run_id,
                   session_provenance=asdict(self._identity),identity_status="FAIL",
                   SKETCH_PRIMITIVE_REACHED=False,SKETCH_PRIMITIVE_RESULT="NOT_REACHED",
                   EXTRUSION_PRIMITIVE_REACHED=False,EXTRUSION_PRIMITIVE_RESULT="NOT_REACHED",
                   COMPOUND_OPERATION_ENTERED=False)
        feature=_native_sketch_to_feature(sketch,row.setdefault("interface_conversion",{}))
        specific=_invoke_com_method(feature,"GetSpecificFeature2",require_result=True,
                                    diagnostic=row.setdefault("roundtrip_method",{}))
        ext=member(self._part,"Extension")
        roundtrip=capture_persistent_reference(ext,specific,"SKETCH")["encoded"]
        row["roundtrip_sketch_reference"]=roundtrip
        require(roundtrip==encoded,"sketch interface roundtrip persistent identity mismatch")
        feature_ref=None
        try:
            feature_encoded=capture_persistent_reference(ext,feature,"FEATURE")["encoded"]
            feature_ref=base64.b64decode(feature_encoded,validate=True)
            row.update(feature_persistent_reference=feature_encoded,feature_persistent_identity="PASS")
        except Exception as exc:
            # A closed sketch roundtrip is mandatory; this extra feature anchor
            # is optional. Frozen parameterized collectors are not changed here.
            row.update(feature_persistent_reference=None,feature_persistent_identity="PARTIAL",
                       feature_reference_error=f"{type(exc).__name__}: {exc}")
        row.update(identity_status="PASS",roundtrip_status="PASS")
        self._native_sketch_handoff=_NativeSketchHandoff(sketch,feature,reference,feature_ref,
            self._part,self._identity,self._run_id,support["encoded"],"BEFORE_END_SKETCH",
            "MEASURED_ACTIVE_SKETCH",row)

    def _crosscheck_backend_sketch(self, backend_ref, handoff):
        ext=member(self._part,"Extension")
        sketch=getattr(backend_ref,"sketch",None)
        feature=getattr(backend_ref,"feature",None)
        row=handoff.diagnostic
        row.update(backend_sketch_crosscheck="NOT_APPLICABLE",backend_feature_crosscheck="NOT_APPLICABLE")
        if sketch is not None:
            row["backend_sketch_crosscheck"]="FAIL"
            ref=capture_persistent_reference(ext,sketch,"SKETCH")["encoded"]
            require(base64.b64decode(ref,validate=True)==handoff.sketch_persistent_reference,
                    "backend sketch persistent identity mismatch")
            row["backend_sketch_crosscheck"]="PASS"
        if feature is not None:
            row["backend_feature_crosscheck"]="FAIL"
            if handoff.feature_persistent_reference is not None:
                ref=capture_persistent_reference(ext,feature,"FEATURE")["encoded"]
                require(base64.b64decode(ref,validate=True)==handoff.feature_persistent_reference,
                        "backend feature persistent identity mismatch")
            else:
                specific=_invoke_com_method(feature,"GetSpecificFeature2",require_result=True)
                ref=capture_persistent_reference(ext,specific,"SKETCH")["encoded"]
                require(base64.b64decode(ref,validate=True)==handoff.sketch_persistent_reference,
                        "backend feature sketch identity mismatch")
            row["backend_feature_crosscheck"]="PASS"

    def _validate_profile_continuity(self, command, sketch_ref):
        handoff=self._native_sketch_handoff
        require(handoff is not None and handoff.document is self._part
                and handoff.operation_id==command.operation_id,"operation sketch handoff unavailable")
        expected=handoff.feature_persistent_reference or handoff.sketch_persistent_reference
        handoff.diagnostic["profile_continuity"]="FAIL"
        require(base64.b64decode(sketch_ref,validate=True)==expected,
                "post-extrusion profile persistent identity mismatch")
        handoff.diagnostic["profile_continuity"]="PASS"

    def call_primitive(self, command):
        api=self._api()
        require(command.profile_kind in {"RECTANGLE","CIRCLE","STRAIGHT_SLOT"},"unsupported bounded profile")
        slot_args=_slot_arguments(command) if command.profile_kind=="STRAIGHT_SLOT" else None
        handoff=self._native_sketch_handoff
        require(handoff is not None and handoff.document is self._part
                and handoff.session_identity is self._identity and handoff.run_id==self._run_id
                and handoff.support_reference==command.datum.reference
                and handoff.operation_id is None,"current operation native sketch identity unavailable or consumed")
        if slot_args is not None:
            require(handoff.native_feature is not None,"slot native sketch feature unavailable")
        handoff.operation_id=command.operation_id
        row=handoff.diagnostic
        row["COMPOUND_OPERATION_ENTERED"]=True
        self.diagnostics["backend_boundary"]["modeling_primitive_reached"]=True
        self.diagnostics["geometry"]["modeling_primitive_called"]=True
        self.diagnostics["geometry"]["body_created"]=None
        row.update(SKETCH_PRIMITIVE_REACHED=True,SKETCH_PRIMITIVE_RESULT="FAIL")
        if command.profile_kind=="RECTANGLE":
            for a,b in zip(command.points_m,command.points_m[1:]+command.points_m[:1]):
                require(api.sketch_line(self._part,*a,*b) is not None,"external sketch line returned None")
        elif command.profile_kind=="CIRCLE":
            require(api.sketch_circle(self._part,*command.points_m[0],command.size_m) is not None,
                    "external sketch circle returned None")
        else:
            result=_invoke_com_method(member(self._part,"SketchManager"),"CreateSketchSlot",*slot_args,
                                      diagnostic=row.setdefault("slot_primitive",{}),require_result=True)
            require(not isinstance(result,(bool,int,float,str)),"slot primitive returned invalid native object")
        sketch=api.end_sketch(self._part)
        row["SKETCH_PRIMITIVE_RESULT"]="PASS"
        cache_key=getattr(api,"_model_cache_key",None)
        if callable(cache_key):self._owned_sketch_cache_keys.add(cache_key(self._part))
        self._crosscheck_backend_sketch(sketch,handoff)
        native=handoff.native_feature
        row["selection_validation"]="FAIL"
        _invoke_com_method(self._part,"ClearSelection2",True)
        selected_ok=_invoke_com_method(native,"Select2",False,0)
        row["selection_success"]=selected_ok is True
        require(selected_ok is True,"exact sketch selection failed; no name fallback")
        count=_invoke_com_method(member(self._part,"SelectionManager"),"GetSelectedObjectCount2",-1)
        row["selected_count"]=count
        require(type(count) is int and count==1,"exact sketch selection set is not unique")
        row.update(selection_validation="PASS",selection_success=True,selected_count=count)
        # The pinned helper explicitly accepts this sentinel and immediately
        # uses the existing selection. Passing a name/ref would enable its
        # FeatureByName/SelectByID2 recovery, which this boundary forbids.
        selected="__current_selection__"
        if command.operation_type in {"base_extrude","boss_extrude"}:
            self.diagnostics["geometry"]["feature_created"]=None
            row.update(EXTRUSION_PRIMITIVE_REACHED=True,EXTRUSION_PRIMITIVE_RESULT="FAIL")
            result=api.extrude_boss(self._part,selected,command.depth_m,
                                    direction=command.reverse_from_sketch_normal,merge=True)
            self.diagnostics["geometry"]["feature_created"]=result is not None and not isinstance(result,(bool,int,str))
            require(self.diagnostics["geometry"]["feature_created"],"external extrusion returned invalid native feature")
            row["EXTRUSION_PRIMITIVE_RESULT"]="PASS"
            self._refresh_body_diagnostic()
            return result
        # Reviewed external cut's third argument Dir is fixed False. It cannot
        # reverse an opposite sketch frame; refuse before feature creation.
        require(_dot(command.model_direction,command.datum.normal)<0,
                "reviewed cut primitive cannot express the measured sketch-normal direction")
        require(command.depth_m>0,"bounded host cut may not request global through-all")
        self.diagnostics["geometry"]["feature_created"]=None
        row.update(EXTRUSION_PRIMITIVE_REACHED=True,EXTRUSION_PRIMITIVE_RESULT="FAIL")
        result=api.extrude_cut(self._part,selected,command.depth_m,direction=True,flip=False)
        self.diagnostics["geometry"]["feature_created"]=result is not None and not isinstance(result,(bool,int,str))
        require(self.diagnostics["geometry"]["feature_created"],"external extrusion returned invalid native feature")
        row["EXTRUSION_PRIMITIVE_RESULT"]="PASS"
        self._refresh_body_diagnostic()
        return result

    def _refresh_body_diagnostic(self):
        try:
            count=len(_invoke_com_method(self._part,"GetBodies2",0,False) or [])
            self.diagnostics["geometry"]["body_created"]=count>0
            self.diagnostics["document"]["body_count"]=count
        except Exception as exc:
            self.diagnostics["document"]["query_errors"]["body_count_after_primitive"]=f"{type(exc).__name__}: {exc}"

    def native_row(self, operation, requirements, command, feature, support, selected_history_id, evidence):
        require(feature is not None and not isinstance(feature,(bool,int,str)),"external helper returned invalid native feature")
        ext=member(self._part,"Extension")
        kind=str(member(feature,"GetTypeName2"))
        underlying=str(member(feature,"GetTypeName")) if kind=="ICE" else kind
        allowed={"Cut","CutExtrude"} if operation.type.startswith("cut_") else {"Extrusion","Boss","BossExtrude"}
        require(underlying in allowed,"native feature family mismatch")
        error=collect_feature_error(feature,warning_slot_factory=self._warning_slot_factory)
        require(error["api_status"]=="PASS" and type(error["error_code"]) is int and error["error_code"]==0
                and error["warning_flag"] is False,"native feature error/warning unknown or nonzero")
        require(member(feature,"IsSuppressed") is False,"native suppression unproven")
        feature_ref=capture_persistent_reference(ext,feature,"FEATURE")
        profile,sketch_ref=native_profile_feature(ext,feature)
        self._validate_profile_continuity(command,sketch_ref)
        parents=[capture_persistent_reference(ext,p,"FEATURE") for p in member(profile,"GetParents") or []]
        parent=(self._resolve({"reference_type":"FEATURE","encoded":self._feature_refs[requirements.datum.host_feature]})
                if requirements.datum.host_feature else self._resolve(support))
        require(sum(self._same(self._resolve(p),parent) for p in parents)==1,"intended sketch host lineage missing or ambiguous")
        child_diagnostic={"operation_id":operation.operation_id,"source_feature_id":operation.source_feature_id}
        self.diagnostics.setdefault("getchildren_dispatch",[]).append(child_diagnostic)
        children=[capture_persistent_reference(ext,p,"FEATURE") for p in _invoke_getchildren(feature,child_diagnostic)]
        require(not any(self._same(feature,f) for f in self._features.values()),"duplicate native feature identity")
        self._features[operation.source_feature_id]=feature
        self._feature_refs[operation.source_feature_id]=feature_ref["encoded"]
        self._sketch_refs[operation.source_feature_id]=sketch_ref
        return {"operation_id":operation.operation_id,"source_feature_id":operation.source_feature_id,
                "feature_type":{"base_extrude":"BASE_BLOCK","boss_extrude":"BOSS",
                                "cut_extrude_through_circle":"THROUGH_HOLE",
                                "cut_extrude_through_slot":"STRAIGHT_SLOT"}[operation.type],"selected_history_id":selected_history_id,
                "dependency_ids":list(operation.depends_on_operation_ids),"host_feature_id":requirements.datum.host_feature,
                "datum_requirement":asdict(requirements.datum),"requested_parameters":asdict(command),
                "backend_primitive":"sw_part.extrude_cut" if operation.type.startswith("cut_") else "sw_part.extrude_boss",
                "native_feature_reference":feature_ref,"native_sketch_reference":{"reference_type":"SKETCH","encoded":sketch_ref},
                "native_support_reference":support,"feature_error":error,"suppressed":False,
                "parent_evidence":parents,"child_evidence":children,
                "source_evidence":{eid:evidence[eid] for eid in requirements.evidence_ids},
                "execution_status":"BACKEND_CALL_SUCCESS_NOT_ENGINEERING_VALIDATED",
                "evidence_kind":"SIMULATED" if self.simulated else "NATIVE"}

    def execute(self, planned, composition, *, evidence_digests):
        canonical=_preflight(planned,composition)
        require(not self.ledger,"adapter cannot be reused for another execution attempt")
        require(all(op.type in {"base_extrude","boss_extrude","cut_extrude_through_circle","cut_extrude_through_slot"}
                    for op in canonical.modeling_plan.operations),"unsupported bounded operation")
        document=self._run_id  # Document is run-owned, not identified by a display name.
        requirements={r.operation_id:r for r in canonical.operation_requirements}
        completed=set()
        evidence=dict(evidence_digests)
        needed={eid for row in canonical.operation_requirements for eid in row.evidence_ids}
        require(needed<=set(evidence) and all(isinstance(evidence[eid],str) and len(evidence[eid])==64
                and all(c in "0123456789abcdef" for c in evidence[eid]) for eid in needed),"source evidence digests incomplete")
        self.new_part()
        self.execution_gates["document_creation"]="PASS"
        self.diagnostics["pre_operation_native_snapshot"]={"session":self.diagnostics["session"],
            "document":dict(self.diagnostics["document"]),"feature_chain_diagnostic":self.diagnostics["native_feature_chains"],
            "body_count":self.diagnostics["document"]["body_count"]}
        for op in canonical.modeling_plan.operations:
            row=requirements[op.operation_id]
            require(set(op.depends_on_operation_ids)<=completed,"operation dependencies unavailable")
            host=row.datum.host_feature
            require(host is None or host in self._feature_refs,"datum host unavailable")
            b=op.profile["region_bounds_mm"]
            req=SupportRequest(row.datum.token,row.datum.kind,row.datum.support_region,host,
                self._feature_refs.get(host),document,b[5 if op.type.startswith("cut_") else 4]/1000,
                (0,0,1),tuple(v/1000 for v in b[:4]))
            datum,support=self.bind_sketch(req)
            checks={
                "DOMAIN_VALID":composition.status=="INFERRED",
                "TOPOLOGY_VALID":composition.status=="INFERRED",
                "DATUM_AVAILABLE":row.datum.token in self._support_refs,
                "HOST_AVAILABLE":host in self._feature_refs,
                "SUPPORT_REGION_AVAILABLE":datum.support_region==row.datum.support_region,
                "MATERIAL_EXISTS":host in self._features,
            }
            preconditions=[]
            for check in row.preconditions:
                passed=(check.reference in completed if check.kind=="DEPENDENCY_COMPLETED" else checks.get(check.kind,False))
                require(passed,f"operation precondition failed: {check.kind}")
                preconditions.append({**asdict(check),"verified":True})
            command=_command(op,datum,(0,0,0))
            self.commands.append(command)
            try:
                feature=self.call_primitive(command)
                self.ledger.append(self.native_row(op,row,command,feature,support,canonical.selected_history_id,evidence))
                self.ledger[-1]["precondition_evidence"]=preconditions
            except Exception as exc:
                self.ledger.append({"operation_id":op.operation_id,"source_feature_id":op.source_feature_id,
                    "selected_history_id":canonical.selected_history_id,"dependency_ids":list(op.depends_on_operation_ids),
                    "host_feature_id":host,"datum_requirement":asdict(row.datum),"requested_parameters":asdict(command),
                    "execution_status":"FAILED","native_feature_reference":None,"failure_reason":str(exc),
                    "evidence_kind":"SIMULATED" if self.simulated else "NATIVE"})
                diagnostics=self.diagnostics.get("getchildren_dispatch",[])
                if diagnostics and diagnostics[-1]["operation_id"]==op.operation_id:
                    self.ledger[-1]["getchildren_dispatch_diagnostic"]=diagnostics[-1]
                raise
            completed.add(op.operation_id)
        self.rebuild_and_resolve()
        self.execution_gates.update(canonical_operations="PASS",operation_checks="PASS",rebuild="PASS")
        return {"status":"SIMULATED" if self.simulated else "BACKEND_CALL_SUCCESS",
                "engineering_status":"NOT_VALIDATED","generation_truth_reads":0,
                "plan_digest":canonical_digest(canonical.to_dict()),"ledger":self.ledger}

    def rebuild_and_resolve(self):
        self._api()
        require(_invoke_com_method(self._part,"ForceRebuild3",False) is True,"native rebuild did not explicitly succeed")
        ext=member(self._part,"Extension")
        rows=[]
        for row in self.ledger:
            feature=self._resolve(row["native_feature_reference"])
            profile,sketch_ref=native_profile_feature(ext,feature)
            require(self._same(profile,self._resolve(row["native_sketch_reference"]),"SKETCH"),"sketch identity changed")
            support=self._resolve(row["native_support_reference"])
            host=row["host_feature_id"]
            expected=(self._resolve({"reference_type":"FEATURE","encoded":self._feature_refs[host]}) if host else support)
            parents=member(profile,"GetParents") or []
            require(sum(self._same(parent,expected) for parent in parents)==1,"native dependency unresolved after rebuild")
            error=collect_feature_error(feature,warning_slot_factory=self._warning_slot_factory)
            require(error["api_status"]=="PASS" and error["error_code"]==0 and error["warning_flag"] is False
                    and member(feature,"IsSuppressed") is False,"rebuilt feature state invalid")
            self._features[row["source_feature_id"]]=feature
            rows.append({"source_feature_id":row["source_feature_id"],"resolved":True,"feature_error":error,
                         "support_resolved":True,"sketch_resolved":True,"dependencies_resolved":True})
        return {"status":"PASS","rows":rows,"evidence_kind":"SIMULATED" if self.simulated else "NATIVE"}

    def reopen_part(self,path):
        self._verify_boundary()
        require(self._part is None and Path(path).is_file(),"reopen requires closed saved part")
        self._part=self._session.open(str(path),read_only=True,silent=True)
        require(self._part is not None and Path(str(member(self._part,"GetPathName"))).resolve()==Path(path).resolve(),"reopen exact path failed")
        self._owned_titles.add(str(member(self._part,"GetTitle")))
        lifecycle_event(self,"REOPEN",self._part_doc_token,document=self._part)
        lifecycle_event(self,"ALIAS_ADD",self._part_doc_token,document=self._part,
                        title=self.diagnostics.get("document_lifecycle",{}).get("documents",{}).get(self._part_doc_token,{}).get("title"))
        return self.rebuild_and_resolve()

    def release_sketch_cache(self,model):
        cache=getattr(self._part_api,"_SKETCH_SELECTION_CACHE",None)
        if isinstance(cache,dict):
            # Save As changes the upstream model's cache key. Retain keys when
            # sketches are captured, rather than looking up only the new title.
            for key in self._owned_sketch_cache_keys:cache.pop(key,None)
            self._owned_sketch_cache_keys.clear()

    def close_part(self):
        self._verify_boundary()
        self.release_sketch_cache(self._part)
        if self._part is None:return super().close_part()
        lifecycle_event(self,"TITLE_BEFORE_CLOSE",self._part_doc_token,document=self._part)
        title=self.diagnostics.get("document_lifecycle",{}).get("documents",{}).get(self._part_doc_token,{}).get("title","NOT_RECORDED")
        result=observe_close_call(self,self._part_doc_token,title,super().close_part)
        if result is True:lifecycle_event(self,"ALIAS_REMOVE",self._part_doc_token,title=title,registry_operation="remove")
        return result

    def collect_native(self, *, observation_context=None):
        observation=None
        if observation_context is not None:
            from .r004_native_observed_evidence import (
                _n1e_begin_transport, _n1e_retain_incidence, _n1e_finish_transport,
            )
            observation=_n1e_begin_transport(self,observation_context)
        self._api()
        ext=member(self._part,"Extension")
        if observation is not None:
            require(ext is observation["extension"],"controlled native extension changed")
        body_count=(collect_body_count(self._part,inventory_context=observation["context"])
                    if observation is not None else None)
        if observation is not None:
            observation["inventory"]=body_count["typed_inventory"]
        refs=self._current_refs()
        reverse={r:fid for fid,r in refs.items()}
        faces=[]
        for body in ([observation["inventory"]["source_body"]] if observation is not None
                     else _invoke_com_method(self._part,"GetBodies2",0,False) or []):
            for face in ([entity for _,entity in observation["inventory"]["capture_record"]["faces"]]
                         if observation is not None else member(body,"GetFaces") or []):
                surface=member(face,"GetSurface")
                native_owner=member(face,"GetFeature")
                require(native_owner is not None,"native face ownership unavailable")
                owner=capture_persistent_reference(ext,native_owner,"FEATURE")
                record={"reference":capture_persistent_reference(ext,face,"FACE"),"owner_reference":owner,
                    "native_owner_feature_id":reverse.get(owner["encoded"]),
                    "bounds_mm":source_bounds([v*1000 for v in member(face,"GetBox")]),
                    "area_mm2":float(member(face,"GetArea"))*1e6}
                if member(surface,"IsPlane") is True:
                    params=member(surface,"PlaneParams")
                    record.update(surface_type="PLANE",normal=list(from_native(member(face,"Normal"))),
                                  origin_mm=list(from_native([v*1000 for v in params[3:6]])))
                elif member(surface,"IsCylinder") is True:
                    params=member(surface,"CylinderParams")
                    record.update(surface_type="CYLINDER",axis=list(from_native(params[3:6])),
                        center_mm=list(from_native([v*1000 for v in params[:3]])),radius_mm=abs(params[6])*1000,
                        interior=member(face,"FaceInSurfaceSense") is True)
                else:
                    record["surface_type"]="UNSUPPORTED"
                edges=[]
                for edge in member(face,"GetEdges") or []:
                    adjacent=member(edge,"GetTwoAdjacentFaces2") or []
                    require(len(adjacent)==2 and all(a is not None for a in adjacent),"non-manifold native edge")
                    edges.append({"reference":capture_persistent_reference(ext,edge,"EDGE"),
                        "adjacent_face_references":[capture_persistent_reference(ext,a,"FACE")["encoded"] for a in adjacent]})
                    if observation is not None:
                        _n1e_retain_incidence(observation,face,record,edge,adjacent,edges[-1])
                record["edges"]=edges
                require(self._same(face,self._resolve(record["reference"]),"FACE"),"native face persistent resolution failed")
                record["persistent_reference_resolved"]=True
                faces.append(record)
        mass=member(ext,"CreateMassProperty")
        volume=float(member(mass,"Volume"))*1e9
        result={"evidence_kind":"SIMULATED" if self.simulated else "NATIVE",
            "body":({key:value for key,value in body_count.items() if key!="typed_inventory"}
                    if observation is not None else collect_body_count(self._part)),
            "overall_bounds_mm":source_bounds(collect_signed_part_bounds(self._part)),"volume_mm3":volume,"faces":faces,
            "source":"native final body: GetFeature/GetPersistReference3/GetSurface/GetArea/CreateMassProperty"}
        if observation is not None:
            result["_observation"]=_n1e_finish_transport(observation,result)
        return result

    def _math_point(self, point, transform):
        utility=member(self._session.sw,"GetMathUtility")
        source=_invoke_com_method(utility,"CreatePoint",_double_safearray_point(point),require_result=True)
        transformed=_invoke_com_method(source,"MultiplyTransform",transform,require_result=True)
        result=member(transformed,"ArrayData")
        require(_vector(result,3),"native drawing point transform unavailable")
        return list(result)

    def _insert_centerlines(self,drawing,view,role,native):
        # Annotation intent uses measured cylinders, never evaluator annotations.
        require(_invoke_com_method(drawing,"ActivateView",str(member(view,"Name"))) is True,"drawing view activation failed")
        manager=member(drawing,"SketchManager")
        _invoke_com_method(manager,"InsertSketch",True)
        sketch=member(manager,"ActiveSketch")
        require(sketch is not None,"native drawing view sketch unavailable")
        model_to_view=member(view,"ModelToViewTransform")
        sheet_to_sketch=member(sketch,"ModelToSketchTransform")
        # Native drawing inference can snap an endpoint to another model view.
        # AddToDB is drawing-local and restored even when entity creation fails.
        diagnostic={"role":role,"source":"ISketchManager.AddToDB native get/set",
                    "prior":None,"applied":None,"restored":None,"created_count":0}
        self.diagnostics.setdefault("drawing_centerline_inference",[]).append(diagnostic)
        prior=member(manager,"AddToDB")
        diagnostic["prior"]=prior
        require(type(prior) is bool,"native drawing AddToDB prior value unavailable")
        try:
            manager.AddToDB=True
            diagnostic["applied"]=member(manager,"AddToDB")
            require(diagnostic["applied"] is True,"native drawing AddToDB was not enabled")
            for face in native["faces"]:
                if face["surface_type"]!="CYLINDER" or face.get("interior") is not True:
                    continue
                x,y=face["center_mm"][:2]; r=face["radius_mm"]; lo,hi=face["bounds_mm"][4:6]
                pairs=([((x-r-2,y,hi),(x+r+2,y,hi)),((x,y-r-2,hi),(x,y+r+2,hi))] if role=="TOP" else
                       [((x,y,lo-2),(x,y,hi+2))])
                for a,b in pairs:
                    local=[]
                    for p in (a,b):
                        sheet=self._math_point(to_native(tuple(v/1000 for v in p)),model_to_view)
                        local.append(self._math_point(sheet,sheet_to_sketch))
                    require(_invoke_com_method(manager,"CreateCenterLine",*local[0],*local[1]) is not None,
                            "native centerline creation failed")
                    diagnostic["created_count"]+=1
        except Exception as exc:
            diagnostic["creation_error"]=f"{type(exc).__name__}: {exc}"
            raise
        finally:
            manager.AddToDB=prior
            diagnostic["restored"]=member(manager,"AddToDB")
            require(diagnostic["restored"] is prior,"native drawing AddToDB restore failed")
        _invoke_com_method(manager,"InsertSketch",True)

    def drawing_roundtrip(self,part_path,drawing_path,native, *, view_factory=None,
                          collector=None, projection_standard="FIRST_ANGLE"):
        require(projection_standard in {"FIRST_ANGLE","THIRD_ANGLE"},"explicit drawing projection standard required")
        first_angle=projection_standard=="FIRST_ANGLE"
        make_views=view_factory or create_r003_physical_views
        collect=collector or self._collect_drawing
        self._api()
        drawing=self._session.new_drawing()
        require(drawing is not None,"drawing creation returned None")
        initial_title=str(member(drawing,"GetTitle"))
        self._owned_titles.add(initial_title)
        drawing_token=lifecycle_event(self,"REGISTER",kind="DRAWING",document=drawing,title=initial_title)
        lifecycle_event(self,"ALIAS_ADD",drawing_token,title=initial_title)
        self.drawing_gates["drawing_creation"]="PASS"
        views=make_views(drawing,self._part,str(part_path),empty_variant_factory=self._empty_variant_factory)["views"]
        _drawing_title_after_model_views(self, drawing, drawing_token, initial_title)
        sheet=member(drawing,"GetCurrentSheet")
        properties=list(member(sheet,"GetProperties2"))
        require(len(properties)==8,"native sheet properties unavailable")
        _invoke_com_method(sheet,"SetProperties2",int(properties[0]),int(properties[1]),properties[2],properties[3],first_angle,
               properties[5],properties[6],bool(properties[7]))
        require(list(member(sheet,"GetProperties2"))[4]==float(first_angle),"projection sheet setting not acknowledged")
        positions=({"FRONT":(.10,.18),"TOP":(.10,.08),"LEFT":(.22,.18)} if first_angle else
                   {"FRONT":(.18,.10),"TOP":(.18,.22),"LEFT":(.06,.10)})
        for role,record in views.items():
            view=record["native_view"]
            view.Position=positions[role]
            if native is not None:
                self._insert_centerlines(drawing,view,role,native)
        require(_invoke_com_method(drawing,"ForceRebuild3",False) is True,"drawing rebuild failed")
        # Yield raw evidence to the evaluator before saving. No truth or
        # evaluator callback enters the generation adapter.
        yield collect(drawing,part_path,roundtrip=False)
        old_title=str(member(drawing,"GetTitle"))
        lifecycle_event(self,"TITLE_BEFORE_SAVE",drawing_token,document=drawing,title=old_title)
        require(self._session.save(drawing,str(drawing_path)) is True and Path(drawing_path).is_file()
                and Path(drawing_path).stat().st_size>0,"drawing save failed")
        title=str(member(drawing,"GetTitle")); self._owned_titles.discard(old_title); self._owned_titles.add(title)
        lifecycle_event(self,"SAVE_RENAME",drawing_token,document=drawing,title=title,previous_native_title=old_title)
        lifecycle_event(self,"SAVE",drawing_token,title=title,save_return=True)
        lifecycle_event(self,"ALIAS_REMOVE",drawing_token,title=old_title,registry_operation="discard")
        lifecycle_event(self,"ALIAS_ADD",drawing_token,title=title)
        require(observe_close_call(self,drawing_token,title,lambda:self._session.close(title=title)) is True,"drawing close failed")
        self._owned_titles.remove(title)
        lifecycle_event(self,"ALIAS_REMOVE",drawing_token,title=title,registry_operation="remove")
        reopened=self._session.open(str(drawing_path),read_only=True,silent=True)
        require(reopened is not None and Path(str(member(reopened,"GetPathName"))).resolve()==Path(drawing_path).resolve(),
                "drawing reopen exact path failed")
        title=str(member(reopened,"GetTitle")); self._owned_titles.add(title)
        lifecycle_event(self,"REOPEN",drawing_token,document=reopened,title=title)
        lifecycle_event(self,"ALIAS_ADD",drawing_token,title=title)
        require(_invoke_com_method(reopened,"ForceRebuild3",False) is True,"reopened drawing rebuild failed")
        require(list(member(member(reopened,"GetCurrentSheet"),"GetProperties2"))[4]==float(first_angle),"reopened sheet projection changed")
        final=collect(reopened,part_path,roundtrip=True)
        require(observe_close_call(self,drawing_token,title,lambda:self._session.close(title=title)) is True,"reopened drawing close failed")
        self._owned_titles.remove(title)
        lifecycle_event(self,"ALIAS_REMOVE",drawing_token,title=title,registry_operation="remove")
        self.drawing_gates["drawing_save_reopen"]="PASS"
        yield final

    def _collect_drawing(self,drawing,part_path, *, roundtrip):
        self._verify_boundary()
        require(list(member(member(drawing,"GetCurrentSheet"),"GetProperties2"))[4]==1.0,"native sheet not first-angle")
        roles={"FRONT","TOP","LEFT"}
        hlr,hlv,frames,centers={},{},{},{}
        current=next_native(member(drawing,"GetFirstView"),"GetNextView")
        from experiments.three_view_reconstruction.drawing.view_orientation import canonicalize
        while current is not None:
            role=canonicalize(str(member(current,"GetOrientationName"))).canonical_role.value
            require(role in roles and role not in frames,"unexpected/duplicate physical view")
            # Frozen collector's THIRD_ANGLE argument selects its canonical basis;
            # actual sheet standard is measured separately and retained explicitly.
            frame=normalize_native_view_transform(current,expected_role=role,model_reference=str(part_path),projection_standard="THIRD_ANGLE")
            frame["collector_basis_convention"]="THIRD_ANGLE"
            frame["projection_standard"]="FIRST_ANGLE"
            frame["sheet_first_angle_native_readback"]=True
            frames[role]=frame
            hlr[role],hlv[role]=collect_r003_native_view_modes(drawing,current,role=role,model_path=str(part_path))
            for row in (hlr[role],hlv[role]):row["primitive_source_space"]="MODEL_PROJECTED_PRIMITIVE"
            rows=[]
            sketch=member(current,"GetSketch")
            if sketch is not None:
                inverse=member(member(sketch,"ModelToSketchTransform"),"Inverse")
                segment_call={"role":role,"api":"ISketch.GetSketchSegments"}
                self.diagnostics.setdefault("drawing_sketch_segment_calls",[]).append(segment_call)
                segments=_invoke_com_method(sketch,"GetSketchSegments",diagnostic=segment_call)
                require(segments is None or isinstance(segments,(list,tuple)),"native sketch segments not an array")
                segment_call.update(raw_return_is_null=segments is None,
                                    returned_count=0 if segments is None else len(segments))
                for segment in segments or []:
                    if member(segment,"ConstructionGeometry") is not True:
                        continue
                    require(member(segment,"GetType")==0,"unsupported construction annotation")
                    endpoints=[]
                    for name in ("GetStartPoint2","GetEndPoint2"):
                        p=member(segment,name)
                        endpoints.append(self._math_point([member(p,k) for k in ("X","Y","Z")],inverse))
                    rows.append({"sheet_endpoints_m":endpoints,"native_segment_id":list(member(segment,"GetID")),
                                 "source":"IView.GetSketch/GetSketchSegments construction LINE",
                                 "primitive_source_space":"SHEET_ANNOTATION"})
            centers[role]=rows
            current=next_native(current,"GetNextView")
        require(set(frames)==roles,"physical view set incomplete")
        self.drawing_gates["physical_views"]="PASS"
        return {"evidence_kind":"SIMULATED" if self.simulated else "NATIVE","native_views":frames,
                "hlr_views":hlr,"hlv_views":hlv,"centerlines":centers,"drawing_save_reopen":roundtrip}

    def cleanup_documents(self):
        # Classify before invoking frozen close: an absent title is never identity proof.
        diagnostic={"evidence_class":"BEST_EFFORT_DIAGNOSTIC_EVIDENCE",
            "cleanup_documents_reached":"YES","cleanup_documents_completed":"NOT_REACHED",
            "ExitApp_reached":"NOT_REACHED","ExitApp_returned":"NOT_REACHED",
            "COM_release_reached":"NOT_REACHED","COM_release_completed":"NOT_REACHED",
            "PID_exit_verification_reached":"NOT_REACHED","PID_residual":"NOT_RECORDED",
            "native_calls":[],"documents":[]}
        self.diagnostics["cleanup"]=diagnostic
        try:
            self._verify_boundary()
            self.release_sketch_cache(self._part)
            diagnostic["before"]=document_inventory(self._session)
            diagnostic["owned_documents"]=[row for row in diagnostic["before"]["documents"]
                                            if row["title"] in self._owned_titles]
            with observe_cleanup(diagnostic):
                # A referenced PART cannot close while its DRAWING remains open.
                # Order by the independently observed document type, never a name heuristic.
                document_types={row["title"]:row["document_type"] for row in diagnostic["before"]["documents"]}
                ordered_titles=sorted(self._owned_titles,key=lambda title:(document_types.get(title)!=3,title))
                for ordinal,title in enumerate(ordered_titles,1):
                    before=document_inventory(self._session)
                    active=before["active_document"]
                    observed=next((row for row in before["documents"] if row["title"]==title),{})
                    row={"cleanup_ordinal":ordinal,"title":title,
                         "path":observed.get("path","NOT_RECORDED"),
                         "document_type":observed.get("document_type","NOT_RECORDED"),
                         "stable_identity":observed.get("stable_identity","NOT_RECORDED"),
                         "before":before,"title_existed_before":inventory_title_state(before,title),
                         "was_active_before":active.get("title")==title if isinstance(active,dict) else
                             False if active is None else "NOT_RECORDED",
                         "close_attempted":False,"wrapper_close_return":"NOT_RECORDED",
                         "CLOSE_CLASSIFICATION":"CLOSE_FAILED","NATIVE_CLOSE_REQUIRED":"NOT_RECORDED",
                         "NATIVE_CLOSE_ATTEMPTED":"NO","POSTCONDITION_ABSENT":"NOT_RECORDED",
                         "SUCCESSFUL_CLOSE_EVIDENCE_REUSED":"NO","native_CloseDoc_invoked":"NO",
                         "native_CloseDoc_calls":[],"after":before,
                         "title_exists_after":inventory_title_state(before,title)}
                    diagnostic["documents"].append(row)
                    row["lifecycle_identity"]=cleanup_entry_identity(self,title,before)
                    identity=row["lifecycle_identity"]
                    if row["title_existed_before"] is False:
                        require(identity["ALREADY_CLOSED_IDENTITY_EVIDENCE"]=="SUFFICIENT",
                                "run-owned document closure failed")
                        # Recheck complete logical-document absence before removing this alias only.
                        after=document_inventory(self._session)
                        confirmed=cleanup_entry_identity(self,title,after)
                        row.update(after=after,title_exists_after=inventory_title_state(after,title),
                                   lifecycle_identity_after=confirmed)
                        require(confirmed["ALREADY_CLOSED_IDENTITY_EVIDENCE"]=="SUFFICIENT",
                                "run-owned document closure failed")
                        row.update(CLOSE_CLASSIFICATION="ALREADY_CLOSED",NATIVE_CLOSE_REQUIRED="NO",
                                   POSTCONDITION_ABSENT="YES",SUCCESSFUL_CLOSE_EVIDENCE_REUSED="YES",
                                   wrapper_close_return="NOT_REQUIRED",ALREADY_CLOSED_PROOF=confirmed["ALREADY_CLOSED_PROOF"])
                        lifecycle_event(self,"CLOSE_ALREADY_ABSENT_OBSERVED",identity["owned_doc_token"],
                                        title=title,identity=confirmed)
                    else:
                        require(identity["CURRENT_OPEN_IDENTITY"]=="SUFFICIENT",
                                "run-owned document closure failed")
                        row.update(close_attempted=True,NATIVE_CLOSE_REQUIRED="YES")
                        lifecycle_event(self,"CLOSE_ATTEMPT",identity["owned_doc_token"],
                                        title=title,before_inventory=before)
                        start=len(diagnostic["native_calls"])
                        stock_close=getattr(getattr(self._session,"close",None),"__func__",None) is R003OwnedNativeSession.close
                        try:
                            result=self._session.close(title=title)
                            row["wrapper_close_return"]=result if type(result) in (bool,int,str,float) or result is None else type(result).__name__
                        except Exception as exc:
                            row["exception"]={"type":type(exc).__name__,"message":str(exc)}
                            raise
                        finally:
                            after=document_inventory(self._session)
                            row.update(after=after,title_exists_after=inventory_title_state(after,title),
                                       active_document_after=after["active_document"])
                            calls=[call for call in diagnostic["native_calls"][start:] if call["name"]=="CloseDoc"]
                            row["native_CloseDoc_calls"]=calls
                            row["native_CloseDoc_invoked"]=("YES" if any(call["native_invocation_attempted"]=="YES" for call in calls)
                                else "NO" if stock_close and not diagnostic.get("observer_errors") else "NOT_RECORDED")
                            row["NATIVE_CLOSE_ATTEMPTED"]=row["native_CloseDoc_invoked"]
                            record_cleanup_close(self,row)
                        require(result is True and not diagnostic.get("observer_errors")
                                and row["lifecycle_identity_after"]["ALREADY_CLOSED_IDENTITY_EVIDENCE"]=="SUFFICIENT",
                                "run-owned document closure failed")
                        row.update(CLOSE_CLASSIFICATION="CLOSED_NOW",POSTCONDITION_ABSENT="YES")
                    self._owned_titles.remove(title)
                    lifecycle_event(self,"ALIAS_REMOVE",row["lifecycle_identity"].get("owned_doc_token"),title=title,registry_operation="remove")
                lifecycle_event(self,"FINAL_INVENTORY",inventory=document_inventory(self._session))
                self._part=None
                self._preparation_sketch=None
                self._native_sketch_handoff=None
                self._features.clear()
                result=self.quit_owned_instance()
                diagnostic["quit_result"]=dict(result) if isinstance(result,dict) else type(result).__name__
                if isinstance(result,dict) and diagnostic["PID_exit_verification_reached"]=="YES" and type(result.get("process_exited")) is bool:
                    diagnostic["PID_residual"]=int(not result["process_exited"])
                require(isinstance(result,dict) and result.get("exit_app_called") is True
                        and result.get("com_released") is True and result.get("process_exited") is True
                        and result.get("pid")==self._identity.pid,
                        "run-owned instance cleanup failed")
                diagnostic.update(cleanup_documents_completed="YES",CLEANUP_STATUS="PASS")
                return result
        except Exception as exc:
            diagnostic["CLEANUP_STATUS"]="FAIL"
            diagnostic["cleanup_documents_completed"]="NO"
            diagnostic["exception"]={"type":type(exc).__name__,"message":str(exc)}
            lifecycle_event(self,"FINAL_INVENTORY",inventory=document_inventory(self._session))
            raise
