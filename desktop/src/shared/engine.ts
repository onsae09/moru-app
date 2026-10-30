/**
 * Engine API contract types - mirror of contracts/engine-api.yaml.
 * Keep field names snake_case: these are wire types, not view models.
 */

export type JobType = "scan" | "translate" | "export" | "upload";

/**
 * `interrupted` (translate only): stopped early but resumable — the provider
 * ran out of capacity, or the engine shut down mid-run. Its partial result
 * stays reviewable like a cancelled one.
 */
export type JobStatus =
  | "pending"
  | "running"
  | "done"
  | "failed"
  | "cancelled"
  | "interrupted";

export interface Job {
  id: string;
  type: JobType;
  status: JobStatus;
  error: string | null;
  created_at: string;
}

/**
 * `pending` is a seeded, not-yet-touched entry from a manual-seed run. It is
 * deliberately outside the set that feeds the resource pack, so an
 * untranslated entry can never ship.
 */
export type EntryStatus =
  | "passed"
  | "warning"
  | "failed"
  | "modified"
  | "tm_hit"
  | "migrated"
  | "skipped"
  | "pending";

/** Entry buckets the engine can filter by. Some are views, not statuses. */
export type EntryFilter =
  | "all"
  | "pending"
  | "failed"
  | "warning"
  | "modified"
  | "flagged"
  | "stale_source"
  | "reviewed"
  | "unreviewed"
  | "glossary";

/** Who produced `translated_text`. Absent on entries written before it existed. */
export type EntryOrigin = "machine" | "human" | "tm" | "community";

export interface Entry {
  key: string;
  file: string;
  source_text: string;
  translated_text: string;
  status: EntryStatus;
  errors: string[];
  origin?: EntryOrigin | null;
  flagged?: boolean;
  /** Signed off on the review screen. Absent means not yet. */
  reviewed?: boolean;
  /**
   * Agreed glossary target terms the translation lacks although its source
   * names them. Only on `/entries` rows that lack any.
   */
  glossary_missing?: string[];
  /**
   * The entry holds a human translation whose recorded source hash no longer
   * matches the scanned source: the pack changed the string underneath it.
   * The translation is kept, never discarded, and export is blocked until the
   * user confirms it.
   */
  stale_source?: boolean;
}

/** The buckets an AI re-review pass can cover. */
export type ReviewBucket = "failed" | "warning" | "glossary";

/** POST /translate/{jobId}/review/targets — what a re-review would send. */
export interface ReviewTargets {
  refs: { key: string; file: string }[];
  /** Hand translations, never offered to the model. */
  skipped_human: number;
  /** Signed-off entries, left out unless asked for. */
  skipped_reviewed: number;
}

/** POST /translate/{jobId}/review — one slice of a re-review pass. */
export interface ReviewSliceResult {
  /** Entries whose new translation was better and replaced the old one. */
  improved: Entry[];
  /** Reviewed, but the answer was no better: left as they were. */
  unchanged: number;
  /** Not reached: the slice was interrupted first. */
  pending: number;
  /** Gone, or hand-edited since the target list was built. */
  skipped: number;
  /** Set when the provider refused the credential or ran out of quota. */
  interruption: { reason: string; message: string; detail: string } | null;
  stats: PipelineStats;
}

export interface EntryPage {
  total: number;
  page: number;
  entries: Entry[];
}

/** GET /translate/{jobId}/entries/counts — every bucket in one pass. */
export type EntryCounts = Record<EntryFilter, number>;

/** GET /translate/{jobId}/entries/files — one row per source file. */
export interface EntryFileProgress {
  file: string;
  total: number;
  failed: number;
  modified: number;
  reviewed: number;
}

/**
 * POST /translate/{jobId}/validate — structured issues, unlike the flattened
 * English strings on `Entry.errors`. Switch on `issue_type` to localize;
 * `message` is English-only.
 */
export type ValidationIssueType =
  | "key_mismatch"
  | "empty_translation"
  | "untranslated"
  | "placeholder_count"
  | "placeholder_order"
  | "color_code"
  | "length_ratio"
  | "glossary_term_mismatch"
  | "glossary_noun_mismatch"
  | "format_string";

