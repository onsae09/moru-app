"""Launcher priority, fingerprint fallback and persistence of scan identities."""

from __future__ import annotations

import json
from pathlib import Path

from moru_engine.curseforge import CurseForgeClient, curseforge_fingerprint
from moru_engine.migration.previous_translation import _current_archive_files
from moru_engine.modsources import ModSources, resolve_mod_sources
from moru_engine.pipeline import PipelineConfig, PipelineResult
from moru_engine.server.jobs import JobManager, JobRecord, JobStatus, JobType
from moru_engine.server.sessions import SessionStore


def _instance(root: Path) -> Path:
    game = root / ".minecraft"
    mods = game / "mods"
    index = mods / ".index"
    index.mkdir(parents=True)
    for name in ("instance.jar", "prism.jar", "modified.jar", "unknown.jar"):
        (mods / name).write_bytes(name.encode())
    (root / "minecraftinstance.json").write_text(json.dumps({"installedAddons": [
        {"addonID": 1, "fileNameOnDisk": "INSTANCE.jar", "installedFile": {"id": 11}},
        {"addonID": 3, "isModified": True, "installedFile": {"id": 33, "fileName": "modified.jar"}},
        {"addonID": True, "installedFile": {"id": 22, "fileName": "unknown.jar"}},
    ]}))
    (index / "instance.pw.toml").write_text('filename = "instance.jar"\n[update.curseforge]\nproject-id = 9\nfile-id = 99\n')
    (index / "prism.pw.toml").write_text('filename = "prism.jar"\n[update.curseforge]\nproject-id = 2\nfile-id = 22\n')
    (index / "broken.pw.toml").write_text('filename = "broken.jar"\n[update')
    return game


async def test_launchers_precede_fingerprints_and_modified_addons_are_not_trusted(tmp_path: Path) -> None:
    game = _instance(tmp_path)
    fingerprint = curseforge_fingerprint(game / "mods" / "modified.jar")

    class Client:
        enabled = True

        async def fingerprint_matches(self, fingerprints):
            assert set(fingerprints) == {
                fingerprint, curseforge_fingerprint(game / "mods" / "unknown.jar")
            }
            return {fingerprint: (30, 330, ["1.20.1", "Forge"], "Modified release")}

    state = await resolve_mod_sources(game, ModSources(), Client())
    assert _current_archive_files(tmp_path) == {"instance.jar": (1, 11)}
    assert state.payload() == {
        "status": "done", "total": 4, "resolved": 3, "unresolved": 1,
        "sources": [
            {"jar_name": "instance.jar", "project_id": 1, "file_id": 11, "source": "instance"},
            {"jar_name": "modified.jar", "project_id": 30, "file_id": 330, "source": "fingerprint"},
            {"jar_name": "prism.jar", "project_id": 2, "file_id": 22, "source": "prism"},
            {"jar_name": "unknown.jar", "project_id": None, "file_id": None, "source": "unresolved"},
        ],
    }
    store = SessionStore(tmp_path / "sessions")
    store.save_mod_sources("scan", state)
    restored = store.load_mod_sources("scan")
    assert restored == state
    record = JobRecord(
        id="translate", type=JobType.TRANSLATE, params={"modpack_path": str(game)},
        status=JobStatus.DONE, finished=True,
        result=PipelineResult(config=PipelineConfig(modpack_path=game, manual_seed=True)),
        mod_sources=state,
    )
    store.save_job_session(record)
    restored_job = store.load_job_session("translate")
    assert restored_job.mod_sources == state
    assert restored_job.result.config.manual_seed is True
    manager = JobManager(session_store=store)
    assert manager.get_mod_sources("scan").payload() == state.payload()
    assert manager.get_mod_sources("translate").sources[1].version_label == "Modified release"
    await manager.aclose()


async def test_lookup_failure_retains_local_ids_and_disabled_client_skips_hashes(tmp_path: Path) -> None:
    game = _instance(tmp_path)

    class BrokenClient:
        enabled = True

        async def fingerprint_matches(self, fingerprints):
            raise OSError("upstream unavailable")

    failed = await resolve_mod_sources(game, ModSources(), BrokenClient())
    assert failed.status == "failed"
    assert failed.payload()["resolved"] == 2
    assert [source.source for source in failed.sources] == ["instance", "unresolved", "prism", "unresolved"]
    disabled = await resolve_mod_sources(game, ModSources(), CurseForgeClient(""))
    assert disabled.status == "done"
    assert disabled.sources == failed.sources
