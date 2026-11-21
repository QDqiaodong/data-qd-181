"""Atomicity, idempotent retry identity, concurrency and crash recovery."""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from astro_stack.archive import apply_batch, initialize, render
from astro_stack.archive.store import Store, canonical_json, batch_content

from archive_helpers import batch, classic_definition, dark, flat, frame, release
from archive_helpers import TempArchive

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


def standard_batch(batch_id="b1", expected_revision=0):
    return batch(batch_id, expected_revision,
                 add_darks=[dark("d0")], add_flats=[flat()],
                 add_releases=[release("r1", 1)],
                 put_frames=[frame("fr", 0)])


class BatchAtomicityTests(unittest.TestCase):
    """Acceptance behavior 4: an illegal last record rolls back the whole batch."""

    def test_failed_batch_consumes_no_revision_version_or_fact(self):
        archive = TempArchive()
        self.addCleanup(archive.close)
        initialize(str(archive.path), classic_definition())
        good = standard_batch()
        # last record references a missing dark
        good["add_releases"].append(release("r-bad", 2, dark_id="missing"))
        with self.assertRaises(ValueError):
            apply_batch(str(archive.path), good)
        with Store(str(archive.path)) as store:
            self.assertEqual(store.state["revision"], 0)
            self.assertEqual(store.index["batches"], [])
            self.assertEqual(store.snapshot.darks, {})
            self.assertEqual(store.snapshot.frames, {})
        # the batch id was never consumed; the corrected batch commits
        fixed = standard_batch()
        receipt = apply_batch(str(archive.path), fixed)
        self.assertEqual(receipt["revision"], 1)

    def test_retry_identity_priority_over_expected_revision(self):
        archive = TempArchive()
        self.addCleanup(archive.close)
        initialize(str(archive.path), classic_definition())
        first = apply_batch(str(archive.path), standard_batch())
        # advance history with another batch
        apply_batch(str(archive.path), batch(
            "b2", 1, add_releases=[release("r2", 2)]))
        stale = standard_batch()
        stale["expected_revision"] = 999
        retry = apply_batch(str(archive.path), stale)
        self.assertEqual(retry, first)
        with Store(str(archive.path)) as store:
            self.assertEqual(store.state["revision"], 2)
            self.assertEqual(len(store.index["batches"]), 2)
        # same id, different content is always rejected
        altered = standard_batch()
        altered["add_darks"] = []
        altered["expected_revision"] = 2
        with self.assertRaisesRegex(ValueError, "different content"):
            apply_batch(str(archive.path), altered)
        # an unseen id still obeys expected_revision
        unseen = batch("b3", 5)
        unseen["add_releases"] = [release("r3", 3)]
        with self.assertRaisesRegex(ValueError, "expected_revision"):
            apply_batch(str(archive.path), unseen)

    def test_canonical_text_ignores_input_formatting(self):
        value = standard_batch()
        compact = canonical_json(batch_content(value))
        pretty = canonical_json(batch_content(json.loads(json.dumps(value, indent=4))))
        self.assertEqual(compact, pretty)
        reordered = {key: value[key] for key in reversed(list(value))}
        self.assertEqual(canonical_json(batch_content(reordered)), compact)

    def test_expected_revision_excluded_from_retry_identity(self):
        value = standard_batch()
        other = dict(value)
        other["expected_revision"] = 42
        self.assertEqual(canonical_json(batch_content(value)),
                         canonical_json(batch_content(other)))
        self.assertNotEqual(canonical_json(value), canonical_json(other))


