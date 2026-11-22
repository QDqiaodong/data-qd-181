"""astro-sky command line: render a background-subtracted revision report."""
import argparse
import json

from .io import load, publish
from .sky import SkyError, render


def _parser():
    parser = argparse.ArgumentParser(description="空间背景模型与扣背景交付")
    parser.add_argument("--archive", required=True, metavar="PATH")
    parser.add_argument("--revision", required=True, type=int, metavar="N")
    parser.add_argument("--spec", required=True, metavar="SPEC_JSON")
    parser.add_argument("--output", required=True, metavar="REPORT_JSON")
    return parser


def main(argv=None):
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        specification = load(args.spec)
        report = render(args.archive, args.revision, specification)
        publish(args.output, report)
        print(json.dumps(report["summary"], ensure_ascii=False, sort_keys=True))
        return 0
    except SkyError as exc:
        parser.exit(2, f"astro-sky: {exc}\n")
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
        parser.exit(2, f"astro-sky: {exc}\n")


if __name__ == "__main__":
    main()
