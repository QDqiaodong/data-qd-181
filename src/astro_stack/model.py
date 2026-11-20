"""Image coordinates are detector row/column, not celestial coordinates."""
from .io import fields, integer, records, rows


def matrix(value, width, height, name, boolean=False):
    rows(value, name, height, height)
    for row in value:
        rows(row, name, width, width)
        for pixel in row:
            if boolean:
                if type(pixel) is not bool:
                    raise ValueError(f"{name} requires boolean pixels")
            else:
                integer(pixel, name, 0, 10**9)


def validate(data):
    fields(data, ("width", "height", "dark_exposure_s", "dark", "flat", "bad_mask", "frames", "apertures"))
    width = integer(data["width"], "width", 1, 64)
    height = integer(data["height"], "height", 1, 64)
    integer(data["dark_exposure_s"], "dark_exposure_s", 1, 86400)
    for name in ("dark", "flat"):
        matrix(data[name], width, height, name)
    matrix(data["bad_mask"], width, height, "bad_mask", True)
    usable = [data["flat"][y][x] for y in range(height) for x in range(width)
              if not data["bad_mask"][y][x] and data["flat"][y][x] > 0]
    if not usable:
        raise ValueError("flat has no usable unmasked positive pixels")
    frames = records(data["frames"], "frame", 1, 50)
    for frame in frames.values():
        fields(frame, ("id", "exposure_s", "dx", "dy", "saturation", "pixels"))
        integer(frame["exposure_s"], "exposure_s", 1, 86400)
        integer(frame["dx"], "dx", -64, 64)
        integer(frame["dy"], "dy", -64, 64)
        integer(frame["saturation"], "saturation", 1, 10**9)
        matrix(frame["pixels"], width, height, "frame pixels")
    apertures = records(data["apertures"], "aperture", 0, 100)
    for aperture in apertures.values():
        fields(aperture, ("id", "x", "y", "radius"))
        integer(aperture["x"], "x", 0, width - 1)
        integer(aperture["y"], "y", 0, height - 1)
        integer(aperture["radius"], "radius", 0, 64)
    return width, height, frames, apertures, usable
