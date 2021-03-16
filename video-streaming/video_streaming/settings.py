"""
settings
"""
from decouple import Config, RepositoryEnv
from video_streaming.env_config import ENV_FILE_PATH

env_config = Config(RepositoryEnv(ENV_FILE_PATH))
PROJECT_NAME = "video_streaming"

##################################################
#   celery                                       #
##################################################

#: Only add pickle to this list if your broker is secured
#: from unwanted access (see userguide/security.html)

CELERY_BROKER_URL = env_config.get(
    "CELERY_BROKER_URL",
    default="",
    cast=str)
CELERY_RESULT_BACKEND = env_config.get(
    "CELERY_RESULT_BACKEND",
    default="",
    cast=str)
CELERY_ACCEPT_CONTENT = ['application/json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'
CELERY_BROKER_HEARTBEAT = 60


# load task modules from modules
AUTO_DISCOVER_TASKS = [
    f'{PROJECT_NAME}.core',
    f'{PROJECT_NAME}.grpc',
    f'{PROJECT_NAME}.ffmpeg'
]

BASE_TASK_CLASS = 'video_streaming.core.tasks:BaseTask'
TASK_RETRY_BACKOFF_MAX = env_config.get(
    "TASK_RETRY_BACKOFF_MAX",
    default=10,
    cast=int)
TASK_RETRY_FFMPEG_COMMAND_MAX = env_config.get(
    "TASK_RETRY_BACKOFF_MAX",
    default=1,
    cast=int)

##################################################
#    Tasks Default Parameters                    #
##################################################

# HLS or MPEG-Dash
DEFAULT_PLAYLIST_IS_HLS = env_config.get(
    "DEBUG_PLAYLIST_IS_HLS",
    default=True,
    cast=bool)

# to use fmp4 segment type in HLS playlist
DEFAULT_SEGMENT_TYPE_IS_FMP4 = env_config.get(
    "DEFAULT_SEGMENT_TYPE_IS_FMP4",
    default=True,
    cast=bool)

# Check if s3_output_key is already exist, ignore the task
DONT_REPLACE_OUTPUT = env_config.get(
    "DONT_REPLACE_OUTPUT",
    default=True,
    cast=bool)

# Create the output bucket If not exist
CREATE_OUTPUT_BUCKET = env_config.get(
    "CREATE_OUTPUT_BUCKET",
    default=True,
    cast=bool)

# The encode format of playlist,
# e.g "h264", "hevc" or "vp9"
DEFAULT_ENCODE_FORMAT = env_config.get(
    "DEFAULT_ENCODE_FORMAT",
    default="h264",
    cast=str)


##################################################
#    S3 Object Storage                           #
##################################################

"""
Setup s3 client
"""

S3_ENDPOINT_URL = env_config.get(
    "S3_ENDPOINT_URL",
    default="",
    cast=str)
S3_ACCESS_KEY_ID = env_config.get(
    "S3_ACCESS_KEY_ID",
    default="",
    cast=str)
S3_SECRET_ACCESS_KEY = env_config.get(
    "S3_SECRET_ACCESS_KEY",
    default="",
    cast=str)
S3_REGION_NAME = env_config.get(
    "S3_REGION_NAME",
    default=None,
    cast=str)
S3_IS_SECURE = env_config.get(
    "S3_IS_SECURE",
    default=False,
    cast=bool)

"""
Default bucket names
"""

S3_DEFAULT_BUCKET = env_config.get(
    "S3_DEFAULT_BUCKET",
    default="",
    cast=str)
S3_DEFAULT_INPUT_BUCKET_NAME = env_config.get(
    "S3_DEFAULT_INPUT_BUCKET_NAME",
    default=S3_DEFAULT_BUCKET,
    cast=str)
S3_DEFAULT_OUTPUT_BUCKET_NAME = env_config.get(
    "S3_DEFAULT_INPUT_BUCKET_NAME",
    default=S3_DEFAULT_BUCKET,
    cast=str)

"""
Configuration object for managed S3 transfers
"""

# The transfer size threshold for which multipart uploads, downloads,
# and copies will automatically be triggered.
S3_TRANSFER_MULTIPART_THRESHOLD = env_config.get(
    "S3_TRANSFER_MULTIPART_THRESHOLD",
    default=8 * 1024 * 1024,  # 8MB
    cast=int)

# The maximum number of threads that will be making requests to perform
# a transfer. If use_threads is set to False, the value provided is
# ignored as the transfer will only ever use the main thread.
S3_TRANSFER_MAX_CONCURRENCY = env_config.get(
    "S3_TRANSFER_MAX_CONCURRENCY",
    default=10,
    cast=int)

# The partition size of each part for a multipart transfer.
S3_TRANSFER_MULTIPART_CHUNKSIZE = env_config.get(
    "S3_TRANSFER_MULTIPART_CHUNKSIZE",
    default=8 * 1024 * 1024,  # 8MB
    cast=int)

# The number of download attempts that will be retried upon errors
# with downloading an object in S3. Note that these retries account
# for errors that occur when streaming down the data from s3
# (i.e. socket errors and read timeouts that occur after receiving
# an OK response from s3). Other retryable exceptions such as
# throttling errors and 5xx errors are already retried by botocore
# (this default is 5). This does not take into account the number
# of exceptions retried by botocore.
S3_TRANSFER_NUM_DOWNLOAD_ATTEMPTS = env_config.get(
    "S3_TRANSFER_NUM_DOWNLOAD_ATTEMPTS",
    default=5,
    cast=int)

# The maximum amount of read parts that can be queued in memory to be
# written for a download. The size of each of these read parts is at
# most the size of io_chunksize.
S3_TRANSFER_MAX_IO_QUEUE = env_config.get(
    "S3_TRANSFER_MAX_IO_QUEUE",
    default=100,
    cast=int)

# The max size of each chunk in the io queue. Currently, this is size
# used when read is called on the downloaded stream as well.
S3_TRANSFER_IO_CHUNKSIZE = env_config.get(
    "S3_TRANSFER_IO_CHUNKSIZE",
    default=256 * 1024,  # 256KB
    cast=int)

# If True, threads will be used when performing S3 transfers. If False,
# no threads will be used in performing transfers: all logic will be ran
# in the main thread.
S3_TRANSFER_USE_THREADS = env_config.get(
    "S3_TRANSFER_USE_THREADS",
    default=True,
    cast=bool)

##################################################
#    File System                                 #
##################################################

# temporary directory of downloaded videos
TMP_DOWNLOADED_DIR = env_config.get(
    "TMP_DOWNLOADED_DIR",
    default="",
    cast=str)
# temporary directory of processed videos
TMP_PROCESSED_DIR = env_config.get(
    "TMP_PROCESSED_DIR",
    default="",
    cast=str)

##################################################
#    FFmpeg                                      #
##################################################

FFPROBE_BIN_PATH = env_config.get(
    "FFPROBE_BIN_PATH",
    default="/usr/local/bin/ffprobe",
    cast=str)
FFMPEG_BIN_PATH = env_config.get(
    "FFMPEG_BIN_PATH",
    default="/usr/local/bin/ffmpeg",
    cast=str)

##################################################
#    Redis                                       #
##################################################

REDIS_TIMEOUT_SECOND = env_config.get(
    "REDIS_TIMEOUT_SECOND",
    default=60 * 60 * 24,
    cast=int)

REDIS_URL = env_config.get(
    "REDIS_URL",
    default="",
    cast=str
)
