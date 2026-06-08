"""Web dashboard runner (``pr-agent-ops serve``).

Boots the FastAPI app under uvicorn. The web stack is an optional dependency —
install with ``pip install 'pr-agent-ops[serve]'``.
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
            "pr-agent-ops serve needs the web extra: pip install 'pr-agent-ops[serve]'",
            file=sys.stderr,
        )
        raise SystemExit(1)

    host = os.environ.get("PR_AGENT_OPS_HOST", "127.0.0.1")
    port = int(os.environ.get("PR_AGENT_OPS_PORT", os.environ.get("PORT", "9000")))
    reload_enabled = os.environ.get("PR_AGENT_OPS_RELOAD", "").lower() in {"1", "true", "yes", "on"}

    if _port_in_use(host, port):
        print(
            f"pr-agent-ops dashboard already running at http://{host}:{port} "
            f"— port {port} is in use",
            file=sys.stderr,
        )
        raise SystemExit(1)

    uvicorn.run(
        "pr_agent_ops.app:app",
        host=host,
        port=port,
        reload=reload_enabled,
    )


if __name__ == "__main__":
    main()
