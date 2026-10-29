"""Checkpoint/resume: an interrupted translation keeps every completed batch,
and a resumed run pays only for what is still missing.

The translators here are duck-typed stand-ins for BatchTranslator (same
pattern as test_orchestrator.py / test_pipeline_e2e.py): deterministic
``KO <text>`` output, recording exactly which entries reached the "model".
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

import dspy
import pytest
from dspy.utils.exceptions import AdapterParseError
from fastapi.testclient import TestClient
from pydantic import TypeAdapter, ValidationError

import moru_engine.pipeline.checkpoint as checkpoint_module
import moru_engine.pipeline.orchestrator as orchestrator
from moru_engine.dspy_modules.lm import provider_limit
from moru_engine.dspy_modules.signatures import TranslateEntries
from moru_engine.models import LanguageFilePair
from moru_engine.pipeline import (
    EntryStatus,
    PipelineConfig,
    PipelineResult,
    TranslationPipeline,
)
from moru_engine.pipeline.checkpoint import (
    CheckpointEntry,
    CheckpointState,
    CheckpointStore,
    TranslationCheckpoint,
    source_digest,
)
from moru_engine.scanner import ScanResult, TranslationFile
from moru_engine.server import create_app

if TYPE_CHECKING:
    from collections.abc import Iterator

FIXTURE = Path(__file__).resolve().parents[1] / "test" / "modpack"
TOKEN = "checkpoint-resume-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


# -- fake providers ------------------------------------------------------------


class RecordingTranslator:
    """Deterministic translator recording every (file, key) it was sent."""

    def __init__(self) -> None:
        self.calls = 0
        self.sent: list[tuple[str, str]] = []

    async def acall(
        self, *, context: str, entries: dict[str, str], **_: Any
    ) -> dspy.Prediction:
        self.calls += 1
        file = context.split(";", 1)[0].removeprefix("file: ")
        self.sent.extend((file, key) for key in entries)
        return dspy.Prediction(
            translations={key: f"KO {text}" for key, text in entries.items()},
            failed={},
        )


class HangingAfter(RecordingTranslator):
    """Completes ``completed`` batches, then hangs: the call a kill cuts off."""

    def __init__(self, completed: int) -> None:
        super().__init__()
        self.completed = completed
        self.hung = asyncio.Event()

    async def acall(self, **kwargs: Any) -> dspy.Prediction:
        if self.calls >= self.completed:
            self.hung.set()
            await asyncio.Event().wait()
        return await super().acall(**kwargs)


class ExhaustedAfter(RecordingTranslator):
    """Completes ``completed`` batches, then the provider refuses for good."""

    def __init__(self, completed: int, error: Exception) -> None:
        super().__init__()
        self.completed = completed
        self.error = error
        self.refusals = 0

    async def acall(self, **kwargs: Any) -> dspy.Prediction:
        if self.calls >= self.completed:
            self.refusals += 1
            raise self.error
        return await super().acall(**kwargs)


class QuotaExhausted(Exception):
    """A provider 429 (transport-shaped: retried with backoff first)."""

    status_code = 429


class CreditExhausted(Exception):
    """Anthropic's spent-credit refusal: a 400, so never transport-shaped."""

    status_code = 400


# -- helpers -------------------------------------------------------------------


def _lang_pack(root: Path, entries: dict[str, str]) -> tuple[Path, ScanResult]:
    modpack = root / "modpack"
    source = modpack / "kubejs/assets/test/lang/en_us.json"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(json.dumps(entries), encoding="utf-8")
    return modpack, ScanResult(
        modpack_path=modpack,
        source_only_files=[LanguageFilePair(source_path=source)],
        translation_files=[
            TranslationFile(input_path=str(source), file_type="kubejs")
        ],
    )


def _lang_config(modpack: Path, out: Path) -> PipelineConfig:
    return PipelineConfig(
        modpack_path=modpack,
        output_dir=out,
        batch_size=2,
        max_concurrent=1,
        file_workers=1,
        use_tm=False,
        use_mod_translations=False,
        use_user_glossary=False,
        use_vanilla_glossary=False,
    )


async def _run(
    config: PipelineConfig,
    translator: Any,
    *,
    scan: ScanResult | None = None,
    checkpoint: TranslationCheckpoint | None = None,
    resume_from: CheckpointState | None = None,
    events: list[str] | None = None,
) -> tuple[TranslationPipeline, PipelineResult]:
    pipeline = TranslationPipeline(
        config,
        lm=dspy.utils.DummyLM([]),
        checkpoint=checkpoint,
        resume_from=resume_from,
        on_event=(lambda event, _payload: events.append(event))
        if events is not None
        else None,
    )
    pipeline.translator = translator
    try:
        return pipeline, await pipeline.run(scan)
    finally:
        pipeline.close()


