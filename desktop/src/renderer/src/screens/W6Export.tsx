/**
 * W6: export & community share.
 * One export job produces plain and bilingual resource-pack and overrides
 * zips (kubejs/config/ftbquests files a resource pack cannot carry).
 * Web upload runs an engine upload job against
 * moru.gg; signed-in uploads are attributed to the moru.gg account,
 * anonymous uploads to no account (allowed for the desktop app only).
 */

import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import type {
  JobEventFrame,
  ModPackUploadResult,
  ModSourcesResult,
  PipelineStats,
  UploadParams,
} from "../../../shared/engine";
import { WIZARD_PAGE_CLASS } from "@/components/wizardPage";
import { Button } from "@/components/Button";
import { ErrorDetails } from "@/components/ErrorDetails";
import { useWizardResetGuard } from "@/components/WizardResetGuard";
import { api, openJobEvents } from "@/lib/api";
import { moru } from "@/lib/bridge";
import { formatCompact, formatDuration, formatInt, formatUsd, packInitials } from "@/lib/format";
import { cacheRatioPercent, costUsd, priceForModel, usePricingTable } from "@/lib/pricing";
import { WEB_URL, web, type ModpackSearchResult } from "@/lib/web";
import { useAccount } from "@/stores/account";
import { useSessions } from "@/stores/sessions";
import { useRouter } from "@/stores/router";
import { useSettings } from "@/stores/settings";
import { useWizard } from "@/stores/wizard";

type UploadPhase =
  | { kind: "idle" }
  | { kind: "running"; stage: string; current: number; total: number; message: string }
  | {
      kind: "done";
      url: string | null;
      modpackSlug: string | null;
      modPacks: ModPackUploadResult | null;
    }
  | { kind: "failed"; message: string };

/**
 * Upload-frame handler. Exported for tests: the share link must land on
 * the session that STARTED the upload — by completion time the wizard may
 * have been reset or reopened onto another session.
 */
export function makeUploadFrameHandler(
  sourceSessionId: string | null,
  setUpload: (phase: UploadPhase) => void,
): (frame: JobEventFrame) => void {
  return (frame) => {
    if (frame.type === "progress") {
      setUpload({
        kind: "running",
        stage: frame.stage,
        current: frame.current ?? frame.done ?? 0,
        total: frame.total ?? 0,
        message: frame.message ?? "",
      });
    } else if (frame.type === "done") {
      const url = frame.url ?? null;
      setUpload({
        kind: "done",
        url,
        modpackSlug: frame.modpack_slug ?? null,
        modPacks: frame.mod_packs ?? null,
      });
      // Persist the share link so Home/History "share link" buttons stay
      // live after the wizard moves on.
      if (sourceSessionId !== null && url !== null) {
        useSessions.getState().patch(sourceSessionId, { sharedUrl: url });
      }
    } else if (frame.type === "failed" || frame.type === "cancelled") {
      setUpload({ kind: "failed", message: frame.error ?? "upload failed" });
    }
  };
}

type SearchPhase =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "done"; results: ModpackSearchResult[] }
  | { kind: "failed"; message: string };

/** CurseForge pack the upload will be registered under. */
interface ConfirmedPack {
  id: number;
  name: string;
  logoUrl: string | null;
}

/** "96.8" / "94" - one decimal, trailing .0 trimmed. */
function trim1(n: number): string {
  return n.toFixed(1).replace(/\.0$/, "");
}

