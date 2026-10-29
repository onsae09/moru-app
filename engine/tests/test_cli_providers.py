"""Coding-CLI provider tests.

The wire details here are ported from oh-my-pi, so the assertions pin the
behaviour that a real Claude Code / Codex / Gemini CLI client produces —
notably the ``cch`` attestation, whose golden vector was cross-checked
against the original Bun implementation (``Bun.hash.xxHash64``).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import httpx
import pytest
from litellm.types.utils import ModelResponse

from moru_engine.cli_providers import (
    antigravity,
    claude_code,
    codex,
    credentials,
    gemini_cli,
    provider_status,
)

# --------------------------------------------------------------------------
# Claude Code — billing header + cch attestation
# --------------------------------------------------------------------------

FIRST_USER = "Translate these Minecraft modpack strings into Korean."

#: Produced by the oh-my-pi TypeScript implementation for the payload built
#: in `_golden_payload`. Any drift here means the port diverged.
GOLDEN_BILLING = (
    "x-anthropic-billing-header: cc_version=2.1.220.3a0; "
    "cc_entrypoint=claude-desktop; cch=00000;"
)
GOLDEN_CCH = "ad4c2"


def _golden_payload() -> dict[str, object]:
    return {
        "model": "claude-sonnet-4-6",
        "max_tokens": 8192,
        "system": [
            {"type": "text", "text": claude_code.create_billing_header(FIRST_USER)},
            {"type": "text", "text": claude_code.CLAUDE_CODE_SYSTEM_INSTRUCTION},
            {"type": "text", "text": "너는 마인크래프트 번역가다."},
        ],
        "messages": [{"role": "user", "content": FIRST_USER}],
        "stream": False,
    }


def test_billing_header_matches_claude_code_fingerprint() -> None:
    assert claude_code.create_billing_header(FIRST_USER) == GOLDEN_BILLING


def test_billing_header_pads_short_messages() -> None:
    # Fingerprint reads msg[4], msg[7], msg[20]; missing chars become "0".
    short = claude_code.create_billing_header("hi")
    assert short.startswith("x-anthropic-billing-header: cc_version=2.1.220.")
    assert short != GOLDEN_BILLING


def test_cch_attestation_matches_bun_reference() -> None:
    body = bytearray(
        json.dumps(_golden_payload(), ensure_ascii=False, separators=(",", ":")).encode()
    )
    assert claude_code.patch_cch(body) == "patched"
    assert f"cch={GOLDEN_CCH}".encode() in bytes(body)
    # The hash covers the body with the placeholder still in place, so the
    # patched bytes must differ from a re-hash of themselves.
    assert b"cch=00000" not in bytes(body)


def test_cch_skips_bodies_without_a_billing_header() -> None:
    body = bytearray(b'{"system":[{"type":"text","text":"plain"}]}')
    assert claude_code.patch_cch(body) == "no-billing-header"


def test_cch_reports_unanchored_placeholder() -> None:
    # Placeholder present but pushed beyond the search window after the marker.
    filler = "x" * 300
    body = bytearray(
        f'{{"system":[{{"type":"text","text":"x-anthropic-billing-header:{filler}cch=00000"}}]}}'.encode()
    )
    assert claude_code.patch_cch(body) == "unanchored"


def test_serialize_is_compact_so_the_marker_anchors() -> None:
    raw = claude_code.serialize(_golden_payload())
    # Pretty-printed JSON would break the byte marker the attestation needs.
    assert b'"system":[{"type":"text","text":"x-anthropic-billing-header:' in raw
    assert f"cch={GOLDEN_CCH}".encode() in raw


# --------------------------------------------------------------------------
# Claude Code — payload shape
# --------------------------------------------------------------------------


def test_claude_payload_puts_identity_block_before_user_system_prompts() -> None:
    payload = claude_code.build_payload(
        "sonnet",
        [
            {"role": "system", "content": "번역가 지침"},
            {"role": "user", "content": "hello"},
        ],
        {"max_tokens": 1024, "temperature": 0.3},
    )
    texts = [block["text"] for block in payload["system"]]
    assert texts[0].startswith("x-anthropic-billing-header:")
    assert texts[1] == claude_code.CLAUDE_CODE_SYSTEM_INSTRUCTION
    assert texts[2] == "번역가 지침"
    assert payload["model"] == "claude-sonnet-4-6"  # alias resolved
    assert payload["messages"] == [{"role": "user", "content": "hello"}]
    assert payload["temperature"] == 0.3


def test_claude_payload_opens_on_a_user_turn() -> None:
    payload = claude_code.build_payload(
        "claude-haiku-4-5", [{"role": "assistant", "content": "prior"}], {}
    )
    assert payload["messages"][0]["role"] == "user"


def test_claude_thinking_drops_sampling_and_reserves_budget() -> None:
    payload = claude_code.build_payload(
        "sonnet",
        [{"role": "user", "content": "hi"}],
        {"max_tokens": 2048, "temperature": 0.7, "reasoning_effort": "medium"},
    )
    assert payload["thinking"] == {"type": "enabled", "budget_tokens": 8192}
    # Anthropic rejects temperature alongside extended thinking.
    assert "temperature" not in payload
    assert payload["max_tokens"] > payload["thinking"]["budget_tokens"]


def test_claude_headers_carry_the_oauth_fingerprint() -> None:
    headers = claude_code.build_headers("tok-123")
    assert headers["Authorization"] == "Bearer tok-123"
    assert headers["User-Agent"] == claude_code.COWORK_USER_AGENT
    assert headers["x-app"] == "cli"
    assert headers["anthropic-version"] == "2023-06-01"
    assert "structured-outputs-2025-12-15" in headers["anthropic-beta"]


# --------------------------------------------------------------------------
# Codex
# --------------------------------------------------------------------------


def test_codex_payload_omits_every_sampling_parameter() -> None:
    payload = codex.build_payload(
        "gpt-5.6-terra",
        [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
        ],
        {"temperature": 0.3, "max_tokens": 4096, "top_p": 0.9},
    )
    # The ChatGPT backend 400s on any of these.
    for forbidden in ("temperature", "top_p", "max_output_tokens", "max_completion_tokens"):
        assert forbidden not in payload
    assert payload["store"] is False
    assert payload["stream"] is True
    assert payload["instructions"] == "sys"
    assert payload["input"][0]["content"][0]["type"] == "input_text"


def test_codex_defaults_to_low_reasoning_and_honors_overrides() -> None:
    base = codex.build_payload("gpt-5.6-terra", [{"role": "user", "content": "x"}], {})
    assert base["reasoning"] == {"effort": "low"}
    high = codex.build_payload(
        "gpt-5.6-terra", [{"role": "user", "content": "x"}], {"reasoning_effort": "high"}
    )
    assert high["reasoning"] == {"effort": "high"}


def test_codex_extra_system_prompts_become_developer_items() -> None:
    payload = codex.build_payload(
        "default",
        [
            {"role": "system", "content": "first"},
            {"role": "system", "content": "second"},
            {"role": "user", "content": "hi"},
        ],
        {},
    )
    assert payload["instructions"] == "first"
    assert payload["input"][0]["role"] == "developer"
    assert payload["input"][0]["content"][0]["text"] == "second"
    assert payload["model"] == "gpt-5.6-terra"  # alias resolved


def test_codex_aliases_resolve_to_real_gpt56_skus() -> None:
    """The GPT-5.6 Codex lineup is luna/terra/sol.

    Shipping an invented slug ("gpt-5.6-codex-mini") made the backend 400
    every chunk with "model is not supported"; pin the real ids.
    """
    assert {codex.resolve_model(a) for a in ("default", "fast", "balanced", "best")} == {
        "gpt-5.6-luna",
        "gpt-5.6-terra",
        "gpt-5.6-sol",
    }
    # An unknown alias must pass through untouched, never silently remap.
    assert codex.resolve_model("gpt-5.1-codex") == "gpt-5.1-codex"


def test_codex_discovery_orders_by_priority_and_drops_hidden() -> None:
    models = codex._normalize_models(
        {
            "models": [
                {"slug": "gpt-5.6-sol", "priority": 3},
                {"slug": "gpt-5.6-luna", "priority": 1},
                {"slug": "internal-preview", "priority": 0, "visibility": "hidden"},
                {"id": "gpt-5.6-terra", "priority": 2},
                {"display_name": "no slug at all"},
            ]
        }
    )
    assert models == [
        "codex/gpt-5.6-luna",
        "codex/gpt-5.6-terra",
        "codex/gpt-5.6-sol",
    ]


def test_codex_discovery_accepts_the_data_envelope() -> None:
    assert codex._normalize_models({"data": [{"slug": "gpt-5.6-luna"}]}) == [
        "codex/gpt-5.6-luna"
    ]
    assert codex._normalize_models({}) == []
    assert codex._normalize_models("nope") == []


def test_codex_unsupported_model_error_names_the_fix() -> None:
    body = (
        '{"detail":"The \'gpt-5.6-codex-mini\' model is not supported when '
        'using Codex with a ChatGPT account."}'
    )
    with pytest.raises(RuntimeError) as excinfo:
        codex._check_status(400, body)
    message = str(excinfo.value)
    assert "모델 목록을 새로고침" in message
    assert not isinstance(excinfo.value, credentials.CliAuthError)


def test_codex_stream_state_collects_text_and_usage() -> None:
    state = codex._StreamState()
    codex.parse_sse(
        [
            'data: {"type":"response.output_text.delta","delta":"안녕"}',
            'data: {"type":"response.output_text.delta","delta":"하세요"}',
            'data: {"type":"response.completed","response":{"usage":'
            '{"input_tokens":120,"output_tokens":8,"input_tokens_details":{"cached_tokens":100}}}}',
            "data: [DONE]",
        ],
        state,
    )
    assert "".join(state.text) == "안녕하세요"
    assert state.usage["input_tokens"] == 120


def test_codex_falls_back_to_final_output_when_no_deltas_streamed() -> None:
    state = codex._StreamState()
    codex.parse_sse(
        [
            'data: {"type":"response.completed","response":{"output":'
            '[{"type":"message","content":[{"type":"output_text","text":"done"}]}],"usage":{}}}'
        ],
        state,
    )
    assert "".join(state.text) == "done"


def test_codex_surfaces_stream_errors() -> None:
    state = codex._StreamState()
    codex.parse_sse(['data: {"type":"response.failed","error":{"message":"quota"}}'], state)
    assert state.error == "quota"


# --------------------------------------------------------------------------
# Gemini CLI
# --------------------------------------------------------------------------


def test_gemini_payload_wraps_in_the_code_assist_envelope() -> None:
    payload = gemini_cli.build_payload(
        "flash",
        [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "prior"},
        ],
        {"temperature": 0.3, "max_tokens": 2048},
        "my-project",
    )
    assert payload["project"] == "my-project"
    assert payload["model"] == "gemini-3.5-flash"  # alias resolved
    request = payload["request"]
    assert request["systemInstruction"] == {"parts": [{"text": "sys"}]}
    assert request["contents"][0] == {"role": "user", "parts": [{"text": "hi"}]}
    # OpenAI's "assistant" is Gemini's "model".
    assert request["contents"][1]["role"] == "model"
    assert request["generationConfig"]["maxOutputTokens"] == 2048


def test_gemini_maps_json_response_format_to_mime_type() -> None:
    payload = gemini_cli.build_payload(
        "flash",
        [{"role": "user", "content": "hi"}],
        {"response_format": {"type": "json_object"}},
        "p",
    )
    assert payload["request"]["generationConfig"]["responseMimeType"] == "application/json"


def test_gemini_stream_state_skips_thought_parts() -> None:
    state = gemini_cli._StreamState()
    gemini_cli.parse_sse(
        [
            'data: {"response":{"candidates":[{"content":{"parts":['
            '{"text":"reasoning","thought":true},{"text":"answer"}]}}]}}',
            'data: {"response":{"candidates":[{"finishReason":"STOP"}],'
            '"usageMetadata":{"promptTokenCount":50,"candidatesTokenCount":3,'
            '"cachedContentTokenCount":10,"totalTokenCount":53}}}',
        ],
        state,
    )
    assert "".join(state.text) == "answer"
    assert state.finish == "stop"
    assert state.usage["promptTokenCount"] == 50


def test_gemini_cli_headers_identify_as_the_real_cli() -> None:
    headers = gemini_cli.gemini_cli_headers("gemini-3.5-flash")
    assert headers["User-Agent"].startswith("GeminiCLI/")
    assert "gemini-3.5-flash" in headers["User-Agent"]
    assert headers["Client-Metadata"].startswith("ideType=")


# --------------------------------------------------------------------------
# Credential stores
# --------------------------------------------------------------------------


@pytest.fixture
def claude_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    # Never read the developer's real macOS login keychain.
    monkeypatch.setattr(credentials.ClaudeCodeStore, "_keychain_read", lambda self: None)
    monkeypatch.setattr(credentials.ClaudeCodeStore, "_keychain_write", lambda self, doc: False)
    return tmp_path


def test_claude_store_reports_disconnected_without_a_file(claude_home) -> None:
    store = credentials.ClaudeCodeStore()
    assert store.available() is False
    assert store.status()["connected"] is False


def test_claude_store_ignores_a_logged_out_credential(claude_home) -> None:
    (claude_home / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "", "refreshToken": "", "expiresAt": 0}})
    )
    assert credentials.ClaudeCodeStore().available() is False


def test_claude_store_refreshes_and_writes_back_preserving_other_keys(
    claude_home, monkeypatch
) -> None:
    path = claude_home / ".credentials.json"
    path.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "old-access",
                    "refreshToken": "old-refresh",
                    "expiresAt": 1,  # long expired
                    "subscriptionType": "max",
                },
                # Unrelated block the CLI owns; a write must not drop it.
                "mcpOAuth": {"figma": {"accessToken": "keep-me"}},
            }
        )
    )
    store = credentials.ClaudeCodeStore()
    monkeypatch.setattr(
        store,
        "_post_json",
        lambda *a, **kw: {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_in": 3600,
            "account": {"uuid": "acc-1", "email_address": "me@example.com"},
        },
    )

    creds = store.credentials()
    assert creds.access == "new-access"
    assert creds.email == "me@example.com"

    written = json.loads(path.read_text())
    assert written["claudeAiOauth"]["accessToken"] == "new-access"
    assert written["claudeAiOauth"]["refreshToken"] == "new-refresh"
    # Rotation must not clobber the CLI's own unrelated state.
    assert written["mcpOAuth"]["figma"]["accessToken"] == "keep-me"
    assert written["claudeAiOauth"]["subscriptionType"] == "max"


def test_claude_store_keeps_a_live_token_untouched(claude_home, monkeypatch) -> None:
    path = claude_home / ".credentials.json"
    path.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "live",
                    "refreshToken": "r",
                    "expiresAt": credentials._now_ms() + 60 * 60 * 1000,
                }
            }
        )
    )
    store = credentials.ClaudeCodeStore()

    def _boom(*a, **kw):
        raise AssertionError("must not refresh a live token")

    monkeypatch.setattr(store, "_post_json", _boom)
    assert store.token() == "live"


def test_credentials_are_stale_inside_the_refresh_skew() -> None:
    inside = credentials.OAuthCredentials(
        access="a", refresh="r", expires=credentials._now_ms() + 60_000
    )
    assert inside.stale() is True
    outside = credentials.OAuthCredentials(
        access="a", refresh="r", expires=credentials._now_ms() + 30 * 60_000
    )
    assert outside.stale() is False
    # Unknown expiry must not trigger a refresh on every call.
    assert credentials.OAuthCredentials(access="a", refresh="r", expires=0).stale() is False


def test_codex_store_reads_tokens_and_jwt_claims(tmp_path, monkeypatch) -> None:
    import base64

    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    claims = {
        "exp": 2_000_000_000,
        "https://api.openai.com/auth": {"chatgpt_account_id": "acct-9"},
        "https://api.openai.com/profile": {"email": "me@example.com"},
    }
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    (tmp_path / "auth.json").write_text(
        json.dumps(
            {
                "OPENAI_API_KEY": None,
                "auth_mode": "chatgpt",
                "tokens": {"access_token": f"h.{body}.s", "refresh_token": "r"},
            }
        )
    )
    creds = credentials.CodexStore().credentials()
    assert creds.account_id == "acct-9"
    assert creds.email == "me@example.com"
    assert creds.expires == 2_000_000_000 * 1000


def test_missing_credentials_raise_an_actionable_error(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    with pytest.raises(credentials.CliAuthError) as excinfo:
        credentials.CodexStore().credentials()
    assert "codex login" in str(excinfo.value)


# --------------------------------------------------------------------------
# Gemini CLI / Antigravity CLI — install detection and project resolution
# --------------------------------------------------------------------------


@pytest.fixture
def gemini_home(tmp_path, monkeypatch):
    """An isolated config root on a machine with neither CLI installed.

    Pinned with GEMINI_CLI_HOME, the legacy CLI's real relocation switch.
    This fixture used to pin with GEMINI_CONFIG_DIR, which is honoured by
    neither CLI — its only occurrences in gemini-cli are assignments in
    that project's own test harness, never read by production code — so
    the suite was exercising a mechanism no user has.
    """
    monkeypatch.setenv("GEMINI_CLI_HOME", str(tmp_path))
    for name in ("GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_PROJECT_ID", "GEMINI_CONFIG_DIR"):
        monkeypatch.delenv(name, raising=False)
    # ~/.env is a real avenue in the resolution chain: keep the host's out.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    # The binary probe walks absolute install paths that a dev machine may
    # genuinely have; pin it so these assertions are about moru, not the host.
    monkeypatch.setattr(credentials, "_agy_installed", lambda: False)
    return tmp_path


def _write_grant(directory, **extra):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "oauth_creds.json").write_text(
        json.dumps(
            {
                "access_token": "live-token",
                "refresh_token": "r",
                # Far future: never triggers a refresh mid-test.
                "expiry_date": credentials._now_ms() + 24 * 60 * 60 * 1000,
                **extra,
            }
        )
    )


def _response(status: int, payload: object) -> httpx.Response:
    return httpx.Response(status, json=payload)


class _Recorder:
    """Stands in for the store's single HTTP seam."""

    def __init__(self, *responses: httpx.Response) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict]] = []

    def __call__(self, method, url, *, headers=None, form=None, json_body=None):
        self.calls.append((method, url, json_body or {}))
        if not self.responses:
            raise AssertionError(f"unexpected extra request: {method} {url}")
        return self.responses.pop(0)


