#!/usr/bin/env python3
"""Disposable benchmark fixture and outcome grader for the ``parallelism`` skill.

Ships in ``templates/benchmark``: a small two-module Python task with explicit
user requirements, split file ownership, tests that fail against the shipped
stubs, and an integration check. The grader runs those checks against the
*actual* resulting files and reports a per-requirement outcome.

Deliberate limits, so no claim outruns the evidence:

* No model API, CLI, or agent is invoked here. This module never measures agent
  behaviour; it measures the workspace the agents left behind.
* Every graded command is fixed in this file; nothing read from the graded
  workspace is executed as a shell command. The fixed commands still run the
  graded `unittest` modules and `integration_check.py` as normal Python code with
  the host's usual permissions, so grade only workspaces you are willing to run.
* The standard-library check parses imports statically with `ast`. It sees static
  `import` and `from ... import` statements only; a dynamic import inside a
  function is not detected.
* The expected hashes of the shipped checks are read from this package, never
  from the graded workspace, so a writable local manifest cannot relax grading.
* Transcript text and human review notes supplied with ``--observations`` are
  recorded beside the result and never count towards pass or fail.
* One run proves nothing about team performance. Compare repeated comparable
  runs against a single-agent baseline before drawing conclusions.

Commands::

    python3 scripts/benchmark.py generate --dest /tmp/par-bench
    python3 scripts/benchmark.py grade --workspace /tmp/par-bench/task \\
        --out /tmp/par-bench/report.json
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ROOT = SKILL_ROOT / "templates" / "benchmark"

CAVEATS = [
    "This grader measures the resulting workspace, not agent behaviour or model routing.",
    "Offline or synthetic runs are not evidence of live model quality.",
    "Compare repeated comparable runs against a single-agent baseline before "
    "drawing performance conclusions.",
]
MANUAL_NOTES = (
    "Supplied transcript or review notes are recorded here for humans. "
    "They are never parsed into a pass or fail."
)

SHIPPED_CHECK_FILES = (
    "tests/__init__.py",
    "tests/test_textstats.py",
    "tests/test_report.py",
    "integration_check.py",
)


def sha256_path(path: Path) -> str:
    """Return the lowercase hex SHA-256 of a file's bytes."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_requirements() -> dict:
    """Load the shipped requirement list used for grading."""
    return json.loads((TEMPLATE_ROOT / "requirements.json").read_text(encoding="utf-8"))


def canonical_check_hashes() -> dict[str, str]:
    """Return the shipped digests of the checks, read from this package.

    The graded workspace carries a copy for transparency, but grading always
    compares against these canonical values so a writable manifest cannot be
    edited to make a tampered check pass.
    """
    return {
        name: sha256_path(TEMPLATE_ROOT / "task" / name) for name in SHIPPED_CHECK_FILES
    }


def _skill_overlap(resolved: Path) -> str | None:
    """Describe how a destination overlaps the skill package, if it does."""
    skill = Path(os.path.realpath(SKILL_ROOT))
    if resolved == skill:
        return "the skill package itself"
    if skill in resolved.parents:
        return "inside the skill package"
    if resolved in skill.parents:
        return "an ancestor of the skill package"
    return None


def _assert_dest_is_safe(dest: Path) -> Path:
    """Refuse a populated destination, or one overlapping the skill package."""
    resolved = Path(os.path.realpath(dest))
    overlap = _skill_overlap(resolved)
    if overlap is not None:
        raise ValueError(
            f"refusing to write benchmark artifacts to {overlap}: {resolved}"
        )
    if resolved.exists():
        if not resolved.is_dir():
            raise ValueError(f"destination exists and is not a directory: {resolved}")
        if any(resolved.iterdir()):
            raise ValueError(
                f"destination is not empty: {resolved}. Choose a new or empty "
                "directory; this tool never replaces existing files."
            )
    return resolved


