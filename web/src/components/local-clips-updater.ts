/**
 * 本機開發專屬：更新球員影片片段互動視窗 (local-clips-updater.ts)
 * - 僅在 import.meta.env.DEV 由 main.ts 動態載入，正式建置會完全 Tree-Shake 移除
 * - 遵循 MWG 1.0 原生 <dialog> 規範（不加 role="dialog"、aria-modal、手動 Esc 或 focus trap）
 */

const API_BASE = "/__local-api/clips-update";
const DEFAULT_SINCE = "2026-11-01";
const FIXED_ASR_MODE = "speech_pa";
const FIXED_MODEL = "small";
const MAX_DISPLAY_LOGS = 800;

type JobStatus = "idle" | "running" | "succeeded" | "no_changes" | "failed" | "cancelled";
type StepId = "git_pull" | "clips_pipeline" | "check_diff" | "git_push";
type StepStatus = "pending" | "running" | "done" | "error" | "skipped";

interface StepState {
  id: StepId;
  label: string;
  status: StepStatus;
}

interface JobStatePayload {
  status: JobStatus;
  step: StepId | null;
  steps: StepState[];
  startedAt: string | null;
  finishedAt: string | null;
  commandPreview: string;
  summary: string | null;
  commitSha: string | null;
  autoGitPush: boolean;
}

interface JobSnapshot extends JobStatePayload {
  logs: string[];
}

export interface LocalClipsUpdaterOptions {
  onRunningChange?: (running: boolean) => void;
  onClipsUpdated?: () => void;
}

export interface LocalClipsUpdaterController {
  dialogElement: HTMLDialogElement;
  open: () => void;
  destroy: () => void;
}

function buildCommandText(since: string): string {
  return `.venv\\Scripts\\python.exe -u src/clips_pipeline.py --since ${since} --asr-mode ${FIXED_ASR_MODE} --model ${FIXED_MODEL}`;
}

function getDefaultSteps(): StepState[] {
  return [
    { id: "git_pull", label: "同步遠端最新賽事資料 (git pull --rebase)", status: "pending" },
    { id: "clips_pipeline", label: "執行雙軌影片定位管線 (clips_pipeline.py)", status: "pending" },
    { id: "check_diff", label: "檢查 player-clips.json 實質異動", status: "pending" },
    { id: "git_push", label: "提交並推送至 GitHub (git commit & push)", status: "pending" },
  ];
}

function stepStatusIcon(status: StepStatus): string {
  switch (status) {
    case "running":
      return "⏳";
    case "done":
      return "✅";
    case "error":
      return "❌";
    case "skipped":
      return "⏭️";
    default:
      return "○";
  }
}

