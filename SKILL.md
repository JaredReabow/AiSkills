---
name: parallelism
description: Coordinate substantial work with the user's selected main model, a user-selected planner and acceptance reviewer, and parallel DeepSeek workers. Use when the user wants delegated implementation with independent review and a reviewer model they can change during the task. The reviewer requests workers from the main agent; all children are siblings. Skip ordinary single-agent tasks and other orchestration workflows.
---

# Parallelism

Run one flat team: the user's selected main agent owns scheduling and
communication; one reviewer child owns architecture, research decisions, and
acceptance; DeepSeek worker children do bounded research, implementation,
verification, and integration. The reviewer asks the main agent to create or resume
workers. Only the main agent spawns.

This is a workflow instruction, not a model switch or a permission boundary.
Keep whichever main model the user selected, and keep a separate reviewer child
even when the main agent and reviewer use the same model, so
authoring and acceptance stay separate roles. The skill cannot change the
current model, enforce an approval gate in code, or grant tools the host does
not expose. Respect higher-priority instructions and existing action
permissions, and never rewrite AGENTS.md, provider settings, agent bindings, or
concurrency settings while running it.

## 1. Establish the contract

Read the request and the repository instructions. Record the objective,
constraints, existing approvals, the workspace baseline including uncommitted and
untracked work, and any user time, spending, or concurrency limit. Reuse an
existing plan.

At the start of each task, use the reviewer model the user explicitly selected
for that task. If none was supplied, ask which available model should plan and
review before dispatching the reviewer or work that needs its approval. Do not
silently default to Astra, the main model, or a prior task's selection. Read-only
baseline collection can continue while waiting for the answer. Any model with
an available native subagent route may fill the reviewer role, including the
same model as the main agent or workers; actor separation still applies.

Check the live tool schema and model choices. Record the user's choice, resolved
reviewer model id, reviewer actor id, and selection time in the checkpoint.
Workers still use `deepseek/deepseek-v4.1-flash` unless the user selects another
available DeepSeek model. Verify the main model from session metadata: global
defaults and model self-identification do not prove it. Use explicit child model
selection or a verified installed role whose binding matches the requested
model and whose instructions permit planning and review. If the route is missing
or rejected, report it and ask for another
selection; do not silently substitute or change provider settings.

Send the reviewer the **original request and every amendment verbatim**, plus the
constraints, baseline, artifact paths, capacity, and the reviewer brief in
[the delegation protocol](references/protocol.md). The reviewer replies with an
approved plan and worker requests, or names the missing evidence. Translate
those requests into tool calls. You may reorder ready independent jobs; changed
scope, shared contracts, or acceptance criteria go back to the reviewer first.

### Change the reviewer during work

The user can select a different reviewer at any time. Record the request
verbatim as an objective amendment and add a dated handoff to the checkpoint:
outgoing/incoming actor and model, accepted artifact versions, pending reviews,
open findings, running jobs, and the next decision. Verify the new route before
claiming a switch succeeded. If unavailable, hold new acceptance decisions and
ask for a supported choice; existing independent approved work may continue.

Stop assigning reviews to the outgoing actor and mark its authority superseded
at the handoff boundary. Finish or stop only its review turn as needed, clean up
its slot, and spawn a separate reviewer using the newly selected model. Never
pretend that resuming the old actor changes its model. Give the new reviewer the
original request and amendments, ledger, contracts, evidence, and open findings.
It must acknowledge the handoff and inspect evidence before deciding; ignore
late outgoing decisions as approvals. Keep healthy workers running unless the
user's change affects their scope or safety.

Preserve earlier review records with their original actor/model attribution;
a model change alone does not invalidate unchanged, previously accepted
artifacts. Archive a prior decision before replacing a task's review record.
The new reviewer owns pending decisions and final acceptance, and may reopen
prior work with specific findings. Changed artifacts or contracts still require
fresh review. Record the actual reviewer model on every new review packet.

## 2. Record it as a ledger

