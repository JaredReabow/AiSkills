# Task ledger

One JSON file per task records the objective, the user's requirements, the
approved work, the evidence, and the review decisions. It exists so the
scheduling agent can ask a checker instead of remembering.

Helper: `scripts/workflow.py` (Python standard library only).
Example: `templates/ledger.example.json`, with its evidence file
`templates/sample-evidence.txt`.

## Commands

```bash
python3 scripts/workflow.py validate --ledger /path/to/ledger.json
python3 scripts/workflow.py validate --ledger /path/to/ledger.json --mode final
python3 scripts/workflow.py validate --ledger /path/to/ledger.json --json
python3 scripts/workflow.py schedule --ledger /path/to/ledger.json \
    --host-capacity 6 --other-active 0
```

`validate` exits `0` when the ledger is usable (warnings allowed), `1` when it
has findings, and `2` when the file is unreadable or not valid JSON. `schedule`
prints JSON and exits `1` if the ledger is invalid.

`--mode planning` (the default) is used while work is in flight: a requirement
that no acceptance criterion maps to yet is a warning. `--mode final` is the
completion check: every requirement must be covered by an **accepted** task, and
no task may still be queued, in flight, blocked, or failed.

Add `--no-verify-files` while drafting, to skip workspace existence and artifact
hash checks. Leave it off before dispatching: the file checks are the point.

**Guarantees.** Both subcommands open files read-only, never write to the ledger
or the workspace, never spawn an agent, and never execute a `command` string
recorded in the ledger. Those strings are evidence metadata for a human reader.

**What a ledger does not prove.** `owner.actor`, `owner.model`, and `command`
are declarations. The checker does not authenticate routing, re-run anything, or
treat a log as proof of execution. It verifies that declared artifact hashes
match the bytes actually on disk. Treat the rest as claims that the reviewer inspects.

Record the active reviewer actor/model, reasoning effort and selection time in `checkpoint`.
Record a user-requested switch in `objective.amendments` and append its handoff
to the checkpoint. Each new `review` records `reviewer_actor` and
`reviewer_model`; the schema permits legacy missing model fields, but the skill
requires attribution for new decisions. Preserve previous review packets in
checkpoint-linked evidence when replacing a task review. The checker accepts
arbitrary model strings and historical reviewers; it does not authenticate
routes, enforce the active reviewer, or execute handoffs.

The example ledger uses the default reviewer model, `gpt-6-sol`. Its medium
reasoning effort belongs in the checkpoint, since review records have no effort
field. Replace the model and checkpoint effort when the user selects another route.

## Schema

Top level:

