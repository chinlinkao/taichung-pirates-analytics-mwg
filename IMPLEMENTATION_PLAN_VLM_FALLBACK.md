# 正式管線整合 Targeted VLM Fallback ＋ OpenCV ±2s 微調實行計畫

> **目標**：針對現行球員影片雙軌定位管線（`src/clips_pipeline.py`），在遇到「**無語音播報（無 ASR）且無 Scorebug 有效更新**」的偶發盲區時段時，加入條件式第三軌 **Targeted VLM Fallback**（以極少張 3×3 九宮格拼貼圖抓出開賽點、換局點與各打席區間），並視情況搭配 **OpenCV 局部 ±2 秒微調**，大幅降低盲區漂移誤差，同時維持正常時段的零額外 API 成本。

---

## 一、 管線架構與設計原則

在現有 `src/clips_pipeline.py` 的流程中插入**條件式軌道 C**：

```text
[1/6] 官網單場文字紀錄解析 (src/match_events.py)
  ↓
[2/6] 軌道 A：Scorebug 掃描與半局對齊 (src/clips_scorebug.py)  ← 含孤立單幀與偽高信心修正
  ↓
[3/6] 軌道 B：本機 faster-whisper ASR 語音精修 (src/clips_asr.py)
  ↓
[4/6] 軌道 C：Targeted VLM Fallback + OpenCV ±2s 微調 (新增 src/clips_vlm_fallback.py)
      ※ 僅針對「無 ASR 覆蓋 且 Scorebug 缺漏／不可靠」的盲區半局觸發，正常片段 100% 略過
  ↓
[5/6] 套用人工覆寫表 (data/player_clip_overrides.csv，永遠最高優先) 並寫入 player_video_clips
  ↓
[6/6] 匯出前端靜態 JSON (web/public/data/player-clips.json)
```

---

## 二、 分階段實作項目

### Phase 0：修正既有盲區偵測盲點（`src/clips_scorebug.py`）
1. **孤立單幀過濾（`clean_scorebug_timeline`）**：
   - 若某半局原始 `detected=True` 樣本數 `< 3` 或時間跨度 `< 25s`（於 `PA >= 3` 之半局），視為單點 OCR 誤觸（例如 `XegkC7ILcrs` 1 局上僅在 `t=20s` 抓到 1 幀卻被滑動視窗擴張為 `15–30s`），強制降級為 `is_interpolated=True`。
2. **區分實測出局錨點與比例補算（`align_game_all_players`）**：
   - 新增 `has_real_out1` / `has_real_out2` 旗標，唯有 Scorebug 實測讀到出局數時 `has_hard_anchor` 才為 `True`。
   - 比例補算出的半局一律標記為 `scorebug_half_interp (low)` 或 `bracket_interp (low)`，不再誤標為 `scorebug_anchor (high)`，讓真正的盲區能準確落入軌道 C 觸發清單。

### Phase 1：盲區自動識別與區段裁切（新增 `src/clips_vlm_fallback.py`）
1. **觸發條件（以半局為單位）**：同時符合以下兩項才納入盲區：
   - **Scorebug 不可靠**：`is_interpolated == True` 或 `sample_count < 6` 或該半局片段多數為 `low`；
   - **ASR 無覆蓋**：該半局經軌道 B 後，升級為 `dual_track` 的打席比例 `< 50%`（例如 `XegkC7ILcrs` 4 局上雖無 Scorebug，但 ASR 已全數命中 `dual_track`，因此**不觸發 VLM**）。
2. **相鄰盲區合併與區段下載**：
   - 將相鄰盲區半局合併為單一時間窗（如 `XegkC7ILcrs` 的 `1上 + 1下` 合併為 `[0s, 860s]`），僅針對該視窗透過 `yt-dlp --download-sections`（360p）或既有暫存檔抽幀，絕不對整場影片送模型。