def test_gemini_store_reads_the_legacy_gemini_layout(gemini_home) -> None:
    _write_grant(gemini_home)
    store = credentials.GeminiCliStore()
    assert store.antigravity() is False
    assert store.login_hint == "gemini"
    assert store.path == gemini_home / "oauth_creds.json"
    assert store.available() is True


def test_gemini_store_prefers_the_antigravity_layout(gemini_home) -> None:
    """A migrated machine keeps ~/.gemini but nests agy's config inside it."""
    _write_grant(gemini_home)
    _write_grant(gemini_home / "antigravity-cli")
    store = credentials.GeminiCliStore()
    assert store.antigravity() is True
    assert store.login_hint == "agy"
    assert store.path == gemini_home / "antigravity-cli" / "oauth_creds.json"


def test_gemini_store_follows_an_installed_agy_with_no_config_dir_yet(
    gemini_home, monkeypatch
) -> None:
    """agy keeps the session in the OS keyring, so the file can be absent."""
    monkeypatch.setattr(credentials, "_agy_installed", lambda: True)
    store = credentials.GeminiCliStore()
    assert store.antigravity() is True
    assert store.login_hint == "agy"
    # Nothing on disk yet: the reported path is the migrated CLI's.
    assert store.path == gemini_home / "antigravity-cli" / "oauth_creds.json"


