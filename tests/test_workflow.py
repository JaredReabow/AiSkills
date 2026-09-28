#!/usr/bin/env python3
"""Invariant tests for scripts/workflow.py.

Four named scenarios required by the skill are covered here:

* ``test_scenario_missing_info_is_surfaced_not_guessed``
* ``test_scenario_contradictory_success_claim_is_rejected``
* ``test_scenario_conflicting_writes_is_rejected``
* ``test_scenario_revised_artifact_invalidates_acceptance``
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

import support
import workflow

SKILL_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_LEDGER = SKILL_ROOT / "templates" / "ledger.example.json"


class LedgerBasics(support.LedgerCase):
    """Positive path: a well-formed ledger validates and stays untouched."""

    def test_valid_ledger_passes_without_warnings(self):
        report = self.validate(self.document())
        self.assertTrue(report.ok, report.errors)
        self.assertEqual([], report.warnings)

    def test_shipped_example_validates_against_real_file_hashes(self):
        document = workflow.load_ledger(EXAMPLE_LEDGER)
        report = workflow.validate_ledger(
            document, base_dir=EXAMPLE_LEDGER.parent, verify_files=True
        )
        self.assertTrue(report.ok, report.errors)
        self.assertEqual([], report.warnings)

    def test_validate_and_schedule_never_mutate_files(self):
        document = self.document()
        support.task(document, "T3")["state"] = "running"
        support.task(document, "T3")["owner"] = {"actor": "worker-c", "model": "m"}
        path = self.write_ledger(document)
        before = {
            "ledger": path.read_bytes(),
            "a": (self.workspace / "src" / "a.py").read_bytes(),
            "mtime": path.stat().st_mtime_ns,
        }
        report = workflow.validate_ledger(document, base_dir=self.tmp, verify_files=True)
        self.assertTrue(report.ok, report.errors)
        result = workflow.schedule(document, base_dir=self.tmp, host_capacity=6)
        self.assertTrue(result["ok"], result.get("errors"))
        self.assertEqual(before["ledger"], path.read_bytes())
        self.assertEqual(before["mtime"], path.stat().st_mtime_ns)
        self.assertEqual(before["a"], (self.workspace / "src" / "a.py").read_bytes())

    def test_cli_reports_exit_codes(self):
        good = self.write_ledger(self.document())
        run = subprocess.run(
            [sys.executable, str(SKILL_ROOT / "scripts" / "workflow.py"),
             "validate", "--ledger", str(good)],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(0, run.returncode, run.stdout + run.stderr)
        self.assertIn("ledger valid", run.stdout)

        broken = support.clone(self.document())
        support.task(broken, "T3")["state"] = "invented-state"
        bad = self.write_ledger(broken)
        run = subprocess.run(
            [sys.executable, str(SKILL_ROOT / "scripts" / "workflow.py"),
             "validate", "--ledger", str(bad)],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(1, run.returncode)
        self.assertIn("error:", run.stdout)

    def test_cli_rejects_unreadable_input_with_exit_2(self):
        missing = self.tmp / "absent.json"
        run = subprocess.run(
            [sys.executable, str(SKILL_ROOT / "scripts" / "workflow.py"),
             "validate", "--ledger", str(missing)],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(2, run.returncode)


class PathOwnership(support.LedgerCase):
    """Ownership is a tree of real absolute paths, and path checks never raise."""

    def in_flight(self, *task_ids: str, actor_prefix: str = "worker") -> dict:
        """Base ledger with the named tasks running under distinct actors."""
        document = self.document()
        support.drop_task(document, "T2")
        for index, task_id in enumerate(task_ids):
            support.dispatch(document, task_id, f"{actor_prefix}-{index}")
        return document

    def test_ancestor_and_descendant_write_paths_conflict(self):
        document = self.in_flight("T1")
        support.add_task(
            document, id="T4", state="running", write_paths=["src"],
            owner={"actor": "worker-d", "model": "m"},
        )
        self.assertInvalid(self.validate(document), "ownership conflict")

    def test_deeply_nested_write_paths_conflict(self):
        document = self.in_flight("T1")
        support.add_task(
            document, id="T4", state="running", write_paths=["src/nested"],
            owner={"actor": "worker-d", "model": "m"},
        )
        support.add_task(
            document, id="T5", state="running", write_paths=["src/nested/deep/mod.py"],
            owner={"actor": "worker-e", "model": "m"},
        )
        self.assertInvalid(self.validate(document), "ownership conflict")

    def test_sibling_prefixes_do_not_conflict(self):
        document = self.in_flight("T1")
        support.add_task(
            document, id="T4", state="running", write_paths=["src2/a.py"],
            owner={"actor": "worker-d", "model": "m"},
        )
        report = self.validate(document)
        self.assertTrue(report.ok, report.errors)

    def test_nested_workspace_roots_naming_the_same_file_conflict(self):
        document = self.document()
        support.drop_task(document, "T2")
        support.dispatch(document, "T1", "worker-a")  # writes src/a.py
        support.add_task(
            document, id="T4", state="running", workspace="src",
            write_paths=["a.py"], owner={"actor": "worker-d", "model": "m"},
        )
        self.assertInvalid(self.validate(document), "ownership conflict")

    def test_unsafe_path_is_reported_without_raising(self):
        document = self.in_flight("T1")
        support.add_task(
            document, id="T4", state="running", write_paths=["../escape.py"],
            owner={"actor": "worker-d", "model": "m"},
        )
        report = self.validate(document)  # must not raise PathEscape
        self.assertInvalid(report, "unsafe write path")

    def test_embedded_nul_path_is_reported_without_raising(self):
        document = self.in_flight("T1")
        support.add_task(
            document, id="T4", state="running", write_paths=["src/\x00bad.py"],
            owner={"actor": "worker-d", "model": "m"},
        )
        report = self.validate(document)
        self.assertInvalid(report, "unsafe write path")

    def test_embedded_nul_artifact_path_is_reported_without_raising(self):
        document = self.document()
        document["artifacts"][0]["path"] = "src/\x00bad.py"
        report = self.validate(document)
        self.assertInvalid(report, "unsafe artifact path")

    def test_reader_conflicts_with_a_live_writer(self):
        document = self.in_flight("T1")
        support.add_task(
            document, id="T4", state="running", read_paths=["src/a.py"],
            owner={"actor": "worker-d", "model": "m"},
        )
        self.assertInvalid(self.validate(document), "read path")

    def test_writer_conflicts_with_a_live_reader(self):
        document = self.in_flight("T1")
        support.task(document, "T1")["read_paths"] = ["src/b.py"]
        support.add_task(
            document, id="T4", state="running", write_paths=["src/b.py"],
            owner={"actor": "worker-d", "model": "m"},
        )
        self.assertInvalid(self.validate(document), "read path")

    def test_two_readers_do_not_conflict(self):
        document = self.in_flight("T1")
        support.task(document, "T1")["read_paths"] = ["docs/notes.md"]
        support.add_task(
            document, id="T4", state="running", read_paths=["docs/notes.md"],
            owner={"actor": "worker-d", "model": "m"},
        )
        report = self.validate(document)
        self.assertTrue(report.ok, report.errors)

    def test_verifier_may_read_a_frozen_target(self):
        document = self.document()
        support.task(document, "T1")["state"] = "ready_for_review"
        report = self.validate(document)
        self.assertTrue(report.ok, report.errors)

    def test_verifier_may_not_read_a_running_target(self):
        document = self.document()
        support.dispatch(document, "T1", "worker-a")
        self.assertInvalid(self.validate(document), "frozen ready_for_review")


class StateConsistency(support.LedgerCase):
    """State must agree with dependencies, targets, and actor occupancy."""

    def test_accepted_task_with_unaccepted_dependency_rejected(self):
        document = self.document()
        # T1 is accepted but depends on T3, which is still queued.
        support.task(document, "T1")["depends_on"] = ["T3"]
        self.assertInvalid(self.validate(document), "is not accepted")

    def test_in_flight_task_with_unaccepted_dependency_rejected(self):
        document = self.document()
        third = support.task(document, "T3")
        third["depends_on"] = ["T1"]
        support.dispatch(document, "T3", "worker-c")
        support.task(document, "T1")["state"] = "ready_for_review"
        support.drop_task(document, "T2")
        self.assertInvalid(self.validate(document), "is not accepted")

    def test_two_in_flight_tasks_for_one_actor_rejected(self):
        document = self.document()
        support.drop_task(document, "T2")
        support.dispatch(document, "T1", "worker-a")
        support.dispatch(document, "T3", "worker-a")
        self.assertInvalid(self.validate(document), "in-flight tasks")

    def test_unknown_verified_task_rejected(self):
        document = self.document()
        support.task(document, "T2")["verifies"] = ["T1", "T404"]
        self.assertInvalid(self.validate(document), "unknown verified task")

    def test_verifier_depending_on_its_target_rejected(self):
        document = self.document()
        support.task(document, "T2")["depends_on"] = ["T1"]
        self.assertInvalid(self.validate(document), "must not depend on the target")


class AcceptanceRules(support.LedgerCase):
    """What an accepted task must actually prove."""

    def test_accepted_with_non_accept_decision_rejected(self):
        document = self.document()
        support.task(document, "T1")["review"]["decision"] = "request_changes"
        self.assertInvalid(self.validate(document), "needs review.decision 'accept'")

    def test_empty_status_assertion_is_not_evidence(self):
        document = self.document()
        support.task(document, "T1")["evidence"] = [
            {"id": "ev-1", "acceptance": ["A1-1"], "status": "pass"}
        ]
        self.assertInvalid(self.validate(document), "bare status is not evidence")

    def test_pass_without_artifact_or_exit_status_rejected(self):
        document = self.document()
        evidence = support.task(document, "T1")["evidence"][0]
        evidence["exit_status"] = None
        evidence["artifact"] = None
        self.assertInvalid(self.validate(document), "bind to a hashed artifact")

    def test_accepted_without_artifact_bound_evidence_rejected(self):
        document = self.document()
        support.task(document, "T1")["evidence"][0]["artifact"] = None
        support.task(document, "T1")["evidence"][0]["exit_status"] = 0
        report = self.validate(document)
        self.assertFalse(report.ok, report.errors)
        self.assertTrue(
            any("artifact-bound" in error for error in report.errors), report.errors
        )

    def test_artifact_free_acceptance_must_be_declared(self):
        # T3 produces no artifact and has no dependencies, so its closure is
        # empty and the acceptance must say so explicitly.
        document = self.document()
        third = support.task(document, "T3")
        third["state"] = "accepted"
        third["owner"] = {"actor": "worker-c", "model": "m"}
        third["resources"] = []
        third["review"] = {
            "reviewer_actor": "astra-1",
            "decision": "accept",
            "verified_hashes": {},
        }
        third["evidence"] = [
            {
                "id": "ev-3",
                "acceptance": ["A3-1"],
                "command": "python3 -c 'import src.b as m; assert m.OTHER == 2'",
                "exit_status": 0,
                "status": "pass",
            }
        ]
        self.assertInvalid(self.validate(document), "artifact_free")

    def test_artifact_free_flag_rejected_when_hashes_exist(self):
        document = self.document()
        support.task(document, "T1")["review"]["artifact_free"] = True
        self.assertInvalid(self.validate(document), "closure contains hashed artifacts")

    def test_accepted_implementation_needs_its_own_hashed_artifact(self):
        document = self.document()
        document["artifacts"] = []
        support.task(document, "T1")["review"]["verified_hashes"] = {}
        verifier = support.task(document, "T2")
        verifier["review"]["verified_hashes"] = {}
        verifier["evidence"][0]["artifact"] = None
        verifier["evidence"][0]["exit_status"] = 0
        report = self.validate(document)
        self.assertFalse(report.ok)
        self.assertTrue(
            any("hashed artifact of its own" in error for error in report.errors),
            report.errors,
        )

    def test_artifact_free_is_rejected_for_implementation_work(self):
        document = self.document()
        third = support.task(document, "T3")
        third["state"] = "accepted"
        third["owner"] = {"actor": "worker-c", "model": "m"}
        third["resources"] = []
        third["evidence"] = [
            {
                "id": "ev-3",
                "acceptance": ["A3-1"],
                "command": "python3 -c 'import src.b as m; assert m.OTHER == 2'",
                "exit_status": 0,
                "status": "pass",
            }
        ]
        third["review"] = {
            "reviewer_actor": "astra-1",
            "decision": "accept",
            "artifact_free": True,
            "verified_hashes": {},
        }
        self.assertInvalid(self.validate(document), "only research tasks may be artifact_free")

    def test_accepted_verification_task_needs_hashed_targets(self):
        document = self.document()
        document["artifacts"] = []
        first = support.task(document, "T1")
        first["review"]["verified_hashes"] = {}
        first["evidence"][0]["artifact"] = None
        first["evidence"][0]["exit_status"] = 0
        verifier = support.task(document, "T2")
        verifier["review"]["verified_hashes"] = {}
        verifier["review"]["artifact_free"] = True
        verifier["evidence"][0]["artifact"] = None
        verifier["evidence"][0]["exit_status"] = 0
        report = self.validate(document)
        self.assertFalse(report.ok)
        self.assertTrue(
            any("must snapshot hashed targets" in error for error in report.errors),
            report.errors,
        )

    def test_research_task_may_declare_itself_artifact_free(self):
        document = self.document()
        support.add_task(
            document, id="T5", type="research", objective="Answer the open question.",
            state="accepted", requirements=["R1"],
            acceptance=[
                {"id": "A5-1", "criterion": "Sources cited.", "requirement_ids": ["R1"]}
            ],
            owner={"actor": "researcher-1", "model": "m"},
            evidence=[
                {
                    "id": "ev-5",
                    "acceptance": ["A5-1"],
                    "command": "read docs/notes.md and cite it",
                    "exit_status": 0,
                    "status": "pass",
                }
            ],
        )
        support.task(document, "T5")["review"] = {
            "reviewer_actor": "astra-1",
            "decision": "accept",
            "artifact_free": True,
            "verified_hashes": {},
        }
        report = self.validate(document)
        self.assertTrue(report.ok, report.errors)


class FinalValidation(support.LedgerCase):
    """Completion mode is stricter than planning mode."""

    def finished_document(self) -> dict:
        """A ledger where every task is accepted with its own hashed artifact."""
        document = self.document()
        hash_b = support.sha256_file(self.workspace / "src" / "b.py")
        document["artifacts"].append(
            {
                "id": "art-b",
                "task_id": "T3",
                "path": "src/b.py",
                "sha256": hash_b,
                "kind": "source",
            }
        )
        third = support.task(document, "T3")
        third["state"] = "accepted"
        third["owner"] = {"actor": "worker-c", "model": "m"}
        third["resources"] = []
        third["evidence"] = [
            {
                "id": "ev-3",
                "acceptance": ["A3-1"],
                "command": "python3 -c 'import src.b as m; assert m.OTHER == 2'",
                "exit_status": 0,
                "status": "pass",
                "artifact": "art-b",
            }
        ]
        third["review"] = {
            "reviewer_actor": "astra-1",
            "decision": "accept",
            "verified_hashes": {"art-b": hash_b},
        }
        return document

    def final(self, document: dict):
        """Validate a document in final/completion mode."""
        return workflow.validate_ledger(
            document, base_dir=self.tmp, verify_files=True, mode="final"
        )

    def test_final_mode_accepts_a_finished_ledger(self):
        report = self.final(self.finished_document())
        self.assertTrue(report.ok, report.errors)

    def test_planning_mode_warns_about_uncovered_requirement(self):
        document = self.document()
        document["requirements"].append(
            {"id": "R9", "text": "not yet mapped", "source": "objective"}
        )
        report = self.validate(document)
        self.assertTrue(report.ok, report.errors)
        self.assertTrue(any("R9" in warning for warning in report.warnings))

    def test_final_mode_requires_accepted_coverage(self):
        document = self.document()
        document["requirements"].append(
            {"id": "R9", "text": "mapped only by unfinished work", "source": "objective"}
        )
        third = support.task(document, "T3")
        third["requirements"] = ["R9"]
        third["acceptance"][0]["requirement_ids"] = ["R9"]
        report = self.final(document)
        self.assertFalse(report.ok, "final mode must reject an uncovered requirement")
        self.assertTrue(any("R9" in error for error in report.errors), report.errors)

    def test_final_mode_rejects_unfinished_tasks(self):
        document = self.document()
        support.task(document, "T3")["state"] = "blocked"
        report = self.final(document)
        self.assertFalse(report.ok)
        self.assertTrue(
            any("still open" in error for error in report.errors), report.errors
        )

    def test_unknown_mode_rejected(self):
        report = workflow.validate_ledger(
            self.document(), base_dir=self.tmp, verify_files=True, mode="sideways"
        )
        self.assertFalse(report.ok)


def two_module_ledger(case: support.LedgerCase) -> dict:
    """Build two independently authored accepted modules and one combined verifier."""
    document = case.document()
    document["requirements"].append(
        {"id": "R3", "text": "src/c.py exposes THIRD == 3.", "source": "objective"}
    )
    hash_c = support.sha256_file(case.workspace / "src" / "c.py")
    document["artifacts"].append(
        {
            "id": "art-c",
            "task_id": "T3",
            "path": "src/c.py",
            "sha256": hash_c,
            "kind": "source",
        }
    )
    third = support.task(document, "T3")
    third.update(
        {
            "state": "accepted",
            "importance": "important",
            "requirements": ["R3"],
            "depends_on": [],
            "owner": {"actor": "worker-c", "model": "deepseek/deepseek-v4.1-flash"},
            "write_paths": ["src/c.py"],
            "resources": [],
        }
    )
    third["acceptance"] = [
        {"id": "A3-1", "criterion": "c module", "requirement_ids": ["R3"]}
    ]
    third["evidence"] = [
        {
            "id": "ev-4",
            "acceptance": ["A3-1"],
            "command": "python3 -c 'import src.c as m; assert m.THIRD == 3'",
            "exit_status": 0,
            "status": "pass",
            "artifact": "art-c",
        }
    ]
    third["review"] = {
        "reviewer_actor": "astra-1",
        "decision": "accept",
        "independent": True,
        "verified_hashes": {"art-c": hash_c},
    }

    first = support.task(document, "T1")
    first["requirements"] = ["R1", "R2"]
    first["acceptance"][0]["requirement_ids"] = ["R1", "R2"]

    verifier = support.task(document, "T2")
    verifier["verifies"] = ["T1", "T3"]
    verifier["depends_on"] = []
    verifier["acceptance"][0]["requirement_ids"] = ["R1", "R3"]
    verifier["evidence"][0]["artifact"] = "art-a"
    verifier["review"]["verified_hashes"] = {
        "art-a": document["artifacts"][0]["sha256"],
        "art-c": hash_c,
    }
    return document


class VerificationTargets(support.LedgerCase):
    """One verifier may cover several frozen targets without deadlocking."""

    def two_module_document(self) -> dict:
        """Two independently authored modules, each accepted, with one combined verifier."""
        return two_module_ledger(self)

    def test_combined_verifier_covers_both_targets(self):
        report = self.validate(self.two_module_document())
        self.assertTrue(report.ok, report.errors)

    def test_list_form_reports_unknown_target(self):
        document = self.two_module_document()
        support.task(document, "T2")["verifies"] = ["T1", "T3", "T404"]
        self.assertInvalid(self.validate(document), "unknown verified task")

    def test_changed_target_invalidates_the_verifier_snapshot(self):
        document = self.two_module_document()
        support.write_file(self.workspace / "src" / "c.py", "THIRD = 99\n")
        report = self.validate(document)
        self.assertFalse(report.ok)
        self.assertTrue(any("stale" in error for error in report.errors), report.errors)
        self.assertTrue(
            any("task 'T2'" in error for error in report.errors),
            "the combined verifier's acceptance must also be invalidated",
        )

    def test_verifier_can_accept_over_a_frozen_target(self):
        """Deadlock-free sequence: targets freeze, verifier accepts."""
        document = self.two_module_document()
        for task_id in ("T1", "T3"):
            entry = support.task(document, task_id)
            entry["state"] = "ready_for_review"
            entry.pop("review", None)
            entry["evidence"] = []
        report = self.validate(document)
        self.assertTrue(report.ok, report.errors)

    def test_important_targets_accept_after_the_verifier(self):
        """The full sequence must not deadlock on accepted dependencies."""
        document = self.two_module_document()
        for task_id in ("T1", "T3"):
            entry = support.task(document, task_id)
            entry["state"] = "ready_for_review"
            entry.pop("review", None)
            entry["evidence"] = []
        frozen = self.validate(document)
        self.assertTrue(frozen.ok, frozen.errors)
        for task_id in ("T1", "T3"):
            entry = support.task(document, task_id)
            entry["state"] = "accepted"
            entry["review"] = {
                "reviewer_actor": "astra-1",
                "decision": "accept",
                "independent": True,
                "verified_hashes": {
                    "art-a": document["artifacts"][0]["sha256"],
                    "art-c": document["artifacts"][1]["sha256"],
                },
            }
            entry["evidence"] = [
                {
                    "id": f"ev-{task_id}",
                    "acceptance": [entry["acceptance"][0]["id"]],
                    "command": "python3 -m unittest tests",
                    "exit_status": 0,
                    "status": "pass",
                    "artifact": "art-a" if task_id == "T1" else "art-c",
                }
            ]
        report = workflow.validate_ledger(
            document, base_dir=self.tmp, verify_files=True, mode="final"
        )
        self.assertTrue(report.ok, report.errors)


class BackpressureDeadlock(support.LedgerCase):
    """Backpressure must not stall the verification needed to clear it."""

    def backlogged(self) -> dict:
        """Two important implementations waiting on review plus their combined verifier."""
        document = two_module_ledger(self)
        for task_id in ("T1", "T3"):
            entry = support.task(document, task_id)
            entry["state"] = "ready_for_review"
            entry.pop("review", None)
            entry["evidence"] = []
        verifier = support.task(document, "T2")
        verifier["state"] = "queued"
        verifier.pop("review", None)
        verifier.pop("owner", None)
        verifier["evidence"] = []
        document["limits"]["review_backlog_limit"] = 2
        support.add_task(
            document, id="T4", write_paths=["src/d.py"],
            acceptance=[{"id": "A4-1", "criterion": "d", "requirement_ids": ["R2"]}],
        )
        return document

    def test_verifier_dispatches_while_implementation_is_held(self):
        document = self.backlogged()
        result = self.schedule(document, host_capacity=4)
        self.assertTrue(result["ok"], result.get("errors"))
        self.assertTrue(result["backpressure"])
        self.assertEqual(2, result["review_backlog"])
        kinds = {item["task_id"]: item["kind"] for item in result["suggestions"]}
        self.assertEqual(
            "verification",
            kinds.get("T2"),
            f"the verifier that clears the backlog must stay dispatchable: {result['suggestions']}",
        )
        self.assertNotIn("T4", kinds, "new implementation must be held")
        self.assertTrue(
            any(
                item["task_id"] == "T4" and "review backlog" in item["reason"]
                for item in result["deferred"]
            ),
            result["deferred"],
        )

    def test_held_implementation_is_ready_again_once_the_backlog_clears(self):
        document = self.backlogged()
        held = self.schedule(document, host_capacity=4)
        self.assertNotIn(
            "T4", {item["task_id"] for item in held["suggestions"]}
        )
        # The reviewer accepts both frozen modules; backlog drops below the limit.
        verifier = support.task(document, "T2")
        verifier["state"] = "accepted"
        verifier["owner"] = {"actor": "verifier-ds-3", "model": "m"}
        verifier["evidence"] = [
            {
                "id": "ev-3",
                "acceptance": ["A2-1"],
                "command": "python3 tools/check_combined.py",
                "exit_status": 0,
                "status": "pass",
                "artifact": "art-a",
            }
        ]
        verifier["review"] = {
            "reviewer_actor": "astra-1",
            "decision": "accept",
            "verified_hashes": {
                "art-a": document["artifacts"][0]["sha256"],
                "art-c": document["artifacts"][1]["sha256"],
            },
        }
        for task_id in ("T1", "T3"):
            entry = support.task(document, task_id)
            entry["state"] = "accepted"
            entry["review"] = {
                "reviewer_actor": "astra-1",
                "decision": "accept",
                "verified_hashes": {
                    "art-a": document["artifacts"][0]["sha256"],
                    "art-c": document["artifacts"][1]["sha256"],
                },
            }
            entry["evidence"] = [
                {
                    "id": f"ev-{task_id}",
                    "acceptance": [entry["acceptance"][0]["id"]],
                    "command": "python3 -m unittest tests",
                    "exit_status": 0,
                    "status": "pass",
                    "artifact": "art-a" if task_id == "T1" else "art-c",
                }
            ]
        result = self.schedule(document, host_capacity=4)
        self.assertTrue(result["ok"], result.get("errors"))
        self.assertFalse(result["backpressure"])
        self.assertIn(
            "T4", {item["task_id"] for item in result["suggestions"]}
        )


class MalformedInput(support.LedgerCase):
    """Shapes and identifiers that must never be accepted silently."""

    def test_missing_top_level_field_rejected(self):
        document = self.document()
        del document["baseline"]
        self.assertInvalid(self.validate(document), "missing required field 'baseline'")

    def test_wrong_schema_version_rejected(self):
        document = self.document()
        document["schema_version"] = 99
        self.assertInvalid(self.validate(document), "schema_version")

    def test_tasks_must_be_a_list(self):
        document = self.document()
        document["tasks"] = {"T1": {}}
        self.assertInvalid(self.validate(document), "must be a list")

    def test_task_field_types_are_checked(self):
        document = self.document()
        support.task(document, "T3")["state"] = 5
        self.assertInvalid(self.validate(document), "must be a string")

    def test_unknown_task_type_rejected(self):
        document = self.document()
        support.task(document, "T3")["type"] = "refactor"
        self.assertInvalid(self.validate(document), "type")

    def test_unknown_field_warns_without_failing(self):
        document = self.document()
        support.task(document, "T3")["mystery"] = "value"
        report = self.validate(document)
        self.assertTrue(report.ok, report.errors)
        self.assertTrue(any("mystery" in warning for warning in report.warnings))

    def test_duplicate_task_ids_rejected(self):
        document = self.document()
        support.add_task(document, id="T1", state="queued")
        self.assertInvalid(self.validate(document), "duplicate task id")

    def test_duplicate_requirement_ids_rejected(self):
        document = self.document()
        document["requirements"].append(
            {"id": "R1", "text": "duplicate", "source": "objective"}
        )
        self.assertInvalid(self.validate(document), "duplicate requirement id")

    def test_duplicate_acceptance_ids_rejected(self):
        document = self.document()
        updated = support.clone(document)
        support.task(updated, "T2")["acceptance"][0]["id"] = "A1-1"
        self.assertInvalid(self.validate(updated), "already used by task")

    def test_unknown_requirement_reference_rejected(self):
        document = self.document()
        support.task(document, "T3")["requirements"] = ["R404"]
        self.assertInvalid(self.validate(document), "unknown requirement reference")

    def test_unknown_dependency_rejected(self):
        document = self.document()
        support.task(document, "T3")["depends_on"] = ["T404"]
        self.assertInvalid(self.validate(document), "unknown dependency")

    def test_unknown_artifact_reference_rejected(self):
        document = self.document()
        support.task(document, "T3")["evidence"] = [
            {
                "id": "ev-x",
                "acceptance": ["A3-1"],
                "status": "pass",
                "exit_status": 0,
                "artifact": "art-missing",
            }
        ]
        self.assertInvalid(self.validate(document), "unknown artifact reference")

    def test_dependency_cycle_rejected(self):
        document = self.document()
        support.task(document, "T3")["depends_on"] = ["T1"]
        support.task(document, "T1")["depends_on"] = ["T2"]
        support.task(document, "T2")["depends_on"] = ["T3"]
        self.assertInvalid(self.validate(document), "dependency cycle")

    def test_self_dependency_rejected(self):
        document = self.document()
        support.task(document, "T3")["depends_on"] = ["T3"]
        self.assertInvalid(self.validate(document), "cannot depend on itself")


class HandoffCompleteness(support.LedgerCase):
    """Fields the skill promises every dispatched task will carry."""

    def test_running_task_without_owner_rejected(self):
        document = self.document()
        support.task(document, "T3")["state"] = "running"
        self.assertInvalid(self.validate(document), "owner")

    def test_dispatch_without_baseline_revision_rejected(self):
        document = self.document()
        document["baseline"]["revision"] = ""
        support.task(document, "T3")["state"] = "running"
        support.task(document, "T3")["owner"] = {"actor": "worker-c", "model": "m"}
        self.assertInvalid(self.validate(document), "baseline revision")

    def test_verification_task_without_target_rejected(self):
        document = self.document()
        support.task(document, "T2").pop("verifies")
        self.assertInvalid(self.validate(document), "must name the task, or tasks, it verifies")

    def test_task_without_explicit_model_warns(self):
        document = self.document()
        support.task(document, "T3")["state"] = "running"
        support.task(document, "T3")["owner"] = {"actor": "worker-c"}
        report = self.validate(document)
        self.assertTrue(report.ok, report.errors)
        self.assertTrue(any("explicit model" in warning for warning in report.warnings))

    def test_acceptance_must_map_to_a_requirement(self):
        document = self.document()
        support.task(document, "T3")["acceptance"][0]["requirement_ids"] = []
        self.assertInvalid(self.validate(document), "must map to at least one user requirement")

    def test_uncovered_requirement_warns(self):
        document = self.document()
        document["requirements"].append(
            {"id": "R9", "text": "unmapped ask", "source": "objective"}
        )
        report = self.validate(document)
        self.assertTrue(report.ok, report.errors)
        self.assertTrue(any("R9" in warning for warning in report.warnings))


class PathSafety(support.LedgerCase):
    """Workspace escapes, including escapes through symlinks."""

    def test_parent_traversal_write_path_rejected(self):
        document = self.document()
        support.task(document, "T3")["write_paths"] = ["../outside.py"]
        self.assertInvalid(self.validate(document), "unsafe write path")

    def test_absolute_write_path_outside_workspace_rejected(self):
        document = self.document()
        outside = self.tmp / "elsewhere" / "x.py"
        support.task(document, "T3")["write_paths"] = [str(outside)]
        self.assertInvalid(self.validate(document), "unsafe write path")

    def test_symlink_escape_rejected(self):
        document = self.document()
        outside = self.tmp / "outside"
        outside.mkdir()
        link = self.workspace / "link"
        try:
            os.symlink(outside, link)
        except (OSError, NotImplementedError):  # pragma: no cover - platform guard
            self.skipTest("symlinks unavailable on this filesystem")
        support.task(document, "T3")["write_paths"] = ["link/escape.py"]
        self.assertInvalid(self.validate(document), "unsafe write path")

    def test_symlink_inside_workspace_is_allowed(self):
        document = self.document()
        link = self.workspace / "alias"
        try:
            os.symlink(self.workspace / "src", link)
        except (OSError, NotImplementedError):  # pragma: no cover - platform guard
            self.skipTest("symlinks unavailable on this filesystem")
        support.task(document, "T3")["write_paths"] = ["alias/b.py"]
        report = self.validate(document)
        self.assertTrue(report.ok, report.errors)

    def test_missing_workspace_rejected(self):
        document = self.document()
        document["baseline"]["workspace"] = str(self.tmp / "nope")
        self.assertInvalid(self.validate(document), "workspace directory does not exist")


class OwnershipConflicts(support.LedgerCase):
    """Simultaneous writers and shared exclusive resources."""

    def test_scenario_conflicting_writes_is_rejected(self):
        document = self.document()
        support.drop_task(document, "T2")
        support.dispatch(document, "T1", "worker-a")
        support.add_task(
            document, id="T4", state="running", write_paths=["src/a.py"],
            owner={"actor": "worker-d", "model": "m"},
        )
        self.assertInvalid(self.validate(document), "ownership conflict")

    def test_distinct_write_paths_are_allowed_concurrently(self):
        document = self.document()
        support.drop_task(document, "T2")
        support.task(document, "T1")["state"] = "running"
        support.task(document, "T3")["state"] = "running"
        support.task(document, "T3")["owner"] = {"actor": "worker-c", "model": "m"}
        support.task(document, "T3")["resources"] = []
        report = self.validate(document)
        self.assertTrue(report.ok, report.errors)

    def test_exclusive_resource_conflict_rejected(self):
        document = self.document()
        support.drop_task(document, "T2")
        support.task(document, "T1")["state"] = "running"
        support.task(document, "T3")["state"] = "running"
        support.task(document, "T3")["owner"] = {"actor": "worker-c", "model": "m"}
        self.assertInvalid(self.validate(document), "exclusive resource conflict")

    def test_shared_resource_allows_two_holders(self):
        document = self.document()
        support.drop_task(document, "T2")
        document["resources"][0]["kind"] = "shared"
        support.task(document, "T1")["state"] = "running"
        support.task(document, "T3")["state"] = "running"
        support.task(document, "T3")["owner"] = {"actor": "worker-c", "model": "m"}
        report = self.validate(document)
        self.assertTrue(report.ok, report.errors)

    def test_ready_for_review_still_holds_its_paths(self):
        document = self.document()
        support.drop_task(document, "T2")
        support.task(document, "T1")["state"] = "ready_for_review"
        support.add_task(
            document, id="T4", state="running", write_paths=["src/a.py"],
            owner={"actor": "worker-d", "model": "m"},
        )
        self.assertInvalid(self.validate(document), "ownership conflict")


class EvidenceAndReview(support.LedgerCase):
    """Acceptance must rest on real, current, independently reviewed evidence."""

    def test_self_approval_rejected(self):
        document = self.document()
        support.task(document, "T1")["review"]["reviewer_actor"] = "worker-a"
        self.assertInvalid(self.validate(document), "self-approval rejected")

    def test_scenario_contradictory_success_claim_is_rejected(self):
        document = self.document()
        support.task(document, "T1")["evidence"][0]["status"] = "fail"
        support.task(document, "T1")["evidence"][0]["exit_status"] = 1
        self.assertInvalid(self.validate(document), "no passing evidence")

    def test_pass_status_with_failing_exit_code_is_rejected(self):
        document = self.document()
        support.task(document, "T1")["evidence"][0]["exit_status"] = 1
        self.assertInvalid(self.validate(document), "no passing evidence")

    def test_accepted_task_without_evidence_rejected(self):
        document = self.document()
        support.task(document, "T1")["evidence"] = []
        self.assertInvalid(self.validate(document), "missing required evidence")

    def test_accepted_task_without_review_rejected(self):
        document = self.document()
        support.task(document, "T1").pop("review")
        self.assertInvalid(self.validate(document), "must carry a review decision")

    def test_accepting_review_requires_hash_snapshot(self):
        document = self.document()
        support.task(document, "T1")["review"].pop("verified_hashes")
        self.assertInvalid(self.validate(document), "snapshot the artifact hashes")

    def test_accepting_review_must_cover_dependency_closure(self):
        document = self.document()
        support.task(document, "T2")["review"]["verified_hashes"] = {}
        self.assertInvalid(self.validate(document), "omitted artifacts from its dependency closure")

    def test_generic_acceptance_criterion_can_be_optional(self):
        document = self.document()
        support.task(document, "T1")["acceptance"].append(
            {
                "id": "A1-99",
                "criterion": "Optional polish with no evidence yet.",
                "requirement_ids": ["R1"],
                "required": False,
            }
        )
        report = self.validate(document)
        self.assertTrue(report.ok, report.errors)

    def test_evidence_referencing_unknown_acceptance_rejected(self):
        document = self.document()
        support.task(document, "T1")["evidence"][0]["acceptance"] = ["A9-9"]
        self.assertInvalid(self.validate(document), "unknown acceptance reference")

    def test_duplicate_evidence_id_rejected(self):
        document = self.document()
        support.task(document, "T1")["evidence"][0]["id"] = "ev-2"
        self.assertInvalid(self.validate(document), "duplicate evidence id")


class ArtifactFreshness(support.LedgerCase):
    """Recorded hashes must match the bytes on disk."""

    def test_scenario_revised_artifact_invalidates_acceptance(self):
        document = self.document()
        support.write_file(self.workspace / "src" / "a.py", "VALUE = 99\n")
        self.assertInvalid(self.validate(document), "stale acceptance")

    def test_revised_artifact_also_breaks_its_own_recorded_hash(self):
        document = self.document()
        support.write_file(self.workspace / "src" / "a.py", "VALUE = 99\n")
        self.assertInvalid(self.validate(document), "stale artifact hash")

    def test_review_hash_matching_a_stale_artifact_is_still_rejected(self):
        document = self.document()
        support.write_file(self.workspace / "src" / "a.py", "VALUE = 5\n")
        new_digest = support.sha256_file(self.workspace / "src" / "a.py")
        # The reviewer "re-verified" only by copying the producer's new digest.
        document["artifacts"][0]["sha256"] = new_digest
        support.task(document, "T1")["review"]["verified_hashes"]["art-a"] = new_digest
        support.task(document, "T2")["review"]["verified_hashes"]["art-a"] = new_digest
        report = self.validate(document)
        self.assertTrue(report.ok, report.errors)

    def test_stale_downstream_review_hash_rejected(self):
        document = self.document()
        support.write_file(self.workspace / "src" / "a.py", "VALUE = 7\n")
        self.assertInvalid(self.validate(document), "stale acceptance")

    def test_hash_for_unrelated_artifact_reported_separately(self):
        document = self.document()
        support.add_task(
            document, id="T4", write_paths=["src/b.py"],
            acceptance=[{"id": "A4-1", "criterion": "b", "requirement_ids": ["R2"]}],
        )
        document["artifacts"].append(
            {
                "id": "art-b",
                "task_id": "T4",
                "path": "src/b.py",
                "sha256": support.sha256_file(self.workspace / "src" / "b.py"),
            }
        )
        support.task(document, "T1")["review"]["verified_hashes"]["art-b"] = "0" * 64
        report = self.validate(document)
        self.assertInvalid(report, "does not match the file on disk")
        self.assertTrue(
            any("outside this task's dependency closure" in warning for warning in report.warnings)
        )

    def test_missing_artifact_file_rejected(self):
        document = self.document()
        (self.workspace / "src" / "a.py").unlink()
        self.assertInvalid(self.validate(document), "artifact file is missing")

    def test_bad_digest_shape_rejected(self):
        document = self.document()
        document["artifacts"][0]["sha256"] = "not-a-hash"
        self.assertInvalid(self.validate(document), "64-character lowercase hex")

    def test_hash_checks_skipped_when_files_not_verified(self):
        document = self.document()
        support.write_file(self.workspace / "src" / "a.py", "VALUE = 42\n")
        report = self.validate(document, verify_files=False)
        self.assertTrue(report.ok, report.errors)


class IndependentVerification(support.LedgerCase):
    """Important work needs a verifier that is not its author."""

    def test_author_verifying_own_important_task_rejected(self):
        document = self.document()
        support.task(document, "T2")["owner"] = {
            "actor": "worker-a",
            "model": "deepseek/deepseek-v4.1-flash",
        }
        self.assertInvalid(self.validate(document), "must use a different actor")

    def test_important_task_accepted_without_verification_rejected(self):
        document = self.document()
        document["tasks"] = [
            entry for entry in document["tasks"] if entry["id"] != "T2"
        ]
        self.assertInvalid(self.validate(document), "independent verification")

    def test_standard_task_does_not_require_verification(self):
        document = self.document()
        support.task(document, "T1")["importance"] = "standard"
        document["tasks"] = [
            entry for entry in document["tasks"] if entry["id"] != "T2"
        ]
        report = self.validate(document)
        self.assertTrue(report.ok, report.errors)

    def test_verification_task_pointing_at_itself_rejected(self):
        document = self.document()
        support.task(document, "T2")["verifies"] = "T2"
        self.assertInvalid(self.validate(document), "cannot verify itself")


class Scheduler(support.LedgerCase):
    """Read-only, capacity-aware suggestions."""

    def ready_document(self) -> dict:
        """A consistent ledger: one task awaiting review and two ready jobs.

        No task is accepted and no artifact is tracked, so the fixture stays
        valid under every state and evidence rule while giving the scheduler one
        pending review, one dependency-priority candidate, and one free job.
        """
        document = self.document()
        document["artifacts"] = []
        document["tasks"] = [
            {
                "id": "T1",
                "type": "implementation",
                "objective": "Module A.",
                "state": "queued",
                "requirements": ["R1"],
                "depends_on": [],
                "write_paths": ["src/a.py"],
                "read_paths": [],
                "resources": [],
                "acceptance": [
                    {"id": "A1-1", "criterion": "a", "requirement_ids": ["R1"]}
                ],
                "evidence": [],
            },
            {
                "id": "T2",
                "type": "implementation",
                "objective": "Module B, awaiting review.",
                "state": "ready_for_review",
                "requirements": ["R2"],
                "depends_on": [],
                "owner": {"actor": "worker-b", "model": "m"},
                "write_paths": ["src/b.py"],
                "read_paths": [],
                "resources": [],
                "acceptance": [
                    {"id": "A2-1", "criterion": "b", "requirement_ids": ["R2"]}
                ],
                "evidence": [],
            },
            {
                "id": "T3",
                "type": "implementation",
                "objective": "Module C, depends on A.",
                "state": "queued",
                "requirements": ["R2"],
                "depends_on": ["T1"],
                "write_paths": ["src/c.py"],
                "read_paths": [],
                "resources": [],
                "acceptance": [
                    {"id": "A3-1", "criterion": "c", "requirement_ids": ["R2"]}
                ],
                "evidence": [],
            },
        ]
        return document

    def test_invalid_ledger_produces_no_suggestions(self):
        document = self.document()
        support.task(document, "T3")["state"] = "bogus"
        result = self.schedule(document)
        self.assertFalse(result["ok"])
        self.assertNotIn("suggestions", result)
        self.assertTrue(result["errors"])

    def test_review_pending_reserves_a_reviewer_slot(self):
        result = self.schedule(self.ready_document(), host_capacity=2)
        self.assertTrue(result["ok"], result.get("errors"))
        self.assertEqual(1, result["capacity"]["reviewer_slot_reserved"])
        kinds = [item["kind"] for item in result["suggestions"]]
        self.assertIn("review", kinds)

    def test_reviewer_stays_runnable_at_minimum_capacity(self):
        result = self.schedule(self.ready_document(), host_capacity=1)
        self.assertTrue(result["ok"], result.get("errors"))
        self.assertEqual(0, result["capacity"]["free_slots"])
        kinds = [item["kind"] for item in result["suggestions"]]
        self.assertEqual(["review"], kinds)

    def test_other_active_jobs_count_against_capacity(self):
        result = self.schedule(self.ready_document(), host_capacity=3, other_active=2)
        self.assertTrue(result["ok"], result.get("errors"))
        self.assertEqual(0, result["capacity"]["free_slots"])

    def test_user_limit_caps_host_capacity(self):
        document = self.ready_document()
        document["limits"]["max_concurrent_workers"] = 1
        result = self.schedule(document, host_capacity=8)
        self.assertEqual(1, result["capacity"]["limit_max_concurrent_workers"])
        self.assertEqual(1, result["capacity"]["worker_slots_free"])

    def test_review_backlog_applies_backpressure(self):
        document = self.ready_document()
        first = support.task(document, "T1")
        first["state"] = "ready_for_review"
        first["owner"] = {"actor": "worker-a", "model": "m"}
        # T3 becomes an independent ready job so backpressure has something to hold.
        support.task(document, "T3")["depends_on"] = []
        document["limits"]["review_backlog_limit"] = 2
        result = self.schedule(document, host_capacity=6)
        self.assertTrue(result["backpressure"])
        self.assertEqual(2, result["review_backlog"])
        self.assertFalse(
            any(item["kind"] != "review" for item in result["suggestions"]),
            result["suggestions"],
        )
        self.assertTrue(
            any("review backlog" in item["reason"] for item in result["deferred"]),
            result["deferred"],
        )

    def test_priority_prefers_the_task_that_unblocks_more(self):
        document = self.ready_document()
        support.add_task(
            document, id="T4", depends_on=["T1"], write_paths=["src/d.py"],
            acceptance=[{"id": "A4-1", "criterion": "c", "requirement_ids": ["R2"]}],
        )
        result = self.schedule(document, host_capacity=2)
        self.assertTrue(result["ok"], result.get("errors"))
        dispatchable = [
            item for item in result["suggestions"] if item["kind"] != "review"
        ]
        self.assertEqual(["T1"], [item["task_id"] for item in dispatchable],
                         result["suggestions"])

    def test_dependency_wait_is_reported_not_guessed(self):
        document = self.ready_document()
        result = self.schedule(document, host_capacity=4)
        self.assertTrue(result["ok"], result.get("errors"))
        suggested = {item["task_id"] for item in result["suggestions"]}
        self.assertNotIn("T3", suggested, "T3 must wait for T1 acceptance")
        self.assertTrue(
            any("waiting for dependency acceptance" in item["reason"]
                for item in result["deferred"])
        )

    def test_failed_dependency_blocks_instead_of_deadlocking(self):
        document = self.ready_document()
        first = support.task(document, "T1")
        first["state"] = "failed"
        first["owner"] = {"actor": "worker-a", "model": "m"}
        result = self.schedule(document, host_capacity=4)
        self.assertTrue(result["ok"], result.get("errors"))
        blocked = {item["task_id"]: item["reason"] for item in result["blocked_tasks"]}
        self.assertIn("T3", blocked)
        self.assertIn("T1", blocked["T3"])
        self.assertFalse(
            any(item["task_id"] == "T3" for item in result["suggestions"])
        )

    def test_scenario_missing_info_is_surfaced_not_guessed(self):
        document = self.ready_document()
        first = support.task(document, "T1")
        first["state"] = "needs_research"
        first["owner"] = {"actor": "worker-a", "model": "m"}
        result = self.schedule(document, host_capacity=4)
        self.assertTrue(result["ok"], result.get("errors"))
        item = next(
            entry for entry in result["suggestions"] if entry["task_id"] == "T1"
        )
        self.assertEqual("review", item["kind"])
        self.assertIn("needs_research", item["reason"])

    def test_rework_resumes_the_same_task(self):
        document = self.ready_document()
        first = support.task(document, "T1")
        first["state"] = "needs_changes"
        first["owner"] = {"actor": "worker-a", "model": "m"}
        first["review"] = {
            "reviewer_actor": "astra-1",
            "decision": "request_changes",
            "notes": "tighten the exit code path",
        }
        result = self.schedule(document, host_capacity=4)
        self.assertTrue(result["ok"], result.get("errors"))
        item = next(
            entry for entry in result["suggestions"] if entry["task_id"] == "T1"
        )
        self.assertEqual("rework", item["kind"])

    def test_scheduler_skips_a_task_blocked_by_live_ownership(self):
        document = self.ready_document()
        # T3 is mid-flight and holds src/c.py; T4 wants the same file.
        support.task(document, "T3")["depends_on"] = []
        support.dispatch(document, "T3", "worker-c")
        support.add_task(
            document, id="T4", write_paths=["src/c.py"],
            acceptance=[{"id": "A4-1", "criterion": "also c", "requirement_ids": ["R2"]}],
        )
        result = self.schedule(document, host_capacity=4)
        self.assertTrue(result["ok"], result.get("errors"))
        self.assertFalse(
            any(item["task_id"] == "T4" for item in result["suggestions"]),
            result["suggestions"],
        )
        self.assertTrue(
            any("in-flight task" in item["reason"] for item in result["deferred"]),
            result["deferred"],
        )

    def test_budget_stays_unknown_when_not_supplied(self):
        document = self.document()
        document["limits"]["budget_usd"] = None
        result = self.schedule(document, host_capacity=4)
        self.assertIsNone(result["budget"]["usd"])
        self.assertEqual("unknown", result["budget"]["status"])
        self.assertIn("No cost is estimated", result["budget"]["note"])

    def test_reason_and_provenance_are_always_reported(self):
        result = self.schedule(self.ready_document(), host_capacity=4)
        for item in result["suggestions"]:
            self.assertTrue(item["reason"], item)
        self.assertIn("declarations", result["provenance_note"])

    def test_capacity_never_exceeds_the_host(self):
        document = self.ready_document()
        document["limits"]["max_concurrent_workers"] = 99
        result = self.schedule(document, host_capacity=2)
        self.assertLessEqual(result["capacity"]["worker_slots_free"], 2)
        self.assertEqual(1, result["capacity"]["worker_slots_free"])

    def test_host_capacity_subtracts_reviewer_and_other_active(self):
        result = self.schedule(self.ready_document(), host_capacity=4, other_active=1)
        capacity = result["capacity"]
        self.assertEqual(1, capacity["reviewer_slot_reserved"])
        self.assertEqual(1, capacity["other_active_supplied"])
        self.assertEqual(2, capacity["host_slots_free_after_reservations"])
        self.assertEqual(2, capacity["worker_slots_free"])
        dispatchable = [
            item for item in result["suggestions"] if item["kind"] != "review"
        ]
        self.assertLessEqual(len(dispatchable), 2, result["suggestions"])

    def test_max_concurrent_workers_caps_workers_not_the_host_budget(self):
        document = self.ready_document()
        document["limits"]["max_concurrent_workers"] = 2
        result = self.schedule(document, host_capacity=8)
        capacity = result["capacity"]
        self.assertEqual(8, capacity["host_capacity"])
        self.assertEqual(7, capacity["host_slots_free_after_reservations"])
        self.assertEqual(2, capacity["worker_slots_free"])
        self.assertEqual(0, capacity["workers_running"])

    def test_total_worker_limit_blocks_new_worker_starts(self):
        document = self.ready_document()
        document["limits"]["max_total_workers"] = 1  # worker-b already dispatched
        result = self.schedule(document, host_capacity=6)
        self.assertTrue(result["ok"], result.get("errors"))
        self.assertEqual(1, result["capacity"]["dispatched_workers"])
        self.assertEqual(0, result["capacity"]["additional_workers_allowed"])
        suggested = {item["task_id"] for item in result["suggestions"]}
        self.assertEqual({"T2"}, suggested, result["suggestions"])
        self.assertTrue(
            any("max_total_workers" in item["reason"] for item in result["deferred"]),
            result["deferred"],
        )

    def test_rework_reuses_a_dispatched_worker_within_the_total_limit(self):
        document = self.ready_document()
        document["limits"]["max_total_workers"] = 1
        second = support.task(document, "T2")
        second["state"] = "needs_changes"
        second["review"] = {"reviewer_actor": "astra-1", "decision": "request_changes"}
        result = self.schedule(document, host_capacity=6)
        self.assertTrue(result["ok"], result.get("errors"))
        kinds = {item["task_id"]: item["kind"] for item in result["suggestions"]}
        self.assertEqual("rework", kinds.get("T2"), result["suggestions"])


class LimitEnforcement(support.LedgerCase):
    """Supplied user limits are enforced, not advisory."""

    def test_concurrency_limit_violation_rejected(self):
        document = self.document()
        document["limits"]["max_concurrent_workers"] = 1
        support.task(document, "T1")["state"] = "running"
        support.add_task(
            document, id="T4", state="running", write_paths=["src/c.py"],
            owner={"actor": "worker-d", "model": "m"},
        )
        self.assertInvalid(self.validate(document), "exceed limits.max_concurrent_workers")

    def test_total_worker_limit_violation_rejected(self):
        document = self.document()
        document["limits"]["max_total_workers"] = 1
        self.assertInvalid(self.validate(document), "exceed limits.max_total_workers")

    def test_negative_limit_rejected(self):
        document = self.document()
        document["limits"]["review_backlog_limit"] = -1
        self.assertInvalid(self.validate(document), "review_backlog_limit")

    def test_unknown_limit_field_warns(self):
        document = self.document()
        document["limits"]["max_agents"] = 4
        report = self.validate(document)
        self.assertTrue(report.ok, report.errors)
        self.assertTrue(any("max_agents" in warning for warning in report.warnings))


class Observations(support.LedgerCase):
    """Observed metrics are optional and never invented."""

    def test_missing_metrics_stay_null(self):
        document = self.document()
        result = self.schedule(document, host_capacity=4)
        self.assertIsNone(document["observations"]["total_elapsed_s"])
        self.assertIsNone(document["observations"]["provider_reported_tokens"])
        self.assertNotIn("savings", json.dumps(result).lower())

    def test_non_json_observation_rejected(self):
        document = self.document()
        document["observations"] = {"weird": object()}
        self.assertInvalid(self.validate(document), "must be a JSON value")


if __name__ == "__main__":
    unittest.main()
