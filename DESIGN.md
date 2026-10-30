---
name: 回饋洞察 AI 平台
description: 用統計抓住關鍵、用分析抓住決策、用反饋提升認同
colors:
  forest-ink: "#133b2b"
  forest: "#22614a"
  forest-bright: "#2c7659"
  sage-slate: "#4f6f61"
  sage-mist: "#c8dacd"
  sage-wash: "#eef5f0"
  paper: "#f6f4ee"
  sheet: "#ffffff"
  sheet-warm: "#faf9f5"
  sheet-muted: "#f0ede5"
  rule: "#e6e1d6"
  rule-strong: "#d5cebf"
  ink: "#1c1b18"
  ink-soft: "#45413a"
  ink-muted: "#676257"
  ink-faint: "#8a857a"
  on-forest: "#fbfaf6"
  ochre-deep: "#7a5a0a"
  ochre: "#b58b19"
  ochre-wash: "#fbf4e1"
  brick-deep: "#8a2f22"
  brick: "#b44732"
  brick-wash: "#fbefec"
typography:
  display:
    fontFamily: "Noto Sans TC, Segoe UI, Microsoft JhengHei, sans-serif"
    fontSize: "clamp(50px, 5.3vw, 76px)"
    fontWeight: 700
    lineHeight: 1.08
    letterSpacing: "-0.045em"
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
    backgroundColor: "{colors.forest}"
    textColor: "{colors.on-forest}"
    rounded: "{rounded.sm}"
    padding: "9px 18px"
    height: "40px"
  button-primary-hover:
    backgroundColor: "{colors.forest-bright}"
    textColor: "{colors.on-forest}"
  button-ghost:
    backgroundColor: "{colors.sheet}"
    textColor: "{colors.ink-soft}"
    rounded: "{rounded.sm}"
    padding: "9px 18px"
    height: "40px"
  button-ghost-hover:
    backgroundColor: "{colors.sheet-warm}"
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
    textColor: "{colors.on-forest}"
    rounded: "{rounded.sm}"
    padding: "10px 12px"
  tab-active:
    backgroundColor: "{colors.forest}"
    textColor: "{colors.on-forest}"
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

視覺上是一本整理得很好的營運筆記：溫和的紙色底（paper）、白色紙張（sheet）承載內容、深森林綠作為唯一的品牌聲音。密度屬於工作工具而非行銷頁：清楚層級、表格數字等寬對齊、說明文字退到次要灰階。品牌感不靠裝飾，靠精準的細節：對齊、留白節奏、數字的排印。

層次以平面為主：內容靠淡邊框與底色區分，陰影只輕輕托起白紙，並在懸停、彈出訊息等「狀態」時才加深。元件手感沉穩內斂：小圓角、綠色主按鈕、140ms 的快速過場，不跳動、不浮誇。

**Key Characteristics:**
- 紙色底＋白紙內容＋森林綠單一品牌色
- 關鍵數字大而等寬，說明文字小而退後
- 平面為主，陰影只表達狀態
- 小圓角、沉穩過場、無位移動畫
- 全站繁體中文排版，Noto Sans TC 為主字體

## Colors

溫暖紙色中性底，森林綠是唯一品牌聲音，赭黃與磚紅只用來表達警示與錯誤。

### Primary
- **Forest Ink 深林墨綠** (forest-ink)：管理端側邊欄底色、最深的品牌面積；只用在導覽外框這種「框住工作區」的地方。
- **Forest 森林綠** (forest)：主按鈕、作用中的分頁標籤、關鍵連結。每個畫面主要行動的顏色。
- **Forest Bright 亮森林綠** (forest-bright)：主按鈕懸停、輸入框聚焦邊框、焦點光圈的色源。
- **Sage Slate 灰鼠尾草** (sage-slate)：區段小標（kicker）等低調的品牌提示。
- **Sage Mist / Sage Wash 鼠尾草霧／淡洗** (sage-mist, sage-wash)：選取反白、卡片懸停邊框、品牌色的淡底提示。

