/**
 * Engine API client (contracts/engine-api.yaml). All calls carry the
 * session token; the base URL comes from the engine store once the
 * sidecar handshake completes.
 */

import type {
  CheckpointSummary,
  ImportedModpack,
  CommunitySyncResult,
  Entry,
  EntryContext,
  EntryCounts,
  EntryFilter,
  EntryPage,
  ExportParams,
  Glossary,
  Job,
  JobEventFrame,
  JobSnapshot,
  ModSourcesResult,
  PipelineStats,
  PlaceholderPattern,
  Provider,
  ProviderModels,
  ProviderTestResult,
  ResumeTranslateParams,
  ScanParams,
  ScanResult,
  SourceTextExportParams,
  TmStats,
  TranslateParams,
  TranslationGraphResponse,
  UploadParams,
  ValidationIssue,
} from "../../../shared/engine";
import { useEngineStore } from "../stores/engine";
import { isCliProvider } from "./cliProviders";
import { rememberCliProviders } from "./models";

export class EngineApiError extends Error {
  constructor(
    readonly status: number,
    detail: string,
  ) {
    super(detail);
    this.name = "EngineApiError";
  }
}

function endpoint(): { base: string; token: string } {
  const { info } = useEngineStore.getState();
  if (info.state !== "ready" || info.port === null || info.token === null) {
    throw new EngineApiError(0, "engine is not ready");
  }
  return { base: `http://127.0.0.1:${info.port}`, token: info.token };
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const { base, token } = endpoint();
  const res = await fetch(`${base}${path}`, {
    ...init,
    headers: {
      authorization: `Bearer ${token}`,
      ...(init?.body !== undefined ? { "content-type": "application/json" } : {}),
      ...init?.headers,
    },
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = (await res.json()) as { detail?: string };
      if (body.detail !== undefined) detail = body.detail;
    } catch {
      // non-JSON error body
    }
    throw new EngineApiError(res.status, detail);
  }
  return (await res.json()) as T;
}

export interface SessionSummary {
  id: string;
  modpack_name: string;
  modpack_path: string;
  source_locale: string;
  target_locale: string;
  model: string;
  status: string;
  created_at: string | null;
  finished_at: string | null;
  total_entries: number;
  done_entries: number;
  stats: Record<string, unknown> | null;
  export_zip_path: string | null;
  export_overrides_zip_path: string | null;
  export_bilingual_zip_path: string | null;
  export_bilingual_overrides_zip_path: string | null;
}

/**
 * GET /community/translation — a published pack already covering this
 * modpack. Declared here rather than in shared/engine.ts only because that
 * file is being edited elsewhere; it belongs there with the other wire types.
 *
 * `uncovered_entries` is the field that matters and it is a LOWER BOUND, not
 * a percentage: null means the local side was not measured, 0 means no
 * missing entry could be established. Never present it as exact coverage.
 */
export interface TranslationMatch {
  pack_id: string;
  modpack_version: string | null;
  exact: boolean;
  compatible_versions: { min: string; max: string } | null;
  total_entries: number | null;
  uncovered_entries: number | null;
  uncovered_by_category: Record<string, number>;
  url: string | null;
  download_url: string | null;
  note: string;
}

/** POST /community/translation/download — local ZIPs for the A/B/C inputs. */
export interface CommunityDownloadResult {
  resourcepack_path: string | null;
  overrides_path: string | null;
}

/** Engine's largest allowed entries page; bulk walks use it to cut round-trips. */
const FAILED_PAGE_SIZE = 500;

