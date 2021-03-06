import json
import sys
from celery import Task
# from video_streaming.core.celery import custom_states
from video_streaming.core.constants.cache_keys import CacheKeysTemplates


class S3DownloadCallback(object):
    def __init__(
            self,
            object_size,
            task: Task = None,
            task_id: str = None
            ):

        self.downloaded = 0
        self._object_size = object_size
        self.task = task

        # to prevent TypeError, needs sure the task id is not None
        # see https://github.com/celery/celery/issues/1996
        self.task_id = self.task.request.id if self.task.request.id else task_id

    def progress(self, chunk):
        if self.downloaded == 0:
            # save input step using input_number and request_id
            self.task.save_input_step(
                self.task.input_steps.DOWNLOADING)

        self.downloaded += chunk

        self.task.save_input_downloading_progress(
            total=self._object_size,
            current=self.downloaded
        )

        if self.task.request.called_directly:
            percent = round(self.downloaded / self._object_size * 100)
            sys.stdout.write(
                f"\rDownloading...({percent}%) {self.downloaded} [{'#' * percent}{'-' * (100 - percent)}]"
            )
            sys.stdout.flush()

        # if self.task and self.task_id:
        #     # update state
        #     current_state = custom_states.DownloadingVideoState().create(
        #         progress_total=self._object_size,
        #         progress_current=self.downloaded,
        #         task_id=self.task_id
        #     )
        #     self.task.update_state(**current_state)
