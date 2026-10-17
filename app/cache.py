import json
import threading
import time
from collections import OrderedDict
import redis


class Cache:
    """Redis is an optimization; the database generation is the invalidation authority."""

    def __init__(self, settings):
        self.settings = settings
        self.redis = (
            redis.Redis.from_url(settings.redis_url, socket_connect_timeout=1, socket_timeout=1)
            if settings.redis_url
            else None
        )
        self.memory = OrderedDict()
        self.limits = {}
        self.lock = threading.Lock()

    def get(self, key):
        if self.redis:
            try:
                value = self.redis.get(key)
                return json.loads(value) if value else None
            except (redis.RedisError, ValueError):
                pass
        with self.lock:
            record = self.memory.get(key)
            return json.loads(record[1]) if record and record[0] > time.time() else None

    def put(self, key, value):
        encoded = json.dumps(value)
        if self.redis:
            try:
                self.redis.setex(key, self.settings.cache_ttl, encoded)
                return
            except redis.RedisError:
                pass
        with self.lock:
            self.memory[key] = (time.time() + self.settings.cache_ttl, encoded)
            self.memory.move_to_end(key)
            while len(self.memory) > 256:
                self.memory.popitem(last=False)

    def allow(self, tenant_id):
        window = int(time.time() // 60)
        key = f"drag:rate:{tenant_id}:{window}"
        if self.redis:
            try:
                # Both operations execute atomically, avoiding immortal counters after crashes.
                pipe = self.redis.pipeline(transaction=True)
                pipe.incr(key)
                pipe.expire(key, 120)
                return pipe.execute()[0] <= self.settings.rate_limit
            except redis.RedisError:
                pass
        with self.lock:
            if len(self.limits) > 10000:
                self.limits = {k: v for k, v in self.limits.items() if v[0] == window}
            prev_window, count = self.limits.get(tenant_id, (window, 0))
            count = count + 1 if prev_window == window else 1
            self.limits[tenant_id] = (window, count)
            return count <= self.settings.rate_limit
