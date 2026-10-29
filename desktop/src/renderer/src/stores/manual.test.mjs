/**
 * Hand-translation queue contracts.
 *
 * The ordering rules are load-bearing for translation quality, not cosmetics:
 * mods split one sentence across numbered keys, and a translator shown
 * `desc2` alone — or shown the parts out of order — will mistranslate it. The
 * grouping must match the engine's `_NUMBERED_KEY_RE` behaviour exactly.
 */
import { expect, test } from "bun:test";

// manual.ts transitively imports lib/api -> stores/engine, and persist()
// touches localStorage at module scope. Merge globals rather than replacing
// them: other test files in the same bun process may have set them up first.
const storage = new Map();
const localStorageStub = {
  getItem: (k) => storage.get(k) ?? null,
  setItem: (k, v) => void storage.set(k, String(v)),
  removeItem: (k) => void storage.delete(k),
};
globalThis.localStorage ??= localStorageStub;
const win = (globalThis.window ??= {});
win.localStorage ??= localStorageStub;
win.location ??= { search: "" };
win.moru ??= {};

const {
  contentKind,
  fileLabel,
  groupQueue,
  orderQueue,
  refId,
  sameRun,
  visibleQueue,
} = await import("./manual.ts");

const entry = (key, file = "a.snbt", extra = {}) => ({
  key,
  file,
  source_text: `src ${key}`,
  translated_text: "",
  status: "pending",
  errors: [],
  ...extra,
});

/* ---- numbered runs -------------------------------------------------- */

test("orderQueue pulls a numbered run together and sorts it by ordinal", () => {
  const out = orderQueue([
    entry("quest.desc2"),
    entry("quest.title"),
    entry("quest.desc3"),
    entry("quest.desc1"),
  ]).map((e) => e.key);
  // The run takes its first member's position (desc2 came first), and the
  // non-numbered key keeps its own place after it.
  expect(out).toEqual(["quest.desc1", "quest.desc2", "quest.desc3", "quest.title"]);
});

test("orderQueue sorts numerically, not lexically", () => {
  const out = orderQueue([
    entry("tip10"),
    entry("tip2"),
    entry("tip1"),
  ]).map((e) => e.key);
  expect(out).toEqual(["tip1", "tip2", "tip10"]);
});

test("orderQueue handles bracketed ordinals", () => {
  const out = orderQueue([entry("pages[2]"), entry("pages[1]")]).map((e) => e.key);
  expect(out).toEqual(["pages[1]", "pages[2]"]);
});

test("a numbered run does not span two files", () => {
  const out = orderQueue([
    entry("desc1", "one.snbt"),
    entry("desc1", "two.snbt"),
    entry("desc2", "one.snbt"),
  ]);
  expect(out.map((e) => `${e.file}:${e.key}`)).toEqual([
    "one.snbt:desc1",
    "one.snbt:desc2",
    "two.snbt:desc1",
  ]);
});

test("separator stays in the stem, so tooltip2 and tooltip.2 are distinct runs", () => {
  // Mirrors the engine: the stem keeps its separator, so these must not merge.
  expect(sameRun("tooltip2", "tooltip.2")).toBe(false);
  expect(sameRun("tooltip1", "tooltip2")).toBe(true);
  expect(sameRun("tooltip.1", "tooltip.2")).toBe(true);
});

test("a key with no trailing ordinal is never part of a run", () => {
  expect(sameRun("title", "title")).toBe(false);
  expect(sameRun("desc1", "title")).toBe(false);
});

test("a bare ordinal has no stem and forms no run", () => {
  // `1` and `2` share an empty stem; treating them as siblings would group
  // unrelated keys, so parseNumberedKey rejects an empty stem.
  expect(sameRun("1", "2")).toBe(false);
});

/* ---- content kind --------------------------------------------------- */

test("contentKind names the string's kind, ignoring the ordinal", () => {
  expect(contentKind("quest.3.desc2")).toBe("desc");
  expect(contentKind("quest.3.title")).toBe("title");
  expect(contentKind("pages[1].text")).toBe("text");
  expect(contentKind("tooltip4")).toBe("tooltip");
  expect(contentKind("item.mekanism.ingot_osmium")).toBe("ingot_osmium");
});

/* ---- file labels ---------------------------------------------------- */

test("fileLabel reads the namespace out of a resource-pack path", () => {
  expect(fileLabel("mods/mekanism.jar/assets/mekanism/lang/en_us.json")).toEqual({
    mod: "mekanism",
    name: "en_us",
  });
});

