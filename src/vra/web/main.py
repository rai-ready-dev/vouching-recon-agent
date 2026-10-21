"""`vra-web`: start the team web app, or manage users from the command line."""
from __future__ import annotations

import argparse
import getpass
import sys
import threading
import webbrowser
from pathlib import Path

from ..envfile import load_env_file


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="vra-web", description=__doc__)
    ap.add_argument("--data-dir", default=None, help="where the database and uploaded files live (default ./vra_data)")
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("serve", help="start the web app (default)")
    s.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 to let colleagues on your network connect")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--solo", action="store_true", help="single-person testing: allow signing off your own run")
    s.add_argument("--open", action="store_true", help="open the browser")
    u = sub.add_parser("add-user", help="create a user from the command line")
    u.add_argument("username")
    u.add_argument("--role", choices=["admin", "reviewer", "preparer"], default="preparer")
    u.add_argument("--name", default="")
    args = ap.parse_args(argv)
    load_env_file(".env")

    from .app import create_app
    from .config import Settings

    cmd = args.cmd or "serve"
    kwargs = {"data_dir": Path(args.data_dir)} if args.data_dir else {}
    if cmd == "add-user":
        settings = Settings(**kwargs)
        app = create_app(settings)
        pw = getpass.getpass("Password (min %d characters): " % settings.min_password_len)
        if pw != getpass.getpass("Repeat password: "):
            sys.exit("Passwords do not match.")
        app.state.svc.auth.create_user(args.username, args.name, pw, args.role)
        print(f"Created {args.role} '{args.username}'.")
        return

    host = getattr(args, "host", "127.0.0.1")
    port = getattr(args, "port", 8000)
    settings = Settings(host=host, port=port, solo=getattr(args, "solo", False), **kwargs)
    app = create_app(settings)
    import uvicorn

    url = f"http://{'localhost' if host in ('127.0.0.1', '0.0.0.0') else host}:{port}"
    print(f"\nVouching & Reconciliation Agent\n  Open {url}\n  Data folder: {settings.data_dir}"
          f"\n  {'SOLO mode: you may sign off your own runs' if settings.solo else 'Team mode: a different person must sign off'}"
          f"\n  Press Ctrl+C to stop.\n")
    if host == "0.0.0.0":
        print("  WARNING: reachable by other computers on your network. Use HTTPS (a reverse proxy) for real use.\n")
    if getattr(args, "open", False):
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
