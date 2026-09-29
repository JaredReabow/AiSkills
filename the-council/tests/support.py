#!/usr/bin/env python3
"""Shared fixtures for The Council harness.

The helpers build a real frozen package on disk and a real packet document, so
the tests exercise artifact bytes, hashes, cross-references, and round history
rather than wording. A ``COMPLETE`` outcome requires the frozen bytes to be
verified, so every fixture that expects acceptance is bound to a temporary
package directory.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_ROOT / "scripts"
COUNCIL_PY = SCRIPTS_DIR / "council.py"
TEMPLATE = SKILL_ROOT / "templates" / "council-packet.example.json"

PYTHON = sys.executable

sys.dont_write_bytecode = True
sys.path.insert(0, str(SCRIPTS_DIR))
import council  # noqa: E402

# A task manifest small enough to inline, standing in for the frozen original
# request and amendments the real workflow stores beside the artifacts.
TASK_MANIFEST_TEXT = json.dumps(
    {
        "task": "Give one to three reviewers the same frozen package.",
        "amendments": [
            "Completion needs every reviewer accepting one package version.",
        ],
    },
    indent=2,
) + "\n"

PACKAGE_FILES = {
    "task.json": TASK_MANIFEST_TEXT,
    "SKILL.md": "---\nname: the-council\n---\n\n# The Council\n",
    "scripts/council.py": "# frozen copy of the decision checker\n",
}


def hex64(seed: str) -> str:
    """Return a deterministic lowercase hex SHA-256 digest for a seed string."""
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def materialize_package(root: Path) -> list:
    """Write a real frozen package under ``root`` and return its artifact list."""
    artifacts = []
    for relative, text in PACKAGE_FILES.items():
        target = Path(root) / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        artifacts.append(
            {
                "path": relative,
                "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            }
        )
    return artifacts


def synthetic_artifacts() -> list:
    """Return hash-only artifacts for shape tests that never verify bytes."""
    return [
        {"path": relative, "sha256": hex64(relative)}
        for relative in sorted(PACKAGE_FILES)
    ]


def finalize(document: dict) -> dict:
    """Recompute the package fingerprint and bind it to rounds, reports, votes."""
    fingerprint = council.package_fingerprint_from_packet(document)
    if isinstance(document.get("package"), dict):
        document["package"]["fingerprint"] = fingerprint
    for round_entry in document["rounds"]:
        if isinstance(round_entry, dict):
            round_entry["package_fingerprint"] = fingerprint
    for report in document.get("initial_reports", []):
        if isinstance(report, dict):
            report["package_fingerprint"] = fingerprint
    for final in document["reviewer_finals"]:
        if isinstance(final, dict):
            final["package_fingerprint"] = fingerprint
    for finding in document["findings"]:
        if not isinstance(finding, dict):
            continue
        resolution = finding.get("resolution")
        if isinstance(resolution, dict):
            resolution["package_fingerprint"] = fingerprint
    return document


def base_packet(artifacts: list | None = None) -> dict:
    """Return a valid council packet.

    With no ``artifacts`` the packet carries hash-only entries, which is enough
    for shape tests but can never verify, so it cannot reach ``COMPLETE``. Pass
    :func:`materialize_package` output when a verified outcome is required.
    """
    if artifacts is None:
        artifacts = synthetic_artifacts()
    document = {
        "schema_version": 1,
        "council_id": "council-parallelism-the-council",
        "package": {
            "id": "pkg-the-council",
            "version": "3fc08c1+council",
            "fingerprint": "0" * 64,
            "task_manifest": "task.json",
            "produced_by": ["worker-a", "worker-b"],
            "artifacts": copy.deepcopy(artifacts),
        },
        "requirements": [
            {
                "id": "R1",
                "text": "Parallelism keeps its current single-reviewer behaviour.",
            },
            {
                "id": "R2",
                "text": "The Council seats one to three independent reviewers.",
            },
            {
                "id": "R3",
                "text": "Completion needs every reviewer accepting one package.",
            },
        ],
        "required_checks": [
            {
                "id": "C1",
                "text": "council harness passes",
                "criteria": "tests/run_harness.py exits 0",
                "status": "passed",
            },
            {
                "id": "C2",
                "text": "parallelism harness still passes",
                "criteria": "tests/run_harness.py exits 0 in the parent skill",
                "status": "passed",
            },
        ],
        "integration": {
            "mode": "council",
            "lead_planner_actor": "rev-lead",
            "single_reviewer_actor": None,
            "panel_generation": 1,
            "mode_history": [],
        },
        "panel": [
            {
                "actor": "rev-lead",
                "model": "gpt-6-sol",
                "reasoning_effort": "medium",
                "role": "lead",
            },
            {
                "actor": "rev-2",
                "model": "gpt-6-sol",
                "reasoning_effort": "medium",
                "role": "reviewer",
            },
            {
                "actor": "rev-3",
                "model": "gpt-6-sol",
                "reasoning_effort": "medium",
                "role": "reviewer",
            },
        ],
        "rounds": [
            {
                "id": "round-1-independent",
                "kind": "independent",
                "package_fingerprint": "0" * 64,
                "new_evidence": False,
                "reviewers": ["rev-lead", "rev-2", "rev-3"],
                "raised_finding_ids": ["F1", "F2"],
            },
            {
                "id": "round-2-reconciliation",
                "kind": "reconciliation",
                "package_fingerprint": "0" * 64,
                "new_evidence": True,
                "reviewers": ["rev-lead", "rev-2", "rev-3"],
                "raised_finding_ids": [],
            },
        ],
        "initial_reports": [
            {
                "actor": "rev-lead",
                "round_id": "round-1-independent",
                "package_fingerprint": "0" * 64,
                "panel_generation": 1,
                "finding_ids": [],
            },
            {
                "actor": "rev-2",
                "round_id": "round-1-independent",
                "package_fingerprint": "0" * 64,
                "panel_generation": 1,
                "finding_ids": ["F2"],
            },
            {
                "actor": "rev-3",
                "round_id": "round-1-independent",
                "package_fingerprint": "0" * 64,
                "panel_generation": 1,
                "finding_ids": ["F1"],
            },
        ],
        "findings": [
            {
                "id": "F1",
                "origin_actor": "rev-3",
                "raised_at_round": "round-1-independent",
                "requirement_ids": ["R2"],
                "classification": "blocking",
                "status": "resolved",
                "summary": "Fourth reviewer accepted by the panel parser.",
                "resolution": {
                    "reason": "Panel cap is enforced and covered by a test.",
                    "evidence": [
                        "tests/test_council.py::PanelRules",
                        "scripts/council.py panel()",
                    ],
                    "package_fingerprint": "0" * 64,
                },
            },
            {
                "id": "F2",
                "origin_actor": "rev-2",
                "raised_at_round": "round-1-independent",
                "requirement_ids": ["R1"],
                "classification": "suggestion",
                "status": "open",
                "summary": "Could document the standalone invocation in the README.",
            },
        ],
        "reviewer_finals": [
            {
                "actor": "rev-lead",
                "round_id": "round-2-reconciliation",
                "package_fingerprint": "0" * 64,
                "panel_generation": 1,
                "covered_requirement_ids": ["R1", "R2", "R3"],
                "verdict": "COMPLETE",
                "outcomes": [
                    {"finding_id": "F1", "disposition": "resolved"},
                    {"finding_id": "F2", "disposition": "not_applicable"},
                ],
            },
            {
                "actor": "rev-2",
                "round_id": "round-2-reconciliation",
                "package_fingerprint": "0" * 64,
                "panel_generation": 1,
                "covered_requirement_ids": ["R1", "R2", "R3"],
                "verdict": "COMPLETE",
                "outcomes": [
                    {"finding_id": "F1", "disposition": "resolved"},
                    {"finding_id": "F2", "disposition": "accepted"},
                ],
            },
            {
                "actor": "rev-3",
                "round_id": "round-2-reconciliation",
                "package_fingerprint": "0" * 64,
                "panel_generation": 1,
                "covered_requirement_ids": ["R1", "R2", "R3"],
                "verdict": "COMPLETE",
                "outcomes": [
                    {"finding_id": "F1", "disposition": "resolved"},
                    {"finding_id": "F2", "disposition": "not_applicable"},
                ],
            },
        ],
    }
    return finalize(document)


def packet(artifacts: list | None = None, **overrides) -> dict:
    """Return a finalised packet with top-level overrides applied."""
    document = copy.deepcopy(base_packet(artifacts))
    document.update(copy.deepcopy(overrides))
    return finalize(document)


def single_reviewer_packet(artifacts: list | None = None) -> dict:
    """Return a valid one-seat council packet, used for self-reconciliation tests."""
    document = packet(artifacts)
    document["panel"] = [
        {
            "actor": "rev-solo",
            "model": "gpt-6-sol",
            "reasoning_effort": "medium",
            "role": "lead",
        }
    ]
    document["integration"]["lead_planner_actor"] = "rev-solo"
    for entry in document["rounds"]:
        entry["reviewers"] = ["rev-solo"]
    document["initial_reports"] = [
        {
            "actor": "rev-solo",
            "round_id": document["rounds"][0]["id"],
            "package_fingerprint": document["package"]["fingerprint"],
            "panel_generation": 1,
            "finding_ids": ["F1", "F2"],
        }
    ]
    document["findings"][0]["origin_actor"] = "rev-solo"
    document["findings"][1]["origin_actor"] = "rev-solo"
    document["reviewer_finals"] = [
        {
            "actor": "rev-solo",
            "round_id": document["rounds"][-1]["id"],
            "package_fingerprint": document["package"]["fingerprint"],
            "panel_generation": 1,
            "covered_requirement_ids": ["R1", "R2", "R3"],
            "verdict": "COMPLETE",
            "outcomes": [
                {"finding_id": "F1", "disposition": "resolved"},
                {"finding_id": "F2", "disposition": "accepted"},
            ],
        }
    ]
    return finalize(document)


def write_packet(path: Path, document: dict) -> Path:
    """Serialise a packet to ``path`` and return the path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return path


def set_generation(document: dict, generation: int) -> dict:
    """Re-form the panel at a new generation, binding every report and vote."""
    document["integration"]["panel_generation"] = generation
    for report in document["initial_reports"]:
        report["panel_generation"] = generation
    for final in document["reviewer_finals"]:
        final["panel_generation"] = generation
    return document


def run_cli(*args: str) -> subprocess.CompletedProcess:
    """Run the checker CLI in a subprocess and capture its output."""
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run(
        [PYTHON, str(COUNCIL_PY), *args],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def problem_codes(report: dict) -> set:
    """Return the set of problem codes in a report."""
    return {problem.split(":", 1)[0] for problem in report["problems"]}


def note_codes(report: dict) -> set:
    """Return the set of note codes in a report."""
    return {note.split(":", 1)[0] for note in report["notes"]}
