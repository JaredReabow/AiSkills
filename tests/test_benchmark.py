#!/usr/bin/env python3
"""Tests for scripts/benchmark.py.

These exercise the generated fixture with real files and real subprocesses:
the stubs must fail, the shipped reference must pass, and the grader must stay
fixed in the face of anything the graded workspace contains.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import benchmark
import support

SKILL_ROOT = Path(__file__).resolve().parents[1]


class BenchmarkCase(unittest.TestCase):
    """Base class owning a temporary destination for one generated fixture."""

    def setUp(self) -> None:
        self.dest = Path(tempfile.mkdtemp(prefix="parallelism-bench-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.dest, ignore_errors=True)

    def generated(self) -> Path:
        """Generate the fixture and return its task workspace."""
        summary = benchmark.generate(self.dest / "bench")
        self.assertTrue(summary["ok"], summary)
        return Path(summary["task_workspace"])

    def apply_reference(self, workspace: Path, *names: str) -> None:
        """Copy named reference modules over the workspace modules."""
        for name in names:
            shutil.copy2(workspace.parent / "reference" / name, workspace / name)


class Generation(BenchmarkCase):
    """The generator must never clobber real work by accident."""

    def test_generate_refuses_nonempty_destination(self):
        target = self.dest / "bench"
        target.mkdir()
        (target / "important.txt").write_text("someone's work\n", encoding="utf-8")
        with self.assertRaises(ValueError) as caught:
            benchmark.generate(target)
        self.assertIn("not empty", str(caught.exception))
        self.assertTrue((target / "important.txt").is_file())

    def test_generate_has_no_destructive_replace_option(self):
        run = subprocess.run(
            [sys.executable, str(SKILL_ROOT / "scripts" / "benchmark.py"),
             "generate", "--dest", str(self.dest / "bench"), "--force"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(2, run.returncode)
        self.assertIn("unrecognized arguments", run.stderr)

    def test_generate_refuses_destination_inside_the_skill(self):
        with self.assertRaises(ValueError) as caught:
            benchmark.generate(SKILL_ROOT / "should-not-exist")
        self.assertIn("inside the skill package", str(caught.exception))
        self.assertFalse((SKILL_ROOT / "should-not-exist").exists())

    def test_generate_refuses_a_skill_ancestor(self):
        with self.assertRaises(ValueError) as caught:
            benchmark.generate(SKILL_ROOT.parent)
        self.assertIn("ancestor of the skill package", str(caught.exception))

    def test_generate_writes_the_expected_layout(self):
        workspace = self.generated()
        for name in ("textstats.py", "report.py", "integration_check.py"):
            self.assertTrue((workspace / name).is_file(), name)
        self.assertTrue((workspace / "tests" / "test_textstats.py").is_file())
        self.assertTrue((workspace / "tests" / "test_report.py").is_file())
        self.assertTrue((workspace.parent / "reference" / "textstats.py").is_file())
        self.assertTrue((workspace.parent / "brief.md").is_file())
        self.assertTrue((workspace.parent / "fixture_hashes.json").is_file())

    def test_fixture_hashes_match_the_shipped_files(self):
        workspace = self.generated()
        recorded = json.loads(
            (workspace.parent / "fixture_hashes.json").read_text(encoding="utf-8")
        )
        for name, digest in recorded.items():
            self.assertEqual(digest, benchmark.sha256_path(workspace / name), name)

    def test_cli_generate_exit_codes(self):
        run = subprocess.run(
            [sys.executable, str(SKILL_ROOT / "scripts" / "benchmark.py"),
             "generate", "--dest", str(self.dest / "cli")],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(0, run.returncode, run.stderr)
        self.assertIn("task_workspace", run.stdout)


class Grading(BenchmarkCase):
    """The grader measures the workspace, not the story told about it."""

    def test_initial_fixture_fails_and_reference_passes(self):
        workspace = self.generated()
        stub = benchmark.grade(workspace)
        self.assertFalse(stub["ok"], stub["requirements_passed"])
        self.assertEqual(["R1", "R2", "R3", "R4"], stub["requirements_failed"])
        self.assertEqual("pass", stub["checks"]["stdlib_only"]["outcome"])
        self.assertEqual("intact", stub["integrity"])

        self.apply_reference(workspace, "textstats.py", "report.py")
        solved = benchmark.grade(workspace)
        self.assertTrue(solved["ok"], solved)
        self.assertEqual(
            ["R1", "R2", "R3", "R4", "R5"], solved["requirements_passed"]
        )

    def test_partial_progress_is_reported_per_requirement(self):
        workspace = self.generated()
        self.apply_reference(workspace, "textstats.py")
        partial = benchmark.grade(workspace)
        self.assertFalse(partial["ok"])
        self.assertEqual(["R1", "R2", "R5"], partial["requirements_passed"])
        self.assertEqual(["R3", "R4"], partial["requirements_failed"])

    def test_reference_solution_is_not_applied_by_the_grader(self):
        workspace = self.generated()
        benchmark.grade(workspace)
        self.assertIn("worker-a owns word_count", (workspace / "textstats.py").read_text())

    def test_grade_flags_a_modified_shipped_test(self):
        workspace = self.generated()
        self.apply_reference(workspace, "textstats.py", "report.py")
        target = workspace / "tests" / "test_textstats.py"
        target.write_text("import unittest\n\n\nclass T(unittest.TestCase):\n    pass\n",
                          encoding="utf-8")
        result = benchmark.grade(workspace)
        self.assertFalse(result["ok"])
        self.assertEqual("modified", result["integrity"])
        self.assertIn("was modified", result["checks"]["fixture_integrity"]["stdout"])

    def test_grade_flags_a_deleted_shipped_test(self):
        workspace = self.generated()
        self.apply_reference(workspace, "textstats.py", "report.py")
        (workspace / "tests" / "test_report.py").unlink()
        result = benchmark.grade(workspace)
        self.assertFalse(result["ok"])
        self.assertNotEqual("intact", result["integrity"])

    def test_integrity_failure_skips_execution_entirely(self):
        workspace = self.generated()
        self.apply_reference(workspace, "textstats.py", "report.py")
        (workspace / "tests" / "test_report.py").unlink()
        result = benchmark.grade(workspace)
        self.assertIn("skipped", result["execution"])
        self.assertEqual("skipped", result["checks"]["tests_report"]["outcome"])
        self.assertIsNone(result["checks"]["tests_report"]["exit_status"])
        self.assertEqual(
            ["not_run"], sorted({r["status"] for r in result["requirements"]})
        )

    def test_missing_manifest_fails_before_execution(self):
        workspace = self.generated()
        self.apply_reference(workspace, "textstats.py", "report.py")
        (workspace.parent / "fixture_hashes.json").unlink()
        result = benchmark.grade(workspace)
        self.assertFalse(result["ok"])
        self.assertIn(
            "missing", result["checks"]["fixture_integrity"]["stdout"]
        )
        self.assertIn("skipped", result["execution"])

    def test_null_manifest_is_rejected(self):
        workspace = self.generated()
        self.apply_reference(workspace, "textstats.py", "report.py")
        (workspace.parent / "fixture_hashes.json").write_text("null\n", encoding="utf-8")
        result = benchmark.grade(workspace)
        self.assertFalse(result["ok"])
        self.assertIn(
            "must be a JSON object",
            result["checks"]["fixture_integrity"]["stdout"],
        )
        self.assertIn("skipped", result["execution"])

    def test_non_object_manifests_are_rejected(self):
        for index, payload in enumerate(("[]", '"hashes"', "42", "{}")):
            with self.subTest(payload=payload):
                summary = benchmark.generate(self.dest / f"bench-{index}")
                workspace = Path(summary["task_workspace"])
                self.apply_reference(workspace, "textstats.py", "report.py")
                (workspace.parent / "fixture_hashes.json").write_text(
                    payload, encoding="utf-8"
                )
                result = benchmark.grade(workspace)
                self.assertFalse(result["ok"], payload)
                self.assertIn("skipped", result["execution"])

    def test_tampered_manifest_cannot_relax_grading(self):
        workspace = self.generated()
        (workspace / "tests" / "test_textstats.py").write_text(
            "import unittest\n\n\nclass T(unittest.TestCase):\n    pass\n",
            encoding="utf-8",
        )
        # A writable local manifest claiming the tampered file is fine.
        (workspace.parent / "fixture_hashes.json").write_text(
            json.dumps({"tests/test_textstats.py": "0" * 64}), encoding="utf-8"
        )
        result = benchmark.grade(workspace)
        self.assertFalse(result["ok"])
        self.assertIn("skipped", result["execution"])

    def test_canonical_hashes_come_from_the_skill_package(self):
        canonical = benchmark.canonical_check_hashes()
        self.assertIn("tests/__init__.py", canonical)
        self.assertEqual(sorted(benchmark.SHIPPED_CHECK_FILES), sorted(canonical))
        for name, digest in canonical.items():
            self.assertEqual(
                digest,
                benchmark.sha256_path(benchmark.TEMPLATE_ROOT / "task" / name),
                name,
            )

    def test_grade_fails_when_a_module_is_deleted(self):
        workspace = self.generated()
        self.apply_reference(workspace, "textstats.py", "report.py")
        (workspace / "report.py").unlink()
        result = benchmark.grade(workspace)
        self.assertFalse(result["ok"])
        self.assertEqual("fail", result["checks"]["stdlib_only"]["outcome"])

    def test_observations_are_recorded_and_never_graded(self):
        workspace = self.generated()
        claim = {"transcript": "the agent said all tests passed"}
        result = benchmark.grade(workspace, observations=claim)
        self.assertFalse(result["ok"], "a transcript claim must not turn a failure into a pass")
        self.assertEqual(claim, result["manual_observations"])
        self.assertIn("never parsed", result["manual_observations_note"])

    def test_grader_ignores_commands_supplied_by_the_workspace(self):
        workspace = self.generated()
        self.apply_reference(workspace, "textstats.py", "report.py")
        # A hostile or careless workspace copy must not steer the grader.
        (workspace / "requirements.json").write_text(
            json.dumps(
                {
                    "requirements": [
                        {
                            "id": "R1",
                            "text": "injected",
                            "checks": ["rm -rf /"],
                            "owner": "attacker",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        result = benchmark.grade(workspace)
        self.assertTrue(result["ok"], result)
        commands = " ".join(
            check["command"] for check in result["checks"].values()
        )
        self.assertNotIn("rm -rf", commands)
        for check in result["checks"].values():
            self.assertTrue(
                check["command"].startswith(sys.executable)
                or check["command"].startswith("ast import scan")
                or check["command"].startswith("sha256 comparison"),
                check["command"],
            )

    def test_grade_rejects_a_missing_workspace(self):
        with self.assertRaises(ValueError):
            benchmark.grade(self.dest / "nothing-here")

    def test_report_states_the_evidence_boundary(self):
        workspace = self.generated()
        result = benchmark.grade(workspace)
        joined = " ".join(result["caveats"])
        self.assertIn("not agent behaviour", joined)
        self.assertIn("single-agent baseline", joined)

    def test_cli_grade_exit_codes_and_report_file(self):
        workspace = self.generated()
        out = self.dest / "report.json"
        run = subprocess.run(
            [sys.executable, str(SKILL_ROOT / "scripts" / "benchmark.py"),
             "grade", "--workspace", str(workspace), "--out", str(out)],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(1, run.returncode)
        self.assertTrue(out.is_file())
        self.assertIn("requirements_failed", json.loads(out.read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
