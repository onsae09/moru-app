"""Parser for Minecraft .lang files (legacy key=value format)."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

import aiofiles

from .base import BaseParser, DumpError, ParseError, is_metadata_key

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

logger = logging.getLogger(__name__)

PARSE_ESCAPES_DIRECTIVE = "#PARSE_ESCAPES"


class LangParser(BaseParser):
    """Parser for Minecraft 1.12 style .lang files.

    Handles key=value format with one entry per line.
    Properly processes JSON escape sequences for special characters.
    Preserves the ``#PARSE_ESCAPES`` directive on dump when the source
    file declared it (required by BetterQuesting and other 1.12.x mods
    that opt into escape handling explicitly).
    """

    file_extensions = (".lang",)

    async def parse(self) -> Mapping[str, str]:
        """Parse a .lang file and extract key-value pairs.

        Returns:
            A mapping of translation keys to values.

        Raises:
            ParseError: If the file cannot be read.
        """
        self._check_extension()
        logger.info("Parsing .lang file: %s", self.path)

        try:
            async with aiofiles.open(
                self.path, encoding="utf-8-sig", errors="replace"
            ) as f:
                text = await f.read()
        except OSError as e:
            raise ParseError(self.path, f"Could not read file: {e}") from e

        mapping = self.parse_text(text)

        logger.debug("Extracted %d entries from %s", len(mapping), self.path)
        return mapping

    @classmethod
    def parse_text(cls, text: str) -> dict[str, str]:
        """Key-value pairs from raw ``.lang`` text.

        Path-free counterpart of :meth:`parse`, for callers that hold the
        bytes instead of a file — a language file read straight out of a
        resource pack ZIP member has no path to hand a parser, and must
        still honour ``#PARSE_ESCAPES`` and the comment-key convention.
        """
        text = text.removeprefix("\ufeff")
        mapping: dict[str, str] = {}
        parse_escapes = any(
            line.strip().upper() == PARSE_ESCAPES_DIRECTIVE
            for line in text.splitlines()
        )

        for line_num, line in enumerate(text.splitlines(), start=1):
            line = line.strip()

            if not line or line.startswith("#"):
                continue

            if "=" not in line:
                logger.debug("Skipping line %d (no '=' found): %s", line_num, line[:50])
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()

            # "#" lines above are the format's own comments; a leading
            # underscore is the same note carried as a real entry.
            if is_metadata_key(key):
                continue

            parsed_value = value
            if parse_escapes:
                try:
                    parsed_value = json.loads(f'"{value}"')
                except json.JSONDecodeError:
                    logger.debug(
                        "Could not parse escape sequences on line %d, using raw value",
                        line_num,
                    )

            mapping[key] = parsed_value

        return mapping

    async def dump(self, data: Mapping[str, str]) -> None:
        """Write key-value pairs to a .lang file.

        Args:
            data: Mapping of translation keys to values.

        Raises:
            DumpError: If writing fails.
        """
        logger.info("Dumping .lang file: %s", self.path)

        directive_source = self.original_path if self.original_path else self.path
        has_parse_escapes = await self._has_parse_escapes_directive(directive_source)

        lines: list[str] = []
        if has_parse_escapes:
            lines.append(PARSE_ESCAPES_DIRECTIVE)
            lines.append("")

        for key, value in sorted(data.items()):
            if isinstance(value, str):
                escaped_value = (
                    json.dumps(value, ensure_ascii=False)[1:-1]
                    if has_parse_escapes
                    else value.replace("\r", "\\r").replace("\n", "\\n")
                )
            else:
                escaped_value = str(value)
            lines.append(f"{key}={escaped_value}")

        try:
            async with aiofiles.open(self.path, "w", encoding="utf-8") as f:
                await f.write("\n".join(lines))
        except OSError as e:
            raise DumpError(self.path, f"Could not write file: {e}") from e

        logger.debug("Successfully wrote %d entries to %s", len(data), self.path)

    @staticmethod
    async def _has_parse_escapes_directive(source: Path) -> bool:
        """Return True when the file declares ``#PARSE_ESCAPES``.

        Forge 1.12 permits the directive anywhere in the file. BetterQuesting
        and similar mods honor escape sequences only when it is present, so
        the generated file must echo it.
        """
        try:
            async with aiofiles.open(
                source, encoding="utf-8-sig", errors="replace"
            ) as f:
                async for raw_line in f:
                    line = raw_line.strip()
                    if line.upper() == PARSE_ESCAPES_DIRECTIVE:
                        return True
        except (OSError, FileNotFoundError):
            return False
        return False
