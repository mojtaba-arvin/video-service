import os
from video_streaming import settings
from video_streaming.celery import celery_app
from video_streaming.core.celery import VideoStreamingTask, \
    DownloadInputTask, CreatePlaylistTask, UploadDirectoryTask
from video_streaming.core.services import S3Service
from video_streaming.ffmpeg.utils import FfmpegCallback


"""
If some connection errors occur, task retry by celery jitter.
these exceptions are defined in S3Service class as RETRY_FOR.

The base class of task is `VideoStreamingTask`, and default values for 
required parameters of tasks can be set as attrs in the `VideoStreamingTask`
"""
decorator_kwargs = dict(
    bind=True,
    autoretry_for=S3Service.RETRY_FOR,
    retry_backoff_max=settings.TASK_RETRY_BACKOFF_MAX,
    retry_jitter=True,
    track_started=True
)


@celery_app.task(name="check_input_key",
                 base=VideoStreamingTask,
                 **decorator_kwargs)
def check_input_key(self,
                    s3_input_key: str = None,
                    s3_input_bucket: str = None,
                    request_id: str = None) -> dict:
    """check s3_input_key is exist on s3_input_bucket

       required parameters:
         - s3_input_key
         - s3_input_bucket
    """

    self._initial_params()
    self.save_primary_step(self.primary_steps.CHECKING)

    # check s3_input_key on s3_input_bucket by head request
    object_details = self.check_input_video()

    self.incr_passed_checks()

    return dict(object_details=object_details)


@celery_app.task(name="check_output_bucket",
                 base=VideoStreamingTask,
                 **decorator_kwargs)
def check_output_bucket(self,
                        s3_output_bucket: str = None,
                        s3_create_bucket: bool = None,
                        request_id: str = None):
    """check output bucket or create if s3_create_bucket is True

       required parameters:
         - s3_output_bucket
    """

    self._initial_params()
    self.save_primary_step(self.primary_steps.CHECKING)

    # check output bucket is exist
    # or create if s3_create_bucket is True
    self.ensure_bucket_exist()

    self.incr_passed_checks()


@celery_app.task(name="check_output_key",
                 base=VideoStreamingTask,
                 **decorator_kwargs)
def check_output_key(self,
                     s3_output_key: str = None,
                     s3_output_bucket: str = None,
                     s3_dont_replace: bool = None,
                     request_id: str = None):
    """check if s3_output_key is already exist

       required parameters:
         - s3_output_key
         - s3_output_bucket
    """

    self._initial_params()
    self.save_primary_step(self.primary_steps.CHECKING)

    # check if s3_output_key is already exist
    # and raise if s3_dont_replace is True
    self.check_output_key()

    self.incr_passed_checks()


@celery_app.task(name="download_input",
                 base=DownloadInputTask,
                 **decorator_kwargs)
def download_input(self, object_details: dict = None,
                   request_id: str = None, s3_input_key: str = None,
                   s3_input_bucket: str = None, input_number: int = None
                   ) -> dict:
    """download video to local input path

       required parameters:
         - object_details
         - request_id
         - s3_input_key
         - s3_input_bucket
    """

    self._initial_params()

    # save primary step using request_id
    self.save_primary_step(self.primary_steps.INPUTS_DOWNLOADING)

    # save input step using input_number and request_id
    self.save_input_step(self.input_steps.PREPARATION_DOWNLOADS)

    # set self.input_path
    self.set_input_path()

    downloaded = os.path.exists(self.input_path)
    if not downloaded:
        self.download_video()

    # save input step using input_number and request_id
    self.save_input_step(self.input_steps.DOWNLOADING_FINISHED)

    self.incr_ready_inputs()

    return dict(input_path=self.input_path)


@celery_app.task(name="create_playlist",
                 base=CreatePlaylistTask,
                 **decorator_kwargs)
def create_playlist(self,
                    input_path: str = None,
                    output_path: str = None,
                    s3_output_key: str = None,
                    fragmented: bool = None,
                    encode_format: str = None,
                    video_codec: str = None,
                    audio_codec: str = None,
                    quality_names: list[str] = None,
                    custom_qualities: list[dict] = None,
                    async_run: bool = None,
                    request_id: str = None,
                    output_number: int = None,
                    is_hls: bool = None) -> dict:
    """create an playlist ( HLS or DASH )

       required parameters:
         - input_path
         - output_path or s3_output_key
         - encode_format
    """

    self._initial_params()

    # save primary step using request_id
    self.save_primary_step(self.primary_steps.OUTPUTS_PROGRESSING)

    # save output step using output_number and request_id
    self.save_output_step(self.output_steps.PREPARATION_PROCESSING)

    playlist = self.initial_protocol()

    # ensure set self.directory and self.output_path
    self.ensure_set_output_location()

    try:
        # self.output_path includes the file name
        playlist.output(
            self.output_path,
            monitor=FfmpegCallback(
                task=self,
                task_id=self.request.id.__str__()
            ).progress,
            ffmpeg_bin=settings.FFMPEG_BIN_PATH,
            async_run=self.async_run)
    except Exception as e:
        # TODO handle possible Runtime Errors
        raise e

    self.save_output_step(self.output_steps.PROCESSING_FINISHED)

    return dict(directory=self.directory)


@celery_app.task(name="upload_directory",
                 base=UploadDirectoryTask,
                 **decorator_kwargs)
def upload_directory(self,
                     directory: str = None,
                     s3_output_key: str = None,
                     s3_output_bucket: str = None,
                     request_id: str = None,
                     output_number: int = None):
    """upload the directory of the output files to S3 object storage

       required parameters:
         - directory
         - s3_input_key
         - s3_input_bucket
    """

    self._initial_params()

    # save output step using output_number and request_id
    self.save_output_step(self.output_steps.PLAYLIST_UPLOADING)

    self.upload_directory()

    self.save_output_step(self.output_steps.UPLOADING_FINISHED)

    self.incr_ready_outputs()
