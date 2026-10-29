/** Translation-session restore contracts: engine snapshot first, then a
 * cursor-based live WebSocket for running work or a result probe for review. */
import { beforeEach, expect, test } from "bun:test";

const storage = new Map();
const localStorageStub = {
  getItem: (key) => storage.get(key) ?? null,
  setItem: (key, value) => void storage.set(key, String(value)),
  removeItem: (key) => void storage.delete(key),
};
globalThis.localStorage ??= localStorageStub;
const win = (globalThis.window ??= {});
win.localStorage ??= localStorageStub;
win.location ??= { search: "" };
win.setTimeout ??= globalThis.setTimeout;
// lib/bridge caches `window.moru ?? shim` at import; a partial stub here
// silently strips fields (platform) from every test file that loads later.
win.moru ??= {
  platform: "win32",
  versions: { app: "test", electron: "test" },
  setBusy: () => undefined,
  secrets: { get: async () => null },
};

const fetchCalls = [];
let nextResponse = null;
/** url substring -> response, consulted before the shared nextResponse. */
const routeResponses = new Map();
const responses = [];
globalThis.fetch = async (url) => {
  const href = String(url);
  fetchCalls.push(href);
  for (const [needle, response] of routeResponses) {
    if (href.includes(needle)) return response;
  }
  const queued = responses.shift();
  if (queued !== undefined) return queued;
  if (nextResponse !== null) return nextResponse;
  throw new Error(`unexpected fetch: ${url}`);
};

const sockets = [];
class FakeWebSocket {
  constructor(url) {
    this.url = url;
    this.onmessage = null;
    this.onclose = null;
    sockets.push(this);
  }
  close() {}
  emit(frame) {
    this.onmessage?.({ data: JSON.stringify(frame) });
  }
  /** Engine-side close (a sidecar restart), not our own unsubscribe. */
  emitClose() {
    this.onclose?.({});
  }
}
globalThis.WebSocket = FakeWebSocket;

const {
  buildTranslateParams,
  deleteHistorySession,
  reconcileResumableSessions,
  selectedScanTotals,
  useWizard,
  useSessionJobs,
} = await import("./wizard.ts");
const { useSessions } = await import("./sessions.ts");
const { useEngineStore } = await import("./engine.ts");
const bridge = await import("../lib/bridge.ts");
bridge.moru.setBusy = () => undefined;

useEngineStore.setState({
  info: { state: "ready", port: 59999, token: "tk", restarts: 0 },
});

const totals = {
  files: 2,
  entries: 50,
  chars: 500,
  migrationEntries: 10,
  migrationChars: 100,
  translationEntries: 40,
  translationChars: 400,
};

const runSettings = {
  outputDir: null,
  model: "openai/gpt-test",
  temperature: 0.3,
  batchSize: 30,
  maxConcurrent: 4,
  maxRefine: 2,
  thinkingEnabled: false,
  thinkingEffort: "medium",
  useTm: true,
  useVanillaGlossary: true,
  extractGlossary: true,
  glossaryMaxTerms: 3000,
  ollamaBaseUrl: "http://localhost:11434",
  openaiCompatBaseUrl: "http://localhost:1234/v1",
  targetLocale: "ko_kr",
};

const translateState = {
  modpackPath: "C:/packs/current",
  sourceLocale: "en_us",
  targetLocale: "ko_kr",
  migrationEnabled: false,
  previousModpackPath: "C:/packs/old",
  previousResourcepackPath: "C:/translations/old-rp.zip",
  previousOverridesPath: "C:/translations/old-overrides.zip",
  scanJobId: "w2-scan",
  scanResult: null,
  excludedCategories: [],
};

function record(id, status, overrides = {}) {
  return {
    id,
    modpackPath: `/packs/${id}`,
    modpackName: `Pack ${id}`,
    sourceLocale: "en_us",
    targetLocale: "ko_kr",
    model: "openai/gpt-test",
    status,
    createdAt: 1_000,
    finishedAt: status === "running" ? null : 2_000,
    doneEntries: 42,
    totalEntries: 50,
    translateJobId: null,
    scanTotals: totals,
    stats: status === "running" ? null : stats,
    error: null,
    exportZipPath: status === "done" ? "/exports/pack.zip" : null,
    exportOverridesZipPath: null,
    sharedUrl: null,
    ...overrides,
  };
}

