import json
import redis
from video_streaming import settings


class RedisCache:
    TIMEOUT_SECOND = settings.REDIS_TIMEOUT_SECOND
    REDIS_URL = settings.REDIS_URL

    def __init__(self, url=None, **kwargs):
        url = url or self.REDIS_URL
        self.redis = redis.from_url(
            url,
            charset="utf-8",
            decode_responses=True,
            **kwargs)

    def set(self, key, value, timeout: int = None):
        if timeout is None:
            timeout = self.TIMEOUT_SECOND
        self.redis.set(key, value, ex=timeout)

    def incr(self, key, amount: int = 1):
        self.redis.incr(key, amount=amount)

    def get(self, key):
        # getting right type of float and int values
        value = self.redis.get(key)
        if value is not None:
            return json.loads(value)

    def delete(self, *key):
        return self.redis.delete(*key)


