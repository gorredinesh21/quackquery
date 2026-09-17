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
