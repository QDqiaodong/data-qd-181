"""Crash-safe local storage for discovery workspaces.

Layout (all writes are same-directory temporary files followed by
``os.replace``):

* ``state.json``        - fixed definition, processed revision pointer, queue head
* ``.lock``             - cross-process exclusive lock
* ``advances/``         - one immutable envelope per successful advance, named
                          by the source revision, holding receipt and events
* ``snapshots/``        - full immutable science snapshots, named by revision
* ``tmp/``              - staging directory; committed files never live here

The replacement of ``state.json`` is the single commit boundary: everything a
new revision needs (snapshot file, advance envelope) already exists under its
final name before the pointer switches, so reopening can only ever observe the
complete old or the complete new state.
"""
import fcntl
import json
import os
import tempfile
from pathlib import Path

FORMAT = "astro-discovery/v1"
STATE_NAME = "state.json"
LOCK_NAME = ".lock"
STATE_TMP_PREFIX = ".state-"
SNAP_TMP_PREFIX = ".snap-"
ENVELOPE_TMP_PREFIX = ".advance-"
ADVANCES_DIR = "advances"
SNAPSHOTS_DIR = "snapshots"
TMP_DIR = "tmp"


class WorkspaceError(ValueError):
    """Raised when workspace contents cannot be interpreted consistently."""


def _read_json(path):
    with Path(path).open("rb") as source:
        return json.loads(source.read())