def test_gemini_store_keeps_the_legacy_grant_readable_after_migration(
    gemini_home, monkeypatch
) -> None:
    """agy installed, but only the old file has a grant — it must still load."""
    monkeypatch.setattr(credentials, "_agy_installed", lambda: True)
    _write_grant(gemini_home)
    store = credentials.GeminiCliStore()
    assert store.antigravity() is True
    assert store.path == gemini_home / "oauth_creds.json"
    assert store.credentials().access == "live-token"


def test_gemini_store_ignores_a_workspace_style_antigravity_directory(
    gemini_home,
) -> None:
    """`~/.gemini/antigravity/` is NOT a global config root.

    It does exist, but only workspace-relative, holding artifacts and
    transcript.jsonl. The shipped agy binary's only `.gemini/antigravity/`
    strings are of that form; its global root is `antigravity-cli`, with
    the suffix. Treating the unsuffixed one as a credential home made any
    home directory that happened to also be a workspace look like a
    migrated Antigravity install.
    """
    _write_grant(gemini_home / "antigravity")
    store = credentials.GeminiCliStore()
    assert store.antigravity() is False
    # The legacy location is still what a request would read.
    assert store.path == gemini_home / "oauth_creds.json"


def test_agy_is_found_off_path_at_its_documented_install_location(
    tmp_path, monkeypatch
) -> None:
    """The installer writes ~/.local/bin/agy, which a sidecar's PATH omits."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(credentials.shutil, "which", lambda _cmd: None)
    assert credentials._agy_installed() is False

    binary = tmp_path / ".local" / "bin" / "agy"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    assert credentials._agy_installed() is True


# --------------------------------------------------------------------------
# GEMINI_CLI_HOME — the CLI contradicts itself, so both readings are probed
# --------------------------------------------------------------------------


def test_gemini_home_follows_the_nested_reading_when_that_is_where_config_is(
    tmp_path, monkeypatch
) -> None:
    """`homedir()` treats GEMINI_CLI_HOME as a home base, config below it.

    The shipped bundle has both readings. Under this one the credential
    lives at `$GEMINI_CLI_HOME/.gemini/`, and hardcoding the other
    interpretation stranded these users with an empty probe one level up.
    """
    monkeypatch.setenv("GEMINI_CLI_HOME", str(tmp_path))
    monkeypatch.setattr(credentials, "_agy_installed", lambda: False)
    _write_grant(tmp_path / ".gemini")
    store = credentials.GeminiCliStore()
    assert store.home == tmp_path / ".gemini"
    assert store.config_dir_source == store.HOME_ENV_NESTED
    assert store.available() is True


def test_gemini_home_prefers_the_direct_reading_when_both_hold_a_grant(
    tmp_path, monkeypatch
) -> None:
    """The documented tiebreak, and it must be observable.

    A migration leaving a stale `$GEMINI_CLI_HOME/.gemini` beside a live
    `$GEMINI_CLI_HOME` is an ordinary state. "Whichever we checked first"
    is how a provider works on one machine and not another, so the rule is
    fixed — the direct reading wins, because that is what moru already
    shipped and breaking the tie this way cannot regress a working user —
    and the winner is reported so it can be debugged.
    """
    monkeypatch.setenv("GEMINI_CLI_HOME", str(tmp_path))
    monkeypatch.setattr(credentials, "_agy_installed", lambda: False)
    _write_grant(tmp_path)
    _write_grant(tmp_path / ".gemini")
    store = credentials.GeminiCliStore()
    assert store.home == tmp_path
    assert store.config_dir_source == store.HOME_ENV_DIRECT
    assert store.status()["config_dir"] == str(tmp_path)
    assert store.status()["config_dir_source"] == store.HOME_ENV_DIRECT


def test_gemini_config_dir_env_var_is_not_honoured(tmp_path, monkeypatch) -> None:
    """GEMINI_CONFIG_DIR is read by neither CLI, so moru must not either.

    Honouring a variable no CLI reads is worse than ignoring it: a user who
    sets it believes their config moved while every probe looks elsewhere.
    """
    monkeypatch.delenv("GEMINI_CLI_HOME", raising=False)
    monkeypatch.setenv("GEMINI_CONFIG_DIR", str(tmp_path / "ignored"))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(credentials, "_agy_installed", lambda: False)
    store = credentials.GeminiCliStore()
    assert store.home == tmp_path / ".gemini"
    assert store.config_dir_source == store.HOME_DEFAULT


# --------------------------------------------------------------------------
# Auth states and transport precedence
# --------------------------------------------------------------------------


def test_status_separates_a_missing_cli_from_a_logged_out_one(monkeypatch) -> None:
    """The distinction the UI needs: install the CLI vs. log in.

    Without it both look like `connected: false` and the app can only
    offer one generic message.
    """
    store = credentials.CodexStore()
    monkeypatch.setattr(store, "_load", lambda: None)

    monkeypatch.setattr(credentials.shutil, "which", lambda _cmd: None)
    absent = store.status()
    assert absent["state"] == credentials.STATE_CLI_MISSING
    assert absent["cli_installed"] is False
    assert absent["cli"] == "codex"

    monkeypatch.setattr(credentials.shutil, "which", lambda _cmd: "/usr/bin/codex")
    present = store.status()
    assert present["state"] == credentials.STATE_LOGGED_OUT
    assert present["cli_installed"] is True


def test_antigravity_status_never_claims_logged_out_from_a_missing_file(
    gemini_home, monkeypatch
) -> None:
    """agy keeps its session in the OS keyring, so a file proves nothing.

    `oauth_creds` appears zero times in the shipped binary. Reporting
    `logged-out` because no JSON exists would call a signed-in user signed
    out — the defect this whole reporting change exists to end.
    """
    monkeypatch.setattr(credentials, "_agy_installed", lambda: True)
    store = credentials.GeminiCliStore()
    status = store.status()
    assert status["antigravity"] is True
    assert status["transport"] == credentials.TRANSPORT_AGY_CLI
    assert status["state"] == credentials.STATE_CLI_READY
    assert status["state"] != credentials.STATE_LOGGED_OUT
    assert status["credentials_in_keyring"] is True
    assert status["cli"] == "agy"


def test_antigravity_status_never_resolves_a_cloud_project(
    gemini_home, monkeypatch
) -> None:
    """Finding 3, made a test: no Code Assist onboarding on the agy path.

    The agy binary contains no GOOGLE_CLOUD_PROJECT string; it bills
    against a Google AI plan. Demanding a project these users never needed,
    and leaking a Code Assist traceback when it could not be resolved, is
    the original reported failure. A request here must never touch the
    HTTP seam at all.
    """
    monkeypatch.setattr(credentials, "_agy_installed", lambda: True)

    def _explode(*_a, **_k):
        raise AssertionError("agy path must not call Cloud Code Assist")

    store = credentials.GeminiCliStore()
    monkeypatch.setattr(store, "_send", _explode)
    # Status must stay quiet, and project() must refuse rather than onboard.
    assert store.status()["project"] is None
    with pytest.raises(credentials.CliAuthError):
        store.project()


def test_transport_precedence_prefers_a_readable_legacy_grant(
    gemini_home, monkeypatch
) -> None:
    """Both CLIs present: the file-backed path wins, deterministically.

    agy stores its session in the keyring, so an oauth_creds.json on disk
    was written by the legacy CLI — and borrowing that token over HTTP is
    the path we can verify end to end.
    """
    monkeypatch.setattr(credentials, "_agy_installed", lambda: True)
    store = credentials.GeminiCliStore()
    assert store.transport == credentials.TRANSPORT_AGY_CLI

    _write_grant(gemini_home)
    assert store.transport == credentials.TRANSPORT_LEGACY_HTTP


def test_project_error_tells_the_user_what_to_do_and_no_dead_link() -> None:
    """The remediation text is the whole point of reaching this error.

    gemini-cli prints goo.gle/gemini-cli-auth-docs, which now redirects to
    a 404 whose `#workspace-gca` anchor no longer exists (verified with
    curl). Copying an upstream broken link into our own UI is not a fix.
    """
    message = credentials._NEEDS_PROJECT_ENV
    assert "GOOGLE_CLOUD_PROJECT" in message
    assert "goo.gle/gemini-cli-auth-docs" not in message
    assert "geminicli.com/docs/get-started/authentication/#set-gcp" in message


# --------------------------------------------------------------------------
# `agy models` — decorated text is the only format on offer
# --------------------------------------------------------------------------


def test_agy_models_parses_the_documented_two_column_output() -> None:
    """Real shape, from the official sample. Slugs carry an effort suffix."""
    slugs = antigravity.parse_models_output(
        "Fetching available models...\n"
        "gemini-3.7-flash-high     Gemini 3.7 Flash (High)\n"
        "gemini-3.7-flash-medium   Gemini 3.7 Flash (Medium)\n"
        "gemini-3.1-pro-high       Gemini 3.1 Pro (High)\n"
        "claude-sonnet-4-6         Claude Sonnet 4.6 (Thinking)\n"
        "...\n"
    )
    assert slugs == [
        "gemini-3.7-flash-high",
        "gemini-3.7-flash-medium",
        "gemini-3.1-pro-high",
        "claude-sonnet-4-6",
    ]


def test_agy_models_treats_rc_zero_with_an_error_line_as_failure(monkeypatch) -> None:
    """Verified on the real binary: signed out, it errors AND exits 0.

    So a non-zero exit status cannot be the signal. Trusting rc alone
    would hand a signed-out user an empty lineup, which reads as "my
    subscription includes no models".
    """
    signed_out = subprocess.CompletedProcess(
        args=["agy", "models"],
        returncode=0,
        stdout="Fetching available models...\n",
        stderr=(
            "Error: Please sign in to view available models. "
            "Launch the CLI without arguments to sign in.\n"
        ),
    )
    monkeypatch.setattr(antigravity, "run_agy", lambda *a, **k: signed_out)
    assert antigravity.list_models() == list(antigravity.AGY_FALLBACK_MODELS)


def test_agy_models_falls_back_rather_than_reporting_zero_models(monkeypatch) -> None:
    """An unrecognisable restyle must degrade, not empty the list."""
    restyled = subprocess.CompletedProcess(
        args=["agy", "models"],
        returncode=0,
        stdout="╭─ Models ─╮\n│ (see the /model picker) │\n╰──────────╯\n",
        stderr="",
    )
    monkeypatch.setattr(antigravity, "run_agy", lambda *a, **k: restyled)
    models = antigravity.list_models()
    assert models == list(antigravity.AGY_FALLBACK_MODELS)
    assert models, "an empty lineup reads to a user as 'no models'"


def test_agy_fallback_models_are_effort_suffixed_not_legacy_ids() -> None:
    """The two transports do not share a model namespace.

    A legacy Gemini API id on the agy path fails at request time, and
    headless mode exits non-zero on an unknown --model rather than falling
    back — so a stale namespace here is a hard break, not a downgrade.
    """
    assert antigravity.AGY_DEFAULT_MODEL in antigravity.AGY_FALLBACK_MODELS
    for legacy in ("gemini-3.5-flash", "gemini-3.1-pro-preview", "gemini-3.1-flash-lite"):
        assert legacy not in antigravity.AGY_FALLBACK_MODELS


def test_agy_on_path_is_enough(monkeypatch) -> None:
    monkeypatch.setattr(
        credentials.shutil, "which", lambda cmd: "/usr/bin/agy" if cmd == "agy" else None
    )
    assert credentials._agy_installed() is True


def test_gemini_home_follows_the_clis_own_relocation_switch(
    tmp_path, monkeypatch
) -> None:
    """GEMINI_CLI_HOME is what the CLI itself reads its config root from."""
    monkeypatch.delenv("GEMINI_CONFIG_DIR", raising=False)
    monkeypatch.setenv("GEMINI_CLI_HOME", str(tmp_path / "relocated"))
    assert credentials.GeminiCliStore().home == tmp_path / "relocated"


def test_gemini_project_comes_from_the_environment(gemini_home, monkeypatch) -> None:
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "explicit-project")
    _write_grant(gemini_home)
    store = credentials.GeminiCliStore()

    def _boom(*a, **kw):
        raise AssertionError("an explicit project must not hit the network")

    monkeypatch.setattr(store, "_send", _boom)
    assert store.project() == "explicit-project"


def test_gemini_project_comes_from_the_clis_own_env_file(gemini_home, monkeypatch) -> None:
    """The CLI exports GOOGLE_CLOUD_PROJECT from <config>/.env; so does moru."""
    _write_grant(gemini_home)
    (gemini_home / ".env").write_text("GOOGLE_CLOUD_PROJECT=from-dotenv\n")
    store = credentials.GeminiCliStore()

    def _boom(*a, **kw):
        raise AssertionError("a configured project must not hit the network")

    monkeypatch.setattr(store, "_send", _boom)
    assert store.project() == "from-dotenv"


def test_gemini_project_is_discovered_for_an_account_that_already_has_one(
    gemini_home, monkeypatch
) -> None:
    _write_grant(gemini_home)
    store = credentials.GeminiCliStore()
    send = _Recorder(
        _response(
            200,
            {
                "currentTier": {"id": "free-tier"},
                "cloudaicompanionProject": "managed-42",
            },
        )
    )
    monkeypatch.setattr(store, "_send", send)

    assert store.project() == "managed-42"
    # Persisted, so the next process resolves it without a round trip.
    assert (gemini_home / "moru_project_id").read_text() == "managed-42"
    assert credentials.GeminiCliStore()._cached_project() == "managed-42"


def test_gemini_project_is_provisioned_by_onboarding_without_any_env_var(
    gemini_home, monkeypatch
) -> None:
    """The regression: a personal account gets a project from onboardUser.

    loadCodeAssist answers with no currentTier and no project at all, which
    used to raise the GOOGLE_CLOUD_PROJECT error outright.
    """
    _write_grant(gemini_home)
    store = credentials.GeminiCliStore()
    send = _Recorder(
        _response(200, {"allowedTiers": [{"id": "free-tier", "isDefault": True}]}),
        _response(
            200,
            {"done": True, "response": {"cloudaicompanionProject": {"id": "onboarded-7"}}},
        ),
    )
    monkeypatch.setattr(store, "_send", send)

    assert store.project() == "onboarded-7"
    onboard = send.calls[1]
    assert onboard[1].endswith(":onboardUser")
    assert onboard[2]["tierId"] == "free-tier"
    # The free tier runs on a managed project; naming one is a 412.
    assert "cloudaicompanionProject" not in onboard[2]


def test_gemini_onboards_an_unknown_tier_instead_of_refusing(
    gemini_home, monkeypatch
) -> None:
    """No default tier means legacy-tier, which is NOT a reason to give up.

    The CLI onboards with an undefined project and only fails if the
    operation comes back without one; bailing here is what produced the
    reported `credentials.py:619` hard failure.
    """
    _write_grant(gemini_home)
    store = credentials.GeminiCliStore()
    send = _Recorder(
        _response(200, {"allowedTiers": []}),
        _response(
            200,
            {"done": True, "response": {"cloudaicompanionProject": {"id": "legacy-9"}}},
        ),
    )
    monkeypatch.setattr(store, "_send", send)

    assert store.project() == "legacy-9"
    assert send.calls[1][2]["tierId"] == "legacy-tier"


def test_gemini_project_polls_a_pending_onboarding_operation(
    gemini_home, monkeypatch
) -> None:
    _write_grant(gemini_home)
    store = credentials.GeminiCliStore()
    send = _Recorder(
        _response(200, {"allowedTiers": [{"id": "free-tier", "isDefault": True}]}),
        _response(200, {"done": False, "name": "operations/abc"}),
        _response(
            200,
            {"done": True, "response": {"cloudaicompanionProject": {"id": "slow-1"}}},
        ),
    )
    monkeypatch.setattr(store, "_send", send)

    assert store.project() == "slow-1"
    assert send.calls[2][0] == "GET"
    assert send.calls[2][1].endswith("/v1internal/operations/abc")


def test_gemini_project_error_only_when_every_avenue_is_exhausted(
    gemini_home, monkeypatch
) -> None:
    """A Workspace/GCA account really does have to name its own project."""
    _write_grant(gemini_home)
    store = credentials.GeminiCliStore()
    send = _Recorder(
        _response(200, {"allowedTiers": [{"id": "standard-tier", "isDefault": True}]}),
        _response(200, {"done": True, "response": {}}),
    )
    monkeypatch.setattr(store, "_send", send)

    with pytest.raises(credentials.CliAuthError) as excinfo:
        store.project()
    assert "GOOGLE_CLOUD_PROJECT" in str(excinfo.value)
    # It exhausted the chain rather than refusing up front.
    assert len(send.calls) == 2
    assert not (gemini_home / "moru_project_id").exists()


def test_gemini_project_honors_the_project_id_env_alias(gemini_home, monkeypatch) -> None:
    """GOOGLE_CLOUD_PROJECT_ID is the CLI's second accepted spelling."""
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT_ID", "my-workspace-project")
    _write_grant(gemini_home)
    store = credentials.GeminiCliStore()

    def _boom(*a, **kw):
        raise AssertionError("an explicit project must not hit the network")

    monkeypatch.setattr(store, "_send", _boom)
    assert store.project() == "my-workspace-project"


