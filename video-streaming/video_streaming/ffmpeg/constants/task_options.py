"""If some connection errors occur, task retry by celery jitter.
these exceptions are defined in S3Service class as RETRY_FOR.
"""
from video_streaming import settings
from video_streaming.core.services import S3Service


TASK_DECORATOR_KWARGS = dict(
    bind=True,
    autoretry_for=S3Service.RETRY_FOR,
    retry_backoff_max=settings.TASK_RETRY_BACKOFF_MAX,
    retry_jitter=True,
    track_started=True
)


class InputType:
    VIDEO_INPUT = "video"
    WATERMARK_INPUT = "watermark"
