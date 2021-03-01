import sys
from celery import Task
from video_streaming.core.celery import custom_states


class FfmpegCallback(object):

    def __init__(
            self,
            task: Task = None,
            task_id: str = None
            ):
        self.task = task

        # to prevent TypeError, needs sure the task id is not None
        # see https://github.com/celery/celery/issues/1996
        self.task_id = self.task.request.id if self.task.request.id else task_id

    def progress(self, ffmpeg, duration, time_, time_left, process):
        if self.task.request.called_directly:
            percent = round(time_ / duration * 100)
            sys.stdout.write(
                f"\rProcessing...({percent}%) {time_} [{'#' * percent}{'-' * (100 - percent)}]"
            )
            sys.stdout.flush()

        if self.task and self.task_id:

            # update state
            current_state = custom_states.VideoProcessingState().create(
                progress_total=duration,
                progress_current=time_,
                task_id=self.task_id
            )
            self.task.update_state(**current_state)
