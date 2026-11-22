"""Time-series transient discovery against a fixed local calibration archive.

Public entry points:

* initialize(path, archive_path, config)
* advance(path, revision)
* snapshot(path)
* pending_events(path, limit)
* acknowledge(path, through_seq)

Science inputs are fixed archive revisions only: every advance captures the
archive's full frame versions, masters and release state at one committed
revision and never mixes in newer facts. This module never writes facts into
the source archive and does not change ``service.run``, the stack/campaign
entries or the archive commit/render contracts.
"""
from ..archive.store import Store, replay_snapshot
from .config import validate_config
from .science import build_snapshot, diff_alerts
from .store import FORMAT, Workspace, WorkspaceError, create_workspace

__all__ = ["initialize", "advance", "snapshot", "pending_events",
           "acknowledge", "WorkspaceError"]


def _strict_nonneg_int(value, name):
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} requires a non-negative integer")
    return value


def _strict_positive_int(value, name, maximum):
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"{name} requires an integer in [1,{maximum}]")
    return value


def _initial_state(archive_path, definition, config):
    return {"format": FORMAT,
            "dataset_id": definition["dataset_id"],
            "archive_path": str(archive_path),
            "config": config,
            "processed_revision": None,
            "last_seq": 0,
            "acknowledged_through": 0,
            "advances": []}


def initialize(path, archive_path, config):
    """Create a discovery workspace bound to an existing archive and config."""
    with Store(archive_path) as store:
        parsed_config = validate_config(config, [a["id"]
                                                 for a in store.state["apertures"]])
        state = _initial_state(archive_path, store.state, parsed_config)
    create_workspace(path, state)
    return {"dataset_id": state["dataset_id"], "processed_revision": None}


def advance(path, revision, fault_hook=None):
    """Replay the full time-series analysis at one committed archive revision.

    A revision already processed returns its original receipt without emitting
    events again or moving the current snapshot. New revisions must be greater
    than the last processed one; revisions may be skipped.
    """
    _strict_nonneg_int(revision, "revision")
    with Workspace(path) as workspace:
        existing = workspace.advance_revision(revision)
        if existing is not None:
            return existing
        processed = workspace.state["processed_revision"]
        if processed is not None and revision < processed:
            raise ValueError(
                f"revision {revision} is older than processed revision {processed}")
        # capture the fixed revision entirely while holding the archive lock,
        # then release it before any science computation
        with Store(workspace.state["archive_path"]) as store:
            if store.state["dataset_id"] != workspace.state["dataset_id"]:
                raise ValueError("archive dataset_id does not match the workspace")
            if revision > store.state["revision"]:
                raise ValueError(f"unknown archive revision: {revision}")
            captured = replay_snapshot(store.state, store.root, revision)
        previous = workspace.load_snapshot()
        report = build_snapshot(captured, workspace.state["config"], revision)
        previous_alerts = previous["active_alerts"] if previous else []
        changes = diff_alerts(previous_alerts, report["active_alerts"])
        return workspace.commit_advance(revision, report, changes,
                                        fault_hook=fault_hook)


def snapshot(path):
    """Return the immutable report of the latest successful advance."""
    with Workspace(path) as workspace:
        report = workspace.load_snapshot()
        if report is None:
            raise ValueError("workspace has no successful advance yet")
        return report


def pending_events(path, limit):
    """Read up to limit unacknowledged events without consuming them."""
    _strict_positive_int(limit, "limit", 100)
    with Workspace(path) as workspace:
        cursor, events = workspace.pending(limit)
        return {"acknowledged_through": cursor, "events": events}


def acknowledge(path, through_seq):
    """Advance the persistent acknowledgment cursor; older values are idempotent."""
    _strict_nonneg_int(through_seq, "through_seq")
    with Workspace(path) as workspace:
        cursor = workspace.acknowledge(through_seq)
        return {"acknowledged_through": cursor}
