---
name: the-council
description: Give one to three independent reviewers the same frozen package, collate their findings, then have every reviewer re-decide whether the work is complete. Use when the user asks for a council, a panel, or multiple independent reviewers of a task, plan, or artifact, or when Parallelism should take acceptance from a panel instead of one reviewer. Skip ordinary single-reviewer work and single-agent tasks.
---

# The Council

One to three reviewers judge the same frozen package. They review independently
first, the coordinator collates what they found, and every reviewer then decides
again with the collated set in front of them. Acceptance needs all of them
accepting the same package version, so one seat cannot carry a result and one
seat cannot quietly drop another seat's finding.

This is a workflow instruction, not a model switch, an approval gate, or a
permission boundary. The main agent still owns scheduling, the ledger, and every
spawn; the Council owns acceptance decisions. Respect higher-priority
instructions and existing action permissions, and never rewrite provider
settings, agent bindings, or concurrency settings while running it.

Nothing here is host-enforced. The coordinator follows this protocol, and the
packet record lets that decision be checked afterwards. Nothing here makes a
model run, and passing the checker below is evidence about the record, not proof
that the named models executed.

## Roles and seats

- **Main agent**: owns tools, scheduling, and execution. It never accepts its own
  or a worker's work.
- **Lead planner**: one seat among the maximum of three. It organises work and
  consolidates correction and research requests. It cannot override a Council
  blocker or accept alone.
- **Reviewers**: the remaining seats. Each reviews the actual artifacts, raises
  findings with requirement ids, and disposes of every collated finding.

Reviewers do not edit implementation, spawn workers, or approve their own
output. A package author never holds a seat. Two seats may share a model; they
still need separate actors, and the same input must be given to both.

## Choose the panel

Seats are actors, so the panel is the list of reviewer actors, their models, and
their reasoning efforts. Verify each requested model and effort against the live
native subagent catalog before claiming the seat exists. If a route is missing or
rejected, say so and ask for a supported choice; never substitute silently.

- Standalone `$the-council`: **three reviewers on Sol (`gpt-6-sol`) at medium
  reasoning** unless the user selects otherwise.
- `$parallelism $the-council`: reuse the Parallelism reviewer already selected
  for the task as the lead planner, and fill the remaining seats with Sol at
  medium reasoning. Parallelism keeps its current single-reviewer behaviour when
  the Council is not requested.
- `$parallelism $the-council sol medium, astra low, deepseek high`: an explicit
  list. The first member is the lead planner unless the user names a different
  member as lead.

Record the panel, the package fingerprint, and a `panel_generation` counter.
Changing membership, or returning to single-reviewer mode, starts a new
generation: earlier votes stop counting even when the actors are unchanged, and
open findings carry across the handoff verbatim.

## Run the rounds

1. **Freeze the package.** Record a fingerprint over the requirements, the
   required checks and their criteria, and every artifact hash. The frozen
   package carries the original task and its amendments as a hashed artifact
   such as `task.json`, named in `package.task_manifest`. That artifact is
   required: without it the record cannot show which task the reviewers
   accepted, and the checker blocks.
2. **Independent review.** Every seat receives the identical package and brief
   and files its own raw findings before seeing anyone else's. Those reports are
   the independent source of truth.
3. **Collate and reconcile.** The coordinator merges the reports into one
   immutable finding set, preserves every finding id and its origin, and sends
   the same collated package to every seat. Each seat then returns a verdict plus
   a disposition for every finding.
4. **Targeted rounds, bounded.** If disagreement needs evidence, run at most two
   targeted rounds, each justified by new evidence, and reconcile inside them.
   The whole review stays inside one independent round, at least one
   reconciliation, and at most four rounds total. Without new evidence the loop
   stops.

The final round must be a reconciliation, or a targeted round that seats every
reviewer so the collation is reconciled inside it.

