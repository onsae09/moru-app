/**
 * Shared entry-text presentation: status chips, placeholder-token
 * highlighting, and Minecraft §-code rendering.
 *
 * Extracted from W5Review so the review table and the manual translation
 * surface render an entry identically. A second copy of the token regex or
 * the § palette would drift, and the two screens would then disagree about
 * what counts as a placeholder — which is exactly the class of bug the
 * engine-side validator would catch only after the fact.
 */

import type { ReactNode } from "react";
import { useEffect, useReducer } from "react";

import type { EntryStatus } from "../../../shared/engine";

/* ---- status presentation --------------------------------------------- */

export const STATUS_COLOR: Record<EntryStatus, string> = {
  passed: "#3DDC84",
  warning: "#F5B454",
  failed: "#F26B6B",
  modified: "#6BB3F5",
  tm_hit: "#A78BFA",
  // Reuse shares one hue across the app; the glyph tells TM from migrated.
  migrated: "#A78BFA",
  skipped: "#6A7C74",
  // Deliberately brighter than `skipped`: an untranslated entry is work to
  // do, not work that was intentionally passed over.
  pending: "#8A9AA8",
};

/** i18n keys for each status, so both screens label them the same way. */
export const STATUS_LABEL_KEY: Record<EntryStatus, string> = {
  passed: "common.status.passed",
  warning: "common.status.warning",
  failed: "common.status.failed",
  modified: "common.status.modified",
  tm_hit: "common.status.tmHit",
  migrated: "common.status.migrated",
  skipped: "common.status.skipped",
  pending: "common.status.untranslated",
};

export function StatusIcon({
  status,
  size = 12,
}: {
  status: EntryStatus;
  size?: number;
}): ReactNode {
  const s = size - 4;
  let glyph: ReactNode;
  switch (status) {
    case "failed":
      glyph = (
        <svg width={s} height={s} viewBox="0 0 8 8" fill="none" stroke="#0A100D" strokeWidth="2">
          <path d="M2 2 L6 6 M6 2 L2 6" />
        </svg>
      );
      break;
    case "warning":
      glyph = (
        <svg width={s} height={s} viewBox="0 0 8 8" shapeRendering="crispEdges">
          <rect x="3" y="1" width="2" height="4" fill="#0A100D" />
          <rect x="3" y="6" width="2" height="1" fill="#0A100D" />
        </svg>
      );
      break;
    case "modified":
      glyph = (
        <svg width={s} height={s} viewBox="0 0 8 8" fill="none" stroke="#0A100D" strokeWidth="1.5">
          <path d="M1 6 L5 2 L7 4 L3 8 Z" fill="#0A100D" />
        </svg>
      );
      break;
    case "tm_hit":
      glyph = (
        <svg width={s} height={s} viewBox="0 0 8 8" shapeRendering="crispEdges">
          <rect x="1" y="1" width="6" height="2" fill="#0A100D" />
          <rect x="1" y="5" width="6" height="2" fill="#0A100D" />
        </svg>
      );
      break;
    case "migrated":
      glyph = (
        <svg width={s} height={s} viewBox="0 0 8 8" fill="none" stroke="#0A100D" strokeWidth="1.5">
          <path d="M1 2 H5 V1 L7 3 L5 5 V4 H1 Z" fill="#0A100D" />
          <path d="M7 6 H3 V7 L1 5 L3 3 V4 H7 Z" fill="#0A100D" />
        </svg>
      );
      break;
    case "skipped":
      glyph = (
        <svg width={s} height={s} viewBox="0 0 8 8" shapeRendering="crispEdges">
          <rect x="1" y="3" width="6" height="2" fill="#0A100D" />
        </svg>
      );
      break;
    case "pending":
      // Hollow: nothing has been written into this entry yet.
      glyph = (
        <svg width={s} height={s} viewBox="0 0 8 8" fill="none" stroke="#0A100D" strokeWidth="1.5">
          <rect x="1.5" y="1.5" width="5" height="5" />
        </svg>
      );
      break;
    default:
      glyph = (
        <svg width={s} height={s} viewBox="0 0 8 8" fill="none" stroke="#0A100D" strokeWidth="2">
          <path d="M1 4 L3 6 L7 2" />
        </svg>
      );
  }
  return (
    <div
      className="flex items-center justify-center"
      style={{ width: size, height: size, background: STATUS_COLOR[status] }}
    >
      {glyph}
    </div>
  );
}

/* ---- placeholder token highlighting ---------------------------------- */

/**
 * Fallback token grammar, used until the engine's patterns arrive and whenever
 * the sidecar is unreachable — a real state, which is why `EngineGate` exists.
 *
 * It is deliberately close to, but not identical with, the engine's `PATTERNS`:
 * it lacks Patchouli `$(...)` macros and `&`-colour codes. A translator could
 * delete one of those and the highlight would not warn them, while the engine
 * would fail the entry. That asymmetry is the whole reason
 * `installTokenPatterns` exists — the engine is the authority on what a
 * placeholder is, because the engine is what rejects the translation.
 */
const FALLBACK_TOKEN_SRC =
  String.raw`\{\{[^{}]*\}\}|\{[^{}]*\}|<[^<>]+>|%(?:\d+\$)?[A-Za-z]|§.|\\n`;