function probe(name) {
  return {
    exists: true,
    isDirectory: true,
    name,
    hasMods: true,
    hasConfig: true,
    hasKubejs: false,
    hasResourcepacks: false,
    modJarCount: 3,
  };
}

const stats = {
  total_files: 2,
  total_entries: 50,
  translated_entries: 30,
  failed_entries: 2,
  tm_hits: 8,
  migration_hits: 10,
  skipped_entries: 0,
  categories: {},
  prompt_tokens: 100,
  completion_tokens: 20,
  duration_seconds: 1,
  coverage_percent: 96,
  quality_score: 0.96,
};

function response(body, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 404 ? "Not Found" : "OK",
    json: async () => body,
  };
}

function snapshot(id, status, events = [], overrides = {}) {
  return {
    job: {
      id,
      type: "translate",
      status,
      error: status === "failed" ? "provider failed" : null,
      created_at: new Date(1_000).toISOString(),
    },
    cursor: events.at(-1)?.seq ?? 0,
    events,
    failed_count: 0,
    translation_started_at: 1_200,
    ...overrides,
  };
}

const okPage = response({ entries: [], total: 0, page: 1 });
const notFound = response({ detail: "unknown job" }, 404);

beforeEach(() => {
  useWizard.getState().reset();
  useSessionJobs.setState({ jobs: {} });
  useSessions.setState({ sessions: [] });
  fetchCalls.length = 0;
  responses.length = 0;
  nextResponse = null;
  routeResponses.clear();
  sockets.length = 0;
});

test("refuses to clobber a different run in flight", async () => {
  useWizard.setState({ sessionId: "other", runState: "running" });
  useSessions.getState().upsert(record("s1", "done", { translateJobId: "job-1" }));
  expect(await useWizard.getState().reopenSession("s1")).toBe("busy");
  expect(fetchCalls).toHaveLength(0);
});

test("selected scan totals separate migration reuse inside included categories", () => {
  const scanResult = {
    categories: [
      {
        name: "Included",
        file_count: 2,
        entry_count: 7,
        char_count: 70,
        files: [
          { migration_entry_count: 2, migration_char_count: 20 },
          { migration_entry_count: 1, migration_char_count: 5 },
        ],
      },
      {
        name: "Excluded",
        file_count: 1,
        entry_count: 4,
        char_count: 40,
        files: [{ migration_entry_count: 4, migration_char_count: 40 }],
      },
    ],
  };
  expect(selectedScanTotals({ scanResult, excludedCategories: ["Excluded"] })).toEqual({
    files: 2,
    entries: 7,
    chars: 70,
    migrationEntries: 3,
    migrationChars: 25,
    translationEntries: 4,
    translationChars: 45,
  });
});

test("uses compact persisted totals after the full scan tree is gone", () => {
  expect(
    selectedScanTotals({ scanResult: null, scanTotals: totals, excludedCategories: [] }),
  ).toEqual(totals);
});

test("ordinary queued/manual translation reuses the completed v1.0 W2 scan", () => {
  const params = buildTranslateParams(translateState, runSettings, "secret");
  expect(params.scan_job_id).toBe("w2-scan");
  expect(params.previous_modpack_path).toBeUndefined();
  expect(params.previous_resourcepack_path).toBeUndefined();
  expect(params.previous_overrides_path).toBeUndefined();
  expect(params.include_categories).toBeUndefined();
});

test("A/B/C migration alone reuses its W2 scan and previous inputs", () => {
  const params = buildTranslateParams(
    { ...translateState, migrationEnabled: true },
    runSettings,
  );
  expect(params.scan_job_id).toBe("w2-scan");
  expect(params.previous_modpack_path).toBe("C:/packs/old");
  expect(params.previous_resourcepack_path).toEndWith("old-rp.zip");
  expect(params.previous_overrides_path).toEndWith("old-overrides.zip");
});

