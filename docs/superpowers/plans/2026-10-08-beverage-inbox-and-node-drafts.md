# 飲料店改走收件匣、網站草稿指派節點 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 飲料店示範改由節點建立問卷、模擬填答經雲端收件匣進入節點；網站新建的草稿自動屬於節點。收件匣只對伺服器端明確允許的自測問卷開放。

**Architecture:** 雲端以兩個環境設定限定收件匣只收自測問卷（`uses_inbox`、發布檢查、`accept_submission`、填答頁四處一致）。`seed_demo_beverage` 新增兩條路徑：node 模式 `--node-create` 經既有節點 API（`survey_lifecycle`）建立並發布固定 UUID 的問卷，cloud 模式 `--inbox` 以 `accept_submission(user=None)` 送出帶「飲料店模擬填答」標記的模擬填答。網站 `SurveyCreateView` 在原型開關開啟時，於同一交易建立草稿並 `assign_survey_to_node`。

**Tech Stack:** Django 5、既有 cloudapi／cloudsync、`cloudsync.testing.CloudServer`（雙隔離 SQLite 端到端）。

**Spec:** [節點唯一分析規格](../specs/2026-10-04-node-only-analysis-design.md) 第 1 節（明文過渡限制）、第 4 節、第 5 節；[雲端同步規格](../specs/2026-10-01-cloud-sync-design.md) 第 10–12 節。

## Global Constraints

- 明文收件匣只適用於模擬／自測資料，「另以伺服器端明確允許的問卷／資料模式限制收件，不能僅依全站 `CLOUD_INBOX_ENABLED`」。不在允許範圍的節點問卷：不能發布、填答頁明確拒絕，不回退成雲端本地寫入。
- 模擬填答的填答者名稱一律以 `飲料店模擬填答` 開頭（`NAME_PREFIX`），在節點的 `FeedbackSubmission.respondent_name` 上可見。
- 關鍵字分類（`KeywordCategory`）只在節點建立。
- 不新增 migration。不刪除、不封存舊飲料店問卷；`seed_demo_beverage` 不帶新旗標時行為不變（`--reset`／`--cleanup`／預設路徑與現有測試照舊）。
- 同一 `--seed` 產生的模擬答案與現行預設路徑相同（同一個產生函式），飲料店仍須產出七種統計方法。
- 正式操作（Render 環境變數、正式 DB、`--inbox` 對正式雲端、清除舊問卷、Gemini）不在本計畫內，各自另行授權。
- 分支 `feat/beverage-inbox`，從 `main` 開；PR #31 未合併且不碰收件匣與 seed，合併衝突只可能在文件。
- 測試：cloud 用 `.venv/Scripts/python.exe manage.py test <label> --settings=config.settings_test`；node 前綴 `DEPLOYMENT_MODE=node FEEDBACK_HUB_NODE_HOME=<暫存目錄>`。CI 的 cloud job 不跑 `cloudsync`、`node`。

## Review Focus

1. 同一 `--seed` 重跑 `--inbox`：不得重複收件，全部視為重送（`reused`）。→ Task 3 `test_inbox_rerun_with_same_seed_is_a_resend`
2. `--inbox` 對不在允許範圍、未發布或不存在的問卷：明確錯誤且不寫入任何收據。→ Task 3 `test_inbox_refuses_survey_outside_self_test_scope`
3. `--node-create` 在節點未連結雲端時：明確錯誤，本機不留下半成品問卷或關鍵字。→ Task 2 `test_node_create_requires_cloud_link`
4. 網站建立問卷時有兩個啟用中節點：擋下且不建立草稿；已撤銷的節點不算啟用。→ Task 4 `test_two_active_nodes_block_creation`、`test_revoked_node_is_not_counted`
5. 自測允許清單之外的節點問卷在收件匣開啟後嘗試發布：回 422（`PublishBlocked`），不設定 `published_version`。→ Task 1 `test_publish_outside_scope_is_blocked`

---

### Task 1: 收件匣的自測範圍限制（雲端）

