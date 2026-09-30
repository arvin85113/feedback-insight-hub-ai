# 本機節點：主控台外殼與登入（子專案 1＋2a）

狀態：設計已確認，待審閱規格（2026-09-30）。共用決策見
[本機節點架構總覽](2026-09-30-local-node-architecture-design.md)。

## 目標與成功標準

客戶安裝 EXE 後，以瀏覽器使用本機網頁主控台：首次設定建立擁有者、以本機帳號或受邀的 Google 帳號登入、
預設只能從本機連線，並可由擁有者開放到區域網路（HTTPS）。現有的問卷與分析頁在主控台中可用；Render 雲端網站行為不變。

驗收標準：
1. 首次開啟約 2 分鐘內完成設定精靈，建立擁有者帳號。
2. 預設情況下，同一區域網路的其他電腦連不到主控台。
3. 受邀的 Google 帳號能在本機登入；未受邀的帳號被拒絕。
4. 關閉瀏覽器後從系統匣重開，登入狀態正確；閒置超過設定時間自動登出。
5. 總覽頁正確顯示資料庫、Worker、磁碟空間、區域網路與雲端連線狀態。
6. 現有問卷管理、編排、統計、文字、AI 結果頁在主控台中正常運作。
7. 擁有者開放區域網路後，其他電腦經 HTTPS 以本機帳號登入；擁有者未啟用兩步驟驗證時無法開放。
8. 雲端模式（Render）現有測試全部通過，且看不到任何本機節點頁面。

## 不在本規格範圍

部門與部門層級角色、`accessible_surveys` 權限過濾（子專案 2b）；雲端連線與收件匣（8）；改善任務（9）；
工作與日誌頁（3）；資料來源頁（4）；本地 LLM（5）；備份與還原（7）。

## 1. 部署模式開關

- `config/settings.py` 新增 `DEPLOYMENT_MODE`（`cloud` 預設、`node`），以環境變數設定；不另建設定檔。
- `node` 模式時：
  - 資料庫預設為 `%LOCALAPPDATA%\FeedbackInsightHub\data\node.sqlite3`，啟用 WAL（`init_command`／`transaction_mode` 設定），
    可由 `NODE_DATABASE_URL` 改為 PostgreSQL（不讀 `DATABASE_URL`，避免開發機 `.env` 的 Supabase 連線被誤用）。
  - `SECRET_KEY` 讀自 `secrets\secret_key`（首次啟動產生，檔案 ACL 只允許目前 Windows 帳號）。
  - `ALLOWED_HOSTS` 預設為 `127.0.0.1`、`localhost`；開放區域網路時加入選定的位址。
  - `INSTALLED_APPS` 加入 `node`、`organizations`、allauth 相關 app；URL 只在此模式掛載 `/setup/`、`/node/…`、allauth 路由。
  - 顧客公開註冊與公開首頁不掛載；根路徑導向主控台總覽。
- `cloud` 模式維持現況，不載入上述 app 與路由。
- CI 新增一個以 `DEPLOYMENT_MODE=node` 執行全部測試的 job。

## 2. 執行方式與啟動器

- 網頁伺服器：**cheroot**（純 Python 正式 WSGI 伺服器，Windows 可原生提供 HTTPS）。預設綁定 `127.0.0.1:8750`，
  被占用時依序嘗試下一個埠，實際網址寫入日誌與系統匣選單。
- 同一個 EXE 以參數切換角色：
  - 無參數：啟動器（系統匣＋網頁伺服器＋管理 Worker 子程序）
  - `--worker`：分析 Worker 子程序（沿用 `run_analysis_worker` 的佇列、租約、心跳）
  - `--legacy-workbench`：現有 Dear PyGui 工作台（子專案 3 完成前保留）。以 `cloud` 模式設定與外部 `.env` 的
    `DATABASE_URL` 連 Supabase，維持現有「本機分析、發布到 Render 展示」流程；不使用本機節點的 SQLite。
  - `--smoke-test`：打包驗證
