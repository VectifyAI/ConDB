"""Prefix cache implementation for Block-level Beam Search."""

import hashlib
import sys
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class CacheEntry:
    """Single cache entry with metadata."""

    key: str
    value: Any
    created_at: float
    accessed_at: float
    hit_count: int = 0
    size_bytes: int = 0


class PrefixCache:
    """LRU cache for block content and LLM results."""

    def __init__(
        self,
        max_size: int = 1000,  # Max entries
        max_memory_mb: int = 100,  # Max memory in MB
        ttl_seconds: int = 3600,  # Time to live
    ):
        self.max_size = max_size
        self.max_memory_bytes = max_memory_mb * 1024 * 1024
        self.ttl_seconds = ttl_seconds

        self._cache: OrderedDict[str, CacheEntry] = OrderedDict()
        self._current_memory = 0

        # Statistics
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> Optional[Any]:
        """Get value from cache."""
        entry = self._cache.get(key)

        if entry is None:
            self.misses += 1
            return None

        # Check TTL
        if time.time() - entry.created_at > self.ttl_seconds:
            self._remove(key)
            self.misses += 1
            return None

        # Update access time and move to end (LRU)
        entry.accessed_at = time.time()
        entry.hit_count += 1
        self._cache.move_to_end(key)

        self.hits += 1
        return entry.value

    def set(self, key: str, value: Any) -> None:
        """Set value in cache."""
        # Estimate size
        size = sys.getsizeof(str(value))

        # Evict if necessary
        while len(self._cache) >= self.max_size or self._current_memory + size > self.max_memory_bytes:
            if not self._cache:
                break
            self._evict_oldest()

        # Add entry
        now = time.time()
        entry = CacheEntry(key=key, value=value, created_at=now, accessed_at=now, size_bytes=size)

        self._cache[key] = entry
        self._current_memory += size

    def _remove(self, key: str) -> None:
        """Remove entry from cache."""
        entry = self._cache.pop(key, None)
        if entry:
            self._current_memory -= entry.size_bytes

    def _evict_oldest(self) -> None:
        """Evict oldest (least recently used) entry."""
        if self._cache:
            key = next(iter(self._cache))
            self._remove(key)

    def clear(self) -> None:
        """Clear all cache entries."""
        self._cache.clear()
        self._current_memory = 0
        self.hits = 0
        self.misses = 0

    def stats(self) -> dict:
        """Return cache statistics."""
        total_requests = self.hits + self.misses
        hit_rate = self.hits / total_requests if total_requests > 0 else 0

        return {
            "entries": len(self._cache),
            "memory_mb": self._current_memory / (1024 * 1024),
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": hit_rate,
        }

    def contains(self, key: str) -> bool:
        """Check if key exists in cache (without updating access time)."""
        return key in self._cache


class BlockContentCache:
    """Specialized cache for block content strings."""

    def __init__(self, max_blocks: int = 500):
        self.max_blocks = max_blocks
        self._content_cache: OrderedDict[str, str] = OrderedDict()
        self._hash_to_block: dict[str, str] = {}

    def get_or_create(self, block_id: str, content_generator) -> tuple[str, str]:
        """Get cached content or generate and cache. Returns (content, content_hash)."""
        if block_id in self._content_cache:
            content = self._content_cache[block_id]
            self._content_cache.move_to_end(block_id)
            return content, self._hash_content(content)

        # Generate content
        content = content_generator()
        content_hash = self._hash_content(content)

        # Evict if necessary
        while len(self._content_cache) >= self.max_blocks:
            oldest_id = next(iter(self._content_cache))
            old_content = self._content_cache[oldest_id]
            old_hash = self._hash_content(old_content)
            self._hash_to_block.pop(old_hash, None)
            del self._content_cache[oldest_id]

        self._content_cache[block_id] = content
        self._hash_to_block[content_hash] = block_id

        return content, content_hash

    def _hash_content(self, content: str) -> str:
        """Generate hash for content."""
        return hashlib.md5(content.encode()).hexdigest()

    def get_by_hash(self, content_hash: str) -> Optional[str]:
        """Get content by hash (for cache lookup)."""
        block_id = self._hash_to_block.get(content_hash)
        if block_id:
            return self._content_cache.get(block_id)
        return None

    def get(self, block_id: str) -> Optional[str]:
        """Get cached content by block_id."""
        content = self._content_cache.get(block_id)
        if content:
            self._content_cache.move_to_end(block_id)
        return content

    def clear(self) -> None:
        """Clear all cached content."""
        self._content_cache.clear()
        self._hash_to_block.clear()
