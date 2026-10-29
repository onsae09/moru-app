/**
 * Translation-graph canvas (ECharts force layout).
 *
 * Live jobs poll /translate/{id}/graph every 3s with known_version, so an
 * unchanged graph costs one tiny round-trip and never re-renders the
 * canvas; finished jobs (and restored sessions) fetch once. Rendered as a
 * collapsible section — the query and the chart only exist while open.
 */

import { useQuery } from "@tanstack/react-query";
import { GraphChart } from "echarts/charts";
import type { GraphSeriesOption } from "echarts/charts";
import { LegendComponent, TooltipComponent } from "echarts/components";
import type { LegendComponentOption, TooltipComponentOption } from "echarts/components";
import * as echarts from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";
import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import type {
  TranslationGraphNode,
  TranslationGraphSnapshot,
} from "../../../shared/engine";
import { pressable } from "@/components/a11y";
import { api, EngineApiError } from "@/lib/api";
import { cssVar, useResolvedTheme, withAlpha } from "@/lib/theme";

echarts.use([GraphChart, TooltipComponent, LegendComponent, CanvasRenderer]);

type ChartOption = echarts.ComposeOption<
  GraphSeriesOption | TooltipComponentOption | LegendComponentOption
>;

type StatusFilter = "all" | "settled" | "pending";

type EdgeStyle = { color: string; type: "solid" | "dashed"; opacity: number };

/**
 * Canvas colors come from the live CSS tokens (ECharts cannot resolve
 * var()), read at render time so a theme switch repaints the graph.
 */
function readPalette() {
  const accent = cssVar("--color-accent");
  const amber = cssVar("--color-amber");
  const muted = cssVar("--color-text3");
  const purple = cssVar("--color-purple");
  const edges: Record<string, EdgeStyle> = {
    defines: { color: withAlpha(accent, 0.55), type: "solid", opacity: 0.9 },
    mentions: { color: withAlpha(muted, 0.35), type: "solid", opacity: 0.6 },
    sibling: { color: withAlpha(purple, 0.55), type: "dashed", opacity: 0.8 },
  };
  return {
    settled: accent,
    pending: amber,
    entry: muted,
    edges,
    tooltipBg: cssVar("--color-well"),
    tooltipBorder: cssVar("--color-edge"),
    tooltipText: cssVar("--color-text"),
    legendText: cssVar("--color-text2"),
    labelText: cssVar("--color-text2"),
  };
}

