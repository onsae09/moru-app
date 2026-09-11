"""Source-text export (W3): a translated run's structure, untranslated values.

``PipelineConfig.source_text_only`` drives the same pipeline and the same
output generator as a real run, settling every entry that would have been
translated to its own source text. The archives it produces must therefore be
structurally identical to a W6 export of the same pack — that identity is the
whole point of the feature (kunho-park/moru-app#3: reviewers diff moru's
output against packs shared on moru.gg).

Nothing here needs an LM: a source-text pipeline builds neither an LM nor a
translator. The translated side uses test_pipeline_e2e's deterministic fake.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import dspy
import pytest

from moru_engine.output import (
    OVERRIDES_DIRNAME,
    RESOURCEPACK_DIRNAME,
    create_zip_from_directory,
)
from moru_engine.pipeline import (
    EntryStatus,
    PipelineConfig,
    PipelineResult,
    TranslationPipeline,
    output_root,
)
from test_pipeline_e2e import FakeTranslator

FIXTURE = Path(__file__).resolve().parents[1] / "test" / "modpack"


@pytest.fixture
def modpack(tmp_path: Path) -> Path:
    target = tmp_path / "modpack"
    shutil.copytree(FIXTURE, target, ignore=shutil.ignore_patterns(".mct_cache"))
    return target


def _tree(root: Path) -> list[str]:
    """Every generated file, relative to the run's output root."""
    return sorted(
        p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()
    )


def _source_config(modpack: Path, output_dir: Path, store: Path) -> PipelineConfig:
    return PipelineConfig(
        modpack_path=modpack,
        output_dir=output_dir,
        source_text_only=True,
        glossary_store_dir=store,
    )


def _translated_config(
    modpack: Path, output_dir: Path, store: Path, tm_db: Path
) -> PipelineConfig:
    return PipelineConfig(
        modpack_path=modpack,
        output_dir=output_dir,
        tm_db_path=tm_db,
        glossary_store_dir=store,
        use_vanilla_glossary=False,
    )


async def _run_source_text(config: PipelineConfig) -> PipelineResult:
    """A source-text run, asserting no provider is constructed for it."""
    pipeline = TranslationPipeline(config)
    assert pipeline.lm is None
    assert pipeline.translator is None
    try:
        return await pipeline.run()
    finally:
        pipeline.close()


async def _run_translated(config: PipelineConfig) -> PipelineResult:
    pipeline = TranslationPipeline(config, lm=dspy.utils.DummyLM([]))
    pipeline.translator = FakeTranslator()
    try:
        return await pipeline.run()
    finally:
        pipeline.close()


def test_source_text_config_disables_every_provider_stage(tmp_path: Path) -> None:
    """No API key, no TM, no LLM glossary curation - by construction."""
    config = PipelineConfig(
        modpack_path=tmp_path,
        source_text_only=True,
        # Explicitly ask for everything a normal run does; the mode wins.
        use_tm=True,
        use_translation_graph=True,
        use_vanilla_glossary=True,
        use_user_glossary=True,
        use_mod_translations=True,
        extract_glossary=True,
    )
    assert config.api_key is None
    assert config.use_tm is False
    assert config.use_translation_graph is False
    assert config.use_vanilla_glossary is False
    assert config.use_user_glossary is False
    assert config.use_mod_translations is False
    assert config.extract_glossary is False
    # A translated config keeps its knobs.
    normal = PipelineConfig(modpack_path=tmp_path, use_tm=True, extract_glossary=True)
    assert (normal.use_tm, normal.extract_glossary) == (True, True)


def test_source_text_output_root_is_a_sub_root(tmp_path: Path) -> None:
    """Both trees are regenerated from scratch, so the two modes must not
    share one root or either would wipe the other's artifacts."""
    translated = PipelineConfig(modpack_path=tmp_path / "pack")
    source = PipelineConfig(modpack_path=tmp_path / "pack", source_text_only=True)
    assert output_root(translated) == tmp_path / "pack" / "moru_output"
    assert output_root(source) == tmp_path / "pack" / "moru_output" / "source_text"


