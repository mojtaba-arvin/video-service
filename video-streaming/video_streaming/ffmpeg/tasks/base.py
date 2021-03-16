import json
import os
import shutil
from abc import ABC
from celery import states
from celery.utils.log import get_task_logger
from video_streaming import settings
from video_streaming.cache import RedisCache
from video_streaming.core.constants.status import StopReason
from video_streaming.core.tasks import BaseTask
from video_streaming.core.constants.cache_keys import CacheKeysTemplates
from video_streaming.core.services import S3Service
from video_streaming.core.constants import ErrorMessages, \
    PrimaryStatus, InputStatus, OutputStatus
celery_logger = get_task_logger(__name__)


class BaseStreamingTask(BaseTask, ABC):
    """Base class for video streaming tasks
    default values of tasks parameters are set in this class as attr
    """

    s3_service = S3Service()  # create s3 client
    error_messages = ErrorMessages
    logger = celery_logger
    cache = RedisCache()
    primary_status = PrimaryStatus
    input_status = InputStatus
    output_status = OutputStatus
    stop_reason = StopReason

    TMP_DOWNLOADED_DIR: str = settings.TMP_DOWNLOADED_DIR or ""
    INPUTS_DIRECTORY_PREFIX: str = "inputs_"
    TMP_PROCESSED_DIR: str = settings.TMP_PROCESSED_DIR or ""
    OUTPUTS_DIRECTORY_PREFIX: str = "outputs_"

    # delete local inputs after all outputs have been processed
    delete_inputs: bool = True

    # delete local outputs after all outputs have been uploaded
    delete_outputs: bool = True

    # # attrs that can not be empty or whitespace string
    # _NO_SPACE_STRINGS = [
    #     'request_id',
    #     's3_input_key',
    #     's3_output_key',
    #     's3_input_bucket',
    #     's3_output_bucket',
    #     'encode_format',
    #     'video_codec',
    #     'audio_codec',
    #     'webhook_url',
    #     'input_path',
    #     'output_path',
    #     'directory'
    # ]

    # def _initial_params(self):
    #     self.logger.info(
    #         'Executing task id {0.id},'
    #         ' args: {0.args!r} kwargs: {0.kwargs!r}'.
    #         format(self.request)
    #     )
    #
    #     for name, value in self.request.kwargs.items():
    #
    #         # mapping attr value as default value when it's not exist
    #         # in kwargs
    #         value = self.request.kwargs.get(
    #             name, getattr(self, name))
    #
    #         # mapping attr value as default value when string
    #         # parameters is "" instead of None
    #         if name in self._NO_SPACE_STRINGS:
    #
    #             # ensure value is not a whitespace string, and fill it
    #             # by attr value as default
    #             if isinstance(value, str) and value.isspace():
    #                 value = getattr(self, name)
    #
    #         setattr(self, name, value)
    #
    #     # callback
    #     if self.is_forced_to_stop():
    #         raise self.raise_revoke()

    def save_primary_status(self, status_name, request_id):
        """
            1. check request_id and JOB_DETAILS has been set
            2. check current status is not 'FAILED' or 'REVOKED'
            3. add to celery logger
            4. save primary status on cache
        """

        if request_id is None:
            # request_id has been not set
            return

        if not self.cache.get(CacheKeysTemplates.JOB_DETAILS.format(
                request_id=request_id)):
            # JOB_DETAILS has been not set
            return None

        # to prevent set any status after it was set to 'FAILED' or 'REVOKED'
        if self.can_set_status(request_id):

            # add to celery logger
            log_message = f"primary status: {status_name}"
            if request_id:
                log_message += f" ,request id: {request_id}"
            self.logger.info(log_message)

            self.cache.set(
                CacheKeysTemplates.PRIMARY_STATUS.format(
                    request_id=request_id),
                status_name
            )

            # save as celery task status
            # self.update_state(
            #     task_id=self.request.id,
            #     state=status_name)

    def can_set_status(self, request_id) -> None or bool:
        """to check current primary status of job is
         in 'FAILED' and 'REVOKED'
        """
        if request_id is None:
            return None
        current_status = self.cache.get(
            CacheKeysTemplates.PRIMARY_STATUS.format(
                request_id=request_id), decode=False)
        return current_status not in [
            self.primary_status.FAILED,
            self.primary_status.REVOKED
        ]

    def incr(self, key_template: str, request_id: str) -> None or int:
        if request_id is None:
            return None
        key = getattr(CacheKeysTemplates, key_template).format(
            request_id=request_id)
        self.cache.incr(key)
        return self.cache.get(key)

    def inputs_remover(self,
                       directory: str = None,
                       request_id: str = None):
        if not directory:
            directory = self.get_inputs_root_directory(request_id)

        # check remove directory is safe and not remove other inputs
        if directory and directory != self.INPUTS_DIRECTORY_PREFIX:
            shutil.rmtree(
                directory,
                ignore_errors=False,
                onerror=self.on_error_delete_inputs)

    def on_error_delete_inputs(self, func, path, exc_info: tuple):
        self.logger.error(f"error_delete_inputs : {path}")
        self.logger.error(exc_info)

    def outputs_remover(self,
                        request_id: str = None,
                        directory: str = None):
        if not directory:
            directory = self.get_outputs_root_directory(request_id)

        # check remove directory is safe and not remove other outputs
        if directory and directory != self.OUTPUTS_DIRECTORY_PREFIX:
            shutil.rmtree(
                directory,
                ignore_errors=False,
                onerror=self.on_error_delete_outputs)

    def on_error_delete_outputs(self, func, path, exc_info: tuple):
        self.logger.error(f"error_delete_outputs : {path}")
        self.logger.error(exc_info)

    def save_input_downloading_progress(self,
                                        total,
                                        current,
                                        request_id,
                                        input_number):
        if request_id is not None and \
                input_number is not None:
            # save progress of input downloading
            # by input_number and request_id
            self.cache.set(
                CacheKeysTemplates.INPUT_DOWNLOADING_PROGRESS.format(
                    request_id=request_id,
                    input_number=input_number
                ),
                json.dumps(dict(
                    total=total,
                    current=current
                )))

    def save_output_progress(self,
                             total,
                             current,
                             request_id,
                             output_id):
        if request_id is not None and \
                output_id is not None:
            # save progress of processing or uploading
            # by output_id and request_id
            self.cache.set(
                CacheKeysTemplates.OUTPUT_PROGRESS.format(
                    request_id=request_id,
                    output_id=output_id
                ),
                json.dumps(dict(
                    total=total,
                    current=current
                )))

    def get_inputs_root_directory(self, request_id) -> None or str:
        if request_id is None:
            return None
        return os.path.join(
            self.TMP_DOWNLOADED_DIR,
            self.INPUTS_DIRECTORY_PREFIX + str(request_id))

    def get_outputs_root_directory(self, request_id) -> None or str:
        if request_id is None:
            return None
        return os.path.join(
            self.TMP_PROCESSED_DIR,
            self.OUTPUTS_DIRECTORY_PREFIX + str(request_id))

    def raise_revoke(self, request_id: str = None):
        if self.delete_inputs:
            self.inputs_remover(request_id=request_id)
        if self.delete_outputs:
            self.outputs_remover(request_id=request_id)
        self.save_primary_status(
            self.primary_status.REVOKED,
            request_id
        )
        self.save_job_stop_reason(
            self.stop_reason.FORCE_REVOKED,
            request_id
        )
        raise self.raise_ignore(
            message=self.error_messages.TASK_WAS_FORCIBLY_STOPPED,
            state=states.REVOKED,
            request_kwargs=self.request.kwargs)

    def is_forced_to_stop(self, request_id) -> None or bool:
        force_stop: None or bool = self.cache.get(
            CacheKeysTemplates.FORCE_STOP_REQUEST.format(
                request_id=request_id))
        return force_stop

    def save_job_stop_reason(self, reason, request_id):
        # save primary status on cache when request_id
        # and JOB_DETAILS has been set
        if request_id is None or not self.cache.get(
                CacheKeysTemplates.JOB_DETAILS.format(
                request_id=request_id)):
            return None

        # check already has stop reason, to prevent rewrite reason
        if not self.has_already_stop_reason(request_id):

            # add to celery logger
            self.logger.info(f"stop reason: {reason}")

            self.cache.set(
                CacheKeysTemplates.STOP_REASON.format(
                    request_id=request_id),
                reason
            )

    def has_already_stop_reason(self, request_id) -> None or bool:
        """to check already has stop reason"""
        if request_id is None:
            return None
        reason = self.cache.get(
            CacheKeysTemplates.STOP_REASON.format(
                request_id=request_id), decode=False)
        return reason is not None
