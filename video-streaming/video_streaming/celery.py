"""Extensions registry

All extensions here are used as singletons
"""

from celery import Celery, states
from celery.exceptions import Ignore

from . import settings


class CeleryApplication(Celery):

    def raise_ignore(self, message=None):
        try:
            # to trigger the task_failure signal
            raise Exception
        except Exception:
            update_kwargs = dict(state=states.FAILURE)
            if message is not None:
                update_kwargs['meta'] = dict(exc_message=message)
            self.update_state(
                **update_kwargs)
            raise Ignore()


# celery instance
celery_app = Celery()
celery_app.config_from_object(settings, namespace='CELERY')
celery_app.autodiscover_tasks(settings.AUTO_DISCOVER_TASKS)

