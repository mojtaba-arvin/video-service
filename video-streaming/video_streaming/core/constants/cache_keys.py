

__all__ = [
    'CacheKeysTemplates',
]


class CacheKeysTemplates:
    _PREFIX = "req_"

    # to save job details
    JOB_DETAILS = _PREFIX + "job_{request_id}"

    # to save celery result id of the request
    REQUEST_RESULT_ID = _PREFIX + "result_{request_id}"

    # count keys

    # to save number of passed checks as int
    PASSED_CHECKS = _PREFIX + "passed_{request_id}"

    # to save number of downloaded inputs as int
    READY_INPUTS = _PREFIX + "ready_inputs_{request_id}"

    # to save number of finished outputs as int
    READY_OUTPUTS = _PREFIX + "ready_outputs_{request_id}"

    # save steps

    # to save primary step name
    PRIMARY_STEP = _PREFIX + "step_{request_id}"

    # to save input step name
    INPUT_STEP = _PREFIX + "i_step_{request_id}_{input_number}"

    # to save output step name
    OUTPUT_STEP = _PREFIX + "o_step_{request_id}_{output_number}"

    # progress

    # to save progress of downloading for every input
    INPUT_DOWNLOADING_PROGRESS = _PREFIX + "i_down_{request_id}_{input_number}"

    # to save progress of processing for every output
    OUTPUT_PROCESSING_PROGRESS = _PREFIX + "o_process_{request_id}_{output_number}"

    # to save progress of uploading for every output
    OUTPUT_UPLOADING_PROGRESS = _PREFIX + "o_up_{request_id}_{output_number}"

