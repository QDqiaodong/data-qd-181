"""Crash-safe local release library.

Layout (all writes are same-directory temporary files followed by
``os.replace``):

* ``state.json``  - format marker and the issued request ledger
* ``packages/``   - one immutable canonical package per issued request, named
                    ``<sha256-of-request-id>.json``
* ``.lock``       - cross-process exclusive lock

The replacement of ``state.json`` is the single commit boundary: the package
already exists under its final name before the ledger switches, so reopening
can only ever observe "not issued" or one complete, retrievable issuance.
"""
import fcntl
import json
import os
import tempfile
from pathlib import Path

from ..io import pairs, reject_constant
from .package import canonical_bytes, sha256_hex

FORMAT = "astro-release-library/v1"
STATE_NAME = "state.json"
LOCK_NAME = ".lock"
PACKAGES_DIR = "packages"
STATE_TMP_PREFIX = ".state-"
PACKAGE_TMP_PREFIX = ".package-"


class ReleaseError(ValueError):
    """Raised when the release library or an issuance is inconsistent."""


def _read_json(path):
    with Path(path).open("rb") as source:
        return json.loads(source.read(), object_pairs_hook=pairs,
                          parse_constant=reject_constant)


def _atomic_write(path, data):
    target = Path(path)
    descriptor, temporary = tempfile.mkstemp(prefix=PACKAGE_TMP_PREFIX,
                                             dir=target.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _atomic_json(path, value):
    _atomic_write(path, (json.dumps(value, ensure_ascii=False, allow_nan=False,
                                    sort_keys=True) + "\n").encode("utf-8"))


def _request_text(request):
    return json.dumps(request, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def package_name(request_id):
    return sha256_hex(request_id.encode("utf-8")) + ".json"


def initialize(path):
    """Create a brand-new release library; never overwrite an existing path."""
    root = Path(path)
    try:
        root.mkdir(parents=True)
    except FileExistsError:
        raise FileExistsError(f"release library already exists: {path}")
    _populate(root)


def _populate(root):
    (root / PACKAGES_DIR).mkdir()
    try:
        open(root / LOCK_NAME, "a+b").close()
        _atomic_json(root / STATE_NAME,
                     {"format": FORMAT, "issued": []})
    except BaseException:
        _discard(root)
        raise


def _discard(root):
    for base, directories, files in os.walk(root, topdown=False):
        for name in files:
            try:
                os.unlink(os.path.join(base, name))
            except FileNotFoundError:
                pass
        for name in directories:
            try:
                os.rmdir(os.path.join(base, name))
            except OSError:
                pass
    try:
        root.rmdir()
    except OSError:
        pass


def _open_lock(root):
    handle = open(root / LOCK_NAME, "a+b")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    return handle


class Library:
    """An opened release library; opening reconciles and replays the ledger."""

    def __init__(self, path, create=False):
        self.root = Path(path)
        if not self.root.exists():
            if not create:
                raise ReleaseError(f"not a release library directory: {path}")
            initialize(path)
        elif not self.root.is_dir():
            raise ReleaseError(f"not a release library directory: {path}")
        elif create and not (self.root / LOCK_NAME).exists():
            # create-only semantics: only an empty directory may become a library
            if any(self.root.iterdir()):
                raise ReleaseError(
                    f"directory is not an empty release library: {path}")
            _populate(self.root)
        if not (self.root / LOCK_NAME).exists():
            raise ReleaseError("release library is missing its lock file")
        self.lock = _open_lock(self.root)
        try:
            self._reconcile()
            self.state = self._load_state()
        except BaseException:
            self.lock.close()
            raise

    def close(self):
        if not self.lock.closed:
            self.lock.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()

    def _load_state(self):
        try:
            state = _read_json(self.root / STATE_NAME)
        except FileNotFoundError as exc:
            raise ReleaseError("release library state is missing") from exc
        except json.JSONDecodeError as exc:
            raise ReleaseError(f"release library state is corrupt: {exc}") from exc
        self._check_state(state)
        return state

    def _check_state(self, state):
        if not isinstance(state, dict) or set(state) != {"format", "issued"} \
                or state.get("format") != FORMAT \
                or not isinstance(state["issued"], list):
            raise ReleaseError("release library state has an unknown layout")
        seen = set()
        for entry in state["issued"]:
            if not isinstance(entry, dict) or set(entry) != \
                    {"request", "receipt", "package"}:
                raise ReleaseError("release ledger entry has an unknown layout")
            request = entry["request"]
            if not isinstance(request, dict):
                raise ReleaseError("release ledger request is corrupt")
            request_id = request.get("request_id")
            if not isinstance(request_id, str) or request_id in seen:
                raise ReleaseError("release ledger has a bad or duplicate id")
            seen.add(request_id)
            receipt = entry["receipt"]
            if not isinstance(receipt, dict) or set(receipt) != \
                    {"request_id", "dataset_id", "revision", "sha256"} \
                    or receipt["request_id"] != request_id:
                raise ReleaseError("release ledger receipt is corrupt")
            if not isinstance(entry["package"], str) \
                    or not entry["package"].endswith(".json"):
                raise ReleaseError("release ledger package name is corrupt")

    def _reconcile(self):
        """Crash recovery: commit boundary is the state replacement.

        A staged package left without a ledger entry never became visible, so
        stale temporary files and unreferenced package files are removed.  A
        ledger entry whose package is missing or whose digest is stale is
        corruption and is reported, never rebuilt.
        """
        state_path = self.root / STATE_NAME
        if not state_path.exists():
            for stray in self.root.rglob(STATE_TMP_PREFIX + "*"):
                _unlink_quiet(stray)
            for stray in (self.root / PACKAGES_DIR).glob(PACKAGE_TMP_PREFIX + "*"):
                _unlink_quiet(stray)
            return
        try:
            state = _read_json(state_path)
        except json.JSONDecodeError as exc:
            raise ReleaseError(f"release library state is corrupt: {exc}") from exc
        self._check_state(state)
        referenced = set()
        for entry in state["issued"]:
            name = entry["package"]
            referenced.add(name)
            package_path = self.root / PACKAGES_DIR / name
            if not package_path.is_file():
                raise ReleaseError(
                    f"issued package is missing for request "
                    f"{entry['request']['request_id']!r}")
            data = package_path.read_bytes()
            if sha256_hex(data) != entry["receipt"]["sha256"]:
                raise ReleaseError(
                    f"issued package fails its receipt digest for request "
                    f"{entry['request']['request_id']!r}")
        for stray in self.root.rglob(STATE_TMP_PREFIX + "*"):
            _unlink_quiet(stray)
        package_dir = self.root / PACKAGES_DIR
        for stray in package_dir.glob(PACKAGE_TMP_PREFIX + "*"):
            _unlink_quiet(stray)
        for path in package_dir.glob("*.json"):
            if path.name not in referenced:
                _unlink_quiet(path)

    def lookup(self, request):
        """Return the existing receipt for a request id, or None.

        Raises ReleaseError when the same id is bound to a different canonical
        request.  Licenses and the source archive are not consulted here.
        """
        target = _request_text(request)
        for entry in self.state["issued"]:
            if entry["request"]["request_id"] == request["request_id"]:
                if _request_text(entry["request"]) != target:
                    raise ReleaseError(
                        f"request_id {request['request_id']!r} is already bound "
                        "to a different request")
                return dict(entry["receipt"])
        return None

    def issue(self, request, document, fault_hook=None):
        """Commit one package and its ledger entry atomically; return receipt."""
        existing = self.lookup(request)
        if existing is not None:
            return existing
        data = canonical_bytes(document)
        digest = sha256_hex(data)
        name = package_name(request["request_id"])
        package_path = self.root / PACKAGES_DIR / name
        if package_path.exists():
            raise ReleaseError(
                f"package name collision for request_id {request['request_id']!r}")
        descriptor, temporary = tempfile.mkstemp(prefix=PACKAGE_TMP_PREFIX,
                                                 dir=package_path.parent)
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
        if fault_hook is not None:
            fault_hook("after_package_staged", self, request["request_id"])
        os.replace(temporary, package_path)
        receipt = {"request_id": request["request_id"],
                   "dataset_id": request["dataset_id"],
                   "revision": request["revision"], "sha256": digest}
        entry = {"request": request, "receipt": receipt, "package": name}
        next_state = {"format": FORMAT,
                      "issued": self.state["issued"] + [entry]}
        descriptor, state_tmp = tempfile.mkstemp(prefix=STATE_TMP_PREFIX,
                                                 dir=self.root)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write((json.dumps(next_state, ensure_ascii=False,
                                         allow_nan=False, sort_keys=True)
                              + "\n").encode("utf-8"))
            if fault_hook is not None:
                fault_hook("before_state_replace", self, request["request_id"])
            os.replace(state_tmp, self.root / STATE_NAME)
        finally:
            _unlink_quiet(state_tmp)
        if fault_hook is not None:
            fault_hook("after_state_replace", self, request["request_id"])
        self.state = next_state
        return dict(receipt)

    def package_bytes(self, request_id):
        for entry in self.state["issued"]:
            if entry["request"]["request_id"] == request_id:
                path = self.root / PACKAGES_DIR / entry["package"]
                data = path.read_bytes()
                if sha256_hex(data) != entry["receipt"]["sha256"]:
                    raise ReleaseError(
                        f"issued package fails its receipt digest for request "
                        f"{request_id!r}")
                return data, dict(entry["receipt"])
        raise ReleaseError(f"unknown request_id: {request_id!r}")


def _unlink_quiet(path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
