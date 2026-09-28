# History

- 2026-09-28: Created the DeepSeek main-agent and Astra review workflow. Adopted
  sibling workers requested by Astra and spawned by DeepSeek. Added explicit
  evidence-based acceptance, ownership, lifecycle, and model-routing checks,
  with primary-source rationale and behavioral evaluation scenarios.
- 2026-09-28: Generalized the main agent to the user's selected model, retaining
  Astra as a separate planner/reviewer and DeepSeek as the parallel worker model.
  Preserved the existing skill identifier and added model-selection scenarios.
- 2026-09-28: Clarified dynamic worker counts, multiple workers per kind of work,
  and refilling available slots. Replaced the role-count illustration with N workers.
- 2026-09-28: Renamed the skill to parallelism and updated the folder, display
  name, and invocation examples at the user's request.
- 2026-09-28: Upgraded the skill at the user's request. Added a standard-library
  task ledger (`scripts/workflow.py`) with read-only validation and capacity-aware
  scheduling, a documented schema and self-verifying example, a disposable
  two-module benchmark fixture with an outcome grader (`scripts/benchmark.py`),
  and one automated harness (`tests/run_harness.py`, 95 invariant tests plus the
  bundled skill validator). Moved detail into `references/protocol.md`,
  `references/ledger.md`, and `references/benchmark.md`; shortened `SKILL.md` and
  preserved its safety, ownership, explicit-model, lifecycle, and no-config-change
  boundaries. Independent verification of important changes now goes to a worker
  other than the author, and a fresh Astra pass is conditional on risk rather than
  mandatory for every task.
- 2026-09-28: Correction batch after acceptance review. Ownership conflicts are
  now checked on absolute symlink-resolved paths, so ancestor/descendant paths and
  nested workspace roots collide; read/write conflicts are rejected while both
  tasks are in flight, with one documented exception for a verifier reading the
  frozen `ready_for_review` target it covers. Path checks no longer raise.
  Scheduling compares selected candidates against each other, keeps verification
  and research dispatchable under review backpressure, gates verifiers until
  targets are frozen, and separates host capacity from worker limits with actor
  based counting plus a total-worker cap. Acceptance now requires an `accept`
  decision, substantive artifact-bound evidence, and a non-empty verified
  snapshot; implementation and integration need their own hashed artifact and
  only research may be `artifact_free`. Added state/dependency consistency checks,
  a `--mode final` completion check, and multi-target `verifies`. The benchmark
  lost its destructive replace option, grades against canonical hashes from the
  skill package including `tests/__init__.py`, and refuses to execute anything
  when the shipped checks are missing or tampered (including a `null` manifest).
- 2026-09-28: Added the requested timer-event waiting policy. The main agent
  schedules and records one wake-up for its task while subagents are running,
  resumes from current child evidence, and cancels or pauses wake-ups on
  completion, user stop/pause, exhausted budget, or a user-input blocker.
  Documented thread-heartbeat fallback and bounded waits when scheduling fails;
  ordinary sleeps do not establish a durable wake-up.
