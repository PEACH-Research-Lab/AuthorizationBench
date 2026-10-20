"""LLM calling infrastructure for task generation pipeline."""
from __future__ import annotations

import json
import os
import re
import tomllib
from abc import abstractmethod
from pathlib import Path
from typing import Any, ClassVar

try:
    import litellm
except ImportError:
    litellm = None

from authorization_eva_sandbox.task_generation.shared.check import (
    Check,
    CheckContext,
    CostTier,
    Issue,
    Severity,
)


def _load_llm_profile(config_path: Path, profile: str) -> dict[str, Any]:
    data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    profiles = data.get("llm", {})
    if profile not in profiles:
        available = ", ".join(sorted(profiles))
        raise ValueError(f"Unknown LLM profile {profile!r}. Available profiles: {available}")
    return dict(profiles[profile])


def profile_base_url(profile: dict[str, Any]) -> str | None:
    """A profile that brings its own api_key is authoritative about routing:
    its base_url (or None = the provider's default endpoint) wins. The
    LITELLM_BASE_URL env var only applies to fully env-driven profiles —
    otherwise an oracle/harness env pointing at OpenRouter would silently
    reroute direct-OpenAI profiles (gpt-4.1, gpt-5.6-sol) and 401 them."""
    if profile.get("base_url"):
        return str(profile["base_url"])
    if profile.get("api_key"):
        return None
    return os.environ.get("LITELLM_BASE_URL")


def _extract_json_object(raw: str) -> dict[str, Any]:
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            # Models occasionally emit several concatenated JSON objects
            # (prose + payload, or one object per file). Decode each object
            # in sequence and keep the largest — the payload dwarfs any
            # preamble object.
            decoder = json.JSONDecoder()
            best: dict[str, Any] | None = None
            best_len = -1
            pos = start
            while True:
                pos = text.find("{", pos)
                if pos == -1:
                    break
                try:
                    obj, consumed = decoder.raw_decode(text[pos:])
                except json.JSONDecodeError:
                    pos += 1
                    continue
                if isinstance(obj, dict) and consumed > best_len:
                    best, best_len = obj, consumed
                pos += consumed
            if best is None:
                raise
            return best


def resolve_api(api: str, model: str) -> str:
    if api != "auto":
        return api
    return "openai-responses" if model.startswith("gpt-5") else "litellm"


# Generation-side resilience: primary providers occasionally hang or return
# empty content (DeepSeek v4 pro timed out for 20+ min on 2026-09-14). Every
# stage funnels through generate_vignette, so the fallback lives here.
DEFAULT_FALLBACK_PROFILE = "gpt5_6_sol"
DEFAULT_LLM_TIMEOUT_S = 240


def resolve_profile(config_file: Path, profile_name: str) -> dict[str, Any]:
    """Config profile -> the exact kwargs generate_vignette needs."""
    profile = _load_llm_profile(config_file, profile_name)
    model = str(profile.get("model", ""))
    ck = profile.get("completion_kwargs")
    return {
        "model": model,
        "api": resolve_api("auto", model),
        "api_key": (profile.get("api_key") or os.environ.get("LITELLM_API_KEY")
                    or os.environ.get("OPENAI_API_KEY")),
        "base_url": profile_base_url(profile),
        "reasoning_effort": profile.get("reasoning_effort"),
        "completion_kwargs": dict(ck) if isinstance(ck, dict) else None,
    }


def fallback_for(config_file: Path, primary_profile: str,
                 fallback_profile: str = DEFAULT_FALLBACK_PROFILE) -> dict[str, Any] | None:
    """Resolved fallback kwargs, or None when primary IS the fallback."""
    if primary_profile == fallback_profile:
        return None
    try:
        return resolve_profile(config_file, fallback_profile)
    except ValueError:
        return None


