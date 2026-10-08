# 現況與待辦

只記錄**目前**狀態與未完成事項；完成的項目直接刪除，不保留歷史（歷史看 Git log）。
實際程式、資料庫與驗證證據優先於本文件；標示「待確認」者不可當成事實。

## 本機接手進度（2026-10-07，局部實作／正式切換未執行）

- [自助雲端發布與修改流程](superpowers/plans/2026-10-07-self-service-cloud-publication.md)中的「發布歷史分析」擱置，非本輪範圍；先完成原節點規格，不新增第三種歷史來源契約。歷史副本已依完成且對帳成功的匯入紀錄標為「僅本機」，移除無效修復入口，不再列為上傳／同步失敗；真實來源與分析／上傳錯誤仍提示。相關 49 項隔離測試通過（含匯出／匯入後沿用 Worker 發布的狀態核對）；此次僅原始碼更新，EXE 尚未包含此收斂修正。
- [歷史問卷與回答接手](history-transfer.md)：飲料店已接入實際本機 DB（100 筆回覆／1,000 筆答案，逐筆重送對帳通過），接手前 SQLite 完整備份通過完整性檢查。它是獨立 UUID 的歷史副本，不是節點所屬雲端問卷，不能直接上傳覆蓋舊問卷。雲端問卷及回答全部保留。
- TripAdvisor 完整 201,295 列的既有 Parquet 已完成節點雲端來源登錄（9 題，定義版本 1，雲端 slug `pka6fikw`）。本機工作 #7 重算統計／文字，發布 Snapshot #4 後自動建立 C3 上傳紀錄；本次以限定來源的上傳服務送達，雲端唯讀核對兩段結果皆最新、涵蓋 201,295 筆。沒有重新下載、抽樣、展開回答或上傳評論正文。
- 新節點 TripAdvisor 與舊雲端問卷是不同 UUID；舊雲端三份問卷、回答及發布內容的前後摘要核對一致。飲料店歷史副本維持僅本機，不需要綁定來消除同步警告；新的飲料店節點示範另按規格建立問卷與限定自測收件流程，不改寫舊問卷所有權。正式收件切換仍未執行。
- 自動上傳工作流已修正並重建 EXE：發布交易自動建立上傳紀錄，啟動器正常運行時每 5 分鐘背景同步；同一發布重送沿用上傳身分，未綁定／漏建／失敗不回報整體完成。工作頁分列本機發布與本次版本雲端確認。此次補正等價 UTC／時區時間比較，嚴格拒絕真正不同的來源時間；相關隔離測試及授權操作證據見接手計畫。未部署程式或呼叫 Gemini；節點新結果沒有沿用舊版本 AI。
- 分析工作頁已改為總覽／來源卡片／階段狀態／最近紀錄，顯示真實工作與發布狀態、錯誤代碼及最近回報；10 秒唯讀自動更新可暫停，Gemini 確認期間不更新。同版本統計工作執行／等待中會停用按鈕，POST 也避免重複排程。Worker 租約綁定的進度表示階段完成數，不是耗時比例；未回報時使用動態條，本機發布才顯示 100%，另列雲端收妥狀態。工作流相關 79 個隔離場景已通過，UTC 修正另以 13 項資料集頁測試驗證（含 2 個新增場景）。

