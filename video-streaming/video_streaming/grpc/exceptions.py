from video_streaming.core.constants import ErrorMessages, ErrorCodes
from video_streaming.core.exceptions import GrpcBaseException


__all__ = [
    'S3KeyCanNotBeEmptyException',
    'BucketNameIsNotValidException',
    'DuplicateOutputLocationsException',
    'OneOutputIsRequiredException'
]


class S3KeyCanNotBeEmptyException(GrpcBaseException):
    status_code = ErrorCodes.S3_KEY_CAN_NOT_BE_EMPTY
    message = ErrorMessages.S3_KEY_CAN_NOT_BE_EMPTY


class BucketNameIsNotValidException(GrpcBaseException):
    status_code = ErrorCodes.S3_BUCKET_NAME_IS_NOT_VALID
    message = ErrorMessages.S3_BUCKET_NAME_IS_NOT_VALID


class DuplicateOutputLocationsException(GrpcBaseException):
    status_code = ErrorCodes.DUPLICATE_OUTPUT_LOCATIONS
    message = ErrorMessages.DUPLICATE_OUTPUT_LOCATIONS


class OneOutputIsRequiredException(GrpcBaseException):
    status_code = ErrorCodes.ONE_OUTPUT_IS_REQUIRED
    message = ErrorMessages.ONE_OUTPUT_IS_REQUIRED
