"""Installable output generation: resource pack + overrides.

The pipeline produces per-file translation maps; this module turns them
into the two artifacts a player actually installs:

- ``resourcepack/`` — ``pack.mcmeta`` plus ``assets/<ns>/lang/*``.
  Only *fresh* entries are written: language keys merge across packs at
  runtime, so re-shipping translations the modpack already contains is
  pure bloat — and worse, template-merging dumps would re-emit the
  English source for those keys, overriding the pack's own translations.
- ``overrides/`` — files a resource pack cannot carry (kubejs/, config/,
  scripts/, ftbquests/, patchouli_books/). They are copied over the
  modpack root and replace whole files, so they carry the *full* merged
  state (fresh + pre-existing target entries); a fresh-only file would
  wipe translations the modpack already shipped.

A file with no fresh entry is skipped entirely — the modpack already has
that translation. JAR-internal ``data/`` files would require patching the
mod ``.jar`` (unsupported); they are counted and skipped.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import zipfile
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path

import aiofiles

from ..handlers.base import create_default_registry
from ..parsers import BaseParser, LangParser, ParserError
from ..utils.locale_helper import replace_locale_in_path
from .bilingual import annotate_entries
from .mcmeta_text import fit_description

logger = logging.getLogger(__name__)

#: Used only when the modpack's Minecraft version cannot be determined at
#: all. A resolved version always maps through :data:`RESOURCE_PACK_FORMATS`.
DEFAULT_PACK_FORMAT = 15

#: Resource-pack format from which ``pack.mcmeta`` swapped ``pack_format``
#: for a required ``min_format``/``max_format`` pair and dropped
#: ``supported_formats`` (MC 1.21.9, snapshot 25w31a). Emitting a single
#: ``pack_format`` integer at or above this format is structurally wrong,
#: not merely a stale number.
MINOR_VERSION_ERA_FORMAT = 65

RESOURCEPACK_DIRNAME = "resourcepack"
OVERRIDES_DIRNAME = "overrides"

#: Subdirectory holding the bilingual variant's own resourcepack/overrides
#: pair, so both variants ship from one run without colliding.
BILINGUAL_DIRNAME = "bilingual"
#: Appended to the bilingual pack's description. The two packs are otherwise
#: byte-identical in layout, so this is the only in-game way to tell them
#: apart in the resource-pack list. Kept to 28px (a bracket form rather than
#: a parenthesised phrase) so that even the longest realistic version prefix
#: still leaves the whole description inside the two-visual-line budget —
#: see output/mcmeta_text.py.
BILINGUAL_DESCRIPTION_SUFFIX = " §8[병기]"

#: First Minecraft release using each RESOURCE-pack ``pack_format``, newest
#: first, so a lookup is "the first entry that is <= the target version".
#:
#: RESOURCE-pack values only. The data-pack line diverged at 1.18.2
#: (resource 8 / data 9) and has never re-synced, so the two must never
#: share a constant. Values 10, 23 and 27 never existed for resource packs;
#: the remaining gaps are formats that only ever shipped in snapshots.
#: Source: https://minecraft.wiki/w/Pack_format, resource-pack table.
RESOURCE_PACK_FORMATS: tuple[tuple[tuple[int, ...], int], ...] = (
    ((26, 2), 88),
    ((26, 1), 84),
    ((1, 21, 11), 75),
    ((1, 21, 9), 69),
    ((1, 21, 7), 64),
    ((1, 21, 6), 63),
    ((1, 21, 5), 55),
    ((1, 21, 4), 46),
    ((1, 21, 2), 42),
    ((1, 21), 34),
    ((1, 20, 5), 32),
    ((1, 20, 3), 22),
    ((1, 20, 2), 18),
    ((1, 20), 15),
    ((1, 19, 4), 13),
    ((1, 19, 3), 12),
    ((1, 19), 9),
    ((1, 18), 8),
    ((1, 17), 7),
    ((1, 16, 2), 6),
    ((1, 15), 5),
    ((1, 13), 4),
    ((1, 11), 3),
    ((1, 9), 2),
    ((1, 6, 1), 1),
)

#: A bare release number — "1.20.1", "1.21", "26.2". Anchored, because
#: ``PackIdentity.mc_version`` comes from launcher metadata and is exactly
#: this.
_RELEASE_RE = re.compile(r"^v?(\d{1,4}(?:\.\d+){0,3})$")
#: Fallback for a version embedded in prose ("Forge 1.12.2"). Restricted to
#: the ``1.x`` scheme: an unanchored search for the post-1.21.11 ``YY.N``
#: scheme would just as happily match a mod-loader version.
_EMBEDDED_RELEASE_RE = re.compile(r"(?<!\d)(1\.\d+(?:\.\d+)?)(?!\d)")


def _release_tuple(mc_version: str) -> tuple[int, ...] | None:
    """Numeric release tuple, or None when there is no release number.

    Comparable across the numbering change: Minecraft went ``1.21.11`` ->
    ``26.1``, and ``(26, 1) > (1, 21, 11)`` holds, whereas naive string or
    semver comparison misorders them.
    """
    stripped = mc_version.strip()
    match = _RELEASE_RE.match(stripped) or _EMBEDDED_RELEASE_RE.search(stripped)
    if match is None:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def pack_format_for_minecraft_version(
    mc_version: str | None,
    fallback: int = DEFAULT_PACK_FORMAT,
) -> int:
    """Return the resource-pack format for a Minecraft version.

    A wrong ``pack_format`` can stop the generated pack from loading at
    all, so every release from 1.6.1 onward is mapped explicitly instead of
    guessed — the previous two-value approximation (3, else 15) was right
    only for 1.11-1.12.2 and 1.20-1.20.1.

    Unknown or unparseable versions keep ``fallback`` rather than guessing.
    A version NEWER than the table resolves to the newest known format:
    declaring a format older than the running game's yields a "made for an
    older version" warning and the pack still loads, whereas fabricating a
    higher number can be rejected outright, so clamping downward is the
    safe direction.
    """
    if mc_version is None:
        return fallback
    release = _release_tuple(mc_version)
    if release is None:
        return fallback
    for first_release, pack_format in RESOURCE_PACK_FORMATS:
        if first_release <= release:
            return pack_format
    # Older than 1.6.1, which predates pack_format entirely.
    return fallback

#: Bundled moru anvil icon, shipped as ``pack.png`` so the generated pack
#: is recognizable in the resource-pack selection screen.
PACK_ICON_ASSET = Path(__file__).resolve().parents[1] / "assets" / "pack.png"

#: Path fragments (lowercased, ``/``-normalized) that mark a file as a
#: modpack-root override rather than resource-pack content.
OVERRIDE_MARKERS = (
    "kubejs/",
    "config/",
    "scripts/",
    "/ftbquests/",
    "patchouli_books/",
)

#: Fragments marking content extracted out of an archive by the scanner.
EXTRACTED_MARKERS = (".mct_cache", "extracted")


class Route(Enum):
    """Destination of one translated file."""

    RESOURCE_PACK = "resource_pack"
    OVERRIDE = "override"
    #: JAR-internal data/ file — needs .jar patching, unsupported.
    SKIP_JAR_DATA = "skip_jar_data"
    #: Override-pattern file inside an extracted archive — nothing on disk
    #: to overwrite, and a resource pack cannot carry it either.
    SKIP_EXTRACTED = "skip_extracted"


@dataclass(slots=True)
class FileOutput:
    """Translation state of one source file, ready to be routed."""

    source_path: Path
    #: Entries newly produced by this run (LLM, TM hits, review edits).
    fresh: dict[str, str]
    #: ``fresh`` plus entries whose translation pre-existed in the modpack.
    full: dict[str, str]
    namespace: str = ""


@dataclass(slots=True)
class OutputConfig:
    modpack_root: Path
    output_dir: Path
    source_locale: str = "en_us"
    target_locale: str = "ko_kr"
    pack_format: int = DEFAULT_PACK_FORMAT
    description: str = "§a모루§7로 번역됨 — §amoru.gg"
    #: Every translated run renders a bilingual display-name sibling tree.
    #: Source-text-only runs disable it because the values are unchanged.
    bilingual_names: bool = True
    #: Optional previous font definitions/files/textures. The migration index
    #: has already removed translated files and pack metadata.
    resourcepack_seed_dir: Path | None = None


@dataclass(slots=True, frozen=True)
class JarDataLoss:
    """One translated file that no installable channel can deliver.

    A mod JAR's own ``data/`` tree is the one place a translation cannot
    reach. Patchouli proves why: ``BookRegistry.init`` walks each mod
    container's files directly and opens the hit with
    ``Files.newInputStream(mod.getPath(file))``, so ``book.json`` never
    passes through the resource-pack OR data-pack stack and nothing
    outside the JAR can shadow it. The resource pack carries ``assets/``,
    the overrides tree carries modpack-root files, and neither is a
    channel for this.

    This is NOT hardcoded text. Hardcoded text has no lang key at all, so
    it was never translatable and the mod author has to change the mod.
    Here the string WAS extracted and translated successfully; only
    delivery is impossible, and patching the JAR is the only fix. The two
    must stay separate in any report, because the user's next action
    differs: nothing versus repackage the JAR yourself.
    """

    #: Path as scanned, inside the extraction cache.
    source_path: str
    #: JAR the file came out of, for attribution. "" when unattributable.
    mod: str
    #: Translations produced for this file and then dropped.
    entry_count: int


@dataclass(slots=True)
class GenerationResult:
    resourcepack_dir: Path
    overrides_dir: Path
    resourcepack_files: list[Path] = field(default_factory=list)
    override_files: list[Path] = field(default_factory=list)
    pack_mcmeta: Path | None = None
    pack_icon: Path | None = None
    #: Files skipped because every entry already had a translation.
    skipped_existing: int = 0
    #: Files whose translations were produced and then had nowhere to go.
    jar_data_losses: list[JarDataLoss] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    #: Bilingual-variant pass, when ``OutputConfig.bilingual_names`` is set.
    bilingual: GenerationResult | None = None

    @property
    def all_files(self) -> list[Path]:
        files = [*self.resourcepack_files, *self.override_files]
        if self.pack_mcmeta is not None:
            files.append(self.pack_mcmeta)
        if self.pack_icon is not None:
            files.append(self.pack_icon)
        if self.bilingual is not None:
            files.extend(self.bilingual.all_files)
        return files

    @property
    def skipped_jar_data(self) -> int:
        """Translated files lost to a JAR's internal ``data/`` tree."""
        return len(self.jar_data_losses)

    @property
    def jar_data_loss_mods(self) -> list[str]:
        """JARs owning at least one lost file, in first-seen order."""
        seen: dict[str, None] = {}
        for loss in self.jar_data_losses:
            if loss.mod:
                seen.setdefault(loss.mod, None)
        return list(seen)


