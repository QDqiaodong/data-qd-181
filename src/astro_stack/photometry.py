def nominal_disk(aperture):
    x0, y0, radius = aperture["x"], aperture["y"], aperture["radius"]
    return [(x, y) for y in range(y0 - radius, y0 + radius + 1)
            for x in range(x0 - radius, x0 + radius + 1)
            if (x - x0)**2 + (y - y0)**2 <= radius**2]


def measure(image, apertures):
    height, width = len(image), len(image[0])
    results = []
    for aperture_id, aperture in sorted(apertures.items()):
        nominal = nominal_disk(aperture)
        inside = [(x, y) for x, y in nominal if 0 <= x < width and 0 <= y < height]
        valid = [image[y][x] for x, y in inside if image[y][x] is not None]
        results.append({"aperture_id": aperture_id, "nominal_pixels": len(nominal),
                        "outside_pixels": len(nominal) - len(inside),
                        "missing_pixels": len(inside) - len(valid), "measured_pixels": len(valid),
                        "sum_rate": str(sum(valid)) if valid else None,
                        "complete": len(valid) == len(nominal)})
    return results
