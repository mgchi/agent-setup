# Agent Setup

[![Checks](https://github.com/mgchi/agent-setup/actions/workflows/ci.yml/badge.svg)](https://github.com/mgchi/agent-setup/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Portable context and independent Claude/Codex advice for your coding agents.

Keep preferences and project knowledge in a local directory. Load a small, relevant
packet when work starts. Let the lead agent ask independent advisers for feedback,
then save supported decisions and lessons for the next session.

The repository contains the runtime, installer, tests, and blank templates. Your
knowledge and account settings stay in your own directories.

## What you get

- **Context across sessions.** Shared personal preferences and separate project
  knowledge, including support for Git worktrees.
- **Independent advice.** Claude and Codex can assess a task and review a candidate
  answer. The lead owns edits, testing, and the final decision.
- **Graceful failures.** Bounded waits, provider cooldowns, and error handling let
  the lead finish when an adviser is unavailable.
- **Bounded memory.** Evidence-backed checkpoints, stable keys, retention limits,
  and compaction keep the store manageable.
- **Portable setup.** An installer that preserves existing settings and private
  state, with isolated installation and repeat-install checks.
- **Temporary adviser sessions.** Claude workers use `--no-session-persistence`;
  Codex workers use `--ephemeral`.

This is an early release. macOS has been exercised locally. The CI matrix checks
the isolated runtime on macOS and Linux; native Windows is unsupported. Conductor
desktop integration requires macOS. Hosted models, CLI capabilities, and hook
support depend on your installed clients and accounts.

## Quick start

You need Git and [uv](https://docs.astral.sh/uv/getting-started/installation/).
On macOS, install uv with `brew install uv`.

Start with a signed-in Claude Code or Codex client. A local Conductor session
can use the same setup. Choose a short project name and the path to the
repository where you want to work. Replace `my-project` and
`/path/to/project` below.

```sh
git clone https://github.com/mgchi/agent-setup.git
./agent-setup/install --brief
~/.local/bin/agent-context project add my-project /path/to/project --brief
```

Open a new session **in that project** and give it a normal task. In Codex,
approve the installed hooks in `/hooks` if prompted. The first project
prompt through each client emits **“Project context loaded for my-project.
You're ready.”** New registrations start with context alone.

If your client hides hook messages, check from the project directory:

```sh
~/.local/bin/agent-context status --brief
```

This names the project and the clients whose prompt hooks have run. To check
one client, use `status --client codex` or `status --client claude`. While
waiting, it gives the next step. Running a manual task or opening a session
alone does not create a prompt receipt.

The remaining examples use `agent-context` on your PATH. Add
`export PATH="$HOME/.local/bin:$PATH"` to your shell startup file if needed.

The installer uses Python 3.14 and pinned dependencies. It adds runtime commands,
workflow skills, managed instruction sections, and hooks for session entry,
prompts, tool use, and completion. On macOS it also schedules local maintenance
at login and 04:15. Existing lead model settings are preserved.

### Try it in a project

Start a new agent session in the registered project and give it a normal task.
For a durable preference, state it explicitly, for example:

> I prefer to reuse this project's existing components.

The workflow asks the lead to checkpoint supported preferences and decisions.
It also recognizes standalone statements such as “I want my workflow to stay
predictable.” The lead checks that the complete statement describes a standing
preference. One-task requests stay in the current task.
Check `agent-context status --cwd /absolute/path/to/project` and review the local
context files to confirm what was retained. Model compliance and client hook
support affect automatic capture; the store is editable and inspectable.

## How it works

```text
Your prompt + local context
            |
         Lead agent ------ sanitized task/evidence ------ Advisers
            |                                             |
            +--------------- optional feedback -----------+
            |
      Edits and validation
            |
    Evidence-backed checkpoint
            |
      Local context store
```

The lead loads relevant context, performs the work, reconciles available adviser
feedback, and checkpoints explicit preferences, accepted decisions, or verified
lessons. Workers are tool-free and cannot authorize actions. They receive
sanitized task, candidate, and evidence text. Failed or pending advice does not
block completion after the bounded wait.

When the receipt selects no advisers, stage one JSON file under the run's
`task-inputs/` directory. The only required field is the proposed outgoing
answer. A local completion with separate evidence can look like this:

```json
{"answer": "The sample export passed.", "evidence": "The exported file contained both fixture records."}
```

Run `agent-context checkpoint --run <id> --local-file <file>`, delete that
individual input after success, then run `agent-context finish --run <id>`.
The checkpoint screens the original task, answer and evidence locally. Supply
a sanitized `request` if the original task was withheld. An optional
`checkpoint` object accepts the same memory drafts and note as `--file`.
This path creates no review artifacts or provider calls.

For a paused task, a short local handoff can capture the decision, the last
verified checkout and the next authorized action. Three paired replays of
public changes produced the same first actions with a plain note and a labeled
one. [Handoff experiment](experiments/handoff-pilot.md)

Adviser defaults are `claude-fable-5-2` and `gpt-6.2-astra` at maximum effort.
`claude-fable-5-2` needs Claude Code 2.1.290 or newer, and `gpt-6.2-astra` needs
Codex CLI 0.166.0 or newer. Both advisers can incur provider usage charges. A
missing model or exhausted provider does not prevent the lead from finishing.

A new installation saves `toolkit-default` for each model and effort, so
updating the toolkit also moves it to newer defaults. An installation that saved
a model keeps it, and `./install` says when a saved model differs from the
default. Choose and check models with:

```sh
agent-context adviser list                           # models, effort, and client releases
agent-context adviser set codex --model gpt-6-astra  # pin a model your account supports
agent-context adviser set claude --effort high
agent-context adviser reset codex                    # follow the toolkit default again
agent-context adviser test                           # one small real call per adviser
```

`adviser list` flags a client that is too old for the default model. `adviser
test` uses each adviser's model and effort exactly as a task would.

Add `--project my-project` to `adviser list`, `set`, `reset`, or `test`
to inspect or change a registered project's choices. For example,
`agent-context adviser set codex --project my-project --effort high` changes
only that project's Codex effort. A project reset inherits the machine
settings. An explicit `team-codex-model:` or `team-codex-effort:` line takes
precedence for one task. Saved effort accepts low, medium, high, xhigh, max,
and toolkit-default. Individual clients and models determine what they support.

`review --wait` and `collect --wait` use `adviser_wait` (50 seconds).
Use `--wait 300` for an individual longer review. The provider call timeout
also caps the budget. A call returns within 90 seconds. If `wait_again`
is true, repeat `collect` with the same wait option to use the remaining
time. The deadline starts with the first wait for the phase. Workers enforce
their idle timeout and terminal outcomes return immediately. One slot per
provider is the default. Set `adviser_concurrency.codex` or
`adviser_concurrency.claude` to allow up to four. Additional slots can
increase usage. Adjust `adviser_cooldowns` by failure kind when needed.

`agent-context adviser stats` counts busy outcomes and successful replies
received after a wait expired or after the lead checkpointed. It samples
retained assessments and their latest reviews. Timing counts use receipts
written by versions that record reply arrival.

Registered Git worktrees share project context. Separate projects and nested
repositories remain isolated. Advisers are disabled for unregistered projects
unless you explicitly enable a personal override.

New projects start with advisers off. To enable automatic advice for a project,
register it with the following option.

```sh
agent-context project add my-project /absolute/path/to/project --advisers on
```

Use `--advisers off` to turn it off again. Re-registering a project without the
option preserves its choice. For one task, request both advisers with
`team: both` or disable advice with `team: off`. Context-only operation still
loads context, applies tool guards, and supports checkpoints.

### Which sessions are covered?

The installer puts hooks and managed instruction sections in user-level Claude
and Codex configuration. Terminal and local Conductor sessions can use the same
`AGENTS.md`, `CLAUDE.md` and workflow skill. Conductor stores its own General
preferences in Repository Settings. An optional [prompt snippet](runtime/templates/conductor-prompt.txt)
is available for that field. [Conductor's agent behavior reference](https://www.conductor.build/docs/reference/agent-behavior)

Fresh installations leave Conductor's legacy TOML file alone. An upgrade
removes the old installer-owned prompt block only when it matches the
previously installed template. The installer backs up that edit and preserves
custom prompt text, unrelated settings and file permissions.

Installation alone does not prove execution. Codex hook trust must be approved;
client versions, configuration roots, command-line options, and local policy
can affect which hooks load. Minimal/safe modes that disable customizations,
worker sessions that suppress recursion, and clients that ignore user settings
do not provide the same coverage. Other computers and cloud environments need
their own installation and project registrations.

The first matching prompt creates an empty local marker beside the machine
settings. A marker records that a client handled a project prompt at least
once. Check for a fresh run receipt to verify a particular session after a
client or configuration change. `project list` shows the
registered projects; `status --cwd /path/to/repository` reports the selected
scope. `personal` means that no project context was selected. Registration
creates starter files; it does not research the repository or maintain its
product knowledge automatically. Link to existing authoritative documents and
keep those documents current. Scheduled cleanup bounds storage; optional source
refresh is a separate workflow.

### Where things live

| Location | Purpose |
| --- | --- |
| `~/context/local/personal.md` | Personal context shared across projects |
| `~/context/workspaces/<slug>/` | Registered project's context |
| `~/context/local/` | Local store and temporary workflow data |
| `~/.config/agent-context/settings.json` | Machine settings and registrations |
| `~/.config/agent-context/backups/` | Configuration backups and manifests |
| `~/.local/share/agent-context/` | Installed runtime and Python environment |
| `~/.local/bin/agent-context` | Command entry point |

Defaults cap an entry packet at 16,000 characters, retain captures for 7 days and
runs for 14 days, and limit local storage to 256 MiB. The memory database has an
8 MiB limit. Limits and retention are configurable.

### Operating settings

Inspect effective values, defaults, stored overrides, and accepted bounds:

```sh
agent-context config show --project my-project
agent-context config show refresh.max_window_days
agent-context config set refresh.sources.slack.enabled false
agent-context config set refresh.incremental_budget_usd 5.50
agent-context config set lesson_topics.personal.gauge-care '["gauge","calibration"]'
agent-context config reset refresh.sources.slack.enabled
```

Changes use the install lock, validate the machine settings, and replace the
file atomically. A stored value equal to the default shows `overridden: false`.
Project adviser settings are changed with `adviser set` and `adviser reset`.

| Operating keys | Purpose and defaults |
| --- | --- |
| `packet_chars` | Entry packet size, 16,000 characters |
| `capture_days`, `run_days` | Retention, 7 and 14 days |
| `max_runs`, `max_memory`, `max_capture` | Retained counts, 32, 48, and 80 |
| `max_local_bytes`, `max_database_bytes` | Storage budgets, 256 MiB and 8 MiB |
| `lesson_topics.<scope>.<lesson-key>` | Optional list of 1 to 32 lowercase topic words for a saved lesson. Reset to restore ordinary retrieval |
| `call_timeout`, `idle_timeout`, `provider_wait` | Overall call, output idle, and slot waits, 480, 180, and 20 seconds |
| `adviser_wait` | Lead wait budget, 50 seconds |
| `adviser_concurrency.<provider>` | Simultaneous calls, 1 per provider |
| `adviser_test_timeout`, `adviser_test_idle_timeout` | Probe deadlines, 180 and 120 seconds |
| `adviser_cooldowns.<kind>` | Seconds after failure. Account-policy and auth 86400, quota 3600, unavailable 21600, transient, timeout, and default 300 |
| `refresh.enabled`, `refresh.project` | External refresh defaults off. Choose a registered project |
| `refresh.sources.<name>.enabled`, `refresh.sources.<name>.mcp_server` | All five sources selected when refresh is enabled. GitHub uses the CLI. The MCP names default to slack, linear-server, notion, and granola |
| `refresh.github_repo`, `refresh.github_user`, `refresh.github_inventory_limit` | Repository and user for GitHub, initially empty. Open PR inventory limit 2000 |
| `refresh.model`, `refresh.stale_after_hours` | Refresh and gatherer model, sonnet. Warn after 26 hours |
| `refresh.bootstrap_days`, `refresh.max_window_days` | Initial lookback and maximum interval per pass, 14 and 7 days |
| `refresh.bootstrap_budget_usd`, `refresh.incremental_budget_usd` | Maximum agent spend per run, 40 and 10 USD |
| `refresh.bootstrap_timeout`, `refresh.incremental_timeout` | Agent timeout per run, 5400 and 2400 seconds |
| `refresh.meeting_days`, `refresh.meeting_count` | Keep recent meeting notes for 90 days, up to 160. Referenced notes take priority |

The same private settings file holds paths, registrations, provider commands,
adapter pins, and refresh permissions. Use the existing project and adviser
commands or edit those structures deliberately. Set `refresh.permissions`
to an object containing the reviewed `allow` and `deny` lists to keep refresh
policy here too. With this field absent, the existing refresh resource
directory's permission file supplies the lists. Connector authentication and
MCP registrations stay in the connected client. The operating settings display
omits credentials, command lines, permission rules, and private path lists.

### Existing context and completion checks

A registered project can select existing canonical files without moving them.
Add `context_sources` (an explicit list of absolute regular file paths) to that
project in the private settings file. `privacy_roots` lists additional directories
used only by the disclosure scanner. Neither field uploads files or grants a
worker access to them. Both reject symlinked paths. The runtime does not rewrite
external canonical files; use the project's existing reviewed update workflow.

To keep an existing project digest where it is, set that project's `context_dir`
to an existing directory inside the context root. Loading, the decision outbox,
and refresh then use it in place of `workspaces/<slug>`, and the disclosure
scanner indexes it.

Packets expose full-byte source hashes and explicitly name omitted or shortened
sources. Read required source material when an excerpt is insufficient. After
updating an authoritative source, run `agent-context refresh --run <id>` and read
the current packet before checkpointing. `agent-context finish --run <id>` checks
the checkpoint against current sources and the local checkout, including dirty
and untracked content. It does not attest remote synchronization, hosted checks,
deployment, or resource cleanup. A changed finalized run needs a new checkpoint
in a new run. With selected advisers an empty checkpoint uses
`checkpoint --run <id> --none`. The local completion file defaults to an
empty checkpoint when its `checkpoint` field is absent.

Use `context_sources` for the small standing brief, and `topic_sources` or
`topic_dirs` for explicit topic files or directories whose immediate Markdown
children are searched only when relevant. New files in a selected topic directory
are discovered automatically. At most two matching excerpts enter a packet;
unregistered projects and personal scope cannot retrieve them. Mark a retired
topic with `Context-Status: historical`, `superseded`, or `archived` to preserve it
on disk while excluding it from automatic topic retrieval. Core source contents
reserve room for later sources, including a short live brief.

Empty project notes created by registration keep their source receipts and
fingerprints without taking space in the briefing. To retrieve a detailed
saved lesson only on its topic, set `lesson_topics` in the machine settings.
For example, `{"lesson_topics":{"personal":{"gauge-care":["gauge","calibration"]}}}`
shows the personal lesson keyed `gauge-care` when the request contains either
whole word, ignoring case. A project slug limits the rule to that project's
lesson. Existing lessons follow their usual retrieval behavior until configured.
An explicit `core_memory_keys` pin keeps a lesson available on every task.

Optional `source_max_age_days` maps registered source paths to freshness limits.
These measure file age, not truth. `knowledge_revision` records the product Git
revision last reconciled with canonical knowledge; a different checkout produces
a visible warning. Update it deliberately after reconciling current facts.
`agent-context status` reports source ages, omissions, freshness warnings, memory
kinds, review-due entries, retained runs/checkpoints, installation revision, and
runtime file drift. Retained run counts are a bounded sample, not lifetime usage.

Checkpoint memory drafts accept optional Unix timestamps `review_after` and
`expires`. Review-due entries stay available for deliberate review but leave the
automatic packet. Forgetting, expiry, replacement, and eviction also retire the
runtime-owned entries in the generated inbox. Human-authored prose is preserved;
the runtime never silently rewrites canonical decisions. Reconcile a material
outcome into its existing topic and current brief, preserve old evidence in Git
or history, and checkpoint only the supported reusable memory that remains.

An adopted context directory may set `private_github_repository` to its exact
`owner/repository` destination. Before checking or publishing its staged files,
the guard verifies the canonical repository root, the sole `origin` remote and
all resolved push URLs, and confirms private visibility through the authenticated
`gh` CLI. Only this verified destination permits private context text; credentials
remain forbidden. Unknown visibility, changed remotes and push URL overrides fail
closed. This does not relax public-repository or arbitrary-upload checks.

For an explicitly configured instruction-copy workflow, private settings may
provide `instruction_sources` mapping output Markdown filenames to source files.
`privacy --instruction-copies <private-local-directory>` exports only that bounded
list after credential redaction and disclosure validation. It rejects private
context sources and existing destinations; it is not a general file reader.

Projects can set `collaboration` to `exchange` to request one reciprocal round
after successful independent assessments. The lead synthesizes the result.
Independent advice survives a failed exchange, and configured timeouts still
apply. The default remains `advisory`.

For an existing canonical update helper, private settings may register a
`local_adapters` entry with a registered `project`, absolute standalone Python
`script`, its `sha256`, and allowed `verbs`. Invoke it with
`agent-context adapter <name> <verb> <arguments...>` from that project. The runtime
checks project scope, the verb, file type and current hash before running the
helper through its isolated Python interpreter, with a two-minute timeout and
the helper's exit status. Changed scripts require deliberate review and a new
pin. Adapters are trusted local code with the lead's permissions, not a sandbox;
review their arguments and behavior before registration. They reuse an existing
canonical workflow without creating another context store or exempting arbitrary
shell commands from disclosure checks.

Hooks cover participating client sessions. They prompt the lead to save explicit
preferences, accepted decisions and verified lessons; they do not retain every
action, monitor other apps, or reconstruct old conversations. Verify retained
entries with `status` and `load`. Canonical source history and optional external
refresh remain separate from bounded local memory.

### Privacy boundaries

**Loaded context is sent to the hosted lead model you use.** Local storage does
not make hosted inference local. Adviser prompts are sanitized, and tool guards
reduce accidental disclosure, but neither guarantees confidentiality. Review
outgoing tool inputs, files, issues, pull requests, and messages.

**Selected repository evidence is off by default.** A project owner can enable
it with `agent-context project add my-project /path/to/repo --adviser-access
selected-files`. For an individual review, add `--repo-file src/example.py`
to the usual `agent-context review` command. Repeat the option for up to
eight explicitly chosen files. The lead sends their committed UTF-8 text
from one HEAD revision to the selected hosted providers. Review the named
files and ensure the project permits sharing them before submitting.
The combined evidence budget is 18,000 bytes when selecting repository files.
Private roots, hidden paths, credentials, symlinks, and untracked files are
ineligible. Advisers run tool-free in their existing isolated directories.
Use `--diff` for eligible working changes or `--evidence` for a curated excerpt.
Set `--adviser-access none` on the project to turn off file selection.

Run review and local completion inputs belong in `<context-root>/local/runs/<run-id>/task-inputs/`.
After capture, prefer a deletion-only patch to remove those individual regular
files. A bare `rm -- <files>` is also supported, subject to the client's command
policy. Directory listings stay local; recursive cleanup,
globs, mixed public/private patches and deletion of canonical context are not
exempted. Shell pipelines, redirects and substitutions do not gain a private
operation exemption. Symlinked executables in simple commands are resolved and
scanned; symlinked attachments remain blocked. The exact system null device is
recognized as an empty sink, without exempting private uploads or other devices.

Keep credentials out of memory. Keep private context, transcripts, captures,
databases, backups, and machine configuration out of this repository. Provider
data retention and account policies still apply to temporary adviser sessions.
Existing ChatGPT conversations are not deleted by this toolkit.

See [SECURITY.md](SECURITY.md) for reporting a vulnerability privately.

## More projects and computers

Register each project using its own slug and path:

```sh
agent-context project add another-project /absolute/path/to/another-project
agent-context project list
```

On another computer, clone this repository, install, authenticate the CLIs, and
register that computer's project paths. The public repository is enough to
recreate the tooling. Private knowledge moves separately through an encrypted
transfer you control.

For private-state migration, stop context jobs before copying `state.sqlite3`, or
use SQLite's backup facility to obtain a consistent copy. Omit temporary runs,
captures, transcripts, logs, and machine configuration. Review source links and
local paths at the destination. Automatic cross-device synchronization is not
included in this release.

## Optional source refresh

External refresh is off by default. Templates cover Slack, Linear, Notion,
Granola, and GitHub. To enable it, configure a registered `refresh.project`, an
existing nonsymlink `refresh.resource_root`, your intended sources and
connectors, and a separate schedule. Customize the local templates and retrieval
scope first. Daily local maintenance and external-source refresh are separate.

`agent-context status --cwd /path/to/project` shows the latest attempt, last
full success, age in hours, and source outcomes for the refresh project. Its
session briefing names failed sources and the next action. A daily schedule
has two hours of grace by default. Set `refresh.stale_after_hours` to the
expected interval plus grace for a different schedule. Existing refresh
receipts have limited source detail until the next attempt.

Omitted sources keep their defaults. For example, this part of the machine
settings disables Slack and selects a different Notion server name:

```json
{"refresh": {"sources": {"slack": {"enabled": false}, "notion": {"mcp_server": "notes"}}}}
```

Use each selected MCP server's existing client registration and grant its
intended read tools in `refresh.permissions` or the existing resource permission file.
GitHub uses the authenticated `gh` CLI. Configure `refresh.github_repo` and
`refresh.github_user` when GitHub is enabled. Disabled sources require no
credentials. A missing connector or allowance is reported as unavailable.

Each run processes up to `refresh.max_window_days` (7) of missed history
per source, starting with its oldest uncovered interval. Rerun the refresh
job to catch up sooner. Open pull requests use a separate paginated inventory.
A full search is split by time. An exhausted collection limit leaves that
source incomplete. Complete sources publish dated notes in `source-updates/`.
During partial coverage the previous combined digest and failed sources' notes
are preserved. The session reads the dated updates alongside them. When all
enabled sources catch up, the combined digest can be refreshed. Publication
commits the notes and their coverage together.

The runner dates verified source notes and protects the pinned section of
`sources.md` from model edits. During collection the model can propose changes
to the discovered section in `.cache/discovered.md`. The runner combines
validated proposals with the original pinned text. Status reports safe
validation reasons alongside connector failures, including missing or malformed
manifests, unexpected paths, and edits to protected sources.

The refresh model defaults to `sonnet`. Bootstrap and incremental runs have
separate USD budgets and timeouts in seconds. Bootstrap starts with 14 days
of history. Source selection, model, budgets, timeouts, inventory size, catch-up
window, and meeting retention are machine settings.

Installation does not authorize messages or changes in external services.

## Updates and recovery

```sh
cd /path/to/agent-setup
git pull --ff-only
# Review the changes, then update the installed runtime.
./install
```

Restart agent sessions afterward. Repeated installation preserves private state,
registrations, custom roots, unrelated settings, and lead model choices.

| Installer option | Effect |
| --- | --- |
| `--brief` | Print the next setup step and any configuration notices. Interactive terminals use this format automatically. |
| `--home PATH` | Use an isolated destination and skip launchctl activation |
| `--no-activate` | Skip activation during a normal installation |
| `--seed FILE` | Read an explicit local configuration seed |

Configuration writes are validated and rolled back on failure. Dependency
installation runs first and is outside that rollback. Backups and manifests live
under `~/.config/agent-context/backups/`; restore selected files from the relevant
backup after checking their paths. A full uninstall command is not yet included.

### Replacing an existing workflow

Install and rehearse with `--home` before changing live client settings. Keep old
jobs stopped during a coordinated cutover, and restart sessions afterward.
A migration manifest explicitly identifies owned obsolete hooks and managed
instruction sections; installation alone preserves unrelated hooks.

```json
{
  "version": 1,
  "hook_files": [{
    "path": "/absolute/private/client-hooks.json",
    "expected_sha256": "<SHA-256 of the current file bytes>",
    "commands": ["exact obsolete command"]
  }],
  "instruction_files": [{
    "path": "/absolute/private/AGENTS.md",
    "expected_sha256": "<SHA-256 of the current file bytes>",
    "sections": ["old-managed-section"]
  }],
  "toml_sections": []
}
```

`toml_sections` entries select a `.toml` file with `path`, `expected_sha256`,
`table` (for example `["prompts"]`), `key` (for example `"general"`), and
`sections`. Only selected prompt strings are edited. Inspect effective
repository-specific overrides as well as user settings.

```sh
agent-context migrate /private/path/migration.json
agent-context migrate /private/path/migration.json --apply --backup-root /private/path/backups
agent-context migrate --restore /private/path/backups/migration-ID/migration.json
```

Preview is the default. Apply removes exact selected entries and creates guarded
backups; a repeat apply is a no-op. Restore rejects subsequent edits rather than
overwriting them. Unknown hooks, resource scripts, Git guards, private data and
schedules are preserved. Inspect and migrate those integrations explicitly.
This is migration of selected owned configuration, not a full uninstall.

`agent-context privacy --cwd /absolute/path/to/project` checks actual staged Git
blobs without requiring a client tool hook. It can be chained from an existing
pre-commit hook; preserve other guards and hook-manager configuration. Review
public prose and screenshots separately because copied-text checks cannot detect
every disclosure.

## Contributing

Start with [CONTRIBUTING.md](CONTRIBUTING.md). Small fixes, reproducible bug
reports, platform testing, and clearer onboarding are welcome. Run the same
isolated checks as CI:

```sh
./scripts/check
```

This command uses a temporary destination and leaves your installed context
setup alone. It runs the unit suite and verifies that a second installation
changes no managed files.

### Measure context and advice

`runtime/evaluate_context.py` is an opt-in, synthetic decision pilot. It compares
task-only input, manually supplied facts, runtime-loaded facts, and the same
runtime candidate after independent advice. It uses fictional context in a
temporary store and reads only provider definitions from the settings file.
It never retrieves your personal or project context.

```sh
~/.local/share/agent-context/.venv/bin/python runtime/evaluate_context.py \
  --live --trials 2 --output .context/context-pilot.json
```

This makes up to 20 model calls with two trials and may consume paid usage: five
calls per client per trial, with exactly two clients. Choose a new output filename
for each run; an existing report is never overwritten. Reports stay marked as
in progress until the run finishes, and failed runs are marked incomplete. Models
and effort come from your configuration. Answer keys stay outside the prompts;
missing-information abstentions are separate from errors. The report includes
provider failures, requested and observed model identifiers, and elapsed time.
Blank observed identifiers mean the serving model was not verified. Billed cost
is not measured. The advice condition reports the initial answer plus review and
revision time, with the additional time recorded separately.

This small pilot checks supplied-context decisions, including stale facts,
untrusted quoted text, a missing contract, and self-contained code defects.
It does not establish production coding quality, long-term memory usefulness,
or a causal advantage over native tools. The manual-context condition helps
separate the benefit of available facts from the runtime's packaging. Broader
evaluation should use repeated real tasks, held-out checks, user corrections,
adviser defect catches, total usage, and time to an accepted result. Unit tests
validate the evaluator without contacting providers.

The initial pilot records both successful cases and an unavailable batch.
[Supplied-context decision pilot](experiments/context-pilot.md)

Two simulated newcomers completed the quick start. In a separate trial, ten
settings tasks passed on the first attempt with automatic context and with
manually supplied notes. The report includes the onboarding task, scoring
sheet, timings, and limits. [First use and ten project tasks](experiments/first-use-and-project-tasks.md)

Ideas and questions belong in [Discussions](https://github.com/mgchi/agent-setup/discussions).
Report reproducible bugs through [Issues](https://github.com/mgchi/agent-setup/issues/new/choose).

## Direction

The next priorities are full uninstall support, broader client and platform
verification, and better visibility into retained context and
adviser outcomes. These are areas for contribution, not commitments to a
delivery date.

## License

[MIT](LICENSE).
