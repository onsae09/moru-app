"""Best-effort JAR identities from launcher metadata, then CurseForge hashes."""

from __future__ import annotations

import asyncio
import json
import logging
import tomllib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from .curseforge import CurseForgeClient, curseforge_fingerprint

logger = logging.getLogger(__name__)


@dataclass
class ModSource:
    jar_name: str
    project_id: int | None = None
    file_id: int | None = None
    source: Literal["instance", "prism", "fingerprint", "unresolved"] = "unresolved"
    game_versions: list[str] = field(default_factory=list)
    version_label: str = ""


@dataclass
class ModSources:
    status: Literal["pending", "running", "done", "failed"] = "pending"
    sources: list[ModSource] = field(default_factory=list)

    def payload(self) -> dict[str, Any]:
        resolved = sum(source.project_id is not None for source in self.sources)
        return {
            "status": self.status,
            "total": len(self.sources),
            "resolved": resolved,
            "unresolved": len(self.sources) - resolved,
            "sources": [
                {key: value for key, value in asdict(source).items()
                 if key not in ("game_versions", "version_label")}
                for source in self.sources
            ],
        }


def _positive_id(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _current_archive_files(modpack_root: Path) -> dict[str, tuple[int, int]]:
    """Map unmodified installed addon filenames to CurseForge identities."""
    path = modpack_root / "minecraftinstance.json"
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return {}
    if not isinstance(payload, dict) or not isinstance(payload.get("installedAddons"), list):
        return {}
    result: dict[str, tuple[int, int]] = {}
    for addon in payload["installedAddons"]:
        if not isinstance(addon, dict) or addon.get("isModified") is True:
            continue
        installed = addon.get("installedFile")
        if not isinstance(installed, dict):
            continue
        project = addon.get("addonID", installed.get("projectId"))
        file_id = installed.get("id")
        filename = addon.get("fileNameOnDisk") or installed.get("fileNameOnDisk") or installed.get("fileName")
        if _positive_id(project) and _positive_id(file_id) and isinstance(filename, str):
            result[filename.lower()] = (project, file_id)
    return result


def _prism_archive_files(mods_dir: Path) -> dict[str, tuple[int, int]]:
    result: dict[str, tuple[int, int]] = {}
    for path in sorted((mods_dir / ".index").glob("*.pw.toml")):
        try:
            payload = tomllib.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, tomllib.TOMLDecodeError):
            continue
        filename = payload.get("filename")
        update = payload.get("update")
        curseforge = update.get("curseforge") if isinstance(update, dict) else None
        if not isinstance(filename, str) or not isinstance(curseforge, dict):
            continue
        project, file_id = curseforge.get("project-id"), curseforge.get("file-id")
        if _positive_id(project) and _positive_id(file_id):
            result[filename.lower()] = (project, file_id)
    return result


def _local_sources(root: Path) -> tuple[list[ModSource], dict[str, Path]]:
    roots = [root, root / ".minecraft", root / "minecraft"]
    metadata_roots = ([root.parent] if root.name in (".minecraft", "minecraft") else []) + roots
    instance: dict[str, tuple[int, int]] = {}
    for candidate in metadata_roots:
        instance.update(_current_archive_files(candidate))
    sources: dict[str, ModSource] = {}
    remaining: dict[str, Path] = {}
    for candidate in roots:
        mods_dir = candidate / "mods"
        prism = _prism_archive_files(mods_dir)
        for jar in sorted(mods_dir.glob("*.jar")):
            if not jar.is_file():
                continue
            key = jar.name.lower()
            ids = instance.get(key) or prism.get(key)
            source = ModSource(jar_name=jar.name)
            if ids is not None:
                source.project_id, source.file_id = ids
                source.source = "instance" if key in instance else "prism"
            else:
                remaining[jar.name] = jar
            sources[jar.name] = source
    return list(sources.values()), remaining


async def resolve_mod_sources(
    root: Path, state: ModSources, client: CurseForgeClient | None = None
) -> ModSources:
    """Update the shared state in place; failures retain launcher identities.

    This coroutine belongs to the scan record, not its scan task: cancelling
    an upload waiter must never cancel discovery for another consumer.
    """
    state.status = "running"
    failed = False
    try:
        state.sources, remaining = await asyncio.to_thread(_local_sources, root)
        client = client or CurseForgeClient()
        if client.enabled and remaining:
            loop = asyncio.get_running_loop()
            with ThreadPoolExecutor(max_workers=4, thread_name_prefix="moru-fingerprint") as pool:
                fingerprints = await asyncio.gather(*(
                    loop.run_in_executor(pool, curseforge_fingerprint, path)
                    for path in remaining.values()
                ), return_exceptions=True)
            hashes: dict[str, int] = {}
            for jar_name, value in zip(remaining, fingerprints, strict=True):
                if isinstance(value, BaseException):
                    failed = True
                    logger.warning("Could not fingerprint %s: %s", jar_name, value)
                else:
                    hashes[jar_name] = value
            matches = await client.fingerprint_matches(list(hashes.values()))
            for source in state.sources:
                match = matches.get(hashes.get(source.jar_name))
                if match is not None:
                    source.project_id, source.file_id, source.game_versions, source.version_label = match
                    source.source = "fingerprint"
    except asyncio.CancelledError:
        state.status = "failed"
        raise
    except Exception:
        failed = True
        logger.exception("Mod identity lookup failed for %s", root)
    state.status = "failed" if failed else "done"
    return state
