"""Append-only local archive storage with file locking and crash recovery."""
import fcntl
import json
import os
import tempfile
from pathlib import Path

from .validation import CAMPAIGN_FRAME_LIMIT, CLASSIC_FRAME_LIMIT, validate_batch

FORMAT = "astro-archive/v1"
STATE_NAME = "state.json"
INDEX_NAME = "index.json"
LOCK_NAME = ".lock"
STATE_TMP_PREFIX = ".state-"
ENVELOPE_TMP_PREFIX = ".batch-"
DATA_TMP_PREFIX = ".data-"


class ArchiveError(ValueError):
    """Raised when archive contents cannot be interpreted as a consistent history."""


def canonical_json(value):
    """Canonical batch text: sorted object keys, array order kept, no extra
    whitespace, unicode preserved, ordinary decimal integers."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def batch_content(batch_value):
    """Retry identity excludes expected_revision: its staleness must not break
    the idempotent retry of an otherwise identical batch."""
    return {key: value for key, value in batch_value.items()
            if key != "expected_revision"}


def _atomic_write(path, data):
    target = Path(path)
    descriptor, temporary = tempfile.mkstemp(prefix=DATA_TMP_PREFIX,
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


def _read_json(path):
    with Path(path).open("rb") as source:
        return json.loads(source.read())


def create(path, definition):
    """Create a brand-new archive; never overwrite an existing path."""
    root = Path(path)
    try:
        root.mkdir(parents=True)
    except FileExistsError:
        raise FileExistsError(f"archive already exists: {path}")
    for name in ("darks", "flats", "frames", "revisions"):
        (root / name).mkdir()
    lock_handle = _open_lock(root)
    try:
        state = {"format": FORMAT, "dataset_id": definition["dataset_id"],
                 "width": definition["width"], "height": definition["height"],
                 "apertures": definition["apertures"],
                 "products": definition["products"], "revision": 0}
        _atomic_json(root / STATE_NAME, state)
        _atomic_json(root / INDEX_NAME, {"format": FORMAT, "batches": []})
    except BaseException:
        # failed initialization must never leave something that looks like an archive
        _discard(root)
        raise
    return state


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


class Store:
    """An opened archive; opening replays the whole append-only history."""

    def __init__(self, path):
        self.root = Path(path)
        if not self.root.is_dir():
            raise ArchiveError(f"not an archive directory: {path}")
        self.lock = _open_lock(self.root)
        try:
            self._reconcile()
            self.state = self._load_state()
            self.index = self._load_index()
            self.snapshot = replay_snapshot(self.state, self.root)
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

    # ------------------------------------------------------------------ open
    def _load_state(self):
        try:
            state = _read_json(self.root / STATE_NAME)
        except FileNotFoundError as exc:
            raise ArchiveError("archive state is missing") from exc
        except json.JSONDecodeError as exc:
            raise ArchiveError(f"archive state is corrupt: {exc}") from exc
        required = {"format", "dataset_id", "width", "height", "apertures",
                    "products", "revision"}
        if not isinstance(state, dict) or set(state) != required \
                or state.get("format") != FORMAT:
            raise ArchiveError("archive state has an unknown layout")
        if type(state["revision"]) is not int or state["revision"] < 0:
            raise ArchiveError("archive state has an invalid revision")
        return state

    def _load_index(self):
        path = self.root / INDEX_NAME
        if not path.exists():
            return {"format": FORMAT, "batches": []}
        try:
            index = _read_json(path)
        except json.JSONDecodeError as exc:
            raise ArchiveError(f"batch index is corrupt: {exc}") from exc
        if not isinstance(index, dict) or set(index) != {"format", "batches"} \
                or index.get("format") != FORMAT \
                or not isinstance(index["batches"], list):
            raise ArchiveError("batch index has an unknown layout")
        return index

    def _reconcile(self):
        """Recover from a crash between state replacement and index update, and
        remove temporary files that never became committed artifacts."""
        state_path = self.root / STATE_NAME
        if not state_path.exists():
            for stray in self.root.glob(STATE_TMP_PREFIX + "*"):
                _unlink_quiet(stray)
            return
        try:
            state = _read_json(state_path)
        except json.JSONDecodeError as exc:
            raise ArchiveError(f"archive state is corrupt: {exc}") from exc
        if not isinstance(state, dict) or state.get("format") != FORMAT:
            raise ArchiveError("archive state has an unknown layout")
        revision = state.get("revision")
        if type(revision) is not int or revision < 0:
            raise ArchiveError("archive state has an invalid revision")
        revision_dir = self.root / "revisions"
        if revision > 0:
            envelope = revision_dir / f"{revision:012d}.json"
            if not envelope.exists():
                raise ArchiveError(
                    f"state points at revision {revision} but its batch is missing")
        index_path = self.root / INDEX_NAME
        index = {"format": FORMAT, "batches": []}
        if index_path.exists():
            try:
                index = _read_json(index_path)
            except json.JSONDecodeError as exc:
                raise ArchiveError(f"batch index is corrupt: {exc}") from exc
            if not isinstance(index, dict) or index.get("format") != FORMAT \
                    or not isinstance(index.get("batches"), list):
                raise ArchiveError("batch index has an unknown layout")
        committed = {entry["revision"]: entry
                     for entry in index["batches"]
                     if isinstance(entry, dict)
                     and type(entry.get("revision")) is int}
        for number in range(1, revision + 1):
            envelope_path = revision_dir / f"{number:012d}.json"
            try:
                payload = _read_json(envelope_path)
            except FileNotFoundError as exc:
                raise ArchiveError(f"missing batch envelope for revision {number}") from exc
            except json.JSONDecodeError as exc:
                raise ArchiveError(f"batch envelope {number} is corrupt: {exc}") from exc
            self._check_envelope(payload, number, state.get("dataset_id"))
            if number not in committed:
                committed[number] = {"batch_id": payload["batch_id"],
                                     "revision": number,
                                     "canonical": payload["canonical"],
                                     "receipt": payload["receipt"]}
        expected = [committed[number] for number in range(1, revision + 1)]
        if [entry["revision"] for entry in index["batches"]] != \
                [number for number in range(1, revision + 1)]:
            _atomic_json(index_path, {"format": FORMAT, "batches": expected})
        for pattern in (STATE_TMP_PREFIX + "*", ENVELOPE_TMP_PREFIX + "*",
                        DATA_TMP_PREFIX + "*"):
            for stray in self.root.rglob(pattern):
                if stray.is_file():
                    _unlink_quiet(stray)

    def _check_envelope(self, payload, number, dataset_id):
        required = {"format", "dataset_id", "batch_id", "revision", "canonical",
                    "batch", "files", "receipt"}
        if not isinstance(payload, dict) or set(payload) != required:
            raise ArchiveError(f"batch envelope {number} has an unknown layout")
        if payload["format"] != FORMAT \
                or payload["dataset_id"] != dataset_id \
                or payload["revision"] != number:
            raise ArchiveError(f"batch envelope {number} is inconsistent")
        if payload["receipt"] != {"dataset_id": payload["dataset_id"],
                                  "batch_id": payload["batch_id"],
                                  "revision": number}:
            raise ArchiveError(f"batch envelope {number} has a bad receipt")
        if canonical_json(batch_content(payload["batch"])) != payload["canonical"]:
            raise ArchiveError(f"batch envelope {number} fails canonical check")
        for group in ("darks", "flats", "frames"):
            files = payload["files"].get(group)
            if not isinstance(files, list) or any(
                    not isinstance(name, str) for name in files):
                raise ArchiveError(f"batch envelope {number} has bad file lists")
            for name in files:
                if not (self.root / group / name).is_file():
                    raise ArchiveError(
                        f"batch envelope {number} references missing {group}/{name}")

    def state_id_hint(self):
        state_path = self.root / STATE_NAME
        try:
            return _read_json(state_path).get("dataset_id")
        except (OSError, json.JSONDecodeError):
            return None

    # ----------------------------------------------------------------- commit
    def commit(self, batch_value, fault_hook=None):
        """Validate and atomically commit one batch; return its receipt."""
        width = self.state["width"]
        height = self.state["height"]
        batch = validate_batch(batch_value, width, height)
        canonical = canonical_json(batch_content(batch_value))
        for entry in self.index["batches"]:
            if entry["batch_id"] == batch["batch_id"]:
                if entry["canonical"] == canonical:
                    return dict(entry["receipt"])
                raise ValueError(
                    f"batch_id {batch['batch_id']!r} already committed with "
                    "different content")
        if batch["expected_revision"] != self.state["revision"]:
            raise ValueError(
                f"expected_revision {batch['expected_revision']} does not match "
                f"current revision {self.state['revision']}")
        plan = self.snapshot.plan(batch)
        next_revision = self.state["revision"] + 1
        try:
            files = self._write_batch_data(next_revision, plan)
            receipt = {"dataset_id": self.state["dataset_id"],
                       "batch_id": batch["batch_id"], "revision": next_revision}
            envelope = {"format": FORMAT, "dataset_id": self.state["dataset_id"],
                        "batch_id": batch["batch_id"], "revision": next_revision,
                        "canonical": canonical, "batch": batch_value,
                        "files": files, "receipt": receipt}
            envelope_path = self.root / "revisions" / f"{next_revision:012d}.json"
            descriptor, temporary = tempfile.mkstemp(prefix=ENVELOPE_TMP_PREFIX,
                                                     dir=envelope_path.parent)
            with os.fdopen(descriptor, "wb") as output:
                output.write((json.dumps(envelope, ensure_ascii=False,
                                         allow_nan=False, sort_keys=True)
                              + "\n").encode("utf-8"))
            # the envelope is only real once its final name exists
            os.replace(temporary, envelope_path)
        except BaseException:
            # nothing past this batch is visible yet; drop its unreferenced files
            for group in ("darks", "flats", "frames"):
                prefix = f"{next_revision:012d}-"
                for orphan in (self.root / group).glob(prefix + "*"):
                    _unlink_quiet(orphan)
            raise
        next_state = dict(self.state)
        next_state["revision"] = next_revision
        state_tmp = None
        try:
            descriptor, state_tmp = tempfile.mkstemp(
                prefix=STATE_TMP_PREFIX, dir=self.root)
            with os.fdopen(descriptor, "wb") as output:
                output.write((json.dumps(next_state, ensure_ascii=False,
                                         allow_nan=False, sort_keys=True)
                              + "\n").encode("utf-8"))
            if fault_hook is not None:
                fault_hook("before_state_replace", self, next_revision)
            os.replace(state_tmp, self.root / STATE_NAME)
            state_tmp = None
        finally:
            if state_tmp is not None:
                _unlink_quiet(state_tmp)
        if fault_hook is not None:
            fault_hook("after_state_replace", self, next_revision)
        index = {"format": FORMAT,
                 "batches": self.index["batches"] + [{
                     "batch_id": batch["batch_id"], "revision": next_revision,
                     "canonical": canonical, "receipt": receipt}]}
        _atomic_json(self.root / INDEX_NAME, index)
        if fault_hook is not None:
            fault_hook("after_index_replace", self, next_revision)
        self.state = next_state
        self.index = index
        self.snapshot = replay_snapshot(self.state, self.root)
        return dict(receipt)

    def _write_batch_data(self, revision, plan):
        files = {"darks": [], "flats": [], "frames": []}
        for record in plan["darks"]:
            name = f"{revision:012d}-{record['id']}.json"
            _atomic_json(self.root / "darks" / name, record)
            files["darks"].append(name)
        for record in plan["flats"]:
            stored = {key: value for key, value in record.items()
                      if key != "mean_flat"}
            stored["mean_flat"] = str(record["mean_flat"])
            name = f"{revision:012d}-{record['id']}.json"
            _atomic_json(self.root / "flats" / name, stored)
            files["flats"].append(name)
        for record in plan["frames"]:
            name = f"{revision:012d}-{record['id']}.json"
            _atomic_json(self.root / "frames" / name, record)
            files["frames"].append(name)
        return files


def _unlink_quiet(path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def replay_snapshot(state, root, revision=None):
    """Replay history up to a revision (default: the state's current one)."""
    if revision is None:
        revision = state["revision"]
    if type(revision) is not int or not 0 <= revision <= state["revision"]:
        raise ValueError(f"unknown revision: {revision}")
    snapshot = Snapshot(state)
    snapshot.revision = revision
    for number in range(1, revision + 1):
        payload = _read_json(root / "revisions" / f"{number:012d}.json")
        snapshot.apply_envelope(payload, root)
    return snapshot


class Snapshot:
    """In-memory view of every fact visible at one revision."""

    def __init__(self, state):
        self.dataset_id = state["dataset_id"]
        self.width = state["width"]
        self.height = state["height"]
        self.apertures = state["apertures"]
        self.products = state["products"]
        self.darks = {}
        self.flats = {}
        self.releases = {}
        self.versions = set()
        self.frames = {}
        self.revision = 0
        self.frame_limit = (CAMPAIGN_FRAME_LIMIT if state["products"] is not None
                            else CLASSIC_FRAME_LIMIT)

    def apply_envelope(self, payload, root):
        for name in payload["files"]["darks"]:
            record = _read_json(root / "darks" / name)
            self.darks[record["id"]] = record
        for name in payload["files"]["flats"]:
            record = _read_json(root / "flats" / name)
            record["mean_flat"] = _parse_fraction(record["mean_flat"])
            self.flats[record["id"]] = record
        batch = payload["batch"]
        for release in batch["add_releases"]:
            self.releases[release["id"]] = dict(release, retracted=False)
        for release_id in batch["retract_releases"]:
            self.releases[release_id]["retracted"] = True
        self.versions.update(release["version"]
                             for release in batch["add_releases"])
        for name in payload["files"]["frames"]:
            record = _read_json(root / "frames" / name)
            self.frames[record["id"]] = record

    def plan(self, batch):
        """Validate the batch against current state and build its stored records."""
        dark_ids = set(self.darks)
        flat_ids = set(self.flats)
        for record in batch["add_darks"]:
            if record["id"] in dark_ids:
                raise ValueError(f"dark id already registered: {record['id']}")
            dark_ids.add(record["id"])
        for record in batch["add_flats"]:
            if record["id"] in flat_ids:
                raise ValueError(f"flat id already registered: {record['id']}")
            flat_ids.add(record["id"])
        release_ids = set(self.releases)
        versions = set(self.versions)
        for record in batch["add_releases"]:
            if record["id"] in release_ids:
                raise ValueError(f"release id already registered: {record['id']}")
            release_ids.add(record["id"])
            if record["version"] in versions:
                raise ValueError(
                    f"release version already used: {record['version']}")
            versions.add(record["version"])
            if record["dark_id"] not in dark_ids:
                raise ValueError(
                    f"release references unknown dark_id: {record['dark_id']}")
            if record["flat_id"] not in flat_ids:
                raise ValueError(
                    f"release references unknown flat_id: {record['flat_id']}")
        added_releases = {record["id"] for record in batch["add_releases"]}
        for release_id in batch["retract_releases"]:
            if release_id in added_releases:
                raise ValueError(
                    f"release added and retracted in the same batch: {release_id}")
            existing = self.releases.get(release_id)
            if existing is None:
                raise ValueError(f"unknown release id: {release_id}")
            if existing["retracted"]:
                raise ValueError(f"release already retracted: {release_id}")
        touched_frames = set()
        stored_frames = []
        for record in batch["put_frames"]:
            frame_id = record["id"]
            if frame_id not in self.frames and record["expected_version"] != 0:
                raise ValueError(
                    f"new frame {frame_id!r} requires expected_version 0")
            if frame_id in self.frames:
                current = self.frames[frame_id]
                if record["expected_version"] != current["frame_version"]:
                    raise ValueError(
                        f"frame version conflict for {frame_id!r}: "
                        f"expected {record['expected_version']}, "
                        f"current {current['frame_version']}")
            stored = {key: record[key] for key in
                     ("id", "exposure_s", "dx", "dy", "saturation", "pixels",
                      "observed_at_s", "variance")}
            stored["frame_version"] = record["expected_version"] + 1
            stored["withdrawn"] = False
            stored_frames.append(stored)
            touched_frames.add(frame_id)
        for record in batch["withdraw_frames"]:
            frame_id = record["id"]
            current = self.frames.get(frame_id)
            if current is None or current["withdrawn"]:
                raise ValueError(f"no active frame to withdraw: {frame_id}")
            if record["expected_version"] != current["frame_version"]:
                raise ValueError(
                    f"frame version conflict for {frame_id!r}: "
                    f"expected {record['expected_version']}, "
                    f"current {current['frame_version']}")
            stored = dict(current)
            stored["frame_version"] = current["frame_version"] + 1
            stored["withdrawn"] = True
            stored_frames.append(stored)
            touched_frames.add(frame_id)
        ever_seen = set(self.frames) | touched_frames
        if len(ever_seen) > self.frame_limit:
            raise ValueError(
                f"archive holds at most {self.frame_limit} distinct frame ids")
        return {"darks": batch["add_darks"], "flats": batch["add_flats"],
                "frames": stored_frames}


def _parse_fraction(text):
    from fractions import Fraction
    return Fraction(text)
