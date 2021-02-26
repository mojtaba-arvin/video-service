import os
from os import path
import ffmpeg_streaming
from ffmpeg_streaming import Formats
from video_streaming.celery import celery_app
from video_streaming.core.services import S3Service
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
        request_id,     # unique request id - several tasks can point one request_id
        s3_input_key,   # the object name
        s3_output_key=None,
        s3_input_bucket=S3_DEFAULT_INPUT_BUCKET_NAME,
        s3_output_bucket=S3_DEFAULT_OUTPUT_BUCKET_NAME,
        fragmented=False,
        webhook_url=None
        ):
    """
    create a HTTP Live Streaming (HLS)
    """
    task_id = self.request.id.__str__()

    # destination path of input on local machine
    local_input_path = os.path.join(
        TMP_DOWNLOADED_DIR, request_id, s3_input_key)

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
    hls = video.hls(Formats.h264)
    hls.auto_generate_representations()

    if fragmented:
        hls.fragmented_mp4()

    # output path on local machine
    local_output_path = os.path.join(
        TMP_TRANSCODED_DIR, request_id, task_id, s3_output_key)
    print(local_output_path)

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
