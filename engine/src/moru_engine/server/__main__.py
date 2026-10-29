"""Sidecar entrypoint: ``python -m moru_engine.server --port N --token T``.

Spawned by the Electron main process, which picks a free port and issues a
fresh session token per run. Binds 127.0.0.1 only.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time

import uvicorn

from ..utils.log import setup_logging
from .app import create_app

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="moru-engine-server",
        description="Moru engine local API server (desktop sidecar).",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind address; loopback only by design (default: %(default)s).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=43110,
        help="TCP port to listen on (default: %(default)s).",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="Session token; falls back to the MORU_SESSION_TOKEN env var.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Root log level (default: %(default)s).",
    )
    parser.add_argument(
        "--exit-with-stdin",
        action="store_true",
        help=(
            "Shut down when stdin reaches EOF. The desktop holds the other end "
            "of the pipe, so the OS closes it when the app dies by any means."
        ),
    )
    args = parser.parse_args(argv)

    token = args.token or os.environ.get("MORU_SESSION_TOKEN")
    if not token:
        parser.error("--token is required (or set MORU_SESSION_TOKEN)")

    setup_logging(getattr(logging, args.log_level))
    app = create_app(token=token)
    config = uvicorn.Config(app, host=args.host, port=args.port, log_config=None)
    server = uvicorn.Server(config)
    # POST /shutdown flips server.should_exit for a graceful drain.
    app.state.uvicorn_server = server
    if args.exit_with_stdin:
        _exit_with_stdin(server)
    logger.info("Moru engine API listening on %s:%d", args.host, args.port)
    server.run()
    return 0


#: How long a graceful drain may take once the parent is gone.
_ORPHAN_GRACE_SECONDS = 10.0


def _exit_with_stdin(server: uvicorn.Server) -> None:
    """Stop the server once the parent's end of stdin closes.

    A desktop killed from the task manager, or crashing, never sends
    POST /shutdown; without this the engine kept running and kept calling a
    paid provider for a run nobody could see.
    """

    def watch() -> None:
        try:
            while sys.stdin.buffer.read(4096):
                pass
        except (OSError, ValueError):
            pass
        logger.warning("Desktop app is gone (stdin closed); shutting down")
        server.should_exit = True
        time.sleep(_ORPHAN_GRACE_SECONDS)
        os._exit(0)

    if sys.stdin is None:
        return
    threading.Thread(target=watch, name="parent-watch", daemon=True).start()


if __name__ == "__main__":
    sys.exit(main())