def _norm(path: Path | str) -> str:
    return str(path).replace("\\", "/").lower()


def jar_name_for(source_path: Path | str) -> str:
    """JAR a scanner-extracted path came out of, else ``""``.

    The scanner unpacks each archive to
    ``.mct_cache/extracted/<jar name>/...``, so the segment after
    ``extracted`` is the JAR — the only attribution available once a file
    has been lifted out of its archive.
    """
    parts = str(source_path).replace("\\", "/").split("/")
    for index, part in enumerate(parts):
        if part.lower() == "extracted" and index + 1 < len(parts):
            return parts[index + 1]
    return ""


def create_zip_from_directory(source_dir: Path, zip_path: Path) -> None:
    """Zip ``source_dir`` contents (arcnames relative to it)."""
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for file_path in sorted(source_dir.rglob("*")):
            if file_path.is_file():
                zf.write(file_path, file_path.relative_to(source_dir))


def route_for(source_path: Path | str) -> Route:
    """Classify one source file. Pure; used by generator and tests."""
    norm = _norm(source_path)
    extracted = any(marker in norm for marker in EXTRACTED_MARKERS)
    if extracted and "/data/" in norm:
        return Route.SKIP_JAR_DATA
    if any(marker in norm for marker in OVERRIDE_MARKERS):
        if not extracted:
            return Route.OVERRIDE
        # Inside an archive only asset-tree content (e.g. a mod's builtin
        # patchouli book) can ride along in the resource pack.
        if "patchouli_books/" in norm and "/assets/" in norm:
            return Route.RESOURCE_PACK
        return Route.SKIP_EXTRACTED
    return Route.RESOURCE_PACK


