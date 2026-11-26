# 台中小海盜棒球數據庫 — 第五頁籤「🎬 球員影片集合」實行計畫 (IMPLEMENTATION_PLAN_PLAYER_CLIPS.md)

> **版本狀態**：架構設計與 PoC 驗證完成（以 `高〇展_2015-10` 全 13 場出賽為評估基準）  
> **目標頁籤**：新增第五個頁籤 **「🎬 球員影片集合」**（Tab ID: `clips`），維持純靜態架構（`Vite + Vanilla TypeScript + MWG 1.0`）與零外部寫入原則。

---

## 一、 目標與前期實測結論

### 1.1 使用者需求
在現有四大戰情頁籤之後新增第五個頁籤 **「🎬 球員影片集合」**：
- 支援依 `player_uid` 搜尋單一球員（相容同隊同名區分與中文注音 IME 組字安全）。
- 將該球員每一場實際出賽的以下三類影片片段整理成表格，供教練與家長一鍵跳轉至對應秒數（`https://www.youtube.com/watch?v={video_id}&t={sec}s`）：
  1. **每打席 YouTube 連結（含開始秒數與打擊結果）**
  2. **上場投球 YouTube 連結（含開始秒數、先發/中繼與面對打席數）**
  3. **參與守備 YouTube 連結（含開始秒數、守位與處理/刺殺/失誤事件）**

### 1.2 前期技術實測結論（關鍵事實）
經過對專案資料庫、YouTube 影片與 `myscore.games` 單場頁面的實際測試，我們確認以下四項核心事實：

1. **YouTube 無可用現成中文字幕**：
   - 實測 4 支「就是棒」比賽影片（`zOYGcQUuul4`, `pHzNMoBhQ2k`, `MXqvlJCIw-g`, `bHz5VH3CoYY`），其中 3 支完全無字幕軌道、1 支僅有誤判為 `en` 且內容為空的自動字幕。因此**無法直接抓取 YouTube 內建逐字稿**。
2. **每場影片皆具備兩大高價值定位訊號**：
   - **訊號 A（畫面 Scorebug 記分板）**：絕大多數比賽畫面左下角（少數在右下角，如 `DEFa5AdYACA`、`ihw5vktVM8k`）皆固定疊加即時記分板，主要分為兩種標準樣式：
     - **樣式 A（黑底白比數框，2025 秋季中後期～2026）**：顯示隊名、白底黑字比數、三個壘包菱形、`B-S` 球數、`X Out(s)` 與 `TOP N / BOT N`。
     - **樣式 B（MEVO 深綠底，2025 秋季初期）**：顯示隊名、比數、亮黃綠壘包菱形、2 顆亮綠出局圓點、`▲N / ▼N` 與 `B-S`。
   - **訊號 B（專業中文主播語音）**：實測抽取多場 20 秒音軌試聽，直播皆有清楚的中文主播實況播報（明確唱名背號、選手姓名、球數、安打與守備位置處理）。
3. **官網 `myscore.games` 具備完整單場事件骨幹**：
   - 資料庫 `matches` 表已儲存每場的 `boxscore_url`（`show_record.php`）、`pbp_url`（`show_text.php`）與 `summary_url`（`score_book.php`）。
   - `show_record.php` 提供單場個人打擊與投球成績（含背號與遮罩姓名），可 100% 鎖定球員實際出賽場次。
   - `show_text.php` 提供先發九人打序與守備位置、每半局 `先發：投手` / `換投：投手`，以及逐打席與標註守備位置（如 `三壘手接到後傳向一壘刺殺打者出局`、`遭到左外野手接殺出局`）的完整文字紀錄。
4. **架構結論：雙軌定位引擎（官網事件骨幹 ＋ 軌道 A 視覺狀態機 ＋ 軌道 B 語音 ASR 精修）**：
   - 僅靠語音逐字稿容易因主播漏報姓名、環境風聲或遮罩姓名同音誤判而遺漏片段；僅靠 Scorebug 則在「單一出局區間內連續多位打者上壘」或「轉播員延遲開啟/忘記切換記分板」時會降級為區間插值。
   - 採用 **「官網文字紀錄作為事件真值清單 ＋ 軌道 A（Scorebug 狀態機）鎖定半局與出局錨點 ＋ 軌道 B（ASR 語音含時間戳記逐字稿）精修微調與唱名驗證」**，是兼顧準確率、可解釋性與離線重跑效率的最佳架構。

