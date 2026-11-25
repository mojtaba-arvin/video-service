import json
from typing import Optional

from celery import Task
from video_streaming.security.webhook_signing import (
    build_webhook_signature_headers,
    stable_delivery_id,
)
from video_streaming import settings
from video_streaming.core.constants import CacheKeysTemplates
from video_streaming.core.tasks import BaseTask
from video_streaming.ffmpeg.tasks.base import BaseStreamingTask
from video_streaming.ffmpeg.webhook_transport import (
    WebhookDestinationError,
    WebhookTransportError,
    request_webhook,
)


class CallWebhookMixin(object):

    cache: BaseStreamingTask.cache
    logger: BaseStreamingTask.logger
    error_messages: BaseStreamingTask.error_messages
    raise_ignore: BaseTask.raise_ignore
    retry: Task.retry
    request: Task.request

    def call_webhook(self,
                     request_id: str,
                     webhook_url: Optional[str] = None,
                     signing_secret: Optional[str] = None,
                     delivery_stage_key: Optional[str] = None) -> None or True:
        """
        returns None when job details has been expired and
        returns True when delivered by 2xx HTTP status
        """

        job_details: dict = self.cache.get(
            CacheKeysTemplates.JOB_DETAILS.format(
                request_id=request_id))
        if job_details is None:
            return None

        encoded_body = json.dumps({
            "request_id": request_id,
            "reference_id": job_details["reference_id"]
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")
        delivery_id = stable_delivery_id(
            request_id, delivery_stage_key or "legacy-callback"
        )
        headers = {"Idempotency-Key": delivery_id}
        if signing_secret is not None:
            if not delivery_stage_key:
                raise self.raise_ignore(
                    message=self.error_messages.WEBHOOK_URL_IS_REQUIRED,
                    request_kwargs=self.request.kwargs,
                )
            headers.update(build_webhook_signature_headers(
                signing_secret, delivery_id, encoded_body
            ))
        try:
            http_response = request_webhook(
                webhook_url,
                body=encoded_body,
                connect_timeout=settings.WEBHOOK_CONNECT_TIMEOUT_SECONDS,
                read_timeout=settings.WEBHOOK_READ_TIMEOUT_SECONDS,
                allow_loopback=settings.WEBHOOK_ALLOW_LOOPBACK_CALLBACKS,
                headers=headers,
            )
        except WebhookDestinationError:
            # Never include the supplied URL in task state or exception text.
            raise self.raise_ignore(
                message=self.error_messages.WEBHOOK_URL_NOT_ALLOWED,
                request_kwargs=self.request.kwargs,
            )
        except WebhookTransportError as exc:
            raise self.retry(exc=exc)

        try:
            # Webhook URLs can contain bearer material or signed query values.
            self.logger.info(
                "Webhook response received",
                extra={
                    "http_status": http_response.status,
                    "request_id": request_id,
                },
            )

            # check the HTTP status code to raise ignore/retry
            return self.check_http_status(http_response, request_id)
        finally:
            http_response.close()

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
