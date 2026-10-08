# 現況與待辦

只記錄**目前**狀態與未完成事項；完成的項目直接刪除，不保留歷史（歷史看 Git log）。
實際程式、資料庫與驗證證據優先於本文件；標示「待確認」者不可當成事實。

## 目前狀態（2026-10-08 查證）

**部署**
- `main` 由 GitHub Actions CI 驗證（Python 3.13 全套測試＋本機節點模式＋PostgreSQL 17 併發測試）。
  Render 設定為 CI 通過後自動部署，Build Command 為 `bash build.sh`（含 `migrate`）；
  **合併含 migration 的 PR 就等於對正式資料庫套用 migration**，需要先處理資料的 migration 要等處理完再合併。
  Render 服務不是 Blueprint 建立的，`render.yaml` 不會套用，實際設定以 Dashboard 為準（見 [除錯筆記](debugging-notes.md)）。
- 網站（本機與 Render）分析頁只讀已發布結果；`/healthz/`、`/healthz/db/` 健康檢查；資料庫無法連線時回 503 提示頁。
- `.github/workflows/keepalive.yml` 每三天呼叫 `/healthz/db/`，避免 Supabase 因閒置暫停。
- Render 已開啟 `CLOUD_SYNC_PROTOTYPE_ENABLED`、`CLOUD_INBOX_ENABLED`；`CLOUD_INBOX_SELF_TEST_SURVEYS` 目前只列飲料店節點問卷。

**資料歸屬（[節點唯一分析規格](superpowers/specs/2026-10-04-node-only-analysis-design.md)）**
- 本機節點是唯一分析入口：這台 PC（裝置 `Arvin-PC`，雲端唯一啟用中的節點），封裝 `dist/FeedbackInsightHubNode/`。
  背景同步每 5 分鐘收件、確認收訖、上傳結果；錯誤寫入 `%LOCALAPPDATA%\FeedbackInsightHub\logs\`。
- 正式 DB 只有節點擁有的問卷；雲端模式的分析排程只供開發與測試使用（雲端擁有的問卷沒有執行者）。
- 網站新建的問卷自動指派給節點；回覆經明文收件匣進入節點（2026-10-04 批准，**僅限模擬與自測資料**，接入真實顧客回覆前須完成加密）。
- Supabase 實測 16 MB（免費上限 500 MB；2026-10-08 清除舊問卷前備份於 `data/local/backups/supabase-dumpdata-20261008-201127/`），只存問卷定義、收據、收件匣暫存與上傳的發布結果。
- TripAdvisor 201,295 筆在本機 Parquet，經節點「資料集」登錄；評論正文不上傳。

**問卷**

| 問卷 | slug | 狀態 |
|---|---|---|
| TripAdvisor（節點，外部資料） | `pka6fikw` | 統計／文字最新，**尚無 Gemini** |
| 飲料店（節點，收件匣模擬 100 筆，種子 7） | `rx2ffwkk` | 統計／文字最新，**尚無 Gemini** |
| 咖啡店顧客體驗調查（草稿） | `rlcpny8x` | 已指派節點；發布前須把 UUID 加入 `CLOUD_INBOX_SELF_TEST_SURVEYS` |

**分析與 Gemini**
- `GEMINI_MODEL=gemini-3.6-flash`（Vertex／Agent Platform express mode）；節點的金鑰存在 Windows 認證管理員，在主控台確認後才呼叫。
- Prompt 為 evidence-grounded：AI 文字中的數字須能對應所引用 evidence（`feedback/ai_grounding.py`）；
  單一不合格的發現或改善草稿只捨棄該項（原因記於 `discarded_finding_reasons`、`discarded_ungrounded_numbers`）。

## 待辦（依優先順序）

1. **兩份節點問卷跑 Gemini**：在節點主控台「分析工作」確認執行（付費，約 3 次呼叫／份）。
2. **收件匣加密**：接入真實顧客回覆的前提（[雲端同步規格](superpowers/specs/2026-10-01-cloud-sync-design.md) 第 12 節）。
3. **AI 評估下一輪**：依 [AI 輸出評估](ai-eval.md) 的發現，在引用代號寫法加上「句中代號也計入 4 筆引用上限」的規則並重跑評估（付費，需授權）；
   若穩定優於現行寫法，再規劃改進正式綜合解析。案例說明見 [case-study](case-study.md)。
4. **機器學習**：先決定目標（展示或實用）、運算資源（CPU／GPU）、EXE 大小容忍度；建議起點為關鍵驅動因子分析與
   TF-IDF＋邏輯迴歸文字分類，使用依日期的固定切分，並與現行詞典方法比較。
5. **為 ML 調整 schema**（需 migration 與授權）：`Answer` 加數值欄位、選項表（穩定 key）、題目版本、
   模型預測另存一表（含模型版本）、指標歷史表；模型產物與向量留在本機，Supabase 只存中繼資料與彙總。
6. **安裝包與簽章**：本機節點已由系統匣啟動器監督 Worker；尚缺安裝程式與程式碼簽章。
7. **文字分析效能**：節點 TripAdvisor 全量文字階段約 70 秒、統計約 9 秒；瓶頸與改善另行量測。
8. **歷史版本比較介面**（依賴第 5 項的指標歷史表）。
9. **清理舊版單次 AI 報告的顯示相容程式**（`ai_report_service` 驗證器、舊草稿匯入 view、`get_report_status` 等）。
10. **待決定**：手動新增改善項目是否應讓已發布的 AI synthesis 過期（會觸發付費重跑）。
11. **UI 小項**：文字雲調色盤仍是舊的橘／藍色系，與品牌綠不一致；節點頁面文案「本頁只讀取 Supabase 已發布結果」在節點上不正確。

## 延後範圍

預測模型的正式上線、多組織（owner）資料隔離、多節點，需各自的規格文件後再開始。
