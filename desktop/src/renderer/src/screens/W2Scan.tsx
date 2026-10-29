/**
 * W2 - scan results. Category tree with include/exclude checkboxes on the
 * left, file list + source-sample preview on the right, live cost estimate
 * on top. Three states off wizard.scanState: running / failed / done.
 */

import { useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { useQuery } from "@tanstack/react-query";

import type { HardcodedMod, ScanCategory, ScanFile } from "../../../shared/engine";
import { ChevronIcon } from "@/components/icons";
import { WIZARD_PAGE_CLASS } from "@/components/wizardPage";
import { pressable } from "@/components/a11y";
import { api, type TranslationMatch } from "@/lib/api";
import { moru } from "@/lib/bridge";
import { WEB_URL } from "@/lib/web";
import { formatCompact, formatInt, formatUsd } from "@/lib/format";
import { RECOMMENDED_MODEL, estimateUsage, modelDisplayName } from "@/lib/models";
import { costUsd, estimatePriceForModel, usePricingTable } from "@/lib/pricing";
import { useRouter } from "@/stores/router";
import { DEFAULT_MOD_BLACKLIST, useSettings } from "@/stores/settings";
import { selectedScanTotals, useWizard } from "@/stores/wizard";

/* ---- screen-local helpers ---- */

const DOT_PATTERN = {
  backgroundImage: "radial-gradient(circle at 2px 2px, var(--color-accent) 1px, transparent 1px)",
  backgroundSize: "4px 4px",
} as const;

const DASH_LINE = {
  backgroundImage: "linear-gradient(90deg, var(--color-edge) 50%, transparent 50%)",
  backgroundSize: "6px 1px",
} as const;

function formatSize(chars: number): string {
  if (chars < 1024) return `${chars} B`;
  if (chars < 1024 * 1024) return `${(chars / 1024).toFixed(1)} KB`;
  return `${(chars / (1024 * 1024)).toFixed(1)} MB`;
}

/** Category icon by handler/name keyword. */
function CategoryIcon({ category, size = 14 }: { category: ScanCategory; size?: number }) {
  const hint = `${category.handler} ${category.name}`.toLowerCase();
  let color = "#6A7C74";
  let body = (
    <>
      <rect x="2" y="4" width="10" height="6" />
      <path d="M4 7 H10" />
    </>
  );
  if (/quest|ftb/.test(hint)) {
    color = "#3DDC84";
    body = (
      <>
        <path d="M2 2 H12 V11 L10 12 L7 11 L4 12 L2 11 Z" />
        <path d="M5 5 H9 M5 8 H8" />
      </>
    );
  } else if (/patchouli|book|guide/.test(hint)) {
    color = "#A78BFA";
    body = (
      <>
        <rect x="2" y="2" width="10" height="10" />
        <path d="M5 5 H9 M5 7 H9 M5 9 H7" />
      </>
    );
  } else if (/lang/.test(hint)) {
    color = "#6BB3F5";
    body = (
      <>
        <path d="M2 12 L5 3 L8 12 M3 9 H7" />
        <path d="M9 4 H12 M10.5 4 V12" />
      </>
    );
  } else if (/kubejs|script|js/.test(hint)) {
    color = "#F5B454";
    body = (
      <>
        <path d="M2 2 L4 7 L2 12 M12 2 L10 7 L12 12" />
        <path d="M6 4 L8 10" />
      </>
    );
  }
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 14 14"
      fill="none"
      stroke={color}
      strokeWidth="1.5"
      className="shrink-0"
    >
      {body}
    </svg>
  );
}

function CheckBox({
  checked,
  indeterminate = false,
  size = 14,
}: {
  checked: boolean;
  indeterminate?: boolean;
  size?: number;
}) {
  if (!checked && !indeterminate) {
    return (
      <div
        className="shrink-0 border border-edge bg-bar"
        style={{ width: size, height: size }}
      />
    );
  }
  return (
    <div
      className="flex shrink-0 items-center justify-center bg-accent"
      style={{ width: size, height: size }}
    >
      <svg
        width={size - 4}
        height={size - 4}
        viewBox="0 0 10 10"
        fill="none"
        stroke="#0A100D"
        strokeWidth="2"
      >
        {checked ? <path d="M1 5 L4 8 L9 2" /> : <path d="M2 5 L8 5" />}
      </svg>
    </div>
  );
}

/** 22px mono stat value; trailing M/K (or a custom suffix) drops to 14px text3. */
function StatValue({
  text,
  suffix,
  className = "text-text",
}: {
  text: string;
  suffix?: string;
  className?: string;
}) {
  const match = suffix === undefined ? /^(.*?)([MK])$/.exec(text) : null;
  const body = match !== null ? match[1] : text;
  const tail = match !== null ? match[2] : suffix;
  return (
    <div className={`font-mono text-[22px] font-bold tracking-[-0.02em] ${className}`}>
      {body}
      {tail !== undefined && <span className="text-[14px] text-text3">{tail}</span>}
    </div>
  );
}

function StepHeader({ label }: { label: string }) {
  return (
    <div className="mb-2 flex items-center gap-2.5 font-mono text-[11px] font-semibold tracking-[0.08em] text-text3 uppercase">
      <span className="text-accent">02</span>
      <span>{label}</span>
      <div className="h-px flex-1" style={DASH_LINE} />
    </div>
  );
}

const FILE_ROW_LIMIT = 150;

/* ---- states ---- */

