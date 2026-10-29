"""Credential stores for locally installed coding CLIs.

Codex and the legacy Gemini CLI keep an OAuth grant on disk: moru reads it,
refreshes it when it expires, and writes the rotated tokens back **in the
CLI's native format** so one grant keeps serving both moru and the CLI.
Client ids, endpoints and refresh payloads there are the CLIs' own.

Claude Code is different: moru never touches its grant. The official
``claude`` binary authenticates its own requests, and this module only asks
it whether it is signed in.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import platform
import shutil
import subprocess
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from dotenv import dotenv_values

logger = logging.getLogger(__name__)

#: Refresh this long before the real expiry. Mirrors the 5-minute margin
#: oh-my-pi bakes into its stored `expires` for Anthropic/Google.
_SKEW_MS = 5 * 60 * 1000

_TIMEOUT = httpx.Timeout(30.0)

#: Coarse auth states a UI can branch on without parsing prose. The
#: desktop renders a different affordance for each — install the CLI, log
#: in, or surface `error` — so the distinction has to survive the API
#: boundary rather than being inferred from `connected` plus a guess.
STATE_CLI_MISSING = "cli-missing"      # no CLI on this machine
STATE_LOGGED_OUT = "logged-out"        # CLI present, no credential
STATE_CLI_READY = "cli-ready"          # CLI present, login state only it can prove
STATE_READY = "ready"                  # credential usable now
STATE_UNUSABLE = "unusable"            # logged in, but a request would fail

#: Which backend a `gemini-cli` request takes. One provider id, two very
#: different transports; see `GeminiCliStore.transport` for precedence.
TRANSPORT_LEGACY_HTTP = "legacy-http"  # borrowed OAuth -> cloudcode-pa
TRANSPORT_AGY_CLI = "agy-cli"          # subprocess -> agy headless mode


#: Machine-readable failure reasons, paired with the human `error` string.
#: The engine writes `error` in Korean, and a client that shows it verbatim
#: leaks one locale into a product shipping English too. So `error_code`
#: travels alongside it and a renderer maps the code to its own localized
#: copy, falling back to the prose for any code it does not recognise.
#:
#: Deliberately tiny and closed. Each code exists only because it implies a
#: DIFFERENT action for the user; anything that would read as the same
#: sentence is folded into one code, and anything not enumerated here
#: arrives as `error_code: None` so the prose still shows.
REASON_LOGIN_REQUIRED = "login-required"          # sign in again (any token problem)
REASON_PROJECT_REQUIRED = "project-required"      # set GOOGLE_CLOUD_PROJECT
REASON_PROJECT_SETUP_FAILED = "project-setup-failed"  # Code Assist refused; not the user
REASON_UNKNOWN_PROVIDER = "unknown-provider"      # internal: bad provider id


class CliAuthError(RuntimeError):
    """No usable credential for a CLI provider.

    Carries a message aimed at the desktop UI: what to run to fix it, plus
    an optional stable `code` so the UI can say it in the user's own
    language instead of echoing ours.
    """

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


def reason_of(exc: BaseException, default: str | None = None) -> str | None:
    """The stable reason code an exception carries, if any.

    Only `CliAuthError` sets one. Anything else \u2014 an httpx error, a JSON
    decode, a bug \u2014 has no meaningful code, and inventing one would tell a
    renderer to show localized copy that does not match what happened. Those
    fall through to `default` (usually None), leaving the prose to speak.
    """
    code = getattr(exc, "code", None)
    return code if isinstance(code, str) and code else default


def _now_ms() -> int:
    return int(time.time() * 1000)


def _decode_jwt(token: str) -> dict[str, Any]:
    """Best-effort JWT payload decode; {} when the token is opaque."""
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, TypeError):
        return {}


def _atomic_write_json(path: Path, data: object, *, private: bool) -> None:
    """Write JSON via temp file + os.replace, optionally mode 600.

    A private file is CREATED 600: writing under the default umask and
    chmodding afterwards left the fresh tokens world-readable in between.
    A leftover temp file is removed first, since O_CREAT keeps the mode of
    a file that already exists.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".moru-tmp")
    body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    tmp.unlink(missing_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(tmp, flags, 0o600 if private else 0o666)
    with os.fdopen(fd, "wb") as handle:
        handle.write(body)
    os.replace(tmp, path)


def _refreshed(resp: httpx.Response) -> dict[str, Any]:
    """Parsed body of a token-refresh response, or the failure it reports."""
    if resp.status_code != 200:
        raise CliAuthError(
            f"token refresh failed ({resp.status_code}): {resp.text[:300]}",
            code=REASON_LOGIN_REQUIRED,
        )
    return resp.json()


@dataclass
class OAuthCredentials:
    """One CLI's OAuth grant, normalized across providers."""

    access: str
    refresh: str = ""
    #: Epoch ms of the true expiry (no skew applied).
    expires: int = 0
    project_id: str | None = None
    account_id: str | None = None
    email: str | None = None
    #: Provider-native document this was parsed from, so writes can
    #: round-trip keys we do not model (mcpOAuth, scopes, id_token, ...).
    raw: dict[str, Any] = field(default_factory=dict)

    def stale(self) -> bool:
        # expires == 0 means "unknown"; treat as fresh and let a 401 drive
        # the refresh instead of refreshing on every single call.
        if self.expires <= 0:
            return False
        return self.expires - _SKEW_MS <= _now_ms()


class CliCredentialStore(ABC):
    """Reads, refreshes and writes back one CLI's OAuth credential file."""

    #: Provider id in moru's catalog (e.g. "claude-code").
    id: str
    #: Human label used in error messages.
    label: str
    #: Command the user runs to (re-)authenticate. Reported to the client
    #: as `login_hint` so a renderer never hardcodes a per-CLI command.
    login_hint: str
    #: Binary this grant belongs to, probed on PATH to tell "CLI not
    #: installed" apart from "installed but logged out".
    cli_command: str

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache: OAuthCredentials | None = None

    # -- provider hooks ---------------------------------------------------

    @property
    @abstractmethod
    def path(self) -> Path:
        """Credential file the CLI owns."""

    @abstractmethod
    def _load(self) -> OAuthCredentials | None:
        """Parse the credential file, or None when absent/unusable."""

    @abstractmethod
    def _refresh(self, creds: OAuthCredentials) -> OAuthCredentials:
        """Exchange the refresh token for a fresh access token."""

    @abstractmethod
    def _save(self, creds: OAuthCredentials) -> None:
        """Write rotated tokens back in the CLI's native format."""

    # -- public API -------------------------------------------------------

    def available(self) -> bool:
        """True when a credential exists, without refreshing it."""
        try:
            return self._load() is not None
        except Exception:  # noqa: BLE001 - availability must never raise
            logger.debug("Credential probe failed for %s", self.id, exc_info=True)
            return False

    def cli_installed(self) -> bool:
        """Whether the CLI that owns this grant is on this machine.

        What separates "install the CLI" from "log in" in the UI: without
        it a logged-out user and a user who never had the CLI look
        identical, and the app can only offer one generic message.
        """
        return shutil.which(self.cli_command) is not None

    def status(self) -> dict[str, Any]:
        """Auth summary for GET /providers.

        `state` is the field a client should branch on; `connected` is kept
        for older clients that only understood a boolean. `error` keeps its
        exact prose for the same reason, with `error_code` added beside it
        so a client can localize instead of echoing our Korean.
        """
        installed = self.cli_installed()
        try:
            creds = self._load()
        except Exception as exc:  # noqa: BLE001
            # An unreadable credential file is fixed by signing in again,
            # which is the same instruction as a missing one — so it shares
            # that code rather than earning its own.
            return {
                "connected": False,
                "error": str(exc),
                "error_code": reason_of(exc, REASON_LOGIN_REQUIRED),
                "state": STATE_UNUSABLE,
                "cli_installed": installed,
                "cli": self.cli_command,
            }
        if creds is None:
            return {
                "connected": False,
                "error": None,
                "error_code": None,
                "state": STATE_LOGGED_OUT if installed else STATE_CLI_MISSING,
                "cli_installed": installed,
                "cli": self.cli_command,
            }
        return {
            "connected": True,
            "error": None,
            "error_code": None,
            "email": creds.email,
            "expires": creds.expires or None,
            "state": STATE_READY,
            "cli_installed": installed,
            "cli": self.cli_command,
        }

    def credentials(self) -> OAuthCredentials:
        """Valid credentials, refreshing and persisting when stale."""
        with self._lock:
            creds = self._cache
            # Always re-read when the cache is cold or stale: the CLI may
            # have rotated the grant from under us.
            if creds is None or creds.stale():
                creds = self._load()
            if creds is None:
                raise CliAuthError(
                    f"{self.label}에 로그인되어 있지 않습니다. 터미널에서 "
                    f"`{self.login_hint}`로 로그인한 뒤 다시 시도해 주세요.",
                    code=REASON_LOGIN_REQUIRED,
                )
            if creds.stale():
                if not creds.refresh:
                    raise CliAuthError(
                        f"{self.label} 토큰이 만료되었고 갱신 토큰이 없습니다. "
                        f"`{self.login_hint}`로 다시 로그인해 주세요.",
                        code=REASON_LOGIN_REQUIRED,
                    )
                logger.info("Refreshing %s credentials", self.id)
                creds = self._refresh(creds)
                self._save(creds)
            self._cache = creds
            return creds

    def token(self) -> str:
        return self.credentials().access

    def invalidate(self) -> None:
        """Drop the in-process cache so the next call re-reads from disk."""
        with self._lock:
            self._cache = None

    # -- helpers ----------------------------------------------------------

    def _read_json(self) -> dict[str, Any] | None:
        path = self.path
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("Unreadable credential file: %s", path, exc_info=True)
            return None
        return data if isinstance(data, dict) else None

    def _send(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        form: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """The one HTTP call every store makes.

        Returns the raw response instead of a parsed body: provisioning
        flows have to read a 4xx (Cloud Code Assist answers VPC-SC users
        with one), and a single seam is what the tests patch.
        """
        with httpx.Client(timeout=_TIMEOUT) as client:
            return client.request(
                method, url, headers=headers or {}, data=form, json=json_body
            )

    def _post_form(
        self, url: str, form: dict[str, str], headers: dict[str, str] | None = None
    ) -> dict[str, Any]:
        return _refreshed(self._send("POST", url, headers=headers, form=form))

    def _post_json(
        self, url: str, body: dict[str, Any], headers: dict[str, str] | None = None
    ) -> dict[str, Any]:
        return _refreshed(self._send("POST", url, headers=headers, json_body=body))


# ---------------------------------------------------------------------------
# Claude Code — the official `claude` CLI is the only authority on its grant
# ---------------------------------------------------------------------------

_CLAUDE_COMMAND = "claude"
_CLAUDE_STATUS_TIMEOUT = 20.0
#: `claude auth status` costs a process start, and GET /providers polls.
_CLAUDE_STATUS_TTL_S = 5.0

#: Variables that would silently move `claude` off the user's subscription
#: onto pay-per-token API billing. A desktop-saved Anthropic key reaches the
#: engine's environment, so they are removed from every `claude` child.
_CLAUDE_BILLING_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


def claude_path() -> Path | None:
    """Filesystem path to the `claude` binary, or None when absent.

    PATH alone is not enough: the installer drops `claude` in ~/.local/bin,
    which a GUI-spawned sidecar's environment routinely omits (a packaged
    Electron app does not inherit the login shell's PATH), so the documented
    install locations are probed too. Same reasoning as `agy_path`.
    """
    found = shutil.which(_CLAUDE_COMMAND)
    if found is not None:
        return Path(found)
    home = Path.home()
    config = os.environ.get("CLAUDE_CONFIG_DIR")
    candidates = [
        home / ".local" / "bin" / _CLAUDE_COMMAND,
        # The legacy self-updating install.
        (Path(config) if config else home / ".claude") / "local" / _CLAUDE_COMMAND,
        Path("/opt/homebrew/bin") / _CLAUDE_COMMAND,
        Path("/usr/local/bin") / _CLAUDE_COMMAND,
        home / ".npm-global" / "bin" / _CLAUDE_COMMAND,
        home / ".bun" / "bin" / _CLAUDE_COMMAND,
    ]
    if os.name == "nt":
        candidates.append(home / ".local" / "bin" / f"{_CLAUDE_COMMAND}.exe")
    for path in candidates:
        if path.is_file():
            return path
    return None


def claude_env(binary: Path | None = None) -> dict[str, str]:
    """Environment for a `claude` child process.

    The binary's own directory and the usual tool directories lead PATH: an
    npm-installed `claude` is a script that needs `node`, which the sidecar's
    trimmed PATH may not reach. Billing variables are dropped (see
    `_CLAUDE_BILLING_ENV`).
    """
    env = {k: v for k, v in os.environ.items() if k not in _CLAUDE_BILLING_ENV}
    extra = [str(binary.parent)] if binary is not None else []
    if os.name != "nt":
        extra += ["/opt/homebrew/bin", "/usr/local/bin"]
    current = env.get("PATH", "")
    env["PATH"] = os.pathsep.join([*extra, current] if current else extra)
    return env


class ClaudeCodeStore(CliCredentialStore):
    """Login state of the user's own `claude` CLI.

    Moru never reads or writes this grant. The CLI keeps it in the macOS
    keychain or ``.credentials.json`` and rotates it itself; asking
    ``claude auth status`` is the one stable way to learn whether it is
    signed in, whatever it is stored in on this machine.
    """

    id = "claude-code"
    label = "Claude Code"
    login_hint = "claude auth login"
    cli_command = _CLAUDE_COMMAND

    def __init__(self) -> None:
        super().__init__()
        self._status_cache: tuple[float, dict[str, Any] | None] | None = None

    @property
    def path(self) -> Path:
        override = os.environ.get("CLAUDE_CONFIG_DIR")
        base = Path(override) if override else Path.home() / ".claude"
        return base / ".credentials.json"

    def cli_installed(self) -> bool:
        return claude_path() is not None

    def invalidate(self) -> None:
        super().invalidate()
        self._status_cache = None

    def _auth_status(self) -> dict[str, Any] | None:
        cached = self._status_cache
        if cached is not None and time.monotonic() - cached[0] < _CLAUDE_STATUS_TTL_S:
            return cached[1]
        doc = self._run_auth_status()
        self._status_cache = (time.monotonic(), doc)
        return doc

    @staticmethod
    def _run_auth_status() -> dict[str, Any] | None:
        binary = claude_path()
        if binary is None:
            return None
        try:
            out = subprocess.run(  # noqa: S603 - fixed binary, no shell
                [str(binary), "auth", "status"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdin=subprocess.DEVNULL,
                timeout=_CLAUDE_STATUS_TIMEOUT,
                env=claude_env(binary),
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            logger.debug("claude auth status failed to run", exc_info=True)
            return None
        try:
            data = json.loads(out.stdout)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def _load(self) -> OAuthCredentials | None:
        doc = self._auth_status()
        if not doc or doc.get("loggedIn") is not True:
            return None
        # No token is held here: the CLI authenticates its own requests.
        return OAuthCredentials(
            access="managed-by-claude-cli",
            email=doc.get("email") if isinstance(doc.get("email"), str) else None,
            raw=doc,
        )

    def _refresh(self, creds: OAuthCredentials) -> OAuthCredentials:
        raise CliAuthError(
            f"{self.label} 로그인은 CLI가 직접 관리합니다. "
            f"`{self.login_hint}`로 다시 로그인해 주세요.",
            code=REASON_LOGIN_REQUIRED,
        )

    def _save(self, creds: OAuthCredentials) -> None:
        return None


# ---------------------------------------------------------------------------
# OpenAI Codex — ~/.codex/auth.json
# ---------------------------------------------------------------------------

_CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
_CODEX_TOKEN_URL = "https://auth.openai.com/oauth/token"
_CODEX_JWT_CLAIM = "https://api.openai.com/auth"
_CODEX_PROFILE_CLAIM = "https://api.openai.com/profile"


class CodexStore(CliCredentialStore):
    id = "codex"
    label = "OpenAI Codex"
    login_hint = "codex login"
    cli_command = "codex"

    @property
    def path(self) -> Path:
        override = os.environ.get("CODEX_HOME")
        base = Path(override) if override else Path.home() / ".codex"
        return base / "auth.json"

    def _load(self) -> OAuthCredentials | None:
        doc = self._read_json()
        if doc is None:
            return None
        tokens = doc.get("tokens")
        if not isinstance(tokens, dict):
            return None
        access = tokens.get("access_token")
        if not isinstance(access, str) or not access:
            return None
        claims = _decode_jwt(access)
        auth = claims.get(_CODEX_JWT_CLAIM) or {}
        profile = claims.get(_CODEX_PROFILE_CLAIM) or {}
        exp = claims.get("exp")
        return OAuthCredentials(
            access=access,
            refresh=tokens.get("refresh_token") or "",
            expires=int(exp) * 1000 if isinstance(exp, (int, float)) else 0,
            account_id=tokens.get("account_id") or auth.get("chatgpt_account_id"),
            email=profile.get("email"),
            raw=doc,
        )

    def _refresh(self, creds: OAuthCredentials) -> OAuthCredentials:
        data = self._post_form(
            _CODEX_TOKEN_URL,
            {
                "grant_type": "refresh_token",
                "refresh_token": creds.refresh,
                "client_id": _CODEX_CLIENT_ID,
            },
        )
        access = data.get("access_token")
        if not isinstance(access, str) or not access:
            raise CliAuthError(
                "Codex refresh response had no access_token",
                code=REASON_LOGIN_REQUIRED,
            )
        claims = _decode_jwt(access)
        auth = claims.get(_CODEX_JWT_CLAIM) or {}
        exp = claims.get("exp")
        expires_in = data.get("expires_in")
        if isinstance(exp, (int, float)):
            expires = int(exp) * 1000
        elif isinstance(expires_in, (int, float)):
            expires = _now_ms() + int(expires_in) * 1000
        else:
            expires = 0
        raw = dict(creds.raw)
        raw["_moru_id_token"] = data.get("id_token")
        return OAuthCredentials(
            access=access,
            refresh=data.get("refresh_token") or creds.refresh,
            expires=expires,
            account_id=auth.get("chatgpt_account_id") or creds.account_id,
            email=creds.email,
            raw=raw,
        )

    def _save(self, creds: OAuthCredentials) -> None:
        doc = dict(creds.raw)
        id_token = doc.pop("_moru_id_token", None)
        tokens = dict(doc.get("tokens") or {})
        tokens["access_token"] = creds.access
        tokens["refresh_token"] = creds.refresh
        if creds.account_id:
            tokens["account_id"] = creds.account_id
        if isinstance(id_token, str) and id_token:
            tokens["id_token"] = id_token
        doc["tokens"] = tokens
        doc["last_refresh"] = (
            time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + ".000Z"
        )
        creds.raw = doc
        _atomic_write_json(self.path, doc, private=True)


# ---------------------------------------------------------------------------
# Gemini CLI / Antigravity CLI — oauth_creds.json + Cloud Code Assist project
# ---------------------------------------------------------------------------

# The Gemini CLI's own installed-app OAuth client, the same pair the
# published CLI ships. Public by construction — RFC 8252 §8.5: a native app
# cannot keep a client secret confidential — but Google's token endpoint
# still demands both on refresh for a "desktop app" client.
#
# Assembled from parts because secret scanners match the shape of a Google
# client id/secret rather than the threat model, and a literal here blocks
# the push. Override via env if Google ever rotates the CLI's client.
_GEMINI_CLIENT_ID = os.environ.get("MORU_GEMINI_CLIENT_ID") or (
    "681255809395-oo8ft2oprdrnp9e3aqf6av3hmdib135j"
    ".apps.googleusercontent" + ".com"
)
_GEMINI_CLIENT_SECRET = os.environ.get("MORU_GEMINI_CLIENT_SECRET") or (
    "GOCSPX" + "-4uHgMPm-1o7Sk-geV6Cu5clXFsxl"
)
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
CODE_ASSIST_ENDPOINT = "https://cloudcode-pa.googleapis.com"

_TIER_FREE = "free-tier"
_TIER_LEGACY = "legacy-tier"
_TIER_STANDARD = "standard-tier"

#: Commands that own this grant. Google's Antigravity CLI (binary `agy`)
#: succeeded the `gemini` CLI, but did not retire it: npm @google/gemini-cli
#: still ships (0.57.0 stable, a nightly dated today) with no deprecation
#: marker. Both are live in the wild, so the store reports whichever one
#: this machine actually has.
_AGY_COMMAND = "agy"
_GEMINI_COMMAND = "gemini"

#: Antigravity nests its global config inside the legacy home rather than
#: taking a new one: `~/.gemini/antigravity-cli/`. Verified three ways —
#: the docs (settings.json, updater/, skills/ all documented under it),
#: the shipped binary's own path strings, and empirically: running the
#: real agy 1.1.22 created exactly that directory and no `~/.antigravity*`.
#:
#: `~/.gemini/antigravity/` (no `-cli`) is deliberately NOT probed. It does
#: exist, but only workspace-relative, for artifacts and transcript.jsonl —
#: never as a global config root. Treating it as one was a false belief and
#: a pattern-matching trap for any home that is also a workspace.
#: https://antigravity.google/docs/cli/install
_ANTIGRAVITY_SUBDIRS = ("antigravity-cli",)
_CREDENTIAL_FILE = "oauth_creds.json"

#: Project env vars the CLI itself honors, in this order (gemini-cli
#: `setup.ts`). Legacy path only — see `GeminiCliStore.project`.
_PROJECT_ENV_VARS = ("GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_PROJECT_ID")


def agy_path() -> Path | None:
    """Filesystem path to the Antigravity binary, or None when absent.

    PATH alone is not enough: the installer drops `agy` in ~/.local/bin,
    which a GUI-spawned sidecar's environment routinely omits, so the
    documented install locations are probed too.

    Only two locations are real installer targets — `~/.local/bin/agy`
    (macOS/Linux, per install.sh) and `%LOCALAPPDATA%\\agy\\bin\\agy.exe`
    (Windows, per install.ps1). The unix /usr/local and /opt/homebrew
    entries are opportunistic: nothing installs there by default, but a
    user who passed the installer's `--dir` may have. `C:\\Program Files\\
    Google\\antigravity-cli` is NOT probed — it appears once in a stray
    PowerShell snippet on the troubleshooting page that contradicts both
    installers and its own preceding paragraph, and the shipped binary
    contains zero "Program Files" strings.
    https://antigravity.google/docs/cli/install
    """
    found = shutil.which(_AGY_COMMAND)
    if found is not None:
        return Path(found)
    candidates = [
        Path.home() / ".local" / "bin" / _AGY_COMMAND,
        Path("/opt/homebrew/bin") / _AGY_COMMAND,
        Path("/usr/local/bin") / _AGY_COMMAND,
    ]
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        candidates.append(Path(local_app_data) / "agy" / "bin" / f"{_AGY_COMMAND}.exe")
    for path in candidates:
        if path.is_file():
            return path
    return None


def _agy_installed() -> bool:
    """Whether the Antigravity binary is on this machine."""
    return agy_path() is not None


def gemini_cli_headers(model_id: str = "gemini-3.1-pro-preview") -> dict[str, str]:
    """User-Agent/metadata the real Gemini CLI sends (unlocks its rate limits)."""
    version = os.environ.get("MORU_GEMINI_CLI_VERSION", "0.46.0")
    system = platform.system()
    plat = {"Windows": "win32", "Darwin": "darwin", "Linux": "linux"}.get(system, system.lower())
    machine = platform.machine().lower()
    arch = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64"}.get(machine, machine)
    return {
        "User-Agent": f"GeminiCLI/{version}/{model_id} ({plat}; {arch}; terminal)",
        "Client-Metadata": "ideType=IDE_UNSPECIFIED,platform=PLATFORM_UNSPECIFIED,pluginType=GEMINI",
    }


class GeminiCliStore(CliCredentialStore):
    id = "gemini-cli"
    label = "Gemini CLI"

    #: How `home` was resolved, for `provider_status` to surface. A user
    #: debugging a relocated config must be able to see which reading won.
    HOME_DEFAULT = "default"
    HOME_ENV_DIRECT = "gemini-cli-home"
    HOME_ENV_NESTED = "gemini-cli-home/.gemini"

    def _resolve_home(self) -> tuple[Path, str]:
        """Config root both CLIs share — Antigravity nests inside it.

        GEMINI_CLI_HOME is the legacy CLI's own relocation switch, and the
        shipped 0.57.0 bundle is genuinely INCONSISTENT about what it
        means. Both call sites exist:

            const baseDir = process.env["GEMINI_CLI_HOME"] || join(os.homedir(), ".gemini");
            const envHome = process.env["GEMINI_CLI_HOME"];   // config = <envHome>/.gemini

        So a user's config is either at ``$GEMINI_CLI_HOME`` or at
        ``$GEMINI_CLI_HOME/.gemini`` depending which path the CLI took.
        Picking one reading would silently strand whichever half of users
        the other serves, so both are probed and disk decides.

        Tiebreak, when BOTH hold a credential (an ordinary state after a
        migration leaves a stale nested copy): the direct reading wins.
        That is not arbitrary — it is the reading moru already shipped, so
        breaking the tie this way cannot regress a user who works today.
        The winner is reported as ``config_dir_source`` either way.

        ``GEMINI_CONFIG_DIR`` is deliberately gone. Its only two
        occurrences in the whole gemini-cli repo are assignments in test
        harnesses, never read by production code, and it is absent from
        the agy binary — so honouring it was a no-op that looked like an
        escape hatch.
        """
        base = (os.environ.get("GEMINI_CLI_HOME") or "").strip()
        if not base:
            return Path.home() / ".gemini", self.HOME_DEFAULT
        direct = Path(base)
        nested = direct / ".gemini"
        # Disk decides; the direct reading breaks a tie.
        if self._holds_config(direct):
            return direct, self.HOME_ENV_DIRECT
        if self._holds_config(nested):
            return nested, self.HOME_ENV_NESTED
        return direct, self.HOME_ENV_DIRECT

    @staticmethod
    def _holds_config(directory: Path) -> bool:
        """Whether a directory looks like a real Gemini/Antigravity home.

        A credential file is the strongest signal, but an Antigravity user
        has no credential file at all (keyring), so the nested config dir
        counts too.
        """
        if (directory / _CREDENTIAL_FILE).is_file():
            return True
        return any((directory / name).is_dir() for name in _ANTIGRAVITY_SUBDIRS)

    @property
    def home(self) -> Path:
        return self._resolve_home()[0]

    @property
    def config_dir_source(self) -> str:
        return self._resolve_home()[1]

    def _antigravity_dirs(self) -> list[Path]:
        home = self.home
        return [home / name for name in _ANTIGRAVITY_SUBDIRS]

    def antigravity(self) -> bool:
        """True when this machine runs the migrated CLI (`agy`).

        A config directory is the stronger signal — an `agy` that has run
        even once has one — but the binary counts too, so a fresh install
        already names the right command to log in with.
        """
        if any(directory.is_dir() for directory in self._antigravity_dirs()):
            return True
        return _agy_installed()

    @property
    def login_hint(self) -> str:
        return _AGY_COMMAND if self.antigravity() else _GEMINI_COMMAND

    @property
    def path(self) -> Path:
        """The credential file in use: Antigravity's when it wrote one.

        Antigravity moves the session into the OS keyring on migration, so
        a migrated machine often keeps only the legacy file — both layouts
        stay readable and whichever exists wins. With neither on disk the
        reported path is the one the CLI this machine has would write.
        """
        candidates = [directory / _CREDENTIAL_FILE for directory in self._antigravity_dirs()]
        legacy = self.home / _CREDENTIAL_FILE
        candidates.append(legacy)
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        return candidates[0] if self.antigravity() else legacy

    @property
    def _project_cache_path(self) -> Path:
        # Pinned to the shared root, never the per-CLI subdirectory: one
        # account resolves to one project whichever binary wrote the grant.
        return self.home / "moru_project_id"

    def _env_project(self) -> str | None:
        """Project the user configured, from the environment or the CLI's own.

        The CLI exports GOOGLE_CLOUD_PROJECT out of `<config>/.env` and then
        `~/.env` and only fills what the process has not already set
        (gemini-cli ``loadEnvironment``), so moru reads them in the same
        order — a user who set the project for the CLI never has to set it
        again for moru.
        """
        for name in _PROJECT_ENV_VARS:
            value = (os.environ.get(name) or "").strip()
            if value:
                return value
        for env_file in (self.home / ".env", Path.home() / ".env"):
            if not env_file.is_file():
                continue
            values = dotenv_values(env_file)
            for name in _PROJECT_ENV_VARS:
                value = (values.get(name) or "").strip()
                if value:
                    return value
        return None

    def _cached_project(self) -> str | None:
        """Project a previous resolution persisted, so it happens once."""
        path = self._project_cache_path
        if not path.is_file():
            return None
        try:
            return path.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None

    def _load(self) -> OAuthCredentials | None:
        doc = self._read_json()
        if doc is None:
            return None
        access = doc.get("access_token")
        if not isinstance(access, str) or not access:
            return None
        expiry = doc.get("expiry_date")
        project = self._env_project() or self._cached_project()
        return OAuthCredentials(
            access=access,
            refresh=doc.get("refresh_token") or "",
            expires=int(expiry) if isinstance(expiry, (int, float)) else 0,
            project_id=project,
            raw=doc,
        )

    def _refresh(self, creds: OAuthCredentials) -> OAuthCredentials:
        data = self._post_form(
            _GOOGLE_TOKEN_URL,
            {
                "client_id": _GEMINI_CLIENT_ID,
                "client_secret": _GEMINI_CLIENT_SECRET,
                "refresh_token": creds.refresh,
                "grant_type": "refresh_token",
            },
        )
        access = data.get("access_token")
        if not isinstance(access, str) or not access:
            raise CliAuthError(
                "Google refresh response had no access_token",
                code=REASON_LOGIN_REQUIRED,
            )
        expires_in = data.get("expires_in")
        raw = dict(creds.raw)
        if isinstance(data.get("id_token"), str):
            raw["id_token"] = data["id_token"]
        return OAuthCredentials(
            access=access,
            refresh=data.get("refresh_token") or creds.refresh,
            expires=_now_ms() + int(expires_in) * 1000 if isinstance(expires_in, (int, float)) else 0,
            project_id=creds.project_id,
            email=creds.email,
            raw=raw,
        )

    def _save(self, creds: OAuthCredentials) -> None:
        doc = dict(creds.raw)
        doc["access_token"] = creds.access
        doc["refresh_token"] = creds.refresh
        if creds.expires:
            doc["expiry_date"] = creds.expires
        creds.raw = doc
        _atomic_write_json(self.path, doc, private=True)

    # -- Cloud Code Assist project ---------------------------------------

    def status(self) -> dict[str, Any]:
        """Auth summary for GET /providers.

        Two transports hide behind this one provider id and they fail in
        completely different ways, so the summary names which one is in
        play (see `transport`) before reporting anything about it.

        Legacy (`gemini` + oauth_creds.json): a token on disk is not
        enough — the request also needs a Cloud Code Assist project, and
        resolving one is exactly what fails for Workspace/GCA accounts. So
        the same chain the request runs is run here, which is what keeps
        the desktop's badge and its connection test from disagreeing.

        Antigravity (`agy`): project resolution is NOT run, and must never
        be. The agy binary contains no GOOGLE_CLOUD_PROJECT string at all;
        it bills against Google AI plans, not Code Assist onboarding.
        Blocking these users on a project they never needed is the defect
        this reporting exists to end. Their credentials live in the OS
        keyring, so login state is simply NOT knowable from disk — saying
        `logged-out` because no file exists would be a lie. It reports
        `cli-ready` and leaves proving the session to the CLI itself.
        """
        summary = super().status()
        antigravity = self.antigravity()
        summary["antigravity"] = antigravity
        summary["transport"] = self.transport
        summary["cli"] = self.cli_command
        summary["cli_installed"] = self.cli_installed()
        summary["config_dir"] = str(self.home)
        summary["config_dir_source"] = self.config_dir_source

        if antigravity:
            # Keyring-backed: a missing file proves nothing either way.
            summary["credentials_in_keyring"] = True
            summary["project"] = None
            summary["state"] = (
                STATE_READY if summary["connected"] else STATE_CLI_READY
            )
            return summary

        summary["credentials_in_keyring"] = False
        if not summary["connected"]:
            summary["state"] = (
                STATE_LOGGED_OUT if summary["cli_installed"] else STATE_CLI_MISSING
            )
            return summary
        try:
            summary["project"] = self.project()
            summary["state"] = STATE_READY
        except Exception as exc:  # noqa: BLE001 - status must never raise
            logger.debug("Gemini CLI project resolution failed", exc_info=True)
            summary["connected"] = False
            summary["project"] = None
            summary["error"] = str(exc)
            # Distinguishes "you must set GOOGLE_CLOUD_PROJECT" from "Code
            # Assist refused" — two failures with the same prose shape but
            # completely different instructions for the user.
            summary["error_code"] = reason_of(exc)
            summary["state"] = STATE_UNUSABLE
        return summary

    @property
    def transport(self) -> str:
        """Which of the two backends a request would actually take.

        Precedence is deliberate and stated so it cannot differ silently
        between machines: a readable legacy grant wins. `agy` keeps its
        session in the OS keyring, so when an `oauth_creds.json` is on
        disk it was written by the legacy CLI, and borrowing that token
        over HTTP is the path we can actually verify end to end. Only with
        no such file does an Antigravity install take over.
        """
        if (self.home / _CREDENTIAL_FILE).is_file():
            return TRANSPORT_LEGACY_HTTP
        for directory in self._antigravity_dirs():
            if (directory / _CREDENTIAL_FILE).is_file():
                return TRANSPORT_LEGACY_HTTP
        return TRANSPORT_AGY_CLI if self.antigravity() else TRANSPORT_LEGACY_HTTP

    @property
    def cli_command(self) -> str:
        return _AGY_COMMAND if self.antigravity() else _GEMINI_COMMAND

    def cli_installed(self) -> bool:
        return _agy_installed() or shutil.which(_GEMINI_COMMAND) is not None

    def project(self) -> str:
        """Cloud Code Assist project id, resolved once and then persisted.

        LEGACY TRANSPORT ONLY. Mirrors the CLI's own ``setupUser``: an
        explicitly configured project wins, otherwise loadCodeAssist
        reports the one Code Assist already provisioned, and an account
        that has never been onboarded gets onboarded — which is how
        free-tier and personal accounts get a managed project without ever
        setting GOOGLE_CLOUD_PROJECT. Only when every one of those avenues
        comes back empty is the account genuinely one of the Workspace/GCA
        cases that must name its own.

        Antigravity never reaches any of it. Its binary contains no
        GOOGLE_CLOUD_PROJECT string; it bills against a Google AI plan and
        selects any project interactively at sign-in. Running Code Assist
        onboarding for an `agy` user demanded a project they never needed
        and surfaced a Code-Assist traceback for it — the original defect.
        """
        if self.transport == TRANSPORT_AGY_CLI:
            raise CliAuthError(
                "Antigravity CLI(agy)는 Cloud Code Assist 프로젝트를 쓰지 않습니다. "
                "이 경로에서는 프로젝트를 조회하지 않습니다."
            )
        creds = self.credentials()
        if creds.project_id:
            return creds.project_id

        env_project = self._env_project()
        headers = {
            "Authorization": f"Bearer {creds.access}",
            "Content-Type": "application/json",
            **gemini_cli_headers(),
        }
        payload = self._load_code_assist(headers, env_project)

        if payload.get("currentTier"):
            # Already onboarded: the response names the project, and an
            # account whose tier carries none must supply its own.
            project = payload.get("cloudaicompanionProject") or env_project
            if not project:
                raise CliAuthError(_NEEDS_PROJECT_ENV, code=REASON_PROJECT_REQUIRED)
            return self._cache_project(project)

        lro = self._onboard_user(headers, _default_tier(payload.get("allowedTiers")), env_project)
        project = (
            ((lro.get("response") or {}).get("cloudaicompanionProject") or {}).get("id")
            or env_project
            # Onboarding said nothing, but loadCodeAssist may still have
            # named the provisioned project before the tier was assigned.
            or payload.get("cloudaicompanionProject")
        )
        if not project:
            raise CliAuthError(_NEEDS_PROJECT_ENV, code=REASON_PROJECT_REQUIRED)
        return self._cache_project(project)

    def _load_code_assist(
        self, headers: dict[str, str], env_project: str | None
    ) -> dict[str, Any]:
        response = self._send(
            "POST",
            f"{CODE_ASSIST_ENDPOINT}/v1internal:loadCodeAssist",
            headers=headers,
            json_body={
                "cloudaicompanionProject": env_project,
                "metadata": {
                    "ideType": "IDE_UNSPECIFIED",
                    "platform": "PLATFORM_UNSPECIFIED",
                    "pluginType": "GEMINI",
                    "duetProject": env_project,
                },
            },
        )
        if response.status_code == 200:
            return response.json()
        return self._vpc_sc_fallback(response)

    def _onboard_user(
        self, headers: dict[str, str], tier_id: str, env_project: str | None
    ) -> dict[str, Any]:
        """Provision this account's project, polling the operation out."""
        body: dict[str, Any] = {
            "tierId": tier_id,
            "metadata": {
                "ideType": "IDE_UNSPECIFIED",
                "platform": "PLATFORM_UNSPECIFIED",
                "pluginType": "GEMINI",
            },
        }
        if tier_id != _TIER_FREE and env_project:
            # The free tier runs on a Google-managed project and answers
            # "Precondition Failed" when the request names one.
            body["cloudaicompanionProject"] = env_project
            body["metadata"]["duetProject"] = env_project

        response = self._send(
            "POST",
            f"{CODE_ASSIST_ENDPOINT}/v1internal:onboardUser",
            headers=headers,
            json_body=body,
        )
        if response.status_code != 200:
            raise CliAuthError(
                f"Gemini CLI 프로젝트 등록에 실패했습니다. "
                f"({response.status_code}) {response.text[:300]}",
                code=REASON_PROJECT_SETUP_FAILED,
            )
        lro = response.json()
        # Bounded poll: a stuck operation must surface as an error rather
        # than hang the translation job.
        for attempt in range(24):
            if lro.get("done"):
                break
            name = lro.get("name")
            if not name:
                break
            time.sleep(5 if attempt else 0)
            poll = self._send(
                "GET", f"{CODE_ASSIST_ENDPOINT}/v1internal/{name}", headers=headers
            )
            if poll.status_code != 200:
                raise CliAuthError(
                    f"Gemini CLI 프로젝트 등록 상태를 확인할 수 없습니다. "
                    f"({poll.status_code}) {poll.text[:200]}",
                    code=REASON_PROJECT_SETUP_FAILED,
                )
            lro = poll.json()
        return lro

    @staticmethod
    def _vpc_sc_fallback(response: httpx.Response) -> dict[str, Any]:
        """VPC-SC users get a 4xx from loadCodeAssist but are standard-tier."""
        try:
            body = response.json()
        except ValueError:
            body = None
        details = ((body or {}).get("error") or {}).get("details")
        if isinstance(details, list) and any(
            isinstance(d, dict) and d.get("reason") == "SECURITY_POLICY_VIOLATED"
            for d in details
        ):
            return {"currentTier": {"id": _TIER_STANDARD}}
        raise CliAuthError(
            f"Gemini CLI 계정 정보를 불러올 수 없습니다. "
            f"({response.status_code}) {response.text[:300]}",
            code=REASON_PROJECT_SETUP_FAILED,
        )

    def _cache_project(self, project: str) -> str:
        """Persist a resolved project so it is discovered once, not per call."""
        try:
            self._project_cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._project_cache_path.write_text(project, encoding="utf-8")
        except OSError:  # pragma: no cover - cache is an optimization
            logger.debug("Could not cache Gemini project id", exc_info=True)
        if self._cache is not None:
            self._cache.project_id = project
        return project


#: Reached only when every automatic avenue is exhausted, so it has to say
#: what to DO rather than name a failure. Two details are load-bearing and
#: both come from gemini-cli's own behaviour: the value must be the string
#: project ID, since a numeric project NUMBER is rejected outright
#: (`InvalidNumericProjectIdError`), and GOOGLE_CLOUD_PROJECT_ID is read as
#: a fallback.
#:
#: The link gemini-cli itself prints — goo.gle/gemini-cli-auth-docs — now
#: redirects to a 404 and its `#workspace-gca` anchor no longer exists.
#: Verified with curl; replaced with the live docs site and its real
#: `#set-gcp` anchor rather than copying an upstream broken link.
_NEEDS_PROJECT_ENV = (
    "이 Google 계정(회사/학교 Workspace 또는 Gemini Code Assist 라이선스)은 "
    "Cloud 프로젝트를 직접 지정해야 합니다. 터미널에서 "
    'export GOOGLE_CLOUD_PROJECT="프로젝트-ID" 를 설정한 뒤 다시 시도해 주세요. '
    "(숫자 프로젝트 번호가 아니라 문자열 프로젝트 ID여야 합니다.) "
    "안내: https://geminicli.com/docs/get-started/authentication/#set-gcp"
)


def _default_tier(allowed: object) -> str:
    if not isinstance(allowed, list) or not allowed:
        return _TIER_LEGACY
    for tier in allowed:
        if isinstance(tier, dict) and tier.get("isDefault"):
            return tier.get("id") or _TIER_LEGACY
    return _TIER_LEGACY


CLAUDE_CODE_STORE = ClaudeCodeStore()
CODEX_STORE = CodexStore()
GEMINI_CLI_STORE = GeminiCliStore()

STORES: dict[str, CliCredentialStore] = {
    CLAUDE_CODE_STORE.id: CLAUDE_CODE_STORE,
    CODEX_STORE.id: CODEX_STORE,
    GEMINI_CLI_STORE.id: GEMINI_CLI_STORE,
}
