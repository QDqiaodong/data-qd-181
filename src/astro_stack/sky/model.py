"""Auditable spatial background model and background-subtracted delivery.

A single fixed plane ``b(x, y) = beta0 + beta_x*x + beta_y*y`` is fitted at one
committed archive revision from caller-nominated empty-sky samples.  All rank,
support, estimability and minimum-variance decisions are exact rational linear
algebra (see :mod:`astro_stack.sky.linalg`); no epsilon, floating pseudo-inverse
or arbitrary slope value is ever introduced.

Random-source identities follow the archive contract:

* a raw frame pixel is ``("raw", frame_id, detector_x, detector_y)``;
* a dark pixel is ``("dark", dark_id, detector_x, detector_y)``.

Different identities are independent even when their numeric values coincide.
"""
from fractions import Fraction

from ..io import fields
from . import linalg


class SkyError(ValueError):
    """Identifiable sky-analysis failure (bad request or noise-support mismatch)."""


class SkyInconsistent(SkyError):
    """The observed samples do not lie in the model's exact noise support."""

    def __init__(self):
        super().__init__("sky_inconsistent: samples are outside the noise "
                         "support of the background plane")


# --------------------------------------------------------------------------
# Linear forms over the independent base noise variables
# --------------------------------------------------------------------------
class Form:
    """A linear statistic ``mean + sum_s coefficient_s * noise_s``.

    ``raw`` and ``dark`` hold the coefficients keyed by the two independent
    source-name spaces; ``mean`` is the deterministic calibrated value.
    """

    __slots__ = ("mean", "raw", "dark")

    def __init__(self, mean=Fraction(0), raw=None, dark=None):
        self.mean = mean
        self.raw = raw if raw is not None else {}
        self.dark = dark if dark is not None else {}

    def add_raw(self, key, coefficient):
        self.raw[key] = self.raw.get(key, Fraction(0)) + coefficient

    def add_dark(self, key, coefficient):
        self.dark[key] = self.dark.get(key, Fraction(0)) + coefficient

    def __add__(self, other):
        raw = dict(self.raw)
        for key, coefficient in other.raw.items():
            raw[key] = raw.get(key, Fraction(0)) + coefficient
        dark = dict(self.dark)
        for key, coefficient in other.dark.items():
            dark[key] = dark.get(key, Fraction(0)) + coefficient
        return Form(self.mean + other.mean, raw, dark)

    def __sub__(self, other):
        raw = dict(self.raw)
        for key, coefficient in other.raw.items():
            raw[key] = raw.get(key, Fraction(0)) - coefficient
        dark = dict(self.dark)
        for key, coefficient in other.dark.items():
            dark[key] = dark.get(key, Fraction(0)) - coefficient
        return Form(self.mean - other.mean, raw, dark)

    def scale(self, factor):
        return Form(
            self.mean * factor,
            {key: factor * coefficient for key, coefficient in self.raw.items()},
            {key: factor * coefficient for key, coefficient in self.dark.items()})

    def variance(self, raw_variance_of, dark_variance_of):
        total = Fraction(0)
        for key, coefficient in self.raw.items():
            _kind, frame_id, x, y = key
            total += Fraction(raw_variance_of(frame_id, x, y)) * coefficient ** 2
        for key, coefficient in self.dark.items():
            total += Fraction(dark_variance_of(key)) * coefficient ** 2
        return total


def covariance(left, right, raw_variance_of, dark_variance_of):
    """Exact covariance of two linear forms, including every cross term."""
    total = Fraction(0)
    for key, coefficient in left.raw.items():
        other = right.raw.get(key)
        if other is not None:
            _kind, frame_id, x, y = key
            total += Fraction(raw_variance_of(frame_id, x, y)) * coefficient * other
    for key, coefficient in left.dark.items():
        other = right.dark.get(key)
        if other is not None:
            total += Fraction(dark_variance_of(key)) * coefficient * other
    return total


