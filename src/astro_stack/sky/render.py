"""Schema_version=4 rendering: one fixed plane, subtracted deliverables.

The archive itself is never modified: a snapshot of the requested committed
revision is captured under the archive lock and all science runs against that
immutable in-memory view.  Classic archives stack every ready frame; campaign
archives keep the existing windows, mutual exclusion, ``max_frames`` and
optimal ordering, but feasibility now requires the subtracted aperture
variance to satisfy the requirement (correlations with the plane included).
"""
from itertools import combinations

from ..archive.science import (binding_rows, select_bindings,
                               status_counts, _ready_frames)
from ..archive.store import Store, replay_snapshot
from ..campaign import _assign_with_options, precompute_bound
from ..photometry import nominal_disk
from .engine import StackEngine
from .model import Form, SkyError, SkyPlane, validate_specification, covariance
from fractions import Fraction


def _capture(path, revision):
    if type(revision) is not int or revision < 0:
        raise SkyError(f"illegal revision: {revision!r}")
    with Store(path) as store:
        snapshot = replay_snapshot(store.state, store.root, revision)
    return snapshot


def _variance_callbacks(snapshot):
    def raw_variance_of(frame_id, x, y):
        return snapshot.frames[frame_id]["variance"][y][x]

    def dark_variance_of(source):
        _kind, dark_id, x, y = source
        return snapshot.darks[dark_id]["dark_variance"][y][x]

    def dark_exposure_of(dark_id):
        return snapshot.darks[dark_id]["dark_exposure_s"]

    return raw_variance_of, dark_variance_of, dark_exposure_of


def _sample_rows(samples):
    rows = []
    for item in samples:
        contribution = item["contribution"]
        out_x, out_y = contribution[0], contribution[1]
        rows.append({
            "frame_id": item["frame_id"],
            "detector_x": item["detector_x"],
            "detector_y": item["detector_y"],
            "output_x": out_x, "output_y": out_y,
            "dark_id": item["binding"]["dark_id"],
            "flat_id": item["binding"]["flat_id"],
            "rate": str(contribution[5])})
    return rows


def _sky_fit_block(plane, samples):
    fit = plane.fit_report()
    return {"samples": _sample_rows(samples),
            "rank": plane.rank,
            "basis": fit["basis"],
            "estimates": fit["estimates"],
            "covariance": fit["covariance"]}


def _prepare(snapshot, specification):
    width, height = snapshot.width, snapshot.height
    bindings = select_bindings(snapshot)
    ready = _ready_frames(snapshot, bindings)
    bound = {frame_id: bindings[frame_id] for frame_id in ready}
    masters = {"darks": snapshot.darks, "flats": snapshot.flats}
    # the precompute view is needed even when ready is empty to validate that
    # every requested sample belongs to a ready frame; guard the empty case
    precomp = precompute_bound(width, height, ready, bound, masters)
    samples = validate_specification(specification, snapshot, bindings,
                                     precomp, width, height)
    raw_variance_of, dark_variance_of, dark_exposure_of = \
        _variance_callbacks(snapshot)
    for item in samples:
        item["output_x"] = item["contribution"][0]
        item["output_y"] = item["contribution"][1]
    plane = SkyPlane(samples, raw_variance_of, dark_variance_of,
                     dark_exposure_of)
    aperture_records = {record["id"]: record for record in snapshot.apertures}
    return {
        "bindings": bindings, "ready": ready, "precomp": precomp,
        "samples": samples, "plane": plane,
        "aperture_records": aperture_records,
        "raw_variance_of": raw_variance_of,
        "dark_variance_of": dark_variance_of}