**Files:**
- Modify: `config/settings.py`（`CLOUD_INBOX_*` 區塊）、`config/settings_test.py`
- Modify: `cloudapi/inbox.py`、`cloudapi/writes.py:103-107`（`_lifecycle` 的發布檢查）、`feedback/views.py`（填答頁 `post` 中 `uses_inbox` 分支，約 969 行）
- Modify: `cloudsync/testing.py`（`CloudServer`）
- Test: `cloudapi/tests/test_inbox_scope.py`

**Interfaces:**
- Produces:
  - `settings.CLOUD_INBOX_REQUIRE_SELF_TEST: bool`：環境變數 `CLOUD_INBOX_REQUIRE_SELF_TEST`，預設 `True`（只有字串 `false` 不分大小寫為 False）；`settings_test.py` 設為 `False`，讓既有收件匣測試不受影響。
  - `settings.CLOUD_INBOX_SELF_TEST_SURVEYS: frozenset[str]`：環境變數 `CLOUD_INBOX_SELF_TEST_SURVEYS`，逗號分隔的問卷 UUID，去空白、轉小寫；未設定為空集合。
  - `cloudapi.inbox.inbox_scope_allows(survey) -> bool`：`not REQUIRE_SELF_TEST or str(survey.uuid) in SELF_TEST_SURVEYS`。
  - `cloudapi.inbox.uses_inbox(survey)`：原條件再加上 `inbox_scope_allows(survey)`。
  - `CloudServer(workdir, *, startup_timeout=60, inbox=False, self_test_surveys=None)`：`None` 時子程序 env 設 `CLOUD_INBOX_REQUIRE_SELF_TEST=False`；給清單時設 `True` 並以逗號寫入 `CLOUD_INBOX_SELF_TEST_SURVEYS`。

- [ ] **Step 1: 寫失敗的測試** `cloudapi/tests/test_inbox_scope.py`，類別 `@override_settings(CLOUD_INBOX_ENABLED=True, CLOUD_INBOX_REQUIRE_SELF_TEST=True)`，fixture 仿 `test_inbox_accept.AcceptTests.setUp`（節點問卷、已發布、`inbox_since` 已設）：
  - `test_survey_outside_scope_does_not_use_inbox`：`CLOUD_INBOX_SELF_TEST_SURVEYS=frozenset()` 時 `uses_inbox(survey)` 為 False；設為 `{str(survey.uuid)}` 時為 True。
  - `test_accept_outside_scope_raises_survey_closed`：清單為空時 `accept_submission(...)` 拋 `SurveyClosed`，且 `SubmissionReceipt`、`InboxSubmission` 皆為 0 筆。
  - `test_publish_outside_scope_is_blocked`：節點擁有的草稿（`create_node_survey` 建立）加一題後以 `change_definition(..., definition={..., "published": True})` 發布，拋 `PublishBlocked`，`published_version` 仍為 None；清單含其 UUID 時發布成功。
  - `test_fill_page_refuses_node_survey_outside_scope`：清單為空，已登入顧客 POST 填答頁，回應含 `SurveyClosed.user_message`，`FeedbackSubmission` 與 `SubmissionReceipt` 都是 0 筆（不回退成雲端本地寫入）。
  - `test_settings_parse_comma_separated_uuids`：`config.settings.parse_uuid_list(" A ,b,, ") == frozenset({"a", "b"})`，`parse_uuid_list("") == frozenset()`（settings.py 用這個純函式產生設定值，測試不需重新載入設定）。

- [ ] **Step 2: 確認失敗**
  Run: `.venv/Scripts/python.exe manage.py test cloudapi.tests.test_inbox_scope --settings=config.settings_test`
  Expected: ImportError（`inbox_scope_allows`／`parse_uuid_list` 不存在）或斷言失敗。

