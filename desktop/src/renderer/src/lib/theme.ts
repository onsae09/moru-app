/**
 * Theme plumbing: resolves the persisted `theme` setting ("system" follows
 * prefers-color-scheme) and mirrors it onto `<html data-theme>`, which is
 * what styles/global.css keys the light token block on.
 */

import { useEffect, useState } from "react";

import { useSettings, type ThemeSetting } from "../stores/settings";

export type ResolvedTheme = "dark" | "light";

const LIGHT_QUERY = "(prefers-color-scheme: light)";

function systemPrefersLight(): boolean {
  return typeof window.matchMedia === "function" && window.matchMedia(LIGHT_QUERY).matches;
}

export function resolveTheme(setting: ThemeSetting): ResolvedTheme {
  if (setting === "light") return "light";
  if (setting === "system") return systemPrefersLight() ? "light" : "dark";
  return "dark";
}

function apply(setting: ThemeSetting): void {
  document.documentElement.dataset.theme = resolveTheme(setting);
}

let installed = false;

/**
 * Applies the theme now (before the first render, so a light user never sees
 * a dark flash; zustand's localStorage hydration is synchronous) and keeps it
 * in sync with the setting and, for "system", with the OS preference.
 */
export function installTheme(): void {
  if (installed) return;
  installed = true;
  apply(useSettings.getState().theme);
  useSettings.subscribe((state, prev) => {
    if (state.theme !== prev.theme) apply(state.theme);
  });
  if (typeof window.matchMedia === "function") {
    window.matchMedia(LIGHT_QUERY).addEventListener("change", () => {
      apply(useSettings.getState().theme);
    });
  }
}

/** The theme actually on screen; re-renders when it changes. */
export function useResolvedTheme(): ResolvedTheme {
  const setting = useSettings((s) => s.theme);
  const [systemLight, setSystemLight] = useState(systemPrefersLight);
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const mq = window.matchMedia(LIGHT_QUERY);
    const onChange = () => setSystemLight(mq.matches);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);
  if (setting === "system") return systemLight ? "light" : "dark";
  return setting === "light" ? "light" : "dark";
}

/** Current value of a CSS custom property on <html> (for canvas renderers). */
export function cssVar(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

/** `#rrggbb` + alpha (0–1) -> `rgba(...)`; passes anything else through. */
export function withAlpha(hex: string, alpha: number): string {
  const m = /^#([0-9a-f]{6})$/i.exec(hex);
  if (m === null) return hex;
  const n = parseInt(m[1], 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${alpha})`;
}
