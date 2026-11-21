import itertools
import random
import unittest

from astro_stack.campaign import assign, feasible_subsets, precompute
from astro_stack.model import validate


def build_random_data(rng):
    frame_count = rng.randint(1, 8)
    product_count = rng.randint(1, 3)
    frames = []
    for index in range(frame_count):
        frames.append({
            "id": f"f{index}", "exposure_s": rng.randint(1, 4),
            "dx": 0, "dy": 0, "saturation": rng.choice((1, 100)),
            "pixels": [[rng.randint(0, 5)]],
            "observed_at_s": rng.randint(0, 9), "variance": [[rng.randint(0, 2)]]})
    aperture_count = rng.randint(1, 2)
    apertures = [{"id": f"a{i}", "x": 0, "y": 0, "radius": 0}
                 for i in range(aperture_count)]
    products = []
    for index in range(product_count):
        start = rng.randint(0, 8)
        end = rng.randint(start + 1, 10)
        requirements = [{
            "aperture_id": rng.choice(apertures)["id"],
            "min_exposure_s": rng.randint(1, 3),
            "max_variance": rng.choice(("0", "1", "2", "1/2", "100"))}]
        products.append({"id": f"p{index}", "start_s": start, "end_s": end,
                         "priority": rng.randint(1, 5),
                         "max_frames": rng.randint(1, 3),
                         "requirements": requirements})
    return {
        "width": 1, "height": 1, "dark_exposure_s": 1,
        "dark": [[rng.randint(0, 3)]], "flat": [[1]], "bad_mask": [[False]],
        "frames": frames, "apertures": apertures,
        "campaign": {"dark_variance": [[rng.randint(0, 2)]], "products": products}}


def brute_force_goal(assignment, products, frames):
    signature = [sorted(assignment.get(pid, ())) for pid in sorted(products)]
    priority_sum = sum(products[pid]["priority"] for pid in assignment)
    used = {frame_id for subset in assignment.values() for frame_id in subset}
    cost = sum(frames[frame_id]["exposure_s"] for frame_id in used)
    return (-priority_sum, -len(assignment), cost, signature)


class AllocationOracleTests(unittest.TestCase):
    def test_dp_matches_brute_force_on_random_instances(self):
        rng = random.Random(20261120)
        for trial in range(300):
            data = build_random_data(rng)
            width, height, frames, apertures, usable, campaign = validate(data)
            precomp = precompute(data, width, height, frames, usable)
            aperture_map = {aid: dict(aperture, _width=width, _height=height)
                            for aid, aperture in apertures.items()}
            products = {p["id"]: p for p in campaign["products"]}
            dark_variance = campaign["dark_variance"]
            got = assign(products, frames, aperture_map, precomp, dark_variance)

            product_ids = sorted(products)
            all_ids = sorted(frames)
            options = {}
            for pid in product_ids:
                product = products[pid]
                eligible = [fid for fid in all_ids
                            if product["start_s"] <= frames[fid]["observed_at_s"]
                            < product["end_s"]]
                options[pid] = [set(subset) for subset in feasible_subsets(
                    product, eligible, aperture_map, precomp, dark_variance)]
            best_goal = None
            for choice in itertools.product(*(range(len(options[pid]) + 1)
                                              for pid in product_ids)):
                assignment = {}
                used = set()
                valid = True
                for pid, pick in zip(product_ids, choice):
                    if pick == 0:
                        continue
                    subset = options[pid][pick - 1]
                    if used & subset:
                        valid = False
                        break
                    used |= subset
                    assignment[pid] = subset
                if not valid:
                    continue
                goal = brute_force_goal(assignment, products, frames)
                if best_goal is None or goal < best_goal:
                    best_goal = goal
            expected = {pid: list(ids) for pid, ids in
                        zip(product_ids, best_goal[3]) if ids}
            self.assertEqual(got, expected, f"trial {trial}: {data}")


if __name__ == "__main__":
    unittest.main()
