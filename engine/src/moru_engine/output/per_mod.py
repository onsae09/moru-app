"""Installable resource packs grouped by the JAR that owns their entries."""

from __future__ import annotations

import asyncio
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from ..modsources import ModSources
from ..pipeline.orchestrator import (
    EntryStatus,
    PipelineResult,
    _FRESH_STATUSES,
    file_outputs,
)
from ..scanner.pack_identity import PackIdentity, detect_pack_identity
from .generator import (
    BILINGUAL_DIRNAME,
    RESOURCEPACK_DIRNAME,
    FileOutput,
    OutputConfig,
    OutputGenerator,
    create_zip_from_directory,
    jar_name_for,
    pack_format_for_minecraft_version,
)

_MINECRAFT_VERSION = re.compile(r"^\d+\.\d+(\.\d+)?$")


def build_mod_packs(
    result: PipelineResult,
    sources: ModSources,
    staging: Path,
    identity: PackIdentity | None = None,
) -> list[dict[str, Any]]:
    """Build in a worker thread, keeping handler I/O off the server event loop.

    Empty or unidentified mods stay in the result so the caller can account
    for every skipped JAR rather than silently losing them from the summary.
    """
    return asyncio.run(_build_mod_packs(result, sources, staging, identity))


async def _build_mod_packs(
    result: PipelineResult,
    sources: ModSources,
    staging: Path,
    identity: PackIdentity | None,
) -> list[dict[str, Any]]:
    identity = identity or detect_pack_identity(result.config.modpack_path)
    by_jar: dict[str, list[FileOutput]] = defaultdict(list)
    for output in file_outputs(result):
        jar = jar_name_for(output.source_path)
        if jar.lower().endswith(".jar"):
            by_jar[jar].append(output)
    entries: dict[str, list] = defaultdict(list)
    for entry in result.entries:
        jar = jar_name_for(entry.file)
        if jar.lower().endswith(".jar"):
            entries[jar].append(entry)
    names: dict[str, str] = {}
    if result.scan_result is not None:
        for file in result.scan_result.translation_files:
            if file.jar_name and file.category.startswith("Mod: "):
                names[file.jar_name] = file.category.removeprefix("Mod: ")
    metadata = {source.jar_name.lower(): source for source in sources.sources}
    records: list[dict[str, Any]] = []
    for index, (jar, mod_entries) in enumerate(entries.items()):
        source = metadata.get(jar.lower())
        display_name = names.get(jar) or Path(jar).stem.split("-")[0].replace("_", " ").title()
        versions = list(dict.fromkeys(
            version for version in (source.game_versions if source else [])
            if _MINECRAFT_VERSION.fullmatch(version)
        ))
        if not versions and identity.mc_version:
            versions = [identity.mc_version]
        translated = sum(
            entry.status in _FRESH_STATUSES and bool(entry.translated_text)
            for entry in mod_entries
        )
        failed = sum(entry.status is EntryStatus.FAILED for entry in mod_entries)
        total = len(mod_entries)
        skipped = sum(entry.status is EntryStatus.SKIPPED for entry in mod_entries)
        directory = staging / str(index)
        generator = OutputGenerator(OutputConfig(
            modpack_root=result.config.modpack_path,
            output_dir=directory,
            source_locale=result.config.source_locale,
            target_locale=result.config.target_locale,
            pack_format=pack_format_for_minecraft_version(identity.mc_version, result.config.pack_format),
            description=f"§a모루§7로 번역됨 — §f{display_name} §amoru.gg",
            bilingual_names=True,
        ))
        generation = await generator.generate(by_jar.get(jar, []))
        if generation.errors or (generation.bilingual and generation.bilingual.errors):
            raise ValueError(f"Could not generate complete mod pack for {jar}")
        zips: dict[str, Path] = {}
        for kind, tree in (
            ("resource_pack", directory / RESOURCEPACK_DIRNAME),
            ("resource_pack_bilingual", directory / BILINGUAL_DIRNAME / RESOURCEPACK_DIRNAME),
        ):
            if tree.is_dir() and any(path.is_file() for path in tree.rglob("*")):
                archive = directory / f"{kind}.zip"
                create_zip_from_directory(tree, archive)
                zips[kind] = archive
        records.append({
            "jar_name": jar,
            "display_name": display_name,
            "project_id": source.project_id if source else None,
            "file_id": source.file_id if source else None,
            "game_versions": versions,
            "version_label": (source.version_label if source else "") or Path(jar).stem,
            "zips": zips,
            "stats": {
                "total_entries": total,
                "translated_entries": translated,
                "failed_entries": failed,
                "coverage_percent": round(100.0 * translated / max(total - skipped, 1), 2),
            },
        })
    return records
