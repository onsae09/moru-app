"""Safe extraction of user-facing string literals from KubeJS scripts.

KubeJS scripts frequently register user-facing text directly in JavaScript
instead of language JSON: item names (``.displayName('…')``), tooltips
(``.tooltip('…')`` on a builder, ``event.add(item, '…')`` in
``ItemEvents.tooltip``) and text components (``Text.of('…')``,
``Text.gold('…')``, ``Component.literal('…')``). Translating the whole script
would put executable code in the model's hands, so this handler exposes only
direct string/template-literal arguments of those calls and splices the
reviewed translations back into an otherwise byte-for-byte copy of the file.

``Text.translate``/``Component.translatable`` take a language KEY, never
prose, and are left alone.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

import aiofiles

from .base import ContentHandler

if TYPE_CHECKING:
    from collections.abc import Mapping


logger = logging.getLogger(__name__)

_DISPLAY_NAME_CALL_RE = re.compile(r"\.displayName\s*\(\s*")

#: Builder tooltips (``event.create('x').tooltip('…')``).
_TOOLTIP_CALL_RE = re.compile(r"\.tooltip\s*\(\s*")

#: Text components whose single argument is literal display text. The color
#: shorthands are KubeJS's ``Text.<color>(…)`` helpers.
_TEXT_CALL_RE = re.compile(
    r"\b(?:Text\.(?:of|literal|string|black|darkBlue|darkGreen|darkAqua|darkRed|"
    r"darkPurple|gold|gray|darkGray|blue|green|aqua|red|lightPurple|yellow|white)"
    r"|Component\.literal)\s*\(\s*"
)

#: ``event.add(<item>, '…')`` inside ``ItemEvents.tooltip``: the item is a
#: quoted id (``mod:item``, ``#mod:tag``) or an array of them; only the
#: SECOND argument is text. The same call shape registers TAGS in server
#: scripts (``event.add('forge:ingots', 'kubejs:ingot')``) and lang entries
#: in ``ClientEvents.lang`` (``event.add('item.x', 'Name')``, key without a
#: colon), so it is read only in client scripts, only after an id-shaped
#: first argument, and never when the "text" is itself an id.
_ID = r"#?[a-z0-9_.-]+:[a-z0-9_./-]+"
_TOOLTIP_ADD_RE = re.compile(
    r"\b(?:event|e|evt|tooltip)\.add\s*\(\s*"
    rf"(?:'{_ID}'|\"{_ID}\"|\[(?:\s*(?:'{_ID}'|\"{_ID}\")\s*,?)+\s*\])"
    r"\s*,\s*"
)

#: A literal that is a resource id or tag, not prose.
_RESOURCE_ID_RE = re.compile(rf"^{_ID}$")


@dataclass(frozen=True, slots=True)
class _DisplayNameLiteral:
    key: str
    quote: str
    text: str
    start: int
    end: int


def _region_map(source: str) -> list[str]:
    """Classify each character as ``code``, ``comment`` or ``literal``."""
    regions = ["code"] * len(source)
    state = "code"
    cursor = 0
    while cursor < len(source):
        char = source[cursor]
        next_char = source[cursor + 1] if cursor + 1 < len(source) else ""
        if state == "code":
            if char == "/" and next_char == "/":
                state = "line_comment"
                regions[cursor] = regions[cursor + 1] = "comment"
                cursor += 2
                continue
            if char == "/" and next_char == "*":
                state = "block_comment"
                regions[cursor] = regions[cursor + 1] = "comment"
                cursor += 2
                continue
            if char in {"'", '"', "`"}:
                state = char
                regions[cursor] = "literal"
            cursor += 1
            continue
        if state == "line_comment":
            if char in "\r\n":
                state = "code"
            else:
                regions[cursor] = "comment"
            cursor += 1
            continue
        if state == "block_comment":
            regions[cursor] = "comment"
            if char == "*" and next_char == "/":
                state = "code"
                regions[cursor + 1] = "comment"
                cursor += 2
            else:
                cursor += 1
            continue
        # A quote-named state is a JS string/template literal.
        regions[cursor] = "literal"
        if char == "\\":
            if cursor + 1 < len(source):
                regions[cursor + 1] = "literal"
            cursor += 2
            continue
        if char == state:
            state = "code"
        cursor += 1
    return regions


def _literal_end(source: str, start: int) -> int | None:
    """Index of the closing quote of the literal opening at ``start``."""
    quote = source[start]
    cursor = start + 1
    while cursor < len(source):
        if source[cursor] == "\\":
            cursor += 2
            continue
        if source[cursor] == quote:
            return cursor
        cursor += 1
    return None


def _call_literals(
    source: str,
    path: Path,
    regions: list[str],
    call_re: re.Pattern[str],
    label: str,
    closers: str = ")",
) -> list[tuple[str, int, int]]:
    """(quote, start, end) of each direct literal argument ``call_re`` opens.

    The literal must be the whole argument: followed by one of ``closers``
    (``)``, or ``,``/``)`` for a list element). Anything else — a
    concatenation, a method call on the literal — is an expression and is
    left alone.
    """
    found: list[tuple[str, int, int]] = []
    for call in call_re.finditer(source):
        region = regions[call.start()]
        if region != "code":
            if region == "literal":
                # The scanner has no regex-literal state, so a pattern such as
                # ``/'/`` shifts every later boundary. Skipping is safe (extract
                # and apply agree), but the miss must not be silent.
                logger.warning(
                    "Skipped %s at %s:%d: scanned as string literal",
                    label,
                    path,
                    source.count("\n", 0, call.start()) + 1,
                )
            continue
        start = call.end()
        if start >= len(source) or source[start] not in {"'", '"', "`"}:
            continue
        end = _literal_end(source, start)
        if end is None:
            continue
        tail = end + 1
        while tail < len(source) and source[tail].isspace():
            tail += 1
        if tail >= len(source) or source[tail] not in closers:
            continue
        found.append((source[start], start + 1, end))
    return found


#: Call shapes beside ``displayName``: key prefix, pattern, and whether the
#: shape is read only in ``client_scripts/``.
_EXTRA_CALLS: tuple[tuple[str, re.Pattern[str], bool], ...] = (
    ("tooltip", _TOOLTIP_CALL_RE, False),
    ("tooltip", _TOOLTIP_ADD_RE, True),
    ("text", _TEXT_CALL_RE, False),
)


def _is_client_script(path: Path) -> bool:
    return "client_scripts" in (part.lower() for part in path.parts)


def _display_name_literals(source: str, path: Path) -> list[_DisplayNameLiteral]:
    """Return direct quoted arguments, ignoring expressions and comments.

    The small scanner deliberately understands only JavaScript string and
    comment boundaries. Calls such as
    ``displayName(Component.translatable(...))`` remain untouched, while both
    ordinary strings and template literals such as ```Orb of ${name}``` are
    safe to translate.

    ``display_name.NNNN`` keys number displayName calls exactly as before
    tooltips and text components were extracted, so earlier sessions and
    reviewed translations keep their keys; the other shapes number their own
    ``tooltip.NNNN`` / ``text.NNNN`` sequences in file order.
    """
    regions = _region_map(source)
    found: list[_DisplayNameLiteral] = []
    taken: set[int] = set()
    for quote, start, end in _call_literals(source, path, regions, _DISPLAY_NAME_CALL_RE, "displayName"):
        taken.add(start)
        found.append(
            _DisplayNameLiteral(
                key=f"display_name.{len(found) + 1:04d}",
                quote=quote,
                text=source[start:end],
                start=start,
                end=end,
            )
        )
    extra: list[tuple[str, str, int, int]] = []
    client = _is_client_script(path)
    for prefix, call_re, client_only in _EXTRA_CALLS:
        if client_only and not client:
            continue
        for quote, start, end in _call_literals(source, path, regions, call_re, prefix):
            if _RESOURCE_ID_RE.match(source[start:end]):
                continue
            if start not in taken:
                taken.add(start)
                extra.append((prefix, quote, start, end))
    counters: dict[str, int] = {}
    for prefix, quote, start, end in sorted(extra, key=lambda item: item[2]):
        counters[prefix] = counters.get(prefix, 0) + 1
        found.append(
            _DisplayNameLiteral(
                key=f"{prefix}.{counters[prefix]:04d}",
                quote=quote,
                text=source[start:end],
                start=start,
                end=end,
            )
        )
    return found


#: Raw line terminators end a quoted JS literal; escaping them keeps the same
#: runtime text in both quoted strings and multiline template literals.
_LINE_TERMINATOR_ESCAPES = {
    "\n": "\\n",
    "\r": "\\r",
    "\u2028": "\\u2028",
    "\u2029": "\\u2029",
}


def _escape_delimiter(text: str, delimiter: str) -> str:
    """Escape delimiters and line breaks without disturbing existing JS escapes."""
    output: list[str] = []
    backslashes = 0
    for char in text:
        escape = _LINE_TERMINATOR_ESCAPES.get(char)
        if escape is not None:
            output.append(escape)
            backslashes = 0
            continue
        if char == delimiter and backslashes % 2 == 0:
            output.append("\\")
        output.append(char)
        if char == "\\":
            backslashes += 1
        else:
            backslashes = 0
    if backslashes % 2:
        # A dangling backslash would otherwise escape the closing delimiter.
        output.append("\\")
    return "".join(output)


class KubeJSDisplayNameHandler(ContentHandler):
    """Translate only direct display-text literals in KubeJS JS/TS."""

    name: ClassVar[str] = "kubejs_display_name"
    priority: ClassVar[int] = 14
    extensions: ClassVar[tuple[str, ...]] = (".js", ".ts")

    def can_handle(self, path: Path) -> bool:
        normalized = path.as_posix().lower()
        in_kubejs = normalized.startswith("kubejs/") or "/kubejs/" in normalized
        return in_kubejs and path.suffix.lower() in self.extensions

    async def extract(self, path: Path) -> Mapping[str, str]:
        async with aiofiles.open(path, encoding="utf-8", errors="replace") as file:
            source = await file.read()
        literals = _display_name_literals(source, path)
        return {literal.key: literal.text for literal in literals}

    async def apply(
        self,
        path: Path,
        translations: Mapping[str, str],
        output_path: Path | None = None,
    ) -> None:
        async with aiofiles.open(path, encoding="utf-8", errors="replace") as file:
            source = await file.read()
        replacements = []
        for literal in _display_name_literals(source, path):
            translated = translations.get(literal.key)
            if translated is None:
                continue
            replacements.append(
                (
                    literal.start,
                    literal.end,
                    _escape_delimiter(translated, literal.quote),
                )
            )
        for start, end, translated in reversed(replacements):
            source = source[:start] + translated + source[end:]

        target = output_path or path
        target.parent.mkdir(parents=True, exist_ok=True)
        async with aiofiles.open(target, "w", encoding="utf-8") as file:
            await file.write(source)
