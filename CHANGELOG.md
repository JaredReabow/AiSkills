# Changelog

## 2026-09-29

- Added `$parallelism <model> <effort>` reviewer selection, including linked
  mentions; repeating it during a task requests a switch without restarting work.
- Reviewer model is selectable at task start, defaults to Sol (`gpt-6-sol`)
  with medium reasoning, and can change during work through an evidence handoff.
- Preserve historical review attribution, ignore superseded approvals, and keep
  independent approved workers running during reviewer replacement.
- Updated invocation metadata, protocol, ledger examples and evaluation scenarios;
  the ledger remains model-agnostic and retains actor-based self-approval checks.

## 2026-09-28

- Main-agent waits now schedule timer events (or a thread heartbeat) to resume
  coordination while subagents work, with deduplication, checkpoint recovery,
  adaptive intervals, completion handling, and explicit stopping conditions.
- Added the DeepSeek + Astra Team skill and invocation metadata.
- Added source-backed design guidance and runtime evaluation scenarios.
- Installation adds instructions only; model selection remains a session setting.
- Main agent now preserves any user-selected model with supported agent tools;
  Astra review and DeepSeek worker routes remain explicit.
- Worker count explicitly scales with independent approved tasks and available
  capacity, with ready jobs dispatched as slots become available.
- Renamed the skill to Parallelism; invoke it with `$parallelism`.

### Task ledger, verification, and benchmark

- Added `scripts/workflow.py`: standard-library ledger validation and read-only
  scheduling. It never spawns agents, never executes a `command` string recorded
  in a ledger, and never writes to the ledger or the workspace.
- Added the ledger schema and commands in `references/ledger.md`, plus a
  self-verifying example in `templates/ledger.example.json`.
- Objectives now carry the user's original request and amendments; requirements
  get stable ids and every acceptance criterion maps to at least one of them.
- Validation rejects malformed shapes, duplicate or unknown ids, dependency
  cycles, incomplete handoffs, workspace escapes through `..` or symlinks,
  overlapping in-flight write ownership and exclusive resources, self-approval,
  failed or missing evidence on accepted work, stale artifact hashes, acceptance
  invalidated by a changed upstream artifact, and states exceeding a user limit.
- Scheduling counts running workers, other active jobs, and a reserved reviewer
  slot; prioritises tasks that unblock more dependents; keeps the reviewer
  runnable while review is pending; and applies bounded review backlog
  backpressure. Budgets stay unknown instead of being estimated.
- Important changes now require independent verification by a worker other than
  the author. A fresh Astra pass is conditional on risk rather than mandatory for
  every task; Astra keeps ownership of acceptance.
- Added `scripts/benchmark.py` and `templates/benchmark/`: a disposable two-module
  Python fixture with split ownership, tests that fail against the stubs, an
  integration check, a fixture-integrity check, and a per-requirement grader.
  No model API or CLI is invoked, and manual observations are recorded separately
  from the grade.
- Added `tests/run_harness.py` as the single harness command, with 95 tests over
  real temporary files: ledger invariants, scheduler behaviour, benchmark
  generation, grading, and tamper detection. It also runs the bundled
  `quick_validate.py` packaging check.
- Shortened `SKILL.md` and moved protocol, ledger, and benchmark detail into
  focused references. Default invocation policy, explicit worker model selection,
  worker ownership rules, lifecycle handling, and the no-automatic-config-change
  boundary are unchanged.

### Correction batch after acceptance review

- Ownership is now a tree: ancestor/descendant write paths collide, and two
  workspace roots that resolve to the same real file collide. Comparisons use
  absolute symlink-resolved paths, and unusable or escaping paths are reported as
  findings instead of raising.
- Reads are checked against writers. A reader conflicts with an in-flight writer
  of the same path, except for the one documented case where a verifier reads the
  frozen `ready_for_review` target it covers.
- The scheduler compares selected candidates against each other, so two queued
  colliding writers can no longer both be dispatched.
- Review backpressure now holds new implementation work only. `verification` and
  `research` stay dispatchable, because they are what clears the backlog; a
  combined verifier can start while two important targets wait for acceptance.
- Verifiers gate on frozen targets, `verifies` accepts a single id or a list,
  and the acceptance snapshot follows `verifies` as well as `depends_on`, so a
  changed target invalidates the verifier's acceptance. A verifier must not
  depend on the target it verifies, which removes the acceptance deadlock.
- Acceptance now requires `review.decision: accept`, evidence with a real command
  and a concrete binding, and a non-empty verified snapshot. An empty status
  assertion is no longer evidence.
- Accepted `implementation` and `integration` tasks must declare their own hashed
  artifact, verification must cover hashed targets, and `artifact_free` is
  restricted to research with genuinely nothing to hash.
- Added state/dependency consistency checks: accepted or in-flight work cannot sit
  behind an unaccepted dependency, verifiers need frozen targets, and one actor
  cannot be inside two in-flight tasks.
- Added `validate --mode final` for completion: every requirement must be covered
  by an accepted task and no task may still be unfinished. Planning mode keeps
  reporting unmapped requirements as warnings.
- Capacity is now explicit: hosts count total agents and subtract `other_active`
  and the reserved reviewer, `max_concurrent_workers` caps running workers
  separately, workers are counted by actor, and `max_total_workers` limits how
  many distinct workers are ever dispatched (rework reuses its existing owner).
- Benchmark generation no longer has a destructive replace option; it refuses
  non-empty destinations and any destination that is the skill package, inside
  it, or an ancestor of it.
- Benchmark grading reads canonical check hashes from the skill package
  (including `tests/__init__.py`) and verifies them before executing anything. A
  missing, malformed, `null`, or tampered manifest fails the grade and marks every
  requirement `not_run` instead of silently continuing.
- Documented that graded commands still execute workspace code with normal host
  permissions, and that the standard-library check only sees static imports.
- `SKILL.md` step 4 no longer claims the validator checks every brief field; it
  lists the machine-checked subset separately from prompt-only fields, and adds
  the rule to run only isolated checks while a peer owns an input.
