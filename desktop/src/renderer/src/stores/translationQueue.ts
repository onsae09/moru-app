/**
 * Durable, renderer-side queue for unattended multi-pack translation.
 *
 * The queue deliberately orchestrates the existing wizard store instead of
 * introducing a second engine pipeline. One item owns the wizard at a time;
 * completed jobs remain ordinary History sessions and use the existing W5/W6
 * review/export flow.
 */

import i18next from "i18next";
import { create } from "zustand";
import { persist } from "zustand/middleware";

import type { DesktopPlatform, ModpackProbe } from "../../../shared/bridge";
import { moru } from "../lib/bridge";
import { engineGeneration } from "./engine";
import { useSessions } from "./sessions";
import {
  snapshotTranslationSettings,
  type TranslationRunSettings,
} from "./settings";
import { useWizard } from "./wizard";

export type TranslationQueueItemStatus =
  | "pending"
  | "scanning"
  | "translating"
  | "done"
  | "failed"
  | "cancelled";

export type TranslationQueuePhase =
  | "idle"
  | "running"
  | "pausing"
  | "paused"
  | "complete";

export interface TranslationQueueItem {
  id: string;
  path: string;
  name: string;
  status: TranslationQueueItemStatus;
  sessionId: string | null;
  error: string | null;
  addedAt: number;
  startedAt: number | null;
  finishedAt: number | null;
}

export interface TranslationQueueInput {
  path: string;
  name: string;
}

interface TranslationQueueStore {
  items: TranslationQueueItem[];
  phase: TranslationQueuePhase;
  settingsSnapshot: TranslationRunSettings | null;
  lastError: string | null;
  /** False only while an interrupted persisted item is being reconciled. */
  ready: boolean;
  add: (inputs: TranslationQueueInput[]) => void;
  remove: (id: string) => void;
  move: (id: string, direction: -1 | 1) => void;
  retry: (id: string) => void;
  clear: () => void;
  patchItem: (id: string, patch: Partial<TranslationQueueItem>) => void;
}

interface PersistedTranslationQueueState {
  items: TranslationQueueItem[];
  phase: TranslationQueuePhase;
  settingsSnapshot: TranslationRunSettings | null;
  lastError: string | null;
}

export function queuePathKey(path: string, platform: DesktopPlatform): string {
  const normalized = path.trim().replace(/[\\/]+$/, "").replaceAll("\\", "/");
  return platform === "win32" ? normalized.toLocaleLowerCase("en-US") : normalized;
}

/** Rows that own the wizard right now. */
function ownsWizard(status: TranslationQueueItemStatus): boolean {
  return status === "scanning" || status === "translating";
}

export function appendUniqueQueueItems(
  existing: TranslationQueueItem[],
  inputs: TranslationQueueInput[],
  platform: DesktopPlatform,
  makeId: () => string = () => crypto.randomUUID(),
  now: () => number = () => Date.now(),
): TranslationQueueItem[] {
  // Only active rows reserve their path; terminal rows stay as history
  // instead of permanently blocking a re-add of the same folder.
  const keys = new Set(
    existing
      .filter((item) => item.status === "pending" || ownsWizard(item.status))
      .map((item) => queuePathKey(item.path, platform)),
  );
  const added: TranslationQueueItem[] = [];
  for (const input of inputs) {
    const key = queuePathKey(input.path, platform);
    if (key.length === 0 || keys.has(key)) continue;
    keys.add(key);
    added.push({
      id: makeId(),
      path: input.path,
      name: input.name,
      status: "pending",
      sessionId: null,
      error: null,
      addedAt: now(),
      startedAt: null,
      finishedAt: null,
    });
  }
  return [...existing, ...added];
}

export function movePendingQueueItem(
  items: TranslationQueueItem[],
  id: string,
  direction: -1 | 1,
): TranslationQueueItem[] {
  const index = items.findIndex((item) => item.id === id && item.status === "pending");
  if (index < 0) return items;
  let swapIndex = index + direction;
  while (swapIndex >= 0 && swapIndex < items.length && items[swapIndex].status !== "pending") {
    swapIndex += direction;
  }
  if (swapIndex < 0 || swapIndex >= items.length) return items;
  const next = [...items];
  [next[index], next[swapIndex]] = [next[swapIndex], next[index]];
  return next;
}

export function retryQueueItem(
  item: TranslationQueueItem,
): TranslationQueueItem {
  if (item.status !== "failed" && item.status !== "cancelled") return item;
  return {
    ...item,
    status: "pending",
    sessionId: null,
    error: null,
    startedAt: null,
    finishedAt: null,
  };
}

