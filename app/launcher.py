"""`networthy` — run the app on your own machine, one command.

    uvx networthy          # no install
    pipx run networthy     # same, via pipx

Sets local mode, picks a data directory the OS actually lets a pip-installed
package write to, finds a free port, starts uvicorn and opens a browser. Nothing
here is used by the hosted deployment, which runs uvicorn directly.

Why a launcher at all: the app is a web app, so "run it locally" means starting a
server and pointing a browser at it. Doing that by hand is three commands and an
environment variable — this is those three commands.
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import webbrowser
from pathlib import Path


def user_data_dir() -> Path:
    """Per-OS location for the database.

    A pip-installed package has no writable directory of its own — site-packages
    is shared and often read-only — so the DB has to live in the user's own data
    directory, in the place each platform expects to find it.
    """
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "Networthy"
    if os.name == "nt":
        base = os.environ.get("APPDATA") or (home / "AppData" / "Roaming")
        return Path(base) / "Networthy"
    # Linux and friends: honour the XDG spec.
    base = os.environ.get("XDG_DATA_HOME") or (home / ".local" / "share")
    return Path(base) / "networthy"


def free_port(preferred: int = 8321) -> int:
    """The preferred port if it's free, else whatever the OS hands out.

    Binding a fixed port fails if the user already has something there (or a
    second copy of this running), and a crashed-on-startup app is a terrible
    first impression for a one-command install.
    """
    for candidate in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", candidate))
                return s.getsockname()[1]
            except OSError:
                continue
    return preferred


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="networthy",
        description="Run Networthy HQ locally. Your data stays on this machine.",
    )
    parser.add_argument("--port", type=int, default=None,
                        help="port to serve on (default: 8321, or any free port)")
    parser.add_argument("--data-dir", default=None,
                        help=f"where to keep the database (default: {user_data_dir()})")
    parser.add_argument("--no-browser", action="store_true",
                        help="don't open a browser window")
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address (default: 127.0.0.1, i.e. this machine only)")
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir).expanduser() if args.data_dir else user_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)

    # Set before importing the app: storage reads NETWORTHY_DATA_DIR at import
    # time, and auth reads NETWORTHY_LOCAL per request.
    os.environ["NETWORTHY_DATA_DIR"] = str(data_dir)
    os.environ.setdefault("NETWORTHY_LOCAL", "1")

    port = args.port or free_port()
    url = f"http://127.0.0.1:{port}/"

    banner = (
        f"\n  Networthy HQ — running locally\n"
        f"  {url}\n"
        f"  Data: {data_dir}\n\n"
        f"  Your statements and holdings stay on this machine. Live prices\n"
        f"  send only ticker symbols. Ctrl-C to stop.\n"
    )
    # flush: the URL is the one thing this command exists to tell you, and a
    # buffered stdout (piped, or a launcher window) would swallow it.
    print(banner, flush=True)

    if not args.no_browser:
        # Wait for the server to accept connections before opening the tab,
        # otherwise the browser races startup and shows a connection error.
        threading.Thread(target=_open_when_ready, args=(port, url), daemon=True).start()

    import uvicorn
    uvicorn.run("app.main:app", host=args.host, port=port, log_level="warning")
    return 0


def _open_when_ready(port: int, url: str, timeout: float = 20.0) -> None:
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.25)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                webbrowser.open(url)
                return
        time.sleep(0.15)


if __name__ == "__main__":
    raise SystemExit(main())
