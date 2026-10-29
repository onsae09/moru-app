"""Antigravity CLI (`agy`) support: model listing and process invocation.

Antigravity is the successor to the `gemini` CLI. It keeps its OAuth session
in the OS keyring rather than a JSON file, which is why this module talks to
the binary instead of reading credentials: the CLI is the only authority on
its own session, and reimplementing Keychain / libsecret / Credential Manager
access would break the first time Google changed storage.

Everything asserted here was verified against the real binary (v1.1.22,
sha512 matched against Google's own release manifest) or its official docs;
nothing is inferred from the model names' shape.
https://antigravity.google/docs/cli/headless
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
import weakref
from pathlib import Path
from typing import Any

from ..placeholder import TOKEN_RE
from .credentials import agy_path
from .wire import strip_wire_marker

logger = logging.getLogger(__name__)

#: Offline fallback lineup. Deliberately a small, current subset rather than
#: an exhaustive dump: every slug here is a literal copied from the official
#: `agy models` sample output, so none of it is guessed.
#:
#: Note the shape — Antigravity slugs carry a reasoning-effort suffix, which
#: is why the legacy ids (`gemini-3.5-flash`, `gemini-3.1-pro-preview`) are
#: NOT valid here. Those remain correct for the legacy HTTP transport, whose
#: model namespace is the Gemini API's, not this one.
#:
#: Claude Opus and GPT-OSS are offered by Antigravity per /docs/models but
#: their slugs never appear literally in any sample, so they are omitted
#: rather than extrapolated from `claude-sonnet-4-6`. A live list supplies
#: them; this fallback stays honest.
#: https://antigravity.google/docs/cli/headless
AGY_FALLBACK_MODELS: tuple[str, ...] = (
    "gemini-3.7-flash-high",
    "gemini-3.7-flash-medium",
    "gemini-3.6-flash-high",
    "gemini-3.6-flash-medium",
    "gemini-3.5-flash-medium",
    "gemini-3.1-pro-high",
    "claude-sonnet-4-6",
)

#: Default for translation: newest Flash at medium effort. Flash because a
#: modpack is thousands of short requests, and 3.7 because /docs/models
#: marks it available on every plan tier including Free — a default that
#: only paid plans can call breaks first-run for everyone else.
AGY_DEFAULT_MODEL = "gemini-3.7-flash-medium"

#: A slug as `agy models` prints it in the first column.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9.\-]*$")


#: Aliases resolved against the AGY namespace. Distinct from gemini_cli's
#: aliases on purpose: `flash` there means the legacy Gemini API id, which
#: this CLI rejects outright.
_AGY_ALIASES = {
    "default": AGY_DEFAULT_MODEL,
    "flash": AGY_DEFAULT_MODEL,
    "pro": "gemini-3.1-pro-high",
}

#: Ids moru offered before Antigravity, mapped to their nearest current
#: slug. These are real Gemini API ids and remain correct on the legacy
#: HTTP transport — they are simply not part of this CLI's namespace, so a
#: saved config carrying one has to be told, not silently downgraded.
_LEGACY_EQUIVALENT = {
    "gemini-3.5-flash": "gemini-3.5-flash-medium",
    "gemini-3.1-pro-preview": "gemini-3.1-pro-high",
    # No flash-lite tier exists in Antigravity at all; Flash is the floor.
    "gemini-3.1-flash-lite": AGY_DEFAULT_MODEL,
}

#: Progress chatter to ignore. `agy models` writes this before the list.
_NOISE_PREFIXES = ("fetching available models", "fetching models")

_MODELS_TIMEOUT = 30.0


class AgyError(RuntimeError):
    """`agy` could not answer. Message is aimed at the desktop UI."""


def resolve_model_for_agy(model: str) -> str:
    """Wire id or alias -> a slug `agy --model` will actually accept.

    Raises rather than guessing when a saved model belongs to the legacy
    namespace. Passing it through would hit agy's own error — headless mode
    exits non-zero on an unknown `--model` instead of falling back — and
    that message names no replacement, so the user would see a failed
    translation with no idea which model to pick. Silently substituting is
    worse still: it would bill a different model than the one displayed.
    """
    slug = model.strip()
    # Tolerate the public catalog form (`gemini-cli/<slug>`) as well as the
    # wire form LiteLLM hands the handler (`@/<slug>`): both reach here from
    # different call sites and neither is wrong.
    if slug.startswith("gemini-cli/"):
        slug = slug[len("gemini-cli/") :]
    slug = strip_wire_marker(slug).strip()
    slug = _AGY_ALIASES.get(slug.lower(), slug)
    replacement = _LEGACY_EQUIVALENT.get(slug)
    if replacement is not None:
        raise AgyError(
            f"'{slug}' 모델은 Antigravity CLI(agy)에서 제공하지 않습니다. "
            f"'{replacement}'(으)로 바꿔 주세요. "
            "설정에서 모델 목록을 새로 불러오면 사용 가능한 모델만 표시됩니다."
        )
    return slug or AGY_DEFAULT_MODEL


def parse_models_output(text: str) -> list[str]:
    """Slugs out of `agy models`' decorated console output.

    THE one place this format is parsed. `agy models` has no JSON mode —
    verified: the subcommand accepts only `-h/--help`, and passing
    `--output-format json` fails with "flags provided but not defined". So
    a text parse is forced on us, and isolating it here means the blast
    radius of Google restyling that output is a single function.

    Deliberately conservative: a line contributes a slug only when its
    first token really looks like one. Anything else — banners, blank
    lines, a trailing "..." continuation marker, error prose — is dropped
    rather than guessed at, so a restyle degrades to the fallback list
    instead of inventing a model id that fails at request time.

    Real observed shape (official sample):
        gemini-3.7-flash-medium   Gemini 3.7 Flash (Medium)
        claude-sonnet-4-6         Claude Sonnet 4.6 (Thinking)
    """
    slugs: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("-"):
            continue
        lowered = line.lower()
        if lowered.startswith(_NOISE_PREFIXES) or lowered.startswith("error:"):
            continue
        token = line.split()[0]
        if not _SLUG_RE.match(token):
            continue
        # A bare "..." or a display-name-only row has no usable slug.
        if token not in slugs:
            slugs.append(token)
    return slugs


def _reports_error(text: str) -> str | None:
    """The error `agy` printed, if any.

    Necessary because exit status is unusable: verified on the real binary,
    `agy models` while signed out prints
    "Error: Please sign in to view available models." and STILL exits 0. So
    rc==0 is not success, and an error line has to be treated as first
    class or a signed-out user silently gets an empty lineup.
    """
    for raw in text.splitlines():
        line = raw.strip()
        if line.lower().startswith("error:"):
            return line[len("error:") :].strip() or line
    return None


def _missing_binary() -> AgyError:
    return AgyError(
        "Antigravity CLI(agy)를 찾을 수 없습니다. "
        "https://antigravity.google/docs/cli/install 를 참고해 설치해 주세요."
    )


def run_agy(
    args: list[str],
    *,
    timeout: float,
    cwd: Path | None = None,
    stdin_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Invoke `agy` non-interactively, never letting it block on a prompt.

    stdin is closed rather than inherited. This is not defensive padding:
    an unauthenticated `agy -p` was observed to print an OAuth URL, wait
    60s, and prompt "paste the authorization code here" on stdin, despite
    the docs claiming it exits instead of hanging. Inside a 15-way
    concurrent translation run, fifteen processes each holding a terminal
    prompt open is the worst failure mode available, so the prompt is
    denied any input and the wait is bounded twice — by `--print-timeout`
    where the caller sets it, and by this hard kill.

    ``stdin_text`` (the stream-json prompt line, see `_invocation`) is
    written and the pipe closed at once, so a prompt for a code still
    reads EOF.
    """
    binary = agy_path()
    if binary is None:
        raise _missing_binary()
    try:
        return subprocess.run(  # noqa: S603 - fixed binary, no shell
            [str(binary), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            input=stdin_text,
            stdin=subprocess.DEVNULL if stdin_text is None else None,
            timeout=timeout,
            cwd=str(cwd) if cwd else None,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AgyError(
            f"Antigravity CLI(agy) 응답이 {timeout:.0f}초 안에 오지 않았습니다."
        ) from exc
    except OSError as exc:
        raise AgyError(f"Antigravity CLI(agy) 실행에 실패했습니다: {exc}") from exc


def list_models(*, timeout: float = _MODELS_TIMEOUT) -> list[str]:
    """Models this account may call, falling back to the documented list.

    Never returns empty. An empty lineup reads to a user as "my
    subscription has no models", which is a worse lie than a slightly
    stale list — and the fallback slugs are real, so picking one still
    produces a working request for any plan that carries it.
    """
    try:
        proc = run_agy(["models"], timeout=timeout)
    except AgyError as exc:
        logger.debug("agy models unavailable: %s", exc)
        return list(AGY_FALLBACK_MODELS)

    combined = f"{proc.stdout}\n{proc.stderr}"
    reported = _reports_error(combined)
    if reported:
        # Signed out, or the backend refused. Not a parse failure, so log
        # it distinctly -- this is the case the exit code lies about.
        logger.info("agy models refused: %s", reported)
        return list(AGY_FALLBACK_MODELS)

    slugs = parse_models_output(proc.stdout)
    if not slugs:
        logger.warning(
            "agy models returned nothing parseable; falling back to the "
            "documented lineup. Output format may have changed."
        )
        return list(AGY_FALLBACK_MODELS)
    return slugs


# ---------------------------------------------------------------------------
# Translation transport — `agy -p` headless mode
# ---------------------------------------------------------------------------

#: Peak RSS of one `agy` process, measured on linux-x64 v1.1.22 via
#: /usr/bin/time (145116 KB). Used to derive a process cap, because the
#: binary is ~208MB and fifteen of these is ~2.2GB — affordable on a
#: developer machine, not on every machine.
_RSS_MB_PER_PROCESS = 145

#: Safety factor over measured RSS: the measurement is a `--version` run,
#: and a real agent turn holds conversation state on top of it.
_RSS_HEADROOM = 2

#: Ceiling regardless of available memory. Matches the orchestrator's own
#: default max_concurrent, so this gate never becomes the thing that makes
#: translation slower than the pipeline asked for.
_MAX_PROCESSES = 15

#: Used when available memory cannot be determined (non-Linux). Deliberately
#: below the ceiling: guessing high risks an OOM kill mid-pack, which loses
#: far more than the throughput a higher guess would have won.
_UNKNOWN_MEMORY_PROCESSES = 6

#: Operator override, for a machine that knows better than this heuristic.
MAX_PROCESSES_ENV = "MORU_AGY_MAX_PROCESSES"

#: Matches `--print-timeout`'s own default (5m0s). Bounded twice: agy gives
#: up on its own, and `run_agy` hard-kills a little later if it does not.
_PRINT_TIMEOUT_S = 300.0
_KILL_GRACE_S = 30.0

#: Forced response shape. An agent narrates by default — it is built to
#: explain itself — so the answer is constrained to one string field and
#: read out of `structured_output`. Narration then has nowhere to go: it
#: cannot appear inside a schema-validated string field without the model
#: having put it there deliberately.
_TEXT_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
}

#: Delimiter used to fold a system prompt into the single `-p` string.
_SYSTEM_DELIM = "### SYSTEM INSTRUCTIONS ###"
_INPUT_DELIM = "### INPUT ###"

_gate: threading.BoundedSemaphore | None = None
_gate_lock = threading.Lock()
#: The async path's gate, one per event loop (see `_async_slot`).
_async_gates: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    weakref.WeakKeyDictionary()
)

