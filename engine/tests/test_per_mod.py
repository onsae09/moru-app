"""Per-JAR archives preserve namespaces, reviewed text and bilingual siblings."""

from __future__ import annotations

import asyncio
import json
import zipfile
from pathlib import Path

from moru_engine.modsources import ModSource, ModSources
from moru_engine.output.per_mod import build_mod_packs
from moru_engine.pipeline import EntryResult, EntryStatus, PipelineConfig, PipelineResult
from moru_engine.scanner.pack_identity import PackIdentity


def make_result(root: Path) -> PipelineResult:
    entries = []
    namespaces = {}
    for jar, namespace, word, translated in (
        ("alpha-1.jar", "alpha", "Copper Wand", "구리 지팡이"),
        ("beta-2.jar", "beta", "Iron Pickaxe", "철 곡괭이"),
        ("unknown.jar", "unknown", "Silver Shovel", "은 삽"),
    ):
        relative = f".mct_cache/extracted/{jar}/lang/en_us.json"
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({f"item.{namespace}.tool": word, f"item.{namespace}.pending": "Unfinished"}))
        namespaces[path.resolve().as_posix()] = namespace
        entries += [
            EntryResult(file=relative, key=f"item.{namespace}.tool", source_text=word, translated_text=translated, status=EntryStatus.MODIFIED),
            EntryResult(file=relative, key=f"item.{namespace}.pending", source_text="Unfinished", translated_text="must not ship", status=EntryStatus.PENDING),
            EntryResult(file=relative, key=f"item.{namespace}.failed", source_text="Failed", translated_text="must not ship", status=EntryStatus.FAILED),
        ]
    override = "kubejs/assets/pack/lang/en_us.json"
    path = root / override
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"item.pack.extra": "Extra"}')
    entries.append(EntryResult(file=override, key="item.pack.extra", source_text="Extra", translated_text="추가", status=EntryStatus.MODIFIED))
    return PipelineResult(
        config=PipelineConfig(modpack_path=root, output_dir=root / "output", manual_seed=True),
        entries=entries, namespaces=namespaces,
    )


def resolved_sources() -> ModSources:
    return ModSources(status="done", sources=[
        ModSource("alpha-1.jar", 1, 101, "instance"),
        ModSource("beta-2.jar", 2, 202, "fingerprint", ["Forge", "1.20.1", "1.21", "24w14a", "1.20.1"], "Beta release 2"),
        ModSource("unknown.jar"),
    ])


async def test_mod_packs_keep_each_jar_isolated_and_preserve_both_variants(tmp_path: Path) -> None:
    result = make_result(tmp_path / "pack")
    packs = await asyncio.to_thread(
        build_mod_packs, result, resolved_sources(), tmp_path / "stage",
        PackIdentity(mc_version="1.20.1", loader="forge"),
    )
    assert [pack["jar_name"] for pack in packs] == ["alpha-1.jar", "beta-2.jar", "unknown.jar"]
    assert packs[0]["version_label"] == "alpha-1"
    assert packs[0]["game_versions"] == ["1.20.1"]
    assert packs[1]["version_label"] == "Beta release 2"
    assert packs[1]["game_versions"] == ["1.20.1", "1.21"]
    assert packs[2]["project_id"] is None
    assert packs[0]["stats"] == {
        "total_entries": 3, "translated_entries": 1,
        "failed_entries": 1, "coverage_percent": 33.33,
    }
    for pack, namespace, translated, source in zip(
        packs, ("alpha", "beta", "unknown"),
        ("구리 지팡이", "철 곡괭이", "은 삽"),
        ("Copper Wand", "Iron Pickaxe", "Silver Shovel"), strict=True,
    ):
        assert set(pack["zips"]) == {"resource_pack", "resource_pack_bilingual"}
        for kind, archive in pack["zips"].items():
            assert archive.name == f"{kind}.zip"
            with zipfile.ZipFile(archive) as zf:
                member = f"assets/{namespace}/lang/ko_kr.json"
                assert set(zf.namelist()) == {member, "pack.mcmeta", "pack.png"}
                expected = translated if kind == "resource_pack" else f"{translated} ({source})"
                assert json.loads(zf.read(member)) == {f"item.{namespace}.tool": expected}
                metadata = json.loads(zf.read("pack.mcmeta"))["pack"]
                assert metadata["pack_format"] == 15
                assert "moru.gg" in metadata["description"]


async def test_unfinished_mod_has_no_empty_metadata_only_archives(tmp_path: Path) -> None:
    result = make_result(tmp_path / "pack")
    result.entries = [entry for entry in result.entries if entry.status is EntryStatus.PENDING]
    packs = await asyncio.to_thread(build_mod_packs, result, resolved_sources(), tmp_path / "stage")
    assert [pack["zips"] for pack in packs] == [{}, {}, {}]
    assert all(pack["stats"]["translated_entries"] == 0 for pack in packs)
