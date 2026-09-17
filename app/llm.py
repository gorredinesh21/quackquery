"""LLM layer: text-to-SQL, repair, answer+chart.

Two interchangeable providers:

- VertexLLM  — Gemini on Vertex AI, reusing the proven support-copilot auth
   pattern: metadata-server token on GCP, `gcloud auth print-access-token`
   locally, 429 backoff honoring Retry-After. Plain REST via `requests`,
   no SDK weight.
- MockLLM    — deterministic, zero-API rule-based generator (QUACK_LLM=mock)
   so tests, CI, and offline demos never need credentials.

Both expose: generate_sql / repair_sql / answer_and_chart / describe_columns.
"""
import json
import logging
import random
import re
import subprocess
import time

import requests

from . import config

log = logging.getLogger("quackquery.llm")

# --------------------------------------------------------------------------
# Prompts
# --------------------------------------------------------------------------

SQL_RULES = """Rules:
- Output ONE read-only DuckDB SQL statement. SELECT or WITH only.
- No semicolons, no comments, no PRAGMA, no file functions (read_csv etc).
- Use exactly the column names given; the table alias is "data".
- Round money aggregates to 2 decimals when it makes sense.
- Reply with the SQL statement ONLY — no prose, no markdown fences."""

SQL_PROMPT = """You are an expert DuckDB analyst. Turn the question into SQL.

{schema}

Question: {question}

{rules}

SQL:"""

REPAIR_PROMPT = """You are an expert DuckDB analyst. Your SQL failed.

{schema}

Question: {question}

Broken SQL:
{sql}

Error:
{error}

Fix the SQL. {rules}

Corrected SQL:"""

DESCRIBE_PROMPT = """For each table column below, write a one-line description
(what it measures, its unit if any). Reply as a JSON array of strings, same
order, no prose.

Table columns:
{columns}"""

ANSWER_PROMPT = """You ran this SQL against a dataset to answer a business question:

Question: {question}
SQL: {sql}
Result (up to 20 rows): {rows}

Reply with ONLY a JSON object:
{{"answer": "1-3 sentence plain-English answer citing concrete numbers from the result",
  "chart": {{"mark": "bar"|"line"|"point", "x": "<column>", "y": "<column>"}} | null}}
The chart key must pick the two most chart-worthy columns, or be null if the
result is not chartable. No markdown fences."""


# --------------------------------------------------------------------------
# VertexLLM (copied/adapted from support-copilot — same auth + backoff)
# --------------------------------------------------------------------------

class LLMError(RuntimeError):
    pass


def _fetch_token() -> str:
    """Access token: metadata server on GCP, gcloud CLI locally."""
    try:
        r = requests.get(
            "http://metadata.google.internal/computeMetadata/v1/instance/"
            "service-accounts/default/token",
            headers={"Metadata-Flavor": "Google"}, timeout=3)
        if r.status_code == 200:
            return r.json()["access_token"]
    except requests.RequestException:
        pass
    out = subprocess.run(["gcloud", "auth", "print-access-token"],
                         capture_output=True, text=True, timeout=30)
    if out.returncode != 0:
        raise LLMError(f"gcloud auth failed: {out.stderr[:200]}")
    return out.stdout.strip()


_token_cache = {"tok": None, "exp": 0.0}


def _auth_header() -> dict:
    now = time.time()
    if _token_cache["tok"] is None or now > _token_cache["exp"]:
        _token_cache["tok"] = _fetch_token()
        _token_cache["exp"] = now + 45 * 60  # refresh well before expiry
    return {"Authorization": f"Bearer {_token_cache['tok']}"}


