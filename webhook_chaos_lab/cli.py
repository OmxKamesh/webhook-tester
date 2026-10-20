import argparse
import json
from pathlib import Path
import sys
from .demo import demo_server
from .engine import run_suite


def human(report, label):
    print(f"\n{label}: {'PASS' if report['passed'] else 'FAIL'}")
    for row in report["scenarios"]:
        failed = ", ".join(k for k, v in row["checks"].items() if not v)
        print(f"  {'PASS' if row['passed'] else 'FAIL'} {row['scenario']}" + (f" ({failed})" if failed else ""))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Test webhook outcomes using independent readback. Isolated test data only.")
    sub = parser.add_subparsers(dest="command", required=True)
    demo = sub.add_parser("demo", help="Run broken and fixed SQLite examples; expected failures are success")
    run = sub.add_parser("run", help="Exercise your test adapter; creates durable test effects")
    run.add_argument("--target", required=True)
    run.add_argument("--readback", required=True)
    run.add_argument("--allow-remote", action="store_true", help="Explicitly permit HTTPS isolated staging; never production")
    for command in (demo, run):
        command.add_argument("--json", type=Path, metavar="PATH", help="Save machine-readable report")
        command.add_argument("--settle-timeout", type=float, default=2, help="Observation window per scenario, seconds (0 < value <= 60)")
    args = parser.parse_args(argv)
    try:
        if args.command == "demo":
            with demo_server() as base:
                broken = run_suite(base + "/broken", base + "/state?mode=broken", settle_timeout=args.settle_timeout)
                fixed = run_suite(base + "/fixed", base + "/state?mode=fixed", settle_timeout=args.settle_timeout)
            human(broken, "Intentionally broken receiver")
            human(fixed, "Fixed transactional receiver")
            valid = all(not r["passed"] for r in broken["scenarios"]) and fixed["passed"]
            report = {"schema_version": 1, "demo_passed": valid, "broken": broken, "fixed": fixed}
            print("\nDemo " + ("PASS: detected all broken cases; fixed receiver passed." if valid else "FAIL: unexpected demo outcome."))
            code = 0 if valid else 1
        else:
            report = run_suite(args.target, args.readback, allow_remote=args.allow_remote, settle_timeout=args.settle_timeout)
            human(report, "Webhook outcome checks")
            code = 0 if report["passed"] else 1
        if args.json:
            args.json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        return code
    except (ValueError, OSError) as exc:
        print(f"Configuration/transport error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
