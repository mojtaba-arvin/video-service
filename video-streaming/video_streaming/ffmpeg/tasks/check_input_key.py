from abc import ABC
from video_streaming import settings
from video_streaming.celery import celery_app
from video_streaming.ffmpeg.constants import TASK_DECORATOR_KWARGS, \
    InputType
from .base import BaseStreamingTask
from .mixins import CheckInputMixin


class CheckInputKeyTask(
        CheckInputMixin,
        BaseStreamingTask,
        ABC
        ):

    # rewrite BaseCheckMixin.save_failed
    def save_failed(self, request_id):
        super().save_failed(request_id)
        # stop reason will only be set if there is no reason before.
        # set common reason for the task, it's can be connection error
        # after many retries or etc.
        self.save_job_stop_reason(
            self.stop_reason.FAILED_INPUT_KEY_CHECKING,
            request_id
        )


@celery_app.task(name="check_input_key",
                 base=CheckInputKeyTask,
                 **TASK_DECORATOR_KWARGS)
def check_input_key(self,
                    *args,
                    s3_input_key: str = None,
                    s3_input_bucket: str = settings.S3_DEFAULT_INPUT_BUCKET_NAME,
                    request_id: str = None,
                    input_type: str = InputType.VIDEO_INPUT
                    ) -> dict:
    """check s3_input_key is exist on s3_input_bucket

    Args:
        self:
        *args:
        s3_input_key:
            The S3 key of input video.
            e.g. "/foo/bar/example.mp4"
        s3_input_bucket:
        request_id:
            job request id as unique id,
            several tasks can be points to one request_id.
            e.g. "3b06519e-1h5c-475b-p473-1c8ao63bbe58"
        input_type:
            to map object_details to the name that will use
            in the download_input task

    Returns:

    """

    self.check_input_key_requirements(
        request_id=request_id,
        s3_input_key=s3_input_key,
        s3_input_bucket=s3_input_bucket
    )

    if self.is_forced_to_stop(request_id):
        raise self.raise_revoke(request_id)

    self.save_primary_status(
        self.primary_status.CHECKING,
        request_id)

    # get object details by s3_input_key on s3_input_bucket
    object_details = self.get_object_details(s3_input_key, s3_input_bucket)

    # object_details is None for 404 or 403 reason
    if not object_details:
        self.save_primary_status(
            self.primary_status.FAILED,
            request_id)
        self.save_job_stop_reason(
            self.stop_reason.INPUT_VIDEO_ON_S3_IS_404_OR_403,
            request_id
        )
        raise self.raise_ignore(
            message=self.error_messages.INPUT_VIDEO_404_OR_403,
            request_kwargs=self.request.kwargs)

    self.incr_passed_checks(request_id)

    return {input_type+"_details": object_details}
