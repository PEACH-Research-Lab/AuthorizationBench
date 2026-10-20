Assess agent reasoning/thinking
================================

Goal: across every model we test, capture a comparable signal for "how much
did the model think on this turn". For statistics we use the **server-reported
`reasoning_tokens`** count; reasoning *text* is optional and most providers
hide it by default.


## 1. Where reasoning shows up in an LLM API response

| Field | What it is | Use for our stats? |
|---|---|---|
| `content` | The model's user-visible answer (or text alongside `tool_calls`). Some legacy o1-class models inlined a CoT summary here, but every modern reasoning model we use (gpt-5.x, Claude 4.x, Gemini 3.x, Qwen-thinking) routes reasoning to a separate field. | No. Don't try to scrape CoT out of `content`. |
| `reasoning_content` (a.k.a. `reasoning`) | Plain-text chain-of-thought, when the provider chooses to expose it. | Optional. We don't need it for stats. |
| `thinking_blocks` | Structured reasoning blocks. LiteLLM populates this mostly for Anthropic extended thinking; in our 4-model probe only Sonnet 4.6 produces them, and only when `reasoning.exclude=false`. | Optional. |
| `usage.completion_tokens_details.reasoning_tokens` | **Server-reported count** of reasoning tokens consumed. Subset of `completion_tokens` (i.e. counted, but not echoed in `content`). Universally available across our reasoning providers. | **Yes — this is our primary signal.** |


## 2. What each model returns (probed on T0005a-h)

| Model | `reasoning_content` text | `reasoning_tokens` count | Notes |
|---|---|---|---|
| gpt-4.1 | empty | 0 | non-reasoning model |
| qwen3-coder-30b-a3b-instruct | empty | 0 | non-reasoning model |
| qwen3.6-max-preview | empty | 0 | non-reasoning model |
| gpt-5.4 (`reasoning_effort=medium`) | always empty (OpenAI policy hides CoT) | **3,795** | OpenAI direct |
| claude-sonnet-4.6 (extended thinking, budget=4096) | empty (we set `exclude=true`); could be populated as `thinking_blocks` if we flip it | **271** | OpenRouter, `extra_body.reasoning={max_tokens=4096, exclude=true}` |
| qwen3-vl-30b-a3b-thinking | empty by default; would return `<think>...</think>` text only with `extra_body.reasoning.exclude=false` | **17,301** | OpenRouter, default thinking mode |
| gemini-3.1-pro-preview | empty (Google hides CoT) | **1,534** | OpenRouter default |

Takeaways:
- **Token count is universal**: every reasoning model exposes it via
  `usage.completion_tokens_details.reasoning_tokens`. Non-reasoning models
  report 0 (or omit the field, which we coerce to 0). This is the single
  cross-model number we use for "thinking amount".
- **Reasoning text is rarely free**. Most providers hide it; even the ones
  that *can* return it (OpenRouter for Qwen-thinking / Sonnet) require a
  per-call opt-in (`reasoning.exclude=false`). We don't opt in — we don't
  need the text for our analysis.
- `content` is **never** the right place to look for reasoning.




## 3. Where this lands in our pipeline

After the patches in `patches/`:

- `cost_<task>.json::combined.reasoning_tokens` — accumulated count for the run.
- `traj_<task>.json::trajectory[*].data.reasoning_content` — populated only
  if the provider actually returned text (rare, by design).
- `traj_<task>.json::trajectory[*].data.thinking_blocks` — Anthropic-style
  structured blocks, populated only if `reasoning.exclude=false`.

For analysis we use `cost_*.json::combined.reasoning_tokens` as the single
per-run feature, comparable across models.
