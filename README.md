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

Install and authenticate the Claude and Codex CLIs separately. An existing
Conductor installation can use the same local setup. This project does not install
Conductor, supply model access, or transfer connector authentication.

```sh
git clone https://github.com/mgchi/agent-setup.git
cd agent-setup
./install
export PATH="$HOME/.local/bin:$PATH"
agent-context project add my-project /absolute/path/to/project
agent-context status --cwd /absolute/path/to/project
```

Add the PATH line to your shell startup file if needed. Restart your agent sessions
after installation. In Codex, approve the installed hooks through `/hooks` when
prompted. Until hooks are approved, use the installed workflow skill's manual
entry instructions.

The installer uses Python 3.14 and pinned dependencies. It adds runtime commands,
workflow skills, managed instruction sections, and hooks for session entry,
prompts, tool use, and completion. On macOS it also schedules local maintenance
at login and 04:15. Existing lead model settings are preserved.

### Try it in a project

Start a new agent session in the registered project and give it a normal task.
For a durable preference, state it explicitly, for example:

> For this project, prefer the existing components and helpers. Keep public
> interfaces backward compatible unless I request a breaking change.

The workflow asks the lead to checkpoint supported preferences and decisions.
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

Adviser defaults are `claude-fable-5-1` and `gpt-6-astra` at maximum effort. Change
them in `~/.config/agent-context/settings.json` to models your accounts support.
Both advisers can incur provider usage charges. A missing model or exhausted
provider does not prevent the lead from finishing.

Registered Git worktrees share project context. Separate projects and nested
repositories remain isolated. Advisers are disabled for unregistered projects
unless you explicitly enable a personal override.

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

### Existing context and completion checks

A registered project can select existing canonical files without moving them.
Add `context_sources` (an explicit list of absolute regular file paths) to that
project in the private settings file. `privacy_roots` lists additional directories
used only by the disclosure scanner. Neither field uploads files or grants a
worker access to them. Both reject symlinked paths. The runtime does not rewrite
external canonical files; use the project's existing reviewed update workflow.

Packets expose full-byte source hashes and explicitly name omitted or shortened
sources. Read required source material when an excerpt is insufficient. After
updating an authoritative source, run `agent-context refresh --run <id>` and read
the current packet before checkpointing. `agent-context finish --run <id>` checks
the checkpoint against current sources and the local checkout, including dirty
and untracked content. It does not attest remote synchronization, hosted checks,
deployment, or resource cleanup. A changed finalized run needs a new checkpoint
in a new run. Empty checkpoints use `checkpoint --run <id> --none`.

Projects can set `collaboration` to `exchange` to request one reciprocal round
after successful independent assessments. The lead synthesizes the result.
Independent advice survives a failed exchange, and configured timeouts still
apply. The default remains `advisory`.

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

Ideas and questions belong in [Discussions](https://github.com/mgchi/agent-setup/discussions).
Report reproducible bugs through [Issues](https://github.com/mgchi/agent-setup/issues/new/choose).

## Direction

The next priorities are full uninstall support, broader client and platform
verification, and better visibility into retained context and
adviser outcomes. These are areas for contribution, not commitments to a
delivery date.

## License

[MIT](LICENSE).