Write the contract into one task-ledger JSON file: the objective and amendments,
one stable id per user requirement (`R1`, `R2`, ...), the approved tasks, their
acceptance criteria, their evidence, and the review decisions. **Every
acceptance criterion maps to at least one requirement id**, so finished work
traces back to what the user actually asked for.

Schema, example, and commands: [ledger reference](references/ledger.md). Use
`scripts/workflow.py validate` before each dispatch and
`scripts/workflow.py schedule` to choose the next job from real capacity.

## 3. Fill capacity with independent work

There is no fixed worker count. The reviewer identifies every useful independent task
and requests a worker for each ready bounded task. Keep an explicit approved
queue and fill available capacity with ready, independent jobs. Capacity comes
from the host's real limit, other active children, the reviewer lifecycle, and
the user's budget; a configured maximum is a ceiling, not a target. Simultaneous
workers and total workers over the task are separate counts, and duplicate work
is never created just to fill a slot.

The scheduler counts running workers, other active jobs, and a reserved reviewer
slot explicitly. It prioritises tasks that unblock the most dependents, keeps
the reviewer runnable while review work is pending, and applies bounded review
backlog backpressure instead of piling up unreviewed implementation. It only
suggests, with a stated reason per suggestion.

Every writer gets exclusive ownership of its files, generated outputs, and
mutable resources. Give each worker the instruction block from
[the delegation protocol](references/protocol.md): stay inside your assigned
ownership, do not revert other people's edits, report conflicts, and do not
spawn agents or self-approve.

While another worker owns a path, run only checks isolated to your own files.
Defer cross-module and integration checks until the inputs are frozen, and
record what you deferred in the task's `deferred_checks`. A test that runs
against a clearly labelled local stub of an agreed interface is fine; importing
a peer's changing file is not. The ledger enforces the same rule by rejecting a
declared read of a path that an in-flight peer is still writing.

Serialize shared manifests, lockfiles, version files, hardware, deployments,
ports, databases, and build outputs. Use separate worktrees when code or build
state cannot be isolated in one checkout; a native child does not automatically
have its own worktree, and HEAD alone does not include dirty changes.
Parallelize only when isolation and dependencies are understood.

## 4. Hand off completely

Every dispatched task brief states: task id and kind (research, implementation,
verification, or integration); objective; requirement ids; dependencies and the
accepted interface contract; non-goals; absolute workspace path and baseline
revision; owned paths and forbidden changes; acceptance criteria mapped to
requirement ids; required evidence; and any resource limit.

The ledger machine-checks the dispatch-critical subset: id, type, state,
objective, requirement mapping, dependencies, owner and explicit model,
workspace and baseline revision, owned paths, acceptance criteria, evidence,
review, and the verification target. The remaining brief fields - non-goals,
completion condition, and per-task time or budget limits - are part of the
prompt the reviewer and the worker read; the validator does not pretend to check them.
Full field list: [delegation protocol](references/protocol.md).

### Retrigger while waiting for subagents

While reviewer or worker jobs are outstanding, use timer events to retrigger the
main agent instead of leaving the task dormant. Before yielding for a wait,
schedule a wake-up for this same task with the host's available timer tool and
confirm that scheduling succeeded. Prefer a one-shot timer; if only scheduled
automations are available, use a thread heartbeat through `automation_update`.
Keep one outstanding wake-up per orchestration, reuse its id, and record it with
the checkpoint, child ids, last-seen result cursors, and the next action.
Only the main agent owns this timer; children must not create competing timers.

Choose an interval appropriate to the expected work and the host's supported
cadence, and back off after unchanged checks. Continue useful independent work
while children run, and handle completion events promptly without waiting for
the timer. On every wake, reload the checkpoint and inspect current child
status before acting. Collect new evidence, mark finished children done, route
results to the reviewer, and dispatch newly ready work. Never duplicate a job or
interrupt a healthy child merely because a timer fired. If work remains,
rearm the one-shot timer or retain the existing heartbeat before yielding.
Keep unchanged checks quiet; notify on meaningful progress, blockers, or a
required user decision.