def _entry_view(entries: Any) -> dict[tuple[str, str], tuple[str | None, str]]:
    return {
        (entry.file, entry.key): (entry.translated_text, entry.status.value)
        for entry in entries
    }


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


LANG_ENTRIES = {
    "quest.alpha": "Collect the ancient shard",
    "quest.beta": "Return to the village",
    "quest.gamma": "Defeat the forest guardian",
    "quest.delta": "Craft a %s with %d gems",
    "quest.epsilon": "Find the hidden cave",
    "quest.zeta": "Speak with the elder",
}


# -- the checkpoint file ---------------------------------------------------------


def test_checkpoint_replays_batches_and_survives_a_torn_tail(tmp_path: Path) -> None:
    checkpoint = TranslationCheckpoint(tmp_path / "session.jsonl")
    checkpoint.start({"session_id": "s1", "identity": "id", "params": {"model": "m"}})
    checkpoint.record_plan(total=5, settled=1)
    checkpoint.record_batch(
        "a.json",
        [("k1", source_digest("One"), "하나"), ("k2", source_digest("Two"), "Two")],
    )
    checkpoint.record_batch("a.json", [("k2", source_digest("Two"), "둘")], retried=True)
    # A crash in the middle of an append leaves a torn last line.
    with checkpoint.path.open("a", encoding="utf-8") as fh:
        fh.write('{"v":1,"t":"batch","file":"a.json","entries":[["k3","')

    state = checkpoint.load()
    assert state is not None
    assert state.params == {"model": "m"}
    assert state.entries[("a.json", "k1")] == CheckpointEntry(source_digest("One"), "하나")
    # The retry wave's later record wins and marks the retry as spent.
    assert state.entries[("a.json", "k2")] == CheckpointEntry(
        source_digest("Two"), "둘", retried=True
    )
    assert ("a.json", "k3") not in state.entries

    summary = checkpoint.summarize()
    assert summary is not None
    # 1 settled at prepare + k1 + k2; the retry line is not counted twice.
    assert summary["translated_entries"] == 3
    assert summary["total_entries"] == 5
    assert summary["state"] == "interrupted"
    assert summary["reason"] is None

    checkpoint.record_end("interrupted", reason="quota", error="한도")
    ended = checkpoint.summarize()
    assert ended is not None
    assert (ended["state"], ended["reason"]) == ("interrupted", "quota")


def test_fresh_start_replaces_the_checkpoint_and_resume_keeps_it(
    tmp_path: Path,
) -> None:
    checkpoint = TranslationCheckpoint(tmp_path / "session.jsonl")
    checkpoint.start({"session_id": "s1", "params": {}})
    checkpoint.record_batch("a.json", [("k", source_digest("A"), "가")])

    checkpoint.resume({"session_id": "s1", "params": {}})
    resumed = checkpoint.load()
    assert resumed is not None and ("a.json", "k") in resumed.entries

    checkpoint.start({"session_id": "s1", "params": {"fresh": True}})
    fresh = checkpoint.load()
    assert fresh is not None
    assert fresh.entries == {}
    assert fresh.params == {"fresh": True}
    assert checkpoint.delete()
    assert checkpoint.load() is None


def test_checkpoint_match_requires_unchanged_source() -> None:
    state = CheckpointState(
        header={},
        entries={
            (".mct_cache/extracted/mod-1.0.jar/assets/mod/lang/en_us.json", "k"): (
                CheckpointEntry(source_digest("Iron Plate"), "철판")
            ),
        },
    )
    old_file = ".mct_cache/extracted/mod-1.0.jar/assets/mod/lang/en_us.json"
    assert state.match(old_file, "k", source_digest("Iron Plate")) is not None
    # The pack changed the string: it must reach the model again.
    assert state.match(old_file, "k", source_digest("Steel Plate")) is None

    # A modpack update renamed the JAR: same logical file, same text.
    def logical_of(file: str) -> str:
        return "resource:" + file.rsplit("/assets/", 1)[1]

    renamed = ".mct_cache/extracted/mod-1.1.jar/assets/mod/lang/en_us.json"
    hit = state.match(
        renamed,
        "k",
        source_digest("Iron Plate"),
        logical_file=logical_of(renamed),
        logical_of=logical_of,
    )
    assert hit is not None and hit.text == "철판"
    assert (
        state.match(
            renamed,
            "k",
            source_digest("Steel Plate"),
            logical_file=logical_of(renamed),
            logical_of=logical_of,
        )
        is None
    )


