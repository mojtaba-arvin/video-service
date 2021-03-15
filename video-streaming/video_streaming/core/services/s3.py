import re
import os
import traceback
import boto3
from typing import Union
from boto3.s3 import transfer
from botocore import exceptions as botocore_exceptions
from botocore.config import Config
from video_streaming import settings
from video_streaming.core.exceptions import s3_exceptions


class S3Service:

    DEFAULT_SERVICE_NAME = "s3"
    BASE_URL = settings.S3_ENDPOINT_URL
    ACCESS_KEY = settings.S3_ACCESS_KEY_ID
    SECRET_KEY = settings.S3_SECRET_ACCESS_KEY
    REGION_NAME = settings.S3_REGION_NAME
    IS_SECURE = settings.S3_IS_SECURE
    DEFAULT_BUCKET = settings.S3_DEFAULT_BUCKET

    TRANSFER_MULTIPART_THRESHOLD = settings.S3_TRANSFER_MULTIPART_THRESHOLD
    TRANSFER_MAX_CONCURRENCY = settings.S3_TRANSFER_MAX_CONCURRENCY
    TRANSFER_MULTIPART_CHUNKSIZE = settings.S3_TRANSFER_MULTIPART_CHUNKSIZE
    TRANSFER_NUM_DOWNLOAD_ATTEMPTS = settings.S3_TRANSFER_NUM_DOWNLOAD_ATTEMPTS
    TRANSFER_MAX_IO_QUEUE = settings.S3_TRANSFER_MAX_IO_QUEUE
    TRANSFER_IO_CHUNKSIZE = settings.S3_TRANSFER_IO_CHUNKSIZE
    TRANSFER_USE_THREADS = settings.S3_TRANSFER_USE_THREADS

    # exceptions
    exceptions = s3_exceptions
    RETRY_FOR = (
        botocore_exceptions.ConnectionError,  # such as EndpointConnectionError, ConnectionClosedError, ...
        botocore_exceptions.HTTPClientError,  # such as ConnectionClosedError, ReadTimeoutError
        botocore_exceptions.IncompleteReadError,
    )
    DEVELOPER_ERRORS = (
        botocore_exceptions.ParamValidationError,
        botocore_exceptions.ValidationError,
        botocore_exceptions.MissingParametersError,
        botocore_exceptions.UnknownServiceError,
        botocore_exceptions.ApiVersionNotFoundError,
        # TODO add more
    )

    client: boto3.client

    def __init__(
            self,
            service_name: str = None,
            region_name: str = None,
            api_version: str = None,
            use_ssl: bool = True,
            verify: Union[bool, str] = None,
            endpoint_url: str = None,
            aws_access_key_id: str = None,
            aws_secret_access_key: str = None,
            aws_session_token: str = None,
            config: Config = None,
            transfer_config: transfer.TransferConfig = None,
            _default_bucket: str = None):

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
        # self.s3_exceptions = self.client.exceptions
        self.transfer_config = transfer_config or self.transfer_config_generator()

    def _exception_handler(self, exc: Exception):
        """
        returns None for 404 and 403 errors, raises other exceptions
        """

        # handle ClientError
        if isinstance(exc, botocore_exceptions.ClientError):
            exception = self.exceptions.S3BaseException(exc)
            print(exception.response_object)
            if exception.is_404_error or exception.is_403_error:
                return None
            raise exception

        if isinstance(exc, self.DEVELOPER_ERRORS):
            # TODO : notify the developer
            pass

        if isinstance(exc, botocore_exceptions.CapacityNotAvailableError):
            # TODO : notify
            pass

        # TODO capture errors
        print(traceback.format_exc())

        raise exc

    @staticmethod
    def validate_bucket_name(name: str) -> bool:
        regex = "(?!^(\d{1,3}\.){3}\d{1,3}$)(^[a-z0-9]([a-z0-9-]*(\.[a-z0-9])?)*$(?<!\-))"
        return bool(re.search(regex, name))

    def head(self,
             key: str,
             bucket_name: str = None,
             **kwargs):
        kwargs['Key'] = key
        kwargs['Bucket'] = bucket_name or self.DEFAULT_BUCKET
        try:
            return self.client.head_object(**kwargs)
        except Exception as e:
            # _exception_handler returns None when object is not found
            return self._exception_handler(e)

    def head_bucket(
            self,
            bucket_name: str = None,
            **kwargs):
        kwargs['Bucket'] = bucket_name or self.DEFAULT_BUCKET
        try:
            # see : https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/s3.html#S3.Client.head_bucket
            return self.client.head_bucket(**kwargs)
        except Exception as e:
            # _exception_handler returns None when bucket is not found
            return self._exception_handler(e)

    def create_bucket(
            self,
            bucket_name: str = None,
            **kwargs):
        kwargs['Bucket'] = bucket_name or self.DEFAULT_BUCKET
        try:
            return self.client.create_bucket(**kwargs)
        except Exception as e:
            # TODO handle possible errors
            return self._exception_handler(e)

    @staticmethod
    def get_object_size(object_details):
        try:
            return int(object_details['ContentLength'])
        except Exception as e:
            print(0)
            return None

    def download(
            self,
            key: str,
            destination_path: str = None,
            bucket_name: str = None,
            extra_args: dict = None,
            callback: callable = None,
            config: transfer.TransferConfig = None):
        """
        returns destination_path if success
        """
        bucket_name = bucket_name or self.DEFAULT_BUCKET

        # to ensure output directory is exist and create it if not exist
        directory = destination_path.rpartition('/')[0]
        os.makedirs(directory, exist_ok=True)

        try:
            with open(destination_path, 'wb') as file_like_object:
                self.client.download_fileobj(
                    Bucket=bucket_name,
                    Key=key,
                    Fileobj=file_like_object,
                    ExtraArgs=extra_args,
                    Callback=callback,
                    Config=config or self.transfer_config)
            return destination_path
        except Exception as e:
            # remove if file created
            try:
                os.remove(destination_path)
            except OSError:
                pass
            return self._exception_handler(e)

    def transfer_config_generator(
            self,
            multipart_threshold: int = None,
            max_concurrency: int = None,
            multipart_chunksize: int = None,
            num_download_attempts: int = None,
            max_io_queue: int = None,
            io_chunksize: int = None,
            use_threads: bool = None):
        try:
            return transfer.TransferConfig(
                multipart_threshold=multipart_threshold or self.TRANSFER_MULTIPART_THRESHOLD,
                max_concurrency=max_concurrency or self.TRANSFER_MAX_CONCURRENCY,
                multipart_chunksize=multipart_chunksize or self.TRANSFER_MULTIPART_CHUNKSIZE,
                num_download_attempts=num_download_attempts or self.TRANSFER_NUM_DOWNLOAD_ATTEMPTS,
                max_io_queue=max_io_queue or self.TRANSFER_MAX_IO_QUEUE,
                io_chunksize=io_chunksize or self.TRANSFER_IO_CHUNKSIZE,
                use_threads=use_threads or self.TRANSFER_USE_THREADS)
        except Exception as e:
            return self._exception_handler(e)

    def upload_file_by_path(
            self,
            key: str,
            file_path: str,  # Path on the local filesystem from which object data will be read.
            bucket_name: str = None,
            callback: callable = None,
            extra_args: dict = None,
            config: transfer.TransferConfig = None
            ):
        try:
            self.client.upload_file(
                Filename=file_path,
                Bucket=bucket_name or self.DEFAULT_BUCKET,
                Key=key,
                ExtraArgs=extra_args,
                Callback=callback,
                Config=config or self.transfer_config)
        except Exception as e:
            return self._exception_handler(e)




