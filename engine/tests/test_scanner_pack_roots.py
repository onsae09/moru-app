"""Scanner coverage for every root a user-installed pack can live in.

OpenLoader and Paxi mount resource/data packs from their own folders. A scan
that only walks ``resourcepacks/`` returns zero entries for those packs, and
the modpack comes back untranslated.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from moru_engine.output import Route, route_for
from moru_engine.scanner import scan_modpack
from moru_engine.scanner.modpack_scanner import PACK_ROOTS


def _lang(path: Path, key: str = "smoke.key") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({key: "Hello"}), encoding="utf-8")
    return path


def _pack_zip(zip_path: Path, namespace: str, staging: Path) -> Path:
    """A resource pack shipped as a ZIP, the way users install them."""
    _lang(staging / "assets" / namespace / "lang" / "en_us.json")
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w") as zf:
        for file in sorted(staging.rglob("*")):
            if file.is_file():
                zf.write(file, file.relative_to(staging))
    return zip_path


def _namespaces(result, modpack: Path) -> set[str]:
    found = set()
    for file in result.translation_files:
        parts = Path(file.input_path).parts
        if "assets" in parts:
            found.add(parts[parts.index("assets") + 1])
    return found


@pytest.mark.asyncio
async def test_loose_packs_found_in_every_pack_root(tmp_path: Path) -> None:
    """Each resource root contributes its entries, not just ``resourcepacks/``."""
    modpack = tmp_path / "modpack"
    expected = set()
    for rel_root, file_type in PACK_ROOTS:
        if file_type != "resourcepacks":
            continue
        namespace = rel_root.replace("/", "_")
        expected.add(namespace)
        _lang(
            modpack / rel_root / "Pack" / "assets" / namespace / "lang" / "en_us.json"
        )

    result = await scan_modpack(modpack)

    assert _namespaces(result, modpack) == expected


@pytest.mark.asyncio
async def test_zipped_openloader_pack_is_extracted_and_scanned(
    tmp_path: Path,
) -> None:
    """A ZIP under an OpenLoader root reaches the scan through the cache."""
    modpack = tmp_path / "modpack"
    _pack_zip(
        modpack / "openloader" / "resources" / "Zipped.zip",
        "olzip",
        tmp_path / "staging",
    )

    result = await scan_modpack(modpack)

    assert "olzip" in _namespaces(result, modpack)
    # Unpacked into the modpack-local cache, never beside the user's ZIP.
    assert not list(modpack.rglob("*.zip_extracted"))
    assert (modpack / ".mct_cache" / "resourcepacks").is_dir()


@pytest.mark.asyncio
async def test_same_zip_name_in_two_roots_does_not_collide(tmp_path: Path) -> None:
    """Per-root cache subdirectories keep identically named packs apart."""
    modpack = tmp_path / "modpack"
    _pack_zip(
        modpack / "openloader" / "resources" / "Pack.zip",
        "fromopenloader",
        tmp_path / "a",
    )
    _pack_zip(
        modpack / "paxi" / "resourcepacks" / "Pack.zip",
        "frompaxi",
        tmp_path / "b",
    )

    result = await scan_modpack(modpack)

    assert {"fromopenloader", "frompaxi"} <= _namespaces(result, modpack)


@pytest.mark.asyncio
async def test_pack_root_content_routes_into_the_resource_pack(
    tmp_path: Path,
) -> None:
    """Scanning is pointless if the output router then drops the files."""
    modpack = tmp_path / "modpack"
    _lang(
        modpack
        / "openloader"
        / "resources"
        / "Loose"
        / "assets"
        / "ol"
        / "lang"
        / "en_us.json"
    )
    _pack_zip(
        modpack / "paxi" / "resourcepacks" / "Zipped.zip", "px", tmp_path / "staging"
    )

    result = await scan_modpack(modpack)

    assert result.translation_files
    for file in result.translation_files:
        assert route_for(file.input_path) is Route.RESOURCE_PACK


@pytest.mark.asyncio
async def test_updated_zip_is_re_extracted_and_a_removed_one_dropped(
    tmp_path: Path,
) -> None:
    """The extraction used to be reused for as long as its folder existed,
    so an updated pack kept being scanned from its first version."""
    import os

    modpack = tmp_path / "modpack"
    zip_path = modpack / "resourcepacks" / "Pack.zip"
    _pack_zip(zip_path, "oldns", tmp_path / "v1")
    assert "oldns" in _namespaces(await scan_modpack(modpack), modpack)

    zip_path.unlink()
    _pack_zip(zip_path, "newns", tmp_path / "v2")
    os.utime(zip_path, ns=(1, 1))  # a different stamp even within one tick
    found = _namespaces(await scan_modpack(modpack), modpack)
    assert "newns" in found
    assert "oldns" not in found

    zip_path.unlink()
    assert _namespaces(await scan_modpack(modpack), modpack) == set()


@pytest.mark.asyncio
async def test_zip_bomb_resource_pack_is_refused(tmp_path: Path, monkeypatch) -> None:
    from moru_engine.scanner import modpack_scanner

    modpack = tmp_path / "modpack"
    _pack_zip(modpack / "resourcepacks" / "Bomb.zip", "bomb", tmp_path / "s")
    monkeypatch.setattr(modpack_scanner, "MAX_RESOURCE_PACK_UNPACKED_BYTES", 4)

    result = await scan_modpack(modpack)

    assert "bomb" not in _namespaces(result, modpack)
    assert not (modpack / ".mct_cache" / "resourcepacks" / "resourcepacks" / "Bomb.zip").exists()
