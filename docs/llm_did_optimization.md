# LLM + D-ID Response Speed Optimizations

## Overview

The current pipeline processes every `rag_turn` sequentially, meaning the user waits for every step to complete before receiving any response. The total latency can range from **30–90 seconds** for video responses. The optimizations below target each bottleneck in order of impact.

---

## Current Pipeline (Sequential)

```
User message
  → Pinecone retrieval
  → Claude API call (LLM)         ← 5–15s  (claude-opus-4-6)
  → ElevenLabs TTS                ← 1–3s
  → Upload audio to Supabase      ← 0.5–1s
  → D-ID clip creation + polling  ← 20–60s
  → Redis publish → WebSocket
```

Every step blocks the next. The user sees nothing until the very end.

---

## Optimization 1 — Switch to a Faster Model per Mode

**File:** `app/ai/rag/claude.py` ✅ **Implemented**

**Problem:** `claude-opus-4-6` was used for all modes, including review evaluations that output ~200 tokens of structured JSON. Opus is the slowest and most expensive model for this.

**Fix:** Mode-specific models. Reserve Opus for learn mode only.

| Mode | Model | Why |
|---|---|---|
| `learn` | `claude-opus-4-6` | Deep reasoning for rich explanatory responses |
| `application` | `claude-sonnet-4-6` | Strong quality for scenario generation, 3–8s faster |
| `review` | `claude-haiku-4-5-20251001` | Fastest — outputs ~200 tokens of structured JSON |

**Expected saving:** 3–8s per review/application turn.

---

## Optimization 2 — Send Text to the Frontend Immediately, Video Async

**Files:** `app/ai/tasks.py`, frontend WebSocket handler

**Problem:** The user waits for the full video pipeline (TTS → upload → D-ID → polling) before receiving any response. The text is ready in seconds but is held back.

**Fix:** Publish the text response to the WebSocket immediately after the LLM call, then publish the video as a second message when it is ready. The frontend displays text first and swaps in the video on arrival.

```python
# app/ai/tasks.py — _do_rag_turn

async def _do_rag_turn(session_id: str, user_id: str, message: str, mode: str):
    memory = await load_memory(session_id)
    result = await query_rag(user_message=message, mode=mode, memory=memory)
    tutor_text = result["response"]

    # ✅ Publish text immediately — user sees a response within seconds
    _publish_to_ws(session_id, {
        "type": "text_ready",
        "response": tutor_text,
        "response_format": "text",
    })

    # Continue generating video in background (only for learn mode + video preference)
    response_format = await response_format_for_mode(session_id, mode)
    if response_format == "video" and mode == "learn":
        try:
            delivery = await maybe_generate_video(
                text=tutor_text,
                response_format=response_format,
                user_id=user_id,
                session_id=session_id,
                mode=mode,
            )
            # ✅ Publish video when ready as a follow-up message
            _publish_to_ws(session_id, {
                "type": "video_ready",
                **delivery,
            })
        except Exception as e:
            print(f"VIDEO GENERATION ERROR: {e}")

    await log_conversation(...)
```

**Frontend:** Listen for both `text_ready` and `video_ready` event types on the WebSocket and render accordingly.

**Expected saving:** Perceived latency drops by **10–30s** — users see text almost immediately.

---

## Optimization 3 — Replace Polling with Webhooks (D-ID primary + Tavus fallback)

**Files:** `app/media/did_service.py`, `app/media/tavus_service.py`, `app/api/v1/webhooks/did.py`, `app/api/v1/webhooks/tavus.py`, `app/media/video_service.py` ✅ **Implemented**

**Problem:** Both D-ID and Tavus were polled every 2.5–5s for up to 6–10 minutes, blocking the Celery video worker thread the entire time.

**Fix:** Pass a webhook URL when submitting the job. The worker exits immediately after submission (~5–10s). The provider POSTs back on completion; the webhook handler publishes the video URL to Redis → WebSocket.

### Architecture

```
video_tasks queue
  → Celery worker: TTS + upload + POST to D-ID/Tavus (with webhook URL)
  → worker free in ~5–10s  ← key change

  Later (20–60s for D-ID, 30–60s for Tavus with fast:true):
  D-ID/Tavus → POST /webhooks/did/{session_id} or /webhooks/tavus/{session_id}
             → publish to Redis ws:{session_id}
             → WebSocket → client receives video_ready event
```

