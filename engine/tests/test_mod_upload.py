"""Real HTTP grouped uploads, batch limits and whole-pack-first registration."""

from __future__ import annotations

import asyncio
import io
import json
import threading
import zipfile
from pathlib import Path

import pytest
from aiohttp import web

from moru_engine import __version__
from moru_engine.server import jobs, upload
from moru_engine.server.jobs import JobManager, JobRecord, JobStatus, JobType
from moru_engine.server.sessions import SessionStore
from test_per_mod import make_result, resolved_sources


@pytest.fixture
async def platform(aiohttp_server):
    seen = {"slots": [], "registrations": [], "puts": {}, "active": 0, "maximum": 0,
            "fail_put": None, "fail_register": None, "order": []}

    async def slots(request):
        specs = (await request.json())["files"]
        seen["slots"].append(specs)
        seen["order"].append("mod_slots" if "group" in specs[0] else "pack_slots")
        return web.json_response({"uploads": [
            {"kind": spec["kind"], **({"group": spec["group"]} if "group" in spec else {}),
             "url": f"{seen['url']}/put/{spec.get('group', 'whole')}/{spec['kind']}",
             "object_key": f"translations/{spec.get('group', 'whole')}/{spec['kind']}.zip"}
            for spec in specs
        ]})

    async def put(request):
        group, kind = request.match_info["group"], request.match_info["kind"]
        if group == seen["fail_put"]:
            return web.json_response({"error": "storage unavailable"}, status=503)
        seen["active"] += 1
        seen["maximum"] = max(seen["maximum"], seen["active"])
        seen["puts"][(group, kind)] = await request.read()
        await asyncio.sleep(0.002)
        seen["active"] -= 1
        return web.Response(status=200)

    async def register(request):
        body = await request.json()
        seen["order"].append("pack_registered")
        seen["pack"] = body
        return web.json_response({"pack_id": "whole", "url": f"{seen['url']}/ko/pack/whole", "modpack_slug": "test-modpack"}, status=201)

    async def batch(request):
        assert request.headers["Authorization"] == "Bearer test-token"
        body = await request.json()
        seen["registrations"].append(body)
        seen["order"].append("mods_registered")
        return web.json_response({
            "packs": [{"curseforge_id": mod["curseforge_id"], "pack_id": f"p{mod['curseforge_id']}", "url": "https://moru.gg/ko/pack/p"}
                      for mod in body["mods"] if mod["curseforge_id"] != seen["fail_register"]],
            "failed": [{"curseforge_id": mod["curseforge_id"], "error": "invalid mod"}
                       for mod in body["mods"] if mod["curseforge_id"] == seen["fail_register"]],
        }, status=201)

    app = web.Application()
    app.add_routes([web.post("/api/upload-url", slots), web.put("/put/{group}/{kind}", put),
                    web.post("/api/translations", register), web.post("/api/translations/batch", batch)])
    server = await aiohttp_server(app)
    seen["url"] = f"http://{server.host}:{server.port}"
    return seen


async def test_batch_upload_is_bounded_isolated_and_reports_partial_failures(tmp_path: Path, platform) -> None:
    mods = []
    for project_id in range(1, 102):
        jar = f"mod-{project_id}.jar"
        zips = {}
        for kind in ("resource_pack", "resource_pack_bilingual"):
            archive = tmp_path / f"{project_id}-{kind}.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("content.json", json.dumps({"mod": project_id, "kind": kind}))
            zips[kind] = archive
        mods.append({"jar_name": jar, "display_name": f"Mod {project_id}", "project_id": project_id,
                     "file_id": project_id + 1000, "version_label": f"release-{project_id}",
                     "game_versions": ["1.20.1"], "zips": zips,
                     "stats": {"total_entries": 1, "translated_entries": 1, "failed_entries": 0, "coverage_percent": 100}})
    mods.append({**mods[0], "project_id": None, "jar_name": "unknown.jar"})
    platform.update(fail_put="mod-3.jar", fail_register=2)
    frames = []
    counts = await upload.upload_mod_packs(
        platform["url"], "test-token", mods,
        {"modpack_curseforge_id": 500, "modpack_name": "Test", "modpack_version": "1.0",
         "target_lang": "ko_kr", "source_lang": "en_us", "engine_version": __version__, "is_manual_translation": True},
        loader="forge", progress=lambda *frame: frames.append(frame),
    )
    assert counts == {"shared": 99, "skipped": 1, "failed": 2}
    assert list(map(len, platform["slots"])) == [200, 2]
    assert 1 < platform["maximum"] <= 6
    assert frames[-1][0:2] == (101, 101)
    assert [len(batch["mods"]) for batch in platform["registrations"]] == [99, 1]
    for batch in platform["registrations"]:
        assert batch["is_manual_translation"] is True
        assert batch["modpack_curseforge_id"] == 500
        for mod in batch["mods"]:
            assert mod["curseforge_file_id"] == mod["curseforge_id"] + 1000
            assert mod["loader"] == "forge"
            assert set(mod) == {"curseforge_id", "curseforge_file_id", "name", "version", "game_versions", "loader", "files", "stats"}
            for file in mod["files"]:
                assert set(file) == {"kind", "object_key"}
                group = file["object_key"].split("/")[1]
                with zipfile.ZipFile(io.BytesIO(platform["puts"][(group, file["kind"])])) as zf:
                    assert json.loads(zf.read("content.json")) == {"mod": mod["curseforge_id"], "kind": file["kind"]}


