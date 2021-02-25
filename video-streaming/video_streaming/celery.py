"""Extensions registry

All extensions here are used as singletons
"""

from celery import Celery
from . import settings


# celery instance
celery_app = Celery()
celery_app.config_from_object(settings, namespace='CELERY')
celery_app.autodiscover_tasks(settings.AUTO_DISCOVER_TASKS)