function validPhase(value: unknown): value is TranslationQueuePhase {
  return ["idle", "running", "pausing", "paused", "complete"].includes(String(value));
}

function validStatus(value: unknown): value is TranslationQueueItemStatus {
  return ["pending", "scanning", "translating", "done", "failed", "cancelled"].includes(
    String(value),
  );
}

/** A truncated snapshot would drive real spend, so re-snapshot instead. */
function validSettingsSnapshot(value: unknown): value is TranslationRunSettings {
  if (value === null || typeof value !== "object") return false;
  const snapshot = value as Partial<TranslationRunSettings>;
  return (
    (snapshot.outputDir === null || typeof snapshot.outputDir === "string") &&
    typeof snapshot.model === "string" &&
    typeof snapshot.temperature === "number" &&
    typeof snapshot.batchSize === "number" &&
    typeof snapshot.maxConcurrent === "number" &&
    typeof snapshot.maxRefine === "number" &&
    typeof snapshot.thinkingEnabled === "boolean" &&
    (snapshot.thinkingEffort === "low" ||
      snapshot.thinkingEffort === "medium" ||
      snapshot.thinkingEffort === "high") &&
    typeof snapshot.useTm === "boolean" &&
    typeof snapshot.useVanillaGlossary === "boolean" &&
    typeof snapshot.extractGlossary === "boolean" &&
    (snapshot.glossaryMaxTerms === null || typeof snapshot.glossaryMaxTerms === "number") &&
    typeof snapshot.ollamaBaseUrl === "string" &&
    typeof snapshot.openaiCompatBaseUrl === "string" &&
    typeof snapshot.targetLocale === "string"
  );
}

/** Normalize localStorage data and never resume paid work automatically. */
export function normalizePersistedQueueState(
  persisted: unknown,
): PersistedTranslationQueueState {
  const value = (persisted ?? {}) as Partial<PersistedTranslationQueueState>;
  const items = Array.isArray(value.items)
    ? value.items
        .filter(
          (item): item is TranslationQueueItem =>
            item !== null &&
            typeof item === "object" &&
            typeof item.id === "string" &&
            typeof item.path === "string" &&
            validStatus(item.status),
        )
        .map((item) => ({
          ...item,
          name: typeof item.name === "string" && item.name.length > 0 ? item.name : item.path,
          sessionId: typeof item.sessionId === "string" ? item.sessionId : null,
          error: typeof item.error === "string" ? item.error : null,
          addedAt: Number.isFinite(item.addedAt) ? item.addedAt : Date.now(),
          startedAt: Number.isFinite(item.startedAt) ? item.startedAt : null,
          finishedAt: Number.isFinite(item.finishedAt) ? item.finishedAt : null,
        }))
    : [];
  const storedPhase = validPhase(value.phase) ? value.phase : "idle";
  return {
    items,
    phase: storedPhase === "running" || storedPhase === "pausing" ? "paused" : storedPhase,
    settingsSnapshot: validSettingsSnapshot(value.settingsSnapshot)
      ? value.settingsSnapshot
      : null,
    lastError: typeof value.lastError === "string" ? value.lastError : null,
  };
}

export const useTranslationQueue = create<TranslationQueueStore>()(
  persist(
    (set, get) => ({
      items: [],
      phase: "idle",
      settingsSnapshot: null,
      lastError: null,
      ready: false,
      add: (inputs) =>
        set((state) => ({
          items: appendUniqueQueueItems(state.items, inputs, moru.platform),
          phase: state.phase === "complete" ? "idle" : state.phase,
          lastError: null,
        })),
      remove: (id) =>
        set((state) => ({
          items: state.items.filter(
            (item) => item.id !== id || ownsWizard(item.status),
          ),
        })),
      move: (id, direction) =>
        set((state) => ({ items: movePendingQueueItem(state.items, id, direction) })),
      retry: (id) =>
        set((state) => {
          const target = state.items.find((item) => item.id === id);
          if (target === undefined) return state;
          // Terminal rows no longer block a re-add, so the same folder may
          // already be queued again; never translate one pack twice.
          const key = queuePathKey(target.path, moru.platform);
          const queuedAgain = state.items.some(
            (item) =>
              item.id !== id &&
              (item.status === "pending" || ownsWizard(item.status)) &&
              queuePathKey(item.path, moru.platform) === key,
          );
          if (queuedAgain) return state;
          return {
            items: state.items.map((item) => (item.id === id ? retryQueueItem(item) : item)),
            phase: state.phase === "complete" ? "idle" : state.phase,
            lastError: null,
          };
        }),
      clear: () => {
        if (get().items.some((item) => ownsWizard(item.status))) {
          // Abandoning is the only in-app escape when a wizard wait never
          // settles. Stop the engine job first, then release the wizard so
          // the parked drain loop resolves and exits on the idle phase.
          void useWizard.getState().cancelTranslate().catch(() => undefined);
          useWizard.getState().reset();
        }
        set({ items: [], phase: "idle", settingsSnapshot: null, lastError: null });
      },
      patchItem: (id, patch) =>
        set((state) => ({
          items: state.items.map((item) => (item.id === id ? { ...item, ...patch } : item)),
        })),
    }),
    {
      name: "moru-translation-queue",
      version: 1,
      partialize: (state) => ({
        items: state.items,
        phase: state.phase,
        settingsSnapshot: state.settingsSnapshot,
        lastError: state.lastError,
      }),
      merge: (persisted, current) => ({
        ...current,
        ...normalizePersistedQueueState(persisted),
      }),
    },
  ),
);