def test_gemini_onboarded_account_without_a_project_still_needs_one(
    gemini_home, monkeypatch
) -> None:
    """currentTier but no project and nothing configured: genuinely stuck."""
    _write_grant(gemini_home)
    store = credentials.GeminiCliStore()
    send = _Recorder(_response(200, {"currentTier": {"id": "standard-tier"}}))
    monkeypatch.setattr(store, "_send", send)

    with pytest.raises(credentials.CliAuthError) as excinfo:
        store.project()
    assert "GOOGLE_CLOUD_PROJECT" in str(excinfo.value)
    # Never onboards an account the server already placed on a tier.
    assert len(send.calls) == 1


def test_gemini_vpc_sc_denial_is_read_as_standard_tier(gemini_home, monkeypatch) -> None:
    """VPC-SC users get a 403 from loadCodeAssist yet are standard-tier.

    Treating it as a transport failure would hide the one message that
    actually tells them what to do.
    """
    _write_grant(gemini_home)
    store = credentials.GeminiCliStore()
    send = _Recorder(
        _response(403, {"error": {"details": [{"reason": "SECURITY_POLICY_VIOLATED"}]}})
    )
    monkeypatch.setattr(store, "_send", send)

    with pytest.raises(credentials.CliAuthError) as excinfo:
        store.project()
    assert "GOOGLE_CLOUD_PROJECT" in str(excinfo.value)
    assert len(send.calls) == 1


