/**
 * Persisted translation session history. The engine's JobManager is
 * in-memory per run, so the desktop keeps its own durable record of every
 * wizard session - this feeds the home dashboard and the history screen.
 */

import { create } from "zustand";
import { persist } from "zustand/middleware";

import type { CheckpointSummary, PipelineStats } from "../../../shared/engine";
import { parseTimestamp } from "../lib/format";

/**
 * `interrupted`: the run stopped early but resumably — the provider's quota
 * or credit ran out, or the app/engine closed mid-run.
 */
export type SessionStatus = "running" | "done" | "failed" | "cancelled" | "interrupted";

/** Compact W2 totals retained without persisting the full scan tree. */
export interface SessionScanTotals {
  files: number;
  entries: number;
  chars: number;
  migrationEntries: number;
  migrationChars: number;
  translationEntries: number;
  translationChars: number;
}

export interface SessionRecord {
  id: string;
  modpackPath: string;
  modpackName: string;
  sourceLocale: string;
  targetLocale: string;
  model: string;
  status: SessionStatus;
  createdAt: number;
  finishedAt: number | null;
  /** live counters while running; final stats when done */
  doneEntries: number;
  totalEntries: number;
  /** Process-scoped engine job id; probed before every restore. */
  translateJobId: string | null;
  /** W4 estimates/progress denominator when the full scan state is gone. */
  scanTotals: SessionScanTotals | null;
  stats: PipelineStats | null;
  error: string | null;
  exportZipPath: string | null;
  exportOverridesZipPath: string | null;
  exportBilingualZipPath: string | null;
  exportBilingualOverridesZipPath: string | null;
  sharedUrl: string | null;
  /**
   * The engine still holds this session's translation checkpoint, so
   * "이어서 번역" continues it without paying for finished batches again.
   * Owned by the engine: set from terminal frames and GET /checkpoints.
   */
  resumable: boolean;
}

interface SessionsStore {
  sessions: SessionRecord[];
  upsert: (record: SessionRecord) => void;
  patch: (id: string, patch: Partial<SessionRecord>) => void;
  remove: (id: string) => void;
}

/** Normalize older persisted records before Zustand merges in store actions. */
export function migrateSessionsState(
  persisted: unknown,
  now = Date.now(),
): { sessions: SessionRecord[] } {
  // merge runs on a fresh install too, with nothing persisted yet.
  const state = (persisted ?? {}) as Partial<SessionsStore>;
  return {
    ...state,
    sessions: (state.sessions ?? []).map((session) => {
      const translateJobId = session.translateJobId ?? null;
      // Before job ids were persisted, a renderer/app restart could leave a
      // record permanently labelled running even though no engine job could
      // possibly be reattached. Do not keep presenting that zombie as live.
      const orphaned = session.status === "running" && translateJobId === null;
      return {
        ...session,
        doneEntries: session.doneEntries ?? 0,
        totalEntries: session.totalEntries ?? 0,
        translateJobId,
        scanTotals: session.scanTotals ?? null,
        status: orphaned ? ("failed" as const) : session.status,
        finishedAt: orphaned
          ? (session.finishedAt ?? now)
          : (session.finishedAt ?? null),
        stats: session.stats ?? null,
        error: orphaned
          ? (session.error ?? "앱 재시작 후 작업 상태를 복원할 수 없습니다.")
          : (session.error ?? null),
        exportZipPath: session.exportZipPath ?? null,
        exportOverridesZipPath: session.exportOverridesZipPath ?? null,
        exportBilingualZipPath: session.exportBilingualZipPath ?? null,
        exportBilingualOverridesZipPath: session.exportBilingualOverridesZipPath ?? null,
        sharedUrl: session.sharedUrl ?? null,
        resumable: session.resumable ?? false,
      };
    }),
  };
}

/**
 * Fold the engine's resumable checkpoints (GET /checkpoints) into history.
 *
 * Only the engine knows whether a session's paid batches are still on disk,
 * so it decides `resumable`. A row last seen running whose job died with the
 * app or the engine becomes interrupted with the checkpoint's progress,
 * instead of a zombie that later reads as a lost failure. A checkpoint with
 * no row (its record was lost) gets one, so the work can still be resumed.
 * Pure: the caller owns fetching and storing.
 */