**Controlled by:** `PUBLIC_BASE_URL` env var. If unset, falls back to polling for local dev.

### D-ID webhook field

```python
payload["webhook"] = "https://your-domain.com/webhooks/did/{session_id}"
```

D-ID posts back with `status: "done"`, `result_url`, `subtitles_url`.

### Tavus webhook field + fast mode

```python
payload["callback_url"] = "https://your-domain.com/webhooks/tavus/{session_id}"
payload["fast"] = True   # cuts render time from 3–10min → 30–60s
```

Tavus posts back with `status: "ready"`, `download_url`, `hosted_url`, `stream_url`.

> **Bug fixed:** The original `tavus_service.py` was passing `audio_url` as the `"script"` field (wrong). Fixed to use `"audio_url"` (correct BYOA field).

### .env requirement

```
PUBLIC_BASE_URL=https://api.yourdomain.com
```

**Expected saving:** Video worker freed in 5–10s instead of 20–360s. Eliminates all polling overhead.

---

## Optimization 4 — Parallelize DB Lookups + TTS

**File:** `app/media/video_service.py` ✅ **Implemented**

**Problem:** User profile, bundle config, and text cleaning were all sequential, even though they are independent.

**Fix:** Two `asyncio.gather` calls overlap the independent work:

```
Round 1 (parallel):  user profile DB query  +  text cleaning
Round 2 (parallel):  bundle DB query        +  ElevenLabs TTS
Round 3 (sequential): Supabase Storage upload
```

This ensures DB I/O is never blocking TTS and vice versa.

**Expected saving:** 0.5–2s per video request by overlapping DB I/O with text cleaning and TTS.

---

## Optimization 5 — LLM Streaming (Advanced)

**File:** `app/ai/rag/claude.py`

**Problem:** Claude generates the entire response before TTS begins. For a 300-word learn-mode answer, the first word and the last word are both held until generation is complete.

**Fix:** Use the Anthropic streaming API. Buffer output sentence-by-sentence and pipe the first complete sentence to TTS as soon as it arrives, while the rest is still being generated.

```python
# app/ai/rag/claude.py — streaming variant

async def generate_response_streaming(prompt: str, mode: str):
    """
    Yields text chunks as they arrive from Claude.
    Caller is responsible for sentence-level buffering.
    """
    config = get_mode_config(mode)

    async with client.messages.stream(
        model=config["model"],
        temperature=config["temperature"],
        max_tokens=config["max_tokens"],
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        async for text_chunk in stream.text_stream:
            yield text_chunk
```

To use this in a sentence-pipeline pattern:

```python
import re

async def stream_to_sentences(prompt: str, mode: str):
    buffer = ""
    async for chunk in generate_response_streaming(prompt, mode):
        buffer += chunk
        # Yield complete sentences
        sentences = re.split(r'(?<=[.!?])\s+', buffer)
        for sentence in sentences[:-1]:
            yield sentence.strip()
        buffer = sentences[-1]
    if buffer.strip():
        yield buffer.strip()
```

Then call TTS on the first sentence while awaiting the rest — overlapping LLM generation with audio synthesis.

**Expected saving:** 3–8s perceived latency reduction for learn mode.

---

## Summary Table

| # | Optimization | Files Affected | Estimated Saving | Complexity |
|---|---|---|---|---|
| 1 | Faster model for review/practice | `claude.py` | 3–8s | Low |
| 2 | Text-first WebSocket delivery | `tasks.py`, frontend | 10–30s perceived | Low |
| 3 | D-ID webhooks (no polling) | `did_service.py`, new webhook router | 5–30s | Medium |
| 4 | Parallel DB + TTS setup | `video_service.py` | 0.5–2s | Low |
| 5 | LLM streaming + sentence-pipeline TTS | `claude.py`, `tasks.py` | 3–8s | High |

### Recommended Implementation Order

1. **Optimization 2** — Text-first delivery. Zero risk, immediate user-perceived improvement.
2. **Optimization 1** — Faster model for review/practice. One-line change per mode.
3. **Optimization 3** — D-ID webhooks. Requires a new endpoint but eliminates the biggest bottleneck.
4. **Optimization 4** — Parallel DB calls. Low risk refactor.
5. **Optimization 5** — Streaming LLM. Most complex; do last once others are stable.
