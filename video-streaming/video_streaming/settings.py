"""
settings
"""
from decouple import Config, RepositoryEnv
from .env_config import ENV_FILE_PATH

env_config = Config(RepositoryEnv(ENV_FILE_PATH))
PROJECT_NAME = "video_streaming"

##################################################
#   celery                                       #
##################################################

#: Only add pickle to this list if your broker is secured
#: from unwanted access (see userguide/security.html)

CELERY_BROKER_URL = env_config.get('CELERY_BROKER_URL', cast=str)
CELERY_RESULT_BACKEND = env_config.get('CELERY_RESULT_BACKEND', cast=str)
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

##################################################
#    S3 Object Storage                           #
##################################################

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
S3_DEFAULT_INPUT_BUCKET_NAME = env_config.get(
    "S3_DEFAULT_INPUT_BUCKET_NAME",
    default="",
    cast=str)
S3_DEFAULT_OUTPUT_BUCKET_NAME = env_config.get(
    "S3_DEFAULT_INPUT_BUCKET_NAME",
    default="",
    cast=str)

