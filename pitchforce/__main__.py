"""Command line: ``python -m pitchforce <command>``

  migrate              apply database migrations
  web [--port 8000]    run the web server (does not run background jobs)
  worker               run the background worker (media, analysis, exports)
  dev                  migrate, then run web + one worker together (local convenience)
  create-user EMAIL    create a user (prompts for password via PITCHFORCE_PASSWORD env or stdin)
  sample               create the clearly labeled synthetic SAMPLE event and process it
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys


def migrate() -> None:
    from alembic import command
    from alembic.config import Config

    from .config import ROOT

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    command.upgrade(cfg, "head")


def bootstrap_admin() -> None:
    from sqlalchemy import select

    from .auth import create_user
    from .config import get_settings
    from .db import SessionLocal
    from .models import User

    s = get_settings()
    with SessionLocal() as db:
        if db.scalar(select(User.id).limit(1)) is None and s.admin_email and s.admin_password:
            create_user(db, s.admin_email, s.admin_password, "Administrator", is_admin=True)
            print(f"Created administrator account {s.admin_email}")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="pitchforce")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate")
    w = sub.add_parser("web")
    w.add_argument("--host", default="127.0.0.1")
    w.add_argument("--port", type=int, default=8000)
    sub.add_parser("worker")
    d = sub.add_parser("dev")
    d.add_argument("--port", type=int, default=8000)
    cu = sub.add_parser("create-user")
    cu.add_argument("email")
    cu.add_argument("--name", default="")
    cu.add_argument("--admin", action="store_true")
    sub.add_parser("sample")
    a = p.parse_args(argv)

    if a.cmd == "migrate":
        migrate()
        print("Database is up to date.")
    elif a.cmd == "web":
        import uvicorn

        migrate()
        bootstrap_admin()
        uvicorn.run("pitchforce.web.app:app", host=a.host, port=a.port, proxy_headers=True)
    elif a.cmd == "worker":
        from .jobs.worker import main as worker_main

        migrate()
        worker_main()
    elif a.cmd == "dev":
        migrate()
        bootstrap_admin()
        worker = subprocess.Popen([sys.executable, "-m", "pitchforce", "worker"], env=os.environ.copy())
        try:
            import uvicorn

            uvicorn.run("pitchforce.web.app:app", host="127.0.0.1", port=a.port)
        finally:
            worker.terminate()
    elif a.cmd == "create-user":
        from .auth import create_user
        from .db import SessionLocal

        migrate()
        pw = os.environ.get("PITCHFORCE_PASSWORD") or input("Password (min 10 chars): ")
        with SessionLocal() as db:
            create_user(db, a.email, pw, a.name, is_admin=a.admin)
        print(f"Created user {a.email}")
    elif a.cmd == "sample":
        from .db import SessionLocal
        from .jobs.worker import drain
        from .services.sample import create_sample_event

        migrate()
        with SessionLocal() as db:
            ev = create_sample_event(db, user_id=None)
            db.commit()
            print(f"Created sample event {ev.id}; processing…")
        drain()
        print("Sample event processed. Open the web app to review it.")


if __name__ == "__main__":
    main()
