"""Previous-version A/B/C translation migration."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import dspy
import pytest
from moru_engine.migration import (
    MigrationError,
    build_migration_catalog,
    logical_file_id,
    safe_extract_zip,
)
from moru_engine.pipeline import EntryStatus, PipelineConfig, TranslationPipeline
from moru_engine.scanner import scan_modpack


def _write_json(path: Path, payload: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


class RecordingTranslator:
    def __init__(self) -> None:
        self.sources: list[str] = []

    async def acall(self, *, entries, **_kwargs):
        self.sources.extend(entries.values())
        return dspy.Prediction(
            translations={key: f"AI {value}" for key, value in entries.items()},
            failed={},
        )


def _seed_three_way(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    old = tmp_path / "old"
    current = tmp_path / "current"
    resourcepack = tmp_path / "previous-resourcepack"
    overrides = tmp_path / "previous-overrides"

    _write_json(
        old / "resourcepacks/base/assets/demo/lang/en_us.json",
        {"same": "Same resource", "changed": "Old resource"},
    )
    _write_json(
        current / "resourcepacks/base/assets/demo/lang/en_us.json",
        {"same": "Same resource", "changed": "New resource"},
    )
    _write_json(
        resourcepack / "assets/demo/lang/ko_kr.json",
        {"same": "수동 리소스", "changed": "예전 리소스"},
    )
    font = resourcepack / "assets/demo/font/readable.bin"
    font.parent.mkdir(parents=True, exist_ok=True)
    font.write_bytes(b"font-bytes-\x00-unchanged")
    _write_json(
        resourcepack / "assets/demo/font/default.json",
        {"providers": [{"type": "ttf", "file": "demo:font/readable.ttf"}]},
    )
    glyph = resourcepack / "assets/demo/textures/font/glyph.png"
    glyph.parent.mkdir(parents=True, exist_ok=True)
    glyph.write_bytes(b"font-glyph")
    unrelated_texture = resourcepack / "assets/demo/textures/gui/legacy.png"
    unrelated_texture.parent.mkdir(parents=True, exist_ok=True)
    unrelated_texture.write_bytes(b"old-ui-texture")
    unrelated_sound = resourcepack / "assets/demo/sounds/legacy.ogg"
    unrelated_sound.parent.mkdir(parents=True, exist_ok=True)
    unrelated_sound.write_bytes(b"old-sound")
    _write_json(resourcepack / "pack.mcmeta", {"pack": {"pack_format": 1}})

    old_skill = {
        "title": "Same override",
        "description": "Old override",
        "gameplay_setting": "old-value",
    }
    current_skill = {
        "title": "Same override",
        "description": "New override",
        "gameplay_setting": "new-value",
    }
    previous_skill = {
        "title": "수동 덮어쓰기",
        "description": "예전 덮어쓰기",
        "gameplay_setting": "must-not-leak",
    }
    relative = Path("config/puffish_skills/categories/demo/definitions.json")
    _write_json(old / relative, old_skill)
    _write_json(current / relative, current_skill)
    _write_json(overrides / relative, previous_skill)
    _write_json(overrides / "config/legacy-only.json", {"enabled": False})
    return old, current, resourcepack, overrides


def test_logical_file_id_keeps_kubejs_assets_in_overrides(tmp_path: Path) -> None:
    root = tmp_path / "pack"
    kube = root / "kubejs/assets/demo/lang/en_us.json"
    jar = root / ".mct_cache/extracted/mod.jar/assets/demo/lang/en_us.json"
    assert logical_file_id(kube, root) == "override:kubejs/assets/demo/lang/{locale}.json"
    assert logical_file_id(jar, root) == "resource:demo/lang/{locale}.json"


def test_logical_file_id_only_folds_real_locale_codes(tmp_path: Path) -> None:
    root = tmp_path / "pack"
    lang = root / "config/quests/lang/en_us.json"
    chore = root / "config/quests/lang/to_do.json"
    chapter = root / "config/quests/chapters/go_to_nether.snbt"
    suffixed = root / "config/quests/lang/defaults_ko_kr.json"
    assert logical_file_id(lang, root) == "override:config/quests/lang/{locale}.json"
    assert logical_file_id(chore, root) == "override:config/quests/lang/to_do.json"
    assert (
        logical_file_id(chapter, root)
        == "override:config/quests/chapters/go_to_nether.snbt"
    )
    assert (
        logical_file_id(suffixed, root)
        == "override:config/quests/lang/defaults_{locale}.json"
    )


def test_safe_extract_zip_rejects_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../escaped.txt", "nope")
    with pytest.raises(MigrationError, match="unsafe entry"):
        safe_extract_zip(archive, tmp_path / "extract")
    assert not (tmp_path / "escaped.txt").exists()


def test_safe_extract_zip_rejects_symlink(tmp_path: Path) -> None:
    archive = tmp_path / "symlink.zip"
    info = zipfile.ZipInfo("linked-font.ttf")
    info.create_system = 3
    info.external_attr = 0o120777 << 16
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(info, "outside.ttf")
    with pytest.raises(MigrationError, match="unsafe entry"):
        safe_extract_zip(archive, tmp_path / "extract")


@pytest.mark.asyncio
async def test_catalog_matches_only_exact_source_and_preserves_assets(
    tmp_path: Path,
) -> None:
    old, current, resourcepack, overrides = _seed_three_way(tmp_path)
    scan = await scan_modpack(current)
    catalog = await build_migration_catalog(
        previous_modpack_path=old,
        previous_resourcepack_path=resourcepack,
        previous_overrides_path=overrides,
        current_modpack_root=current,
        current_scan=scan,
        source_locale="en_us",
        target_locale="ko_kr",
        asset_cache_dir=tmp_path / "asset-cache",
    )

    assert catalog.match("resource:demo/lang/{locale}.json", "same", "Same resource") == "수동 리소스"
    assert catalog.match("resource:demo/lang/{locale}.json", "changed", "New resource") is None
    assert catalog.match(
        "override:config/puffish_skills/categories/demo/definitions.json",
        "title",
        "Same override",
    ) == "수동 덮어쓰기"
    assert catalog.match(
        "override:config/puffish_skills/categories/demo/definitions.json",
        "description",
        "New override",
    ) is None
    assert (tmp_path / "asset-cache/assets/demo/font/readable.bin").read_bytes() == b"font-bytes-\x00-unchanged"
    assert (tmp_path / "asset-cache/assets/demo/font/default.json").is_file()
    assert (tmp_path / "asset-cache/assets/demo/textures/font/glyph.png").read_bytes() == b"font-glyph"
    assert not (tmp_path / "asset-cache/assets/demo/textures/gui/legacy.png").exists()
    assert not (tmp_path / "asset-cache/assets/demo/sounds/legacy.ogg").exists()
    assert not (tmp_path / "asset-cache/pack.mcmeta").exists()
    assert catalog.stats.preserved_resourcepack_assets == 3


@pytest.mark.asyncio
async def test_catalog_drops_ambiguous_and_non_target_translations(
    tmp_path: Path,
) -> None:
    old = tmp_path / "old"
    current = tmp_path / "current"
    translated = tmp_path / "translated"
    _write_json(
        old / "resourcepacks/base/assets/demo/lang/en_us.json",
        {"same": "Same", "new": "Old-only"},
    )
    _write_json(
        current / "resourcepacks/base/assets/demo/lang/en_us.json",
        {"same": "Same", "new": "New-only"},
    )
    _write_json(
        translated / "assets/demo/lang/ko_kr.json",
        {"same": "번역 하나", "new": "예전 번역"},
    )
    _write_json(
        translated / "two/assets/demo/lang/ko_kr.json",
        {"same": "충돌 번역", "new": "예전 번역"},
    )
    _write_json(
        translated / "assets/demo/lang/ja_jp.json",
        {"same": "日本語"},
    )

    scan = await scan_modpack(current)
    catalog = await build_migration_catalog(
        previous_modpack_path=old,
        previous_resourcepack_path=translated,
        previous_overrides_path=None,
        current_modpack_root=current,
        current_scan=scan,
        source_locale="en_us",
        target_locale="ko_kr",
        asset_cache_dir=tmp_path / "assets",
    )

    logical = "resource:demo/lang/{locale}.json"
    assert catalog.match(logical, "same", "Same") is None
    assert catalog.match(logical, "new", "New-only") is None
    assert (logical, "same") in catalog.ambiguous


@pytest.mark.asyncio
async def test_overrides_patch_archive_reuses_translation_and_preserves_font(
    tmp_path: Path,
) -> None:
    old = tmp_path / "old"
    current = tmp_path / "current"
    overrides = tmp_path / "overrides"
    _write_json(
        old / "resourcepacks/base/assets/demo/lang/en_us.json",
        {"same": "Same"},
    )
    _write_json(
        current / "resourcepacks/base/assets/demo/lang/en_us.json",
        {"same": "Same"},
    )
    patch = overrides / "config/paxi/resourcepacks/legacy.zip"
    patch.parent.mkdir(parents=True)
    with zipfile.ZipFile(patch, "w") as zf:
        zf.writestr(
            "assets/demo/lang/ko_kr.json",
            json.dumps({"same": "중첩 수동 번역"}, ensure_ascii=False),
        )
        zf.writestr("assets/demo/font/readable.ttf", b"embedded-font")
        zf.writestr(
            "assets/demo/font/default.json",
            json.dumps(
                {"providers": [{"type": "ttf", "file": "demo:font/readable.ttf"}]}
            ),
        )
        zf.writestr("assets/demo/textures/gui/legacy.png", b"old-ui-texture")
        zf.writestr("assets/demo/sounds/legacy.ogg", b"old-sound")
        zf.writestr("META-INF/not-a-resource.txt", "do not copy")

    scan = await scan_modpack(current)
    catalog = await build_migration_catalog(
        previous_modpack_path=old,
        previous_resourcepack_path=None,
        previous_overrides_path=overrides,
        current_modpack_root=current,
        current_scan=scan,
        source_locale="en_us",
        target_locale="ko_kr",
        asset_cache_dir=tmp_path / "assets",
    )

    assert catalog.match(
        "resource:demo/lang/{locale}.json", "same", "Same"
    ) == "중첩 수동 번역"
    assert (tmp_path / "assets/assets/demo/font/readable.ttf").read_bytes() == b"embedded-font"
    assert (tmp_path / "assets/assets/demo/font/default.json").is_file()
    assert not (tmp_path / "assets/assets/demo/textures/gui/legacy.png").exists()
    assert not (tmp_path / "assets/assets/demo/sounds/legacy.ogg").exists()
    assert not (tmp_path / "assets/META-INF/not-a-resource.txt").exists()
    assert catalog.stats.preserved_resourcepack_assets == 2


@pytest.mark.asyncio
async def test_overrides_font_only_paxi_pack_is_preserved(tmp_path: Path) -> None:
    old = tmp_path / "old"
    current = tmp_path / "current"
    overrides = tmp_path / "overrides"
    old.mkdir()
    current.mkdir()
    patch = overrides / "config/paxi/resourcepacks/font-only.zip"
    patch.parent.mkdir(parents=True)
    with zipfile.ZipFile(patch, "w") as zf:
        zf.writestr("pack.mcmeta", json.dumps({"pack": {"pack_format": 15}}))
        zf.writestr("assets/minecraft/font/readable.ttf", b"font-only-paxi")
        zf.writestr(
            "assets/minecraft/font/default.json",
            json.dumps(
                {
                    "providers": [
                        {"type": "ttf", "file": "minecraft:font/readable.ttf"}
                    ]
                }
            ),
        )

    scan = await scan_modpack(current)
    catalog = await build_migration_catalog(
        previous_modpack_path=old,
        previous_resourcepack_path=None,
        previous_overrides_path=overrides,
        current_modpack_root=current,
        current_scan=scan,
        source_locale="en_us",
        target_locale="ko_kr",
        asset_cache_dir=tmp_path / "assets",
    )

    assert (
        tmp_path / "assets/assets/minecraft/font/readable.ttf"
    ).read_bytes() == b"font-only-paxi"
    assert (tmp_path / "assets/assets/minecraft/font/default.json").is_file()
    assert catalog.stats.preserved_resourcepack_assets == 2


@pytest.mark.asyncio
async def test_catalog_indexes_target_locale_patchouli_files(
    tmp_path: Path,
) -> None:
    old = tmp_path / "old"
    current = tmp_path / "current"
    translated = tmp_path / "translated"
    relative = Path(
        "resourcepacks/base/assets/demo/patchouli_books/guide/en_us/entries/intro.json"
    )
    for root in (old, current):
        _write_json(
            root / relative,
            {"name": "Introduction", "pages": [{"text": "Same page"}]},
        )
    _write_json(
        translated
        / "assets/demo/patchouli_books/guide/ko_kr/entries/intro.json",
        {"name": "소개", "pages": [{"text": "같은 페이지"}]},
    )

    scan = await scan_modpack(current)
    catalog = await build_migration_catalog(
        previous_modpack_path=old,
        previous_resourcepack_path=translated,
        previous_overrides_path=None,
        current_modpack_root=current,
        current_scan=scan,
        source_locale="en_us",
        target_locale="ko_kr",
        asset_cache_dir=tmp_path / "assets",
    )

    logical = (
        "resource:demo/patchouli_books/guide/{locale}/entries/intro.json"
    )
    assert catalog.match(logical, "name", "Introduction") == "소개"
    assert catalog.match(logical, "pages[0].text", "Same page") == "같은 페이지"


@pytest.mark.asyncio
async def test_catalog_indexes_target_locale_ftbquests_json5_tree(
    tmp_path: Path,
) -> None:
    """FTB Quests 26.1.2+ lang is a per-locale directory of JSON5 files."""
    old = tmp_path / "old"
    current = tmp_path / "current"
    overrides = tmp_path / "overrides"
    lang = Path("config/ftbquests/quests/lang")
    source = (
        "{\n"
        '  "quest.66ECC26BC81D0093.title": "Tier: &bBasic",\n'
        '  "quest.33816AF0E699F19F.quest_desc": [\n'
        '    "This block will drain FE from any charged item.",\n'
        "  ],\n"
        "}"
    )
    for root in (old, current):
        path = root / lang / "en_us/chapters/powah.json5"
        path.parent.mkdir(parents=True)
        path.write_text(source, encoding="utf-8")
    previous = overrides / lang / "ko_kr/chapters/powah.json5"
    previous.parent.mkdir(parents=True)
    previous.write_text(
        "{\n"
        '  "quest.66ECC26BC81D0093.title": "티어: &b기본",\n'
        '  "quest.33816AF0E699F19F.quest_desc": [\n'
        '    "이 블록은 충전된 모든 아이템에서 FE를 배수합니다.",\n'
        "  ],\n"
        "}",
        encoding="utf-8",
    )

    scan = await scan_modpack(current)
    catalog = await build_migration_catalog(
        previous_modpack_path=old,
        previous_resourcepack_path=None,
        previous_overrides_path=overrides,
        current_modpack_root=current,
        current_scan=scan,
        source_locale="en_us",
        target_locale="ko_kr",
        asset_cache_dir=tmp_path / "assets",
    )

    logical = "override:config/ftbquests/quests/lang/{locale}/chapters/powah.json5"
    assert (
        catalog.match(logical, "quest.66ECC26BC81D0093.title", "Tier: &bBasic")
        == "티어: &b기본"
    )
    assert (
        catalog.match(
            logical,
            "quest.33816AF0E699F19F.quest_desc[0]",
            "This block will drain FE from any charged item.",
        )
        == "이 블록은 충전된 모든 아이템에서 FE를 배수합니다."
    )


@pytest.mark.asyncio
async def test_pipeline_emits_resourcepack_when_migration_has_only_assets(
    tmp_path: Path,
) -> None:
    old = tmp_path / "old"
    current = tmp_path / "current"
    resourcepack = tmp_path / "resourcepack"
    old.mkdir()
    current.mkdir()
    font = resourcepack / "assets/minecraft/font/readable.ttf"
    font.parent.mkdir(parents=True)
    font.write_bytes(b"font-only")

    config = PipelineConfig(
        modpack_path=current,
        output_dir=tmp_path / "out",
        use_tm=False,
        use_user_glossary=False,
        use_mod_translations=False,
        previous_modpack_path=old,
        previous_resourcepack_path=resourcepack,
    )
    pipeline = TranslationPipeline(config, lm=dspy.utils.DummyLM([]))
    try:
        result = await pipeline.run()
    finally:
        pipeline.close()

    assert result.entries == []
    assert (tmp_path / "out/resourcepack/assets/minecraft/font/readable.ttf").read_bytes() == b"font-only"
    assert (tmp_path / "out/resourcepack/pack.mcmeta").is_file()


@pytest.mark.asyncio
async def test_curseforge_manifest_reuses_unchanged_current_mod_as_old_source(
    tmp_path: Path,
) -> None:
    current = tmp_path / "current"
    mods = current / "mods"
    mods.mkdir(parents=True)
    jar_name = "demo-1.0.jar"
    with zipfile.ZipFile(mods / jar_name, "w") as zf:
        zf.writestr(
            "assets/demo/lang/en_us.json",
            json.dumps({"same": "Same mod text"}),
        )
    _write_json(
        current / "minecraftinstance.json",
        {
            "installedAddons": [
                {
                    "addonID": 123,
                    "fileNameOnDisk": jar_name,
                    "installedFile": {"id": 456, "fileName": jar_name},
                }
            ]
        },
    )
    old_export = tmp_path / "old-export.zip"
    with zipfile.ZipFile(old_export, "w") as zf:
        zf.writestr(
            "manifest.json",
            json.dumps({"files": [{"projectID": 123, "fileID": 456}]}),
        )
        zf.writestr("overrides/options.txt", "")
    resourcepack = tmp_path / "resourcepack"
    _write_json(
        resourcepack / "assets/demo/lang/ko_kr.json",
        {"same": "동일 모드 수동 번역"},
    )

    scan = await scan_modpack(current)
    catalog = await build_migration_catalog(
        previous_modpack_path=old_export,
        previous_resourcepack_path=resourcepack,
        previous_overrides_path=None,
        current_modpack_root=current,
        current_scan=scan,
        source_locale="en_us",
        target_locale="ko_kr",
        asset_cache_dir=tmp_path / "assets",
    )

    assert catalog.match(
        "resource:demo/lang/{locale}.json", "same", "Same mod text"
    ) == "동일 모드 수동 번역"


@pytest.mark.asyncio
async def test_curseforge_manifest_reuses_unchanged_resourcepack_addon(
    tmp_path: Path,
) -> None:
    current = tmp_path / "current"
    resourcepacks = current / "resourcepacks"
    resourcepacks.mkdir(parents=True)
    archive_name = "demo-pack.zip"
    with zipfile.ZipFile(resourcepacks / archive_name, "w") as zf:
        zf.writestr(
            "assets/demo/lang/en_us.json",
            json.dumps({"same": "Same resource-pack text"}),
        )
    _write_json(
        current / "minecraftinstance.json",
        {
            "installedAddons": [
                {
                    "addonID": 789,
                    "fileNameOnDisk": archive_name,
                    "isModified": False,
                    "installedFile": {"id": 101, "fileName": archive_name},
                }
            ]
        },
    )
    old_export = tmp_path / "old-export.zip"
    with zipfile.ZipFile(old_export, "w") as zf:
        zf.writestr(
            "manifest.json",
            json.dumps({"files": [{"projectID": 789, "fileID": 101}]}),
        )
        zf.writestr("overrides/options.txt", "")
    translated = tmp_path / "translated"
    _write_json(
        translated / "assets/demo/lang/ko_kr.json",
        {"same": "동일 리소스팩 수동 번역"},
    )

    scan = await scan_modpack(current)
    catalog = await build_migration_catalog(
        previous_modpack_path=old_export,
        previous_resourcepack_path=translated,
        previous_overrides_path=None,
        current_modpack_root=current,
        current_scan=scan,
        source_locale="en_us",
        target_locale="ko_kr",
        asset_cache_dir=tmp_path / "assets",
    )

    assert catalog.match(
        "resource:demo/lang/{locale}.json",
        "same",
        "Same resource-pack text",
    ) == "동일 리소스팩 수동 번역"


@pytest.mark.asyncio
async def test_manifest_inference_never_overrides_explicit_old_source(
    tmp_path: Path,
) -> None:
    current = tmp_path / "current"
    resourcepacks = current / "resourcepacks"
    resourcepacks.mkdir(parents=True)
    archive_name = "demo-pack.zip"
    with zipfile.ZipFile(resourcepacks / archive_name, "w") as zf:
        zf.writestr(
            "assets/demo/lang/en_us.json",
            json.dumps({"same": "Inferred C source", "missing": "Filled from C"}),
        )
    _write_json(
        current / "minecraftinstance.json",
        {
            "installedAddons": [
                {
                    "addonID": 789,
                    "fileNameOnDisk": archive_name,
                    "isModified": False,
                    "installedFile": {"id": 101, "fileName": archive_name},
                }
            ]
        },
    )
    old_export = tmp_path / "old-export.zip"
    with zipfile.ZipFile(old_export, "w") as zf:
        zf.writestr(
            "manifest.json",
            json.dumps({"files": [{"projectID": 789, "fileID": 101}]}),
        )
        zf.writestr(
            "overrides/resourcepacks/explicit/assets/demo/lang/en_us.json",
            json.dumps({"same": "Explicit A source"}),
        )
    translated = tmp_path / "translated"
    _write_json(
        translated / "assets/demo/lang/ko_kr.json",
        {"same": "명시 원문 번역", "missing": "추론 원문 번역"},
    )

    scan = await scan_modpack(current)
    catalog = await build_migration_catalog(
        previous_modpack_path=old_export,
        previous_resourcepack_path=translated,
        previous_overrides_path=None,
        current_modpack_root=current,
        current_scan=scan,
        source_locale="en_us",
        target_locale="ko_kr",
        asset_cache_dir=tmp_path / "assets",
    )

    logical = "resource:demo/lang/{locale}.json"
    assert catalog.old_sources[logical]["same"] == "Explicit A source"
    assert catalog.old_sources[logical]["missing"] == "Filled from C"
    assert (logical, "same") not in catalog.ambiguous


@pytest.mark.asyncio
async def test_manifest_inference_conflict_between_addons_stays_ambiguous(
    tmp_path: Path,
) -> None:
    current = tmp_path / "current"
    mods = current / "mods"
    mods.mkdir(parents=True)
    with zipfile.ZipFile(mods / "alpha-1.0.jar", "w") as zf:
        zf.writestr(
            "assets/minecraft/lang/en_us.json",
            json.dumps({"conflict": "Alpha text", "alpha_only": "Alpha only"}),
        )
    with zipfile.ZipFile(mods / "beta-1.0.jar", "w") as zf:
        zf.writestr(
            "assets/minecraft/lang/en_us.json",
            json.dumps({"conflict": "Beta text"}),
        )
    _write_json(
        current / "minecraftinstance.json",
        {
            "installedAddons": [
                {
                    "addonID": 111,
                    "fileNameOnDisk": "alpha-1.0.jar",
                    "installedFile": {"id": 222, "fileName": "alpha-1.0.jar"},
                },
                {
                    "addonID": 333,
                    "fileNameOnDisk": "beta-1.0.jar",
                    "installedFile": {"id": 444, "fileName": "beta-1.0.jar"},
                },
            ]
        },
    )
    old_export = tmp_path / "old-export.zip"
    with zipfile.ZipFile(old_export, "w") as zf:
        zf.writestr(
            "manifest.json",
            json.dumps(
                {
                    "files": [
                        {"projectID": 111, "fileID": 222},
                        {"projectID": 333, "fileID": 444},
                    ]
                }
            ),
        )
        zf.writestr("overrides/options.txt", "")
    translated = tmp_path / "translated"
    _write_json(
        translated / "assets/minecraft/lang/ko_kr.json",
        {"conflict": "충돌 번역", "alpha_only": "알파 전용 번역"},
    )

    scan = await scan_modpack(current)
    catalog = await build_migration_catalog(
        previous_modpack_path=old_export,
        previous_resourcepack_path=translated,
        previous_overrides_path=None,
        current_modpack_root=current,
        current_scan=scan,
        source_locale="en_us",
        target_locale="ko_kr",
        asset_cache_dir=tmp_path / "assets",
    )

    # Both addons collapse to one logical file, so a key whose inferred source
    # text differs between them cannot be attributed to either mod.
    logical = "resource:minecraft/lang/{locale}.json"
    assert (logical, "conflict") in catalog.ambiguous
    assert catalog.match(logical, "conflict", "Alpha text") is None
    assert catalog.match(logical, "conflict", "Beta text") is None
    assert catalog.match(logical, "alpha_only", "Alpha only") == "알파 전용 번역"


@pytest.mark.asyncio
async def test_old_modpack_zip_rejects_unsafe_nested_scanner_archive(
    tmp_path: Path,
) -> None:
    current = tmp_path / "current"
    _write_json(
        current / "resourcepacks/base/assets/demo/lang/en_us.json",
        {"same": "Same"},
    )
    old_export = tmp_path / "old.zip"
    nested = tmp_path / "nested.zip"
    with zipfile.ZipFile(nested, "w") as zf:
        zf.writestr("../escaped.txt", "nope")
    with zipfile.ZipFile(old_export, "w") as zf:
        zf.writestr("manifest.json", json.dumps({"files": []}))
        zf.write(nested, "overrides/config/paxi/resourcepacks/unsafe.zip")
    translated = tmp_path / "translated"
    _write_json(
        translated / "assets/demo/lang/ko_kr.json",
        {"same": "번역"},
    )

    scan = await scan_modpack(current)
    with pytest.raises(MigrationError, match="unsafe entry"):
        await build_migration_catalog(
            previous_modpack_path=old_export,
            previous_resourcepack_path=translated,
            previous_overrides_path=None,
            current_modpack_root=current,
            current_scan=scan,
            source_locale="en_us",
            target_locale="ko_kr",
            asset_cache_dir=tmp_path / "assets",
        )


@pytest.mark.asyncio
async def test_pipeline_reuses_exact_diff_and_keeps_current_override_template(
    tmp_path: Path,
) -> None:
    old, current, resourcepack, overrides = _seed_three_way(tmp_path)
    config = PipelineConfig(
        modpack_path=current,
        output_dir=tmp_path / "out",
        use_tm=False,
        use_user_glossary=False,
        use_mod_translations=False,
        previous_modpack_path=old,
        previous_resourcepack_path=resourcepack,
        previous_overrides_path=overrides,
    )
    translator = RecordingTranslator()
    pipeline = TranslationPipeline(config, lm=dspy.utils.DummyLM([]))
    pipeline.translator = translator
    try:
        result = await pipeline.run()
    finally:
        pipeline.close()

    assert result.stats.migration_hits == 2
    assert result.stats.translated_entries == 2
    assert set(translator.sources) == {"New resource", "New override"}

    entries = {(entry.file, entry.key): entry for entry in result.entries}
    migrated = [entry for entry in result.entries if entry.status is EntryStatus.MIGRATED]
    assert {entry.translated_text for entry in migrated} == {"수동 리소스", "수동 덮어쓰기"}

    pack_lang = json.loads(
        (tmp_path / "out/resourcepack/assets/demo/lang/ko_kr.json").read_text(encoding="utf-8")
    )
    assert pack_lang == {"changed": "AI New resource", "same": "수동 리소스"}
    assert (tmp_path / "out/resourcepack/assets/demo/font/readable.bin").read_bytes() == b"font-bytes-\x00-unchanged"
    assert (tmp_path / "out/resourcepack/assets/demo/font/default.json").is_file()

    override_path = tmp_path / "out/overrides/config/puffish_skills/categories/demo/definitions.json"
    override_payload = json.loads(override_path.read_text(encoding="utf-8"))
    assert override_payload["title"] == "수동 덮어쓰기"
    assert override_payload["description"] == "AI New override"
    assert override_payload["gameplay_setting"] == "new-value"
    assert not (tmp_path / "out/overrides/config/legacy-only.json").exists()
    assert entries


@pytest.mark.asyncio
async def test_wrong_previous_inputs_fall_back_to_normal_translation(
    tmp_path: Path,
) -> None:
    _old, current, resourcepack, overrides = _seed_three_way(tmp_path)
    wrong_old = tmp_path / "wrong-old"
    _write_json(
        wrong_old / "resourcepacks/base/assets/demo/lang/en_us.json",
        {"same": "Different", "changed": "Also different"},
    )
    config = PipelineConfig(
        modpack_path=current,
        output_dir=tmp_path / "wrong-out",
        use_tm=False,
        use_user_glossary=False,
        use_mod_translations=False,
        previous_modpack_path=wrong_old,
        previous_resourcepack_path=resourcepack,
        previous_overrides_path=overrides,
    )
    translator = RecordingTranslator()
    pipeline = TranslationPipeline(config, lm=dspy.utils.DummyLM([]))
    pipeline.translator = translator
    try:
        result = await pipeline.run()
    finally:
        pipeline.close()

    # The unrelated override A data is absent and resource sources differ;
    # wrong user inputs simply produce zero reuse rather than an identity error.
    assert result.stats.migration_hits == 0
    assert set(translator.sources) == {
        "Same resource",
        "New resource",
        "Same override",
        "New override",
    }


@pytest.mark.asyncio
async def test_pipeline_keeps_current_translation_over_migrated_value(
    tmp_path: Path,
) -> None:
    old = tmp_path / "old"
    current = tmp_path / "current"
    resourcepack = tmp_path / "previous-resourcepack"
    for root in (old, current):
        _write_json(
            root / "resourcepacks/base/assets/demo/lang/en_us.json",
            {"kept": "Same kept", "fresh": "Same fresh"},
        )
    _write_json(
        current / "resourcepacks/base/assets/demo/lang/ko_kr.json",
        {"kept": "C가 배포한 번역"},
    )
    _write_json(
        resourcepack / "assets/demo/lang/ko_kr.json",
        {"kept": "예전 번역", "fresh": "예전 신규 번역"},
    )

    config = PipelineConfig(
        modpack_path=current,
        output_dir=tmp_path / "out",
        use_tm=False,
        use_user_glossary=False,
        use_mod_translations=False,
        previous_modpack_path=old,
        previous_resourcepack_path=resourcepack,
    )
    translator = RecordingTranslator()
    pipeline = TranslationPipeline(config, lm=dspy.utils.DummyLM([]))
    pipeline.translator = translator
    try:
        result = await pipeline.run()
    finally:
        pipeline.close()

    assert result.stats.migration_hits == 1
    assert translator.sources == []
    statuses = {entry.key: entry.status for entry in result.entries}
    assert statuses == {"kept": EntryStatus.SKIPPED, "fresh": EntryStatus.MIGRATED}
    kept = next(entry for entry in result.entries if entry.key == "kept")
    assert kept.translated_text == "C가 배포한 번역"
    # C already ships "kept", so only the migrated new key is overlaid and B's
    # competing value for "kept" never reaches the output.
    pack_lang = json.loads(
        (tmp_path / "out/resourcepack/assets/demo/lang/ko_kr.json").read_text(
            encoding="utf-8"
        )
    )
    assert pack_lang == {"fresh": "예전 신규 번역"}
