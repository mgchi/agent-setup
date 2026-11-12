# Supplied-context decision pilot

Run date: 2026-11-11. This is a small synthetic pilot, not a production coding
benchmark. Run it with `runtime/evaluate_context.py`; live calls are opt-in.

## Method

Six decision cases cover three missing project contracts, a pagination boundary,
replayed billing events, and an unapproved retention policy. Context includes a
superseded value and an untrusted quoted instruction. An unrelated project's
retention fact is stored separately and must not enter the retrieved packet.
Two seeds vary contract values and answer ordering. Answer keys stay outside
the model requests. The clients have no tools or access to real project data.

Each client runs task-only, manually supplied context, and runtime-loaded
context in a seeded shuffled order. The manual condition contains the same
facts as the runtime condition. A peer then reviews the runtime candidate, and
the original client considers that advice in a fresh invocation. Advice time
includes the original answer, peer review, and final reconsideration.

The configured routes requested `claude-fable-5-2` and `gpt-6-astra`, both at
maximum effort. Claude reported the requested serving model. Codex completed
calls but did not expose its serving model through the runtime's telemetry, so
that identity remains unverified. No defaults or account policies were changed.

## Observations

- Task-only: three of four batches completed. They produced nine correct
  responses and nine justified abstentions where project facts were absent.
  One Codex batch hit the 90-second idle timeout; its six cases are unavailable.
- Manual context: all four batches completed, with 24/24 correct responses.
- Runtime context: all four batches completed, with 24/24 correct responses.
- Runtime context plus advice: all four batches completed, with 24/24 correct
  responses. Peers reported no findings and no choices changed. Review and
  reconsideration added 35.4 to 55.6 seconds per batch, median 46.2 seconds.

There were 20 model invocations. The command exited with failure status because
of the timeout, while retaining the partial results. Billed cost was not
measured. Case counts are repeated outcomes from six fixtures, not 24
independent real-world engineering tasks.

## Implications and limits

The runtime delivered the needed facts without observed scope contamination,
and matched the manual-context control. The task-only clients appropriately
declined to invent missing decisions when explicitly instructed to abstain and
given that answer choice. This is a delivery check on clear, matching facts; it
does not establish how an agent behaves on open-ended tasks or a reasoning
advantage from the runtime itself.

The context conditions reached the fixture ceiling, leaving no observed errors
for reviewers to fix. The extra advice therefore establishes overhead on these
easy cases, not that independent review is ineffective on difficult work. The
pilot does not test repository editing, long-term retention, realistic noisy
retrieval, or deployed-client coverage. The single timeout and small sample
also prevent a reliable speed comparison. Both clients share each seed's order,
and concurrent execution can affect timings. There is no second-pass control
without advice, so a future revision improvement alone would not isolate the
effect of the peer review.

Start new registrations in context-only mode when that fits the work, and request
advice for tasks whose consequences or uncertainty justify it. Before changing
an established review policy, compare actual tasks with held-out checks and
record validated defects caught, user corrections, total usage, and time to an
accepted result. Keep the same-information manual control and report failures.
