#!/usr/bin/env python3
"""Behavioural tests for The Council decision checker.

Every test builds a real packet bound to a real frozen package on disk, decides
it, and asserts on the machine-readable report: outcome, problem codes, and CLI
exit status. Nothing here mirrors instruction wording; the suite exists to catch
a checker that would accept a packet a reviewer would reject, including one that
would report COMPLETE without verifying the frozen bytes.
"""

from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))

import council  # noqa: E402
import support  # noqa: E402


class CouncilCase(unittest.TestCase):
    """Shared fixture: one temp package directory and one finalised packet."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.package_dir = Path(self._tmp.name) / "package"
        self.artifacts = support.materialize_package(self.package_dir)
        self.packet = support.base_packet(self.artifacts)
        self.fp = self.packet["package"]["fingerprint"]

    def build(self, **overrides) -> dict:
        """Return a finalised packet with top-level overrides."""
        return support.packet(self.artifacts, **overrides)

    def decide(self, document: dict) -> dict:
        """Decide with the frozen package directory, as acceptance must."""
        return council.council_decide(document, package_dir=self.package_dir)

    def codes(self, report: dict) -> set:
        return support.problem_codes(report)


class ShapeContract(CouncilCase):
    """Unusable input is a distinct, loud failure, not a soft verdict."""

    def test_non_object_packet_raises(self):
        for value in ([], "packet", 7, None):
            with self.subTest(value=value):
                with self.assertRaises(council.CouncilInputError):
                    council.council_decide(value)

    def test_unsupported_schema_version_raises(self):
        with self.assertRaises(council.CouncilInputError):
            council.council_decide(self.build(schema_version=99))

    def test_missing_section_raises(self):
        for section in (
            "package",
            "requirements",
            "required_checks",
            "integration",
            "panel",
            "rounds",
            "initial_reports",
            "findings",
            "reviewer_finals",
        ):
            with self.subTest(section=section):
                document = support.base_packet(self.artifacts)
                del document[section]
                with self.assertRaises(council.CouncilInputError):
                    council.council_decide(document)

    def test_wrong_section_type_raises(self):
        with self.assertRaises(council.CouncilInputError):
            council.council_decide(self.build(panel={"actor": "rev-lead"}))

    def test_cli_exit_two_on_malformed_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cases = {
                "not-json.json": "{ this is not json",
                "no-sections.json": json.dumps({"schema_version": 1}),
                "bad-version.json": json.dumps(self.build(schema_version=2)),
            }
            for name, text in cases.items():
                with self.subTest(name=name):
                    path = root / name
                    path.write_text(text, encoding="utf-8")
                    completed = support.run_cli(str(path))
                    self.assertEqual(completed.returncode, 2, completed)
                    self.assertIn("error:", completed.stderr)
            missing = support.run_cli(str(root / "absent.json"))
            self.assertEqual(missing.returncode, 2, missing)

    def test_cli_exit_codes_for_valid_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            good = support.write_packet(root / "good.json", self.packet)
            completed = support.run_cli(
                str(good), "--package-dir", str(self.package_dir)
            )
            self.assertEqual(completed.returncode, 0, completed)
            bad = support.write_packet(
                root / "bad.json", self.build(panel=self.packet["panel"][:2])
            )
            failed = support.run_cli(
                str(bad), "--package-dir", str(self.package_dir)
            )
            self.assertEqual(failed.returncode, 1, failed)

    def test_cli_json_report_is_parseable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = support.write_packet(Path(tmp) / "packet.json", self.packet)
            completed = support.run_cli(
                str(path), "--json", "--package-dir", str(self.package_dir)
            )
            self.assertEqual(completed.returncode, 0, completed)
            self.assertEqual(json.loads(completed.stdout)["outcome"], "COMPLETE")


class ArtifactVerification(CouncilCase):
    """A COMPLETE outcome needs the frozen bytes verified, never assumed."""

    def test_without_package_dir_never_completes(self):
        report = council.council_decide(self.packet)
        self.assertEqual(report["outcome"], "EVIDENCE_NEEDED")
        self.assertIn("artifacts_unverified", self.codes(report))
        self.assertIsNone(report["package_verified"])

    def test_cli_without_package_dir_exits_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = support.write_packet(Path(tmp) / "packet.json", self.packet)
            completed = support.run_cli(str(path))
            self.assertEqual(completed.returncode, 1, completed)
            self.assertIn("EVIDENCE_NEEDED", completed.stdout)

    def test_verified_package_completes(self):
        report = self.decide(self.packet)
        self.assertEqual(report["outcome"], "COMPLETE", report["problems"])
        self.assertTrue(report["package_verified"])

    def test_tampered_artifact_fails_closed(self):
        target = self.package_dir / "SKILL.md"
        target.write_text("---\nname: tampered\n---\n", encoding="utf-8")
        report = self.decide(self.packet)
        self.assertEqual(report["outcome"], "BLOCKED")
        self.assertIn("artifact_hash_mismatch", self.codes(report))
        self.assertFalse(report["package_verified"])

    def test_missing_artifact_fails_closed(self):
        (self.package_dir / "task.json").unlink()
        report = self.decide(self.packet)
        self.assertIn("artifact_missing", self.codes(report))
        self.assertEqual(report["outcome"], "BLOCKED")

    def test_directory_in_place_of_a_file_fails_closed(self):
        (self.package_dir / "SKILL.md").unlink()
        (self.package_dir / "SKILL.md").mkdir()
        report = self.decide(self.packet)
        self.assertIn("artifact_missing", self.codes(report))

    def test_unreadable_artifact_fails_closed(self):
        target = self.package_dir / "SKILL.md"
        target.chmod(0o000)
        self.addCleanup(target.chmod, 0o644)
        report = self.decide(self.packet)
        if report["package_verified"] is False:
            self.assertEqual(report["outcome"], "BLOCKED")
            self.assertIn("artifact_unreadable", self.codes(report))
        else:
            # Running with privileges that ignore the mode bits; nothing to prove.
            self.skipTest("permissions are not enforced for this user")

    def test_declared_artifacts_empty_with_a_package_dir_is_unverified(self):
        document = self.build()
        document["package"]["artifacts"] = []
        support.finalize(document)
        report = self.decide(document)
        self.assertIn("artifacts_unverified", self.codes(report))
        self.assertNotEqual(report["outcome"], "COMPLETE")


class ArtifactPaths(CouncilCase):
    """Artifact paths stay inside the package, whatever the packet claims."""

    def test_absolute_path_rejected(self):
        document = self.build()
        document["package"]["artifacts"][0]["path"] = "/etc/hosts"
        support.finalize(document)
        report = self.decide(document)
        self.assertIn("artifact_path_unsafe", self.codes(report))
        self.assertEqual(report["outcome"], "BLOCKED")

    def test_parent_traversal_rejected(self):
        document = self.build()
        document["package"]["artifacts"][0]["path"] = "../outside.txt"
        support.finalize(document)
        report = self.decide(document)
        self.assertIn("artifact_path_unsafe", self.codes(report))

    def test_drive_letter_and_empty_segments_rejected(self):
        for bad_path in ("C:secret.txt", "src//a.py", "src/./a.py", ""):
            with self.subTest(path=bad_path):
                document = self.build()
                document["package"]["artifacts"][0]["path"] = bad_path
                support.finalize(document)
                codes = support.problem_codes(self.decide(document))
                self.assertTrue(
                    codes & {"artifact_path_unsafe", "artifact_shape"}, codes
                )

    def test_symlink_escaping_the_package_rejected(self):
        outside = self.package_dir.parent / "outside.txt"
        outside.write_text("escaped\n", encoding="utf-8")
        link = self.package_dir / "link.txt"
        link.symlink_to(outside)
        document = self.build()
        document["package"]["artifacts"].append(
            {
                "path": "link.txt",
                "sha256": hashlib.sha256(outside.read_bytes()).hexdigest(),
            }
        )
        support.finalize(document)
        report = self.decide(document)
        self.assertEqual(report["outcome"], "BLOCKED")
        self.assertIn("artifact_path_unsafe", self.codes(report))

    def test_symlink_inside_the_package_is_allowed(self):
        link = self.package_dir / "alias.md"
        link.symlink_to(self.package_dir / "SKILL.md")
        document = self.build()
        document["package"]["artifacts"].append(
            {
                "path": "alias.md",
                "sha256": hashlib.sha256(
                    (self.package_dir / "SKILL.md").read_bytes()
                ).hexdigest(),
            }
        )
        support.finalize(document)
        self.assertEqual(self.decide(document)["outcome"], "COMPLETE")

    def test_duplicate_artifact_path_rejected(self):
        document = self.build()
        document["package"]["artifacts"].append(
            copy.deepcopy(document["package"]["artifacts"][0])
        )
        support.finalize(document)
        self.assertIn("artifact_duplicate_path", self.codes(self.decide(document)))

    def test_bad_hash_format_rejected(self):
        document = self.build()
        document["package"]["artifacts"][0]["sha256"] = "not-a-hash"
        support.finalize(document)
        self.assertIn("artifact_hash_format", self.codes(self.decide(document)))


class TaskManifest(CouncilCase):
    """The original request and amendments must be a hashed artifact."""

    def test_absent_task_manifest_blocks(self):
        document = self.build()
        del document["package"]["task_manifest"]
        report = self.decide(document)
        self.assertEqual(report["outcome"], "BLOCKED")
        self.assertIn("task_manifest_missing", self.codes(report))

    def test_task_manifest_must_be_a_declared_artifact(self):
        document = self.build()
        document["package"]["task_manifest"] = "TASK.md"
        report = self.decide(document)
        self.assertIn("package_shape", self.codes(report))

    def test_task_manifest_hash_is_verified(self):
        (self.package_dir / "task.json").write_text("{}\n", encoding="utf-8")
        report = self.decide(self.packet)
        self.assertIn("artifact_hash_mismatch", self.codes(report))


class CompletePath(CouncilCase):
    """A well-formed unanimous packet is accepted, read-only, and stable."""

    def test_valid_packet_completes(self):
        report = self.decide(self.packet)
        self.assertEqual(report["outcome"], "COMPLETE")
        self.assertTrue(report["ok"])
        self.assertEqual(report["problems"], [])
        self.assertEqual(report["panel_size"], 3)
        self.assertEqual(
            report["reviewer_verdicts"],
            {"rev-lead": "COMPLETE", "rev-2": "COMPLETE", "rev-3": "COMPLETE"},
        )

    def test_shipped_template_matches_builder_shape(self):
        template = json.loads(support.TEMPLATE.read_text(encoding="utf-8"))
        self.assertEqual(template["schema_version"], 1)
        self.assertEqual(
            sorted(template), sorted(support.base_packet(support.synthetic_artifacts()))
        )
        self.assertEqual(
            template["package"]["task_manifest"], "task.json"
        )

    def test_report_shape_and_determinism(self):
        first = self.decide(self.packet)
        second = self.decide(self.packet)
        self.assertEqual(first, second)
        for key in (
            "ok",
            "outcome",
            "problems",
            "notes",
            "unresolved_blocking_finding_ids",
            "disputed_finding_ids",
            "failed_required_check_ids",
            "not_run_required_check_ids",
            "reviewer_verdicts",
            "package_fingerprint",
            "package_verified",
            "panel_size",
            "provenance_note",
        ):
            self.assertIn(key, first)
        self.assertIn("cannot authenticate", first["provenance_note"])

    def test_input_is_not_mutated(self):
        document = self.build()
        before = copy.deepcopy(document)
        self.decide(document)
        self.assertEqual(document, before)

    def test_checker_writes_no_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = support.write_packet(root / "packet.json", self.packet)
            before = sorted(p.name for p in root.iterdir())
            completed = support.run_cli(
                str(path), "--json", "--package-dir", str(self.package_dir)
            )
            self.assertEqual(completed.returncode, 0, completed)
            self.assertEqual(before, sorted(p.name for p in root.iterdir()))

    def test_single_reviewer_packet_completes_with_self_reconciliation_note(self):
        document = support.single_reviewer_packet(self.artifacts)
        report = self.decide(document)
        self.assertEqual(report["outcome"], "COMPLETE", report["problems"])
        self.assertIn("single_reviewer_panel", support.note_codes(report))


class PanelRules(CouncilCase):
    """Panel size, actor identity, and authority are enforced."""

    def test_four_reviewers_rejected(self):
        document = self.build()
        document["panel"].append(
            {
                "actor": "rev-4",
                "model": "gpt-6-sol",
                "reasoning_effort": "medium",
                "role": "reviewer",
            }
        )
        for entry in document["rounds"]:
            entry["reviewers"].append("rev-4")
        document["reviewer_finals"].append(
            {
                "actor": "rev-4",
                "round_id": "round-2-reconciliation",
                "package_fingerprint": self.fp,
                "panel_generation": 1,
                "covered_requirement_ids": ["R1", "R2", "R3"],
                "verdict": "COMPLETE",
                "outcomes": [
                    {"finding_id": "F1", "disposition": "resolved"},
                    {"finding_id": "F2", "disposition": "not_applicable"},
                ],
            }
        )
        report = self.decide(document)
        self.assertEqual(report["outcome"], "BLOCKED")
        self.assertIn("panel_size", self.codes(report))

    def test_duplicate_actor_rejected(self):
        document = self.build()
        document["panel"][2]["actor"] = "rev-2"
        report = self.decide(document)
        self.assertIn("duplicate_actor", self.codes(report))
        self.assertNotEqual(report["outcome"], "COMPLETE")

    def test_package_author_cannot_hold_a_seat(self):
        document = self.build()
        document["package"]["produced_by"] = ["rev-2"]
        report = self.decide(document)
        self.assertIn("author_self_review", self.codes(report))
        self.assertEqual(report["outcome"], "BLOCKED")

    def test_lead_planner_must_be_seated(self):
        document = self.build()
        document["integration"]["lead_planner_actor"] = "not-seated"
        self.assertIn("lead_not_on_panel", self.codes(self.decide(document)))

    def test_unavailable_reviewer_cannot_accept(self):
        document = self.build()
        document["panel"][1]["unavailable"] = True
        report = self.decide(document)
        self.assertIn("unavailable_reviewer", self.codes(report))
        self.assertEqual(report["outcome"], "BLOCKED")

    def test_single_mode_needs_one_seat_and_matching_authority(self):
        document = self.build()
        document["integration"]["mode"] = "single"
        document["integration"]["single_reviewer_actor"] = "rev-lead"
        self.assertIn("authority_mismatch", self.codes(self.decide(document)))

        solo = support.single_reviewer_packet(self.artifacts)
        solo["integration"]["mode"] = "single"
        solo["integration"]["single_reviewer_actor"] = "rev-solo"
        accepted = self.decide(solo)
        self.assertEqual(accepted["outcome"], "COMPLETE", accepted["problems"])

    def test_council_mode_rejects_single_reviewer_authority_field(self):
        document = self.build()
        document["integration"]["single_reviewer_actor"] = "rev-lead"
        self.assertIn("authority_mismatch", self.codes(self.decide(document)))


class RoundProtocol(CouncilCase):
    """Independent review, reconciliation, attendance, and the bounded loop."""

    def test_first_round_must_be_independent(self):
        document = self.build()
        document["rounds"][0]["kind"] = "reconciliation"
        self.assertIn("round_order", self.codes(self.decide(document)))

    def test_reconciliation_round_required(self):
        document = self.build(rounds=[self.packet["rounds"][0]])
        document["reviewer_finals"] = []
        self.assertIn("round_order", self.codes(self.decide(document)))

    def test_concluding_round_must_seat_every_reviewer_with_two_reconciliations(self):
        """F-V1: an earlier reconciliation must not excuse an absent final seat."""
        document = self.build()
        document["rounds"].append(
            {
                "id": "round-3-reconciliation",
                "kind": "reconciliation",
                "package_fingerprint": self.fp,
                "new_evidence": True,
                "reviewers": ["rev-3"],
                "raised_finding_ids": [],
            }
        )
        for entry in document["reviewer_finals"]:
            entry["round_id"] = "round-3-reconciliation"
        report = self.decide(document)
        self.assertEqual(report["outcome"], "BLOCKED")
        self.assertIn("round_attendance", self.codes(report))

    def test_verdict_from_an_absent_seat_rejected(self):
        document = self.build()
        document["rounds"][-1]["reviewers"] = ["rev-lead", "rev-2"]
        report = self.decide(document)
        self.assertIn("round_attendance", self.codes(report))

    def test_targeted_round_needs_new_evidence(self):
        document = self.build()
        document["rounds"].insert(
            2,
            {
                "id": "round-3-targeted",
                "kind": "targeted",
                "package_fingerprint": self.fp,
                "new_evidence": False,
                "reviewers": ["rev-lead", "rev-2", "rev-3"],
                "raised_finding_ids": [],
            },
        )
        self.assertIn("targeted_without_evidence", self.codes(self.decide(document)))

    def test_bounded_loop_allows_a_targeted_round_then_reconciliation(self):
        document = self.build()
        document["rounds"] = [
            document["rounds"][0],
            document["rounds"][1],
            {
                "id": "round-3-targeted",
                "kind": "targeted",
                "package_fingerprint": self.fp,
                "new_evidence": True,
                "reviewers": ["rev-lead", "rev-2"],
                "raised_finding_ids": [],
            },
            {
                "id": "round-4-reconciliation",
                "kind": "reconciliation",
                "package_fingerprint": self.fp,
                "new_evidence": True,
                "reviewers": ["rev-lead", "rev-2", "rev-3"],
                "raised_finding_ids": [],
            },
        ]
        for entry in document["reviewer_finals"]:
            entry["round_id"] = "round-4-reconciliation"
        report = self.decide(document)
        self.assertEqual(report["outcome"], "COMPLETE", report["problems"])

    def test_targeted_round_may_conclude_when_it_seats_everyone(self):
        document = self.build()
        document["rounds"].append(
            {
                "id": "round-3-targeted",
                "kind": "targeted",
                "package_fingerprint": self.fp,
                "new_evidence": True,
                "reviewers": ["rev-lead", "rev-2", "rev-3"],
                "raised_finding_ids": [],
            }
        )
        for entry in document["reviewer_finals"]:
            entry["round_id"] = "round-3-targeted"
        report = self.decide(document)
        self.assertEqual(report["outcome"], "COMPLETE", report["problems"])

    def test_lone_targeted_final_round_rejected(self):
        document = self.build()
        document["rounds"].append(
            {
                "id": "round-3-targeted",
                "kind": "targeted",
                "package_fingerprint": self.fp,
                "new_evidence": True,
                "reviewers": ["rev-lead"],
                "raised_finding_ids": [],
            }
        )
        report = self.decide(document)
        self.assertIn("round_conclusion", self.codes(report))
        self.assertIn("round_attendance", self.codes(report))

    def test_targeted_round_cap_enforced(self):
        document = self.build()
        template = {
            "kind": "targeted",
            "package_fingerprint": self.fp,
            "new_evidence": True,
            "reviewers": ["rev-lead", "rev-2", "rev-3"],
            "raised_finding_ids": [],
        }
        document["rounds"] = [
            document["rounds"][0],
            document["rounds"][1],
            dict(template, id="round-3-targeted"),
            dict(template, id="round-4-targeted"),
            dict(template, id="round-5-targeted"),
        ]
        report = self.decide(document)
        self.assertIn("targeted_limit", self.codes(report))
        self.assertIn("round_limit", self.codes(report))

    def test_verdicts_must_come_from_the_concluding_round(self):
        document = self.build()
        for entry in document["reviewer_finals"]:
            entry["round_id"] = "round-1-independent"
        self.assertIn("final_round_invalid", self.codes(self.decide(document)))

    def test_every_seated_reviewer_must_reconcile(self):
        document = self.build()
        document["rounds"][1]["reviewers"] = ["rev-lead", "rev-2"]
        self.assertIn("round_reviewer_missing", self.codes(self.decide(document)))

    def test_unknown_round_reviewer_rejected(self):
        document = self.build()
        document["rounds"][0]["reviewers"].append("rev-outsider")
        self.assertIn("round_reviewer_unknown", self.codes(self.decide(document)))


class Findings(CouncilCase):
    """Collation keeps every finding, and settlement needs evidence."""

    def test_open_blocking_finding_blocks_completion(self):
        document = self.build()
        del document["findings"][0]["resolution"]
        document["findings"][0]["status"] = "open"
        report = self.decide(document)
        self.assertEqual(report["outcome"], "EVIDENCE_NEEDED")
        self.assertEqual(report["unresolved_blocking_finding_ids"], ["F1"])
        self.assertIn("unresolved_blocking_finding", self.codes(report))

    def test_settled_finding_needs_a_reason(self):
        document = self.build()
        document["findings"][0]["resolution"]["reason"] = ""
        self.assertIn("resolution_missing_reason", self.codes(self.decide(document)))

    def test_settled_finding_needs_evidence(self):
        document = self.build()
        document["findings"][0]["resolution"]["evidence"] = []
        self.assertIn("resolution_missing_evidence", self.codes(self.decide(document)))

    def test_settlement_bound_to_a_different_package_is_stale(self):
        document = self.build()
        document["findings"][0]["resolution"]["package_fingerprint"] = support.hex64(
            "older-package"
        )
        self.assertIn("resolution_stale_package", self.codes(self.decide(document)))

    def test_dispute_on_a_settled_blocking_finding_blocks(self):
        document = self.build()
        document["reviewer_finals"][1]["outcomes"][0]["disposition"] = "disputed"
        report = self.decide(document)
        self.assertEqual(report["outcome"], "EVIDENCE_NEEDED")
        self.assertEqual(report["disputed_finding_ids"], ["F1"])
        self.assertIn("finding_dispute_unresolved", self.codes(report))

    def test_confirmed_failure_outranks_a_dispute(self):
        document = self.build()
        document["reviewer_finals"][1]["outcomes"][0]["disposition"] = "disputed"
        document["required_checks"][0]["status"] = "failed"
        self.assertEqual(self.decide(document)["outcome"], "CHANGES_NEEDED")

    def test_collator_cannot_delete_a_minority_finding_from_both_places(self):
        document = self.build()
        document["findings"] = [document["findings"][0]]
        for entry in document["reviewer_finals"]:
            entry["outcomes"] = [{"finding_id": "F1", "disposition": "resolved"}]
        report = self.decide(document)
        self.assertEqual(report["outcome"], "BLOCKED")
        self.assertIn("collation_dropped_finding", self.codes(report))

    def test_collated_finding_with_no_source_report_rejected(self):
        document = self.build()
        document["findings"].append(
            {
                "id": "F3",
                "origin_actor": "rev-lead",
                "raised_at_round": "round-2-reconciliation",
                "requirement_ids": ["R3"],
                "classification": "suggestion",
                "status": "open",
                "summary": "An invented finding with no reviewer report.",
            }
        )
        for entry in document["reviewer_finals"]:
            entry["outcomes"].append(
                {"finding_id": "F3", "disposition": "not_applicable"}
            )
        report = self.decide(document)
        self.assertIn("finding_unattributed", self.codes(report))
        self.assertEqual(report["outcome"], "BLOCKED")

    def test_finding_attribution_must_match_its_reporter(self):
        document = self.build()
        document["findings"][1]["origin_actor"] = "rev-lead"
        self.assertIn("finding_attribution", self.codes(self.decide(document)))

    def test_missing_first_pass_report_rejected(self):
        document = self.build()
        document["initial_reports"] = document["initial_reports"][:2]
        report = self.decide(document)
        self.assertIn("initial_report_shape", self.codes(report))
        self.assertEqual(report["outcome"], "BLOCKED")

    def test_open_suggestion_does_not_block(self):
        self.assertEqual(self.packet["findings"][1]["classification"], "suggestion")
        self.assertEqual(self.packet["findings"][1]["status"], "open")
        self.assertEqual(self.decide(self.packet)["outcome"], "COMPLETE")


class RequiredChecks(CouncilCase):
    """Checks outrank approval."""

    def test_failed_check_blocks_unanimous_approval(self):
        document = self.build()
        document["required_checks"][0]["status"] = "failed"
        report = self.decide(document)
        self.assertEqual(report["outcome"], "CHANGES_NEEDED")
        self.assertEqual(report["failed_required_check_ids"], ["C1"])
        self.assertEqual(set(report["reviewer_verdicts"].values()), {"COMPLETE"})

    def test_not_run_check_asks_for_evidence(self):
        document = self.build()
        document["required_checks"][1]["status"] = "not_run"
        report = self.decide(document)
        self.assertEqual(report["outcome"], "EVIDENCE_NEEDED")
        self.assertEqual(report["not_run_required_check_ids"], ["C2"])
        self.assertIn("check_not_run", self.codes(report))

    def test_confirmed_failure_outranks_a_request_for_evidence(self):
        document = self.build()
        document["required_checks"][0]["status"] = "failed"
        document["required_checks"][1]["status"] = "not_run"
        self.assertEqual(self.decide(document)["outcome"], "CHANGES_NEEDED")

    def test_unknown_check_status_rejected(self):
        document = self.build()
        document["required_checks"][0]["status"] = "probably"
        report = self.decide(document)
        self.assertIn("check_status", self.codes(report))
        self.assertEqual(report["outcome"], "BLOCKED")


class Finals(CouncilCase):
    """Acceptance needs every seat, on one package, covering every requirement."""

    def test_missing_reviewer_cannot_count_as_approval(self):
        document = self.build()
        document["reviewer_finals"] = document["reviewer_finals"][:2]
        report = self.decide(document)
        self.assertEqual(report["outcome"], "BLOCKED")
        self.assertIn("final_missing_reviewer", self.codes(report))

    def test_stale_package_verdict_rejected(self):
        document = self.build()
        document["reviewer_finals"][2]["package_fingerprint"] = support.hex64("old")
        report = self.decide(document)
        self.assertIn("final_stale_package", self.codes(report))
        self.assertEqual(report["outcome"], "BLOCKED")

    def test_requirement_coverage_required_from_every_reviewer(self):
        document = self.build()
        document["reviewer_finals"][1]["covered_requirement_ids"] = ["R1"]
        self.assertIn("final_missing_requirement", self.codes(self.decide(document)))

    def test_unknown_verdict_rejected(self):
        document = self.build()
        document["reviewer_finals"][0]["verdict"] = "LGTM"
        report = self.decide(document)
        self.assertIn("verdict_unknown", self.codes(report))
        self.assertEqual(report["outcome"], "BLOCKED")

    def test_dissent_reports_changes_needed(self):
        document = self.build()
        document["reviewer_finals"][1]["verdict"] = "CHANGES_NEEDED"
        report = self.decide(document)
        self.assertEqual(report["outcome"], "CHANGES_NEEDED")
        self.assertIn("verdict_not_complete", self.codes(report))

    def test_evidence_needed_verdict_reported(self):
        document = self.build()
        for entry in document["reviewer_finals"]:
            entry["verdict"] = "EVIDENCE_NEEDED"
        self.assertEqual(self.decide(document)["outcome"], "EVIDENCE_NEEDED")

    def test_most_restrictive_verdict_wins(self):
        document = self.build()
        document["reviewer_finals"][0]["verdict"] = "EVIDENCE_NEEDED"
        document["reviewer_finals"][1]["verdict"] = "CHANGES_NEEDED"
        document["reviewer_finals"][2]["verdict"] = "BLOCKED"
        self.assertEqual(self.decide(document)["outcome"], "BLOCKED")

    def test_duplicate_final_from_one_reviewer_rejected(self):
        document = self.build()
        document["reviewer_finals"].append(
            copy.deepcopy(document["reviewer_finals"][0])
        )
        self.assertIn("final_duplicate_reviewer", self.codes(self.decide(document)))


class ModeSwitches(CouncilCase):
    """Changing reviewers or mode preserves findings and forces fresh review."""

    def test_switch_must_carry_outstanding_findings(self):
        document = self.build()
        document["integration"]["mode_history"] = [
            {
                "at_round": "round-1-independent",
                "mode": "single",
                "preserved_finding_ids": ["F1"],
            }
        ]
        self.assertIn("mode_history_lost_finding", self.codes(self.decide(document)))

    def test_switch_carrying_findings_passes(self):
        document = self.build()
        document["integration"]["mode_history"] = [
            {
                "at_round": "round-1-independent",
                "mode": "single",
                "preserved_finding_ids": ["F1", "F2"],
            }
        ]
        support.set_generation(document, 2)
        report = self.decide(document)
        self.assertEqual(report["outcome"], "COMPLETE", report["problems"])

    def test_reformed_panel_needs_fresh_votes_even_with_the_same_actors(self):
        document = self.build()
        document["integration"]["mode_history"] = [
            {
                "at_round": "round-1-independent",
                "mode": "single",
                "preserved_finding_ids": ["F1", "F2"],
            }
        ]
        document["integration"]["panel_generation"] = 2
        report = self.decide(document)
        self.assertEqual(report["outcome"], "BLOCKED")
        self.assertIn("stale_panel_vote", self.codes(report))

    def test_switch_needs_a_later_review_of_the_current_package(self):
        document = self.build()
        document["integration"]["mode_history"] = [
            {
                "at_round": "round-2-reconciliation",
                "mode": "single",
                "preserved_finding_ids": ["F1", "F2"],
            }
        ]
        self.assertIn("mode_history_no_rereview", self.codes(self.decide(document)))

    def test_panel_generation_needs_a_recorded_change(self):
        document = self.build()
        document["integration"]["panel_generation"] = 3
        self.assertIn("panel_generation", self.codes(self.decide(document)))

    def test_unknown_mode_rejected(self):
        document = self.build()
        document["integration"]["mode"] = "quorum"
        self.assertIn("mode_unknown", self.codes(self.decide(document)))


class FingerprintBinding(CouncilCase):
    """Criteria edits after the vote, and stale rounds, fail closed."""

    def test_criteria_change_after_the_vote_breaks_the_fingerprint(self):
        document = self.build()
        document["requirements"][1]["text"] = "Council seats one or two reviewers."
        report = self.decide(document)
        self.assertEqual(report["outcome"], "BLOCKED")
        self.assertIn("fingerprint_mismatch", self.codes(report))

    def test_check_criteria_change_after_the_vote_breaks_the_fingerprint(self):
        document = self.build()
        document["required_checks"][0]["criteria"] = "anything at all"
        self.assertIn("fingerprint_mismatch", self.codes(self.decide(document)))

    def test_artifact_list_change_after_the_vote_breaks_the_fingerprint(self):
        document = self.build()
        document["package"]["artifacts"][0]["sha256"] = support.hex64("swap")
        self.assertIn("fingerprint_mismatch", self.codes(self.decide(document)))

    def test_refreezing_the_package_restores_a_valid_record(self):
        document = self.build()
        document["requirements"][1]["text"] = "Council seats at most three."
        support.finalize(document)
        report = self.decide(document)
        self.assertEqual(report["outcome"], "COMPLETE", report["problems"])

    def test_package_change_requires_a_fresh_independent_round(self):
        """Correction 4: a stale independent round blocks, it is not just noted."""
        document = self.build()
        stale = support.hex64("previous")
        document["rounds"][0]["package_fingerprint"] = stale
        for report in document["initial_reports"]:
            report["package_fingerprint"] = stale
        result = self.decide(document)
        self.assertEqual(result["outcome"], "BLOCKED")
        self.assertIn("round_stale_history", self.codes(result))

    def test_stale_independent_report_blocks(self):
        document = self.build()
        document["initial_reports"][0]["package_fingerprint"] = support.hex64("old")
        self.assertIn("initial_report_age", self.codes(self.decide(document)))

    def test_stale_panel_generation_verdict_rejected(self):
        document = self.build()
        document["reviewer_finals"][2]["panel_generation"] = 0
        report = self.decide(document)
        self.assertEqual(report["outcome"], "BLOCKED")
        self.assertIn("stale_panel_vote", self.codes(report))

    def test_stale_panel_generation_first_pass_report_rejected(self):
        document = self.build()
        document["initial_reports"][1]["panel_generation"] = 7
        self.assertIn("stale_panel_vote", self.codes(self.decide(document)))

    def test_concluding_round_must_run_against_the_current_package(self):
        document = self.build()
        document["rounds"][-1]["package_fingerprint"] = support.hex64("previous")
        report = self.decide(document)
        self.assertIn("round_stale_package", self.codes(report))
        self.assertEqual(report["outcome"], "BLOCKED")

    def test_initial_report_from_a_different_round_rejected(self):
        document = self.build()
        document["initial_reports"][0]["round_id"] = "round-2-reconciliation"
        self.assertIn("initial_report_round", self.codes(self.decide(document)))

    def test_first_pass_union_must_match_the_round_list(self):
        document = self.build()
        document["rounds"][0]["raised_finding_ids"] = ["F1"]
        self.assertIn("initial_report_shape", self.codes(self.decide(document)))


class RequirementCoverage(CouncilCase):
    """The packet cannot drop the requirement list."""

    def test_empty_requirements_rejected(self):
        document = self.build()
        document["requirements"] = []
        support.finalize(document)
        report = self.decide(document)
        self.assertIn("requirements_empty", self.codes(report))
        self.assertEqual(report["outcome"], "BLOCKED")

    def test_duplicate_requirement_ids_rejected(self):
        document = self.build()
        document["requirements"].append({"id": "R1", "text": "duplicate"})
        support.finalize(document)
        self.assertIn("requirement_duplicate_id", self.codes(self.decide(document)))

    def test_unknown_covered_requirement_rejected(self):
        document = self.build()
        document["reviewer_finals"][0]["covered_requirement_ids"] = [
            "R1",
            "R2",
            "R3",
            "R4",
        ]
        self.assertIn("final_unknown_requirement", self.codes(self.decide(document)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
