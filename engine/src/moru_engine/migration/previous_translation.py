"""Run-scoped A/B/C migration for a previous modpack translation.

``A`` is the old original modpack, ``B`` is its translated resource pack
and/or overrides archive, and ``C`` is the current pipeline input.  This
module indexes A and B once, then lets the normal pipeline reuse B only for
entries whose source text is byte-for-byte unchanged between A and C.

The catalog deliberately does not validate pack names or versions.  Wrong
inputs merely produce no exact matches.  It is separate from the global TM so
translations with pack-specific context never leak into unrelated runs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import stat
import tempfile
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from ..glossary.pair_harvester import is_untranslated_copy
from ..handlers.base import HandlerRegistry, create_default_registry
from ..modsources import _current_archive_files
from ..scanner import ModpackScanner
from ..scanner.modpack_scanner import LOCALE_CHUNK_RE

if TYPE_CHECKING:
    from ..models import LanguageFilePair
    from ..scanner import ScanResult

logger = logging.getLogger(__name__)

# Detection stays deliberately broad: a false locale only skips one B file,
# while a missed one would index another language as the target translation.
_LOCALE_TOKEN_RE = re.compile(r"(?i)(?<![a-z])([a-z]{2}_[a-z]{2})(?![a-z])")
# Identity normalization instead reuses the scanner's locale-chunk boundary,
# narrowed to Minecraft's shipped codes so an ordinary name like "to_do.json"
# cannot collapse onto a sibling "en_us.json"/"ko_kr.json" and steal its reuse.
_LOCALE_CODES = frozenset(
    "af_za ar_sa az_az ba_ru be_by bg_bg br_fr bs_ba ca_es cs_cz cy_gb da_dk "
    "de_at de_ch de_de el_gr en_au en_ca en_gb en_nz en_pt en_ud en_us eo_uy "
    "es_ar es_cl es_ec es_es es_mx es_uy es_ve et_ee eu_es fa_ir fi_fi fo_fo "
    "fr_ca fr_fr fy_nl ga_ie gd_gb gl_es he_il hi_in hn_no hr_hr hu_hu hy_am "
    "id_id ig_ng io_en is_is it_it ja_jp ka_ge kk_kz kn_in ko_kr kw_gb la_la "
    "lb_lu li_li lo_la lt_lt lv_lv mk_mk mn_mn ms_my mt_mt nl_be nl_nl nn_no "
    "no_no oc_fr pl_pl pt_br pt_pt ro_ro ru_ru ry_ua se_no sk_sk sl_si so_so "
    "sq_al sr_cs sr_sp sv_se ta_in th_th tl_ph tr_tr tt_ru uk_ua vi_vn vp_vl "
    "yi_de yo_ng zh_cn zh_hk zh_tw".split()
)
_ARCHIVE_SUFFIXES = {".zip", ".jar"}
_MAX_ARCHIVE_FILES = 1_000_000
_MAX_ARCHIVE_BYTES = 20 * 1024 * 1024 * 1024
_FONT_FILE_SUFFIXES = {".otf", ".ttc", ".ttf", ".woff", ".woff2"}


class MigrationError(ValueError):
    """A previous-version migration input cannot be used safely."""


@dataclass(slots=True)
class MigrationStats:
    """Index and lookup counters surfaced in logs/tests."""

    old_source_entries: int = 0
    previous_translation_entries: int = 0
    ambiguous_entries: int = 0
    preserved_resourcepack_assets: int = 0
    reused_entries: int = 0


@dataclass(slots=True)
class MigrationCatalog:
    """Exact A-source and B-translation indexes for one pipeline run."""

    old_sources: dict[str, dict[str, str]] = field(default_factory=dict)
    previous_translations: dict[str, dict[str, str]] = field(default_factory=dict)
    ambiguous: set[tuple[str, str]] = field(default_factory=set)
    resourcepack_assets_dir: Path | None = None
    stats: MigrationStats = field(default_factory=MigrationStats)

    def match(
        self,
        logical_file: str,
        entry_key: str,
        current_source: str,
    ) -> str | None:
        """Return B iff A and C contain the exact same keyed source text."""
        coordinate = (logical_file, entry_key)
        if coordinate in self.ambiguous:
            return None
        old_source = self.old_sources.get(logical_file, {}).get(entry_key)
        if old_source != current_source:
            return None
        translated = self.previous_translations.get(logical_file, {}).get(entry_key)
        if translated is None or is_untranslated_copy(old_source, translated):
            return None
        return translated

    def lookup(
        self,
        logical_file: str,
        entry_key: str,
        current_source: str,
    ) -> str | None:
        """Count and return one actual pipeline reuse."""
        translated = self.match(logical_file, entry_key, current_source)
        if translated is not None:
            self.stats.reused_entries += 1
        return translated


def _locale_placeholder(match: re.Match[str]) -> str:
    token = match.group()
    return "{locale}" if token in _LOCALE_CODES else token


def _normalized_locale_path(value: str) -> str:
    normalized = value.replace("\\", "/").strip("/").lower()
    return LOCALE_CHUNK_RE.sub(_locale_placeholder, normalized)


_OVERRIDE_ROOTS = {"config", "kubejs", "scripts", "patchouli_books"}


def logical_file_id(
    path: Path,
    modpack_root: Path,
    *,
    channel: str | None = None,
) -> str:
    """Stable output-channel/file identity shared by A, B, and C.

    Archive/cache wrapper names and mod JAR versions disappear at the
    ``assets/`` boundary.  Root overrides keep their modpack-relative path.
    Locale tokens are normalized so ``en_us`` and ``ko_kr`` pair naturally.
    """
    try:
        relative = path.resolve().relative_to(modpack_root.resolve()).as_posix()
    except ValueError:
        relative = path.name
    while relative.lower().startswith("overrides/"):
        relative = relative[len("overrides/") :]

    first = relative.replace("\\", "/").split("/", 1)[0].lower()
    inferred_override = first in _OVERRIDE_ROOTS
    raw = path.as_posix()
    lower = raw.lower()
    extracted_archive = ".zip_extracted/" in lower or "/.mct_cache/" in lower
    if channel == "override" or (
        channel is None and inferred_override and not extracted_archive
    ):
        return f"override:{_normalized_locale_path(relative)}"

    for marker in ("/assets/", "/resources/"):
        index = lower.rfind(marker)
        if index >= 0:
            relative = raw[index + len(marker) :]
            return f"resource:{_normalized_locale_path(relative)}"

    return f"override:{_normalized_locale_path(relative)}"


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0xFFFF
    return stat.S_ISLNK(mode)


def _validate_archive_members(
    archive: Path,
    infos: list[zipfile.ZipInfo],
) -> None:
    if len(infos) > _MAX_ARCHIVE_FILES:
        raise MigrationError(f"archive has too many entries: {archive}")
    total = sum(info.file_size for info in infos)
    if total > _MAX_ARCHIVE_BYTES:
        raise MigrationError(f"archive is too large to extract: {archive}")
    for info in infos:
        member = PurePosixPath(info.filename.replace("\\", "/"))
        if (
            member.is_absolute()
            or ".." in member.parts
            or any(":" in part for part in member.parts)
            or _is_symlink(info)
        ):
            raise MigrationError(
                f"archive contains an unsafe entry: {archive} -> {info.filename}"
            )


def safe_extract_zip(archive: Path, destination: Path) -> Path:
    """Extract an untrusted local ZIP without traversal or link entries."""
    destination.mkdir(parents=True, exist_ok=True)
    resolved_destination = destination.resolve()
    try:
        with zipfile.ZipFile(archive) as zf:
            infos = zf.infolist()
            _validate_archive_members(archive, infos)
            for info in infos:
                member = PurePosixPath(info.filename.replace("\\", "/"))
                target = destination.joinpath(*member.parts)
                resolved_target = target.resolve()
                try:
                    resolved_target.relative_to(resolved_destination)
                except ValueError as exc:
                    raise MigrationError(
                        f"archive entry escapes its destination: {info.filename}"
                    ) from exc
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
    except zipfile.BadZipFile as exc:
        raise MigrationError(f"invalid ZIP archive: {archive}") from exc
    return destination


def _validate_nested_scanner_archives(root: Path) -> None:
    """Validate archives that the existing scanner may extract from ZIP A.

    A folder follows the scanner's established trust boundary. A newly
    supported outer ZIP, however, must not smuggle a traversal entry inside a
    nested mod/resource-pack archive after the outer archive passed validation.
    """
    candidates: set[Path] = set()
    mods = root / "mods"
    if mods.is_dir():
        candidates.update(path for path in mods.rglob("*") if path.suffix.lower() == ".jar")
    resourcepacks = root / "resourcepacks"
    if resourcepacks.is_dir():
        candidates.update(
            path
            for path in resourcepacks.iterdir()
            if path.is_file() and path.suffix.lower() == ".zip"
        )
    candidates.update(
        path
        for path in root.rglob("*.zip")
        if "paxi" in path.as_posix().lower()
        or "openloader" in path.as_posix().lower()
    )

    for archive in candidates:
        try:
            with zipfile.ZipFile(archive) as zf:
                _validate_archive_members(archive, zf.infolist())
        except zipfile.BadZipFile:
            # The scanner already isolates corrupt archives. Only a valid
            # archive with an unsafe member needs to abort migration.
            continue


def _materialize_input(path: Path, destination: Path, label: str) -> Path:
    if not path.exists():
        raise MigrationError(f"{label} does not exist: {path}")
    if path.is_dir():
        return path
    if path.is_file() and path.suffix.lower() == ".zip":
        return safe_extract_zip(path, destination)
    raise MigrationError(f"{label} must be a folder or ZIP file: {path}")


def _single_wrapper(root: Path) -> Path:
    children = [child for child in root.iterdir() if child.name != "__MACOSX"]
    if len(children) == 1 and children[0].is_dir():
        return children[0]
    return root


_MODPACK_ROOT_NAMES = {
    "mods",
    "config",
    "kubejs",
    "resourcepacks",
    "datapacks",
    "resources",
    "scripts",
    "patchouli_books",
}


def _has_named_child(root: Path, names: set[str]) -> bool:
    try:
        return any(child.name.lower() in names for child in root.iterdir())
    except OSError:
        return False


def _old_modpack_root(root: Path) -> tuple[Path, Path]:
    """Return (scanner root, metadata root), including CurseForge exports."""
    candidate = root
    if not (
        _has_named_child(candidate, _MODPACK_ROOT_NAMES)
        or (candidate / "manifest.json").is_file()
        or (candidate / "minecraftinstance.json").is_file()
    ):
        candidate = _single_wrapper(candidate)
    if (candidate / "overrides").is_dir() and (candidate / "manifest.json").is_file():
        return candidate / "overrides", candidate
    return candidate, candidate


def _resourcepack_root(root: Path) -> Path:
    candidate = root
    if (candidate / "resourcepack").is_dir():
        candidate = candidate / "resourcepack"
    elif not (
        (candidate / "assets").is_dir()
        or (candidate / "pack.mcmeta").is_file()
    ):
        candidate = _single_wrapper(candidate)
    if (candidate / "assets").is_dir() or (candidate / "pack.mcmeta").is_file():
        return candidate
    matches = sorted(
        {path.parent for path in candidate.rglob("pack.mcmeta")}
        | {path.parent for path in candidate.rglob("assets") if path.is_dir()}
    )
    if len(matches) == 1:
        return matches[0]
    raise MigrationError(f"cannot find one resource-pack root under: {root}")


def _overrides_root(root: Path) -> Path:
    candidate = root
    if (candidate / "overrides").is_dir():
        return candidate / "overrides"
    if not _has_named_child(candidate, _MODPACK_ROOT_NAMES):
        candidate = _single_wrapper(candidate)
        if (candidate / "overrides").is_dir():
            return candidate / "overrides"
    return candidate


def _merge_entries(
    destination: dict[str, dict[str, str]],
    ambiguous: set[tuple[str, str]],
    logical_file: str,
    entries: Mapping[str, str],
) -> None:
    bucket = destination.setdefault(logical_file, {})
    for key, value in entries.items():
        coordinate = (logical_file, key)
        if coordinate in ambiguous:
            continue
        previous = bucket.get(key)
        if previous is not None and previous != value:
            bucket.pop(key, None)
            ambiguous.add(coordinate)
            continue
        bucket[key] = value


async def _extract_entries(
    registry: HandlerRegistry,
    path: Path,
    *,
    handler_path: Path | None = None,
) -> dict[str, str]:
    handler = registry.get_handler(handler_path or path)
    if handler is None:
        return {}
    try:
        return dict(await handler.extract(path))
    except Exception:  # noqa: BLE001 - one malformed legacy file is isolated
        logger.warning("Previous translation parse failed for %s", path, exc_info=True)
        return {}


async def _index_scan_sources(
    scan: ScanResult,
    registry: HandlerRegistry,
    destination: dict[str, dict[str, str]],
    ambiguous: set[tuple[str, str]],
) -> None:
    async def one(pair: LanguageFilePair) -> None:
        entries = await _extract_entries(registry, pair.source_path)
        if entries:
            _merge_entries(
                destination,
                ambiguous,
                logical_file_id(pair.source_path, scan.modpack_path),
                entries,
            )

    await asyncio.gather(*(one(pair) for pair in scan.all_translation_pairs))


async def _index_tree(
    root: Path,
    logical_root: Path,
    registry: HandlerRegistry,
    destination: dict[str, dict[str, str]],
    ambiguous: set[tuple[str, str]],
    archive_scratch: Path,
    channel: str,
    target_locale: str,
    resourcepack_assets_dir: Path | None = None,
) -> None:
    """Index translated files plus translated files embedded in patch JARs."""
    files = sorted(path for path in root.rglob("*") if path.is_file())

    async def ordinary(
        path: Path,
        root_for_id: Path,
        file_channel: str = channel,
    ) -> None:
        handler_path = Path(
            re.sub(
                re.escape(target_locale),
                "en_us",
                str(path),
                flags=re.IGNORECASE,
            )
        )
        handler = registry.get_handler(handler_path)
        if handler is None:
            return
        try:
            locale_path = path.resolve().relative_to(root_for_id.resolve()).as_posix()
        except ValueError:
            locale_path = path.name
        locales = {
            token.lower() for token in _LOCALE_TOKEN_RE.findall(locale_path.lower())
        }
        if locales and target_locale.lower() not in locales:
            return
        entries = await _extract_entries(
            registry,
            path,
            handler_path=handler_path,
        )
        if not entries:
            return
        _merge_entries(
            destination,
            ambiguous,
            logical_file_id(path, root_for_id, channel=file_channel),
            entries,
        )

    ordinary_files = [
        path for path in files if path.suffix.lower() not in _ARCHIVE_SUFFIXES
    ]
    await asyncio.gather(*(ordinary(path, logical_root) for path in ordinary_files))

    # Overrides sometimes ship tiny i18n helper JARs and Paxi/OpenLoader
    # resource-pack ZIPs.  Index their translation-bearing files, but never
    # copy the old archive into C.
    for index, archive in enumerate(
        path for path in files if path.suffix.lower() in _ARCHIVE_SUFFIXES
    ):
        extract_dir = archive_scratch / f"archive-{index}"
        try:
            safe_extract_zip(archive, extract_dir)
        except MigrationError:
            logger.warning("Skipping invalid previous patch archive: %s", archive)
            continue
        embedded = [path for path in extract_dir.rglob("*") if path.is_file()]
        await asyncio.gather(
            *(
                ordinary(
                    path,
                    extract_dir,
                    "resource" if "/assets/" in path.as_posix().lower() else channel,
                )
                for path in embedded
            )
        )
        if (
            resourcepack_assets_dir is not None
            and _is_embedded_resourcepack_archive(archive, extract_dir)
        ):
            _copy_embedded_resourcepack_assets(
                extract_dir,
                resourcepack_assets_dir,
            )
        # Strings are indexed and fonts copied within this iteration. An
        # overrides tree can ship every bundled mod JAR, and the per-archive
        # size cap leaves the extractions unbounded in aggregate.
        shutil.rmtree(extract_dir, ignore_errors=True)


def _copy_resourcepack_assets(
    root: Path,
    destination: Path,
) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=True)
    for source in root.rglob("*"):
        if not source.is_file() or source.is_symlink():
            continue
        relative = source.relative_to(root)
        if not _is_font_resource(relative):
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _copy_embedded_resourcepack_assets(
    archive_root: Path,
    destination: Path,
) -> None:
    """Merge font-related ``assets/`` files from a patch archive.

    Previous overrides commonly contain Paxi/OpenLoader resource-pack ZIPs.
    Their wrapper/config must not replace C. Only font definitions, font files,
    and font textures are carried into Moru's resource-pack output by default.
    Translation JARs are indexed for strings only and never copied as an asset
    source.
    """
    asset_roots = sorted(
        path
        for path in archive_root.rglob("assets")
        if path.is_dir() and not path.is_symlink()
    )
    for asset_root in asset_roots:
        for source in asset_root.rglob("*"):
            if not source.is_file() or source.is_symlink():
                continue
            relative = Path("assets") / source.relative_to(asset_root)
            if not _is_font_resource(relative):
                continue
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


def _is_font_resource(relative: Path) -> bool:
    """Whether an ``assets/<namespace>/...`` path is a font dependency.

    Minecraft font providers conventionally live below ``font/`` and bitmap
    glyph sheets below ``textures/font/``. Font binaries may be referenced from
    another resource path, so their distinctive extensions are also accepted.
    """
    parts = tuple(part.casefold() for part in relative.parts)
    try:
        assets_index = parts.index("assets")
    except ValueError:
        return False
    resource_parts = parts[assets_index + 2 :]
    if not resource_parts:
        return False
    if resource_parts[0] == "font":
        return True
    if len(resource_parts) >= 2 and resource_parts[:2] == ("textures", "font"):
        return True
    return relative.suffix.casefold() in _FONT_FILE_SUFFIXES


def _is_embedded_resourcepack_archive(archive: Path, extracted: Path) -> bool:
    if archive.suffix.lower() != ".zip":
        return False
    normalized = archive.as_posix().lower()
    return (
        "paxi" in normalized
        or "openloader" in normalized
        or "/resourcepacks/" in normalized
        or (extracted / "pack.mcmeta").is_file()
    )


def _curseforge_files(metadata_root: Path) -> dict[int, int]:
    candidates = [metadata_root / "manifest.json", metadata_root / "minecraftinstance.json"]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError, UnicodeError):
            continue
        manifest = payload.get("manifest", payload) if isinstance(payload, dict) else {}
        files = manifest.get("files", []) if isinstance(manifest, dict) else []
        result: dict[int, int] = {}
        for item in files:
            if not isinstance(item, dict):
                continue
            project = item.get("projectID", item.get("projectId"))
            file_id = item.get("fileID", item.get("fileId"))
            if isinstance(project, int) and isinstance(file_id, int):
                result[project] = file_id
        if result:
            return result
    return {}




def _source_archive_name(path: Path) -> str | None:
    """Return the JAR/resource-pack ZIP wrapper used by the C scanner."""
    parts = path.as_posix().split("/")
    lowered = [part.lower() for part in parts]
    for index, part in enumerate(lowered):
        if part == "extracted" and index + 1 < len(parts):
            return parts[index + 1]
        if part != "resourcepacks" or index == 0 or lowered[index - 1] != ".mct_cache":
            continue
        # v1.0 isolates each configured resource-pack root below a slug:
        # .mct_cache/resourcepacks/<root-slug>/<archive.zip>/assets/...
        # Older scans placed <archive.zip> directly after resourcepacks.
        for candidate in parts[index + 1 :]:
            if Path(candidate).suffix.casefold() in _ARCHIVE_SUFFIXES:
                return candidate
    return None


async def _augment_unchanged_manifest_addons(
    old_metadata_root: Path,
    current_root: Path,
    current_scan: ScanResult,
    registry: HandlerRegistry,
    translations: Mapping[str, Mapping[str, str]],
    destination: dict[str, dict[str, str]],
    ambiguous: set[tuple[str, str]],
) -> None:
    """Use C as A for addon files proven identical by CurseForge file IDs.

    CurseForge export ZIPs contain a manifest but not mod/resource-pack addon
    payloads. If an addon's project/file pair is unchanged, its C source bytes
    are also its A source bytes, allowing translations to migrate without
    downloading or installing the whole old instance. Addons marked modified
    by the launcher are deliberately excluded.
    """
    old_files = _curseforge_files(old_metadata_root)
    current_archives = _current_archive_files(current_root)
    if not old_files or not current_archives:
        return

    # Explicit payloads found while scanning A are authoritative, so the
    # shortcut only fills coordinates A's export could not carry. The snapshot
    # must predate the gather: two unchanged addons can share one logical id,
    # and reading the live index would let whichever extraction finished first
    # hide the other's conflicting source text instead of recording ambiguity.
    scanned = {logical: set(entries) for logical, entries in destination.items()}

    async def one(pair: LanguageFilePair) -> None:
        archive = _source_archive_name(pair.source_path)
        ids = current_archives.get(archive.lower()) if archive else None
        if ids is None or old_files.get(ids[0]) != ids[1]:
            return
        logical = logical_file_id(pair.source_path, current_root)
        if logical not in translations:
            return
        entries = await _extract_entries(registry, pair.source_path)
        known = scanned.get(logical, frozenset())
        missing = {
            key: value for key, value in entries.items() if key not in known
        }
        if missing:
            _merge_entries(destination, ambiguous, logical, missing)

    await asyncio.gather(*(one(pair) for pair in current_scan.all_translation_pairs))


async def build_migration_catalog(
    *,
    previous_modpack_path: Path,
    previous_resourcepack_path: Path | None,
    previous_overrides_path: Path | None,
    current_modpack_root: Path,
    current_scan: ScanResult,
    source_locale: str,
    target_locale: str,
    asset_cache_dir: Path,
    on_progress: Callable[[str], None] | None = None,
) -> MigrationCatalog:
    """Build the optional migration index without changing the main pipeline."""
    if previous_resourcepack_path is None and previous_overrides_path is None:
        raise MigrationError("at least one previous translation artifact is required")

    catalog = MigrationCatalog(resourcepack_assets_dir=asset_cache_dir)
    registry = create_default_registry()
    with tempfile.TemporaryDirectory(prefix="moru-migration-") as temporary:
        scratch = Path(temporary)
        if on_progress is not None:
            on_progress("indexing previous translation")

        if previous_resourcepack_path is not None:
            materialized = await asyncio.to_thread(
                _materialize_input,
                previous_resourcepack_path,
                scratch / "resourcepack-input",
                "previous_resourcepack_path",
            )
            resource_root = _resourcepack_root(materialized)
            await _index_tree(
                resource_root,
                resource_root,
                registry,
                catalog.previous_translations,
                catalog.ambiguous,
                scratch / "resourcepack-jars",
                "resource",
                target_locale,
            )
            await asyncio.to_thread(
                _copy_resourcepack_assets,
                resource_root,
                asset_cache_dir,
            )
        else:
            if asset_cache_dir.exists():
                await asyncio.to_thread(shutil.rmtree, asset_cache_dir)
            asset_cache_dir.mkdir(parents=True, exist_ok=True)

        if previous_overrides_path is not None:
            materialized = await asyncio.to_thread(
                _materialize_input,
                previous_overrides_path,
                scratch / "overrides-input",
                "previous_overrides_path",
            )
            overrides_root = _overrides_root(materialized)
            await _index_tree(
                overrides_root,
                overrides_root,
                registry,
                catalog.previous_translations,
                catalog.ambiguous,
                scratch / "override-jars",
                "override",
                target_locale,
                asset_cache_dir,
            )

        if on_progress is not None:
            on_progress("indexing previous original")
        materialized_old = await asyncio.to_thread(
            _materialize_input,
            previous_modpack_path,
            scratch / "old-modpack-input",
            "previous_modpack_path",
        )
        old_root, old_metadata_root = _old_modpack_root(materialized_old)
        if previous_modpack_path.is_file():
            await asyncio.to_thread(_validate_nested_scanner_archives, old_root)
        old_scan = await ModpackScanner(source_locale, target_locale).scan(old_root)
        await _index_scan_sources(
            old_scan,
            registry,
            catalog.old_sources,
            catalog.ambiguous,
        )
        await _augment_unchanged_manifest_addons(
            old_metadata_root,
            current_modpack_root,
            current_scan,
            registry,
            catalog.previous_translations,
            catalog.old_sources,
            catalog.ambiguous,
        )

    catalog.stats.preserved_resourcepack_assets = sum(
        1 for path in asset_cache_dir.rglob("*") if path.is_file()
    )
    catalog.stats.old_source_entries = sum(
        len(entries) for entries in catalog.old_sources.values()
    )
    catalog.stats.previous_translation_entries = sum(
        len(entries) for entries in catalog.previous_translations.values()
    )
    catalog.stats.ambiguous_entries = len(catalog.ambiguous)
    logger.info(
        "Previous translation index: %d old source, %d translated, %d ambiguous, "
        "%d font assets",
        catalog.stats.old_source_entries,
        catalog.stats.previous_translation_entries,
        catalog.stats.ambiguous_entries,
        catalog.stats.preserved_resourcepack_assets,
    )
    return catalog
