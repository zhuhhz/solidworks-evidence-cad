"""Read-only lifecycle observations; never close, activate, quit or kill CAD."""
from contextlib import contextmanager
import linecache
import sys

from .r003_native_collectors import member
from .r003_native_adapter import R003SolidWorksSessionAdapter, _exact_pid_present


def document_inventory(session):
    """Best-effort scalars only; observation failure cannot alter cleanup."""
    result = {"documents": [], "active_document": "NOT_RECORDED", "errors": []}
    def read(obj, name):
        try:
            absent = object()
            value = getattr(obj, name, absent)
            if value is absent:
                raise AttributeError(name)
            # Optional ActiveDoc/GetNext can legitimately return None.
            return value() if callable(value) and not hasattr(value, "_oleobj_") else value
        except Exception as exc:
            result["errors"].append({"member": name, "exception": type(exc).__name__, "message": str(exc)})
            return None
    def record(doc):
        return {"title": read(doc, "GetTitle"), "path": read(doc, "GetPathName"),
                "document_type": read(doc, "GetType"), "stable_identity": "NOT_RECORDED",
                "creation_open_order": "NOT_RECORDED"}
    try:
        sw = getattr(session, "sw", None)
        if sw is None:
            result["errors"].append({"member": "sw", "message": "session unavailable"})
            return result
        active_errors = len(result["errors"])
        active = read(sw, "ActiveDoc")
        if active is not None:
            result["active_document"] = record(active)
        elif len(result["errors"]) == active_errors:
            result["active_document"] = None
        count = read(sw, "GetDocumentCount")
        if type(count) is not int or not 0 <= count <= 1000:
            result["errors"].append({"member": "GetDocumentCount", "message": "inventory count unavailable or unbounded"})
            return result
        doc = read(sw, "GetFirstDocument")
        while doc is not None and len(result["documents"]) <= count:
            result["documents"].append(record(doc))
            doc = read(doc, "GetNext")
        if len(result["documents"]) != count or doc is not None:
            result["errors"].append({"member": "inventory", "message": "native count/chain mismatch"})
        result["inventory_complete"] = not result["errors"]
    except Exception as exc:
        result["errors"].append({"member": "inventory", "exception": type(exc).__name__, "message": str(exc)})
    return result


def inventory_title_state(inventory, title):
    if inventory.get("inventory_complete") is not True:
        return "NOT_RECORDED"
    return any(doc["title"] == title for doc in inventory["documents"])


@contextmanager
def observe_cleanup(record):
    """Trace the unchanged frozen call paths without replacing their methods."""
    previous = sys.gettrace()
    prior_locals = {}
    native_rows = {}
    member_code = member.__code__
    quit_code = R003SolidWorksSessionAdapter.quit_owned_instance.__code__
    pid_code = _exact_pid_present.__code__
    def observer_error(exc):
        record.setdefault("observer_errors", []).append({"type": type(exc).__name__, "message": str(exc)})
        # An unavailable observer cannot establish that a later stage was absent.
        for key in ("ExitApp_reached", "ExitApp_returned", "COM_release_reached",
                    "COM_release_completed", "PID_exit_verification_reached"):
            if record.get(key) == "NOT_REACHED": record[key] = "NOT_RECORDED"
    def trace(frame, event, arg):
        prior = previous if event == "call" else prior_locals.get(id(frame))
        if prior is not None:
            prior_locals[id(frame)] = prior(frame, event, arg)
        try:
            if frame.f_code == member_code and frame.f_locals.get("name") in {"CloseDoc", "ExitApp"}:
                name = frame.f_locals["name"]
                if event == "call":
                    row = {"name": name, "native_invocation_attempted": "NOT_RECORDED", "returned": "NOT_RECORDED"}
                    record["native_calls"].append(row); native_rows[id(frame)] = row
                    if name == "ExitApp": record["ExitApp_reached"] = "YES"
                row = native_rows.get(id(frame))
                if row is not None and "value" in frame.f_locals:
                    value = frame.f_locals["value"]
                    row["native_invocation_attempted"] = "YES" if callable(value) and not hasattr(value, "_oleobj_") else "NO"
                if event == "exception" and row is not None:
                    row["exception"] = {"type": arg[0].__name__, "message": str(arg[1])}
                if event == "return" and row is not None:
                    row["returned"] = "NO" if "exception" in row else "YES"
                    row["return_value"] = arg if arg is None or type(arg) in (bool, str, int, float) else type(arg).__name__
                    if name == "ExitApp": record["ExitApp_returned"] = row["returned"]
                    native_rows.pop(id(frame), None)
            elif frame.f_code == quit_code and event == "line":
                statement = linecache.getline(frame.f_code.co_filename, frame.f_lineno).strip()
                if statement == "self._part = None": record["COM_release_reached"] = "YES"
                if statement.startswith("deadline ="):
                    record["COM_release_completed"] = "YES"
                if statement.startswith("present = _exact_pid_present") or statement.startswith("observed = self._probe"):
                    record["PID_exit_verification_reached"] = "YES"
            elif frame.f_code == pid_code and event == "return" and type(arg) is bool:
                record["PID_residual"] = int(arg)
        except Exception as exc:
            observer_error(exc)
        if event == "return": prior_locals.pop(id(frame), None)
        return trace
    installed = False
    try:
        try:
            sys.settrace(trace); installed = True
        except Exception as exc:
            observer_error(exc)
        yield
    finally:
        if installed: sys.settrace(previous)
