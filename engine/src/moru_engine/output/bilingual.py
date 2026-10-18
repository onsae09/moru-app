"""Bilingual display-name rendering: ``철 곡괭이 (Iron Pickaxe)``.

A *post-translation* transform. The compiled prompt artifact tells the
model to never append the English original in parentheses, and
``evalset/metrics.py`` scores against it, so the parenthetical must never
be asked of the model — it is derived here, deterministically, from the
already-translated value plus the source string. One translation run
therefore yields both variants: the plain pack and the bilingual pack are
the same data rendered twice, at no extra model cost.

Why only display names
----------------------
Appending the source to *every* string would double the length of every
tooltip, quest description, book page and dialogue line and make them
unreadable. The parenthetical only pays for itself on short noun-like
names, where it lets a player search JEI/REI/EMI and in-game search boxes
by either spelling and follow English-language guides and wikis.

The gate is three stages, each measured against the real vanilla 1.21.5
``en_us``/``ko_kr`` corpus (7159 translated keys):

1. **Prose exclusion** (:func:`_has_prose_segment`) — a key-segment sweep
   dropping anything under a prose field (``…tooltip``, ``…desc``,
   ``…lore``, ``…_description``) or inside a book/guide document. Removes
   52 vanilla entries that stages 2-3 would otherwise annotate, e.g.
   ``block.minecraft.spawner.desc1`` ("Interact with Spawn Egg:") and the
   banner-pattern ``.desc`` lines. Mod authors put descriptive text under
   item-shaped keys more often than a clean namespace model predicts, so
   this net is load-bearing rather than belt-and-braces.
2. **Name-namespace anchoring** (:func:`_is_display_name`) — the key's
   FIRST segment must be a display-name namespace
   (:data:`_NAME_HEAD_SEGMENTS`).
3. **Value guards** (:func:`should_annotate`) — identity, existing
   brackets, formatting codes, length.

Why the head is anchored instead of reusing ``is_name_entry``
-------------------------------------------------------------
``graph.is_name_entry`` is the engine's existing "is this a name" test and
was the obvious thing to reuse, but its ``NAME_KEY_RE.search`` matches a
name prefix *anywhere* in the key — correct for term mining, too loose
here. On the vanilla corpus it admits 848 entries that are not names:

- 831 ``subtitles.*`` sound-accessibility captions ("Anvil destroyed",
  "Beacon hums") — they match on ``.block.``/``.entity.`` mid-key.
- 17 ``argument.*`` / ``arguments.*`` / ``commands.*`` error messages
  ("Invalid name or UUID", "No entity was found") — they match on
  ``.entity.`` / ``.item.``.

Anchoring the head keeps all 2885 genuine names (block 1774, item 677,
entity 204, attribute 66, biome 65, enchantment 43, effect 40, creative
tab 16) and drops exactly those 848. The *value*-shape half of the
definition is still shared with the rest of the engine via
``term_miner.is_name_value``/``clean_text``, so "what a name-shaped value
looks like" has one owner.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from ..glossary.term_miner import clean_text, is_name_value
from ..placeholder import PlaceholderProtector

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "MAX_ANNOTATED_CHARS",
    "MAX_SOURCE_CHARS",
    "annotate_entries",
    "annotate_value",
    "should_annotate",
]

#: Leading key segment of a game-content display name. Anchored: see the
#: module docstring for the 848 non-name entries an unanchored search
#: admits. Covers the modern ``item.``/``block.`` namespaces, the 1.12
#: ``tile.`` spelling, the registry namespaces mods use for named content,
#: and the creative-tab spellings (``itemGroup.`` is the vanilla one and is
#: unmatchable by the glossary miner's regex, which requires a separator
#: after "item").
_NAME_HEAD_SEGMENTS = frozenset(
    {
        "attribute",
        "banner_pattern",
        "biome",
        "block",
        "creative_tab",
        "creativetab",
        "dimension",
        "effect",
        "enchantment",
        "entity",
        "fluid",
        "instrument",
        "item",
        "item_group",
        "itemgroup",
        "jukebox_song",
        "material",
        "mob_effect",
        "painting",
        "potion",
        "spell",
        "structure",
        "tile",
        "trim_material",
        "trim_pattern",
        "villager",
    }
)

#: Key segments naming a *prose field* of an object rather than the object
#: itself. Excluded at ANY position: none is ever an item name on its own,
#: so a positional rule would only add false negatives. Superset of
#: ``translation_graph.SIBLING_FIELDS`` minus ``name``/``title``, which ARE
#: the display name (``item.foo.name`` is the 1.12 spelling).
_PROSE_FIELDS = frozenset(
    {
        "body",
        "chapter_subtitle",
        "comment",
        "desc",
        "descr",
        "description",
        "descriptions",
        "detail",
        "details",
        "flavor",
        "flavour",
        "help",
        "hint",
        "info",
        "line",
        "lore",
        "message",
        "msg",
        "note",
        "para",
        "paragraph",
        "quest_desc",
        "quest_subtitle",
        "subtitle",
        "subtitles",
        "summary",
        "text",
        "tooltip",
        "tooltips",
        "warning",
    }
)

#: Segments marking a book/guide *document*. Unlike ``_PROSE_FIELDS`` these
#: are legitimate item names on their own — ``item.minecraft.book`` is the
#: item "Book", ``item.minecraft.knowledge_book`` is "Knowledge Book" — so
#: they only exclude when something follows them in the key, i.e. when they
#: are a container in the path (``item.mymod.book.page1``) rather than the
#: thing being named.
_PROSE_CONTAINERS = frozenset(
    {
        "book",
        "books",
        "chapter",
        "chapters",
        "entries",
        "entry",
        "guide",
        "guides",
        "journal",
        "lexicon",
        "manual",
        "manuals",
        "page",
        "pages",
        "patchouli",
        "tome",
        "tomes",
    }
)

#: ``::jsonseg[n]`` suffix added by structured-content handlers.
_JSONSEG_RE = re.compile(r"::jsonseg\[\d+\]$")
#: Trailing array indices on a segment: ``description[3]`` -> ``description``.
_TRAILING_INDEX_RE = re.compile(r"(?:\[\d+\])+$")
#: Trailing ordinal on a continuation segment: ``tooltip2``, ``desc_3``,
#: ``line-10`` -> ``tooltip``/``desc``/``line``. Numbered siblings are
#: continuation lines of ONE sentence; annotating them would splice the
#: English original into the middle of a paragraph.
_TRAILING_ORDINAL_RE = re.compile(r"[ _-]*\d+$")
#: Any run of key separators.
_SEGMENT_SPLIT_RE = re.compile(r"[./:]+")
#: Word separators *inside* one segment, so a compound field name like
#: ``additions_slot_description`` is recognised by its final word.
_SUBTOKEN_SPLIT_RE = re.compile(r"[_\-\s]+")

#: Brackets that would nest or duplicate if we appended inside them.
_BRACKETS = ("(", ")", "[", "]", "（", "）", "【", "】")

#: Minimum cleaned-source length, mirroring
#: ``translation_graph._MIN_SURFACE_CHARS``: shorter surfaces are too
#: ambiguous to be worth an annotation.
_MIN_SOURCE_CHARS = 3

#: Longest cleaned source that still earns a parenthetical.
#:
#: Measured on the vanilla name corpus: median name length is 14, p95 is
#: 25, max 38. A cap of 32 admits 99.6% of names and excludes 14 entries,
#: all of them heraldic banner-pattern descriptions ("Magenta Per Bend
#: Sinister Inverted", 34) that no player searches by name and where the
#: suffix would push a single tooltip line past the width Minecraft
#: renders comfortably.
MAX_SOURCE_CHARS = 32

#: Ceiling on the rendered ``translation (source)`` result. Reuses the
#: engine's existing name-length ceiling (``translation_graph
#: ._MAX_TARGET_CHARS``) rather than inventing a second number.
MAX_ANNOTATED_CHARS = 64


def _normalized_segments(key: str) -> list[str]:
    """Key segments, lowercased, with indices and ordinals stripped."""
    base = _JSONSEG_RE.sub("", key)
    segments: list[str] = []
    for raw in _SEGMENT_SPLIT_RE.split(base):
        if not raw:
            continue
        segment = _TRAILING_INDEX_RE.sub("", raw).lower()
        segments.append(_TRAILING_ORDINAL_RE.sub("", segment))
    return segments


def _has_prose_segment(key: str) -> bool:
    """True when the key lives under a prose field or inside a document.

    Matching is per *segment*, on the whole segment and on its final
    underscore-delimited word — never a substring sweep of the entire key.
    A substring test for "book"/"lore"/"info"/"text" wrongly drops real
    display names: on the vanilla corpus it would lose 19 of them,
    including Bookshelf, Chiseled Bookshelf, Book, Enchanted Book, Written
    Book, Book and Quill, Knowledge Book, Reinforced Deepslate ("info"),
    Text Display ("text"), Zombie Reinforcements ("info"), Explorer
    Pottery Sherd ("lore") and Colored Blocks ("lore"). Matching the final
    word instead still catches compound prose fields such as
    ``additions_slot_description``.
    """
    segments = _normalized_segments(key)
    if not segments:
        return True
    last = len(segments) - 1
    for index, segment in enumerate(segments):
        subtokens = _SUBTOKEN_SPLIT_RE.split(segment)
        tail = subtokens[-1] if subtokens else segment
        if segment in _PROSE_FIELDS or tail in _PROSE_FIELDS:
            return True
        # A document word only excludes when it is a container: something
        # follows it, so it is not the thing being named.
        if index < last and (
            segment in _PROSE_CONTAINERS or tail in _PROSE_CONTAINERS
        ):
            return True
    return False


def _is_display_name(key: str, source: str) -> bool:
    """True when ``key: source`` names game content (stage 2 + value shape)."""
    segments = _normalized_segments(key)
    if not segments or segments[0] not in _NAME_HEAD_SEGMENTS:
        return False
    cleaned = clean_text(source)
    return len(cleaned) >= _MIN_SOURCE_CHARS and is_name_value(cleaned)


def _is_formatted(text: str) -> bool:
    """True when the text carries placeholders or formatting codes.

    Covers ``%s``/``%1$s``, ``§a``, ``&a``, ``{...}`` (including an
    unrestored ``{{ARG1}}`` protection token), XML-ish tags and ``\\n``.
    Such a value must never be annotated: appending the source would
    duplicate every format specifier — breaking the argument count a
    ``String.format`` call depends on — and would let colour state bleed
    into the suffix.
    """
    return bool(PlaceholderProtector.count_placeholders(text))


def should_annotate(key: str, source: str, translated: str) -> bool:
    """Whether ``key`` earns a ``translated (source)`` rendering."""
    if not source or not translated.strip():
        return False
    # Stage 1: prose exclusion (cheapest test, and the one protecting
    # readability, so it runs first).
    if _has_prose_segment(key):
        return False
    # Stage 2: anchored name namespace + name-shaped value.
    if not _is_display_name(key, source):
        return False

    cleaned_source = clean_text(source)
    # Nothing to add when the "translation" is the source: an untranslated
    # or skipped entry, or a dry run, must never become
    # "Iron Pickaxe (Iron Pickaxe)".
    if clean_text(translated).casefold() == cleaned_source.casefold():
        return False
    # Existing brackets on either side would nest ("철 곡괭이 (Iron Pickaxe
    # (Tier 2))") or duplicate an annotation. Skipping also makes the
    # transform idempotent: re-running it over a bilingual value is a no-op.
    if any(bracket in source or bracket in translated for bracket in _BRACKETS):
        return False
    if _is_formatted(source) or _is_formatted(translated):
        return False
    if len(cleaned_source) > MAX_SOURCE_CHARS:
        return False
    # +3 for the " (" and ")" wrapper.
    return len(translated.rstrip()) + len(cleaned_source) + 3 <= MAX_ANNOTATED_CHARS


def annotate_value(key: str, source: str, translated: str) -> str:
    """``translated (source)`` when ``key`` qualifies, else ``translated``."""
    if not should_annotate(key, source, translated):
        return translated
    return f"{translated.rstrip()} ({clean_text(source)})"


def annotate_entries(
    translations: Mapping[str, str], sources: Mapping[str, str]
) -> dict[str, str]:
    """Bilingual copy of ``translations``.

    Entries whose source text is unknown are passed through untouched — a
    missing source is a reason to leave the value alone, never to guess.
    """
    return {
        key: annotate_value(key, sources.get(key, ""), value)
        for key, value in translations.items()
    }
