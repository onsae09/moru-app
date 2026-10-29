"""The {{KIND}} token roundtrip is sacred: protect -> (translate) -> restore.

Any mismatch must raise PlaceholderError — never pass silently.
"""

from __future__ import annotations

import pytest

from moru_engine.placeholder import TOKEN_RE, PlaceholderError, PlaceholderProtector


def test_protect_covers_all_token_kinds() -> None:
    text = "§6Hello §b%s§6! Use %1$s and {player} plus <b>bold</b>\\n done"
    protected = PlaceholderProtector().protect(text)
    assert "§" not in protected.protected
    assert "%s" not in protected.protected
    assert "{player}" not in protected.protected
    assert "<b>" not in protected.protected
    assert "\\n" not in protected.protected
    assert len(protected.placeholders) >= 7


def test_roundtrip_restores_original() -> None:
    text = "§aTip:§r Press %s to open the §bQuest Book§r!"
    protected = PlaceholderProtector().protect(text)
    # simulate translation: tokens kept, text replaced
    translated = protected.protected.replace("Press", "누르세요").replace(
        "to open the", "열려면"
    )
    restored = protected.restore(translated)
    for original in ("§a", "§r", "%s", "§b"):
        assert original in restored
    assert not TOKEN_RE.search(restored)


def test_missing_token_raises() -> None:
    protected = PlaceholderProtector().protect("Durability: %s / %s")
    tokens = [p.token for p in protected.placeholders]
    broken = protected.protected.replace(tokens[0], "")
    with pytest.raises(PlaceholderError):
        protected.restore(broken)


def test_leftover_unknown_token_raises() -> None:
    protected = PlaceholderProtector().protect("Level %s")
    with pytest.raises(PlaceholderError):
        protected.restore(protected.protected + " {{PH99}}")


def test_placeholder_only_detection() -> None:
    protector = PlaceholderProtector()
    only = protector.protect("§a%s§r")
    text = protector.protect("§aHello§r")
    assert protector.is_only_placeholders(only)
    assert not protector.is_only_placeholders(text)


def test_token_kinds_are_semantic() -> None:
    protected = PlaceholderProtector().protect(
        "§aHello§r %s and &6gold&r {player} <b>\\n"
    )
    kinds = {p.original: p.token for p in protected.placeholders}
    assert kinds["§a"].startswith("{{COLOR")
    assert kinds["§r"].startswith("{{RESET")
    assert kinds["&6"].startswith("{{COLOR")
    assert kinds["&r"].startswith("{{RESET")
    assert kinds["%s"].startswith("{{ARG")
    assert kinds["{player}"].startswith("{{VAR")
    assert kinds["<b>"].startswith("{{TAG")
    assert kinds["\\n"].startswith("{{BR")
    # roundtrip still exact
    assert (
        protected.restore(protected.protected)
        == "§aHello§r %s and &6gold&r {player} <b>\\n"
    )


def test_identical_literals_share_one_bare_token() -> None:
    # RESET and BR are always one literal -> never numbered; repeated %s
    # is one literal -> one bare token used twice.
    protected = PlaceholderProtector().protect("§6Gold§r %s / %s\\n§6More§r")
    tokens = {p.original: p.token for p in protected.placeholders}
    assert tokens["§6"] == "{{COLOR}}"
    assert tokens["§r"] == "{{RESET}}"
    assert tokens["%s"] == "{{ARG}}"
    assert tokens["\\n"] == "{{BR}}"
    assert protected.protected == (
        "{{COLOR}}Gold{{RESET}} {{ARG}} / {{ARG}}{{BR}}{{COLOR}}More{{RESET}}"
    )
    assert protected.restore(protected.protected) == "§6Gold§r %s / %s\\n§6More§r"


def test_distinct_literals_get_numbered_by_literal() -> None:
    # Two colors -> numbers identify WHICH color; repeats share the number.
    protected = PlaceholderProtector().protect("§6a§r §ab§r §6c")
    tokens = [(p.original, p.token) for p in sorted(protected.placeholders, key=lambda p: p.position)]
    assert tokens == [
        ("§6", "{{COLOR1}}"),
        ("§r", "{{RESET}}"),
        ("§a", "{{COLOR2}}"),
        ("§r", "{{RESET}}"),
        ("§6", "{{COLOR1}}"),
    ]
    assert protected.restore(protected.protected) == "§6a§r §ab§r §6c"


