"""CurseForge fingerprints: unsigned arithmetic, normalization and exact matches."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx

from moru_engine import curseforge
from moru_engine.curseforge import CurseForgeClient, curseforge_fingerprint


def test_murmur2_vector_and_only_curseforge_whitespace(tmp_path: Path, monkeypatch) -> None:
    # Reference implementation verified against JEI file 8858454 on the live
    # API: both that file and our implementation returned 2950913249.
    path = tmp_path / "fingerprint.bin"
    path.write_bytes(b"moru!!!")
    assert curseforge_fingerprint(path) == 3042456682
    path.write_bytes(b" \tm\no\rr u!\t!\n!\r ")
    # Five-byte chunks split both normalized words and whitespace sequences.
    monkeypatch.setattr(curseforge, "_CHUNK_SIZE", 5)
    assert curseforge_fingerprint(path) == 3042456682
    path.write_bytes(b"moru!!!\x0b")
    assert curseforge_fingerprint(path) != 3042456682


async def test_lookup_chunks_exact_matches_and_ignores_unmatched(monkeypatch) -> None:
    requests = []
    active = maximum = 0

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal active, maximum
        assert request.url.path == "/v1/fingerprints/432"
        assert request.headers["x-api-key"] == "test-key"
        fingerprints = json.loads(request.content)["fingerprints"]
        requests.append(fingerprints)
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.001)
        active -= 1
        return httpx.Response(200, json={"data": {
            "exactMatches": [
                {"id": 50, "file": {
                    "modId": 50, "id": value + 1000,
                    "fileFingerprint": value, "gameVersions": ["1.20.1", "Forge"],
                    "displayName": f"Mod release {value}",
                }} for value in fingerprints if value != 204
            ],
            "partialMatches": [],
        }})

    original = httpx.AsyncClient
    monkeypatch.setattr(curseforge.httpx, "AsyncClient", lambda **kwargs: original(
        **kwargs, transport=httpx.MockTransport(respond)
    ))
    matches = await CurseForgeClient("test-key").fingerprint_matches(list(range(205)) + [0])
    assert sorted(map(len, requests)) == [5, 100, 100]
    assert 1 < maximum <= 4
    assert set(matches) == set(range(204))
    assert matches[203] == (50, 1203, ["1.20.1", "Forge"], "Mod release 203")
    assert await CurseForgeClient("").fingerprint_matches([0]) == {}
