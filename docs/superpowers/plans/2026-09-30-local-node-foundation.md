# 本機節點基礎（計畫 A）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓同一個 EXE 以「本機節點」模式啟動：系統匣＋本機網頁主控台、首次設定建立擁有者、以本機帳號（allauth）登入、總覽頁顯示節點狀態，現有問卷與分析頁在主控台內可用；雲端（Render）行為不變。

**Architecture:** `DEPLOYMENT_MODE=node` 在 `config/settings.py` 末段切換資料庫（本機 SQLite WAL）、機密來源（檔案）、已安裝 app（`organizations`、`node`、allauth）與網址；`cloud` 模式完全不載入這些 app。啟動器以 cheroot 提供 WSGI、以子程序執行既有的 `run_analysis_worker`，兩者之間只透過檔案（心跳、狀態、埠）溝通，因此資料庫故障時仍看得到 Worker 狀態。

**Tech Stack:** Django 6.0.8、django-allauth 65.19.5（account）、cheroot、pystray＋Pillow、SQLite WAL、PyInstaller one-folder、Python 3.13。

**Spec:** [本機節點：主控台外殼與登入](../specs/2026-09-30-local-node-console-and-auth-design.md)、[架構總覽](../specs/2026-09-30-local-node-architecture-design.md)

**範圍切分：** 規格（子專案 1＋2a）拆成兩份計畫，各自可交付：
- **計畫 A（本文件）**：模式開關、資料目錄、組織與稽核模型、首次設定、本機帳號登入與速率限制、閒置逾時、總覽與設定頁、Worker 心跳與監督、啟動器與系統匣、EXE 打包、CI 雙模式。
- **計畫 B（之後另寫）**：Google 登入（桌面用戶端＋PKCE、受邀限制）、邀請與成員頁、兩步驟驗證、重新驗證、擁有權轉移、區域網路 HTTPS 與防火牆、keyring 機密、登入鎖定稽核、SQLite 鎖定／損毀說明頁、總覽的「擁有者未啟用兩步驟驗證」提醒。

## Global Constraints

- `DEPLOYMENT_MODE` 只接受 `cloud`（預設）與 `node`；其他值啟動即失敗。
- 資料根目錄：`%LOCALAPPDATA%\FeedbackInsightHub\`；可由 `FEEDBACK_HUB_NODE_HOME` 覆寫（測試、CI、開發機試跑用）。
- 本機節點資料庫：預設 `data\node.sqlite3`（WAL）；**只讀 `NODE_DATABASE_URL`，不讀 `DATABASE_URL`**（規格修訂：開發機 `.env` 的 `DATABASE_URL` 指向 Supabase，node 模式不得誤用）。
- 主控台預設綁定 `127.0.0.1:8750`，占用時依序嘗試到 `8769`。
- 閒置逾時預設 4 小時（`SESSION_COOKIE_AGE=14400`、`SESSION_SAVE_EVERY_REQUEST=True`）；本計畫只有 HTTP 本機連線，Cookie 不加 Secure。
- Worker 重啟上限：5 分鐘內最多重啟 3 次，第 4 次異常結束即停止並在總覽警示。
- node 模式的 Worker 不帶 `--allow-paid-ai`；Gemini 金鑰改由計畫 B 的 keyring 管理。
- 使用者可見文字用繁體中文；程式註解沿用英文。
- 所有 commit、push、開 PR 須依 AGENTS.md 取得使用者在執行當次的授權；分支 `feat/local-node-foundation`，PR 由使用者合併。
- node 模式測試指令（PowerShell）：
  `$env:DEPLOYMENT_MODE='node'; $env:FEEDBACK_HUB_NODE_HOME="$env:TEMP\fih-node-test"; .\.venv\Scripts\python.exe manage.py test <labels> --settings=config.settings_test`
  結束後 `Remove-Item Env:DEPLOYMENT_MODE, Env:FEEDBACK_HUB_NODE_HOME`。cloud 模式測試指令不設這兩個變數。

## Review Focus

1. **EXE 被連點兩次**：第二個行程應打開既有主控台後結束，不能再起一組伺服器與 Worker 搶同一個 SQLite（Task 8 `ExistingConsoleTests`）。
2. **開發機 `.env` 帶 Supabase `DATABASE_URL` 時以 node 模式啟動**：必須仍用本機 SQLite（Task 2 `NodeSettingsSubprocessTests`）。
3. **Worker 正在跑長時間工作**：心跳暫停更新時，總覽不能顯示「無回應」（Task 4 忙碌心跳、Task 5 `worker_status` 測試）。
4. **設定頁開著時權杖被重新產生或設定已完成**：舊分頁送出應被拒（403／404），不能 500 或建立第二位擁有者（Task 6 測試）。
5. **Email 大小寫不同**：設定時輸入 `Owner@Example.com`，登入時輸入 `owner@example.com` 仍能登入（Task 7 測試）。

---

### Task 1: 本機節點資料目錄與設定權杖

**Files:**
- Create: `config/node_paths.py`
- Test: `config/test_node_paths.py`

**Interfaces:**
- Produces:
  - `NodePaths(root: Path)`（frozen dataclass），`NodePaths.from_environment(environ=None) -> NodePaths`
  - 屬性：`data_dir`、`database_file`、`artifacts_dir`、`secrets_dir`、`secret_key_file`、`setup_dir`、`setup_token_file`、`run_dir`、`heartbeat_file`、`worker_state_file`、`port_file`、`logs_dir`（皆為 `Path`）
  - `NodePaths.ensure(restrict=restrict_to_current_user) -> None`
  - `restrict_to_current_user(path, *, platform=sys.platform, run=subprocess.run, environ=None) -> None`
  - `load_or_create_secret_key(path: Path) -> str`
  - `issue_setup_token(paths) -> str`、`read_setup_token(paths) -> str | None`、`clear_setup_token(paths) -> None`

- [ ] **Step 1: 寫失敗測試**

```python
# config/test_node_paths.py
import os
import tempfile
from pathlib import Path
from unittest import skipIf

from django.test import SimpleTestCase

from config.node_paths import (
    NodePaths,
    clear_setup_token,
    issue_setup_token,
    load_or_create_secret_key,
    read_setup_token,
    restrict_to_current_user,
)


class NodePathsLocationTests(SimpleTestCase):
    def test_explicit_home_wins_over_local_app_data(self):
        paths = NodePaths.from_environment(
            {"FEEDBACK_HUB_NODE_HOME": "D:/node-home", "LOCALAPPDATA": "C:/Users/x/AppData/Local"}
        )
        self.assertEqual(paths.root, Path("D:/node-home"))

    def test_local_app_data_is_the_default_root(self):
        paths = NodePaths.from_environment({"LOCALAPPDATA": "C:/Users/x/AppData/Local"})
        self.assertEqual(paths.root, Path("C:/Users/x/AppData/Local") / "FeedbackInsightHub")

    def test_non_windows_falls_back_to_home(self):
        paths = NodePaths.from_environment({})
        self.assertEqual(paths.root, Path.home() / ".feedback-insight-hub")

    def test_layout_matches_spec(self):
        paths = NodePaths(Path("R"))
        self.assertEqual(paths.database_file, Path("R/data/node.sqlite3"))
        self.assertEqual(paths.artifacts_dir, Path("R/data/artifacts"))
        self.assertEqual(paths.secret_key_file, Path("R/secrets/secret_key"))
        self.assertEqual(paths.setup_token_file, Path("R/setup/token"))
        self.assertEqual(paths.heartbeat_file, Path("R/run/worker.heartbeat"))
        self.assertEqual(paths.worker_state_file, Path("R/run/worker.state"))
        self.assertEqual(paths.port_file, Path("R/run/port"))
        self.assertEqual(paths.logs_dir, Path("R/logs"))


class NodePathsEnsureTests(SimpleTestCase):
    def test_creates_directories_and_restricts_only_new_private_ones(self):
        restricted = []
        with tempfile.TemporaryDirectory() as directory:
            paths = NodePaths(Path(directory) / "home")
            paths.ensure(restrict=restricted.append)
            for folder in (paths.data_dir, paths.artifacts_dir, paths.secrets_dir, paths.setup_dir, paths.run_dir, paths.logs_dir):
                self.assertTrue(folder.is_dir(), folder)
            self.assertEqual(restricted, [paths.secrets_dir, paths.setup_dir])

            restricted.clear()
            paths.ensure(restrict=restricted.append)
            self.assertEqual(restricted, [])


class RestrictToCurrentUserTests(SimpleTestCase):
    def test_windows_removes_inheritance_and_grants_only_current_user(self):
        calls = []
        restrict_to_current_user(
            Path("C:/n/secrets"),
            platform="win32",
            run=lambda args, **kwargs: calls.append((args, kwargs)),
            environ={"USERNAME": "arvin"},
        )
        args, kwargs = calls[0]
        self.assertEqual(args[0], "icacls")
        self.assertIn("/inheritance:r", args)
        self.assertIn("arvin:(OI)(CI)F", args)
        self.assertTrue(kwargs["check"])

    @skipIf(os.name == "nt", "POSIX permission bits")
    def test_posix_limits_directory_to_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "secrets"
            target.mkdir()
            restrict_to_current_user(target, platform="linux")
            self.assertEqual(target.stat().st_mode & 0o777, 0o700)


class SecretKeyTests(SimpleTestCase):
    def test_key_is_created_once_and_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            key_file = Path(directory) / "secret_key"
            first = load_or_create_secret_key(key_file)
            second = load_or_create_secret_key(key_file)
            self.assertEqual(first, second)
            self.assertGreaterEqual(len(first), 50)


class SetupTokenTests(SimpleTestCase):
    def test_issue_read_and_clear(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = NodePaths(Path(directory))
            self.assertIsNone(read_setup_token(paths))
            first = issue_setup_token(paths)
            self.assertEqual(read_setup_token(paths), first)
            second = issue_setup_token(paths)
            self.assertNotEqual(first, second)
            self.assertEqual(read_setup_token(paths), second)
            clear_setup_token(paths)
            self.assertIsNone(read_setup_token(paths))
            clear_setup_token(paths)  # idempotent
```

- [ ] **Step 2: 執行確認失敗**

Run: `.\.venv\Scripts\python.exe manage.py test config.test_node_paths --settings=config.settings_test`
Expected: FAIL，`ModuleNotFoundError: No module named 'config.node_paths'`

- [ ] **Step 3: 實作**

```python
# config/node_paths.py
"""Filesystem layout of a local node installation (DEPLOYMENT_MODE=node).

Everything lives under one per-user root so backup and uninstall have a single
target.  Only this module decides the layout; settings, the launcher and the
console read paths from here.  Import-safe before Django is configured.
"""

import os
import secrets
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

APP_DIR_NAME = "FeedbackInsightHub"


@dataclass(frozen=True)
class NodePaths:
    root: Path

    @classmethod
    def from_environment(cls, environ=None):
        environ = os.environ if environ is None else environ
        override = environ.get("FEEDBACK_HUB_NODE_HOME", "").strip()
        if override:
            return cls(Path(override).expanduser())
        local_app_data = environ.get("LOCALAPPDATA", "").strip()
        if local_app_data:
            return cls(Path(local_app_data) / APP_DIR_NAME)
        return cls(Path.home() / ".feedback-insight-hub")

    @property
    def data_dir(self):
        return self.root / "data"

    @property
    def database_file(self):
        return self.data_dir / "node.sqlite3"

    @property
    def artifacts_dir(self):
        return self.data_dir / "artifacts"

    @property
    def secrets_dir(self):
        return self.root / "secrets"

    @property
    def secret_key_file(self):
        return self.secrets_dir / "secret_key"

    @property
    def setup_dir(self):
        return self.root / "setup"

    @property
    def setup_token_file(self):
        return self.setup_dir / "token"

    @property
    def run_dir(self):
        return self.root / "run"

    @property
    def heartbeat_file(self):
        return self.run_dir / "worker.heartbeat"

    @property
    def worker_state_file(self):
        return self.run_dir / "worker.state"

    @property
    def port_file(self):
        return self.run_dir / "port"

    @property
    def logs_dir(self):
        return self.root / "logs"

    def ensure(self, restrict=None):
        """Create the layout; lock down private folders the first time they appear."""

        restrict = restrict_to_current_user if restrict is None else restrict
        for folder in (self.data_dir, self.artifacts_dir, self.run_dir, self.logs_dir):
            folder.mkdir(parents=True, exist_ok=True)
        for folder in (self.secrets_dir, self.setup_dir):
            if not folder.is_dir():
                folder.mkdir(parents=True)
                restrict(folder)


def restrict_to_current_user(path, *, platform=sys.platform, run=subprocess.run, environ=None):
    """Only the signed-in account may read the folder; files inside inherit it."""

    if platform == "win32":
        environ = os.environ if environ is None else environ
        user = environ["USERNAME"]
        run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:(OI)(CI)F"],
            check=True,
            capture_output=True,
        )
    else:
        os.chmod(path, 0o700)


def load_or_create_secret_key(path):
    path = Path(path)
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_urlsafe(50)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        # Another process (launcher vs worker) created it first.
        return path.read_text(encoding="utf-8").strip()
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(key)
    return key


def issue_setup_token(paths):
    """A fresh one-time token; any earlier token stops working."""

    token = secrets.token_urlsafe(32)
    paths.setup_dir.mkdir(parents=True, exist_ok=True)
    paths.setup_token_file.write_text(token, encoding="utf-8")
    return token


def read_setup_token(paths):
    try:
        token = paths.setup_token_file.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError):
        return None
    return token or None


def clear_setup_token(paths):
    try:
        paths.setup_token_file.unlink()
    except FileNotFoundError:
        pass
