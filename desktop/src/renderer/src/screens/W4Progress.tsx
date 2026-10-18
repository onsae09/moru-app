/**
 * W4 - live translation progress.
 * Everything renders from the wizard store's live WS-fed state: big ring,
 * ticker, counters, per-file progress, and the collapsible live log.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { TranslationGraphView } from "@/components/TranslationGraphView";
import {
  formatCompact,
  formatDuration,
  formatInt,
  formatUsd,
  ratePerSecond,
  remainingSeconds,
} from "@/lib/format";
import { estimateUsage } from "@/lib/models";
import {
  cacheRatioPercent,
  costUsd,
  estimatePriceForModel,
  priceForModel,
  usePricingTable,
} from "@/lib/pricing";
import { useRouter } from "@/stores/router";
import { useSettings } from "@/stores/settings";
import { selectedScanTotals, useWizard } from "@/stores/wizard";
import type { FileProgress, GlossaryProgress, LogLine } from "@/stores/wizard";

import { ActiveBatchPanel } from "./ActiveBatchPanel";

const RING_CIRCUMFERENCE = 263.9; // 2 * PI * r(42) - the progress ring's dasharray base

const LOG_LEVEL: Record<LogLine["level"], { label: string; className: string }> = {
  info: { label: "INFO", className: "text-blue" },
  warn: { label: "WARN", className: "text-amber" },
  error: { label: "ERR", className: "text-red" },
};

/** Bar colors by position, dim -> bright. */
const RATE_BAR_COLORS = [
  "bg-edge",
  "bg-edge",
  "bg-edge",
  "bg-accent-mid",
  "bg-accent-mid",
  "bg-accent",
  "bg-accent",
  "bg-accent",
];