---

## 二、 整體資料流與目錄規劃

```text
myscore.games (show_record.php / show_text.php)
   │ (1. 循序抓取 >=1.6s，快取至 data/raw_html/matches/)
   ▼
src/parse_match_events.py  ──► data/pirates.db (match_lineups, match_events)
                                     ▲
YouTube 比賽影片 (360p/480p 暫存)     │
   ├── 軌道 A：ffmpeg nokey + WinRT OCR / OpenCV
   │      ──► data/video_timelines/{video_id}.json (Scorebug 清洗時間軸)
   └── 軌道 B：yt-dlp 音軌 + ASR 含時間戳記逐字稿
          ──► data/transcripts/{video_id}.json (保留於本地/私有快取，不公開原名)
                                     │
                                     ▼
src/align_player_clips.py (+ data/player_clip_overrides.csv 人工校正)
   │  雙軌融合對齊 + 寫入 player_video_clips
   ▼
src/export_clips_json.py
   │  唯讀匯出輕量化靜態 JSON (僅含遮罩姓名與公開 URL/秒數)
   ▼
web/public/data/player-clips.json  ◄── 第五頁籤 Lazy Load（按需讀取）
```

### 目錄與檔案配置
- `data/raw_html/matches/`：單場 `{league_id}_{site_match_id}_record.html` 與 `_text.html`（沿用爬蟲快取原則，離線可重跑）。
- `data/video_timelines/`：每支影片的 Scorebug 原始抽樣與清洗後時間軸 JSON（PoC 階段對應目錄為 `data/poc_scorebug/`）。
- `data/transcripts/`：選配之語音含時間戳記逐字稿快取 JSON（**納入 `.gitignore` 避免公開未成年選手完整真實姓名**）。
- `data/player_clip_overrides.csv`：人工秒數微調覆寫表（`utf-8-sig`，格式比照 `youtube_manual_overrides.csv`）。
- `web/public/data/player-clips.json`：獨立於 `pirates.json` 的靜態片段資料檔（納入 Git 版控，前端僅於使用者切換至第五頁籤時載入）。

---

## 三、 SQLite 資料庫 Schema 擴充設計

遵循 `AGENTS.md` 規範：所有 SQLite 連線必須啟用 `PRAGMA foreign_keys = ON;`，且所有球員歸戶一律以 `player_uid` 為外鍵。於 `src/db.py` 擴充以下五張資料表：

