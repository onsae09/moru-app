"""Durable, batch-granular checkpoints for resumable translation runs.

Why this exists: a translate job used to reach disk only when it finished
(``SessionStore.save_job_session`` runs on the terminal state). Quitting the
app mid-run, a killed or crashed sidecar, or a provider whose daily quota ran
out therefore threw away every batch the user had already paid for.

The checkpoint is the write path that closes that gap. Every completed
provider batch is appended here before its progress is reported, so an
interruption of any kind loses at most the batches still in flight. A resumed
run replays the file and sends only what is missing to the model.

Format: one append-only JSON-lines file per session. Records, in order:

``begin``
    Written when a run starts (a fresh run replaces the whole file; a resume
    appends another one). Carries the run identity (modpack path + locales),
    the translate params minus secrets, and display metadata.
``plan``
    Written once the prepare phase knows the run's scope: ``total`` entries
    needing a translation and how many were ``settled`` without the model
    (TM, previous-version migration, an earlier checkpoint). It is the
    progress denominator a relaunched app shows before anything is resumed.
``batch``
    One line per completed provider batch: ``[key, source digest, text]``
    rows for one file. ``retried`` marks the one-shot unchanged-copy retry,
    so a resumed run does not pay for that retry twice.
``end``
    The run stopped without completing (cancel, quota, shutdown, failure).

Each record is a single ``write`` followed by ``fsync``. A crash can tear only
the record being written, and replay skips a malformed line, so durability is
per batch — one fsync per provider call, never one per string.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator

logger = logging.getLogger(__name__)

CHECKPOINT_VERSION = 1

#: Checkpoint suffix inside the checkpoint directory: ``<session id>.jsonl``.
CHECKPOINT_SUFFIX = ".jsonl"

#: A session id becomes a filename; ids arrive over the wire, so everything
#: outside this set is replaced before the id is joined to a path.
_UNSAFE_ID_RE = re.compile(r"[^A-Za-z0-9_-]+")

#: State a summary reports when no ``end`` record exists: the process stopped
#: (quit, crash, kill) before it could write one.
STATE_INTERRUPTED = "interrupted"

#: Unlink attempts for a checkpoint another handle holds open. Windows
#: refuses to delete a file a concurrent reader (GET /checkpoints) has open;
#: that sharing violation clears within milliseconds.
_DELETE_ATTEMPTS = 4
_DELETE_RETRY_SECONDS = 0.05


def source_digest(source_text: str) -> str:
    """Digest recorded with every checkpointed translation.

    Truncated like the manual journal's ``src_sha``: it detects a changed
    source string, not an attack, and the short form keeps batch lines small.
    """
    return hashlib.sha256(
        source_text.encode("utf-8", errors="surrogatepass")
    ).hexdigest()[:16]


def checkpoint_identity(
    modpack_path: Path | str, source_locale: str, target_locale: str
) -> str:
    """Stable identity of the work a checkpoint holds.

    Translations are only reusable for the same pack and the same locale
    pair; a resumed run whose identity differs must not inherit them.
    """
    path = Path(modpack_path)
    try:
        resolved = str(path.resolve())
    except OSError:
        resolved = str(path)
    raw = "\x1f".join(
        (os.path.normcase(resolved), source_locale.lower(), target_locale.lower())
    )
    return hashlib.sha256(raw.encode("utf-8", errors="surrogatepass")).hexdigest()[:32]


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class CheckpointEntry:
    """One translation an earlier run of the session already paid for."""

    src_sha: str
    text: str
    #: The unchanged-copy retry was already spent on this entry.
    retried: bool = False


@dataclass
class CheckpointState:
    """A replayed checkpoint: the run header plus every settled translation."""

    header: dict[str, Any]
    #: ``(file, key)`` -> entry. ``file`` is modpack-relative, as in EntryResult.
    entries: dict[tuple[str, str], CheckpointEntry] = field(default_factory=dict)
    _by_logical: dict[tuple[str, str], CheckpointEntry | None] | None = field(
        default=None, repr=False
    )

    @property
    def identity(self) -> str | None:
        value = self.header.get("identity")
        return value if isinstance(value, str) else None

    @property
    def params(self) -> dict[str, Any]:
        value = self.header.get("params")
        return dict(value) if isinstance(value, dict) else {}

    def match(
        self,
        file: str,
        key: str,
        digest: str,
        *,
        logical_file: str | None = None,
        logical_of: Callable[[str], str] | None = None,
    ) -> CheckpointEntry | None:
        """The reusable translation for one entry, or None.

        An exact ``(file, key)`` record is authoritative: if its source
        digest no longer matches, the string changed and must be translated
        again. Only when the file itself is unknown — a mod JAR renamed by a
        modpack update moves its extracted lang file — is the record looked
        up by logical file identity, and then only when that coordinate is
        unambiguous across the checkpoint.
        """
        exact = self.entries.get((file, key))
        if exact is not None:
            return exact if exact.src_sha == digest else None
        if logical_file is None or logical_of is None:
            return None
        if self._by_logical is None:
            self._by_logical = self._logical_index(logical_of)
        fallback = self._by_logical.get((logical_file, key))
        if fallback is None or fallback.src_sha != digest:
            return None
        return fallback

    def _logical_index(
        self, logical_of: Callable[[str], str]
    ) -> dict[tuple[str, str], CheckpointEntry | None]:
        logical_for = {file: logical_of(file) for file, _ in self.entries}
        index: dict[tuple[str, str], CheckpointEntry | None] = {}
        for (file, key), entry in self.entries.items():
            coordinate = (logical_for[file], key)
            if coordinate not in index:
                index[coordinate] = entry
                continue
            previous = index[coordinate]
            if previous is not None and (previous.src_sha, previous.text) != (
                entry.src_sha,
                entry.text,
            ):
                # Two files disagree about the same logical entry: no guess.
                index[coordinate] = None
        return index


class TranslationCheckpoint:
    """Append-only, fsync'd checkpoint journal for one session."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def exists(self) -> bool:
        """Whether the file holds any record. An emptied one (see ``delete``)
        does not count: it can never be resumed."""
        try:
            return self.path.stat().st_size > 0
        except OSError:
            return False

    # -- writes ----------------------------------------------------------------

    def _line(self, record: dict[str, Any]) -> bytes:
        record = {"v": CHECKPOINT_VERSION, "ts": _now(), **record}
        text = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        # A lone surrogate (an undecodable byte some mod files carry) has no
        # UTF-8 form; inside a JSON string its backslash escape decodes back
        # to the same character, so the record stays lossless.
        return (text + "\n").encode("utf-8", errors="backslashreplace")

    def _append(self, record: dict[str, Any]) -> None:
        """One line + fsync. Serialized: interleaved appends would tear."""
        data = self._line(record)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a+b") as fh:
                fh.seek(0, os.SEEK_END)
                if fh.tell() > 0:
                    fh.seek(-1, os.SEEK_END)
                    if fh.read(1) != b"\n":
                        # A crash tore the previous append. Start a new line
                        # so this record is not glued onto the torn one and
                        # skipped with it on replay.
                        data = b"\n" + data
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())

    def start(self, header: dict[str, Any]) -> None:
        """Begin a fresh run: atomically replace any earlier checkpoint.

        A fresh run is an explicit "start over", so whatever an earlier run
        of this session left behind is discarded rather than reused.
        """
        data = self._line({"t": "begin", **header})
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with tmp.open("wb") as fh:
                    fh.write(data)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, self.path)
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise

    def resume(self, header: dict[str, Any]) -> None:
        """Begin a resumed run: keep every recorded translation."""
        self._append({"t": "begin", "resumed": True, **header})

    def record_plan(self, total: int, settled: int) -> None:
        self._append({"t": "plan", "total": total, "settled": settled})

    def record_batch(
        self,
        file: str,
        rows: Iterable[tuple[str, str, str]],
        *,
        retried: bool = False,
    ) -> None:
        """Persist one completed batch: ``(key, source digest, text)`` rows."""
        entries = [[key, digest, text] for key, digest, text in rows]
        if not entries:
            return
        record: dict[str, Any] = {"t": "batch", "file": file, "entries": entries}
        if retried:
            record["retried"] = True
        self._append(record)

    def record_end(
        self, status: str, *, reason: str | None = None, error: str | None = None
    ) -> None:
        self._append({"t": "end", "status": status, "reason": reason, "error": error})

    def delete(self) -> bool:
        """Remove the checkpoint; True when there was one.

        Unlink is retried briefly (see ``_DELETE_ATTEMPTS``). If it still
        fails, the file is emptied in place instead: a checkpoint without a
        ``begin`` record is never listed and never resumed, which is all a
        delete has to guarantee. Raises OSError only when neither works.
        """
        with self._lock:
            with contextlib.suppress(OSError):
                self.path.with_suffix(self.path.suffix + ".tmp").unlink(missing_ok=True)
            for attempt in range(1, _DELETE_ATTEMPTS + 1):
                try:
                    self.path.unlink()
                    return True
                except FileNotFoundError:
                    return False
                except OSError:
                    if attempt == _DELETE_ATTEMPTS:
                        break
                    time.sleep(_DELETE_RETRY_SECONDS * attempt)
            try:
                # Opening for writing is allowed where deleting is not: a
                # reader shares write access, but not delete access.
                with self.path.open("r+b") as fh:
                    fh.truncate(0)
                    fh.flush()
                    os.fsync(fh.fileno())
            except FileNotFoundError:
                return True  # the holder let go and the file is gone anyway
            logger.warning("Checkpoint %s is in use; emptied it instead", self.path)
            return True

    # -- reads -----------------------------------------------------------------

    def _records(self) -> Iterator[dict[str, Any]]:
        """Records in file order. A torn or malformed line is skipped: the
        last append can be cut by a crash, and it must not cost every batch
        before it."""
        try:
            fh = self.path.open("r", encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return
        except OSError:
            logger.exception("Failed to open checkpoint %s", self.path)
            return
        with fh:
            for lineno, line in enumerate(fh, start=1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    logger.warning(
                        "Skipping malformed checkpoint line %s:%d", self.path, lineno
                    )
                    continue
                if isinstance(record, dict) and isinstance(record.get("t"), str):
                    yield record

    def read_header(self) -> dict[str, Any] | None:
        """The first ``begin`` record: the run identity and its params."""
        for record in self._records():
            if record["t"] == "begin":
                return record
        return None

    def load(self) -> CheckpointState | None:
        """Replay the journal; None when there is no usable checkpoint."""
        header: dict[str, Any] | None = None
        entries: dict[tuple[str, str], CheckpointEntry] = {}
        for record in self._records():
            kind = record["t"]
            if kind == "begin":
                if header is None:
                    header = record
                continue
            if kind != "batch":
                continue
            file = record.get("file")
            rows = record.get("entries")
            if not isinstance(file, str) or not isinstance(rows, list):
                continue
            retried = bool(record.get("retried"))
            for row in rows:
                if not (
                    isinstance(row, list)
                    and len(row) == 3
                    and all(isinstance(value, str) for value in row)
                ):
                    continue
                key, digest, text = row
                previous = entries.get((file, key))
                # The latest record wins, but a spent retry stays spent while
                # the source it was spent on is unchanged.
                spent = (
                    previous is not None
                    and previous.retried
                    and previous.src_sha == digest
                )
                entries[(file, key)] = CheckpointEntry(digest, text, retried or spent)
        if header is None:
            return None
        return CheckpointState(header=header, entries=entries)

    def summarize(self) -> dict[str, Any] | None:
        """Listing view: who the run was, how far it got, why it stopped.

        ``translated_entries`` counts entries settled for the run — reused at
        prepare time (TM, migration, an earlier checkpoint) plus every entry
        a later batch translated; ``total_entries`` is None until a run of
        the session reached its plan.
        """
        header: dict[str, Any] | None = None
        updated_at: str | None = None
        total: int | None = None
        settled = 0
        fresh: set[tuple[str, str]] = set()
        end: dict[str, Any] | None = None
        for record in self._records():
            kind = record["t"]
            if isinstance(record.get("ts"), str):
                updated_at = record["ts"]
            if kind == "begin":
                header = header or record
                end = None
            elif kind == "plan":
                try:
                    total = int(record.get("total", 0))
                    settled = int(record.get("settled", 0))
                except (TypeError, ValueError):
                    continue
                fresh = set()
            elif kind == "batch" and not record.get("retried"):
                file = record.get("file")
                rows = record.get("entries")
                if isinstance(file, str) and isinstance(rows, list):
                    fresh.update(
                        (file, row[0])
                        for row in rows
                        if isinstance(row, list) and row and isinstance(row[0], str)
                    )
            elif kind == "end":
                end = record
        if header is None:
            return None
        translated = settled + len(fresh)
        return {
            "session_id": str(header.get("session_id") or ""),
            "modpack_path": str(header.get("modpack_path") or ""),
            "modpack_name": str(header.get("modpack_name") or ""),
            "source_locale": str(header.get("source_locale") or "en_us"),
            "target_locale": str(header.get("target_locale") or "ko_kr"),
            "model": str(header.get("model") or ""),
            "created_at": header.get("ts"),
            "updated_at": updated_at,
            "state": str(end.get("status")) if end is not None else STATE_INTERRUPTED,
            "reason": end.get("reason") if end is not None else None,
            "error": end.get("error") if end is not None else None,
            "translated_entries": (
                min(translated, total) if total is not None else translated
            ),
            "total_entries": total,
        }


class CheckpointStore:
    """Directory of per-session checkpoints."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def for_session(self, session_id: str) -> TranslationCheckpoint:
        stem = _UNSAFE_ID_RE.sub("-", session_id).strip("-")[:120] or "session"
        return TranslationCheckpoint(self.directory / f"{stem}{CHECKPOINT_SUFFIX}")

    def summaries(self) -> list[dict[str, Any]]:
        """Every resumable checkpoint, most recently touched first."""
        if not self.directory.is_dir():
            return []
        summaries: list[dict[str, Any]] = []
        for path in self.directory.glob(f"*{CHECKPOINT_SUFFIX}"):
            summary = TranslationCheckpoint(path).summarize()
            # A file with no begin record cannot be resumed: no params.
            if summary is not None and summary["session_id"]:
                summaries.append(summary)
        summaries.sort(key=lambda s: s.get("updated_at") or "", reverse=True)
        return summaries

    def delete(self, session_id: str) -> bool:
        return self.for_session(session_id).delete()