export const api = {
  startScan: (params: ScanParams) =>
    request<Job>("/jobs", { method: "POST", body: JSON.stringify({ type: "scan", params }) }),
  startTranslate: (params: TranslateParams) =>
    request<Job>("/jobs", { method: "POST", body: JSON.stringify({ type: "translate", params }) }),
  /** Continue an interrupted session from the engine's durable checkpoint. */
  resumeTranslate: (params: ResumeTranslateParams) =>
    request<Job>("/jobs", { method: "POST", body: JSON.stringify({ type: "translate", params }) }),
  /** Both export entry points: W6 (translated) and W3 (source text). */
  startExport: (params: ExportParams | SourceTextExportParams) =>
    request<Job>("/jobs", { method: "POST", body: JSON.stringify({ type: "export", params }) }),
  startUpload: (params: UploadParams) =>
    request<Job>("/jobs", { method: "POST", body: JSON.stringify({ type: "upload", params }) }),

  job: (id: string) => request<Job>(`/jobs/${id}`),
  jobSnapshot: (id: string) => request<JobSnapshot>(`/jobs/${id}/snapshot`),
  cancelJob: (id: string) => request<{ id: string; status: string }>(`/jobs/${id}/cancel`, { method: "POST" }),
  /** Stop a translation resumably: it ends interrupted (reason `paused`). */
  pauseJob: (id: string) => request<{ id: string; status: string }>(`/jobs/${id}/pause`, { method: "POST" }),

  scanResult: (jobId: string) => request<ScanResult>(`/scan/${jobId}/result`),
  modSources: (scanJobId: string) =>
    request<ModSourcesResult>(`/jobs/${scanJobId}/mod-sources`),

  entries: (
    jobId: string,
    filter: EntryFilter,
    page: number,
    pageSize = 100,
    search = "",
  ) =>
    request<EntryPage>(
      `/translate/${jobId}/entries?filter=${filter}&page=${page}&page_size=${pageSize}` +
        (search === "" ? "" : `&search=${encodeURIComponent(search)}`),
    ),
  /**
   * Every failed entry's identity, walked across pages.
   *
   * The review screen holds one PAGE_SIZE slice, so a bulk retry driven off
   * the rows in view silently skips failures sitting on other pages.
   */
  allFailedRefs: async (jobId: string): Promise<{ key: string; file: string }[]> => {
    const refs: { key: string; file: string }[] = [];
    for (let page = 1; ; page += 1) {
      const slice = await api.entries(jobId, "failed", page, FAILED_PAGE_SIZE);
      if (slice.entries.length === 0) break;
      refs.push(...slice.entries.map((e) => ({ key: e.key, file: e.file })));
      if (refs.length >= slice.total) break;
    }
    return refs;
  },
  retranslateEntry: (jobId: string, key: string, file?: string) =>
    request<Entry>(`/translate/${jobId}/entries/${encodeURIComponent(key)}/retranslate`, {
      method: "POST",
      body: JSON.stringify({ file }),
    }),
  /** Every bucket's count in one server-side pass over the result. */
  entryCounts: (jobId: string, search = "") =>
    request<EntryCounts>(
      `/translate/${jobId}/entries/counts` +
        (search === "" ? "" : `?search=${encodeURIComponent(search)}`),
    ),
  /**
   * Commit or draft one hand translation. No model, no provider, no network
   * beyond the loopback engine — this is the manual write path.
   *
   * `commit: false` records the in-progress text durably without settling the
   * entry, so a crash cannot lose it and nothing half-written can ship.
   * `srcSha` lets a later scan detect that the source changed underneath the
   * translation instead of shipping it as if still valid.
   */
  commitEntry: (
    jobId: string,
    key: string,
    translatedText: string,
    opts: {
      file?: string;
      commit?: boolean;
      srcSha?: string;
      flagged?: boolean;
    } = {},
  ) =>
    request<Entry>(`/translate/${jobId}/entries/${encodeURIComponent(key)}`, {
      method: "PATCH",
      body: JSON.stringify({
        translated_text: translatedText,
        file: opts.file,
        commit: opts.commit ?? true,
        origin: "human",
        src_sha: opts.srcSha,
        flagged: opts.flagged,
      }),
    }),
  /** Per-entry translation aids. Never needs a provider. */
  entryContext: (jobId: string, key: string, file?: string) =>
    request<EntryContext>(
      `/translate/${jobId}/entries/${encodeURIComponent(key)}/context` +
        (file === undefined ? "" : `?file=${encodeURIComponent(file)}`),
    ),
  /** Live validation of a draft. Pure and synchronous server-side. */
  validateDraft: (jobId: string, key: string, translatedText: string, file?: string) =>
    request<{ issues: ValidationIssue[] }>(`/translate/${jobId}/validate`, {
      method: "POST",
      body: JSON.stringify({ key, file, translated_text: translatedText }),
    }),
  /** The engine's own placeholder patterns, in overlap-priority order. */
  placeholderPatterns: () =>
    request<{ patterns: PlaceholderPattern[] }>("/placeholder/patterns"),
  /**
   * Translation-graph snapshot (live while running, rebuilt after).
   * `knownVersion` short-circuits an unchanged poll server-side; the
   * caller must only send it when q/status match the snapshot it holds —
   * the version check ignores filters.
   */
  translationGraph: (
    jobId: string,
    opts: {
      q?: string;
      status?: "all" | "settled" | "pending";
      limitTerms?: number;
      mentionsPerTerm?: number;
      knownVersion?: number;
    } = {},
  ) => {
    const params = new URLSearchParams();
    if (opts.q !== undefined && opts.q !== "") params.set("q", opts.q);
    if (opts.status !== undefined) params.set("status", opts.status);
    if (opts.limitTerms !== undefined) params.set("limit_terms", String(opts.limitTerms));
    if (opts.mentionsPerTerm !== undefined)
      params.set("mentions_per_term", String(opts.mentionsPerTerm));
    if (opts.knownVersion !== undefined) params.set("known_version", String(opts.knownVersion));
    const qs = params.toString();
    return request<TranslationGraphResponse>(
      `/translate/${jobId}/graph${qs === "" ? "" : `?${qs}`}`,
    );
  },
  translateStats: (jobId: string) =>
    request<PipelineStats>(`/translate/${jobId}/stats`),

  glossary: (sourceLang: string, targetLang: string) =>
    request<Glossary>(`/glossary?source_lang=${sourceLang}&target_lang=${targetLang}`),
  putGlossary: (doc: Glossary) =>
    request<Glossary>("/glossary", { method: "PUT", body: JSON.stringify(doc) }),

  tmStats: () => request<TmStats>("/tm/stats"),
  syncCommunity: (webUrl: string, targetLang: string, sourceLang = "en_us") =>
    request<CommunitySyncResult>("/community/sync", {
      method: "POST",
      body: JSON.stringify({
        web_url: webUrl,
        source_lang: sourceLang,
        target_lang: targetLang,
      }),
    }),
  communityTranslation: (webUrl: string, jobId: string, targetLang: string) => {
    const params = new URLSearchParams({
      web_url: webUrl,
      job_id: jobId,
      target_lang: targetLang,
    });
    return request<{ match: TranslationMatch | null }>(
      `/community/translation?${params.toString()}`,
    );
  },
  downloadCommunityTranslation: (packId: string, downloadUrl: string) =>
    request<CommunityDownloadResult>("/community/translation/download", {
      method: "POST",
      body: JSON.stringify({ pack_id: packId, download_url: downloadUrl }),
    }),
  providers: async () => {
    const providers = await request<Provider[]>("/providers");
    // Keep the id-only view used by pricing in step with the catalog, so a
    // CLI provider the engine adds later still estimates at zero cost.
    rememberCliProviders(providers.filter(isCliProvider).map((p) => p.id));
    return providers;
  },
  testProvider: (provider: string, apiKey?: string, model?: string, apiBase?: string) =>
    request<ProviderTestResult>("/providers/test", {
      method: "POST",
      body: JSON.stringify({ provider, api_key: apiKey, model, api_base: apiBase }),
    }),
  providerModels: (provider: string, apiKey?: string, apiBase?: string) =>
    request<ProviderModels>("/providers/models", {
      method: "POST",
      body: JSON.stringify({ provider, api_key: apiKey, api_base: apiBase }),
    }),

  config: () => request<Record<string, unknown>>("/config"),
  putConfig: (config: Record<string, unknown>) =>
    request<Record<string, unknown>>("/config", { method: "PUT", body: JSON.stringify(config) }),

  listSessions: () => request<SessionSummary[]>("/sessions"),
  restoreSession: (sessionId: string) =>
    request<Job>(`/sessions/${sessionId}/restore`, { method: "POST" }),
  exportSession: (sessionId: string, outputPath: string) =>
    request<{ status: string; path: string }>(`/sessions/${sessionId}/export`, {
      method: "POST",
      body: JSON.stringify({ output_path: outputPath }),
    }),
  importSession: (inputPath: string) =>
    request<{ status: string; session: SessionSummary; job: Job }>("/sessions/import", {
      method: "POST",
      body: JSON.stringify({ input_path: inputPath }),
    }),
  deleteSession: (sessionId: string) =>
    request<{ status: string; id: string }>(`/sessions/${sessionId}`, { method: "DELETE" }),
  /** Sessions whose translation stopped early and can be resumed. */
  checkpoints: () => request<CheckpointSummary[]>("/checkpoints"),

  /** Extract a .zip/.mrpack modpack (cached) and return the folder to scan. */
  importModpack: (archivePath: string) =>
    request<ImportedModpack>("/modpacks/import", {
      method: "POST",
      body: JSON.stringify({ archive_path: archivePath }),
    }),
};

/**
 * Subscribe to a job's event stream. Buffered history replays first, then
 * live frames; the socket closes itself after the terminal frame.
 * Returns an unsubscribe (close) function.
 */
export function openJobEvents(
  jobId: string,
  onFrame: (frame: JobEventFrame) => void,
  onClose?: (event: CloseEvent, manuallyClosed: boolean) => void,
  after?: number,
): () => void {
  const { base, token } = endpoint();
  const cursor = after === undefined ? "" : `&after=${after}`;
  const url = `${base.replace("http", "ws")}/jobs/${jobId}/events?token=${token}${cursor}`;
  const ws = new WebSocket(url);
  let manuallyClosed = false;
  ws.onmessage = (event) => {
    let frame: JobEventFrame;
    try {
      frame = JSON.parse(event.data as string) as JobEventFrame;
    } catch {
      // One unreadable frame must not throw out of the socket handler and
      // stop the rest of the stream from being processed.
      console.warn(`[engine] dropped an unparseable event frame for job ${jobId}`);
      return;
    }
    onFrame(frame);
  };
  ws.onclose = (event) => onClose?.(event, manuallyClosed);
  return () => {
    manuallyClosed = true;
    ws.close();
  };
}