@pytest.mark.asyncio
async def test_source_text_tree_matches_a_translated_run(
    modpack: Path, tmp_path: Path
) -> None:
    source = await _run_source_text(
        _source_config(modpack, tmp_path / "out-src", tmp_path / "glossaries")
    )
    translated = await _run_translated(
        _translated_config(
            modpack,
            tmp_path / "out-tr",
            tmp_path / "glossaries",
            tmp_path / "tm.sqlite3",
        )
    )

    source_root = output_root(source.config)
    translated_root = output_root(translated.config)
    # A translated run always carries the bilingual sibling tree; a source
    # text export has nothing to annotate, so the identity holds for the
    # plain trees only.
    plain_translated_tree = [
        name for name in _tree(translated_root) if not name.startswith("bilingual/")
    ]
    assert _tree(source_root) == plain_translated_tree
    assert any(name.startswith("bilingual/") for name in _tree(translated_root))
    # Sanity: the comparison is not vacuous.
    assert "resourcepack/pack.mcmeta" in _tree(source_root)
    assert any(name.startswith("overrides/") for name in _tree(source_root))

    # Same entry set, same per-file key sets: the source export is a
    # key-for-key skeleton of the translated one.
    assert {(e.file, e.key) for e in source.entries} == {
        (e.file, e.key) for e in translated.entries
    }
    for relative in _tree(source_root):
        if not relative.endswith(".json"):
            continue
        left = json.loads((source_root / relative).read_text(encoding="utf-8"))
        right = json.loads((translated_root / relative).read_text(encoding="utf-8"))
        if relative.endswith("pack.mcmeta"):
            continue
        if isinstance(left, dict) and isinstance(right, dict):
            assert left.keys() == right.keys(), relative


@pytest.mark.asyncio
async def test_archives_are_structurally_identical(
    modpack: Path, tmp_path: Path
) -> None:
    """The zips a W3 and a W6 export build hold the same member paths.

    ``source_text`` is an on-disk working root only - it must never appear
    inside an archive, and members stay relative to resourcepack/ and
    overrides/ exactly as the game expects them.
    """
    source = await _run_source_text(
        _source_config(modpack, tmp_path / "out-src", tmp_path / "glossaries")
    )
    translated = await _run_translated(
        _translated_config(
            modpack,
            tmp_path / "out-tr",
            tmp_path / "glossaries",
            tmp_path / "tm.sqlite3",
        )
    )

    import zipfile

    def names(result: PipelineResult, tree: str, label: str) -> list[str]:
        zip_path = tmp_path / f"{label}-{tree}.zip"
        create_zip_from_directory(output_root(result.config) / tree, zip_path)
        with zipfile.ZipFile(zip_path) as archive:
            return sorted(archive.namelist())

    for tree in (RESOURCEPACK_DIRNAME, OVERRIDES_DIRNAME):
        source_names = names(source, tree, "source")
        assert source_names == names(translated, tree, "translated")
        assert source_names
        assert not any("source_text" in name for name in source_names)
        assert not any(name.startswith(f"{tree}/") for name in source_names)
    # The resource pack is installable as-is: mcmeta at the archive root.
    assert "pack.mcmeta" in names(source, RESOURCEPACK_DIRNAME, "source")


