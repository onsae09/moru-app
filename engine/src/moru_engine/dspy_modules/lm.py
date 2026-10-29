"""LM factory and engine-wide DSPy configuration.

Any provider is addressed by a single LiteLLM model string
("openai/gpt-5.6-luna", "anthropic/...", "ollama_chat/qwen3:8b", ...);
no per-provider adapter code is needed.

The locally installed coding CLIs (Claude Code, OpenAI Codex, Gemini CLI)
join that contract through LiteLLM custom providers registered at import —
"claude-code/...", "codex/...", "gemini-cli/..." resolve to handlers that
ride the CLI's own OAuth grant instead of an API key.
"""

from __future__ import annotations

import logging
import re
from typing import Literal

import dspy
import litellm
from dspy.utils.exceptions import AdapterParseError
from pydantic import ValidationError

from ..cli_providers import CLI_PROVIDER_IDS, register_cli_providers, to_wire_model

logger = logging.getLogger(__name__)

register_cli_providers()

DEFAULT_TEMPERATURE = 0.3
DEFAULT_MAX_TOKENS = 8192
#: Claude with thinking on: room for thinking plus a full batch, while
#: staying a non-streaming request (Anthropic guidance: ~16k).
THINKING_CLAUDE_MAX_TOKENS = 16000

#: Ollama's own default context window is VRAM-tiered and only 4096 tokens
#: below 24 GiB (ollama server/routes.go), while the compiled instructions
#: plus one batch run 36-41k characters (~16k tokens). An overflowing
#: prompt is TRUNCATED, not rejected: ollama drops the oldest messages and
#: logs it at debug level only (server/prompt.go), so a run silently loses
#: the GEPA instructions and degrades with nothing to observe. Sized for
#: prompt + DEFAULT_MAX_TOKENS of completion with headroom.
DEFAULT_OLLAMA_NUM_CTX = 32768
#: Ollama unloads an idle runner after 5 minutes, and a runner carrying a
#: LoRA adapter costs a full process restart to bring back (ollama
#: server/sched.go needsReload compares AdapterPaths). Pack runs have
#: gaps longer than that between provider calls.
DEFAULT_OLLAMA_KEEP_ALIVE = "30m"
#: litellm's own default request_timeout is 6000s, so a wedged local
#: request would hold a client admission slot for 100 minutes. Long enough
#: to absorb a deep server-side queue, short enough to be shed.
DEFAULT_OLLAMA_TIMEOUT = 600.0
#: A local runtime serves OLLAMA_NUM_PARALLEL (default 1) requests at a
#: time and queues the rest, so blind client retries underneath our own
#: admission limiter are pure amplification. The limiter owns that budget
#: (see the pipeline's transport-failure path); leave one cheap retry here
#: for a genuinely dropped connection.
DEFAULT_OLLAMA_NUM_RETRIES = 1
#: Fraction of the configured context window at which an observed
#: prompt_tokens count means the window is full and ollama truncated.
_CONTEXT_FULL_RATIO = 0.98


#: "claude-sonnet-5-5", "claude-opus-5", OpenRouter's "claude-sonnet-4.6".
_CLAUDE_ID_RE = re.compile(r"claude-(?:opus|sonnet|haiku|fable|mythos)-(\d+)(?:[.-](\d+))?")


def _claude_version(model: str) -> tuple[int, int] | None:
    """(major, minor) of a Claude model id on any route, else None."""
    match = _CLAUDE_ID_RE.search(model.rsplit("/", 1)[-1].lower())
    if match is None:
        return None
    minor = match.group(2)
    # A trailing 8-digit date suffix ("claude-haiku-4-5-20251001") never
    # reaches group 2, which stops at the first numeric segment.
    return int(match.group(1)), int(minor) if minor is not None else 0


def _claude_rejects_temperature(model: str) -> bool:
    version = _claude_version(model)
    return version is not None and version >= (4, 7)


