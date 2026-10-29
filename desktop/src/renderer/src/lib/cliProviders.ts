/**
 * Coding-CLI subscription providers in the renderer.
 *
 * These authenticate through a coding CLI the user already installed and
 * logged into — there is no API key to paste. Everything the UI says about
 * one comes off the wire: `GET /providers` marks them `auth: "cli"` and
 * carries the login command, the signed-in account, and the reason a grant
 * cannot serve a request.
 *
 * Deliberately absent from this file: provider ids, product names and login
 * commands. The engine's `CLI_PROVIDER_CATALOG` owns all three, so a fourth
 * CLI provider — or `gemini-cli` being renamed to Antigravity while it keeps
 * its id — lands with no change on this side. A login command duplicated
 * here would go stale silently, which is the same class of bug that let a
 * provider report "connected" while a real call failed.
 */
import type { TFunction } from "i18next";

import type { Provider } from "../../../shared/engine";

/** Trimmed value, or null for absent/blank — the wire uses both for "none". */
function nonEmpty(value: string | null | undefined): string | null {
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  return trimmed.length > 0 ? trimmed : null;
}

/**
 * True when the engine authenticates this provider through a coding CLI.
 *
 * `auth: "cli"` is the engine's own discriminator — it stamps it onto every
 * entry of `CLI_PROVIDER_CATALOG`, the same table whose `env` is None
 * because these never take a key. Branching on the wire rather than a local
 * id list is what keeps the two in step.
 */
export function isCliProvider(provider: Provider): boolean {
  return provider.auth === "cli";
}

export function needsAccountRiskNotice(provider: Provider): boolean {
  // Only the Claude subscription is read through Anthropic's consumer
  // plan terms; Codex and Gemini carry no such warning.
  return provider.id === "claude-code";
}

/**
 * What the user has to do next — the only thing these states differ by, and
 * the reason a boolean was never enough.
 *
 * - `ready`       — a request would succeed; nothing to do.
 * - `cli-ready`   — the CLI is installed but keeps its login somewhere we
 *                   cannot read (Antigravity uses the OS keyring). Neither
 *                   "signed in" nor "signed out" is knowable from here, so
 *                   the honest move is to try a real request.
 * - `unusable`    — signed in, but a request would still fail. Signing in
 *                   again does not fix it.
 * - `logged-out`  — CLI present, no credential. Sign in with `command`.
 * - `cli-missing` — no CLI on this machine. Install `cli` first; telling
 *                   this user to run a login command is useless.
 */
export type CliSetupState =
  | { kind: "ready"; account: string | null }
  | { kind: "cli-ready"; command: string | null }
  | { kind: "unusable"; reason: string | null }
  | { kind: "logged-out"; command: string | null; cli: string | null }
  | { kind: "cli-missing"; cli: string | null };

/**
 * The provider's setup state, taken from what the engine reports.
 *
 * `state` is the contract; `connected` is only consulted when an older
 * engine omits it, and then it can only tell the coarse story a boolean
 * can. The engine's `ready` is an honest answer — on the legacy Gemini
 * transport it resolves a Cloud Code Assist project exactly as a request
 * would, so it cannot come back true for a login that fails on first use.
 */
export function cliSetupState(provider: Provider): CliSetupState {
  const command = nonEmpty(provider.login_hint);
  const cli = nonEmpty(provider.cli);
  switch (provider.state) {
    case "ready":
      return { kind: "ready", account: nonEmpty(provider.account) };
    case "cli-ready":
      return { kind: "cli-ready", command };
    case "unusable":
      return { kind: "unusable", reason: cliFailureReason(provider.error) };
    case "logged-out":
      return { kind: "logged-out", command, cli };
    case "cli-missing":
      return { kind: "cli-missing", cli };
  }
  // Pre-`state` engine: a bool plus an error string is all there is.
  if (provider.connected ?? provider.has_key) {
    return { kind: "ready", account: nonEmpty(provider.account) };
  }
  if (nonEmpty(provider.error) !== null) {
    return { kind: "unusable", reason: cliFailureReason(provider.error) };
  }
  return { kind: "logged-out", command, cli };
}

/**
 * The status word and its tone. Every surface that shows a CLI provider's
 * standing reads it from here, which is what makes "설정 필요 in Settings,
 * 연결됨 in the translation step" structurally impossible rather than a
 * thing to remember.
 */
