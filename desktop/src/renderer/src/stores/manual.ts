/**
 * Manual (hand) translation session state.
 *
 * Holds the working queue, the focused entry, and the local draft buffer for
 * W5Manual. Two things are deliberately persisted and two deliberately are
 * not:
 *
 * - `drafts` and `flags` ARE persisted. A draft is uncommitted work; losing it
 *   to a reload or a crash is the single worst thing this feature could do.
 *   The engine's edit journal (see the manual-mode design) is the durable and
 *   portable record, but the renderer still needs a local fast path, and
 *   keeping it in localStorage means the text survives even when the engine
 *   never saw it.
 * - `refs` / `entries` are NOT persisted. They are a server-owned snapshot;
 *   re-fetching is correct and cheap, and a 60k-entry pack would blow the
 *   localStorage quota.
 *
 * The queue is a SNAPSHOT, not a live query. Committing an entry moves it out
 * of the `pending` bucket, so a live query would renumber pages and reorder
 * rows underneath a translator mid-sentence. The snapshot keeps its order
 * until explicitly refreshed.
 */

import { create } from "zustand";
import { persist } from "zustand/middleware";

import type { Entry, EntryFilter } from "../../../shared/engine";
import { installTokenPatterns } from "../components/EntryText";
import { api } from "../lib/api";
import { useWizard } from "./wizard";

/** Entries fetched per request. The engine caps `page_size` at 500. */
const PAGE_SIZE = 500;
/** Prefetch the next page once the cursor is this close to the loaded end. */
const PREFETCH_MARGIN = 40;
/** Drafts retained per job, most-recently-touched first. */
const MAX_DRAFTS_PER_JOB = 2000;

export interface EntryRef {
  key: string;
  file: string;
}

/**
 * Mirrors the engine's `_NUMBERED_KEY_RE` (engine `batching.py`). The stem
 * keeps its separator, so `tooltip2` and `tooltip.2` never merge into one
 * run — matching the engine's grouping exactly matters, because the engine
 * uses it to decide what reaches the model as one unit.
 */
const NUMBERED_KEY_RE = /^(.*?)(?:\[(\d+)\]|(\d+))$/;

interface NumberedKey {
  stem: string;
  ordinal: number;
}

function parseNumberedKey(key: string): NumberedKey | null {
  const m = NUMBERED_KEY_RE.exec(key);
  if (m === null) return null;
  const digits = m[2] ?? m[3];
  if (digits === undefined || m[1] === "") return null;
  return { stem: m[1], ordinal: Number.parseInt(digits, 10) };
}

/**
 * Whether two keys belong to the same numbered run, i.e. two lines of one
 * split sentence. Exported so the focus pane shows exactly the grouping the
 * engine batches by — one definition of "sibling", not two.
 */
export function sameRun(a: string, b: string): boolean {
  const sa = parseNumberedKey(a)?.stem;
  const sb = parseNumberedKey(b)?.stem;
  return sa !== undefined && sa === sb;
}

/** `file:key` — a key is only unique within its own source file. */
export function refId(ref: EntryRef): string {
  return `${ref.file}:${ref.key}`;
}

/**
 * The entry's content type, for grouping a queue by what the strings ARE
 * rather than only by which file they came from.
 *
 * Handlers build dotted/bracketed path keys (`quest.3.desc2`,
 * `pages[1].text`), so the last non-numeric segment of the key names the kind
 * of string: `title`, `desc`, `subtitle`, `text`. There is no per-entry type
 * field on the wire to read instead.
 */
export function contentKind(key: string): string {
  const stem = parseNumberedKey(key)?.stem ?? key;
  const cleaned = stem.replace(/[.\-_[\]]+$/, "");
  const leaf = cleaned.split(/[.\[\]]/).filter((s) => s !== "").at(-1);
  return leaf === undefined || leaf === "" ? key : leaf;
}