```

- [ ] **Step 4: 執行確認通過**

Run: `.\.venv\Scripts\python.exe manage.py test config.test_node_paths --settings=config.settings_test`
Expected: PASS（Windows 上 POSIX 權限測試為 skipped）

- [ ] **Step 5: Commit（需使用者授權）**

```bash
git add config/node_paths.py config/test_node_paths.py
git commit -m "feat(node): local node data layout, secret key and setup token files"
```

---

### Task 2: 部署模式開關、組織與稽核模型

**Files:**
- Modify: `config/settings.py`（模式判斷放在 `BASE_DIR` 後；`SECRET_KEY` 區塊；檔尾新增 node 區塊）
- Modify: `config/settings_test.py`（加 `NODE_SETUP_GATE = False`）
- Create: `organizations/__init__.py`、`organizations/apps.py`、`organizations/models.py`、`organizations/access.py`、`organizations/admin.py`、`organizations/migrations/__init__.py`、`organizations/migrations/0001_initial.py`（由 makemigrations 產生）
- Create: `node/__init__.py`、`node/apps.py`、`node/models.py`、`node/audit.py`、`node/admin.py`、`node/migrations/__init__.py`、`node/migrations/0001_initial.py`（由 makemigrations 產生）
- Create: `config/test_deployment_mode.py`、`node/tests/__init__.py`、`node/tests/test_models.py`、`organizations/tests.py`
- Modify: `docs/superpowers/specs/2026-09-30-local-node-console-and-auth-design.md`（規格修訂）

**Interfaces:**
- Consumes: Task 1 `NodePaths`、`load_or_create_secret_key`
- Produces:
  - settings：`DEPLOYMENT_MODE: str`、`IS_NODE: bool`、`NODE_PATHS: NodePaths`（僅 node）、`NODE_SETUP_GATE: bool`（僅 node）
  - `organizations.models.Organization`（`name`、`created_at`、`Organization.current() -> Organization | None`）
  - `organizations.models.OrganizationMembership`（`user`、`organization`、`role`、`Role.OWNER="owner"`、`Role.ADMIN="admin"`）
  - `organizations.access.organization_role(user) -> str | None`
  - `node.models.NodeInstallation`（單例 pk=1，`setup_completed_at`、`load()`、`is_setup_complete`、`NodeInstallation.setup_complete() -> bool`（不寫入的查詢））
  - `node.models.NodeAuditEvent`（`actor`、`actor_email`、`action`、`target`、`ip`、`details`、`created_at`）、`node.models.AuditLogImmutable`
  - `node.audit.record(action, *, request=None, actor=None, target="", **details) -> NodeAuditEvent`
  - 動作常數：`SETUP_COMPLETED`、`LOGIN_SUCCEEDED`、`LOGIN_FAILED`、`ORGANIZATION_RENAMED`；`ACTION_LABELS: dict[str, str]`

- [ ] **Step 1: 寫失敗測試（跨模式設定，於 cloud 模式執行）**

```python
# config/test_deployment_mode.py
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

PROBE = (
    "import json, django; from django.conf import settings; django.setup();"
    "db = settings.DATABASES['default'];"
    "print(json.dumps({'mode': settings.DEPLOYMENT_MODE, 'engine': db['ENGINE'], 'name': str(db['NAME']),"
    "'apps': settings.INSTALLED_APPS, 'hosts': settings.ALLOWED_HOSTS,"
    "'secret_from_file': settings.SECRET_KEY == (settings.NODE_PATHS.secret_key_file.read_text(encoding='utf-8').strip())}))"
)


def run_settings_probe(**overrides):
    env = {key: value for key, value in os.environ.items() if key not in {"NODE_DATABASE_URL", "DJANGO_SETTINGS_MODULE"}}
    env.update({"DJANGO_SETTINGS_MODULE": "config.settings", "PYTHONIOENCODING": "utf-8"}, **overrides)
    return subprocess.run(
        [sys.executable, "-c", PROBE],
        cwd=settings.BASE_DIR,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )


class NodeSettingsSubprocessTests(SimpleTestCase):
    def test_node_mode_uses_local_sqlite_even_when_database_url_is_set(self):
        with tempfile.TemporaryDirectory() as home:
            result = run_settings_probe(
                DEPLOYMENT_MODE="node",
                FEEDBACK_HUB_NODE_HOME=home,
                DATABASE_URL="postgres://must-not-be-used@example.invalid/db",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            probe = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertEqual(probe["mode"], "node")
            self.assertEqual(probe["engine"], "django.db.backends.sqlite3")
            self.assertEqual(Path(probe["name"]), Path(home) / "data" / "node.sqlite3")
            self.assertIn("node", probe["apps"])
            self.assertIn("organizations", probe["apps"])
            self.assertEqual(probe["hosts"], ["127.0.0.1", "localhost"])
            self.assertTrue(probe["secret_from_file"])

    def test_unknown_mode_refuses_to_start(self):
        result = run_settings_probe(DEPLOYMENT_MODE="hybrid")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DEPLOYMENT_MODE", result.stderr)


class CloudModeTests(SimpleTestCase):
    def test_current_process_mode_is_consistent(self):
        self.assertEqual(settings.IS_NODE, settings.DEPLOYMENT_MODE == "node")
        if not settings.IS_NODE:
            self.assertNotIn("node", settings.INSTALLED_APPS)
            self.assertNotIn("organizations", settings.INSTALLED_APPS)
```

- [ ] **Step 2: 執行確認失敗**

Run: `.\.venv\Scripts\python.exe manage.py test config.test_deployment_mode --settings=config.settings_test`
Expected: FAIL（`settings` 沒有 `IS_NODE`；node 探測失敗）

- [ ] **Step 3: 建立兩個 app 與模型**

```python
# organizations/apps.py
from django.apps import AppConfig


class OrganizationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "organizations"
    verbose_name = "組織"
```

```python
# organizations/models.py
from django.conf import settings
from django.db import models
from django.db.models import Q


class Organization(models.Model):
    name = models.CharField("組織名稱", max_length=120)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "組織"
        verbose_name_plural = "組織"

    def __str__(self):
        return self.name

    @classmethod
    def current(cls):
        """The single organization a node serves (None before first-run setup)."""

        return cls.objects.order_by("pk").first()


class OrganizationMembership(models.Model):
    class Role(models.TextChoices):
        OWNER = "owner", "擁有者"
        ADMIN = "admin", "組織管理員"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="organization_memberships")
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="memberships")
    role = models.CharField("角色", max_length=20, choices=Role.choices)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "組織成員"
        verbose_name_plural = "組織成員"
        constraints = [
            models.UniqueConstraint(fields=["user", "organization"], name="organization_membership_unique_user"),
            models.UniqueConstraint(
                fields=["organization"],
                condition=Q(role="owner"),
                name="organization_single_owner",
            ),
        ]

    def __str__(self):
        return f"{self.user} · {self.get_role_display()}"
```

```python
# organizations/access.py
from .models import OrganizationMembership


def organization_role(user):
    """The user's organization-level role ("owner"/"admin"), or None."""

    if not getattr(user, "is_authenticated", False):
        return None
    membership = OrganizationMembership.objects.filter(user=user).only("role").first()
    return membership.role if membership else None
```

```python
# organizations/admin.py
from django.contrib import admin

from .models import Organization, OrganizationMembership

admin.site.register(Organization)
admin.site.register(OrganizationMembership)
```

```python
# node/apps.py
from django.apps import AppConfig


class NodeConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "node"
    verbose_name = "本機節點"
```

```python
# node/models.py
from django.conf import settings
from django.db import models


class AuditLogImmutable(Exception):
    """Audit events are append-only."""


class NodeInstallation(models.Model):
    """Singleton describing this installation; the row always has pk=1."""

    setup_completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "節點安裝狀態"
        verbose_name_plural = "節點安裝狀態"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        installation, _created = cls.objects.get_or_create(pk=1)
        return installation

    @classmethod
    def setup_complete(cls):
        """Read-only check used on every request; never creates the row."""

        return cls.objects.filter(pk=1, setup_completed_at__isnull=False).exists()

    @property
    def is_setup_complete(self):
        return self.setup_completed_at is not None


class AuditEventQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise AuditLogImmutable("稽核紀錄不可修改")

    def delete(self):
        raise AuditLogImmutable("稽核紀錄不可刪除")


class NodeAuditEvent(models.Model):
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    # Kept so the event still names the person after the account is removed.
    actor_email = models.CharField(max_length=254, blank=True)
    action = models.CharField(max_length=64)
    target = models.CharField(max_length=255, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    objects = AuditEventQuerySet.as_manager()

    class Meta:
        verbose_name = "稽核紀錄"
        verbose_name_plural = "稽核紀錄"
        ordering = ["-created_at", "-pk"]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise AuditLogImmutable("稽核紀錄不可修改")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise AuditLogImmutable("稽核紀錄不可刪除")
```

```python
# node/audit.py
"""Append-only audit trail for security-relevant node actions."""

from .models import NodeAuditEvent

SETUP_COMPLETED = "setup.completed"
LOGIN_SUCCEEDED = "login.succeeded"
LOGIN_FAILED = "login.failed"
ORGANIZATION_RENAMED = "organization.renamed"

ACTION_LABELS = {
    SETUP_COMPLETED: "完成首次設定",
    LOGIN_SUCCEEDED: "登入成功",
    LOGIN_FAILED: "登入失敗",
    ORGANIZATION_RENAMED: "變更組織名稱",
}


def client_ip(request):
    # The node serves browsers directly (no reverse proxy), so REMOTE_ADDR is the client.
    return request.META.get("REMOTE_ADDR") or None


def record(action, *, request=None, actor=None, target="", **details):
    if actor is None and request is not None:
        user = getattr(request, "user", None)
        actor = user if getattr(user, "is_authenticated", False) else None
    return NodeAuditEvent.objects.create(
        actor=actor,
        actor_email=(getattr(actor, "email", "") or "")[:254],
        action=action,
        target=str(target)[:255],
        ip=client_ip(request) if request is not None else None,
        details=details,
    )
```

```python
# node/admin.py
from django.contrib import admin

from .models import NodeAuditEvent, NodeInstallation


@admin.register(NodeAuditEvent)
class NodeAuditEventAdmin(admin.ModelAdmin):
    list_display = ("created_at", "action", "actor_email", "target", "ip")
    list_filter = ("action",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


admin.site.register(NodeInstallation)
```

- [ ] **Step 4: settings 模式開關**

在 `config/settings.py` 的 `BASE_DIR = ...` 之後加入：

```python
DEPLOYMENT_MODE = os.getenv("DEPLOYMENT_MODE", "cloud").strip().lower() or "cloud"
if DEPLOYMENT_MODE not in {"cloud", "node"}:
    raise ImproperlyConfigured("DEPLOYMENT_MODE must be 'cloud' or 'node'.")
IS_NODE = DEPLOYMENT_MODE == "node"
```

`DEBUG` 那一行改為 node 預設關閉：

```python
DEBUG = os.getenv("DEBUG", "False" if (_IS_RENDER_RUNTIME or IS_NODE) else "True").lower() == "true"
```

`SECRET_KEY` 區塊改為：

```python
if IS_NODE:
    from config.node_paths import NodePaths, load_or_create_secret_key

    NODE_PATHS = NodePaths.from_environment()
    NODE_PATHS.ensure()
    SECRET_KEY = load_or_create_secret_key(NODE_PATHS.secret_key_file)
else:
    SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "").strip()
    if not SECRET_KEY:
        if _IS_RENDER_RUNTIME or not DEBUG:
            raise ImproperlyConfigured("DJANGO_SECRET_KEY must be set when DEBUG is off or on Render.")
        SECRET_KEY = "dev-secret-key-change-me"
```

檔尾（`LOGGING` 之後）加入：

```python
if IS_NODE:
    # Local node: browsers on this machine only, plain HTTP on loopback.
    ALLOWED_HOSTS = ["127.0.0.1", "localhost"]
    CSRF_TRUSTED_ORIGINS = []
    SECURE_SSL_REDIRECT = False
    SESSION_COOKIE_SECURE = False
    CSRF_COOKIE_SECURE = False
    # Only NODE_DATABASE_URL: a developer .env points DATABASE_URL at Supabase.
    _node_database_url = os.getenv("NODE_DATABASE_URL", "").strip()
    DATABASES = {
        "default": dj_database_url.parse(_node_database_url, conn_max_age=600)
        if _node_database_url
        else {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": NODE_PATHS.database_file,
            "OPTIONS": {
                "init_command": "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;",
                "transaction_mode": "IMMEDIATE",
                "timeout": 20,
            },
        }
    }
    INSTALLED_APPS += ["organizations", "node"]
    NODE_SETUP_GATE = True
```

`config/settings_test.py` 檔尾加入：

```python
# Existing suites exercise pages directly; node setup tests switch the gate back on.
NODE_SETUP_GATE = False
```

- [ ] **Step 5: 產生 migration**

Run（node 模式，見 Global Constraints 的環境變數）：
`$env:DEPLOYMENT_MODE='node'; $env:FEEDBACK_HUB_NODE_HOME="$env:TEMP\fih-node-test"; .\.venv\Scripts\python.exe manage.py makemigrations organizations node --settings=config.settings_test`
Expected: 產生 `organizations/migrations/0001_initial.py` 與 `node/migrations/0001_initial.py`（先建立空的 `migrations/__init__.py`）

- [ ] **Step 6: 寫 node 模式模型測試**

```python
# node/tests/test_models.py
from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase

from node.audit import LOGIN_FAILED, SETUP_COMPLETED, record
from node.models import AuditLogImmutable, NodeAuditEvent, NodeInstallation


class NodeInstallationTests(TestCase):
    def test_singleton_row_and_read_only_check(self):
        self.assertFalse(NodeInstallation.setup_complete())
        self.assertFalse(NodeInstallation.objects.exists())
        installation = NodeInstallation.load()
        self.assertEqual(installation.pk, 1)
        other = NodeInstallation()
        other.save()
        self.assertEqual(NodeInstallation.objects.count(), 1)


class AuditEventTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="o@example.com", email="o@example.com", password="x")

    def test_record_captures_actor_ip_and_details(self):
        request = RequestFactory().get("/", REMOTE_ADDR="127.0.0.1")
        request.user = self.user
        event = record(SETUP_COMPLETED, request=request, target="Acme", step="done")
        self.assertEqual(event.actor, self.user)
        self.assertEqual(event.actor_email, "o@example.com")
        self.assertEqual(event.ip, "127.0.0.1")
        self.assertEqual(event.details, {"step": "done"})

    def test_events_cannot_be_changed_or_deleted(self):
        event = record(LOGIN_FAILED, target="x@example.com")
        event.target = "tampered"
        with self.assertRaises(AuditLogImmutable):
            event.save()
        with self.assertRaises(AuditLogImmutable):
            event.delete()
        with self.assertRaises(AuditLogImmutable):
            NodeAuditEvent.objects.all().update(target="tampered")
        with self.assertRaises(AuditLogImmutable):
            NodeAuditEvent.objects.all().delete()

    def test_deleting_the_user_keeps_the_event(self):
        event = record(SETUP_COMPLETED, actor=self.user)
        self.user.delete()
        event.refresh_from_db()
        self.assertIsNone(event.actor)
        self.assertEqual(event.actor_email, "o@example.com")
