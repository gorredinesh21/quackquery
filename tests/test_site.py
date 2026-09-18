"""New-site features: dataset profile endpoint, query history endpoint,
and the 5 HTML pages (clean URLs) + static assets mount."""
import io
import re

import pytest


# ---------------------------------------------------------------- profile

def test_profile_shape(client, demos):
    r = client.get(f"/api/datasets/{demos['ecommerce_orders']}/profile")
    assert r.status_code == 200
    body = r.json()
    assert body["dataset_id"] == demos["ecommerce_orders"]
    assert body["name"] == "ecommerce_orders"
    assert body["row_count"] == 420
    cols = {c["name"]: c for c in body["columns"]}
    assert set(cols) >= {"order_id", "payment_method", "revenue_inr", "status"}
    pm = cols["payment_method"]
    assert pm["type"].upper().startswith("VARCHAR")
    assert pm["nulls"] == 0
    assert pm["distinct"] == 5          # 5 payment methods
    assert isinstance(pm["top_values"], list) and pm["top_values"]
    assert len(pm["top_values"]) <= 8   # top-values cap
    tv = pm["top_values"][0]
    assert set(tv) == {"value", "count"}
    # counts are descending and, with 5 distinct values, cover every row
    counts = [t["count"] for t in pm["top_values"]]
    assert counts == sorted(counts, reverse=True)
    assert sum(counts) == 420
    # high-cardinality column gets no top values
    assert cols["order_id"]["distinct"] == 420
    assert cols["order_id"]["top_values"] is None


def test_profile_nulls_counted(client):
    csv = b"city,pop\nPune,7m\nIndore,\n,,\n"
    up = client.post("/api/datasets", files={
        "file": ("nulls.csv", io.BytesIO(csv), "text/csv")})
    assert up.status_code == 200
    ds = up.json()["dataset_id"]
    body = client.get(f"/api/datasets/{ds}/profile").json()
    cols = {c["name"]: c for c in body["columns"]}
    assert body["row_count"] == 3
    assert cols["city"]["nulls"] == 1
    assert cols["pop"]["nulls"] == 2
    assert cols["city"]["distinct"] == 2


def test_profile_unknown_dataset_404(client):
    assert client.get("/api/datasets/zzz/profile").status_code == 404


# ---------------------------------------------------------------- history

def test_query_history_lists_newest_first(client, demos):
    q = {"dataset_id": demos["ipl_matches"],
         "question": "history probe: total matches per season"}
    res = client.post("/api/query", json=q).json()
    hist = client.get("/api/query").json()
    assert isinstance(hist, list) and hist
    newest = hist[0]
    assert newest["qid"] == res["qid"]
    assert newest["question"] == q["question"]
    assert newest["dataset"] == "ipl_matches"
    assert newest["dataset_id"] == q["dataset_id"]
    assert newest["sql"].upper().startswith("SELECT")
    assert newest["attempts"] == res["attempts"]
    assert newest["timing_ms"] >= 0
    assert newest["row_count"] == res["row_count"]
    assert newest["answer"]
    assert isinstance(newest["ts"], float)  # timestamp present
    # newest first: timestamps non-increasing
    tss = [h["ts"] for h in hist]
    assert tss == sorted(tss, reverse=True)


def test_query_history_limit(client, demos):
    for i in range(3):
        r = client.post("/api/query", json={
            "dataset_id": demos["ipl_matches"],
            "question": f"history limit probe {i}"})
        assert r.status_code == 200
    hist = client.get("/api/query?limit=2").json()
    assert len(hist) == 2
    assert hist[0]["question"] == "history limit probe 2"


@pytest.mark.parametrize("bad", ["0", "-1", "999", "abc"])
def test_query_history_limit_validation(client, bad):
    assert client.get(f"/api/query?limit={bad}").status_code == 422


# ---------------------------------------------------------------- pages

PAGES = {
    "/": ("QuackQuery", "How it works", "Why trust it"),
    "/demo": ("Try it", "Pipeline", "upload your own CSV"),
    "/data": ("Data profile", "Load demo datasets", "Refresh list"),
    "/history": ("Query history", "newest first", "Refresh"),
    "/about": ("How QuackQuery is built", "The pipeline", "Cloud Run"),
}

_NAV_LABEL_BY_PATH = {"Overview": "/", "Try it": "/demo", "Data": "/data",
                      "History": "/history", "About": "/about"}


@pytest.mark.parametrize("path", list(PAGES))
def test_pages_served_clean_urls(client, path):
    r = client.get(path)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    for marker in PAGES[path]:
        assert marker in r.text, f"{marker!r} missing from {path}"


@pytest.mark.parametrize("path", list(PAGES))
def test_pages_distinct_and_share_layout(client, path):
    r = client.get(path)
    # every page has the sidebar, the full nav, and the shared stylesheet
    assert 'class="sidebar"' in r.text
    for label, href in _NAV_LABEL_BY_PATH.items():
        assert f'href="{href}"' in r.text, f"nav link {href} missing from {path}"
    assert "/static/style.css" in r.text
    # exactly one active nav entry, and it is this page
    assert r.text.count('class="active"') == 1
    m = re.search(r'<a href="[^"]*" class="active">.*?</svg>\s*([^<]+?)</a>',
                  r.text, re.S)
    assert m, f"no active nav label found on {path}"
    assert _NAV_LABEL_BY_PATH[m.group(1).strip()] == path
    # distinct <title> per page
    t = re.search(r"<title>(.*?)</title>", r.text).group(1)
    assert t


def test_static_assets_mounted(client):
    r = client.get("/static/style.css")
    assert r.status_code == 200
    assert "text/css" in r.headers["content-type"]
    for js in ("demo.js", "data.js", "history.js"):
        assert client.get(f"/static/{js}").status_code == 200
