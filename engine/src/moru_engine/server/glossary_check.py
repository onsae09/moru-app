"""Which review entries miss a glossary term they should carry.

The review screen's "glossary" filter needs this answer for every entry of a
run at once, which the validator's per-pair API was never shaped for: it
checks every rule against every string, and a run is tens of thousands of
strings against hundreds of rules. Two things keep it cheap here:

* rules are indexed by the first word of each alias, so an entry is only
  matched against rules that could possibly fire on its source;
* the answer is cached per entry and recomputed only when that entry's
  source or translation changed, so a commit costs one entry, not the run.

The matching itself is the validator's: the same CJK-safe alias regex, and a
miss means the expected target text is absent from the translation. One
deliberate difference: a rule scoped away from the entry's key (``key_scope``)
does not apply to it, exactly as it does not apply during translation.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from ..validator import TranslationValidator
from .manual_journal import entry_ref

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ..models.glossary import Glossary
    from ..pipeline import EntryResult

_WORD_RE = re.compile(r"\w+")


class _Rule:
    __slots__ = ("expected", "patterns", "term")

    def __init__(self, expected: str, patterns: list[re.Pattern[str]], term: object) -> None:
        self.expected = expected
        self.patterns = patterns
        #: The TermRule for scoped rules, else None (proper nouns are unscoped).
        self.term = term


class GlossaryMissIndex:
    """Glossary rules indexed for bulk "is a required term missing" checks."""

    def __init__(self, glossary: Glossary) -> None:
        self._by_word: dict[str, list[_Rule]] = {}
        #: Rules with an alias whose first word may sit inside a longer \w run
        #: (CJK text has no spaces), so no word index can rule them out.
        self._always: list[_Rule] = []
        compile_alias = TranslationValidator._compile_cjk_safe_alias  # noqa: SLF001
        for term in glossary.term_rules:
            self._add(
                term.term_ko,
                [a for a in term.aliases if a.strip()],
                term if term.key_scope else None,
                compile_alias,
            )
        for noun in glossary.proper_noun_rules:
            self._add(
                noun.preferred_ko,
                [a for a in (noun.source_like, *noun.aliases) if a.strip()],
                None,
                compile_alias,
            )
        self._cache: dict[str, tuple[str, str, tuple[str, ...]]] = {}

    def _add(self, expected, aliases, term, compile_alias) -> None:  # noqa: ANN001
        if not expected or not aliases:
            return
        rule = _Rule(expected, [compile_alias(a) for a in aliases], term)
        heads: set[str] = set()
        for alias in aliases:
            words = _WORD_RE.findall(alias.lower())
            if not words or not words[0].isascii():
                self._always.append(rule)
                return
            heads.add(words[0])
        for head in heads:
            self._by_word.setdefault(head, []).append(rule)

    @property
    def empty(self) -> bool:
        return not self._by_word and not self._always

    def misses(self, key: str, source: str, translated: str) -> tuple[str, ...]:
        """Expected target terms absent from ``translated``, in rule order."""
        if not translated.strip() or not source:
            return ()
        lowered = source.lower()
        seen: set[int] = set()
        out: list[str] = []
        candidates: list[_Rule] = list(self._always)
        for word in set(_WORD_RE.findall(lowered)):
            candidates.extend(self._by_word.get(word, ()))
        for rule in candidates:
            if id(rule) in seen:
                continue
            seen.add(id(rule))
            if rule.expected in translated or rule.expected in out:
                continue
            if rule.term is not None and rule.term.scope_rank(key) is None:  # type: ignore[attr-defined]
                continue
            if any(p.search(lowered) for p in rule.patterns):
                out.append(rule.expected)
        return tuple(out)

    def for_entries(self, entries: Sequence[EntryResult]) -> dict[str, tuple[str, ...]]:
        """Every entry that misses a term, by journal ref. Cached per entry."""
        if self.empty:
            return {}
        out: dict[str, tuple[str, ...]] = {}
        for e in entries:
            ref = entry_ref(e.file, e.key)
            translated = e.translated_text or ""
            cached = self._cache.get(ref)
            if cached is not None and cached[0] == e.source_text and cached[1] == translated:
                found = cached[2]
            else:
                found = self.misses(e.key, e.source_text or "", translated)
                self._cache[ref] = (e.source_text, translated, found)
            if found:
                out[ref] = found
        return out
