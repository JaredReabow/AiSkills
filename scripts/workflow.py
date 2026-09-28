#!/usr/bin/env python3
"""Read-only task-ledger validation and scheduling for the ``parallelism`` skill.

The helper exists so the scheduling agent can keep one machine-checkable record
of an objective, its user requirements, the approved task graph, the evidence,
and the review decisions, then ask for a deterministic next-action suggestion
instead of reasoning about capacity from memory.

Guarantees, in order of importance:

* ``validate`` and ``schedule`` never write to the ledger, the workspace, or any
  other file. Every file is opened for reading only.
* Nothing recorded in a ledger is executed. A ``command`` string is evidence
  metadata; it is never passed to a shell.
* No agent, process, or network call is started by this module.

Actor names, model names, and command strings in a ledger are *declarations*.
They are not proof that a model was routed, that a command ran, or that a claim
is true. Reports carry that caveat explicitly.

Commands::

    python3 scripts/workflow.py validate --ledger ledger.json [--json]
    python3 scripts/workflow.py schedule --ledger ledger.json \
        --host-capacity 6 [--other-active 0] [--json]

Exit codes: ``0`` success (warnings allowed), ``1`` validation findings,
``2`` unusable input (missing file or invalid JSON).

The ledger schema is documented in ``references/ledger.md``; a complete valid
example lives in ``templates/ledger.example.json``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

SCHEMA_VERSION = 1

TASK_TYPES = ("research", "implementation", "verification", "integration")
TASK_STATES = (
    "queued",
    "running",
    "ready_for_review",
    "needs_changes",
    "needs_research",
    "blocked",
    "failed",
    "accepted",
)
# A task in one of these states may still be resumed with corrections, so its
# owned paths and exclusive resources stay reserved.
WORKSPACE_HOLD_STATES = (
    "running",
    "ready_for_review",
    "needs_changes",
    "needs_research",
)
# A task that has left the queue is in flight and implies satisfied dependencies.
IN_FLIGHT_STATES = WORKSPACE_HOLD_STATES
FROZEN_TARGET_STATES = ("ready_for_review", "accepted")
# States that still represent open work when checking for completion.
UNFINISHED_STATES = (
    "queued",
    "running",
    "ready_for_review",
    "needs_changes",
    "needs_research",
    "blocked",
    "failed",
)
VALIDATION_MODES = ("planning", "final")
# Task types that stay dispatchable under review backpressure: they are what
# lets pending reviews finish, so holding them would stall the backlog forever.
BACKLOG_EXEMPT_TYPES = ("verification", "research")
# Only a task with a worker actively engaged occupies an agent slot.
SLOT_STATES = ("running",)
EVIDENCE_STATUSES = ("pass", "fail", "missing")
REVIEW_DECISIONS = ("accept", "request_changes", "request_research", "blocked")
IMPORTANCE_LEVELS = ("standard", "important")
RESOURCE_KINDS = ("exclusive", "shared")

TOP_LEVEL_REQUIRED = ("schema_version", "objective", "requirements", "baseline", "tasks")
TOP_LEVEL_OPTIONAL = (
    "limits",
    "resources",
    "artifacts",
    "observations",
    "checkpoint",
    "extends",
)
LIMIT_KEYS = (
    "max_concurrent_workers",
    "max_total_workers",
    "review_backlog_limit",
    "budget_usd",
    "notes",
)

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]*$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_ID_LENGTH = 128

PROVENANCE_NOTE = (
    "Actor, model, and command fields are declarations supplied in the ledger. "
    "This helper does not authenticate routing, re-run commands, or treat a log "
    "as proof of execution."
)
BUDGET_NOTE = (
    "No cost is estimated here. A null budget stays unknown rather than being "
    "converted into an invented dollar figure or saving."
)
CAPACITY_NOTE = (
    "Workers are counted by actor, not by task. host_capacity is the total agent "
    "budget: other_active and the reserved reviewer slot are subtracted first, "
    "then limits.max_concurrent_workers caps running workers on its own. "
    "limits.max_total_workers caps how many distinct workers are ever dispatched."
)


class LedgerError(Exception):
    """Unusable input (unreadable file or invalid JSON) rather than a finding."""


class Report:
    """Validation findings plus the normalised model the scheduler reuses.

    ``tasks``, ``artifacts``, ``resources``, and ``limits`` hold the records the
    validator already parsed, so :func:`schedule` does not re-derive them and
    cannot drift from the checks that were actually applied.
    """

    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.tasks: dict[str, dict] = {}
        self.artifacts: dict[str, dict] = {}
        self.resources: dict[str, str] = {}
        self.limits: dict = {}

    def error(self, where: str, message: str) -> None:
        self.errors.append(f"{where}: {message}")

    def warn(self, where: str, message: str) -> None:
        self.warnings.append(f"{where}: {message}")

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "provenance_note": PROVENANCE_NOTE,
        }


# --------------------------------------------------------------------------
# small typed accessors
# --------------------------------------------------------------------------


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _check_id(report: Report, where: str, value: object, *, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        report.error(where, "must be a non-empty string identifier")
        return None
    if len(value) > MAX_ID_LENGTH:
        report.error(where, f"identifier is longer than {MAX_ID_LENGTH} characters")
        return None
    if not ID_RE.match(value):
        report.error(
            where,
            "identifier must match [A-Za-z0-9][A-Za-z0-9._:@+-]* "
            "(letters, digits, dot, underscore, colon, at, plus, hyphen)",
        )
        return None
    return value


def _check_str(
    report: Report,
    where: str,
    value: object,
    *,
    required: bool = True,
    allow_empty: bool = False,
) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str):
        report.error(where, f"must be a string, got {type(value).__name__}")
        return None
    if not allow_empty and not value.strip():
        report.error(where, "must not be empty")
        return None
    return value


def _check_list(report: Report, where: str, value: object, *, required: bool = True) -> list | None:
    if value is None and not required:
        return None
    if not isinstance(value, list):
        report.error(where, f"must be a list, got {type(value).__name__}")
        return None
    return value


def _check_dict(report: Report, where: str, value: object, *, required: bool = True) -> dict | None:
    if value is None and not required:
        return None
    if not isinstance(value, dict):
        report.error(where, f"must be an object, got {type(value).__name__}")
        return None
    return value


def _warn_unknown_keys(report: Report, where: str, obj: dict, allowed: tuple) -> None:
    for key in sorted(set(obj) - set(allowed)):
        report.warn(f"{where}.{key}", "unrecognized field is ignored by this helper")


def _id_list(report: Report, where: str, value: object, *, required: bool = True) -> list[str]:
    """Validate a list of identifier strings and return the usable ones."""
    items = _check_list(report, where, value, required=required)
    if items is None:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        ident = _check_id(report, f"{where}[{index}]", item)
        if ident is None:
            continue
        if ident in seen:
            report.error(f"{where}[{index}]", f"duplicate reference {ident!r} in the same list")
            continue
        seen.add(ident)
        out.append(ident)
    return out


def _path_list(report: Report, where: str, value: object, *, required: bool = True) -> list[str]:
    """Validate a list of non-empty path strings.

    Paths are deliberately not identifier-checked: separators and dots are
    expected. Workspace containment is enforced separately by
    :func:`resolve_within`.
    """
    items = _check_list(report, where, value, required=required)
    if items is None:
        return []
    out: list[str] = []
    for index, item in enumerate(items):
        if not isinstance(item, str) or not item.strip():
            report.error(f"{where}[{index}]", "must be a non-empty path string")
            continue
        out.append(item)
    return out


# --------------------------------------------------------------------------
# paths and hashes
# --------------------------------------------------------------------------


class PathEscape(Exception):
    """A declared relative path resolves outside its declared workspace."""


def resolve_within(base: Path, candidate: str) -> Path:
    """Resolve ``candidate`` under ``base``, rejecting escapes through ``..`` or symlinks.

    ``os.path.realpath`` resolves every existing symlink and normalises the
    non-existent tail lexically, so this catches a symlinked directory that
    points outside the workspace even when the final file does not exist yet.
    """
    base_real = Path(os.path.realpath(base))
    raw = Path(candidate)
    target = raw if raw.is_absolute() else base_real / raw
    resolved = Path(os.path.realpath(target))
    if resolved != base_real and base_real not in resolved.parents:
        raise PathEscape(f"{candidate!r} resolves outside the workspace ({resolved})")
    return resolved


def resolve_any(base: Path, candidate: str) -> Path:
    """Resolve an absolute or workspace-relative path without an inside-workspace rule."""
    raw = Path(candidate)
    target = raw if raw.is_absolute() else Path(os.path.realpath(base)) / raw
    return Path(os.path.realpath(target))


def resolve_within_safe(base: Path, candidate: str) -> tuple[Path | None, str | None]:
    """Resolve ``candidate`` without ever raising.

    Returns ``(absolute_path, None)`` or ``(None, message)``. Callers record the
    message as a validation error, so an unusable path can never escape as an
    uncaught exception while other checks are running.
    """
    try:
        return resolve_within(base, candidate), None
    except PathEscape as exc:
        return None, f"unsafe path: {exc}"
    except (OSError, ValueError) as exc:  # embedded NUL, undecodable name, ...
        return None, f"unusable path {candidate!r}: {exc}"


def resolve_absolute_safe(candidate: str) -> tuple[Path | None, str | None]:
    """Resolve an absolute path without raising."""
    try:
        return Path(os.path.realpath(candidate)), None
    except (OSError, ValueError) as exc:
        return None, f"unusable path {candidate!r}: {exc}"


def paths_overlap(left: Path, right: Path) -> bool:
    """Return True when two absolute paths name the same path or nest.

    Ownership is a tree, not a set: writing ``src`` and ``src/module.py`` at the
    same time is one conflict, not two unrelated writes.
    """
    if left == right:
        return True
    return left in right.parents or right in left.parents


def file_sha256(path: Path) -> str:
    """Return the lowercase hex SHA-256 of a file's bytes."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------


