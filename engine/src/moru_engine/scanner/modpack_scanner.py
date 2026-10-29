"""Scanner for finding language files in Minecraft modpacks."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import tomllib
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from glob import escape as glob_escape
from glob import iglob
from pathlib import Path
from typing import TYPE_CHECKING

from ..handlers.base import create_default_registry
from ..models import LanguageFilePair, ScanProgressCallback
from ..parsers import BaseParser
from . import resource_overrides
from .class_strings import HardcodedMod, HardcodedString, find_hardcoded_strings

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = logging.getLogger(__name__)

# Directories to scan for translation files
DIR_FILTER_WHITELIST = [
    "lang/",
    "assets/",
    "data/",
    "kubejs/",
    "config/",
    "patchouli_books/",
]


#: (relative root, recorded ``file_type``) for every place a user-installed
#: pack can live. OpenLoader and Paxi are loader mods that mount packs from
#: their own folders instead of ``resourcepacks/``/``datapacks/``, so a pack
#: installed through either is invisible to a scan that only looks at the
#: vanilla folders - the reason those modpacks came back untranslated.
PACK_ROOTS: tuple[tuple[str, str], ...] = (
    ("resourcepacks", "resourcepacks"),
    ("openloader/resources", "resourcepacks"),
    ("paxi/resourcepacks", "resourcepacks"),
    ("datapacks", "datapacks"),
    ("openloader/data", "datapacks"),
    ("paxi/datapacks", "datapacks"),
)


#: The blacklist matches on the mod id a JAR *declares* in its loader
#: metadata: that is the identifier the game itself uses — it namespaces
#: the mod's assets and therefore every translation key it owns
#: ("item.jade.…") — and it is fixed for the life of the mod. JAR file
#: names and display names are not: the same build ships as
#: "Jade-1.20.1-forge-11.6.1.jar" or "jade_1.20.1.jar" depending on the
#: loader and the launcher, and :meth:`ModpackScanner._clean_mod_name`
#: reduces those two to different strings ("Jade" vs "jade").
#:
#: Non-alphanumerics are dropped before comparing ids, so a user typing a
#: mod's display name ("Entity Culling", "Modern-Fix") still matches its
#: declared id ("entityculling", "modernfix"). Ids that legitimately
#: contain "_" ("archers_expansion") normalize on both sides and stay
#: equal, so the relaxation cannot make two real ids collide.
_ID_NOISE_RE = re.compile(r"[^a-z0-9]+")


def normalize_mod_id(text: str) -> str:
    """Comparison form of a mod id or blacklist entry."""
    return _ID_NOISE_RE.sub("", text.strip().lower())


def _json_entry(zf: zipfile.ZipFile, name: str) -> object:
    """Parse a JSON JAR entry; any problem means "not this format"."""
    try:
        return json.loads(zf.read(name).decode("utf-8-sig", errors="replace"))
    except (KeyError, OSError, ValueError, zipfile.BadZipFile):
        return None


def _toml_entry(zf: zipfile.ZipFile, name: str) -> dict[str, object]:
    """Parse a TOML JAR entry; any problem means "not this format"."""
    try:
        return tomllib.loads(zf.read(name).decode("utf-8-sig", errors="replace"))
    except (KeyError, OSError, ValueError, tomllib.TOMLDecodeError, zipfile.BadZipFile):
        return {}


def declared_mod_ids(zf: zipfile.ZipFile) -> set[str]:
    """Mod ids a JAR declares in its loader metadata.

    Empty for a JAR carrying none (a bare asset-only JAR, or metadata this
    tolerant reader cannot make sense of); the caller falls back to the
    asset namespaces the scanner already keys files by.
    """
    names = set(zf.namelist())
    ids: set[str] = set()

    fabric = _json_entry(zf, "fabric.mod.json") if "fabric.mod.json" in names else None
    if isinstance(fabric, dict) and isinstance(fabric.get("id"), str):
        ids.add(fabric["id"])

    quilt = _json_entry(zf, "quilt.mod.json") if "quilt.mod.json" in names else None
    if isinstance(quilt, dict):
        loader = quilt.get("quilt_loader")
        if isinstance(loader, dict) and isinstance(loader.get("id"), str):
            ids.add(loader["id"])

    for entry in ("META-INF/mods.toml", "META-INF/neoforge.mods.toml"):
        if entry not in names:
            continue
        data = _toml_entry(zf, entry)
        # Forge/NeoForge declare a [[mods]] array; the flat form appears in
        # hand-written and generated single-mod JARs.
        if isinstance(data.get("modId"), str):
            ids.add(str(data["modId"]))
        mods = data.get("mods")
        if isinstance(mods, list):
            for mod in mods:
                if isinstance(mod, dict) and isinstance(mod.get("modId"), str):
                    ids.add(mod["modId"])

    legacy = _json_entry(zf, "mcmod.info") if "mcmod.info" in names else None
    if isinstance(legacy, list):
        for mod in legacy:
            if isinstance(mod, dict) and isinstance(mod.get("modid"), str):
                ids.add(mod["modid"])

    return {mod_id for mod_id in ids if mod_id.strip()}


def asset_namespaces(zf: zipfile.ZipFile) -> set[str]:
    """Top-level ``assets/<namespace>`` names present in a JAR."""
    found: set[str] = set()
    for entry in zf.namelist():
        parts = entry.replace("\\", "/").split("/")
        if len(parts) > 2 and parts[0] == "assets" and parts[1]:
            found.add(parts[1])
    return found


#: Marker inside a resource-pack extraction naming the ZIP stamp it came
#: from. Extension-less, so no handler ever reads it as a translation file.
_EXTRACT_STAMP_NAME = ".moru-extract-stamp"
#: Declared uncompressed size beyond which a resource-pack ZIP is refused.
#: Real packs with lang files are tens of MB even with textures; a crafted
#: one could otherwise expand into the user's disk.
MAX_RESOURCE_PACK_UNPACKED_BYTES = 2 * 1024**3


#: JARs extracted concurrently during a scan.
_JAR_SCAN_WORKERS = min(8, os.cpu_count() or 4)
#: Minecraft 1.21's singular data-pack folders (recipes/ -> recipe/, ...):
#: game data, never player-facing text, like their plural predecessors.
_SINGULAR_DATA_DIR_RE = re.compile(
    r"(?:^|/)data/[^/]+/(?:recipe|loot_table|advancement|structure|predicate"
    r"|item_modifier)/"
)


#: Cache-key form of a JAR: size and mtime identify the exact bytes
#: without hashing megabytes, the same trade every build system makes.
def _jar_stamp(jar_path: str) -> str:
    try:
        stat = os.stat(jar_path)
    except OSError:
        return ""
    return f"{stat.st_size}:{stat.st_mtime_ns}"


def _load_hardcoded_cache(cache: Path, stamp: str) -> HardcodedMod | None:
    """A previous verdict for this exact JAR, or None to rescan."""
    try:
        record = json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(record, dict) or record.get("stamp") != stamp:
        return None
    raw = record.get("strings")
    if not isinstance(raw, list):
        return None
    try:
        strings = [
            HardcodedString(
                text=item["text"], class_name=item["class_name"], kind=item["kind"]
            )
            for item in raw
        ]
    except (TypeError, KeyError):
        return None
    return HardcodedMod(
        mod_id=str(record.get("mod_id", "")),
        jar_name=str(record.get("jar_name", "")),
        strings=strings,
    )


def _store_hardcoded_cache(
    cache: Path, stamp: str, mod_id: str, jar_name: str, finding: HardcodedMod | None
) -> None:
    """Memoize the verdict, including "nothing found" — the clean result
    is the common one and is exactly what a rescan should not redo."""
    record = {
        "stamp": stamp,
        "mod_id": finding.mod_id if finding else mod_id,
        "jar_name": jar_name,
        "strings": [
            {"text": s.text, "class_name": s.class_name, "kind": s.kind}
            for s in (finding.strings if finding else ())
        ],
    }
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(record), encoding="utf-8")
    except OSError as exc:
        logger.debug("Could not cache hardcoded-text verdict for %s: %s", jar_name, exc)
#: A locale-shaped chunk counts only at a real boundary: preceded by a
#: non-alphanumeric (start, /, \, _, -) and followed by a separator or
#: the end. An optional prefix here once made "act_ii.snbt" and
#: "act_iv.snbt" both normalize to ".../a/LOCALE.snbt" ("ct_ii"/"ct_iv"
#: read as locales), so one chapter silently overwrote the other in the
#: pairing dict — which file survived depended on glob order.
LOCALE_CHUNK_RE = re.compile(r"(?<![a-z0-9])[a-z]{2}_[a-z]{2}(?=[/\\.]|$)")


@dataclass
class TranslationFile:
    """Information about a translation file."""

    input_path: str
    file_type: (
        str  # config, ftbquests, kubejs, patchouli, resourcepacks, datapacks, mod
    )
    lang_type: str = "source"  # source, target, other
    jar_name: str | None = None
    category: str = ""


@dataclass
class ExcludedMod:
    """One mod JAR left out of the scan by the mod blacklist."""

    #: The blacklisted identifier this JAR matched.
    mod_id: str
    jar_name: str


@dataclass
class SourceOverride:
    """Per-pack tally of source strings an enabled resource pack replaced."""

    pack: str
    keys: int


@dataclass
class ScanResult:
    """Result of scanning a modpack for language files."""

    modpack_path: Path
    source_locale: str = "en_us"
    target_locale: str = "ko_kr"

    # Found files
    paired_files: list[LanguageFilePair] = field(default_factory=list)
    source_only_files: list[LanguageFilePair] = field(default_factory=list)
    target_only_files: list[Path] = field(default_factory=list)

    # Every discovered translation file, including unpaired ones
    translation_files: list[TranslationFile] = field(default_factory=list)

    # Mod JARs the blacklist kept out: never extracted, so their entries
    # are absent from every count above. Reported so the scan screen can
    # show what was dropped and let the user put a mod back.
    excluded_mods: list[ExcludedMod] = field(default_factory=list)

    # Source strings replaced by the resource packs options.txt enables,
    # tallied per pack.
    source_overrides: list[SourceOverride] = field(default_factory=list)

    # Mods that build player-facing text in Java instead of a lang file.
    # No language file — ours, the pack's, or a hand translation — can
    # reach these strings, so they are reported rather than silently
    # left in English for the user to mistake for a moru failure.
    hardcoded_mods: list[HardcodedMod] = field(default_factory=list)

    # Statistics
    total_source_files: int = 0
    total_target_files: int = 0
    total_paired: int = 0

    @property
    def all_translation_pairs(self) -> list[LanguageFilePair]:
        """Get all files that need translation (paired + source-only)."""
        return self.paired_files + self.source_only_files


class ModpackScanner:
    """Scanner for Minecraft modpack language files.

    Walks a modpack directory structure (mods, config, kubejs, quest
    files, resource packs) to find translatable language files and pair
    source-locale files with their target-locale counterparts.
    """

    def __init__(
        self,
        source_locale: str = "en_us",
        target_locale: str = "ko_kr",
        progress_callback: ScanProgressCallback | None = None,
        mod_blacklist: Iterable[str] | None = None,
    ) -> None:
        """Initialize the scanner.

        Args:
            source_locale: Source language locale code.
            target_locale: Target language locale code.
            progress_callback: Optional callback for progress updates.
            mod_blacklist: Mod ids whose JARs are left out of the scan.
                None or empty scans everything, exactly as before.
        """
        self.source_locale = source_locale.lower()
        self.target_locale = target_locale.lower()
        self.progress_callback = progress_callback
        self.mod_blacklist = frozenset(
            normalize_mod_id(entry) for entry in mod_blacklist or () if entry.strip()
        )
        self.supported_extensions = BaseParser.get_supported_extensions()
        self.handler_registry = create_default_registry()
        self.max_scan_files = 1000000  # Safety limit to prevent OOM

        logger.info(
            "Initialized scanner: %s -> %s (%d blacklisted mods)",
            self.source_locale,
            self.target_locale,
            len(self.mod_blacklist),
        )

    async def scan(self, modpack_path: Path) -> ScanResult:
        """Scan a modpack for language files.

        Args:
            modpack_path: Path to the modpack root directory.

        Returns:
            Scan result with found files and pairs.
        """
        logger.info("Scanning modpack: %s", modpack_path)

        if not await asyncio.to_thread(modpack_path.exists):
            logger.error("Modpack path does not exist: %s", modpack_path)
            return ScanResult(modpack_path=modpack_path)

        result = ScanResult(
            modpack_path=modpack_path,
            source_locale=self.source_locale,
            target_locale=self.target_locale,
        )

        # Scan every known translation location in a fixed pass order
        self._report_progress(
            "ZIP 파일 추출 중...", 0, 8, "리소스팩 ZIP을 추출하고 있습니다..."
        )
        await self._extract_resource_pack_zips(modpack_path)

        self._report_progress(
            "Config 파일 스캔 중...", 1, 8, "config 폴더를 스캔하고 있습니다..."
        )
        await self._load_config_files(modpack_path, result)

        self._report_progress(
            "The Vault 퀘스트 스캔 중...",
            1,
            8,
            "The Vault 퀘스트 파일을 스캔하고 있습니다...",
        )
        await self._load_the_vault_quest_files(modpack_path, result)

        self._report_progress(
            "FTB Quests 스캔 중...", 2, 8, "FTB Quests 파일을 스캔하고 있습니다..."
        )
        await self._load_ftbquests_files(modpack_path, result)

        self._report_progress(
            "KubeJS 스캔 중...", 3, 8, "kubejs 폴더를 스캔하고 있습니다..."
        )
        await self._load_kubejs_files(modpack_path, result)

        self._report_progress(
            "Patchouli 스캔 중...", 4, 8, "patchouli 폴더를 스캔하고 있습니다..."
        )
        await self._load_patchouli_files(modpack_path, result)

        self._report_progress(
            "리소스팩 스캔 중...",
            5,
            8,
            "리소스팩·데이터팩을 스캔하고 있습니다...",
        )
        await self._load_resourcepack_files(modpack_path, result)
        await self._load_resources_overlay_files(modpack_path, result)

        self._report_progress(
            "JAR 파일 스캔 중...", 6, 8, "JAR 파일들을 처리하고 있습니다..."
        )
        await self._load_mod_files(modpack_path, result)

        self._report_progress(
            "리소스팩 오버라이드 확인 중...",
            7,
            8,
            "활성화된 리소스팩의 원문 오버라이드를 적용하고 있습니다...",
        )
        await self._apply_source_overrides(modpack_path, result)

        # Build file pairs from translation files
        self._build_file_pairs(result)

        self._report_progress(
            "스캔 완료!",
            8,
            8,
            f"총 {len(result.translation_files)}개 파일 발견",
        )

        logger.info(
            "Scan complete: %d source, %d target, %d paired, total files: %d",
            result.total_source_files,
            result.total_target_files,
            result.total_paired,
            len(result.translation_files),
        )

        return result

    def _report_progress(
        self, stage: str, current: int, total: int, detail: str
    ) -> None:
        """Report progress if callback is set."""
        if self.progress_callback:
            self.progress_callback(stage, current, total, detail)

    @staticmethod
    def _normalize_glob_path(path: Path) -> str:
        """Normalize glob pattern path."""
        path_str = str(path).replace("\\", "/")
        parts = []
        for part in path_str.split("/"):
            if part.startswith("**") or part.startswith("*"):
                parts.append(part)
            else:
                parts.append(glob_escape(part))
        return "/".join(parts)

    def _is_translation_file(self, file_path: str) -> bool:
        """Check if file is a translation candidate."""
        # Check if any handler can handle this file
        path = Path(file_path)
        if self.handler_registry.get_handler(path):
            return True

        return False

    @staticmethod
    def _safe_iglob_sync(pattern: str, recursive: bool = True) -> list[str]:
        """Collect glob results synchronously."""
        return list(iglob(pattern, recursive=recursive))

    async def _safe_iglob(self, pattern: str, recursive: bool = True) -> list[str]:
        """Safely iterate over glob results."""
        try:
            return await asyncio.to_thread(
                self._safe_iglob_sync, pattern, recursive
            )
        except (OSError, ValueError) as e:
            logger.error("Glob failed for pattern %s: %s", pattern, e)
            return []

    @staticmethod
    def _extract_resource_pack_sync(zip_path: str, extract_dir: str) -> None:
        """Extract a Minecraft resource pack ZIP into the given directory.

        Resource packs are user-installed asset overrides shipped as ZIP
        archives that the scanner cannot inspect without unpacking. We
        extract them into the modpack-local cache so the existing
        glob-based resource pack scan can pick up ``assets/<ns>/lang/*``
        entries inside.
        """
        stamp = _jar_stamp(zip_path)
        marker = Path(extract_dir) / _EXTRACT_STAMP_NAME
        try:
            if stamp and marker.read_text(encoding="utf-8") == stamp:
                logger.debug("Resource pack already extracted: %s", zip_path)
                return
        except OSError:
            pass
        # Keyed on the ZIP's size+mtime: an updated pack used to keep being
        # read from its first extraction for as long as the folder existed.
        # A stale tree is removed whole so a file the new ZIP dropped does
        # not linger as a phantom source.
        shutil.rmtree(extract_dir, ignore_errors=True)
        with zipfile.ZipFile(zip_path, "r") as zf:
            declared = sum(info.file_size for info in zf.infolist())
            if declared > MAX_RESOURCE_PACK_UNPACKED_BYTES:
                raise zipfile.BadZipFile(
                    f"uncompressed size {declared} bytes exceeds the "
                    f"{MAX_RESOURCE_PACK_UNPACKED_BYTES}-byte cap"
                )
            logger.info("Extracting resource pack ZIP: %s", zip_path)
            os.makedirs(extract_dir, exist_ok=True)
            zf.extractall(extract_dir)
        if stamp:
            marker.write_text(stamp, encoding="utf-8")

    @staticmethod
    def _prune_extractions(slug_root: Path, keep: set[str]) -> None:
        try:
            entries = list(slug_root.iterdir())
        except OSError:
            return
        for entry in entries:
            if entry.name not in keep and entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry, ignore_errors=True)

    async def _extract_resource_pack_zips(self, modpack_path: Path) -> None:
        """Unpack resource-pack ZIPs from every root a pack can be installed in.

        Covers ``resourcepacks/`` plus the folders OpenLoader and Paxi mount
        their own packs from. Contents land under
        ``.mct_cache/resourcepacks/<root-slug>/`` so two roots shipping the
        same file name cannot overwrite each other, and ``route_for`` sees a
        ``.mct_cache`` asset path and routes it into the resource pack.

        Data packs are deliberately left alone: the output router treats any
        ``.mct_cache`` path containing ``data/`` as a JAR-mod candidate and
        would skip the extracted files anyway.
        """
        cache_root = modpack_path / ".mct_cache" / "resourcepacks"

        for rel_root, file_type in PACK_ROOTS:
            if file_type != "resourcepacks":
                continue

            pattern = self._normalize_glob_path(modpack_path / rel_root / "*.zip")
            zip_files = await self._safe_iglob(str(pattern), recursive=False)
            slug = rel_root.replace("/", "_")
            # A ZIP removed from the pack must take its extraction with it,
            # or its strings keep being scanned as a live source.
            await asyncio.to_thread(
                self._prune_extractions,
                cache_root / slug,
                {os.path.basename(path) for path in zip_files},
            )
            if not zip_files:
                continue

            for zip_path in zip_files:
                extract_dir = cache_root / slug / os.path.basename(zip_path)
                try:
                    await asyncio.to_thread(
                        self._extract_resource_pack_sync,
                        zip_path,
                        str(extract_dir),
                    )
                except (zipfile.BadZipFile, OSError) as e:
                    logger.error(
                        "Failed to extract resource pack (%s): %s", zip_path, e
                    )

    async def _load_config_files(self, modpack_path: Path, result: ScanResult) -> None:
        """Load translation files from config folder (excluding ftbquests)."""
        pattern = self._normalize_glob_path(modpack_path / "config" / "**" / "*.*")
        logger.info("Scanning config files: %s", pattern)

        try:
            for file_path in await self._safe_iglob(str(pattern), recursive=True):
                if len(result.translation_files) >= self.max_scan_files:
                    logger.warning("Max file limit reached during config scan")
                    break

                try:
                    if "ftbquests" in file_path.lower():
                        continue
                    if (
                        "the_vault/quest" in file_path.lower()
                        or "the_vault\\quest" in file_path.lower()
                    ):
                        continue
                    if self._is_translation_file(file_path):
                        result.translation_files.append(
                            TranslationFile(
                                input_path=file_path,
                                file_type="config",
                                category="Configuration",
                            )
                        )
                except (OSError, ValueError, TypeError) as e:
                    logger.debug("Failed to process config file %s: %s", file_path, e)
        except (OSError, ValueError, TypeError) as e:
            logger.error("Config scan failed: %s", e)

        count = len([f for f in result.translation_files if f.file_type == "config"])
        logger.info("Found %d files in config folder", count)

    async def _load_ftbquests_files(
        self, modpack_path: Path, result: ScanResult
    ) -> None:
        """Load translation files from ftbquests folder."""
        search_paths = [modpack_path / "config" / "ftbquests"]
        # FTB Quests 26.1.2+ writes every quest and lang file as .json5,
        # with lang as a directory tree per locale (lang/en_us/chapters/x.json5)
        # that pairs with its target-locale mirror through the locale chunk.
        ftbquests_extensions = (".snbt", ".nbt", ".json5")

        for path in search_paths:
            if not await asyncio.to_thread(path.is_dir):
                continue
            pattern = self._normalize_glob_path(path / "**" / "*.*")
            logger.info("Scanning FTB Quests: %s", pattern)

            try:
                for file_path in await self._safe_iglob(str(pattern), recursive=True):
                    if len(result.translation_files) >= self.max_scan_files:
                        logger.warning("Max file limit reached during FTB scan")
                        break

                    try:
                        # Only accept quest-book formats for ftbquests
                        ext = os.path.splitext(file_path)[1].lower()
                        if ext in ftbquests_extensions and self._is_translation_file(
                            file_path
                        ):
                            result.translation_files.append(
                                TranslationFile(
                                    input_path=file_path,
                                    file_type="ftbquests",
                                    category="FTB Quests",
                                )
                            )
                    except (OSError, ValueError, TypeError) as e:
                        logger.debug("Failed to process FTB file %s: %s", file_path, e)
            except (OSError, ValueError, TypeError) as e:
                logger.error("FTB Quests scan failed: %s", e)

        count = len([f for f in result.translation_files if f.file_type == "ftbquests"])
        logger.info(
            "Found %d files in ftbquests folder (.snbt, .nbt, .json5)", count
        )

    async def _load_the_vault_quest_files(
        self, modpack_path: Path, result: ScanResult
    ) -> None:
        """Load translation files from the_vault/quest folder."""
        search_paths = [modpack_path / "config" / "the_vault" / "quest"]

        for path in search_paths:
            if not await asyncio.to_thread(path.is_dir):
                continue
            pattern = self._normalize_glob_path(path / "**" / "*.json")
            logger.info("Scanning The Vault Quests: %s", pattern)

            try:
                for file_path in await self._safe_iglob(str(pattern), recursive=True):
                    if len(result.translation_files) >= self.max_scan_files:
                        logger.warning(
                            "Max file limit reached during The Vault Quest scan"
                        )
                        break

                    try:
                        if self._is_translation_file(file_path):
                            result.translation_files.append(
                                TranslationFile(
                                    input_path=file_path,
                                    file_type="the_vault_quest",
                                    category="The Vault Quests",
                                )
                            )
                    except (OSError, ValueError, TypeError) as e:
                        logger.debug(
                            "Failed to process The Vault Quest file %s: %s",
                            file_path,
                            e,
                        )
            except (OSError, ValueError, TypeError) as e:
                logger.error("The Vault Quest scan failed: %s", e)

        count = len(
            [f for f in result.translation_files if f.file_type == "the_vault_quest"]
        )
        logger.info("Found %d files in the_vault/quest folder", count)

    async def _load_kubejs_files(self, modpack_path: Path, result: ScanResult) -> None:
        """Load translation files from kubejs folder."""
        pattern = self._normalize_glob_path(modpack_path / "kubejs" / "**" / "*.*")
        logger.info("Scanning KubeJS: %s", pattern)

        try:
            for file_path in await self._safe_iglob(str(pattern), recursive=True):
                if len(result.translation_files) >= self.max_scan_files:
                    logger.warning("Max file limit reached during KubeJS scan")
                    break

                try:
                    if self._is_translation_file(file_path):
                        result.translation_files.append(
                            TranslationFile(
                                input_path=file_path,
                                file_type="kubejs",
                                category="KubeJS",
                            )
                        )
                except (OSError, ValueError, TypeError) as e:
                    logger.debug("Failed to process KubeJS file %s: %s", file_path, e)
        except (OSError, ValueError, TypeError) as e:
            logger.error("KubeJS scan failed: %s", e)

        count = len([f for f in result.translation_files if f.file_type == "kubejs"])
        logger.info("Found %d files in kubejs folder", count)

    async def _load_patchouli_files(
        self, modpack_path: Path, result: ScanResult
    ) -> None:
        """Load translation files from patchouli_books folder."""
        pattern = self._normalize_glob_path(
            modpack_path / "patchouli_books" / "**" / "*.*"
        )
        logger.info("Scanning Patchouli: %s", pattern)

        try:
            for file_path in await self._safe_iglob(str(pattern), recursive=True):
                if len(result.translation_files) >= self.max_scan_files:
                    logger.warning("Max file limit reached during Patchouli scan")
                    break

                try:
                    if self._is_translation_file(file_path):
                        result.translation_files.append(
                            TranslationFile(
                                input_path=file_path,
                                file_type="patchouli",
                                category="Patchouli Books",
                            )
                        )
                except (OSError, ValueError, TypeError) as e:
                    logger.debug(
                        "Failed to process Patchouli file %s: %s", file_path, e
                    )
        except (OSError, ValueError, TypeError) as e:
            logger.error("Patchouli scan failed: %s", e)

        count = len([f for f in result.translation_files if f.file_type == "patchouli"])
        logger.info("Found %d files in patchouli folder", count)

    async def _load_resourcepack_files(
        self, modpack_path: Path, result: ScanResult
    ) -> None:
        """Load translation files from resource packs and data packs.

        Scans every root in :data:`PACK_ROOTS` for loose (already unzipped)
        packs, plus the ``.mct_cache/<file_type>/`` trees holding contents of
        the ZIPs :meth:`_extract_resource_pack_zips` unpacked.
        """
        scan_targets: list[tuple[str, Path]] = [
            (file_type, modpack_path / rel_root) for rel_root, file_type in PACK_ROOTS
        ]
        for file_type in ("resourcepacks", "datapacks"):
            scan_targets.append((file_type, modpack_path / ".mct_cache" / file_type))

        for folder, root in scan_targets:
            pattern = self._normalize_glob_path(root / "**" / "*.*")
            logger.info("Scanning %s (%s): %s", folder, root, pattern)

            try:
                for file_path in await self._safe_iglob(str(pattern), recursive=True):
                    if len(result.translation_files) >= self.max_scan_files:
                        logger.warning("Max file limit reached during %s scan", folder)
                        break

                    try:
                        if self._is_translation_file(file_path):
                            result.translation_files.append(
                                TranslationFile(
                                    input_path=file_path,
                                    file_type=folder,
                                    category="Resource/Data Packs",
                                )
                            )
                    except (OSError, ValueError, TypeError) as e:
                        logger.debug(
                            "Failed to process resource pack file %s: %s", file_path, e
                        )
            except (OSError, ValueError, TypeError) as e:
                logger.error("%s scan failed: %s", folder, e)

        count = len(
            [
                f
                for f in result.translation_files
                if f.file_type in ["resourcepacks", "datapacks"]
            ]
        )
        logger.info("Found %d files in resource packs/datapacks", count)

    async def _load_resources_overlay_files(
        self, modpack_path: Path, result: ScanResult
    ) -> None:
        """Load translation files from the ``resources/`` overlay folder.

        The 1.12.x launcher convention (Twitch/CurseForge, ATLauncher) auto-
        mounts ``<modpack>/resources/<namespace>/<asset_type>/...`` as a
        default-loaded resource pack overlay, so language files like
        ``resources/betterquesting/lang/en_us.lang`` are translatable but
        live OUTSIDE both ``resourcepacks/`` and the standard ``assets/``
        layout. This stage picks them up.
        """
        root = modpack_path / "resources"
        if not await asyncio.to_thread(root.is_dir):
            return

        pattern = self._normalize_glob_path(root / "**" / "*.*")
        logger.info("Scanning resources overlay: %s", pattern)

        try:
            for file_path in await self._safe_iglob(str(pattern), recursive=True):
                if len(result.translation_files) >= self.max_scan_files:
                    logger.warning(
                        "Max file limit reached during resources overlay scan"
                    )
                    break

                try:
                    if self._is_translation_file(file_path):
                        result.translation_files.append(
                            TranslationFile(
                                input_path=file_path,
                                file_type="resources",
                                category="Resources Overlay",
                            )
                        )
                except (OSError, ValueError, TypeError) as e:
                    logger.debug(
                        "Failed to process resources file %s: %s", file_path, e
                    )
        except (OSError, ValueError, TypeError) as e:
            logger.error("Resources overlay scan failed: %s", e)

        count = len(
            [f for f in result.translation_files if f.file_type == "resources"]
        )
        logger.info("Found %d files in resources folder", count)

    async def _load_mod_files(self, modpack_path: Path, result: ScanResult) -> None:
        """Load translation files from mods JAR files."""
        pattern = self._normalize_glob_path(modpack_path / "mods" / "*.jar")
        logger.info("Scanning Mods: %s", pattern)

        try:
            jar_files = await self._safe_iglob(str(pattern))
            total_jars = len(jar_files)
            jar_files_found = total_jars
            done = 0
            # JARs are independent and the work is zip inflation plus file
            # writes, both of which release the GIL: one JAR at a time left
            # a 300-mod pack's scan bound to a single core.
            gate = asyncio.Semaphore(_JAR_SCAN_WORKERS)

            async def scan_one(jar_path: str) -> ScanResult:
                nonlocal done
                # Each JAR fills its own partial result, merged below in
                # glob order, so the outcome never depends on which thread
                # finished first.
                partial = ScanResult(modpack_path=modpack_path)
                async with gate:
                    try:
                        await self._extract_from_jar(modpack_path, jar_path, partial)
                    except (zipfile.BadZipFile, OSError) as e:
                        logger.error("Failed to process JAR file (%s): %s", jar_path, e)
                done += 1
                if self.progress_callback:
                    self._report_progress(
                        "JAR 파일 스캔 중...",
                        6,
                        7,
                        f"JAR 파일 처리 중 ({done}/{total_jars}): "
                        f"{os.path.basename(jar_path)}",
                    )
                return partial

            for partial in await asyncio.gather(*(scan_one(p) for p in jar_files)):
                result.translation_files.extend(partial.translation_files)
                result.excluded_mods.extend(partial.excluded_mods)
                result.hardcoded_mods.extend(partial.hardcoded_mods)

            count = len([f for f in result.translation_files if f.file_type == "mod"])
            logger.info(
                "Scanned %d JAR files in mods folder, found %d translation files "
                "(%d excluded by blacklist)",
                jar_files_found,
                count,
                len(result.excluded_mods),
            )
        except (OSError, ValueError, TypeError) as e:
            logger.error("Mod scan failed: %s", e)

    def _blacklisted_id(self, zf: zipfile.ZipFile) -> str | None:
        """The blacklist entry this JAR matches, or None.

        Matched against the ids the JAR declares; a JAR that declares none
        falls back to the ``assets/<namespace>`` names, which is the
        identifier the rest of the scanner already keys its files by.
        """
        if not self.mod_blacklist:
            return None
        candidates = declared_mod_ids(zf) or asset_namespaces(zf)
        for candidate in sorted(candidates):
            if normalize_mod_id(candidate) in self.mod_blacklist:
                return candidate
        return None

    def _extract_from_jar_sync(
        self, modpack_path: Path, jar_path: str, result: ScanResult
    ) -> None:
        """Extract translation files from a JAR file."""
        jar_name = os.path.basename(jar_path)
        mod_display_name = Path(jar_name).stem.split("-")[0].replace("_", " ").title()

        with zipfile.ZipFile(jar_path, "r") as zf:
            # Blacklisted mods are dropped before extraction, so their
            # entries never exist on disk and cannot be counted, billed or
            # translated. Nothing the pack already ships is touched: a mod
            # JAR is never rewritten, and only *fresh* translations of its
            # keys would have been generated in the first place.
            blacklisted = self._blacklisted_id(zf)
            if blacklisted is not None:
                logger.info(
                    "Skipping blacklisted mod %s (%s)", blacklisted, jar_name
                )
                result.excluded_mods.append(
                    ExcludedMod(mod_id=blacklisted, jar_name=jar_name)
                )
                return

            # Use a hidden cache directory instead of mods/extracted
            # This prevents extracted files from being treated as part of the modpack structure
            extract_dir = modpack_path / ".mct_cache" / "extracted" / jar_name
            extract_dir.mkdir(parents=True, exist_ok=True)

            for entry in zf.namelist():
                if self._should_extract_from_jar(entry):
                    try:
                        zf.extract(entry, extract_dir)
                        extracted_path = extract_dir / entry

                        if extracted_path.is_file() and self._is_translation_file(
                            str(extracted_path)
                        ):
                            result.translation_files.append(
                                TranslationFile(
                                    input_path=str(extracted_path),
                                    file_type="mod",
                                    jar_name=jar_name,
                                    category=f"Mod: {mod_display_name}",
                                )
                            )
                    except (zipfile.BadZipFile, OSError, KeyError) as e:
                        logger.debug("Failed to extract file from JAR (%s): %s", entry, e)

            # Same open handle: the class pass costs one extra read of the
            # entries already in this jar and never touches the disk, so
            # `.class` files are decoded in memory and discarded rather
            # than joining the thousands of files extracted above.
            self._collect_hardcoded(modpack_path, jar_path, jar_name, zf, result)

    def _collect_hardcoded(
        self,
        modpack_path: Path,
        jar_path: str,
        jar_name: str,
        zf: zipfile.ZipFile,
        result: ScanResult,
    ) -> None:
        """Record display text this JAR builds in Java, cache the verdict.

        Reading every constant pool in a large pack costs seconds, and the
        blacklist band restarts the scan on every edit, so the verdict is
        memoized against the JAR's size and mtime. A rebuilt or swapped
        JAR misses the cache and is re-read; an untouched one never is.
        """
        cache = modpack_path / ".mct_cache" / "hardcoded" / f"{jar_name}.json"
        stamp = _jar_stamp(jar_path)
        restored = _load_hardcoded_cache(cache, stamp) if stamp else None
        if restored is not None:
            if restored.strings:
                result.hardcoded_mods.append(restored)
            return

        mod_id = next(iter(sorted(declared_mod_ids(zf))), "") or next(
            iter(sorted(asset_namespaces(zf))), Path(jar_name).stem
        )
        try:
            finding = find_hardcoded_strings(
                zf,
                jar_name=jar_name,
                mod_id=mod_id,
                source_locale=self.source_locale,
            )
        except (zipfile.BadZipFile, OSError, ValueError) as exc:
            # A jar we cannot read here is still perfectly translatable
            # through its lang files; the finding is an extra, never a
            # precondition, so a failure must not fail the scan.
            logger.warning("Hardcoded-text scan failed for %s: %s", jar_name, exc)
            return

        if finding is not None:
            result.hardcoded_mods.append(finding)
        if stamp:
            _store_hardcoded_cache(cache, stamp, mod_id, jar_name, finding)

    async def _extract_from_jar(
        self, modpack_path: Path, jar_path: str, result: ScanResult
    ) -> None:
        """Extract translation files from a JAR file."""
        await asyncio.to_thread(self._extract_from_jar_sync, modpack_path, jar_path, result)

    def _should_extract_from_jar(self, entry_path: str) -> bool:
        """Check if JAR entry should be extracted."""
        entry_lower = entry_path.lower()
        ext = os.path.splitext(entry_path)[1].lower()

        if ext not in self.supported_extensions:
            return False

        # Exclude non-translatable directories
        excluded_dirs = [
            "/recipes/",
            "/tags/",
            "/loot_tables/",
            "/advancements/",
            "/structures/",
            "/worldgen/",
            "/dimension/",
            "/dimension_type/",
            "\\recipes\\",
            "\\tags\\",
            "\\loot_tables\\",
            "\\advancements\\",
            "\\structures\\",
            "\\worldgen\\",
            "\\dimension\\",
            "\\dimension_type\\",
        ]

        for excluded in excluded_dirs:
            if excluded in entry_lower:
                return False
        # 1.21 renamed these data folders to the singular. Matched only
        # directly under data/<namespace>/, since a bare "/recipe/" also
        # names, say, a Patchouli book category that does hold text.
        if _SINGULAR_DATA_DIR_RE.search(entry_lower.replace("\\", "/")):
            return False

        return any(d.lower() in entry_lower for d in DIR_FILTER_WHITELIST)

    async def _apply_source_overrides(
        self, modpack_path: Path, result: ScanResult
    ) -> None:
        """Substitute enabled resource packs' strings for the mods' own.

        A modpack that renames items with a bundled resource pack ships
        one string in the mod and shows another in game; translating the
        mod's string translates text no player reads. The packs
        ``options.txt`` enables are the effective source, so their entries
        replace the values in the mod JAR copies this scan extracted
        (:meth:`_extract_from_jar_sync` re-extracts them on every scan, so
        the substitution starts from pristine mod content and never
        compounds). Keys, files and therefore output routing are
        untouched — only values change.

        User-authored trees (loose packs, ``resources/``, ``kubejs/``) are
        deliberately not rewritten; an overriding pack's own language file
        is already scanned and already carries the winning string.
        """
        overrides, packs = await asyncio.to_thread(
            resource_overrides.collect_overrides, modpack_path, self.source_locale
        )
        if not overrides:
            return

        applied: Counter[str] = Counter()
        for tf in result.translation_files:
            if tf.file_type != "mod":
                continue
            namespace = self._lang_namespace(Path(tf.input_path))
            entries = overrides.get(namespace) if namespace else None
            if not entries:
                continue
            applied += await resource_overrides.apply_overrides(
                Path(tf.input_path), entries
            )

        result.source_overrides = [
            SourceOverride(pack=pack.name, keys=applied[pack.name])
            for pack in packs
            if applied[pack.name]
        ]
        logger.info(
            "Resource-pack source overrides applied: %d keys from %s",
            sum(applied.values()),
            ", ".join(o.pack for o in result.source_overrides) or "no pack",
        )

    def _lang_namespace(self, path: Path) -> str | None:
        """Namespace of an ``assets/<ns>/lang/<source_locale>.*`` file."""
        parts = path.parts
        if len(parts) < 4 or parts[-2] != "lang":
            return None
        if path.stem.lower() != self.source_locale:
            return None
        if parts[-4] != "assets":
            return None
        return parts[-3]

    def _build_file_pairs(self, result: ScanResult) -> None:
        """Build file pairs from translation files."""
        source_files: dict[str, TranslationFile] = {}
        target_files: dict[str, TranslationFile] = {}

        # First pass: detect all locale codes in the files
        detected_locales: set[str] = set()
        locale_pattern = re.compile(r"[/\\]([a-z]{2}_[a-z]{2})(?:[/\\.]|$)")

        for tf in result.translation_files:
            path_lower = tf.input_path.replace("\\", "/").lower()
            matches = locale_pattern.findall(path_lower)
            detected_locales.update(matches)

        # Remove source and target locales from detected locales
        other_locales = detected_locales - {self.source_locale, self.target_locale}

        if other_locales:
            logger.info(
                "Detected other locales to filter out: %s",
                ", ".join(sorted(list[str](set[str](other_locales)))),
            )

        # Categorize by language
        for tf in result.translation_files:
            base_path = self._get_base_path(tf.input_path)
            path_lower = tf.input_path.replace("\\", "/").lower()

            # Skip files with other locale codes
            if any(
                self._path_has_locale(path_lower, locale)
                for locale in other_locales
            ):
                logger.debug("Skipping other locale file: %s", tf.input_path)
                continue

            if self._path_has_locale(path_lower, self.source_locale):
                tf.lang_type = "source"
                source_files[base_path] = tf
            elif self._path_has_locale(path_lower, self.target_locale):
                tf.lang_type = "target"
                target_files[base_path] = tf
            else:
                # Default to source for files without locale in path
                # (e.g., config files without lang code)
                tf.lang_type = "source"
                source_files[base_path] = tf

        result.total_source_files = len(source_files)
        result.total_target_files = len(target_files)

        # Build pairs
        matched_targets: set[str] = set()

        for base_path, source_tf in source_files.items():
            source_path = Path(source_tf.input_path)
            namespace, mod_id = self._extract_namespace(source_path, source_tf)

            target_path: Path | None = None
            if base_path in target_files:
                target_path = Path(target_files[base_path].input_path)
                matched_targets.add(base_path)
                result.total_paired += 1

            pair = LanguageFilePair(
                source_path=source_path,
                target_path=target_path,
                namespace=namespace,
                mod_id=mod_id,
            )

            if target_path:
                result.paired_files.append(pair)
            else:
                result.source_only_files.append(pair)

        # Target-only files
        for base_path, target_tf in target_files.items():
            if base_path not in matched_targets:
                result.target_only_files.append(Path(target_tf.input_path))

    @classmethod
    def _path_has_locale(cls, path_lower: str, locale: str) -> bool:
        """True when ``locale`` appears in the path at a locale boundary."""
        return any(
            match.group() == locale
            for match in LOCALE_CHUNK_RE.finditer(path_lower)
        )

    def _get_base_path(self, file_path: str) -> str:
        """Get base path without locale for matching."""
        path_normalized = file_path.replace("\\", "/").lower()
        # Replace boundary-anchored locale codes so source/target paths of
        # the same file collapse to one matching key.
        return LOCALE_CHUNK_RE.sub("LOCALE", path_normalized)

    def _extract_namespace(
        self, file_path: Path, tf: TranslationFile
    ) -> tuple[str, str]:
        """Extract namespace and mod ID from a file path."""
        parts = file_path.parts
        mod_name = None

        if tf.file_type == "mod" and tf.jar_name:
            mod_name = self._clean_mod_name(tf.jar_name)

        try:
            assets_idx = parts.index("assets")
            if assets_idx + 1 < len(parts):
                namespace = parts[assets_idx + 1]
                return namespace, (mod_name if mod_name else namespace)
        except ValueError:
            pass

        if tf.file_type == "resources":
            try:
                resources_idx = parts.index("resources")
                if resources_idx + 1 < len(parts):
                    namespace = parts[resources_idx + 1]
                    return namespace, namespace
            except ValueError:
                pass

        if mod_name:
            return mod_name, mod_name

        return tf.file_type, tf.file_type

    def _clean_mod_name(self, jar_name: str) -> str:
        """Extract clean mod name from jar filename."""
        name = Path(jar_name).stem

        # Simple regex to strip version numbers and loaders
        # Matches - or _ followed by (forge, fabric, quilt, neoforge, mc, v digit, or digit)
        # and everything after
        pattern = r"[-_](?:forge|fabric|quilt|neoforge|mc|v?\d).*"
        clean_name = re.sub(pattern, "", name, flags=re.IGNORECASE)

        return clean_name


async def scan_modpack(
    modpack_path: Path | str,
    source_locale: str = "en_us",
    target_locale: str = "ko_kr",
    progress_callback: ScanProgressCallback | None = None,
    mod_blacklist: Iterable[str] | None = None,
) -> ScanResult:
    """Convenience function to scan a modpack.

    Args:
        modpack_path: Path to the modpack directory.
        source_locale: Source language locale.
        target_locale: Target language locale.
        progress_callback: Optional progress callback.
        mod_blacklist: Mod ids whose JARs are left out of the scan.

    Returns:
        Scan result.
    """
    scanner = ModpackScanner(
        source_locale, target_locale, progress_callback, mod_blacklist
    )
    return await scanner.scan(Path(modpack_path))
