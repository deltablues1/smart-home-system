# LLMs and cost

> 🇭🇷 [Hrvatska verzija](../hr/modeli-i-trosak.md)

The agents are built on Google ADK, which speaks Gemini natively. Claude is
reached through ADK's LiteLLM wrapper, selected per agent, with Gemini kept
wherever a Google-only feature is needed. Every model call is counted, priced
and capped.

Source of truth: `agents/adk_agents/adk_agent_factory.py`,
`config/runtime_patches.py`, `tools/observability/token_stats.py`,
`services/budget.py`.

---

## Choosing a model per agent

```bash
LLM_PROVIDER=anthropic          # or gemini
ANTHROPIC_API_KEY=...
```

With `anthropic`, each agent resolves its model in this order:

1. `CLAUDE_AGENT_MODELS` — explicit `agent=model` pairs win over everything.
2. Content and reasoning workers (analyst, mailer, researcher, scribe,
   synthesizer) → `CLAUDE_PRO_MODEL`.
3. Everything else → the tier mapped from the agent's Gemini tier
   (`CLAUDE_FLASH_MODEL`, `CLAUDE_LITE_MODEL`).

Agents that depend on Vertex AI RAG corpora — `socrates`, `christian_guide` —
stay on Gemini regardless; `CLAUDE_GEMINI_ONLY_AGENTS` adds more. If LiteLLM or
the key is missing, the factory logs a warning and falls back to Gemini for that
agent rather than failing.

On the Pi every Claude agent runs `claude-sonnet-5` with adaptive thinking on.

### Things Sonnet 5 does differently

- It rejects non-default sampling parameters, so the factory strips
  `temperature` for Sonnet 5, Opus 4.7+ and Fable. Two agents that built their
  generation config by hand after the factory had cleaned it broke every
  orchestrated request until a shared helper,
  `claude_safe_generation_kwargs`, became the only way to build one.
- Thinking tokens count against the output cap. Caps below 8,192 truncated
  tool-call JSON mid-argument, which is how one agent looped on a malformed
  call; the factory raises them.
- LiteLLM is set to drop unsupported parameters as a last line of defence.

---

## Prompt caching

Anthropic bills cache reads at roughly a tenth of the input price, so the
static part of every prompt is marked cacheable. Two details made the
difference between a working cache and `cached=0`:

- **A cache break.** Prompts that need the current time keep it below a
  `<!-- CACHE_BREAK -->` marker. Everything above is identical on every request
  and cached; the clock line is not. Putting the date at the top of a prompt
  broke the cache every minute.
- **A breakpoint at the end of the conversation**, not only on the system
  prompt. Agents with tools re-send the whole history on every loop iteration;
  without the second breakpoint a deep research run paid full price for the same
  scraped pages eight times ([the $6.28 question](engineering-notes.md#a-research-question-that-cost-628)).

---

## Counting tokens

`tools/observability/token_stats.py` hooks ADK's `after_model_callback`, so it
sees every model call whatever the provider, and records per agent and model:
calls, input, output, cache-read tokens and an estimated cost. After each turn a
report goes to the log:

```
[TOKENS]
TOKEN USAGE (turn)
agent             model                 calls       in     out  cached       ~$
smart_orchestrator claude-sonnet-5          2     9120     410    8400   0.0118
smart_home        claude-sonnet-5           1     3050     120    2600   0.0034
```

Telegram exposes the cumulative numbers with `/tokens` (and `/tokens reset`).

Cost is derived, not measured: token counts are exact, prices come from a table
in the same module, and cache reads are credited at `CACHE_READ_RATE` (0.1).
The first-call cache-write premium is not tracked separately, so the figure is
a close estimate rather than an invoice.

---

## A ceiling

`DAILY_LLM_BUDGET_USD` caps estimated spend per day (`services/budget.py`,
state in `data/llm_budget.json`). It exists because of an incident: false wake
words ran ~6.9 million tokens of full agent turns in one day
([details](engineering-notes.md#false-wakes-are-an-invoice)).

---

## Where the money goes

Two measurements are worth keeping in mind:

| Case | Tokens | Cost | Cause |
|------|--------|------|-------|
| One deep research question, Opus | 1.18 M in, 43 k cached | $6.28 | tool loop re-sending scraped pages |
| One false wake word | ~56 k per turn | a few cents, times follow-up turns | full orchestration for noise |

The routing decisions in the [architecture](architecture.md#the-voice-router)
are cost decisions as much as latency ones: a time question costs nothing, a
general question goes to a ~1,500-token agent, and only real tasks reach the
~7,000-token orchestrator.