class VertexLLM:
    """Gemini via Vertex AI REST. One call = one generateContent POST with
    429/5xx Retry-After backoff (max 4 attempts)."""

    def __init__(self, model: str = config.LLM_MODEL,
                 project: str = config.GCP_PROJECT,
                 region: str = config.GCP_REGION):
        self.model, self.project, self.region = model, project, region

    def _call(self, prompt: str, temperature: float = 0.1,
              max_tokens: int = 2048) -> str:
        url = (f"https://{self.region}-aiplatform.googleapis.com/v1/projects/"
               f"{self.project}/locations/{self.region}/publishers/google/"
               f"models/{self.model}:generateContent")
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
                # gemini-2.5 is a thinking model; SQL/JSON tasks don't need
                # it and thinking tokens can eat the whole budget (observed:
                # MAX_TOKENS with zero visible parts)
                "thinkingConfig": {"thinkingBudget": 0},
            },
        }
        r = None
        for attempt in range(1, config.LLM_MAX_ATTEMPTS + 1):
            r = requests.post(url, headers=_auth_header(), json=payload,
                              timeout=config.LLM_TIMEOUT_S)
            if r.status_code in (429, 500, 503):
                wait = int(r.headers.get("Retry-After", "20")) + (attempt - 1) * 5
                log.warning("LLM %s; backing off %ss (attempt %s)",
                            r.status_code, wait, attempt)
                time.sleep(wait)
                continue
            break
        if r is None or r.status_code != 200:
            raise LLMError(f"LLM API {getattr(r, 'status_code', '?')}: "
                           f"{getattr(r, 'text', '')[:200]}")
        try:
            cand = r.json()["candidates"][0]
            for part in cand.get("content", {}).get("parts", []):
                if "text" in part:
                    return part["text"].strip()
            raise LLMError(
                f"LLM returned no text (finishReason="
                f"{cand.get('finishReason', '?')}); raise maxOutputTokens")
        except (KeyError, IndexError) as e:
            raise LLMError(f"unexpected LLM response shape: {e}")

    # -- pipeline API (catalog kwarg ignored here; MockLLM heuristics need it) --

    def generate_sql(self, question: str, schema_block: str,
                     catalog: dict | None = None) -> str:
        raw = self._call(SQL_PROMPT.format(schema=schema_block, question=question,
                                           rules=SQL_RULES))
        return _extract_sql(raw)

    def repair_sql(self, question: str, schema_block: str, sql: str,
                   error: str, catalog: dict | None = None) -> str:
        raw = self._call(REPAIR_PROMPT.format(schema=schema_block,
                                              question=question, sql=sql,
                                              error=error[:500], rules=SQL_RULES))
        return _extract_sql(raw)

    def describe_columns(self, columns: list) -> list:
        cols = "\n".join(f'- "{c["name"]}" {c["type"]}; e.g. '
                         f"{c['samples'][:3]}" for c in columns)
        raw = self._call(DESCRIBE_PROMPT.format(columns=cols),
                         temperature=0.0, max_tokens=1024)
        try:
            m = re.search(r"\[.*\]", raw, re.S)
            vals = json.loads(m.group(0)) if m else []
            return [str(v) for v in vals][:len(columns)]
        except json.JSONDecodeError:
            return []

    def answer_and_chart(self, question: str, sql: str, columns: list,
                         rows: list) -> dict:
        raw = self._call(ANSWER_PROMPT.format(
            question=question, sql=sql, columns=columns,
            rows=json.dumps([list(r) for r in rows[:20]], default=str)),
            temperature=0.2)
        try:
            m = re.search(r"\{.*\}", raw, re.S)
            parsed = json.loads(m.group(0)) if m else {}
            return {"answer": parsed.get("answer", ""), "chart": parsed.get("chart")}
        except json.JSONDecodeError:
            return {"answer": raw[:500], "chart": None}


def _extract_sql(raw: str) -> str:
    """Pull SQL out of a model reply (tolerates ```sql fences and prose)."""
    m = re.search(r"```(?:sql)?\s*(.+?)```", raw, re.S | re.I)
    if m:
        return m.group(1).strip()
    return raw.strip()


# --------------------------------------------------------------------------
# MockLLM — deterministic rule-based generator (QUACK_LLM=mock)
# --------------------------------------------------------------------------

_STOPWORDS = {"the", "a", "an", "of", "in", "on", "for", "by", "per", "each",
              "what", "which", "who", "how", "many", "much", "is", "are",
              "was", "were", "did", "do", "does", "top", "and", "with",
              "had", "has", "have", "most", "least", "all", "show", "me",
              "give", "list", "tell", "about", "from", "that", "this"}