def contribution_form(frame_id, contribution, dark_exposure_of):
    """Linear form of one calibrated detector sample in the base noise variables.

    Calibration is ``(raw/exposure - dark/dark_exposure) * M/flat``, so the
    calibrated rate equals its deterministic observed value minus independent
    zero-mean raw and dark noise terms with coefficients::

        raw coefficient  = (M/flat) / exposure
        dark coefficient = -(M/flat) / (flat * dark_exposure) = -dark_h

    The frame exposure divides only the raw count; the dark subtraction's
    coefficient is the stored ``dark_h`` and carries no extra frame-exposure
    factor, matching the archive's existing exact variance propagation.
    ``dark_exposure_of(dark_id)`` supplies the master dark's exposure so the
    flat factor M/flat can be recovered exactly as dark_h * dark_exposure.
    """
    (_out_x, _out_y, det_x, det_y, exposure, cal_rate, _qf_term, dark_h,
     _flat, raw_variance, _dark_variance, dark_id) = contribution
    form = Form(cal_rate)
    if raw_variance:
        flat_record_factor = dark_h * dark_exposure_of(dark_id)
        form.add_raw(("raw", frame_id, det_x, det_y),
                     flat_record_factor / exposure)
    form.add_dark(("dark", dark_id, det_x, det_y), -dark_h)
    return form


# --------------------------------------------------------------------------
# Request validation and sample preparation
# --------------------------------------------------------------------------
def _strict_int(value, name):
    # bool is a subclass of int and must be rejected explicitly
    if type(value) is not int:
        raise SkyError(f"{name} requires a non-boolean integer")
    return value


def validate_specification(specification, snapshot, bindings, precomp, width, height):
    """Return sky samples sorted by (frame_id, detector_y, detector_x).

    Every check is whole-request: any failure raises and no fallback sample or
    calibration is ever substituted.
    """
    if not isinstance(specification, dict):
        raise SkyError("specification must be an object")
    fields(specification, ("sky_samples",))
    entries = specification["sky_samples"]
    if not isinstance(entries, list) or not 1 <= len(entries) <= 64:
        raise SkyError("sky_samples requires an array of 1..64 entries")
    parsed = []
    seen = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise SkyError(f"sky_samples[{index}] must be an object")
        fields(entry, ("frame_id", "detector_x", "detector_y"))
        frame_id = entry["frame_id"]
        if not isinstance(frame_id, str) or not frame_id:
            raise SkyError(f"sky_samples[{index}].frame_id requires a frame id string")
        x = _strict_int(entry["detector_x"], f"sky_samples[{index}].detector_x")
        y = _strict_int(entry["detector_y"], f"sky_samples[{index}].detector_y")
        if not (0 <= x < width and 0 <= y < height):
            raise SkyError(f"sky_samples[{index}] coordinates are outside the image")
        identity = (frame_id, x, y)
        if identity in seen:
            raise SkyError(f"duplicate sky sample on frame {frame_id!r} at ({x},{y})")
        seen.add(identity)
        binding = bindings.get(frame_id)
        frame = snapshot.frames.get(frame_id)
        if frame is None or frame["withdrawn"] or binding is None \
                or binding.get("retracted"):
            raise SkyError(f"sky sample frame is not ready or unknown: {frame_id!r}")
        bundle = precomp["frames"].get(frame_id)
        if bundle is None:
            raise SkyError(f"sky sample frame is not ready: {frame_id!r}")
        match = next((candidate for candidate in bundle["contributions"]
                      if candidate[2] == x and candidate[3] == y), None)
        if match is None:
            raise SkyError(
                f"sky sample ({frame_id},{x},{y}) fails mask, zero-flat, "
                "saturation or registered-output checks")
        parsed.append({"frame_id": frame_id, "detector_x": x,
                       "detector_y": y, "contribution": match,
                       "binding": binding})
    parsed.sort(key=lambda item: (item["frame_id"], item["detector_y"],
                                  item["detector_x"]))
    return parsed


