"""R004 worker-local runtime boundary; no COM server or typelib is initialized.

Reuse the frozen read audit. Runtime files are individual verified members,
never distribution directories. CPython audit events additionally cover cold
import reads and cache writes; native/SolidWorks internal I/O is out of scope.
"""
from contextlib import contextmanager
from hashlib import sha256
from importlib import metadata, import_module
import base64
import importlib.machinery
import os
from pathlib import Path
import re
import struct
import sys
import sysconfig
import threading
import time

from experiments.three_view_reconstruction.run_r003_v2_real_validation import GenerationReadAudit, R003PreflightError


def check(value, reason):
    if not value:
        raise R003PreflightError(reason)


def file_hash(path):
    return sha256(Path(path).read_bytes()).hexdigest()


def runtime_provenance(config):
    """Resolve RECORD-backed members without importing any COM dependency."""
    pins = config["runtime_pins"]
    check(list(sys.version_info[:3]) == pins["python_version"] and struct.calcsize("P") * 8 == pins["python_bits"],
          "incompatible pinned Python runtime")
    members = {}
    for name, pin in sorted(pins["distributions"].items()):
        dist = metadata.distribution(name)
        check(dist.version == pin["version"], "runtime distribution version drift")
        record = next(f for f in dist.files if str(f).endswith(".dist-info/RECORD"))
        check(file_hash(dist.locate_file(record)) == pin["record_sha256"], "runtime RECORD pin drift")
        for entry in dist.files:
            # Distribution scripts/tests/help are not part of the import boundary.
            rel = str(entry).replace("\\", "/")
            if not entry.hash or entry.hash.mode != "sha256" or ".." in Path(rel).parts:
                continue
            if not any(rel.startswith(prefix) for prefix in pin["import_prefixes"]):
                continue
            path = Path(dist.locate_file(entry)).resolve(strict=True)
            observed = file_hash(path)
            expected = base64.urlsafe_b64decode(entry.hash.value + "=" * (-len(entry.hash.value) % 4)).hex()
            check(observed == expected, "runtime RECORD member drift")
            members[str(path)] = {"role": "DEPENDENCY_MODULE", "sha256": observed,
                "origin": "PREEXISTING_APPROVED", "identity": f"{name}=={dist.version}:RECORD:{pin['record_sha256']}"}
        for rel, expected in pin.get("source_sha256", {}).items():
            path = Path(dist.locate_file(rel)).resolve(strict=True)
            check(file_hash(path) == expected and str(path) in members, "runtime source pin drift")
        path = Path(dist.locate_file(record)).resolve(strict=True)
        members[str(path)] = {"role": "DEPENDENCY_METADATA", "sha256": pin["record_sha256"],
            "origin": "PREEXISTING_APPROVED", "identity": f"{name}=={dist.version}:RECORD"}
    # Inventory stdlib code, excluding any third-party install directory. Hashes
    # describe this pinned interpreter environment; they are rechecked on access.
    stdlib = Path(sysconfig.get_path("stdlib")).resolve(strict=True)
    for path in sorted(stdlib.rglob("*.py")):
        rel = path.relative_to(stdlib)
        if {p.casefold() for p in rel.parts} & {"site-packages", "test", "tests", "ensurepip", "idlelib", "tkinter"}:
            continue
        check(not path.is_symlink() and path.resolve().is_relative_to(stdlib), "stdlib path escape")
        members[str(path.resolve())] = {"role": "PYTHON_STDLIB", "sha256": file_hash(path),
            "origin": "PREEXISTING_APPROVED", "identity": f"CPython:{pins['python_version']}:{pins['python_bits']}"}
    # Extension modules have OS loader reads beyond this Python audit scope.
    return {"python_version": list(sys.version_info[:3]), "python_bits": struct.calcsize("P") * 8,
            "distribution_pins": pins["distributions"], "members": dict(sorted(members.items()))}