/** 161 -> "02:41", 5023 -> "1:23:43" (mono clock style used by the ring card). */
function clock(totalSeconds: number): string {
  const s = Math.max(0, Math.round(totalSeconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const mm = String(m).padStart(2, "0");
  const ss = String(s % 60).padStart(2, "0");
  return h > 0 ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

/** Log line timestamp: "15:42:18". */
function timeOf(ts: number): string {
  const d = new Date(ts);
  return [d.getHours(), d.getMinutes(), d.getSeconds()]
    .map((n) => String(n).padStart(2, "0"))
    .join(":");
}

/** formatCompact with the K/M suffix rendered dim+small. */
function CompactValue({ value }: { value: number }) {
  const text = formatCompact(value);
  const match = /^(.*?)([KM])$/.exec(text);
  if (match === null) return <>{text}</>;
  return (
    <>
      {match[1]}
      <span className="text-[14px] text-text3">{match[2]}</span>
    </>
  );
}

function StatCard({
  label,
  dotClass,
  value,
  sub,
}: {
  label: string;
  dotClass?: string;
  value: React.ReactNode;
  sub: React.ReactNode;
}) {
  return (
    <div className="border border-line2 bg-raised px-[14px] py-3">
      <div className="mb-[6px] flex items-center gap-1 font-mono text-[10px] font-semibold tracking-[0.06em] text-text3 uppercase">
        {dotClass !== undefined && <div className={`h-[5px] w-[5px] ${dotClass}`} />}
        {label}
      </div>
      {value}
      {sub}
    </div>
  );
}

function FileRow({
  progress,
  seconds,
  lang,
}: {
  progress: FileProgress;
  seconds: number | null;
  lang: "ko" | "en";
}) {
  const { t } = useTranslation();
  const isDone = progress.total > 0 && progress.done >= progress.total;
  const percent =
    progress.total > 0 ? Math.min(100, Math.floor((progress.done / progress.total) * 100)) : 0;

  return (
    <div
      className={`grid grid-cols-[24px_1fr_100px_80px_90px] items-center gap-[10px] border-b border-line px-[14px] py-2 ${
        isDone ? "" : "bg-[#17211D]"
      }`}
    >
      {isDone ? (
        <div className="flex h-3.5 w-3.5 items-center justify-center bg-accent">
          <svg width="10" height="10" viewBox="0 0 10 10" fill="none" stroke="#0A100D" strokeWidth="2">
            <path d="M1 5 L4 8 L9 2" />
          </svg>
        </div>
      ) : (
        <div className="h-3.5 w-3.5 animate-pxspin rounded-full border-[1.5px] border-amber border-t-transparent [animation-duration:0.8s]" />
      )}
      <div className="min-w-0">
        <div
          className={`overflow-hidden font-mono text-[12px] text-ellipsis whitespace-nowrap ${
            isDone ? "text-text2" : "text-text"
          }`}
        >
          {progress.file}
        </div>
        {!isDone && (
          <div className="mt-1 flex h-[3px] gap-px bg-bar">
            <div className="bg-amber" style={{ flex: percent }} />
            <div style={{ flex: 100 - percent }} />
          </div>
        )}
      </div>
      <div className="font-mono text-[11px] text-text2">
        {t("w4.files.entries", { count: formatInt(progress.total) })}
      </div>
      {isDone ? (
        <div className="font-mono text-[11px] font-bold text-accent">{t("w4.files.done")}</div>
      ) : (
        <div className="font-mono text-[11px] font-bold text-amber">
          {t("w4.files.running", { percent })}
        </div>
      )}
      <div className="text-right font-mono text-[10px] text-text3">
        {seconds !== null ? formatDuration(seconds, lang) : "—"}
      </div>
    </div>
  );
}

/** Glossary extraction stage row: chunk progress + schema-error retry state. */
function GlossaryRow({ progress }: { progress: GlossaryProgress }) {
  const { t } = useTranslation();
  const isDone = progress.total > 0 && progress.done >= progress.total;
  const percent =
    progress.total > 0 ? Math.min(100, Math.floor((progress.done / progress.total) * 100)) : 0;
  const retrying = progress.retrying !== null;

  return (
    <div
      className={`grid grid-cols-[24px_1fr_100px_80px_90px] items-center gap-[10px] border-b border-line px-[14px] py-2 ${
        isDone ? "" : "bg-[#17211D]"
      }`}
    >
      {isDone ? (
        <div className="flex h-3.5 w-3.5 items-center justify-center bg-purple">
          <svg width="10" height="10" viewBox="0 0 10 10" fill="none" stroke="#0A100D" strokeWidth="2">
            <path d="M1 5 L4 8 L9 2" />
          </svg>
        </div>
      ) : (
        <div
          className={`h-3.5 w-3.5 animate-pxspin rounded-full border-[1.5px] border-t-transparent [animation-duration:0.8s] ${
            retrying ? "border-amber" : "border-purple"
          }`}
        />
      )}
      <div className="min-w-0">
        <div className={`font-mono text-[12px] ${isDone ? "text-text2" : "text-text"}`}>
          {t("w4.glossary.label")}
          {retrying && (
            <span className="ml-2 text-[10px] font-semibold text-amber">
              {t("w4.glossary.retrying", { attempt: progress.retrying })}
            </span>
          )}
        </div>
        {retrying && progress.lastError !== null && (
          <div
            className="mt-0.5 overflow-hidden font-mono text-[10px] text-ellipsis whitespace-nowrap text-text3"
            title={progress.lastError}
          >
            {progress.lastError}
          </div>
        )}
        {!isDone && (
          <div className="mt-1 flex h-[3px] gap-px bg-bar">
            <div className={retrying ? "bg-amber" : "bg-purple"} style={{ flex: percent }} />
            <div style={{ flex: 100 - percent }} />
          </div>
        )}
      </div>
      <div className="font-mono text-[11px] text-text2">
        {t("w4.glossary.chunks", { done: progress.done, total: progress.total })}
      </div>
      {isDone ? (
        <div className="font-mono text-[11px] font-bold text-purple">{t("w4.files.done")}</div>
      ) : (
        <div className={`font-mono text-[11px] font-bold ${retrying ? "text-amber" : "text-purple"}`}>
          {t("w4.files.running", { percent })}
        </div>
      )}
      <div className="text-right font-mono text-[10px] text-purple">
        {t("w4.glossary.terms", { count: formatInt(progress.newTerms) })}
      </div>
    </div>
  );
}


export function W4Progress() {
  const { t, i18n } = useTranslation();
  const lang: "ko" | "en" = i18n.language === "en" ? "en" : "ko";
  const go = useRouter((s) => s.go);
  const settingsModel = useSettings((s) => s.model);
  const model = useWizard((s) => s.model) ?? settingsModel;
  const batchSize = useSettings((s) => s.batchSize);
  const maxConcurrent = useSettings((s) => s.maxConcurrent);
  const maxRefine = useSettings((s) => s.maxRefine);
  const thinkingEnabled = useSettings((s) => s.thinkingEnabled);
  const useVanillaGlossary = useSettings((s) => s.useVanillaGlossary);
  const extractGlossary = useSettings((s) => s.extractGlossary);
  const glossaryMaxTerms = useSettings((s) => s.glossaryMaxTerms);
  const pricingTable = usePricingTable();
  const wizard = useWizard();
  const {
    runState,
    runError,
    resumable,
    interruptReason,
    sessionId,
    translateJobId,
    startedAt,
    translationStartedAt,
    finishedAt,
    doneEntries,
    fileProgress,
    glossaryProgress,
    activeBatches,
    failedKeys,
    failedEntryCount,
    promptTokens,
    completionTokens,
    cachedTokens,
    ticker,
    log,
    stats,
    scanResult,
    scanTotals,
    excludedCategories,
    sourceLocale,
    targetLocale,
    startTranslate,
    cancelTranslate,
    resumeTranslate,
  } = wizard;

  const running = runState === "running";

  /* 1s tick while running: drives elapsed/ETA and the per-second rate chart. */
  const [now, setNow] = useState(() => Date.now());
  const rateSamplesRef = useRef<number[]>([]);
  const fileTimesRef = useRef<Record<string, { start: number; end?: number }>>({});

  useEffect(() => {
    fileTimesRef.current = {};
  }, [startedAt]);

  useEffect(() => {
    rateSamplesRef.current = [];
  }, [translationStartedAt]);

  useEffect(() => {
    if (!running) return;
    const id = setInterval(() => {
      const samples = rateSamplesRef.current;
      const current = useWizard.getState();
      const migrated = selectedScanTotals(current).migrationEntries;
      samples.push(Math.max(0, current.doneEntries - migrated));
      if (samples.length > 9) samples.shift();
      setNow(Date.now());
    }, 1000);
    return () => clearInterval(id);
  }, [running]);

  /* Per-file wall-clock timing, derived client-side from first/last sighting. */
  useEffect(() => {
    const times = fileTimesRef.current;
    const ts = Date.now();
    for (const fp of Object.values(fileProgress)) {
      const rec = times[fp.file] ?? (times[fp.file] = { start: ts });
      if (fp.total > 0 && fp.done >= fp.total && rec.end === undefined) rec.end = ts;
    }
  }, [fileProgress]);

  /* ---- derived progress numbers ---- */
  const totals = useMemo(
    () => selectedScanTotals({ scanResult, scanTotals, excludedCategories }),
    [scanResult, scanTotals, excludedCategories],
  );
  const totalEntries = totals.entries > 0 ? totals.entries : (stats?.total_entries ?? 0);
  const doneShown =
    doneEntries > 0
      ? doneEntries
      : stats !== null
        ? stats.translated_entries + stats.tm_hits + (stats.migration_hits ?? 0)
        : 0;
  const pct =
    totalEntries > 0 ? Math.min(1, doneShown / totalEntries) : runState === "done" ? 1 : null;

  const elapsedLiveSec = startedAt !== null ? Math.max(0, ((finishedAt ?? now) - startedAt) / 1000) : 0;
  const elapsedSec = !running && stats !== null ? stats.duration_seconds : elapsedLiveSec;
  const translatedDone = Math.max(0, doneShown - totals.migrationEntries);
  const rate = ratePerSecond(translatedDone, translationStartedAt, finishedAt ?? now);
  const remainingSec = running
    ? remainingSeconds(totals.translationEntries, translatedDone, rate)
    : null;

  /* rate chart: last 8 per-second deltas (re-derived each 1s tick render) */
  const rateSamples = rateSamplesRef.current;
  const rateBars: number[] = [];
  for (let i = 1; i < rateSamples.length; i++) {
    rateBars.push(Math.max(0, rateSamples[i] - rateSamples[i - 1]));
  }
  while (rateBars.length < 8) rateBars.unshift(0);
  rateBars.splice(0, rateBars.length - 8);
  const rateBarMax = Math.max(...rateBars, 1);

  /* tokens + cost */
  const usedTokens = promptTokens + completionTokens;
  const price = priceForModel(pricingTable, model);
  const liveUsage = { promptTokens, completionTokens, cachedTokens };
  const liveCost = price !== null ? costUsd(liveUsage, price) : null;
  const cachePercent = cacheRatioPercent(liveUsage);
  const estimate = useMemo(
    () =>
      totals.translationChars > 0 || (extractGlossary && totals.entries > 0)
        ? estimateUsage({
            chars: totals.translationChars,
            entries: totals.translationEntries,
            glossaryEntries: totals.entries,
            batchSize,
            maxRefine,
            glossary: useVanillaGlossary,
            extractGlossary,
            glossaryMaxTerms,
            thinking: thinkingEnabled,
          })
        : null,
    [
      totals.translationChars,
      totals.translationEntries,
      totals.entries,
      batchSize,
      maxRefine,
      useVanillaGlossary,
      extractGlossary,
      glossaryMaxTerms,
      thinkingEnabled,
    ],
  );
  // Displayed estimate never reads below what the run has already burned:
  // clamp element-wise to the live counters (estimates are conservative,
  // but a pathological run must not show "used 18M / est 17M").
  const shownEstimate =
    estimate !== null
      ? {
          promptTokens: Math.max(estimate.promptTokens, promptTokens),
          completionTokens: Math.max(estimate.completionTokens, completionTokens),
          totalTokens: Math.max(estimate.totalTokens, usedTokens),
        }
      : null;
  const estPrice = estimatePriceForModel(pricingTable, model);
  const estCost =
    shownEstimate !== null && estPrice !== null
      ? Math.max(costUsd(shownEstimate, estPrice), liveCost ?? 0)
      : null;

  /* files */
  const fileList = Object.values(fileProgress);
  const activeFiles = fileList.filter((f) => !(f.total > 0 && f.done >= f.total));
  const doneFiles = fileList.filter((f) => f.total > 0 && f.done >= f.total);
  const totalFiles = totals.files > 0 ? totals.files : (stats?.total_files ?? fileList.length);
  // Scanned files with nothing to translate never report progress (FTB
  // Quests 26.1.2+ chapter data files, for one): once the run is done they
  // are not pending, they had no work.
  const pendingFiles = runState === "done" ? 0 : Math.max(0, totalFiles - fileList.length);
  const failedCount = Math.max(
    failedEntryCount,
    Object.keys(failedKeys).length,
    stats?.failed_entries ?? 0,
  );

  const latestTick = ticker[0];
  const activeBatchList = Object.values(activeBatches).sort(
    (a, b) => a.requestId - b.requestId,
  );
  const glossaryActive =
    glossaryProgress !== null &&
    glossaryProgress.total > 0 &&
    glossaryProgress.done < glossaryProgress.total;

  /* ---- cancel dialog ---- */
  const [showCancel, setShowCancel] = useState(false);

  /* ---- live log panel ---- */
  const [logOpen, setLogOpen] = useState(true);
  const [copied, setCopied] = useState(false);
  const logRef = useRef<HTMLDivElement | null>(null);
  const logPinnedRef = useRef(true);

  useEffect(() => {
    if (runState === "failed") setLogOpen(true);
  }, [runState]);

  useEffect(() => {
    const el = logRef.current;
    if (el !== null && logPinnedRef.current) el.scrollTop = el.scrollHeight;
  }, [log.length, logOpen]);

  const copyLog = () => {
    const text = log
      .map((line) => `${timeOf(line.ts)} ${LOG_LEVEL[line.level].label} ${line.text}`)
      .join("\n");
    void navigator.clipboard.writeText(text);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  /* ---- state-dependent chrome ---- */
  const stepColor =
    runState === "failed"
      ? "text-red"
      : runState === "cancelled" || runState === "interrupted"
        ? "text-amber"
        : "text-accent";
  const tickerLabelKey =
    runState === "done"
      ? "done"
      : runState === "failed"
        ? "failed"
        : runState === "cancelled" || runState === "interrupted"
          ? runState
          : running
            ? "live"
            : "idle";
  const tickerDotClass = running
    ? "bg-accent animate-pxpulse [animation-duration:1.2s]"
    : runState === "done"
      ? "bg-accent"
      : runState === "failed"
        ? "bg-red"
        : runState === "cancelled" || runState === "interrupted"
          ? "bg-amber"
          : "bg-edge";
  const tickerLabelClass =
    runState === "failed"
      ? "text-red"
      : runState === "cancelled" || runState === "interrupted"
        ? "text-amber"
        : running || runState === "done"
          ? "text-accent"
          : "text-text3";

  return (
    <div className="animate-fade-in-up px-10 py-7">
      {/* Step header + controls */}
      <div className="mb-2 flex items-center gap-[10px] font-mono text-[11px] font-semibold tracking-[0.08em] text-text3 uppercase">
        <span className={`${stepColor} ${running ? "animate-pxpulse" : ""}`}>04</span>
        <span>{t(`w4.state.${runState}`)}</span>
        <div
          className="h-px flex-1"
          style={{
            backgroundImage: "linear-gradient(90deg, #24322B 50%, transparent 50%)",
            backgroundSize: "6px 1px",
          }}
        />
        {running && (
          <div className="flex gap-1.5">
            <button
              disabled
              title={t("w4.controls.pauseSoon")}
              className="flex items-center gap-1.5 border border-edge bg-transparent px-3 py-1.5 text-[11px] font-semibold text-text2 disabled:cursor-not-allowed disabled:opacity-40"
            >
              <svg width="10" height="10" viewBox="0 0 10 10" shapeRendering="crispEdges">
                <rect x="1" y="1" width="3" height="8" fill="currentColor" />
                <rect x="6" y="1" width="3" height="8" fill="currentColor" />
              </svg>
              {t("w4.controls.pause")}
            </button>
            <button
              onClick={() => setShowCancel(true)}
              className="flex items-center gap-1.5 border border-edge bg-transparent px-3 py-1.5 text-[11px] font-semibold text-text2 hover:border-red hover:text-red"
            >
              <svg width="10" height="10" viewBox="0 0 10 10" shapeRendering="crispEdges">
                <rect x="1" y="1" width="8" height="8" fill="currentColor" />
              </svg>
              {t("w4.controls.cancel")}
            </button>
          </div>
        )}
      </div>

      {/* Terminal-state banner */}
      {runState === "done" && (
        <div className="mb-4 flex items-center gap-3 border border-accent-lo bg-tint px-4 py-3">
          <div className="flex h-4 w-4 shrink-0 items-center justify-center bg-accent">
            <svg width="10" height="10" viewBox="0 0 10 10" fill="none" stroke="#0A100D" strokeWidth="2">
              <path d="M1 5 L4 8 L9 2" />
            </svg>
          </div>
          <div className="min-w-0 flex-1">
            <div className="text-[13px] font-bold text-text">{t("w4.banner.doneTitle")}</div>
            <div className="mt-0.5 text-[11.5px] text-text2">
              {t("w4.banner.doneBody", { count: formatInt(doneShown) })}
            </div>
          </div>
          <button
            onClick={() => go("w5")}
            className="shrink-0 bg-accent px-4 py-2 text-[12px] font-bold text-sel-ink hover:bg-accent-hi"
          >
            {t("w4.banner.toReview")} →
          </button>
        </div>
      )}
      {runState === "failed" && (
        <div className="mb-4 flex items-center gap-3 border border-[rgba(242,107,107,0.4)] bg-[rgba(242,107,107,0.06)] px-4 py-3">
          <div className="flex h-4 w-4 shrink-0 items-center justify-center bg-[rgba(242,107,107,0.16)]">
            <svg width="8" height="8" viewBox="0 0 8 8" fill="none" stroke="#F26B6B" strokeWidth="2">
              <path d="M2 2 L6 6 M6 2 L2 6" />
            </svg>
          </div>
          <div className="min-w-0 flex-1">
            <div className="text-[13px] font-bold text-red">{t("w4.banner.failedTitle")}</div>
            <div className="mt-0.5 overflow-hidden font-mono text-[11.5px] text-ellipsis whitespace-nowrap text-text2">
              {runError ?? t("w4.banner.failedFallback")}
            </div>
          </div>
          {resumable && sessionId !== null && (
            <button
              onClick={() => void resumeTranslate(sessionId)}
              className="shrink-0 bg-red px-4 py-2 text-[12px] font-bold text-sel-ink hover:bg-[#f58585]"
            >
              {t("w4.banner.resume")}
            </button>
          )}
          <button
            onClick={() => {
              void startTranslate();
            }}
            className="shrink-0 border border-red px-4 py-2 text-[12px] font-bold text-red hover:bg-red hover:text-sel-ink"
          >
            {t("w4.banner.retry")}
          </button>
        </div>
      )}
      {runState === "cancelled" && (
        <div className="mb-4 flex items-center gap-3 border border-[rgba(245,180,84,0.4)] bg-[rgba(245,180,84,0.06)] px-4 py-3">
          <div className="flex h-4 w-4 shrink-0 items-center justify-center bg-[rgba(245,180,84,0.16)]">
            <svg width="8" height="8" viewBox="0 0 8 8" shapeRendering="crispEdges">
              <rect x="1" y="1" width="6" height="6" fill="#F5B454" />
            </svg>
          </div>
          <div className="min-w-0 flex-1">
            <div className="text-[13px] font-bold text-amber">{t("w4.banner.cancelledTitle")}</div>
            <div className="mt-0.5 text-[11.5px] text-text2">{t("w4.banner.cancelledBody")}</div>
          </div>
          {resumable && sessionId !== null && (
            <button
              onClick={() => void resumeTranslate(sessionId)}
              className="shrink-0 bg-amber px-4 py-2 text-[12px] font-bold text-sel-ink hover:opacity-90"
            >
              {t("w4.banner.resume")}
            </button>
          )}
          <button
            onClick={() => go("w5")}
            className="shrink-0 border border-amber px-4 py-2 text-[12px] font-bold text-amber hover:bg-amber hover:text-sel-ink"
          >
            {t("w4.banner.toReview")} →
          </button>
        </div>
      )}
      {runState === "interrupted" && (
        <div className="mb-4 flex items-center gap-3 border border-[rgba(245,180,84,0.4)] bg-[rgba(245,180,84,0.06)] px-4 py-3">
          <div className="flex h-4 w-4 shrink-0 items-center justify-center bg-[rgba(245,180,84,0.16)]">
            <svg width="8" height="8" viewBox="0 0 8 8" shapeRendering="crispEdges">
              <rect x="1" y="1" width="2" height="6" fill="#F5B454" />
              <rect x="5" y="1" width="2" height="6" fill="#F5B454" />
            </svg>
          </div>
          <div className="min-w-0 flex-1">
            <div className="text-[13px] font-bold text-amber">
              {t(interruptReason === "quota" ? "w4.banner.quotaTitle" : "w4.banner.interruptedTitle")}
            </div>
            <div className="mt-0.5 text-[11.5px] text-text2">
              {t(interruptReason === "quota" ? "w4.banner.quotaBody" : "w4.banner.interruptedBody")}
            </div>
          </div>
          {resumable && sessionId !== null && (
            <button
              onClick={() => void resumeTranslate(sessionId)}
              className="shrink-0 bg-amber px-4 py-2 text-[12px] font-bold text-sel-ink hover:opacity-90"
            >
              {t("w4.banner.resume")}
            </button>
          )}
          {translateJobId !== null && stats !== null && (
            <button
              onClick={() => go("w5")}
              className="shrink-0 border border-amber px-4 py-2 text-[12px] font-bold text-amber hover:bg-amber hover:text-sel-ink"
            >
              {t("w4.banner.toReview")} →
            </button>
          )}
        </div>
      )}

      {/* Hero: big progress + ticker */}
      <div className="mb-5 grid grid-cols-[380px_1fr] gap-5">
        {/* Progress ring */}
        <div
          className="relative overflow-hidden border border-accent-lo p-6 text-center"
          style={{ background: "linear-gradient(135deg, #14201A 0%, #141C18 100%)" }}
        >
          <div
            className="absolute inset-0"
            style={{
              backgroundImage:
                "radial-gradient(circle at 2px 2px, rgba(61,220,132,0.06) 1px, transparent 1px)",
              backgroundSize: "12px 12px",
            }}
          />
          <div className="relative mx-auto mb-4 h-[200px] w-[200px]">
            <div className={`h-full w-full ${pct === null && running ? "animate-pxspin" : ""}`}>
              <svg
                viewBox="0 0 100 100"
                className="h-full w-full -rotate-90"
                shapeRendering="crispEdges"
              >
                <circle cx="50" cy="50" r="42" fill="none" stroke="#0F1613" strokeWidth="10" />
                <circle
                  cx="50"
                  cy="50"
                  r="42"
                  fill="none"
                  stroke="#3DDC84"
                  strokeWidth="10"
                  strokeDasharray={
                    pct === null
                      ? `66 ${RING_CIRCUMFERENCE}`
                      : `${(pct * RING_CIRCUMFERENCE).toFixed(1)} ${RING_CIRCUMFERENCE}`
                  }
                  strokeLinecap="butt"
                />
              </svg>
            </div>
            <div className="absolute inset-0 flex flex-col items-center justify-center">
              {pct === null ? (
                <>
                  <div className="animate-pxpulse font-mono text-[46px] leading-none font-bold tracking-[-0.03em] text-text2">
                    —<span className="text-[24px] text-accent">%</span>
                  </div>
                  <div className="mt-1 font-mono text-[11px] text-text3">
                    {t("w4.ring.counting")}
                  </div>
                </>
              ) : (
                <>
                  <div className="font-mono text-[46px] leading-none font-bold tracking-[-0.03em] text-text">
                    {Math.floor(pct * 100)}
                    <span className="text-[24px] text-accent">%</span>
                  </div>
                  <div className="mt-1 font-mono text-[11px] text-text3">
                    {formatInt(doneShown)} / {formatInt(totalEntries)}
                  </div>
                </>
              )}
            </div>
          </div>
          <div className="relative flex justify-center gap-6 border-t border-[rgba(61,220,132,0.12)] pt-3">
            <div>
              <div className="font-mono text-[10px] font-semibold tracking-[0.08em] text-text3 uppercase">
                {t("w4.ring.elapsed")}
              </div>
              <div className="mt-0.5 font-mono text-[16px] font-bold text-text">
                {startedAt !== null ? clock(elapsedSec) : "--:--"}
              </div>
            </div>
            <div className="w-px bg-line2" />
            <div>
              <div className="font-mono text-[10px] font-semibold tracking-[0.08em] text-text3 uppercase">
                {t("w4.ring.remaining")}
              </div>
              <div className="mt-0.5 font-mono text-[16px] font-bold text-accent">
                {running
                  ? remainingSec !== null
                    ? clock(remainingSec)
                    : "--:--"
                  : runState === "done"
                    ? "00:00"
                    : "--:--"}
              </div>
            </div>
          </div>
        </div>

        {/* Live stats + ticker */}
        <div className="flex flex-col gap-3">
          {running && (
            <ActiveBatchPanel
              batches={activeBatchList}
              limit={maxConcurrent}
              now={now}
              glossaryActive={glossaryActive}
            />
          )}
          {/* Ticker */}
          <div className="relative overflow-hidden border border-line2 bg-raised px-5 py-4">
            <div className="mb-2 flex items-center gap-1.5">
              <div className={`h-1.5 w-1.5 ${tickerDotClass}`} />
              <span
                className={`font-mono text-[10px] font-bold tracking-[0.06em] uppercase ${tickerLabelClass}`}
              >
                {t(`w4.ticker.${tickerLabelKey}`)}
              </span>
              {latestTick !== undefined && (
                <span className="min-w-0 truncate font-mono text-[10px] text-text3">
                  · {latestTick.key}
                </span>
              )}
            </div>
            <div className="relative h-[72px]">
              {latestTick === undefined ? (
                <div className="flex h-full items-center gap-2">
                  <div className="h-1.5 w-1.5 animate-pxpulse bg-edge2" />
                  <span className="text-[13px] text-text3">
                    {running ? t("w4.ticker.waiting") : t("w4.ticker.empty")}
                  </span>
                </div>
              ) : (
                <div
                  key={latestTick.key}
                  className={`absolute inset-0 ${running ? "animate-ticker" : ""}`}
                >
                  <div className="mb-1 font-mono text-[12px] text-text3">
                    {(sourceLocale.split(/[_-]/)[0] ?? sourceLocale).toUpperCase()}
                  </div>
                  <div className="mb-1.5 overflow-hidden text-[15px] font-medium text-ellipsis whitespace-nowrap text-text2">
                    "{latestTick.source}"
                  </div>
                  <div className="mb-1 font-mono text-[12px] text-accent">
                    {(targetLocale.split(/[_-]/)[0] ?? targetLocale).toUpperCase()} ➜
                  </div>
                  <div className="overflow-hidden text-[15px] font-semibold text-ellipsis whitespace-nowrap text-text">
                    "{latestTick.translated}"
                  </div>
                </div>
              )}
            </div>
          </div>

          {/* Stats grid */}
          <div className="grid flex-1 grid-cols-4 gap-2">
            <StatCard
              label={t("w4.stats.rate")}
              value={
                <div className="font-mono text-[20px] font-bold tracking-[-0.02em] text-text">
                  {rate.toFixed(1)}
                </div>
              }
              sub={
                <div className="mt-[6px] flex h-4 items-end gap-px">
                  {rateBars.map((delta, idx) => (
                    <div
                      key={idx}
                      className={`flex-1 ${RATE_BAR_COLORS[idx]}`}
                      style={{ height: `${Math.max(6, Math.round((delta / rateBarMax) * 100))}%` }}
                    />
                  ))}
                </div>
              }
            />
            <StatCard
              label={t("w4.stats.tokens")}
              value={
                <div className="font-mono text-[20px] font-bold tracking-[-0.02em] text-text">
                  <CompactValue value={usedTokens} />
                </div>
              }
              sub={
                <div className="mt-[6px] font-mono text-[10px] text-text3">
                  {shownEstimate !== null
                    ? t("w4.stats.estimate", { value: formatCompact(shownEstimate.totalTokens) })
                    : "—"}
                </div>
              }
            />
            <StatCard
              label={t("w4.stats.reuse")}
              dotClass="bg-purple"
              value={
                <div className="font-mono text-[20px] font-bold tracking-[-0.02em] text-purple">
                  {stats !== null
                    ? formatInt(stats.tm_hits + (stats.migration_hits ?? 0))
                    : "—"}
                </div>
              }
              sub={
                <div className="mt-[6px] font-mono text-[10px] text-text3">
                  {stats !== null
                    ? t("w4.stats.reuseBreakdown", {
                        migrated: formatInt(stats.migration_hits ?? 0),
                        tm: formatInt(stats.tm_hits),
                      })
                    : t("w4.stats.afterDone")}
                </div>
              }
            />
            <StatCard
              label={t("w4.stats.cost")}
              value={
                <div className="font-mono text-[20px] font-bold tracking-[-0.02em] text-accent">
                  {liveCost !== null ? formatUsd(liveCost) : "—"}
                </div>
              }
              sub={
                <div className="mt-[6px] flex items-center gap-1.5 font-mono text-[10px] text-text3">
                  <span>
                    {liveCost === null
                      ? t("w4.stats.noPrice")
                      : estCost !== null
                        ? t("w4.stats.estimate", { value: formatUsd(estCost) })
                        : "—"}
                  </span>
                  {cachePercent !== null && cachePercent > 0 && (
                    <span className="bg-[rgba(61,220,132,0.08)] px-1 text-accent">
                      {t("w4.stats.cacheHit", { value: cachePercent })}
                    </span>
                  )}
                </div>
              }
            />
          </div>
        </div>
      </div>

      {/* Files + Log */}
      <div className="grid grid-cols-[1fr_380px] gap-5">
        {/* File progress list */}
        <div className="border border-line2 bg-raised">
          <div className="flex items-center justify-between border-b border-line2 px-[14px] py-2.5 font-mono text-[11px] font-semibold tracking-[0.06em] text-text2 uppercase">
            <span>{t("w4.files.title", { count: formatInt(totalFiles) })}</span>
            <div className="flex gap-3">
              <span className="text-accent">
                {t("w4.files.chipDone", { count: formatInt(doneFiles.length) })}
              </span>
              <span className="text-amber">
                {t("w4.files.chipRunning", { count: formatInt(activeFiles.length) })}
              </span>
              {stats !== null && (
                <span className="text-purple">
                  {t("w4.files.chipTm", { count: formatInt(stats.tm_hits) })}
                </span>
              )}
              {stats !== null && (stats.migration_hits ?? 0) > 0 && (
                <span className="text-purple">
                  {t("w4.files.chipMigrated", {
                    count: formatInt(stats.migration_hits ?? 0),
                  })}
                </span>
              )}
              <span className="text-red">
                {t("w4.files.chipFailed", { count: formatInt(failedCount) })}
              </span>
              <span className="text-text3">
                {t("w4.files.chipPending", { count: formatInt(pendingFiles) })}
              </span>
            </div>
          </div>
          <div className="max-h-[320px] overflow-y-auto">
            {glossaryProgress !== null && <GlossaryRow progress={glossaryProgress} />}
            {[...activeFiles, ...doneFiles].map((fp) => {
              const rec = fileTimesRef.current[fp.file];
              const isDone = fp.total > 0 && fp.done >= fp.total;
              const seconds =
                rec === undefined
                  ? null
                  : isDone
                    ? rec.end !== undefined
                      ? (rec.end - rec.start) / 1000
                      : null
                    : (now - rec.start) / 1000;
              return <FileRow key={fp.file} progress={fp} seconds={seconds} lang={lang} />;
            })}
            {fileList.length === 0 && pendingFiles === 0 && (
              <div className="px-[14px] py-2.5 text-center font-mono text-[11px] text-text3">
                {t("w4.files.waiting")}
              </div>
            )}
            {pendingFiles > 0 && (
              <div className="px-[14px] py-2.5 text-center font-mono text-[11px] text-text3">
                {t("w4.files.pendingMore", { count: formatInt(pendingFiles) })}
              </div>
            )}
          </div>
        </div>

        {/* Log panel (collapsible) */}
        <div
          className={`flex flex-col border bg-[#0A0D0B] ${
            runState === "failed" ? "border-[rgba(242,107,107,0.4)]" : "border-line2"
          }`}
        >
          <div
            onClick={() => setLogOpen((open) => !open)}
            className="flex cursor-pointer items-center justify-between px-[14px] py-2.5 font-mono text-[11px] font-semibold tracking-[0.06em] text-text2 uppercase"
          >
            <div className="flex items-center gap-1.5">
              <svg width="10" height="10" viewBox="0 0 10 10" fill="none" stroke="#3DDC84" strokeWidth="1.5">
                <rect x="1" y="1" width="8" height="8" />
                <path d="M3 3 L5 5 L3 7" />
              </svg>
              <span>{t("w4.log.title")}</span>
              <span className="text-text4">{logOpen ? "▾" : "▸"}</span>
            </div>
            <button
              onClick={(e) => {
                e.stopPropagation();
                copyLog();
              }}
              className={`text-[10px] ${copied ? "text-accent" : "text-text3 hover:text-text"}`}
            >
              {copied ? t("w4.log.copied") : t("w4.log.copy")}
            </button>
          </div>
          {logOpen && (
            <div
              ref={logRef}
              onScroll={(e) => {
                const el = e.currentTarget;
                logPinnedRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
              }}
              className="max-h-[320px] overflow-y-auto border-t border-line2 px-[14px] py-3 font-mono text-[11px] leading-[1.65] text-text2"
            >
              {log.length === 0 && <div className="text-text4">{t("w4.log.empty")}</div>}
              {log.map((line, idx) => (
                <div
                  key={`${line.ts}-${idx}`}
                  className={
                    line.level === "error" ? "-mx-[14px] bg-[rgba(242,107,107,0.06)] px-[14px]" : ""
                  }
                >
                  <span className="text-text4">{timeOf(line.ts)}</span>{" "}
                  <span className={LOG_LEVEL[line.level].className}>
                    {LOG_LEVEL[line.level].label}
                  </span>{" "}
                  {line.text}
                </div>
              ))}
              {running && (
                <div className="mt-1.5 flex items-center gap-1.5 text-text3">
                  <div className="h-3 w-1.5 animate-pxblink bg-accent [animation-timing-function:steps(2)]" />
                  <span>{t("w4.log.streaming")}</span>
                </div>
              )}
            </div>
          )}
        </div>
      </div>

      {/* Translation graph (collapsible, live-polling while running) */}
      {wizard.translateJobId !== null && (
        <div className="mt-5">
          <TranslationGraphView jobId={wizard.translateJobId} live={running} />
        </div>
      )}

      {/* Cancel confirmation dialog */}
      {showCancel && (
        <div
          onClick={() => setShowCancel(false)}
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/60"
        >
          <div
            onClick={(e) => e.stopPropagation()}
            className="w-[400px] border border-edge bg-panel p-6"
          >
            <div className="mb-2 font-mono text-[10px] font-semibold tracking-[0.08em] text-text3 uppercase">
              04 · {t("w4.controls.cancel")}
            </div>
            <div className="text-[15px] font-bold text-text">{t("w4.cancelDialog.title")}</div>
            <div className="mt-2 text-[12.5px] leading-relaxed text-text2">
              {t("w4.cancelDialog.body")}
            </div>
            <div className="mt-5 flex justify-end gap-2">
              <button
                onClick={() => setShowCancel(false)}
                className="border border-edge px-4 py-2 text-[12px] font-semibold text-text2 hover:border-edge2 hover:text-text"
              >
                {t("w4.cancelDialog.keep")}
              </button>
              <button
                onClick={() => {
                  void cancelTranslate();
                  setShowCancel(false);
                }}
                className="border border-red bg-[rgba(242,107,107,0.12)] px-4 py-2 text-[12px] font-bold text-red hover:bg-red hover:text-sel-ink"
              >
                {t("w4.cancelDialog.confirm")}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
