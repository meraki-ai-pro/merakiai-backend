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
  --queues=render_manim --concurrency=1 --pool=solo --loglevel=info
```

This worker also needs **manim** and a **LaTeX distribution** (the generated
scenes use `MathTex`). Neither is in requirements.txt on purpose — only the
render container needs them. Put manim in its own virtualenv and point
`MANIM_PYTHON` at that interpreter instead of installing a renderer's worth of
dependencies into the backend venv:

```bash
python -m venv .venv-manim && .venv-manim/Scripts/python -m pip install manim==0.18.1
```

```bash
MANIM_PYTHON="$PWD/.venv-manim/Scripts/python.exe"
```

Verified on Windows with MiKTeX supplying `latex` and `dvisvgm`. `MANIM_QUALITY=-ql`
renders in well under a minute, which is what you want while iterating; the
default `-qm` is the pilot setting. The pin matches `requirements-render.txt` —
0.21 also works but is not what ships.

Without LaTeX the render reaches manim and then dies with
`FileNotFoundError: [WinError 2]` when manim tries to spawn `latex`.

### Narration is a SECOND job, on the worker from step 5

A finished render is silent. The spoken track is added afterwards by
`app.ai.tasks.process_narration_task` on **video_tasks** — the ordinary worker
from step 5 — not by the render worker.

That split is deliberate and is not worth "simplifying". The render worker
executes model-generated Python, so `render.Dockerfile` installs no ElevenLabs
client and `docker-compose.render.yml` passes it no key: a payload that escapes
the AST allowlist finds no credential to steal and no HTTP client to steal it
with. Adding TTS there would undo that in one line.

Consequences while running locally:

* the worker from step 5 must be running, or every video stays silent with
  `narration_status = 'pending'`;
* that worker needs **ffmpeg and ffprobe on PATH** (the main Dockerfile
  installs them; a bare Windows checkout does not). Without them the video is
  still fine and reviewable — narration just records `failed`;
* `ELEVENLABS_API_KEY` must be set, and a voice must be resolvable: either
  `RENDER_NARRATION_VOICE_ID`, or an active row in `avatar_voice_bundles`.

| Variable | Default | Meaning |
|---|---|---|
| `RENDER_NARRATION` | `1` | Set to `0` to render silent videos. Assets are marked `skipped`, not `failed`. |
| `RENDER_NARRATION_VOICE_ID` | _(empty)_ | Global override that forces ONE voice for every course. Normally left empty so the voice comes from the course — see below. |
| `DEFAULT_NARRATION_VOICE_ID` | first active bundle | The house narrator, used by any course whose lecturer has not recorded a voice. Point this at a clear, well-paced ElevenLabs voice. |
| `RENDER_FFMPEG_TIMEOUT` | `180` | Seconds before the mux is abandoned. |
| `MANIM_SPEED` | `0.85` | Playback stretch on finished Manim renders — 0.85 plays them back 15% slower. `1.0` disables it. |

### Whose voice speaks

One resolver, `app/media/voices.voice_for_course`, answers this for BOTH the
concept videos and the Learn-mode lesson board, so the two cannot drift into
different voices for the same course:

1. the voice the lecturer recorded and attached to that course
   (`/lecturer/voice`, then the course's Settings tab);
2. `DEFAULT_NARRATION_VOICE_ID`;
3. the first active `avatar_voice_bundles` row, so a deployment that has
   configured nothing still speaks.

The **lesson board** now calls `POST /narration/board`, which synthesises a
slide in the course's voice and caches it in the audio bucket keyed by voice +
text — a cohort of two hundred pays for each slide once. The browser's own
speech synthesis remains as a fallback, so an outage makes the lesson sound
worse rather than silent.

Requires `sql/014_lecturer_voices.sql`. Without it, voice recording reports
itself unavailable and every course falls back to the default narrator.

An asset is briefly `status='ready'` with `has_audio=false`. That is a real
state, it is stored, and the lecturer's review queue shows "Adding narration…"
rather than pretending the video is finished.

### Renders for Biology, Chemistry and other non-mathematical courses

Those route to **Remotion**, not Manim, and Remotion is a different worker
image (`remotion.Dockerfile`, `RENDERER=remotion`). With only the Manim worker
running, a Chemistry render sits queued for ever — which looks like a hang and
is not one.

**Each renderer has its own queue** — `render_manim` and `render_remotion`.
They deliberately do not share one: each image registers only the renderer it
carries, so a shared queue lets whichever worker is free take a job it cannot
serve, and it fails with `No renderer registered under 'manim'`. Dispatch picks
the queue from the asset's renderer (`routing.render_queue()`), so a department
teaching both maths and biology just runs both workers.

To run Remotion locally rather than in its container:

```bash
cd remotion && npm install          # once
```

```bash
RENDERER=remotion REMOTION_PROJECT="$PWD/remotion" \
CELERY_INCLUDE=app.media.render.tasks .venv/Scripts/python -m celery \
  -A app.core.celery_app.celery_app worker \
  --queues=render_remotion --concurrency=1 --pool=solo --loglevel=info
```

`REMOTION_PROJECT` is required outside the container — it defaults to
`/app/remotion`, which does not exist on a dev machine, and the renderer then
reports the project as missing.

Check where a course will route before queueing anything:

```bash
.venv/Scripts/python scripts/check_concept_videos.py
```

Offline, no keys, no cost. Add `--live --course <id> --token <lecturer-jwt>` to
queue real renders across four subjects and poll them to completion.

Routing reads `courses.subject` (Settings tab in the lecturer workspace). Leave
it blank and the course **name** is used as the hint; if neither matches, the
default renderer is Manim — which is right for a maths course and wrong for a
Pharmacology one.

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

774 tests, no network or database needed, about 40 seconds.

Two in `test_retriever.py::TestChunkPresentation` fail on a checkout as of
2026-08-19 and are unrelated to anything above: `RetrievedChunk.location`
deliberately omits the storage filename ("Student-safe source label without
internal storage filenames") and those two tests still assert the older shape
that included it.

## Database

Schema changes live in `sql/`, numbered in dependency order. If the backend
starts logging "Apply 0XX_….sql", that migration has not been run against your
Supabase project. See `sql/README.md`.

`sql/013_roster_import_narration_and_upload_tags.sql` is the most recent. Every
feature that needs it degrades rather than breaking — roster import reports the
un-registered half as failures, narration bookkeeping is skipped, upload tags
are dropped, and retrieval falls back to the pre-tagging column set — so the
symptom is quiet missing behaviour, not a 500. Apply it.
