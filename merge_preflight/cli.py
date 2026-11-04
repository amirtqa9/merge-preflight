"""Command-line entry point for MergePreflight."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .core import CheckSpec, InfrastructureError, Plan, PreflightError, cleanup_owned, owned_worktrees, preflight


def load_check_config(path: Path) -> tuple[CheckSpec, ...]:
    """Load explicitly declared checks from a small JSON policy file."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PreflightError(f"Invalid check config {path}: {exc}") from exc
    if not isinstance(data, dict) or set(data) != {"checks"} or not isinstance(data["checks"], list):
        raise PreflightError("Check config must be an object containing only a checks array")
    checks = []
    for index, item in enumerate(data["checks"]):
        if not isinstance(item, dict) or set(item) != {"command", "required"}:
            raise PreflightError(f"checks[{index}] needs command and required fields")
        if not isinstance(item["command"], str) or not item["command"].strip() or not isinstance(item["required"], bool):
            raise PreflightError(f"checks[{index}] needs a nonempty command and boolean required flag")
        checks.append(CheckSpec(item["command"], item["required"]))
    return tuple(checks)


def render_markdown(report: dict) -> str:
    lines = ["# Merge preflight report", "", f"Base: `{report['plan']['base_sha']}`", "",
             "## Findings", ""]
    if report["findings"]:
        for item in report["findings"]:
            lines.append(f"- **{item['severity']}** `{item['kind']}`: {', '.join(item['paths']) or item['evidence']}")
    else:
        lines.append("No findings.")
    lines.extend(["", "## Merge steps", ""])
    for step in report["merge_steps"]:
        lines.append(f"- `{step['branch']}` (`{step['sha']}`): exit {step['exit_code']}")
    lines.extend(["", "## Checks", ""])
    for check in report["checks"]:
        level = "required" if check.get("required", True) else "advisory"
        lines.append(f"- `{check['command']}` ({level}): {check['status']}")
    lines.extend(["", f"Cleanup: {report['cleanup']['status']}", ""])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="merge-preflight")
    sub = parser.add_subparsers(dest="action", required=True)
    check = sub.add_parser("check", help="Rehearse an isolated merge")
    check.add_argument("--repo", type=Path, default=Path.cwd())
    check.add_argument("--base", required=True)
    check.add_argument("--branches", nargs="+", required=True)
    check.add_argument("--test", action="append", default=[])
    check.add_argument("--config", type=Path,
                       help='JSON file: {"checks":[{"command":"pytest -q","required":true}]}')
    check.add_argument("--timeout", type=int, default=60)
    check.add_argument("--log-dir", type=Path,
                       help="write complete check output to private files outside the source repository")
    check.add_argument("--format", choices=["json", "md"], default="json")
    check.add_argument("--out", type=Path)
    check.add_argument("--keep-worktree", action="store_true")
    explain = sub.add_parser("explain", help="Summarize a JSON report")
    explain.add_argument("report", type=Path)
    clean = sub.add_parser("cleanup", help="List or remove tool-owned worktrees")
    clean.add_argument("--repo", type=Path, default=Path.cwd())
    clean.add_argument("--yes", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.action == "check":
            configured = load_check_config(args.config) if args.config else ()
            report = preflight(Plan(args.repo, args.base, tuple(args.branches), tuple(args.test), args.timeout,
                                    keep_worktree=args.keep_worktree, checks=configured, log_dir=args.log_dir))
            output = render_markdown(report) if args.format == "md" else json.dumps(report, indent=2) + "\n"
            if args.out:
                args.out.write_text(output, encoding="utf-8")
            else:
                print(output, end="")
            if report["cleanup"]["status"] == "failed":
                return 3
            return 1 if any(f["severity"] == "blocking" for f in report["findings"]) else 0
        if args.action == "explain":
            report = json.loads(args.report.read_text(encoding="utf-8"))
            print(render_markdown(report), end="")
            return 0
        paths = cleanup_owned(args.repo) if args.yes else [str(p) for p in owned_worktrees(args.repo)]
        for path in paths:
            print(path)
        return 0
    except PreflightError as exc:
        print(f"merge-preflight: {exc}", file=sys.stderr)
        return 2
    except (InfrastructureError, OSError, ValueError, KeyError) as exc:
        print(f"merge-preflight: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
