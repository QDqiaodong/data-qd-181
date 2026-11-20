from .model import validate
from .calibrate import combine
from .photometry import measure


def run(data):
    width, height, frames, apertures, usable = validate(data)
    image, exposure, counts, audits = combine(data, width, height, frames, usable)
    measurements = measure(image, apertures)
    return {"schema_version": 1,
            "rate_image": [[str(v) if v is not None else None for v in row] for row in image],
            "exposure_s": exposure, "contributor_count": counts, "frame_audit": audits,
            "apertures": measurements,
            "summary": {"frames": len(frames), "covered_pixels": sum(v is not None for row in image for v in row),
                        "accepted_contributions": sum(row["accepted_pixels"] for row in audits),
                        "complete_apertures": sum(a["complete"] for a in measurements)}}
