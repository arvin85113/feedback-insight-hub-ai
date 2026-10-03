# 問卷建立工具改版 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 依規格 v4.2 把問卷建立工具改成 Google 表單式卡片編輯、草稿／發布生命週期、由題型推得資料型態，並讓選項代碼、分數與「不納入分析」一路貫穿填答、同步與統計。

**Architecture:** 題型規則集中在新模組 `feedback/question_schema.py`（推型態、選項正規化、分析清單），模型與定義 dict 都經由它。
**所有問卷**（不只節點問卷）改走同一條寫入路徑：編輯 dict → `cloudapi.writes.change_definition`（雲端）或 `cloudsync.survey_write.node_commit`（本機），
每個版本都存 revision，`published_version` 固定發布時的版本；狀態轉換、重算、回答合約與刪除關聯以規格 **§7「流程與合約」**為準。統計端以題目物件上的 `analysis_options`／`analysis_excluded_options` 取得清單，ORM 與 Worker 兩條路徑相同。

**Tech Stack:** Django 6.0.8、pandas／SciPy（既有）、原生 JS（無框架）、SQLite（測試）、PostgreSQL 17（併發測試）。

**Spec:** [問卷建立工具改版 v4.2](../specs/2026-10-03-survey-builder-redesign-design.md)（**§7 流程與合約優先**）；相關：[雲端同步](../specs/2026-10-01-cloud-sync-design.md)（開頭的取代說明）、[purge_survey 計畫](2026-10-03-purge-survey.md)（**前置，須先合併**）。

## Global Constraints

- 前置：`feedback/survey_purge.py` 的 `purge_survey(survey, *, dry_run=False)` 已存在（purge 計畫）。
- **移除或改名任何介面時，同一個 task 內一併修改所有呼叫端與相關測試**（包括行為已改變的斷言，不只補 fixture）；每個 task 結束時，雲端與本機兩種模式的完整測試都必須通過。
- 分析重算只依規格 §7.2：定義寫入只儲存實際變動的欄位與題目；草稿不排程分析；不把任何 `PROTECT` 改成 `CASCADE`。
- 測試指令：雲端 `.venv/Scripts/python.exe manage.py test <labels> --settings=config.settings_test`；
  本機模式另以 `DEPLOYMENT_MODE=node` 執行相同指令（CI 兩個 job 都跑）。雲端專用測試加 `feedback.test_utils.cloud_only`。
- 每個 task 結束前 `makemigrations --check --dry-run --settings=config.settings_test` 必須為 `No changes detected`（Task 1、2 自己產生的 migration 除外）。
- 統計方法七種全部保留（Welch t、ANOVA、卡方、Mann-Whitney U、Kruskal-Wallis、Pearson、Spearman）；整數題維持描述統計。
- 題型對應（spec §1）：簡答 `short_text`／段落 `long_text` → `text`；單選 `single_choice`+`display=radio`、下拉 `single_choice`+`display=dropdown` → `nominal`（`ordered` 時 `ordinal`）；
  核取方塊 `multiple_choice` → `nominal`；線性刻度 `scale` → `ordinal`；數字 `integer` → `discrete`、`decimal` → `continuous`。
- 刻度：`scale_min` ∈ {0, 1}，`scale_max` ∈ 2–10，預設 1–5；只有兩端標籤 `scale_min_label`／`scale_max_label`（各 ≤ 40 字，選填）。
- 選項：`choices` 項目為 `{"code","label","excluded","score"}`；代碼 `c<n>`、題目代碼 `q<n>` 由計數器產生、不回收；有序題分數從 `score_start`（0 或 1，預設 1）起依清單順序由低到高；
  `excluded` 只用於單選／下拉；有序題至少 2 個未排除選項、名目題至少 1 個；同題選項文字不可重複；選項文字 ≤ 200 字。
- 發布後白名單（spec §4.2）：`is_active`、`archived_at`、`category`、`analysis_enabled`、`thank_you_email_enabled`、`improvement_tracking_enabled`。
- 使用者可見文字（逐字）：「問卷尚未開放」、「此問卷已在其他視窗修改」、「重新載入（捨棄我的修改）」、「繼續編輯」、「不納入分析」、
  「允許小數的數字題可做平均數比較與相關分析」、「問卷已發布，題目不能修改；請複製為新草稿」、「收件匣尚未開啟，無法發布指派節點的問卷」、「複製為新草稿」、「發布」。
- 不呼叫 Gemini、不寫任何非測試 DB；`seed_demo_beverage` 等指令只在測試中執行（AGENTS.md）。
- 每個 task 結束 commit；全部完成後 push 並開 PR（使用者偏好），不合併。

## Review Focus

1. **舊 schema v1 revision 讀入後再次寫回**（節點從雲端拿到舊版本後編輯）：轉換結果與雲端一致，不產生第二組代碼——Task 4 `test_v1_revision_applied_on_node_then_edited_writes_v2`。
2. **選項文字含逗號、或與排除選項同名的空白差異**（「不適用 」）：選擇題一律以代碼對應，不靠文字切分或比對——Task 8 `test_comma_label_multi_choice_round_trips`、Task 7 `test_choice_value_is_code_not_label`。
3. **停止收件再恢復時已開啟的表單**：仍以 `published_version` 核對成功送出——Task 10 `test_whitelist_change_does_not_reject_open_form`。
4. **刪除最後一個選項後新增**：不重用代碼——Task 1 `test_choice_code_counter_never_reuses`。
5. **TripAdvisor 轉換前後**：已發布統計、文字、AI 判定仍為最新——Task 3 `test_tripadvisor_published_results_stay_current_after_migration`、Task 9。

---

### Task 1: 題型規則模組與 schema 欄位

**Files:**
- Create: `feedback/question_schema.py`
- Modify: `feedback/models.py`（`Survey`、`Question`、`FeedbackSubmission`、`Answer`）
- Create: `feedback/migrations/0023_survey_builder_fields.py`（`makemigrations` 產生，只新增欄位）
- Test: `feedback/test_question_schema.py`

**Interfaces:**
- Produces（`feedback/question_schema.py`）：
  - `derive_data_type(kind: str, *, ordered: bool) -> str`
  - `UI_TYPES = ("short_text", "long_text", "radio", "dropdown", "checkbox", "scale", "number")`；`ui_type_of(kind, display) -> str`；
    `kind_display_for(ui_type: str, *, allow_decimal: bool) -> tuple[str, str]`（`number` → `integer`／`decimal`，非選擇題 display 為 `""`）
  - `normalize_question(item: dict) -> dict`：補代碼（用 `item["next_choice_number"]`）、算分數、推 `data_type`、非文字題 `enable_keyword_tracking=False`、
    非相關題型清空 `choices`／刻度欄位、`options_text` 為 `"\n".join(label)`（刻度為範圍數字），回傳新 dict
  - `question_errors(item: dict) -> dict[str, str]`（欄位名 → 錯誤訊息；空 dict 代表合法）
  - `analysis_levels(question) -> list[str]`、`analysis_excluded(question) -> list[str]`（接受有屬性的物件）
  - `CHOICE_KINDS = {"single_choice", "multiple_choice"}`
  - `fields_from_legacy(kind, data_type, options_text) -> dict`：舊格式 → 新欄位（spec §5 第 3 步：選擇題 `choices`、`ordered` 取自 `data_type`、分數從 1；整數連續選項的刻度 → `scale_min`／`scale_max`；文字選項的刻度 → `single_choice`＋`ordered`＋`display=radio`；無選項刻度 → 1–5）。Task 3 轉換與 `Question.save()` 相容路徑共用
