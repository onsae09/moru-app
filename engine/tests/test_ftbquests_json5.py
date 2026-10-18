"""FTB Quests 26.1.2+ JSON5 quest books: scan, extract, write back.

FTB Quests 26.1.2 replaced every SNBT file with JSON5 and split the lang
file into a per-locale directory tree: ``lang/<locale>/chapters/<x>.json5``
holds a chapter's quest/task text, ``lang/<locale>/<object_type>.json5``
the rest, and the game merges a locale's whole tree into one table. Packs
on that version (FTB Evolution 2) came back with no quest translation at
all, because neither the scanner nor the handler accepted ``.json5``.

Fixtures are trimmed from AllTheMods/ATM-11, which ships this layout with
en_us, ko_kr and other locales side by side.
"""

from __future__ import annotations

import json
from pathlib import Path

import dspy
import pyjson5
import pytest

from moru_engine.handlers.ftbquests import FTBQuestsHandler
from moru_engine.parsers import BaseParser, JSON5Parser, ParseError
from moru_engine.pipeline import EntryStatus, PipelineConfig, TranslationPipeline
from moru_engine.scanner import scan_modpack

QUESTS = Path("config/ftbquests/quests")

#: lang/en_us/chapters/powah.json5 — game-written style: quoted dotted keys,
#: descriptions as string arrays, trailing commas (strict JSON rejects it).
#: ``\\&`` is FTB's escaped literal ampersand ("Forbidden \\& Arcanus").
EN_POWAH = r"""{
  "quest.33816AF0E699F19F.quest_desc": [
    "This block will drain FE from any charged item.",
  ],
  "quest.66ECC26BC81D0093.title": "Tier: &bBasic",
  "task.36E378BF22554E0A.title": "Energy Cables",
  "quest.6B2027DA7AA6FF34.quest_subtitle": "Forbidden \\& Arcane power",
  "quest.6B2027DA7AA6FF34.quest_desc": [
    "&9Powah&r is a tech mod that's all about generating, storing, and transmitting &dPower&r.\n\nTo get started, go out and mine some &aUraninite&r!",
    "",
    "{@pagebreak}",
    "{\"text\":\"Open the guide\",\"color\":\"aqua\",\"clickEvent\":{\"action\":\"run_command\",\"value\":\"/powah guide\"}}",
  ],
}"""

#: lang/en_us/chapter.json5 — every chapter's title/subtitle in one file.
EN_CHAPTER = r"""{
  "chapter.2A6EBEEBAB882679.title": "Powah",
  "chapter.2A6EBEEBAB882679.chapter_subtitle": [
    "And Other Spawners",
  ],
}"""

#: lang/ko_kr/chapters/powah.json5 — the pack already ships two of them.
KO_POWAH = r"""{
  "quest.33816AF0E699F19F.quest_desc": [
    "이 블록은 충전된 모든 아이템에서 FE를 배수합니다.",
  ],
  "quest.66ECC26BC81D0093.title": "티어: &b기본",
}"""

JA_POWAH = r"""{
  "quest.66ECC26BC81D0093.title": "ティア: &bベーシック",
}"""

#: chapters/powah.json5 — quest DATA. 26.1.2 serializes item components as
#: structured JSON: the icon's rainbow name is one ``text`` leaf per letter.
#: A toast reward's ``description`` is the one inline text the game reads.
POWAH_DATA = r"""{
  id: "2A6EBEEBAB882679",
  group: "49AECADBA9E79A3B",
  order_index: 0,
  filename: 'powah',
  icon: {
    id: "powah:energy_cell_basic",
    count: 1,
    components: {
      "minecraft:custom_name": {
        text: "",
        extra: [
          { text: "R", color: "blue", },
          { text: "a", color: "red", },
        ],
      },
    },
  },
  quests: [
    {
      id: "6B2027DA7AA6FF34",
      x: -0.49642857142856656,
      y: 0.0,
      shape: "gear",
      size: 0x2,
      optional: true,
      tasks: [
        {
          id: "36E378BF22554E0A",
          type: "item",
          item: {
            id: "powah:energy_cable_starter",
            count: 1,
            components: {
              "minecraft:lore": [
                { text: "Starter", italic: false, },
              ],
            },
          },
        },
      ],
      rewards: [
        {
          id: "4956CB79E44FB523",
          type: "toast",
          description: "You found &aUraninite&r!",
        },
      ],
    },
  ],
  // The game never writes comments, but hand-edited packs carry them.
}"""

