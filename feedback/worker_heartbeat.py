"""Worker liveness file for a supervising launcher (local node).

A plain file rather than a database row: the launcher and console must still
see a stopped worker when the database itself is what broke.
"""

import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)


def write_heartbeat(path, state, *, clock=time.time, pid=None):
    path = Path(path)
    payload = json.dumps({"at": clock(), "state": state, "pid": os.getpid() if pid is None else pid})
    temporary = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        # A reader holding the file on Windows must never crash the worker.
        logger.debug("heartbeat write skipped", exc_info=True)


def read_heartbeat(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("at"), (int, float)):
        return None
    return data
