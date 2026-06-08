"""Web dashboard runner (``agentic-pr-dash serve``).

Boots the FastAPI app under uvicorn. The web stack is an optional dependency —
install with ``pip install 'agentic-pr-dash[serve]'``.
"""

from __future__ import annotations

import os
import socket
import sys


def _port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def main() -> None:
    try:
        import uvicorn
    except ImportError:
        print(
            "agentic-pr-dash serve needs the web extra: pip install 'agentic-pr-dash[serve]'",
            file=sys.stderr,
        )
        raise SystemExit(1)

    host = os.environ.get("AGENTIC_PR_DASH_HOST", "127.0.0.1")
    port = int(os.environ.get("AGENTIC_PR_DASH_PORT", os.environ.get("PORT", "9000")))
    reload_enabled = os.environ.get("AGENTIC_PR_DASH_RELOAD", "").lower() in {"1", "true", "yes", "on"}

    if _port_in_use(host, port):
        print(
            f"agentic-pr-dash dashboard already running at http://{host}:{port} "
            f"— port {port} is in use",
            file=sys.stderr,
        )
        raise SystemExit(1)

    uvicorn.run(
        "agentic_pr_dash.app:app",
        host=host,
        port=port,
        reload=reload_enabled,
    )


if __name__ == "__main__":
    main()
