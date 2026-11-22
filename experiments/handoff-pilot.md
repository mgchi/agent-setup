# Plain and structured handoffs

Six fresh, tool-free sessions proposed a first action from public repository
history on November 21, 2026. Both formats passed all three cases. The
installed workflow uses an ordinary local note. A new handoff command or
store would need evidence of reduced rework on actual interrupted tasks.

Each pair received the same three sentences about the decision, the last
verification and the next authorized action, followed by the same checkout
observation. The plain note joined the sentences into a paragraph. The other
note put each sentence on its own line with a label. Both sessions were asked
for the first action, target checkout, applicable verification and any
question that required the owner. The plain note came first in cases one
and three. The structured note came first in case two.

| Replay | Expected first action | Plain | Structured |
| --- | --- | --- | --- |
| Same checkout. Local concurrency and repeat-install checks passed at `7ea798d`. Hosted results were missing. [PR 17](https://github.com/mgchi/agent-setup/pull/17) | Inspect hosted checks for that head before merging. | Correct | Correct |
| Moved checkout. The selected-file suite passed at `92a4d2f`. Current verification was requested at `8f097ab`. [PR 18](https://github.com/mgchi/agent-setup/pull/18) | Run current local verification and attribute the earlier pass to its original commit. | Correct | Correct |
| Sibling worktree. An isolated install was authorized from `30321d9`. The selected checkout was `7ea798d`, with a clean tree at the authorized head available. [PR 19](https://github.com/mgchi/agent-setup/pull/19) | Use the existing worktree at `30321d9` for the isolated install. | Correct | Correct |

Neither format requested repeated information or treated verification from
another checkout as current. Three replies also mentioned unavailable local
tooling. The score covers the first action and the supplied evidence.

The runner requested `gpt-6.2-astra` at maximum effort in each session.
The client did not independently report the served model. One preliminary
call returned no answer and was excluded before running the six comparisons.
The task observations were reconstructed from the linked changes. The
sessions proposed actions without editing, installing or measuring a
person's interruptions. A future live comparison should record repeated
investigation, clarification requests and stale verification.
