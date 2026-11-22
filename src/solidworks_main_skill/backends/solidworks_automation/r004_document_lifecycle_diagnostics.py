"""Run-local document lineage evidence for bounded R004 cleanup decisions."""
from pathlib import Path

from .r003_native_collectors import member
from .r004_cleanup_diagnostics import document_inventory, inventory_title_state, observe_cleanup


def lifecycle_event(adapter, event, token=None, *, kind=None, document=None, title=None, **details):
    """Keep only scalars. Diagnostic getter failures cannot change CAD behavior."""
    try:
        log = adapter.diagnostics.setdefault("document_lifecycle", {
            "identity_scope": "R004_LOCAL_SINGLE_RUN_ONLY", "documents": {}, "events": [], "errors": []})
        if event == "REGISTER":
            token = f"{adapter._run_id}:owned-doc:{len(log['documents']) + 1}"
            identity = adapter._identity
            log["documents"][token] = {"owned_doc_token": token, "kind": kind,
                "run_id": adapter._run_id, "session_pid": identity.pid,
                "session_creation_time": identity.create_time, "initial_title": title,
                "title": "NOT_RECORDED", "path": "NOT_RECORDED", "doc_type": "NOT_RECORDED",
                "last_open_sequence": len(log["events"]) + 1,
                "known_aliases": [], "registry_aliases": [], "successful_close_events": [],
                "identity_errors": []}
        record = log["documents"].get(token)
        observed = {}
        if document is not None:
            for key, name in (("title", "GetTitle"), ("path", "GetPathName"), ("doc_type", "GetType")):
                try:
                    value = member(document, name)
                    if key == "path":
                        if value == "": value = "UNSAVED"
                        elif isinstance(value, str) and Path(value).is_absolute(): value = str(Path(value).resolve())
                        else: raise ValueError("absolute document path unavailable")
                    if key == "doc_type" and type(value) is not int:
                        raise ValueError("native document type unavailable")
                    if key == "title" and not isinstance(value, str):
                        raise ValueError("native document title unavailable")
                    observed[key] = value
                except Exception as exc:
                    log["errors"].append({"event": event, "member": name, "exception": type(exc).__name__, "message": str(exc)})
        if record is not None:
            previous_title = record["title"]
            expected_type = {"PART": 1, "DRAWING": 3}.get(record["kind"])
            if "doc_type" in observed and observed["doc_type"] != expected_type:
                record["identity_errors"].append("DOCUMENT_TYPE_MISMATCH")
            if event == "REOPEN" and observed.get("path") != record["path"]:
                record["identity_errors"].append("REOPEN_PATH_MISMATCH")
            if event in {"OPEN", "REOPEN"}: record["last_open_sequence"] = len(log["events"]) + 1
            for key, value in observed.items():
                if value != "NOT_RECORDED": record[key] = value
            if title is not None and event in {"REGISTER", "REOPEN", "TITLE_BEFORE_SAVE", "SAVE_RENAME"}:
                record["title"] = title
            if event == "REGISTER" and record["initial_title"] is None:
                record["initial_title"] = record["title"]
            for alias in (observed.get("title"), title):
                if isinstance(alias, str) and alias != "NOT_RECORDED" and alias not in record["known_aliases"]:
                    record["known_aliases"].append(alias)
            if event == "ALIAS_ADD" and title in adapter._owned_titles and title not in record["registry_aliases"]:
                record["registry_aliases"].append(title)
            if event == "ALIAS_REMOVE" and title not in adapter._owned_titles and title in record["registry_aliases"]:
                record["registry_aliases"].remove(title)
            row = {key: record[key] for key in ("owned_doc_token", "kind", "title", "path", "doc_type")}
            row.update(previous_title=previous_title, known_aliases=list(record["known_aliases"]),
                registry_aliases=list(record["registry_aliases"]), native_current_title=record["title"])
            if title is not None: row["title"] = title
        else:
            row = {"owned_doc_token": "NOT_RECORDED", "kind": kind or "NOT_RECORDED",
                   "title": title or "NOT_RECORDED", "path": "NOT_RECORDED", "doc_type": "NOT_RECORDED"}
        row.update(event=event, sequence_number=len(log["events"]) + 1,
                   actual_registry_titles=sorted(adapter._owned_titles), **details)
        log["events"].append(row)
        return token
    except Exception as exc:
        # No diagnostic state is permitted to replace a production exception.
        if isinstance(getattr(adapter, "diagnostics", None), dict):
            adapter.diagnostics.setdefault("lifecycle_observer_errors", []).append(str(exc))
        return None


