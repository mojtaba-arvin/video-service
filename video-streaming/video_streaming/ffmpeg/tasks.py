import os
from os import path
import ffmpeg_streaming
from ffmpeg_streaming import Representation, Size, Bitrate
from video_streaming.celery import celery_app
from video_streaming.core.services import S3Service
from video_streaming.ffmpeg.constants import Resolutions, \
    VideoEncodingFormats
from video_streaming.ffmpeg.utils import S3DownloadCallback, \
    FfmpegCallback
from video_streaming.settings import (
    TMP_DOWNLOADED_DIR,
    TMP_TRANSCODED_DIR,
    S3_DEFAULT_INPUT_BUCKET_NAME,
    S3_DEFAULT_OUTPUT_BUCKET_NAME)


@celery_app.task(bind=True, name="create_hls")
def create_hls(
        self,
        request_id: str,  # unique request id - several tasks can point one request_id
        s3_input_key: str,
        s3_output_key: str,
        s3_input_bucket: str = S3_DEFAULT_INPUT_BUCKET_NAME,
        s3_output_bucket: str = S3_DEFAULT_OUTPUT_BUCKET_NAME,
        fragmented: bool = False,
        encode_format: str = VideoEncodingFormats.H264,
        video_codec: str = None,
        audio_codec: str = None,
        quality_names: list = None,  # ["360p","480p","720p"] or [Resolutions.360P, Resolutions.480P, Resolutions.720P]
        custom_qualities: list = None,  # [dict(size=[256, 144], bitrate=[97280, 65536])]
        webhook_url: str = None
        ):
    """
    create a HTTP Live Streaming (HLS)
    """
    # get celery task id
    task_id = self.request.id.__str__()

    # destination path of input on local machine
    local_input_path = os.path.join(
        TMP_DOWNLOADED_DIR, request_id, s3_input_key)

    # output path on local machine
    local_output_path = os.path.join(
        TMP_TRANSCODED_DIR, request_id, task_id, s3_output_key)

    # create s3 client
    s3_service = S3Service()

    # Size of the body in bytes.
    object_size = s3_service.get_object_size(
        s3_input_key,
        s3_input_bucket)

    # Check if the file is being downloaded
    # or downloaded by another task in the request.
    downloaded = path.exists(local_input_path)
    if not downloaded:
        s3_service.download(
            s3_input_key,
            destination_path=local_input_path,
            bucket_name=s3_input_bucket,
            callback=S3DownloadCallback(object_size).progress
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
        # is like [dict(size=[256, 144], bitrate=[97280, 65536])]
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

    hls.output(local_output_path, monitor=FfmpegCallback().progress)

    # TODO
    # 1. decrease used count to remove input file when reach 0
    # 2. upload output to s3 + progress
    # 3. decrease tasks counts to call webhook when reach 0


@celery_app.task(bind=True, name="create_dash")
def create_dash(self):
    """
    create a Dynamic Adaptive Streaming over HTTP (DASH)
    """
    pass
