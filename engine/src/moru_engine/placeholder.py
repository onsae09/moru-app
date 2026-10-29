"""Placeholder protection utilities for translation."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

#: Flags, width, precision and conversion of a java.util.Formatter specifier
#: (everything after "%" and the optional "n$" index). Shared with the
#: validator so protection and validation see the same specifiers.
JAVA_FORMAT_BODY = r"[-#+0,(]*\d*(?:\.\d+)?[sdifxXobeEgGaAcChHnp]"
JAVA_FORMAT_RE = re.compile(rf"%(?:\d+\$)?{JAVA_FORMAT_BODY}")
NUMBERED_FORMAT_RE = re.compile(rf"%(\d+)\${JAVA_FORMAT_BODY}")

# Placeholder patterns. Dict order is the overlap priority: when two
# patterns match overlapping spans, the earlier pattern keeps its match
# and the later one is dropped (see protect()).
PATTERNS = {
    # Patchouli guidebook macros: $(li), $(br2), $(l:entry/path), $(#f00),
    # $(k:inventory), $(). FIRST in the table on purpose: the game's own
    # parser (BookTextParser.COMMAND_PATTERN, verbatim `\$\(([^)]*)\)`)
    # treats the whole span as one atomic command, so anything inside it
    # is macro syntax and never a placeholder of its own. Letting a nested
    # match win instead would drop the macro and hand its "$(" and ")" to
    # the model bare. Unlike angle brackets, "$(" carries no prose usage —
    # 8642 occurrences across 269 real books are all macros — so the
    # body stays unrestricted, matching the game byte for byte.
    "patchouli_macro": re.compile(r"\$\([^)]*\)"),
    # JavaScript template interpolation used by KubeJS display names.
    # Keep the leading '$' inside the token so translation cannot separate it
    # from the expression when reordering words for the target language.
    #
    # Must stay AHEAD of "named_placeholder": that pattern matches the inner
    # "{...}" of "${...}", so claiming it first would drop this wider span
    # and leave the "$" outside the token as loose prose — the very thing
    # this entry exists to prevent.
    "js_template": re.compile(r"\$\{[^{}]*\}"),
    # Java format specifiers: %s, %d, %1$s, %2$d, %.1f, %,d, %02d, %-5s ...
    # The space flag is left out on purpose: "50% done" would read as "% d".
    "java_format": re.compile(rf"%%|%(?:\d+\$)?{JAVA_FORMAT_BODY}"),
    # Minecraft color codes: §a, §0-§f, §k-§o, §r
    "mc_color_section": re.compile(r"§[0-9a-fk-or]", re.IGNORECASE),
    # Alternative color codes: &a, &0-&f, etc.
    "mc_color_ampersand": re.compile(r"&[0-9a-fk-or]", re.IGNORECASE),
    # Named placeholders: {player}, {item}, etc.
    "named_placeholder": re.compile(r"\{[^}]+\}"),
    # XML-like tags: <b>, </b>, <color=red>, <font color="red">, <br />.
    # Whitespace inside a tag is allowed ONLY for name=value attributes
    # (or a spaced self-close), so prose in angle brackets
    # ("<Error occurred, plz report to %s>") stays translatable instead
    # of being frozen as one opaque token.
    "xml_tags": re.compile(
        # whitespace-free tags: <b>, </b>, <color=red>, <#FF0000>, <br/>
        r"<[^>\s]+>"
        # attribute tags — every attribute must carry =value (bare words
        # after the name read as prose): <font color="red" size=2>
        r"|<[A-Za-z][\w:.-]*"
        r"(?:\s+[A-Za-z][\w:.-]*\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+))+"
        r"\s*/?>"
        # spaced self-closing tag: <br />
        r"|<[A-Za-z][\w:.-]*\s*/>"
    ),
    # Newlines and special characters
    "special_chars": re.compile(r"\\[nrt]"),
}

# Semantic token kinds: the LLM sees WHAT each token is so it can place it
# naturally in the target language (a color span must keep wrapping the
# same words after reordering).
#
# Numbering policy: a number identifies WHICH literal a token stands for,
# so it is added only when one entry mixes two or more distinct literals
# of the same kind ("&6..&a" -> {{COLOR1}}/{{COLOR2}}). When every
# occurrence of a kind is the same literal — always true for RESET ("&r")
# and BR ("\n") — the bare {{KIND}} token is used for every occurrence:
# spurious numbers measurably confuse small models into inventing or
# renumbering tokens. Occurrences of the SAME literal share one token, so
# restore() stays a literal, order-free replacement either way.
TOKEN_KIND_BY_PATTERN = {
    "js_template": "VAR",
    "java_format": "ARG",
    "named_placeholder": "VAR",
    "xml_tags": "TAG",
    "special_chars": "BR",
}

# Any protected token: {{COLOR}}, {{RESET}}, {{ARG1}}, {{VAR2}}, ...
TOKEN_RE = re.compile(r"\{\{[A-Z]+\d*\}\}")

# An UNINDEXED java specifier consumes the NEXT argument, so every
# occurrence stands for a different one ("%1$s" names a fixed argument and
# "%%" is a literal percent — neither is order-sensitive). The char class
# mirrors PATTERNS["java_format"].
_POSITIONAL_ARG_RE = re.compile(rf"^%{JAVA_FORMAT_BODY}$")


#: Macro bodies that Patchouli renders as a line break: ``$(br)``/``$(br2)``
#: (``$(p)``/``$(2br)`` are aliases) and the ``$(li)``/``$(li2)`` list
#: bullets, which break the line and indent (BookTextParser.listProcessor).
_PATCHOULI_BREAK_RE = re.compile(r"br2?|2br|p|li\d?")

#: Bodies that reset the span style: "" plus its ``reset``/``clear``
#: aliases, and ``nocolor`` which drops back to the base color.
_PATCHOULI_RESET_BODIES = frozenset({"", "reset", "clear", "nocolor"})

#: Span-closing commands: link, tooltip and command-link ends.
_PATCHOULI_CLOSE_BODIES = frozenset({"/l", "/t", "/c"})

#: ``$(playername)`` substitutes a runtime value, like ``{player}`` does.
_PATCHOULI_VALUE_BODIES = frozenset({"playername"})


def _classify_patchouli_kind(body: str) -> str:
    """Semantic kind for the body of a ``$(...)`` macro.

    Mapped onto the EXISTING kind vocabulary the translator prompt
    documents ({{COLOR}}/{{RESET}}/{{VAR}}/{{TAG}}/{{BR}}) rather than
    inventing patchouli-specific kinds the model has never been told
    about. The split follows BookTextParser's own dispatch:

    * breaks and list bullets -> BR (they end the line)
    * "", reset, clear, nocolor -> RESET (mirrors "§r")
    * "l:x"/"t:x"/"c:x" and their "/l"/"/t"/"/c" ends -> TAG, the wrapping
      markup kind that already covers "<a href=...>".."</a>"
    * "k:keybind" and playername -> VAR, a value substituted at render time
    * everything else -> COLOR, the color/format span kind: single
      "[0-9a-f]" codes, "#rrggbb", the "l"/"o"/"m"/"n"/"k" format codes
      and their word aliases, and book-defined macros (``$(item)`` and
      ``$(thing)`` expand to ``$(#b0b)``/``$(#490)``).
    """
    if body in _PATCHOULI_RESET_BODIES:
        return "RESET"
    if _PATCHOULI_BREAK_RE.fullmatch(body):
        return "BR"
    if body in _PATCHOULI_CLOSE_BODIES:
        return "TAG"
    # "l:entry" is a link but bare "l" is bold, so the parameter form is
    # what distinguishes them (BookTextParser.lookupFunctionProcessor
    # requires the colon at index > 0).
    name, colon, _ = body.partition(":")
    if colon and name:
        return "VAR" if name == "k" else "TAG"
    if body in _PATCHOULI_VALUE_BODIES:
        return "VAR"
    return "COLOR"


def _classify_kind(pattern_name: str, original: str) -> str:
    """Semantic kind for a matched literal ("&r" is RESET, "&6" COLOR)."""
    if pattern_name in ("mc_color_section", "mc_color_ampersand"):
        return "RESET" if original[1].lower() == "r" else "COLOR"
    if pattern_name == "patchouli_macro":
        return _classify_patchouli_kind(original[2:-1])
    return TOKEN_KIND_BY_PATTERN.get(pattern_name, "PH")


class PlaceholderError(ValueError):
    """Exception raised when placeholder restoration fails."""

    pass


@dataclass
class PlaceholderInfo:
    """Information about a placeholder in text."""

    original: str
    token: str
    pattern_name: str
    position: int


@dataclass
class ProtectedText:
    """Text with placeholders replaced by tokens."""

    original: str
    protected: str
    placeholders: list[PlaceholderInfo] = field(default_factory=list)

    def collapsed_positional_arg(self) -> tuple[str, str, int] | None:
        """The ARG token that stands for SEVERAL distinct arguments.

        Sharing one token per literal is order-free for formatting kinds,
        but LOSSY for unindexed positional specifiers: "Added %s to %s for
        %s (now %s)" has four distinct arguments and a single {{ARG}}, so
        the token can no longer say WHICH argument it is. Korean word order
        forces exactly such a reorder, and a model that reorders can only
        express it by numbering the occurrences itself.

        Returns:
            ``(token, literal, occurrences)`` when the source collapsed two
            or more distinct arguments into one token, else None.
        """
        token: str | None = None
        literal = ""
        occurrences = 0
        for placeholder in self.placeholders:
            if placeholder.pattern_name != "ARG":
                continue
            if token is None:
                token, literal = placeholder.token, placeholder.original
            elif placeholder.token != token:
                # Mixed ARG literals are already numbered BY LITERAL, so a
                # per-occurrence number would be ambiguous — stay strict.
                return None
            occurrences += 1
        if token is None or occurrences < 2:
            return None
        if _POSITIONAL_ARG_RE.match(literal) is None:
            return None
        return token, literal, occurrences

    def _resolve_collapsed_arg(self, translated: str) -> tuple[str, str] | None:
        """Rewrite per-occurrence ARG numbering into indexed literals.

        A source-side ambiguity must never hard-fail an otherwise good
        translation, and {{ARGk}} over a collapsed {{ARG}} is repairable
        with certainty: k is argument k, whose java form is "%k$s". The
        result is exact rather than merely accepted — the arguments keep
        the meaning the source gave them even after a reorder.

        Returns:
            ``(result, token)`` with every {{ARGk}} replaced and the
            consumed bare token, or None when the numbering is absent or
            incomplete (then the strict roundtrip applies unchanged).
        """
        collapsed = self.collapsed_positional_arg()
        if collapsed is None:
            return None
        token, literal, occurrences = collapsed
        if token in translated:
            return None  # mixes bare and numbered forms: not a clean rewrite
        numbered = [f"{{{{ARG{index}}}}}" for index in range(1, occurrences + 1)]
        if any(translated.count(form) != 1 for form in numbered):
            return None
        result = translated
        # "{{ARG1}}" cannot match inside "{{ARG10}}" — tokens end in "}}".
        for index, form in enumerate(numbered, start=1):
            result = result.replace(form, f"%{index}${literal[1]}")
        logger.info(
            "Resolved %d collapsed positional argument(s) from per-occurrence "
            "numbering. Original: '%s', Translated: '%s'",
            occurrences,
            self.original,
            translated,
        )
        return result, token

    def restore(self, translated: str) -> str:
        """Restore placeholders in translated text.

        The roundtrip is strict: every token must occur in the translation
        exactly as many times as in the protected source. Occurrences of
        one literal share one token, so the check is per unique token.

        The one exception is a source-side ambiguity the token scheme
        cannot express: repeated unindexed positional specifiers collapse
        into a single token, and per-occurrence numbering of THAT token is
        rewritten into indexed literals instead of failing the entry (see
        ``_resolve_collapsed_arg``). The reorder then surfaces as a
        non-blocking PLACEHOLDER_ORDER warning from the validator.

        Args:
            translated: Translated text with tokens.

        Returns:
            Translated text with original placeholders restored.

        Raises:
            PlaceholderError: On any missing, surplus, or unknown token.
        """
        result = translated

        literal_by_token: dict[str, str] = {}
        expected_counts: dict[str, int] = {}
        for placeholder in self.placeholders:
            literal_by_token[placeholder.token] = placeholder.original
            expected_counts[placeholder.token] = (
                expected_counts.get(placeholder.token, 0) + 1
            )

        resolved = self._resolve_collapsed_arg(result)
        if resolved is not None:
            result, consumed = resolved
            del literal_by_token[consumed]
            del expected_counts[consumed]

        count_issues = []
        # Longest token first so a numbered form can never be corrupted by
        # replacing a shorter one.
        for token in sorted(literal_by_token, key=len, reverse=True):
            actual = result.count(token)
            expected = expected_counts[token]
            if actual != expected:
                count_issues.append(f"{token} x{actual} (expected x{expected})")
                logger.error(
                    "Placeholder token '%s' occurs %d time(s), expected %d. "
                    "Original: '%s', Translated: '%s'",
                    token,
                    actual,
                    expected,
                    self.original,
                    translated,
                )
                continue
            result = result.replace(token, literal_by_token[token])

        # Anything still token-shaped is unknown or was left by a count
        # mismatch above.
        has_issues = bool(count_issues)
        if TOKEN_RE.search(result):
            logger.error(
                "Unrestored placeholder tokens remaining in translation. "
                "Original: '%s', Result: '%s'",
                self.original,
                result,
            )
            has_issues = True

        if has_issues:
            raise PlaceholderError(
                f"Placeholder validation failed for original text: '{self.original}'"
            )

        return result


class PlaceholderProtector:
    """Protects placeholders during translation.

    Replaces placeholders with unique tokens before translation
    and restores them afterward.
    """

    def __init__(self) -> None:
        """Initialize the protector (stateless; kept for API stability)."""

    def protect(self, text: str) -> ProtectedText:
        """Replace placeholders with tokens.

        Args:
            text: Original text with placeholders.

        Returns:
            Protected text info.
        """
        protected = text
        placeholders: list[PlaceholderInfo] = []

        matches: list[tuple[int, str, str]] = []  # (position, literal, kind)
        claimed: list[tuple[int, int]] = []  # spans owned by earlier patterns
        for pattern_name, pattern in PATTERNS.items():
            for match in pattern.finditer(text):
                start, end = match.span()
                # A match overlapping an earlier pattern's span is dropped
                # (PATTERNS order is the priority). Keeping both corrupts
                # restore(): replacing the inner literal shifts the text,
                # so the outer literal's recorded position/length go stale
                # ("<...%s>" used to restore as "{{TAG}}RG}}>").
                if any(start < c_end and c_start < end for c_start, c_end in claimed):
                    continue
                claimed.append((start, end))
                original = match.group(0)
                matches.append(
                    (start, original, _classify_kind(pattern_name, original))
                )

        # Number tokens per kind ONLY when that kind mixes distinct
        # literals; the number then identifies the literal (not the
        # occurrence), assigned in first-appearance order.
        matches.sort(key=lambda m: m[0])
        literals_by_kind: dict[str, list[str]] = {}
        for _, original, kind in matches:
            seen = literals_by_kind.setdefault(kind, [])
            if original not in seen:
                seen.append(original)

        for position, original, kind in matches:
            literals = literals_by_kind[kind]
            suffix = "" if len(literals) == 1 else str(literals.index(original) + 1)
            placeholders.append(
                PlaceholderInfo(
                    original=original,
                    token=f"{{{{{kind}{suffix}}}}}",
                    pattern_name=kind,
                    position=position,
                )
            )

        # Sort by position (reverse) to replace from end to start
        placeholders.sort(key=lambda p: p.position, reverse=True)

        for placeholder in placeholders:
            protected = (
                protected[: placeholder.position]
                + placeholder.token
                + protected[placeholder.position + len(placeholder.original) :]
            )

        return ProtectedText(
            original=text,
            protected=protected,
            placeholders=placeholders,
        )

    def is_only_placeholders(self, protected_text: ProtectedText) -> bool:
        """Check if protected text contains only placeholder tokens.

        Args:
            protected_text: Protected text to check.

        Returns:
            True if text contains only tokens and whitespace.
        """
        if not protected_text.placeholders:
            return False

        # Remove all tokens and check if anything remains
        temp = protected_text.protected
        for placeholder in protected_text.placeholders:
            temp = temp.replace(placeholder.token, "")

        # If only whitespace remains, it's placeholder-only
        return temp.strip() == ""

    @staticmethod
    def count_placeholders(text: str) -> dict[str, int]:
        """Count placeholders by type.

        Args:
            text: Text to analyze.

        Returns:
            Dictionary of pattern names to counts.
        """
        counts: dict[str, int] = {}

        for name, pattern in PATTERNS.items():
            matches = pattern.findall(text)
            if matches:
                counts[name] = len(matches)

        return counts
