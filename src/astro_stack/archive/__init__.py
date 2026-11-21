"""Local calibration archive: traceable per-revision astrophysical reporting.

Public entry points:

* initialize(path, definition)
* apply_batch(path, batch)
* render(path, revision)
"""
from .science import render as _render
from .store import ArchiveError, Store, create, replay_snapshot
from .validation import validate_definition

__all__ = ["initialize", "apply_batch", "render", "ArchiveError"]


def initialize(path, definition):
    """Create a new archive at path and return the revision-0 receipt."""
    parsed = validate_definition(definition)
    state = create(path, parsed)
    return {"dataset_id": state["dataset_id"], "revision": 0}


def apply_batch(path, batch, fault_hook=None):
    """Atomically apply one batch; retries of an identical batch are idempotent."""
    with Store(path) as store:
        return store.commit(batch, fault_hook=fault_hook)


def render(path, revision):
    """Render the exact committed revision without changing the archive."""
    with Store(path) as store:
        snapshot = replay_snapshot(store.state, store.root, revision)
        return _render(snapshot)