export function cliStatusChip(
  state: CliSetupState,
  t: TFunction,
): { label: string; tone: string } {
  switch (state.kind) {
    case "ready":
      return { label: t("settings.models.cliConnected"), tone: "text-accent" };
    case "cli-ready":
      return { label: t("settings.models.cliUnverified"), tone: "text-text2" };
    case "unusable":
      return { label: t("settings.models.cliNeedsSetup"), tone: "text-amber" };
    case "logged-out":
      return { label: t("settings.models.cliNeedsLogin"), tone: "text-text3" };
    case "cli-missing":
      return { label: t("settings.models.cliNotInstalled"), tone: "text-text3" };
  }
}

/**
 * The sentence telling the user what to do next. Shared for the same reason
 * as the chip: two screens describing one provider must not diverge.
 *
 * Every branch is actionable and none of them can print a traceback — the
 * engine's raw `str(exc)` is filtered by `cliFailureReason` first, and the
 * fallback copy takes over when nothing readable survives.
 */
export function cliGuidance(state: CliSetupState, t: TFunction): string {
  switch (state.kind) {
    case "ready":
      return t("settings.models.cliReady");
    case "cli-ready":
      // Nothing on disk proves this session either way, so the copy must
      // not claim a login state. Running a request is the only proof.
      return state.command !== null
        ? t("settings.models.cliKeyringHint", { cmd: state.command })
        : t("settings.models.cliKeyringHintNoCmd");
    case "unusable":
      return state.reason !== null
        ? t("settings.models.cliBlocked", { reason: state.reason })
        : t("settings.models.cliBlockedUnknown");
    case "logged-out":
      // `cli` is the binary ("claude"); `command` is what signs it in
      // ("claude login", or bare "agy"). They are not interchangeable.
      return state.command !== null && state.cli !== null
        ? t("settings.models.cliLoginHint", { cli: state.cli, cmd: state.command })
        : t("settings.models.cliLoginHintNoCmd");
    case "cli-missing":
      // Naming a login command here would be useless: there is nothing to
      // run it with yet.
      return state.cli !== null
        ? t("settings.models.cliMissingHint", { cli: state.cli })
        : t("settings.models.cliMissingHintNoName");
  }
}

/** A Python traceback, verbatim from the engine's `str(exc)`. */
const TRACEBACK = /Traceback \(most recent call last\)|^\s+File ".+", line \d+/m;
/** `litellm.APIConnectionError`, `KeyError`, `json.decoder.JSONDecodeError`… */
const EXCEPTION_CLASS = /(?:[A-Za-z_]\w*\.)*[A-Z]\w*(?:Error|Exception)\b/;
/** Longer than this and the reason stops being a sentence the user can act on. */
const MAX_REASON = 200;

/**
 * Engine failure text fit to show a user, or null when it is not.
 *
 * The engine hands back `str(exc)` on any unexpected failure, so the same
 * field carries either hand-written guidance ("this Google account needs
 * GOOGLE_CLOUD_PROJECT…") or a raw traceback. Prose passes through; anything
 * carrying a stack or an exception class name is dropped so the caller can
 * fall back to copy that actually tells the user what to do.
 */
export function cliFailureReason(raw: string | null | undefined): string | null {
  if (typeof raw !== "string") return null;
  if (TRACEBACK.test(raw)) return null;
  const text = raw.replace(/\s+/g, " ").trim();
  if (text.length === 0) return null;
  if (EXCEPTION_CLASS.test(text)) return null;
  return text.length > MAX_REASON ? `${text.slice(0, MAX_REASON - 1)}…` : text;
}

/**
 * Display name for a provider: a locale override when one exists, otherwise
 * the engine's own `name`.
 *
 * Keyed by id so a locale *can* override, defaulted to the engine so an id
 * no locale knows still renders — and so a rename engine-side lands without
 * a desktop release. That last part is not hypothetical: `gemini-cli` keeps
 * its id for saved-settings compatibility while its name becomes
 * Antigravity, so a table of names keyed by id would start lying.
 *
 * Overrides go under `providers.name.<id>` in any i18n file. None exist
 * today: the engine's names are product names, not prose, and read the same
 * in both locales.
 */
export function providerLabel(provider: Provider, t: TFunction): string {
  return t(`providers.name.${provider.id}`, { defaultValue: provider.name });
}