test("review stat updates reach both the live wizard and persisted session", () => {
  useSessions.getState().upsert(
    record("s1", "done", {
      translateJobId: "job-1",
      exportZipPath: "/exports/old.zip",
      exportOverridesZipPath: "/exports/old_overrides.zip",
    }),
  );
  useWizard.setState({
    sessionId: "s1",
    stats,
    exportState: "done",
    exportZipPath: "/exports/old.zip",
    exportOverridesZipPath: "/exports/old_overrides.zip",
  });
  const refreshed = {
    ...stats,
    translated_entries: 31,
    migration_hits: 9,
    coverage_percent: 96,
  };

  useWizard.getState().updateReviewStats(refreshed);

  expect(useWizard.getState().stats).toEqual(refreshed);
  expect(useSessions.getState().sessions[0].stats).toEqual(refreshed);
  expect(useWizard.getState().exportState).toBe("idle");
  expect(useWizard.getState().exportZipPath).toBe(null);
  expect(useWizard.getState().exportOverridesZipPath).toBe(null);
  expect(useSessions.getState().sessions[0].exportZipPath).toBe(null);
  expect(useSessions.getState().sessions[0].exportOverridesZipPath).toBe(null);
});

test("falls back to the persisted session id before reporting a missing job", async () => {
  useSessions.getState().upsert(record("s1", "done"));
  responses.push(notFound, notFound);
  fetchCalls.length = 0;
  expect(await useWizard.getState().reopenSession("s1")).toBe("gone");
  expect(fetchCalls).toHaveLength(2);
  expect(fetchCalls[0]).toContain("/jobs/s1/snapshot");
  expect(fetchCalls[1]).toContain("/sessions/s1/restore");
});

test("hydrates a finished run from snapshot then verifies review result", async () => {
  useSessions.getState().upsert(record("s1", "done", { translateJobId: "job-1" }));
  responses.push(
    response(
      snapshot("job-1", "done", [
        {
          type: "progress",
          stage: "translate",
          file: "a.json",
          done: 50,
          total: 50,
          seq: 1,
          emitted_at: 1_500,
        },
        {
          type: "tokens",
          prompt_tokens: 100,
          completion_tokens: 20,
          seq: 2,
          emitted_at: 1_800,
        },
        { type: "done", status: "done", stats, seq: 3, emitted_at: 2_000 },
      ]),
    ),
    okPage,
    notFound,
  );

  expect(await useWizard.getState().reopenSession("s1")).toBe("ok");
  expect(fetchCalls[0]).toContain("/jobs/job-1/snapshot");
  expect(fetchCalls[1]).toContain("/translate/job-1/entries");
  const wizard = useWizard.getState();
  expect(wizard.runState).toBe("done");
  expect(wizard.scanTotals).toEqual(totals);
  expect(wizard.fileProgress["a.json"].done).toBe(50);
  expect(wizard.promptTokens).toBe(100);
  expect(wizard.stats.coverage_percent).toBe(96);
  expect(wizard.exportState).toBe("done");
});

test("restores a running job and consumes only post-snapshot events", async () => {
  useSessions.getState().upsert(record("s1", "running", { translateJobId: "job-live" }));
  responses.push(
    response(
      snapshot(
        "job-live",
        "running",
        [
          {
            type: "progress",
            stage: "translate",
            file: "a.json",
            done: 20,
            total: 40,
            seq: 8,
            emitted_at: 1_500,
          },
          {
            type: "batch_started",
            request_id: 4,
            file: "a.json",
            key: "entry.a",
            entries: 5,
            seq: 9,
            emitted_at: 1_600,
          },
        ],
        { cursor: 9, failed_count: 12 },
      ),
    ),
    notFound,
  );

  expect(await useWizard.getState().reopenSession("s1")).toBe("ok");
  expect(useWizard.getState().runState).toBe("running");
  expect(useWizard.getState().doneEntries).toBe(20);
  expect(useWizard.getState().failedEntryCount).toBe(12);
  expect(Object.keys(useWizard.getState().activeBatches)).toEqual(["4"]);
  expect(sockets).toHaveLength(1);
  expect(sockets[0].url).toContain("after=9");

  sockets[0].emit({
    type: "progress",
    stage: "translate",
    file: "a.json",
    done: 25,
    total: 40,
    seq: 10,
    emitted_at: 1_700,
  });
  expect(useWizard.getState().doneEntries).toBe(25);
});

test("invalidates a persisted running job when the sidecar lost it", async () => {
  useSessions.getState().upsert(record("s1", "running", { translateJobId: "dead" }));
  useWizard.setState({ sessionId: "s1", runState: "running", translateJobId: "dead" });
  responses.push(notFound);

  expect(await useWizard.getState().reopenSession("s1")).toBe("gone");
  const stored = useSessions.getState().sessions.find((item) => item.id === "s1");
  expect(stored.translateJobId).toBe(null);
  expect(stored.status).toBe("failed");
  expect(useWizard.getState().runState).toBe("failed");
});