### Phase 2：兩階段九宮格 VLM 定位 ＋ OpenCV 局部微調（`src/clips_vlm_fallback.py`）
1. **粗掃九宮格（Coarse Scan）**：
   - 每 `10s` 抽 1 幀、拼成 `3×3`（`1278×720`）Contact Sheet，每格左上角壓印高對比 `MM:SS (XXXs)` 時間標籤。
   - 連同盲區內的官網逐打席文字（打者、結果、跑壘／出局順序）與前後已知邊界錨點，以結構化 JSON Schema 呼叫 VLM（預設 `gemini-3.5-flash-lite`，支援免費層且速度最快；亦可透過 `--vlm-model` 切換 `gemini-3.8-flash` 或 `gemini-3.6-flash`；透過 `google-genai` SDK 與環境變數 `GEMINI_API_KEY` 呼叫）。
   - 模型輸出：`game_start_sec`（排除列隊與熱身）、各半局起訖秒、每個官網事件之 `[approx_start_sec, approx_end_sec]` 與 `needs_fine_scan` 旗標。
2. **細掃九宮格（Fine Scan，選用觸發）**：
   - 僅當相鄰打席間隔 `< 35s` 或模型標記 `needs_fine_scan=true`（例如快速交錯換棒），對該 `30–40s` 區段以 `2–3s` 間隔產出 1–2 張細掃九宮格精修。
3. **OpenCV 局部 ±2s 微調（Optional Refinement）**：
   - 僅在 VLM 給出的 `[t_est - 12s, t_est + 12s]` 小窗內，執行抗雲影紅土均值補償之本壘打擊區前景進入點／內野動態峰值檢查。
   - 若無觀眾遮擋（`left_occ <= 0.25`），在 `±2–5s` 範圍內鎖定打者站定起點並套用前置緩衝；若遇遮擋則原樣保留 VLM 秒數。
4. **本地快取防重複計費**：
   - 存入 `data/vlm_cache/{video_id}.json`（加入 `.gitignore`），記錄時間窗、模型版本、Prompt 版本與對齊結果；後續執行 `--align-only` 或增量同步時直接命中快取，同一盲區不重複呼叫 API。

### Phase 3：CLI、無 Key 降級策略與前端串接
1. **CLI 參數**（`src/clips_pipeline.py`）：
   - `--vlm-mode {off,blind_only,cv_only,force}`：
     - `blind_only`（預設）：僅針對盲區呼叫 VLM＋OpenCV 微調。
     - `cv_only`（選配）：無 API Key 時僅以純本機 OpenCV 處理無快取盲區。
     - `off`：停用軌道 C。
     - `force`：忽略既有 `data/vlm_cache/` 快取強制重跑。
   - `--vlm-model`（預設 `gemini-3.5-flash-lite`〔官方 `2.5-flash` 直接繼任者，免費層完整支援、速度最快、配額最寬鬆〕；亦可選用 `gemini-3.8-flash` 或 `gemini-3.6-flash`〔免費層同樣為 Free of charge，視覺推理能力更強〕）、`--no-vlm-opencv-refine`（可關閉局部微調以利對照）。
   - **金鑰安全規範**：`GEMINI_API_KEY` 僅透過本機環境變數或受 `.gitignore` 保護之 `.env` 載入，**嚴禁寫入任何受版控之程式碼、設定檔或計畫文件**。
2. **未設定 `GEMINI_API_KEY` 時的安全降級行為**：
   - **會自動跳過**：
     1. 不會呼叫外部 Gemini Vision API，終端機僅輸出略過提示，不使管線或 GitHub Actions CI 報錯中斷。
     2. 不會為無快取盲區額外透過 `yt-dlp` 下載影片區段與產生九宮格圖。
     3. 無快取的新盲區維持軌道 A＋B 的原本推估（如 `bracket_interp` / `scorebug_half_interp (low)`）；若選用 `--vlm-mode cv_only` 則可改跑純本機 OpenCV。
   - **仍然照常執行**：
     1. Phase 0 的 `clips_scorebug.py` 孤立單幀過濾與真實出局數防呆照常生效。
     2. 軌道 A（Scorebug）與軌道 B（本機 `faster-whisper` ASR）完整執行。
     3. 若 `data/vlm_cache/{video_id}.json` 已有先前產出之本地快取，仍會直接套用已快取的 VLM 結果。
     4. 人工覆寫（`data/player_clip_overrides.csv`）、入庫與前端靜態 JSON 匯出不受影響。
