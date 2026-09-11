/**
 * Persisted translation session history. The engine's JobManager is
 * in-memory per run, so the desktop keeps its own durable record of every
 * wizard session - this feeds the home dashboard and the history screen.
 */

import { create } from "zustand";
import { persist } from "zustand/middleware";

import type { PipelineStats } from "../../../shared/engine";

export type SessionStatus = "running" | "done" | "failed" | "cancelled";

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
      };
    }),
  };
}

export const useSessions = create<SessionsStore>()(
  persist(
    (set) => ({
      sessions: [],
      upsert: (record) =>
        set((state) => ({
          sessions: [record, ...state.sessions.filter((s) => s.id !== record.id)],
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
