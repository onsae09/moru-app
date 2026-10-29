"""Provider-quirk parameter selection, and the transport/output failure
split that keeps a slow server from being treated as a bad answer.

LiteLLM's gpt-5 transformation rejects temperature != 1 for model ids
missing from its bundled map (the 5.6 family), so build_lm must send
temperature=None (never forwarded) for every gpt-5-family id while
leaving other providers' sampling temperature intact.

Nothing here reaches a real model or a real Ollama server: build_lm is
asserted on its assembled kwargs, and the pipeline tests inject a fake
translator at the same seam test_orchestrator.py uses.
"""

import json
from pathlib import Path

import dspy
import litellm
import pytest
from litellm.utils import get_optional_params

from moru_engine.dspy_modules.lm import (
    DEFAULT_CLI_NUM_RETRIES,
    DEFAULT_NUM_RETRIES,
    DEFAULT_OLLAMA_KEEP_ALIVE,
    DEFAULT_OLLAMA_NUM_CTX,
    DEFAULT_OLLAMA_NUM_RETRIES,
    DEFAULT_OLLAMA_TIMEOUT,
    DEFAULT_TIMEOUT,
    build_lm,
    context_window_filled,
    is_transport_error,
)
from moru_engine.pipeline import orchestrator
from moru_engine.pipeline.orchestrator import PipelineConfig, TranslationPipeline


def test_gpt56_direct_omits_temperature():
    lm = build_lm("openai/gpt-5.6-luna", temperature=0.3, cache=False)
    assert lm.kwargs["temperature"] is None
    assert lm.kwargs["max_tokens"] == 8192


def test_gpt56_via_openrouter_omits_temperature():
    lm = build_lm("openrouter/openai/gpt-5.6-sol", temperature=0.7, cache=False)
    assert lm.kwargs["temperature"] is None


def test_non_gpt5_models_keep_temperature():
    lm = build_lm("anthropic/claude-sonnet-4-6", temperature=0.3, cache=False)
    assert lm.kwargs["temperature"] == 0.3

    lm = build_lm("openai/gpt-4.1", temperature=0.3, cache=False)
    assert lm.kwargs["temperature"] == 0.3


def test_openrouter_reasoning_effort_maps_to_extra_body():
    """litellm rejects reasoning_effort for openrouter; build_lm must
    translate it to the native extra_body reasoning object."""
    lm = build_lm(
        "openrouter/~deepseek/deepseek-v4-flash-latest",
        reasoning_effort="disable",
        cache=False,
    )
    assert "reasoning_effort" not in lm.kwargs
    assert lm.kwargs["extra_body"]["reasoning"] == {"enabled": False}

    lm = build_lm(
        "openrouter/anthropic/claude-sonnet-5",
        reasoning_effort="low",
        cache=False,
    )
    assert "reasoning_effort" not in lm.kwargs
    assert lm.kwargs["extra_body"]["reasoning"] == {"effort": "low"}


def test_reasoning_effort_untouched_off_openrouter():
    lm = build_lm("openai/gpt-4.1", reasoning_effort="low", cache=False)
    assert lm.kwargs["reasoning_effort"] == "low"
    assert "extra_body" not in lm.kwargs

    # Ollama thinking models still default to disable (litellm think=false)
    lm = build_lm("ollama_chat/qwen3:8b", cache=False)
    assert lm.kwargs["reasoning_effort"] == "disable"


def test_ollama_gets_local_runtime_defaults():
    """Ollama ships none of these in a shape that suits a 36-41k-character
    prompt driven concurrently, and each is silent when wrong: an
    over-long prompt is truncated rather than rejected, an idle runner is
    unloaded after 5 minutes, and litellm's own timeout is 6000s."""
    lm = build_lm("ollama_chat/qwen3:8b", cache=False)
    assert lm.kwargs["num_ctx"] == DEFAULT_OLLAMA_NUM_CTX
    assert lm.kwargs["keep_alive"] == DEFAULT_OLLAMA_KEEP_ALIVE
    assert lm.kwargs["timeout"] == DEFAULT_OLLAMA_TIMEOUT
    assert lm.num_retries == DEFAULT_OLLAMA_NUM_RETRIES


