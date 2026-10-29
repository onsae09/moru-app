"""Open a modpack archive (.zip / .mrpack) as a folder the scanner can read.

Players often hold a pack as the file they downloaded: a CurseForge export
(``manifest.json`` + ``overrides/``), a Modrinth ``.mrpack``
(``modrinth.index.json`` + ``overrides/``) or a plain zipped instance. The
archive is extracted once into a cache folder keyed by its path, size and
mtime, so picking the same file again reuses the extraction.

Both launcher export formats list mods by download id instead of shipping
the JARs, so such a pack carries only what ``overrides/`` holds — configs,
quests, KubeJS scripts, resource packs. The caller reports that through
``has_mods``.
"""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path

from .migration.previous_translation import MigrationError, safe_extract_zip

ARCHIVE_SUFFIXES = (".zip", ".mrpack")

#: Marks a finished extraction; a folder without it was interrupted.
_COMPLETE_MARKER = ".moru-import-complete"

_TOP_LEVEL_NAMES = {"mods", "config", "kubejs", "resourcepacks", "defaultconfigs", "overrides"}


class ArchiveImportError(ValueError):
    """The archive cannot be opened as a modpack."""


@dataclass(frozen=True, slots=True)
class ImportedPack:
    #: Folder to scan: the archive's ``overrides/`` for launcher exports.
    modpack_path: Path
    name: str
    has_mods: bool


def _cache_key(archive: Path) -> str:
    stat = archive.stat()
    raw = f"{archive.resolve()}|{stat.st_size}|{stat.st_mtime_ns}".encode()
    return hashlib.sha256(raw).hexdigest()[:12]


def _pack_root(extracted: Path) -> Path:
    """The folder holding the pack's game files inside an extraction."""
    root = extracted
    children = [child for child in root.iterdir() if child.name != "__MACOSX"]
    names = {child.name.lower() for child in children}
    if not names & (_TOP_LEVEL_NAMES | {"manifest.json", "modrinth.index.json"}):
        # A zipped folder: one wrapper directory around the instance.
        if len(children) == 1 and children[0].is_dir():
            root = children[0]
    if (root / "overrides").is_dir() and (
        (root / "manifest.json").is_file() or (root / "modrinth.index.json").is_file()
    ):
        return root / "overrides"
    return root


def import_modpack_archive(archive: Path, cache_root: Path) -> ImportedPack:
    """Extract ``archive`` under ``cache_root`` (once) and locate the pack."""
    if not archive.is_file() or archive.suffix.lower() not in ARCHIVE_SUFFIXES:
        raise ArchiveImportError(f"not a .zip or .mrpack file: {archive}")
    destination = cache_root / f"{archive.stem[:60]}-{_cache_key(archive)}"
    if not (destination / _COMPLETE_MARKER).is_file():
        shutil.rmtree(destination, ignore_errors=True)
        staging = destination.with_name(destination.name + ".partial")
        shutil.rmtree(staging, ignore_errors=True)
        try:
            safe_extract_zip(archive, staging)
        except MigrationError as exc:
            shutil.rmtree(staging, ignore_errors=True)
            raise ArchiveImportError(str(exc)) from exc
        (staging / _COMPLETE_MARKER).write_text(str(archive), encoding="utf-8")
        staging.rename(destination)
    root = _pack_root(destination)
    mods = root / "mods"
    has_mods = mods.is_dir() and any(path.suffix.lower() == ".jar" for path in mods.iterdir())
    return ImportedPack(modpack_path=root, name=archive.stem, has_mods=has_mods)