export function W6Export() {
  const { t, i18n } = useTranslation();
  const go = useRouter((s) => s.go);
  const wizard = useWizard();
  const settingsModel = useSettings((s) => s.model);
  // Cost math must price the run's model, not whatever settings now hold.
  const model = wizard.model ?? settingsModel;
  const account = useAccount();
  const pricingTable = usePricingTable();
  const [upload, setUpload] = useState<UploadPhase>({ kind: "idle" });
  const [shareMods, setShareMods] = useState(true);
  const [modSources, setModSources] = useState<ModSourcesResult | null>(null);
  const [modSourcesError, setModSourcesError] = useState(false);
  const [copiedPath, setCopiedPath] = useState<string | null>(null);
  const closeUploadEvents = useRef<(() => void) | null>(null);
  const uploadAttempt = useRef(0);
  // Restored sessions can resolve their saved mod sources by translate job id.
  const sourceJobId = wizard.scanJobId ?? wizard.translateJobId;

  useEffect(() => {
    return () => {
      uploadAttempt.current += 1;
      closeUploadEvents.current?.();
      closeUploadEvents.current = null;
    };
  }, []);

  useEffect(() => {
    setModSources(null);
    setModSourcesError(false);
    if (sourceJobId === null) return;
    const jobId = sourceJobId;
    let active = true;
    let timer: number | undefined;
    async function poll(): Promise<void> {
      try {
        const result = await api.modSources(jobId);
        if (!active) return;
        setModSources(result);
        if (result.status === "done" || result.status === "failed") return;
        timer = window.setTimeout(() => void poll(), 3000);
      } catch {
        if (active) setModSourcesError(true);
      }
    }
    void poll();
    return () => {
      active = false;
      window.clearTimeout(timer);
    };
  }, [sourceJobId]);

  /* CurseForge identity gate: uploads must map to a real CF modpack, so a
     mis-detected pack can't be published under the wrong name. */
  const identity = wizard.scanResult?.identity ?? null;
  const autoPack: ConfirmedPack | null =
    identity !== null && identity.curseforge_project_id !== null
      ? {
          id: identity.curseforge_project_id,
          name: identity.name ?? wizard.modpackName,
          logoUrl: null,
        }
      : null;
  const [pickedPack, setPickedPack] = useState<ConfirmedPack | null>(null);
  const confirmedPack = autoPack ?? pickedPack;
  const [searchQuery, setSearchQuery] = useState(() => identity?.name ?? wizard.modpackName);
  const [search, setSearch] = useState<SearchPhase>({ kind: "idle" });
  const [version, setVersion] = useState(() => identity?.version ?? "");

  const lang: "ko" | "en" = i18n.language === "en" ? "en" : "ko";
  const stats = wizard.stats;

  const kicker = (
    <div className="mb-2 flex items-center gap-[10px] font-mono text-[11px] font-semibold uppercase tracking-[0.08em] text-text3">
      <span className="text-accent">06</span>
      <span>{t("w6.kicker")}</span>
      <div
        className="h-px flex-1"
        style={{
          backgroundImage: "linear-gradient(90deg, var(--color-edge) 50%, transparent 50%)",
          backgroundSize: "6px 1px",
        }}
      />
    </div>
  );

  /* ---- empty state: nothing to export before the run finished ---- */
  // An interrupted run the engine stopped gracefully keeps a partial result,
  // exportable exactly like a cancelled one.
  const partial =
    wizard.runState === "cancelled" ||
    (wizard.runState === "interrupted" && wizard.stats !== null);
  if (wizard.runState !== "done" && !partial) {
    const toStart = wizard.runState === "idle";
    return (
      <div className={`${WIZARD_PAGE_CLASS}`}>
        {kicker}
        <div className="relative mt-6 overflow-hidden border border-line2 bg-card px-10 py-16 text-center">
          <div
            className="pointer-events-none absolute inset-0 opacity-[0.05]"
            style={{
              backgroundImage: "radial-gradient(circle at 2px 2px, var(--color-accent) 1px, transparent 1px)",
              backgroundSize: "10px 10px",
            }}
          />
          <div className="relative mx-auto mb-4 flex h-14 w-14 items-center justify-center border border-edge bg-raised">
            <svg width="24" height="24" viewBox="0 0 20 20" shapeRendering="crispEdges" fill="#1F8A5B">
              <rect x="2" y="4" width="16" height="2" />
              <rect x="2" y="4" width="2" height="12" />
              <rect x="16" y="4" width="2" height="12" />
              <rect x="2" y="14" width="16" height="2" />
              <rect x="6" y="8" width="8" height="2" />
              <rect x="6" y="10" width="8" height="2" />
            </svg>
          </div>
          <div className="relative mb-1 text-[15px] font-bold text-text">{t("w6.empty.title")}</div>
          <div className="relative mb-5 text-xs text-text2">{t("w6.empty.desc")}</div>
          <button
            className="relative bg-line2 px-5 py-[10px] text-[13px] font-bold text-text hover:bg-edge"
            onClick={() => go(toStart ? "w1" : "w4")}
          >
            {toStart ? t("w6.empty.goStart") : t("w6.empty.goProgress")}
          </button>
        </div>
      </div>
    );
  }

  /* ---- derived run summary ---- */
  const cancelled = partial;
  const price = priceForModel(pricingTable, model);
  const finalUsage =
    stats !== null
      ? {
          promptTokens: stats.prompt_tokens,
          completionTokens: stats.completion_tokens,
          cachedTokens: stats.cached_tokens ?? 0,
        }
      : null;
  const finalCostUsd = finalUsage !== null && price !== null ? costUsd(finalUsage, price) : null;
  const cachePercent = finalUsage !== null ? cacheRatioPercent(finalUsage) : null;
  // what the bill would have been with zero cache hits, minus the real bill
  const cacheSavedUsd =
    finalUsage !== null && price !== null && finalUsage.cachedTokens > 0
      ? costUsd({ ...finalUsage, cachedTokens: 0 }, price) - costUsd(finalUsage, price)
      : null;
  const tmPercent =
    stats !== null && stats.total_entries > 0 ? (stats.tm_hits / stats.total_entries) * 100 : 0;

  const summary =
    stats !== null
      ? t("w6.banner.summary", {
          name: wizard.modpackName,
          total: formatInt(stats.total_entries),
          passed: formatInt(stats.translated_entries),
          skipped: formatInt(stats.skipped_entries),
          failed: formatInt(stats.failed_entries),
          duration: formatDuration(stats.duration_seconds, lang),
        })
      : t("w6.banner.summaryNoStats", {
          name: wizard.modpackName,
          done: formatInt(wizard.doneEntries),
        });

  /* ---- export card state ---- */
  const exportBusy = wizard.exportState === "running";
  const exportDone = wizard.exportState === "done";
  const zipBasename = wizard.exportZipPath?.split(/[\\/]/).at(-1) ?? "—";
  const overridesBasename = wizard.exportOverridesZipPath?.split(/[\\/]/).at(-1) ?? "—";

  function handleExportPrimary(): void {
    if (exportBusy) return;
    if (exportDone && wizard.exportZipPath !== null) {
      void moru.showItemInFolder(wizard.exportZipPath);
    } else {
      void wizard.startExport();
    }
  }

  async function copyExportPath(path: string): Promise<void> {
    await navigator.clipboard.writeText(path);
    setCopiedPath(path);
  }

  /* ---- upload: engine job -> moru.gg (anonymous allowed; a signed-in
     account attributes the pack and earns contributor score) ---- */
  async function handleUpload(): Promise<void> {
    if (
      wizard.translateJobId === null ||
      upload.kind === "running" ||
      confirmedPack === null
    )
      return;
    // Pin attribution now: reopenSession/reset may switch the wizard's
    // session while the upload job runs.
    const sourceSessionId = wizard.sessionId;
    closeUploadEvents.current?.();
    closeUploadEvents.current = null;
    const attempt = ++uploadAttempt.current;
    setUpload({ kind: "running", stage: "pack", current: 0, total: 0, message: "" });
    try {
      const job = await api.startUpload({
        translate_job_id: wizard.translateJobId,
        modpack_name: confirmedPack.name,
        curseforge_id: confirmedPack.id,
        ...(version.trim() !== "" ? { modpack_version: version.trim() } : {}),
        web_url: WEB_URL,
        share_mods: shareMods,
        ...(account.token !== null ? { api_token: account.token } : {}),
      } satisfies UploadParams);
      if (attempt !== uploadAttempt.current) return;
      const onFrame = makeUploadFrameHandler(sourceSessionId, setUpload);
      closeUploadEvents.current = openJobEvents(job.id, (frame) => {
        if (attempt !== uploadAttempt.current) return;
        onFrame(frame);
        if (frame.type === "done" || frame.type === "failed" || frame.type === "cancelled") {
          closeUploadEvents.current?.();
          closeUploadEvents.current = null;
        }
      });
    } catch (error) {
      if (attempt !== uploadAttempt.current) return;
      setUpload({
        kind: "failed",
        message: error instanceof Error ? error.message : String(error),
      });
    }
  }

  /** CurseForge modpack search via the web proxy (CORS-exempt main process). */
  async function handleSearch(): Promise<void> {
    const q = searchQuery.trim();
    if (q === "" || search.kind === "loading") return;
    setSearch({ kind: "loading" });
    try {
      const { results } = await web.searchModpacks(q);
      setSearch({ kind: "done", results });
    } catch (error) {
      setSearch({
        kind: "failed",
        message: error instanceof Error ? error.message : String(error),
      });
    }
  }

  const { requestReset, dialog: resetDialog } = useWizardResetGuard();

  function finishAnd(screen: "home" | "w1"): void {
    requestReset(screen);
  }

  return (
    <div className={`${WIZARD_PAGE_CLASS}`}>
      {kicker}

      {/* Success banner */}
      <div
        className={`relative mb-7 flex items-center gap-5 overflow-hidden border px-6 py-5 ${cancelled ? "border-amber/50" : "border-accent-lo"}`}
        style={{ background: "linear-gradient(135deg, var(--color-tint) 0%, var(--color-raised) 100%)" }}
      >
        <div
          className="absolute -right-5 -top-5 h-[140px] w-[140px] opacity-10"
          style={{
            backgroundImage: `radial-gradient(circle at 2px 2px, ${cancelled ? "#F5B454" : "#3DDC84"} 1px, transparent 1px)`,
            backgroundSize: "8px 8px",
          }}
        />
        <div
          className={`flex h-14 w-14 shrink-0 items-center justify-center ${cancelled ? "bg-amber" : "bg-accent"}`}
          style={{
            boxShadow: `0 0 32px ${cancelled ? "rgba(245,180,84,0.35)" : "rgba(61,220,132,0.35)"}`,
          }}
        >
          {cancelled ? (
            <svg viewBox="0 0 32 32" width="32" height="32" shapeRendering="crispEdges">
              <rect x="10" y="8" width="4" height="16" fill="#0A100D" />
              <rect x="18" y="8" width="4" height="16" fill="#0A100D" />
            </svg>
          ) : (
            <svg viewBox="0 0 32 32" width="32" height="32" shapeRendering="crispEdges">
              <rect x="6" y="16" width="4" height="4" fill="#0A100D" />
              <rect x="10" y="20" width="4" height="4" fill="#0A100D" />
              <rect x="14" y="16" width="4" height="4" fill="#0A100D" />
              <rect x="18" y="12" width="4" height="4" fill="#0A100D" />
              <rect x="22" y="8" width="4" height="4" fill="#0A100D" />
            </svg>
          )}
        </div>
        <div className="flex-1">
          <h1 className="mb-1 text-[24px] font-bold tracking-[-0.02em] text-text">
            {cancelled ? t("w6.banner.titleCancelled") : t("w6.banner.title")}
          </h1>
          <div className="mb-2 text-[13px] text-text2">
            {summary}
            {finalCostUsd !== null && ` · ${formatUsd(finalCostUsd)}`}
          </div>
          {stats !== null && (
            <div className="flex gap-3 font-mono text-[11px]">
              <span className="text-accent">
                {t("w6.banner.passRate", { percent: trim1(stats.coverage_percent) })}
              </span>
              <span className="text-purple">
                {t("w6.banner.tmReuse", {
                  percent: trim1(tmPercent),
                  count: formatInt(stats.tm_hits),
                })}
              </span>
              {(stats.migration_hits ?? 0) > 0 && (
                <span className="text-purple">
                  {t("w6.banner.migrationReuse", {
                    count: formatInt(stats.migration_hits ?? 0),
                  })}
                </span>
              )}
              <span className="text-blue">
                {t("w6.banner.quality", { score: trim1(stats.quality_score * 100) })}
              </span>
              {cachePercent !== null && cachePercent > 0 && (
                <span className="text-amber">
                  {cacheSavedUsd !== null
                    ? t("w6.banner.cacheSaved", {
                        percent: cachePercent,
                        saved: formatUsd(cacheSavedUsd),
                      })
                    : t("w6.banner.cacheHit", { percent: cachePercent })}
                </span>
              )}
            </div>
          )}
        </div>
      </div>

      <UndeliverableBand />

      {/* Export + Share */}
      <div className="mb-5 grid grid-cols-2 gap-4">
        {/* Left: export cards */}
        <div className="flex flex-col gap-3">
          <div className="flex items-center gap-2 font-mono text-xs font-bold uppercase tracking-[0.06em] text-text2">
            <div className="h-3 w-1 bg-accent" />
            {t("w6.export.heading")}
          </div>

          {/* Card 1: resource pack */}
          <div className="relative border border-line2 bg-raised p-5">
            <div className="flex items-start gap-[14px]">
              <div
                className="flex h-10 w-10 shrink-0 items-center justify-center border border-edge bg-card"
                style={{
                  backgroundImage:
                    "repeating-linear-gradient(45deg, transparent 0 3px, rgba(61,220,132,0.06) 3px 4px)",
                }}
              >
                <svg width="20" height="20" viewBox="0 0 20 20" shapeRendering="crispEdges" fill="#3DDC84">
                  <rect x="2" y="4" width="16" height="2" />
                  <rect x="2" y="4" width="2" height="12" />
                  <rect x="16" y="4" width="2" height="12" />
                  <rect x="2" y="14" width="16" height="2" />
                  <rect x="6" y="8" width="8" height="2" />
                  <rect x="6" y="10" width="8" height="2" />
                </svg>
              </div>
              <div className="flex-1">
                <div className="mb-1 flex items-center gap-2">
                  <div className="text-[15px] font-bold tracking-[-0.01em] text-text">
                    {t("w6.export.zipTitle")}
                  </div>
                  <span className="bg-accent/8 px-[5px] py-[2px] font-mono text-[10px] text-accent">
                    {t("w6.export.badgeRecommended")}
                  </span>
                </div>
                <div className="text-xs leading-[1.5] text-text2">{t("w6.export.zipDesc")}</div>
              </div>
            </div>

            <div className="mb-3 mt-[14px] grid grid-cols-2 gap-[6px] bg-card px-3 py-[10px] font-mono text-[11px]">
              <div>
                <span className="text-text3">{t("w6.export.fileName")} </span>
                <span className="text-text">{zipBasename}</span>
              </div>
              <div>
                <span className="text-text3">{t("w6.export.target")} </span>
                <span className="text-text">{wizard.targetLocale}</span>
              </div>
              <div>
                <span className="text-text3">{t("w6.export.entries")} </span>
                <span className="text-text">
                  {formatInt(stats?.translated_entries ?? wizard.doneEntries)}
                </span>
              </div>
              <div>
                <span className="text-text3">{t("w6.export.engine")} </span>
                <span className="text-text">v{moru.versions.app}</span>
              </div>
            </div>

            {exportDone && wizard.exportZipPath !== null && (
              <div className="mb-3 border border-accent-lo bg-tint px-3 py-2 font-mono text-[11px] leading-[1.5]">
                <span className="text-text3">{t("w6.export.savedTo")} </span>
                <span className="break-all text-accent">{wizard.exportZipPath}</span>
              </div>
            )}
            {wizard.exportState === "failed" && (
              <div className="mb-3 border border-red/30 bg-red/5 px-3 py-2 text-[11px] leading-[1.5]">
                <span className="font-semibold text-red">{t("w6.export.failed")}</span>
                <span className="text-text2"> — {t("w6.export.failedHint")}</span>
                <ErrorDetails raw={wizard.exportError} className="mt-1" />
              </div>
            )}

            <div className="grid grid-cols-[2fr_1fr] gap-[6px]">
              <button
                className="flex items-center justify-center gap-[6px] bg-accent p-[10px] text-[13px] font-bold text-sel-ink hover:bg-accent-hi disabled:cursor-not-allowed disabled:opacity-60"
                disabled={exportBusy}
                onClick={handleExportPrimary}
              >
                {exportBusy ? (
                  <>
                    <span className="inline-block h-3 w-3 animate-pxspin border-2 border-sel-ink border-t-transparent" />
                    {t("w6.export.creating")}
                  </>
                ) : exportDone && wizard.exportZipPath !== null ? (
                  <>
                    <svg width="12" height="12" viewBox="0 0 12 12" shapeRendering="crispEdges" fill="currentColor">
                      <rect x="1" y="2" width="4" height="2" />
                      <rect x="1" y="4" width="10" height="6" />
                      <rect x="5" y="3" width="6" height="1" />
                    </svg>
                    {t("common.action.openFolder")}
                  </>
                ) : wizard.exportState === "failed" ? (
                  t("common.action.retry")
                ) : (
                  <>
                    <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.5">
                      <path d="M6 1 V8 M3 5 L6 8 L9 5" />
                      <path d="M1 10 H11" />
                    </svg>
                    {t("w6.export.create")}
                  </>
                )}
              </button>
              <button
                className="border border-edge bg-transparent p-[10px] text-xs font-semibold text-text2 enabled:hover:border-edge2 enabled:hover:text-text disabled:cursor-not-allowed disabled:opacity-40"
                disabled={wizard.exportZipPath === null}
                onClick={() => {
                  if (wizard.exportZipPath !== null) void copyExportPath(wizard.exportZipPath);
                }}
              >
                {copiedPath !== null && copiedPath === wizard.exportZipPath
                  ? t("w6.export.copied")
                  : t("w6.export.copyPath")}
              </button>
            </div>
          </div>

          {/* Card 2: overrides zip — kubejs/config/ftbquests files a resource
              pack cannot carry; built by the same export job as card 1 */}
          <div className="border border-line2 bg-raised p-5">
            <div className="flex items-start gap-[14px]">
              <div className="flex h-10 w-10 shrink-0 items-center justify-center border border-edge bg-card">
                <svg width="20" height="20" viewBox="0 0 20 20" shapeRendering="crispEdges" fill="#F5B454">
                  <rect x="2" y="4" width="16" height="14" />
                  <rect x="4" y="2" width="12" height="2" />
                  <rect x="4" y="10" width="12" height="2" fill="#0A100D" />
                </svg>
              </div>
              <div className="flex-1">
                <div className="mb-1 flex items-center gap-2">
                  <div className="text-[15px] font-bold tracking-[-0.01em] text-text">
                    {t("w6.export.ovTitle")}
                  </div>
                  <span className="bg-amber/8 px-[5px] py-[2px] font-mono text-[10px] text-amber">
                    {t("w6.export.badgeAdvanced")}
                  </span>
                </div>
                <div className="text-xs leading-[1.5] text-text2">{t("w6.export.ovDesc")}</div>
              </div>
            </div>

            <div className="mb-3 mt-[14px] grid grid-cols-2 gap-[6px] bg-card px-3 py-[10px] font-mono text-[11px]">
              <div>
                <span className="text-text3">{t("w6.export.fileName")} </span>
                <span className="text-text">{overridesBasename}</span>
              </div>
              <div>
                <span className="text-text3">{t("w6.export.ovContents")} </span>
                <span className="text-text">{t("w6.export.ovContentsValue")}</span>
              </div>
            </div>

            {exportDone && wizard.exportOverridesZipPath !== null && (
              <div className="mb-3 border border-accent-lo bg-tint px-3 py-2 font-mono text-[11px] leading-[1.5]">
                <span className="text-text3">{t("w6.export.savedTo")} </span>
                <span className="break-all text-accent">{wizard.exportOverridesZipPath}</span>
              </div>
            )}
            {exportDone && wizard.exportOverridesZipPath === null && (
              <div className="mb-3 border border-edge bg-card px-3 py-2 text-[11px] leading-[1.5] text-text2">
                {t("w6.export.ovNone")}
              </div>
            )}

            <div className="grid grid-cols-[2fr_1fr] gap-[6px]">
              <button
                className="bg-line2 p-[10px] text-[13px] font-bold text-text enabled:hover:bg-edge disabled:cursor-not-allowed disabled:opacity-40"
                disabled={wizard.exportOverridesZipPath === null}
                title={exportDone ? undefined : t("w6.export.ovHint")}
                onClick={() => {
                  if (wizard.exportOverridesZipPath !== null)
                    void moru.showItemInFolder(wizard.exportOverridesZipPath);
                }}
              >
                {t("common.action.openFolder")}
              </button>
              <button
                className="border border-edge bg-transparent p-[10px] text-xs font-semibold text-text2 enabled:hover:border-edge2 enabled:hover:text-text disabled:cursor-not-allowed disabled:opacity-40"
                disabled={wizard.exportOverridesZipPath === null}
                onClick={() => {
                  if (wizard.exportOverridesZipPath !== null)
                    void copyExportPath(wizard.exportOverridesZipPath);
                }}
              >
                {copiedPath !== null && copiedPath === wizard.exportOverridesZipPath
                  ? t("w6.export.copied")
                  : t("w6.export.copyPath")}
              </button>
            </div>
          </div>

          {exportDone &&
            [
              { title: "w6.export.zipTitle", path: wizard.exportBilingualZipPath },
              { title: "w6.export.ovTitle", path: wizard.exportBilingualOverridesZipPath },
            ].map(({ title, path }) =>
              path == null ? null : (
                <div key={title} className="border border-line2 bg-raised p-5">
                  <div className="mb-1 flex flex-wrap items-center gap-2">
                    <div className="text-[15px] font-bold tracking-[-0.01em] text-text">
                      {t(title)}
                    </div>
                    <span className="bg-purple/8 px-[5px] py-[2px] font-mono text-[10px] text-purple">
                      {t("w6.export.bilingual")}
                    </span>
                  </div>
                  <div className="text-xs leading-[1.5] text-text2">
                    {t("w6.export.bilingualDesc")}
                  </div>
                  <div className="mb-3 mt-[14px] border border-accent-lo bg-tint px-3 py-2 font-mono text-[11px] leading-[1.5]">
                    <span className="text-text3">{t("w6.export.savedTo")} </span>
                    <span className="break-all text-accent">{path}</span>
                  </div>
                  <div className="grid grid-cols-[2fr_1fr] gap-[6px]">
                    <button
                      className="bg-line2 p-[10px] text-[13px] font-bold text-text hover:bg-edge"
                      onClick={() => void moru.showItemInFolder(path)}
                    >
                      {t("common.action.openFolder")}
                    </button>
                    <button
                      className="border border-edge bg-transparent p-[10px] text-xs font-semibold text-text2 hover:border-edge2 hover:text-text"
                      onClick={() => void copyExportPath(path)}
                    >
                      {copiedPath === path ? t("w6.export.copied") : t("w6.export.copyPath")}
                    </button>
                  </div>
                </div>
              ),
            )}
        </div>

        {/* Right: community share */}
        <div className="flex flex-col">
          <div className="mb-3">
            <div className="flex items-center gap-2 font-mono text-xs font-bold uppercase tracking-[0.06em] text-text2">
              <div className="h-3 w-1 bg-purple" />
              {t("w6.share.heading")}
            </div>
          </div>
          <div
            className="relative flex-1 overflow-hidden border border-[#5B3EBD] p-6"
            style={{ background: "linear-gradient(135deg, #16141F 0%, var(--color-raised) 100%)" }}
          >
            <div
              className="absolute -bottom-[30px] -right-[30px] h-[120px] w-[120px] opacity-15"
              style={{
                backgroundImage: "radial-gradient(circle at 2px 2px, var(--color-purple) 1px, transparent 1px)",
                backgroundSize: "6px 6px",
              }}
            />

            {/* Account row: connected user, or a sign-in prompt for guests */}
            <div className="relative mb-4 flex items-center gap-[10px] border border-edge bg-card px-[10px] py-2">
              {account.status === "connected" ? (
                <div
                  className="flex h-6 w-6 items-center justify-center font-mono text-[10px] font-bold text-bar"
                  style={{ background: "linear-gradient(135deg, var(--color-accent), var(--color-accent-lo))" }}
                >
                  {(account.name ?? "").slice(0, 2).toUpperCase() || "?"}
                </div>
              ) : (
                <div className="flex h-6 w-6 items-center justify-center bg-line2 font-mono text-[10px] font-bold text-text3">
                  ?
                </div>
              )}
              <div className="flex-1">
                <div className="text-xs font-semibold text-text">
                  {account.status === "connected" ? account.name : t("common.account.guest")}
                </div>
                <div className="flex items-center gap-1 text-[10px] text-text3">
                  <div className={`h-1 w-1 ${account.status === "connected" ? "bg-discord" : "bg-text4"}`} />
                  {account.status === "connected"
                    ? t("common.account.connected")
                    : t("w6.share.guestSub")}
                </div>
              </div>
              {account.status === "connected" ? (
                <span className="bg-purple/8 px-[6px] py-[3px] font-mono text-[10px] text-purple">
                  {t("w6.share.badgeConnected")}
                </span>
              ) : (
                <button
                  onClick={() => void account.login()}
                  disabled={account.pending}
                  className="border border-edge px-[6px] py-[3px] font-mono text-[10px] text-text2 hover:border-edge2 hover:text-text disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {t("w6.share.login")}
                </button>
              )}
            </div>

            {/* Pack to share */}
            <div className="relative mb-4">
              <div className="mb-2 font-mono text-[10px] font-bold uppercase tracking-[0.06em] text-text3">
                {t("w6.share.packLabel")}
              </div>
              <div className="flex gap-[10px] border border-edge bg-card p-[10px]">
                <div className="flex h-10 w-10 shrink-0 items-center justify-center border border-accent-lo bg-tint">
                  <span className="font-mono text-[13px] font-bold text-accent">
                    {packInitials(wizard.modpackName)}
                  </span>
                </div>
                <div className="min-w-0 flex-1">
                  <div className="truncate text-[13px] font-bold text-text">{wizard.modpackName}</div>
                  <div className="font-mono text-[11px] text-text3">
                    {t("w6.share.packMeta", {
                      source: wizard.sourceLocale,
                      target: wizard.targetLocale,
                      files: formatInt(stats?.total_files ?? 0),
                    })}
                  </div>
                </div>
              </div>
            </div>

            {/* CurseForge identity gate - upload requires a confirmed CF pack */}
            <div className="relative mb-4">
              <div className="mb-2 font-mono text-[10px] font-bold uppercase tracking-[0.06em] text-text3">
                {t("w6.share.cfLabel")}
              </div>
              {confirmedPack !== null ? (
                <div className="flex items-center gap-[10px] border border-accent-lo bg-tint p-[10px]">
                  {confirmedPack.logoUrl !== null ? (
                    <img
                      src={confirmedPack.logoUrl}
                      alt=""
                      className="h-9 w-9 shrink-0 border border-edge object-cover"
                    />
                  ) : (
                    <div className="flex h-9 w-9 shrink-0 items-center justify-center border border-accent-lo bg-card">
                      <span className="font-mono text-[12px] font-bold text-accent">
                        {packInitials(confirmedPack.name)}
                      </span>
                    </div>
                  )}
                  <div className="min-w-0 flex-1">
                    <div className="truncate text-[13px] font-bold text-text">
                      {confirmedPack.name}
                    </div>
                    <div className="font-mono text-[10px] text-text3">
                      CurseForge #{confirmedPack.id}
                    </div>
                  </div>
                  {autoPack !== null ? (
                    <span className="shrink-0 bg-accent/8 px-[6px] py-[3px] font-mono text-[10px] text-accent">
                      {t("w6.share.cfAutoDetected")}
                    </span>
                  ) : (
                    <button
                      className="shrink-0 border border-edge px-[6px] py-[3px] font-mono text-[10px] text-text2 hover:border-edge2 hover:text-text"
                      onClick={() => setPickedPack(null)}
                    >
                      {t("w6.share.cfChange")}
                    </button>
                  )}
                </div>
              ) : (
                <div className="border border-edge bg-card p-[10px]">
                  <div className="flex gap-[6px]">
                    <input
                      type="text"
                      value={searchQuery}
                      onChange={(e) => setSearchQuery(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") void handleSearch();
                      }}
                      placeholder={t("w6.share.cfSearchPlaceholder")}
                      className="min-w-0 flex-1 border border-edge bg-bar px-2 py-[6px] font-mono text-[11px] text-text placeholder:text-text4"
                    />
                    <button
                      className="shrink-0 border border-edge px-3 py-[6px] text-[11px] font-semibold text-text2 hover:border-edge2 hover:text-text disabled:cursor-not-allowed disabled:opacity-50"
                      disabled={search.kind === "loading" || searchQuery.trim() === ""}
                      onClick={() => void handleSearch()}
                    >
                      {search.kind === "loading"
                        ? t("w6.share.cfSearching")
                        : t("w6.share.cfSearch")}
                    </button>
                  </div>
                  {search.kind === "failed" && (
                    <div className="mt-2 text-[11px] leading-[1.5]">
                      <span className="font-semibold text-red">
                        {t("w6.share.cfSearchFailed")}
                      </span>
                      <span className="text-text2"> — {t("common.error.networkHint")}</span>
                      <ErrorDetails raw={search.message} className="mt-1" />
                    </div>
                  )}
                  {search.kind === "done" && search.results.length === 0 && (
                    <div className="mt-2 font-mono text-[10px] text-text3">
                      {t("w6.share.cfNoResults")}
                    </div>
                  )}
                  {search.kind === "done" && search.results.length > 0 && (
                    <div className="mt-2 max-h-[168px] overflow-y-auto border-t border-line">
                      {search.results.map((r) => (
                        <button
                          key={r.id}
                          className="flex w-full items-center gap-2 border-b border-line px-1 py-[6px] text-left hover:bg-raised-hover"
                          onClick={() =>
                            setPickedPack({ id: r.id, name: r.name, logoUrl: r.logoUrl })
                          }
                        >
                          {r.logoUrl !== null ? (
                            <img
                              src={r.logoUrl}
                              alt=""
                              className="h-7 w-7 shrink-0 border border-edge object-cover"
                            />
                          ) : (
                            <div className="flex h-7 w-7 shrink-0 items-center justify-center border border-edge bg-bar">
                              <span className="font-mono text-[10px] font-bold text-text3">
                                {packInitials(r.name)}
                              </span>
                            </div>
                          )}
                          <div className="min-w-0 flex-1">
                            <div className="truncate text-[12px] font-semibold text-text">
                              {r.name}
                            </div>
                            <div className="truncate font-mono text-[10px] text-text3">
                              {r.author ?? "—"} · ↓ {formatCompact(r.downloads)}
                            </div>
                          </div>
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              )}
              {confirmedPack !== null && (
                <div className="mt-[6px] flex items-center gap-[6px]">
                  <span className="shrink-0 font-mono text-[10px] text-text3">
                    {t("w6.share.cfVersionLabel")}
                  </span>
                  <input
                    type="text"
                    value={version}
                    onChange={(e) => setVersion(e.target.value)}
                    placeholder={t("w6.share.cfVersionPlaceholder")}
                    className="min-w-0 flex-1 border border-edge bg-card px-2 py-1 font-mono text-[11px] text-text placeholder:text-text4"
                  />
                </div>
              )}
            </div>

            <div className="relative mb-4">
              <button
                type="button"
                role="checkbox"
                aria-checked={shareMods}
                aria-describedby="share-mods-hint"
                disabled={account.token === null || upload.kind === "running" || upload.kind === "done"}
                onClick={() => setShareMods((value) => !value)}
                className="flex w-full items-start gap-[10px] border border-line2 bg-raised p-3 text-left disabled:cursor-not-allowed disabled:opacity-60"
              >
                {shareMods ? (
                  <span className="mt-px flex h-4 w-4 shrink-0 items-center justify-center bg-accent">
                    <svg width="10" height="10" viewBox="0 0 10 10" fill="none" stroke="currentColor" strokeWidth="2" className="text-sel-ink">
                      <path d="M1 5 L4 8 L9 2" />
                    </svg>
                  </span>
                ) : (
                  <span className="mt-px h-4 w-4 shrink-0 border border-edge bg-bar" />
                )}
                <span>
                  <span className="mb-[2px] block text-[12px] font-bold text-text">
                    {t("w6.share.modsTitle")}
                  </span>
                  <span id="share-mods-hint" className="block font-mono text-[11px] text-text3">
                    {t(account.token === null ? "w6.share.modsLoginHint" : "w6.share.modsHint")}
                  </span>
                </span>
              </button>
              {sourceJobId !== null && (
                <div className="mt-2 font-mono text-[11px] text-text3" role="status">
                  {modSourcesError || modSources?.status === "failed"
                    ? t("w6.share.modSourcesFailed")
                    : modSources === null
                      ? t("w6.share.modSourcesPending")
                      : t("w6.share.modSourcesProgress", {
                          resolved: formatInt(modSources.resolved),
                          total: formatInt(modSources.total),
                        })}
                </div>
              )}
            </div>

            {/* Public stats preview */}
            <div className="relative mb-4">
              <div className="mb-2 font-mono text-[10px] font-bold uppercase tracking-[0.06em] text-text3">
                {t("w6.share.statsLabel")}
              </div>
              <div className="grid grid-cols-4 gap-1">
                <div className="border border-edge bg-card p-2">
                  <div className="font-mono text-base font-bold text-text">
                    {stats !== null ? `${trim1(stats.coverage_percent)}%` : "—"}
                  </div>
                  <div className="font-mono text-[10px] text-text3">{t("w6.share.statCoverage")}</div>
                </div>
                <div className="border border-edge bg-card p-2">
                  <div className="font-mono text-base font-bold text-text">
                    {stats !== null ? formatInt(stats.translated_entries) : "—"}
                  </div>
                  <div className="font-mono text-[10px] text-text3">{t("w6.share.statEntries")}</div>
                </div>
                <div className="border border-edge bg-card p-2">
                  <div className="font-mono text-base font-bold text-text">
                    {stats !== null ? `${trim1(stats.quality_score * 100)}%` : "—"}
                  </div>
                  <div className="font-mono text-[10px] text-text3">{t("w6.share.statQuality")}</div>
                </div>
                <div className="border border-edge bg-card p-2">
                  <div className="font-mono text-base font-bold text-text">
                    {stats !== null ? formatDuration(stats.duration_seconds, lang) : "—"}
                  </div>
                  <div className="font-mono text-[10px] text-text3">{t("w6.share.statDuration")}</div>
                </div>
              </div>
            </div>

            <div
              className="relative mb-[14px] border px-3 py-[10px] text-[11px] leading-[1.5] text-text2"
              style={{
                background: "rgba(167,139,250,0.06)",
                borderColor: "rgba(167,139,250,0.2)",
              }}
            >
              <b className="text-purple">{t("w6.share.noticeStrong")}</b>
              {t("w6.share.noticeRest")}
            </div>

            {upload.kind === "done" ? (
              <div className="relative border border-accent-lo bg-tint px-4 py-3">
                <div className="mb-2 text-xs font-semibold leading-[1.5] text-accent">
                  {t("w6.share.done")}
                </div>
                {upload.modPacks !== null && (
                  <div className="mb-3 text-[11px] leading-[1.5] text-text2">
                    <div>
                      {t("w6.share.modsResult", {
                        shared: formatInt(upload.modPacks.shared),
                        skipped: formatInt(upload.modPacks.skipped),
                        failed: formatInt(upload.modPacks.failed),
                      })}
                    </div>
                    {upload.modPacks.skipped_reason !== undefined && (
                      <div className="mt-1 text-text3">
                        {t(`w6.share.modsSkipped.${upload.modPacks.skipped_reason}`)}
                      </div>
                    )}
                  </div>
                )}
                {upload.url !== null && (
                  <button
                    className="inline-flex items-center gap-[6px] border border-edge px-[10px] py-[6px] text-[11px] font-semibold text-text2 hover:border-edge2 hover:text-text"
                    onClick={() => {
                      if (upload.url !== null) void moru.openExternal(upload.url);
                    }}
                  >
                    {t("w6.share.viewOnWeb")}
                    <svg width="10" height="10" viewBox="0 0 10 10" fill="none" stroke="currentColor" strokeWidth="1.5">
                      <path d="M2 8 L8 2 M4 2 H8 V6" />
                    </svg>
                  </button>
                )}
                {upload.modpackSlug !== null && (
                  <button
                    className="mt-2 flex items-center gap-[6px] border border-edge px-[10px] py-[6px] text-left text-[11px] font-semibold text-text2 hover:border-edge2 hover:text-text"
                    onClick={() =>
                      void moru.openExternal(`${WEB_URL}/ko/modpack/${upload.modpackSlug}`)
                    }
                  >
                    {t("w6.share.buildBundle")}
                    <svg width="10" height="10" viewBox="0 0 10 10" fill="none" stroke="currentColor" strokeWidth="1.5" className="shrink-0">
                      <path d="M2 8 L8 2 M4 2 H8 V6" />
                    </svg>
                  </button>
                )}
              </div>
            ) : (
              <div className="relative">
                {upload.kind === "running" && (
                  <div className="mb-3 border border-edge bg-card p-3" role="status">
                    <div className="mb-2 flex items-center justify-between gap-2 font-mono text-[11px]">
                      <span className="font-semibold text-purple">
                        {t(
                          upload.stage === "pack"
                            ? "w6.share.stages.pack"
                            : upload.stage === "mods"
                              ? "w6.share.stages.mods"
                              : "w6.share.stages.upload",
                        )}
                      </span>
                      {upload.total > 0 && (
                        <span className="text-text2">
                          {formatInt(upload.current)}/{formatInt(upload.total)}
                        </span>
                      )}
                    </div>
                    <div
                      className="h-2 overflow-hidden bg-line2"
                      role="progressbar"
                      aria-label={t("w6.share.uploading")}
                      aria-valuemin={0}
                      aria-valuemax={upload.total > 0 ? upload.total : undefined}
                      aria-valuenow={upload.total > 0 ? Math.min(upload.total, Math.max(0, upload.current)) : undefined}
                    >
                      <div
                        className={`h-full bg-purple transition-[width] ${upload.total > 0 ? "" : "animate-pulse"}`}
                        style={{
                          width: upload.total > 0
                            ? `${Math.min(100, Math.max(0, (upload.current / upload.total) * 100))}%`
                            : "100%",
                        }}
                      />
                    </div>
                    {upload.message !== "" && (
                      <div className="mt-2 break-words font-mono text-[11px] text-text2">
                        {upload.message}
                      </div>
                    )}
                  </div>
                )}
                {upload.kind === "failed" && (
                  <div className="mb-2 text-[11px] leading-[1.5]">
                    <span className="font-semibold text-red">{t("w6.share.uploadFailed")}</span>
                    <span className="text-text2"> — {t("common.error.networkHint")}</span>
                    <ErrorDetails raw={upload.message} className="mt-1" />
                  </div>
                )}
                <button
                  className="flex w-full items-center justify-center gap-[6px] bg-[linear-gradient(180deg,#A78BFA_0%,#7C5CE0_100%)] p-3 text-[13px] font-bold text-sel-ink hover:bg-[linear-gradient(180deg,#B99BFF_0%,#8B6CE8_100%)] disabled:cursor-not-allowed disabled:opacity-60"
                  style={{ boxShadow: "0 4px 12px rgba(167,139,250,0.2)" }}
                  disabled={
                    upload.kind === "running" ||
                    wizard.translateJobId === null ||
                    confirmedPack === null
                  }
                  onClick={() => void handleUpload()}
                >
                  {upload.kind === "running" ? (
                    t("w6.share.uploading")
                  ) : (
                    <>
                      <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.8">
                        <path d="M7 1 V9 M4 6 L7 9 L10 6" />
                        <path d="M2 13 A5 3 0 0 1 12 13" />
                      </svg>
                      {upload.kind === "failed"
                        ? t("common.action.retry")
                        : t("w6.share.upload")}
                    </>
                  )}
                </button>
                {confirmedPack === null && (
                  <div className="mt-[6px] text-center font-mono text-[10px] text-text3">
                    {t("w6.share.cfRequired")}
                  </div>
                )}
                {account.status !== "connected" && (
                  <div className="mt-[6px] text-center font-mono text-[10px] text-text3">
                    {t("w6.share.anonUploadHint")}
                  </div>
                )}
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Bottom actions */}
      <div className="flex items-center justify-between border-t border-line pt-5">
        <Button variant="ghost" size={null} className="px-[18px] py-[10px] text-ui" onClick={() => go("w5")}>
          <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.5">
            <path d="M8 2 L4 6 L8 10" />
          </svg>
          {t("w6.actions.backToReview")}
        </Button>
        <div className="flex gap-2">
          <Button variant="secondary" onClick={() => finishAnd("home")}>
            {t("w6.actions.home")}
          </Button>
          <Button variant="neutral" onClick={() => finishAnd("w1")}>
            <svg width="12" height="12" viewBox="0 0 12 12" shapeRendering="crispEdges">
              <rect x="5" y="1" width="2" height="10" fill="currentColor" />
              <rect x="1" y="5" width="10" height="2" fill="currentColor" />
            </svg>
            {t("w6.actions.newPack")}
          </Button>
        </div>
      </div>
      {resetDialog}
    </div>
  );
}

/**
 * What the band should show, or null to render nothing.
 *
 * Split out from the component because the "nothing to show" decision is
 * the load-bearing part: a jar file whose guidebook definition holds only
 * lang keys produced no translation to lose, and the engine already
 * refuses to count it, so a non-zero file count here always means real
 * lost text. Zero must stay invisible.
 */
export function undeliverableSummary(
  stats: PipelineStats | null | undefined,
): { files: number; entries: number; mods: string[] } | null {
  const files = stats?.undeliverable_jar_files ?? 0;
  if (files <= 0) return null;
  return {
    files,
    entries: stats?.undeliverable_jar_entries ?? 0,
    mods: stats?.undeliverable_jar_mods ?? [],
  };
}

/**
 * Translations that succeeded but cannot be installed.
 *
 * A guidebook definition lives inside the mod jar's own `data/` tree, and
 * Patchouli reads it straight out of the jar rather than through the
 * resource-pack or data-pack stack — so nothing we ship can override it.
 *
 * Deliberately NOT the hardcoded-text band on W2Scan, in wording or in
 * placement. That one means "we could not translate this" and the fix
 * belongs to the mod author; this one means "we translated it but cannot
 * install it" and the fix is repackaging the jar. Different next action,
 * different phase, different screen.
 *
 * Renders nothing at zero, which is the normal case: a real 330-jar pack
 * produced exactly one. An always-present empty warning would only teach
 * users to ignore the panel beside it.
 */
function UndeliverableBand(): React.JSX.Element | null {
  const { t } = useTranslation();
  const found = undeliverableSummary(useWizard((s) => s.stats));
  if (found === null) return null;
  const { files, entries, mods } = found;

  return (
    <div className="mb-7 border border-amber/40 bg-amber/5 px-5 py-4">
      <div className="mb-1.5 flex items-center gap-2 font-mono text-xs font-bold uppercase tracking-[0.06em] text-amber">
        <div className="h-3 w-1 bg-amber" />
        {t("w6.undeliverable.header")}
        <span className="font-normal normal-case tracking-normal text-text3">
          {t("w6.undeliverable.summary", {
            files: formatInt(files),
            entries: formatInt(entries),
          })}
        </span>
      </div>
      <p className="m-0 mb-2 text-[12px] leading-relaxed text-text2">
        {t("w6.undeliverable.body")}
      </p>
      {mods.length > 0 && (
        <div className="mb-2 flex flex-wrap gap-1">
          {mods.map((mod) => (
            <span
              key={mod}
              className="border border-edge bg-bar px-[5px] py-[2px] font-mono text-[10px] text-text2"
            >
              {mod}
            </span>
          ))}
        </div>
      )}
      <p className="m-0 text-[11px] leading-relaxed text-text3">
        {t("w6.undeliverable.hint")}
      </p>
    </div>
  );
}
