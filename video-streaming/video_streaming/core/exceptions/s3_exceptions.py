from botocore.exceptions import ClientError
from .service_base import ServiceBaseException


__all__ = [
    'S3BaseException',
    'BucketExist'
]


class S3BaseException(ServiceBaseException):
    code: str = None              # service custom error code, e.g. 'NoSuchBucket', 'BucketAlreadyOwnedByYou'
    http_status: int = None       # standard HTTP status code, e.g. 404
    message: str = None           # 'Not Found'
    operation_name: str = None    # 'HeadObject'
    retry_attempts: int = 0
    response_object: dict = None

    def __init__(self, exception: ClientError = None):

        self.code = exception.response['Error']['Code']
        self.http_status = exception.response['ResponseMetadata']['HTTPStatusCode']
        self.message = exception.response['Error']['Message']
        self.operation_name = exception.operation_name
        self.retry_attempts = exception.response['ResponseMetadata']['RetryAttempts']
        self.response_object = exception.response

        """
        sample response object
        {'Error': {'Code': '404', 'Message': 'Not Found'}, 'ResponseMetadata': {'RequestId': '1667D46AF1D56DF6', 'HostId': '', 'HTTPStatusCode': 404, 'HTTPHeaders': {'accept-ranges': 'bytes', 'content-length': '0', 'content-security-policy': 'block-all-mixed-content', 'server': 'MinIO/RELEASE.2020-03-25T07-03-04Z', 'vary': 'Origin', 'x-amz-request-id': '1667D46AF1D56DF6', 'x-xss-protection': '1; mode=block', 'date': 'Sun, 28 Feb 2021 06:09:07 GMT'}, 'RetryAttempts': 0}}
        """
        super().__init__(self.message)

    @property
    def is_404_error(self):
        return self.http_status == 404

    @property
    def is_403_error(self):
        return self.http_status == 403


class BucketExist(S3BaseException):
    pass
