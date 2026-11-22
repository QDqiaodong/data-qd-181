"""Discovery science: exact signals, reference products and trigger decisions."""
import unittest
from fractions import Fraction

from astro_stack.archive import apply_batch, initialize
from astro_stack import discovery

from archive_helpers import batch, classic_definition, dark, flat, frame, release
from discovery_helpers import TempWorkspace, base_config


def px_frame(frame_id, expected_version, pixels, variance, observed_at,
             exposure=1, dx=0, dy=0, saturation=100):
    return {"id": frame_id, "exposure_s": exposure, "dx": dx, "dy": dy,
            "saturation": saturation, "pixels": pixels,
            "observed_at_s": observed_at, "variance": variance,
            "expected_version": expected_version}


class ScenarioOneCorrectionReplayTests(unittest.TestCase):
    """Acceptance 1: correcting an early frame replays later references/findings."""

    def _archive(self, root, b_value):
        definition = classic_definition()
        initialize(str(root), definition)
        apply_batch(str(root), batch("b1", 0,
            add_darks=[dark("d0", value=0, variance=0)],
            add_flats=[flat()],
            add_releases=[release("r1", 1)],
            put_frames=[frame("a", 0, value=10, variance=1, observed_at=1),
                        frame("b", 0, value=b_value, variance=1, observed_at=2),
                        frame("c", 0, value=14, variance=1, observed_at=3)]))

    def test_first_advance_bootstrap_quiet_and_single_member_reference(self):
        with TempWorkspace() as workspace:
            self._archive(workspace.archive, 12)
            workspace.initialize()
            receipt = discovery.advance(str(workspace.path), 1)
            self.assertEqual(receipt, {"dataset_id": "ds", "revision": 1,
                                       "first_seq": None, "last_seq": None})
            snap = discovery.snapshot(str(workspace.path))
            statuses = [(o["frame"]["frame_id"], o["status"])
                        for o in snap["observations"]]
            self.assertEqual(statuses, [("a", "bootstrap"), ("b", "quiet"),
                                        ("c", "quiet")])
            b, c = snap["observations"][1:]
            self.assertEqual(b["measurements"], [{
                "aperture_id": "a", "delta": "2", "variance": "2",
                "triggered": False}])
            self.assertEqual(c["measurements"], [{
                "aperture_id": "a", "delta": "2", "variance": "2",
                "triggered": False}])
            # historical reference products, never the final one in disguise
            self.assertEqual(
                [m["frame_id"] for m in
                 snap["observations"][0]["reference_before"]["members"]], [])
            self.assertEqual(
                [m["frame_id"] for m in
                 snap["observations"][1]["reference_before"]["members"]], ["a"])
            self.assertEqual(
                [m["frame_id"] for m in
                 snap["observations"][2]["reference_before"]["members"]], ["b"])
            final = snap["final_reference"]
            self.assertEqual([m["frame_id"] for m in final["members"]], ["c"])
            self.assertEqual(final["stack"]["rate_image"], [["14"]])
            self.assertEqual(final["stack"]["exposure_s"], [[1]])
            self.assertEqual(final["stack"]["contributor_count"], [[1]])
            self.assertEqual(snap["active_alerts"], [])
            self.assertEqual(snap["summary"], {
                "ready_frames": 3, "ineligible_frames": 0,
                "bootstrap_frames": 1, "quiet_frames": 2,
                "triggered_frames": 0, "active_alerts": 0,
                "reference_frames": 1})

    def test_correction_triggers_b_and_c_and_repairs_on_restore(self):
        with TempWorkspace() as workspace:
            self._archive(workspace.archive, 12)
            workspace.initialize()
            discovery.advance(str(workspace.path), 1)
            apply_batch(str(workspace.archive), batch(
                "b2", 1, put_frames=[frame("b", 1, value=20, variance=1,
                                           observed_at=2)]))
            receipt = discovery.advance(str(workspace.path), 2)
            self.assertEqual(receipt, {"dataset_id": "ds", "revision": 2,
                                       "first_seq": 1, "last_seq": 2})
            snap = discovery.snapshot(str(workspace.path))
            self.assertEqual([o["status"] for o in snap["observations"]],
                             ["bootstrap", "triggered", "triggered"])
            corrected_b = snap["observations"][1]
            self.assertEqual(corrected_b["measurements"][0]["delta"], "10")
            self.assertEqual(corrected_b["measurements"][0]["variance"], "2")
            c = snap["observations"][2]
            self.assertEqual(c["measurements"][0]["delta"], "4")
            self.assertEqual(c["measurements"][0]["variance"], "2")
            # b was rejected: c compares against a and only a stays
            self.assertEqual(
                [m["frame_id"] for m in
                 c["reference_before"]["members"]], ["a"])
            self.assertEqual(
                [m["frame_id"] for m in
                 snap["final_reference"]["members"]], ["a"])
            self.assertEqual(snap["final_reference"]["stack"]["rate_image"],
                             [["10"]])
            alerts = {(a["frame"]["frame_id"], a["aperture_id"]): a
                      for a in snap["active_alerts"]}
            self.assertEqual(set(alerts), {("b", "a"), ("c", "a")})
            self.assertEqual(
                [m["frame_id"] for m in alerts[("b", "a")]["reference_members"]],
                ["a"])
            # restore b=12: both discoveries retract even though unacknowledged
            apply_batch(str(workspace.archive), batch(
                "b3", 2, put_frames=[frame("b", 2, value=12, variance=1,
                                           observed_at=2)]))
            receipt = discovery.advance(str(workspace.path), 3)
            self.assertEqual(receipt, {"dataset_id": "ds", "revision": 3,
                                       "first_seq": 3, "last_seq": 4})
            pending = discovery.pending_events(str(workspace.path), 100)
            self.assertEqual([e["seq"] for e in pending["events"]],
                             [1, 2, 3, 4])
            self.assertEqual([e["kind"] for e in pending["events"]],
                             ["discover", "discover", "retract", "retract"])
            self.assertTrue(all(e["before"] is None
                                for e in pending["events"][:2]))
            self.assertTrue(all(e["after"] is None
                                for e in pending["events"][2:]))
            restored = discovery.snapshot(str(workspace.path))
            self.assertEqual(
                [m["frame_id"] for m in
                 restored["final_reference"]["members"]], ["c"])
            self.assertEqual(restored["final_reference"]["stack"]["rate_image"],
                             [["14"]])
            self.assertEqual(restored["active_alerts"], [])
            # exact retry of an old revision keeps its receipt and snapshot
            retry = discovery.advance(str(workspace.path), 1)
            self.assertEqual(retry, {"dataset_id": "ds", "revision": 1,
                                     "first_seq": None, "last_seq": None})
            self.assertEqual(discovery.snapshot(str(workspace.path))["revision"],
                             3)
            self.assertEqual(
                len(discovery.pending_events(str(workspace.path), 100)["events"]),
                4)


