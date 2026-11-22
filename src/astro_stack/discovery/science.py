"""Revision-bound time-series transient discovery with exact rational noise.

The science of one ``advance`` is fully defined by replaying every ready raw
frame visible at a fixed archive revision. Frame calibration, rejection
precedence, local exposure weighting and random-source identities reuse the
archive's per-frame binding machinery (``precompute_bound``) so this module
never invents a second rate, variance or binding table.
"""
from fractions import Fraction

from ..campaign import precompute_bound
from ..photometry import measure, nominal_disk
from ..archive.science import select_bindings


def _parse_config(config):
    return {"aperture_ids": list(config["aperture_ids"]),
            "bootstrap_frames": config["bootstrap_frames"],
            "reference_capacity": config["reference_capacity"],
            "min_exposure_s": config["min_exposure_s"],
            "min_abs_delta": Fraction(config["min_abs_delta"]),
            "sigma_squared": Fraction(config["sigma_squared"])}


def _lineage(frame, binding):
    return {"frame_id": frame["id"], "frame_version": frame["frame_version"],
            "observed_at_s": frame["observed_at_s"],
            "release_id": binding["id"], "dark_id": binding["dark_id"],
            "flat_id": binding["flat_id"]}


class ReferenceAccumulator:
    """Exposure-weighted stack of admitted members with removable members.

    Removal is exact: every stored numerator and raw/dark accumulator is an
    additive contribution of one frame, so evicting the oldest member restores
    the identical product that existed before it was admitted. Dark random
    sources are keyed by (dark_id, detector_x, detector_y) and merged before
    uncertainty propagation; flat, mask and rejection stay deterministic.
    """

    def __init__(self, width, height, apertures, snapshot):
        self.width = width
        self.height = height
        self.apertures = apertures
        self.snapshot = snapshot
        self.weighted = [[Fraction(0) for _ in range(width)]
                         for _ in range(height)]
        self.exposure = [[0 for _ in range(width)] for _ in range(height)]
        self.counts = [[0 for _ in range(width)] for _ in range(height)]
        self.raw_accum = [[Fraction(0) for _ in range(width)]
                          for _ in range(height)]
        self.dark_accum = [[{} for _ in range(width)] for _ in range(height)]
        self.audits = {}
        self.members = []

    def add(self, frame_id, bundle):
        self.members.append(frame_id)
        self.audits[frame_id] = dict(bundle["audit"])
        for contribution in bundle["contributions"]:
            (out_x, out_y, _det_x, _det_y, frame_exposure, cal_rate,
             qf_term, dark_h) = contribution[:8]
            dark_id = contribution[-1]
            source = (dark_id, contribution[2], contribution[3])
            self.weighted[out_y][out_x] += cal_rate * frame_exposure
            self.exposure[out_y][out_x] += frame_exposure
            self.counts[out_y][out_x] += 1
            self.raw_accum[out_y][out_x] += qf_term
            bucket = self.dark_accum[out_y][out_x]
            bucket[source] = bucket.get(source, Fraction(0)) + \
                dark_h * frame_exposure

    def remove(self, frame_id, bundle):
        self.members.remove(frame_id)
        del self.audits[frame_id]
        for contribution in bundle["contributions"]:
            (out_x, out_y, _det_x, _det_y, frame_exposure, cal_rate,
             qf_term, dark_h) = contribution[:8]
            dark_id = contribution[-1]
            source = (dark_id, contribution[2], contribution[3])
            self.weighted[out_y][out_x] -= cal_rate * frame_exposure
            self.exposure[out_y][out_x] -= frame_exposure
            self.counts[out_y][out_x] -= 1
            self.raw_accum[out_y][out_x] -= qf_term
            bucket = self.dark_accum[out_y][out_x]
            updated = bucket[source] - dark_h * frame_exposure
            if updated:
                bucket[source] = updated
            else:
                del bucket[source]

    def _variance_of(self, source):
        dark_id, x, y = source
        return self.snapshot.darks[dark_id]["dark_variance"][y][x]

    def render_product(self, lineages):
        """Full reference product: members, classic stack and uncertainty."""
        width, height = self.width, self.height
        order = sorted(self.apertures)
        image = [[self.weighted[y][x] / self.exposure[y][x]
                  if self.exposure[y][x] else None
                  for x in range(width)] for y in range(height)]
        variance_image = [[None for _ in range(width)] for _ in range(height)]
        disks = {aperture_id: {(x, y)
                               for x, y in nominal_disk(self.apertures[aperture_id])
                               if 0 <= x < width and 0 <= y < height}
                 for aperture_id in order}
        aperture_uncertainty = {
            aperture_id: {"dark_g": {}, "raw": Fraction(0)}
            for aperture_id in order}
        complete = {}
        for aperture_id in order:
            disk = nominal_disk(self.apertures[aperture_id])
            complete[aperture_id] = len(disks[aperture_id]) == len(disk) and all(
                image[y][x] is not None for x, y in disks[aperture_id])
        for y in range(height):
            for x in range(width):
                if not self.exposure[y][x]:
                    continue
                total = Fraction(self.exposure[y][x])
                qd = Fraction(0)
                for source, numerator in self.dark_accum[y][x].items():
                    qd += Fraction(self._variance_of(source)) * \
                        (numerator / total) ** 2
                variance_image[y][x] = self.raw_accum[y][x] / total ** 2 + qd
                for aperture_id in order:
                    if (x, y) in disks[aperture_id]:
                        uncertainty = aperture_uncertainty[aperture_id]
                        uncertainty["raw"] += self.raw_accum[y][x] / total ** 2
                        for source, numerator in self.dark_accum[y][x].items():
                            coefficient = numerator / total
                            uncertainty["dark_g"][source] = \
                                uncertainty["dark_g"].get(source, Fraction(0)) + \
                                coefficient
        measurements = measure(image, self.apertures)
        stack = {
            "rate_image": [[str(value) if value is not None else None
                            for value in row] for row in image],
            "exposure_s": [list(row) for row in self.exposure],
            "contributor_count": [list(row) for row in self.counts],
            "frame_audit": [dict(self.audits[frame_id])
                            for frame_id in sorted(self.audits)],
            "apertures": measurements,
            "summary": {
                "frames": len(self.members),
                "covered_pixels": sum(v is not None for row in image
                                      for v in row),
                "accepted_contributions": sum(
                    row["accepted_pixels"] for row in self.audits.values()),
                "complete_apertures": sum(
                    entry["complete"] for entry in measurements)}}
        covariance = [[None for _ in order] for _ in order]
        for i, aperture_id in enumerate(order):
            if not complete[aperture_id]:
                continue
            left = aperture_uncertainty[aperture_id]
            for j, other_id in enumerate(order):
                if not complete[other_id]:
                    continue
                right = aperture_uncertainty[other_id]
                value = left["raw"] if aperture_id == other_id else Fraction(0)
                for source, g in left["dark_g"].items():
                    other = right["dark_g"].get(source)
                    if other is not None:
                        value += Fraction(self._variance_of(source)) * g * other
                covariance[i][j] = str(value)
        return {
            "members": [dict(lineages[frame_id]) for frame_id in self.members],
            "stack": stack,
            "uncertainty": {
                "variance_image": [[str(value) if value is not None else None
                                    for value in row] for row in variance_image],
                "aperture_order": order,
                "aperture_covariance": covariance}}

    def compare_aperture(self, frame_id, bundle, aperture_id, aperture):
        """Exact delta and variance of one frame against the current reference.

        The candidate frame is never part of ``self``: its raw samples and the
        reference's raw samples are independent random variables, while dark
        samples sharing the (dark_id, detector x, detector y) identity are
        merged first and their partial coefficients differenced.
        """
        width, height = self.width, self.height
        disk = nominal_disk(aperture)
        inside = {(x, y) for x, y in disk
                  if 0 <= x < width and 0 <= y < height}
        frame_rate = Fraction(0)
        frame_raw_variance = Fraction(0)
        frame_dark = {}
        frame_pixels = {}
        for contribution in bundle["contributions"]:
            out_x, out_y = contribution[0], contribution[1]
            if (out_x, out_y) not in inside:
                continue
            exposure = contribution[4]
            frame_rate += contribution[5]
            frame_raw_variance += contribution[6]
            source = (contribution[-1], contribution[2], contribution[3])
            frame_dark[source] = frame_dark.get(source, Fraction(0)) + \
                contribution[7]
            frame_pixels[(out_x, out_y)] = contribution
        reference_rate = Fraction(0)
        reference_raw_variance = Fraction(0)
        for x, y in inside:
            if self.exposure[y][x]:
                reference_rate += self.weighted[y][x] / self.exposure[y][x]
                reference_raw_variance += \
                    self.raw_accum[y][x] / self.exposure[y][x] ** 2
        dark_variance_total = Fraction(0)
        sources = set(frame_dark)
        for x, y in inside:
            sources.update(self.dark_accum[y][x])
        for source in sources:
            frame_coefficient = frame_dark.get(source, Fraction(0))
            reference_coefficient = Fraction(0)
            for x, y in inside:
                numerator = self.dark_accum[y][x].get(source)
                if numerator is not None:
                    reference_coefficient += numerator / self.exposure[y][x]
            difference = frame_coefficient - reference_coefficient
            dark_variance_total += Fraction(self._variance_of(source)) * \
                difference ** 2
        delta = frame_rate - reference_rate
        variance = frame_raw_variance + reference_raw_variance + dark_variance_total
        return delta, variance


