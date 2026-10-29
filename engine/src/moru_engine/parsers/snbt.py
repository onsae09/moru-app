"""Parser for SNBT (Stringified NBT) format files."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

import aiofiles

from .base import BaseParser, DumpError, ParseError

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = logging.getLogger(__name__)

# Try to import ftb_snbt_lib
try:
    import ftb_snbt_lib as slib

    HAS_SNBT_LIB = True
except ImportError:
    HAS_SNBT_LIB = False
    slib = None  # type: ignore[assignment]


class SNBTParser(BaseParser):
    """Parser for SNBT format files.

    Uses the ftb_snbt_lib library to parse SNBT files and handles
    Minecraft color codes appropriately.
    """

    file_extensions = (".snbt",)

    async def parse(self) -> Mapping[str, str]:
        self._check_extension()
        logger.info("Parsing SNBT file: %s", self.path)

        try:
            # Read the file asynchronously
            async with aiofiles.open(
                self.path, encoding="utf-8-sig", errors="replace"
            ) as f:
                content = await f.read()
        except OSError as e:
            raise ParseError(self.path, f"Could not read file: {e}") from e

        if "\x00" in content:
            # A zero-filled or binary file (seen in shipped packs). The lexer
            # would print one "Illegal character" line per byte, every parse.
            raise ParseError(self.path, "file contains NUL bytes (corrupt or binary)")

        if HAS_SNBT_LIB:
            try:
                # Try parsing with ftb_snbt_lib
                data = slib.loads(content)
                result = self._flatten_snbt(data)
                logger.debug("Extracted %d strings from %s", len(result), self.path)
                return result
            except Exception as e:
                raise ParseError(self.path, f"SNBT parsing failed: {e}") from e
        else:
            # Fall back to regex extraction when ftb_snbt_lib is unavailable
            logger.warning(
                "ftb_snbt_lib not available, using fallback regex parsing for %s",
                self.path,
            )
            return self._parse_snbt_fallback(content)

    async def dump(self, data: Mapping[str, str]) -> None:
        """Write translated data back in SNBT format."""
        if not HAS_SNBT_LIB:
            raise DumpError(
                self.path,
                "The ftb_snbt_lib library is required to dump SNBT files",
            )

        logger.info("Dumping SNBT file: %s", self.path)

        try:
            # Reload the original SNBT structure
            source_path = self.original_path if self.original_path else self.path
            async with aiofiles.open(
                source_path,
                encoding="utf-8",
                errors="replace",
            ) as f:
                original_content = await f.read()

            original_data = slib.loads(original_content)

            # Swap translated values into the ORIGINAL tag tree. Every other
            # tag keeps its own type (1b, 2.0f, 3L, [I; ...], true) and its
            # text: rebuilding the tree from plain Python values turned them
            # into ints, doubles and plain lists.
            escaped = {key: self._replace_ampersand(value) for key, value in data.items()}
            snbt_content = slib.dumps(self._unflatten_snbt(original_data, flat_data=escaped))

            # Write the file asynchronously
            async with aiofiles.open(self.path, "w", encoding="utf-8") as f:
                await f.write(snbt_content)

            logger.debug("Successfully wrote %d entries to %s", len(data), self.path)

        except Exception as e:
            raise DumpError(self.path, f"Could not write SNBT: {e}") from e

    def _parse_snbt_fallback(self, content: str) -> dict[str, str]:
        """Basic SNBT string extraction without ftb_snbt_lib."""
        mapping: dict[str, str] = {}

        # Extract string literals
        string_re = re.compile(r'"([^"\\]*(?:\\.[^"\\]*)*)"')
        for idx, match in enumerate(string_re.finditer(content), start=1):
            mapping[str(idx)] = match.group(1)

        return mapping

    def _flatten_snbt(self, data: Any, prefix: str = "") -> dict[str, str]:
        """Flatten an SNBT structure, extracting only string values."""
        result: dict[str, str] = {}

        if isinstance(data, dict):
            for key, value in data.items():
                new_key = f"{prefix}.{key}" if prefix else key
                if isinstance(value, str):
                    result[new_key] = value
                elif isinstance(value, (dict, list)):
                    result.update(self._flatten_snbt(value, new_key))
        elif isinstance(data, list):
            for i, item in enumerate(data):
                new_key = f"{prefix}[{i}]" if prefix else f"[{i}]"
                if isinstance(item, str):
                    result[new_key] = item
                elif isinstance(item, (dict, list)):
                    result.update(self._flatten_snbt(item, new_key))

        return result

    def _unflatten_snbt(self, original: Any, flat_data: Mapping[str, str]) -> Any:
        """Restore flattened data into the original SNBT structure."""
        # Walk the original structure recursively, swapping in translated values
        return self._update_structure_recursive(original, flat_data, "")

    def _update_structure_recursive(
        self, obj: Any, flat_data: Mapping[str, str], prefix: str
    ) -> Any:
        """Recursively walk the structure, replacing values with translations."""
        # Check for ftb_snbt_lib tag types
        try:
            from ftb_snbt_lib.tag import (
                ByteArray,
                Compound,
                IntArray,
                LongArray,
                String,
            )
            from ftb_snbt_lib.tag import List as SNBTList

            # Return array types as-is without traversal (preserve structure)
            if isinstance(obj, (ByteArray, IntArray, LongArray)):
                return obj

            # SNBT Compound objects
            if isinstance(obj, Compound):
                result_dict = {}
                for key, value in obj.items():
                    new_key = f"{prefix}.{key}" if prefix else key

                    # SNBT String object with a translation
                    if isinstance(value, String) and new_key in flat_data:
                        result_dict[key] = String(flat_data[new_key])
                    # Plain string with a translation
                    elif isinstance(value, str) and new_key in flat_data:
                        result_dict[key] = flat_data[new_key]
                    # Recurse into nested structures
                    elif isinstance(value, (Compound, SNBTList, dict, list)):
                        result_dict[key] = self._update_structure_recursive(
                            value, flat_data, new_key
                        )
                    else:
                        # Otherwise keep the original value
                        result_dict[key] = value
                return Compound(result_dict)

            # SNBT List objects
            elif isinstance(obj, SNBTList):
                result_list = []
                for i, item in enumerate(obj):
                    new_key = f"{prefix}[{i}]" if prefix else f"[{i}]"

                    # SNBT String object with a translation
                    if isinstance(item, String) and new_key in flat_data:
                        result_list.append(String(flat_data[new_key]))
                    # Plain string with a translation
                    elif isinstance(item, str) and new_key in flat_data:
                        result_list.append(flat_data[new_key])
                    # Recurse into nested structures
                    elif isinstance(item, (Compound, SNBTList, dict, list)):
                        result_list.append(
                            self._update_structure_recursive(item, flat_data, new_key)
                        )
                    else:
                        # Otherwise keep the original value
                        result_list.append(item)
                return SNBTList(result_list)

            # SNBT String objects
            elif isinstance(obj, String) and prefix in flat_data:
                return String(flat_data[prefix])

        except ImportError:
            # ftb_snbt_lib missing: fall through to plain-Python handling
            pass

        # Plain Python objects
        if isinstance(obj, dict):
            # Process each key-value pair
            result = {}
            for key, value in obj.items():
                new_key = f"{prefix}.{key}" if prefix else key

                if isinstance(value, str) and new_key in flat_data:
                    # Replace string values that have a translation
                    result[key] = flat_data[new_key]
                elif isinstance(value, (dict, list)):
                    # Recurse into nested structures
                    result[key] = self._update_structure_recursive(
                        value, flat_data, new_key
                    )
                else:
                    # Otherwise keep the original value
                    result[key] = value
            return result

        elif isinstance(obj, list):
            # Process each list item
            result_list_py: list[Any] = []
            for i, item in enumerate(obj):
                new_key = f"{prefix}[{i}]" if prefix else f"[{i}]"

                if isinstance(item, str) and new_key in flat_data:
                    # Replace string values that have a translation
                    result_list_py.append(flat_data[new_key])
                elif isinstance(item, (dict, list)):
                    # Recurse into nested structures
                    result_list_py.append(
                        self._update_structure_recursive(item, flat_data, new_key)
                    )
                else:
                    # Otherwise keep the original value
                    result_list_py.append(item)
            return result_list_py

        else:
            # Strings and other primitive types
            if isinstance(obj, str) and prefix in flat_data:
                return flat_data[prefix]
            return obj

    @staticmethod
    def _replace_ampersand(obj: Any) -> Any:
        """Escape ``&`` characters in an object (Minecraft color codes excluded)."""
        if isinstance(obj, str):
            # Keep Minecraft color codes (&0-&9, &a-&f, &k-&o, &r) unescaped,
            # and an already escaped "\&" as it is: output read back in (a
            # migration, a re-run) must not grow another backslash each time.
            pattern = r"(?<!\\)&(?![0-9a-fk-or])"
            return re.sub(pattern, r"\\&", re.sub(r"\n", r"\\n", obj))
        elif isinstance(obj, dict):
            return {k: SNBTParser._replace_ampersand(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [SNBTParser._replace_ampersand(item) for item in obj]
        else:
            return obj
