FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# System deps for document parsing libraries
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libpq-dev libmagic1 ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.lock.txt .
RUN python -m pip install --require-hashes --requirement requirements.lock.txt

ARG APP_UID=10001
RUN addgroup --system --gid ${APP_UID} merakiai \
    && adduser --system --uid ${APP_UID} --ingroup merakiai --home /app merakiai

COPY --chown=merakiai:merakiai . .

USER merakiai

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"]

# Default: run the FastAPI server.
# Override CMD in docker-compose for the Celery worker.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
