"""MySQL storage variants; SQLite/PostgreSQL behavior remains unchanged."""
from datetime import timezone

from sqlalchemy import DateTime, Integer, BigInteger
from sqlalchemy.dialects.mysql import DATETIME, LONGTEXT
from sqlalchemy.types import TypeDecorator, Text

MYSQL_TABLE_OPTIONS = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_0900_bin",
}


class MySQLUTCDateTime(TypeDecorator):
    impl = DATETIME
    cache_ok = True

    def __init__(self):
        super().__init__(fsp=6)

    def process_bind_param(self, value, dialect):
        if value is not None and value.tzinfo is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        return value

    def process_result_value(self, value, dialect):
        return value.replace(tzinfo=timezone.utc) if value is not None else None


def utc_datetime():
    return DateTime(timezone=True).with_variant(MySQLUTCDateTime(), "mysql")


def money_integer():
    return Integer().with_variant(BigInteger(), "mysql")


def operation_text():
    return Text().with_variant(LONGTEXT(), "mysql")