- Produces（模型）：`Survey.published_version: int|None`、`Survey.published_at: datetime|None`、`Survey.next_question_number: int = 1`、
  `Survey.is_published` property、`Survey.accepts_responses`（加上 `is_published`）；
  `Question.display: str = ""`、`ordered: bool = False`、`score_start: int = 1`、`scale_min: int|None`、`scale_max: int|None`、`scale_min_label: str = ""`、`scale_max_label: str = ""`、
  `choices: list = []`（JSONField）、`next_choice_number: int = 1`；`Question.analysis_options`、`Question.analysis_excluded_options` property
  （有 `_analysis_options`／`_analysis_excluded` 時回傳它，否則由 `analysis_levels`／`analysis_excluded` 計算）；`Question.options` 改回傳 `analysis_options`；
  `Question.save()`：無代碼時以 `allocate_question_code(survey)` 取 `q<n>`（鎖 `Survey` 列遞增 `next_question_number`；若該代碼已存在則繼續遞增），`choices` 為空且題型需要時，先以 `fields_from_legacy` 由 `options_text` 補齊（相容路徑，讓 Task 12 前的既有呼叫端與測試照常運作），再以 `normalize_question` 同步 `data_type`、`options_text`；
  `FeedbackSubmission.definition_version: int|None`；`Answer.choice_codes: list|None`。

- [ ] **Step 1: 寫失敗測試** `feedback/test_question_schema.py`：

```python
def test_derive_data_type_table(self):
    cases = {("short_text", False): "text", ("long_text", False): "text", ("single_choice", False): "nominal",
             ("single_choice", True): "ordinal", ("multiple_choice", False): "nominal", ("scale", False): "ordinal",
             ("integer", False): "discrete", ("decimal", False): "continuous"}
    for (kind, ordered), expected in cases.items():
        self.assertEqual(derive_data_type(kind, ordered=ordered), expected)

def test_scores_follow_order_and_skip_excluded(self):
    item = normalize_question(choice_item(["很快", "普通", "很久", "不適用"], ordered=True, score_start=0, excluded={"不適用"}))
    self.assertEqual([c["score"] for c in item["choices"]], [0, 1, 2, None])
    self.assertEqual([c["code"] for c in item["choices"]], ["c1", "c2", "c3", "c4"])
    self.assertEqual(item["next_choice_number"], 5)

def test_choice_code_counter_never_reuses(self):
    item = normalize_question(choice_item(["A", "B"]))
    item["choices"].pop()                       # 刪掉 c2
    item["choices"].append({"code": "", "label": "C", "excluded": False, "score": None})
    self.assertEqual(normalize_question(item)["choices"][-1]["code"], "c3")

def test_errors(self):
    self.assertIn("choices", question_errors(choice_item(["A", "不適用"], ordered=True, excluded={"不適用"})))  # 有序題少於 2 個未排除
    self.assertIn("choices", question_errors(choice_item(["A", "A"])))                                         # 重複文字
    self.assertIn("choices", question_errors(choice_item(["A"], kind="multiple_choice", excluded={"A"})))      # 複選不可排除
    self.assertIn("scale_min", question_errors(scale_item(2, 5)))
    self.assertIn("scale_max", question_errors(scale_item(1, 11)))

def test_non_text_keyword_tracking_forced_off(self):
    self.assertFalse(normalize_question({**scale_item(1, 5), "enable_keyword_tracking": True})["enable_keyword_tracking"])

def test_analysis_levels(self):
    q = Question(kind="scale", scale_min=0, scale_max=3)
    self.assertEqual(analysis_levels(q), ["0", "1", "2", "3"])
    q = Question(kind="single_choice", ordered=True, choices=[ch("c1", "低", 1), ch("c2", "不適用", None, True), ch("c3", "高", 2)])
    self.assertEqual((analysis_levels(q), analysis_excluded(q)), (["低", "高"], ["不適用"]))

def test_question_code_allocated_from_counter(self):
    survey = Survey.objects.create(title="S", slug="s")
    first = Question.objects.create(survey=survey, title="中文題名", kind="short_text")
    second = Question.objects.create(survey=survey, title="中文題名", kind="short_text")
    self.assertEqual((first.code, second.code), ("q1", "q2"))
    self.assertEqual(first.data_type, "text")

def test_draft_does_not_accept_responses(self):
    survey = Survey.objects.create(title="S", slug="s")
    self.assertFalse(survey.accepts_responses)
    survey.published_version = 1
    self.assertTrue(survey.accepts_responses)
```

  （`choice_item`、`scale_item`、`ch` 為測試檔內的小 helper，產生 spec 的 item dict。）

- [ ] **Step 2: 執行確認失敗**：`.venv/Scripts/python.exe manage.py test feedback.test_question_schema --settings=config.settings_test` → ImportError。
- [ ] **Step 3: 實作 `feedback/question_schema.py`**（純函式，不碰 ORM，除了 `allocate_question_code`）。
- [ ] **Step 4: 修改 `feedback/models.py`**：新增欄位與 property；`Question.clean()` 改為呼叫 `question_errors`（錯誤鍵沿用欄位名）；
  移除 `save()` 內的 slugify 代碼產生。`makemigrations feedback --name survey_builder_fields --settings=config.settings_test` 產生 0023（只有 AddField）。
- [ ] **Step 5: 執行測試** → PASS；再跑雲端與本機兩種模式的完整測試。`accepts_responses` 改變會讓既有填答相關測試失敗：新增測試 helper `feedback.test_utils.published(survey)`（設 `published_version=definition_version or 1` 並以 `update()` 寫入），在這些測試的 fixture 呼叫它，直到全綠。
- [ ] **Step 6: Commit** `feat: question schema module and survey builder fields`

---

### Task 2: 定義 dict v2（雲端與本機共用）

**Files:**
- Modify: `cloudapi/definition.py`
- Test: `cloudapi/tests/test_definition.py`

