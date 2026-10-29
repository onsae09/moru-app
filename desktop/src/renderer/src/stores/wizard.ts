/**
 * Wizard session state (W1-W6). One active translation session at a time;
 * job lifecycles (scan/translate/export) and their WS event streams are
 * driven from here so screens stay declarative.
 */

import { create } from "zustand";

import type {
  CheckpointSummary,
  JobEventFrame,
  JobSnapshot,
  PipelineStats,
  Provider,
  ScanResult,
  TranslateParams,
} from "../../../shared/engine";
import type { ModpackProbe } from "../../../shared/bridge";
import { api, EngineApiError, openJobEvents } from "../lib/api";
import { providerIdOf } from "../lib/models";
import { moru } from "../lib/bridge";
import { WEB_URL } from "../lib/web";
import {
  reconcileCheckpoints,
  useSessions,
  type SessionRecord,
  type SessionScanTotals,
} from "./sessions";
// Cycle-tolerant by construction: translationQueue imports this module too,
// and both sides only dereference the other's store at call time.
import { waitForEngineReady } from "./engine";
import { useTranslationQueue } from "./translationQueue";
import {
  snapshotTranslationSettings,
  useSettings,
  type TranslationRunSettings,
} from "./settings";

export interface TickerPair {
  key: string;
  source: string;
  translated: string;
}

export interface ActiveBatch {
  requestId: number;
  file: string;
  key: string;
  entries: number;
  startedAt: number;
}

export interface FileProgress {
  file: string;
  done: number;
  total: number;
}

export interface GlossaryProgress {
  /** extraction chunks completed / total */
  done: number;
  total: number;
  newTerms: number;
  /** attempt number currently being retried after a schema error */
  retrying: number | null;
  lastError: string | null;
}

export interface LogLine {
  ts: number;
  level: "info" | "warn" | "error";
  text: string;
}

/**
 * Hot runtime cache of session id -> engine translate job id. SessionRecord
 * also persists the id so a renderer reload can probe the still-running
 * sidecar. Every restore verifies it; a sidecar restart invalidates the id.
 */
interface SessionJobsStore {
  jobs: Record<string, string>;
  register: (sessionId: string, jobId: string) => void;
  unregister: (sessionId: string) => void;
}

export const useSessionJobs = create<SessionJobsStore>((set) => ({
  jobs: {},
  register: (sessionId, jobId) =>
    set((state) => ({ jobs: { ...state.jobs, [sessionId]: jobId } })),
  unregister: (sessionId) =>
    set((state) => {
      if (!(sessionId in state.jobs)) return state;
      const jobs = { ...state.jobs };
      delete jobs[sessionId];
      return { jobs };
    }),
}));

/**
 * Why a hand-translation seed did or did not start.
 *
 * Specific reasons rather than a flat "busy": every one of them is a refusal
 * the user has to be told about on screen. A button that silently does
 * nothing is indistinguishable from a broken app, which is exactly how the
 * queue-busy and no-pack refusals were reported.
 */
export type ManualSeedOutcome =
  | "started"
  | "noPack"
  | "runBusy"
  | "queueBusy"
  | "requestFailed";

/** Why "이어서 번역" did or did not start; each refusal is shown on screen. */
export type ResumeOutcome = "started" | "busy" | "gone" | "requestFailed";

interface WizardStore {
  /* W1 */
  sessionId: string | null;
  modpackPath: string | null;
  modpackName: string;
  probe: ModpackProbe | null;
  sourceLocale: string;
  targetLocale: string;
  migrationEnabled: boolean;
  previousModpackPath: string | null;
  previousResourcepackPath: string | null;
  previousOverridesPath: string | null;

  /* W2 */
  scanJobId: string | null;
  scanState: "idle" | "running" | "done" | "failed";
  scanProgress: { current: number; total: number; message: string };
  scanError: string | null;
  scanResult: ScanResult | null;
  /** Selected W2 totals retained when a restored run no longer has its tree. */
  scanTotals: SessionScanTotals | null;
  /** category names excluded from translation (default: all included) */
  excludedCategories: string[];

  /* W4 */
  translateJobId: string | null;
  /** Model the run was started with; settings.model may drift afterwards. */
  model: string | null;
  /**
   * `interrupted`: stopped early but resumable (provider quota, app or engine
   * shutdown). Like `cancelled`, any partial result stays reviewable.
   */
  runState: "idle" | "running" | "done" | "failed" | "cancelled" | "interrupted";
  runError: string | null;
  /** The engine keeps this run's checkpoint: "이어서 번역" can continue it. */
  resumable: boolean;
  /** Why an `interrupted` run stopped (`quota` | `shutdown`), when known. */
  interruptReason: string | null;
  startedAt: number | null;
  /** First provider batch start; excludes scan/glossary from live throughput. */
  translationStartedAt: number | null;
  finishedAt: number | null;
  doneEntries: number;
  fileProgress: Record<string, FileProgress>;
  glossaryProgress: GlossaryProgress | null;
  failedKeys: Record<string, string[]>;
  /** Total failures; failedKeys intentionally keeps only the useful tail. */
  failedEntryCount: number;
  promptTokens: number;
  completionTokens: number;
  /** prompt tokens served from the provider cache (subset of promptTokens) */
  cachedTokens: number;
  ticker: TickerPair[];
  activeBatches: Record<number, ActiveBatch>;
  log: LogLine[];
  stats: PipelineStats | null;

  /* W6 */
  exportJobId: string | null;
  exportState: "idle" | "running" | "done" | "failed";
  exportZipPath: string | null;
  exportOverridesZipPath: string | null;
  exportBilingualZipPath: string | null;
  exportBilingualOverridesZipPath: string | null;
  exportError: string | null;

  /* W3: export as source text. Its own slice, kept out of the W6 export
     state above - a source-text export must not unlock the review/export
     steps, nor overwrite the session's translated-export paths. */
  sourceExportJobId: string | null;
  sourceExportState: "idle" | "running" | "done" | "failed";
  sourceExportZipPath: string | null;
  sourceExportOverridesZipPath: string | null;
  sourceExportError: string | null;

