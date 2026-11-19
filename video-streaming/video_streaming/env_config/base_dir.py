import os

BASE_DIR = os.path.dirname(  # video_streaming dir
    os.path.dirname(  # env_config dir
        os.path.abspath(__file__)
    )
)
