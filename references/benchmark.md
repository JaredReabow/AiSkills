# Benchmark fixture

A small disposable two-module Python task with explicit requirements, split file
ownership, tests that fail against the shipped stubs, and an integration check.
It is the smallest run that still shows whether a team keeps ownership straight,
records honest evidence, and finishes integration.

`scripts/benchmark.py` never calls a model API or CLI. It generates files and
grades the files that came back. Nothing in the graded workspace is executed as a
shell command: every graded command is fixed inside the helper. Those fixed
commands do run the graded `unittest` modules and `integration_check.py` as
ordinary Python with the host's usual permissions, so grade only a workspace you
are willing to run. The standard-library check parses imports statically with
`ast` and sees only static `import` and `from ... import` statements; a dynamic
import inside a function is not detected.

## Generate and grade

```bash
SKILL=/Users/leo/.codex/skills/parallelism
BENCH=$(mktemp -d)/bench

python3 "$SKILL/scripts/benchmark.py" generate --dest "$BENCH"
python3 "$SKILL/scripts/benchmark.py" grade --workspace "$BENCH/task" \
    --out "$BENCH/report.json"
```

`generate` writes only into a new or empty directory. It refuses any destination
that exists and holds files, and any destination that is the skill package, sits
inside it, or is one of its ancestors. There is no replace or force option;
choose a new directory. `grade` exits `0` only when every requirement passes and
the shipped checks are intact.

Before the trial, `grade` on the fresh fixture fails `R1`-`R4` and passes `R5`.
That is the expected starting state; it proves the tests are real.

To sanity-check the grader itself, copy `reference/textstats.py` and
`reference/report.py` over the workspace modules and grade again: all five
requirements must pass. The reference solution exists for that check and is
never applied automatically.

```bash
cp "$BENCH/reference/textstats.py" "$BENCH/reference/report.py" "$BENCH/task/"
python3 "$SKILL/scripts/benchmark.py" grade --workspace "$BENCH/task"
```

## What is in the fixture

| Path | Contents |
| --- | --- |
| `brief.md` | The worker brief: frozen interface, roles, ownership, acceptance. |
| `requirements.json` | `R1`-`R5`, each mapped to the checks that demonstrate it and to an owner. |
| `task/textstats.py` | Worker-a's deliverable; ships raising `NotImplementedError`. |
| `task/report.py` | Worker-b's deliverable; imports `textstats`, ships raising. |
| `task/tests/` | Shipped tests for both modules. Editing them invalidates the grade. |
| `task/integration_check.py` | Cross-module check owned by the integrator. |
| `reference/` | Reference modules used only to validate the grader. |
| `fixture_hashes.json` | Generated digests of the shipped checks; detects tampering. |

`fixture_hashes.json` is a copy for transparency, never the source of truth.
Grading compares the workspace against the canonical digests of
`templates/benchmark/task/**` inside the skill package, including
`tests/__init__.py`. The integrity check runs **before** anything from the
workspace is executed: a missing manifest, a manifest that is not a JSON object
(including `null`), a manifest that disagrees with the canonical digests, or a
deleted or modified shipped check stops the grade and marks every requirement
`not_run`. That is why a tampered local manifest cannot relax grading.

Requirements map to checks, so progress is visible per requirement:

| Requirement | Check |
| --- | --- |
| `R1`, `R2` (word statistics) | `tests.test_textstats` |
| `R3` (report rendering) | `tests.test_report` |
| `R4` (integration) | `integration_check.py` exit status |
| `R5` (standard library only) | AST import scan of both modules |

Applying only the `textstats` reference passes `R1`, `R2`, and `R5` while `R3`
and `R4` stay failing. That granularity is deliberate: a single "tests" bucket
would hide which half of the task is actually done.

## Suggested roles and brief

Give Astra and the workers the contents of `brief.md` plus the ledger. The short
version:

| Role | Owns | Acceptance |
| --- | --- | --- |
| worker-a (DeepSeek) | `task/textstats.py` | `R1`, `R2` |
| worker-b (DeepSeek) | `task/report.py` | `R3` |
| verifier (DeepSeek, different actor from the author) | read-only | reproduce `R4` from a clean run |
| Astra | review and acceptance | confirm `R1`-`R5` against the diff and the recorded evidence |

The two modules can be written in parallel because the interface is frozen in
`brief.md` before either worker starts. That is the point of the fixture: the
shared contract is agreed up front, so ownership stays clean.

## Recording observations

Lifecycle events, transcripts, and human review notes go in a separate JSON file:

```bash
python3 "$SKILL/scripts/benchmark.py" grade --workspace "$BENCH/task" \
    --observations observations.json --out "$BENCH/report.json"
```

They are echoed under `manual_observations` and never parsed into a pass or fail.
A transcript claiming success does not move a failing grade; the files do.

## Ledger scenarios

Run the same trial as a ledger. `tests/test_workflow.py` already covers the four
failure modes worth watching for, by name:

| Scenario | Test |
| --- | --- |
| Missing information | `test_scenario_missing_info_is_surfaced_not_guessed` |
| Success claim contradicted by evidence | `test_scenario_contradictory_success_claim_is_rejected` |
| Conflicting writes | `test_scenario_conflicting_writes_is_rejected` |
| Revised artifact invalidating acceptance | `test_scenario_revised_artifact_invalidates_acceptance` |

## Drawing conclusions

One run is an anecdote. Before claiming the team is faster or better than a
single agent:

1. Run the same fixture at least three times with the team, and three times with
   one agent working alone on the same brief.
2. Record per run: requirement outcomes, wall-clock time, total workers,
   simultaneous peak, correction rounds, and provider-reported usage when the
   provider supplies it.
3. Keep unknown values `null`. Do not convert missing usage data into estimated
   savings.
4. Compare the distributions, then state the comparison and its limits.

The fixture measures correctness and coordination on a small, well-specified
task. It does not measure ambiguous requirements, large codebases, or long
horizon plans, and a passing grade is not evidence that a model ran or that
routing was verified.
