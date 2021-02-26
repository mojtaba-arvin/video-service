import os
import boto3
from video_streaming import settings
# TODO : from botocore.exceptions import ClientError


class S3Service:
    DEFAULT_SERVICE_NAME = "s3"
    BASE_URL = settings.S3_ENDPOINT_URL
    ACCESS_KEY = settings.S3_ACCESS_KEY_ID
    SECRET_KEY = settings.S3_SECRET_ACCESS_KEY
    REGION_NAME = settings.S3_REGION_NAME
    IS_SECURE = settings.S3_IS_SECURE
    DEFAULT_BUCKET = settings.S3_DEFAULT_BUCKET

    def __init__(
            self,
            service_name=None,
            region_name=None,
            api_version=None,
            use_ssl=True,
            verify=None,
            endpoint_url=None,
            aws_access_key_id=None,
            aws_secret_access_key=None,
            aws_session_token=None,
            config=None,
            _default_bucket=None):

        if _default_bucket is not None:
            self.DEFAULT_BUCKET = str(_default_bucket)

        # boto3 client parameters
        if service_name is None:
            service_name = self.DEFAULT_SERVICE_NAME
        if endpoint_url is None:
            endpoint_url = self.BASE_URL
        if aws_access_key_id is None:
            aws_access_key_id = self.ACCESS_KEY
        if aws_secret_access_key is None:
            aws_secret_access_key = self.SECRET_KEY
        if use_ssl is None:
            use_ssl = self.IS_SECURE
        if region_name is None:
            region_name = self.REGION_NAME

        self.client = boto3.client(
            service_name,
            region_name=region_name,
            api_version=api_version,
            use_ssl=use_ssl,
            verify=verify,
            endpoint_url=endpoint_url,
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key,
            aws_session_token=aws_session_token,
            config=config)

    def head(self,
             key,
             bucket_name=None,
             **kwargs):
        kwargs['Key'] = key
        kwargs['Bucket'] = bucket_name or self.DEFAULT_BUCKET
        return self.client.head_object(**kwargs)

    def get_object_size(
            self,
            key,
            bucket_name=None):
        """
        Size of the object body in bytes
        """
        bucket_name = bucket_name or self.DEFAULT_BUCKET
        return int(self.head(
            key,
            bucket_name).get('ContentLength', 0))

    def download(
            self,
            key,
            destination_path=None,
            bucket_name=None,
            extra_args=None,
            callback=None,
            config=None):
        bucket_name = bucket_name or self.DEFAULT_BUCKET

        directory, _, filename = destination_path.rpartition('/')
        os.makedirs(directory, exist_ok=True)

        with open(destination_path, 'wb') as file_like_object:
            self.client.download_fileobj(
                Bucket=bucket_name,
                Key=key,
                Fileobj=file_like_object,
                ExtraArgs=extra_args,
                Callback=callback,
                Config=config)