/**
 * A human-facing origin for an entry's file: which mod or content pack it
 * came from, plus a short name for the file itself.
 *
 * `EntryResult` carries only the modpack-relative path — `mod_id` and
 * `namespace` live on the scan result, which a restored session does not
 * rebuild — so the path is what there is. A resource-pack layout names the
 * namespace directly (`.../assets/<ns>/...`); otherwise the segment under a
 * known container directory is the best available answer.
 */
const CONTAINER_DIRS: Record<string, true> = {
  config: true,
  mods: true,
  kubejs: true,
  scripts: true,
  resourcepacks: true,
  datapacks: true,
  defaultconfigs: true,
};

export function fileLabel(file: string): { mod: string; name: string } {
  const segments = file.split("/").filter((s) => s !== "");
  const assetsAt = segments.indexOf("assets");
  const base = segments.at(-1) ?? file;
  const name = base.replace(/\.[^.]+$/, "");

  if (assetsAt >= 0 && assetsAt + 1 < segments.length) {
    return { mod: segments[assetsAt + 1], name };
  }
  if (segments.length > 1 && CONTAINER_DIRS[segments[0]] === true) {
    return { mod: segments[1].replace(/\.(jar|zip)$/, ""), name };
  }
  return { mod: segments[0] ?? file, name };
}

/**
 * Order a page so numbered siblings stay adjacent and in ordinal order.
 *
 * Mods routinely split one sentence across `tooltip1..tooltipN`; a translator
 * shown `desc2` alone will mistranslate it. Entries arrive in scan order,
 * which is already file-grouped, so this only has to pull each run together
 * and sort within it — the run's position is its first member's position.
 */
export function orderQueue(entries: Entry[]): Entry[] {
  const runIndex: Record<string, number> = {};
  const runs: Entry[][] = [];
  const out: (Entry | Entry[])[] = [];

  for (const entry of entries) {
    const parsed = parseNumberedKey(entry.key);
    if (parsed === null) {
      out.push(entry);
      continue;
    }
    const runKey = `${entry.file}\u0000${parsed.stem}`;
    const existing = runIndex[runKey];
    if (existing === undefined) {
      const run = [entry];
      runIndex[runKey] = runs.length;
      runs.push(run);
      out.push(run);
      continue;
    }
    runs[existing].push(entry);
  }

  const flat: Entry[] = [];
  for (const item of out) {
    if (!Array.isArray(item)) {
      flat.push(item);
      continue;
    }
    // A run of one is not a run; it just keeps its place.
    if (item.length > 1) {
      item.sort(
        (a, b) =>
          (parseNumberedKey(a.key)?.ordinal ?? 0) - (parseNumberedKey(b.key)?.ordinal ?? 0),
      );
    }
    flat.push(...item);
  }
  return flat;
}

/** Grouping shown in the queue rail: one heading per file, in queue order. */
interface QueueGroup {
  file: string;
  kind: string;
  refs: EntryRef[];
}

export function groupQueue(refs: EntryRef[]): QueueGroup[] {
  const groups: QueueGroup[] = [];
  for (const ref of refs) {
    const kind = contentKind(ref.key);
    const last = groups.at(-1);
    if (last !== undefined && last.file === ref.file && last.kind === kind) {
      last.refs.push(ref);
      continue;
    }
    groups.push({ file: ref.file, kind, refs: [ref] });
  }
  return groups;
}

/**
 * Buckets the engine can filter server-side today. `pending` and
 * `stale_source` are declared on the wire but only produced once the engine
 * gains manual-seed and drift tracking, so they are not offered here yet —
 * a chip that returns nothing is worse than no chip. `flagged` is a local
 * view (see `flaggedOnly`) because bookmarks live in this store until the
 * engine records them.
 */
export type ManualFilter = Extract<
  EntryFilter,
  "all" | "failed" | "warning" | "modified"