```sql
-- 1. 單場先發打序與守備位置
CREATE TABLE IF NOT EXISTS match_lineups (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id        INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
    team_id         INTEGER NOT NULL REFERENCES teams(team_id),
    player_uid      TEXT REFERENCES players(player_uid), -- 對手球隊或無對應者為 NULL
    masked_name     TEXT NOT NULL,
    batting_order   TEXT NOT NULL,                       -- '1'~'9' 或 'P'
    position        TEXT NOT NULL,                       -- '投','捕','一','二','三','游','左','中','右'
    UNIQUE(match_id, team_id, batting_order, masked_name)
);

-- 2. 單場逐半局文字事件明細（對應 show_text.php）
CREATE TABLE IF NOT EXISTS match_events (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id            INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
    inning              INTEGER NOT NULL,
    half_side           TEXT NOT NULL CHECK (half_side IN ('TOP', 'BOT')),
    event_index         INTEGER NOT NULL,
    event_type          TEXT NOT NULL,                   -- 'pa' | 'pitch_change' | 'runner' | 'sub'
    batter_uid          TEXT REFERENCES players(player_uid),
    batter_name         TEXT,
    pitcher_uid         TEXT REFERENCES players(player_uid),
    pitcher_name        TEXT,
    outs_before         INTEGER NOT NULL DEFAULT 0,
    outs_delta          INTEGER NOT NULL DEFAULT 0,
    runs_scored         INTEGER NOT NULL DEFAULT 0,
    involved_positions  TEXT,                            -- JSON 陣列，如 '["三","一"]'
    event_text          TEXT NOT NULL,
    UNIQUE(match_id, inning, half_side, event_index)
);

-- 3. 影片 Scorebug 半局時間軸快取摘要（軌道 A）
CREATE TABLE IF NOT EXISTS video_scorebug_segments (
    video_id        TEXT NOT NULL REFERENCES youtube_videos(video_id) ON DELETE CASCADE,
    inning          INTEGER NOT NULL,
    half_side       TEXT NOT NULL CHECK (half_side IN ('TOP', 'BOT')),
    start_sec       INTEGER NOT NULL,
    end_sec         INTEGER NOT NULL,
    sample_count    INTEGER NOT NULL DEFAULT 0,
    is_interpolated INTEGER NOT NULL DEFAULT 0,          -- 0=畫面實測鎖定, 1=夾擠補間
    PRIMARY KEY (video_id, inning, half_side)
);

-- 4. 影片語音逐字稿關鍵分段摘要（軌道 B，選配）
CREATE TABLE IF NOT EXISTS video_transcript_segments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id        TEXT NOT NULL REFERENCES youtube_videos(video_id) ON DELETE CASCADE,
    start_sec       REAL NOT NULL,
    end_sec         REAL NOT NULL,
    text_masked     TEXT NOT NULL,                       -- 入庫前自動遮罩選手全名中間字
    matched_uid     TEXT REFERENCES players(player_uid)
);

-- 5. 球員影片片段對齊主表（前端第 5 頁籤直接資料來源）
CREATE TABLE IF NOT EXISTS player_video_clips (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id        INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
    player_uid      TEXT NOT NULL REFERENCES players(player_uid),
    video_id        TEXT NOT NULL REFERENCES youtube_videos(video_id) ON DELETE CASCADE,
    clip_type       TEXT NOT NULL CHECK (clip_type IN ('batting', 'pitching', 'fielding')),
    inning          INTEGER NOT NULL,
    half_side       TEXT NOT NULL CHECK (half_side IN ('TOP', 'BOT')),
    event_index     INTEGER NOT NULL,
    label_short     TEXT NOT NULL,                       -- 如 '三振', '二壘安打', '先發・對4打席', '[游] 守備處理'
    event_detail    TEXT NOT NULL,                       -- 官網完整文字說明或投球局摘要
    start_sec       INTEGER NOT NULL,                    -- 含 pre-roll 之開始秒數
    align_method    TEXT NOT NULL,                       -- 'dual_track' | 'scorebug_anchor' | 'scorebug_bs_reset' | 'scorebug_anchor_interp' | 'bracket_interp' | 'manual'
    confidence      TEXT NOT NULL CHECK (confidence IN ('high', 'medium', 'low')),
    UNIQUE(match_id, player_uid, clip_type, inning, half_side, event_index)
);

CREATE INDEX IF NOT EXISTS idx_player_video_clips_uid ON player_video_clips(player_uid);
```

---

## 四、 雙軌時間戳記定位引擎技術規格

### 4.1 軌道 A：Scorebug 視覺狀態機（主骨幹）
1. **極速關鍵幀抽取**：
   - 使用 `yt-dlp -N 8` 下載 `360p/480p` 暫存 `.mp4`，接著以 `ffmpeg -skip_frame nokey -vf "select='isnan(prev_selected_t)+gte(t-prev_selected_t,4)',crop=iw:ih*0.32:0:ih*0.68,showinfo"` 僅解碼關鍵幀並裁切畫面下方 32%（實測單場 5000 秒影片僅需 **6.7～69.3 秒**即可完成抽幀與 OCR，且 `select>=4s` 可防止 MEVO 全關鍵幀錄影產生過多影像）。
2. **雙樣式＋雙側自動定位**：
   - 探測畫面左下與右下角區域：
     - **樣式 A**：以兩個垂直對齊的純白比數方塊為 Anchor，切出底部黑條交由 `WinRT OCR (en-US)` 解析 `TOP/BOT N`、`X Out(s)` 與 `B-S`，並以白色菱形輪廓質心判定一二三壘有人狀態。
     - **樣式 B（MEVO 深綠底）**：以深綠卡片輪廓為 Anchor，透過亮黃綠幾何元件（面積比判定正三角形 `▲ TOP` / 倒三角形 `▼ BOT`、右上雙圓點判定 `0/1/2 Outs`、三顆菱形位置判定壘包）搭配右下角數字子區 OCR 讀取局數與 `B-S`。