- [ ] **Step 3: 實作**
  - `config/settings.py`：`parse_uuid_list(raw: str) -> frozenset[str]` 與兩個設定。
  - `cloudapi/inbox.py`：`inbox_scope_allows`；`uses_inbox` 加條件；`accept_submission` 在「只有新回覆才檢查」區段（`survey.accepts_responses` 旁）加 `if not inbox_scope_allows(survey): raise SurveyClosed()`。重送判斷維持在前面，已成功的重送仍回成功。
  - `cloudapi/writes.py` `_lifecycle`：`if survey.owner_node_id and not (settings.CLOUD_INBOX_ENABLED and inbox_scope_allows(survey)): raise PublishBlocked()`。
  - `feedback/views.py` 填答頁 POST：在 `if uses_inbox(self.object)` 之前，`self.object.owner_node_id` 已設但 `uses_inbox` 為 False 時，回傳 `self._notice(SurveyClosed.user_message, "error")`。
  - `cloudsync/testing.py`：`self_test_surveys` 參數。
  - `.env.example`（若存在）或 README 的環境變數表加入兩個設定與一句說明：「明文收件匣只限自測問卷；接入真實顧客回覆前須完成加密或另行批准。」

- [ ] **Step 4: 確認通過，並跑既有收件匣測試**
  Run: `.venv/Scripts/python.exe manage.py test cloudapi feedback --settings=config.settings_test`
  Expected: OK。

- [ ] **Step 5: Commit**
  `git commit -m "feat(cloudapi): limit the plaintext inbox to server-allowed self-test surveys"`

---

### Task 2: `seed_demo_beverage --node-create`（node 模式）

**Files:**
- Modify: `feedback/seed_support.py`、`feedback/management/commands/seed_demo_beverage.py`
- Test: `cloudsync/tests/test_seed_beverage_node.py`（node 模式才會被 CI 收進來）

**Interfaces:**
- Consumes: `feedback.survey_lifecycle.create_draft(definition_with_questions)`、`feedback.survey_lifecycle.commit(survey, definition, expected_version)`、`cloudapi.definition.serialize_definition`；Task 1 的 `CloudServer(self_test_surveys=...)`。
- Produces:
  - `seed_support.survey_definition(*, survey_uuid, title, questions, description="", category=None, thank_you_email_enabled=False) -> dict`：`blank_definition` 加上逐題 `add_question`（order 從 1 起）。`create_published_survey` 改用它，行為不變。
  - `seed_support.create_published_node_survey(*, survey_uuid, title, questions, description="", category=None, keywords=()) -> Survey`：本機已有同 UUID 的問卷時拋 `CommandError("問卷已存在…")`；否則 `create_draft(survey_definition(...))` → `commit(..., published=True)` → 建 `KeywordCategory`（threshold=2）→ 回傳重新讀取的 Survey。
  - `seed_demo_beverage.BEVERAGE_NODE_SURVEY_UUID = uuid.uuid5(uuid.NAMESPACE_URL, "https://feedbackhub.local/demo/beverage-node")`
  - `seed_demo_beverage.simulated_responses(rng: random.Random, count: int) -> list[tuple[int, dict[str, object], bool]]`：`(序號, {題目標題: 值}, consent_follow_up)`，由現行 `_seed_responses` 迴圈抽出，亂數呼叫順序不變；預設路徑改用它。
  - 旗標 `--node-create`：只允許 `settings.IS_NODE`（否則 `CommandError("--node-create 只能在本機節點模式執行")`）；不可與 `--inbox`、`--reset`、`--cleanup` 併用；只建立並發布問卷與關鍵字，不灌填答。輸出問卷 UUID 與 slug。

