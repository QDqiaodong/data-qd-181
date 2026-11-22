"""Background-subtracted stack engine shared by classic and campaign reports.

One engine is built for a concrete frame set (all ready frames for a classic
archive, or one product's assigned subset).  It reproduces the existing local
exposure-weighted stack and nominal circular apertures, while additionally
carrying every statistic as a :class:`~astro_stack.sky.model.Form` over the
independent base noise variables so that the final subtracted measurements
propagate all raw/dark correlations, including correlations with the fitted
plane.
"""
from fractions import Fraction

from ..photometry import measure, nominal_disk
from .model import Form, covariance


class StackEngine:
    def __init__(self, frame_ids, precomp, width, height, aperture_records,
                 plane, raw_variance_of, dark_variance_of):
        self.frame_ids = sorted(frame_ids)
        self.width = width
        self.height = height
        self.aperture_records = aperture_records
        self.plane = plane
        self.raw_variance_of = raw_variance_of
        self.dark_variance_of = dark_variance_of
        self.weighted = [[Fraction(0) for _ in range(width)] for _ in range(height)]
        self.exposure = [[0 for _ in range(width)] for _ in range(height)]
        self.counts = [[0 for _ in range(width)] for _ in range(height)]
        self.forms = [[Form(Fraction(0)) for _ in range(width)]
                      for _ in range(height)]
        self.audits = []
        for frame_id in self.frame_ids:
            bundle = precomp["frames"][frame_id]
            self.audits.append(bundle["audit"])
            for contribution in bundle["contributions"]:
                out_x, out_y = contribution[0], contribution[1]
                frame_exposure = contribution[4]
                cal_rate = contribution[5]
                sample_form = self.plane.sample_form(frame_id, contribution)
                self.weighted[out_y][out_x] += cal_rate * frame_exposure
                self.exposure[out_y][out_x] += frame_exposure
                self.counts[out_y][out_x] += 1
                self.forms[out_y][out_x] = \
                    self.forms[out_y][out_x] + sample_form.scale(frame_exposure)
        self.image = [
            [self.weighted[y][x] / self.exposure[y][x] if self.exposure[y][x]
             else None for x in range(width)] for y in range(height)]
        self.pixel_forms = [[None for _ in range(width)] for _ in range(height)]
        for y in range(height):
            for x in range(width):
                if self.exposure[y][x]:
                    self.pixel_forms[y][x] = \
                        self.forms[y][x].scale(Fraction(1, self.exposure[y][x]))

    # ------------------------------------------------------------- pixels
    def pixel_status_table(self):
        table = [["ok" for _ in range(self.width)] for _ in range(self.height)]
        for y in range(self.height):
            for x in range(self.width):
                if not self.exposure[y][x]:
                    table[y][x] = "uncovered"
                elif self.plane.solve_target(
                        [Fraction(1), Fraction(x), Fraction(y)]) is None:
                    table[y][x] = "unestimable"
        return table

    def subtracted_pixel(self, x, y):
        """Return (status, rate string|None, variance string|None, Form|None)."""
        if not self.exposure[y][x]:
            return "uncovered", None, None, None
        target = [Fraction(1), Fraction(x), Fraction(y)]
        solved = self.plane.solve_target(target)
        science_form = self.pixel_forms[y][x]
        if solved is None:
            return "unestimable", None, None, None
        _weights, background_form = solved
        subtracted = science_form - background_form
        return ("ok", str(subtracted.mean),
                str(subtracted.variance(self.raw_variance_of,
                                        self.dark_variance_of)),
                subtracted)

    def images(self):
        rate_image = [[None for _ in range(self.width)]
                      for _ in range(self.height)]
        variance_image = [[None for _ in range(self.width)]
                          for _ in range(self.height)]
        pixel_status = self.pixel_status_table()
        pixel_forms = [[None for _ in range(self.width)]
                       for _ in range(self.height)]
        for y in range(self.height):
            for x in range(self.width):
                status, rate, variance, form = self.subtracted_pixel(x, y)
                rate_image[y][x] = rate
                variance_image[y][x] = variance
                pixel_forms[y][x] = form
        return rate_image, variance_image, pixel_status, pixel_forms

    # ---------------------------------------------------------- apertures
    def aperture_records_with_status(self):
        """Physical coverage plus whole-aperture background estimability.

        Returns a list in aperture-id order; each entry carries the original
        photometry fields, a ``status`` and the subtracted aperture Form for
        ``ok`` apertures (None otherwise).
        """
        measurements = measure(self.image, self.aperture_records)
        results = []
        for entry in measurements:
            aperture = self.aperture_records[entry["aperture_id"]]
            disk = nominal_disk(aperture)
            inside = [(x, y) for x, y in disk
                      if 0 <= x < self.width and 0 <= y < self.height]
            result = dict(entry)
            if not entry["complete"]:
                result["status"] = "incomplete"
                result["sum_rate"] = None
                result["form"] = None
                results.append(result)
                continue
            # identical unit weights for the science measurement q (a sum of
            # the output-pixel rates, exactly like photometry.measure) and the
            # plane target l*beta; the aperture is one linear object and is
            # judged estimable as a whole, never pixel by pixel
            science_form = Form(Fraction(0))
            target = [Fraction(0), Fraction(0), Fraction(0)]
            for x, y in inside:
                science_form = science_form + self.pixel_forms[y][x]
                target[0] += Fraction(1)
                target[1] += Fraction(x)
                target[2] += Fraction(y)
            solved = self.plane.solve_target(target)
            if solved is None:
                result["status"] = "unestimable"
                result["sum_rate"] = None
                result["form"] = None
                results.append(result)
                continue
            _weights_vec, background_form = solved
            subtracted = science_form - background_form
            result["status"] = "ok"
            result["sum_rate"] = str(subtracted.mean)
            result["form"] = subtracted
            results.append(result)
        return results

    def stack_report(self):
        """The stack dict (schema v1 fields plus pixel_status and statuses)."""
        rate_image, variance_image, pixel_status, _forms = self.images()
        apertures = []
        aperture_forms = {}
        for result in self.aperture_records_with_status():
            aperture_forms[result["aperture_id"]] = result.pop("form")
            apertures.append(result)
        stack = {
            "rate_image": rate_image,
            "exposure_s": [list(row) for row in self.exposure],
            "contributor_count": [list(row) for row in self.counts],
            "pixel_status": pixel_status,
            "frame_audit": [dict(audit) for audit in self.audits],
            "apertures": apertures,
            "summary": {
                "frames": len(self.frame_ids),
                "covered_pixels": sum(1 for row in self.exposure
                                      for value in row if value),
                "accepted_contributions": sum(
                    row["accepted_pixels"] for row in self.audits),
                "complete_apertures": sum(1 for row in apertures
                                          if row["complete"])}}
        return stack, variance_image, aperture_forms

    def aperture_covariance(self, aperture_order, aperture_forms):
        """Covariance square; non-ok apertures keep a null row and column."""
        size = len(aperture_order)
        matrix = [[None for _ in range(size)] for _ in range(size)]
        ok_ids = [aperture_id for aperture_id in aperture_order
                  if aperture_forms.get(aperture_id) is not None]
        index_of = {aperture_id: index
                    for index, aperture_id in enumerate(aperture_order)}
        for left_id in ok_ids:
            for right_id in ok_ids:
                matrix[index_of[left_id]][index_of[right_id]] = str(covariance(
                    aperture_forms[left_id], aperture_forms[right_id],
                    self.raw_variance_of, self.dark_variance_of))
        return matrix