export interface ValidationIssue {
  issue_type: ValidationIssueType;
  severity: "error" | "warning" | "info";
  key?: string;
  message: string;
  suggestion?: string | null;
  source_value?: string | null;
  translated_value?: string | null;
}

/**
 * GET /translate/{jobId}/entries/{key}/context — per-entry translation aids.
 * Every field is derived without a model or a network call, so this is
 * available with no provider configured.
 */
export interface EntrySibling {
  key: string;
  source_text: string;
  translated_text: string | null;
  status: EntryStatus;
}

export interface ContextTermRule {
  aliases: string[];
  term_ko: string;
  preferred_style?: string | null;
  /**
   * Dotted lang-key globs the rule is limited to. Already evaluated
   * server-side — the rule is only present because it applies to this entry —
   * so this is for showing WHY it applies, never for re-filtering.
   */
  key_scope: string[];
}

export interface ContextProperNoun {
  source_like: string;
  preferred_ko: string;
}

/** Prose-only guidance: never machine-checked. Display, do not imply enforcement. */
export interface ContextFormattingRule {
  rule_name: string;
  description: string;
}

export interface ContextTmMatch {
  translated_text: string;
  origin: string;
  updated_at?: string | null;
}

export interface ContextTmSibling {
  file: string;
  key: string;
  translated_text: string;
  agrees: boolean;
}

export interface ContextPlaceholder {
  token: string;
  kind: string;
  literal: string;
}

export interface EntryContext {
  mod_id: string | null;
  namespace: string | null;
  content_type: string | null;
  file: string;
  /** The numbered-key run in ordinal order, excluding this entry. */
  siblings: EntrySibling[];
  glossary: {
    terms: ContextTermRule[];
    proper_nouns: ContextProperNoun[];
    formatting: ContextFormattingRule[];
  };
  tm: {
    exact: ContextTmMatch | null;
    same_source_elsewhere: ContextTmSibling[];
  };
  placeholders: ContextPlaceholder[];
}

/**
 * POST /translate/{jobId}/entries/{key}/assist — advisory only. Nothing here
 * has been written to the entry; the user decides what to accept.
 */
export type AssistKind = "suggest" | "alternatives" | "explain";

export interface AssistSuggestion {
  text: string;
  note?: string | null;
}

export interface AssistResult {
  kind: AssistKind;
  model: string | null;
  elapsed_ms: number;
  suggestions: AssistSuggestion[];
  explanation: string | null;
}

/**
 * GET /placeholder/patterns — the engine's own pattern list, in
 * overlap-priority order (earlier wins). Fetched so the renderer highlights
 * and counts tokens with the same definitions the validator enforces, rather
 * than keeping a second copy that drifts.
 */
export interface PlaceholderPattern {
  name: string;
  regex: string;
  kind?: string;
}

/** /translate/{jobId}/graph — translation-graph snapshot for the canvas. */
export interface TranslationGraphNode {
  id: string;
  kind: "term" | "entry";
  label: string;
  settled: boolean;
  /** term only: settled translation (null while pending). */
  target?: string | null;
  /** term only */
  category?: string;
  definers?: number;
  mentions?: number;
  /** entry only */
  file?: string;
}

export interface TranslationGraphEdge {
  source: string;
  target: string;
  kind: "defines" | "mentions" | "sibling";
}

export interface TranslationGraphSnapshot {
  version: number;
  job_finished: boolean;
  truncated: boolean;
  stats: { entries: number; terms: number; mentions: number; sibling_groups: number };
  nodes: TranslationGraphNode[];
  edges: TranslationGraphEdge[];
}

/** known_version matched: the payload-free polling short form. */
export interface TranslationGraphUnchanged {
  version: number;
  unchanged: true;
  job_finished: boolean;
}

export type TranslationGraphResponse = TranslationGraphSnapshot | TranslationGraphUnchanged;

export interface ScanFile {
  path: string;
  entry_count: number;
  char_count: number;
  /** Exact A/B/C matches that skip AI translation (migration scans only). */
  migration_entry_count?: number;
  migration_char_count?: number;
  sample: Record<string, string>;
}

/** Compact restore payload: current UI state plus a gap-free WS cursor. */
export interface JobSnapshot {
  job: Job;
  cursor: number;
  events: JobEventFrame[];
  failed_count: number;
  translation_started_at: number | null;
}

