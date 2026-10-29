"""Tests for the FastAPI sidecar (moru_engine.server).

Translate jobs are never run here (they need an LLM); scan jobs run for
real against the repo fixture at test/modpack, which is fast and LLM-free.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import Mock

import aiohttp
import pytest
from fastapi.testclient import TestClient
from moru_engine import __version__
from moru_engine.cli_providers.credentials import STORES
from moru_engine.community import TranslationMatch
from moru_engine.graph import TranslationGraph
from moru_engine.pipeline import (
    EntryResult,
    EntryStatus,
    PipelineConfig,
    PipelineResult,
    PipelineStats,
    TranslationPipeline,
)
from moru_engine.server import create_app
from moru_engine.server.jobs import JobRecord, JobStatus, JobType
from moru_engine.server.live_models import fetch_live_models
from moru_engine.server.upload import WebUploadError, _auth_headers
from starlette.websockets import WebSocketDisconnect

if TYPE_CHECKING:
    from collections.abc import Iterator

TOKEN = "test-session-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
MODPACK = Path(__file__).resolve().parents[1] / "test" / "modpack"


@pytest.fixture(scope="module")
def shutdown_flag() -> threading.Event:
    return threading.Event()


@pytest.fixture(scope="module")
def client(
    tmp_path_factory: pytest.TempPathFactory, shutdown_flag: threading.Event
) -> Iterator[TestClient]:
    root = tmp_path_factory.mktemp("server")
    app = create_app(
        token=TOKEN,
        config_dir=root / "config",
        tm_db_path=root / "tm.sqlite3",
        shutdown_handler=shutdown_flag.set,
        shutdown_delay=0.0,
    )
    # Context manager keeps one event loop (anyio portal) alive across
    # requests, so background job tasks actually run between polls.
    with TestClient(app) as test_client:
        yield test_client


def _wait_for_job(
    client: TestClient, job_id: str, timeout: float = 60.0
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/jobs/{job_id}", headers=AUTH)
        assert response.status_code == 200
        body = response.json()
        if body["status"] in {"done", "failed", "cancelled"}:
            return body
        time.sleep(0.05)
    pytest.fail(f"job {job_id} did not finish within {timeout}s")


@pytest.fixture(scope="module")
def scan_job(client: TestClient) -> dict[str, Any]:
    """A real, completed scan job over the repo fixture modpack."""
    assert MODPACK.is_dir(), f"missing fixture: {MODPACK}"
    response = client.post(
        "/jobs",
        json={"type": "scan", "params": {"modpack_path": str(MODPACK)}},
        headers=AUTH,
    )
    assert response.status_code == 201, response.text
    job = response.json()
    assert job["type"] == "scan"
    assert job["status"] in {"pending", "running"}
    assert job["created_at"]
    final = _wait_for_job(client, job["id"])
    assert final["status"] == "done", final
    return final


# -- health and auth ---------------------------------------------------------


def test_health_requires_no_auth(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__}


def test_missing_token_is_401(client: TestClient) -> None:
    assert client.get("/providers").status_code == 401
    assert client.get("/config").status_code == 401
    assert client.post("/jobs", json={"type": "scan", "params": {}}).status_code == 401


def test_wrong_token_is_401(client: TestClient) -> None:
    response = client.get("/providers", headers={"Authorization": "Bearer wrong-token"})
    assert response.status_code == 401


def test_valid_token_is_200(client: TestClient) -> None:
    response = client.get("/providers", headers=AUTH)
    assert response.status_code == 200
    providers = {p["id"]: p for p in response.json()}
    assert {"openai", "anthropic", "ollama", "openai-compatible"} <= providers.keys()
    assert providers["ollama"]["has_key"] is True
    # No key requirement and no static catalog: models come live from the
    # user's own server (LM Studio, llama.cpp, ...).
    assert providers["openai-compatible"]["has_key"] is True
    assert providers["openai-compatible"]["models"] == []
    for provider in providers.values():
        if provider["id"] != "openai-compatible":
            assert provider["models"], provider["id"]
        assert isinstance(provider["has_key"], bool)


# -- jobs: scan flow -----------------------------------------------------------


def test_scan_job_completes(scan_job: dict[str, Any]) -> None:
    assert scan_job["status"] == "done"
    assert scan_job["error"] is None


def test_scan_result_category_tree(
    client: TestClient, scan_job: dict[str, Any]
) -> None:
    response = client.get(f"/scan/{scan_job['id']}/result", headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert body["modpack_path"] == str(MODPACK)
    categories = body["categories"]
    assert isinstance(categories, list) and categories
    names = {c["name"] for c in categories}
    assert "KubeJS" in names
    for category in categories:
        assert set(category) == {
            "name",
            "handler",
            "file_count",
            "entry_count",
            "char_count",
            "files",
        }
        assert category["file_count"] == len(category["files"]) > 0
        assert category["entry_count"] == sum(
            f["entry_count"] for f in category["files"]
        )
        for file_info in category["files"]:
            assert file_info["path"]
            assert set(file_info) == {"path", "entry_count", "char_count", "sample"}
    # The parse pass counts real entries: the fixture modpack has content.
    assert sum(c["entry_count"] for c in categories) > 0
    assert sum(c["char_count"] for c in categories) > 0


def test_scan_counts_only_entries_missing_target_locale(
    client: TestClient, scan_job: dict[str, Any]
) -> None:
    body = client.get(f"/scan/{scan_job['id']}/result", headers=AUTH).json()
    kubejs = next(
        category for category in body["categories"] if category["name"] == "KubeJS"
    )
    file_info = next(
        file
        for file in kubejs["files"]
        if Path(file["path"]).name.lower() == "en_us.json"
    )
    source_path = Path(file_info["path"])
    target_path = source_path.with_name("ko_kr.json")
    source = json.loads(source_path.read_text(encoding="utf-8"))
    existing = json.loads(target_path.read_text(encoding="utf-8"))
    pending = {
        key: value
        for key, value in source.items()
        if not str(existing.get(key, "")).strip()
    }

    assert file_info["entry_count"] == len(pending)
    assert file_info["entry_count"] < len(source)
    assert file_info["char_count"] == sum(len(value) for value in pending.values())
    assert file_info["sample"] == {
        key: value[:160] for key, value in list(pending.items())[:3]
    }


def test_scan_treats_source_copy_target_as_pending(
    client: TestClient,
    tmp_path: Path,
) -> None:
    lang = tmp_path / "copy-target/kubejs/assets/demo/lang"
    lang.mkdir(parents=True)
    (lang / "en_us.json").write_text(
        '{"copied":"Same English","done":"Finished"}',
        encoding="utf-8",
    )
    (lang / "ko_kr.json").write_text(
        '{"copied":"Same English","done":"완료"}',
        encoding="utf-8",
    )
    response = client.post(
        "/jobs",
        json={
            "type": "scan",
            "params": {"modpack_path": str(tmp_path / "copy-target")},
        },
        headers=AUTH,
    )
    assert response.status_code == 201
    job = _wait_for_job(client, response.json()["id"])
    assert job["status"] == "done", job
    payload = client.get(f"/scan/{job['id']}/result", headers=AUTH).json()
    files = [file for category in payload["categories"] for file in category["files"]]
    file_info = next(file for file in files if file["path"].endswith("en_us.json"))
    assert file_info["entry_count"] == 1
    assert file_info["sample"] == {"copied": "Same English"}


def test_scan_result_samples_are_bounded(
    client: TestClient, scan_job: dict[str, Any]
) -> None:
    body = client.get(f"/scan/{scan_job['id']}/result", headers=AUTH).json()
    saw_sample = False
    for category in body["categories"]:
        for file_info in category["files"]:
            sample = file_info["sample"]
            assert len(sample) <= 3
            for value in sample.values():
                assert len(value) <= 160
            if sample:
                saw_sample = True
    assert saw_sample, "no file produced a sample"


def test_scan_ws_emits_parse_stage(
    client: TestClient, scan_job: dict[str, Any]
) -> None:
    frames: list[dict[str, Any]] = []
    with client.websocket_connect(f"/jobs/{scan_job['id']}/events?token={TOKEN}") as ws:
        while True:
            frame = ws.receive_json()
            frames.append(frame)
            if frame["type"] in {"done", "failed", "cancelled"}:
                break
    stages = {f.get("stage") for f in frames if f["type"] == "progress"}
    assert "parse" in stages


def test_scan_migration_counts_and_translate_reuses_scan_index(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = tmp_path / "current"
    old = tmp_path / "old"
    translated = tmp_path / "translated"
    relative = Path("kubejs/assets/demo/lang/en_us.json")
    for root in (current, old):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"same":"Same","changed":"New"}', encoding="utf-8")
    target = translated / "kubejs/assets/demo/lang/ko_kr.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        '{"same":"수동 번역","changed":"변경 전 번역"}',
        encoding="utf-8",
    )

    response = client.post(
        "/jobs",
        headers=AUTH,
        json={
            "type": "scan",
            "params": {
                "modpack_path": str(current),
                "previous_modpack_path": str(old),
                "previous_overrides_path": str(translated),
            },
        },
    )
    assert response.status_code == 201, response.text
    scan = _wait_for_job(client, response.json()["id"])
    assert scan["status"] == "done", scan
    scan_payload = client.get(f"/scan/{scan['id']}/result", headers=AUTH).json()
    assert scan_payload["migration"]["entry_count"] == 2
    assert scan_payload["migration"]["char_count"] == len("Same") + len("New")
    file_payload = scan_payload["categories"][0]["files"][0]
    assert file_payload["migration_entry_count"] == 2

    captured: dict[str, Any] = {}

    async def fake_run_pipeline(
        config: PipelineConfig, **kwargs: Any
    ) -> PipelineResult:
        captured.update(kwargs)
        return PipelineResult(config=config)

    monkeypatch.setattr("moru_engine.server.jobs.run_pipeline", fake_run_pipeline)
    response = client.post(
        "/jobs",
        headers=AUTH,
        json={
            "type": "translate",
            "params": {
                "modpack_path": str(current),
                "scan_job_id": scan["id"],
                "previous_modpack_path": str(old),
                "previous_overrides_path": str(translated),
                "use_tm": False,
            },
        },
    )
    assert response.status_code == 201, response.text
    translated_job = _wait_for_job(client, response.json()["id"])
    assert translated_job["status"] == "done", translated_job
    stored = client.app.state.job_manager._jobs[scan["id"]].result
    assert stored.migration_input_fingerprint is not None
    assert captured["scan_result"] is stored.scan
    assert captured["migration"] is stored.migration

    # A scan result is only an optimization. If C changes after W2, W4 must
    # fall back to the normal fresh scan/index path instead of using stale data.
    (current / relative).write_text(
        '{"same":"Same","changed":"Newest","added":"Added"}',
        encoding="utf-8",
    )
    captured.clear()
    response = client.post(
        "/jobs",
        headers=AUTH,
        json={
            "type": "translate",
            "params": {
                "modpack_path": str(current),
                "scan_job_id": scan["id"],
                "previous_modpack_path": str(old),
                "previous_overrides_path": str(translated),
                "use_tm": False,
            },
        },
    )
    assert response.status_code == 201, response.text
    refreshed_job = _wait_for_job(client, response.json()["id"])
    assert refreshed_job["status"] == "done", refreshed_job
    assert captured["scan_result"] is None
    assert captured["migration"] is None


def test_migration_scan_refreshes_when_launcher_metadata_changes(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = tmp_path / "current"
    old = tmp_path / "old"
    translated = tmp_path / "translated"
    for root in (current, old, translated):
        root.mkdir()

    captured: dict[str, Any] = {}

    async def fake_run_pipeline(
        config: PipelineConfig, **kwargs: Any
    ) -> PipelineResult:
        captured.update(kwargs)
        return PipelineResult(config=config)

    monkeypatch.setattr("moru_engine.server.jobs.run_pipeline", fake_run_pipeline)
    scan = client.post(
        "/jobs",
        headers=AUTH,
        json={
            "type": "scan",
            "params": {
                "modpack_path": str(current),
                "previous_modpack_path": str(old),
                "previous_overrides_path": str(translated),
            },
        },
    ).json()
    assert _wait_for_job(client, scan["id"])["status"] == "done"

    # These launcher files affect pack identity and generated pack metadata,
    # even though they are outside the translation-content folders.
    (current / "modrinth.index.json").write_text(
        '{"name":"Demo","versionId":"2.0","dependencies":{"minecraft":"1.21"}}',
        encoding="utf-8",
    )
    response = client.post(
        "/jobs",
        headers=AUTH,
        json={
            "type": "translate",
            "params": {
                "modpack_path": str(current),
                "scan_job_id": scan["id"],
                "previous_modpack_path": str(old),
                "previous_overrides_path": str(translated),
                "use_tm": False,
            },
        },
    )
    assert response.status_code == 201, response.text
    assert _wait_for_job(client, response.json()["id"])["status"] == "done"
    assert captured["scan_result"] is None
    assert captured["migration"] is None


def test_migration_scan_refreshes_when_an_openloader_pack_changes(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OpenLoader and Paxi mount packs from their own top-level folders, so
    the scanner reads them like resourcepacks/ and the freshness check has to
    cover them too."""
    current = tmp_path / "current"
    old = tmp_path / "old"
    translated = tmp_path / "translated"
    for root in (current, old, translated):
        root.mkdir()

    captured: dict[str, Any] = {}

    async def fake_run_pipeline(
        config: PipelineConfig, **kwargs: Any
    ) -> PipelineResult:
        captured.update(kwargs)
        return PipelineResult(config=config)

    monkeypatch.setattr("moru_engine.server.jobs.run_pipeline", fake_run_pipeline)
    scan = client.post(
        "/jobs",
        headers=AUTH,
        json={
            "type": "scan",
            "params": {
                "modpack_path": str(current),
                "previous_modpack_path": str(old),
                "previous_overrides_path": str(translated),
            },
        },
    ).json()
    assert _wait_for_job(client, scan["id"])["status"] == "done"

    translate = {
        "type": "translate",
        "params": {
            "modpack_path": str(current),
            "scan_job_id": scan["id"],
            "previous_modpack_path": str(old),
            "previous_overrides_path": str(translated),
            "use_tm": False,
        },
    }
    response = client.post("/jobs", headers=AUTH, json=translate)
    assert response.status_code == 201, response.text
    assert _wait_for_job(client, response.json()["id"])["status"] == "done"
    # Baseline: untouched inputs reuse the W2 scan and its A/B/C index, so the
    # rebuild below can only come from the openloader pack.
    stored = client.app.state.job_manager._jobs[scan["id"]].result
    assert captured["scan_result"] is stored.scan
    assert captured["migration"] is stored.migration

    pack = current / "openloader" / "resources" / "extra" / "pack.mcmeta"
    pack.parent.mkdir(parents=True, exist_ok=True)
    pack.write_text('{"pack":{"pack_format":15}}', encoding="utf-8")
    captured.clear()
    response = client.post("/jobs", headers=AUTH, json=translate)
    assert response.status_code == 201, response.text
    assert _wait_for_job(client, response.json()["id"])["status"] == "done"
    assert captured["scan_result"] is None
    assert captured["migration"] is None


