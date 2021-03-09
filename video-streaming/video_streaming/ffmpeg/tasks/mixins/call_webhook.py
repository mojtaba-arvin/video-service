import json
import urllib3
from celery import Task
from video_streaming.core.tasks import BaseTask
from video_streaming.ffmpeg.tasks.base import BaseStreamingTask


class CallWebhookMixin(object):
    request_id: str

    retry: Task.retry
    raise_ignore: BaseTask.raise_ignore
    logger: BaseStreamingTask.logger
    get_job_details_by_request_id: BaseStreamingTask.get_job_details_by_request_id
    error_messages: BaseStreamingTask.error_messages

    def call_webhook(self, url: str = None) -> None or True:
        if not url or self.request_id:
            return None
        job_details = self.get_job_details_by_request_id()
        if job_details is None:
            return None

        http_method = 'POST'
        headers = {'Content-Type': 'application/json'}
        encoded_body = json.dumps({
            "request_id": self.request_id,
            "reference_id": job_details["reference_id"]
        })
        http_pool = urllib3.PoolManager()
        try:
            http_response = http_pool.request(
                http_method,
                url,
                headers=headers,
                body=encoded_body)
        except Exception as e:
            # urllib3.exceptions.NewConnectionError
            raise self.retry(exc=e)

        self.logger.info(
            f'http status: {http_response.status}, url: {url}')

        # check the HTTP status code to raise ignore/retry
        is_delivered = self.check_http_status(http_response)
        return is_delivered

    def check_http_status(self, http_response) -> True or None:
        if 200 <= http_response.status < 300:
            # success
            return True
        if 300 <= http_response.status < 400:
            raise self.raise_ignore(
                message=self.error_messages.WEBHOOK_URL_MUST_NOT_BE_REDIRECTED
            )
        if 400 <= http_response.status < 500:
            raise self.raise_ignore(
                message=self.error_messages.WEBHOOK_HTTP_FAILED.format(
                    status=http_response.status,
                    reason=http_response.reason,
                    request_id=self.request_id
                )
            )
        if 500 <= http_response.status:
            raise self.retry(
                exc=Exception(
                    self.error_messages.WEBHOOK_HTTP_FAILED.format(
                        status=http_response.status,
                        reason=http_response.reason,
                        request_id=self.request_id
                    )))
