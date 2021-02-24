from ffmpeg_streaming import S3
from video_streaming import settings


class S3Service:
    BASE_URL = settings.S3_ENDPOINT_URL
    ACCESS_KEY = settings.S3_ACCESS_KEY_ID
    SECRET_KEY = settings.S3_SECRET_ACCESS_KEY
    REGION_NAME = settings.S3_REGION_NAME
    IS_SECURE = settings.S3_IS_SECURE
    DEFAULT_BUCKET_NAME = settings.S3_DEFAULT_BUCKET_NAME

    def __init__(
            self,
            endpoint_url=None,
            aws_access_key_id=None,
            aws_secret_access_key=None,
            use_ssl=None,
            region_name=None,
            default_bucket_name=None,
            **options
            ):
        if endpoint_url is not None:
            self.BASE_URL = str(endpoint_url)
        if aws_access_key_id is not None:
            self.ACCESS_KEY = str(aws_access_key_id)
        if aws_secret_access_key is not None:
            self.SECRET_KEY = str(aws_secret_access_key)
        if use_ssl is not None:
            self.IS_SECURE = bool(use_ssl)
        if region_name is not None:
            self.REGION_NAME = str(region_name)
        if default_bucket_name is not None:
            self.DEFAULT_BUCKET_NAME = str(default_bucket_name)

        self.options = options.copy()

    @property
    def s3_cloud(self):
        # create S3 instance of ffmpeg_streaming package to pass constants
        return S3(
            endpoint_url=self.BASE_URL,
            aws_access_key_id=self.ACCESS_KEY,
            aws_secret_access_key=self.SECRET_KEY,
            region_name=self.REGION_NAME,
            use_ssl=self.IS_SECURE,
            **self.options
        )

    def upload_directory(self, directory, **options):
        # bucket name is required, so pass default bucket name as constant
        self.s3_cloud.upload_directory(
            directory,
            bucket_name=options.get(
                'bucket_name',
                self.DEFAULT_BUCKET_NAME),
            **options)
