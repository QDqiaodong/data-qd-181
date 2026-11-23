"""astro-mosaic command line: render a sub-pixel survey mosaic report."""
import argparse
import json

from .io import load, publish
from .mosaic import render


def _parser():
    parser = argparse.ArgumentParser(description="亚像素巡天拼图")
    parser.add_argument("--spec", required=True, metavar="SPEC_JSON")
    parser.add_argument("--output", required=True, metavar="REPORT_JSON")
    return parser


def main(argv=None):
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        specification = load(args.spec)
        report = render(specification)
        publish(args.output, report)
        print(json.dumps(report["summary"], ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
        parser.exit(2, f"astro-mosaic: {exc}\n")


if __name__ == "__main__":
    main()