export interface ScanCategory {
  name: string;
  handler: string;
  file_count: number;
  entry_count: number;
  char_count: number;
  files: ScanFile[];
}

/** Pack identity resolved by the scanner from launcher metadata/manifests. */
export interface PackIdentity {
  name: string | null;
  version: string | null;
  mc_version: string | null;
  loader: string | null;
  curseforge_project_id: number | null;
  curseforge_file_id: number | null;
  modrinth_project_id: string | null;
  modrinth_version_id: string | null;
  source:
    | "curseforge_instance"
    | "curseforge_manifest"
    | "modrinth_pack"
    | "prism_managed"
    | "prism_instance"
    | "folder";
  confident: boolean;
}

/** One mod JAR the mod blacklist kept out of the scan. */
export interface ExcludedMod {
  /** the blacklisted id this JAR matched — the mod's declared mod id */
  mod_id: string;
  jar_name: string;
}

/** Source strings one enabled resource pack replaced, as a tally. */
export interface SourceOverride {
  /** pack file/directory name from the instance's resourcepacks/ */
  pack: string;
  keys: number;
}

/** One display string a mod builds in Java instead of a lang file. */
export interface HardcodedString {
  /** the English as the player sees it */
  text: string;
  /** declaring class inside the jar */
  class_name: string;
  /**
   * `component` — a serialized text component, the shape Mojang uses for
   * displayable text. `associated` — a multi-word label in a class that
   * already produced a component hit.
   */
  kind: "component" | "associated";
}

/** A mod whose display text no language file can reach. */
export interface HardcodedMod {
  mod_id: string;
  jar_name: string;
  strings: HardcodedString[];
}

export interface ScanResult {
  modpack_path: string;
  categories: ScanCategory[];
  /** null only when the engine could not run detection (defensive) */
  identity: PackIdentity | null;
  /** Mods excluded by the blacklist; absent on legacy scan records. */
  excluded_mods?: ExcludedMod[];
  /** Per-pack counts of source strings taken from enabled resource packs. */
  source_overrides?: SourceOverride[];
  /** Mods that hardcode display text in compiled code; absent on legacy records. */
  hardcoded_mods?: HardcodedMod[];
  migration?: {
    entry_count: number;
    char_count: number;
    resourcepack_asset_count: number;
  } | null;
}

export type GlossaryOrigin = "vanilla" | "extracted" | "manual" | "community";

export interface GlossaryTerm {
  source: string;
  target: string;
  origin: GlossaryOrigin;
  /**
   * Lang keys the term applies to, as dotted globs: every segment is a
   * literal or `*`, and a trailing `*` absorbs the remaining segments
   * (`effect.minecraft.wither`, `effect.*`, `subtitles.*.wither`). Empty
   * means every key. A scoped rule beats an unscoped one on the keys it
   * covers, which is what keeps "Wither" the boss 위더 under `entity.*`
   * and the status effect 시듦 under `effect.*`.
   */
  key_scope: string[];
}

export interface Glossary {
  source_lang: string;
  target_lang: string;
  terms: GlossaryTerm[];
}

export interface TmStats {
  entries: number;
  hits: number;
  last_sync_version: string | null;
  by_origin?: Record<string, number>;
}

/** POST /community/sync result: null side = nothing published on the web. */
export interface CommunitySyncResult {
  glossary: { version: string; terms: number; updated: boolean } | null;
  tm: { version: string; entries: number; updated: boolean } | null;
}

/**
 * Setup state of an `auth: "cli"` provider, as the engine reports it.
 *
 * - `cli-missing` — the CLI is not on this machine; install it.
 * - `logged-out`  — CLI present, no credential; sign in.
 * - `cli-ready`   — CLI present, but its login lives somewhere we cannot
 *                   read (Antigravity keeps it in the OS keyring), so only
 *                   a real request can prove the session either way.
 * - `ready`       — a request would succeed now.
 * - `unusable`    — signed in, but a request would still fail.
 */
export type CliProviderState =
  | "cli-missing"
  | "logged-out"
  | "cli-ready"
  | "ready"
  | "unusable";