export interface QueueItemOutcome {
  status: "done" | "failed" | "cancelled";
  sessionId?: string | null;
  name?: string;
  error?: string | null;
}

export interface TranslationQueueDriver {
  getPhase: () => TranslationQueuePhase;
  getNext: () => TranslationQueueItem | undefined;
  run: (item: TranslationQueueItem) => Promise<QueueItemOutcome>;
  settle: (item: TranslationQueueItem, outcome: QueueItemOutcome) => void;
  setPhase: (phase: TranslationQueuePhase) => void;
}

/** Generic drain loop kept dependency-injectable for deterministic tests. */
export async function drainTranslationQueue(driver: TranslationQueueDriver): Promise<void> {
  while (true) {
    const phase = driver.getPhase();
    if (phase === "pausing") {
      driver.setPhase("paused");
      return;
    }
    if (phase !== "running") return;
    const item = driver.getNext();
    if (item === undefined) {
      driver.setPhase("complete");
      return;
    }
    let outcome: QueueItemOutcome;
    try {
      outcome = await driver.run(item);
    } catch (error) {
      outcome = { status: "failed", error: String(error) };
    }
    driver.settle(item, outcome);
    const settled = driver.getPhase();
    // An explicit cancel of the running pack must not silently start the next
    // pack's paid translation; treat it like the pause request it really is.
    if (settled === "pausing" || (settled === "running" && outcome.status === "cancelled")) {
      driver.setPhase("paused");
      return;
    }
  }
}

type WizardState = ReturnType<typeof useWizard.getState>;

function waitForWizard(
  terminal: (state: WizardState) => boolean,
): Promise<WizardState> {
  const current = useWizard.getState();
  if (terminal(current)) return Promise.resolve(current);
  return new Promise((resolve) => {
    const unsubscribe = useWizard.subscribe((state) => {
      if (!terminal(state)) return;
      unsubscribe();
      resolve(state);
    });
  });
}

function probeError(path: string, probe: ModpackProbe): string | null {
  if (!probe.exists) return i18next.t("queue.validation.notFound", { path });
  if (!probe.isDirectory) return i18next.t("queue.validation.notDirectory", { path });
  if (!probe.hasMods) return i18next.t("queue.validation.noMods", { path });
  return null;
}

/** The queue lost the wizard mid-item, so the row is abandoned, not failed. */
function abandonOutcome(sessionId: string | null): QueueItemOutcome {
  if (sessionId !== null) {
    // Never leave a permanently "running" row behind in History.
    useSessions.getState().patch(sessionId, { status: "cancelled", finishedAt: Date.now() });
  }
  return { status: "cancelled", sessionId };
}

function ensureFailedSession(
  item: TranslationQueueItem,
  sessionId: string | null,
  settings: TranslationRunSettings,
  error: string,
): string {
  const wizard = useWizard.getState();
  const id = sessionId ?? wizard.sessionId ?? crypto.randomUUID();
  const sessions = useSessions.getState();
  const existing = sessions.sessions.find((session) => session.id === id);
  if (existing !== undefined) {
    sessions.patch(id, {
      status: "failed",
      finishedAt: Date.now(),
      error,
    });
    return id;
  }
  const totals = wizard.scanTotals;
  sessions.upsert({
    id,
    modpackPath: item.path,
    modpackName: wizard.modpackName || item.name,
    sourceLocale: wizard.sourceLocale || "en_us",
    targetLocale: wizard.targetLocale || settings.targetLocale,
    model: settings.model,
    status: "failed",
    createdAt: item.startedAt ?? Date.now(),
    finishedAt: Date.now(),
    doneEntries: 0,
    totalEntries: totals?.entries ?? 0,
    translateJobId: null,
    scanTotals: totals,
    stats: null,
    error,
    exportZipPath: null,
    exportOverridesZipPath: null,
    exportBilingualZipPath: null,
    exportBilingualOverridesZipPath: null,
    sharedUrl: null,
    resumable: false,
  });
  return id;
}

