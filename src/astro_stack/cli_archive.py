"""astro-archive command line: initialize, apply batches and render revisions."""
import argparse
import json

from . import archive
from .io import load, publish


def _parser():
    parser = argparse.ArgumentParser(description="本地校准档案")
    parser.add_argument("--archive", required=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--initialize", metavar="DEFINITION_JSON")
    action.add_argument("--apply", metavar="BATCH_JSON")
    action.add_argument("--revision", type=int, metavar="N")
    parser.add_argument("--output", metavar="REPORT_JSON")
    return parser


def main(argv=None):
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.initialize is not None:
            receipt = archive.initialize(args.archive, load(args.initialize))
            print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
            return 0
        if args.apply is not None:
            receipt = archive.apply_batch(args.archive, load(args.apply))
            print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
            return 0
        if args.output is None:
            parser.error("--revision requires --output REPORT_JSON")
        report = archive.render(args.archive, args.revision)
        publish(args.output, report)
        print(json.dumps(report["summary"], ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
        parser.exit(2, f"astro-archive: {exc}\n")


if __name__ == "__main__":
    main()