@pytest.mark.parametrize(
    ("message", "status", "expected"),
    [
        ("Your credit balance is too low to access the Anthropic API.", 400, "quota"),
        ("OpenAIException - insufficient_quota", 429, "quota"),
        # What LiteLLM raises for OpenAI's insufficient_quota keeps only the
        # message, and Gemini sends the same words for per-minute limits: a
        # throttling signal that interrupts only once backoff gave up on it.
        (
            "litellm.RateLimitError: RateLimitError: OpenAIException - You "
            "exceeded your current quota, please check your plan and billing "
            "details.",
            429,
            "rate",
        ),
        ("Insufficient credits", 402, "quota"),
        (
            "RESOURCE_EXHAUSTED: Quota exceeded for metric "
            "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
            429,
            "quota",
        ),
        ("Codex 사용량 한도에 도달했습니다. (429)", None, "quota"),
        ("RESOURCE_EXHAUSTED: please retry in 30s", 429, "rate"),
        ("Too Many Requests", None, "rate"),
        ("upstream connect error", 503, None),
        ("invalid JSON in model output", None, None),
    ],
)
def test_provider_limit_classifies_capacity_refusals(
    message: str, status: int | None, expected: str | None
) -> None:
    error = Exception(message)
    if status is not None:
        error.status_code = status  # type: ignore[attr-defined]
    assert provider_limit(error) == expected


def test_provider_limit_ignores_model_output_failures() -> None:
    # A parse failure quotes the model's response; mod keys and tooltips say
    # "daily", "per day" or "usage limit" without any provider refusing.
    parse_error = AdapterParseError(
        adapter_name="JSONAdapter",
        signature=TranslateEntries,
        lm_response='{"translations": {"quest.daily_reward.title": "일일 보상"',
        message="usage limit per day",
    )
    assert provider_limit(parse_error) is None
    try:
        TypeAdapter(dict[str, str]).validate_python({"quest.daily.title": 1})
    except ValidationError as error:
        assert provider_limit(error) is None
    else:
        pytest.fail("expected a ValidationError")


def test_provider_limit_reads_the_error_litellm_wraps() -> None:
    # LiteLLM's message is the same for OpenAI's spent credit and Gemini's
    # per-minute limit; only the wrapped SDK error carries OpenAI's code.
    throttled = Exception("RateLimitError: You exceeded your current quota")
    throttled.status_code = 429  # type: ignore[attr-defined]
    assert provider_limit(throttled) == "rate"

    sdk_error = Exception("Error code: 429")
    sdk_error.body = {"code": "insufficient_quota"}  # type: ignore[attr-defined]
    throttled.__cause__ = sdk_error
    assert provider_limit(throttled) == "quota"

    # A parse failure the provider error was raised during is model output,
    # not part of the refusal.
    retried = Exception("RateLimitError: Too Many Requests")
    retried.status_code = 429  # type: ignore[attr-defined]
    retried.__context__ = AdapterParseError(
        adapter_name="JSONAdapter",
        signature=TranslateEntries,
        lm_response='{"translations": {"quest.daily_reward.title": "일일 보상"}}',
    )
    assert provider_limit(retried) == "rate"


# -- pipeline: interrupt and resume ----------------------------------------------


