"""Revision-bound calibration selection, stacks, photometry and deliverability."""
from fractions import Fraction

from ..campaign import (assign_generic, covariance_matrix_generic,
                        precompute_bound, stack_generic)
from ..photometry import measure


def _products_for_science(records):
    """Parse stored rational strings back into exact Fractions."""
    products = {}
    for record in records:
        product = dict(record)
        product["requirements"] = [
            dict(requirement,
                 max_variance=Fraction(requirement["max_variance"]))
            for requirement in record["requirements"]]
        products[product["id"]] = product
    return products


def select_bindings(snapshot):
    """Pick the highest-version visible release covering every frame's label.

    Bindings maps frame id to either the chosen release record or None
    (withdrawn frame or no covering release).
    """
    bindings = {}
    for frame_id in sorted(snapshot.frames):
        frame = snapshot.frames[frame_id]
        if frame["withdrawn"]:
            bindings[frame_id] = None
            continue
        observed = frame["observed_at_s"]
        candidates = [release for release in snapshot.releases.values()
                      if release["start_s"] <= observed < release["end_s"]]
        if not candidates:
            bindings[frame_id] = None
            continue
        bindings[frame_id] = max(candidates, key=lambda release: release["version"])
    return bindings


def binding_rows(snapshot, bindings):
    """frame_bindings entries in frame_id order for the archive report."""
    rows = []
    for frame_id in sorted(snapshot.frames):
        frame = snapshot.frames[frame_id]
        if frame["withdrawn"]:
            status = "withdrawn"
            release_id = dark_id = flat_id = None
        else:
            release = bindings[frame_id]
            if release is None:
                status = "no_release"
                release_id = dark_id = flat_id = None
            elif release["retracted"]:
                status = "retracted_release"
                release_id = release["id"]
                dark_id = release["dark_id"]
                flat_id = release["flat_id"]
            else:
                status = "ready"
                release_id = release["id"]
                dark_id = release["dark_id"]
                flat_id = release["flat_id"]
        rows.append({"frame_id": frame_id,
                     "frame_version": frame["frame_version"],
                     "status": status, "release_id": release_id,
                     "dark_id": dark_id, "flat_id": flat_id})
    return rows


def status_counts(rows):
    counts = {"frames_seen": len(rows), "ready_frames": 0,
              "blocked_frames": 0, "withdrawn_frames": 0}
    for row in rows:
        if row["status"] == "ready":
            counts["ready_frames"] += 1
        elif row["status"] == "withdrawn":
            counts["withdrawn_frames"] += 1
        else:
            counts["blocked_frames"] += 1
    return counts


def _ready_frames(snapshot, bindings):
    ready = {}
    for frame_id, release in bindings.items():
        if release is not None and not release["retracted"]:
            ready[frame_id] = snapshot.frames[frame_id]
    return ready


def _bound_helpers(snapshot):
    def source_of(contribution):
        # precompute_bound appends dark_id as the last contribution element
        return (contribution[-1], contribution[2], contribution[3])

    def variance_of(source):
        dark_id, x, y = source
        return snapshot.darks[dark_id]["dark_variance"][y][x]

    return source_of, variance_of


def render_classic(snapshot, bindings):
    """Schema v1 stack plus a full-frame exact uncertainty block."""
    ready = _ready_frames(snapshot, bindings)
    width, height = snapshot.width, snapshot.height
    aperture_records = {record["id"]: record for record in snapshot.apertures}
    if not ready:
        image = [[None for _ in range(width)] for _ in range(height)]
        measurements = measure(image, aperture_records)
        report = {
            "schema_version": 1,
            "rate_image": [[None for _ in range(width)] for _ in range(height)],
            "exposure_s": [[0 for _ in range(width)] for _ in range(height)],
            "contributor_count": [[0 for _ in range(width)] for _ in range(height)],
            "frame_audit": [], "apertures": measurements,
            "summary": {"frames": 0, "covered_pixels": 0,
                        "accepted_contributions": 0,
                        "complete_apertures": sum(a["complete"]
                                                  for a in measurements)}}
        order = sorted(aperture_records)
        size = len(order)
        uncertainty = {
            "variance_image": [[None for _ in range(width)]
                               for _ in range(height)],
            "aperture_order": order,
            "aperture_covariance": [[None for _ in range(size)]
                                    for _ in range(size)]}
        return report, uncertainty
    bound = {frame_id: bindings[frame_id] for frame_id in ready}
    masters = {"darks": snapshot.darks, "flats": snapshot.flats}
    precomp = precompute_bound(width, height, ready, bound, masters)
    source_of, variance_of = _bound_helpers(snapshot)
    stack, variance_image, aperture_uncertainty = stack_generic(
        ready, precomp, width, height, aperture_records,
        sorted(aperture_records), variance_of, source_of)
    order = sorted(aperture_records)
    covariance = [[None for _ in order] for _ in order]
    complete = {entry["aperture_id"]: entry["complete"]
                for entry in stack["apertures"]}
    for i, aperture_id in enumerate(order):
        if not complete[aperture_id]:
            continue
        for j, other_id in enumerate(order):
            if not complete[other_id]:
                continue
            value = Fraction(0)
            left = aperture_uncertainty[aperture_id]
            right = aperture_uncertainty[other_id]
            for source, g in left["dark_g"].items():
                other = right["dark_g"].get(source)
                if other is not None:
                    value += Fraction(variance_of(source)) * g * other
            for output, qf in left["output_qf"].items():
                if output in right["output_qf"]:
                    value += qf
            covariance[i][j] = str(value)
    uncertainty = {
        "variance_image": [[str(value) if value is not None else None
                            for value in row] for row in variance_image],
        "aperture_order": order,
        "aperture_covariance": covariance}
    return stack, uncertainty