  /* actions */
  startSession: (path: string, probe: ModpackProbe, targetLocale?: string) => void;
  resumeSession: (sessionId: string) => boolean;
  /** Reopens a finished session on W5/W6 after verifying the engine job. */
  reopenSession: (sessionId: string) => Promise<"ok" | "busy" | "gone">;
  setTargetLocale: (locale: string) => void;
  setMigrationEnabled: (enabled: boolean) => void;
  setMigrationInput: (
    kind: "modpack" | "resourcepack" | "overrides",
    path: string | null,
  ) => void;
  toggleCategory: (name: string, included: boolean) => void;
  setCategories: (names: string[], included: boolean) => void;
  startScan: () => Promise<void>;
  startTranslate: (settingsOverride?: TranslationRunSettings) => Promise<void>;
  /**
   * Continue an interrupted, cancelled or failed session from the engine's
   * checkpoint: already-translated entries are reused, so only the rest is
   * sent (and billed). Only ever started by a user click — nothing resumes
   * paid work on its own.
   */
  resumeTranslate: (sessionId: string) => Promise<ResumeOutcome>;
  /**
   * Seed a hand-translation session: a translate job flagged `manual_seed`,
   * which the engine settles as untranslated instead of calling a provider.
   * Drives the same `runState` as an ordinary run, so W5/W5M/W6 and the
   * history record need no special case.
   */
  startManualSeed: () => Promise<ManualSeedOutcome>;
  handleTranslationFrame: (frame: JobEventFrame, sessionId: string) => void;
  cancelTranslate: () => Promise<void>;
  updateReviewStats: (stats: PipelineStats) => void;
  startExport: () => Promise<void>;
  /** W3 source-text export: same engine export job, untranslated values. */
  startSourceExport: () => Promise<void>;
  appendLog: (level: LogLine["level"], text: string) => void;
  reset: () => void;
}

/** Entry volume of the current selection, from the enriched scan result. */
export function selectedScanTotals(state: {
  scanResult: ScanResult | null;
  excludedCategories: string[];
  scanTotals?: SessionScanTotals | null;
}): SessionScanTotals {
  if (state.scanResult === null && state.scanTotals != null) {
    return state.scanTotals;
  }
  let files = 0;
  let entries = 0;
  let chars = 0;
  let migrationEntries = 0;
  let migrationChars = 0;
  if (state.scanResult !== null) {
    for (const cat of state.scanResult.categories) {
      if (state.excludedCategories.includes(cat.name)) continue;
      files += cat.file_count;
      entries += cat.entry_count;
      chars += cat.char_count;
      migrationEntries += cat.files.reduce(
        (sum, file) => sum + (file.migration_entry_count ?? 0),
        0,
      );
      migrationChars += cat.files.reduce(
        (sum, file) => sum + (file.migration_char_count ?? 0),
        0,
      );
    }
  }
  return {
    files,
    entries,
    chars,
    migrationEntries,
    migrationChars,
    translationEntries: Math.max(0, entries - migrationEntries),
    translationChars: Math.max(0, chars - migrationChars),
  };
}

type TranslateParamState = Pick<
  WizardStore,
  | "sourceLocale"
  | "targetLocale"
  | "migrationEnabled"
  | "previousModpackPath"
  | "previousResourcepackPath"
  | "previousOverridesPath"
  | "scanJobId"
  | "scanResult"
  | "excludedCategories"
> & { modpackPath: string };

/** Pure request builder shared by manual and queued ordinary translations. */
export function buildTranslateParams(
  state: TranslateParamState,
  settings: TranslationRunSettings,
  apiKey?: string,
): TranslateParams {
  const providerId = providerIdOf(settings.model);
  const apiBase =
    providerId === "ollama"
      ? settings.ollamaBaseUrl
      : providerId === "openai-compatible"
        ? settings.openaiCompatBaseUrl
        : undefined;
  return {
    modpack_path: state.modpackPath,
    source_locale: state.sourceLocale,
    target_locale: state.targetLocale,
    model: settings.model,
    api_key: apiKey,
    api_base: apiBase,
    temperature: settings.temperature,
    batch_size: settings.batchSize,
    max_concurrent: settings.maxConcurrent,
    max_refine: settings.maxRefine,
    reasoning_effort: settings.thinkingEnabled ? settings.thinkingEffort : undefined,
    // Translation style. Every one of these is omitted at its default so an
    // untouched run posts byte-identical params to before they existed —
    // same idiom as `reasoning_effort` above.
    //
    // `speech_level` is additionally gated on a Korean target because the
    // engine renders that block only for `target_lang.startswith("ko")`
    // (`dspy_modules/signatures.py`). Mirroring the gate here means a value
    // the user picked for Korean can never ride along into a Japanese run
    // and misreport what was asked for.
    speech_level:
      state.targetLocale.startsWith("ko") && settings.speechLevel !== "auto"
        ? settings.speechLevel
        : undefined,
    term_style: settings.termStyle !== "auto" ? settings.termStyle : undefined,
    use_tm: settings.useTm,
    use_vanilla_glossary: settings.useVanillaGlossary,
    extract_glossary: settings.extractGlossary,
    glossary_max_terms: settings.glossaryMaxTerms,
    include_categories: includedCategories(state),
    // A per-mod axis, frozen with the rest of the run settings so a queued
    // pack uses the blacklist that was in force when the queue started.
    mod_blacklist: settings.modBlacklist,
    output_dir: settings.outputDir ?? undefined,
    // Reuse the completed W2 scan through the official v1.0 session pipeline.
    // Migration scans receive an additional A/B/C fingerprint check server-side.
    scan_job_id: state.scanJobId ?? undefined,
    ...(state.migrationEnabled && state.previousModpackPath !== null
      ? { previous_modpack_path: state.previousModpackPath }
      : {}),
    ...(state.migrationEnabled && state.previousResourcepackPath !== null
      ? { previous_resourcepack_path: state.previousResourcepackPath }
      : {}),
    ...(state.migrationEnabled && state.previousOverridesPath !== null
      ? { previous_overrides_path: state.previousOverridesPath }
      : {}),
  };
}

/**
 * Scan categories still in translation scope, in the engine's param shape:
 * undefined means "every category", which is what an untouched selection
 * sends. Shared by the translate run and the source-text export so both
 * cover exactly the categories picked on W2.
 */
export function includedCategories(state: {
  scanResult: ScanResult | null;
  excludedCategories: string[];
}): string[] | undefined {
  if (state.excludedCategories.length === 0 || state.scanResult === null) {
    return undefined;
  }
  return state.scanResult.categories
    .map((category) => category.name)
    .filter((name) => !state.excludedCategories.includes(name));
}

let closeScanEvents: (() => void) | null = null;
let closeTranslateEvents: (() => void) | null = null;
let closeExportEvents: (() => void) | null = null;
let closeSourceExportEvents: (() => void) | null = null;
let lastSessionProgressPersistedAt = 0;

const MAX_LOG_LINES = 500;
const MAX_TICKER = 24;
const MAX_FAILED_KEY_SAMPLES = 50;

