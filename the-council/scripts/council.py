#!/usr/bin/env python3
"""Deterministic, read-only decision checker for The Council review protocol.

The Council runs one to three independent reviewers against the same frozen
package. This module does not run reviewers, choose models, or touch the network.
It validates the *record* of a council decision and returns the outcome the
record actually supports, so a coordinator cannot claim completion from an
incomplete, stale, or self-contradicting packet.

Public API::

    from council import council_decide, CouncilInputError

    report = council_decide(packet)                # -> dict, never mutates packet
    report = council_decide(packet, package_dir="/path/to/frozen/package")

``report["outcome"]`` is one of ``COMPLETE``, ``CHANGES_NEEDED``,
``EVIDENCE_NEEDED``, or ``BLOCKED``. ``report["problems"]`` holds one
``code: where: message`` string per reason the outcome is not ``COMPLETE``.

Command line::

    python3 scripts/council.py packet.json --json \
      --package-dir /path/to/frozen-package

Exit status is ``0`` for ``COMPLETE``, ``1`` for any other supported outcome, and
``2`` when the input is unusable or does not meet the minimum shape contract.
``COMPLETE`` also requires the frozen artifact bytes to verify against
``--package-dir``; without it the best available outcome is ``EVIDENCE_NEEDED``.

What this checker does not do is stated in ``PROVENANCE_NOTE`` and in the skill
instructions. It validates the packet, not the honesty of the reviewers.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

SCHEMA_VERSION = 1
MAX_PANEL = 3
MAX_TARGETED_ROUNDS = 2
MAX_ROUNDS = 4
MAX_ID_LENGTH = 64

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]*$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

OUTCOMES = ("COMPLETE", "CHANGES_NEEDED", "EVIDENCE_NEEDED", "BLOCKED")
VERDICTS = frozenset(OUTCOMES)
# Severity ladder. A confirmed failure outranks a missing-evidence dispute, and
# an unavailable dependency outranks a confirmed failure.
OUTCOME_SEVERITY = {"EVIDENCE_NEEDED": 1, "CHANGES_NEEDED": 2, "BLOCKED": 3}
DISPOSITIONS = frozenset(
    {"accepted", "disputed", "resolved", "withdrawn", "not_applicable"}
)
# A disposition that still asserts the finding stands.
STANDING_DISPOSITIONS = frozenset({"accepted", "disputed"})
FINDING_CLASSES = frozenset({"blocking", "suggestion"})
FINDING_STATUSES = frozenset({"open", "resolved", "withdrawn"})
CHECK_STATUSES = frozenset({"passed", "failed", "not_run"})
ROUND_KINDS = frozenset({"independent", "reconciliation", "targeted"})
MODES = frozenset({"single", "council"})
PANEL_ROLES = frozenset({"lead", "reviewer"})

REQUIRED_SECTIONS = (
    ("package", dict),
    ("requirements", list),
    ("required_checks", list),
    ("integration", dict),
    ("panel", list),
    ("rounds", list),
    ("initial_reports", list),
    ("findings", list),
    ("reviewer_finals", list),
)

# Codes that mean the record itself cannot be trusted. They force BLOCKED, so a
# broken packet can never be reported as an accepted result in any other form.
RECORD_CODES = frozenset(
    {
        "council_id",
        "package_shape",
        "task_manifest_missing",
        "fingerprint_mismatch",
        "requirement_duplicate_id",
        "requirement_shape",
        "requirements_empty",
        "check_duplicate_id",
        "check_shape",
        "check_status",
        "panel_size",
        "panel_shape",
        "duplicate_actor",
        "panel_role",
        "unavailable_reviewer",
        "mode_unknown",
        "lead_not_on_panel",
        "authority_mismatch",
        "author_self_review",
        "panel_generation",
        "rounds_shape",
        "round_duplicate_id",
        "round_kind",
        "round_order",
        "round_conclusion",
        "round_attendance",
        "round_stale_package",
        "round_stale_history",
        "round_limit",
        "round_reviewer_missing",
        "round_reviewer_unknown",
        "targeted_limit",
        "targeted_without_evidence",
        "initial_report_shape",
        "initial_report_actor",
        "initial_report_round",
        "initial_report_age",
        "collation_dropped_finding",
        "finding_unattributed",
        "finding_attribution",
        "finding_duplicate_id",
        "finding_shape",
        "finding_class_unknown",
        "finding_status_unknown",
        "finding_unknown_requirement",
        "finding_unknown_actor",
        "finding_unknown_round",
        "resolution_missing_reason",
        "resolution_missing_evidence",
        "resolution_stale_package",
        "final_missing_reviewer",
        "final_unknown_reviewer",
        "final_duplicate_reviewer",
        "final_shape",
        "stale_panel_vote",
        "verdict_unknown",
        "final_round_invalid",
        "final_stale_package",
        "final_missing_requirement",
        "final_unknown_requirement",
        "final_dropped_finding",
        "final_unknown_finding",
        "final_duplicate_outcome",
        "disposition_unknown",
        "outcome_shape",
        "mode_history_shape",
        "mode_history_round",
        "mode_history_lost_finding",
        "mode_history_no_rereview",
        "artifact_duplicate_path",
        "artifact_shape",
        "artifact_path_unsafe",
        "artifact_hash_format",
        "artifact_missing",
        "artifact_unreadable",
        "artifact_hash_mismatch",
    }
)

PROVENANCE_NOTE = (
    "This checker validates the recorded council decision, not the reviewers. "
    "It cannot authenticate a model route, judge whether an objection is "
    "supported, detect a fabricated transcript, or prove that recorded evidence "
    "is truthful. Listed artifact hashes are re-hashed only when --package-dir "
    "is supplied, and a declared fingerprint only binds the fields it was "
    "computed over. The skill instructions carry the rules this record cannot "
    "encode."
)


class CouncilInputError(Exception):
    """The input is unreadable or fails the minimum shape contract (exit 2)."""


# --------------------------------------------------------------------------
# small typed accessors
# --------------------------------------------------------------------------


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _valid_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value) <= MAX_ID_LENGTH
        and bool(ID_RE.match(value))
    )


def _looks_like_sha256(value: object) -> bool:
    return isinstance(value, str) and bool(SHA256_RE.match(value))


def _unsafe_relative_path(path: str) -> str | None:
    """Return a reason a declared artifact path is unsafe, or None when it is fine.

    Artifact paths are package-relative and must stay inside the frozen package,
    so absolute paths, drive letters, Windows separators, uncollapsed separators,
    empty segments, ``.`` and ``..`` traversal are all rejected before anything
    is read. A path must be canonical so two records cannot name one file.
    """
    if not path or not path.strip():
        return "path is empty"
    if "\\" in path:
        return "path must use '/' separators"
    if path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        return "path must be relative to the package, not absolute"
    segments = path.split("/")
    if any(segment in {"", ".", ".."} for segment in segments):
        return "path must not contain empty, '.' or '..' segments"
    return None


def _concluding_round(rounds: list, actors: list) -> dict | None:
    """Return the round whose votes conclude the review, if any.

    A reconciliation round concludes. A targeted evidence round may also
    conclude when it seats every reviewer, because it reconciles the collated
    findings inside the same round; that keeps the loop inside four rounds.
    """
    if not rounds:
        return None
    last = rounds[-1]
    if last["kind"] == "reconciliation":
        return last
    if last["kind"] == "targeted" and set(actors).issubset(set(last["reviewers"])):
        return last
    return next((r for r in reversed(rounds) if r["kind"] == "reconciliation"), None)


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def package_fingerprint_from_packet(packet: dict) -> str:
    """Return the fingerprint the packet's own criteria and artifacts imply.

    The digest binds the requirement list, the required-check list and their
    criteria, and every declared artifact hash. Editing a requirement or a check
    after the reviewers voted therefore changes the expected fingerprint and the
    record stops being ``COMPLETE``.

    It does not bind the original task or its amendments directly. The frozen
    package is expected to carry those as a hashed artifact (see
    ``package.task_manifest``), so their bytes are bound through the artifact
    hash they are listed under.

    This is a consistency guard for an honest coordinator. It cannot stop an
    actor who rewrites every field of the record to match.
    """
    package = packet.get("package") or {}
    requirements = [
        {"id": r.get("id"), "text": r.get("text")}
        for r in (packet.get("requirements") or [])
        if isinstance(r, dict)
    ]
    checks = [
        {"id": c.get("id"), "text": c.get("text"), "criteria": c.get("criteria", "")}
        for c in (packet.get("required_checks") or [])
        if isinstance(c, dict)
    ]
    artifacts = [
        {"path": a.get("path"), "sha256": a.get("sha256")}
        for a in (package.get("artifacts") or [])
        if isinstance(a, dict)
    ]
    manifest = {"requirements": requirements, "required_checks": checks, "artifacts": artifacts}
    try:
        encoded = _canonical(manifest)
    except TypeError as exc:
        raise CouncilInputError(
            f"packet fields cannot be canonicalised into a fingerprint: {exc}"
        ) from exc
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _require_shape(packet: object) -> None:
    """Raise CouncilInputError when the packet cannot be evaluated at all."""
    if not isinstance(packet, dict):
        raise CouncilInputError("packet must be a JSON object")
    version = packet.get("schema_version")
    if version != SCHEMA_VERSION:
        raise CouncilInputError(
            f"unsupported schema_version {version!r}; expected {SCHEMA_VERSION}"
        )
    for key, expected in REQUIRED_SECTIONS:
        if key not in packet:
            raise CouncilInputError(f"missing required section '{key}'")
        if not isinstance(packet[key], expected):
            raise CouncilInputError(
                f"section '{key}' must be {expected.__name__}, "
                f"got {type(packet[key]).__name__}"
            )


def load_packet(path: Path) -> dict:
    """Read a packet from disk. Raises CouncilInputError on unusable input."""
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise CouncilInputError(f"cannot read {path}: {exc}") from exc
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CouncilInputError(f"{path} is not valid JSON: {exc}") from exc
    _require_shape(parsed)
    return parsed


def package_fingerprint(root: Path) -> str:
    """Return a deterministic SHA-256 over every file under ``root``."""
    digest = hashlib.sha256()
    files = sorted(p for p in Path(root).rglob("*") if p.is_file())
    for path in files:
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


# --------------------------------------------------------------------------
# the checker
# --------------------------------------------------------------------------


class _Checker:
    """Accumulates findings about one packet. Never writes to disk."""

    def __init__(self, package_dir: Path | None) -> None:
        self.problems: list[str] = []
        self.notes: list[str] = []
        self.package_dir = Path(package_dir) if package_dir is not None else None
        self.package_verified: bool | None = None
        self.artifacts_unverified = False
        self.unverified_reason = ""
        self.fingerprint = ""
        self.unresolved: list[str] = []
        self.disputed: dict[str, list[str]] = {}
        self.failed_checks: list[str] = []
        self.not_run_checks: list[str] = []
        self.verdicts: dict[str, str] = {}
        self.raised_at: dict[str, int] = {}
        self.raw_finding_ids: set[str] = set()
        self.reports: dict[str, list] = {}
        self.generation = 1

    # -- reporting helpers -------------------------------------------------

    def bad(self, code: str, where: str, message: str) -> None:
        self.problems.append(f"{code}: {where}: {message}")

    def note(self, code: str, message: str) -> None:
        self.notes.append(f"{code}: {message}")

    @property
    def record_broken(self) -> bool:
        return any(p.split(":", 1)[0] in RECORD_CODES for p in self.problems)

    # -- sections ----------------------------------------------------------

    def package(self, section: dict) -> dict:
        where = "package"
        package_id = section.get("id")
        if not _valid_id(package_id):
            self.bad("package_shape", where, "id must be a valid identifier")
        version = section.get("version")
        if not _is_text(version):
            self.bad("package_shape", where, "version must be a non-empty string")
        fingerprint = section.get("fingerprint")
        if not _looks_like_sha256(fingerprint):
            self.bad(
                "package_shape",
                where,
                "fingerprint must be a lowercase hex SHA-256 of the frozen package",
            )
        else:
            self.fingerprint = fingerprint

        produced_by = section.get("produced_by", [])
        if produced_by is None:
            produced_by = []
        if not isinstance(produced_by, list) or not all(
            _valid_id(a) for a in produced_by
        ):
            self.bad(
                "package_shape", where, "produced_by must be a list of actor ids"
            )
            produced_by = []

        artifacts = section.get("artifacts", [])
        if artifacts is None:
            artifacts = []
        if not isinstance(artifacts, list):
            self.bad("artifact_shape", where, "artifacts must be a list")
            artifacts = []
        seen_paths: set[str] = set()
        normalized: list[dict] = []
        for index, entry in enumerate(artifacts):
            spot = f"{where}.artifacts[{index}]"
            if not isinstance(entry, dict):
                self.bad("artifact_shape", spot, "must be an object")
                continue
            path = entry.get("path")
            if not _is_text(path):
                self.bad("artifact_shape", spot, "path must be a non-empty string")
                continue
            unsafe = _unsafe_relative_path(path)
            if unsafe:
                self.bad("artifact_path_unsafe", spot, f"{path!r}: {unsafe}")
                continue
            if path in seen_paths:
                self.bad("artifact_duplicate_path", spot, f"duplicate path {path!r}")
            seen_paths.add(path)
            digest = entry.get("sha256")
            if not _looks_like_sha256(digest):
                self.bad(
                    "artifact_hash_format", spot, "sha256 must be lowercase hex, 64 chars"
                )
                continue
            normalized.append({"path": path, "sha256": digest})

        task_manifest = section.get("task_manifest")
        if task_manifest is None:
            self.bad(
                "task_manifest_missing",
                where,
                "package.task_manifest must name the hashed artifact holding the "
                "original request and its amendments, so the task the reviewers "
                "accepted is the task in the record",
            )
        elif not _is_text(task_manifest):
            self.bad(
                "package_shape", where, "task_manifest must be a non-empty string path"
            )
        elif task_manifest not in seen_paths:
            self.bad(
                "package_shape",
                where,
                f"task_manifest {task_manifest!r} is not one of the declared artifacts",
            )

        if self.package_dir is None:
            self.artifacts_unverified = True
            self.unverified_reason = (
                f"{len(normalized)} declared artifact hash(es) were not re-hashed; "
                "no --package-dir was supplied"
            )
            self.package_verified = None
        elif not normalized:
            self.artifacts_unverified = True
            self.unverified_reason = (
                "the package declares no artifacts, so no frozen bytes were "
                "verified"
            )
            self.package_verified = False
        else:
            self.package_verified = self._verify_artifacts(normalized)
            self.artifacts_unverified = not self.package_verified
            self.unverified_reason = (
                "one or more declared artifacts failed verification"
            )
        return {
            "id": package_id,
            "version": version,
            "fingerprint": fingerprint,
            "produced_by": list(produced_by),
            "artifacts": normalized,
            "task_manifest": task_manifest,
        }

    def _verify_artifacts(self, artifacts: list) -> bool:
        """Re-hash declared artifacts inside the package directory, fail closed.

        Every failure mode - escape, missing file, unreadable file, changed bytes -
        is recorded as a problem, so verification never fails open.
        """
        try:
            base = self.package_dir.resolve()
        except OSError as exc:
            self.bad(
                "artifact_unreadable",
                "package",
                f"cannot resolve package directory {self.package_dir}: {exc}",
            )
            return False
        verified = True
        for entry in sorted(artifacts, key=lambda e: e["path"]):
            target = base / entry["path"]
            try:
                resolved = target.resolve()
            except OSError as exc:
                verified = False
                self.bad(
                    "artifact_unreadable",
                    "package",
                    f"{entry['path']} could not be resolved: {exc}",
                )
                continue
            if not resolved.is_relative_to(base):
                verified = False
                self.bad(
                    "artifact_path_unsafe",
                    "package",
                    f"{entry['path']} resolves outside {base}",
                )
                continue
            if not resolved.is_file():
                verified = False
                self.bad(
                    "artifact_missing",
                    "package",
                    f"{entry['path']} is not a regular file under {base}",
                )
                continue
            try:
                actual = hashlib.sha256(resolved.read_bytes()).hexdigest()
            except OSError as exc:
                verified = False
                self.bad(
                    "artifact_unreadable",
                    "package",
                    f"{entry['path']} could not be read: {exc}",
                )
                continue
            if actual != entry["sha256"]:
                verified = False
                self.bad(
                    "artifact_hash_mismatch",
                    "package",
                    f"{entry['path']} is {actual}, packet records {entry['sha256']}",
                )
        return verified

    def requirements(self, section: list) -> list[str]:
        if not section:
            self.bad("requirements_empty", "requirements", "at least one required")
        seen: set[str] = set()
        ordered: list[str] = []
        for index, entry in enumerate(section):
            spot = f"requirements[{index}]"
            if not isinstance(entry, dict):
                self.bad("requirement_shape", spot, "must be an object")
                continue
            rid = entry.get("id")
            if not _valid_id(rid):
                self.bad("requirement_shape", spot, "id must be a valid identifier")
                continue
            if rid in seen:
                self.bad("requirement_duplicate_id", spot, f"duplicate id {rid!r}")
                continue
            if not _is_text(entry.get("text")):
                self.bad("requirement_shape", spot, "text must be a non-empty string")
            seen.add(rid)
            ordered.append(rid)
        return ordered

    def required_checks(self, section: list) -> list[str]:
        seen: set[str] = set()
        passed: list[str] = []
        for index, entry in enumerate(section):
            spot = f"required_checks[{index}]"
            if not isinstance(entry, dict):
                self.bad("check_shape", spot, "must be an object")
                continue
            cid = entry.get("id")
            if not _valid_id(cid):
                self.bad("check_shape", spot, "id must be a valid identifier")
                continue
            if cid in seen:
                self.bad("check_duplicate_id", spot, f"duplicate id {cid!r}")
                continue
            seen.add(cid)
            if not _is_text(entry.get("text")):
                self.bad("check_shape", spot, "text must be a non-empty string")
            status = entry.get("status")
            if status not in CHECK_STATUSES:
                self.bad(
                    "check_status",
                    spot,
                    f"status must be one of {sorted(CHECK_STATUSES)}, got {status!r}",
                )
                continue
            if status == "passed":
                passed.append(cid)
            elif status == "not_run":
                self.not_run_checks.append(cid)
            else:
                self.failed_checks.append(cid)
        return passed

    def panel(self, section: list) -> list[dict]:
        actors: list[str] = []
        normalized: list[dict] = []
        for index, entry in enumerate(section):
            spot = f"panel[{index}]"
            if not isinstance(entry, dict):
                self.bad("panel_shape", spot, "must be an object")
                continue
            actor = entry.get("actor")
            if not _valid_id(actor):
                self.bad("panel_shape", spot, "actor must be a valid identifier")
                continue
            if actor in actors:
                self.bad(
                    "duplicate_actor",
                    spot,
                    f"actor {actor!r} already holds a separate Council seat; "
                    "same-model peers still need distinct actors",
                )
                continue
            model = entry.get("model")
            if not _is_text(model):
                self.bad("panel_shape", spot, "model must be a non-empty string")
            effort = entry.get("reasoning_effort")
            if not _is_text(effort):
                self.bad(
                    "panel_shape", spot, "reasoning_effort must be a non-empty string"
                )
            role = entry.get("role", "reviewer")
            if role not in PANEL_ROLES:
                self.bad(
                    "panel_role",
                    spot,
                    f"role must be one of {sorted(PANEL_ROLES)}, got {role!r}",
                )
            if entry.get("unavailable") is True:
                self.bad(
                    "unavailable_reviewer",
                    spot,
                    f"reviewer {actor!r} is marked unavailable; an absent or "
                    "timed-out reviewer cannot count as approval",
                )
            actors.append(actor)
            normalized.append(
                {
                    "actor": actor,
                    "model": model,
                    "reasoning_effort": effort,
                    "role": role,
                }
            )
        if not normalized or len(normalized) > MAX_PANEL:
            self.bad(
                "panel_size",
                "panel",
                f"a Council panel holds 1 to {MAX_PANEL} reviewers, got {len(normalized)}",
            )
        if len(normalized) == 1:
            self.note(
                "single_reviewer_panel",
                "one reviewer still reconciles its own findings; unanimity here is "
                "self-reconciliation, not cross-reviewer consensus",
            )
        return normalized

    def integration(self, section: dict, panel: list[dict], package: dict) -> dict:
        where = "integration"
        mode = section.get("mode")
        if mode not in MODES:
            self.bad(
                "mode_unknown",
                where,
                f"mode must be one of {sorted(MODES)}, got {mode!r}",
            )
        actors = [entry["actor"] for entry in panel]
        lead = section.get("lead_planner_actor")
        single = section.get("single_reviewer_actor")
        if mode == "council":
            if not _valid_id(lead) or lead not in actors:
                self.bad(
                    "lead_not_on_panel",
                    where,
                    "council mode needs a lead planner that occupies one of the "
                    f"{MAX_PANEL} seats, got {lead!r}",
                )
            if single is not None:
                self.bad(
                    "authority_mismatch",
                    where,
                    "single_reviewer_actor must be null in council mode",
                )
        elif mode == "single":
            if len(panel) != 1:
                self.bad(
                    "authority_mismatch",
                    where,
                    f"single-reviewer mode needs a one-seat panel, got {len(panel)}",
                )
            if not _valid_id(single):
                self.bad(
                    "authority_mismatch",
                    where,
                    "single-reviewer mode needs single_reviewer_actor",
                )
            elif actors and single != actors[0]:
                self.bad(
                    "authority_mismatch",
                    where,
                    f"single_reviewer_actor {single!r} is not the seated reviewer "
                    f"{actors[0]!r}",
                )
            if lead is not None and lead != single:
                self.bad(
                    "authority_mismatch",
                    where,
                    "single-reviewer mode has one authority; lead_planner_actor "
                    "must be absent or identical",
                )

        produced_by = set(package.get("produced_by") or [])
        overlap = sorted(produced_by.intersection(actors))
        if overlap:
            self.bad(
                "author_self_review",
                where,
                f"package producers {overlap} also hold a Council seat; acceptance "
                "requires a reviewer independent of the author",
            )

        history = section.get("mode_history", [])
        if history is None:
            history = []
        generation = section.get("panel_generation", 1)
        if not _is_int(generation) or generation < 1:
            self.bad(
                "panel_generation",
                where,
                f"panel_generation must be an integer of at least 1, got "
                f"{generation!r}",
            )
        elif generation > 1 and not history:
            self.bad(
                "panel_generation",
                where,
                f"panel_generation {generation} claims earlier panels, but no "
                "mode_history records the membership change and its carried "
                "findings",
            )
        if _is_int(generation) and generation >= 1:
            self.generation = generation
        if not isinstance(history, list):
            self.bad("mode_history_shape", where, "mode_history must be a list")
            history = []
        normalized_history: list[dict] = []
        for index, entry in enumerate(history):
            spot = f"{where}.mode_history[{index}]"
            if not isinstance(entry, dict):
                self.bad("mode_history_shape", spot, "must be an object")
                continue
            if entry.get("mode") not in MODES:
                self.bad(
                    "mode_unknown",
                    spot,
                    f"mode must be one of {sorted(MODES)}, got {entry.get('mode')!r}",
                )
            preserved = entry.get("preserved_finding_ids", [])
            if preserved is None:
                preserved = []
            if not isinstance(preserved, list) or not all(
                _valid_id(f) for f in preserved
            ):
                self.bad(
                    "mode_history_shape",
                    spot,
                    "preserved_finding_ids must be a list of finding ids",
                )
                preserved = []
            normalized_history.append(
                {
                    "at_round": entry.get("at_round"),
                    "mode": entry.get("mode"),
                    "preserved_finding_ids": list(preserved),
                }
            )
        return {
            "mode": mode,
            "lead_planner_actor": lead,
            "single_reviewer_actor": single,
            "panel_generation": generation,
            "mode_history": normalized_history,
        }

    def rounds(self, section: list, panel: list[dict]) -> list[dict]:
        where = "rounds"
        actors = [entry["actor"] for entry in panel]
        if not section:
            self.bad("rounds_shape", where, "at least one round is required")
        ids: list[str] = []
        normalized: list[dict] = []
        for index, entry in enumerate(section):
            spot = f"{where}[{index}]"
            if not isinstance(entry, dict):
                self.bad("rounds_shape", spot, "must be an object")
                continue
            rid = entry.get("id")
            if not _valid_id(rid):
                self.bad("rounds_shape", spot, "id must be a valid identifier")
                continue
            if rid in ids:
                self.bad("round_duplicate_id", spot, f"duplicate round id {rid!r}")
                continue
            ids.append(rid)
            kind = entry.get("kind")
            if kind not in ROUND_KINDS:
                self.bad(
                    "round_kind",
                    spot,
                    f"kind must be one of {sorted(ROUND_KINDS)}, got {kind!r}",
                )
                continue
            reviewers = entry.get("reviewers")
            if not isinstance(reviewers, list) or not reviewers:
                self.bad(
                    "round_reviewer_missing", spot, "reviewers must be a non-empty list"
                )
                reviewers = []
            for actor in reviewers:
                if not _valid_id(actor) or actor not in actors:
                    self.bad(
                        "round_reviewer_unknown",
                        spot,
                        f"{actor!r} is not on the panel",
                    )
            raised = entry.get("raised_finding_ids")
            if not isinstance(raised, list) or not all(_valid_id(f) for f in raised):
                self.bad(
                    "rounds_shape",
                    spot,
                    "raised_finding_ids must list the findings first raised in this "
                    "round, so a collator cannot drop one silently",
                )
                raised = []
            self.raw_finding_ids.update(raised)
            if kind == "targeted" and entry.get("new_evidence") is not True:
                self.bad(
                    "targeted_without_evidence",
                    spot,
                    "a targeted round must record new_evidence: true; without new "
                    "evidence the loop stops",
                )
            fingerprint = entry.get("package_fingerprint")
            if not _looks_like_sha256(fingerprint):
                self.bad(
                    "rounds_shape",
                    spot,
                    "package_fingerprint must be a lowercase hex SHA-256",
                )
            normalized.append(
                {
                    "id": rid,
                    "kind": kind,
                    "package_fingerprint": fingerprint,
                    "new_evidence": entry.get("new_evidence") is True,
                    "reviewers": list(reviewers),
                    "raised_finding_ids": list(raised),
                }
            )

        kinds = [r["kind"] for r in normalized]
        if normalized:
            if kinds[0] != "independent":
                self.bad(
                    "round_order",
                    where,
                    "the first round must be the independent first review",
                )
            if kinds.count("independent") != 1:
                self.bad(
                    "round_order",
                    where,
                    "exactly one independent round is allowed",
                )
            if "reconciliation" not in kinds:
                self.bad(
                    "round_order",
                    where,
                    "each reviewer must receive the collated findings in a "
                    "reconciliation round before any verdict",
                )
            else:
                concluding = _concluding_round(normalized, actors)
                seats_concluded = concluding is not None and concluding is normalized[-1]
                if not seats_concluded:
                    self.bad(
                        "round_conclusion",
                        where,
                        "the final round must be a reconciliation, or a targeted "
                        "round seating every reviewer so the collated findings are "
                        "reconciled inside it",
                    )
            concluding = normalized[-1]
            absent = [actor for actor in actors if actor not in concluding["reviewers"]]
            if absent:
                self.bad(
                    "round_attendance",
                    concluding["id"],
                    f"the concluding round omits seated reviewer(s) {absent}; an "
                    "earlier reconciliation does not substitute for the final one",
                )
            if normalized[-1]["package_fingerprint"] != self.fingerprint:
                self.bad(
                    "round_stale_package",
                    normalized[-1]["id"],
                    "the concluding round ran against "
                    f"{normalized[-1]['package_fingerprint']!r}, but the package "
                    f"under review is {self.fingerprint!r}",
                )
            if any(
                r["package_fingerprint"] != self.fingerprint for r in normalized[:-1]
            ):
                stale = [
                    r["id"]
                    for r in normalized[:-1]
                    if r["package_fingerprint"] != self.fingerprint
                ]
                self.bad(
                    "round_stale_history",
                    where,
                    f"round(s) {stale} ran against a different package fingerprint. "
                    "A changed package needs a fresh independent review in a new "
                    "packet that carries the original finding history, not a "
                    "reconciliation of the old round",
                )
            if kinds.count("targeted") > MAX_TARGETED_ROUNDS:
                self.bad(
                    "targeted_limit",
                    where,
                    f"at most {MAX_TARGETED_ROUNDS} targeted rounds are allowed, "
                    f"got {kinds.count('targeted')}",
                )
            if len(normalized) > MAX_ROUNDS:
                self.bad(
                    "round_limit",
                    where,
                    f"at most {MAX_ROUNDS} rounds are allowed, got {len(normalized)}",
                )
            independent = normalized[0]
            for actor in actors:
                if actor not in independent["reviewers"]:
                    self.bad(
                        "round_reviewer_missing",
                        independent["id"],
                        f"seated reviewer {actor!r} is absent from the first "
                        "independent review",
                    )
            reconciled: set[str] = set()
            for entry in normalized:
                if entry["kind"] == "reconciliation":
                    reconciled.update(entry["reviewers"])
            for actor in actors:
                if actor not in reconciled:
                    self.bad(
                        "round_reviewer_missing",
                        where,
                        f"seated reviewer {actor!r} never received the collated "
                        "findings in a reconciliation round",
                    )
        return normalized

    def initial_reports(self, section: list, panel: list[dict], rounds: list[dict]):
        """Validate each reviewer's raw first-pass findings, before collation.

        These lists are the independent source of truth: a finding that a
        reviewer raised here must exist in the collated set, so dropping a
        minority finding from both the collation and the verdicts fails.
        """
        where = "initial_reports"
        actors = [entry["actor"] for entry in panel]
        independent = rounds[0] if rounds else None
        independent_id = independent["id"] if independent else None
        independent_fingerprint = independent["package_fingerprint"] if independent else None
        reports: dict[str, list] = {}
        for index, entry in enumerate(section):
            spot = f"{where}[{index}]"
            if not isinstance(entry, dict):
                self.bad("initial_report_shape", spot, "must be an object")
                continue
            actor = entry.get("actor")
            if not _valid_id(actor) or actor not in actors:
                self.bad(
                    "initial_report_actor",
                    spot,
                    f"{actor!r} is not a seated reviewer",
                )
                continue
            if actor in reports:
                self.bad(
                    "initial_report_shape",
                    spot,
                    f"{actor!r} filed more than one first-pass report",
                )
                continue
            if entry.get("round_id") != independent_id:
                self.bad(
                    "initial_report_round",
                    spot,
                    f"first-pass report belongs to the independent round "
                    f"{independent_id!r}, got {entry.get('round_id')!r}",
                )
            if entry.get("package_fingerprint") != independent_fingerprint:
                self.bad(
                    "initial_report_age",
                    spot,
                    "first-pass report names "
                    f"{entry.get('package_fingerprint')!r} but the independent "
                    f"round ran against {independent_fingerprint!r}",
                )
            if entry.get("panel_generation") != self.generation:
                self.bad(
                    "stale_panel_vote",
                    spot,
                    f"first-pass report comes from panel generation "
                    f"{entry.get('panel_generation')!r}, current generation is "
                    f"{self.generation!r}",
                )
            ids = entry.get("finding_ids")
            if not isinstance(ids, list) or not all(_valid_id(f) for f in ids):
                self.bad(
                    "initial_report_shape",
                    spot,
                    "finding_ids must list the raw finding ids this reviewer raised",
                )
                ids = []
            if len(set(ids)) != len(ids):
                self.bad(
                    "initial_report_shape", spot, "finding_ids contains duplicates"
                )
            reports[actor] = list(ids)
            self.raw_finding_ids.update(ids)
        self.reports = reports
        for actor in actors:
            if actor not in reports:
                self.bad(
                    "initial_report_shape",
                    where,
                    f"seated reviewer {actor!r} filed no independent first-pass "
                    "report; the collation has no independent source to check",
                )
        if independent is not None and reports:
            union = {fid for ids in reports.values() for fid in ids}
            if union != set(independent["raised_finding_ids"]):
                self.bad(
                    "initial_report_shape",
                    independent["id"],
                    "raised_finding_ids must equal the union of the first-pass "
                    f"reports ({sorted(union)})",
                )
        return reports

    def findings(
        self,
        section: list,
        panel: list[dict],
        requirements: list[str],
        rounds: list[dict],
    ) -> dict[str, dict]:
        where = "findings"
        actors = {entry["actor"] for entry in panel}
        round_ids = [entry["id"] for entry in rounds]
        normalized: dict[str, dict] = {}
        for index, entry in enumerate(section):
            spot = f"{where}[{index}]"
            if not isinstance(entry, dict):
                self.bad("finding_shape", spot, "must be an object")
                continue
            fid = entry.get("id")
            if not _valid_id(fid):
                self.bad("finding_shape", spot, "id must be a valid identifier")
                continue
            if fid in normalized:
                self.bad("finding_duplicate_id", spot, f"duplicate finding id {fid!r}")
                continue
            origin = entry.get("origin_actor")
            if not _valid_id(origin) or origin not in actors:
                self.bad(
                    "finding_unknown_actor",
                    spot,
                    f"origin_actor {origin!r} is not a seated reviewer",
                )
            raised = entry.get("raised_at_round")
            if raised not in round_ids:
                self.bad(
                    "finding_unknown_round",
                    spot,
                    f"raised_at_round {raised!r} is not a recorded round",
                )
            reqs = entry.get("requirement_ids")
            if not isinstance(reqs, list) or not reqs:
                self.bad(
                    "finding_shape",
                    spot,
                    "requirement_ids must map the finding to at least one requirement",
                )
                reqs = []
            for rid in reqs:
                if rid not in requirements:
                    self.bad(
                        "finding_unknown_requirement",
                        spot,
                        f"requirement {rid!r} is not declared in the packet",
                    )
            classification = entry.get("classification")
            if classification not in FINDING_CLASSES:
                self.bad(
                    "finding_class_unknown",
                    spot,
                    f"classification must be one of {sorted(FINDING_CLASSES)}, "
                    f"got {classification!r}",
                )
            status = entry.get("status")
            if status not in FINDING_STATUSES:
                self.bad(
                    "finding_status_unknown",
                    spot,
                    f"status must be one of {sorted(FINDING_STATUSES)}, got {status!r}",
                )
            if not _is_text(entry.get("summary")):
                self.bad("finding_shape", spot, "summary must be a non-empty string")

            resolution = entry.get("resolution")
            if status in {"resolved", "withdrawn"}:
                if not isinstance(resolution, dict):
                    self.bad(
                        "resolution_missing_reason",
                        spot,
                        f"status {status!r} needs a resolution recording why, with "
                        "evidence; a vote alone does not settle a finding",
                    )
                    resolution = {}
                if not _is_text(resolution.get("reason")):
                    self.bad(
                        "resolution_missing_reason",
                        spot,
                        "resolution.reason must state why the finding is settled",
                    )
                evidence = resolution.get("evidence")
                if isinstance(evidence, list):
                    ok_evidence = bool(evidence) and all(_is_text(e) for e in evidence)
                else:
                    ok_evidence = _is_text(evidence)
                if not ok_evidence:
                    self.bad(
                        "resolution_missing_evidence",
                        spot,
                        "resolution.evidence must cite the check, test, or artifact "
                        "that settles the finding",
                    )
                recorded = resolution.get("package_fingerprint")
                if not _looks_like_sha256(recorded):
                    self.bad(
                        "resolution_stale_package",
                        spot,
                        "resolution.package_fingerprint must name the reviewed "
                        "package version",
                    )
                elif self.fingerprint and recorded != self.fingerprint:
                    self.bad(
                        "resolution_stale_package",
                        spot,
                        "resolution was settled against a different package "
                        "fingerprint than the one under review",
                    )
            if classification == "blocking" and status == "open":
                self.unresolved.append(fid)

            normalized[fid] = {
                "id": fid,
                "origin_actor": origin,
                "raised_at_round": raised,
                "classification": classification,
                "status": status,
                "requirement_ids": list(reqs),
            }
        return normalized

    def finals(
        self,
        section: list,
        panel: list[dict],
        requirements: list[str],
        rounds: list[dict],
        findings: dict[str, dict],
        package: dict,
    ) -> list[dict]:
        where = "reviewer_finals"
        actors = [entry["actor"] for entry in panel]
        decided: set[str] = set()
        normalized: list[dict] = []
        concluding = _concluding_round(rounds, actors)
        for index, entry in enumerate(section):
            spot = f"{where}[{index}]"
            if not isinstance(entry, dict):
                self.bad("final_shape", spot, "must be an object")
                continue
            actor = entry.get("actor")
            if not _valid_id(actor) or actor not in actors:
                self.bad(
                    "final_unknown_reviewer", spot, f"{actor!r} is not a seated reviewer"
                )
                continue
            if actor in decided:
                self.bad(
                    "final_duplicate_reviewer",
                    spot,
                    f"{actor!r} returned more than one final verdict",
                )
                continue
            decided.add(actor)

            verdict = entry.get("verdict")
            if verdict not in VERDICTS:
                self.bad(
                    "verdict_unknown",
                    spot,
                    f"verdict must be one of {list(OUTCOMES)}, got {verdict!r}",
                )
            self.verdicts[actor] = verdict

            if concluding is not None:
                if entry.get("round_id") != concluding["id"]:
                    self.bad(
                        "final_round_invalid",
                        spot,
                        f"verdict must be issued in the concluding round "
                        f"{concluding['id']!r}, got {entry.get('round_id')!r}",
                    )
                elif actor not in concluding["reviewers"]:
                    self.bad(
                        "round_attendance",
                        spot,
                        f"{actor!r} filed a verdict for the concluding round "
                        f"{concluding['id']!r} without attending it",
                    )
            if entry.get("package_fingerprint") != package.get("fingerprint"):
                self.bad(
                    "final_stale_package",
                    spot,
                    "verdict is bound to "
                    f"{entry.get('package_fingerprint')!r}, but the package under "
                    f"review is {package.get('fingerprint')!r}; changed artifacts "
                    "need a fresh review",
                )
            if entry.get("panel_generation") != self.generation:
                self.bad(
                    "stale_panel_vote",
                    spot,
                    f"verdict comes from panel generation "
                    f"{entry.get('panel_generation')!r}, current generation is "
                    f"{self.generation!r}; a re-formed panel needs fresh votes "
                    "even when the actors are unchanged",
                )

            covered = entry.get("covered_requirement_ids")
            if not isinstance(covered, list) or not all(_valid_id(c) for c in covered):
                self.bad(
                    "final_missing_requirement",
                    spot,
                    "covered_requirement_ids must list the requirements this "
                    "reviewer checked",
                )
                covered = []
            for rid in covered:
                if rid not in requirements:
                    self.bad(
                        "final_unknown_requirement",
                        spot,
                        f"requirement {rid!r} is not declared in the packet",
                    )
            missing_reqs = [r for r in requirements if r not in covered]
            if missing_reqs:
                self.bad(
                    "final_missing_requirement",
                    spot,
                    f"no coverage recorded for requirements {missing_reqs}",
                )

            outcomes = entry.get("outcomes")
            if not isinstance(outcomes, list):
                self.bad(
                    "final_dropped_finding",
                    spot,
                    "outcomes must list a disposition for every collated finding",
                )
                outcomes = []
            seen_findings: set[str] = set()
            parsed_outcomes: list[dict] = []
            for j, outcome in enumerate(outcomes):
                ospot = f"{spot}.outcomes[{j}]"
                if not isinstance(outcome, dict):
                    self.bad("outcome_shape", ospot, "must be an object")
                    continue
                fid = outcome.get("finding_id")
                if fid not in findings:
                    self.bad(
                        "final_unknown_finding",
                        ospot,
                        f"finding {fid!r} is not in the collated finding set",
                    )
                    continue
                if fid in seen_findings:
                    self.bad(
                        "final_duplicate_outcome",
                        ospot,
                        f"finding {fid!r} has more than one disposition",
                    )
                    continue
                seen_findings.add(fid)
                disposition = outcome.get("disposition")
                if disposition not in DISPOSITIONS:
                    self.bad(
                        "disposition_unknown",
                        ospot,
                        f"disposition must be one of {sorted(DISPOSITIONS)}, "
                        f"got {disposition!r}",
                    )
                parsed_outcomes.append(
                    {"finding_id": fid, "disposition": disposition}
                )
                if (
                    disposition in STANDING_DISPOSITIONS
                    and findings[fid]["classification"] == "blocking"
                ):
                    self.disputed.setdefault(fid, []).append(actor)
            for fid, finding in sorted(findings.items()):
                if fid in seen_findings:
                    continue
                self.bad(
                    "final_dropped_finding",
                    spot,
                    f"reviewer {actor!r} did not dispose of finding {fid!r} raised "
                    f"by {finding['origin_actor']!r}; a minority finding stays in "
                    "the collated set for every reviewer",
                )
            normalized.append(
                {
                    "actor": actor,
                    "verdict": verdict,
                    "covered_requirement_ids": list(covered),
                    "outcomes": parsed_outcomes,
                }
            )

        for actor in actors:
            if actor not in decided:
                self.bad(
                    "final_missing_reviewer",
                    where,
                    f"seated reviewer {actor!r} returned no final verdict; a missing "
                    "or timed-out reviewer is not acceptance",
                )
        for fid, holders in sorted(self.disputed.items()):
            finding = findings.get(fid)
            if finding is None:
                continue
            if finding["classification"] != "blocking":
                continue
            if finding["status"] not in {"resolved", "withdrawn"}:
                continue
            self.bad(
                "finding_dispute_unresolved",
                "findings",
                f"{fid!r} is recorded {finding['status']} but "
                f"{sorted(set(holders))} still stand on it; settling a blocking "
                "finding needs reasons and evidence every reviewer accepts",
            )
        return normalized

    def mode_history(self, history: list[dict], rounds: list[dict]) -> None:
        if not history:
            return
        order = {entry["id"]: index for index, entry in enumerate(rounds)}
        raised_at = self.raised_at
        for index, entry in enumerate(history):
            spot = f"integration.mode_history[{index}]"
            at = entry["at_round"]
            if at not in order:
                self.bad(
                    "mode_history_round",
                    spot,
                    f"at_round {at!r} is not a recorded round",
                )
                continue
            boundary = order[at]
            preserved = set(entry["preserved_finding_ids"])
            outstanding = {
                fid for fid, r in raised_at.items() if r is not None and r <= boundary
            }
            lost = sorted(outstanding - preserved)
            if lost:
                self.bad(
                    "mode_history_lost_finding",
                    spot,
                    f"mode change at round {at!r} dropped outstanding finding(s) "
                    f"{lost}; a handoff preserves open defects and evidence",
                )
            later = [
                entry_round
                for position, entry_round in enumerate(rounds)
                if position > boundary
                and entry_round["package_fingerprint"] == self.fingerprint
                and entry_round["kind"] in {"reconciliation", "targeted"}
            ]
            if not later:
                self.bad(
                    "mode_history_no_rereview",
                    spot,
                    f"mode change at round {at!r} has no later review against the "
                    "current package; a new authority needs a fresh review",
                )

    def decide(self) -> str:
        """Return the most restrictive outcome the recorded facts support.

        A confirmed fact outranks a missing-evidence dispute: a failed required
        check or a reviewer's ``CHANGES_NEEDED`` forces ``CHANGES_NEEDED`` even
        when an unresolved dispute would otherwise only ask for evidence. A
        reviewer's ``BLOCKED`` (an unavailable dependency or decision) outranks
        both.
        """
        if self.record_broken:
            return "BLOCKED"
        reasons: set = set()
        for verdict in self.verdicts.values():
            if verdict in VERDICTS and verdict != "COMPLETE":
                reasons.add(verdict)
        if self.failed_checks:
            reasons.add("CHANGES_NEEDED")
        if self.not_run_checks:
            reasons.add("EVIDENCE_NEEDED")
        if self.artifacts_unverified:
            reasons.add("EVIDENCE_NEEDED")
        if self.unresolved or self.disputed:
            reasons.add("EVIDENCE_NEEDED")
        if not reasons:
            return "COMPLETE"
        return max(reasons, key=lambda v: OUTCOME_SEVERITY[v])


# --------------------------------------------------------------------------
# public entry points
# --------------------------------------------------------------------------


def council_decide(packet: dict, *, package_dir: str | Path | None = None) -> dict:
    """Decide the outcome a council packet supports. Never mutates ``packet``.

    Raises :class:`CouncilInputError` when the packet is unusable or fails the
    minimum shape contract. Every other defect is reported in ``problems`` and
    forces a non-``COMPLETE`` outcome.
    """
    _require_shape(packet)
    checker = _Checker(package_dir)
    package = checker.package(packet["package"])
    requirements = checker.requirements(packet["requirements"])
    checker.required_checks(packet["required_checks"])
    panel = checker.panel(packet["panel"])
    integration = checker.integration(packet["integration"], panel, package)
    rounds = checker.rounds(packet["rounds"], panel)
    reports = checker.initial_reports(packet["initial_reports"], panel, rounds)
    findings = checker.findings(
        packet["findings"], panel, requirements, rounds
    )
    checker.finals(
        packet["reviewer_finals"], panel, requirements, rounds, findings, package
    )

    expected = package_fingerprint_from_packet(packet)
    if checker.fingerprint and expected != checker.fingerprint:
        checker.bad(
            "fingerprint_mismatch",
            "package",
            f"declared fingerprint {checker.fingerprint} does not cover the "
            f"requirements, checks, and artifacts in this packet ({expected}); "
            "criteria changed after the vote",
        )
    if checker.artifacts_unverified:
        checker.bad(
            "artifacts_unverified",
            "package",
            f"{checker.unverified_reason}; completion needs the frozen bytes "
            "verified against --package-dir",
        )
    independent_id = rounds[0]["id"] if rounds else None
    collated = set(findings)
    for fid in sorted(checker.raw_finding_ids - collated):
        checker.bad(
            "collation_dropped_finding",
            "findings",
            f"{fid!r} was raised in a reviewer's own report but never reached the "
            "collated finding set",
        )
    for fid in sorted(collated - checker.raw_finding_ids):
        checker.bad(
            "finding_unattributed",
            "findings",
            f"{fid!r} appears only in the collated set; no reviewer report raised it",
        )
    round_raised = {
        entry["id"]: set(entry["raised_finding_ids"]) for entry in rounds
    }
    for fid, finding in sorted(findings.items()):
        if fid not in round_raised.get(finding["raised_at_round"], set()):
            checker.bad(
                "finding_unattributed",
                "findings",
                f"{fid!r} names round {finding['raised_at_round']!r}, which does "
                "not list it as raised there",
            )
        if finding["raised_at_round"] != independent_id:
            continue
        if fid not in reports.get(finding["origin_actor"], []):
            checker.bad(
                "finding_attribution",
                "findings",
                f"{fid!r} is attributed to {finding['origin_actor']!r}, whose "
                "first-pass report does not list it",
            )

    order = {entry["id"]: index for index, entry in enumerate(rounds)}
    checker.raised_at = {
        fid: order.get(finding["raised_at_round"])
        for fid, finding in findings.items()
    }
    checker.mode_history(integration["mode_history"], rounds)

    for cid in sorted(set(checker.failed_checks)):
        checker.bad(
            "check_failed",
            "required_checks",
            f"{cid!r} did not pass; unanimity cannot override a required check",
        )
    for cid in sorted(set(checker.not_run_checks)):
        checker.bad(
            "check_not_run",
            "required_checks",
            f"{cid!r} was never run; completion needs every required check passed",
        )
    for fid in sorted(set(checker.unresolved)):
        checker.bad(
            "unresolved_blocking_finding",
            "findings",
            f"{fid!r} is blocking and still open; resolve it with evidence or "
            "withdraw it with a reason",
        )
    for actor, verdict in sorted(checker.verdicts.items()):
        if verdict in VERDICTS and verdict != "COMPLETE":
            checker.bad(
                "verdict_not_complete",
                f"reviewer_finals[{actor}]",
                f"returned {verdict}; completion needs every reviewer accepting the "
                "same package",
            )

    outcome = checker.decide()
    problems = sorted(checker.problems)
    return {
        "ok": not problems,
        "outcome": outcome,
        "problems": problems,
        "notes": sorted(checker.notes),
        "unresolved_blocking_finding_ids": sorted(set(checker.unresolved)),
        "disputed_finding_ids": sorted(set(checker.disputed)),
        "failed_required_check_ids": sorted(set(checker.failed_checks)),
        "not_run_required_check_ids": sorted(set(checker.not_run_checks)),
        "reviewer_verdicts": dict(checker.verdicts),
        "package_fingerprint": checker.fingerprint or None,
        "package_verified": checker.package_verified,
        "panel_size": len(panel),
        "provenance_note": PROVENANCE_NOTE,
    }


def decide_file(
    path: str | Path, *, package_dir: str | Path | None = None
) -> dict:
    """Load ``path`` and decide it. Raises CouncilInputError on unusable input."""
    return council_decide(load_packet(Path(path)), package_dir=package_dir)


# --------------------------------------------------------------------------
# command line
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Decide whether The Council record supports completion."
    )
    parser.add_argument("packet", help="path to a council packet JSON file")
    parser.add_argument(
        "--package-dir",
        help="re-hash declared artifacts against this directory",
    )
    parser.add_argument("--json", action="store_true", help="print the full report")
    return parser


def _print_text(report: dict) -> None:
    print(f"outcome: {report['outcome']}")
    if report["problems"]:
        print("problems:")
        for problem in report["problems"]:
            print(f"  - {problem}")
    if report["notes"]:
        print("notes:")
        for note in report["notes"]:
            print(f"  - {note}")
    print(f"provenance: {report['provenance_note']}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = decide_file(args.packet, package_dir=args.package_dir)
    except CouncilInputError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        _print_text(report)
    return 0 if report["outcome"] == "COMPLETE" else 1


if __name__ == "__main__":
    raise SystemExit(main())
