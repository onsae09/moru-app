"""Mod blacklist: library/optimization mods excluded from the scan.

Only the modpack author ever reads the strings a library, optimization or
tooling mod ships, so paying to translate them is waste. The blacklist
matches the mod id a JAR *declares* — the identifier the game namespaces
the mod's assets with. JAR file names are not usable for this: the same
mod ships as "Jade-1.20.1-forge-11.6.1.jar" or "jade_1.20.1.jar"
depending on loader and launcher, and ``_clean_mod_name`` reduces those
two to different strings.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from moru_engine.scanner import scan_modpack
from moru_engine.scanner.modpack_scanner import normalize_mod_id


def _jar(
    path: Path,
    mod_id: str | None,
    *,
    metadata: str = "forge",
    namespace: str | None = None,
    keys: dict[str, str] | None = None,
) -> Path:
    """A mod JAR declaring ``mod_id`` the way ``metadata``'s loader does."""
    namespace = namespace or mod_id or "anon"
    keys = keys or {f"item.{namespace}.thing": "Thing"}
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(f"assets/{namespace}/lang/en_us.json", json.dumps(keys))
        if mod_id is None:
            return path
        if metadata == "forge":
            zf.writestr(
                "META-INF/mods.toml",
                f'modLoader="javafml"\n[[mods]]\nmodId="{mod_id}"\nversion="1.0"\n',
            )
        elif metadata == "neoforge":
            zf.writestr(
                "META-INF/neoforge.mods.toml",
                f'[[mods]]\nmodId="{mod_id}"\n',
            )
        elif metadata == "flat_toml":
            zf.writestr("META-INF/mods.toml", f'modId="{mod_id}"\n')
        elif metadata == "fabric":
            zf.writestr("fabric.mod.json", json.dumps({"id": mod_id, "version": "1"}))
        elif metadata == "quilt":
            zf.writestr(
                "quilt.mod.json", json.dumps({"quilt_loader": {"id": mod_id}})
            )
        elif metadata == "legacy":
            zf.writestr("mcmod.info", json.dumps([{"modid": mod_id, "name": mod_id}]))
        else:  # pragma: no cover - guards a typo in a test
            raise AssertionError(f"unknown metadata flavour: {metadata}")
    return path


def _mod_namespaces(result) -> set[str]:
    found = set()
    for file in result.translation_files:
        parts = Path(file.input_path).parts
        if file.file_type == "mod" and "assets" in parts:
            found.add(parts[parts.index("assets") + 1])
    return found


@pytest.mark.asyncio
async def test_no_blacklist_scans_every_mod(tmp_path: Path) -> None:
    """The default (absent config) must reproduce the pre-feature scan."""
    modpack = tmp_path / "modpack"
    _jar(modpack / "mods" / "jade-1.20.1-forge.jar", "jade")
    _jar(modpack / "mods" / "content-mod.jar", "contentmod")

    result = await scan_modpack(modpack)

    assert _mod_namespaces(result) == {"jade", "contentmod"}
    assert result.excluded_mods == []


@pytest.mark.asyncio
async def test_blacklisted_mod_contributes_no_files(tmp_path: Path) -> None:
    """Excluded entries must not be counted, billed or translated."""
    modpack = tmp_path / "modpack"
    _jar(modpack / "mods" / "jade-1.20.1-forge-11.6.1.jar", "jade")
    _jar(modpack / "mods" / "content-mod.jar", "contentmod")

    result = await scan_modpack(modpack, mod_blacklist=["jade"])

    assert _mod_namespaces(result) == {"contentmod"}
    assert [(m.mod_id, m.jar_name) for m in result.excluded_mods] == [
        ("jade", "jade-1.20.1-forge-11.6.1.jar")
    ]
    # Not merely filtered out of the report: never extracted at all.
    assert not (
        modpack / ".mct_cache" / "extracted" / "jade-1.20.1-forge-11.6.1.jar"
    ).exists()
    assert all("jade" not in str(pair.source_path) for pair in result.all_translation_pairs)


@pytest.mark.asyncio
async def test_matching_follows_the_declared_id_not_the_file_name(
    tmp_path: Path,
) -> None:
    """The identifier has to survive repackaging and mislabelled JARs."""
    modpack = tmp_path / "modpack"
    # Same mod, two real-world file-name shapes: _clean_mod_name turns these
    # into "Jade" and "jade", so a file-name blacklist would catch one only.
    _jar(modpack / "mods" / "Jade-1.20.1-forge-11.6.1.jar", "jade", namespace="jade")
    _jar(modpack / "mods" / "jade_1.20.1.jar", "jade", namespace="jade2")
    # A file name that merely mentions the id belongs to another mod.
    _jar(modpack / "mods" / "jade-addons-forge.jar", "jadeaddons")

    result = await scan_modpack(modpack, mod_blacklist=["jade"])

    assert {m.jar_name for m in result.excluded_mods} == {
        "Jade-1.20.1-forge-11.6.1.jar",
        "jade_1.20.1.jar",
    }
    assert _mod_namespaces(result) == {"jadeaddons"}


