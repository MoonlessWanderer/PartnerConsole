"""Test-only API worker with a fixed Tehran billing clock."""

from datetime import datetime, timezone
from importlib import import_module
import os

from app.core import timeutil


fixed_now = datetime.fromisoformat(os.environ["STAGE6_FIXED_TEHRAN_NOW"])
original_now_tehran = timeutil.now_tehran


def fixed_now_tehran(now=None):
    return original_now_tehran(fixed_now if now is None else now)


def fixed_utcnow():
    return fixed_now.astimezone(timezone.utc)


timeutil.utcnow = fixed_utcnow
timeutil.now_tehran = fixed_now_tehran
app = import_module("app.main").app