function ScanningState() {
  const { t } = useTranslation();
  const scanProgress = useWizard((s) => s.scanProgress);
  const pct =
    scanProgress.total > 0
      ? Math.min(100, Math.round((scanProgress.current / scanProgress.total) * 100))
      : 0;

  return (
    <div className={`${WIZARD_PAGE_CLASS}`}>
      <StepHeader label={t("w2.stepLabel")} />
      <h1 className="m-0 mb-1.5 text-[26px] font-bold tracking-[-0.02em] text-text">
        {t("w2.scanning.title")}
      </h1>
      <p className="m-0 mb-6 text-[13px] text-text2">
        {scanProgress.message !== "" ? scanProgress.message : t("w2.scanning.hint")}
      </p>

      <div className="mb-2 h-[10px] overflow-hidden border border-line2 bg-bar">
        {scanProgress.total > 0 ? (
          <div
            className="h-full bg-accent transition-[width] duration-200"
            style={{ width: `${pct}%` }}
          />
        ) : (
          <div className="h-full w-1/3 animate-bar-crawl bg-accent" />
        )}
      </div>
      <div className="mb-6 flex items-center justify-between font-mono text-[11px] text-text3">
        <span className="animate-pxblink text-accent">▍</span>
        <span>
          {formatInt(scanProgress.current)} / {scanProgress.total > 0 ? formatInt(scanProgress.total) : "—"}
        </span>
      </div>

      <div className="mb-6 grid grid-cols-5 gap-2 border border-line2 bg-raised p-4">
        {Array.from({ length: 5 }, (_, i) => (
          <div key={i}>
            <div className="mb-2 h-[10px] w-12 animate-pxpulse bg-hover" />
            <div className="h-[22px] w-16 animate-pxpulse bg-hover" />
          </div>
        ))}
      </div>
      <div className="grid grid-cols-[340px_1fr] gap-3">
        <div className="flex flex-col gap-2 border border-line2 bg-raised p-3.5">
          {Array.from({ length: 6 }, (_, i) => (
            <div key={i} className="h-[34px] animate-pxpulse bg-hover" />
          ))}
        </div>
        <div className="flex flex-col gap-2 border border-line2 bg-raised p-3.5">
          <div className="h-[34px] animate-pxpulse bg-hover" />
          <div className="h-[180px] animate-pxpulse bg-hover" />
          <div className="h-[34px] animate-pxpulse bg-hover" />
        </div>
      </div>
    </div>
  );
}

function FailedState() {
  const { t } = useTranslation();
  const scanError = useWizard((s) => s.scanError);
  const startScan = useWizard((s) => s.startScan);
  const go = useRouter((s) => s.go);

  return (
    <div className={`${WIZARD_PAGE_CLASS}`}>
      <StepHeader label={t("w2.stepLabel")} />
      <div className="mx-auto mt-16 max-w-[520px] border border-red/40 bg-card p-8 text-center">
        <div className="mx-auto mb-4 flex h-10 w-10 items-center justify-center bg-red/12">
          <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="#F26B6B" strokeWidth="2">
            <path d="M3 3 L13 13 M13 3 L3 13" />
          </svg>
        </div>
        <div className="mb-2 text-[15px] font-bold text-text">{t("w2.failed.title")}</div>
        {scanError !== null && (
          <div className="mb-5 border border-line bg-bar p-3 text-left font-mono text-[11px] leading-[1.6] break-all text-text2">
            {scanError}
          </div>
        )}
        <div className="flex items-center justify-center gap-2">
          <button
            onClick={() => go("w1")}
            className="flex items-center gap-1.5 px-[18px] py-2.5 text-[13px] font-semibold text-text2 hover:text-text"
          >
            <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.5">
              <path d="M8 2 L4 6 L8 10" />
            </svg>
            {t("w2.footer.back")}
          </button>
          <button
            onClick={() => void startScan()}
            className="px-5 py-2.5 bg-accent text-[13px] font-bold text-sel-ink hover:bg-accent-hi"
          >
            {t("w2.failed.retry")}
          </button>
        </div>
      </div>
    </div>
  );
}

function EmptyState() {
  const { t } = useTranslation();
  const startScan = useWizard((s) => s.startScan);
  const go = useRouter((s) => s.go);

  return (
    <div className="mx-auto mt-16 max-w-[520px] border border-line2 bg-card p-8 text-center">
      <div className="mx-auto mb-4 h-10 w-10 opacity-40" style={DOT_PATTERN} />
      <div className="mb-2 text-[15px] font-bold text-text">{t("w2.empty.title")}</div>
      <p className="m-0 mb-5 text-[12px] leading-[1.6] text-text2">{t("w2.empty.desc")}</p>
      <div className="flex items-center justify-center gap-2">
        <button
          onClick={() => go("w1")}
          className="flex items-center gap-1.5 px-[18px] py-2.5 text-[13px] font-semibold text-text2 hover:text-text"
        >
          <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.5">
            <path d="M8 2 L4 6 L8 10" />
          </svg>
          {t("w2.footer.back")}
        </button>
        <button
          onClick={() => void startScan()}
          className="px-5 py-2.5 bg-accent text-[13px] font-bold text-sel-ink hover:bg-accent-hi"
        >
          {t("w2.failed.retry")}
        </button>
      </div>
    </div>
  );
}

/* ---- mod blacklist / source overrides ---- */

/** Comparison form of a mod id — mirrors the engine's `normalize_mod_id`,
 * so the id the scan reports maps back onto the list entry the user typed
 * ("Entity Culling" -> "entityculling"). */
function normalizeModId(text: string): string {
  return text.trim().toLowerCase().replace(/[^a-z0-9]+/g, "");
}

function FilterBand({
  header,
  summary,
  expandable = false,
  expanded = false,
  onToggle,
  children,
}: {
  header: string;
  summary: string;
  expandable?: boolean;
  expanded?: boolean;
  onToggle?: () => void;
  children?: ReactNode;
}) {
  return (
    <div className="-mt-4 mb-6 border border-line2 bg-raised">
      <div
        {...pressable(expandable ? onToggle : undefined)}
        aria-expanded={expandable ? expanded : undefined}
        className={`flex items-center gap-2 px-3.5 py-2 font-mono text-[11px] text-text2 ${
          expandable ? "cursor-pointer hover:bg-raised-hover" : ""
        }`}
      >
        <span className="font-semibold tracking-[0.06em] text-text3 uppercase">
          {header}
        </span>
        <div className="h-px flex-1" style={DASH_LINE} />
        <span>{summary}</span>
        {expandable && (
          <svg
            width="10"
            height="10"
            viewBox="0 0 10 10"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.5"
            className={expanded ? "rotate-180" : undefined}
          >
            <path d="M1 3 L5 7 L9 3" />
          </svg>
        )}
      </div>
      {expanded && children !== undefined && (
        <div className="border-t border-line2 px-3.5 py-3">{children}</div>
      )}
    </div>
  );
}

/**
 * Mod blacklist band: what this scan dropped, what else is on the list,
 * and the controls to change it. Editing the list restarts the scan,
 * because the engine decides which JARs to even open from it.
 */
