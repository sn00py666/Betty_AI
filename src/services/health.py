"""Общие файлы состояния для бота, сборщика и отдельного монитора."""

import asyncio
import json
import os
import re
import time
from pathlib import Path


def read(path: Path):
    if not path.exists():
        return {}
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("Ожидался JSON-объект")
    return value


def write(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False))
    temporary.replace(path)


def safe_error(value):
    text = re.sub(r"https?://\S+", "[URL]", str(value or ""))
    text = re.sub(r"\b\d{6,}:[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9_-]+", "[секрет скрыт]", text)
    return text[:180]


def api_state(path: Path, *, error=None, status=None, retry_at=0):
    try:
        state = read(path)
    except (OSError, ValueError):
        state = {}
    now = time.time()
    state.update(last_attempt=now, error=safe_error(error) or None, http_status=status, retry_at=retry_at)
    if error is None:
        state["last_success"] = now
    write(path, state)


async def heartbeat(path: Path, **details):
    started = time.time()
    while True:
        await asyncio.to_thread(
            write, path, dict(updated_at=time.time(), started_at=started, pid=os.getpid(), **details)
        )
        await asyncio.sleep(30)