test("failed history records are not reopened", async () => {
  useSessions.getState().upsert(record("s1", "failed", { translateJobId: "job-1" }));
  expect(await useWizard.getState().reopenSession("s1")).toBe("gone");
  expect(fetchCalls).toHaveLength(0);
});

test("restores the scan screen only when the session carries a scan payload", async () => {
  const scanPayload = { modpack_path: "/packs/s4", categories: [], identity: null };
  useSessions.getState().upsert(record("s4", "done"));
  useSessionJobs.getState().register("s4", "job-4");
  routeResponses.set("/jobs/job-4/snapshot", response(snapshot("job-4", "done")));
  routeResponses.set("/translate/job-4/entries", okPage);
  routeResponses.set("/scan/job-4/result", response(scanPayload));

  expect(await useWizard.getState().reopenSession("s4")).toBe("ok");
  expect(useWizard.getState().scanResult).toEqual(scanPayload);
  expect(useWizard.getState().scanState).toBe("done"); // otherwise w3 is unreachable

  // A different session with no persisted payload: the scan screen stays
  // idle rather than half-restored. (Reopening s4 again would short-circuit
  // on the already-live path and keep its state.)
  useSessions.getState().upsert(record("s5", "done", { scanTotals: null }));
  useSessionJobs.getState().register("s5", "job-5");
  routeResponses.set("/jobs/job-5/snapshot", response(snapshot("job-5", "done")));
  routeResponses.set("/translate/job-5/entries", okPage);
  routeResponses.set("/scan/job-5/result", notFound);
  expect(await useWizard.getState().reopenSession("s5")).toBe("ok");
  expect(useWizard.getState().scanResult).toBe(null);
  expect(useWizard.getState().scanState).toBe("idle");
  routeResponses.clear();
});

test("a rejected scan result probe still settles the scan state", async () => {
  useWizard.getState().startSession("C:/packs/new", probe("New Pack"), "ko_kr");
  routeResponses.set("/jobs", response({ id: "scan-1", type: "scan", status: "pending" }));
  routeResponses.set("/scan/scan-1/result", notFound);

  await useWizard.getState().startScan();
  expect(useWizard.getState().scanState).toBe("running");
  sockets.at(-1).emit({ type: "done" });
  await new Promise((resolve) => setTimeout(resolve, 0));

  // Otherwise the queue runner's waitForWizard never resolves and the whole
  // drain loop deadlocks with no in-app escape.
  expect(useWizard.getState().scanState).toBe("failed");
  expect(useWizard.getState().scanError).toContain("unknown job");
  routeResponses.clear();
});

test("a dropped scan socket settles the scan instead of parking it", async () => {
  const scanPayload = { modpack_path: "C:/packs/new", categories: [], identity: null };
  useWizard.getState().startSession("C:/packs/new", probe("New Pack"), "ko_kr");
  routeResponses.set("/jobs", response({ id: "scan-2", type: "scan", status: "pending" }));
  routeResponses.set("/scan/scan-2/result", response(scanPayload));

  await useWizard.getState().startScan();
  sockets.at(-1).emitClose();
  await new Promise((resolve) => setTimeout(resolve, 0));

  expect(useWizard.getState().scanState).toBe("done");
  expect(useWizard.getState().scanResult).toEqual(scanPayload);
  routeResponses.clear();
});

test("entry failures from different files stay separate rows", () => {
  useWizard.setState({ sessionId: "s1", runState: "running", translateJobId: "job-1" });
  const frame = { type: "entry_failed", key: "display_name.0001", errors: ["schema"] };
  useWizard.getState().handleTranslationFrame({ ...frame, file: "a.js" }, "s1");
  useWizard.getState().handleTranslationFrame({ ...frame, file: "b.js" }, "s1");
  // The screens only read Object.keys(failedKeys).length, so a bare key would
  // report one failure for every KubeJS script that minted the same id.
  expect(Object.keys(useWizard.getState().failedKeys)).toEqual([
    "a.js:display_name.0001",
    "b.js:display_name.0001",
  ]);

  // Replayed snapshots and older engines carry no file: still one row each.
  useWizard.getState().handleTranslationFrame(
    { type: "entry_failed", key: "legacy.key", errors: ["boom"] },
    "s1",
  );
  expect(useWizard.getState().failedKeys["legacy.key"]).toEqual(["boom"]);
});

