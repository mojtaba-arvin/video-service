from abc import ABC
from celery import Task, states
from celery.exceptions import Ignore


__all__ = [
    'BaseTask'
]


class BaseTask(Task, ABC):

    def __call__(self, *args, **kwargs):
        print(f"task name: {self.name}, args: {args}, kwargs: {kwargs}")
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