export interface Provider {
  id: string;
  name: string;
  models: string[];
  /** Ready to use: an API key is present, or — for `auth: "cli"` — the
   *  user's coding CLI is logged in. */
  has_key: boolean;
  /** Present only for coding-CLI subscriptions (Claude Code, Codex, Gemini CLI). */
  auth?: "cli";
  /** Switched off engine-side: listed, never probed, cannot be selected. */
  disabled?: boolean;
  connected?: boolean;
  /** What the UI should branch on. `connected` is a bool kept for older
   *  clients and cannot separate "install it" from "sign in" from "signed
   *  in but broken" — three different next actions. */
  state?: CliProviderState;
  /** The CLI binary that owns this grant, e.g. "claude", "agy". */
  cli?: string | null;
  /** Whether that binary is on this machine. */
  cli_installed?: boolean;
  /** Command that signs the CLI in, resolved per machine — Gemini CLI
   *  reports "gemini" or "agy" depending on which one is installed. */
  login_hint?: string | null;
  /** Signed-in account, when the CLI's credential exposes one. */
  account?: string | null;
  /** Why an `auth: "cli"` provider is not usable, when it is not. */
  error?: string | null;
  /** gemini-cli only: which backend a request would take ("legacy-http" |
   *  "agy-cli"). Diagnostic — the two fail in completely different ways. */
  transport?: string | null;
  config_dir?: string | null;
  config_dir_source?: string | null;
}

/** POST /providers/models - live model listing with static-catalog fallback. */
export interface ProviderModels {
  provider: string;
  models: string[];
  /** `catalog`: the provider publishes no list, so the fixed lineup is the answer. */
  source: "live" | "static" | "catalog";
  error: string | null;
}

export interface ProviderTestResult {
  ok: boolean;
  error: string | null;
}

export interface PipelineStats {
  total_files: number;
  total_entries: number;
  translated_entries: number;
  failed_entries: number;
  tm_hits: number;
  /** Exact A/B/C matches reused from a user-supplied previous translation. */
  migration_hits?: number;
  skipped_entries: number;
  prompt_tokens: number;
  completion_tokens: number;
  /** cumulative prompt tokens served from the provider cache */
  cached_tokens?: number;
  duration_seconds: number;
  coverage_percent: number;
  quality_score: number;
  /** translated-entry counts per content bucket (quests, guidebook, ...) */
  categories?: Record<string, number>;
  /**
   * Translated files a mod jar's own `data/` tree swallowed. Patchouli
   * reads `book.json` straight out of the jar, so no resource pack or data
   * pack can carry our translation. NOT hardcoded text: that is reported
   * at scan time and was never translatable in the first place.
   */
  undeliverable_jar_files?: number;
  undeliverable_jar_entries?: number;
  undeliverable_jar_mods?: string[];
}

/**
 * Target speech level (말투) forced into the run's style directives.
 * `auto` keeps whatever per-surface register the compiled prompt already
 * prescribes; the others hold ONE register across the whole pack.
 *
 * Korean-only by construction: the engine renders this block solely for a
 * target locale starting `ko`, so on any other target the value is inert.
 */
export type SpeechLevel = "auto" | "polite" | "banmal" | "hage";

/**
 * Preference for terms with no established target-language name, where a
 * meaning translation and a transliteration (음차) both read acceptably.
 * `auto` keeps today's split. Applies to EVERY target language — the engine
 * renders this block ungated, phrased against "the target language".
 */
export type TermStyle = "auto" | "translate" | "transliterate";

