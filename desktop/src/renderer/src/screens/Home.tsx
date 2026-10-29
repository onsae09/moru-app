/** Home dashboard. */

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Trans, useTranslation } from "react-i18next";

import { toast } from "@/components/Toast";
import { useWizardResetGuard } from "@/components/WizardResetGuard";
import { moru } from "@/lib/bridge";
import { formatCompact, formatInt, formatRelative, formatUsd, packInitials } from "@/lib/format";
import { modelDisplayName } from "@/lib/models";
import { WEB_URL, web } from "@/lib/web";
import { useAccount } from "@/stores/account";
import { useRouter } from "@/stores/router";
import { aggregateStats, useSessions, type SessionRecord } from "@/stores/sessions";
import { useSettings } from "@/stores/settings";
import { selectQueueActive, useTranslationQueue } from "@/stores/translationQueue";
import { useWizard } from "@/stores/wizard";

import { deleteSessionWithToast } from "./sessionActions";

/**
 * TOKENS_PER_ENTRY: TM-savings heuristic. Each TM hit skips one LLM round
 * trip for that entry; an average entry costs roughly 40 prompt+completion
 * tokens, so saved tokens = tmHits x 40.
 */
const TOKENS_PER_ENTRY = 40;
/** Blended price assumption for the "saved" estimate: $3 per 1M tokens. */
const USD_PER_MILLION_TOKENS = 3;

const DOT_PATTERN: React.CSSProperties = {
  backgroundImage: "radial-gradient(circle at 2px 2px, #3DDC84 1px, transparent 1px)",
  backgroundSize: "4px 4px",
};

/** Notification dot color by web notification type. */
const NOTIF_DOT: Record<string, string> = {
  correction_proposed: "bg-accent",
  correction_accepted: "bg-accent",
  correction_rejected: "bg-red",
  review_posted: "bg-blue",
  request_fulfilled: "bg-purple",
  request_claimed: "bg-purple",
};

function ImportIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.5">
      <path d="M2 6 H10 M6 2 L10 6 L6 10" />
    </svg>
  );
}

function TrendUpIcon() {
  return (
    <svg width="10" height="10" viewBox="0 0 10 10" fill="none" stroke="currentColor" strokeWidth="1.5">
      <path d="M2 7 L5 4 L8 7" />
    </svg>
  );
}

function ClockIcon() {
  return (
    <svg width="10" height="10" viewBox="0 0 10 10" fill="none" stroke="currentColor" strokeWidth="1.5">
      <circle cx="5" cy="5" r="4" />
      <path d="M5 3 V5 L7 6" />
    </svg>
  );
}

function FolderIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.5">
      <rect x="1" y="3" width="10" height="7" />
      <path d="M4 3 V1 H8 V3" />
    </svg>
  );
}

function StatusBadge({ record }: { record: SessionRecord }) {
  const { t } = useTranslation();
  const base =
    "flex items-center gap-1 px-1.5 py-[3px] font-mono text-[10px] font-bold tracking-[0.06em] uppercase";
  if (record.status === "running") {
    return (
      <div className={`${base} bg-accent/[0.12] text-accent`}>
        <div className="h-1 w-1 animate-pxpulse bg-accent" />
        {t("common.status.running")}
      </div>
    );
  }
  if (record.status === "done") {
    return record.sharedUrl !== null ? (
      <div className={`${base} bg-blue/[0.12] text-blue`}>{t("common.status.shared")}</div>
    ) : (
      <div className={`${base} bg-accent/[0.12] text-accent`}>{t("common.status.done")}</div>
    );
  }
  if (record.status === "interrupted") {
    return (
      <div className={`${base} bg-amber/[0.12] text-amber`}>{t("common.status.interrupted")}</div>
    );
  }
  return <div className={`${base} bg-red/[0.12] text-red`}>{t("common.status.cancelled")}</div>;
}

