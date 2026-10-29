"""Translation pipeline orchestrator.

scan -> glossary/TM lookup -> [TM hit: reuse] -> protect -> DSPy translate
-> restore -> validate -> [fail: refined inside module / surfaced] -> write
translated files.

Placeholder protect/restore lives HERE (outside the LLM).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

import dspy
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .. import batching
from ..community import (
    default_glossary_store_dir,
    load_user_glossary_terms,
    merge_extracted_terms,
)
from ..dspy_modules import GlossaryExtractor, build_lm, load_translator
from ..dspy_modules.lm import (
    cli_auth_failure,
    context_window_filled,
    is_transport_error,
    provider_limit,
    token_usage,
)
from ..dspy_modules.signatures import (
    SpeechLevel,
    TermStyle,
    render_style_directives,
)
from ..glossary.pair_harvester import (
    TranslatedTerm,
    build_term_rules,
    collect_translated_terms,
    is_untranslated_copy,
)
from ..glossary.term_miner import TermCandidate, mine_candidates
from ..graph import SIBLING_CONTEXT_HEADER, TranslationGraph, is_name_entry
from ..handlers.base import ContentHandler, create_default_registry
from ..migration import MigrationCatalog, build_migration_catalog, logical_file_id
from ..models import Glossary, TermRule, ValidationSeverity
from ..models.glossary_filter import GlossaryFilter
from ..output import (
    DEFAULT_PACK_FORMAT,
    FileOutput,
    GenerationResult,
    OutputConfig,
    OutputGenerator,
    pack_format_for_minecraft_version,
)
from ..placeholder import PlaceholderError, PlaceholderProtector, ProtectedText
from ..scanner import ModpackScanner, ScanResult
from ..scanner.pack_identity import detect_pack_identity
from ..tm import LocalTM
from ..validator import TranslationValidator
from .checkpoint import source_digest

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

    from ..models import LanguageFilePair
    from .checkpoint import CheckpointState, TranslationCheckpoint

logger = logging.getLogger(__name__)

#: Every Nth freshly translated entry becomes an entry_done ticker frame.
ENTRY_TICKER_INTERVAL = 5
#: Ticker frames truncate source/translated text to this many characters.
TICKER_TEXT_LIMIT = 120

#: Provider-request admission starts here for a locally hosted runtime.
#: Ollama serves OLLAMA_NUM_PARALLEL (default 1) requests at a time and
#: queues the rest with no queue-wait timeout, so the hosted default of 15
#: buys no throughput there — it only inflates queue latency until it
#: crosses the request timeout. The limiter grows back toward
#: ``max_concurrent`` once the server proves it can take it.
OLLAMA_START_CONCURRENT = 2
#: Retries of the SAME batch after a transport failure before its entries
#: are failed. Deliberately small: a saturated server needs less load, and
#: a user-initiated "retry failed" is the right place for another attempt.
MAX_TRANSPORT_RETRIES = 2
#: First backoff after a transport failure; doubles per consecutive one.
TRANSPORT_BACKOFF_BASE = 2.0
TRANSPORT_BACKOFF_MAX = 60.0
#: How long a provider may refuse for capacity (429 / RESOURCE_EXHAUSTED)
#: with not one call succeeding before the run stops resumably. Longer than
#: a per-minute window, which reopens by itself; a spent daily quota never
#: does. Per batch, the retry budget above cannot measure this: with dozens
#: of batches queued behind one permit, a batch's three attempts were spread
#: over many minutes while every queued batch kept hitting the provider.
CAPACITY_GIVE_UP_SECONDS = 120.0

#: Shown when a run stops because the provider account is out of capacity.
#: Korean-first like the engine's other surfaced messages; the desktop renders
#: its own localized copy from the interruption reason and falls back to this.
AUTH_INTERRUPTION_MESSAGE = (
    "AI 제공자가 인증을 거부해 번역을 일시 중지했습니다. 다시 로그인하거나 "
    "다른 제공자를 고른 뒤 '이어서 번역'으로 계속할 수 있습니다."
)
QUOTA_INTERRUPTION_MESSAGE = (
    "AI 제공자의 사용량 한도(할당량·크레딧)에 도달해 번역을 일시 중지했습니다. "
    "완료된 번역은 저장되었으니 한도가 초기화된 뒤 '이어서 번역'으로 "
    "계속할 수 있습니다."
)

_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9_./:-]+")
_DOTTED_VERSION_RE = re.compile(r"\d+(?:\.\d+)+")
_TEXT_PROTECTOR = PlaceholderProtector()  # stateless; safe to share for estimates
_CATEGORY_BUCKET_BY_FILE_TYPE = {
    "ftbquests": "quests",
    "the_vault_quest": "quests",
    "patchouli": "guidebook",
    "kubejs": "scripts",
    "mod": "lang",
    "resources": "lang",
    "resourcepacks": "lang",
    "datapacks": "lang",
    "config": "json",
}


def looks_like_identifier(text: str) -> bool:
    """Detect Patchouli/lang-key references that should not reach the LLM.

    Patchouli page text can itself be another language key. Translating that
    reference invents prose instead of preserving the lookup; dotted numeric
    versions have the same untranslatable shape and are intentionally included.
    """
    value = text.strip()
    if not value or _IDENTIFIER_RE.fullmatch(value) is None:
        return False
    if "." not in value and ":" not in value:
        return False
    has_letter = any(
        "A" <= character <= "Z" or "a" <= character <= "z"
        for character in value
    )
    if not has_letter and _DOTTED_VERSION_RE.fullmatch(value) is None:
        return False
    segments = re.split(r"[./:]+", value)
    return len(segments) >= 2 and all(segments)


def is_translatable_text(
    text: str,
    protected: ProtectedText | None = None,
) -> bool:
    """Mirror the pipeline's pre-LLM skip rules for scan-time estimates."""
    prepared = protected or _TEXT_PROTECTOR.protect(text)
    return bool(text.strip()) and not (
        _TEXT_PROTECTOR.is_only_placeholders(prepared) or looks_like_identifier(text)
    )


class RetranslateError(Exception):
    """Single-entry retranslation produced no acceptable output."""


class EntryStatus(str, Enum):
    PASSED = "passed"
    WARNING = "warning"
    FAILED = "failed"
    TM_HIT = "tm_hit"
    MIGRATED = "migrated"
    SKIPPED = "skipped"
    MODIFIED = "modified"
    #: Seeded by a manual-seed run and not yet touched by a human. Deliberately
    #: absent from _FRESH_STATUSES: an untranslated entry must never reach the
    #: resource pack, so this is the one status that carries no output.
    PENDING = "pending"


class EntryResult(BaseModel):
    """Per-entry outcome, the unit surfaced to the review screen."""

    key: str
    file: str
    source_text: str
    translated_text: str | None = None
    status: EntryStatus = EntryStatus.FAILED
    errors: list[str] = Field(default_factory=list)


class PipelineStats(BaseModel):
    total_files: int = 0
    total_entries: int = 0
    translated_entries: int = 0
    failed_entries: int = 0
    tm_hits: int = 0
    migration_hits: int = 0
    skipped_entries: int = 0
    categories: dict[str, int] = Field(default_factory=dict)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    duration_seconds: float = 0.0
    coverage_percent: float = 0.0
    quality_score: float = 0.0
    #: Translated files a mod JAR's own data/ tree swallowed: Patchouli
    #: reads book.json straight out of the JAR, so no resource pack or
    #: data pack can carry our translation. NOT hardcoded text — that is
    #: reported at scan time and was never translatable.
    undeliverable_jar_files: int = 0
    undeliverable_jar_entries: int = 0
    undeliverable_jar_mods: list[str] = Field(default_factory=list)

    def finalize(self) -> None:
        done = self.translated_entries + self.tm_hits + self.migration_hits
        translatable = max(self.total_entries - self.skipped_entries, 1)
        self.coverage_percent = round(100.0 * done / translatable, 2)
        checked = done + self.failed_entries
        self.quality_score = round(done / checked, 4) if checked else 0.0


