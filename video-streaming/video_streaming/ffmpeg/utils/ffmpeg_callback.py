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
            is_hls: bool = None,
            output_number: int = None,
            request_id: str = None
            ):
        self.task = task

        # to prevent TypeError, needs sure the task id is not None
        # see https://github.com/celery/celery/issues/1996
        self.task_id = self.task.request.id if self.task.request.id else task_id

        self.is_hls = is_hls

        self.first_chunk = True
        self.output_number = output_number
        self.request_id = request_id

    def progress(self, ffmpeg, duration, time_, time_left, process):
        is_job_stop = self.request_id is not None and \
                      self.task.is_forced_to_stop(self.request_id)
        is_output_stop = self.request_id is not None and \
            self.output_number is not None and \
            self.task.is_output_forced_to_stop(self.request_id,
                                               self.output_number)

        if is_job_stop or is_output_stop:
            process.kill()
            # raise inside callback, is just to finish processing,
            # so, will write 'ffmpeg executed command successfully' log
            self.task.raise_ignore(
                message=self.task.error_messages.
                TASK_WAS_FORCIBLY_STOPPED,
                state=states.REVOKED,
                request_kwargs=self.task.request.kwargs
            )

        psutil_process = psutil.Process(process.pid)

        if self.first_chunk:
            # save output status using output_number and request_id
            self.task.save_output_status(
                self.task.output_status.PROCESSING,
                self.output_number,
                self.request_id)

            try:
                self.task.cache.set(
                    CacheKeysTemplates.OUTPUT_START_PROCESSING_TIME.format(
                        request_id=self.request_id,
                        output_number=self.output_number),
                    time.time())
                cpu_times = psutil_process.cpu_times()
                self.task.cache.set(
                    CacheKeysTemplates.OUTPUT_START_CPU_TIMES.format(
                        request_id=self.request_id,
                        output_number=self.output_number),
                    json.dumps(cpu_times))
                self.task.cache.set(
                    CacheKeysTemplates.OUTPUT_START_MEMORY_RSS.format(
                        request_id=self.request_id,
                        output_number=self.output_number),
                    psutil_process.memory_info().rss)
            except Exception as e:
                # TODO notify developer
                print(e)

            self.first_chunk = False

        try:
            self.task.cache.set(
                CacheKeysTemplates.OUTPUT_END_PROCESSING_TIME.format(
                    request_id=self.request_id,
                    output_number=self.output_number),
                time.time())
            cpu_times = psutil_process.cpu_times()
            if cpu_times:
                self.task.cache.set(
                    CacheKeysTemplates.OUTPUT_END_CPU_TIMES.format(
                        request_id=self.request_id,
                        output_number=self.output_number),
                    json.dumps(cpu_times))
            memory_rss = psutil_process.memory_info().rss
            if memory_rss:
                self.task.cache.set(
                    CacheKeysTemplates.OUTPUT_END_MEMORY_RSS.format(
                        request_id=self.request_id,
                        output_number=self.output_number),
                    psutil_process.memory_info().rss)
        except Exception as e:
            # TODO notify developer
            print(e)

        self.task.save_output_progress(
            total=duration,
            current=time_,
            request_id=self.request_id,
            output_number=self.output_number
        )

        if self.task.request.called_directly:
            percent = round(time_ / duration * 100)
            sys.stdout.write(
                f"\r{self.request_id} | {self.output_number} Processing...({percent}%) {time_} [{'#' * percent}{'-' * (100 - percent)}]"
            )
            sys.stdout.flush()

