"""API integration: health, datasets, query (happy + repair + guard),
SSE streaming, replay cache, rate limit — all with the mock LLM."""
import io
import json

from app import config, llm as llm_mod, main as main_mod
from app.llm import MockLLM


def test_healthz(client):
    r = client.get("/api/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["llm"] == "mock"
    assert body["duckdb"] is True
    assert body["datasets"] >= 3


def test_demo_load_idempotent(client, demos):
    r = client.get("/api/datasets/demo")
    again = {d["name"]: d["dataset_id"] for d in r.json()}
    assert again == demos  # same ids — no duplicate registration


def test_dataset_lifecycle(client):
    csv = b"city,pop\nPune,7m\nIndore,3m\n"
    up = client.post("/api/datasets",
                     files={"file": ("cities.csv", io.BytesIO(csv), "text/csv")})
    assert up.status_code == 200
    ds_id = up.json()["dataset_id"]

    lst = client.get("/api/datasets").json()
    assert any(d["dataset_id"] == ds_id for d in lst)

    one = client.get(f"/api/datasets/{ds_id}")
    assert one.status_code == 200
    assert one.json()["row_count"] == 2

    assert client.get("/api/datasets/nope").status_code == 404


def test_query_happy_path(client, demos):
    r = client.post("/api/query", json={
        "dataset_id": demos["ecommerce_orders"],
        "question": "how many orders per payment method"})
    assert r.status_code == 200
    body = r.json()
    assert body["attempts"] == 1
    assert body["sql"].upper().startswith("SELECT")
    assert body["row_count"] == 5
    assert [row[0] for row in body["rows"]][0] == "COD"
    assert body["chart"]["mark"] == "bar"
    assert body["answer"]
    # replay by qid
    rr = client.get(f"/api/query/{body['qid']}")
    assert rr.status_code == 200
    assert rr.json()["qid"] == body["qid"]


def test_query_unknown_dataset(client):
    r = client.post("/api/query", json={"dataset_id": "zzz", "question": "hello?"})
    assert r.status_code == 404


def test_query_result_cached(client, demos):
    q = {"dataset_id": demos["ipl_matches"], "question": "unique cached q 42"}
    first = client.post("/api/query", json=q).json()
    assert first["cached"] is False
    second = client.post("/api/query", json=q).json()
    assert second["cached"] is True
    assert second["rows"] == first["rows"]


def _mock() -> MockLLM:
    m = llm_mod.get_llm()
    assert isinstance(m, MockLLM)
    m.script.clear()
    return m


def test_self_correction_exec_error(client, demos):
    """Bad SQL first, corrected SQL second — the repair loop recovers."""
    m = _mock()
    m.queue("SELECT no_such_column FROM data",
            "SELECT COUNT(*) AS value FROM data")
    r = client.post("/api/query", json={
        "dataset_id": demos["ipl_matches"],
        "question": "how many matches in total"})
    assert r.status_code == 200
    body = r.json()
    assert body["attempts"] == 2
    assert len(body["sql_attempts"]) == 2
    assert body["row_count"] == 1
    assert body["rows"][0][0] == 208


def test_self_correction_guard_rejection_then_success(client, demos):
    m = _mock()
    m.queue("DROP TABLE data", "SELECT 1 AS value")
    r = client.post("/api/query", json={
        "dataset_id": demos["ecommerce_orders"], "question": "select one"})
    assert r.status_code == 200
    assert r.json()["attempts"] == 2  # guard rejected DROP, retry succeeded


def test_all_attempts_exhausted_422(client, demos):
    """UPDATE/DELETE never runs; after 4 attempts the API returns 422 with
    the SQL attempt trail."""
    m = _mock()
    m.queue(*["UPDATE data SET x = 1"] * 4)
    r = client.post("/api/query", json={
        "dataset_id": demos["ecommerce_orders"],
        "question": "update everything"})
    assert r.status_code == 422
    body = r.json()
    assert len(body["sql_attempts"]) == 4
    assert all(s.lower().startswith("update") for s in body["sql_attempts"])


def test_sse_stream_event_sequence(client, demos):
    """SSE emits stage -> sql -> rows -> chart -> answer -> done, in order."""
    events = []
    with client.stream("POST", "/api/query/stream", json={
            "dataset_id": demos["indian_startup_funding"],
            "question": "sse: how many startups per round"}) as r:
        assert r.status_code == 200
        assert "text/event-stream" in r.headers["content-type"]
        for line in r.iter_lines():
            if line.startswith("data:"):
                events.append(json.loads(line[5:]))

    types = [e["type"] for e in events]
    assert types[0] == "stage"
    assert "sql" in types
    assert "rows" in types
    assert "chart" in types
    assert "answer" in types
    assert types[-1] == "done"
    # order sanity
    assert types.index("sql") < types.index("rows") < types.index("done")

    sql_ev = next(e for e in events if e["type"] == "sql")
    assert sql_ev["sql"].upper().startswith("SELECT")
    rows_ev = next(e for e in events if e["type"] == "rows")
    assert rows_ev["row_count"] == 5
    done_ev = events[-1]
    assert done_ev["qid"]

    # replay the qid non-streaming
    rr = client.get(f"/api/query/{done_ev['qid']}")
    assert rr.status_code == 200


def test_sse_cached_second_call(client, demos):
    q = {"dataset_id": demos["ipl_matches"],
         "question": "sse cached: which team won the most matches"}
    with client.stream("POST", "/api/query/stream", json=q) as r:
        first = [json.loads(l[5:]) for l in r.iter_lines()
                 if l.startswith("data:")]
    with client.stream("POST", "/api/query/stream", json=q) as r:
        second = [json.loads(l[5:]) for l in r.iter_lines()
                  if l.startswith("data:")]
    assert [e["type"] for e in first][-1] == "done"
    done = second[-1]
    assert done["cached"] is True
    assert "sql" not in [e["type"] for e in second]  # cache short-circuit


def test_sse_error_event(client, demos):
    m = _mock()
    m.queue(*["DELETE FROM data"] * 4)
    with client.stream("POST", "/api/query/stream", json={
            "dataset_id": demos["ipl_matches"],
            "question": "delete everything now"}) as r:
        events = [json.loads(l[5:]) for l in r.iter_lines()
                  if l.startswith("data:")]
    assert events[-1]["type"] == "error"
    assert events[-1]["status"] == 422
    assert len(events[-1]["sql_attempts"]) == 4


def test_rate_limit(client, demos):
    """Rate limit applies to query endpoints (healthz stays unlimited)."""
    main_mod._hits.clear()
    old = config.RATE_LIMIT_RPM
    config.RATE_LIMIT_RPM = 2
    try:
        codes = []
        for _ in range(3):
            codes.append(client.post("/api/query", json={
                "dataset_id": demos["ipl_matches"],
                "question": "rate limit probe"}).status_code)
        assert codes[0] == 200
        assert codes[1] == 200
        assert codes[2] == 429
    finally:
        config.RATE_LIMIT_RPM = old
        main_mod._hits.clear()


def test_request_id_header(client):
    r = client.get("/api/healthz")
    assert r.headers.get("X-Request-ID")
    r2 = client.get("/api/healthz", headers={"X-Request-ID": "fixed-rid-1"})
    assert r2.headers["X-Request-ID"] == "fixed-rid-1"


def test_landing_page(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "QuackQuery" in r.text
