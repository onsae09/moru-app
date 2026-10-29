"""Per-job hand-translation state and the queries built on it.

Lives outside ``app.py`` deliberately. The routes here are thin — a lookup, an
append, a filter — and keeping the substance in its own module means the HTTP
layer gains a handful of delegating lines rather than two hundred lines of
editing-session logic it would then own forever.

The state itself is a cache, never the source of truth: it is rebuilt by
replaying the session's edit journal, so dropping it (or restarting the
sidecar) costs nothing but the replay.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Literal, get_args

from .manual_journal import (
    COMPACT_THRESHOLD,
    ManualEntryState,
    ManualJournal,
    StaleState,
    entry_ref,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from ..pipeline import EntryResult
    from .sessions import SessionStore


#: Every bucket ``GET /entries`` accepts, and every key ``/entries/counts``
#: returns. ``flagged``, ``stale_source``, ``reviewed`` and ``unreviewed`` are
#: views over journal state, not entry statuses, so they cannot be answered by
#: comparing ``status.value``. ``glossary`` (a required glossary term is
#: missing from the translation) is computed against the user glossary.
#:
#: Declared as a Literal so FastAPI can validate the query parameter, with the
#: tuple derived from it — one definition, not two that can drift.
EntryBucket = Literal[
    "all",
    "pending",
    "failed",
    "warning",
    "modified",
    "flagged",
    "stale_source",
    "reviewed",
    "unreviewed",
    "glossary",
]
ENTRY_BUCKETS: tuple[str, ...] = get_args(EntryBucket)


class ManualStore:
    """Journal-backed editing state for every open session."""

    def __init__(self, session_store: SessionStore) -> None:
        self._sessions = session_store
        self._cache: dict[str, dict[str, ManualEntryState]] = {}

    # ---- state ---------------------------------------------------------

    def journal(self, job_id: str) -> ManualJournal:
        return self._sessions.journal_for(job_id)

    def state(self, job_id: str) -> dict[str, ManualEntryState]:
        state = self._cache.get(job_id)
        if state is None:
            state = self.journal(job_id).replay()
            self._cache[job_id] = state
        return state

    def entry_state(self, job_id: str, entry: EntryResult) -> ManualEntryState:
        return self.state(job_id).setdefault(
            entry_ref(entry.file, entry.key), ManualEntryState()
        )

    def forget(self, job_id: str) -> None:
        """Drop the cache for one job; the journal on disk is untouched."""
        self._cache.pop(job_id, None)

    def human_translations(self, job_id: str) -> dict[str, dict[str, str]]:
        """An earlier session's committed hand translations, by source file.

        Shaped for ``run_pipeline(human_translations=...)``, which adopts them
        as already-settled so a later automatic run neither re-sends nor
        overwrites them.

        Selection is by PROVENANCE, never by content: a record exists only
        because a human had the entry open and committed it. Drafts are
        excluded — they set ``draft``, not ``text`` — because an unfinished
        translation is not a decision. Content is never inspected, which is
        what makes a deliberate "leave this proper noun in English" adoptable
        even though its target equals its source.
        """
        out: dict[str, dict[str, str]] = {}
        for ref, state in self.state(job_id).items():
            if state.text is None or state.origin != "human":
                continue
            file, _, key = ref.partition("\u0000")
            if not key:
                continue
            out.setdefault(file, {})[key] = state.text
        return out

    # ---- wire shape ----------------------------------------------------

    def enrich(
        self, payload: dict[str, Any], job_id: str, entry: EntryResult
    ) -> dict[str, Any]:
        """Add editing-session fields to a base entry payload.

        These live here rather than on ``EntryResult`` because they describe how
        the text was produced, not what the pipeline computed. ``stale_source``
        is derived on read rather than stored: comparing the recorded hash
        against the source text present *now* cannot go out of date, whereas a
        stored boolean silently would.
        """
        manual = self.state(job_id).get(entry_ref(entry.file, entry.key))
        if manual is None:
            return payload
        payload["origin"] = manual.origin
        payload["flagged"] = manual.flagged
        payload["reviewed"] = manual.reviewed
        payload["stale_source"] = (
            manual.stale_against(entry.source_text) is not StaleState.CLEAN
        )
        return payload

    # ---- queries -------------------------------------------------------

    def select(
        self,
        job_id: str,
        entries: Sequence[EntryResult],
        bucket: str,
        search: str,
        glossary: Mapping[str, object] | None = None,
    ) -> list[EntryResult]:
        """Entries in one bucket, narrowed by search.

        ``glossary`` holds the refs of entries missing a glossary term (see
        ``GlossaryMissIndex``); it is computed by the caller because the
        glossary lives with the app, not with the editing session.
        """
        selected: Sequence[EntryResult] = entries
        if bucket == "glossary":
            misses = glossary or {}
            selected = [e for e in entries if entry_ref(e.file, e.key) in misses]
        elif bucket in ("flagged", "stale_source"):
            state = self.state(job_id)
            want_stale = bucket == "stale_source"
            picked: list[EntryResult] = []
            for e in entries:
                manual = state.get(entry_ref(e.file, e.key))
                if manual is None:
                    continue
                if want_stale:
                    if manual.stale_against(e.source_text) is not StaleState.CLEAN:
                        picked.append(e)
                elif manual.flagged:
                    picked.append(e)
            selected = picked
        elif bucket in ("reviewed", "unreviewed"):
            state = self.state(job_id)
            want = bucket == "reviewed"
            selected = [
                e
                for e in entries
                if (
                    (m := state.get(entry_ref(e.file, e.key))) is not None
                    and m.reviewed
                )
                is want
            ]
        elif bucket != "all":
            selected = [e for e in entries if e.status.value == bucket]

        needle = search.strip().casefold()
        if not needle:
            return list(selected)
        return [
            e
            for e in selected
            if needle in e.key.casefold()
            or needle in (e.source_text or "").casefold()
            or needle in (e.translated_text or "").casefold()
        ]

    def counts(
        self,
        job_id: str,
        entries: Sequence[EntryResult],
        search: str,
        glossary: Mapping[str, object] | None = None,
    ) -> dict[str, int]:
        """Every bucket's size.

        One call instead of one request per bucket: the review and manual
        screens both need all of them at once, and asking separately costs a
        full scan of the result each time — paid again on every commit.
        """
        return {
            bucket: len(self.select(job_id, entries, bucket, search, glossary))
            for bucket in ENTRY_BUCKETS
        }

    # ---- writes --------------------------------------------------------

    async def record_flag(self, job_id: str, entry: EntryResult, on: bool) -> None:
        manual = self.entry_state(job_id, entry)
        if manual.flagged == on:
            return
        await asyncio.to_thread(
            self.journal(job_id).flag, file=entry.file, key=entry.key, on=on
        )
        manual.flagged = on

    async def record_review(
        self, job_id: str, entries: Sequence[EntryResult], on: bool
    ) -> int:
        """Mark entries reviewed (or not). Returns how many actually changed.

        Entries already in the requested state are skipped, and the rest go to
        the journal as one batched line however many there are.
        """
        changed = [
            e for e in entries if self.entry_state(job_id, e).reviewed is not on
        ]
        if not changed:
            return 0
        await asyncio.to_thread(
            self.journal(job_id).review,
            refs=[(e.file, e.key) for e in changed],
            on=on,
        )
        for e in changed:
            self.entry_state(job_id, e).reviewed = on
        return len(changed)

    async def record_draft(
        self, job_id: str, entry: EntryResult, text: str, src_sha: str | None
    ) -> None:
        """Durably record in-progress text without settling the entry."""
        await asyncio.to_thread(
            self.journal(job_id).draft,
            file=entry.file,
            key=entry.key,
            text=text,
            src_sha=src_sha,
        )
        self.entry_state(job_id, entry).draft = text

    async def record_commit(
        self,
        job_id: str,
        entry: EntryResult,
        text: str,
        src_sha: str | None,
        origin: str,
    ) -> bool:
        """Append a commit. Returns True when the journal wants compacting.

        The append is the durable write; the caller settles the entry only
        after it succeeds, so an edit that could not be recorded is never
        acknowledged as saved.
        """
        journal = self.journal(job_id)
        await asyncio.to_thread(
            journal.commit,
            file=entry.file,
            key=entry.key,
            text=text,
            src_sha=src_sha,
            origin=origin,
        )
        manual = self.entry_state(job_id, entry)
        manual.text = text
        manual.src_sha = src_sha
        manual.origin = origin
        manual.draft = None
        return journal.lines_since_compaction >= COMPACT_THRESHOLD

    def _residual_records(self, job_id: str) -> list[dict[str, Any]]:
        """What a snapshot cannot hold: flags, review marks, unsaved drafts.

        Flags and review marks collapse into one batched line each, so the
        carried-over journal stays a handful of lines however many entries
        were marked.
        """
        flagged: list[list[str]] = []
        reviewed: list[list[str]] = []
        records: list[dict[str, Any]] = []
        for ref, manual in self.state(job_id).items():
            file, _, key = ref.partition("\u0000")
            if not key:
                continue
            if manual.flagged:
                flagged.append([file, key])
            if manual.reviewed:
                reviewed.append([file, key])
            if manual.draft is not None:
                records.append(
                    {"t": "draft", "file": file, "key": key, "text": manual.draft}
                )
        if flagged:
            records.append({"t": "flag", "refs": flagged, "on": True})
        if reviewed:
            records.append({"t": "review", "refs": reviewed, "on": True})
        return records

    async def compacted(self, job_id: str) -> None:
        """Fold the journal after its committed text reached a snapshot.

        Committed text is in the snapshot now; everything else is carried
        over into a fresh journal rather than dropped with the old one.
        """
        records = self._residual_records(job_id)
        await asyncio.to_thread(self.journal(job_id).rewrite, records)