3. **新定位方法與前端型別**：
   - 新增 `align_method`：`vlm_fallback`（九宮格 AI 盲區定位，`medium`/`high`）與 `vlm_cv_refined`（九宮格 AI ＋ OpenCV 微調，`high`）。
   - 更新 `web/src/types/data.ts`（`ClipAlignMethod`）與 `web/src/views/player-clips.ts`（`formatConfidenceTitle` 繁體中文提示文案）。
   - 依賴與忽略清單：`requirements-clips.txt` 增列 `google-genai>=1.0.0`、`.gitignore` 增列 `data/vlm_cache/`。

### Phase 4：測試與強制驗證閘道
1. 新增離線單元測試 `tests/test_vlm_fallback.py`（Mock VLM 回應）：驗證孤立單幀過濾、盲區觸發規則（1上／1下觸發、4上不觸發）、九宮格拼接、JSON 解析容錯、快取命中與 OpenCV ±2s 邊界約束。
2. 執行全套強制驗證閘道：
   - Python：`.venv\Scripts\python.exe -m unittest discover -s tests`、`src/verify.py`、`src/export_json.py`、`src/export_clips_json.py`
   - 前端：`cd web && npm run typecheck && npm test && npm run build` 與 `npx modern-web-guidance@latest legacy-scan ./web/src`

### 預估成本
- 以 `XegkC7ILcrs`（81 分鐘）為例，僅針對 `0:00–14:20` 盲區送出約 10 張粗掃＋3–5 張細掃（合計 **13–15 張** `1278×720` 圖），預設使用免費層支援之 `gemini-3.5-flash-lite`（或可選 `gemini-3.8-flash` / `gemini-3.6-flash`）**在免費配額內為 Free of charge（零成本）**，且寫入本地快取後不重複計費（即使改走付費層亦遠低於 US$ 0.01）。

---

## 三、 範例對比：2026-10-26 11:00 比賽（`match_id=157` / `XegkC7ILcrs`）

- **觸發範圍**：全場 9 個半局、47 個打席中，**僅 `1局上`＋`1局下`（`#1~#8`，`0:00–14:20`）符合盲區條件並觸發軌道 C**（另微調銜接 `#9` 換局邊界）；其餘 **`#10~#47`（含已有 ASR 的 `4局上`）均不觸發 VLM，100% 沿用原軌秒數**。
- **盲區精度改善**：`#1~#8` 原管線偏差達 **150–260 秒**（1 上甚至被誤壓在開播前 26 秒內），新管線依 `0:00–6:35` 九宮格實看與 OpenCV v2 實驗結果，已將 `#1`、`#2`、`#3`、`#4`、`#6`、`#8` 全部拉回真實打擊起點。

