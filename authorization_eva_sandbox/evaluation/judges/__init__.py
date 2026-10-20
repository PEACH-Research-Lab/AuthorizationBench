"""
judges — LLM-as-judge package.

Splits judging into three layers so completion and violation are fully
decoupled at the code, prompt, and model-config levels:

  - `base.LLMJudgeBase`   : LLM call + JSON parse + retry.  Domain-agnostic.
                            Subclasses supply their own prompt template.
  - `completion_judge.CompletionJudge`
                          : prompt for "did the agent complete the task?"
                            Reads model from `[llm.<COMPLETION_JUDGE_PROFILE>]`.
                            Used by the post-hoc CompletionDetector.
  - `violation_judge.ViolationJudge`
                          : prompt for "did the agent perform any violated
                            action?" — keeps the full evidence-priority rules.
                            Reads model from `[llm.<VIOLATION_JUDGE_PROFILE>]`.

Import surface:

    from authorization_eva_sandbox.evaluation.judges import (
        CompletionJudge, ViolationJudge, JudgeOutput,
    )
"""
from .base import LLMJudgeBase, JudgeOutput
from .completion_judge import CompletionJudge, COMPLETION_PROMPT_TEMPLATE
from .violation_judge import ViolationJudge, VIOLATION_PROMPT_TEMPLATE

__all__ = [
    "LLMJudgeBase",
    "JudgeOutput",
    "CompletionJudge",
    "ViolationJudge",
    "COMPLETION_PROMPT_TEMPLATE",
    "VIOLATION_PROMPT_TEMPLATE",
]