class SharedDarkCorrelationTests(unittest.TestCase):
    """Acceptance 2: shared dark sources merge before variance propagation."""

    def _build(self, tmp, second_dark_id):
        darks = [dark("d0", value=0, variance=100)]
        if second_dark_id:
            darks.append(dark(second_dark_id, value=0, variance=100))
        initialize(str(tmp.archive), classic_definition())
        second_release = release("r2", 2,
                                 dark_id=second_dark_id or "d0",
                                 start=10, end=100)
        apply_batch(str(tmp.archive), batch("b1", 0,
            add_darks=darks, add_flats=[flat()],
            add_releases=[release("r1", 1, start=0, end=10), second_release],
            put_frames=[frame("a", 0, value=0, variance=1, observed_at=5),
                        frame("b", 0, value=10, variance=1, observed_at=20)]))
        tmp.initialize()
        discovery.advance(str(tmp.path), 1)
        return discovery.snapshot(str(tmp.path))

    def test_shared_dark_cancels_to_variance_two_and_triggers(self):
        with TempWorkspace() as workspace:
            snap = self._build(workspace, None)
            measurement = snap["observations"][1]["measurements"][0]
            self.assertEqual(measurement["variance"], "2")
            self.assertEqual(measurement["delta"], "10")
            self.assertTrue(measurement["triggered"])
            self.assertEqual(snap["observations"][1]["status"], "triggered")

    def test_equivalent_distinct_dark_does_not_cancel_and_stays_quiet(self):
        with TempWorkspace() as workspace:
            snap = self._build(workspace, "d1")
            measurement = snap["observations"][1]["measurements"][0]
            self.assertEqual(measurement["variance"], "202")
            self.assertFalse(measurement["triggered"])
            self.assertEqual(snap["observations"][1]["status"], "quiet")
            # the quiet frame is admitted; with capacity 1 it evicts the
            # bootstrap member rather than corrupting the reference
            self.assertEqual(
                [m["frame_id"] for m in
                 snap["final_reference"]["members"]], ["b"])

    def test_distinct_releases_sharing_dark_id_still_cancel(self):
        with TempWorkspace() as workspace:
            initialize(str(workspace.archive), classic_definition())
            apply_batch(str(workspace.archive), batch("b1", 0,
                add_darks=[dark("d0", value=0, variance=100)],
                add_flats=[flat("f0"), flat("f1")],
                add_releases=[
                    release("r1", 1, dark_id="d0", flat_id="f0",
                            start=0, end=10),
                    release("r2", 2, dark_id="d0", flat_id="f1",
                            start=10, end=100)],
                put_frames=[frame("a", 0, value=0, variance=1, observed_at=5),
                            frame("b", 0, value=10, variance=1,
                                  observed_at=20)]))
            workspace.initialize()
            discovery.advance(str(workspace.path), 1)
            snap = discovery.snapshot(str(workspace.path))
            self.assertEqual(
                snap["observations"][1]["measurements"][0]["variance"], "2")

    def test_different_flat_coefficients_only_partly_cancel(self):
        definition = classic_definition(width=2, height=1, apertures=[
            {"id": "a", "x": 1, "y": 0, "radius": 0}])
        with TempWorkspace() as workspace:
            initialize(str(workspace.archive), definition)
            apply_batch(str(workspace.archive), batch("b1", 0,
                add_darks=[{
                    "id": "d0", "dark_exposure_s": 1,
                    "dark": [[0, 0]], "dark_variance": [[100, 100]]}],
                add_flats=[
                    flat("f0", values=[[2, 4]]),
                    flat("f1", values=[[1, 1]])],
                add_releases=[
                    release("r1", 1, dark_id="d0", flat_id="f0",
                            start=0, end=10),
                    release("r2", 2, dark_id="d0", flat_id="f1",
                            start=10, end=100)],
                put_frames=[
                    px_frame("a", 0, [[0, 0]], [[1, 1]], observed_at=5),
                    px_frame("b", 0, [[0, 0]], [[1, 1]], observed_at=20)]))
            config = base_config(aperture_ids=["a"], min_abs_delta="1000")
            workspace.initialize(config=config)
            discovery.advance(str(workspace.path), 1)
            snap = discovery.snapshot(str(workspace.path))
            measurement = snap["observations"][1]["measurements"][0]
            # both frames read detector (d0, x=1) at output 1:
            # frame a coefficient M0/flat = 3/4, frame b coefficient = 1;
            # dark 100*(1 - 3/4)^2 = 25/4; raw 1 + (3/4)^2 = 25/16
            self.assertEqual(Fraction(measurement["variance"]),
                             Fraction(125, 16))

    def test_shift_keeps_detector_dark_identity(self):
        definition = classic_definition(width=2, height=1, apertures=[
            {"id": "a", "x": 1, "y": 0, "radius": 0}])
        with TempWorkspace() as workspace:
            initialize(str(workspace.archive), definition)
            apply_batch(str(workspace.archive), batch("b1", 0,
                add_darks=[{
                    "id": "d0", "dark_exposure_s": 1,
                    "dark": [[0, 0]], "dark_variance": [[100, 100]]}],
                add_flats=[flat(values=[[1, 1]])],
                add_releases=[release("r1", 1)],
                put_frames=[
                    px_frame("a", 0, [[0, 0]], [[1, 1]], observed_at=5,
                             dx=1),
                    px_frame("b", 0, [[0, 0]], [[1, 1]], observed_at=6,
                             dx=1)]))
            workspace.initialize()
            discovery.advance(str(workspace.path), 1)
            snap = discovery.snapshot(str(workspace.path))
            # identical shifts map detector x=0 of the same dark onto the
            # aperture's output pixel 1 in both frames, so it still cancels
            self.assertEqual(
                snap["observations"][1]["measurements"][0]["variance"], "2")

    def test_multi_pixel_aperture_correlation_is_not_a_naive_sum(self):
        # a radius-1 aperture integrates 5 output pixels; every pixel shares
        # the same dark id, so dark coefficients merge across pixels before
        # propagation and the aperture variance is not a per-pixel blind sum
        definition = classic_definition(width=3, height=3, apertures=[
            {"id": "big", "x": 1, "y": 1, "radius": 1}])
        ones = [[1, 1, 1], [1, 1, 1], [1, 1, 1]]
        with TempWorkspace() as workspace:
            initialize(str(workspace.archive), definition)
            apply_batch(str(workspace.archive), batch("b1", 0,
                add_darks=[dark("d0", value=0, variance=9, width=3, height=3)],
                add_flats=[flat(values=[[1] * 3 for _ in range(3)])],
                add_releases=[release("r1", 1, start=0, end=100)],
                put_frames=[
                    px_frame("ref", 0, [[0] * 3 for _ in range(3)],
                             [row[:] for row in ones], observed_at=1),
                    px_frame("cand", 0, [[0] * 3 for _ in range(3)],
                             [row[:] for row in ones], observed_at=20)]))
            workspace.initialize(config=base_config(aperture_ids=["big"],
                                                     min_abs_delta="1000"))
            discovery.advance(str(workspace.path), 1)
            measurement = discovery.snapshot(str(workspace.path)) \
                ["observations"][1]["measurements"][0]
            # five pixels, each with frame raw variance 1 and reference raw
            # variance 1 (single bootstrap member) -> raw term 10; the shared
            # dark (same id at every detector pixel) cancels per pixel because
            # both frames use identical coefficients, leaving no dark term
            self.assertEqual(Fraction(measurement["variance"]),
                             Fraction(10))
            # now cand moves onto a distinct dark id: none of its five pixels
            # can merge with the reference, so each adds 9 + 9 dark variance
            # on top of the unchanged raw term 10
            apply_batch(str(workspace.archive), batch("b2", 1,
                add_darks=[dark("d1", value=0, variance=9, width=3, height=3)],
                add_releases=[release("r2", 2, dark_id="d1", start=10,
                                      end=100)]))
            discovery.advance(str(workspace.path), 2)
            changed = discovery.snapshot(str(workspace.path)) \
                ["observations"][1]["measurements"][0]
            self.assertEqual(Fraction(changed["variance"]),
                             Fraction(10) + Fraction(90))


