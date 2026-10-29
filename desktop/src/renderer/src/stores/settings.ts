/** Persisted UI/translation preferences (renderer-local). */

import { create } from "zustand";
import { persist } from "zustand/middleware";

import { LAUNCHER_IDS, type LauncherPaths } from "../../../shared/bridge";
import type { SpeechLevel, TermStyle } from "../../../shared/engine";

/**
 * Mods excluded from translation out of the box: libraries, optimization
 * mods and author tooling, whose strings only the modpack author ever
 * reads. Entries are **declared mod ids** (`fabric.mod.json:id`,
 * `mods.toml:modId`), the identifier that namespaces a mod's assets and
 * therefore every translation key it owns — jar file names and display
 * names both vary per build and per launcher. Every id below was read out
 * of that mod's real published jar.
 *
 * The user owns this list: it is persisted, editable on W2, and matching
 * ignores case and punctuation, so typing a display name ("Entity
 * Culling") still hits the id ("entityculling").
 */
export const DEFAULT_MOD_BLACKLIST: readonly string[] = [
  "jade", // Jade — in-world tooltip HUD
  "modernfix", // ModernFix — startup/memory optimization
  "craftpresence", // CraftPresence — Discord rich presence
  "kiwi", // Kiwi (Library) — Snownee's shared code
  "clumps", // Clumps — XP orb merging
  "sodium", // Sodium — rendering engine
  "entityculling", // Entity Culling — render culling
  "fancymenu", // FancyMenu — the author's own menu editor
  "chunky", // Chunky — chunk pregeneration command
  "bookshelf", // Bookshelf — Darkhax's shared code
];

export type PresetId = "fast" | "balanced" | "best";

/** Serializable translation inputs locked when an unattended queue starts. */
export interface TranslationRunSettings {
  outputDir: string | null;
  model: string;
  temperature: number;
  batchSize: number;
  maxConcurrent: number;
  maxRefine: number;
  thinkingEnabled: boolean;
  thinkingEffort: "low" | "medium" | "high";
  useTm: boolean;
  useVanillaGlossary: boolean;
  extractGlossary: boolean;
  glossaryMaxTerms: number | null;
  ollamaBaseUrl: string;
  openaiCompatBaseUrl: string;
  /**
   * Translation style, frozen with the rest for the same reason
   * `modBlacklist` is: a queued pack must run with the style that was in
   * force when the queue started, not whatever it is edited to mid-drain.
   */
  speechLevel: SpeechLevel;
  termStyle: TermStyle;
  targetLocale: string;
  /** Frozen with the rest: a queued run must use the blacklist that was
   *  in force when the queue started, not whatever it is edited to later. */
  modBlacklist: string[];
}

interface SettingsStore {
  uiLanguage: "ko" | "en";
  theme: "dark" | "light";
  outputDir: string | null;
  /** last-used translate settings, restored as W3 defaults */
  preset: PresetId | "custom";
  /** selected LLM provider id (W3 provider band); model belongs to it */
  provider: string;
  model: string;
  temperature: number;
  batchSize: number;
  maxConcurrent: number;
  maxRefine: number;
  /** Send litellm reasoning_effort so reasoning-capable models think. */
  thinkingEnabled: boolean;
  /** Effort level sent when thinking is enabled. */
  thinkingEffort: "low" | "medium" | "high";
  useTm: boolean;
  useVanillaGlossary: boolean;
  extractGlossary: boolean;
  /** Maximum mined candidates sent to glossary curation; null means unlimited. */
  glossaryMaxTerms: number | null;
  ollamaBaseUrl: string;
  /** OpenAI-compatible server (LM Studio, llama.cpp, vLLM) base URL incl. /v1 */
  openaiCompatBaseUrl: string;
  /**
   * Forced Korean speech level (말투). "auto" keeps the compiled prompt's own
   * per-surface register, which is today's behaviour. Inert on a non-Korean
   * target, so W3 disables the control there rather than pretending.
   */
  speechLevel: SpeechLevel;
  /** Meaning-translate vs transliterate for terms with no established name. */
  termStyle: TermStyle;
  targetLocale: string;
  /**
   * USD ceiling for one translation run (start or resume); reaching it
   * pauses the run resumably. null = no ceiling. Not enforceable for a
   * model with no known price.
   */
  budgetUsd: number | null;
  recentFolders: string[];
  /** Mod ids never translated; seeded from DEFAULT_MOD_BLACKLIST. */
  modBlacklist: string[];
  /**
   * DEFAULT_MOD_BLACKLIST as of the last launch. A default added in a later
   * release is appended to `modBlacklist` once; one the user removed stays
   * removed.
   */
  seenDefaultBlacklist: string[];
  /** Instance folders set per launcher in Settings; absent = the default. */
  launcherPaths: LauncherPaths;

