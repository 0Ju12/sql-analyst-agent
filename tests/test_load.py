import duckdb

from sql_agent.load import load_manifest


def test_load_manifest(tmp_path):
    (tmp_path / "raw").mkdir()
    (tmp_path / "raw" / "a.csv").write_text("id,v\n1,가\n2,나\n", encoding="utf-8")
    (tmp_path / "m.yaml").write_text(
        "tables:\n  - name: a\n    file: raw/a.csv\n    options: {header: true}\n", encoding="utf-8"
    )
    db = tmp_path / "w.duckdb"
    assert load_manifest(tmp_path / "m.yaml", db) == {"a": 2}
    assert duckdb.connect(str(db)).execute("select v from a order by id").fetchall() == [
        ("가",),
        ("나",),
    ]
