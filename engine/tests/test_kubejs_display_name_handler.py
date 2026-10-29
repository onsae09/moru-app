"""KubeJS display-name extraction and lossless rewrite coverage."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from moru_engine.handlers.kubejs_display_name import KubeJSDisplayNameHandler
from moru_engine.placeholder import PlaceholderProtector
from moru_engine.scanner import scan_modpack


@pytest.mark.asyncio
async def test_extracts_static_and_template_display_names_only(tmp_path: Path) -> None:
    script = tmp_path / "modpack/kubejs/startup_scripts/items.js"
    script.parent.mkdir(parents=True)
    script.write_text(
        "event.create('one').displayName('Superior Shop')\n"
        "event.create(`orb_${orb}`).displayName(`Orb of ${orb.toUpperCase()}`)\n"
        "event.create('kept').displayName(Component.translatable('item.kept'))\n"
        "// event.create('old').displayName('Commented Name')\n"
        "/* event.create('old2').displayName('Block Comment Name') */\n",
        encoding="utf-8",
    )

    handler = KubeJSDisplayNameHandler()
    extracted = await handler.extract(script)

    assert extracted == {
        "display_name.0001": "Superior Shop",
        "display_name.0002": "Orb of ${orb.toUpperCase()}",
    }


@pytest.mark.asyncio
async def test_apply_changes_only_display_name_contents(tmp_path: Path) -> None:
    script = tmp_path / "modpack/kubejs/startup_scripts/items.js"
    script.parent.mkdir(parents=True)
    original = (
        "const untouched = 'Orb of Nothing'\n"
        "event.create('shop').displayName('Superior Shop').texture('x:y')\n"
        "event.create(`orb_${orb}`).displayName(`Orb of ${orb.toUpperCase()}`)\n"
    )
    script.write_text(original, encoding="utf-8")
    output = tmp_path / "out/kubejs/startup_scripts/items.js"

    await KubeJSDisplayNameHandler().apply(
        script,
        {
            "display_name.0001": "고급 상점",
            "display_name.0002": "${orb.toUpperCase()}의 오브",
        },
        output,
    )

    rewritten = output.read_text(encoding="utf-8")
    assert "const untouched = 'Orb of Nothing'" in rewritten
    assert ".displayName('고급 상점').texture('x:y')" in rewritten
    assert ".displayName(`${orb.toUpperCase()}의 오브`)" in rewritten


@pytest.mark.asyncio
async def test_apply_escapes_quotes_backslashes_and_line_breaks(tmp_path: Path) -> None:
    script = tmp_path / "modpack/kubejs/startup_scripts/items.js"
    script.parent.mkdir(parents=True)
    script.write_text(
        "event.create('a').displayName('Quote Shop')\n"
        "event.create('b').displayName('Slash Shop')\n"
        "event.create('c').displayName('Newline Shop')\n"
        "event.create('d').displayName(`Tick Shop`)\n",
        encoding="utf-8",
    )
    output = tmp_path / "out/kubejs/startup_scripts/items.js"

    await KubeJSDisplayNameHandler().apply(
        script,
        {
            "display_name.0001": "It's `Mine`",
            "display_name.0002": "상점\\",
            "display_name.0003": "고급\n상점\r끝",
            "display_name.0004": "`고급` 상점",
        },
        output,
    )

    rewritten = output.read_text(encoding="utf-8")
    assert r".displayName('It\'s `Mine`')" in rewritten
    assert r".displayName('상점\\')" in rewritten
    assert r".displayName('고급\n상점\r끝')" in rewritten
    assert r".displayName(`\`고급\` 상점`)" in rewritten


@pytest.mark.asyncio
async def test_escaped_quote_in_source_round_trips_without_doubling(
    tmp_path: Path,
) -> None:
    script = tmp_path / "modpack/kubejs/startup_scripts/items.js"
    script.parent.mkdir(parents=True)
    script.write_text(
        r"event.create('e').displayName('It\'s Mine')" + "\n", encoding="utf-8"
    )
    handler = KubeJSDisplayNameHandler()

    assert await handler.extract(script) == {"display_name.0001": r"It\'s Mine"}

    output = tmp_path / "out/kubejs/startup_scripts/items.js"
    await handler.apply(script, {"display_name.0001": r"It\'s 내꺼"}, output)

    assert r".displayName('It\'s 내꺼')" in output.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_non_literal_argument_is_left_alone(tmp_path: Path) -> None:
    script = tmp_path / "modpack/kubejs/startup_scripts/items.js"
    script.parent.mkdir(parents=True)
    script.write_text(
        "event.create('v').displayName(shopName)\n"
        "event.create('w').displayName('Superior Shop')\n",
        encoding="utf-8",
    )
    handler = KubeJSDisplayNameHandler()

    assert await handler.extract(script) == {"display_name.0001": "Superior Shop"}

    output = tmp_path / "out/kubejs/startup_scripts/items.js"
    await handler.apply(script, {"display_name.0001": "고급 상점"}, output)

    rewritten = output.read_text(encoding="utf-8")
    assert ".displayName(shopName)" in rewritten
    assert ".displayName('고급 상점')" in rewritten


@pytest.mark.asyncio
async def test_display_name_missed_after_regex_literal_is_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    script = tmp_path / "modpack/kubejs/startup_scripts/items.js"
    script.parent.mkdir(parents=True)
    script.write_text(
        "// event.create('old').displayName('Commented Name')\n"
        "const clean = raw.replace(/'/g, '')\n"
        "event.create('a').displayName('Superior Shop')\n",
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING):
        extracted = await KubeJSDisplayNameHandler().extract(script)

    assert extracted == {}
    # The commented-out call is a correct skip and must stay silent.
    assert len(caplog.records) == 1
    assert "Skipped displayName" in caplog.text


@pytest.mark.asyncio
async def test_scanner_includes_kubejs_display_name_script(tmp_path: Path) -> None:
    modpack = tmp_path / "modpack"
    script = modpack / "kubejs/startup_scripts/items.js"
    script.parent.mkdir(parents=True)
    script.write_text("event.create('x').displayName('Example Item')", encoding="utf-8")

    result = await scan_modpack(modpack)

    assert any(pair.source_path == script for pair in result.source_only_files)


def test_template_expression_round_trips_as_one_placeholder() -> None:
    protected = PlaceholderProtector().protect("Orb of ${orb.toUpperCase()}")

    assert protected.protected == "Orb of {{VAR}}"
    assert protected.restore("{{VAR}}의 오브") == "${orb.toUpperCase()}의 오브"


@pytest.mark.asyncio
async def test_tooltips_and_text_components_are_extracted(tmp_path: Path) -> None:
    script = tmp_path / "modpack/kubejs/client_scripts/tooltips.js"
    script.parent.mkdir(parents=True)
    script.write_text(
        "ItemEvents.tooltip(event => {\n"
        "  event.add('minecraft:stone', 'Very heavy rock')\n"
        "  event.add(['a:b', \"c:d\"], Text.gold('Shiny'))\n"
        "  event.add('x:y', Text.translate('tooltip.x'))\n"
        "  event.addAdvanced('z:w', (item, adv, text) => { text.add(1, Text.of('Line two')) })\n"
        "})\n"
        "event.create('gem').displayName('Gem').tooltip('Glows at night')\n"
        "const id = Text.of(someVar)\n"
        "console.log('not user text')\n",
        encoding="utf-8",
    )
    extracted = await KubeJSDisplayNameHandler().extract(script)
    assert extracted == {
        "display_name.0001": "Gem",
        "tooltip.0001": "Very heavy rock",
        "tooltip.0002": "Glows at night",
        "text.0001": "Shiny",
        "text.0002": "Line two",
    }


@pytest.mark.asyncio
async def test_tooltip_rewrite_touches_only_the_text(tmp_path: Path) -> None:
    script = tmp_path / "modpack/kubejs/client_scripts/tooltips.js"
    script.parent.mkdir(parents=True)
    script.write_text("event.add('minecraft:stone', 'Very heavy rock')\n", encoding="utf-8")
    out = tmp_path / "out.js"
    await KubeJSDisplayNameHandler().apply(script, {"tooltip.0001": "아주 무거운 돌"}, out)
    assert out.read_text(encoding="utf-8") == "event.add('minecraft:stone', '아주 무거운 돌')\n"


@pytest.mark.asyncio
async def test_tag_events_are_never_mistaken_for_tooltips(tmp_path: Path) -> None:
    script = tmp_path / "modpack/kubejs/server_scripts/tags.js"
    script.parent.mkdir(parents=True)
    script.write_text(
        "ServerEvents.tags('item', event => {\n"
        "  event.add('forge:ingots', 'kubejs:my_ingot')\n"
        "  event.add('forge:gems', 'Shiny Gem')\n"
        "})\n",
        encoding="utf-8",
    )
    assert await KubeJSDisplayNameHandler().extract(script) == {}


@pytest.mark.asyncio
async def test_client_tag_shaped_add_and_lang_entries_are_skipped(tmp_path: Path) -> None:
    script = tmp_path / "modpack/kubejs/client_scripts/lang.js"
    script.parent.mkdir(parents=True)
    script.write_text(
        "ClientEvents.lang('en_us', event => { event.add('item.kubejs.gem', 'Gem') })\n"
        "ItemEvents.tooltip(event => { event.add('#forge:ingots', 'forge:ores') })\n"
        "ItemEvents.tooltip(event => { event.add('#forge:ingots', 'Smeltable') })\n",
        encoding="utf-8",
    )
    assert await KubeJSDisplayNameHandler().extract(script) == {"tooltip.0001": "Smeltable"}