```

```python
# organizations/tests.py
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase

from organizations.access import organization_role
from organizations.models import Organization, OrganizationMembership

Role = OrganizationMembership.Role


class OrganizationMembershipTests(TestCase):
    def setUp(self):
        users = get_user_model().objects
        self.owner = users.create_user(username="owner", password="x")
        self.second = users.create_user(username="second", password="x")
        self.organization = Organization.objects.create(name="Acme")

    def test_only_one_owner_per_organization(self):
        OrganizationMembership.objects.create(user=self.owner, organization=self.organization, role=Role.OWNER)
        with self.assertRaises(IntegrityError), transaction.atomic():
            OrganizationMembership.objects.create(user=self.second, organization=self.organization, role=Role.OWNER)
        OrganizationMembership.objects.create(user=self.second, organization=self.organization, role=Role.ADMIN)

    def test_role_lookup(self):
        OrganizationMembership.objects.create(user=self.owner, organization=self.organization, role=Role.OWNER)
        self.assertEqual(organization_role(self.owner), "owner")
        self.assertIsNone(organization_role(self.second))
        self.assertEqual(Organization.current(), self.organization)
```

`node/tests/__init__.py` 為空檔。

- [ ] **Step 7: 執行兩種模式**

Run（cloud）：`.\.venv\Scripts\python.exe manage.py test config.test_deployment_mode --settings=config.settings_test`
Expected: PASS
Run（node）：`$env:DEPLOYMENT_MODE='node'; $env:FEEDBACK_HUB_NODE_HOME="$env:TEMP\fih-node-test"; .\.venv\Scripts\python.exe manage.py test node organizations config.test_deployment_mode --settings=config.settings_test`
Expected: PASS
Run（兩種模式各一次）：`manage.py makemigrations --check --dry-run --settings=config.settings_test`
Expected: `No changes detected`

- [ ] **Step 8: 規格修訂**

在 `docs/superpowers/specs/2026-09-30-local-node-console-and-auth-design.md` 第 1 節把「可由 `DATABASE_URL` 改為 PostgreSQL」改為「可由 `NODE_DATABASE_URL` 改為 PostgreSQL（不讀 `DATABASE_URL`，避免開發機 `.env` 的 Supabase 連線被誤用）」；第 2 節資料目錄表加一列 `data\artifacts\`｜Worker 版本化分析產物｜目前帳號。

- [ ] **Step 9: Commit（需使用者授權）**

```bash
git add config/settings.py config/settings_test.py config/test_deployment_mode.py organizations node docs/superpowers/specs/2026-09-30-local-node-console-and-auth-design.md
git commit -m "feat(node): DEPLOYMENT_MODE switch with organization and audit models"
```

---

### Task 3: CI 以 node 模式跑全部測試

**Files:**
- Modify: `.github/workflows/ci.yml`
- Modify: `feedback/test_utils.py`（新增 `cloud_only`）
- Modify: 在 node 模式下失敗、且屬雲端專用功能的既有測試（依下方分類規則）

**Interfaces:**
- Produces: `feedback.test_utils.cloud_only`（`unittest.skipUnless` 裝飾器，後續 Task 6、7 沿用）

- [ ] **Step 1: 新增裝飾器**

在 `feedback/test_utils.py` 的 import 區後加入：

```python
from unittest import skipUnless

# Features that exist only on the public cloud site (landing page, customer
# sign-up, the shared login entry).  The local node CI job skips them.
cloud_only = skipUnless(settings.DEPLOYMENT_MODE == "cloud", "雲端模式專用功能")
```

- [ ] **Step 2: node 模式跑既有套件**

Run：`$env:DEPLOYMENT_MODE='node'; $env:FEEDBACK_HUB_NODE_HOME="$env:TEMP\fih-node-test"; .\.venv\Scripts\python.exe manage.py test feedback accounts config node organizations --settings=config.settings_test`
Expected: 此時 node 模式只改了資料庫、機密與 app 清單，預期全部通過。若有失敗，逐一套用分類規則：
- 失敗原因是雲端專用功能（`/` 公開首頁、`accounts:signup`、`accounts:login` 共用入口頁面內容、Email 驗證信）→ 在該測試類別或方法加 `@cloud_only`。
- 其他任何失敗都是 node 模式的回歸 → 修正程式，不得加 `@cloud_only`。

- [ ] **Step 3: CI 新 job**

在 `.github/workflows/ci.yml` 的 `test` job 之後加入：

```yaml
  node-mode:
    name: Django tests (local node mode)
    runs-on: ubuntu-latest
    env:
      DEPLOYMENT_MODE: node
      FEEDBACK_HUB_NODE_HOME: ${{ github.workspace }}/.ci-node-home
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.13"
          cache: pip
      - run: python -m pip install -r requirements.txt
      - name: Check for missing migrations
        run: python manage.py makemigrations --check --dry-run --settings=config.settings_test
      - name: Django system check
        run: python manage.py check --settings=config.settings_test
      - name: Tests (node mode, isolated in-memory SQLite)
        run: python manage.py test feedback accounts config node organizations --settings=config.settings_test
```

- [ ] **Step 4: 本機 cloud 模式回歸**

Run：`.\.venv\Scripts\python.exe manage.py test feedback accounts config --settings=config.settings_test`
Expected: PASS（與之前相同數量，另加 Task 1、2 的新測試）

- [ ] **Step 5: Commit（需使用者授權）**

```bash
git add .github/workflows/ci.yml feedback/test_utils.py
git commit -m "ci: run the full suite in local node mode as well"
```

---

### Task 4: Worker 心跳檔

**Files:**
- Create: `feedback/worker_heartbeat.py`
- Modify: `feedback/management/commands/run_analysis_worker.py`（新增 `--heartbeat-file`）
- Test: `feedback/test_worker_heartbeat.py`、`feedback/test_worker_command.py`

**Interfaces:**
- Produces:
  - `write_heartbeat(path, state: str, *, clock=time.time, pid=None) -> None`（`state` 為 `"idle"` 或 `"busy"`；寫入失敗只記 debug log）
  - `read_heartbeat(path) -> dict | None`（`{"at": float, "state": str, "pid": int}`；檔案不存在或內容損壞回傳 `None`）
  - `run_analysis_worker --heartbeat-file PATH`

- [ ] **Step 1: 寫失敗測試**

```python
# feedback/test_worker_heartbeat.py
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from feedback.worker_heartbeat import read_heartbeat, write_heartbeat