**Interfaces:**
- Consumes: Task 1 `normalize_question`、`question_errors`、`derive_data_type`。
- Produces:
  - `SCHEMA_VERSION = 2`；`serialize_definition(survey) -> dict`：加入 `schema_version`、`published`（bool）、`published_version`、`published_at`、`next_question_number`；
    題目加入 `display`、`ordered`、`score_start`、`scale_min`、`scale_max`、`scale_min_label`、`scale_max_label`、`choices`、`next_choice_number`，移除 `options_text`
  - `definition_from_fields(survey, questions, category_name) -> dict`：只讀欄位、不用 property 的序列化（歷史模型可用），`serialize_definition` 改為呼叫它
  - `upgrade_v1(definition: dict) -> dict`（spec §4.3 的固定規則；無 `schema_version` 視為 1；`published=True`）
  - `validate_definition(definition) -> dict`：**回傳**升級後的新 dict（不修改輸入）；呼叫端一律使用回傳值（`apply_definition`、`cloudsync.definitions.upsert_definition`、`change_definition`、`create_node_survey`）；`data_type` 必須等於 `derive_data_type`；題目錯誤取 `question_errors` 第一則轉 `DefinitionError`
  - `apply_definition(survey, definition, *, version)`：寫新欄位；`published_version`／`published_at` 只從 dict 複製（雲端在 Task 4 自行設定後再呼叫）；
    dict 中缺少的題目：未發布的問卷直接刪除，已發布者停用；**只儲存實際變動的欄位**：問卷以 `save(update_fields=<變動欄位>)`，題目內容完全相同者不儲存（規格 §7.2）
  - 編輯 helper（供 Task 6）：`add_question(definition, item) -> dict`、`update_question(definition, uuid, item)`、`delete_question(definition, uuid)`、
    `move_question(definition, uuid, direction)`（保留）、`update_survey(definition, data)`、`archive_survey(definition, when)`（保留）
  - 本 task **不移除** `SEMANTIC_FIELDS`、`set_question_active`（分別在 Task 4、Task 6 與其呼叫端一起移除）；`SEMANTIC_FIELDS` 暫時改為 `("kind", "data_type", "choices")`

- [ ] **Step 1: 寫失敗測試**：

```python
def test_serialize_round_trip_v2(self):
    d = serialize_definition(published_survey_with_all_kinds())
    self.assertEqual(d["schema_version"], 2)
    self.assertNotIn("options_text", d["questions"][0])
    validate_definition(d)

def test_v1_upgrade_is_deterministic_and_matches_cloud(self):
    v1 = V1_FIXTURE      # 舊格式：options_text「A\nB」、data_type ordinal、scale 選項 1–5
    first, second = upgrade_v1(copy.deepcopy(v1)), upgrade_v1(copy.deepcopy(v1))
    self.assertEqual(first, second)
    self.assertEqual([c["code"] for c in first["questions"][0]["choices"]], ["c1", "c2"])
    self.assertEqual((first["questions"][1]["scale_min"], first["questions"][1]["scale_max"]), (1, 5))

def test_rejects_data_type_not_matching_kind(self):
    d = valid_v2(); d["questions"][0]["data_type"] = "continuous"
    with self.assertRaises(DefinitionError):
        validate_definition(d)

def test_missing_question_deleted_on_draft_but_deactivated_when_published(self): ...

def test_validate_returns_upgraded_copy(self):
    v1 = copy.deepcopy(V1_FIXTURE)
    upgraded = validate_definition(v1)
    self.assertEqual((v1.get("schema_version"), upgraded["schema_version"]), (None, 2))

def test_apply_saves_only_changed_rows(self):
    survey = published_survey_with_two_questions()
    d = serialize_definition(survey); d["is_active"] = False
    with mock.patch("feedback.signals.schedule_survey_analysis") as schedule:
        apply_definition(survey, d, version=survey.definition_version + 1)
    schedule.assert_not_called()          # 題目沒變不儲存；is_active 不在重算欄位內
```

- [ ] **Step 2–4: 確認失敗 → 實作 → 執行** `cloudapi.tests.test_definition`、`cloudsync.tests.test_definitions` → PASS（更新既有測試 fixture 為 v2 或依賴 `upgrade_v1`）。
- [ ] **Step 5: Commit** `feat: survey definition dict v2 with v1 upgrade`

---

### Task 3: 既有資料轉換 migration

**Files:**
- Create: `feedback/schema_conversion.py`
- Create: `feedback/migrations/0024_convert_survey_definitions.py`（`RunPython(convert, noop)`；dependencies 加 `("cloudapi", "0003_publishedresultrecord")`）
- Create: `feedback/management/commands/survey_conversion_report.py`（唯讀）
- Modify: `feedback/importing/service.py`（抽出 `mapping_compatibility_errors(mapping, survey) -> list[str]`，`_ensure_survey_and_questions` 改呼叫它；比對規則不變，Task 11 再更新）
- Test: `feedback/test_schema_conversion.py`

**Interfaces:**
- Consumes: Task 1 的 `fields_from_legacy`、`normalize_question`；Task 2 的 `definition_from_fields(survey, questions, category_name) -> dict`（Task 2 從 `serialize_definition` 抽出的純欄位序列化，**只讀欄位、不用任何 property**，`serialize_definition` 改為呼叫它）。
- Produces: `convert_definitions(apps, *, using="default") -> dict`（回傳轉換摘要；所有查詢使用 `apps.get_model(...)` 與 `using`）；`ConversionAborted(Exception)`（訊息列出問卷 slug 與原因）；管理指令 `survey_conversion_report`（唯讀：每份問卷的狀態、版本、revision 是否齊全、題目轉換結果、已發布結果是否仍為最新）。

- [ ] **Step 1: 寫失敗測試**。**以 `MigrationExecutor` 將測試資料庫退回 `feedback 0022`（含 `cloudapi 0003`），用該狀態的歷史模型建立舊格式資料，再前進到 0024**；不得用目前模型建立 fixture（Task 1 的 `Question.save()` 會先自動轉換而掩蓋問題）。下列 `old_*` helper 都以歷史模型建立：

