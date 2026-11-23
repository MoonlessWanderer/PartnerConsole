"""Verify that the MySQL integration fixture cannot redirect its destination."""
import pytest
from sqlalchemy.dialects.mysql.pymysql import MySQLDialect_pymysql

from test_mysql_integration import isolated_mysql_url


@pytest.mark.parametrize("query", ["database=synthetic_other", "host=other.example.test", "charset=latin1"])
def test_test_url_rejects_driver_overrides(query):
    with pytest.raises(ValueError, match="may only include charset"):
        isolated_mysql_url(f"mysql+pymysql://synthetic@127.0.0.1/partnerpanel_test_example?{query}")


def test_isolated_url_maps_to_local_test_schema_in_driver():
    url = isolated_mysql_url("mysql+pymysql://synthetic@127.0.0.1:3307/partnerpanel_test_example?charset=utf8mb4")
    _, opts = MySQLDialect_pymysql().create_connect_args(url)
    assert opts["host"] == "127.0.0.1" and opts["port"] == 3307
    assert opts.get("database", opts.get("db")) == "partnerpanel_test_example"
    assert opts["charset"] == "utf8mb4"
