from video_streaming.celery import celery_app


@celery_app.task(bind=True, name="create_hls")
def create_hls(self):
    """
    create a HTTP Live Streaming (HLS)
    """
    pass


@celery_app.task(bind=True, name="create_dash")
def create_dash(self):
    """
    create a Dynamic Adaptive Streaming over HTTP (DASH)
    """
    pass
