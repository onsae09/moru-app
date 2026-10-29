"""Local coding-CLI providers: Claude Code, OpenAI Codex, Gemini CLI.

Each is registered as a LiteLLM custom provider, so the whole engine
addresses them with the same model-string contract as every hosted
provider (``claude-code/claude-sonnet-4-6``, ``codex/gpt-5.6-terra``,
``gemini-cli/gemini-3.5-flash``) and ``build_lm`` needs no special case.

Auth is the CLI's own OAuth grant read off disk — no API key, no login
flow of our own. See ``credentials`` for the stores.
"""

from __future__ import annotations

import logging
from typing import Any

import litellm
from litellm.utils import custom_llm_setup

from .antigravity import AGY_DEFAULT_MODEL, AGY_FALLBACK_MODELS, list_models
from .claude_code import ClaudeCodeLLM
from .codex import CodexLLM
from .credentials import (
    GEMINI_CLI_STORE,
    REASON_UNKNOWN_PROVIDER,
    STATE_CLI_MISSING,
    STORES,
    TRANSPORT_AGY_CLI,
    CliAuthError,
)
from .gemini_cli import GeminiCliLLM
from .wire import WIRE_MARKER

logger = logging.getLogger(__name__)

__all__ = [
    "AGY_DEFAULT_MODEL",
    "AGY_FALLBACK_MODELS",
    "CLI_PROVIDER_CATALOG",
    "CLI_PROVIDER_IDS",
    "CliAuthError",
    "is_disabled_model",
    "provider_models",
    "provider_status",
    "register_cli_providers",
    "to_wire_model",
]

#: Catalog entries in the same shape as the engine's hosted-provider table.
#: ``env`` is None: these never take an API key, and the login command is
#: the store's to report — it depends on which CLI the machine has.
#:
#: ``name`` is an English-neutral product name on purpose. The engine does
#: not own presentation copy: the desktop ships English and Korean and
#: localizes these labels renderer-side, keyed off ``id``, treating this
#: field as the fallback for ids it does not recognise. A Korean-only
#: string here showed "(구독)" to English users; a bare product name is
#: correct in both until the renderer substitutes its own.
CLI_PROVIDER_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "id": "claude-code",
        "name": "Claude Code",
        "env": None,
        "auth": "cli",
        # Switched off: the handler's OAuth path is being replaced. Listed
        # so a saved selection still has a card to explain itself, but no
        # probe or completion is sent (see ClaudeCodeLLM).
        "disabled": True,
        "models": [
            "claude-code/claude-sonnet-4-6",
            "claude-code/claude-haiku-4-5",
            "claude-code/claude-opus-4-8",
        ],
    },
    {
        "id": "codex",
        "name": "OpenAI Codex",
        "env": None,
        "auth": "cli",
        # Static fallback only — POST /providers/models asks the backend,
        # which is the sole authority on what a given plan may call.
        "models": [
            "codex/gpt-5.6-luna",
            "codex/gpt-5.6-terra",
            "codex/gpt-5.6-sol",
        ],
    },
    {
        # Two CLIs, one id. `agy` (Antigravity) superseded `gemini` but did
        # not retire it, and the id stays `gemini-cli` so every saved model
        # string and provider selection keeps resolving. Renaming it would
        # have meant migrating stored settings for no user-visible gain.
        #
        # These static ids are the LEGACY namespace, correct for the
        # cloudcode-pa transport and verified as literal constants in the
        # installed gemini-cli 0.57.0 bundle. Antigravity's own slugs carry
        # a reasoning-effort suffix and differ entirely; on an `agy`
        # machine POST /providers/models serves those instead, live from
        # `agy models` with a documented fallback.
        "id": "gemini-cli",
        "name": "Gemini CLI / Antigravity",
        "env": None,
        "auth": "cli",
        "models": [
            "gemini-cli/gemini-3.5-flash",
            "gemini-cli/gemini-3.1-pro-preview",
            "gemini-cli/gemini-3.1-flash-lite",
        ],
    },
)


CLI_PROVIDER_IDS: frozenset[str] = frozenset(p["id"] for p in CLI_PROVIDER_CATALOG)


def is_disabled_model(model: str) -> bool:
    """Whether ``model`` routes to a catalog provider switched off here."""
    prefix = model.split("/", 1)[0]
    return any(p["id"] == prefix and p.get("disabled") for p in CLI_PROVIDER_CATALOG)


