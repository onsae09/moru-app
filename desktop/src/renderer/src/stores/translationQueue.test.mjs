import { expect, test } from "bun:test";

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
win.moru ??= {
  platform: "win32",
  versions: { app: "test", electron: "test" },
  setBusy: () => undefined,
  secrets: { get: async () => null },
};

const {
  appendUniqueQueueItems,
  drainTranslationQueue,
  initializeTranslationQueue,
  movePendingQueueItem,
  normalizePersistedQueueState,
  retryQueueItem,
  useTranslationQueue,
} = await import("./translationQueue.ts");
const { useWizard } = await import("./wizard.ts");

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

function item(id, status = "pending", path = `C:\\Packs\\${id}`) {
  return {
    id,
    path,
    name: id,
    status,
    sessionId: null,
    error: status === "failed" ? "failed" : null,
    addedAt: 1,
    startedAt: null,
    finishedAt: null,
  };
}

test("deduplicates Windows paths across case, slash, and trailing separators", () => {
  let id = 0;
  const result = appendUniqueQueueItems(
    [],
    [
      { path: "C:\\Packs\\Alpha", name: "Alpha" },
      { path: "c:/packs/alpha/", name: "Duplicate" },
      { path: "C:\\Packs\\Beta", name: "Beta" },
    ],
    "win32",
    () => `id-${++id}`,
    () => 100,
  );
  expect(result.map((entry) => entry.name)).toEqual(["Alpha", "Beta"]);
});

test("moves only among pending items and leaves active or finished rows fixed", () => {
  const original = [item("done", "done"), item("a"), item("active", "translating"), item("b")];
  const moved = movePendingQueueItem(original, "b", -1);
  expect(moved.map((entry) => entry.id)).toEqual(["done", "b", "active", "a"]);
  expect(movePendingQueueItem(moved, "active", -1)).toBe(moved);
});

test("retry resets only failed and cancelled items", () => {
  const failed = { ...item("a", "failed"), sessionId: "old", startedAt: 2, finishedAt: 3 };
  expect(retryQueueItem(failed)).toMatchObject({
    status: "pending",
    sessionId: null,
    error: null,
    startedAt: null,
    finishedAt: null,
  });
  const done = item("done", "done");
  expect(retryQueueItem(done)).toBe(done);
});

test("persisted running queues restart paused without discarding the active item", () => {
  const restored = normalizePersistedQueueState({
    items: [item("active", "translating")],
    phase: "running",
    settingsSnapshot: null,
    lastError: null,
  });
  expect(restored.phase).toBe("paused");
  expect(restored.items[0].status).toBe("translating");
});

test("drains packs strictly one at a time and continues after a failure", async () => {
  let phase = "running";
  let items = [item("a"), item("b"), item("c")];
  const order = [];
  let inFlight = 0;
  let maxInFlight = 0;

  await drainTranslationQueue({
    getPhase: () => phase,
    getNext: () => items.find((entry) => entry.status === "pending"),
    run: async (entry) => {
      order.push(entry.id);
      inFlight += 1;
      maxInFlight = Math.max(maxInFlight, inFlight);
      await Promise.resolve();
      inFlight -= 1;
      if (entry.id === "b") throw new Error("provider failed");
      return { status: "done" };
    },
    settle: (entry, outcome) => {
      items = items.map((candidate) =>
        candidate.id === entry.id
          ? { ...candidate, status: outcome.status, error: outcome.error ?? null }
          : candidate,
      );
    },
    setPhase: (next) => {
      phase = next;
    },
  });

  expect(order).toEqual(["a", "b", "c"]);
  expect(maxInFlight).toBe(1);
  expect(items.map((entry) => entry.status)).toEqual(["done", "failed", "done"]);
  expect(phase).toBe("complete");
});

test("pause request waits for the current item and leaves the next pending", async () => {
  let phase = "running";
  let items = [item("a"), item("b")];

  await drainTranslationQueue({
    getPhase: () => phase,
    getNext: () => items.find((entry) => entry.status === "pending"),
    run: async () => {
      phase = "pausing";
      return { status: "done" };
    },
    settle: (entry, outcome) => {
      items = items.map((candidate) =>
        candidate.id === entry.id ? { ...candidate, status: outcome.status } : candidate,
      );
    },
    setPhase: (next) => {
      phase = next;
    },
  });

  expect(items.map((entry) => entry.status)).toEqual(["done", "pending"]);
  expect(phase).toBe("paused");
});

test("a finished pack no longer blocks re-adding the same folder", () => {
  const result = appendUniqueQueueItems(
    [item("old", "done", "C:\\Packs\\Alpha"), item("live", "translating", "C:\\Packs\\Beta")],
    [
      { path: "c:/packs/alpha/", name: "Alpha again" },
      { path: "C:\\Packs\\Beta", name: "Beta again" },
    ],
    "win32",
    () => "re-added",
    () => 100,
  );
  // Terminal rows stay as history; only the active row still reserves its path.
  expect(result.map((entry) => entry.name)).toEqual(["old", "live", "Alpha again"]);
});

