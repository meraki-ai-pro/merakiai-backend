FROM python:3.11-slim

WORKDIR /app

# System deps for document parsing libraries
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libpq-dev libmagic1 ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# Default: run the FastAPI server.
# Override CMD in docker-compose for the Celery worker.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
