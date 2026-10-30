import { expect, test } from "bun:test";

import ko from "../i18n/ko/w5.json";
import en from "../i18n/en/w5.json";
import {
  addSlice,
  emptyTally,
  entryId,
  fileLabel,
  nextRowId,
  reviewKeyAction,
  reviewedPercent,
  reviewSlices,
  sortFiles,
} from "./w5ReviewModel.ts";

test("J/K mirror the arrows and Space signs off", () => {
  expect(reviewKeyAction("j")).toBe("next");
  expect(reviewKeyAction("ArrowDown")).toBe("next");
  expect(reviewKeyAction("K")).toBe("prev");
  expect(reviewKeyAction("ArrowUp")).toBe("prev");
  expect(reviewKeyAction(" ")).toBe("toggleReviewed");
  expect(reviewKeyAction("R")).toBe("retranslate");
  expect(reviewKeyAction("e")).toBe("edit");
  expect(reviewKeyAction("x")).toBeNull();
});

test("a lang file is labelled by its mod namespace", () => {
  expect(fileLabel("kubejs/assets/create/lang/en_us.json")).toEqual({
    mod: "create",
    name: "lang/en_us.json",
  });
  expect(fileLabel("somemod/lang/en_us.json").mod).toBe("somemod");
  expect(fileLabel("config\\ftbquests\\quests\\chapters\\a.snbt")).toEqual({
    mod: "config",
    name: "chapters/a.snbt",
  });
});

test("the side list groups files by mod", () => {
  const files = [
    { file: "z/assets/b/lang/en_us.json" },
    { file: "a/assets/c/lang/en_us.json" },
    { file: "y/assets/b/lang/a.json" },
  ];
  expect(sortFiles(files).map((f) => f.file)).toEqual([
    "y/assets/b/lang/a.json",
    "z/assets/b/lang/en_us.json",
    "a/assets/c/lang/en_us.json",
  ]);
});

const rows = [
  { file: "a.json", key: "k1" },
  { file: "a.json", key: "k2" },
  { file: "b.json", key: "k1" },
];

test("entry ids carry the file, since a key repeats across files", () => {
  expect(entryId(rows[0])).not.toBe(entryId(rows[2]));
});

test("signing a row off moves the cursor to the next row", () => {
  expect(nextRowId(rows, "a.json:k1")).toBe("a.json:k2");
});

test("the last row falls back to the one before it, not to the top", () => {
  expect(nextRowId(rows, "b.json:k1")).toBe("a.json:k2");
});

test("a lone row has nowhere to go", () => {
  expect(nextRowId([rows[0]], "a.json:k1")).toBeNull();
});

test("an unknown id falls back to the first row", () => {
  expect(nextRowId(rows, "gone:x")).toBe("a.json:k1");
  expect(nextRowId([], "gone:x")).toBeNull();
});

test("review progress is a clamped percentage, absent without a total", () => {
  expect(reviewedPercent(25, 100)).toBe(25);
  expect(reviewedPercent(5, 0)).toBeNull();
  expect(reviewedPercent(null, 10)).toBeNull();
  expect(reviewedPercent(12, 10)).toBe(100);
});

function keyPaths(obj, prefix = "") {
  return Object.entries(obj).flatMap(([k, v]) =>
    v !== null && typeof v === "object" ? keyPaths(v, `${prefix}${k}.`) : [`${prefix}${k}`],
  );
}

test("the review screen's ko and en strings have identical keys", () => {
  expect(keyPaths(ko).sort()).toEqual(keyPaths(en).sort());
});

test("a re-review walks the targets in request-sized slices", () => {
  const refs = Array.from({ length: 95 }, (_, i) => i);
  const slices = reviewSlices(refs, 40);
  expect(slices.map((s) => s.length)).toEqual([40, 40, 15]);
  expect(slices.flat()).toEqual(refs);
  expect(reviewSlices([], 40)).toEqual([]);
});

test("an interrupted slice counts only what it reached as done", () => {
  let tally = emptyTally(80);
  tally = addSlice(tally, { improved: [1, 2], unchanged: 37, skipped: 1, pending: 0 }, 40);
  tally = addSlice(tally, { improved: [3], unchanged: 4, skipped: 0, pending: 35 }, 40);
  expect(tally).toEqual({ done: 45, total: 80, improved: 3, unchanged: 41, skipped: 1 });
});
