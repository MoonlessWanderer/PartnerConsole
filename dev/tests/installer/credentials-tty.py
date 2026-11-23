"""Exercise the installed launcher as root with a real Linux controlling terminal."""

import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pexpect
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import Base, make_engine
from app.core.deps import get_db
from app.core.security import hash_password, verify_password
from app.main import app as api
from app.models import AdminUser


LAUNCHER = "/usr/local/bin/partnerconsole"
ROOT_URL = "mysql+pymysql://root:fixture-root-password@127.0.0.1:3306/mysql"
URL = "mysql+pymysql://partnerconsole:fixture-application-password@127.0.0.1:3306/partnerconsole"
DECOY_URL = "mysql+pymysql://root:fixture-root-password@127.0.0.1:3306/credentials_decoy"
ORIGINAL_PASSWORD = "original-fixture-password"
NEW_PASSWORD = "replacement-fixture-password"


def seed(engine, names):
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.query(AdminUser).delete()
        for admin_id, name in enumerate(names, start=1):
            db.add(AdminUser(id=admin_id, username=name, password_hash=hash_password(ORIGINAL_PASSWORD)))
        db.commit()


def accounts(engine):
    with Session(engine) as db:
        return list(db.execute(select(AdminUser.id, AdminUser.username, AdminUser.password_hash).order_by(AdminUser.id)))


def spawn(*args, **env):
    return pexpect.spawn(LAUNCHER, list(args), env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", **env},
                         encoding="utf-8", timeout=15)


def listing(child, choice="1"):
    child.expect_exact("Admin Credentials")
    child.expect_exact("Choice: ")
    child.sendline(choice)
    child.expect_exact("Admin id: ")
    return child.before


def finish(child, status=0):
    child.expect(pexpect.EOF)
    child.close()
    assert child.exitstatus == status


def configured_admins(output):
    assert "1 fixture_admin" in output and "2 other_admin" in output, "configured MySQL admins were not listed"


@contextmanager
def unchanged(config, *engines):
    before = [accounts(engine) for engine in engines]
    env = config.read_bytes() if config.exists() else None
    yield
    assert [accounts(engine) for engine in engines] == before, "unexpected database change"
    assert (config.read_bytes() if config.exists() else None) == env, "unexpected config change"


def baseline(mysql, sqlite, decoy, config):
    with unchanged(config, mysql, sqlite, decoy):
        for inherited, unwanted in (({}, "sqlite_decoy"), ({"DATABASE_URL": DECOY_URL}, "mysql_decoy")):
            with spawn("credentials", **inherited) as child:
                output = listing(child)
                assert f"1 {unwanted}" in output, "RC7 did not select the expected fallback database"
                try:
                    configured_admins(output)
                except AssertionError:
                    pass
                else:
                    raise AssertionError("RC7 passed the MySQL regression check")
                child.sendline("not-an-id")
                child.expect_exact("Choose an administrator by id.")
                child.expect_exact("Choice: ")
                child.sendline("0")
                finish(child)
    print("RC7 reproduced: the configured MySQL admins are absent. The CLI selected SQLite or inherited MySQL.")


def rename(child, admin_id, username):
    child.sendline(admin_id)
    child.expect_exact("New username: ")
    child.sendline(username)


def password(child, first, second, admin_id="1"):
    child.sendline(admin_id)
    child.expect_exact("New password: ")
    child.sendline(first)
    child.expect_exact("Repeat password: ")
    assert first not in child.before, "the password was echoed"
    child.sendline(second)


def return_to_menu(child, expected, *, success=False):
    child.expect_exact("Choice: ")
    output = child.before
    assert expected in output, f"missing diagnostic: {expected}"
    assert not any(secret in output for secret in (URL, DECOY_URL, ORIGINAL_PASSWORD, NEW_PASSWORD))
    if not success:
        assert "Password updated" not in output and "is now" not in output


def api_login(engine):
    def fixture_db():
        with Session(engine) as db:
            yield db
            db.commit()

    settings.secret_key = "synthetic-login-check-key-for-the-credentials-job"
    api.dependency_overrides[get_db] = fixture_db
    client = TestClient(api, raise_server_exceptions=True)
    try:
        for candidate, status in ((NEW_PASSWORD, 200), (ORIGINAL_PASSWORD, 401)):
            response = client.post("/api/admin/auth/login", json={"username": "fixture_owner", "password": candidate})
            assert response.status_code == status, "administrator login did not match the updated hash"
    finally:
        client.close()
        api.dependency_overrides.clear()