def load_ledger(path: Path) -> dict:
    """Load a ledger JSON document or raise :class:`LedgerError`."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise LedgerError(f"cannot read ledger {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LedgerError(f"ledger {path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise LedgerError(f"ledger {path} must contain a JSON object at the top level")
    return data


def validate_ledger(
    ledger: dict,
    *,
    base_dir: Path,
    verify_files: bool = True,
    mode: str = "planning",
) -> Report:
    """Validate one ledger and return every finding.

    ``base_dir`` is the ledger file's directory, used to resolve relative
    workspace paths. ``verify_files`` enables content checks: workspace
    existence, path escapes, and recorded artifact hashes. ``mode`` is
    ``planning`` (unmapped requirements warn) or ``final`` (every requirement
    must be covered by an accepted task, and no task may still be unfinished).
    """
    report = Report()
    allowed = set(TOP_LEVEL_REQUIRED) | set(TOP_LEVEL_OPTIONAL)
    _warn_unknown_keys(report, "ledger", ledger, tuple(sorted(allowed)))
    for key in TOP_LEVEL_REQUIRED:
        if key not in ledger:
            report.error("ledger", f"missing required field {key!r}")
    if not report.ok and "tasks" not in ledger:
        return report

    version = ledger.get("schema_version")
    if not _is_int(version) or version != SCHEMA_VERSION:
        report.error(
            "ledger.schema_version",
            f"must be the integer {SCHEMA_VERSION}, got {version!r}",
        )
        # A mismatched schema makes deeper checks unreliable.
        return report
    if mode not in VALIDATION_MODES:
        report.error(
            "ledger", f"validation mode must be one of {', '.join(VALIDATION_MODES)}"
        )
        return report

    amendments = _validate_objective(report, ledger.get("objective"))
    requirement_ids = _validate_requirements(report, ledger.get("requirements"), amendments)
    baseline_dir, baseline_revision = _validate_baseline(
        report, ledger.get("baseline"), base_dir, verify_files
    )
    limits = _validate_limits(report, ledger.get("limits"))
    resource_ids, resource_kinds = _validate_resources(report, ledger.get("resources"))
    tasks = _validate_tasks(
        report,
        ledger.get("tasks"),
        baseline_dir=baseline_dir,
        baseline_revision=baseline_revision,
        requirement_ids=requirement_ids,
        resource_ids=resource_ids,
        verify_files=verify_files,
    )
    artifact_ids = _validate_artifacts(
        report,
        ledger.get("artifacts"),
        tasks,
        verify_files=verify_files,
    )
    _validate_task_references(report, tasks, artifact_ids=artifact_ids)
    _validate_dependency_graph(report, tasks)
    _validate_state_consistency(report, tasks)
    _validate_acceptance(report, tasks, requirement_ids=requirement_ids, mode=mode)
    _validate_ownership_conflicts(report, tasks, resource_kinds)
    _validate_evidence(report, tasks, artifact_ids, verify_files=verify_files)
    _validate_reviews(report, tasks, artifact_ids, verify_files=verify_files)
    _validate_limits_usage(report, tasks, limits)
    _validate_observations(report, ledger.get("observations"))
    report.tasks = tasks
    report.artifacts = artifact_ids
    report.resources = resource_kinds
    report.limits = limits
    return report


def _validate_objective(report: Report, objective: object) -> dict[str, str]:
    """Validate the objective block and return amendment ids mapped to text."""
    obj = _check_dict(report, "ledger.objective", objective)
    if obj is None:
        return {}
    _warn_unknown_keys(report, "ledger.objective", obj, ("id", "text", "amendments"))
    _check_id(report, "ledger.objective.id", obj.get("id"))
    _check_str(report, "ledger.objective.text", obj.get("text"))
    amendments = _check_list(
        report, "ledger.objective.amendments", obj.get("amendments", []), required=False
    )
    result: dict[str, str] = {}
    if not amendments:
        return result
    for index, amendment in enumerate(amendments):
        where = f"ledger.objective.amendments[{index}]"
        item = _check_dict(report, where, amendment)
        if item is None:
            continue
        _warn_unknown_keys(report, where, item, ("id", "text", "recorded_at"))
        ident = _check_id(report, f"{where}.id", item.get("id"))
        _check_str(report, f"{where}.text", item.get("text"))
        _check_str(report, f"{where}.recorded_at", item.get("recorded_at"), required=False)
        if ident is None:
            continue
        if ident in result:
            report.error(f"{where}.id", f"duplicate amendment id {ident!r}")
            continue
        result[ident] = str(item.get("text", ""))
    return result


def _validate_requirements(report: Report, requirements: object, amendments: dict) -> set[str]:
    """Validate requirements, which give every user ask a stable identifier."""
    items = _check_list(report, "ledger.requirements", requirements)
    if items is None:
        return set()
    if not items:
        report.error("ledger.requirements", "must contain at least one requirement")
    ids: set[str] = set()
    for index, requirement in enumerate(items):
        where = f"ledger.requirements[{index}]"
        item = _check_dict(report, where, requirement)
        if item is None:
            continue
        _warn_unknown_keys(report, where, item, ("id", "text", "source", "notes"))
        ident = _check_id(report, f"{where}.id", item.get("id"))
        _check_str(report, f"{where}.text", item.get("text"))
        source = _check_str(report, f"{where}.source", item.get("source"))
        if source is not None and source != "objective" and source not in amendments:
            report.error(
                f"{where}.source",
                f"unknown source {source!r}; expected 'objective' or an amendment id",
            )
        if ident is None:
            continue
        if ident in ids:
            report.error(f"{where}.id", f"duplicate requirement id {ident!r}")
            continue
        ids.add(ident)
    return ids


def _validate_baseline(
    report: Report, baseline: object, base_dir: Path, verify_files: bool
) -> tuple[Path | None, str | None]:
    """Validate the baseline block and return its workspace and revision."""
    item = _check_dict(report, "ledger.baseline", baseline)
    if item is None:
        return None, None
    _warn_unknown_keys(
        report, "ledger.baseline", item, ("workspace", "revision", "dirty", "dirty_paths", "notes")
    )
    workspace = _check_str(report, "ledger.baseline.workspace", item.get("workspace"))
    revision = _check_str(report, "ledger.baseline.revision", item.get("revision"))
    if "dirty" in item and not isinstance(item["dirty"], bool):
        report.error("ledger.baseline.dirty", "must be a boolean when present")
    _path_list(report, "ledger.baseline.dirty_paths", item.get("dirty_paths", []), required=False)
    if workspace is None:
        return None, revision
    raw = Path(workspace)
    resolved = raw if raw.is_absolute() else (base_dir / raw)
    resolved = Path(os.path.realpath(resolved))
    if verify_files and not resolved.is_dir():
        report.error(
            "ledger.baseline.workspace",
            f"workspace directory does not exist: {resolved}",
        )
        return None, revision
    return resolved, revision


def _validate_limits(report: Report, limits: object) -> dict:
    """Validate the user-limit block. A null limit means 'not supplied'."""
    if limits is None:
        return {}
    item = _check_dict(report, "ledger.limits", limits)
    if item is None:
        return {}
    _warn_unknown_keys(report, "ledger.limits", item, LIMIT_KEYS)
    for key in ("max_concurrent_workers", "max_total_workers"):
        value = item.get(key)
        if value is None:
            continue
        if not _is_int(value) or value < 1:
            report.error(f"ledger.limits.{key}", "must be a positive integer or null")
    backlog = item.get("review_backlog_limit")
    if backlog is not None and (not _is_int(backlog) or backlog < 0):
        report.error(
            "ledger.limits.review_backlog_limit", "must be a non-negative integer or null"
        )
    budget = item.get("budget_usd")
    if budget is not None and (not _is_number(budget) or budget < 0):
        report.error("ledger.limits.budget_usd", "must be a non-negative number or null")
    if "notes" in item and item["notes"] is not None:
        _check_str(report, "ledger.limits.notes", item["notes"], allow_empty=True)
    return item


def _validate_resources(report: Report, resources: object) -> tuple[set[str], dict[str, str]]:
    """Validate declared shared/exclusive resources and return ids and kinds."""
    if resources is None:
        return set(), {}
    items = _check_list(report, "ledger.resources", resources, required=False)
    if items is None:
        return set(), {}
    ids: set[str] = set()
    kinds: dict[str, str] = {}
    for index, resource in enumerate(items):
        where = f"ledger.resources[{index}]"
        item = _check_dict(report, where, resource)
        if item is None:
            continue
        _warn_unknown_keys(report, where, item, ("id", "kind", "description", "paths"))
        ident = _check_id(report, f"{where}.id", item.get("id"))
        kind = _check_str(report, f"{where}.kind", item.get("kind"))
        if kind is not None and kind not in RESOURCE_KINDS:
            report.error(f"{where}.kind", f"must be one of {', '.join(RESOURCE_KINDS)}")
        _check_str(report, f"{where}.description", item.get("description"), required=False)
        _path_list(report, f"{where}.paths", item.get("paths", []), required=False)
        if ident is None:
            continue
        if ident in ids:
            report.error(f"{where}.id", f"duplicate resource id {ident!r}")
            continue
        ids.add(ident)
        kinds[ident] = kind or "exclusive"
    return ids, kinds


def _validate_tasks(
    report: Report,
    tasks: object,
    *,
    baseline_dir: Path | None,
    baseline_revision: str | None,
    requirement_ids: set[str],
    resource_ids: set[str],
    verify_files: bool,
) -> dict[str, dict]:
    """Validate every task record and return usable tasks keyed by id."""
    items = _check_list(report, "ledger.tasks", tasks)
    if items is None:
        return {}
    result: dict[str, dict] = {}
    for index, task in enumerate(items):
        where = f"ledger.tasks[{index}]"
        item = _check_dict(report, where, task)
        if item is None:
            continue
        _warn_unknown_keys(
            report,
            where,
            item,
            (
                "id",
                "type",
                "objective",
                "state",
                "requirements",
                "depends_on",
                "owner",
                "workspace",
                "baseline_revision",
                "write_paths",
                "read_paths",
                "resources",
                "acceptance",
                "evidence",
                "review",
                "importance",
                "verifies",
                "deferred_checks",
                "non_goals",
                "notes",
            ),
        )
        ident = _check_id(report, f"{where}.id", item.get("id"))
        task_type = _check_str(report, f"{where}.type", item.get("type"))
        if task_type is not None and task_type not in TASK_TYPES:
            report.error(f"{where}.type", f"must be one of {', '.join(TASK_TYPES)}")
        state = _check_str(report, f"{where}.state", item.get("state"))
        if state is not None and state not in TASK_STATES:
            report.error(f"{where}.state", f"must be one of {', '.join(TASK_STATES)}")
        _check_str(report, f"{where}.objective", item.get("objective"))
        importance = item.get("importance", "standard")
        if importance not in IMPORTANCE_LEVELS:
            report.error(f"{where}.importance", f"must be one of {', '.join(IMPORTANCE_LEVELS)}")
            importance = "standard"

        requirement_refs = _id_list(
            report, f"{where}.requirements", item.get("requirements", [])
        )
        for ref in requirement_refs:
            if ref not in requirement_ids:
                report.error(
                    f"{where}.requirements", f"unknown requirement reference {ref!r}"
                )
        if task_type in ("implementation", "integration", "verification") and not requirement_refs:
            report.error(
                f"{where}.requirements",
                f"a {task_type} task must map to at least one user requirement id",
            )

        dependencies = _id_list(report, f"{where}.depends_on", item.get("depends_on", []))
        if ident is not None and ident in dependencies:
            report.error(f"{where}.depends_on", "a task cannot depend on itself")

        resource_refs = _id_list(report, f"{where}.resources", item.get("resources", []))
        for ref in resource_refs:
            if ref not in resource_ids:
                report.error(f"{where}.resources", f"unknown resource reference {ref!r}")

        owner = item.get("owner")
        actor = None
        if state is not None and state != "queued":
            owner_obj = _check_dict(report, f"{where}.owner", owner)
            if owner_obj is not None:
                _warn_unknown_keys(report, f"{where}.owner", owner_obj, ("actor", "model"))
                actor = _check_id(report, f"{where}.owner.actor", owner_obj.get("actor"))
                if owner_obj.get("model") is None:
                    report.warn(
                        f"{where}.owner.model",
                        "no explicit model recorded; the skill requires an explicit route",
                    )
                else:
                    _check_str(report, f"{where}.owner.model", owner_obj["model"])
        elif owner is not None:
            owner_obj = _check_dict(report, f"{where}.owner", owner)
            if owner_obj is not None:
                actor = _check_id(
                    report, f"{where}.owner.actor", owner_obj.get("actor"), required=False
                )

        task_workspace = item.get("workspace")
        resolved_workspace = baseline_dir
        if task_workspace is not None:
            raw = _check_str(report, f"{where}.workspace", task_workspace)
            if raw is not None:
                candidate = Path(raw)
                if candidate.is_absolute():
                    resolved_workspace = Path(os.path.realpath(candidate))
                elif baseline_dir is not None:
                    resolved_workspace = Path(os.path.realpath(baseline_dir / candidate))
                else:
                    report.error(
                        f"{where}.workspace",
                        "a relative task workspace needs a usable ledger baseline workspace",
                    )
                    resolved_workspace = None
        if state != "queued" and resolved_workspace is not None and verify_files:
            if not resolved_workspace.is_dir():
                report.error(
                    f"{where}.workspace", f"workspace directory does not exist: {resolved_workspace}"
                )

        write_abs, read_abs = _validate_task_paths(report, where, item, resolved_workspace)
        _validate_task_acceptance(report, where, item, requirement_ids)

        verifies = _normalize_verifies(report, where, item)
        _path_list(report, f"{where}.deferred_checks", item.get("deferred_checks", []), required=False)
        if task_type == "verification":
            if not verifies:
                report.error(
                    f"{where}.verifies",
                    "a verification task must name the task, or tasks, it verifies",
                )
            for target in verifies:
                if target == ident:
                    report.error(
                        f"{where}.verifies", "a verification task cannot verify itself"
                    )
                if target in (item.get("depends_on") or []):
                    report.error(
                        f"{where}.depends_on",
                        f"a verifier must not depend on the target it verifies ({target!r}); "
                        "it consumes the frozen ready_for_review revision instead, which "
                        "would otherwise deadlock acceptance",
                    )
        elif verifies:
            report.warn(f"{where}.verifies", "only verification tasks should set 'verifies'")

        # A partially specified task cannot be dispatched.
        if state is not None and state != "queued":
            if actor is None:
                report.error(
                    f"{where}.owner.actor", f"a {state} task must record the owning actor"
                )
            if item.get("baseline_revision") is None and not baseline_revision:
                report.error(
                    f"{where}.baseline_revision",
                    "the handoff must record a baseline revision here or in ledger.baseline",
                )
            if item.get("baseline_revision") is not None:
                _check_str(report, f"{where}.baseline_revision", item["baseline_revision"])

        if ident is None:
            continue
        if ident in result:
            report.error(f"{where}.id", f"duplicate task id {ident!r}")
            continue
        item = dict(item)
        item["_index"] = index
        item["_importance"] = importance
        item["_actor"] = actor
        item["_workspace"] = resolved_workspace
        item["_write_abs"] = write_abs
        item["_read_abs"] = read_abs
        item["_verifies"] = verifies
        result[ident] = item
    return result


def _normalize_verifies(report: Report, where: str, item: dict) -> list[str]:
    """Return declared verification targets as a list of task ids.

    Accepts a single id or a list, so one verifier can cover several frozen
    targets such as two independently authored modules. Normalising here keeps
    closure, readiness, and conflict logic working from one representation.
    """
    raw = item.get("verifies")
    if raw is None:
        return []
    if isinstance(raw, str):
        candidates: list = [raw]
    elif isinstance(raw, list):
        candidates = raw
    else:
        report.error(
            f"{where}.verifies", "must be a task id or a list of task ids"
        )
        return []
    targets: list[str] = []
    for index, candidate in enumerate(candidates):
        checked = _check_id(report, f"{where}.verifies[{index}]", candidate)
        if checked is not None and checked not in targets:
            targets.append(checked)
    return targets


def _validate_task_paths(
    report: Report, where: str, item: dict, workspace: Path | None
) -> tuple[list[Path], list[Path]]:
    """Check declared paths and return absolute write and read paths.

    Paths that cannot be resolved safely are reported and dropped, so ownership
    comparison later works on real absolute paths and never raises.
    """
    write_abs: list[Path] = []
    read_abs: list[Path] = []
    write_paths = _path_list(report, f"{where}.write_paths", item.get("write_paths", []))
    read_paths = _path_list(report, f"{where}.read_paths", item.get("read_paths", []))
    if workspace is None:
        return write_abs, read_abs
    for path in write_paths:
        resolved, problem = resolve_within_safe(workspace, path)
        if resolved is None:
            report.error(f"{where}.write_paths", f"unsafe write path: {problem}")
        else:
            write_abs.append(resolved)
    for path in read_paths:
        # Absolute reads may point outside the workspace; relative ones may not.
        if Path(path).is_absolute():
            resolved, problem = resolve_absolute_safe(path)
            if resolved is None:
                report.error(f"{where}.read_paths", f"unusable read path: {problem}")
            else:
                read_abs.append(resolved)
            continue
        resolved, problem = resolve_within_safe(workspace, path)
        if resolved is None:
            report.error(f"{where}.read_paths", f"unsafe read path: {problem}")
        else:
            read_abs.append(resolved)
    return write_abs, read_abs


def _validate_task_acceptance(
    report: Report, where: str, item: dict, requirement_ids: set[str]
) -> None:
    """Validate the acceptance criteria attached to one task."""
    acceptance = _check_list(report, f"{where}.acceptance", item.get("acceptance", []))
    if acceptance is None:
        return
    state = item.get("state")
    task_type = item.get("type")
    if not acceptance and (state != "queued" or task_type in ("implementation", "integration")):
        report.error(
            f"{where}.acceptance",
            "at least one acceptance criterion is required for non-queued or build work",
        )
    for index, criterion in enumerate(acceptance):
        cwhere = f"{where}.acceptance[{index}]"
        entry = _check_dict(report, cwhere, criterion)
        if entry is None:
            continue
        _warn_unknown_keys(
            report, cwhere, entry, ("id", "criterion", "requirement_ids", "required")
        )
        _check_id(report, f"{cwhere}.id", entry.get("id"))
        _check_str(report, f"{cwhere}.criterion", entry.get("criterion"))
        refs = _id_list(
            report, f"{cwhere}.requirement_ids", entry.get("requirement_ids", [])
        )
        if not refs:
            report.error(
                f"{cwhere}.requirement_ids",
                "acceptance must map to at least one user requirement id",
            )
        for ref in refs:
            if ref not in requirement_ids:
                report.error(
                    f"{cwhere}.requirement_ids", f"unknown requirement reference {ref!r}"
                )
        if "required" in entry and not isinstance(entry["required"], bool):
            report.error(f"{cwhere}.required", "must be a boolean when present")


def _validate_artifacts(
    report: Report,
    artifacts: object,
    tasks: dict[str, dict],
    *,
    verify_files: bool,
) -> dict[str, dict]:
    """Validate artifact records and optionally confirm their recorded hashes."""
    if artifacts is None:
        return {}
    items = _check_list(report, "ledger.artifacts", artifacts, required=False)
    if items is None:
        return {}
    result: dict[str, dict] = {}
    for index, artifact in enumerate(items):
        where = f"ledger.artifacts[{index}]"
        entry = _check_dict(report, where, artifact)
        if entry is None:
            continue
        _warn_unknown_keys(report, where, entry, ("id", "task_id", "path", "sha256", "kind"))
        ident = _check_id(report, f"{where}.id", entry.get("id"))
        task_id = _check_id(report, f"{where}.task_id", entry.get("task_id"))
        path = _check_str(report, f"{where}.path", entry.get("path"))
        digest = _check_str(report, f"{where}.sha256", entry.get("sha256"))
        if digest is not None and not SHA256_RE.match(digest):
            report.error(f"{where}.sha256", "must be a 64-character lowercase hex digest")
        if task_id is not None and task_id not in tasks:
            report.error(f"{where}.task_id", f"unknown task reference {task_id!r}")
        _check_str(report, f"{where}.kind", entry.get("kind"), required=False)
        record = dict(entry)
        record["_index"] = index
        if path is not None and task_id in tasks:
            workspace = tasks[task_id].get("_workspace")
            if workspace is not None:
                resolved, problem = resolve_within_safe(workspace, path)
                if resolved is None:
                    report.error(f"{where}.path", f"unsafe artifact path: {problem}")
                else:
                    record["_resolved"] = resolved
                    if verify_files:
                        _check_artifact_content(report, where, resolved, digest)
        if ident is None:
            continue
        if ident in result:
            report.error(f"{where}.id", f"duplicate artifact id {ident!r}")
            continue
        result[ident] = record
    return result


def _check_artifact_content(
    report: Report, where: str, resolved: Path, digest: str | None
) -> None:
    """Confirm a declared artifact digest against the bytes actually on disk."""
    if not resolved.is_file():
        report.error(f"{where}.path", f"artifact file is missing: {resolved}")
        return
    if digest is None or not SHA256_RE.match(digest):
        return
    actual = file_sha256(resolved)
    if actual != digest:
        report.error(
            f"{where}.sha256",
            f"stale artifact hash: recorded {digest[:12]}... but the file on disk "
            f"hashes to {actual[:12]}...",
        )


def _validate_task_references(
    report: Report,
    tasks: dict[str, dict],
    *,
    artifact_ids: dict[str, dict],
) -> None:
    """Check every cross-reference a task record makes."""
    acceptance_ids: dict[str, str] = {}
    for task_id, task in tasks.items():
        where = f"ledger.tasks[{task['_index']}]"
        for criterion in task.get("acceptance", []) or []:
            if not isinstance(criterion, dict):
                continue
            cid = criterion.get("id")
            if not isinstance(cid, str):
                continue
            if cid in acceptance_ids:
                report.error(
                    f"{where}.acceptance",
                    f"acceptance id {cid!r} is already used by task {acceptance_ids[cid]!r}",
                )
                continue
            acceptance_ids[cid] = task_id
        for dep in task.get("depends_on", []) or []:
            if isinstance(dep, str) and dep not in tasks:
                report.error(f"{where}.depends_on", f"unknown dependency {dep!r}")
        for target in task.get("_verifies", []):
            if target not in tasks:
                report.error(f"{where}.verifies", f"unknown verified task {target!r}")

    evidence_ids: dict[str, str] = {}
    for task_id, task in tasks.items():
        where = f"ledger.tasks[{task['_index']}]"
        for index, entry in enumerate(task.get("evidence", []) or []):
            if not isinstance(entry, dict):
                continue
            ewhere = f"{where}.evidence[{index}]"
            eid = entry.get("id")
            if isinstance(eid, str):
                if eid in evidence_ids:
                    report.error(ewhere, f"duplicate evidence id {eid!r}")
                else:
                    evidence_ids[eid] = task_id
            for ref in entry.get("acceptance", []) or []:
                if isinstance(ref, str) and ref not in acceptance_ids:
                    report.error(
                        f"{ewhere}.acceptance", f"unknown acceptance reference {ref!r}"
                    )
            artifact = entry.get("artifact")
            if artifact is not None and (
                not isinstance(artifact, str) or artifact not in artifact_ids
            ):
                report.error(f"{ewhere}.artifact", f"unknown artifact reference {artifact!r}")


def _validate_dependency_graph(report: Report, tasks: dict[str, dict]) -> None:
    """Reject dependency cycles, including self-dependencies."""
    colour: dict[str, int] = {}

    def visit(node: str, stack: list[str]) -> None:
        colour[node] = 1
        stack.append(node)
        for dep in tasks[node].get("depends_on", []) or []:
            if not isinstance(dep, str) or dep not in tasks:
                continue
            if colour.get(dep) == 1:
                index = stack.index(dep) if dep in stack else 0
                cycle = stack[index:] + [dep]
                report.error(
                    "ledger.tasks",
                    "dependency cycle: " + " -> ".join(cycle),
                )
                continue
            if colour.get(dep, 0) == 0:
                visit(dep, stack)
        stack.pop()
        colour[node] = 2

    for task_id in tasks:
        if colour.get(task_id, 0) == 0:
            visit(task_id, [])


def _validate_state_consistency(report: Report, tasks: dict[str, dict]) -> None:
    """Check that a task's state agrees with its dependencies and actor.

    State is a claim about the world. A task that has left ``queued`` implies its
    dependencies were satisfied, an ``accepted`` task implies accepted
    dependencies, a verifier implies a frozen target, and one actor cannot be
    inside two tasks at once without making capacity accounting meaningless.
    """
    for task_id, task in sorted(tasks.items()):
        state = task.get("state")
        where = f"task {task_id!r}"
        dependencies = [d for d in (task.get("depends_on") or []) if isinstance(d, str)]
        if state in IN_FLIGHT_STATES + ("accepted",):
            unfinished = [
                dep for dep in dependencies if tasks.get(dep, {}).get("state") != "accepted"
            ]
            if unfinished:
                report.error(
                    where,
                    f"state {state!r} while dependenc"
                    f"{'y' if len(unfinished) == 1 else 'ies'} "
                    + ", ".join(f"{dep!r}={tasks.get(dep, {}).get('state')!r}" for dep in unfinished)
                    + " is not accepted",
                )
        if task.get("type") == "verification" and state in IN_FLIGHT_STATES + ("accepted",):
            for target in task.get("_verifies", []):
                target_state = tasks.get(target, {}).get("state")
                if target_state not in FROZEN_TARGET_STATES:
                    report.error(
                        where,
                        f"verifier is {state!r} but its target {target!r} is {target_state!r}; "
                        "verification may only read a frozen ready_for_review or accepted target",
                    )

    by_actor: dict[str, list[str]] = {}
    for task_id, task in tasks.items():
        if task.get("state") in IN_FLIGHT_STATES and task.get("_actor"):
            by_actor.setdefault(task["_actor"], []).append(task_id)
    for actor, owned in sorted(by_actor.items()):
        if len(owned) > 1:
            report.error(
                "ledger.tasks",
                f"actor {actor!r} owns {len(owned)} in-flight tasks "
                f"({', '.join(sorted(owned))}); one worker can only be inside one task, "
                "otherwise slot accounting is ambiguous",
            )


def _validate_acceptance(
    report: Report, tasks: dict[str, dict], *, requirement_ids: set[str], mode: str = "planning"
) -> None:
    """Check requirement coverage and the evidence required by acceptance.

    Planning mode warns about requirements that no acceptance criterion maps to,
    because the ledger is still being written. Final mode requires every
    requirement to be covered by a task that is actually ``accepted``, and
    requires the whole ledger to be finished.
    """
    covered: set[str] = set()
    covered_by_accepted: set[str] = set()
    for task_id, task in tasks.items():
        where = f"task {task_id!r}"
        for criterion in task.get("acceptance", []) or []:
            if not isinstance(criterion, dict):
                continue
            for ref in criterion.get("requirement_ids", []) or []:
                if isinstance(ref, str):
                    covered.add(ref)
                    if task.get("state") == "accepted":
                        covered_by_accepted.add(ref)
        if task.get("state") == "accepted" and not task.get("acceptance"):
            report.error(where, "an accepted task must carry acceptance criteria")
    if mode == "final":
        for requirement in sorted(requirement_ids - covered_by_accepted):
            if requirement in covered:
                report.error(
                    "ledger.requirements",
                    f"requirement {requirement!r} is mapped only by unaccepted tasks; "
                    "completion needs an accepted task covering it",
                )
            else:
                report.error(
                    "ledger.requirements",
                    f"requirement {requirement!r} has no acceptance criterion mapping to it",
                )
        unfinished = sorted(
            f"{tid} ({task.get('state')})"
            for tid, task in tasks.items()
            if task.get("state") in UNFINISHED_STATES
        )
        if unfinished:
            report.error(
                "ledger.tasks",
                "final validation requires every task to be accepted; still open: "
                + ", ".join(unfinished),
            )
    else:
        for requirement in sorted(requirement_ids - covered):
            report.warn(
                "ledger.requirements",
                f"requirement {requirement!r} has no acceptance criterion mapping to it yet",
            )


def _validate_ownership_conflicts(
    report: Report, tasks: dict[str, dict], resource_kinds: dict[str, str]
) -> None:
    """Reject conflicting in-flight ownership and exclusive-resource reuse.

    Paths are compared as absolute, symlink-resolved locations, so two nested
    workspace roots that name the same file conflict, and a write to a directory
    conflicts with a write to anything beneath it. A reader conflicts with a
    writer of the same location, with one documented exception: a verifier may
    read the frozen ``ready_for_review`` revision of the task it verifies.
    """
    holders = sorted(
        (tid for tid, task in tasks.items() if task.get("state") in WORKSPACE_HOLD_STATES)
    )
    for left_index, left_id in enumerate(holders):
        left = tasks[left_id]
        for right_id in holders[left_index + 1 :]:
            right = tasks[right_id]
            conflict = _path_conflict(left, right)
            if conflict is not None:
                report.error(
                    "ledger.tasks",
                    f"ownership conflict between {left_id!r} and {right_id!r}: {conflict}",
                )
            shared_exclusive = _shared_exclusive(left, right, resource_kinds)
            if shared_exclusive:
                report.error(
                    "ledger.tasks",
                    f"exclusive resource conflict between {left_id!r} and {right_id!r}: "
                    + ", ".join(shared_exclusive),
                )


def _path_conflict(left: dict, right: dict) -> str | None:
    """Describe a write/write or read/write conflict between two in-flight tasks."""
    for a in left.get("_write_abs", []):
        for b in right.get("_write_abs", []):
            if paths_overlap(a, b):
                return f"both tasks write overlapping paths ({a} and {b})"
    for reader, writer in ((left, right), (right, left)):
        if _frozen_read_exception(reader, writer):
            continue
        for a in reader.get("_read_abs", []):
            for b in writer.get("_write_abs", []):
                if paths_overlap(a, b):
                    return (
                        f"read path {a} of {reader.get('id')!r} overlaps a write of "
                        f"{writer.get('id')!r} ({b})"
                    )
    return None


def _frozen_read_exception(reader: dict, writer: dict) -> bool:
    """True when a verifier reads the frozen ready_for_review revision it verifies."""
    return (
        reader.get("type") == "verification"
        and writer.get("id") in (reader.get("_verifies") or [])
        and writer.get("state") == "ready_for_review"
    )


def _validate_evidence(
    report: Report,
    tasks: dict[str, dict],
    artifact_ids: dict[str, dict],
    *,
    verify_files: bool,
) -> None:
    """Check evidence shapes, hash freshness, and accepted-task requirements."""
    for task_id, task in sorted(tasks.items()):
        where = f"task {task_id!r}"
        for index, entry in enumerate(task.get("evidence", []) or []):
            ewhere = f"{where} evidence[{index}]"
            item = _check_dict(report, ewhere, entry)
            if item is None:
                continue
            _warn_unknown_keys(
                report, ewhere, item, ("id", "acceptance", "command", "exit_status", "status", "artifact", "notes")
            )
            _check_id(report, f"{ewhere}.id", item.get("id"))
            _check_str(report, f"{ewhere}.command", item.get("command"), required=False)
            status = item.get("status")
            if status not in EVIDENCE_STATUSES:
                report.error(f"{ewhere}.status", f"must be one of {', '.join(EVIDENCE_STATUSES)}")
            exit_status = item.get("exit_status")
            if exit_status is not None and not _is_int(exit_status):
                report.error(f"{ewhere}.exit_status", "must be an integer or null")
            _check_str(report, f"{ewhere}.notes", item.get("notes"), required=False)
            artifact = item.get("artifact")
            if artifact is not None and (not isinstance(artifact, str) or not artifact):
                report.error(f"{ewhere}.artifact", "must be an artifact id or null")
            if status in ("pass", "fail"):
                command = item.get("command")
                if not isinstance(command, str) or not command.strip():
                    report.error(
                        f"{ewhere}.command",
                        f"a {status} evidence row must record what was observed or run; "
                        "a bare status is not evidence",
                    )
            if status == "pass":
                has_artifact = isinstance(artifact, str) and bool(artifact)
                if not has_artifact and not _is_int(exit_status):
                    report.error(
                        f"{ewhere}",
                        "a passing evidence row must bind to a hashed artifact or record an "
                        "integer exit status",
                    )
            if isinstance(artifact, str) and artifact in artifact_ids:
                artifact_ids[artifact].setdefault("_used_by", []).append(task_id)

        if task.get("state") != "accepted":
            continue
        evidence = [e for e in (task.get("evidence", []) or []) if isinstance(e, dict)]
        closure_ids = _dependency_closure(tasks, task_id)
        closure_artifacts = {
            aid for aid, art in artifact_ids.items() if art.get("task_id") in closure_ids
        }
        if verify_files:
            bound = {
                aid
                for aid in closure_artifacts
                if _artifact_hash_is_current(artifact_ids[aid])
            }
        else:
            # Shape-only mode: hashes were not recomputed, so any declared
            # closure artifact counts as the binding target.
            bound = set(closure_artifacts)
        for criterion in task.get("acceptance", []) or []:
            if not isinstance(criterion, dict):
                continue
            if criterion.get("required", True) is False:
                continue
            cid = criterion.get("id")
            matching = [
                e
                for e in evidence
                if isinstance(cid, str) and cid in (e.get("acceptance", []) or [])
            ]
            passing = [e for e in matching if _evidence_is_passing(e)]
            if not passing:
                if matching:
                    report.error(
                        where,
                        f"accepted task has no passing evidence for required acceptance "
                        f"criterion {cid!r}; recorded evidence is failing",
                    )
                else:
                    report.error(
                        where,
                        f"accepted task is missing required evidence for acceptance "
                        f"criterion {cid!r}",
                    )
        if closure_artifacts:
            artifact_bound = [
                e
                for e in evidence
                if _evidence_is_passing(e) and e.get("artifact") in bound
            ]
            if not artifact_bound:
                report.error(
                    where,
                    "accepted task has no artifact-bound passing evidence, but its "
                    "dependency closure contains hashed artifacts",
                )


def _evidence_is_passing(entry: dict) -> bool:
    """True when an evidence row is a substantive pass, not an empty claim.

    A bare ``{"status": "pass"}`` is not evidence: the row must say what was
    observed and bind it to something concrete, either a hashed artifact or a
    recorded process status.
    """
    if entry.get("status") != "pass":
        return False
    if entry.get("exit_status") not in (None, 0):
        return False
    command = entry.get("command")
    if not isinstance(command, str) or not command.strip():
        return False
    artifact = entry.get("artifact")
    if isinstance(artifact, str) and artifact:
        return True
    return _is_int(entry.get("exit_status"))


def _artifact_hash_is_current(artifact: dict) -> bool:
    """True when a recorded artifact digest matches the bytes on disk."""
    resolved = artifact.get("_resolved")
    digest = artifact.get("sha256")
    if resolved is None or not resolved.is_file() or not isinstance(digest, str):
        return False
    return file_sha256(resolved) == digest


def _dependency_closure(tasks: dict[str, dict], task_id: str) -> set[str]:
    """Return ``task_id`` plus every task it transitively depends on or verifies.

    ``verifies`` is included deliberately. A verifier reviews a frozen revision
    of its target even when it declares no ``depends_on``, so the target's
    artifacts belong in the verifier's acceptance snapshot and a later change to
    the target invalidates the verifier's acceptance too.
    """
    seen: set[str] = set()
    stack = [task_id]
    while stack:
        current = stack.pop()
        if current in seen or current not in tasks:
            continue
        seen.add(current)
        for field in ("depends_on", "verifies"):
            linked = tasks[current].get(field)
            if isinstance(linked, str):
                stack.append(linked)
            elif isinstance(linked, list):
                stack.extend(d for d in linked if isinstance(d, str))
    return seen


def _validate_reviews(
    report: Report,
    tasks: dict[str, dict],
    artifact_ids: dict[str, dict],
    *,
    verify_files: bool,
) -> None:
    """Check review decisions, self-approval, independence, and hash snapshots."""
    for task_id, task in sorted(tasks.items()):
        where = f"task {task_id!r}"
        review = task.get("review")
        if review is None:
            if task.get("state") == "accepted":
                report.error(where, "an accepted task must carry a review decision")
            continue
        item = _check_dict(report, f"{where} review", review)
        if item is None:
            continue
        _warn_unknown_keys(
            report,
            f"{where} review",
            item,
            (
                "reviewer_actor",
                "reviewer_model",
                "decision",
                "at",
                "notes",
                "verified_hashes",
                "independent",
                "artifact_free",
            ),
        )
        reviewer = _check_id(report, f"{where} review.reviewer_actor", item.get("reviewer_actor"))
        _check_str(report, f"{where} review.reviewer_model", item.get("reviewer_model"), required=False)
        decision = item.get("decision")
        if decision not in REVIEW_DECISIONS:
            report.error(
                f"{where} review.decision",
                f"must be one of {', '.join(REVIEW_DECISIONS)}",
            )
        _check_str(report, f"{where} review.at", item.get("at"), required=False)
        _check_str(report, f"{where} review.notes", item.get("notes"), required=False)
        if "independent" in item and not isinstance(item["independent"], bool):
            report.error(f"{where} review.independent", "must be a boolean when present")
        if reviewer is not None and reviewer == task.get("_actor"):
            report.error(
                where,
                f"self-approval rejected: reviewer {reviewer!r} is also the task owner",
            )
        state = task.get("state")
        if state == "accepted" and decision != "accept":
            report.error(
                where,
                f"an accepted task needs review.decision 'accept'; recorded {decision!r}",
            )
        if decision == "accept" and state != "accepted":
            report.warn(
                where, f"review decision is 'accept' but task state is {state!r}"
            )

        hashes = item.get("verified_hashes")
        artifact_free = item.get("artifact_free", False)
        if not isinstance(artifact_free, bool):
            report.error(f"{where} review.artifact_free", "must be a boolean when present")
            artifact_free = False
        if hashes is None:
            if decision == "accept":
                report.error(
                    f"{where} review.verified_hashes",
                    "an accepting review must snapshot the artifact hashes it verified",
                )
            continue
        mapping = _check_dict(report, f"{where} review.verified_hashes", hashes)
        if mapping is None:
            continue
        closure = _dependency_closure(tasks, task_id)
        in_closure = {
            aid for aid, art in artifact_ids.items() if art.get("task_id") in closure
        }
        own_artifacts = {
            aid for aid, art in artifact_ids.items() if art.get("task_id") == task_id
        }
        task_type = task.get("type")
        if decision == "accept":
            if task_type in ("implementation", "integration") and not own_artifacts:
                report.error(
                    f"{where} review.verified_hashes",
                    f"an accepted {task_type} task must declare at least one hashed "
                    "artifact of its own; there is nothing otherwise to version-bind",
                )
            if task_type == "verification" and not in_closure:
                report.error(
                    f"{where} review.verified_hashes",
                    "an accepted verification task must snapshot hashed targets; none "
                    "of the tasks it verifies declares an artifact",
                )
            if artifact_free and task_type != "research":
                report.error(
                    f"{where} review.artifact_free",
                    f"only research tasks may be artifact_free; {task_type} work must "
                    "declare a hashed result (a captured evidence note is enough)",
                )
            if artifact_free and in_closure:
                report.error(
                    f"{where} review.artifact_free",
                    "review.artifact_free is true but the dependency closure contains "
                    "hashed artifacts that must be snapshotted",
                )
            if artifact_free and mapping:
                report.error(
                    f"{where} review.verified_hashes",
                    "an artifact_free acceptance must not record verified hashes",
                )
            if not artifact_free and not in_closure:
                report.error(
                    f"{where} review.artifact_free",
                    "this acceptance covers no hashed artifact; a research task may set "
                    "review.artifact_free to true, everything else must declare one",
                )
        for artifact_id, digest in mapping.items():
            if artifact_id not in artifact_ids:
                report.error(
                    f"{where} review.verified_hashes",
                    f"unknown artifact reference {artifact_id!r}",
                )
                continue
            if not isinstance(digest, str) or not SHA256_RE.match(digest):
                report.error(
                    f"{where} review.verified_hashes.{artifact_id}",
                    "must be a 64-character lowercase hex digest",
                )
                continue
            artifact = artifact_ids[artifact_id]
            if artifact_id not in in_closure:
                report.warn(
                    f"{where} review.verified_hashes.{artifact_id}",
                    "artifact is outside this task's dependency closure",
                )
            if not verify_files:
                continue
            resolved = artifact.get("_resolved")
            if resolved is None:
                continue
            if not resolved.is_file():
                report.error(
                    f"{where} review.verified_hashes.{artifact_id}",
                    f"artifact file is missing: {resolved}",
                )
                continue
            actual = file_sha256(resolved)
            if actual != digest:
                if artifact_id in in_closure:
                    report.error(
                        where,
                        f"stale acceptance: upstream artifact {artifact_id!r} changed since "
                        f"acceptance (recorded {digest[:12]}..., now {actual[:12]}...)",
                    )
                else:
                    report.error(
                        f"{where} review.verified_hashes.{artifact_id}",
                        f"recorded hash does not match the file on disk "
                        f"({digest[:12]}... != {actual[:12]}...)",
                    )
        if decision == "accept":
            missing = sorted(in_closure - set(mapping))
            if missing:
                report.error(
                    f"{where} review.verified_hashes",
                    "accepting review omitted artifacts from its dependency closure: "
                    + ", ".join(missing),
                )

    _validate_independent_verification(report, tasks)


def _validate_independent_verification(report: Report, tasks: dict[str, dict]) -> None:
    """Important work needs a verifier that is not the author."""
    for task_id, task in sorted(tasks.items()):
        if task.get("_importance") != "important":
            continue
        for verifier_id, verifier in sorted(tasks.items()):
            if task_id not in (verifier.get("_verifies") or []):
                continue
            if verifier.get("type") != "verification":
                report.error(
                    f"task {verifier_id!r}",
                    f"declares verifies={task_id!r} but its type is {verifier.get('type')!r}",
                )
                continue
            author = task.get("_actor")
            verifier_actor = verifier.get("_actor")
            if verifier_actor is not None and verifier_actor == author:
                report.error(
                    f"task {verifier_id!r}",
                    f"verification of important task {task_id!r} must use a different actor "
                    f"than the author {author!r}",
                )
        if task.get("state") != "accepted":
            continue
        independent = [
            vid
            for vid, verifier in tasks.items()
            if task_id in (verifier.get("_verifies") or [])
            and verifier.get("state") == "accepted"
            and verifier.get("_actor") is not None
            and verifier.get("_actor") != task.get("_actor")
        ]
        if not independent:
            report.error(
                f"task {task_id!r}",
                "an important change cannot be accepted without independent verification by "
                "a different actor",
            )


def _validate_limits_usage(report: Report, tasks: dict[str, dict], limits: dict) -> None:
    """Reject states that already exceed a supplied user limit."""
    max_concurrent = limits.get("max_concurrent_workers")
    running = sum(1 for task in tasks.values() if task.get("state") in SLOT_STATES)
    if isinstance(max_concurrent, int) and running > max_concurrent:
        report.error(
            "ledger.tasks",
            f"{running} running tasks exceed limits.max_concurrent_workers={max_concurrent}",
        )
    max_total = limits.get("max_total_workers")
    dispatched = sum(1 for task in tasks.values() if task.get("state") != "queued")
    if isinstance(max_total, int) and dispatched > max_total:
        report.error(
            "ledger.tasks",
            f"{dispatched} dispatched tasks exceed limits.max_total_workers={max_total}",
        )


def _validate_observations(report: Report, observations: object) -> None:
    """Validate optional observed metrics without inventing values."""
    if observations is None:
        return
    item = _check_dict(report, "ledger.observations", observations)
    if item is None:
        return
    for key, value in item.items():
        if value is None or isinstance(value, (str, bool, int, float, list, dict)):
            continue
        report.error(
            f"ledger.observations.{key}",
            f"must be a JSON value or null, got {type(value).__name__}",
        )


# --------------------------------------------------------------------------
# scheduling
# --------------------------------------------------------------------------


def _transitive_dependents(tasks: dict[str, dict]) -> dict[str, int]:
    """Count how many tasks each task transitively unblocks."""
    dependents: dict[str, set[str]] = {tid: set() for tid in tasks}
    for tid, task in tasks.items():
        for dep in task.get("depends_on", []) or []:
            if isinstance(dep, str) and dep in dependents:
                dependents[dep].add(tid)
    counts: dict[str, int] = {}
    for tid in tasks:
        seen: set[str] = set()
        stack = list(dependents[tid])
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            stack.extend(dependents.get(current, ()))
        counts[tid] = len(seen)
    return counts


def schedule(
    ledger: dict,
    *,
    base_dir: Path,
    host_capacity: int,
    other_active: int = 0,
    verify_files: bool = True,
) -> dict:
    """Return a deterministic, read-only set of next-action suggestions.

    The function never dispatches anything and never writes. It reports why each
    candidate was suggested or deferred so the scheduling agent can decide.
    """
    report = validate_ledger(ledger, base_dir=base_dir, verify_files=verify_files)
    if not report.ok:
        return {
            "ok": False,
            "errors": list(report.errors),
            "warnings": list(report.warnings),
            "provenance_note": PROVENANCE_NOTE,
        }

    # Reuse exactly the records the validator parsed and checked.
    tasks = report.tasks
    limits = report.limits
    artifacts = report.artifacts
    resource_kinds = report.resources

    counts: dict[str, int] = {state: 0 for state in TASK_STATES}
    for task in tasks.values():
        state = task.get("state")
        if state in counts:
            counts[state] += 1

    running = [tid for tid, t in tasks.items() if t.get("state") in SLOT_STATES]
    review_pending = [tid for tid, t in tasks.items() if t.get("state") == "ready_for_review"]
    research = [tid for tid, t in tasks.items() if t.get("state") == "needs_research"]
    failed = {tid for tid, t in tasks.items() if t.get("state") in ("failed", "blocked")}
    accepted = {tid for tid, t in tasks.items() if t.get("state") == "accepted"}

    # Workers are counted by actor, not by task: one resumed worker is one agent
    # slot even when it is handed a second task. An actor may only be inside one
    # in-flight task, which the validator enforces, so this stays unambiguous.
    running_actors = {
        tasks[tid]["_actor"] for tid in running if tasks[tid].get("_actor")
    }
    dispatched_actors = {
        task["_actor"]
        for task in tasks.values()
        if task.get("state") != "queued" and task.get("_actor")
    }
    reviewer_slot = 1 if review_pending else 0
    worker_limit = limits.get("max_concurrent_workers")
    total_limit = limits.get("max_total_workers")

    # The host budget covers every agent: other active jobs, the reserved
    # reviewer, and running workers come off it before a worker can start.
    host_slots_free = max(
        0, host_capacity - other_active - reviewer_slot - len(running_actors)
    )
    if isinstance(worker_limit, int):
        free_slots = max(0, min(host_slots_free, worker_limit - len(running_actors)))
    else:
        free_slots = host_slots_free
    additional_workers_allowed = (
        None
        if not isinstance(total_limit, int)
        else max(0, total_limit - len(dispatched_actors))
    )

    backlog_limit = limits.get("review_backlog_limit")
    backpressure = isinstance(backlog_limit, int) and len(review_pending) >= backlog_limit > 0

    suggestions: list[dict] = []
    deferred: list[dict] = []
    blocked_tasks: list[dict] = []

    if review_pending:
        for tid in sorted(review_pending):
            suggestions.append(
                {
                    "task_id": tid,
                    "kind": "review",
                    "reason": "task is ready_for_review and its reviewer slot is reserved",
                }
            )
    for tid in sorted(research):
        suggestions.append(
            {
                "task_id": tid,
                "kind": "review",
                "reason": "task reported needs_research; the reviewer decides the next question "
                "or supplies the missing fact",
            }
        )

    dependents = _transitive_dependents(tasks)
    holders = [
        t for t in tasks.values() if t.get("state") in WORKSPACE_HOLD_STATES
    ]
    candidates: list[tuple] = []
    for tid, task in tasks.items():
        state = task.get("state")
        if state not in ("queued", "needs_changes"):
            continue
        bad_deps = [
            dep
            for dep in task.get("depends_on", []) or []
            if isinstance(dep, str) and dep in failed
        ]
        if bad_deps:
            blocked_tasks.append(
                {
                    "task_id": tid,
                    "reason": "dependency did not complete: " + ", ".join(sorted(bad_deps)),
                }
            )
            continue
        waiting = [
            dep
            for dep in task.get("depends_on", []) or []
            if isinstance(dep, str) and dep not in accepted
        ]
        if waiting:
            deferred.append(
                {
                    "task_id": tid,
                    "reason": "waiting for dependency acceptance: "
                    + ", ".join(sorted(waiting)),
                }
            )
            continue
        if task.get("type") == "verification":
            unfrozen = [
                target
                for target in task.get("_verifies", [])
                if tasks.get(target, {}).get("state") not in FROZEN_TARGET_STATES
            ]
            if unfrozen:
                deferred.append(
                    {
                        "task_id": tid,
                        "reason": "verification target not frozen yet: "
                        + ", ".join(
                            f"{target}={tasks.get(target, {}).get('state')!r}"
                            for target in unfrozen
                        ),
                    }
                )
                continue
        conflict = _first_hold_conflict(task, tid, holders, resource_kinds)
        if conflict is not None:
            deferred.append({"task_id": tid, "reason": conflict})
            continue
        candidates.append((tid, task))

    candidates.sort(
        key=lambda pair: (-dependents.get(pair[0], 0), _depth(pair[1]), pair[0])
    )

    selected: list[tuple[str, dict]] = []
    new_actors = 0
    for tid, task in candidates:
        kind = "rework" if task.get("state") == "needs_changes" else str(task.get("type"))
        if backpressure and kind not in BACKLOG_EXEMPT_TYPES:
            deferred.append(
                {
                    "task_id": tid,
                    "reason": f"holding new {kind} work while review backlog "
                    f"({len(review_pending)}) is at or above "
                    f"limits.review_backlog_limit={backlog_limit}; "
                    "verification and research stay dispatchable so the backlog "
                    "can be cleared",
                }
            )
            continue
        if len(selected) >= free_slots:
            deferred.append(
                {"task_id": tid, "reason": f"ready but no free worker slot ({free_slots} free)"}
            )
            continue
        actor = task.get("_actor")
        starts_new_worker = actor is None or actor not in dispatched_actors
        if (
            starts_new_worker
            and additional_workers_allowed is not None
            and new_actors >= additional_workers_allowed
        ):
            deferred.append(
                {
                    "task_id": tid,
                    "reason": "dispatching this task would exceed "
                    f"limits.max_total_workers={total_limit} "
                    f"({len(dispatched_actors) + new_actors} workers already used)",
                }
            )
            continue
        conflict = _selected_conflict(task, tid, selected, holders, resource_kinds)
        if conflict is not None:
            deferred.append({"task_id": tid, "reason": conflict})
            continue
        if starts_new_worker:
            new_actors += 1
        selected.append((tid, task))
        unblocks = dependents.get(tid, 0)
        suggestions.append(
            {
                "task_id": tid,
                "kind": kind,
                "reason": f"ready to dispatch; unblocks {unblocks} dependent task(s)",
            }
        )

    return {
        "ok": True,
        "warnings": list(report.warnings),
        "capacity": {
            "host_capacity": host_capacity,
            "other_active_supplied": other_active,
            "reviewer_slot_reserved": reviewer_slot,
            "workers_running": len(running_actors),
            "running_tasks": len(running),
            "host_slots_free_after_reservations": host_slots_free,
            "limit_max_concurrent_workers": worker_limit,
            "worker_slots_free": free_slots,
            "free_slots": free_slots,
            "dispatched_workers": len(dispatched_actors),
            "limit_max_total_workers": total_limit,
            "additional_workers_allowed": additional_workers_allowed,
            "note": CAPACITY_NOTE,
        },
        "counts": counts,
        "review_backlog": len(review_pending),
        "backpressure": backpressure,
        "suggestions": suggestions,
        "deferred": sorted(deferred, key=lambda item: item["task_id"]),
        "blocked_tasks": sorted(blocked_tasks, key=lambda item: item["task_id"]),
        "artifacts_tracked": len(artifacts),
        "budget": {
            "usd": limits.get("budget_usd"),
            "status": "supplied" if _is_number(limits.get("budget_usd")) else "unknown",
            "note": BUDGET_NOTE,
        },
        "provenance_note": PROVENANCE_NOTE,
    }


def _depth(task: dict) -> int:
    """Deterministic tie-break: number of declared dependencies."""
    deps = task.get("depends_on", []) or []
    return len([d for d in deps if isinstance(d, str)])


def _first_hold_conflict(
    task: dict, task_id: str, holders: list[dict], resource_kinds: dict[str, str]
) -> str | None:
    """Return a reason string when a candidate collides with an in-flight task."""
    for other in holders:
        if other.get("id") == task_id:
            continue
        conflict = _path_conflict(task, other)
        if conflict is not None:
            return (
                f"{conflict}; in-flight task {other.get('id')!r} "
                f"({other.get('state')}) holds it"
            )
        shared = _shared_exclusive(task, other, resource_kinds)
        if shared:
            return (
                f"exclusive resource {', '.join(shared)} is held by in-flight task "
                f"{other.get('id')!r} ({other.get('state')})"
            )
    return None


def _selected_conflict(
    task: dict,
    task_id: str,
    selected: list[tuple[str, dict]],
    holders: list[dict],
    resource_kinds: dict[str, str],
) -> str | None:
    """Return a reason when a candidate collides with a holder or an already chosen peer.

    Two queued writers can be individually clear of every running task and still
    collide with each other, so selected candidates are checked as a set.
    """
    conflict = _first_hold_conflict(task, task_id, holders, resource_kinds)
    if conflict is not None:
        return conflict
    for other_id, other in selected:
        peer = _path_conflict(task, other)
        if peer is not None:
            return f"{peer}; already selected in this batch as {other_id!r}"
        shared = _shared_exclusive(task, other, resource_kinds)
        if shared:
            return (
                f"exclusive resource {', '.join(shared)} is already taken by selected "
                f"task {other_id!r}"
            )
    return None


def _shared_exclusive(
    left: dict, right: dict, resource_kinds: dict[str, str]
) -> list[str]:
    """Return exclusive resources both records claim."""
    return sorted(
        {
            r
            for r in left.get("resources", []) or []
            if isinstance(r, str)
            and resource_kinds.get(r, "exclusive") == "exclusive"
            and r in (right.get("resources", []) or [])
        }
    )


# --------------------------------------------------------------------------
# cli
# --------------------------------------------------------------------------


def _print_report(report: Report, as_json: bool) -> None:
    if as_json:
        print(json.dumps(report.to_dict(), indent=2))
        return
    for warning in report.warnings:
        print(f"warning: {warning}")
    for error in report.errors:
        print(f"error: {error}")
    if report.ok:
        print(f"ledger valid ({len(report.warnings)} warning(s))")
    else:
        print(f"ledger invalid ({len(report.errors)} error(s), {len(report.warnings)} warning(s))")
    print(PROVENANCE_NOTE)


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser for this helper."""
    parser = argparse.ArgumentParser(
        prog="workflow.py",
        description=(
            "Read-only task-ledger validation and scheduling. Never dispatches agents, "
            "never executes command strings recorded in the ledger, never writes files."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate", help="validate a ledger and report findings")
    validate.add_argument("--ledger", required=True, help="path to the ledger JSON file")
    validate.add_argument("--json", action="store_true", help="emit the report as JSON")
    validate.add_argument(
        "--mode",
        choices=VALIDATION_MODES,
        default="planning",
        help=(
            "planning (default) treats unmapped requirements as warnings; final "
            "requires every requirement covered by an accepted task and no "
            "unfinished tasks"
        ),
    )
    validate.add_argument(
        "--no-verify-files",
        action="store_true",
        help="skip workspace existence and artifact-hash content checks",
    )

    schedule_cmd = sub.add_parser(
        "schedule", help="suggest the next actions within the supplied capacity"
    )
    schedule_cmd.add_argument("--ledger", required=True, help="path to the ledger JSON file")
    schedule_cmd.add_argument(
        "--host-capacity",
        type=int,
        required=True,
        help="simultaneous agent slots this host actually exposes",
    )
    schedule_cmd.add_argument(
        "--other-active",
        type=int,
        default=0,
        help="active jobs outside this ledger to count against capacity",
    )
    schedule_cmd.add_argument("--json", action="store_true", help="accepted for symmetry; schedule is always JSON")
    schedule_cmd.add_argument(
        "--no-verify-files",
        action="store_true",
        help="skip workspace existence and artifact-hash content checks",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns a process exit code."""
    args = build_parser().parse_args(argv)
    ledger_path = Path(args.ledger)
    try:
        ledger = load_ledger(ledger_path)
    except LedgerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    base_dir = ledger_path.parent
    verify = not args.no_verify_files

    if args.command == "validate":
        report = validate_ledger(
            ledger, base_dir=base_dir, verify_files=verify, mode=args.mode
        )
        _print_report(report, args.json)
        return 0 if report.ok else 1

    if args.host_capacity < 1:
        print("error: --host-capacity must be a positive integer", file=sys.stderr)
        return 2
    if args.other_active < 0:
        print("error: --other-active must be zero or greater", file=sys.stderr)
        return 2
    result = schedule(
        ledger,
        base_dir=base_dir,
        host_capacity=args.host_capacity,
        other_active=args.other_active,
        verify_files=verify,
    )
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
