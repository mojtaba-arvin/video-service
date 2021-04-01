import sys
from celery import Task


class S3DownloadCallback(object):

    def __init__(
            self,
            object_size,
            task: Task = None,
            task_id: str = None,
            input_number: int = None,
            request_id: str = None
            ):

        self.downloaded = 0
        self._object_size = object_size
        self.task = task

        # to prevent TypeError, needs sure the task id is not None
        # see https://github.com/celery/celery/issues/1996
        self.task_id = self.task.request.id if self.task.request.id else task_id

        self.input_number = input_number
        self.request_id = request_id

    def progress(self, chunk):
        if self.downloaded == 0:
            # save input status using input_number and request_id
            self.task.save_input_status(
                self.task.input_status.DOWNLOADING,
                self.input_number,
                self.request_id)

        self.downloaded += chunk

        self.task.save_input_downloading_progress(
            total=self._object_size,
            current=self.downloaded,
            request_id=self.request_id,
            input_number=self.input_number
        )

        if self.task.request.called_directly:
            percent = round(self.downloaded / self._object_size * 100)
            sys.stdout.write(
                f"\rDownloading...({percent}%) {self.downloaded} [{'#' * percent}{'-' * (100 - percent)}]"
            )
            sys.stdout.flush()

