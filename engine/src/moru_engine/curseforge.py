"""CurseForge's whitespace-normalized MurmurHash2 fingerprints and lookup API."""

from __future__ import annotations

import asyncio
import os
import struct
from pathlib import Path

import httpx

_API_URL = "https://api.curseforge.com/v1"
_MASK = 0xFFFFFFFF
_MULTIPLIER = 0x5BD1E995
_WHITESPACE = b"\x09\x0a\x0d\x20"
_CHUNK_SIZE = 1024 * 1024

FingerprintMatch = tuple[int, int, list[str], str]


def curseforge_fingerprint(path: Path) -> int:
    """MurmurHash2, seed 1, ignoring precisely tab/LF/CR/space bytes.

    Murmur2 seeds with the normalized length. Two streaming passes avoid
    keeping a large JAR and its normalized copy resident for every worker.
    Carry the last 0..3 bytes between chunks to retain little-endian words.
    """
    with path.open("rb") as stream:
        length = 0
        while chunk := stream.read(_CHUNK_SIZE):
            length += len(chunk.translate(None, _WHITESPACE))
        stream.seek(0)
        h = (1 ^ length) & _MASK
        remainder = b""
        while chunk := stream.read(_CHUNK_SIZE):
            data = remainder + chunk.translate(None, _WHITESPACE)
            end = len(data) & ~3
            for (k,) in struct.iter_unpack("<I", memoryview(data)[:end]):
                k = (k * _MULTIPLIER) & _MASK
                k ^= k >> 24
                k = (k * _MULTIPLIER) & _MASK
                h = ((h * _MULTIPLIER) & _MASK) ^ k
            remainder = data[end:]
        if remainder:
            h ^= int.from_bytes(remainder, "little")
            h = (h * _MULTIPLIER) & _MASK
        h ^= h >> 13
        h = (h * _MULTIPLIER) & _MASK
        h ^= h >> 15
        return h & _MASK


def _api_key() -> str | None:
    key = os.environ.get("MORU_CURSEFORGE_API_KEY")
    if key:
        return key
    try:
        from ._embedded_keys import CURSEFORGE_API_KEY
    except ImportError:
        return None
    return CURSEFORGE_API_KEY or None


class CurseForgeClient:
    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key if api_key is not None else _api_key()

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    async def fingerprint_matches(
        self, fingerprints: list[int]
    ) -> dict[int, FingerprintMatch]:
        """Exact matches only; at most 100 fingerprints and four requests at once."""
        if not self.enabled or not fingerprints:
            return {}
        unique = list(dict.fromkeys(fingerprints))
        semaphore = asyncio.Semaphore(4)
        matches: dict[int, FingerprintMatch] = {}
        async with httpx.AsyncClient(
            base_url=_API_URL + "/",
            headers={"x-api-key": self.api_key, "Accept": "application/json"},
            timeout=15.0,
        ) as client:
            async def lookup(chunk: list[int]) -> None:
                async with semaphore:
                    response = await client.post(
                        "fingerprints/432", json={"fingerprints": chunk}
                    )
                    response.raise_for_status()
                    for match in response.json()["data"]["exactMatches"]:
                        file = match["file"]
                        fingerprint = file["fileFingerprint"]
                        if fingerprint in chunk:
                            matches[fingerprint] = (
                                file["modId"],
                                file["id"],
                                file.get("gameVersions") or [],
                                file.get("displayName") or file.get("fileName") or "",
                            )

            await asyncio.gather(*(
                lookup(unique[start:start + 100])
                for start in range(0, len(unique), 100)
            ))
        return matches
