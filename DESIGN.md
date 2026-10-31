---
name: 回饋洞察 AI 平台
description: 用統計抓住關鍵、用分析抓住決策、用反饋提升認同
colors:
  navy-ink: "#0b1f33"
  green: "#0d7a52"
  green-deep: "#0a6343"
  green-mist: "#bfe3cf"
  green-wash: "#e5f4ec"
  canvas: "#f5f7fa"
  sheet: "#ffffff"
  sheet-subtle: "#f9fafb"
  sheet-muted: "#f1f4f7"
  rule: "#e2e8ee"
  rule-strong: "#cfd8e1"
  ink: "#0b1f33"
  ink-soft: "#34485c"
  ink-muted: "#56687a"
  ink-faint: "#7b8b9a"
  ochre-deep: "#8a5a00"
  ochre: "#c58a0b"
  ochre-wash: "#fdf6e3"
  brick-deep: "#a8321f"
  brick: "#c2412d"
  brick-wash: "#fdeeea"
  public-ink: "#0b1f33"
  public-ink-soft: "#34485c"
  public-ink-muted: "#56687a"
  public-line: "#e2e8ee"
  public-band: "#f5f7fa"
  store-green: "#0d7a52"
  store-green-deep: "#0a6343"
  store-green-soft: "#e5f4ec"
  customer-amber: "#b4530a"
  customer-amber-deep: "#93430a"
  customer-amber-soft: "#fdf0e1"
typography:
  display:
    fontFamily: "Noto Sans TC, Segoe UI, Microsoft JhengHei, sans-serif"
    fontSize: "clamp(28px, 3.5vw, 50px)"
    fontWeight: 900
    lineHeight: 1.14
    letterSpacing: "-0.02em"
  metric:
    fontFamily: "Noto Sans TC, Segoe UI, Microsoft JhengHei, sans-serif"
    fontSize: "2rem"
    fontWeight: 700
    lineHeight: 1.15
    letterSpacing: "-0.02em"
    fontFeature: "tnum"
  headline:
    fontFamily: "Noto Sans TC, Segoe UI, Microsoft JhengHei, sans-serif"
    fontSize: "1.25rem"
    fontWeight: 700
    lineHeight: 1.3
    letterSpacing: "0.01em"
  title:
    fontFamily: "Noto Sans TC, Segoe UI, Microsoft JhengHei, sans-serif"
    fontSize: "1.15rem"
    fontWeight: 700
    letterSpacing: "0.01em"
  body:
    fontFamily: "Noto Sans TC, Segoe UI, Microsoft JhengHei, sans-serif"
    fontSize: "15px"
    fontWeight: 400
    lineHeight: 1.65
  label:
    fontFamily: "IBM Plex Mono, Consolas, monospace"
    fontSize: "0.72rem"
    fontWeight: 600
    letterSpacing: "0.12em"
rounded:
  sm: "8px"
  md: "12px"
  lg: "16px"
  xl: "20px"
  pill: "999px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "16px"
  lg: "24px"
  xl: "32px"
components:
  button-primary:
    backgroundColor: "{colors.green}"
    textColor: "{colors.sheet}"
    rounded: "{rounded.sm}"
    padding: "9px 18px"
    height: "40px"
  button-primary-hover:
    backgroundColor: "{colors.green-deep}"
    textColor: "{colors.sheet}"
  button-ghost:
    backgroundColor: "{colors.sheet}"
    textColor: "{colors.ink-soft}"
    rounded: "{rounded.sm}"
    padding: "9px 18px"
    height: "40px"
  button-ghost-hover:
    backgroundColor: "{colors.sheet-subtle}"
    textColor: "{colors.ink}"
  button-quiet:
    backgroundColor: "transparent"
    textColor: "{colors.ink-muted}"
    rounded: "{rounded.sm}"
  input:
    backgroundColor: "{colors.sheet}"
    textColor: "{colors.ink}"
    rounded: "{rounded.sm}"
  panel:
    backgroundColor: "{colors.sheet}"
    rounded: "{rounded.lg}"
    padding: "24px"
  metric-card:
    backgroundColor: "{colors.sheet}"
    rounded: "{rounded.lg}"
    padding: "20px 22px"
  nav-link:
    textColor: "{colors.sheet}"
    rounded: "{rounded.sm}"
    padding: "10px 12px"
  tab-active:
    backgroundColor: "{colors.green}"
    textColor: "{colors.sheet}"
    rounded: "{rounded.sm}"
  chip:
    backgroundColor: "{colors.sheet-muted}"
    textColor: "{colors.ink-muted}"
    rounded: "{rounded.pill}"
    padding: "2px 8px"
