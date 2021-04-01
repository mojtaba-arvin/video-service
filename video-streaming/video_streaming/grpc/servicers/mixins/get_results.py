import pprint

from ffmpeg_streaming.ffprobe import Streams
from video_streaming.cache import RedisCache
from video_streaming.core.constants import CacheKeysTemplates, \
    PrimaryStatus
from video_streaming.grpc.protos import streaming_pb2


class GetResultsMixin(object):

    cache: RedisCache
    pb2: streaming_pb2

    @staticmethod
    def get_cpu_usage(start: list, end: list):

        # iowait: (Linux) time spent waiting for blocking I/O to
        # complete. This value is excluded from user and system times
        # count (because the CPU is not doing any work).
        start_iowait = start.pop()
        end_iowait = end.pop()

        # total CPU time at start (including idle time)
        start_total = sum(start)
        # calculates the busy CPU time at start
        start_busy = start_total - start_iowait

        # total CPU time at end (including idle time)
        end_total = sum(end)
        # calculates the busy CPU time at end
        end_busy = end_total - end_iowait

        if end_busy <= start_busy:
            return 0.0

        busy_delta = end_busy - start_busy
        return busy_delta

    def _playlists(self, request_id, output_ids, outputs):
        for output_id in output_ids:
            output_status = self.cache.get(
                CacheKeysTemplates.OUTPUT_STATUS.format(
                    request_id=request_id,
                    output_id=output_id),
                decode=False)
            if output_status:
                directory_size: int = self.cache.get(
                    CacheKeysTemplates.OUTPUT_SIZE.format(
                        request_id=request_id,
                        output_id=output_id)) or 0
                output_details = dict(
                    id=output_id,
                    status=self.pb2.OutputStatus.Value(
                        output_status),
                    directory_size=directory_size,
                )
                progress: dict = self.cache.get(
                    CacheKeysTemplates.OUTPUT_PROGRESS.format(
                        request_id=request_id,
                        output_id=output_id))
                if progress:
                    output_details['output_progress'] = self.pb2.\
                        Progress(**progress)

                start_progressing_times: float = self.cache.get(
                    CacheKeysTemplates.OUTPUT_START_PROCESSING_TIME.format(
                        request_id=request_id,
                        output_id=output_id))
                end_progressing_times: float = self.cache.get(
                    CacheKeysTemplates.OUTPUT_END_PROCESSING_TIME.format(
                        request_id=request_id,
                        output_id=output_id))
                if start_progressing_times and end_progressing_times:
                    output_details['spent_time'] = end_progressing_times - start_progressing_times
                    start_cpu_times: list = self.cache.get(
                        CacheKeysTemplates.OUTPUT_START_CPU_TIMES.format(
                            request_id=request_id,
                            output_id=output_id))
                    end_cpu_times: list = self.cache.get(
                        CacheKeysTemplates.OUTPUT_END_CPU_TIMES.format(
                            request_id=request_id,
                            output_id=output_id))
                    if start_cpu_times and end_cpu_times:
                        output_details['cpu_usage'] = self.get_cpu_usage(
                            list(start_cpu_times),
                            list(end_cpu_times)
                        )
                    start_memory_rss: int = self.cache.get(
                        CacheKeysTemplates.OUTPUT_START_MEMORY_RSS.format(
                            request_id=request_id,
                            output_id=output_id))
                    end_memory_rss: int = self.cache.get(
                        CacheKeysTemplates.OUTPUT_END_MEMORY_RSS.format(
                            request_id=request_id,
                            output_id=output_id))
                    if start_memory_rss and end_memory_rss:
                        output_details['memory_usage'] = end_memory_rss - start_memory_rss

                outputs.append(self.pb2.PlaylistDetails(**output_details))

    def _thumbnails(self, request_id, output_ids, outputs):
        for output_id in output_ids:
            output_status = self.cache.get(
                CacheKeysTemplates.OUTPUT_STATUS.format(
                    request_id=request_id,
                    output_id=output_id),
                decode=False)
            if output_status:
                file_size: int = self.cache.get(
                    CacheKeysTemplates.OUTPUT_SIZE.format(
                        request_id=request_id,
                        output_id=output_id)) or 0
                output_details = dict(
                    id=output_id,
                    status=self.pb2.OutputStatus.Value(
                        output_status),
                    file_size=file_size,
                )
                outputs.append(self.pb2.ThumbnailDetails(**output_details))

    def _watermarked_video(
            self,
            request_id
            ) -> None or streaming_pb2.WatermarkedVideoDetails:
        # output id of watermarked video
        output_id = CacheKeysTemplates. \
            WATERMARKED_VIDEO_OUTPUT_ID.format(number=0)
        output_status = self.cache.get(
            CacheKeysTemplates.OUTPUT_STATUS.format(
                request_id=request_id,
                output_id=output_id),
            decode=False)
        if output_status:
            file_size: int = self.cache.get(
                CacheKeysTemplates.OUTPUT_SIZE.format(
                    request_id=request_id,
                    output_id=output_id)) or 0
            output_details = dict(
                id=output_id,
                status=self.pb2.OutputStatus.Value(
                    output_status),
                file_size=file_size
            )
            progress: dict = self.cache.get(
                CacheKeysTemplates.OUTPUT_PROGRESS.format(
                    request_id=request_id,
                    output_id=output_id))
            if progress:
                output_details['output_progress'] = self.pb2. \
                    Progress(**progress)

            start_progressing_times: float = self.cache.get(
                CacheKeysTemplates.OUTPUT_START_PROCESSING_TIME.format(
                    request_id=request_id,
                    output_id=output_id))
            end_progressing_times: float = self.cache.get(
                CacheKeysTemplates.OUTPUT_END_PROCESSING_TIME.format(
                    request_id=request_id,
                    output_id=output_id))
            if start_progressing_times and end_progressing_times:
                output_details['spent_time'] = end_progressing_times - start_progressing_times
                start_cpu_times: list = self.cache.get(
                    CacheKeysTemplates.OUTPUT_START_CPU_TIMES.format(
                        request_id=request_id,
                        output_id=output_id))
                end_cpu_times: list = self.cache.get(
                    CacheKeysTemplates.OUTPUT_END_CPU_TIMES.format(
                        request_id=request_id,
                        output_id=output_id))
                if start_cpu_times and end_cpu_times:
                    output_details['cpu_usage'] = self.get_cpu_usage(
                        list(start_cpu_times),
                        list(end_cpu_times)
                    )
                start_memory_rss: int = self.cache.get(
                    CacheKeysTemplates.OUTPUT_START_MEMORY_RSS.format(
                        request_id=request_id,
                        output_id=output_id))
                end_memory_rss: int = self.cache.get(
                    CacheKeysTemplates.OUTPUT_END_MEMORY_RSS.format(
                        request_id=request_id,
                        output_id=output_id))
                if start_memory_rss and end_memory_rss:
                    output_details['memory_usage'] = end_memory_rss - start_memory_rss

            return self.pb2.WatermarkedVideoDetails(**output_details)

    def _inputs(self, request_id, total_inputs):
        inputs: list[streaming_pb2.InputDetails] = []
        for input_number in range(total_inputs):
            input_status: str = self.cache.get(
                CacheKeysTemplates.INPUT_STATUS.format(
                    request_id=request_id,
                    input_number=input_number),
                decode=False)
            if input_status:
                input_details: dict = dict(
                    id=input_number,
                    status=self.pb2.InputStatus.Value(
                        input_status),
                    )
                progress: dict = self.cache.get(
                    CacheKeysTemplates.INPUT_DOWNLOADING_PROGRESS.format(
                        request_id=request_id,
                        input_number=input_number)) or {}
                if progress:
                    input_details['input_progress'] = self.pb2.\
                        Progress(**progress)
                ffprobe_data: dict = self.cache.get(
                    CacheKeysTemplates.INPUT_FFPROBE_DATA.format(
                        request_id=request_id,
                        input_number=input_number
                    ))
                if ffprobe_data:
                    format_: dict = ffprobe_data['format']
                    streams = Streams(ffprobe_data['streams'])
                    file_info = self.pb2.OriginalFileInfo(
                        general=self.pb2.GeneralInfo(
                            duration=float(format_['duration']),
                            file_size=int(format_['size']),
                            bit_rate=int(format_['bit_rate']),
                            file_formats=format_['format_name'],
                        ),
                        video=self.pb2.VideoInfo(
                            codec=streams.video()['codec_name'],
                            width=int(streams.video().get('width', 0)),
                            height=int(
                                streams.video().get('height', 0)),
                            frame_rate=streams.video()['r_frame_rate'],
                            bit_rate=int(streams.video()['bit_rate'])
                        ),
                        audio=self.pb2.AudioInfo(
                            codec=streams.audio()['codec_name'],
                            sample_rate=int(
                                streams.audio()['sample_rate']),
                            bit_rate=int(streams.audio()['bit_rate']),
                            channel_layout=streams.audio()[
                                'channel_layout']
                        )
                    )
                    input_details['file_info'] = file_info
                inputs.append(
                    self.pb2.InputDetails(**input_details))
        return inputs

    def _get_result(self,
                    request_id: str
                    ) -> None or streaming_pb2.ResultDetails:
        primary_status: str = self.cache.get(
            CacheKeysTemplates.PRIMARY_STATUS.format(
                request_id=request_id), decode=False)
        job_details: dict = self.cache.get(
            CacheKeysTemplates.JOB_DETAILS.format(
                request_id=request_id))
        if primary_status and job_details:
            status = self.pb2.PrimaryStatus.Value(
                primary_status)
            reference_id: str = job_details['reference_id']
            total_checks: int = job_details['total_checks']
            total_inputs: int = job_details['total_inputs']
            total_outputs: int = job_details['total_outputs']
            # total_playlists: int = job_details['total_playlists']
            # total_thumbnails: int = job_details['total_thumbnails']

            ready_outputs: int = self.cache.get(
                CacheKeysTemplates.READY_OUTPUTS.format(
                    request_id=request_id)) or 0
            revoked_outputs: int = self.cache.get(
                CacheKeysTemplates.REVOKED_OUTPUTS.format(
                    request_id=request_id)) or 0
            failed_outputs: int = self.cache.get(
                CacheKeysTemplates.FAILED_OUTPUTS.format(
                    request_id=request_id)) or 0
            checks = self.pb2.Checks(
                total=total_checks,
                passed=self.cache.get(
                    CacheKeysTemplates.PASSED_CHECKS.format(
                        request_id=request_id)) or 0)

            playlists_outputs: list[streaming_pb2.PlaylistDetails] = []
            thumbnails_outputs: list[streaming_pb2.ThumbnailDetails] = []

            self._playlists(
                request_id,
                job_details['no_watermarked_playlists_ids'],
                playlists_outputs)
            self._playlists(
                request_id,
                job_details['watermarked_playlists_ids'],
                playlists_outputs)

            self._thumbnails(
                request_id,
                job_details['no_watermarked_thumbnails_ids'],
                thumbnails_outputs)
            self._thumbnails(
                request_id,
                job_details['watermarked_thumbnails_ids'],
                thumbnails_outputs)

            result_details = dict(
                tracking_id=request_id,
                reference_id=reference_id,
                status=status,
                total_outputs=total_outputs,
                revoked_outputs=revoked_outputs,
                ready_outputs=ready_outputs,
                failed_outputs=failed_outputs,
                checks=checks,
                inputs=self._inputs(request_id, total_inputs),
                playlists=self.pb2.Playlists(
                    outputs=playlists_outputs),
                thumbnails=self.pb2.Thumbnails(
                    outputs=thumbnails_outputs),
                watermarked_video=self._watermarked_video(request_id)
            )
            # get stop reason if primary status is FAILED or REVOKED
            if primary_status in [
                    PrimaryStatus.FAILED,
                    PrimaryStatus.REVOKED]:
                stop_reason: str = self.cache.get(
                    CacheKeysTemplates.STOP_REASON.format(
                        request_id=request_id), decode=False)
                if stop_reason:
                    result_details['reason'] = self.pb2.StopReason.Value(
                        stop_reason)

            return self.pb2.ResultDetails(**result_details)

    def _get_results(self, request, context):
        results: list[streaming_pb2.ResultDetails] = []
        for request_id in request.tracking_ids:
            result = self._get_result(request_id)
            if result:
                results.append(result)
        return self.pb2.JobsResultsResponse(results=results)
