# Review protocol

How to run a Council, brief its seats, collate results, and hand off. The packet
fields are in [schema.md](schema.md); the short rules are in `SKILL.md`.

## Before the first round

Read the request and the repository instructions. Record the objective, the
amendments, the workspace baseline including uncommitted work, and any user
limit on time, spending, or concurrency. Freeze the package: name the artifact
that carries the original task and amendments (`task.json`), hash every artifact,
and compute the fingerprint that binds the criteria to those artifacts. The task
manifest is required; a packet without it blocks, because the record cannot show
which task the reviewers accepted. Every artifact path stays package-relative
and canonical, and no path may leave the package directory.

Acceptance is a verification, not a formality: `council_decide` and the CLI are
given `package_dir="/path/to/frozen-package"`, the exact directory the hashes
were taken from. Without it the best available outcome is `EVIDENCE_NEEDED`,
because unverified bytes are not acceptance.

Resolve each seat against the live native subagent catalog before claiming it.
`$the-council` alone means three Sol medium reviewers. Under
`$parallelism $the-council`, the reviewer already selected for the Parallelism
task takes the lead seat and Sol medium fills the rest. An explicit list such as
`sol medium, astra low, deepseek high` seats those actors in order; the first
member leads unless the user names another member. If a route or effort is
missing, report it and ask for a supported choice rather than substituting.

## Reviewer brief

Give every seat the identical brief. Include:

- the frozen package path, its hash, and the artifact that carries the task;
- the requirement list, and every amendment verbatim;
- the required checks and their acceptance criteria;
- the review round, the panel, and the package fingerprint;
- the exact output shape: a verdict, a finding id per issue with its requirement
  ids and a reproduction, and one disposition per collated finding;
- the instruction block below.

> You are one seat on a Council. Review the actual artifacts, not a summary.
> Read only; do not edit implementation, spawn agents, or approve your own
> output. Do not report a check as passed unless you ran it. Record each finding
> with a stable id, the requirement it maps to, its location, and how to
> reproduce it, and say when something was not checked. Report your first-pass
> findings before you see any other seat's findings. In the reconciliation
> round, dispose of every collated finding, including findings you disagree
> with, and explain any change of position. A disagreement is settled with
> evidence, not by counting votes. Return your packet and finish the turn.

Send the brief to each seat separately. A shared transcript between seats
destroys the independence the first round exists to create.

## Round one: independent review

Each seat receives the same package and brief and files its own findings. Record
those raw lists in `initial_reports`, one entry per seat, with the round id, the
package fingerprint, and the panel generation. `rounds[0].raised_finding_ids`
must equal the union of those lists: if the two disagree, collation has already
lost track of a finding.

Seats review read-only. Any test a seat runs must be isolated to its own files
or hold a clearly labelled local stub of an agreed interface; importing a peer's
changing file is not a review. Nothing in review authorises implementation.

## Collation

Build one immutable finding set:

- assign every finding a stable id and keep its origin seat, because attribution
  is what makes a dropped minority finding detectable;
- merge duplicates, keeping every id and every originating seat;
- state contradictions plainly rather than averaging them;
- never remove a finding to make the set tidier, and never mark one resolved on
  the strength of the votes it received.

Then send the identical collated package, with the original request and
amendments, to every seat.

## Round two: reconciliation

Each seat returns one verdict for the package version, a disposition for every
collated finding, and its reasons. Findings a seat disagrees with still get a
disposition. Where a seat changes position, the record should show which
evidence changed its mind.

Settling a finding needs a reason and evidence bound to the reviewed
fingerprint. A finding recorded as settled while a seat still stands on it is
not settled, and the record reports it rather than accepting it.

## Targeted rounds

If the panel disagrees, or a required check has not been run, run a targeted
round with `new_evidence: true`: a specific test, a bounded investigation, or a
measurement that can settle the point. At most two targeted rounds are allowed,
and the review stays inside four rounds in total. Without new evidence the loop
stops, because repeating the same disagreement does not create information.

An objection that no evidence supports gets one bounded investigation, not a
permanent veto. An unreachable or timed-out seat is not acceptance either: wait
for it, replace it at a new panel generation, or ask the user to reduce the
panel. When the last round is a targeted round, it must seat every reviewer so
the collation is reconciled inside it; there is no separate fifth round.

Every seat must take part in the concluding round. An earlier reconciliation
does not cover for an absent seat in the final one, and a verdict naming a round
the seat did not attend is rejected.

If the package changes, the earlier independent round and its reports are stale.
Do not reconcile the old findings against new bytes: open a new packet for the
new fingerprint, run a fresh independent review, and carry the earlier finding
ids, origins, and evidence forward so the history survives.

## Outcomes

Use the reviewer verdicts `COMPLETE`, `CHANGES_NEEDED`, `EVIDENCE_NEEDED`, and
`BLOCKED`. The Council outcome is the most restrictive verdict the record
supports. `COMPLETE` requires every seat accepting the same package, every
required check passed, every requirement covered, every collated finding
disposed of, and nothing blocking left open.

Optional suggestions are recorded and do not block. "Not checked" is not
"passed". A passing test suite supports review; it does not establish that the
right behaviour was implemented.

## Handoff and mode changes

The user can change the panel, or return the task to single-reviewer mode, at any
time. Record the request verbatim, add a dated `mode_history` entry naming the
round, and bump `panel_generation`. Carry every outstanding finding across with
its id, origin, and evidence, and require a fresh review of the current package
before the new authority accepts anything. Earlier decisions keep their original
attribution and stop counting for acceptance.

A changed artifact invalidates the acceptance that covered the old version.
Reopen the affected findings, or restart the review against the new fingerprint.
Returning to single-reviewer mode does not close open defects and does not
accept disputed work.

## Standalone and integrated use

Standalone `$the-council` reviews a task, plan, or artifact set and reports its
outcome. It does not authorise implementation; a verdict of `CHANGES_NEEDED` or
`EVIDENCE_NEEDED` tells the main agent what to do next, within the permission it
already has.

`$parallelism $the-council` keeps Parallelism's ledger, worker model, ownership
rules, checkpoint, and timer. One coordinator and one acceptance authority
exist at a time: single-reviewer mode uses the selected reviewer, Council mode
uses this protocol. The lead planner is one of the three seats, not a fourth,
and the Council outcome replaces the single reviewer's acceptance for the
package versions it covers. Parallelism without a Council request keeps its
current behaviour unchanged.

## Waiting for seats

The main agent owns one durable wake-up for the review: a timer event, or a
thread heartbeat when that is all the host offers. Record its id beside the
checkpoint and child ids, and use it to reload state and collect arriving
verdicts. Do not create a timer per reviewer and do not let a reviewer create
one; only the coordinator wakes the task.

If no durable timer is available, use bounded native agent waits and report the
limitation instead of claiming a wake-up is scheduled. A shell sleep or a
JavaScript `setTimeout` does not survive the turn. On a wake, mark finished
children complete with the host's completion handling before resuming them with
a follow-up task, and never interrupt a healthy seat. Cancel the wake-up when
the review concludes, the user stops it, or the next step needs a user decision.
