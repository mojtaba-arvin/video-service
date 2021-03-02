

class ErrorMessages:
    INPUT_VIDEO_404_OR_403 = "Input video is not found on S3 or permission denieded. make sure bucket name and file name is exist."
    OUTPUT_BUCKET_404_OR_403 = "Output bucket is not found or permission denieded."
    OUTPUT_KEY_IS_ALREADY_EXIST = "Output key on S3 is already exist."
    REPRESENTATION_NEEDS_BOTH_SIZE_AND_BITRATE = "Representation needs both size and bitrate"

    # task params errors
    S3_INPUT_KEY_IS_REQUIRED = "s3_input_key is required."
    S3_INPUT_BUCKET_IS_REQUIRED = "s3_input_bucket is required."
    S3_OUTPUT_BUCKET_IS_REQUIRED = "s3_output_bucket is required."
    S3_OUTPUT_KEY_IS_REQUIRED = "s3_output_key is required."
    OBJECT_DETAILS_IS_REQUIRED = "object_details is required."
    REQUEST_ID_IS_REQUIRED = "request_id is required."
    INPUT_PATH_IS_REQUIRED = "input_path is required."
    OUTPUT_PATH_IS_REQUIRED = "output_path is required."
    DIRECTORY_IS_REQUIRED = "directory is required."

    OUTPUT_PATH_OR_S3_OUTPUT_KEY_IS_REQUIRED = "output_path or s3_output_key is required."
    INPUT_SIZE_CAN_NOT_BE_ZERO = "input file size can not be zero"
    INPUT_FILE_IS_NOT_FOUND = "input file is not found"