def test_reordered_tokens_restore_correct_literals() -> None:
    # Word-order changes move tokens; numbered tokens carry their literal.
    protected = PlaceholderProtector().protect("§6Gold§r and §aGreen§r")
    reordered = "{{COLOR2}}초록{{RESET}} 그리고 {{COLOR1}}금색{{RESET}}"
    assert protected.restore(reordered) == "§a초록§r 그리고 §6금색§r"


def test_partial_count_loss_raises() -> None:
    # Shared bare tokens are count-checked: dropping ONE of two {{ARG}}
    # occurrences must still fail the roundtrip.
    protected = PlaceholderProtector().protect("Durability: %s / %s")
    assert protected.protected.count("{{ARG}}") == 2
    with pytest.raises(PlaceholderError):
        protected.restore("내구도: {{ARG}}")


def test_surplus_known_token_raises() -> None:
    protected = PlaceholderProtector().protect("Level %s")
    with pytest.raises(PlaceholderError):
        protected.restore("레벨 {{ARG}} {{ARG}}")


def test_bracketed_prose_with_format_arg_roundtrips() -> None:
    # Regression: "<Error occurred, plz report to %s>" is prose, not a tag.
    # The old <[^>]+> pattern swallowed the whole sentence, and the nested
    # %s match shifted the outer span so restore produced "{{TAG}}RG}}>".
    text = "<Error occurred, plz report to %s>"
    protected = PlaceholderProtector().protect(text)
    assert protected.protected == "<Error occurred, plz report to {{ARG}}>"
    assert protected.restore(protected.protected) == text


def test_real_tags_still_protected() -> None:
    text = "<b>bold</b> and <color=red>red"
    protected = PlaceholderProtector().protect(text)
    assert sorted(p.token for p in protected.placeholders) == [
        "{{TAG1}}",
        "{{TAG2}}",
        "{{TAG3}}",
    ]
    assert protected.restore(protected.protected) == text


def test_overlapping_matches_keep_earlier_pattern() -> None:
    # "<%s>" is both a tag-shaped span and a nested format arg: the
    # earlier pattern (java_format) wins and the overlapping tag match
    # is dropped, so restore stays a clean literal replacement.
    text = "<%s>"
    protected = PlaceholderProtector().protect(text)
    assert protected.protected == "<{{ARG}}>"
    assert protected.restore(protected.protected) == text


def test_attribute_tags_with_spaces_stay_protected() -> None:
    # Whitespace inside a tag is fine when it separates name=value
    # attributes (or a spaced self-close) — only bare prose is rejected.
    text = '<font color="red">hot</font> <a href=\'https://moru.gg\'>link</a><br />'
    protected = PlaceholderProtector().protect(text)
    tag_literals = {
        p.original for p in protected.placeholders if p.token.startswith("{{TAG")
    }
    assert tag_literals == {
        '<font color="red">',
        "</font>",
        "<a href='https://moru.gg'>",
        "</a>",
        "<br />",
    }
    assert protected.restore(protected.protected) == text


def test_valueless_words_after_tag_name_read_as_prose() -> None:
    # "<Error occurred plz report>" has no =value attributes: bare words
    # after the first are prose, not attributes, so nothing is frozen.
    protected = PlaceholderProtector().protect("<Error occurred plz report>")
    assert protected.placeholders == []


def test_repeated_positional_arg_is_reported_as_collapsed() -> None:
    # Four distinct arguments, one literal -> one token that can no longer
    # say WHICH argument it is. That ambiguity comes from the SOURCE.
    protected = PlaceholderProtector().protect("Added %s to %s for %s (now %s)")
    assert protected.protected == "Added {{ARG}} to {{ARG}} for {{ARG}} (now {{ARG}})"
    assert protected.collapsed_positional_arg() == ("{{ARG}}", "%s", 4)


