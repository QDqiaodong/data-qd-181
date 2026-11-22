"""Discovery concurrency, atomic commit boundaries and crash recovery."""
import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from astro_stack.archive import apply_batch, initialize
from astro_stack import discovery
from astro_stack.discovery.store import Workspace, WorkspaceError

from archive_helpers import batch, classic_definition, dark, flat, frame, release
from discovery_helpers import TempWorkspace, base_config

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


def build_archive(root):
    """Three frames; revisions 2 and 3 each emit two events."""
    initialize(str(root), classic_definition())
    apply_batch(str(root), batch("b1", 0,
        add_darks=[dark("d0", value=0, variance=0)],
        add_flats=[flat()],
        add_releases=[release("r1", 1)],
        put_frames=[frame("a", 0, value=10, variance=1, observed_at=1),
                    frame("b", 0, value=12, variance=1, observed_at=2),
                    frame("c", 0, value=14, variance=1, observed_at=3)]))


def correct_b(root, batch_id, expected_revision, value, frame_version):
    apply_batch(str(root), batch(batch_id, expected_revision,
        put_frames=[frame("b", frame_version, value=value, variance=1,
                          observed_at=2)]))


def queue_invariants(workspace_path):
    """The durable history can never split snapshots, receipts and events."""
    with Workspace(str(workspace_path)) as workspace:
        state = workspace.state
        envelopes = sorted((workspace_path / "advances").glob("*.json"))
        snapshots = sorted((workspace_path / "snapshots").glob("*.json"))
        self_count = len(state["advances"])
        assert len(envelopes) == self_count
        assert len(snapshots) == self_count
        seqs = []
        for entry in state["advances"]:
            envelope = json.loads((workspace_path / "advances" /
                                   f"{entry['revision']:012d}.json").read_text())
            assert (workspace_path / "snapshots" /
                    f"{entry['revision']:012d}.json").is_file()
            seqs.extend(event["seq"] for event in envelope["events"])
        assert seqs == list(range(1, state["last_seq"] + 1))
        assert 0 <= state["acknowledged_through"] <= state["last_seq"]
        return state


