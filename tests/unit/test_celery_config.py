"""
Unit tests for app/core/celery_app.py

Verifies the Celery broker configuration, queue definitions, and
priority settings without connecting to RabbitMQ or Redis.
"""
import pytest


@pytest.mark.unit
class TestCeleryQueues:
    def test_text_tasks_queue_exists(self):
        from app.core.celery_app import task_queues
        names = [q.name for q in task_queues]
        assert "text_tasks" in names

    def test_video_tasks_queue_exists(self):
        from app.core.celery_app import task_queues
        names = [q.name for q in task_queues]
        assert "video_tasks" in names

    def test_dead_letter_queue_exists(self):
        from app.core.celery_app import task_queues
        names = [q.name for q in task_queues]
        assert "dead_letter" in names

    def test_text_tasks_has_max_priority_10(self):
        from app.core.celery_app import task_queues
        text_q = next(q for q in task_queues if q.name == "text_tasks")
        assert text_q.queue_arguments.get("x-max-priority") == 10

    def test_video_tasks_has_max_priority_10(self):
        from app.core.celery_app import task_queues
        video_q = next(q for q in task_queues if q.name == "video_tasks")
        assert video_q.queue_arguments.get("x-max-priority") == 10

    def test_text_tasks_has_dead_letter_exchange(self):
        from app.core.celery_app import task_queues
        text_q = next(q for q in task_queues if q.name == "text_tasks")
        assert text_q.queue_arguments.get("x-dead-letter-exchange") == "dlx"

    def test_video_tasks_has_dead_letter_exchange(self):
        from app.core.celery_app import task_queues
        video_q = next(q for q in task_queues if q.name == "video_tasks")
        assert video_q.queue_arguments.get("x-dead-letter-exchange") == "dlx"


@pytest.mark.unit
class TestCeleryWorkerConfig:
    def test_prefetch_multiplier_is_1(self):
        """Ensures workers take one task at a time — prevents video tasks
        from starving text tasks on the same worker."""
        from app.core.celery_app import celery_app
        assert celery_app.conf.worker_prefetch_multiplier == 1

    def test_default_queue_is_text_tasks(self):
        from app.core.celery_app import celery_app
        assert celery_app.conf.task_default_queue == "text_tasks"

    def test_serializer_is_json(self):
        from app.core.celery_app import celery_app
        assert celery_app.conf.task_serializer == "json"
        assert celery_app.conf.result_serializer == "json"

    def test_broker_url_is_amqp(self):
        """Broker must be RabbitMQ (AMQP), not Redis."""
        from app.core.celery_app import celery_app
        assert celery_app.conf.broker_url.startswith("amqp://")

    def test_result_backend_is_redis(self):
        from app.core.celery_app import celery_app
        assert celery_app.conf.result_backend.startswith("redis://")