def build_lm(
    model: str,
    *,
    api_key: str | None = None,
    api_base: str | None = None,
    temperature: float = DEFAULT_TEMPERATURE,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    cache: bool = True,
    **extra: object,
) -> dspy.LM:
    """Build a dspy.LM from a LiteLLM model string.

    Args:
        model: LiteLLM model identifier, e.g. "openai/gpt-5.6-luna" or
            "ollama_chat/qwen3:8b".
        api_key: Provider API key; falls back to provider env vars.
        api_base: Override base URL (Ollama, proxies).
        temperature: Sampling temperature.
        max_tokens: Completion token cap.
        cache: Enable the DSPy disk cache (free re-runs of identical batches).
        extra: Passed through to litellm (e.g. reasoning_effort; for
            openrouter/ models it is translated to the native
            extra_body reasoning object).
    """
    kwargs: dict[str, object] = {
        "temperature": temperature,
        "max_tokens": max_tokens,
        "cache": cache,
    }
    # GPT-5-family models (direct "openai/gpt-5*" or via OpenRouter) accept
    # only the default temperature: LiteLLM's gpt-5 transformation raises
    # UnsupportedParamsError for temperature != 1 on ids missing from its
    # bundled model map (e.g. the 5.6 family). temperature=None is never
    # forwarded (dspy keeps the key, LiteLLM skips None), so the provider
    # default applies.
    if model.rsplit("/", 1)[-1].lower().startswith("gpt-5"):
        kwargs["temperature"] = None
    # Claude Opus 4.7+ and the whole 5.x line reject any non-default
    # sampling parameter (400; LiteLLM raises UnsupportedParamsError before
    # sending), and extended thinking on any Claude runs only at the
    # default temperature.
    claude = _claude_version(model)
    claude_thinks = claude is not None and (claude >= (5, 0) or "reasoning_effort" in extra)
    if _claude_rejects_temperature(model) or claude_thinks:
        kwargs["temperature"] = None
    # Thinking tokens count against max_tokens, and the 5.x line thinks by
    # default. 8192 then truncates a large batch mid-JSON, which costs a
    # split-and-retry. A ceiling, not a charge; explicit values are kept.
    if claude_thinks and max_tokens == DEFAULT_MAX_TOKENS:
        kwargs["max_tokens"] = THINKING_CLAUDE_MAX_TOKENS
    if model.startswith("ollama"):
        if "reasoning_effort" not in extra:
            # Local thinking models (qwen3 family) burn the whole completion
            # budget on reasoning_content and return empty text. Translation
            # batches need the tokens for output; litellm maps
            # reasoning_effort="disable" to Ollama think=false. Override by
            # passing reasoning_effort explicitly.
            extra["reasoning_effort"] = "disable"
        # Locally hosted runtime defaults. Ollama ships none of these in a
        # shape that suits a 36-41k-character prompt driven concurrently,
        # and every one of them is silent when wrong; see the constants.
        # setdefault so an explicit caller value always wins.
        extra.setdefault("num_ctx", DEFAULT_OLLAMA_NUM_CTX)
        extra.setdefault("keep_alive", DEFAULT_OLLAMA_KEEP_ALIVE)
        extra.setdefault("timeout", DEFAULT_OLLAMA_TIMEOUT)
        extra.setdefault("num_retries", DEFAULT_OLLAMA_NUM_RETRIES)
    if model.startswith("openrouter/") and "reasoning_effort" in extra:
        # litellm's openrouter integration rejects the OpenAI-style
        # reasoning_effort parameter outright (UnsupportedParamsError) but
        # forwards extra_body verbatim; OpenRouter's unified `reasoning`
        # object is the native control: {"enabled": false} turns thinking
        # off, {"effort": "low"|"medium"|"high"} bounds it.
        effort = extra.pop("reasoning_effort")
        reasoning: dict[str, object] = (
            {"enabled": False}
            if effort in ("disable", "none", "off")
            else {"effort": effort}
        )
        body = extra.get("extra_body")
        if isinstance(body, dict):
            body["reasoning"] = reasoning
        else:
            extra["extra_body"] = {"reasoning": reasoning}
    if model.startswith(("anthropic/", "openrouter/anthropic/")):
        # Anthropic caches nothing unless a block is marked, unlike OpenAI,
        # Gemini, DeepSeek and xAI which cache a repeated prefix on their
        # own. The system message carries the compiled instructions (~36k
        # characters, identical on every batch of a run), so marking it
        # bills every batch after the first at the cache-read rate. The
        # prompt text is unchanged, so output is too.
        extra.setdefault(
            "cache_control_injection_points",
            [{"location": "message", "role": "system"}],
        )
    provider = model.partition("/")[0]
    if provider in CLI_PROVIDER_IDS and "reasoning_effort" in extra:
        # LiteLLM validates OpenAI-style parameters before dispatching to a
        # CustomLLM.  Our CLI handlers consume reasoning_effort themselves,
        # so explicitly allow it through that pre-dispatch gate. Without this
        # every Codex batch fails before CodexLLM.acompletion is reached.
        allowed = list(extra.get("allowed_openai_params") or [])
        if "reasoning_effort" not in allowed:
            allowed.append("reasoning_effort")
        extra["allowed_openai_params"] = allowed
    kwargs.update(extra)
    if api_key:
        kwargs["api_key"] = api_key
    if api_base:
        kwargs["api_base"] = api_base
    logger.info("Building LM: %s", model)
    # CLI providers only: keeps LiteLLM from hijacking a model whose bare
    # name collides with its built-in OpenAI table. No-op for everything else.
    return dspy.LM(to_wire_model(model), **kwargs)


