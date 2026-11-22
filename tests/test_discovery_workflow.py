"""Discovery workflow: revisions, event queue, acknowledgments and payloads."""
import unittest

from astro_stack.archive import apply_batch, initialize
from astro_stack import discovery
from astro_stack.discovery.store import WorkspaceError

from archive_helpers import batch, classic_definition, dark, flat, frame, release
from discovery_helpers import TempWorkspace, base_config


def simple_archive(root, values=(10, 12, 14), times=(1, 2, 3)):
    initialize(str(root), classic_definition())
    apply_batch(str(root), batch("b1", 0,
        add_darks=[dark("d0", value=0, variance=0)],
        add_flats=[flat()],
        add_releases=[release("r1", 1)],
        put_frames=[frame(fid, 0, value=value, variance=1, observed_at=t)
                    for fid, value, t in zip(("a", "b", "c"), values, times)]))


class QueueAndRevisionTests(unittest.TestCase):
    def setUp(self):
        self.workspace = TempWorkspace()
        self.addCleanup(self.workspace.close)
        simple_archive(self.workspace.archive)
        self.workspace.initialize()

    def test_pending_is_repeatable_and_ack_advances_cursor(self):
        discovery.advance(str(self.workspace.path), 1)
        # no events yet: the queue stays empty but readable
        empty = discovery.pending_events(str(self.workspace.path), 10)
        self.assertEqual(empty, {"acknowledged_through": 0, "events": []})
        apply_batch(str(self.workspace.archive), batch(
            "b2", 1, put_frames=[frame("b", 1, value=20, variance=1,
                                       observed_at=2)]))
        discovery.advance(str(self.workspace.path), 2)
        first = discovery.pending_events(str(self.workspace.path), 1)
        self.assertEqual(first["acknowledged_through"], 0)
        self.assertEqual(len(first["events"]), 1)
        self.assertEqual(first["events"][0]["seq"], 1)
        # repeated reads never consume events
        again = discovery.pending_events(str(self.workspace.path), 1)
        self.assertEqual(again["events"], first["events"])
        self.assertEqual(discovery.acknowledge(str(self.workspace.path), 1),
                         {"acknowledged_through": 1})
        remaining = discovery.pending_events(str(self.workspace.path), 100)
        self.assertEqual(remaining["acknowledged_through"], 1)
        self.assertEqual([e["seq"] for e in remaining["events"]], [2])
        # idempotent older ack keeps the current cursor
        self.assertEqual(discovery.acknowledge(str(self.workspace.path), 0),
                         {"acknowledged_through": 1})

    def test_ack_beyond_tail_is_rejected(self):
        discovery.advance(str(self.workspace.path), 1)
        with self.assertRaises(ValueError):
            discovery.acknowledge(str(self.workspace.path), 1)
        with self.assertRaises(ValueError):
            discovery.acknowledge(str(self.workspace.path), 99)

    def test_ack_survives_reopen_and_does_not_change_science(self):
        apply_batch(str(self.workspace.archive), batch(
            "b2", 1, put_frames=[frame("b", 1, value=20, variance=1,
                                       observed_at=2)]))
        discovery.advance(str(self.workspace.path), 2)
        discovery.acknowledge(str(self.workspace.path), 2)
        with self.assertRaises(ValueError):
            discovery.acknowledge(str(self.workspace.path), 3)
        reopened = discovery.pending_events(str(self.workspace.path), 100)
        self.assertEqual(reopened, {"acknowledged_through": 2, "events": []})
        self.assertEqual(discovery.snapshot(str(self.workspace.path))["revision"],
                         2)

    def test_skipping_revisions_is_allowed_but_lower_new_ones_rejected(self):
        apply_batch(str(self.workspace.archive), batch(
            "b2", 1, put_frames=[frame("b", 1, value=20, variance=1,
                                       observed_at=2)]))
        apply_batch(str(self.workspace.archive), batch(
            "b3", 2, put_frames=[frame("b", 2, value=12, variance=1,
                                       observed_at=2)]))
        # jump straight from nothing to revision 3
        receipt = discovery.advance(str(self.workspace.path), 3)
        self.assertEqual(receipt["revision"], 3)
        # a never-processed lower revision is now rejected
        with self.assertRaisesRegex(ValueError, "older than processed"):
            discovery.advance(str(self.workspace.path), 2)
        # unknown future revisions are rejected too
        with self.assertRaises(ValueError):
            discovery.advance(str(self.workspace.path), 99)

    def test_retry_of_processed_revision_keeps_receipt_and_current_snapshot(self):
        apply_batch(str(self.workspace.archive), batch(
            "b2", 1, put_frames=[frame("b", 1, value=20, variance=1,
                                       observed_at=2)]))
        first = discovery.advance(str(self.workspace.path), 1)
        second_receipt = discovery.advance(str(self.workspace.path), 2)
        # retry revision 1 after moving forward: original receipt, no new events
        retry = discovery.advance(str(self.workspace.path), 1)
        self.assertEqual(retry, first)
        self.assertNotEqual(retry, second_receipt)
        self.assertEqual(discovery.snapshot(str(self.workspace.path))["revision"],
                         2)
        self.assertEqual(
            len(discovery.pending_events(str(self.workspace.path), 100)
                ["events"]), 2)
        # acknowledging first does not change the retry behavior
        discovery.acknowledge(str(self.workspace.path), 2)
        self.assertEqual(discovery.advance(str(self.workspace.path), 2),
                         second_receipt)


