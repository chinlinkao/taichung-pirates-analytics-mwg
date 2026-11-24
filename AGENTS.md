# 台中小海盜數據專案規範 (AGENTS.md)

## 1. 執行環境與語言
- 後端管線使用 Python 3.11+（`.venv` 虛擬環境）；前端使用 Node.js 20+、Vite 與 Vanilla TypeScript（**嚴禁引入 React / Vue / Angular 等重型框架**）。
- 所有程式碼註解、Commit 訊息、介面文案與回覆一律使用**繁體中文**。
- CSV 讀取與匯出一律使用 `encoding="utf-8-sig"`（含 BOM，確保 Excel 開啟不亂碼）；SQLite 連線必須開啟 `PRAGMA foreign_keys = ON;`。

## 2. 爬蟲安全紅線（Python 後端）
- 對 `myscore.games` 採單線程循序請求，每次請求間隔必須 `>= 1.5` 秒，嚴禁高併發。
- 下載之原始網頁一律先快取至 `data/raw_html/`，修改或重跑 `src/parse.py` 時只能讀取本地快取，禁止重新請求網站。

## 3. 棒球數據模型鐵則（前後端共同遵守）
- **球員唯一識別**：跨隊／跨屆歸戶與前端狀態綁定一律使用 `player_uid`（`{masked_name}_{birth_ym}` 或 `id:{site_player_id}`），嚴禁僅用遮罩姓名合併數據（必須能精準區分同隊同名選手，例如 `蔡〇宇_2015-11` 與 `蔡〇宇_2017-06`）。
- **投球局數整數化**：入庫與 JSON 傳輸一律使用整數出局數 `outs = round(float(ip) * 3)`，嚴禁以小數局數直接相加。前端顯示局數一律由 `Math.floor(outs / 3)` 與 `outs % 3` 組合為 `X.Y`（如 `10.1`）。
- **0 局責失防呆**：合併跨季 ERA 時，必須累加所有紀錄的 `er` 與 `outs`（包含 `outs === 0 && er > 0` 如 `林〇晏` 之紀錄），不得先過濾掉 0 局紀錄。當總 `outs === 0` 時，ERA / WHIP / K7 一律回傳 `null`（UI 顯示 `-`）。
- **海盜內戰判定**：比對場次勝負時，必須優先精準比對 `away_team === team_name` / `home_team === team_name`，不可僅憑字串包含 `'海盜'` 判斷，避免白隊對戰黑隊時勝負反置。
- **排除教練拋打**：投手排行榜與球員檔案名單必須排除 `player_uid === 'coach_pitch_888'`。

## 4. 現代前端與 MWG 1.0 開發規範（`web/`）
- **MWG 優先原則**：實作任何 UI 模式（Tabs、Sortable Table、Responsive Table、Dialog/Popover、Dark Mode、Form-factor 適配、搜尋輸入）前，必須先查詢已安裝的 **Modern Web Guidance (MWG)** 技能指引，優先採用原生 HTML/CSS/Web Platform API。
- **瀏覽器相容基準**：以 **Baseline Widely available** 為預設目標（支援 Chrome、Safari/iOS WebKit、Firefox、Edge），若使用較新特性必須提供漸進增強 Fallback。
- **零外部寫入與純靜態部署**：前端僅唯讀載入靜態 `public/data/pirates.json`，不依賴後端 API；Vite `base` 必須設定為 `'./'` 以相容 GitHub Pages 子路徑部署。
- **無障礙與中文輸入體驗**：所有互動元件須支援鍵盤操作與語意化 ARIA；中文搜尋框須處理 IME 組合事件（`isComposing`），避免注音選字誤觸。

## 5. 強制驗證閘道（Verification Gates）
- 修改 Python 解析或資料匯出邏輯後，必須執行並通過：
  - `.venv\Scripts\python.exe src/verify.py`
  - `.venv\Scripts\python.exe src/export_json.py`
- 修改 `web/` 前端程式後，必須執行並全數通過以下檢查才算完成：
  - `cd web && npm run typecheck`（`tsc --noEmit` 零錯誤）
  - `cd web && npm test`（Vitest 棒球算式與 SQLite Views 對齊測試全過）
  - `cd web && npm run build`（Vite 正式建置成功）
  - `npx modern-web-guidance@latest legacy-scan ./web/src`（確認無過時或不合規寫法）