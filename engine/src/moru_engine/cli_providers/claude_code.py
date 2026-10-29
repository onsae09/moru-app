"""Claude Code LiteLLM provider: drives the user's own ``claude`` CLI.

Each request is one headless ``claude -p`` run of the official binary, which
authenticates with whatever subscription login the user already has. Moru
sends no HTTP request to Anthropic itself and holds no token.

A run is configured to behave like a plain completion endpoint:

* ``--system-prompt`` replaces Claude Code's agent prompt with the caller's.
* ``--tools ""`` removes every tool, so a turn can only answer.
* ``--safe-mode`` and ``--setting-sources ""`` keep the user's hooks, MCP
  servers, plugins and CLAUDE.md out of a translation batch.
* ``--no-session-persistence`` stops each batch leaving a session file in
  ``~/.claude/projects``.
* The process runs in an empty temporary directory, with billing variables
  such as ANTHROPIC_API_KEY removed so it stays on the subscription.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import platform
import signal
import subprocess
import tempfile
import threading
import weakref
from typing import Any

from litellm import CustomLLM
from litellm.types.utils import Choices, Message, ModelResponse, PromptTokensDetails, Usage

from .credentials import (
    REASON_LOGIN_REQUIRED,
    CliAuthError,
    claude_env,
    claude_path,
)
from .wire import strip_wire_marker

logger = logging.getLogger(__name__)

#: CLI-style aliases the `claude` CLI accepts, mapped to wire model ids.
_MODEL_ALIASES = {
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-8",
    "haiku": "claude-haiku-4-5",
    "default": "claude-sonnet-4-6",
}

#: Effort when the caller did not ask for any. The 5.x models think by
#: default, which slows a batch and spends subscription quota for no gain
#: on translation, so the floor is requested explicitly.
DEFAULT_EFFORT = "low"
_EFFORTS = ("low", "medium", "high", "xhigh", "max")

#: Matches the engine's request timeout for every other provider.
REQUEST_TIMEOUT_S = 600.0

#: Concurrency. Peak RSS of one `claude -p` run, measured on macOS (about
#: 380 MB), is what the memory-derived cap is built from.
_RSS_MB_PER_PROCESS = 400
#: Share of RAM the translation may occupy with `claude` processes.
_MEMORY_SHARE = 0.5
#: Default ceiling: the pipeline's own default concurrency, so this gate
#: never becomes what makes translation slower than the pipeline asked for.
DEFAULT_MAX_PROCESSES = 15
#: Used when memory cannot be determined. Below the ceiling on purpose:
#: guessing high risks an OOM kill mid-pack.
_UNKNOWN_MEMORY_PROCESSES = 6
#: Operator override. Unlike the memory-derived default it is NOT clamped to
#: the default ceiling, so a machine that can afford more can be told to.
MAX_PROCESSES_ENV = "MORU_CLAUDE_MAX_PROCESSES"

#: Longest system prompt (UTF-8 bytes) passed as an argument; past it the
#: system text is folded into stdin instead. Windows caps the whole command
#: line at 32,767 characters, Linux one argument at 128 KiB.
_ARGV_SYSTEM_LIMIT = 100_000
_WINDOWS_ARGV_SYSTEM_LIMIT = 12_000

_SYSTEM_DELIM = "### SYSTEM INSTRUCTIONS ###"
_INPUT_DELIM = "### INPUT ###"
_FALLBACK_SYSTEM = "You are a precise assistant. Answer exactly as instructed."

_gate: threading.BoundedSemaphore | None = None
_gate_lock = threading.Lock()
#: The async path's gate, one per event loop (asyncio primitives are loop-bound).
_async_gates: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    weakref.WeakKeyDictionary()
)

_AUTH_MARKERS = (
    "not logged in",
    "please run /login",
    "invalid authentication",
    "invalid api key",
    "authentication_error",
    "oauth token has expired",
    "failed to authenticate",
)
_LIMIT_MARKERS = ("usage limit", "rate limit", "rate_limit", "limit reached", "overloaded")


class ClaudeCliError(RuntimeError):
    """`claude` could not answer. Message is aimed at the desktop UI."""


def resolve_model(model: str) -> str:
    """Wire id or CLI alias -> the slug `claude --model` accepts."""
    model = strip_wire_marker(model)
    if model.startswith("claude-code/"):
        model = model[len("claude-code/") :]
    return _MODEL_ALIASES.get(model.strip().lower(), model)


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


def _memory_budget_mb() -> float | None:
    """RAM the `claude` processes may occupy, in MB; None where unknown."""
    system = platform.system()
    try:
        if system == "Linux":
            # Linux reports what is actually free; use that as the budget.
            with open("/proc/meminfo", encoding="utf-8") as handle:
                for line in handle:
                    if line.startswith("MemAvailable:"):
                        return float(line.split()[1]) / 1024.0
        elif system == "Darwin":
            out = subprocess.run(  # noqa: S603,S607 - fixed argv
                ["sysctl", "-n", "hw.memsize"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            return int(out.stdout.strip()) / (1024 * 1024) * _MEMORY_SHARE
    except (OSError, IndexError, ValueError, subprocess.SubprocessError):
        return None
    return None


def process_cap() -> int:
    """How many `claude` processes may run at once.

    ``MORU_CLAUDE_MAX_PROCESSES`` wins and may exceed the default of 15.
    Otherwise the cap follows RAM (half of it on macOS, 400 MB per process)
    and never goes above 15, so an 8 GB laptop gets 10 and a 16 GB one gets
    the full 15.
    """
    override = (os.environ.get(MAX_PROCESSES_ENV) or "").strip()
    if override:
        try:
            return max(1, int(override))
        except ValueError:
            logger.warning("Ignoring non-numeric %s=%r", MAX_PROCESSES_ENV, override)
    budget = _memory_budget_mb()
    if budget is None:
        return _UNKNOWN_MEMORY_PROCESSES
    return max(1, min(int(budget // _RSS_MB_PER_PROCESS), DEFAULT_MAX_PROCESSES))


def _acquire_slot() -> threading.BoundedSemaphore:
    global _gate
    with _gate_lock:
        if _gate is None:
            cap = process_cap()
            logger.info("Claude Code transport capped at %d concurrent processes", cap)
            _gate = threading.BoundedSemaphore(cap)
        return _gate


def _async_slot() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    gate = _async_gates.get(loop)
    if gate is None:
        cap = process_cap()
        logger.info("Claude Code transport capped at %d concurrent processes", cap)
        gate = _async_gates[loop] = asyncio.Semaphore(cap)
    return gate


# ---------------------------------------------------------------------------
# Invocation
# ---------------------------------------------------------------------------


def _text_of(content: object) -> str:
    """Flatten an OpenAI-style content field to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(str(part.get("text", "")))
            elif isinstance(part, str):
                parts.append(part)
        return "\n".join(parts)
    return "" if content is None else str(content)