def test_translate_without_migration_reuses_matching_v1_scan(
    client: TestClient,
    scan_job: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_run_pipeline(
        config: PipelineConfig, **kwargs: Any
    ) -> PipelineResult:
        captured.update(kwargs)
        return PipelineResult(config=config)

    monkeypatch.setattr("moru_engine.server.jobs.run_pipeline", fake_run_pipeline)
    response = client.post(
        "/jobs",
        headers=AUTH,
        json={
            "type": "translate",
            "params": {
                "modpack_path": str(MODPACK),
                "scan_job_id": scan_job["id"],
                "use_tm": False,
            },
        },
    )
    assert response.status_code == 201, response.text
    final = _wait_for_job(client, response.json()["id"])
    assert final["status"] == "done", final
    stored = client.app.state.job_manager._jobs[scan_job["id"]].result
    assert captured["scan_result"] is stored.scan
    assert captured["migration"] is None


def test_translate_falls_back_when_the_scan_job_does_not_match(
    client: TestClient,
    scan_job: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Param drift makes the scan unusable, not the run impossible."""
    different = tmp_path / "different"
    different.mkdir()
    old = tmp_path / "old-for-mismatch"
    translated = tmp_path / "translated-for-mismatch"
    old.mkdir()
    translated.mkdir()
    captured: dict[str, Any] = {}

    async def fake_run_pipeline(
        config: PipelineConfig, **kwargs: Any
    ) -> PipelineResult:
        captured.update(kwargs)
        return PipelineResult(config=config)

    monkeypatch.setattr("moru_engine.server.jobs.run_pipeline", fake_run_pipeline)
    response = client.post(
        "/jobs",
        headers=AUTH,
        json={
            "type": "translate",
            "params": {
                "modpack_path": str(different),
                "scan_job_id": scan_job["id"],
                "previous_modpack_path": str(old),
                "previous_overrides_path": str(translated),
                "use_tm": False,
            },
        },
    )
    assert response.status_code == 201, response.text
    assert _wait_for_job(client, response.json()["id"])["status"] == "done"
    assert captured["scan_result"] is None
    assert captured["migration"] is None


def test_translate_falls_back_when_the_scan_job_is_gone(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scan jobs are never persisted, so a sidecar restart under a live
    renderer leaves every wizard holding an unresolvable scan id. Start must
    re-scan instead of failing the run."""
    captured: dict[str, Any] = {}

    async def fake_run_pipeline(
        config: PipelineConfig, **kwargs: Any
    ) -> PipelineResult:
        captured.update(kwargs)
        return PipelineResult(config=config)

    monkeypatch.setattr("moru_engine.server.jobs.run_pipeline", fake_run_pipeline)
    response = client.post(
        "/jobs",
        headers=AUTH,
        json={
            "type": "translate",
            "params": {
                "modpack_path": str(MODPACK),
                "scan_job_id": str(uuid.uuid4()),
                "use_tm": False,
            },
        },
    )
    assert response.status_code == 201, response.text
    assert _wait_for_job(client, response.json()["id"])["status"] == "done"
    assert captured["scan_result"] is None
    assert captured["migration"] is None


def test_scan_result_of_unknown_job_is_404(client: TestClient) -> None:
    assert client.get("/scan/nope/result", headers=AUTH).status_code == 404


def test_job_status_of_unknown_job_is_404(client: TestClient) -> None:
    assert client.get("/jobs/nope", headers=AUTH).status_code == 404


def test_cancel_unknown_job_is_404(client: TestClient) -> None:
    assert client.post("/jobs/nope/cancel", headers=AUTH).status_code == 404


def test_scan_job_missing_modpack_path_is_422(client: TestClient) -> None:
    response = client.post("/jobs", json={"type": "scan", "params": {}}, headers=AUTH)
    assert response.status_code == 422


def test_scan_job_incomplete_migration_params_is_422(client: TestClient) -> None:
    """Rejected by POST /jobs, not by a created job that immediately fails."""
    # B without A.
    response = client.post(
        "/jobs",
        headers=AUTH,
        json={
            "type": "scan",
            "params": {
                "modpack_path": str(MODPACK),
                "previous_overrides_path": str(MODPACK),
            },
        },
    )
    assert response.status_code == 422
    assert "previous_modpack_path" in response.json()["detail"]
    # A with no previous translation artifact.
    response = client.post(
        "/jobs",
        headers=AUTH,
        json={
            "type": "scan",
            "params": {
                "modpack_path": str(MODPACK),
                "previous_modpack_path": str(MODPACK),
            },
        },
    )
    assert response.status_code == 422
    assert "previous resource pack or overrides" in response.json()["detail"]


def test_export_requires_completed_translate_job(
    client: TestClient, scan_job: dict[str, Any]
) -> None:
    # Unknown translate job -> 404.
    response = client.post(
        "/jobs",
        json={"type": "export", "params": {"translate_job_id": "nope"}},
        headers=AUTH,
    )
    assert response.status_code == 404
    # A done *scan* job is the wrong kind of source -> 409.
    response = client.post(
        "/jobs",
        json={"type": "export", "params": {"translate_job_id": scan_job["id"]}},
        headers=AUTH,
    )
    assert response.status_code == 409


# -- jobs: upload flow ---------------------------------------------------------


def _install_translate_record(
    client: TestClient, tmp_path: Path, *, include_pack: bool = True
) -> JobRecord:
    """A completed translate job injected straight into the manager.

    Running a real translate needs an LLM, so build the JobRecord by hand
    (the shape _run leaves behind) and register it in manager._jobs.
    """
    output_dir = tmp_path / "out"
    output_files: list[Path] = []
    if include_pack:
        lang_file = (
            output_dir / "resourcepack" / "assets" / "somemod" / "lang" / "ko_kr.json"
        )
        lang_file.parent.mkdir(parents=True)
        lang_file.write_text('{"key.hello": "안녕"}', encoding="utf-8")
        mcmeta = output_dir / "resourcepack" / "pack.mcmeta"
        mcmeta.write_text('{"pack": {"pack_format": 15}}', encoding="utf-8")
        output_files += [lang_file, mcmeta]
    override_file = (
        output_dir / "overrides" / "kubejs" / "assets" / "test" / "lang" / "ko_kr.json"
    )
    override_file.parent.mkdir(parents=True)
    override_file.write_text('{"gui.done": "완료"}', encoding="utf-8")
    output_files.append(override_file)
    result = PipelineResult(
        config=PipelineConfig(
            modpack_path=tmp_path / "modpack",
            output_dir=output_dir,
            source_locale="en_us",
            target_locale="ko_kr",
            model="openai/gpt-4o-mini",
        ),
        entries=[
            EntryResult(
                key="key.hello",
                file="somemod/lang/en_us.json",
                source_text="Hello",
                translated_text="안녕",
                status=EntryStatus.PASSED,
            )
        ],
        output_files=output_files,
        stats=PipelineStats(
            total_entries=10,
            translated_entries=8,
            failed_entries=1,
            tm_hits=1,
            duration_seconds=12.5,
        ),
    )
    result.stats.finalize()  # coverage 90.0, quality 0.9
    record = JobRecord(
        id=f"translate-{uuid.uuid4()}",
        type=JobType.TRANSLATE,
        params={"modpack_path": str(tmp_path / "modpack")},
        status=JobStatus.DONE,
        result=result,
        finished=True,
    )
    client.app.state.job_manager.register_job(record)
    client.app.state.job_manager.session_store.save_job_session(record)
    return record


@pytest.fixture
def done_translate_job(client: TestClient, tmp_path: Path) -> JobRecord:
    return _install_translate_record(client, tmp_path)


@pytest.fixture
def upload_stubs(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub the three web calls in moru_engine.server.upload, recording args."""
    calls: dict[str, Any] = {}

    async def fake_slots(
        web_url: str, api_token: str | None, files: list[dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        calls["slots"] = {
            "web_url": web_url,
            "api_token": api_token,
            "files": files,
        }
        return {
            spec["kind"]: {
                "kind": spec["kind"],
                "url": f"https://r2.test/put/{spec['kind']}",
                "object_key": f"translations/test/{spec['kind']}.zip",
            }
            for spec in files
        }

    async def fake_put(url: str, zip_path: Path) -> None:
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
        calls.setdefault("puts", []).append({"url": url, "names": names})

    async def fake_register(
        web_url: str, api_token: str | None, payload: dict[str, Any]
    ) -> dict[str, Any]:
        calls["register"] = {
            "web_url": web_url,
            "api_token": api_token,
            "payload": payload,
        }
        return {"pack_id": "pk_123", "url": f"{web_url}/packs/pk_123"}

    monkeypatch.setattr("moru_engine.server.upload.request_upload_slots", fake_slots)
    monkeypatch.setattr("moru_engine.server.upload.put_archive", fake_put)
    monkeypatch.setattr("moru_engine.server.upload.register_pack", fake_register)
    return calls


def _start_upload(
    client: TestClient, params: dict[str, Any]
) -> "Any":  # httpx.Response
    return client.post("/jobs", json={"type": "upload", "params": params}, headers=AUTH)


def _terminal_frame(client: TestClient, job_id: str) -> dict[str, Any]:
    with client.websocket_connect(f"/jobs/{job_id}/events?token={TOKEN}") as ws:
        while True:
            frame = ws.receive_json()
            if frame["type"] in {"done", "failed", "cancelled"}:
                return frame


def test_upload_job_success_with_token(
    client: TestClient,
    done_translate_job: JobRecord,
    upload_stubs: dict[str, Any],
) -> None:
    response = _start_upload(
        client,
        {
            "translate_job_id": done_translate_job.id,
            "modpack_name": "ATM 10",
            "modpack_version": "1.2.3",
            "web_url": "https://web.test",
            "api_token": "desktop-api-token",
        },
    )
    assert response.status_code == 201
    job_id = response.json()["id"]
    final = _wait_for_job(client, job_id)
    assert final["status"] == "done", final["error"]

    frame = _terminal_frame(client, job_id)
    assert frame["pack_id"] == "pk_123"
    assert frame["url"] == "https://web.test/packs/pk_123"

    # The bearer token reaches both authenticated web calls.
    assert upload_stubs["slots"]["api_token"] == "desktop-api-token"
    assert upload_stubs["register"]["api_token"] == "desktop-api-token"
    assert upload_stubs["slots"]["web_url"] == "https://web.test"

    # One slot spec per non-empty output tree, each hashed independently.
    specs = {spec["kind"]: spec for spec in upload_stubs["slots"]["files"]}
    assert set(specs) == {"resource_pack", "overrides"}
    for spec in specs.values():
        assert spec["size"] > 0
        assert len(spec["sha256"]) == 64

    # Each tree ships as its own installable archive: pack.mcmeta sits at
    # the resource-pack archive ROOT (game-loadable as-is) and the
    # overrides zip mirrors the modpack root — the overrides/ folder never
    # leaks into the resource pack.
    puts = {p["url"]: set(p["names"]) for p in upload_stubs["puts"]}
    assert puts == {
        "https://r2.test/put/resource_pack": {
            "pack.mcmeta",
            "assets/somemod/lang/ko_kr.json",
        },
        "https://r2.test/put/overrides": {
            "kubejs/assets/test/lang/ko_kr.json",
        },
    }

    # TranslationPackCreate payload mapped from the pipeline result.
    payload = upload_stubs["register"]["payload"]
    assert payload["modpack_name"] == "ATM 10"
    assert payload["modpack_version"] == "1.2.3"
    assert payload["target_lang"] == "ko_kr"
    assert payload["source_lang"] == "en_us"
    assert payload["engine_version"] == __version__
    assert payload["files"] == [
        {
            "kind": "resource_pack",
            "object_key": "translations/test/resource_pack.zip",
        },
        {"kind": "overrides", "object_key": "translations/test/overrides.zip"},
    ]
    assert payload["stats"] == {
        "total_entries": 10,
        "translated_entries": 8,
        "failed_entries": 1,
        "coverage_percent": 90.0,
        "quality_score": 0.9,
        "tm_hits": 1,
        "migration_hits": 0,
        "model": "openai/gpt-4o-mini",
        "duration_seconds": 12.5,
    }


def test_upload_job_overrides_only_registers_single_file(
    client: TestClient,
    tmp_path: Path,
    upload_stubs: dict[str, Any],
) -> None:
    """A run with no resource-pack tree uploads only the overrides zip."""
    record = _install_translate_record(client, tmp_path, include_pack=False)
    response = _start_upload(
        client,
        {"translate_job_id": record.id, "modpack_name": "Overrides Only"},
    )
    assert response.status_code == 201
    final = _wait_for_job(client, response.json()["id"])
    assert final["status"] == "done", final["error"]

    assert [spec["kind"] for spec in upload_stubs["slots"]["files"]] == ["overrides"]
    puts = {p["url"]: set(p["names"]) for p in upload_stubs["puts"]}
    assert puts == {
        "https://r2.test/put/overrides": {"kubejs/assets/test/lang/ko_kr.json"}
    }
    assert upload_stubs["register"]["payload"]["files"] == [
        {"kind": "overrides", "object_key": "translations/test/overrides.zip"}
    ]


def test_export_job_builds_pack_and_overrides_zips(
    client: TestClient, done_translate_job: JobRecord, tmp_path: Path
) -> None:
    target = tmp_path / "exports" / "pack.zip"
    response = client.post(
        "/jobs",
        json={
            "type": "export",
            "params": {
                "translate_job_id": done_translate_job.id,
                "output_zip": str(target),
            },
        },
        headers=AUTH,
    )
    assert response.status_code == 201
    final = _wait_for_job(client, response.json()["id"])
    assert final["status"] == "done", final["error"]

    frame = _terminal_frame(client, final["id"])
    assert frame["zip_path"] == str(target)
    overrides_zip = target.with_name("pack_overrides.zip")
    assert frame["overrides_zip_path"] == str(overrides_zip)

    # Resource pack zip is installable as-is: mcmeta at the archive root.
    with zipfile.ZipFile(target) as zf:
        assert set(zf.namelist()) == {
            "pack.mcmeta",
            "assets/somemod/lang/ko_kr.json",
        }
    # Overrides zip mirrors the modpack root.
    with zipfile.ZipFile(overrides_zip) as zf:
        assert zf.namelist() == ["kubejs/assets/test/lang/ko_kr.json"]


def test_source_text_export_runs_without_a_translate_job(
    client: TestClient, tmp_path: Path
) -> None:
    """W3's export path: a scan is all the user has — no translate job, no
    API key, no provider call — and it must still produce both archives with
    a translated export's exact layout (kunho-park/moru-app#3)."""
    target = tmp_path / "exports" / "source.zip"
    response = client.post(
        "/jobs",
        json={
            "type": "export",
            "params": {
                "source_text": True,
                "modpack_path": str(MODPACK),
                "source_locale": "en_us",
                "target_locale": "ko_kr",
                "output_dir": str(tmp_path / "out"),
                "output_zip": str(target),
            },
        },
        headers=AUTH,
    )
    assert response.status_code == 201, response.text
    job = response.json()
    assert job["type"] == "export"
    final = _wait_for_job(client, job["id"])
    assert final["status"] == "done", final["error"]

    frame = _terminal_frame(client, job["id"])
    overrides_zip = target.with_name("source_overrides.zip")
    assert frame["zip_path"] == str(target)
    assert frame["overrides_zip_path"] == str(overrides_zip)

    # Archive-internal layout is the W6 one: members are relative to the
    # tree root, so the source_text working root never leaks into a zip.
    with zipfile.ZipFile(target) as zf:
        assert set(zf.namelist()) == {
            "pack.mcmeta",
            "pack.png",
            "assets/testmod/lang/ko_kr.json",
        }
        # Target-locale filename with untranslated values: a key-for-key
        # skeleton of a pack shared on moru.gg.
        lang = json.loads(zf.read("assets/testmod/lang/ko_kr.json"))
    assert lang["item.testmod.copper_wand"] == "Copper Wand"
    with zipfile.ZipFile(overrides_zip) as zf:
        assert "kubejs/assets/test/lang/ko_kr.json" in zf.namelist()

    # Only translate jobs persist, so a source-text export leaves no row in
    # the history the user browses.
    sessions = client.get("/sessions", headers=AUTH).json()
    assert all(session["id"] != job["id"] for session in sessions)


def test_source_text_export_validates_its_own_params(client: TestClient) -> None:
    """It needs no translate job, but it does need a real pack path."""
    for params in (
        {"source_text": True},
        {"source_text": True, "modpack_path": "/nope/does-not-exist"},
    ):
        response = client.post(
            "/jobs", json={"type": "export", "params": params}, headers=AUTH
        )
        assert response.status_code == 422, response.text


def test_cancelled_translate_result_can_be_reviewed_and_exported(
    client: TestClient, done_translate_job: JobRecord, tmp_path: Path
) -> None:
    done_translate_job.status = JobStatus.CANCELLED

    review = client.get(
        f"/translate/{done_translate_job.id}/entries",
        headers=AUTH,
    )
    assert review.status_code == 200
    assert review.json()["total"] == len(done_translate_job.result.entries)

    target = tmp_path / "exports" / "partial.zip"
    response = client.post(
        "/jobs",
        json={
            "type": "export",
            "params": {
                "translate_job_id": done_translate_job.id,
                "output_zip": str(target),
            },
        },
        headers=AUTH,
    )
    assert response.status_code == 201
    final = _wait_for_job(client, response.json()["id"])
    assert final["status"] == "done", final["error"]
    assert target.exists()
    assert target.with_name("partial_overrides.zip").exists()


def test_sessions_persistence_export_import(
    client: TestClient, done_translate_job: JobRecord, tmp_path: Path
) -> None:
    session_id = done_translate_job.id

    # 1. GET /sessions lists saved sessions
    list_res = client.get("/sessions", headers=AUTH)
    assert list_res.status_code == 200
    sessions_list = list_res.json()
    assert any(s["id"] == session_id for s in sessions_list)

    # 2. Simulate RAM clear / engine restart by dropping the in-memory record
    app = client.app
    manager = app.state.job_manager
    manager.forget_job(session_id)

    # 3. GET /translate/{job_id}/entries triggers auto-restoration from disk
    entries_res = client.get(f"/translate/{session_id}/entries", headers=AUTH)
    assert entries_res.status_code == 200
    assert entries_res.json()["total"] == len(done_translate_job.result.entries)

    # 4. PATCH entry persists modified status
    first_key = done_translate_job.result.entries[0].key
    patch_res = client.patch(
        f"/translate/{session_id}/entries/{first_key}",
        json={"translated_text": "수정된 테스트 번역문"},
        headers=AUTH,
    )
    assert patch_res.status_code == 200
    assert patch_res.json()["status"] == "modified"

    # Clear RAM again and verify patched value persisted
    manager.forget_job(session_id)
    recheck_res = client.get(
        f"/translate/{session_id}/entries?filter=modified", headers=AUTH
    )
    assert recheck_res.status_code == 200
    assert recheck_res.json()["total"] == 1
    assert recheck_res.json()["entries"][0]["translated_text"] == "수정된 테스트 번역문"

    # 5. Export session file (.moru)
    export_file = tmp_path / "exported_session.moru"
    export_res = client.post(
        f"/sessions/{session_id}/export",
        json={"output_path": str(export_file)},
        headers=AUTH,
    )
    assert export_res.status_code == 200
    assert export_file.exists()

    # 6. Delete session
    del_res = client.delete(f"/sessions/{session_id}", headers=AUTH)
    assert del_res.status_code == 200

    # Verify deleted
    assert not (manager.session_store.sessions_dir / f"{session_id}.moru").exists()
    listed_after = client.get("/sessions", headers=AUTH)
    assert all(s["id"] != session_id for s in listed_after.json())
    assert client.delete(f"/sessions/{session_id}", headers=AUTH).status_code == 404
    assert (
        client.post(
            f"/sessions/{uuid.uuid4()}/export",
            json={"output_path": str(tmp_path / "nope.moru")},
            headers=AUTH,
        ).status_code
        == 404
    )

    # 7. Import exported session file
    import_res = client.post(
        "/sessions/import",
        json={"input_path": str(export_file)},
        headers=AUTH,
    )
    assert import_res.status_code == 200, import_res.json()
    imported_job = import_res.json()["job"]
    assert imported_job["id"] == session_id
    # The history screen rebuilds its row from this summary.
    imported_summary = import_res.json()["session"]
    assert imported_summary["total_entries"] == len(done_translate_job.result.entries)
    assert imported_summary["modpack_name"]

    # Verify imported session entries
    imported_entries = client.get(f"/translate/{session_id}/entries", headers=AUTH)
    assert imported_entries.status_code == 200
    assert imported_entries.json()["total"] == len(done_translate_job.result.entries)


def test_scan_result_replays_the_payload_persisted_on_a_session(
    client: TestClient, tmp_path: Path
) -> None:
    """A reopened translate session can still draw the scan screen.

    The scan job keeps its own id and is never persisted, so the payload
    rides along on the translate record. A translate job that never carried
    one answers 404 - it must not try to read a PipelineResult as a scan.
    """
    manager = client.app.state.job_manager
    record = _install_translate_record(client, tmp_path / "withscan")
    payload = {
        "modpack_path": str(tmp_path / "modpack"),
        "categories": [],
        "identity": None,
    }
    record.params["scan_result"] = payload
    manager.session_store.save_job_session(record)
    manager.forget_job(record.id)

    replayed = client.get(f"/scan/{record.id}/result", headers=AUTH)
    assert replayed.status_code == 200
    assert replayed.json() == payload

    bare = _install_translate_record(client, tmp_path / "noscan", include_pack=False)
    assert client.get(f"/scan/{bare.id}/result", headers=AUTH).status_code == 404


def test_translate_job_adopts_the_scan_payload_of_its_scan_job(
    client: TestClient,
) -> None:
    """POST /jobs translate copies the scan payload onto the new session."""
    scan_job = client.post(
        "/jobs",
        json={"type": "scan", "params": {"modpack_path": str(MODPACK)}},
        headers=AUTH,
    ).json()
    _wait_for_job(client, scan_job["id"])
    expected = client.get(f"/scan/{scan_job['id']}/result", headers=AUTH).json()

    manager = client.app.state.job_manager
    record = JobRecord(
        id=str(uuid.uuid4()),
        type=JobType.TRANSLATE,
        params={"modpack_path": str(MODPACK), "scan_job_id": scan_job["id"]},
    )
    manager._attach_scan_payload(record)
    assert record.params["scan_result"] == expected


def test_translate_job_rejects_a_non_uuid_session_id(client: TestClient) -> None:
    """The id becomes a session filename, so only UUIDs are accepted."""
    response = client.post(
        "/jobs",
        json={
            "type": "translate",
            "params": {"modpack_path": str(MODPACK), "session_id": "not/a/uuid"},
        },
        headers=AUTH,
    )
    assert response.status_code == 422
    assert "session_id" in response.json()["detail"]


def test_entry_edits_address_the_named_file(client: TestClient, tmp_path: Path) -> None:
    """One key can live in two files; the edit lands on the named one."""
    manager = client.app.state.job_manager
    result = PipelineResult(
        config=PipelineConfig(modpack_path=tmp_path / "modpack"),
        entries=[
            EntryResult(
                key="gui.done",
                file="a/en_us.json",
                source_text="Done",
                translated_text="완료",
                status=EntryStatus.PASSED,
            ),
            EntryResult(
                key="gui.done",
                file="b/en_us.json",
                source_text="Done",
                translated_text="완료",
                status=EntryStatus.PASSED,
            ),
        ],
    )
    record = JobRecord(
        id=str(uuid.uuid4()),
        type=JobType.TRANSLATE,
        params={"modpack_path": str(tmp_path / "modpack")},
        status=JobStatus.DONE,
        result=result,
        finished=True,
    )
    manager.register_job(record)

    patched = client.patch(
        f"/translate/{record.id}/entries/gui.done",
        json={"translated_text": "끝", "file": "b/en_us.json"},
        headers=AUTH,
    )
    assert patched.status_code == 200
    assert patched.json()["file"] == "b/en_us.json"
    assert result.entries[0].translated_text == "완료"
    assert result.entries[1].translated_text == "끝"

    missing = client.patch(
        f"/translate/{record.id}/entries/gui.done",
        json={"translated_text": "x", "file": "c/en_us.json"},
        headers=AUTH,
    )
    assert missing.status_code == 404


def _write_session_file(path: Path, **overrides: Any) -> Path:
    payload: dict[str, Any] = {
        "version": "1.0",
        "id": str(uuid.uuid4()),
        "modpack_name": "Pack",
        "modpack_path": str(path.parent / "modpack"),
        "status": "done",
        "stats": {},
        "config": {},
        "entries": [
            {
                "key": "gui.done",
                "file": "a/en_us.json",
                "source_text": "Done",
                "translated_text": "완료",
                "status": "passed",
                "errors": [],
            }
        ],
    }
    payload.update(overrides)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_imported_session_stays_inside_the_session_directory(
    client: TestClient, tmp_path: Path
) -> None:
    """The id inside an imported file becomes a filename; keep it contained."""
    source = _write_session_file(tmp_path / "relocate.moru", id="../../relocated")
    response = client.post(
        "/sessions/import", json={"input_path": str(source)}, headers=AUTH
    )
    assert response.status_code == 200, response.json()

    sessions_dir = client.app.state.job_manager.session_store.sessions_dir
    assert not (sessions_dir.parent.parent / "relocated.moru").exists()
    assert all(p.parent == sessions_dir for p in sessions_dir.glob("*.moru"))


def test_import_rejects_a_session_that_never_finished(
    client: TestClient, tmp_path: Path
) -> None:
    """A restored record claims finished=True, so only terminal states load."""
    source = _write_session_file(tmp_path / "running.moru", status="running")
    response = client.post(
        "/sessions/import", json={"input_path": str(source)}, headers=AUTH
    )
    assert response.status_code == 422


def test_upload_job_without_token_uses_defaults(
    client: TestClient,
    done_translate_job: JobRecord,
    upload_stubs: dict[str, Any],
) -> None:
    response = _start_upload(
        client,
        {"translate_job_id": done_translate_job.id, "modpack_name": "ATM 10"},
    )
    assert response.status_code == 201
    final = _wait_for_job(client, response.json()["id"])
    assert final["status"] == "done", final["error"]

    frame = _terminal_frame(client, final["id"])
    assert frame["url"] == "https://moru.gg/packs/pk_123"
    # No token -> no Authorization header is attached, the default web_url
    # is used, and the web platform admits the call via the desktop client
    # marker (anonymous upload, attributed to no account).
    assert upload_stubs["slots"]["api_token"] is None
    assert upload_stubs["register"]["api_token"] is None
    assert upload_stubs["slots"]["web_url"] == "https://moru.gg"


def test_upload_headers_carry_desktop_client_marker() -> None:
    """The X-Moru-Client marker is what lets anonymous desktop uploads
    through the web platform (web-api.yaml contract v3)."""
    anonymous = _auth_headers(None)
    assert anonymous["X-Moru-Client"] == f"moru-engine/{__version__}"
    assert "Authorization" not in anonymous

    authed = _auth_headers("desktop-api-token")
    assert authed["Authorization"] == "Bearer desktop-api-token"
    assert authed["X-Moru-Client"] == f"moru-engine/{__version__}"


def test_upload_job_missing_params_is_422(client: TestClient) -> None:
    response = _start_upload(client, {"modpack_name": "X"})
    assert response.status_code == 422
    assert "translate_job_id" in response.json()["detail"]

    response = _start_upload(client, {"translate_job_id": "whatever"})
    assert response.status_code == 422
    assert "modpack_name" in response.json()["detail"]


def test_upload_job_unknown_translate_job_is_404(client: TestClient) -> None:
    response = _start_upload(client, {"translate_job_id": "nope", "modpack_name": "X"})
    assert response.status_code == 404


def test_upload_requires_completed_translate_job(
    client: TestClient, scan_job: dict[str, Any]
) -> None:
    # A scan job is not a valid translate source -> 409.
    response = _start_upload(
        client, {"translate_job_id": scan_job["id"], "modpack_name": "X"}
    )
    assert response.status_code == 409

    # A translate job that has not finished yet -> 409.
    record = JobRecord(
        id=f"translate-{uuid.uuid4()}",
        type=JobType.TRANSLATE,
        params={},
        status=JobStatus.RUNNING,
    )
    client.app.state.job_manager._jobs[record.id] = record
    response = _start_upload(
        client, {"translate_job_id": record.id, "modpack_name": "X"}
    )
    assert response.status_code == 409
    assert "upload requires a completed translate job" in response.json()["detail"]


def test_upload_job_web_failure_marks_job_failed(
    client: TestClient,
    done_translate_job: JobRecord,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def failing_slots(
        web_url: str, api_token: str | None, files: list[dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        raise WebUploadError("upload slot request failed: HTTP 503 - storage down")

    monkeypatch.setattr("moru_engine.server.upload.request_upload_slots", failing_slots)
    response = _start_upload(
        client,
        {"translate_job_id": done_translate_job.id, "modpack_name": "ATM 10"},
    )
    assert response.status_code == 201
    final = _wait_for_job(client, response.json()["id"])
    assert final["status"] == "failed"
    assert "HTTP 503" in final["error"]
    assert "storage down" in final["error"]


# -- websocket events ------------------------------------------------------------


def test_ws_replays_history_for_finished_job(
    client: TestClient, scan_job: dict[str, Any]
) -> None:
    frames: list[dict[str, Any]] = []
    with client.websocket_connect(f"/jobs/{scan_job['id']}/events?token={TOKEN}") as ws:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            while True:
                frames.append(ws.receive_json())
        assert exc_info.value.code == 1000
    assert frames, "expected replayed events for a finished job"
    assert all("type" in frame for frame in frames)
    terminal = frames[-1]
    assert terminal["type"] == "done"
    assert terminal["status"] == "done"
    # The terminal frame is the only terminal-typed frame in the stream.
    assert [f for f in frames if f["type"] in {"done", "failed", "cancelled"}] == [
        terminal
    ]


def test_job_snapshot_compacts_history_and_cursor_replays_only_new_events(
    client: TestClient,
) -> None:
    manager = client.app.state.job_manager
    record = JobRecord(
        id=f"snapshot-{uuid.uuid4()}",
        type=JobType.TRANSLATE,
        params={},
        status=JobStatus.RUNNING,
    )
    manager._jobs[record.id] = record
    manager._deliver(
        record,
        {
            "type": "progress",
            "stage": "translate",
            "file": "a.json",
            "done": 1,
            "total": 5,
        },
    )
    manager._deliver(
        record,
        {
            "type": "progress",
            "stage": "translate",
            "file": "a.json",
            "done": 3,
            "total": 5,
        },
    )
    manager._deliver(
        record,
        {
            "type": "batch_started",
            "request_id": 7,
            "file": "a.json",
            "key": "k",
            "entries": 2,
        },
    )
    manager._deliver(
        record,
        {"type": "entry_failed", "key": "bad", "errors": ["broken"]},
    )

    response = client.get(f"/jobs/{record.id}/snapshot", headers=AUTH)
    assert response.status_code == 200
    snapshot = response.json()
    assert snapshot["job"]["status"] == "running"
    assert snapshot["cursor"] == 4
    assert snapshot["failed_count"] == 1
    progress = [event for event in snapshot["events"] if event["type"] == "progress"]
    assert len(progress) == 1
    assert progress[0]["done"] == 3
    assert any(event["type"] == "batch_started" for event in snapshot["events"])

    manager._deliver(
        record,
        {
            "type": "progress",
            "stage": "translate",
            "file": "a.json",
            "done": 5,
            "total": 5,
        },
    )
    manager._deliver(record, {"type": "done", "status": "done"})
    record.status = JobStatus.DONE
    record.finished = True
    with client.websocket_connect(
        f"/jobs/{record.id}/events?token={TOKEN}&after={snapshot['cursor']}"
    ) as ws:
        assert ws.receive_json()["done"] == 5
        terminal = ws.receive_json()
        assert terminal["type"] == "done"
        assert terminal["seq"] == 6


def test_job_snapshot_unknown_job_is_404(client: TestClient) -> None:
    assert client.get("/jobs/nope/snapshot", headers=AUTH).status_code == 404


def test_ws_rejects_bad_token(client: TestClient, scan_job: dict[str, Any]) -> None:
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/jobs/{scan_job['id']}/events?token=wrong"):
            pass


def test_ws_unknown_job_closes(client: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/jobs/nope/events?token={TOKEN}"):
            pass


# -- translate review endpoints ----------------------------------------------------


def test_patch_entry_unknown_job_is_404(client: TestClient) -> None:
    response = client.patch(
        "/translate/nope/entries/some.key",
        json={"translated_text": "x"},
        headers=AUTH,
    )
    assert response.status_code == 404


def test_patch_entry_rejects_running_job(
    client: TestClient, done_translate_job: JobRecord
) -> None:
    done_translate_job.finished = False
    response = client.patch(
        f"/translate/{done_translate_job.id}/entries/gui.ok",
        json={"translated_text": "확인"},
        headers=AUTH,
    )
    assert response.status_code == 409


def test_patch_migrated_entry_refreshes_stats(
    client: TestClient, done_translate_job: JobRecord
) -> None:
    result = done_translate_job.result
    assert isinstance(result, PipelineResult)
    result.entries = [
        EntryResult(
            key="migrated.entry",
            file="lang/en_us.json",
            source_text="Same source",
            translated_text="이전 수동 번역",
            status=EntryStatus.MIGRATED,
        )
    ]
    result.stats = PipelineStats()
    TranslationPipeline._refresh_stats(result)
    assert result.stats.migration_hits == 1
    assert result.stats.coverage_percent == 100

    response = client.patch(
        f"/translate/{done_translate_job.id}/entries/migrated.entry",
        json={"translated_text": "검수 후 번역"},
        headers=AUTH,
    )
    assert response.status_code == 200, response.text
    assert result.stats.migration_hits == 0
    assert result.stats.translated_entries == 1
    assert result.stats.coverage_percent == 100
    stats_response = client.get(
        f"/translate/{done_translate_job.id}/stats", headers=AUTH
    )
    assert stats_response.status_code == 200
    assert stats_response.json()["migration_hits"] == 0
    assert done_translate_job.done_payload == {"stats": result.stats.model_dump()}


def test_translate_stats_unknown_job_is_404(client: TestClient) -> None:
    assert client.get("/translate/nope/stats", headers=AUTH).status_code == 404


def test_entries_unknown_job_is_404(client: TestClient) -> None:
    response = client.get("/translate/nope/entries", headers=AUTH)
    assert response.status_code == 404


def _populate_entries(record: JobRecord, count: int) -> None:
    """Fill a translate result with `count` synthetic review entries."""
    record.result.entries = [
        EntryResult(
            key=f"item.mod.entry_{i}",
            file="lang/en_us.json",
            source_text=f"Source text {i}",
            translated_text=f"번역 {i}",
            status=EntryStatus.PASSED,
        )
        for i in range(count)
    ]


def test_entry_search_spans_every_page(
    client: TestClient, done_translate_job: JobRecord
) -> None:
    """A match on page 3 must be findable from page 1.

    The review screen used to filter only the rows it had already fetched,
    so anything past the first page was invisible to search.
    """
    _populate_entries(done_translate_job, 250)
    # entry_240 sits on page 3 of the unfiltered list (page size 100).
    response = client.get(
        f"/translate/{done_translate_job.id}/entries",
        params={"search": "entry_240", "page": 1},
        headers=AUTH,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert [e["key"] for e in body["entries"]] == ["item.mod.entry_240"]


def test_entry_search_is_case_insensitive_across_all_three_fields(
    client: TestClient, done_translate_job: JobRecord
) -> None:
    _populate_entries(done_translate_job, 5)
    done_translate_job.result.entries[2].source_text = "Netherite Ingot"
    done_translate_job.result.entries[3].translated_text = "네더라이트 주괴"

    def search(term: str) -> list[str]:
        response = client.get(
            f"/translate/{done_translate_job.id}/entries",
            params={"search": term},
            headers=AUTH,
        )
        assert response.status_code == 200
        return [e["key"] for e in response.json()["entries"]]

    assert search("NETHERITE") == ["item.mod.entry_2"]  # source, case-folded
    assert search("주괴") == ["item.mod.entry_3"]  # translation
    assert search("entry_1") == ["item.mod.entry_1"]  # key


def test_entry_search_paginates_the_narrowed_set(
    client: TestClient, done_translate_job: JobRecord
) -> None:
    _populate_entries(done_translate_job, 250)
    # "entry_1" matches entry_1, entry_1x, entry_1xx -> 111 rows.
    first = client.get(
        f"/translate/{done_translate_job.id}/entries",
        params={"search": "entry_1", "page": 1},
        headers=AUTH,
    ).json()
    assert first["total"] == 111
    assert len(first["entries"]) == 100
    second = client.get(
        f"/translate/{done_translate_job.id}/entries",
        params={"search": "entry_1", "page": 2},
        headers=AUTH,
    ).json()
    assert second["total"] == 111
    assert len(second["entries"]) == 11


def test_entry_search_composes_with_the_status_filter(
    client: TestClient, done_translate_job: JobRecord
) -> None:
    _populate_entries(done_translate_job, 20)
    done_translate_job.result.entries[7].status = EntryStatus.FAILED
    response = client.get(
        f"/translate/{done_translate_job.id}/entries",
        params={"filter": "failed", "search": "entry_7"},
        headers=AUTH,
    )
    assert [e["key"] for e in response.json()["entries"]] == ["item.mod.entry_7"]

    miss = client.get(
        f"/translate/{done_translate_job.id}/entries",
        params={"filter": "failed", "search": "entry_8"},
        headers=AUTH,
    )
    assert miss.json()["total"] == 0


def test_blank_search_returns_the_whole_page(
    client: TestClient, done_translate_job: JobRecord
) -> None:
    _populate_entries(done_translate_job, 30)
    body = client.get(
        f"/translate/{done_translate_job.id}/entries",
        params={"search": "   "},
        headers=AUTH,
    ).json()
    assert body["total"] == 30


def test_entries_on_scan_job_is_404(
    client: TestClient, scan_job: dict[str, Any]
) -> None:
    response = client.get(f"/translate/{scan_job['id']}/entries", headers=AUTH)
    assert response.status_code == 404


def test_retranslate_unknown_job_is_404(client: TestClient) -> None:
    response = client.post("/translate/nope/entries/some.key/retranslate", headers=AUTH)
    assert response.status_code == 404


def test_retranslate_on_scan_job_is_404(
    client: TestClient, scan_job: dict[str, Any]
) -> None:
    response = client.post(
        f"/translate/{scan_job['id']}/entries/some.key/retranslate",
        headers=AUTH,
    )
    assert response.status_code == 404


# -- glossary / tm / config ----------------------------------------------------------


def test_glossary_empty_then_roundtrip(client: TestClient) -> None:
    params = {"source_lang": "en_us", "target_lang": "ko_kr"}
    response = client.get("/glossary", params=params, headers=AUTH)
    assert response.status_code == 200
    assert response.json() == {
        "source_lang": "en_us",
        "target_lang": "ko_kr",
        "terms": [],
    }

    doc = {
        "source_lang": "en_us",
        "target_lang": "ko_kr",
        "terms": [
            {"source": "Creeper", "target": "크리퍼"},
            # Key-scoped row: the boss sense of an ambiguous term.
            {
                "source": "Wither",
                "target": "시듦",
                "key_scope": ["effect.*"],
            },
        ],
    }
    response = client.put("/glossary", json=doc, headers=AUTH)
    assert response.status_code == 200

    response = client.get("/glossary", params=params, headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    # key_scope survives the round trip; absent means "every key".
    assert body["terms"] == [
        {
            "source": "Creeper",
            "target": "크리퍼",
            "origin": "manual",
            "key_scope": [],
        },
        {
            "source": "Wither",
            "target": "시듦",
            "origin": "manual",
            "key_scope": ["effect.*"],
        },
    ]


def test_glossary_rejects_bad_locale(client: TestClient) -> None:
    response = client.get(
        "/glossary",
        params={"source_lang": "../evil", "target_lang": "ko_kr"},
        headers=AUTH,
    )
    assert response.status_code == 422


def test_glossary_put_refuses_a_scope_that_could_never_fire(
    client: TestClient,
) -> None:
    """A scope is only ever a narrowing, so an unparseable pattern narrows the
    rule to nothing: stored, listed and editable while applying to no key.
    The real source is a separator mismatch across the CSV boundary — the
    desktop joins patterns one way and moru-web splits them another, so
    ``effect.*;status_effect.*`` arrives as a single pattern. Refuse it here,
    naming the pattern, rather than accepting a rule that silently does
    nothing.
    """
    params = {"source_lang": "en_us", "target_lang": "ko_kr"}
    response = client.put(
        "/glossary",
        json={
            **params,
            "terms": [
                {
                    "source": "Wither",
                    "target": "시듦",
                    "origin": "manual",
                    "key_scope": ["effect.*;status_effect.*"],
                }
            ],
        },
        headers=AUTH,
    )
    assert response.status_code == 422
    assert "effect.*;status_effect.*" in response.json()["detail"]


def test_glossary_put_accepts_a_well_formed_scope(client: TestClient) -> None:
    """The companion to the rejection: the shape the desktop actually sends
    for a scoped rule must still round-trip untouched.
    """
    params = {"source_lang": "en_us", "target_lang": "ko_kr"}
    response = client.put(
        "/glossary",
        json={
            **params,
            "terms": [
                {
                    "source": "Wither",
                    "target": "시듦",
                    "origin": "manual",
                    "key_scope": ["effect.*", "subtitles.*.wither"],
                }
            ],
        },
        headers=AUTH,
    )
    assert response.status_code == 200
    assert response.json()["terms"][0]["key_scope"] == [
        "effect.*",
        "subtitles.*.wither",
    ]


def test_glossary_recovers_a_legacy_or_damaged_store(
    tmp_path_factory: pytest.TempPathFactory, shutdown_flag: threading.Event
) -> None:
    """The stored document is whatever a past build wrote.

    A bare term list predates the doc wrapper, and a crash mid-write can
    leave `terms` holding something that is not a list at all. Returning the
    file verbatim served the first case as empty and pushed the second into
    the renderer, where mapping over it throws and blanks the glossary screen
    rather than showing the empty state.
    """
    root = tmp_path_factory.mktemp("glossary-legacy")
    store = root / "config" / "glossaries"
    store.mkdir(parents=True)
    term = {"source": "Diamond", "target": "다이아몬드", "origin": "vanilla"}
    params = {"source_lang": "en_us", "target_lang": "ko_kr"}

    def read(document: object) -> dict[str, Any]:
        (store / "en_us_ko_kr.json").write_text(
            json.dumps(document, ensure_ascii=False), encoding="utf-8"
        )
        app = create_app(
            token=TOKEN,
            config_dir=root / "config",
            tm_db_path=root / "tm.sqlite3",
            shutdown_handler=shutdown_flag.set,
            shutdown_delay=0.0,
        )
        with TestClient(app) as legacy_client:
            response = legacy_client.get("/glossary", params=params, headers=AUTH)
        assert response.status_code == 200
        return response.json()

    # A pre-wrapper store is still readable, and the missing key_scope
    # deserializes to "every key".
    body = read([term])
    assert body["terms"] == [{**term, "key_scope": []}]

    # A non-list `terms` reads as empty instead of reaching the renderer.
    assert read({**params, "terms": {"bogus": 1}})["terms"] == []

    # One unusable row must not cost the rows around it.
    body = read(
        {**params, "terms": [term, {"nope": True}, {"source": "Gold", "target": "금"}]}
    )
    assert [entry["source"] for entry in body["terms"]] == ["Diamond", "Gold"]


def test_tm_stats_empty_db(
    tmp_path_factory: pytest.TempPathFactory, shutdown_flag: threading.Event
) -> None:
    """A fresh TM reports zeros.

    Deliberately NOT the module-scoped `client`: that shares one TM database
    across the whole file, and a manual entry commit now writes a
    `manual`-origin row so a translator's decisions reach later runs. The
    shared database is therefore no longer empty by the time this test runs,
    which is correct behaviour — so this test brings its own.
    """
    root = tmp_path_factory.mktemp("tm-empty")
    app = create_app(
        token=TOKEN,
        config_dir=root / "config",
        tm_db_path=root / "tm.sqlite3",
        shutdown_handler=shutdown_flag.set,
        shutdown_delay=0.0,
    )
    with TestClient(app) as fresh:
        response = fresh.get("/tm/stats", headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert body["entries"] == 0
    assert body["hits"] == 0
    assert body["last_sync_version"] is None


def test_config_roundtrip(client: TestClient) -> None:
    response = client.get("/config", headers=AUTH)
    assert response.status_code == 200
    assert response.json() == {}

    payload = {"theme": "dark", "nested": {"batch_size": 30, "flag": True}}
    response = client.put("/config", json=payload, headers=AUTH)
    assert response.status_code == 200

    response = client.get("/config", headers=AUTH)
    assert response.status_code == 200
    assert response.json() == payload


# -- providers ----------------------------------------------------------------------


def test_providers_include_openrouter(client: TestClient) -> None:
    body = client.get("/providers", headers=AUTH).json()
    openrouter = next(p for p in body if p["id"] == "openrouter")
    assert openrouter["name"] == "OpenRouter"
    assert openrouter["models"]
    assert all(m.startswith("openrouter/") for m in openrouter["models"])


@pytest.mark.parametrize(
    ("provider_id", "prefix"),
    [
        ("codex", "codex/"),
        ("gemini-cli", "gemini-cli/"),
    ],
)
def test_providers_expose_cli_subscriptions(
    client: TestClient, provider_id: str, prefix: str
) -> None:
    body = client.get("/providers", headers=AUTH).json()
    entry = next(p for p in body if p["id"] == provider_id)
    assert entry["auth"] == "cli"
    # The store is the only thing that knows which command owns the grant on
    # this machine — Gemini CLI's is `agy` once Antigravity is installed —
    # so the route must echo it rather than carry its own copy.
    assert entry["login_hint"] == STORES[provider_id].login_hint
    assert entry["models"]
    assert all(m.startswith(prefix) for m in entry["models"])
    # has_key tracks the CLI login, not an env var, and the two agree.
    assert entry["has_key"] == entry["connected"]


def test_claude_code_is_listed_disabled_and_never_probed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import moru_engine.server.app as app_module

    def boom(provider: str) -> dict:
        raise AssertionError(f"probed {provider}")

    monkeypatch.setattr(app_module, "provider_status", lambda p: (
        boom(p) if p == "claude-code" else {"connected": False}
    ))
    body = client.get("/providers", headers=AUTH).json()
    entry = next(p for p in body if p["id"] == "claude-code")
    assert entry["auth"] == "cli"
    assert entry["disabled"] is True
    assert entry["connected"] is False


def test_claude_code_test_probe_refuses_without_a_request(client: TestClient) -> None:
    body = client.post(
        "/providers/test", headers=AUTH, json={"provider": "claude-code"}
    ).json()
    assert body["ok"] is False
    assert "사용할 수 없습니다" in body["error"]


@pytest.mark.parametrize("provider_id", ["claude-code", "gemini-cli"])
def test_cli_providers_without_discovery_use_the_static_catalog(
    client: TestClient,
    provider_id: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """These subscription surfaces publish no model-list endpoint."""
    # Empty config dirs: a logged-out store, so the listing never reaches
    # the network and the lineup still comes back.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("GEMINI_CONFIG_DIR", str(tmp_path))
    for store in STORES.values():
        store.invalidate()
    response = client.post(
        "/providers/models", json={"provider": provider_id}, headers=AUTH
    )
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "static"
    assert body["error"] is None
    assert body["models"]


def test_codex_models_fall_back_to_static_when_the_cli_is_logged_out(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex DOES publish /codex/models, and its plan gates the SKUs.

    Without a CLI login the live call cannot run, so the route degrades to
    the static catalog and surfaces why — it must never invent a lineup.
    """

    async def logged_out(
        provider: str,
        *,
        api_key: str | None = None,
        api_base: str | None = None,
    ) -> list[str]:
        assert provider == "codex"
        raise RuntimeError("codex login required")

    monkeypatch.setattr("moru_engine.server.app.fetch_live_models", logged_out)
    response = client.post(
        "/providers/models", json={"provider": "codex"}, headers=AUTH
    )
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "static"
    assert "codex login" in (body["error"] or "")
    assert "codex/gpt-5.6-terra" in body["models"]


def test_provider_models_requires_token(client: TestClient) -> None:
    response = client.post("/providers/models", json={"provider": "openai"})
    assert response.status_code == 401


def test_provider_models_unknown_provider_is_404(client: TestClient) -> None:
    response = client.post("/providers/models", headers=AUTH, json={"provider": "nope"})
    assert response.status_code == 404


def test_provider_models_live_success(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_fetch(
        provider: str, *, api_key: str | None = None, api_base: str | None = None
    ) -> list[str]:
        assert (provider, api_key) == ("openai", "sk-test")
        return ["openai/gpt-4o-mini", "openai/gpt-4.1"]

    monkeypatch.setattr("moru_engine.server.app.fetch_live_models", fake_fetch)
    response = client.post(
        "/providers/models",
        headers=AUTH,
        json={"provider": "openai", "api_key": "sk-test"},
    )
    assert response.status_code == 200
    assert response.json() == {
        "provider": "openai",
        "models": ["openai/gpt-4o-mini", "openai/gpt-4.1"],
        "source": "live",
        "error": None,
    }


def test_provider_models_falls_back_to_static_on_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_fetch(
        provider: str, *, api_key: str | None = None, api_base: str | None = None
    ) -> list[str]:
        raise ValueError("api key required")

    monkeypatch.setattr("moru_engine.server.app.fetch_live_models", fake_fetch)
    response = client.post(
        "/providers/models", headers=AUTH, json={"provider": "anthropic"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "static"
    assert body["error"] == "api key required"
    catalog = client.get("/providers", headers=AUTH).json()
    anthropic = next(p for p in catalog if p["id"] == "anthropic")
    assert body["models"] == anthropic["models"]


def test_provider_models_falls_back_to_static_on_empty(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_fetch(
        provider: str, *, api_key: str | None = None, api_base: str | None = None
    ) -> list[str]:
        return []

    monkeypatch.setattr("moru_engine.server.app.fetch_live_models", fake_fetch)
    response = client.post(
        "/providers/models", headers=AUTH, json={"provider": "ollama"}
    )
    body = response.json()
    assert body["source"] == "static"
    assert body["error"] == "provider returned no models"
    assert all(m.startswith("ollama_chat/") for m in body["models"])


def test_fetch_live_models_rejects_missing_key_and_unknown_provider() -> None:
    for provider in ("openai", "anthropic", "gemini", "deepseek", "xai"):
        with pytest.raises(ValueError, match="api key required"):
            asyncio.run(fetch_live_models(provider))
    with pytest.raises(ValueError, match="api base required"):
        asyncio.run(fetch_live_models("openai-compatible"))
    with pytest.raises(ValueError, match="unknown provider"):
        asyncio.run(fetch_live_models("nope"))


def test_fetch_openai_compatible_lists_hosted_vllm_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Base URL is joined with /models and ids map to hosted_vllm/ —
    LiteLLM's generic OpenAI-compatible route."""
    seen: dict[str, Any] = {}

    async def fake_get_json(
        url: str, headers: dict[str, str] | None = None
    ) -> dict[str, Any]:
        seen["url"] = url
        seen["headers"] = headers
        return {"data": [{"id": "qwen2.5-7b-instruct"}, {"id": "llama-3.2-3b"}]}

    monkeypatch.setattr("moru_engine.server.live_models._get_json", fake_get_json)
    models = asyncio.run(
        fetch_live_models("openai-compatible", api_base="http://localhost:1234/v1/")
    )
    assert models == [
        "hosted_vllm/qwen2.5-7b-instruct",
        "hosted_vllm/llama-3.2-3b",
    ]
    assert seen["url"] == "http://localhost:1234/v1/models"
    assert seen["headers"] is None  # keyless local server -> no auth header


def test_providers_test_passes_api_base_to_lm(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    class FakeLM:
        def __call__(self, prompt: str) -> str:
            return "pong"

    def fake_build_lm(model: str, **kwargs: Any) -> FakeLM:
        seen["model"] = model
        seen["kwargs"] = kwargs
        return FakeLM()

    monkeypatch.setattr("moru_engine.server.app.build_lm", fake_build_lm)
    response = client.post(
        "/providers/test",
        headers=AUTH,
        json={
            "provider": "openai-compatible",
            "model": "hosted_vllm/qwen2.5-7b-instruct",
            "api_base": "http://localhost:1234/v1",
        },
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True, "error": None}
    assert seen["model"] == "hosted_vllm/qwen2.5-7b-instruct"
    assert seen["kwargs"]["api_base"] == "http://localhost:1234/v1"


def test_providers_test_without_model_needs_static_catalog(
    client: TestClient,
) -> None:
    """openai-compatible has no static models, so a model is mandatory."""
    response = client.post(
        "/providers/test", headers=AUTH, json={"provider": "openai-compatible"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert "pass a model" in body["error"]


# -- shutdown -----------------------------------------------------------------------


def test_shutdown_schedules_handler(
    client: TestClient, shutdown_flag: threading.Event
) -> None:
    response = client.post("/shutdown", headers=AUTH)
    assert response.status_code == 202
    assert shutdown_flag.wait(timeout=2.0), "shutdown handler was not invoked"


# -- translate graph endpoint ---------------------------------------------------


def test_translate_graph_finished_job_rebuilds_and_short_circuits(
    client: TestClient, done_translate_job: JobRecord
) -> None:
    res = client.get(f"/translate/{done_translate_job.id}/graph", headers=AUTH)
    assert res.status_code == 200
    body = res.json()
    assert body["job_finished"] is True
    assert body["version"] == 1
    assert body["stats"]["entries"] == 1
    assert isinstance(body["nodes"], list) and isinstance(body["edges"], list)
    # rebuilt graph is cached on the record
    assert done_translate_job.graph_cache is not None

    unchanged = client.get(
        f"/translate/{done_translate_job.id}/graph",
        params={"known_version": body["version"]},
        headers=AUTH,
    )
    assert unchanged.status_code == 200
    assert unchanged.json() == {
        "version": body["version"],
        "unchanged": True,
        "job_finished": True,
    }


def test_translate_graph_live_pipeline_and_version_polling(
    client: TestClient,
) -> None:
    graph = TranslationGraph.build(
        [
            (
                "lang/en_us.json",
                {"item.m.core": "Ember Core"},
                {},
            ),
            (
                "q.snbt",
                {"quests[0].description[0]": "Craft the Ember Core."},
                {},
            ),
        ]
    )

    class _StubPipeline:
        """Only the .graph attribute is read by the endpoint."""

        def __init__(self, g: TranslationGraph) -> None:
            self.graph = g

    record = JobRecord(
        id=f"translate-{uuid.uuid4()}",
        type=JobType.TRANSLATE,
        params={},
        status=JobStatus.RUNNING,
        pipeline=_StubPipeline(graph),  # type: ignore[arg-type]
    )
    client.app.state.job_manager.register_job(record)

    first = client.get(f"/translate/{record.id}/graph", headers=AUTH).json()
    assert first["job_finished"] is False
    assert first["version"] == 1
    term = next(n for n in first["nodes"] if n["kind"] == "term")
    assert term["settled"] is False

    # unchanged poll costs nothing…
    poll = client.get(
        f"/translate/{record.id}/graph",
        params={"known_version": 1},
        headers=AUTH,
    ).json()
    assert poll == {"version": 1, "unchanged": True, "job_finished": False}

    # …until a settlement bumps the version
    graph.record_translation("lang/en_us.json", "item.m.core", "잉걸불 핵")
    changed = client.get(
        f"/translate/{record.id}/graph",
        params={"known_version": 1},
        headers=AUTH,
    ).json()
    assert changed["version"] == 2
    term = next(n for n in changed["nodes"] if n["kind"] == "term")
    assert term["settled"] is True and term["target"] == "잉걸불 핵"


def test_translate_graph_running_without_graph_is_409(
    client: TestClient,
) -> None:
    record = JobRecord(
        id=f"translate-{uuid.uuid4()}",
        type=JobType.TRANSLATE,
        params={},
        status=JobStatus.RUNNING,
    )
    client.app.state.job_manager.register_job(record)
    res = client.get(f"/translate/{record.id}/graph", headers=AUTH)
    assert res.status_code == 409


def test_translate_graph_unknown_or_wrong_type_is_404(
    client: TestClient, scan_job: dict[str, Any]
) -> None:
    assert client.get("/translate/nope/graph", headers=AUTH).status_code == 404
    assert (
        client.get(f"/translate/{scan_job['id']}/graph", headers=AUTH).status_code
        == 404
    )


# -- community translation lookup ---------------------------------------------


def test_community_translation_measures_coverage_against_the_real_scan(
    client: TestClient, scan_job: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The point of asking after the scan: the answer can be quantified.

    A bare "someone translated this" is not actionable. The endpoint feeds
    the scan's own per-category entry counts to the matcher so the offer can
    say how much of THIS pack the published one leaves untranslated.
    """
    captured: dict[str, Any] = {}

    async def fake_find(
        web_url: str,
        identity: Any,
        target_lang: str,
        local_categories: Any = None,
    ) -> TranslationMatch:
        captured["web_url"] = web_url
        captured["identity"] = identity
        captured["target_lang"] = target_lang
        captured["local_categories"] = local_categories
        return TranslationMatch(
            pack_id="pack-1",
            modpack_version="4.1.2",
            exact=True,
            compatible_versions=None,
            total_entries=8_036,
            uncovered_entries=40,
            uncovered_by_category={"": 40},
            url="https://moru.gg/ko/pack/pack-1",
            download_url="https://moru.gg/api/packs/pack-1/download",
            note="4.1.2 버전용으로 제작된 번역팩입니다.",
        )

    monkeypatch.setattr("moru_engine.server.app.find_translation", fake_find)
    response = client.get(
        "/community/translation",
        headers=AUTH,
        params={
            "web_url": "https://moru.gg",
            "job_id": scan_job["id"],
            "target_lang": "ko_kr",
        },
    )

    assert response.status_code == 200
    match = response.json()["match"]
    assert match["pack_id"] == "pack-1"
    assert match["uncovered_entries"] == 40
    assert match["download_url"].endswith("/download")

    # The local side really came from the scan, not an empty placeholder.
    scan_payload = client.get(f"/scan/{scan_job['id']}/result", headers=AUTH).json()
    assert captured["local_categories"] == {
        c["name"]: c["entry_count"] for c in scan_payload["categories"]
    }
    assert captured["local_categories"]
    assert captured["identity"].name == scan_payload["identity"]["name"]
    assert captured["target_lang"] == "ko_kr"


def test_community_translation_failure_is_invisible_to_the_user(
    client: TestClient, scan_job: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An optional lookup must never turn into a wizard error.

    Offline, a platform too old to serve the route, a 500, a dropped socket:
    none of these are anything the user can act on, and the scan screen has
    nothing to say about them. Every one collapses to "nothing found".
    """
    for failure in (
        aiohttp.ClientConnectorError(Mock(), OSError("offline")),
        aiohttp.ClientResponseError(Mock(), (), status=500),
        TimeoutError(),
        ValueError("malformed candidate body"),
    ):

        async def fake_find(*_: Any, exc: BaseException = failure, **__: Any) -> None:
            raise exc

        monkeypatch.setattr("moru_engine.server.app.find_translation", fake_find)
        response = client.get(
            "/community/translation",
            headers=AUTH,
            params={
                "web_url": "https://moru.gg",
                "job_id": scan_job["id"],
                "target_lang": "ko_kr",
            },
        )
        assert response.status_code == 200, failure
        assert response.json() == {"match": None}, failure


def test_community_translation_nothing_published_is_a_clean_null(
    client: TestClient, scan_job: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_find(*_: Any, **__: Any) -> None:
        return None

    monkeypatch.setattr("moru_engine.server.app.find_translation", fake_find)
    response = client.get(
        "/community/translation",
        headers=AUTH,
        params={
            "web_url": "https://moru.gg",
            "job_id": scan_job["id"],
            "target_lang": "ko_kr",
        },
    )
    assert response.status_code == 200
    assert response.json() == {"match": None}


def test_community_translation_download_returns_migration_inputs(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """What comes back is exactly what the A/B/C migration inputs accept."""
    rp = tmp_path / "resource_pack.zip"
    rp.write_bytes(b"RP")

    async def fake_download(download_url: str, destination: Path) -> dict[str, Path]:
        assert download_url == "https://moru.gg/api/packs/pack-1/download"
        return {"resource_pack": rp}

    monkeypatch.setattr("moru_engine.server.app.download_translation", fake_download)
    response = client.post(
        "/community/translation/download",
        headers=AUTH,
        json={
            "pack_id": "pack-1",
            "download_url": "https://moru.gg/api/packs/pack-1/download",
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "resourcepack_path": str(rp),
        "overrides_path": None,
    }


def test_community_translation_download_reports_its_own_failure(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unlike the lookup, this one is a button press and is owed an answer."""

    async def fake_download(*_: Any, **__: Any) -> dict[str, Path]:
        raise aiohttp.ClientError("connection reset")

    monkeypatch.setattr("moru_engine.server.app.download_translation", fake_download)
    response = client.post(
        "/community/translation/download",
        headers=AUTH,
        json={"pack_id": "pack-1", "download_url": "https://moru.gg/x"},
    )
    assert response.status_code == 502
    assert "translation download failed" in response.json()["detail"]


def test_a_downloaded_pack_alone_cannot_start_a_migration_scan(
    client: TestClient, tmp_path: Path
) -> None:
    """The safety boundary, pinned.

    A pack downloaded from moru.gg is B: target-locale files, with no record
    of the source text they were translated from. Reuse is only sound when
    that source can be shown byte-identical between the old modpack and this
    one, which needs A. Accepting B alone would apply a stale translation to
    a changed English string without anyone noticing, so it is refused at the
    request boundary rather than quietly producing zero reuse.
    """
    downloaded = tmp_path / "resource_pack.zip"
    downloaded.write_bytes(b"RP")

    response = client.post(
        "/jobs",
        headers=AUTH,
        json={
            "type": "scan",
            "params": {
                "modpack_path": str(MODPACK),
                "previous_resourcepack_path": str(downloaded),
            },
        },
    )
    assert response.status_code == 422
    assert "previous_modpack_path" in response.json()["detail"]