- [ ] **Step 1: 寫失敗的測試**（類別加 `@node_only`；需要雲端的測試共用一個 `CloudServer(workdir, inbox=True, self_test_surveys=[str(BEVERAGE_NODE_SURVEY_UUID)])`，setUp 照 `test_e2e_inbox` 連結節點與 loopback）
  - `test_node_create_publishes_owned_survey_with_keywords`：執行 `call_command("seed_demo_beverage", "--node-create")`；本機 `Survey.objects.get(uuid=BEVERAGE_NODE_SURVEY_UUID)` 的 `published_version` 不為 None、題目 10 題且題型順序同 `test_beverage_seed_question_set`、`KeywordCategory` 6 筆；雲端 `self.cloud.shell(...)` 查得同 UUID 問卷 `owner_node` 為 e2e 節點且已發布。
  - `test_node_create_requires_cloud_link`：`CloudLink.unlink()` 後執行，拋 `CommandError`（訊息含「尚未連結雲端」），本機沒有該 UUID 的 Survey、`KeywordCategory` 0 筆。
  - `test_node_create_twice_refuses`：第二次拋含「已存在」的 `CommandError`。
  - `test_node_create_refused_in_cloud_mode`：放在 `feedback/test_seed_commands.py`、`@cloud_only`，拋含「本機節點模式」的 `CommandError`。
  - `test_default_path_answers_unchanged`：放在 `feedback/test_seed_commands.py`，`simulated_responses(random.Random(7), 100)` 第 1 筆的值等於重構前以 seed 7 灌入的第 1 筆（先在重構前跑一次記下 `門市`、`整體滿意度`、`等候分鐘數` 三個值，寫死進斷言）。

- [ ] **Step 2: 確認失敗**
  Run（node）: `DEPLOYMENT_MODE=node FEEDBACK_HUB_NODE_HOME=<tmp> .venv/Scripts/python.exe manage.py test cloudsync.tests.test_seed_beverage_node --settings=config.settings_test`
  Expected: ImportError（`BEVERAGE_NODE_SURVEY_UUID` 不存在）。

- [ ] **Step 3: 實作** 上列 Produces。`create_published_node_survey` 在 `NotLinkedError`／`DefinitionCommitError` 時轉成 `CommandError(exc.user_message)`；草稿建立後若發布失敗，不在本機另做清除（本機副本來自雲端回覆，雲端草稿留著可重跑——第二次執行會因「已存在」停下，訊息提示到節點主控台處理）。

- [ ] **Step 4: 確認通過**
  Run: node 指令同 Step 2，加上 `feedback.test_seed_commands`；cloud: `.venv/Scripts/python.exe manage.py test feedback.test_seed_commands --settings=config.settings_test`
  Expected: OK。

- [ ] **Step 5: Commit** `git commit -m "feat(seed): create the beverage demo as a node-owned survey"`

---

### Task 3: `seed_demo_beverage --inbox`（cloud 模式）與端到端驗收

**Files:**
- Modify: `cloudapi/inbox.py`（`accept_submission` 的模擬名稱）、`feedback/seed_support.py`、`feedback/management/commands/seed_demo_beverage.py`
- Test: `cloudapi/tests/test_seed_beverage_inbox.py`、`cloudsync/tests/test_seed_beverage_node.py`（加端到端）

**Interfaces:**
- Consumes: Task 1 `inbox_scope_allows`；Task 2 `BEVERAGE_NODE_SURVEY_UUID`、`simulated_responses`。
- Produces:
  - `accept_submission(survey, *, user, submission_uuid, form_version, consent_follow_up, answers, simulated_name="")`：`simulated_name` 只在 `user is None` 時寫入封套的 `name`；`user` 不為 None 且給了 `simulated_name` 拋 `ValueError`。不改 `payload_hash` 的輸入。
  - `seed_support.inbox_answers(survey, values: dict[str, object]) -> dict[str, object]`：鍵為 `str(question.uuid)`；單選回單一代碼字串、複選回代碼清單（標籤轉代碼重用 `feedback.local_service._encode_answer`）、其他題型 `str(value)`；`None` 略過。
  - 旗標 `--inbox`：只允許 cloud 模式；必須同時給 `--seed`（否則 `CommandError("--inbox 需要 --seed，重跑才不會重複收件")`）；不可與 `--node-create`、`--reset`、`--cleanup` 併用。找 `uuid=BEVERAGE_NODE_SURVEY_UUID` 的問卷，任一不成立就 `CommandError` 並不寫入：存在、`published_version` 不為 None、`uses_inbox(survey)` 為 True。逐筆 `accept_submission(survey, user=None, submission_uuid=uuid.uuid5(BEVERAGE_NODE_SURVEY_UUID, f"{seed}:{i}"), form_version=survey.published_version, consent_follow_up=consent, answers=inbox_answers(...), simulated_name=f"{NAME_PREFIX} #{i}")`。輸出新收件與重送筆數。

