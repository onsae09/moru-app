/**
 * Manual translation queue rail: progress, buckets, search, and the ordered
 * work list grouped by file and content kind.
 *
 * The list is a snapshot, not a live query — see `stores/manual`. Rows do not
 * move when an entry is committed; only the row's own status changes. A
 * translator mid-sentence must never have the list reorder underneath them.
 */

import type { ReactNode } from "react";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import type { EntryCounts } from "../../../shared/engine";
import { STATUS_TOKEN, StatusIcon, tint } from "@/components/EntryText";
import { api } from "@/lib/api";
import { formatInt } from "@/lib/format";
import {
  MANUAL_FILTERS,
  type ManualFilter,
  fileLabel,
  groupQueue,
  refId,
  useManual,
  visibleQueue,
} from "@/stores/manual";

const FILTER_COLOR: Record<ManualFilter, string> = {
  all: "var(--color-accent)",
  failed: "var(--color-red)",
  warning: "var(--color-amber)",
  modified: "var(--color-blue)",
};

export function ManualQueue(): ReactNode {
  const { t } = useTranslation();
  const jobId = useManual((s) => s.jobId);
  const refs = useManual((s) => s.refs);
  const entries = useManual((s) => s.entries);
  const flags = useManual((s) => s.flags);
  const drafts = useManual((s) => s.drafts);
  const flaggedOnly = useManual((s) => s.flaggedOnly);
  const filter = useManual((s) => s.filter);
  const search = useManual((s) => s.search);
  const total = useManual((s) => s.total);
  const cursor = useManual((s) => s.cursor);
  const loading = useManual((s) => s.loading);
  const setFilter = useManual((s) => s.setFilter);
  const setSearch = useManual((s) => s.setSearch);
  const setFlaggedOnly = useManual((s) => s.setFlaggedOnly);
  const select = useManual((s) => s.select);
  const loadMore = useManual((s) => s.loadMore);

  const listRef = useRef<HTMLDivElement>(null);
  const queue = visibleQueue({ refs, flaggedOnly, flags, jobId });
  const jobFlags = jobId === null ? [] : (flags[jobId] ?? []);
  const jobDrafts = jobId === null ? {} : (drafts[jobId] ?? {});

  // Bucket sizes in one server-side pass. Refetched when the job, the search,
  // or the number of settled entries changes — committing moves an entry
  // between buckets, so a stale count would understate the work left.
  const [counts, setCounts] = useState<EntryCounts | null>(null);
  const settledCount = refs.reduce((n, ref) => {
    const entry = entries[refId(ref)];
    return entry !== undefined && entry.translated_text !== "" ? n + 1 : n;
  }, 0);
  useEffect(() => {
    if (jobId === null) return;
    let live = true;
    api
      .entryCounts(jobId, search)
      .then((res) => {
        if (live) setCounts(res);
      })
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, [jobId, search, settledCount]);

  const percent = refs.length === 0 ? 0 : (settledCount / refs.length) * 100;

  // Follow the cursor. Keyboard navigation is the primary way through the
  // queue, so the focused row has to stay on screen without a mouse.
  useEffect(() => {
    const active = queue[cursor];
    if (active === undefined) return;
    listRef.current
      ?.querySelector(`[data-ref="${CSS.escape(refId(active))}"]`)
      ?.scrollIntoView({ block: "nearest" });
  }, [cursor, queue]);

  const groups = groupQueue(queue);
  let flatIndex = 0;

  return (
    <div className="flex min-h-0 flex-col border-r border-line2">
      {/* Progress */}
      <div className="border-b border-line2 px-3 py-[10px]">
        <div className="mb-[6px] flex items-baseline justify-between font-mono text-[11px]">
          <span className="text-text2">
            {formatInt(settledCount)}
            <span className="text-text3"> / {formatInt(refs.length)}</span>
          </span>
          <span className="text-accent">{percent.toFixed(1)}%</span>
        </div>
        <div className="h-[3px] w-full bg-line2">
          <div className="h-full bg-accent" style={{ width: `${percent}%` }} />
        </div>
        {refs.length < total && (
          <div className="mt-[6px] font-mono text-[10px] text-text3">
            {t("w5m.queue.loaded", {
              loaded: formatInt(refs.length),
              total: formatInt(total),
            })}
          </div>
        )}
      </div>

      {/* Buckets */}
      <div className="flex flex-wrap gap-1 border-b border-line2 px-3 py-2">
        {MANUAL_FILTERS.map((f) => {
          const c = FILTER_COLOR[f];
          const active = filter === f && !flaggedOnly;
          return (
            <button
              key={f}
              onClick={() => {
                setFlaggedOnly(false);
                void setFilter(f);
              }}
              className={`flex items-center gap-1 border px-[8px] py-[4px] text-[10px] font-semibold ${
                active ? "" : "border-edge text-text2 hover:border-edge2 hover:text-text"
              }`}
              style={active ? { background: tint(c, 8), borderColor: c, color: c } : undefined}
            >
              <div className="h-[4px] w-[4px]" style={{ background: c }} />
              {t(`w5m.filter.${f}`)}
              {counts !== null ? ` ${formatInt(counts[f])}` : ""}
            </button>
          );
        })}
        <button
          onClick={() => setFlaggedOnly(!flaggedOnly)}
          className={`flex items-center gap-1 border px-[8px] py-[4px] text-[10px] font-semibold ${
            flaggedOnly
              ? "border-amber bg-amber/8 text-amber"
              : "border-edge text-text2 hover:border-edge2 hover:text-text"
          }`}
        >
          <svg width="8" height="8" viewBox="0 0 8 8" fill="currentColor">
            <path d="M1 0 H7 V8 L4 5.5 L1 8 Z" />
          </svg>
          {t("w5m.filter.flagged")}
          {jobFlags.length > 0 ? ` ${formatInt(jobFlags.length)}` : ""}
        </button>
      </div>

      {/* Search */}
      <div className="border-b border-line2 px-3 py-2">
        <input
          type="text"
          value={search}
          onChange={(e) => void setSearch(e.target.value)}
          placeholder={t("w5m.queue.searchPlaceholder")}
          className="w-full border border-edge bg-card px-[8px] py-[5px] font-mono text-[11px] text-text placeholder:text-text4"
        />
      </div>

      {/* Work list */}
      <div ref={listRef} className="min-h-0 flex-1 overflow-y-auto">
        {queue.length === 0 && !loading ? (
          <div className="px-3 py-10 text-center font-mono text-[11px] text-text3">
            {flaggedOnly ? t("w5m.queue.noFlagged") : t("w5m.queue.empty")}
          </div>
        ) : (
          groups.map((group, gi) => {
            const label = fileLabel(group.file);
            return (
              <div key={`${group.file}:${group.kind}:${gi}`}>
                <div className="sticky top-0 flex items-baseline gap-[6px] border-b border-line bg-hover px-3 py-[5px]">
                  <span className="font-mono text-[10px] font-bold text-accent">{label.mod}</span>
                  <span className="truncate font-mono text-[10px] text-text3">{label.name}</span>
                  <span className="ml-auto shrink-0 font-mono text-[10px] text-text3">
                    {group.kind}
                  </span>
                </div>
                {group.refs.map((ref) => {
                  const index = flatIndex++;
                  const id = refId(ref);
                  const entry = entries[id];
                  const isSel = index === cursor;
                  const hasDraft = jobDrafts[id] !== undefined;
                  const isFlagged = jobFlags.includes(id);
                  const color =
                    entry === undefined ? "var(--color-text4)" : STATUS_TOKEN[entry.status];
                  // Numbered siblings share a run; the bracket is what tells a
                  // translator this line continues into the next one.
                  const inRun = group.refs.length > 1;
                  return (
                    <button
                      key={id}
                      data-ref={id}
                      onClick={() => select(index)}
                      className={`flex w-full items-center gap-[6px] border-b border-line px-3 py-[6px] text-left ${
                        isSel ? "" : "hover:bg-raised-hover"
                      }`}
                      style={
                        isSel
                          ? {
                              background: `linear-gradient(90deg, ${tint(color, 8)} 0%, transparent 100%)`,
                              borderLeft: `2px solid ${color}`,
                            }
                          : { borderLeft: "2px solid transparent" }
                      }
                    >
                      {inRun && <div className="h-3 w-px shrink-0 bg-edge2" />}
                      {entry !== undefined && <StatusIcon status={entry.status} size={10} />}
                      <span
                        className={`min-w-0 flex-1 truncate font-mono text-[11px] ${
                          isSel ? "text-text" : "text-text2"
                        }`}
                      >
                        {ref.key}
                      </span>
                      {hasDraft && (
                        <span
                          title={t("w5m.queue.hasDraft")}
                          className="shrink-0 font-mono text-[10px] text-amber"
                        >
                          ●
                        </span>
                      )}
                      {isFlagged && (
                        <svg
                          width="8"
                          height="8"
                          viewBox="0 0 8 8"
                          fill="currentColor"
                          className="text-amber shrink-0"
                        >
                          <path d="M1 0 H7 V8 L4 5.5 L1 8 Z" />
                        </svg>
                      )}
                    </button>
                  );
                })}
              </div>
            );
          })
        )}
        {refs.length < total && (
          <button
            onClick={() => void loadMore()}
            disabled={loading}
            className="w-full px-3 py-[10px] text-center font-mono text-[11px] text-text3 hover:bg-raised-hover hover:text-text disabled:opacity-50"
          >
            {loading ? t("w5m.queue.loading") : t("w5m.queue.loadMore")}
          </button>
        )}
      </div>
    </div>
  );
}