function attachTranslationEvents(jobId: string, sessionId: string, after?: number): void {
  closeTranslateEvents?.();
  closeTranslateEvents = openJobEvents(
    jobId,
    (frame) => useWizard.getState().handleTranslationFrame(frame, sessionId),
    (_event, manuallyClosed) => {
      const state = useWizard.getState();
      if (
        manuallyClosed ||
        state.sessionId !== sessionId ||
        state.translateJobId !== jobId ||
        state.runState !== "running"
      ) {
        return;
      }
      state.appendLog("warn", "event stream disconnected; restoring current engine state");
      window.setTimeout(() => {
        // A crashed engine is restarting: every probe would fail meanwhile
        // and settle a resumable run as "failed". Ask once it answers; if it
        // never comes back, leave the row for the next launch to reconcile.
        void waitForEngineReady(ENGINE_RECOVERY_TIMEOUT_MS).then((ready) => {
          const current = useWizard.getState();
          if (
            ready &&
            current.sessionId === sessionId &&
            current.translateJobId === jobId &&
            current.runState === "running"
          ) {
            void current.reopenSession(sessionId);
          }
        });
      }, 500);
    },
    after,
  );
}

/** The engine's checkpoint for a session whose run is no longer live. */
/**
 * Model a resume continues on: the run's own, unless its provider refused
 * the login (`auth`) or is switched off now — then the settings' current
 * choice, provided that one is usable.
 */
async function resumeModel(
  original: string,
  interruptReason: string | null,
  current: string,
): Promise<string> {
  if (current === original) return original;
  let providers: Provider[] = [];
  try {
    providers = await api.providers();
  } catch {
    return original;
  }
  const disabled = (model: string): boolean =>
    providers.some((p) => p.id === providerIdOf(model) && p.disabled === true);
  if (disabled(current)) return original;
  return interruptReason === "auth" || disabled(original) ? current : original;
}

/** How long a dropped event stream waits for a restarting engine. */
const ENGINE_RECOVERY_TIMEOUT_MS = 90_000;

/** A start or resume is between its checks and `runState: "running"`. */
let translateLaunching = false;

async function findCheckpoint(sessionId: string): Promise<CheckpointSummary | null> {
  try {
    const checkpoints = await api.checkpoints();
    return (
      checkpoints.find(
        (checkpoint) => checkpoint.session_id === sessionId && checkpoint.state !== "running",
      ) ?? null
    );
  } catch {
    return null;
  }
}

/**
 * Fold the engine's resumable checkpoints into history once the engine is
 * up — before the queue reattaches its rows — so a translation the app was
 * closed or killed in reads as interrupted with its saved progress, never
 * as a lost failure. Only marks rows resumable; resuming is always a click.
 */
export async function reconcileResumableSessions(): Promise<void> {
  let checkpoints: CheckpointSummary[];
  try {
    checkpoints = await api.checkpoints();
  } catch {
    return; // history stays as it was; nothing is lost by skipping this
  }
  useSessions.setState((state) => ({
    sessions: reconcileCheckpoints(state.sessions, checkpoints),
  }));
}

/**
 * Delete a history row everywhere it lives. The engine keeps the session
 * file and its translation checkpoint, and the next reconcile re-adds a row
 * for every checkpoint it still holds — dropping only the local record would
 * bring a resumable session straight back.
 */
export async function deleteHistorySession(
  sessionId: string,
): Promise<"deleted" | "running" | "failed"> {
  const jobId = useSessionJobs.getState().jobs[sessionId] ?? sessionId;
  try {
    await api.deleteSession(jobId);
  } catch (error) {
    // 404: the engine never had (or already dropped) it — only the row is left.
    // Anything else leaves the row: removing it locally while the engine
    // keeps the session would bring it back on the next reconcile.
    if (!(error instanceof EngineApiError && error.status === 404)) {
      return error instanceof EngineApiError && error.status === 409 ? "running" : "failed";
    }
  }
  useSessions.getState().remove(sessionId);
  useSessionJobs.getState().unregister(sessionId);
  return "deleted";
}

function withLog(
  log: LogLine[],
  level: LogLine["level"],
  text: string,
  ts: number,
): LogLine[] {
  if (log.at(-1)?.text === text) return log;
  return [...log.slice(-MAX_LOG_LINES + 1), { ts, level, text }];
}

/** Pure event reducer shared by first-run streaming and snapshot restoration. */
function translationFramePatch(
  prev: WizardStore,
  frame: JobEventFrame,
): Partial<WizardStore> {
  const ts = frame.emitted_at ?? Date.now();
  switch (frame.type) {
    case "progress": {
      const file = frame.file;
      if (frame.stage === "translate" && file !== undefined) {
        const done = frame.done ?? 0;
        const total = frame.total ?? 0;
        const previousDone = prev.fileProgress[file]?.done ?? 0;
        return {
          fileProgress: {
            ...prev.fileProgress,
            [file]: { file, done, total },
          },
          doneEntries: Math.max(0, prev.doneEntries - previousDone + done),
        };
      }
      return { log: withLog(prev.log, "info", `stage: ${frame.stage}`, ts) };
    }
    case "batch_started":
      return {
        translationStartedAt: prev.translationStartedAt ?? ts,
        activeBatches: {
          ...prev.activeBatches,
          [frame.request_id]: {
            requestId: frame.request_id,
            file: frame.file,
            key: frame.key,
            entries: frame.entries,
            startedAt: ts,
          },
        },
      };
    case "batch_finished": {
      const activeBatches = { ...prev.activeBatches };
      delete activeBatches[frame.request_id];
      return { activeBatches };
    }
    case "tokens":
      return {
        promptTokens: frame.prompt_tokens,
        completionTokens: frame.completion_tokens,
        ...(frame.cached_tokens !== undefined ? { cachedTokens: frame.cached_tokens } : {}),
      };
    case "entry_done":
      return {
        ticker: [
          { key: frame.key, source: frame.source, translated: frame.translated },
          ...prev.ticker,
        ].slice(0, MAX_TICKER),
      };
    case "entry_failed": {
      const failedKeys = { ...prev.failedKeys };
      // Keys are only unique within a file, so a bare key collapses distinct
      // failures (every KubeJS script mints display_name.0001) into one row.
      failedKeys[frame.file !== undefined ? `${frame.file}:${frame.key}` : frame.key] =
        frame.errors;
      while (Object.keys(failedKeys).length > MAX_FAILED_KEY_SAMPLES) {
        const oldest = Object.keys(failedKeys)[0];
        if (oldest === undefined) break;
        delete failedKeys[oldest];
      }
      return {
        failedKeys,
        failedEntryCount: prev.failedEntryCount + 1,
        log: withLog(
          prev.log,
          "warn",
          `entry failed: ${frame.key} — ${frame.errors.join("; ")}`,
          ts,
        ),
      };
    }
    case "glossary_progress": {
      const failedAttempt = frame.error !== undefined;
      const patch: Partial<WizardStore> = {
        glossaryProgress: {
          done: frame.done,
          total: frame.total,
          newTerms: frame.new_terms,
          retrying: failedAttempt && !frame.skipped ? (frame.attempt ?? 1) : null,
          lastError: failedAttempt ? (frame.error ?? null) : null,
        },
      };
      if (failedAttempt) {
        patch.log = withLog(
          prev.log,
          "warn",
          `glossary chunk ${frame.chunk ?? frame.done + 1}/${frame.total} ` +
            (frame.skipped ? "skipped" : `retry ${frame.attempt}`) +
            `: ${frame.error}`,
          ts,
        );
      }
      return patch;
    }
    case "glossary_extracted":
      return {
        log: withLog(prev.log, "info", `glossary: +${frame.new_terms} terms extracted`, ts),
      };
    case "done":
      return {
        runState: "done",
        runError: null,
        resumable: false,
        interruptReason: null,
        finishedAt: ts,
        activeBatches: {},
        stats: frame.stats ?? null,
        log: withLog(prev.log, "info", "translate done", ts),
      };
    case "failed": {
      const error = frame.error ?? "translate failed";
      return {
        runState: "failed",
        runError: error,
        resumable: frame.resumable ?? false,
        interruptReason: null,
        finishedAt: ts,
        activeBatches: {},
        log: withLog(prev.log, "error", error, ts),
      };
    }
    case "cancelled":
      return {
        runState: "cancelled",
        resumable: frame.resumable ?? false,
        interruptReason: null,
        finishedAt: ts,
        activeBatches: {},
        stats: frame.stats ?? null,
        log: withLog(prev.log, "warn", "translate cancelled; partial result preserved", ts),
      };
    case "interrupted": {
      const error = frame.error ?? "translate interrupted";
      return {
        runState: "interrupted",
        runError: error,
        resumable: frame.resumable ?? false,
        interruptReason: frame.reason ?? null,
        finishedAt: ts,
        activeBatches: {},
        stats: frame.stats ?? null,
        log: withLog(
          prev.log,
          "warn",
          frame.detail !== undefined ? `${error} (${frame.detail})` : error,
          ts,
        ),
      };
    }
  }
}

