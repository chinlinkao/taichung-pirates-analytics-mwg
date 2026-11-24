# 台中小海盜棒球數據庫 — MWG 現代前端遷移實行計畫 (IMPLEMENTATION_PLAN_MWG.md)

> **執行對象**：Antigravity (agy) Coding Agent  
> **架構決策**：方案 A — 靜態 JSON 匯出（`src/export_json.py`） + 現代標準前端（`web/`：Vite + Vanilla TypeScript + MWG 1.0 指南）  
> **遷移策略**：與既有 `src/app.py`（Streamlit）漸進共存，待新版前端通過全數驗收並完成 GitHub Pages 部署後，再由使用者決定是否移除 Streamlit 檔案。

---

## 一、 目標與整體資料流

```text
myscore.games
   │ (1. 禮貌爬蟲 >=1.5s，既有)
   ▼
data/raw_html/*.html
   │ (2. 本地快取解析，既有)
   ▼
data/pirates.db  ◄── src/verify.py（3. 資料邏輯驗算，既有）
   │ (4. 本計畫新增：src/export_json.py)
   ▼
web/public/data/pirates.json  (約 200~500 KB，純靜態資料)
   │ (5. update_all.bat git push → GitHub Actions 自動 Vite Build)
   ▼
GitHub Pages（MWG 現代響應式戰情看板，零冷啟動、手機秒開）
```

---

## 二、 `web/` 完整資料夾結構規劃

在專案根目錄新增 `web/` 目錄，採 **Vite + Vanilla TypeScript**（無 React/Vue），清楚切分資料層、純函式運算層、UI 元件層與頁面視圖層：

```text
pirates-baseball-db/
├── .github/
│   └── workflows/
│       └── deploy-web.yml          # 自動建置 web/ 並部署至 GitHub Pages
├── src/
│   ├── ... (保留所有既有 Python 模組)
│   └── export_json.py              # [新增] 從 data/pirates.db 匯出靜態 JSON 與校驗基準
├── web/
│   ├── index.html                  # 語意化主頁殼層（Landmark、Skip Link、SEO/OG Meta）
│   ├── package.json                # 僅含 vite, typescript, vitest, chart.js 等輕量依賴
│   ├── tsconfig.json               # strict: true, noUncheckedIndexedAccess: true
│   ├── vite.config.ts              # base: './', build 輸出至 dist/
│   ├── public/
│   │   ├── favicon.svg             # 海盜主題 SVG 圖示
│   │   └── data/
│   │       └── pirates.json        # [自動產出] 由 src/export_json.py 生成並納入 Git 版控
│   └── src/
│       ├── main.ts                 # 進入點：載入 JSON、初始化路由/篩選/四大頁籤
│       ├── types/
│       │   └── data.ts             # 對應 pirates.json 的嚴格 TypeScript Schema 型別
│       ├── core/
│       │   ├── store.ts            # 全域篩選狀態與 URL Query 雙向同步 (?tab=&uid=&era=)
│       │   ├── stats.ts            # 棒球純函式計算引擎（AVG/OBP/SLG/OPS/ERA/WHIP/K7/IP）
│       │   ├── filters.ts          # 依篩選條件過濾 team_ids、聚合打者/投手與戰績
│       │   └── csv.ts              # UTF-8 BOM (utf-8-sig) 瀏覽器端 CSV 匯出工具
│       ├── components/
│       │   ├── filter-bar.ts       # 桌機精簡篩選列 + 手機原生 <dialog> 篩選抽屜
│       │   ├── tabs.ts             # 依 MWG `tabs` 指南實作之無障礙 ARIA 頁籤元件
│       │   ├── data-table.ts       # 依 MWG `sortable-table` + `responsive-data-table` 實作
│       │   ├── player-combobox.ts  # IME 安全、鍵盤可操作之球員搜尋選單（綁定 player_uid）
│       │   └── charts.ts           # Chart.js 精簡樹搖封裝（支援亮/暗色模式自動同步）
│       ├── views/
│       │   ├── overview.ts         # Tab 1: 分隊戰情總覽（5 KPI 卡片 + 各隊戰績明細表）
│       │   ├── leaderboards.ts     # Tab 2: 投打排行榜（跨季/單季、Min PA/IP 滑桿、CSV 匯出）
│       │   ├── player-profile.ts   # Tab 3: 球員跨屆成長檔案（履歷、明細表、2 張趨勢圖）
│       │   └── opponents.ts        # Tab 4: 賽程戰果與對手分析（對戰統計 + 外部三連結）
│       ├── styles/
│       │   ├── tokens.css          # Design Tokens、light-dark() 色彩系統、字型與間距
│       │   ├── reset.css           # 現代輕量 CSS Reset
│       │   ├── layout.css          # Responsive Grid、Header、Filter Bar、Container Queries
│       │   └── components.css      # KPI Cards、Tabs、Sortable Table、Dialog、Combobox 樣式
│       └── tests/
│           └── stats.test.ts       # Vitest 單元測試（含真實邊界案例與 SQLite Views 比對）
├── AGENTS.md                       # [更新] 納入前端與 MWG 規範
├── run_pipeline.py                 # [更新] 階段 4 串接 export_json.py
├── run_web_dev.bat                 # [新增] Windows 本機一鍵啟動 Vite 預覽
└── .gitignore                      # [更新] 忽略 web/node_modules/ 與 web/dist/，保留 pirates.json
```

