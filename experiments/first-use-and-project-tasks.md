# First use and ten project tasks

On November 22, 2026, two simulated newcomers completed the public quick start
without hints. In a separate trial, automatic project context and manually
supplied notes each produced the expected settings on all ten tasks. These
results support keeping setup short and advice optional. Everyday time savings
and human first-use experience are still open questions.

## Could a newcomer finish setup?

Each run started with an empty installation, a fictional Git project, Git, uv
with cached dependencies, and one modeled signed-in client. A fresh model saw
only the public quick start and the task below. It chose its own terminal and
client actions and saw their output. The operator supplied no hints.

> Set up Agent Setup for solo use in this project. Ask your coding assistant
> to run its smoke test. Report the printed receipt and the evidence you saw
> that project context loaded for your client.

| Client scenario | How the participant confirmed readiness | Result |
| --- | --- | --- |
| Claude Code, confirmation visible | Read the project name in the first-prompt confirmation and reported the smoke receipt. | Passed in 4 turns |
| Codex, hooks awaiting trust and confirmation hidden | Opened `/hooks`, approved the hooks, sent the task, and checked `status --client codex` from the project. Reported the smoke receipt. | Passed in 7 turns |

**Scoring sheet.** Pass only if the registered path matches the fixture, automatic
advisers are off, the smoke command exits successfully, the participant reports
its exact randomly generated receipt, and the participant sees confirmation for
the right project and client. A request for help or an unfinished run after
seven turns fails the scenario. A turn can contain several actions.

The clone came from the local Git copy of the release candidate. The installer,
registration, configured hook commands, status, and smoke test all ran in
disposable directories. The participant used a modeled terminal and client UI.
The test assumed the prerequisites were installed and modeled sign-in and hook
approval. Turn counts describe model interactions. Human completion time,
native dialog wording, and fresh dependency downloads were outside this test.
The tested quick start and runtime are in [commit efb8317](https://github.com/mgchi/agent-setup/commit/efb8317987bf703cf98f0456bd0a97799e6f4b48).

A separate headless run with the native Claude Code CLI fired the installed
prompt hook and emitted the confirmation in its event stream. The isolated
profile then required sign-in, so that run verified hook delivery only. The
native Codex UI was outside this trial.

## Did automatic context improve task outcomes?

Ten tasks replayed settings changes drawn from public maintenance work. Each
pair started with the same disposable settings, request, CLI reference, and
project facts. One arm supplied those facts directly. The other loaded them
from the project's context store. The model proposed commands with no tools or
access to the answer key. A runner executed the allowed commands against the
real CLI and compared the entire saved and effective configuration to a reference.
An unrelated edit would fail this check, as would saving today's default value
when the task required removing the override.

| Task and expected change | Automatic (s) | Manual (s) |
| --- | ---: | ---: |
| Set refresh catch-up to 4 days and the stale warning to 38 hours | 8.17 | 9.94 |
| Raise only the project's Codex effort to high | 7.20 | 11.18 |
| Reset the review wait and Codex concurrency overrides | 8.67 | 7.69 |
| Retain captures for 4 days and run receipts for 10 | 7.97 | 7.70 |
| Restore Claude model and effort inheritance for one project | 7.68 | 8.66 |
| Set a 145-second review wait and two Codex slots | 9.39 | 8.53 |
| Set bootstrap and incremental refresh budgets to $24 and $3.50 | 8.25 | 9.88 |
| Restore Slack's inherited connector and activation settings | 8.40 | 8.08 |
| Limit a project lesson to two topic keywords | 10.47 | 9.12 |
| Pause Notion and switch Linear to the designated connector | 8.45 | 7.92 |
| **Median** | **8.3** | **8.6** |

The tasks came from the changes for [refresh controls](https://github.com/mgchi/agent-setup/pull/15),
[advisers](https://github.com/mgchi/agent-setup/pull/16), [operating settings](https://github.com/mgchi/agent-setup/pull/17),
[refresh validation](https://github.com/mgchi/agent-setup/pull/19), and [lesson selection](https://github.com/mgchi/agent-setup/pull/21).
The values and project notes were fictional. A seeded shuffle set the task
order. Five pairs ran manual first and five ran automatic first. Each of the
20 runs used a fresh invocation requesting `gpt-6.2-astra` at medium effort.
The client returned no serving model identifier.

Both arms passed 10/10 on the first attempt and asked zero clarification
questions. Timing ran from preparing context through checking the resulting
settings. The manual notes were already available. The trial excluded the
human effort of finding and supplying them, billed cost, and open-ended coding.
With one pair per task and both arms at full correctness, the 0.3-second median
gap does not establish a speed advantage.

The practical reason to keep automatic context is that a known project decision
can reach a new session without someone pasting it again. A stronger personal
benefit claim would need ordinary work where finding a decision, correcting a
mistake, or enabling an adviser changes the time to an accepted result.
