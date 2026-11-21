"""Time-resolved campaign delivery with exact rational uncertainty propagation."""
from fractions import Fraction
from itertools import combinations

from .photometry import measure, nominal_disk


def precompute(data, width, height, frames, usable):
    """Per-frame accepted detector samples, shared by every campaign calculation."""
    mean_flat = Fraction(sum(usable), len(usable))
    dark_exposure = data["dark_exposure_s"]
    per_frame = {}
    for frame_id in sorted(frames):
        frame = frames[frame_id]
        audit = {"frame_id": frame_id, "accepted_pixels": 0, "masked_pixels": 0,
                 "zero_flat_pixels": 0, "saturated_pixels": 0, "outside_pixels": 0}
        contributions = []
        for y in range(height):
            for x in range(width):
                if data["bad_mask"][y][x]:
                    audit["masked_pixels"] += 1
                    continue
                flat = data["flat"][y][x]
                if flat == 0:
                    audit["zero_flat_pixels"] += 1
                    continue
                pixel = frame["pixels"][y][x]
                if pixel >= frame["saturation"]:
                    audit["saturated_pixels"] += 1
                    continue
                out_x, out_y = x + frame["dx"], y + frame["dy"]
                if not (0 <= out_x < width and 0 <= out_y < height):
                    audit["outside_pixels"] += 1
                    continue
                exposure = frame["exposure_s"]
                dark_rate = Fraction(data["dark"][y][x], dark_exposure)
                cal_rate = (Fraction(pixel, exposure) - dark_rate) * mean_flat / flat
                flat_factor = mean_flat / flat
                qf_term = Fraction(frame["variance"][y][x]) * flat_factor ** 2
                dark_h = mean_flat / (flat * dark_exposure)
                contributions.append((out_x, out_y, x, y, exposure, cal_rate,
                                      qf_term, dark_h, flat,
                                      frame["variance"][y][x],
                                      data["campaign"]["dark_variance"][y][x]))
                audit["accepted_pixels"] += 1
        per_frame[frame_id] = {"audit": audit, "contributions": contributions}
    return {"mean_flat": mean_flat, "frames": per_frame,
            "dark_exposure": dark_exposure}


def _aperture_records(precomp, aperture, eligible_ids):
    """Per eligible frame: output pixel -> (detector x, detector y, exposure, qf term, dark h)."""
    disk = nominal_disk(aperture)
    width = aperture["_width"]
    height = aperture["_height"]
    inside = {(x, y) for x, y in disk if 0 <= x < width and 0 <= y < height}
    records = {}
    for frame_id in eligible_ids:
        per_output = {}
        for contribution in precomp["frames"][frame_id]["contributions"]:
            out_x, out_y, det_x, det_y, exposure, _cal, qf_term, dark_h = \
                contribution[:8]
            if (out_x, out_y) in inside:
                per_output[(out_x, out_y)] = (det_x, det_y, exposure, qf_term, dark_h)
        records[frame_id] = per_output
    return disk, inside, records


def _evaluate(aperture, requirement, subset, records, dark_variance):
    """Exact rational aperture variance for a frame subset; None when undeliverable."""
    disk, inside, _ = records
    if len(inside) != len(disk):
        return None
    per_output = {}
    for frame_id in subset:
        for output, entry in records[2][frame_id].items():
            det_x, det_y, exposure, qf_term, dark_h = entry
            accum = per_output.get(output)
            if accum is None:
                accum = [0, Fraction(0), {}]
                per_output[output] = accum
            accum[0] += exposure
            accum[1] += qf_term
            dark_accum = accum[2].get((det_x, det_y))
            if dark_accum is None:
                accum[2][(det_x, det_y)] = (exposure, dark_h)
            else:
                accum[2][(det_x, det_y)] = (dark_accum[0] + exposure, dark_h)
    minimum = requirement["min_exposure_s"]
    for output in inside:
        if output not in per_output or per_output[output][0] < minimum:
            return None
    raw_variance = Fraction(0)
    dark_g = {}
    for output, (total_exposure, raw_accum, dark_accum) in per_output.items():
        e_squared = Fraction(total_exposure) ** 2
        raw_variance += raw_accum / e_squared
        for source, (local_exposure, dark_h) in dark_accum.items():
            g = dark_h * Fraction(local_exposure, total_exposure)
            dark_g[source] = dark_g.get(source, Fraction(0)) + g
    dark_term = Fraction(0)
    for (x, y), g in dark_g.items():
        dark_term += Fraction(dark_variance[y][x]) * g ** 2
    variance = raw_variance + dark_term
    if variance > requirement["max_variance"]:
        return None
    return variance


