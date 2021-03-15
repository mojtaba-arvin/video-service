

__all__ = [
    'CacheKeysTemplates',
]


class CacheKeysTemplates:
    _PREFIX = "req_"

    # dict
    # to save job details
    JOB_DETAILS = _PREFIX + "job_{request_id}"

    # string
    # to save celery result id of the request
    REQUEST_RESULT_ID = _PREFIX + "result_{request_id}"

    # boolean
    # force stop all tasks of request, and delete all inputs and outputs
    FORCE_STOP_REQUEST = _PREFIX + "stop_{request_id}"

    # boolean
    # force stop one output pip and inc the ready outputs to delete inputs and outputs
    FORCE_STOP_OUTPUT_REQUEST = _PREFIX + "o_stop_{request_id}_{output_number}"

    # dict
    # to save ffprobe data of input video
    INPUT_FFPROBE_DATA = _PREFIX + "i_ffprobe_{request_id}_{input_number}"

    # integer
    # to save number of passed checks
    PASSED_CHECKS = _PREFIX + "passed_{request_id}"

    # integer
    # to save number of downloaded inputs
    READY_INPUTS = _PREFIX + "ready_inputs_{request_id}"

    # integer
    # to save number of processed outputs
    PROCESSED_OUTPUTS = _PREFIX + "processed_outputs_{request_id}"

    # integer
    # to save number of ready outputs
    READY_OUTPUTS = _PREFIX + "ready_outputs_{request_id}"

    # integer
    # to save number of revoked outputs
    REVOKED_OUTPUTS = _PREFIX + "revoked_outputs_{request_id}"

    # integer
    # to save number of failed outputs
    FAILED_OUTPUTS = _PREFIX + "failed_outputs_{request_id}"

    # save status

    # string
    # to save primary status name
    PRIMARY_STATUS = _PREFIX + "status_{request_id}"

    # string
    # to save stop reason
    STOP_REASON = _PREFIX + "stop_reason_{request_id}"

    # string
    # to save input status name
    INPUT_STATUS = _PREFIX + "i_status_{request_id}_{input_number}"

    # string
    # to save output status name
    OUTPUT_STATUS = _PREFIX + "o_status_{request_id}_{output_number}"

    # progress

    # dict
    # to save progress of downloading for every input
    INPUT_DOWNLOADING_PROGRESS = _PREFIX + "i_down_{request_id}_{input_number}"

    # dict
    # to save progress of processing or uploading for every output
    OUTPUT_PROGRESS = _PREFIX + "o_progress_{request_id}_{output_number}"

    # details

    # integer
    # to save size of playlist directory
    OUTPUT_SIZE = _PREFIX + "o_size_{request_id}_{output_number}"

    # usage

    # integer
    # save video proceeding time
    OUTPUT_START_PROCESSING_TIME = _PREFIX + "o_start_processing_time_{request_id}_{output_number}"
    OUTPUT_END_PROCESSING_TIME = _PREFIX + "o_end_processing_time_{request_id}_{output_number}"

    # list
    # save cpu usage of output psutil cpu_times
    OUTPUT_START_CPU_TIMES = _PREFIX + "o_start_cpu_times_{request_id}_{output_number}"
    OUTPUT_END_CPU_TIMES = _PREFIX + "o_end_cpu_times_{request_id}_{output_number}"

    # integer
    # save memory usage of output using psutil memory rss
    OUTPUT_START_MEMORY_RSS = _PREFIX + "o_start_memory_rss_{request_id}_{output_number}"
    OUTPUT_END_MEMORY_RSS = _PREFIX + "o_end_memory_rss_{request_id}_{output_number}"