```python
def test_choice_questions_get_codes_and_scores(self):
    q = old_question(kind="single_choice", data_type="ordinal", options_text="很快\n普通\n很久")
    convert_definitions(apps)
    q.refresh_from_db()
    self.assertEqual([(c["code"], c["label"], c["score"]) for c in q.choices], [("c1", "很快", 1), ("c2", "普通", 2), ("c3", "很久", 3)])
    self.assertTrue(q.ordered)

def test_integer_scale_becomes_range(self):
    q = old_question(kind="scale", data_type="ordinal", options_text="1\n2\n3\n4\n5")
    convert_definitions(apps)
    q.refresh_from_db()
    self.assertEqual((q.scale_min, q.scale_max, q.choices), (1, 5, []))

def test_question_counter_skips_existing_codes(self):
    survey = old_survey_with_codes(["q2", "overall-scale"])
    migrate_to_0024()
    self.assertEqual(new_survey_model().objects.get(pk=survey.pk).next_question_number, 3)

def test_existing_surveys_become_published_with_revision(self):
    survey = old_survey(definition_version=0)
    convert_definitions(apps)
    survey.refresh_from_db()
    self.assertEqual((survey.definition_version, survey.published_version), (1, 1))
    self.assertTrue(SurveyDefinitionRevision.objects.filter(survey=survey, version=1).exists())

def test_single_choice_answers_get_codes(self): ...   # Answer.value == "普通" → choice_codes == ["c2"]

def test_aborts_on_multiple_choice_answers(self):
    q = old_question(kind="multiple_choice", data_type="nominal", options_text="A\nB")
    old_answer(q, "A, B")
    with self.assertRaisesMessage(ConversionAborted, q.survey.slug):
        convert_definitions(apps)

def test_aborts_on_unmatched_single_choice_answer(self): ...   # value "其他" 不在選項 → ConversionAborted
def test_aborts_on_text_scale_with_answers(self): ...          # 刻度選項「非常滿意…」且有答案 → ConversionAborted，且資料庫完全未變（無部分轉換）
def test_text_scale_without_answers_becomes_ordered_choice(self): ...  # → single_choice、ordered、display=radio

def test_updated_at_untouched(self):
    survey = old_survey()
    before = Survey.objects.values_list("updated_at", flat=True).get(pk=survey.pk)
    convert_definitions(apps)
    self.assertEqual(Survey.objects.values_list("updated_at", flat=True).get(pk=survey.pk), before)

def test_tripadvisor_published_results_stay_current_after_migration(self):
    survey = old_tripadvisor_survey()                       # 依 tripadvisor_hotel_reviews.json 九題、外部資料來源與已發布狀態
    publish_fixture_results(survey)                         # 建立已發布的統計、文字與 AI Stage（manifest 與版本一致）
    migrate_to_0024()
    survey = Survey.objects.get(pk=survey.pk)
    state = survey.analysis_state
    for key in ("statistics", "text"):
        self.assertTrue(_stage_is_current(state, key), key)
    self.assertTrue(is_published_ai_stage_current(state.published_ai_stage))
    self.assertEqual(mapping_compatibility_errors(survey), [])

def test_conversion_report_is_read_only(self): ...          # 執行前後所有模型筆數與 updated_at 不變
```

- [ ] **Step 2: 執行確認失敗** → ImportError。
- [ ] **Step 3: 實作 `convert_definitions`**：先全面掃描防呆（spec §5 第 4 步，三種情況任一即 `ConversionAborted`，**不做部分轉換**），再以 `bulk_update`／`update()` 轉換：
  題目欄位一律用 Task 1 的 `fields_from_legacy`＋`normalize_question`（文字選項的刻度 → 有序單選，規格 §5 第 3 步）；`Survey.next_question_number`＝max(既有 `q<n>` 的 n, 0)＋1；`next_choice_number`、`Survey.next_question_number`（＝現有題數＋1）；
  `published_version`（`definition_version` 為 0 者兩者設 1）、`published_at=created_at`；以 `serialize_definition` 建立缺少的 revision；單選答案寫 `choice_codes`。
  題目 `code` 不改（既有與 mapping 代碼保留）。
- [ ] **Step 4: 建立 migration 0024** 呼叫 `convert_definitions(apps)`。
- [ ] **Step 5: 實作 `survey_conversion_report`**（只查詢，不寫入）。
- [ ] **Step 6: 執行測試** → PASS（TripAdvisor 測試中的 AI Stage 判定在 Task 9 完成前若失敗，以 `expectedFailure` 標記並在 Task 9 移除）。
- [ ] **Step 7: Commit** `feat: convert existing survey definitions to the builder schema`

---

### Task 4: 版本、發布與白名單（雲端寫入）

**Files:**
- Modify: `cloudapi/writes.py`、`cloudapi/errors.py`、`cloudapi/views.py:100-115`（錯誤對應）、`cloudsync/survey_write.py:_translate`、`cloudsync/client.py`（`SEMANTIC` 分類沿用給 422）
- Modify: `cloudapi/management/commands/assign_survey_node.py`、`enable_survey_inbox.py`、`feedback/analysis_jobs.py`（`schedule_survey_analysis`）
- Test: `cloudapi/tests/test_writes.py`、`cloudapi/tests/test_api.py`

**Interfaces:**
- Consumes: Task 2。
- Produces:
  - `errors.PublishedLocked(DefinitionCommitError)`：`user_message="問卷已發布，題目不能修改；請複製為新草稿"`，API 422 `{"error": "published_locked"}`
  - `errors.PublishBlocked(DefinitionCommitError)`：`user_message="收件匣尚未開啟，無法發布指派節點的問卷"`，API 422 `{"error": "publish_blocked"}`
  - `writes.record_version(survey) -> SurveyDefinitionRevision`（取代 `record_revision`；revision 一律建立，`SurveyChange` 只在 `owner_node_id` 有值時建立）
  - `writes.random_slug() -> str`（8 個小寫英數字，碰撞重抽）
  - `change_definition(survey_uuid, *, expected_version, definition)`：版本核對 → 若已發布，比對白名單外欄位與全部題目，有差異 `PublishedLocked` →
    若 `definition["published"]` 由假變真：有 `owner_node` 且 `not settings.CLOUD_INBOX_ENABLED` 時 `PublishBlocked`；問卷至少一題（否則 `DefinitionError("至少需要一題才能發布")`）；
    版本＋1、`published_version`＝新版本、`published_at=now`、有節點時 `inbox_since=now` → `apply_definition` → `record_version`
  - `create_node_survey`：草稿、`random_slug()`
  - `assign_survey_to_node(survey, node)`：已發布 `PublishedLocked`；不再回填 `has_received_answer`
  - 移除 `check_semantic_lock`、`SemanticLockViolation`、`SEMANTIC_FIELDS`，並在本 task 一併修改其所有呼叫端（`cloudapi/views.py`、`cloudsync/survey_write.py`、`cloudsync/client.py`）與測試
  - 雲端 API `POST surveys/`、`PUT surveys/<uuid>/` 只接受 `schema_version == 2`（否則 400 `{"error": "schema_version"}`）
  - `enable_survey_inbox`：一律 `CommandError("已改為發布時設定收件匣，請改用發布")`
  - `schedule_survey_analysis`：問卷未發布時回傳 `None`（規格 §7.2）

- [ ] **Step 1: 寫失敗測試**：

