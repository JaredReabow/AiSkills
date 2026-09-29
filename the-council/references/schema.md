# Packet schema

The Council's record is one JSON packet. The execution rules live in
`SKILL.md`; the running order and handoff rules live in
[protocol.md](protocol.md); a complete example lives in
`templates/council-packet.example.json`.

`scripts/council.py` is the reference reader. It is standard-library only,
read-only, deterministic, and never mutates the packet it is given.

## Shape contract and exit status

The checker separates unusable input from a decidable-but-failing record:

| Condition | Result |
| --- | --- |
| Not JSON, not an object, unsupported `schema_version`, or a missing/wrong-typed top-level section | `CouncilInputError`; CLI exit `2` |
| Any other defect | listed in `problems`; CLI exit `1` |
| Record supports completion **and** the frozen artifact bytes verify | `outcome: COMPLETE`; CLI exit `0` |

Anything that is not `COMPLETE` fails closed. Integrity problems force
`BLOCKED`; a failed required check or a confirmed `CHANGES_NEEDED` verdict
forces `CHANGES_NEEDED`; missing evidence, an unresolved blocking finding, or a
disputed settlement asks for `EVIDENCE_NEEDED`; an unverified package is always
`EVIDENCE_NEEDED` at best; a reviewer `BLOCKED` verdict outranks all of them.

`council_decide(packet)` with no `package_dir` cannot verify the frozen bytes, so
it never returns `COMPLETE`. Acceptance calls must pass
`package_dir="/path/to/frozen-package"`, the exact directory the artifact hashes
were taken from.

## Top level

| Field | Type | Meaning |
| --- | --- | --- |
| `schema_version` | integer | Must be `1`. |
| `council_id` | id | Stable identifier for this review. |
| `package` | object | The frozen package under review. |
| `requirements` | list | `{id, text}`, the original requirements. |
| `required_checks` | list | `{id, text, criteria, status}`. |
| `integration` | object | Mode, lead planner, panel generation, change history. |
| `panel` | list | One to three `{actor, model, reasoning_effort, role, unavailable?}`. |
| `rounds` | list | Ordered review rounds. |
| `initial_reports` | list | Each seat's raw first-pass finding ids. |
| `findings` | list | The immutable collated finding set. |
| `reviewer_finals` | list | One verdict per seat, with a disposition per finding. |

## `package`

| Field | Meaning |
| --- | --- |
| `id`, `version` | Package identity and revision label. |
| `fingerprint` | Must equal `package_fingerprint_from_packet(packet)`. |
| `task_manifest` | **Required.** Path of the hashed artifact that holds the original task and its amendments, usually `task.json`. Must be one of `artifacts`; when it is absent the packet blocks with `task_manifest_missing`. |
| `produced_by` | Actors that produced the package. No producer may hold a seat. |
| `artifacts` | `[{path, sha256}]`. Re-hashed against `--package-dir`, which is required for `COMPLETE`. |

Artifact paths must be canonical and package-relative: no absolute path, no
drive letter, no backslash separator, no empty, `.` or `..` segment, and no
duplicate entry for one path. Paths are rejected before anything is read, and a
path whose resolved target leaves the package directory - including a symlink
that points outside - is rejected as well. A symlink that stays inside the
package is fine. A missing file, a directory where a file is expected, a
permission error, and a changed digest are each recorded as a problem rather
than treated as a pass.

`fingerprint` is a SHA-256 over a canonical JSON manifest of the requirement
list, the required-check list with its criteria, and the artifact hashes. It
binds the acceptance criteria to the vote: editing a requirement or a check
after the reviewers voted changes the expected fingerprint, so the record stops
being `COMPLETE` instead of silently accepting work against a different task.

It does not hash the package bytes itself; the artifact entries do that, under
`--package-dir`. It also cannot stop an actor who rewrites every field of the
record to agree with itself.

## `required_checks`

`status` is `passed`, `failed`, or `not_run`. Only `passed` supports completion.
A `failed` check means `CHANGES_NEEDED`. A `not_run` check means
`EVIDENCE_NEEDED`; "not checked" is never "passed". `criteria` states what the
check demonstrates, and is part of the fingerprint.

## `integration`

| Field | Meaning |
| --- | --- |
| `mode` | `single` or `council`. |
| `lead_planner_actor` | Required in `council` mode; must occupy one of the seats. |
| `single_reviewer_actor` | Required in `single` mode; must be the seated reviewer. |
| `panel_generation` | Integer of at least `1`. Bumped whenever the panel changes. |
| `mode_history` | `[{at_round, mode, preserved_finding_ids}]`. |

Every first-pass report and every verdict carries `panel_generation`. A vote
from an earlier generation is rejected even when the actor is unchanged, so
re-forming the panel cannot recycle old approvals. A generation above `1`
requires a `mode_history` entry; that entry must preserve every finding already
outstanding at its round, and a later round must review the current package.

## `panel`

One to three entries; a fourth is rejected. `actor` values must be unique:
same-model peers still need separate actors, because one actor cannot file two
independent reviews. `role` is `lead` or `reviewer`. `unavailable: true`
records a missing or timed-out seat and blocks completion. The checker does not
verify that a model name is routable; that is a live-catalog check for the
coordinator.

## `rounds`