def generate_vignette(
    *,
    prompt: str,
    model: str,
    api: str,
    api_key: str | None,
    base_url: str | None,
    reasoning_effort: str | None,
    completion_kwargs: dict[str, Any] | None,
    temperature: float,
    max_tokens: int,
    timeout: float = DEFAULT_LLM_TIMEOUT_S,
    fallback_profile: dict[str, Any] | None = None,
    system: str = (
        "You generate strict JSON vignette drafts for an "
        "authorization-scope benchmark."
    ),
) -> dict[str, Any]:
    """One generation call. If the primary provider times out, cannot be
    reached, or returns empty/unparseable content and a resolved
    ``fallback_profile`` is given, the SAME prompt is retried once on the
    fallback (see resolve_profile / fallback_for)."""
    def _once(p: dict[str, Any]) -> dict[str, Any]:
        if p["api"] == "openai-responses":
            return generate_vignette_openai_responses(
                prompt=prompt, model=p["model"], api_key=p["api_key"],
                base_url=p["base_url"], reasoning_effort=p["reasoning_effort"],
                max_tokens=max_tokens, timeout=timeout, system=system,
            )
        return generate_vignette_litellm(
            prompt=prompt, model=p["model"], api_key=p["api_key"],
            base_url=p["base_url"], reasoning_effort=p["reasoning_effort"],
            completion_kwargs=p["completion_kwargs"], temperature=temperature,
            max_tokens=max_tokens, timeout=timeout, system=system,
        )

    primary = {"model": model, "api": api, "api_key": api_key,
               "base_url": base_url, "reasoning_effort": reasoning_effort,
               "completion_kwargs": completion_kwargs}
    try:
        result = _once(primary)
        if not result:
            raise ValueError(f"model {model!r} returned an empty JSON object")
        return result
    except Exception as exc:  # noqa: BLE001 — provider failures of any shape
        if not fallback_profile:
            raise
        print(f"[llm] primary {model!r} failed ({type(exc).__name__}: "
              f"{str(exc)[:120]}); falling back to {fallback_profile['model']!r}",
              flush=True)
        result = _once(fallback_profile)
        if not result:
            raise ValueError(
                f"fallback model {fallback_profile['model']!r} returned "
                "an empty JSON object"
            )
        return result


def generate_vignette_litellm(
    *,
    prompt: str,
    model: str,
    api_key: str | None,
    base_url: str | None,
    reasoning_effort: str | None,
    completion_kwargs: dict[str, Any] | None,
    temperature: float,
    max_tokens: int,
    timeout: float = DEFAULT_LLM_TIMEOUT_S,
    system: str = (
        "You generate strict JSON vignette drafts for an "
        "authorization-scope benchmark."
    ),
) -> dict[str, Any]:
    if litellm is None:
        raise RuntimeError("litellm is required to generate a vignette. Install requirements or use --print-prompt.")
    is_anthropic = model.startswith("claude-") and not (isinstance(base_url, str) and base_url)
    kwargs = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": system,
            },
            {"role": "user", "content": prompt},
        ],
        "api_key": api_key,
        "base_url": base_url,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "timeout": timeout,
    }
    if not is_anthropic:
        kwargs["response_format"] = {"type": "json_object"}
    deepseek_direct = isinstance(base_url, str) and "api.deepseek.com" in base_url
    if deepseek_direct:
        kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
    if reasoning_effort:
        kwargs["reasoning_effort"] = reasoning_effort
        if deepseek_direct:
            kwargs["allowed_openai_params"] = ["reasoning_effort"]
    if isinstance(completion_kwargs, dict):
        kwargs.update(completion_kwargs)
    response = litellm.completion(**kwargs)
    raw = response.choices[0].message.content
    if not raw:
        finish_reason = getattr(response.choices[0], "finish_reason", None)
        raise ValueError(
            f"model returned empty content (finish_reason={finish_reason!r}); "
            "likely the token budget was consumed by reasoning or the response "
            "was truncated — consider retrying with a higher max_tokens."
        )
    return _extract_json_object(raw)


def generate_vignette_openai_responses(
    *,
    prompt: str,
    model: str,
    api_key: str | None,
    base_url: str | None,
    reasoning_effort: str | None,
    max_tokens: int,
    timeout: float = DEFAULT_LLM_TIMEOUT_S,
    system: str = (
        "You generate strict JSON vignette drafts for an "
        "authorization-scope benchmark."
    ),
) -> dict[str, Any]:
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("openai is required for --api openai-responses.") from exc
    client_kwargs: dict[str, Any] = {"timeout": timeout}
    if api_key:
        client_kwargs["api_key"] = api_key
    if base_url:
        client_kwargs["base_url"] = base_url
    client = OpenAI(**client_kwargs)
    request: dict[str, Any] = {
        "model": model,
        "instructions": system,
        "input": prompt,
        "max_output_tokens": max_tokens,
    }
    if reasoning_effort:
        request["reasoning"] = {"effort": reasoning_effort}
    response = client.responses.create(**request)
    if getattr(response, "status", None) == "incomplete":
        reason = getattr(getattr(response, "incomplete_details", None), "reason", "unknown")
        raise RuntimeError(f"OpenAI Responses API returned incomplete output: {reason}")
    raw = response.output_text
    return _extract_json_object(raw)