---

# Design System: 回饋洞察 AI 平台

## Overview

**Creative North Star: "關鍵 → 決策 → 認同"**

> 用統計抓住關鍵、用分析抓住決策、用反饋提升認同。

整套介面服務一條路徑：先讓店家一眼看到**關鍵數字**，再把分析收斂成**一個可以做的決策**，最後把改善結果**回饋給顧客**，換來顧客的認同。每個頁面都要能回答「它在這條路徑的哪一段」：統計頁的主角是數字，分析頁的主角是要做的改善，通知與顧客端的主角是「我們聽見了、我們改了」。

視覺上是一本整理得很好的營運筆記：冷灰底（canvas）、白色卡片（sheet）承載內容、店家綠作為唯一的品牌聲音。密度屬於工作工具而非行銷頁：清楚層級、表格數字等寬對齊、說明文字退到次要灰階。品牌感不靠裝飾，靠精準的細節：對齊、留白節奏、數字的排印。

層次以平面為主：內容靠淡邊框與底色區分，陰影只輕輕托起白色，並在懸停、彈出訊息等「狀態」時才加深。元件手感沉穩內斂：小圓角、綠色主按鈕、140ms 的快速過場，不跳動、不浮誇。

**Key Characteristics:**
- 冷灰底＋白色內容＋店家綠單一品牌色
- 關鍵數字大而等寬，說明文字小而退後
- 平面為主，陰影只表達狀態
- 小圓角、沉穩過場、無位移動畫
- 全站繁體中文排版，Noto Sans TC 為主字體

**公開頁是另一套視覺。** 首頁（`/`）與案例說明頁（`/case-study/`）採一般 SaaS 首頁做法（以 Stripe 的完成度為標竿）：白底與冷灰帶狀區、深海軍藍墨色、店家模式用綠、顧客模式用琥珀。規則見下方〈Public Pages〉，樣式只在 `static/css/landing.css`（`body.landing` 範圍內）。管理端與公開頁共用同一組顏色值（深海軍藍墨色、冷灰底、店家綠），見下方〈Colors〉。

## Colors

管理端（`app.css` 的 `:root` token）與首頁店家模式同一組顏色：冷灰底、白色卡片、深海軍藍墨色，店家綠是唯一品牌色；赭黃與磚紅只用來表達警示與錯誤。

### Primary
- **Green 店家綠** (green)：主按鈕、作用中的側欄項目與分頁、關鍵連結、趨勢圖的今天。每個畫面主要行動的顏色。
- **Green Deep 深綠** (green-deep)：主按鈕懸停。
- **Green Mist / Green Wash 淡綠** (green-mist, green-wash)：選取反白、作用中側欄項目的底、正向徽章底。

### Neutral
- **Canvas 冷灰底** (canvas)：頁面底色。
- **Sheet / Sheet Subtle / Sheet Muted 白／淡灰／霧灰** (sheet, sheet-subtle, sheet-muted)：卡片與側欄、表頭與懸停列、chip 與側欄懸停底。
- **Rule / Rule Strong 細線／粗線** (rule, rule-strong)：卡片邊框與表格分隔；輸入框與 ghost 按鈕邊框。
- **Ink 墨色四階** (ink, ink-soft, ink-muted, ink-faint)：主文字、次要文字、說明文字、最弱提示。