DATA = r"""{
  version: 13,
  lock_message: "",
  fallback_locale: "en_us",
  icon: { id: "allthetweaks:atm_star", count: 1, },
}"""


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _modpack(root: Path) -> Path:
    quests = root / QUESTS
    _write(quests / "lang/en_us/chapters/powah.json5", EN_POWAH)
    _write(quests / "lang/en_us/chapter.json5", EN_CHAPTER)
    _write(quests / "lang/ko_kr/chapters/powah.json5", KO_POWAH)
    _write(quests / "lang/ja_jp/chapters/powah.json5", JA_POWAH)
    _write(quests / "chapters/powah.json5", POWAH_DATA)
    _write(quests / "data.json5", DATA)
    return root


@pytest.mark.asyncio
async def test_parser_round_trips_json5_and_keeps_errors_short(
    tmp_path: Path,
) -> None:
    source = _write(tmp_path / "chapters/powah.json5", POWAH_DATA)
    flat = dict(await JSON5Parser(source).parse())

    # Same key scheme as the SNBT parser: dotted objects, [i] lists.
    assert flat["filename"] == "powah"
    assert flat["icon.components.minecraft:custom_name.extra[1].text"] == "a"
    assert flat["quests[0].rewards[0].description"] == "You found &aUraninite&r!"
    assert "quests[0].size" not in flat  # only strings are flattened

    out = tmp_path / "out/powah.json5"
    out.parent.mkdir()
    await JSON5Parser(out, original_path=source).dump(
        {"quests[0].rewards[0].description": "&aUraninite&r 발견!"}
    )
    expected = pyjson5.decode(POWAH_DATA)
    expected["quests"][0]["rewards"][0]["description"] = "&aUraninite&r 발견!"
    written = out.read_text(encoding="utf-8")
    # Values (hex literals included), booleans and key order survive; the
    # Korean is written as UTF-8, not \u escapes.
    assert pyjson5.decode(written) == expected
    assert list(pyjson5.decode(written)) == list(expected)
    assert "발견" in written

    # A broken file fails as a ParseError naming the problem, without
    # dumping the partially decoded document into the log line.
    broken = _write(
        tmp_path / "broken.json5",
        '{"quest.0000000000000001.title": "' + "x" * 500 + '",,}',
    )
    with pytest.raises(ParseError) as caught:
        await JSON5Parser(broken).parse()
    assert "x" * 50 not in str(caught.value)


@pytest.mark.asyncio
async def test_scan_pairs_locale_trees_and_extracts_only_quest_text(
    tmp_path: Path,
) -> None:
    modpack = _modpack(tmp_path / "modpack")
    quests = modpack / QUESTS

    result = await scan_modpack(modpack)

    assert {tf.file_type for tf in result.translation_files} == {"ftbquests"}
    pairs = {
        pair.source_path.relative_to(quests).as_posix(): (
            pair.target_path.relative_to(quests).as_posix()
            if pair.target_path
            else None
        )
        for pair in result.all_translation_pairs
    }
    # The existing ko_kr file pairs as the target of its en_us mirror; the
    # ja_jp tree is another locale and is neither a source nor a target.
    assert pairs == {
        "lang/en_us/chapters/powah.json5": "lang/ko_kr/chapters/powah.json5",
        "lang/en_us/chapter.json5": None,
        "chapters/powah.json5": None,
        "data.json5": None,
    }
    assert result.target_only_files == []

    handler = FTBQuestsHandler()
    assert dict(await handler.extract(quests / "lang/en_us/chapters/powah.json5")) == {
        "quest.33816AF0E699F19F.quest_desc[0]": (
            "This block will drain FE from any charged item."
        ),
        "quest.66ECC26BC81D0093.title": "Tier: &bBasic",
        "task.36E378BF22554E0A.title": "Energy Cables",
        "quest.6B2027DA7AA6FF34.quest_subtitle": "Forbidden \\& Arcane power",
        "quest.6B2027DA7AA6FF34.quest_desc[0]": (
            "&9Powah&r is a tech mod that's all about generating, storing, "
            "and transmitting &dPower&r.\n\nTo get started, go out and mine "
            "some &aUraninite&r!"
        ),
        "quest.6B2027DA7AA6FF34.quest_desc[2]": "{@pagebreak}",
        "quest.6B2027DA7AA6FF34.quest_desc[3]::jsonseg[0]": "Open the guide",
    }
    assert dict(await handler.extract(quests / "lang/en_us/chapter.json5")) == {
        "chapter.2A6EBEEBAB882679.title": "Powah",
        "chapter.2A6EBEEBAB882679.chapter_subtitle[0]": "And Other Spawners",
    }
    # Quest data: the toast text is extracted, the item names and lore in
    # item components are not, and the book settings carry no text at all.
    assert dict(await handler.extract(quests / "chapters/powah.json5")) == {
        "quests[0].rewards[0].description": "You found &aUraninite&r!",
    }
    assert dict(await handler.extract(quests / "data.json5")) == {}