TEXT_PROTOCOL_SYSTEM = (
    "Return the requested protocol text only. Do not wrap it in JSON."
)


def complete_text_litellm(
    *,
    prompt: str,
    model: str,
    api_key: str | None,
    base_url: str | None,
    reasoning_effort: str | None,
    completion_kwargs: dict[str, Any] | None,
    temperature: float,
    max_tokens: int,
    timeout: float = DEFAULT_LLM_TIMEOUT_S,
    system: str = TEXT_PROTOCOL_SYSTEM,
) -> str:
    if litellm is None:
        raise RuntimeError(
            "litellm is required for text completions. "
            "Install requirements or use --print-prompt."
        )
    is_anthropic = model.startswith("claude-") and not (
        isinstance(base_url, str) and base_url
    )
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "api_key": api_key,
        "base_url": base_url,
        "max_tokens": max_tokens,
        "timeout": timeout,
    }
    if not model.startswith("gpt-5"):
        kwargs["temperature"] = temperature
    if not is_anthropic:
        kwargs["response_format"] = {"type": "text"}
    deepseek_direct = isinstance(base_url, str) and "api.deepseek.com" in base_url
    if deepseek_direct:
        kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
    if reasoning_effort:
        kwargs["reasoning_effort"] = reasoning_effort
        if deepseek_direct:
            kwargs["allowed_openai_params"] = ["reasoning_effort"]
    if isinstance(completion_kwargs, dict):
        kwargs.update(completion_kwargs)
    response = litellm.completion(**kwargs)
    raw = response.choices[0].message.content
    if not raw or not str(raw).strip():
        finish_reason = getattr(response.choices[0], "finish_reason", None)
        raise ValueError(
            f"model returned empty text (finish_reason={finish_reason!r})"
        )
    return str(raw).strip()


def complete_text_openai_responses(
    *,
    prompt: str,
    model: str,
    api_key: str | None,
    base_url: str | None,
    reasoning_effort: str | None,
    max_tokens: int,
    timeout: float = DEFAULT_LLM_TIMEOUT_S,
    system: str = TEXT_PROTOCOL_SYSTEM,
) -> str:
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("openai is required for --api openai-responses.") from exc
    client_kwargs: dict[str, Any] = {"timeout": timeout}
    if api_key:
        client_kwargs["api_key"] = api_key
    if base_url:
        client_kwargs["base_url"] = base_url
    client = OpenAI(**client_kwargs)
    request: dict[str, Any] = {
        "model": model,
        "instructions": system,
        "input": prompt,
        "max_output_tokens": max_tokens,
    }
    if reasoning_effort:
        request["reasoning"] = {"effort": reasoning_effort}
    response = client.responses.create(**request)
    if getattr(response, "status", None) == "incomplete":
        reason = getattr(
            getattr(response, "incomplete_details", None), "reason", "unknown"
        )
        raise RuntimeError(
            f"OpenAI Responses API returned incomplete output: {reason}"
        )
    raw = response.output_text
    if not raw or not str(raw).strip():
        raise ValueError("model returned empty text")
    return str(raw).strip()


def complete_text(
    *,
    prompt: str,
    profile_name: str,
    config_file: Path,
    max_tokens: int,
    retries: int = 1,
    system: str = TEXT_PROTOCOL_SYSTEM,
    temperature: float = 0,
    timeout: float = DEFAULT_LLM_TIMEOUT_S,
) -> str:
    """One text completion. Does not JSON-parse the reply."""
    resolved = resolve_profile(config_file, profile_name)
    last_error: Exception | None = None
    attempt_tokens = max_tokens
    for attempt in range(retries + 1):
        try:
            if resolved["api"] == "openai-responses":
                text = complete_text_openai_responses(
                    prompt=prompt,
                    model=resolved["model"],
                    api_key=resolved["api_key"],
                    base_url=resolved["base_url"],
                    reasoning_effort=resolved["reasoning_effort"],
                    max_tokens=attempt_tokens,
                    timeout=timeout,
                    system=system,
                )
            else:
                text = complete_text_litellm(
                    prompt=prompt,
                    model=resolved["model"],
                    api_key=resolved["api_key"],
                    base_url=resolved["base_url"],
                    reasoning_effort=resolved["reasoning_effort"],
                    completion_kwargs=resolved["completion_kwargs"],
                    temperature=temperature,
                    max_tokens=attempt_tokens,
                    timeout=timeout,
                    system=system,
                )
            if not text:
                raise ValueError("model returned empty text")
            return text
        except (ValueError, AttributeError, RuntimeError) as exc:
            last_error = exc
            if attempt >= retries:
                break
            attempt_tokens = int(attempt_tokens * 1.5)
            print(
                f"[llm] text call returned no usable reply ({exc}); "
                f"retrying with max_tokens={attempt_tokens} ...",
                flush=True,
            )
    raise RuntimeError(
        f"text model {profile_name!r} failed after retries: {last_error}"
    ) from last_error


