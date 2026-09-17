"""Configuration — env-driven knobs. QUACK_LLM=mock runs the whole pipeline
with zero API calls (tests + offline demo)."""
import os

# LLM provider: "vertex" (default on GCP / with gcloud ADC) or "mock"
LLM_PROVIDER = os.environ.get("QUACK_LLM", "vertex")

GCP_PROJECT = os.environ.get("GCP_PROJECT", "personal-project-dg21")
GCP_REGION = os.environ.get("GCP_REGION", "us-central1")
LLM_MODEL = os.environ.get("QUACK_LLM_MODEL", "gemini-2.5-flash")
LLM_TIMEOUT_S = float(os.environ.get("QUACK_LLM_TIMEOUT_S", "30"))
LLM_MAX_ATTEMPTS = 4   # per HTTP call: 429/5xx backoff rounds

# Pipeline knobs
MAX_RETRIES = int(os.environ.get("QUACK_MAX_RETRIES", "3"))   # LLM repair passes after a failed exec
QUERY_TIMEOUT_S = float(os.environ.get("QUACK_QUERY_TIMEOUT_S", "10"))
MAX_UPLOAD_BYTES = int(os.environ.get("QUACK_MAX_UPLOAD_BYTES", str(50 * 1024 * 1024)))
MAX_ROWS_IN_RESPONSE = int(os.environ.get("QUACK_MAX_ROWS", "200"))
CACHE_TTL_S = int(os.environ.get("QUACK_CACHE_TTL_S", str(30 * 60)))
CACHE_MAX_ENTRIES = 256

# Storage (DuckDB catalog file). Kept in a writable data dir; Cloud Run uses /tmp-sized mem.
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("QUACK_DATA_DIR", os.path.join(BASE_DIR, "data"))
DB_PATH = os.path.join(DATA_DIR, "quackquery.duckdb")
# Bundled demo CSVs always ship with the repo/image, independent of DATA_DIR
DEMO_DIR = os.path.join(BASE_DIR, "data", "demo")
STATIC_DIR = os.path.join(BASE_DIR, "static")

# Rate limit (sliding window per client IP)
RATE_LIMIT_RPM = int(os.environ.get("QUACK_RATE_LIMIT_RPM", "60"))

# Result preview / sample sizes
SCHEMA_SAMPLE_VALUES = 5
