from abc import ABC
from celery import Task, states
from celery.exceptions import Ignore
from celery.utils.log import get_task_logger


logger = get_task_logger(__name__)


__all__ = [
    'BaseTask'
]


class BaseTask(Task, ABC):

    def __call__(self, *args, **kwargs):
        # Task arguments can contain signed URLs, credentials and user data.
        # Log execution identity only; never serialize raw arguments.
        try:
            task_id = getattr(self.request, "id", None)
        except (AttributeError, RuntimeError):
            task_id = None
        logger.info(
            "Executing Celery task",
            extra={
                "task_name": self.name,
                "task_id": task_id,
            },
        )
        return super().__call__(*args, **kwargs)

    def raise_ignore(self,
                     message=None,
                     state=states.FAILURE,
                     request_kwargs: dict = None):

        try:
            # to trigger the task_failure signal
            raise Exception
        except Exception:
            if not self.request.called_directly:
                update_kwargs = dict(state=state)
                if message is not None:
                    update_kwargs['meta'] = dict(
                        exc_type='Exception',
                        exc_message=message)
                self.update_state(**update_kwargs)
            raise Ignore()
