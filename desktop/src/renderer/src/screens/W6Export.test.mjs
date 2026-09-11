/**
 * Upload attribution contract: the share link lands on the session that
 * STARTED the upload, even when the wizard was reopened onto another
 * session (or reset) before the upload job finished.
 */
import { expect, test } from "bun:test";

// W6Export.tsx transitively imports lib/bridge (window.moru) and lib/web
// (window.localStorage) at module scope; merge globals before importing
// (other test files in the same bun process may have installed them).
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

// stores/account.ts hydrates from moru.secrets at import time - fill the
// cached bridge singleton before W6Export pulls it in.
const bridge = await import("../lib/bridge.ts");
bridge.moru.secrets ??= {
  get: async () => null,
  set: async () => undefined,
  delete: async () => undefined,
};
const { makeUploadFrameHandler } = await import("./W6Export.tsx");
const { useSessions } = await import("../stores/sessions.ts");
const { useWizard } = await import("../stores/wizard.ts");

function record(id) {
  return {
    id,
    modpackPath: `/packs/${id}`,
    modpackName: `Pack ${id}`,
    sourceLocale: "en_us",
    targetLocale: "ko_kr",
    model: "openai/gpt-test",
    status: "done",
    createdAt: 1_000,
    finishedAt: 2_000,
    doneEntries: 1,
    totalEntries: 1,
    stats: null,
    error: null,
    exportZipPath: null,
    exportOverridesZipPath: null,
    exportBilingualZipPath: null,
    exportBilingualOverridesZipPath: null,
    sharedUrl: null,
  };
}

test("done frame patches the launching session, not the current one", () => {
  useSessions.setState({ sessions: [record("upload-a"), record("upload-b")] });
  // The user reopened session B while A's upload was still running.
  useWizard.setState({ sessionId: "upload-b" });

  const phases = [];
  const handler = makeUploadFrameHandler("upload-a", (p) => phases.push(p));
  handler({ type: "done", status: "done", url: "https://moru.gg/p/1" });

  const byId = Object.fromEntries(useSessions.getState().sessions.map((s) => [s.id, s]));
  expect(byId["upload-a"].sharedUrl).toBe("https://moru.gg/p/1");
  expect(byId["upload-b"].sharedUrl).toBe(null); // no cross-session bleed
  expect(phases).toEqual([{
    kind: "done",
    url: "https://moru.gg/p/1",
    modpackSlug: null,
    modPacks: null,
  }]);
});

test("wizard reset before completion still attributes the link", () => {
  useSessions.setState({ sessions: [record("upload-c")] });
  useWizard.setState({ sessionId: null }); // reset happened mid-upload
  const handler = makeUploadFrameHandler("upload-c", () => undefined);
  handler({ type: "done", status: "done", url: "https://moru.gg/p/2" });
  expect(useSessions.getState().sessions[0].sharedUrl).toBe("https://moru.gg/p/2");
});

test("failed and cancelled frames surface an error phase without patching", () => {
  useSessions.setState({ sessions: [record("upload-d")] });
  const phases = [];
  const handler = makeUploadFrameHandler("upload-d", (p) => phases.push(p));
  handler({ type: "failed", status: "failed", error: "boom" });
  handler({ type: "cancelled", status: "cancelled" });
  expect(phases[0]).toEqual({ kind: "failed", message: "boom" });
  expect(phases[1].kind).toBe("failed");
  expect(useSessions.getState().sessions[0].sharedUrl).toBe(null);
});

test("per-mod progress starts its own denominator and keeps the share result", () => {
  useSessions.setState({ sessions: [record("upload-mods")] });
  let phase;
  const handler = makeUploadFrameHandler("upload-mods", (next) => { phase = next; });
  handler({ type: "progress", stage: "pack", done: 4, total: 4 });
  expect(phase).toEqual({ kind: "running", stage: "pack", current: 4, total: 4, message: "" });
  handler({ type: "progress", stage: "mods", current: 1, total: 3, message: "Create" });
  expect(phase).toEqual({
    kind: "running", stage: "mods", current: 1, total: 3, message: "Create",
  });
  expect(useSessions.getState().sessions[0].sharedUrl).toBe(null);
  handler({
    type: "done",
    status: "done",
    url: "https://moru.gg/ko/pack/123",
    modpack_slug: "all-the-mods-10",
    mod_packs: { shared: 2, skipped: 1, failed: 0 },
  });
  expect(phase).toEqual({
    kind: "done",
    url: "https://moru.gg/ko/pack/123",
    modpackSlug: "all-the-mods-10",
    modPacks: { shared: 2, skipped: 1, failed: 0 },
  });
  expect(useSessions.getState().sessions[0].sharedUrl).toBe("https://moru.gg/ko/pack/123");
});

test("anonymous uploads succeed while explaining why per-mod packs were skipped", () => {
  let phase;
  const handler = makeUploadFrameHandler(null, (next) => { phase = next; });
  handler({
    type: "done",
    status: "done",
    url: "https://moru.gg/ko/pack/anon",
    mod_packs: { shared: 0, skipped: 12, failed: 0, skipped_reason: "login_required" },
  });
  expect(phase.kind).toBe("done");
  expect(phase.url).toBe("https://moru.gg/ko/pack/anon");
  expect(phase.modPacks).toEqual({
    shared: 0, skipped: 12, failed: 0, skipped_reason: "login_required",
  });
});