def render_campaign(snapshot, bindings):
    """Schema v2 campaign report driven entirely by per-frame bindings."""
    ready = _ready_frames(snapshot, bindings)
    width, height = snapshot.width, snapshot.height
    aperture_records = {record["id"]: record for record in snapshot.apertures}
    aperture_map = {aperture_id: dict(aperture, _width=width, _height=height)
                    for aperture_id, aperture in aperture_records.items()}
    products = _products_for_science(snapshot.products)
    if not ready:
        return {
            "schema_version": 2, "products": [], "measurement_order": [],
            "covariance": [], "unfulfilled_product_ids": sorted(products),
            "unused_frame_ids": [],
            "summary": {"requested_products": len(products),
                        "delivered_products": 0, "total_priority": 0,
                        "used_exposure_s": 0, "used_frames": 0}}
    bound = {frame_id: bindings[frame_id] for frame_id in ready}
    masters = {"darks": snapshot.darks, "flats": snapshot.flats}
    precomp = precompute_bound(width, height, ready, bound, masters)
    source_of, variance_of = _bound_helpers(snapshot)
    assignment = assign_generic(products, ready, aperture_map, precomp,
                                variance_of, source_of)
    product_entries = []
    measurement_order = []
    deliveries = {}
    used_frames = set()
    total_priority = 0
    used_exposure_s = 0
    for product_id in sorted(assignment):
        subset = assignment[product_id]
        product = products[product_id]
        used_frames.update(subset)
        total_priority += product["priority"]
        used_exposure_s += sum(ready[frame_id]["exposure_s"]
                               for frame_id in subset)
        product_frame_map = {frame_id: ready[frame_id] for frame_id in subset}
        required_apertures = sorted(requirement["aperture_id"]
                                    for requirement in product["requirements"])
        stack_report, variance_image, aperture_uncertainty = stack_generic(
            product_frame_map, precomp, width, height, aperture_records,
            required_apertures, variance_of, source_of)
        aperture_variances = []
        for aperture_id in required_apertures:
            uncertainty = aperture_uncertainty[aperture_id]
            variance = uncertainty["raw"]
            for source, coefficient in uncertainty["dark_g"].items():
                variance += Fraction(variance_of(source)) * coefficient ** 2
            aperture_variances.append({"aperture_id": aperture_id,
                                       "variance": str(variance)})
            deliveries[(product_id, aperture_id)] = uncertainty
            measurement_order.append((product_id, aperture_id))
        product_entries.append({
            "product_id": product_id,
            "frame_ids": sorted(subset),
            "stack": stack_report,
            "uncertainty": {
                "variance_image": [[str(value) if value is not None else None
                                    for value in row] for row in variance_image],
                "aperture_variances": aperture_variances}})
    measurement_order.sort()
    covariances = covariance_matrix_generic(measurement_order, deliveries,
                                            variance_of)
    unfulfilled = sorted(set(products) - set(assignment))
    unused = sorted(set(ready) - used_frames)
    return {
        "schema_version": 2,
        "products": product_entries,
        "measurement_order": [{"product_id": product_id, "aperture_id": aperture_id}
                              for product_id, aperture_id in measurement_order],
        "covariance": [[str(value) for value in row] for row in covariances],
        "unfulfilled_product_ids": unfulfilled,
        "unused_frame_ids": unused,
        "summary": {"requested_products": len(products),
                    "delivered_products": len(assignment),
                    "total_priority": total_priority,
                    "used_exposure_s": used_exposure_s,
                    "used_frames": len(used_frames)}}


def render(snapshot):
    """Full schema_version=3 archive report for the snapshot's exact revision."""
    bindings = select_bindings(snapshot)
    rows = binding_rows(snapshot, bindings)
    summary = status_counts(rows)
    if snapshot.products is None:
        report, uncertainty = render_classic(snapshot, bindings)
    else:
        report = render_campaign(snapshot, bindings)
        uncertainty = None
    return {"schema_version": 3, "dataset_id": snapshot.dataset_id,
            "revision": snapshot.revision,
            "frame_bindings": rows, "report": report,
            "uncertainty": uncertainty, "summary": summary}