  set: (patch: Partial<SettingsStore>) => void;
  rememberFolder: (path: string) => void;
}

export const useSettings = create<SettingsStore>()(
  persist(
    (set) => ({
      uiLanguage: "ko",
      theme: "dark",
      outputDir: null,
      preset: "balanced",
      provider: "anthropic",
      model: "anthropic/claude-haiku-5-5",
      temperature: 0.3,
      batchSize: 30,
      maxConcurrent: 15,
      maxRefine: 2,
      thinkingEnabled: false,
      thinkingEffort: "medium",
      useTm: true,
      useVanillaGlossary: true,
      extractGlossary: true,
      glossaryMaxTerms: 3000,
      ollamaBaseUrl: "http://localhost:11434",
      openaiCompatBaseUrl: "http://localhost:1234/v1",
      speechLevel: "auto",
      termStyle: "auto",
      budgetUsd: null,
      targetLocale: "ko_kr",
      recentFolders: [],
      modBlacklist: [...DEFAULT_MOD_BLACKLIST],
      seenDefaultBlacklist: [...DEFAULT_MOD_BLACKLIST],
      launcherPaths: {},

      set: (patch) => set(patch),
      rememberFolder: (path) =>
        set((state) => ({
          recentFolders: [path, ...state.recentFolders.filter((p) => p !== path)].slice(0, 8),
        })),
    }),
    {
      name: "moru-settings",
      merge: (persisted, current) => {
        const saved = {
          ...(persisted as Partial<SettingsStore> & { bilingualNames?: boolean }),
        };
        // Both variants are always generated, regardless of the old setting.
        delete saved.bilingualNames;
        // Hand-edited or older storage: keep only absolute folders (C:\…,
        // \\server\…, /…), which is all the main process will scan, so the
        // settings screen never shows a folder detect silently ignores.
        const launcherPaths: LauncherPaths = {};
        const storedPaths = saved.launcherPaths as unknown;
        if (typeof storedPaths === "object" && storedPaths !== null) {
          for (const launcher of LAUNCHER_IDS) {
            const value = (storedPaths as Record<string, unknown>)[launcher];
            if (typeof value === "string" && /^(?:[A-Za-z]:[\\/]|[\\/])/.test(value.trim())) {
              launcherPaths[launcher] = value.trim();
            }
          }
        }
        // Storage from before `seenDefaultBlacklist` existed cannot tell an
        // old default the user removed from a new one: treat the current
        // defaults as seen, so later additions are the ones that arrive.
        const seen = Array.isArray(saved.seenDefaultBlacklist)
          ? saved.seenDefaultBlacklist
          : [...DEFAULT_MOD_BLACKLIST];
        const blacklist = Array.isArray(saved.modBlacklist)
          ? saved.modBlacklist
          : [...DEFAULT_MOD_BLACKLIST];
        const added = DEFAULT_MOD_BLACKLIST.filter(
          (id) => !seen.includes(id) && !blacklist.includes(id),
        );
        return {
          ...current,
          ...saved,
          launcherPaths,
          modBlacklist: [...blacklist, ...added],
          seenDefaultBlacklist: [...DEFAULT_MOD_BLACKLIST],
        };
      },
    },
  ),
);

/** Excludes UI state, recent folders, and store actions from a queued run. */
export function snapshotTranslationSettings(): TranslationRunSettings {
  const state = useSettings.getState();
  return {
    outputDir: state.outputDir,
    model: state.model,
    temperature: state.temperature,
    batchSize: state.batchSize,
    maxConcurrent: state.maxConcurrent,
    maxRefine: state.maxRefine,
    thinkingEnabled: state.thinkingEnabled,
    thinkingEffort: state.thinkingEffort,
    useTm: state.useTm,
    useVanillaGlossary: state.useVanillaGlossary,
    extractGlossary: state.extractGlossary,
    glossaryMaxTerms: state.glossaryMaxTerms,
    ollamaBaseUrl: state.ollamaBaseUrl,
    openaiCompatBaseUrl: state.openaiCompatBaseUrl,
    speechLevel: state.speechLevel,
    termStyle: state.termStyle,
    targetLocale: state.targetLocale,
    modBlacklist: [...state.modBlacklist],
  };
}
