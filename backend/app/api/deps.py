from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.core.deps import get_current_admin, get_current_partner, get_db
from app.models import AdminUser, Partner


def db_session(db: Session = Depends(get_db)) -> Session:
    return db


def current_admin(request: Request, db: Session = Depends(get_db)) -> AdminUser:
    return get_current_admin(request, db)


def current_partner(request: Request, db: Session = Depends(get_db)) -> Partner:
    return get_current_partner(request, db)
