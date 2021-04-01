

__all__ = [
    'PrimaryStatus',
    'InputStatus',
    'OutputStatus',
    'StopReason'
]


class PrimaryStatus:

    QUEUING_CHECKS = "QUEUING_CHECKS"
    # one of checks tasks has been started
    CHECKING = "CHECKING"
    # when all first level tasks has been finished
    CHECKS_FINISHED = "CHECKS_FINISHED"

    # It will be set outside of tasks,
    # when CHECKS_FINISHED and not INPUTS_DOWNLOADING
    QUEUING_INPUTS_DOWNLOADING = "QUEUING_INPUTS_DOWNLOADING"
    # when one of checks tasks has been started
    INPUTS_DOWNLOADING = "INPUTS_DOWNLOADING"
    # when downloading all inputs has been finished
    ALL_INPUTS_DOWNLOADED = "ALL_INPUTS_DOWNLOADED"

    # It will be set outside of tasks,
    # when ALL_INPUTS_DOWNLOADED and not QUEUING_OUTPUTS
    QUEUING_OUTPUTS = "QUEUING_OUTPUTS"
    # when one of level3 chains has been started
    OUTPUTS_PROGRESSING = "OUTPUTS_PROGRESSING"
    # when all outputs are finished
    # means total_outputs == (ready_outputs + revoked_outputs + failed_outputs)
    FINISHED = "FINISHED"

    # when all outputs revoked
    REVOKED = "REVOKED"
    # when one of checks has been failed, one of inputs has been failed,
    # or all outputs has been failed
    FAILED = "FAILED"


class InputStatus:
    # for every input

    PREPARATION_DOWNLOAD = "PREPARATION_DOWNLOAD"
    DOWNLOADING = "DOWNLOADING"
    DOWNLOADING_FINISHED = "DOWNLOADING_FINISHED"

    QUEUING_TO_ANALYZE = "QUEUING_TO_ANALYZE"
    ANALYZING = "ANALYZING"
    ANALYZING_FINISHED = "ANALYZING_FINISHED"

    INPUT_REVOKED = "INPUT_REVOKED"
    INPUT_FAILED = "INPUT_FAILED"


class OutputStatus:
    # for every output

    OUTPUT_REVOKED = "OUTPUT_REVOKED"
    OUTPUT_FAILED = "OUTPUT_FAILED"

    PREPARATION_PROCESSING = "PREPARATION_PROCESSING"
    PROCESSING = "PROCESSING"
    PROCESSING_FINISHED = "PROCESSING_FINISHED"

    # It will be set outside of tasks,
    # when PROCESSING_FINISHED and not UPLOADING
    QUEUING_UPLOADING = "QUEUING_UPLOADING"
    UPLOADING = "UPLOADING"
    UPLOADING_FINISHED = "UPLOADING_FINISHED"


class StopReason:

    FORCE_REVOKED = "FORCE_REVOKED"
    INTERNAL_ERROR = "INTERNAL_ERROR"

    # CheckInputKeyTask and DownloadInputTask
    INPUT_VIDEO_ON_S3_IS_404_OR_403 = "INPUT_VIDEO_ON_S3_IS_404_OR_403"

    # CheckInputKeyTask
    FAILED_INPUT_KEY_CHECKING = "FAILED_INPUT_KEY_CHECKING"

    # CheckOutputBucketTask
    FAILED_OUTPUT_BUCKET_CHECKING = "FAILED_OUTPUT_BUCKET_CHECKING"
    OUTPUT_BUCKET_ON_S3_IS_404_OR_403 = "OUTPUT_BUCKET_ON_S3_IS_404_OR_403"

    # CheckOutputKeyTask
    FAILED_OUTPUT_KEY_CHECKING = "FAILED_OUTPUT_KEY_CHECKING"
    OUTPUT_KEY_IS_ALREADY_EXIST = "OUTPUT_KEY_IS_ALREADY_EXIST"

    # DownloadInputTask
    DOWNLOADING_FAILED = "DOWNLOADING_FAILED"

    # AnalyzeInputTask
    FAILED_ANALYZE_INPUT = "FAILED_ANALYZE_INPUT"

    # AnalyzeInputTask
    INPUT_VIDEO_CODEC_TYPE_IN_NOT_VIDEO = "INPUT_VIDEO_CODEC_TYPE_IN_NOT_VIDEO"

    # InputsFunnelTask
    AGGREGATE_INPUTS_FAILED = "AGGREGATE_INPUTS_FAILED"

    # CreatePlaylistTask
    FAILED_CREATE_PLAYLIST = "FAILED_CREATE_PLAYLIST"
    INPUT_VIDEO_SIZE_CAN_NOT_BE_ZERO = "INPUT_VIDEO_SIZE_CAN_NOT_BE_ZERO"
    REPRESENTATION_NEEDS_BOTH_SIZE_AND_BITRATE = "REPRESENTATION_NEEDS_BOTH_SIZE_AND_BITRATE"

    # UploadDirectoryTask
    FAILED_UPLOAD_DIRECTORY = "FAILED_UPLOAD_DIRECTORY"

    # GenerateThumbnailTask
    FAILED_GENERATE_THUMBNAIL = "FAILED_GENERATE_THUMBNAIL"

    # UploadFileTask
    FAILED_UPLOAD_FILE = "FAILED_UPLOAD_FILE"

    # AddWatermarkTask
    FAILED_ADD_WATERMARK = "FAILED_ADD_WATERMARK"

    # InputsFunnelTask
    # when job_details not found
    JOB_TIMEOUT = "JOB_TIMEOUT"
