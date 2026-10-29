/**
 * Pure helpers behind the review screen (W5), kept out of the component so
 * the review flow can be tested without a DOM.
 */

/** One entry's identity. A key is only unique within its own source file. */
export interface EntryRef {
  key: string;
  file: string;
}

/** The row id the review list uses: `file:key`. */
export function entryId(ref: EntryRef): string {
  return `${ref.file}:${ref.key}`;
}

/**
 * Where the cursor goes once the reviewer has dealt with `currentId`.
 *
 * The row after it, else the one before (it was the last row), else none.
 * Used when a review mark moves the reviewer on, and when the mark takes the
 * row out of the filtered list — on "unreviewed", signing a row off removes
 * it, and falling back to the first row would throw the reviewer back to the
 * top of the page.
 */
export function nextRowId(rows: readonly EntryRef[], currentId: string): string | null {
  const idx = rows.findIndex((r) => entryId(r) === currentId);
  if (idx === -1) return rows.length > 0 ? entryId(rows[0]) : null;
  const next = rows[idx + 1] ?? rows[idx - 1];
  return next === undefined ? null : entryId(next);
}

/** Share of entries signed off, as a 0..100 percentage; null when empty. */
export function reviewedPercent(reviewed: number | null, total: number | null): number | null {
  if (reviewed === null || total === null || total <= 0) return null;
  return Math.min(100, (reviewed / total) * 100);
}