function BlacklistBand() {
  const { t } = useTranslation();
  const blacklist = useSettings((s) => s.modBlacklist);
  const setSettings = useSettings((s) => s.set);
  const scanResult = useWizard((s) => s.scanResult);
  const scanState = useWizard((s) => s.scanState);
  const startScan = useWizard((s) => s.startScan);
  const [expanded, setExpanded] = useState(false);
  const [draft, setDraft] = useState("");

  const excluded = scanResult?.excluded_mods ?? [];
  // An entry is "listed only" when this pack ships no JAR matching it.
  const hitIds = new Set(excluded.map((mod) => normalizeModId(mod.mod_id)));
  const listedOnly = blacklist.filter((entry) => !hitIds.has(normalizeModId(entry)));

  const apply = (next: string[]): void => {
    setSettings({ modBlacklist: next });
    void startScan();
  };
  const drop = (id: string): void =>
    apply(blacklist.filter((entry) => normalizeModId(entry) !== normalizeModId(id)));
  const add = (): void => {
    const entry = draft.trim();
    if (entry === "") return;
    setDraft("");
    if (blacklist.some((e) => normalizeModId(e) === normalizeModId(entry))) return;
    apply([...blacklist, entry]);
  };

  return (
    <FilterBand
      header={t("w2.blacklist.header")}
      summary={
        scanState === "running"
          ? t("w2.blacklist.rescanning")
          : excluded.length > 0
            ? t("w2.blacklist.summary", { excluded: excluded.length })
            : t("w2.blacklist.summaryNone", { listed: blacklist.length })
      }
      expandable
      expanded={expanded}
      onToggle={() => setExpanded((prev) => !prev)}
    >
      <p className="m-0 mb-3 text-[11px] leading-relaxed text-text3">
        {t("w2.blacklist.hint")}
      </p>

      {excluded.length > 0 && (
        <>
          <div className="mb-1.5 font-mono text-[10px] font-semibold tracking-[0.06em] text-text3 uppercase">
            {t("w2.blacklist.excludedHeader")}
          </div>
          <div className="mb-3 flex flex-col gap-1">
            {excluded.map((mod) => (
              <div
                key={`${mod.mod_id}:${mod.jar_name}`}
                className="flex items-center gap-2 bg-hover px-2.5 py-1.5"
              >
                <span className="font-mono text-[11px] font-bold text-text">
                  {mod.mod_id}
                </span>
                <span className="truncate font-mono text-[10px] text-text3">
                  {mod.jar_name}
                </span>
                <button
                  onClick={() => drop(mod.mod_id)}
                  className="ml-auto shrink-0 px-1.5 font-mono text-[10px] text-accent uppercase hover:underline"
                >
                  {t("w2.blacklist.include")}
                </button>
              </div>
            ))}
          </div>
        </>
      )}

      {listedOnly.length > 0 && (
        <>
          <div className="mb-1.5 font-mono text-[10px] font-semibold tracking-[0.06em] text-text3 uppercase">
            {t("w2.blacklist.listedHeader")}
          </div>
          <div className="mb-3 flex flex-wrap gap-1.5">
            {listedOnly.map((entry) => (
              <span
                key={entry}
                className="flex items-center gap-1.5 border border-edge px-2 py-[3px] font-mono text-[10px] text-text2"
              >
                {entry}
                <button
                  onClick={() => drop(entry)}
                  aria-label={t("w2.blacklist.include")}
                  className="text-text3 hover:text-text"
                >
                  ×
                </button>
              </span>
            ))}
          </div>
        </>
      )}

      <div className="flex items-center gap-2">
        <input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") add();
          }}
          placeholder={t("w2.blacklist.addPlaceholder")}
          className="min-w-0 flex-1 border border-edge bg-bar px-2 py-1.5 font-mono text-[11px] text-text placeholder:text-text3"
        />
        <button
          onClick={add}
          className="shrink-0 border border-edge px-2.5 py-1.5 font-mono text-[10px] text-text2 uppercase hover:text-text"
        >
          {t("w2.blacklist.add")}
        </button>
        <button
          onClick={() => apply([...DEFAULT_MOD_BLACKLIST])}
          className="shrink-0 px-1.5 font-mono text-[10px] text-text3 uppercase hover:text-text2"
        >
          {t("w2.blacklist.reset")}
        </button>
      </div>
    </FilterBand>
  );
}

/** Which enabled resource packs supplied the source text, and how much. */
function SourceOverrideBand() {
  const { t } = useTranslation();
  const overrides = useWizard((s) => s.scanResult?.source_overrides) ?? [];
  const total = overrides.reduce((sum, o) => sum + o.keys, 0);
  if (total === 0) return null;

  return (
    <FilterBand
      header={t("w2.overrides.header")}
      summary={t("w2.overrides.summary", { keys: formatInt(total) })}
      expandable
      expanded
    >
      <div className="flex flex-col gap-1">
        {overrides.map((o) => (
          <div key={o.pack} className="font-mono text-[11px] text-text2">
            {t("w2.overrides.packLine", { pack: o.pack, keys: formatInt(o.keys) })}
          </div>
        ))}
      </div>
    </FilterBand>
  );
}

/**
 * What the community-translation band should show, or null to render nothing.
 *
 * Split out from the component because the honesty of this band lives in the
 * numbers, not the markup. `uncovered_entries` is a LOWER bound on the local
 * entries a published pack does not cover, so it is presented as "at least N
 * of your M" and never as a coverage percentage: only a key-level diff of the
 * downloaded pack could justify one, and a 40%-covered translation offered as
 * "compatible" is worse than offering nothing at all.
 *
 * Null when there is no match, or when the match carries nothing the user
 * could act on - a pack with neither a page to read nor an archive to fetch
 * is a notification with no next step.
 */
export function communityOffer(
  match: TranslationMatch | null | undefined,
  localEntries: number,
): {
  packId: string;
  version: string | null;
  exact: boolean;
  uncovered: number | null;
  localEntries: number;
  packEntries: number | null;
  gaps: { category: string; entries: number }[];
  url: string | null;
  downloadUrl: string | null;
  note: string;
} | null {
  if (!match) return null;
  if (match.url === null && match.download_url === null) return null;
  return {
    packId: match.pack_id,
    version: match.modpack_version,
    exact: match.exact,
    uncovered: match.uncovered_entries,
    localEntries,
    packEntries: match.total_entries,
    // Biggest gap first: "mods +412" is the line that decides whether the
    // offer is worth taking, and it must not sit under three small ones.
    gaps: Object.entries(match.uncovered_by_category)
      .map(([category, entries]) => ({ category, entries }))
      .sort((a, b) => b.entries - a.entries),
    url: match.url,
    downloadUrl: match.download_url,
    note: match.note,
  };
}

/**
 * A translation somebody already published for this modpack.
 *
 * Placed on the scan screen because this is the last moment the information
 * can change what the user does: the key counts are known, so the offer can
 * be quantified, and no model has been chosen or paid for yet.
 *
 * Reuse is deliberately harder than "accept". A downloaded pack is B -
 * target-locale files with no record of the English they were translated
 * from - so the engine can only reuse an entry whose source text it can show
 * unchanged, which needs the previous version's modpack as A. Where that
 * cannot be established the band says so and the entries get translated
 * normally, because a stale line delivered silently is worse than paying to
 * redo it.
 */