### Status
- **Ochre 赭黃三階** (ochre-deep, ochre, ochre-wash)：警告、中立情緒、待處理（例如「待分析」）。
- **Brick 磚紅三階** (brick-deep, brick, brick-wash)：錯誤、失敗、負面情緒、下降的變化百分比。
- 正面情緒用綠系（目前程式內為 `#2f8a5b`，尚未收進 token）。

### Named Rules
**The One Voice Rule.** 品牌色只有店家綠一系。新增圖表、徽章或插圖不得引入第二個品牌色；文字雲與圖表配色也必須從綠、赭黃、磚紅與墨色推導。顧客琥珀只屬於公開頁的顧客模式與顧客端。

**The Color Means Something Rule.** 顏色只用來表達意義（行動、狀態、情緒），不用來裝飾。卡片家族一律同一條淡邊框，「顏色只承載意義」。

## Typography

**Body Font:** Noto Sans TC（備用 Segoe UI、Microsoft JhengHei、sans-serif）
**Label/Mono Font:** IBM Plex Mono（備用 Consolas、monospace）

**Character:** 單一中文無襯線字族撐起所有層級，靠字重與大小建立層次；等寬字只用在小標與代碼，給介面一點「筆記本的索引」感。

### Hierarchy
- **Display** (900, clamp(28px, 3.5vw, 50px), 1.14)：只用在公開首頁主標，固定兩行，用 `<br>` 斷行；字級讓最長一行（11 字）放得下，不留單字成行。
- **Metric** (700, 2rem, 1.15, tabular-nums)：關鍵數字卡的主數字。這是「抓住關鍵」的主角。
- **Headline** (700, 1.25rem, 1.3)：管理端頂欄的頁面標題。
- **Title** (700, 1.15rem)：面板與區段標題。
- **Body** (400, 15px, 1.65)：內文；說明段落限寬 72ch。
- **Label** (600, 0.72rem, 0.12em, 大寫)：區段小標（kicker），使用等寬字與灰綠色。管理頁已不用英文小標。表頭為 600、0.8rem、ink-muted。

### Named Rules
**The Tabular Numbers Rule.** 表格、指標卡與任何會被比較的數字都用 `font-variant-numeric: tabular-nums`，讓數字對齊、可以直向比較。

**The Plain Chinese Rule.** 介面文字一律白話繁體中文；中英文混排時英文與數字前後保留半形空格。

## Layout

- **管理端**：左側 240px 白色側邊欄（右側細線）＋右側工作區；頂欄吸附並帶半透明白色模糊底；內容區最寬 1320px，內距 28px 32px 56px。
- **公開頁**：內容容器 `.lp-wrap` 為 `min(1180px, 100% - 48px)` 置中；斷點 1040px 與 720px。
- **間距節奏**：4／8／16／24／32px；卡片間距 16px，面板內距 24px。
- **指標卡格線**：`repeat(auto-fit, minmax(200px, 1fr))`，寬螢幕一列排完，自然換行。
- **斷點**：1100px（內距縮為 22px 20px）、860px（側邊欄改為頂部橫向捲動導覽，頁尾資訊隱藏）。
- 有錨點的區塊預留 140px 捲動邊界，避免被吸附的頂欄與分頁列遮住。

## Elevation & Depth

平面為主的混合系統：靜止時靠淡邊框與冷灰／白的層次區分，陰影只用來托起卡片與表達狀態。

### Shadow Vocabulary
- **Rest** (`box-shadow: 0 1px 2px rgba(11, 31, 51, 0.05)`)：面板、卡片、主按鈕的靜止狀態。
- **Raised** (`box-shadow: 0 1px 3px rgba(11, 31, 51, 0.06), 0 4px 12px rgba(11, 31, 51, 0.05)`)：卡片懸停、吸附的分頁列。
- **Floating** (`box-shadow: 0 12px 32px -12px rgba(11, 31, 51, 0.18)`)：彈出訊息（flash）等浮在內容之上的元素。

