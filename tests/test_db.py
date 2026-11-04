import duckdb
import pytest

from sql_agent.db import QueryTimeout, run_query


def test_connection_is_read_only(con):
    with pytest.raises(duckdb.Error):
        con.execute("delete from orders")


def test_external_access_blocked_even_without_guard(con):
    with pytest.raises(duckdb.Error):
        con.execute("select * from read_csv('/etc/passwd')").fetchall()


def test_settings_locked(con):
    with pytest.raises(duckdb.Error):
        con.execute("set enable_external_access = true")


def test_row_cap(con):
    r = run_query(con, "select * from range(1000)", max_rows=10)
    assert len(r.rows) == 10 and r.truncated


def test_timeout(con):
    with pytest.raises(QueryTimeout):
        run_query(con, "select count(*) from range(100000000000) a", timeout_s=0.5)
