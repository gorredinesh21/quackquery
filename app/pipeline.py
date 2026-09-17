"""The query pipeline: catalog → SQL → guard → execute → (repair loop) →
rows → chart + answer. Shared by POST /api/query (JSON) and
POST /api/query/stream (SSE); both consume the same emit(event) stream.
"""
import logging
import time
import uuid

from . import cache, catalog as catalog_mod, config, executor, guard, llm as llm_mod, store

log = logging.getLogger("quackquery.pipeline")

MAX_ATTEMPTS = 1 + 3  # initial + up to 3 LLM repair passes


class PipelineError(RuntimeError):
    """All attempts exhausted. Carries the SQL attempt trail."""

    def __init__(self, message: str, attempts: list):
        super().__init__(message)
        self.attempts = attempts


def _jsonable_rows(columns, rows, max_rows):
    out = []
    for r in rows[:max_rows]:
        out.append([str(v) if not isinstance(v, (int, float, bool, type(None), str)) else v
                    for v in r])
    return out


def run_pipeline(dataset_id: str, question: str, emit) -> dict:
    """Run the full text-to-SQL pipeline for one question, emitting stage
    events to `emit(dict)`. Returns the final result payload."""
    t0 = time.time()

    # stage 1: catalog (per-dataset, built at upload)
    ds = store.get(dataset_id)
    if ds is None:
        raise KeyError(f"unknown dataset_id: {dataset_id}")
    cat = ds["catalog"]
    schema_block = catalog_mod.schema_prompt_block(cat)
    emit({"type": "stage", "name": "catalog", "dataset": ds["name"],
          "row_count": ds["row_count"]})

    # cached? short-circuit everything after catalog
    ck = cache.result_cache.key(dataset_id, question)
    cached = cache.result_cache.get(ck)
    if cached is not None:
        cached = dict(cached)
        cached["cached"] = True
        cached["qid"] = uuid.uuid4().hex[:12]
        cache.remember_qid(cached["qid"], cached)
        emit({"type": "done", **{k: cached[k] for k in
                                 ("qid", "cached", "timing_ms", "attempts")}})
        return cached

    client = llm_mod.get_llm()
    attempts: list = []
    last_error = None
    columns = rows = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        if attempt == 1:
            sql = client.generate_sql(question, schema_block, cat)
        else:
            sql = client.repair_sql(question, schema_block, attempts[-1],
                                    last_error or "unknown error", cat)
        attempts.append(sql)
        emit({"type": "sql", "sql": sql, "attempt": attempt})

        # stage 3: guard — anything non-SELECT never reaches DuckDB
        try:
            guarded = guard.validate(sql)
        except guard.GuardError as e:
            last_error = f"guard rejected: {e}"
            emit({"type": "guard_error", "error": last_error, "attempt": attempt})
            if attempt == MAX_ATTEMPTS:
                break
            emit({"type": "retry", "attempt": attempt + 1})
            continue

        # stage 4: execute (cursor-local TEMP view "data" -> dataset table)
        try:
            columns, rows = executor.run_query(guarded, ds["table"])
            break
        except executor.QueryTimeout as e:
            last_error = f"timeout: {e}"
        except Exception as e:  # noqa: BLE001 — duckdb binder/parser errors
            last_error = f"{type(e).__name__}: {e}"
        # stage 5: repair loop
        emit({"type": "exec_error", "error": last_error, "attempt": attempt})
        if attempt == MAX_ATTEMPTS:
            break
        emit({"type": "retry", "attempt": attempt + 1})
    else:  # pragma: no cover — loop always exits via break/raise above
        pass

    if rows is None:
        raise PipelineError(
            f"could not produce executable SQL after {len(attempts)} attempt(s); "
            f"last error: {last_error}", attempts)

    payload_rows = _jsonable_rows(columns, rows, 10_000)
    preview = payload_rows[:50]
    emit({"type": "rows", "columns": columns, "rows": preview,
          "row_count": len(payload_rows)})

    # stage 6: answer + chart (LLM in vertex mode; heuristic in mock)
    answer_block = client.answer_and_chart(question, attempts[-1], columns,
                                           payload_rows[:20])
    answer = (answer_block or {}).get("answer", "")
    chart = (answer_block or {}).get("chart")
    if not chart:
        chart = llm_mod.heuristic_chart(columns, payload_rows)
    emit({"type": "chart", "spec": chart} if chart else {"type": "chart", "spec": None})
    emit({"type": "answer", "text": answer})

    timing_ms = int((time.time() - t0) * 1000)
    result = {
        "qid": uuid.uuid4().hex[:12],
        "dataset_id": dataset_id,
        "question": question,
        "sql": attempts[-1],
        "sql_attempts": attempts,
        "columns": columns,
        "row_count": len(payload_rows),
        "chart": chart,
        "answer": answer,
        "attempts": len(attempts),
        "timing_ms": timing_ms,
        "cached": False,
    }
    # Cap stored/served rows
    result["rows"] = payload_rows[:config.MAX_ROWS_IN_RESPONSE]

    cache.result_cache.put(ck, {k: v for k, v in result.items() if k != "qid"})
    cache.remember_qid(result["qid"], result)
    emit({"type": "done", "qid": result["qid"], "cached": False,
          "timing_ms": timing_ms, "attempts": len(attempts)})
    return result