>;
export const MANUAL_FILTERS: readonly ManualFilter[] = [
  "all",
  "failed",
  "warning",
  "modified",
];

interface ManualStore {
  /** Engine translate job the session is working on. */
  jobId: string | null;
  filter: ManualFilter;
  search: string;
  /** Narrow the loaded queue to bookmarked entries, client-side. */
  flaggedOnly: boolean;

  /** Ordered queue snapshot. Grows as pages load; never reorders on commit. */
  refs: EntryRef[];
  entries: Record<string, Entry>;
  /** Server-reported size of the current filter, which `refs` may trail. */
  total: number;
  pagesLoaded: number;
  cursor: number;
  loading: boolean;
  error: string | null;

  /** Uncommitted text, per job. Persisted. */
  drafts: Record<string, Record<string, string>>;
  /** Revisit bookmarks, per job. Persisted. */
  flags: Record<string, string[]>;
  /**
   * Last job id each session's drafts were filed under. Restoring a session
   * hands it a new job id; without this its drafts stayed under the old one,
   * invisible and never cleaned up. Persisted.
   */
  sessionJobs: Record<string, string>;

  open: (jobId: string) => Promise<void>;
  setFilter: (filter: ManualFilter) => Promise<void>;
  setSearch: (search: string) => Promise<void>;
  refresh: () => Promise<void>;
  setFlaggedOnly: (on: boolean) => void;
  loadMore: () => Promise<void>;

  select: (index: number) => void;
  move: (delta: number) => void;
  /** Advance to the next entry still lacking a committed translation. */
  advanceToNextPending: () => void;

  setDraft: (text: string) => void;
  clearDraft: (id: string) => void;
  commit: (opts?: { advance?: boolean }) => Promise<void>;
  toggleFlag: () => void;
  reset: () => void;
}

const EMPTY_STATE = {
  refs: [] as EntryRef[],
  entries: {} as Record<string, Entry>,
  total: 0,
  pagesLoaded: 0,
  cursor: 0,
  loading: false,
  error: null as string | null,
};

/** `translated_text` is `""` for a pending entry, so emptiness is the test. */
function isSettled(entry: Entry | undefined): boolean {
  return entry !== undefined && entry.translated_text !== "";
}

/**
 * The queue the translator actually walks. `flaggedOnly` narrows the loaded
 * snapshot client-side, and `cursor` indexes THIS list — so every navigation
 * action must go through here rather than touching `refs` directly, or the
 * cursor would point at a row the rail is not showing.
 */
export function visibleQueue(state: {
  refs: EntryRef[];
  flaggedOnly: boolean;
  flags: Record<string, string[]>;
  jobId: string | null;
}): EntryRef[] {
  if (!state.flaggedOnly || state.jobId === null) return state.refs;
  const flagged = state.flags[state.jobId] ?? [];
  return state.refs.filter((r) => flagged.includes(refId(r)));
}

