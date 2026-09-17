"""Catalog: schema inference + samples via the API upload path."""
import io

CSV = b"""id,name,price,ordered_on,notes
1,Chai,12.5,2024-01-02,ok
2,Coffee,20.0,2024-01-03,ok
3,Biscuit,5.25,2024-01-04,fine
4,Cake,30.0,2024-01-05,ok
"""


def _upload(client):
    r = client.post("/api/datasets",
                    files={"file": ("types.csv", io.BytesIO(CSV), "text/csv")})
    assert r.status_code == 200
    return r.json()


def test_type_inference(client):
    out = _upload(client)
    schema = {c["name"]: c for c in out["schema"]["columns"]}
    assert schema["id"]["type"].upper().startswith(("BIGINT", "INTEGER", "INT"))
    assert schema["id"]["numeric"] is True
    assert schema["price"]["numeric"] is True
    assert "DOUBLE" in schema["price"]["type"].upper()
    assert schema["ordered_on"]["temporal"] is True
    assert schema["name"]["numeric"] is False
    assert schema["name"]["type"].upper().startswith("VARCHAR")


def test_samples_and_descriptions(client):
    out = _upload(client)
    cols = {c["name"]: c for c in out["schema"]["columns"]}
    assert set(cols["name"]["samples"]) == {"Chai", "Coffee", "Biscuit", "Cake"}
    for c in cols.values():
        assert c["description"]  # mock mode: rule-based, always present


def test_upload_rejects_non_csv(client):
    r = client.post("/api/datasets",
                    files={"file": ("x.txt", b"a,b\n1,2", "text/plain")})
    assert r.status_code == 400


def test_upload_rejects_empty(client):
    r = client.post("/api/datasets",
                    files={"file": ("x.csv", b"   ", "text/csv")})
    assert r.status_code == 400
