"""Shared fixtures. QUACK_LLM=mock + temp data dir BEFORE any app import so
every test runs with zero API calls and full isolation."""
import os
import tempfile

os.environ["QUACK_LLM"] = "mock"
os.environ.setdefault("QUACK_DATA_DIR", tempfile.mkdtemp(prefix="qq_test_"))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import main as app_main  # noqa: E402


@pytest.fixture(scope="session")
def client():
    with TestClient(app_main.app) as c:
        yield c


@pytest.fixture(scope="session", autouse=True)
def demos(client):
    """The 3 bundled demo datasets, loaded once (idempotent endpoint).
    autouse so test file ordering never matters."""
    r = client.get("/api/datasets/demo")
    assert r.status_code == 200
    out = {d["name"]: d["dataset_id"] for d in r.json()}
    assert len(out) == 3
    return out
