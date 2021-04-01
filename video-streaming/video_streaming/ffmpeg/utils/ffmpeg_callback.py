import json
import time
import sys
import psutil
from celery import Task, states
from video_streaming.core.constants import CacheKeysTemplates


class FfmpegCallback(object):

    def __init__(
            self,
            task: Task = None,
            task_id: str = None,
            output_id: str = None,
            request_id: str = None
            ):
        self.task = task
        # to prevent TypeError, needs sure the task id is not None
        # see https://github.com/celery/celery/issues/1996
        self.task_id = self.task.request.id if self.task.request.id else task_id
        self.first_chunk = True
        self.output_id = output_id
        self.request_id = request_id
        self.start_memory_rss = None

    def progress(self, ffmpeg_line, duration, time_, time_left, process):
        try:
            self._check_to_kill(process)
            psutil_process = psutil.Process(process.pid)
            if self.first_chunk:
                # save output status using output_id and request_id
                self.task.save_output_status(
                    self.task.output_status.PROCESSING,
                    self.output_id,
                    self.request_id)
                self._save_start_usage(psutil_process)
                self.first_chunk = False
            self._save_end_usage(psutil_process)
            self.task.save_output_progress(
                total=duration,
                current=time_,
                request_id=self.request_id,
                output_id=self.output_id
            )
            if self.task.request.called_directly:
                percent = round(time_ / duration * 100)
                sys.stdout.write(
                    f"\r{self.request_id} | {self.output_id} Processing...({percent}%) {time_} [{'#' * percent}{'-' * (100 - percent)}]"
                )
                sys.stdout.flush()
        except psutil.NoSuchProcess as e:
            print(e)

    # def ffmpeg_progress(self, ffmpeg_line, process):
    #     try:
    #         self._check_to_kill(process)
    #         psutil_process = psutil.Process(process.pid)
    #         if self.first_chunk:
    #             # save output status using output_id and request_id
    #             self.task.save_output_status(
    #                 self.task.output_status.PROCESSING,
    #                 self.output_id,
    #                 self.request_id)
    #             self._save_start_usage(psutil_process)
    #             self.first_chunk = False
    #         self._save_end_usage(psutil_process)
    #     except psutil.NoSuchProcess as e:
    #         print(e)

    def _check_to_kill(self, process):
        is_job_stop = self.request_id is not None and \
                      self.task.is_forced_to_stop(self.request_id)
        is_output_stop = self.request_id is not None and \
            self.output_id is not None and \
            self.task.is_output_forced_to_stop(self.request_id,
                                               self.output_id)

        if is_job_stop or is_output_stop:
            try:
                process.kill()
            except Exception as e:
                print(e)
            # raise inside callback, is just to finish processing,
            # so, will write 'ffmpeg executed command successfully' log
            self.task.raise_ignore(
                message=self.task.error_messages.
                TASK_WAS_FORCIBLY_STOPPED,
                state=states.REVOKED,
                request_kwargs=self.task.request.kwargs
            )

    def _save_end_usage(self, psutil_process):
        try:
            self.task.cache.set(
                CacheKeysTemplates.OUTPUT_END_PROCESSING_TIME.format(
                    request_id=self.request_id,
                    output_id=self.output_id),
                time.time())
            cpu_times = psutil_process.cpu_times()
            if cpu_times:
                self.task.cache.set(
                    CacheKeysTemplates.OUTPUT_END_CPU_TIMES.format(
                        request_id=self.request_id,
                        output_id=self.output_id),
                    json.dumps(cpu_times))

            current_memory_rss = psutil_process.memory_info().rss
            if not current_memory_rss:
                return
            if self.start_memory_rss is None:
                return
            if current_memory_rss < self.start_memory_rss:
                return
            last_memory_rss = self.task.cache.get(
                CacheKeysTemplates.OUTPUT_END_MEMORY_RSS.format(
                    request_id=self.request_id,
                    output_id=self.output_id)) or 0
            if current_memory_rss < last_memory_rss:
                return
            self.task.cache.set(
                CacheKeysTemplates.OUTPUT_END_MEMORY_RSS.format(
                    request_id=self.request_id,
                    output_id=self.output_id),
                current_memory_rss)
        except Exception as e:
            # TODO notify developer
            print(e)

    def _save_start_usage(self, psutil_process):
        try:
            self.task.cache.set(
                CacheKeysTemplates.OUTPUT_START_PROCESSING_TIME.format(
                    request_id=self.request_id,
                    output_id=self.output_id),
                time.time())
            cpu_times = psutil_process.cpu_times()
            self.task.cache.set(
                CacheKeysTemplates.OUTPUT_START_CPU_TIMES.format(
                    request_id=self.request_id,
                    output_id=self.output_id),
                json.dumps(cpu_times))
            memory_rss = psutil_process.memory_info().rss
            self.task.cache.set(
                CacheKeysTemplates.OUTPUT_START_MEMORY_RSS.format(
                    request_id=self.request_id,
                    output_id=self.output_id),
                memory_rss)
            self.start_memory_rss = memory_rss
        except Exception as e:
            # TODO notify developer
            print(e)
