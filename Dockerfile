# QuackQuery — self-correcting text-to-SQL analytics API.
FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY static/ ./static/
COPY data/demo/ ./data/demo/

# Writable scratch for the DuckDB file (Cloud Run container FS is fine at demo scale)
ENV QUACK_DATA_DIR=/tmp/quackquery

CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers 1