let TOKEN_SPLIT = new RegExp(`(${FALLBACK_TOKEN_SRC})`, "g");
let TOKEN_EXACT = new RegExp(`^(?:${FALLBACK_TOKEN_SRC})$`);

const patternListeners = new Set<() => void>();

/**
 * Adopt the engine's placeholder patterns (`GET /placeholder/patterns`).
 *
 * Declaration order is preserved because it IS the overlap priority: an
 * earlier pattern wins, which is how the engine keeps a `$(...)` macro whole
 * when its body happens to contain something that looks like a format
 * specifier.
 */
export function installTokenPatterns(
  patterns: readonly { regex: string }[],
): void {
  const source = patterns.map((p) => p.regex).join("|");
  if (source === "") return;
  try {
    TOKEN_SPLIT = new RegExp(`(${source})`, "g");
    TOKEN_EXACT = new RegExp(`^(?:${source})$`);
  } catch {
    // A pattern this browser's regex engine rejects must not break rendering;
    // the fallback grammar stays in place.
    return;
  }
  for (const notify of patternListeners) notify();
}

/**
 * Re-render when the engine's grammar replaces the fallback. Needed because
 * the patterns arrive after first paint, and the highlight would otherwise
 * stay stale until some unrelated state change happened to repaint.
 */
export function useTokenPatterns(): void {
  const [, bump] = useReducer((n: number) => n + 1, 0);
  useEffect(() => {
    patternListeners.add(bump);
    return () => {
      patternListeners.delete(bump);
    };
  }, [bump]);
}

/** accent -> blue -> purple -> amber, cycling; same token = same color. */
const TOKEN_PALETTE = [
  { color: "var(--color-accent)", bg: "rgba(61,220,132,0.15)", border: "rgba(61,220,132,0.3)" },
  { color: "var(--color-blue)", bg: "rgba(107,179,245,0.15)", border: "rgba(107,179,245,0.3)" },
  { color: "var(--color-purple)", bg: "rgba(167,139,250,0.15)", border: "rgba(167,139,250,0.3)" },
  { color: "var(--color-amber)", bg: "rgba(245,180,84,0.15)", border: "rgba(245,180,84,0.3)" },
];

export function tokenColorMap(...texts: string[]): Map<string, number> {
  const map = new Map<string, number>();
  for (const text of texts) {
    for (const part of text.split(TOKEN_SPLIT)) {
      if (part !== "" && TOKEN_EXACT.test(part) && !map.has(part)) {
        map.set(part, map.size % TOKEN_PALETTE.length);
      }
    }
  }
  return map;
}

/** Every placeholder token in `text`, in order, with duplicates kept. */
export function tokensOf(text: string): string[] {
  return text.split(TOKEN_SPLIT).filter((p) => p !== "" && TOKEN_EXACT.test(p));
}

export function TokenText({
  text,
  colors,
}: {
  text: string;
  colors: Map<string, number>;
}): ReactNode {
  const parts = text.split(TOKEN_SPLIT).filter((p) => p !== "");
  return (
    <>
      {parts.map((part, i) => {
        const idx = TOKEN_EXACT.test(part) ? colors.get(part) : undefined;
        if (idx === undefined) return <span key={i}>{part}</span>;
        const p = TOKEN_PALETTE[idx];
        return (
          <span
            key={i}
            className="border px-[3px] font-mono text-[11px]"
            style={{ background: p.bg, color: p.color, borderColor: p.border }}
          >
            {part}
          </span>
        );
      })}
    </>
  );
}

/* ---- Minecraft § color rendering -------------------------------------- */

const MC_COLORS: Record<string, string> = {
  "0": "#000000",
  "1": "#0000AA",
  "2": "#00AA00",
  "3": "#00AAAA",
  "4": "#AA0000",
  "5": "#AA00AA",
  "6": "#FFAA00",
  "7": "#AAAAAA",
  "8": "#555555",
  "9": "#5555FF",
  a: "#55FF55",
  b: "#55FFFF",
  c: "#FF5555",
  d: "#FF55FF",
  e: "#FFFF55",
  f: "#FFFFFF",
};

/** Renders text split on §-codes with approximate Minecraft colors/styles. */
export function McText({ text }: { text: string }): ReactNode {
  const parts = text.split(/(§.)/);
  const spans: ReactNode[] = [];
  let color: string | null = null;
  let bold = false;
  let italic = false;
  let underline = false;
  let strike = false;
  parts.forEach((part, i) => {
    if (part.length === 2 && part.startsWith("§")) {
      const code = part[1].toLowerCase();
      if (MC_COLORS[code] !== undefined) {
        color = MC_COLORS[code];
        bold = italic = underline = strike = false;
      } else if (code === "l") bold = true;
      else if (code === "o") italic = true;
      else if (code === "n") underline = true;
      else if (code === "m") strike = true;
      else if (code === "r") {
        color = null;
        bold = italic = underline = strike = false;
      }
      return;
    }
    if (part === "") return;
    const deco = [underline ? "underline" : null, strike ? "line-through" : null]
      .filter((d) => d !== null)
      .join(" ");
    spans.push(
      <span
        key={i}
        style={{
          color: color ?? undefined,
          fontWeight: bold ? 700 : undefined,
          fontStyle: italic ? "italic" : undefined,
          textDecoration: deco === "" ? undefined : deco,
        }}
      >
        {part}
      </span>,
    );
  });
  return <>{spans}</>;
}