### Named Rules
**The Flat-By-Default Rule.** 表面靜止時是平的；陰影加深只回應狀態（懸停、浮出、聚焦），從不當裝飾。

## Shapes

小而一致的圓角：6px 用在按鈕、輸入框、分頁標籤、側欄項目；8px 用在列表卡與分頁列；12px 用在面板與指標卡；16px 保留給大型容器；999px 只給 chip 與徽章。邊框一律 1px 細線，狀態色以左側 4px 色條表達（flash 訊息、情緒列）。

## Components

### Buttons
- **Shape:** 小圓角（8px），高度 40px；小型 34px。
- **Primary:** 店家綠底、綠底白字、Rest 陰影，內距 9px 18px，字重 500。
- **Hover / Focus:** 懸停轉亮店家綠，不位移；聚焦顯示 3px 店家綠光圈。
- **Ghost:** 白底、粗線邊框、次要墨色；懸停轉淡灰與更深邊框。
- **Quiet:** 透明底、說明墨色；懸停霧灰底。
- **Disabled:** 透明度 50%，游標 not-allowed。

### Chips
- **Style:** 霧灰底、說明墨色、膠囊圓角、0.74rem 字重 600。用於分類標籤，不可點擊時不得長得像按鈕。

### Cards / Containers
- **Corner Style:** 面板與指標卡 16px；列表卡 12px。
- **Background:** 白色。
- **Shadow Strategy:** Rest；可點擊的卡片懸停時轉 Raised，邊框轉淡綠。
- **Border:** 1px 細線。
- **Internal Padding:** 面板 24px；指標卡 20px 22px；列表卡 14px 16px。

### Inputs / Fields
- **Style:** 白底、粗線邊框、8px 圓角。
- **Focus:** 邊框轉亮店家綠並加焦點光圈，不用瀏覽器預設外框。

### Navigation
- **Style:** 管理端側邊欄白底、右側細線；連結 0.92rem、說明墨色，上方是「回饋洞察／管理後台（或本機節點）」，底部顯示登入者。
- **States:** 懸停霧灰底、主墨色；作用中淡綠底、店家綠字、字重 600。
- **Mobile:** 860px 以下改為頂部橫向捲動的連結列。

### Metric Card（Signature）
「抓住關鍵」的主要載體：上方 0.86rem 說明標籤、中間 2rem 等寬主數字、下方 0.82rem 最弱提示。一張卡只說一個數字。

### Flash Message
白色、12px 圓角、左側 4px 狀態色條（成功店家綠、警告赭黃、錯誤磚紅），Floating 陰影。

## Do's and Don'ts

### Do:
- **Do** 每個頁面先放關鍵數字或關鍵結論，再放細節（關鍵 → 決策 → 認同）。
- **Do** 讓分析頁的終點是一個明確行動（建立改善項目），讓改善的終點是通知顧客。
- **Do** 新樣式只引用 `app.css` `:root` 的 `--c-*`、`--radius-*`、`--shadow-*`、`--font-*` token。
- **Do** 數字一律 tabular-nums；中英文混排保留空格。
- **Do** 尊重 `prefers-reduced-motion`；過場維持 140ms ease，只變色不位移。

### Don't:
- **Don't** 在店家或顧客看得到的文字使用開發者用語（Supabase、snapshot、唯讀展示、版本號細節）。
- **Don't** 在管理端引入第二個品牌色；文字雲現行的舊橘／藍配色屬於待清除的舊樣式。顧客琥珀只屬於公開頁顧客模式與顧客端，不得帶進管理端。
- **Don't** 新增舊別名變數（`--bg`、`--brand`、`--surface` 等）的用法；它們只為相容舊規則而保留。
- **Don't** 讓公開／顧客頁的樣式影響管理頁（管理頁規則一律收在 `.manager-shell` 之下）。
- **Don't** 在靜止狀態使用 Raised 或 Floating 陰影。