class ConcurrentAdvanceTests(unittest.TestCase):
    def _worker(self, workspace_dir):
        return textwrap.dedent(f'''
import json, sys
sys.path.insert(0, {str(SRC)!r})
from astro_stack import discovery
workspace, revision = sys.argv[1], int(sys.argv[2])
try:
    receipt = discovery.advance(workspace, revision)
    print(json.dumps({{"ok": True, "receipt": receipt}}))
except Exception as exc:  # reported back as JSON, never raised to the runner
    print(json.dumps({{"ok": False, "error": str(exc)}}))
''')

    def test_independent_processes_advance_same_revision_once(self):
        with TempWorkspace() as workspace:
            build_archive(workspace.archive)
            workspace.initialize()
            discovery.advance(str(workspace.path), 1)
            correct_b(workspace.archive, "b2", 1, 20, 1)
            with tempfile.TemporaryDirectory() as directory:
                script = Path(directory, "worker.py")
                script.write_text(self._worker(directory))
                env = dict(os.environ, PYTHONPATH=str(SRC))
                processes = [
                    subprocess.Popen(
                        [sys.executable, str(script), str(workspace.path), "2"],
                        stdout=subprocess.PIPE, text=True, env=env)
                    for _ in range(4)]
                outcomes = [json.loads(proc.communicate(timeout=30)[0])
                            for proc in processes]
            self.assertTrue(all(outcome["ok"] for outcome in outcomes))
            receipts = {json.dumps(outcome["receipt"], sort_keys=True)
                        for outcome in outcomes}
            self.assertEqual(len(receipts), 1)
            state = queue_invariants(workspace.path)
            self.assertEqual(state["last_seq"], 2)
            self.assertEqual(len(state["advances"]), 2)
            pending = discovery.pending_events(str(workspace.path), 100)
            self.assertEqual([event["seq"] for event in pending["events"]],
                             [1, 2])
            self.assertTrue(all(event["kind"] == "discover"
                                for event in pending["events"]))

    def test_acknowledgment_and_advance_concurrently_lose_nothing(self):
        with TempWorkspace() as workspace:
            build_archive(workspace.archive)
            workspace.initialize()
            discovery.advance(str(workspace.path), 1)
            correct_b(workspace.archive, "b2", 1, 20, 1)
            correct_b(workspace.archive, "b3", 2, 12, 2)
            advancer = textwrap.dedent(f'''
import sys, time
sys.path.insert(0, {str(SRC)!r})
from astro_stack import discovery
workspace = sys.argv[1]
discovery.advance(workspace, 2)
time.sleep(0.15)
discovery.advance(workspace, 3)
''')
            acker = textwrap.dedent(f'''
import sys, time
from pathlib import Path
sys.path.insert(0, {str(SRC)!r})
from astro_stack import discovery
workspace = Path(sys.argv[1])
for _ in range(200):
    if (workspace / "state.json").exists():
        break
    time.sleep(0.01)
for _ in range(100):
    if (workspace / "advances" / "000000000002.json").exists():
        break
    time.sleep(0.01)
discovery.acknowledge(str(workspace), 2)
for _ in range(100):
    if (workspace / "advances" / "000000000003.json").exists():
        break
    time.sleep(0.01)
discovery.acknowledge(str(workspace), 4)
''')
            with tempfile.TemporaryDirectory() as directory:
                adv_path = Path(directory, "advancer.py")
                ack_path = Path(directory, "acker.py")
                adv_path.write_text(advancer)
                ack_path.write_text(acker)
                env = dict(os.environ, PYTHONPATH=str(SRC))
                proc_adv = subprocess.Popen(
                    [sys.executable, str(adv_path), str(workspace.path)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, env=env)
                proc_ack = subprocess.Popen(
                    [sys.executable, str(ack_path), str(workspace.path)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, env=env)
                _, adv_err = proc_adv.communicate(timeout=30)
                _, ack_err = proc_ack.communicate(timeout=30)
                self.assertEqual(adv_err, "")
                self.assertEqual(ack_err, "")
            state = queue_invariants(workspace.path)
            self.assertEqual(state["last_seq"], 4)
            self.assertEqual(state["acknowledged_through"], 4)
            # acknowledgment changes delivery only; history stays complete
            self.assertEqual(discovery.snapshot(str(workspace.path))["revision"],
                             3)
            self.assertEqual(
                discovery.pending_events(str(workspace.path), 100)["events"],
                [])


class CommitBoundaryCrashTests(unittest.TestCase):
    def setUp(self):
        self.workspace = TempWorkspace()
        self.addCleanup(self.workspace.close)
        build_archive(self.workspace.archive)
        self.workspace.initialize()
        discovery.advance(str(self.workspace.path), 1)
        correct_b(self.workspace.archive, "b2", 1, 20, 1)

    def test_crash_before_state_switch_consumes_nothing(self):
        def hook(stage, workspace, revision):
            if stage == "before_state_replace":
                raise RuntimeError("simulated termination")

        with self.assertRaises(RuntimeError):
            discovery.advance(str(self.workspace.path), 2, fault_hook=hook)
        state = queue_invariants(self.workspace.path)
        self.assertEqual(state["processed_revision"], 1)
        self.assertEqual(state["last_seq"], 0)
        # staged artifacts were reconciled away; retrying starts seq at 1
        receipt = discovery.advance(str(self.workspace.path), 2)
        self.assertEqual(receipt, {"dataset_id": "ds", "revision": 2,
                                   "first_seq": 1, "last_seq": 2})
        queue_invariants(self.workspace.path)

    def test_crash_after_state_switch_retry_returns_original_receipt(self):
        def hook(stage, workspace, revision):
            if stage == "after_state_replace":
                raise RuntimeError("simulated termination")

        with self.assertRaises(RuntimeError):
            discovery.advance(str(self.workspace.path), 2, fault_hook=hook)
        # reopen sees the complete new state even though the caller got nothing
        state = queue_invariants(self.workspace.path)
        self.assertEqual(state["processed_revision"], 2)
        self.assertEqual(state["last_seq"], 2)
        retry = discovery.advance(str(self.workspace.path), 2)
        self.assertEqual(retry, {"dataset_id": "ds", "revision": 2,
                                 "first_seq": 1, "last_seq": 2})
        # no duplicate numbering and the snapshot carries the discoveries
        pending = discovery.pending_events(str(self.workspace.path), 100)
        self.assertEqual([event["seq"] for event in pending["events"]], [1, 2])
        self.assertEqual(
            len(discovery.snapshot(str(self.workspace.path))["active_alerts"]),
            2)

    def test_sigkill_at_each_boundary_then_reopen_and_retry(self):
        worker = textwrap.dedent(f'''
import os, signal, sys
sys.path.insert(0, {str(SRC)!r})
from astro_stack import discovery
def hook(stage, _workspace, _revision):
    if stage == sys.argv[3]:
        os.kill(os.getpid(), signal.SIGKILL)
discovery.advance(sys.argv[1], int(sys.argv[2]), fault_hook=hook)
''')
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory, "killer.py")
            script.write_text(worker)
            env = dict(os.environ, PYTHONPATH=str(SRC))
            for stage in ("before_state_replace", "after_state_replace"):
                with self.subTest(stage=stage):
                    workspace = TempWorkspace()
                    self.addCleanup(workspace.close)
                    build_archive(workspace.archive)
                    workspace.initialize()
                    discovery.advance(str(workspace.path), 1)
                    correct_b(workspace.archive, "b2", 1, 20, 1)
                    proc = subprocess.Popen(
                        [sys.executable, str(script), str(workspace.path),
                         "2", stage],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        text=True, env=env)
                    proc.communicate(timeout=30)
                    self.assertEqual(proc.returncode, -signal.SIGKILL)
                    state = queue_invariants(workspace.path)
                    expected_revision = (1 if stage == "before_state_replace"
                                         else 2)
                    self.assertEqual(state["processed_revision"],
                                     expected_revision)
                    # a retry after either crash gets the one original receipt
                    receipt = discovery.advance(str(workspace.path), 2)
                    self.assertEqual(receipt, {
                        "dataset_id": "ds", "revision": 2,
                        "first_seq": 1, "last_seq": 2})
                    queue_invariants(workspace.path)
                    self.assertEqual(
                        [event["seq"] for event in
                         discovery.pending_events(str(workspace.path), 100)
                         ["events"]], [1, 2])


class CorruptionReportingTests(unittest.TestCase):
    def test_unexplainable_damage_is_reported_never_silently_reset(self):
        with TempWorkspace() as workspace:
            build_archive(workspace.archive)
            workspace.initialize()
            discovery.advance(str(workspace.path), 1)
            correct_b(workspace.archive, "b2", 1, 20, 1)
            discovery.advance(str(workspace.path), 2)
            # break the contiguous seq contract inside a committed envelope
            envelope = workspace.path / "advances" / "000000000002.json"
            payload = json.loads(envelope.read_text())
            payload["events"][0]["seq"] = 99
            envelope.write_text(json.dumps(payload))
            with self.assertRaises(WorkspaceError):
                discovery.pending_events(str(workspace.path), 10)
            with self.assertRaises(WorkspaceError):
                discovery.advance(str(workspace.path), 3)
            # the queue is not rebuilt or truncated
            with self.assertRaises(WorkspaceError):
                discovery.snapshot(str(workspace.path))


if __name__ == "__main__":
    unittest.main()
