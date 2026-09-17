"""Semantic catalog: DuckDB-inferred schema + stats + human descriptions.

The catalog is what the text-to-SQL prompt is built from — column names alone
are ambiguous ("amt", "dt"); names + types + sample values + one-line
descriptions make single-shot SQL generation reliable.
"""
from datetime import date, datetime

from . import config, executor


def _is_numeric(dtype: str) -> bool:
    d = dtype.upper()
    return any(t in d for t in ("INT", "DOUBLE", "FLOAT", "DECIMAL", "NUMERIC", "HUGEINT"))

def _is_temporal(dtype: str) -> bool:
    d = dtype.upper()
    return "DATE" in d or "TIMESTAMP" in d or "TIME" in d


def _default_description(column: str, dtype: str, samples: list) -> str:
    """Cheap rule-based description — used in mock mode and as LLM fallback."""
    c = column.lower()
    guesses = {
        "id": "identifier", "date": "date of the record", "year": "calendar year",
        "city": "city name", "state": "state name", "name": "name",
        "revenue": "revenue amount", "amount": "amount of money",
        "price": "price per unit", "qty": "quantity", "units": "unit count",
        "count": "count", "total": "total amount", "status": "status category",
        "category": "category label", "sector": "business sector",
        "team": "team name", "winner": "winning side", "venue": "venue name",
    }
    for k, v in guesses.items():
        if k in c:
            return f"{v} ({dtype.lower()})"
    if _is_temporal(dtype):
        return f"temporal column ({dtype.lower()})"
    if _is_numeric(dtype):
        return f"numeric column ({dtype.lower()})"
    return f"text column ({dtype.lower()}); e.g. {samples[:2]}"


def _jsonable(v):
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    if isinstance(v, (int, float, str, bool)) or v is None:
        return v
    return str(v)


def build(table: str, llm=None) -> dict:
    """Return catalog dict: columns with type, samples, description."""
    cols = executor.describe(table)
    columns = []
    for name, dtype in cols:
        samples = [_jsonable(v) for v in executor.sample_values(table, name)]
        entry = {
            "name": name,
            "type": dtype,
            "numeric": _is_numeric(dtype),
            "temporal": _is_temporal(dtype),
            "samples": samples,
            "description": None,
        }
        columns.append(entry)

    # One batched LLM call for all column descriptions (vertex mode only).
    if llm is not None:
        try:
            descriptions = llm.describe_columns(columns)
            for col, desc in zip(columns, descriptions):
                if desc:
                    col["description"] = str(desc)[:200]
        except Exception:  # noqa: BLE001 — catalog must never fail an upload
            pass
    for col in columns:
        if not col["description"]:
            col["description"] = _default_description(col["name"], col["type"], col["samples"])
    return {"columns": columns}


def schema_prompt_block(catalog: dict) -> str:
    """Render the catalog as a prompt block for the SQL LLM."""
    lines = ["Table: data (the only table; use plain column names)",
             "Columns:"]
    for c in catalog["columns"]:
        samples = ", ".join(str(s) for s in c["samples"][:3])
        lines.append(f'- "{c["name"]}" {c["type"]} — {c["description"]}'
                     + (f" | e.g. {samples}" if samples else ""))
    return "\n".join(lines)
