import os
from celery import Celery
from kombu import Exchange, Queue

from app.config import load_env

load_env()

# Broker: prefer RABBITMQ_URL (production) → CELERY_BROKER_URL → Redis fallback.
# This lets the Docker Compose setup use Redis for everything without a separate
# RabbitMQ container, while production deployments can still use RabbitMQ.
redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")

rabbitmq_url = (
    os.getenv("RABBITMQ_URL")
    or os.getenv("CELERY_BROKER_URL")
    or redis_url.replace("/0", "/1")  # Redis DB 1 for broker
)

# ---------------------------------------------------------------------------
# Priority queue definitions
#
#   text_tasks  — text responses, priority 9 (high), target delivery ≤200ms
#   video_tasks — video responses, priority 3 (low),  target delivery ≤2s
#
# Both queues declare a Dead Letter Exchange so failed/expired tasks are
# routed to "dlx" instead of silently dropped.
# ---------------------------------------------------------------------------

_text_exchange      = Exchange("text_tasks",      type="direct")
_video_exchange     = Exchange("video_tasks",     type="direct")
_ingestion_exchange = Exchange("ingestion_tasks", type="direct")
_render_exchange    = Exchange("render_tasks",    type="direct")
_dlx_exchange       = Exchange("dlx",             type="direct")

task_queues = (
    Queue(
        "text_tasks",
        _text_exchange,
        routing_key="text",
        queue_arguments={
            "x-max-priority": 10,
            "x-dead-letter-exchange": "dlx",
        },
    ),
    Queue(
        "video_tasks",
        _video_exchange,
        routing_key="video",
        queue_arguments={
            "x-max-priority": 10,
            "x-dead-letter-exchange": "dlx",
        },
    ),
    Queue(
        "ingestion_tasks",
        _ingestion_exchange,
        routing_key="ingestion",
        # No priority needed — ingestion is always a single admin job.
        # DLX still catches failures so nothing is silently dropped.
        queue_arguments={
            "x-dead-letter-exchange": "dlx",
        },
    ),
    # ONE QUEUE PER RENDERER, not one shared render queue.
    #
    # Each render image registers only the renderer it carries (see
    # app/media/render/tasks.py), so a shared queue lets the Remotion worker
    # pick up a Manim job it cannot serve — it fails with "No renderer
    # registered under 'manim'", which looks like a broken install rather than
    # a misrouted message. Dispatch picks the queue from the asset's renderer
    # via routing.render_queue().
    #
    # Renders take minutes and must never share a worker with a student turn,
    # which is why none of these is the default queue.
    Queue(
        "render_manim",
        _render_exchange,
        routing_key="render.manim",
        queue_arguments={"x-dead-letter-exchange": "dlx"},
    ),
    Queue(
        "render_remotion",
        _render_exchange,
        routing_key="render.remotion",
        queue_arguments={"x-dead-letter-exchange": "dlx"},
    ),
    # Kept declared so messages still in flight from a pre-split deployment are
    # not lost on upgrade. Nothing dispatches here any more.
    Queue(
        "render_tasks",
        _render_exchange,
        routing_key="render",
        queue_arguments={
            "x-dead-letter-exchange": "dlx",
        },
    ),
    # Dead Letter Queue — receives tasks that fail or expire
    Queue(
        "dead_letter",
        _dlx_exchange,
        routing_key="dlx",
    ),
)

result_backend = os.getenv("CELERY_RESULT_BACKEND", redis_url)

# Which task modules this process imports at startup.
#
# The render worker sets CELERY_INCLUDE=app.media.render.tasks so it does NOT
# import app.ai.tasks — that module pulls in video_service (ElevenLabs, D-ID,
# Tavus) and the RAG service (Pinecone, OpenAI), none of which a render needs.
# Keeping them out halves the image and, more importantly, means those client
# libraries are not present in the container that executes generated code.
#
# Dispatch does not require the import: the API sends render jobs by task name.
_include = [
    module.strip()
    for module in os.getenv("CELERY_INCLUDE", "app.ai.tasks").split(",")
    if module.strip()
]

celery_app = Celery(
    "backend",
    broker=rabbitmq_url,
    backend=result_backend,
    include=_include,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    # Workers take one task at a time — prevents a slow video task
    # from blocking a fast text task on the same worker process.
    worker_prefetch_multiplier=1,
    task_queues=task_queues,
    # Default queue for any task not explicitly routed
    task_default_queue="text_tasks",
    task_default_exchange="text_tasks",
    task_default_routing_key="text",
    # Explicit routing so all tasks land in the correct queue regardless of
    # how they are dispatched (.delay() vs .apply_async()).
    # Note: process_rag_turn_task is intentionally absent here because its
    # queue (text_tasks vs video_tasks) is chosen dynamically at call-site
    # based on the user's prefers_video setting.
    task_routes={
        "app.ai.tasks.process_mode_session_start_task": {"queue": "text_tasks"},
        "app.ai.tasks.process_mode_session_turn_task":  {"queue": "text_tasks"},
        "app.ai.tasks.process_ingestion_task":          {"queue": "ingestion_tasks"},
        # No static route: the queue depends on the ASSET's renderer, so every
        # dispatch passes queue= explicitly (routing.render_queue()). A static
        # entry here would silently override that for .delay() callers.
        # Narration is dispatched BY THE RENDER WORKER but must not run there:
        # that container has no ElevenLabs client and no key for one, because it
        # executes model-generated code. video_tasks is the ordinary media
        # worker, which has ffmpeg and the TTS credentials.
        "app.ai.tasks.process_narration_task":          {"queue": "video_tasks"},
    },
)
