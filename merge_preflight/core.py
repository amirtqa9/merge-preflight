"""Safe, deterministic merge rehearsal and report generation."""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import shlex
import signal
import subprocess
import tempfile
import threading
import time
from typing import Any
import uuid


SCHEMA_VERSION = 1


class PreflightError(Exception):
    """Invalid input or repository state."""


class InfrastructureError(Exception):
    """Git or worktree infrastructure failed."""


@dataclass(frozen=True)
class CheckSpec:
    command: str
    required: bool = True


@dataclass(frozen=True)
class Plan:
    repo_root: Path
    base: str
    branches: tuple[str, ...]
    tests: tuple[str, ...] = ()
    timeout: int = 60
    max_output_bytes: int = 16_384
    keep_worktree: bool = False
    checks: tuple[CheckSpec, ...] = ()
    log_dir: Path | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _git(repo: Path, *args: str, check: bool = True, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InfrastructureError(str(exc)) from exc
    if check and result.returncode:
        raise PreflightError(result.stderr.strip() or result.stdout.strip() or "Git command failed")
    return result


def _resolve(repo: Path, ref: str) -> str:
    if not ref or ref.startswith("-"):
        raise PreflightError(f"Invalid ref: {ref!r}")
    return _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").stdout.strip()


def _owned_root(repo: Path) -> Path:
    digest = hashlib.sha256(str(repo.resolve()).encode()).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / "merge-preflight-worktrees" / digest


def owned_worktrees(repo: Path) -> list[Path]:
    root = _owned_root(repo).resolve()
    output = _git(repo, "worktree", "list", "--porcelain").stdout
    paths: list[Path] = []
    for line in output.splitlines():
        if line.startswith("worktree "):
            path = Path(line[9:]).resolve()
            if path.parent == root:
                paths.append(path)
    return paths


def cleanup_owned(repo: Path) -> list[str]:
    removed = []
    for path in owned_worktrees(repo):
        _git(repo, "worktree", "remove", "--force", str(path))
        removed.append(str(path))
    return removed


def _changes(repo: Path, base_sha: str, sha: str) -> list[dict[str, str]]:
    output = _git(repo, "diff", "--name-status", "-M", base_sha, sha, "--").stdout
    changes = []
    for line in output.splitlines():
        parts = line.split("\t")
        code = parts[0]
        item = {"status": {"A": "added", "M": "modified", "D": "deleted"}.get(code[0], "renamed" if code.startswith("R") else code), "path": parts[-1]}
        if code.startswith("R") and len(parts) >= 3:
            item["old_path"] = parts[1]
        changes.append(item)
    return changes


def _excerpt(value: str, limit: int) -> dict[str, Any]:
    raw = value.encode("utf-8", errors="replace")
    return {"excerpt": raw[:limit].decode("utf-8", errors="replace"), "sha256": hashlib.sha256(raw).hexdigest(), "truncated": len(raw) > limit}


def _open_log(path: Path) -> Any:
    """Create a private, new log file without following a final symlink."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    return os.fdopen(os.open(path, flags, 0o600), "wb")


def _run_check(argv: list[str], cwd: Path, timeout: int, limit: int,
               log_paths: tuple[Path, Path] | None = None) -> tuple[str, int | None, dict[str, Any], dict[str, Any]]:
    """Drain output continuously while retaining at most `limit` bytes per stream."""
    streams = {"stdout": {"prefix": bytearray(), "digest": hashlib.sha256(), "size": 0},
               "stderr": {"prefix": bytearray(), "digest": hashlib.sha256(), "size": 0}}
    env = {key: value for key, value in os.environ.items() if key not in {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"}}
    try:
        with ExitStack() as stack:
            log_files = {}
            if log_paths is not None:
                log_files = {name: stack.enter_context(_open_log(path))
                             for name, path in zip(("stdout", "stderr"), log_paths)}
            try:
                process = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                           env=env, start_new_session=True)
            except OSError as exc:
                output, error = _excerpt("", limit), _excerpt(str(exc), limit)
                if log_paths is not None:
                    output["log_path"], error["log_path"] = map(str, log_paths)
                    log_files["stderr"].write(str(exc).encode("utf-8", errors="replace"))
                return "failed", None, output, error

            errors: list[OSError] = []

            def drain(pipe: Any, name: str) -> None:
                state = streams[name]
                try:
                    while True:
                        chunk = pipe.read(64 * 1024)
                        if not chunk:
                            break
                        state["digest"].update(chunk)
                        state["size"] += len(chunk)
                        remaining = limit - len(state["prefix"])
                        if remaining > 0:
                            state["prefix"].extend(chunk[:remaining])
                        if name in log_files:
                            try:
                                log_files[name].write(chunk)
                            except OSError as exc:
                                errors.append(exc)
                                log_files.pop(name)
                finally:
                    pipe.close()

            threads = [threading.Thread(target=drain, args=(process.stdout, "stdout"), daemon=True),
                       threading.Thread(target=drain, args=(process.stderr, "stderr"), daemon=True)]
            for thread in threads:
                thread.start()
            try:
                code = process.wait(timeout=timeout)
                status = "passed" if code == 0 else "failed"
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                status, code = "timeout", None
            for thread in threads:
                thread.join(timeout=2)
            if any(thread.is_alive() for thread in threads) or errors:
                raise InfrastructureError(f"check log write failed: {errors[0] if errors else 'output stream did not close'}")
    except OSError as exc:
        raise InfrastructureError(f"Cannot create check logs: {exc}") from exc

    def summary(name: str) -> dict[str, Any]:
        state = streams[name]
        result = {"excerpt": state["prefix"].decode("utf-8", errors="replace"),
                "sha256": state["digest"].hexdigest(), "truncated": state["size"] > limit,
                "size_bytes": state["size"]}
        if log_paths is not None:
            result["log_path"] = str(log_paths[0 if name == "stdout" else 1])
        return result

    return status, code, summary("stdout"), summary("stderr")


def preflight(plan: Plan) -> dict[str, Any]:
    """Rehearse merges in a disposable worktree, leaving branch refs untouched."""
    repo = Path(plan.repo_root).resolve()
    if len(plan.branches) < 2 or plan.timeout <= 0 or plan.max_output_bytes <= 0:
        raise PreflightError("Provide at least two branches, positive timeout, and positive output limit")
    log_root = Path(plan.log_dir).resolve() if plan.log_dir is not None else None
    if log_root is not None and (log_root == repo or log_root.is_relative_to(repo)
                                 or log_root.is_relative_to(_owned_root(repo))):
        raise PreflightError("log_dir must be outside the source repository and temporary worktrees")
    configured_checks = [CheckSpec(command, True) for command in plan.tests] + list(plan.checks)
    for check_spec in configured_checks:
        if not isinstance(check_spec, CheckSpec) or not isinstance(check_spec.command, str) or not check_spec.command.strip() or not isinstance(check_spec.required, bool):
            raise PreflightError("Every check needs a nonempty command and boolean required flag")
        try:
            if not shlex.split(check_spec.command):
                raise PreflightError("Empty check command")
        except ValueError as exc:
            raise PreflightError(f"Invalid check command: {exc}") from exc
    top = Path(_git(repo, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
    if top != repo:
        raise PreflightError("repo_root must be the repository root")
    base_sha = _resolve(repo, plan.base)
    branch_shas = [{"name": name, "sha": _resolve(repo, name)} for name in plan.branches]
    if len({x["sha"] for x in branch_shas}) != len(branch_shas):
        raise PreflightError("Branch refs must resolve to distinct commits")
    warnings = []
    for branch in branch_shas:
        common = _git(repo, "merge-base", base_sha, branch["sha"], check=False)
        if common.returncode:
            raise PreflightError(f"No common ancestor for {branch['name']}")
        if common.stdout.strip() != base_sha:
            warnings.append(f"{branch['name']} does not contain the base commit")
    git_version = _git(repo, "--version").stdout.strip()
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION, "started_at": _now(), "ended_at": None,
        "git_version": git_version,
        "plan": {"repo_root": str(repo), "base": plan.base, "base_sha": base_sha, "branches": branch_shas,
                 "tests": list(plan.tests), "checks": [{"command": spec.command, "required": spec.required} for spec in configured_checks],
                 "timeout": plan.timeout, "log_dir": str(log_root) if log_root is not None else None},
        "changes": {}, "merge_steps": [], "checks": [], "findings": [], "warnings": warnings,
        "cleanup": {"status": "not_started", "worktree": None},
    }
    paths: dict[str, list[str]] = {}
    for branch in branch_shas:
        changed = _changes(repo, base_sha, branch["sha"])
        report["changes"][branch["name"]] = changed
        for item in changed:
            for path in {item["path"], item.get("old_path", item["path"])}:
                paths.setdefault(path, []).append(branch["name"])
    for path, names in sorted(paths.items()):
        unique = list(dict.fromkeys(names))
        if len(unique) > 1:
            report["findings"].append({"kind": "path_overlap", "severity": "warning", "paths": [path],
                                       "branches": unique, "evidence": "Changed by multiple branches",
                                       "suggestion": "Review the combined result"})
    interface_pairs = {".pyi": ".py", ".d.ts": ".ts", ".h": ".c", ".hpp": ".cpp"}
    for branch in branch_shas:
        name = branch["name"]
        for item in report["changes"][name]:
            interface_path = item["path"]
            for suffix, source_suffix in interface_pairs.items():
                if not interface_path.endswith(suffix):
                    continue
                source_path = interface_path[:-len(suffix)] + source_suffix
                consumers = [other for other in branch_shas if other["name"] != name
                             and source_path in {change["path"] for change in report["changes"][other["name"]]}]
                for other in consumers:
                    report["findings"].append({"kind": "interface_risk", "severity": "warning",
                                               "paths": [interface_path, source_path],
                                               "branches": [name, other["name"]],
                                               "evidence": "Interface declaration and matching source changed on separate branches",
                                               "suggestion": "Review compatibility of the combined interface and source"})
    root = _owned_root(repo)
    root.mkdir(parents=True, exist_ok=True)
    worktree = root / uuid.uuid4().hex
    added = False
    try:
        _git(repo, "worktree", "add", "--detach", str(worktree), base_sha)
        added = True
        report["cleanup"]["worktree"] = str(worktree)
        for branch in branch_shas:
            result = _git(worktree, "-c", "user.name=MergePreflight", "-c", "user.email=merge-preflight@localhost",
                          "merge", "--no-ff", "--no-commit", branch["sha"], check=False, timeout=plan.timeout)
            unmerged = _git(worktree, "diff", "--name-only", "--diff-filter=U", "--").stdout.splitlines()
            step = {"branch": branch["name"], "sha": branch["sha"], "exit_code": result.returncode,
                    "unmerged_paths": unmerged, "stdout": _excerpt(result.stdout, plan.max_output_bytes),
                    "stderr": _excerpt(result.stderr, plan.max_output_bytes)}
            report["merge_steps"].append(step)
            if result.returncode:
                report["findings"].append({"kind": "git_conflict", "severity": "blocking", "paths": unmerged,
                                           "branches": [branch["name"]], "evidence": step["stderr"]["excerpt"],
                                           "suggestion": "Resolve conflicts in the affected paths"})
                break
            # Git may leave a staged merge (including a no-op). Commit only inside this detached worktree.
            pending = _git(worktree, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False)
            if pending.returncode == 0:
                _git(worktree, "-c", "user.name=MergePreflight", "-c", "user.email=merge-preflight@localhost",
                     "commit", "--no-verify", "-m", f"Preflight merge {branch['name']}")
        if not any(x["kind"] == "git_conflict" for x in report["findings"]):
            run_logs = None
            if configured_checks and log_root is not None:
                try:
                    log_root.mkdir(mode=0o700, parents=True, exist_ok=True)
                    run_logs = log_root / worktree.name
                    run_logs.mkdir(mode=0o700)
                except OSError as exc:
                    raise InfrastructureError(f"Cannot create log directory: {exc}") from exc
            for number, check_spec in enumerate(configured_checks, 1):
                command = check_spec.command
                argv = shlex.split(command)
                started = time.monotonic()
                log_paths = ((run_logs / f"check-{number:03d}.stdout.log",
                              run_logs / f"check-{number:03d}.stderr.log") if run_logs is not None else None)
                status, code, stdout, stderr = _run_check(argv, worktree, plan.timeout, plan.max_output_bytes,
                                                          log_paths)
                check = {"command": command, "required": check_spec.required,
                         "status": status, "exit_code": code,
                         "duration_ms": int((time.monotonic() - started) * 1000),
                         "stdout": stdout, "stderr": stderr}
                report["checks"].append(check)
                if status != "passed":
                    report["findings"].append({"kind": "check_timeout" if status == "timeout" else "check_failure",
                                               "severity": "blocking" if check_spec.required else "warning",
                                               "paths": [], "branches": [],
                                               "evidence": check, "suggestion": "Investigate the combined tree and rerun this check"})
    finally:
        if added and not plan.keep_worktree:
            removed = _git(repo, "worktree", "remove", "--force", str(worktree), check=False)
            report["cleanup"]["status"] = "removed" if removed.returncode == 0 else "failed"
            if removed.returncode:
                report["cleanup"]["recovery"] = f"git -C {shlex.quote(str(repo))} worktree remove --force {shlex.quote(str(worktree))}"
        elif added:
            report["cleanup"]["status"] = "kept"
        else:
            report["cleanup"]["status"] = "not_created"
        report["ended_at"] = _now()
    return report
