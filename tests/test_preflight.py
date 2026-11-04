import contextlib
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from merge_preflight.cli import main
from merge_preflight.core import CheckSpec, Plan, PreflightError, cleanup_owned, owned_worktrees, preflight


def git(repo, *args):
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)
    return result.stdout.strip()


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.email", "test@example.com")
        git(self.repo, "config", "user.name", "Test")
        (self.repo / "shared.txt").write_text("base\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "base")
        git(self.repo, "branch", "-M", "main")
        self.base_sha = git(self.repo, "rev-parse", "HEAD")

    def branch(self, name, path, content):
        git(self.repo, "switch", "-q", "-c", name, "main")
        (self.repo / path).write_text(content)
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", name)
        sha = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "switch", "-q", "main")
        return sha

    def assert_untouched(self, before_status):
        self.assertEqual(git(self.repo, "rev-parse", "main"), self.base_sha)
        self.assertEqual(git(self.repo, "status", "--porcelain"), before_status)
        self.assertEqual(owned_worktrees(self.repo), [])

    def test_clean_merge_and_failed_check(self):
        left = self.branch("left", "left.txt", "left\n")
        right = self.branch("right", "right.txt", "right\n")
        before = git(self.repo, "status", "--porcelain")
        report = preflight(Plan(self.repo, "main", ("left", "right"),
                                (f"{sys.executable} -c 'import sys; sys.exit(7)'",)))
        self.assertEqual([s["sha"] for s in report["merge_steps"]], [left, right])
        self.assertEqual(report["checks"][0]["exit_code"], 7)
        self.assertEqual(report["findings"][-1]["kind"], "check_failure")
        self.assertEqual(report["cleanup"]["status"], "removed")
        self.assert_untouched(before)

    def test_conflict_reports_path_and_skips_checks(self):
        self.branch("left", "shared.txt", "left\n")
        self.branch("right", "shared.txt", "right\n")
        report = preflight(Plan(self.repo, "main", ("left", "right"), ("true",)))
        self.assertEqual(report["merge_steps"][-1]["unmerged_paths"], ["shared.txt"])
        self.assertEqual(report["checks"], [])
        self.assertIn("git_conflict", [f["kind"] for f in report["findings"]])
        self.assert_untouched("")

    def test_timeout(self):
        self.branch("left", "left.txt", "left\n")
        self.branch("right", "right.txt", "right\n")
        report = preflight(Plan(self.repo, "main", ("left", "right"),
                                (f"{sys.executable} -c 'import time; time.sleep(2)'",), timeout=1))
        self.assertEqual(report["checks"][0]["status"], "timeout")
        self.assertEqual(report["findings"][-1]["kind"], "check_timeout")
        self.assert_untouched("")

    def test_invalid_ref_is_rejected(self):
        self.branch("left", "left.txt", "left\n")
        with self.assertRaises(PreflightError):
            preflight(Plan(self.repo, "main", ("left", "--bad")))
        self.assert_untouched("")

    def test_dirty_primary_worktree_and_retained_cleanup(self):
        self.branch("left", "left.txt", "left\n")
        self.branch("right", "right.txt", "right\n")
        (self.repo / "untracked.txt").write_text("leave me alone\n")
        before = git(self.repo, "status", "--porcelain")
        report = preflight(Plan(self.repo, "main", ("left", "right"), keep_worktree=True))
        self.assertEqual(report["cleanup"]["status"], "kept")
        self.assertEqual(len(owned_worktrees(self.repo)), 1)
        self.assertEqual(len(cleanup_owned(self.repo)), 1)
        self.assert_untouched(before)

    def test_rename_edit_and_delete_edit(self):
        git(self.repo, "switch", "-q", "-c", "rename", "main")
        git(self.repo, "mv", "shared.txt", "renamed.txt")
        git(self.repo, "commit", "-qm", "rename")
        git(self.repo, "switch", "-q", "main")
        self.branch("edit", "shared.txt", "edited\n")
        report = preflight(Plan(self.repo, "main", ("rename", "edit")))
        self.assertEqual(report["changes"]["rename"][0]["status"], "renamed")
        self.assertIn("shared.txt", [f["paths"][0] for f in report["findings"] if f["kind"] == "path_overlap"])
        self.assert_untouched("")

        git(self.repo, "switch", "-q", "-c", "delete", "main")
        git(self.repo, "rm", "shared.txt")
        git(self.repo, "commit", "-qm", "delete")
        git(self.repo, "switch", "-q", "main")
        report = preflight(Plan(self.repo, "main", ("delete", "edit")))
        self.assertEqual(report["changes"]["delete"][0]["status"], "deleted")
        self.assertEqual(report["merge_steps"][-1]["unmerged_paths"], ["shared.txt"])
        self.assert_untouched("")

    def test_interface_warning_and_bounded_output(self):
        self.branch("interface", "api.pyi", "def f() -> int: ...\n")
        self.branch("source", "api.py", "def f(): return 1\n")
        command = f"{sys.executable} -c 'import sys; sys.stdout.write(\"X\"*1000000)'"
        report = preflight(Plan(self.repo, "main", ("interface", "source"), (command,), max_output_bytes=100))
        self.assertIn("interface_risk", [f["kind"] for f in report["findings"]])
        output = report["checks"][0]["stdout"]
        self.assertEqual(len(output["excerpt"]), 100)
        self.assertEqual(output["size_bytes"], 1000000)
        self.assertTrue(output["truncated"])
        self.assert_untouched("")

    def test_cli_exit_codes(self):
        self.branch("left", "left.txt", "left\n")
        self.branch("right", "right.txt", "right\n")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["check", "--repo", str(self.repo), "--base", "main", "--branches", "left", "right"]), 0)
            self.assertEqual(main(["check", "--repo", str(self.repo), "--base", "main", "--branches", "left", "right", "--test", "false"]), 1)

    def test_config_advisory_failure_does_not_block(self):
        self.branch("left", "left.txt", "left\n")
        self.branch("right", "right.txt", "right\n")
        config = Path(self.temp.name) / "checks.json"
        config.write_text(json.dumps({"checks": [{"command": "false", "required": False},
                                                  {"command": "true", "required": True}]}))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["check", "--repo", str(self.repo), "--base", "main", "--branches", "left", "right",
                         "--config", str(config)])
        report = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual([c["required"] for c in report["checks"]], [False, True])
        self.assertEqual([c["status"] for c in report["checks"]], ["failed", "passed"])
        self.assertEqual([f["severity"] for f in report["findings"] if f["kind"] == "check_failure"], ["warning"])
        self.assert_untouched("")

    def test_config_required_failure_blocks_and_cli_test_stays_required(self):
        self.branch("left", "left.txt", "left\n")
        self.branch("right", "right.txt", "right\n")
        config = Path(self.temp.name) / "checks.json"
        config.write_text(json.dumps({"checks": [{"command": "false", "required": True}]}))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["check", "--repo", str(self.repo), "--base", "main", "--branches", "left", "right",
                         "--test", "true", "--config", str(config)])
        report = json.loads(output.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual([c["required"] for c in report["checks"]], [True, True])
        self.assertEqual([f["severity"] for f in report["findings"] if f["kind"] == "check_failure"], ["blocking"])
        self.assert_untouched("")

    def test_invalid_config_fails_before_worktree_creation(self):
        self.branch("left", "left.txt", "left\n")
        self.branch("right", "right.txt", "right\n")
        config = Path(self.temp.name) / "checks.json"
        config.write_text(json.dumps({"checks": [{"command": "false", "required": "no"}]}))
        with contextlib.redirect_stderr(io.StringIO()):
            code = main(["check", "--repo", str(self.repo), "--base", "main", "--branches", "left", "right",
                         "--config", str(config)])
        self.assertEqual(code, 2)
        self.assert_untouched("")

    def test_plan_api_supports_advisory_checks(self):
        self.branch("left", "left.txt", "left\n")
        self.branch("right", "right.txt", "right\n")
        report = preflight(Plan(self.repo, "main", ("left", "right"), checks=(CheckSpec("false", False),)))
        self.assertEqual(report["checks"][0]["required"], False)
        self.assertEqual(report["findings"][-1]["severity"], "warning")
        self.assert_untouched("")

    def test_full_check_logs_preserve_output_and_bounded_report(self):
        self.branch("left", "left.txt", "left\n")
        self.branch("right", "right.txt", "right\n")
        log_dir = Path(self.temp.name) / "logs"
        passing = f"{sys.executable} -c 'import sys; sys.stdout.write(\"X\"*100000); sys.stderr.write(\"Y\"*50000)'"
        failing = f"{sys.executable} -c 'import sys; sys.stderr.write(\"failure\"); sys.exit(4)'"
        report = preflight(Plan(self.repo, "main", ("left", "right"), (passing, failing),
                                max_output_bytes=25, log_dir=log_dir))
        self.assertEqual([item["status"] for item in report["checks"]], ["passed", "failed"])
        self.assertEqual(report["cleanup"]["status"], "removed")
        for index, expected_out, expected_err in ((0, b"X" * 100000, b"Y" * 50000),
                                                   (1, b"", b"failure")):
            check = report["checks"][index]
            for stream, expected in (("stdout", expected_out), ("stderr", expected_err)):
                summary = check[stream]
                path = Path(summary["log_path"])
                self.assertTrue(path.is_file())
                self.assertEqual(path.read_bytes(), expected)
                self.assertEqual(summary["sha256"], hashlib.sha256(expected).hexdigest())
                self.assertEqual(summary["size_bytes"], len(expected))
                self.assertLessEqual(len(summary["excerpt"].encode()), 25)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assert_untouched("")

    def test_log_dir_rejects_primary_worktree_and_cli_accepts_external_directory(self):
        self.branch("left", "left.txt", "left\n")
        self.branch("right", "right.txt", "right\n")
        with self.assertRaisesRegex(PreflightError, "outside the source repository"):
            preflight(Plan(self.repo, "main", ("left", "right"), ("true",),
                           log_dir=self.repo / "logs"))
        self.assertFalse((self.repo / "logs").exists())
        log_dir = Path(self.temp.name) / "outside-logs"
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["check", "--repo", str(self.repo), "--base", "main", "--branches", "left", "right",
                         "--test", "true", "--log-dir", str(log_dir)])
        self.assertEqual(code, 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["plan"]["log_dir"], str(log_dir.resolve()))
        self.assertEqual(Path(report["checks"][0]["stdout"]["log_path"]).read_bytes(), b"")
        self.assert_untouched("")

    def test_timeout_keeps_partial_log_and_cleans_worktree(self):
        self.branch("left", "left.txt", "left\n")
        self.branch("right", "right.txt", "right\n")
        log_dir = Path(self.temp.name) / "logs"
        command = f"{sys.executable} -u -c 'import sys,time; print(\"started\", flush=True); time.sleep(2)'"
        report = preflight(Plan(self.repo, "main", ("left", "right"), (command,), timeout=1,
                                log_dir=log_dir))
        check = report["checks"][0]
        self.assertEqual(check["status"], "timeout")
        self.assertEqual(Path(check["stdout"]["log_path"]).read_text(), "started\n")
        self.assertEqual(report["cleanup"]["status"], "removed")
        self.assert_untouched("")


if __name__ == "__main__":
    unittest.main()
