import argparse
import os
import secrets
import tempfile
from contextlib import contextmanager
from pathlib import Path

from aiohttp import web

from .app import create_app


@contextmanager
def owned_pidfile(filename):
    if not filename:
        yield
        return
    path = Path(filename).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    own_pid = str(os.getpid())
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="ascii", dir=path.parent, delete=False) as output:
            temp_path = Path(output.name)
            os.chmod(temp_path, 0o600)
            output.write(own_pid + "\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp_path, path)
        temp_path = None
        yield
    finally:
        if temp_path:
            temp_path.unlink(missing_ok=True)
        try:
            if path.is_file() and path.read_text(encoding="ascii").strip() == own_pid:
                path.unlink()
        except (OSError, UnicodeError):
            # A replaced/unreadable file is no longer ours to clean up.
            pass


def main():
    parser = argparse.ArgumentParser(description="duet private Linux agent service")
    parser.add_argument("--host", default="127.0.0.1", choices=["127.0.0.1", "::1"])
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", default="~/.local/share/duet")
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--pid-file")
    parser.add_argument("--desktop-dir", default="~/.local/share/duet/desktop")
    parser.add_argument("--display", default=":99")
    parser.add_argument("--vnc-port", type=int, default=5900)
    parser.add_argument("--novnc-dir", default="/usr/share/novnc")
    args = parser.parse_args()
    os.umask(0o077)
    path = Path(args.token_file).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.exists():
        with path.open("x", encoding="utf-8") as output:
            output.write(secrets.token_hex(32))
    os.chmod(path, 0o600)
    token = path.read_text(encoding="utf-8").strip()
    if len(token) < 32 or any(c.isspace() for c in token):
        parser.error("token file must contain at least 32 non-whitespace characters")
    with owned_pidfile(args.pid_file):
        web.run_app(create_app(args.data_dir, token, display=args.display, vnc_port=args.vnc_port,
                              novnc_dir=args.novnc_dir, desktop_dir=args.desktop_dir), host=args.host, port=args.port,
                    access_log=None, print=lambda _: None)


if __name__ == "__main__":
    main()
