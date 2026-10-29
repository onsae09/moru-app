/** Custom titlebar for the frameless window. */

import { useTranslation } from "react-i18next";

import { moru } from "@/lib/bridge";
import { MoruLogo } from "@/components/MoruLogo";

export function Titlebar() {
  const { t } = useTranslation();
  return (
    <div
      className="flex h-11 shrink-0 select-none items-center border-b border-line bg-bar px-3"
      style={{ WebkitAppRegion: "drag" } as React.CSSProperties}
    >
      <div className="flex items-center gap-2.5">
        <MoruLogo />
        <div className="text-sm font-bold tracking-[-0.01em] text-text">moru</div>
        <div className="h-3.5 w-px bg-edge" />
        <div className="text-xs font-medium text-text3">{t("common.appTagline")}</div>
      </div>

      {/* center spacer with subtle dot pattern */}
      <div
        className="h-full flex-1"
        style={{
          backgroundImage: "radial-gradient(circle at 2px 2px, #1A231F 1px, transparent 1px)",
          backgroundSize: "8px 8px",
          backgroundPosition: "center",
        }}
      />

      <div
        className="flex gap-0.5"
        style={{ WebkitAppRegion: "no-drag" } as React.CSSProperties}
      >
        <button
          aria-label={t("common.window.minimize")}
          onClick={() => moru.win.minimize()}
          className="flex h-7 w-8 items-center justify-center text-text3 hover:bg-raised hover:text-text"
        >
          <svg width="10" height="10" viewBox="0 0 10 10" shapeRendering="crispEdges">
            <rect x="0" y="5" width="10" height="1" fill="currentColor" />
          </svg>
        </button>
        <button
          aria-label={t("common.window.maximize")}
          onClick={() => moru.win.toggleMaximize()}
          className="flex h-7 w-8 items-center justify-center text-text3 hover:bg-raised hover:text-text"
        >
          <svg
            width="10"
            height="10"
            viewBox="0 0 10 10"
            shapeRendering="crispEdges"
            fill="none"
            stroke="currentColor"
          >
            <rect x="0.5" y="0.5" width="9" height="9" />
          </svg>
        </button>
        <button
          aria-label={t("common.window.close")}
          onClick={() => moru.win.close()}
          className="flex h-7 w-8 items-center justify-center text-text3 hover:bg-[#E33] hover:text-white"
        >
          <svg width="10" height="10" viewBox="0 0 10 10" shapeRendering="crispEdges">
            <path d="M0 0 L10 10 M10 0 L0 10" stroke="currentColor" strokeWidth="1" />
          </svg>
        </button>
      </div>
    </div>
  );
}
