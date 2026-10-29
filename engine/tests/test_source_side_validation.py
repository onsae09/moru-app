"""Source-side anomalies must never block review, and notes are not work.

Two review-screen regressions live here:

* A placeholder anomaly that originates in the SOURCE string (repeated
  unindexed "%s" collapsing several arguments into one token) used to fail
  the entry, which discards its translation and can never be cleared by
  retranslation because the ambiguity is not in the translation. It is now
  a non-blocking warning.
* Comment/metadata keys ("_comment": "Misc") used to be extracted as
  translatable text and counted as failures. They are excluded at the
  extraction layer, so they never become entries at all.

Editability itself is asserted through the real PATCH endpoint: the review
screen gates the editor on nothing, so saving must succeed in EVERY status.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

import dspy
import pytest
from fastapi.testclient import TestClient

from moru_engine.parsers import BaseParser
from moru_engine.parsers.base import is_metadata_key
from moru_engine.pipeline import (
    EntryStatus,
    PipelineConfig,
    TranslationPipeline,
)
from moru_engine.server import create_app
from moru_engine.server.jobs import JobRecord, JobStatus, JobType

if TYPE_CHECKING:
    from collections.abc import Iterator

REPORTED_KEY = "commands.scoreboard.players.add.success.single"
REPORTED_SOURCE = "Added %s to %s for %s (now %s)"
#: What vanilla ko_kr actually says for this key: Korean word order forces
#: the four arguments to be reordered, which needs indexed specifiers.
REPORTED_KO = "%3$s의 %2$s에 %1$s을(를) 더했습니다 (이제 %4$s입니다)"

TOKEN = "source-side-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class NumberingTranslator:
    """Reorders the collapsed {{ARG}} by numbering its occurrences.

    ``**_`` absorbs the module's other keyword arguments so this fake keeps
    working when the signature grows.
    """

    def __init__(self, output: str) -> None:
        self.output = output
        self.seen: list[str] = []

    async def acall(self, *, entries: dict[str, str], **_: object) -> dspy.Prediction:
        self.seen.extend(entries)
        translations = {
            key: (self.output if key == REPORTED_KEY else f"KO {text}")
            for key, text in entries.items()
        }
        return dspy.Prediction(translations=translations, failed={})


def _write_pack(root: Path, lang: dict[str, str]) -> Path:
    lang_dir = root / "modpack" / "kubejs" / "assets" / "test" / "lang"
    lang_dir.mkdir(parents=True, exist_ok=True)
    (lang_dir / "en_us.json").write_text(
        json.dumps(lang, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return root / "modpack"


def _config(root: Path) -> PipelineConfig:
    return PipelineConfig(
        modpack_path=root / "modpack",
        output_dir=root / "out",
        tm_db_path=root / "tm.sqlite3",
        # Hermetic: never read the developer's real glossary store.
        glossary_store_dir=root / "glossaries",
        use_vanilla_glossary=False,
        use_tm=False,
        extract_glossary=False,
    )


async def _run(root: Path, translator: NumberingTranslator, lang: dict[str, str]):
    _write_pack(root, lang)
    pipeline = TranslationPipeline(_config(root), lm=dspy.utils.DummyLM([]))
    pipeline.translator = translator
    try:
        return await pipeline.run()
    finally:
        pipeline.close()


def _entry(result: Any, key: str):
    return next((e for e in result.entries if e.key == key), None)


# -- bug 1: a source-side anomaly warns, it does not fail ------------------


async def test_collapsed_source_args_warn_instead_of_failing(
    tmp_path: Path,
) -> None:
    result = await _run(
        tmp_path,
        NumberingTranslator(
            "{{ARG3}}의 {{ARG2}}에 {{ARG1}}을(를) 더했습니다 (이제 {{ARG4}}입니다)"
        ),
        {REPORTED_KEY: REPORTED_SOURCE, "gui.done": "Done"},
    )

    entry = _entry(result, REPORTED_KEY)
    assert entry is not None
    assert entry.status is EntryStatus.WARNING
    assert entry.translated_text == REPORTED_KO
    assert entry.errors == ["Numbered placeholder order changed"]
    # Non-blocking: absent from the failure count, and the translation is
    # actually written out instead of being discarded.
    assert result.stats.failed_entries == 0
    assert result.failed == []
    written = json.loads(
        (
            tmp_path
            / "out"
            / "overrides"
            / "kubejs"
            / "assets"
            / "test"
            / "lang"
            / "ko_kr.json"
        ).read_text(encoding="utf-8")
    )
    assert written[REPORTED_KEY] == REPORTED_KO


async def test_translation_side_placeholder_loss_still_fails(
    tmp_path: Path,
) -> None:
    # The other half of the contract: dropping arguments is the
    # TRANSLATION's fault and must keep failing exactly as before.
    result = await _run(
        tmp_path,
        NumberingTranslator("{{ARG1}}의 {{ARG2}}"),
        {REPORTED_KEY: REPORTED_SOURCE},
    )

    entry = _entry(result, REPORTED_KEY)
    assert entry is not None
    assert entry.status is EntryStatus.FAILED
    assert result.stats.failed_entries == 1


# -- bug 1: editing and saving must work in every status ------------------


@pytest.fixture
def review_client(tmp_path: Path) -> Iterator[tuple[TestClient, Any]]:
    """A finished translate job holding one WARNING and one FAILED entry."""
    from moru_engine.pipeline.orchestrator import EntryResult, PipelineResult

    result = PipelineResult(
        config=_config(tmp_path),
        entries=[
            EntryResult(
                key=REPORTED_KEY,
                file="kubejs/assets/test/lang/en_us.json",
                source_text=REPORTED_SOURCE,
                translated_text=REPORTED_KO,
                status=EntryStatus.WARNING,
                errors=["Numbered placeholder order changed"],
            ),
            EntryResult(
                key="tooltip.item.durability",
                file="kubejs/assets/test/lang/en_us.json",
                source_text="Durability: %s / %s",
                translated_text="{{ARG3}} 내구도",
                status=EntryStatus.FAILED,
                errors=[
                    "Placeholder validation failed for original text: "
                    "'Durability: %s / %s'"
                ],
            ),
        ],
    )
    record = JobRecord(
        id="review-job",
        type=JobType.TRANSLATE,
        params={},
        status=JobStatus.DONE,
        finished=True,
        result=result,
    )
    app = create_app(
        token=TOKEN,
        config_dir=tmp_path / "config",
        tm_db_path=tmp_path / "tm.sqlite3",
        shutdown_handler=threading.Event().set,
    )
    app.state.job_manager.register_job(record)
    with TestClient(app) as client:
        yield client, result


@pytest.mark.parametrize(
    ("key", "status"),
    [(REPORTED_KEY, "warning"), ("tooltip.item.durability", "failed")],
)
def test_manual_edit_saves_in_any_status(
    review_client: tuple[TestClient, Any], key: str, status: str
) -> None:
    # The reported symptom is "번역본 수정이 불가". Saving must succeed for a
    # warned entry AND for a failed one -- the state the user was stuck in.
    client, result = review_client
    listed = client.get("/translate/review-job/entries", headers=AUTH).json()
    assert {e["key"]: e["status"] for e in listed["entries"]}[key] == status

    edited = "%3$s의 %2$s에 %1$s을(를) 손으로 고쳤습니다 (이제 %4$s)"
    response = client.patch(
        f"/translate/review-job/entries/{key}",
        headers=AUTH,
        json={
            "translated_text": edited,
            "file": "kubejs/assets/test/lang/en_us.json",
        },
    )

    assert response.status_code == 200
    assert response.json()["translated_text"] == edited
    assert response.json()["status"] == "modified"
    assert _entry(result, key).translated_text == edited


# -- bug 2: comment/metadata keys are not translation work ----------------


def test_metadata_key_predicate_scope() -> None:
    assert is_metadata_key("_comment")
    assert is_metadata_key("__comment")
    assert is_metadata_key("tooltips._comment")
    # A leading underscore is the whole convention: real keys only ever use
    # one INSIDE a segment.
    assert not is_metadata_key("gui.socialInteractions.tab_all")
    assert not is_metadata_key("block.minecraft.oak_log")
    assert not is_metadata_key("item.minecraft.diamond_sword")
    assert not is_metadata_key("quest.ftbquests.chapter.getting_started")


async def test_json_parser_drops_comment_keys(tmp_path: Path) -> None:
    path = tmp_path / "en_us.json"
    path.write_text(
        json.dumps(
            {
                "_comment": "Misc",
                "__note": ["multi", "line"],
                "_meta": {"author": "someone"},
                "gui.done": "Done",
                "tooltips": {"_comment": "internal", "hint": "Hold shift"},
            }
        ),
        encoding="utf-8",
    )
    parser = BaseParser.create_parser(path)
    assert parser is not None

    assert await parser.parse() == {
        "gui.done": "Done",
        "tooltips.hint": "Hold shift",
    }


async def test_lang_parser_drops_comment_keys(tmp_path: Path) -> None:
    path = tmp_path / "en_us.lang"
    path.write_text(
        "# a real comment line\n_comment=Misc\ngui.done=Done\n",
        encoding="utf-8",
    )
    parser = BaseParser.create_parser(path)
    assert parser is not None

    assert await parser.parse() == {"gui.done": "Done"}


async def test_comment_keys_never_reach_translation_or_failures(
    tmp_path: Path,
) -> None:
    translator = NumberingTranslator("KO")
    result = await _run(
        tmp_path,
        translator,
        {"_comment": "Misc", "gui.done": "Done"},
    )

    assert "_comment" not in translator.seen
    assert _entry(result, "_comment") is None
    assert result.stats.total_entries == 1
    assert result.stats.failed_entries == 0
    assert result.failed == []
    # The note itself survives in the override file, which replaces the
    # whole file: the dump merges into the original structure.
    written = json.loads(
        (
            tmp_path
            / "out"
            / "overrides"
            / "kubejs"
            / "assets"
            / "test"
            / "lang"
            / "ko_kr.json"
        ).read_text(encoding="utf-8")
    )
    assert written["_comment"] == "Misc"


def test_dropped_precision_specifier_is_reported() -> None:
    from moru_engine.validator.translation_validator import TranslationValidator

    issues = TranslationValidator()._check_placeholders("k", "Speed: %.1f", "속도:")
    assert issues
