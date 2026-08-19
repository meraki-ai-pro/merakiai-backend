# Running the backend locally

For frontend work. Gets you a backend the Next.js app can talk to, including
streaming answers over WebSocket.

The backend is four processes, not one:

```
Next.js  :3000  ──HTTP──▶  FastAPI  :8000  ──▶  RabbitMQ :5672 ──▶ Celery worker
    ▲                                                                    │
    └────────────── WebSocket ◀── FastAPI ◀── Redis :6379 pub/sub ◀───────┘
```

**A student turn is asynchronous.** `POST /rag/turn` returns a `task_id`
immediately; the answer arrives on the WebSocket. If you skip the Celery
worker, every request will look like it succeeded and no answer will ever
come — which is the single most common way to lose an afternoon here.

---

## 1. Prerequisites

- **Python 3.12** (3.13 is untested)
- **Docker Desktop**, running — Redis and RabbitMQ come from containers
- A `.env` file. Ask the backend team; it is gitignored and contains live keys.

## 2. Install

```bash
cd "Backend - V1"
python -m venv .venv
.venv/Scripts/activate          # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

## 3. Start Redis and RabbitMQ

```bash
docker compose -p merakiai -f docker-compose.dev.yml up -d
```

Wait for RabbitMQ to report healthy — the worker will not connect before then:

```bash
docker compose -p merakiai -f docker-compose.dev.yml ps
```

## 4. Run the API

```bash
.venv/Scripts/python -m uvicorn app.main:app --reload --port 8000
```

Check it: <http://localhost:8000/health> should return `{"status":"ok"}`, and
<http://localhost:8000/docs> gives you every endpoint with a try-it button.

## 5. Run the Celery worker — in a second terminal

**Without this, answers never arrive.**

```bash
.venv/Scripts/python -m celery -A app.core.celery_app.celery_app worker \
  --queues=text_tasks,video_tasks --concurrency=2 --pool=solo --loglevel=info
```

`--pool=solo` is needed on Windows; on macOS or Linux you can drop it.

Wait for `celery@<host> ready.` before sending a turn.

Ingestion (uploading course documents) runs on its own queue. Only start this
third worker if you are testing uploads:

```bash
.venv/Scripts/python -m celery -A app.core.celery_app.celery_app worker \
  --queues=ingestion_tasks --concurrency=1 --pool=solo --loglevel=info
```

## 6. Concept videos — a fourth worker, only if you need them

Renders run on their own queue, and that worker **must** set `CELERY_INCLUDE`.
Without it the task module is never imported, jobs sit unconsumed in the queue,
and the worker logs `KeyError: app.media.render.tasks.process_render_task` —
which reads like a broken queue rather than a missing environment variable.

```bash
CELERY_INCLUDE=app.media.render.tasks .venv/Scripts/python -m celery \
  -A app.core.celery_app.celery_app worker \
  --queues=render_tasks --concurrency=1 --pool=solo --loglevel=info
```

This worker also needs **manim** and a **LaTeX distribution** (the generated
scenes use `MathTex`). Neither is in requirements.txt on purpose — only the
render container needs them. If manim lives in its own virtualenv, point
`MANIM_PYTHON` at that interpreter instead of installing a renderer's worth of
dependencies into the backend venv:

```bash
MANIM_PYTHON=/path/to/manim-venv/Scripts/python.exe
```

Without LaTeX the render reaches manim and then dies with
`FileNotFoundError: [WinError 2]` when manim tries to spawn `latex`.

## 7. Point the frontend at it

`merakiai-frontend/.env.local`:

```
NEXT_PUBLIC_API_URL=http://localhost:8000
NEXT_PUBLIC_WS_URL=ws://localhost:8000
```

The frontend currently runs on port 3001. Configure the backend with:

```bash
ALLOWED_ORIGINS=http://localhost:3001
PUBLIC_SITE_URL=http://localhost:3001
```

For production, replace that origin with the public frontend URL in both
variables and add `<frontend-origin>/auth/reset-password` to Supabase Auth's
allowed redirect URLs. If multiple frontend origins are active,
`ALLOWED_ORIGINS` accepts a comma-separated list.

---

## Verifying it works end to end

1. Log in as a student who is **enrolled on a course**. Enrolment is enforced —
   an unenrolled account gets `403` on session creation, not an empty screen.
2. Start a Learn session and ask a question about the course material.
3. In DevTools → Network → WS you should see, in order:

   ```
   text_stream_start
   status      stage=retrieving
   sources     (before the first token — this is deliberate)
   status      stage=generating
   text_chunk  … many …
   response_complete
   ```

`sources` arriving before generation is intentional: the client can render what
the answer is being drawn from while it is still being written.

Expect **3–5 seconds** to the first token and 25–35 seconds for a full answer.
Most of that is the model writing, and it streams, so the screen is never idle.

---

## Things that will waste your time

**Nothing happens after `POST /rag/turn`.**
The Celery worker is not running, or is not consuming `text_tasks`. Check its
terminal for `ready.` and the `[queues]` block.

**`docker compose up` fails with a name conflict.**
Another project on your machine owns a container literally named `redis` or
`rabbitmq`. `docker-compose.dev.yml` deliberately sets no `container_name` to
avoid this. If you hit it anyway: `docker ps -a --filter name=redis`.

**Everything returns 401.**
Your Supabase JWT expired. Log out and back in. Tokens are short-lived.

**403 on a course that clearly exists.**
Not enrolled, or the course has Application mode disabled. `GET /enrolments`
shows what the logged-in account is actually on.

**404 from a `/lecturer/...` route on a real course.**
The account does not own that course. This returns 404 rather than 403 on
purpose, so course ids cannot be probed by guessing.

**Answers arrive but with no citations or sources.**
The course's documents are unpublished, or tagged for a different mode. A
lecturer can check with the Knowledge tab's test-query box.

**The board renders as plain text with `::: slide` visible.**
`LESSON_BOARD` is off, or the frontend is not parsing fences. Set
`LESSON_BOARD=1` in the backend `.env`.

**Redis is down.**
Answers still work but get noticeably slower — the query-embedding cache
degrades to calling OpenAI every time. Rate limiting also fails open. Neither
breaks anything; both make things worse quietly, so check Redis before chasing
a performance ghost.

---

## Useful without a frontend

`http://localhost:8000/docs` — every endpoint, with auth. Paste a JWT from
your browser's cookies (`meraki_token`) into the **Authorize** box and you can
drive the lecturer and assessment APIs directly.

## Tests

```bash
.venv/Scripts/python -m pytest tests/unit -q
```

667 tests, no network or database needed, about 10 seconds.

## Database

Schema changes live in `sql/`, numbered in dependency order. If the backend
starts logging "Apply 0XX_….sql", that migration has not been run against your
Supabase project. See `sql/README.md`.
