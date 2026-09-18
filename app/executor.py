"""DuckDB execution layer.

One embedded DuckDB file holds every uploaded dataset as its own table
(`ds_<id>`). Writes (uploads) are serialized behind a lock; reads run on
per-call cursors (concurrent SELECTs on the shared instance) with a hard
timeout enforced via connection.interrupt() from the watchdog.

No DB server anywhere — DuckDB is in-process, which is the whole point.
"""
import logging
import os
import threading
import time
import uuid
from datetime import date, datetime

import duckdb

from . import config

log = logging.getLogger("quackquery.executor")

_conn = None
_conn_lock = threading.Lock()


class QueryTimeout(RuntimeError):
    pass


def db() -> duckdb.DuckDBPyConnection:
    """Lazily open the single read-write connection (instance cached by path)."""
    global _conn
    if _conn is None:
        with _conn_lock:
            if _conn is None:
                os.makedirs(config.DATA_DIR, exist_ok=True)
                _conn = duckdb.connect(config.DB_PATH)
                log.info("duckdb opened at %s", config.DB_PATH)
    return _conn


def run_write(fn):
    """Run `fn(conn)` serialized under the write lock (DDL / ingest)."""
    conn = db()  # open outside the lock — db() itself takes _conn_lock on init
    with _conn_lock:
        return fn(conn)


def create_dataset_table(csv_path: str) -> str:
    """Register a CSV as table ds_<uuid>. Returns the table name."""
    table = "ds_" + uuid.uuid4().hex[:12]
    sql = (f'CREATE TABLE "{table}" AS SELECT * FROM read_csv(?, header=true, auto_detect=true)')
    def _op(conn):
        conn.execute(sql, [csv_path])
        n = conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
        return n
    n = run_write(_op)
    log.info("created %s from %s (%d rows)", table, csv_path, n)
    return table, n


def drop_dataset_table(table: str) -> None:
    run_write(lambda conn: conn.execute(f'DROP TABLE IF EXISTS "{table}"'))


def describe(table: str):
    """[(name, type), ...] via DuckDB's own inference."""
    rows = run_write(lambda conn: conn.execute(f'DESCRIBE "{table}"').fetchall())
    return [(r[0], r[1]) for r in rows]


def sample_values(table: str, column: str, k: int = config.SCHEMA_SAMPLE_VALUES):
    """k distinct non-null sample values, used to prompt the LLM."""
    col = column.replace('"', '""')
    rows = run_write(lambda conn: conn.execute(
        f'SELECT DISTINCT "{col}" FROM "{table}" WHERE "{col}" IS NOT NULL LIMIT {k}'
    ).fetchall())
    return [r[0] for r in rows]


def _qi(identifier: str) -> str:
    """Quote a SQL identifier (column/table), doubling embedded quotes."""
    return '"' + identifier.replace('"', '""') + '"'


def _jsonable(v):
    """DuckDB scalar -> JSON-safe value (dates ISO, Decimal -> float)."""
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    if isinstance(v, (int, float, str, bool)) or v is None:
        return v
    return str(v)


def profile_table(table: str, top_k: int = 8, low_card_max: int = 50) -> dict:
    """Column-level profile for the Data page: null count, distinct count,
    and top values for low-cardinality columns (capped at `top_k`).

    Fast by construction: ONE aggregate query for all null/distinct counts,
    then one GROUP BY per low-cardinality column only.
    """
    cols = describe(table)
    if not cols:
        return {"row_count": 0, "columns": []}

    parts = ["count(*) AS __n"]
    for i, (name, _dtype) in enumerate(cols):
        q = _qi(name)
        parts.append(f"count({q}) AS __nn{i}")
        parts.append(f"count(DISTINCT {q}) AS __nd{i}")
    agg_sql = f"SELECT {', '.join(parts)} FROM {_qi(table)}"
    row = run_write(lambda conn: conn.execute(agg_sql).fetchone())
    n = row[0]

    columns = []
    for i, (name, dtype) in enumerate(cols):
        nonnull, distinct = row[1 + 2 * i], row[2 + 2 * i]
        entry = {
            "name": name,
            "type": dtype,
            "nulls": n - nonnull,
            "distinct": distinct,
            "top_values": None,
        }
        if 0 < distinct <= low_card_max:
            q = _qi(name)
            top_sql = (f"SELECT {q} AS v, count(*) AS c FROM {_qi(table)} "
                       f"WHERE {q} IS NOT NULL GROUP BY 1 "
                       f"ORDER BY 2 DESC, 1 ASC LIMIT {int(top_k)}")
            vals = run_write(lambda conn: conn.execute(top_sql).fetchall())
            entry["top_values"] = [{"value": _jsonable(v), "count": c}
                                   for v, c in vals]
        columns.append(entry)
    return {"row_count": n, "columns": columns}


def run_query(sql: str, table: str | None = None,
              timeout_s: float = config.QUERY_TIMEOUT_S):
    """Execute a (already guard-approved) SELECT on a fresh cursor.

    If `table` is given, a cursor-local TEMP view named `data` is created for
    it first — concurrent queries against different datasets can never see
    each other's `data` (cursor-scoped isolation, verified in tests).

    Returns (columns, rows). Raises QueryTimeout past the deadline, and
    propagates duckdb.Error (Binder/Catalog/...) for the repair loop.
    """
    cur = db().cursor()
    setup = (f'CREATE TEMP VIEW data AS SELECT * FROM "{table}"'
             if table else None)
    holder = {}

    def _work():
        try:
            if setup:
                cur.execute(setup)
            res = cur.execute(sql)
            holder["cols"] = [d[0] for d in cur.description]
            holder["rows"] = res.fetchall()
        except BaseException as e:  # noqa: BLE001 — worker thread boundary
            holder["err"] = e

    t = threading.Thread(target=_work, daemon=True, name="duckdb-query")
    t.start()
    t.join(timeout_s)
    if t.is_alive():
        cur.interrupt()
        t.join(2)
        raise QueryTimeout(f"query exceeded {timeout_s:.0f}s limit")
    if "err" in holder:
        raise holder["err"]
    return holder["cols"], holder["rows"]


def ping() -> bool:
    try:
        return db().execute("SELECT 42").fetchone()[0] == 42
    except Exception:  # noqa: BLE001
        return False