@pytest.mark.asyncio
async def test_throttling_past_the_give_up_window_interrupts_resumably(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(orchestrator, "TRANSPORT_BACKOFF_BASE", 0.0)
    # Every refusal counts as having outlasted the window.
    monkeypatch.setattr(orchestrator, "CAPACITY_GIVE_UP_SECONDS", 0.0)
    modpack, scan = _lang_pack(tmp_path, LANG_ENTRIES)
    checkpoint = TranslationCheckpoint(tmp_path / "session.jsonl")
    checkpoint.start({"session_id": "s1", "params": {}})

    exhausted = ExhaustedAfter(2, QuotaExhausted("You exceeded your current quota"))
    events: list[str] = []
    _, partial = await _run(
        _lang_config(modpack, tmp_path / "out"),
        exhausted,
        scan=scan,
        checkpoint=checkpoint,
        events=events,
    )

    assert partial.interruption is not None
    assert partial.interruption.reason == "quota"
    assert "이어서 번역" in partial.interruption.message
    # Nothing got through for the whole give-up window, so the first refusal
    # stopped the run instead of failing every remaining batch.
    assert exhausted.refusals == 1
    assert "done" not in events
    assert all(entry.status is EntryStatus.PASSED for entry in partial.entries)
    assert len(partial.entries) == 4
    state = checkpoint.load()
    assert state is not None
    assert set(state.entries) == {(e.file, e.key) for e in partial.entries}

    resumed_translator = RecordingTranslator()
    pipeline, resumed = await _run(
        _lang_config(modpack, tmp_path / "out"),
        resumed_translator,
        scan=scan,
        checkpoint=checkpoint,
        resume_from=state,
    )
    assert resumed.interruption is None
    assert pipeline.resumed_entries == 4
    assert set(resumed_translator.sent).isdisjoint(state.entries)
    assert len(resumed_translator.sent) == 2

    reference_translator = RecordingTranslator()
    _, reference = await _run(
        _lang_config(modpack, tmp_path / "reference"),
        reference_translator,
        scan=scan,
    )
    assert _entry_view(resumed.entries) == _entry_view(reference.entries)
    assert _tree(tmp_path / "out") == _tree(tmp_path / "reference")
    # The resumed run kept checkpointing: everything is on disk now.
    final_state = checkpoint.load()
    assert final_state is not None
    assert set(final_state.entries) == {(e.file, e.key) for e in reference.entries}


@pytest.mark.asyncio
async def test_spent_credit_interrupts_without_splitting(tmp_path: Path) -> None:
    modpack, scan = _lang_pack(tmp_path, LANG_ENTRIES)
    exhausted = ExhaustedAfter(
        1,
        CreditExhausted(
            "Your credit balance is too low to access the Anthropic API."
        ),
    )
    _, result = await _run(
        _lang_config(modpack, tmp_path / "out"), exhausted, scan=scan
    )

    assert result.interruption is not None
    assert result.interruption.reason == "quota"
    # Output-shaped failures are normally split and re-asked; an exhausted
    # account would only refuse every half, so it stops on the first refusal.
    assert exhausted.refusals == 1
    assert not any(entry.status is EntryStatus.FAILED for entry in result.entries)


@pytest.mark.asyncio
async def test_short_throttling_fails_the_batch_without_stopping_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A per-minute limit reopens by itself: until refusals outlast
    # CAPACITY_GIVE_UP_SECONDS (120s, untouched here) the run goes on, and the
    # refused batches fail like any transport error.
    monkeypatch.setattr(orchestrator, "TRANSPORT_BACKOFF_BASE", 0.0)
    modpack, scan = _lang_pack(tmp_path, LANG_ENTRIES)
    exhausted = ExhaustedAfter(2, QuotaExhausted("429 Too Many Requests"))
    _, result = await _run(
        _lang_config(modpack, tmp_path / "out"), exhausted, scan=scan
    )

    assert result.interruption is None
    assert exhausted.refusals == orchestrator.MAX_TRANSPORT_RETRIES + 1
    statuses = [entry.status for entry in result.entries]
    assert statuses.count(EntryStatus.PASSED) == 4
    assert statuses.count(EntryStatus.FAILED) == 2


@pytest.mark.asyncio
async def test_spent_openai_quota_interrupts_without_backoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Real backoff timing: an empty account must not wait for any of it.
    monkeypatch.setattr(orchestrator, "TRANSPORT_BACKOFF_BASE", 30.0)
    modpack, scan = _lang_pack(tmp_path, LANG_ENTRIES)
    # LiteLLM's RateLimitError says only "You exceeded your current quota";
    # the insufficient_quota code lives on the OpenAI SDK error it wraps.
    refusal = QuotaExhausted("OpenAIException - You exceeded your current quota")
    sdk_error = Exception("Error code: 429")
    sdk_error.code = "insufficient_quota"  # type: ignore[attr-defined]
    refusal.__cause__ = sdk_error
    exhausted = ExhaustedAfter(1, refusal)
    _, result = await asyncio.wait_for(
        _run(_lang_config(modpack, tmp_path / "out"), exhausted, scan=scan),
        timeout=10,
    )

    assert result.interruption is not None
    assert result.interruption.reason == "quota"
    assert exhausted.refusals == 1
    assert not any(entry.status is EntryStatus.FAILED for entry in result.entries)


@pytest.mark.asyncio
async def test_resume_resends_only_entries_whose_source_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(orchestrator, "TRANSPORT_BACKOFF_BASE", 0.0)
    monkeypatch.setattr(orchestrator, "CAPACITY_GIVE_UP_SECONDS", 0.0)
    modpack, scan = _lang_pack(tmp_path, LANG_ENTRIES)
    checkpoint = TranslationCheckpoint(tmp_path / "session.jsonl")
    checkpoint.start({"session_id": "s1", "params": {}})
    _, partial = await _run(
        _lang_config(modpack, tmp_path / "out"),
        ExhaustedAfter(2, QuotaExhausted("429 Too Many Requests")),
        scan=scan,
        checkpoint=checkpoint,
    )
    done_keys = {entry.key for entry in partial.entries}
    changed_key = sorted(done_keys)[0]

    # The modpack updated that string while the run was interrupted.
    edited = {**LANG_ENTRIES, changed_key: "A brand new objective"}
    modpack, scan = _lang_pack(tmp_path, edited)
    translator = RecordingTranslator()
    _, resumed = await _run(
        _lang_config(modpack, tmp_path / "out"),
        translator,
        scan=scan,
        checkpoint=checkpoint,
        resume_from=checkpoint.load(),
    )

    sent = {key for _file, key in translator.sent}
    assert sent == (set(LANG_ENTRIES) - done_keys) | {changed_key}
    changed = next(entry for entry in resumed.entries if entry.key == changed_key)
    assert changed.translated_text == "KO A brand new objective"


@pytest.mark.asyncio
async def test_killed_run_resumes_with_only_the_remaining_entries(
    tmp_path: Path,
) -> None:
    """Acceptance: a run killed after N batches resumes from what reached
    disk, sends only the rest, and ends exactly where an uninterrupted run
    would — the same entries and the same generated output trees."""
    modpack = tmp_path / "modpack"
    shutil.copytree(FIXTURE, modpack, ignore=shutil.ignore_patterns(".mct_cache"))

    def config(out: str, tm: str) -> PipelineConfig:
        return PipelineConfig(
            modpack_path=modpack,
            output_dir=tmp_path / out,
            tm_db_path=tmp_path / tm,
            glossary_store_dir=tmp_path / "glossaries",
            use_vanilla_glossary=False,
            batch_size=3,
            max_concurrent=1,
            file_workers=1,
        )

    reference_translator = RecordingTranslator()
    _, reference = await _run(config("reference", "reference.sqlite3"), reference_translator)
    assert reference_translator.calls > 4

    checkpoint = TranslationCheckpoint(tmp_path / "session.jsonl")
    checkpoint.start({"session_id": "s1", "params": {}})
    hanging = HangingAfter(3)
    pipeline = TranslationPipeline(
        config("run", "run.sqlite3"),
        lm=dspy.utils.DummyLM([]),
        checkpoint=checkpoint,
    )
    pipeline.translator = hanging
    task = asyncio.create_task(pipeline.run())
    try:
        await asyncio.wait_for(hanging.hung.wait(), timeout=10)
        # Wait until every batch the provider answered is durable, then take
        # the file exactly as a killed process would leave it.
        deadline = time.monotonic() + 10
        while True:
            state = checkpoint.load()
            if state is not None and set(hanging.sent) <= set(state.entries):
                break
            assert time.monotonic() < deadline, "batches never reached disk"
            await asyncio.sleep(0.01)
        killed = TranslationCheckpoint(tmp_path / "killed.jsonl")
        shutil.copy(checkpoint.path, killed.path)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    finally:
        pipeline.close()

    on_disk = killed.load()
    assert on_disk is not None and on_disk.entries
    resumed_translator = RecordingTranslator()
    events: list[str] = []
    resumed_pipeline, resumed = await _run(
        config("run", "run.sqlite3"),
        resumed_translator,
        checkpoint=killed,
        resume_from=on_disk,
        events=events,
    )
    # The screen is told the replay happened before any batch is sent.
    assert "resume" in events
    assert events.index("resume") < events.index("batch_started")

    # Only the remaining entries reached the model: nothing paid for twice,
    # and together the two runs asked exactly what one run would have.
    assert set(resumed_translator.sent).isdisjoint(hanging.sent)
    assert set(resumed_translator.sent).isdisjoint(on_disk.entries)
    assert set(resumed_translator.sent) | set(hanging.sent) == set(
        reference_translator.sent
    )
    assert resumed_translator.calls == reference_translator.calls - hanging.calls
    assert resumed_pipeline.resumed_entries == len(on_disk.entries)
    # ...and the result is the uninterrupted one.
    assert resumed.interruption is None
    assert _entry_view(resumed.entries) == _entry_view(reference.entries)
    assert _tree(tmp_path / "run") == _tree(tmp_path / "reference")


# -- server: lifecycle and API ---------------------------------------------------


@pytest.fixture
def engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, dict[str, Any]]]:
    """Sidecar running the REAL pipeline over the repo fixture, with the
    provider swapped for a fake (``box["translator"]``)."""
    box: dict[str, Any] = {"translator": RecordingTranslator()}
    monkeypatch.setattr(orchestrator, "TRANSPORT_BACKOFF_BASE", 0.0)
    monkeypatch.setattr(orchestrator, "CAPACITY_GIVE_UP_SECONDS", 0.0)
    monkeypatch.setattr(
        orchestrator, "build_lm", lambda *_args, **_kwargs: dspy.utils.DummyLM([])
    )
    monkeypatch.setattr(
        orchestrator,
        "load_translator",
        lambda *_args, **_kwargs: (box["translator"], None),
    )
    modpack = tmp_path / "modpack"
    shutil.copytree(FIXTURE, modpack, ignore=shutil.ignore_patterns(".mct_cache"))
    box["modpack"] = modpack
    box["root"] = tmp_path
    app = create_app(
        token=TOKEN,
        config_dir=tmp_path / "config",
        tm_db_path=tmp_path / "tm.sqlite3",
    )
    with TestClient(app) as client:
        yield client, box


