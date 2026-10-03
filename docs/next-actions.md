# 現況與待辦

只記錄**目前**狀態與未完成事項；完成的項目直接刪除，不保留歷史（歷史看 Git log）。
實際程式、資料庫與驗證證據優先於本文件；標示「待確認」者不可當成事實。

## 目前狀態（2026-09-29 查證）

**部署**
- `main` 由 GitHub Actions CI 驗證（Python 3.13 全套測試＋PostgreSQL 17 併發測試），合併後 Render 自動部署。
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
- TripAdvisor（201,295 筆，現行管線）與飲料店問卷的三段 Gemini 結果皆為 gemini-3.6-flash、綜合解析 prompt `4-29b4625aa1`。
  「2026 Q1 跨部門…」（46 筆）仍是前一版綜合解析 prompt（`4-e242379872`），桌面工作台會顯示「待更新」。
- 驗證規則：綜合解析與其他兩段一致，單一不合格的發現或改善草稿只捨棄該項（原因記於 `discarded_finding_reasons`）；
  未對應 evidence 的數字記於 `ungrounded_numbers`／`discarded_ungrounded_numbers`（只存數字）。負值 evidence 可引用其絕對值。
- TripAdvisor 來源：本機 clean Parquet 201,295 筆，已登錄為外部分析來源。

**桌面工作台／EXE**
- `dist/FeedbackInsightHub/` 為單一 windowed 版；錯誤寫入 `%LOCALAPPDATA%\FeedbackInsightHub\logs\desktop.log`。
- 本機 `.venv`、EXE 與 Render 都使用 Python 3.13（本機為獨立安裝的 3.13.15，Render 釘選 3.13.2）；
  舊的 3.12 環境保留於 `.venv-py312` 供回退，確認無誤後可刪除。

## 待辦（依優先順序）

0. **AI 評估下一輪**：依 [AI 輸出評估](ai-eval.md) 的發現，在引用代號寫法加上「句中代號也計入 4 筆引用上限」的規則並重跑評估（付費，需授權）；
   若穩定優於現行寫法，再規劃改進正式綜合解析。案例說明見 [case-study](case-study.md)。
1. **重跑 Gemini**（付費，需授權）：「2026 Q1 跨部門…」問卷的綜合解析改用目前 prompt。
- **雲端同步**（規格 `2026-10-01-cloud-sync-design.md`）：C1 問卷定義同步已完成（原型，閘門預設關閉）；
  接續 C2 收件匣、C3 結果、C4 搬移，各需先寫實作計畫。本機節點計畫 B 仍暫停。
2. **機器學習**：先決定目標（展示或實用）、運算資源（CPU／GPU）、EXE 大小容忍度；建議起點為關鍵驅動因子分析與
   TF-IDF＋邏輯迴歸文字分類，使用依日期的固定切分，並與現行詞典方法比較。
3. **為 ML 調整 schema**（需 migration 與授權）：`Answer` 加數值欄位、選項表（穩定 key）、題目版本、
   模型預測另存一表（含模型版本）、指標歷史表；模型產物與向量留在本機，Supabase 只存中繼資料與彙總。
4. **安裝包與簽章**：本機節點已由系統匣啟動器監督 Worker；尚缺安裝程式與程式碼簽章。
5. **文字分析效能**：TripAdvisor 全量文字階段約 21.6 秒，瓶頸在逐筆處理與重複斷詞，可改為分批平行處理。
6. **歷史版本比較介面**（依賴第 3 項的指標歷史表）。
7. **清理舊版單次 AI 報告的顯示相容程式**（`ai_report_service` 驗證器、舊草稿匯入 view、`get_report_status` 等）。
8. **待決定**：手動新增改善項目是否應讓已發布的 AI synthesis 過期（會觸發付費重跑）。
9. **UI 小項**：文字雲調色盤仍是舊的橘／藍色系，與品牌綠不一致。

## 延後範圍

預測模型的正式上線、多組織（owner）資料隔離，需各自的規格文件後再開始。
