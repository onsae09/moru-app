"""FastAPI application factory for the Moru engine sidecar.

Electron main spawns ``python -m moru_engine.server --port N --token T``,
polls GET /health, then talks to every other route with the session token
(``Authorization: Bearer <token>``). The server binds 127.0.0.1 only.

Implements moru-app/contracts/engine-api.yaml. Known deviations are
documented inline on each route.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import re
import secrets
import signal
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
    WebSocket,
)
from fastapi.middleware.cors import CORSMiddleware
from platformdirs import user_cache_dir, user_config_dir
from pydantic import BaseModel, Field, ValidationError, field_validator
from starlette.websockets import WebSocketDisconnect

from .. import __version__
from ..cli_providers import (
    CLI_PROVIDER_CATALOG,
    CLI_PROVIDER_IDS,
    provider_models,
    provider_status,
)
from ..cli_providers.http import aclose_async_client
from ..community import download_translation, find_translation, sync_community
from ..dspy_modules import build_lm
from ..graph import TranslationGraph
from ..imports import ArchiveImportError, import_modpack_archive
from ..pipeline import (
    EntryStatus,
    PipelineResult,
    RetranslateError,
    ReviewTarget,
    TranslationPipeline,
)
from ..pipeline.checkpoint import CheckpointStore
from ..models.glossary import Glossary, is_key_scope_pattern
from ..scanner.pack_identity import PackIdentity
from ..tm import MANUAL_ORIGIN, SHARED_GLOSSARY_VERSION, LocalTM
from ..validator import TranslationValidator
from .assist import (
    build_entry_context,
    glossary_from_store,
    placeholder_patterns,
    validate_pair,
)
from .jobs import (
    JobManager,
    JobParamsError,
    JobStateError,
    JobStatus,
    JobType,
    UnknownJobError,
    scan_result_payload,
)
from .live_models import LIVE_MODEL_PROVIDERS, fetch_live_models
from .glossary_check import GlossaryMissIndex
from .manual_journal import entry_ref
from .manual_state import EntryBucket, ManualStore
from .sessions import SessionStore

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from ..pipeline import EntryResult
    from .jobs import JobRecord

logger = logging.getLogger(__name__)

_LOCALE_RE = re.compile(r"[A-Za-z0-9_-]{1,32}")

#: Characters kept when a pack id becomes a cache directory name. Ids are
#: platform uuids, but the id is attacker-influenced in the sense that it
#: arrives over the wire, and it is about to be joined onto a filesystem
#: path — so it is filtered rather than trusted.
_PACK_ID_RE = re.compile(r"[^A-Za-z0-9_-]")


def _safe_segment(pack_id: str) -> str:
    return _PACK_ID_RE.sub("_", pack_id)[:64]


#: Static provider catalog. Model ids are LiteLLM strings usable directly
#: as PipelineConfig.model. has_key only reflects env-var presence; the
#: desktop keeps real keys in safeStorage and passes them per-job.
_PROVIDER_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "id": "openai",
        "name": "OpenAI",
        "env": "OPENAI_API_KEY",
        "models": [
            "openai/gpt-5.6-sol",
            "openai/gpt-5.6-terra",
            "openai/gpt-5.6-luna",
            "openai/gpt-4.1",
            "openai/gpt-4.1-mini",
        ],
    },
    {
        "id": "anthropic",
        "name": "Anthropic",
        "env": "ANTHROPIC_API_KEY",
        "models": [
            "anthropic/claude-sonnet-5-5",
            "anthropic/claude-haiku-5-5",
            "anthropic/claude-opus-5-5",
        ],
    },
    {
        "id": "gemini",
        "name": "Google Gemini",
        "env": "GEMINI_API_KEY",
        "models": [
            "gemini/gemini-3.1-pro-preview",
            "gemini/gemini-3.5-flash",
            "gemini/gemini-3.1-flash-lite",
        ],
    },
    {
        "id": "deepseek",
        "name": "DeepSeek",
        "env": "DEEPSEEK_API_KEY",
        "models": ["deepseek/deepseek-chat", "deepseek/deepseek-reasoner"],
    },
    {
        "id": "xai",
        "name": "xAI",
        "env": "XAI_API_KEY",
        "models": ["xai/grok-4", "xai/grok-3", "xai/grok-3-mini"],
    },
    {
        "id": "openrouter",
        "name": "OpenRouter",
        "env": "OPENROUTER_API_KEY",
        "models": [
            "openrouter/anthropic/claude-sonnet-5.5",
            "openrouter/anthropic/claude-haiku-5.5",
            "openrouter/openai/gpt-5.6-luna",
            "openrouter/google/gemini-3.5-flash",
            "openrouter/deepseek/deepseek-chat-v3-0324",
        ],
    },
    {
        "id": "ollama",
        "name": "Ollama (local)",
        "env": None,
        "models": [
            "ollama_chat/qwen3:8b",
            "ollama_chat/llama3.1:8b",
            "ollama_chat/gemma3:12b",
        ],
    },
    {
        # Any OpenAI-compatible server: LM Studio, llama.cpp, vLLM, ...
        # No static models — the lineup is whatever the user's server
        # loaded; the desktop lists it live via POST /providers/models
        # with the server's base URL.
        "id": "openai-compatible",
        "name": "OpenAI Compatible",
        "env": None,
        "models": [],
    },
) + CLI_PROVIDER_CATALOG


class JobRequest(BaseModel):
    type: Literal["scan", "translate", "export", "upload"]
    params: dict[str, Any]


class EntryPatch(BaseModel):
    translated_text: str
    #: Disambiguates a key that appears in more than one source file. Older
    #: clients omit it and keep the historical first-match behaviour.
    file: str | None = None
    #: False records a durable `draft` journal line WITHOUT settling the
    #: entry: in-progress text survives a crash but can never reach an
    #: export. True settles the entry as before.
    commit: bool = True
    #: Who produced the text. A manual edit is `human`; the AI retranslate
    #: path reports `machine`. Older clients omit it and are treated as human,
    #: which is what a review-screen edit has always been.
    origin: Literal["human", "machine"] = "human"
    #: sha256(source_text)[:16] as the client saw it. Recorded so a later scan
    #: that changes the source can mark this translation stale instead of
    #: shipping it as though still valid. Omitted -> staleness is UNKNOWN,
    #: which is deliberately not the same as clean.
    src_sha: str | None = None
    #: Set or clear the revisit bookmark. None leaves it unchanged.
    flagged: bool | None = None


class RetranslateRequest(BaseModel):
    file: str | None = None


class EntryRef(BaseModel):
    key: str
    file: str


#: The buckets an AI re-review can be asked to cover.
ReviewBucket = Literal["failed", "warning", "glossary"]


class ReviewTargetsRequest(BaseModel):
    """Which flagged entries an AI re-review pass should cover."""

    buckets: list[ReviewBucket] = Field(min_length=1, max_length=3)
    file: str | None = Field(None, max_length=1024)
    #: Entries the reviewer already signed off are left alone by default.
    include_reviewed: bool = False


class ReviewRequest(BaseModel):
    """One slice of an AI re-review pass; the client walks the target list."""

    #: Bounded so one request stays seconds-to-a-minute and a cancel between
    #: slices takes effect soon.
    refs: list[EntryRef] = Field(min_length=1, max_length=200)


class ReviewMark(BaseModel):
    """Sign entries off on the review screen, or take the mark back."""

    #: A page at most is what the review screen marks at once; the bound
    #: keeps one request from writing an unbounded journal line.
    refs: list[EntryRef] = Field(min_length=1, max_length=1000)
    reviewed: bool


class ReviewAllMark(BaseModel):
    """Sign off every entry a review view shows, across all its pages."""

    reviewed: bool
    filter: EntryBucket = "all"
    search: str = Field("", max_length=200)
    file: str | None = Field(None, max_length=1024)


class ValidateRequest(BaseModel):
    """Live validation of a candidate translation the user is still typing."""

    key: str
    file: str | None = None
    translated_text: str


class GlossaryTerm(BaseModel):
    source: str
    target: str
    origin: Literal["vanilla", "extracted", "manual", "community"] = "manual"
    #: Lang keys the term applies to, as dotted globs (see TermRule). Empty
    #: means every key, which is what every pre-scope row deserializes to.
    key_scope: list[str] = Field(default_factory=list)


class GlossaryDoc(BaseModel):
    source_lang: str
    target_lang: str
    terms: list[GlossaryTerm] = Field(default_factory=list)


class CommunitySyncRequest(BaseModel):
    web_url: str
    source_lang: str = "en_us"
    target_lang: str


class CommunityDownloadRequest(BaseModel):
    """A matched pack to fetch. ``pack_id`` only names the cache directory."""

    pack_id: str
    download_url: str


class ModpackImportRequest(BaseModel):
    """A modpack archive (.zip / .mrpack) to open as a scannable folder."""

    archive_path: str


class ProviderTestRequest(BaseModel):
    provider: str
    api_key: str | None = None
    model: str | None = None
    api_base: str | None = None


class ProviderModelsRequest(BaseModel):
    provider: str
    api_key: str | None = None
    api_base: str | None = None


class SessionExportRequest(BaseModel):
    output_path: str

    @field_validator("output_path")
    @classmethod
    def validate_path(cls, v: str) -> str:
        p = Path(v)
        if not p.is_absolute():
            raise ValueError("output_path must be an absolute path")
        if p.suffix.lower() not in (".moru", ".json"):
            raise ValueError("output_path must end with .moru or .json")
        return v


class SessionImportRequest(BaseModel):
    input_path: str

    @field_validator("input_path")
    @classmethod
    def validate_path(cls, v: str) -> str:
        p = Path(v)
        if not p.is_absolute():
            raise ValueError("input_path must be an absolute path")
        if p.suffix.lower() not in (".moru", ".json"):
            raise ValueError("input_path must end with .moru or .json")
        return v


def _atomic_write_json(path: Path, data: object) -> None:
    """Write JSON via a sibling temp file + os.replace (atomic on POSIX/NTFS)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> Any | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.exception("Failed to read %s", path)
        return None