3. **時序清洗與缺漏半局夾擠補間**：
   - 3 幀滑動多數決去噪後，依 `show_text.php` 實際存在之半局順序單調遞增鎖定 `[start_sec, end_sec]`。
   - 若遇到「開賽延遲開啟記分板」或「尾局操作員漏切換半局」，自動以相鄰已鎖定半局時間為邊界，依各半局實際 PA 數比例執行**夾擠補間（`bracket_interp`）**，並誠實標註信心度為 `low`。
4. **多段影片（Part 1 / Part 2）支援**：
   - 若 `match_videos` 該場有 2 段以上影片，依 `sort_order` 逐段掃描；以兩段各自偵測到的半局範圍自動指派每一個半局所屬 `video_id`（例如 `1上~3下` 屬 Part 1、`4上~6下` 屬 Part 2）。

### 4.2 軌道 B：語音含時間戳記逐字稿精修（精修與校驗層）
1. **為什麼需要軌道 B**：
   - 軌道 A 對「單打席區間」與「投球換局」已有高準確度，但在**單一出局區間連續多人上壘（如保送＋安打＋失誤）**或**記分板漏操作（`bracket_interp`）**時，語音逐字稿能直接抓到主播唱名（「現在輪到背號 79 號高〇〇打擊」、「投手換上 79 號…」），將 `low / medium` 片段直接校驗升為 `high`。
2. **ASR 方案比較與全站 241 小時成本試算**（本機環境為 `Intel Core i5-12500H` 12 核無獨立 NVIDIA GPU）：

| 方案 | 引擎 / 模型 | 全站 241 小時推論耗時估算 | 預估費用 (USD) | 繁中棒球專有名詞準確度 | 建議定位 |
| :--- | :--- | :---: | :---: | :---: | :--- |
| **本機免費** | `faster-whisper` (`small`/`medium`, int8 CPU) | 約 60～120 小時（僅需離線跑一次，支援背景分批） | **\$0** | 中高（同音字需用姓名拼音/背號模糊比對） | 零預算或純地端首選 |
| **雲端極速** | Groq Whisper API (`whisper-large-v3-turbo`) | 約 1～2 小時（並發送音檔片段） | 約 **\$9.6 USD**（\$0.04/hr） | 高 | **性價比最高（推薦）** |
| **雲端標準** | OpenAI Whisper API (`whisper-1`) | 約 2～3 小時 | 約 **\$86.8 USD**（\$0.36/hr） | 高 | 成本較高 |
| **多模態語音** | Gemini Flash Audio (`gemini-2.5-flash`) | 約 1～2 小時（可傳入 Prompt 提供當場攻守名單） | 約 **\$15～\$25 USD** | **極高**（Prompt 給定當場先發名單與背號，幾乎不錯字） | **品質最佳選項** |

3. **背號＋遮罩姓名同音模糊比對邏輯**：
   - 已知當場打序中目標選手的 `{背號, 遮罩姓名}`（如 `79`、`高〇展`）與半局時間窗 `[half_start_sec, half_end_sec]`。
   - 使用 `pypinyin` 取得選手姓氏＋尾字讀音（如 `高*展` -> `gao * zhan`），加上常見主播句型（「輪到第 X 棒」、「背號 79 號」、「XX 號的打擊」），在對應半局的時間窗內搜尋語音片段，命中後取代純插值秒數並標記 `align_method = 'dual_track'`, `confidence = 'high'`。

### 4.3 Pre-roll 緩衝與人工覆寫機制
- **Pre-roll 預留秒數**：
  - **打席**：`max(0, start_sec - 5)`（提前 5 秒進場，正好看到打者站上打擊區）。
  - **投球登板**：`max(0, start_sec - 5)`（含半局開始或中繼換投登場）。
  - **守備處理**：`max(0, end_sec - 12)`（提前 12 秒，從投球出手→打者擊球→守備接傳完整呈現）。
- **人工覆寫（`data/player_clip_overrides.csv`）**：
  - 欄位：`match_id, player_uid, clip_type, inning, half_side, event_index, video_id, start_sec, note`
  - 管線執行時最後套用覆寫表，設為 `align_method = 'manual'`, `confidence = 'high'`，提供教練／家長即時微調任何誤差片段的管道。

---

## 五、 前端第五個頁籤「🎬 球員影片集合」設計規格（MWG 1.0）

嚴格遵循 `AGENTS.md` 規範（Vanilla TypeScript、Baseline Widely available、MWG 1.0 無障礙與響應式標準）：