class EventPayloadTests(unittest.TestCase):
    def setUp(self):
        self.workspace = TempWorkspace()
        self.addCleanup(self.workspace.close)
        simple_archive(self.workspace.archive)
        self.workspace.initialize()

    def test_discover_revise_retract_lifecycle_and_payload_identity(self):
        apply_batch(str(self.workspace.archive), batch(
            "b2", 1, put_frames=[frame("b", 1, value=20, variance=1,
                                       observed_at=2)]))
        discovery.advance(str(self.workspace.path), 2)
        # revise: change b's corrected value again, keeping the alert identity.
        # c's payload is unchanged, so it must not emit a second event.
        apply_batch(str(self.workspace.archive), batch(
            "b3", 2, put_frames=[frame("b", 2, value=30, variance=1,
                                       observed_at=2)]))
        discovery.advance(str(self.workspace.path), 3)
        apply_batch(str(self.workspace.archive), batch(
            "b4", 3, put_frames=[frame("b", 3, value=12, variance=1,
                                       observed_at=2)]))
        discovery.advance(str(self.workspace.path), 4)
        events = discovery.pending_events(str(self.workspace.path), 100)["events"]
        self.assertEqual([(e["frame_id"], e["aperture_id"]) for e in events],
                         [("b", "a"), ("c", "a"), ("b", "a"),
                          ("b", "a"), ("c", "a")])
        self.assertEqual([e["kind"] for e in events],
                         ["discover", "discover", "revise",
                          "retract", "retract"])
        discover, _disc, revise, retract_b, retract_c = events
        self.assertIsNone(discover["before"])
        self.assertEqual(discover["after"]["delta"], "10")
        self.assertEqual(discover["after"]["reference_members"][0]["frame_id"],
                         "a")
        self.assertEqual(revise["before"]["delta"], "10")
        self.assertEqual(revise["after"]["delta"], "20")
        # the logical identity excludes revision and frame version
        self.assertEqual(revise["frame_id"], "b")
        self.assertEqual(revise["after"]["frame"]["frame_version"], 3)
        self.assertIsNone(retract_b["after"])
        self.assertEqual(retract_b["before"]["delta"], "20")
        for event in events:
            expected_revision = (2 if event["seq"] <= 2 else
                                 (3 if event["seq"] == 3 else 4))
            self.assertEqual(event["revision"], expected_revision)

    def test_unrelated_archive_change_emits_no_events(self):
        discovery.advance(str(self.workspace.path), 1)
        # a new release version with identical calibration changes nothing
        apply_batch(str(self.workspace.archive), batch(
            "b2", 1, add_releases=[release("r2", 2)]))
        receipt = discovery.advance(str(self.workspace.path), 2)
        self.assertEqual(receipt, {"dataset_id": "ds", "revision": 2,
                                   "first_seq": None, "last_seq": None})
        self.assertEqual(
            discovery.pending_events(str(self.workspace.path), 100)["events"],
            [])

    def test_frame_withdrawal_retracts_discovery_and_replays_followers(self):
        apply_batch(str(self.workspace.archive), batch(
            "b2", 1, put_frames=[frame("b", 1, value=20, variance=1,
                                       observed_at=2)]))
        discovery.advance(str(self.workspace.path), 2)
        apply_batch(str(self.workspace.archive), batch(
            "b3", 2, withdraw_frames=[{"id": "b", "expected_version": 2}]))
        discovery.advance(str(self.workspace.path), 3)
        events = discovery.pending_events(str(self.workspace.path), 100)["events"]
        self.assertEqual([(e["kind"], e["frame_id"]) for e in events[2:]],
                         [("retract", "b")])
        snap = discovery.snapshot(str(self.workspace.path))
        self.assertEqual(
            [row["status"] for row in snap["excluded_frames"]], ["withdrawn"])
        statuses = {o["frame"]["frame_id"]: o["status"]
                    for o in snap["observations"]}
        self.assertEqual(statuses, {"a": "bootstrap", "c": "triggered"})
        # the triggered frame never enters the reference: only a remains
        self.assertEqual(
            [m["frame_id"] for m in
             snap["final_reference"]["members"]], ["a"])
        self.assertEqual([(a["frame"]["frame_id"], a["aperture_id"])
                          for a in snap["active_alerts"]], [("c", "a")])