class MockLLM:
    """Tiny deterministic text-to-SQL: keyword heuristics over the catalog.

    Supports `script` — a list of canned responses popped in order — so tests
    can drive the self-correction loop (bad SQL first, fix second).
    """

    def __init__(self):
        self.script: list = []          # test hook: queued raw SQL replies
        self.calls: list = []           # audit trail of prompt kinds

    # -- test hooks ---------------------------------------------------------
    def queue(self, *raw_sqls: str) -> None:
        self.script.extend(raw_sqls)

    def _next_scripted(self) -> str | None:
        return self.script.pop(0) if self.script else None

    # -- pipeline API -------------------------------------------------------
    def generate_sql(self, question: str, schema_block: str,
                     catalog: dict | None = None) -> str:
        self.calls.append(("generate", question))
        canned = self._next_scripted()
        if canned is not None:
            return canned
        return mock_sql(question, catalog or {})

    def repair_sql(self, question: str, schema_block: str, sql: str,
                   error: str, catalog: dict | None = None) -> str:
        self.calls.append(("repair", question, error))
        canned = self._next_scripted()
        if canned is not None:
            return canned
        # Deterministic "repair": ask for the plain fallback shape.
        return mock_sql(question, catalog or {}, fallback_safe=True)

    def describe_columns(self, columns: list) -> list:
        return [f"{c['name']} ({c['type']})" for c in columns]

    def answer_and_chart(self, question: str, sql: str, columns: list,
                         rows: list) -> dict:
        n = len(rows)
        head = ", ".join(f"{c}={r}" for c, r in zip(columns, rows[0])) if rows else "no rows"
        chart = heuristic_chart(columns, rows)
        return {"answer": f"Query returned {n} row(s); first row: {head}.",
                "chart": chart}


# --------------------------------------------------------------------------
# Mock heuristics
# --------------------------------------------------------------------------

# question word -> column base name it usually means
ALIASES = {
    "winner": ["won", "wins", "victories", "champion"],
    "match_id": ["matches", "match", "games"],
    "amount_cr_inr": ["funding", "raised", "investment", "deal"],
    "revenue_inr": ["revenue", "sales", "turnover"],
    "order_id": ["orders", "order"],
    "startup_name": ["startups", "startup", "companies", "company"],
    "lead_investor": ["investor", "investors"],
    "player_of_match": ["player", "players", "potm"],
    "win_margin": ["margin", "margin_of_victory"],
}

_FILLERS = {"each", "the", "a", "an", "every", "all", "of", "was", "were",
            "many", "much"}
# words that describe aggregation, not the grouping dimension
_PHRASE_STOP = _FILLERS | {"total", "sum", "average", "avg", "number",
                           "count", "highest", "lowest", "most", "least",
                           "top", "overall", "maximum", "minimum"}


def _cols(catalog: dict) -> list:
    return catalog.get("columns", [])


def _is_id_like(name: str) -> bool:
    n = name.lower()
    return n.endswith("_id") or n in ("id", "uuid")


def _singular(w: str) -> str:
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"     # categories -> category
    if len(w) > 3 and w.endswith("es"):
        return w[:-2]           # matches -> match
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]           # sectors -> sector
    return w


def _column_matches_phrase(col: dict, phrase_words: list) -> bool:
    """Does a column match a 1-3 word phrase (plural-tolerant)?
    Loose shared-token matches are NOT enough: either the full name/base or
    an alias word appears, or EVERY token of the column name appears."""
    name = col["name"].lower()
    base = re.sub(r"\d+$", "", name)
    tokens = {t for t in name.split("_") if len(t) >= 3}
    cover = set(phrase_words) | {_singular(w) for w in phrase_words}
    names = {name, base}
    aliases = set(ALIASES.get(name, [])) | set(ALIASES.get(base, []))
    if names & cover or aliases & cover:
        return True
    return bool(tokens) and tokens <= cover


def _mentioned(question: str, catalog: dict) -> list:
    """Catalog columns referenced by the question, in QUESTION order."""
    q = " " + re.sub(r"[^a-z0-9_ ]", " ", question.lower()) + " "
    hits = []
    for c in _cols(catalog):
        name = c["name"].lower()
        base = re.sub(r"\d+$", "", name)
        tokens = [t for t in name.split("_") if len(t) >= 3]
        aliases = ALIASES.get(name, []) + ALIASES.get(base, [])
        patterns = {name, base, *tokens, *aliases}
        # plural forms: category -> categories, match -> matches
        patterns |= {p + "s" for p in patterns}
        patterns |= {p[:-1] + "ies" for p in patterns if p.endswith("y")}
        pos = -1
        for p in patterns:
            if len(p) < 3:
                continue
            m = re.search(rf"\b{re.escape(p)}\b", q)
            if m and (pos == -1 or m.start() < pos):
                pos = m.start()
        if pos >= 0:
            hits.append((pos, c))
    return [c for _, c in sorted(hits, key=lambda t: t[0])]


