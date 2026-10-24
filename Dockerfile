# Sartarosh navbat bot — production image (ARCHITECTURE.md §0.2: python 3.12 runtime).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    SHOP_TZ=Asia/Tashkent

WORKDIR /app

# requirements.lock is the frozen, deployable set; fall back to requirements.txt if absent
COPY requirements.txt requirements.lock* ./
RUN if [ -s requirements.lock ]; then pip install -r requirements.lock; else pip install -r requirements.txt; fi

COPY . .

# non-root user; data/ (SQLite, bot.lock, backups) is the only writable path
RUN useradd --create-home --uid 10001 bot \
    && mkdir -p /app/data \
    && chown -R bot:bot /app/data
USER bot

VOLUME ["/app/data"]

# Telegram Mini App (WEBAPP_ENABLED=true); serve it through a TLS reverse proxy
EXPOSE 8080

# take bot.lock (/app/data), apply migrations, then start polling (one instance only)
CMD ["python", "run.py", "--migrate"]
