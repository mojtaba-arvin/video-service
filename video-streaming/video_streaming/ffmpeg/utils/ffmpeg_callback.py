import sys
from celery import Task


class FfmpegCallback(object):

    def __init__(
            self,
            task: Task = None,
            task_id: str = None,
            is_hls: bool = None
            ):
        self.task = task

        # to prevent TypeError, needs sure the task id is not None
        # see https://github.com/celery/celery/issues/1996
        self.task_id = self.task.request.id if self.task.request.id else task_id

        self.is_hls = is_hls

        self.first_chunk = True

    def progress(self, ffmpeg, duration, time_, time_left, process):

        if self.task.is_forced_to_stop():
            process.kill()
            # raise inside callback, is just to finish processing,
            # so, will write 'ffmpeg executed command successfully' log
            self.task.raise_revoked()

        if self.first_chunk:
            # save output status using output_number and request_id
            self.task.save_output_status(
                self.task.output_status.PROCESSING)
            self.first_chunk = False

        self.task.save_output_progress(
            total=duration,
            current=time_
        )

        if self.task.request.called_directly:
            percent = round(time_ / duration * 100)
            sys.stdout.write(
                f"\rProcessing...({percent}%) {time_} [{'#' * percent}{'-' * (100 - percent)}]"
            )
            sys.stdout.flush()