| Field | Meaning |
| --- | --- |
| `id` | Round identifier. |
| `kind` | `independent`, `reconciliation`, or `targeted`. |
| `package_fingerprint` | The package version this round reviewed. |
| `new_evidence` | Must be `true` on a `targeted` round. |
| `reviewers` | Seats that took part. |
| `raised_finding_ids` | Finding ids first raised in this round. |

Limits: exactly one `independent` round and it comes first; at least one
`reconciliation` round; at most two `targeted` rounds; at most four rounds
total. Every seat must appear in the first independent round and in every
concluding round. The concluding round must run against the current fingerprint,
must be a reconciliation or a targeted round, and must seat every reviewer: an
earlier reconciliation does not substitute for the final one, and a verdict
from a seat that did not attend the concluding round is rejected
(`round_attendance`).

Every round must run against the current fingerprint. If the package changed,
the earlier independent round and its reports are stale
(`round_stale_history`, `initial_report_age`) and the record blocks: a changed
package needs a fresh independent review in a new packet that carries the
original finding history, not a reconciliation of the old round.

## `initial_reports`

`{actor, round_id, package_fingerprint, panel_generation, finding_ids}` — one
entry per seat, filed at the independent round. This is the independent source
of truth. `rounds[0].raised_finding_ids` must equal the union of these lists.

A finding id that appears in a first-pass report but not in `findings` is a
`collation_dropped_finding`; a collated finding that no report and no round
lists is a `finding_unattributed`. Together these catch a collator that drops a
minority finding from the collated set and the verdicts at the same time, and a
finding that was invented after collation. A finding raised at the independent
round must also be attributed to a seat whose own report lists it.

## `findings`

| Field | Meaning |
| --- | --- |
| `id` | Stable finding id, preserved from the source report through settlement. |
| `origin_actor` | The seat that raised it. |
| `raised_at_round` | The round that first recorded it. |
| `requirement_ids` | At least one declared requirement. |
| `classification` | `blocking` or `suggestion`. |
| `status` | `open`, `resolved`, or `withdrawn`. |
| `summary` | What is wrong, where, and how to reproduce it. |
| `resolution` | Required when settled: `{reason, evidence, package_fingerprint}`. |

`resolved` and `withdrawn` need a reason and evidence bound to the reviewed
package version; a vote alone does not settle a finding, and a settlement from
another fingerprint is stale. A blocking finding that stays `open` keeps the
outcome at `EVIDENCE_NEEDED`. A blocking finding recorded as settled while any
seat still stands on it produces `finding_dispute_unresolved`. Optional
suggestions never block completion.

## `reviewer_finals`

`{actor, round_id, package_fingerprint, panel_generation,
covered_requirement_ids, verdict, outcomes}` with `outcomes` holding one
`{finding_id, disposition}` per collated finding.

Every seat returns exactly one verdict, in the concluding round, against the
current fingerprint and generation, covering every requirement, and disposing
of every collated finding. A missing seat, a stale vote, a dropped finding, or
an uncovered requirement fails closed.

Dispositions are `resolved`, `withdrawn`, `not_applicable`, `accepted`, and
`disputed`. The last two still assert that a blocking finding stands, so they
prevent completion while the record calls it settled.

## Report and problem codes

`council_decide(packet, package_dir=None)` returns:

| Key | Meaning |
| --- | --- |
| `ok` | True only when `problems` is empty. |
| `outcome` | `COMPLETE`, `CHANGES_NEEDED`, `EVIDENCE_NEEDED`, or `BLOCKED`. |
| `problems` | One `code: where: message` string per reason completion is not supported. |
| `notes` | Non-blocking observations, such as unverified artifacts. |
| `unresolved_blocking_finding_ids` | Blocking findings still open. |
| `disputed_finding_ids` | Settled blocking findings a seat still stands on. |
| `failed_required_check_ids` | Checks that ran and failed. |
| `not_run_required_check_ids` | Checks that never ran. |
| `reviewer_verdicts` | Verdict per seat. |
| `package_fingerprint`, `package_verified`, `panel_size` | Context. |
| `provenance_note` | What the checker does not prove. |

Codes are stable and safe to assert on. The full set that forces `BLOCKED`
includes `panel_size`, `duplicate_actor`, `author_self_review`,
`lead_not_on_panel`, `authority_mismatch`, `unavailable_reviewer`,
`panel_generation`, `stale_panel_vote`, `round_order`, `round_conclusion`,
`round_attendance`, `round_stale_package`, `round_stale_history`,
`targeted_limit`, `round_limit`, `targeted_without_evidence`,
`initial_report_shape`, `initial_report_actor`, `initial_report_round`,
`initial_report_age`, `collation_dropped_finding`, `finding_unattributed`,
`finding_attribution`, `finding_duplicate_id`, `finding_unknown_requirement`,
`finding_unknown_round`, `resolution_stale_package`, `final_missing_reviewer`,
`final_stale_package`, `final_round_invalid`, `final_missing_requirement`,
`final_dropped_finding`, `final_duplicate_outcome`, `fingerprint_mismatch`,
`task_manifest_missing`, `check_status`, `verdict_unknown`,
`disposition_unknown`, `artifact_path_unsafe`, `artifact_unreadable`,
`artifact_hash_mismatch`, `artifact_missing`, and `artifact_hash_format`.

Decision codes that force an incomplete but non-blocking outcome include
`artifacts_unverified`, `check_failed`, `check_not_run`,
`unresolved_blocking_finding`, `finding_dispute_unresolved`,
`resolution_missing_reason`, `resolution_missing_evidence`, and
`verdict_not_complete`.