# --------------------------------------------------------------------------
# Classic archive
# --------------------------------------------------------------------------
def _render_classic(snapshot, prepared):
    width, height = snapshot.width, snapshot.height
    ready = prepared["ready"]
    order = sorted(prepared["aperture_records"])
    if not ready:
        image = [[None for _ in range(width)] for _ in range(height)]
        measurements = [_empty_aperture(prepared["aperture_records"][aperture_id],
                                        width, height) for aperture_id in order]
        report = {
            "schema_version": 1,
            "rate_image": image,
            "exposure_s": [[0 for _ in range(width)] for _ in range(height)],
            "contributor_count": [[0 for _ in range(width)]
                                  for _ in range(height)],
            "pixel_status": [["uncovered" for _ in range(width)]
                             for _ in range(height)],
            "frame_audit": [], "apertures": measurements,
            "summary": {"frames": 0, "covered_pixels": 0,
                        "accepted_contributions": 0,
                        "complete_apertures": sum(1 for row in measurements
                                                  if row["complete"])}}
        size = len(order)
        uncertainty = {
            "variance_image": [[None for _ in range(width)]
                               for _ in range(height)],
            "aperture_order": order,
            "aperture_covariance": [[None for _ in range(size)]
                                    for _ in range(size)]}
        return report, uncertainty
    engine = StackEngine(ready, prepared["precomp"], width, height,
                         prepared["aperture_records"], prepared["plane"],
                         prepared["raw_variance_of"],
                         prepared["dark_variance_of"])
    stack, variance_image, aperture_forms = engine.stack_report()
    covariance_matrix = engine.aperture_covariance(order, aperture_forms)
    uncertainty = {
        "variance_image": variance_image,
        "aperture_order": order,
        "aperture_covariance": covariance_matrix}
    return stack, uncertainty


def _empty_aperture(aperture, width, height):
    disk = nominal_disk(aperture)
    inside = [(x, y) for x, y in disk
              if 0 <= x < width and 0 <= y < height]
    complete = len(inside) == len(disk)
    return {"aperture_id": aperture["id"], "nominal_pixels": len(disk),
            "outside_pixels": len(disk) - len(inside),
            "missing_pixels": len(inside), "measured_pixels": 0,
            "status": "incomplete", "sum_rate": None,
            "complete": complete}


# --------------------------------------------------------------------------
# Campaign archive with subtracted-variance feasibility
# --------------------------------------------------------------------------
def _aperture_target_and_feasible(engine_builder, aperture, subset, plane,
                                  requirement):
    """One requirement against one subset; None means not deliverable."""
    engine = engine_builder(subset)
    disk = nominal_disk(aperture)
    inside = [(x, y) for x, y in disk
              if 0 <= x < engine.width and 0 <= y < engine.height]
    if len(inside) != len(disk):
        return None
    target = [Fraction(0), Fraction(0), Fraction(0)]
    science_form = Form(Fraction(0))
    for x, y in inside:
        if not engine.exposure[y][x] or \
                engine.exposure[y][x] < requirement["min_exposure_s"]:
            return None
        science_form = science_form + engine.pixel_forms[y][x]
        target[0] += Fraction(1)
        target[1] += Fraction(x)
        target[2] += Fraction(y)
    if plane.solve_target(target) is None:
        return None
    _weights, background_form = plane.solve_target(target)
    subtracted = science_form - background_form
    variance = subtracted.variance(engine.raw_variance_of,
                                   engine.dark_variance_of)
    if variance > requirement["max_variance"]:
        return None
    return variance


def _sky_options(product, eligible_ids, all_frame_ids, aperture_map,
                 engine_builder, plane):
    """Inclusion-minimal feasible frame masks under the sky-subtracted rules.

    Masks use the global frame ordering so the disjoint-assignment DP can
    compare masks across products.
    """
    minimal_masks = []
    bit_of = {frame_id: 1 << index
              for index, frame_id in enumerate(all_frame_ids)}
    requirements = {entry["aperture_id"]: entry
                    for entry in product["requirements"]}
    ordered = sorted(eligible_ids)
    for size in range(1, min(product["max_frames"], len(ordered)) + 1):
        for subset_tuple in combinations(ordered, size):
            subset = list(subset_tuple)
            mask = 0
            for frame_id in subset:
                mask |= bit_of[frame_id]
            if any((mask & found) == found for found in minimal_masks):
                continue
            feasible = True
            for requirement in product["requirements"]:
                aperture = aperture_map[requirement["aperture_id"]]
                if _aperture_target_and_feasible(
                        engine_builder, aperture, subset, plane,
                        requirements[requirement["aperture_id"]]) is None:
                    feasible = False
                    break
            if feasible:
                minimal_masks.append(mask)
    minimal_masks.sort()
    return minimal_masks