### 5.1 檔案變更清單
1. `web/src/types/data.ts`：
   - `TabId` 擴充為 `"overview" | "leaderboards" | "player" | "opponents" | "clips"`。
   - 新增 `PlayerClipItem`、`PlayerClipMatchRow`、`PlayerClipsDataset` 型別定義。
2. `web/src/components/tabs.ts`：
   - `MAIN_TABS` 末尾新增 `{ id: "clips", label: "🎬 球員影片集合" }`。
3. `web/src/core/store.ts`：
   - `VALID_TABS` 加入 `"clips"`。
   - `serializeStateToSearch` 與 `parseStateFromSearch` 支援 `?tab=clips&uid={player_uid}` 雙向同步，支援直接複製網址分享單一球員的影片集。
4. `web/src/views/player-clips.ts`（新增）：
   - 頂部工具列：複用 `createPlayerCombobox`（支援姓名、出生年月、背號搜尋與 IME 組字安全防護）、事件類型切換膠囊（`全部` / `僅打席` / `僅投球` / `僅守備`）、以及「📥 匯出 CSV (utf-8-sig)」按鈕與「🔗 複製分享連結」按鈕。
   - 頂部統計摘要列：顯示該球員在目前全域篩選條件（`teamIds`）下的「出賽影片場數」、「打席片段數」、「投球半局數」、「守備片段數」。
   - 主表格（複用 `createDataTable`，遵循 MWG `sortable-table` 與 `responsive-table`）：
     - 欄位：`比賽日期（含 stickyRowHeader）`、`賽事 / 對戰與比數`、`先發守位`、`打席 YouTube 連結`、`投球 YouTube 連結`、`參與守備 YouTube 連結`。
     - 每個片段顯示為語意化連結膠囊 `<a href="..." target="_blank" rel="noopener noreferrer">`（例如 `1上 二壘安打 (18:47)`、`4上 先發・對3打席 (1:05:54)`、`3下 [游] 守備處理 (40:16)`），並依 `confidence` 加上低調視覺標記（`high` 實心、`medium` 一般、`low` 虛線框＋提示「區間推估」）。
     - 若需手機端更佳可讀性，透過 CSS `@media` 與表格水平滾動提示確保 375px 不破版。
5. `web/src/main.ts`：
   - **Lazy Load 設計**：初次開啟網站只 fetch `pirates.json`；僅當 `state.tab === "clips"` 時才 `fetch("./data/player-clips.json")`（並快取 Promise），確保首頁 LCP 零延遲。
6. `web/src/views/player-profile.ts`：
   - 在球員個人履歷頂部按鈕列新增「🎬 查看此球員影片集合」按鈕，一鍵切換至 `?tab=clips&uid={selectedPlayerUid}`。

### 5.2 未成年選手隱私保護設計
- 前端 `player-clips.json` **嚴格僅包含 `player_uid` 與 `masked_name`（如 `高〇展`）**，絕不輸出 ASR 語音逐字稿原文或球員真實全名。

---

## 六、 全站建置與日常維護成本／效能試算

### 6.1 實測效能基準（來自 `高〇展_2015-10` 13 場實測）
- 全站總規模：`matches` 181 筆（**172 場獨立比賽**）、**181 支獨立比賽影片**（總片長 **241.09 小時**）、**267 位**非教練球員。
- 單場處理實測耗時（本機 i5-12500H 網路實測）：
  - **官網下載與解析**：單場約 `3.2 秒`（2 頁 × 1.6s），全站 172 場（344 頁）約 **9～10 分鐘**（僅首次全量，之後增量極快）。
  - **影片 360p/480p 下載（`yt-dlp -N 8`）**：每場 `18.8s ～ 117.8s`（平均約 `~58s`）。
  - **Scorebug 掃描（`ffmpeg nokey` + `WinRT OCR`）**：Style A 約 `6.7s ～ 69.3s`（平均 `~32s`）；Style B 約 `40.2s ～ 63.4s`。
  - **單場總計（軌道 A）**：平均約 **90 秒／場**；全站 181 支影片首次離線全量建置約 **4.5 小時**（支援斷點續跑與單支快取，處理完單支立即刪除 `.mp4`，磁碟峰值佔用 `< 250 MB`）。