def _validate_locale(value: str, name: str) -> str:
    if not _LOCALE_RE.fullmatch(value):
        raise HTTPException(status_code=422, detail=f"invalid {name}: {value!r}")
    return value.lower()


def _entry_payload(entry: EntryResult) -> dict[str, Any]:
    # Contract requires translated_text as a plain string, while the engine
    # keeps None for untranslated entries, so coerce that one internal state.
    return {
        "key": entry.key,
        "file": entry.file,
        "source_text": entry.source_text,
        "translated_text": entry.translated_text or "",
        "status": entry.status.value,
        "errors": list(entry.errors),
    }


def _find_entry(
    result: PipelineResult, key: str, file: str | None = None
) -> EntryResult | None:
    """Locate one review entry; ``file`` splits a key shared by two files."""
    return next(
        (
            e
            for e in result.entries
            if e.key == key and (file is None or e.file == file)
        ),
        None,
    )


def create_app(
    token: str,
    *,
    config_dir: Path | None = None,
    tm_db_path: Path | None = None,
    shutdown_handler: Callable[[], None] | None = None,
    shutdown_delay: float = 0.2,
) -> FastAPI:
    """Build the sidecar app bound to one session token.

    Args:
        token: Session token required on every route except /health.
        config_dir: Override for the platformdirs config root (tests).
        tm_db_path: Override for the local TM database path (tests).
        shutdown_handler: Override for the POST /shutdown action (tests).
        shutdown_delay: Seconds between the 202 response and the shutdown
            action, so the HTTP response can flush first.
    """
    if not token:
        raise ValueError("a non-empty session token is required")

    config_root = config_dir or Path(user_config_dir("moru", "moru"))
    config_path = config_root / "engine.json"
    glossary_dir = config_root / "glossaries"
    # Extracted packs can be gigabytes: a cache folder, never the (roaming,
    # on Windows) config folder — except under a test's config override.
    imports_dir = (
        config_root / "imports" if config_dir is not None
        else Path(user_cache_dir("moru", "moru")) / "imports"
    )
    session_store = SessionStore(sessions_dir=config_root / "sessions")
    manager = JobManager(
        glossary_store_dir=glossary_dir,
        session_store=session_store,
        checkpoint_store=CheckpointStore(config_root / "checkpoints"),
    )
    tm_holder: dict[str, LocalTM] = {}

    def get_tm() -> LocalTM:
        tm = tm_holder.get("tm")
        if tm is None:
            tm = LocalTM(tm_db_path)
            tm_holder["tm"] = tm
        return tm

    #: Hand-translation editing state, replayed from each session's edit
    #: journal. A cache, never the source of truth.
    manual = ManualStore(session_store)
    # A translate run started with seed_from_job_id needs the earlier session's
    # committed hand translations; the journal that records them lives here.
    manager.set_manual_resolver(manual.human_translations)

    #: Aid-panel caches, keyed by locale pair. The validator's constructor
    #: compiles one regex per glossary alias, so a live editor calling it per
    #: keystroke must reuse the instance rather than rebuild it.
    assist_holder: dict[str, Glossary] = {}
    validator_holder: dict[str, TranslationValidator] = {}
    miss_index_holder: dict[str, GlossaryMissIndex] = {}

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await manager.aclose()
        await aclose_async_client()
        tm = tm_holder.pop("tm", None)
        if tm is not None:
            tm.close()

    app = FastAPI(title="Moru Engine API", version=__version__, lifespan=lifespan)
    # The renderer runs on a foreign origin (file:// in the packaged app,
    # http://localhost:* in dev/browser mode). The server binds loopback and
    # every route still requires the per-session bearer token, so a blanket
    # allow-origin is safe and lets the authorization preflight through.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["authorization", "content-type"],
    )
    app.state.job_manager = manager

    def _token_matches(candidate: str | None) -> bool:
        return candidate is not None and secrets.compare_digest(candidate, token)

    def _bearer_value(header: str | None) -> str | None:
        if header is None:
            return None
        scheme, _, credentials = header.partition(" ")
        if scheme.lower() != "bearer" or not credentials:
            return None
        return credentials.strip()

    async def require_token(request: Request) -> None:
        if not _token_matches(_bearer_value(request.headers.get("authorization"))):
            raise HTTPException(
                status_code=401, detail="invalid or missing bearer token"
            )

    api = APIRouter(dependencies=[Depends(require_token)])

    # -- health / lifecycle ----------------------------------------------------

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    def _default_shutdown() -> None:
        server = getattr(app.state, "uvicorn_server", None)
        if server is not None:
            server.should_exit = True  # graceful uvicorn drain
        else:
            os.kill(os.getpid(), signal.SIGTERM)

    @api.post("/shutdown", status_code=202)
    async def shutdown() -> dict[str, str]:
        handler = shutdown_handler or _default_shutdown
        # Delay lets the 202 response flush before the process starts exiting.
        asyncio.get_running_loop().call_later(shutdown_delay, handler)
        logger.info("Shutdown scheduled in %.1fs", shutdown_delay)
        return {"status": "shutting down"}

    # -- jobs --------------------------------------------------------------------

    @api.post("/jobs", status_code=201)
    async def create_job(body: JobRequest) -> dict[str, Any]:
        try:
            record = manager.create_job(body.type, body.params)
        except UnknownJobError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except JobStateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except JobParamsError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return record.to_public()

    @api.get("/jobs/{job_id}")
    async def get_job(job_id: str) -> dict[str, Any]:
        try:
            record = manager.get(job_id)
        except UnknownJobError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return record.to_public()

    @api.get("/jobs/{job_id}/snapshot")
    async def get_job_snapshot(job_id: str) -> dict[str, Any]:
        try:
            return manager.snapshot(job_id)
        except UnknownJobError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @api.get("/jobs/{job_id}/mod-sources")
    async def get_mod_sources(job_id: str) -> dict[str, Any]:
        try:
            return manager.get_mod_sources(job_id).payload()
        except UnknownJobError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except JobStateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @api.post("/jobs/{job_id}/cancel", status_code=202)
    async def cancel_job(job_id: str) -> dict[str, Any]:
        try:
            record = manager.cancel(job_id)
        except UnknownJobError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"id": record.id, "status": record.status.value}

    @api.post("/jobs/{job_id}/pause", status_code=202)
    async def pause_job(job_id: str) -> dict[str, Any]:
        try:
            record = manager.pause(job_id)
        except UnknownJobError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except JobStateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"id": record.id, "status": record.status.value}

    @app.websocket("/jobs/{job_id}/events")
    async def job_events(websocket: WebSocket, job_id: str) -> None:
        # WS auth: ?token= query param OR Authorization header. Closing
        # before accept() rejects the handshake.
        supplied = websocket.query_params.get("token") or _bearer_value(
            websocket.headers.get("authorization")
        )
        if not _token_matches(supplied):
            await websocket.close(code=1008, reason="unauthorized")
            return
        after_value = websocket.query_params.get("after")
        try:
            after = int(after_value) if after_value is not None else None
            if after is not None and after < 0:
                raise ValueError
        except ValueError:
            await websocket.close(code=1008, reason="invalid event cursor")
            return
        try:
            history, queue = manager.subscribe(job_id, after=after)
        except UnknownJobError:
            await websocket.close(code=1008, reason=f"unknown job: {job_id}")
            return
        await websocket.accept()
        try:
            for frame in history:
                await websocket.send_json(frame)
            if queue is not None:
                while True:
                    frame = await queue.get()
                    if frame is None:  # terminal sentinel from JobManager
                        break
                    await websocket.send_json(frame)
            await websocket.close(code=1000)
        except WebSocketDisconnect:
            logger.debug("Events subscriber for %s disconnected", job_id)
        finally:
            if queue is not None:
                manager.unsubscribe(job_id, queue)

    # -- scan / translate results -------------------------------------------------

    def _get_typed_job(job_id: str, expected: JobType) -> JobRecord:
        try:
            record = manager.get(job_id)
        except UnknownJobError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if record.type is not expected:
            raise HTTPException(
                status_code=404,
                detail=f"job {job_id} is {record.type.value}, not {expected.value}",
            )
        return record

    @api.get("/scan/{job_id}/result")
    async def scan_result(job_id: str) -> dict[str, Any]:
        try:
            record = manager.get(job_id)
        except UnknownJobError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        # Translate records carry the payload of the scan they came from, so
        # a session reopened after a sidecar restart can still replay the
        # scan screen. Only a scan job can compute one on demand.
        cached = record.params.get("scan_result")
        if cached:
            return cached
        if record.type is not JobType.SCAN:
            raise HTTPException(
                status_code=404,
                detail=f"job {job_id} is {record.type.value}, not scan",
            )
        if record.status is not JobStatus.DONE or record.result is None:
            raise HTTPException(
                status_code=409,
                detail=f"scan job {job_id} is {record.status.value}, not done",
            )
        payload = scan_result_payload(record.result)  # type: ignore[arg-type]
        record.params["scan_result"] = payload
        return payload

    def _get_pipeline_result(job_id: str) -> tuple[JobRecord, PipelineResult]:
        record = _get_typed_job(job_id, JobType.TRANSLATE)
        result = record.result
        if result is None:
            raise HTTPException(
                status_code=409,
                detail=f"translate job {job_id} is {record.status.value}; "
                "no result available",
            )
        return record, result  # type: ignore[return-value]

    def _assist_glossary(result: PipelineResult) -> Glossary:
        """Glossary for the aid panels, cached per locale pair.

        The constructor cost is what matters: TranslationValidator compiles one
        regex per alias, so a live editor must reuse it rather than rebuild it
        per keystroke.
        """
        config = result.config
        cache_key = f"{config.source_locale}\u0000{config.target_locale}"
        cached = assist_holder.get(cache_key)
        if cached is None:
            cached = glossary_from_store(
                glossary_dir, config.source_locale, config.target_locale
            )
            assist_holder[cache_key] = cached
        return cached

    def _glossary_misses(result: PipelineResult) -> dict[str, tuple[str, ...]]:
        """Entries missing a required glossary term, by journal ref."""
        config = result.config
        cache_key = f"{config.source_locale}\u0000{config.target_locale}"
        index = miss_index_holder.get(cache_key)
        if index is None:
            index = GlossaryMissIndex(_assist_glossary(result))
            miss_index_holder[cache_key] = index
        return index.for_entries(result.entries)

    def _in_file(
        entries: list[EntryResult], file: str | None
    ) -> list[EntryResult]:
        """Entries of one source file, or all of them when none is named."""
        return entries if file is None else [e for e in entries if e.file == file]

    def _drop_glossary_caches() -> None:
        """Forget every glossary-derived cache after the glossary changed.

        Without this the aid panel, live validation and the review screen's
        glossary filter keep answering from the glossary as it was when they
        first looked, until the sidecar restarts.
        """
        assist_holder.clear()
        validator_holder.clear()
        miss_index_holder.clear()

    def _assist_validator(result: PipelineResult) -> TranslationValidator:
        config = result.config
        cache_key = f"{config.source_locale}\u0000{config.target_locale}"
        cached = validator_holder.get(cache_key)
        if cached is None:
            glossary = _assist_glossary(result)
            cached = TranslationValidator(glossary if glossary.has_rules else None)
            validator_holder[cache_key] = cached
        return cached

    @api.get("/translate/{job_id}/entries/{entry_key:path}/context")
    async def entry_context(
        job_id: str, entry_key: str, file: str | None = Query(None)
    ) -> dict[str, Any]:
        """Translation aids for one entry. Never touches a provider."""
        _, result = _get_pipeline_result(job_id)
        entry = _find_entry(result, entry_key, file)
        if entry is None:
            raise HTTPException(status_code=404, detail=f"unknown entry: {entry_key}")
        return build_entry_context(
            entry,
            result.entries,
            glossary=_assist_glossary(result),
            tm=get_tm() if result.config.use_tm else None,
            target_lang=result.config.target_locale,
            glossary_version=SHARED_GLOSSARY_VERSION,
        )

    @api.post("/translate/{job_id}/validate")
    async def validate_entry(
        job_id: str, body: ValidateRequest
    ) -> dict[str, list[dict[str, Any]]]:
        """Validate a candidate translation. Pure, synchronous, no model."""
        _, result = _get_pipeline_result(job_id)
        entry = _find_entry(result, body.key, body.file)
        if entry is None:
            raise HTTPException(status_code=404, detail=f"unknown entry: {body.key}")
        return {
            "issues": validate_pair(
                _assist_validator(result), entry, body.translated_text
            )
        }

    # Registered before the `{entry_key:path}` routes: a path converter would
    # otherwise swallow "counts" as an entry key.
    @api.get("/translate/{job_id}/entries/counts")
    async def translate_entry_counts(
        job_id: str,
        search: str = Query("", max_length=200),
        file: str | None = Query(None, max_length=1024),
    ) -> dict[str, int]:
        """Every bucket's size in one pass over the result (or one file)."""
        _, result = _get_pipeline_result(job_id)
        misses = await asyncio.to_thread(_glossary_misses, result)
        return manual.counts(job_id, _in_file(result.entries, file), search, misses)

    @api.get("/translate/{job_id}/entries/files")
    async def translate_entry_files(job_id: str) -> dict[str, Any]:
        """Per-source-file progress for the review screen's side list.

        One pass over the result. The review table is paginated, so the
        client cannot add these up from the rows it holds.
        """
        _, result = _get_pipeline_result(job_id)
        state = manual.state(job_id)
        files: dict[str, dict[str, Any]] = {}
        for e in result.entries:
            row = files.get(e.file)
            if row is None:
                row = files[e.file] = {
                    "file": e.file,
                    "total": 0,
                    "failed": 0,
                    "modified": 0,
                    "reviewed": 0,
                }
            row["total"] += 1
            if e.status is EntryStatus.FAILED:
                row["failed"] += 1
            elif e.status is EntryStatus.MODIFIED:
                row["modified"] += 1
            mark = state.get(entry_ref(e.file, e.key))
            if mark is not None and mark.reviewed:
                row["reviewed"] += 1
        return {"files": sorted(files.values(), key=lambda r: r["file"])}

    @api.post("/translate/{job_id}/entries/reviewed")
    async def mark_reviewed(job_id: str, body: ReviewMark) -> dict[str, int]:
        """Mark entries reviewed. One journal line, whatever the batch size.

        Unknown refs are skipped rather than failing the batch: the list was
        built from a page the client fetched, and an entry vanishing between
        fetch and mark must not cost the reviewer the rest of the page.
        """
        _, result = _get_pipeline_result(job_id)
        by_ref = {(e.file, e.key): e for e in result.entries}
        picked = [
            entry
            for ref in body.refs
            if (entry := by_ref.get((ref.file, ref.key))) is not None
        ]
        changed = await manual.record_review(job_id, picked, body.reviewed)
        return {"matched": len(picked), "changed": changed}

    @api.post("/translate/{job_id}/entries/reviewed/all")
    async def mark_all_reviewed(job_id: str, body: ReviewAllMark) -> dict[str, int]:
        """Mark every entry of one review view, not just the page on screen.

        Same selection as ``GET /entries`` with the same filter, search and
        file, so "all" means what the reviewer is looking at. Still one
        journal line.
        """
        _, result = _get_pipeline_result(job_id)
        misses = await asyncio.to_thread(_glossary_misses, result)
        picked = manual.select(
            job_id, _in_file(result.entries, body.file), body.filter, body.search, misses
        )
        changed = await manual.record_review(job_id, picked, body.reviewed)
        return {"matched": len(picked), "changed": changed}

    @api.get("/translate/{job_id}/entries")
    async def translate_entries(
        job_id: str,
        filter: EntryBucket = "all",
        page: int = Query(1, ge=1),
        page_size: int = Query(100, ge=1, le=500),
        search: str = Query("", max_length=200),
        file: str | None = Query(None, max_length=1024),
    ) -> dict[str, Any]:
        """One page of review entries, optionally from one source file.

        `search` filters across the WHOLE result set before paginating, so
        the review screen can find an entry that lives on any page — a
        client-side filter would only ever see the 100 rows it fetched.

        `glossary_missing` lists the glossary target terms an entry's
        translation lacks, on entries that lack any.
        """
        _, result = _get_pipeline_result(job_id)
        # Cached per entry: after the first call this is a dict walk.
        misses = await asyncio.to_thread(_glossary_misses, result)
        entries = manual.select(
            job_id, _in_file(result.entries, file), filter, search, misses
        )
        start = (page - 1) * page_size
        rows: list[dict[str, Any]] = []
        for e in entries[start : start + page_size]:
            payload = manual.enrich(_entry_payload(e), job_id, e)
            missing = misses.get(entry_ref(e.file, e.key))
            if missing:
                payload["glossary_missing"] = list(missing)
            rows.append(payload)
        return {"total": len(entries), "page": page, "entries": rows}

    @api.get("/translate/{job_id}/graph")
    async def translate_graph(
        job_id: str,
        q: str | None = Query(None, max_length=200),
        status: Literal["all", "settled", "pending"] = "all",
        limit_terms: int = Query(300, ge=1, le=2000),
        mentions_per_term: int = Query(10, ge=0, le=50),
        known_version: int | None = None,
    ) -> dict[str, Any]:
        """Translation-graph snapshot for the desktop's graph canvas.

        A running job serves the live pipeline graph (synchronous
        same-loop snapshot, always consistent); a finished job — session
        restorations included — serves a graph rebuilt once from
        result.entries and cached on the record. ``known_version`` lets
        the polling client skip unchanged payloads.
        """
        record = _get_typed_job(job_id, JobType.TRANSLATE)
        graph: TranslationGraph
        if record.pipeline is not None and not record.finished:
            live = record.pipeline.graph
            if live is None:
                raise HTTPException(
                    status_code=409,
                    detail="translation graph is disabled for this job or "
                    "not built yet",
                )
            graph = live
        else:
            _, result = _get_pipeline_result(job_id)
            if record.graph_cache is None:
                record.graph_cache = TranslationGraph.from_entries(result.entries)
            graph = record.graph_cache
        if known_version is not None and known_version == graph.version:
            return {
                "version": graph.version,
                "unchanged": True,
                "job_finished": record.finished,
            }
        payload = graph.snapshot(
            q=q,
            status=status,
            limit_terms=limit_terms,
            mentions_per_term=mentions_per_term,
        )
        payload["job_finished"] = record.finished
        return payload

    async def _persist_session(record: JobRecord) -> None:
        """Persist a post-run edit without failing the request that made it.

        Serializing a large session is slow enough to stall the event loop,
        so it runs off-loop, and a disk error must not discard an edit the
        caller already applied in memory.
        """
        try:
            await asyncio.to_thread(manager.session_store.save_job_session, record)
        except Exception as exc:  # noqa: BLE001 — persistence is best-effort
            logger.warning("Failed to save session %s: %s", record.id, exc)

    @api.patch("/translate/{job_id}/entries/{entry_key:path}")
    async def patch_entry(
        job_id: str, entry_key: str, body: EntryPatch
    ) -> dict[str, Any]:
        """Record one hand translation. No model, no provider, no network.

        The durable write is an append to the session's edit journal, not a
        rewrite of the whole session document: a translator saves constantly,
        and re-serializing every entry per keystroke-level save is O(entries)
        work for one changed string. The full snapshot is folded in only when
        the journal grows past its compaction threshold.
        """
        record, result = _get_pipeline_result(job_id)
        if not record.finished:
            raise HTTPException(
                status_code=409,
                detail=f"translate job {job_id} is still running",
            )
        entry = _find_entry(result, entry_key, body.file)
        if entry is None:
            raise HTTPException(status_code=404, detail=f"unknown entry: {entry_key}")
        if body.flagged is not None:
            await manual.record_flag(job_id, entry, body.flagged)

        if not body.commit:
            # Draft only: durable, but the entry is untouched so nothing
            # half-written can reach an export.
            await manual.record_draft(job_id, entry, body.translated_text, body.src_sha)
            return manual.enrich(_entry_payload(entry), job_id, entry)

        # The journal append is the durable write; the entry is settled only
        # after it succeeds, so an edit that could not be recorded is never
        # acknowledged as saved.
        should_compact = await manual.record_commit(
            job_id, entry, body.translated_text, body.src_sha, body.origin
        )
        entry.translated_text = body.translated_text
        entry.status = EntryStatus.MODIFIED
        TranslationPipeline._refresh_stats(result)

        # Propagate the decision. Without this a translator's terminology
        # choices die with the session and every later run re-asks the model
        # about strings a human already settled. `is_cacheable_pair` still
        # applies and is deliberately NOT bypassed: a target equal to its
        # source is kept in this entry's output but must not become a
        # permanent global promise, because a TM row is keyed on source text
        # alone and would suppress translation of that string everywhere.
        if body.origin == "human" and result.config.use_tm:
            await asyncio.to_thread(
                get_tm().store,
                entry.source_text,
                result.config.target_locale,
                SHARED_GLOSSARY_VERSION,
                body.translated_text,
                MANUAL_ORIGIN,
            )
        manager.refresh_translate_stats(record)
        if should_compact:
            await _persist_session(record)
            await manual.compacted(job_id)
        return manual.enrich(_entry_payload(entry), job_id, entry)

    @api.post("/translate/{job_id}/entries/{entry_key:path}/retranslate")
    async def retranslate_entry(
        job_id: str, entry_key: str, body: RetranslateRequest | None = None
    ) -> dict[str, Any]:
        """One-entry AI retranslation for the review screen.

        Builds a fresh pipeline from the job's own config (model, api_key,
        locales) and awaits the single LLM round-trip inline - seconds, not
        a job. 422 carries LM/config errors or a failed retranslation of a
        previously passing entry.
        """
        record, result = _get_pipeline_result(job_id)
        if not record.finished:
            raise HTTPException(
                status_code=409,
                detail=f"translate job {job_id} is still running",
            )
        target_file = body.file if body is not None else None
        if _find_entry(result, entry_key, target_file) is None:
            raise HTTPException(status_code=404, detail=f"unknown entry: {entry_key}")
        try:
            pipeline = TranslationPipeline(result.config)
        except Exception as exc:  # noqa: BLE001 — bad model/key config
            raise HTTPException(
                status_code=422, detail=f"cannot build translator: {exc}"
            ) from exc
        try:
            entry = await pipeline.retranslate_entry(
                result, entry_key, file=target_file
            )
        except RetranslateError as exc:
            raise HTTPException(
                status_code=422, detail=f"retranslation failed: {exc}"
            ) from exc
        finally:
            pipeline.close()
        # New model text was never looked at, so a sign-off of the old text
        # must not carry over to it.
        await manual.record_review(job_id, [entry], False)
        manager.refresh_translate_stats(record)
        await _persist_session(record)
        return manual.enrich(_entry_payload(entry), job_id, entry)

    def _is_human_edit(job_id: str, entry: EntryResult) -> bool:
        mark = manual.state(job_id).get(entry_ref(entry.file, entry.key))
        return mark is not None and mark.text is not None and mark.origin == "human"

    @api.post("/translate/{job_id}/review/targets")
    async def review_targets(job_id: str, body: ReviewTargetsRequest) -> dict[str, Any]:
        """The entries an AI re-review pass would cover, without touching a model.

        Hand translations are never offered: a re-review must not overwrite
        what a person settled. Signed-off entries are left out unless asked.
        """
        _, result = _get_pipeline_result(job_id)
        misses = await asyncio.to_thread(_glossary_misses, result)
        entries = _in_file(result.entries, body.file)
        picked: dict[tuple[str, str], EntryResult] = {}
        for bucket in body.buckets:
            for e in manual.select(job_id, entries, bucket, "", misses):
                picked.setdefault((e.file, e.key), e)
        state = manual.state(job_id)
        refs: list[dict[str, str]] = []
        skipped_human = skipped_reviewed = 0
        for e in picked.values():
            if _is_human_edit(job_id, e):
                skipped_human += 1
                continue
            mark = state.get(entry_ref(e.file, e.key))
            if not body.include_reviewed and mark is not None and mark.reviewed:
                skipped_reviewed += 1
                continue
            refs.append({"key": e.key, "file": e.file})
        return {
            "refs": refs,
            "skipped_human": skipped_human,
            "skipped_reviewed": skipped_reviewed,
        }

    @api.post("/translate/{job_id}/review")
    async def review_entries(job_id: str, body: ReviewRequest) -> dict[str, Any]:
        """AI re-review of a slice of flagged entries, in batches.

        The model sees each entry's current translation and its problems
        (validation messages, missing glossary terms). An entry changes only
        when the answer is better; ``improved`` lists those. A refused
        credential or spent quota ends the slice early and is reported in
        ``interruption`` so the client stops walking the list.
        """
        record, result = _get_pipeline_result(job_id)
        if not record.finished:
            raise HTTPException(
                status_code=409,
                detail=f"translate job {job_id} is still running",
            )
        misses = await asyncio.to_thread(_glossary_misses, result)
        by_ref = {(e.file, e.key): e for e in result.entries}
        targets: list[ReviewTarget] = []
        skipped = 0
        for ref in body.refs:
            entry = by_ref.get((ref.file, ref.key))
            # Re-checked here: a hand edit may have landed since the list
            # was built.
            if entry is None or _is_human_edit(job_id, entry):
                skipped += 1
                continue
            problems = list(entry.errors)
            problems += [
                f"required glossary term missing: {term}"
                for term in misses.get(entry_ref(entry.file, entry.key), ())
            ]
            targets.append(ReviewTarget(entry=entry, problems=problems))
        try:
            pipeline = TranslationPipeline(result.config)
        except Exception as exc:  # noqa: BLE001 — bad model/key config
            raise HTTPException(
                status_code=422, detail=f"cannot build translator: {exc}"
            ) from exc
        miss_index = miss_index_holder.get(
            f"{result.config.source_locale}\u0000{result.config.target_locale}"
        )
        try:
            outcome = await pipeline.review_entries(
                result,
                targets,
                miss_index.misses if miss_index is not None else None,
            )
        finally:
            pipeline.close()
        if outcome.improved:
            # New model text was never looked at: an old sign-off must not
            # carry over to it.
            await manual.record_review(job_id, outcome.improved, False)
            manager.refresh_translate_stats(record)
            await _persist_session(record)
        interruption = outcome.interruption
        return {
            "improved": [
                manual.enrich(_entry_payload(e), job_id, e) for e in outcome.improved
            ],
            "unchanged": outcome.unchanged,
            "pending": outcome.pending,
            "skipped": skipped,
            "interruption": (
                None if interruption is None else interruption.model_dump()
            ),
            "stats": result.stats.model_dump(),
        }

    @api.get("/translate/{job_id}/stats")
    async def translate_stats(job_id: str) -> dict[str, Any]:
        """Current counters, including any post-run review mutations."""
        _, result = _get_pipeline_result(job_id)
        return result.stats.model_dump()

    # -- sessions -------------------------------------------------------------------

    @api.get("/sessions")
    async def list_sessions() -> list[dict[str, Any]]:
        # Parses every stored payload, so it runs off-loop; a large session
        # store must not stall event delivery for a live job.
        return await asyncio.to_thread(manager.session_store.list_sessions)

    @api.post("/sessions/{session_id}/restore")
    async def restore_session(session_id: str) -> dict[str, Any]:
        try:
            record = manager.get(session_id)
        except UnknownJobError as exc:
            raise HTTPException(
                status_code=404, detail=f"session {session_id} not found"
            ) from exc
        return record.to_public()

    @api.post("/sessions/{session_id}/export")
    async def export_session(
        session_id: str, body: SessionExportRequest
    ) -> dict[str, Any]:
        # Flush in-memory review edits first so the exported file matches
        # the session the user is looking at.
        try:
            record = manager.get(session_id)
        except UnknownJobError:
            pass
        else:
            await _persist_session(record)
        try:
            exported = await asyncio.to_thread(
                manager.session_store.export_session_file,
                session_id,
                Path(body.output_path),
            )
        except FileNotFoundError as exc:
            raise HTTPException(
                status_code=404,
                detail=f"session_file_not_found: {session_id}",
            ) from exc
        except OSError as exc:
            raise HTTPException(
                status_code=500, detail=f"export failed: {exc}"
            ) from exc
        return {"status": "ok", "path": str(exported)}

    @api.post("/modpacks/import")
    async def import_modpack(body: ModpackImportRequest) -> dict[str, Any]:
        archive = Path(body.archive_path)
        if not archive.is_file():
            raise HTTPException(status_code=404, detail=f"no such file: {archive}")
        try:
            pack = await asyncio.to_thread(import_modpack_archive, archive, imports_dir)
        except (ArchiveImportError, OSError) as exc:
            raise HTTPException(status_code=422, detail=f"import failed: {exc}") from exc
        return {
            "modpack_path": str(pack.modpack_path),
            "name": pack.name,
            "has_mods": pack.has_mods,
        }

    @api.post("/sessions/import")
    async def import_session(body: SessionImportRequest) -> dict[str, Any]:
        try:
            record = await asyncio.to_thread(
                manager.session_store.import_session_file, Path(body.input_path)
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (ValueError, OSError) as exc:
            raise HTTPException(
                status_code=422, detail=f"import failed: {exc}"
            ) from exc
        manager.register_job(record)
        result = record.result if isinstance(record.result, PipelineResult) else None
        summary = manager.session_store.summarize_session(
            {
                "id": record.id,
                "modpack_name": record.params.get("modpack_name", ""),
                "modpack_path": record.params.get("modpack_path", ""),
                "source_locale": record.params.get("source_locale", "en_us"),
                "target_locale": record.params.get("target_locale", "ko_kr"),
                "model": record.params.get("model", ""),
                "status": record.status.value,
                "created_at": record.created_at.isoformat(),
                "entries": (
                    [{"status": e.status.value} for e in result.entries]
                    if result is not None
                    else []
                ),
                "stats": result.stats.model_dump() if result is not None else {},
                "done_payload": record.done_payload,
            }
        )
        return {"status": "ok", "session": summary, "job": record.to_public()}

    @api.delete("/sessions/{session_id}")
    async def delete_session(session_id: str) -> dict[str, str]:
        try:
            manager.forget_finished_job(session_id)
        except JobStateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        try:
            # The checkpoint first: it belongs to the session too, and a
            # deleted history row must not come back as a resumable one on
            # the next launch. It empties itself when it cannot be unlinked.
            removed = await asyncio.to_thread(manager.checkpoints.delete, session_id)
            removed = (
                await asyncio.to_thread(
                    manager.session_store.delete_session, session_id
                )
                or removed
            )
        except OSError as exc:
            # Windows refuses to unlink a file another request is reading.
            logger.warning("Could not delete session %s: %s", session_id, exc)
            raise HTTPException(
                status_code=409, detail=f"session {session_id} is in use; try again"
            ) from exc
        if not removed:
            raise HTTPException(
                status_code=404, detail=f"session {session_id} not found"
            )
        return {"status": "ok", "id": session_id}

    # -- checkpoints ----------------------------------------------------------------

    @api.get("/checkpoints")
    async def list_checkpoints() -> list[dict[str, Any]]:
        """Translate runs that stopped early and can be resumed.

        Listed from disk, so a session interrupted by a quit, crash or killed
        engine is still offered after the next launch.
        """
        return await manager.list_checkpoints()

    @api.delete("/checkpoints/{session_id}")
    async def delete_checkpoint(session_id: str) -> dict[str, str]:
        try:
            removed = await manager.delete_checkpoint(session_id)
        except JobStateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except OSError as exc:
            logger.warning("Could not delete checkpoint %s: %s", session_id, exc)
            raise HTTPException(
                status_code=409,
                detail=f"the checkpoint of session {session_id} is in use; try again",
            ) from exc
        if not removed:
            raise HTTPException(
                status_code=404, detail=f"no checkpoint for session {session_id}"
            )
        return {"status": "ok", "id": session_id}

    # -- glossary -------------------------------------------------------------------

    def _glossary_path(source_lang: str, target_lang: str) -> Path:
        source = _validate_locale(source_lang, "source_lang")
        target = _validate_locale(target_lang, "target_lang")
        return glossary_dir / f"{source}_{target}.json"

    @api.get("/glossary")
    async def get_glossary(source_lang: str, target_lang: str) -> dict[str, Any]:
        """The stored glossary, normalized to the response contract.

        The document on disk is whatever some past build wrote: a bare term
        list before the doc wrapper existed, rows without ``key_scope``, or a
        half-written file from a crash. Returning it verbatim pushed that
        straight into the renderer, where a ``terms`` value that is not a list
        throws mid-render and blanks the screen instead of showing the empty
        state. Parse through the response model, drop rows it rejects, and
        keep serving the ones that survive.
        """
        stored = _read_json(_glossary_path(source_lang, target_lang))
        raw_terms = stored.get("terms") if isinstance(stored, dict) else stored
        terms: list[GlossaryTerm] = []
        dropped = 0
        for entry in raw_terms if isinstance(raw_terms, list) else ():
            try:
                terms.append(GlossaryTerm.model_validate(entry))
            except ValidationError:
                dropped += 1
        if dropped:
            logger.warning(
                "Dropped %d malformed glossary term(s) for %s->%s",
                dropped,
                source_lang,
                target_lang,
            )
        return GlossaryDoc(
            source_lang=source_lang, target_lang=target_lang, terms=terms
        ).model_dump()

    @api.put("/glossary")
    async def put_glossary(body: GlossaryDoc) -> dict[str, Any]:
        """Persist the glossary, refusing a scope that could never fire.

        A ``key_scope`` is only ever a narrowing, and an unparseable pattern
        narrows a rule to nothing: it matches no key, so the rule is stored,
        listed and edited like any other while never applying to anything.
        The way that happens in practice is a separator mismatch across the
        CSV boundary — ``effect.*;status_effect.*`` arriving as one pattern —
        and neither the model's sort/dedup validator nor ``scope_rank`` can
        tell it apart from an honest miss. So reject it here, loudly, where
        the client can still show the user which pattern was wrong.
        """
        bad = sorted(
            {
                pattern
                for term in body.terms
                for pattern in term.key_scope
                if not is_key_scope_pattern(pattern)
            }
        )
        if bad:
            raise HTTPException(
                status_code=422,
                detail=(
                    "invalid key_scope pattern(s): "
                    + ", ".join(repr(pattern) for pattern in bad)
                ),
            )
        path = _glossary_path(body.source_lang, body.target_lang)
        data = body.model_dump()
        _atomic_write_json(path, data)
        _drop_glossary_caches()
        return data

    # -- tm / community / providers / config --------------------------------------------

    @api.post("/community/sync")
    async def community_sync(body: CommunitySyncRequest) -> dict[str, Any]:
        """Pull the latest community TM/glossary snapshots from the web
        platform into the local TM and the user glossary store."""
        source = _validate_locale(body.source_lang, "source_lang")
        target = _validate_locale(body.target_lang, "target_lang")
        try:
            return await sync_community(
                body.web_url, source, target, get_tm(), glossary_dir
            )
        except Exception as exc:  # noqa: BLE001 — network/payload errors -> 502
            raise HTTPException(
                status_code=502, detail=f"community sync failed: {exc}"
            ) from exc
        finally:
            # The sync writes community terms into the glossary store.
            _drop_glossary_caches()

    @api.get("/community/translation")
    async def community_translation(
        web_url: str, job_id: str, target_lang: str
    ) -> dict[str, Any]:
        """Is a published community translation already covering this pack?

        Asked once the scan is done, so the answer can quote how many of the
        user's own entries the published pack does not cover — the figure
        that decides whether reusing it is worth anything. The scan payload
        supplies both halves: the launcher-metadata identity to look up, and
        the per-category entry counts to measure against.

        Deviation from the usual error mapping: every failure is a null
        match, never a status code. Nothing published, a platform too old to
        serve the route, a modpack with no usable identity, a dropped
        connection — none of these are conditions the user can act on, and a
        wizard step that reports a red error because an optional lookup
        failed is worse than one that quietly offers nothing.
        """
        target = _validate_locale(target_lang, "target_lang")
        payload = await scan_result(job_id)
        identity_data = payload.get("identity")
        if not isinstance(identity_data, dict):
            return {"match": None}
        # Filter by field name: cached scan payloads persist inside sessions,
        # so one written by an older engine may carry keys this build's
        # PackIdentity no longer has.
        known = {f.name for f in dataclasses.fields(PackIdentity)}
        identity = PackIdentity(
            **{k: v for k, v in identity_data.items() if k in known}
        )
        categories = payload.get("categories")
        local_categories = (
            {
                str(entry["name"]): int(entry["entry_count"])
                for entry in categories
                if isinstance(entry, dict) and "name" in entry
            }
            if isinstance(categories, list)
            else None
        )
        try:
            match = await find_translation(
                web_url, identity, target, local_categories=local_categories
            )
        except Exception as exc:  # noqa: BLE001 — an optional lookup, see above
            logger.info("Community translation lookup skipped: %s", exc)
            return {"match": None}
        return {"match": dataclasses.asdict(match) if match else None}

    @api.post("/community/translation/download")
    async def community_translation_download(
        body: CommunityDownloadRequest,
    ) -> dict[str, Any]:
        """Fetch a matched pack's archives for run-scoped migration reuse.

        Returns local ZIP paths for the migration inputs. This is only the B
        side of A/B/C: whether any entry may actually be reused is still
        decided by the byte-identity check in the migration index, which
        needs the previous original modpack as A. A caller that supplies B
        alone is rejected by the job validator, deliberately.

        Unlike the lookup, a failure here IS reported: the user pressed a
        button and is owed an answer.
        """
        if not body.pack_id.strip():
            raise HTTPException(status_code=422, detail="pack_id is required")
        destination = config_root / "community-packs" / _safe_segment(body.pack_id)
        try:
            archives = await download_translation(body.download_url, destination)
        except Exception as exc:  # noqa: BLE001 — network/disk errors -> 502
            raise HTTPException(
                status_code=502, detail=f"translation download failed: {exc}"
            ) from exc
        return {
            "resourcepack_path": str(archives["resource_pack"])
            if "resource_pack" in archives
            else None,
            "overrides_path": str(archives["overrides"])
            if "overrides" in archives
            else None,
        }

    @api.get("/tm/stats")
    async def tm_stats() -> dict[str, Any]:
        # Deviation: LocalTM does not count lookup hits yet -> hits is
        # always 0. by_origin is an additive extra for the GUI.
        stats = get_tm().stats()
        return {
            "entries": stats.total_entries,
            "hits": 0,
            "last_sync_version": stats.last_shared_version,
            "by_origin": stats.by_origin,
        }

    @api.get("/placeholder/patterns")
    async def placeholder_pattern_list() -> dict[str, list[dict[str, str]]]:
        """The engine's placeholder patterns, in overlap-priority order.

        Exists so a client tokenizes with the same definitions the validator
        enforces. A hand-maintained copy in the renderer drifts, and the two
        layers then disagree about what counts as a placeholder — which the
        engine only catches after the fact.
        """
        return {"patterns": placeholder_patterns()}

    @api.get("/providers")
    async def providers() -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for p in _PROVIDER_CATALOG:
            entry: dict[str, Any] = {
                "id": p["id"],
                "name": p["name"],
                "models": list(p["models"]),
                "has_key": p["env"] is None or bool(os.environ.get(p["env"])),
            }
            if p.get("disabled"):
                # Never probed: a disabled provider reads no credentials.
                entry["auth"] = "cli"
                entry["disabled"] = True
                entry["has_key"] = False
                entry["connected"] = False
            elif p["id"] in CLI_PROVIDER_IDS:
                # No API key exists for these: "ready" means the user's own
                # CLI is logged in AND its grant can actually serve a
                # request, which for the legacy Gemini CLI includes
                # resolving a Cloud Code Assist project. `error` carries why
                # it cannot, so the card stops telling a migrated or
                # Workspace user to just log in again.
                #
                # `state` is what a client should branch on. `connected` is
                # a bool and cannot distinguish "CLI not installed" from
                # "installed but logged out" from "logged in but unusable",
                # which are three different things to tell a user to do.
                # Kept alongside, not replaced, so older clients still read.
                status = await asyncio.to_thread(provider_status, p["id"])
                entry["auth"] = "cli"
                entry["has_key"] = bool(status.get("connected"))
                entry["connected"] = bool(status.get("connected"))
                entry["login_hint"] = status.get("login_hint")
                entry["account"] = status.get("email")
                entry["error"] = status.get("error")
                # Paired with `error`: the prose stays for older clients,
                # the code lets a localized client say it in the user's own
                # language instead of rendering our Korean verbatim.
                entry["error_code"] = status.get("error_code")
                entry["state"] = status.get("state")
                entry["cli"] = status.get("cli")
                entry["cli_installed"] = status.get("cli_installed")
                # gemini-cli only: which of its two backends is in play, so
                # a support conversation is not guesswork.
                if status.get("transport") is not None:
                    entry["transport"] = status["transport"]
                    entry["config_dir"] = status.get("config_dir")
                    entry["config_dir_source"] = status.get("config_dir_source")
            out.append(entry)
        return out

    @api.post("/providers/test")
    async def providers_test(body: ProviderTestRequest) -> dict[str, Any]:
        catalog = {p["id"]: p for p in _PROVIDER_CATALOG}
        model = body.model
        if model is None:
            entry = catalog.get(body.provider)
            if entry is None or not entry["models"]:
                return {
                    "ok": False,
                    "error": f"no default model for provider: {body.provider}"
                    " (pass a model)",
                }
            model = entry["models"][0]

        def probe() -> None:
            # Minimal 1-token completion; cache off so a cached success can
            # never mask a revoked key.
            lm = build_lm(
                model,
                api_key=body.api_key,
                api_base=body.api_base,
                max_tokens=1,
                cache=False,
            )
            lm("ping")

        try:
            await asyncio.to_thread(probe)
        except Exception as exc:
            logger.info("Provider test failed for %s: %s", body.provider, exc)
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "error": None}

    @api.post("/providers/models")
    async def providers_models(body: ProviderModelsRequest) -> dict[str, Any]:
        """Live model list for one provider, static catalog as fallback."""
        catalog = {p["id"]: p for p in _PROVIDER_CATALOG}
        entry = catalog.get(body.provider)
        if entry is None:
            raise HTTPException(
                status_code=404, detail=f"unknown provider: {body.provider}"
            )
        # Desktop-saved key wins; otherwise fall back to the engine's env var
        # (matches has_key in GET /providers).
        env_name = entry["env"]
        if (
            body.provider in CLI_PROVIDER_IDS
            and body.provider not in LIVE_MODEL_PROVIDERS
        ):
            # Subscription surfaces with no /models endpoint to enumerate --
            # Cloud Code Assist publishes none -- so the catalog is
            # authoritative. Codex does publish one and gates SKUs per plan,
            # so it goes down the live path below. The lineup is still only
            # callable once the CLI's grant resolves, so the same auth probe
            # the badge uses rides along: the models are always listed, and
            # `error` says when they cannot actually be reached.
            status = await asyncio.to_thread(provider_status, body.provider)
            return {
                "provider": body.provider,
                "models": provider_models(body.provider),
                # Not a failed live fetch: the CLI publishes no list, so
                # this IS the lineup. A distinct value keeps the client from
                # presenting it as a degraded fallback.
                "source": "catalog",
                "error": status.get("error"),
            }
        api_key = body.api_key or (os.environ.get(env_name) if env_name else None)
        try:
            models = await fetch_live_models(
                body.provider, api_key=api_key, api_base=body.api_base
            )
        except Exception as exc:
            logger.info("Live model fetch failed for %s: %s", body.provider, exc)
            return {
                "provider": body.provider,
                "models": list(entry["models"]),
                "source": "static",
                "error": str(exc),
            }
        if not models:
            return {
                "provider": body.provider,
                "models": list(entry["models"]),
                "source": "static",
                "error": "provider returned no models",
            }
        return {
            "provider": body.provider,
            "models": models,
            "source": "live",
            "error": None,
        }

    @api.get("/config")
    async def get_config() -> dict[str, Any]:
        stored = _read_json(config_path)
        return stored if isinstance(stored, dict) else {}

    @api.put("/config")
    async def put_config(body: dict[str, Any]) -> dict[str, Any]:
        _atomic_write_json(config_path, body)
        return body

    app.include_router(api)
    return app