class ConcurrencyTests(unittest.TestCase):
    """Acceptance behavior 4: two processes, one expected_revision, one winner."""

    def _worker_script(self):
        return textwrap.dedent(f'''
import json, sys, time
sys.path.insert(0, {str(SRC)!r})
from pathlib import Path
from astro_stack.archive.store import Store
root, batch_path, marker = sys.argv[1:4]
payload = json.loads(Path(batch_path).read_text())
for _ in range(250):
    if Path(marker).exists():
        break
    time.sleep(0.02)
try:
    with Store(root) as store:
        receipt = store.commit(payload)
    print(json.dumps({{"ok": True, "receipt": receipt}}))
except Exception as exc:  # noqa: BLE001 - reported back as JSON
    print(json.dumps({{"ok": False, "error": str(exc)}}))
''')

    def test_two_processes_one_complete_commit(self):
        archive = TempArchive()
        self.addCleanup(archive.close)
        initialize(str(archive.path), classic_definition())
        with tempfile.TemporaryDirectory() as workspace:
            script = Path(workspace, "worker.py")
            script.write_text(self._worker_script())
            payload_a = standard_batch("bA")
            payload_b = standard_batch("bB")
            payload_b["put_frames"][0]["id"] = "frB"
            path_a = Path(workspace, "a.json")
            path_b = Path(workspace, "b.json")
            path_a.write_text(json.dumps(payload_a))
            path_b.write_text(json.dumps(payload_b))
            marker = Path(workspace, "go")
            env = dict(os.environ, PYTHONPATH=str(SRC))
            proc_a = subprocess.Popen(
                [sys.executable, str(script), str(archive.path),
                 str(path_a), str(marker)],
                stdout=subprocess.PIPE, text=True, env=env)
            proc_b = subprocess.Popen(
                [sys.executable, str(script), str(archive.path),
                 str(path_b), str(marker)],
                stdout=subprocess.PIPE, text=True, env=env)
            marker.write_text("go")
            result_a = json.loads(proc_a.communicate(timeout=30)[0])
            result_b = json.loads(proc_b.communicate(timeout=30)[0])
        outcomes = [result_a, result_b]
        self.assertEqual(sum(1 for result in outcomes if result["ok"]), 1)
        winner = next(result for result in outcomes if result["ok"])
        self.assertEqual(winner["receipt"]["revision"], 1)
        with Store(str(archive.path)) as store:
            self.assertEqual(store.state["revision"], 1)
            self.assertEqual(len(store.index["batches"]), 1)
            self.assertEqual(len(store.snapshot.frames), 1)

    def test_render_releases_lock_before_science_finishes(self):
        archive = TempArchive()
        self.addCleanup(archive.close)
        initialize(str(archive.path), classic_definition())
        apply_batch(str(archive.path), standard_batch())
        renderer = textwrap.dedent(f'''
import json, sys, time
sys.path.insert(0, {str(SRC)!r})
from pathlib import Path
from astro_stack.archive.store import Store, replay_snapshot
from astro_stack.archive.science import render as do_render
root, marker = sys.argv[1:3]
with Store(root) as store:
    snapshot = replay_snapshot(store.state, store.root, 1)
Path(marker).write_text("captured")
time.sleep(1.2)
report = do_render(snapshot)
print(json.dumps({{"revision": report["revision"],
                  "frames": [b["frame_id"] for b in report["frame_bindings"]]}}))
''')
        with tempfile.TemporaryDirectory() as workspace:
            script = Path(workspace, "renderer.py")
            script.write_text(renderer)
            marker = Path(workspace, "captured")
            env = dict(os.environ, PYTHONPATH=str(SRC))
            proc = subprocess.Popen(
                [sys.executable, str(script), str(archive.path), str(marker)],
                stdout=subprocess.PIPE, text=True, env=env)
            for _ in range(100):
                if marker.exists():
                    break
                time.sleep(0.02)
            started = time.time()
            receipt = apply_batch(str(archive.path), batch(
                "b2", 1, add_releases=[release("r2", 2)]))
            self.assertLess(time.time() - started, 0.5)
            output = json.loads(proc.communicate(timeout=15)[0])
        self.assertEqual(receipt["revision"], 2)
        self.assertEqual(output, {"revision": 1, "frames": ["fr"]})


class CrashRecoveryTests(unittest.TestCase):
    """Acceptance behavior 5: fault injection at a real commit boundary."""

    def _archive_with_one_pending_batch(self):
        archive = TempArchive()
        self.addCleanup(archive.close)
        initialize(str(archive.path), classic_definition())
        return archive, standard_batch()

    def test_crash_after_state_pointer_before_receipt(self):
        archive, payload = self._archive_with_one_pending_batch()

        def hook(stage, store, revision):
            if stage == "after_state_replace":
                raise RuntimeError("simulated process termination")

        with self.assertRaises(RuntimeError):
            with Store(str(archive.path)) as store:
                store.commit(payload, fault_hook=hook)
        # reopen: the new revision is fully visible and the index is rebuilt
        report = render(str(archive.path), 1)
        self.assertEqual(report["report"]["rate_image"], [["10"]])
        # exact retry finds the original receipt instead of double-committing
        retry = apply_batch(str(archive.path), payload)
        self.assertEqual(retry, {"dataset_id": "ds", "batch_id": "b1",
                                 "revision": 1})
        with Store(str(archive.path)) as store:
            self.assertEqual(store.state["revision"], 1)
            self.assertEqual(len(store.index["batches"]), 1)

    def test_crash_before_state_pointer_leaves_old_revision(self):
        archive, payload = self._archive_with_one_pending_batch()

        def hook(stage, store, revision):
            if stage == "before_state_replace":
                raise RuntimeError("simulated process termination")

        with self.assertRaises(RuntimeError):
            with Store(str(archive.path)) as store:
                store.commit(payload, fault_hook=hook)
        with Store(str(archive.path)) as store:
            self.assertEqual(store.state["revision"], 0)
            self.assertEqual(store.index["batches"], [])
        # nothing was consumed: the same batch now commits normally
        receipt = apply_batch(str(archive.path), payload)
        self.assertEqual(receipt["revision"], 1)
        leftovers = [path for path in archive.path.rglob(".*")
                     if path.is_file() and path.name != ".lock"]
        self.assertEqual(leftovers, [])

    def test_corrupt_state_is_reported_not_replaced(self):
        archive, payload = self._archive_with_one_pending_batch()
        apply_batch(str(archive.path), payload)
        (archive.path / "state.json").write_text("{not json")
        with self.assertRaises(ValueError):
            render(str(archive.path), 1)
        with self.assertRaises(ValueError):
            apply_batch(str(archive.path),
                        batch("b2", 1, add_releases=[release("r2", 2)]))

    def test_missing_envelope_for_claimed_revision_is_corruption(self):
        archive, payload = self._archive_with_one_pending_batch()
        apply_batch(str(archive.path), payload)
        os.unlink(archive.path / "revisions" / "000000000001.json")
        with self.assertRaises(ValueError):
            with Store(str(archive.path)):
                pass


if __name__ == "__main__":
    unittest.main()