@pytest.mark.asyncio
async def test_generated_values_are_the_untranslated_source(
    modpack: Path, tmp_path: Path
) -> None:
    result = await _run_source_text(
        _source_config(modpack, tmp_path / "out", tmp_path / "glossaries")
    )
    root = output_root(result.config)

    # No provider was reachable, so nothing was billed.
    assert result.stats.prompt_tokens == 0
    assert result.stats.completion_tokens == 0
    assert result.stats.failed_entries == 0

    # Every entry this run settled carries its own source text.
    fresh = [e for e in result.entries if e.status is EntryStatus.PASSED]
    assert fresh
    assert all(e.translated_text == e.source_text for e in fresh)

    # A jar-extracted mod lang file lands at the TARGET locale filename with
    # the source strings inside: that is what lines up key-for-key against a
    # translated pack shared on moru.gg.
    pack_lang = root / "resourcepack/assets/testmod/lang/ko_kr.json"
    assert pack_lang.exists()
    assert not (root / "resourcepack/assets/testmod/lang/en_us.json").exists()
    generated = json.loads(pack_lang.read_text(encoding="utf-8"))
    assert generated["item.testmod.copper_wand"] == "Copper Wand"
    # Placeholders and color codes survive untouched.
    assert generated["item.testmod.copper_wand.tooltip"].count("%s") == 1
    assert "§7" in generated["item.testmod.copper_wand.tooltip"]

    # An override replaces the whole file, so it keeps the translations the
    # pack already ships and shows source text only where moru would have
    # translated.
    override = json.loads(
        (root / "overrides/kubejs/assets/test/lang/ko_kr.json").read_text(
            encoding="utf-8"
        )
    )
    existing = json.loads(
        (modpack / "kubejs/assets/test/lang/ko_kr.json").read_text(encoding="utf-8")
    )
    source_data = json.loads(
        (modpack / "kubejs/assets/test/lang/en_us.json").read_text(encoding="utf-8")
    )
    assert override["item.minecraft.diamond"] == existing["item.minecraft.diamond"]
    untranslated = [
        key for key in source_data if not str(existing.get(key, "")).strip()
    ]
    assert untranslated
    for key in untranslated:
        assert override[key] == source_data[key]

    # The pack description marks the artifact so it cannot be mistaken for a
    # translated pack in the resource-pack list.
    mcmeta = json.loads(
        (root / "resourcepack/pack.mcmeta").read_text(encoding="utf-8")
    )
    assert "원문" in mcmeta["pack"]["description"]
    assert mcmeta["pack"]["pack_format"] > 0


class ExplodingTranslator:
    """Any provider call at all is a bug on the source-text path."""

    async def acall(self, **kwargs: object) -> None:
        raise AssertionError(f"translator called: {sorted(kwargs)}")


@pytest.mark.asyncio
async def test_source_text_run_never_calls_a_translator(
    modpack: Path, tmp_path: Path
) -> None:
    config = _source_config(modpack, tmp_path / "out", tmp_path / "glossaries")
    pipeline = TranslationPipeline(config)
    pipeline.translator = ExplodingTranslator()
    try:
        result = await pipeline.run()
    finally:
        pipeline.close()
    assert result.output_files
    assert result.stats.total_entries > 0


@pytest.mark.asyncio
async def test_modes_do_not_clobber_each_other(modpack: Path, tmp_path: Path) -> None:
    """A W3 export before/after a real run leaves the other's zip inputs
    alone, even though both share one configured output directory."""
    out = tmp_path / "out"
    source = await _run_source_text(
        _source_config(modpack, out, tmp_path / "glossaries")
    )
    source_tree = _tree(output_root(source.config))

    translated = await _run_translated(
        _translated_config(
            modpack, out, tmp_path / "glossaries", tmp_path / "tm.sqlite3"
        )
    )
    # The translated run regenerated its own trees without touching ours.
    assert _tree(output_root(source.config)) == source_tree
    translated_root = output_root(translated.config)
    assert (translated_root / RESOURCEPACK_DIRNAME / "pack.mcmeta").exists()

    # And the reverse: re-running the source export keeps the translated trees.
    translated_pack_lang = (
        translated_root / RESOURCEPACK_DIRNAME / "assets/testmod/lang/ko_kr.json"
    )
    before = translated_pack_lang.read_text(encoding="utf-8")
    await _run_source_text(_source_config(modpack, out, tmp_path / "glossaries"))
    assert translated_pack_lang.read_text(encoding="utf-8") == before
    assert "KO " in before