| # | 局數 | 進攻方 | 打者 | 官網逐打席文字紀錄 | 舊管線 YouTube（秒數／方式） | 新管線 YouTube（秒數／方式） | 差異 (Δ) |
|---:|:---:|:---:|:---:|---|---|---|---:|
| **1** | **1上** | 小海盜(黑) | 曾〇瑞 | 曾〇瑞打擊出一壘安打 | [00:10 (`10s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=10s) `scorebug_anchor(誤高)` | **[02:16 (`136s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=136s)** `vlm_cv_refined(高)` | **+126s** |
| **2** | **1上** | 小海盜(黑) | 高〇展 | 高〇展打擊成滾地球，游擊手接到球傳向一壘刺殺打者出局 | [00:18 (`18s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=18s) `scorebug_anchor(誤高)` | **[02:48 (`168s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=168s)** `vlm_cv_refined(高)` *(牽制後 03:30 重打)* | **+150s** |
| **3** | **1上** | 小海盜(黑) | 楊〇皓 | 楊〇皓揮棒落空，遭到投手三振 | [00:26 (`26s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=26s) `scorebug_anchor(誤高)` | **[04:12 (`252s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=252s)** `vlm_cv_refined(高)` | **+226s** |
| **4** | **1下** | 仁美M | 黃〇胤 | 黃〇胤打擊成飛球遭到投手接殺出局 | [03:03 (`183s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=183s) `bracket_interp(低)` | **[06:35 (`395s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=395s)** `vlm_cv_refined(高)` | **+212s** |
| **5** | **1下** | 仁美M | 梁〇樂 | 梁〇樂獲得四壞球保送上壘 | [03:15 (`195s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=195s) `scorebug_est(低)` | **[07:35 (`455s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=455s)** `vlm_fallback(中)` | **+260s** |
| **6** | **1下** | 仁美M | 陳〇宇 | 陳〇宇打擊出二壘安打 | [06:01 (`361s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=361s) `scorebug_est(低)` | **[09:35 (`575s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=575s)** `vlm_cv_refined(高)` | **+214s** |
| **7** | **1下** | 仁美M | 陳〇 | 陳〇擊出球造成三壘手接球失誤上壘 | [11:20 (`680s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=680s) `bracket_interp(低)` | **[11:20 (`680s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=680s)** `vlm_fallback(中)` | 0s |
| **8** | **1下** | 仁美M | 吳〇頵 | 吳〇頵打擊出二壘安打，帶有1分打點 | [11:32 (`692s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=692s) `scorebug_est(低)` | **[13:55 (`835s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=835s)** `vlm_cv_refined(高)` | **+143s** |
| **9** | **2上** | 小海盜(黑) | 林〇恩 | 林〇恩打擊出內野安打 | [14:13 (`853s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=853s) `scorebug_anchor_interp(中)` | **[14:20 (`860s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=860s)** `換局邊界微調(中)` | **+7s** |
| 10 | 2上 | 小海盜(黑) | 楊〇皜 | 楊〇皜打擊出二壘安打，帶有1分打點 | [17:46 (`1066s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=1066s) `dual_track(高)` | [17:46 (`1066s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=1066s) *(沿用)* | 0s |
| 11 | 2上 | 小海盜(黑) | 羅〇宣 | 羅〇宣揮棒落空，遭到投手三振 | [20:29 (`1229s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=1229s) `dual_track(高)` | [20:29 (`1229s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=1229s) *(沿用)* | 0s |
| 12 | 2上 | 小海盜(黑) | 陳〇翔 | 陳〇翔打擊出二壘安打，帶有1分打點 | [22:25 (`1345s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=1345s) `dual_track(高)` | [22:25 (`1345s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=1345s) *(沿用)* | 0s |
| 13 | 2上 | 小海盜(黑) | 潘〇劭 | 潘〇劭揮棒落空，遭到投手三振 | [25:26 (`1526s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=1526s) `dual_track(高)` | [25:26 (`1526s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=1526s) *(沿用)* | 0s |
| 14 | 2上 | 小海盜(黑) | 陳〇翊 | 陳〇翊揮棒落空，遭到投手三振 | [25:48 (`1548s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=1548s) `dual_track(高)` | [25:48 (`1548s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=1548s) *(沿用)* | 0s |
| 15 | 2下 | 仁美M | 黃〇愷 | 黃〇愷打擊出內野安打 | [25:58 (`1558s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=1558s) `scorebug_half_start(高)` | [25:58 (`1558s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=1558s) *(沿用)* | 0s |
| 16 | 2下 | 仁美M | 余〇澔 | 余〇澔打擊到內野的滾地球，游擊手接到後傳給二壘手使跑者出局 | [29:33 (`1773s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=1773s) `scorebug_anchor_interp(中)` | [29:33 (`1773s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=1773s) *(沿用)* | 0s |
| 17 | 2下 | 仁美M | 蔣〇誠 | 蔣〇誠靠不死三振上壘 | [29:45 (`1785s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=1785s) `scorebug_est(中)` | [29:45 (`1785s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=1785s) *(沿用)* | 0s |
| 18 | 2下 | 仁美M | 黃〇胤 | 黃〇胤打擊到外野的飛球，遭到中外野手接殺出局 | [33:16 (`1996s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=1996s) `scorebug_anchor(高)` | [33:16 (`1996s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=1996s) *(沿用)* | 0s |
| 19 | 3上 | 小海盜(黑) | 曾〇瑞 | 曾〇瑞揮棒落空，遭到投手三振 | [34:08 (`2048s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=2048s) `dual_track(高)` | [34:08 (`2048s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=2048s) *(沿用)* | 0s |
| 20 | 3上 | 小海盜(黑) | 高〇展 | 高〇展打到投手方向的滾地球，投手接到傳給一壘手刺殺打者出局 | [35:21 (`2121s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=2121s) `dual_track(高)` | [35:21 (`2121s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=2121s) *(沿用)* | 0s |
| 21 | 3上 | 小海盜(黑) | 楊〇皓 | 楊〇皓打擊成內野滾地球，二壘手接到後傳給一壘手刺殺打者出局 | [36:25 (`2185s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=2185s) `dual_track(高)` | [36:25 (`2185s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=2185s) *(沿用)* | 0s |
| 22 | 3下 | 仁美M | 梁〇樂 | 梁〇樂揮棒落空，遭到投手三振 | [37:03 (`2223s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=2223s) `scorebug_half_start(高)` | [37:03 (`2223s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=2223s) *(沿用)* | 0s |
| 23 | 3下 | 仁美M | 陳〇宇 | 陳〇宇打擊成滾地球，游擊手接到球傳向一壘刺殺打者出局 | [40:16 (`2416s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=2416s) `scorebug_anchor(高)` | [40:16 (`2416s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=2416s) *(沿用)* | 0s |
| 24 | 3下 | 仁美M | 陳〇 | 陳〇打擊成內野滾地球，二壘手接到後傳給一壘手刺殺打者出局 | [41:56 (`2516s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=2516s) `scorebug_anchor(高)` | [41:56 (`2516s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=2516s) *(沿用)* | 0s |
| 25 | 4上 | 小海盜(黑) | 梁〇華 | 梁〇華打擊成內野滾地球，二壘手接到後傳給一壘手刺殺打者出局 | [43:35 (`2615s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=2615s) `dual_track(高)` | [43:35 (`2615s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=2615s) *(ASR已覆蓋・不觸發)* | 0s |
| 26 | 4上 | 小海盜(黑) | 楊〇皜 | 楊〇皜打擊到內野的滾地球，三壘手接到後傳向一壘刺殺打者出局 | [44:05 (`2645s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=2645s) `dual_track(高)` | [44:05 (`2645s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=2645s) *(沿用)* | 0s |
| 27 | 4上 | 小海盜(黑) | 羅〇宣 | 羅〇宣獲得四壞球保送上壘 | [44:28 (`2668s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=2668s) `dual_track(高)` | [44:28 (`2668s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=2668s) *(沿用)* | 0s |
| 28 | 4上 | 小海盜(黑) | 陳〇翔 | 陳〇翔擊出球造成二壘手接球失誤上壘 | [46:42 (`2802s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=2802s) `dual_track(高)` | [46:42 (`2802s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=2802s) *(沿用)* | 0s |
| 29 | 4上 | 小海盜(黑) | 龍〇恩 | 龍〇恩打擊出內野安打 | [47:16 (`2836s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=2836s) `dual_track(高)` | [47:16 (`2836s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=2836s) *(沿用)* | 0s |
| 30 | 4上 | 小海盜(黑) | 陳〇毅 | 陳〇毅打擊成內野滾地球，二壘手接到後傳給一壘手刺殺打者出局 | [47:46 (`2866s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=2866s) `dual_track(高)` | [47:46 (`2866s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=2866s) *(沿用)* | 0s |
| 31 | 4下 | 仁美M | 吳〇頵 | 吳〇頵打擊出一壘安打 | [48:48 (`2928s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=2928s) `scorebug_half_start(高)` | [48:48 (`2928s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=2928s) *(沿用)* | 0s |
| 32 | 4下 | 仁美M | 林〇燁 | 林〇燁打擊成滾地球，游擊手接到球傳向一壘刺殺打者出局，帶有1分打點 | [53:26 (`3206s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=3206s) `scorebug_anchor_interp(中)` | [53:26 (`3206s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=3206s) *(沿用)* | 0s |
| 33 | 4下 | 仁美M | 黃〇愷 | 黃〇愷獲得四壞球保送上壘 | [53:38 (`3218s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=3218s) `scorebug_est(中)` | [53:38 (`3218s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=3218s) *(沿用)* | 0s |
| 34 | 4下 | 仁美M | 余〇澔 | 余〇澔獲得四壞球保送上壘 | [56:08 (`3368s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=3368s) `scorebug_est(中)` | [56:08 (`3368s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=3368s) *(沿用)* | 0s |
| 35 | 4下 | 仁美M | 蔣〇誠 | 蔣〇誠遭到觸身球保送上壘 | [58:38 (`3518s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=3518s) `scorebug_est(中)` | [58:38 (`3518s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=3518s) *(沿用)* | 0s |
| 36 | 4下 | 仁美M | 黃〇胤 | 黃〇胤打擊成飛球遭到游擊手接殺出局 | [61:08 (`3668s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=3668s) `scorebug_est(中)` | [61:08 (`3668s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=3668s) *(沿用)* | 0s |
| 37 | 4下 | 仁美M | 李〇祐 | 李〇祐遭到觸身球保送上壘，帶有1分打點 | [63:38 (`3818s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=3818s) `scorebug_est(中)` | [63:38 (`3818s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=3818s) *(沿用)* | 0s |
| 38 | 4下 | 仁美M | 陳〇宇 | 陳〇宇打擊出二壘安打，帶有2分打點 | [65:53 (`3953s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=3953s) `scorebug_est(中)` | [65:53 (`3953s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=3953s) *(沿用)* | 0s |
| 39 | 4下 | 仁美M | 陳〇 | 陳〇獲得四壞球保送上壘 | [68:08 (`4088s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=4088s) `scorebug_est(中)` | [68:08 (`4088s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=4088s) *(沿用)* | 0s |
| 40 | 4下 | 仁美M | 饒〇睎 | 饒〇睎揮棒落空，遭到投手三振 | [70:23 (`4223s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=4223s) `scorebug_est(中)` | [70:23 (`4223s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=4223s) *(沿用)* | 0s |
| 41 | 5上 | 小海盜(黑) | 曾〇瑞 | 曾〇瑞打擊成滾地球，游擊手接到球傳向一壘刺殺打者出局 | [73:23 (`4403s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=4403s) `scorebug_anchor(高)` | [73:23 (`4403s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=4403s) *(沿用)* | 0s |
| 42 | 5上 | 小海盜(黑) | 陳〇然 | 陳〇然擊出球造成一壘手接球失誤上壘 | [74:23 (`4463s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=4463s) `scorebug_anchor_interp(中)` | [74:23 (`4463s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=4463s) *(沿用)* | 0s |
| 43 | 5上 | 小海盜(黑) | 楊〇皓 | 楊〇皓擊出球造成一壘手接球失誤上壘 | [75:23 (`4523s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=4523s) `scorebug_anchor_interp(中)` | [75:23 (`4523s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=4523s) *(沿用)* | 0s |
| 44 | 5上 | 小海盜(黑) | 梁〇華 | 梁〇華打擊出三壘安打，帶有2分打點 | [77:15 (`4635s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=4635s) `dual_track(高)` | [77:15 (`4635s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=4635s) *(沿用)* | 0s |
| 45 | 5上 | 小海盜(黑) | 楊〇皜 | 楊〇皜打擊到外野的飛球，遭到中外野手接殺出局 | [78:25 (`4705s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=4705s) `dual_track(高)` | [78:25 (`4705s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=4705s) *(沿用)* | 0s |
| 46 | 5上 | 小海盜(黑) | 羅〇宣 | 羅〇宣擊出球造成三壘手傳球失誤上壘 | [78:53 (`4733s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=4733s) `dual_track(高)` | [78:53 (`4733s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=4733s) *(沿用)* | 0s |
| 47 | 5上 | 小海盜(黑) | 陳〇翔 | 陳〇翔打擊成飛球遭到三壘手接殺出局 | [80:10 (`4810s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=4810s) `dual_track(高)` | [80:10 (`4810s`)](https://www.youtube.com/watch?v=XegkC7ILcrs&t=4810s) *(沿用)* | 0s |
