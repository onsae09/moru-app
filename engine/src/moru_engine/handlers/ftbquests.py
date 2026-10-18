"""FTBQuests content handler for extracting translatable strings from quest files."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from ..parsers import BaseParser, DumpError, ParseError
from .base import ContentHandler

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = logging.getLogger(__name__)

#: Marker suffix for segments lifted out of an embedded JSON text component:
#: ``quests[0].description[3]::jsonseg[2]`` is the third translatable segment
#: of that description string.
_JSONSEG = "::jsonseg["

#: Key segment holding an item stack's data components. FTB Quests 26.1.2+
#: JSON5 serializes components as structured JSON, so an item's custom name
#: is a raw-JSON-text OBJECT whose ``text`` leaves pass the key filter like
#: quest text. They are item data, not quest text: ATM-11 spells one rainbow
#: item name a letter per ``text`` leaf, which would reach the model as
#: single-letter "sentences". 1.21.1-era SNBT kept the same components as
#: JSON strings under keys the filter never matches.
_ITEM_COMPONENTS_SEGMENT = "components"


def _in_item_components(key: str) -> bool:
    """True when ``key`` runs through an item stack's ``components`` map."""
    return any(
        segment.split("[", 1)[0] == _ITEM_COMPONENTS_SEGMENT
        for segment in key.split(".")[:-1]
    )


def _collect_segment_refs(
    node: object, refs: list[tuple[list[object] | dict[str, object], int | str]]
) -> None:
    """DFS over a Minecraft raw-JSON-text tree, recording mutable text slots.

    Translatable slots are bare strings inside component arrays and the
    ``text`` field of component objects (recursing into ``extra``). Event
    payloads (clickEvent/hoverEvent) are never visited — commands and
    tooltips must survive verbatim. Visit order is deterministic, which is
    what keys extracted segments to their slots across extract/apply.
    """
    if isinstance(node, list):
        for index, item in enumerate(node):
            if isinstance(item, str):
                if item.strip():
                    refs.append((node, index))
            else:
                _collect_segment_refs(item, refs)
    elif isinstance(node, dict):
        text = node.get("text")
        if isinstance(text, str) and text.strip():
            refs.append((node, "text"))
        extra = node.get("extra")
        if isinstance(extra, list):
            _collect_segment_refs(extra, refs)