def test_gemini_load_failure_surfaces_in_korean(gemini_home, monkeypatch) -> None:
    _write_grant(gemini_home)
    store = credentials.GeminiCliStore()
    monkeypatch.setattr(store, "_send", _Recorder(_response(500, {"error": {}})))

    with pytest.raises(credentials.CliAuthError) as excinfo:
        store.project()
    assert "계정 정보를 불러올 수 없습니다" in str(excinfo.value)


def test_gemini_status_refuses_to_claim_connected_without_a_project(
    gemini_home, monkeypatch
) -> None:
    """The badge and 연결 테스트 have to agree: both run this chain."""
    _write_grant(gemini_home)
    store = credentials.GeminiCliStore()
    send = _Recorder(
        _response(200, {"allowedTiers": [{"id": "standard-tier", "isDefault": True}]}),
        _response(200, {"done": True, "response": {}}),
    )
    monkeypatch.setattr(store, "_send", send)

    status = store.status()
    assert status["connected"] is False
    assert "GOOGLE_CLOUD_PROJECT" in status["error"]


def test_gemini_status_reports_the_resolved_project(gemini_home, monkeypatch) -> None:
    _write_grant(gemini_home)
    store = credentials.GeminiCliStore()
    send = _Recorder(
        _response(200, {"currentTier": {"id": "free-tier"}, "cloudaicompanionProject": "p-1"})
    )
    monkeypatch.setattr(store, "_send", send)

    status = store.status()
    assert status["connected"] is True
    assert status["project"] == "p-1"
    assert status["error"] is None
    assert status["antigravity"] is False


def test_gemini_status_stays_disconnected_when_no_cli_is_logged_in(gemini_home) -> None:
    status = credentials.GeminiCliStore().status()
    assert status["connected"] is False
    assert status["error"] is None


class _FakeStream:
    """The SSE response `GeminiCliLLM.completion` streams."""

    status_code = 200

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None

    def iter_lines(self):
        return iter(self._lines)


class _FakeClient:
    def __init__(self, stream: _FakeStream, seen: dict) -> None:
        self._stream = stream
        self._seen = seen

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None

    def stream(self, method, url, *, headers, json):
        self._seen["headers"] = headers
        self._seen["payload"] = json
        return self._stream


def test_gemini_completion_runs_for_an_account_with_no_project_env_var(
    gemini_home, monkeypatch
) -> None:
    """The reported bug, end to end: 연결 테스트 died before the request.

    `project()` raised GOOGLE_CLOUD_PROJECT for a personal account, so the
    Cloud Code Assist call was never even attempted.
    """
    from litellm.types.utils import ModelResponse

    _write_grant(gemini_home)
    monkeypatch.setattr(
        credentials.GEMINI_CLI_STORE,
        "_send",
        _Recorder(
            _response(200, {"allowedTiers": [{"id": "free-tier", "isDefault": True}]}),
            _response(
                200,
                {"done": True, "response": {"cloudaicompanionProject": {"id": "managed-3"}}},
            ),
        ),
    )
    credentials.GEMINI_CLI_STORE.invalidate()
    seen: dict = {}
    stream = _FakeStream(
        [
            'data: {"response":{"candidates":[{"content":{"parts":[{"text":"안녕"}]},'
            '"finishReason":"STOP"}],"usageMetadata":{"promptTokenCount":3,'
            '"candidatesTokenCount":1,"totalTokenCount":4}}}'
        ]
    )
    monkeypatch.setattr(
        gemini_cli.httpx, "Client", lambda **kwargs: _FakeClient(stream, seen)
    )

    response = gemini_cli.GeminiCliLLM().completion(
        model="flash",
        messages=[{"role": "user", "content": "hi"}],
        optional_params={"max_tokens": 16},
        model_response=ModelResponse(),
    )

    assert response.choices[0].message.content == "안녕"
    # The project the chain provisioned is what the envelope carries.
    assert seen["payload"]["project"] == "managed-3"
    assert seen["headers"]["Authorization"] == "Bearer live-token"
    credentials.GEMINI_CLI_STORE.invalidate()