async function runQueueItem(
  item: TranslationQueueItem,
  settings: TranslationRunSettings,
): Promise<QueueItemOutcome> {
  const queue = useTranslationQueue.getState();
  queue.patchItem(item.id, {
    status: "scanning",
    error: null,
    startedAt: Date.now(),
    finishedAt: null,
  });

  let sessionId: string | null = null;
  try {
    const probe = await moru.probeModpack(item.path);
    const invalid = probeError(item.path, probe);
    if (invalid !== null) {
      sessionId = ensureFailedSession(item, null, settings, invalid);
      return { status: "failed", sessionId, error: invalid };
    }

    useWizard.getState().reset();
    useWizard.getState().startSession(item.path, probe, settings.targetLocale);
    sessionId = useWizard.getState().sessionId;
    queue.patchItem(item.id, { sessionId, name: probe.name });

    await useWizard.getState().startScan();
    // A cleared queue resets the wizard: without that escape these waits, and
    // with them the whole drain loop, would park forever.
    const scanned = await waitForWizard(
      (state) => state.sessionId !== sessionId || state.scanState !== "running",
    );
    if (scanned.sessionId !== sessionId) return abandonOutcome(sessionId);
    if (scanned.scanState !== "done") {
      const error = scanned.scanError ?? i18next.t("queue.error.scanFailed");
      sessionId = ensureFailedSession(item, sessionId, settings, error);
      return { status: "failed", sessionId, name: scanned.modpackName, error };
    }

    queue.patchItem(item.id, { status: "translating", name: scanned.modpackName });
    await useWizard.getState().startTranslate(settings);
    const translated = await waitForWizard(
      (state) =>
        state.sessionId !== sessionId ||
        state.runState === "done" ||
        state.runState === "failed" ||
        state.runState === "cancelled" ||
        state.runState === "interrupted",
    );
    if (translated.sessionId !== sessionId) return abandonOutcome(sessionId);
    // An interrupted pack (spent quota, closed engine) settles as cancelled:
    // the drain loop then pauses instead of spending on the next pack, and
    // the History row offers "이어서 번역" — a click, never automatic.
    const status =
      translated.runState === "done" || translated.runState === "failed"
        ? translated.runState
        : translated.runState === "cancelled" || translated.runState === "interrupted"
          ? "cancelled"
          : "failed";
    return {
      status,
      sessionId: translated.sessionId,
      name: translated.modpackName,
      error: translated.runError,
    };
  } catch (error) {
    const message = String(error);
    sessionId = ensureFailedSession(item, sessionId, settings, message);
    return { status: "failed", sessionId, error: message };
  }
}

function wizardBusy(): boolean {
  const state = useWizard.getState();
  return (
    state.scanState === "running" ||
    state.runState === "running" ||
    state.exportState === "running"
  );
}

let runnerPromise: Promise<void> | null = null;

export function startTranslationQueue(): void {
  const queue = useTranslationQueue.getState();
  if (!queue.ready || runnerPromise !== null) return;
  if (wizardBusy()) {
    useTranslationQueue.setState({
      phase: "paused",
      lastError: i18next.t("queue.error.wizardBusy"),
    });
    return;
  }
  if (!queue.items.some((item) => item.status === "pending")) return;

  const settings =
    queue.phase === "paused" && queue.settingsSnapshot !== null
      ? queue.settingsSnapshot
      : snapshotTranslationSettings();
  useTranslationQueue.setState({
    phase: "running",
    settingsSnapshot: settings,
    lastError: null,
  });

  runnerPromise = drainTranslationQueue({
    getPhase: () => useTranslationQueue.getState().phase,
    getNext: () =>
      useTranslationQueue.getState().items.find((item) => item.status === "pending"),
    run: (item) => runQueueItem(item, settings),
    settle: (item, outcome) => {
      useTranslationQueue.getState().patchItem(item.id, {
        status: outcome.status,
        sessionId: outcome.sessionId ?? item.sessionId,
        name: outcome.name ?? item.name,
        error: outcome.error ?? null,
        finishedAt: Date.now(),
      });
    },
    // A finished queue must not advertise, or silently reuse, the settings
    // locked by the run that just ended.
    setPhase: (phase) =>
      useTranslationQueue.setState(
        phase === "complete" ? { phase, settingsSnapshot: null } : { phase },
      ),
  }).finally(() => {
    runnerPromise = null;
    if (useTranslationQueue.getState().phase === "running") {
      useTranslationQueue.setState({ phase: "paused" });
    }
  });
}