class WorkerHeartbeatTests(SimpleTestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run" / "worker.heartbeat"
            write_heartbeat(path, "busy", clock=lambda: 1000.5, pid=42)
            self.assertEqual(read_heartbeat(path), {"at": 1000.5, "state": "busy", "pid": 42})

    def test_missing_or_corrupt_file_reads_as_none(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "worker.heartbeat"
            self.assertIsNone(read_heartbeat(path))
            path.write_text("{not json", encoding="utf-8")
            self.assertIsNone(read_heartbeat(path))
            path.write_text('{"state": "idle"}', encoding="utf-8")
            self.assertIsNone(read_heartbeat(path))
```

在 `feedback/test_worker_command.py` 的 `ContinuousWorkerCommandTests` 加入（檔頭補 `from pathlib import Path` 與 `from .worker_heartbeat import read_heartbeat`）：

```python
    def test_heartbeat_file_reports_idle_after_an_empty_poll(self):
        with tempfile.TemporaryDirectory() as directory:
            heartbeat = Path(directory) / "worker.heartbeat"
            call_command(
                "run_analysis_worker",
                worker_id="loop-worker",
                output=directory,
                once=True,
                heartbeat_file=str(heartbeat),
                stdout=io.StringIO(),
            )
            self.assertEqual(read_heartbeat(heartbeat)["state"], "idle")

    def test_heartbeat_is_busy_while_a_job_runs(self):
        schedule_survey_analysis(self.survey.pk, change="input")
        seen = []
        with tempfile.TemporaryDirectory() as directory:
            heartbeat = Path(directory) / "worker.heartbeat"
            from feedback.management.commands import run_analysis_worker as module

            original = module.Command._run_deterministic

            def spy(command, job, **kwargs):
                seen.append(read_heartbeat(heartbeat)["state"])
                return original(command, job, **kwargs)

            with patch.object(module.Command, "_run_deterministic", spy):
                call_command(
                    "run_analysis_worker",
                    worker_id="loop-worker",
                    output=directory,
                    once=True,
                    heartbeat_file=str(heartbeat),
                    stdout=io.StringIO(),
                )
        self.assertEqual(seen, ["busy"])
```

- [ ] **Step 2: 執行確認失敗**

Run：`.\.venv\Scripts\python.exe manage.py test feedback.test_worker_heartbeat feedback.test_worker_command --settings=config.settings_test`
Expected: FAIL（模組不存在；`--heartbeat-file` 為未知參數）

- [ ] **Step 3: 實作**

```python
# feedback/worker_heartbeat.py
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
```

在 `run_analysis_worker.py`：
- import 區加 `from feedback.worker_heartbeat import write_heartbeat`
- `add_arguments` 加：

```python
        parser.add_argument("--heartbeat-file", help="本機節點監督用的心跳檔路徑")
```

- `handle` 的 `while True:` 迴圈改為：

```python
        heartbeat_file = options.get("heartbeat_file")

        def beat(state):
            if heartbeat_file:
                write_heartbeat(heartbeat_file, state)

        self._event(status="started", paid_ai=bool(options["allow_paid_ai"]))
        while True:
            beat("idle")
            job = claim_next_job(
                options["worker_id"],
                lease_seconds=lease_seconds,
                executor=AnalysisJob.Executor.DETERMINISTIC,
                external_source_refs=external_inputs.keys(),
            )
            if job:
                beat("busy")
                self._event(
                    **self._run_deterministic(
                        job,
                        output=options["output"],
                        external_inputs=external_inputs,
                        lease_seconds=lease_seconds,
                    )
                )
            elif options["allow_paid_ai"]:
                job = claim_next_job(
                    options["worker_id"],
                    lease_seconds=lease_seconds,
                    executor=AnalysisJob.Executor.AI,
                )
                if job:
                    beat("busy")
                    self._event(**self._run_ai(job, lease_seconds=lease_seconds))
            if options["once"]:
                if not job:
                    self._event(status="idle")
                beat("idle")
                return
            if not job:
                time.sleep(poll_seconds)
```

（`test_heartbeat_is_busy_while_a_job_runs` 在工作執行中讀到 `busy`；結束時檔案回到 `idle`。）

- [ ] **Step 4: 執行確認通過**

Run：`.\.venv\Scripts\python.exe manage.py test feedback.test_worker_heartbeat feedback.test_worker_command --settings=config.settings_test`
Expected: PASS

- [ ] **Step 5: Commit（需使用者授權）**

```bash
git add feedback/worker_heartbeat.py feedback/management/commands/run_analysis_worker.py feedback/test_worker_heartbeat.py feedback/test_worker_command.py
git commit -m "feat(worker): optional heartbeat file for a supervising launcher"
```

---

### Task 5: 主控台外框、總覽與設定頁

**Files:**
- Create: `node/status.py`、`node/views.py`、`node/forms.py`、`node/urls.py`
- Create: `templates/node/overview.html`、`templates/node/settings.html`
- Create: `config/context_processors.py`
- Modify: `config/settings.py`（`TEMPLATES` 加 context processor）
- Modify: `config/urls.py`（node 模式掛載 `/node/` 與根路徑導向）
- Modify: `feedback/views.py:186-209`（`DashboardBaseMixin` 支援 node 導覽）
- Modify: `templates/feedback/_nav_icon.html`（新增 `server`、`gear` 圖示）
- Modify: `templates/feedback/dashboard_base.html`、`templates/base.html`、`templates/public_base.html`（node 模式隱藏「返回官網」「建立帳號」）
- Modify: `static/css/ui.css`（狀態卡樣式）
- Test: `node/tests/test_status.py`、`node/tests/test_console.py`、`config/test_deployment_mode.py`

**Interfaces:**
- Consumes: Task 1 `NodePaths`；Task 2 `organization_role`、`Organization`、`NodeAuditEvent`、`record`、`ACTION_LABELS`、`ORGANIZATION_RENAMED`；Task 4 `read_heartbeat`
- Produces:
  - `node.status.StatusItem(key, label, state, summary)`（`state` ∈ `"ok"`、`"warn"`、`"off"`）
  - `database_status() -> StatusItem`、`worker_status(paths, *, now=None, stale_after=60) -> StatusItem`、`disk_status(paths, *, usage=shutil.disk_usage, low_bytes=LOW_DISK_BYTES) -> StatusItem`、`lan_status()`、`cloud_status()`、`pending_items(items) -> list[str]`
  - URL 名稱：`node:overview`（`/node/`）、`node:settings`（`/node/settings/`）
  - `node.views.NodeConsoleMixin`（登入＋管理者＋組織角色 owner/admin）
  - context 變數 `is_node`

- [ ] **Step 1: 寫狀態函式的失敗測試**

```python
# node/tests/test_status.py
import tempfile
from collections import namedtuple
from pathlib import Path

from django.test import SimpleTestCase, TestCase

from config.node_paths import NodePaths
from feedback.worker_heartbeat import write_heartbeat
from node.status import cloud_status, database_status, disk_status, lan_status, pending_items, worker_status

Usage = namedtuple("Usage", "total used free")


class WorkerStatusTests(SimpleTestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.paths = NodePaths(Path(self._directory.name))
        self.paths.run_dir.mkdir(parents=True)

    def tearDown(self):
        self._directory.cleanup()

    def test_never_started(self):
        self.assertEqual(worker_status(self.paths, now=100).state, "off")

    def test_recent_idle_heartbeat_is_running(self):
        write_heartbeat(self.paths.heartbeat_file, "idle", clock=lambda: 100)
        item = worker_status(self.paths, now=130)
        self.assertEqual(item.state, "ok")
        self.assertIn("運作中", item.summary)

    def test_old_idle_heartbeat_is_unresponsive(self):
        write_heartbeat(self.paths.heartbeat_file, "idle", clock=lambda: 100)
        item = worker_status(self.paths, now=400)
        self.assertEqual(item.state, "warn")
        self.assertIn("無回應", item.summary)

    def test_busy_worker_is_not_reported_dead_during_a_long_job(self):
        write_heartbeat(self.paths.heartbeat_file, "busy", clock=lambda: 100)
        item = worker_status(self.paths, now=100 + 3600)
        self.assertEqual(item.state, "ok")
        self.assertIn("處理工作中", item.summary)

    def test_supervisor_gave_up(self):
        write_heartbeat(self.paths.heartbeat_file, "idle", clock=lambda: 100)
        self.paths.worker_state_file.write_text("stopped", encoding="utf-8")
        item = worker_status(self.paths, now=110)
        self.assertEqual(item.state, "warn")
        self.assertIn("已停止", item.summary)


class DiskAndFixedStatusTests(SimpleTestCase):
    def test_low_disk_warns(self):
        paths = NodePaths(Path("."))
        low = disk_status(paths, usage=lambda _path: Usage(100 * 1024**3, 99 * 1024**3, 1 * 1024**3))
        self.assertEqual(low.state, "warn")
        fine = disk_status(paths, usage=lambda _path: Usage(100 * 1024**3, 50 * 1024**3, 50 * 1024**3))
        self.assertEqual(fine.state, "ok")

    def test_lan_and_cloud_are_off_in_this_release(self):
        self.assertEqual(lan_status().summary, "未開放（僅限本機）")
        self.assertEqual(cloud_status().summary, "未連線")

    def test_pending_items_come_from_warnings(self):
        paths = NodePaths(Path("."))
        items = [disk_status(paths, usage=lambda _path: Usage(10, 10, 0)), lan_status()]
        self.assertEqual(len(pending_items(items)), 1)


class DatabaseStatusTests(TestCase):
    def test_reachable_database(self):
        self.assertEqual(database_status().state, "ok")
```

- [ ] **Step 2: 執行確認失敗**

Run（node）：`... manage.py test node.tests.test_status --settings=config.settings_test`
Expected: FAIL（`node.status` 不存在）

- [ ] **Step 3: 實作 `node/status.py`**

```python
# node/status.py
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
```

- [ ] **Step 4: 執行狀態測試通過**

Run（node）：`... manage.py test node.tests.test_status --settings=config.settings_test`
Expected: PASS

- [ ] **Step 5: 寫主控台頁面的失敗測試**

```python
# node/tests/test_console.py
import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from config.node_paths import NodePaths
from node.audit import ORGANIZATION_RENAMED, SETUP_COMPLETED, record
from node.models import NodeAuditEvent
from organizations.models import Organization, OrganizationMembership

User = get_user_model()
Role = OrganizationMembership.Role


class ConsoleTestCase(TestCase):
    def setUp(self):
        self._home = tempfile.TemporaryDirectory()
        self.addCleanup(self._home.cleanup)
        self.paths = NodePaths(Path(self._home.name))
        self.paths.ensure(restrict=lambda _path: None)
        override = override_settings(NODE_PATHS=self.paths)
        override.enable()
        self.addCleanup(override.disable)
        self.organization = Organization.objects.create(name="Acme")
        self.owner = self._member("owner@example.com", Role.OWNER)

    def _member(self, email, role):
        user = User.objects.create_user(username=email, email=email, password="pw-Complex-123", role=User.Role.MANAGER)
        OrganizationMembership.objects.create(user=user, organization=self.organization, role=role)
        return user


class OverviewTests(ConsoleTestCase):
    def test_anonymous_is_sent_to_login(self):
        response = self.client.get(reverse("node:overview"))
        self.assertEqual(response.status_code, 302)

    def test_manager_outside_the_organization_is_forbidden(self):
        outsider = User.objects.create_user(username="m@example.com", password="x", role=User.Role.MANAGER)
        self.client.force_login(outsider)
        self.assertEqual(self.client.get(reverse("node:overview")).status_code, 403)

    def test_owner_sees_every_status_and_recent_audit(self):
        record(SETUP_COMPLETED, actor=self.owner, target="Acme")
        self.client.force_login(self.owner)
        response = self.client.get(reverse("node:overview"))
        self.assertEqual(response.status_code, 200)
        for label in ("資料庫", "分析 Worker", "磁碟空間", "區域網路", "雲端連線", "未開放（僅限本機）", "未連線", "完成首次設定"):
            self.assertContains(response, label)

    def test_stopped_worker_is_listed_as_pending(self):
        self.paths.worker_state_file.write_text("stopped", encoding="utf-8")
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse("node:overview")), "分析 Worker 需要處理")

    def test_admin_can_open_overview_and_settings(self):
        admin = self._member("admin@example.com", Role.ADMIN)
        self.client.force_login(admin)
        self.assertEqual(self.client.get(reverse("node:overview")).status_code, 200)
        self.assertEqual(self.client.get(reverse("node:settings")).status_code, 200)

    def test_console_nav_appears_on_existing_manager_pages(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("feedback:survey-manager"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("node:overview"))
        self.assertNotContains(response, "返回官網")

    def test_root_redirects_to_console(self):
        response = self.client.get("/")
        self.assertRedirects(response, reverse("node:overview"), fetch_redirect_response=False)


class SettingsTests(ConsoleTestCase):
    def test_rename_organization_is_audited(self):
        self.client.force_login(self.owner)
        response = self.client.post(reverse("node:settings"), {"name": "Acme Taiwan"})
        self.assertRedirects(response, reverse("node:settings"), fetch_redirect_response=False)
        self.organization.refresh_from_db()
        self.assertEqual(self.organization.name, "Acme Taiwan")
        event = NodeAuditEvent.objects.get(action=ORGANIZATION_RENAMED)
        self.assertEqual(event.details, {"from": "Acme", "to": "Acme Taiwan"})

    def test_blank_name_is_rejected(self):
        self.client.force_login(self.owner)
        response = self.client.post(reverse("node:settings"), {"name": "  "})
        self.assertEqual(response.status_code, 200)
        self.organization.refresh_from_db()
        self.assertEqual(self.organization.name, "Acme")
```

在 `config/test_deployment_mode.py` 加入（檔頭補 `from feedback.test_utils import cloud_only`、`from django.test import TestCase`）：

```python
@cloud_only
class CloudHidesNodeConsoleTests(TestCase):
    def test_node_urls_do_not_exist_on_the_cloud_site(self):
        self.assertEqual(self.client.get("/node/").status_code, 404)
        self.assertEqual(self.client.get("/setup/").status_code, 404)
```

- [ ] **Step 6: 執行確認失敗**

Run（node）：`... manage.py test node.tests.test_console --settings=config.settings_test`
Expected: FAIL（`NoReverseMatch: 'node' is not a registered namespace`）

- [ ] **Step 7: 實作頁面**

```python
# config/context_processors.py
from django.conf import settings


def deployment(request):
    return {"is_node": settings.IS_NODE}
```

`config/settings.py` 的 `context_processors` 清單加上 `"config.context_processors.deployment",`。

```python
# node/forms.py
from django import forms


class OrganizationSettingsForm(forms.Form):
    name = forms.CharField(label="組織名稱", max_length=120)
```

（Django 的 `CharField` 預設 `strip=True`，全空白會成為空字串並觸發必填錯誤。）

```python
# node/views.py
from django.conf import settings
from django.contrib import messages
from django.shortcuts import redirect
from django.views.generic import FormView, TemplateView

from feedback.views import DashboardBaseMixin
from organizations.access import organization_role
from organizations.models import Organization, OrganizationMembership

from .audit import ACTION_LABELS, ORGANIZATION_RENAMED, record
from .forms import OrganizationSettingsForm
from .models import NodeAuditEvent
from .status import cloud_status, database_status, disk_status, lan_status, pending_items, worker_status

Role = OrganizationMembership.Role


class NodeConsoleMixin(DashboardBaseMixin):
    """Console pages: manager shell plus an organization-level role."""

    allowed_roles = (Role.OWNER, Role.ADMIN)

    def test_func(self):
        return super().test_func() and organization_role(self.request.user) in self.allowed_roles

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(self.get_dashboard_base_context())
        return context


class OverviewView(NodeConsoleMixin, TemplateView):
    template_name = "node/overview.html"
    active_section = "node:overview"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        paths = settings.NODE_PATHS
        items = [database_status(), worker_status(paths), disk_status(paths), lan_status(), cloud_status()]
        events = list(NodeAuditEvent.objects.all()[:10])
        for event in events:
            event.label = ACTION_LABELS.get(event.action, event.action)
        context.update(
            {
                "organization": Organization.current(),
                "status_items": items,
                "pending": pending_items(items),
                "recent_events": events,
                "data_root": paths.root,
            }
        )
        return context


class SettingsView(NodeConsoleMixin, FormView):
    template_name = "node/settings.html"
    active_section = "node:settings"
    form_class = OrganizationSettingsForm

    def get_initial(self):
        organization = Organization.current()
        return {"name": organization.name if organization else ""}

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["data_root"] = settings.NODE_PATHS.root
        return context

    def form_valid(self, form):
        organization = Organization.current()
        previous, new = organization.name, form.cleaned_data["name"]
        if previous != new:
            organization.name = new
            organization.save(update_fields=["name"])
            record(ORGANIZATION_RENAMED, request=self.request, target=new, **{"from": previous, "to": new})
            messages.success(self.request, "組織名稱已更新。")
        return redirect("node:settings")
```

（既有管理頁都在各自的 `get_context_data` 呼叫 `context.update(self.get_dashboard_base_context())`；`NodeConsoleMixin` 用同一方式，只是集中在 mixin。）

```python
# node/urls.py
from django.urls import path

from .views import OverviewView, SettingsView

app_name = "node"

urlpatterns = [
    path("", OverviewView.as_view(), name="overview"),
    path("settings/", SettingsView.as_view(), name="settings"),
]
```

`config/urls.py` 改為：

```python
from django.conf import settings
from django.contrib import admin
from django.urls import include, path
from django.views.generic import RedirectView

from .health import database_health, liveness

urlpatterns = [
    path("healthz/", liveness, name="healthz"),
    path("healthz/db/", database_health, name="healthz-db"),
    path("admin/", admin.site.urls),
    path("accounts/", include("accounts.urls")),
]

if settings.IS_NODE:
    urlpatterns += [
        # The node has no public landing page; "/" opens the console.
        path("", RedirectView.as_view(pattern_name="node:overview", permanent=False)),
        path("node/", include("node.urls")),
    ]

urlpatterns += [path("", include("feedback.urls"))]
```

`feedback/views.py` 的 `DashboardBaseMixin`：

```python
NODE_CONSOLE_NAV = [("node:overview", "節點總覽", "server")]
NODE_CONSOLE_NAV_TAIL = [("node:settings", "設定", "gear")]


class DashboardBaseMixin(ManagerRequiredMixin):
    dashboard_nav = [
        # （原清單不變）
    ]

    active_section = ""

    def get_dashboard_nav(self):
        if settings.IS_NODE:
            return NODE_CONSOLE_NAV + self.dashboard_nav + NODE_CONSOLE_NAV_TAIL
        return self.dashboard_nav

    def get_dashboard_base_context(self):
        nav = self.get_dashboard_nav()
        return {
            "dashboard_nav": nav,
            "active_section": self.active_section,
            "section_label": next(
                (label for route, label, _icon in nav if route == self.active_section),
                "管理工作區",
            ),
            "survey_list": analysis_visible_surveys().order_by("title"),
        }
```

（`feedback/views.py` 目前沒有 import settings，在檔頭 import 區加上 `from django.conf import settings`。）

`templates/feedback/_nav_icon.html` 在 `{% else %}` 前加入兩個分支：

```django
{% elif name == "server" %}<rect x="4" y="4" width="16" height="7" rx="1.5"/><rect x="4" y="13" width="16" height="7" rx="1.5"/><path d="M8 7.5h.01M8 16.5h.01"/>{% elif name == "gear" %}<circle cx="12" cy="12" r="3"/><path d="M12 3.5v2.5M12 18v2.5M3.5 12H6M18 12h2.5M6 6l1.8 1.8M16.2 16.2 18 18M6 18l1.8-1.8M16.2 7.8 18 6"/>
```

`templates/feedback/dashboard_base.html` 的「返回官網」連結包進 `{% if not is_node %}…{% endif %}`，側欄 `<small>Manager Workspace</small>` 改為 `<small>{% if is_node %}Local Node{% else %}Manager Workspace{% endif %}</small>`。
`templates/base.html` 與 `templates/public_base.html` 的「建立帳號」連結包進 `{% if not is_node %}…{% endif %}`。

```django
{# templates/node/overview.html #}
{% extends "feedback/dashboard_base.html" %}

{% block title %}節點總覽 | FeedBack IQ{% endblock %}

{% block dashboard_content %}
<section class="manager-header manager-header-inline">
    <div>
        <p class="section-kicker">Local Node</p>
        <p>{{ organization.name|default:"尚未設定組織" }} · 資料位置 <code>{{ data_root }}</code></p>
    </div>
</section>

{% if pending %}
<section class="manager-panel node-pending" aria-label="待處理事項">
    <h2>待處理事項</h2>
    <ul>{% for message in pending %}<li>{{ message }}</li>{% endfor %}</ul>
</section>
{% endif %}

<section class="node-status-grid" aria-label="節點狀態">
    {% for item in status_items %}
    <article class="summary-card node-status-card node-status-{{ item.state }}">
        <span class="info-label">{{ item.label }}</span>
        <strong>{{ item.summary }}</strong>
    </article>
    {% endfor %}
</section>

<section class="manager-panel">
    <h2>最近活動</h2>
    <div class="record-list">
        {% for event in recent_events %}
        <div class="record-row">
            <strong>{{ event.label }}</strong>
            <span>{{ event.actor_email|default:event.target }}</span>
            <span>{{ event.created_at|date:"m/d H:i" }}</span>
        </div>
        {% empty %}
        <p>目前沒有紀錄。</p>
        {% endfor %}
    </div>
</section>
{% endblock %}
```

```django
{# templates/node/settings.html #}
{% extends "feedback/dashboard_base.html" %}

{% block title %}設定 | FeedBack IQ{% endblock %}

{% block dashboard_content %}
<section class="manager-panel">
    <h2>組織</h2>
    <form method="post" class="form-stack">
        {% csrf_token %}
        {{ form.name.errors }}
        <label for="{{ form.name.id_for_label }}">{{ form.name.label }}</label>
        {{ form.name }}
        <button type="submit" class="button button-primary">儲存</button>
    </form>
</section>
<section class="manager-panel">
    <h2>資料位置</h2>
    <p><code>{{ data_root }}</code></p>
</section>
{% endblock %}
```

`static/css/ui.css` 檔尾加入：

```css
/* Local node console status cards */
.node-status-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 12px;
  margin-bottom: 20px;
}
.node-status-card { border-left: 4px solid var(--c-border, #d9dee7); }
.node-status-ok { border-left-color: var(--c-success, #1f9d55); }
.node-status-warn { border-left-color: var(--c-warning, #d97706); }
.node-status-off { border-left-color: var(--c-muted, #9aa3b2); }
.node-pending { border-left: 4px solid var(--c-warning, #d97706); }
```

（先用 `rg -n "^\s*--c-(success|warning|muted|border)" static/css/app.css` 確認 token 名稱，改用實際存在的名稱。）
樣板與 CSS 變更後，把引用 `ui.css` 的 `?v=` 版本字串一律改為 `20260930-node1`（`rg -l "ui.css" templates`）。

- [ ] **Step 8: 執行兩種模式**

Run（node）：`... manage.py test node organizations --settings=config.settings_test`
Expected: PASS
Run（cloud）：`.\.venv\Scripts\python.exe manage.py test feedback accounts config --settings=config.settings_test`
Expected: PASS（含 `CloudHidesNodeConsoleTests`）
Run（node 全套件）：`... manage.py test feedback accounts config node organizations --settings=config.settings_test`
Expected: 依 Task 3 分類規則處理；可預期的新失敗只有請求 `/` 公開首頁的測試（改為 `@cloud_only`）。

- [ ] **Step 9: Commit（需使用者授權）**

```bash
git add node config feedback/views.py templates static/css/ui.css
git commit -m "feat(node): console overview and settings inside the manager shell"
```

---

### Task 6: 首次設定

**Files:**
- Create: `node/middleware.py`、`node/setup.py`、`node/setup_views.py`
- Create: `templates/node/auth_base.html`、`templates/node/setup.html`、`templates/node/setup_blocked.html`
- Modify: `node/forms.py`（新增 `NodeSetupForm`）
- Modify: `config/settings.py`（node 區塊 `MIDDLEWARE.append("node.middleware.SetupRequiredMiddleware")`）
- Modify: `config/urls.py`（node 模式掛載 `setup/`）
- Test: `node/tests/test_setup.py`

**Interfaces:**
- Consumes: Task 1 `issue_setup_token`、`read_setup_token`、`clear_setup_token`；Task 2 模型、`record`、`SETUP_COMPLETED`；Task 5 `node:overview`
- Produces:
  - `node.setup.complete_setup(*, organization_name, email, password, request=None) -> User`、`SetupAlreadyCompleted`
  - `node.setup.token_digest(token: str) -> str`
  - URL 名稱 `node-setup`（`/setup/`）
  - session 鍵 `node_setup_token_digest`
  - `templates/node/auth_base.html`（Task 7 的登入頁沿用）

- [ ] **Step 1: 寫失敗測試**

```python
# node/tests/test_setup.py
import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from config.node_paths import NodePaths, issue_setup_token, read_setup_token
from node.audit import SETUP_COMPLETED
from node.models import NodeAuditEvent, NodeInstallation
from organizations.models import Organization, OrganizationMembership

User = get_user_model()
VALID = {
    "organization_name": "Acme",
    "email": "Owner@Example.com",
    "password1": "Correct-Horse-9",
    "password2": "Correct-Horse-9",
}


class SetupTestCase(TestCase):
    def setUp(self):
        self._home = tempfile.TemporaryDirectory()
        self.addCleanup(self._home.cleanup)
        self.paths = NodePaths(Path(self._home.name))
        self.paths.ensure(restrict=lambda _path: None)
        override = override_settings(NODE_PATHS=self.paths, NODE_SETUP_GATE=True)
        override.enable()
        self.addCleanup(override.disable)

    def approve(self):
        token = issue_setup_token(self.paths)
        response = self.client.get(f"/setup/?token={token}")
        self.assertRedirects(response, "/setup/", fetch_redirect_response=False)
        return token


class SetupGateTests(SetupTestCase):
    def test_pages_redirect_to_setup_until_done(self):
        self.assertRedirects(self.client.get("/dashboard/"), "/setup/", fetch_redirect_response=False)
        self.assertRedirects(self.client.get("/node/"), "/setup/", fetch_redirect_response=False)

    def test_health_checks_are_not_gated(self):
        self.assertEqual(self.client.get("/healthz/").status_code, 200)


class SetupTokenTests(SetupTestCase):
    def test_missing_token_is_refused_without_echo(self):
        response = self.client.get("/setup/")
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "請從系統匣重新開啟設定", status_code=403)

    def test_wrong_token_is_refused_without_echo(self):
        issue_setup_token(self.paths)
        response = self.client.get("/setup/?token=guess-123")
        self.assertEqual(response.status_code, 403)
        self.assertNotContains(response, "guess-123", status_code=403)

    def test_remote_address_is_refused_even_with_the_right_token(self):
        token = issue_setup_token(self.paths)
        response = self.client.get(f"/setup/?token={token}", REMOTE_ADDR="192.168.1.20")
        self.assertEqual(response.status_code, 403)

    def test_regenerated_token_invalidates_an_open_form(self):
        self.approve()
        issue_setup_token(self.paths)  # tray reopened setup
        response = self.client.post("/setup/", VALID)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(User.objects.exists())


class SetupCompletionTests(SetupTestCase):
    def test_valid_form_creates_owner_organization_and_audit(self):
        self.approve()
        self.assertEqual(self.client.get("/setup/").status_code, 200)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post("/setup/", VALID)
        self.assertRedirects(response, reverse("node:overview"), fetch_redirect_response=False)

        owner = User.objects.get()
        self.assertEqual(owner.email, "owner@example.com")
        self.assertTrue(owner.is_manager)
        self.assertTrue(owner.check_password("Correct-Horse-9"))
        membership = OrganizationMembership.objects.get()
        self.assertEqual((membership.user, membership.role), (owner, "owner"))
        self.assertEqual(Organization.current().name, "Acme")
        self.assertTrue(NodeInstallation.setup_complete())
        self.assertTrue(NodeAuditEvent.objects.filter(action=SETUP_COMPLETED, actor=owner).exists())
        self.assertIsNone(read_setup_token(self.paths))
        self.assertEqual(self.client.get(reverse("node:overview")).status_code, 200)  # signed in

    def test_setup_is_gone_after_completion(self):
        token = self.approve()
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post("/setup/", VALID)
        self.assertEqual(self.client.get("/setup/").status_code, 404)
        self.assertEqual(self.client.get(f"/setup/?token={token}").status_code, 404)
        self.assertEqual(self.client.post("/setup/", VALID).status_code, 404)
        self.assertEqual(User.objects.count(), 1)

    def test_mismatched_or_weak_password_creates_nothing(self):
        self.approve()
        response = self.client.post("/setup/", {**VALID, "password2": "Other-Horse-9"})
        self.assertEqual(response.status_code, 200)
        response = self.client.post("/setup/", {**VALID, "password1": "123", "password2": "123"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.exists())
        self.assertFalse(NodeInstallation.setup_complete())
```

- [ ] **Step 2: 執行確認失敗**

Run（node）：`... manage.py test node.tests.test_setup --settings=config.settings_test`
Expected: FAIL（`/setup/` 404、沒有 gate）

- [ ] **Step 3: 實作**

```python
# node/middleware.py
from django.conf import settings
from django.shortcuts import redirect

from .models import NodeInstallation


class SetupRequiredMiddleware:
    """Until first-run setup finishes, every page except setup leads to /setup/."""

    EXEMPT_PREFIXES = ("/setup/", "/healthz/")

    def __init__(self, get_response):
        self.get_response = get_response
        self._complete = False  # setup never becomes incomplete again

    def __call__(self, request):
        if settings.NODE_SETUP_GATE and not self._complete and not self._exempt(request.path):
            if NodeInstallation.setup_complete():
                self._complete = True
            else:
                return redirect("node-setup")
        return self.get_response(request)

    def _exempt(self, path):
        static_prefix = "/" + settings.STATIC_URL.lstrip("/")
        return path.startswith(self.EXEMPT_PREFIXES) or path.startswith(static_prefix)
```

```python
# node/setup.py
import hashlib

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from config.node_paths import clear_setup_token
from organizations.models import Organization, OrganizationMembership

from .audit import SETUP_COMPLETED, record
from .models import NodeInstallation


class SetupAlreadyCompleted(Exception):
    pass


def token_digest(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@transaction.atomic
def complete_setup(*, organization_name, email, password, request=None):
    installation = NodeInstallation.load()
    if installation.is_setup_complete:
        raise SetupAlreadyCompleted
    User = get_user_model()
    owner = User.objects.create_user(
        username=email,
        email=email,
        password=password,
        role=User.Role.MANAGER,
        is_staff=True,
        is_superuser=True,
        is_email_verified=True,
    )
    organization = Organization.objects.create(name=organization_name)
    OrganizationMembership.objects.create(
        user=owner, organization=organization, role=OrganizationMembership.Role.OWNER
    )
    installation.setup_completed_at = timezone.now()
    installation.save()
    record(SETUP_COMPLETED, request=request, actor=owner, target=organization.name)
    paths = settings.NODE_PATHS
    transaction.on_commit(lambda: clear_setup_token(paths))
    return owner
```

`node/forms.py` 加入：

```python
from django.contrib.auth import get_user_model, password_validation
from django.core.exceptions import ValidationError


class NodeSetupForm(forms.Form):
    organization_name = forms.CharField(label="組織名稱", max_length=120)
    email = forms.EmailField(label="擁有者 Email")
    password1 = forms.CharField(label="密碼", strip=False, widget=forms.PasswordInput)
    password2 = forms.CharField(label="確認密碼", strip=False, widget=forms.PasswordInput)

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        User = get_user_model()
        if User.objects.filter(email__iexact=email).exists() or User.objects.filter(username__iexact=email).exists():
            raise ValidationError("此 Email 已有帳號。")
        return email

    def clean(self):
        cleaned = super().clean()
        password1, password2 = cleaned.get("password1"), cleaned.get("password2")
        if password1 and password2 and password1 != password2:
            self.add_error("password2", "兩次輸入的密碼不一致。")
        elif password1 and cleaned.get("email"):
            candidate = get_user_model()(username=cleaned["email"], email=cleaned["email"])
            try:
                password_validation.validate_password(password1, user=candidate)
            except ValidationError as error:
                self.add_error("password1", error)
        return cleaned
```

```python
# node/setup_views.py
import secrets

from django.conf import settings
from django.contrib.auth import login
from django.http import Http404
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache

from config.node_paths import read_setup_token

from .forms import NodeSetupForm
from .models import NodeInstallation
from .setup import SetupAlreadyCompleted, complete_setup, token_digest

LOCAL_ADDRESSES = {"127.0.0.1", "::1"}
SESSION_KEY = "node_setup_token_digest"


def _blocked(request, reason):
    return render(request, "node/setup_blocked.html", {"reason": reason}, status=403)


def _current_digest():
    token = read_setup_token(settings.NODE_PATHS)
    return token_digest(token) if token else None


@never_cache
def setup_view(request):
    if NodeInstallation.setup_complete():
        raise Http404
    if request.META.get("REMOTE_ADDR") not in LOCAL_ADDRESSES:
        return _blocked(request, "首次設定只能在安裝本程式的電腦上進行。")

    current = _current_digest()
    supplied = request.GET.get("token", "")
    if supplied:
        if current and secrets.compare_digest(token_digest(supplied), current):
            request.session[SESSION_KEY] = current
            return redirect("node-setup")  # drop the token from the address bar
        return _blocked(request, "設定連結無效或已過期。")

    approved = request.session.get(SESSION_KEY, "")
    if not (current and approved and secrets.compare_digest(approved, current)):
        return _blocked(request, "設定連結無效或已過期。")

    form = NodeSetupForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            owner = complete_setup(
                organization_name=form.cleaned_data["organization_name"],
                email=form.cleaned_data["email"],
                password=form.cleaned_data["password1"],
                request=request,
            )
        except SetupAlreadyCompleted:
            raise Http404
        request.session.pop(SESSION_KEY, None)
        login(request, owner, backend="django.contrib.auth.backends.ModelBackend")
        return redirect("node:overview")
    return render(request, "node/setup.html", {"form": form})
```

`config/urls.py` 的 node 區塊加入（檔頭在 `if settings.IS_NODE:` 內 import，cloud 模式不會載入 node app）：

```python
if settings.IS_NODE:
    from node.setup_views import setup_view

    urlpatterns += [
        path("setup/", setup_view, name="node-setup"),
        path("", RedirectView.as_view(pattern_name="node:overview", permanent=False)),
        path("node/", include("node.urls")),
    ]
```

`config/settings.py` node 區塊在 `NODE_SETUP_GATE = True` 前加：`MIDDLEWARE.append("node.middleware.SetupRequiredMiddleware")`。

```django
{# templates/node/auth_base.html #}
{% extends "base.html" %}
{% block body_class %}public-body auth-public-body{% endblock %}
{% block shell %}
<main class="auth-layout auth-page-public">
    <section class="auth-card">
        {% if messages %}
            {% for message in messages %}<div class="flash-message flash-{{ message.tags|default:'info' }}">{{ message }}</div>{% endfor %}
        {% endif %}
        {% block body %}{% block content %}{% endblock %}{% endblock %}
    </section>
</main>
{% endblock %}
```

```django
{# templates/node/setup.html #}
{% extends "node/auth_base.html" %}
{% block title %}首次設定 | FeedBack IQ{% endblock %}
{% block content %}
<p class="section-kicker">First-run Setup</p>
<h1>建立擁有者帳號</h1>
<p>擁有者可以管理成員與節點設定。完成後會直接進入主控台，並顯示資料庫與資料目錄的檢查結果。</p>
<form method="post" class="form-stack">
    {% csrf_token %}
    {{ form.non_field_errors }}
    {% for field in form %}
        <label for="{{ field.id_for_label }}">{{ field.label }}</label>
        {{ field }}
        {{ field.errors }}
    {% endfor %}
    <button type="submit" class="button button-primary">完成設定</button>
</form>
{% endblock %}
```

```django
{# templates/node/setup_blocked.html #}
{% extends "node/auth_base.html" %}
{% block title %}無法開啟設定 | FeedBack IQ{% endblock %}
{% block content %}
<h1>無法開啟設定</h1>
<p>{{ reason }}</p>
<p>請從系統匣重新開啟設定：在工作列右下角的 FeedBack IQ 圖示按右鍵，選「開啟主控台」。</p>
{% endblock %}
```

- [ ] **Step 4: 執行確認通過**

Run（node）：`... manage.py test node --settings=config.settings_test`
Expected: PASS
Run（cloud）：`.\.venv\Scripts\python.exe manage.py test config --settings=config.settings_test`
Expected: PASS（`/setup/` 在 cloud 仍為 404）

- [ ] **Step 5: Commit（需使用者授權）**

```bash
git add node config templates/node
git commit -m "feat(node): one-time local first-run setup that creates the owner"
```

---

### Task 7: 本機帳號登入（allauth）與閒置逾時

**Files:**
- Modify: `requirements.txt`（`django-allauth==65.19.5`）
- Modify: `config/settings.py`（node 區塊加 allauth 與 session 設定）
- Modify: `config/urls.py`（node 模式掛載 `auth/`）
- Modify: `accounts/urls.py`、`accounts/views.py`（node 模式 `login` 轉址、`signup` 404）
- Create: `node/adapters.py`、`node/signals.py`；Modify: `node/apps.py`（`ready()` 註冊 signals）
- Modify: `node/setup.py`（建立 allauth `EmailAddress`）
- Create: `templates/account/login.html`、`templates/allauth/layouts/base.html`
- Test: `node/tests/test_login.py`；依分類規則為既有測試加 `@cloud_only`

**Interfaces:**
- Consumes: Task 6 `complete_setup`、`templates/node/auth_base.html`；Task 2 `record`、`LOGIN_SUCCEEDED`、`LOGIN_FAILED`
- Produces: URL 名稱 `account_login`（`/auth/login/`）、`account_logout`；`accounts:login` 在 node 模式轉址到 `account_login`（保留查詢字串）

- [ ] **Step 1: 確認 allauth 與 Django 6.0 相容（套件安裝需使用者授權）**

Run：`.\.venv\Scripts\python.exe -m pip install "django-allauth==65.19.5"`
然後在 `requirements.txt` 依字母順序加入 `django-allauth==65.19.5`，完成 Step 3 設定後執行
`$env:DEPLOYMENT_MODE='node'; ... manage.py check --settings=config.settings_test`
Expected: `System check identified no issues`。若出現與 Django 6.0 不相容的錯誤，停止並回報錯誤訊息，不換套件、不自行降版 Django。

- [ ] **Step 2: 寫失敗測試**

```python
# node/tests/test_login.py
import tempfile
from datetime import timedelta
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from config.node_paths import NodePaths
from node.audit import LOGIN_FAILED, LOGIN_SUCCEEDED
from node.models import NodeAuditEvent
from node.setup import complete_setup

User = get_user_model()
PASSWORD = "Correct-Horse-9"


class NodeLoginTests(TestCase):
    def setUp(self):
        cache.clear()
        self._home = tempfile.TemporaryDirectory()
        self.addCleanup(self._home.cleanup)
        paths = NodePaths(Path(self._home.name))
        paths.ensure(restrict=lambda _path: None)
        override = override_settings(NODE_PATHS=paths, NODE_SETUP_GATE=True)
        override.enable()
        self.addCleanup(override.disable)
        with self.captureOnCommitCallbacks(execute=True):
            self.owner = complete_setup(organization_name="Acme", email="owner@example.com", password=PASSWORD)

    def login(self, email, password):
        return self.client.post(reverse("account_login"), {"login": email, "password": password})

    def test_owner_signs_in_and_lands_on_console(self):
        response = self.login("owner@example.com", PASSWORD)
        self.assertRedirects(response, reverse("node:overview"), fetch_redirect_response=False)
        self.assertTrue(NodeAuditEvent.objects.filter(action=LOGIN_SUCCEEDED, actor=self.owner).exists())

    def test_email_case_does_not_matter(self):
        response = self.login("OWNER@Example.com", PASSWORD)
        self.assertRedirects(response, reverse("node:overview"), fetch_redirect_response=False)

    def test_wrong_password_is_audited(self):
        response = self.login("owner@example.com", "nope")
        self.assertEqual(response.status_code, 200)
        event = NodeAuditEvent.objects.get(action=LOGIN_FAILED)
        self.assertEqual(event.target, "owner@example.com")
        self.assertEqual(event.ip, "127.0.0.1")

    def test_repeated_failures_are_rate_limited(self):
        for _ in range(5):
            self.login("owner@example.com", "nope")
        response = self.login("owner@example.com", PASSWORD)
        self.assertNotEqual(response.status_code, 302)

    def test_legacy_login_url_forwards_to_allauth(self):
        response = self.client.get("/accounts/login/?next=/dashboard/")
        self.assertRedirects(response, "/auth/login/?next=/dashboard/", fetch_redirect_response=False)

    def test_anonymous_console_request_goes_to_allauth_login(self):
        response = self.client.get(reverse("node:overview"))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith("/auth/login/"))

    def test_no_public_sign_up(self):
        self.assertEqual(self.client.get("/accounts/signup/").status_code, 404)
        self.client.post(
            "/auth/signup/",
            {"email": "new@example.com", "password1": PASSWORD, "password2": PASSWORD},
        )
        self.assertFalse(User.objects.filter(email="new@example.com").exists())

    def test_idle_session_expires(self):
        self.login("owner@example.com", PASSWORD)
        session = self.client.session
        session.set_expiry(timezone.now() - timedelta(seconds=1))
        session.save()
        response = self.client.get(reverse("node:overview"))
        self.assertEqual(response.status_code, 302)

    def test_idle_timeout_settings(self):
        from django.conf import settings

        self.assertEqual(settings.SESSION_COOKIE_AGE, 4 * 3600)
        self.assertTrue(settings.SESSION_SAVE_EVERY_REQUEST)
```

- [ ] **Step 3: 設定與實作**

`config/settings.py` node 區塊：把 `INSTALLED_APPS += ["organizations", "node"]` 改為下方第一行，並在 `NODE_SETUP_GATE = True` 之前加入其餘設定（Task 6 的 `SetupRequiredMiddleware` 那行保留不動）：

```python
    INSTALLED_APPS += ["allauth", "allauth.account", "organizations", "node"]
    MIDDLEWARE.append("allauth.account.middleware.AccountMiddleware")
    AUTHENTICATION_BACKENDS = [
        "django.contrib.auth.backends.ModelBackend",
        "allauth.account.auth_backends.AuthenticationBackend",
    ]
    ACCOUNT_ADAPTER = "node.adapters.NodeAccountAdapter"
    ACCOUNT_LOGIN_METHODS = {"email"}
    ACCOUNT_SIGNUP_FIELDS = ["email*", "password1*", "password2*"]
    ACCOUNT_EMAIL_VERIFICATION = "none"
    ACCOUNT_SESSION_REMEMBER = True
    LOGIN_URL = "account_login"
    LOGIN_REDIRECT_URL = "node:overview"
    LOGOUT_REDIRECT_URL = "account_login"
    # Idle timeout: every request pushes expiry forward by SESSION_COOKIE_AGE.
    SESSION_COOKIE_AGE = int(os.getenv("NODE_SESSION_IDLE_SECONDS", str(4 * 3600)))
    SESSION_SAVE_EVERY_REQUEST = True
```

```python
# node/adapters.py
from allauth.account.adapter import DefaultAccountAdapter


class NodeAccountAdapter(DefaultAccountAdapter):
    def is_open_for_signup(self, request):
        # Staff join a node by invitation from the owner, never by self sign-up.
        return False
```

```python
# node/signals.py
from django.contrib.auth.signals import user_logged_in, user_login_failed
from django.dispatch import receiver

from .audit import LOGIN_FAILED, LOGIN_SUCCEEDED, record


@receiver(user_logged_in)
def audit_login(sender, request, user, **kwargs):
    record(LOGIN_SUCCEEDED, request=request, actor=user)


@receiver(user_login_failed)
def audit_login_failure(sender, credentials, request=None, **kwargs):
    attempted = credentials.get("email") or credentials.get("username") or ""
    record(LOGIN_FAILED, request=request, target=str(attempted).lower())
```

`node/apps.py` 的 `NodeConfig` 加：

```python
    def ready(self):
        from . import signals  # noqa: F401
```

`node/setup.py` 的 `complete_setup` 在建立 `owner` 後加入：

```python
    from allauth.account.models import EmailAddress

    EmailAddress.objects.create(user=owner, email=email, primary=True, verified=True)
```

`accounts/views.py` 加入：

```python
from django.http import Http404


def signup_unavailable(request):
    raise Http404("本機節點不開放公開註冊")
```

`accounts/urls.py` 檔尾加入（檔頭補 `from django.conf import settings`、`from django.views.generic import RedirectView`，並把 `signup_unavailable` 加進 `.views` import）：

```python
if settings.IS_NODE:
    # Local node: sign-in is allauth's, and there is no public sign-up.
    urlpatterns = [pattern for pattern in urlpatterns if pattern.name not in {"login", "signup"}] + [
        path("login/", RedirectView.as_view(pattern_name="account_login", query_string=True), name="login"),
        path("signup/", signup_unavailable, name="signup"),
    ]
```

`config/urls.py` 的 node 區塊加入 `path("auth/", include("allauth.urls")),`。

```django
{# templates/account/login.html #}
{% extends "node/auth_base.html" %}
{% block title %}登入主控台 | FeedBack IQ{% endblock %}
{% block content %}
<p class="section-kicker">Local Node</p>
<h1>登入主控台</h1>
<form method="post" action="{% url 'account_login' %}" class="form-stack">
    {% csrf_token %}
    {{ form.non_field_errors }}
    <label for="{{ form.login.id_for_label }}">Email</label>
    {{ form.login }}
    {{ form.login.errors }}
    <label for="{{ form.password.id_for_label }}">密碼</label>
    {{ form.password }}
    {{ form.password.errors }}
    {% if redirect_field_value %}
        <input type="hidden" name="{{ redirect_field_name }}" value="{{ redirect_field_value }}">
    {% endif %}
    <button type="submit" class="button button-primary">登入</button>
</form>
{% endblock %}
```

```django
{# templates/allauth/layouts/base.html — allauth's own pages (logout, sign-up closed, password reset) #}
{% extends "node/auth_base.html" %}
{% block title %}{% block head_title %}{% endblock %} | FeedBack IQ{% endblock %}
```

- [ ] **Step 4: 執行確認通過**

Run（node）：`... manage.py test node --settings=config.settings_test`
Expected: PASS

- [ ] **Step 5: 全套件與分類**

Run（node）：`... manage.py test feedback accounts config node organizations --settings=config.settings_test`
Expected: 可預期的新失敗只有請求 `/accounts/login/` 頁面內容（例如 `accounts/tests.py` 的 `SharedLoginEntryTests`）或 `/accounts/signup/` 的測試 → 加 `@cloud_only`（從 `feedback.test_utils` import）。斷言「未登入導向某登入網址」的測試若寫死 `/accounts/login/`，改為比對 `resolve_url(settings.LOGIN_URL)`，不加 `@cloud_only`。其他失敗視為回歸並修正。
Run（cloud）：`.\.venv\Scripts\python.exe manage.py test feedback accounts config --settings=config.settings_test`
Expected: PASS（allauth 未安裝進 cloud 的 `INSTALLED_APPS`）

- [ ] **Step 6: Commit（需使用者授權）**

```bash
git add requirements.txt config accounts node templates/account templates/allauth accounts/tests.py feedback
git commit -m "feat(node): allauth local sign-in with rate limit, audit and idle timeout"
```

---

### Task 8: 啟動器核心（埠、Worker 監督、單一實例）

**Files:**
- Create: `desktop_app/node_runtime.py`
- Test: `feedback/test_node_runtime.py`

**Interfaces:**
- Consumes: Task 1 `NodePaths`
- Produces:
  - 常數 `DEFAULT_HOST="127.0.0.1"`、`DEFAULT_PORT=8750`、`PORT_ATTEMPTS=20`
  - `bind_server(app, *, server_factory, host=DEFAULT_HOST, start_port=DEFAULT_PORT, attempts=PORT_ATTEMPTS) -> tuple[server, int]`（`server_factory((host, port), app)` 回傳具 `prepare()` 的物件）
  - `console_url(port, host=DEFAULT_HOST) -> str`、`setup_url(base_url, token) -> str`
  - `worker_command(*, executable=None, frozen=None) -> list[str]`
  - `WorkerSupervisor(start_process, *, state_file, clock=time.monotonic, max_restarts=3, window_seconds=300)`：`start()`、`poll()`、`shutdown(timeout=10)`、屬性 `stopped: bool`
  - `existing_console(paths, *, probe) -> str | None`、`http_probe(url, timeout=1.0) -> bool`

- [ ] **Step 1: 寫失敗測試**

```python
# feedback/test_node_runtime.py
import subprocess
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from config.node_paths import NodePaths
from desktop_app.node_runtime import (
    WorkerSupervisor,
    bind_server,
    console_url,
    existing_console,
    setup_url,
    worker_command,
)


class FakeServer:
    def __init__(self, address, app, busy):
        self.address = address
        self._busy = busy

    def prepare(self):
        if self.address[1] in self._busy:
            raise OSError("address in use")


class BindServerTests(SimpleTestCase):
    def test_skips_busy_ports(self):
        server, port = bind_server(object(), server_factory=lambda address, app: FakeServer(address, app, {8750, 8751}))
        self.assertEqual(port, 8752)
        self.assertEqual(server.address, ("127.0.0.1", 8752))

    def test_gives_up_after_the_range(self):
        with self.assertRaises(RuntimeError):
            bind_server(object(), attempts=3, server_factory=lambda address, app: FakeServer(address, app, {8750, 8751, 8752}))


class UrlTests(SimpleTestCase):
    def test_urls(self):
        base = console_url(8752)
        self.assertEqual(base, "http://127.0.0.1:8752/")
        self.assertEqual(setup_url(base, "a+b/c"), "http://127.0.0.1:8752/setup/?token=a%2Bb%2Fc")


class WorkerCommandTests(SimpleTestCase):
    def test_frozen_exe_reuses_itself(self):
        self.assertEqual(worker_command(executable="C:/app/FIH.exe", frozen=True), ["C:/app/FIH.exe", "--worker"])

    def test_source_run_uses_the_package(self):
        self.assertEqual(
            worker_command(executable="python.exe", frozen=False), ["python.exe", "-m", "desktop_app", "--worker"]
        )


class FakeProcess:
    def __init__(self):
        self.returncode = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9


class WorkerSupervisorTests(SimpleTestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.state_file = Path(self._directory.name) / "worker.state"
        self.now = 0.0
        self.started = []

    def supervisor(self):
        def start():
            process = FakeProcess()
            self.started.append(process)
            return process

        return WorkerSupervisor(start, state_file=self.state_file, clock=lambda: self.now)

    def crash(self):
        self.started[-1].returncode = 1

    def test_restarts_three_times_then_stops(self):
        supervisor = self.supervisor()
        supervisor.start()
        self.assertEqual(self.state_file.read_text(encoding="utf-8"), "running")
        for _ in range(3):
            self.now += 10
            self.crash()
            supervisor.poll()
        self.assertEqual(len(self.started), 4)
        self.assertFalse(supervisor.stopped)
        self.now += 10
        self.crash()
        supervisor.poll()
        self.assertTrue(supervisor.stopped)
        self.assertEqual(len(self.started), 4)
        self.assertEqual(self.state_file.read_text(encoding="utf-8"), "stopped")

    def test_crashes_outside_the_window_do_not_accumulate(self):
        supervisor = self.supervisor()
        supervisor.start()
        for _ in range(6):
            self.now += 301
            self.crash()
            supervisor.poll()
        self.assertFalse(supervisor.stopped)
        self.assertEqual(len(self.started), 7)

    def test_healthy_worker_is_left_alone(self):
        supervisor = self.supervisor()
        supervisor.start()
        supervisor.poll()
        self.assertEqual(len(self.started), 1)

    def test_shutdown_terminates_and_clears_state(self):
        supervisor = self.supervisor()
        supervisor.start()
        supervisor.shutdown()
        self.assertTrue(self.started[-1].terminated)
        self.assertFalse(self.state_file.exists())


class ExistingConsoleTests(SimpleTestCase):
    def test_running_instance_is_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = NodePaths(Path(directory))
            paths.run_dir.mkdir(parents=True)
            paths.port_file.write_text("8753", encoding="utf-8")
            probed = []
            url = existing_console(paths, probe=lambda target: probed.append(target) or True)
            self.assertEqual(url, "http://127.0.0.1:8753/")
            self.assertEqual(probed, ["http://127.0.0.1:8753/healthz/"])

    def test_stale_port_file_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = NodePaths(Path(directory))
            self.assertIsNone(existing_console(paths, probe=lambda target: True))
            paths.run_dir.mkdir(parents=True)
            paths.port_file.write_text("8753", encoding="utf-8")
            self.assertIsNone(existing_console(paths, probe=lambda target: False))
            paths.port_file.write_text("garbage", encoding="utf-8")
            self.assertIsNone(existing_console(paths, probe=lambda target: True))
```

- [ ] **Step 2: 執行確認失敗**

Run：`.\.venv\Scripts\python.exe manage.py test feedback.test_node_runtime --settings=config.settings_test`
Expected: FAIL（`desktop_app.node_runtime` 不存在）

- [ ] **Step 3: 實作**

```python
# desktop_app/node_runtime.py
"""Local node launcher logic that needs neither a GUI nor a web server.

Kept free of pystray/cheroot imports so it is unit-testable on any OS.
"""

import logging
import subprocess
import sys
import time
import urllib.request
from collections import deque
from urllib.parse import urlencode

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8750
PORT_ATTEMPTS = 20


def bind_server(app, *, server_factory, host=DEFAULT_HOST, start_port=DEFAULT_PORT, attempts=PORT_ATTEMPTS):
    last_error = None
    for port in range(start_port, start_port + attempts):
        server = server_factory((host, port), app)
        try:
            server.prepare()
        except OSError as error:
            last_error = error
            continue
        return server, port
    raise RuntimeError(f"{host}:{start_port}-{start_port + attempts - 1} 都已被占用") from last_error


def console_url(port, host=DEFAULT_HOST):
    return f"http://{host}:{port}/"


def setup_url(base_url, token):
    return f"{base_url}setup/?{urlencode({'token': token})}"


def worker_command(*, executable=None, frozen=None):
    executable = executable or sys.executable
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    return [executable, "--worker"] if frozen else [executable, "-m", "desktop_app", "--worker"]


def start_worker_process():
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.Popen(worker_command(), creationflags=flags)


class WorkerSupervisor:
    """Restart a crashed worker, but give up after repeated crashes in a short window."""

    def __init__(self, start_process, *, state_file, clock=time.monotonic, max_restarts=3, window_seconds=300):
        self._start_process = start_process
        self._state_file = state_file
        self._clock = clock
        self._max_restarts = max_restarts
        self._window_seconds = window_seconds
        self._crashes = deque()
        self.process = None
        self.stopped = False

    def _write_state(self, state):
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        self._state_file.write_text(state, encoding="utf-8")

    def start(self):
        self.stopped = False
        self._crashes.clear()
        self.process = self._start_process()
        self._write_state("running")

    def poll(self):
        if self.stopped or self.process is None:
            return
        code = self.process.poll()
        if code is None:
            return
        now = self._clock()
        self._crashes.append(now)
        while self._crashes and now - self._crashes[0] > self._window_seconds:
            self._crashes.popleft()
        if len(self._crashes) > self._max_restarts:
            self.stopped = True
            self._write_state("stopped")
            logger.error("worker exited with %s; restart limit reached, not restarting", code)
            return
        logger.warning("worker exited with %s; restarting", code)
        self.process = self._start_process()

    def shutdown(self, timeout=10):
        process, self.process = self.process, None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
        try:
            self._state_file.unlink()
        except FileNotFoundError:
            pass


def http_probe(url, timeout=1.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status == 200
    except OSError:
        return False


def existing_console(paths, *, probe=http_probe):
    """URL of a console another launcher already serves, or None."""

    try:
        port = int(paths.port_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    url = console_url(port)
    return url if probe(f"{url}healthz/") else None
```

- [ ] **Step 4: 執行確認通過**

Run：`.\.venv\Scripts\python.exe manage.py test feedback.test_node_runtime --settings=config.settings_test`
Expected: PASS

- [ ] **Step 5: Commit（需使用者授權）**

```bash
git add desktop_app/node_runtime.py feedback/test_node_runtime.py
git commit -m "feat(node): launcher core for port fallback, worker supervision and single instance"
```

---

### Task 9: 執行角色、系統匣、EXE 與文件

**Files:**
- Create: `desktop_app/node_launcher.py`、`desktop_app/node_tray.py`、`desktop_app/autostart.py`
- Create: `requirements-node.txt`；Modify: `requirements-desktop.txt`
- Modify: `desktop_app/__main__.py`（角色切換）
- Modify: `scripts/build_desktop.ps1`（打包模板、靜態檔與新依賴）
- Modify: `README.md`、`docs/architecture.md`、`docs/next-actions.md`、`docs/superpowers/specs/2026-09-30-local-node-architecture-design.md`（子專案狀態）
- Test: `feedback/test_desktop_entrypoint.py`、`feedback/test_autostart.py`

**Interfaces:**
- Consumes: Task 1 `NodePaths`、`issue_setup_token`；Task 2 `NodeInstallation.setup_complete()`；Task 4 `--heartbeat-file`；Task 8 全部
- Produces:
  - `desktop_app.__main__.select_role(argv) -> str`（`"launcher"`、`"worker"`、`"legacy"`、`"smoke"`）
  - `desktop_app.__main__.prepare_environment(role, environ=None) -> Path | None`
  - `desktop_app.autostart.is_enabled(*, registry=None, command=None) -> bool`、`set_enabled(enabled, *, registry=None, command=None) -> None`
  - `desktop_app.node_launcher.run_launcher()`、`run_worker()`

- [ ] **Step 1: 寫失敗測試**

`feedback/test_desktop_entrypoint.py` 加入（檔頭 import 補 `prepare_environment, select_role`）：

```python
class RoleSelectionTests(SimpleTestCase):
    def test_roles(self):
        self.assertEqual(select_role([]), "launcher")
        self.assertEqual(select_role(["--worker"]), "worker")
        self.assertEqual(select_role(["--legacy-workbench"]), "legacy")
        self.assertEqual(select_role(["--smoke-test"]), "smoke")

    def test_node_roles_never_load_the_external_cloud_env(self):
        environ = {}
        with patch("desktop_app.__main__._load_external_environment") as load:
            prepare_environment("launcher", environ)
            prepare_environment("worker", environ)
        load.assert_not_called()
        self.assertEqual(environ["DEPLOYMENT_MODE"], "node")

    def test_legacy_workbench_stays_on_the_cloud_database(self):
        environ = {"FEEDBACK_HUB_DATABASE_URL": "postgres://example.invalid/db"}
        with patch("desktop_app.__main__._load_external_environment", return_value=None):
            prepare_environment("legacy", environ)
        self.assertEqual(environ["DEPLOYMENT_MODE"], "cloud")
        self.assertEqual(environ["DATABASE_URL"], "postgres://example.invalid/db")
```

（若檔頭沒有 `from unittest.mock import patch`，一併補上。）

```python
# feedback/test_autostart.py
from django.test import SimpleTestCase

from desktop_app.autostart import is_enabled, set_enabled


class FakeRegistry:
    HKEY_CURRENT_USER = "HKCU"
    KEY_READ = 1
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self):
        self.values = {}

    def OpenKey(self, root, path, reserved=0, access=0):
        return self

    def CreateKey(self, root, path):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def QueryValueEx(self, key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        return self.values[name], self.REG_SZ

    def SetValueEx(self, key, name, reserved, kind, value):
        self.values[name] = value

    def DeleteValue(self, key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        del self.values[name]


class AutostartTests(SimpleTestCase):
    def test_toggle(self):
        registry = FakeRegistry()
        command = '"C:\\Apps\\FeedbackInsightHub.exe"'
        self.assertFalse(is_enabled(registry=registry, command=command))
        set_enabled(True, registry=registry, command=command)
        self.assertTrue(is_enabled(registry=registry, command=command))
        set_enabled(False, registry=registry, command=command)
        self.assertFalse(is_enabled(registry=registry, command=command))
        set_enabled(False, registry=registry, command=command)  # already off
```

- [ ] **Step 2: 執行確認失敗**

Run：`.\.venv\Scripts\python.exe manage.py test feedback.test_desktop_entrypoint feedback.test_autostart --settings=config.settings_test`
Expected: FAIL（`select_role`、`desktop_app.autostart` 不存在）

- [ ] **Step 3: 實作 autostart 與系統匣**

```python
# desktop_app/autostart.py
"""Start the node when the user signs in to Windows (HKCU Run key, no admin rights)."""

import sys

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "FeedbackInsightHub"


def _registry(registry):
    if registry is not None:
        return registry
    import winreg

    return winreg


def launch_command():
    return f'"{sys.executable}"'


def is_enabled(*, registry=None, command=None):
    registry = _registry(registry)
    command = command or launch_command()
    try:
        with registry.OpenKey(registry.HKEY_CURRENT_USER, RUN_KEY, 0, registry.KEY_READ) as key:
            value, _kind = registry.QueryValueEx(key, VALUE_NAME)
    except FileNotFoundError:
        return False
    return value == command


def set_enabled(enabled, *, registry=None, command=None):
    registry = _registry(registry)
    command = command or launch_command()
    with registry.CreateKey(registry.HKEY_CURRENT_USER, RUN_KEY) as key:
        if enabled:
            registry.SetValueEx(key, VALUE_NAME, 0, registry.REG_SZ, command)
        else:
            try:
                registry.DeleteValue(key, VALUE_NAME)
            except FileNotFoundError:
                pass
```

```python
# desktop_app/node_tray.py
"""System tray menu for the local node launcher."""


def build_icon_image():
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((4, 4, 60, 60), radius=14, fill=(37, 99, 235, 255))
    draw.text((18, 22), "FI", fill=(255, 255, 255, 255))
    return image


def run_tray(*, open_console, open_logs, autostart_enabled, set_autostart, on_exit):
    import pystray

    def exit_app(icon, _item):
        on_exit()
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("開啟主控台", lambda _icon, _item: open_console(), default=True),
        pystray.MenuItem("查看日誌", lambda _icon, _item: open_logs()),
        pystray.MenuItem(
            "開機自動啟動",
            lambda _icon, _item: set_autostart(not autostart_enabled()),
            checked=lambda _item: autostart_enabled(),
        ),
        pystray.MenuItem("結束", exit_app),
    )
    pystray.Icon("FeedbackInsightHub", build_icon_image(), "FeedBack IQ 本機節點", menu).run()
```

- [ ] **Step 4: 實作啟動器與 Worker 角色**

```python
# desktop_app/node_launcher.py
"""Local node roles: the tray launcher (web console + worker supervisor) and the worker."""

import logging
import os
import socket
import threading
import webbrowser

logger = logging.getLogger("desktop_app.node")

SUPERVISE_INTERVAL_SECONDS = 5


def run_worker():
    import django

    django.setup()
    from django.conf import settings
    from django.core.management import call_command

    paths = settings.NODE_PATHS
    call_command(
        "run_analysis_worker",
        worker_id=f"node-{socket.gethostname()}"[:64],
        output=str(paths.artifacts_dir),
        heartbeat_file=str(paths.heartbeat_file),
    )


def run_launcher():
    import django

    django.setup()
    from cheroot import wsgi
    from django.conf import settings
    from django.core.management import call_command
    from django.core.wsgi import get_wsgi_application
    from django.db import close_old_connections

    from config.node_paths import issue_setup_token
    from desktop_app import autostart
    from desktop_app.node_runtime import (
        WorkerSupervisor,
        bind_server,
        console_url,
        existing_console,
        setup_url,
        start_worker_process,
    )
    from desktop_app.node_tray import run_tray

    paths = settings.NODE_PATHS
    running = existing_console(paths)
    if running:
        # A second double-click: show the running console instead of a second server.
        webbrowser.open(running)
        return

    # The node owns its local database; bring the schema up to date before serving.
    call_command("migrate", interactive=False, verbosity=0)
    server, port = bind_server(get_wsgi_application(), server_factory=wsgi.Server)
    paths.port_file.write_text(str(port), encoding="utf-8")
    base_url = console_url(port)
    logger.info("console listening at %s", base_url)

    supervisor = WorkerSupervisor(start_worker_process, state_file=paths.worker_state_file)
    supervisor.start()
    stop = threading.Event()

    def supervise():
        while not stop.wait(SUPERVISE_INTERVAL_SECONDS):
            supervisor.poll()

    def open_console():
        from node.models import NodeInstallation

        close_old_connections()
        if NodeInstallation.setup_complete():
            webbrowser.open(base_url)
        else:
            webbrowser.open(setup_url(base_url, issue_setup_token(paths)))

    def shutdown():
        stop.set()
        supervisor.shutdown()
        server.stop()
        try:
            paths.port_file.unlink()
        except FileNotFoundError:
            pass

    threading.Thread(target=server.serve, name="console-server", daemon=True).start()
    threading.Thread(target=supervise, name="worker-supervisor", daemon=True).start()
    open_console()
    try:
        run_tray(
            open_console=open_console,
            open_logs=lambda: os.startfile(paths.logs_dir),  # noqa: S606 - opens a folder in Explorer
            autostart_enabled=autostart.is_enabled,
            set_autostart=autostart.set_enabled,
            on_exit=shutdown,
        )
    finally:
        if not stop.is_set():
            shutdown()
```

- [ ] **Step 5: 改寫 `desktop_app/__main__.py` 的 `main`**

保留 `_external_env_candidates`、`_load_external_environment`、`_configure_file_logging` 不變；新增並改寫：

```python
ROLE_FLAGS = (
    ("--worker", "worker"),
    ("--legacy-workbench", "legacy"),
    ("--smoke-test", "smoke"),
)


def select_role(argv):
    for flag, role in ROLE_FLAGS:
        if flag in argv:
            return role
    return "launcher"


def prepare_environment(role, environ=None):
    """Node roles run on local data only; the workbench keeps its cloud .env."""

    environ = os.environ if environ is None else environ
    if role in {"launcher", "worker"}:
        environ["DEPLOYMENT_MODE"] = "node"
        return None
    environ["DEPLOYMENT_MODE"] = "cloud"
    loaded = _load_external_environment()
    desktop_database_url = environ.get("FEEDBACK_HUB_DATABASE_URL", "").strip()
    if desktop_database_url and not environ.get("DATABASE_URL", "").strip():
        environ["DATABASE_URL"] = desktop_database_url
    return loaded


def _run_smoke_test():
    import django

    django.setup()
    import dearpygui.dearpygui as dpg
    from cheroot import wsgi  # noqa: F401 - bundled for the node launcher
    import pystray  # noqa: F401
    from django.template.loader import get_template

    from desktop_app.app import FeedbackInsightDesktop
    from feedback.background_analysis import PROFILE_PATH, pipeline_version

    for template in ("feedback/dashboard_base.html", "node/overview.html", "node/setup.html", "account/login.html"):
        get_template(template)
    profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    pipeline_version(profile)
    dpg.create_context()
    try:
        application = FeedbackInsightDesktop()
        application._configure_style()
        application._build()
    finally:
        dpg.destroy_context()


def main():
    role = select_role(sys.argv[1:])
    # Deployment credentials remain external to the bundle and are never logged.
    prepare_environment(role)
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    if role == "smoke":
        _run_smoke_test()
        return
    if role in {"launcher", "worker"}:
        from desktop_app import node_launcher

        # Each runner calls django.setup() and then configures its own log file.
        (node_launcher.run_launcher if role == "launcher" else node_launcher.run_worker)()
        return
    import django

    django.setup()
    # After Django's LOGGING so the file handler is not reset by dictConfig.
    _configure_file_logging()
    from desktop_app.app import main as run_app

    run_app()
```

`_configure_file_logging` 改為可指定目錄與檔名（舊工作台呼叫方式不變）：

```python
def _configure_file_logging(*, local_app_data=None, log_dir=None, filename="desktop.log"):
    """Write warnings and crashes to a rotating log, replacing the console build.

    The windowed EXE has no console, so this file is the diagnostic channel.
    Log records never include credentials (see ``main``).  The launcher and the
    worker are separate processes, so each gets its own file to avoid two
    processes rotating the same log.
    """

    import logging
    from logging.handlers import RotatingFileHandler

    if log_dir is None:
        root = Path(local_app_data or os.getenv("LOCALAPPDATA", "").strip() or Path.cwd())
        log_dir = root / "FeedbackInsightHub" / "logs"
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(log_dir / filename, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.getLogger().addHandler(handler)

    def log_uncaught(exc_type, exc, traceback):
        logging.getLogger("desktop_app").critical("uncaught exception", exc_info=(exc_type, exc, traceback))
        sys.__excepthook__(exc_type, exc, traceback)

    sys.excepthook = log_uncaught
    return log_dir / filename
```

`desktop_app/node_launcher.py`（Step 4）在兩個 runner 取得 `paths` 之後各加一行：
- `run_worker`：`_configure_file_logging(log_dir=paths.logs_dir, filename="worker.log")`
- `run_launcher`：`_configure_file_logging(log_dir=paths.logs_dir, filename="node.log")`

並在兩個函式的 import 區加 `from desktop_app.__main__ import _configure_file_logging`。

`feedback/test_desktop_entrypoint.py` 再加一個測試：

```python
    def test_node_processes_log_to_their_own_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = _configure_file_logging(log_dir=Path(directory) / "logs", filename="worker.log")
            self.assertEqual(path, Path(directory) / "logs" / "worker.log")
            logging.getLogger("desktop_app").warning("probe")
            for handler in logging.getLogger().handlers:
                handler.flush()
            self.assertIn("probe", path.read_text(encoding="utf-8"))
```

（放在既有 `test_warnings_and_uncaught_errors_are_written_to_local_log` 所在的類別；沿用該測試清理 handler 與 `sys.excepthook` 的方式，並補上所需的 `logging`、`tempfile`、`Path` import。）

- [ ] **Step 6: 依賴與打包**

```text
# requirements-node.txt
# Local node launcher: WSGI server with TLS on Windows, tray icon, credential vault.
cheroot>=10,<11
pystray>=0.19,<0.20
Pillow>=11,<13
keyring>=25,<26
```

`requirements-desktop.txt` 在 `-r requirements.txt` 下一行加入 `-r requirements-node.txt`。
安裝（需使用者授權）：`.\.venv\Scripts\python.exe -m pip install -r requirements-desktop.txt`，之後以 `pip freeze` 查實際安裝版本，把 `requirements-node.txt` 的範圍改為確切版本（`==`）。

`scripts/build_desktop.ps1` 的 PyInstaller 參數在 `--collect-all dearpygui` 後加入：

```powershell
        --collect-all allauth `
        --add-data "$ProjectRoot\templates;templates" `
        --add-data "$ProjectRoot\static;static" `
        --hidden-import pystray._win32 `
        --hidden-import cheroot.wsgi `
        --hidden-import node `
        --hidden-import node.apps `
        --hidden-import node.models `
        --hidden-import node.middleware `
        --hidden-import node.signals `
        --hidden-import organizations `
        --hidden-import organizations.apps `
        --hidden-import organizations.models `
        --hidden-import config.urls `
        --hidden-import desktop_app.node_launcher `
        --hidden-import desktop_app.node_runtime `
        --hidden-import desktop_app.node_tray `
        --hidden-import desktop_app.autostart `
```

（`node.urls`、`node.views`、`node.setup_views`、`organizations.admin`、`node.admin` 由 URL 與 admin autodiscover 動態載入，也一併加 `--hidden-import`。）

- [ ] **Step 7: 執行測試**

Run（cloud）：`.\.venv\Scripts\python.exe manage.py test feedback accounts config --settings=config.settings_test`
Run（node）：`... manage.py test feedback accounts config node organizations --settings=config.settings_test`
Expected: 兩者 PASS

- [ ] **Step 8: 打包與實機驗證**

Run：`powershell -ExecutionPolicy Bypass -File scripts\build_desktop.ps1`
Run：`dist\FeedbackInsightHub\FeedbackInsightHub.exe --smoke-test; $LASTEXITCODE`
Expected: `0`

實機走一次（使用隔離的資料根目錄，不動真實 `%LOCALAPPDATA%` 資料）：
1. `$env:FEEDBACK_HUB_NODE_HOME="$env:TEMP\fih-node-manual"; dist\FeedbackInsightHub\FeedbackInsightHub.exe`
2. 瀏覽器自動開啟 `/setup/`；建立擁有者與組織，約 2 分鐘內完成（驗收 1），進入總覽並看到五個狀態（驗收 5）。
3. 從另一台電腦或手機連 `http://<本機區網 IP>:8750/`，應無法連線（驗收 2）。
4. 關閉瀏覽器，從系統匣「開啟主控台」，仍為登入狀態（驗收 4 前半）。
5. 再雙擊一次 EXE：只開瀏覽器，不出現第二個系統匣圖示（Review Focus 1）。
6. 進入問卷管理、統計、文字、AI 結果頁，頁面正常（驗收 6；空資料庫時顯示空狀態）。
7. 系統匣「結束」後，工作管理員中不再有 FeedbackInsightHub 行程。
8. 驗證後 `Remove-Item -Recurse "$env:TEMP\fih-node-manual"`、`Remove-Item Env:FEEDBACK_HUB_NODE_HOME`。

- [ ] **Step 9: 文件（依 docs-current-state-only 原則，只寫現況）**

- `README.md` 桌面章節：無參數啟動為本機節點（系統匣＋ `http://127.0.0.1:8750/`）；`--legacy-workbench` 為原 Dear PyGui 工作台（讀外部 `.env` 連 Supabase）；`--worker` 由啟動器自動使用；資料根目錄與 `FEEDBACK_HUB_NODE_HOME`、`NODE_DATABASE_URL`。
- `docs/architecture.md`：新增「部署模式」一節（`cloud`／`node` 差異表，連結兩份 spec，不複製程式碼）。
- `docs/next-actions.md`：移除「本機節點計畫 A」待辦，加入「計畫 B」項目清單（見本文件開頭）。
- 架構總覽 spec 的子專案表：1、2a 狀態改為「計畫 A 已實作；Google／兩步驟驗證／區域網路等見計畫 B」。

- [ ] **Step 10: Commit 與 PR（需使用者授權）**

```bash
git add desktop_app requirements-node.txt requirements-desktop.txt scripts/build_desktop.ps1 feedback/test_desktop_entrypoint.py feedback/test_autostart.py README.md docs
git commit -m "feat(node): tray launcher, worker role and EXE packaging for the local node"
```

推送並以 gh 開 PR，由使用者合併。