def build_prompt(messages: list[dict[str, Any]]) -> tuple[str, str]:
    """OpenAI-style messages -> (system text, stdin prompt).

    The CLI takes one system prompt and one user prompt. Earlier turns of a
    multi-turn exchange are folded into the prompt under role labels; the
    engine sends a single user turn, so this is the rare path.
    """
    system: list[str] = []
    turns: list[tuple[str, str]] = []
    for msg in messages:
        role = msg.get("role")
        text = _text_of(msg.get("content"))
        if not text.strip():
            continue
        if role in ("system", "developer"):
            system.append(text)
        elif role in ("user", "assistant"):
            turns.append((str(role), text))
    if len(turns) <= 1:
        user = turns[0][1] if turns else "."
    else:
        user = "\n\n".join(f"[{role}]\n{text}" for role, text in turns)
    return "\n\n".join(system), user


def _system_limit() -> int:
    return _WINDOWS_ARGV_SYSTEM_LIMIT if os.name == "nt" else _ARGV_SYSTEM_LIMIT


def build_invocation(
    model: str, messages: list[dict[str, Any]], optional_params: dict[str, Any]
) -> tuple[list[str], str]:
    """argv (after the binary) and the stdin text for one request."""
    system, user = build_prompt(messages)
    effort = optional_params.get("reasoning_effort")
    effort = effort if effort in _EFFORTS else DEFAULT_EFFORT

    args = [
        "-p",
        "--model",
        resolve_model(model),
        "--effort",
        effort,
        "--output-format",
        "json",
        "--no-session-persistence",
        "--tools",
        "",
        "--safe-mode",
        "--setting-sources",
        "",
        "--disable-slash-commands",
    ]
    if system and len(system.encode("utf-8")) <= _system_limit():
        args += ["--system-prompt", system]
    elif system:
        # Too big for an argument: it rides stdin under visible delimiters.
        args += ["--system-prompt", _FALLBACK_SYSTEM]
        user = f"{_SYSTEM_DELIM}\n{system}\n\n{_INPUT_DELIM}\n{user}"
    else:
        args += ["--system-prompt", _FALLBACK_SYSTEM]
    return args, user