### 6.2 日常維護與 CI/CD 整合策略
- **GitHub Actions `update-data.yml` 每日自動化**：
  - GitHub-hosted runner 不適合長時下載大量影片；建議採「**分層增量更新**」：
    1. 每日排程（`09:41`）：在 GitHub Actions 僅增量抓取**新開打場次的 `show_record.php` / `show_text.php`**（每週僅新增約 2～6 場），若偵測到「有新賽程且尚未建立 Scorebug 時間軸」者，先以官網資料標記「待掃描」或於 Actions 針對新配對到的 **單日少數新影片（1～2 支，耗時約 2～3 分鐘）** 執行輕量軌道 A 掃描（Linux Runner 上可用 `Tesseract` 或 `RapidOCR` 取代 Windows 專屬 `WinRT OCR`）。
    2. 歷史 181 支影片由本機執行一次全量建置後，將輕量快取 `data/video_timelines/*.json` 與 `web/public/data/player-clips.json` 納入 Repo（預估全站 181 支時間軸摘要約 5~10 MB、`player-clips.json` 約 1~2 MB）。

---

## 七、 風險、邊界條件與開發驗證閘道

### 7.1 已知風險與對策
1. **守備位置中途無紀錄調動**：`show_text.php` 會明確記載「先發守位」與「換投」，但偶爾教練於局間交換內外野守位（非換投）時網站文字紀錄不會寫明，可能造成極少數守備判斷歸屬到原守位。對策：於前端註明「守位依官網先發與換投紀錄推導」並支援 `player_clip_overrides.csv` 校正。
2. **海盜內戰雙視角去重**：當白隊對戰黑隊時，同一場比賽有兩個 `team_id` 與兩筆 `matches`（`119` 號或 `154` 號）。對策：`match_lineups` 與 `player_video_clips` 依目標球員實際所屬 `team_id` 與 `match_id` 綁定，不會重複出現兩筆。
3. **無記分板或極端遮擋場次**：若歷史 2024 年部分早期影片完全無 Scorebug，軌道 A 會退回均勻分段；此時若啟用軌道 B（ASR 語音逐字稿），仍可透過主播唱名精準定位。
4. **Windows vs Linux OCR 引擎相容**：本機 PoC 使用 `winrt-Windows.Media.Ocr`（速度快且準確率高），正式版跨平台（GitHub Actions Ubuntu）可實作雙後端抽換介面（Windows 優先 WinRT OCR，Linux 自動退回 `rapidocr-onnxruntime` 或 `tesseract`）。

### 7.2 正式開發階段必過驗證閘道（`AGENTS.md`）
- **Python 後端**：
  - `.venv\Scripts\python.exe src/verify.py`（新增逐場 PA/Out 加總與 `team_batting_stats`/`team_pitching_stats` 0 誤差檢核）
  - `.venv\Scripts\python.exe src/export_json.py`
- **前端與 MWG 1.0 檢查**：
  - `cd web && npm run typecheck`
  - `cd web && npm test`
  - `cd web && npm run build`
  - `npx modern-web-guidance@latest legacy-scan ./web/src`

---

## 八、 `高〇展_2015-10` PoC 實測結果摘要

本次已實際針對 `高〇展_2015-10` 完成端對端 PoC 驗證（未修改既有 `pirates.db`、`pirates.json` 與 `web/src/`），相關評估產出檔案如下：
- **逐場官網解析結果**：`data/poc_eval/高〇展_2015-10_parsed_games.json`（22 場隊伍賽程中精準篩出 13 場出賽；PA=26、投球 G=7 / Outs=22 與 `pirates.db` 100% 一致）
- **評估資料 JSON**：`data/poc_eval/高〇展_2015-10_clips.json`
- **評估資料 CSV（`utf-8-sig`）**：`data/poc_eval/高〇展_2015-10_clips.csv`（共 **55 筆**：26 打席 + 11 投球半局 + 18 守備）
- **完整評估報告（含 13 場可點擊總表與 10 筆截圖驗證紀錄）**：`data/poc_eval/高〇展_2015-10_report.md`
- **PoC 腳本與快取**：
  - `scripts/poc_fetch_and_parse_matches.py`
  - `scripts/poc_scorebug_align.py`
  - `scripts/poc_export_eval.py`
  - `data/poc_scorebug/*.json`（13 支影片 Scorebug 清洗時間軸快取）