Cancel the timer or pause its heartbeat when the task completes, the user stops
or pauses it, a budget is exhausted, or continuation requires user input with
no independent work remaining. Ignore late wake-ups for stopped or superseded
work. If scheduling is unavailable or fails, use bounded native agent waits
and report the limitation; do not claim a wake-up is scheduled. A shell sleep,
`clock.sleep`, or JavaScript `setTimeout` is not a durable cross-turn wake-up.
Timers resume only the already-authorized task and never expand its scope.

## 5. Verify, then accept

Track work as `queued -> running -> ready_for_review -> accepted`, with
`needs_changes`, `needs_research`, and `blocked` as side states. A worker
finishing its turn means `ready_for_review` and nothing more. The main agent
cannot accept its own work or turn a worker's success claim into approval.

An **important** change is verified by a worker other than its author. That is a
normal delegated task with its own acceptance criteria and evidence, not
automatically a reviewer turn. A fresh reviewer check is also warranted when risk
justifies it: security, authorization, payments, tenancy, secrets, destructive
migrations, production or shared infrastructure, or a material revision after
acceptance. Routine tasks do not each need a fresh reviewer pass on top of normal
review.

The reviewer owns acceptance and inspects the actual artifacts, checking
specification, correctness, coverage, risk, and interaction with other accepted
work. Passing tests support review; they do not establish that the right
behavior was implemented. Send the reviewer compact batches at dependency or
integration boundaries: task ids, baseline and resulting hashes, changed paths,
artifact locations, checks with outcomes, sources, and open issues. Keep raw
evidence in files rather than forwarding huge logs, and keep a worker's claim
distinct from independently observed evidence.

The reviewer answers with one decision per task: `ACCEPT`, `REQUEST_CHANGES` (findings,
locations, required verification), `REQUEST_RESEARCH` (the open question, why it
matters, what evidence is needed), or `BLOCKED` (the exact unavailable
dependency, permission, or user decision). Send consolidated findings back to
the same worker where practical. After two failed correction rounds on one
issue, have the reviewer diagnose the cause and choose a new approach within scope
rather than repeating the loop or declaring success. Ask the user only for a
genuinely unresolved decision or limit, and honour explicit budgets without
silently raising them.

Acceptance applies to the inspected artifact version only. A later edit or
material dependency change invalidates the affected acceptance and requires
review. Before finishing, integrate accepted outputs in dependency order, run
the combined checks, and send the resulting state to the reviewer for final acceptance.
One worker owns integration, version, and history updates when these would
otherwise collide. Commits, pushes, deployments, flashing, and other external
actions keep the user's own authorization requirements; reviewer acceptance adds none.

## 6. Close out

Keep a compact checkpoint: goal, constraints, approved contracts, reviewer and
worker ids, task ownership, accepted artifact versions, open findings, and the
next ready jobs. Prefer the ledger, and keep raw evidence in an authorized
artifact directory rather than committing scratch logs.

When a child finishes a turn or errors, record its output and call the host's
completion tool. In this Codex host, call `interrupt_agent` on a finished child
so it is marked done, and never interrupt healthy work for cleanup. Use
`followup_task` to resume an idle child for review or corrections;
`send_message` alone does not start an idle turn. If the host removes completed
children, pass the checkpoint to a replacement of the same verified model. Do
not leave idle agents consuming capacity.

New user instructions reach the reviewer and every affected worker before dependent
work continues. Stop only the jobs the change makes unsafe or obsolete.

Finish only after the reviewer accepts the final state and the required checks pass, or
clearly report the remaining blocker or user budget limit. Report the outcome,
the evidence, the limitations, and whether actual model routing was verified. A
requested model name or a static configuration check is not proof of the
provider that ran.

## References

- [Delegation protocol](references/protocol.md): the reviewer brief, the worker
  instruction block, full handoff fields, capacity rules, and review packets.
- [Ledger reference](references/ledger.md): schema, validator and scheduler
  commands, example, and what a ledger does not prove.
- [Benchmark reference](references/benchmark.md): the disposable fixture, exact
  commands, and how to compare against a single agent.
- [Design and validation](references/design.md): source rationale, invocation
  examples, and behavioural scenarios.