function escapeHtml(text: string): string {
  return text
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

/**
 * Recover the node we attached to an ECharts datum (`raw` in setOption).
 * In-process round-trip: ECharts only erases the type, so after checking
 * the property exists, the single named cast restores what we stored.
 */
function nodeOfDatum(data: unknown): TranslationGraphNode | null {
  if (data === null || typeof data !== "object" || !("raw" in data)) return null;
  return data.raw as TranslationGraphNode;
}

function GraphCanvas({
  snapshot,
  onSelect,
}: {
  snapshot: TranslationGraphSnapshot;
  onSelect: (node: TranslationGraphNode | null) => void;
}) {
  const { t } = useTranslation();
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<echarts.ECharts | null>(null);
  const onSelectRef = useRef(onSelect);
  onSelectRef.current = onSelect;
  const theme = useResolvedTheme();

  useEffect(() => {
    const el = containerRef.current;
    if (el === null) return;
    const chart = echarts.init(el);
    chartRef.current = chart;
    chart.on("click", (params) => {
      if (params.dataType === "node") {
        onSelectRef.current(nodeOfDatum(params.data));
      }
    });
    const observer = new ResizeObserver(() => chart.resize());
    observer.observe(el);
    return () => {
      observer.disconnect();
      chart.dispose();
      chartRef.current = null;
    };
  }, []);

  useEffect(() => {
    const chart = chartRef.current;
    if (chart === null) return;
    const palette = readPalette();
    const categories = [
      { name: t("graph.legend.settledTerm"), itemStyle: { color: palette.settled } },
      { name: t("graph.legend.pendingTerm"), itemStyle: { color: palette.pending } },
      { name: t("graph.legend.entry"), itemStyle: { color: palette.entry } },
    ];
    const option: ChartOption = {
      backgroundColor: "transparent",
      tooltip: {
        confine: true,
        backgroundColor: palette.tooltipBg,
        borderColor: palette.tooltipBorder,
        textStyle: { color: palette.tooltipText, fontSize: 11, fontFamily: "monospace" },
        formatter: (params) => {
          const node = nodeOfDatum(
            Array.isArray(params) ? params[0]?.data : params.data,
          );
          if (node === null) return "";
          if (node.kind === "term") {
            const target =
              node.target != null
                ? escapeHtml(node.target)
                : `<i>${escapeHtml(t("graph.detail.pending"))}</i>`;
            return (
              `<b>${escapeHtml(node.label)}</b> → ${target}<br/>` +
              `${escapeHtml(t("graph.detail.definers"))}: ${node.definers ?? 0} · ` +
              `${escapeHtml(t("graph.detail.mentions"))}: ${node.mentions ?? 0}`
            );
          }
          return (
            `<b>${escapeHtml(node.label)}</b><br/>` +
            `${escapeHtml(node.file ?? "")}<br/>` +
            `${escapeHtml(t("graph.detail.settled"))}: ${node.settled ? "✓" : "✗"}`
          );
        },
      },
      legend: {
        top: 4,
        left: 8,
        icon: "rect",
        itemWidth: 8,
        itemHeight: 8,
        textStyle: { color: palette.legendText, fontSize: 10, fontFamily: "monospace" },
        data: categories.map((c) => c.name),
      },
      series: [
        {
          type: "graph",
          layout: "force",
          roam: true,
          draggable: true,
          categories,
          force: { repulsion: 130, edgeLength: [40, 110], gravity: 0.08, friction: 0.2 },
          label: {
            position: "right",
            color: palette.labelText,
            fontSize: 10,
            fontFamily: "monospace",
          },
          labelLayout: { hideOverlap: true },
          emphasis: { focus: "adjacency", label: { show: true } },
          lineStyle: { curveness: 0.08 },
          data: snapshot.nodes.map((node) => ({
            id: node.id,
            name: node.label,
            raw: node,
            category: node.kind === "entry" ? 2 : node.settled ? 0 : 1,
            symbolSize:
              node.kind === "term" ? Math.min(34, 12 + (node.mentions ?? 0) * 1.5) : 7,
            label: { show: node.kind === "term" },
          })),
          links: snapshot.edges.map((edge) => ({
            source: edge.source,
            target: edge.target,
            lineStyle: palette.edges[edge.kind] ?? palette.edges.mentions,
          })),
        },
      ],
    };
    chart.setOption(option);
    // `theme` is read through the CSS tokens above, so it must re-run this.
  }, [snapshot, t, theme]);

  return <div ref={containerRef} className="h-[420px] w-full" />;
}

function DetailPanel({ node }: { node: TranslationGraphNode }) {
  const { t } = useTranslation();
  const rows: [string, React.ReactNode][] =
    node.kind === "term"
      ? [
          [t("graph.detail.source"), node.label],
          [
            t("graph.detail.target"),
            node.target ?? <span className="text-amber">{t("graph.detail.pending")}</span>,
          ],
          [t("graph.detail.category"), node.category ?? "—"],
          [t("graph.detail.definers"), String(node.definers ?? 0)],
          [t("graph.detail.mentions"), String(node.mentions ?? 0)],
        ]
      : [
          [t("graph.detail.key"), node.label],
          [t("graph.detail.file"), node.file ?? "—"],
          [
            t("graph.detail.settled"),
            node.settled ? (
              <span className="text-accent">✓</span>
            ) : (
              <span className="text-text3">✗</span>
            ),
          ],
        ];
  return (
    <div className="w-[260px] shrink-0 border-l border-line2 px-3 py-2 font-mono text-[11px]">
      <div className="mb-2 text-[10px] font-semibold tracking-[0.06em] text-text3 uppercase">
        {node.kind === "term" ? t("graph.detail.term") : t("graph.detail.entry")}
      </div>
      {rows.map(([label, value]) => (
        <div key={label} className="mb-1.5">
          <div className="text-[10px] text-text3 uppercase">{label}</div>
          <div className="break-all text-text2">{value}</div>
        </div>
      ))}
    </div>
  );
}

function GraphBody({ jobId, live }: { jobId: string; live: boolean }) {
  const { t } = useTranslation();
  const [search, setSearch] = useState("");
  const [q, setQ] = useState("");
  const [status, setStatus] = useState<StatusFilter>("all");
  const [selected, setSelected] = useState<TranslationGraphNode | null>(null);
  const lastSnapRef = useRef<{ snap: TranslationGraphSnapshot; q: string; status: StatusFilter } | null>(null);

  useEffect(() => {
    const id = setTimeout(() => setQ(search.trim()), 300);
    return () => clearTimeout(id);
  }, [search]);

  const query = useQuery({
    queryKey: ["graph", jobId, q, status],
    queryFn: async (): Promise<TranslationGraphSnapshot> => {
      const last = lastSnapRef.current;
      // known_version ignores filters server-side; only reuse it when the
      // held snapshot was fetched with the SAME filters.
      const held = last !== null && last.q === q && last.status === status ? last.snap : null;
      const res = await api.translationGraph(jobId, {
        q,
        status,
        knownVersion: held?.version,
      });
      if ("unchanged" in res) {
        // Only reachable when knownVersion was sent, i.e. held !== null.
        if (held === null) throw new EngineApiError(0, "unexpected unchanged response");
        const snap = { ...held, job_finished: res.job_finished };
        lastSnapRef.current = { snap, q, status };
        return snap;
      }
      lastSnapRef.current = { snap: res, q, status };
      return res;
    },
    refetchInterval: (current) =>
      live && current.state.data?.job_finished !== true ? 3000 : false,
    staleTime: live ? 0 : 60_000,
  });

  const snapshot = query.data;
  const unavailable =
    query.error instanceof EngineApiError && query.error.status === 409
      ? query.error.message
      : null;

  const statLine = useMemo(() => {
    if (snapshot === undefined) return null;
    const { stats } = snapshot;
    return t("graph.stats", {
      terms: stats.terms,
      mentions: stats.mentions,
      groups: stats.sibling_groups,
    });
  }, [snapshot, t]);

  return (
    <div className="border-t border-line2">
      {/* controls */}
      <div className="flex items-center gap-2 border-b border-line2 px-[14px] py-2">
        <input
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder={t("graph.search")}
          className="w-[200px] border border-line2 bg-transparent px-2 py-1 font-mono text-[11px] text-text placeholder:text-text4 focus:border-accent focus:outline-none"
        />
        {(["all", "settled", "pending"] as const).map((f) => (
          <button
            key={f}
            onClick={() => setStatus(f)}
            className={`border px-2 py-1 font-mono text-[10px] uppercase ${
              status === f
                ? "border-accent text-accent"
                : "border-line2 text-text3 hover:text-text"
            }`}
          >
            {t(`graph.status.${f}`)}
          </button>
        ))}
        <div className="ml-auto flex items-center gap-2 font-mono text-[10px] text-text3">
          {snapshot?.truncated === true && (
            <span className="bg-amber/10 px-1.5 py-0.5 text-amber">
              {t("graph.truncated")}
            </span>
          )}
          {statLine !== null && <span>{statLine}</span>}
          {!live && (
            <button
              onClick={() => void query.refetch()}
              className="border border-line2 px-2 py-0.5 text-text3 hover:text-text"
            >
              {t("graph.refresh")}
            </button>
          )}
        </div>
      </div>

      {/* canvas + detail */}
      {unavailable !== null ? (
        <div className="px-[14px] py-8 text-center font-mono text-[11px] text-text3">
          {t("graph.unavailable")}
        </div>
      ) : query.isPending ? (
        <div className="px-[14px] py-8 text-center font-mono text-[11px] text-text3">
          {t("graph.loading")}
        </div>
      ) : query.isError ? (
        <div className="px-[14px] py-8 text-center font-mono text-[11px] text-red">
          {String(query.error)}
        </div>
      ) : snapshot !== undefined && snapshot.nodes.length === 0 ? (
        <div className="px-[14px] py-8 text-center font-mono text-[11px] text-text3">
          {t("graph.empty")}
        </div>
      ) : snapshot !== undefined ? (
        <div className="flex">
          <div className="min-w-0 flex-1">
            <GraphCanvas snapshot={snapshot} onSelect={setSelected} />
          </div>
          {selected !== null && <DetailPanel node={selected} />}
        </div>
      ) : null}
    </div>
  );
}

/**
 * Collapsible section wrapper (W4 log-panel look). The graph query and the
 * ECharts instance only live while the section is open.
 */
export function TranslationGraphView({ jobId, live }: { jobId: string; live: boolean }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  return (
    <div className="border border-line2 bg-raised">
      <div
        {...pressable(() => setOpen((o) => !o))}
        aria-expanded={open}
        className="flex cursor-pointer items-center justify-between px-[14px] py-2.5 font-mono text-[11px] font-semibold tracking-[0.06em] text-text2 uppercase"
      >
        <div className="flex items-center gap-1.5">
          <svg className="text-accent" width="10" height="10" viewBox="0 0 10 10" fill="none" stroke="currentColor" strokeWidth="1.5">
            <circle cx="2.5" cy="2.5" r="1.5" />
            <circle cx="7.5" cy="7.5" r="1.5" />
            <path d="M3.5 3.5 L6.5 6.5" />
          </svg>
          <span>{t("graph.title")}</span>
          <span className="text-text4">{open ? "▾" : "▸"}</span>
        </div>
        {live && open && (
          <span className="flex items-center gap-1.5 text-[10px] text-accent normal-case">
            <span className="h-[5px] w-[5px] animate-pulse bg-accent" />
            {t("graph.live")}
          </span>
        )}
      </div>
      {open && <GraphBody jobId={jobId} live={live} />}
    </div>
  );
}
