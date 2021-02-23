"""
settings
"""

from decouple import Config, RepositoryEnv
from .constants import ENV_FILE_PATH


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


# load task modules from modules
AUTO_DISCOVER_TASKS = [
    f'{PROJECT_NAME}.core',
    f'{PROJECT_NAME}.grpc',
    f'{PROJECT_NAME}.ffmpeg'
]