export const useManual = create<ManualStore>()(
  persist(
    (set, get) => ({
      jobId: null,
      filter: "all",
      search: "",
      flaggedOnly: false,
      ...EMPTY_STATE,
      drafts: {},
      flags: {},
      sessionJobs: {},

      open: async (jobId) => {
        // Idempotent per job: React mounts effects twice under StrictMode, and
        // keying off `refs.length` let both passes through while the first
        // fetch was still in flight — appending the same page twice.
        // `refresh()` is the way to deliberately re-snapshot.
        //
        // "Already in flight or already succeeded" rather than a bare id
        // match, because a bare id match latched FAILURE too: the engine
        // answers 409 on /entries until the job has a result, and one such
        // miss left the queue permanently empty with no way back — no retry,
        // no message, just an empty screen.
        const current = get();
        if (current.jobId === jobId && (current.loading || current.error === null)) {
          return;
        }
        requestEpoch += 1;
        set({ jobId, ...EMPTY_STATE });
        adoptSessionDrafts(jobId);
        // Adopt the engine's placeholder grammar. The renderer's fallback is
        // close but not identical, and the engine is the authority because the
        // engine is what rejects the translation. Failure is survivable: the
        // fallback stays in place.
        void api
          .placeholderPatterns()
          .then((res) => installTokenPatterns(res.patterns))
          .catch(() => undefined);
        await get().loadMore();
      },

      setFilter: async (filter) => {
        requestEpoch += 1;
        set({ filter, ...EMPTY_STATE });
        await get().loadMore();
      },

      setFlaggedOnly: (on) => set({ flaggedOnly: on, cursor: 0 }),

      setSearch: async (search) => {
        requestEpoch += 1;
        set({ search, ...EMPTY_STATE });
        await get().loadMore();
      },

      refresh: async () => {
        const { cursor } = get();
        requestEpoch += 1;
        set({ ...EMPTY_STATE });
        await get().loadMore();
        // Keep the translator roughly where they were after a re-snapshot.
        set((s) => ({ cursor: Math.min(cursor, Math.max(0, s.refs.length - 1)) }));
      },

      loadMore: async () => {
        const { jobId, filter, search, pagesLoaded, loading, refs, total } = get();
        if (jobId === null || loading) return;
        if (pagesLoaded > 0 && refs.length >= total) return;
        const wanted = pagesLoaded + 1;
        const epoch = requestEpoch;
        set({ loading: true, error: null });
        try {
          const page = await api.entries(jobId, filter, wanted, PAGE_SIZE, search);
          const ordered = orderQueue(page.entries);
          set((s) => {
            // A filter/search change or refresh started over meanwhile: this
            // page belongs to the old list, and the loading flag to the new
            // request — leave both alone.
            if (epoch !== requestEpoch) return {};
            // The snapshot moved on while this request was in flight (a
            // concurrent load): drop this page rather than appending it out
            // of sequence.
            if (s.jobId !== jobId || s.pagesLoaded + 1 !== wanted) {
              return { loading: false };
            }
            const entries = { ...s.entries };
            for (const entry of ordered) entries[refId(entry)] = entry;
            return {
              refs: [...s.refs, ...ordered.map((e) => ({ key: e.key, file: e.file }))],
              entries,
              total: page.total,
              pagesLoaded: wanted,
              loading: false,
            };
          });
        } catch (err) {
          if (epoch !== requestEpoch) return;
          set({
            loading: false,
            error: err instanceof Error ? err.message : String(err),
          });
        }
      },

      select: (index) => {
        const state = get();
        const queue = visibleQueue(state);
        if (queue.length === 0) return;
        set({ cursor: Math.min(Math.max(0, index), queue.length - 1) });
        // Prefetch against the unfiltered snapshot: `flaggedOnly` shrinks the
        // visible list, but more pages are still what unblocks navigation.
        if (index >= state.refs.length - PREFETCH_MARGIN) void get().loadMore();
      },

      move: (delta) => get().select(get().cursor + delta),

      advanceToNextPending: () => {
        const state = get();
        const queue = visibleQueue(state);
        const jobDrafts = state.jobId === null ? {} : (state.drafts[state.jobId] ?? {});
        for (let i = state.cursor + 1; i < queue.length; i += 1) {
          const id = refId(queue[i]);
          if (!isSettled(state.entries[id]) || jobDrafts[id] !== undefined) {
            get().select(i);
            return;
          }
        }
        // Nothing left needing work ahead: step forward anyway so the loop
        // never silently refuses to advance.
        get().move(1);
      },

      setDraft: (text) => {
        const state = get();
        const queue = visibleQueue(state);
        const { jobId } = state;
        if (jobId === null || queue.length === 0) return;
        const id = refId(queue[state.cursor]);
        set((s) => {
          const jobDrafts = { ...(s.drafts[jobId] ?? {}) };
          jobDrafts[id] = text;
          const keys = Object.keys(jobDrafts);
          if (keys.length > MAX_DRAFTS_PER_JOB) {
            // Oldest-inserted first: JS object key order is insertion order
            // for string keys, and re-setting an existing key keeps its slot.
            for (const stale of keys.slice(0, keys.length - MAX_DRAFTS_PER_JOB)) {
              delete jobDrafts[stale];
            }
          }
          return { drafts: { ...s.drafts, [jobId]: jobDrafts } };
        });
      },

      clearDraft: (id) => {
        const { jobId } = get();
        if (jobId === null) return;
        set((s) => {
          const jobDrafts = { ...(s.drafts[jobId] ?? {}) };
          delete jobDrafts[id];
          return { drafts: { ...s.drafts, [jobId]: jobDrafts } };
        });
      },

      commit: async ({ advance = true } = {}) => {
        const state = get();
        const queue = visibleQueue(state);
        const { jobId, entries, drafts } = state;
        if (jobId === null || queue.length === 0) return;
        const ref = queue[state.cursor];
        const id = refId(ref);
        const entry = entries[id];
        const text = drafts[jobId]?.[id] ?? entry?.translated_text ?? "";
        if (entry === undefined || text === "") return;
        set({ error: null });
        try {
          const updated = await api.commitEntry(jobId, ref.key, text, {
            file: ref.file,
            commit: true,
            srcSha: await sourceSha(entry.source_text),
          });
          set((s) => ({ entries: { ...s.entries, [id]: updated } }));
          get().clearDraft(id);
          if (advance) get().advanceToNextPending();
        } catch (err) {
          set({ error: err instanceof Error ? err.message : String(err) });
        }
      },

      toggleFlag: () => {
        const state = get();
        const queue = visibleQueue(state);
        const { jobId } = state;
        if (jobId === null || queue.length === 0) return;
        const id = refId(queue[state.cursor]);
        set((s) => {
          const current = s.flags[jobId] ?? [];
          const next = current.includes(id)
            ? current.filter((x) => x !== id)
            : [...current, id];
          return { flags: { ...s.flags, [jobId]: next } };
        });
      },

      reset: () => set({ jobId: null, ...EMPTY_STATE }),
    }),
    {
      name: "moru-manual",
      version: 1,
      // Unlike the other persisted stores this one MUST partialize: `refs`
      // and `entries` are a server snapshot that would exceed the
      // localStorage quota on a large pack, and re-fetching them is correct.
      partialize: (state) => ({
        drafts: state.drafts,
        flags: state.flags,
        sessionJobs: state.sessionJobs,
      }),
    },
  ),
);