```python
def test_every_version_records_revision_change_only_for_node(self):
    plain = draft_survey()
    change_definition(plain.uuid, expected_version=plain.definition_version, definition=renamed(plain, "新"))
    self.assertTrue(SurveyDefinitionRevision.objects.filter(survey=plain, version=1).exists())
    self.assertFalse(SurveyChange.objects.filter(survey=plain).exists())

def test_publish_sets_published_version(self):
    s = draft_survey_with_question()
    change_definition(s.uuid, expected_version=s.definition_version, definition=published(s))
    s.refresh_from_db()
    self.assertEqual(s.published_version, s.definition_version)

def test_published_question_change_locked_even_without_replies(self):
    s = published_survey()
    with self.assertRaises(PublishedLocked):
        change_definition(s.uuid, expected_version=s.definition_version, definition=retitled_question(s))

def test_whitelist_change_keeps_published_version(self):
    s = published_survey(); pv = s.published_version
    change_definition(s.uuid, expected_version=s.definition_version, definition=with_fields(s, is_active=False))
    s.refresh_from_db()
    self.assertEqual((s.published_version, s.definition_version), (pv, pv + 1))

@override_settings(CLOUD_INBOX_ENABLED=False)
def test_node_survey_cannot_publish_while_inbox_off(self): ...   # PublishBlocked

@override_settings(CLOUD_INBOX_ENABLED=True)
def test_node_survey_publish_sets_inbox_since(self): ...

def test_assign_published_survey_refused(self): ...               # PublishedLocked；指令 assign_survey_node 與 enable_survey_inbox 亦 CommandError
def test_node_created_survey_is_draft_with_random_slug(self): ... # re.fullmatch(r"[a-z0-9]{8}", slug)
def test_api_returns_published_locked_422(self): ...
def test_api_rejects_v1_writes(self): ...

# 規格 §7.2 重算對照：每一列一個測試
def test_whitelist_status_category_email_tracking_do_not_bump_versions(self): ...  # state.input_version／config_version 不變
def test_analysis_enabled_and_archive_bump_config(self): ...
def test_draft_edits_and_publish_schedule_nothing(self): ...

# cloudsync（本機模式）：完整路徑
def test_v1_revision_applied_on_node_then_edited_writes_v2(self):
    # 雲端存有 schema 1 的 revision → 本機 upsert 套用 → 本機編輯一題 → node_commit 以 v2 送出 → 雲端接受，代碼與雲端 upgrade_v1 結果相同
    ...
```

- [ ] **Step 2–4: 確認失敗 → 實作 → 執行** 雲端與 `DEPLOYMENT_MODE=node` 的完整測試 → PASS。行為已改變的既有測試改寫斷言，例如 `cloudapi/tests/test_writes.py` 的 `test_assign_backfills_answered_questions_and_records_version_1`（改為：草稿可指派、不回填、已發布被拒）與所有語意鎖測試（依雲端同步規格取代說明刪除）。
- [ ] **Step 5:** 更新 `cloudapi/test_postgres.py`：移除「語意修改與第一筆回答同時發生」，改為「發布與草稿儲存同時發生時依序列化，後到者 409」。
- [ ] **Step 6: Commit** `feat: draft/publish versioning, whitelist and inbox-gated publish`

---

### Task 5: 所有問卷走同一條寫入路徑與生命週期操作

**Files:**
- Create: `feedback/survey_lifecycle.py`
- Modify: `cloudapi/writes.py`（新增 `create_survey(definition) -> SurveyDefinitionRevision`：雲端網站建立草稿，無節點）
- Modify: `feedback/views.py`（`SurveyCreateView`、`SurveyBuilderView.post`、`SurveyDeleteView.form_valid`、`SurveyCategoryDeleteView`）、`cloudapi/builder.py`
- Test: `feedback/test_survey_lifecycle.py`

**Interfaces:**
- Consumes: Task 4。
- Produces（`feedback/survey_lifecycle.py`）：
  - `commit(survey, definition, expected_version) -> None`：`settings.IS_NODE` 時 `node_commit`，否則 `cloud_commit`（所有雲端問卷，不再限 `owner_node`）
  - `create_draft(definition: dict) -> Survey`：雲端以 `cloudapi.writes.create_survey(definition)`（新函式：`random_slug()`、版本 1、revision、無節點）；本機以 `cloudsync.survey_write.create_survey`（雲端 `create_node_survey` 指派該節點）
  - `copy_as_draft(survey) -> Survey`：由 `definition_from_fields` 產生 dict，換新 survey／題目 uuid、`published=False`、清空發布欄位，保留題目 `code`、`choices`（含代碼）與兩種計數器，再交給 `create_draft`。結果：版本 1、未發布；雲端複製無節點，本機複製歸該節點（規格 §4.4）
  - `delete_or_archive(survey, expected_version)`：在交易內鎖問卷，**鎖內重新確認**版本相符且為「未指派節點的草稿」才呼叫 `purge_survey`；已發布或已指派節點的草稿 → `archive_survey` 經 `commit`
- `cloudapi/builder.py` 的 `builder_post` 改為呼叫 `survey_lifecycle.commit`，並新增 action `publish`、`copy`（Task 6 接上 UI）。

- [ ] **Step 1: 寫失敗測試**：`test_plain_cloud_survey_edit_goes_through_change_definition`（編輯後有新 revision）、`test_create_draft_has_random_slug_and_is_unpublished`、
  `test_copy_keeps_codes_and_counters`（`[q.code for q in copy] == [q.code for q in original]`、兩種計數器相同、`copy.definition_version == 1`、`copy.published_version is None`、`copy.owner_node is None`）、
  `test_node_copy_belongs_to_node_and_is_draft`（本機模式）、`test_delete_unassigned_draft_purges`、`test_delete_assigned_draft_archives`、`test_delete_published_archives`、
  `test_delete_rechecks_under_lock`（取得版本後另一請求先發布 → 刪除改為 409，不硬刪）、`test_category_delete_clears_published_survey_category`。
- [ ] **Step 2–4: 確認失敗 → 實作 → 執行** `feedback.test_survey_lifecycle cloudapi.tests.test_builder` → PASS。
- [ ] **Step 5: Commit** `feat: single write path and lifecycle actions for all surveys`

---

### Task 6: 卡片式編輯介面

**Files:**
- Modify: `feedback/forms.py`（移除 `QuestionCreateForm`，新增 `QuestionCardForm`）、`feedback/views.py`（import 與 `SurveyBuilderView.get_context_data`）、`cloudapi/definition.py`（移除 `set_question_active`，新增 `duplicate_question`）、`cloudapi/builder.py`、`templates/feedback/survey_builder.html`（重寫題目分頁）、
  `static/js/question-editor.js`（重寫）、`static/css/` 中 builder 樣式（只限管理頁 class）
- Test: `feedback/test_builder_cards.py`；改寫 `cloudapi/tests/test_builder.py`（例如「草稿刪除只停用」改為依規格 §7.1）