/** Params for POST /jobs {type: "translate"} - engine PipelineConfig surface. */
export interface TranslateParams {
  session_id?: string;
  /** Scan the run came from; lets the engine persist its payload on the
   *  translate session so reopening can replay the scan screen. */
  scan_job_id?: string;
  modpack_path: string;
  output_dir?: string;
  source_locale?: string;
  target_locale?: string;
  model?: string;
  api_key?: string;
  api_base?: string;
  temperature?: number;
  batch_size?: number;
  max_concurrent?: number;
  file_workers?: number;
  max_refine?: number;
  /** litellm reasoning_effort: thinking effort for reasoning-capable models */
  reasoning_effort?: "low" | "medium" | "high";
  /** Forced speech level (말투). Korean targets only; see `SpeechLevel`. */
  speech_level?: SpeechLevel;
  /** Rendering preference for un-named terms; every target language. */
  term_style?: TermStyle;
  use_tm?: boolean;
  use_vanilla_glossary?: boolean;
  extract_glossary?: boolean;
  /** Maximum mined glossary candidates; null means unlimited. */
  glossary_max_terms?: number | null;
  include_categories?: string[];
  /**
   * Mod ids never translated (library/optimization/tooling mods). A
   * different axis from `include_categories`: per-mod, not per-category, and
   * applied while scanning so the excluded mods' entries are never
   * extracted. Omitted or empty scans every mod.
   */
  mod_blacklist?: string[];
  /**
   * Seed a hand-translation session. The run makes no provider call and needs
   * no `api_key`; every entry it would otherwise translate is left
   * untranslated with status `pending`. Unlike a source-text export this does
   * NOT disable the LLM-free helper stages — TM, glossaries, mod-translation
   * harvest and the sibling graph all still run, because only glossary
   * curation needs a model.
   */
  manual_seed?: boolean;
  /**
   * Adopt an earlier job's human translations (`origin === "human"`) as
   * already-settled, so an automatic run over a partially hand-translated
   * pack never re-translates and never overwrites the human work.
   */
  seed_from_job_id?: string;
  previous_modpack_path?: string;
  previous_resourcepack_path?: string;
  previous_overrides_path?: string;
}

/**
 * Params for POST /jobs {type: "translate"} that continue an interrupted
 * session from its engine-side checkpoint. Everything that shapes the output
 * comes from the checkpoint; only connection settings are re-sent, because
 * the api_key is never written to disk.
 */
export interface ResumeTranslateParams {
  session_id: string;
  resume: true;
  api_key?: string;
  api_base?: string;
  /** Only to move a run off a provider that refused it or was switched off. */
  model?: string;
}

/** GET /checkpoints — a translate session that can be resumed. */
export interface CheckpointSummary {
  session_id: string;
  modpack_path: string;
  modpack_name: string;
  source_locale: string;
  target_locale: string;
  model: string;
  created_at: string | null;
  updated_at: string | null;
  /** `running`: live in this engine process — reattach, never resume. */
  state: "running" | "interrupted" | "cancelled" | "failed";
  /** Why an interrupted run stopped; null when the process died first. */
  reason: "quota" | "auth" | "paused" | "shutdown" | null;
  error: string | null;
  /** Entries a resume will not send to the provider again. */
  translated_entries: number;
  /** Entries needing a translation; null until a run finished preparing. */
  total_entries: number | null;
}

export interface ScanParams {
  modpack_path: string;
  source_locale?: string;
  target_locale?: string;
  /** Mod ids to leave out of the scan; omitted or empty scans every mod. */
  mod_blacklist?: string[];
  previous_modpack_path?: string;
  previous_resourcepack_path?: string;
  previous_overrides_path?: string;
}

/** Params for POST /jobs {type: "export"} - archives a translate job's trees. */
export interface ExportParams {
  translate_job_id: string;
  output_zip?: string;
}

/**
 * Params for POST /jobs {type: "export", source_text: true} - the W3
 * "export as source text" run. The engine extracts the pack and drives the
 * same pipeline and output generator as a translated run, settling every
 * entry to its own source string, so the archives carry the identical file
 * structure with the untranslated text. No translate job, model, or API
 * key is involved.
 */
export interface SourceTextExportParams {
  source_text: true;
  modpack_path: string;
  source_locale?: string;
  target_locale?: string;
  /** Scan categories to include (omitted = every category). */
  include_categories?: string[];
  /** Mod ids to leave out of the run (omitted or empty = every mod). */
  mod_blacklist?: string[];
  output_dir?: string;
  output_zip?: string;
}

/** Params for POST /jobs {type: "upload"} - engine upload job surface. */
export interface UploadParams {
  translate_job_id: string;
  modpack_name: string;
  modpack_version?: string;
  /** CurseForge modpack id, forwarded to the web pack registration */
  curseforge_id?: number;
  description?: string;
  changelog?: string;
  web_url?: string;
  api_token?: string;
  share_mods?: boolean;
}

export interface ModPackUploadResult {
  shared: number;
  skipped: number;
  failed: number;
  skipped_reason?: "login_required" | "disabled" | "unresolved";
  bundle_url?: string;
}

