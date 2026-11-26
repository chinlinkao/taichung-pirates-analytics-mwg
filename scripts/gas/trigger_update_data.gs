/**
 * 台中小海盜戰情看板 — Google Apps Script 每日定時觸發器
 *
 * 用途：透過 GitHub REST API 主動觸發 update-data.yml 的 workflow_dispatch 事件，
 *       避開 GitHub Actions on.schedule 在高負載時可能發生的延遲或漏觸發問題，
 *       並與原本的 on.schedule 排程並行作為雙重保險。
 */

const GITHUB_OWNER = "chinlinkao";
const GITHUB_REPO = "taichung-pirates-analytics-mwg";
const WORKFLOW_FILE = "update-data.yml";
const TARGET_BRANCH = "main";
const TIMEZONE = "Asia/Taipei";
const TRIGGER_HOUR = 9;
const TRIGGER_MINUTE = 40;
const MAX_RETRIES = 2; // 首次呼叫 + 最多重試 2 次 = 共 3 次
const RETRY_DELAY_MS = 15000; // 15 秒
const PAT_EXPIRY_WARN_DAYS = 7;
const ACTIONS_PAGE_URL =
  "https://github.com/chinlinkao/taichung-pirates-analytics-mwg/actions/workflows/update-data.yml";
const PAT_SETTINGS_URL = "https://github.com/settings/personal-access-tokens";

const NON_RETRYABLE_STATUS_CODES = [401, 403, 404, 422];

/**
 * 每日排程主函式：檢查 PAT 到期日並觸發 GitHub Actions workflow_dispatch。
 */
function triggerDailySync() {
  checkPatExpiration_();

  const props = PropertiesService.getScriptProperties();
  const rawToken = props.getProperty("GITHUB_PAT");
  const token = rawToken ? rawToken.trim() : "";

  if (!token) {
    const errMsg = "未設定指令碼屬性 GITHUB_PAT，無法觸發 GitHub Actions。";
    Logger.log(errMsg);
    sendAlertEmail_(
      "[台中小海盜同步告警] 缺少 GITHUB_PAT 設定",
      `${errMsg}\n\n請至 Google Apps Script「專案設定 → 指令碼屬性」新增 GITHUB_PAT。\nActions 頁面：${ACTIONS_PAGE_URL}`
    );
    throw new Error(errMsg);
  }

  const url = `https://api.github.com/repos/${GITHUB_OWNER}/${GITHUB_REPO}/actions/workflows/${WORKFLOW_FILE}/dispatches`;
  const options = {
    method: "post",
    contentType: "application/json",
    muteHttpExceptions: true,
    headers: {
      Accept: "application/vnd.github+json",
      Authorization: `Bearer ${token}`,
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "taichung-pirates-gas-scheduler",
    },
    // 沿用 update-data.yml 預設輸入值（auto / false），自動帶入 --auto-current
    payload: JSON.stringify({ ref: TARGET_BRANCH }),
  };

  let lastStatus = null;
  let lastErrorText = "";

  for (let attempt = 0; attempt <= MAX_RETRIES; attempt++) {
    try {
      const response = UrlFetchApp.fetch(url, options);
      lastStatus = response.getResponseCode();
      lastErrorText = response.getContentText() || "(無回應內容)";

      if (lastStatus === 204) {
        Logger.log(`觸發成功 (HTTP 204)：已送出 ${WORKFLOW_FILE} (${TARGET_BRANCH}) 的 workflow_dispatch。`);
        return;
      }

      Logger.log(`第 ${attempt + 1} 次嘗試失敗，HTTP 狀態碼：${lastStatus}，回應：${lastErrorText}`);

      if (NON_RETRYABLE_STATUS_CODES.indexOf(lastStatus) !== -1) {
        break;
      }
    } catch (err) {
      lastStatus = "NETWORK_ERROR";
      lastErrorText = err && err.message ? err.message : String(err);
      Logger.log(`第 ${attempt + 1} 次嘗試發生網路或執行例外：${lastErrorText}`);
    }

    if (attempt < MAX_RETRIES) {
      Logger.log(`等待 ${RETRY_DELAY_MS / 1000} 秒後進行第 ${attempt + 2} 次重試...`);
      Utilities.sleep(RETRY_DELAY_MS);
    }
  }

  const failSummary = `觸發 GitHub Actions 失敗（最後狀態：${lastStatus}）：${lastErrorText}`;
  sendAlertEmail_(
    `[台中小海盜同步告警] GitHub Actions 觸發失敗 (${lastStatus})`,
    `${failSummary}\n\n請檢查 GITHUB_PAT 是否過期或權限是否足夠，或至以下網址手動補跑：\n${ACTIONS_PAGE_URL}`
  );
  throw new Error(failSummary);
}