def configure_engine(lm: dspy.LM, *, json_adapter: bool = True) -> None:
    """Configure global DSPy settings for the engine process.

    JSONAdapter keeps dict-typed fields robust on small models
    (structured-output where the provider supports it).
    """
    adapter = dspy.JSONAdapter() if json_adapter else None
    dspy.configure(lm=lm, adapter=adapter)


#: litellm failure classes that mean "the server could not serve this now".
_TRANSPORT_EXCEPTIONS = (
    litellm.exceptions.Timeout,
    litellm.exceptions.APIConnectionError,
    litellm.exceptions.RateLimitError,
    litellm.exceptions.ServiceUnavailableError,
    litellm.exceptions.InternalServerError,
)
#: HTTP statuses meaning "retry later", never "your request was wrong".
#: Ollama answers 503 with "server busy, please try again." once its
#: pending queue (OLLAMA_MAX_QUEUE, 512) overflows.
_TRANSPORT_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


def is_transport_error(exc: BaseException) -> bool:
    """Whether a failed provider call means the server could not serve it,
    as opposed to the model having answered badly.

    The two demand opposite responses, which is why they must not share a
    retry budget. A malformed or oversized response is a property of the
    batch, so splitting it and asking again is right. A timeout, dropped
    connection or 429/503 is a property of the SERVER, and splitting
    doubles the concurrent load that caused it: a local Ollama runtime
    serves OLLAMA_NUM_PARALLEL (default 1) requests at a time and queues
    the rest with no queue-wait timeout, so amplifying there is a
    congestion collapse rather than a retry.

    A 400-class request error (including ContextWindowExceededError) is
    deliberately NOT transport: retrying it unchanged cannot help.
    """
    if isinstance(exc, TimeoutError):
        return True
    if isinstance(exc, _TRANSPORT_EXCEPTIONS):
        return True
    status = getattr(exc, "status_code", None)
    return isinstance(status, int) and status in _TRANSPORT_STATUS


#: Phrases a provider uses when the ACCOUNT is out of capacity: spent prepaid
#: credit, a daily request quota, a subscription usage window. Nothing inside
#: one run can clear these — only time or a top-up can.
_QUOTA_MARKERS = (
    # OpenAI's out-of-credit code. LiteLLM's own message drops it ("You
    # exceeded your current quota…", which Gemini also sends for per-minute
    # limits); it survives on the SDK error LiteLLM wraps (_refusal_text).
    "insufficient_quota",
    "credit balance is too low",  # Anthropic (400)
    "insufficient credit",  # OpenRouter "Insufficient credits" (402)
    "insufficient balance",  # DeepSeek (402)
    "insufficient_balance",
    "payment required",  # any 402
    "perday",  # Gemini quota ids: GenerateRequestsPerDayPerProjectPerModel
    "per day",
    "per_day",
    "daily",
    "usage limit",  # Claude / Codex subscription windows
    "사용량 한도",  # the CLI providers' own 429 message (cli_providers/*)
)
#: Throttling phrases. The provider refused for capacity, but the window may
#: reopen by itself: a per-minute limit and a daily one can look identical on
#: the wire (Gemini answers both with 429 RESOURCE_EXHAUSTED).
_RATE_MARKERS = (
    "rate limit",
    "rate_limit",
    "too many requests",
    "resource_exhausted",
    "resource exhausted",
    "quota",
)