def _missing_binary() -> CliAuthError:
    return CliAuthError(
        "Claude Code CLI(claude)를 찾을 수 없습니다. "
        "https://docs.claude.com/en/docs/claude-code/setup 를 참고해 설치해 주세요."
    )


def run_claude(args: list[str], stdin_text: str, *, timeout: float) -> subprocess.CompletedProcess[str]:
    binary = claude_path()
    if binary is None:
        raise _missing_binary()
    try:
        with tempfile.TemporaryDirectory(prefix="moru-claude-") as workspace:
            return subprocess.run(  # noqa: S603 - fixed binary, no shell
                [str(binary), *args],
                input=stdin_text,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                cwd=workspace,
                env=claude_env(binary),
                check=False,
            )
    except subprocess.TimeoutExpired as exc:
        raise ClaudeCliError(
            f"Claude Code CLI(claude) 응답이 {timeout:.0f}초 안에 오지 않았습니다."
        ) from exc
    except OSError as exc:
        raise ClaudeCliError(f"Claude Code CLI(claude) 실행에 실패했습니다: {exc}") from exc


async def arun_claude(
    args: list[str], stdin_text: str, *, timeout: float
) -> subprocess.CompletedProcess[str]:
    """`run_claude` on the event loop; the process dies with its caller.

    The child runs in its own process group so a timeout or a cancelled
    translation kills it and anything it spawned instead of leaving a
    process finishing a turn nobody will read.
    """
    binary = claude_path()
    if binary is None:
        raise _missing_binary()
    group: dict[str, Any]
    if os.name == "nt":
        group = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    else:
        group = {"start_new_session": True}
    with tempfile.TemporaryDirectory(prefix="moru-claude-") as workspace:
        try:
            proc = await asyncio.create_subprocess_exec(  # noqa: S603 - fixed binary
                str(binary),
                *args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=workspace,
                env=claude_env(binary),
                **group,
            )
        except OSError as exc:
            raise ClaudeCliError(f"Claude Code CLI(claude) 실행에 실패했습니다: {exc}") from exc
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(stdin_text.encode("utf-8")), timeout
            )
        except TimeoutError as exc:
            await _kill_group(proc)
            raise ClaudeCliError(
                f"Claude Code CLI(claude) 응답이 {timeout:.0f}초 안에 오지 않았습니다."
            ) from exc
        except BaseException:
            await _kill_group(proc)
            raise
    return subprocess.CompletedProcess(
        args=[str(binary), *args],
        returncode=proc.returncode if proc.returncode is not None else -1,
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
    )


