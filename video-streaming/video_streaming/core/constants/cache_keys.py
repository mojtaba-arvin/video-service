

__all__ = [
    'CacheKeysTemplates',
]


class CacheKeysTemplates:
    _PREFIX = "req_"

    # to save job details
    JOB_DETAILS = _PREFIX + "job_{request_id}"

    # to save celery result id of the request
    REQUEST_RESULT_ID = _PREFIX + "result_{request_id}"

    # to save ffprobe data of input video as json
    INPUT_FFPROBE_DATA = _PREFIX + "i_ffprobe_{request_id}_{input_number}"

    # to save number of passed checks as int
    PASSED_CHECKS = _PREFIX + "passed_{request_id}"

    # to save number of downloaded inputs as int
    READY_INPUTS = _PREFIX + "ready_inputs_{request_id}"

    # to save number of processed outputs as int
    PROCESSED_OUTPUTS = _PREFIX + "processed_outputs_{request_id}"

    # to save number of ready outputs after upload directory as int
    READY_OUTPUTS = _PREFIX + "ready_outputs_{request_id}"

    # save status

    # to save primary status name
    PRIMARY_STATUS = _PREFIX + "status_{request_id}"

    # to save input status name
    INPUT_STATUS = _PREFIX + "i_status_{request_id}_{input_number}"

    # to save output status name
    OUTPUT_STATUS = _PREFIX + "o_status_{request_id}_{output_number}"

    # progress

    # to save progress of downloading for every input
    INPUT_DOWNLOADING_PROGRESS = _PREFIX + "i_down_{request_id}_{input_number}"

    # to save progress of processing or uploading for every output
    OUTPUT_PROGRESS = _PREFIX + "o_progress_{request_id}_{output_number}"