def _resolve_dim(question: str, catalog: dict, exclude: set):
    """Pick the GROUP BY column: 'per/by X' phrase, then 'top N X' phrase,
    then the last mentioned non-id column."""
    q = question.lower()

    def try_phrase(phrase: str):
        words = [w for w in phrase.split() if w not in _PHRASE_STOP]
        for take in (3, 2, 1):
            if len(words) < take:
                continue
            sub = words[:take]
            for c in _cols(catalog):
                if (c["name"] not in exclude
                        and not _is_id_like(c["name"])
                        and _column_matches_phrase(c, sub)):
                    return c
        return None

    m = re.search(r"\b(?:per|by|in each|for each)\s+([a-z0-9_ ]{2,40})", q)
    if m:
        dim = try_phrase(m.group(1))
        if dim is not None:
            return dim
    m = re.search(r"\btop\s+\d+\s+([a-z0-9_ ]{2,40}?)(?=\s+by\b|\s+with\b|$| [?.])", q)
    if m:
        dim = try_phrase(m.group(1))
        if dim is not None:
            return dim
    for c in reversed(_mentioned(question, catalog)):
        if c["name"] not in exclude and not _is_id_like(c["name"]):
            return c
    return None


def mock_sql(question: str, catalog: dict, fallback_safe: bool = False) -> str:
    """Deterministic rule-based question -> SQL for mock mode."""
    q = question.lower()
    cols = _cols(catalog)
    if fallback_safe or not cols:
        return "SELECT COUNT(*) AS value FROM data"

    counting = bool(re.search(r"\bhow many\b|\bcount\b|\bnumber of\b", q))
    averaging = bool(re.search(r"\baverage\b|\bavg\b|\bmean\b", q))
    aggregating = averaging or bool(
        re.search(r"\btotal\b|\bsum\b|\bcombined\b|\boverall\b", q))
    desc = not bool(re.search(r"\blowest\b|\bleast\b|\bsmallest\b|\bminimum\b|\bworst\b", q))
    m = re.search(r"\btop\s+(\d+)", q)
    limit = int(m.group(1)) if m else (
        1 if re.search(r"\bmost\b|\bhighest\b|\blargest\b|\bbiggest\b|\bbest\b"
                       r"|\bmaximum\b|\bmax\b", q) else 5)
    order = "DESC" if desc else "ASC"

    # metric only counts when the question aggregates (total/sum/average ...)
    metric = None
    if aggregating:
        metric = next((c for c in _mentioned(question, catalog)
                       if c["numeric"] and not _is_id_like(c["name"])), None)
        if metric is None:
            metric = next((c for c in cols
                           if c["numeric"] and not _is_id_like(c["name"])), None)

    exclude = {metric["name"]} if metric else set()
    dim = _resolve_dim(question, catalog, exclude)

    if not aggregating or metric is None:
        # counting mode
        if dim is not None:
            return (f'SELECT "{dim["name"]}", COUNT(*) AS value FROM data '
                    f'GROUP BY "{dim["name"]}" ORDER BY value {order} '
                    f'NULLS LAST LIMIT {limit}')
        return "SELECT COUNT(*) AS value FROM data"

    agg = "AVG" if averaging else "SUM"
    if dim is None:
        return (f'SELECT ROUND({agg}("{metric["name"]}")::DOUBLE, 2) AS value '
                f'FROM data')
    return (f'SELECT "{dim["name"]}", '
            f'ROUND({agg}("{metric["name"]}")::DOUBLE, 2) AS value FROM data '
            f'GROUP BY "{dim["name"]}" ORDER BY value {order} '
            f'NULLS LAST LIMIT {limit}')


def heuristic_chart(columns: list, rows: list) -> dict | None:
    """Pick a vega-lite-lite spec from result shape: bar for categorical x,
    line for temporal x, else none."""
    if len(rows) < 2 or len(columns) < 2:
        return None
    kinds = []
    for i in range(min(2, len(columns))):
        vals = [r[i] for r in rows if r[i] is not None]
        if not vals:
            return None
        if all(isinstance(v, (int, float)) for v in vals):
            kinds.append("numeric")
        else:
            kinds.append("other")
    if kinds == ["other", "numeric"] or kinds == ["numeric", "other"]:
        x, y = (0, 1) if kinds[0] == "other" else (1, 0)
        import datetime as _dt
        vals = [r[x] for r in rows if r[x] is not None]
        temporal = any(isinstance(v, (_dt.date, _dt.datetime)) for v in vals)
        mark = "line" if temporal else "bar"
        return {"mark": mark, "x": columns[x], "y": columns[y]}
    return None


# --------------------------------------------------------------------------
# Factory
# --------------------------------------------------------------------------

_llm = None


def get_llm():
    global _llm
    if _llm is None:
        if config.LLM_PROVIDER == "mock":
            _llm = MockLLM()
        else:
            _llm = VertexLLM()
    return _llm


def reset_llm():
    """Test hook: drop the singleton (e.g. to re-read QUACK_LLM)."""
    global _llm
    _llm = None