test("reset keeps the runtime job registry so history can still reopen", () => {
  useSessionJobs.getState().register("s1", "job-1");
  useWizard.setState({ sessionId: "s1" });
  useWizard.getState().reset();
  expect(useWizard.getState().sessionId).toBe(null);
  expect(useSessionJobs.getState().jobs.s1).toBe("job-1"); // affordance survives
});

test("a failed probe of an unrelated session leaves the live wizard alone", async () => {
  useSessions.getState().upsert(record("live", "done", { translateJobId: "job-live" }));
  useSessions.getState().upsert(record("dead", "cancelled", { translateJobId: "job-dead" }));
  useSessionJobs.getState().register("dead", "job-dead");
  routeResponses.set("/jobs/job-live/snapshot", response(snapshot("job-live", "done")));
  routeResponses.set("/translate/job-live/entries", okPage);
  routeResponses.set("/scan/job-live/result", notFound);
  expect(await useWizard.getState().reopenSession("live")).toBe("ok");
  useWizard.setState({ failedKeys: { "block.x": ["schema"] } }); // richer in-run state

  nextResponse = notFound;
  expect(await useWizard.getState().reopenSession("dead")).toBe("gone");
  expect(useSessionJobs.getState().jobs.dead).toBeUndefined(); // affordance revoked
  expect(useWizard.getState().sessionId).toBe("live"); // untouched
  expect(useWizard.getState().failedKeys["block.x"]).toEqual(["schema"]);
  routeResponses.clear();
});

test("a quota interruption ends the run resumable, never as a failure", () => {
  useSessions.getState().upsert(record("s1", "running", { translateJobId: "job-1", resumable: false }));
  useWizard.setState({ sessionId: "s1", runState: "running", translateJobId: "job-1" });
  useWizard.getState().handleTranslationFrame(
    {
      type: "interrupted",
      status: "interrupted",
      reason: "quota",
      error: "AI 제공자의 사용량 한도에 도달해 번역을 일시 중지했습니다.",
      detail: "429 RESOURCE_EXHAUSTED",
      resumable: true,
      stats,
      seq: 5,
      emitted_at: 3_000,
    },
    "s1",
  );
  const wizard = useWizard.getState();
  expect(wizard.runState).toBe("interrupted");
  expect(wizard.resumable).toBe(true);
  expect(wizard.interruptReason).toBe("quota");
  const stored = useSessions.getState().sessions.find((item) => item.id === "s1");
  expect(stored.status).toBe("interrupted");
  expect(stored.resumable).toBe(true);
});

test("resume sends only the session id and the flag, then streams the job", async () => {
  useSessions.getState().upsert(record("s1", "interrupted", { resumable: true }));
  // Another suite may have loaded the bridge first with a stub lacking secrets.
  bridge.moru.secrets = { get: async () => null };
  const posted = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    posted.push({ url: String(url), body: JSON.parse(init.body) });
    return response(
      { id: "s1", type: "translate", status: "pending", error: null, created_at: new Date(0).toISOString() },
      201,
    );
  };
  try {
    expect(await useWizard.getState().resumeTranslate("s1")).toBe("started");
  } finally {
    globalThis.fetch = originalFetch;
  }
  // Everything that shapes the output comes from the engine's checkpoint.
  expect(posted).toHaveLength(1);
  expect(posted[0].url).toContain("/jobs");
  expect(posted[0].body).toEqual({ type: "translate", params: { session_id: "s1", resume: true } });
  expect(useWizard.getState().runState).toBe("running");
  expect(useWizard.getState().translateJobId).toBe("s1");
  expect(sockets).toHaveLength(1);
  expect(useSessions.getState().sessions[0].status).toBe("running");
});

test("a session without a checkpoint is never resumed", async () => {
  useSessions.getState().upsert(record("s2", "cancelled", { resumable: false }));
  expect(await useWizard.getState().resumeTranslate("s2")).toBe("gone");
  expect(fetchCalls).toHaveLength(0);
  expect(useWizard.getState().runState).toBe("idle");
});

