from celery import result as celery_result, chain, group
from google.protobuf import reflection
from video_streaming.cache import RedisCache
from video_streaming.core.constants import CacheKeysTemplates, \
    PrimaryStatus
from video_streaming.ffmpeg import tasks
from video_streaming.ffmpeg.constants import VideoEncodingFormats
from video_streaming.grpc.protos import streaming_pb2


class CreateJobMixin(object):

    cache: RedisCache
    pb2: streaming_pb2

    def _get_format(self, output):
        encode_format = output.options.WhichOneof('encode_format')
        video_codec = None
        audio_codec = None

        if encode_format == VideoEncodingFormats.H264:
            video_codec, audio_codec = self._codec_names(
                format_cls=self.pb2.H264,
                video_codec_id=output.options.h264.video_codec,
                audio_codec_id=output.options.h264.audio_codec
            )
        elif encode_format == VideoEncodingFormats.HEVC:
            video_codec, audio_codec = self._codec_names(
                format_cls=self.pb2.Hevc,
                video_codec_id=output.options.hevc.video_codec,
                audio_codec_id=output.options.hevc.audio_codec
            )
        elif encode_format == VideoEncodingFormats.VP9:
            video_codec, audio_codec = self._codec_names(
                format_cls=self.pb2.Vp9,
                video_codec_id=output.options.vp9.video_codec,
                audio_codec_id=output.options.vp9.audio_codec
            )
        return encode_format, video_codec, audio_codec

    @staticmethod
    def _codec_names(
            format_cls: reflection.GeneratedProtocolMessageType,
            video_codec_id: int,
            audio_codec_id: int):
        video_codec = format_cls.VideoCodec.__dict__['_enum_type'].values[video_codec_id].name.lower()

        # 'libvpx-vp9' format due to the dashed line in the name, it is
        # defined as underline in the proto, so here just map it to correct name
        if video_codec == 'libvpx_vp9':
            video_codec = 'libvpx-vp9'

        audio_codec = format_cls.AudioCodec.__dict__['_enum_type'].values[audio_codec_id].name.lower()
        return video_codec.lower(), audio_codec

    @staticmethod
    def _has_create_flag(output_bucket, request_outputs):
        # check bucket has one create flag
        for output in request_outputs:
            if output.s3.bucket == output_bucket and \
                    output.s3.create_bucket:
                return True
        return False

    def _parse_quality_names(self, qualities):
        quality_names = []
        for idx in qualities:
            quality_names.append(
                (
                    self.pb2.
                    QualityName.
                    __dict__['_enum_type'].
                    values[idx].
                    name
                ).lower()[2:]
                # remove "r_" from the name
            )
        # sample : ["360p", "480p", "720p"]
        return quality_names

    @staticmethod
    def _parse_custom_qualities(qualities):
        custom_qualities = []
        for quality in qualities:

            size = None
            # width and height can not be zero
            if quality.size.width and quality.size.height:
                size = [quality.size.width, quality.size.height]

            bitrate = None
            # bitrate required one of (video, audio or overall) at least
            if quality.bitrate.overall or \
                    quality.bitrate.video or \
                    quality.bitrate.audio:
                bitrate = [
                    quality.bitrate.video or None,  # map 0 to None
                    quality.bitrate.audio or None,
                    quality.bitrate.overall or None
                ]

            # When size and bitrate are None, not append
            # quality_dict to the custom_qualities
            if not size and not bitrate:
                continue

            quality_dict = dict(
                size=size,
                bitrate=[
                    quality.bitrate.video,
                    quality.bitrate.audio,
                    quality.bitrate.overall]
            )
            custom_qualities.append(quality_dict)
        # sample : [dict(size=[256, 144], bitrate=[97280, 65536])]
        return custom_qualities

    def _append_tasks(self,
                      request_id: str,
                      outputs: streaming_pb2.StreamingOutput,
                      append_to: list[chain]
                      ):
        """initial processing tasks by output formats
        and chains them with upload task
        """
        for output_number, output in enumerate(outputs):
            encode_format, video_codec, audio_codec = self._get_format(
                output)
            quality_names: list[str] = self._parse_quality_names(
                output.options.quality_names)
            custom_qualities: list[
                dict] = self._parse_custom_qualities(
                output.options.custom_qualities)

            # append to third_level_tasks
            append_to.append(
                # chain of create playlist and upload_directory
                chain(
                    # input_path will come from second level
                    tasks.create_playlist.s(
                        s3_output_key=output.s3.key,
                        fragmented=output.options.fmp4,
                        # just for HLS type
                        encode_format=encode_format,
                        video_codec=video_codec,
                        audio_codec=audio_codec,
                        quality_names=quality_names,
                        custom_qualities=custom_qualities,
                        request_id=request_id,
                        output_number=output_number,
                        is_hls=output.protocol == self.pb2.Protocol.HLS
                    ),
                    # directory will come from create playlist task
                    tasks.upload_directory.s(
                        s3_output_key=output.s3.key,
                        s3_output_bucket=output.s3.bucket,
                        request_id=request_id,
                        output_number=output_number
                    )
                )
            )
        return append_to

    def _apply_job(
            self,
            request_id,
            ordered_levels):

        # TODO delete local files on failure callback

        job = chain(*[group(*level_tasks) for level_tasks in ordered_levels])

        # set first primary status as QUEUING_CHECKS
        self.cache.set(
            CacheKeysTemplates.PRIMARY_STATUS.format(
                request_id=request_id),
            PrimaryStatus.QUEUING_CHECKS
        )

        # apply tasks
        result = job.apply_async()

        # # check last level has one task or more to detect result type
        # # is GroupResult or AsyncResult
        # if len(ordered_levels[-1]) > 1:
        #     result: celery_result.GroupResult
        #     result.save()

        # saving celery result id of the request
        self.cache.set(
            CacheKeysTemplates.REQUEST_RESULT_ID.format(
                request_id=request_id
            ),
            str(result.id)
        )

    def _job_response(self, request_id):
        response = self.pb2.JobResponse()
        response.tracking_id = request_id
        return response
