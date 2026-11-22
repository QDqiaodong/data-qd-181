"""astro-release command line: issue, export and offline verify."""
import argparse
import json

from .release import export, issue, verify


def _parser():
    parser = argparse.ArgumentParser(description="离线可复算科学图像发布")
    action = parser.add_subparsers(dest="action", required=True)

    issued = action.add_parser("issue", help="签发一份发布包")
    issued.add_argument("--library", required=True)
    issued.add_argument("--archive", required=True)
    issued.add_argument("--request", required=True)
    issued.add_argument("--license", required=True)

    exported = action.add_parser("export", help="按请求号取回已签发包")
    exported.add_argument("--library", required=True)
    exported.add_argument("--request-id", required=True)
    exported.add_argument("--output", required=True)

    verified = action.add_parser("verify", help="离线复算一个发布包")
    verified.add_argument("--package", required=True)
    verified.add_argument("--expected-sha256", required=True)
    return parser


def main(argv=None):
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.action == "issue":
            receipt = issue(args.library, args.archive, args.request,
                            args.license)
            print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
            return 0
        if args.action == "export":
            receipt = export(args.library, args.request_id, args.output)
            print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
            return 0
        result = verify(args.package, args.expected_sha256)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
        parser.exit(2, f"astro-release: {exc}\n")


if __name__ == "__main__":
    main()