const initialJobState = {
  scanJobId: null,
  scanState: "idle" as const,
  scanProgress: { current: 0, total: 0, message: "" },
  scanError: null,
  scanResult: null,
  scanTotals: null,
  excludedCategories: [],
  translateJobId: null,
  model: null,
  runState: "idle" as const,
  runError: null,
  resumable: false,
  interruptReason: null,
  startedAt: null,
  translationStartedAt: null,
  finishedAt: null,
  doneEntries: 0,
  fileProgress: {},
  glossaryProgress: null,
  failedKeys: {},
  failedEntryCount: 0,
  promptTokens: 0,
  completionTokens: 0,
  cachedTokens: 0,
  ticker: [],
  activeBatches: {},
  log: [],
  stats: null,
  exportJobId: null,
  exportState: "idle" as const,
  exportZipPath: null,
  exportOverridesZipPath: null,
  exportBilingualZipPath: null,
  exportBilingualOverridesZipPath: null,
  exportError: null,
  sourceExportJobId: null,
  sourceExportState: "idle" as const,
  sourceExportZipPath: null,
  sourceExportOverridesZipPath: null,
  sourceExportError: null,
};

export const useWizard = create<WizardStore>((set, get) => ({
  sessionId: null,
  modpackPath: null,
  modpackName: "",
  probe: null,
  sourceLocale: "en_us",
  targetLocale: useSettings.getState().targetLocale,
  migrationEnabled: false,
  previousModpackPath: null,
  previousResourcepackPath: null,
  previousOverridesPath: null,
  ...initialJobState,

  startSession: (path, probe, targetLocale) => {
    closeScanEvents?.();
    closeTranslateEvents?.();
    closeExportEvents?.();
    closeSourceExportEvents?.();
    set({
      sessionId: crypto.randomUUID(),
      modpackPath: path,
      modpackName: probe.name,
      probe,
      targetLocale: targetLocale ?? useSettings.getState().targetLocale,
      migrationEnabled: false,
      previousModpackPath: null,
      previousResourcepackPath: null,
      previousOverridesPath: null,
      ...initialJobState,
    });
    useSettings.getState().rememberFolder(path);
  },

  resumeSession: (sessionId) => {
    // Engine jobs are in-memory: resuming a session from a previous app run
    // restores its metadata so the wizard can re-run from W1/W3.
    const record = useSessions.getState().sessions.find((s) => s.id === sessionId);
    if (record === undefined) return false;
    if (get().sessionId === sessionId) return true; // live in this run
    set({
      sessionId: record.id,
      modpackPath: record.modpackPath,
      modpackName: record.modpackName,
      probe: null,
      sourceLocale: record.sourceLocale,
      targetLocale: record.targetLocale,
      migrationEnabled: false,
      previousModpackPath: null,
      previousResourcepackPath: null,
      previousOverridesPath: null,
      ...initialJobState,
    });
    return true;
  },

  reopenSession: async (sessionId) => {
    const state = get();
    const record = useSessions.getState().sessions.find((s) => s.id === sessionId);
    if (record === undefined || record.status === "failed") return "gone";
    if (
      state.scanState === "running" ||
      state.exportState === "running" ||
      state.sourceExportState === "running" ||
      (state.sessionId !== sessionId && state.runState === "running")
    ) {
      return "busy";
    }
    const candidates = [
      record.translateJobId,
      useSessionJobs.getState().jobs[sessionId],
      state.sessionId === sessionId ? state.translateJobId : null,
      sessionId,
    ].filter((value, index, values): value is string =>
      value !== null && value !== undefined && values.indexOf(value) === index,
    );

    const probe = async (jobId: string): Promise<JobSnapshot> => {
      const value = await api.jobSnapshot(jobId);
      if (value.job.type !== "translate") throw new Error("not a translate job");
      // Review/export require an actual PipelineResult, not only metadata.
      if (
        value.job.status === "done" ||
        value.job.status === "cancelled" ||
        value.job.status === "interrupted"
      ) {
        await api.entries(jobId, "all", 1, 1);
      }
      return value;
    };

    let jobId: string | null = null;
    let snapshot: JobSnapshot | null = null;
    for (const candidate of candidates) {
      try {
        snapshot = await probe(candidate);
        jobId = candidate;
        break;
      } catch {
        // Try the next live/persisted identity before declaring the row gone.
      }
    }
    if (snapshot === null || jobId === null) {
      try {
        const restored = await api.restoreSession(sessionId);
        snapshot = await probe(restored.id);
        jobId = restored.id;
      } catch {
        snapshot = null;
        jobId = null;
      }
    }
    if (snapshot === null || jobId === null) {
      useSessionJobs.getState().unregister(sessionId);
      useSessions.getState().patch(sessionId, { translateJobId: null });
      if (record.status === "running") {
        // The job died with the engine (crash or restart). If its checkpoint
        // survived, the session is interrupted and resumable — not lost.
        const checkpoint = await findCheckpoint(sessionId);
        if (checkpoint !== null) {
          const [interrupted] = reconcileCheckpoints([record], [checkpoint]);
          useSessions.getState().patch(sessionId, interrupted);
          if (get().sessionId === sessionId) {
            set({
              runState: interrupted.status === "running" ? "interrupted" : interrupted.status,
              runError: interrupted.error,
              resumable: true,
              interruptReason: checkpoint.reason,
              finishedAt: interrupted.finishedAt,
              activeBatches: {},
            });
            moru.setBusy(false);
          }
          return "gone";
        }
        const error = "engine no longer holds this translation job";
        useSessions.getState().patch(sessionId, {
          status: "failed",
          finishedAt: Date.now(),
          error,
        });
        if (get().sessionId === sessionId) {
          set({ runState: "failed", runError: error, finishedAt: Date.now() });
          moru.setBusy(false);
        }
      }
      return "gone";
    }

    let scanResult: ScanResult | null = null;
    try {
      scanResult = await api.scanResult(jobId);
    } catch {
      // Older sessions may not carry their W2 payload; compact totals remain.
    }

    closeScanEvents?.();
    closeTranslateEvents?.();
    closeExportEvents?.();
    closeSourceExportEvents?.();

    const engineRunState =
      snapshot.job.status === "pending" ? "running" : snapshot.job.status;
    const startedAt = Date.parse(snapshot.job.created_at);
    let restored: WizardStore = {
      ...get(),
      sessionId: record.id,
      modpackPath: record.modpackPath,
      modpackName: record.modpackName,
      probe: null,
      sourceLocale: record.sourceLocale,
      targetLocale: record.targetLocale,
      migrationEnabled: false,
      previousModpackPath: null,
      previousResourcepackPath: null,
      previousOverridesPath: null,
      ...initialJobState,
      scanState: scanResult !== null || record.scanTotals !== null ? "done" : "idle",
      scanResult,
      scanTotals:
        scanResult !== null
          ? selectedScanTotals({ scanResult, excludedCategories: [] })
          : record.scanTotals,
      translateJobId: jobId,
      model: record.model,
      runState: engineRunState,
      runError: snapshot.job.error,
      // Terminal frames below refresh it from the engine.
      resumable: record.resumable,
      startedAt: Number.isFinite(startedAt) ? startedAt : record.createdAt,
      translationStartedAt: snapshot.translation_started_at,
      finishedAt: engineRunState === "running" ? null : record.finishedAt,
      stats: engineRunState === "running" ? null : record.stats,
      exportState:
        record.exportZipPath !== null ||
        record.exportOverridesZipPath !== null ||
        record.exportBilingualZipPath != null ||
        record.exportBilingualOverridesZipPath != null
          ? "done"
          : "idle",
      exportZipPath: record.exportZipPath,
      exportOverridesZipPath: record.exportOverridesZipPath,
      exportBilingualZipPath: record.exportBilingualZipPath ?? null,
      exportBilingualOverridesZipPath: record.exportBilingualOverridesZipPath ?? null,
    };
    for (const frame of snapshot.events) {
      restored = { ...restored, ...translationFramePatch(restored, frame) };
    }
    restored.failedEntryCount = Math.max(restored.failedEntryCount, snapshot.failed_count);
    if (Object.keys(restored.fileProgress).length === 0) {
      restored.doneEntries = record.doneEntries;
    }
    set(restored);
    useSessionJobs.getState().register(sessionId, jobId);
    const current = get();
    useSessions.getState().patch(sessionId, {
      translateJobId: jobId,
      status: current.runState === "idle" ? record.status : current.runState,
      finishedAt: current.runState === "running" ? null : current.finishedAt,
      doneEntries: current.doneEntries,
      stats: current.stats,
      error: current.runError,
      resumable: current.resumable,
    });
    moru.setBusy(current.runState === "running");
    if (current.runState === "running") {
      attachTranslationEvents(jobId, sessionId, snapshot.cursor);
    }
    return "ok";
  },

  setTargetLocale: (locale) => {
    set({ targetLocale: locale });
    useSettings.getState().set({ targetLocale: locale });
  },

  setMigrationEnabled: (enabled) => set({ migrationEnabled: enabled }),

  setMigrationInput: (kind, path) =>
    set(
      kind === "modpack"
        ? { previousModpackPath: path }
        : kind === "resourcepack"
          ? { previousResourcepackPath: path }
          : { previousOverridesPath: path },
    ),

  toggleCategory: (name, included) =>
    set((state) => ({
      excludedCategories: included
        ? state.excludedCategories.filter((c) => c !== name)
        : [...new Set([...state.excludedCategories, name])],
    })),

  setCategories: (names, included) =>
    set((state) => ({
      excludedCategories: included
        ? state.excludedCategories.filter((c) => !names.includes(c))
        : [...new Set([...state.excludedCategories, ...names])],
    })),

  startScan: async () => {
    const {
      modpackPath,
      sourceLocale,
      targetLocale,
      migrationEnabled,
      previousModpackPath,
      previousResourcepackPath,
      previousOverridesPath,
    } = get();
    if (modpackPath === null || get().scanState === "running") return;
    closeScanEvents?.();
      set({
        scanState: "running",
        scanError: null,
        scanResult: null,
        scanTotals: null,
        scanProgress: { current: 0, total: 0, message: "" },
    });
    try {
      const job = await api.startScan({
        modpack_path: modpackPath,
        source_locale: sourceLocale,
        target_locale: targetLocale,
        // The blacklist decides which mods the scan even opens, so it has
        // to travel with the scan, not just with the translate run.
        mod_blacklist: useSettings.getState().modBlacklist,
        ...(migrationEnabled && previousModpackPath !== null
          ? { previous_modpack_path: previousModpackPath }
          : {}),
        ...(migrationEnabled && previousResourcepackPath !== null
          ? { previous_resourcepack_path: previousResourcepackPath }
          : {}),
        ...(migrationEnabled && previousOverridesPath !== null
          ? { previous_overrides_path: previousOverridesPath }
          : {}),
      });
      set({ scanJobId: job.id });
      // The scan must always reach a terminal state: an unhandled result
      // rejection used to pin scanState at "running", which now also blocks
      // the queue runner waiting on it.
      let settling = false;
      const settleScan = (): void => {
        if (settling) return;
        settling = true;
        void api
          .scanResult(job.id)
          .then((result) => {
            // A newer scan (or a reset) took over while this result was in
            // flight: it must not overwrite that scan's state.
            if (get().scanJobId !== job.id) return;
            // Confident launcher-metadata identity beats the folder-name guess.
            const identity = result.identity;
            set({
              scanState: "done",
              scanResult: result,
              scanTotals: selectedScanTotals({
                scanResult: result,
                excludedCategories: [],
              }),
              excludedCategories: [],
              ...(identity?.confident === true && identity.name !== null
                ? { modpackName: identity.name }
                : {}),
            });
          })
          .catch((error) => {
            if (get().scanJobId !== job.id) return;
            set({ scanState: "failed", scanError: String(error) });
          });
      };
      closeScanEvents = openJobEvents(
        job.id,
        (frame) => {
          if (get().scanJobId !== job.id) return; // superseded scan
          if (frame.type === "progress") {
            set({
              scanProgress: {
                current: frame.current ?? frame.done ?? 0,
                total: frame.total ?? 0,
                message: frame.message ?? "",
              },
            });
          } else if (frame.type === "done") {
            settleScan();
          } else if (frame.type === "failed") {
            set({ scanState: "failed", scanError: frame.error ?? "scan failed" });
          } else if (frame.type === "cancelled") {
            set({ scanState: "idle" });
          }
        },
        (_event, manuallyClosed) => {
          // Like the translate stream, a dropped socket must not leave the
          // scan running forever; the result probe decides done vs failed.
          const state = get();
          if (manuallyClosed || state.scanJobId !== job.id || state.scanState !== "running") {
            return;
          }
          settleScan();
        },
      );
    } catch (error) {
      set({ scanState: "failed", scanError: String(error) });
    }
  },

  startTranslate: async (settingsOverride) => {
    const state = get();
    if (state.modpackPath === null || state.runState === "running" || translateLaunching) return;
    const settings = settingsOverride ?? snapshotTranslationSettings();
    const scanTotals = selectedScanTotals(state);
    closeTranslateEvents?.();

    const providerId = providerIdOf(settings.model);
    let apiKey: string | undefined;
    // Claimed before the first await: a second click landing while the key
    // is read would otherwise pass the runState check too and start (and
    // bill) a second session.
    translateLaunching = true;
    try {
      apiKey = (await moru.secrets.get(`apikey:${providerId}`)) ?? undefined;
    } finally {
      translateLaunching = false;
    }
    const sessionId = state.sessionId ?? crypto.randomUUID();
    const params: TranslateParams = {
      ...buildTranslateParams(
        { ...state, modpackPath: state.modpackPath },
        settings,
        apiKey,
      ),
      session_id: sessionId,

    };

    set({
      runState: "running",
      model: settings.model,
      runError: null,
      startedAt: Date.now(),
      translationStartedAt: null,
      finishedAt: null,
      doneEntries: 0,
      scanTotals,
      fileProgress: {},
      glossaryProgress: null,
      failedKeys: {},
      failedEntryCount: 0,
      promptTokens: 0,
      completionTokens: 0,
      cachedTokens: 0,
      ticker: [],
      activeBatches: {},
      log: [],
      stats: null,
    });
    get().appendLog("info", `translate start: ${state.modpackName} → ${state.targetLocale}`);
    moru.setBusy(true);

    // Best-effort community pull: fresh approved corrections/terms land in
    // the local TM + user glossary store before the glossary stage reads
    // them. Network failure only logs - the run itself never blocks on it.
    try {
      const sync = await api.syncCommunity(WEB_URL, state.targetLocale, state.sourceLocale);
      const parts: string[] = [];
      if (sync.tm !== null) {
        parts.push(`TM v${sync.tm.version} (${sync.tm.entries})${sync.tm.updated ? " *" : ""}`);
      }
      if (sync.glossary !== null) {
        parts.push(
          `glossary v${sync.glossary.version} (${sync.glossary.terms})${sync.glossary.updated ? " *" : ""}`,
        );
      }
      get().appendLog(
        "info",
        parts.length > 0 ? `community sync: ${parts.join(", ")}` : "community sync: nothing published",
      );
    } catch (error) {
      get().appendLog("warn", `community sync skipped: ${String(error)}`);
    }

    const sessions = useSessions.getState();
    sessions.upsert({
      id: sessionId,
      modpackPath: state.modpackPath,
      modpackName: state.modpackName,
      sourceLocale: state.sourceLocale,
      targetLocale: state.targetLocale,
      model: settings.model,
      status: "running",
      createdAt: Date.now(),
      finishedAt: null,
      doneEntries: 0,
      totalEntries: scanTotals.entries,
      translateJobId: null,
      scanTotals,
      stats: null,
      error: null,
      exportZipPath: null,
      exportOverridesZipPath: null,
      exportBilingualZipPath: null,
      exportBilingualOverridesZipPath: null,
      sharedUrl: null,
      // The engine discards the checkpoint only once it accepts this run: a
      // refused request (409/422/offline) leaves it resumable.
      resumable: sessions.sessions.find((s) => s.id === sessionId)?.resumable ?? false,
    });
    set({ sessionId });

    const finish = (
      runState: "done" | "failed" | "cancelled",
      patch: Partial<WizardStore> = {},
    ): void => {
      moru.setBusy(false);
      set({ runState, finishedAt: Date.now(), activeBatches: {}, ...patch });
      const current = get();
      useSessions.getState().patch(sessionId, {
        status: runState,
        finishedAt: Date.now(),
        doneEntries: current.doneEntries,
        stats: current.stats,
        error: current.runError,
      });
    };

    try {
      const job = await api.startTranslate(params);
      // Accepted: this run starts the session over, its checkpoint replaced.
      set({ translateJobId: job.id, resumable: false, interruptReason: null });
      useSessionJobs.getState().register(sessionId, job.id);
      useSessions.getState().patch(sessionId, { translateJobId: job.id, resumable: false });
      lastSessionProgressPersistedAt = 0;
      attachTranslationEvents(job.id, sessionId);
    } catch (error) {
      get().appendLog("error", String(error));
      finish("failed", { runError: String(error) });
    }
  },

  handleTranslationFrame: (frame, sessionId) => {
    if (get().sessionId !== sessionId) return;
    set((prev) => translationFramePatch(prev, frame));
    const current = get();
    const terminal =
      frame.type === "done" ||
      frame.type === "failed" ||
      frame.type === "cancelled" ||
      frame.type === "interrupted";
    if (terminal) moru.setBusy(false);

    const now = Date.now();
    if (terminal || (frame.type === "progress" && now - lastSessionProgressPersistedAt >= 1000)) {
      lastSessionProgressPersistedAt = now;
      useSessions.getState().patch(sessionId, {
        ...(current.runState === "idle" ? {} : { status: current.runState }),
        finishedAt: current.finishedAt,
        doneEntries: current.doneEntries,
        stats: current.stats,
        error: current.runError,
        ...(terminal ? { resumable: current.resumable } : {}),
      });
    }
  },

  resumeTranslate: async (sessionId) => {
    const state = get();
    const queuePhase = useTranslationQueue.getState().phase;
    if (
      state.scanState === "running" ||
      state.runState === "running" ||
      state.exportState === "running" ||
      state.sourceExportState === "running" ||
      queuePhase === "running" ||
      queuePhase === "pausing" ||
      translateLaunching
    ) {
      return "busy";
    }
    const record: SessionRecord | undefined = useSessions
      .getState()
      .sessions.find((session) => session.id === sessionId);
    if (record === undefined || !record.resumable || record.status === "running") {
      return "gone";
    }

    // A run stopped because its provider refused the login, or whose
    // provider has since been switched off, could never finish on that
    // model: it continues on the model currently chosen in settings.
    const settings = useSettings.getState();
    let model: string;
    let providerId: string;
    let apiKey: string | undefined;
    translateLaunching = true; // see startTranslate
    try {
      model = await resumeModel(record.model, state.interruptReason, settings.model);
      // The key is never written into the checkpoint, so it is re-read here.
      // A local runtime's address is environment, not run semantics: current.
      providerId = providerIdOf(model);
      apiKey = (await moru.secrets.get(`apikey:${providerId}`)) ?? undefined;
    } finally {
      translateLaunching = false;
    }
    const apiBase =
      providerId === "ollama"
        ? settings.ollamaBaseUrl
        : providerId === "openai-compatible"
          ? settings.openaiCompatBaseUrl
          : undefined;

    closeScanEvents?.();
    closeTranslateEvents?.();
    closeExportEvents?.();
    closeSourceExportEvents?.();
    set({
      sessionId: record.id,
      modpackPath: record.modpackPath,
      modpackName: record.modpackName,
      probe: null,
      sourceLocale: record.sourceLocale,
      targetLocale: record.targetLocale,
      migrationEnabled: false,
      previousModpackPath: null,
      previousResourcepackPath: null,
      previousOverridesPath: null,
      ...initialJobState,
      scanState: record.scanTotals !== null ? "done" : "idle",
      scanTotals: record.scanTotals,
      model,
      runState: "running",
      startedAt: Date.now(),
    });
    get().appendLog("info", `translate resume: ${record.modpackName} → ${record.targetLocale}`);
    if (model !== record.model) {
      get().appendLog("warn", `model switched for resume: ${record.model} → ${model}`);
    }
    moru.setBusy(true);
    useSessions.getState().patch(sessionId, { status: "running", finishedAt: null, error: null });

    try {
      const job = await api.resumeTranslate({
        session_id: sessionId,
        resume: true,
        api_key: apiKey,
        api_base: apiBase,
        ...(model !== record.model ? { model } : {}),
      });
      set({ translateJobId: job.id });
      useSessionJobs.getState().register(sessionId, job.id);
      useSessions.getState().patch(sessionId, { translateJobId: job.id });
      lastSessionProgressPersistedAt = 0;
      attachTranslationEvents(job.id, sessionId);
      return "started";
    } catch (error) {
      // A refused resume leaves the checkpoint as it was; only a 404 means
      // the engine no longer has one to continue.
      const gone = error instanceof EngineApiError && error.status === 404;
      const message = String(error);
      get().appendLog("error", message);
      moru.setBusy(false);
      const finishedAt = Date.now();
      const status = gone ? "failed" : record.status;
      set({ runState: status, runError: message, resumable: !gone, finishedAt });
      useSessions.getState().patch(sessionId, {
        status,
        finishedAt,
        error: message,
        resumable: !gone,
      });
      return "requestFailed";
    }
  },

  cancelTranslate: async () => {
    const { translateJobId } = get();
    if (translateJobId === null) return;
    await api.cancelJob(translateJobId);
  },

  updateReviewStats: (stats) => {
    // Any successful review mutation makes previously built archives stale.
    // The engine already drops zip paths from its persisted done payload;
    // mirror that invalidation in the renderer and local history so W6 offers
    // "Create export" again instead of only opening the obsolete artifact.
    set({
      stats,
      exportJobId: null,
      exportState: "idle",
      exportZipPath: null,
      exportOverridesZipPath: null,
      exportBilingualZipPath: null,
      exportBilingualOverridesZipPath: null,
      exportError: null,
    });
    const sessionId = get().sessionId;
    if (sessionId !== null) {
      useSessions.getState().patch(sessionId, {
        stats,
        exportZipPath: null,
        exportOverridesZipPath: null,
        exportBilingualZipPath: null,
        exportBilingualOverridesZipPath: null,
      });
    }
  },

  startExport: async () => {
    const { translateJobId, exportState } = get();
    if (translateJobId === null || exportState === "running") return;
    closeExportEvents?.();
    set({ exportState: "running", exportError: null });
    try {
      const job = await api.startExport({ translate_job_id: translateJobId });
      set({ exportJobId: job.id });
      closeExportEvents = openJobEvents(job.id, (frame) => {
        if (frame.type === "done") {
          const zipPath = frame.zip_path ?? null;
          const overridesZipPath = frame.overrides_zip_path ?? null;
          const bilingualZipPath = frame.bilingual_zip_path ?? null;
          const bilingualOverridesZipPath = frame.bilingual_overrides_zip_path ?? null;
          set({
            exportState: "done",
            exportZipPath: zipPath,
            exportOverridesZipPath: overridesZipPath,
            exportBilingualZipPath: bilingualZipPath,
            exportBilingualOverridesZipPath: bilingualOverridesZipPath,
          });
          const sessionId = get().sessionId;
          if (sessionId !== null) {
            useSessions.getState().patch(sessionId, {
              exportZipPath: zipPath,
              exportOverridesZipPath: overridesZipPath,
              exportBilingualZipPath: bilingualZipPath,
              exportBilingualOverridesZipPath: bilingualOverridesZipPath,
            });
          }
        } else if (frame.type === "failed") {
          set({ exportState: "failed", exportError: frame.error ?? "export failed" });
        }
      });
    } catch (error) {
      set({ exportState: "failed", exportError: String(error) });
    }
  },

  /**
   * Seed a hand-translation session (W3 "손으로 번역하기").
   *
   * An ordinary translate job in every respect the rest of the app can see —
   * same `runState`, same history record, same W5/W6 — but flagged
   * `manual_seed`, so the engine leaves every entry untranslated for a human
   * instead of calling a provider. No model, api_key or api_base is sent, and
   * none is required: this is the path for someone who has configured nothing.
   *
   * `use_tm` and the glossaries deliberately still apply. They are pure local
   * lookups and are exactly the help a hand translator wants; only glossary
   * curation needs a model, which is why `extract_glossary` is forced off
   * here as well as engine-side.
   *
   * Refuses with a NAMED reason rather than clobbering state, and W3 renders
   * it: the queue drives this same single wizard session and reads a
   * `sessionId` change as abandonment, so starting a manual session mid-drain
   * would silently drop a queued pack.
   */
  startManualSeed: async () => {
    const state = get();
    if (state.modpackPath === null) return "noPack";
    if (state.runState === "running") return "runBusy";
    // The queue drives this same single wizard session and reads a `sessionId`
    // change as abandonment, so seeding a manual session mid-drain would
    // silently drop a queued pack. (translationQueue imports this module, but
    // the cycle is inert: both sides only dereference at call time.)
    const queuePhase = useTranslationQueue.getState().phase;
    if (queuePhase === "running" || queuePhase === "pausing") return "queueBusy";

    const settings = snapshotTranslationSettings();
    const scanTotals = selectedScanTotals(state);
    closeTranslateEvents?.();

    const sessionId = state.sessionId ?? crypto.randomUUID();
    const base = buildTranslateParams(
      { ...state, modpackPath: state.modpackPath },
      settings,
    );
    const params: TranslateParams = {
      ...base,
      session_id: sessionId,
      manual_seed: true,
      // Explicitly cleared rather than merely unused: a model recorded on a
      // session no provider was asked about would misreport how the pack was
      // translated.
      model: undefined,
      api_key: undefined,
      api_base: undefined,
      extract_glossary: false,
    };

    set({
      runState: "running",
      // No model was involved; the history row says so rather than naming one.
      model: null,
      runError: null,
      startedAt: Date.now(),
      translationStartedAt: null,
      finishedAt: null,
      doneEntries: 0,
      scanTotals,
      fileProgress: {},
      glossaryProgress: null,
      failedKeys: {},
      failedEntryCount: 0,
      promptTokens: 0,
      completionTokens: 0,
      cachedTokens: 0,
      ticker: [],
      activeBatches: {},
      log: [],
      stats: null,
    });
    get().appendLog(
      "info",
      `manual seed: ${state.modpackName} → ${state.targetLocale} (no provider)`,
    );

    useSessions.getState().upsert({
      id: sessionId,
      modpackPath: state.modpackPath,
      modpackName: state.modpackName,
      sourceLocale: state.sourceLocale,
      targetLocale: state.targetLocale,
      model: "",
      status: "running",
      createdAt: Date.now(),
      finishedAt: null,
      doneEntries: 0,
      totalEntries: scanTotals.entries,
      translateJobId: null,
      scanTotals,
      stats: null,
      error: null,
      exportZipPath: null,
      exportOverridesZipPath: null,
      exportBilingualZipPath: null,
      exportBilingualOverridesZipPath: null,
      sharedUrl: null,
      // Cleared only once the engine accepts the seed, as in startTranslate.
      resumable:
        useSessions.getState().sessions.find((s) => s.id === sessionId)?.resumable ?? false,
    });
    set({ sessionId });

    try {
      const job = await api.startTranslate(params);
      set({ translateJobId: job.id });
      useSessionJobs.getState().register(sessionId, job.id);
      // The SAME plumbing an ordinary run uses, deliberately — a raw
      // `openJobEvents` here left a manual seed with neither of the two
      // guarantees W5M now depends on:
      //  * the session record never learned its `translateJobId`, so
      //    reopening the session later found no job and showed the
      //    "nothing to translate" panel instead of the queue;
      //  * a dropped event stream was never re-established, so a missed
      //    terminal frame pinned `runState` at "running" forever.
      useSessions.getState().patch(sessionId, { translateJobId: job.id, resumable: false });
      set({ resumable: false, interruptReason: null });
      lastSessionProgressPersistedAt = 0;
      attachTranslationEvents(job.id, sessionId);
      return "started";
    } catch (error) {
      get().appendLog("error", String(error));
      set({ runState: "failed", runError: String(error) });
      useSessions.getState().patch(sessionId, {
        status: "failed",
        finishedAt: Date.now(),
        error: String(error),
      });
      return "requestFailed";
    }
  },

  /**
   * Export the pack as source text (W3). Same engine export job W6 runs,
   * flagged so the engine settles every entry to its own source string:
   * no translate job, no model, and no API key is involved, so this works
   * straight off a scan.
   */
  startSourceExport: async () => {
    const state = get();
    if (state.modpackPath === null || state.sourceExportState === "running") return;
    closeSourceExportEvents?.();
    set({ sourceExportState: "running", sourceExportError: null });
    try {
      const job = await api.startExport({
        source_text: true,
        modpack_path: state.modpackPath,
        source_locale: state.sourceLocale,
        target_locale: state.targetLocale,
        include_categories: includedCategories(state),
        mod_blacklist: useSettings.getState().modBlacklist,
        output_dir: useSettings.getState().outputDir ?? undefined,
      });
      set({ sourceExportJobId: job.id });
      closeSourceExportEvents = openJobEvents(job.id, (frame) => {
        if (frame.type === "done") {
          set({
            sourceExportState: "done",
            sourceExportZipPath: frame.zip_path ?? null,
            sourceExportOverridesZipPath: frame.overrides_zip_path ?? null,
          });
        } else if (frame.type === "failed" || frame.type === "cancelled") {
          set({
            sourceExportState: "failed",
            sourceExportError: frame.error ?? "source export failed",
          });
        }
      });
    } catch (error) {
      set({ sourceExportState: "failed", sourceExportError: String(error) });
    }
  },

  appendLog: (level, text) =>
    set((prev) => ({
      log: [...prev.log.slice(-MAX_LOG_LINES + 1), { ts: Date.now(), level, text }],
    })),

  reset: () => {
    closeScanEvents?.();
    closeTranslateEvents?.();
    closeExportEvents?.();
    closeSourceExportEvents?.();
    moru.setBusy(false);
    set({
      sessionId: null,
      modpackPath: null,
      modpackName: "",
      probe: null,
      migrationEnabled: false,
      previousModpackPath: null,
      previousResourcepackPath: null,
      previousOverridesPath: null,
      ...initialJobState,
    });
  },
}));
