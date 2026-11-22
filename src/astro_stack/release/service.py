"""Local release service: issue, export and offline verify entry points."""
import json
import os
import tempfile
from pathlib import Path

from ..archive.store import Store, replay_snapshot
from ..io import load, pairs
from .package import PackageError, sha256_hex
from .store import Library, ReleaseError
from .validation import validate_license, validate_request
from .verify import verify_document

PACKAGE_READ_LIMIT = 256_000_000


def issue(library_path, archive_path, request, license_value, fault_hook=None):
    """Issue one release; identical retries return the original receipt.

    ``request`` and ``license_value`` may be either parsed objects or paths to
    strict JSON files.  The request is parsed before the issuance ledger is
    consulted; a same-id identical request returns the stored receipt without
    opening the archive or license, while a same-id different request conflicts.
    """
    parsed_request = _as_object(request, validate_request)
    with Library(library_path, create=True) as library:
        existing = library.lookup(parsed_request)
        if existing is not None:
            return existing
        parsed_license = _as_object(license_value, validate_license)
        if parsed_license["dataset_id"] != parsed_request["dataset_id"]:
            raise ValueError("license dataset_id does not match the request")
        # capture one fixed revision, then release the archive lock for science
        with Store(archive_path) as store:
            snapshot = replay_snapshot(store.state, store.root,
                                       parsed_request["revision"])
            if snapshot.dataset_id != parsed_request["dataset_id"]:
                raise ValueError("request dataset_id does not match the archive")
            width, height = snapshot.width, snapshot.height
            for frame_id, frame_version, x, y in parsed_license["denied"]:
                if not (0 <= x < width and 0 <= y < height):
                    raise ValueError(
                        f"denied sample ({frame_id!r}, v{frame_version}, "
                        f"{x},{y}) is outside the detector")
            document = _build(snapshot, parsed_request,
                              parsed_license["denied"])
        return library.issue(parsed_request, document, fault_hook=fault_hook)


def _build(snapshot, request, denied):
    from .package import build_package
    return build_package(snapshot, request, denied)


def export(library_path, request_id, output_path):
    """Retrieve one issued package to a path that must not already exist."""
    if not isinstance(request_id, str) or not request_id.strip() \
            or len(request_id) > 200:
        raise ValueError("request_id requires a nonempty string <=200 chars")
    target = Path(output_path)
    if target.exists():
        raise FileExistsError(f"export target already exists: {output_path}")
    with Library(library_path) as library:
        data, receipt = library.package_bytes(request_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    # create-only hard link semantics, mirroring io.publish: os.link itself
    # fails if another process created the target in the meantime
    descriptor, temporary = tempfile.mkstemp(prefix=".export-",
                                             dir=target.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
        os.link(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        if target.exists():
            try:
                os.unlink(target)
            except OSError:
                pass
        raise
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    if sha256_hex(target.read_bytes()) != receipt["sha256"]:
        try:
            os.unlink(target)
        except FileNotFoundError:
            pass
        raise ReleaseError("exported bytes failed their receipt digest")
    return dict(receipt)


def verify(package_path, expected_sha256):
    """Offline verification: digest gate first, then full recomputation."""
    if not isinstance(expected_sha256, str) \
            or len(expected_sha256) != 64 \
            or any(ch not in "0123456789abcdef" for ch in expected_sha256):
        raise ValueError("expected SHA-256 must be 64 lowercase hex characters")
    path = Path(package_path)
    with path.open("rb") as source:
        data = source.read(PACKAGE_READ_LIMIT + 1)
    if len(data) > PACKAGE_READ_LIMIT:
        raise PackageError("package exceeds the offline read limit")
    actual = sha256_hex(data)
    if actual != expected_sha256:
        raise PackageError("package SHA-256 does not match the trusted receipt")
    try:
        document = json.loads(data, object_pairs_hook=pairs,
                              parse_constant=_reject_constant)
    except (json.JSONDecodeError, PackageError, ValueError) as exc:
        raise PackageError(f"package is not valid strict JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise PackageError("package root must be an object")
    result = verify_document(document)
    result["sha256"] = actual
    result["digest_match"] = True
    return result


def _reject_constant(value):
    raise PackageError(f"non-finite JSON value: {value}")


def _as_object(value, validator):
    if isinstance(value, (str, os.PathLike)):
        value = load(value)
    return validator(value)
