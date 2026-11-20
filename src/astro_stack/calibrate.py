from fractions import Fraction


def combine(data, width, height, frames, usable):
    mean_flat = Fraction(sum(usable), len(usable))
    weighted = [[Fraction(0) for _ in range(width)] for _ in range(height)]
    exposure = [[0 for _ in range(width)] for _ in range(height)]
    counts = [[0 for _ in range(width)] for _ in range(height)]
    audits = []
    for frame_id, frame in sorted(frames.items()):
        audit = {"frame_id": frame_id, "accepted_pixels": 0, "masked_pixels": 0,
                 "zero_flat_pixels": 0, "saturated_pixels": 0, "outside_pixels": 0}
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
                dark_rate = Fraction(data["dark"][y][x], data["dark_exposure_s"])
                calibrated_rate = (Fraction(pixel, frame["exposure_s"]) - dark_rate) * mean_flat / flat
                weighted[out_y][out_x] += calibrated_rate * frame["exposure_s"]
                exposure[out_y][out_x] += frame["exposure_s"]
                counts[out_y][out_x] += 1
                audit["accepted_pixels"] += 1
        audits.append(audit)
    image = [[weighted[y][x] / exposure[y][x] if exposure[y][x] else None
              for x in range(width)] for y in range(height)]
    return image, exposure, counts, audits
