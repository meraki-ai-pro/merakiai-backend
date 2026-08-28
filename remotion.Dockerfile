# Render worker — Remotion.
#
# Unlike the Manim image this one executes NO generated code. The model emits a
# JSON spec validated against app/media/render/remotion_spec.py, and the fixed
# React components in remotion/ render it. There is no interpreter to escape,
# so the AST allowlist that guards Manim has no analogue here and needs none.
#
# The container hardening still applies — Chromium is a large attack surface in
# its own right, quite apart from what we ask it to draw.
#
# Build:
#   docker build -f remotion.Dockerfile -t merakiai-remotion .

FROM node:22-bookworm-slim

# Chromium's runtime libraries. Remotion downloads its own browser build, but
# these shared objects are not in the slim image.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates fonts-liberation ffmpeg \
        libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 \
        libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2 \
        libpango-1.0-0 libcairo2 \
        python3 python3-pip python3-venv \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Node deps first so a change to Python or React does not re-resolve npm.
COPY remotion/package.json remotion/
RUN cd remotion && npm install --omit=dev --no-audit --no-fund

COPY requirements-remotion.txt .
RUN pip install --no-cache-dir --break-system-packages -r requirements-remotion.txt

COPY remotion ./remotion
COPY app ./app

# Fetch the browser at build time. Runtime has a read-only root filesystem, so
# a missing browser must fail the image build instead of failing the first job.
RUN cd remotion && npx --no-install remotion browser ensure

RUN useradd --create-home --uid 10001 renderer \
    && mkdir -p /tmp/renders \
    && chown -R renderer:renderer /tmp/renders /app/remotion/node_modules
USER renderer

ENV TMPDIR=/tmp/renders \
    HOME=/tmp/renders \
    REMOTION_PROJECT=/app/remotion \
    REMOTION_DISABLE_TELEMETRY=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    RENDERER=remotion \
    CELERY_INCLUDE=app.media.render.tasks

CMD ["celery", "-A", "app.core.celery_app.celery_app", "worker", \
     "--queues=render_remotion", "--concurrency=1", "--loglevel=info"]
