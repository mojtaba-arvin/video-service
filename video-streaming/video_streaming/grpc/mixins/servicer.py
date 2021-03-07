from google.protobuf import reflection
from video_streaming.cache import RedisCache
from video_streaming.core.constants import CacheKeysTemplates
from video_streaming.ffmpeg.constants import VideoEncodingFormats
from video_streaming.grpc.protos import streaming_pb2, streaming_pb2_grpc


class BaseGrpcServiceMixin(object):

    cache = RedisCache()
    pb2 = streaming_pb2

    def _add_to_server(self, server):
        streaming_pb2_grpc.add_StreamingServicer_to_server(
            self.__class__(),
            server)

    def _get_format(self, output):
        encode_format = output.options.WhichOneof('encode_format')
        video_codec = None
        audio_codec = None

        if encode_format == VideoEncodingFormats.H264:
            video_codec, audio_codec = self.__class__._codec_names(
                format_cls=self.pb2.H264,
                video_codec_id=output.options.h264.video_codec,
                audio_codec_id=output.options.h264.audio_codec
            )
        elif encode_format == VideoEncodingFormats.HEVC:
            video_codec, audio_codec = self.__class__._codec_names(
                format_cls=self.pb2.Hevc,
                video_codec_id=output.options.hevc.video_codec,
                audio_codec_id=output.options.hevc.audio_codec
            )
        elif encode_format == VideoEncodingFormats.VP9:
            video_codec, audio_codec = self.__class__._codec_names(
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
                # remove first character "r_" from the name
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

    def _outputs(self, request_id, total_outputs):
        outputs: list[BaseGrpcServiceMixin.pb2.OutputProgress] = []
        for output_number in range(total_outputs):
            output_status = self.cache.get(
                CacheKeysTemplates.OUTPUT_STATUS.format(
                    request_id=request_id,
                    output_number=output_number),
                decode=False)
            if output_status:
                progress: dict = self.cache.get(
                    CacheKeysTemplates.OUTPUT_PROGRESS.format(
                        request_id=request_id,
                        output_number=output_number)) or {}
                outputs.append(
                    self.pb2.OutputProgress(
                        id=output_number,
                        status=self.pb2.OutputStatus.Value(
                            output_status),
                        **progress))
        return outputs

    def _inputs(self, request_id, total_inputs):
        inputs: list[BaseGrpcServiceMixin.pb2.InputProgress] = []
        for input_number in range(total_inputs):
            input_status: str = self.cache.get(
                CacheKeysTemplates.INPUT_STATUS.format(
                    request_id=request_id,
                    input_number=input_number),
                decode=False)
            if input_status:
                progress: dict = self.cache.get(
                    CacheKeysTemplates.INPUT_DOWNLOADING_PROGRESS.format(
                        request_id=request_id,
                        input_number=input_number)) or {}
                inputs.append(
                    self.pb2.InputProgress(
                        id=input_number,
                        status=self.pb2.InputStatus.Value(
                            input_status),
                        **progress))
        return inputs