**Interfaces:**
- Consumes: Task 1 `kind_display_for`、`question_errors`；Task 2 編輯 helper；Task 5 `commit`、`delete_or_archive`、`copy_as_draft`。
- Produces: `QuestionCardForm`（欄位：`question_uuid`（hidden，可空）、`ui_type`（`UI_TYPES`）、`title`、`help_text`、`is_required`、`enable_keyword_tracking`、
  `ordered`、`score_start`、`allow_decimal`、`scale_min`、`scale_max`、`scale_min_label`、`scale_max_label`）；選項為**每列完整的一組欄位** `choices-<i>-code`、`choices-<i>-label`、`choices-<i>-excluded`（hidden `0` 加 checkbox `1`，取最後值）、`choices-<i>-position`，依 `position` 排序、空白文字列略過；
  `QuestionCardForm.to_item() -> dict`。段落題新增時「納入文字分析」預設勾選、簡答預設不勾。
  `duplicate_question(definition, uuid) -> dict`（新 uuid、`code` 留空由雲端配發、選項代碼保留、放在原題之後）。
  POST action：`save-question`、`duplicate-question`、`delete-question`、`move-question`、`update-survey`、`publish`、`copy`、`delete-survey`。

- [ ] **Step 1: 寫失敗測試**（Django test client）：

```python
def test_each_ui_type_saves_with_defaults(self):
    for ui_type in UI_TYPES:
        resp = self.post_card(ui_type=ui_type, title=f"{ui_type} 題", choices=["甲", "乙"])
        self.assertEqual(resp.status_code, 302, ui_type)
    self.assertEqual(self.survey.questions.count(), 7)

def test_posted_data_type_is_ignored(self):
    self.post_card(ui_type="scale", title="滿意度", data_type="continuous")
    self.assertEqual(self.survey.questions.get().data_type, "ordinal")

def test_invalid_card_shows_error_and_keeps_input(self):
    resp = self.post_card(ui_type="radio", title="門市", choices=["甲", "甲"])
    self.assertContains(resp, "甲", status_code=200)
    self.assertContains(resp, "選項文字不可重複")

def test_version_conflict_keeps_input(self):
    resp = self.post_card(ui_type="short_text", title="我的修改", definition_version=self.survey.definition_version - 1)
    self.assertContains(resp, "此問卷已在其他視窗修改", status_code=409)
    self.assertContains(resp, "我的修改", status_code=409)
    self.assertContains(resp, "重新載入（捨棄我的修改）", status_code=409)

def test_published_survey_renders_read_only_with_copy(self):
    resp = self.client.get(builder_url(published_survey()))
    self.assertContains(resp, "複製為新草稿")
    self.assertNotContains(resp, 'name="action" value="save-question"')

def test_decimal_hint_present(self):
    self.assertContains(self.client.get(builder_url(self.survey)), "允許小數的數字題可做平均數比較與相關分析")

def test_choice_rows_keep_excluded_on_the_right_row(self):
    self.post_card(ui_type="radio", title="等候", rows=[("很快", False), ("不適用 ", True), ("很久", False)], ordered=True)
    q = self.survey.questions.get()
    self.assertEqual([(c["label"], c["excluded"]) for c in q.choices], [("很快", False), ("不適用", True), ("很久", False)])

def test_choice_reorder_and_blank_rows(self): ...       # position 決定順序、空白列不存
def test_duplicate_question_places_copy_after_original(self): ...
def test_long_text_tracking_default_on_short_text_off(self): ...
```

- [ ] **Step 2–4: 確認失敗 → 實作 → 執行** `feedback.test_builder_cards` → PASS。
  預覽：建立工具頁以 iframe 載入 `survey/<slug>/?preview=1`（Task 7 提供，本 task 只放 iframe 元素）。
  JS 行為（不另寫 JS 測試，於 Step 5 以瀏覽器驗證）：同一時間只展開一張卡片；切換時有未儲存修改跳出「儲存／捨棄／繼續編輯」；`beforeunload` 提醒；
  選項列 Enter 新增下一列；依 `ui_type` 顯示對應設定；上移／下移後 `aria-live` 宣告「已移到第 N 題」。
- [ ] **Step 5: Commit** `feat: Google Forms style question cards`（瀏覽器驗證移到 Task 7，預覽完成後一起做）

---

### Task 7: 填答表單與一般填答寫入

**Files:**
- Modify: `feedback/forms.py`（`SurveyFormBuilder`）、`feedback/local_service.py`（`submit_survey_payload`）、`feedback/views.py`（`SurveyDetailView`）、
  `templates/feedback/survey_detail.html`（刻度按鈕與兩端標籤、預覽模式）
- Test: `feedback/test_fill_form.py`

**Interfaces:**
- Consumes: Task 1。
- Produces: `SurveyFormBuilder` 的選擇題 `choices` 為 `(code, label)`；`submit_survey_payload` 在交易內 `select_for_update` 重新取得問卷，未發布或未收件時 `raise SurveyNotAccepting`（`feedback/local_service.py` 新例外，View 顯示「這份問卷目前未開放填答。」），再寫入 `Answer.choice_codes`（單選 `[code]`、複選依選項順序）、`Answer.value`（標籤，複選以「, 」串接）、`FeedbackSubmission.definition_version = survey.published_version`；
  `SurveyDetailView`：未發布 → 「問卷尚未開放」；管理者加 `?preview=1` 可預覽草稿（不顯示送出鈕、POST 拒絕），**只有這個預覽回應**以 `xframe_options_sameorigin` 允許同源嵌入，其他頁面維持 `DENY`。

- [ ] **Step 1: 寫失敗測試**：`test_choice_value_is_code_not_label`、`test_dropdown_uses_select_widget`、`test_scale_renders_range_with_end_labels`（0–10 產生 11 個選項、兩端標籤出現、無預選）、
  `test_integer_and_decimal_fields`、`test_submission_records_codes_and_published_version`、`test_draft_shows_not_open_notice`、`test_manager_preview_of_draft_has_no_submit`、
  `test_service_rejects_draft_and_closed_survey`（直接呼叫 `submit_survey_payload`）、`test_view_checked_then_closed_before_write_is_rejected`、
  `test_preview_header_sameorigin_only_for_manager_preview`（顧客帶 `?preview=1` 不能預覽；一般填答頁仍是 `DENY`）、`test_preview_post_rejected`。
- [ ] **Step 2–4: 確認失敗 → 實作 → 執行** `feedback.test_fill_form feedback.tests` → PASS。
- [ ] **Step 5: 瀏覽器驗證**：以 `.claude/launch.json` 的伺服器搭配測試資料（或使用者授權的開發 DB）開啟建立工具，七種題型各新增一次、觸發一次驗證錯誤、一次 409、預覽 iframe 正常載入、發布後唯讀，截圖存證。
- [ ] **Step 6: Commit** `feat: fill form uses choice codes, scale ranges and published version`

---

### Task 8: 分析清單、排除計數與複選題

**Files:**
- Modify: `feedback/analysis_input.py`（`AnalysisField.excluded`）、`feedback/analysis_adapters.py`、`feedback/background_analysis.py`（`descriptors`、`calculate_statistics`、`field_coverage`）、
  `feedback/local_service.py`（`get_survey_pandas_stats`、`analyze_frame`）、`templates/feedback/stats_overview.html`、`feedback/ai_snapshot_service.py`（分布）、`feedback/ai_grounding.py`