- [分段接手與回退](superpowers/plans/2026-10-05-node-only-analysis-integration.md)：先建立外部版本綁定的本機路徑登錄、Worker 取檔與 C3 來源身分核對；保留既有工作台、EXE、資料與正式設定。
- PR #30 已合併，基準 `2a48db8`；本段在 `codex/node-function-completion`，使用者於 2026-10-07 授權提交、推送並開 PR 供審查，不構成合併或部署授權。規範正本：[節點唯一分析規格](superpowers/specs/2026-10-04-node-only-analysis-design.md)。
- 本機 OWNER 工作頁、Windows Gemini 憑證庫、版本綁定確認與有限呼叫、取消及安全稽核已實作；壞來源不阻塞其他上傳，外部來源缺失不接受填答。
- 一般回覆及四筆外部 fixture 已經兩個獨立隔離 DB 的真 HTTP 驗收；Gemini 管線／Worker 使用 mock。另已完成真實 TripAdvisor 全量統計／文字與正式結果 API 上傳，未呼叫真實 Gemini。
- 最新 `FeedbackInsightHubNode` 封裝包含工作流與 UTC 修正，隔離 smoke 退出 0、未建立 DB，五份 UI 資產一致且未封裝環境設定、SQLite 或資料集。舊版完整保留於 `.tmp/node-workflow-exe-backup-20261007-192929/`。本次未跑 migrate、未啟動完整節點服務；Render 瀏覽器驗收遇登入頁，未驗收登入後畫面。雲端驗證限於正式 API 收妥及正式 DB 的唯讀展示服務核對，不將此當成登入後 GUI 驗收。
- 既有來源換版／登錄／發布 PostgreSQL 併發已由 PR #30 CI 的 PG17 job 通過；本段未重跑，不據此推定新確認流程的 PG 行為。
- 待完成：雲端全面停止排程、明文自測範圍限制、網站自動指派、正式切換及驗收後退役舊工作台。正式站收件閘門不變；詳見接手文件。

## 目前狀態（2026-10-04 查證）

**部署**
- `main` 由 GitHub Actions CI 驗證（Python 3.13 全套測試＋本機節點模式＋PostgreSQL 17 併發測試）。
  Render 設定為 CI 通過後自動部署，Build Command 為 `bash build.sh`（含 `migrate`）；
  **合併含 migration 的 PR 就等於對正式資料庫套用 migration**，需要先處理資料的 migration 要等處理完再合併。
  Render 服務不是 Blueprint 建立的，`render.yaml` 不會套用，實際設定以 Dashboard 為準（見 [除錯筆記](debugging-notes.md)）。
  目前 Live 的 commit 以 Render Dashboard 為準。
- 網站（本機與 Render）分析頁只讀已發布結果；`/healthz/`、`/healthz/db/` 健康檢查；資料庫無法連線時回 503 提示頁。
- `.github/workflows/keepalive.yml` 每三天呼叫 `/healthz/db/`，避免 Supabase 因閒置暫停。

**資料庫（Supabase 免費方案，上限 500 MB）**
- 2026-09-29 實測 15 MB。TripAdvisor 的資料列只在本機 Parquet；Supabase 中該問卷沒有回覆列，
  只保留問卷定義、`DatasetImportBatch` 匯入紀錄、來源版本與發布結果。
- 早期匯入的 10 萬筆樣本列已從 Supabase 移除，備份於 `data/local/backups/supabase-tripadvisor-sample-20260929/`
  （Parquet＋`manifest.json`，含 SHA-256 與筆數驗證）。

**分析與 Gemini**
- `GEMINI_MODEL=gemini-3.6-flash`（Vertex／Agent Platform express mode）；gemini-2.5-flash 預定 2026-10 停用。
- Prompt 為 evidence-grounded：AI 文字中的數字須能對應所引用 evidence（`feedback/ai_grounding.py`）。
- 舊展示來源為 TripAdvisor（201,295 筆）與飲料店（`seed_demo_beverage` 模擬 100 筆，種子 7），舊 Gemini 結果保留。
  另有 2026-10-07 新節點 TripAdvisor（slug `pka6fikw`），目前只有最新統計／文字結果，尚無該版本 Gemini 結果。
- 驗證規則：綜合解析與其他兩段一致，單一不合格的發現或改善草稿只捨棄該項（原因記於 `discarded_finding_reasons`）；
  未對應 evidence 的數字記於 `ungrounded_numbers`／`discarded_ungrounded_numbers`（只存數字）。負值 evidence 可引用其絕對值。
- TripAdvisor 來源：本機 clean Parquet 201,295 筆，已登錄為外部分析來源。