### Neutral
- **Paper 紙色** (paper)：頁面底色。
- **Sheet / Sheet Warm / Sheet Muted 白紙／暖白紙／霧面紙** (sheet, sheet-warm, sheet-muted)：卡片與面板、次要面板、chip 與懸停底。
- **Rule / Rule Strong 細線／粗線** (rule, rule-strong)：卡片邊框與表格分隔；輸入框與 ghost 按鈕邊框。
- **Ink 墨色四階** (ink, ink-soft, ink-muted, ink-faint)：主文字、次要文字、說明文字、最弱提示。
- **On Forest 綠底白** (on-forest)：森林綠上的文字。

### Status
- **Ochre 赭黃三階** (ochre-deep, ochre, ochre-wash)：警告、中立情緒、待處理提醒。
- **Brick 磚紅三階** (brick-deep, brick, brick-wash)：錯誤、失敗、負面情緒。
- 正面情緒用森林綠系（目前程式內為 `#2f8a5b`，尚未收進 token）。

### Named Rules
**The One Voice Rule.** 品牌色只有森林綠一系。新增圖表、徽章或插圖不得引入第二個品牌色；文字雲與圖表配色也必須從森林綠、赭黃、磚紅與墨色推導。

**The Color Means Something Rule.** 顏色只用來表達意義（行動、狀態、情緒），不用來裝飾。卡片家族一律同一條淡邊框，「顏色只承載意義」。

## Typography

**Body Font:** Noto Sans TC（備用 Segoe UI、Microsoft JhengHei、sans-serif）
**Label/Mono Font:** IBM Plex Mono（備用 Consolas、monospace）

**Character:** 單一中文無襯線字族撐起所有層級，靠字重與大小建立層次；等寬字只用在小標與代碼，給介面一點「筆記本的索引」感。

### Hierarchy
- **Display** (700, clamp(50px, 5.3vw, 76px), 1.08)：只用在公開首頁主標，最多 14 字寬。
- **Metric** (700, 2rem, 1.15, tabular-nums)：關鍵數字卡的主數字。這是「抓住關鍵」的主角。
- **Headline** (700, 1.25rem, 1.3)：管理端頂欄的頁面標題。
- **Title** (700, 1.15rem)：面板與區段標題。
- **Body** (400, 15px, 1.65)：內文；說明段落限寬 72ch。
- **Label** (600, 0.72rem, 0.12em, 大寫)：區段小標（kicker），使用等寬字與灰鼠尾草色。表頭為 600、0.8rem、ink-muted。

### Named Rules
**The Tabular Numbers Rule.** 表格、指標卡與任何會被比較的數字都用 `font-variant-numeric: tabular-nums`，讓數字對齊、可以直向比較。

**The Plain Chinese Rule.** 介面文字一律白話繁體中文；中英文混排時英文與數字前後保留半形空格。

## Layout

- **管理端**：左側 248px 固定側邊欄＋右側工作區；頂欄吸附並帶半透明紙色模糊底；內容區最寬 1320px，內距 28px 32px 56px。
- **公開頁**：內容容器 `min(1180px, 100% - 32px)` 置中。
- **間距節奏**：4／8／16／24／32px；卡片間距 16px，面板內距 24px。
- **指標卡格線**：`repeat(auto-fit, minmax(200px, 1fr))`，寬螢幕一列排完，自然換行。
- **斷點**：1100px（內距縮為 22px 20px）、860px（側邊欄改為頂部橫向捲動導覽，頁尾資訊隱藏）。
- 有錨點的區塊預留 140px 捲動邊界，避免被吸附的頂欄與分頁列遮住。

## Elevation & Depth

平面為主的混合系統：靜止時靠淡邊框與紙色層次區分，陰影只用來「托起白紙」與表達狀態。

### Shadow Vocabulary
- **Rest** (`box-shadow: 0 1px 2px rgba(28, 27, 24, 0.05)`)：面板、卡片、主按鈕的靜止狀態。
- **Raised** (`box-shadow: 0 4px 16px rgba(28, 27, 24, 0.06), 0 1px 3px rgba(28, 27, 24, 0.04)`)：卡片懸停、吸附的分頁列。
- **Floating** (`box-shadow: 0 16px 40px rgba(28, 27, 24, 0.08)`)：彈出訊息（flash）等浮在內容之上的元素。