def generate(dest: Path) -> dict:
    """Write the fixture to an empty destination and summarise the created files."""
    resolved = _assert_dest_is_safe(dest)
    shutil.copytree(TEMPLATE_ROOT / "task", resolved / "task")
    shutil.copytree(TEMPLATE_ROOT / "reference", resolved / "reference")
    shutil.copy2(TEMPLATE_ROOT / "brief.md", resolved / "brief.md")
    shutil.copy2(TEMPLATE_ROOT / "requirements.json", resolved / "requirements.json")
    # A convenience copy. Grading compares the workspace against the canonical
    # hashes above, not against this file.
    hashes = canonical_check_hashes()
    (resolved / "fixture_hashes.json").write_text(
        json.dumps(hashes, indent=2) + "\n", encoding="utf-8"
    )
    return {
        "ok": True,
        "dest": str(resolved),
        "task_workspace": str(resolved / "task"),
        "reference_solution": str(resolved / "reference"),
        "brief": str(resolved / "brief.md"),
        "files": sorted(
            str(path.relative_to(resolved))
            for path in resolved.rglob("*")
            if path.is_file()
        ),
    }


def _run(command: list[str], workspace: Path, timeout: int = 120) -> dict:
    """Run one fixed check command inside the graded workspace."""
    try:
        completed = subprocess.run(
            command,
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
    except subprocess.TimeoutExpired:
        return {
            "command": " ".join(command),
            "exit_status": None,
            "outcome": "timeout",
            "stdout": "",
            "stderr": f"timed out after {timeout}s",
        }
    except OSError as exc:
        return {
            "command": " ".join(command),
            "exit_status": None,
            "outcome": "error",
            "stdout": "",
            "stderr": str(exc),
        }
    return {
        "command": " ".join(command),
        "exit_status": completed.returncode,
        "outcome": "pass" if completed.returncode == 0 else "fail",
        "stdout": completed.stdout[-4000:],
        "stderr": completed.stderr[-4000:],
    }


def _stdlib_only(workspace: Path) -> dict:
    """Check that the two delivered modules import nothing outside the standard library."""
    allowed = set(sys.stdlib_module_names) | {"textstats", "report"}
    problems: list[str] = []
    for name in ("textstats.py", "report.py"):
        path = workspace / name
        if not path.is_file():
            problems.append(f"{name} is missing")
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            problems.append(f"{name} does not parse: {exc}")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [(node.module or "").split(".")[0]]
            else:
                continue
            for imported in names:
                if imported and imported not in allowed:
                    problems.append(f"{name} imports non-standard module {imported!r}")
    return {
        "command": "ast import scan of textstats.py and report.py",
        "exit_status": 0 if not problems else 1,
        "outcome": "pass" if not problems else "fail",
        "stdout": "; ".join(problems),
        "stderr": "",
    }


def _integrity_problems(workspace: Path) -> list[str]:
    """Compare the workspace checks against the canonical shipped digests."""
    problems: list[str] = []
    canonical = canonical_check_hashes()
    manifest = workspace.parent / "fixture_hashes.json"
    if not manifest.is_file():
        problems.append("fixture_hashes.json is missing from the generated root")
    else:
        try:
            local = json.loads(manifest.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            problems.append(f"fixture_hashes.json is unreadable or not valid JSON: {exc}")
        else:
            # Any non-object value, including JSON null, is a tampered or
            # malformed manifest and must not be treated as "nothing to check".
            if not isinstance(local, dict):
                problems.append(
                    "fixture_hashes.json must be a JSON object of check file hashes, "
                    f"got {type(local).__name__}"
                )
            elif local != canonical:
                problems.append(
                    "fixture_hashes.json disagrees with the shipped canonical check hashes"
                )
    for name, digest in canonical.items():
        path = workspace / name
        if not path.is_file():
            problems.append(f"shipped file {name} was deleted")
        elif sha256_path(path) != digest:
            problems.append(f"shipped file {name} was modified")
    return problems


def _skipped_checks(problems: list[str]) -> dict:
    """Report the check set as not run, with the reason kept explicit."""
    reason = "not run: " + "; ".join(problems)
    checks = {
        "fixture_integrity": {
            "command": "sha256 comparison of shipped tests against the skill package",
            "exit_status": 1,
            "outcome": "fail",
            "stdout": "; ".join(problems),
            "stderr": "",
        }
    }
    for name, command in (
        ("tests_textstats", "python3 -m unittest tests.test_textstats"),
        ("tests_report", "python3 -m unittest tests.test_report"),
        ("integration", "python3 integration_check.py"),
        ("stdlib_only", "ast import scan of textstats.py and report.py"),
    ):
        checks[name] = {
            "command": command,
            "exit_status": None,
            "outcome": "skipped",
            "stdout": reason,
            "stderr": "",
        }
    return checks


def grade(workspace: Path, *, observations: object = None) -> dict:
    """Run the shipped checks against a resulting workspace and grade requirements."""
    workspace = Path(os.path.realpath(workspace))
    if not workspace.is_dir():
        raise ValueError(f"workspace does not exist: {workspace}")

    # Integrity is decided before anything from the workspace is executed.
    integrity_problems = _integrity_problems(workspace)
    if integrity_problems:
        checks = _skipped_checks(integrity_problems)
        results = [
            {
                "id": requirement["id"],
                "text": requirement["text"],
                "checks": requirement["checks"],
                "status": "not_run",
                "blocked_by": ["fixture_integrity"],
            }
            for requirement in load_requirements()["requirements"]
        ]
        return {
            "workspace": str(workspace),
            "checks": checks,
            "requirements": results,
            "requirements_passed": [],
            "requirements_failed": [],
            "integrity": "modified",
            "execution": "skipped: shipped-check integrity failed before execution",
            "ok": False,
            "manual_observations": observations,
            "manual_observations_note": MANUAL_NOTES,
            "caveats": CAVEATS,
        }

    checks = {
        "tests_textstats": _run(
            [sys.executable, "-m", "unittest", "tests.test_textstats"], workspace
        ),
        "tests_report": _run(
            [sys.executable, "-m", "unittest", "tests.test_report"], workspace
        ),
        "integration": _run([sys.executable, "integration_check.py"], workspace),
        "stdlib_only": _stdlib_only(workspace),
        "fixture_integrity": {
            "command": "sha256 comparison of shipped tests against the skill package",
            "exit_status": 0,
            "outcome": "pass",
            "stdout": "",
            "stderr": "",
        },
    }
    outcomes = {name: result["outcome"] for name, result in checks.items()}

    results = []
    for requirement in load_requirements()["requirements"]:
        failed = [name for name in requirement["checks"] if outcomes.get(name) != "pass"]
        results.append(
            {
                "id": requirement["id"],
                "text": requirement["text"],
                "checks": requirement["checks"],
                "status": "pass" if not failed else "fail",
                "blocked_by": failed,
            }
        )

    return {
        "workspace": str(workspace),
        "checks": checks,
        "requirements": results,
        "requirements_passed": [r["id"] for r in results if r["status"] == "pass"],
        "requirements_failed": [r["id"] for r in results if r["status"] == "fail"],
        "integrity": "intact" if not integrity_problems else "modified",
        "execution": "ran",
        "ok": all(result["status"] == "pass" for result in results)
        and not integrity_problems,
        "manual_observations": observations,
        "manual_observations_note": MANUAL_NOTES,
        "caveats": CAVEATS,
    }


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser for the benchmark helper."""
    parser = argparse.ArgumentParser(
        prog="benchmark.py",
        description=(
            "Generate a disposable two-module benchmark fixture and grade the "
            "resulting workspace. No model or agent is ever invoked."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate", help="write the fixture to a new directory")
    gen.add_argument(
        "--dest",
        required=True,
        help="new or empty destination directory; existing files are never replaced",
    )

    grade_cmd = sub.add_parser("grade", help="grade a resulting workspace")
    grade_cmd.add_argument("--workspace", required=True, help="the task workspace to grade")
    grade_cmd.add_argument("--out", help="optional path to write the JSON report")
    grade_cmd.add_argument(
        "--observations",
        help="optional JSON file with transcript or manual review notes (never graded)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns a process exit code."""
    args = build_parser().parse_args(argv)
    if args.command == "generate":
        try:
            summary = generate(Path(args.dest))
        except (ValueError, OSError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(summary, indent=2))
        return 0

    observations = None
    if args.observations:
        path = Path(args.observations)
        if not path.is_file():
            print(f"error: observations file not found: {path}", file=sys.stderr)
            return 2
        try:
            observations = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print(f"error: observations file is not valid JSON: {exc}", file=sys.stderr)
            return 2
    try:
        report = grade(Path(args.workspace), observations=observations)
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    text = json.dumps(report, indent=2)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
