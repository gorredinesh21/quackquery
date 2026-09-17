"""Guard unit tests: the read-only SQL gate."""
import pytest

from app.guard import GuardError, validate

VALID = [
    "SELECT 1",
    "select * from data",
    "SELECT a, b FROM data WHERE x > 5 LIMIT 10",
    "WITH t AS (SELECT 1 AS x) SELECT * FROM t",
    "  (SELECT count(*) FROM data)  ",
    "SELECT 'DELETE FROM data is just a string' AS s",   # literal stripped
    "SELECT 1;",                                          # single trailing ;
    "SELECT\n  col\nFROM data",                           # newlines fine
    "SELECT * FROM data -- trailing comment",
]

INVALID = {
    "DELETE FROM data": "statement",
    "delete from data": "lowercase delete",
    "UPDATE data SET x = 1": "update",
    "INSERT INTO data VALUES (1)": "insert",
    "DROP TABLE data": "drop",
    "CREATE TABLE t (x INT)": "create",
    "TRUNCATE data": "truncate",
    "ALTER TABLE data ADD COLUMN z INT": "alter",
    "SELECT 1; DELETE FROM data": "stacked",
    "SELECT 1; SELECT 2": "stacked selects",
    "SELECT * FROM read_csv_auto('x.csv')": "file function",
    "SELECT * FROM read_csv('x.csv')": "file function 2",
    "SELECT * FROM read_parquet('x.parquet')": "parquet escape",
    "PRAGMA database_list": "pragma",
    "PRAGMA table_info('data')": "pragma 2",
    "ATTACH 'other.db' AS o": "attach",
    "INSTALL httpfs": "install",
    "COPY data TO 'out.csv'": "copy out",
    "SET memory_limit='1GB'": "set",
    "CALL pragma_table_info('data')": "call",
    "WITH d AS (DELETE FROM data RETURNING *) SELECT * FROM d": "cte smuggle",
    "SELECT shell('rm -rf /')": "shell function",
    "EXPLAIN SELECT 1": "explain head",
    "": "empty",
    "   ": "whitespace",
}


@pytest.mark.parametrize("sql", VALID)
def test_valid(sql):
    assert validate(sql).startswith(("SELECT", "select", "WITH", "with", "("))


@pytest.mark.parametrize(("sql", "label"), sorted(INVALID.items()))
def test_invalid(sql, label):
    with pytest.raises(GuardError):
        validate(sql)


def test_multiple_semicolons():
    with pytest.raises(GuardError):
        validate("SELECT 1;;")


def test_error_message_is_safe():
    with pytest.raises(GuardError, match="only SELECT/WITH"):
        validate("DELETE FROM data")