class PipelineConfig(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    modpack_path: Path
    output_dir: Path | None = None
    source_locale: str = "en_us"
    target_locale: str = "ko_kr"
    #: pack.mcmeta pack_format for the generated resource pack.
    pack_format: int = DEFAULT_PACK_FORMAT

    #: Emit every entry's own source text instead of a translation. The run
    #: makes no provider call at all (the W3 "export as source text" path):
    #: extraction, routing and output generation are untouched, so the
    #: generated trees hold the same files and keys as a translated run,
    #: carrying the untranslated strings as their values.
    source_text_only: bool = False

    #: Also emit the bilingual display-name variant (resourcepack/overrides
    #: rendered with the original in parentheses on short display names).
    bilingual_names: bool = True

    model: str = "openai/gpt-5.6-luna"
    api_key: str | None = None
    api_base: str | None = None
    temperature: float = 0.3
    #: LiteLLM reasoning_effort passthrough for reasoning-capable providers;
    #: an explicit value overrides build_lm's Ollama auto-disable.
    reasoning_effort: str | None = None
    #: Target speech level (말투) written into the prompt's style
    #: directives. "auto" keeps the compiled instructions' own
    #: per-surface register; "polite"/"banmal"/"hage" force one register
    #: across the whole pack. Korean targets only.
    speech_level: SpeechLevel = "auto"
    #: Preference for terms with no established target-language name,
    #: where both a meaning translation and a transliteration (음차) read
    #: acceptably. "auto" keeps today's behavior.
    term_style: TermStyle = "auto"

    batch_size: int = batching.DEFAULT_BATCH_SIZE
    max_batch_chars: int = batching.DEFAULT_MAX_BATCH_CHARS
    max_concurrent: int = Field(default=15, ge=1)
    #: Files prepared concurrently. None derives max_concurrent so enough
    #: batches exist to fill every LLM slot; a small fixed value starves
    #: the request semaphore when the pack has many small files.
    file_workers: int | None = Field(default=None, ge=1)
    max_refine: int = 2

    use_tm: bool = True
    tm_db_path: Path | None = None
    #: Build the in-memory entry relationship graph: name-defining entries
    #: translate in a first wave, their settled translations merge into the
    #: run glossary as bindings, and already-translated sibling entries are
    #: injected as batch context.
    use_translation_graph: bool = True
    #: Include origin="vanilla" rows synced from the server into the user
    #: glossary store (the web-published vanilla bundle). No local vanilla
    #: glossary is bundled anymore; disabling this only drops those rows.
    use_vanilla_glossary: bool = True
    #: Merge the engine's user glossary store (hub manual terms + synced
    #: vanilla/community terms) into every run. Store dir defaults to the
    #: shared platformdirs location used by the sidecar server.
    use_user_glossary: bool = True
    glossary_store_dir: Path | None = None
    #: Harvest terminology from lang files the pack's mods already ship in
    #: the configured target locale alongside the source locale (e.g.
    #: en_us + ko_kr side by side). Deterministic and LLM-free; only
    #: unanimous source→target pairs become rules, and the user glossary
    #: store always wins on overlap.
    use_mod_translations: bool = True
    extract_glossary: bool = False
    #: Cap on mined term candidates sent to the curation LLM. Candidates
    #: come from a deterministic whole-corpus scan (term_miner), not from
    #: sampling, so coverage does not depend on file order.
    glossary_max_terms: int | None = Field(default=3000, ge=1)
    #: Candidates per curation LLM call. Small chunks bound the blast
    #: radius of one malformed response and make progress observable.
    glossary_chunk_size: int = 50
    #: Extra attempts per chunk after a schema-invalid LLM response; the
    #: validation error is fed back verbatim so the model can fix it.
    glossary_max_retries: int = 2
    artifacts_dir: Path | None = None

    #: Scan categories to translate (None = all). Names follow the scan
    #: payload convention: TranslationFile.category or file_type fallback.
    include_categories: list[str] | None = None

    #: Mods excluded from translation (None = none). A per-mod axis,
    #: independent of include_categories' category axis.
    mod_blacklist: list[str] | None = None

    #: Optional A/B inputs for run-scoped previous-version migration.  A is a
    #: previous original modpack folder or CurseForge export ZIP.  B can have
    #: either or both of the normal Moru output channels.
    previous_modpack_path: Path | None = None
    previous_resourcepack_path: Path | None = None
    previous_overrides_path: Path | None = None

    #: Seed a hand-translation session. Like ``source_text_only`` the run makes
    #: no provider call and needs no api_key, but every entry it would
    #: otherwise translate is left UNTRANSLATED (``EntryStatus.PENDING``) so
    #: the manual surface can tell "not yet touched" from "translated".
    #:
    #: Unlike a source-text run it deliberately keeps the LLM-FREE helper
    #: stages on: TM, the vanilla and user glossaries, mod-translation harvest,
    #: previous-version migration and the sibling graph. Only glossary
    #: curation reaches a provider, so only that is switched off. Those stages
    #: are exactly the help a hand translator wants, and turning them off
    #: "for symmetry" with source_text_only would strip the feature for no
    #: gain.
    manual_seed: bool = False

    #: Adopt an earlier job's human translations as already-settled, so an
    #: automatic run over a partially hand-translated pack neither re-sends
    #: nor overwrites the human's work. Resolved by the caller into
    #: ``human_translations``; this field only records provenance for the
    #: session payload.
    seed_from_job_id: str | None = None

    @model_validator(mode="after")
    def _output_dir_cannot_swallow_the_pack(self) -> PipelineConfig:
        """Refuse an output folder whose regenerated trees hold the pack.

        Output generation deletes ``<output>/resourcepack`` and
        ``<output>/overrides`` before writing them. Pointed at the pack
        root itself, that is the pack's own ``overrides/`` (every
        CurseForge export has one) gone without a backup; pointed above
        the pack, the pack may sit inside one of those trees.
        """
        if self.output_dir is None:
            return self
        from ..output.generator import OVERRIDES_DIRNAME, RESOURCEPACK_DIRNAME

        out = self.output_dir.expanduser().resolve()
        pack = self.modpack_path.expanduser().resolve()
        if out == pack:
            raise ValueError("output_dir must not be the modpack folder itself")
        for name in (RESOURCEPACK_DIRNAME, OVERRIDES_DIRNAME):
            if pack.is_relative_to(out / name):
                raise ValueError(
                    f"output_dir would replace {out / name}, which contains the modpack"
                )
        return self

    @model_validator(mode="after")
    def _disable_provider_stages(self) -> PipelineConfig:
        """A run that must not need a model or an API key: enforce it here.

        Switching the provider-bound stages off here instead of at each
        call site means no caller can assemble a source-text config that
        still reaches an LLM (glossary curation) or writes a real
        translation over the source text (TM, mod-translation harvest,
        user glossary, name-binding graph waves).

        It also settles the two output-shape flags' interaction: a
        source-text run's values ARE the source, so every entry would trip
        the bilingual transform's ``translation == source`` guard and the
        variant tree would come out identical to the plain one. Correct
        output, wasted work — so disable it here rather than at one call
        site, and the rule holds for every future caller too.

        A manual seed shares the "no provider" requirement but NOT the rest.
        Only ``extract_glossary`` reaches a model; TM, both glossaries, the
        mod-translation harvest, migration and the sibling graph are pure
        local work and are precisely the aids a hand translator needs, so
        they stay on. ``bilingual_names`` also stays: it is an output-shape
        choice about the eventual export, orthogonal to who produced the
        translation, and a hand-translated pack may well want it.
        """
        if self.source_text_only:
            self.use_tm = False
            self.use_translation_graph = False
            self.use_vanilla_glossary = False
            self.use_user_glossary = False
            self.use_mod_translations = False
            self.extract_glossary = False
            self.bilingual_names = False
        elif self.manual_seed:
            self.extract_glossary = False
        return self


@dataclass(frozen=True, slots=True)
class ReviewEdit:
    """A translation the user settled on an earlier, interrupted partial
    result of the same session: a hand commit or an AI retranslation.

    A resumed run adopts it as MODIFIED — exactly what the entry was when the
    run stopped — so it outranks the pack, the TM, migration and the
    checkpoint's machine text, never reaches the model again, and ships like
    any review edit.
    """

    text: str
    #: Source text the edit was made against: it is reused only while that is
    #: unchanged. None for a hand commit, adopted by provenance like
    #: ``human_translations`` — its journal ``src_sha`` lets the review screen
    #: flag it if the pack has since changed the string.
    source_text: str | None = None


class PipelineInterruption(BaseModel):
    """Why a run stopped early in a resumable state rather than failing.

    Partial results are kept exactly like a cancellation, and the run's
    checkpoint stays on disk so the session can continue later without
    re-sending what was already translated.
    """

    #: ``quota``: the provider account ran out of capacity (credit, daily
    #: quota, or throttling that outlasted the retry/backoff budget).
    reason: str
    #: User-facing explanation (Korean-first).
    message: str
    #: Provider error excerpt, for logs and the desktop's live log.
    detail: str = ""


class PipelineResult(BaseModel):
    """Mutable across stages: retry mutates in place."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    config: PipelineConfig
    scan_result: ScanResult | None = None
    entries: list[EntryResult] = Field(default_factory=list)
    output_files: list[Path] = Field(default_factory=list)
    stats: PipelineStats = Field(default_factory=PipelineStats)
    artifact_id: str | None = None
    #: Effective glossary of the full run. retry_failed/retranslate_entry
    #: reuse it so post-run fixes see the exact same rules - harvested mod
    #: terms and LLM-curated terms are run-scoped and would be lost by a
    #: bare rebuild.
    glossary: Glossary | None = None
    #: Run-scoped A/B index. It also owns the preserved resource-pack asset
    #: directory needed when review edits regenerate the output trees.
    migration: MigrationCatalog | None = None
    #: Resource-pack namespace per absolute source path, as a FALLBACK for a
    #: session restored from disk. ``scan_result`` is not rebuilt on restore, so
    #: without this ``write_outputs`` would find an empty namespace map and
    #: handler-extracted files with no ``assets/`` segment in their path would
    #: silently land under ``minecraft``. Populated from ``scan_result`` when
    #: one exists, and persisted alongside the entries.
    namespaces: dict[str, str] = Field(default_factory=dict)
    #: Set when the run stopped early but resumably (see PipelineInterruption).
    interruption: PipelineInterruption | None = None

    @property
    def failed(self) -> list[EntryResult]:
        return [e for e in self.entries if e.status == EntryStatus.FAILED]


def category_stats(result: PipelineResult) -> dict[str, int]:
    """Count non-failed entries by their web-facing file-type bucket."""
    if result.scan_result is None:
        return {}

    modpack_path = result.config.modpack_path.resolve()
    file_type_by_path: dict[str, str] = {}
    for translation_file in result.scan_result.translation_files:
        input_path = Path(translation_file.input_path)
        try:
            relative = input_path.resolve().relative_to(modpack_path).as_posix()
        except ValueError:
            relative = input_path.as_posix()
        file_type_by_path[relative] = translation_file.file_type

    categories: dict[str, int] = {}
    for entry in result.entries:
        if entry.status is EntryStatus.FAILED:
            continue
        file_type = file_type_by_path.get(entry.file)
        if file_type is None:
            continue
        category = _CATEGORY_BUCKET_BY_FILE_TYPE.get(file_type, file_type)
        categories[category] = categories.get(category, 0) + 1
    return categories


@dataclass
class _PreparedFile:
    """Per-file state carried across the prepare/translate/finalize phases."""

    pair: LanguageFilePair
    rel: str
    handler: ContentHandler
    source_data: dict[str, str]
    existing_keys: set[str]
    work_total: int
    #: Output values settled so far: existing + identity skips + TM hits,
    #: later extended with validated fresh translations.
    final: dict[str, str] = field(default_factory=dict)
    file_entries: list[EntryResult] = field(default_factory=list)
    protected_map: dict[str, ProtectedText] = field(default_factory=dict)
    #: Keys still awaiting an LLM outcome (protected text). Waves drain it.
    to_translate: dict[str, str] = field(default_factory=dict)
    #: Real pre-settled translations (existing + TM hits) for graph builds.
    known_translations: dict[str, str] = field(default_factory=dict)
    #: Fresh restored translations accumulated across waves.
    translated_raw: dict[str, str] = field(default_factory=dict)
    #: Keys whose value came from the run-scoped migration catalog, plus
    #: keys that borrowed such a value: they never reach the global TM.
    migrated_keys: set[str] = field(default_factory=set)
    #: Keys whose one-shot unchanged-copy retry an earlier, interrupted run
    #: of this session already spent (replayed from its checkpoint).
    retried_keys: set[str] = field(default_factory=set)
    was_cancelled: bool = False


class _AdaptiveLimiter:
    """Provider-request admission that sheds concurrency under overload.

    A plain semaphore cannot shrink, and a fixed limit is precisely what
    turns one slow provider into a congestion collapse: every request that
    times out becomes more concurrent load, which makes the next timeout
    more likely. This starts at ``start`` permits, halves toward ``floor``
    whenever the caller reports a transport failure, and earns one permit
    back per ``_GROW_AFTER`` consecutive successes until ``maximum``.

    Growth matters as much as shrinking: it means a conservative start for
    a local runtime costs nothing on a server that can actually keep up.
    """

    #: Consecutive successes that earn one permit back.
    _GROW_AFTER = 8

    def __init__(self, start: int, maximum: int, *, floor: int = 1) -> None:
        self._maximum = max(1, maximum)
        self._floor = max(1, min(floor, self._maximum))
        self._limit = max(self._floor, min(start, self._maximum))
        self._active = 0
        self._successes = 0
        self._failures = 0
        #: Monotonic time of the first transport failure since the last
        #: success; None while calls are getting through.
        self._failing_since: float | None = None
        self._cond = asyncio.Condition()

    @property
    def limit(self) -> int:
        """Permits currently admitted concurrently."""
        return self._limit

    @property
    def failing_for(self) -> float:
        """Seconds every call has failed in transport, 0 after a success."""
        if self._failing_since is None:
            return 0.0
        return time.monotonic() - self._failing_since

    async def __aenter__(self) -> None:
        async with self._cond:
            while self._active >= self._limit:
                await self._cond.wait()
            self._active += 1

    async def __aexit__(self, *_exc: object) -> None:
        async with self._cond:
            self._active -= 1
            self._cond.notify()

    async def record_success(self) -> None:
        async with self._cond:
            self._failures = 0
            self._failing_since = None
            if self._limit >= self._maximum:
                return
            self._successes += 1
            if self._successes >= self._GROW_AFTER:
                self._successes = 0
                self._limit += 1
                # A raised ceiling has to wake a waiter itself: __aexit__
                # only ever notifies for a permit it released.
                self._cond.notify()

    async def record_transport_failure(self) -> float:
        """Halve admission; return the seconds the caller must back off."""
        async with self._cond:
            self._successes = 0
            self._failures += 1
            if self._failing_since is None:
                self._failing_since = time.monotonic()
            self._limit = max(self._floor, self._limit // 2)
            return min(
                TRANSPORT_BACKOFF_BASE * 2 ** (self._failures - 1),
                TRANSPORT_BACKOFF_MAX,
            )


class TranslationPipeline:
    """Orchestrates one translation session over a scanned modpack."""

    def __init__(
        self,
        config: PipelineConfig,
        *,
        on_event: Callable[[str, dict[str, object]], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
        lm: object | None = None,
        human_translations: dict[str, dict[str, str]] | None = None,
        checkpoint: TranslationCheckpoint | None = None,
        resume_from: CheckpointState | None = None,
        review_edits: Mapping[str, Mapping[str, ReviewEdit]] | None = None,
    ) -> None:
        self.config = config
        self.on_event = on_event
        self.cancel_check = cancel_check
        #: rel file path -> {entry key: text} committed by a human in an
        #: earlier session (``seed_from_job_id``). Adopted as already-settled
        #: by provenance, so these keys never reach the model and can never be
        #: overwritten by it. Resolved by the caller, which owns the journal.
        self.human_translations = human_translations or {}
        #: Durable per-batch record of this run's fresh translations, so an
        #: interruption of any kind loses at most the batches in flight.
        self.checkpoint = checkpoint
        #: An earlier, interrupted run of the same session: its translations
        #: settle matching entries at prepare time instead of the model.
        self.resume_from = resume_from
        #: Entries settled from ``resume_from`` (diagnostics and tests).
        self.resumed_entries = 0
        #: rel file path -> {entry key: edit} the user made on the partial
        #: result this run resumes. Resolved by the caller, which owns the
        #: session file and the edit journal.
        self.review_edits = review_edits or {}
        #: Set when the provider ran out of capacity mid-run. It stops every
        #: further dispatch, like a cancel, and marks the result resumable.
        self._interruption: PipelineInterruption | None = None
        #: Quota exhaustion only interrupts a whole run(). The review paths
        #: (retry_failed, retranslate_entry) keep reporting per-entry errors.
        self._interruptible = False
        self.registry = create_default_registry()
        lm_extra: dict[str, object] = {}
        if config.reasoning_effort is not None:
            lm_extra["reasoning_effort"] = config.reasoning_effort
        # `lm` injection is a test/embedding seam; production builds from
        # config. Neither a source-text run nor a manual seed calls a provider,
        # so both build neither an LM (no api_key required) nor a translator
        # (no compiled artifact to load); token_usage() reports zeros for a
        # None LM.
        if config.source_text_only or config.manual_seed:
            self.lm = lm
            self.translator = None
            self.artifact_id = None
        else:
            self.lm = lm if lm is not None else build_lm(
                config.model,
                api_key=config.api_key,
                api_base=config.api_base,
                temperature=config.temperature,
                **lm_extra,
            )
            self.translator, self.artifact_id = load_translator(
                config.model,
                config.source_locale,
                config.target_locale,
                max_refine=config.max_refine,
                base_dir=config.artifacts_dir,
            )
        self.tm = LocalTM(config.tm_db_path) if config.use_tm else None
        # A locally hosted runtime starts conservatively: Ollama serves
        # OLLAMA_NUM_PARALLEL (default 1) requests at a time, so opening 15
        # in parallel buys nothing and only queues them until they cross
        # the request timeout. Hosted providers start at the configured
        # value, so their behaviour is unchanged.
        self._llm_limiter = _AdaptiveLimiter(
            OLLAMA_START_CONCURRENT
            if config.model.startswith("ollama")
            else config.max_concurrent,
            config.max_concurrent,
        )
        self._file_semaphore = asyncio.Semaphore(
            config.file_workers
            if config.file_workers is not None
            else config.max_concurrent
        )
        #: Monotonic count of freshly translated entries, drives the sampled
        #: entry_done ticker frames (every ENTRY_TICKER_INTERVAL-th entry).
        self._entry_counter = 0
        #: Monotonic provider-request id for live concurrent-work events.
        self._request_counter = 0
        #: One-shot latch for the context-overflow warning: ollama truncates
        #: an oversized prompt silently, so this warning is the only signal
        #: a user gets that the window cannot hold the compiled prompt.
        self._context_warned = False
        #: Entry relationship graph of the current run (built after the
        #: prepare phase; rebuilt from entries for post-run paths).
        self._graph: TranslationGraph | None = None
        #: Prompt style directives for this run — rendered once from the
        #: signature-owned rule text, identical for every batch.
        self._style_directives = render_style_directives(
            config.target_locale,
            config.speech_level,
            config.term_style,
        )
        #: Built only when previous-version inputs are supplied; the default
        #: path pays no scan/index cost and follows the existing pipeline.
        self.migration: MigrationCatalog | None = None

    @property
    def graph(self) -> TranslationGraph | None:
        """The live run's relationship graph (None before prepare or when
        use_translation_graph is off). Read-only view for the server's
        graph endpoint; snapshots must stay synchronous (same-loop
        cooperative access, see TranslationGraph.snapshot)."""
        return self._graph

    # -- events ------------------------------------------------------------

    def _emit(self, event: str, payload: dict[str, object]) -> None:
        if self.on_event is not None:
            try:
                self.on_event(event, payload)
            except Exception:  # noqa: BLE001 — listener bugs must not kill jobs
                logger.exception("Event listener failed for %s", event)

    def _check_cancelled(self) -> None:
        if self._interruption is not None or (
            self.cancel_check is not None and self.cancel_check()
        ):
            raise asyncio.CancelledError("pipeline cancelled")

    def _interrupt_for_auth(self, exc: BaseException) -> NoReturn:
        """Stop the run resumably: the provider refused the credential.

        Same unwinding as :meth:`_interrupt_for_quota`. Without it a
        logged-out CLI provider read as a transport failure, and a run whose
        every call is refused backed off batch after batch for hours.
        """
        if self._interruption is None:
            detail = str(exc)[:500]
            self._interruption = PipelineInterruption(
                reason="auth", message=AUTH_INTERRUPTION_MESSAGE, detail=detail
            )
            logger.warning("Provider refused the credential; interrupting: %s", detail)
        raise asyncio.CancelledError("provider credential refused")

    def _interrupt_for_quota(self, exc: BaseException) -> NoReturn:
        """Stop the whole run resumably: the provider has no capacity left.

        Continuing would only turn every remaining batch into a failed entry
        (and, for split batches, multiply the doomed requests); stopping
        keeps the completed batches, which the checkpoint already holds.
        Raised as a cancellation so every wave unwinds through the same
        partial-result path a user cancel takes.
        """
        if self._interruption is None:
            detail = str(exc)[:500]
            self._interruption = PipelineInterruption(
                reason="quota", message=QUOTA_INTERRUPTION_MESSAGE, detail=detail
            )
            logger.warning(
                "Provider capacity exhausted; interrupting the run resumably: %s",
                detail,
            )
        raise asyncio.CancelledError("provider capacity exhausted")

    # -- glossary ----------------------------------------------------------

    async def _build_glossary(
        self,
        pairs: list[LanguageFilePair],
        *,
        harvest_pairs: list[LanguageFilePair] | None = None,
    ) -> tuple[Glossary, str]:
        glossary = Glossary(
            locale_source=self.config.source_locale,
            locale_target=self.config.target_locale,
        )

        user_rules = self._load_user_glossary()
        if user_rules:
            glossary = glossary.merge_with(
                Glossary(
                    locale_source=self.config.source_locale,
                    locale_target=self.config.target_locale,
                    term_rules=user_rules,
                )
            )

        if self.config.use_mod_translations and harvest_pairs:
            harvested = await self._harvest_mod_terms(harvest_pairs, glossary)
            if harvested:
                glossary = glossary.merge_with(
                    Glossary(
                        locale_source=self.config.source_locale,
                        locale_target=self.config.target_locale,
                        term_rules=harvested,
                    )
                )

        if self.config.extract_glossary and pairs:
            corpus: dict[str, str] = {}
            for pair in pairs:
                handler = self.registry.get_handler(pair.source_path)
                if handler is None:
                    continue
                data = await handler.extract(pair.source_path)
                if pair.target_path is not None and pair.target_path.exists():
                    existing = await handler.extract(pair.target_path)
                    data = {
                        key: value
                        for key, value in data.items()
                        if is_untranslated_copy(value, existing.get(key, ""))
                    }
                for key, value in data.items():
                    corpus[f"{pair.source_path}:{key}"] = value
            candidates = mine_candidates(
                corpus,
                {
                    alias
                    for rule in glossary.term_rules
                    for alias in rule.aliases
                },
                max_terms=self.config.glossary_max_terms,
            )
            logger.info(
                "Glossary mining: %d candidates from %d entries",
                len(candidates),
                len(corpus),
            )
            if candidates:
                extractor = GlossaryExtractor()
                size = max(1, self.config.glossary_chunk_size)
                chunks = [
                    candidates[i : i + size]
                    for i in range(0, len(candidates), size)
                ]
                total = len(chunks)
                progress = {"done": 0, "new_terms": 0}
                chunk_rules: list[list[TermRule] | None] = [None] * total
                self._emit(
                    "glossary_progress",
                    {"done": 0, "total": total, "new_terms": 0},
                )
                work = iter(enumerate(chunks))

                async def curate_worker() -> None:
                    for index, chunk in work:
                        rules = await self._curate_glossary_chunk(
                            extractor,
                            glossary,
                            chunk,
                            index,
                            total,
                            progress,
                        )
                        chunk_rules[index] = rules
                        progress["done"] += 1
                        progress["new_terms"] += len(rules)
                        self._emit(
                            "glossary_progress",
                            {
                                "done": progress["done"],
                                "total": total,
                                "new_terms": progress["new_terms"],
                            },
                        )

                workers = [
                    asyncio.create_task(
                        curate_worker(), name=f"moru-glossary-{worker_index}"
                    )
                    for worker_index in range(
                        min(self.config.max_concurrent, total)
                    )
                ]
                try:
                    await asyncio.gather(*workers)
                except BaseException:
                    for worker in workers:
                        worker.cancel()
                    await asyncio.gather(*workers, return_exceptions=True)
                    raise

                extracted_rules = [
                    rule
                    for rules in chunk_rules
                    if rules is not None
                    for rule in rules
                ]
                new_terms = len(extracted_rules)
                if extracted_rules:
                    glossary = glossary.merge_with(
                        Glossary(
                            locale_source=self.config.source_locale,
                            locale_target=self.config.target_locale,
                            term_rules=extracted_rules,
                        )
                    )
                    self._persist_extracted_terms(extracted_rules)
                self._emit("glossary_extracted", {"new_terms": new_terms})

        fingerprint = hashlib.sha256(
            "\x1e".join(
                sorted(
                    f"{'|'.join(t.aliases)}@{','.join(t.key_scope)}={t.term_ko}"
                    for t in glossary.term_rules
                )
            ).encode("utf-8")
        ).hexdigest()[:12]
        return glossary, fingerprint

    async def _harvest_mod_terms(
        self, pairs: list[LanguageFilePair], glossary: Glossary
    ) -> list[TermRule]:
        """Deterministic terms from lang files mods ship in the target
        locale (see pair_harvester). Harvest scope is every paired file in
        the scan - independent of the translate scope, since evidence from
        an excluded category is still evidence. Best-effort: one broken
        file skips that file only."""

        async def harvest_one(pair: LanguageFilePair) -> list[TranslatedTerm]:
            handler = self.registry.get_handler(pair.source_path)
            if handler is None or pair.target_path is None:
                return []
            try:
                # File-semaphore bound: a large pack can pair hundreds of
                # lang files, and an unbounded gather would exhaust fds.
                async with self._file_semaphore:
                    source_data = await handler.extract(pair.source_path)
                    target_data = await handler.extract(pair.target_path)
            except Exception:  # noqa: BLE001 — enhancement path, never fatal
                logger.warning(
                    "Mod translation harvest failed for %s",
                    pair.source_path,
                    exc_info=True,
                )
                return []
            return collect_translated_terms(source_data, target_data)

        candidates = [p for p in pairs if p.has_existing_translation]
        if not candidates:
            return []
        per_file = await asyncio.gather(*(harvest_one(p) for p in candidates))
        rules = build_term_rules(
            (term for terms in per_file for term in terms),
            {alias for rule in glossary.term_rules for alias in rule.aliases},
        )
        if rules:
            logger.info(
                "Mod translation harvest: %d terms from %d translated lang files",
                len(rules),
                len(candidates),
            )
        return rules

    def _persist_extracted_terms(self, rules: list[TermRule]) -> None:
        """Extracted terms land in the user glossary store so the hub's
        glossary screen (and future runs' TM/glossary merge) can see them.
        Store I/O failure only logs - it never kills a run."""
        store_dir = self.config.glossary_store_dir or default_glossary_store_dir()
        try:
            added = merge_extracted_terms(
                store_dir,
                self.config.source_locale,
                self.config.target_locale,
                [(alias, rule.term_ko) for rule in rules for alias in rule.aliases],
            )
        except OSError:
            logger.exception("Failed to persist extracted glossary terms")
            return
        if added:
            logger.info("User glossary store: %d extracted terms added", added)

    def _load_user_glossary(self) -> list[TermRule]:
        """Store terms (manual/extracted + synced vanilla/community) as rules."""
        if not self.config.use_user_glossary:
            return []
        store_dir = self.config.glossary_store_dir or default_glossary_store_dir()
        rules: list[TermRule] = []
        for term in load_user_glossary_terms(
            store_dir, self.config.source_locale, self.config.target_locale
        ):
            source = str(term.get("source") or "").strip()
            target = str(term.get("target") or "").strip()
            if not source or not target:
                continue
            origin = str(term.get("origin") or "manual")
            # Vanilla rows are server-synced; the config toggle opts out of
            # them without touching the rest of the store.
            if origin == "vanilla" and not self.config.use_vanilla_glossary:
                continue
            rules.append(
                TermRule(
                    term_ko=target,
                    preferred_style="용어 고정",
                    aliases=[source],
                    notes=f"user glossary ({origin})",
                    key_scope=[str(s) for s in (term.get("key_scope") or [])],
                )
            )
        if rules:
            logger.info("User glossary store: %d terms merged", len(rules))
        return rules

    async def _curate_glossary_chunk(
        self,
        extractor: GlossaryExtractor,
        glossary: Glossary,
        chunk: list[TermCandidate],
        index: int,
        total: int,
        progress: dict[str, int],
    ) -> list[TermRule]:
        """One curation LLM call with schema-error feedback retries.

        A schema-invalid response (wrong category literal, missing field -
        pydantic rejects it inside the DSPy adapter) is retried up to
        glossary_max_retries times with the error text fed back as the
        `feedback` input. Glossary curation is an enhancement: a chunk
        that still fails is skipped with a warning, never fatal.
        """
        feedback = ""
        for attempt in range(1, self.config.glossary_max_retries + 2):
            self._check_cancelled()
            try:
                async with self._llm_limiter:
                    self._check_cancelled()
                    with dspy.context(lm=self.lm, adapter=dspy.JSONAdapter()):
                        pred = await extractor.acall(
                            candidates="\n".join(c.as_line() for c in chunk),
                            existing_glossary=GlossaryFilter.filter_for_texts(
                                glossary,
                                {str(i): c.term for i, c in enumerate(chunk)},
                            ).to_context_string(),
                            target_lang=self.config.target_locale,
                            feedback=feedback,
                        )
                return list(pred.term_rules or [])
            except Exception as exc:  # noqa: BLE001 — LLM output is untrusted
                error = str(exc)
                skipped = attempt > self.config.glossary_max_retries
                logger.warning(
                    "Glossary chunk %d/%d attempt %d failed%s: %s",
                    index + 1,
                    total,
                    attempt,
                    " (skipping chunk)" if skipped else " (retrying)",
                    error[:500],
                )
                self._emit(
                    "glossary_progress",
                    {
                        "done": progress["done"],
                        "total": total,
                        "new_terms": progress["new_terms"],
                        "chunk": index + 1,
                        "attempt": attempt,
                        "error": error[:300],
                        "skipped": skipped,
                    },
                )
                if skipped:
                    # Curation has no backoff, so a bare throttling refusal
                    # here is weak evidence; translation applies its own
                    # backoff before deciding. An exhausted account, though,
                    # cannot translate either.
                    if self._interruptible:
                        auth = cli_auth_failure(exc)
                        if auth is not None:
                            self._interrupt_for_auth(auth)
                        if provider_limit(exc) == "quota":
                            self._interrupt_for_quota(exc)
                    return []
                # Full error text goes back to the model; pydantic messages
                # name the offending index/field, which is exactly the fix
                # instruction the retry needs.
                feedback = error[:2000]
            finally:
                # Glossary curation uses the same LM as translation. Publish
                # cumulative usage after every attempt so token/cost cards do
                # not sit at zero until the first translation batch.
                self._emit("tokens", token_usage(self.lm))
        return []

    # -- batching ----------------------------------------------------------

    def _make_batches(self, entries: Mapping[str, str]) -> list[dict[str, str]]:
        return batching.pack_batches(
            entries,
            batch_size=self.config.batch_size,
            max_batch_chars=self.config.max_batch_chars,
        )

    def _warn_on_context_overflow(self) -> None:
        """Warn once when served prompts are filling the context window.

        Ollama truncates an oversized prompt instead of rejecting it, so a
        window too small for the compiled instructions silently degrades
        every translation. Nothing else in the stack surfaces that.
        """
        if self._context_warned or self.lm is None:
            return
        filled = context_window_filled(self.lm)
        if not filled:
            return
        self._context_warned = True
        logger.warning(
            "Prompt filled the model's context window (%d tokens, num_ctx=%s). "
            "Ollama truncates silently, so the compiled instructions may be "
            "cut: raise num_ctx or lower batch_size/max_batch_chars.",
            filled,
            self.lm.kwargs.get("num_ctx"),
        )

    async def _translate_batch(
        self,
        batch: dict[str, str],
        glossary_text: str,
        context: str,
        *,
        file: str | None = None,
        rollout_id: int | None = None,
    ) -> tuple[dict[str, str], dict[str, list[str]]]:
        """One guarded module call, with the two failure families separated.

        An output-shaped failure (unparseable JSON, schema violation) is a
        property of THIS batch, so splitting it and asking again is the
        right response and is what happens below. A transport failure
        (timeout, dropped connection, 429/503) is a property of the SERVER:
        splitting would double the concurrent load that caused it, which is
        how one slow provider becomes a congestion collapse. So admission
        shrinks, the caller backs off, and the SAME batch is retried a
        bounded number of times instead.
        """
        transport_error: Exception | None = None
        output_error: Exception | None = None
        for attempt in range(MAX_TRANSPORT_RETRIES + 1):
            async with self._llm_limiter:
                self._check_cancelled()
                self._request_counter += 1
                request_id = self._request_counter
                self._emit(
                    "batch_started",
                    {
                        "request_id": request_id,
                        "file": file or context,
                        "key": next(iter(batch)),
                        "entries": len(batch),
                    },
                )
                try:
                    # Task-local LM binding: dspy.configure() is single-task
                    # only, and server jobs run in separate asyncio tasks.
                    with dspy.context(lm=self.lm, adapter=dspy.JSONAdapter()):
                        pred = await self.translator.acall(
                            source_lang=self.config.source_locale,
                            target_lang=self.config.target_locale,
                            context=context,
                            glossary=glossary_text,
                            style_directives=self._style_directives,
                            entries=batch,
                            **({} if rollout_id is None else {"rollout_id": rollout_id}),
                        )
                except Exception as exc:  # noqa: BLE001 — LLM/adapter errors
                    auth = cli_auth_failure(exc)
                    if auth is not None:
                        if self._interruptible:
                            self._interrupt_for_auth(auth)
                        # One-off calls (retranslate, retry): neither retry
                        # nor split — every half would be refused the same.
                        return {}, {key: [str(auth)] for key in batch}
                    if self._interruptible and provider_limit(exc) == "quota":
                        # The account itself is out (spent credit, a daily
                        # quota): every retry and every queued batch would be
                        # refused alike, so stop now with the checkpoint kept.
                        self._interrupt_for_quota(exc)
                    if is_transport_error(exc):
                        transport_error = exc
                    else:
                        transport_error = None
                        output_error = exc
                else:
                    await self._llm_limiter.record_success()
                    self._warn_on_context_overflow()
                    return dict(pred.translations), dict(pred.failed)
                finally:
                    self._emit("batch_finished", {"request_id": request_id})
            if transport_error is None:
                break
            delay = await self._llm_limiter.record_transport_failure()
            if (
                self._interruptible
                and provider_limit(transport_error) is not None
                and self._llm_limiter.failing_for >= CAPACITY_GIVE_UP_SECONDS
            ):
                # Throttled with nothing getting through for longer than a
                # per-minute window lasts: the limit will not reopen soon.
                self._interrupt_for_quota(transport_error)
            if attempt == MAX_TRANSPORT_RETRIES:
                break
            logger.warning(
                "Provider unavailable (%s); admission now %d, retrying "
                "%d entries in %.1fs",
                transport_error,
                self._llm_limiter.limit,
                len(batch),
                delay,
            )
            # Back off outside the limiter so the permit is free meanwhile.
            await asyncio.sleep(delay)
        if transport_error is not None:
            # Throttling that has not yet outlasted CAPACITY_GIVE_UP_SECONDS
            # (checked above on every refusal) fails this batch's entries like
            # any transport error; "retry failed" or a resume retries them.
            # Never split a saturated server's work: that is the amplifier.
            logger.error(
                "Provider unavailable for %d entries after %d retries: %s",
                len(batch),
                MAX_TRANSPORT_RETRIES,
                transport_error,
            )
            reason = f"provider unavailable: {transport_error}"
            return {}, {key: [reason] for key in batch}
        if len(batch) == 1:
            key = next(iter(batch))
            logger.error("Translation failed for %s: %s", key, output_error)
            return {}, {key: [f"llm call failed: {output_error}"]}
        # Split outside the limiter to avoid deadlock.
        items = list(batch.items())
        mid = len(items) // 2
        left, right = dict(items[:mid]), dict(items[mid:])
        logger.warning(
            "Batch of %d failed; splitting into %d + %d",
            len(batch),
            len(left),
            len(right),
        )
        l_res, r_res = await asyncio.gather(
            self._translate_batch(left, glossary_text, context, file=file),
            self._translate_batch(right, glossary_text, context, file=file),
        )
        return {**l_res[0], **r_res[0]}, {**l_res[1], **r_res[1]}

    # -- per-file processing -------------------------------------------------

    async def _extract_for_graph(
        self, pair: LanguageFilePair
    ) -> tuple[str, dict[str, str], dict[str, str]] | None:
        """Extract a translate-scope-excluded file as graph evidence only.

        Returns ``(rel, source_data, real_existing_translations)`` or None.
        Best-effort like the harvest path: one broken file skips that
        file only.
        """
        handler = self.registry.get_handler(pair.source_path)
        if handler is None:
            return None
        try:
            async with self._file_semaphore:
                source_data = dict(await handler.extract(pair.source_path))
                existing: dict[str, str] = {}
                if pair.target_path is not None and pair.target_path.exists():
                    existing = dict(await handler.extract(pair.target_path))
        except Exception:  # noqa: BLE001 — enhancement path, never fatal
            logger.warning(
                "Graph extraction failed for %s",
                pair.source_path,
                exc_info=True,
            )
            return None
        if not source_data:
            return None
        known = {
            key: existing[key]
            for key, text in source_data.items()
            if key in existing and not is_untranslated_copy(text, existing[key])
        }
        return self._relative(pair.source_path), source_data, known

    async def _prepare_pair(
        self,
        pair: LanguageFilePair,
        glossary_version: str,
    ) -> _PreparedFile | None:
        """Phase 0 for one file: extract, classify, protect, TM lookup.

        I/O bound, so the file semaphore only covers this phase; LLM
        concurrency during the translation waves stays bounded by
        ``_llm_limiter`` alone.
        """
        async with self._file_semaphore:
            self._check_cancelled()
            handler = self.registry.get_handler(pair.source_path)
            if handler is None:
                logger.warning("No handler for %s", pair.source_path)
                return None
            rel = self._relative(pair.source_path)
            source_data = dict(await handler.extract(pair.source_path))
            if not source_data:
                return None

            existing: dict[str, str] = {}
            if pair.target_path is not None and pair.target_path.exists():
                existing = dict(await handler.extract(pair.target_path))
            # Reuse only REAL existing translations. Mods routinely copy the
            # source-locale file into other locales (wholly or partially);
            # identical values are untranslated filler that must reach the
            # LLM.
            existing_keys = {
                key
                for key, text in source_data.items()
                if not is_untranslated_copy(text, existing.get(key, ""))
            }
            # Hand translations adopted from an earlier session are decided by
            # PROVENANCE, never by content: "did a human decide this" is not a
            # question about the bytes, so it must not be asked of a byte
            # predicate. Merging them into `existing` above would route them
            # through is_untranslated_copy, and a translator who deliberately
            # kept a proper noun in English would have that decision read as
            # untranslated filler and silently sent to the model.
            human = self.human_translations.get(rel)
            if human:
                for key, text in human.items():
                    if key not in source_data:
                        continue
                    existing[key] = text
                    existing_keys.add(key)
            prepared = _PreparedFile(
                pair=pair,
                rel=rel,
                handler=handler,
                source_data=source_data,
                existing_keys=existing_keys,
                work_total=len(source_data) - len(existing_keys),
            )

            edits = self.review_edits.get(rel, {})
            protector = PlaceholderProtector()
            for key, text in source_data.items():
                edit = edits.get(key)
                if edit is not None and (
                    edit.source_text is None or edit.source_text == text
                ):
                    # The user's own decision on the partial result this run
                    # resumes: kept as it was, ahead of every other source.
                    prepared.final[key] = edit.text
                    prepared.known_translations[key] = edit.text
                    prepared.file_entries.append(
                        EntryResult(
                            key=key,
                            file=rel,
                            source_text=text,
                            translated_text=edit.text,
                            status=EntryStatus.MODIFIED,
                        )
                    )
                    continue
                if key in existing_keys:
                    prepared.final[key] = existing[key]
                    prepared.known_translations[key] = existing[key]
                    prepared.file_entries.append(
                        EntryResult(
                            key=key,
                            file=rel,
                            source_text=text,
                            translated_text=existing[key],
                            status=EntryStatus.SKIPPED,
                        )
                    )
                    continue
                protected = protector.protect(text)
                if not is_translatable_text(text, protected):
                    prepared.final[key] = text
                    prepared.file_entries.append(
                        EntryResult(
                            key=key,
                            file=rel,
                            source_text=text,
                            translated_text=text,
                            status=EntryStatus.SKIPPED,
                        )
                    )
                    continue
                prepared.protected_map[key] = protected
                prepared.to_translate[key] = protected.protected

            if self.config.source_text_only:
                self._settle_as_source_text(prepared)
            # Previous-version A/B/C match is deliberately stricter than the
            # global source-text TM: file identity + entry key + exact A/C
            # source equality.  It is run-scoped and never stored globally.
            if self.migration is not None and prepared.to_translate:
                logical = logical_file_id(
                    pair.source_path, self.config.modpack_path
                )
                for key in list(prepared.to_translate):
                    translated = self.migration.lookup(
                        logical, key, source_data[key]
                    )
                    if translated is None:
                        continue
                    prepared.final[key] = translated
                    prepared.known_translations[key] = translated
                    prepared.migrated_keys.add(key)
                    prepared.to_translate.pop(key, None)
                    prepared.file_entries.append(
                        EntryResult(
                            key=key,
                            file=rel,
                            source_text=source_data[key],
                            translated_text=translated,
                            status=EntryStatus.MIGRATED,
                        )
                    )

            # Before the TM, deliberately: these are this session's own paid
            # translations, and the uninterrupted run would have kept them
            # even if the TM has since learned a different value.
            if self.resume_from is not None and prepared.to_translate:
                self._apply_checkpoint(prepared)

            # TM lookup on raw source text
            if self.tm is not None and prepared.to_translate:
                raw = {k: source_data[k] for k in prepared.to_translate}
                tm_hits = await asyncio.to_thread(
                    self.tm.lookup_many,
                    raw,
                    self.config.target_locale,
                    glossary_version,
                )
                for key, translated in tm_hits.items():
                    prepared.final[key] = translated
                    prepared.known_translations[key] = translated
                    prepared.to_translate.pop(key, None)
                    prepared.file_entries.append(
                        EntryResult(
                            key=key,
                            file=rel,
                            source_text=source_data[key],
                            translated_text=translated,
                            status=EntryStatus.TM_HIT,
                        )
                    )

            # Last, deliberately: migration and the TM get first refusal, so a
            # translator is handed a remembered translation to confirm rather
            # than a blank box. Whatever is still unclaimed is genuinely theirs
            # to write.
            if self.config.manual_seed:
                self._settle_as_manual_pending(prepared)

            if prepared.work_total > 0:
                self._emit(
                    "progress",
                    {
                        "stage": "translate",
                        "file": rel,
                        # Checkpointed entries are already settled too; a
                        # fresh run has none here, so its numbers are as
                        # before.
                        "done": (
                            len(prepared.final)
                            - len(existing_keys)
                            + len(prepared.translated_raw)
                        ),
                        "total": prepared.work_total,
                    },
                )
            return prepared

    def _settle_as_source_text(self, prepared: _PreparedFile) -> None:
        """Settle every still-pending entry to its own source text.

        The source-text export's stand-in for the translation waves: it
        leaves ``final`` and ``file_entries`` in exactly the shape a fully
        successful run does — PASSED is the "fresh" status the output
        generator writes into the resource pack — so the generated trees
        carry the same files and keys as a translated run, with the
        untranslated strings as their values. Entries the pack already
        translated keep that translation (SKIPPED, settled by the caller):
        moru would not have touched them either.
        """
        for key in prepared.to_translate:
            text = prepared.source_data[key]
            prepared.final[key] = text
            prepared.file_entries.append(
                EntryResult(
                    key=key,
                    file=prepared.rel,
                    source_text=text,
                    translated_text=text,
                    status=EntryStatus.PASSED,
                )
            )
        prepared.to_translate.clear()
        prepared.protected_map.clear()

    def _settle_as_manual_pending(self, prepared: _PreparedFile) -> None:
        """Leave every still-pending entry untranslated, for a human.

        The manual-seed counterpart to :meth:`_settle_as_source_text`, and the
        one place the two differ: this writes no translation at all. ``final``
        is deliberately NOT populated, because a seed run produces a work queue
        rather than an output tree — PENDING is outside ``_FRESH_STATUSES`` and
        carries no text, so nothing here could reach a resource pack even if
        the output stage ran.

        Entries the pack already translated keep that translation (SKIPPED),
        and anything migration or the TM settled keeps its own status: those
        are answers a translator confirms, not work they still owe.
        """
        for key in prepared.to_translate:
            prepared.file_entries.append(
                EntryResult(
                    key=key,
                    file=prepared.rel,
                    source_text=prepared.source_data[key],
                    translated_text=None,
                    status=EntryStatus.PENDING,
                )
            )
        prepared.to_translate.clear()
        prepared.protected_map.clear()

    # -- checkpoint ----------------------------------------------------------

    def _apply_checkpoint(self, prepared: _PreparedFile) -> None:
        """Settle the entries an earlier, interrupted run already paid for.

        Reused only while the source text is unchanged (digest match): a
        string a modpack update rewrote must reach the model again. Values
        re-enter as fresh model output (``translated_raw``), so validation,
        duplicate canonicalization, the unchanged-copy retry, TM writes and
        output generation treat them exactly as if this run produced them.
        """
        state = self.resume_from
        assert state is not None  # caller checks
        root = self.config.modpack_path
        logical = logical_file_id(prepared.pair.source_path, root)

        def logical_of(rel: str) -> str:
            return logical_file_id(root / rel, root)

        for key in list(prepared.to_translate):
            hit = state.match(
                prepared.rel,
                key,
                source_digest(prepared.source_data[key]),
                logical_file=logical,
                logical_of=logical_of,
            )
            if hit is None:
                continue
            del prepared.to_translate[key]
            prepared.translated_raw[key] = hit.text
            # Graph evidence, exactly as the wave that produced it recorded it.
            prepared.known_translations[key] = hit.text
            if hit.retried:
                prepared.retried_keys.add(key)
            self.resumed_entries += 1

    async def _checkpoint_batch(
        self,
        prepared: _PreparedFile,
        settled: Mapping[str, str],
        *,
        retried: bool = False,
    ) -> None:
        """Make one completed batch durable before its progress is reported.

        A write failure is logged rather than raised: the results are still
        held in memory and reach the session file at the end, so a full disk
        costs resumability, never the paid run itself.
        """
        if self.checkpoint is None or not settled:
            return
        rows = [
            (key, source_digest(prepared.source_data[key]), text)
            for key, text in settled.items()
        ]
        try:
            await asyncio.to_thread(
                self.checkpoint.record_batch, prepared.rel, rows, retried=retried
            )
        except OSError:
            logger.exception(
                "Failed to checkpoint %d translations of %s", len(rows), prepared.rel
            )

    async def _checkpoint_plan(self, prepared_files: list[_PreparedFile]) -> None:
        """Record the run's scope: the progress a relaunched app can show."""
        if self.checkpoint is None:
            return
        total = sum(prepared.work_total for prepared in prepared_files)
        settled = sum(
            prepared.work_total - len(prepared.to_translate)
            for prepared in prepared_files
        )
        try:
            await asyncio.to_thread(self.checkpoint.record_plan, total, settled)
        except OSError:
            logger.exception("Failed to checkpoint the run plan")

    async def _translate_wave(
        self,
        prepared: _PreparedFile,
        keys: list[str],
        glossary: Glossary,
        graph: TranslationGraph | None,
        *,
        retry_unchanged: bool = False,
    ) -> None:
        """Translate a subset of one file's pending entries.

        Every key of a completed batch leaves ``to_translate`` (success,
        placeholder failure, or model failure alike get a terminal
        outcome), so a later wave never re-sends it. Cancellation flushes
        the completed batches and sets ``was_cancelled`` instead of
        re-raising - the caller finalizes partial results either way.
        """
        pending = {
            key: prepared.to_translate[key]
            for key in keys
            if key in prepared.to_translate
        }
        if not pending:
            return
        # Repeated texts within THIS file's wave share one model call: the
        # dispatched (protected) text plus the wave's fixed context is
        # exactly the scope in which two occurrences are interchangeable.
        # Aliases are expanded back before the loop below, so every key
        # still gets a terminal outcome.
        unique, aliases = batching.dedup_entries(pending)
        rel = prepared.rel
        source_data = prepared.source_data
        base_context = f"file: {rel}; handler: {prepared.handler.name}"
        if retry_unchanged:
            base_context += (
                "; retry: the prior attempt copied the source unchanged; "
                "translate user-facing natural language while preserving "
                "intentional proper nouns and placeholders"
            )

        async def translate_one(
            batch: dict[str, str],
        ) -> tuple[
            dict[str, str],
            dict[str, str],
            dict[str, list[str]],
        ]:
            self._check_cancelled()
            context = self._with_sibling_context(
                base_context, graph, rel, batch.keys()
            )
            # Prompt only the glossary rules relevant to THIS batch —
            # rendering the whole store (a synced vanilla set alone is
            # thousands of rules) blows up the prompt and derails small
            # models.
            batch_glossary = GlossaryFilter.filter_for_texts(
                glossary, {k: source_data[k] for k in batch}
            ).to_context_string()
            translations, failed = await self._translate_batch(
                batch, batch_glossary, context, file=rel
            )
            return batch, translations, failed

        batch_tasks = [
            asyncio.create_task(translate_one(batch))
            for batch in self._make_batches(unique)
        ]
        try:
            for completed in asyncio.as_completed(batch_tasks):
                batch, translations, failed = await completed
                batch = batching.expand_aliases(batch, aliases)
                translations = batching.expand_aliases(translations, aliases)
                failed = batching.expand_aliases(failed, aliases)
                # This batch's usable translations, made durable below.
                settled: dict[str, str] = {}
                for key in batch:
                    prepared.to_translate.pop(key, None)
                    protected = prepared.protected_map[key]
                    out = translations.get(key)
                    errors = list(failed.get(key, []))
                    if out is None:
                        reported = errors or ["no translation returned"]
                        prepared.file_entries.append(
                            EntryResult(
                                key=key,
                                file=rel,
                                source_text=source_data[key],
                                status=EntryStatus.FAILED,
                                errors=reported,
                            )
                        )
                        self._emit(
                            "entry_failed",
                            {"key": key, "file": rel, "errors": reported},
                        )
                        continue
                    try:
                        restored = protected.restore(out)
                    except PlaceholderError as exc:
                        prepared.file_entries.append(
                            EntryResult(
                                key=key,
                                file=rel,
                                source_text=source_data[key],
                                translated_text=out,
                                status=EntryStatus.FAILED,
                                errors=[*errors, str(exc)],
                            )
                        )
                        self._emit(
                            "entry_failed",
                            {
                                "key": key,
                                "file": rel,
                                "errors": [*errors, str(exc)],
                            },
                        )
                        continue
                    prepared.translated_raw[key] = restored
                    settled[key] = restored
                    if graph is not None:
                        graph.record_translation(rel, key, restored)
                    self._entry_counter += 1
                    if self._entry_counter % ENTRY_TICKER_INTERVAL == 1:
                        # Sampled live preview pair for the GUI ticker.
                        self._emit(
                            "entry_done",
                            {
                                "key": key,
                                "source": source_data[key][:TICKER_TEXT_LIMIT],
                                "translated": restored[:TICKER_TEXT_LIMIT],
                            },
                        )
                # Durable before it is reported: a crash after this point
                # can no longer cost the batch, and progress the user saw
                # is progress a resumed run keeps. Retry-wave results are
                # marked so a resume does not pay for that retry twice.
                await self._checkpoint_batch(
                    prepared, settled, retried=retry_unchanged
                )
                if not retry_unchanged:
                    # Retry waves re-enter keys the file already reported as
                    # done; re-emitting would walk the bar backwards.
                    self._emit(
                        "progress",
                        {
                            "stage": "translate",
                            "file": rel,
                            # Existing target-locale keys are excluded from
                            # the scan totals and therefore from live
                            # progress. Every key removed from to_translate
                            # has reached a terminal outcome, including
                            # failed model batches.
                            "done": (
                                prepared.work_total - len(prepared.to_translate)
                            ),
                            "total": prepared.work_total,
                        },
                    )
                # Once per completed batch keeps live token/cost counters
                # current; build_lm's LM totals usage as calls land.
                self._emit("tokens", token_usage(self.lm))
        except asyncio.CancelledError:
            prepared.was_cancelled = True
        finally:
            for task in batch_tasks:
                if not task.done():
                    task.cancel()
            if batch_tasks:
                await asyncio.gather(*batch_tasks, return_exceptions=True)

    @staticmethod
    def _canonicalize_unchanged_duplicates(
        prepared_files: Iterable[_PreparedFile],
        graph: TranslationGraph | None = None,
    ) -> int:
        """Reuse the dominant real translation for an identical source.

        A model can translate one occurrence of ``Superior Shop`` and copy
        another unchanged even inside the same language file.  Existing,
        migrated, TM, and fresh translations are all valid evidence; only
        source-equal fresh results are replaced.  Majority vote with
        first-seen tie breaking avoids arbitrary last-writer behavior.
        """
        prepared_files = list(prepared_files)
        votes: dict[str, dict[str, int]] = {}
        first_seen: dict[tuple[str, str], int] = {}
        # (source, translation) pairs evidenced outside the run-scoped
        # migration catalog; the rest must never reach the global TM.
        storable: set[tuple[str, str]] = set()
        order = 0
        for prepared in prepared_files:
            candidates = {**prepared.final, **prepared.translated_raw}
            for key, translated in candidates.items():
                source = prepared.source_data[key]
                if is_untranslated_copy(source, translated):
                    continue
                by_translation = votes.setdefault(source, {})
                by_translation[translated] = by_translation.get(translated, 0) + 1
                first_seen.setdefault((source, translated), order)
                order += 1
                if key not in prepared.migrated_keys:
                    storable.add((source, translated))

        canonical = {
            source: min(
                choices,
                key=lambda translated: (
                    -choices[translated],
                    first_seen[(source, translated)],
                ),
            )
            for source, choices in votes.items()
        }
        repaired = 0
        for prepared in prepared_files:
            for key, translated in list(prepared.translated_raw.items()):
                source = prepared.source_data[key]
                replacement = canonical.get(source)
                if replacement is None or not is_untranslated_copy(source, translated):
                    continue
                prepared.translated_raw[key] = replacement
                if (source, replacement) not in storable:
                    prepared.migrated_keys.add(key)
                if graph is not None:
                    graph.record_translation(prepared.rel, key, replacement)
                repaired += 1
        return repaired

    async def _retry_unchanged_translations(
        self,
        prepared_files: Iterable[_PreparedFile],
        glossary: Glossary,
        graph: TranslationGraph | None,
    ) -> int:
        """Retry fresh user-facing values that remain source-identical once."""
        work: list[tuple[_PreparedFile, dict[str, str]]] = []
        count = 0
        for prepared in prepared_files:
            # Cancelled files keep whatever their completed batches settled.
            if prepared.was_cancelled:
                continue
            stashed = {
                key: translated
                for key, translated in prepared.translated_raw.items()
                # One retry per entry per session, even across a resume.
                if key not in prepared.retried_keys
                and is_untranslated_copy(prepared.source_data[key], translated)
            }
            if not stashed:
                continue
            count += len(stashed)
            for key in stashed:
                prepared.translated_raw.pop(key, None)
                prepared.to_translate[key] = prepared.protected_map[key].protected
            work.append((prepared, stashed))
        if not work:
            return 0
        await asyncio.gather(
            *(
                self._translate_wave(
                    prepared,
                    list(stashed),
                    glossary,
                    graph,
                    retry_unchanged=True,
                )
                for prepared, stashed in work
            )
        )
        # A retry that produced nothing must not destroy the value it was
        # retrying: the source-identical string still ships, and the retry's
        # FAILED entry would book it as a loss on top.
        spent_retries: list[tuple[_PreparedFile, dict[str, str]]] = []
        for prepared, stashed in work:
            recovered = {
                key: translated
                for key, translated in stashed.items()
                if key not in prepared.translated_raw
            }
            if not recovered:
                continue
            # Keys whose retry batch did complete, just without a usable
            # answer (still in to_translate means it was never sent). Their
            # one retry is spent; record that so a resume does not repeat it.
            spent_retries.append(
                (
                    prepared,
                    {
                        key: translated
                        for key, translated in recovered.items()
                        if key not in prepared.to_translate
                    },
                )
            )
            prepared.file_entries = [
                entry
                for entry in prepared.file_entries
                if entry.key not in recovered
            ]
            for key, translated in recovered.items():
                prepared.to_translate.pop(key, None)
                prepared.translated_raw[key] = translated
        # After every file is restored: an await inside the loop could be
        # cancelled and leave later files without their recovered values.
        for prepared, spent in spent_retries:
            await self._checkpoint_batch(prepared, spent, retried=True)
        return count

    @staticmethod
    def _with_sibling_context(
        base_context: str,
        graph: TranslationGraph | None,
        file: str,
        keys: Iterable[str],
    ) -> str:
        """Append already-settled sibling entry lines to a batch context."""
        if graph is None:
            return base_context
        keys = list(keys)
        block = graph.sibling_context(file, keys, exclude=set(keys))
        if not block:
            return base_context
        return f"{base_context}\n{SIBLING_CONTEXT_HEADER}\n{block}"

    def _ensure_graph(self, result: PipelineResult) -> TranslationGraph | None:
        """The run's graph if alive, else a rebuild from result entries.

        The server creates a fresh pipeline per retranslate call, so the
        in-run graph is usually gone; entries carry enough state (file,
        key, source, translation) to rebuild an equivalent one.
        """
        if not self.config.use_translation_graph:
            return None
        if self._graph is None:
            self._graph = TranslationGraph.from_entries(result.entries)
        return self._graph

    async def _gather_waves(
        self,
        work: list[tuple[_PreparedFile, list[str]]],
        glossary: Glossary,
        graph: TranslationGraph | None,
    ) -> None:
        """One wave across files. Only external cancellation escapes."""
        tasks = [
            asyncio.create_task(self._translate_wave(prepared, keys, glossary, graph))
            for prepared, keys in work
            if keys and not prepared.was_cancelled
        ]
        if not tasks:
            return
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            # gather propagates cancellation before child cleanup is
            # necessarily visible. Wait for every wave to flush its
            # completed batches into the per-file partial state.
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    async def _finalize_pair(
        self,
        prepared: _PreparedFile,
        validator: TranslationValidator,
        result: PipelineResult,
        glossary_version: str,
    ) -> None:
        """Phase 3 for one file: validate, order, extend result, TM store."""
        rel = prepared.rel
        source_data = prepared.source_data
        translated_raw = prepared.translated_raw
        file_entries = prepared.file_entries

        # Post-restore validation with the full validator
        if translated_raw:
            report = validator.validate(
                {k: source_data[k] for k in translated_raw}, translated_raw
            )
            issues_by_key: dict[str, list[str]] = {}
            error_keys: set[str] = set()
            for issue in report.issues:
                issues_by_key.setdefault(issue.key, []).append(issue.message)
                if issue.severity == ValidationSeverity.ERROR:
                    error_keys.add(issue.key)
            for key, translated in translated_raw.items():
                issues = issues_by_key.get(key, [])
                if key in error_keys:
                    file_entries.append(
                        EntryResult(
                            key=key,
                            file=rel,
                            source_text=source_data[key],
                            translated_text=translated,
                            status=EntryStatus.FAILED,
                            errors=issues,
                        )
                    )
                    self._emit(
                        "entry_failed",
                        {"key": key, "file": rel, "errors": issues},
                    )
                    continue
                prepared.final[key] = translated
                file_entries.append(
                    EntryResult(
                        key=key,
                        file=rel,
                        source_text=source_data[key],
                        translated_text=translated,
                        status=(
                            EntryStatus.WARNING if issues else EntryStatus.PASSED
                        ),
                        errors=issues,
                    )
                )

        # Concurrent batches finish out of order; restore source order for
        # review rows and generated language files. Extend before the
        # optional TM write so cancellation still preserves these entries.
        key_order = {key: index for index, key in enumerate(source_data)}
        file_entries.sort(key=lambda entry: key_order[entry.key])
        result.entries.extend(file_entries)

        # Persist fresh translations into TM only on the normal path.
        # Migration-derived values stay run-scoped. An interrupted run is not
        # finished either: its resume reuses these values from the checkpoint
        # and stores them once, as an uninterrupted run would — a partial
        # store here would turn the resume's other occurrences of the same
        # text into TM hits the uninterrupted run never had.
        if (
            self.tm is not None
            and not prepared.was_cancelled
            and self._interruption is None
        ):
            stored = [
                (source_data[k], v)
                for k, v in translated_raw.items()
                if k in prepared.final and k not in prepared.migrated_keys
            ]
            if stored:
                await asyncio.to_thread(
                    self.tm.store_many,
                    stored,
                    self.config.target_locale,
                    glossary_version,
                )

        if prepared.was_cancelled:
            raise asyncio.CancelledError("file translation cancelled")

    def _relative(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(
                self.config.modpack_path.resolve()
            ).as_posix()
        except ValueError:
            return path.as_posix()

    @staticmethod
    def _stage_frame(done: int) -> dict[str, object]:
        return {"stage": "write", "done": done, "total": 1}

    # -- entry point ---------------------------------------------------------

    async def run(
        self,
        scan_result: ScanResult | None = None,
        migration: MigrationCatalog | None = None,
    ) -> PipelineResult:
        started = time.monotonic()
        result = PipelineResult(config=self.config)
        cancelled = False
        self._interruptible = True

        try:
            self._emit("progress", {"stage": "scan", "done": 0, "total": 1})
            if scan_result is None:
                scanner = ModpackScanner(
                    source_locale=self.config.source_locale,
                    target_locale=self.config.target_locale,
                    mod_blacklist=self.config.mod_blacklist,
                )
                scan_result = await scanner.scan(self.config.modpack_path)
            result.scan_result = scan_result
            result.artifact_id = self.artifact_id
            self.migration = migration
            result.migration = migration

            migration_inputs = (
                self.config.previous_modpack_path,
                self.config.previous_resourcepack_path,
                self.config.previous_overrides_path,
            )
            if self.migration is None and any(
                path is not None for path in migration_inputs
            ):
                if self.config.previous_modpack_path is None:
                    raise ValueError(
                        "previous_modpack_path is required for translation migration"
                    )
                if (
                    self.config.previous_resourcepack_path is None
                    and self.config.previous_overrides_path is None
                ):
                    raise ValueError(
                        "a previous resource pack or overrides artifact is required"
                    )
                self._emit(
                    "progress", {"stage": "migration", "done": 0, "total": 1}
                )
                migration_assets = output_root(self.config) / ".migration_assets"
                self.migration = await build_migration_catalog(
                    previous_modpack_path=self.config.previous_modpack_path,
                    previous_resourcepack_path=self.config.previous_resourcepack_path,
                    previous_overrides_path=self.config.previous_overrides_path,
                    current_modpack_root=self.config.modpack_path,
                    current_scan=scan_result,
                    source_locale=self.config.source_locale,
                    target_locale=self.config.target_locale,
                    asset_cache_dir=migration_assets,
                )
                result.migration = self.migration
                self._emit(
                    "progress", {"stage": "migration", "done": 1, "total": 1}
                )

            all_pairs = scan_result.all_translation_pairs
            pairs = all_pairs
            #: Pairs excluded from translation but still graph evidence: a
            #: quests-only run wants mod-lang names (and their existing
            #: translations) as binding sources - same principle as the
            #: harvest scope below.
            out_of_scope: list[LanguageFilePair] = []
            if self.config.include_categories is not None:
                allowed = set(self.config.include_categories)
                category_by_path = {
                    str(Path(tf.input_path)): tf.category or tf.file_type
                    for tf in scan_result.translation_files
                }
                pairs = []
                for p in all_pairs:
                    if category_by_path.get(str(p.source_path)) in allowed:
                        pairs.append(p)
                    else:
                        out_of_scope.append(p)
            result.stats.total_files = len(pairs)

            self._emit("progress", {"stage": "glossary", "done": 0, "total": 1})
            # Harvest scope = every paired file in the scan, regardless of
            # the include_categories translate scope: existing translations
            # in an excluded category are still terminology evidence.
            glossary, glossary_version = await self._build_glossary(
                pairs, harvest_pairs=scan_result.paired_files
            )
            result.glossary = glossary
            validator = TranslationValidator(
                glossary if glossary.has_rules else None
            )

            prepared_files: list[_PreparedFile] = []
            interrupted = False
            try:
                prepare_tasks = [
                    asyncio.create_task(self._prepare_pair(pair, glossary_version))
                    for pair in pairs
                ]
                try:
                    prepared_files = [
                        prepared
                        for prepared in await asyncio.gather(*prepare_tasks)
                        if prepared is not None
                    ]
                except asyncio.CancelledError:
                    # gather propagates cancellation before child cleanup is
                    # necessarily visible. Keep what completed so its skipped
                    # and TM-hit entries still reach the result.
                    gathered = await asyncio.gather(
                        *prepare_tasks, return_exceptions=True
                    )
                    prepared_files = [
                        prepared
                        for prepared in gathered
                        if isinstance(prepared, _PreparedFile)
                    ]
                    raise

                if self.resume_from is not None:
                    logger.info(
                        "Resume: %d checkpointed translations reused, %d entries "
                        "left for the model",
                        self.resumed_entries,
                        sum(len(prepared.to_translate) for prepared in prepared_files),
                    )
                await self._checkpoint_plan(prepared_files)

                graph: TranslationGraph | None = None
                if self.config.use_translation_graph:
                    outside = await asyncio.gather(
                        *(self._extract_for_graph(pair) for pair in out_of_scope)
                    )
                    graph = TranslationGraph.build(
                        [
                            (
                                prepared.rel,
                                prepared.source_data,
                                prepared.known_translations,
                            )
                            for prepared in prepared_files
                        ]
                        + [row for row in outside if row is not None]
                    )
                    self._graph = graph
                    graph_stats = graph.stats()
                    logger.info(
                        "translation graph: %d terms, %d mentions, "
                        "%d sibling groups over %d entries",
                        graph_stats["terms"],
                        graph_stats["mentions"],
                        graph_stats["sibling_groups"],
                        graph_stats["entries"],
                    )

                    # Wave 1: name-defining entries. Their settled
                    # translations become glossary bindings for the rest.
                    await self._gather_waves(
                        [
                            (
                                prepared,
                                [
                                    key
                                    for key in prepared.to_translate
                                    if is_name_entry(
                                        key, prepared.source_data[key]
                                    )
                                ],
                            )
                            for prepared in prepared_files
                        ],
                        glossary,
                        graph,
                    )
                    # Merge bindings even when wave 1 was empty: pre-settled
                    # names (existing translations, TM hits) bind too. The TM
                    # fingerprint intentionally stays at the base glossary -
                    # bindings are model-output-derived and would invalidate
                    # the TM on every run.
                    bindings = graph.bindings(glossary)
                    glossary = glossary.merge_with(
                        Glossary(
                            locale_source=self.config.source_locale,
                            locale_target=self.config.target_locale,
                            term_rules=bindings,
                        )
                    )
                    result.glossary = glossary
                    validator = TranslationValidator(
                        glossary if glossary.has_rules else None
                    )
                    if bindings:
                        logger.info(
                            "translation graph: %d name bindings merged "
                            "into the run glossary",
                            len(bindings),
                        )

                # Final wave: everything still pending (all of it when the
                # graph is disabled).
                await self._gather_waves(
                    [
                        (prepared, list(prepared.to_translate))
                        for prepared in prepared_files
                    ],
                    glossary,
                    graph,
                )

                repaired = self._canonicalize_unchanged_duplicates(
                    prepared_files, graph
                )
                if repaired:
                    logger.info(
                        "Reused consistent translations for %d unchanged model outputs",
                        repaired,
                    )
                retried = await self._retry_unchanged_translations(
                    prepared_files, glossary, graph
                )
                if retried:
                    logger.info(
                        "Retried %d user-facing translations copied from source",
                        retried,
                    )
                    # A successful retry can now repair other duplicate copies.
                    self._canonicalize_unchanged_duplicates(prepared_files, graph)
            except asyncio.CancelledError:
                interrupted = True
                for prepared in prepared_files:
                    prepared.was_cancelled = True

            # Finalize EVERY prepared file - a cancelled run still validates
            # and preserves its completed batches - then surface the
            # cancellation once.
            cancelled_files = interrupted
            for prepared in prepared_files:
                try:
                    await self._finalize_pair(
                        prepared, validator, result, glossary_version
                    )
                except asyncio.CancelledError:
                    cancelled_files = True
            if cancelled_files:
                raise asyncio.CancelledError("file translation cancelled")
            self._check_cancelled()
        except asyncio.CancelledError:
            # Cancellation is a successful partial-result boundary, not a
            # failed pipeline. Completed batches/files remain in `result`.
            cancelled = True
            logger.info(
                "Pipeline cancellation captured with %d partial entries",
                len(result.entries),
            )

        # Generate installable trees from every preserved partial entry. A
        # cancellation arriving during the write is consumed once and retried.
        has_migrated_assets = (
            result.migration is not None
            and result.migration.stats.preserved_resourcepack_assets > 0
        )
        # A manual seed produces a work queue, not an artifact: every entry it
        # created is PENDING with no text, so the only thing an output pass
        # could emit is the pack's own pre-existing translations. Export runs
        # later, from the reviewed entries, via apply_entry_edits.
        if (result.entries or has_migrated_assets) and not self.config.manual_seed:
            self._emit("progress", self._stage_frame(0))
            try:
                await write_outputs(result)
            except asyncio.CancelledError:
                cancelled = True
                await write_outputs(result)
            self._emit("progress", self._stage_frame(1))

        usage = token_usage(self.lm)
        stats = result.stats
        stats.total_entries = len(result.entries)
        stats.tm_hits = sum(
            1 for e in result.entries if e.status == EntryStatus.TM_HIT
        )
        stats.migration_hits = sum(
            1 for e in result.entries if e.status == EntryStatus.MIGRATED
        )
        stats.skipped_entries = sum(
            1 for e in result.entries if e.status == EntryStatus.SKIPPED
        )
        stats.failed_entries = sum(
            1 for e in result.entries if e.status == EntryStatus.FAILED
        )
        stats.translated_entries = sum(
            1
            for e in result.entries
            # MODIFIED: review edits a resumed run carried over (see
            # ReviewEdit). A fresh run produces none.
            if e.status
            in (EntryStatus.PASSED, EntryStatus.WARNING, EntryStatus.MODIFIED)
        )
        stats.categories = category_stats(result)
        stats.prompt_tokens = usage["prompt_tokens"]
        stats.completion_tokens = usage["completion_tokens"]
        stats.cached_tokens = usage["cached_tokens"]
        stats.duration_seconds = round(time.monotonic() - started, 2)
        stats.finalize()

        if not cancelled:
            self._emit("done", {"stats": stats.model_dump()})
            logger.info(
                "Pipeline done: %d translated, %d TM hits, %d failed, "
                "%d migrated, %d skipped (%.1fs)",
                stats.translated_entries,
                stats.tm_hits,
                stats.failed_entries,
                stats.migration_hits,
                stats.skipped_entries,
                stats.duration_seconds,
            )
        result.interruption = self._interruption
        self._interruptible = False
        return result

    async def retry_failed(self, result: PipelineResult) -> PipelineResult:
        """Re-translate failed entries, mutating the same PipelineResult."""
        failed = result.failed
        if not failed:
            return result
        glossary = result.glossary
        if glossary is None:  # result predates run-scoped glossary storage
            glossary, _ = await self._build_glossary([])
        validator = TranslationValidator(glossary if glossary.has_rules else None)

        by_file: dict[str, list[EntryResult]] = {}
        for entry in failed:
            by_file.setdefault(entry.file, []).append(entry)

        graph = self._ensure_graph(result)
        for rel, entries in by_file.items():
            protector = PlaceholderProtector()
            protected = {e.key: protector.protect(e.source_text) for e in entries}
            pending = {e.key: protected[e.key].protected for e in entries}
            source_by_key = {e.key: e.source_text for e in entries}
            # Same budget as every other dispatch. An unbounded per-file
            # prompt on top of the ~36-41k-character compiled instructions
            # overflows the context window, and that failure is
            # OUTPUT-shaped, so it feeds the split path below — a second
            # amplification loop, on a path that exists because these
            # entries already failed once.
            unique, aliases = batching.dedup_entries(pending)
            translations: dict[str, str] = {}
            failures: dict[str, list[str]] = {}
            for chunk in self._make_batches(unique):
                chunk_glossary = GlossaryFilter.filter_for_texts(
                    glossary, {key: source_by_key[key] for key in chunk}
                ).to_context_string()
                context = self._with_sibling_context(
                    f"retry; file: {rel}", graph, rel, chunk
                )
                chunk_translations, chunk_failures = await self._translate_batch(
                    chunk, chunk_glossary, context
                )
                translations.update(
                    batching.expand_aliases(chunk_translations, aliases)
                )
                failures.update(batching.expand_aliases(chunk_failures, aliases))
            for entry in entries:
                out = translations.get(entry.key)
                if out is None:
                    entry.errors = failures.get(entry.key, entry.errors)
                    continue
                try:
                    restored = protected[entry.key].restore(out)
                except PlaceholderError as exc:
                    entry.errors = [str(exc)]
                    continue
                report = validator.validate(
                    {entry.key: entry.source_text}, {entry.key: restored}
                )
                if report.get_errors():
                    entry.errors = [i.message for i in report.get_errors()]
                    continue
                entry.translated_text = restored
                entry.status = EntryStatus.MODIFIED
                entry.errors = []
                if graph is not None:
                    graph.record_translation(rel, entry.key, restored)
        self._refresh_stats(result)
        return result

    async def retranslate_entry(
        self, result: PipelineResult, key: str, file: str | None = None
    ) -> EntryResult:
        """Re-translate ONE entry in place (review screen "AI retranslate").

        Works on entries in any status. Success marks the entry MODIFIED
        with cleared errors. Failure of a FAILED entry refreshes its error
        list; failure of a previously passing entry leaves it untouched and
        raises RetranslateError so the caller can surface the reason.
        """
        entry = next(
            (
                e
                for e in result.entries
                if e.key == key and (file is None or e.file == file)
            ),
            None,
        )
        if entry is None:
            raise KeyError(key)
        glossary = result.glossary
        if glossary is None:  # result predates run-scoped glossary storage
            glossary, _ = await self._build_glossary([])
        validator = TranslationValidator(glossary if glossary.has_rules else None)

        graph = self._ensure_graph(result)
        protector = PlaceholderProtector()
        protected = protector.protect(entry.source_text)
        batch_glossary = GlossaryFilter.filter_for_texts(
            glossary, {entry.key: entry.source_text}
        ).to_context_string()
        context = self._with_sibling_context(
            f"retranslate; file: {entry.file}", graph, entry.file, [entry.key]
        )
        translations, failures = await self._translate_batch(
            {entry.key: protected.protected},
            batch_glossary,
            context,
            # The user asked for ANOTHER translation: a cached replay of the
            # one they are looking at would be no answer at all.
            rollout_id=secrets.randbits(31),
        )

        errors: list[str]
        restored: str | None = None
        out = translations.get(entry.key)
        if out is None:
            errors = failures.get(entry.key, ["model returned no translation"])
        else:
            try:
                restored = protected.restore(out)
                report = validator.validate(
                    {entry.key: entry.source_text}, {entry.key: restored}
                )
                errors = [i.message for i in report.get_errors()]
            except PlaceholderError as exc:
                errors = [str(exc)]

        if errors:
            if entry.status is EntryStatus.FAILED:
                entry.errors = errors
                return entry
            raise RetranslateError("; ".join(errors))

        assert restored is not None  # errors == [] implies successful restore
        entry.translated_text = restored
        entry.status = EntryStatus.MODIFIED
        entry.errors = []
        if graph is not None:
            graph.record_translation(entry.file, entry.key, restored)
        self._refresh_stats(result)
        return entry

    @staticmethod
    def _refresh_stats(result: PipelineResult) -> None:
        """Recompute the counters a post-run mutation can change."""
        stats = result.stats
        stats.total_entries = len(result.entries)
        stats.tm_hits = sum(
            1 for entry in result.entries if entry.status == EntryStatus.TM_HIT
        )
        stats.migration_hits = sum(
            1 for entry in result.entries if entry.status == EntryStatus.MIGRATED
        )
        stats.skipped_entries = sum(
            1 for entry in result.entries if entry.status == EntryStatus.SKIPPED
        )
        stats.failed_entries = sum(
            1 for entry in result.entries if entry.status == EntryStatus.FAILED
        )
        stats.translated_entries = sum(
            1
            for e in result.entries
            if e.status
            in (EntryStatus.PASSED, EntryStatus.WARNING, EntryStatus.MODIFIED)
        )
        stats.categories = category_stats(result)
        stats.finalize()

    def close(self) -> None:
        if self.tm is not None:
            self.tm.close()


async def run_pipeline(
    config: PipelineConfig,
    *,
    on_event: Callable[[str, dict[str, object]], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    on_pipeline: Callable[[TranslationPipeline], None] | None = None,
    scan_result: ScanResult | None = None,
    migration: MigrationCatalog | None = None,
    human_translations: dict[str, dict[str, str]] | None = None,
    checkpoint: TranslationCheckpoint | None = None,
    resume_from: CheckpointState | None = None,
    review_edits: Mapping[str, Mapping[str, ReviewEdit]] | None = None,
) -> PipelineResult:
    """Convenience wrapper: build, run, close.

    ``on_pipeline`` receives the pipeline instance before the run starts —
    the server uses it to expose the live translation graph while the job
    is running.

    ``human_translations`` carries an earlier session's committed hand
    translations (``config.seed_from_job_id``). The caller resolves them,
    because the caller owns the edit journal that records who wrote what.

    ``checkpoint`` receives every completed batch; ``resume_from`` is the
    replayed checkpoint of an interrupted run of the same session, whose
    translations are reused instead of being sent to the model again.

    ``review_edits`` are the user's changes to that run's partial result
    (hand commits, AI retranslations); they win over the checkpoint.
    """
    pipeline = TranslationPipeline(
        config,
        on_event=on_event,
        cancel_check=cancel_check,
        human_translations=human_translations,
        checkpoint=checkpoint,
        resume_from=resume_from,
        review_edits=review_edits,
    )
    if on_pipeline is not None:
        on_pipeline(pipeline)
    try:
        return await pipeline.run(scan_result=scan_result, migration=migration)
    finally:
        pipeline.close()


#: Sub-root for a source-text run's output trees. OutputGenerator wipes and
#: regenerates resourcepack/ and overrides/ on every run, so sharing one
#: root would let a W3 source-text export destroy the translated trees a
#: finished run left behind (and the other way round).
SOURCE_TEXT_DIRNAME = "source_text"


def output_root(config: PipelineConfig) -> Path:
    """Root directory holding the generated output trees."""
    root = config.output_dir or (config.modpack_path / "moru_output")
    return root / SOURCE_TEXT_DIRNAME if config.source_text_only else root


#: Statuses whose translation was produced by THIS run (vs. pre-existing).
_FRESH_STATUSES = (
    EntryStatus.PASSED,
    EntryStatus.WARNING,
    EntryStatus.MODIFIED,
    EntryStatus.TM_HIT,
    EntryStatus.MIGRATED,
)


def file_outputs(result: PipelineResult) -> list[FileOutput]:
    """Entries and persisted namespaces shared by whole-pack and per-mod output."""
    config = result.config
    # A restored session has no scan_result, so fall back to the map persisted
    # with it. Without that, a handler-extracted file whose path carries no
    # `assets/` segment loses its namespace and lands under `minecraft`.
    namespaces: dict[str, str] = dict(result.namespaces)
    if result.scan_result is not None:
        for pair in result.scan_result.all_translation_pairs:
            namespaces[pair.source_path.resolve().as_posix()] = pair.namespace
        result.namespaces = dict(namespaces)

    outputs: dict[str, FileOutput] = {}
    for entry in result.entries:
        # PENDING is stated rather than left to the empty-text check: an
        # untranslated entry must never reach output, and that should not
        # depend on a second condition happening to also be true.
        if (
            entry.status is EntryStatus.FAILED
            or entry.status is EntryStatus.PENDING
            or not entry.translated_text
        ):
            continue
        file_output = outputs.get(entry.file)
        if file_output is None:
            source_path = config.modpack_path / entry.file
            file_output = FileOutput(
                source_path=source_path,
                fresh={},
                full={},
                namespace=namespaces.get(source_path.resolve().as_posix(), ""),
            )
            outputs[entry.file] = file_output
        file_output.full[entry.key] = entry.translated_text
        if entry.status in _FRESH_STATUSES:
            file_output.fresh[entry.key] = entry.translated_text
    return list(outputs.values())


async def write_outputs(result: PipelineResult) -> GenerationResult:
    """Regenerate both installable variants from the reviewed entry state."""
    config = result.config
    outputs = file_outputs(result)

    # The description shows under the moru icon in the resource-pack UI.
    # The pack list already displays the pack's name, so the description
    # carries only the translated version + attribution, e.g.
    # "v6.5.4hotfix / §a모루§7로 번역됨 — §amoru.gg". A source-text pack has
    # the translated one's exact layout, so this line is the only in-game
    # way to tell the two apart.
    # (identity versions are pre-stripped of any leading "v" marker.)
    #
    # Kept deliberately short. That screen wraps the description to 157px
    # (151px once the list scrolls) and renders only the first TWO VISUAL
    # lines, dropping the rest — and the URL sits at the far end, so length
    # costs attribution. At 107px the translated note leaves room for even a
    # 22-character version prefix without spilling past two lines; the older
    # 143px wording ("한국어로" was redundant, the target locale is already in
    # the pack name) did not. output/mcmeta_text.py enforces the budget as a
    # backstop, but not needing the backstop keeps the version visible too.
    identity = detect_pack_identity(config.modpack_path)
    version_prefix = f"v{identity.version} / " if identity.version else ""
    note = (
        "§7원문 그대로 — §amoru.gg"
        if config.source_text_only
        else "§a모루§7로 번역됨 — §amoru.gg"
    )
    pack_format = pack_format_for_minecraft_version(
        identity.mc_version,
        config.pack_format,
    )
    generator = OutputGenerator(
        OutputConfig(
            modpack_root=config.modpack_path,
            output_dir=output_root(config),
            source_locale=config.source_locale,
            target_locale=config.target_locale,
            pack_format=pack_format,
            description=f"{version_prefix}{note}",
            bilingual_names=config.bilingual_names,
            resourcepack_seed_dir=(
                result.migration.resourcepack_assets_dir
                if result.migration is not None
                else None
            ),
        )
    )
    generation = await generator.generate(outputs)
    # Surface the one loss the installable outputs cannot absorb, so it
    # reaches the completion screen instead of dying in a log line.
    result.stats.undeliverable_jar_files = generation.skipped_jar_data
    result.stats.undeliverable_jar_entries = sum(
        loss.entry_count for loss in generation.jar_data_losses
    )
    result.stats.undeliverable_jar_mods = generation.jar_data_loss_mods
    result.output_files = generation.all_files
    return generation


async def apply_entry_edits(result: PipelineResult) -> int:
    """Regenerate outputs when entries were edited after the run.

    Review-screen mutations (manual PATCH, AI retranslate) change
    ``result.entries`` in memory but not the files the pipeline already
    wrote. Called before export/upload so the zips carry the reviewed
    state. Returns the number of files written (0 when nothing changed).
    """
    if not any(e.status is EntryStatus.MODIFIED for e in result.entries):
        return 0
    generation = await write_outputs(result)
    return len(generation.all_files)
