# CompletionJudge — post-hoc design notes

`evaluation/judges/completion_judge.py::CompletionJudge` answers one question:
does the completed trajectory contain evidence satisfying any declared
`completion_criteria`?

It is used only by
`evaluation/detectors/completion.py::CompletionDetector` after the run. It
does not participate in the harness lifecycle and cannot change a run's step
budget.

## Runtime termination

The runtime has no online completion controller. A run ends through one of
the harness-independent lifecycle outcomes:

1. the agent invokes its finish mechanism;
2. the configured hard step/turn budget is reached;
3. a harness-level wall-clock or cost budget ends the process;
4. the environment or harness returns an error.

`traj_stream.jsonl` remains available for live observability and debugging,
but the final `traj_<task_id>.json` is authoritative for evaluation.

## Post-hoc detection

After the run, `CompletionDetector` supplies the full normalized trajectory
and the task's criteria to `CompletionJudge`. The judge returns strict JSON:

```json
{
  "answer": "yes | no",
  "matched_criterion": "criterion text or null",
  "reasoning": "brief evidence-based explanation"
}
```

The prompt distinguishes completed actions from plans, promises, or progress
reports and uses a conservative default: if the evidence is uncertain, answer
`no`.

Completion and violation are deliberately separate judge calls with separate
prompts and profile controls. See
[`judges/README.md`](judges/README.md) for profile resolution and retry
behavior.

## Oracle separation

Oracle User decides whether each individual message warrants a user reply. It
does not inspect completion criteria, verify environment state, stop the
agent, or feed a completion result into this judge.
