"""Dataset registry: metadata + catalogs persisted to a JSON file next to
the DuckDB database. Small, restartable, zero infra.

(Swap-in point for GCS later: same dict shape, different backend.)
"""
import json
import logging
import os
import threading
import time
import uuid

from . import config

log = logging.getLogger("quackquery.store")

_lock = threading.Lock()
_datasets: dict | None = None   # dataset_id -> metadata dict


def _path() -> str:
    return os.path.join(config.DATA_DIR, "datasets.json")


def _load() -> dict:
    global _datasets
    if _datasets is None:
        os.makedirs(config.DATA_DIR, exist_ok=True)
        if os.path.exists(_path()):
            try:
                with open(_path()) as f:
                    _datasets = json.load(f)
            except (json.JSONDecodeError, OSError):
                log.warning("datasets.json corrupt; starting fresh")
                _datasets = {}
        else:
            _datasets = {}
    return _datasets


def _flush() -> None:
    tmp = _path() + ".tmp"
    with open(tmp, "w") as f:
        json.dump(_datasets, f, indent=1, default=str)
    os.replace(tmp, _path())


def new_dataset_id() -> str:
    return uuid.uuid4().hex[:12]


def put(meta: dict) -> dict:
    with _lock:
        ds = _load()
        ds[meta["dataset_id"]] = meta
        _flush()
    return meta


def get(dataset_id: str) -> dict | None:
    return _load().get(dataset_id)


def exists(dataset_id: str) -> bool:
    return dataset_id in _load()


def list_all() -> list:
    return sorted(_load().values(), key=lambda m: m.get("created_at", 0), reverse=True)


def stats() -> dict:
    ds = _load()
    return {"datasets": len(ds),
            "tables": sorted({m["table"] for m in ds.values()})}


def make_meta(name: str, table: str, row_count: int, catalog: dict,
              source: str = "upload") -> dict:
    return {
        "dataset_id": new_dataset_id(),
        "name": name,
        "table": table,
        "row_count": row_count,
        "source": source,
        "created_at": time.time(),
        "catalog": catalog,
    }