class OutputGenerator:
    """Writes the ``resourcepack/`` + ``overrides/`` trees for one run."""

    def __init__(self, config: OutputConfig) -> None:
        self.config = config
        self.registry = create_default_registry()

    @property
    def resourcepack_dir(self) -> Path:
        return self.config.output_dir / RESOURCEPACK_DIRNAME

    @property
    def overrides_dir(self) -> Path:
        return self.config.output_dir / OVERRIDES_DIRNAME

    async def generate(self, files: list[FileOutput]) -> GenerationResult:
        result = GenerationResult(
            resourcepack_dir=self.resourcepack_dir,
            overrides_dir=self.overrides_dir,
        )
        # Both trees are fully derived artifacts: regenerate from scratch
        # so stale files from a previous run never leak into the zips.
        for tree in (self.resourcepack_dir, self.overrides_dir):
            if tree.exists():
                shutil.rmtree(tree)

        # A previous translation can carry fonts needed for Korean readability.
        # Seed only its pre-filtered font definitions/files/textures; generated
        # C translations below win on any path collision.
        seed = self.config.resourcepack_seed_dir
        if seed is not None and seed.is_dir():
            for source in sorted(seed.rglob("*")):
                if not source.is_file():
                    continue
                target = self.resourcepack_dir / source.relative_to(seed)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                result.resourcepack_files.append(target)

        # Language files from several sources (mod JAR + resourcepack zip
        # + overlay) can map to the same output file; merge them so the
        # last-scanned source wins per key instead of per file.
        lang_buckets: dict[Path, dict[str, str]] = {}
        bucket_sources: dict[Path, Path] = {}

        for file in files:
            routed = route_for(file.source_path)
            if routed is Route.SKIP_JAR_DATA:
                # A loss only when this run actually produced something for
                # the file. A mod's book.json routinely holds nothing but
                # lang keys (Botania's "item.botania.lexicon"), which the
                # handler skips, so it yields no entries at all — counting
                # it would report a loss the user cannot act on and cannot
                # even see, since that title IS translated through the
                # JAR's own lang file.
                if file.fresh:
                    result.jar_data_losses.append(
                        JarDataLoss(
                            source_path=str(file.source_path),
                            mod=jar_name_for(file.source_path),
                            entry_count=len(file.fresh),
                        )
                    )
                    logger.info(
                        "Cannot install %d translated entr%s for %s: a JAR's "
                        "own data/ tree is not reachable by a resource pack "
                        "or a data pack",
                        len(file.fresh),
                        "y" if len(file.fresh) == 1 else "ies",
                        file.source_path,
                    )
                continue
            if routed is Route.SKIP_EXTRACTED:
                logger.debug(
                    "Skipping extracted override candidate: %s", file.source_path
                )
                continue
            if not file.fresh:
                # Everything in this file already had a translation.
                result.skipped_existing += 1
                continue

            try:
                if routed is Route.OVERRIDE:
                    written = await self._write_override(file)
                    if written is not None:
                        result.override_files.append(written)
                    continue

                handler = self.registry.get_handler(file.source_path)
                if handler is not None and handler.name != "language":
                    # Structured asset (patchouli book, tconstruct book…):
                    # whole-file format, must carry the full merged state.
                    written = await self._write_structured_asset(file)
                    if written is not None:
                        result.resourcepack_files.append(written)
                    continue

                output_path = self._lang_output_path(file)
                bucket = lang_buckets.setdefault(output_path, {})
                bucket.update(file.fresh)
                bucket_sources.setdefault(output_path, file.source_path)
            except (OSError, ValueError, TypeError, ParserError) as exc:
                message = f"Failed to generate output for {file.source_path}: {exc}"
                logger.error(message)
                result.errors.append(message)

        for output_path, data in lang_buckets.items():
            try:
                await self._write_lang_file(
                    output_path, bucket_sources[output_path], data
                )
                result.resourcepack_files.append(output_path)
            except (OSError, ValueError, TypeError, ParserError) as exc:
                message = f"Failed to write lang file {output_path}: {exc}"
                logger.error(message)
                result.errors.append(message)

        if result.resourcepack_files:
            result.pack_mcmeta = await self._write_pack_mcmeta()
            result.pack_icon = self._write_pack_icon()

        logger.info(
            "Output generation: %d resourcepack + %d override files "
            "(%d already translated, %d jar-data skipped, %d errors)",
            len(result.resourcepack_files),
            len(result.override_files),
            result.skipped_existing,
            result.skipped_jar_data,
            len(result.errors),
        )

        if self.config.bilingual_names:
            result.bilingual = await self._generate_bilingual(files)
        return result

    # -- bilingual variant ---------------------------------------------------

    async def _generate_bilingual(
        self, files: list[FileOutput]
    ) -> GenerationResult:
        """Render the same trees again with display names carrying the source.

        Built from the SAME entries the plain pass used — one translation
        run, two variants, no extra model cost. The only thing the plain
        pass does not carry is the *source* text, so each source file is
        re-read for its original strings; ``_write_override`` and
        ``_write_structured_asset`` already re-read the original the same
        way, so the keys line up by construction.

        Routing and writing then go through the identical code path (a
        sibling generator rooted at ``output_dir/bilingual``), which is what
        guarantees the two trees are structurally identical and differ only
        in the annotated values.
        """
        cache: dict[Path, dict[str, str]] = {}
        annotated: list[FileOutput] = []
        for file in files:
            sources = await self._source_entries(file.source_path, cache)
            annotated.append(
                replace(
                    file,
                    fresh=annotate_entries(file.fresh, sources),
                    full=annotate_entries(file.full, sources),
                )
            )

        sibling = OutputGenerator(
            replace(
                self.config,
                output_dir=self.config.output_dir / BILINGUAL_DIRNAME,
                description=(
                    self.config.description + BILINGUAL_DESCRIPTION_SUFFIX
                ),
                bilingual_names=False,
            )
        )
        return await sibling.generate(annotated)

    async def _source_entries(
        self, source_path: Path, cache: dict[Path, dict[str, str]]
    ) -> dict[str, str]:
        """Original strings of one source file, memoized for this run.

        A read failure degrades to "annotate nothing in this file" — the
        bilingual tree then simply matches the plain one there — rather
        than failing generation over a cosmetic variant.
        """
        cached = cache.get(source_path)
        if cached is not None:
            return cached

        entries: dict[str, str] = {}
        try:
            handler = self.registry.get_handler(source_path)
            if handler is not None:
                entries = dict(await handler.extract(source_path))
            else:
                parser = BaseParser.create_parser(source_path)
                if parser is not None:
                    entries = dict(await parser.parse())
        except (OSError, ValueError, TypeError, ParserError) as exc:
            logger.warning(
                "Bilingual pass: cannot read source strings from %s: %s",
                source_path,
                exc,
            )
        cache[source_path] = entries
        return entries

    # -- resource pack -----------------------------------------------------

    async def _write_pack_mcmeta(self) -> Path:
        """Write ``pack.mcmeta`` using the target version's field set.

        MC 1.21.9 (resource format 65) replaced ``pack_format`` with a
        required ``min_format``/``max_format`` pair and disallowed
        ``supported_formats``. A single ``pack_format`` integer is therefore
        not merely stale for those versions, it is the wrong schema, so the
        two eras emit different keys.

        A bare integer means ``[N, 0]`` for ``min_format`` but "major N,
        any minor" for ``max_format`` — deliberately asymmetric upstream —
        which is exactly "this one target version".
        """
        pack_format = self.config.pack_format
        pack: dict[str, object] = (
            {"min_format": pack_format, "max_format": pack_format}
            if pack_format >= MINOR_VERSION_ERA_FORMAT
            else {"pack_format": pack_format}
        )
        # The selection screen drops overflow from the END, which is exactly
        # where our attribution URL sits, and offers no tooltip to recover
        # it. Fitting trims from the front instead so moru.gg survives.
        pack["description"] = fit_description(self.config.description)
        mcmeta = {"pack": pack}
        path = self.resourcepack_dir / "pack.mcmeta"
        path.parent.mkdir(parents=True, exist_ok=True)
        async with aiofiles.open(path, "w", encoding="utf-8") as f:
            await f.write(json.dumps(mcmeta, ensure_ascii=False, indent=2))
        return path

    def _write_pack_icon(self) -> Path | None:
        """Copy the bundled moru icon next to pack.mcmeta as ``pack.png``."""
        if not PACK_ICON_ASSET.is_file():
            logger.warning("Pack icon asset missing: %s", PACK_ICON_ASSET)
            return None
        path = self.resourcepack_dir / "pack.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(PACK_ICON_ASSET, path)
        return path

    def _asset_relative(self, source_path: Path) -> str | None:
        """Path below the asset root, or None when there is no such root.

        Recognized layouts:
        - ``…/assets/<ns>/…`` — standard resource pack / mod JAR tree.
        - ``…/resources/<ns>/…`` — 1.12.x launcher overlay where
          ``resources/`` itself is the asset root.
        """
        source_str = str(source_path).replace("\\", "/")
        lower = source_str.lower()
        for marker in ("/assets/", "/resources/"):
            idx = lower.find(marker)
            if idx >= 0:
                rel = source_str[idx + len(marker) :]
                return replace_locale_in_path(
                    rel,
                    self.config.source_locale,
                    self.config.target_locale,
                    preserve_case=self.config.pack_format < 3,
                )
        return None

    def _lang_output_path(self, file: FileOutput) -> Path:
        assets_dir = self.resourcepack_dir / "assets"
        rel = self._asset_relative(file.source_path)
        if rel is not None:
            return assets_dir / rel
        # Handler-extracted files without an assets/ segment still land
        # somewhere the game can load them.
        ns = file.namespace or "minecraft"
        suffix = file.source_path.suffix or ".json"
        return assets_dir / ns / "lang" / f"{self.config.target_locale}{suffix}"

    async def _write_lang_file(
        self, output_path: Path, source_path: Path, data: dict[str, str]
    ) -> None:
        """Write a merged, fresh-only language file.

        Deliberately NOT the template-merging JSON parser: that re-emits
        every source key, putting English text back over keys the modpack
        already translated. ``.lang`` uses the parser (its dump writes only
        the given entries and preserves ``#PARSE_ESCAPES``); everything
        else becomes a flat JSON object.
        """
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if output_path.suffix.lower() == ".lang":
            await LangParser(output_path, original_path=source_path).dump(data)
            return
        payload = json.dumps(
            dict(sorted(data.items())), ensure_ascii=False, indent=2
        )
        async with aiofiles.open(output_path, "w", encoding="utf-8") as f:
            await f.write(payload)

    async def _write_structured_asset(self, file: FileOutput) -> Path | None:
        rel = self._asset_relative(file.source_path)
        if rel is None:
            logger.warning(
                "Cannot mirror structured asset (no assets/ segment): %s",
                file.source_path,
            )
            return None
        output_path = self.resourcepack_dir / "assets" / rel
        output_path.parent.mkdir(parents=True, exist_ok=True)
        handler = self.registry.get_handler(file.source_path)
        assert handler is not None  # caller routed via get_handler
        await handler.apply(file.source_path, file.full, output_path)
        return output_path

    # -- overrides -----------------------------------------------------------

    async def _write_override(self, file: FileOutput) -> Path | None:
        source_path = file.source_path
        try:
            rel = source_path.resolve().relative_to(
                self.config.modpack_root.resolve()
            ).as_posix()
        except ValueError:
            rel = source_path.name
        rel = replace_locale_in_path(
            rel, self.config.source_locale, self.config.target_locale
        )
        output_path = self.overrides_dir / rel
        output_path.parent.mkdir(parents=True, exist_ok=True)

        handler = self.registry.get_handler(source_path)
        if handler is not None:
            await handler.apply(source_path, file.full, output_path)
            return output_path

        parser = BaseParser.create_parser(output_path, source_path)
        if parser is None:
            logger.warning("No handler or parser for override: %s", source_path)
            return None
        await parser.dump(file.full)
        return output_path