def test_per_occurrence_numbering_resolves_to_indexed_literals() -> None:
    # Korean word order reverses the arguments, and over a collapsed token
    # the only way to say so is to number the occurrences. Resolving that
    # to indexed literals reproduces vanilla ko_kr for this very key, so a
    # SOURCE-side ambiguity never has to fail the entry.
    protected = PlaceholderProtector().protect("Added %s to %s for %s (now %s)")
    reordered = "{{ARG3}}의 {{ARG2}}에 {{ARG1}}을(를) 더했습니다 (이제 {{ARG4}}입니다)"
    assert (
        protected.restore(reordered)
        == "%3$s의 %2$s에 %1$s을(를) 더했습니다 (이제 %4$s입니다)"
    )


def test_collapsed_arg_resolution_keeps_other_kinds_intact() -> None:
    protected = PlaceholderProtector().protect("§6Gold§r %s / %s")
    assert protected.restore("{{COLOR}}금{{RESET}} {{ARG2}} / {{ARG1}}") == (
        "§6금§r %2$s / %1$s"
    )


def test_bare_shared_tokens_still_restore_unnumbered() -> None:
    # The untouched path: a model that keeps the shared token gets the
    # original literals back, not the indexed rewrite.
    protected = PlaceholderProtector().protect("Added %s to %s for %s (now %s)")
    assert protected.restore(protected.protected) == "Added %s to %s for %s (now %s)"


def test_single_positional_arg_is_not_collapsed() -> None:
    # One occurrence is unambiguous, so an invented number is a model error.
    protected = PlaceholderProtector().protect("Level %s")
    assert protected.collapsed_positional_arg() is None
    with pytest.raises(PlaceholderError):
        protected.restore("레벨 {{ARG1}}")


def test_indexed_source_args_are_never_collapsed() -> None:
    # "%1$s"/"%2$s" already name their argument: distinct literals, numbered
    # BY LITERAL, so a per-occurrence number would be ambiguous.
    protected = PlaceholderProtector().protect("%1$s was slain by %2$s")
    assert protected.collapsed_positional_arg() is None
    with pytest.raises(PlaceholderError):
        protected.restore("{{ARG1}}이(가) {{ARG3}}에게 죽었습니다")


def test_repeated_indexed_arg_is_not_collapsed() -> None:
    # The same "%1$s" twice is the SAME argument, not two of them.
    protected = PlaceholderProtector().protect("%1$s trusts %1$s")
    assert protected.collapsed_positional_arg() is None


def test_mixed_bare_and_numbered_arg_forms_still_raise() -> None:
    protected = PlaceholderProtector().protect("Added %s to %s for %s (now %s)")
    with pytest.raises(PlaceholderError):
        protected.restore("{{ARG}}의 {{ARG2}}에 {{ARG3}} {{ARG4}}")


def test_incomplete_occurrence_numbering_still_raises() -> None:
    # Two of four arguments dropped is a TRANSLATION error, not a source
    # ambiguity, and must keep failing the entry.
    protected = PlaceholderProtector().protect("Added %s to %s for %s (now %s)")
    with pytest.raises(PlaceholderError):
        protected.restore("{{ARG1}}의 {{ARG2}}")


def test_duplicated_occurrence_number_still_raises() -> None:
    protected = PlaceholderProtector().protect("Durability: %s / %s")
    with pytest.raises(PlaceholderError):
        protected.restore("내구도: {{ARG1}} / {{ARG1}}")


def test_percent_literal_is_not_a_positional_argument() -> None:
    # "%%" renders a literal percent sign; it consumes no argument.
    protected = PlaceholderProtector().protect("100%% of 100%%")
    assert protected.collapsed_positional_arg() is None


@pytest.mark.parametrize("spec", ["%.1f", "%,d", "%02d", "%-5s", "%1$.2f", "%+d", "%%"])
def test_formatter_flags_width_and_precision_are_protected(spec: str) -> None:
    protected = PlaceholderProtector().protect(f"Speed: {spec} blocks")
    assert spec not in protected.protected
    assert protected.restore(protected.protected) == f"Speed: {spec} blocks"


def test_percent_before_prose_is_not_a_specifier() -> None:
    # The space flag is excluded so "50% done" stays prose.
    protected = PlaceholderProtector().protect("50% done")
    assert protected.placeholders == []
