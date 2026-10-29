import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";

/**
 * Guards the light palette in global.css against contrast regressions:
 * the rules stated above its `:root[data-theme="light"]` block, as numbers.
 */

const css = readFileSync(new URL("./global.css", import.meta.url), "utf8");

function block(selector) {
  const start = css.indexOf(`${selector} {`);
  if (start < 0) throw new Error(`missing ${selector} block`);
  const end = css.indexOf("}", start);
  const tokens = {};
  for (const m of css.slice(start, end).matchAll(/--color-([\w-]+):\s*(#[0-9a-fA-F]{6})\s*;/g)) {
    tokens[m[1]] = m[2];
  }
  return tokens;
}

function luminance(hex) {
  const [r, g, b] = [1, 3, 5].map((i) => {
    const c = parseInt(hex.slice(i, i + 2), 16) / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function contrast(a, b) {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

const light = block(':root[data-theme="light"]');
const dark = block("@theme");

const SURFACES = ["ink", "bar", "well", "panel", "bg", "card", "hover", "raised", "raised-hover", "tint"];
const TEXT = ["text", "text2", "text3", "accent", "accent-hi", "accent-bright", "red", "red-hi", "amber", "blue", "purple", "sky", "pending"];
const FILLS = ["accent", "accent-hi", "accent-mid", "accent-lo", "red", "red-hi", "amber", "blue", "purple", "sky", "discord", "pending"];
const BORDERS = ["edge", "edge2"];

function failures(fg, bg, min) {
  const out = [];
  for (const f of fg) {
    for (const b of bg) {
      const ratio = contrast(light[f], light[b]);
      if (ratio < min) out.push(`${f} on ${b}: ${ratio.toFixed(2)}`);
    }
  }
  return out;
}

test("the light block redefines every color token the dark theme has", () => {
  expect(Object.keys(light).sort()).toEqual(Object.keys(dark).sort());
});

test("light text tokens reach 4.5:1 on every light surface", () => {
  expect(failures(TEXT, SURFACES, 4.5)).toEqual([]);
});

test("on-accent ink reaches 4.5:1 on every light fill it sits on", () => {
  expect(failures(["sel-ink"], FILLS, 4.5)).toEqual([]);
});

test("light input and strong borders reach 3:1 on every light surface", () => {
  expect(failures(BORDERS, SURFACES, 3)).toEqual([]);
});

test("the light block also flips the native color-scheme", () => {
  const start = css.indexOf(':root[data-theme="light"] {');
  expect(css.slice(start, css.indexOf("}", start))).toContain("color-scheme: light");
});
