"""Parser for JSON5 files (FTB Quests 26.1.2+ quest and translation data)."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

import aiofiles
import pyjson5

from .base import BaseParser, DumpError, ParseError

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

logger = logging.getLogger(__name__)

# Type alias for decoded JSON5 values (recursive)
type JSON5Value = (
    str | int | float | bool | None | list[JSON5Value] | dict[str, JSON5Value]
)


def _decode_reason(error: pyjson5.Json5Exception) -> str:
    """Human-readable cause of a decode failure.

    ``str()`` of a pyjson5 exception embeds the partially decoded document,
    which for a broken quest file is the whole chapter dumped into one log
    line; the message alone names the problem and its offset.
    """
    return error.message or type(error).__name__


class JSON5Parser(BaseParser):
    """Parser for JSON5 format files.

    FTB Quests 26.1.2 moved every quest and translation file from SNBT to
    JSON5, whose trailing commas, unquoted keys and comments the strict
    JSON parser rejects. Values flatten to the key paths the SNBT parser
    produces (``quests[0].title``, ``quest.<id>.quest_desc[2]``), so
    handlers, the validator and the TM see one scheme for both formats.

    Writing re-reads the original document and swaps translated strings in
    place, so numbers, booleans and untranslated fields survive untouched.
    The result is standard JSON, a strict subset of JSON5: the comments and
    unquoted keys it drops carry nothing the game reads.
    """

    file_extensions = (".json5",)

    async def parse(self) -> Mapping[str, str]:
        """Parse a JSON5 file and extract its string values.

        Returns:
            A flattened mapping of key paths to string values.

        Raises:
            ParseError: If the file cannot be read or is not valid JSON5.
        """
        self._check_extension()
        logger.info("Parsing JSON5 file: %s", self.path)

        try:
            data = await self._load(self.path)
        except OSError as e:
            raise ParseError(self.path, f"Could not read file: {e}") from e
        except pyjson5.Json5Exception as e:
            raise ParseError(self.path, f"Invalid JSON5: {_decode_reason(e)}") from e

        result = self._flatten(data)
        logger.debug("Extracted %d strings from %s", len(result), self.path)
        return result

    async def dump(self, data: Mapping[str, str]) -> None:
        """Write translated values back into the original JSON5 structure.

        Args:
            data: Flattened mapping of key paths to translated values.

        Raises:
            DumpError: If the original cannot be read or the output written.
        """
        logger.info("Dumping JSON5 file: %s", self.path)

        source_path = self.original_path if self.original_path else self.path
        try:
            document = await self._load(source_path)
        except OSError as e:
            raise DumpError(self.path, f"Could not read original file: {e}") from e
        except pyjson5.Json5Exception as e:
            raise DumpError(
                self.path, f"Original is not valid JSON5: {_decode_reason(e)}"
            ) from e

        self._replace_strings(document, data, "")

        # Two-space indentation matches the files FTB Quests writes itself.
        content = json.dumps(document, ensure_ascii=False, indent=2)
        try:
            async with aiofiles.open(self.path, "w", encoding="utf-8") as f:
                await f.write(content)
        except OSError as e:
            raise DumpError(self.path, f"Could not write file: {e}") from e

        logger.debug("Successfully wrote %d entries to %s", len(data), self.path)

    @staticmethod
    async def _load(path: Path) -> JSON5Value:
        """Decode one JSON5 document.

        ``utf-8-sig`` drops the BOM a Windows editor may prepend. Nesting is
        unbounded like the game's own reader: item data nests containers
        inside containers, and pyjson5's default cap of 32 levels would
        reject a chapter the game loads.

        Raises:
            OSError: If the file cannot be read.
            pyjson5.Json5Exception: If the content is not valid JSON5.
        """
        async with aiofiles.open(path, encoding="utf-8-sig", errors="replace") as f:
            content = await f.read()
        return pyjson5.decode(content, maxdepth=-1)

    @classmethod
    def _flatten(cls, data: JSON5Value, prefix: str = "") -> dict[str, str]:
        """Flatten a decoded document to its string leaves.

        Args:
            data: Decoded JSON5 value.
            prefix: Key path of ``data`` itself.

        Returns:
            Mapping of ``a.b[0].c``-style key paths to string values.
        """
        result: dict[str, str] = {}

        if isinstance(data, dict):
            for key, value in data.items():
                new_key = f"{prefix}.{key}" if prefix else key
                if isinstance(value, str):
                    result[new_key] = value
                elif isinstance(value, dict | list):
                    result.update(cls._flatten(value, new_key))
        elif isinstance(data, list):
            for i, item in enumerate(data):
                new_key = f"{prefix}[{i}]" if prefix else f"[{i}]"
                if isinstance(item, str):
                    result[new_key] = item
                elif isinstance(item, dict | list):
                    result.update(cls._flatten(item, new_key))

        return result

    @classmethod
    def _replace_strings(
        cls,
        data: JSON5Value,
        flat_data: Mapping[str, str],
        prefix: str,
    ) -> None:
        """Swap translated strings into the decoded document in place.

        Walks the original structure rather than parsing the flat keys, so
        dotted lang keys (``quest.<id>.title``) resolve unambiguously.

        Args:
            data: Decoded JSON5 value (modified in place).
            flat_data: Flattened translated data.
            prefix: Key path of ``data`` itself.
        """
        if isinstance(data, dict):
            for key, value in data.items():
                new_key = f"{prefix}.{key}" if prefix else key
                if isinstance(value, str):
                    if new_key in flat_data:
                        data[key] = flat_data[new_key]
                elif isinstance(value, dict | list):
                    cls._replace_strings(value, flat_data, new_key)
        elif isinstance(data, list):
            for i, item in enumerate(data):
                new_key = f"{prefix}[{i}]" if prefix else f"[{i}]"
                if isinstance(item, str):
                    if new_key in flat_data:
                        data[i] = flat_data[new_key]
                elif isinstance(item, dict | list):
                    cls._replace_strings(item, flat_data, new_key)