/** Background CurseForge identification for a scanned modpack's jars. */
export interface ModSourcesResult {
  status: "pending" | "running" | "done" | "failed";
  total: number;
  resolved: number;
  unresolved: number;
  sources: Array<{
    jar_name: string;
    project_id: number | null;
    file_id: number | null;
    source: "instance" | "prism" | "fingerprint" | "unresolved";
  }>;
}

/* ---- WebSocket event frames (/jobs/{id}/events) ---- */

export interface JobEventMeta {
  /** Monotonic within one job; used as the reconnect cursor. */
  seq?: number;
  /** Engine wall-clock timestamp in epoch milliseconds. */
  emitted_at?: number;
}

export interface ProgressFrame extends JobEventMeta {
  type: "progress";
  stage: string;
  /** translate/export style */
  done?: number;
  total?: number;
  file?: string;
  /** scan style */
  current?: number;
  message?: string;
}

export interface EntryFailedFrame extends JobEventMeta {
  type: "entry_failed";
  key: string;
  /** Owning file; keys are unique per file, not across the pack. */
  file?: string;
  errors: string[];
}

export interface EntryDoneFrame extends JobEventMeta {
  type: "entry_done";
  key: string;
  source: string;
  translated: string;
}

export interface BatchStartedFrame extends JobEventMeta {
  type: "batch_started";
  request_id: number;
  file: string;
  key: string;
  entries: number;
}

export interface BatchFinishedFrame extends JobEventMeta {
  type: "batch_finished";
  request_id: number;
}

export interface TokensFrame extends JobEventMeta {
  type: "tokens";
  prompt_tokens: number;
  completion_tokens: number;
  /** cumulative prompt tokens served from the provider cache */
  cached_tokens?: number;
}

export interface GlossaryExtractedFrame extends JobEventMeta {
  type: "glossary_extracted";
  new_terms: number;
}

export interface GlossaryProgressFrame extends JobEventMeta {
  type: "glossary_progress";
  /** extraction chunks completed */
  done: number;
  total: number;
  /** one-based chunk number for retry/skip diagnostics */
  chunk?: number;
  /** terms extracted so far */
  new_terms: number;
  /** present when a schema-invalid LLM response failed this attempt */
  attempt?: number;
  error?: string;
  /** chunk gave up after exhausting retries */
  skipped?: boolean;
}

/**
 * A resumed run reusing its checkpoint: `checkpointed` arrives first (the
 * file loaded, before the rescan), then `reused`/`remaining` once every file
 * is prepared and the saved translations were matched.
 */
export interface ResumeFrame extends JobEventMeta {
  type: "resume";
  checkpointed: number;
  reused?: number;
  remaining?: number;
}

export interface TerminalFrame extends JobEventMeta {
  type: "done" | "failed" | "cancelled" | "interrupted";
  status: JobStatus;
  error?: string;
  /**
   * Translate jobs that call a provider: the session's checkpoint is kept,
   * so "이어서 번역" can continue it. True for every outcome except done.
   */
  resumable?: boolean;
  /** interrupted: `quota` (capacity spent), `auth` (login refused), `paused` or `shutdown`. */
  reason?: "quota" | "auth" | "paused" | "shutdown";
  /** interrupted: the provider's own error excerpt, for the live log. */
  detail?: string;
  stats?: PipelineStats;
  /** export terminal payload — each zip is null when its tree is empty */
  zip_path?: string | null;
  overrides_zip_path?: string | null;
  bilingual_zip_path?: string | null;
  bilingual_overrides_zip_path?: string | null;
  file_count?: number;
  /** upload terminal payload */
  pack_id?: string;
  url?: string;
  modpack_slug?: string;
  bundle_url?: string;
  mod_packs?: ModPackUploadResult;
}

export type JobEventFrame =
  | ProgressFrame
  | EntryFailedFrame
  | EntryDoneFrame
  | TokensFrame
  | BatchStartedFrame
  | BatchFinishedFrame
  | GlossaryProgressFrame
  | GlossaryExtractedFrame
  | ResumeFrame
  | TerminalFrame;

/** POST /modpacks/import — an archive opened as a folder to scan. */
export interface ImportedModpack {
  modpack_path: string;
  name: string;
  /** False for launcher exports, which list mods instead of shipping JARs. */
  has_mods: boolean;
}
