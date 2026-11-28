FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Kolkata \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install ".[broker,telegram]" \
    && useradd --uid 10001 --create-home engine \
    && mkdir -p /app/data \
    && chown engine:engine /app/data

USER engine
# Healthy while the heartbeat thread keeps writing (every 20 s).
HEALTHCHECK --interval=60s --timeout=5s --start-period=120s --retries=3 \
  CMD python -c "import os,sys,time; p='/app/data/heartbeat'; sys.exit(0 if os.path.exists(p) and time.time()-os.path.getmtime(p) < 180 else 1)"
CMD ["python", "-m", "sensex_engine"]
