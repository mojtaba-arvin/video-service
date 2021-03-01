import os
from video_streaming import settings
from video_streaming.celery import celery_app
from video_streaming.core.celery import VideoStreamingTask
from video_streaming.core.services import S3Service
from video_streaming.ffmpeg.constants import VideoEncodingFormats
from video_streaming.ffmpeg.utils import FfmpegCallback


@celery_app.task(
    base=VideoStreamingTask,
    bind=True,
    name="create_hls",
    autoretry_for=S3Service.RETRY_FOR,
    retry_backoff_max=settings.TASK_RETRY_BACKOFF_MAX,
    retry_jitter=True,
    track_started=True
    )
def create_hls(
        self,
        request_id: str,
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
        quality_names: list[str] = None,      # ["360p","480p","720p"] or [Resolutions.360P, Resolutions.480P, Resolutions.720P]
        custom_qualities: list[dict] = None,  # [dict(size=[256, 144], bitrate=[97280, 65536])]
        webhook_url: str = None,
        ):
    """create an HTTP Live Streaming (HLS)

    this task, downloads the input video from S3 object storage,
    and create an HLS by FFmpeg. finally uploads the output files to S3.

    in every step such as downloading, processing and uploading,
    the state of the task will updates on progress callback.

    if some connections errors occur, task retry by celery jitter.
    these exceptions are defined in S3Service class as RETRY_FOR.

    Args:
      request_id: Unique request id as gRPC request id,
                  several tasks can be points to one request_id.
                  e.g. "3b06519e-1h5c-475b-p473-1c8ao63bbe58"
      s3_input_key: The S3 key of video for ffmpeg input.
                    e.g. "/foo/bar/example.mp4"
      s3_output_key: The S3 key to save m3u8.
                     e.g. "/foo/bar/example.m3u8"

    Kwargs:
      s3_input_bucket:
      s3_output_bucket:
      s3_create_bucket: Create the output bucket If not exist
      s3_dont_replace: Check if s3_output_key is already exist, ignore the task
      fragmented: Set hls segment type to fmp4 if True
      encode_format: The encode format of HLS,
                     e.g "h264", "hevc" or "vp9"
      video_codec: The video codec format,
                   e.g "libx264", "libx265" or "libvpx-vp9"
      audio_codec: The audio codec format, e.g "aac"
      quality_names: List of quality names to genrate.
                     e.g. ["360p","480p","720p"] or [Resolutions.360P, Resolutions.480P, Resolutions.720P]
      custom_qualities: e.g. [dict(size=[256, 144], bitrate=[97280, 65536])]
      webhook_url:

    Returns:
      None
    """

    self.logger.info(
        'Executing task id {0.id}, args: {0.args!r} kwargs: {0.kwargs!r}'.
        format(self.request)
    )

    # initial
    self.request_id = request_id
    self.s3_input_key = s3_input_key
    self.s3_output_key = s3_output_key
    self.s3_input_bucket = s3_input_bucket
    self.s3_output_bucket = s3_output_bucket
    self.s3_create_bucket = s3_create_bucket
    self.s3_dont_replace = s3_dont_replace
    self.fragmented = fragmented
    self.encode_format = encode_format
    self.video_codec = video_codec
    self.audio_codec = audio_codec
    self.quality_names = quality_names
    self.custom_qualities = custom_qualities
    self.webhook_url = webhook_url

    # check s3_input_key on s3_input_bucket
    object_details = self.check_input_video()

    # check output bucket is exist
    # or create if s3_create_bucket is True
    self.check_output_bucket()

    # check if s3_output_key is already exist
    # and raise if s3_dont_replace is True
    self.check_output_key()

    # destination path of input on local machine
    local_input_path = os.path.join(
        settings.TMP_DOWNLOADED_DIR,
        self.request_id,
        self.s3_input_key)

    # sometimes s3_output_key includes s3 folders, just filename is enough
    output_filename = s3_output_key.rpartition('/')[2]

    output_directory = os.path.join(
        settings.TMP_TRANSCODED_DIR,
        self.request_id,
        self.s3_output_key)

    # when called directly, the task id is None
    if self.request.called_directly:
        output_directory = os.path.join(
            settings.TMP_TRANSCODED_DIR,
            self.request_id,
            output_filename.rpartition('/')[0])
    self.logger("output_directory:", output_directory)

    # output path on local machine
    local_output_path = os.path.join(output_directory, output_filename)

    # Check if the file is being downloaded
    # or downloaded by another task in the request.
    # note: local_input_path includes request_id, so object
    # with same name and bucket, can will download again
    downloaded = os.path.exists(local_input_path)
    if not downloaded:
        self.download_video(object_details, local_input_path)

    hls = self.initial_hls(local_input_path)

    # local_output_path includes the file name
    hls.output(
        local_output_path,
        monitor=FfmpegCallback(
            task=self,
            task_id=self.request.id.__str__()
        ).progress,
        ffmpeg_bin=settings.FFMPEG_BIN_PATH)

    self.upload_directory(output_directory)


@celery_app.task(base=VideoStreamingTask, bind=True, name="create_dash")
def create_dash(self):
    """create a Dynamic Adaptive Streaming over HTTP (DASH)"""
    pass