def feasible_subsets(product, eligible_ids, aperture_map, precomp, dark_variance):
    """All frame subsets (sorted id tuples) that deliver the product, size then id order."""
    options = []
    prepared = []
    for requirement in product["requirements"]:
        aperture = aperture_map[requirement["aperture_id"]]
        records = _aperture_records(precomp, aperture, eligible_ids)
        prepared.append((aperture, requirement, records))
    ordered = sorted(eligible_ids)
    for size in range(1, min(product["max_frames"], len(ordered)) + 1):
        for subset in combinations(ordered, size):
            ok = True
            for aperture, requirement, records in prepared:
                if _evaluate(aperture, requirement, subset, records, dark_variance) is None:
                    ok = False
                    break
            if ok:
                options.append(subset)
    return options


def _prepare_requirements(product, aperture_map, precomp, eligible_ids, dark_variance):
    prepared = []
    for requirement in product["requirements"]:
        aperture = aperture_map[requirement["aperture_id"]]
        disk, inside, _records = _aperture_records(precomp, aperture, eligible_ids)
        if len(inside) != len(disk):
            return None
        prepared.append({"requirement": requirement, "inside": inside,
                         "minimum": requirement["min_exposure_s"]})
    return prepared


def _build_requirement_state(precomp, prepared, eligible_ids, dark_variance):
    """Integer accumulator layout for one requirement's DFS.

    Exact identity per output pixel o, with c_o the product of the squared
    flats of its contributing detector sources:

        variance_o = M^2 / (c_o D^2 E_o^2) * N_o
        N_o = c_o D^2 * sum(frame_var/flat^2)
              + sum_source (c_o/flat_s^2) * darkvar_s * L_s^2

    so every running quantity is an integer and the aperture threshold becomes
    sum_o N_o / (c_o E_o^2) <= max_variance * D^2 / M^2, a single comparison.
    """
    inside = prepared["inside"]
    mean_flat = precomp["mean_flat"]
    dark_exposure = precomp["dark_exposure"]
    source_flat = {output: {} for output in inside}
    samples_by_frame = {}
    for frame_id in sorted(eligible_ids):
        samples = []
        for contribution in precomp["frames"][frame_id]["contributions"]:
            (out_x, out_y, det_x, det_y, exposure, _cal, _qf, _dark_h,
             flat, frame_raw_variance, source_dark_variance) = contribution
            output = (out_x, out_y)
            if output not in inside:
                continue
            source = (det_x, det_y)
            source_flat[output][source] = flat
            samples.append((output, source, exposure, flat,
                            frame_raw_variance, source_dark_variance))
        if samples:
            samples_by_frame[frame_id] = samples
    scale = {}
    source_weight = {}
    for output, flats in source_flat.items():
        c_o = 1
        for flat in set(flats.values()):
            c_o *= flat * flat
        scale[output] = c_o
        source_weight[output] = {
            source: (c_o // (flat * flat)) * dark_variance[source[1]][source[0]]
            for source, flat in flats.items()}
    for frame_id, samples in samples_by_frame.items():
        weighted = []
        for output, source, exposure, flat, frame_raw_variance, _dv in samples:
            raw_w = (scale[output] // (flat * flat)) * frame_raw_variance \
                * dark_exposure * dark_exposure
            weighted.append((output, source, exposure, raw_w,
                             source_weight[output][source]))
        samples_by_frame[frame_id] = weighted
    threshold = prepared["requirement"]["max_variance"] \
        * dark_exposure * dark_exposure / mean_flat ** 2
    return {
        "inside": inside,
        "samples_by_frame": samples_by_frame,
        "scale": scale, "threshold": threshold,
        "minimum": prepared["minimum"]}


def minimal_feasible_masks(product, eligible_ids, aperture_map, precomp,
                           dark_variance, bit_of):
    """Inclusion-minimal feasible frame bitmasks via flat-array depth-first search.

    Per requirement every disk output is an integer index, so the search path
    mutates integer lists (exposure, raw/dark numerators and per-source exposure)
    instead of nested dictionaries. Feasibility is evaluated exactly once per
    (requirement, mask) and memoized; a strict superset of a feasible mask is
    pruned because it cannot be inclusion-minimal.
    """
    prepared_all = _prepare_requirements(product, aperture_map, precomp,
                                         eligible_ids, dark_variance)
    if prepared_all is None:
        return []
    built_states = []
    search_frames = set()
    for prepared in prepared_all:
        state = _build_requirement_state(precomp, prepared, eligible_ids,
                                         dark_variance)
        inside = state["inside"]
        output_index = {output: index for index, output in enumerate(sorted(inside))}
        size = len(inside)
        source_index = [{} for _ in range(size)]
        for frame_id, rows in state["samples_by_frame"].items():
            for output, source, _exposure, _raw_w, _dark_w in rows:
                o = output_index[output]
                if source not in source_index[o]:
                    source_index[o][source] = len(source_index[o])
        frame_samples = []
        contributing = sorted(state["samples_by_frame"])
        search_frames.update(contributing)
        for frame_id in contributing:
            rows = []
            for output, source, exposure, raw_w, dark_w in \
                    state["samples_by_frame"][frame_id]:
                o = output_index[output]
                rows.append((o, source_index[o][source], exposure, raw_w, dark_w))
            frame_samples.append((frame_id, rows))
        built_states.append({
            "state": state, "size": size, "output_index": output_index,
            "source_index": source_index,
            "frames": dict(frame_samples),
            "exposure": [0] * size,
            "raw_numerator": [0] * size,
            "dark_numerator": [0] * size,
            "source_exposure": [[0] * len(mapping) for mapping in source_index],
            "cache": {}})
    ordered = sorted(search_frames)
    limit = min(product["max_frames"], len(ordered))
    minimal_masks = []

    def evaluate(built, mask):
        cached = built["cache"].get(mask)
        if cached is not None:
            return cached
        state = built["state"]
        minimum = state["minimum"]
        exposure = built["exposure"]
        size = built["size"]
        if any(exposure[o] < minimum for o in range(size)):
            result = False
        else:
            grouped = {}
            scale = state["scale"]
            raw_numerator = built["raw_numerator"]
            dark_numerator = built["dark_numerator"]
            for o, output in enumerate(sorted(state["inside"])):
                key = (scale[output], exposure[o])
                grouped[key] = grouped.get(key, 0) + raw_numerator[o] + dark_numerator[o]
            lhs = Fraction(0)
            for (c_o, total), numerator in grouped.items():
                lhs += Fraction(numerator, c_o * total * total)
            result = lhs <= state["threshold"]
        built["cache"][mask] = result
        return result

    def search(position, chosen, used_mask):
        if any((used_mask & found) == found for found in minimal_masks):
            return
        if chosen and all(evaluate(built, used_mask) for built in built_states):
            minimal_masks.append(used_mask)
            return
        if chosen == limit or position == len(ordered):
            return
        frame_id = ordered[position]
        frame_mask = bit_of[frame_id]
        # explore subsets that skip this frame first, keeping mask order stable
        search(position + 1, chosen, used_mask)
        applied = []
        for built in built_states:
            touched = []
            for o, s, sample_exposure, raw_w, dark_w in \
                    built["frames"].get(frame_id, ()):
                exposure = built["exposure"]
                raw_numerator = built["raw_numerator"]
                dark_numerator = built["dark_numerator"]
                source_exposure = built["source_exposure"][o]
                exposure[o] += sample_exposure
                raw_numerator[o] += raw_w
                previous = source_exposure[s]
                updated = previous + sample_exposure
                dark_numerator[o] += dark_w * (
                    updated * updated - previous * previous)
                source_exposure[s] = updated
                touched.append((o, s, sample_exposure, raw_w, dark_w,
                                previous, updated))
            applied.append(touched)
        search(position + 1, chosen + 1, used_mask | frame_mask)
        for built, touched in zip(built_states, applied):
            exposure = built["exposure"]
            raw_numerator = built["raw_numerator"]
            dark_numerator = built["dark_numerator"]
            for o, s, sample_exposure, raw_w, dark_w, previous, updated in touched:
                exposure[o] -= sample_exposure
                raw_numerator[o] -= raw_w
                dark_numerator[o] -= dark_w * (
                    updated * updated - previous * previous)
                built["source_exposure"][o][s] = previous

    search(0, 0, 0)
    minimal_masks.sort()
    return minimal_masks


def assign(products, frames, aperture_map, precomp, dark_variance):
    """Exact optimal disjoint delivery via frame-bitmask dynamic programming.

    Per product only inclusion-minimal feasible subsets are retained: a strict
    superset costs at least as much exposure and blocks strictly more frames,
    so it can never occur in an optimal assignment. The DP keeps, for every set
    of used frame masks, the lexicographically smallest partial objective
    (-priority, -delivered count, exposure cost, partial signature).
    """
    product_ids = sorted(products)
    all_frame_ids = sorted(frames)
    frame_count = len(all_frame_ids)
    bit_of = {frame_id: 1 << index for index, frame_id in enumerate(all_frame_ids)}
    frame_cost = [frames[frame_id]["exposure_s"] for frame_id in all_frame_ids]
    options = {}
    for product_id in product_ids:
        product = products[product_id]
        eligible_ids = [frame_id for frame_id in all_frame_ids
                        if product["start_s"] <= frames[frame_id]["observed_at_s"]
                        < product["end_s"]]
        options[product_id] = minimal_feasible_masks(
            product, eligible_ids, aperture_map, precomp, dark_variance, bit_of)

    subset_cost = {}
    subset_signature = {}
    for subset_masks in options.values():
        for mask in subset_masks:
            if mask not in subset_cost:
                subset_cost[mask] = sum(
                    frame_cost[index] for index in range(frame_count)
                    if mask & (1 << index))
                subset_signature[mask] = tuple(
                    all_frame_ids[index] for index in range(frame_count)
                    if mask & (1 << index))

    # state: used_mask -> (objective tuple, choices tuple of per-product masks)
    states = {0: ((0, 0, 0, ()), ())}
    for index, product_id in enumerate(product_ids):
        priority = products[product_id]["priority"]
        next_states = {}

        def consider(used_mask, objective, choices):
            incumbent = next_states.get(used_mask)
            if incumbent is None or objective < incumbent[0]:
                next_states[used_mask] = (objective, choices)

        for used_mask, (objective, choices) in states.items():
            neg_priority, neg_count, cost, signature = objective
            consider(used_mask,
                     (neg_priority, neg_count, cost, signature + ((),)),
                     choices + (0,))
            for subset_mask in options[product_id]:
                if used_mask & subset_mask:
                    continue
                consider(
                    used_mask | subset_mask,
                    (neg_priority - priority, neg_count - 1,
                     cost + subset_cost[subset_mask],
                     signature + (subset_signature[subset_mask],)),
                    choices + (subset_mask,))
        states = next_states

    _, best_choices = min(states.values(), key=lambda entry: entry[0])
    assignment = {}
    for product_id, subset_mask in zip(product_ids, best_choices):
        if subset_mask:
            assignment[product_id] = [all_frame_ids[index]
                                      for index in range(frame_count)
                                      if subset_mask & (1 << index)]
    return assignment


def stack(product_frames, precomp, data, width, height, apertures, required_apertures):
    """Build the schema v1 stack, variance image and aperture uncertainty for one product."""
    weighted = [[Fraction(0) for _ in range(width)] for _ in range(height)]
    exposure = [[0 for _ in range(width)] for _ in range(height)]
    counts = [[0 for _ in range(width)] for _ in range(height)]
    raw_accum = [[Fraction(0) for _ in range(width)] for _ in range(height)]
    dark_accum = [[{} for _ in range(width)] for _ in range(height)]
    audits = []
    for frame_id in sorted(product_frames):
        bundle = precomp["frames"][frame_id]
        audits.append(bundle["audit"])
        for contribution in bundle["contributions"]:
            (out_x, out_y, det_x, det_y, frame_exposure,
             cal_rate, qf_term, dark_h) = contribution[:8]
            weighted[out_y][out_x] += cal_rate * frame_exposure
            exposure[out_y][out_x] += frame_exposure
            counts[out_y][out_x] += 1
            raw_accum[out_y][out_x] += qf_term
            source = (det_x, det_y)
            previous = dark_accum[out_y][out_x].get(source)
            if previous is None:
                dark_accum[out_y][out_x][source] = (frame_exposure, dark_h)
            else:
                dark_accum[out_y][out_x][source] = (previous[0] + frame_exposure, dark_h)
    image = [[weighted[y][x] / exposure[y][x] if exposure[y][x] else None
              for x in range(width)] for y in range(height)]
    variance_image = [[None for _ in range(width)] for _ in range(height)]
    aperture_uncertainty = {}
    disk_sets = {}
    for aperture_id in required_apertures:
        aperture_uncertainty[aperture_id] = {"dark_g": {}, "raw": Fraction(0),
                                             "output_qf": {}}
        disk_sets[aperture_id] = {(x, y) for x, y in nominal_disk(apertures[aperture_id])
                                  if 0 <= x < width and 0 <= y < height}
    for y in range(height):
        for x in range(width):
            if not exposure[y][x]:
                continue
            total = Fraction(exposure[y][x])
            qf = raw_accum[y][x] / total ** 2
            qd = Fraction(0)
            for (det_x, det_y), (local_exposure, dark_h) in dark_accum[y][x].items():
                g = dark_h * Fraction(local_exposure, total)
                qd += Fraction(data["campaign"]["dark_variance"][det_y][det_x]) * g ** 2
            variance_image[y][x] = qf + qd
            for aperture_id in required_apertures:
                if (x, y) in disk_sets[aperture_id]:
                    uncertainty = aperture_uncertainty[aperture_id]
                    uncertainty["raw"] += qf
                    uncertainty["output_qf"][(x, y)] = qf
                    for (det_x, det_y), (local_exposure, dark_h) \
                            in dark_accum[y][x].items():
                        g = dark_h * Fraction(local_exposure, total)
                        uncertainty["dark_g"][(det_x, det_y)] = \
                            uncertainty["dark_g"].get((det_x, det_y), Fraction(0)) + g
    measurements = measure(image, apertures)
    stack = {"rate_image": [[str(v) if v is not None else None for v in row] for row in image],
             "exposure_s": exposure, "contributor_count": counts, "frame_audit": audits,
             "apertures": measurements,
             "summary": {"frames": len(product_frames),
                         "covered_pixels": sum(v is not None for row in image for v in row),
                         "accepted_contributions": sum(row["accepted_pixels"] for row in audits),
                         "complete_apertures": sum(a["complete"] for a in measurements)}}
    return stack, variance_image, aperture_uncertainty


def covariance_matrix(order, deliveries, dark_variance):
    """Square exact covariance matrix for requirement measurements in measurement order."""
    size = len(order)
    matrix = [[Fraction(0) for _ in range(size)] for _ in range(size)]
    for i in range(size):
        for j in range(i, size):
            left = deliveries[order[i]]
            right = deliveries[order[j]]
            value = Fraction(0)
            for (x, y), g in left["dark_g"].items():
                other = right["dark_g"].get((x, y))
                if other is not None:
                    value += Fraction(dark_variance[y][x]) * g * other
            if order[i][0] == order[j][0]:
                for output, qf in left["output_qf"].items():
                    if output in right["output_qf"]:
                        value += qf
            matrix[i][j] = value
            matrix[j][i] = value
    return matrix
