"""Bounded job memory: event history, subscriber queues, finished runs.

A pack run emits tens of thousands of frames, and a long-lived engine runs
many packs. None of that may grow without bound, and none of the bounding
may change what the desktop sees: a snapshot, then a replay after its
cursor.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from moru_engine.server import jobs
from moru_engine.server.jobs import (
    JobManager,
    JobRecord,
    JobStatus,
    JobType,
    _reduce_history,
)
from moru_engine.server.sessions import SessionStore


def _feed(record: JobRecord, count: int) -> list[dict]:
    """Deliver a realistic frame mix; return every frame delivered."""
    delivered: list[dict] = []
    probe: asyncio.Queue = asyncio.Queue()
    record.subscribers.add(probe)
    for i in range(count):
        kind = i % 6
        if kind == 0:
            frame = {"type": "batch_started", "request_id": i, "file": "f", "key": "k",
                     "entries": 1}
        elif kind == 1:
            frame = {"type": "batch_finished", "request_id": i - 1}
        elif kind == 2:
            frame = {"type": "entry_done", "key": f"k{i}", "source": "s", "translated": "t"}
        elif kind == 3:
            frame = {"type": "entry_failed", "key": f"k{i}", "errors": ["x"]}
        elif kind == 4:
            frame = {"type": "progress", "stage": "translate", "file": f"f{i % 7}",
                     "done": i, "total": count}
        else:
            frame = {"type": "tokens", "prompt_tokens": i, "completion_tokens": i}
        JobManager._deliver(record, frame)
        delivered.append(probe.get_nowait())
    record.subscribers.discard(probe)
    return delivered


def test_compacted_history_keeps_the_snapshot_and_a_gap_free_tail() -> None:
    record = JobRecord(id="r", type=JobType.TRANSLATE, params={})
    full = _feed(record, 60_000)

    assert len(record.history) <= jobs.HISTORY_COMPACT_AT + 1
    expected_frames, expected_failed, expected_started, _ = _reduce_history(full)
    frames, failed, started, _ = _reduce_history(record.history)
    assert frames == expected_frames
    assert failed + record.compacted_failures == expected_failed
    assert (record.compacted_started_at or started) == expected_started
    # A cursor inside the verbatim tail replays exactly what was missed.
    cursor = record.event_seq - 1_000
    replay = [f for f in record.history if f["seq"] > cursor]
    assert replay == [f for f in full if f["seq"] > cursor]


def test_snapshot_matches_before_and_after_compaction(tmp_path: Path) -> None:
    manager = JobManager(session_store=SessionStore(tmp_path))
    record = JobRecord(id="s", type=JobType.TRANSLATE, params={})
    manager.register_job(record)
    _feed(record, jobs.HISTORY_COMPACT_AT - 10)
    reference = JobRecord(id="s", type=JobType.TRANSLATE, params={})
    reference.history = list(record.history)
    _feed(record, 30_000)
    full_failed = sum(
        1 for f in reference.history if f["type"] == "entry_failed"
    ) + 30_000 // 6
    snapshot = manager.snapshot("s")
    assert snapshot["failed_count"] == full_failed
    assert snapshot["cursor"] == record.event_seq
    assert snapshot["translation_started_at"] == reference.history[0]["emitted_at"]


def test_a_stalled_subscriber_is_ended_not_buffered_forever(monkeypatch) -> None:
    monkeypatch.setattr(jobs, "SUBSCRIBER_QUEUE_LIMIT", 5)
    record = JobRecord(id="q", type=JobType.TRANSLATE, params={})
    stalled: asyncio.Queue = asyncio.Queue()
    record.subscribers.add(stalled)
    for i in range(20):
        JobManager._deliver(record, {"type": "entry_done", "key": str(i)})
    assert stalled not in record.subscribers
    items = [stalled.get_nowait() for _ in range(stalled.qsize())]
    assert len(items) == 6
    assert items[-1] is None  # stream end: the desktop resyncs via snapshot


def test_old_finished_runs_are_evicted_and_reload_on_demand(
    tmp_path: Path, monkeypatch
) -> None:
    store = SessionStore(tmp_path)
    manager = JobManager(session_store=store)
    ids = [f"run{i}" for i in range(jobs.MAX_RESIDENT_FINISHED_RUNS + 3)]
    for job_id in ids:
        record = JobRecord(id=job_id, type=JobType.TRANSLATE, params={},
                           status=JobStatus.DONE, finished=True)
        manager.register_job(record)
        manager._persisted_order.append(job_id)
    # An interrupted run is resumable and stays, however old.
    manager._jobs["run1"].status = JobStatus.INTERRUPTED

    manager._evict_finished()

    resident = [job_id for job_id in ids if job_id in manager._jobs]
    assert "run1" in resident
    assert "run0" not in resident
    assert len(resident) == jobs.MAX_RESIDENT_FINISHED_RUNS
    reloaded = JobRecord(id="run0", type=JobType.TRANSLATE, params={})
    monkeypatch.setattr(store, "load_job_session", lambda job_id: reloaded)
    assert manager.get("run0") is reloaded
