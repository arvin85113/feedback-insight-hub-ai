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


def cloud_status(link=None):
    from cloudsync.models import CloudLink

    link = CloudLink.load() if link is None else link
    if not link.is_linked:
        return StatusItem("cloud", "雲端連線", "off", "未連線")
    if link.last_error_kind == "unauthorized":
        return StatusItem("cloud", "雲端連線", "warn", "雲端連線已撤銷，請重新連結")
    if link.last_error_kind:
        return StatusItem("cloud", "雲端連線", "warn", f"無法同步（{link.last_error_kind}）")
    if link.last_success_at is None:
        return StatusItem("cloud", "雲端連線", "off", "已連結，尚未同步")
    minutes = int((time.time() - link.last_success_at.timestamp()) // 60)
    return StatusItem("cloud", "雲端連線", "ok", f"已連線 · 上次同步 {minutes} 分鐘前")


def inbox_status(link=None):
    from cloudsync.models import CloudLink, PendingAck

    link = CloudLink.load() if link is None else link
    if not link.is_linked:
        return StatusItem("inbox", "收件匣", "off", "未連線")
    inbox = link.inbox_status or {}
    pending = int(inbox.get("pending_count") or 0)
    if inbox.get("deadline_state") in ("warn", "critical"):
        return StatusItem("inbox", "收件匣", "warn", f"待收 {pending} 筆，最舊一筆已超過處理期限")
    if int(inbox.get("quarantined_count") or 0):
        return StatusItem("inbox", "收件匣", "warn", f"待收 {pending} 筆，有衝突項目待處理")
    if PendingAck.objects.exclude(last_status="").exists():
        return StatusItem("inbox", "收件匣", "warn", f"待收 {pending} 筆，有回覆尚未確認同步")
    return StatusItem("inbox", "收件匣", "ok", f"待收 {pending} 筆")


def results_status(link=None):
    from django.db.models.fields.json import KT

    from cloudsync.models import CloudLink, ResultUpload, SyncedSubmissionSource
    from feedback.models import SurveyAnalysisState

    link = CloudLink.load() if link is None else link
    if not link.is_linked:
        return StatusItem("results", "結果上傳", "off", "未連線")
    from cloudsync.publication_status import publication_issues
    issues = publication_issues()
    missing_binding = issues.filter(cloud_bound=False).count()
    if missing_binding:
        return StatusItem("results", "結果上傳", "warn", f"{missing_binding} 份已在本機發布，但尚未接上雲端發布")
    if issues.exists():
        return StatusItem("results", "結果上傳", "warn", "已發布結果尚未建立上傳紀錄；下次同步會補建")
    failed = ResultUpload.objects.filter(status=ResultUpload.Status.FAILED).count()
    if failed:
        return StatusItem("results", "結果上傳", "warn", f"{failed} 份結果上傳失敗")
    pending = ResultUpload.objects.filter(status=ResultUpload.Status.PENDING).count()
    # Fixed number of queries however many surveys: read watermarks without loading Snapshot JSON.
    rows = (
        SurveyAnalysisState.objects.filter(survey__sync_state__isnull=False)
        .annotate(watermark=KT("published_snapshot__source_snapshot__data_scope__analyzed_through_sequence"))
        .values_list("survey_id", "published_snapshot__response_count", "watermark")
    )
    watermarks, analysed = {}, 0
    for survey_id, response_count, watermark in rows:
        analysed += response_count or 0
        watermarks[survey_id] = int(watermark or 0)
    waiting = sum(
        1
        for survey_id, sequence in SyncedSubmissionSource.objects.filter(
            submission__survey_id__in=list(watermarks)
        ).values_list("submission__survey_id", "response_sequence")
        if sequence > watermarks[survey_id]
    )
    return StatusItem("results", "結果上傳", "ok", f"已分析 {analysed} 筆 · 尚未分析 {waiting} 筆 · 待上傳 {pending} 份")


PENDING_MESSAGES = {
    "worker": "分析 Worker 需要處理：請從系統匣結束並重新開啟程式，再查看日誌。",
    "disk": "磁碟剩餘空間不足 2 GB，分析產物可能無法寫入。",
    "database": "資料庫無法連線，請查看日誌。",
    "cloud": "雲端同步需要處理：請到「雲端連線」查看。",
    "inbox": "收件匣需要處理：請到「雲端連線」查看待收期限與衝突項目。",
    "results": "結果上傳需要處理：請到「雲端連線」查看。",
}


def pending_items(items):
    return [PENDING_MESSAGES[item.key] for item in items if item.state == "warn" and item.key in PENDING_MESSAGES]