- [ ] **Step 1: 寫失敗的測試**
  - `cloudapi/tests/test_seed_beverage_inbox.py`（`@cloud_only`，`override_settings(CLOUD_INBOX_ENABLED=True, CLOUD_INBOX_REQUIRE_SELF_TEST=True, CLOUD_INBOX_SELF_TEST_SURVEYS=frozenset({str(BEVERAGE_NODE_SURVEY_UUID)}))`；fixture：以 `create_node_survey(node, survey_definition(survey_uuid=BEVERAGE_NODE_SURVEY_UUID, title=SURVEY_TITLE, questions=QUESTIONS))` 建立後 `change_definition(..., published=True)`）：
    - `test_inbox_writes_labelled_receipts_only`：`--inbox --count 8 --seed 7` 後 `SubmissionReceipt` 8 筆、`InboxSubmission` 8 筆且每筆 `envelope["respondent"]["name"]` 以 `NAME_PREFIX` 開頭、`FeedbackSubmission` 0 筆。
    - `test_inbox_rerun_with_same_seed_is_a_resend`：連跑兩次，收據仍 8 筆，第二次輸出含「重送 8」。
    - `test_inbox_refuses_survey_outside_self_test_scope`：允許清單為空集合時拋 `CommandError`，收據 0 筆；問卷不存在時同樣。
    - `test_inbox_requires_seed`：缺 `--seed` 拋含「--seed」的 `CommandError`。
    - `test_simulated_name_only_without_user`：`accept_submission(..., user=<顧客>, simulated_name="x")` 拋 `ValueError`。
  - `cloudsync/tests/test_seed_beverage_node.py` 加 `test_beverage_through_inbox_end_to_end`：節點 `--node-create` → `self.cloud._manage("seed_demo_beverage", "--inbox", "--count", "12", "--seed", "7")` → `run_cycle(force=True) == "ok"` → 本機該問卷 `FeedbackSubmission` 12 筆、名稱皆以 `NAME_PREFIX` 開頭、複選題 `Answer.choice_codes` 有值、有待執行的 `AnalysisJob`；雲端收據狀態皆 `synced`。

- [ ] **Step 2: 確認失敗**
  Run: `.venv/Scripts/python.exe manage.py test cloudapi.tests.test_seed_beverage_inbox --settings=config.settings_test`
  Expected: `CommandError: unrecognized arguments: --inbox` 或 `TypeError`（`simulated_name`）。

- [ ] **Step 3: 實作** 上列 Produces。

- [ ] **Step 4: 確認通過**
  Run: cloud `cloudapi.tests.test_seed_beverage_inbox feedback.test_seed_commands cloudapi.tests.test_inbox_accept`；node `cloudsync.tests.test_seed_beverage_node cloudsync.tests.test_e2e_inbox`
  Expected: OK。

- [ ] **Step 5: Commit** `git commit -m "feat(seed): send labelled beverage simulations through the cloud inbox"`

---

### Task 4: 網站新建問卷自動指派節點（cloud 模式）

**Files:**
- Modify: `feedback/views.py`（`SurveyCreateView.form_valid`／`get_context_data`、`SurveyBuilderView.get_context_data`）、`templates/feedback/survey_create.html`、`templates/feedback/survey_builder.html`
- Test: `feedback/test_survey_create_node.py`

