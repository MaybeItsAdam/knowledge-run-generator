"""Shared cache-directory resolution.

Expensive artefacts (the London drive graph, street/alias indexes, geocoding
caches) used to live in ``/tmp/app_cache`` and evaporated on reboot. They now
default to a persistent per-user cache directory, overridable with
``KRG_CACHE_DIR``. The legacy ``/tmp/app_cache`` is still consulted read-only:
any file already cached there is copied across once, so existing checkouts do
not re-download the graph.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

_LEGACY_DIR = Path("/tmp/app_cache")


def cache_dir() -> Path:
    """Return the cache directory, creating it if needed."""
    root = os.environ.get("KRG_CACHE_DIR")
    path = Path(root) if root else Path.home() / ".cache" / "knowledge-run-generator"
    path.mkdir(parents=True, exist_ok=True)
    return path


def cache_path(name: str) -> Path:
    """Path for a named cache file, migrating a legacy /tmp copy if present."""
    target = cache_dir() / name
    if not target.exists():
        legacy = _LEGACY_DIR / name
        if legacy.exists():
            try:
                shutil.copy2(legacy, target)
            except OSError:
                pass
    return target