---

## 三、 `src/export_json.py` 與 `pirates.json` Schema 設計

### 3.1 匯出設計原則
1. **單一事實來源（Single Source of Truth）**：直接以唯讀方式從 `data/pirates.db` 匯出正規化資料表，由前端進行毫秒級篩選與跨季動態重算，確保所有篩選器組合皆精準。
2. **預先解算海盜內戰視角**：在匯出 `matches` 時，依據該筆紀錄所屬的 `team_id` 對應之 `teams.team_name`，預先計算 `pirate_side`（`'away' | 'home' | null`）與 `opponent_team`，徹底修正原 `app.py` 遇白隊 vs 黑隊內戰可能誤判勝負之問題：
   - 優先精準比對：`away_team == team_name` → `'away'`；`home_team == team_name` → `'home'`。
   - 次之去除空白比對；若仍無法比對，僅當單側含 `'海盜'` 時才以關鍵字判定。
3. **保留測試基準快照（`benchmarks`）**：同時匯出由 SQLite `v_player_career_batting` / `v_player_career_pitching` 計算出的關鍵測試球員基準值（例如 `蔡〇宇_2015-11`、`蔡〇宇_2017-06`、`林〇晏` 相關隊伍），供前端 Vitest 自動化比對，確保 JS 算式與 SQLite Views **小數點完全一致**。
4. **輸出壓縮與穩定排序**：JSON 依主鍵固定排序輸出（`ensure_ascii=False, separators=(",", ":")`），減少 Git diff 雜訊與檔案體積。

### 3.2 `web/public/data/pirates.json` JSON Schema（對應 `web/src/types/data.ts`）

```typescript
export interface PiratesDataset {
  schema_version: "1.0.0";
  meta: {
    generated_at: string;            // ISO8601 UTC 匯出時間
    last_fetched_at: string | null;  // SELECT MAX(last_fetched_at) FROM teams
    counts: {
      leagues: number;
      teams: number;
      players: number;
      matches: number;
    };
    filter_options: {
      age_groups: AgeGroup[];             // 依 ['U8','U10','U12','U15'] 排序
      team_groups: TeamGroup[];           // 依 ['海盜本隊','白隊','黑隊','U15海盜'] 排序
      years: number[];                    // 遞增排序 [2024, 2025, 2026, ...]
      seasons: Season[];                  // ['春季','秋季','其他']
      tournament_types: TournamentType[]; // ['例行賽','季後賽','台灣大賽']
    };
  };
  leagues: LeagueRecord[];
  teams: TeamRecord[];
  players: PlayerRecord[];
  rosters: RosterRecord[];
  batting_stats: BattingStatRecord[];
  pitching_stats: PitchingStatRecord[];
  matches: MatchRecord[];
  /** 供前端 Vitest 驗證前端算式與 SQLite Views 一致性的抽樣基準 */
  benchmarks: {
    career_batting: Array<{
      player_uid: string;
      pa: number;
      ab: number;
      h: number;
      calc_avg: number;
      calc_obp: number;
      calc_slg: number;
    }>;
    career_pitching: Array<{
      player_uid: string;
      total_outs: number;
      er: number;
      ip_baseball: string;
      calc_era_7: number | null;
      calc_era_9: number | null;
      calc_whip: number | null;
      calc_k7: number | null;
    }>;
  };
}

export type AgeGroup = "U8" | "U10" | "U12" | "U15";
export type TeamGroup = "海盜本隊" | "白隊" | "黑隊" | "U15海盜";
export type Season = "春季" | "秋季" | "其他";
export type TournamentType = "例行賽" | "季後賽" | "台灣大賽";
export type Division = "大聯盟" | "小聯盟" | "一般";

export interface LeagueRecord {
  league_id: number;
  league_title: string;
  year: number;
  season: Season;
  tournament_type: TournamentType;
  age_group: AgeGroup;
  division: Division;
}

export interface TeamRecord {
  team_id: number;
  league_id: number;
  csv_row_no: number | null;
  team_name: string;
  team_group: TeamGroup;
  leader: string | null;
  head_coach: string | null;
  manager: string | null;
  coaches: string | null;
  source_url: string;
  last_fetched_at: string | null;
}

export interface PlayerRecord {
  player_uid: string;
  site_player_id: number | null;
  masked_name: string;
  birth_ym: string | null;
}

export interface RosterRecord {
  team_id: number;
  player_uid: string;
  jersey_number: string | null;
  position: string | null;
}

export interface BattingStatRecord {
  team_id: number;
  player_uid: string;
  jersey_number: string | null;
  g: number;
  pa: number;
  ab: number;
  h: number;
  doubles: number;
  triples: number;
  hr: number;
  bb: number;
  so: number;
  rbi: number;
  r: number;
  sb_top10: number | null;
}

export interface PitchingStatRecord {
  team_id: number;
  player_uid: string;
  jersey_number: string | null;
  w: number;
  l: number;
  sv: number;
  g: number;
  outs: number;           // 整數出局數（鐵則）
  ip_raw: string | null;
  ab: number;
  h: number;
  doubles: number;
  triples: number;
  hr: number;
  bb: number;
  so: number;
  r: number;
  er: number;
}

export interface MatchRecord {
  id: number;
  team_id: number;
  league_id: number;
  match_date: string | null;
  match_date_iso: string | null;
  match_time: string | null;
  away_team: string;
  home_team: string;
  away_score: number | null;
  home_score: number | null;
  pirate_side: "away" | "home" | null;   // 由 export_json.py 精準判定，解決內戰問題
  opponent_team: string | null;          // 另一側球隊名稱
  venue: string | null;
  boxscore_url: string | null;
  pbp_url: string | null;
  summary_url: string | null;
}
```