def _refusal_text(exc: BaseException) -> str:
    """Lower-cased text of a provider error and the errors it wraps.

    LiteLLM re-raises the provider SDK's error under its own message and
    keeps machine-readable fields only on the wrapped one (OpenAI's
    ``code="insufficient_quota"``), so the cause chain is read too. Model
    output failures in the chain are skipped: their text quotes the model's
    response, where ``quest.daily_reward.title`` must not read as a quota.
    """
    parts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen and len(seen) < 8:
        seen.add(id(current))
        if not isinstance(current, AdapterParseError | ValidationError):
            parts.append(str(current))
            parts.extend(
                str(value)
                for attr in ("code", "type", "body")
                if (value := getattr(current, attr, None))
            )
        current = current.__cause__ or current.__context__
    return " ".join(parts).lower()


def provider_limit(exc: BaseException) -> Literal["quota", "rate"] | None:
    """Whether a failed call means the provider refused for capacity.

    ``"quota"``: the account is out of capacity (credit, daily quota, usage
    window); retrying, splitting or continuing the run only burns requests,
    so the run stops at once.
    ``"rate"``: a throttling refusal that may clear by itself; the run stops
    only once nothing got through for ``CAPACITY_GIVE_UP_SECONDS``.
    None: not a capacity refusal at all — including model-output failures,
    whose text quotes the model's response.
    """
    if isinstance(exc, AdapterParseError | ValidationError):
        return None
    message = _refusal_text(exc)
    status = getattr(exc, "status_code", None)
    if status == 402 or any(marker in message for marker in _QUOTA_MARKERS):
        return "quota"
    if (
        status == 429
        or isinstance(exc, litellm.exceptions.RateLimitError)
        or any(marker in message for marker in _RATE_MARKERS)
    ):
        return "rate"
    return None


def _usage_value(usage: object, key: str) -> object:
    """Read a usage field from either a dict or a pydantic-style object."""
    if isinstance(usage, dict):
        return usage.get(key)
    return getattr(usage, key, None)


def _cached_tokens(usage: object) -> int:
    """Provider-cached prompt tokens from a LiteLLM usage payload.

    OpenAI-compatible providers nest the count under
    prompt_tokens_details.cached_tokens; Anthropic reports
    cache_read_input_tokens at the top level. Either level may be a dict or
    a pydantic model, so read both ways and take whichever is non-zero.
    """
    details = _usage_value(usage, "prompt_tokens_details")
    cached = _usage_value(details, "cached_tokens")
    if not cached:
        cached = _usage_value(usage, "cache_read_input_tokens")
    try:
        return max(int(cached or 0), 0)
    except (TypeError, ValueError):
        return 0


def token_usage(lm: dspy.LM) -> dict[str, int]:
    """Aggregate prompt/completion token usage from an LM's call history.

    cached_tokens is the cumulative share of prompt_tokens served from the
    provider prompt cache, clamped per call so it can never exceed
    prompt_tokens.
    """
    prompt = 0
    completion = 0
    cached = 0
    for entry in getattr(lm, "history", None) or []:
        usage = entry.get("usage") or {}
        entry_prompt = int(_usage_value(usage, "prompt_tokens") or 0)
        prompt += entry_prompt
        completion += int(_usage_value(usage, "completion_tokens") or 0)
        cached += min(_cached_tokens(usage), entry_prompt)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "cached_tokens": cached,
    }


def context_window_filled(lm: dspy.LM) -> int:
    """Largest served prompt_tokens that filled the configured num_ctx.

    Ollama does not reject a prompt longer than the context window: it
    drops the oldest messages, keeps the system and final message, and logs
    that at debug level only. So a window too small for the compiled
    instructions degrades output with no error anywhere. The observable
    symptom is prompt_eval_count saturating at the window, which is what
    this reports; 0 means no window is configured or nothing came close.
    """
    window = lm.kwargs.get("num_ctx") if hasattr(lm, "kwargs") else None
    if not isinstance(window, int) or window <= 0:
        return 0
    threshold = window * _CONTEXT_FULL_RATIO
    worst = 0
    for entry in getattr(lm, "history", None) or []:
        prompt = int(_usage_value(entry.get("usage") or {}, "prompt_tokens") or 0)
        if prompt >= threshold:
            worst = max(worst, prompt)
    return worst