class FTBQuestsHandler(ContentHandler):
    """Handler for FTBQuests SNBT/NBT/JSON5 files.

    Extracts only translatable keys from FTBQuests files:
    - title, name: Quest/chapter names
    - description, text: Descriptions
    - subtitle: Subtitles
    - quest_desc, quest_subtitle, chapter_subtitle: Lang-file entries
    - Lore, Name: Item display components

    FTB Quests 26.1.2+ stores everything as JSON5: quest text lives in
    ``quests/lang/<locale>/`` (``chapters/<chapter>.json5`` for quests,
    tasks and quest links, ``<object_type>.json5`` for the rest), keyed
    ``<object_type>.<id>.<field>``. The game merges every file of a locale
    tree into one table, so translated files mirror the source tree under
    the target locale. JSON5 quest data keeps no inline quest text beyond
    reward fields such as a toast ``description``; its item components are
    never extracted (see ``_ITEM_COMPONENTS_SEGMENT``).

    Strings that embed a Minecraft raw-JSON-text component (single object
    or component array) are split into per-segment entries keyed with the
    ``::jsonseg[n]`` marker; apply() splices the translated segments back
    and re-serializes, so structure, styling, and click/hover events
    survive byte-for-byte semantics.
    """

    name: ClassVar[str] = "ftbquests"
    priority: ClassVar[int] = 15

    path_patterns: ClassVar[tuple[str, ...]] = (
        "/ftbquests/",
        "\\ftbquests\\",
        "/config/ftbquests/",
        "\\config\\ftbquests\\",
    )

    extensions: ClassVar[tuple[str, ...]] = (".snbt", ".nbt", ".json5")

    # Keys that should be translated
    TRANSLATABLE_KEYS: ClassVar[frozenset[str]] = frozenset(
        {
            "title",
            "name",
            "description",
            "text",
            "subtitle",
            "quest_desc",
            "quest_subtitle",
            "chapter_subtitle",
            "Lore",
            "Name",
        }
    )

    def can_handle(self, path: Path) -> bool:
        """Check if this is an FTBQuests file.

        Args:
            path: Path to check.

        Returns:
            True if this is an FTBQuests file.
        """
        if path.suffix.lower() not in self.extensions:
            return False

        path_str = str(path).replace("\\", "/").lower()
        return any(p.lower().replace("\\", "/") in path_str for p in self.path_patterns)

    def _should_translate_key(self, key: str) -> bool:
        """Check if a key should be translated.

        Args:
            key: Full key path (e.g., "quests[0].title").

        Returns:
            True if the key should be translated.
        """
        # Get the last part of the key
        parts = key.split(".")
        last_part = parts[-1]

        # Remove array index if present
        if "[" in last_part:
            last_part = last_part.split("[")[0]

        return last_part in self.TRANSLATABLE_KEYS

    @staticmethod
    def _parse_json_component(text: str) -> dict[str, object] | list[object] | None:
        """Parse an embedded raw-JSON-text component, else None.

        Only whole-string ``{...}`` / ``[...]`` bodies qualify; anything
        that fails to parse is treated as plain text by the caller.
        """
        stripped = text.strip()
        if not (
            (stripped.startswith("{") and stripped.endswith("}"))
            or (stripped.startswith("[") and stripped.endswith("]"))
        ):
            return None
        try:
            data = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            return None
        return data if isinstance(data, (dict, list)) else None

    def _apply_component_segments(
        self, key: str, value: str, translations: Mapping[str, str]
    ) -> str | None:
        """Splice translated ``::jsonseg[n]`` entries back into a component.

        Returns the re-serialized JSON string, or None when the value is
        not a component or no segment translation changes anything.
        """
        prefix = f"{key}{_JSONSEG}"
        if not any(k.startswith(prefix) for k in translations):
            return None
        component = self._parse_json_component(value)
        if component is None:
            return None
        refs: list[tuple[list[object] | dict[str, object], int | str]] = []
        _collect_segment_refs(component, refs)
        changed = False
        for n, (parent, slot) in enumerate(refs):
            translated = translations.get(f"{prefix}{n}]")
            if translated is not None and translated != parent[slot]:  # type: ignore[index]
                parent[slot] = translated  # type: ignore[index]
                changed = True
        return json.dumps(component, ensure_ascii=False) if changed else None

    async def extract(self, path: Path) -> Mapping[str, str]:
        """Extract translatable strings from FTBQuests file.

        Args:
            path: Path to the file.

        Returns:
            Mapping of keys to translatable text.
        """
        parser = BaseParser.create_parser(path)
        if parser is None:
            logger.warning("No parser found for: %s", path)
            return {}

        try:
            raw_data = await parser.parse()
        except (ParseError, OSError) as e:
            logger.error("Failed to parse %s: %s", path, e)
            return {}

        entries: dict[str, str] = {}
        self._extract_recursive(dict(raw_data), entries, "")
        if path.suffix.lower() == ".json5":
            # Scoped to JSON5 (see _ITEM_COMPONENTS_SEGMENT); SNBT item data
            # keeps the extraction it always had.
            entries = {
                key: value
                for key, value in entries.items()
                if not _in_item_components(key)
            }

        logger.debug(
            "Extracted %d entries from FTBQuests file: %s", len(entries), path.name
        )
        return entries

    def _extract_recursive(
        self,
        data: dict[str, object] | list[object] | str,
        entries: dict[str, str],
        prefix: str,
    ) -> None:
        """Recursively extract translatable strings.

        Args:
            data: Data to extract from.
            entries: Dictionary to store entries.
            prefix: Current key prefix.
        """
        if isinstance(data, dict):
            for key, value in data.items():
                full_key = f"{prefix}.{key}" if prefix else key
                self._extract_value(full_key, value, entries)

        elif isinstance(data, list):
            for i, item in enumerate(data):
                full_key = f"{prefix}[{i}]"
                self._extract_value(full_key, item, entries)

    def _extract_value(
        self,
        key: str,
        value: object,
        entries: dict[str, str],
    ) -> None:
        """Extract value if it's translatable.

        Args:
            key: Full key path.
            value: Value to check.
            entries: Dictionary to store entries.
        """
        if isinstance(value, str):
            if self._should_translate_key(key) and value.strip():
                component = self._parse_json_component(value)
                if component is not None:
                    # Embedded raw-JSON-text: one entry per text segment so
                    # the LLM never sees (or breaks) the JSON structure.
                    refs: list[
                        tuple[list[object] | dict[str, object], int | str]
                    ] = []
                    _collect_segment_refs(component, refs)
                    for n, (parent, slot) in enumerate(refs):
                        entries[f"{key}{_JSONSEG}{n}]"] = parent[slot]  # type: ignore[index,assignment]
                    return
                entries[key] = value

        elif isinstance(value, dict):
            self._extract_recursive(value, entries, key)

        elif isinstance(value, list):
            for i, item in enumerate(value):
                item_key = f"{key}[{i}]"
                self._extract_value(item_key, item, entries)

    async def apply(
        self,
        path: Path,
        translations: Mapping[str, str],
        output_path: Path | None = None,
    ) -> None:
        """Apply translations to FTBQuests file.

        Args:
            path: Path to the original file.
            translations: Mapping of keys to translated text.
            output_path: Optional output path.
        """
        target_path = output_path or path

        parser = BaseParser.create_parser(path)
        if parser is None:
            logger.warning("No parser found for: %s", path)
            return

        try:
            raw_data = await parser.parse()
            data = dict(raw_data)
        except (ParseError, OSError) as e:
            logger.error("Failed to parse %s: %s", path, e)
            return

        # Apply translations
        modified = self._apply_recursive(data, translations, "")

        if not modified:
            logger.debug("No translations applied to: %s", path.name)
            return

        # Write output
        target_path.parent.mkdir(parents=True, exist_ok=True)

        output_parser = BaseParser.create_parser(target_path, original_path=path)
        if output_parser is None:
            logger.warning("No parser found for output: %s", target_path)
            return

        try:
            await output_parser.dump(data)
            logger.debug("Applied translations to: %s", target_path.name)
        except (DumpError, OSError) as e:
            logger.error("Failed to write %s: %s", target_path, e)
            raise

    def _apply_recursive(
        self,
        data: dict[str, object],
        translations: Mapping[str, str],
        prefix: str,
    ) -> bool:
        """Recursively apply translations.

        Args:
            data: Data to modify.
            translations: Translations to apply.
            prefix: Current key prefix.

        Returns:
            True if any translation was applied.
        """
        modified = False

        for key, value in list(data.items()):
            full_key = f"{prefix}.{key}" if prefix else key

            if isinstance(value, str):
                rebuilt = self._apply_component_segments(
                    full_key, value, translations
                )
                if rebuilt is not None:
                    data[key] = rebuilt
                    modified = True
                elif full_key in translations:
                    data[key] = translations[full_key]
                    modified = True

            elif isinstance(value, dict):
                if self._apply_recursive(value, translations, full_key):
                    modified = True

            elif isinstance(value, list):
                if self._apply_list(value, translations, full_key):
                    modified = True

        return modified

    def _apply_list(
        self,
        data: list[object],
        translations: Mapping[str, str],
        prefix: str,
    ) -> bool:
        """Apply translations to list items.

        Args:
            data: List to modify.
            translations: Translations to apply.
            prefix: Current key prefix.

        Returns:
            True if any translation was applied.
        """
        modified = False

        for i, item in enumerate(data):
            item_key = f"{prefix}[{i}]"

            if isinstance(item, str):
                rebuilt = self._apply_component_segments(
                    item_key, item, translations
                )
                if rebuilt is not None:
                    data[i] = rebuilt
                    modified = True
                elif item_key in translations:
                    data[i] = translations[item_key]
                    modified = True

            elif isinstance(item, dict):
                if self._apply_recursive(item, translations, item_key):
                    modified = True

            elif isinstance(item, list):
                if self._apply_list(item, translations, item_key):
                    modified = True

        return modified