**Interfaces:**
- Consumes: `cloudapi.writes.assign_survey_to_node(survey, node)`、`NodeDevice.Status.ACTIVE`。
- Produces:
  - `feedback.views.NODE_MISSING_NOTICE = "尚未連接本機節點，發布後不會產生分析"`
  - `feedback.views.active_nodes() -> QuerySet[NodeDevice]`：`settings.IS_NODE` 或 `not settings.CLOUD_SYNC_PROTOTYPE_ENABLED` 時回 `NodeDevice.objects.none()`；否則 `status=ACTIVE`。
  - 建立頁與編輯頁 context `node_notice: str`：原型開關開啟、cloud 模式、問卷沒有 `owner_node`、且沒有啟用中節點時為 `NODE_MISSING_NOTICE`，否則 `""`。模板以頁面既有的提示樣式顯示（沿用 builder 既有 alert markup，不新增全站樣式）。

- [ ] **Step 1: 寫失敗的測試**（`@cloud_only`、`override_settings(CLOUD_SYNC_PROTOTYPE_ENABLED=True)`、manager 登入，POST `feedback:survey-create` 只帶 `title`）
  - `test_one_active_node_owns_new_draft`：一個 `NodeDevice.issue("office")`；新問卷 `owner_node` 為該節點、`published_version` 為 None，`SurveyDefinitionRevision` 最新版本的 `definition_version` 等於問卷版本。
  - `test_no_node_creates_unowned_draft_with_notice`：沒有節點；草稿建立、`owner_node` 為 None；GET 建立頁與該草稿編輯頁都含 `NODE_MISSING_NOTICE`。
  - `test_two_active_nodes_block_creation`：兩個啟用節點；回應含「多個本機節點」，`Survey` 數量不變。
  - `test_revoked_node_is_not_counted`：一啟用、一已撤銷 → 指派給啟用的那個。
  - `test_prototype_off_keeps_current_behaviour`：`CLOUD_SYNC_PROTOTYPE_ENABLED=False` 且有一個節點 → `owner_node` 為 None、頁面不含提示。

- [ ] **Step 2: 確認失敗**
  Run: `.venv/Scripts/python.exe manage.py test feedback.test_survey_create_node --settings=config.settings_test`
  Expected: 斷言失敗（`owner_node` 為 None）。

- [ ] **Step 3: 實作** cloud 分支：先數 `active_nodes()`；多於 1 個時 `messages.error("已連接多個本機節點，請先在後台撤銷不用的節點再建立問卷")` 並 `form_invalid`；否則在 `transaction.atomic()` 內 `create_draft(...)`，恰好 1 個時接著 `self.object = assign_survey_to_node(self.object, node).survey`。

- [ ] **Step 4: 確認通過**
  Run: `.venv/Scripts/python.exe manage.py test feedback cloudapi --settings=config.settings_test`
  Expected: OK。

- [ ] **Step 5: Commit** `git commit -m "feat(builder): new website drafts belong to the connected node"`

---

### Task 5: 文件與完整驗證

**Files:**
- Modify: `docs/next-actions.md`（問卷與同步段落、待辦）、`docs/superpowers/specs/2026-10-04-node-only-analysis-design.md` 第 7 節第 3、8 步（寫明兩個環境變數與 `--node-create`／`--inbox` 指令）

- [ ] **Step 1: 更新文件** next-actions 只寫目前狀態（依「文件只記現況」）：第 4、5 節程式已完成未部署；部署後的操作順序（設 `CLOUD_INBOX_SELF_TEST_SURVEYS=<BEVERAGE_NODE_SURVEY_UUID>` → 開 `CLOUD_INBOX_ENABLED` → 節點 `--node-create` → 雲端 `--inbox --count 100 --seed 7` → 同步／分析／展示驗收 → 封存舊問卷），每步另行授權。
- [ ] **Step 2: 完整驗證**
  Run: cloud `manage.py test feedback accounts config cloudapi --settings=config.settings_test`；node `manage.py test feedback accounts config node organizations cloudapi cloudsync --settings=config.settings_test`；`git diff --check`
  Expected: 兩組 OK、diff check 無輸出。
- [ ] **Step 3: Commit** `git commit -m "docs: beverage inbox path and node-owned drafts"`