def cleanup_entry_identity(adapter, title, inventory):
    """Prove run-local identity and the latest epoch before cleanup can act."""
    result = {"registry_title": title, "owned_doc_token": "NOT_RECORDED", "known_aliases": [],
        "known_path": "NOT_RECORDED", "known_doc_type": "NOT_RECORDED",
        "known_successful_close_events": [], "identity_resolution": "UNKNOWN",
        "EXACT_OWNED_IDENTITY": "INSUFFICIENT", "CURRENT_OPEN_IDENTITY": "INSUFFICIENT",
        "ALREADY_CLOSED_IDENTITY_EVIDENCE": "INSUFFICIENT"}
    try:
        log = adapter.diagnostics.get("document_lifecycle", {})
        candidates = [row for row in log.get("documents", {}).values() if title in row["known_aliases"]]
        if len(candidates) != 1:
            result["identity_resolution"] = "AMBIGUOUS" if candidates else "UNKNOWN"
            return result
        record = candidates[0]
        token = record["owned_doc_token"]
        result.update(owned_doc_token=token, known_aliases=list(record["known_aliases"]),
            known_path=record["path"], known_doc_type=record["doc_type"],
            known_successful_close_events=list(record["successful_close_events"]), identity_resolution="LINKED")
        others = [row for row in log["documents"].values() if row["owned_doc_token"] != token]
        saved = record["path"] not in {"UNSAVED", "NOT_RECORDED"}
        if any(set(row["known_aliases"]) & set(record["known_aliases"])
               or saved and Path(row["path"]) == Path(record["path"]) for row in others):
            result["identity_resolution"] = "AMBIGUOUS"
            return result
        events = log["events"]
        if [e["sequence_number"] for e in events] != list(range(1, len(events) + 1)):
            return result
        lineage = [e for e in events if e["owned_doc_token"] == token]
        opens = [e for e in lineage if e["event"] in {"REGISTER", "OPEN", "REOPEN"}]
        latest = opens[-1]["sequence_number"] if opens else None
        if (not opens or opens[0]["event"] != "REGISTER"
                or latest != record["last_open_sequence"]
                or title not in record["registry_aliases"]
                or not any(e["event"] == "ALIAS_ADD" and e["title"] == title for e in lineage)
                or record["run_id"] != adapter._run_id or record["session_pid"] != adapter._identity.pid
                or record["session_creation_time"] != adapter._identity.create_time
                or record["identity_errors"] or log.get("errors")
                or adapter.diagnostics.get("lifecycle_observer_errors")
                or record["doc_type"] != {"PART": 1, "DRAWING": 3}.get(record["kind"])
                or record["path"] == "NOT_RECORDED"
                or inventory.get("inventory_complete") is not True or inventory.get("errors")):
            return result
        result["EXACT_OWNED_IDENTITY"] = "SUFFICIENT"
        live = [row for row in inventory["documents"] if row["title"] in record["known_aliases"]
                or saved and Path(row["path"]) == Path(record["path"])]
        # A stored integer alone is not successful native-close evidence.
        successes = []
        for event in lineage:
            seq = event["sequence_number"]
            if (event["event"] != "CLOSE_SUCCESS" or event.get("native_close_proven") is not True
                    or seq not in record["successful_close_events"] or seq < 2):
                continue
            observation = events[seq - 2]
            if (observation["event"] == "CLOSE_RESULT" and observation["owned_doc_token"] == token
                    and observation.get("wrapper_result") is True and not observation.get("exception")
                    and observation.get("before_inventory", {}).get("inventory_complete") is True
                    and observation.get("after_inventory", {}).get("inventory_complete") is True
                    and any(call.get("name") == "CloseDoc" and call.get("native_invocation_attempted") == "YES"
                            and call.get("returned") == "YES" and not call.get("exception")
                            for call in observation.get("native_calls", []))):
                successes.append(seq)
        current_close = max((seq for seq in successes if seq > latest), default=None)
        if (len(live) == 1 and live[0]["title"] == title == record["title"]
                and live[0]["document_type"] == record["doc_type"]
                and (not saved and live[0]["path"] == "" or saved and Path(live[0]["path"]) == Path(record["path"]))
                and current_close is None):
            result["CURRENT_OPEN_IDENTITY"] = "SUFFICIENT"
        proof = {"token": token, "alias": title, "latest_open_or_reopen_sequence": latest,
                 "successful_close_sequence": current_close, "inventory_complete": True,
                 "alias_absent": not any(row["title"] == title for row in inventory["documents"]),
                 "current_token_open_aliases": [row["title"] for row in live]}
        result["ALREADY_CLOSED_PROOF"] = proof
        if current_close is not None and not live:
            result["ALREADY_CLOSED_IDENTITY_EVIDENCE"] = "SUFFICIENT"
        return result
    except Exception:
        result.update(EXACT_OWNED_IDENTITY="INSUFFICIENT", CURRENT_OPEN_IDENTITY="INSUFFICIENT",
                      ALREADY_CLOSED_IDENTITY_EVIDENCE="INSUFFICIENT")
        return result


