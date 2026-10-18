"""Shared data scope for every script: only activities from DATA_SINCE onward.

fetch.py stores and downloads only these; analyze/progress/story read only
these, so older files already on disk are ignored without a re-sync.
"""
from datetime import datetime, timezone

DATA_SINCE = datetime(2026, 1, 1)


def local_start(raw_ts):
    """start_date_local_raw is the wall-clock time encoded as a UTC epoch.

    Decode it as UTC, otherwise the machine's timezone shifts it (+7h in WIB
    turns a 17:24 run into 00:24 the next day).
    """
    return datetime.fromtimestamp(raw_ts, timezone.utc).replace(tzinfo=None)


def in_scope(models):
    """Raw Strava activity models that start on or after DATA_SINCE."""
    return [m for m in models if local_start(m["start_date_local_raw"]) >= DATA_SINCE]
