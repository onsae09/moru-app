import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import { ConfirmDialog } from "@/components/ConfirmDialog";
import { moru } from "@/lib/bridge";
import { formatInt } from "@/lib/format";
import { useRouter } from "@/stores/router";
import {
  pauseTranslationQueue,
  startTranslationQueue,
  useTranslationQueue,
  type TranslationQueueItem,
  type TranslationQueueItemStatus,
} from "@/stores/translationQueue";
import { useWizard } from "@/stores/wizard";

function statusClass(status: TranslationQueueItemStatus): string {
  switch (status) {
    case "done":
      return "border-accent-lo bg-tint text-accent";
    case "failed":
      return "border-red/40 bg-red/8 text-red";
    case "cancelled":
      return "border-amber/40 bg-amber/8 text-amber";
    case "scanning":
    case "translating":
      return "border-blue/40 bg-blue/8 text-blue";
    default:
      return "border-line2 bg-bar text-text3";
  }
}

function QueueRow({
  item,
  index,
  pendingIds,
}: {
  item: TranslationQueueItem;
  index: number;
  pendingIds: string[];
}) {
  const { t } = useTranslation();
  const go = useRouter((state) => state.go);
  const remove = useTranslationQueue((state) => state.remove);
  const move = useTranslationQueue((state) => state.move);
  const retry = useTranslationQueue((state) => state.retry);
  const scanProgress = useWizard((state) => state.scanProgress);
  const doneEntries = useWizard((state) => state.doneEntries);
  const totalEntries = useWizard((state) => state.scanTotals?.entries ?? 0);
  const active = item.status === "scanning" || item.status === "translating";
  const pendingIndex = pendingIds.indexOf(item.id);

  const progress =
    item.status === "scanning"
      ? scanProgress.total > 0
        ? Math.min(100, Math.round((scanProgress.current / scanProgress.total) * 100))
        : null
      : item.status === "translating" && totalEntries > 0
        ? Math.min(100, Math.round((doneEntries / totalEntries) * 100))
        : null;

  return (
    <div
      className={`border px-4 py-3.5 ${active ? "border-accent-lo bg-tint" : "border-line2 bg-raised"}`}
    >
      <div className="flex items-center gap-3">
        <div className="flex h-7 w-7 shrink-0 items-center justify-center border border-line2 bg-bar font-mono text-[11px] text-text3">
          {String(index + 1).padStart(2, "0")}
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="truncate text-[13px] font-bold text-text">{item.name}</span>
            <span
              className={`shrink-0 border px-2 py-0.5 font-mono text-[10px] font-semibold ${statusClass(item.status)}`}
            >
              {t(`queue.status.${item.status}`)}
            </span>
          </div>
          <div className="mt-1 truncate font-mono text-[10px] text-text3">{item.path}</div>
          {item.error !== null && (
            <div className="mt-2 text-[11px] leading-4 text-red">{item.error}</div>
          )}
        </div>

        {item.status === "pending" && (
          <div className="flex shrink-0 items-center gap-1">
            <button
              type="button"
              aria-label={t("queue.action.moveUp")}
              disabled={pendingIndex <= 0}
              onClick={() => move(item.id, -1)}
              className="h-7 w-7 border border-line2 text-xs text-text2 enabled:hover:border-edge2 enabled:hover:text-text disabled:opacity-30"
            >
              ↑
            </button>
            <button
              type="button"
              aria-label={t("queue.action.moveDown")}
              disabled={pendingIndex < 0 || pendingIndex >= pendingIds.length - 1}
              onClick={() => move(item.id, 1)}
              className="h-7 w-7 border border-line2 text-xs text-text2 enabled:hover:border-edge2 enabled:hover:text-text disabled:opacity-30"
            >
              ↓
            </button>
            <button
              type="button"
              onClick={() => remove(item.id)}
              className="border border-line2 px-2.5 py-[6px] text-[11px] font-semibold text-text3 hover:border-red/50 hover:text-red"
            >
              {t("queue.action.remove")}
            </button>
          </div>
        )}

        {(item.status === "failed" || item.status === "cancelled") && (
          <button
            type="button"
            onClick={() => retry(item.id)}
            className="shrink-0 border border-edge px-3 py-2 text-[11px] font-semibold text-text2 hover:border-edge2 hover:text-text"
          >
            {t("queue.action.retry")}
          </button>
        )}

        {item.status === "done" && (
          <button
            type="button"
            onClick={() => go("history")}
            className="shrink-0 border border-edge px-3 py-2 text-[11px] font-semibold text-text2 hover:border-edge2 hover:text-text"
          >
            {t("queue.action.history")}
          </button>
        )}

        {!active && item.status !== "pending" && (
          <button
            type="button"
            onClick={() => remove(item.id)}
            className="shrink-0 border border-line2 px-2.5 py-[6px] text-[11px] font-semibold text-text3 hover:border-red/50 hover:text-red"
          >
            {t("queue.action.remove")}
          </button>
        )}
      </div>

      {active && (
        <div className="mt-3 border-t border-accent-lo pt-3">
          <div className="mb-1.5 flex justify-between font-mono text-[10px] text-text3">
            <span>
              {item.status === "scanning"
                ? scanProgress.message || t("queue.progress.scanning")
                : t("queue.progress.translating", {
                    done: formatInt(doneEntries),
                    total: formatInt(totalEntries),
                  })}
            </span>
            <span>{progress === null ? "…" : `${progress}%`}</span>
          </div>
          <div className="h-1.5 overflow-hidden bg-bar">
            <div
              className={`h-full bg-accent transition-[width] ${progress === null ? "w-1/3 animate-pxspin" : ""}`}
              style={progress === null ? undefined : { width: `${progress}%` }}
            />
          </div>
        </div>
      )}
    </div>
  );
}

