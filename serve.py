#!/usr/bin/env python3
"""Track_My_Tracker — run the headless API + web dashboard server.

    ./run                  # convenience wrapper (recommended)
    python3 serve.py       # auto-re-execs into ./.venv if deps are missing

You no longer need to remember `./.venv/bin/python` — if this is launched with
an interpreter that lacks the deps, it re-execs itself under the project venv.
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV_PY = ROOT / ".venv" / "bin" / "python"


def _ensure_venv():
    """Guarantee we're running under an interpreter that has uvicorn.

    If imports work, we're done. Otherwise re-exec with the project venv's
    python so a bare `python3 serve.py` (any interpreter) just works. The
    self==venv guard prevents an infinite re-exec loop when the venv itself
    is incomplete.
    """
    try:
        import uvicorn  # noqa: F401
        return
    except ModuleNotFoundError:
        pass
    # Re-exec into the project venv. A sentinel (not path comparison) guards the
    # loop: venvs that share a base interpreter resolve to the same real path,
    # so comparing sys.executable can't tell us whether we're already inside.
    if VENV_PY.exists() and not os.environ.get("_TMT_REEXEC"):
        os.environ["_TMT_REEXEC"] = "1"
        os.execv(str(VENV_PY), [str(VENV_PY), str(Path(__file__).resolve()),
                                *sys.argv[1:]])
    sys.exit(
        "uvicorn is not installed and no usable .venv was found.\n"
        f"  expected venv interpreter: {VENV_PY}\n"
        "  create it with:\n"
        "    python3 -m venv .venv\n"
        "    .venv/bin/pip install -r requirements.txt\n"
    )


def _lan_ips():
    """Best-effort primary LAN IPv4 (no packets actually sent)."""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return None


_ensure_venv()

import uvicorn

from tmt import config as configmod

if __name__ == "__main__":
    cfg = configmod.load()["api"]
    host, port = cfg["host"], cfg["port"]
    # Show where to open the dashboard. When bound to 0.0.0.0, surface the LAN
    # address too so it's one tap from a phone/laptop on the same network.
    print("\n  Track_My_Tracker dashboard")
    print(f"    local:   http://localhost:{port}")
    if host in ("0.0.0.0", "::"):
        ip = _lan_ips()
        if ip:
            print(f"    network: http://{ip}:{port}")
    print()
    uvicorn.run("tmt.api:app", host=host, port=port, log_level="info")