# --------------------------------------------------------------------------
# LiteLLM routing
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "expected_provider", "expected_model"),
    [
        ("claude-code/claude-sonnet-4-6", "claude-code", "claude-sonnet-4-6"),
        ("codex/gpt-5.6-luna", "codex", "gpt-5.6-luna"),
        ("gemini-cli/gemini-3.5-flash", "gemini-cli", "gemini-3.5-flash"),
    ],
)
def test_models_route_to_the_cli_handler_without_a_prior_sync_call(
    model: str, expected_provider: str, expected_model: str
) -> None:
    """Registration must reach LiteLLM's provider_list, not just the map.

    LiteLLM folds custom_provider_map into provider_list inside
    custom_llm_setup(), which only its SYNC wrapper calls. acompletion
    resolves the provider before that, so an unregistered prefix fell
    through to name heuristics: "codex/gpt-5.6-luna" went to OpenAI and
    failed with "Missing credentials ... OPENAI_API_KEY". The engine
    translates over the async path, so this is the path that matters.
    """
    from litellm.litellm_core_utils.get_llm_provider_logic import get_llm_provider

    from moru_engine.cli_providers import to_wire_model

    resolved_model, provider, _, _ = get_llm_provider(model=to_wire_model(model))
    assert provider == expected_provider
    # The handler strips the wire marker back off before calling the API.
    assert resolved_model.endswith(expected_model)


def test_cli_providers_are_registered_in_litellms_provider_list() -> None:
    import litellm

    for provider in ("claude-code", "codex", "gemini-cli"):
        assert provider in litellm.provider_list
        assert provider in litellm._custom_providers


def test_wire_marker_keeps_codex_out_of_litellms_openai_table() -> None:
    """Codex SKUs share OpenAI's model names, and LiteLLM dispatches on those.

    `completion()` tests `model in litellm.open_ai_chat_completion_models`
    BEFORE it consults custom_provider_map, so a bare "codex/gpt-5.6-luna"
    was handed to the OpenAI client and died on a missing OPENAI_API_KEY.
    """
    import litellm

    from moru_engine.cli_providers import to_wire_model

    # Precondition: these really are OpenAI names, so the hazard is real.
    assert "gpt-5.6-luna" in litellm.open_ai_chat_completion_models

    wire = to_wire_model("codex/gpt-5.6-luna")
    assert wire == "codex/@/gpt-5.6-luna"
    bare = wire.split("/", 1)[1]
    assert bare not in litellm.open_ai_chat_completion_models

    # Round-trips to the slug the backend expects.
    assert codex.resolve_model(bare) == "gpt-5.6-luna"


def test_to_wire_model_leaves_non_cli_models_alone() -> None:
    from moru_engine.cli_providers import to_wire_model

    for untouched in ("openai/gpt-5.6-luna", "ollama_chat/qwen3:8b", "gpt-4.1", ""):
        assert to_wire_model(untouched) == untouched
    # Idempotent: re-wrapping an already-wired id must not double the marker.
    assert to_wire_model("codex/@/gpt-5.6-luna") == "codex/@/gpt-5.6-luna"


def test_every_catalogued_cli_model_survives_the_wire_round_trip() -> None:
    """No catalog entry may resolve to a name LiteLLM would hijack."""
    import litellm

    from moru_engine.cli_providers import CLI_PROVIDER_CATALOG, to_wire_model

    resolvers = {
        "claude-code": claude_code.resolve_model,
        "codex": codex.resolve_model,
        "gemini-cli": gemini_cli.resolve_model,
    }
    for entry in CLI_PROVIDER_CATALOG:
        for public in entry["models"]:
            bare = to_wire_model(public).split("/", 1)[1]
            assert bare not in litellm.open_ai_chat_completion_models, public
            assert resolvers[entry["id"]](bare) == public.split("/", 1)[1]


# --------------------------------------------------------------------------
# Antigravity translation transport — `agy -p` headless mode
# --------------------------------------------------------------------------


