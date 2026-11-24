from round_robin.research.service import execute_research
from sqlalchemy.exc import SQLAlchemyError
from .app import celery_app


@celery_app.task(name="round_robin.research", acks_late=True, reject_on_worker_lost=True, soft_time_limit=90, time_limit=100)
def research_task(run_id: str):
    try:
        return execute_research(run_id)
    except SQLAlchemyError:
        # Leased reconciliation retains uncertainty; worker logs contain no SQL
        # parameters or private evidence from a failed persistence operation.
        return {"status": "storage_unavailable", "outcome": "reconcile_existing_lease"}
