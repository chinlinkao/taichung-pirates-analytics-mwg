# 🏴‍☠️ 台中小海盜棒球數據庫 (Taichung Pirates Baseball Analytics)

本專案針對「台中就是棒社區棒球聯賽 (2024–2026)」的台中小海盜隊（U8 / U10 / U12 / U15），建立完整的數據爬取、清洗解析、SQLite 入庫、靜態 JSON 匯出，以及零冷啟動的現代化靜態響應式戰情看板。

---

## 🌟 功能特點

1. **數據爬取與快取 (`src/fetch.py`)**：安全合規爬取 myscore.games 賽事紀錄，落實本地 HTML 快取保護與 `>= 1.5` 秒請求間隔。
2. **清洗、入庫與驗算 (`src/parse.py`, `src/db.py`, `src/verify.py`)**：
   - 投球局數精確轉換為整數出局數（`outs = round(float(ip) * 3)`）儲存。
   - 嚴格以 `player_uid` 跨屆歸戶，精準區分同名同姓球員（例如 `蔡〇宇_2015-11` 與 `蔡〇宇_2017-06`）。
   - 保留 0 局責失紀錄並於合併計算時累加，杜絕漏計失分。
3. **靜態 JSON 匯出 (`src/export_json.py`)**：
   - 從唯讀 SQLite 資料庫匯出結構化靜態檔案至 `web/public/data/pirates.json`（含預先判定海盜內戰視角的 `pirate_side` 與 SQLite Views 校驗基準）。
4. **MWG 現代靜態前端戰情看板 (`web/`)**：
   - 採用 **Vite + Vanilla TypeScript + Modern Web Guidance (MWG 1.0)** 實作，支援亮／暗色主題自動切換、手機原生抽屜篩選、可排序響應式數據表、IME 安全球員搜尋選單、Chart.js 歷屆成長趨勢圖與 URL Query 分享連結（如 `?tab=player&uid=蔡〇宇_2015-11&era=7`）。

---

## 🚀 快速上手 (Quick Start)

### 1. 建立 Python 虛擬環境與執行數據管線

```powershell
# 建立並啟動虛擬環境 (Python 3.11+)
python -m venv .venv
.venv\Scripts\Activate.ps1

# 安裝後端爬蟲與管線依賴
pip install -r requirements-crawler.txt

# 離線重構入庫、驗算並匯出前端靜態 JSON（不發出網路請求）
.venv\Scripts\python.exe run_pipeline.py --parse-only

# 日常更新 2026 賽事並推送 GitHub 觸發自動部署（Windows 可直接雙擊）
update_all.bat
```

### 2. 啟動新版 MWG 前端看板 (`web/`)

```powershell
# Windows 可直接雙擊啟動本機開發預覽（自動開啟瀏覽器）：
run_web_dev.bat

# 或手動執行（需 Node.js 20+）：
cd web
npm install
npm run dev
```

### 3. 前端驗證閘道（開發與測試）

```powershell
cd web
npm run typecheck   # tsc --noEmit 型別檢查
npm test            # Vitest 棒球算式與 SQLite Views 基準對齊測試
npm run build       # Vite 正式環境靜態打包 (輸出至 web/dist)
```

---

## 📂 專案結構說明

```text
├── .github/
│   └── workflows/
│       └── deploy-web.yml         # GitHub Actions 自動建置 web/ 並部署 GitHub Pages
├── .gitignore                     # 忽略 .venv、data/raw_html、web/node_modules 等
├── AGENTS.md                      # 專案規範、棒球數據模型鐵則與驗證閘道
├── IMPLEMENTATION_PLAN_MWG.md     # MWG 現代前端遷移實行計畫
├── requirements-crawler.txt       # Python 爬蟲與管線依賴清單
├── run_pipeline.py                # 數據管線總入口 (爬蟲 → 解析 → verify → export_json)
├── run_web_dev.bat                # Windows 一鍵啟動新版 MWG 前端預覽腳本
├── update_all.bat                 # 一鍵更新最新賽果、匯出 JSON 並推送 GitHub
├── OLD/                           # 封存之舊版腳本與歷史文件
├── src/
│   ├── db.py                      # SQLite Schema 與生涯統計 Views
│   ├── discovery.py               # 自動探索新賽事與增量更新種子 CSV
│   ├── fetch.py                   # 禮貌網頁爬蟲與本地快取
│   ├── parse.py                   # 比賽攻守與球員數據解析
│   ├── seed_loader.py             # 種子 CSV 清單讀取模組
│   ├── verify.py                  # 數據邏輯與邊界驗算腳本
│   └── export_json.py             # 靜態 JSON 匯出器 (產出 web/public/data/pirates.json)
├── data/
│   ├── pirates.db                 # SQLite 資料庫
│   └── 海盜隊網頁標題.csv         # 聯賽種子清單
└── web/                           # 新版 MWG 現代靜態前端 (Vite + Vanilla TypeScript)
    ├── index.html
    ├── package.json
    ├── vite.config.ts
    ├── public/data/pirates.json   # 靜態數據（由 export_json.py 自動產生並納入版控）
    └── src/
        ├── core/                  # 棒球計算引擎、篩選聚合、CSV 匯出、URL Query 狀態同步
        ├── components/            # Header、FilterBar(<dialog>)、Tabs、DataTable、Combobox、Charts
        ├── views/                 # 四大戰情視圖 (總覽 / 排行榜 / 球員成長 / 對手分析)
        ├── styles/                # tokens / reset / layout / components CSS
        └── tests/stats.test.ts    # Vitest 單元與 SQLite Views 一致性測試
```

---

## ☁️ 雲端自動部署（GitHub Pages）

本儲存庫已內建 [.github/workflows/deploy-web.yml](.github/workflows/deploy-web.yml)：
1. 前往 GitHub Repository 的 **Settings → Pages**。
2. 在 **Build and deployment → Source** 選擇 **GitHub Actions**。
3. 每次執行 `update_all.bat` 或推送至 `main` 分支時，GitHub Actions 會自動執行 `npm ci → typecheck → test → build` 並發布至：
   - **線上網址**：https://chinlinkao.github.io/taichung-pirates-analytics/