@pytest.mark.parametrize(
    "metadata",
    ["forge", "neoforge", "flat_toml", "fabric", "quilt", "legacy"],
)
@pytest.mark.asyncio
async def test_every_loader_metadata_flavour_is_read(
    tmp_path: Path, metadata: str
) -> None:
    """A pack mixes loaders and mod ages; all of them must be matchable."""
    modpack = tmp_path / f"modpack-{metadata}"
    _jar(modpack / "mods" / "opt.jar", "modernfix", metadata=metadata)

    result = await scan_modpack(modpack, mod_blacklist=["modernfix"])

    assert [m.mod_id for m in result.excluded_mods] == ["modernfix"]


@pytest.mark.asyncio
async def test_metadata_less_jar_falls_back_to_asset_namespace(
    tmp_path: Path,
) -> None:
    """An asset-only JAR still has the namespace the scanner keys files by."""
    modpack = tmp_path / "modpack"
    _jar(modpack / "mods" / "bare.jar", None, namespace="clumps")

    result = await scan_modpack(modpack, mod_blacklist=["clumps"])

    assert [m.mod_id for m in result.excluded_mods] == ["clumps"]


@pytest.mark.asyncio
async def test_display_name_spelling_still_matches_the_id(tmp_path: Path) -> None:
    """Users type what CurseForge shows them, not the declared id."""
    modpack = tmp_path / "modpack"
    _jar(modpack / "mods" / "ec.jar", "entityculling", metadata="fabric")

    result = await scan_modpack(modpack, mod_blacklist=["Entity Culling"])

    assert [m.mod_id for m in result.excluded_mods] == ["entityculling"]


@pytest.mark.asyncio
async def test_blacklist_entry_is_not_a_substring_match(tmp_path: Path) -> None:
    """"sodium" must not drag "sodiumdynamiclights" out of the scan."""
    modpack = tmp_path / "modpack"
    _jar(modpack / "mods" / "sodium.jar", "sodium", metadata="fabric")
    _jar(modpack / "mods" / "sdl.jar", "sodiumdynamiclights", metadata="fabric")

    result = await scan_modpack(modpack, mod_blacklist=["sodium"])

    assert [m.mod_id for m in result.excluded_mods] == ["sodium"]
    assert _mod_namespaces(result) == {"sodiumdynamiclights"}


def test_normalize_keeps_distinct_ids_distinct() -> None:
    """Dropping punctuation may relax matching but never merge real ids."""
    assert normalize_mod_id("Entity Culling") == normalize_mod_id("entityculling")
    assert normalize_mod_id("Modern-Fix") == normalize_mod_id("modernfix")
    # "_" is legal inside an id and normalizes on both sides, so ids that
    # differ only in punctuation are the only ones that collapse.
    assert normalize_mod_id("archers_expansion") == normalize_mod_id("ArchersExpansion")
    assert normalize_mod_id("sodium") != normalize_mod_id("sodiumdynamiclights")


@pytest.mark.asyncio
async def test_parallel_jar_scan_is_deterministic_and_complete(tmp_path: Path) -> None:
    """JARs are scanned concurrently; the merged result must not depend on
    which one finished first."""
    modpack = tmp_path / "pack"
    ids = [f"mod{i:02d}" for i in range(24)]
    for mod_id in ids:
        _jar(modpack / "mods" / f"{mod_id}.jar", mod_id)
    _jar(modpack / "mods" / "zz_sodium.jar", "sodium")

    first = await scan_modpack(modpack, mod_blacklist=["sodium"])
    second = await scan_modpack(modpack, mod_blacklist=["sodium"])

    paths = [f.input_path for f in first.translation_files if f.file_type == "mod"]
    assert paths == [f.input_path for f in second.translation_files if f.file_type == "mod"]
    assert _mod_namespaces(first) == set(ids)
    assert [m.mod_id for m in first.excluded_mods] == ["sodium"]


@pytest.mark.parametrize(
    ("entry", "extracted"),
    [
        ("data/mod/recipe/thing.json", False),
        ("data/mod/loot_table/blocks/thing.json", False),
        ("data/mod/advancement/root.json", False),
        ("data/mod/recipes/thing.json", False),
        ("assets/mod/lang/en_us.json", True),
        # Not a data-pack root folder: a book category that holds text.
        ("data/mod/patchouli_books/guide/en_us/entries/recipe/a.json", True),
    ],
)
def test_singular_1_21_data_folders_are_not_extracted(entry: str, extracted: bool) -> None:
    from moru_engine.scanner.modpack_scanner import ModpackScanner

    assert ModpackScanner()._should_extract_from_jar(entry) is extracted
