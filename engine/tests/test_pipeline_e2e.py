"""Pipeline E2E over the repo fixture with a deterministic fake translator.

Exercises the full orchestration: scan -> extract -> existing-target skip
-> TM -> protect -> translate -> restore -> validate -> apply/output.
The DSPy module itself is covered separately with DummyLM.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
from pathlib import Path

import dspy
import pytest

from moru_engine.pipeline import (
    EntryStatus,
    PipelineConfig,
    TranslationPipeline,
)

FIXTURE = Path(__file__).resolve().parents[1] / "test" / "modpack"
PH_RE = re.compile(r"\{\{[A-Z]+\d*\}\}")


class FakeTranslator:
    """Deterministic stand-in for BatchTranslator (duck-typed acall)."""

    def __init__(self, break_predicate=None) -> None:
        self.calls = 0
        self.break_predicate = break_predicate

    async def acall(
        self,
        *,
        source_lang,
        target_lang,
        context,
        glossary,
        style_directives="",
        entries,
        **_extra,
    ):
        self.calls += 1
        translations: dict[str, str] = {}
        for key, text in entries.items():
            if self.break_predicate is not None and self.break_predicate(key):
                translations[key] = PH_RE.sub("", text)  # drop all tokens
            else:
                translations[key] = f"KO {text}"
        return dspy.Prediction(translations=translations, failed={})


class ConcurrentTranslator(FakeTranslator):
    """Tracks actual overlap while returning deterministic translations."""

    def __init__(self, delay: float = 0.02) -> None:
        super().__init__()
        self.delay = delay
        self.active = 0
        self.max_active = 0

    async def acall(self, **kwargs):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(self.delay)
            return await super().acall(**kwargs)
        finally:
            self.active -= 1


class PartiallyBlockingTranslator(FakeTranslator):
    """Completes two batches, then blocks until the pipeline is cancelled."""

    def __init__(self) -> None:
        super().__init__()
        self.blocked = asyncio.Event()

    async def acall(
        self,
        *,
        source_lang,
        target_lang,
        context,
        glossary,
        style_directives="",
        entries,
        **_extra,
    ):
        self.calls += 1
        if self.calls > 2:
            self.blocked.set()
            await asyncio.Event().wait()
        return dspy.Prediction(
            translations={key: f"KO {text}" for key, text in entries.items()},
            failed={},
        )


@pytest.fixture
def modpack(tmp_path: Path) -> Path:
    target = tmp_path / "modpack"
    shutil.copytree(FIXTURE, target, ignore=shutil.ignore_patterns(".mct_cache"))
    return target


def _config(modpack: Path, tmp_path: Path) -> PipelineConfig:
    return PipelineConfig(
        modpack_path=modpack,
        output_dir=tmp_path / "out",
        tm_db_path=tmp_path / "tm.sqlite3",
        # Hermetic store: never read the developer's real platformdirs store.
        glossary_store_dir=tmp_path / "glossaries",
        use_vanilla_glossary=False,  # drop synced vanilla rows; toggle covered below
    )


def test_default_glossary_candidate_budget_is_3000(tmp_path: Path) -> None:
    config = PipelineConfig(modpack_path=tmp_path)
    assert config.glossary_max_terms == 3000
    assert config.glossary_chunk_size == 50
    unlimited = PipelineConfig(modpack_path=tmp_path, glossary_max_terms=None)
    assert unlimited.glossary_max_terms is None


async def _run(
    config: PipelineConfig, translator: FakeTranslator, on_event=None
):
    pipeline = TranslationPipeline(
        config, lm=dspy.utils.DummyLM([]), on_event=on_event
    )
    pipeline.translator = translator
    try:
        return await pipeline.run()
    finally:
        pipeline.close()


@pytest.mark.asyncio
async def test_full_run_translates_and_writes_outputs(
    modpack: Path, tmp_path: Path
) -> None:
    fake = FakeTranslator()
    events: list[tuple[str, dict]] = []
    result = await _run(
        _config(modpack, tmp_path),
        fake,
        on_event=lambda event, payload: events.append((event, payload)),
    )
    stats = result.stats

    assert fake.calls > 0
    assert stats.failed_entries == 0
    assert stats.translated_entries > 0
    # kubejs fixture already ships ~48 ko_kr entries -> reused, not retranslated
    assert stats.skipped_entries > 0
    assert stats.coverage_percent == 100.0
    assert stats.quality_score == 1.0

    source = json.loads(
        (modpack / "kubejs/assets/test/lang/en_us.json").read_text(encoding="utf-8")
    )
    existing = json.loads(
        (modpack / "kubejs/assets/test/lang/ko_kr.json").read_text(encoding="utf-8")
    )
    pending_count = sum(
        1 for key in source if not str(existing.get(key, "")).strip()
    )
    kube_progress = [
        payload
        for event, payload in events
        if event == "progress"
        and str(payload.get("file", "")).endswith(
            "kubejs/assets/test/lang/en_us.json"
        )
    ]
    assert kube_progress
    assert {payload["total"] for payload in kube_progress} == {pending_count}
    assert max(payload["done"] for payload in kube_progress) == pending_count
    assert all(payload["done"] <= payload["total"] for payload in kube_progress)

    out = tmp_path / "out"
    # kubejs cannot ride in a resource pack -> overrides tree, and since the
    # override replaces the whole file it carries existing + fresh entries.
    kubejs_out = out / "overrides/kubejs/assets/test/lang/ko_kr.json"
    assert kubejs_out.exists()
    data = json.loads(kubejs_out.read_text(encoding="utf-8"))
    # placeholders restored: raw tokens must never leak into outputs
    assert not any(PH_RE.search(v) for v in data.values())
    # a format-heavy entry keeps its specifiers
    assert data["tooltip.item.durability"].count("%s") == 2
    assert data["modpack.welcome.title"].count("§6") == 2
    # existing translations reused verbatim
    assert data["item.minecraft.diamond"] == "다이아몬드"
    # newly translated entries came from the fake
    assert data["gui.done"] == "완료" or data["gui.done"].startswith("KO ")

    # jar-extracted mod lang lands in the resource pack tree (never a
    # .mct_cache mirror), next to a valid pack.mcmeta
    pack_lang = out / "resourcepack/assets/testmod/lang/ko_kr.json"
    assert pack_lang.exists()
    assert not (out / "resourcepack/assets/testmod/lang/en_us.json").exists()
    mcmeta = json.loads(
        (out / "resourcepack/pack.mcmeta").read_text(encoding="utf-8")
    )
    assert mcmeta["pack"]["pack_format"] > 0
    assert not any(".mct_cache" in str(p) for p in result.output_files)

    # ftbquests snbt output written (path has no locale token -> same rel path)
    assert (out / "overrides/config/ftbquests/quests/chapters/welcome.snbt").exists()
    # patchouli output lands in the ko_kr book tree
    assert (
        out / "overrides/patchouli_books/testbook/ko_kr/entries/basics/intro.json"
    ).exists()


@pytest.mark.asyncio
async def test_second_run_hits_tm_without_llm_calls(
    modpack: Path, tmp_path: Path
) -> None:
    first = FakeTranslator()
    r1 = await _run(_config(modpack, tmp_path), first)
    assert first.calls > 0 and r1.stats.translated_entries > 0
    # identical source text across files may already hit TM within run 1
    reusable = r1.stats.translated_entries + r1.stats.tm_hits

    second = FakeTranslator()
    r2 = await _run(_config(modpack, tmp_path), second)
    assert second.calls == 0
    assert r2.stats.tm_hits == reusable
    assert r2.stats.failed_entries == 0


@pytest.mark.asyncio
async def test_placeholder_corruption_surfaces_as_failed(
    modpack: Path, tmp_path: Path
) -> None:
    # break every entry whose source contains a protected token
    fake = FakeTranslator(break_predicate=lambda key: True)
    result = await _run(_config(modpack, tmp_path), fake)

    failed = result.failed
    assert failed, "token-dropping translations must surface as failures"
    for entry in failed:
        assert entry.status == EntryStatus.FAILED
        assert entry.errors
    # entries without any placeholder cannot fail via token-dropping
    assert result.stats.translated_entries > 0
    assert result.stats.quality_score < 1.0

    # Failed keys fall back to the SOURCE value in the output (structure-
    # preserving dump writes complete lang files; missing keys would break
    # the game). Corrupted translations must never leak through.
    kubejs_out = tmp_path / "out/overrides/kubejs/assets/test/lang/ko_kr.json"
    data = json.loads(kubejs_out.read_text(encoding="utf-8"))
    source = json.loads(
        (modpack / "kubejs/assets/test/lang/en_us.json").read_text(encoding="utf-8")
    )
    kubejs_failed = {
        e.key for e in failed if e.file == "kubejs/assets/test/lang/en_us.json"
    }
    assert kubejs_failed
    for key in kubejs_failed:
        assert data[key] == source[key], key
    assert not any(PH_RE.search(v) for v in data.values())


def _seed_glossary_store(store: Path, terms: list[dict]) -> None:
    """Write a user glossary store document the way community sync does."""
    store.mkdir(parents=True, exist_ok=True)
    (store / "en_us_ko_kr.json").write_text(
        json.dumps(
            {"source_lang": "en_us", "target_lang": "ko_kr", "terms": terms}
        ),
        encoding="utf-8",
    )


class GlossaryCapturingTranslator(FakeTranslator):
    """Records the rendered per-batch glossary strings."""

    def __init__(self) -> None:
        super().__init__()
        self.glossaries: list[str] = []

    async def acall(
        self,
        *,
        source_lang,
        target_lang,
        context,
        glossary,
        style_directives="",
        entries,
        **_extra,
    ):
        self.glossaries.append(glossary)
        return await super().acall(
            source_lang=source_lang,
            target_lang=target_lang,
            context=context,
            glossary=glossary,
            style_directives=style_directives,
            entries=entries,
        )


@pytest.mark.asyncio
async def test_synced_vanilla_glossary_rows_reach_prompts_when_enabled(
    modpack: Path, tmp_path: Path
) -> None:
    # Vanilla terms come from the server-synced store now, not a bundle:
    # "Furnace" is untranslated in the fixture, so the batch that carries it
    # must render the vanilla row into the prompt glossary.
    config = _config(modpack, tmp_path)
    config.use_vanilla_glossary = True
    _seed_glossary_store(
        tmp_path / "glossaries",
        [{"source": "Furnace", "target": "화로", "origin": "vanilla"}],
    )
    fake = GlossaryCapturingTranslator()
    result = await _run(config, fake)
    assert result.stats.failed_entries == 0
    assert any("화로" in g for g in fake.glossaries)


@pytest.mark.asyncio
async def test_use_vanilla_glossary_false_drops_only_vanilla_rows(
    modpack: Path, tmp_path: Path
) -> None:
    config = _config(modpack, tmp_path)  # use_vanilla_glossary=False
    _seed_glossary_store(
        tmp_path / "glossaries",
        [
            {"source": "Furnace", "target": "화로", "origin": "vanilla"},
            {"source": "Wither", "target": "위더", "origin": "manual"},
        ],
    )
    fake = GlossaryCapturingTranslator()
    result = await _run(config, fake)
    assert result.stats.failed_entries == 0
    joined = "\n".join(fake.glossaries)
    assert "화로" not in joined  # vanilla rows gated off by the toggle
    assert "위더" in joined  # manual rows still merge


@pytest.mark.asyncio
async def test_key_scoped_store_row_reaches_only_matching_keys(
    modpack: Path, tmp_path: Path
) -> None:
    # The reported defect, through the whole chain (store row -> TermRule ->
    # GlossaryFilter -> to_context_string -> the prompt the translator gets).
    # The fixture's only "Wither" is entity.minecraft.wither, untranslated so
    # it really gets batched, and the pack has no effect.* key at all: the
    # effect-scoped row must never render, the boss reading must.
    config = _config(modpack, tmp_path)
    _seed_glossary_store(
        tmp_path / "glossaries",
        [
            {
                "source": "Wither",
                "target": "시듦",
                "origin": "manual",
                "key_scope": ["effect.*"],
            },
            {"source": "Wither", "target": "위더", "origin": "manual"},
        ],
    )
    fake = GlossaryCapturingTranslator()
    result = await _run(config, fake)
    assert result.stats.failed_entries == 0
    joined = "\n".join(fake.glossaries)
    # The entity key gets the default reading, rendered in full...
    assert (
        "- **Wither** → **위더** (스타일: 용어 고정) — user glossary (manual)"
        in joined
    )
    # ...and the status-effect reading reaches no prompt at all, neither as
    # a target nor as a scope annotation.
    assert "시듦" not in joined
    assert "적용 키" not in joined

@pytest.mark.asyncio
async def test_include_categories_limits_translation(
    modpack: Path, tmp_path: Path
) -> None:
    full = await _run(_config(modpack, tmp_path), FakeTranslator())
    all_files = {e.file for e in full.entries}
    assert any("kubejs" in f for f in all_files)
    assert any("kubejs" not in f for f in all_files)

    config = _config(modpack, tmp_path / "filtered")
    config.include_categories = ["KubeJS"]
    filtered = await _run(config, FakeTranslator())
    filtered_files = {e.file for e in filtered.entries}
    assert filtered_files
    assert all("kubejs" in f for f in filtered_files)
    assert filtered.stats.total_files < full.stats.total_files


@pytest.mark.asyncio
async def test_events_include_ticker_and_token_frames(
    modpack: Path, tmp_path: Path
) -> None:
    events: list[tuple[str, dict]] = []
    await _run(
        _config(modpack, tmp_path),
        FakeTranslator(),
        on_event=lambda event, payload: events.append((event, payload)),
    )
    tickers = [p for e, p in events if e == "entry_done"]
    assert tickers, "expected sampled entry_done frames"
    for payload in tickers:
        assert payload["key"]
        assert isinstance(payload["source"], str)
        assert isinstance(payload["translated"], str)
        assert len(payload["source"]) <= 120
        assert len(payload["translated"]) <= 120
    token_frames = [p for e, p in events if e == "tokens"]
    assert token_frames, "expected tokens frames after batches"
    assert {"prompt_tokens", "completion_tokens", "total_tokens"} <= set(
        token_frames[-1]
    )


@pytest.mark.asyncio
async def test_max_concurrent_controls_real_provider_overlap(
    modpack: Path, tmp_path: Path
) -> None:
    config = _config(modpack, tmp_path)
    config.use_tm = False
    config.batch_size = 1
    config.max_concurrent = 5
    config.file_workers = 1
    fake = ConcurrentTranslator()
    events: list[tuple[str, dict]] = []

    await _run(config, fake, on_event=lambda e, p: events.append((e, p)))

    assert fake.max_active == 5
    active: set[int] = set()
    max_event_active = 0
    for event, payload in events:
        if event == "batch_started":
            active.add(payload["request_id"])
            max_event_active = max(max_event_active, len(active))
        elif event == "batch_finished":
            active.discard(payload["request_id"])
    assert max_event_active == 5
    assert not active


def test_file_workers_defaults_to_max_concurrent(tmp_path: Path) -> None:
    """Unset file_workers derives from max_concurrent: the GUI only sends
    max_concurrent, and a small fixed file limit would starve the provider
    semaphore on packs with many small files."""
    config = _config(tmp_path, tmp_path)
    config.max_concurrent = 7
    assert config.file_workers is None
    pipeline = TranslationPipeline(config, lm=dspy.utils.DummyLM([]))
    try:
        assert pipeline._file_semaphore._value == 7
        # Hosted providers admit max_concurrent from the start; only a
        # locally hosted runtime begins below it.
        assert pipeline._llm_limiter.limit == 7
    finally:
        pipeline.close()

    explicit = _config(tmp_path, tmp_path / "explicit")
    explicit.max_concurrent = 7
    explicit.file_workers = 2
    pipeline = TranslationPipeline(explicit, lm=dspy.utils.DummyLM([]))
    try:
        assert pipeline._file_semaphore._value == 2
    finally:
        pipeline.close()


@pytest.mark.asyncio
async def test_task_cancellation_returns_reviewable_partial_result(
    modpack: Path, tmp_path: Path
) -> None:
    config = _config(modpack, tmp_path)
    config.use_tm = False
    config.batch_size = 1
    config.max_concurrent = 3
    config.file_workers = 1
    fake = PartiallyBlockingTranslator()
    first_translation = asyncio.Event()
    pipeline = TranslationPipeline(
        config,
        lm=dspy.utils.DummyLM([]),
        on_event=lambda event, _: (
            first_translation.set() if event == "entry_done" else None
        ),
    )
    pipeline.translator = fake
    task = asyncio.create_task(pipeline.run())
    try:
        await asyncio.wait_for(fake.blocked.wait(), timeout=2)
        await asyncio.wait_for(first_translation.wait(), timeout=2)
        task.cancel()
        result = await asyncio.wait_for(task, timeout=2)
    finally:
        pipeline.close()

    assert result.entries
    assert result.stats.translated_entries > 0
    assert result.stats.total_entries == len(result.entries)
    assert result.output_files


@pytest.mark.asyncio
async def test_retranslate_entry_fixes_failed_entry(
    modpack: Path, tmp_path: Path
) -> None:
    config = _config(modpack, tmp_path)
    result = await _run(config, FakeTranslator(break_predicate=lambda key: True))
    failed = result.failed
    assert failed, "fixture run should produce failed entries"
    target = failed[0]

    pipeline = TranslationPipeline(config, lm=dspy.utils.DummyLM([]))
    pipeline.translator = FakeTranslator()  # now behaves
    try:
        entry = await pipeline.retranslate_entry(result, target.key)
    finally:
        pipeline.close()
    assert entry.status is EntryStatus.MODIFIED
    assert entry.errors == []
    assert entry.translated_text is not None
    assert entry.translated_text.startswith("KO ")
    assert result.stats.failed_entries == len(result.failed)


class InventingTranslator:
    """Fake that invents a placeholder token - always a roundtrip failure."""

    async def acall(
        self,
        *,
        source_lang,
        target_lang,
        context,
        glossary,
        style_directives="",
        entries,
        **_extra,
    ):
        return dspy.Prediction(
            translations={k: f"KO {v} {{{{ARG99}}}}" for k, v in entries.items()},
            failed={},
        )


@pytest.mark.asyncio
async def test_retranslate_entry_failure_keeps_passing_entry(
    modpack: Path, tmp_path: Path
) -> None:
    from moru_engine.pipeline import RetranslateError

    config = _config(modpack, tmp_path)
    result = await _run(config, FakeTranslator())
    passing = next(e for e in result.entries if e.status is EntryStatus.PASSED)
    before_text = passing.translated_text

    pipeline = TranslationPipeline(config, lm=dspy.utils.DummyLM([]))
    pipeline.translator = InventingTranslator()
    try:
        with pytest.raises(RetranslateError):
            await pipeline.retranslate_entry(result, passing.key)
    finally:
        pipeline.close()
    assert passing.translated_text == before_text
    assert passing.status is EntryStatus.PASSED
    assert passing.errors == []


@pytest.mark.asyncio
async def test_retranslate_unknown_key_raises(
    modpack: Path, tmp_path: Path
) -> None:
    config = _config(modpack, tmp_path)
    result = await _run(config, FakeTranslator())
    pipeline = TranslationPipeline(config, lm=dspy.utils.DummyLM([]))
    pipeline.translator = FakeTranslator()
    try:
        with pytest.raises(KeyError):
            await pipeline.retranslate_entry(result, "no.such.key")
    finally:
        pipeline.close()


@pytest.mark.asyncio
async def test_apply_entry_edits_rewrites_output(
    modpack: Path, tmp_path: Path
) -> None:
    from moru_engine.pipeline import apply_entry_edits

    config = _config(modpack, tmp_path)
    result = await _run(config, FakeTranslator())
    entry = next(e for e in result.entries if e.status is EntryStatus.PASSED)

    # No modifications yet -> no rewrites.
    assert await apply_entry_edits(result) == 0

    # Simulate a review-screen manual edit (server PATCH semantics).
    entry.translated_text = "검수에서 고친 번역"
    entry.status = EntryStatus.MODIFIED
    rewritten = await apply_entry_edits(result)
    assert rewritten == len(result.output_files) > 0

    joined = "".join(
        p.read_text(encoding="utf-8")
        for p in result.output_files
        if p.is_file() and p.suffix not in {".mcmeta", ".png"}
    )
    assert "검수에서 고친 번역" in joined


# -- glossary curation: mining + chunking + schema-error feedback retry -----

SCHEMA_ERROR = (
    "3 validation errors for list[TermRule] 20.category Input should be "
    "'item', 'block', 'ui', 'entity', 'effect', 'biome' or 'other'"
)


class FlakyGlossaryExtractor:
    """Duck-typed GlossaryExtractor: schema errors for the first N calls."""

    def __init__(self, fail_times: int = 0) -> None:
        self.feedbacks: list[str] = []
        self.candidate_payloads: list[str] = []
        self.fail_times = fail_times

    async def acall(
        self, *, candidates, existing_glossary, target_lang, feedback=""
    ):
        from moru_engine.models import TermRule

        self.feedbacks.append(feedback)
        self.candidate_payloads.append(candidates)
        if len(self.feedbacks) <= self.fail_times:
            raise ValueError(SCHEMA_ERROR)
        rule = TermRule(
            term_ko=f"용어{len(self.feedbacks)}",
            preferred_style="한글 표기",
            aliases=[f"Term{len(self.feedbacks)}"],
            category="item",
        )
        return dspy.Prediction(term_rules=[rule])


class BlockingGlossaryExtractor:
    """Hold curation calls open so request overlap is observable."""

    def __init__(self) -> None:
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self.overlapped = asyncio.Event()
        self.release = asyncio.Event()

    async def acall(
        self, *, candidates, existing_glossary, target_lang, feedback=""
    ):
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        if self.active >= 2:
            self.overlapped.set()
        try:
            await self.release.wait()
        finally:
            self.active -= 1
        return dspy.Prediction(term_rules=[])


def _fixed_candidates(count: int):
    from moru_engine.glossary.term_miner import TermCandidate

    return [
        TermCandidate(term=f"Mined Term {i}", count=i + 1, from_name_key=True)
        for i in range(count)
    ]


def _glossary_config(modpack: Path, tmp_path: Path) -> PipelineConfig:
    config = _config(modpack, tmp_path)
    config.extract_glossary = True
    config.use_user_glossary = False
    config.glossary_chunk_size = 2
    config.glossary_max_retries = 1
    return config


@pytest.mark.asyncio
async def test_glossary_corpus_excludes_existing_target_entries(
    modpack: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, str] = {}

    def capture(entries, *args, **kwargs):
        captured.update(entries)
        return []

    monkeypatch.setattr(
        "moru_engine.pipeline.orchestrator.mine_candidates", capture
    )
    await _run(_glossary_config(modpack, tmp_path), FakeTranslator())

    kube_keys = {
        key.rsplit(":", 1)[-1]
        for key in captured
        if "kubejs/assets/test/lang/en_us.json" in key.replace("\\", "/")
    }
    assert "item.minecraft.diamond" not in kube_keys
    assert "item.minecraft.enchanted_golden_apple" in kube_keys


def _patch_glossary(
    monkeypatch: pytest.MonkeyPatch, fake: FlakyGlossaryExtractor, candidates: int
) -> None:
    monkeypatch.setattr(
        "moru_engine.pipeline.orchestrator.GlossaryExtractor", lambda: fake
    )
    # 4 fixed candidates + chunk_size 2 -> deterministic 2 chunks.
    monkeypatch.setattr(
        "moru_engine.pipeline.orchestrator.mine_candidates",
        lambda *args, **kwargs: _fixed_candidates(candidates),
    )


@pytest.mark.asyncio
async def test_glossary_retry_feeds_error_back_and_recovers(
    modpack: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FlakyGlossaryExtractor(fail_times=1)
    _patch_glossary(monkeypatch, fake, candidates=4)
    events: list[tuple[str, dict]] = []
    result = await _run(
        _glossary_config(modpack, tmp_path),
        FakeTranslator(),
        on_event=lambda e, p: events.append((e, p)),
    )

    # chunk 1 failed once then recovered; chunk 2 clean -> 3 calls total,
    # the retry call carrying the schema error as feedback.
    assert len(fake.feedbacks) == 3
    assert fake.feedbacks[0] == ""
    assert SCHEMA_ERROR in fake.feedbacks[1]
    assert fake.feedbacks[2] == ""
    # Candidate lines carry corpus evidence for the curation prompt.
    assert "Mined Term 0 (x1)" in fake.candidate_payloads[0]

    retries = [p for e, p in events if e == "glossary_progress" and "error" in p]
    assert len(retries) == 1
    assert retries[0]["attempt"] == 1
    assert retries[0]["skipped"] is False
    assert SCHEMA_ERROR[:100] in retries[0]["error"]

    extracted = next(p for e, p in events if e == "glossary_extracted")
    assert extracted["new_terms"] == 2
    assert result.stats.failed_entries == 0


@pytest.mark.asyncio
async def test_glossary_chunk_progress_events(
    modpack: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FlakyGlossaryExtractor()
    _patch_glossary(monkeypatch, fake, candidates=4)
    events: list[tuple[str, dict]] = []
    await _run(
        _glossary_config(modpack, tmp_path),
        FakeTranslator(),
        on_event=lambda e, p: events.append((e, p)),
    )

    steps = [
        (p["done"], p["total"], p["new_terms"])
        for e, p in events
        if e == "glossary_progress"
    ]
    assert steps == [(0, 2, 0), (1, 2, 1), (2, 2, 2)]
    first_batch = next(i for i, (event, _) in enumerate(events) if event == "batch_started")
    glossary_token_frames = [
        payload for event, payload in events[:first_batch] if event == "tokens"
    ]
    assert len(glossary_token_frames) == 2


@pytest.mark.asyncio
async def test_glossary_curation_obeys_max_concurrent(
    modpack: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = BlockingGlossaryExtractor()
    _patch_glossary(monkeypatch, fake, candidates=6)
    config = _glossary_config(modpack, tmp_path)
    config.max_concurrent = 2

    run_task = asyncio.create_task(
        _run(config, FakeTranslator())
    )
    try:
        await asyncio.wait_for(fake.overlapped.wait(), timeout=1)
    finally:
        fake.release.set()
        await run_task

    assert fake.calls == 3
    assert fake.max_active == 2


@pytest.mark.asyncio
async def test_glossary_exhausted_retries_skip_chunk_not_job(
    modpack: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FlakyGlossaryExtractor(fail_times=99)
    _patch_glossary(monkeypatch, fake, candidates=4)
    events: list[tuple[str, dict]] = []
    result = await _run(
        _glossary_config(modpack, tmp_path),
        FakeTranslator(),
        on_event=lambda e, p: events.append((e, p)),
    )

    # 2 chunks x (1 try + 1 retry) - every attempt failed, job still done.
    assert len(fake.feedbacks) == 4
    skips = [p for e, p in events if e == "glossary_progress" and p.get("skipped")]
    assert len(skips) == 2
    extracted = next(p for e, p in events if e == "glossary_extracted")
    assert extracted["new_terms"] == 0
    assert result.stats.failed_entries == 0
    assert result.output_files
