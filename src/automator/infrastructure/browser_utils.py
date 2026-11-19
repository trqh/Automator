#!/usr/bin/env python3
from __future__ import annotations

import os
import threading
from typing import Optional

_chrome_lock = threading.Lock()
_cached_chromedriver_path: Optional[str] = None


def resolve_chromedriver_path() -> Optional[str]:
    """
    Resolve ChromeDriver path in a cross-platform way.

    Priority:
    1. CHROMEDRIVER_PATH environment variable, if it points to an existing path
    2. webdriver_manager auto-install result

    Returns:
        Absolute/usable path to chromedriver, or None if resolution fails.
    """
    global _cached_chromedriver_path

    with _chrome_lock:
        if _cached_chromedriver_path is not None:
            return _cached_chromedriver_path

        env_path = os.environ.get("CHROMEDRIVER_PATH", "").strip()
        if env_path and os.path.exists(env_path):
            _cached_chromedriver_path = env_path
            return _cached_chromedriver_path

        try:
            from webdriver_manager.chrome import ChromeDriverManager  # type: ignore

            installed = ChromeDriverManager().install()
            if installed and os.path.exists(installed):
                _cached_chromedriver_path = installed
                return _cached_chromedriver_path
        except Exception:
            _cached_chromedriver_path = None
            return None

        _cached_chromedriver_path = None
        return None