test("a lost running job with a surviving checkpoint reads as interrupted", async () => {
  useSessions.getState().upsert(record("s1", "running", { translateJobId: "dead", resumable: false }));
  useWizard.setState({ sessionId: "s1", runState: "running", translateJobId: "dead" });
  routeResponses.set(
    "/checkpoints",
    response([
      {
        session_id: "s1",
        modpack_path: "/packs/s1",
        modpack_name: "Pack s1",
        source_locale: "en_us",
        target_locale: "ko_kr",
        model: "openai/gpt-test",
        created_at: null,
        updated_at: null,
        state: "interrupted",
        reason: null,
        error: null,
        translated_entries: 30,
        total_entries: 50,
      },
    ]),
  );
  nextResponse = notFound;

  expect(await useWizard.getState().reopenSession("s1")).toBe("gone");
  const stored = useSessions.getState().sessions.find((item) => item.id === "s1");
  expect(stored.status).toBe("interrupted");
  expect(stored.resumable).toBe(true);
  expect(stored.doneEntries).toBe(30);
  expect(useWizard.getState().runState).toBe("interrupted");
  expect(useWizard.getState().resumable).toBe(true);
});

test("deleting a resumable session from Home keeps it gone after reconcile", async () => {
  useSessions.getState().upsert(record("s1", "interrupted", { resumable: true }));
  useSessionJobs.getState().register("s1", "s1");
  // A stateful engine: the checkpoint lives until DELETE /sessions drops it.
  let checkpoints = [
    {
      session_id: "s1",
      modpack_path: "/packs/s1",
      modpack_name: "Pack s1",
      source_locale: "en_us",
      target_locale: "ko_kr",
      model: "openai/gpt-test",
      created_at: null,
      updated_at: null,
      state: "interrupted",
      reason: "quota",
      error: null,
      translated_entries: 30,
      total_entries: 50,
    },
  ];
  const deletes = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url, init = {}) => {
    const href = String(url);
    if (init.method === "DELETE" && href.includes("/sessions/s1")) {
      deletes.push(href);
      checkpoints = [];
      return response({ status: "ok", id: "s1" });
    }
    if (href.includes("/checkpoints")) return response(checkpoints);
    throw new Error(`unexpected fetch: ${href}`);
  };
  try {
    await deleteHistorySession("s1");
    await reconcileResumableSessions();
  } finally {
    globalThis.fetch = originalFetch;
  }
  expect(deletes).toHaveLength(1);
  expect(useSessions.getState().sessions).toEqual([]);
  expect(useSessionJobs.getState().jobs.s1).toBeUndefined();
});

test("a refused fresh run leaves the interrupted session resumable", async () => {
  bridge.moru.secrets = { get: async () => null };
  useSessions.getState().upsert(record("s1", "interrupted", { resumable: true }));
  useWizard.setState({
    ...translateState,
    sessionId: "s1",
    modpackName: "Pack s1",
    runState: "interrupted",
    resumable: true,
  });
  routeResponses.set("/community/sync", response({ tm: null, glossary: null }));
  routeResponses.set("/jobs", response({ detail: "job s1 is still running" }, 409));

  await useWizard.getState().startTranslate(runSettings);

  // The engine never accepted the run, so it never discarded the checkpoint.
  expect(useWizard.getState().runState).toBe("failed");
  expect(useWizard.getState().resumable).toBe(true);
  expect(useSessions.getState().sessions.find((item) => item.id === "s1").resumable).toBe(true);
});

test("a double click on start launches one translation, not two", async () => {
  routeResponses.set("/community/sync", response({ tm: null, glossary: null }));
  routeResponses.set("/jobs", response({ id: "job-x", type: "translate", status: "running" }));
  useWizard.setState({ ...translateState, modpackName: "Pack" });
  await Promise.all([
    useWizard.getState().startTranslate(runSettings),
    useWizard.getState().startTranslate(runSettings),
  ]);
  expect(fetchCalls.filter((url) => url.endsWith("/jobs")).length).toBe(1);
});

test("a delete the engine refuses keeps the history row", async () => {
  useSessions.getState().upsert(record("s1", "done"));
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () =>
    new Response(JSON.stringify({ detail: "job s1 is still running" }), { status: 409 });
  let outcome;
  try {
    outcome = await deleteHistorySession("s1");
  } finally {
    globalThis.fetch = originalFetch;
  }
  expect(outcome).toBe("running");
  expect(useSessions.getState().sessions.map((s) => s.id)).toEqual(["s1"]);
});