def successful_operations(config, mysql, sqlite, decoy):
    old, other = accounts(mysql)
    metadata = config.stat()
    before = config.read_bytes()
    with unchanged(config, mysql, sqlite, decoy), spawn("credentials", DATABASE_URL=DECOY_URL) as child:
        output = listing(child)
        configured_admins(output)
        assert "mysql_decoy" not in output and "sqlite_decoy" not in output
        child.sendline("invalid")
        return_to_menu(child, "Choose an administrator by id.")
        child.sendline("0")
        finish(child)

    with spawn() as child:
        child.expect_exact("10) Admin Credentials")
        child.expect_exact("Choice: ")
        child.sendline("10")
        configured_admins(listing(child))
        rename(child, "1", "  fixture_owner  ")
        return_to_menu(child, "Administrator 1 is now fixture_owner.", success=True)
        after_rename = accounts(mysql)
        assert after_rename == [(old.id, "fixture_owner", old.password_hash), other]
        child.sendline("2")
        child.expect_exact("Admin id: ")
        password(child, NEW_PASSWORD, NEW_PASSWORD)
        return_to_menu(child, "Password updated.", success=True)
        child.sendline("0")
        child.expect_exact("Press Enter to return to the menu...")
        child.sendline("")
        child.expect_exact("Choice: ")
        child.sendline("0")
        finish(child)

    assert config.read_bytes() == before.replace(b"ADMIN_USERNAME=fixture_admin", b"ADMIN_USERNAME=fixture_owner")
    updated = config.stat()
    assert (updated.st_mode, updated.st_uid, updated.st_gid) == (metadata.st_mode, metadata.st_uid, metadata.st_gid)
    changed, untouched = accounts(mysql)
    assert changed.id == old.id and changed.username == "fixture_owner" and untouched == other
    assert changed.password_hash != old.password_hash and verify_password(NEW_PASSWORD, changed.password_hash)
    assert not verify_password(ORIGINAL_PASSWORD, changed.password_hash)
    api_login(mysql)
    print("Configured MySQL 8.4 verified through both entry points: rename, config sync, password hash and API login.")


def rejected_start(expected, choice=None):
    with spawn("credentials", DATABASE_URL=DECOY_URL) as child:
        if choice is not None:
            child.expect_exact("Choice: ")
            child.sendline(choice)
        child.expect_exact(expected)
        output = child.before
        finish(child, 1)
        output += child.before
        assert not any(secret in output for secret in (URL, DECOY_URL, "incorrect-fixture-password"))
        assert "Password updated" not in output and "is now" not in output


def rejected_config(config, engines, app):
    valid = config.read_bytes()
    config.unlink()
    with unchanged(config, *engines):
        rejected_start("Missing or unsafe Credentials file:")
    config.write_bytes(valid)
    config.chmod(0)
    with unchanged(config, *engines):
        rejected_start("Cannot read config/api.env.")
    config.chmod(0o640)
    config.rename(config.with_suffix(".original"))
    config.symlink_to(config.with_suffix(".original"))
    with unchanged(config, *engines):
        rejected_start("Missing or unsafe Credentials file:")
    config.unlink()
    config.with_suffix(".original").rename(config)

    for script in ("common.sh", "credentials.sh"):
        path = app / "scripts" / script
        mode = path.stat().st_mode
        path.chmod(mode | 0o020)
        with unchanged(config, *engines):
            rejected_start("someone other than root can write it")
        path.chmod(mode)

    for value, diagnostic, choice in (
        ("this is not an assignment", "Invalid line in", None),
        ("# unset", "Set DATABASE_URL in config/api.env", None),
        ("DATABASE_URL=", "Set DATABASE_URL in config/api.env", None),
        ("DATABASE_URL=sqlite:///./data/partner.db", "must select the installed local MySQL database", None),
        ("DATABASE_URL=invalid-url", "must select the installed local MySQL database", None),
        (f"DATABASE_URL={URL}_wrong", "must select the installed local MySQL database", None),
        (f"DATABASE_URL={URL.replace(':3306/', ':3307/')}", "must select the installed local MySQL database", None),
        (f"DATABASE_URL={URL.replace('fixture-application-password', 'incorrect-fixture-password')}",
         "Credentials operation failed", "1"),
    ):
        config.write_text(f"ENVIRONMENT=production\n{value}\nADMIN_USERNAME=fixture_owner\n", encoding="utf-8")
        with unchanged(config, *engines):
            rejected_start(diagnostic, choice)
    config.write_bytes(valid)

    with unchanged(config, *engines):
        result = subprocess.run([LAUNCHER, "credentials"], stdin=subprocess.DEVNULL, start_new_session=True,
                                capture_output=True, text=True, timeout=15)
        assert result.returncode == 1 and "Admin credentials need a terminal." in result.stderr
        with spawn("credentials") as child:
            child.expect_exact("Choice: ")
            rejected_start("Another Credentials menu is already open")
            child.sendline("0")
            finish(child)
    print("Rejected unsafe or invalid configuration, wrong MySQL targets, bad authentication and missing TTY.")