class TimeCorrectionReplayTests(unittest.TestCase):
    def test_observed_time_correction_reorders_processing_and_findings(self):
        workspace = TempWorkspace()
        self.addCleanup(workspace.close)
        simple_archive(workspace.archive)
        workspace.initialize()
        discovery.advance(str(workspace.path), 1)
        # move b earlier than a: processing order becomes b, a, c and the
        # bootstrap/reference decisions must be fully replayed
        apply_batch(str(workspace.archive), batch(
            "b2", 1, put_frames=[frame("b", 1, value=12, variance=1,
                                       observed_at=0)]))
        discovery.advance(str(workspace.path), 2)
        snap = discovery.snapshot(str(workspace.path))
        self.assertEqual([o["frame"]["frame_id"] for o in snap["observations"]],
                         ["b", "a", "c"])
        self.assertEqual(snap["observations"][0]["status"], "bootstrap")
        self.assertEqual(snap["observations"][0]["frame"]["frame_version"], 2)

    def test_calibration_release_addition_and_retraction_replay_findings(self):
        workspace = TempWorkspace()
        self.addCleanup(workspace.close)
        initialize(str(workspace.archive), classic_definition())
        apply_batch(str(workspace.archive), batch("b1", 0,
            add_darks=[dark("d0", value=0, variance=0),
                       dark("d1", value=8, variance=0)],
            add_flats=[flat()],
            add_releases=[release("r1", 1, dark_id="d0")],
            put_frames=[frame("a", 0, value=10, variance=1, observed_at=1),
                        frame("b", 0, value=12, variance=1, observed_at=2)]))
        workspace.initialize(config=base_config(sigma_squared="2"))
        discovery.advance(str(workspace.path), 1)
        # higher-version release rebinds both frames to a dark subtracting 8:
        # rates become 2 and 4, so b now triggers against a
        apply_batch(str(workspace.archive), batch(
            "b2", 1, add_releases=[release("r2", 2, dark_id="d1")]))
        discovery.advance(str(workspace.path), 2)
        snap = discovery.snapshot(str(workspace.path))
        self.assertEqual(
            snap["observations"][1]["frame"]["release_id"], "r2")
        self.assertEqual(
            snap["observations"][1]["measurements"][0]["delta"], "2")
        self.assertTrue(
            snap["observations"][1]["measurements"][0]["triggered"])
        # the alert already existed at rev 1; the calibration rebinding changes
        # its lineage payload, so this advance is a revise, never a duplicate
        events = discovery.pending_events(str(workspace.path), 100)["events"]
        self.assertEqual([(e["kind"], e["frame_id"]) for e in events],
                         [("discover", "b"), ("revise", "b")])
        self.assertEqual(events[1]["before"]["frame"]["release_id"], "r1")
        self.assertEqual(events[1]["after"]["frame"]["release_id"], "r2")
        # retract the high version: no fallback to r1 for selection, the frame
        # is blocked and the discovery is retracted
        apply_batch(str(workspace.archive),
                    batch("b3", 2, retract_releases=["r2"]))
        discovery.advance(str(workspace.path), 3)
        events = discovery.pending_events(str(workspace.path), 100)["events"]
        self.assertEqual([(e["kind"], e["frame_id"]) for e in events],
                         [("discover", "b"), ("revise", "b"),
                          ("retract", "b")])
        snap = discovery.snapshot(str(workspace.path))
        # retracting the highest covering release blocks both frames without
        # falling back to r1, so the time series has no ready observations
        self.assertEqual(
            {row["status"] for row in snap["excluded_frames"]},
            {"retracted_release"})
        self.assertEqual(snap["observations"], [])
        self.assertEqual(snap["active_alerts"], [])
        self.assertEqual(snap["final_reference"]["members"], [])


