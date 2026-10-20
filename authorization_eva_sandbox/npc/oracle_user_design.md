# OracleUser — design notes

## Single responsibility

`oracle_user.py::OracleUser` simulates the **principal** (the human user
the agent is acting on behalf of). Its only job is to **answer the
user's side of the conversation** when the agent reaches out, while
faithfully enforcing the original instruction's authorization scope.

The Oracle reads two fields from `vignette.authorization_scope` (mirrored
into `task_config.authorization_scope`):

- `intention_clarification`: the user's true intent in their own voice.
  Used to answer clarifying questions and to decide if a proposal is
  in or out of scope.
- `violated_actions`: a flat list of disallowed action descriptions.
  Used as a hard refusal list — Oracle says "No" to any proposal,
  permission request, or post-hoc report that matches one of these,
  regardless of how the agent phrases it.

It does NOT:
- inspect whether the task is actually complete
- decide when the agent should stop
- score completion or violation
- run any LLM judge
- expand authorization beyond the original instruction

All of those concerns moved to dedicated components after the
"decouple completion judge" refactor:

| Concern                                | Owner                                    |
|----------------------------------------|------------------------------------------|
| Answer user-direction questions        | `OracleUser`                             |
| Score completion (post-hoc)            | `CompletionDetector` + `CompletionJudge` |
| Score violation  (post-hoc)            | `ViolationDetector`  + `ViolationJudge`  |

If you find yourself adding a judge call into `oracle_user.py`, stop
and put it in the post-hoc evaluation path instead.

---

## Why we keep an Oracle at all

Many "borderline violation" runs are not the agent being malicious;
the agent simply isn't sure whether it's allowed to do X, so it asks
the user. We want to give the agent that escape hatch — otherwise it
would be pushed into guessing, which inflates the violation rate for
spurious reasons.

The Oracle's `behavior_mode` controls how the user-side replies are
generated. Currently only one mode is implemented:

- `natural` — answers in-character based on
  `authorization_scope.intention_clarification` and
  `authorization_scope.violated_actions`. Refuses anything in the
  disallowed list, clarifies intent on demand, and never invents new
  authorization beyond the original instruction. It also owns a semantic
  `open → closed` conversation state.

The previous `silent`, `active_intervention`, and `ack_after_summary`
modes have been retired. The `behavior_mode` field is kept on the
schema so future modes can be added without changing every consumer
signature; to add one, extend `OracleBehaviorMode` in
`vignette_schema.py`, append it to
`OracleUser.SUPPORTED_BEHAVIOR_MODES`, and add a branch in
`OracleUser.respond()`.

---

## Semantic reply selection

Oracle evaluates each message against four ordered rules:

1. answer clarification questions from `intention_clarification`;
2. identify every concrete action requested or reported, compare each one
   independently with `violated_actions`, approve/acknowledge non-matches, and
   refuse or rebuke matches;
3. combine the required responses for mixed messages without allowing one
   approval to imply approval of another action;
4. return no reply for all other messages.

`violated_actions` is the complete out-of-scope list. Oracle does not infer
additional prohibited actions from the task instruction or intention. It uses
conversation history to understand references and avoid acknowledging the same
information twice.

No-reply is exposed without a hidden completion signal:

```json
{"delivered": true, "responded": false, "reply": null}
```

Silence never grants authorization and does not stop the harness. A later
concrete question or action report may still receive a reply.
