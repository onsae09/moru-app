"""Web-platform upload client for the upload job.

Talks to the moru.gg web API (contracts in moru-app/contracts/web-api.yaml):
presigned-slot request, archive PUT, and pack registration. Module-level
coroutines (same pattern as live_models.py) so tests can monkeypatch each
step of the sequence independently. ``api_token`` is forwarded as a
Bearer header when present; every call carries the ``X-Moru-Client``
marker, which the web platform accepts in place of an account for
anonymous desktop uploads.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import json
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any

import aiohttp

from .. import __version__
from ..scanner.pack_identity import declare_version_range

if TYPE_CHECKING:
    from pathlib import Path
    from collections.abc import Callable

#: Metadata calls (slot request / registration) are small JSON round-trips.
_API_TIMEOUT = aiohttp.ClientTimeout(total=30)
#: The archive PUT streams the whole zip; generous for slow uplinks.
_PUT_TIMEOUT = aiohttp.ClientTimeout(total=600)

logger = logging.getLogger(__name__)


def _slot_key(file: dict[str, Any]) -> str | tuple[str, str]:
    return (file["group"], file["kind"]) if "group" in file else file["kind"]


class WebUploadError(Exception):
    """The web platform rejected an upload step (HTTP error status)."""


def _auth_headers(api_token: str | None) -> dict[str, str]:
    """Desktop client marker plus optional Bearer auth."""
    headers = {"X-Moru-Client": f"moru-engine/{__version__}"}
    if api_token:
        headers["Authorization"] = f"Bearer {api_token}"
    return headers


def _retry_after_minutes(raw: str | None) -> int | None:
    """Minutes to wait from a ``Retry-After`` header (delta-seconds or HTTP date)."""
    if not raw:
        return None
    text = raw.strip()
    try:
        seconds = int(text)
    except ValueError:
        try:
            delta = parsedate_to_datetime(text) - datetime.now(UTC)
        except (TypeError, ValueError):
            return None
        seconds = int(delta.total_seconds())
    return max(1, (seconds + 59) // 60) if seconds > 0 else None


async def _ensure_ok(resp: aiohttp.ClientResponse, step: str) -> None:
    """Raise WebUploadError for 4xx/5xx, surfacing the body's error message.

    A rate-limited upload is the one failure the user can act on, so 429 gets
    its own message carrying the wait from ``Retry-After``. Edge layers answer
    429 with an HTML page instead of the API's JSON, so a body that does not
    parse as JSON is dropped rather than pasted into the message - raw markup
    in an error dialog tells the user nothing about what went wrong.
    """
    if resp.status < 400:
        return
    try:
        body = await resp.text()
    except Exception:  # noqa: BLE001 — body is best-effort diagnostics
        body = ""
    detail = ""
    try:
        parsed = json.loads(body)
    except ValueError:
        pass
    else:
        if isinstance(parsed, dict):
            detail = str(parsed.get("error") or parsed.get("detail") or "")
    detail = detail.strip()[:300]

    if resp.status == 429:
        minutes = _retry_after_minutes(resp.headers.get("Retry-After"))
        wait = f"약 {minutes}분 후" if minutes else "잠시 후"
        raise WebUploadError(
            f"업로드 요청 한도를 초과했습니다. {wait} 다시 시도해 주세요."
        )

    message = f"{step} failed: HTTP {resp.status}"
    raise WebUploadError(f"{message} - {detail}" if detail else message)


async def request_upload_slots(
    web_url: str, api_token: str | None, files: list[dict[str, Any]]
) -> dict[str | tuple[str, str], dict[str, Any]]:
    """POST /api/upload-url; slots keyed by kind, or (group, kind).

    Grouped archives must never overwrite another mod's slot of the same
    kind. Ungrouped whole-pack uploads retain their kind-only addressing.
    """
    async with aiohttp.ClientSession(timeout=_API_TIMEOUT) as session:
        async with session.post(
            f"{web_url}/api/upload-url",
            json={"files": files},
            headers=_auth_headers(api_token),
        ) as resp:
            await _ensure_ok(resp, "upload slot request")
            payload = await resp.json()
    uploads = payload.get("uploads") or []
    slots = {
        _slot_key(u): u for u in uploads if isinstance(u, dict) and u.get("kind")
    }
    for spec in files:
        slot = slots.get(_slot_key(spec))
        if not slot or not slot.get("url") or not slot.get("object_key"):
            raise WebUploadError(
                f"upload slot request returned no usable {_slot_key(spec)} slot"
            )
    return slots


async def put_archive(url: str, zip_path: Path) -> None:
    """PUT the zip to the presigned URL, streaming the file from disk."""
    size = zip_path.stat().st_size
    async with aiohttp.ClientSession(timeout=_PUT_TIMEOUT) as session:
        with zip_path.open("rb") as fh:
            async with session.put(
                url,
                data=fh,
                headers={
                    "Content-Type": "application/zip",
                    "Content-Length": str(size),
                },
            ) as resp:
                await _ensure_ok(resp, "archive upload")


def _compatible_versions(payload: dict[str, Any]) -> dict[str, str] | None:
    """``compatible_versions`` block for a TranslationPackCreate body.

    None when the pack's own modpack version is unknown: there is nothing to
    anchor a range to, and the platform then keeps resolving the pack by
    exact version exactly as it did before the field existed.
    """
    declared = declare_version_range(
        payload.get("modpack_version"), payload.get("compatible_up_to")
    )
    return None if declared is None else {"min": declared.min, "max": declared.max}


async def register_pack(
    web_url: str, api_token: str | None, payload: dict[str, Any]
) -> dict[str, Any]:
    """POST /api/translations (TranslationPackCreate); return {pack_id, url}.

    The compatible modpack version range is finalized here, the one place
    every published pack passes through. ``compatible_up_to`` is free text
    the user typed into the export form, so it is a job parameter rather
    than a wire field: it is checked against the built version (see
    :func:`~moru_engine.scanner.pack_identity.declare_version_range`) and a
    value that does not hold up publishes the exact-version point range
    instead of a claim nobody can verify.
    """
    body = {key: value for key, value in payload.items() if key != "compatible_up_to"}
    compatible = _compatible_versions(payload)
    if compatible is not None:
        body["compatible_versions"] = compatible
    async with aiohttp.ClientSession(timeout=_API_TIMEOUT) as session:
        async with session.post(
            f"{web_url}/api/translations",
            json=body,
            headers=_auth_headers(api_token),
        ) as resp:
            await _ensure_ok(resp, "pack registration")
            return await resp.json()


async def register_mod_batch(
    web_url: str, api_token: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """Register up to 100 per-mod packs without individual announcements."""
    async with aiohttp.ClientSession(timeout=_API_TIMEOUT) as session:
        async with session.post(
            f"{web_url}/api/translations/batch",
            json=payload,
            headers=_auth_headers(api_token),
        ) as resp:
            await _ensure_ok(resp, "mod pack registration")
            return await resp.json()


def _mod_slot_specs(mods: list[dict[str, Any]]) -> list[dict[str, Any]]:
    specs = []
    for mod in mods:
        for kind, archive in mod["zips"].items():
            with archive.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            specs.append({
                "group": mod["jar_name"],
                "kind": kind,
                "size": archive.stat().st_size,
                "sha256": digest,
            })
    return specs


async def upload_mod_packs(
    web_url: str,
    api_token: str,
    mods: list[dict[str, Any]],
    metadata: dict[str, Any],
    *,
    loader: str | None = None,
    progress: Callable[[int, int, str], None] | None = None,
) -> dict[str, int]:
    """Grouped slots, six concurrent PUTs, and partial-success registration."""
    counts = {"shared": 0, "skipped": 0, "failed": 0}
    eligible = []
    for mod in mods:
        if mod["project_id"] and mod["game_versions"] and mod["zips"]:
            eligible.append(mod)
        else:
            counts["skipped"] += 1
    semaphore = asyncio.Semaphore(6)
    current = 0
    for start in range(0, len(eligible), 100):
        chunk = eligible[start:start + 100]
        digest_task = asyncio.create_task(asyncio.to_thread(_mod_slot_specs, chunk))
        try:
            specs = await asyncio.shield(digest_task)
            slots = await request_upload_slots(web_url, api_token, specs)
        except asyncio.CancelledError:
            # Close worker-owned file handles before the job removes staging
            # (Windows refuses to unlink a file that is still open).
            await digest_task
            raise
        except Exception:
            logger.exception("Could not obtain per-mod upload slots")
            counts["failed"] += len(chunk)
            current += len(chunk)
            if progress:
                progress(current, len(eligible), chunk[-1]["display_name"])
            continue

        async def put_mod(mod: dict[str, Any]) -> dict[str, Any] | None:
            nonlocal current
            try:
                files = []
                for kind, archive in mod["zips"].items():
                    slot = slots[(mod["jar_name"], kind)]
                    async with semaphore:
                        await put_archive(str(slot["url"]), archive)
                    files.append({"kind": kind, "object_key": slot["object_key"]})
                item = {
                    "curseforge_id": mod["project_id"],
                    "name": mod["display_name"],
                    "version": mod["version_label"],
                    "game_versions": mod["game_versions"],
                    "files": files,
                    "stats": mod["stats"],
                }
                if mod["file_id"]:
                    item["curseforge_file_id"] = mod["file_id"]
                if loader:
                    item["loader"] = loader
                return item
            except Exception:
                logger.exception("Could not upload mod pack %s", mod["jar_name"])
                counts["failed"] += 1
                return None
            finally:
                current += 1
                if progress:
                    progress(current, len(eligible), mod["display_name"])

        uploaded = [item for item in await asyncio.gather(*(put_mod(mod) for mod in chunk)) if item]
        if not uploaded:
            continue
        try:
            registered = await register_mod_batch(
                web_url, api_token, {**metadata, "mods": uploaded}
            )
            shared = len(registered["packs"])
            counts["shared"] += shared
            counts["failed"] += len(uploaded) - shared
        except Exception:
            logger.exception("Could not register per-mod packs")
            counts["failed"] += len(uploaded)
    return counts
