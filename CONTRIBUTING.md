# Contributing

Thanks for helping improve Agent Setup. Focused pull requests and reproducible
bug reports make this project easier to maintain.

## Before you start

For a bug, search existing issues and include a small, sanitized reproduction.
For a substantial behavior change, open an issue or discussion explaining the
problem and proposed result before building it. Documentation corrections and
small fixes can go straight to a pull request.

Never include private context, tokens, transcripts, database files, backup files,
or account configuration. Use fictional projects and temporary directories in
examples. Report security problems through [SECURITY.md](SECURITY.md).

## Development

Clone your fork and install [uv](https://docs.astral.sh/uv/getting-started/installation/).
The project uses Python 3.14 and the versions in `requirements.txt`.

```sh
./scripts/check
```

The check script installs into a temporary directory, runs the unit suite, and
checks repeat-install behavior. It never activates scheduled jobs. Its temporary
files are removed when it exits. Existing model accounts are not needed for the
test suite, and tests must not make paid model calls or access live user data.

For an installation you want to inspect afterward:

```sh
./install --home /absolute/path/to/test-home
/absolute/path/to/test-home/.local/share/agent-context/.venv/bin/python \
  -m unittest discover -s runtime -v
```

## Project layout

| Area | Responsibility |
| --- | --- |
| `runtime/agent_context.py` | CLI and hook entry points |
| `runtime/context_store.py` | Settings, project resolution, and local memory |
| `runtime/team_runtime.py` | Adviser processes, waits, and failure handling |
| `runtime/install_local.py` | Managed installation and configuration rollback |
| `runtime/disclosure.py`, `runtime/tool_privacy.py` | Disclosure checks |
| `runtime/refresh_context.py` | Optional source refresh and local maintenance |
| `runtime/templates/` | Generic installed instructions and skills |
| `runtime/test_*.py` | Unit and regression coverage |

## Changes we can review

- Reuse existing helpers and preserve current callers and settings.
- Keep installation repeatable and test changes under an isolated home.
- Add regression coverage for behavior changes, especially project isolation,
  retention, process cleanup, failure handling, and disclosure checks.
- Keep provider failures recoverable and adviser capabilities restricted.
- Keep instructions concise and examples independent of any particular company.
- Describe observable behavior and validation in the pull request.

Run `./scripts/check` before submitting. CI repeats the checks on macOS and Linux.
Passing unit tests does not establish compatibility with every hosted model or
agent client; call out what you exercised and what remains unverified.

By submitting a contribution, you agree that it is licensed under the project's
[MIT license](LICENSE). Follow the [Code of Conduct](CODE_OF_CONDUCT.md).
