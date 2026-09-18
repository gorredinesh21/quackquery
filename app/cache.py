"""Question-hash result cache: LRU with TTL (default 30 min).

Key = sha1(dataset_id + normalized question). Good enough at demo scale;
single-instance, in-memory — documented as such.
"""
import hashlib
import threading
import time
from collections import OrderedDict

from . import config


class TTLCache:
    def __init__(self, max_entries: int = config.CACHE_MAX_ENTRIES,
                 ttl_s: int = config.CACHE_TTL_S):
        self.max_entries = max_entries
        self.ttl_s = ttl_s
        self._data: OrderedDict = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    @staticmethod
    def key(dataset_id: str, question: str) -> str:
        norm = " ".join(question.lower().split())
        return hashlib.sha1(f"{dataset_id}|{norm}".encode()).hexdigest()

    def get(self, k: str):
        with self._lock:
            item = self._data.get(k)
            if item is None:
                self.misses += 1
                return None
            expires, value = item
            if time.time() > expires:
                del self._data[k]
                self.misses += 1
                return None
            self._data.move_to_end(k)
            self.hits += 1
            return value

    def put(self, k: str, value) -> None:
        with self._lock:
            self._data[k] = (time.time() + self.ttl_s, value)
            self._data.move_to_end(k)
            while len(self._data) > self.max_entries:
                self._data.popitem(last=False)

    def stats(self) -> dict:
        with self._lock:
            return {"entries": len(self._data), "ttl_s": self.ttl_s,
                    "hits": self.hits, "misses": self.misses}


result_cache = TTLCache()
query_results: dict = {}   # qid -> cached full result (for GET /api/query/{qid})
_history: list = []        # newest last: [{qid, ts}] — timestamp ledger for GET /api/query
_query_lock = threading.Lock()


def remember_qid(qid: str, result: dict) -> None:
    with _query_lock:
        if len(query_results) >= config.CACHE_MAX_ENTRIES:
            query_results.pop(next(iter(query_results)))
        query_results[qid] = result
        _history.append({"qid": qid, "ts": time.time()})
        if len(_history) > config.CACHE_MAX_ENTRIES:
            del _history[:len(_history) - config.CACHE_MAX_ENTRIES]


def get_qid(qid: str) -> dict | None:
    with _query_lock:
        return query_results.get(qid)


def list_recent(limit: int = 50) -> list:
    """Recent query history, newest first. Skips entries whose full result
    has been evicted from the replay cache. Summary fields only."""
    out = []
    with _query_lock:
        for rec in reversed(_history):
            r = query_results.get(rec["qid"])
            if r is None:
                continue
            out.append({
                "qid": r.get("qid", rec["qid"]),
                "ts": rec["ts"],
                "dataset_id": r.get("dataset_id"),
                "question": r.get("question"),
                "sql": r.get("sql"),
                "attempts": r.get("attempts"),
                "timing_ms": r.get("timing_ms"),
                "row_count": r.get("row_count"),
                "answer": r.get("answer"),
            })
            if len(out) >= limit:
                break
    return out