def _wait(client: TestClient, job_id: str, timeout: float = 60.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/jobs/{job_id}", headers=AUTH).json()
        if body["status"] in {"done", "failed", "cancelled", "interrupted"}:
            return body
        time.sleep(0.02)
    pytest.fail(f"job {job_id} did not finish within {timeout}s")


def _translate(
    client: TestClient,
    box: dict[str, Any],
    session_id: str,
    tag: str,
    **extra: Any,
) -> str:
    response = client.post(
        "/jobs",
        headers=AUTH,
        json={
            "type": "translate",
            "params": {
                "session_id": session_id,
                "modpack_path": str(box["modpack"]),
                "output_dir": str(box["root"] / f"out-{tag}"),
                "tm_db_path": str(box["root"] / f"tm-{tag}.sqlite3"),
                "model": "openai/gpt-test",
                "api_key": "sk-secret-value",
                "batch_size": 3,
                "max_concurrent": 1,
                "file_workers": 1,
                "use_vanilla_glossary": False,
                **extra,
            },
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _entries(client: TestClient, job_id: str) -> dict[tuple[str, str], tuple[str, str]]:
    body = client.get(
        f"/translate/{job_id}/entries?page_size=500", headers=AUTH
    ).json()
    return {
        (entry["file"], entry["key"]): (entry["translated_text"], entry["status"])
        for entry in body["entries"]
    }


def _checkpoint_ids(client: TestClient) -> dict[str, dict[str, Any]]:
    response = client.get("/checkpoints", headers=AUTH)
    assert response.status_code == 200
    return {summary["session_id"]: summary for summary in response.json()}


def test_quota_interrupted_job_lists_and_resumes_through_the_api(
    engine: tuple[TestClient, dict[str, Any]],
) -> None:
    client, box = engine

    # Uninterrupted reference run: done, and leaves no checkpoint behind.
    reference_translator = RecordingTranslator()
    box["translator"] = reference_translator
    reference_id = _translate(client, box, str(uuid.uuid4()), "reference")
    assert _wait(client, reference_id)["status"] == "done"
    assert reference_id not in _checkpoint_ids(client)
    reference = _entries(client, reference_id)

    # The provider's daily quota runs out after two batches.
    session_id = str(uuid.uuid4())
    exhausted = ExhaustedAfter(2, QuotaExhausted("RESOURCE_EXHAUSTED per day"))
    box["translator"] = exhausted
    _translate(client, box, session_id, "run")
    job = _wait(client, session_id)
    assert job["status"] == "interrupted"
    assert "이어서 번역" in job["error"]
    terminal = client.get(f"/jobs/{session_id}/snapshot", headers=AUTH).json()["events"][-1]
    assert terminal["type"] == "interrupted"
    assert terminal["reason"] == "quota"
    assert terminal["resumable"] is True

    listed = _checkpoint_ids(client)[session_id]
    assert listed["state"] == "interrupted"
    assert listed["reason"] == "quota"
    assert listed["target_locale"] == "ko_kr"
    assert listed["total_entries"] > listed["translated_entries"] > 0
    # Secrets never reach disk; the desktop re-sends the key on resume.
    checkpoint_text = (box["root"] / "config/checkpoints" / f"{session_id}.jsonl").read_text(
        encoding="utf-8"
    )
    assert "sk-secret-value" not in checkpoint_text
    # The partial result stays reviewable, like a user cancel.
    partial = _entries(client, session_id)
    partial_statuses = {status for _text, status in partial.values()}
    assert "passed" in partial_statuses
    assert "failed" not in partial_statuses
    assert len(partial) < len(reference)

    # Resume: only the session id, the flag and the connection settings.
    resumed_translator = RecordingTranslator()
    box["translator"] = resumed_translator
    response = client.post(
        "/jobs",
        headers=AUTH,
        json={
            "type": "translate",
            "params": {"session_id": session_id, "resume": True, "api_key": "k2"},
        },
    )
    assert response.status_code == 201, response.text
    assert _wait(client, session_id)["status"] == "done"
    resume_frames = [
        frame
        for frame in client.app.state.job_manager.subscribe(session_id)[0]
        if frame["type"] == "resume"
    ]
    # First the loaded checkpoint (said before the rescan), then the replay.
    assert resume_frames[0]["checkpointed"] == len(exhausted.sent)
    assert resume_frames[-1]["reused"] == len(exhausted.sent)
    assert resume_frames[-1]["remaining"] == len(resumed_translator.sent)

    assert set(resumed_translator.sent).isdisjoint(exhausted.sent)
    assert set(resumed_translator.sent) | set(exhausted.sent) == set(
        reference_translator.sent
    )
    assert _entries(client, session_id) == reference
    assert _tree(box["root"] / "out-run") == _tree(box["root"] / "out-reference")
    # A completed run's checkpoint is not offered again.
    assert session_id not in _checkpoint_ids(client)


def test_fresh_run_and_session_delete_discard_the_checkpoint(
    engine: tuple[TestClient, dict[str, Any]],
) -> None:
    client, box = engine
    session_id = str(uuid.uuid4())
    box["translator"] = ExhaustedAfter(2, QuotaExhausted("rate limit"))
    _translate(client, box, session_id, "run")
    assert _wait(client, session_id)["status"] == "interrupted"
    assert session_id in _checkpoint_ids(client)

    # Starting over explicitly reuses nothing the interrupted run paid for.
    fresh = RecordingTranslator()
    box["translator"] = fresh
    _translate(client, box, session_id, "run")
    assert _wait(client, session_id)["status"] == "done"
    reference = RecordingTranslator()
    box["translator"] = reference
    reference_id = _translate(client, box, str(uuid.uuid4()), "reference")
    assert _wait(client, reference_id)["status"] == "done"
    assert set(fresh.sent) == set(reference.sent)
    assert session_id not in _checkpoint_ids(client)

    # Deleting a session also drops its checkpoint, so it cannot resurface.
    doomed = str(uuid.uuid4())
    box["translator"] = ExhaustedAfter(1, QuotaExhausted("rate limit"))
    _translate(client, box, doomed, "doomed")
    assert _wait(client, doomed)["status"] == "interrupted"
    assert client.delete(f"/sessions/{doomed}", headers=AUTH).status_code == 200
    assert doomed not in _checkpoint_ids(client)
    resume = client.post(
        "/jobs",
        headers=AUTH,
        json={"type": "translate", "params": {"session_id": doomed, "resume": True}},
    )
    assert resume.status_code == 404
    assert client.delete(f"/checkpoints/{doomed}", headers=AUTH).status_code == 404

    # A hand-translation seed replaces the session's work as well, so the AI
    # run's checkpoint must not resurface as "이어서 번역" afterwards.
    seeded = str(uuid.uuid4())
    box["translator"] = ExhaustedAfter(1, QuotaExhausted("rate limit"))
    _translate(client, box, seeded, "seeded")
    assert _wait(client, seeded)["status"] == "interrupted"
    _translate(client, box, seeded, "seeded", manual_seed=True)
    assert _wait(client, seeded)["status"] == "done"
    assert seeded not in _checkpoint_ids(client)


class RetranslatingTranslator:
    """The review screen's "AI retranslate": a recognisably different answer."""

    async def acall(self, *, entries: dict[str, str], **_: Any) -> dspy.Prediction:
        return dspy.Prediction(
            translations={key: f"다시 번역 {text}" for key, text in entries.items()},
            failed={},
        )


def _shipped_text(root: Path) -> str:
    """Every generated file's text with JSON unescaped: "did it ship"."""
    parts: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if path.suffix == ".json":
            with contextlib.suppress(ValueError):
                text = json.dumps(json.loads(text), ensure_ascii=False)
        parts.append(text)
    return "\n".join(parts)


@pytest.mark.parametrize("relaunch", [False, True], ids=["same-process", "relaunch"])
def test_resume_keeps_review_edits_and_the_scan_payload(
    engine: tuple[TestClient, dict[str, Any]], relaunch: bool
) -> None:
    """Edits made on the interrupted partial result win over checkpoint text:
    a hand commit and an AI retranslation are neither re-sent nor replaced,
    they ship, and the resumed run's final save keeps them."""
    client, box = engine
    manager = client.app.state.job_manager
    scan = client.post(
        "/jobs",
        headers=AUTH,
        json={"type": "scan", "params": {"modpack_path": str(box["modpack"])}},
    )
    assert scan.status_code == 201, scan.text
    scan_id = scan.json()["id"]
    assert _wait(client, scan_id)["status"] == "done"

    session_id = str(uuid.uuid4())
    box["translator"] = ExhaustedAfter(2, QuotaExhausted("RESOURCE_EXHAUSTED per day"))
    _translate(client, box, session_id, "run", scan_job_id=scan_id)
    assert _wait(client, session_id)["status"] == "interrupted"
    scan_payload = client.get(f"/scan/{session_id}/result", headers=AUTH).json()
    assert scan_payload["categories"]

    passed = sorted(
        ref
        for ref, (_text, status) in _entries(client, session_id).items()
        if status == "passed" and re.fullmatch(r"[\w.]+", ref[1])
    )
    (edit_file, edit_key), (retry_file, retry_key) = passed[:2]
    patched = client.patch(
        f"/translate/{session_id}/entries/{edit_key}",
        headers=AUTH,
        json={"translated_text": "사람이 고친 번역", "file": edit_file},
    )
    assert patched.status_code == 200, patched.text
    box["translator"] = RetranslatingTranslator()
    retried = client.post(
        f"/translate/{session_id}/entries/{retry_key}/retranslate",
        headers=AUTH,
        json={"file": retry_file},
    )
    assert retried.status_code == 200, retried.text
    retranslated = retried.json()["translated_text"]
    assert retranslated.startswith("다시 번역")

    if relaunch:
        # A new engine process: neither the run, its scan nor the scan cache
        # is in memory, so the edits come from the session file and journal.
        manager.forget_job(session_id)
        manager.forget_job(scan_id)
        manager._scan_cache.clear()

    resumed = RecordingTranslator()
    box["translator"] = resumed
    response = client.post(
        "/jobs",
        headers=AUTH,
        json={"type": "translate", "params": {"session_id": session_id, "resume": True}},
    )
    assert response.status_code == 201, response.text
    assert _wait(client, session_id)["status"] == "done"

    edited = {
        (edit_file, edit_key): ("사람이 고친 번역", "modified"),
        (retry_file, retry_key): (retranslated, "modified"),
    }
    assert set(edited).isdisjoint(resumed.sent)
    final = _entries(client, session_id)
    assert {ref: final[ref] for ref in edited} == edited
    shipped = _shipped_text(box["root"] / "out-run")
    assert "사람이 고친 번역" in shipped
    assert retranslated in shipped
    # The resumed run's final save holds both, and the scan screen survives.
    manager.forget_job(session_id)
    reloaded = _entries(client, session_id)
    assert {ref: reloaded[ref] for ref in edited} == edited
    assert client.get(f"/scan/{session_id}/result", headers=AUTH).json() == scan_payload


def test_delete_empties_a_checkpoint_it_cannot_unlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows refuses to unlink a file a concurrent GET /checkpoints has
    open; the deleted session must still never come back as resumable."""
    store = CheckpointStore(tmp_path)
    checkpoint = store.for_session("s1")
    checkpoint.start({"session_id": "s1", "params": {}})
    checkpoint.record_batch("a.json", [("k", source_digest("A"), "가")])
    original_unlink = Path.unlink

    def locked_unlink(self: Path, missing_ok: bool = False) -> None:
        if self == checkpoint.path:
            raise PermissionError(13, "The process cannot access the file")
        original_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", locked_unlink)
    monkeypatch.setattr(checkpoint_module, "_DELETE_RETRY_SECONDS", 0.0)

    assert store.delete("s1")
    assert store.summaries() == []
    assert store.for_session("s1").load() is None
    assert not store.for_session("s1").exists()


@pytest.mark.asyncio
async def test_refused_cli_login_interrupts_instead_of_backing_off(tmp_path: Path) -> None:
    import litellm

    from moru_engine.cli_providers.credentials import CliAuthError

    modpack, scan = _lang_pack(tmp_path, LANG_ENTRIES)
    try:
        raise litellm.exceptions.APIConnectionError(
            message="login", llm_provider="claude-code", model="x"
        ) from CliAuthError("`claude login`으로 다시 로그인해 주세요.")
    except litellm.exceptions.APIConnectionError as exc:
        wrapped = exc
    refused = ExhaustedAfter(1, wrapped)
    _, result = await _run(_lang_config(modpack, tmp_path / "out"), refused, scan=scan)

    assert result.interruption is not None
    assert result.interruption.reason == "auth"
    assert refused.refusals == 1