test("fileLabel falls back to the segment under a container directory", () => {
  expect(fileLabel("config/ftbquests/chapters/intro.snbt")).toEqual({
    mod: "ftbquests",
    name: "intro",
  });
  expect(fileLabel("mods/create.jar")).toEqual({ mod: "create", name: "create" });
});

test("fileLabel degrades to the first segment for an unrecognized layout", () => {
  expect(fileLabel("weird/place/thing.json")).toEqual({ mod: "weird", name: "thing" });
});

/* ---- grouping ------------------------------------------------------- */

test("groupQueue breaks on file and on content kind, preserving order", () => {
  const groups = groupQueue([
    { file: "a.snbt", key: "q.desc1" },
    { file: "a.snbt", key: "q.desc2" },
    { file: "a.snbt", key: "q.title" },
    { file: "b.snbt", key: "q.title" },
  ]);
  expect(groups.map((g) => [g.file, g.kind, g.refs.length])).toEqual([
    ["a.snbt", "desc", 2],
    ["a.snbt", "title", 1],
    ["b.snbt", "title", 1],
  ]);
});

/* ---- flagged view --------------------------------------------------- */

test("visibleQueue is the full snapshot until flaggedOnly narrows it", () => {
  const refs = [
    { file: "a.snbt", key: "one" },
    { file: "a.snbt", key: "two" },
  ];
  const flags = { job1: [refId(refs[1])] };
  expect(visibleQueue({ refs, flaggedOnly: false, flags, jobId: "job1" })).toHaveLength(2);
  const narrowed = visibleQueue({ refs, flaggedOnly: true, flags, jobId: "job1" });
  expect(narrowed).toEqual([refs[1]]);
});

test("visibleQueue ignores flaggedOnly with no open job", () => {
  const refs = [{ file: "a.snbt", key: "one" }];
  expect(visibleQueue({ refs, flaggedOnly: true, flags: {}, jobId: null })).toEqual(refs);
});

test("flags are scoped per job", () => {
  const refs = [{ file: "a.snbt", key: "one" }];
  const flags = { other: [refId(refs[0])] };
  expect(visibleQueue({ refs, flaggedOnly: true, flags, jobId: "job1" })).toEqual([]);
});

test("a page requested under the old filter never lands in the new list", async () => {
  const { useManual } = await import("./manual.ts");
  const { useEngineStore } = await import("./engine.ts");
  useEngineStore.setState({ info: { state: "ready", port: 1, token: "t", restarts: 0 } });
  const pending = [];
  const realFetch = globalThis.fetch;
  globalThis.fetch = (url) =>
    new Promise((resolve) => pending.push({ url: String(url), resolve }));
  const page = (keys) =>
    new Response(JSON.stringify({ entries: keys.map((k) => entry(k)), total: keys.length }), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  try {
    useManual.setState({ jobId: "job", filter: "all", refs: [], entries: {}, pagesLoaded: 0, total: 0, loading: false });
    const first = useManual.getState().loadMore();
    const second = useManual.getState().setFilter("pending");
    await new Promise((r) => setTimeout(r, 0));
    // The new filter's page answers first, the stale one afterwards.
    pending[1].resolve(page(["new.a"]));
    await second;
    pending[0].resolve(page(["old.a", "old.b"]));
    await first;
    expect(useManual.getState().refs.map((r) => r.key)).toEqual(["new.a"]);
    expect(useManual.getState().loading).toBe(false);
  } finally {
    globalThis.fetch = realFetch;
    // One bun process runs every test file: hand the next one a clean store.
    useManual.setState({ jobId: null, filter: "all", refs: [], entries: {}, pagesLoaded: 0, total: 0 });
  }
});

test("restoring a session keeps its drafts under the new job id", async () => {
  const { useManual } = await import("./manual.ts");
  const { useWizard } = await import("./wizard.ts");
  useWizard.setState({ sessionId: "sess" });
  useManual.setState({ sessionJobs: { sess: "old-job" }, drafts: { "old-job": { "a.snbt\u0000k": "초안" } }, flags: {} });
  const realFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response(JSON.stringify({ entries: [], total: 0, patterns: [] }), { status: 200 });
  try {
    await useManual.getState().open("new-job");
    expect(useManual.getState().drafts["new-job"]).toEqual({ "a.snbt\u0000k": "초안" });
    expect(useManual.getState().drafts["old-job"]).toBeUndefined();
  } finally {
    globalThis.fetch = realFetch;
    useManual.setState({ jobId: null, drafts: {}, flags: {}, sessionJobs: {}, refs: [], entries: {} });
    useWizard.setState({ sessionId: null });
  }
});