---

## 四、 前端實作規格與 MWG 1.0 指引對照

### 4.1 棒球公式規格（`web/src/core/stats.ts`）
必須與 `src/db.py` 的 Views 100% 數學一致：
- **打擊率 `AVG`**：`ab > 0 ? round(h / ab, 3) : 0`
- **上壘率 `OBP`**：`(ab + bb) > 0 ? round((h + bb) / (ab + bb), 3) : 0`
- **壘打數 `TB`**：`h + doubles + 2 * triples + 3 * hr`
- **長打率 `SLG`**：`ab > 0 ? round(tb / ab, 3) : 0`
- **整體攻擊 `OPS`**：`round(obp_raw + slg_raw, 3)`（注意：以未四捨五入之原始 OBP + SLG 相加後再取小數 3 位，對齊原 `app.py` SQL）
- **投球局數顯示 `formatInnings(outs)`**：`${Math.floor(outs / 3)}.${outs % 3}`
- **防禦率 `ERA`**：`outs > 0 ? round((er * (eraInnings === 7 ? 21 : 27)) / outs, 2) : null`
- **每局被上壘率 `WHIP`**：`outs > 0 ? round(((h + bb) * 3) / outs, 2) : null`
- **7 局三振率 `K/7`**：`outs > 0 ? round((so * 21) / outs, 2) : null`

### 4.2 MWG 1.0 元件與版面規格
開發以下元件前，agy Agent 必須先查閱對應 MWG 指南：
1. **全域佈局與深淺色主題**（MWG: `dark-mode`, `form-factor-adaptation`）
   - 採用海盜隊黑／金／白視覺基調，使用 CSS `color-scheme: light dark` 與 `light-dark()` 自動跟隨系統主題，並提供右上角手動切換按鈕（偏好存入 `localStorage`）。
   - 桌機寬螢幕顯示精簡篩選工具列；手機窄螢幕收合為「篩選條件（含目前篩選計數徽章）」按鈕，點擊開啟原生 `<dialog>` 抽屜。
2. **四大核心頁籤與網址分享**（MWG: `tabs`）
   - 完整實作 `role="tablist"` / `role="tab"` / `role="tabpanel"` 與鍵盤方向鍵切換。
   - URL Query 同步：切換分頁、ERA 局數制、篩選條件或選定球員時同步更新 URL（例如 `?tab=player&uid=蔡〇宇_2015-11&era=7`），支援直接複製網址分享給家長與教練。