| Field | Required | Meaning |
| --- | --- | --- |
| `schema_version` | yes | Must be the integer `1`. |
| `objective` | yes | `id`, `text` (the user's original request), and `amendments[]` of `{id, text, recorded_at}`. |
| `requirements` | yes | `{id, text, source}` where `source` is `objective` or an amendment id. Ids are stable (`R1`, `R2`, ...). |
| `baseline` | yes | `workspace` (absolute, or relative to the ledger file), `revision`, optional `dirty`, `dirty_paths`, `notes`. |
| `tasks` | yes | The approved work; see the task table. |
| `limits` | no | `max_concurrent_workers`, `max_total_workers`, `review_backlog_limit`, `budget_usd`, `notes`. `null` means not supplied. |
| `resources` | no | `{id, kind: exclusive or shared, description?, paths?}`. Exclusive resources admit one holder at a time. |
| `artifacts` | no | `{id, task_id, path, sha256, kind?}`. `path` is relative to the task workspace. |
| `observations` | no | Optional observed metrics. Missing values stay `null`; nothing is estimated. |
| `checkpoint` | no | Free-form resume notes. |

Each task:

| Field | Required for | Meaning |
| --- | --- | --- |
| `id` | always | Unique across the ledger. |
| `type` | always | `research`, `implementation`, `verification`, or `integration`. |
| `objective` | always | The bounded result. |
| `state` | always | `queued`, `running`, `ready_for_review`, `needs_changes`, `needs_research`, `blocked`, `failed`, or `accepted`. |
| `requirements` | always | Requirement ids this task satisfies. Non-research tasks need at least one. |
| `depends_on` | always | Task ids that must be `accepted` first. |
| `owner` | any state other than `queued` | `{actor, model}`. The model should be explicit. |
| `workspace` | optional | Defaults to `baseline.workspace`. |
| `baseline_revision` | optional | Defaults to `baseline.revision`; one of them must be present once the task leaves `queued`. |
| `write_paths` | always | Paths this task may write, relative to its workspace. Must resolve inside the workspace, including through symlinks. |
| `read_paths` | optional | Extra reads. Absolute paths are allowed; relative ones must stay inside the workspace. |
| `resources` | optional | Resource ids, checked for exclusive conflicts against in-flight tasks. |
| `acceptance` | non-queued and build work | `{id, criterion, requirement_ids, required?}`. Acceptance ids are unique across the ledger. |
| `evidence` | required to accept | `{id, acceptance[], command?, exit_status?, status, artifact?, notes?}` with `status` in `pass`, `fail`, `missing`. |
| `review` | required to accept | `{reviewer_actor, reviewer_model?, decision, at?, notes?, independent?, verified_hashes, artifact_free?}`. |
| `importance` | optional | `standard` (default) or `important`. |
| `verifies` | verification tasks | The task id, or a list of task ids, being verified. A verification task cannot verify itself, and must not list a target in `depends_on`. |
| `deferred_checks` | optional | Checks postponed because a peer still owns an input, e.g. `integration_check.py`. |
| `non_goals` | optional | What must not change. |

`verified_hashes` maps artifact ids to the digest the reviewer saw. An accepting
review must cover every artifact belonging to the task's dependency closure, so a
later upstream edit is detectable. The closure follows `depends_on` **and**
`verifies`, so a verifier's snapshot also covers the targets it reviewed even
though it declares no dependency on them.

## Acceptance rules

An `accepted` task needs all of the following.

- `review.decision` is exactly `accept`.
- Every required acceptance criterion has passing evidence that is not an empty
  status assertion: the row records a command, and binds to a hashed artifact or
  an integer exit status.
- `verified_hashes` covers the whole dependency closure, and at least one
  passing evidence row is bound to one of those artifacts.
- `implementation` and `integration` tasks declare at least one hashed artifact
  of their own, so their result is version-bound.
- `verification` tasks cover hashed targets; a verifier whose targets declare no
  artifact cannot be accepted.
- `research` tasks normally hash a captured evidence note. A research task with
  genuinely nothing to hash sets `review.artifact_free: true` and
  `verified_hashes: {}`; no other type may use that escape.

When a target is re-edited after acceptance, both the target's own acceptance and
the verifier's snapshot become stale, and validation reports it.

## Readiness for verification

A verifier does not wait for its target to be accepted. It reads the target's
**frozen** `ready_for_review` revision, which is the only case where a read of a
path an in-flight peer owns is allowed. This is what stops the obvious deadlock:
an important target needs an accepted verifier before it can be accepted, so the
verifier must be able to run against the frozen revision. A verifier that reads a
still-`running` or `needs_changes` target is rejected, and the scheduler will not
dispatch a verifier until every target it covers is frozen or accepted.

## What the validator rejects

- Malformed shapes and wrong types anywhere in the document. Unknown field names
  produce warnings instead of errors, so a typo stays visible without blocking.
- Duplicate or malformed ids for tasks, requirements, resources, artifacts,
  acceptance criteria, evidence, and amendments.
- Unknown references: dependencies, requirement ids, resource ids, artifact ids,
  acceptance ids, and requirement `source` values.
- Dependency cycles and self-dependencies.
- Handoffs missing dispatch-critical information: owner, explicit model,
  baseline revision, requirement mapping, or acceptance criteria.
- Write paths that escape the task workspace through `..` or a symlink.
- Two in-flight tasks whose paths collide, or which both hold an exclusive
  resource. `running`, `ready_for_review`, `needs_changes`, and `needs_research`
  all count as in-flight, because any of them may be resumed. Collision is
  checked on absolute, symlink-resolved paths, so a writer of `src` collides with
  a writer of `src/module.py`, and two different workspace roots that name the
  same real file collide too. Reads are checked as well: reading a path an
  in-flight peer is writing is an error, unless that peer is the `ready_for_review`
  target the reader verifies.
- Self-approval: a reviewer that is also the task owner.
- State that disagrees with the dependency graph: a task that left `queued`, or
  was accepted, while a dependency is not accepted; a verifier whose target is
  not frozen; or one actor inside two in-flight tasks.
- An accepted task whose required acceptance criterion has no passing evidence,
  or whose evidence is recorded as failing.
- A recorded artifact hash that no longer matches the file, and an acceptance
  whose upstream artifact changed after it was recorded.
- An `important` task accepted without an accepted verification task owned by a
  different actor.
- State that already exceeds a supplied user limit.

Warnings (still exit code 0) cover softer gaps: an uncovered requirement, a task
without an explicit model, or a hash recorded for an artifact outside the task's
dependency closure.

## Scheduler output

```bash
python3 scripts/workflow.py schedule --ledger ledger.json --host-capacity 6
```

The result reports the effective capacity, how many running workers, reserved
reviewer slots, and other active jobs it counted; the free slot count; the review
backlog and whether backpressure is active; and then:

- `suggestions`: ready work, each with `kind` (`review`, `rework`, `research`,
  `implementation`, `verification`, `integration`) and a plain-language `reason`.
  Tasks that unblock more dependents are listed first.
- `deferred`: ready work held back, with the reason (dependency not accepted, a
  live write-ownership conflict, no free slot, or review backpressure).
- `blocked_tasks`: work whose dependency failed or is blocked.

`budget.usd` echoes the user's figure and stays `null` when unknown. The helper
never estimates cost or claims a saving.

### Capacity arithmetic

- Workers are counted by **actor**, not by task: one resumed worker is one agent
  slot even when it is handed a second task. An actor may only be inside one
  in-flight task, which the validator enforces.
- `host_capacity` is the total agent budget. Subtract `other_active` and the
  reserved reviewer slot first, then subtract running workers.
- `limits.max_concurrent_workers` caps running workers on its own, so it applies
  after the host arithmetic rather than replacing it.
- `limits.max_total_workers` caps how many distinct workers are ever dispatched.
  A suggestion that would need a new actor is held once that cap is reached; a
  rework task resuming its existing owner does not consume a new worker.

Under review backpressure the scheduler holds **new implementation work** only.
`verification` and `research` tasks stay dispatchable, because they are what let
pending reviews finish; holding them would leave the backlog stuck. A verifier
that clears the backlog is therefore suggested even when implementation is held.

## Writing one

1. Copy `templates/ledger.example.json` next to the work, or write the minimum
   fields above.
2. Point `baseline.workspace` at the real project and record the revision.
3. Add one requirement per user ask. Add an amendment entry when the user
   changes the request, and a requirement sourced from that amendment.
4. Dispatch tasks in the states above, recording owner, paths, resources, and
   acceptance as they start.
5. Record artifacts with their real digests as they are produced.
6. Record the review decision with the hashes it verified.
7. Run `validate` before each dispatch and `schedule` instead of guessing.

The shipped example is self-verifying: its workspace is `.` and its artifact is
the sample evidence file in this package. Pointing it at a real project means
replacing the workspace and re-recording hashes.