function RecentJobCard({
  record,
  onResume,
  onResumeTranslation,
  onRetry,
  onRemove,
}: {
  record: SessionRecord;
  onResume: () => void;
  onResumeTranslation: () => void;
  onRetry: () => void;
  onRemove: () => void;
}) {
  const { t, i18n } = useTranslation();
  const lang = i18n.language === "en" ? "en" : "ko";
  const running = record.status === "running";
  const pct =
    record.totalEntries > 0
      ? Math.min(100, Math.round((record.doneEntries / record.totalEntries) * 100))
      : 0;
  const when = formatRelative(record.finishedAt ?? record.createdAt, lang);

  return (
    <div className="relative overflow-hidden border border-line2 bg-raised p-4 hover:border-edge2 hover:bg-raised-hover">
      {/* status accent bar */}
      {running ? (
        <div
          className="absolute inset-x-0 top-0 h-0.5"
          style={{ background: `linear-gradient(90deg, #3DDC84 0%, #1F8A5B ${pct}%, #1F2B25 ${pct}%)` }}
        />
      ) : (
        <div
          className={`absolute inset-x-0 top-0 h-0.5 ${
            record.status === "done"
              ? "bg-accent-lo"
              : record.status === "interrupted"
                ? "bg-amber"
                : "bg-red"
          }`}
        />
      )}

      <div className="mb-3 flex items-start gap-3">
        <div
          className="flex h-11 w-11 shrink-0 items-center justify-center border border-edge bg-card"
          style={
            running
              ? {
                  backgroundImage:
                    "repeating-linear-gradient(45deg, transparent 0 4px, rgba(61,220,132,0.06) 4px 5px)",
                }
              : undefined
          }
        >
          <span className={`font-mono text-[13px] font-bold ${running ? "text-accent" : "text-text2"}`}>
            {packInitials(record.modpackName)}
          </span>
        </div>
        <div className="min-w-0 flex-1">
          <div className="mb-[3px] truncate text-[13px] font-bold tracking-[-0.01em] text-text">
            {record.modpackName}
          </div>
          <div className="truncate font-mono text-[11px] text-text3">
            {modelDisplayName(record.model)} · {record.targetLocale}
          </div>
        </div>
        <StatusBadge record={record} />
      </div>

      {running && (
        <>
          <div className="mb-2 flex items-center gap-2 font-mono text-[11px] text-text2">
            <span className="font-bold text-accent">{pct}%</span>
            <span className="text-text4">·</span>
            <span>
              {formatInt(record.doneEntries)} / {formatInt(record.totalEntries)}
            </span>
          </div>
          {/* pixel progress bar */}
          <div className="flex h-1.5 gap-px border border-line bg-bar p-px">
            <div
              style={{
                flex: pct,
                background: "#3DDC84",
                backgroundImage:
                  "repeating-linear-gradient(90deg, transparent 0 3px, rgba(0,0,0,0.2) 3px 4px)",
              }}
            />
            <div style={{ flex: 100 - pct }} />
          </div>
          <button
            onClick={onResume}
            className="mt-3 w-full bg-line2 p-2 text-xs font-semibold text-text hover:bg-edge"
          >
            {t("home.recent.resume")}
          </button>
        </>
      )}

      {record.status === "done" && (
        <>
          <div className="mb-2 flex items-center gap-3 font-mono text-[11px] text-text2">
            <span>{t("home.recent.entries", { n: formatInt(record.doneEntries) })}</span>
            {record.stats !== null && (
              <>
                <span className="text-text4">·</span>
                <span>
                  {t("home.recent.passRate", {
                    percent: (record.stats.quality_score * 100).toFixed(1),
                  })}
                </span>
              </>
            )}
          </div>
          <div className="mb-3 flex items-center gap-1.5 text-[11px] text-text3">
            <ClockIcon />
            <span>{t("home.recent.doneAt", { when })}</span>
          </div>
          <div className="grid grid-cols-2 gap-1">
            <button
              onClick={() => {
                if (record.exportZipPath !== null) void moru.showItemInFolder(record.exportZipPath);
              }}
              disabled={record.exportZipPath === null}
              className="bg-line2 p-2 text-[11px] font-semibold text-text enabled:hover:bg-edge disabled:opacity-40"
            >
              {t("home.recent.folder")}
            </button>
            <button
              onClick={() => {
                if (record.sharedUrl !== null) void moru.openExternal(record.sharedUrl);
              }}
              disabled={record.sharedUrl === null}
              className="bg-line2 p-2 text-[11px] font-semibold text-text enabled:hover:bg-edge disabled:opacity-40"
            >
              {t("home.recent.shareLink")}
            </button>
          </div>
        </>
      )}

      {(record.status === "failed" ||
        record.status === "cancelled" ||
        record.status === "interrupted") && (
        <>
          <div className="mb-1.5 text-xs leading-[1.4] text-text2">
            {record.error !== null && record.error !== ""
              ? record.error
              : t(
                  record.status === "interrupted"
                    ? "home.recent.interruptedFallback"
                    : record.status === "cancelled"
                      ? "home.recent.cancelledFallback"
                      : "home.recent.failedFallback",
                )}
          </div>
          <div className="mb-3 font-mono text-[11px] text-text3">
            {formatInt(record.doneEntries)} / {formatInt(record.totalEntries)} · {when}
          </div>
          <div className="grid grid-cols-2 gap-1">
            {record.resumable ? (
              <button
                onClick={onResumeTranslation}
                className="bg-accent p-2 text-[11px] font-bold text-bar hover:bg-accent-hi"
              >
                {t("home.recent.resumeTranslation")}
              </button>
            ) : (
              <button
                onClick={onRetry}
                className="bg-line2 p-2 text-[11px] font-semibold text-text hover:bg-edge"
              >
                {t("home.recent.retry")}
              </button>
            )}
            <button
              onClick={onRemove}
              className="bg-line2 p-2 text-[11px] font-semibold text-text2 hover:bg-edge hover:text-text"
            >
              {t("home.recent.delete")}
            </button>
          </div>
        </>
      )}
    </div>
  );
}