3. **可排序響應式數據表**（MWG: `sortable-table`, `responsive-data-table`, `dynamic-sort`）
   - 點擊表頭可切換升冪／降冪（含 `aria-sort="ascending" | "descending" | "none"`）。
   - 寬表格於窄螢幕支援水平捲動陰影提示與第一欄（姓名／球隊）`position: sticky; inset-inline-start: 0` 固定。
   - **IP 欄位排序防呆**：點擊 `IP` 排序時，底層必須以整數 `outs` 排序而非字串排序！
4. **球員搜尋選單**（`player-combobox.ts`）
   - 支援以「遮罩姓名」、「出生年月」或「曾穿背號」即時模糊過濾。
   - 正確處理 `compositionstart` / `compositionend`（防止注音輸入法選字誤觸）。
   - 選項明確顯示 `{masked_name}（{birth_ym}）｜{age_groups}`，value 嚴格綁定 `player_uid`。
   - 預設選中 `蔡〇宇_2015-11`（若存在），方便即時驗收同名區分能力。
5. **互動趨勢圖**（`charts.ts`）
   - 安裝 `chart.js`，僅按需註冊 `LineController`, `LineElement`, `PointElement`, `LinearScale`, `CategoryScale`, `Tooltip`, `Legend`, `Title`。
   - 球員成長頁提供兩張響應式折線圖：
     1. 歷屆打擊成長趨勢（AVG / OBP / SLG / OPS）
     2. 歷屆投球表現趨勢（ERA [依 7/9 局切換] / WHIP / K/7）
   - 切換深淺色主題或 ERA 局數制時，圖表即時重繪不閃爍。
6. **賽程對戰與外部連結**（`opponents.ts`）
   - 下拉選單列出符合篩選條件之所有對手球隊（排除空字串；若發生海盜內戰則正常列出對戰分隊）。
   - 外部連結（`攻守數據`、`文字紀錄`、`戰況表`）加上 `target="_blank" rel="noopener noreferrer"` 與清楚的圖示標籤。

---

## 五、 agy Agent 分階段執行任務清單（Execution Milestones）

請嚴格按照以下順序執行，每個 Milestone 完成後執行對應檢查指令並回報結果。

### Milestone 0：環境初始化與規範對齊
- [ ] 確認根目錄 `AGENTS.md` 已更新為新版規範。
- [ ] 安裝 MWG 1.0 技能包至專案工作區：
  ```bash
  npx modern-web-guidance@latest install
  ```
- [ ] 更新 `.gitignore`：加入 `web/node_modules/`、`web/dist/`、`web/.vite/`，並明確加上 `!web/public/data/pirates.json`。
- [ ] 順手強化 `src/verify.py`：`check_coverage` 改為動態讀取 `data/海盜隊網頁標題.csv` 筆數比對（取代寫死的 `41`）。
- [ ] **驗證指令**：
  ```bash
  .venv\Scripts\python.exe src/verify.py
  ```

### Milestone 1：建置靜態 JSON 匯出器與管線整合
- [ ] 實作 `src/export_json.py`：
  - 唯讀開啟 `data/pirates.db`。
  - 產出符合第三節 Schema 之 `web/public/data/pirates.json`（含 `pirate_side` 內戰精準判定與 `benchmarks` 抽樣基準）。
  - 支援 CLI 參數 `--db` 與 `--output`。
- [ ] 修改 `run_pipeline.py`：在 `verify.py` 通過後自動執行階段 `[4/4] 匯出前端靜態 JSON (export_json.py)`。
- [ ] **驗證指令**：
  ```bash
  .venv\Scripts\python.exe run_pipeline.py --parse-only
  ```
  確認 `web/public/data/pirates.json` 成功產生，檢查檔案大小、UTF-8 編碼、以及 `team_id=3439` 兩位 `蔡〇宇` 與 `林〇晏 (outs=0, er=1)` 皆正確存在。

### Milestone 2：初始化 `web/` Vite + Vanilla TypeScript 專案與核心引擎
- [ ] 建立 `web/package.json`、`web/tsconfig.json`、`web/vite.config.ts`（`base: './'`）。
- [ ] 安裝依賴：`vite`、`typescript`、`vitest`、`chart.js`。
- [ ] 建立 `web/src/types/data.ts`、`web/src/core/stats.ts`、`web/src/core/filters.ts`、`web/src/core/csv.ts`、`web/src/core/store.ts`。
- [ ] 撰寫 `web/src/tests/stats.test.ts`，驗證：
  1. 讀取真實 `web/public/data/pirates.json` 的 `benchmarks`，逐筆比對 JS 算出的 AVG/OBP/SLG/ERA(7/9)/WHIP/K7/IP 與 SQLite Views 誤差 `=== 0`。
  2. 測試 `蔡〇宇_2015-11` 與 `蔡〇宇_2017-06` 各自獨立計算不合併。
  3. 測試 `outs=0, er=1` 合併至其他局數時責失分正確計入，單獨 0 局時 ERA 回傳 `null`。
  4. 測試海盜白隊 vs 黑隊內戰之勝負判定。
  5. 測試未開賽隊伍（`team_id=4046`）零資料不拋例外。
