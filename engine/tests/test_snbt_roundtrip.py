"""SNBT output keeps every tag the translation did not touch exactly typed."""

from __future__ import annotations

from pathlib import Path

import ftb_snbt_lib as slib
import pytest

from moru_engine.parsers.snbt import SNBTParser

SOURCE = """{
\ttitle: "Tom & Jerry"
\toptional: true
\tflag: 1b
\tweight: 2.0f
\tbig: 3L
\tcount: 5s
\tids: [I; 1, 2]
\tbytes: [B; 1b, 2b]
\tcommand: "/say a & b"
\tdesc: ["Hello", "&aGreen"]
}"""


@pytest.mark.asyncio
async def test_untranslated_tags_keep_their_types(tmp_path: Path) -> None:
    source = tmp_path / "chapter.snbt"
    source.write_text(SOURCE, encoding="utf-8")
    out = tmp_path / "out.snbt"
    await SNBTParser(out, original_path=source).dump(
        {"title": "톰 & 제리", "desc[0]": "안녕"}
    )

    before, after = slib.loads(SOURCE), slib.loads(out.read_text(encoding="utf-8"))
    for key in ("optional", "flag", "weight", "big", "count", "ids", "bytes"):
        assert type(after[key]) is type(before[key]), key
        assert after[key] == before[key], key
    # Only translated values are escaped; the untouched command is verbatim.
    assert str(after["command"]) == "/say a & b"
    assert str(after["title"]) == "톰 \\& 제리"
    assert str(after["desc"][1]) == "&aGreen"


def test_ampersand_escape_is_idempotent() -> None:
    once = SNBTParser._replace_ampersand("a & b")
    assert SNBTParser._replace_ampersand(once) == once == "a \\& b"


@pytest.mark.asyncio
async def test_a_zero_filled_file_is_refused_without_lexing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from moru_engine.parsers.base import ParseError

    broken = tmp_path / "reward.snbt"
    broken.write_bytes(b"\x00" * 539)
    with pytest.raises(ParseError, match="NUL"):
        await SNBTParser(broken).parse()
    assert "Illegal character" not in capsys.readouterr().out
