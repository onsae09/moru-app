"""Bilingual display-name variant: predicate, rendering, both-tree output."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from moru_engine.output import FileOutput, OutputConfig, OutputGenerator
from moru_engine.output.bilingual import (
    MAX_ANNOTATED_CHARS,
    MAX_SOURCE_CHARS,
    annotate_entries,
    annotate_value,
    should_annotate,
)

KO = "철 곡괭이"
EN = "Iron Pickaxe"


# -- which entries earn a parenthetical --------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "item.minecraft.iron_pickaxe",
        "block.minecraft.bookshelf",
        "entity.minecraft.creeper",
        "enchantment.minecraft.sharpness",
        "effect.minecraft.speed",
        "biome.minecraft.plains",
        "itemGroup.mymod.machines",
        "item_group.mymod.machines",
        "attribute.name.armor",
        # 1.12 spelling: the object name lives in a trailing `.name`.
        "tile.ironOre.name",
        "item.ironPickaxe.name",
    ],
)
def test_display_name_keys_are_annotated(key: str) -> None:
    assert should_annotate(key, EN, KO)


@pytest.mark.parametrize(
    "key",
    [
        # Prose fields under an item-shaped key.
        "item.mymod.wand.tooltip",
        "item.mymod.wand.desc",
        "item.mymod.wand.description",
        "item.mymod.wand.lore",
        "item.mymod.wand.info",
        "item.mymod.wand.subtitle",
        # Numbered siblings are continuation lines of ONE sentence.
        "item.mymod.wand.tooltip2",
        "item.mymod.wand.tooltip3",
        "item.mymod.wand.desc_2",
        "item.mymod.wand.line10",
        "item.mymod.wand.tooltip.2",
        "item.mymod.wand.lore[1]",
        # Compound field name: recognised by its final word.
        "item.minecraft.smithing_template.armor_trim.base_slot_description",
        # Book/guide documents.
        "item.mymod.guide.page1",
        "item.mymod.book.chapter1",
        # Not a display-name namespace at all.
        "ftbquests.chapter.abc.quest.xyz.title",
        "patchouli.mymod.entry.intro",
    ],
)
def test_prose_keys_are_not_annotated(key: str) -> None:
    assert not should_annotate(key, EN, KO)


@pytest.mark.parametrize(
    "key",
    [
        # `NAME_KEY_RE.search` matches `.block.`/`.entity.` mid-key, which is
        # right for term mining and wrong here: these are sound captions and
        # command errors, not names. The head anchor is what excludes them.
        "subtitles.block.anvil.destroy",
        "subtitles.entity.cow.milk",
        "argument.entity.invalid",
        "arguments.item.component.expected",
        "commands.data.entity.invalid",
        "advancements.story.root.description",
        "death.attack.item",
        "gui.item.stack",
    ],
)
def test_non_name_namespaces_are_not_annotated(key: str) -> None:
    assert not should_annotate(key, "All entities", "모든 개체")


def test_document_word_as_the_object_name_is_still_a_name() -> None:
    """`book`/`tome` are real item names when nothing follows them."""
    assert should_annotate("item.minecraft.book", "Book", "책")
    assert should_annotate("item.minecraft.knowledge_book", "Knowledge Book", "지식의 책")
    assert should_annotate("item.mymod.tome_of_fire", "Tome of Fire", "화염의 고서")
    # ...and a container when something does.
    assert not should_annotate("item.mymod.book.page2", "Page Two", "2쪽")


def test_substring_keyword_in_a_name_is_not_treated_as_prose() -> None:
    """Segment matching, not a substring sweep of the whole key.

    A substring test for "book"/"lore"/"info"/"text" would drop these real
    display names.
    """
    for key, source, target in [
        ("block.minecraft.bookshelf", "Bookshelf", "책장"),
        ("block.minecraft.reinforced_deepslate", "Reinforced Deepslate", "보강된 심층암"),
        ("entity.minecraft.text_display", "Text Display", "문자 표시"),
        ("item.minecraft.explorer_pottery_sherd", "Explorer Pottery Sherd", "탐험가 도자기"),
    ]:
        assert should_annotate(key, source, target), key


# -- value guards ------------------------------------------------------------


def test_translation_equal_to_source_is_not_annotated() -> None:
    """Never "Iron Pickaxe (Iron Pickaxe)"."""
    assert not should_annotate("item.minecraft.iron_pickaxe", EN, EN)
    assert not should_annotate("item.minecraft.tnt", "TNT", "TNT")
    # Case and formatting noise must not defeat the check.
    assert not should_annotate("item.minecraft.iron_pickaxe", EN, "iron pickaxe")
    assert not should_annotate("item.minecraft.iron_pickaxe", EN, "  Iron   Pickaxe ")


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("Widget (Tier 2)", "위젯"),
        ("Widget [Rare]", "위젯"),
        ("Widget", "위젯 (Tier 2)"),
        # Idempotency: an already-annotated value is left alone.
        ("Widget", "위젯 (Widget)"),
    ],
)
def test_existing_brackets_block_annotation(source: str, target: str) -> None:
    assert not should_annotate("item.mymod.widget", source, target)


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("%s Ingot", "%s 주괴"),
        ("%1$s of %2$s", "%2$s의 %1$s"),
        ("§aEmerald", "§a에메랄드"),
        ("&aEmerald", "&a에메랄드"),
        ("{count} Ingot", "{count} 주괴"),
        ("<b>Ingot</b>", "<b>주괴</b>"),
        ("Line\\nBreak", "줄\\n바꿈"),
        # A protection token that was never restored must never be annotated.
        ("{{ARG1}} Ingot", "{{ARG1}} 주괴"),
    ],
)
def test_placeholders_and_format_codes_block_annotation(
    source: str, target: str
) -> None:
    """Duplicating a format specifier would break String.format arity."""
    assert not should_annotate("item.mymod.thing", source, target)


def test_length_thresholds() -> None:
    key = "item.mymod.thing"
    at_cap = "A" * MAX_SOURCE_CHARS
    assert should_annotate(key, at_cap, "가")
    assert not should_annotate(key, "A" * (MAX_SOURCE_CHARS + 1), "가")

    # The rendered result has its own ceiling.
    source = "A" * 20
    target = "가" * (MAX_ANNOTATED_CHARS - 20 - 3)
    assert should_annotate(key, source, target)
    assert not should_annotate(key, source, target + "가")


def test_empty_and_missing_values_are_untouched() -> None:
    assert not should_annotate("item.mymod.thing", "", KO)
    assert not should_annotate("item.mymod.thing", EN, "")
    assert not should_annotate("item.mymod.thing", EN, "   ")


# -- rendering ---------------------------------------------------------------


def test_annotate_value_renders_and_passes_through() -> None:
    assert annotate_value("item.minecraft.iron_pickaxe", EN, KO) == f"{KO} ({EN})"
    assert annotate_value("item.mymod.wand.tooltip", EN, KO) == KO


def test_annotate_value_is_idempotent() -> None:
    once = annotate_value("item.minecraft.iron_pickaxe", EN, KO)
    assert annotate_value("item.minecraft.iron_pickaxe", EN, once) == once


def test_annotate_entries_skips_keys_with_no_known_source() -> None:
    out = annotate_entries(
        {"item.minecraft.iron_pickaxe": KO, "item.minecraft.orphan": "고아"},
        {"item.minecraft.iron_pickaxe": EN},
    )
    assert out == {
        "item.minecraft.iron_pickaxe": f"{KO} ({EN})",
        "item.minecraft.orphan": "고아",
    }


# -- generator: one run, two trees -------------------------------------------


def _lang_modpack(tmp_path: Path) -> tuple[Path, Path]:
    """A modpack whose mod ships one lang file with a name and a tooltip."""
    modpack = tmp_path / "modpack"
    source = (
        modpack / ".mct_cache" / "extracted" / "m.jar" / "assets" / "m" / "lang"
        / "en_us.json"
    )
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        json.dumps(
            {
                "item.m.iron_pickaxe": EN,
                "item.m.iron_pickaxe.tooltip": "Mines stone quickly",
            }
        ),
        encoding="utf-8",
    )
    return modpack, source


@pytest.mark.asyncio
async def test_generate_emits_both_variants(tmp_path: Path) -> None:
    modpack, source = _lang_modpack(tmp_path)
    translations = {
        "item.m.iron_pickaxe": KO,
        "item.m.iron_pickaxe.tooltip": "돌을 빠르게 캡니다",
    }
    generator = OutputGenerator(
        OutputConfig(
            modpack_root=modpack,
            output_dir=tmp_path / "out",
            bilingual_names=True,
        )
    )

    result = await generator.generate(
        [FileOutput(source_path=source, fresh=dict(translations), full=dict(translations))]
    )

    assert result.errors == []
    assert result.bilingual is not None

    plain = json.loads(
        (result.resourcepack_dir / "assets/m/lang/ko_kr.json").read_text("utf-8")
    )
    bilingual = json.loads(
        (
            result.bilingual.resourcepack_dir / "assets/m/lang/ko_kr.json"
        ).read_text("utf-8")
    )

    # Same keys, same layout — only the display name gained the original.
    assert plain.keys() == bilingual.keys()
    assert plain["item.m.iron_pickaxe"] == KO
    assert bilingual["item.m.iron_pickaxe"] == f"{KO} ({EN})"
    # The tooltip is prose and must be byte-identical in both variants.
    assert bilingual["item.m.iron_pickaxe.tooltip"] == plain["item.m.iron_pickaxe.tooltip"]


@pytest.mark.asyncio
async def test_bilingual_pack_is_distinguishable_in_game(tmp_path: Path) -> None:
    modpack, source = _lang_modpack(tmp_path)
    generator = OutputGenerator(
        OutputConfig(
            modpack_root=modpack,
            output_dir=tmp_path / "out",
            bilingual_names=True,
        )
    )
    result = await generator.generate(
        [FileOutput(source_path=source, fresh={"item.m.iron_pickaxe": KO}, full={})]
    )

    assert result.pack_mcmeta is not None
    assert result.bilingual is not None
    assert result.bilingual.pack_mcmeta is not None
    plain_desc = json.loads(result.pack_mcmeta.read_text("utf-8"))["pack"]["description"]
    bilingual_desc = json.loads(
        result.bilingual.pack_mcmeta.read_text("utf-8")
    )["pack"]["description"]
    assert plain_desc != bilingual_desc
    assert bilingual_desc.startswith(plain_desc)


@pytest.mark.asyncio
async def test_default_export_has_plain_and_bilingual_values(tmp_path: Path) -> None:
    modpack, source = _lang_modpack(tmp_path)
    generator = OutputGenerator(
        OutputConfig(modpack_root=modpack, output_dir=tmp_path / "out")
    )
    result = await generator.generate(
        [FileOutput(source_path=source, fresh={"item.m.iron_pickaxe": KO}, full={})]
    )

    assert result.bilingual is not None
    bilingual = json.loads(
        (result.bilingual.resourcepack_dir / "assets/m/lang/ko_kr.json").read_text("utf-8")
    )
    assert bilingual["item.m.iron_pickaxe"] == f"{KO} ({EN})"
    plain = json.loads(
        (result.resourcepack_dir / "assets/m/lang/ko_kr.json").read_text("utf-8")
    )
    assert plain["item.m.iron_pickaxe"] == KO


@pytest.mark.asyncio
async def test_unreadable_source_degrades_to_plain_values(tmp_path: Path) -> None:
    """A missing source file costs the annotation, never the generation."""
    modpack = tmp_path / "modpack"
    missing = (
        modpack / ".mct_cache" / "extracted" / "m.jar" / "assets" / "m" / "lang"
        / "en_us.json"
    )
    generator = OutputGenerator(
        OutputConfig(
            modpack_root=modpack,
            output_dir=tmp_path / "out",
            bilingual_names=True,
        )
    )
    result = await generator.generate(
        [FileOutput(source_path=missing, fresh={"item.m.iron_pickaxe": KO}, full={})]
    )

    assert result.errors == []
    assert result.bilingual is not None
    bilingual = json.loads(
        (
            result.bilingual.resourcepack_dir / "assets/m/lang/ko_kr.json"
        ).read_text("utf-8")
    )
    assert bilingual["item.m.iron_pickaxe"] == KO