/**
 * 手動測試函式：立即執行一次觸發，用於部署後驗證連線與 Token 權限。
 */
function testConnection() {
  Logger.log("開始執行連線測試...");
  triggerDailySync();
  Logger.log(`連線測試完成，請前往 GitHub Actions 確認執行狀態：${ACTIONS_PAGE_URL}`);
}

/**
 * 一鍵建立每日時間驅動觸發器（先清除舊的同名觸發器避免重複）。
 */
function setupDailyTrigger() {
  removeDailyTrigger();

  ScriptApp.newTrigger("triggerDailySync")
    .timeBased()
    .everyDays(1)
    .atHour(TRIGGER_HOUR)
    .nearMinute(TRIGGER_MINUTE)
    .inTimezone(TIMEZONE)
    .create();

  Logger.log(
    `排程設定完成：將於每日台灣時間 (${TIMEZONE}) 09:25–09:55 之間自動觸發 triggerDailySync。`
  );
}

/**
 * 移除所有綁定於 triggerDailySync 的既有觸發器。
 */
function removeDailyTrigger() {
  const triggers = ScriptApp.getProjectTriggers();
  let removedCount = 0;

  for (let i = 0; i < triggers.length; i++) {
    if (triggers[i].getHandlerFunction() === "triggerDailySync") {
      ScriptApp.deleteTrigger(triggers[i]);
      removedCount++;
    }
  }

  Logger.log(`已移除 ${removedCount} 筆既有的 triggerDailySync 觸發器。`);
}

/**
 * 檢查指令碼屬性 PAT_EXPIRES_AT（格式 YYYY-MM-DD），於到期前 7 天內寄信提醒續期。
 * 本函式僅發送提醒，不中斷主同步流程。
 */
function checkPatExpiration_() {
  const rawExpires = PropertiesService.getScriptProperties().getProperty("PAT_EXPIRES_AT");
  const expiresStr = rawExpires ? rawExpires.trim() : "";
  if (!expiresStr) {
    return;
  }

  if (!/^\d{4}-\d{2}-\d{2}$/.test(expiresStr)) {
    Logger.log(`PAT_EXPIRES_AT 格式不正確（應為 YYYY-MM-DD）：${expiresStr}`);
    return;
  }

  const expiryDate = new Date(`${expiresStr}T23:59:59+08:00`);
  if (isNaN(expiryDate.getTime())) {
    Logger.log(`無法解析 PAT_EXPIRES_AT 日期：${expiresStr}`);
    return;
  }

  const msPerDay = 24 * 60 * 60 * 1000;
  const remainingDays = Math.ceil((expiryDate.getTime() - Date.now()) / msPerDay);

  if (remainingDays <= PAT_EXPIRY_WARN_DAYS) {
    const statusText =
      remainingDays <= 0
        ? `已於 ${expiresStr} 到期（或超過到期日）`
        : `將於 ${expiresStr} 到期（剩餘約 ${remainingDays} 天）`;

    Logger.log(`PAT 到期提醒：${statusText}`);
    sendAlertEmail_(
      "[台中小海盜同步提醒] GitHub PAT 即將到期或已過期",
      `您設定的 GITHUB_PAT ${statusText}。\n\n請儘速前往 GitHub 重新產生或延長 Fine-grained PAT，並更新 Google Apps Script 指令碼屬性 GITHUB_PAT 與 PAT_EXPIRES_AT：\n${PAT_SETTINGS_URL}`
    );
  }
}

/**
 * 寄送告警或提醒信件（優先使用指令碼屬性 NOTIFY_EMAIL，否則寄給腳本擁有者）。
 * 外層捕捉例外，避免寄信配額或權限問題遮蔽原本的錯誤流程。
 *
 * @param {string} subject 信件主旨
 * @param {string} body 信件內文
 */
function sendAlertEmail_(subject, body) {
  try {
    const customEmail = PropertiesService.getScriptProperties().getProperty("NOTIFY_EMAIL");
    const recipient = (customEmail ? customEmail.trim() : "") || Session.getEffectiveUser().getEmail();

    if (!recipient) {
      Logger.log(`找不到收件人信箱，略過寄送通知：${subject}`);
      return;
    }

    MailApp.sendEmail(recipient, subject, body);
    Logger.log(`已寄送通知信至 ${recipient}：${subject}`);
  } catch (mailErr) {
    const msg = mailErr && mailErr.message ? mailErr.message : String(mailErr);
    Logger.log(`寄送通知信失敗（不影響主流程錯誤回報）：${msg}`);
  }
}