def close_observation(adapter, token, title, before, after, result, native_calls, exception=None):
    """Record closure evidence without changing its return value or classification."""
    lifecycle_event(adapter, "CLOSE_RESULT", token, title=title, wrapper_result=result,
        before_inventory=before, after_inventory=after, native_calls=native_calls, exception=exception)
    log = adapter.diagnostics.get("document_lifecycle", {})
    record = log.get("documents", {}).get(token)
    native_ok = any(row.get("name") == "CloseDoc" and row.get("native_invocation_attempted") == "YES"
                    and row.get("returned") == "YES" and not row.get("exception") for row in native_calls)
    matching = [row for row in before.get("documents", []) if row["title"] == title]
    linked = (record is not None and len(matching) == 1 and not record["identity_errors"]
              and matching[0]["document_type"] == record["doc_type"]
              and (record["path"] == "UNSAVED" and matching[0]["path"] == ""
                   or record["path"] not in {"UNSAVED", "NOT_RECORDED"} and Path(matching[0]["path"]) == Path(record["path"])))
    if (result is True and exception is None and native_ok and linked
            and cleanup_entry_identity(adapter, title, before)["CURRENT_OPEN_IDENTITY"] == "SUFFICIENT"
            and inventory_title_state(before, title) is True and inventory_title_state(after, title) is False):
        lifecycle_event(adapter, "CLOSE_SUCCESS", token, title=title, native_close_proven=True)
        record["successful_close_events"].append(log["events"][-1]["sequence_number"])
    elif inventory_title_state(before, title) is False and inventory_title_state(after, title) is False:
        lifecycle_event(adapter, "CLOSE_ALREADY_ABSENT_OBSERVED", token, title=title,
            identity=cleanup_entry_identity(adapter, title, after))


def record_cleanup_close(adapter, row):
    try:
        identity = cleanup_entry_identity(adapter, row["title"], row["before"])
        row["lifecycle_identity"] = identity
        token = identity.get("owned_doc_token")
        close_observation(adapter, token, row["title"], row["before"], row["after"],
            row["wrapper_close_return"], row["native_CloseDoc_calls"], row.get("exception"))
        row["lifecycle_identity_after"] = cleanup_entry_identity(adapter, row["title"], row["after"])
    except Exception as exc:
        lifecycle_event(adapter, "OBSERVATION_UNAVAILABLE", title=row.get("title"), failure=str(exc))


def observe_close_call(adapter, token, title, action):
    """Invoke the existing close once; retain the original result and exception."""
    before = document_inventory(adapter._session)
    lifecycle_event(adapter, "CLOSE_ATTEMPT", token, title=title, before_inventory=before)
    observation = {"native_calls": []}
    result = "NOT_RECORDED"
    exception = None
    try:
        with observe_cleanup(observation):
            result = action()
        return result
    except Exception as exc:
        exception = {"type": type(exc).__name__, "message": str(exc)}
        raise
    finally:
        try:
            close_observation(adapter, token, title, before, document_inventory(adapter._session),
                              result, observation["native_calls"], exception)
        except Exception as exc:
            lifecycle_event(adapter, "OBSERVATION_UNAVAILABLE", token, title=title, failure=str(exc))
