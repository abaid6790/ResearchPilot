"""Celery worker entry point.

celery -A jobs.worker.celery_app worker --loglevel=info --concurrency=2
(Windows: add  --pool=threads )
"""

from app import create_app
from jobs.celery_app import make_worker_app

flask_app = create_app()
celery_app = make_worker_app(flask_app)
