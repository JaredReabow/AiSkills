# Install Parallelism

This guide installs the skill and explains the separate setup needed for its
models. The default team is your selected main model, a Sol reviewer with medium
reasoning, and DeepSeek V4.1 Flash workers. All must be usable through the host's
native subagent tools. Installing the Markdown files alone does not enable
model routes.

Instructions were checked on **2026-09-29** against the linked official Codex
documentation and Codex Router documentation. Router and host behavior can
change; check the upstream instructions when commands differ from your version.

## 1. Prepare Codex and the router

Install and sign in to your chosen Codex surface using
[OpenAI's Codex documentation](https://learn.chatgpt.com/docs).
Confirm that the session exposes native subagent creation, messaging, and status
tools. Access to the requested reviewer model and reasoning effort is also
required; installing this skill does not add that access.

For DeepSeek, this guide uses
[Codex Router](https://github.com/duolahypercho/codex-router), a community project
maintained independently of OpenAI. An existing integration is sufficient if it
already supports the required native child routes. Do not install a second
router over a working setup just to install this skill.

### macOS: Homebrew router/CLI installation

With [Homebrew](https://brew.sh/) already installed:

```sh
brew tap duolahypercho/codex-router https://github.com/duolahypercho/codex-router
brew install codex-router
codex-router setup --guided
```

This installs the router CLI and its formula dependencies. It does not install
the tray or Control Center. `codex-router` is not in Homebrew's core repository,
so the tap command matters. There is no supported `npm install codex-router`
recipe. See [the upstream Homebrew instructions](https://github.com/duolahypercho/codex-router#homebrew-macos-or-linux).

### Alternative: inspect and install from source

Install [Git](https://git-scm.com/downloads),
[Node.js](https://nodejs.org/en/download) 22.19+ (upstream recommends 24 LTS),
and either [uv](https://docs.astral.sh/uv/getting-started/installation/) or
[Python](https://www.python.org/downloads/) 3.10+ with `venv` support. Use a stable
directory: the router service remembers the checkout's absolute path.

On macOS or Linux, after reviewing the cloned project's installation instructions:

```sh
git clone https://github.com/duolahypercho/codex-router.git
cd codex-router
./install.sh --target codex --guided --no-tray
```

On Windows, clone the same repository, review it, and use PowerShell:

```powershell
git clone https://github.com/duolahypercho/codex-router.git
Set-Location codex-router
./install.ps1 -Target codex -Guided -NoTray
```

The optional UI uses `--with-tray` or `-WithTray` instead. Building that companion
on macOS requires full Xcode; standalone Command Line Tools are insufficient.
The router can be used without the companion. Upstream covers macOS/Windows App
and CLI, and Linux CLI; read its
[installation guide](https://github.com/duolahypercho/codex-router/blob/main/docs/INSTALL.md)
for platform-specific prerequisites and upgrades.

## 2. Connect DeepSeek

Create or use your account at [DeepSeek Platform](https://platform.deepseek.com/),
create an API key there, and ensure the account has usable API quota. See the
[official API introduction](https://api-docs.deepseek.com/guides/agent_integrations/openclaw)
for the provider's authentication and API details.

The router handles API calls; this skill does not require an OpenAI or DeepSeek
SDK. Keep credentials in the provider's local setup flow, outside chat messages,
the skill repository, and task ledgers. The provider receives the prompts and
tool context routed to it. DeepSeek billing and the chosen reviewer's usage
limits are separate from the skill itself.

For a Homebrew install:

```sh
codex-router providers
codex-router providers enable deepseek
codex-router provider-key deepseek set
codex-router doctor
```

`provider-key deepseek set` uses a hidden local prompt. Guided setup may already
have completed this step. In a source checkout, the equivalent command prefix
is `./bin/model-router codex`, for example:

```sh
./bin/model-router codex provider-key deepseek set
./bin/model-router codex doctor
```

Select the desired DeepSeek model in the router's model catalog as well as
enabling its provider. Parallelism's default worker route is
`deepseek/deepseek-v4.1-flash`; this is a router ID, not a promise that the direct
DeepSeek API uses the same name. Fully quit and reopen Codex after publishing
catalog changes. Consult the [router configuration guide](https://github.com/duolahypercho/codex-router#readme)
when the route is absent from your installed version.

## 3. Verify native subagent routes

This is a compatibility check, not just a model-picker check. The intended team
requires **your chosen main model to create both the selected reviewer and
DeepSeek workers**, with the requested model actually executing each child.
It also needs follow-up messages and returned results to work.

The router exposes subagent selection separately from ordinary model selection.
First inspect the route without making an inference request. With a Homebrew
installation:

```sh
codex-router control subagents explain deepseek/deepseek-v4.1-flash
```

In a source checkout:

```sh
./bin/control subagents explain deepseek/deepseek-v4.1-flash
```

The `explain` command reports selection, publication, and generated
agent-definition state. Follow its specific findings. If the route is supported
but needs enabling, use the Control Center's Subagents control, or:

```sh
codex-router control subagents set deepseek/deepseek-v4.1-flash on
```

For a source checkout, use `./bin/control subagents set` with the same arguments.
**Enabling can launch the router's background compatibility probe and consume
provider quota.** It changes configuration and is not itself a successful
delegation test. Let its result settle, inspect `explain` again, and restart
Codex after catalog changes. If it rejects the route, investigate the reported
restriction rather than forcing a catalog flag.

These command prefixes follow the inspected
[router control implementation](https://github.com/duolahypercho/codex-router/blob/main/src/control.mjs).
Some upstream prose uses `model-router codex subagents`, but that command is
absent from the inspected wrapper's command list. Use the control entry point
above; command availability may differ in other releases.

The [router's native-subagent guide](https://github.com/duolahypercho/codex-router/blob/main/docs/SUBAGENT-CERTIFICATION.md)
distinguishes eligibility from live delegation. In the documented version,
eligible routes publish as `multiAgentVersion: "v2"` and have agent definitions;
`v1` is not a working fallback for native delegation. The same guide records
unresolved certification for a ChatGPT-authenticated signed-provider path.
Turning on signed routing alone is not proof that this workflow works.

Some router setups bind all children to the routed parent's model. That setup
cannot satisfy a request for a different reviewer and worker model merely by
loading Parallelism. If a route is rejected or model selection is inherited,
stop and resolve compatibility or choose a supported model arrangement explicitly.
Do not silently substitute models or use separate user-owned Codex tasks as a
replacement for native children.

After installation, use an authorized small task to verify creation, returned
results, and a same-child follow-up for each required route. Inspect host routing
metadata or execution evidence, rather than a model's statement of its own
identity. Live checks consume model quota. The local harness below does not run
these checks, and this repository makes no universal compatibility claim.

## 4. Install the skill files

Current [Codex skill documentation](https://learn.chatgpt.com/docs/build-skills)
lists `~/.agents/skills` for user skills and `.agents/skills` in a repository for
project skills. Some existing installations, including the original development
setup for this skill, use `~/.codex/skills` or a host-specific `$CODEX_HOME/skills`.
Use the directory your host discovers and maintain **one copy** of Parallelism.

### New user-wide installation: macOS or Linux

First check whether `parallelism` is already installed. If the destination
exists, follow the update steps instead of cloning over it.

```sh
mkdir -p "$HOME/.agents/skills"
git clone https://github.com/JaredReabow/AiSkills.git "$HOME/.agents/skills/parallelism"
```

### New user-wide installation: Windows PowerShell

```powershell
$skillsRoot = Join-Path $env:USERPROFILE '.agents/skills'
New-Item -ItemType Directory -Force -Path $skillsRoot | Out-Null
git clone https://github.com/JaredReabow/AiSkills.git (Join-Path $skillsRoot 'parallelism')
```

The resulting layout must be `parallelism/SKILL.md`, with the `agents`,
`references`, `scripts`, `templates`, and `tests` directories beside it. The
repository root already is the skill: do not add another nesting level or copy
only `SKILL.md`, because it links to the supporting files.

For a legacy host, substitute its discovered skills directory in the clone
command. For a project-only install, place the same folder under that project's
`.agents/skills`. Avoid creating duplicate user and project installations with
the same name. Restart Codex if the skill is not discovered, then invoke
`$parallelism` from a new task.

## 5. Run local checks

### Install the optional Council skill

Skip this step to keep using Parallelism's existing single-reviewer workflow.
The companion's source is `the-council/` inside this repository. It needs its
own discoverable skill path; keep one canonical source rather than maintaining
independent copies. For the new-install path used above, create a sibling link
on macOS or Linux, only if the destination does not already exist:

```sh
ln -s "$HOME/.agents/skills/parallelism/the-council" "$HOME/.agents/skills/the-council"
```

For an existing legacy installation under `~/.codex/skills`, use:

```sh
ln -s "$HOME/.codex/skills/parallelism/the-council" "$HOME/.codex/skills/the-council"
```

On Windows, a directory junction can expose the same source without copying:

```powershell
$skillsRoot = Join-Path $env:USERPROFILE '.agents/skills'
New-Item -ItemType Junction -Path (Join-Path $skillsRoot 'the-council') -Target (Join-Path $skillsRoot 'parallelism/the-council')
```

Inspect any existing destination rather than overwriting it. Skill discovery
behavior can vary by host: if the nested source is already listed, do not create
a second independent copy. References resolving to the same source should be
treated as the same skill. Restart or start a new task if discovery needs a refresh.

For standalone Council use, install just the repository's `the-council/` folder
into your discovered skills directory using the skill installer, or link that
folder from a checkout outside the discovery directory. Include all its files.
It does not require Parallelism, DeepSeek, or Codex Router unless your selected
models require those integrations.

Use `$parallelism $the-council` for collective acceptance during a build, or
`$the-council` to review existing work. Supply one to three comma-separated
model/effort entries after the Council mention to select its panel. Plain
`$parallelism` and `$parallelism astra low` remain single-reviewer invocations.

### Validate the installation

From the installed `parallelism` folder, using Python 3.10+:

```sh
python3 --version
python3 tests/run_harness.py --quiet
```

On Windows, use a configured Python 3.10+ interpreter, for example
`py -3 tests/run_harness.py --quiet` after checking `py -3 --version`.

The current package runs **148 standard-library tests** over temporary files.
These cover the ledger, scheduling, fixture grading, and associated invariants.
The tests do not call a model API or require provider credentials.
The combined harness also runs the bundled Council suite when present. Run
`python3 the-council/tests/run_harness.py` from the repository root to check that
companion alone, or add `--skip-council` to the root harness to isolate the
original Parallelism stages. A standalone Parallelism install without the
companion reports its suite as skipped; it remains usable.

The harness also looks for Codex's bundled `skill-creator` packaging validator
in its known `.codex/skills/.system` locations. If absent, that stage is reported
as **SKIPPED**. If present, it runs using the same Python interpreter and requires
PyYAML. A missing PyYAML import makes that stage fail, even if unit tests pass.

For an isolated optional validation environment outside the skill directory:

```sh
python3 -m venv "$HOME/.venvs/aiskills-checks"
"$HOME/.venvs/aiskills-checks/bin/python" -m pip install pyyaml
"$HOME/.venvs/aiskills-checks/bin/python" tests/run_harness.py --quiet
```

On Windows the venv interpreter is under `Scripts/python.exe` instead of `bin/python`.
Installing PyYAML does not install the bundled validator itself. To deliberately
run only the standard-library checks:

```sh
python3 tests/run_harness.py --quiet --skip-skill-validation
```

This option skips packaging checks for both suites. Record skipped packaging
validation separately; a passing unit suite does not
establish that the optional stage ran.

## 6. Use it and check the first task

Try a bounded task with clear ownership, such as:

```text
$parallelism sol medium
Add two independent documentation examples and check their commands.
Use at most two concurrent workers. Report the actual child routes and evidence.
```

Check that the main agent records the reviewer choice, uses distinct reviewer
and worker actors, gives workers non-conflicting ownership, and sends completed
work for review. The host must expose the requested routes; a completed empty
turn is not evidence that an agent did the work. Then check that the reviewer
inspects actual artifacts and that the main agent reports final checks.

If the host supports durable timers or task heartbeats, the main agent schedules
one while children are outstanding and cancels or pauses it when finished.
Otherwise, expect bounded native waits and an explicit limitation. Installing
this skill does not add a timer service.

To switch reviewers, send `$parallelism astra low` during the task. Expect a new
reviewer with an evidence handoff; existing independent workers can continue.

### Make it a project preference

Explicit invocation is the clearest way to request the team. To make it the
normal approach for substantial work in a project, you can add this instruction
to that project's `AGENTS.md`:

```text
Use the parallelism skill for substantial work that benefits from independent
workers and review. Default the reviewer to Sol medium unless I select another
model or effort. Skip trivial single-agent work and respect host capabilities.
```

This is an instruction preference; it does not create a model route or force
delegation for every turn. Installation does not modify `AGENTS.md` for you.

## Update or remove

In your actual installed folder (use its legacy path if applicable):

```sh
git status --short
git remote -v
git pull --ff-only
python3 tests/run_harness.py --quiet
```

Before pulling, inspect any local modifications and preserve them with a commit
or backup outside the skill discovery directories. If Git reports conflicts or
divergent history, resolve them deliberately; do not reset away local work. An
older installation without `.git` should be backed up outside those directories
before cloning a fresh copy into the chosen skill path.

To remove the skill, move its folder outside all discovered skills directories
and restart Codex if needed. Pause any task heartbeat it created for unfinished
work. Removing the skill does not uninstall Codex Router, revoke provider keys,
or change model settings. Manage those separately through their own tools.
If you created a Council discovery link, remove that link before moving its
source; do not recursively delete through a link. Removing only the Council link
does not change Parallelism's default workflow, although hosts that discover
nested skills may still list the bundled source.

## Troubleshooting

| Symptom | Check or next action |
| --- | --- |
| `$parallelism` is missing | Confirm the discovery directory and `parallelism/SKILL.md` layout, remove duplicate discovery copies, and restart Codex. |
| Sol or Astra is unavailable | Inspect the native subagent catalog. Select an available reviewer explicitly; installing files cannot grant model access. |
| DeepSeek is missing | Check provider enablement, credentials, selected models, catalog publication, and restart. |
| DeepSeek is selectable but cannot spawn | Inspect `subagents explain`, the generated role, and the host's native tools. A picker entry is not delegation proof. |
| Every child uses the main model | The active integration may pin child routing. The requested mixed-model team is unavailable until that restriction is resolved. |
| Child creation fails under ChatGPT authentication | Consult the router's certification limitations for the exact host/provider combination. Do not assume signed routing or a catalog toggle fixes it. |
| API authentication or quota error | Check the configured provider account locally. Do not put its key in chat or a ledger. |
| The agent cannot schedule a wake-up | Use bounded native waits and report the missing durable timer capability. |
| Python syntax/import errors | Confirm Python 3.10+ and use the same interpreter for the harness and optional dependencies. |
| Packaging fails with `No module named yaml` | Install PyYAML in the validation environment, or explicitly skip and report the optional stage. |
| Workers collide or the reviewer is overloaded | Reduce ready work, isolate shared resources, and inspect the ledger's ownership/dependency and backlog findings. |

Return to the [workflow overview](../README.md) or read the complete
[agent instructions](../SKILL.md).