- 系統匣（`pystray`＋`Pillow`）選單：開啟主控台、查看日誌、開機自動啟動（切換）、結束。
- Worker 子程序由啟動器監督：異常結束時自動重啟，5 分鐘內最多 3 次，超過即停止並在總覽顯示警示。
  Worker 每次輪詢更新 `run\worker.heartbeat` 的時間戳。
- 資料目錄（皆位於 `%LOCALAPPDATA%\FeedbackInsightHub\`）：

| 路徑 | 內容 | 權限 |
|---|---|---|
| `data\` | `node.sqlite3` | 目前帳號 |
| `datartifacts` | Worker 版本化分析產物 | 目前帳號 |
| `secrets\` | `secret_key` | 僅目前帳號 |
| `tls\` | 自簽憑證與私鑰 | 僅目前帳號 |
| `setup\token` | 一次性設定權杖（設定完成即刪除） | 僅目前帳號 |
| `run\` | `worker.heartbeat`、實際使用的埠 | 目前帳號 |
| `logs\` | 既有輪替日誌 | 目前帳號 |

- 依賴：`cheroot`、`pystray`、`Pillow`、`keyring` 放入新的 `requirements-node.txt`（`requirements-desktop.txt` 引用它）；
  `django-allauth[mfa]` 放入 `requirements.txt`，讓兩種模式的 CI 都能安裝。實作第一步先確認 allauth 與 Django 6.0 相容的版本並釘選。

## 3. 首次設定

- 沒有任何擁有者時，所有主控台頁面導向 `/setup/`。
- 啟動器產生隨機設定權杖寫入 `setup\token`，並以 `http://127.0.0.1:<port>/setup/?token=…` 開啟瀏覽器。
  `/setup/` 只接受本機來源、以常數時間比對權杖；權杖錯誤或不存在時顯示「請從系統匣重新開啟設定」，不回顯權杖。
- 步驟：建立擁有者（Email、密碼，沿用 Django 密碼規則）→ 可選擇連結 Google 帳號 → 設定組織名稱 → 檢查資料庫與資料目錄。
- 完成後刪除權杖檔、記錄 `setup_completed_at`，`/setup/` 之後回應 404。

## 4. 登入與帳號

- 採 **django-allauth**：`account`（本機帳號）、`socialaccount` 的 Google provider、`mfa`（TOTP＋恢復碼）、內建登入速率限制。
  沿用 `accounts.User`；`node` 模式關閉公開註冊。
- **Google 登入**：使用 Google「桌面應用程式」用戶端＋PKCE，導回 `http://127.0.0.1:<port>/…`。只在本機來源的請求提供此按鈕；
  區域網路來源只提供帳號密碼登入。用戶端 ID 由設定提供，不視為機密。
- **只有受邀或已存在的 Email 可以用 Google 登入**：以 allauth adapter 在社群登入前檢查；未受邀者顯示「這個 Email 未受邀」。
  Google 無法連線時，本機帳號密碼登入仍可用。
- **邀請**：擁有者在「成員」頁輸入 Email 與角色產生邀請；受邀者可用連結設定密碼，或在本機以同 Email 的 Google 帳號登入。
  邀請有效期 7 天、可撤銷。
- **角色（本規格範圍）**：組織層級的「擁有者」與「組織管理員」（權限見架構總覽）；部門層級角色由 2b 加入。
  組織只能有一位擁有者，擁有權可轉移（需重新驗證）。
- **工作階段**：閒置逾時預設 4 小時（`SESSION_SAVE_EVERY_REQUEST`）；區域網路模式下 Cookie 加上 Secure。
- **重新驗證**：開放區域網路、變更金鑰、轉移擁有權、移除成員時要求重新輸入密碼（allauth reauthentication）。
- **兩步驟驗證**：任何人可自行啟用；開放區域網路的前提是擁有者已啟用。

## 5. 組織資料

- 新增 app `organizations`，模型 `Organization(name, created_at)`；`node` 模式恰有一筆（首次設定時建立）。
  組織層級角色以成員關聯表 `OrganizationMembership(user, organization, role)` 表示（`owner`／`admin`）。
  2b 在此 app 內加入 `Department` 與部門成員關聯，不另起模型。

