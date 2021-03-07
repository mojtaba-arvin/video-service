

__all__ = [
    'PrimaryStatus',
    'InputStatus',
    'OutputStatus'
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
    # when all outputs uploaded successfully
    ALL_OUTPUTS_ARE_READY = "ALL_OUTPUTS_ARE_READY"


class InputStatus:
    # for every input

    PREPARATION_DOWNLOADS = "PREPARATION_DOWNLOAD"
    DOWNLOADING = "DOWNLOADING"
    DOWNLOADING_FINISHED = "DOWNLOADING_FINISHED"


class OutputStatus:
    # for every output

    PREPARATION_PROCESSING = "PREPARATION_PROCESSING"
    PROCESSING = "PROCESSING"
    PROCESSING_FINISHED = "PROCESSING_FINISHED"

    # It will be set outside of tasks,
    # when PROCESSING_FINISHED and not PLAYLIST_UPLOADING
    QUEUING_UPLOADING = "QUEUING_UPLOADING_PLAYLIST"
    PLAYLIST_UPLOADING = "PLAYLIST_UPLOADING"
    UPLOADING_FINISHED = "UPLOADING_FINISHED"

