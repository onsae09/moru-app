/** Hub navigation sidebar. */

import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { useWizardResetGuard } from "@/components/WizardResetGuard";
import { moru } from "@/lib/bridge";
import { WEB_URL } from "@/lib/web";
import { useAccount } from "@/stores/account";
import { useRouter } from "@/stores/router";
import { useSessions } from "@/stores/sessions";
import { selectQueueActive, useTranslationQueue } from "@/stores/translationQueue";
import type { UpdateState } from "../../../shared/bridge";

function ActiveBar() {
  return <div className="absolute top-1.5 bottom-1.5 left-0 w-[3px] bg-accent" />;
}

function NavButton({
  active,
  onClick,
  icon,
  label,
  trailing,
}: {
  active: boolean;
  onClick: () => void;
  icon: React.ReactNode;
  label: string;
  trailing?: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-current={active ? "page" : undefined}
      className="relative flex items-center gap-2.5 px-3 py-[9px] text-left text-[13px] font-medium text-text2 hover:bg-hover hover:text-text"
    >
      {active && <ActiveBar />}
      {icon}
      <span>{label}</span>
      {trailing}
    </button>
  );
}

export function Sidebar() {
  const { t } = useTranslation();
  const screen = useRouter((s) => s.screen);
  const go = useRouter((s) => s.go);
  const sessionCount = useSessions((s) => s.sessions.length);
  const queueCount = useTranslationQueue(
    (s) =>
      s.items.filter(
        (item) =>
          item.status === "pending" ||
          item.status === "scanning" ||
          item.status === "translating",
      ).length,
  );
  const queueActive = useTranslationQueue(selectQueueActive);
  const account = useAccount();
  const [update, setUpdate] = useState<UpdateState>({ status: "idle" });

  useEffect(() => {
    void moru.updates.getState().then(setUpdate);
    return moru.updates.onState(setUpdate);
  }, []);

  const { requestReset, dialog: resetDialog } = useWizardResetGuard();

  const startNewTranslation = useCallback((): void => {
    if (queueActive) {
      go("queue");
      return;
    }
    requestReset("w1");
  }, [go, queueActive, requestReset]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent): void => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "n") {
        event.preventDefault();
        startNewTranslation();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [startNewTranslation]);

  const updateVisible = update.status === "available" || update.status === "downloading" || update.status === "ready";

  return (
    <nav className="flex w-[232px] shrink-0 flex-col border-r border-line bg-panel">
      {/* New Translation CTA */}
      <div className="p-3.5">
        <button
          onClick={startNewTranslation}
          className="flex w-full items-center gap-2 px-3 py-2.5 text-[13px] font-bold text-bar hover:brightness-110 active:translate-y-px"
          style={{
            background: "linear-gradient(180deg, #3DDC84 0%, #2FB86B 100%)",
            boxShadow: "0 1px 0 #56ea99 inset, 0 2px 0 #1F8A5B, 0 6px 12px rgba(61,220,132,0.15)",
          }}
        >
          <svg width="14" height="14" viewBox="0 0 14 14" shapeRendering="crispEdges">
            <rect x="6" y="1" width="2" height="12" fill="currentColor" />
            <rect x="1" y="6" width="12" height="2" fill="currentColor" />
          </svg>
          <span>{t("common.nav.newTranslation")}</span>
          <span className="ml-auto font-mono text-[10px] font-semibold opacity-55">⌘N</span>
        </button>
      </div>

      <div className="mx-3.5 h-px bg-line" />

      {/* Nav items */}
      <div className="flex flex-col gap-0.5 px-2 py-2.5">
        <NavButton
          active={screen === "home"}
          onClick={() => go("home")}
          label={t("common.nav.home")}
          icon={
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
              <path d="M2 7 L8 2 L14 7 V14 H2 Z" />
              <path d="M6 14 V10 H10 V14" />
            </svg>
          }
        />
        <NavButton
          active={screen === "queue"}
          onClick={() => go("queue")}
          label={t("common.nav.queue")}
          icon={
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
              <path d="M3 3 H13 M3 8 H13 M3 13 H13" />
              <path d="M1.5 3 H1.6 M1.5 8 H1.6 M1.5 13 H1.6" strokeWidth="2" />
            </svg>
          }
          trailing={
            queueCount > 0 ? (
              <span className="ml-auto font-mono text-[10px] text-text3">{queueCount}</span>
            ) : undefined
          }
        />
        <NavButton
          active={screen === "history"}
          onClick={() => go("history")}
          label={t("common.nav.history")}
          icon={
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
              <circle cx="8" cy="8" r="6" />
              <path d="M8 4 V8 L11 10" />
            </svg>
          }
          trailing={
            sessionCount > 0 ? (
              <span className="ml-auto font-mono text-[10px] text-text3">{sessionCount}</span>
            ) : undefined
          }
        />
        <NavButton
          active={screen === "glossary"}
          onClick={() => go("glossary")}
          label={t("common.nav.glossary")}
          icon={
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
              <path d="M3 2 H12 A1 1 0 0 1 13 3 V13 A1 1 0 0 1 12 14 H3 A1 1 0 0 1 2 13 V3 A1 1 0 0 1 3 2 Z" />
              <path d="M5 5 H10 M5 8 H10 M5 11 H8" />
            </svg>
          }
        />
        <NavButton
          active={false}
          onClick={() => void moru.openExternal(WEB_URL)}
          label={t("common.nav.community")}
          icon={
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
              <circle cx="5" cy="6" r="2" />
              <circle cx="11" cy="6" r="2" />
              <path d="M2 13 A3 3 0 0 1 5 10 A3 3 0 0 1 8 13 M8 13 A3 3 0 0 1 11 10 A3 3 0 0 1 14 13" />
            </svg>
          }
          trailing={
            <svg
              width="10"
              height="10"
              viewBox="0 0 10 10"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.5"
              className="ml-auto opacity-50"
            >
              <path d="M3 1 H9 V7 M9 1 L1 9" />
            </svg>
          }
        />
      </div>

      <div className="flex-1" />

      {/* Update notice */}
      {updateVisible && (
        <div className="border-t border-line px-3.5 py-2.5">
          <div className="relative overflow-hidden border border-line bg-hover p-2.5">
            <div
              className="absolute top-0 right-0 h-6 w-6 opacity-30"
              style={{
                backgroundImage: "radial-gradient(circle at 2px 2px, #3DDC84 1px, transparent 1px)",
                backgroundSize: "4px 4px",
              }}
            />
            <div className="mb-1 flex items-center gap-1.5 text-[11px] text-text3">
              <div className="h-1.5 w-1.5 animate-pxpulse bg-accent" />
              <span>{t("common.update.available")}</span>
            </div>
            <div className="mb-1.5 text-xs font-semibold text-text">
              moru v{update.status === "available" || update.status === "downloading" || update.status === "ready" ? update.version : ""}
            </div>
            {update.status === "downloading" ? (
              <div className="text-[11px] font-semibold text-accent">
                {t("common.update.downloading", { percent: Math.round(update.percent) })}
              </div>
            ) : (
              <button
                onClick={() => moru.updates.install()}
                disabled={update.status !== "ready"}
                className="text-[11px] font-semibold text-accent disabled:opacity-50"
              >
                {t("common.update.installNow")}
              </button>
            )}
          </div>
        </div>
      )}

      {/* Settings + account */}
      <div className="flex flex-col gap-0.5 border-t border-line p-2">
        <NavButton
          active={screen === "settings"}
          onClick={() => go("settings")}
          label={t("common.nav.settings")}
          icon={
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
              <circle cx="8" cy="8" r="2.5" />
              <path d="M8 1 V3 M8 13 V15 M1 8 H3 M13 8 H15 M2.5 2.5 L4 4 M12 12 L13.5 13.5 M2.5 13.5 L4 12 M12 4 L13.5 2.5" />
            </svg>
          }
        />
        <div className="mt-1 flex items-center gap-2.5 border border-line bg-hover px-3 py-2">
          <div
            className="flex h-6 w-6 items-center justify-center text-[11px] font-bold text-bar"
            style={{ background: "linear-gradient(135deg, #3DDC84, #1F8A5B)" }}
          >
            {account.status === "connected" ? (account.name ?? "").slice(0, 2).toUpperCase() || "?" : "?"}
          </div>
          <div className="min-w-0 flex-1">
            <div className="overflow-hidden text-xs font-semibold text-ellipsis whitespace-nowrap text-text">
              {account.status === "connected" ? account.name : t("common.account.guest")}
            </div>
            <div className="flex items-center gap-1 text-[10px] text-text3">
              <div className="h-1 w-1 bg-discord" />
              {account.status === "connected"
                ? t("common.account.connected")
                : t("common.account.notConnected")}
            </div>
          </div>
        </div>
      </div>
      {resetDialog}
    </nav>
  );
}