def _eligible(frame_id, bundle, config, aperture_records, width, height):
    """Every configured aperture's whole nominal disk must be covered by this
    single frame, and each covered output pixel needs min_exposure_s of valid
    exposure. No other frame may fill a quality gap."""
    minimum = config["min_exposure_s"]
    per_output_exposure = {}
    for contribution in bundle["contributions"]:
        key = (contribution[0], contribution[1])
        per_output_exposure[key] = per_output_exposure.get(key, 0) + \
            contribution[4]
    for aperture_id in config["aperture_ids"]:
        aperture = aperture_records[aperture_id]
        disk = nominal_disk(aperture)
        inside = [(x, y) for x, y in disk
                  if 0 <= x < width and 0 <= y < height]
        if len(inside) != len(disk):
            return False
        for output in inside:
            if per_output_exposure.get(output, 0) < minimum:
                return False
    return True


def analyze(snapshot, config):
    """Run the full time-series analysis for one fixed archive revision.

    Returns ``(observations, excluded, final_product, active_alerts)`` where
    every observation already carries its historical reference-before product.
    """
    parsed = _parse_config(config)
    width, height = snapshot.width, snapshot.height
    aperture_records = {record["id"]: record for record in snapshot.apertures}
    bindings = select_bindings(snapshot)
    excluded = []
    ready = []
    for frame_id in sorted(snapshot.frames):
        frame = snapshot.frames[frame_id]
        binding = bindings[frame_id]
        if frame["withdrawn"]:
            excluded.append({"frame_id": frame_id,
                             "frame_version": frame["frame_version"],
                             "status": "withdrawn"})
            continue
        if binding is None:
            excluded.append({"frame_id": frame_id,
                             "frame_version": frame["frame_version"],
                             "status": "no_release"})
            continue
        if binding["retracted"]:
            excluded.append({"frame_id": frame_id,
                             "frame_version": frame["frame_version"],
                             "status": "retracted_release"})
            continue
        ready.append(frame_id)
    ready.sort(key=lambda frame_id: (snapshot.frames[frame_id]["observed_at_s"],
                                     frame_id))
    frame_map = {frame_id: snapshot.frames[frame_id] for frame_id in ready}
    bound = {frame_id: bindings[frame_id] for frame_id in ready}
    masters = {"darks": snapshot.darks, "flats": snapshot.flats}
    precomp = precompute_bound(width, height, frame_map, bound, masters)
    lineages = {frame_id: _lineage(snapshot.frames[frame_id], bindings[frame_id])
                for frame_id in ready}
    reference = ReferenceAccumulator(width, height, aperture_records, snapshot)
    observations = []
    active_alerts = {}
    for frame_id in ready:
        bundle = precomp["frames"][frame_id]
        reference_before = reference.render_product(lineages)
        if not _eligible(frame_id, bundle, parsed, aperture_records,
                         width, height):
            observations.append({
                "frame": dict(lineages[frame_id]), "status": "ineligible",
                "reference_before": reference_before, "measurements": []})
            continue
        if len(reference.members) < parsed["bootstrap_frames"]:
            observations.append({
                "frame": dict(lineages[frame_id]), "status": "bootstrap",
                "reference_before": reference_before, "measurements": []})
            reference.add(frame_id, bundle)
            if len(reference.members) > parsed["reference_capacity"]:
                oldest = reference.members[0]
                reference.remove(oldest, precomp["frames"][oldest])
            continue
        measurements = []
        frame_triggered = False
        for aperture_id in parsed["aperture_ids"]:
            delta, variance = reference.compare_aperture(
                frame_id, bundle, aperture_id, aperture_records[aperture_id])
            triggered = abs(delta) >= parsed["min_abs_delta"] and \
                delta ** 2 >= parsed["sigma_squared"] * variance
            if triggered:
                frame_triggered = True
            measurements.append({
                "aperture_id": aperture_id, "delta": str(delta),
                "variance": str(variance), "triggered": triggered})
        status = "triggered" if frame_triggered else "quiet"
        observations.append({
            "frame": dict(lineages[frame_id]), "status": status,
            "reference_before": reference_before,
            "measurements": measurements})
        if frame_triggered:
            for measurement in measurements:
                if measurement["triggered"]:
                    active_alerts[(frame_id, measurement["aperture_id"])] = {
                        "frame": dict(lineages[frame_id]),
                        "aperture_id": measurement["aperture_id"],
                        "reference_members": [
                            dict(lineages[member])
                            for member in reference.members],
                        "delta": measurement["delta"],
                        "variance": measurement["variance"]}
        else:
            reference.add(frame_id, bundle)
            if len(reference.members) > parsed["reference_capacity"]:
                oldest = reference.members[0]
                reference.remove(oldest, precomp["frames"][oldest])
    final_product = reference.render_product(lineages)
    alerts = [active_alerts[key] for key in sorted(active_alerts)]
    return observations, excluded, final_product, alerts