## Public Pages

首頁與案例說明頁共用 `templates/feedback/_landing_head.html`（字型與 `landing.css`）和 `_public_footer.html`。`app.css` 不放任何 `.lp-` 規則，`base.html`（管理端）不載入 `landing.css`。

### 雙模式首頁
- **一頁兩種讀者**：首屏有「給店家／給顧客」切換鈕，整頁內容跟著換；每個模式只講自己的重點，不寫「適用所有人」的通用文案。
- **切換機制**：`<main class="lp" data-mode="store|customer">`，區塊用 `data-mode-panel` 標記。沒有 JS 時切換鈕就是 `?mode=` 連結，由 `HomeView` 讀參數（預設 store）；有 JS 時 `static/js/landing.js` 原地切換、用 `history.replaceState` 改網址並記住選擇。
- **模式色**：`--lp-accent` 在店家模式是 store-green，在顧客模式是 customer-amber；按鈕、重點數字、首屏斜切色塊都跟著換。兩色都只當「目前是誰在看」的訊號，不另作裝飾。

### 色彩與層次
- 白底（paper）與冷灰帶狀區（public-band）交替切出段落；深色收尾帶用 public-ink。文字三階 public-ink／soft／muted，邊框 public-line。
- 首屏右側是一個大型產品畫面，壓在依模式著色的斜切色塊上（`clip-path` 多邊形），不再用整片淡綠底。

### 產品畫面（Signature）
- 每個區段最多一個產品畫面，內容要真的看得懂（分店比較表、AI 發現與被擋下的一項、手機上的處理進度），不放空白假卡片。
- 白底、12px 圓角、1px public-line 邊框、柔和的偏移陰影（`--lp-shadow`；首屏用 `--lp-shadow-lg`）。
- 每個產品畫面都標「示範資料」，頁尾再註明一次。

### 文案
- 首頁不出現開發者用語；架構、AI 數字驗證、統計方法與資料處理一律放案例說明頁。
- 案例說明頁是閱讀頁：左側吸附目錄、右側正文與比較表。

### 動態
- 只有切換模式時，新顯示的內容做一次 520ms 的淡入上移（`.lp.is-switched`）；首次載入不播。按鈕與連結過場 140ms 只變色。`prefers-reduced-motion` 下全部關閉。

## Customer Pages

顧客端（我的回饋 `/app/`、通知、填問卷與送出完成、帳號的個人資料／通知設定／變更密碼）沿用首頁顧客模式的視覺：白底卡片放在冷灰底上、海軍藍文字、琥珀色強調。樣式只在 `static/css/customer.css`（`body.cx` 範圍內），版型是 `templates/feedback/customer_base.html`，不載入 `app.css`。

- **手機優先**：內容欄最寬 720px。手機上主選單是底部三個分頁（我的回饋／通知／帳號），720px 以上改放在頂欄。
- **進度時間軸（Signature）**：每份問卷一張卡片，節點用顏色表達狀態：綠色實心是已發生，琥珀空心環是新通知，虛線是等待店家。店家寫給顧客的內容放在淡琥珀底的引言框。
- **填問卷**：一次一題，頂部有吸附的進度條；手機上「上一題／下一題」固定在畫面底部。選項是整列可點的按鈕，評分題是一排等寬按鈕，選中的評分填滿琥珀色。沒有 JS 時所有題目一起顯示，只有一個送出鈕。管理者預覽（`?preview=1`）不顯示頂欄。
- **表單不依賴 JS**：通知開關、標為已讀都是一般表單；`static/js/customer.js` 只負責開關一改就儲存，以及原地標為已讀。
- **文案**：不放英文小標，不提系統內部流程；空狀態要告訴顧客下一步怎麼做。