class RuntimeReadAudit(GenerationReadAudit):
    """Exact-member runtime extension, with a separate controlled cache policy."""

    def __init__(self, *, runtime, cache_root, write_roots=(), generated_wrapper_ids=(), **kwargs):
        super().__init__(**kwargs)
        self.runtime = dict(runtime["members"])
        self.cache_root = Path(cache_root).absolute()
        self.cache_members = {}
        self.write_roots = tuple(Path(p).resolve(strict=True) for p in write_roots)
        self.generated_wrapper_ids = frozenset(generated_wrapper_ids)
        self._internal = threading.local()
        self._hook_enabled = False
        self._registered_hook = False

    def canonical(self, file):
        raw = Path(os.fsdecode(file)).absolute()
        check(".." not in raw.parts, "runtime path traversal")
        for parent in (raw, *raw.parents):
            if parent.exists():
                check(not parent.is_symlink() and not parent.is_junction(), "runtime reparse path")
        resolved = raw.resolve()
        check(raw == resolved, "runtime path escape")
        return resolved

    def truth_path(self, path):
        parts = {p.casefold() for p in path.parts}
        return (any(path.is_relative_to(root) for root in self.forbidden) or
                bool(parts & {"truth", "expected", "evaluator", "reference_answer", "datasets", "benchmark",
                              "benchmarks", "expected_geometry", "expected_ownership", "golden"}) or
                path.name.casefold().startswith(("expected", "golden")))

    def event(self, path, *, allowed, role=None, member=None, operation="read", reason=""):
        category = "TRUTH_READ" if self.truth_path(path) else (
            "APPROVED_RUNTIME_SUPPORT_READ" if allowed and operation == "read" else
            "APPROVED_RUNTIME_SUPPORT_WRITE" if allowed else "FORBIDDEN_GENERATION_READ" if operation == "read" else "FORBIDDEN_RUNTIME_WRITE")
        self.events.append({"path": str(path), "normalized_path": str(path), "source": str(path),
            "category": category, "allowed": allowed, "operation": operation, "reason": reason,
            "runtime_support_role": role, "provenance_identity": (member or {}).get("identity"),
            "sha256": (member or {}).get("sha256"), "origin": (member or {}).get("origin"),
            "timestamp": time.time(), "phase": self.phase, "pid": os.getpid(), "thread_id": threading.get_ident()})

    def record(self, file, mode):
        if getattr(self._internal, "busy", False):
            return
        if not ("r" in mode or "+" in mode):
            return
        if not isinstance(file, (str, bytes, os.PathLike)):
            return super().record(file, mode)
        try:
            path = self.canonical(file)
            check(not self.truth_path(path), "truth/reference/evaluator path")
            member = self.runtime.get(str(path)) or self.cache_members.get(str(path))
            if member:
                self._internal.busy = True
                try:
                    check(file_hash(path) == member["sha256"], "pinned runtime member changed")
                finally:
                    self._internal.busy = False
                self.event(path, allowed=True, role=member["role"], member=member, reason="verified exact runtime member")
                self.reads.append(str(path))
                return
            check(not path.is_relative_to(self.cache_root), "unknown cache member role")
            # Binary runtime indices need exact role/provenance even when a
            # general application/output directory is otherwise readable.
            check(path.suffix.casefold() not in {".dat", ".pkl", ".pickle"}, "unregistered runtime binary role")
        except (R003PreflightError, OSError) as exc:
            path = Path(os.fsdecode(file)).absolute().resolve()
            self.event(path, allowed=False, reason=str(exc))
            self.denied.append(str(path))
            # Count actual truth reads only: this attempt was rejected pre-open.
            raise R003PreflightError(str(exc)) from exc
        try:
            return super().record(file, mode)
        finally:
            if self.events:
                self.events[-1].update(path=str(file),normalized_path=str(path),pid=os.getpid(),
                    thread_id=threading.get_ident(),runtime_support_role=None,provenance_identity=None,sha256=None)

    def _hook(self, name, args):
        if not self._hook_enabled or getattr(self._internal, "busy", False):
            return
        if name == "open":
            file, mode, flags = args
            if not isinstance(file, (str, bytes, os.PathLike)):
                self.record(file, "r" if mode is None else mode)
                return
            writing = bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
            path = self.canonical(file)
            if writing and (path.is_relative_to(self.cache_root) or self.truth_path(path) or
                            not any(path.is_relative_to(root) for root in self.write_roots)):
                self.event(path, allowed=False, operation="write", reason="write outside approved output/member transaction")
                self.denied.append(str(path))
                raise R003PreflightError("unapproved cache/runtime write")
            if not writing or flags & os.O_RDWR:
                self.record(file, "r")
        elif name in {"os.rename", "os.remove", "os.rmdir", "os.mkdir", "os.link", "os.symlink", "os.truncate"}:
            paths = args[:2] if name in {"os.rename", "os.link", "os.symlink"} else args[:1]
            if name in {"os.rename", "os.link"}:
                check(all(value in (-1, None) for value in args[2:]), "directory-FD mutations unsupported")
            for file in paths:
                check(not isinstance(file,int), "unattributable FD mutation")
                if isinstance(file, (str, bytes, os.PathLike)):
                    path = self.canonical(file)
                    check(not path.is_relative_to(self.cache_root) and not self.truth_path(path) and
                          any(path.is_relative_to(root) for root in self.write_roots), "unapproved cache/runtime mutation")

    def write_member(self, name, data, *, role, identity, replace=False):
        """Explicit generation transaction. Unknown external generator writes fail.

        Typelib-generated code requires an explicit reviewed typelib identity;
        basename alone cannot authorize an index or executable wrapper.
        """
        check(role in {"COM_DICT_INDEX", "COM_CACHE_PACKAGE", "COM_GENERATED_WRAPPER"}, "unknown cache role")
        check(identity and isinstance(data, bytes), "cache provenance required")
        if role == "COM_DICT_INDEX":
            check(name == "dicts.dat" and identity == "pywin32:312:index-schema:1", "index role identity invalid")
        elif role == "COM_CACHE_PACKAGE":
            check(name == "__init__.py" and identity == "R004:controlled-package:1", "package role identity invalid")
        else:
            check(re.fullmatch(r"[0-9A-Fa-f-]{36}x[0-9]+x[0-9]+x[0-9]+\.py", name) and
                  identity == "TYPELIB:" + name[:-3] and identity in self.generated_wrapper_ids,
                  "wrapper role identity not in reviewed generation plan")
        path = self.canonical(self.cache_root / name)
        check(path.parent == self.cache_root and not self.truth_path(path), "cache containment/truth boundary")
        member = self.cache_members.get(str(path))
        check((not path.exists() and not replace) or (replace and member is not None and file_hash(path) == member["sha256"]),
              "unknown or changed pre-existing cache member")
        self._internal.busy = True
        try:
            # Validate replacement before an atomic publication; no arbitrary rename.
            temporary = path.with_suffix(path.suffix + ".r004-new")
            with temporary.open("xb") as output:
                output.write(data)
            if replace:
                os.replace(temporary, path)
            else:
                check(not path.exists(), "cache member appeared during publication")
                os.link(temporary, path)
                temporary.unlink()
        finally:
            self._internal.busy = False
        member = {"role": role, "identity": identity, "origin": "GENERATED_THIS_ATTEMPT", "sha256": sha256(data).hexdigest()}
        self.cache_members[str(path)] = member
        self.event(path, allowed=True, role=role, member=member, operation="write", reason="explicit controlled member transaction")

    def bootstrap(self):
        check(not self.cache_root.exists(), "cache root must be fresh; pre-existing members rejected")
        check(not self.truth_path(self.cache_root), "cache root overlaps truth")
        self.canonical(self.cache_root)
        self.cache_root.mkdir(parents=True, exist_ok=False)
        # Two version-1 pickle streams generated by this worker, not shared cache.
        self.write_member("dicts.dat", bytes.fromhex("80054b012e80057d942e"), role="COM_DICT_INDEX", identity="pywin32:312:index-schema:1")
        self.write_member("__init__.py", b"# R004 isolated generated-wrapper package\n", role="COM_CACHE_PACKAGE", identity="R004:controlled-package:1")

    def validate_cache_members(self):
        observed = {str(self.canonical(p)) for p in self.cache_root.iterdir()}
        check(observed == set(self.cache_members), "unexpected cache members")
        for path in observed:
            self.record(path, "r")

    def configure_pywin32(self):
        check(not any(k == "win32com" or k.startswith("win32com.") for k in sys.modules), "cold worker required")
        check(self.cache_members and self.is_active, "cache and active audit required before import")
        self.validate_cache_members()
        module = import_module("win32com")
        module.__gen_path__ = str(self.cache_root)
        module.gen_py.__path__ = [str(self.cache_root)]
        import_module("win32com.client")
        check(module.__gen_path__ == str(self.cache_root) and module.gen_py.__path__ == [str(self.cache_root)], "cache configuration drift")

    @contextmanager
    def active(self):
        if not self._registered_hook:
            sys.addaudithook(self._hook)
            self._registered_hook = True
        old = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        # Ignore unpinned pre-existing bytecode by compiling verified source.
        # The source read still traverses both audits; imports are not exempt.
        class VerifiedSourceLoader(importlib.machinery.SourceFileLoader):
            def get_code(loader, fullname):
                data = loader.get_data(loader.path)
                return loader.source_to_code(data, loader.path)
        class SourceFinder:
            @staticmethod
            def find_spec(fullname, path=None, target=None):
                spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
                if spec and isinstance(spec.loader, importlib.machinery.SourceFileLoader):
                    spec.loader = VerifiedSourceLoader(fullname, spec.origin)
                return spec
        finder = SourceFinder()
        sys.meta_path.insert(0, finder)
        try:
            with super().active():
                self._hook_enabled = True
                try:
                    yield self
                finally:
                    self._hook_enabled = False
        finally:
            sys.meta_path.remove(finder)
            sys.dont_write_bytecode = old

    def evidence(self):
        return {**super().evidence(), "runtime_members": self.runtime, "cache_members": self.cache_members,
                "controlled_cache_root": str(self.cache_root), "runtime_write_policy": "EXPLICIT_MEMBER_TRANSACTIONS_ONLY",
                "approved_nontruth_roots": [str(p) for p in self.allowed], "forbidden_roots": [str(p) for p in self.forbidden],
                "approved_write_roots": [str(p) for p in self.write_roots], "reviewed_wrapper_identities": sorted(self.generated_wrapper_ids),
                "additional_audit_scope": "CPython audit open/import and cache mutation events; native I/O excluded"}