test("retry refuses a failed row whose folder is already queued again", () => {
  useTranslationQueue.setState({
    items: [
      item("stale", "failed", "C:\\Packs\\Alpha"),
      item("fresh", "pending", "C:/Packs/Alpha/"),
    ],
    phase: "idle",
    settingsSnapshot: null,
    lastError: null,
    ready: true,
  });
  useTranslationQueue.getState().retry("stale");
  // Two pending rows for one folder would translate - and bill - it twice.
  expect(useTranslationQueue.getState().items.map((entry) => entry.status)).toEqual([
    "failed",
    "pending",
  ]);

  useTranslationQueue.getState().remove("fresh");
  useTranslationQueue.getState().retry("stale");
  expect(useTranslationQueue.getState().items.map((entry) => entry.status)).toEqual(["pending"]);
});

test("a malformed settings snapshot normalizes to null", () => {
  expect(normalizePersistedQueueState({ settingsSnapshot: runSettings }).settingsSnapshot).toEqual(
    runSettings,
  );
  // Otherwise a resumed queue sends model: undefined into the engine params.
  const { model: _model, ...truncated } = runSettings;
  expect(normalizePersistedQueueState({ settingsSnapshot: truncated }).settingsSnapshot).toBe(null);
  expect(normalizePersistedQueueState({ settingsSnapshot: "nope" }).settingsSnapshot).toBe(null);
  expect(
    normalizePersistedQueueState({ settingsSnapshot: { ...runSettings, batchSize: "30" } })
      .settingsSnapshot,
  ).toBe(null);
});

test("cancelling the running pack stops the queue instead of starting the next", async () => {
  let phase = "running";
  let items = [item("a"), item("b")];

  await drainTranslationQueue({
    getPhase: () => phase,
    getNext: () => items.find((entry) => entry.status === "pending"),
    run: async () => ({ status: "cancelled" }),
    settle: (entry, outcome) => {
      items = items.map((candidate) =>
        candidate.id === entry.id ? { ...candidate, status: outcome.status } : candidate,
      );
    },
    setPhase: (next) => {
      phase = next;
    },
  });

  // W4's cancel button is the only cancel affordance a queued run has; it must
  // not read as "skip this pack" and spend on the next one unattended.
  expect(items.map((entry) => entry.status)).toEqual(["cancelled", "pending"]);
  expect(phase).toBe("paused");
});

test("clearing an active queue releases the wizard so the runner can exit", () => {
  useTranslationQueue.setState({
    items: [item("a", "translating"), item("b")],
    phase: "running",
    settingsSnapshot: runSettings,
    lastError: null,
    ready: true,
  });
  useWizard.setState({ sessionId: "s1", runState: "running", translateJobId: "job-1" });

  useTranslationQueue.getState().clear();

  const state = useTranslationQueue.getState();
  expect(state.items).toEqual([]);
  expect(state.phase).toBe("idle");
  expect(state.settingsSnapshot).toBe(null);
  // runQueueItem's waits key off the session, so dropping it is what unparks
  // the drain loop when a wizard state machine gets stuck.
  expect(useWizard.getState().sessionId).toBe(null);
  expect(useWizard.getState().runState).toBe("idle");
});

test("a restored queue never auto-resumes and re-snapshots current settings", async () => {
  useTranslationQueue.setState({
    items: [item("a"), item("b")],
    phase: "idle",
    settingsSnapshot: { ...runSettings, model: "stale/model" },
    lastError: null,
    ready: false,
  });

  await initializeTranslationQueue();

  const state = useTranslationQueue.getState();
  expect(state.ready).toBe(true);
  // A queue that never ran must not read as resumable, or the start button
  // says "Resume" and relocks the settings a previous run finished with.
  expect(state.phase).toBe("idle");
  expect(state.settingsSnapshot).toBe(null);
  expect(state.items.map((entry) => entry.status)).toEqual(["pending", "pending"]);
});

test("one pack may wait once per target language", () => {
  let id = 0;
  const result = appendUniqueQueueItems(
    [],
    [
      { path: "/packs/alpha", name: "Alpha", targetLocale: "ko_kr" },
      { path: "/packs/alpha", name: "Alpha", targetLocale: "ja_jp" },
      { path: "/packs/alpha", name: "Alpha", targetLocale: "ja_jp" },
    ],
    "linux",
    () => `id-${++id}`,
    () => 100,
  );
  expect(result.map((entry) => entry.targetLocale)).toEqual(["ko_kr", "ja_jp"]);
});