async def test_upload_job_registers_whole_pack_before_mods_and_cleans_staging(tmp_path: Path, platform, monkeypatch) -> None:
    result = make_result(tmp_path / "pack")
    (result.config.modpack_path / "manifest.json").write_text(json.dumps({
        "manifestType": "minecraftModpack", "name": "Test", "version": "1.0",
        "minecraft": {"version": "1.20.1", "modLoaders": [{"id": "forge-47.0.0", "primary": True}]},
    }))
    staging = tmp_path / "upload-stage"
    staging.mkdir()
    monkeypatch.setattr("moru_engine.server.jobs.tempfile.mkdtemp", lambda **kwargs: str(staging))
    manager = JobManager(session_store=SessionStore(tmp_path / "sessions"))
    source = JobRecord(id="source", type=JobType.TRANSLATE,
                       params={"modpack_path": str(result.config.modpack_path)},
                       result=result, status=JobStatus.DONE, finished=True, mod_sources=resolved_sources())
    manager.register_job(source)
    record = manager.create_job("upload", {
        "translate_job_id": "source", "modpack_name": "Test", "modpack_version": "1.0",
        "curseforge_id": 500, "api_token": "test-token", "web_url": platform["url"],
    })
    await record.task
    assert record.status is JobStatus.DONE, record.error
    assert record.done_payload["mod_packs"] == {"shared": 2, "skipped": 1, "failed": 0}
    assert record.done_payload["modpack_slug"] == "test-modpack"
    assert "bundle_url" not in record.done_payload
    assert platform["pack"]["mod_pack_count"] == 2
    assert platform["pack"]["is_manual_translation"] is True
    assert [file["kind"] for file in platform["pack"]["files"]] == [
        "resource_pack", "overrides", "resource_pack_bilingual", "overrides_bilingual"
    ]
    assert platform["order"].index("pack_registered") < platform["order"].index("mod_slots")
    assert any(frame.get("stage") == "mods" and frame.get("message") == "Beta" for frame in record.history)
    assert not staging.exists()
    await manager.aclose()


async def test_anonymous_mod_sharing_is_skipped_without_hiding_whole_pack(tmp_path: Path, platform) -> None:
    result = make_result(tmp_path / "pack")
    manager = JobManager(session_store=SessionStore(tmp_path / "sessions"))
    source = JobRecord(id="source", type=JobType.TRANSLATE, params={}, result=result,
                       status=JobStatus.DONE, finished=True)
    manager.register_job(source)
    record = manager.create_job("upload", {
        "translate_job_id": "source", "modpack_name": "Test", "web_url": platform["url"],
    })
    await record.task
    assert record.status is JobStatus.DONE, record.error
    assert record.done_payload["mod_packs"] == {"shared": 0, "skipped": 3, "failed": 0, "skipped_reason": "login_required"}
    assert platform["pack"]["mod_pack_count"] == 0
    assert platform["registrations"] == []
    assert source.mod_sources_task is None
    disabled = manager.create_job("upload", {
        "translate_job_id": "source", "modpack_name": "Test", "web_url": platform["url"],
        "api_token": "test-token", "share_mods": False,
    })
    await disabled.task
    assert disabled.status is JobStatus.DONE, disabled.error
    assert disabled.done_payload["mod_packs"]["skipped_reason"] == "disabled"
    assert platform["registrations"] == []
    await manager.aclose()


async def test_cancelled_upload_joins_archive_writer_before_cleanup(tmp_path: Path, monkeypatch) -> None:
    result = make_result(tmp_path / "pack")
    manager = JobManager(session_store=SessionStore(tmp_path / "sessions"))
    source = JobRecord(id="source", type=JobType.TRANSLATE, params={}, result=result,
                       status=JobStatus.DONE, finished=True)
    manager.register_job(source)
    staging = tmp_path / "upload-stage"
    staging.mkdir()
    monkeypatch.setattr(jobs.tempfile, "mkdtemp", lambda **kwargs: str(staging))
    started, release = threading.Event(), threading.Event()
    original = jobs.create_zip_from_directory

    def write_archive(tree, archive):
        started.set()
        assert release.wait(5)
        original(tree, archive)

    monkeypatch.setattr(jobs, "create_zip_from_directory", write_archive)
    record = manager.create_job("upload", {"translate_job_id": "source", "modpack_name": "Test"})
    assert await asyncio.to_thread(started.wait, 5)
    manager.cancel(record.id)
    release.set()
    await record.task
    assert record.status is JobStatus.CANCELLED
    assert not staging.exists()
    await manager.aclose()
