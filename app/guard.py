"""Read-only SQL guard.

Every generated SQL statement passes through `validate()` BEFORE it touches
DuckDB. The guard is intentionally paranoid and deny-by-default:

  1. exactly one statement (no `;` stacking, no comment smuggling)
  2. must start with SELECT or WITH (CTE) — everything else is rejected
  3. denylist of keywords/functions regardless of position: DML/DDL, PRAGMA,
     COPY/READ/LOAD file functions, ATTACH, INSTALL, CALL, SET ...
  4. no string-literal obfuscation tricks survive because we scan the
     uppercased statement with string literals stripped first

DuckDB is additionally connected read-only for query execution; the guard is
the first layer, not the only one (defense in depth).
"""
import re

# Statements must begin with one of these (after stripping whitespace/parens)
_ALLOWED_STARTS = ("SELECT", "WITH")

# Flat keyword denylist — matched as whole words against the literal-stripped SQL.
_DENIED_KEYWORDS = {
    # DML / DDL
    "INSERT", "UPDATE", "DELETE", "MERGE", "REPLACE",
    "CREATE", "DROP", "ALTER", "TRUNCATE", "VACUUM",
    # transaction / session control
    "BEGIN", "COMMIT", "ROLLBACK", "ABORT", "SET", "RESET",
    "PREPARE", "EXECUTE", "DEALLOCATE",
    # duckdb-specific escape hatches
    "PRAGMA", "ATTACH", "DETACH", "INSTALL", "LOAD", "EXPORT",
    "COPY", "IMPORT", "CHECKPOINT", "CALL", "USE",
}

# Function denylist (matched as word(...) patterns even inside a SELECT).
_DENIED_FUNCTIONS = [
    "read_csv", "read_csv_auto", "read_parquet", "read_json", "read_json_auto",
    "read_sql", "read_blob", "write_csv", "write_parquet", "write_blob",
    "sniff_csv", "glob", "file_search", "ls", "pwd", "gethostname",
    "shell", "system", "exec", "eval", "python_udf",
]

_COMMENT_RE = re.compile(r"--[^\n]*|/\*.*?\*/", re.S)
_LITERAL_RE = re.compile(r"'(?:[^']|'')*'")


class GuardError(ValueError):
    """Raised when SQL fails the read-only guard. Message is safe to show."""


def _strip(sql: str) -> str:
    """Remove comments and string literals so hides inside them can't dodge the scan."""
    sql = _COMMENT_RE.sub(" ", sql)
    sql = _LITERAL_RE.sub(" '' ", sql)
    return sql


def validate(sql: str) -> str:
    """Return the (cleaned) SQL if it is a single read-only SELECT; raise GuardError.

    Cleaning = strip trailing whitespace and a single trailing semicolon.
    """
    if not sql or not sql.strip():
        raise GuardError("empty SQL")
    cleaned = sql.strip()
    if cleaned.endswith(";"):
        cleaned = cleaned[:-1].rstrip()
        if ";" in cleaned:  # e.g. "SELECT 1;;"
            raise GuardError("multiple statements are not allowed")
    if ";" in cleaned:
        raise GuardError("multiple statements are not allowed (stacked query)")

    stripped = _strip(cleaned)
    first = stripped.lstrip("( \n\t").split(None, 1)
    head = first[0].upper() if first else ""
    if head not in _ALLOWED_STARTS:
        raise GuardError(
            f"only {_ALLOWED_STARTS[0]}/{_ALLOWED_STARTS[1]} statements are allowed "
            f"(got {head or '<empty>'})")

    tokens = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", stripped.upper()))
    hit = tokens & _DENIED_KEYWORDS
    if hit:
        raise GuardError(f"forbidden keyword(s): {', '.join(sorted(hit))}")

    lowered = stripped.lower()
    for fn in _DENIED_FUNCTIONS:
        if re.search(rf"\b{re.escape(fn)}\s*\(", lowered):
            raise GuardError(f"forbidden function: {fn}()")

    return cleaned
