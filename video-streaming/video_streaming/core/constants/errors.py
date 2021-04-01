

__all__ = [
    'ErrorMessages',
    'ErrorCodes'
]


class ErrorMessages:

    INPUT_VIDEO_404_OR_403 = "Input video is not found on S3 or permission denieded. make sure bucket name and file name is exist."
    OUTPUT_BUCKET_404_OR_403 = "Output bucket is not found or permission denieded."
    OUTPUT_KEY_IS_ALREADY_EXIST = "Output key on S3 is already exist."
    REPRESENTATION_NEEDS_BOTH_SIZE_AND_BITRATE = "Representation needs both size and bitrate"
    S3_INPUT_KEY_IS_REQUIRED = "s3_input_key is required."
    S3_INPUT_BUCKET_IS_REQUIRED = "s3_input_bucket is required."
    S3_OUTPUT_BUCKET_IS_REQUIRED = "s3_output_bucket is required."
    S3_OUTPUT_KEY_IS_REQUIRED = "s3_output_key is required."
    OBJECT_DETAILS_IS_REQUIRED = "object_details is required."
    OBJECT_DETAILS_IS_INVALID = "object_details must be a dict with 'ContentLength' key."
    REQUEST_ID_IS_REQUIRED = "request_id is required."
    OUTPUT_NUMBER_IS_REQUIRED = "request_id is required."
    INPUT_NUMBER_IS_REQUIRED = "input_number is required."
    WEBHOOK_URL_IS_REQUIRED = "webhook_url is required and can not be empty."
    INPUT_PATH_IS_REQUIRED = "input_path is required."
    OUTPUT_PATH_IS_REQUIRED = "output_path is required."
    DIRECTORY_IS_REQUIRED = "directory is required."
    OUTPUT_PATH_OR_S3_OUTPUT_KEY_IS_REQUIRED = "output_path or s3_output_key is required."
    INPUT_SIZE_CAN_NOT_BE_ZERO = "input file size can not be zero."
    INPUT_FILE_IS_NOT_FOUND = "input file is not found."
    WEBHOOK_URL_MUST_NOT_BE_REDIRECTED = "webhook url must not be redirected."
    WEBHOOK_HTTP_FAILED = "webhook task failed" \
                           ", HTTP response status: '{status}'" \
                           ", reason: '{reason}'" \
                           ", request_id: '{request_id}'"
    HTTP_STATUS_CODE_NOT_SUPPORT = "response HTTP status code is not support" \
                           ", HTTP response status: '{status}'" \
                           ", reason: '{reason}'" \
                           ", request_id: '{request_id}'"
    TASK_WAS_FORCIBLY_STOPPED = "task was forcibly stopped."
    CAN_NOT_UPLOAD_DIRECTORY = "can not upload directory"
    INPUT_VIDEO_CODEC_TYPE_IN_NOT_VIDEO = "input video codec type is not video"
    CAN_NOT_UPLOAD_EMPTY_FILE = "cab not upload empty file"
    CAN_NOT_UPLOAD_FILE = "can not upload the file"
    INPUT_TYPE_IS_REQUIRED = "input type is required."
    JOB_DETAILS_NOT_FOUND = "job details not found."
    WAITING_FOR_AGGREGATE_INPUTS = "waiting for aggregate inputs."

    # gRPC
    INTERNAL_ERROR = "internal server error"
    S3_KEY_CAN_NOT_BE_EMPTY = "s3 key can not be empty."
    S3_BUCKET_NAME_IS_NOT_VALID = "s3 bucket name is not valid."
    DUPLICATE_OUTPUT_LOCATIONS = "there are duplicate output locations."
    ONE_OUTPUT_IS_REQUIRED = "one output is required."
    JOB_NOT_FOUND_BY_TRACKING_ID = "job not found by tracking_id"
    JOB_IS_FAILED = "job is failed"
    JOB_IS_REVOKED = "job is revoked"
    JOB_IS_FINISHED = "job is finished"
    NO_WATERMARK_TO_USE = "no watermark to use"


class ErrorCodes:

    # gRPC base exception error codes
    INTERNAL_ERROR = 1000
    S3_KEY_CAN_NOT_BE_EMPTY = 1001
    S3_BUCKET_NAME_IS_NOT_VALID = 1002
    DUPLICATE_OUTPUT_LOCATIONS = 1003
    ONE_OUTPUT_IS_REQUIRED = 1004  # TODO
    JOB_NOT_FOUND_BY_TRACKING_ID = 1005  # revoke_job_outputs
    JOB_IS_FAILED = 1006
    JOB_IS_REVOKED = 1007
    JOB_IS_FINISHED = 1008
    NO_WATERMARK_TO_USE = 1009
