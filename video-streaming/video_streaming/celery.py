"""Extensions registry

All extensions here are used as singletons
"""
from abc import ABC
from celery import Task, Celery, states
from celery.exceptions import Ignore
from video_streaming import settings


class CeleryTask(Task, ABC):

    # TODO cwhen task executed :
    # 1. decrease used count to remove input file when reach 0
    # 2. decrease tasks counts to call webhook when reach 0

    def raise_ignore(self, message=None):
        try:
            # to trigger the task_failure signal
            raise Exception
        except Exception:
            update_kwargs = dict(state=states.FAILURE)
            if message is not None:
                update_kwargs['meta'] = dict(
                    exc_type='Exception',
                    exc_message=message)
            self.update_state(
                **update_kwargs)
            raise Ignore()


# celery instance
celery_app = Celery('tasks', task_cls='video_streaming.celery:CeleryTask')
celery_app.config_from_object(settings, namespace='CELERY')
celery_app.autodiscover_tasks(settings.AUTO_DISCOVER_TASKS)

