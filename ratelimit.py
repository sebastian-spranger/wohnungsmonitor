#!/usr/bin/env python3
"""
webapp/ratelimit.py — winziger In-Memory-Rate-Limiter
======================================================

API: `ok, retry_after = ratelimit.hit(key, [(n, window_sec), ...])`
Registriert einen Request unter `key` und prüft, ob er erlaubt ist.
Mehrere Limits werden ODER-verknüpft (jede Grenze einzeln). In-Memory reicht
für eine Single-Process-uvicorn-Instanz; bei mehreren Workern bitte auf Redis
o.ä. umstellen.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Dict, List, Tuple

_hits: Dict[str, deque] = defaultdict(deque)  # key -> Timestamps


def hit(key: str, limits: List[Tuple[int, int]]) -> Tuple[bool, int]:
    """Prüfe + registriere einen Request.
    Rückgabe: (erlaubt: bool, retry_after_sec: int)."""
    now = time.time()
    dq = _hits[key]
    window = max(w for _, w in limits)
    # alte Timestamps aufräumen
    while dq and now - dq[0] > window:
        dq.popleft()
    blocked = False
    retry_after = 0
    for n, w in limits:
        cnt = sum(1 for t in dq if now - t <= w)
        if cnt >= n:
            blocked = True
            retry_after = max(retry_after, int(w - (now - dq[0])) + 1)
    if not blocked:
        dq.append(now)
    return (not blocked), retry_after
