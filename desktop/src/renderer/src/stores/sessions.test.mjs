import { expect, test } from "bun:test";

const storage = new Map();
const localStorageStub = {
  getItem: (key) => storage.get(key) ?? null,
  setItem: (key, value) => void storage.set(key, String(value)),
  removeItem: (key) => void storage.delete(key),
};
globalThis.localStorage ??= localStorageStub;
// zustand's persist reads window.localStorage: without it the store silently
// loses its storage (and its persist api) for every later test file too.
const win = (globalThis.window ??= {});
win.localStorage ??= localStorageStub;

const { migrateSessionsState, reconcileCheckpoints, useSessions } = await import("./sessions.ts");

function checkpoint(sessionId, overrides = {}) {
  return {
    session_id: sessionId,
    modpack_path: "C:/packs/tensura",
    modpack_name: "Tensura Neo Otherworld",
    source_locale: "en_us",
    target_locale: "ko_kr",
    model: "gemini/gemini-3.1-pro-preview",
    created_at: "2026-10-17T10:00:00+00:00",
    updated_at: "2026-10-17T11:00:00+00:00",
    state: "interrupted",
    reason: null,
    error: null,
    translated_entries: 1200,
    total_entries: 5000,
    ...overrides,
  };
}

function legacyRecord(status) {
  return {
    id: "legacy",
    modpackPath: "C:/pack",
    modpackName: "Pack",
    sourceLocale: "en_us",
    targetLocale: "ko_kr",
    model: "openai/gpt-test",
    status,
    createdAt: 1_000,
    finishedAt: null,
    doneEntries: 12,
    totalEntries: 20,
  };
}

test("marks legacy running records without a job id as unrecoverable", () => {
  const migrated = migrateSessionsState(
    { sessions: [legacyRecord("running")] },
    5_000,
  );
  const record = migrated.sessions[0];
  expect(record.status).toBe("failed");
  expect(record.finishedAt).toBe(5_000);
  expect(record.translateJobId).toBe(null);
  expect(record.scanTotals).toBe(null);
  expect(record.error).toContain("복원할 수 없습니다");
});

test("keeps a persisted running record recoverable when it has a job id", () => {
  const migrated = migrateSessionsState({
    sessions: [
      {
        ...legacyRecord("running"),
        translateJobId: "job-live",
        scanTotals: { entries: 20 },
      },
    ],
  });
  const record = migrated.sessions[0];
  expect(record.status).toBe("running");
  expect(record.translateJobId).toBe("job-live");
  expect(record.scanTotals.entries).toBe(20);
  expect(record.error).toBe(null);
});

test("a running record with no job id is de-zombified on every hydration", async () => {
  // Current version, so migrate never fires - but the zombie is still
  // reachable at v4 when the app dies inside startTranslate's await window.
  window.localStorage.setItem(
    "moru-sessions",
    JSON.stringify({ version: 4, state: { sessions: [legacyRecord("running")] } }),
  );
  await useSessions.persist.rehydrate();

  const record = useSessions.getState().sessions[0];
  expect(record.status).toBe("failed");
  expect(record.finishedAt).not.toBe(null);
  expect(typeof useSessions.getState().upsert).toBe("function"); // actions survive merge
});

test("a row the app was closed on becomes interrupted with its saved progress", () => {
  const [record] = reconcileCheckpoints(
    [{ ...legacyRecord("running"), id: "s1", translateJobId: "stale-job", resumable: false }],
    [checkpoint("s1")],
    9_000,
  );
  expect(record.status).toBe("interrupted");
  expect(record.resumable).toBe(true);
  expect(record.doneEntries).toBe(1200);
  expect(record.totalEntries).toBe(5000);
  expect(record.translateJobId).toBe(null);
  expect(record.finishedAt).toBe(Date.parse("2026-10-17T11:00:00+00:00"));
});

test("reconcile keeps live and finished runs and drops resume once the checkpoint is gone", () => {
  const live = { ...legacyRecord("running"), id: "live", translateJobId: "job", resumable: false };
  const stale = { ...legacyRecord("cancelled"), id: "old", resumable: true };
  const quota = { ...legacyRecord("interrupted"), id: "quota", resumable: false };
  const reconciled = reconcileCheckpoints(
    [live, stale, quota],
    [
      checkpoint("live", { state: "running" }),
      checkpoint("quota", { reason: "quota", translated_entries: 7, total_entries: 9 }),
    ],
  );
  const byId = Object.fromEntries(reconciled.map((record) => [record.id, record]));
  // A live engine job reattaches through the normal restore path instead.
  expect(byId.live).toBe(live);
  expect(byId.old.resumable).toBe(false);
  expect(byId.quota).toMatchObject({ status: "interrupted", resumable: true, doneEntries: 7 });
});

test("a checkpoint whose history row was lost is listed again", () => {
  const reconciled = reconcileCheckpoints([], [checkpoint("lost", { state: "cancelled" })], 5);
  expect(reconciled).toHaveLength(1);
  expect(reconciled[0]).toMatchObject({
    id: "lost",
    modpackName: "Tensura Neo Otherworld",
    status: "cancelled",
    resumable: true,
    doneEntries: 1200,
    totalEntries: 5000,
  });
});

test("history is capped without dropping live or resumable rows", async () => {
  const { capSessions, MAX_SESSIONS } = await import("./sessions.ts");
  const rows = Array.from({ length: MAX_SESSIONS + 10 }, (_, i) => ({
    id: `s${i}`,
    status: i === MAX_SESSIONS + 9 ? "running" : "done",
    resumable: i === MAX_SESSIONS + 8,
  }));
  const kept = capSessions(rows);
  expect(kept).toHaveLength(MAX_SESSIONS);
  expect(kept.map((s) => s.id)).toContain(`s${MAX_SESSIONS + 9}`);
  expect(kept.map((s) => s.id)).toContain(`s${MAX_SESSIONS + 8}`);
  expect(kept[0].id).toBe("s0");
});
