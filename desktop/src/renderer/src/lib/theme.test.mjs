import { afterEach, expect, test } from "bun:test";

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

const { useSettings } = await import("../stores/settings.ts");
const { resolveTheme } = await import("./theme.ts");
const merge = useSettings.persist.getOptions().merge;

const originalMatchMedia = win.matchMedia;
afterEach(() => {
  win.matchMedia = originalMatchMedia;
});

function prefersLight(light) {
  win.matchMedia = (query) => ({
    matches: light && query === "(prefers-color-scheme: light)",
    addEventListener() {},
    removeEventListener() {},
  });
}

test("system follows prefers-color-scheme; explicit choices ignore it", () => {
  prefersLight(true);
  expect(resolveTheme("system")).toBe("light");
  expect(resolveTheme("dark")).toBe("dark");
  prefersLight(false);
  expect(resolveTheme("system")).toBe("dark");
  expect(resolveTheme("light")).toBe("light");
});

test("system without matchMedia falls back to dark", () => {
  win.matchMedia = undefined;
  expect(resolveTheme("system")).toBe("dark");
});

test("stored themes survive hydration; unknown values fall back to dark", () => {
  const current = useSettings.getState();
  expect(merge({ theme: "light" }, current).theme).toBe("light");
  expect(merge({ theme: "system" }, current).theme).toBe("system");
  expect(merge({ theme: "sepia" }, current).theme).toBe("dark");
  expect(merge({}, current).theme).toBe("dark");
});