/** Live community notifications card; guest CTA until logged in. */
function CommunityCard() {
  const { t, i18n } = useTranslation();
  const account = useAccount();
  const queryClient = useQueryClient();
  const lang: "ko" | "en" = i18n.language === "en" ? "en" : "ko";

  const notifQuery = useQuery({
    queryKey: ["web-notifications", account.token],
    enabled: account.token !== null,
    refetchInterval: 90_000,
    retry: false,
    queryFn: () => web.notifications(account.token ?? ""),
  });
  const markAll = useMutation({
    mutationFn: () => web.markRead(account.token ?? ""),
    onSuccess: () =>
      void queryClient.invalidateQueries({ queryKey: ["web-notifications"] }),
  });

  const unread = notifQuery.data?.unread ?? 0;
  const items = notifQuery.data?.notifications.slice(0, 4) ?? [];

  return (
    <div className="border border-line2 bg-raised p-5">
      <div className="mb-3 flex items-center justify-between">
        <div className="font-mono text-[11px] font-semibold tracking-[0.06em] text-text3 uppercase">
          {t("home.community.title")}
        </div>
        {account.status === "connected" && notifQuery.data !== undefined && (
          <div
            className={`font-mono text-[10px] ${unread > 0 ? "text-accent" : "text-text3"}`}
          >
            ● {t("home.community.newCount", { n: unread })}
          </div>
        )}
      </div>

      {account.status !== "connected" ? (
        <div className="flex flex-col items-center py-6 text-center">
          <div className="mb-3 h-6 w-6 opacity-40" style={DOT_PATTERN} />
          <div className="mb-1 text-xs font-semibold text-text2">
            {t("home.community.loginTitle")}
          </div>
          <div className="mb-3 text-[11px] leading-[1.4] text-text3">
            {t("home.community.loginDesc")}
          </div>
          <button
            onClick={() => void account.login()}
            disabled={account.pending}
            className="bg-accent px-3.5 py-2 text-xs font-bold text-sel-ink hover:bg-accent-hi disabled:cursor-not-allowed disabled:opacity-60"
          >
            {account.pending ? t("home.community.loginPending") : t("home.community.login")}
          </button>
        </div>
      ) : notifQuery.isError ? (
        <div className="py-6 text-center font-mono text-[11px] text-text3">
          {t("home.community.loadError")}
        </div>
      ) : items.length === 0 ? (
        <div className="flex flex-col items-center py-6 text-center">
          <div className="mb-3 h-6 w-6 opacity-40" style={DOT_PATTERN} />
          <div className="mb-1 text-xs font-semibold text-text2">
            {t("home.community.empty")}
          </div>
          <div className="text-[11px] leading-[1.4] text-text3">
            {t("home.community.emptyDesc")}
          </div>
        </div>
      ) : (
        <>
          <div className="flex flex-col">
            {items.map((n, index) => {
              const known = n.type in NOTIF_DOT;
              const href =
                n.type === "request_claimed"
                  ? `${WEB_URL}/${lang}/requests`
                  : n.payload.packId !== undefined
                    ? `${WEB_URL}/${lang}/pack/${n.payload.packId}`
                    : null;
              return (
                <button
                  key={n.id}
                  disabled={href === null}
                  onClick={() => {
                    if (href !== null) void moru.openExternal(href);
                  }}
                  className={`flex items-start gap-2.5 py-2 text-left enabled:hover:bg-hover ${
                    index < items.length - 1 ? "border-b border-line" : ""
                  }`}
                >
                  <div
                    className={`mt-1.5 h-1.5 w-1.5 shrink-0 ${NOTIF_DOT[n.type] ?? "bg-text4"} ${
                      n.readAt !== null ? "opacity-30" : ""
                    }`}
                  />
                  <div className="min-w-0 flex-1">
                    <div className="text-xs leading-[1.4] text-text">
                      {known ? (
                        <Trans
                          i18nKey={`home.community.types.${n.type}`}
                          values={{
                            name: n.payload.authorName ?? n.payload.claimerName ?? "?",
                            pack: n.payload.modpackName ?? "?",
                          }}
                          components={[<b key="0" />]}
                        />
                      ) : (
                        n.type
                      )}
                    </div>
                    <div className="mt-0.5 text-[11px] text-text3">
                      {formatRelative(Date.parse(n.createdAt), lang)}
                    </div>
                  </div>
                </button>
              );
            })}
          </div>
          <div className="mt-2 flex items-center justify-between border-t border-line pt-2">
            <button
              onClick={() => markAll.mutate()}
              disabled={unread === 0 || markAll.isPending}
              className="font-mono text-[11px] text-text3 enabled:hover:text-text disabled:opacity-40"
            >
              {t("home.community.markAllRead")}
            </button>
            <button
              onClick={() => void moru.openExternal(`${WEB_URL}/${lang}/notifications`)}
              className="font-mono text-[11px] text-accent hover:text-accent-hi"
            >
              {t("home.community.visit")} ↗
            </button>
          </div>
        </>
      )}
    </div>
  );
}