# --------------------------------------------------------------------------
# Fitted plane and its BLUE machinery
# --------------------------------------------------------------------------
class SkyPlane:
    """The fixed fitted plane for one revision plus its exact linear solver."""

    def __init__(self, samples, raw_variance_of, dark_variance_of,
                 dark_exposure_of):
        self.samples = samples
        self.raw_variance_of = raw_variance_of
        self.dark_variance_of = dark_variance_of
        self.dark_exposure_of = dark_exposure_of
        self.forms = [
            contribution_form(item["frame_id"], item["contribution"],
                              dark_exposure_of)
            for item in samples]
        self.y = [form.mean for form in self.forms]
        self.x_rows = [[Fraction(1), Fraction(item["output_x"]),
                        Fraction(item["output_y"])] for item in samples]
        self.size = len(samples)
        self.covariance = self._sample_covariance()
        self.basis = linalg.row_space_basis(self.x_rows)
        self.rank = len(self.basis)
        self._kkt = self._build_kkt()
        self._check_support()
        self._blue_cache = {}

    def _sample_covariance(self):
        size = len(self.forms)
        matrix = [[Fraction(0) for _ in range(size)] for _ in range(size)]
        for i in range(size):
            for j in range(i, size):
                value = covariance(self.forms[i], self.forms[j],
                                   self.raw_variance_of, self.dark_variance_of)
                matrix[i][j] = value
                matrix[j][i] = value
        return matrix

    def _build_kkt(self):
        n = self.size
        k = 3
        top = [self.covariance[i] + self.x_rows[i] for i in range(n)]
        bottom = [[self.x_rows[row][col] for row in range(n)]
                  + [Fraction(0) for _ in range(k)] for col in range(k)]
        return top + bottom

    def _check_support(self):
        # y must equal X beta + e with e in col(C): solve [C X; X' 0][u;b]=[y;0]
        n = self.size
        augmented = [self._kkt[i] + [self.y[i] if i < n else Fraction(0)]
                      for i in range(n + 3)]
        if not linalg.is_consistent(augmented):
            raise SkyInconsistent()

    def sample_form(self, frame_id, contribution):
        """Linear noise form of any calibrated detector sample.

        The same identity returns the same random variable whether the sample
        participates in the fit, in a science measurement, or in both.
        """
        return contribution_form(frame_id, contribution,
                                 self.dark_exposure_of)

    def solve_target(self, target):
        """BLUE of target . beta: (weights vector, estimator Form) or None.

        Returns None exactly when ``target`` is not in the row space of X,
        i.e. when the linear background target is not estimable.
        """
        target = [Fraction(value) for value in target]
        if not any(value != 0 for value in target):
            weights = [Fraction(0) for _ in range(self.size)]
            return weights, Form(Fraction(0))
        key = tuple(target)
        if key in self._blue_cache:
            return self._blue_cache[key]
        if not _in_row_space(target, self.basis):
            self._blue_cache[key] = None
            return None
        n = self.size
        rhs = [Fraction(0) for _ in range(n)] + list(target)
        augmented = [self._kkt[i] + [rhs[i]] for i in range(n + 3)]
        solution = linalg.solve_particular(augmented)
        weights = solution[:n]
        estimator = Form(Fraction(0))
        for coefficient, form in zip(weights, self.forms):
            estimator = estimator + form.scale(coefficient)
        self._blue_cache[key] = (weights, estimator)
        return weights, estimator

    def fit_report(self):
        estimates = []
        size = len(self.basis)
        covariance_matrix = [[Fraction(0) for _ in range(size)] for _ in range(size)]
        estimator_forms = []
        for row in self.basis:
            weights, estimator = self.solve_target(row)
            estimator_forms.append(estimator)
            estimates.append(str(estimator.mean))
        for i in range(size):
            for j in range(i, size):
                value = covariance(estimator_forms[i], estimator_forms[j],
                                   self.raw_variance_of, self.dark_variance_of)
                covariance_matrix[i][j] = value
                covariance_matrix[j][i] = value
        return {
            "basis": [[str(value) for value in row] for row in self.basis],
            "estimates": estimates,
            "covariance": [[str(value) for value in row]
                           for row in covariance_matrix]}


def _in_row_space(vector, basis):
    """Whether ``vector`` lies in the span of the basis rows (exact rational)."""
    if not basis:
        return not any(value != 0 for value in vector)
    # solve B' c = v for basis coefficients c; consistency is membership
    transposed = linalg.transpose(basis)
    system = [transposed[i] + [vector[i]] for i in range(len(vector))]
    return linalg.is_consistent(system)
