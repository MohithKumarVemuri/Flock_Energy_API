import time
from typing import Optional, Any, Dict, Tuple


class TTLCache:
    """Lightweight in-memory cache with per-item TTL expiration."""

    def __init__(self):
        # Maps key -> (value, expiry_timestamp)
        self._store: Dict[str, Tuple[Any, float]] = {}

    def get(self, key: str) -> Tuple[Optional[Any], bool]:
        """
        Retrieve item from cache.
        Returns: (value, is_hit)
        """
        now = time.time()
        if key in self._store:
            val, expiry = self._store[key]
            if now < expiry:
                return val, True
            else:
                del self._store[key]
        return None, False

    def set(self, key: str, value: Any, ttl_seconds: float) -> None:
        """Store item with a TTL in seconds."""
        self._store[key] = (value, time.time() + ttl_seconds)

    def invalidate(self, key: str) -> None:
        """Remove item from cache."""
        self._store.pop(key, None)

    def clear(self) -> None:
        """Clear entire cache."""
        self._store.clear()


# Global cache instances for meters and consumption
cache = TTLCache()