def build_snapshot(snapshot, config, revision):
    """Assemble the complete schema_version=1 discovery science report."""
    observations, excluded, final_product, alerts = analyze(snapshot, config)
    counts = {"ready_frames": len(observations), "ineligible_frames": 0,
              "bootstrap_frames": 0, "quiet_frames": 0,
              "triggered_frames": 0}
    for observation in observations:
        counts[f"{observation['status']}_frames"] += 1
    counts["active_alerts"] = len(alerts)
    counts["reference_frames"] = len(final_product["members"])
    return {
        "schema_version": 1,
        "dataset_id": snapshot.dataset_id,
        "revision": revision,
        "config": {
            "aperture_ids": list(config["aperture_ids"]),
            "bootstrap_frames": config["bootstrap_frames"],
            "reference_capacity": config["reference_capacity"],
            "min_exposure_s": config["min_exposure_s"],
            "min_abs_delta": config["min_abs_delta"],
            "sigma_squared": config["sigma_squared"]},
        "excluded_frames": excluded,
        "observations": observations,
        "final_reference": final_product,
        "active_alerts": alerts,
        "summary": counts}


def diff_alerts(previous, current):
    """Compare two active-alert lists by logical identity and full payload."""
    old = {(payload["frame"]["frame_id"], payload["aperture_id"]): payload
           for payload in previous}
    new = {(payload["frame"]["frame_id"], payload["aperture_id"]): payload
           for payload in current}
    events = []
    for identity in sorted(set(old) | set(new)):
        before = old.get(identity)
        after = new.get(identity)
        if before is None:
            kind = "discover"
        elif after is None:
            kind = "retract"
        elif before != after:
            kind = "revise"
        else:
            continue
        events.append({"kind": kind, "frame_id": identity[0],
                       "aperture_id": identity[1],
                       "before": before, "after": after})
    return events
