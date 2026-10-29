import { expect, test } from "bun:test";

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

const { DEFAULT_MOD_BLACKLIST, useSettings } = await import("./settings.ts");
const merge = useSettings.persist.getOptions().merge;

test("a default added in a later release joins the saved blacklist once", () => {
  const [newDefault, ...older] = DEFAULT_MOD_BLACKLIST;
  const merged = merge(
    { modBlacklist: ["my-mod", ...older], seenDefaultBlacklist: older },
    useSettings.getState(),
  );
  expect(merged.modBlacklist).toEqual(["my-mod", ...older, newDefault]);
  expect(merged.seenDefaultBlacklist).toEqual([...DEFAULT_MOD_BLACKLIST]);
});

test("a default the user removed stays removed", () => {
  const [removed, ...kept] = DEFAULT_MOD_BLACKLIST;
  const merged = merge(
    { modBlacklist: kept, seenDefaultBlacklist: [...DEFAULT_MOD_BLACKLIST] },
    useSettings.getState(),
  );
  expect(merged.modBlacklist).not.toContain(removed);
});
