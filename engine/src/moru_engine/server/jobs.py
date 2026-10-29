"""Async job manager backing the sidecar's /jobs API.

The desktop GUI starts long-running work (scan / translate / export)
through ``POST /jobs`` and follows it over a WebSocket. Every job keeps an
event history, so a subscriber that connects after events already fired
first replays the past, then streams live frames. A very long history is
folded down to its snapshot except for a verbatim recent tail (see
``_compact_history``).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import shutil
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from platformdirs import user_data_dir
from pydantic import ValidationError

from .. import __version__
from ..cli_providers import is_disabled_model
from ..glossary.pair_harvester import is_untranslated_copy
from ..handlers.base import create_default_registry
from ..migration import MigrationCatalog, build_migration_catalog, logical_file_id
from ..modsources import ModSources, resolve_mod_sources
from ..output import (
    BILINGUAL_DIRNAME,
    OVERRIDES_DIRNAME,
    RESOURCEPACK_DIRNAME,
    create_zip_from_directory,
    jar_name_for,
)
from ..output.per_mod import build_mod_packs
from ..pipeline import (
    EntryStatus,
    PipelineConfig,
    PipelineResult,
    ReviewEdit,
    apply_entry_edits,
    is_translatable_text,
    output_root,
    run_pipeline,
)
from ..pipeline.checkpoint import CheckpointStore, checkpoint_identity
from ..scanner import ScanResult, scan_modpack
from ..scanner.modpack_scanner import PACK_ROOTS
from ..scanner.pack_identity import PackIdentity, detect_pack_identity
from . import upload

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine, Iterable, Mapping
    from ..graph import TranslationGraph
    from ..models import LanguageFilePair
    from ..pipeline import TranslationPipeline
    from ..pipeline.checkpoint import TranslationCheckpoint
    from .sessions import SessionStore

    #: Deferred producer of the PipelineResult an export job archives.
    ExportResultSource = Callable[[], Awaitable[PipelineResult]]

logger = logging.getLogger(__name__)

#: Concurrent handler.extract() calls during the scan parse pass.
PARSE_CONCURRENCY = 8
#: Sample entries included per file in the enriched scan payload.
SAMPLE_ENTRIES = 3
#: Sample values are truncated to this many characters.
SAMPLE_TEXT_LIMIT = 160
#: A restore snapshot keeps only a small live ticker/error tail. Per-file
#: progress, token usage and active requests are already compacted to latest.
SNAPSHOT_RECENT_ENTRIES = 24
SNAPSHOT_RECENT_FAILURES = 50
#: A run's event history is folded down to its snapshot once it grows past
#: this many frames, keeping the newest HISTORY_KEEP_RECENT verbatim. A pack
#: run emits tens of thousands of batch/entry frames that nothing reads in
#: full: the desktop restores from the snapshot and replays only after its
#: cursor, which always lands inside the verbatim tail.
HISTORY_COMPACT_AT = 20_000
HISTORY_KEEP_RECENT = 5_000
#: Frames a live subscriber may have queued unread before its stream is
#: ended (it resyncs from the snapshot) rather than buffered without bound.
SUBSCRIBER_QUEUE_LIMIT = 10_000
#: Finished, saved translate runs kept in memory; older ones are reloaded
#: from their session files on demand.
MAX_RESIDENT_FINISHED_RUNS = 8
#: Runtime-owned trees do not affect translation discovery and can change while
#: the game is open. Excluding them keeps the migration freshness check useful
#: without hiding changes to mods, configs, resource packs, or overrides.
_FINGERPRINT_IGNORED_DIRS = frozenset(
    {
        ".git",
        ".mct_cache",
        "backups",
        "crash-reports",
        "logs",
        "moru_output",
        "saves",
        "screenshots",
    }
)
#: Top-level pack directories folded into the migration freshness check.
#: The scanner's own pack roots are derived rather than repeated, so adding a
#: root there (OpenLoader/Paxi live at ``openloader/``, ``paxi/``) can never
#: silently drop out of the check and let a stale index be reused.
_MODPACK_FINGERPRINT_DIRS = tuple(
    sorted(
        {
            "config",
            "datapacks",
            "defaultconfigs",
            "kubejs",
            "mods",
            "patchouli_books",
            "resourcepacks",
            "resources",
            "scripts",
            "serverconfig",
            *(root.split("/", 1)[0] for root, _ in PACK_ROOTS),
        }
    )
)
_MODPACK_FINGERPRINT_FILES = (
    "instance.cfg",
    "manifest.json",
    "minecraftinstance.json",
    "mmc-pack.json",
    "modrinth.index.json",
)

#: Upper bound on files folded into the cached-scan staleness signal, so a
#: pathological tree cannot make the freshness check cost more than a scan.
_SCAN_SIGNATURE_MAX_ENTRIES = 200_000
#: Enriched scan results retained per process before FIFO eviction. Each one
#: holds per-file entry samples, so this is a memory bound, not a hit-rate knob.
SCAN_CACHE_LIMIT = 20


@dataclass
class FileParseMeta:
    """Per-source-file untranslated volumes from the scan parse pass."""

    entry_count: int = 0
    char_count: int = 0
    sample: dict[str, str] = field(default_factory=dict)
    parsed: bool = False
    migration_entry_count: int = 0
    migration_char_count: int = 0


@dataclass
class EnrichedScanResult:
    """Scan discovery plus parsed entry volumes (desktop scan-result screen).

    ``files`` is keyed by ``str(pair.source_path)``; files whose handler
    failed to parse stay at zero counts rather than failing the scan.
    ``identity`` is the launcher-metadata match used to prefill the
    upload form (contracts/engine-api.yaml ScanResult.identity).
    """

    scan: ScanResult
    identity: PackIdentity | None = None
    files: dict[str, FileParseMeta] = field(default_factory=dict)
    migration: MigrationCatalog | None = None
    migration_entries: int = 0
    migration_chars: int = 0
    #: Metadata snapshot of A/B/C after W2 finished. W4 may reuse the parsed
    #: scan and migration index only while this still matches the live inputs.
    migration_input_fingerprint: str | None = None


def scan_result_payload(enriched: EnrichedScanResult) -> dict[str, Any]:
    """Contract ScanResult: translatable source files grouped by category.

    Groups the scanner's translation *pairs* (actual translation units), so
    target-locale files never show up as separate rows. Volumes come from
    the scan job's parse pass; a file the parse pass could not read keeps
    zero counts.
    """
    scan = enriched.scan
    category_by_path = {
        str(Path(tf.input_path)): (tf.category or tf.file_type, tf.file_type)
        for tf in scan.translation_files
    }
    groups: dict[tuple[str, str], list[tuple[str, FileParseMeta]]] = {}
    for pair in scan.all_translation_pairs:
        path = str(pair.source_path)
        name, handler = category_by_path.get(path, ("", ""))
        if not name:
            name, handler = "Other", "other"
        meta = enriched.files.get(path) or FileParseMeta()
        # Successfully parsed files with no untranslated entries are already
        # complete in the target locale and should not inflate scan totals.
        if meta.parsed and meta.entry_count == 0:
            continue
        groups.setdefault((name, handler), []).append((path, meta))
    categories = [
        {
            "name": name,
            "handler": handler,
            "file_count": len(files),
            "entry_count": sum(m.entry_count for _, m in files),
            "char_count": sum(m.char_count for _, m in files),
            "files": [
                {
                    "path": path,
                    "entry_count": meta.entry_count,
                    "char_count": meta.char_count,
                    "sample": meta.sample,
                    **(
                        {
                            "migration_entry_count": meta.migration_entry_count,
                            "migration_char_count": meta.migration_char_count,
                        }
                        if enriched.migration is not None
                        else {}
                    ),
                }
                for path, meta in files
            ],
        }
        for (name, handler), files in sorted(groups.items())
    ]
    return {
        "modpack_path": str(scan.modpack_path),
        "categories": categories,
        # Launcher-metadata identity for upload prefill / CurseForge linking;
        # always present (folder-name fallback), null only for legacy records.
        "identity": asdict(enriched.identity) if enriched.identity else None,
        # Mods the blacklist kept out of this scan, so the scan screen can
        # show what was dropped and offer to put one back, and the source
        # strings enabled resource packs replaced, per pack.
        "excluded_mods": [asdict(mod) for mod in scan.excluded_mods],
        "source_overrides": [asdict(o) for o in scan.source_overrides],
        # Mods whose display text lives in compiled code, where no lang
        # file reaches it. Reported so an untranslated string the user
        # sees in game is attributable instead of looking like our bug.
        "hardcoded_mods": [asdict(mod) for mod in scan.hardcoded_mods],
        "migration": (
            {
                "entry_count": enriched.migration_entries,
                "char_count": enriched.migration_chars,
                "resourcepack_asset_count": (
                    enriched.migration.stats.preserved_resourcepack_assets
                ),
            }
            if enriched.migration is not None
            else None
        ),
    }


def _mod_blacklist(params: Mapping[str, Any]) -> list[str]:
    """``params.mod_blacklist`` as a clean list of ids (absent = empty)."""
    raw = params.get("mod_blacklist")
    if not isinstance(raw, list):
        return []
    return [entry.strip() for entry in raw if isinstance(entry, str) and entry.strip()]


#: Translate params never written into a checkpoint. The api_key is a secret
#: (the desktop re-sends it on resume); the rest are per-request hints a
#: resumed run recomputes, because a resume always scans afresh.
_CHECKPOINT_PARAM_EXCLUDES = frozenset(
    {"api_key", "scan_job_id", "scan_result", "session_id", "resume"}
)
#: Connection settings a resume request may supply: where and as whom to
#: reach the provider, never what or how to translate. Everything else comes
#: from the checkpoint, so the resumed output matches an uninterrupted run.
#: ``model`` is the one exception: a session whose provider was switched
#: off or logged out could otherwise never finish, so the desktop may move
#: the rest of the run to another model.
_RESUME_OVERRIDES = ("api_key", "api_base", "model")
#: Why a translate job the engine itself stopped (shutdown) is resumable.
SHUTDOWN_INTERRUPTION_MESSAGE = (
    "앱 또는 번역 엔진이 종료되어 번역이 중단되었습니다. 완료된 번역은 "
    "저장되었으니 '이어서 번역'으로 계속할 수 있습니다."
)


#: Event types that end a job's stream; the manager emits exactly one of
#: these as the final frame of every job.
TERMINAL_EVENT_TYPES = frozenset({"done", "failed", "cancelled", "interrupted"})


class JobError(Exception):
    """Base class for job-manager errors mapped to HTTP responses."""


class UnknownJobError(JobError):
    """Referenced job id does not exist (HTTP 404)."""


class JobStateError(JobError):
    """Job exists but is in the wrong state for the operation (HTTP 409)."""


class JobParamsError(JobError):
    """Job parameters failed validation (HTTP 422)."""


class JobType(str, Enum):
    SCAN = "scan"
    TRANSLATE = "translate"
    EXPORT = "export"
    UPLOAD = "upload"


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
    #: Stopped early but resumable: the provider ran out of capacity, or the
    #: engine shut down mid-run. Partial results are kept like a cancel, and
    #: the run's checkpoint stays on disk for "이어서 번역".
    INTERRUPTED = "interrupted"


@dataclass
class JobRecord:
    """One job: contract-facing fields plus execution internals."""

    id: str
    type: JobType
    params: dict[str, Any]
    status: JobStatus = JobStatus.PENDING
    error: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    #: Stored outcome: EnrichedScanResult (scan), PipelineResult (translate),
    #: dict with zip metadata (export). Preserved on cancellation.
    result: object | None = None
    #: Extra payload merged into the terminal ``done`` frame (e.g. pipeline
    #: stats, export zip path).
    done_payload: dict[str, Any] | None = None

    cancel_requested: bool = False
    finished: bool = False
    #: Monotonic cursor assigned when each event is delivered. A desktop can
    #: fetch a compact snapshot at N and then subscribe after=N without a gap.
    event_seq: int = 0
    history: list[dict[str, Any]] = field(default_factory=list)
    #: What compaction folded out of ``history`` (see _compact_history) and
    #: the snapshot still has to report: failures no longer kept as frames,
    #: and when the first batch started.
    compacted_failures: int = 0
    compacted_started_at: int | None = None
    subscribers: set[asyncio.Queue[dict[str, Any] | None]] = field(
        default_factory=set
    )
    task: asyncio.Task[None] | None = None
    #: Live pipeline while a translate job runs — /translate/{id}/graph
    #: reads its graph. Cleared when the run returns; execution-internal,
    #: never serialized (same as task/subscribers).
    pipeline: TranslationPipeline | None = None
    #: Post-run graph rebuilt once from result.entries by the graph
    #: endpoint, cached here so repeated views stay O(1).
    graph_cache: TranslationGraph | None = None
    #: Shared with its scan/translate consumers while discovery is running.
    mod_sources: ModSources = field(default_factory=ModSources)
    mod_sources_task: asyncio.Task[None] | None = None
    #: Durable per-batch record of a translate run. None for runs that make
    #: no provider call (manual seed, source text) or when it could not be
    #: opened. Execution-internal, never serialized.
    checkpoint: TranslationCheckpoint | None = None
    #: This run continues ``checkpoint`` instead of starting it over.
    resume: bool = False
    #: The engine itself stopped the job (shutdown): it ends interrupted and
    #: resumable, not as a user cancel.
    shutdown: bool = False
    #: A resume's previous run of this session, when it was still in memory:
    #: its partial result holds the review edits the resumed run must keep.
    #: Released once read. Execution-internal, never serialized.
    resume_base: PipelineResult | None = None

    def to_public(self) -> dict[str, Any]:
        """Contract ``Job`` schema representation."""
        return {
            "id": self.id,
            "type": self.type.value,
            "status": self.status.value,
            "error": self.error,
            "created_at": self.created_at.isoformat(),
        }


def _default_export_dir() -> Path:
    return Path(user_data_dir("moru", "moru")) / "exports"


_FILENAME_UNSAFE_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def _export_stem(result: PipelineResult, identity: PackIdentity) -> str:
    """Filename stem for the export zips.

    The resource-pack UI titles a zip pack by its filename, so
    "All the Mods 10 2.32 한국어 (moru)" beats a job UUID. A source-text
    export says so in its name: it carries the translated pack's exact
    layout with the untranslated strings, so the two archives must never
    collide in the exports directory.
    """
    config = result.config
    locale = config.target_locale
    parts = [
        identity.name or config.modpack_path.name,
        identity.version,
        "원문"
        if config.source_text_only
        else ("한국어" if locale == "ko_kr" else locale),
        "(moru)",
    ]
    stem = _FILENAME_UNSAFE_RE.sub(" ", " ".join(p for p in parts if p))
    return " ".join(stem.split()) or "moru-pack"


def _tree_has_files(tree: Path) -> bool:
    """True when the output tree exists and holds at least one file."""
    return tree.is_dir() and any(p.is_file() for p in tree.rglob("*"))


def _sha256_and_size(path: Path) -> tuple[str, int]:
    """Streaming digest for one archive: (hex sha256, byte size)."""
    sha = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            sha.update(chunk)
    return sha.hexdigest(), path.stat().st_size


def _update_path_metadata(
    sha: Any,
    label: str,
    path: Path | None,
    *,
    ignore_generated: bool = False,
) -> None:
    """Add a lightweight, deterministic file-tree snapshot to ``sha``."""
    sha.update(f"\0{label}\0".encode())
    if path is None:
        sha.update(b"none")
        return

    resolved = path.resolve()
    sha.update(str(resolved).encode("utf-8", errors="surrogatepass"))
    if not resolved.exists():
        sha.update(b"\0missing")
        return

    def add_file(source: Path, relative: str) -> None:
        try:
            stat_result = source.stat()
        except OSError as exc:
            sha.update(f"\0error\0{relative}\0{type(exc).__name__}".encode())
            return
        sha.update(
            f"\0file\0{relative}\0{stat_result.st_size}\0"
            f"{stat_result.st_mtime_ns}".encode()
        )

    if resolved.is_file():
        add_file(resolved, resolved.name)
        return
    if not resolved.is_dir():
        sha.update(b"\0other")
        return

    def on_walk_error(exc: OSError) -> None:
        sha.update(f"\0walk-error\0{type(exc).__name__}".encode())

    for current, directory_names, file_names in os.walk(
        resolved, topdown=True, onerror=on_walk_error, followlinks=False
    ):
        current_path = Path(current)
        directory_names[:] = sorted(
            name
            for name in directory_names
            if not (
                ignore_generated
                and (
                    name.casefold() in _FINGERPRINT_IGNORED_DIRS
                    or name.casefold().endswith(".zip_extracted")
                )
            )
            and not (current_path / name).is_symlink()
        )
        for name in sorted(file_names):
            source = current_path / name
            if source.is_symlink():
                continue
            try:
                relative = source.relative_to(resolved).as_posix()
            except ValueError:
                continue
            add_file(source, relative)


def _update_modpack_metadata(
    sha: Any,
    label: str,
    path: Path,
    *,
    old_export: bool,
) -> None:
    """Snapshot only the paths the scanner and A/C matching can consume."""
    resolved = path.resolve()
    if not resolved.is_dir():
        _update_path_metadata(sha, label, path)
        return

    scanner_root = resolved
    metadata_root = resolved
    if old_export:
        try:
            children = [
                child
                for child in resolved.iterdir()
                if child.name != "__MACOSX"
            ]
        except OSError:
            children = []
        has_pack_root = any(
            (resolved / name).is_dir() for name in _MODPACK_FINGERPRINT_DIRS
        )
        has_metadata = any(
            (resolved / name).is_file() for name in _MODPACK_FINGERPRINT_FILES
        )
        if not has_pack_root and not has_metadata:
            wrapper_dirs = [child for child in children if child.is_dir()]
            if len(children) == 1 and len(wrapper_dirs) == 1:
                metadata_root = wrapper_dirs[0]
                scanner_root = metadata_root
        if (
            (metadata_root / "manifest.json").is_file()
            and (metadata_root / "overrides").is_dir()
        ):
            scanner_root = metadata_root / "overrides"

    sha.update(f"\0{label}\0root\0{resolved}".encode("utf-8"))
    for name in _MODPACK_FINGERPRINT_FILES:
        _update_path_metadata(
            sha,
            f"{label}:metadata:{name}",
            metadata_root / name,
            ignore_generated=True,
        )
    for name in _MODPACK_FINGERPRINT_DIRS:
        _update_path_metadata(
            sha,
            f"{label}:tree:{name}",
            scanner_root / name,
            ignore_generated=True,
        )
    if not old_export:
        for game_dir in (".minecraft", "minecraft"):
            for name in _MODPACK_FINGERPRINT_FILES:
                _update_path_metadata(
                    sha,
                    f"{label}:metadata:{game_dir}:{name}",
                    metadata_root / game_dir / name,
                    ignore_generated=True,
                )
        if resolved.name.casefold() in {".minecraft", "minecraft"}:
            for name in _MODPACK_FINGERPRINT_FILES:
                _update_path_metadata(
                    sha,
                    f"{label}:metadata:parent:{name}",
                    resolved.parent / name,
                    ignore_generated=True,
                )


def _migration_inputs_fingerprint(
    *,
    modpack_path: Path,
    previous_modpack_path: Path | None,
    previous_resourcepack_path: Path | None,
    previous_overrides_path: Path | None,
) -> str:
    """Fingerprint the A/B/C inputs that produced a W2 migration index."""
    sha = hashlib.sha256(b"moru-migration-inputs-v1")
    _update_modpack_metadata(sha, "current", modpack_path, old_export=False)
    if previous_modpack_path is None:
        _update_path_metadata(sha, "old", None)
    else:
        _update_modpack_metadata(
            sha,
            "old",
            previous_modpack_path,
            old_export=True,
        )
    _update_path_metadata(sha, "resourcepack", previous_resourcepack_path)
    _update_path_metadata(sha, "overrides", previous_overrides_path)
    return sha.hexdigest()


def _migration_scan_fingerprint(params: Mapping[str, Any]) -> str:
    """Adapt raw W2 job params to the shared A/B/C fingerprint helper."""
    return _migration_inputs_fingerprint(
        modpack_path=Path(str(params["modpack_path"])),
        previous_modpack_path=(
            Path(str(params["previous_modpack_path"]))
            if params.get("previous_modpack_path")
            else None
        ),
        previous_resourcepack_path=(
            Path(str(params["previous_resourcepack_path"]))
            if params.get("previous_resourcepack_path")
            else None
        ),
        previous_overrides_path=(
            Path(str(params["previous_overrides_path"]))
            if params.get("previous_overrides_path")
            else None
        ),
    )


def _migration_assets_dir(params: Mapping[str, Any]) -> Path:
    """Directory holding one W2 scan's preserved resource-pack assets.

    A fixed path the next scan of the same inputs overwrites, mirroring the
    pipeline's own ``.migration_assets`` fallback: migration scans bypass the
    scan cache, so a fresh ``mkdtemp`` per scan would pile up a copy of the
    preserved font files for every scan in a queue run and orphan all of them
    in the temp dir if the app is force-quit. The digest separates two scans
    of the same pack with *different* previous-translation artifacts, whose
    catalogs each keep reading from their own directory.
    """
    digest = hashlib.sha256(
        "\0".join(
            (
                str(params.get("previous_modpack_path") or ""),
                str(params.get("previous_resourcepack_path") or ""),
                str(params.get("previous_overrides_path") or ""),
                str(params.get("target_locale", "ko_kr")),
            )
        ).encode("utf-8", errors="surrogatepass")
    ).hexdigest()[:12]
    root = output_root(
        PipelineConfig(
            modpack_path=Path(str(params["modpack_path"])),
            output_dir=(
                Path(str(params["output_dir"])) if params.get("output_dir") else None
            ),
        )
    )
    return root / f".migration_assets-{digest}"


def _reduce_history(
    history: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int, int | None, list[dict[str, Any]]]:
    """(frames, failed_count, translation_started_at, other) of a history.

    ``frames`` is what a snapshot shows: latest progress per file/stage,
    unfinished batches, cumulative token usage, recent ticker/error samples
    and the terminal frame, in seq order. ``other`` holds frames of types
    the reduction does not know, which the snapshot leaves out.
    """
    progress: dict[tuple[str, str], dict[str, Any]] = {}
    tokens: dict[str, Any] | None = None
    glossary_progress: dict[str, Any] | None = None
    glossary_extracted: dict[str, Any] | None = None
    active_batches: dict[int, dict[str, Any]] = {}
    translation_started_at: int | None = None
    recent_entries: list[dict[str, Any]] = []
    recent_failures: list[dict[str, Any]] = []
    other: list[dict[str, Any]] = []
    failed_count = 0
    terminal: dict[str, Any] | None = None

    for frame in history:
        event_type = frame.get("type")
        if event_type == "progress":
            key = (str(frame.get("stage", "")), str(frame.get("file", "")))
            progress[key] = frame
        elif event_type == "tokens":
            tokens = frame
        elif event_type == "glossary_progress":
            glossary_progress = frame
        elif event_type == "glossary_extracted":
            glossary_extracted = frame
        elif event_type == "batch_started":
            if translation_started_at is None:
                translation_started_at = int(frame.get("emitted_at", 0)) or None
            active_batches[int(frame["request_id"])] = frame
        elif event_type == "batch_finished":
            active_batches.pop(int(frame["request_id"]), None)
        elif event_type == "entry_done":
            recent_entries.append(frame)
            recent_entries = recent_entries[-SNAPSHOT_RECENT_ENTRIES:]
        elif event_type == "entry_failed":
            failed_count += 1
            recent_failures.append(frame)
            recent_failures = recent_failures[-SNAPSHOT_RECENT_FAILURES:]
        elif event_type in TERMINAL_EVENT_TYPES:
            terminal = frame
        else:
            other.append(frame)

    frames = [
        *progress.values(),
        *active_batches.values(),
        *recent_entries,
        *recent_failures,
    ]
    for optional in (tokens, glossary_progress, glossary_extracted, terminal):
        if optional is not None:
            frames.append(optional)
    frames.sort(key=lambda frame: int(frame.get("seq", 0)))
    return frames, failed_count, translation_started_at, other


def _compact_history(record: JobRecord) -> None:
    """Fold all but the newest frames into their snapshot reduction.

    The snapshot of the compacted history equals the snapshot of the full
    one: what the reduction drops is either superseded (older progress,
    finished batches) or counted on the record (failures, start time).
    Replay after any cursor inside the verbatim tail stays gap-free.
    """
    old = record.history[:-HISTORY_KEEP_RECENT]
    recent = record.history[-HISTORY_KEEP_RECENT:]
    frames, failed, started, other = _reduce_history(old)
    kept_failures = sum(1 for frame in frames if frame.get("type") == "entry_failed")
    record.compacted_failures += failed - kept_failures
    if record.compacted_started_at is None:
        record.compacted_started_at = started
    # Frame types the reduction does not know are rare; keep them as-is.
    merged = sorted([*frames, *other], key=lambda frame: int(frame.get("seq", 0)))
    record.history = [*merged, *recent]


class JobManager:
    """Creates, runs, cancels, and streams jobs on the server event loop."""

    def __init__(
        self,
        glossary_store_dir: Path | None = None,
        session_store: SessionStore | None = None,
        checkpoint_store: CheckpointStore | None = None,
    ) -> None:
        self._jobs: dict[str, JobRecord] = {}
        #: Finished translate runs whose session file was saved, oldest
        #: first: the candidates _evict_finished may drop from memory.
        self._persisted_order: list[str] = []
        #: Enriched scan results keyed by pack path, locales, and a cheap
        #: tree signature, so an edited pack rescans instead of replaying
        #: stale counts into the cost estimate.
        self._scan_cache: dict[str, EnrichedScanResult] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        #: Injected by the app so pipeline runs read the same user glossary
        #: store (hub manual terms + synced community terms) the HTTP
        #: endpoints write.
        self._glossary_store_dir = glossary_store_dir
        if session_store is None:
            # Deferred: sessions imports JobRecord/JobStatus/JobType from here.
            from .sessions import SessionStore as _SessionStore

            session_store = _SessionStore()
        self._session_store: SessionStore = session_store
        #: Per-session translate checkpoints. Beside the sessions directory by
        #: default, so whichever root the session store uses — the app data
        #: dir in production, a temp dir in tests — holds both.
        self._checkpoints = checkpoint_store or CheckpointStore(
            session_store.sessions_dir.parent / "checkpoints"
        )
        #: Scratch dirs this process owns, wiped on shutdown. The migration
        #: asset paths are deterministic, so a re-scan re-registers the same
        #: entry instead of adding one per scan.
        self._temporary_dirs: set[Path] = set()
        #: Resolves a job id to that session's committed hand translations.
        #: Injected by the app, because the edit journal lives in the HTTP
        #: layer's ManualStore — the job manager must not grow its own reader
        #: for it.
        self._manual_resolver: (
            Callable[[str], dict[str, dict[str, str]]] | None
        ) = None

    def set_manual_resolver(
        self, resolver: Callable[[str], dict[str, dict[str, str]]]
    ) -> None:
        """Wire the hand-translation lookup used by ``seed_from_job_id``."""
        self._manual_resolver = resolver

    @property
    def session_store(self) -> SessionStore:
        """Persistent session store backing restore/export/import."""
        return self._session_store

    @property
    def checkpoints(self) -> CheckpointStore:
        """Durable per-batch translate checkpoints (resume support)."""
        return self._checkpoints

    # -- lookup --------------------------------------------------------------

    def get(self, job_id: str) -> JobRecord:
        """In-memory job, else one restored from its persisted session file."""
        if job_id in self._jobs:
            return self._jobs[job_id]
        restored = self._session_store.load_job_session(job_id)
        if restored is not None:
            self._jobs[job_id] = restored
            if restored.type is JobType.TRANSLATE:
                # Browsing the history reloads runs one by one; they are as
                # evictable as the runs that finished here.
                self._persisted_order.append(job_id)
                self._evict_finished(keep=job_id)
            return restored
        raise UnknownJobError(f"unknown job: {job_id}")

    def register_job(self, record: JobRecord) -> None:
        """Adopt a record built outside the manager (session import)."""
        self._jobs[record.id] = record

    def get_mod_sources(self, job_id: str) -> ModSources:
        """Scan identities, including snapshots surviving a sidecar restart."""
        try:
            record = self.get(job_id)
        except UnknownJobError:
            saved = self._session_store.load_mod_sources(job_id)
            if saved is not None:
                return saved
            raise
        if record.type not in (JobType.SCAN, JobType.TRANSLATE):
            raise JobStateError(f"job {job_id} has no mod sources")
        if record.status is JobStatus.DONE and record.mod_sources.status == "pending":
            self._start_mod_sources(record)
        return record.mod_sources

    def _start_mod_sources(self, record: JobRecord) -> None:
        for related in self._jobs.values():
            task = related.mod_sources_task
            if related.mod_sources is record.mod_sources and task is not None and not task.done():
                record.mod_sources_task = task
                return
        record.mod_sources.status = "running"
        record.mod_sources_task = asyncio.create_task(
            self._resolve_mod_sources(record), name=f"moru-mod-sources-{record.id}"
        )

    async def _resolve_mod_sources(self, record: JobRecord) -> None:
        try:
            await resolve_mod_sources(
                Path(str(record.params["modpack_path"])), record.mod_sources
            )
        except asyncio.CancelledError:
            record.mod_sources.status = "failed"
            raise
        except Exception:
            record.mod_sources.status = "failed"
            logger.exception("Mod identity lookup failed for %s", record.id)
        finally:
            try:
                self._session_store.save_mod_sources(record.id, record.mod_sources)
                for related in self._jobs.values():
                    if related.finished and related.mod_sources is record.mod_sources:
                        self._session_store.save_job_session(related)
            except Exception:
                logger.exception("Failed to persist mod sources for %s", record.id)

    async def _await_mod_sources(self, source: JobRecord) -> ModSources:
        if source.mod_sources.status in ("pending", "running"):
            self._start_mod_sources(source)
        task = source.mod_sources_task
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=120)
            except TimeoutError:
                logger.warning("Mod identity lookup timed out for upload of %s", source.id)
        return source.mod_sources

    def forget_job(self, job_id: str) -> None:
        """Drop a record from memory; its session file is left alone."""
        self._jobs.pop(job_id, None)

    def forget_finished_job(self, job_id: str) -> None:
        """:meth:`forget_job`, refused for a live run: forgetting one would
        orphan it — it keeps spending tokens and its next checkpoint append
        recreates the file the caller is about to delete."""
        record = self._jobs.get(job_id)
        if record is not None and not record.finished:
            raise JobStateError(f"job {job_id} is still running")
        self.forget_job(job_id)

    # -- checkpoints -----------------------------------------------------------

    async def list_checkpoints(self) -> list[dict[str, Any]]:
        """Resumable translate checkpoints; a run still live here is marked
        ``running`` so a reloaded desktop reattaches instead of resuming."""
        live = {
            job_id
            for job_id, record in self._jobs.items()
            if record.type is JobType.TRANSLATE and not record.finished
        }
        # Reads every checkpoint file, so off-loop like GET /sessions.
        summaries = await asyncio.to_thread(self._checkpoints.summaries)
        for summary in summaries:
            if summary["session_id"] in live:
                summary["state"] = JobStatus.RUNNING.value
        return summaries

    async def delete_checkpoint(self, session_id: str) -> bool:
        """Discard a session's checkpoint. A live run still writes to its
        own, so that is refused rather than left half-deleted."""
        record = self._jobs.get(session_id)
        if record is not None and not record.finished:
            raise JobStateError(f"job {session_id} is still running")
        # Off-loop: a delete retries briefly while another reader holds the
        # file (see TranslationCheckpoint.delete).
        return await asyncio.to_thread(self._checkpoints.delete, session_id)

    # -- creation ------------------------------------------------------------

    def create_job(self, type_: str, params: Mapping[str, Any]) -> JobRecord:
        """Validate params, register the job, and start it as a task.

        Must be called from within the running event loop (route handlers).
        """
        job_type = JobType(type_)
        record = JobRecord(
            id=self._resolve_job_id(params), type=job_type, params=dict(params)
        )
        runner: Coroutine[Any, Any, object]
        if job_type is JobType.SCAN:
            self._require_modpack_path(record.params)
            self._require_migration_params(record.params)
            runner = self._run_scan(record)
        elif job_type is JobType.TRANSLATE:
            record.resume = bool(record.params.pop("resume", False))
            if record.resume:
                record.params = self._resume_params(record)
                previous = self._jobs.get(record.id)
                if (
                    previous is not None
                    and previous.type is JobType.TRANSLATE
                    and isinstance(previous.result, PipelineResult)
                ):
                    record.resume_base = previous.result
            self._require_modpack_path(record.params)
            config_params = dict(record.params)
            scan_job_id = config_params.pop("scan_job_id", None)
            try:
                config = PipelineConfig(**config_params)
            except ValidationError as exc:
                raise JobParamsError(f"invalid translate params: {exc}") from exc
            if not config.source_text_only and is_disabled_model(config.model):
                # Every call would be refused; fail the request, not the run.
                raise JobParamsError(
                    f"provider of {config.model} is switched off; choose another model"
                )
            if config.glossary_store_dir is None:
                config.glossary_store_dir = self._glossary_store_dir
            scan_record = self._jobs.get(str(scan_job_id)) if scan_job_id else None
            if scan_record is not None and scan_record.type is JobType.SCAN:
                record.mod_sources = scan_record.mod_sources
                record.mod_sources_task = scan_record.mod_sources_task
            self._attach_scan_payload(record)
            scan_source = self._resolve_scan_source(scan_job_id, config)
            # Last, after every validation that can refuse the request: a
            # rejected fresh run must not have discarded the checkpoint.
            record.checkpoint = self._open_checkpoint(record, config)
            runner = self._run_pipeline(record, config, scan_source)
        elif job_type is JobType.EXPORT:
            runner = self._run_export(record, self._export_result_source(record))
        else:  # JobType.UPLOAD
            if not record.params.get("modpack_name"):
                raise JobParamsError("params.modpack_name is required")
            curseforge_id = record.params.get("curseforge_id")
            if curseforge_id is not None and (
                isinstance(curseforge_id, bool)
                or not isinstance(curseforge_id, int)
                or curseforge_id <= 0
            ):
                raise JobParamsError(
                    "params.curseforge_id must be a positive integer"
                )
            source = self._resolve_translate_source(record.params, "upload")
            runner = self._run_upload(record, source)

        self._jobs[record.id] = record
        record.task = asyncio.create_task(
            self._run(record, runner), name=f"moru-job-{record.id}"
        )
        return record

    @staticmethod
    def _require_modpack_path(params: Mapping[str, Any]) -> None:
        path = params.get("modpack_path")
        if not path:
            raise JobParamsError("params.modpack_path is required")
        if not Path(str(path)).exists():
            raise JobParamsError(f"modpack_path does not exist: {path}")

    @staticmethod
    def _require_migration_params(params: Mapping[str, Any]) -> None:
        """Validate the optional A/B inputs of a scan job.

        Checked here rather than inside the scan coroutine so a malformed
        request is a 422 from ``POST /jobs``, not a created job that fails
        the moment its task runs.
        """
        requested = (
            params.get("previous_modpack_path"),
            params.get("previous_resourcepack_path"),
            params.get("previous_overrides_path"),
        )
        if all(value is None for value in requested):
            return
        if not params.get("previous_modpack_path"):
            raise JobParamsError(
                "params.previous_modpack_path is required for translation migration"
            )
        if not (
            params.get("previous_resourcepack_path")
            or params.get("previous_overrides_path")
        ):
            raise JobParamsError(
                "a previous resource pack or overrides artifact is required"
            )

    def _resolve_job_id(self, params: Mapping[str, Any]) -> str:
        """Job id, taken from a caller-supplied ``session_id`` when present.

        The id becomes a session filename, so only UUID-shaped values are
        accepted. Reusing a *live* job's id would leave its task running
        with no owner and drop its subscribers, so that is refused; reusing
        a finished session's id is how a retry keeps its history row.
        """
        supplied = params.get("session_id")
        if not supplied:
            return str(uuid.uuid4())
        try:
            job_id = str(uuid.UUID(str(supplied)))
        except ValueError as exc:
            raise JobParamsError(
                f"params.session_id must be a UUID: {supplied!r}"
            ) from exc
        existing = self._jobs.get(job_id)
        if existing is not None and not existing.finished:
            raise JobStateError(f"job {job_id} is still running")
        return job_id

    def _resume_params(self, record: JobRecord) -> dict[str, Any]:
        """Params of the interrupted run a resume continues.

        They come from the checkpoint, not the request: only connection
        settings (``_RESUME_OVERRIDES``) may be re-supplied, because
        everything that shapes the output has to be the original run's for
        the resumed result to equal an uninterrupted one.
        """
        if not record.params.get("session_id"):
            raise JobParamsError("params.session_id is required to resume")
        header = self._checkpoints.for_session(record.id).read_header()
        stored = header.get("params") if header is not None else None
        if not isinstance(stored, dict):
            raise UnknownJobError(f"session {record.id} has no resumable checkpoint")
        overrides = {
            key: record.params[key]
            for key in _RESUME_OVERRIDES
            if record.params.get(key) is not None
        }
        restored = {**stored, **overrides, "session_id": record.id}
        # Kept beside the params (see _open_checkpoint). The scan cache is
        # gone after a relaunch, and without this payload the resumed
        # session would save no scan screen and lose its pack identity.
        if header.get("scan_result"):
            restored["scan_result"] = header["scan_result"]
        return restored

    def _open_checkpoint(
        self, record: JobRecord, config: PipelineConfig
    ) -> TranslationCheckpoint | None:
        """Start (fresh run) or continue (resume) the run's checkpoint.

        A fresh run replaces whatever an earlier run of this session left:
        starting over is an explicit choice, and silently reusing old
        translations under it would not be.
        """
        checkpoint = self._checkpoints.for_session(record.id)
        if config.source_text_only or config.manual_seed:
            # No provider call, so nothing paid for that could be lost. The
            # run still replaces the session's work, so an earlier AI run's
            # checkpoint must not stay behind and resurface as "이어서 번역".
            try:
                checkpoint.delete()
            except OSError:
                logger.exception(
                    "Could not discard the checkpoint of session %s", record.id
                )
            return None
        identity = checkpoint_identity(
            config.modpack_path, config.source_locale, config.target_locale
        )
        if record.resume:
            stored = checkpoint.read_header()
            if stored is None or stored.get("identity") != identity:
                raise JobStateError(
                    f"the checkpoint of session {record.id} belongs to a "
                    "different modpack or locale"
                )
        scan_payload = record.params.get("scan_result")
        pack_identity = (
            scan_payload.get("identity") if isinstance(scan_payload, dict) else None
        )
        pack_name = (
            pack_identity.get("name") if isinstance(pack_identity, dict) else None
        )
        header = {
            "session_id": record.id,
            "identity": identity,
            "modpack_path": str(config.modpack_path),
            "modpack_name": pack_name or config.modpack_path.name,
            "source_locale": config.source_locale,
            "target_locale": config.target_locale,
            "model": config.model,
            "params": {
                key: value
                for key, value in record.params.items()
                if key not in _CHECKPOINT_PARAM_EXCLUDES
            },
        }
        if scan_payload and not record.resume:
            # Beside the params, never in them: a resume replays params as
            # pipeline config, and the first begin record is the one read.
            header["scan_result"] = scan_payload
        try:
            if record.resume:
                checkpoint.resume(header)
            else:
                checkpoint.start(header)
        except OSError:
            logger.exception("Could not open the checkpoint of job %s", record.id)
            if not record.resume:
                # A fresh run must never append under the header an earlier
                # run left behind: a later resume would take that run's
                # translations for this one's. Clear it and start once more;
                # failing that, this run goes without a checkpoint.
                try:
                    checkpoint.delete()
                    checkpoint.start(header)
                except OSError:
                    logger.exception("Job %s runs without a checkpoint", record.id)
                    return None
            # A resume degrades instead: its recorded batches are still on
            # disk, and every later batch write retries the disk on its own.
        return checkpoint

    async def _review_edits(
        self, record: JobRecord
    ) -> dict[str, dict[str, ReviewEdit]]:
        """What the user changed on the partial result a resume continues.

        The checkpoint holds the model's text. Review edits made afterwards —
        AI retranslations and hand commits — must win over it, or the resumed
        run would ship the machine text again and its final save would
        overwrite the session file that held them. The previous result comes
        from memory, else from the session file (snapshot + journal replay).
        """
        previous = record.resume_base
        record.resume_base = None  # read once; a large result is not kept
        if previous is None:
            restored = await asyncio.to_thread(
                self._session_store.load_job_session, record.id
            )
            if restored is not None and isinstance(restored.result, PipelineResult):
                previous = restored.result
        edits: dict[str, dict[str, ReviewEdit]] = {}
        if previous is not None:
            for entry in previous.entries:
                if (
                    entry.status is EntryStatus.MODIFIED
                    and entry.translated_text is not None
                ):
                    edits.setdefault(entry.file, {})[entry.key] = ReviewEdit(
                        entry.translated_text, entry.source_text
                    )
        # Hand commits by provenance, exactly as seed_from_job_id adopts an
        # earlier session's: the journal is the newest record of them.
        if self._manual_resolver is not None:
            for file, texts in self._manual_resolver(record.id).items():
                for key, text in texts.items():
                    edits.setdefault(file, {})[key] = ReviewEdit(text)
        return edits

    def _attach_scan_payload(self, record: JobRecord) -> None:
        """Copy the scan-screen payload onto the translate record.

        Only translate jobs are persisted and the scan job keeps its own
        id, so reopening a session can replay the scan screen only if the
        payload rides along on the translate record.
        """
        if record.params.get("scan_result"):
            return
        enriched: EnrichedScanResult | None = None
        scan_job_id = record.params.get("scan_job_id")
        if scan_job_id:
            scan_record = self._jobs.get(str(scan_job_id))
            if scan_record is not None and scan_record.type is JobType.SCAN:
                cached = scan_record.params.get("scan_result")
                if cached:
                    record.params["scan_result"] = cached
                    return
                if isinstance(scan_record.result, EnrichedScanResult):
                    enriched = scan_record.result
        if enriched is None:
            enriched = self._scan_cache.get(self._scan_cache_key(record.params))
        if enriched is not None:
            record.params["scan_result"] = scan_result_payload(enriched)

    def _scan_cache_key(self, params: Mapping[str, Any]) -> str:
        modpack_path = Path(str(params["modpack_path"])).resolve()
        return ":".join(
            (
                str(modpack_path),
                str(params.get("source_locale", "en_us")),
                str(params.get("target_locale", "ko_kr")),
                # The blacklist changes which mods the scan even looks at,
                # so editing it has to miss the cache — otherwise the scan
                # screen keeps replaying the counts from the old list.
                ",".join(sorted(_mod_blacklist(params))),
                self._scan_tree_signature(modpack_path),
            )
        )

    @staticmethod
    def _scan_tree_signature(modpack_path: Path) -> str:
        """Staleness signal for one pack's cached scan.

        Walks the pack with ``os.scandir`` and folds file count, newest
        mtime, and total size into one string. Stat-only, so it costs a
        fraction of a scan (which extracts every jar and parses the files
        inside), yet still notices a mod added deep in the tree.

        Dot-directories are skipped: the scanner unpacks archives into the
        pack's own ``.mct_cache``, and counting that would change the
        signature on every scan and defeat the cache entirely. So are the
        runtime and output trees the migration fingerprint ignores
        (``_FINGERPRINT_IGNORED_DIRS``): a world save, a log line or our
        own ``moru_output`` changes nothing a scan finds, yet each used to
        throw the cached scan away.
        """
        count = 0
        newest = 0
        total = 0
        stack = [modpack_path]
        while stack and count < _SCAN_SIGNATURE_MAX_ENTRIES:
            try:
                entries = list(os.scandir(stack.pop()))
            except OSError:
                continue
            for entry in entries:
                if entry.name.startswith("."):
                    continue
                try:
                    if entry.is_dir(follow_symlinks=False):
                        folded = entry.name.casefold()
                        if (
                            folded in _FINGERPRINT_IGNORED_DIRS
                            or folded.endswith(".zip_extracted")
                        ):
                            continue
                        stack.append(Path(entry.path))
                        continue
                    stat = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                count += 1
                newest = max(newest, stat.st_mtime_ns)
                total += stat.st_size
        return f"{count}:{newest}:{total}"

    def _resolve_translate_source(
        self, params: Mapping[str, Any], purpose: str
    ) -> JobRecord:
        """Resolve a completed, user-cancelled or interrupted translate job
        with results (the latter two hold a reviewable partial result)."""
        translate_job_id = params.get("translate_job_id")
        if not translate_job_id:
            raise JobParamsError("params.translate_job_id is required")
        source = self.get(str(translate_job_id))
        if source.type is not JobType.TRANSLATE:
            raise JobStateError(
                f"job {source.id} is a {source.type.value} job, not translate"
            )
        if source.status not in (
            JobStatus.DONE,
            JobStatus.CANCELLED,
            JobStatus.INTERRUPTED,
        ) or not isinstance(
            source.result, PipelineResult
        ):
            raise JobStateError(
                f"translate job {source.id} is {source.status.value}; "
                f"{purpose} requires a completed translate job"
            )
        return source

    def _export_result_source(self, record: JobRecord) -> ExportResultSource:
        """Where an export job's entries come from, validated up front.

        Two entry points, one archive path (``_run_export``):

        - W6: a completed (or user-cancelled) translate job, whose
          review-screen edits are folded back into its output trees.
        - W3 (``params.source_text``): a source-text run over the scanned
          modpack. It drives the very same pipeline and output generator,
          only with every entry settled to its own source text, so it
          needs neither a translate job nor an API key.

        Validation happens here rather than in the runner so a bad request
        fails ``POST /jobs`` with 404/409/422 instead of starting a job
        that dies immediately.
        """
        params = record.params
        if params.get("source_text"):
            self._require_modpack_path(params)
            try:
                config = PipelineConfig(**{**params, "source_text_only": True})
            except ValidationError as exc:
                raise JobParamsError(
                    f"invalid source-text export params: {exc}"
                ) from exc
            # A source-text run settles every entry to its own source text,
            # so it never reuses a scan result or an A/B/C migration index.
            return lambda: self._run_pipeline(record, config, None)
        source = self._resolve_translate_source(params, "export")
        return lambda: self._apply_review_edits(source, "export")

    def _resolve_scan_source(
        self,
        scan_job_id: object,
        config: PipelineConfig,
    ) -> JobRecord | None:
        """Resolve a matching migration-enabled W2 scan for possible reuse.

        A scan result is only an optimization. The renderer sends its last
        scan id unconditionally and scan jobs are never persisted, so any
        sidecar restart makes that id unresolvable; anything that does not
        line up therefore degrades to a fresh scan (``None``) exactly like a
        stale ``migration_input_fingerprint`` does, never to a failed Start.
        """
        if scan_job_id is None:
            return None
        try:
            source = self.get(str(scan_job_id))
        except UnknownJobError:
            logger.info(
                "Scan job %s is gone; rebuilding the W4 scan and A/B/C index",
                scan_job_id,
            )
            return None
        if source.type is not JobType.SCAN:
            raise JobStateError(
                f"job {source.id} is a {source.type.value} job, not scan"
            )
        if source.status is not JobStatus.DONE or not isinstance(
            source.result, EnrichedScanResult
        ):
            logger.info(
                "Scan job %s is %s with no reusable result; rebuilding the W4 "
                "scan and A/B/C index",
                source.id,
                source.status.value,
            )
            return None
        expected = {
            "modpack_path": str(config.modpack_path),
            "source_locale": config.source_locale,
            "target_locale": config.target_locale,
            "previous_modpack_path": (
                str(config.previous_modpack_path)
                if config.previous_modpack_path is not None
                else None
            ),
            "previous_resourcepack_path": (
                str(config.previous_resourcepack_path)
                if config.previous_resourcepack_path is not None
                else None
            ),
            "previous_overrides_path": (
                str(config.previous_overrides_path)
                if config.previous_overrides_path is not None
                else None
            ),
        }
        for key, value in expected.items():
            actual = source.params.get(key)
            if key in {"source_locale", "target_locale"} and actual is None:
                actual = "en_us" if key == "source_locale" else "ko_kr"
            if (str(actual) if actual is not None else None) != value:
                logger.info(
                    "Scan job %s does not match translate param %s; rebuilding "
                    "the W4 scan and A/B/C index",
                    source.id,
                    key,
                )
                return None
        return source

    # -- cancellation ----------------------------------------------------------

    def cancel(self, job_id: str) -> JobRecord:
        """Request cancellation; partial results stay on the record."""
        record = self.get(job_id)
        record.cancel_requested = True
        if record.task is not None and not record.task.done():
            record.task.cancel()
        return record

    async def aclose(self) -> None:
        """Cancel every running job (server shutdown)."""
        tasks = {
            task
            for record in self._jobs.values()
            for task in (record.task, record.mod_sources_task)
            if task is not None and not task.done()
        }
        for record in self._jobs.values():
            if not record.finished:
                # Not the user's choice: a running translation ends
                # interrupted and resumable rather than cancelled.
                record.shutdown = True
            record.cancel_requested = True
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for directory in self._temporary_dirs:
            shutil.rmtree(directory, ignore_errors=True)
        self._temporary_dirs.clear()

    # -- event stream ----------------------------------------------------------

    def subscribe(
        self, job_id: str, after: int | None = None
    ) -> tuple[list[dict[str, Any]], asyncio.Queue[dict[str, Any] | None] | None]:
        """Return (history snapshot, live queue).

        The queue is None when the job already finished: history then ends
        with the terminal frame and there is nothing live to wait for.
        Registration and snapshot happen in one synchronous step on the
        event loop thread, so no frame can fall between them.
        """
        record = self.get(job_id)
        history = [
            frame
            for frame in record.history
            if after is None or int(frame.get("seq", 0)) > after
        ]
        if record.finished:
            return history, None
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        record.subscribers.add(queue)
        return history, queue

    def snapshot(self, job_id: str) -> dict[str, Any]:
        """Return compact recoverable UI state plus a gap-free event cursor.

        Translation jobs can emit tens of thousands of batch frames. Replaying
        all of them into React when a user returns from another tab is both
        unnecessary and visibly stalls the renderer. This reduction retains
        the latest progress per file/stage, current requests, cumulative token
        usage, recent ticker/error samples and the terminal frame.
        """
        record = self.get(job_id)
        frames, failed_count, translation_started_at, _ = _reduce_history(record.history)
        if record.compacted_started_at is not None:
            translation_started_at = record.compacted_started_at
        return {
            "job": record.to_public(),
            "cursor": record.event_seq,
            "events": frames,
            "failed_count": failed_count + record.compacted_failures,
            "translation_started_at": translation_started_at,
        }

    @staticmethod
    def refresh_translate_stats(record: JobRecord) -> dict[str, Any]:
        """Keep terminal/snapshot stats aligned with post-run review edits."""
        if not isinstance(record.result, PipelineResult):
            raise JobStateError(f"translate job {record.id} has no pipeline result")
        stats = record.result.stats.model_dump()
        record.done_payload = {"stats": stats}
        for frame in reversed(record.history):
            if frame.get("type") in TERMINAL_EVENT_TYPES:
                frame["stats"] = stats
                break
        return stats

    def unsubscribe(
        self, job_id: str, queue: asyncio.Queue[dict[str, Any] | None]
    ) -> None:
        record = self._jobs.get(job_id)
        if record is not None:
            record.subscribers.discard(queue)

    def _emit(
        self, record: JobRecord, event_type: str, payload: Mapping[str, object]
    ) -> None:
        """Append a frame to history and fan it out to live subscribers.

        Pipeline/scanner ``on_event`` callbacks fire synchronously from
        coroutines running on this process's single event loop, so touching
        the history list and calling ``Queue.put_nowait`` directly is safe:
        we are already on the loop thread and nothing else can interleave.
        ``asyncio.Queue`` is *not* thread-safe, though, so if a callback ever
        arrives from a worker thread (code under ``asyncio.to_thread``), we
        hop back onto the loop with ``call_soon_threadsafe`` instead.
        """
        frame: dict[str, Any] = {"type": event_type, **payload}
        loop = self._loop
        try:
            running: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if loop is None or running is loop:
            self._deliver(record, frame)
        else:
            loop.call_soon_threadsafe(self._deliver, record, frame)

    @staticmethod
    def _deliver(record: JobRecord, frame: dict[str, Any]) -> None:
        record.event_seq += 1
        frame = {
            **frame,
            "seq": record.event_seq,
            "emitted_at": int(time.time() * 1000),
        }
        record.history.append(frame)
        if len(record.history) > HISTORY_COMPACT_AT:
            _compact_history(record)
        for queue in list(record.subscribers):
            if queue.qsize() >= SUBSCRIBER_QUEUE_LIMIT:
                # A subscriber this far behind is not reading. Dropping
                # frames would silently skew its counts, so end its stream
                # instead: the desktop resyncs from the snapshot on close.
                record.subscribers.discard(queue)
                queue.put_nowait(None)
                continue
            queue.put_nowait(frame)

    def _evict_finished(self, keep: str | None = None) -> None:
        """Drop the oldest finished, persisted translate runs from memory.

        Each holds its full PipelineResult and event history, and nothing
        evicted them, so a long-lived engine grew with every run. They are
        not lost: ``get()`` reloads one from its session file on demand.
        """
        resident = [
            job_id
            for job_id in self._persisted_order
            if (record := self._jobs.get(job_id)) is not None
        ]
        self._persisted_order = resident
        excess = len(resident) - MAX_RESIDENT_FINISHED_RUNS
        for job_id in list(resident):
            if excess <= 0:
                break
            record = self._jobs[job_id]
            busy = (
                # Only a completed run: an interrupted or cancelled one may
                # be resumed, and a resume reads its predecessor from here.
                job_id == keep
                or record.status is not JobStatus.DONE
                or not record.finished
                or record.subscribers
                or (record.task is not None and not record.task.done())
                or (
                    record.mod_sources_task is not None
                    and not record.mod_sources_task.done()
                )
            )
            if busy:
                continue
            self._jobs.pop(job_id, None)
            self._persisted_order.remove(job_id)
            excess -= 1

    # -- execution ---------------------------------------------------------------

    async def _run(
        self, record: JobRecord, runner: Coroutine[Any, Any, object]
    ) -> None:
        self._loop = asyncio.get_running_loop()
        record.status = JobStatus.RUNNING
        terminal_type: str
        terminal: dict[str, Any]
        try:
            result = await runner
        except asyncio.CancelledError:
            # Both task.cancel() and the pipeline's cancel_check land here.
            if record.shutdown and record.type is JobType.TRANSLATE:
                terminal_type, terminal = self._interrupted(
                    record, "shutdown", SHUTDOWN_INTERRUPTION_MESSAGE
                )
            else:
                record.status = JobStatus.CANCELLED
                terminal_type, terminal = "cancelled", {"status": "cancelled"}
            logger.info("Job %s (%s) cancelled", record.id, record.type.value)
        except Exception as exc:
            record.status = JobStatus.FAILED
            record.error = str(exc)
            terminal_type = "failed"
            terminal = {"status": "failed", "error": str(exc)}
            logger.exception("Job %s (%s) failed", record.id, record.type.value)
        else:
            record.result = result
            interruption = (
                result.interruption if isinstance(result, PipelineResult) else None
            )
            if interruption is not None:
                terminal_type, terminal = self._interrupted(
                    record,
                    interruption.reason,
                    interruption.message,
                    interruption.detail,
                )
            elif record.cancel_requested:
                if record.shutdown and record.type is JobType.TRANSLATE:
                    terminal_type, terminal = self._interrupted(
                        record, "shutdown", SHUTDOWN_INTERRUPTION_MESSAGE
                    )
                else:
                    record.status = JobStatus.CANCELLED
                    terminal_type, terminal = "cancelled", {"status": "cancelled"}
            else:
                record.status = JobStatus.DONE
                terminal_type, terminal = "done", {"status": "done"}
            if record.done_payload:
                terminal.update(record.done_payload)
        if record.checkpoint is not None:
            terminal["resumable"] = self._close_checkpoint(
                record, terminal.get("reason")
            )
        self._emit(record, terminal_type, terminal)
        record.finished = True
        if (
            record.status is JobStatus.DONE
            and record.type in (JobType.SCAN, JobType.TRANSLATE)
            and record.mod_sources.status == "pending"
        ):
            self._start_mod_sources(record)
        if record.type is JobType.TRANSLATE:
            saved = False
            try:
                saved = self._session_store.save_job_session(record) is not None
            except Exception:
                logger.exception("Failed to save session for job %s", record.id)
            if (
                saved
                and record.status is JobStatus.DONE
                and record.checkpoint is not None
            ):
                # Only now does the session file hold the complete result. A
                # finished run's checkpoint must not be offered for resuming,
                # but until the save landed it was the only durable copy.
                try:
                    await asyncio.to_thread(record.checkpoint.delete)
                except OSError:
                    logger.exception(
                        "Failed to remove the checkpoint of job %s", record.id
                    )
            if saved:
                if record.id in self._persisted_order:
                    self._persisted_order.remove(record.id)
                self._persisted_order.append(record.id)
        for queue in record.subscribers:
            queue.put_nowait(None)  # stream-end sentinel for live listeners
        self._evict_finished()

    @staticmethod
    def _interrupted(
        record: JobRecord, reason: str, message: str, detail: str = ""
    ) -> tuple[str, dict[str, Any]]:
        """Mark a job stopped-but-resumable; return its terminal frame."""
        record.status = JobStatus.INTERRUPTED
        record.error = message
        frame: dict[str, Any] = {
            "status": JobStatus.INTERRUPTED.value,
            "reason": reason,
            "error": message,
        }
        if detail:
            frame["detail"] = detail
        logger.info("Job %s interrupted (%s)", record.id, reason)
        return JobStatus.INTERRUPTED.value, frame

    @staticmethod
    def _close_checkpoint(record: JobRecord, reason: object) -> bool:
        """Settle a finished run's checkpoint; True while it stays resumable.

        Every outcome but a completed run keeps it — a cancel, an
        interruption and a failure all hold paid translations. A completed
        run's is removed after its session file is saved (see ``_run``).
        """
        checkpoint = record.checkpoint
        if checkpoint is None or record.status is JobStatus.DONE:
            return False
        try:
            checkpoint.record_end(
                record.status.value,
                reason=reason if isinstance(reason, str) else None,
                error=record.error,
            )
        except OSError:
            logger.exception("Failed to close the checkpoint of job %s", record.id)
        return checkpoint.exists()

    async def _run_scan(self, record: JobRecord) -> EnrichedScanResult:
        params = record.params
        previous_modpack = params.get("previous_modpack_path")
        # Reuse needs BOTH sides, and the A side is what makes it safe. B on
        # its own - a pack downloaded from moru.gg, say - is target-locale
        # files with no record of the source text they were translated from,
        # and MigrationCatalog.match refuses every lookup whose source cannot
        # be shown byte-identical between the old modpack and this one. So a
        # B-only request can only ever index its way to zero matches, and
        # asking for the catalog without A raised KeyError below and failed
        # the entire scan. Skipping it reports "no previous translation",
        # which is the truth.
        migration_requested = previous_modpack is not None and any(
            params.get(key) is not None
            for key in ("previous_resourcepack_path", "previous_overrides_path")
        )
        migration_fingerprint_before: str | None = None
        if migration_requested:
            migration_fingerprint_before = await asyncio.to_thread(
                _migration_scan_fingerprint, params
            )

        def progress(stage: str, current: int, total: int, message: str) -> None:
            self._emit(
                record,
                "progress",
                {
                    "stage": stage,
                    "current": current,
                    "total": total,
                    "message": message,
                },
            )

        cache_key = self._scan_cache_key(params)
        if not migration_requested and cache_key in self._scan_cache:
            progress("scan", 1, 1, "cached")
            progress("parse", 1, 1, "cached")
            logger.info("Serving cached scan result for %s", cache_key)
            return self._scan_cache[cache_key]

        scan = await scan_modpack(
            params["modpack_path"],
            source_locale=str(params.get("source_locale", "en_us")),
            target_locale=str(params.get("target_locale", "ko_kr")),
            progress_callback=progress,
            mod_blacklist=_mod_blacklist(params),
        )

        # Launcher metadata (CurseForge/Modrinth/Prism files) tells us which
        # published pack this folder is; a handful of fixed-path reads, run
        # off-loop so a slow disk never stalls event delivery.
        identity = await asyncio.to_thread(
            detect_pack_identity, Path(str(params["modpack_path"]))
        )

        # Parse pass: entry/char counts + samples per source file, so the
        # scan screen can show real volumes and cost estimates before any
        # LLM call. Parse failures degrade to zero counts, never fail scan.
        registry = create_default_registry()
        pairs = scan.all_translation_pairs
        enriched = EnrichedScanResult(scan=scan, identity=identity)
        if migration_requested:
            migration_assets = _migration_assets_dir(params)
            self._temporary_dirs.add(migration_assets)
            progress("migration", 0, 1, "이전 번역 비교 준비 중...")
            enriched.migration = await build_migration_catalog(
                previous_modpack_path=Path(str(previous_modpack)),
                previous_resourcepack_path=(
                    Path(str(params["previous_resourcepack_path"]))
                    if params.get("previous_resourcepack_path")
                    else None
                ),
                previous_overrides_path=(
                    Path(str(params["previous_overrides_path"]))
                    if params.get("previous_overrides_path")
                    else None
                ),
                current_modpack_root=Path(str(params["modpack_path"])),
                current_scan=scan,
                source_locale=str(params.get("source_locale", "en_us")),
                target_locale=str(params.get("target_locale", "ko_kr")),
                asset_cache_dir=migration_assets,
            )
            progress("migration", 1, 1, "이전 번역 비교 준비 완료")
        semaphore = asyncio.Semaphore(PARSE_CONCURRENCY)
        parsed_count = 0

        async def parse_one(pair: LanguageFilePair) -> None:
            nonlocal parsed_count
            source_path = pair.source_path
            meta = FileParseMeta()
            async with semaphore:
                handler = registry.get_handler(source_path)
                if handler is not None:
                    try:
                        source_data = await handler.extract(source_path)
                    except Exception as exc:  # noqa: BLE001 — parse-only pass
                        logger.warning("Scan parse failed for %s: %s", source_path, exc)
                    else:
                        pending_data = source_data
                        if pair.target_path is not None:
                            try:
                                existing = await handler.extract(pair.target_path)
                            except Exception as exc:  # noqa: BLE001 — count all source
                                logger.warning(
                                    "Target scan parse failed for %s: %s",
                                    pair.target_path,
                                    exc,
                                )
                            else:
                                pending_data = {
                                    key: value
                                    for key, value in source_data.items()
                                    if is_untranslated_copy(
                                        value,
                                        existing.get(key, ""),
                                    )
                                }
                        meta.parsed = True
                        meta.entry_count = len(pending_data)
                        meta.char_count = sum(len(v) for v in pending_data.values())
                        meta.sample = {
                            key: value[:SAMPLE_TEXT_LIMIT]
                            for key, value in list(pending_data.items())[:SAMPLE_ENTRIES]
                        }
                        if enriched.migration is not None:
                            logical = logical_file_id(
                                pair.source_path, scan.modpack_path
                            )
                            migrated = {
                                key: value
                                for key, value in pending_data.items()
                                if is_translatable_text(value)
                                and enriched.migration.match(logical, key, value)
                                is not None
                            }
                            meta.migration_entry_count = len(migrated)
                            meta.migration_char_count = sum(
                                len(value) for value in migrated.values()
                            )
            enriched.files[str(source_path)] = meta
            enriched.migration_entries += meta.migration_entry_count
            enriched.migration_chars += meta.migration_char_count
            parsed_count += 1
            progress("parse", parsed_count, len(pairs), source_path.name)

        if pairs:
            progress("parse", 0, len(pairs), "")
            await asyncio.gather(*(parse_one(pair) for pair in pairs))
        if migration_requested:
            migration_fingerprint_after = await asyncio.to_thread(
                _migration_scan_fingerprint, params
            )
            if migration_fingerprint_before == migration_fingerprint_after:
                enriched.migration_input_fingerprint = migration_fingerprint_after
            else:
                logger.info(
                    "Migration inputs changed while scan job %s was running; "
                    "W4 will rebuild the scan and index",
                    record.id,
                )
        else:
            while len(self._scan_cache) >= SCAN_CACHE_LIMIT:
                self._scan_cache.pop(next(iter(self._scan_cache)))
            self._scan_cache[cache_key] = enriched
        return enriched

    async def _run_pipeline(
        self,
        record: JobRecord,
        config: PipelineConfig,
        scan_source: JobRecord | None,
    ) -> PipelineResult:
        """Run one pipeline for this record: a translate job, or the
        source-text run behind a W3 export job."""
        def on_event(event: str, payload: dict[str, object]) -> None:
            if event == "done":
                # The pipeline's own "done" {stats} would collide with the
                # manager's terminal frame; stash it so the terminal
                # {type: done, status: done} carries the stats instead.
                record.done_payload = dict(payload)
                return
            self._emit(record, event, payload)

        def cancel_check() -> bool:
            return record.cancel_requested

        enriched = (
            scan_source.result
            if scan_source is not None
            and isinstance(scan_source.result, EnrichedScanResult)
            else None
        )
        reusable_scan: ScanResult | None = None
        reusable_migration: MigrationCatalog | None = None
        migration_requested = any(
            path is not None
            for path in (
                config.previous_modpack_path,
                config.previous_resourcepack_path,
                config.previous_overrides_path,
            )
        )
        if enriched is not None and not migration_requested:
            # Preserve v1.0's ordinary W2 -> W4 scan reuse.
            reusable_scan = enriched.scan
        elif enriched is not None and enriched.migration is not None:
            current_fingerprint = await asyncio.to_thread(
                _migration_inputs_fingerprint,
                modpack_path=config.modpack_path,
                previous_modpack_path=config.previous_modpack_path,
                previous_resourcepack_path=config.previous_resourcepack_path,
                previous_overrides_path=config.previous_overrides_path,
            )
            if (
                enriched.migration_input_fingerprint is not None
                and enriched.migration_input_fingerprint == current_fingerprint
            ):
                reusable_scan = enriched.scan
                reusable_migration = enriched.migration
            else:
                logger.info(
                    "Migration inputs changed after scan job %s; rebuilding W4 "
                    "scan and A/B/C index",
                    scan_source.id if scan_source is not None else "unknown",
                )

        def on_pipeline(pipeline: TranslationPipeline) -> None:
            record.pipeline = pipeline

        # An earlier hand-translation session's committed entries, adopted as
        # already-settled so this run neither re-sends nor overwrites them.
        human = (
            self._manual_resolver(config.seed_from_job_id)
            if config.seed_from_job_id and self._manual_resolver is not None
            else None
        )
        # A resumed run replays everything its session already translated;
        # off-loop, because a large pack's checkpoint is megabytes of JSON.
        resume_from = (
            await asyncio.to_thread(record.checkpoint.load)
            if record.resume and record.checkpoint is not None
            else None
        )
        review_edits = await self._review_edits(record) if record.resume else None
        try:
            result = await run_pipeline(
                config,
                on_event=on_event,
                cancel_check=cancel_check,
                on_pipeline=on_pipeline,
                scan_result=reusable_scan,
                migration=reusable_migration,
                human_translations=human,
                checkpoint=record.checkpoint,
                resume_from=resume_from,
                review_edits=review_edits,
            )
        finally:
            record.pipeline = None
        # Normal and partial-cancelled results expose the same cumulative
        # counters to the desktop terminal frame.
        record.done_payload = {"stats": result.stats.model_dump()}
        return result

    async def _apply_review_edits(
        self, source: JobRecord, stage: str
    ) -> PipelineResult:
        """Fold review-screen edits (manual PATCH / AI retranslate) back
        into the output trees so export/upload archives carry the
        reviewed state."""
        result = source.result
        assert isinstance(result, PipelineResult)  # _resolve_translate_source
        rewritten = await apply_entry_edits(result)
        if rewritten:
            logger.info(
                "%s: re-applied review edits to %d files", stage, rewritten
            )
        return result

    async def _run_export(
        self, record: JobRecord, resolve_result: ExportResultSource
    ) -> dict[str, Any]:
        """Build the installable archives from the generated output trees.

        Each translated tree has a bilingual sibling. Either variant's
        resource pack or overrides path is null when that tree is empty.

        The archived entries come from ``resolve_result`` — a translate
        job's reviewed result (W6) or a source-text run (W3). Everything
        below is identical for both, which is what makes a source-text
        export byte-for-byte structural with a translated one.
        """
        result = await resolve_result()
        root = output_root(result.config)
        pack_dir = root / RESOURCEPACK_DIRNAME
        overrides_dir = root / OVERRIDES_DIRNAME

        output_zip = record.params.get("output_zip")
        if output_zip:
            zip_path = Path(str(output_zip))
        else:
            # Off-loop like the scan: identity probes launcher JSON on disk.
            identity = await asyncio.to_thread(
                detect_pack_identity, result.config.modpack_path
            )
            zip_path = (
                _default_export_dir() / f"{_export_stem(result, identity)}.zip"
            )
        overrides_zip = zip_path.with_name(f"{zip_path.stem}_overrides.zip")
        bilingual_zip = zip_path.with_name(f"{zip_path.stem} (병기).zip")
        bilingual_overrides_zip = zip_path.with_name(
            f"{zip_path.stem} (병기)_overrides.zip"
        )
        archives = {
            "zip_path": (pack_dir, zip_path),
            "overrides_zip_path": (overrides_dir, overrides_zip),
            "bilingual_zip_path": (
                root / BILINGUAL_DIRNAME / RESOURCEPACK_DIRNAME, bilingual_zip
            ),
            "bilingual_overrides_zip_path": (
                root / BILINGUAL_DIRNAME / OVERRIDES_DIRNAME, bilingual_overrides_zip
            ),
        }

        self._emit(
            record,
            "progress",
            {"stage": "export", "current": 0, "total": 4},
        )

        def build() -> dict[str, Any]:
            payload: dict[str, Any] = {
                **dict.fromkeys(archives),
                "file_count": len(result.output_files),
            }
            for key, (tree, archive) in archives.items():
                if _tree_has_files(tree):
                    create_zip_from_directory(tree, archive)
                    payload[key] = str(archive)
            return payload

        # Zip construction is blocking I/O; keep the loop responsive.
        payload = await asyncio.to_thread(build)
        self._emit(
            record,
            "progress",
            {"stage": "export", "current": 4, "total": 4},
        )
        record.done_payload = dict(payload)
        if record.params.get("translate_job_id") and not record.params.get("source_text"):
            source = self.get(str(record.params["translate_job_id"]))
            source.done_payload = {**(source.done_payload or {}), **payload}
            try:
                self._session_store.save_job_session(source)
            except Exception:
                logger.exception("Failed to persist export paths for %s", source.id)
        return payload

    async def _run_upload(
        self, record: JobRecord, source: JobRecord
    ) -> dict[str, Any]:
        """Publish the whole modpack first, then authenticated per-mod packs."""
        params = record.params
        web_url = str(params.get("web_url") or "https://moru.gg").rstrip("/")
        api_token = params.get("api_token") or None

        result = await self._apply_review_edits(source, "pack")
        root = output_root(result.config)
        bilingual_root = root / BILINGUAL_DIRNAME
        trees = {
            "resource_pack": root / RESOURCEPACK_DIRNAME,
            "overrides": root / OVERRIDES_DIRNAME,
            "resource_pack_bilingual": bilingual_root / RESOURCEPACK_DIRNAME,
            "overrides_bilingual": bilingual_root / OVERRIDES_DIRNAME,
        }

        staging = Path(tempfile.mkdtemp(prefix="moru-upload-"))
        build_task: asyncio.Task[Any] | None = None
        mod_records: list[dict[str, Any]] = []
        mod_count = len({
            jar for entry in result.entries
            if (jar := jar_name_for(entry.file)).lower().endswith(".jar")
        })
        mod_packs: dict[str, Any] = {"shared": 0, "skipped": 0, "failed": 0}
        share_mods = params.get("share_mods", True)
        if not share_mods or not api_token:
            mod_packs.update(
                skipped=mod_count,
                skipped_reason="disabled" if not share_mods else "login_required",
            )
        identity: PackIdentity | None = None
        try:
            self._emit(
                record, "progress", {"stage": "pack", "current": 0, "total": 1}
            )

            def build() -> tuple[dict[str, Path], dict[str, tuple[str, int]]]:
                """One installable zip per non-empty tree, staged locally."""
                archives: dict[str, Path] = {}
                for kind, tree in trees.items():
                    if _tree_has_files(tree):
                        zip_path = staging / f"{kind}.zip"
                        create_zip_from_directory(tree, zip_path)
                        archives[kind] = zip_path
                return archives, {
                    kind: _sha256_and_size(archive)
                    for kind, archive in archives.items()
                }

            # Zip construction is blocking I/O; keep the loop responsive.
            build_task = asyncio.create_task(asyncio.to_thread(build))
            archives, digests = await asyncio.shield(build_task)
            if not archives:
                raise upload.WebUploadError(
                    "translate job produced no uploadable files"
                )
            self._emit(
                record, "progress", {"stage": "pack", "current": 1, "total": 1}
            )

            def step(current: int, message: str) -> None:
                self._emit(
                    record,
                    "progress",
                    {
                        "stage": "upload",
                        "current": current,
                        "total": 3,
                        "message": message,
                    },
                )

            step(0, "requesting upload slots")
            slots = await upload.request_upload_slots(
                web_url,
                api_token,
                [
                    {"kind": kind, "size": size, "sha256": sha256}
                    for kind, (sha256, size) in digests.items()
                ],
            )
            step(1, "uploading archives")
            for kind, zip_path in archives.items():
                await upload.put_archive(str(slots[kind]["url"]), zip_path)
            # Prepare the per-mod count before the one modpack announcement.
            # No per-mod network upload/registration precedes the whole pack.
            if share_mods and api_token:
                try:
                    sources = await self._await_mod_sources(source)
                    identity = await asyncio.to_thread(
                        detect_pack_identity, result.config.modpack_path
                    )
                    build_task = asyncio.create_task(asyncio.to_thread(
                        build_mod_packs, result, sources, staging / "mods", identity
                    ))
                    mod_records = await asyncio.shield(build_task)
                except Exception:
                    logger.exception("Could not prepare per-mod packs for %s", source.id)
                    mod_packs["failed"] = mod_count
            pack_payload = self._pack_payload(
                record, source, {kind: slots[kind] for kind in archives}
            )
            pack_payload["mod_pack_count"] = sum(
                bool(mod["project_id"] and mod["game_versions"] and mod["zips"])
                for mod in mod_records
            )
            step(2, "registering pack")
            registered = await upload.register_pack(
                web_url,
                api_token,
                pack_payload,
            )
            step(3, "registered")
            if mod_records:
                def mod_progress(current: int, total: int, message: str) -> None:
                    self._emit(record, "progress", {
                        "stage": "mods", "current": current, "total": total,
                        "message": message,
                    })

                metadata = {
                    "modpack_name": str(params["modpack_name"]),
                    "target_lang": result.config.target_locale,
                    "source_lang": result.config.source_locale,
                    "engine_version": __version__,
                }
                if params.get("curseforge_id"):
                    metadata["modpack_curseforge_id"] = params["curseforge_id"]
                if params.get("modpack_version"):
                    metadata["modpack_version"] = str(params["modpack_version"])
                if result.config.manual_seed:
                    metadata["is_manual_translation"] = True
                mod_progress(0, pack_payload["mod_pack_count"], "")
                mod_packs = await upload.upload_mod_packs(
                    web_url, api_token, mod_records, metadata,
                    loader=identity.loader if identity else None,
                    progress=mod_progress,
                )
                if mod_packs["skipped"] and not mod_packs["shared"] and not mod_packs["failed"]:
                    mod_packs["skipped_reason"] = "unresolved"
        finally:
            try:
                if build_task is not None and not build_task.done():
                    # A cancelled to_thread await does not stop its writer.
                    # Join it before deleting staging so it cannot recreate it.
                    await build_task
            finally:
                shutil.rmtree(staging, ignore_errors=True)

        payload = {
            "pack_id": str(registered["pack_id"]),
            "url": str(registered["url"]),
            "mod_packs": mod_packs,
        }
        if registered.get("modpack_slug"):
            payload["modpack_slug"] = str(registered["modpack_slug"])
        record.done_payload = dict(payload)
        return payload

    @staticmethod
    def _pack_payload(
        record: JobRecord,
        source: JobRecord,
        slots: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, Any]:
        """web-api.yaml TranslationPackCreate body from the translate result."""
        result = source.result
        assert isinstance(result, PipelineResult)  # _resolve_translate_source
        stats = result.stats
        params = record.params
        payload: dict[str, Any] = {
            "modpack_name": str(params["modpack_name"]),
            "target_lang": result.config.target_locale,
            "source_lang": result.config.source_locale,
            "files": [
                {"kind": kind, "object_key": slot["object_key"]}
                for kind, slot in slots.items()
            ],
            "engine_version": __version__,
            "stats": {
                "total_entries": stats.total_entries,
                "translated_entries": stats.translated_entries,
                "failed_entries": stats.failed_entries,
                "coverage_percent": stats.coverage_percent,
                "quality_score": stats.quality_score,
                "tm_hits": stats.tm_hits,
                "migration_hits": stats.migration_hits,
                "model": result.config.model,
                "duration_seconds": stats.duration_seconds,
            },
        }
        if result.config.manual_seed:
            payload["is_manual_translation"] = True
        if stats.categories:
            payload["stats"]["categories"] = stats.categories
        if result.artifact_id:
            payload["artifact_id"] = result.artifact_id
        for key in ("modpack_version", "description", "changelog"):
            value = params.get(key)
            if value:
                payload[key] = str(value)
        # Validated as a positive int in create_job; lets the web platform
        # link the pack to its CurseForge project page.
        curseforge_id = params.get("curseforge_id")
        if curseforge_id:
            payload["curseforge_id"] = int(curseforge_id)
        return payload