- Test: `feedback/test_analysis_choices.py`

**Interfaces:**
- Consumes: Task 1 `analysis_options`／`analysis_excluded_options`。
- Produces:
  - `AnalysisField(..., excluded: tuple[str, ...] = ())`（新增於最後，位置參數相容）
  - `answer_cell(question, value: str, choice_codes: list|None) -> str | list[str] | None`（`feedback/analysis_adapters.py`）：選擇題以代碼對應**目前**標籤，複選回傳清單，其他回傳 `value`
  - `classify_values(series, *, data_type, kind, levels, excluded) -> dict`（`feedback/local_service.py`）回傳 `total_n`、`valid_n`、`missing_n`、`excluded_n`、`invalid_n` 與布林遮罩 `valid_mask`
  - `analyze_frame` 每張圖加入 `valid_n`、`missing_n`、`excluded_n`、`invalid_n`；單選排除選項列於 `excluded_counts: [{"value","total"}]`；
    複選圖 `multi: True`、`answered_n`，每列 `{"value","total","selection_rate","check_share"}`（`total`＝選取人數；`selection_rate`＝total/answered_n×100；`check_share`＝total/全部勾選數×100），不再有 `percent`
  - `field_coverage[name]` 加入 `excluded_n`

- [ ] **Step 1: 寫失敗測試**：

```python
def test_counts_partition_total(self):
    df = frame(["低", "高", "不適用", None, "亂碼"])
    c = classify_values(df, data_type="ordinal", kind="single_choice", levels=["低", "高"], excluded=["不適用"])
    self.assertEqual((c["valid_n"], c["excluded_n"], c["missing_n"], c["invalid_n"], c["total_n"]), (2, 1, 1, 1, 5))

def test_orm_and_worker_paths_agree(self):
    # 1–10 刻度、有序單選（含排除選項）、門市；另有一筆完全未作答的回覆、一筆排除、一筆無效值
    survey = survey_for_path_parity()
    orm = get_survey_pandas_stats(survey)
    adapter = AnswerInput.from_survey(survey, version="t")
    worker, row_count = calculate_statistics(adapter, descriptors(adapter))
    self.assertEqual(row_count, survey.submissions.count())
    pick = lambda rows: sorted((r["method_key"], r["iv_title"], r["dv_title"], r.get("statistic"), r.get("p_value"), r.get("valid_n"))
                               for r in rows if not r.get("skipped_reason"))
    self.assertEqual(pick(orm["inferential_analysis"]), pick(worker["inferential_analysis"]))
    counts = lambda charts: {c["question"].title: (c["valid_n"], c["missing_n"], c["excluded_n"], c["invalid_n"]) for c in charts}
    self.assertEqual(counts(orm["charts"]), counts(worker["charts"]))
    self.assertIn("kruskal_wallis", {r["method_key"] for r in orm["inferential_analysis"]})

def test_multi_choice_selection_rate_vs_check_share(self):
    chart = multi_chart(answers=[["A"]] * 5 + [["A", "B"]] * 5)
    a = next(r for r in chart["counts"] if r["value"] == "A")
    self.assertEqual((a["total"], a["selection_rate"], a["check_share"]), (10, 100.0, 66.67))

def test_comma_label_multi_choice_round_trips(self):
    chart = multi_chart(labels=["珍珠, 椰果", "紅茶"], answers=[["珍珠, 椰果"]] * 3)
    self.assertEqual(chart["counts"][0]["value"], "珍珠, 椰果")

def test_only_multiple_choice_is_split(self): ...      # 單選標籤含逗號不被拆
def test_parquet_path_excluded_n_is_zero(self): ...
def test_snapshot_and_grounding_name_both_rates(self): ...  # 證據標籤分別為「選取率」「勾選次數占比」
```

- [ ] **Step 2–4: 確認失敗 → 實作 → 執行** `feedback.test_analysis_choices feedback.test_background_analysis feedback.test_analysis_e2e feedback.test_ai_grounding feedback.test_ai_stages` → PASS。
  排除與無效值在檢定前一律轉 NA；`encode_ordinal` 讀 `question.analysis_options`；`descriptors` 設定 `_analysis_options`／`_analysis_excluded`。
  `get_survey_pandas_stats` 改以該問卷完成且未作廢的 `FeedbackSubmission` 為列（規格 §7.3，無答案的回覆也算一列），與 Worker 相同；`calculate_statistics` 維持回傳 `(result, row_count)`。
- [ ] **Step 5: Commit** `feat: excluded options, shared levels and multi-choice rates in analysis`

---

### Task 9: AI Snapshot 指紋

**Files:**
- Modify: `feedback/ai_snapshot_service.py`（題目串流）
- Test: `feedback/test_ai_stages.py`（新增）、移除 Task 3 指紋測試的 skip

**Interfaces:**
- Produces: 題目串流在原本 `options_text` 的位置改放 `"\n".join(analysis_levels(q))`（對沒有排除選項的題目，與轉換前的 `options_text` 位元組相同），其餘欄位順序不變，
  僅在不同於預設時附加：有排除選項、`score_start == 0`、有刻度標籤、`display == "dropdown"`；答案串流在 `choice_codes` 非空時附加代碼。

- [ ] **Step 1: 失敗測試**：`test_fingerprint_bytes_unchanged_for_default_fields`（以舊程式先算出 TripAdvisor 九題 fixture 的 `calculate_data_fingerprint(...).value` 寫成常數，改版後相同）、
  `test_fingerprint_changes_when_excluded_option_added`；移除 Task 3 TripAdvisor 測試的 `expectedFailure`。
- [ ] **Step 2–4: 確認失敗 → 實作 → 執行** `feedback.test_ai_stages feedback.test_schema_conversion feedback.test_published_analysis` → PASS（Task 3 的 TripAdvisor 測試此時必須真正通過）。
- [ ] **Step 5: Commit** `feat: snapshot fingerprint stable across builder schema`

---

### Task 10: 收件匣、封套與新鮮度

**Files:**
- Modify: `cloudapi/inbox.py`、`cloudapi/envelope.py`、`cloudapi/views.py`（隔離原因）、`cloudsync/inbox.py`、`cloudsync/capture.py:35`、`cloudapi/freshness.py`、
  `templates/feedback/_analysis_publication_status.html`
- Test: `cloudapi/tests/test_inbox_accept.py`、`cloudapi/tests/test_envelope.py`、`cloudsync/tests/test_inbox.py`、`cloudapi/tests/test_result_display.py`

