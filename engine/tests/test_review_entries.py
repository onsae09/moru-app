"""AI re-review: flagged entries are re-sent with their problems, and only
an answer that is better replaces the current translation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import dspy
import pytest

from moru_engine.models import Glossary
from moru_engine.pipeline.orchestrator import (
    REVIEW_CONTEXT_HEADER,
    EntryResult,
    EntryStatus,
    PipelineConfig,
    PipelineResult,
    ReviewTarget,
    TranslationPipeline,
)

FILE_A = "config/ftbquests/quests/chapters/intro.snbt"
FILE_B = "assets/somemod/lang/en_us.json"


class ScriptedTranslator:
    """Answers each key from a script; records every batch's context."""

    def __init__(self, answers: dict[str, str]) -> None:
        self.answers = answers
        self.contexts: list[str] = []

    async def acall(self, *, context: str, entries: dict[str, str], **_extra: Any) -> dspy.Prediction:
        self.contexts.append(context)
        return dspy.Prediction(
            translations={k: self.answers[k] for k in entries if k in self.answers},
            failed={},
        )


def _config(tmp_path: Path) -> PipelineConfig:
    return PipelineConfig(
        modpack_path=tmp_path,
        use_tm=False,
        use_user_glossary=False,
        use_vanilla_glossary=False,
        use_translation_graph=False,
    )


def _result(config: PipelineConfig) -> PipelineResult:
    return PipelineResult(
        config=config,
        glossary=Glossary(),
        entries=[
            EntryResult(
                key="quest.title",
                file=FILE_A,
                source_text="Ender Pearl",
                status=EntryStatus.FAILED,
                errors=["llm call failed: boom"],
            ),
            EntryResult(
                key="item.orb",
                file=FILE_B,
                source_text="Void Orb",
                translated_text="보이드 오브",
                status=EntryStatus.PASSED,
            ),
            EntryResult(
                key="item.shard",
                file=FILE_B,
                source_text="Void Shard",
                translated_text="보이드 조각",
                status=EntryStatus.PASSED,
            ),
        ],
    )


def _misses(_key: str, source: str, text: str) -> tuple[str, ...]:
    """Toy glossary: "Void" must be translated as 공허."""
    return ("공허",) if "Void" in source and "공허" not in text else ()


async def _review(
    tmp_path: Path, answers: dict[str, str], targets_for: list[str]
) -> tuple[PipelineResult, Any, ScriptedTranslator]:
    config = _config(tmp_path)
    result = _result(config)
    translator = ScriptedTranslator(answers)
    pipeline = TranslationPipeline(config, lm=dspy.utils.DummyLM([]))
    pipeline.translator = translator
    by_key = {e.key: e for e in result.entries}
    targets = [
        ReviewTarget(entry=by_key[k], problems=[*by_key[k].errors, "required glossary term missing: 공허"])
        for k in targets_for
    ]
    try:
        outcome = await pipeline.review_entries(result, targets, _misses)
    finally:
        pipeline.close()
    return result, outcome, translator


@pytest.mark.asyncio
async def test_review_sends_current_text_and_problems(tmp_path: Path) -> None:
    _, _, translator = await _review(tmp_path, {}, ["item.orb"])
    context = translator.contexts[-1]
    assert context.startswith(REVIEW_CONTEXT_HEADER)
    assert '- item.orb: current="보이드 오브"; problems: required glossary term missing: 공허' in context


@pytest.mark.asyncio
async def test_review_keeps_only_better_answers(tmp_path: Path) -> None:
    result, outcome, _ = await _review(
        tmp_path,
        # orb: fixed. shard: still misses the term, so no better.
        {"quest.title": "엔더 진주", "item.orb": "공허 오브", "item.shard": "보이드 파편"},
        ["quest.title", "item.orb", "item.shard"],
    )
    by_key = {e.key: e for e in result.entries}
    assert {e.key for e in outcome.improved} == {"quest.title", "item.orb"}
    assert outcome.unchanged == 1 and outcome.pending == 0
    assert by_key["quest.title"].status is EntryStatus.PASSED
    assert by_key["quest.title"].errors == []
    assert by_key["item.orb"].translated_text == "공허 오브"
    assert by_key["item.shard"].translated_text == "보이드 조각"
    assert by_key["item.shard"].status is EntryStatus.PASSED
    assert result.stats.failed_entries == 0


@pytest.mark.asyncio
async def test_review_missing_answer_leaves_entry(tmp_path: Path) -> None:
    result, outcome, _ = await _review(tmp_path, {}, ["quest.title"])
    assert outcome.improved == [] and outcome.unchanged == 1
    assert result.entries[0].status is EntryStatus.FAILED


@pytest.mark.asyncio
async def test_review_interruption_keeps_finished_batches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    result = _result(config)
    pipeline = TranslationPipeline(config, lm=dspy.utils.DummyLM([]))
    real = pipeline._translate_batch

    async def quota_on_b(batch, glossary_text, context, **kwargs):
        if FILE_B in context:
            pipeline._interrupt_for_quota(RuntimeError("quota exceeded"))
        return await real(batch, glossary_text, context, **kwargs)

    pipeline.translator = ScriptedTranslator({"quest.title": "엔더 진주"})
    monkeypatch.setattr(pipeline, "_translate_batch", quota_on_b)
    targets = [ReviewTarget(entry=e, problems=["x"]) for e in result.entries]
    try:
        outcome = await pipeline.review_entries(result, targets, _misses)
    finally:
        pipeline.close()
    assert outcome.interruption is not None and outcome.interruption.reason == "quota"
    assert [e.key for e in outcome.improved] == ["quest.title"]
    assert outcome.pending == 2
