"""Command line admin: accounts, and importing files without a browser.

    python -m app.cli adduser <name>
    python -m app.cli passwd <name>
    python -m app.cli listusers
    python -m app.cli import <name> <path>...

Passwords are always prompted, never taken as an argument — an argument would end up
in the shell history and in the process list.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from app.auth import create_user, get_user_by_name, hash_password
from app.config import get_settings
from app.db import User, create_db_engine, create_session_factory, init_db
from sqlalchemy import select

MIN_PASSWORD_LENGTH = 8


def _prompt_new_password() -> str:
    while True:
        password = getpass.getpass("Password: ")
        if len(password) < MIN_PASSWORD_LENGTH:
            print(f"Too short — at least {MIN_PASSWORD_LENGTH} characters.", file=sys.stderr)
            continue
        if password != getpass.getpass("Repeat: "):
            print("Passwords do not match.", file=sys.stderr)
            continue
        return password


def cmd_adduser(args, db_factory) -> int:
    with db_factory() as db:
        if get_user_by_name(db, args.username) is not None:
            print(f"User {args.username!r} already exists.", file=sys.stderr)
            return 1
        password = _prompt_new_password()
        create_user(db, args.username, password, is_admin=args.admin)
    print(f"Created user {args.username!r}.")
    return 0


def cmd_passwd(args, db_factory) -> int:
    with db_factory() as db:
        user = get_user_by_name(db, args.username)
        if user is None:
            print(f"No such user: {args.username!r}", file=sys.stderr)
            return 1
        user.password_hash = hash_password(_prompt_new_password())
        db.commit()
    print(f"Password updated for {args.username!r}.")
    return 0


def cmd_listusers(_args, db_factory) -> int:
    with db_factory() as db:
        users = db.execute(select(User).order_by(User.username)).scalars().all()
    if not users:
        print("No users yet. Create one with: python -m app.cli adduser <name>")
        return 0
    for user in users:
        flag = " (admin)" if user.is_admin else ""
        print(f"{user.username}{flag}\tcreated {user.created_at:%Y-%m-%d}")
    return 0


def cmd_import(args, db_factory) -> int:
    """Import files straight from disk, using the same pipeline as the web upload."""
    from app.ingest import Ingestor

    settings = get_settings()
    paths: list[Path] = []
    for raw in args.paths:
        path = Path(raw)
        if path.is_dir():
            paths.extend(sorted(p for p in path.rglob("*") if p.suffix.lower() == ".bys"))
        else:
            paths.append(path)

    if not paths:
        print("No .bys files found.", file=sys.stderr)
        return 1

    with db_factory() as db:
        user = get_user_by_name(db, args.username)
        if user is None:
            print(f"No such user: {args.username!r}", file=sys.stderr)
            return 1

        ingestor = Ingestor(db=db, user=user, settings=settings)
        try:
            counts = {"imported": 0, "duplicate": 0, "failed": 0}
            for result in ingestor.ingest_files(
                (p.name, p.read_bytes()) for p in paths
            ):
                counts[result.status] = counts.get(result.status, 0) + 1
                if result.status != "imported":
                    print(f"{result.status:10s} {result.filename}: {result.reason}")
        finally:
            ingestor.close()

    print(
        f"\nimported {counts['imported']}, "
        f"skipped {counts['duplicate']} duplicate, "
        f"failed {counts['failed']}"
    )
    return 0 if counts["failed"] == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.cli", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("adduser", help="create an account")
    p.add_argument("username")
    p.add_argument("--admin", action="store_true")
    p.set_defaults(func=cmd_adduser)

    p = sub.add_parser("passwd", help="change an account password")
    p.add_argument("username")
    p.set_defaults(func=cmd_passwd)

    p = sub.add_parser("listusers", help="list accounts")
    p.set_defaults(func=cmd_listusers)

    p = sub.add_parser("import", help="import .bys files or a directory of them")
    p.add_argument("username")
    p.add_argument("paths", nargs="+")
    p.set_defaults(func=cmd_import)

    args = parser.parse_args(argv)

    settings = get_settings()
    engine = create_db_engine(settings)
    init_db(engine)
    db_factory = create_session_factory(engine)

    return args.func(args, db_factory)


if __name__ == "__main__":
    raise SystemExit(main())
