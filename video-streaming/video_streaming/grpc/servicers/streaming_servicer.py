from google.protobuf import reflection
from celery import chain, group
from video_streaming.ffmpeg import tasks
from video_streaming.ffmpeg.constants import VideoEncodingFormats
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

    def video_processor(self, request, context):
        request_id = "3b06519e-1h5c-475b-p473-1c8ao63bbe58"
        s3_input_key = request.s3_input.key
        s3_input_bucket = request.s3_input.bucket

        # some checks tasks before download anythings
        first_level_tasks = [
            tasks.check_input_key.s(
                s3_input_key=s3_input_key,
                s3_input_bucket=s3_input_bucket
            )]

        # some download tasks, like target video, watermarks ...
        second_level_tasks = [
            tasks.download_input.s(
                # object_details=object_details,
                request_id=request_id,
                s3_input_key=s3_input_key,
                s3_input_bucket=s3_input_bucket
            )
        ]

        # some chains of processing and uploading each output
        third_level_tasks = []

        reference_id = request.reference_id
        # webhook_url = request.webhook_url

        for output in request.outputs:

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

            # sample : ["360p", "480p", "720p"]
            quality_names = []
            for idx in output.options.quality_names:
                quality_names.append(
                    (
                        streaming_pb2.
                        QualityName.
                        __dict__['_enum_type'].
                        values[idx].
                        name
                    ).lower()[2:]  # remove first character "r_" from the name
                )

            # sample : [dict(size=[256, 144], bitrate=[97280, 65536])]
            custom_qualities = []
            for quality in output.options.custom_qualities:

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

            s3_output_key = output.s3.key
            s3_output_bucket = output.s3.bucket
            s3_create_bucket = output.s3.create_bucket
            s3_dont_replace = output.s3.dont_replace

            first_level_tasks.extend([
                tasks.check_output_bucket.s(
                    s3_output_bucket=s3_output_bucket,
                    s3_create_bucket=s3_create_bucket
                ),
                tasks.check_output_key.s(
                    s3_output_key=s3_output_key,
                    s3_output_bucket=s3_output_bucket,
                    s3_dont_replace=s3_dont_replace
                )
            ])

            create_playlist = None
            if output.protocol == streaming_pb2.Protocol.HLS:

                # HLS protocol

                fragmented = output.options.fmp4
                # input_path will come from second level
                create_playlist = tasks.create_hls.s(
                    s3_output_key=s3_output_key,
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
                    s3_output_key=s3_output_key,
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
                        s3_output_key=s3_output_key,
                        s3_output_bucket=s3_output_bucket
                    )
                )
            )

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

