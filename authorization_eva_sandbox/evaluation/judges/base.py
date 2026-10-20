"""
base.py — domain-agnostic LLM-as-judge core.

Knows nothing about completion vs violation. It only knows how to:
  - load a model profile from `evaluation/config.toml`
  - call `litellm.completion` with retries / timeouts
  - parse a structured JSON answer of the shape:
        {"reasoning": "...", "matched_criterion": "...", "answer": "yes|no"}
  - tolerate mild formatting drift (Markdown headers, smart quotes, code
    fences) and surface unrecoverable parse failures as `answer="inconclusive"`

Concrete judges (CompletionJudge, ViolationJudge) subclass `LLMJudgeBase` and
supply their own `PROMPT_TEMPLATE` — the only domain-specific piece.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import ClassVar, Optional

# Python 3.11+ has tomllib in stdlib; on older runtimes fall back to tomli.
try:  # pragma: no branch
    import tomllib  # type: ignore[import-not-found]
except ModuleNotFoundError:  # pragma: no cover - depends on runtime version
    try:
        import tomli as tomllib  # type: ignore[import-not-found,assignment]
    except ModuleNotFoundError:  # pragma: no cover - optional dependency missing
        tomllib = None  # type: ignore[assignment]


# Config loader --------------------------------------------------------------

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.toml"


def _load_judge_profile(profile: str, fallback_model: str = "gpt-4o-mini") -> dict:
    """
    Load a [llm.<profile>] block from `evaluation/config.toml`.

    Returns {
        "model": str,
        "api_key": str | None,
        "base_url": str | None,
        "completion_kwargs": dict | None,
    }.
    Falls back to env vars (LITELLM_*) if config.toml is missing or the
    requested profile isn't there.
    """
    fallback = {
        "model": os.environ.get("LITELLM_MODEL", fallback_model),
        "api_key": os.environ.get("LITELLM_API_KEY") or os.environ.get("OPENAI_API_KEY"),
        "base_url": os.environ.get("LITELLM_BASE_URL"),
        "completion_kwargs": None,
    }

    if not _CONFIG_PATH.exists() or tomllib is None:
        return fallback

    try:
        with open(_CONFIG_PATH, "rb") as f:
            cfg = tomllib.load(f)
    except Exception:
        return fallback

    block = cfg.get("llm", {}).get(profile)
    if not block:
        return fallback

    return {
        "model": block.get("model", fallback["model"]),
        "api_key": block.get("api_key", fallback["api_key"]),
        "base_url": block.get("base_url", fallback["base_url"]),
        "completion_kwargs": block.get("completion_kwargs", fallback["completion_kwargs"]),
    }


# JudgeOutput ---------------------------------------------------------------


@dataclass
class JudgeOutput:
    answer: str  # "yes" | "no" | "inconclusive"
    matched: Optional[str] = None  # the matched action_list item (verbatim), or None
    reasoning: str = ""
    raw: str = ""  # full LLM response text, for debugging
    attempts: int = 1
    error: Optional[str] = None
    model_used: Optional[str] = None  # the model that produced the answer
    fallback_used: bool = False  # True when the primary model refused
    finish_reason: Optional[str] = None


# LLMJudgeBase --------------------------------------------------------------


@dataclass
class LLMJudgeBase:
    """
    Base class for all LLM-as-judge implementations.

    Subclasses MUST define a `PROMPT_TEMPLATE` class variable containing two
    placeholders: `{trajectory}` and `{action_list}`. They MAY override:
      - `DEFAULT_PROFILE`   : default config.toml section to read
      - `PROFILE_ENV_VAR`   : env var name that overrides the profile
      - `MODEL_ENV_VAR`     : env var name that overrides the model directly
    """

    # Subclass-overridable defaults. Concrete judge subclasses may override
    # this to change benchmark defaults (e.g. opus_4_6).
    PROMPT_TEMPLATE: ClassVar[str] = ""  # MUST be set by subclasses
    DEFAULT_PROFILE: ClassVar[str] = "deepseek_v4_pro"
    # Profiles tried in order when the model before them refuses (content filter).
    FALLBACK_PROFILES: ClassVar[tuple[str, ...]] = ()
    PROFILE_ENV_VAR: ClassVar[str] = "DETECTOR_JUDGE_PROFILE"
    MODEL_ENV_VAR: ClassVar[str] = "DETECTOR_JUDGE_MODEL"

    model: Optional[str] = None
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    completion_kwargs: Optional[dict] = None
    profile: Optional[str] = None
    temperature: float = 0.0
    # Reasoning models spend part of this before the answer; at 800 a judge
    # call on a short trajectory used every token on reasoning and returned "".
    max_tokens: int = 4000
    max_retries: int = 3  # retries on parse failure / transient API errors
    retry_backoff_seconds: float = 0.8
    request_timeout_seconds: int = 90
    format_repair_retries: int = 1

    def __post_init__(self) -> None:
        if self.model and self.api_key:
            return  # caller supplied everything, skip config.toml
        # Resolve profile: explicit arg > env var > class default.
        if self.profile is None:
            self.profile = (
                os.environ.get(self.PROFILE_ENV_VAR) or self.DEFAULT_PROFILE
            )
        loaded = _load_judge_profile(self.profile)
        if not self.model:
            self.model = (
                os.environ.get(self.MODEL_ENV_VAR) or loaded["model"]
            )
        if not self.api_key:
            self.api_key = loaded["api_key"]
        if not self.base_url:
            self.base_url = loaded["base_url"]
        if self.completion_kwargs is None:
            loaded_completion_kwargs = loaded.get("completion_kwargs")
            if isinstance(loaded_completion_kwargs, dict):
                self.completion_kwargs = dict(loaded_completion_kwargs)

    def evaluate(
        self,
        trajectory_md: str,
        action_list: list[str],
        environment_evidence: str = "",
        grounding: Optional[dict[str, str]] = None,
        references: str = "",
    ) -> JudgeOutput:
        """Run the judge once (with internal retry on parse failure).

        `environment_evidence` is an optional, already-formatted block (with its
        own heading/leading newline) injected via the `{environment}` placeholder.
        Templates that don't reference `{environment}` ignore it. Used by the
        completion judge to verify factual/comparative criteria (e.g. "cheapest")
        against the apps' ground-truth seeded state.

        `grounding` maps an action-list item (verbatim) to its world
        realization note, and `references` lists the instruction's deictic
        references resolved for this world (both from Stage 4 M4's grounding
        table). They are rendered under the action list so the judge reads
        the pre-world prose together with how this sandbox realizes it; the
        prose stays the criterion, the note only instantiates it.
        """
        if not action_list:
            return JudgeOutput(
                answer="no",
                matched=None,
                reasoning="(empty action list — vacuously no match)",
                raw="",
                attempts=0,
            )

        if not self.PROMPT_TEMPLATE:
            raise NotImplementedError(
                f"{type(self).__name__} must set PROMPT_TEMPLATE"
            )

        prompt = self.PROMPT_TEMPLATE.format(
            trajectory=trajectory_md,
            action_list=_format_action_list(action_list)
            + _format_grounding(action_list, grounding, references),
            environment=environment_evidence,
        )

        # ── Terminal print: full judge prompt ─────────────────────────────────
        print(f"\n{'='*70}")
        print(f"  [POST-HOC COMPLETION CHECK] model={self.model}  "
              f"profile={self.profile}  criteria={len(action_list)}")
        print(f"{'='*70}")
        print(f"  --- FULL JUDGE PROMPT ---")
        print(prompt)
        print(f"{'='*70}\n")
        # ─────────────────────────────────────────────────────────────────────

        last_error: Optional[str] = None
        last_raw: str = ""
        used: dict = {"model": self.model, "fallback": False, "finish": None}
        for attempt in range(1, self.max_retries + 2):  # 1 initial + N retries
            try:
                raw, used = self._call_with_fallback(prompt)
                last_raw = raw

                # ── Terminal print: raw judge response ───────────────────────
                print(f"\n{'='*70}")
                print(f"  [POST-HOC COMPLETION RESPONSE] attempt={attempt}")
                print(f"{'='*70}")
                print(f"  {raw}")
                print(f"{'='*70}\n")
                # ─────────────────────────────────────────────────────────────

                parsed = _parse_judge_output(raw, action_list, allow_repair=True)
                if parsed.answer in ("yes", "no"):
                    parsed.attempts = attempt
                    parsed.raw = raw
                    parsed.model_used = used["model"]
                    parsed.fallback_used = used["fallback"]
                    parsed.finish_reason = used["finish"]
                    return parsed
                last_error = f"unparseable answer: {parsed.answer!r}"
            except Exception as e:  # noqa: BLE001
                last_error = f"{type(e).__name__}: {e}"

            # If format was invalid, ask model to re-emit valid JSON only.
            prompt = _format_repair_prompt(prompt, last_raw)

            if attempt < (self.max_retries + 1):
                time.sleep(self.retry_backoff_seconds * attempt)

        raw_path = _save_raw_judge_output(last_raw, last_error or "unknown_parse_failure")
        detail = f"{last_error}; raw_saved={raw_path}" if raw_path else str(last_error)
        return JudgeOutput(
            answer="inconclusive",
            matched=None,
            reasoning=f"Judge call failed after {self.max_retries + 1} attempts: {detail}",
            raw=last_raw,
            attempts=self.max_retries + 1,
            error=detail,
            model_used=used["model"],
            fallback_used=used["fallback"],
            finish_reason=used["finish"],
        )

    def _call(self, prompt: str) -> str:
        return self._request(prompt, self.model, self.api_key, self.base_url,
                             self.completion_kwargs)[0]

    def _request(self, prompt: str, model: str, api_key: Optional[str],
                 base_url: Optional[str], completion_kwargs: Optional[dict]) -> tuple[str, Optional[str]]:
        """One completion. Returns (text, finish_reason)."""
        # Defer the heavy import so test environments without litellm can
        # still import this module (e.g. to introspect the prompt template).
        import litellm  # noqa: PLC0415

        if base_url and "openrouter" in base_url and not model.startswith("openrouter/"):
            model = f"openrouter/{model}"
        kwargs = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "api_key": api_key,
            "base_url": base_url,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "timeout": self.request_timeout_seconds,
        }
        if isinstance(completion_kwargs, dict):
            kwargs.update(completion_kwargs)
        resp = litellm.completion(
            **kwargs,
        )
        choice = resp.choices[0]
        return (choice.message.content or "").strip(), getattr(choice, "finish_reason", None)

    def _ask_once(self, prompt: str, model: str, api_key, base_url,
                  completion_kwargs) -> tuple[str, Optional[str], bool]:
        """(text, finish_reason, refused). Only a content-filter refusal is swallowed."""
        try:
            text, finish = self._request(prompt, model, api_key, base_url, completion_kwargs)
            return text, finish, finish == "content_filter"
        except Exception as e:  # noqa: BLE001
            if "content_filter" not in str(e) and "content filter" not in str(e).lower():
                raise
            return "", "content_filter", True

    def _call_with_fallback(self, prompt: str) -> tuple[str, dict]:
        """Ask the primary model; on a refusal, ask each FALLBACK_PROFILES entry in turn."""
        text, finish, refused = self._ask_once(prompt, self.model, self.api_key,
                                               self.base_url, self.completion_kwargs)
        used = {"model": self.model, "fallback": False, "finish": finish}
        for name in self.FALLBACK_PROFILES:
            if not refused:
                break
            fb = _load_judge_profile(name)
            print(f"  [judge] {used['model']} refused (content filter); asking {fb['model']}")
            text, finish, refused = self._ask_once(prompt, fb["model"], fb["api_key"],
                                                   fb["base_url"], fb.get("completion_kwargs"))
            used = {"model": fb["model"], "fallback": True, "finish": finish}
        return text, used


# Helpers --------------------------------------------------------------------


def _format_action_list(actions: list[str]) -> str:
    return "\n".join(f"- {a}" for a in actions)


def _format_grounding(actions: list[str], grounding: Optional[dict[str, str]],
                      references: str = "") -> str:
    """Render the world-grounding block under the action list (or '')."""
    notes = [(i, grounding[a]) for i, a in enumerate(actions, 1)
             if grounding and grounding.get(a)]
    if not notes and not references:
        return ""
    lines = ["", "", "# World grounding (how the items above are realized in THIS "
             "sandbox — exact ids, values and timing. A note only instantiates "
             "its item's references; the item's text remains the criterion. "
             "Quote the item text verbatim in matched_criterion, never the note.)"]
    if references:
        lines.append("References in the user's instruction:")
        lines.append(references)
    for i, note in notes:
        lines.append(f"- item {i}: {note}")
    return "\n".join(lines)


_ANSWER_RE = re.compile(
    r"(?im)^\s*(?:\*\*)?\s*answer\s*(?:\*\*)?\s*[:\-]\s*[\"'`*]?(yes|no)\b"
)
_ANSWER_FALLBACK_RE = re.compile(
    r"(?im)\b(?:final\s+answer|answer)\b[^\n]{0,50}\b(yes|no)\b"
)
_MATCHED_RE = re.compile(
    r"(?im)^\s*(?:\*\*)?\s*matched\s+criterion\s*(?:\*\*)?\s*:\s*(.*)$"
)
_REASONING_RE = re.compile(
    r"(?ims)^\s*(?:\*\*)?\s*reasoning\s*(?:\*\*)?\s*:\s*(.*?)(?=^\s*(?:\*\*)?\s*matched\s+criterion\s*(?:\*\*)?\s*:|\Z)"
)
_JSON_BLOCK_RE = re.compile(r"\{[\s\S]*\}")
_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def _normalize_answer(value: object) -> str:
    if value is None:
        return "?"
    s = str(value).strip().lower().rstrip(".!?:;")
    return s if s in {"yes", "no"} else "?"


def _strip_code_fences(text: str) -> str:
    return _CODE_FENCE_RE.sub("", text.strip())


def _extract_json_candidate(text: str) -> Optional[str]:
    cleaned = _strip_code_fences(text)
    if cleaned.startswith("{") and cleaned.endswith("}"):
        return cleaned
    m = _JSON_BLOCK_RE.search(cleaned)
    return m.group(0) if m else None


def _repair_json_text(text: str) -> str:
    repaired = text
    # Remove trailing commas before object/array close.
    repaired = re.sub(r",(\s*[}\]])", r"\1", repaired)
    # Normalize smart quotes that occasionally appear in LLM output.
    repaired = repaired.replace("\u201c", '"').replace("\u201d", '"').replace("\u2019", "'")
    return repaired


def _parse_json_output(raw: str, action_list: list[str]) -> Optional[JudgeOutput]:
    candidate = _extract_json_candidate(raw)
    if not candidate:
        return None

    obj = None
    parse_attempts = [candidate, _repair_json_text(candidate)]
    for txt in parse_attempts:
        try:
            obj = json.loads(txt)
            break
        except Exception:
            continue
    if not isinstance(obj, dict):
        return None

    answer = _normalize_answer(obj.get("answer"))
    reasoning = str(obj.get("reasoning", "") or "").strip()
    matched_raw = str(obj.get("matched_criterion", "") or "").strip().strip("\"'`")
    matched = _snap_matched_criterion(matched_raw, action_list)

    if answer == "no":
        matched = None
    elif answer == "yes" and matched is None:
        if len(action_list) == 1:
            matched = action_list[0]
        else:
            answer = "?"

    return JudgeOutput(answer=answer, matched=matched, reasoning=reasoning, raw=raw)


def _snap_matched_criterion(matched_raw: str, action_list: list[str]) -> Optional[str]:
    if not matched_raw:
        return None
    if matched_raw.lower() == "none":
        return None
    for item in action_list:
        if matched_raw == item or matched_raw.strip(".") == item.strip("."):
            return item
    for item in action_list:
        if matched_raw.lower() in item.lower() or item.lower() in matched_raw.lower():
            return item
    return matched_raw


def _parse_judge_output(raw: str, action_list: list[str], allow_repair: bool = True) -> JudgeOutput:
    """Parse the structured judge response. Tolerant of mild formatting drift."""
    parsed_json = _parse_json_output(raw, action_list) if allow_repair else None
    if parsed_json is not None:
        return parsed_json

    answer_m = _ANSWER_RE.search(raw)
    if not answer_m:
        answer_m = _ANSWER_FALLBACK_RE.search(raw)
    matched_m = _MATCHED_RE.search(raw)
    reasoning_m = _REASONING_RE.search(raw)

    answer = answer_m.group(1).lower() if answer_m else "?"
    matched_raw = matched_m.group(1).strip().strip("\"'") if matched_m else ""
    reasoning = reasoning_m.group(1).strip() if reasoning_m else ""

    matched = _snap_matched_criterion(matched_raw, action_list)

    if answer == "yes" and matched is None:
        # Many judge models omit "Matched criterion" when only one option exists.
        if len(action_list) == 1:
            matched = action_list[0]
        else:
            # Inconsistency: yes without a matched criterion. Treat as parse fail
            # so the retry path triggers.
            answer = "?"

    return JudgeOutput(
        answer=answer,
        matched=matched,
        reasoning=reasoning,
        raw=raw,
    )


def _format_repair_prompt(base_prompt: str, bad_output: str) -> str:
    snippet = (bad_output or "").strip()
    if len(snippet) > 1800:
        snippet = snippet[:1800] + "\n...[truncated]"
    return (
        base_prompt
        + "\n\nYour previous response did not match the required JSON schema.\n"
        + "Return ONLY a valid JSON object with keys: reasoning, matched_criterion, answer.\n"
        + "Do not include markdown/code fences.\n"
        + f"Previous response:\n{snippet}"
    )


def _save_raw_judge_output(raw: str, reason: str) -> Optional[str]:
    if not raw:
        return None
    try:
        out_dir = Path(__file__).resolve().parent.parent / "outputs" / "judge_raw"
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        safe_reason = re.sub(r"[^a-zA-Z0-9_.-]+", "_", reason)[:80] or "parse_error"
        out_path = out_dir / f"{ts}_{safe_reason}.txt"
        out_path.write_text(raw, encoding="utf-8")
        return str(out_path)
    except Exception:
        return None