def to_wire_model(model: str) -> str:
    """Public CLI model id -> the form LiteLLM routes to our handler.

    ``codex/gpt-5.6-luna`` -> ``codex/@/gpt-5.6-luna``. See ``wire`` for why
    the marker is needed; non-CLI models pass through untouched so this is
    safe to call on every model string.
    """
    provider, sep, rest = model.partition("/")
    if not sep or provider not in CLI_PROVIDER_IDS:
        return model
    if rest.startswith(f"{WIRE_MARKER}/"):
        return model
    return f"{provider}/{WIRE_MARKER}/{rest}"


_HANDLERS = {
    "claude-code": ClaudeCodeLLM,
    "codex": CodexLLM,
    "gemini-cli": GeminiCliLLM,
}

_registered = False


def register_cli_providers() -> None:
    """Install the handlers into LiteLLM once, for sync AND async calls.

    Filling ``custom_provider_map`` alone is not enough. LiteLLM only folds
    that map into ``provider_list`` inside ``custom_llm_setup()``, which its
    *sync* wrapper calls — ``acompletion`` resolves the provider before any
    of that runs. An unregistered prefix then falls through to LiteLLM's
    name heuristics, so ``codex/gpt-5.6-luna`` was routed to OpenAI and
    failed asking for OPENAI_API_KEY. Running the setup here means the very
    first call resolves correctly whichever path it takes.
    """
    global _registered
    if _registered:
        return
    existing = {entry.get("provider") for entry in litellm.custom_provider_map}
    for provider, handler in _HANDLERS.items():
        if provider in existing:
            continue
        litellm.custom_provider_map = litellm.custom_provider_map + [
            {"provider": provider, "custom_handler": handler()}
        ]
    custom_llm_setup()
    _registered = True
    logger.debug("Registered CLI providers: %s", ", ".join(_HANDLERS))


def provider_status(provider_id: str) -> dict[str, Any]:
    """Auth status for one CLI provider.

    Carries the login command as ``login_hint``. That belongs here, next
    to the probe that knows which CLI is actually installed, because it
    differs per CLI and — for this provider — per machine: `claude login`
    and `codex login` are subcommands, while Antigravity has no login
    subcommand at all (verified: none exists in the binary) and is
    authenticated by running bare `agy`. A renderer hardcoding any of
    that would be wrong somewhere.
    """
    store = STORES.get(provider_id)
    if store is None:
        return {
            "connected": False,
            "error": f"unknown provider: {provider_id}",
            "error_code": REASON_UNKNOWN_PROVIDER,
            "state": STATE_CLI_MISSING,
        }
    status = store.status()
    status["login_hint"] = store.login_hint
    status["path"] = str(store.path)
    return status


def provider_models(provider_id: str) -> list[str]:
    """Model list for a CLI provider, wire-prefixed for the catalog.

    Claude Code has a fixed lineup, so the catalog is the source of truth.
    Codex is served live elsewhere (its backend gates SKUs per plan).
    Antigravity does publish a lineup — `agy models` — and it is per
    account, so on an `agy` machine that is asked instead of trusting a
    static list whose slugs the CLI would reject outright.
    """
    if provider_id == "gemini-cli":
        return _gemini_cli_models()
    for entry in CLI_PROVIDER_CATALOG:
        if entry["id"] == provider_id:
            return list(entry["models"])
    return []


def _gemini_cli_models() -> list[str]:
    """Whichever namespace this machine's CLI actually accepts.

    The two transports do not share model ids: `agy` slugs are
    effort-suffixed (`gemini-3.7-flash-medium`) and the legacy Gemini API
    ids are not (`gemini-3.5-flash`). Serving the wrong namespace hands
    the user a list where every entry fails — and on `agy` it fails
    loudly, since headless mode exits non-zero on an unknown `--model`
    rather than falling back.
    """
    static = next(
        (entry["models"] for entry in CLI_PROVIDER_CATALOG if entry["id"] == "gemini-cli"),
        [],
    )
    if GEMINI_CLI_STORE.transport != TRANSPORT_AGY_CLI:
        return list(static)
    return [f"gemini-cli/{slug}" for slug in list_models()]
