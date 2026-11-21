from fractions import Fraction

from .model import validate
from .calibrate import combine
from .photometry import measure
from .campaign import assign, covariance_matrix, precompute, stack


def run(data):
    width, height, frames, apertures, usable, campaign = validate(data)
    if campaign is None:
        image, exposure, counts, audits = combine(data, width, height, frames, usable)
        measurements = measure(image, apertures)
        return {"schema_version": 1,
                "rate_image": [[str(v) if v is not None else None for v in row] for row in image],
                "exposure_s": exposure, "contributor_count": counts, "frame_audit": audits,
                "apertures": measurements,
                "summary": {"frames": len(frames), "covered_pixels": sum(v is not None for row in image for v in row),
                            "accepted_contributions": sum(row["accepted_pixels"] for row in audits),
                            "complete_apertures": sum(a["complete"] for a in measurements)}}
    return _run_campaign(data, width, height, frames, apertures, usable, campaign)


def _run_campaign(data, width, height, frames, apertures, usable, campaign):
    dark_variance = campaign["dark_variance"]
    precomp = precompute(data, width, height, frames, usable)
    aperture_map = {aperture_id: dict(aperture, _width=width, _height=height)
                    for aperture_id, aperture in apertures.items()}
    products = {product["id"]: product for product in campaign["products"]}
    assignment = assign(products, frames, aperture_map, precomp, dark_variance)

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
        used_exposure_s += sum(frames[frame_id]["exposure_s"] for frame_id in subset)
        product_frame_map = {frame_id: frames[frame_id] for frame_id in subset}
        required_apertures = sorted(requirement["aperture_id"]
                                    for requirement in product["requirements"])
        stack_report, variance_image, aperture_uncertainty = stack(
            product_frame_map, precomp, data, width, height, apertures, required_apertures)
        aperture_variances = []
        for aperture_id in required_apertures:
            uncertainty = aperture_uncertainty[aperture_id]
            variance = uncertainty["raw"]
            for (x, y), coefficient in uncertainty["dark_g"].items():
                variance += Fraction(dark_variance[y][x]) * coefficient ** 2
            aperture_variances.append({"aperture_id": aperture_id, "variance": str(variance)})
            deliveries[(product_id, aperture_id)] = uncertainty
            measurement_order.append((product_id, aperture_id))
        product_entries.append({
            "product_id": product_id,
            "frame_ids": sorted(subset),
            "stack": stack_report,
            "uncertainty": {
                "variance_image": [[str(value) if value is not None else None for value in row]
                                   for row in variance_image],
                "aperture_variances": aperture_variances}})

    measurement_order.sort()
    covariances = covariance_matrix(measurement_order, deliveries, dark_variance)
    unfulfilled = sorted(set(products) - set(assignment))
    unused = sorted(set(frames) - used_frames)
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
