"""QuackQuery — ask your data anything.

FastAPI service: CSV upload → DuckDB catalog → LLM text-to-SQL with a
self-correcting repair loop, SELECT-only guard, SSE pipeline streaming,
auto chart spec. Runs fully offline with QUACK_LLM=mock.
"""
import asyncio
import json
import logging
import os
import time
import uuid
from collections import defaultdict, deque

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from . import cache, catalog as catalog_mod, config, executor, llm as llm_mod, pipeline, store

logging.basicConfig(level=logging.INFO,
                    format='{"ts":"%(asctime)s","logger":"%(name)s","msg":%(message)r}')
log = logging.getLogger("quackquery")

app = FastAPI(
    title="QuackQuery",
    version="1.0.0",
    description="Self-correcting text-to-SQL analytics API over any CSV "
                "(DuckDB + Gemini). Upload a CSV, ask in plain English, get "
                "auditable SQL + rows + a chart spec.",
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])


# --------------------------------------------------------------------------
# Middleware: request IDs + structured access logs
# --------------------------------------------------------------------------

@app.middleware("http")
async def request_context(request: Request, call_next):
    rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
    t0 = time.time()
    response = await call_next(request)
    ms = int((time.time() - t0) * 1000)
    response.headers["X-Request-ID"] = rid
    log.info(json.dumps({"rid": rid, "method": request.method,
                         "path": request.url.path, "status": response.status_code,
                         "ms": ms}))
    return response


# --------------------------------------------------------------------------
# Per-IP rate limit (sliding window, in-memory; single instance)
# --------------------------------------------------------------------------

_hits: dict[str, deque] = defaultdict(deque)


def rate_limit(request: Request) -> None:
    ip = request.client.host if request.client else "anon"
    now = time.time()
    q = _hits[ip]
    while q and q[0] < now - 60:
        q.popleft()
    if len(q) >= config.RATE_LIMIT_RPM:
        raise HTTPException(429, "rate limit exceeded — retry in a minute")
    q.append(now)


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------

class QueryIn(BaseModel):
    dataset_id: str
    question: str = Field(min_length=3, max_length=500)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _get_llm_for_catalog():
    """Only the vertex provider adds LLM column descriptions; mock mode uses
    rule-based descriptions (keeps uploads zero-API in tests)."""
    return llm_mod.get_llm() if config.LLM_PROVIDER != "mock" else None


async def _ingest_csv(csv_path: str, name: str, source: str) -> dict:
    def work():
        table, n = executor.create_dataset_table(csv_path)
        cat = catalog_mod.build(table, llm=_get_llm_for_catalog())
        meta = store.make_meta(name, table, n, cat, source=source)
        return store.put(meta)
    return await asyncio.to_thread(work)


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@app.get("/api/healthz")
def healthz():
    return {"status": "ok", "llm": config.LLM_PROVIDER, "model": config.LLM_MODEL,
            "duckdb": executor.ping(), "datasets": store.stats()["datasets"],
            "cache": cache.result_cache.stats()}


@app.post("/api/datasets", dependencies=[Depends(rate_limit)])
async def upload_dataset(file: UploadFile = File(...)):
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(400, "only .csv files are accepted")
    raw = await file.read()
    if len(raw) > config.MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"CSV exceeds {config.MAX_UPLOAD_BYTES // (1024*1024)}MB limit")
    if not raw.strip():
        raise HTTPException(400, "empty file")
    os.makedirs(config.DATA_DIR, exist_ok=True)
    tmp = os.path.join(config.DATA_DIR, f"upload_{uuid.uuid4().hex[:8]}.csv")
    with open(tmp, "wb") as f:
        f.write(raw)
    try:
        meta = await _ingest_csv(tmp, os.path.splitext(file.filename)[0], "upload")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(422, f"could not parse CSV: {str(e)[:200]}")
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return {"dataset_id": meta["dataset_id"], "name": meta["name"],
            "row_count": meta["row_count"], "schema": meta["catalog"]}


@app.get("/api/datasets")
def list_datasets():
    return [{"dataset_id": m["dataset_id"], "name": m["name"],
             "row_count": m["row_count"], "source": m["source"],
             "created_at": m["created_at"]} for m in store.list_all()]