def _render_campaign(snapshot, prepared):
    width, height = snapshot.width, snapshot.height
    ready = prepared["ready"]
    plane = prepared["plane"]
    aperture_records = prepared["aperture_records"]
    aperture_map = {aperture_id: dict(aperture, _width=width, _height=height)
                    for aperture_id, aperture in aperture_records.items()}
    products = _parse_products(snapshot.products)
    precomp = prepared["precomp"]
    raw_variance_of = prepared["raw_variance_of"]
    dark_variance_of = prepared["dark_variance_of"]
    if not ready:
        return {
            "schema_version": 2, "products": [], "measurement_order": [],
            "covariance": [], "unfulfilled_product_ids": sorted(products),
            "unused_frame_ids": [],
            "summary": {"requested_products": len(products),
                        "delivered_products": 0, "total_priority": 0,
                        "used_exposure_s": 0, "used_frames": 0}}

    # the same subset is evaluated for every requirement and again when the
    # chosen assignment is rendered; build each engine exactly once
    engine_cache = {}
    all_frame_ids = sorted(ready)

    def engine_builder(subset):
        key = tuple(sorted(subset))
        cached = engine_cache.get(key)
        if cached is None:
            cached = StackEngine(subset, precomp, width, height,
                                 aperture_records, plane, raw_variance_of,
                                 dark_variance_of)
            engine_cache[key] = cached
        return cached

    def options_for(product_id):
        product = products[product_id]
        eligible_ids = [frame_id for frame_id in all_frame_ids
                        if product["start_s"] <= ready[frame_id]["observed_at_s"]
                        < product["end_s"]]
        return _sky_options(product, eligible_ids, all_frame_ids,
                            aperture_map, engine_builder, plane)

    assignment = _assign_with_options(products, ready, options_for)
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
        required_apertures = sorted(requirement["aperture_id"]
                                    for requirement in product["requirements"])
        engine = engine_builder(subset)
        stack_report, variance_image, aperture_forms = engine.stack_report()
        aperture_variances = []
        for aperture_id in required_apertures:
            form = aperture_forms[aperture_id]
            # feasibility already proved ok; the form must be present
            aperture_variances.append({
                "aperture_id": aperture_id,
                "variance": str(form.variance(raw_variance_of,
                                               dark_variance_of))})
            deliveries[(product_id, aperture_id)] = form
            measurement_order.append((product_id, aperture_id))
        product_entries.append({
            "product_id": product_id,
            "frame_ids": sorted(subset),
            "stack": stack_report,
            "uncertainty": {
                "variance_image": variance_image,
                "aperture_variances": aperture_variances}})
    measurement_order.sort()
    size = len(measurement_order)
    covariance_matrix = [[Fraction(0) for _ in range(size)] for _ in range(size)]
    for i in range(size):
        for j in range(i, size):
            value = covariance(deliveries[measurement_order[i]],
                               deliveries[measurement_order[j]],
                               raw_variance_of, dark_variance_of)
            covariance_matrix[i][j] = value
            covariance_matrix[j][i] = value
    unfulfilled = sorted(set(products) - set(assignment))
    unused = sorted(set(ready) - used_frames)
    return {
        "schema_version": 2,
        "products": product_entries,
        "measurement_order": [{"product_id": product_id, "aperture_id": aperture_id}
                              for product_id, aperture_id in measurement_order],
        "covariance": [[str(value) for value in row]
                       for row in covariance_matrix],
        "unfulfilled_product_ids": unfulfilled,
        "unused_frame_ids": unused,
        "summary": {"requested_products": len(products),
                    "delivered_products": len(assignment),
                    "total_priority": total_priority,
                    "used_exposure_s": used_exposure_s,
                    "used_frames": len(used_frames)}}


def _parse_products(records):
    products = {}
    for record in records:
        product = dict(record)
        product["requirements"] = [
            dict(requirement,
                 max_variance=Fraction(requirement["max_variance"]))
            for requirement in record["requirements"]]
        products[product["id"]] = product
    return products


# --------------------------------------------------------------------------
# Public entry
# --------------------------------------------------------------------------
def render(path, revision, specification):
    """Render the schema_version=4 sky-subtracted report of one revision."""
    snapshot = _capture(path, revision)
    prepared = _prepare(snapshot, specification)
    rows = binding_rows(snapshot, prepared["bindings"])
    summary = status_counts(rows)
    sky_fit = _sky_fit_block(prepared["plane"], prepared["samples"])
    if snapshot.products is None:
        report, uncertainty = _render_classic(snapshot, prepared)
    else:
        report = _render_campaign(snapshot, prepared)
        uncertainty = None
    return {"schema_version": 4,
            "dataset_id": snapshot.dataset_id,
            "revision": snapshot.revision,
            "frame_bindings": rows,
            "report": report,
            "uncertainty": uncertainty,
            "summary": summary,
            "sky_fit": sky_fit}
