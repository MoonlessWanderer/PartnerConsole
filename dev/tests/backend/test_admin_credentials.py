"""Admin username and password changes stay inside the existing account model."""
from sqlalchemy.orm import sessionmaker

from app.core.database import Base, make_engine
from app.core.security import check_password, create_token, decode_token
from app.models import AdminUser, Partner
from app.reset_admin_password import rename_admin, set_password, validate_admin_username
import pytest


def test_rename_changes_only_the_selected_admin(tmp_path):
    engine = make_engine(f"sqlite:///{(tmp_path / 'admins.db').as_posix()}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with sessions() as db:
        first = AdminUser(username="admin", password_hash="hash-one")
        second = AdminUser(username="other", password_hash="hash-two")
        db.add_all([first, second, Partner(username="shop", password_hash="shop-hash", balance_toman=25)])
        db.commit()
        first_id, partner_balance = first.id, 25
        rename_admin(db, first_id, "owner")
    with sessions() as db:
        assert db.get(AdminUser, first_id).username == "owner"
        assert db.get(AdminUser, first_id).password_hash == "hash-one"
        assert db.query(AdminUser).filter_by(username="other").one().password_hash == "hash-two"
        assert db.query(Partner).one().balance_toman == partner_balance


def test_duplicate_username_is_rejected(tmp_path):
    engine = make_engine(f"sqlite:///{(tmp_path / 'admins.db').as_posix()}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    with sessions() as db:
        db.add_all([
            AdminUser(username="admin", password_hash="a"),
            AdminUser(username="owner", password_hash="b"),
        ])
        db.commit()
        admin_id = db.query(AdminUser).filter_by(username="admin").one().id
        with pytest.raises(ValueError):
            rename_admin(db, admin_id, "owner")
        assert db.get(AdminUser, admin_id).username == "admin"


def test_invalid_username_is_rejected():
    with pytest.raises(ValueError):
        validate_admin_username("admin name")
    with pytest.raises(ValueError):
        validate_admin_username("1admin")


def test_new_password_replaces_the_old_one_without_revoking_the_session(tmp_path, monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "secret_key", "synthetic-test-key-1234567890abcd")
    monkeypatch.setattr(settings, "admin_jwt_secret", "synthetic-admin-key-1234567890abcd")
    engine = make_engine(f"sqlite:///{(tmp_path / 'admins.db').as_posix()}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with sessions() as db:
        admin = AdminUser(username="admin", password_hash="placeholder")
        db.add(admin)
        db.commit()
        set_password(db, "admin", "first-password-123")
        token = create_token("admin", admin.id, admin.username)
        set_password(db, "admin", "second-password-123")
        stored = db.get(AdminUser, admin.id).password_hash
    assert check_password("first-password-123", stored) is False
    assert check_password("second-password-123", stored) is True
    assert decode_token(token, "admin")["sub"] == str(admin.id)
