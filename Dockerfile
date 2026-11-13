# Tested with Python 3.13 (the test suite runs on 3.13).
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATABASE_PATH=/app/data/shop.sqlite3

WORKDIR /app

# Exact, tested dependency versions (requirements.lock is generated with pip freeze).
COPY requirements.lock requirements.txt ./
RUN pip install --no-cache-dir -r requirements.lock \
    && useradd --create-home --uid 10001 shop

# Application code belongs to root and is read-only for the app user;
# only /app/data (database, backups, logs) is writable.
COPY . .
RUN mkdir -p /app/data && chown shop:shop /app/data && chmod 700 /app/data

USER shop
VOLUME ["/app/data"]
EXPOSE 8080

# Panel health (GET /healthz needs no login). The bot service overrides this in compose.yaml.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import os,sys,urllib.request; sys.exit(0 if b'true' in urllib.request.urlopen('http://127.0.0.1:'+os.getenv('PORT','8080')+'/healthz',timeout=4).read() else 1)"]

# run.py handles SIGTERM/SIGINT gracefully; compose adds `init: true` so signals reach Python.
CMD ["python", "run.py", "panel"]