function CommunityBand() {
  const { t } = useTranslation();
  const scanJobId = useWizard((s) => s.scanJobId);
  const scanResult = useWizard((s) => s.scanResult);
  const modpackPath = useWizard((s) => s.modpackPath);
  const previousModpackPath = useWizard((s) => s.previousModpackPath);
  const setMigrationEnabled = useWizard((s) => s.setMigrationEnabled);
  const setMigrationInput = useWizard((s) => s.setMigrationInput);
  const startScan = useWizard((s) => s.startScan);
  const targetLocale = useWizard((s) => s.targetLocale);
  const [expanded, setExpanded] = useState(true);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);

  const query = useQuery({
    queryKey: ["community-translation", scanJobId, targetLocale],
    queryFn: () => api.communityTranslation(WEB_URL, scanJobId ?? "", targetLocale),
    enabled: scanJobId !== null,
    // A lookup nobody asked for: one attempt, and silence when it fails.
    retry: false,
    staleTime: 300_000,
  });

  const localEntries = (scanResult?.categories ?? []).reduce(
    (sum, c) => sum + c.entry_count,
    0,
  );
  const offer = communityOffer(query.data?.match, localEntries);
  // Already attached: the purple migration strip above reports the real,
  // measured reuse, so repeating a promise here would only compete with it.
  const attached = scanResult?.migration !== null && scanResult?.migration !== undefined;
  if (offer === null) return null;

  const reuse = async (previous: string): Promise<void> => {
    if (offer.downloadUrl === null) return;
    setBusy(true);
    setFailed(false);
    try {
      const archives = await api.downloadCommunityTranslation(
        offer.packId,
        offer.downloadUrl,
      );
      if (archives.resourcepack_path === null && archives.overrides_path === null) {
        setFailed(true);
        return;
      }
      setMigrationInput("modpack", previous);
      setMigrationInput("resourcepack", archives.resourcepack_path);
      setMigrationInput("overrides", archives.overrides_path);
      setMigrationEnabled(true);
      await startScan();
    } catch {
      // The user pressed a button, so unlike the lookup this is reported -
      // but as a line in the band, not a dialog: nothing is broken and the
      // run can still proceed without reuse.
      setFailed(true);
    } finally {
      setBusy(false);
    }
  };

  const summary = offer.exact
    ? t("w2.community.summaryExact", { version: offer.version ?? "?" })
    : t("w2.community.summaryCompatible", { version: offer.version ?? "?" });

  return (
    <FilterBand
      header={t("w2.community.header")}
      summary={summary}
      expandable
      expanded={expanded}
      onToggle={() => setExpanded((prev) => !prev)}
    >
      <p className="m-0 mb-3 text-[11px] leading-relaxed text-text3">{offer.note}</p>

      <div className="mb-3 flex flex-col gap-1 font-mono text-[11px] text-text2">
        {offer.packEntries !== null && (
          <div>{t("w2.community.packEntries", { entries: formatInt(offer.packEntries) })}</div>
        )}
        {offer.uncovered === null ? (
          <div>{t("w2.community.unmeasured")}</div>
        ) : offer.uncovered === 0 ? (
          <div>{t("w2.community.covered")}</div>
        ) : (
          <div className="text-amber">
            {t("w2.community.uncovered", {
              uncovered: formatInt(offer.uncovered),
              local: formatInt(offer.localEntries),
            })}
          </div>
        )}
      </div>

      {offer.gaps.length > 0 && (
        <>
          <div className="mb-1.5 font-mono text-[10px] font-semibold tracking-[0.06em] text-text3 uppercase">
            {t("w2.community.gapsHeader")}
          </div>
          <div className="mb-3 flex flex-wrap gap-1.5">
            {offer.gaps.map((gap) => (
              <span
                key={gap.category}
                className="border border-edge px-2 py-[3px] font-mono text-[10px] text-text2"
              >
                {gap.category === ""
                  ? t("w2.community.gapTotal", { entries: formatInt(gap.entries) })
                  : t("w2.community.gapLine", {
                      category: gap.category,
                      entries: formatInt(gap.entries),
                    })}
              </span>
            ))}
          </div>
        </>
      )}

      {attached ? (
        <p className="m-0 mb-3 text-[11px] leading-relaxed text-accent">
          {t("w2.community.attached")}
        </p>
      ) : (
        <p className="m-0 mb-3 text-[11px] leading-relaxed text-text3">
          {previousModpackPath !== null
            ? t("w2.community.reuseReady")
            : offer.exact
              ? t("w2.community.sameVersionHint")
              : t("w2.community.needsPrevious")}
        </p>
      )}

      {failed && (
        <p className="m-0 mb-3 text-[11px] leading-relaxed text-red">
          {t("w2.community.failed")}
        </p>
      )}

      <div className="flex flex-wrap items-center gap-2">
        {offer.url !== null && (
          <button
            onClick={() => void moru.openExternal(offer.url as string)}
            className="border border-edge px-2.5 py-1.5 font-mono text-[11px] text-text2 hover:text-text"
          >
            {t("w2.community.open")}
          </button>
        )}
        {!attached && offer.downloadUrl !== null && (
          <>
            {previousModpackPath !== null ? (
              <button
                disabled={busy}
                onClick={() => void reuse(previousModpackPath)}
                className="border border-accent-lo bg-tint px-2.5 py-1.5 font-mono text-[11px] text-accent hover:text-text disabled:opacity-50"
              >
                {busy ? t("w2.community.working") : t("w2.community.reuse")}
              </button>
            ) : (
              <button
                disabled={busy}
                onClick={() => {
                  void (async () => {
                    const picked = await moru.pickFolder();
                    if (picked !== null && picked !== "") await reuse(picked);
                  })();
                }}
                className="border border-edge px-2.5 py-1.5 font-mono text-[11px] text-text2 hover:text-text disabled:opacity-50"
              >
                {busy ? t("w2.community.working") : t("w2.community.pickPrevious")}
              </button>
            )}
            {/* Same-version reuse without the old modpack. Offered ONLY on an
                exact version match, never by default, and labelled with what
                it gives up: the source text is assumed unchanged rather than
                verified, so a mod updated since the pack was published keeps
                its old line. The alternative for this user is paying to
                retranslate a pack that is almost certainly identical. */}
            {!offer.exact || previousModpackPath !== null ? null : (
              <button
                disabled={busy || modpackPath === null}
                onClick={() => void reuse(modpackPath as string)}
                className="px-1.5 font-mono text-[10px] text-text3 uppercase hover:text-text2 disabled:opacity-50"
              >
                {t("w2.community.sameVersionReuse")}
              </button>
            )}
          </>
        )}
      </div>
    </FilterBand>
  );
}