- [ ] **驗證指令**：
  ```bash
  cd web && npm run typecheck && npm test
  ```

### Milestone 3：依 MWG 1.0 建置共用 UI 元件與樣式系統
- [ ] 查閱 MWG 指南並建立 `web/src/styles/`（`tokens.css`, `reset.css`, `layout.css`, `components.css`），完整支援亮／暗色模式與手機響應式佈局。
- [ ] 實作 `web/src/components/`：`filter-bar.ts`、`tabs.ts`、`data-table.ts`、`player-combobox.ts`、`charts.ts`。
- [ ] 實作頂部 Header：顯示專案標題、最後更新時間（`meta.last_fetched_at` 轉台灣時間 `Asia/Taipei`）、暗黑模式切換鍵，以及行動版篩選抽屜開關。
- [ ] **驗證指令**：
  ```bash
  cd web && npm run typecheck && npm test
  ```

### Milestone 4：實作四大戰情視圖並串接 `main.ts`
- [ ] `web/src/views/overview.ts`：5 張 KPI 卡片 + 各聯賽參賽球隊戰績明細表（可排序）。
- [ ] `web/src/views/leaderboards.ts`：跨季／單季切換、打者／投手子頁籤、Min PA / Min IP 滑桿、可排序表（IP 依 `outs` 排序）、下載 `utf-8-sig` CSV 按鈕。
- [ ] `web/src/views/player-profile.ts`：球員搜尋選單（URL `?tab=player&uid=...` 連動）、個人履歷摘要卡、歷屆打擊／投球明細表、2 張 Chart.js 趨勢折線圖，並提供「複製分享連結」按鈕。
- [ ] `web/src/views/opponents.ts`：對手下拉選單、4 張對戰 KPI、對戰明細表含安全外部連結。
- [ ] `web/src/main.ts`：以 `fetch('./data/pirates.json', { cache: 'no-cache' })` 載入資料，處理載入中骨架屏與錯誤提示。
- [ ] **驗證指令**：
  ```bash
  cd web && npm run typecheck && npm test && npm run build
  ```

### Milestone 5：全自動品質掃描、部署設定與本機腳本
- [ ] 建立 `.github/workflows/deploy-web.yml`：當 `main` 分支變更（包含 `update_all.bat` 推送新 `pirates.json`）時，自動執行 `npm ci` → `npm run typecheck` → `npm test` → `npm run build` 並部署 `web/dist` 至 GitHub Pages。
- [ ] 建立 `run_web_dev.bat`：供 Windows 本機雙擊快速啟動 `cd web && npm run dev`。
- [ ] 更新 `README.md` 與 `update_all.bat` 結尾提示網址（保留原有 Git 推送邏輯不變）。
- [ ] **全量驗收指令（必須全數通過）**：
  ```bash
  .venv\Scripts\python.exe run_pipeline.py --parse-only
  cd web && npm run typecheck && npm test && npm run build
  npx modern-web-guidance@latest legacy-scan ./web/src
  ```

---

## 六、 最終驗收清單（Acceptance Criteria）

1. **數據 100% 一致**：Vitest 自動比對前端算式與 SQLite Views 全數通過；隨機抽查 3 位球員（含 `蔡〇宇_2015-11`、`蔡〇宇_2017-06`、`林〇晏`）與原 Streamlit 畫面數值吻合。
2. **零錯誤與零過時語法**：`tsc --noEmit`、`vitest`、`vite build` 與 `modern-web-guidance legacy-scan` 全數無 Error。
3. **手機與無障礙體驗**：於 375px 手機寬度與 1440px 桌機寬度檢查無橫向爆版；所有篩選、頁籤、排序、搜尋選單皆可用鍵盤操作且切換深淺色主題正常。
4. **網址分享與零冷啟動**：直接開啟 `?tab=player&uid=蔡〇宇_2015-11&era=7` 能直接定位至該球員成長檔案與 7 局制數據；`vite preview` 檢視靜態成品所有資源與 `data/pirates.json` 無 404。
5. **既有工作流相容**：執行 `update_all.bat` 可一次完成「爬蟲更新 → SQLite 入庫 → `verify.py` 驗算 → 匯出 `web/public/data/pirates.json` → `git push` 觸發雲端自動部署」。