def rejected_operations(config, root, mysql, sqlite, decoy):
    engines = (mysql, sqlite, decoy)
    for choice, admin_id, values, diagnostic in (
        ("1", "999", ["unused"], "Admin not found"),
        ("1", "1", ["1 invalid"], "Username must start with a letter"),
        ("1", "1", ["other_admin"], "That username is already used"),
        ("2", "999", [], "Admin not found"),
        ("2", "1", ["short", "short"], "Choose a non-default password"),
        ("2", "1", [NEW_PASSWORD, ORIGINAL_PASSWORD], "The passwords do not match."),
    ):
        with unchanged(config, *engines), spawn("credentials") as child:
            listing(child, choice)
            if choice == "1":
                rename(child, admin_id, *values)
            elif values:
                password(child, *values, admin_id=admin_id)
            else:
                child.sendline(admin_id)
            return_to_menu(child, diagnostic)
            child.sendline("0")
            finish(child)

    with root.begin() as db:
        db.execute(text("CREATE TRIGGER partnerconsole.block_update BEFORE UPDATE ON partnerconsole.admin_users "
                        "FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='Synthetic update failure'"))
    try:
        with unchanged(config, *engines), spawn("credentials") as child:
            listing(child)
            rename(child, "1", "blocked_rename")
            return_to_menu(child, "Credentials operation failed")
            child.sendline("2")
            child.expect_exact("Admin id: ")
            password(child, NEW_PASSWORD, NEW_PASSWORD)
            return_to_menu(child, "Credentials operation failed")
            child.sendline("0")
            finish(child)
    finally:
        with root.begin() as db:
            db.execute(text("DROP TRIGGER partnerconsole.block_update"))

    subprocess.run(["mount", "--bind", config.parent, config.parent], check=True)
    try:
        subprocess.run(["mount", "-o", "remount,bind,ro", config.parent], check=True)
        with unchanged(config, *engines), spawn("credentials") as child:
            listing(child)
            rename(child, "1", "config_write_error")
            return_to_menu(child, "Could not update config/api.env.")
            child.sendline("0")
            finish(child)
    finally:
        subprocess.run(["umount", config.parent], check=True)

    seed(mysql, [])
    with unchanged(config, *engines):
        rejected_start("No administrator exists.", "1")
    print("Rejected invalid and duplicate inputs, an empty admin table, MySQL write errors and a read-only config mount.")


def main():
    app, mode = Path(sys.argv[1]), sys.argv[2]
    config = app / "config/api.env"
    config.write_bytes(f"ENVIRONMENT=production\r\nDATABASE_URL={URL}\r\nADMIN_USERNAME=fixture_admin\r\n".encode())
    config.chmod(0o640)
    root = make_engine(ROOT_URL)
    with root.begin() as connection:
        assert connection.scalar(text("SELECT VERSION()")).startswith("8.4.")
        connection.execute(text("CREATE DATABASE IF NOT EXISTS credentials_decoy"))
    mysql = make_engine(URL)
    sqlite = make_engine(f"sqlite:///{app / 'backend/data/partner.db'}")
    decoy = make_engine(DECOY_URL)
    seed(mysql, ["fixture_admin", "other_admin"])
    seed(sqlite, ["sqlite_decoy"])
    seed(decoy, ["mysql_decoy"])
    if mode == "baseline":
        baseline(mysql, sqlite, decoy, config)
    elif mode == "fixed":
        before = accounts(sqlite), accounts(decoy)
        successful_operations(config, mysql, sqlite, decoy)
        rejected_config(config, (mysql, sqlite, decoy), app)
        rejected_operations(config, root, mysql, sqlite, decoy)
        assert (accounts(sqlite), accounts(decoy)) == before, "a decoy database changed"
    else:
        raise SystemExit("Unknown test mode")


if __name__ == "__main__":
    main()