export function createLocalClipsUpdater(
  options: LocalClipsUpdaterOptions
): LocalClipsUpdaterController {
  const dialog = document.createElement("dialog");
  dialog.className = "clips-updater-dialog";
  dialog.setAttribute("closedby", "closerequest");
  dialog.setAttribute("aria-labelledby", "clips-updater-title");

  const closeForm = document.createElement("form");
  closeForm.id = "clips-updater-close-form";
  closeForm.method = "dialog";
  closeForm.hidden = true;

  const shell = document.createElement("div");
  shell.className = "drawer-shell";

  // 標題列
  const header = document.createElement("div");
  header.className = "drawer-header";

  const title = document.createElement("h1");
  title.id = "clips-updater-title";
  title.className = "clips-updater-title";
  title.textContent = "本機更新球員影片片段";

  const headerCloseBtn = document.createElement("button");
  headerCloseBtn.type = "submit";
  headerCloseBtn.setAttribute("form", closeForm.id);
  headerCloseBtn.value = "close";
  headerCloseBtn.className = "btn btn-ghost";
  headerCloseBtn.setAttribute("aria-label", "關閉更新視窗");
  headerCloseBtn.textContent = "✕";

  header.append(title, headerCloseBtn);

  // 內容區
  const body = document.createElement("div");
  body.className = "drawer-body clips-updater-body";

  const lead = document.createElement("p");
  lead.className = "clips-updater-lead";
  lead.textContent =
    "本操作僅於本機開發模式執行：先同步 GitHub 最新賽果資料庫，再呼叫雙軌影片定位腳本產生第五頁籤資料，若有實質異動將自動提交並推送至 GitHub。";

  // 指令預覽
  const cmdWrap = document.createElement("div");
  cmdWrap.className = "clips-cmd-wrap";

  const cmdLabel = document.createElement("span");
  cmdLabel.className = "clips-field-label";
  cmdLabel.textContent = "執行指令預覽";

  const cmdPreview = document.createElement("code");
  cmdPreview.className = "clips-cmd-preview";
  cmdPreview.textContent = buildCommandText(DEFAULT_SINCE);

  cmdWrap.append(cmdLabel, cmdPreview);

  // 參數設定區
  const formGrid = document.createElement("div");
  formGrid.className = "clips-form-grid";

  const sinceField = document.createElement("div");
  sinceField.className = "clips-field";

  const sinceLabel = document.createElement("label");
  sinceLabel.className = "clips-field-label";
  sinceLabel.htmlFor = "clips-since-input";
  sinceLabel.textContent = "起始比賽日期 (--since)";

  const sinceInput = document.createElement("input");
  sinceInput.id = "clips-since-input";
  sinceInput.type = "date";
  sinceInput.className = "clips-date-input";
  sinceInput.value = DEFAULT_SINCE;
  sinceInput.required = true;
  sinceInput.addEventListener("input", () => {
    const val = sinceInput.value.trim() || DEFAULT_SINCE;
    cmdPreview.textContent = buildCommandText(val);
  });

  sinceField.append(sinceLabel, sinceInput);

  const pushOptionLabel = document.createElement("label");
  pushOptionLabel.className = "clips-checkbox-label";

  const autoPushCheckbox = document.createElement("input");
  autoPushCheckbox.id = "clips-auto-push-input";
  autoPushCheckbox.type = "checkbox";
  autoPushCheckbox.checked = true;

  const autoPushText = document.createElement("span");
  autoPushText.textContent =
    "更新前先 git pull --rebase，完成後自動 git commit & git push 至 GitHub";

  pushOptionLabel.append(autoPushCheckbox, autoPushText);
  formGrid.append(sinceField, pushOptionLabel);

  // 4 步驟進度清單
  const stepsList = document.createElement("ol");
  stepsList.className = "clips-steps";
  stepsList.setAttribute("aria-label", "執行步驟進度");

  // 狀態通知列
  const statusBanner = document.createElement("div");
  statusBanner.className = "clips-status-banner";
  statusBanner.setAttribute("role", "status");
  statusBanner.setAttribute("aria-live", "polite");
  statusBanner.dataset.state = "idle";
  statusBanner.textContent = "待命中：確認參數後點擊「開始更新影片片段」。";

  // 即時終端日誌輸出
  const logWrap = document.createElement("div");
  logWrap.className = "clips-log-wrap";

  const logHeader = document.createElement("span");
  logHeader.className = "clips-field-label";
  logHeader.textContent = "執行紀錄輸出";

  const logOutput = document.createElement("pre");
  logOutput.className = "clips-log-output";
  logOutput.setAttribute("role", "log");
  logOutput.setAttribute("aria-live", "off");
  logOutput.textContent = "尚未開始執行。";

  logWrap.append(logHeader, logOutput);
  body.append(lead, cmdWrap, formGrid, stepsList, statusBanner, logWrap);

  // 底部按鈕列
  const footer = document.createElement("div");
  footer.className = "drawer-footer clips-updater-footer";

  const cancelTaskBtn = document.createElement("button");
  cancelTaskBtn.type = "button";
  cancelTaskBtn.className = "btn clips-cancel-btn";
  cancelTaskBtn.textContent = "終止任務";
  cancelTaskBtn.hidden = true;

  const footerRight = document.createElement("div");
  footerRight.className = "toolbar-group";

  const closeFooterBtn = document.createElement("button");
  closeFooterBtn.type = "submit";
  closeFooterBtn.setAttribute("form", closeForm.id);
  closeFooterBtn.value = "close";
  closeFooterBtn.className = "btn";
  closeFooterBtn.setAttribute("autofocus", "");
  closeFooterBtn.textContent = "關閉";

  const startBtn = document.createElement("button");
  startBtn.type = "button";
  startBtn.className = "btn btn-primary";
  startBtn.textContent = "開始更新影片片段";

  footerRight.append(closeFooterBtn, startBtn);
  footer.append(cancelTaskBtn, footerRight);

  shell.append(header, body, footer);
  dialog.append(closeForm, shell);

  let currentStatus: JobStatus = "idle";
  let currentLogs: string[] = [];
  let eventSource: EventSource | null = null;
  let isDestroyed = false;

  const renderSteps = (steps: StepState[]): void => {
    const items = steps.map((step) => {
      const li = document.createElement("li");
      li.className = "clips-step-item";
      li.dataset.stepStatus = step.status;

      const icon = document.createElement("span");
      icon.className = "clips-step-icon";
      icon.setAttribute("aria-hidden", "true");
      icon.textContent = stepStatusIcon(step.status);

      const label = document.createElement("span");
      label.textContent = step.label;

      li.append(icon, label);
      return li;
    });
    stepsList.replaceChildren(...items);
  };

  const renderLogs = (): void => {
    if (currentLogs.length === 0) {
      logOutput.textContent = currentStatus === "running" ? "正在啟動程序..." : "尚未開始執行。";
      return;
    }
    logOutput.textContent = currentLogs.join("\n");
    logOutput.scrollTop = logOutput.scrollHeight;
  };

  const applyJobState = (payload: JobStatePayload): void => {
    const prevStatus = currentStatus;
    currentStatus = payload.status;
    const isRunning = payload.status === "running";

    sinceInput.disabled = isRunning;
    autoPushCheckbox.disabled = isRunning;
    startBtn.disabled = isRunning;
    cancelTaskBtn.hidden = !isRunning;
    cancelTaskBtn.disabled = false;
    closeFooterBtn.textContent = isRunning ? "背景執行" : "關閉";

    renderSteps(payload.steps.length > 0 ? payload.steps : getDefaultSteps());

    statusBanner.dataset.state = payload.status;
    if (payload.summary) {
      statusBanner.textContent = payload.summary;
    } else if (payload.status === "idle") {
      statusBanner.textContent = "待命中：確認參數後點擊「開始更新影片片段」。";
    }

    if ((prevStatus === "running") !== isRunning) {
      options.onRunningChange?.(isRunning);
    }
    if (prevStatus === "running" && payload.status === "succeeded") {
      options.onClipsUpdated?.();
    }
  };

  const closeStream = (): void => {
    if (eventSource) {
      eventSource.close();
      eventSource = null;
    }
  };

  const connectStream = (): void => {
    if (eventSource || isDestroyed) {
      return;
    }
    const source = new EventSource(`${API_BASE}/stream`);
    eventSource = source;

    source.addEventListener("snapshot", (event) => {
      if (!(event instanceof MessageEvent)) return;
      try {
        const snap = JSON.parse(String(event.data)) as JobSnapshot;
        currentLogs = Array.isArray(snap.logs) ? snap.logs.slice(-MAX_DISPLAY_LOGS) : [];
        applyJobState(snap);
        renderLogs();
      } catch {
        /* 忽略格式異常 */
      }
    });

    source.addEventListener("state", (event) => {
      if (!(event instanceof MessageEvent)) return;
      try {
        const nextState = JSON.parse(String(event.data)) as JobStatePayload;
        applyJobState(nextState);
        if (nextState.status !== "running" && !dialog.open) {
          closeStream();
        }
      } catch {
        /* 忽略格式異常 */
      }
    });

    source.addEventListener("log", (event) => {
      if (!(event instanceof MessageEvent)) return;
      try {
        const parsed = JSON.parse(String(event.data)) as { line?: string };
        if (typeof parsed.line === "string") {
          currentLogs.push(parsed.line);
          if (currentLogs.length > MAX_DISPLAY_LOGS) {
            currentLogs.splice(0, currentLogs.length - MAX_DISPLAY_LOGS);
          }
          renderLogs();
        }
      } catch {
        /* 忽略格式異常 */
      }
    });

    source.onerror = () => {
      if (currentStatus !== "running" && !dialog.open) {
        closeStream();
      }
    };
  };

  startBtn.addEventListener("click", async () => {
    const sinceVal = sinceInput.value.trim();
    if (!/^\d{4}-\d{2}-\d{2}$/.test(sinceVal)) {
      statusBanner.dataset.state = "failed";
      statusBanner.textContent = "請輸入有效的起始比賽日期（YYYY-MM-DD）。";
      sinceInput.focus();
      return;
    }

    currentLogs = [];
    renderLogs();
    connectStream();

    startBtn.disabled = true;
    statusBanner.dataset.state = "running";
    statusBanner.textContent = "正在送出執行請求...";

    try {
      const resp = await fetch(`${API_BASE}/start`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          since: sinceVal,
          asrMode: FIXED_ASR_MODE,
          model: FIXED_MODEL,
          autoGitPush: autoPushCheckbox.checked,
        }),
      });
      if (!resp.ok) {
        const errBody = (await resp.json().catch(() => ({}))) as { error?: string };
        throw new Error(errBody.error ?? `啟動失敗（HTTP ${resp.status}）`);
      }
    } catch (err) {
      startBtn.disabled = false;
      statusBanner.dataset.state = "failed";
      statusBanner.textContent =
        err instanceof Error ? err.message : "無法連線至本機開發伺服器外掛。";
    }
  });

  cancelTaskBtn.addEventListener("click", async () => {
    cancelTaskBtn.disabled = true;
    statusBanner.textContent = "正在送出終止任務指令...";
    try {
      await fetch(`${API_BASE}/cancel`, { method: "POST" });
    } catch {
      cancelTaskBtn.disabled = false;
    }
  });

  dialog.addEventListener("close", () => {
    dialog.returnValue = "";
    if (currentStatus !== "running") {
      closeStream();
    }
  });

  // 初始化預設步驟與背景狀態同步
  renderSteps(getDefaultSteps());
  void fetch(`${API_BASE}/status`, { cache: "no-store" })
    .then(async (resp) => {
      if (!resp.ok || isDestroyed) return;
      const snap = (await resp.json()) as JobSnapshot;
      currentLogs = Array.isArray(snap.logs) ? snap.logs.slice(-MAX_DISPLAY_LOGS) : [];
      applyJobState(snap);
      renderLogs();
      if (snap.status === "running") {
        connectStream();
      }
    })
    .catch(() => {
      /* 本機端初次啟動時若尚未建立連線則靜默處理 */
    });

  return {
    dialogElement: dialog,
    open: () => {
      connectStream();
      if (!dialog.open) {
        dialog.showModal();
      }
    },
    destroy: () => {
      isDestroyed = true;
      closeStream();
    },
  };
}
