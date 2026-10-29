"""Append-only edit journal for hand-translated sessions.

Why this exists, and why it is not just "another save path":

``SessionStore.save_job_session`` rewrites the *entire* session document —
every entry, every config field — and it is what the review screen's per-entry
PATCH called on every single edit. That is O(entries) work per keystroke-level
save, and on a large pack it is tens of megabytes rewritten to commit one
string. It also never called ``fsync``, so a power cut could lose an
acknowledged edit even though the rename itself was atomic.

A hand translator saves constantly, which turns both properties from "slow" and
"unlikely" into "unusable" and "data loss". This journal is the write path
instead:

* one ~200-byte line appended per event, cost independent of pack size;
* ``fsync`` on every append, so an acknowledged edit is on the platter;
* the full snapshot is written only on compaction and on session close, where
  its cost is paid once instead of per edit.

Recovery is snapshot + replay: load ``<id>.moru``, then apply the journal in
order. The last record for a given ``(file, key)`` wins.

The journal is also where drift is caught. Entry identity in this engine is the
composite ``(file, key)`` and nothing hashes the source text, so a modpack
update can change a string underneath a finished hand translation with nothing
noticing. Every commit therefore records ``src_sha``, and replay compares it
against the source text actually present now. See ``StaleState``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

JOURNAL_VERSION = 1

#: Snapshot and truncate once the journal grows past this many lines. Replay is
#: linear, so the bound is about keeping session open fast, not about disk.
COMPACT_THRESHOLD = 2000

#: Journal suffix, alongside the session's own "<id>.moru".
JOURNAL_SUFFIX = ".manual.jsonl"

#: Tags a line a compaction carried over rather than a new edit.
_CARRY_MARK = b'"carry":true'


class StaleState(str, Enum):
    """Whether a recorded translation still matches the text it was written for.

    The three-way split is deliberate: ``UNKNOWN`` must not collapse into
    ``CLEAN``. Journals written before ``src_sha`` existed carry no hash, and
    silently certifying that work as current is exactly the failure this field
    exists to prevent.
    """

    CLEAN = "clean"
    #: Source text changed after the translation was committed.
    STALE = "stale"
    #: No recorded hash to compare — treated as suspect, never as clean.
    UNKNOWN = "unknown"


def source_sha(source_text: str) -> str:
    """The hash recorded with a commit. Truncated: this detects change, not
    attack, and 64 bits of collision resistance costs 48 fewer bytes a line."""
    return hashlib.sha256(source_text.encode("utf-8")).hexdigest()[:16]


def entry_ref(file: str, key: str) -> str:
    """Journal identity for one entry.

    ``key`` alone is not unique — the same key legitimately appears in two
    source files, which is why the entry PATCH takes a ``file`` disambiguator.
    """
    return f"{file}\u0000{key}"


class ManualEntryState:
    """Journal-derived facts about one entry.

    Kept beside the pipeline's ``EntryResult`` rather than on it: these are
    properties of the editing session, not outputs of the translation
    pipeline, and the pipeline has no business carrying them.
    """

    __slots__ = ("draft", "flagged", "origin", "reviewed", "src_sha", "text")

    def __init__(self) -> None:
        #: Last committed text, or None if only drafts exist.
        self.text: str | None = None
        #: Uncommitted in-progress text. Survives a crash; never ships.
        self.draft: str | None = None
        self.flagged: bool = False
        #: The reviewer looked at this entry on the review screen and signed
        #: it off. A bookmark about the review, not about the text.
        self.reviewed: bool = False
        self.origin: str | None = None
        self.src_sha: str | None = None

    def stale_against(self, source_text: str) -> StaleState:
        if self.text is None:
            return StaleState.CLEAN
        if self.src_sha is None:
            return StaleState.UNKNOWN
        return (
            StaleState.CLEAN
            if self.src_sha == source_sha(source_text)
            else StaleState.STALE
        )


class ManualJournal:
    """Append-only, fsync'd edit log for one session."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        #: Lines a compaction carried over (flags, review marks, drafts) are
        #: tagged ``"carry":true``. They are not new edits, so they must not
        #: count toward the next compaction, or a large carried-over set
        #: would trigger one on every save.
        self._lines, self._carried = self._count_lines()

    @staticmethod
    def for_session(session_file: Path) -> ManualJournal:
        return ManualJournal(session_file.with_suffix(JOURNAL_SUFFIX))

    @property
    def line_count(self) -> int:
        return self._lines

    @property
    def lines_since_compaction(self) -> int:
        return max(0, self._lines - self._carried)

    def _count_lines(self) -> tuple[int, int]:
        """(all lines, carried-over lines).

        The marker cannot occur inside a string value: JSON escapes the
        quotes it would need.
        """
        if not self.path.is_file():
            return 0, 0
        lines = carried = 0
        try:
            with self.path.open("rb") as fh:
                for raw in fh:
                    lines += 1
                    if _CARRY_MARK in raw:
                        carried += 1
        except OSError:
            logger.exception("Failed to size manual journal %s", self.path)
            return 0, 0
        return lines, carried

    def _append(self, record: dict[str, Any]) -> None:
        """One line + fsync. Serialized: two interleaved appends would tear.

        Raises on I/O failure. An edit the caller cannot durably record must
        not be acknowledged as saved — that is the whole point of this module.
        """
        record["v"] = JOURNAL_VERSION
        record["ts"] = datetime.now(UTC).isoformat()
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())
            self._lines += 1

    def commit(
        self,
        *,
        file: str,
        key: str,
        text: str,
        src_sha: str | None,
        origin: str,
    ) -> None:
        self._append(
            {
                "t": "commit",
                "file": file,
                "key": key,
                "text": text,
                "src_sha": src_sha,
                "origin": origin,
            }
        )

    def draft(self, *, file: str, key: str, text: str, src_sha: str | None) -> None:
        """Record in-progress text without settling the entry.

        This is what makes "never lose work" true rather than aspirational: the
        text is durable before the translator has decided it is finished, and
        because it never touches the entry it can never reach an export.
        """
        self._append(
            {"t": "draft", "file": file, "key": key, "text": text, "src_sha": src_sha}
        )

    def flag(self, *, file: str, key: str, on: bool) -> None:
        self._append({"t": "flag", "file": file, "key": key, "on": on})

    def review(self, *, refs: list[tuple[str, str]], on: bool) -> None:
        """Mark many entries reviewed (or not) in ONE line.

        Signing off a whole page is a single reviewer action; one fsync'd
        append per entry would make it a hundred.
        """
        if not refs:
            return
        self._append({"t": "review", "refs": [list(r) for r in refs], "on": on})

    def rewrite(self, records: list[dict[str, Any]]) -> None:
        """Replace the journal with ``records`` — compaction that keeps state.

        The snapshot written before a compaction carries committed text only;
        flags, review marks and drafts live nowhere but here, so a plain
        truncate would silently drop them. The replacement is written to a
        sibling file, fsync'd and renamed over the journal, so a crash leaves
        either the old journal or the new one, never neither.
        """
        if not records:
            self.truncate()
            return
        stamp = datetime.now(UTC).isoformat()
        body = "".join(
            json.dumps(
                {**record, "carry": True, "v": JOURNAL_VERSION, "ts": stamp},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n"
            for record in records
        )
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tmp.open("w", encoding="utf-8") as fh:
                fh.write(body)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
            self._lines = len(records)
            self._carried = len(records)

    def replay(self) -> dict[str, ManualEntryState]:
        """Fold the journal into per-entry state, in file order.

        A malformed line is skipped with a warning rather than aborting the
        load: the last append can be torn by a crash, and one bad trailing line
        must not cost the translator every edit before it.
        """
        state: dict[str, ManualEntryState] = {}
        if not self.path.is_file():
            return state
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError:
            logger.exception("Failed to read manual journal %s", self.path)
            return state

        for lineno, line in enumerate(raw.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError:
                logger.warning(
                    "Skipping malformed manual journal line %s:%d", self.path, lineno
                )
                continue
            if not isinstance(record, dict):
                continue
            kind = record.get("t")
            refs = record.get("refs")
            if isinstance(refs, list):
                # Batched form: one line covering many entries.
                on = bool(record.get("on"))
                for ref in refs:
                    if (
                        not isinstance(ref, list)
                        or len(ref) != 2
                        or not all(isinstance(part, str) for part in ref)
                    ):
                        continue
                    many = state.setdefault(entry_ref(ref[0], ref[1]), ManualEntryState())
                    if kind == "review":
                        many.reviewed = on
                    elif kind == "flag":
                        many.flagged = on
                continue
            file = record.get("file")
            key = record.get("key")
            if not isinstance(file, str) or not isinstance(key, str):
                continue
            entry = state.setdefault(entry_ref(file, key), ManualEntryState())
            if kind == "commit":
                entry.text = str(record.get("text", ""))
                entry.src_sha = record.get("src_sha") or None
                entry.origin = record.get("origin") or None
                # Committing supersedes whatever was being drafted.
                entry.draft = None
            elif kind == "draft":
                entry.draft = str(record.get("text", ""))
            elif kind == "flag":
                entry.flagged = bool(record.get("on"))
            elif kind == "review":
                entry.reviewed = bool(record.get("on"))
        return state

    def truncate(self) -> None:
        """Drop the journal after its contents were folded into a snapshot."""
        with self._lock:
            try:
                if self.path.is_file():
                    self.path.unlink()
            except OSError:
                logger.exception("Failed to truncate manual journal %s", self.path)
                return
            self._lines = 0
            self._carried = 0

    def delete(self) -> None:
        self.truncate()
