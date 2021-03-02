import os
from video_streaming import settings
from video_streaming.celery import celery_app
from video_streaming.core.celery import VideoStreamingTask
from video_streaming.core.services import S3Service
from video_streaming.ffmpeg.utils import FfmpegCallback


"""
If some connection errors occur, task retry by celery jitter.
these exceptions are defined in S3Service class as RETRY_FOR.

The base class of task is `VideoStreamingTask`, and default values for 
required parameters of tasks can be set as attrs in the `VideoStreamingTask`
"""
decorator_kwargs = dict(
    base=VideoStreamingTask,
    bind=True,
    autoretry_for=S3Service.RETRY_FOR,
    retry_backoff_max=settings.TASK_RETRY_BACKOFF_MAX,
    retry_jitter=True,
    track_started=True
)


@celery_app.task(name="check_input_key", **decorator_kwargs)
def check_input_key(self,
                    s3_input_key: str = None,
                    s3_input_bucket: str = None):
    """check s3_input_key is exist on s3_input_bucket

       required parameters:
         - s3_input_key
         - s3_input_bucket
    """

    self._initial_params()

    # check s3_input_key on s3_input_bucket by head request
    object_details = self.check_input_video()

    return object_details


@celery_app.task(name="check_output_bucket", **decorator_kwargs)
def check_output_bucket(self,
                        s3_output_bucket: str = None,
                        s3_create_bucket: bool = None):
    """check output bucket or create if s3_create_bucket is True

       required parameters:
         - s3_output_bucket
    """

    self._initial_params()

    # check output bucket is exist
    # or create if s3_create_bucket is True
    bucket_details, created = self.get_or_create_bucket()
    return bucket_details, created


@celery_app.task(name="check_output_key", **decorator_kwargs)
def check_output_key(self,
                     s3_output_key: str = None,
                     s3_output_bucket: str = None,
                     s3_dont_replace: bool = None):
    """check if s3_output_key is already exist

       required parameters:
         - s3_output_key
         - s3_output_bucket
    """

    self._initial_params()

    # check if s3_output_key is already exist
    # and raise if s3_dont_replace is True
    self.check_output_key()


@celery_app.task(name="download_input", **decorator_kwargs)
def download_input(self, object_details: dict = None,
                   request_id: str = None, s3_input_key: str = None,
                   s3_input_bucket: str = None) -> str:
    """download video to local input path

       required parameters:
         - object_details
         - request_id
         - s3_input_key
         - s3_input_bucket
    """

    self._initial_params()

    # set self.input_path
    self.set_input_path()

    downloaded = os.path.exists(self.input_path)
    if not downloaded:
        self.download_video()

    return self.input_path


@celery_app.task(name="create_hls", **decorator_kwargs)
def create_hls(self,
               input_path: str = None,
               output_path: str = None,
               s3_output_key: str = None,
               fragmented: bool = None,
               encode_format: str = None,
               video_codec: str = None,
               audio_codec: str = None,
               quality_names: list[str] = None,
               custom_qualities: list[dict] = None) -> str:
    """create an HTTP Live Streaming (HLS)

       required parameters:
         - input_path
         - output_path or s3_output_key
         - encode_format
    """

    self._initial_params()

    hls = self.initial_protocol(is_hls=True)

    # ensure set self.directory and self.output_path
    self.ensure_set_output_location()

    # self.output_path includes the file name
    hls.output(
        self.output_path,
        monitor=FfmpegCallback(
            task=self,
            task_id=self.request.id.__str__()
        ).progress,
        ffmpeg_bin=settings.FFMPEG_BIN_PATH)

    return self.directory


@celery_app.task(name="create_dash", **decorator_kwargs)
def create_dash(self,
                input_path: str = None,
                output_path: str = None,
                s3_output_key: str = None,
                encode_format: str = None,
                video_codec: str = None,
                audio_codec: str = None,
                quality_names: list[str] = None,
                custom_qualities: list[dict] = None) -> str:
    """create a Dynamic Adaptive Streaming over HTTP (DASH)

       required parameters:
         - input_path
         - output_path or s3_output_key
         - encode_format
    """

    self._initial_params()

    dash = self.initial_protocol(is_hls=False)

    # ensure set self.directory and self.output_path
    self.ensure_set_output_location()

    # self.output_path includes the file name
    dash.output(
        self.output_path,
        monitor=FfmpegCallback(
            task=self,
            task_id=self.request.id.__str__()
        ).progress,
        ffmpeg_bin=settings.FFMPEG_BIN_PATH)

    return self.directory


@celery_app.task(name="upload_directory", ignore_result=True,
                 **decorator_kwargs)
def upload_directory(self,
                     directory: str = None,
                     s3_output_key: str = None,
                     s3_output_bucket: str = None):
    """upload the directory of the output files to S3 object storage

       required parameters:
         - directory
         - s3_input_key
         - s3_input_bucket
    """

    self._initial_params()

    self.upload_directory()
