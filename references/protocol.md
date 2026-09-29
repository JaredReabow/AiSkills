# Delegation protocol

Detailed companion to `SKILL.md`. Use it when writing a reviewer brief, a worker
handoff, or a review packet. The ledger schema that records these fields is in
[ledger.md](ledger.md).

## Worker handoff fields

Every request the main agent translates into a `spawn_agent` or `followup_task`
call carries all of the following. A field the task makes irrelevant is still
stated explicitly, for example `dependencies: none` or `write_paths: []`.

- **Task id and kind**: `research`, `implementation`, `verification`, or
  `integration`.
- **Objective**: the bounded result, not the activity.
- **User requirements**: the stable ids (`R1`, `R2`, ...) this task satisfies.
  A task that satisfies none is a planning error, not a small task.
- **Dependencies and contract**: which accepted outputs it consumes, and the
  interface or behavior it may rely on. State the contract; do not let the worker
  infer a shared interface from another worker's unfinished file.
- **Non-goals**: what it must not change, so ownership stays real.
- **Workspace and baseline**: absolute paths, the baseline revision, and which
  uncommitted or untracked work is present. HEAD alone does not describe a dirty
  worktree.
- **Owned paths and resources**: files, generated outputs, ports, devices,
  databases, or build directories it may write. Everything else is read-only.
- **Acceptance criteria**: each one mapped to requirement ids and paired with the
  check that would demonstrate it.
- **Required evidence**: the commands, exit statuses, artifacts, hashes, or
  source citations the worker must return.
- **Completion condition and limits**: what "done" means, plus any time, token,
  spending, or concurrency limit the user set.

The ledger machine-checks the dispatch-critical fields: id, type, state,
objective, requirement mapping, dependencies, owner and explicit model, workspace
and baseline revision, owned paths, acceptance criteria, evidence, review, and
the verification target. Non-goals, completion condition, and per-task limits
live in the brief text; the validator does not pretend to check them.

For verification tasks, add the task id, or list of task ids, being verified.
The verifier must be a different actor from the author when the change is
important, normally has `write_paths: []`, and must not list a target in
`depends_on`: it reads the target's frozen `ready_for_review` revision instead,
which is the only case where reading a path an in-flight peer owns is allowed.
One verifier may cover several frozen targets, for example two independently
authored modules, and its acceptance snapshot then covers both.

## Worker instruction block

Copy this into each worker's assignment:

> You are not alone in the codebase. Do not revert or overwrite other people's
> edits. Stay within your assigned ownership and adapt to accepted shared changes.
> Report conflicts to the main agent. Do not spawn agents or self-approve.
> While a peer owns an input, run only checks isolated to your own files; defer
> cross-module or integration checks until the inputs are frozen, and say what
> you deferred. A test against a clearly labelled local stub of an agreed
> interface is fine; importing a peer's changing file is not.

## Reviewer brief

Include this contract in the reviewer's assignment, alongside the original request and
every amendment verbatim, the ledger path, the baseline revision, the available
capacity, the artifact directory for evidence, and the user's limits:

> You are the planning and acceptance authority for this task. The user's main
> agent owns the tools that schedule the team. Inspect enough source and evidence
> to define clear contracts, divide independent work, and catch incorrect
> reasoning or implementation. Request additional DeepSeek workers through the
> main agent using the handoff fields above. You do not spawn children, change
> model or provider settings, or edit implementation files. Review actual
> artifacts and evidence; request research when facts or assumptions are
> unresolved. Send one consolidated decision packet per useful boundary. Approval
> of a plan authorizes its tasks within the user's scope; it is not acceptance of
> unwritten code. Never mark work accepted with unresolved blocking findings. Your
> review does not grant external action permissions. Return your packet and
> finish the turn; the main agent will resume you with results. Read only the
> relevant parts of the skill and sources.

## Optional Council integration

Ordinary Parallelism retains one planner/acceptance reviewer. Load The Council
only when requested. In that mode the lead planner is a council member and
continues proposing plans and worker briefs; it has no unilateral acceptance
authority. The main agent sends identical review packages to the full panel and
collates reports without resolving disagreements itself.

Before marking a task accepted:

1. Freeze the current task and dependency artifacts. Run The Council's packet
   checker using its documented schema and `--package-dir` pointing to the
   frozen artifact directory; do not use a record-only diagnostic as acceptance.
   Verify the package
   fingerprint against those exact artifacts, user requirements and amendments.
2. Require the complete active panel's evidence-based final acceptance, all
   required checks passed, and the checker's `COMPLETE`. A missing, stale, or
   incomplete Council packet cannot be replaced with the lead's opinion.
3. Record the packet path and SHA-256, panel and membership generation, reviewed
   package fingerprint, findings and outcome in the ledger checkpoint. Record
   `reviewer_actor`/`reviewer_model` as the lead member in the legacy task review,
   with a note identifying it as a projection of the Council packet. Its
   `verified_hashes` must still cover the actual dependency artifacts.
4. Run ordinary `workflow.py validate`, including `--mode final` at completion.
   It validates the existing ledger invariants, not Council votes. Passing that
   command alone does not satisfy council mode. Changes to reviewed inputs
   invalidate affected Council decisions and require a new package and review.