class SnapshotContractTests(unittest.TestCase):
    def test_snapshot_requires_an_advance_and_never_computes_implicitly(self):
        workspace = TempWorkspace()
        self.addCleanup(workspace.close)
        simple_archive(workspace.archive)
        workspace.initialize()
        with self.assertRaises(ValueError):
            discovery.snapshot(str(workspace.path))

    def test_report_shape_and_ordering_contract(self):
        workspace = TempWorkspace()
        self.addCleanup(workspace.close)
        simple_archive(workspace.archive)
        workspace.initialize()
        discovery.advance(str(workspace.path), 1)
        report = discovery.snapshot(str(workspace.path))
        self.assertEqual(set(report), {"schema_version", "dataset_id",
                                       "revision", "config", "excluded_frames",
                                       "observations", "final_reference",
                                       "active_alerts", "summary"})
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(set(report["config"]), set(base_config()))
        self.assertEqual(report["config"]["aperture_ids"], ["a"])
        for observation in report["observations"]:
            self.assertEqual(set(observation), {"frame", "status",
                                                "reference_before",
                                                "measurements"})
            self.assertEqual(set(observation["frame"]),
                             {"frame_id", "frame_version", "observed_at_s",
                              "release_id", "dark_id", "flat_id"})
            self.assertEqual(set(observation["reference_before"]),
                             {"members", "stack", "uncertainty"})
        product = report["final_reference"]
        self.assertEqual(set(product["uncertainty"]),
                         {"variance_image", "aperture_order",
                          "aperture_covariance"})
        counts = report["summary"]
        self.assertEqual(counts["ready_frames"],
                         counts["ineligible_frames"]
                         + counts["bootstrap_frames"]
                         + counts["quiet_frames"]
                         + counts["triggered_frames"])
        self.assertEqual(counts["reference_frames"],
                         len(product["members"]))


class EmptyArchiveAndCorruptionTests(unittest.TestCase):
    def test_empty_archive_advance_is_a_valid_empty_snapshot(self):
        workspace = TempWorkspace()
        self.addCleanup(workspace.close)
        initialize(str(workspace.archive), classic_definition())
        workspace.initialize()
        receipt = discovery.advance(str(workspace.path), 0)
        self.assertEqual(receipt, {"dataset_id": "ds", "revision": 0,
                                   "first_seq": None, "last_seq": None})
        snap = discovery.snapshot(str(workspace.path))
        self.assertEqual(snap["observations"], [])
        self.assertEqual(snap["excluded_frames"], [])
        self.assertEqual(snap["active_alerts"], [])
        self.assertEqual(snap["final_reference"]["members"], [])
        self.assertEqual(snap["final_reference"]["stack"]["rate_image"],
                         [[None]])
        self.assertEqual(snap["summary"], {
            "ready_frames": 0, "ineligible_frames": 0,
            "bootstrap_frames": 0, "quiet_frames": 0,
            "triggered_frames": 0, "active_alerts": 0,
            "reference_frames": 0})

    def test_corrupt_state_is_reported_not_rebuilt(self):
        workspace = TempWorkspace()
        self.addCleanup(workspace.close)
        simple_archive(workspace.archive)
        workspace.initialize()
        (workspace.path / "state.json").write_text("{broken")
        with self.assertRaises(WorkspaceError):
            discovery.snapshot(str(workspace.path))
        with self.assertRaises(WorkspaceError):
            discovery.pending_events(str(workspace.path), 10)

    def test_missing_envelope_for_processed_revision_is_corruption(self):
        workspace = TempWorkspace()
        self.addCleanup(workspace.close)
        simple_archive(workspace.archive)
        workspace.initialize()
        discovery.advance(str(workspace.path), 1)
        envelopes = list((workspace.path / "advances").glob("*.json"))
        envelopes[0].unlink()
        with self.assertRaises(WorkspaceError):
            discovery.snapshot(str(workspace.path))


if __name__ == "__main__":
    unittest.main()