**問卷與同步**
- **雲端同步**（規格 `2026-10-01-cloud-sync-design.md`）：C1 問卷定義同步、C2 收件匣、C3 結果上傳已完成（原型，閘門預設關閉）；
  C4 搬移擱置（收件節點在發布時決定，不搬移既有回覆）。本機節點計畫 B 仍暫停。
- **問卷建立工具**（規格 `2026-10-03-survey-builder-redesign-design.md`）已部署，介面為 Google 表單式卡片；
  之後處理通知系統。
- **飲料店改走收件匣、網站草稿指派節點**（[節點唯一分析規格](superpowers/specs/2026-10-04-node-only-analysis-design.md) 第 1、4、5 節）：
  程式已完成，**尚未部署**。明文收件匣只收 `CLOUD_INBOX_SELF_TEST_SURVEYS` 列出的問卷（`CLOUD_INBOX_REQUIRE_SELF_TEST` 預設開啟）；
  飲料店節點問卷 UUID 固定為 `seed_demo_beverage.BEVERAGE_NODE_SURVEY_UUID`。部署後操作順序，**每步另行授權**：
  1. Render 設 `CLOUD_INBOX_SELF_TEST_SURVEYS=<該 UUID>`、`CLOUD_SYNC_PROTOTYPE_ENABLED=True`、`CLOUD_INBOX_ENABLED=True`，並建立節點裝置權杖。
  2. 節點連結雲端後執行 `seed_demo_beverage --node-create`。
  3. 雲端執行 `seed_demo_beverage --inbox --count 100 --seed 7`（同一 seed 重跑視為重送）。
  4. 節點同步、分析、上傳後驗收網站數量與結果；之後才封存舊 `beverage-feedback` 問卷，清除另行批准。Gemini 另行批准。

**桌面工作台／EXE**
- `dist/FeedbackInsightHub/` 為單一 windowed 版；錯誤寫入 `%LOCALAPPDATA%\FeedbackInsightHub\logs\desktop.log`。
- 本機 `.venv`、EXE 與 Render 都使用 Python 3.13（本機為獨立安裝的 3.13.15，Render 釘選 3.13.2）；
  舊的 3.12 環境保留於 `.venv-py312` 供回退，確認無誤後可刪除。

## 待辦（依優先順序）

1. **AI 評估下一輪**：依 [AI 輸出評估](ai-eval.md) 的發現，在引用代號寫法加上「句中代號也計入 4 筆引用上限」的規則並重跑評估（付費，需授權）；
   若穩定優於現行寫法，再規劃改進正式綜合解析。案例說明見 [case-study](case-study.md)。
2. **機器學習**：先決定目標（展示或實用）、運算資源（CPU／GPU）、EXE 大小容忍度；建議起點為關鍵驅動因子分析與
   TF-IDF＋邏輯迴歸文字分類，使用依日期的固定切分，並與現行詞典方法比較。
3. **為 ML 調整 schema**（需 migration 與授權）：`Answer` 加數值欄位、選項表（穩定 key）、題目版本、
   模型預測另存一表（含模型版本）、指標歷史表；模型產物與向量留在本機，Supabase 只存中繼資料與彙總。
4. **安裝包與簽章**：本機節點已由系統匣啟動器監督 Worker；尚缺安裝程式與程式碼簽章。
5. **文字分析效能**：2026-10-07 節點 TripAdvisor 全量文字階段約 70.8 秒、統計約 9.1 秒；這是該次執行時間，不與舊管線時間直接視為同條件基準。效能改善另行量測。
6. **歷史版本比較介面**（依賴第 3 項的指標歷史表）。
7. **清理舊版單次 AI 報告的顯示相容程式**（`ai_report_service` 驗證器、舊草稿匯入 view、`get_report_status` 等）。
8. **待決定**：手動新增改善項目是否應讓已發布的 AI synthesis 過期（會觸發付費重跑）。
9. **UI 小項**：文字雲調色盤仍是舊的橘／藍色系，與品牌綠不一致。

## 延後範圍

預測模型的正式上線、多組織（owner）資料隔離，需各自的規格文件後再開始。