## 6. 區域網路模式

- 只有擁有者可開啟，需重新驗證且已啟用兩步驟驗證。
- 流程：選擇一個網路介面位址 → 以 `cryptography` 產生自簽憑證（SAN 含該位址與電腦名稱）→ 重啟伺服器改用 HTTPS 綁定該位址
  （本機 `127.0.0.1` 同時保留）→ 請求 Windows 防火牆放行此埠（需系統管理員確認）→ 顯示連線網址與憑證 SHA-256 指紋。
- 設定同步更新 `ALLOWED_HOSTS` 與 `CSRF_TRUSTED_ORIGINS`；關閉區域網路時還原並移除防火牆規則。
- 設定存於 `node.NodeInstallation`（單例）：`setup_completed_at`、`lan_enabled`、`lan_address`、`lan_port`、`tls_fingerprint`。

## 7. 機密保管

- Gemini API 金鑰、日後的雲端連線權杖與 PostgreSQL 密碼存於 Windows 認證管理員（`keyring`，DPAPI）。
  不寫入 `.env`、不以明文存入資料庫；介面只顯示是否已設定及末四碼。
- `SECRET_KEY` 與 TLS 私鑰存於限制權限的檔案（見第 2 節）。

## 8. 稽核紀錄

- `node.NodeAuditEvent(actor, action, target, ip, details, created_at)`，只能新增（模型層禁止更新與刪除，Admin 唯讀）。
- 記錄：登入成功與失敗、鎖定、邀請／撤銷、角色變更、擁有權轉移、兩步驟驗證啟用／停用、開放／關閉區域網路、金鑰變更、設定完成。

## 9. 主控台頁面

- 沿用管理端外框與 `ui.css`；側邊欄只顯示已完成且目前角色有權限的項目（未完成頁面隱藏）。
- 本規格提供：**總覽**、**成員**、**設定**（組織名稱、區域網路、金鑰、安全性），並將既有的問卷與分析結果頁接入外框。
- 總覽：資料庫（連線、檔案大小）、Worker（子程序狀態、最後心跳）、磁碟剩餘空間、區域網路狀態與網址、雲端連線（此階段固定為「未連線」）、
  待處理事項（擁有者未啟用兩步驟驗證、磁碟空間不足、Worker 已停止重啟）、最近 10 筆稽核事件。
- 帳號選單：個人資料、兩步驟驗證、登出。所有頁面支援手機寬度。

## 10. 錯誤處理

- 埠被占用：自動換埠並記錄實際網址。
- 資料庫鎖定或損毀：顯示說明頁與「開啟資料夾」「查看日誌」入口，不顯示空白頁。
- Worker 重複崩潰：停止重啟並在總覽警示。
- Google 登入：分別提示未受邀、使用者取消、網路無法連線。
- 設定權杖無效：提示從系統匣重開，不洩漏權杖。

## 11. 測試

- 首次設定：無權杖／錯誤權杖被拒、權杖只能用一次、非本機來源被拒、完成後 `/setup/` 為 404。
- 本機登入、失敗鎖定、閒置逾時；兩步驟驗證啟用與驗證；重新驗證的敏感操作。
- Google 登入以模擬的 OAuth 回應測試：受邀 Email 可登入、未受邀被拒；區域網路來源不顯示 Google 按鈕。
- 角色：擁有者與組織管理員對成員、設定、區域網路的存取（允許／403）；擁有權轉移。
- 區域網路：未啟用兩步驟驗證無法開放；`ALLOWED_HOSTS`／`CSRF_TRUSTED_ORIGINS` 隨開關更新；憑證指紋計算。
  防火牆與伺服器重啟以可替換的介面測試，不實際修改系統。
- 稽核紀錄不可更新與刪除。
- 模式隔離：`cloud` 模式下本機節點網址為 404；`node` 模式下無公開註冊。CI 以兩種模式各跑一次全部測試。
- 啟動器：換埠、Worker 重啟上限為可單元測試的函式。
- 打包 EXE 並 smoke test，於開發機實際走過一次首次設定與登入。
