"""Engine CLI.

Usage:
    uv run python -m moru_engine.cli scan ./test/modpack
    uv run python -m moru_engine.cli scan ./test/modpack --json
    uv run python -m moru_engine.cli translate ./pack --model openai/gpt-5.6-luna --json
    uv run python -m moru_engine.cli translate ./pack-v2 \\
        --previous-modpack ./pack-v1 --previous-resourcepack ./ko.zip
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import tempfile
from pathlib import Path
from typing import get_args

from .dspy_modules.signatures import SpeechLevel, TermStyle
from .pipeline import PipelineConfig, run_pipeline
from .scanner import ScanResult, scan_modpack
from .utils.log import setup_logging

logger = logging.getLogger(__name__)


def _relative(path: Path, root: Path) -> str:
    """Render a path relative to the modpack root when possible."""
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _scan_report(result: ScanResult) -> dict[str, object]:
    """Build a deterministic, JSON-serializable scan report."""
    root = result.modpack_path
    paired = sorted(
        {
            (
                _relative(p.source_path, root),
                _relative(p.target_path, root) if p.target_path else None,
            )
            for p in result.paired_files
        }
    )
    source_only = sorted(
        {_relative(p.source_path, root) for p in result.source_only_files}
    )
    target_only = sorted({_relative(p, root) for p in result.target_only_files})
    files = sorted(
        {
            (
                _relative(Path(f.input_path), root),
                f.file_type,
                f.lang_type,
                f.category,
            )
            for f in result.translation_files
        }
    )
    return {
        "modpack_path": str(root),
        "source_locale": result.source_locale,
        "target_locale": result.target_locale,
        "totals": {
            "source_files": result.total_source_files,
            "target_files": result.total_target_files,
            "paired": result.total_paired,
        },
        "paired": [{"source": s, "target": t} for s, t in paired],
        "source_only": source_only,
        "target_only": target_only,
        "files": [
            {"path": p, "file_type": ft, "lang_type": lt, "category": c}
            for p, ft, lt, c in files
        ],
    }


def _print_report(report: dict[str, object]) -> None:
    totals = report["totals"]
    assert isinstance(totals, dict)
    sys.stdout.write(f"Modpack: {report['modpack_path']}\n")
    sys.stdout.write(
        f"Locales: {report['source_locale']} -> {report['target_locale']}\n"
    )
    sys.stdout.write(
        "Totals: source={source_files} target={target_files} paired={paired}\n".format(
            **totals
        )
    )
    paired = report["paired"]
    assert isinstance(paired, list)
    sys.stdout.write(f"[paired] ({len(paired)})\n")
    for pair in paired:
        target = pair["target"] or "-"
        sys.stdout.write(f"  {pair['source']} -> {target}\n")
    source_only = report["source_only"]
    assert isinstance(source_only, list)
    sys.stdout.write(f"[source-only] ({len(source_only)})\n")
    for path in source_only:
        sys.stdout.write(f"  {path}\n")
    target_only = report["target_only"]
    assert isinstance(target_only, list)
    sys.stdout.write(f"[target-only] ({len(target_only)})\n")
    for path in target_only:
        sys.stdout.write(f"  {path}\n")
    files = report["files"]
    assert isinstance(files, list)
    sys.stdout.write(f"[files] ({len(files)})\n")
    for f in files:
        sys.stdout.write(
            f"  {f['path']} type={f['file_type']} lang={f['lang_type']}"
            f" category={f['category']}\n"
        )


async def _cmd_scan(args: argparse.Namespace) -> int:
    modpack = Path(args.modpack_path)
    if not modpack.exists():
        logger.error("Modpack path does not exist: %s", modpack)
        return 1
    result = await scan_modpack(
        modpack,
        source_locale=args.source,
        target_locale=args.target,
    )
    report = _scan_report(result)
    if args.json:
        sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    else:
        _print_report(report)
    return 0


async def _cmd_translate(args: argparse.Namespace) -> int:
    modpack = Path(args.modpack_path)
    if not modpack.exists():
        logger.error("Modpack path does not exist: %s", modpack)
        return 1
    previous_rp = Path(args.previous_resourcepack) if args.previous_resourcepack else None
    previous_ov = Path(args.previous_overrides) if args.previous_overrides else None
    if (previous_rp or previous_ov) and not args.previous_modpack:
        logger.error("--previous-resourcepack/--previous-overrides need --previous-modpack")
        return 1
    try:
        config = PipelineConfig(
            modpack_path=modpack,
            output_dir=Path(args.output) if args.output else None,
            source_locale=args.source,
            target_locale=args.target,
            model=args.model,
            api_base=args.api_base,
            reasoning_effort=args.reasoning_effort,
            use_tm=not args.no_tm,
            use_translation_graph=not args.no_graph,
            extract_glossary=args.extract_glossary,
            use_mod_translations=not args.no_mod_terms,
            use_vanilla_glossary=not args.no_vanilla_glossary,
            max_refine=args.max_refine,
            temperature=args.temperature,
            speech_level=args.speech_level,
            term_style=args.term_style,
            source_text_only=args.source_text_only,
            mod_blacklist=args.exclude_mod or None,
            include_categories=args.include_category or None,
            previous_modpack_path=Path(args.previous_modpack) if args.previous_modpack else None,
            previous_resourcepack_path=previous_rp,
            previous_overrides_path=previous_ov,
            **({"batch_size": args.batch_size} if args.batch_size else {}),
            **({"max_concurrent": args.concurrency} if args.concurrency else {}),
        )
    except ValueError as exc:  # pydantic ValidationError is a ValueError
        logger.error("Invalid options: %s", exc)
        return 1

    migration = None
    scan_result = None
    if config.previous_modpack_path is not None:
        # The server builds this index at scan time (W2); headless runs build
        # it here from the same scan the pipeline then reuses.
        from .migration import build_migration_catalog

        scan_result = await scan_modpack(
            modpack,
            source_locale=config.source_locale,
            target_locale=config.target_locale,
            mod_blacklist=config.mod_blacklist,
        )
        cache_dir = Path(tempfile.mkdtemp(prefix="moru-cli-migration-"))
        migration = await build_migration_catalog(
            previous_modpack_path=config.previous_modpack_path,
            previous_resourcepack_path=previous_rp,
            previous_overrides_path=previous_ov,
            current_modpack_root=modpack,
            current_scan=scan_result,
            source_locale=config.source_locale,
            target_locale=config.target_locale,
            asset_cache_dir=cache_dir,
        )

    def on_event(event: str, payload: dict[str, object]) -> None:
        if event == "progress":
            sys.stderr.write(
                f"\r[{payload.get('stage')}] {payload.get('file', '')} "
                f"{payload.get('done', 0)}/{payload.get('total', 0)}    "
            )
            sys.stderr.flush()

    result = await run_pipeline(
        config, on_event=on_event, scan_result=scan_result, migration=migration
    )
    sys.stderr.write("\n")
    stats = result.stats
    if args.json:
        summary = {
            "stats": stats.model_dump(mode="json"),
            "failed": [
                {"file": e.file, "key": e.key, "errors": e.errors} for e in result.failed
            ],
            "output_files": [str(path) for path in result.output_files],
        }
        sys.stdout.write(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
        return 0 if stats.failed_entries == 0 else 2
    sys.stdout.write(
        f"files={stats.total_files} entries={stats.total_entries} "
        f"translated={stats.translated_entries} tm_hits={stats.tm_hits} "
        f"failed={stats.failed_entries} skipped={stats.skipped_entries}\n"
        f"coverage={stats.coverage_percent}% quality={stats.quality_score} "
        f"tokens={stats.prompt_tokens}+{stats.completion_tokens} "
        f"duration={stats.duration_seconds}s\n"
    )
    for entry in result.failed[:20]:
        sys.stdout.write(f"FAILED {entry.file}:{entry.key}: {'; '.join(entry.errors)}\n")
    if len(result.failed) > 20:
        sys.stdout.write(f"... and {len(result.failed) - 20} more failures\n")
    sys.stdout.write(f"output files: {len(result.output_files)}\n")
    return 0 if stats.failed_entries == 0 else 2


async def _cmd_verify_agy(args: argparse.Namespace) -> int:
    """Print a raw `agy` translation envelope and a plain verdict."""
    from .cli_providers.antigravity import AgyError, verify_report

    try:
        ok, report = await asyncio.to_thread(verify_report, args.model)
    except AgyError as exc:
        print(f"VERDICT: FAIL — {exc}")
        return 1
    print(report)
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="moru-engine", description="Moru engine CLI")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="scan a modpack for translatable files")
    scan.add_argument("modpack_path")
    scan.add_argument("--source", default="en_us", help="source locale (xx_yy)")
    scan.add_argument("--target", default="ko_kr", help="target locale (xx_yy)")
    scan.add_argument("--json", action="store_true", help="machine-readable output")
    scan.set_defaults(func=_cmd_scan)

    translate = sub.add_parser("translate", help="translate a modpack")
    translate.add_argument("modpack_path")
    translate.add_argument("--source", default="en_us", help="source locale (xx_yy)")
    translate.add_argument("--target", default="ko_kr", help="target locale (xx_yy)")
    translate.add_argument(
        "--model",
        default="openai/gpt-5.6-luna",
        help="LiteLLM model string (API key via provider env var)",
    )
    translate.add_argument("--api-base", default=None, help="override base URL (Ollama)")
    translate.add_argument("--output", default=None, help="output directory")
    translate.add_argument("--no-tm", action="store_true", help="disable translation memory")
    translate.add_argument(
        "--no-graph",
        action="store_true",
        help="disable the entry relationship graph "
        "(name-first waves, glossary bindings, sibling context)",
    )
    translate.add_argument(
        "--extract-glossary",
        action="store_true",
        help="LLM glossary term extraction before translating",
    )
    translate.add_argument(
        "--no-mod-terms",
        action="store_true",
        help="skip harvesting terms from mods' own target-locale lang files",
    )
    translate.add_argument("--max-refine", type=int, default=2)
    translate.add_argument(
        "--temperature",
        type=float,
        default=0.3,
        help="sampling temperature (also busts the DSPy response cache)",
    )
    translate.add_argument(
        "--reasoning-effort",
        choices=["low", "medium", "high"],
        default=None,
        help="thinking budget for reasoning models",
    )
    translate.add_argument(
        "--speech-level",
        choices=list(get_args(SpeechLevel)),
        default="auto",
        help="Korean speech level of the output",
    )
    translate.add_argument(
        "--term-style",
        choices=list(get_args(TermStyle)),
        default="auto",
        help="translate or transliterate terms with no established name",
    )
    translate.add_argument("--batch-size", type=int, default=None, help="entries per request")
    translate.add_argument(
        "--concurrency", type=int, default=None, help="concurrent provider requests"
    )
    translate.add_argument(
        "--no-vanilla-glossary",
        action="store_true",
        help="do not seed the glossary with vanilla Minecraft names",
    )
    translate.add_argument(
        "--exclude-mod",
        action="append",
        default=[],
        metavar="MOD_ID",
        help="never translate this mod (repeatable)",
    )
    translate.add_argument(
        "--include-category",
        action="append",
        default=[],
        metavar="CATEGORY",
        help="translate only these scan categories (repeatable; see `scan`)",
    )
    translate.add_argument(
        "--source-text-only",
        action="store_true",
        help="write the source text into the output trees; no provider call",
    )
    translate.add_argument(
        "--previous-modpack",
        default=None,
        help="older version of the pack the previous translation was made for",
    )
    translate.add_argument(
        "--previous-resourcepack",
        default=None,
        help="previous translation's resource pack (.zip or folder)",
    )
    translate.add_argument(
        "--previous-overrides",
        default=None,
        help="previous translation's overrides (.zip or folder)",
    )
    translate.add_argument("--json", action="store_true", help="machine-readable summary")
    translate.set_defaults(func=_cmd_translate)

    verify_agy = sub.add_parser(
        "verify-agy",
        help="prove Antigravity CLI (agy) returns usable translations",
        description=(
            "Runs ONE real translation through `agy` headless mode and prints "
            "the raw JSON envelope. Requires an installed, signed-in agy "
            "(run `agy` once to sign in). Answers the one question that "
            "cannot be checked without a Google account: is the response the "
            "bare translation with protected tokens intact, or does the agent "
            "wrap it in commentary?"
        ),
    )
    verify_agy.add_argument(
        "--model",
        default=None,
        help="agy model slug (default: the transport's own default; "
        "list them with `agy models`)",
    )
    verify_agy.set_defaults(func=_cmd_verify_agy)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(logging.DEBUG if args.verbose else logging.WARNING)
    return asyncio.run(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
