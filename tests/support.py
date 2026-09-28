#!/usr/bin/env python3
"""Shared fixtures for the parallelism skill harness.

The helpers here build real temporary workspaces and real ledger documents so
tests exercise files, hashes, and state transitions rather than wording.
"""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path


def sha256_file(path: Path) -> str:
    """Return the lowercase hex SHA-256 of a file."""
    digest = hashlib.sha256()
    digest.update(Path(path).read_bytes())
    return digest.hexdigest()


def write_file(path: Path, text: str) -> Path:
    """Create parents as needed and write UTF-8 text."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def make_workspace(root: Path) -> Path:
    """Create a tiny project workspace with the two modules the ledger tracks."""
    workspace = root / "project"
    write_file(workspace / "src" / "a.py", "VALUE = 1\n")
    write_file(workspace / "src" / "b.py", "OTHER = 2\n")
    write_file(workspace / "src" / "c.py", "THIRD = 3\n")
    write_file(workspace / "docs" / "notes.md", "notes\n")
    return workspace


def base_ledger(workspace: Path) -> dict:
    """Return a valid ledger: one accepted implementation, its verifier, one queued task."""
    artifact_hash = sha256_file(workspace / "src" / "a.py")
    return {
        "schema_version": 1,
        "objective": {
            "id": "obj-demo",
            "text": "Make src/a.py expose VALUE and src/b.py expose OTHER.",
            "amendments": [
                {
                    "id": "amd-verify",
                    "text": "Verify src/a.py independently before accepting it.",
                    "recorded_at": "2026-09-28T00:00:00Z",
                }
            ],
        },
        "requirements": [
            {"id": "R1", "text": "src/a.py exposes VALUE == 1.", "source": "objective"},
            {"id": "R2", "text": "src/b.py exposes OTHER == 2.", "source": "amd-verify"},
        ],
        "baseline": {
            "workspace": str(workspace),
            "revision": "baseline-abc123",
            "dirty": False,
        },
        "limits": {
            "max_concurrent_workers": 3,
            "max_total_workers": 10,
            "review_backlog_limit": 2,
            "budget_usd": None,
            "notes": "No paid runs authorised.",
        },
        "resources": [
            {"id": "res-manifest", "kind": "exclusive", "description": "shared manifest"}
        ],
        "artifacts": [
            {
                "id": "art-a",
                "task_id": "T1",
                "path": "src/a.py",
                "sha256": artifact_hash,
                "kind": "source",
            }
        ],
        "tasks": [
            {
                "id": "T1",
                "type": "implementation",
                "objective": "Implement VALUE in src/a.py.",
                "state": "accepted",
                "importance": "important",
                "requirements": ["R1"],
                "depends_on": [],
                "owner": {"actor": "worker-a", "model": "deepseek/deepseek-v4.1-flash"},
                "write_paths": ["src/a.py"],
                "read_paths": [],
                "resources": ["res-manifest"],
                "acceptance": [
                    {
                        "id": "A1-1",
                        "criterion": "src/a.py exposes VALUE == 1.",
                        "requirement_ids": ["R1"],
                    }
                ],
                "evidence": [
                    {
                        "id": "ev-1",
                        "acceptance": ["A1-1"],
                        "command": "python3 -m unittest discover -s tests",
                        "exit_status": 0,
                        "status": "pass",
                        "artifact": "art-a",
                    }
                ],
                "review": {
                    "reviewer_actor": "astra-1",
                    "reviewer_model": "gpt-6-astra",
                    "decision": "accept",
                    "at": "2026-09-28T00:30:00Z",
                    "independent": True,
                    "verified_hashes": {"art-a": artifact_hash},
                },
            },
            {
                "id": "T2",
                "type": "verification",
                "objective": "Independently reproduce VALUE in src/a.py.",
                "state": "accepted",
                "requirements": ["R1"],
                # A verifier consumes the frozen revision of its target; it must
                # not depend on the target being accepted first, which would
                # deadlock when the target itself needs independent verification.
                "depends_on": [],
                "owner": {"actor": "worker-b", "model": "deepseek/deepseek-v4.1-flash"},
                "write_paths": [],
                "read_paths": ["src/a.py"],
                "resources": [],
                "verifies": "T1",
                "acceptance": [
                    {
                        "id": "A2-1",
                        "criterion": "An independent run reproduces VALUE == 1.",
                        "requirement_ids": ["R1"],
                    }
                ],
                "evidence": [
                    {
                        "id": "ev-2",
                        "acceptance": ["A2-1"],
                        "command": "python3 -c 'import src.a as m; assert m.VALUE == 1'",
                        "exit_status": 0,
                        "status": "pass",
                        "artifact": "art-a",
                    }
                ],
                "review": {
                    "reviewer_actor": "astra-1",
                    "reviewer_model": "gpt-6-astra",
                    "decision": "accept",
                    "at": "2026-09-28T00:45:00Z",
                    "independent": True,
                    "verified_hashes": {"art-a": artifact_hash},
                },
            },
            {
                "id": "T3",
                "type": "implementation",
                "objective": "Implement OTHER in src/b.py.",
                "state": "queued",
                "requirements": ["R2"],
                "depends_on": [],
                "write_paths": ["src/b.py"],
                "read_paths": [],
                "resources": ["res-manifest"],
                "acceptance": [
                    {
                        "id": "A3-1",
                        "criterion": "src/b.py exposes OTHER == 2.",
                        "requirement_ids": ["R2"],
                    }
                ],
                "evidence": [],
            },
        ],
        "observations": {
            "total_elapsed_s": None,
            "worker_turns": 2,
            "provider_reported_tokens": None,
            "source": "manual",
        },
    }


def clone(document: dict) -> dict:
    """Deep-copy a ledger document so a test can mutate it freely."""
    return copy.deepcopy(document)


def task(document: dict, task_id: str) -> dict:
    """Return one task record by id."""
    for entry in document["tasks"]:
        if entry.get("id") == task_id:
            return entry
    raise KeyError(task_id)


def add_task(document: dict, **fields) -> dict:
    """Append a task record, filling the fields every task needs."""
    ident = fields.pop("id")
    record = {
        "id": ident,
        "type": fields.pop("type", "implementation"),
        "objective": fields.pop("objective", "bounded test task"),
        "state": fields.pop("state", "queued"),
        "requirements": fields.pop("requirements", ["R1"]),
        "depends_on": fields.pop("depends_on", []),
        "write_paths": fields.pop("write_paths", []),
        "read_paths": fields.pop("read_paths", []),
        "resources": fields.pop("resources", []),
        "acceptance": fields.pop(
            "acceptance",
            [
                {
                    "id": f"{ident}-1",
                    "criterion": "placeholder criterion",
                    "requirement_ids": ["R1"],
                }
            ],
        ),
        "evidence": fields.pop("evidence", []),
    }
    record.update(fields)
    document["tasks"].append(record)
    return record


def dispatch(document: dict, task_id: str, actor: str, model: str = "deepseek/deepseek-v4.1-flash") -> None:
    """Move a task to running with an explicit owner."""
    entry = task(document, task_id)
    entry["state"] = "running"
    entry["owner"] = {"actor": actor, "model": model}


def drop_task(document: dict, task_id: str) -> None:
    """Remove a task and any acceptance criteria only it declared."""
    document["tasks"] = [t for t in document["tasks"] if t.get("id") != task_id]
    for artifact in document.get("artifacts", []):
        if artifact.get("task_id") == task_id:
            raise AssertionError(f"drop_task would orphan artifact {artifact.get('id')!r}")


class LedgerCase(unittest.TestCase):
    """Base class that owns a temporary directory for one test."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="parallelism-test-"))
        self.workspace = make_workspace(self.tmp)
        self.ledger_path = self.tmp / "ledger.json"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def document(self) -> dict:
        """Return a fresh valid ledger for this test's workspace."""
        return base_ledger(self.workspace)

    def write_ledger(self, document: dict) -> Path:
        """Persist a ledger next to the workspace and return its path."""
        self.ledger_path.write_text(json.dumps(document, indent=2), encoding="utf-8")
        return self.ledger_path

    def validate(self, document: dict, *, verify_files: bool = True):
        """Validate a ledger in-process against the module under test."""
        import workflow

        return workflow.validate_ledger(
            document, base_dir=self.tmp, verify_files=verify_files
        )

    def schedule(self, document: dict, **kwargs):
        """Schedule a ledger in-process against the module under test."""
        import workflow

        return workflow.schedule(
            document,
            base_dir=self.tmp,
            host_capacity=kwargs.pop("host_capacity", 6),
            **kwargs,
        )

    def assertInvalid(self, report, *fragments: str) -> None:
        """Assert the report failed and every fragment appears in some error."""
        self.assertFalse(report.ok, f"expected errors, got warnings only: {report.warnings}")
        joined = "\n".join(report.errors)
        for fragment in fragments:
            self.assertIn(
                fragment,
                joined,
                f"expected an error mentioning {fragment!r}; errors were:\n{joined}",
            )