#: Longest prompt (UTF-8 bytes, never fewer than the UTF-16 units Windows
#: counts) passed as a `-p` argument. Windows caps the whole command line
#: at 32,767 characters and Linux one argument at 128 KiB; past this the
#: prompt goes over stdin instead (`_invocation`).
_ARGV_PROMPT_LIMIT = 24_000 if os.name == "nt" else 100_000

#: How long one auth refusal suppresses further attempts. Long enough to
#: spare a batch minutes of certain-to-fail waiting, short enough that
#: signing in takes effect without restarting the engine.
_AUTH_FAIL_TTL_S = 60.0
_auth_failed_at: float | None = None
_auth_lock = threading.Lock()


def process_cap() -> int:
    """How many `agy` processes this machine can afford at once.

    A considered limit rather than an OOM. Each process peaks around
    145MB, so the pipeline's default 15-way concurrency wants ~2.2GB of
    headroom; a loaded 8GB laptop does not have it, and the failure mode
    without a cap is the OS killing a translation halfway through a pack.
    """
    override = (os.environ.get(MAX_PROCESSES_ENV) or "").strip()
    if override:
        try:
            return max(1, min(int(override), _MAX_PROCESSES))
        except ValueError:
            logger.warning("Ignoring non-numeric %s=%r", MAX_PROCESSES_ENV, override)

    available_mb = _available_memory_mb()
    if available_mb is None:
        return _UNKNOWN_MEMORY_PROCESSES
    affordable = int(available_mb // (_RSS_MB_PER_PROCESS * _RSS_HEADROOM))
    return max(1, min(affordable, _MAX_PROCESSES))


def _available_memory_mb() -> float | None:
    """MemAvailable in MB, or None where it cannot be read.

    Linux only on purpose: /proc/meminfo is the one source available
    without adding a dependency. Everywhere else the answer is honestly
    unknown, and `process_cap` treats unknown as conservative.
    """
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    return float(line.split()[1]) / 1024.0
    except (OSError, IndexError, ValueError):
        return None
    return None


def _acquire_slot() -> threading.BoundedSemaphore:
    global _gate
    with _gate_lock:
        if _gate is None:
            cap = process_cap()
            logger.info("Antigravity transport capped at %d concurrent processes", cap)
            _gate = threading.BoundedSemaphore(cap)
        return _gate


def build_prompt(messages: list[dict[str, Any]]) -> str:
    """OpenAI-style messages -> the single string `-p` accepts.

    THIS IS A REAL SEMANTIC DIFFERENCE FROM EVERY OTHER PROVIDER, not an
    implementation detail. `agy --help` has no `--system` flag: verified
    against the shipped binary, the only prompt input is `-p`. So the
    system/user separation our signatures rely on cannot be expressed on
    the wire, and the two are folded into one string under explicit
    delimiters instead. A model that ignores the delimiters will treat
    instructions as content, which is exactly why the response shape is
    additionally pinned by `--json-schema`.
    """
    system_parts: list[str] = []
    user_parts: list[str] = []
    for message in messages:
        content = message.get("content")
        text = content if isinstance(content, str) else _flatten_content(content)
        if not text:
            continue
        if message.get("role") == "system":
            system_parts.append(text)
        else:
            user_parts.append(text)
    body = "\n\n".join(user_parts)
    if not system_parts:
        return body
    return (
        f"{_SYSTEM_DELIM}\n"
        + "\n\n".join(system_parts)
        + f"\n\n{_INPUT_DELIM}\n"
        + body
    )


def _flatten_content(content: object) -> str:
    if isinstance(content, list):
        parts = [
            str(item.get("text", ""))
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        return "".join(parts)
    return "" if content is None else str(content)


def complete(model: str, messages: list[dict[str, Any]]) -> tuple[str, dict[str, int]]:
    """One translation turn through `agy`, or a loud failure.

    Returns (text, usage). Never returns agent prose dressed as a
    translation: the answer must arrive as a schema-validated
    `structured_output.text`, and any protected `{{TOKEN}}` present in the
    prompt must survive into it. Both of those raise rather than degrade,
    because a corrupted translation that looks successful is worse than a
    failed request — the failure gets retried, the corruption gets shipped.
    """
    _raise_if_recently_unauthenticated()
    prompt = build_prompt(messages)
    args, stdin_text = _invocation(prompt, model)

    gate = _acquire_slot()
    with gate:
        # Run in an empty directory: `agy` is a workspace agent and would
        # otherwise index, and be permitted to touch, whatever it was
        # launched in — which for a sidecar is the user's own project.
        with tempfile.TemporaryDirectory(prefix="moru-agy-") as workspace:
            proc = run_agy(
                args,
                timeout=_PRINT_TIMEOUT_S + _KILL_GRACE_S,
                cwd=Path(workspace),
                **({} if stdin_text is None else {"stdin_text": stdin_text}),
            )
    return _finish(proc, prompt)


async def acomplete(
    model: str, messages: list[dict[str, Any]]
) -> tuple[str, dict[str, int]]:
    """`complete` for the event loop: an asyncio subprocess, not a thread.

    Through `to_thread` every waiting batch held a default-executor worker
    for up to five minutes (starving every other `to_thread` caller), and a
    cancelled run kept its `agy` process going to completion.
    """
    _raise_if_recently_unauthenticated()
    prompt = build_prompt(messages)
    args, stdin_text = _invocation(prompt, model)
    async with _async_slot():
        with tempfile.TemporaryDirectory(prefix="moru-agy-") as workspace:
            proc = await arun_agy(
                args,
                timeout=_PRINT_TIMEOUT_S + _KILL_GRACE_S,
                cwd=Path(workspace),
                stdin_text=stdin_text,
            )
    return _finish(proc, prompt)


def _finish(
    proc: subprocess.CompletedProcess[str], prompt: str
) -> tuple[str, dict[str, int]]:
    try:
        result = _read_envelope(proc, prompt)
    except AgyError as exc:
        _note_auth_failure(str(exc))
        raise
    _clear_auth_failure()
    return result


def _invocation(prompt: str, model: str) -> tuple[list[str], str | None]:
    """argv for one turn, plus the stdin line when the prompt goes there.

    A prompt within `_ARGV_PROMPT_LIMIT` rides `-p`, the verified path.
    A longer one cannot: CreateProcess refuses the whole command line
    (WinError 206) past 32,767 characters, and a pack batch runs ~40k. It
    is sent instead as one documented stream-json `user` event on stdin,
    still one fresh process per batch, so no conversation is shared.
    """
    common = [
        "--json-schema",
        json.dumps(_TEXT_SCHEMA),
        # A translated string can legitimately begin with "/" and must not
        # be expanded as a slash command or skill.
        "--disable-slash-commands",
        "--print-timeout",
        f"{int(_PRINT_TIMEOUT_S)}s",
    ]
    if model:
        common += ["--model", model]
    if len(prompt.encode("utf-8")) <= _ARGV_PROMPT_LIMIT:
        return ["-p", prompt, "--output-format", "json", *common], None
    line = json.dumps(
        {"event": "user", "message": {"content": prompt}}, ensure_ascii=False
    )
    return [
        "--input-format", "stream-json", "--output-format", "stream-json", *common
    ], line + "\n"


def _async_slot() -> asyncio.Semaphore:
    """The running loop's process gate (asyncio primitives are loop-bound)."""
    loop = asyncio.get_running_loop()
    gate = _async_gates.get(loop)
    if gate is None:
        cap = process_cap()
        logger.info("Antigravity transport capped at %d concurrent processes", cap)
        gate = _async_gates[loop] = asyncio.Semaphore(cap)
    return gate


async def arun_agy(
    args: list[str],
    *,
    timeout: float,
    cwd: Path | None = None,
    stdin_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """`run_agy` on the event loop; the process dies with its caller.

    agy runs in its own process group (session) so a timeout or a
    cancelled translation kills it and anything it spawned, instead of
    leaving a ~145MB agent finishing a turn nobody will read.
    """
    binary = agy_path()
    if binary is None:
        raise _missing_binary()
    if os.name == "nt":
        group: dict[str, Any] = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    else:
        group = {"start_new_session": True}
    try:
        proc = await asyncio.create_subprocess_exec(  # noqa: S603 - fixed binary
            str(binary),
            *args,
            stdin=subprocess.DEVNULL if stdin_text is None else subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(cwd) if cwd else None,
            **group,
        )
    except OSError as exc:
        raise AgyError(f"Antigravity CLI(agy) 실행에 실패했습니다: {exc}") from exc
    payload = None if stdin_text is None else stdin_text.encode("utf-8")
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(payload), timeout)
    except TimeoutError as exc:
        await _kill_group(proc)
        raise AgyError(
            f"Antigravity CLI(agy) 응답이 {timeout:.0f}초 안에 오지 않았습니다."
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
    # Reap it; SIGKILL cannot be ignored, so this does not hang.
    with contextlib.suppress(Exception):
        await proc.wait()


def _raise_if_recently_unauthenticated() -> None:
    """Fail the whole batch fast once we know the user is signed out.

    Measured: a signed-out `agy -p` prints an OAuth URL and waits 60
    seconds before giving up, even with stdin closed. At the pipeline's
    concurrency that is minutes of dead waiting per batch, all of it
    certain to fail, so the first refusal short-circuits the rest.

    Deliberately expires: the fix is for the user to run `agy` and sign
    in, and they should not then have to restart the engine.
    """
    with _auth_lock:
        failed_at = _auth_failed_at
    if failed_at is None:
        return
    if time.monotonic() - failed_at > _AUTH_FAIL_TTL_S:
        return
    raise AgyError(
        "Antigravity CLI(agy)에 로그인되어 있지 않습니다. 터미널에서 `agy`를 "
        "실행해 로그인한 뒤 다시 시도해 주세요."
    )


def _note_auth_failure(message: str) -> None:
    global _auth_failed_at
    if "로그인" not in message:
        return
    with _auth_lock:
        _auth_failed_at = time.monotonic()


def _clear_auth_failure() -> None:
    global _auth_failed_at
    with _auth_lock:
        _auth_failed_at = None


def _read_envelope(
    proc: subprocess.CompletedProcess[str], prompt: str
) -> tuple[str, dict[str, int]]:
    """The `--output-format json` envelope, strictly.

    Exit status is checked but never trusted alone: `agy` was observed
    returning rc=0 with `"status":"ERROR"` when unauthenticated, so the
    envelope's own status is authoritative.
    """
    raw = (proc.stdout or "").strip()
    if not raw:
        detail = (proc.stderr or "").strip()[:300]
        raise AgyError(_auth_hint(detail) or f"Antigravity CLI(agy)가 응답하지 않았습니다. {detail}")
    try:
        envelope = json.loads(raw.splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise AgyError(
            f"Antigravity CLI(agy) 응답을 해석할 수 없습니다: {raw[:300]}"
        ) from exc
    # stream-json output (the stdin path): the terminal `result` event
    # carries the same envelope as `--output-format json`.
    if isinstance(envelope, dict) and envelope.get("event") == "result":
        envelope = envelope.get("result")
    if not isinstance(envelope, dict):
        raise AgyError(f"Antigravity CLI(agy) 응답을 해석할 수 없습니다: {raw[:300]}")

    status = envelope.get("status")
    if status != "SUCCESS":
        reported = str(envelope.get("error") or status or "unknown")
        raise AgyError(_auth_hint(reported) or f"Antigravity CLI(agy) 요청이 실패했습니다: {reported[:300]}")

    structured = envelope.get("structured_output")
    if not isinstance(structured, dict) or not isinstance(structured.get("text"), str):
        # The schema was not honoured. Whatever `response` holds is
        # unvalidated free text and may be narration, so it is refused
        # rather than passed off as a translation.
        raise AgyError(
            "Antigravity CLI(agy)가 지정한 형식으로 답하지 않았습니다. "
            "번역 결과로 신뢰할 수 없어 요청을 실패로 처리합니다."
        )
    text = structured["text"]
    _assert_tokens_survived(prompt, text)

    usage = envelope.get("usage") or {}
    return text, {
        "prompt_tokens": int(usage.get("input_tokens") or 0),
        "completion_tokens": int(usage.get("output_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
    }


def _assert_tokens_survived(prompt: str, text: str) -> None:
    """Protected tokens are load-bearing; a dropped one corrupts the pack.

    Uses the engine's own TOKEN_RE so this cannot drift from what the
    placeholder layer protects. An agent that paraphrases rather than
    translating tends to drop these first, which makes them a cheap and
    specific signal that the output is not a faithful translation.
    """
    expected = set(TOKEN_RE.findall(prompt))
    if not expected:
        return
    missing = sorted(token for token in expected if token not in text)
    if missing:
        raise AgyError(
            "Antigravity CLI(agy) 응답에서 보호 토큰이 사라졌습니다: "
            + ", ".join(missing[:8])
        )


def _auth_hint(detail: str) -> str | None:
    """Turn agy's auth failure into something the user can act on."""
    lowered = detail.lower()
    if "authentication" in lowered or "sign in" in lowered or "log in" in lowered:
        return (
            "Antigravity CLI(agy)에 로그인되어 있지 않습니다. 터미널에서 `agy`를 "
            "실행해 로그인한 뒤 다시 시도해 주세요."
        )
    return None


# ---------------------------------------------------------------------------
# Verification — settles output fidelity on a machine that can sign in
# ---------------------------------------------------------------------------

#: Short, realistic probe: two protected tokens plus a Minecraft colour
#: code, which is exactly the shape an agent is most likely to "helpfully"
#: rewrite. Kept tiny so the check costs one cheap request.
VERIFY_SOURCE = "{{COLOR}}Ancient Debris{{PH1}} found at y=15"
VERIFY_SYSTEM = (
    "You translate Minecraft modpack text from English to Korean. "
    "Preserve every placeholder token exactly as given. "
    "Return only the translation."
)


def verify_report(model: str | None = None) -> tuple[bool, str]:
    """Run one real translation through `agy` and report, verbatim.

    Exists because output fidelity is the one axis that cannot be proven
    without a signed-in Google account: `agy` is an AGENT, and an agent may
    narrate, use tools, or wrap its answer in commentary. Narration
    silently pasted into a modpack is worse than a provider that plainly
    does not work, so this prints the RAW envelope rather than a summary
    and lets the reader judge.
    """
    lines: list[str] = []
    binary = agy_path()
    if binary is None:
        return False, (
            "agy not found. Install it first:\n"
            "  curl -fsSL https://antigravity.google/cli/install.sh | bash\n"
            "Docs: https://antigravity.google/docs/cli/install"
        )
    lines.append(f"binary: {binary}")
    version = run_agy(["--version"], timeout=30.0)
    lines.append(f"version: {(version.stdout or '').strip()}")

    slug = resolve_model_for_agy(model) if model else AGY_DEFAULT_MODEL
    lines.append(f"model: {slug}")
    lines.append(f"source: {VERIFY_SOURCE}")

    messages = [
        {"role": "system", "content": VERIFY_SYSTEM},
        {"role": "user", "content": VERIFY_SOURCE},
    ]
    prompt = build_prompt(messages)
    args = [
        "-p", prompt,
        "--output-format", "json",
        "--json-schema", json.dumps(_TEXT_SCHEMA),
        "--disable-slash-commands",
        "--print-timeout", f"{int(_PRINT_TIMEOUT_S)}s",
        "--model", slug,
    ]
    lines.append("")
    lines.append("command: agy " + " ".join(
        f"'{a}'" if " " in a or "\n" in a else a for a in args
    ))

    with tempfile.TemporaryDirectory(prefix="moru-agy-verify-") as workspace:
        proc = run_agy(args, timeout=_PRINT_TIMEOUT_S + _KILL_GRACE_S, cwd=Path(workspace))

    lines.append("")
    lines.append("--- RAW STDOUT ---")
    lines.append(proc.stdout or "(empty)")
    lines.append("--- RAW STDERR ---")
    lines.append(proc.stderr or "(empty)")
    lines.append(f"--- exit code: {proc.returncode} ---")
    lines.append("")

    try:
        text, usage = _read_envelope(proc, prompt)
    except AgyError as exc:
        lines.append(f"VERDICT: FAIL — {exc}")
        return False, "\n".join(lines)

    expected = sorted(set(TOKEN_RE.findall(VERIFY_SOURCE)))
    intact = [tok for tok in expected if tok in text]
    missing = [tok for tok in expected if tok not in text]

    lines.append(f"translation: {text!r}")
    lines.append(f"usage: {usage}")
    lines.append(f"tokens expected: {expected}")
    lines.append(f"tokens intact:   {intact}")
    lines.append(f"tokens MISSING:  {missing or 'none'}")
    lines.append("")
    # A bare translation should be one line and must not restate the
    # instructions or the delimiters we folded in.
    leaked = [
        marker for marker in (_SYSTEM_DELIM, _INPUT_DELIM) if marker in text
    ]
    narration_suspect = bool(leaked) or text.count("\n") > 1
    if missing:
        lines.append("VERDICT: FAIL — protected tokens did not survive.")
        return False, "\n".join(lines)
    if narration_suspect:
        lines.append(
            "VERDICT: SUSPECT — tokens survived, but the output is multi-line "
            f"or echoes our delimiters {leaked}. Read the raw envelope above: "
            "if it contains commentary as well as the translation, the "
            "transport is not safe for bulk translation."
        )
        return False, "\n".join(lines)
    lines.append(
        "VERDICT: PASS — the response is the bare translation, arrived as "
        "schema-validated structured_output, and every protected token "
        "survived byte-for-byte."
    )
    return True, "\n".join(lines)
