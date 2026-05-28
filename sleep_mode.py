"""
Sleep Mode (Module #3a) — manual quiet hours.

When ON:
  - Watcher narrative alerts are QUEUED to Redis, not sent
  - Position TP/SL/chat replies fire normally (user's call)

When turned OFF:
  - Wake-up summary is sent: how many narrative alerts queued, headline of each
  - Queue is drained

Storage:
  sleep:enabled   "1" or "0"
  sleep:queue     JSON list of {ts, text} entries (capped at 50)
  sleep:since     timestamp when sleep was last turned on
"""

import os
import json
import time
import logging
import redis

log = logging.getLogger(__name__)

_redis = redis.from_url(
    os.environ.get("REDIS_URL", "redis://localhost:6379"),
    decode_responses=True,
    ssl_cert_reqs=None,
)

K_ENABLED = "sleep:enabled"
K_QUEUE   = "sleep:queue"
K_SINCE   = "sleep:since"
MAX_QUEUE = 50


def is_sleeping() -> bool:
    try:
        return _redis.get(K_ENABLED) == "1"
    except Exception:
        return False


def turn_on() -> dict:
    try:
        _redis.set(K_ENABLED, "1")
        _redis.set(K_SINCE, str(int(time.time())))
        return {"ok": True, "since": int(time.time())}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def turn_off() -> dict:
    """Returns drained queue + how long sleep lasted."""
    try:
        since = _redis.get(K_SINCE)
        queue = _drain_queue()
        _redis.set(K_ENABLED, "0")
        duration_mins = None
        if since:
            duration_mins = int((time.time() - int(since)) / 60)
        return {"ok": True, "queue": queue, "duration_mins": duration_mins}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def queue_alert(text: str) -> bool:
    """Queue a watcher alert during sleep. Returns True if queued, False if not sleeping."""
    if not is_sleeping():
        return False
    try:
        entry = {"ts": int(time.time()), "text": text}
        pipe = _redis.pipeline()
        pipe.lpush(K_QUEUE, json.dumps(entry))
        pipe.ltrim(K_QUEUE, 0, MAX_QUEUE - 1)
        pipe.execute()
        return True
    except Exception as e:
        log.warning(f"sleep_mode queue failed: {e}")
        return False


def _drain_queue() -> list:
    try:
        raw = _redis.lrange(K_QUEUE, 0, -1)
        _redis.delete(K_QUEUE)
        return [json.loads(x) for x in raw if x]
    except Exception:
        return []


def queue_size() -> int:
    try:
        return _redis.llen(K_QUEUE)
    except Exception:
        return 0


def status() -> dict:
    sleeping = is_sleeping()
    out = {"sleeping": sleeping, "queue_size": queue_size()}
    if sleeping:
        try:
            since = _redis.get(K_SINCE)
            if since:
                out["since_ts"]   = int(since)
                out["mins_asleep"] = int((time.time() - int(since)) / 60)
        except Exception:
            pass
    return out


def format_wake_summary(queue: list, duration_mins: int = None) -> str:
    lines = ["🌅 *Good morning.*"]
    if duration_mins is not None:
        h = duration_mins // 60
        m = duration_mins % 60
        lines.append(f"_Sleep mode was on for {h}h {m}m._")
    lines.append("")
    if not queue:
        lines.append("✅ Nothing queued. No watcher alerts while you slept.")
    else:
        lines.append(f"📨 *{len(queue)} watcher alerts queued:*\n")
        for i, entry in enumerate(queue, 1):
            text = entry.get("text", "")
            # Pull just the headline (first 2 lines) from each alert
            preview = "\n".join(text.split("\n")[:3])
            lines.append(f"{i}. {preview}")
            lines.append("")
    lines.append("_Run `/alerts` to see all alert details._")
    return "\n".join(lines)