def _atomic_write(path, data):
    target = Path(path)
    descriptor, temporary = tempfile.mkstemp(prefix=SNAP_TMP_PREFIX,
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


def create_workspace(path, state):
    """Create a brand-new workspace; never overwrite an existing path."""
    root = Path(path)
    try:
        root.mkdir(parents=True)
    except FileExistsError:
        raise FileExistsError(f"workspace already exists: {path}")
    try:
        for name in (ADVANCES_DIR, SNAPSHOTS_DIR, TMP_DIR):
            (root / name).mkdir()
        lock_handle = _open_lock(root)
        try:
            _atomic_json(root / STATE_NAME, state)
        finally:
            lock_handle.close()
    except BaseException:
        _discard(root)
        raise
    return root


def _envelope_path(root, revision):
    return root / ADVANCES_DIR / f"{revision:012d}.json"


def _snapshot_path(root, revision):
    return root / SNAPSHOTS_DIR / f"{revision:012d}.json"


def _load_and_check_state(root):
    try:
        state = _read_json(root / STATE_NAME)
    except FileNotFoundError as exc:
        raise WorkspaceError("workspace state is missing") from exc
    except json.JSONDecodeError as exc:
        raise WorkspaceError(f"workspace state is corrupt: {exc}") from exc
    required = {"format", "dataset_id", "archive_path", "config",
                "processed_revision", "last_seq", "acknowledged_through",
                "advances"}
    if not isinstance(state, dict) or set(state) != required \
            or state.get("format") != FORMAT:
        raise WorkspaceError("workspace state has an unknown layout")
    if not (state["processed_revision"] is None or
            (type(state["processed_revision"]) is int
             and state["processed_revision"] >= 0)):
        raise WorkspaceError("workspace state has an invalid processed revision")
    if type(state["last_seq"]) is not int or state["last_seq"] < 0:
        raise WorkspaceError("workspace state has an invalid sequence cursor")
    if type(state["acknowledged_through"]) is not int \
            or not 0 <= state["acknowledged_through"] <= state["last_seq"]:
        raise WorkspaceError("workspace state has an invalid ack cursor")
    if not isinstance(state["advances"], list):
        raise WorkspaceError("workspace state has an invalid advance index")
    return state


class Workspace:
    """An opened, reconciled discovery workspace; all operations hold the lock."""

    def __init__(self, path):
        self.root = Path(path)
        if not self.root.is_dir():
            raise WorkspaceError(f"not a workspace directory: {path}")
        self.lock = _open_lock(self.root)
        try:
            self._reconcile()
            self.state = _load_and_check_state(self.root)
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

    def _reconcile(self):
        """Validate the whole durable history and remove uncommitted debris."""
        state = _load_and_check_state(self.root)
        advances = state["advances"]
        expected_revisions = [entry["revision"] for entry in advances]
        if expected_revisions != sorted(expected_revisions) \
                or len(expected_revisions) != len(set(expected_revisions)):
            raise WorkspaceError("advance index is not an ordered revision list")
        if state["processed_revision"] is None:
            if advances or state["last_seq"] != 0:
                raise WorkspaceError("unprocessed workspace carries history")
        else:
            if not advances or advances[-1]["revision"] != \
                    state["processed_revision"]:
                raise WorkspaceError("processed revision lacks its envelope")
        seq_cursor = 0
        for entry in advances:
            revision = entry["revision"]
            try:
                envelope = _read_json(_envelope_path(self.root, revision))
            except FileNotFoundError as exc:
                raise WorkspaceError(
                    f"missing advance envelope for revision {revision}") from exc
            except json.JSONDecodeError as exc:
                raise WorkspaceError(
                    f"advance envelope {revision} is corrupt: {exc}") from exc
            self._check_envelope(envelope, revision, state["dataset_id"], entry)
            if not _snapshot_path(self.root, envelope["revision"]).is_file():
                raise WorkspaceError(
                    f"missing science snapshot for revision {revision}")
            for event in envelope["events"]:
                seq_cursor += 1
                if event["seq"] != seq_cursor:
                    raise WorkspaceError("event sequence is not contiguous")
        if seq_cursor != state["last_seq"]:
            raise WorkspaceError("event queue does not match the sequence cursor")
        for directory in (self.root / ADVANCES_DIR, self.root / SNAPSHOTS_DIR):
            if not directory.is_dir():
                raise WorkspaceError(f"workspace directory missing: {directory}")
        # debris from a process killed before the state pointer switched
        # debris from a process killed before the state pointer switched:
        # the advance never consumed a revision or seq, so it is safe to drop
        referenced_envelopes = {_envelope_path(self.root, entry["revision"])
                                for entry in advances}
        for path in (self.root / ADVANCES_DIR).glob("*.json"):
            if path not in referenced_envelopes:
                os.unlink(path)
        referenced_snapshots = {_snapshot_path(self.root, entry["revision"])
                                for entry in advances}
        for path in (self.root / SNAPSHOTS_DIR).glob("*.json"):
            if path not in referenced_snapshots:
                os.unlink(path)
        for pattern in (STATE_TMP_PREFIX + "*", SNAP_TMP_PREFIX + "*",
                        ENVELOPE_TMP_PREFIX + "*"):
            for stray in self.root.rglob(pattern):
                if stray.is_file():
                    os.unlink(stray)
        tmp_dir = self.root / TMP_DIR
        if tmp_dir.is_dir():
            for stray in tmp_dir.iterdir():
                if stray.is_file():
                    os.unlink(stray)

    def _check_envelope(self, envelope, revision, dataset_id, index_entry):
        required = {"format", "dataset_id", "revision", "receipt",
                    "first_seq", "last_seq", "events", "snapshot"}
        if not isinstance(envelope, dict) or set(envelope) != required:
            raise WorkspaceError(f"advance envelope {revision} has unknown layout")
        if envelope["format"] != FORMAT \
                or envelope["dataset_id"] != dataset_id \
                or envelope["revision"] != revision:
            raise WorkspaceError(f"advance envelope {revision} is inconsistent")
        receipt = {"dataset_id": dataset_id, "revision": revision,
                   "first_seq": envelope["first_seq"],
                   "last_seq": envelope["last_seq"]}
        if envelope["receipt"] != receipt:
            raise WorkspaceError(f"advance envelope {revision} has a bad receipt")
        if index_entry != {"revision": revision,
                           "first_seq": envelope["first_seq"],
                           "last_seq": envelope["last_seq"]}:
            raise WorkspaceError(f"advance index entry {revision} is inconsistent")
        seqs = [event["seq"] for event in envelope["events"]]
        if envelope["events"]:
            if seqs != list(range(envelope["first_seq"],
                                  envelope["last_seq"] + 1)):
                raise WorkspaceError(f"advance {revision} has broken seq range")
        else:
            if envelope["first_seq"] is not None or \
                    envelope["last_seq"] is not None:
                raise WorkspaceError(f"advance {revision} has a bad empty range")
        for event in envelope["events"]:
            fields = {"seq", "revision", "kind", "frame_id", "aperture_id",
                      "before", "after"}
            if set(event) != fields or event["revision"] != revision:
                raise WorkspaceError(
                    f"advance {revision} contains a malformed event")

    def advance_revision(self, revision):
        """Return the stored receipt for an already processed revision."""
        for entry in self.state["advances"]:
            if entry["revision"] == revision:
                return {"dataset_id": self.state["dataset_id"],
                        "revision": revision,
                        "first_seq": entry["first_seq"],
                        "last_seq": entry["last_seq"]}
        return None

    def commit_advance(self, revision, report, changes, fault_hook=None):
        """Persist snapshot, appended events, receipt and cursors atomically.

        All durable artifacts land under final names first; replacing
        ``state.json`` is the only step that makes the advance visible.
        """
        first_seq = self.state["last_seq"] + 1 if changes else None
        last_seq = self.state["last_seq"] + len(changes) if changes else None
        events = []
        for offset, change in enumerate(changes):
            events.append({"seq": self.state["last_seq"] + offset + 1,
                           "revision": revision, "kind": change["kind"],
                           "frame_id": change["frame_id"],
                           "aperture_id": change["aperture_id"],
                           "before": change["before"],
                           "after": change["after"]})
        receipt = {"dataset_id": self.state["dataset_id"],
                   "revision": revision,
                   "first_seq": first_seq, "last_seq": last_seq}
        envelope = {"format": FORMAT,
                    "dataset_id": self.state["dataset_id"],
                    "revision": revision, "receipt": receipt,
                    "first_seq": first_seq, "last_seq": last_seq,
                    "events": events, "snapshot": f"{revision:012d}.json"}
        # stage into the dedicated tmp directory, then publish under final names
        snap_fd, snapshot_tmp_name = tempfile.mkstemp(
            prefix=SNAP_TMP_PREFIX, dir=self.root / TMP_DIR)
        env_fd, envelope_tmp_name = tempfile.mkstemp(
            prefix=ENVELOPE_TMP_PREFIX, dir=self.root / TMP_DIR)
        snapshot_tmp = Path(snapshot_tmp_name)
        envelope_tmp = Path(envelope_tmp_name)
        try:
            with os.fdopen(snap_fd, "wb") as output:
                output.write((json.dumps(report, ensure_ascii=False,
                                         allow_nan=False, sort_keys=True)
                              + "\n").encode("utf-8"))
            with os.fdopen(env_fd, "wb") as output:
                output.write((json.dumps(envelope, ensure_ascii=False,
                                         allow_nan=False, sort_keys=True)
                              + "\n").encode("utf-8"))
            os.replace(snapshot_tmp, _snapshot_path(self.root, revision))
            snapshot_tmp = None
            os.replace(envelope_tmp, _envelope_path(self.root, revision))
            envelope_tmp = None
        except BaseException:
            for path in (snapshot_tmp, envelope_tmp):
                if path is not None:
                    try:
                        os.unlink(path)
                    except FileNotFoundError:
                        pass
            raise
        next_state = dict(self.state)
        next_state["processed_revision"] = revision
        next_state["last_seq"] = (
            self.state["last_seq"] + len(changes))
        next_state["advances"] = self.state["advances"] + [{
            "revision": revision, "first_seq": first_seq,
            "last_seq": last_seq}]
        descriptor, state_tmp = tempfile.mkstemp(
            prefix=STATE_TMP_PREFIX, dir=self.root)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write((json.dumps(next_state, ensure_ascii=False,
                                         allow_nan=False, sort_keys=True)
                              + "\n").encode("utf-8"))
            if fault_hook is not None:
                fault_hook("before_state_replace", self, revision)
            os.replace(state_tmp, self.root / STATE_NAME)
        finally:
            try:
                os.unlink(state_tmp)
            except FileNotFoundError:
                pass
        if fault_hook is not None:
            fault_hook("after_state_replace", self, revision)
        self.state = next_state
        return receipt

    def load_snapshot(self):
        if self.state["processed_revision"] is None:
            return None
        return _read_json(_snapshot_path(
            self.root, self.state["processed_revision"]))

    def pending(self, limit):
        cursor = self.state["acknowledged_through"]
        events = []
        for entry in self.state["advances"]:
            if entry["last_seq"] is None or entry["last_seq"] <= cursor:
                continue
            envelope = _read_json(_envelope_path(self.root, entry["revision"]))
            for event in envelope["events"]:
                if event["seq"] > cursor:
                    events.append(event)
                    if len(events) == limit:
                        return cursor, events
        return cursor, events

    def acknowledge(self, through_seq):
        if through_seq > self.state["last_seq"]:
            raise ValueError(
                f"through_seq {through_seq} is beyond queue tail "
                f"{self.state['last_seq']}")
        if through_seq <= self.state["acknowledged_through"]:
            return self.state["acknowledged_through"]
        next_state = dict(self.state)
        next_state["acknowledged_through"] = through_seq
        descriptor, state_tmp = tempfile.mkstemp(
            prefix=STATE_TMP_PREFIX, dir=self.root)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write((json.dumps(next_state, ensure_ascii=False,
                                         allow_nan=False, sort_keys=True)
                              + "\n").encode("utf-8"))
            os.replace(state_tmp, self.root / STATE_NAME)
        finally:
            try:
                os.unlink(state_tmp)
            except FileNotFoundError:
                pass
        self.state = next_state
        return through_seq