Disputes are settled with evidence or a targeted check, never by majority vote.
An unsupported objection gets a bounded investigation rather than a permanent
veto. Resolving or withdrawing a finding requires a recorded reason and the
evidence that settles it; a vote alone does not close a finding.

If the package changes, do not reconcile the old round against the new bytes.
Start a new packet for the new fingerprint, run a fresh independent review, and
carry the earlier findings across with their ids, origins, and evidence.

## Outcomes

Every seat returns one verdict for the same package version:

| Verdict | Meaning |
| --- | --- |
| `COMPLETE` | This reviewer accepts this package version. |
| `CHANGES_NEEDED` | A confirmed defect or failed check requires edits. |
| `EVIDENCE_NEEDED` | A finding is unresolved or the required evidence is missing. |
| `BLOCKED` | A dependency, permission, or decision outside the panel is missing. |

The Council's outcome is the most restrictive verdict the record supports:
`BLOCKED` outranks `CHANGES_NEEDED`, which outranks `EVIDENCE_NEEDED`, which
outranks `COMPLETE`. A confirmed failure outranks a request for evidence:
a failed required check or a `CHANGES_NEEDED` verdict means `CHANGES_NEEDED` even
while a dispute is still open.

`COMPLETE` needs all of: every seat returning `COMPLETE`, every vote bound to the
current package fingerprint and panel generation, every seat present in the
concluding round, every required check passed, every requirement covered, every
collated finding disposed of by every seat, no unresolved blocking finding, and
the frozen artifact bytes verified against the package directory. Optional
suggestions never block completion. A missing, timed-out, or unavailable
reviewer is not acceptance; wait, replace the seat, or ask the user to reduce the
panel. "Not checked" is not "passed".

## Keep the seats alive

The main agent owns the wait for Council seats, exactly as it does for
Parallelism workers.

Use one durable wake-up for the task - a timer event or a thread heartbeat - and
record its id with the checkpoint. Never create a timer per reviewer, and never
let a reviewer create one. If the host exposes no durable timer, fall back to
bounded native agent waits and say so rather than claiming a wake-up is
scheduled; a shell sleep or `setTimeout` does not survive the turn.

On each wake, reload the checkpoint, inspect the seats, and collect any verdict
that arrived. Call the host's completion handling on a finished child before
resuming it, and use a follow-up task rather than a bare message when a seat
needs another turn. Cancel the wake-up when the review concludes, the user stops
it, or continuation needs a user decision.

## Record and check it

Write the review to one packet, then let the bundled checker decide whether the
record supports completion:

```bash
python3 scripts/council.py council-packet.json --json \
  --package-dir /path/to/frozen-package
```

Exit status is `0` for `COMPLETE`, `1` for every other supported outcome, and `2`
when the input is unusable. Import it directly with
`council_decide(packet, package_dir="/path/to/frozen-package")`, which returns
`outcome`, `problems`, and the supporting detail without mutating the packet.
Without a package directory the checker cannot verify the frozen bytes, so it
reports `EVIDENCE_NEEDED` instead of `COMPLETE`. Acceptance must pass the exact
directory the artifact hashes were taken from.
`package_fingerprint_from_packet(packet)` recomputes the fingerprint the criteria
and artifacts imply.

Full field list and problem codes: [schema](references/schema.md).
Running order and handoff rules: [protocol](references/protocol.md).
Example packet: [template](templates/council-packet.example.json).

## What this cannot do

The checker validates the record and re-hashes the declared artifacts: shape,
cross-references, round limits and attendance, fingerprints, path containment,
coverage, and dispositions. It cannot prove that a named model ran, that a
reviewer is independent in practice, that an objection is well-founded, or that
the evidence is truthful, and an actor who rewrites every field consistently is
outside what any record check can catch. Reviewers must still read the actual
artifacts, and the whole Council runs on read-only inspection with any tests
isolated to the reviewers' own files. Report real model routing as verified only
when it was observed.

Never report `COMPLETE` from the record alone, and never let an approval from an
earlier package version, panel generation, or superseded seat count toward
acceptance.
