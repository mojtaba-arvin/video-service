

__all__ = [
    'CheckingInputVideoState',
    'CreatingOutputBucketState',
    'PreparationVideoDownloadingState',
    'DownloadingVideoState',
    'PreparationVideoProcessingState'
]


class CustomTaskState:
    state = 'EXECUTING'
    description = None

    def __init__(self, description: str = None):
        self.description = description or self.description

    @property
    def message(self):
        return f"'{self.state}': {self.description}"

    def create(
            self,
            progress_total=None,
            progress_current=None,
            task_id=None
            ):
        kwargs = dict(
            state=self.state,
            # using same dict structure for states meta
            meta=dict(
                description=self.description,
                progress=dict(
                    total=progress_total,
                    current=progress_current
                )
            )
        )
        if task_id:
            kwargs['task_id'] = task_id
        return kwargs


class CheckingInputVideoState(CustomTaskState):
    state = 'CHECKING_INPUT_VIDEO'
    description = 'Getting details of the input video from the cloud'


class CreatingOutputBucketState(CustomTaskState):
    state = 'CREATING_OUTPUT_BUCKET'
    description = 'Creating the output bucket on the cloud'


class PreparationVideoDownloadingState(CustomTaskState):
    state = 'PREPARATION_VIDEO_DOWNLOADING'
    description = "Getting ready to download the input video from the cloud"


class DownloadingVideoState(CustomTaskState):
    state = 'VIDEO_DOWNLOADING'
    description = "Downloading the input video from the cloud"


class PreparationVideoProcessingState(CustomTaskState):
    state = 'PREPARATION_VIDEO_PROCESSING'
    description = "Preparation to process the video"


class VideoProcessingState(CustomTaskState):
    state = 'VIDEO_PROCESSING'
    description = "Creating video stream files"


class PreparationUploadOutputsState(CustomTaskState):
    state = 'PREPARATION_UPLOAD_OUTPUTS'
    description = "Preparation to upload the output files to the cloud"


class UploadingOutputsState(CustomTaskState):
    state = 'UPLOADING_OUTPUTS'
    description = "Uploading the output files to the cloud"
