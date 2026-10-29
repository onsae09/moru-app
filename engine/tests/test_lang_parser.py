"""Legacy .lang parsing and dump semantics for Minecraft 1.12."""

from __future__ import annotations

from pathlib import Path

import pytest

from moru_engine.parsers.lang import LangParser


@pytest.mark.asyncio
async def test_plain_lang_keeps_backslash_escapes_literal(tmp_path: Path) -> None:
    source = tmp_path / "en_us.lang"
    source.write_text(r"message=Line\nTwo" + "\n", encoding="utf-8")

    parsed = await LangParser(source).parse()

    assert parsed == {"message": r"Line\nTwo"}


@pytest.mark.asyncio
async def test_parse_escapes_directive_is_honored_anywhere(tmp_path: Path) -> None:
    source = tmp_path / "en_us.lang"
    source.write_text(
        "# generated header\nplain=One\n#PARSE_ESCAPES\nmessage=Line\\nTwo\n",
        encoding="utf-8",
    )

    parsed = await LangParser(source).parse()
    assert parsed["message"] == "Line\nTwo"

    output = tmp_path / "ko_kr.lang"
    await LangParser(output, original_path=source).dump(
        {"plain": "하나", "message": "첫 줄\n둘째 줄"}
    )
    dumped = output.read_text(encoding="utf-8")
    assert dumped.startswith("#PARSE_ESCAPES\n")
    assert "message=첫 줄\\n둘째 줄" in dumped


@pytest.mark.asyncio
async def test_plain_lang_dump_does_not_json_escape_quotes_or_paths(
    tmp_path: Path,
) -> None:
    source = tmp_path / "en_us.lang"
    source.write_text("key=Source\n", encoding="utf-8")
    output = tmp_path / "ko_kr.lang"

    await LangParser(output, original_path=source).dump(
        {"quote": 'Say "hi"', "path": r"C:\\mods"}
    )

    assert output.read_text(encoding="utf-8").splitlines() == [
        r"path=C:\\mods",
        'quote=Say "hi"',
    ]


def test_bom_does_not_leak_into_the_first_key() -> None:
    from moru_engine.parsers.lang import LangParser

    assert list(LangParser.parse_text("﻿item.x=Stone\n")) == ["item.x"]


def test_json_with_bom_still_parses() -> None:
    from moru_engine.parsers.json_parser import JSONParser

    assert JSONParser._load_json_content('﻿{"a": "b"}') == {"a": "b"}