/** Bumped whenever the list starts over; an older page response is stale. */
let requestEpoch = 0;

/** Move the current session's drafts and flags onto its new job id. */
function adoptSessionDrafts(jobId: string): void {
  const sessionId = useWizard.getState().sessionId;
  if (sessionId === null) return;
  useManual.setState((s) => {
    const previous = s.sessionJobs[sessionId];
    const sessionJobs = { ...s.sessionJobs, [sessionId]: jobId };
    if (previous === undefined || previous === jobId) return { sessionJobs };
    const drafts = { ...s.drafts };
    const flags = { ...s.flags };
    if (drafts[previous] !== undefined) {
      drafts[jobId] = { ...drafts[previous], ...(drafts[jobId] ?? {}) };
      delete drafts[previous];
    }
    if (flags[previous] !== undefined) {
      flags[jobId] = [...new Set([...flags[previous], ...(flags[jobId] ?? [])])];
      delete flags[previous];
    }
    return { sessionJobs, drafts, flags };
  });
}

/**
 * `sha256(source_text)` truncated to 16 hex chars — the same shape the engine
 * records, so a later scan can detect that the source changed underneath a
 * hand translation instead of shipping it as if still valid.
 */
async function sourceSha(source: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(source));
  return Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("")
    .slice(0, 16);
}
