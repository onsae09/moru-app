"""In-memory relationship index over a modpack's translatable entries.

Like a code knowledge graph indexes symbols and call edges, this module
indexes the relationships between a pack's entries at scan time:

- ``defines``: name-defining entries (lang name keys) per surface form.
- ``mentions``: entries whose body text mentions a defined surface.
- ``sibling``: entries sharing a key stem within one file
  (``quests[0].title`` <-> ``quests[0].description[*]``).

The graph is derived data, rebuilt in-memory per run (seconds for ~10k
entries); persistence across sessions is the translation memory's job.
It powers name-first wave scheduling, freshly-settled-name glossary
bindings, and sibling context injection in the pipeline orchestrator.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from ..glossary.pair_harvester import is_untranslated_copy
from ..glossary.term_miner import NAME_KEY_RE, clean_text, is_name_value
from ..models import Glossary, TermRule

if TYPE_CHECKING:
    from collections.abc import Container, Iterable, Mapping

__all__ = [
    "SIBLING_CONTEXT_HEADER",
    "SIBLING_FIELDS",
    "GraphEntry",
    "TranslationGraph",
    "is_name_entry",
    "stem",
]

#: Preamble the orchestrator (and the evalset builder, which mirrors the
#: production prompt shape) places above a sibling-context block.
SIBLING_CONTEXT_HEADER = (
    "Already-translated related entries (match their terminology and tone):"
)

_SINGLE_WORD_RE = re.compile(r"\w+$")
_WORDS_RE = re.compile(r"\w+")

#: Marker suffix ftbquests uses for segments lifted out of embedded JSON
#: text components: ``quests[0].description[3]::jsonseg[2]``.
_JSONSEG_RE = re.compile(r"::jsonseg\[\d+\]$")
#: Trailing array index on a key segment (``description[3]`` -> field
#: ``description``).
_TRAILING_INDEX_RE = re.compile(r"(?:\[\d+\])+$")

#: Last key segments that name a *field of* an object rather than the
#: object itself. Dropping them groups an object's title/description/
#: tooltip lines under one sibling stem.
SIBLING_FIELDS = frozenset(
    {
        "title",
        "name",
        "subtitle",
        "description",
        "desc",
        "text",
        "tooltip",
        "lore",
        "info",
        "summary",
        "quest_desc",
        "quest_subtitle",
        "chapter_subtitle",
    }
)

#: NAME_KEY_RE group(1) -> TermRule.category literal (rest -> "other").
#: Mirrors pair_harvester's mapping to satisfy the Literal type.
_KEY_CATEGORY: dict[str, str] = {
    "item": "item",
    "block": "block",
    "entity": "entity",
    "effect": "effect",
    "biome": "biome",
}

#: Surfaces shorter than this are too ambiguous to act as term anchors.
_MIN_SURFACE_CHARS = 3
#: Generous cap for bound target names; translations may expand.
_MAX_TARGET_CHARS = 64
#: Per-field truncation inside sibling context lines.
_SIBLING_FIELD_CHARS = 100


class _EntryLike(Protocol):
    """Structural view of pipeline EntryResult (avoids a circular import)."""

    file: str
    key: str
    source_text: str
    translated_text: str | None


def is_name_entry(key: str, source: str) -> bool:
    """True when ``key: source`` defines a game-content name.

    A name entry sits under a lang name-key prefix (``item.``, ``block.``,
    ...) and carries a short noun-phrase value long enough (>= 3 chars
    after cleanup) to be an unambiguous term anchor.
    """
    if NAME_KEY_RE.search(key) is None:
        return False
    cleaned = clean_text(source)
    if len(cleaned) < _MIN_SURFACE_CHARS:
        return False
    return is_name_value(cleaned)


def stem(key: str) -> str:
    """Sibling-group stem of an entry key.

    Strips the ``::jsonseg[n]`` suffix, then drops the last dot-segment
    when (index-stripped, lowercased) it names a field of the parent
    object (title/description/tooltip/...). Otherwise the key is its own
    stem, so ``item.mod.x`` and ``item.mod.x.tooltip`` share a group.
    """
    base = _JSONSEG_RE.sub("", key)
    head, _, last = base.rpartition(".")
    if not head:
        return base
    field = _TRAILING_INDEX_RE.sub("", last).lower()
    if field in SIBLING_FIELDS:
        return head
    return base


def _category_for(key: str) -> str:
    match = NAME_KEY_RE.search(key)
    prefix = match.group(1).lower() if match is not None else ""
    return _KEY_CATEGORY.get(prefix, "other")


def _flatten(text: str, limit: int = _SIBLING_FIELD_CHARS) -> str:
    """Newlines to spaces, whitespace collapsed, truncated to ``limit``."""
    return " ".join(text.split())[:limit]


@dataclass
class GraphEntry:
    """One translatable entry node, identified by ``(file, key)``."""

    file: str
    key: str
    source: str
    translated: str | None = None


#: Node identifier: modpack-relative posix path + entry key. Keys alone
#: collide across files (every ftbquests chapter has ``quests[0].title``).
_NodeId = tuple[str, str]


class TranslationGraph:
    """Relationship index: name definitions, mentions, sibling groups."""

    def __init__(self) -> None:
        #: Bumped whenever record_translation settles an entry, so pollers
        #: (the desktop graph view) can skip unchanged snapshots.
        self.version: int = 1
        self._entries: dict[_NodeId, GraphEntry] = {}
        #: surface (lowercased) -> defining nodes.
        self._defines: dict[str, list[_NodeId]] = {}
        #: surface (lowercased) -> non-defining nodes mentioning it.
        self._mentions: dict[str, list[_NodeId]] = {}
        #: surface (lowercased) -> TermRule category literal.
        self._categories: dict[str, str] = {}
        #: (file, stem) -> group members, source order. Groups of >= 2 only.
        self._sibling_groups: dict[_NodeId, list[_NodeId]] = {}
        #: node -> its sibling group key.
        self._group_of: dict[_NodeId, _NodeId] = {}

    # -- construction --------------------------------------------------------

    @classmethod
    def build(
        cls,
        files: Iterable[tuple[str, Mapping[str, str], Mapping[str, str]]],
    ) -> TranslationGraph:
        """Build from ``(rel_path, source_data, known_translations)`` rows.

        ``known_translations`` holds real pre-settled values for a subset
        of keys (existing target-locale entries plus TM hits).
        """
        graph = cls()
        for rel, source_data, known in files:
            for key, source in source_data.items():
                node = (rel, key)
                graph._entries[node] = GraphEntry(
                    file=rel,
                    key=key,
                    source=source,
                    translated=known.get(key) or None,
                )
                if is_name_entry(key, source):
                    surface = clean_text(source).lower()
                    graph._defines.setdefault(surface, []).append(node)
                    graph._categories.setdefault(surface, _category_for(key))
        graph._index_siblings()
        graph._index_mentions()
        return graph

    @classmethod
    def from_entries(cls, entries: Iterable[_EntryLike]) -> TranslationGraph:
        """Rebuild from pipeline entry results (post-run retry/retranslate).

        Only file/key/source_text/translated_text are read, so any object
        with those attributes works.
        """
        files: dict[str, tuple[dict[str, str], dict[str, str]]] = {}
        for entry in entries:
            source_data, known = files.setdefault(entry.file, ({}, {}))
            source_data[entry.key] = entry.source_text
            if entry.translated_text:
                known[entry.key] = entry.translated_text
        return cls.build(
            (rel, source_data, known)
            for rel, (source_data, known) in files.items()
        )

    def _index_siblings(self) -> None:
        groups: dict[_NodeId, list[_NodeId]] = {}
        for file, key in self._entries:
            groups.setdefault((file, stem(key)), []).append((file, key))
        for group_key, members in groups.items():
            if len(members) < 2:
                continue
            self._sibling_groups[group_key] = members
            for member in members:
                self._group_of[member] = group_key

    def _index_mentions(self) -> None:
        """One scan per entry against all defined surfaces.

        Same technique as GlossaryFilter._filter_term_rules: single-word
        surfaces resolve via word-set membership, multi-word surfaces via
        one combined word-boundary regex (longest alternative first, so an
        overlapping shorter surface never shadows a longer one).
        """
        if not self._defines:
            return
        single_word: set[str] = set()
        multi_word: list[str] = []
        for surface in self._defines:
            if _SINGLE_WORD_RE.fullmatch(surface):
                single_word.add(surface)
            else:
                multi_word.append(surface)
        pattern = (
            re.compile(
                r"\b(?:"
                + "|".join(
                    re.escape(s)
                    for s in sorted(multi_word, key=len, reverse=True)
                )
                + r")\b"
            )
            if multi_word
            else None
        )
        for node, entry in self._entries.items():
            text = clean_text(entry.source).lower()
            if not text:
                continue
            hits: set[str] = set()
            if single_word:
                hits.update(
                    word
                    for word in _WORDS_RE.findall(text)
                    if word in single_word
                )
            if pattern is not None:
                hits.update(match.group() for match in pattern.finditer(text))
            for surface in hits:
                if node in self._defines[surface]:
                    continue
                self._mentions.setdefault(surface, []).append(node)

    # -- mutation --------------------------------------------------------------

    def record_translation(self, file: str, key: str, translated: str) -> None:
        """Settle a node's translation (freshly produced by this run)."""
        entry = self._entries.get((file, key))
        if entry is not None and entry.translated != translated:
            entry.translated = translated
            self.version += 1

    # -- queries ---------------------------------------------------------------

    def _definition_candidates(
        self, definers: list[_NodeId], *, max_chars: int | None = None
    ) -> list[tuple[_NodeId, str]]:
        """(node, cleaned target) for definers with a real translation.

        Untranslated source copies never count; ``max_chars`` optionally
        drops over-long targets (bindings' plausible-name cap).
        """
        candidates: list[tuple[_NodeId, str]] = []
        for node in definers:
            entry = self._entries[node]
            if not entry.translated:
                continue
            if is_untranslated_copy(entry.source, entry.translated):
                continue
            target = clean_text(entry.translated)
            if not target or (max_chars is not None and len(target) > max_chars):
                continue
            candidates.append((node, target))
        return candidates

    @staticmethod
    def _chosen_target(candidates: list[tuple[_NodeId, str]]) -> str:
        """Most frequent target; ties resolve to the smallest (file, key)."""
        counts = Counter(value for _, value in candidates)
        top = max(counts.values())
        tied = {value for value, count in counts.items() if count == top}
        return next(value for _, value in sorted(candidates) if value in tied)


    def bindings(self, base: Glossary) -> list[TermRule]:
        """Settled name translations as term rules, ready to merge.

        A surface binds when: (1) at least one defining entry has a real
        translation (untranslated source copies excluded), (2) at least
        one *other* entry mentions the surface (skip thousands of
        one-off names nothing references), (3) the surface is not
        already covered by the base glossary (explicit rules win). With
        conflicting definitions the most frequent translation wins; ties
        resolve to the value of the smallest ``(file, key)``.
        """
        known_aliases = {
            alias.lower()
            for rule in base.term_rules
            for alias in rule.aliases
        }
        for noun in base.proper_noun_rules:
            known_aliases.add(noun.source_like.lower())
            known_aliases.update(alias.lower() for alias in noun.aliases)

        rules: list[TermRule] = []
        for surface, definers in self._defines.items():
            if surface in known_aliases:
                continue
            if not self._mentions.get(surface):
                continue
            candidates = self._definition_candidates(
                definers, max_chars=_MAX_TARGET_CHARS
            )
            if not candidates:
                continue
            chosen = self._chosen_target(candidates)
            display = clean_text(self._entries[min(definers)].source)
            rules.append(
                TermRule(
                    term_ko=chosen,
                    preferred_style="용어 고정",
                    aliases=[display],
                    category=self._categories[surface],  # type: ignore[arg-type]
                )
            )
        return rules

    def sibling_context(
        self,
        file: str,
        keys: Iterable[str],
        *,
        exclude: Container[str],
        max_lines: int = 8,
        max_chars: int = 800,
    ) -> str:
        """Already-settled sibling lines for a batch, or ``""``.

        Walks the batch keys in order, collecting translated siblings
        outside ``exclude`` (normally the batch itself) as
        ``- key: "source" => "translated"`` lines until either cap hits.
        """
        lines: list[str] = []
        seen: set[_NodeId] = set()
        total = 0
        for key in keys:
            group_key = self._group_of.get((file, key))
            if group_key is None:
                continue
            for node in self._sibling_groups[group_key]:
                if node in seen:
                    continue
                sibling = self._entries[node]
                if sibling.key in exclude or not sibling.translated:
                    continue
                seen.add(node)
                line = (
                    f'- {sibling.key}: "{_flatten(sibling.source)}"'
                    f' => "{_flatten(sibling.translated)}"'
                )
                if lines and total + len(line) > max_chars:
                    return "\n".join(lines)
                lines.append(line)
                total += len(line)
                if len(lines) >= max_lines or total >= max_chars:
                    return "\n".join(lines)
        return "\n".join(lines)

    def stats(self) -> dict[str, int]:
        """Size counters for one info log line."""
        return {
            "entries": len(self._entries),
            "terms": len(self._defines),
            "mentions": sum(len(nodes) for nodes in self._mentions.values()),
            "sibling_groups": len(self._sibling_groups),
        }

    def snapshot(
        self,
        *,
        q: str | None = None,
        status: str = "all",
        limit_terms: int = 300,
        mentions_per_term: int = 10,
    ) -> dict[str, object]:
        """Visualization payload: term/entry nodes plus defines/mentions/
        sibling edges, capped for canvas rendering.

        Built fully synchronously (no awaits): the running pipeline mutates
        the graph on the same asyncio loop, so a snapshot taken between
        awaits always sees a consistent state without locking.

        Args:
            q: case-insensitive substring over surface, display source, and
                settled target.
            status: "all" | "settled" | "pending" — settled means at least
                one defining entry carries a real translation.
            limit_terms: term-node cap; terms are kept by descending
                mention count (ties: surface order).
            mentions_per_term: mention-edge cap per term.
        """
        needle = q.strip().lower() if q and q.strip() else None
        terms: list[tuple[str, str, str | None, list[_NodeId], list[_NodeId]]] = []
        for surface, definers in self._defines.items():
            candidates = self._definition_candidates(definers)
            target = self._chosen_target(candidates) if candidates else None
            if status == "settled" and target is None:
                continue
            if status == "pending" and target is not None:
                continue
            display = clean_text(self._entries[min(definers)].source)
            if needle is not None and not (
                needle in surface
                or needle in display.lower()
                or (target is not None and needle in target.lower())
            ):
                continue
            terms.append(
                (surface, display, target, definers, self._mentions.get(surface, []))
            )
        terms.sort(key=lambda t: (-len(t[4]), t[0]))
        truncated = len(terms) > limit_terms
        terms = terms[:limit_terms]

        nodes: list[dict[str, object]] = []
        edges: list[dict[str, str]] = []
        included: dict[_NodeId, str] = {}  # node -> payload id

        def entry_id(node: _NodeId) -> str:
            """Add the entry node on first sight; return its payload id."""
            payload_id = included.get(node)
            if payload_id is None:
                payload_id = f"entry:{node[0]}\u0000{node[1]}"
                included[node] = payload_id
                entry = self._entries[node]
                nodes.append(
                    {
                        "id": payload_id,
                        "kind": "entry",
                        "label": node[1],
                        "file": node[0],
                        "settled": bool(entry.translated)
                        and not is_untranslated_copy(
                            entry.source, entry.translated or ""
                        ),
                    }
                )
            return payload_id

        for surface, display, target, definers, mentions in terms:
            term_id = f"term:{surface}"
            nodes.append(
                {
                    "id": term_id,
                    "kind": "term",
                    "label": display,
                    "target": target,
                    "settled": target is not None,
                    "category": self._categories[surface],
                    "definers": len(definers),
                    "mentions": len(mentions),
                }
            )
            for node in definers:
                edges.append(
                    {"source": term_id, "target": entry_id(node), "kind": "defines"}
                )
            if len(mentions) > mentions_per_term:
                truncated = True
            for node in mentions[:mentions_per_term]:
                edges.append(
                    {"source": entry_id(node), "target": term_id, "kind": "mentions"}
                )

        # Sibling edges only BETWEEN entry nodes already in the payload, as
        # a chain in group order (a clique adds quadratic edges for zero
        # extra information on a canvas).
        for group in self._sibling_groups.values():
            present = [node for node in group if node in included]
            for left, right in zip(present, present[1:]):
                edges.append(
                    {
                        "source": included[left],
                        "target": included[right],
                        "kind": "sibling",
                    }
                )

        return {
            "version": self.version,
            "truncated": truncated,
            "stats": self.stats(),
            "nodes": nodes,
            "edges": edges,
        }