async def _kill_group(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        if os.name == "nt":
            proc.kill()
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
    with contextlib.suppress(Exception):
        await proc.wait()


# ---------------------------------------------------------------------------
# Response
# ---------------------------------------------------------------------------


def _login_error(detail: str) -> CliAuthError:
    return CliAuthError(
        "Claude Code 로그인이 필요합니다. 터미널에서 `claude auth login`으로 "
        f"로그인한 뒤 다시 시도해 주세요. {detail[:200]}".strip(),
        code=REASON_LOGIN_REQUIRED,
    )


def read_envelope(proc: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    """The `--output-format json` result, strictly; failures raise."""
    raw = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    if not raw:
        detail = stderr[:300]
        if any(marker in detail.lower() for marker in _AUTH_MARKERS):
            raise _login_error(detail)
        raise ClaudeCliError(f"Claude Code CLI(claude)가 응답하지 않았습니다. {detail}".strip())
    try:
        envelope = json.loads(raw)
    except ValueError:
        # Tolerate banner lines ahead of the JSON document.
        try:
            envelope = json.loads(raw.splitlines()[-1])
        except (ValueError, IndexError) as exc:
            raise ClaudeCliError(
                f"Claude Code CLI(claude) 응답을 해석할 수 없습니다: {raw[:300]}"
            ) from exc
    if isinstance(envelope, list):
        envelope = next(
            (e for e in reversed(envelope) if isinstance(e, dict) and e.get("type") == "result"),
            None,
        )
    if not isinstance(envelope, dict):
        raise ClaudeCliError(f"Claude Code CLI(claude) 응답을 해석할 수 없습니다: {raw[:300]}")

    if envelope.get("is_error") or envelope.get("subtype", "success") != "success":
        text = str(envelope.get("result") or envelope.get("subtype") or "unknown")
        status = envelope.get("api_error_status")
        lowered = text.lower()
        if status in (401, 403) or any(m in lowered for m in _AUTH_MARKERS):
            raise _login_error(text)
        if status == 429 or any(m in lowered for m in _LIMIT_MARKERS):
            raise ClaudeCliError(
                "Claude 구독 사용량 한도에 도달했습니다. 한도가 풀린 뒤 '이어서 번역'으로 "
                f"계속해 주세요. ({text[:200]})"
            )
        raise ClaudeCliError(f"Claude Code 요청이 실패했습니다: {text[:300]}")
    if not isinstance(envelope.get("result"), str):
        raise ClaudeCliError("Claude Code CLI(claude)가 텍스트 답을 주지 않았습니다.")
    return envelope


def _fill_response(
    model_response: ModelResponse, model: str, envelope: dict[str, Any]
) -> ModelResponse:
    finish = {
        "end_turn": "stop",
        "stop_sequence": "stop",
        "max_tokens": "length",
        "refusal": "content_filter",
    }.get(str(envelope.get("stop_reason") or ""), "stop")
    model_response.choices = [
        Choices(
            index=0,
            message=Message(role="assistant", content=envelope["result"]),
            finish_reason=finish,
        )
    ]
    model_response.model = f"claude-code/{resolve_model(model)}"
    usage = envelope.get("usage") or {}
    cache_read = int(usage.get("cache_read_input_tokens") or 0)
    cache_write = int(usage.get("cache_creation_input_tokens") or 0)
    prompt = int(usage.get("input_tokens") or 0) + cache_read + cache_write
    completion = int(usage.get("output_tokens") or 0)
    model_response.usage = Usage(  # type: ignore[attr-defined]
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
        prompt_tokens_details=PromptTokensDetails(cached_tokens=cache_read),
    )
    return model_response


class ClaudeCodeLLM(CustomLLM):
    """LiteLLM provider for ``claude-code/<model>``."""

    def completion(self, *args: Any, **kwargs: Any) -> ModelResponse:
        model = kwargs["model"]
        argv, stdin_text = build_invocation(
            model, kwargs["messages"], kwargs.get("optional_params") or {}
        )
        with _acquire_slot():
            proc = run_claude(argv, stdin_text, timeout=REQUEST_TIMEOUT_S)
        return _fill_response(kwargs["model_response"], model, read_envelope(proc))

    async def acompletion(self, *args: Any, **kwargs: Any) -> ModelResponse:
        model = kwargs["model"]
        argv, stdin_text = build_invocation(
            model, kwargs["messages"], kwargs.get("optional_params") or {}
        )
        async with _async_slot():
            proc = await arun_claude(argv, stdin_text, timeout=REQUEST_TIMEOUT_S)
        return _fill_response(kwargs["model_response"], model, read_envelope(proc))


__all__ = [
    "ClaudeCodeLLM",
    "ClaudeCliError",
    "process_cap",
    "resolve_model",
]