@app.get("/api/datasets/demo")
async def load_demos():
    """Idempotently register the 3 bundled demo datasets (returns them).
    Registered BEFORE /api/datasets/{dataset_id} so 'demo' isn't eaten by it."""
    out = []
    for fname in sorted(os.listdir(config.DEMO_DIR)):
        if not fname.endswith(".csv"):
            continue
        path = os.path.join(config.DEMO_DIR, fname)
        name = os.path.splitext(fname)[0]
        existing = next((m for m in store.list_all()
                         if m["name"] == name and m["source"] == "demo"), None)
        if existing:
            out.append(existing)
            continue
        meta = await _ingest_csv(path, name, "demo")
        out.append(meta)
    return [{"dataset_id": m["dataset_id"], "name": m["name"],
             "row_count": m["row_count"]} for m in out]


@app.get("/api/datasets/{dataset_id}")
def get_dataset(dataset_id: str):
    m = store.get(dataset_id)
    if m is None:
        raise HTTPException(404, f"unknown dataset_id: {dataset_id}")
    return m


@app.get("/api/datasets/{dataset_id}/profile")
def dataset_profile(dataset_id: str):
    """Column-level data profile: type, null count, distinct count, and top
    values (max 8) for low-cardinality columns — computed live in DuckDB."""
    m = store.get(dataset_id)
    if m is None:
        raise HTTPException(404, f"unknown dataset_id: {dataset_id}")
    prof = executor.profile_table(m["table"])
    return {"dataset_id": dataset_id, "name": m["name"],
            "row_count": prof["row_count"], "columns": prof["columns"]}


@app.post("/api/query", dependencies=[Depends(rate_limit)])
async def query(body: QueryIn):
    events = []
    try:
        result = await asyncio.to_thread(
            pipeline.run_pipeline, body.dataset_id, body.question,
            events.append)
        return result
    except KeyError as e:
        raise HTTPException(404, str(e))
    except pipeline.PipelineError as e:
        return JSONResponse(status_code=422, content={
            "detail": str(e), "sql_attempts": e.attempts})
    except Exception as e:  # noqa: BLE001
        log.exception("query failed")
        raise HTTPException(500, f"internal error: {str(e)[:200]}")


@app.post("/api/query/stream", dependencies=[Depends(rate_limit)])
async def query_stream(body: QueryIn):
    q = asyncio.Queue()

    def emit(event: dict):
        q.put_nowait(event)

    async def run():
        try:
            await asyncio.to_thread(pipeline.run_pipeline, body.dataset_id,
                                    body.question, emit)
        except KeyError as e:
            emit({"type": "error", "error": str(e), "status": 404})
        except pipeline.PipelineError as e:
            emit({"type": "error", "error": str(e), "status": 422,
                  "sql_attempts": e.attempts})
        except Exception as e:  # noqa: BLE001
            log.exception("stream query failed")
            emit({"type": "error", "error": str(e)[:300], "status": 500})
        finally:
            q.put_nowait(None)

    task = asyncio.create_task(run())

    async def gen():
        try:
            while True:
                ev = await q.get()
                if ev is None:
                    break
                yield {"event": ev.get("type", "message"),
                       "data": json.dumps(ev, default=str)}
        finally:
            if not task.done():
                task.cancel()

    return EventSourceResponse(gen())


@app.get("/api/query")
def list_queries(limit: int = Query(50, ge=1, le=100)):
    """Recent query history, newest first, from the in-memory replay cache
    (entries expire with it). Includes qid, question, SQL, attempts, timing."""
    names = {m["dataset_id"]: m["name"] for m in store.list_all()}
    items = []
    for it in cache.list_recent(limit):
        it = dict(it)
        it["dataset"] = names.get(it.get("dataset_id"), it.get("dataset_id"))
        items.append(it)
    return items


@app.get("/api/query/{qid}")
def replay(qid: str):
    result = cache.get_qid(qid)
    if result is None:
        raise HTTPException(404, f"unknown or expired query id: {qid}")
    return result


@app.get("/")
def index():
    return FileResponse(os.path.join(config.STATIC_DIR, "index.html"))


# Pages (clean URLs, no .html). Static assets live under /static/.
app.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")


def _page(fname: str) -> FileResponse:
    return FileResponse(os.path.join(config.STATIC_DIR, fname),
                        media_type="text/html")


@app.get("/demo", include_in_schema=False)
def demo_page():
    return _page("demo.html")


@app.get("/data", include_in_schema=False)
def data_page():
    return _page("data.html")


@app.get("/history", include_in_schema=False)
def history_page():
    return _page("history.html")


@app.get("/about", include_in_schema=False)
def about_page():
    return _page("about.html")