export function TranslationQueueScreen() {
  const { t } = useTranslation();
  const items = useTranslationQueue((state) => state.items);
  const phase = useTranslationQueue((state) => state.phase);
  const settings = useTranslationQueue((state) => state.settingsSnapshot);
  const lastError = useTranslationQueue((state) => state.lastError);
  const ready = useTranslationQueue((state) => state.ready);
  const add = useTranslationQueue((state) => state.add);
  const clear = useTranslationQueue((state) => state.clear);
  const [adding, setAdding] = useState(false);
  const [confirmClear, setConfirmClear] = useState(false);
  const [selectionErrors, setSelectionErrors] = useState<string[]>([]);

  const counts = useMemo(() => {
    let pending = 0;
    let done = 0;
    let failed = 0;
    for (const item of items) {
      if (item.status === "pending") pending += 1;
      else if (item.status === "done") done += 1;
      else if (item.status === "failed" || item.status === "cancelled") failed += 1;
    }
    return { pending, done, failed };
  }, [items]);
  const pendingIds = items.filter((item) => item.status === "pending").map((item) => item.id);
  const executing = phase === "running" || phase === "pausing";
  const inFlight = items.some(
    (item) => item.status === "scanning" || item.status === "translating",
  );
  const active = executing || inFlight;

  const pickPacks = async (): Promise<void> => {
    setAdding(true);
    setSelectionErrors([]);
    try {
      const paths = await moru.pickFolders();
      const inputs: { path: string; name: string }[] = [];
      const errors: string[] = [];
      for (const path of paths) {
        try {
          const probe = await moru.probeModpack(path);
          if (!probe.exists) errors.push(t("queue.validation.notFound", { path }));
          else if (!probe.isDirectory) errors.push(t("queue.validation.notDirectory", { path }));
          else if (!probe.hasMods) errors.push(t("queue.validation.noMods", { path }));
          else inputs.push({ path, name: probe.name });
        } catch (error) {
          errors.push(t("queue.validation.failed", { path, error: String(error) }));
        }
      }
      if (inputs.length > 0) {
        const before = useTranslationQueue.getState().items.length;
        add(inputs);
        const skipped =
          inputs.length - (useTranslationQueue.getState().items.length - before);
        // A fully duplicate selection must never vanish without a word.
        if (skipped > 0) errors.push(t("queue.validation.duplicate", { skipped }));
      }
      setSelectionErrors(errors);
    } finally {
      setAdding(false);
    }
  };

  return (
    <div className="animate-fade-in-up max-w-[1180px] px-10 py-8">
      <div className="mb-6 flex items-end justify-between gap-4">
        <div>
          <div className="mb-1.5 font-mono text-xs font-semibold tracking-[0.08em] text-accent uppercase">
            ▍ {t("queue.eyebrow")}
          </div>
          <h1 className="text-[26px] font-bold tracking-[-0.02em] text-text">{t("queue.title")}</h1>
          <p className="mt-2 max-w-2xl text-[13px] leading-5 text-text2">{t("queue.subtitle")}</p>
        </div>
        <button
          type="button"
          disabled={adding}
          onClick={() => void pickPacks()}
          className="bg-accent px-4 py-2.5 text-[13px] font-bold text-sel-ink hover:bg-accent-hi disabled:opacity-50"
        >
          {adding ? t("queue.action.checking") : t("queue.action.addPacks")}
        </button>
      </div>

      <div className="mb-5 grid grid-cols-4 gap-3">
        {[
          ["queue.summary.total", items.length, "text-text"],
          ["queue.summary.pending", counts.pending, "text-blue"],
          ["queue.summary.done", counts.done, "text-accent"],
          ["queue.summary.failed", counts.failed, "text-red"],
        ].map(([label, value, color]) => (
          <div key={String(label)} className="border border-line2 bg-raised px-4 py-3">
            <div className="text-[10px] font-semibold tracking-[0.06em] text-text3 uppercase">
              {t(String(label))}
            </div>
            <div className={`mt-1 font-mono text-xl font-bold ${String(color)}`}>{String(value)}</div>
          </div>
        ))}
      </div>

      <div className="mb-5 border border-line2 bg-card px-4 py-3 text-[11px] leading-5 text-text3">
        <span className="font-semibold text-text2">{t("queue.policy.title")}</span>{" "}
        {t("queue.policy.body")}
        {settings !== null && (
          <span className="ml-2 font-mono text-accent">
            {t("queue.policy.lockedModel", { model: settings.model })}
          </span>
        )}
      </div>

      {(selectionErrors.length > 0 || lastError !== null) && (
        <div className="mb-5 border border-red/40 bg-red/6 px-4 py-3 text-[11px] text-red">
          {lastError !== null && <div>{lastError}</div>}
          {selectionErrors.map((error) => (
            <div key={error}>{error}</div>
          ))}
        </div>
      )}

      <div className="mb-4 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span
            className={`h-2 w-2 ${active ? "animate-pxpulse bg-accent" : phase === "complete" ? "bg-accent" : "bg-text4"}`}
          />
          <span className="font-mono text-[11px] font-semibold text-text2">
            {ready ? t(`queue.phase.${phase}`) : t("queue.phase.recovering")}
          </span>
        </div>
        <div className="flex gap-2">
          <button
            type="button"
            onClick={() => (active ? setConfirmClear(true) : clear())}
            className="border border-line2 px-3 py-2 text-[11px] font-semibold text-text3 hover:border-edge2 hover:text-text"
          >
            {t("queue.action.clear")}
          </button>
          {executing ? (
            <button
              type="button"
              disabled={phase === "pausing"}
              onClick={pauseTranslationQueue}
              className="border border-amber/50 px-4 py-2 text-[12px] font-bold text-amber enabled:hover:bg-amber/8 disabled:opacity-50"
            >
              {phase === "pausing" ? t("queue.action.pausing") : t("queue.action.pause")}
            </button>
          ) : (
            <button
              type="button"
              disabled={!ready || counts.pending === 0}
              onClick={startTranslationQueue}
              className="bg-accent px-4 py-2 text-[12px] font-bold text-sel-ink hover:bg-accent-hi disabled:cursor-not-allowed disabled:opacity-40"
            >
              {phase === "paused" ? t("queue.action.resume") : t("queue.action.start")}
            </button>
          )}
        </div>
      </div>

      {items.length === 0 ? (
        <button
          type="button"
          onClick={() => void pickPacks()}
          className="flex w-full flex-col items-center border border-dashed border-line2 bg-raised px-6 py-14 text-center hover:border-edge2"
        >
          <span className="mb-3 text-3xl text-text4">＋</span>
          <span className="text-[13px] font-bold text-text">{t("queue.empty.title")}</span>
          <span className="mt-1.5 text-xs text-text3">{t("queue.empty.desc")}</span>
        </button>
      ) : (
        <div className="flex flex-col gap-2">
          {items.map((item, index) => (
            <QueueRow key={item.id} item={item} index={index} pendingIds={pendingIds} />
          ))}
        </div>
      )}

      <ConfirmDialog
        open={confirmClear}
        variant="danger"
        title={t("queue.confirmClear.title")}
        body={t("queue.confirmClear.body")}
        confirmLabel={t("queue.action.clear")}
        onCancel={() => setConfirmClear(false)}
        onConfirm={() => {
          clear();
          setConfirmClear(false);
        }}
      />
    </div>
  );
}
