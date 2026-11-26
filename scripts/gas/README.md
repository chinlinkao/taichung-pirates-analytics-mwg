# Google Apps Script 每日定時觸發設定指南

本目錄存放用於取代或補強 GitHub Actions `on.schedule` 的 **Google Apps Script（GAS）** 定時觸發腳本，透過每天台灣時間早上主動呼叫 GitHub `workflow_dispatch` API 來啟動 [update-data.yml](../../.github/workflows/update-data.yml)。

## 設計與雙重保險機制

1. **為何需要外部觸發**：GitHub Actions 原生 `on.schedule` 在高負載時可能延遲數十分鐘甚至直接被略過（dropped）；改由 GAS 主動發送 `workflow_dispatch` 不受 cron 佇列限制，通常數秒內就會啟動。
2. **雙重保險與防衝突**：
   - [update-data.yml](../../.github/workflows/update-data.yml) 仍保留原本的 `on.schedule`（`41 1 * * *`，UTC 01:41 = 台灣時間 09:41）。
   - Workflow 內已設定 `concurrency`（`group: "data-pipeline"`, `cancel-in-progress: false`），即使 GAS 與 GitHub cron 同一天先後觸發，也只會依序排隊執行，不會互相覆蓋或衝突。
   - 若當日無新賽事異動，`detect` 步驟會偵測 `data_changed=false` 並自動略過 commit 與重部署。

---

## 設定步驟（約 3–5 分鐘）

### 步驟 1：建立最小權限 GitHub Fine-grained PAT

1. 登入 GitHub，前往 **Settings → Developer settings → Personal access tokens → Fine-grained tokens**，點擊 **Generate new token**。
2. 填寫以下設定：
   - **Token name**：`gas-pirates-daily-sync`
   - **Expiration**：建議選 `365 days`（或 `90 days`），並記下到期日。
   - **Repository access**：選擇 **Only select repositories** → 選取 `chinlinkao/taichung-pirates-analytics-mwg`。
   - **Permissions → Repository permissions**：僅將 **Actions** 設為 **Read and write**，其餘保持 `No access`。
3. 點擊 **Generate token** 並複製保存（只會顯示一次）。

### 步驟 2：建立 Google Apps Script 專案與時區設定

1. 開啟 [script.google.com](https://script.google.com) → 點擊 **新專案**，可命名為 `台中小海盜每日數據同步`。
2. 點擊左側選單的 **專案設定（齒輪圖示）**：
   - 勾選 **「在編輯器中顯示 "appsscript.json" 資訊清單檔案」**。
3. 切回左側 **編輯器（`<>` 圖示）** → 點擊 `appsscript.json`，確認 `"timeZone"` 為 `"Asia/Taipei"`（若非台北時區請手動修改並儲存）：
   ```json
   {
     "timeZone": "Asia/Taipei",
     "exceptionLogging": "STACKDRIVER",
     "runtimeVersion": "V8"
   }
   ```

### 步驟 3：設定指令碼屬性（Script Properties）

回到左側 **專案設定（齒輪圖示）** → 滑到底部 **指令碼屬性** → 點擊 **新增指令碼屬性**，依序填入：

| 屬性名稱 | 必填 | 說明與範例 |
| :--- | :--- | :--- |
| `GITHUB_PAT` | **必填** | 貼上步驟 1 產生的 GitHub Fine-grained PAT |
| `PAT_EXPIRES_AT` | 選填 | PAT 到期日（格式 `YYYY-MM-DD`，例如 `2027-11-26`），到期前 7 天會自動寄信提醒續期 |
| `NOTIFY_EMAIL` | 選填 | 失敗告警／續期提醒收件信箱；未設定時預設寄給 GAS 專案擁有者的 Gmail |

### 步驟 4：貼上程式碼並建立每日排程

1. 切回 **編輯器** → 開啟 `Code.gs`，清空預設內容後，完整貼上 [trigger_update_data.gs](./trigger_update_data.gs) 的內容並儲存（`Ctrl+S`）。
2. 在上方工具列函式下拉選單選擇 **`setupDailyTrigger`**，點擊 **執行**。
3. 首次執行會跳出 Google 授權視窗（允許連線外部網址與寄送提醒郵件）：
   - 若出現「Google 尚未驗證這個應用程式」，點擊 **進階** → **前往「台中小海盜每日數據同步」（不安全）** → **允許**。
4. 執行完成後，下方執行紀錄會顯示已建立觸發器，每天將於台灣時間 **09:25–09:55** 之間自動執行 `triggerDailySync`。
   - 可於左側 **觸發條件（鬧鐘圖示）** 確認已存在一筆每日執行的時間驅動觸發器。

### 步驟 5：立即測試連線

1. 在上方工具列函式下拉選單選擇 **`testConnection`**，點擊 **執行**。
2. 預期結果：
   - GAS 執行紀錄顯示 `觸發成功 (HTTP 204)`。
   - 前往 [GitHub Actions (`update-data.yml`)](https://github.com/chinlinkao/taichung-pirates-analytics-mwg/actions/workflows/update-data.yml) 可看到剛新增一筆 `workflow_dispatch` 執行紀錄。

---

## 常見故障排除

| 狀態碼 / 現象 | 可能原因與處理方式 |
| :--- | :--- |
| **HTTP 401** | `GITHUB_PAT` 無效、複製時多出空白或已過期 → 重新產生 PAT 並更新指令碼屬性 `GITHUB_PAT` |
| **HTTP 403** | Token 權限不足或未授權該 Repository → 確認 PAT 已對 `chinlinkao/taichung-pirates-analytics-mwg` 開啟 `Actions: Read and write` |
| **HTTP 404** | Repo 名稱、Workflow 檔名（`update-data.yml`）或分支（`main`）對不上 → 檢查腳本頂部常數或 PAT 是否選對 Repository |
| **HTTP 429 / 5xx** | GitHub API 暫時限流或服務異常 → 腳本會自動間隔 15 秒重試最多 2 次，若仍失敗會寄送告警信 |
| **想暫停 GAS 排程** | 在 GAS 編輯器選擇執行 **`removeDailyTrigger`**，或直接至左側 **觸發條件** 刪除觸發器即可 |