def _envelope(**fields) -> subprocess.CompletedProcess[str]:
    body = {
        "conversation_id": "c1",
        "status": "SUCCESS",
        "response": "",
        "duration_seconds": 1.0,
        "num_turns": 1,
        "usage": {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
    }
    body.update(fields)
    return subprocess.CompletedProcess(
        args=["agy"], returncode=0, stdout=json.dumps(body) + "\n", stderr=""
    )


def _capture(monkeypatch, proc):
    """Run complete() against a canned envelope, returning the argv used."""
    seen: dict[str, list[str]] = {}

    def fake(args, *, timeout, cwd=None):
        seen["args"] = list(args)
        seen["cwd"] = str(cwd)
        return proc

    monkeypatch.setattr(antigravity, "run_agy", fake)
    antigravity._clear_auth_failure()
    return seen


def test_agy_completion_reads_structured_output_not_free_text(monkeypatch) -> None:
    """The forced schema is what keeps agent narration out of a translation.

    `response` here holds prose an agent might volunteer; the trustworthy
    value is the schema-validated field, and that is what must be used.
    """
    proc = _envelope(
        response="Sure! Here is the translation you asked for.",
        structured_output={"text": "고대 잔해"},
    )
    _capture(monkeypatch, proc)
    text, usage = antigravity.complete("gemini-3.7-flash-medium", [
        {"role": "user", "content": "Ancient Debris"}
    ])
    assert text == "고대 잔해"
    assert "Sure!" not in text
    assert usage == {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14}


def test_agy_completion_refuses_an_unschematized_answer(monkeypatch) -> None:
    """No structured_output means the answer is unvalidated free text.

    Returning it would risk pasting agent commentary into a modpack as a
    translation. A failed request gets retried; a corrupted one ships.
    """
    proc = _envelope(response="I translated it for you: 고대 잔해")
    _capture(monkeypatch, proc)
    with pytest.raises(antigravity.AgyError):
        antigravity.complete("gemini-3.7-flash-medium", [
            {"role": "user", "content": "Ancient Debris"}
        ])


def test_agy_completion_fails_when_protected_tokens_are_dropped(monkeypatch) -> None:
    """A dropped {{TOKEN}} corrupts the pack, so it must fail loudly."""
    proc = _envelope(structured_output={"text": "고대 잔해 y=15"})
    _capture(monkeypatch, proc)
    with pytest.raises(antigravity.AgyError) as excinfo:
        antigravity.complete("gemini-3.7-flash-medium", [
            {"role": "user", "content": "{{COLOR}}Ancient Debris{{PH1}} y=15"}
        ])
    assert "{{COLOR}}" in str(excinfo.value)


def test_agy_completion_passes_tokens_through_untouched(monkeypatch) -> None:
    proc = _envelope(structured_output={"text": "{{COLOR}}고대 잔해{{PH1}} y=15"})
    _capture(monkeypatch, proc)
    text, _ = antigravity.complete("gemini-3.7-flash-medium", [
        {"role": "user", "content": "{{COLOR}}Ancient Debris{{PH1}} y=15"}
    ])
    assert "{{COLOR}}" in text and "{{PH1}}" in text


def test_agy_status_error_beats_a_zero_exit_code(monkeypatch) -> None:
    """Verified on the real binary: rc=0 alongside status ERROR.

    So the envelope's status is authoritative, not the exit code.
    """
    proc = subprocess.CompletedProcess(
        args=["agy"],
        returncode=0,
        stdout=json.dumps(
            {"status": "ERROR", "response": "", "error": "authentication failed or timed out"}
        ),
        stderr="Authentication required. Please visit the URL to log in:\n",
    )
    _capture(monkeypatch, proc)
    with pytest.raises(antigravity.AgyError) as excinfo:
        antigravity.complete("gemini-3.7-flash-medium", [{"role": "user", "content": "x"}])
    # Translated into something the user can act on, not the raw error.
    assert "agy" in str(excinfo.value)
    antigravity._clear_auth_failure()


def test_agy_invocation_carries_every_mitigation(monkeypatch) -> None:
    """The flags are the safety argument; a silent drop would undo it.

    All four were accepted by the real v1.1.22 binary — an unknown flag
    fails with "flags provided but not defined", which is how we know
    `agy models --output-format` does not exist.
    """
    proc = _envelope(structured_output={"text": "ok"})
    seen = _capture(monkeypatch, proc)
    antigravity.complete("gemini-3.1-pro-high", [{"role": "user", "content": "x"}])
    args = seen["args"]
    assert "-p" in args
    assert args[args.index("--output-format") + 1] == "json"
    assert "--json-schema" in args
    assert "--disable-slash-commands" in args
    assert args[args.index("--print-timeout") + 1].endswith("s")
    assert args[args.index("--model") + 1] == "gemini-3.1-pro-high"
    # stream-json is deliberately absent: one shared conversation would
    # accumulate context and usage across independent batches.
    assert "--input-format" not in args
    # An agent run must not be pointed at the user's own project.
    assert seen["cwd"] and seen["cwd"] != str(Path.cwd())


def test_agy_prompt_folds_the_system_message_with_visible_delimiters() -> None:
    """agy has no --system flag, so separation must be expressed in text."""
    prompt = antigravity.build_prompt(
        [
            {"role": "system", "content": "Translate to Korean."},
            {"role": "user", "content": "Ancient Debris"},
        ]
    )
    assert "Translate to Korean." in prompt
    assert "Ancient Debris" in prompt
    assert prompt.index("Translate to Korean.") < prompt.index("Ancient Debris")
    assert antigravity._SYSTEM_DELIM in prompt
    assert antigravity._INPUT_DELIM in prompt


def test_agy_process_cap_is_bounded_and_overridable(monkeypatch) -> None:
    """A considered limit, because 145MB x 15 is not free on every machine."""
    monkeypatch.setenv(antigravity.MAX_PROCESSES_ENV, "3")
    assert antigravity.process_cap() == 3
    # Never above the pipeline's own concurrency, never below one.
    monkeypatch.setenv(antigravity.MAX_PROCESSES_ENV, "999")
    assert antigravity.process_cap() == antigravity._MAX_PROCESSES
    monkeypatch.setenv(antigravity.MAX_PROCESSES_ENV, "0")
    assert antigravity.process_cap() == 1
    monkeypatch.setenv(antigravity.MAX_PROCESSES_ENV, "not-a-number")
    assert 1 <= antigravity.process_cap() <= antigravity._MAX_PROCESSES


def test_agy_auth_failure_short_circuits_the_next_call(monkeypatch) -> None:
    """A signed-out agy waits 60s per process; the batch must not pay it N times."""
    calls = {"n": 0}

    def fake(args, *, timeout, cwd=None):
        calls["n"] += 1
        return subprocess.CompletedProcess(
            args=["agy"],
            returncode=0,
            stdout=json.dumps({"status": "ERROR", "error": "authentication failed"}),
            stderr="",
        )

    monkeypatch.setattr(antigravity, "run_agy", fake)
    antigravity._clear_auth_failure()
    for _ in range(3):
        with pytest.raises(antigravity.AgyError):
            antigravity.complete("gemini-3.7-flash-medium", [{"role": "user", "content": "x"}])
    assert calls["n"] == 1, "only the first attempt should reach the subprocess"
    antigravity._clear_auth_failure()


_FAKE_AGY = '''#!{python}
import json, os, subprocess, sys, time

args = sys.argv[1:]
if os.environ.get("FAKE_AGY_HANG"):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    with open(os.environ["FAKE_AGY_HANG"], "w") as fh:
        fh.write(f"{{os.getpid()}} {{child.pid}}")
    time.sleep(60)
if "--input-format" in args:
    prompt = json.loads(sys.stdin.readline())["message"]["content"]
    via = "stdin"
else:
    prompt = args[args.index("-p") + 1]
    via = "argv"
envelope = {{"status": "SUCCESS", "structured_output": {{"text": f"{{via}}:{{len(prompt)}}"}},
            "usage": {{"input_tokens": 3, "output_tokens": 1, "total_tokens": 4}}}}
if "--output-format" in args and args[args.index("--output-format") + 1] == "stream-json":
    print(json.dumps({{"event": "init", "init": {{}}}}))
    print(json.dumps({{"event": "result", "result": envelope}}))
else:
    print(json.dumps(envelope))
'''


@pytest.fixture
def fake_agy(tmp_path, monkeypatch):
    import sys

    script = tmp_path / "agy"
    script.write_text(_FAKE_AGY.format(python=sys.executable))
    script.chmod(0o755)
    monkeypatch.setattr(antigravity, "agy_path", lambda: script)
    antigravity._clear_auth_failure()
    return script


@pytest.mark.skipif(__import__("os").name == "nt", reason="POSIX shebang script")
async def test_agy_async_long_prompt_goes_over_stdin(fake_agy, monkeypatch) -> None:
    """A ~40k-character batch prompt as `-p` exceeds Windows' 32,767-char
    command line (WinError 206); past the limit it rides stdin instead."""
    messages = [{"role": "user", "content": "x" * 500}]
    text, usage = await antigravity.acomplete("gemini-3.7-flash-medium", messages)
    assert text == "argv:500"
    assert usage["total_tokens"] == 4
    monkeypatch.setattr(antigravity, "_ARGV_PROMPT_LIMIT", 100)
    text, _ = await antigravity.acomplete("gemini-3.7-flash-medium", messages)
    assert text == "stdin:500"
    # The sync path takes the same route.
    text, _ = antigravity.complete("gemini-3.7-flash-medium", messages)
    assert text == "stdin:500"


def test_agy_windows_limit_counts_the_quoted_command_line(monkeypatch) -> None:
    """CreateProcess counts the quoted line, where each '"' of a JSON-shaped
    prompt gains a backslash: 20k bytes of quotes is 40k on the wire."""
    monkeypatch.setattr(antigravity, "agy_path", lambda: Path("agy.exe"))
    monkeypatch.setattr(antigravity.os, "name", "nt")
    argv, stdin = antigravity._invocation('"' * 20_000, "m")
    assert "-p" not in argv and stdin is not None
    argv, stdin = antigravity._invocation("x" * 20_000, "m")
    assert "-p" in argv and stdin is None


def _alive(pid: int) -> bool:
    import os

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.skipif(__import__("os").name == "nt", reason="POSIX process groups")
async def test_agy_cancel_kills_the_process_and_its_children(
    fake_agy, tmp_path, monkeypatch
) -> None:
    """Under to_thread a cancelled run kept agy (and what it spawned) going."""
    import asyncio

    marker = tmp_path / "pids"
    monkeypatch.setenv("FAKE_AGY_HANG", str(marker))
    task = asyncio.create_task(
        antigravity.acomplete("gemini-3.7-flash-medium", [{"role": "user", "content": "x"}])
    )
    for _ in range(200):
        if marker.exists() and marker.read_text().strip():
            break
        await asyncio.sleep(0.05)
    agy_pid, child_pid = map(int, marker.read_text().split())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    for _ in range(100):
        if not _alive(agy_pid) and not _alive(child_pid):
            break
        await asyncio.sleep(0.05)
    assert not _alive(agy_pid)
    assert not _alive(child_pid)


@pytest.mark.skipif(__import__("os").name == "nt", reason="POSIX shebang script")
async def test_agy_async_timeout_is_a_clear_error(fake_agy, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_AGY_HANG", str(tmp_path / "pids"))
    monkeypatch.setattr(antigravity, "_PRINT_TIMEOUT_S", 0.5)
    monkeypatch.setattr(antigravity, "_KILL_GRACE_S", 0.0)
    with pytest.raises(antigravity.AgyError, match="초 안에"):
        await antigravity.acomplete("gemini-3.7-flash-medium", [{"role": "user", "content": "x"}])
    agy_pid, _ = map(int, (tmp_path / "pids").read_text().split())
    assert not _alive(agy_pid)


def test_gemini_handler_routes_to_agy_when_that_is_the_transport(
    gemini_home, monkeypatch
) -> None:
    """The dispatch itself: no oauth_creds.json, agy installed -> subprocess.

    Proves an Antigravity user can translate without any API key and
    without the legacy HTTP path being touched.
    """
    monkeypatch.setattr(credentials, "_agy_installed", lambda: True)
    monkeypatch.setattr(
        antigravity, "run_agy",
        lambda args, *, timeout, cwd=None: _envelope(structured_output={"text": "고대 잔해"}),
    )
    antigravity._clear_auth_failure()

    def _no_http(*_a, **_k):
        raise AssertionError("agy transport must not call cloudcode-pa")

    monkeypatch.setattr(credentials.GEMINI_CLI_STORE, "_send", _no_http)
    response = gemini_cli.GeminiCliLLM().completion(
        model="@/gemini-3.7-flash-medium",
        messages=[{"role": "user", "content": "Ancient Debris"}],
        model_response=ModelResponse(),
    )
    assert response.choices[0].message.content == "고대 잔해"
    assert response.model == "gemini-cli/gemini-3.7-flash-medium"


def test_saved_legacy_model_on_the_agy_path_names_its_replacement() -> None:
    """A retired id must produce a clear message, not a confusing failure.

    agy exits non-zero on an unknown --model and names no alternative, so
    passing it through would leave the user guessing. Silently substituting
    would bill a model other than the one displayed.
    """
    with pytest.raises(antigravity.AgyError) as excinfo:
        antigravity.resolve_model_for_agy("gemini-cli/gemini-3.5-flash")
    message = str(excinfo.value)
    assert "gemini-3.5-flash" in message
    assert "gemini-3.5-flash-medium" in message


# --------------------------------------------------------------------------
# error_code — a localizable reason beside the Korean prose
# --------------------------------------------------------------------------


def test_error_prose_is_kept_verbatim_beside_the_code(gemini_home, monkeypatch) -> None:
    """`error` must not change shape: an older desktop shows it verbatim.

    The renderer sanitizes and displays this string, so removing or
    restructuring it would regress clients that have no code mapping. The
    code is added ALONGSIDE, never instead.
    """
    _write_grant(gemini_home)
    store = credentials.GeminiCliStore()
    monkeypatch.setattr(
        store, "project",
        lambda: (_ for _ in ()).throw(
            credentials.CliAuthError(
                credentials._NEEDS_PROJECT_ENV,
                code=credentials.REASON_PROJECT_REQUIRED,
            )
        ),
    )
    status = store.status()
    assert status["error"] == credentials._NEEDS_PROJECT_ENV
    assert status["error_code"] == credentials.REASON_PROJECT_REQUIRED
    assert status["state"] == credentials.STATE_UNUSABLE


def test_project_required_and_setup_failed_are_told_apart(gemini_home, monkeypatch) -> None:
    """Same prose shape, opposite instructions.

    "Set GOOGLE_CLOUD_PROJECT" is the user's job; "Code Assist refused" is
    not. A renderer cannot distinguish them from the sentence alone, which
    is the whole reason a code exists.
    """
    _write_grant(gemini_home)
    store = credentials.GeminiCliStore()

    monkeypatch.setattr(
        store, "project",
        lambda: (_ for _ in ()).throw(
            credentials.CliAuthError("등록 실패", code=credentials.REASON_PROJECT_SETUP_FAILED)
        ),
    )
    assert store.status()["error_code"] == credentials.REASON_PROJECT_SETUP_FAILED

    monkeypatch.setattr(
        store, "project",
        lambda: (_ for _ in ()).throw(
            credentials.CliAuthError("프로젝트 필요", code=credentials.REASON_PROJECT_REQUIRED)
        ),
    )
    assert store.status()["error_code"] == credentials.REASON_PROJECT_REQUIRED


def test_an_uncoded_failure_falls_through_to_the_prose(gemini_home, monkeypatch) -> None:
    """No invented taxonomy: an unmapped failure carries no code.

    Emitting a code for an httpx error or a bug would tell the renderer to
    show localized copy that does not describe what happened. None means
    "use the prose", which is the honest answer.
    """
    _write_grant(gemini_home)
    store = credentials.GeminiCliStore()
    monkeypatch.setattr(
        store, "project",
        lambda: (_ for _ in ()).throw(RuntimeError("connection reset by peer")),
    )
    status = store.status()
    assert status["error_code"] is None
    assert "connection reset" in status["error"]


def test_login_required_is_the_code_for_every_token_problem(monkeypatch) -> None:
    """Not logged in, expired, and unreadable all mean "sign in again"."""
    store = credentials.CodexStore()
    monkeypatch.setattr(store, "_load", lambda: None)
    with pytest.raises(credentials.CliAuthError) as excinfo:
        store.credentials()
    assert excinfo.value.code == credentials.REASON_LOGIN_REQUIRED

    # An unreadable credential file: same instruction, same code.
    def _boom():
        raise ValueError("bad json")

    monkeypatch.setattr(store, "_load", _boom)
    assert store.status()["error_code"] == credentials.REASON_LOGIN_REQUIRED


def test_unknown_provider_reports_a_code_too() -> None:
    status = provider_status("not-a-provider")
    assert status["error_code"] == credentials.REASON_UNKNOWN_PROVIDER
    assert "unknown provider" in status["error"]


def test_reason_codes_stay_a_small_closed_set() -> None:
    """Guards against the taxonomy creeping.

    Each code has to imply a DIFFERENT user action; anything that reads as
    the same sentence belongs folded into an existing one. If this list
    grows, that decision should be deliberate rather than incidental.
    """
    codes = {
        name: value
        for name, value in vars(credentials).items()
        if name.startswith("REASON_")
    }
    assert set(codes.values()) == {
        "login-required",
        "project-required",
        "project-setup-failed",
        "unknown-provider",
    }
    # Stable wire strings: renderers key their copy off these.
    assert all(value == value.lower() and " " not in value for value in codes.values())


# --------------------------------------------------------------------------
# Event loop hygiene
# --------------------------------------------------------------------------


@pytest.mark.skipif(not hasattr(__import__("os"), "fchmod"), reason="POSIX modes")
def test_private_credentials_are_never_world_readable(tmp_path, monkeypatch) -> None:
    """Written under the default umask and chmodded after, fresh tokens sat
    world-readable in between. The mode must be right from creation."""
    import os
    import stat

    target = tmp_path / "auth.json"
    stale_tmp = tmp_path / "auth.json.moru-tmp"
    stale_tmp.write_text("{}")
    stale_tmp.chmod(0o644)
    # A chmod-after-write never runs here, as if the process died between
    # the two steps: whatever mode the file was created with is what stays.
    monkeypatch.setattr(Path, "chmod", lambda self, mode: None)
    old = os.umask(0o022)
    try:
        credentials._atomic_write_json(target, {"token": "t"}, private=True)
    finally:
        os.umask(old)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert json.loads(target.read_text()) == {"token": "t"}
    assert not stale_tmp.exists()


def _off_loop_probe(seen: list[bool]):
    import threading

    def probe(*_args: object) -> str:
        seen.append(threading.current_thread() is not threading.main_thread())
        raise credentials.CliAuthError("stop here")

    return probe


async def test_codex_refreshes_credentials_off_the_event_loop(monkeypatch) -> None:
    """A stale grant refreshes over sync httpx (30s timeout): on the loop it
    froze every concurrent batch and the job WebSocket."""
    seen: list[bool] = []
    monkeypatch.setattr(credentials.CODEX_STORE, "credentials", _off_loop_probe(seen))
    with pytest.raises(credentials.CliAuthError):
        await codex.CodexLLM().acompletion(
            model="gpt-5.5", messages=[], model_response=ModelResponse()
        )
    with pytest.raises(credentials.CliAuthError):
        await codex.fetch_models()
    assert seen == [True, True]


async def test_gemini_refreshes_credentials_off_the_event_loop(monkeypatch) -> None:
    seen: list[bool] = []
    monkeypatch.setattr(
        type(credentials.GEMINI_CLI_STORE), "transport", property(lambda self: "http")
    )
    monkeypatch.setattr(credentials.GEMINI_CLI_STORE, "token", _off_loop_probe(seen))
    with pytest.raises(credentials.CliAuthError):
        await gemini_cli.GeminiCliLLM().acompletion(
            model="gemini-3.5-flash", messages=[], model_response=ModelResponse()
        )
    assert seen == [True]


async def test_codex_batches_share_one_pooled_client(monkeypatch) -> None:
    """A client per request paid a fresh TCP+TLS handshake on every batch."""
    import asyncio
    from types import SimpleNamespace

    from moru_engine.cli_providers import http

    sse = (
        'data: {"type":"response.output_text.delta","delta":"ok"}\n\n'
        'data: {"type":"response.completed","response":{"usage":'
        '{"input_tokens":1,"output_tokens":1}}}\n\n'
    )
    clients: list[int] = []

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=sse)

    pooled = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    http._clients[asyncio.get_running_loop()] = pooled
    original = http.async_client

    def tracking() -> httpx.AsyncClient:
        client = original()
        clients.append(id(client))
        return client

    monkeypatch.setattr(codex, "async_client", tracking)
    monkeypatch.setattr(
        credentials.CODEX_STORE,
        "credentials",
        lambda: SimpleNamespace(access="t", account_id="a"),
    )
    for _ in range(2):
        response = await codex.CodexLLM().acompletion(
            model="gpt-5.5", messages=[{"role": "user", "content": "hi"}],
            model_response=ModelResponse(),
        )
        assert response.choices[0].message.content == "ok"
    assert clients == [id(pooled), id(pooled)]
    assert not pooled.is_closed
    await http.aclose_async_client()
    assert pooled.is_closed
    assert http.async_client() is not pooled
    await http.aclose_async_client()
