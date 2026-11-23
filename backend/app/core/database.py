from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.core.config import settings
from app.core.db_types import MYSQL_TABLE_OPTIONS


class Base(DeclarativeBase):
    __table_args__ = MYSQL_TABLE_OPTIONS


def make_engine(url: str | None = None):
    database_url = url or settings.database_url
    if database_url.startswith("sqlite"):
        engine = create_engine(
            database_url,
            connect_args={"check_same_thread": False, "timeout": 30},
            hide_parameters=True,
        )

        @event.listens_for(engine, "connect")
        def _sqlite_connect(dbapi_connection, _connection_record):
            # SQLAlchemy emits BEGIN itself. Without this, two deferred
            # transactions can both read a balance and overwrite each other.
            dbapi_connection.isolation_level = None
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        @event.listens_for(engine, "begin")
        def _sqlite_begin_immediate(connection):
            connection.exec_driver_sql("BEGIN IMMEDIATE")

        return engine
    parsed = make_url(database_url)
    if parsed.get_backend_name() == "mysql":
        if parsed.drivername != "mysql+pymysql":
            raise ValueError("MySQL requires the mysql+pymysql driver")
        parsed = parsed.update_query_dict({"charset": "utf8mb4"})
        engine = create_engine(parsed, pool_pre_ping=True, isolation_level="READ COMMITTED",
                               hide_parameters=True)

        @event.listens_for(engine, "connect")
        def _mysql_connect(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("SET SESSION time_zone = '+00:00'")
                cursor.execute("SET SESSION collation_connection = 'utf8mb4_0900_bin'")
                # Preserve other server modes, while prohibiting silent truncation.
                cursor.execute("SET SESSION sql_mode = CONCAT_WS(',', @@SESSION.sql_mode, 'STRICT_TRANS_TABLES')")
            finally:
                cursor.close()

        return engine
    return create_engine(database_url, pool_pre_ping=True, hide_parameters=True)


engine = make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
