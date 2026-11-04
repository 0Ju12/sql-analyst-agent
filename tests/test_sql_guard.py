import pytest

from sql_agent.sql_guard import check_sql


def test_simple_select_passes_and_gets_limit(catalog):
    r = check_sql("select name, city from customers", catalog, max_rows=50)
    assert r.ok, r.errors
    assert "LIMIT 50" in r.sql
    assert any("LIMIT" in w for w in r.warnings)


def test_existing_limit_kept(catalog):
    r = check_sql("select name from customers limit 3", catalog)
    assert r.ok and r.sql.endswith("LIMIT 3") and not r.warnings


@pytest.mark.parametrize(
    "sql",
    [
        "with t as (select customer_id, sum(amount) s from orders group by 1) "
        "select c.name, t.s from t join customers c using (customer_id)",
        "select city, count(*) n from customers group by city order by n desc",
        "select name from customers union all select status from orders",
        "select date_trunc('month', ordered_at) m, sum(amount) from orders group by m",
        "SELECT * FROM main.orders;",
    ],
)
def test_valid_queries_pass(catalog, sql):
    r = check_sql(sql, catalog)
    assert r.ok, r.errors


@pytest.mark.parametrize(
    "sql, needle",
    [
        ("drop table orders", "DROP"),
        ("delete from orders", "DELETE"),
        ("insert into orders values (1,1,'2024-01-01',1,'x')", "INSERT"),
        ("update orders set amount = 0", "UPDATE"),
        ("create table x as select 1", "CREATE"),
        ("attach 'other.db'", "ATTACH"),
        ("copy orders to 'out.csv'", "COPY"),
        ("pragma version", "PRAGMA"),
        ("set threads = 1", "SET"),
        ("install httpfs", "INSTALL"),
    ],
)
def test_non_select_rejected(catalog, sql, needle):
    r = check_sql(sql, catalog)
    assert not r.ok
    assert needle in r.errors[0]


@pytest.mark.parametrize(
    "sql",
    [
        "select * from read_csv('/etc/passwd')",
        "select * from read_parquet('x.parquet')",
        "select * from '/etc/passwd.csv'",
        "select * from glob('*')",
        "select * from information_schema.tables",
    ],
)
def test_file_and_system_access_rejected(catalog, sql):
    assert not check_sql(sql, catalog).ok


def test_multiple_statements_rejected(catalog):
    r = check_sql("select 1; drop table orders", catalog)
    assert not r.ok and "한 문장" in r.errors[0]


def test_unknown_table_suggests_name(catalog):
    r = check_sql("select * from order", catalog)
    assert not r.ok  # 'order'는 예약어라 문법 오류일 수도 있다
    r = check_sql("select * from ordrs", catalog)
    assert not r.ok and "ordrs" in r.errors[0] and "orders" in r.errors[0]


def test_unknown_column_suggests_name(catalog):
    r = check_sql("select nam from customers", catalog)
    assert not r.ok and "nam" in r.errors[0] and "name" in r.errors[0]


def test_syntax_error(catalog):
    r = check_sql("selec name from customers", catalog)
    assert not r.ok


def test_empty(catalog):
    assert not check_sql("  ;", catalog).ok
