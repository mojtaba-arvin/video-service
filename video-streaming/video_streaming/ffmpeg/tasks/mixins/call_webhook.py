import json
import urllib3
from celery import Task
from video_streaming.core.constants import CacheKeysTemplates
from video_streaming.core.tasks import BaseTask
from video_streaming.ffmpeg.tasks.base import BaseStreamingTask


class CallWebhookMixin(object):

    cache: BaseStreamingTask.cache
    logger: BaseStreamingTask.logger
    error_messages: BaseStreamingTask.error_messages
    raise_ignore: BaseTask.raise_ignore
    retry: Task.retry
    request: Task.request

    def call_webhook(self,
                     request_id: str,
                     webhook_url: str = None) -> None or True:
        """
        returns None when job details has been expired and
        returns True when delivered by 2xx HTTP status
        """

        job_details: dict = self.cache.get(
            CacheKeysTemplates.JOB_DETAILS.format(
                request_id=request_id))
        if job_details is None:
            return None

        http_method = 'POST'
        headers = {'Content-Type': 'application/json'}
        encoded_body = json.dumps({
            "request_id": request_id,
            "reference_id": job_details["reference_id"]
        })
        http_pool = urllib3.PoolManager()
        try:
            http_response = http_pool.request(
                http_method,
                webhook_url,
                headers=headers,
                body=encoded_body)
        except Exception as e:
            # urllib3.exceptions.NewConnectionError
            raise self.retry(exc=e)

        self.logger.info(
            f'http status: {http_response.status}, url: {webhook_url}')

        # check the HTTP status code to raise ignore/retry
        is_delivered = self.check_http_status(
            http_response,
            request_id)
        return is_delivered

    def check_http_status(
            self,
            http_response,
            request_id) -> True:
        if 200 <= http_response.status < 300:
            # success
            return True
        if 300 <= http_response.status < 400:
            raise self.raise_ignore(
                message=self.error_messages.
                WEBHOOK_URL_MUST_NOT_BE_REDIRECTED,
                request_kwargs=self.request.kwargs)
        if 400 <= http_response.status < 500:
            raise self.raise_ignore(
                message=self.error_messages.WEBHOOK_HTTP_FAILED.format(
                    status=http_response.status,
                    reason=http_response.reason,
                    request_id=request_id
                ),
                request_kwargs=self.request.kwargs)
        if 500 <= http_response.status:
            raise self.retry(
                exc=Exception(
                    self.error_messages.WEBHOOK_HTTP_FAILED.format(
                        status=http_response.status,
                        reason=http_response.reason,
                        request_id=request_id
                    )))
        raise self.raise_ignore(
            message=self.error_messages.
            HTTP_STATUS_CODE_NOT_SUPPORT.format(
                        status=http_response.status,
                        reason=http_response.reason,
                        request_id=request_id
                    ),
            request_kwargs=self.request.kwargs)