export function reconcileCheckpoints(
  sessions: SessionRecord[],
  checkpoints: CheckpointSummary[],
  now = Date.now(),
): SessionRecord[] {
  const known = new Set(sessions.map((session) => session.id));
  // A lost record re-enters as a stale "running" row and is settled below
  // exactly like one the app was closed on.
  const orphans = checkpoints
    .filter((checkpoint) => checkpoint.state !== "running" && !known.has(checkpoint.session_id))
    .map(
      (checkpoint): SessionRecord => ({
        id: checkpoint.session_id,
        modpackPath: checkpoint.modpack_path,
        modpackName: checkpoint.modpack_name || checkpoint.modpack_path || "Modpack",
        sourceLocale: checkpoint.source_locale,
        targetLocale: checkpoint.target_locale,
        model: checkpoint.model,
        status: "running",
        createdAt: parseTimestamp(checkpoint.created_at) ?? now,
        finishedAt: null,
        doneEntries: 0,
        totalEntries: 0,
        translateJobId: null,
        scanTotals: null,
        stats: null,
        error: null,
        exportZipPath: null,
        exportOverridesZipPath: null,
        exportBilingualZipPath: null,
        exportBilingualOverridesZipPath: null,
        sharedUrl: null,
        resumable: false,
      }),
    );
  const byId = new Map(checkpoints.map((checkpoint) => [checkpoint.session_id, checkpoint]));
  return [...orphans, ...sessions].map((session) => {
    const checkpoint = byId.get(session.id);
    if (checkpoint === undefined) {
      return session.resumable ? { ...session, resumable: false } : session;
    }
    // A live run reattaches through the normal restore path; a finished one
    // already holds its full result.
    if (checkpoint.state === "running" || session.status === "done") return session;
    const progress = {
      doneEntries: checkpoint.translated_entries,
      totalEntries: checkpoint.total_entries ?? session.totalEntries,
      resumable: true,
    };
    if (session.status !== "running") return { ...session, ...progress };
    return {
      ...session,
      ...progress,
      status:
        checkpoint.state === "cancelled" || checkpoint.state === "failed"
          ? checkpoint.state
          : "interrupted",
      finishedAt: parseTimestamp(checkpoint.updated_at) ?? now,
      translateJobId: null,
      error: checkpoint.error,
    };
  });
}

/**
 * Rows kept in history. Every progress tick rewrites the whole list into
 * localStorage, which has a quota of a few MB: without a bound a long-lived
 * install eventually fails to persist at all.
 */
export const MAX_SESSIONS = 500;

/** Newest-first list trimmed to MAX_SESSIONS, never dropping live or resumable work. */
export function capSessions(sessions: SessionRecord[]): SessionRecord[] {
  if (sessions.length <= MAX_SESSIONS) return sessions;
  const keep = (s: SessionRecord): boolean => s.status === "running" || s.resumable;
  let budget = MAX_SESSIONS - sessions.filter(keep).length;
  return sessions.filter((s) => {
    if (keep(s)) return true;
    budget -= 1;
    return budget >= 0;
  });
}

export const useSessions = create<SessionsStore>()(
  persist(
    (set) => ({
      sessions: [],
      upsert: (record) =>
        set((state) => ({
          sessions: capSessions([record, ...state.sessions.filter((s) => s.id !== record.id)]),
        })),
      patch: (id, patch) =>
        set((state) => ({
          sessions: state.sessions.map((s) => (s.id === id ? { ...s, ...patch } : s)),
        })),
      remove: (id) => set((state) => ({ sessions: state.sessions.filter((s) => s.id !== id) })),
    }),
    {
      name: "moru-sessions",
      version: 4,
      // Records persisted by older builds may miss later-added fields
      // (undefined breaks the `=== null` checks the UI relies on).
      // Zustand passes the old schema version as migrate's second argument;
      // do not accidentally use that small integer as an epoch timestamp.
      migrate: (persisted) => migrateSessionsState(persisted),
      // migrate only runs on a version change, but a crash inside
      // startTranslate's await window persists a running row with no job id
      // at the current version too - merge always runs, so normalize here.
      merge: (persisted, current) => ({ ...current, ...migrateSessionsState(persisted) }),
    },
  ),
);

/** Aggregates for the home dashboard stat strip. */
export function aggregateStats(sessions: SessionRecord[]): {
  totalTranslated: number;
  translatedThisWeek: number;
  tmHits: number;
  completedPacks: number;
  sharedPacks: number;
} {
  const weekAgo = Date.now() - 7 * 24 * 3600 * 1000;
  let totalTranslated = 0;
  let translatedThisWeek = 0;
  let tmHits = 0;
  let completedPacks = 0;
  let sharedPacks = 0;
  for (const s of sessions) {
    const translated = s.stats?.translated_entries ?? 0;
    totalTranslated += translated;
    if ((s.finishedAt ?? s.createdAt) >= weekAgo) translatedThisWeek += translated;
    tmHits += s.stats?.tm_hits ?? 0;
    if (s.status === "done") completedPacks += 1;
    if (s.sharedUrl !== null) sharedPacks += 1;
  }
  return { totalTranslated, translatedThisWeek, tmHits, completedPacks, sharedPacks };
}
