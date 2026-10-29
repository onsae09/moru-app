"""Pooled HTTP client shared by the CLI providers' async requests.

A fresh ``httpx.AsyncClient`` per request paid a TCP + TLS handshake on
every batch and dropped the keep-alive pool right after. One client is
kept per event loop instead: httpx connections are bound to the loop that
opened them, and tests run each case on a loop of its own.

A leaf module so the handlers can import it without cycling back through
the package ``__init__``.
"""

from __future__ import annotations

import asyncio
import weakref

import httpx

#: Long enough for a full streamed batch; connect fails fast.
STREAM_TIMEOUT = httpx.Timeout(600.0, connect=30.0)

_clients: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, httpx.AsyncClient] = (
    weakref.WeakKeyDictionary()
)


def async_client() -> httpx.AsyncClient:
    """The running loop's shared client, created on first use."""
    loop = asyncio.get_running_loop()
    client = _clients.get(loop)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(timeout=STREAM_TIMEOUT)
        _clients[loop] = client
    return client


async def aclose_async_client() -> None:
    """Close the running loop's client (engine shutdown)."""
    client = _clients.pop(asyncio.get_running_loop(), None)
    if client is not None:
        await client.aclose()
