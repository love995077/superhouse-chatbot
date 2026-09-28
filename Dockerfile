# Superhouse Chatbot - production image (FastAPI + Uvicorn on port 8001)
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    SUPERHOUSE_DB=/app/data/superhouse.db

WORKDIR /app

# Run as an unprivileged user. UID matches the 'ubuntu' user on the Oracle VM so the
# bind-mounted ./data directory stays writable without chmod hacks.
ARG APP_UID=1001
RUN useradd --create-home --uid ${APP_UID} --shell /usr/sbin/nologin app

# Dependencies first for better layer caching.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY --chown=app:app . .
RUN mkdir -p /app/data && chown app:app /app/data

USER app
EXPOSE 8001

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8001/health', timeout=4).status == 200 else 1)"

# Rebuild superhouse.db from the bundled Excel on every start (atomic, ~2s), so the data
# always matches the shipped workbook, then hand PID 1 to uvicorn for clean signal handling.
# - single worker: sessions and rate limits are in-process state
# - --no-proxy-headers: Caddy proxies at layer 4 and never sets X-Forwarded-*, so trusting those
#   headers would only let clients spoof their address
# - --no-server-header: don't advertise the server software
# - --limit-concurrency: shed load with 503s instead of piling up connections
CMD ["sh", "-c", "python setup_db.py \"Chatbot data.xlsx\" \"$SUPERHOUSE_DB\" && exec uvicorn main:app --host 0.0.0.0 --port 8001 --workers 1 --no-proxy-headers --no-server-header --limit-concurrency 64 --timeout-keep-alive 5"]
