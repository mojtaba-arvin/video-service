import os
from os import path
import ffmpeg_streaming
from video_streaming import settings
from ffmpeg_streaming import Representation, Size, Bitrate
from video_streaming.celery import celery_app
from video_streaming.core.constants import ErrorMessages
from video_streaming.core.services import S3Service
from video_streaming.ffmpeg.constants import Resolutions, \
    VideoEncodingFormats
from video_streaming.ffmpeg.utils import S3DownloadCallback, \
    FfmpegCallback, S3UploadDirectoryCallback


@celery_app.task(bind=True, name="create_hls")
def create_hls(
        self,
        request_id: str,  # unique request id - several tasks can point to one request_id
        s3_input_key: str,
        s3_output_key: str,
        s3_input_bucket: str = settings.S3_DEFAULT_INPUT_BUCKET_NAME,
        s3_output_bucket: str = settings.S3_DEFAULT_OUTPUT_BUCKET_NAME,
        s3_create_bucket: bool = True,  # create the output bucket If not exist
        s3_dont_replace: bool = True,   # check if s3_output_key is already exist, ignore the task
        fragmented: bool = False,
        encode_format: str = VideoEncodingFormats.H264,
        video_codec: str = None,
        audio_codec: str = None,
        quality_names: list[str] = None,     # ["360p","480p","720p"] or [Resolutions.360P, Resolutions.480P, Resolutions.720P]
        custom_qualities: list[dict] = None,  # [dict(size=[256, 144], bitrate=[97280, 65536])]
        webhook_url: str = None,
        ):
    """
    create a HTTP Live Streaming (HLS)
    """
    # get celery task id
    task_id = self.request.id.__str__()

    # create s3 client
    s3_service = S3Service()

    # check s3_input_key on s3_input_bucket
    object_details = s3_service.head(
            key=s3_input_key, bucket_name=s3_input_bucket)
    if not object_details:
        raise self.raise_ignore(
            message=ErrorMessages.INPUT_VIDEO_404_OR_403)

    # to determine if s3_output_bucket not exists and permission to access it
    if not s3_service.head_bucket(bucket_name=s3_output_bucket):
        if not s3_create_bucket:
            raise self.raise_ignore(
                message=ErrorMessages.OUTPUT_BUCKET_404_OR_403)
        # create s3_output_bucket
        s3_service.create_bucket(bucket_name=s3_output_bucket)
        # TODO handle possible errors on create_bucket

    # check if s3_output_key is already exist
    if s3_service.head(key=s3_output_key, bucket_name=s3_output_bucket):
        if s3_dont_replace:
            raise self.raise_ignore(
                message=ErrorMessages.OUTPUT_KEY_IS_ALREADY_EXIST)

    # destination path of input on local machine
    local_input_path = os.path.join(
        settings.TMP_DOWNLOADED_DIR, request_id, s3_input_key)

    output_directory = os.path.join(
        settings.TMP_TRANSCODED_DIR, request_id, task_id)

    # sometimes s3_output_key includes s3 folders, just filename is enough
    output_filename = s3_output_key.rpartition('/')[2]

    # output path on local machine
    local_output_path = os.path.join(output_directory, output_filename)

    # Check if the file is being downloaded
    # or downloaded by another task in the request.
    downloaded = path.exists(local_input_path)
    if not downloaded:
        # Size of the body in bytes.
        object_size = S3Service.get_object_size(object_details)
        # Initial callback
        download_callback = S3DownloadCallback(object_size).progress
        # Download the input video to the local_input_path
        s3_service.download(
            s3_input_key,
            destination_path=local_input_path,
            bucket_name=s3_input_bucket,
            callback=download_callback
        )

    video = ffmpeg_streaming.input(local_input_path)
    format_instance = VideoEncodingFormats().get_format_class(
        encode_format,
        video=video_codec,
        audio=audio_codec,
    )
    hls = video.hls(format_instance)

    if not (custom_qualities or quality_names):
        # generate default representations
        hls.auto_generate_representations()
    else:
        reps = []
        # quality_names is like ["360p","480p","720p"]
        if quality_names:
            reps.extend(
                Resolutions().get_reps(quality_names)
            )
        # custom_qualities is like [dict(size=[256, 144], bitrate=[97280, 65536])]
        for quality in custom_qualities:
            size = quality.get('size', None)
            bitrate = quality.get('bitrate', None)
            if not (size or bitrate):
                continue
            if not size or not bitrate:
                raise ValueError("Representation needs both size and bitrate")
            reps.append(
                Representation(Size(*size), Bitrate(*bitrate))
            )
        # generate representations
        hls.representations(*reps)

    if fragmented:
        hls.fragmented_mp4()

    # local_output_path includes the file name
    hls.output(local_output_path, monitor=FfmpegCallback().progress)

    s3_service.upload_directory(
        s3_output_key,
        output_directory,
        bucket_name=s3_output_bucket,
        directory_callback=S3UploadDirectoryCallback().progress
    )

    # TODO
    # 1. decrease used count to remove input file when reach 0
    # 3. decrease tasks counts to call webhook when reach 0


@celery_app.task(bind=True, name="create_dash")
def create_dash(self):
    """
    create a Dynamic Adaptive Streaming over HTTP (DASH)
    """
    pass
