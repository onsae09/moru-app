/** Screen router - global navigation state for hub screens and the wizard. */

import { create } from "zustand";

export type Screen =
  | "onboarding"
  | "home"
  | "w1"
  | "w2"
  | "w3"
  | "w4"
  | "w5"
  | "w5m"
  | "w6"
  | "queue"
  | "history"
  | "glossary"
  | "settings";

/**
 * `w5m` (hand translation) is an alternate route through step 05, not a
 * seventh step: it shares the review step's position in the rail so the
 * numbered progression stays 1..6.
 */
export const WIZARD_SCREENS: readonly Screen[] = ["w1", "w2", "w3", "w4", "w5", "w5m", "w6"];

export type SettingsTab = "models" | "general" | "account" | "about";

/** localStorage flag: present once the first-run onboarding was finished or skipped. */
const ONBOARDED_KEY = "moru:onboarded";

interface RouterStore {
  screen: Screen;
  /** First-run onboarding done; App renders the fullscreen wizard while false. */
  onboarded: boolean;
  /** Tab the settings screen opens on: models unless a deep link chose one. */
  settingsTab: SettingsTab;
  go: (screen: Screen) => void;
  /** Opens settings on one tab (W1's "런처 폴더 설정" → general). */
  openSettings: (tab: SettingsTab) => void;
  /** Sets the persistent flag and lands on home (finish and skip both call this). */
  completeOnboarding: () => void;
  /** Clears the flag and re-enters the wizard (Settings > General > replay). */
  replayOnboarding: () => void;
}

export const useRouter = create<RouterStore>((set) => ({
  screen: "home",
  onboarded: window.localStorage.getItem(ONBOARDED_KEY) !== null,
  settingsTab: "models",
  go: (screen) => set({ screen, settingsTab: "models" }),
  openSettings: (tab) => set({ screen: "settings", settingsTab: tab }),
  completeOnboarding: () => {
    window.localStorage.setItem(ONBOARDED_KEY, "1");
    set({ onboarded: true, screen: "home" });
  },
  replayOnboarding: () => {
    window.localStorage.removeItem(ONBOARDED_KEY);
    set({ onboarded: false, screen: "onboarding" });
  },
}));