class RecordingTranslator:
    def __init__(self) -> None:
        self.sources: list[str] = []

    async def acall(self, *, entries, **_kwargs):
        self.sources.extend(entries.values())
        return dspy.Prediction(
            translations={key: f"KO {value}" for key, value in entries.items()},
            failed={},
        )


@pytest.mark.asyncio
async def test_pipeline_writes_target_locale_tree_as_valid_json5(
    tmp_path: Path,
) -> None:
    modpack = _modpack(tmp_path / "modpack")
    config = PipelineConfig(
        modpack_path=modpack,
        output_dir=tmp_path / "out",
        use_tm=False,
        use_user_glossary=False,
        use_mod_translations=False,
    )
    translator = RecordingTranslator()
    pipeline = TranslationPipeline(config, lm=dspy.utils.DummyLM([]))
    pipeline.translator = translator
    try:
        result = await pipeline.run()
    finally:
        pipeline.close()

    assert result.stats.failed_entries == 0
    # Item-component letters, lore and other locales never reach the model.
    assert not {"R", "a", "Starter"} & set(translator.sources)
    assert not any("ティア" in text for text in translator.sources)
    statuses = {entry.key: entry.status for entry in result.entries}
    assert statuses["quest.66ECC26BC81D0093.title"] is EntryStatus.SKIPPED

    out = tmp_path / "out/overrides" / QUESTS
    source = pyjson5.decode(EN_POWAH)
    written = pyjson5.decode(
        (out / "lang/ko_kr/chapters/powah.json5").read_text(encoding="utf-8")
    )
    # Identical keys and list shapes, so the game's merged ko_kr table
    # covers exactly what en_us defines.
    assert list(written) == list(source)
    assert {key: len(v) for key, v in written.items() if isinstance(v, list)} == {
        key: len(v) for key, v in source.items() if isinstance(v, list)
    }
    # Existing pack translations are kept verbatim.
    assert written["quest.66ECC26BC81D0093.title"] == "티어: &b기본"
    assert written["quest.33816AF0E699F19F.quest_desc"] == [
        "이 블록은 충전된 모든 아이템에서 FE를 배수합니다."
    ]
    assert written["task.36E378BF22554E0A.title"] == "KO Energy Cables"
    assert (
        written["quest.6B2027DA7AA6FF34.quest_subtitle"]
        == "KO Forbidden \\& Arcane power"
    )
    description = written["quest.6B2027DA7AA6FF34.quest_desc"]
    # Colour codes and newlines come back intact; blank lines and the page
    # break marker are not translation units and stay as they were.
    assert description[0] == "KO " + source["quest.6B2027DA7AA6FF34.quest_desc"][0]
    assert description[1:3] == ["", "{@pagebreak}"]
    assert json.loads(description[3]) == {
        "text": "KO Open the guide",
        "color": "aqua",
        "clickEvent": {"action": "run_command", "value": "/powah guide"},
    }

    chapter = pyjson5.decode((out / "lang/ko_kr/chapter.json5").read_text("utf-8"))
    assert chapter == {
        "chapter.2A6EBEEBAB882679.title": "KO Powah",
        "chapter.2A6EBEEBAB882679.chapter_subtitle": ["KO And Other Spawners"],
    }

    # Quest data carries only the toast text; everything else, including
    # the rainbow item name, is byte-for-byte the original structure.
    data = pyjson5.decode((out / "chapters/powah.json5").read_text("utf-8"))
    expected = pyjson5.decode(POWAH_DATA)
    expected["quests"][0]["rewards"][0]["description"] = (
        "KO You found &aUraninite&r!"
    )
    assert data == expected
    assert not (out / "data.json5").exists()
    assert not (out / "lang/en_us").exists()
    assert not (out / "lang/ja_jp").exists()

    # The bilingual export carries the same tree.
    bilingual = tmp_path / "out/bilingual/overrides" / QUESTS
    assert (bilingual / "lang/ko_kr/chapters/powah.json5").is_file()

    # And the written tree reads back through the same handler path a
    # later run uses for the pack's existing translation.
    reread = BaseParser.create_parser(out / "lang/ko_kr/chapters/powah.json5")
    assert isinstance(reread, JSON5Parser)
    assert dict(await reread.parse())["task.36E378BF22554E0A.title"] == (
        "KO Energy Cables"
    )
