"""테스트용 작은 예제 DB (가상의 상점 데이터)."""

import duckdb
import pytest

from sql_agent.db import connect_readonly, load_catalog
from sql_agent.tools import ToolContext

SETUP = """
CREATE TABLE customers (customer_id INTEGER, name VARCHAR, city VARCHAR, joined_at DATE);
INSERT INTO customers VALUES
  (1, '가', '서울', '2024-01-05'), (2, '나', '부산', '2024-02-10'),
  (3, '다', '서울', '2024-03-15'), (4, '라', '전주', '2024-03-20');
CREATE TABLE orders (order_id INTEGER, customer_id INTEGER, ordered_at DATE,
                     amount DECIMAL(10,2), status VARCHAR);
INSERT INTO orders VALUES
  (10, 1, '2024-04-01', 12000, 'paid'), (11, 1, '2024-04-15', 8000, 'paid'),
  (12, 2, '2024-04-20', 30000, 'refunded'), (13, 3, '2024-05-02', 15000, 'paid'),
  (14, 4, '2024-05-10', 5000, 'paid'), (15, 3, '2024-05-28', 7000, 'paid');
"""


@pytest.fixture(scope="session")
def db_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("db") / "shop.duckdb"
    con = duckdb.connect(str(path))
    con.execute(SETUP)
    con.close()
    return path


@pytest.fixture
def con(db_path):
    c = connect_readonly(db_path)
    yield c
    c.close()


@pytest.fixture
def catalog(con):
    return load_catalog(con)


@pytest.fixture
def ctx(con, catalog, tmp_path):
    return ToolContext(con=con, catalog=catalog, chart_dir=tmp_path / "charts")
