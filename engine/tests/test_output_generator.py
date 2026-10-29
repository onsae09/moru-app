"""Output generator: routing, fresh-only packs, full overrides, skips."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from moru_engine.output import (
    MINOR_VERSION_ERA_FORMAT,
    RESOURCE_PACK_FORMATS,
    FileOutput,
    OutputConfig,
    OutputGenerator,
    Route,
    pack_format_for_minecraft_version,
    route_for,
)


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _generator(tmp_path: Path) -> OutputGenerator:
    return OutputGenerator(
        OutputConfig(
            modpack_root=tmp_path / "modpack",
            output_dir=tmp_path / "out",
        )
    )


# -- routing -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("mp/kubejs/assets/test/lang/en_us.json", Route.OVERRIDE),
        ("mp/config/ftbquests/quests/chapters/intro.snbt", Route.OVERRIDE),
        ("mp/patchouli_books/book/en_us/entries/a.json", Route.OVERRIDE),
        (
            "mp/.mct_cache/extracted/m.jar/assets/m/lang/en_us.json",
            Route.RESOURCE_PACK,
        ),
        ("mp/resourcepacks/pack/assets/m/lang/en_us.json", Route.RESOURCE_PACK),
        # jar-internal data/ would need .jar patching -> skipped
        (
            "mp/.mct_cache/extracted/m.jar/data/m/patchouli_books/b/en_us/x.json",
            Route.SKIP_JAR_DATA,
        ),
        # jar-internal config-like file: nothing on disk to overwrite
        (
            "mp/.mct_cache/extracted/m.jar/config/something/en_us.json",
            Route.SKIP_EXTRACTED,
        ),
        # a mod's builtin patchouli book rides along in the resource pack
        (
            "mp/.mct_cache/extracted/m.jar/assets/m/patchouli_books/b/en_us/x.json",
            Route.RESOURCE_PACK,
        ),
    ],
)
def test_route_for(path: str, expected: Route) -> None:
    assert route_for(Path(path)) == expected


@pytest.mark.parametrize(
    ("mc_version", "expected"),
    [
        # Every release boundary in the resource-pack table.
        ("1.8.9", 1),
        ("1.9", 2),
        ("1.10.2", 2),
        ("1.11", 3),
        ("1.12.2", 3),
        ("1.13", 4),
        ("1.14.4", 4),
        ("1.15", 5),
        # 1.16.1 is still format 5; 6 starts at 1.16.2.
        ("1.16.1", 5),
        ("1.16.2", 6),
        ("1.16.5", 6),
        ("1.17", 7),
        ("1.17.1", 7),
        ("1.18", 8),
        ("1.18.2", 8),
        ("1.19", 9),
        ("1.19.2", 9),
        ("1.19.3", 12),
        ("1.19.4", 13),
        ("1.20", 15),
        ("1.20.1", 15),
        ("1.20.2", 18),
        ("1.20.3", 22),
        ("1.20.4", 22),
        ("1.20.5", 32),
        ("1.20.6", 32),
        ("1.21", 34),
        ("1.21.1", 34),
        ("1.21.2", 42),
        ("1.21.3", 42),
        ("1.21.4", 46),
        ("1.21.5", 55),
        ("1.21.6", 63),
        ("1.21.7", 64),
        ("1.21.8", 64),
        ("1.21.9", 69),
        ("1.21.10", 69),
        ("1.21.11", 75),
        # Numbering changed to YY.N after 1.21.11; naive string/semver
        # comparison would sort these below 1.21.x.
        ("26.1", 84),
        ("26.1.2", 84),
        ("26.2", 88),
        # Embedded in prose (legacy launcher metadata shape).
        ("Minecraft 1.11.2 Forge", 3),
        ("v1.20.1", 15),
        # Newer than the table: clamp DOWN to the newest known format —
        # too-low warns but loads, too-high can be rejected.
        ("26.3", 88),
        ("27.1", 88),
        # Predates pack_format, and unknown/unparseable input: fallback.
        ("1.5.2", 15),
        ("not-a-version", 15),
        ("", 15),
        (None, 15),
    ],
)
def test_pack_format_for_minecraft_version(
    mc_version: str | None,
    expected: int,
) -> None:
    assert pack_format_for_minecraft_version(mc_version) == expected


def test_pack_format_fallback_is_caller_supplied() -> None:
    assert pack_format_for_minecraft_version(None, fallback=22) == 22
    assert pack_format_for_minecraft_version("garbage", fallback=22) == 22
    # A resolvable version ignores the fallback entirely.
    assert pack_format_for_minecraft_version("1.12.2", fallback=22) == 3


def test_pack_format_table_is_ordered_newest_first() -> None:
    """The lookup takes the first entry <= target, so order is load-bearing."""
    releases = [release for release, _ in RESOURCE_PACK_FORMATS]
    assert releases == sorted(releases, reverse=True)


@pytest.mark.asyncio
async def test_mcmeta_uses_pack_format_before_the_minor_version_era(
    tmp_path: Path,
) -> None:
    generator = OutputGenerator(
        OutputConfig(
            modpack_root=tmp_path / "modpack",
            output_dir=tmp_path / "out",
            pack_format=MINOR_VERSION_ERA_FORMAT - 1,
        )
    )
    path = await generator._write_pack_mcmeta()
    pack = json.loads(path.read_text(encoding="utf-8"))["pack"]
    assert pack["pack_format"] == MINOR_VERSION_ERA_FORMAT - 1
    assert "min_format" not in pack
    assert "max_format" not in pack
    assert "supported_formats" not in pack


@pytest.mark.asyncio
async def test_mcmeta_uses_min_max_format_from_1_21_9(tmp_path: Path) -> None:
    """1.21.9+ requires min_format/max_format and disallows supported_formats."""
    generator = OutputGenerator(
        OutputConfig(
            modpack_root=tmp_path / "modpack",
            output_dir=tmp_path / "out",
            pack_format=pack_format_for_minecraft_version("1.21.9"),
        )
    )
    path = await generator._write_pack_mcmeta()
    pack = json.loads(path.read_text(encoding="utf-8"))["pack"]
    assert pack["min_format"] == 69
    assert pack["max_format"] == 69
    assert "pack_format" not in pack
    assert "supported_formats" not in pack


# -- resource pack -------------------------------------------------------------


@pytest.mark.asyncio
async def test_resourcepack_lang_is_fresh_only_with_mcmeta(
    tmp_path: Path,
) -> None:
    source = _write(
        tmp_path / "modpack/.mct_cache/extracted/m.jar/assets/m/lang/en_us.json",
        json.dumps({"a": "Alpha", "b": "Beta", "c": "Gamma"}),
    )
    gen = _generator(tmp_path)
    result = await gen.generate(
        [
            FileOutput(
                source_path=source,
                # "b" already had a translation in the modpack -> not fresh
                fresh={"a": "알파", "c": "감마"},
                full={"a": "알파", "b": "베타", "c": "감마"},
                namespace="m",
            )
        ]
    )

    out = tmp_path / "out/resourcepack/assets/m/lang/ko_kr.json"
    assert result.resourcepack_files == [out]
    data = json.loads(out.read_text(encoding="utf-8"))
    # pre-existing pairs are omitted: the game merges lang keys across
    # packs, and re-shipping them would shadow the modpack's own strings
    assert data == {"a": "알파", "c": "감마"}

    assert result.pack_mcmeta is not None
    mcmeta = json.loads(result.pack_mcmeta.read_text(encoding="utf-8"))
    assert mcmeta["pack"]["pack_format"] > 0
    assert "moru.gg" in mcmeta["pack"]["description"]

    # branded icon rides along and is part of the zipped file set
    assert result.pack_icon is not None
    assert result.pack_icon.name == "pack.png"
    assert result.pack_icon.stat().st_size > 0
    assert result.pack_icon in result.all_files


@pytest.mark.asyncio
async def test_same_namespace_sources_merge_into_one_lang_file(
    tmp_path: Path,
) -> None:
    jar = _write(
        tmp_path / "modpack/.mct_cache/extracted/m.jar/assets/m/lang/en_us.json",
        json.dumps({"a": "Alpha"}),
    )
    overlay = _write(
        tmp_path / "modpack/resourcepacks/pack/assets/m/lang/en_us.json",
        json.dumps({"b": "Beta"}),
    )
    gen = _generator(tmp_path)
    result = await gen.generate(
        [
            FileOutput(jar, fresh={"a": "알파"}, full={"a": "알파"}, namespace="m"),
            FileOutput(overlay, fresh={"b": "베타"}, full={"b": "베타"}, namespace="m"),
        ]
    )
    assert len(result.resourcepack_files) == 1
    data = json.loads(result.resourcepack_files[0].read_text(encoding="utf-8"))
    assert data == {"a": "알파", "b": "베타"}


@pytest.mark.asyncio
async def test_format_three_lang_file_is_lowercase_and_loadable(
    tmp_path: Path,
) -> None:
    source = _write(
        tmp_path / "modpack/.mct_cache/extracted/m.jar/assets/m/lang/en_US.lang",
        "a=Alpha\n",
    )
    gen = OutputGenerator(
        OutputConfig(
            modpack_root=tmp_path / "modpack",
            output_dir=tmp_path / "out",
            pack_format=3,
        )
    )
    result = await gen.generate(
        [FileOutput(source, fresh={"a": "알파"}, full={"a": "알파"}, namespace="m")]
    )
    out = tmp_path / "out/resourcepack/assets/m/lang/ko_kr.lang"
    assert result.resourcepack_files == [out]
    assert "a=알파" in out.read_text(encoding="utf-8")
    assert result.pack_mcmeta is not None
    mcmeta = json.loads(result.pack_mcmeta.read_text(encoding="utf-8"))
    assert mcmeta["pack"]["pack_format"] == 3


# -- skips ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fully_pretranslated_file_is_skipped(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "modpack/.mct_cache/extracted/m.jar/assets/m/lang/en_us.json",
        json.dumps({"a": "Alpha"}),
    )
    gen = _generator(tmp_path)
    result = await gen.generate(
        [FileOutput(source, fresh={}, full={"a": "알파"}, namespace="m")]
    )
    assert result.skipped_existing == 1
    assert result.resourcepack_files == []
    # nothing shipped -> no pack.mcmeta either
    assert result.pack_mcmeta is None


@pytest.mark.asyncio
async def test_jar_data_files_are_skipped(tmp_path: Path) -> None:
    source = _write(
        tmp_path
        / "modpack/.mct_cache/extracted/m.jar/data/m/quests/chapter.json",
        json.dumps({"q": "Quest"}),
    )
    gen = _generator(tmp_path)
    result = await gen.generate(
        [FileOutput(source, fresh={"q": "퀘스트"}, full={"q": "퀘스트"})]
    )
    assert result.skipped_jar_data == 1
    assert result.all_files == []


# -- overrides -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_override_carries_full_merged_state(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "modpack/kubejs/assets/test/lang/en_us.json",
        json.dumps({"a": "Alpha", "b": "Beta"}),
    )
    gen = _generator(tmp_path)
    result = await gen.generate(
        [
            FileOutput(
                source_path=source,
                fresh={"a": "알파"},
                # "b" pre-existed in the modpack's ko_kr; the override file
                # replaces the whole file so it must keep it
                full={"a": "알파", "b": "베타"},
            )
        ]
    )
    out = tmp_path / "out/overrides/kubejs/assets/test/lang/ko_kr.json"
    assert result.override_files == [out]
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data == {"a": "알파", "b": "베타"}
    # overrides alone never produce a pack.mcmeta
    assert result.pack_mcmeta is None


@pytest.mark.asyncio
async def test_stale_output_trees_are_wiped(tmp_path: Path) -> None:
    stale = _write(tmp_path / "out/resourcepack/assets/old/lang/ko_kr.json", "{}")
    _write(tmp_path / "out/overrides/config/old.snbt", "{}")
    gen = _generator(tmp_path)
    result = await gen.generate([])
    assert not stale.exists()
    assert not (tmp_path / "out/overrides/config/old.snbt").exists()
    assert result.all_files == []


def test_output_dir_cannot_be_the_pack_or_hold_it(tmp_path: Path) -> None:
    import pydantic

    from moru_engine.pipeline.orchestrator import PipelineConfig

    pack = tmp_path / "pack"
    pack.mkdir()
    with pytest.raises(pydantic.ValidationError):
        PipelineConfig(modpack_path=pack, output_dir=pack)
    nested = tmp_path / "overrides" / "pack"
    nested.mkdir(parents=True)
    with pytest.raises(pydantic.ValidationError):
        PipelineConfig(modpack_path=nested, output_dir=tmp_path)
    # The default location inside the pack stays allowed.
    PipelineConfig(modpack_path=pack, output_dir=pack / "moru_output")
