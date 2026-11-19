from celery import Celery
from video_streaming import settings


# celery instance
celery_app = Celery('tasks', task_cls=settings.BASE_TASK_CLASS)
celery_app.config_from_object(settings, namespace='CELERY')
celery_app.autodiscover_tasks(settings.AUTO_DISCOVER_TASKS)

# Ensure group/chain helpers imported from celery use this configured app.
celery_app.set_default()
celery_app.set_current()

