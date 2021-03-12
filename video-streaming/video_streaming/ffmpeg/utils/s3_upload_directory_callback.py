import sys
from celery import Task


class S3UploadDirectoryCallback(object):

    def __init__(
            self,
            task: Task = None,
            task_id: str = None,
            output_number: int = None,
            request_id: str = None
            ):
        self.uploaded = 0
        self.task = task

        # to prevent TypeError, needs sure the task id is not None
        # see https://github.com/celery/celery/issues/1996
        self.task_id = self.task.request.id if self.task.request.id else task_id

        self.output_number = output_number
        self.request_id = request_id

    def progress(self, total_size, total_files, number, chunk):
        if self.uploaded == 0:
            # save input status using input_number and request_id
            self.task.save_output_status(
                self.task.output_status.PLAYLIST_UPLOADING,
                self.output_number,
                self.request_id
            )

        self.uploaded += chunk

        self.task.save_output_progress(
            total=total_size,
            current=self.uploaded,
            request_id=self.request_id,
            output_number=self.output_number
        )

        if self.task.request.called_directly:
            bytes_percent = round(self.uploaded / total_size * 100)
            sys.stdout.write(
                f"\r{self.request_id} | {self.output_number} Uploading {number}/{total_files} files...({bytes_percent}%) {self.uploaded} [{'#' * bytes_percent}{'-' * (100 - bytes_percent)}]"
            )
            sys.stdout.flush()

