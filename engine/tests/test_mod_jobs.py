"""Sidecar consumers see bilingual exports and independent mod-source discovery."""

from __future__ import annotations

import asyncio
import json
import zipfile
from pathlib import Path

import httpx

from moru_engine import modsources
from moru_engine.server.app import create_app
from moru_engine.server.jobs import JobManager, JobRecord, JobStatus, JobType
from moru_engine.server.sessions import SessionStore
from test_per_mod import make_result


async def test_export_terminal_names_and_contents_include_bilingual_siblings(tmp_path: Path) -> None:
    result = make_result(tmp_path / "pack")
    store = SessionStore(tmp_path / "sessions")
    manager = JobManager(session_store=store)
    source = JobRecord(id="source", type=JobType.TRANSLATE,
                       params={"modpack_path": str(result.config.modpack_path)},
                       result=result, status=JobStatus.DONE, finished=True)
    manager.register_job(source)
    target = tmp_path / "export" / "Test Pack.zip"
    record = manager.create_job("export", {"translate_job_id": "source", "output_zip": str(target)})
    await record.task
    assert record.status is JobStatus.DONE, record.error
    payload = record.done_payload
    assert payload["bilingual_zip_path"] == str(target.with_name("Test Pack (병기).zip"))
    assert payload["bilingual_overrides_zip_path"] == str(target.with_name("Test Pack (병기)_overrides.zip"))
    for key, member, expected in (
        ("zip_path", "assets/alpha/lang/ko_kr.json", {"item.alpha.tool": "구리 지팡이"}),
        ("bilingual_zip_path", "assets/alpha/lang/ko_kr.json", {"item.alpha.tool": "구리 지팡이 (Copper Wand)"}),
        ("overrides_zip_path", "kubejs/assets/pack/lang/ko_kr.json", {"item.pack.extra": "추가"}),
        ("bilingual_overrides_zip_path", "kubejs/assets/pack/lang/ko_kr.json", {"item.pack.extra": "추가 (Extra)"}),
    ):
        with zipfile.ZipFile(payload[key]) as zf:
            assert json.loads(zf.read(member)) == expected
    summary = store.list_sessions()[0]
    assert summary["export_bilingual_zip_path"] == payload["bilingual_zip_path"]
    assert summary["export_bilingual_overrides_zip_path"] == payload["bilingual_overrides_zip_path"]
    # Empty trees are null, not stale paths from a previous export.
    result.entries = [entry for entry in result.entries if entry.file.startswith(".mct_cache/")]
    result.config.source_text_only = True
    result.config.bilingual_names = False
    record = manager.create_job("export", {"translate_job_id": "source", "output_zip": str(tmp_path / "source.zip")})
    await record.task
    assert record.done_payload["overrides_zip_path"] is None
    assert record.done_payload["bilingual_zip_path"] is None
    assert record.done_payload["bilingual_overrides_zip_path"] is None
    await manager.aclose()


async def test_scan_done_does_not_wait_for_lookup_and_failed_results_survive_restart(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "pack"
    mods = root / "mods"
    mods.mkdir(parents=True)
    for name in ("known.jar", "unknown.jar"):
        with zipfile.ZipFile(mods / name, "w") as zf:
            zf.writestr(f"assets/{Path(name).stem}/lang/en_us.json", '{"item.tool": "Tool"}')
    (root / "minecraftinstance.json").write_text(json.dumps({"installedAddons": [
        {"addonID": 1, "installedFile": {"id": 11, "fileName": "known.jar"}}
    ]}))
    ready, release = asyncio.Event(), asyncio.Event()

    class Client:
        enabled = True

        async def fingerprint_matches(self, fingerprints):
            ready.set()
            await release.wait()
            raise OSError("CurseForge unavailable")

    monkeypatch.setattr(modsources, "CurseForgeClient", Client)
    app = create_app("test-token", config_dir=tmp_path / "config")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://sidecar",
                                 headers={"Authorization": "Bearer test-token"}) as client:
        created = await client.post("/jobs", json={"type": "scan", "params": {"modpack_path": str(root)}})
        assert created.status_code == 201
        job_id = created.json()["id"]
        await asyncio.wait_for(ready.wait(), 10)
        assert (await client.get(f"/jobs/{job_id}")).json()["status"] == "done"
        pending = (await client.get(f"/jobs/{job_id}/mod-sources")).json()
        assert pending["status"] == "running"
        assert (pending["resolved"], pending["unresolved"]) == (1, 1)
        release.set()
        await app.state.job_manager.get(job_id).mod_sources_task
        finished = (await client.get(f"/jobs/{job_id}/mod-sources")).json()
        assert finished["status"] == "failed"
        assert finished["sources"][0] == {"jar_name": "known.jar", "project_id": 1, "file_id": 11, "source": "instance"}
        assert (await client.get(f"/jobs/{job_id}")).json()["status"] == "done"
        assert (await client.get("/jobs/missing/mod-sources")).status_code == 404
    await app.state.job_manager.aclose()
    restored = create_app("test-token", config_dir=tmp_path / "config")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=restored), base_url="http://sidecar",
                                 headers={"Authorization": "Bearer test-token"}) as client:
        assert (await client.get(f"/jobs/{job_id}/mod-sources")).json() == finished
    await restored.state.job_manager.aclose()
