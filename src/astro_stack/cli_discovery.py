"""astro-discovery command line: workspace lifecycle, advances and the queue."""
import argparse
import json

from . import discovery as discovery_module
from .io import load, publish


def _parser():
    parser = argparse.ArgumentParser(description="时序瞬变发现工作区")
    parser.add_argument("--workspace", required=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--initialize", metavar="CONFIG_JSON")
    action.add_argument("--advance", metavar="REVISION", type=int)
    action.add_argument("--snapshot", action="store_true")
    action.add_argument("--pending", metavar="LIMIT", type=int)
    action.add_argument("--ack", metavar="THROUGH_SEQ", type=int)
    parser.add_argument("--archive", metavar="ARCHIVE_PATH")
    parser.add_argument("--output", metavar="REPORT_JSON")
    return parser


def main(argv=None):
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.initialize is not None:
            if args.archive is None:
                parser.error("--initialize requires --archive ARCHIVE_PATH")
            receipt = discovery_module.initialize(
                args.workspace, args.archive, load(args.initialize))
            print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
            return 0
        if args.advance is not None:
            receipt = discovery_module.advance(args.workspace, args.advance)
            print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
            return 0
        if args.snapshot:
            if args.output is None:
                parser.error("--snapshot requires --output REPORT_JSON")
            report = discovery_module.snapshot(args.workspace)
            publish(args.output, report)
            print(json.dumps(report["summary"], ensure_ascii=False,
                             sort_keys=True))
            return 0
        if args.pending is not None:
            result = discovery_module.pending_events(args.workspace, args.pending)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 0
        result = discovery_module.acknowledge(args.workspace, args.ack)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
        parser.exit(2, f"astro-discovery: {exc}\n")


if __name__ == "__main__":
    main()
