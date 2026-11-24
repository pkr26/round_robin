import os

from celery import Celery


celery_app = Celery("round_robin", broker=os.environ.get("ROUND_ROBIN_BROKER_URL", "redis://127.0.0.1:6379/0"), include=["round_robin.worker.tasks", "round_robin.worker.research_tasks"])
celery_app.conf.update(
    task_serializer="json", accept_content=["json"], result_serializer="json",
    task_ignore_result=True, task_acks_late=True, task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1, task_soft_time_limit=115, task_time_limit=120,
    broker_transport_options={"visibility_timeout": 180, "socket_connect_timeout": 3, "socket_timeout": 3},
    broker_connection_retry_on_startup=True, broker_connection_max_retries=3,
    task_publish_retry=False, task_track_started=False,
    worker_hijack_root_logger=False, task_default_queue=os.environ.get("ROUND_ROBIN_TASK_QUEUE", "round_robin"),
)
