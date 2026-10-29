"""Headless CLI: options reach the pipeline and --json is machine-readable."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from moru_engine.cli import build_parser, main

FIXTURE = Path(__file__).resolve().parents[1] / "test" / "modpack"


def test_translate_parses_the_extended_options() -> None:
    args = build_parser().parse_args(
        [
            "translate", "pack",
            "--speech-level", "banmal",
            "--term-style", "transliterate",
            "--exclude-mod", "jei", "--exclude-mod", "ae2",
            "--previous-modpack", "old", "--previous-resourcepack", "ko.zip",
            "--json",
        ]
    )
    assert args.speech_level == "banmal"
    assert args.exclude_mod == ["jei", "ae2"]
    assert args.previous_resourcepack == "ko.zip"


def test_previous_artifact_without_previous_pack_is_refused(tmp_path: Path) -> None:
    assert main(["translate", str(tmp_path), "--previous-resourcepack", "x.zip"]) == 1


def test_source_text_run_prints_a_json_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pack = tmp_path / "pack"
    shutil.copytree(FIXTURE, pack)
    code = main(
        ["translate", str(pack), "--source-text-only", "--output", str(tmp_path / "out"), "--json"]
    )
    summary = json.loads(capsys.readouterr().out)
    assert code == 0
    assert summary["stats"]["total_entries"] > 0
    assert summary["failed"] == []
