import uuid
from google.protobuf import reflection
from celery import chain, group
from video_streaming.core.services import S3Service
from video_streaming.ffmpeg import tasks
from video_streaming.ffmpeg.constants import VideoEncodingFormats
from video_streaming.grpc import exceptions
from video_streaming.grpc.exceptions import \
    DuplicateOutputLocationsException
from video_streaming.grpc.protos import streaming_pb2, streaming_pb2_grpc


class Streaming(streaming_pb2_grpc.StreamingServicer):

    @staticmethod
    def codec_names(
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
    def has_create_flag(output_bucket, request_outputs):
        # check bucket has one create flag
        for output in request_outputs:
            if output.s3.bucket == output_bucket and \
                    output.s3.create_bucket:
                return True
        return False

    def _get_format(self, output):
        encode_format = output.options.WhichOneof('encode_format')
        video_codec = None
        audio_codec = None

        if encode_format == VideoEncodingFormats.H264:
            video_codec, audio_codec = self.__class__.codec_names(
                format_cls=streaming_pb2.H264,
                video_codec_id=output.options.h264.video_codec,
                audio_codec_id=output.options.h264.audio_codec
            )
        elif encode_format == VideoEncodingFormats.HEVC:
            video_codec, audio_codec = self.__class__.codec_names(
                format_cls=streaming_pb2.Hevc,
                video_codec_id=output.options.hevc.video_codec,
                audio_codec_id=output.options.hevc.audio_codec
            )
        elif encode_format == VideoEncodingFormats.VP9:
            video_codec, audio_codec = self.__class__.codec_names(
                format_cls=streaming_pb2.Vp9,
                video_codec_id=output.options.vp9.video_codec,
                audio_codec_id=output.options.vp9.audio_codec
            )
        return encode_format, video_codec, audio_codec

    @staticmethod
    def _parse_quality_names(qualities):
        quality_names = []
        for idx in qualities:
            quality_names.append(
                (
                    streaming_pb2.
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

    def video_processor(self, request, context):
        """

        1. create empty tasks lists for the workflow
        2. generate unique uuid for the current request
        3. check input video parameter is not empty
        4. check input video is exist on the cloud as a first level task
            on the workflow
        5. check outputs list is empty
        6. check output keys are not empty and bucket names are
            compatible with s3
        7. check if output keys are exist can be replace as first level
            tasks
        8. check duplicate output locations in current request
        9. check unique output buckets are exist on the cloud or create
            them if has one create flag ,as first level tasks
        10. add second level tasks, e.g. download input
        11. initial processing tasks by output formats and chains them with upload task
        12. apply tasks
        """

        # 1. create empty tasks lists for the workflow

        # some checks tasks before download anythings
        first_level_tasks = []
        # some download tasks, like target video, watermarks ...
        second_level_tasks = []
        # some chains of create playlist and upload directory for every output
        third_level_tasks = []

        # 2.generate unique uuid for the current request
        request_id = str(uuid.uuid4())
        # reference_id = request.reference_id
        # webhook_url = request.webhook_url

        # 3. check input video parameter is not empty

        s3_input_key = request.s3_input.key
        s3_input_bucket = request.s3_input.bucket

        # For strings in proto3, the default value is the empty string
        # check input key to not be empty string
        if s3_input_key.isspace():
            raise exceptions.S3KeyCanNotBeEmptyException

        # 4. check input video is exist on the cloud
        # as a first level task
        first_level_tasks.append(
            tasks.check_input_key.s(
                s3_input_key=s3_input_key,
                s3_input_bucket=s3_input_bucket
            ))

        # 5. check outputs list is empty
        if not request.outputs:
            raise exceptions.OneOutputIsRequiredException

        # 6. check output keys are not empty and bucket names
        # are compatible with s3
        output_buckets = []
        output_locations = []
        for output in request.outputs:

            # check the output key is filled
            if output.s3.key.isspace():
                raise exceptions.S3KeyCanNotBeEmptyException

            # when output bucket is filled and has create flag,
            # then check the output bucket is compatible with s3
            if output.s3.bucket and output.s3.create_bucket and \
                    not S3Service.validate_bucket_name(output.s3.bucket):
                raise exceptions.BucketNameIsNotValidException

            # to use for getting unique output buckets names
            output_buckets.append(output.s3.bucket)

            # to use for checking duplicate output locations
            output_locations.append((output.s3.bucket, output.s3.key))

            # 7. check if output keys are exist can be replace
            # as first level tasks
            first_level_tasks.append(
                tasks.check_output_key.s(
                    s3_output_key=output.s3.key,
                    s3_output_bucket=output.s3.bucket,
                    s3_dont_replace=output.s3.dont_replace
                )
            )

        # 8. check duplicate output locations in current request
        if len(output_locations) != len(set(output_locations)):
            raise DuplicateOutputLocationsException

        # 9. check unique output buckets are exist on the cloud or
        # create them if has one create flag ,as first level tasks
        unique_output_buckets = list(set(output_buckets))
        for bucket in unique_output_buckets:
            first_level_tasks.append(
                tasks.check_output_bucket.s(
                    s3_output_bucket=bucket,
                    s3_create_bucket=self.__class__.has_create_flag(
                        bucket, request.outputs)
                ))

        # 10. add second level tasks, e.g. download input
        second_level_tasks.append(
            # input object_details will come from first level
            tasks.download_input.s(
                # object_details=object_details,
                request_id=request_id,
                s3_input_key=s3_input_key,
                s3_input_bucket=s3_input_bucket
            )
        )

        # 11. initial processing tasks by output formats
        # and chains them with upload task
        for output in request.outputs:

            encode_format, video_codec, audio_codec = self._get_format(
                output)
            quality_names = self.__class__._parse_quality_names(
                output.options.quality_names)
            custom_qualities = self.__class__._parse_custom_qualities(
                output.options.custom_qualities)

            create_playlist = None
            if output.protocol == streaming_pb2.Protocol.HLS:
                fragmented = output.options.fmp4
                # input_path will come from second level
                create_playlist = tasks.create_hls.s(
                    s3_output_key=output.s3.key,
                    fragmented=fragmented,
                    encode_format=encode_format,
                    video_codec=video_codec,
                    audio_codec=audio_codec,
                    quality_names=quality_names,
                    custom_qualities=custom_qualities
                )
            else:
                # MPEG-Dash Protocol

                # input_path will come from second level
                create_playlist = tasks.create_hls.s(
                    s3_output_key=output.s3.key,
                    encode_format=encode_format,
                    video_codec=video_codec,
                    audio_codec=audio_codec,
                    quality_names=quality_names,
                    custom_qualities=custom_qualities
                )

            third_level_tasks.append(
                # chain of create_hls and upload_directory
                chain(
                    create_playlist,
                    # directory will come from create playlist task
                    tasks.upload_directory.s(
                        s3_output_key=output.s3.key,
                        s3_output_bucket=output.s3.bucket
                    )
                )
            )

        # 12. apply tasks

        job = chain(
            group(*first_level_tasks),
            group(*second_level_tasks),
            group(*third_level_tasks)
        )
        result = job.apply_async()
        # TODO

        response = streaming_pb2.JobResponse()
        # response.tracking_id =

        return response

    def _add_to_server(self, server):
        streaming_pb2_grpc.add_StreamingServicer_to_server(
            self.__class__(),
            server)