**Interfaces:**
- Consumes: Task 1、Task 7（`SurveyFormBuilder` 代碼）。
- Produces:
  - `envelope.ANSWERS_FORMAT = 2`；`build_envelope(..., answers)` 加 `"answers_format": 2`；`payload_hash(...)` 參數加 `answers_format`（納入雜湊）
  - `encode_answers(survey, cleaned)`：選擇題單選為代碼字串、複選為代碼陣列
  - `accept_submission`：列鎖內先檢查 `survey.accepts_responses`（否則 `InboxRejected` 子類 `SurveyClosed`，訊息「這份問卷目前未開放填答。」），表單版本比對 `published_version`
  - `QUARANTINE_REASONS = ("content_conflict", "definition_unavailable", "answers_format")`；本機 `intake` 遇到缺少或不是 2 的 `answers_format` 回傳 `ANSWERS_FORMAT`
  - 本機寫入：選擇題 `choice_codes` 取自封套、`value` 由代碼對應標籤
  - `capture`：`CaptureScope.definition_version = survey.published_version`
  - `node_freshness`：`definition_current` 比對 `published_version`；移除 `legacy_unmigrated` 與範本中的「雲端既有 N 筆未納入分析」
  - `SurveyDetailView` 的 `inbox_definition_version` 改為 `published_version`

- [ ] **Step 1: 失敗測試**：`test_whitelist_change_does_not_reject_open_form`（取得表單版本 → 停止再恢復收件 → 送出成功）、`test_closed_survey_rejected_under_lock`、
  `test_envelope_carries_answers_format_and_codes`、`test_node_quarantines_missing_answers_format`、`test_node_writes_choice_codes_and_labels`、
  `test_freshness_ignores_whitelist_changes`、`test_capture_uses_published_version`、`test_node_quarantines_answers_format_other_than_2`、`test_payload_hash_covers_answers_format`、`test_resend_same_payload_reuses_receipt_after_whitelist_change`、`test_ack_after_codes_written`（本機 ACK 的 payload_hash 與雲端一致）、`test_unknown_choice_code_quarantined`（規格 §7.3）。
- [ ] **Step 2–4: 確認失敗 → 實作 → 執行** `cloudapi cloudsync --settings=config.settings_test`（雲端與 node 模式）→ PASS，含 `cloudsync.tests.test_e2e_inbox`、`test_e2e_results`。
- [ ] **Step 5: Commit** `feat: inbox envelope answers_format and published_version checks`

---

### Task 11: 外部資料匯入

**Files:**
- Modify: `feedback/importing/service.py`（`_prepare_row`、`_ensure_survey_and_questions`、答案寫入）、`feedback/importing/mapping.py`（以 `derive_data_type` 驗證）
- Test: `feedback/test_dataset_import.py`、`feedback/test_tripadvisor_import.py`

**Interfaces:**
- Consumes: Task 1；Task 4 的 `record_version`。
- Produces: mapping 題目轉成新結構（`code` 沿用 `_question_codes`；整數連續選項的刻度 → 範圍；選擇題 → `choices`）；相容比對改比 `code`、`title`、`kind`、`data_type`、`analysis_options`、
  `is_required`、`enable_keyword_tracking`、`is_active`、`order`；新建問卷在單一交易內設 `definition_version=1`、`published_version=1`、`published_at=now`，再 `record_version`（revision 版本 1）；小型匯入的 `FeedbackSubmission.definition_version=survey.published_version`；答案寫 `choice_codes`（以標籤比對，比對不到計入匯入報告 `invalid_choice`）。

- [ ] **Step 1: 失敗測試**：`test_both_mappings_pass_derive_data_type`、`test_tripadvisor_scales_become_ranges_and_published`、`test_title_question_not_text_tracked`（評論標題 `enable_keyword_tracking=False`）、
  `test_amazon_small_import_writes_choice_codes`、`test_new_import_versions_and_revision_consistent`（定義版本＝發布版本＝revision 版本＝回覆版本＝1）、`test_existing_converted_questions_are_compatible`（Task 3 轉換後的題目再次匯入不報不相容）。
- [ ] **Step 2–4: 確認失敗 → 實作 → 執行** `feedback.test_dataset_import feedback.test_tripadvisor_import feedback.test_local_dataset` → PASS。
- [ ] **Step 5: Commit** `feat: dataset import uses choice codes and published surveys`

---

### Task 12: 種子資料與文件

**Files:**
- Modify: `feedback/management/commands/seed_demo_beverage.py`、`seed_random_responses.py`、`seed_demo.py`、`seed_notification_test.py`、`scripts/seed_demo_data.py`
- Modify: `docs/next-actions.md`、`docs/architecture.md`（題型與資料型態段落）
- Test: `feedback/test_seed_commands.py`

**Interfaces:**
- Consumes: Task 1–8。
- Produces: `seed_demo_beverage` 題目（spec §5 第 6 步，依序）：門市（單選，信義店／台北車站店／公館店／士林店）、內用或外帶（單選，內用／外帶）、品項（複選，沿用 `ITEMS`）、
  整體滿意度（刻度 1–10，「非常不滿意」／「非常滿意」）、推薦意願（刻度 0–10，「完全不會」／「一定會」）、等候時間感受（有序單選：很快／普通／久／很久＋排除「不適用」）、
  等候分鐘數（允許小數）、消費金額（允許小數）、來店次數（整數）、改善建議（段落，納入文字分析）；建立後發布；填答者名稱維持「飲料店模擬填答」前綴；
  模擬分布讓等候分鐘數與滿意度負相關、消費金額與滿意度正相關，內用與外帶的滿意度有差異。其他種子改用新欄位並寫 `choice_codes`、建立後發布。

- [ ] **Step 1: 失敗測試**：`test_beverage_seed_produces_all_seven_methods`（seed 後 `build_stats_payload` 的 `method_key` 集合包含 `welch_t_test`、`one_way_anova`、`chi_square`、`mann_whitney_u`、`kruskal_wallis`、`pearson`、`spearman`）、`test_beverage_seed_marks_simulated_respondents`、`test_beverage_reset_twice`、`test_other_seeds_create_published_surveys_with_codes`、
  `test_beverage_seed_text_analysis_and_publish`（文字分析有關鍵字與情緒結果；以 mock AI 跑完分析並發布，網站讀到最新的已發布結果）。
- [ ] **Step 2–4: 確認失敗 → 實作 → 執行** `feedback.test_seed_commands` → PASS。
- [ ] **Step 5: 文件**：`next-actions.md` 移除已完成的建立工具待辦，加入規格 §7.5 的部署流程（每一步標示需授權；含隔離資料庫還原、`survey_conversion_report` 檢查與失敗復原）；`architecture.md` 更新題型表與單一寫入路徑。`git diff --check`。
- [ ] **Step 6: 全套測試**：雲端與 `DEPLOYMENT_MODE=node` 各跑 CI 的完整指令；`makemigrations --check`；`manage.py check`。
- [ ] **Step 7: Commit** `feat: seeds use the new builder schema; docs`