### Named Rules
**The Flat-By-Default Rule.** 表面靜止時是平的；陰影加深只回應狀態（懸停、浮出、聚焦），從不當裝飾。

## Shapes

小而一致的圓角：8px 用在按鈕、輸入框、分頁標籤；12px 用在列表卡與分頁列；16px 用在面板與指標卡；20px 保留給大型容器；999px 只給 chip 與徽章。邊框一律 1px 細線，狀態色以左側 4px 色條表達（flash 訊息、情緒列）。

## Components

### Buttons
- **Shape:** 小圓角（8px），高度 40px；小型 34px。
- **Primary:** 森林綠底、綠底白字、Rest 陰影，內距 9px 18px，字重 500。
- **Hover / Focus:** 懸停轉亮森林綠，不位移；聚焦顯示 3px 森林綠光圈。
- **Ghost:** 白底、粗線邊框、次要墨色；懸停轉暖白紙與更深邊框。
- **Quiet:** 透明底、說明墨色；懸停霧面紙底。
- **Disabled:** 透明度 50%，游標 not-allowed。

### Chips
- **Style:** 霧面紙底、說明墨色、膠囊圓角、0.74rem 字重 600。用於分類標籤，不可點擊時不得長得像按鈕。

### Cards / Containers
- **Corner Style:** 面板與指標卡 16px；列表卡 12px。
- **Background:** 白紙。
- **Shadow Strategy:** Rest；可點擊的卡片懸停時轉 Raised，邊框轉鼠尾草霧。
- **Border:** 1px 細線。
- **Internal Padding:** 面板 24px；指標卡 20px 22px；列表卡 14px 16px。

### Inputs / Fields
- **Style:** 白底、粗線邊框、8px 圓角。
- **Focus:** 邊框轉亮森林綠並加焦點光圈，不用瀏覽器預設外框。

### Navigation
- **Style:** 管理端側邊欄為深林墨綠底；連結 0.94rem、半透明綠底白字。
- **States:** 懸停加 7% 白色底；作用中加 12% 白色底與左側 3px 淺綠內條（`#8fc4a8`）。
- **Mobile:** 860px 以下改為頂部橫向捲動的連結列。

### Metric Card（Signature）
「抓住關鍵」的主要載體：上方 0.86rem 說明標籤、中間 2rem 等寬主數字、下方 0.82rem 最弱提示。一張卡只說一個數字。

### Flash Message
白紙、12px 圓角、左側 4px 狀態色條（成功森林綠、警告赭黃、錯誤磚紅），Floating 陰影。

## Do's and Don'ts

### Do:
- **Do** 每個頁面先放關鍵數字或關鍵結論，再放細節（關鍵 → 決策 → 認同）。
- **Do** 讓分析頁的終點是一個明確行動（建立改善項目），讓改善的終點是通知顧客。
- **Do** 新樣式只引用 `app.css` `:root` 的 `--c-*`、`--radius-*`、`--shadow-*`、`--font-*` token。
- **Do** 數字一律 tabular-nums；中英文混排保留空格。
- **Do** 尊重 `prefers-reduced-motion`；過場維持 140ms ease，只變色不位移。

### Don't:
- **Don't** 在店家或顧客看得到的文字使用開發者用語（Supabase、snapshot、唯讀展示、版本號細節）。
- **Don't** 引入第二個品牌色；文字雲現行的舊橘／藍配色、公開頁底部的橘色放射漸層都屬於待清除的舊樣式。
- **Don't** 新增舊別名變數（`--bg`、`--brand`、`--surface` 等）的用法；它們只為相容舊規則而保留。
- **Don't** 讓公開／顧客頁的樣式影響管理頁（管理頁規則一律收在 `.manager-shell` 之下）。
- **Don't** 在靜止狀態使用 Raised 或 Floating 陰影。