Reserve runnable capacity for all council members when review is pending. The
unchanged scheduler reserves one reviewer slot for `ready_for_review` tasks.
For council review, add the **additional** reserved seats to `--other-active`:
`actual_other_jobs + max(0, reserved_council_seats - 1)`. Do not also count those
same reserved reviewer actors as other jobs. Outside that readiness state the
scheduler reserves no reviewer slot; count actual council work explicitly in
`--other-active`. The main agent owns this accounting and records it. If host
capacity is smaller than the panel, stage the independent reviews against the
same frozen package without exposing peers' reports. Preserve all requested
seats and explain reduced concurrency rather than dropping a reviewer.

The Council and implementation workers share the existing main-agent timer.
There is no second orchestrator, nested reviewer team, or independent Council
heartbeat when integrated. Standalone Council reviews do not authorize repairs;
in an already-authorized build, confirmed findings become bounded worker jobs.

## Native tool mapping (single reviewer or a Council seat)

Read the current schema before calls. This host exposed these shapes when the
skill was created; names and available routes must be checked in later sessions.
A clean context requires supplying all relevant constraints. The example below
uses the default reviewer, Sol with medium reasoning. Replace its model and
effort with the user's explicit choice after checking the live tool schema.

```json
{
  "task_name": "selected_reviewer",
  "agent_type": "default",
  "model": "gpt-6-sol",
  "reasoning_effort": "medium",
  "fork_turns": "none",
  "message": "Full task contract, baseline, constraints, and the reviewer brief"
}
```

That object is an example argument to `collaboration.spawn_agent`, not a command
to execute verbatim. Use the available generic role for explicit reviewer model
selection; a role with a pinned model takes precedence over a conflicting spawn
model.

This installation also exposed the worker role
`router_deepseek_deepseek_v4_1_flash`, pinned to
`deepseek/deepseek-v4.1-flash`. Use it only while its current schema and binding
still match the intended worker route. Other DeepSeek roles are valid when their
current bindings and instructions fit the assignment. Do not use an old role
name without checking availability, or an inherited default as an unverified
reviewer.

For continuation, this host offers `collaboration.followup_task` for an idle
child. The reviewer can return a decision packet as its final response; the main agent
cleans up that turn, schedules work, and later resumes the reviewer with
evidence. Use the checkpoint if the host cannot resume that child.

When the user changes reviewer models, follow the handoff in `SKILL.md`. Only
the currently designated reviewer can issue new acceptance decisions. Include
its actor id and actual selected model in each packet; keep earlier attribution
intact. A pinned role or an idle child cannot be repurposed as a different model
merely by describing it differently.

## Capacity and backpressure

Count these separately:

- **simultaneous workers**: running DeepSeek children, counted by actor and
  bounded by the host limit and `limits.max_concurrent_workers`;
- **other active jobs**: the main agent's own work plus anything outside the task
  that still consumes a slot;
- **the reviewer**: reserve a slot whenever review work is pending, so acceptance
  cannot be starved by implementation;
- **total workers**: every worker used across the task, which may exceed the
  simultaneous limit over time.

Host capacity is the total agent budget: subtract `other_active` and the reserved
reviewer slot first, then running workers. `limits.max_concurrent_workers` caps
running workers separately, and `limits.max_total_workers` caps how many distinct
workers are ever dispatched. A configured maximum is a ceiling, not a target, and
the user's budget can lower it further.

When the review backlog reaches the user's limit, hold **new implementation**
starts rather than accruing unreviewed output, but keep verification and research
dispatchable: they are what clears the backlog. When a dependency fails or is
blocked, mark dependents blocked with the reason; do not guess that they
completed, and do not leave them queued forever.

`scripts/workflow.py schedule` computes all of this from the ledger and attaches
a reason to every suggestion. It never dispatches.

## Decision packets

The reviewer's planning reply names the approved contract and the tasks it wants. A
research request looks like this:

```yaml
decision: REQUEST_RESEARCH
reviewed_task: initial_design
reason: The storage migration behavior is not established.
requests:
  - task_id: inspect_storage
    kind: research
    objective: Identify existing version migration and rollback behavior.
    depends_on: []
    requirements: [R3]
    workspace: /absolute/path/to/project
    ownership: Read-only source inspection; no edits.
    acceptance: Cite the migration entry points and existing migration tests.
    evidence: File and line references, unanswered questions, relevant test names.
```

The main agent fills in the known baseline and context before dispatch. Missing
decision-critical details go back to the reviewer rather than being guessed. Research
requests must identify how the answer affects a decision.

An acceptance packet identifies the exact task and artifact version, the
criteria satisfied, the evidence inspected, verification outcomes, and remaining
non-blocking limitations. In ledger terms it is a review decision with
`verified_hashes` covering the task's dependency closure; a changed upstream hash
invalidates the acceptance.

`ACCEPT` is a workflow decision, not a tool-enforced access control. A hardened
service would need an actual state machine and enforced transitions; this skill
installs instructions plus a read-only checker, and does not claim that prompting
guarantees compliance.
