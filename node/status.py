"""Node health shown on the console overview.

Each check degrades to a readable StatusItem instead of raising, so the
overview still renders when one subsystem is broken.
"""

import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from django.db import DatabaseError, connection

from feedback.worker_heartbeat import read_heartbeat

LOW_DISK_BYTES = 2 * 1024**3
WORKER_STALE_SECONDS = 60


@dataclass(frozen=True)
class StatusItem:
    key: str
    label: str
    state: str  # "ok" | "warn" | "off"
    summary: str


def human_bytes(value):
    size = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def database_status():
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except DatabaseError:
        return StatusItem("database", "資料庫", "warn", "無法連線")
    name = connection.settings_dict.get("NAME")
    if connection.vendor == "sqlite" and name and Path(str(name)).is_file():
        return StatusItem("database", "資料庫", "ok", f"SQLite · {human_bytes(Path(str(name)).stat().st_size)}")
    return StatusItem("database", "資料庫", "ok", connection.vendor)


def worker_status(paths, *, now=None, stale_after=WORKER_STALE_SECONDS):
    now = time.time() if now is None else now
    try:
        supervisor_state = paths.worker_state_file.read_text(encoding="utf-8").strip()
    except OSError:
        supervisor_state = ""
    if supervisor_state == "stopped":
        return StatusItem("worker", "分析 Worker", "warn", "已停止（短時間內多次異常）")
    heartbeat = read_heartbeat(paths.heartbeat_file)
    if heartbeat is None:
        return StatusItem("worker", "分析 Worker", "off", "尚未啟動")
    age = max(0, int(now - heartbeat["at"]))
    if heartbeat.get("state") == "busy":
        # A long job blocks the loop; the supervisor, not the clock, detects a crash.
        return StatusItem("worker", "分析 Worker", "ok", "處理工作中")
    if age <= stale_after:
        return StatusItem("worker", "分析 Worker", "ok", f"運作中 · {age} 秒前回報")
    return StatusItem("worker", "分析 Worker", "warn", f"無回應 · 最後回報 {age // 60} 分鐘前")


def disk_status(paths, *, usage=shutil.disk_usage, low_bytes=LOW_DISK_BYTES):
    target = paths.root if paths.root.exists() else Path(paths.root.anchor or ".")
    free = usage(target).free
    state = "warn" if free < low_bytes else "ok"
    return StatusItem("disk", "磁碟空間", state, f"剩餘 {human_bytes(free)}")


def lan_status():
    return StatusItem("lan", "區域網路", "off", "未開放（僅限本機）")


def cloud_status():
    return StatusItem("cloud", "雲端連線", "off", "未連線")


PENDING_MESSAGES = {
    "worker": "分析 Worker 需要處理：請從系統匣結束並重新開啟程式，再查看日誌。",
    "disk": "磁碟剩餘空間不足 2 GB，分析產物可能無法寫入。",
    "database": "資料庫無法連線，請查看日誌。",
}


def pending_items(items):
    return [PENDING_MESSAGES[item.key] for item in items if item.state == "warn" and item.key in PENDING_MESSAGES]