def call_json_model(
    *,
    prompt: str,
    system: str,
    profile_name: str,
    config_file: Path,
    max_tokens: int,
    temperature: float = 0.1,
) -> dict[str, Any]:
    profile = _load_llm_profile(config_file, profile_name)
    model = str(profile.get("model"))
    api_key = (
        profile.get("api_key")
        or os.environ.get("LITELLM_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
    )
    base_url = profile_base_url(profile)
    reasoning_effort = profile.get("reasoning_effort")
    completion_kwargs = profile.get("completion_kwargs")
    api = resolve_api("auto", model)
    return generate_vignette(
        prompt=prompt,
        model=model,
        api=api,
        api_key=api_key,
        base_url=base_url,
        reasoning_effort=reasoning_effort,
        completion_kwargs=completion_kwargs if isinstance(completion_kwargs, dict) else None,
        temperature=temperature,
        max_tokens=max_tokens,
        system=system,
    )


class LLMCheck(Check):
    """Base for checks that call an LLM and turn its JSON output into Issues."""

    cost_tier: ClassVar[CostTier] = "expensive"
    system_prompt: ClassVar[str] = ""
    user_prompt_template: ClassVar[str] = ""
    max_tokens: ClassVar[int] = 4000

    @abstractmethod
    def _format_args(self, vignette: dict[str, Any], ctx: CheckContext) -> dict[str, Any]:
        """Return the keyword args ``user_prompt_template.format()`` will consume."""

    def _is_applicable(self, vignette: dict[str, Any], ctx: CheckContext) -> bool:
        return True

    _MAX_LLM_RETRIES: ClassVar[int] = 2

    def __call__(self, vignette: dict[str, Any], ctx: CheckContext) -> list[Issue]:
        if not self._is_applicable(vignette, ctx):
            return []
        if ctx.config_file is None:
            raise ValueError(f"LLMCheck {self.name!r} requires ctx.config_file")
        if not ctx.llm_profile:
            raise ValueError(f"LLMCheck {self.name!r} requires ctx.llm_profile")
        prompt = self.user_prompt_template.format(**self._format_args(vignette, ctx))
        last_exc: Exception | None = None
        for _attempt in range(1 + self._MAX_LLM_RETRIES):
            try:
                raw = call_json_model(
                    prompt=prompt,
                    system=self.system_prompt,
                    profile_name=ctx.llm_profile,
                    config_file=ctx.config_file,
                    max_tokens=self.max_tokens,
                )
                return self._parse_issues(raw)
            except Exception as exc:
                last_exc = exc
        return [
            Issue(
                id=f"{self.name}_llm_error",
                location="$",
                severity="minor",
                problem=f"{self.name} LLM call failed after {1 + self._MAX_LLM_RETRIES} attempts: {last_exc}",
                repair_instruction="Retry the LLM call or skip this check.",
                source=self.name,
            )
        ]

    _SELF_DISMISS_PATTERNS: ClassVar[tuple[str, ...]] = (
        "no repair needed",
        "no repair required",
        "no action needed",
        "no action required",
        "acceptable as is",
        "acceptable materialization",
        "acceptable specialization",
        "no issue",
        "not an issue",
        "n/a",
    )

    def _parse_issues(self, raw: dict[str, Any]) -> list[Issue]:
        items = raw.get("issues") if isinstance(raw, dict) else None
        if not isinstance(items, list):
            return []
        out: list[Issue] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            repair = str(item.get("repair_instruction", "")).strip().lower()
            problem = str(item.get("problem", "")).strip().lower()
            if any(p in repair or p in problem[:80] for p in self._SELF_DISMISS_PATTERNS):
                continue
            severity = str(item.get("severity", "")).strip().lower()
            if severity not in ("minor", "major", "blocker"):
                severity = "major"
            out.append(
                Issue(
                    id=str(item.get("id", "unspecified")).strip() or "unspecified",
                    location=str(item.get("location", "$")).strip() or "$",
                    severity=severity,
                    problem=str(item.get("problem", "")).strip(),
                    repair_instruction=str(item.get("repair_instruction", "")).strip(),
                    source=self.name,
                )
            )
        return out
