from video_streaming.extensions import celery_app


@celery_app.task(bind=True, name="make_hls")
def make_hls(self):
    # TODO
    return "ok"