class EligibilityAndThresholdTests(unittest.TestCase):
    """Acceptance 3: gating, disks, signs and exact threshold equality."""

    def _two_aperture_definition(self):
        return classic_definition(width=2, height=2, apertures=[
            {"id": "a0", "x": 0, "y": 0, "radius": 0},
            {"id": "a1", "x": 1, "y": 1, "radius": 0}])

    def _workspace(self, definition=None, config=None):
        workspace = TempWorkspace()
        self.addCleanup(workspace.close)
        initialize(str(workspace.archive),
                   definition or self._two_aperture_definition())
        return workspace

    def test_capacity_above_one_evicts_oldest_member(self):
        workspace = self._workspace(config=None)
        apply_batch(str(workspace.archive), batch("b1", 0,
            add_darks=[dark("d0", width=2, height=2)],
            add_flats=[flat(values=[[1, 1], [1, 1]])],
            add_releases=[release("r1", 1)],
            put_frames=[
                px_frame(f"f{i}", 0, [[i + 1, 0], [0, 0]],
                         [[1, 1], [1, 1]], observed_at=i)
                for i in range(1, 5)]))
        workspace.initialize(config=base_config(
            aperture_ids=["a0", "a1"], bootstrap_frames=2,
            reference_capacity=3, min_abs_delta="1000"))
        discovery.advance(str(workspace.path), 1)
        snap = discovery.snapshot(str(workspace.path))
        self.assertEqual([o["status"] for o in snap["observations"]],
                         ["bootstrap", "bootstrap", "quiet", "quiet"])
        self.assertEqual(
            [m["frame_id"] for m in snap["final_reference"]["members"]],
            ["f2", "f3", "f4"])
        # exposure-weighted stack at output (0,0): surviving members
        # f2,f3,f4 have rates 3,4,5 with equal exposure -> mean 4
        self.assertEqual(snap["final_reference"]["stack"]["rate_image"][0][0],
                         "4")

    def test_reference_product_matches_canonical_archive_stack(self):
        # the reference is the admitted (post-eviction) members' raw
        # contributions combined with the archive's exact exposure-weighted
        # and uncertainty rules, so it must equal stack_generic over the same
        # surviving member subset with the same bindings
        from astro_stack.campaign import precompute_bound, stack_generic
        from astro_stack.archive.store import Store, replay_snapshot
        definition = classic_definition(width=2, height=1, apertures=[
            {"id": "a", "x": 0, "y": 0, "radius": 0},
            {"id": "z", "x": 1, "y": 0, "radius": 0}])
        workspace = self._workspace(definition=definition)
        apply_batch(str(workspace.archive), batch("b1", 0,
            add_darks=[{
                "id": "d0", "dark_exposure_s": 1,
                "dark": [[0, 0]], "dark_variance": [[1, 1]]}],
            add_flats=[flat(values=[[1, 1]])],
            add_releases=[release("r1", 1)],
            put_frames=[px_frame("f1", 0, [[5, 7]], [[1, 1]], observed_at=1),
                        px_frame("f2", 0, [[5, 7]], [[1, 1]], observed_at=2),
                        px_frame("f3", 0, [[5, 7]], [[1, 1]], observed_at=3)]))
        workspace.initialize(config=base_config(
            aperture_ids=["a", "z"], bootstrap_frames=1,
            reference_capacity=2, min_abs_delta="1000"))
        discovery.advance(str(workspace.path), 1)
        reference = discovery.snapshot(str(workspace.path))["final_reference"]
        # capacity 2 evicts f1; the surviving members are exactly f2 and f3
        survivors = ["f2", "f3"]
        self.assertEqual([m["frame_id"] for m in reference["members"]],
                         survivors)
        with Store(str(workspace.archive)) as store:
            snap = replay_snapshot(store.state, store.root, 1)
        from astro_stack.archive.science import select_bindings
        bindings = select_bindings(snap)
        frame_map = {fid: snap.frames[fid] for fid in survivors}
        bound = {fid: bindings[fid] for fid in survivors}
        masters = {"darks": snap.darks, "flats": snap.flats}
        precomp = precompute_bound(2, 1, frame_map, bound, masters)
        aperture_records = {r["id"]: r for r in snap.apertures}

        def variance_of(source):
            dark_id, x, y = source
            return snap.darks[dark_id]["dark_variance"][y][x]

        def source_of(contribution):
            return contribution[-1], contribution[2], contribution[3]

        stack, variance_image, aperture_uncertainty = stack_generic(
            frame_map, precomp, 2, 1, aperture_records,
            sorted(aperture_records), variance_of, source_of)
        self.assertEqual(reference["stack"]["rate_image"],
                         stack["rate_image"])
        self.assertEqual(reference["stack"]["exposure_s"],
                         stack["exposure_s"])
        self.assertEqual(reference["stack"]["contributor_count"],
                         stack["contributor_count"])
        self.assertEqual(reference["stack"]["frame_audit"],
                         stack["frame_audit"])
        self.assertEqual(reference["uncertainty"]["variance_image"],
                         [[str(v) if v is not None else None
                           for v in row] for row in variance_image])

    def test_bootstrap_shortage_measures_nothing(self):
        workspace = self._workspace()
        apply_batch(str(workspace.archive), batch("b1", 0,
            add_darks=[dark("d0", width=2, height=2)],
            add_flats=[flat(values=[[1, 1], [1, 1]])],
            add_releases=[release("r1", 1)],
            put_frames=[px_frame("f1", 0, [[1, 1], [1, 1]],
                                 [[1, 1], [1, 1]], observed_at=1),
                        px_frame("f2", 0, [[1, 1], [1, 1]],
                                 [[1, 1], [1, 1]], observed_at=2)]))
        workspace.initialize(config=base_config(
            aperture_ids=["a0", "a1"], bootstrap_frames=2,
            reference_capacity=2))
        discovery.advance(str(workspace.path), 1)
        snap = discovery.snapshot(str(workspace.path) )
        self.assertEqual([o["status"] for o in snap["observations"]],
                         ["bootstrap", "bootstrap"])
        self.assertEqual(
            [m["frame_id"] for m in snap["final_reference"]["members"]],
            ["f1", "f2"])

    def test_same_observed_time_breaks_ties_by_frame_id(self):
        workspace = self._workspace()
        apply_batch(str(workspace.archive), batch("b1", 0,
            add_darks=[dark("d0", width=2, height=2)],
            add_flats=[flat(values=[[1, 1], [1, 1]])],
            add_releases=[release("r1", 1)],
            put_frames=[px_frame("z", 0, [[1, 1], [1, 1]],
                                 [[1, 1], [1, 1]], observed_at=7),
                        px_frame("a", 0, [[1, 1], [1, 1]],
                                 [[1, 1], [1, 1]], observed_at=7)]))
        workspace.initialize(config=base_config(
            aperture_ids=["a0", "a1"], bootstrap_frames=1,
            reference_capacity=1, min_abs_delta="1000"))
        discovery.advance(str(workspace.path), 1)
        snap = discovery.snapshot(str(workspace.path) )
        self.assertEqual([o["frame"]["frame_id"] for o in snap["observations"]],
                         ["a", "z"])

    def test_every_configured_aperture_must_have_full_disk_coverage(self):
        workspace = self._workspace()
        apply_batch(str(workspace.archive), batch("b1", 0,
            add_darks=[dark("d0", width=2, height=2)],
            add_flats=[flat(values=[[1, 1], [1, 1]])],
            add_releases=[release("r1", 1)],
            put_frames=[
                px_frame("good", 0, [[1, 1], [1, 1]], [[1, 1], [1, 1]],
                         observed_at=1),
                px_frame("gappy", 0, [[1, 1], [1, 0]], [[1, 1], [1, 1]],
                         observed_at=2, saturation=1)]))
        workspace.initialize(config=base_config(
            aperture_ids=["a0", "a1"], bootstrap_frames=1,
            reference_capacity=2, min_abs_delta="1000"))
        discovery.advance(str(workspace.path), 1)
        snap = discovery.snapshot(str(workspace.path) )
        statuses = {o["frame"]["frame_id"]: o["status"]
                    for o in snap["observations"]}
        self.assertEqual(statuses, {"good": "bootstrap", "gappy": "ineligible"})
        self.assertEqual(
            [m["frame_id"] for m in snap["final_reference"]["members"]],
            ["good"])

    def test_local_exposure_shortage_is_ineligible(self):
        workspace = self._workspace()
        apply_batch(str(workspace.archive), batch("b1", 0,
            add_darks=[dark("d0", width=2, height=2)],
            add_flats=[flat(values=[[1, 1], [1, 1]])],
            add_releases=[release("r1", 1)],
            put_frames=[px_frame("short", 0, [[1, 1], [1, 1]],
                                 [[1, 1], [1, 1]], observed_at=1,
                                 exposure=1)]))
        workspace.initialize(config=base_config(
            aperture_ids=["a0", "a1"], min_exposure_s=2))
        discovery.advance(str(workspace.path), 1)
        self.assertEqual(discovery.snapshot(str(workspace.path))
                         ["observations"][0]["status"], "ineligible")

    def test_mask_zero_flat_and_outside_pixels_make_ineligible(self):
        cases = [
            ("mask", flat("f0", values=[[1, 1], [1, 1]],
                          mask=[[False, False], [False, True]])),
            ("zeroflat", flat("f0", values=[[1, 1], [1, 0]])),
        ]
        for name, flat_record in cases:
            with self.subTest(name=name):
                workspace = self._workspace()
                apply_batch(str(workspace.archive), batch("b1", 0,
                    add_darks=[dark("d0", width=2, height=2)],
                    add_flats=[flat_record],
                    add_releases=[release("r1", 1)],
                    put_frames=[px_frame("f", 0, [[1, 1], [1, 1]],
                                         [[1, 1], [1, 1]], observed_at=1)]))
                workspace.initialize(config=base_config(
                    aperture_ids=["a0", "a1"]))
                discovery.advance(str(workspace.path), 1)
                self.assertEqual(discovery.snapshot(str(workspace.path))
                                 ["observations"][0]["status"], "ineligible")
        # shifted outside the frame
        workspace = self._workspace()
        apply_batch(str(workspace.archive), batch("b1", 0,
            add_darks=[dark("d0", width=2, height=2)],
            add_flats=[flat(values=[[1, 1], [1, 1]])],
            add_releases=[release("r1", 1)],
            put_frames=[px_frame("shifted", 0, [[1, 1], [1, 1]],
                                 [[1, 1], [1, 1]], observed_at=1, dx=1)]))
        workspace.initialize(config=base_config(aperture_ids=["a0", "a1"]))
        discovery.advance(str(workspace.path), 1)
        self.assertEqual(discovery.snapshot(str(workspace.path))
                         ["observations"][0]["status"], "ineligible")

    def test_edge_aperture_extending_outside_blocks_eligibility(self):
        definition = classic_definition(width=2, height=2, apertures=[
            {"id": "edge", "x": 0, "y": 0, "radius": 1}])
        workspace = self._workspace(definition=definition)
        apply_batch(str(workspace.archive), batch("b1", 0,
            add_darks=[dark("d0", width=2, height=2)],
            add_flats=[flat(values=[[1, 1], [1, 1]])],
            add_releases=[release("r1", 1)],
            put_frames=[px_frame("f", 0, [[1, 1], [1, 1]],
                                 [[1, 1], [1, 1]], observed_at=1)]))
        workspace.initialize(config=base_config(aperture_ids=["edge"]))
        discovery.advance(str(workspace.path), 1)
        self.assertEqual(discovery.snapshot(str(workspace.path))
                         ["observations"][0]["status"], "ineligible")

    def test_negative_signal_and_negative_delta_trigger(self):
        workspace = self._workspace()
        apply_batch(str(workspace.archive), batch("b1", 0,
            add_darks=[dark("d0", value=30, width=2, height=2)],
            add_flats=[flat(values=[[1, 1], [1, 1]])],
            add_releases=[release("r1", 1)],
            put_frames=[
                px_frame("ref", 0, [[30, 1], [1, 1]], [[1, 1], [1, 1]],
                         observed_at=1),
                px_frame("dim", 0, [[10, 1], [1, 1]], [[1, 1], [1, 1]],
                         observed_at=2)]))
        workspace.initialize(config=base_config(
            aperture_ids=["a0"], min_abs_delta="5", sigma_squared="1"))
        discovery.advance(str(workspace.path), 1)
        snap = discovery.snapshot(str(workspace.path) )
        measurement = snap["observations"][1]["measurements"][0]
        # rate ref = 0, candidate = -20; negative delta still triggers on abs
        self.assertEqual(measurement["delta"], "-20")
        self.assertTrue(measurement["triggered"])
        self.assertEqual(snap["observations"][1]["status"], "triggered")

    def test_zero_noise_threshold_equality_and_zero_delta(self):
        workspace = self._workspace()
        apply_batch(str(workspace.archive), batch("b1", 0,
            add_darks=[dark("d0", value=0, variance=0, width=2, height=2)],
            add_flats=[flat(values=[[1, 1], [1, 1]])],
            add_releases=[release("r1", 1)],
            put_frames=[
                px_frame("ref", 0, [[10, 0], [0, 0]], [[0, 0], [0, 0]],
                         observed_at=1),
                px_frame("equal", 0, [[13, 0], [0, 0]], [[0, 0], [0, 0]],
                         observed_at=2),
                px_frame("zero", 0, [[10, 0], [0, 0]], [[0, 0], [0, 0]],
                         observed_at=3)]))
        workspace.initialize(config=base_config(
            aperture_ids=["a0"], min_abs_delta="3", sigma_squared="4"))
        discovery.advance(str(workspace.path), 1)
        snap = discovery.snapshot(str(workspace.path) )
        equal, zero = snap["observations"][1:]
        self.assertEqual(equal["measurements"][0]["variance"], "0")
        # equality on both thresholds, zero variance: still triggers
        self.assertEqual(equal["measurements"][0]["delta"], "3")
        self.assertTrue(equal["measurements"][0]["triggered"])
        # zero delta can never trigger while min_abs_delta is strictly positive
        self.assertEqual(zero["measurements"][0]["delta"], "0")
        self.assertFalse(zero["measurements"][0]["triggered"])

    def test_sigma_equality_triggers_with_nonzero_variance(self):
        workspace = TempWorkspace()
        self.addCleanup(workspace.close)
        initialize(str(workspace.archive), classic_definition())
        apply_batch(str(workspace.archive), batch("b1", 0,
            add_darks=[dark("d0", value=0, variance=0)],
            add_flats=[flat()],
            add_releases=[release("r1", 1)],
            put_frames=[frame("ref", 0, value=0, variance=2, observed_at=1),
                        frame("cand", 0, value=4, variance=2,
                              observed_at=2)]))
        # variance = 2+2 = 4; sigma_squared 4 => threshold delta^2 >= 16
        workspace.initialize(config=base_config(
            min_abs_delta="1", sigma_squared="4"))
        discovery.advance(str(workspace.path), 1)
        measurement = discovery.snapshot(str(workspace.path)) \
            ["observations"][1]["measurements"][0]
        self.assertEqual(measurement["variance"], "4")
        self.assertTrue(measurement["triggered"])

    def test_triggered_frame_reports_other_aperture_and_keeps_reference_clean(self):
        workspace = self._workspace()
        apply_batch(str(workspace.archive), batch("b1", 0,
            add_darks=[dark("d0", value=0, variance=0, width=2, height=2)],
            add_flats=[flat(values=[[1, 1], [1, 1]])],
            add_releases=[release("r1", 1)],
            put_frames=[
                px_frame("ref", 0, [[0, 0], [0, 0]], [[0, 0], [0, 0]],
                         observed_at=1),
                px_frame("mix", 0, [[10, 0], [0, 0]], [[0, 0], [0, 0]],
                         observed_at=2)]))
        workspace.initialize(config=base_config(
            aperture_ids=["a0", "a1"], min_abs_delta="5"))
        discovery.advance(str(workspace.path), 1)
        snap = discovery.snapshot(str(workspace.path) )
        observation = snap["observations"][1]
        self.assertEqual(observation["status"], "triggered")
        self.assertEqual([m["aperture_id"] for m in observation["measurements"]],
                         ["a0", "a1"])
        a1 = next(m for m in observation["measurements"]
                  if m["aperture_id"] == "a1")
        self.assertFalse(a1["triggered"])
        # the whole frame is rejected from the reference
        self.assertEqual([m["frame_id"] for m in
                          snap["final_reference"]["members"]], ["ref"])
        # non-configured aperture still appears in reference stack measurements
        definition = classic_definition(width=2, height=2, apertures=[
            {"id": "a0", "x": 0, "y": 0, "radius": 0},
            {"id": "other", "x": 1, "y": 1, "radius": 0}])
        workspace2 = self._workspace(definition=definition)
        apply_batch(str(workspace2.archive), batch("b1", 0,
            add_darks=[dark("d0", value=0, variance=0, width=2, height=2)],
            add_flats=[flat(values=[[1, 1], [1, 1]])],
            add_releases=[release("r1", 1)],
            put_frames=[px_frame("ref", 0, [[5, 0], [0, 7]],
                                 [[0, 0], [0, 0]], observed_at=1)]))
        workspace2.initialize(config=base_config(aperture_ids=["a0"]))
        discovery.advance(str(workspace2.path), 1)
        measured = {a["aperture_id"]: a
                    for a in discovery.snapshot(str(workspace2.path))
                    ["final_reference"]["stack"]["apertures"]}
        self.assertEqual(measured["other"]["sum_rate"], "7")

    def test_empty_reference_product_is_fully_structured(self):
        workspace = self._workspace()
        # a frame exists but is blocked: no release covers it
        apply_batch(str(workspace.archive), batch("b1", 0,
            add_darks=[dark("d0", width=2, height=2)],
            add_flats=[flat(values=[[1, 1], [1, 1]])],
            put_frames=[px_frame("orphan", 0, [[1, 1], [1, 1]],
                                 [[1, 1], [1, 1]], observed_at=1)]))
        workspace.initialize(config=base_config(aperture_ids=["a0", "a1"]))
        discovery.advance(str(workspace.path), 1)
        snap = discovery.snapshot(str(workspace.path))
        # blocked frames never enter the time series; the reference stays empty
        self.assertEqual(snap["observations"], [])
        product = snap["final_reference"]
        self.assertEqual(product["members"], [])
        self.assertEqual(product["stack"]["rate_image"],
                         [[None, None], [None, None]])
        self.assertEqual(product["stack"]["exposure_s"],
                         [[0, 0], [0, 0]])
        self.assertEqual(product["stack"]["frame_audit"], [])
        self.assertTrue(all(a["sum_rate"] is None
                            for a in product["stack"]["apertures"]))
        self.assertEqual(product["uncertainty"]["variance_image"],
                         [[None, None], [None, None]])
        self.assertEqual(product["uncertainty"]["aperture_covariance"],
                         [[None, None], [None, None]])
        self.assertEqual(snap["excluded_frames"], [{
            "frame_id": "orphan", "frame_version": 1,
            "status": "no_release"}])


if __name__ == "__main__":
    unittest.main()