/** CSV of every hardcoded string, for whoever builds the Mixin or
 * resource-pack workaround the language file cannot provide. Built in the
 * renderer because the data is already here — no round trip to the engine. */
function hardcodedCsv(mods: HardcodedMod[]): string {
  const cell = (value: string): string => `"${value.replace(/"/g, '""')}"`;
  const rows = [["mod_id", "jar", "class", "kind", "source", "translation"]];
  for (const mod of mods) {
    for (const s of mod.strings) {
      rows.push([mod.mod_id, mod.jar_name, s.class_name, s.kind, s.text, ""]);
    }
  }
  // BOM: Excel reads a bare UTF-8 CSV as the local codepage and mangles
  // every non-ASCII source string.
  return "\uFEFF" + rows.map((r) => r.map(cell).join(",")).join("\r\n");
}

function downloadCsv(filename: string, body: string): void {
  const url = URL.createObjectURL(new Blob([body], { type: "text/csv;charset=utf-8" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}

/**
 * Mods whose display text is compiled into Java, where no language file
 * reaches it. This band exists to end a specific wrong conclusion: the
 * user sees English in game, assumes the translation failed, and goes
 * looking for a bug on our side. Naming the mod, the count and the exact
 * strings makes the cause attributable and the hunt unnecessary.
 */
function HardcodedBand() {
  const { t } = useTranslation();
  const mods = useWizard((s) => s.scanResult?.hardcoded_mods) ?? [];
  const [expanded, setExpanded] = useState(false);
  const [openMod, setOpenMod] = useState<string | null>(null);
  if (mods.length === 0) return null;

  const strings = mods.reduce((sum, m) => sum + m.strings.length, 0);

  return (
    <FilterBand
      header={t("w2.hardcoded.header")}
      summary={t("w2.hardcoded.summary", {
        mods: formatInt(mods.length),
        strings: formatInt(strings),
      })}
      expandable
      expanded={expanded}
      onToggle={() => setExpanded((prev) => !prev)}
    >
      <p className="m-0 mb-3 text-[11px] leading-relaxed text-text3">
        {t("w2.hardcoded.hint")}
      </p>

      <div className="mb-3 flex flex-col gap-1">
        {mods.map((mod) => {
          const open = openMod === mod.jar_name;
          return (
            <div key={mod.jar_name} className="border border-line2 bg-bar">
              <button
                onClick={() => setOpenMod(open ? null : mod.jar_name)}
                className="flex w-full items-center gap-2 px-2.5 py-1.5 text-left font-mono text-[11px] text-text2 hover:bg-raised-hover"
              >
                <span className="font-semibold text-text">{mod.mod_id}</span>
                <span className="truncate text-text3">{mod.jar_name}</span>
                <div className="h-px flex-1" style={DASH_LINE} />
                <span className="shrink-0">
                  {t("w2.hardcoded.stringCount", {
                    count: mod.strings.length,
                  })}
                </span>
              </button>
              {open && (
                <div className="flex flex-col gap-1.5 border-t border-line2 px-2.5 py-2">
                  {mod.strings.map((s) => (
                    <div key={`${s.class_name}:${s.text}`}>
                      <div className="font-mono text-[10px] text-text3">
                        {s.class_name.split("/").pop()}
                        {s.kind === "associated" && ` · ${t("w2.hardcoded.associated")}`}
                      </div>
                      <div className="font-mono text-[11px] whitespace-pre-wrap text-text2">
                        {s.text}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          );
        })}
      </div>

      <button
        onClick={() => downloadCsv("moru-hardcoded-strings.csv", hardcodedCsv(mods))}
        className="border border-edge px-2.5 py-1.5 font-mono text-[11px] text-text2 hover:text-text"
      >
        {t("w2.hardcoded.exportCsv")}
      </button>
    </FilterBand>
  );
}

/* ---- done state ---- */

/** Handler-level grouping: every "Mod: …" category folds into one mods
 * group; anything else groups by its category name (usually 1:1, but e.g.
 * "Resource/Data Packs" can span two handlers). */
interface CategoryGroup {
  key: string;
  /** null -> mods group; the label comes from i18n */
  label: string | null;
  categories: ScanCategory[];
}

function groupCategories(categories: ScanCategory[]): CategoryGroup[] {
  const groups: CategoryGroup[] = [];
  const byKey = new Map<string, CategoryGroup>();
  for (const cat of categories) {
    const key = cat.handler === "mod" ? "mod" : `cat:${cat.name}`;
    let group = byKey.get(key);
    if (group === undefined) {
      group = {
        key,
        label: cat.handler === "mod" ? null : cat.name,
        categories: [],
      };
      byKey.set(key, group);
      groups.push(group);
    }
    group.categories.push(cat);
  }
  return groups;
}

function GroupRow({
  label,
  group,
  includedCount,
  collapsed,
  onToggle,
  onCollapse,
}: {
  label: string;
  group: CategoryGroup;
  includedCount: number;
  collapsed: boolean;
  onToggle: (included: boolean) => void;
  onCollapse: () => void;
}) {
  const allIncluded = includedCount === group.categories.length;
  const entries = group.categories.reduce((sum, c) => sum + c.entry_count, 0);
  return (
    <div
      {...pressable(onCollapse)}
      aria-expanded={!collapsed}
      className="flex cursor-pointer items-center gap-2.5 border-y border-line2 bg-hover px-3.5 py-2 hover:bg-raised-hover"
    >
      <button
        type="button"
        onClick={(e) => {
          e.stopPropagation();
          onToggle(!allIncluded);
        }}
        aria-checked={allIncluded ? "true" : includedCount > 0 ? "mixed" : "false"}
        aria-label={label}
        role="checkbox"
        className="flex shrink-0 items-center"
      >
        <CheckBox checked={allIncluded} indeterminate={includedCount > 0} />
      </button>
      <ChevronIcon open={!collapsed} className="text-text4" />
      <span className="truncate font-mono text-[11px] font-semibold tracking-[0.06em] text-text2 uppercase">
        {label}
      </span>
      <span className="shrink-0 font-mono text-[10px] text-text3">
        {includedCount}/{group.categories.length}
      </span>
      <span className="ml-auto shrink-0 font-mono text-[11px] text-text2">
        {formatInt(entries)}
      </span>
    </div>
  );
}

function CategoryRow({
  category,
  checked,
  expanded,
  onExpand,
  onToggle,
  onPickFile,
  selectedFilePath,
}: {
  category: ScanCategory;
  checked: boolean;
  expanded: boolean;
  onExpand: () => void;
  onToggle: (included: boolean) => void;
  onPickFile: (path: string) => void;
  selectedFilePath: string | null;
}) {
  const { t } = useTranslation();
  const subFiles = category.files.slice(0, 3);
  const moreFiles = category.files.length - subFiles.length;

  return (
    <>
      <div
        {...pressable(onExpand)}
        aria-expanded={expanded}
        className={`flex cursor-pointer items-center gap-2.5 px-3.5 py-2.5 ${
          expanded
            ? "border-l-[3px] border-l-accent bg-accent/6"
            : checked
              ? "hover:bg-raised-hover"
              : "opacity-60 hover:bg-raised-hover hover:opacity-100"
        }`}
      >
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            onToggle(!checked);
          }}
          aria-checked={checked}
          aria-label={category.name}
          role="checkbox"
          className="flex shrink-0 items-center"
        >
          <CheckBox checked={checked} />
        </button>
        {expanded ? (
          <ChevronIcon className="text-text4" />
        ) : (
          <div className="w-[10px] shrink-0" />
        )}
        <CategoryIcon category={category} />
        <span
          className={`truncate text-[13px] ${
            expanded ? "font-bold text-text" : checked ? "font-semibold text-text" : "font-medium text-text3"
          }`}
          title={`${category.name} · ${category.handler}`}
        >
          {category.name}
        </span>
        <span className="shrink-0 font-mono text-[10px] text-text3">{category.handler}</span>
        <span
          className={`ml-auto shrink-0 font-mono text-[11px] ${
            expanded ? "text-accent" : checked ? "text-text2" : "text-text4"
          }`}
        >
          {formatInt(category.entry_count)}
        </span>
      </div>
      {expanded && (
        <div className="flex flex-col gap-0.5 py-1 pr-3.5 pl-11">
          {subFiles.map((f) => (
            <div
              key={f.path}
              {...pressable(() => onPickFile(f.path))}
              className={`flex cursor-pointer items-center gap-2 py-1 ${
                selectedFilePath === f.path ? "" : "hover:bg-raised-hover"
              }`}
            >
              <div className="flex h-3 w-3 shrink-0 items-center justify-center bg-accent">
                <svg width="8" height="8" viewBox="0 0 8 8" fill="none" stroke="#0A100D" strokeWidth="2">
                  <path d="M1 4 L3 6 L7 2" />
                </svg>
              </div>
              <span
                className={`truncate text-[12px] ${selectedFilePath === f.path ? "text-text" : "text-text2"}`}
                title={f.path}
              >
                {f.path.split("/").at(-1)}
              </span>
              <span className="ml-auto shrink-0 font-mono text-[10px] text-text3">
                {formatInt(f.entry_count)}
              </span>
            </div>
          ))}
          {moreFiles > 0 && (
            <div className="py-1 font-mono text-[10px] text-text3">
              {t("w2.tree.moreFiles", { count: moreFiles })}
            </div>
          )}
        </div>
      )}
    </>
  );
}

function FileRows({
  category,
  selectedFile,
  onPickFile,
}: {
  category: ScanCategory;
  selectedFile: ScanFile | null;
  onPickFile: (path: string) => void;
}) {
  const { t } = useTranslation();
  const visible = category.files.slice(0, FILE_ROW_LIMIT);
  const more = category.files.length - visible.length;

  if (category.files.length === 0) {
    return (
      <div className="flex items-center justify-center px-3.5 py-8 font-mono text-[11px] text-text3">
        {t("w2.preview.noFiles")}
      </div>
    );
  }

  return (
    <>
      {visible.map((f) => {
        const isSelected = selectedFile !== null && f.path === selectedFile.path;
        const meta = t("w2.preview.fileMeta", {
          entries: formatInt(f.entry_count),
          size: formatSize(f.char_count),
        });
        if (isSelected) {
          const sampleRows = Object.entries(f.sample);
          return (
            <div key={f.path}>
              <div className="flex items-center border-b border-line bg-[#1A2420] px-3.5 py-2">
                <span className="truncate font-mono text-[12px] font-semibold text-text" title={f.path}>
                  {f.path.split("/").at(-1)}
                </span>
                <span className="ml-auto shrink-0 font-mono text-[10px] text-text3">{meta}</span>
              </div>
              <div className="bg-card px-3.5 py-3 font-mono text-[12px] leading-[1.7] text-text2">
                {sampleRows.length === 0 ? (
                  <div className="text-[11px] text-text3">{t("w2.preview.emptySample")}</div>
                ) : (
                  sampleRows.map(([key, source]) => (
                    <div key={key} className="flex gap-3 mb-1.5 last:mb-0">
                      <div className="w-[200px] shrink-0 truncate text-text3" title={key}>
                        {key}
                      </div>
                      <div className="min-w-0 flex-1 text-text">&quot;{source}&quot;</div>
                    </div>
                  ))
                )}
              </div>
            </div>
          );
        }
        return (
          <div
            key={f.path}
            {...pressable(() => onPickFile(f.path))}
            className="flex cursor-pointer items-center border-b border-line px-3.5 py-2 hover:bg-raised-hover"
          >
            <span className="truncate font-mono text-[12px] text-text2" title={f.path}>
              {f.path.split("/").at(-1)}
            </span>
            <span className="ml-auto shrink-0 font-mono text-[10px] text-text3">{meta}</span>
          </div>
        );
      })}
      {more > 0 && (
        <div className="flex items-center justify-center px-3.5 py-2 font-mono text-[11px] text-text3">
          {t("w2.preview.moreFiles", { count: more })}
        </div>
      )}
    </>
  );
}

function DoneState() {
  const { t } = useTranslation();
  const go = useRouter((s) => s.go);
  const model = useSettings((s) => s.model);
  const batchSize = useSettings((s) => s.batchSize);
  const maxRefine = useSettings((s) => s.maxRefine);
  const thinkingEnabled = useSettings((s) => s.thinkingEnabled);
  const useVanillaGlossary = useSettings((s) => s.useVanillaGlossary);
  const extractGlossary = useSettings((s) => s.extractGlossary);
  const glossaryMaxTerms = useSettings((s) => s.glossaryMaxTerms);
  const scanResult = useWizard((s) => s.scanResult);
  const excludedCategories = useWizard((s) => s.excludedCategories);
  const toggleCategory = useWizard((s) => s.toggleCategory);
  const setCategories = useWizard((s) => s.setCategories);

  const categories = scanResult?.categories ?? [];
  const groups = groupCategories(categories);
  const [openCategory, setOpenCategory] = useState<string | null>(null);
  const [pickedFile, setPickedFile] = useState<string | null>(null);
  const [collapsedGroups, setCollapsedGroups] = useState<ReadonlySet<string>>(
    new Set(),
  );

  const activeCat =
    categories.find((c) => c.name === openCategory) ?? categories[0] ?? null;
  const activeFile =
    activeCat === null
      ? null
      : (activeCat.files.find((f) => f.path === pickedFile) ?? activeCat.files[0] ?? null);

  const totals = selectedScanTotals({ scanResult, excludedCategories });
  const usage = estimateUsage({
    chars: totals.translationChars,
    entries: totals.translationEntries,
    glossaryEntries: totals.entries,
    batchSize,
    maxRefine,
    glossary: useVanillaGlossary,
    extractGlossary,
    glossaryMaxTerms,
    thinking: thinkingEnabled,
  });
  const pricingTable = usePricingTable();
  const price = estimatePriceForModel(pricingTable, model);
  const estCostUsd = price !== null ? costUsd(usage, price) : null;
  const costText = estCostUsd !== null ? formatUsd(estCostUsd) : "—";
  const recommendedPrice = estimatePriceForModel(pricingTable, RECOMMENDED_MODEL);
  const recommendedCostUsd =
    model !== RECOMMENDED_MODEL && recommendedPrice !== null && usage.totalTokens > 0
      ? costUsd(usage, recommendedPrice)
      : null;
  const selectedCount = categories.filter((c) => !excludedCategories.includes(c.name)).length;
  const allSelected = selectedCount === categories.length;

  const tmQuery = useQuery({
    queryKey: ["tm-stats"],
    queryFn: () => api.tmStats(),
    retry: false,
    staleTime: 60_000,
  });

  const breadcrumb =
    activeFile !== null && activeFile.path.includes("/")
      ? activeFile.path.split("/").slice(0, -1).join(" / ")
      : (activeCat?.handler ?? "");

  if (categories.length === 0) {
    // The blacklist band renders here too: a list broad enough to empty the
    // scan is exactly when the user needs to see it and put a mod back.
    return (
      <div className={`${WIZARD_PAGE_CLASS}`}>
        <StepHeader label={t("w2.stepLabel")} />
        <div className="mt-4">
          <HardcodedBand />
          <BlacklistBand />
        </div>
        <EmptyState />
      </div>
    );
  }

  return (
    <div className={`${WIZARD_PAGE_CLASS}`}>
      <StepHeader label={t("w2.stepLabel")} />
      <h1 className="m-0 mb-1.5 text-[26px] font-bold tracking-[-0.02em] text-text">
        {t("w2.title")}
      </h1>
      <p className="m-0 mb-6 text-[13px] text-text2">{t("w2.subtitle")}</p>

      {/* Top summary stats */}
      <div
        className="relative mb-6 grid grid-cols-5 gap-2 border border-accent-lo p-4"
        style={{ background: "linear-gradient(135deg, var(--color-tint) 0%, var(--color-raised) 100%)" }}
      >
        <div className="absolute top-2 right-2 h-8 w-8 opacity-30" style={DOT_PATTERN} />
        <div>
          <div className="mb-1 font-mono text-[10px] font-semibold tracking-[0.06em] text-text3 uppercase">
            {t("w2.stat.files")}
          </div>
          <StatValue text={formatInt(totals.files)} />
        </div>
        <div>
          <div className="mb-1 font-mono text-[10px] font-semibold tracking-[0.06em] text-text3 uppercase">
            {t("w2.stat.entries")}
          </div>
          <StatValue text={formatInt(totals.entries)} />
        </div>
        <div>
          <div className="mb-1 font-mono text-[10px] font-semibold tracking-[0.06em] text-text3 uppercase">
            {t("w2.stat.tokens")}
          </div>
          <StatValue text={formatCompact(usage.totalTokens)} />
        </div>
        <div>
          <div className="mb-1 font-mono text-[10px] font-semibold tracking-[0.06em] text-text3 uppercase">
            {t("w2.stat.cost")}
          </div>
          <StatValue text={costText} className="text-accent" />
        </div>
        <div>
          <div className="mb-1 flex items-center gap-1 font-mono text-[10px] font-semibold tracking-[0.06em] text-text3 uppercase">
            <div className="h-[5px] w-[5px] bg-purple" />
            {t("w2.stat.tm")}
          </div>
          {tmQuery.data !== undefined ? (
            <StatValue
              text={formatCompact(tmQuery.data.entries)}
              suffix={t("w2.stat.tmSuffix")}
              className="text-purple"
            />
          ) : (
            <StatValue text="—" className="text-text3" />
          )}
        </div>
      </div>

      {scanResult?.migration !== null && scanResult?.migration !== undefined && (
        <div className="-mt-4 mb-6 flex items-center justify-between gap-4 border border-purple/40 bg-purple/6 px-3.5 py-2.5">
          <div className="min-w-0">
            <div className="mb-0.5 flex items-center gap-2 text-[12px] font-bold text-purple">
              <span className="h-1.5 w-1.5 bg-purple" />
              {t("w2.migration.title")}
            </div>
            <div className="text-[11px] text-text2">
              {totals.migrationEntries > 0
                ? t("w2.migration.summary", {
                    reused: formatInt(totals.migrationEntries),
                    translate: formatInt(totals.translationEntries),
                  })
                : t("w2.migration.zero")}
            </div>
          </div>
          <div className="flex shrink-0 gap-5 text-right font-mono">
            <div>
              <div className="text-[10px] text-text3 uppercase">{t("w2.migration.reused")}</div>
              <div className="text-[18px] font-bold text-purple">
                {formatInt(totals.migrationEntries)}
              </div>
            </div>
            <div>
              <div className="text-[10px] text-text3 uppercase">{t("w2.migration.assets")}</div>
              <div className="text-[18px] font-bold text-text">
                {formatInt(scanResult.migration.resourcepack_asset_count)}
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Recommended-combo estimate (hidden when it is already selected) */}
      {recommendedCostUsd !== null && (
        <div className="-mt-4 mb-6 flex items-center gap-2 border border-accent-lo bg-tint px-3.5 py-2 font-mono text-[11px] text-text2">
          <span className="bg-accent px-1.5 py-[1px] text-[10px] font-bold tracking-[0.06em] text-sel-ink uppercase">
            {t("w2.recommend.badge")}
          </span>
          <span>
            {t("w2.recommend.line", {
              model: modelDisplayName(RECOMMENDED_MODEL),
              cost: formatUsd(recommendedCostUsd),
            })}
          </span>
        </div>
      )}

      {/* Whether this needs translating at all comes before what it costs */}
      <CommunityBand />

      {/* What the scan filtered, rewrote, or cannot reach at all */}
      <SourceOverrideBand />
      <HardcodedBand />
      <BlacklistBand />

      {/* 2-column: tree + preview */}
      <div className="grid grid-cols-[340px_1fr] gap-3">
        {/* Category tree */}
        <div className="border border-line2 bg-raised">
          <div className="flex items-center justify-between border-b border-line2 px-3.5 py-2.5 font-mono text-[11px] font-semibold tracking-[0.06em] text-text2 uppercase">
            <span>{t("w2.tree.header")}</span>
            <button
              onClick={() =>
                setCategories(
                  categories.map((c) => c.name),
                  !allSelected,
                )
              }
              className="text-text3 uppercase hover:text-text2"
            >
              {allSelected ? t("w2.tree.deselectAll") : t("w2.tree.selectAll")}
            </button>
          </div>
          <div className="py-1">
            {groups.map((group) => {
              // Single-category groups render flat (no header): their own
              // checkbox already is the group toggle.
              const single = group.categories.length === 1;
              const includedCount = group.categories.filter(
                (c) => !excludedCategories.includes(c.name),
              ).length;
              const collapsed = !single && collapsedGroups.has(group.key);
              return (
                <div key={group.key}>
                  {!single && (
                    <GroupRow
                      label={group.label ?? t("w2.tree.groupMods")}
                      group={group}
                      includedCount={includedCount}
                      collapsed={collapsed}
                      onToggle={(included) =>
                        setCategories(
                          group.categories.map((c) => c.name),
                          included,
                        )
                      }
                      onCollapse={() =>
                        setCollapsedGroups((prev) => {
                          const next = new Set(prev);
                          if (!next.delete(group.key)) next.add(group.key);
                          return next;
                        })
                      }
                    />
                  )}
                  <div className={single ? undefined : "pl-3"}>
                    {(collapsed ? [] : group.categories).map((cat) => (
                      <CategoryRow
                        key={`${cat.name}:${cat.handler}`}
                        category={cat}
                        checked={!excludedCategories.includes(cat.name)}
                        expanded={activeCat !== null && activeCat.name === cat.name}
                        onExpand={() => {
                          setOpenCategory(cat.name);
                          setPickedFile(null);
                        }}
                        onToggle={(included) => toggleCategory(cat.name, included)}
                        onPickFile={(path) => {
                          setOpenCategory(cat.name);
                          setPickedFile(path);
                        }}
                        selectedFilePath={activeFile?.path ?? null}
                      />
                    ))}
                  </div>
                </div>
              );
            })}
          </div>
          <div className="flex items-center justify-between border-t border-line2 bg-hover px-3.5 py-2.5">
            <span className="font-mono text-[11px] text-text3">
              {t("w2.tree.selectedCount", { selected: selectedCount, total: categories.length })}
            </span>
            <span className="font-mono text-[11px] font-bold text-accent">
              {t("w2.tree.entriesCount", { count: formatInt(totals.entries) })}
            </span>
          </div>
        </div>

        {/* Preview panel */}
        <div className="flex flex-col border border-line2 bg-raised">
          {activeCat !== null && (
            <>
              <div className="flex items-center justify-between border-b border-line2 px-3.5 py-2.5">
                <div className="flex min-w-0 items-center gap-2">
                  <CategoryIcon category={activeCat} />
                  <span className="truncate text-[12px] font-bold text-text">
                    {activeCat.name} · {activeCat.handler}
                  </span>
                </div>
                <div className="ml-3 shrink-0 font-mono text-[11px] text-text3">
                  {t("w2.preview.meta", {
                    files: formatInt(activeCat.file_count),
                    entries: formatInt(activeCat.entry_count),
                  })}
                </div>
              </div>

              <div className="flex items-center gap-1.5 border-b border-line2 bg-hover px-3.5 py-2 font-mono text-[10px] text-text3">
                <svg width="10" height="10" viewBox="0 0 10 10" fill="none" stroke="currentColor" strokeWidth="1.5">
                  <path d="M1 3 L5 7 L9 3" />
                </svg>
                <span className="truncate">{breadcrumb}</span>
              </div>

              <div className="max-h-[380px] flex-1 overflow-y-auto">
                <FileRows
                  category={activeCat}
                  selectedFile={activeFile}
                  onPickFile={(path) => setPickedFile(path)}
                />
              </div>
            </>
          )}
        </div>
      </div>

      {/* Wizard footer */}
      <div className="mt-6 flex items-center justify-between border-t border-line pt-5">
        <button
          onClick={() => go("w1")}
          className="flex items-center gap-1.5 px-[18px] py-2.5 text-[13px] font-semibold text-text2 hover:text-text"
        >
          <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.5">
            <path d="M8 2 L4 6 L8 10" />
          </svg>
          {t("w2.footer.back")}
        </button>
        <div className="flex items-center gap-3">
          <span className="font-mono text-[11px] text-text3">
            {t("w2.footer.entriesPart", { entries: formatInt(totals.entries) })}
            {totals.migrationEntries > 0 && (
              <span className="text-purple">
                {t("w2.footer.reusedPart", {
                  reused: formatInt(totals.migrationEntries),
                })}
              </span>
            )}
            <span className="text-accent">
              {price !== null
                ? costText
                : t("w2.footer.tokensOnly", { count: formatCompact(usage.totalTokens) })}
            </span>
            {t("w2.footer.estSuffix")}
          </span>
          <button
            onClick={() => go("w3")}
            disabled={totals.entries === 0}
            className="flex items-center gap-1.5 px-5 py-2.5 bg-accent text-[13px] font-bold text-sel-ink hover:bg-accent-hi disabled:cursor-not-allowed disabled:opacity-40"
          >
            {t("w2.footer.next")}
            <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M4 2 L8 6 L4 10" />
            </svg>
          </button>
        </div>
      </div>
    </div>
  );
}

export function W2Scan() {
  const scanState = useWizard((s) => s.scanState);
  const scanResult = useWizard((s) => s.scanResult);

  if (scanState === "failed") return <FailedState />;
  if (scanState === "done" && scanResult !== null) return <DoneState />;
  return <ScanningState />;
}