export function pauseTranslationQueue(): void {
  const phase = useTranslationQueue.getState().phase;
  if (phase === "running") useTranslationQueue.setState({ phase: "pausing" });
}

let initializationPromise: Promise<void> | null = null;
let initializedFor: string | null = null;

/**
 * Reattach a live translate job after renderer reload, but never advance it.
 * Runs again for every new engine process: a restarted engine no longer
 * holds the jobs the last reconciliation saw.
 */
export function initializeTranslationQueue(): Promise<void> {
  const generation = engineGeneration();
  if (initializationPromise !== null && initializedFor === generation) {
    return initializationPromise;
  }
  initializedFor = generation;
  initializationPromise = (async () => {
    const queue = useTranslationQueue.getState();
    const settings = queue.settingsSnapshot ?? snapshotTranslationSettings();
    const activeItems = queue.items.filter((item) => ownsWizard(item.status));

    for (const item of activeItems) {
      if (item.status === "scanning") {
        const error = i18next.t("queue.error.interruptedScan");
        const sessionId = ensureFailedSession(item, item.sessionId, settings, error);
        useTranslationQueue.getState().patchItem(item.id, {
          status: "failed",
          sessionId,
          error,
          finishedAt: Date.now(),
        });
        continue;
      }

      if (item.sessionId === null) {
        const error = i18next.t("queue.error.lostTranslation");
        const sessionId = ensureFailedSession(item, null, settings, error);
        useTranslationQueue.getState().patchItem(item.id, {
          status: "failed",
          sessionId,
          error,
          finishedAt: Date.now(),
        });
        continue;
      }

      const reopened = await useWizard.getState().reopenSession(item.sessionId);
      if (reopened !== "ok") {
        const session = useSessions
          .getState()
          .sessions.find((record) => record.id === item.sessionId);
        // Its checkpoint survived the restart: paused work, not a failure.
        const resumable = session?.resumable === true;
        useTranslationQueue.getState().patchItem(item.id, {
          status: resumable ? "cancelled" : "failed",
          error: resumable
            ? i18next.t("queue.error.interruptedTranslation")
            : (session?.error ?? i18next.t("queue.error.lostTranslation")),
          finishedAt: Date.now(),
        });
        continue;
      }

      const settle = (restored: WizardState): void => {
        const status =
          restored.runState === "done" || restored.runState === "failed"
            ? restored.runState
            : restored.runState === "cancelled" || restored.runState === "interrupted"
              ? "cancelled"
              : "failed";
        useTranslationQueue.getState().patchItem(item.id, {
          status,
          name: restored.modpackName || item.name,
          error: restored.runError,
          finishedAt: Date.now(),
        });
      };
      if (useWizard.getState().runState === "running") {
        // Still translating after the reattach: settle it when it ends, but
        // do not hold the queue's `ready` hostage for the hours it may run.
        void waitForWizard((state) => state.runState !== "running").then(settle);
        continue;
      }
      settle(useWizard.getState());
    }

    const state = useTranslationQueue.getState();
    const remaining = state.items.some((item) => item.status === "pending");
    // Only an interrupted or explicitly paused run may resume with the locked
    // snapshot; a queue restored with pending items it never started reads as
    // idle so startTranslationQueue re-snapshots the current settings.
    // A reattached item still running owns the wizard: the queue waits for
    // it, paused, instead of reading as idle beside a live run.
    const live = state.items.some((item) => ownsWizard(item.status));
    const resumable = remaining && (state.phase === "paused" || live);
    const phase: TranslationQueuePhase = resumable
      ? "paused"
      : remaining || state.items.length === 0
        ? "idle"
        : "complete";
    useTranslationQueue.setState({
      phase,
      ...(resumable ? {} : { settingsSnapshot: null }),
      ready: true,
    });
  })().catch((error) => {
    useTranslationQueue.setState({
      phase: "paused",
      ready: true,
      lastError: i18next.t("queue.error.recovery", { error: String(error) }),
    });
  });
  return initializationPromise;
}

/** Subscribe with this so a screen re-renders when the queue takes the wizard. */
export function selectQueueActive(state: TranslationQueueStore): boolean {
  return (
    state.phase === "running" ||
    state.phase === "pausing" ||
    state.items.some((item) => ownsWizard(item.status))
  );
}

export function translationQueueIsActive(): boolean {
  return selectQueueActive(useTranslationQueue.getState());
}