def test_explicit_values_beat_the_ollama_defaults():
    lm = build_lm(
        "ollama_chat/qwen3:8b",
        cache=False,
        num_ctx=8192,
        keep_alive="5m",
        timeout=30.0,
        num_retries=0,
    )
    assert lm.kwargs["num_ctx"] == 8192
    assert lm.kwargs["keep_alive"] == "5m"
    assert lm.kwargs["timeout"] == 30.0
    assert lm.num_retries == 0


@pytest.mark.parametrize("model", ["openai/gpt-4.1", "anthropic/claude-sonnet-4-6"])
def test_hosted_providers_keep_their_defaults(model: str):
    """The local-runtime policy must not leak into hosted providers."""
    lm = build_lm(model, cache=False)
    for key in ("num_ctx", "keep_alive"):
        assert key not in lm.kwargs


@pytest.mark.parametrize(
    "model", ["openai/gpt-4.1", "anthropic/claude-sonnet-4-6", "openrouter/x/y"]
)
def test_hosted_providers_get_a_timeout_and_one_retry(model: str):
    """dspy's num_retries=3 nests under MAX_TRANSPORT_RETRIES, and litellm's
    own timeout is 6000s: one wedged call would hold an admission slot for
    100 minutes and a failing batch would be attempted up to 12 times."""
    lm = build_lm(model, cache=False)
    assert lm.kwargs["timeout"] == DEFAULT_TIMEOUT
    assert lm.num_retries == DEFAULT_NUM_RETRIES <= 1


def test_cli_providers_are_not_retried_inside_litellm():
    lm = build_lm("codex/gpt-5.5", cache=False)
    assert lm.num_retries == DEFAULT_CLI_NUM_RETRIES == 0
    assert lm.kwargs["timeout"] == DEFAULT_TIMEOUT


def test_explicit_retry_and_timeout_beat_the_hosted_defaults():
    lm = build_lm("openai/gpt-4.1", cache=False, timeout=30.0, num_retries=3)
    assert lm.kwargs["timeout"] == 30.0
    assert lm.num_retries == 3


@pytest.mark.parametrize(
    "exc",
    [
        litellm.exceptions.Timeout("slow", "qwen3:8b", "ollama_chat"),
        litellm.exceptions.APIConnectionError(
            message="reset", model="qwen3:8b", llm_provider="ollama_chat"
        ),
        litellm.exceptions.RateLimitError(
            message="429", model="gpt-4.1", llm_provider="openai"
        ),
        litellm.exceptions.ServiceUnavailableError(
            message="server busy, please try again.",
            model="qwen3:8b",
            llm_provider="ollama_chat",
        ),
        TimeoutError("asyncio"),
    ],
)
def test_server_side_failures_classify_as_transport(exc: BaseException):
    assert is_transport_error(exc) is True


@pytest.mark.parametrize(
    "exc",
    [
        ValueError("Expected dict, got str"),
        litellm.exceptions.ContextWindowExceededError(
            message="too long", model="qwen3:8b", llm_provider="ollama_chat"
        ),
        litellm.exceptions.AuthenticationError(
            message="bad key", model="gpt-4.1", llm_provider="openai"
        ),
    ],
)
def test_output_and_request_failures_are_not_transport(exc: BaseException):
    """These must reach the split path, not the back-off path: retrying
    them unchanged cannot help, and a malformed response is a property of
    the batch rather than of the server."""
    assert is_transport_error(exc) is False


class _FakeLM:
    def __init__(self, num_ctx: object, prompt_tokens: list[int]) -> None:
        self.kwargs = {} if num_ctx is None else {"num_ctx": num_ctx}
        self.history = [
            {"usage": {"prompt_tokens": n}} for n in prompt_tokens
        ]


def test_context_window_filled_reports_a_saturated_prompt():
    """Ollama truncates an oversized prompt silently, so prompt_tokens
    saturating at num_ctx is the only observable symptom."""
    assert context_window_filled(_FakeLM(4096, [512, 4096])) == 4096
    assert context_window_filled(_FakeLM(4096, [512, 1024])) == 0


