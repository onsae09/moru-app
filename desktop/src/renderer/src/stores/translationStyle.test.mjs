/**
 * Translation-style params: speech level (말투) and term rendering.
 *
 * Two properties matter and both have a real failure mode:
 *
 * - An untouched run must post the SAME params it posted before these
 *   controls existed. They default to the engine's own defaults, so a key
 *   carrying that default is pure noise on the wire; W3 users who change
 *   nothing must get byte-identical output.
 * - `speech_level` must not ride along into a non-Korean run. The engine
 *   renders that directive only for `target_lang.startswith("ko")`, so
 *   sending a value for a Japanese target would record an intent the run
 *   never honoured.
 */
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
win.moru ??= {};

const { buildTranslateParams } = await import("./wizard.ts");
const { snapshotTranslationSettings, useSettings } = await import("./settings.ts");

/** A wizard state sitting on W3 after a scan, with no migration inputs. */
const STATE = {
  modpackPath: "/packs/atm10",
  sourceLocale: "en_us",
  targetLocale: "ko_kr",
  migrationEnabled: false,
  previousModpackPath: null,
  previousResourcepackPath: null,
  previousOverridesPath: null,
  scanJobId: "scan-1",
  scanResult: null,
  excludedCategories: [],
};

beforeEach(() => {
  useSettings.setState({
    speechLevel: "auto",
    termStyle: "auto",
  });
});

/** What actually goes on the wire: JSON drops `undefined` values. */
function postedParams(state = STATE) {
  return JSON.parse(JSON.stringify(buildTranslateParams(state, snapshotTranslationSettings())));
}

test("a queued settings snapshot keeps its style when live preferences change", () => {
  useSettings.setState({ speechLevel: "hage", termStyle: "transliterate" });
  const snapshot = snapshotTranslationSettings();
  useSettings.setState({ speechLevel: "polite", termStyle: "translate" });
  const params = buildTranslateParams(STATE, snapshot);
  expect(params.speech_level).toBe("hage");
  expect(params.term_style).toBe("transliterate");
});

test("defaults post no style keys at all", () => {
  const params = postedParams();
  expect(params).not.toHaveProperty("speech_level");
  expect(params).not.toHaveProperty("term_style");
  expect(params).not.toHaveProperty("bilingual_names");
});

test("a chosen speech level reaches the job params", () => {
  for (const level of ["polite", "banmal", "hage"]) {
    useSettings.setState({ speechLevel: level });
    expect(postedParams().speech_level).toBe(level);
  }
});

test("a chosen term style reaches the job params", () => {
  for (const style of ["translate", "transliterate"]) {
    useSettings.setState({ termStyle: style });
    expect(postedParams().term_style).toBe(style);
  }
});

test("rehydration discards the old bilingual opt-out without losing other preferences", async () => {
  const previous = useSettings.getState();
  try {
    globalThis.localStorage.setItem("moru-settings", JSON.stringify({
      state: { bilingualNames: false, speechLevel: "hage", termStyle: "transliterate" },
      version: 0,
    }));
    await useSettings.persist.rehydrate();
    expect(useSettings.getState()).not.toHaveProperty("bilingualNames");
    expect(postedParams()).not.toHaveProperty("bilingual_names");
    expect(postedParams().speech_level).toBe("hage");
    expect(postedParams().term_style).toBe("transliterate");
  } finally {
    useSettings.setState(previous, true);
  }
});

test("both style choices ride along together, and nothing else moves", () => {
  useSettings.setState({
    speechLevel: "polite",
    termStyle: "translate",
  });
  const params = postedParams();
  expect(params.speech_level).toBe("polite");
  expect(params.term_style).toBe("translate");
  // The rest of the request is untouched by the style axes.
  expect(params.target_locale).toBe("ko_kr");
  expect(params.use_tm).toBe(true);
  expect(params.scan_job_id).toBe("scan-1");
});

/**
 * The Korean gate. `term_style` is deliberately NOT gated: the engine renders
 * that block for every target, phrased against "the target language".
 */
test("speech level is dropped for a non-Korean target, term style is not", () => {
  useSettings.setState({ speechLevel: "hage", termStyle: "translate" });
  for (const locale of ["ja_jp", "zh_cn", "zh_tw"]) {
    const params = postedParams({ ...STATE, targetLocale: locale });
    expect(params).not.toHaveProperty("speech_level");
    expect(params.term_style).toBe("translate");
  }
});

test("the Korean gate is a prefix test, matching the engine's own", () => {
  useSettings.setState({ speechLevel: "banmal" });
  // The engine checks `target_lang.startswith("ko")`, not equality with ko_kr.
  expect(postedParams({ ...STATE, targetLocale: "ko_kr" }).speech_level).toBe("banmal");
  expect(postedParams({ ...STATE, targetLocale: "ko" }).speech_level).toBe("banmal");
});
