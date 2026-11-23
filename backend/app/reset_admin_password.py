"""Explicit, interactive maintenance command. Never called by application startup."""
import argparse
import getpass
import os
import re
from pathlib import Path
from stat import S_IMODE
from tempfile import TemporaryDirectory

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.database import SessionLocal
from app.core.security import hash_password
from app.models import AdminUser

_USERNAME = re.compile(r"[A-Za-z][A-Za-z0-9._-]{0,63}")


def validate_admin_username(username: str) -> str:
    username = username.strip()
    if _USERNAME.fullmatch(username) is None:
        raise ValueError("Username must start with a letter and use only letters, numbers, dots, underscores, or hyphens")
    return username


def _commit_admin_config(db, path: Path, previous: str, username: str) -> None:
    try:
        info = path.stat()
        lines = path.read_bytes().splitlines(keepends=True)
        index = next((i for i in reversed(range(len(lines))) if lines[i].startswith(b"ADMIN_USERNAME=")), None)
        if index is not None:
            line = lines[index].rstrip(b"\r\n")
            if line == f"ADMIN_USERNAME={previous}".encode():
                lines[index] = f"ADMIN_USERNAME={username}".encode() + lines[index][len(line):]
            else:
                index = None
    except OSError:
        raise ValueError("Could not read config/api.env. The administrator username was not changed.") from None

    if index is None:
        db.commit()
        return

    # Apply database constraints before replacing the config. Retain its original
    # inode so a rejected database commit can restore the file without rewriting it.
    db.flush()
    try:
        with TemporaryDirectory(dir=path.parent, prefix=".credentials-") as temporary:
            staged = Path(temporary) / "updated"
            original = Path(temporary) / "original"
            os.link(path, original)
            with staged.open("xb") as stream:
                stream.write(b"".join(lines))
                os.fchown(stream.fileno(), info.st_uid, info.st_gid)
                os.fchmod(stream.fileno(), S_IMODE(info.st_mode))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(staged, path)
            try:
                db.commit()
            except Exception:
                os.replace(original, path)
                raise
    except OSError:
        raise ValueError("Could not update config/api.env. Check its filesystem and try again.") from None


def rename_admin(db, admin_id: int, username: str, *, config_path: Path | None = None) -> str:
    """Rename one admin. Existing JWTs stay valid because they carry the user id."""
    username = validate_admin_username(username)
    admin = db.get(AdminUser, admin_id, with_for_update=True)
    if admin is None:
        raise ValueError("Admin not found; this command does not create accounts or run migrations")
    previous = admin.username
    if username == previous:
        return previous
    taken = db.scalar(select(AdminUser.id).where(AdminUser.username == username))
    if taken is not None:
        raise ValueError("That username is already used")
    admin.username = username
    try:
        if config_path is None:
            db.commit()
        else:
            _commit_admin_config(db, config_path, previous, username)
    except IntegrityError:
        db.rollback()
        raise ValueError("That username is already used") from None
    return previous


def _validate_password(username: str, password: str) -> None:
    if not 12 <= len(password) <= 200 or password == "admin12345" or password == username:
        raise ValueError("Choose a non-default password of 12–200 characters")


def _commit_password(db, admin, password: str) -> None:
    if admin is None:
        raise ValueError("Admin not found; this command does not create accounts or run migrations")
    admin.password_hash = hash_password(password)
    db.commit()


def set_password(db, username: str, password: str) -> None:
    _validate_password(username, password)
    admin = db.scalar(select(AdminUser).where(AdminUser.username == username).with_for_update())
    _commit_password(db, admin, password)


def set_admin_password_by_id(db, admin_id: int, password: str) -> None:
    admin = db.get(AdminUser, admin_id, with_for_update=True)
    if admin is None:
        raise ValueError("Admin not found; this command does not create accounts or run migrations")
    _validate_password(admin.username, password)
    _commit_password(db, admin, password)


def main():
    parser = argparse.ArgumentParser(description="Reset an existing administrator password interactively")
    parser.add_argument("username")
    args = parser.parse_args()
    password = getpass.getpass("New admin password: ")
    if password != getpass.getpass("Repeat password: "):
        raise SystemExit("Passwords do not match")
    try:
        with SessionLocal() as db:
            set_password(db, args.username, password)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    print("Password updated. Rotate ADMIN_JWT_SECRET too if existing sessions must be invalidated.")


if __name__ == "__main__":
    main()
