# Render worker — Manim.
#
# This container executes LLM-generated Python. The AST allowlist in
# app/media/render/sandbox.py is the first line of defence; this file is the
# one that has to hold when the first line is bypassed by something nobody
# anticipated.
#
# Build:
#   docker build -f render.Dockerfile -t merakiai-render .
#
# Run (see the compose service for the full set of hardening flags — several of
# them cannot be expressed in a Dockerfile and are NOT optional):
#   docker run --network none --read-only --memory 2g --cpus 2 merakiai-render

FROM python:3.12-slim

# Manim's dependencies. LaTeX is what typesets MathTex.
#
# This is a deliberately MINIMAL TeX set. Manim's own Debian instructions list
# `texlive texlive-latex-extra texlive-fonts-extra texlive-science tipa`, which
# is a 1069 MB download — most of it texlive-fonts-extra, pulled in only
# because Manim's DEFAULT tex template loads calligra and physics. The much
# smaller texlive-latex-extra package is still required: it provides
# standalone.cls, the document class used by Manim's cropped SVG template.
#
# app/media/render/manim_renderer.py writes its own minimal tex template
# (amsmath, amssymb, mathrsfs, xcolor) instead, so those packages are not
# needed. That covers everything the pilot's calculus, statistics and
# quantitative-techniques content typesets.
#
# If an exotic symbol ever fails to render, the fix is to add the one package
# that provides it here — not to reinstate the full 1 GB set.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        pkg-config \
        libcairo2-dev \
        libpango1.0-dev \
        ffmpeg \
        texlive-latex-base \
        texlive-latex-recommended \
        texlive-latex-extra \
        texlive-fonts-recommended \
        texlive-science \
        dvisvgm \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# requirements-render.txt, not requirements.txt: this container runs generated
# code, so every installed library is reachable surface. Pinecone, OpenAI,
# ElevenLabs, boto3 and unstructured are deliberately absent.
COPY requirements-render.txt .
RUN pip install --no-cache-dir -r requirements-render.txt

COPY app ./app

# Non-root. Generated code runs with this user's privileges, so it must own as
# little as possible — note it does NOT own /app.
RUN useradd --create-home --uid 10001 renderer \
    && mkdir -p /tmp/renders \
    && chown renderer:renderer /tmp/renders
USER renderer

# Renders are written under here. With --read-only on the container, this is
# the only writable location, which is why the renderer sets TMPDIR and HOME
# into its own temporary working directory.
ENV TMPDIR=/tmp/renders \
    HOME=/tmp/renders \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # Do not import app.ai.tasks — see app/core/celery_app.py. That module
    # pulls the video, TTS and RAG stacks, none of which are installed here.
    CELERY_INCLUDE=app.media.render.tasks

# --concurrency=1: a render is CPU-bound and memory-hungry, so one at a time
# per container. Scale by adding containers, not threads.
CMD ["celery", "-A", "app.core.celery_app.celery_app", "worker", \
     "--queues=render_manim", "--concurrency=1", "--loglevel=info"]
