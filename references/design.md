# Design, use, and validation

Why the skill is shaped this way, and how to invoke it. The execution rules live
in `SKILL.md`; the request fields and review packets live in
[protocol.md](protocol.md); the machine-checkable record lives in
[ledger.md](ledger.md); the measurement fixture lives in
[benchmark.md](benchmark.md).

## Start a task

Select any main model that supports Codex's agent tools, then invoke:

```text
Use $parallelism to implement [objective]. Keep my selected main model.
Astra should plan and review, and ask you to create parallel DeepSeek workers
for independent tasks.
```

Optionally add a worker cap, budget, or restriction such as plan-only. Without a
user cap, choose useful concurrency within the actual host limit; do not raise
host settings. Selecting this skill does not switch the already-running model.
DeepSeek identifies the worker model rather than a requirement for the main
model. Both child routes must be available in the session. When Astra is the main
model, it still creates a separate Astra reviewer child for independent artifact
review. If the skill is absent from discovery, refresh the session or reference
its absolute `SKILL.md` path. Do not claim discovery until it is observed in the
host.

```text
User
  |
Your selected main agent -- requests/results -- Astra planner and reviewer
  |-- DeepSeek worker 1: approved independent task
  |-- DeepSeek worker 2: approved independent task
  |-- ...
  `-- DeepSeek worker N: approved independent task
```

N is dynamic: use as many ready independent tasks as available capacity and the
user's budget permit. Several research, implementation, verification, or
integration workers can run at once. Refill freed slots from the approved queue.
Simultaneous workers and total workers across the task are separate counts. The
reviewer and workers are siblings. Astra has decision authority within the task;
the main agent owns scheduling and enforces the review workflow.

## Why this design

Primary sources checked on 2026-09-28:

- [OpenAI: Subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents)
  describes explicit model selection, inherited settings, and parallel work with
  additional care around concurrent writes. This design uses native children,
  specific model routes, bounded assignments, and one scheduling owner.
- [OpenAI: Build skills](https://learn.chatgpt.com/docs/build-skills) describes
  reusable workflows and progressive disclosure. `SKILL.md` holds execution
  rules; the reference files hold protocol detail, schema, and measurement.
- [Anthropic: Building effective agents](https://www.anthropic.com/engineering/building-effective-agents)
  describes orchestrator/worker and evaluator/optimizer patterns. This workflow
  combines task decomposition with explicit acceptance criteria and evidence.
- [Anthropic: Multi-agent research system](https://www.anthropic.com/engineering/multi-agent-research-system)
  reports the value of precise delegation and independent research, along with
  coordination and token costs. Coding often has fewer independent branches than
  research. The skill fills useful capacity rather than maximizing agent count.

Three design choices are inferences rather than vendor prescription:

1. **Astra as a sibling that requests workers through the main agent.** This
   centralises scheduling, capacity accounting, and task ownership without
   nested delegation, and keeps exactly one process spawning children.
2. **Independent verification for important changes, on a normal worker.** A
   reviewer that shares the author's context repeats the author's blind spots;
   using a sibling worker keeps Astra free for batched acceptance. Fresh Astra
   passes are reserved for risk rather than run on every routine task.
3. **A ledger plus a read-only checker.** Prompt-only discipline drifts across a
   long run. Recording requirements, ownership, evidence hashes, and review
   decisions in one file makes the drift visible without giving a helper the
   authority to dispatch anything.

The sources do not establish that this exact DeepSeek/Astra combination outperforms
other combinations. Measure it on representative tasks before making that claim,
using [benchmark.md](benchmark.md) and repeated comparable runs.

## Behavioral evaluation

Static skill validation checks packaging, not actual model routing or behaviour.
Run the whole harness with one command:

```bash
cd /Users/leo/.codex/skills/parallelism && python3 tests/run_harness.py
```

It runs every test module in `tests/` against real temporary workspaces and then
the bundled `skill-creator/scripts/quick_validate.py` packaging check. It does not
exercise agents either: it verifies the ledger and benchmark helpers, not model
behaviour.

For runtime evidence, use the disposable fixture in the benchmark reference and
record actual changes, commands, worker lifecycle events, and review decisions.
Do not run paid model probes merely to install this skill. Use the first
authorized real task, or an explicitly requested evaluation.

Passing evidence per scenario:

| Scenario | Observable passing outcome |
| --- | --- |
| Two independent modules and one shared manifest | Independent work overlaps; one owner handles the manifest; combined checks pass. |
| Missing factual prerequisite | Astra requests a specific source-backed research task before dependent implementation. |
| Worker says tests passed but logs show failure | Acceptance is withheld, a correction is requested, and the revised evidence is checked. |
| New edit after acceptance | Affected acceptance is invalidated and the new artifact is reviewed. |
| Child completes; corrections are needed | The main agent marks it done, resumes it correctly, and avoids a duplicate writer. |
| Capacity reached or worker stream quiet | Ready jobs queue; existing workers are not duplicated or killed for a timeout. |
| More independent jobs than worker slots | Available slots fill with approved jobs and refill as workers finish. |
| Review backlog grows | New implementation is held back; the verification that clears the backlog still dispatches. |
| Two frozen modules need one combined verifier | The verifier starts against both frozen revisions and both modules can then be accepted. |
| One module is still being written | Peers run only isolated checks; cross-module checks are deferred and recorded, not run against a changing file. |
| Two queued writers claim the same path | Only one is suggested; the other stays deferred with the ownership reason. |
| A shipped test is edited before grading | Integrity fails before anything is executed and every requirement reports `not_run`. |
| Astra route unavailable | The task reports the routing limitation; the main agent does not impersonate Astra approval. |
| Main model is not DeepSeek | The selected model is preserved; explicit routes still select Astra review and DeepSeek workers. |
| Main model is Astra | A separate Astra child reviews artifacts; the main agent cannot self-approve. |
| User changes scope during work | The change reaches Astra, the contract is updated, and affected jobs are steered. |
| Root contributes code | Root changes receive the same review as worker changes. |

Evaluate correctness, missed defects, unnecessary agents and rework, total
elapsed time, and available provider-reported usage. Compare with an ordinary
single-agent run on comparable tasks. Treat self-reported success and estimated
costs as insufficient evidence. Add runtime code only if observed failures
justify it.