def test_context_window_filled_needs_a_configured_window():
    assert context_window_filled(_FakeLM(None, [999_999])) == 0
    assert context_window_filled(_FakeLM(0, [999_999])) == 0


# -- transport vs. output failure handling ---------------------------------


def _pipeline(tmp_path: Path, model: str, **overrides: object):
    config = PipelineConfig(
        modpack_path=tmp_path,
        model=model,
        use_tm=False,
        use_user_glossary=False,
        use_vanilla_glossary=False,
        use_mod_translations=False,
        use_translation_graph=False,
        **overrides,
    )
    return TranslationPipeline(config, lm=dspy.utils.DummyLM([]))


class _RecordingTranslator:
    """Records every dispatched batch; fails per ``fail`` policy."""

    def __init__(self, exc: BaseException, *, only_multi: bool = False) -> None:
        self.exc = exc
        self.only_multi = only_multi
        self.calls: list[dict[str, str]] = []

    async def acall(self, *, entries: dict[str, str], **_: object):
        self.calls.append(dict(entries))
        if self.only_multi and len(entries) == 1:
            return dspy.Prediction(
                translations={key: f"ko-{key}" for key in entries}, failed={}
            )
        raise self.exc


@pytest.mark.asyncio
async def test_transport_failure_never_splits_the_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A timeout means the server is saturated. Splitting would double the
    concurrent load that caused it, so the SAME batch is retried instead
    and every dispatched batch keeps its original size."""
    monkeypatch.setattr(orchestrator, "TRANSPORT_BACKOFF_BASE", 0.0)
    pipeline = _pipeline(tmp_path, "ollama_chat/qwen3:8b")
    translator = _RecordingTranslator(
        litellm.exceptions.Timeout("slow", "qwen3:8b", "ollama_chat")
    )
    pipeline.translator = translator
    batch = {f"k{i}": f"text {i}" for i in range(8)}
    try:
        translations, failed = await pipeline._translate_batch(batch, "", "ctx")
    finally:
        pipeline.close()

    assert translations == {}
    assert set(failed) == set(batch)
    assert all("provider unavailable" in msg for msgs in failed.values() for msg in msgs)
    # Bounded retries of the whole batch — never 2N-1 split calls.
    assert len(translator.calls) == orchestrator.MAX_TRANSPORT_RETRIES + 1
    assert all(len(call) == len(batch) for call in translator.calls)


@pytest.mark.asyncio
async def test_output_failure_still_splits_the_batch(tmp_path: Path) -> None:
    """A malformed response is a property of THIS batch, so bisecting to
    singletons stays the right response."""
    pipeline = _pipeline(tmp_path, "ollama_chat/qwen3:8b")
    translator = _RecordingTranslator(ValueError("Expected dict"), only_multi=True)
    pipeline.translator = translator
    batch = {f"k{i}": f"text {i}" for i in range(4)}
    try:
        translations, failed = await pipeline._translate_batch(batch, "", "ctx")
    finally:
        pipeline.close()

    assert failed == {}
    assert translations == {key: f"ko-{key}" for key in batch}
    assert min(len(call) for call in translator.calls) == 1
    assert max(len(call) for call in translator.calls) == len(batch)


@pytest.mark.asyncio
async def test_transport_failure_shrinks_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(orchestrator, "TRANSPORT_BACKOFF_BASE", 0.0)
    pipeline = _pipeline(tmp_path, "openai/gpt-4.1", max_concurrent=16)
    pipeline.translator = _RecordingTranslator(
        litellm.exceptions.RateLimitError(
            message="429", model="gpt-4.1", llm_provider="openai"
        )
    )
    assert pipeline._llm_limiter.limit == 16
    try:
        await pipeline._translate_batch({"k": "v"}, "", "ctx")
    finally:
        pipeline.close()
    # Halved once per attempt: 16 -> 8 -> 4 -> 2.
    assert pipeline._llm_limiter.limit == 2


def test_ollama_starts_below_the_hosted_concurrency(tmp_path: Path) -> None:
    """15 in parallel against a runtime that serves one at a time only
    inflates queue latency; hosted providers must not change."""
    local = _pipeline(tmp_path, "ollama_chat/qwen3:8b")
    hosted = _pipeline(tmp_path, "openai/gpt-4.1")
    try:
        assert local._llm_limiter.limit == orchestrator.OLLAMA_START_CONCURRENT
        assert hosted._llm_limiter.limit == hosted.config.max_concurrent == 15
    finally:
        local.close()
        hosted.close()


@pytest.mark.asyncio
async def test_limiter_grows_back_on_sustained_success() -> None:
    limiter = orchestrator._AdaptiveLimiter(1, 4)
    assert limiter.limit == 1
    for _ in range(limiter._GROW_AFTER):
        await limiter.record_success()
    assert limiter.limit == 2
    await limiter.record_transport_failure()
    assert limiter.limit == 1
    # Never below the floor, however many failures land.
    for _ in range(5):
        await limiter.record_transport_failure()
    assert limiter.limit == 1


@pytest.mark.asyncio
async def test_limiter_backoff_grows_and_is_capped() -> None:
    limiter = orchestrator._AdaptiveLimiter(8, 8)
    first = await limiter.record_transport_failure()
    second = await limiter.record_transport_failure()
    assert second == first * 2
    for _ in range(20):
        delay = await limiter.record_transport_failure()
    assert delay == orchestrator.TRANSPORT_BACKOFF_MAX
    # A success clears the consecutive-failure streak driving the backoff.
    await limiter.record_success()
    assert await limiter.record_transport_failure() == first
def test_cli_reasoning_effort_passes_litellm_pre_dispatch_validation():
    lm = build_lm("codex/gpt-5.6-terra", reasoning_effort="high", cache=False)

    assert lm.kwargs["reasoning_effort"] == "high"
    assert lm.kwargs["allowed_openai_params"] == ["reasoning_effort"]
    assert get_optional_params(
        model="@/gpt-5.6-terra",
        custom_llm_provider="codex",
        reasoning_effort=lm.kwargs["reasoning_effort"],
        allowed_openai_params=lm.kwargs["allowed_openai_params"],
    )["reasoning_effort"] == "high"


def test_hosted_reasoning_effort_does_not_gain_cli_override():
    lm = build_lm("openai/gpt-5.6-terra", reasoning_effort="high", cache=False)

    assert lm.kwargs["reasoning_effort"] == "high"
    assert "allowed_openai_params" not in lm.kwargs


@pytest.mark.parametrize(
    "model", ["anthropic/claude-sonnet-4-6", "openrouter/anthropic/claude-sonnet-4.6"]
)
def test_anthropic_models_cache_the_system_prompt(model: str):
    """Anthropic caches nothing unless asked. The compiled instructions are
    the same ~36k characters on every batch, so mark the system message."""
    lm = build_lm(model, cache=False)
    assert lm.kwargs["cache_control_injection_points"] == [
        {"location": "message", "role": "system"}
    ]


@pytest.mark.parametrize(
    "model", ["openai/gpt-4.1", "gemini/gemini-3.5-flash", "ollama_chat/qwen3:8b"]
)
def test_auto_caching_providers_get_no_injection(model: str):
    lm = build_lm(model, cache=False)
    assert "cache_control_injection_points" not in lm.kwargs


def test_anthropic_cache_control_reaches_the_wire(monkeypatch):
    """The injection point must survive litellm into the request body."""
    sent: dict = {}

    def fake_post(self, url, *args, **kwargs):
        import json as _json

        sent["body"] = _json.loads(kwargs.get("data") or _json.dumps(kwargs["json"]))
        raise RuntimeError("stop before network")

    monkeypatch.setattr(
        "litellm.llms.custom_httpx.http_handler.HTTPHandler.post", fake_post
    )
    lm = build_lm("anthropic/claude-sonnet-4-6", api_key="sk-test", cache=False)
    with pytest.raises(Exception):
        litellm.completion(
            model=lm.model,
            messages=[
                {"role": "system", "content": "instructions"},
                {"role": "user", "content": "batch"},
            ],
            num_retries=0,
            **{k: v for k, v in lm.kwargs.items() if k != "cache"},
        )
    system = sent["body"]["system"]
    assert system[-1]["cache_control"] == {"type": "ephemeral"}


@pytest.mark.parametrize(
    "model",
    [
        "anthropic/claude-haiku-5-5",
        "anthropic/claude-sonnet-5-5",
        "anthropic/claude-opus-5-5",
        "anthropic/claude-opus-4-8",
        "openrouter/anthropic/claude-sonnet-5.5",
    ],
)
def test_claude_models_without_sampling_params_omit_temperature(model: str):
    """Opus 4.7+ and the 5.x line reject any non-default temperature."""
    lm = build_lm(model, temperature=0.3, cache=False)
    assert lm.kwargs["temperature"] is None


def test_claude_thinking_omits_temperature():
    """Extended thinking only runs at the default temperature."""
    lm = build_lm(
        "anthropic/claude-sonnet-4-6", temperature=0.3, reasoning_effort="low", cache=False
    )
    assert lm.kwargs["temperature"] is None


@pytest.mark.parametrize(
    ("model", "extra"),
    [
        ("anthropic/claude-haiku-5-5", {}),
        ("anthropic/claude-haiku-5-5", {"reasoning_effort": "low"}),
        ("anthropic/claude-sonnet-5-5", {"reasoning_effort": "high"}),
    ],
)
def test_claude_55_request_passes_litellm_validation(monkeypatch, model, extra):
    sent: dict = {}

    def fake_post(self, url, *args, **kwargs):
        import json as _json

        sent["body"] = _json.loads(kwargs.get("data") or _json.dumps(kwargs["json"]))
        raise RuntimeError("stop before network")

    monkeypatch.setattr(
        "litellm.llms.custom_httpx.http_handler.HTTPHandler.post", fake_post
    )
    lm = build_lm(model, api_key="sk-test", cache=False, **extra)
    with pytest.raises(Exception):
        litellm.completion(
            model=lm.model,
            messages=[{"role": "user", "content": "batch"}],
            num_retries=0,
            **{k: v for k, v in lm.kwargs.items() if k != "cache"},
        )
    body = sent["body"]
    assert "temperature" not in body
    assert "budget_tokens" not in json.dumps(body)


@pytest.mark.parametrize(
    ("model", "extra"),
    [
        ("anthropic/claude-haiku-5-5", {}),
        ("openrouter/anthropic/claude-sonnet-5.5", {}),
        ("anthropic/claude-sonnet-4-6", {"reasoning_effort": "high"}),
    ],
)
def test_thinking_claude_gets_room_for_thinking_tokens(model: str, extra: dict):
    """Thinking tokens count against max_tokens; 8192 truncates a batch."""
    lm = build_lm(model, cache=False, **extra)
    assert lm.kwargs["max_tokens"] == 16000


def test_explicit_max_tokens_and_non_thinking_claude_untouched():
    assert build_lm("anthropic/claude-haiku-5-5", max_tokens=1, cache=False).kwargs[
        "max_tokens"
    ] == 1
    assert build_lm("anthropic/claude-sonnet-4-6", cache=False).kwargs["max_tokens"] == 8192


def test_cli_auth_refusal_is_not_a_transport_error() -> None:
    import litellm

    from moru_engine.cli_providers.credentials import CliAuthError
    from moru_engine.dspy_modules.lm import cli_auth_failure, is_transport_error

    original = CliAuthError("log in again")
    try:
        try:
            raise original
        except CliAuthError as inner:
            raise litellm.exceptions.APIConnectionError(
                message=str(inner), llm_provider="claude-code", model="x"
            ) from inner
    except litellm.exceptions.APIConnectionError as wrapped:
        assert cli_auth_failure(wrapped) is original
        assert is_transport_error(wrapped) is False