export function HomeScreen() {
  const { t } = useTranslation();
  const go = useRouter((s) => s.go);
  const sessions = useSessions((s) => s.sessions);
  const startSession = useWizard((s) => s.startSession);
  const resumeSession = useWizard((s) => s.resumeSession);
  const reopenSession = useWizard((s) => s.reopenSession);
  const resumeTranslate = useWizard((s) => s.resumeTranslate);
  const recentFolders = useSettings((s) => s.recentFolders);
  const queueActive = useTranslationQueue(selectQueueActive);
  const { requestReset, dialog: resetDialog } = useWizardResetGuard();
  const [dragOver, setDragOver] = useState(false);

  const stats = useMemo(() => aggregateStats(sessions), [sessions]);
  const recent = sessions.slice(0, 3);

  const tmTokens = stats.tmHits * TOKENS_PER_ENTRY;
  const tmCompact = formatCompact(tmTokens);
  const tmSuffix = tmCompact.endsWith("M") || tmCompact.endsWith("K") ? tmCompact.slice(-1) : "";
  const tmValue = tmSuffix === "" ? tmCompact : tmCompact.slice(0, -1);
  const tmSavedUsd = formatUsd((tmTokens * USD_PER_MILLION_TOKENS) / 1_000_000);
  const localPacks = Math.max(stats.completedPacks - stats.sharedPacks, 0);
  const recentFolder: string | null = recentFolders[0] ?? null;

  const beginSession = async (path: string) => {
    if (queueActive) {
      go("queue");
      return;
    }
    const probe = await moru.probeModpack(path);
    startSession(path, probe);
    go("w1");
  };

  const handleImport = async () => {
    if (queueActive) {
      go("queue");
      return;
    }
    const path = await moru.pickFolder();
    if (path === null) return;
    await beginSession(path);
  };

  const handleResume = async (id: string) => {
    if (queueActive) {
      go("queue");
      return;
    }
    const result = await reopenSession(id);
    if (result === "ok") {
      go("w4");
      return;
    }
    const resumable =
      useSessions.getState().sessions.find((s) => s.id === id)?.resumable === true;
    if (result === "busy") toast(t("history.reopen.busy"), { tone: "warn" });
    else if (resumable) toast(t("history.reopen.resumable"), { tone: "warn" });
    else toast(t("history.reopen.gone"), { tone: "err" });
  };

  const handleResumeTranslation = async (id: string) => {
    if (queueActive) {
      go("queue");
      return;
    }
    const outcome = await resumeTranslate(id);
    if (outcome === "started") {
      go("w4");
      return;
    }
    if (outcome === "busy") toast(t("history.reopen.busy"), { tone: "warn" });
    else if (outcome === "gone") toast(t("history.resume.gone"), { tone: "warn" });
    else {
      toast(t("history.resume.failed", { error: useWizard.getState().runError ?? "" }), {
        tone: "err",
      });
    }
  };

  const handleRetry = (id: string) => {
    if (queueActive) {
      go("queue");
      return;
    }
    if (!resumeSession(id)) return;
    go("w1");
  };

  const handleDrop = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setDragOver(false);
    const file = e.dataTransfer.files.item(0);
    if (file === null) return;
    const path = moru.pathForFile(file);
    if (path === "") return;
    void beginSession(path);
  };

  return (
    <div className="animate-fade-in-up max-w-[1400px] px-10 py-8">
      {/* Header */}
      <div className="mb-7 flex items-end justify-between">
        <div>
          <div className="mb-1.5 font-mono text-xs font-semibold tracking-[0.08em] text-text3 uppercase">
            <span className="text-accent">▍</span> {t("home.greeting")}
          </div>
          <h1 className="text-[28px] font-bold tracking-[-0.02em] text-text">
            <Trans i18nKey="home.title">
              오늘도 <span className="text-accent">한글화</span> 해볼까요?
            </Trans>
          </h1>
        </div>
        <div className="flex gap-2">
          <button
            onClick={() => go("queue")}
            className="flex items-center gap-1.5 border border-edge bg-raised px-3.5 py-2 text-xs font-medium text-text2 hover:border-edge2 hover:text-text"
          >
            <span className="font-mono">≡</span>
            {t("home.queue")}
          </button>
          <button
            onClick={() => void handleImport()}
            className="flex items-center gap-1.5 border border-edge bg-raised px-3.5 py-2 text-xs font-medium text-text2 hover:border-edge2 hover:text-text"
          >
            <ImportIcon />
            {t("home.import")}
          </button>
        </div>
      </div>

      {/* Stats strip */}
      <div className="mb-8 grid grid-cols-4 gap-3">
        <div className="relative overflow-hidden border border-line2 bg-raised px-[18px] py-4">
          <div className="mb-2 text-[11px] font-semibold tracking-[0.06em] text-text3 uppercase">
            {t("home.stats.totalEntries")}
          </div>
          <div className="font-mono text-[26px] font-bold tracking-[-0.02em] text-text">
            {formatInt(stats.totalTranslated)}
          </div>
          <div className="mt-1.5 flex items-center gap-1 text-[11px] text-accent">
            <TrendUpIcon />
            {t("home.stats.thisWeek", { n: formatInt(stats.translatedThisWeek) })}
          </div>
        </div>

        <div className="border border-line2 bg-raised px-[18px] py-4">
          <div className="mb-2 text-[11px] font-semibold tracking-[0.06em] text-text3 uppercase">
            {t("home.stats.tmTokens")}
          </div>
          <div className="font-mono text-[26px] font-bold tracking-[-0.02em] text-text">
            {tmValue}
            {tmSuffix !== "" && <span className="text-lg text-text3">{tmSuffix}</span>}
          </div>
          <div className="mt-1.5 text-[11px] text-purple">
            {t("home.stats.tmSaved", { amount: tmSavedUsd })}
          </div>
        </div>

        <div className="border border-line2 bg-raised px-[18px] py-4">
          <div className="mb-2 text-[11px] font-semibold tracking-[0.06em] text-text3 uppercase">
            {t("home.stats.completedPacks")}
          </div>
          <div className="font-mono text-[26px] font-bold tracking-[-0.02em] text-text">
            {formatInt(stats.completedPacks)}
          </div>
          <div className="mt-1.5 text-[11px] text-text3">
            {t("home.stats.packsBreakdown", {
              shared: formatInt(stats.sharedPacks),
              local: formatInt(localPacks),
            })}
          </div>
        </div>

        {/* Contribution ranking - web platform not launched yet */}
        <div
          className="relative border border-accent-lo px-[18px] py-4"
          style={{ background: "linear-gradient(135deg, #14201A 0%, #141C18 100%)" }}
        >
          <div className="absolute top-2 right-2 h-6 w-6 opacity-40" style={DOT_PATTERN} />
          <div className="mb-2 text-[11px] font-semibold tracking-[0.06em] text-accent uppercase">
            {t("home.stats.ranking")}
          </div>
          <div className="font-mono text-[26px] font-bold tracking-[-0.02em] text-text">—</div>
          <div className="mt-1.5 text-[11px] text-text2">{t("home.stats.rankingLocked")}</div>
        </div>
      </div>

      {/* Recent Jobs */}
      <div className="mb-8">
        <div className="mb-3.5 flex items-center justify-between">
          <h2 className="text-base font-bold tracking-[-0.01em] text-text">
            {t("home.recent.title")}
          </h2>
          <button
            onClick={() => go("history")}
            className="text-xs font-medium text-text3 hover:text-accent"
          >
            {t("home.recent.viewAll")}
          </button>
        </div>

        {recent.length === 0 ? (
          <div className="relative overflow-hidden border border-line2 bg-raised px-6 py-12 text-center">
            <div
              className="pointer-events-none absolute inset-0 opacity-5"
              style={{ ...DOT_PATTERN, backgroundSize: "8px 8px" }}
            />
            <div className="relative">
              <div className="mx-auto mb-3.5 h-6 w-6 opacity-40" style={DOT_PATTERN} />
              <div className="mb-1 text-[13px] font-bold text-text">{t("home.empty.title")}</div>
              <div className="mb-4 text-xs text-text3">{t("home.empty.desc")}</div>
              <button
                onClick={() => {
                  if (queueActive) go("queue");
                  else requestReset("w1");
                }}
                className="bg-accent px-4 py-2.5 text-[13px] font-bold text-bar hover:bg-accent-hi"
              >
                {t("home.empty.cta")}
              </button>
            </div>
          </div>
        ) : (
          <div className="grid grid-cols-3 gap-3">
            {recent.map((record) => (
              <RecentJobCard
                key={record.id}
                record={record}
                onResume={() => void handleResume(record.id)}
                onResumeTranslation={() => void handleResumeTranslation(record.id)}
                onRetry={() => handleRetry(record.id)}
                onRemove={() => void deleteSessionWithToast(record.id, t)}
              />
            ))}
          </div>
        )}
      </div>

      {/* Bottom split: Quick start + Community activity */}
      <div className="grid grid-cols-[1.4fr_1fr] gap-4">
        <div
          className={`relative overflow-hidden border bg-raised p-5 ${dragOver ? "border-accent" : "border-line2"}`}
          onDragOver={(e) => {
            e.preventDefault();
            setDragOver(true);
          }}
          onDragLeave={() => setDragOver(false)}
          onDrop={handleDrop}
        >
          <div
            className="pointer-events-none absolute -right-5 -bottom-5 h-[120px] w-[120px] opacity-[0.08]"
            style={{ ...DOT_PATTERN, backgroundSize: "8px 8px" }}
          />
          <div className="mb-2 font-mono text-[11px] font-semibold tracking-[0.06em] text-text3 uppercase">
            {t("home.quick.eyebrow")}
          </div>
          <h3 className="mb-1.5 text-lg font-bold tracking-[-0.01em] text-text">
            {t("home.quick.title")}
          </h3>
          <p className="mb-4 text-[13px] leading-[1.5] text-text2">
            <Trans i18nKey="home.quick.desc">
              CurseForge, Modrinth, Prism, 또는{" "}
              <code className="bg-bar px-[5px] py-px font-mono text-xs text-accent">mods/</code>가
              있는 어떤 폴더든 됩니다.
            </Trans>
          </p>
          <div className="flex gap-2">
            <button
              onClick={() => void handleImport()}
              className="flex items-center gap-1.5 bg-accent px-4 py-2.5 text-[13px] font-bold text-bar hover:bg-accent-hi"
            >
              <FolderIcon />
              {t("home.quick.pickFolder")}
            </button>
            <button
              onClick={() => {
                if (recentFolder !== null) void beginSession(recentFolder);
              }}
              disabled={recentFolder === null}
              className="border border-edge bg-transparent px-4 py-2.5 text-[13px] font-semibold text-text2 enabled:hover:border-edge2 enabled:hover:text-text disabled:opacity-40"
            >
              {t("home.quick.fromRecent")}
            </button>
          </div>
        </div>

        {/* Community alerts */}
        <CommunityCard />
      </div>
      {resetDialog}
    </div>
  );
}
