import json
import uuid
from celery import result as celery_result, chain, group, signature, \
    chord
from google.protobuf import reflection
from video_streaming.cache import RedisCache
from video_streaming.core.constants import CacheKeysTemplates, \
    PrimaryStatus
from video_streaming.core.services import S3Service
from video_streaming.ffmpeg import tasks
from video_streaming.ffmpeg.constants import VideoEncodingFormats, \
    InputType
from video_streaming.grpc import exceptions
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
    def _has_create_flag(output_bucket, *outputs):
        # check bucket has one create flag
        for output in outputs:
            if output.upload_to.bucket == output_bucket and \
                    output.upload_to.create_bucket:
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

    def _append_playlists_tasks(
            self,
            request_id: str,
            playlists: streaming_pb2.PlaylistOutput,
            no_watermarked_tasks: list[chain],
            watermarked_tasks: list[chain],
            request_has_watermark: bool,
            no_watermarked_playlists_ids: list[str],
            watermarked_playlists_ids: list[str]):
        """initial processing tasks by playlists formats
        and chains them with upload task
        """
        for number, output in enumerate(playlists):
            if output.use_watermark and not request_has_watermark:
                raise exceptions.NoWatermarkToUseException

            if output.use_watermark:
                output_id: str = CacheKeysTemplates.WATERMARKED_PLAYLIST_OUTPUT_ID.format(
                    number=number)
            else:
                output_id: str = CacheKeysTemplates.PLAYLIST_OUTPUT_ID.format(
                    number=number)

            encode_format, video_codec, audio_codec = self._get_format(
                output)
            quality_names: list[str] = self._parse_quality_names(
                output.options.quality_names)
            custom_qualities: list[dict] = self._parse_custom_qualities(
                output.options.custom_qualities)

            # chain of create playlist and upload_directory
            chain_tasks = chain(
                # video_path will come from third level
                tasks.create_playlist.s(
                    s3_output_key=output.upload_to.key,
                    fragmented=output.options.fmp4,
                    # just for HLS type
                    encode_format=encode_format,
                    video_codec=video_codec,
                    audio_codec=audio_codec,
                    quality_names=quality_names,
                    custom_qualities=custom_qualities,
                    request_id=request_id,
                    output_id=output_id,
                    is_hls=output.protocol == self.pb2.Protocol.HLS
                ),
                # directory will come from create playlist task and callback
                tasks.upload_directory.s(
                    s3_output_key=output.upload_to.key,
                    s3_output_bucket=output.upload_to.bucket,
                    request_id=request_id,
                    output_id=output_id
                ),
                tasks.call_webhook.s(request_id=request_id)
            )
            if output.use_watermark:
                watermarked_playlists_ids.append(output_id)
                watermarked_tasks.append(
                    chain_tasks)
            else:
                no_watermarked_playlists_ids.append(output_id)
                no_watermarked_tasks.append(
                    chain_tasks)

    def _append_thumbnails_tasks(
            self,
            request_id: str,
            thumbnails: streaming_pb2.ThumbnailOutput,
            no_watermarked_tasks: list[chain],
            watermarked_tasks: list[chain],
            request_has_watermark,
            no_watermarked_thumbnails_ids: list[str],
            watermarked_thumbnails_ids: list[str]):
        """initial processing tasks by thumbnails options
        and chains them with upload task
        """
        for number, output in enumerate(thumbnails):
            if output.use_watermark and not request_has_watermark:
                raise exceptions.NoWatermarkToUseException

            if output.use_watermark:
                output_id: str = CacheKeysTemplates.WATERMARKED_THUMBNAIL_OUTPUT_ID.format(
                    number=number)
            else:
                output_id: str = CacheKeysTemplates.THUMBNAIL_OUTPUT_ID.format(
                    number=number)

            # chain of create generate_thumbnail and upload_file and callback
            chain_tasks = chain(
                # video_path will come from third level
                tasks.generate_thumbnail.s(
                    s3_output_key=output.upload_to.key,
                    request_id=request_id,
                    output_id=output_id,
                    thumbnail_time=output.thumbnail_time,
                    scale_width=output.options.scale_width,
                    scale_height=output.options.scale_height
                ),
                # file_path will come from generate_thumbnail task
                tasks.upload_file.s(
                    s3_output_key=output.upload_to.key,
                    s3_output_bucket=output.upload_to.bucket,
                    request_id=request_id,
                    output_id=output_id
                ),
                tasks.call_webhook.s(request_id=request_id)
            )

            if output.use_watermark:
                watermarked_thumbnails_ids.append(output_id)
                watermarked_tasks.append(
                    chain_tasks)
            else:
                no_watermarked_thumbnails_ids.append(output_id)
                no_watermarked_tasks.append(
                    chain_tasks)

    def _apply_job(
            self,
            request_id,
            ordered_levels):

        chain_items = []
        for level_tasks in ordered_levels:
            if level_tasks:
                chain_items.append(group(*level_tasks))

        job = chain(*chain_items)

        # set first primary status as QUEUING_CHECKS
        self.cache.set(
            CacheKeysTemplates.PRIMARY_STATUS.format(
                request_id=request_id),
            PrimaryStatus.QUEUING_CHECKS
        )

        # apply tasks
        result = job.apply_async()

        # is GroupResult or AsyncResult
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

    @staticmethod
    def _validate_output_bucket(output):
        """when output bucket is filled and has create flag,
        then check the output bucket is compatible with s3
        """
        if output.upload_to.bucket \
            and output.upload_to.create_bucket \
            and not S3Service.validate_bucket_name(
                output.upload_to.bucket):
            raise exceptions.BucketNameIsNotValidException

    @staticmethod
    def _validate_output_key(output):
        """check the output key is filled"""
        if output.upload_to.key.isspace():
            raise exceptions.S3KeyCanNotBeEmptyException

    @staticmethod
    def _has_any_output(*outputs):
        # print(outputs)
        return any(output.upload_to.key and not output.upload_to.key.isspace() for output in outputs)

    def _create_job(self, request, context):
        request_id: str = str(uuid.uuid4())
        print("request_id =", request_id)

        first_level: list = []
        second_level: list = []
        third_level: list = []
        fourth_level: list = []
        fifth_level: list = []
        no_watermarked_tasks: list = []
        watermarked_tasks: list = []
        watermarked_outputs_ids: list[str] = []
        watermarked_playlists_ids: list[str] = []
        watermarked_thumbnails_ids: list[str] = []
        no_watermarked_playlists_ids: list[str] = []
        no_watermarked_thumbnails_ids: list[str] = []
        request_has_watermark: bool = request.watermark.upload_to.key \
            and not request.watermark.s3_input.key.isspace()
        upload_watermarked_video: bool = request.watermark.upload_to.key \
            and not request.watermark.upload_to.key.isspace()
        watermarked_video_output_id: str = CacheKeysTemplates. \
            WATERMARKED_VIDEO_OUTPUT_ID.format(number=0)
        request_outputs: list = [
            *request.playlists,
            *request.thumbnails,
            request.watermark]
        request_inputs: tuple = (
            (0, request.video, InputType.VIDEO_INPUT),
            (1, request.watermark, InputType.WATERMARK_INPUT),
        )

        # For strings in proto3, the default value is the empty string
        # check input key to not be empty string
        if request.video.s3_input.key.isspace():
            raise exceptions.S3KeyCanNotBeEmptyException

        for input_number, input_object, input_type in request_inputs:
            if input_object.s3_input.key.isspace():
                # watermark is optional input, skip it if empty
                continue
            # is video exist on the cloud
            first_level.append(
                tasks.check_input_key.s(
                    s3_input_key=input_object.s3_input.key,
                    s3_input_bucket=input_object.s3_input.bucket,
                    request_id=request_id,
                    input_type=input_type)
            )
            third_level.append(
                chain(
                    # video or watermark object details will
                    # come from previous levels
                    tasks.download_input.s(
                        request_id=request_id,
                        s3_input_key=input_object.s3_input.key,
                        s3_input_bucket=input_object.s3_input.bucket,
                        input_number=input_number,
                        input_type=input_type)
                    ,
                    tasks.analyze_input.s(
                        request_id=request_id,
                        input_number=input_number,
                        input_type=input_type)
                )
            )

        fourth_level.append(
            tasks.inputs_funnel.s(request_id=request_id)
        )

        # is there any defined output
        if not self._has_any_output(*request_outputs):
            raise exceptions.OneOutputIsRequiredException(
                context=context)

        # check output keys are not empty and bucket names
        # are compatible with s3
        output_buckets: list[str] = []
        output_locations: list[tuple] = []
        for output in request_outputs:
            if output.upload_to.key.isspace():
                continue

            self._validate_output_bucket(output)

            # to use for getting unique output buckets names
            output_buckets.append(output.upload_to.bucket)

            # to use for checking duplicate output locations
            output_locations.append(
                (output.upload_to.bucket, output.upload_to.key))

            if output.upload_to.dont_replace:
                # check to can be replace output
                # when output key is already exist
                second_level.append(
                    tasks.check_output_key.s(
                        s3_output_key=output.upload_to.key,
                        s3_output_bucket=output.upload_to.bucket,
                        s3_dont_replace=output.upload_to.dont_replace,
                        request_id=request_id
                    )
                )

        # check duplicate output locations in current request
        if len(output_locations) != len(set(output_locations)):
            raise exceptions.DuplicateOutputLocationsException

        # check unique output buckets are exist on the cloud or
        # create them if has one create flag ,as first level tasks
        for bucket in set(output_buckets):
            # search bucket has one create flag
            s3_create_bucket = self._has_create_flag(
                bucket, *request_outputs)

            first_level.append(
                tasks.check_output_bucket.s(
                    s3_output_bucket=bucket,
                    s3_create_bucket=s3_create_bucket,
                    request_id=request_id
                )
            )

        # initial processing tasks by output formats
        # and chains them with upload task
        self._append_playlists_tasks(
            request_id=request_id,
            playlists=request.playlists,
            no_watermarked_tasks=no_watermarked_tasks,
            watermarked_tasks=watermarked_tasks,
            request_has_watermark=request_has_watermark,
            no_watermarked_playlists_ids=no_watermarked_playlists_ids,
            watermarked_playlists_ids=watermarked_playlists_ids)
        watermarked_outputs_ids.extend(watermarked_playlists_ids)

        self._append_thumbnails_tasks(
            request_id=request_id,
            thumbnails=request.thumbnails,
            no_watermarked_tasks=no_watermarked_tasks,
            watermarked_tasks=watermarked_tasks,
            request_has_watermark=request_has_watermark,
            no_watermarked_thumbnails_ids=no_watermarked_thumbnails_ids,
            watermarked_thumbnails_ids=watermarked_thumbnails_ids)
        watermarked_outputs_ids.extend(watermarked_thumbnails_ids)

        # add all no watermarked outputs to fifth_level
        fifth_level.extend(no_watermarked_tasks)

        # update watermarked_tasks
        if request_has_watermark and upload_watermarked_video:
            watermarked_outputs_ids.append(watermarked_video_output_id)
            watermarked_tasks.append(
                chain(
                    # file_path will come from add_watermark task
                    tasks.upload_file.s(
                        s3_output_key=request.watermark.upload_to.key,
                        s3_output_bucket=request.watermark.upload_to.bucket,
                        request_id=request_id,
                        output_id=watermarked_video_output_id
                    ),
                    tasks.call_webhook.s(request_id=request_id)
                )
            )

        # append add_watermark task to fifth_level
        if request_has_watermark and watermarked_tasks:
            fifth_level.append(
                chain(
                    # video_path and watermark_path
                    # will come from previous level
                    tasks.add_watermark.s(
                        s3_output_key=request.watermark.upload_to.key,
                        request_id=request_id,
                        output_id=watermarked_video_output_id
                    ),
                    # add all watermarked outputs
                    group(*watermarked_tasks)
                )
            )

        job_details = dict(
            reference_id=request.reference_id,
            webhook_url=request.webhook_url,
            total_checks=len(first_level)+len(second_level),
            total_inputs=len(third_level),
            total_outputs=len(no_watermarked_tasks) + len(watermarked_tasks),
            total_playlists=len(request.playlists),
            total_thumbnails=len(request.thumbnails),
            watermarked_outputs_ids=watermarked_outputs_ids,
            watermarked_playlists_ids=watermarked_playlists_ids,
            watermarked_thumbnails_ids=watermarked_thumbnails_ids,
            no_watermarked_playlists_ids=no_watermarked_playlists_ids,
            no_watermarked_thumbnails_ids=no_watermarked_thumbnails_ids
        )
        print(job_details)

        # saving job details
        self.cache.set(
            CacheKeysTemplates.JOB_DETAILS.format(
                request_id=request_id
            ),
            json.dumps(job_details))

        ordered_levels = [
            first_level,  # checks
            second_level,  # checks

            third_level,  # downloads
            fourth_level,  # wait

            fifth_level  # outputs
        ]

        self._apply_job(request_id, ordered_levels)
        return self._job_response(request_id)
