from abc import ABC
from celery import Task, states
from celery.exceptions import Ignore

__all__ = [
    'BaseCeleryTask',
    'VideoStreamingTask'
]


class BaseCeleryTask(Task, ABC):

    def raise_ignore(self, message=None):
        try:
            # to trigger the task_failure signal
            raise Exception
        except Exception:
            if not self.request.called_directly:
                update_kwargs = dict(state=states.FAILURE)
                if message is not None:
                    update_kwargs['meta'] = dict(
                        exc_type='Exception',
                        exc_message=message)
                self.update_state(
                    **update_kwargs)
            raise Ignore()


class VideoStreamingTask(BaseCeleryTask, ABC):

    # TODO when task executed :
    # 1. decrease used count to remove input file when reach 0
    # 2. decrease tasks counts to call webhook when reach 0
    # 3. remove outputs file

    pass
