import os
from celery import Celery
from kombu import Exchange, Queue

from app.config import load_env

load_env()

rabbitmq_url = os.getenv("RABBITMQ_URL", "amqp://guest:guest@localhost:5672//")
redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# ---------------------------------------------------------------------------
# Priority queue definitions
#
#   text_tasks  — text responses, priority 9 (high), target delivery ≤200ms
#   video_tasks — video responses, priority 3 (low),  target delivery ≤2s
#
# Both queues declare a Dead Letter Exchange so failed/expired tasks are
# routed to "dlx" instead of silently dropped.
# ---------------------------------------------------------------------------

_text_exchange  = Exchange("text_tasks",  type="direct")
_video_exchange = Exchange("video_tasks", type="direct")
_dlx_exchange   = Exchange("dlx",         type="direct")

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
    # Dead Letter Queue — receives tasks that fail or expire
    Queue(
        "dead_letter",
        _dlx_exchange,
        routing_key="dlx",
    ),
)

celery_app = Celery(
    "backend",
    broker=rabbitmq_url,
    backend=redis_url,
    include=["app.ai.tasks"],
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
    },
)
