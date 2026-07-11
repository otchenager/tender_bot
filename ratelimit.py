"""Tiny in-memory per-IP rate limiter.

Railway runs the app as a single process, so an in-process sliding window is
sufficient — no Redis/flask-limiter dependency needed. If the deployment ever
scales to multiple workers, swap the storage here for a shared backend.
"""

import time
from collections import defaultdict, deque
from functools import wraps

from flask import jsonify, request

from logger import get_logger

log = get_logger("ratelimit")

_hits: dict[tuple, deque] = defaultdict(deque)


def _client_ip() -> str:
    # Railway sits behind a proxy: the real client is the first entry of
    # X-Forwarded-For; fall back to the socket address for local runs.
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.remote_addr or "?"


def check(key: tuple, max_requests: int, per_seconds: int) -> bool:
    """Record a hit for key; False when the sliding window is exhausted."""
    now = time.time()
    window = _hits[key]
    while window and window[0] <= now - per_seconds:
        window.popleft()
    if len(window) >= max_requests:
        return False
    window.append(now)
    return True


def rate_limit(max_requests: int, per_seconds: int):
    """Sliding-window limit per (route, client IP). Returns HTTP 429 JSON on
    excess — safe for both API callers and browser pages."""
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            ip = _client_ip()
            if not check((fn.__name__, ip), max_requests, per_seconds):
                log.warning(f"Rate limit hit: {fn.__name__} from {ip}")
                return jsonify({"error": "rate limit exceeded"}), 429
            return fn(*args, **kwargs)
        return wrapper
    return decorator


def global_rate_limit(max_requests: int = 120, per_seconds: int = 60):
    """App-wide per-IP guard for all public routes. Sustained >2 req/s from
    one address is not a human on this dashboard."""
    ip = _client_ip()
    if not check(("_global", ip), max_requests, per_seconds):
        log.warning(f"Global rate limit hit from {ip}")
        return jsonify({"error": "rate limit exceeded"}), 429
    return None
