/**
 * 本機開發伺服器專屬外掛：球員影片片段雙軌定位與 Git 自動同步 (vite-plugin-local-clips.ts)
 * - 透過 apply: "serve" 僅於 vite 開發模式掛載，正式建置 (vite build) 與 vitest 完全隔離
 * - 僅允許來自本機迴環介面 (127.0.0.1 / ::1) 的請求
 * - 依序執行：git pull --rebase → clips_pipeline.py → 實質異動檢查 → 精準 git add/commit/push
 */

import { spawn, type ChildProcess } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import type { IncomingMessage, ServerResponse } from "node:http";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import type { Plugin } from "vite";

const API_PREFIX = "/__local-api/clips-update";
const MAX_LOG_LINES = 800;
const ALLOWED_ASR_MODES = new Set([
  "targeted",
  "low_medium",
  "speech_pa",
  "full",
  "off",
]);
const ALLOWED_MODELS = new Set(["tiny", "base", "small", "medium"]);
const DATE_PATTERN = /^\d{4}-\d{2}-\d{2}$/;

type JobStatus = "idle" | "running" | "succeeded" | "no_changes" | "failed" | "cancelled";
type StepId = "git_pull" | "clips_pipeline" | "check_diff" | "git_push";
type StepStatus = "pending" | "running" | "done" | "error" | "skipped";

interface StepState {
  id: StepId;
  label: string;
  status: StepStatus;
}

interface JobSnapshot {
  status: JobStatus;
  step: StepId | null;
  steps: StepState[];
  startedAt: string | null;
  finishedAt: string | null;
  commandPreview: string;
  summary: string | null;
  commitSha: string | null;
  autoGitPush: boolean;
  logs: string[];
}

interface StartParams {
  since: string;
  asrMode: string;
  model: string;
  autoGitPush: boolean;
}

function createInitialSteps(): StepState[] {
  return [
    { id: "git_pull", label: "同步遠端最新賽事資料 (git pull --rebase)", status: "pending" },
    { id: "clips_pipeline", label: "執行雙軌影片定位管線 (clips_pipeline.py)", status: "pending" },
    { id: "check_diff", label: "檢查 player-clips.json 實質異動", status: "pending" },
    { id: "git_push", label: "提交並推送至 GitHub (git commit & push)", status: "pending" },
  ];
}

function buildCommandPreview(params: StartParams): string {
  return `.venv\\Scripts\\python.exe -u src/clips_pipeline.py --since ${params.since} --asr-mode ${params.asrMode} --model ${params.model}`;
}

function isLocalRequest(req: IncomingMessage): boolean {
  const addr = req.socket.remoteAddress ?? "";
  return addr === "127.0.0.1" || addr === "::1" || addr === "::ffff:127.0.0.1";
}

function sendJson(res: ServerResponse, statusCode: number, payload: unknown): void {
  res.statusCode = statusCode;
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  res.setHeader("Cache-Control", "no-store");
  res.end(JSON.stringify(payload));
}

function readJsonBody(req: IncomingMessage): Promise<Record<string, unknown>> {
  return new Promise((resolvePromise, rejectPromise) => {
    const chunks: Buffer[] = [];
    let totalBytes = 0;

    req.on("data", (chunk: Buffer) => {
      totalBytes += chunk.length;
      if (totalBytes > 16 * 1024) {
        rejectPromise(new Error("請求內容過大"));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });

    req.on("end", () => {
      if (chunks.length === 0) {
        resolvePromise({});
        return;
      }
      try {
        const parsed: unknown = JSON.parse(Buffer.concat(chunks).toString("utf-8"));
        if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
          resolvePromise(parsed as Record<string, unknown>);
        } else {
          rejectPromise(new Error("JSON 必須為物件格式"));
        }
      } catch {
        rejectPromise(new Error("無效的 JSON 請求格式"));
      }
    });

    req.on("error", (err) => {
      rejectPromise(err);
    });
  });
}

function validateStartParams(body: Record<string, unknown>): StartParams {
  const rawSince = body.since ?? "2026-11-01";
  const rawAsrMode = body.asrMode ?? "speech_pa";
  const rawModel = body.model ?? "small";
  const rawAutoGitPush = body.autoGitPush ?? true;

  if (typeof rawSince !== "string" || !DATE_PATTERN.test(rawSince.trim())) {
    throw new Error("無效的 --since 日期格式，須為 YYYY-MM-DD");
  }
  const parsedDate = new Date(`${rawSince.trim()}T00:00:00Z`);
  if (Number.isNaN(parsedDate.getTime())) {
    throw new Error("無效的 --since 日期數值");
  }

  if (typeof rawAsrMode !== "string" || !ALLOWED_ASR_MODES.has(rawAsrMode)) {
    throw new Error("不支援的 --asr-mode 參數");
  }

  if (typeof rawModel !== "string" || !ALLOWED_MODELS.has(rawModel)) {
    throw new Error("不支援的 --model 參數");
  }

  if (typeof rawAutoGitPush !== "boolean") {
    throw new Error("autoGitPush 必須為布林值");
  }

  return {
    since: rawSince.trim(),
    asrMode: rawAsrMode,
    model: rawModel,
    autoGitPush: rawAutoGitPush,
  };
}

export function normalizeClipsJsonText(raw: string): string | null {
  try {
    const parsed = JSON.parse(raw) as {
      meta?: Record<string, unknown>;
      players?: Record<string, { matches?: Array<Record<string, unknown>> }>;
      [key: string]: unknown;
    };
    if (parsed.meta && typeof parsed.meta === "object") {
      const { generated_at: _ignored, ...restMeta } = parsed.meta;
      parsed.meta = restMeta;
    }
    if (parsed.players && typeof parsed.players === "object") {
      for (const player of Object.values(parsed.players)) {
        if (!player || !Array.isArray(player.matches)) continue;
        for (const match of player.matches) {
          for (const clipKey of ["batting_clips", "pitching_clips", "fielding_clips"] as const) {
            const list = match[clipKey];
            if (!Array.isArray(list)) continue;
            match[clipKey] = list.map((item) => {
              if (item && typeof item === "object" && !Array.isArray(item)) {
                const { id: _clipId, ...restClip } = item as Record<string, unknown>;
                return restClip;
              }
              return item;
            });
          }
        }
      }
    }
    return JSON.stringify(parsed);
  } catch {
    return null;
  }
}

function normalizeClipsSnapshot(jsonPath: string): string | null {
  if (!existsSync(jsonPath)) {
    return null;
  }
  return normalizeClipsJsonText(readFileSync(jsonPath, "utf-8"));
}

function hasUnmergedConflict(porcelainOutput: string): boolean {
  const conflictCodes = new Set(["UU", "AA", "DD", "AU", "UA", "DU", "UD"]);
  return porcelainOutput
    .split(/\r?\n/)
    .some((line) => line.length >= 2 && conflictCodes.has(line.slice(0, 2)));
}

export function localClipsUpdatePlugin(): Plugin {
  const pluginDir = fileURLToPath(new URL(".", import.meta.url));
  const repoRoot = resolve(pluginDir, "..");
  const clipsJsonPath = resolve(repoRoot, "web", "public", "data", "player-clips.json");

  const defaultParams: StartParams = {
    since: "2026-11-01",
    asrMode: "speech_pa",
    model: "small",
    autoGitPush: true,
  };

  let state: JobSnapshot = {
    status: "idle",
    step: null,
    steps: createInitialSteps(),
    startedAt: null,
    finishedAt: null,
    commandPreview: buildCommandPreview(defaultParams),
    summary: null,
    commitSha: null,
    autoGitPush: true,
    logs: [],
  };

  let activeChild: ChildProcess | null = null;
  let cancelRequested = false;
  const sseClients = new Set<ServerResponse>();

  const broadcastEvent = (eventName: string, data: unknown): void => {
    const payload = `event: ${eventName}\ndata: ${JSON.stringify(data)}\n\n`;
    for (const client of sseClients) {
      if (!client.writableEnded) {
        client.write(payload);
      }
    }
  };

  const broadcastState = (): void => {
    broadcastEvent("state", {
      status: state.status,
      step: state.step,
      steps: state.steps,
      startedAt: state.startedAt,
      finishedAt: state.finishedAt,
      commandPreview: state.commandPreview,
      summary: state.summary,
      commitSha: state.commitSha,
      autoGitPush: state.autoGitPush,
    });
  };

  const appendLog = (line: string): void => {
    const cleanLine = line.replace(/\r$/, "");
    state.logs.push(cleanLine);
    if (state.logs.length > MAX_LOG_LINES) {
      state.logs.splice(0, state.logs.length - MAX_LOG_LINES);
    }
    broadcastEvent("log", { line: cleanLine });
  };

  const setStepStatus = (stepId: StepId, nextStatus: StepStatus): void => {
    if (nextStatus === "running") {
      state.step = stepId;
    }
    state.steps = state.steps.map((item) =>
      item.id === stepId ? { ...item, status: nextStatus } : item
    );
    broadcastState();
  };

  const resolvePythonExecutable = (): string => {
    const winVenv = resolve(repoRoot, ".venv", "Scripts", "python.exe");
    if (existsSync(winVenv)) {
      return winVenv;
    }
    const unixVenv = resolve(repoRoot, ".venv", "bin", "python");
    if (existsSync(unixVenv)) {
      return unixVenv;
    }
    throw new Error("找不到 .venv Python 虛擬環境執行檔 (.venv\\Scripts\\python.exe)");
  };

  const killChildTree = (child: ChildProcess): void => {
    const pid = child.pid;
    if (!pid) {
      return;
    }
    if (process.platform === "win32") {
      spawn("taskkill", ["/pid", String(pid), "/t", "/f"], {
        windowsHide: true,
        stdio: "ignore",
      });
    } else {
      child.kill("SIGTERM");
    }
  };

  const runCommand = (
    cmd: string,
    args: string[],
    options?: {
      streamLogs?: boolean;
      extraEnv?: Record<string, string>;
    }
  ): Promise<{ code: number; stdout: string; stderr: string }> => {
    return new Promise((resolvePromise, rejectPromise) => {
      if (cancelRequested) {
        rejectPromise(new Error("TASK_CANCELLED"));
        return;
      }

      const child = spawn(cmd, args, {
        cwd: repoRoot,
        shell: false,
        windowsHide: true,
        env: {
          ...process.env,
          ...(options?.extraEnv ?? {}),
        },
      });
      activeChild = child;

      let stdoutAll = "";
      let stderrAll = "";
      let stdoutBuf = "";
      let stderrBuf = "";

      const flushBuffer = (chunkText: string, isStderr: boolean): void => {
        if (!options?.streamLogs) {
          return;
        }
        if (isStderr) {
          stderrBuf += chunkText;
          const parts = stderrBuf.split(/\r?\n/);
          stderrBuf = parts.pop() ?? "";
          for (const line of parts) {
            if (line.length > 0) {
              appendLog(line);
            }
          }
        } else {
          stdoutBuf += chunkText;
          const parts = stdoutBuf.split(/\r?\n/);
          stdoutBuf = parts.pop() ?? "";
          for (const line of parts) {
            if (line.length > 0) {
              appendLog(line);
            }
          }
        }
      };

      child.stdout?.on("data", (buf: Buffer) => {
        const text = buf.toString("utf-8");
        stdoutAll += text;
        flushBuffer(text, false);
      });

      child.stderr?.on("data", (buf: Buffer) => {
        const text = buf.toString("utf-8");
        stderrAll += text;
        flushBuffer(text, true);
      });

      child.on("error", (err) => {
        if (activeChild === child) {
          activeChild = null;
        }
        rejectPromise(err);
      });

      child.on("close", (code) => {
        if (activeChild === child) {
          activeChild = null;
        }
        if (options?.streamLogs) {
          if (stdoutBuf.trim().length > 0) {
            appendLog(stdoutBuf);
          }
          if (stderrBuf.trim().length > 0) {
            appendLog(stderrBuf);
          }
        }
        if (cancelRequested) {
          rejectPromise(new Error("TASK_CANCELLED"));
          return;
        }
        resolvePromise({
          code: code ?? 1,
          stdout: stdoutAll,
          stderr: stderrAll,
        });
      });
    });
  };

  const runJob = async (params: StartParams): Promise<void> => {
    cancelRequested = false;
    state = {
      status: "running",
      step: "git_pull",
      steps: createInitialSteps(),
      startedAt: new Date().toISOString(),
      finishedAt: null,
      commandPreview: buildCommandPreview(params),
      summary: "正在準備執行環境...",
      commitSha: null,
      autoGitPush: params.autoGitPush,
      logs: [],
    };
    broadcastState();

    let cleanTargetFilesBeforeRun = false;

    try {
      // 檢查目標檔案執行前是否處於乾淨且無衝突狀態
      const preStatus = await runCommand("git", [
        "status",
        "--porcelain",
        "--",
        "data/pirates.db",
        "web/public/data/player-clips.json",
      ]);
      if (preStatus.code !== 0) {
        throw new Error(`無法檢查 Git 工作區狀態：${preStatus.stderr.trim()}`);
      }
      if (hasUnmergedConflict(preStatus.stdout)) {
        throw new Error("偵測到未解決的 Git 衝突，請先手動處理後再執行。");
      }
      cleanTargetFilesBeforeRun = preStatus.stdout.trim().length === 0;

      // 步驟 1：先同步遠端 main（避免 pirates.db 二進位衝突）
      if (params.autoGitPush) {
        setStepStatus("git_pull", "running");
        appendLog("[步驟 1/4] 正在執行 git pull --rebase origin main...");
        const pullRes = await runCommand(
          "git",
          ["pull", "--rebase", "origin", "main"],
          { streamLogs: true }
        );
        if (pullRes.code !== 0) {
          setStepStatus("git_pull", "error");
          throw new Error("git pull --rebase origin main 失敗，請檢查本機是否有未提交衝突或網路狀態。");
        }
        setStepStatus("git_pull", "done");
      } else {
        appendLog("[步驟 1/4] 已取消勾選自動推送，略過 git pull --rebase。");
        setStepStatus("git_pull", "skipped");
      }

      // 步驟 2：執行雙軌影片定位管線
      setStepStatus("clips_pipeline", "running");
      const beforeSnapshot = normalizeClipsSnapshot(clipsJsonPath);
      const pythonExe = resolvePythonExecutable();
      const pyArgs = [
        "-u",
        "src/clips_pipeline.py",
        "--since",
        params.since,
        "--asr-mode",
        params.asrMode,
        "--model",
        params.model,
      ];
      appendLog(`[步驟 2/4] 執行：${buildCommandPreview(params)}`);
      const pyRes = await runCommand(pythonExe, pyArgs, {
        streamLogs: true,
        extraEnv: {
          PYTHONUTF8: "1",
          PYTHONIOENCODING: "utf-8",
        },
      });
      if (pyRes.code !== 0) {
        setStepStatus("clips_pipeline", "error");
        throw new Error(`clips_pipeline.py 執行失敗（exit code ${pyRes.code}）`);
      }
      setStepStatus("clips_pipeline", "done");

      // 步驟 3：檢查實質資料異動（排除 meta.generated_at）
      setStepStatus("check_diff", "running");
      appendLog("[步驟 3/4] 正在比對 player-clips.json 實質異動...");
      const afterSnapshot = normalizeClipsSnapshot(clipsJsonPath);
      const clipsChanged =
        afterSnapshot !== null &&
        (beforeSnapshot === null || beforeSnapshot !== afterSnapshot);

      const overridesStatus = await runCommand("git", [
        "status",
        "--porcelain",
        "--",
        "data/player_clip_overrides.csv",
      ]);
      const overridesChanged =
        overridesStatus.code === 0 && overridesStatus.stdout.trim().length > 0;

      if (!clipsChanged && !overridesChanged) {
        appendLog("  - 比對結果：無新增或異動的球員影片片段。");
        if (cleanTargetFilesBeforeRun) {
          await runCommand("git", [
            "checkout",
            "--",
            "data/pirates.db",
            "web/public/data/player-clips.json",
          ]);
          appendLog("  - 已還原僅時間戳變動之檔案，保持工作區乾淨。");
        }
        setStepStatus("check_diff", "done");
        setStepStatus("git_push", "skipped");
        state.status = "no_changes";
        state.step = null;
        state.finishedAt = new Date().toISOString();
        state.summary = "執行完成，無新增或異動的球員影片片段，已略過 Git 提交。";
        broadcastState();
        return;
      }

      appendLog("  - 偵測到實質影片片段異動，準備進行 Git 提交。");
      setStepStatus("check_diff", "done");

      // 步驟 4：精準 stage 指定檔案並 commit & push
      if (!params.autoGitPush) {
        setStepStatus("git_push", "skipped");
        state.status = "succeeded";
        state.step = null;
        state.finishedAt = new Date().toISOString();
        state.summary = "已在本機產出最新影片片段資料（依設定未自動提交推送）。";
        broadcastState();
        return;
      }

      setStepStatus("git_push", "running");
      const filesToStage = [
        "data/pirates.db",
        "web/public/data/player-clips.json",
      ];
      if (existsSync(resolve(repoRoot, "data", "player_clip_overrides.csv"))) {
        filesToStage.push("data/player_clip_overrides.csv");
      }

      appendLog(`[步驟 4/4] 正在暫存檔案：${filesToStage.join(", ")}`);
      const addRes = await runCommand("git", ["add", "--", ...filesToStage], {
        streamLogs: true,
      });
      if (addRes.code !== 0) {
        setStepStatus("git_push", "error");
        throw new Error("git add 失敗");
      }

      const cachedDiffRes = await runCommand("git", ["diff", "--cached", "--quiet"]);
      if (cachedDiffRes.code === 0) {
        appendLog("  - 暫存區無實質差異，略過提交與推送。");
        setStepStatus("git_push", "skipped");
        state.status = "no_changes";
        state.step = null;
        state.finishedAt = new Date().toISOString();
        state.summary = "執行完成，Git 暫存區無實質差異，已略過提交。";
        broadcastState();
        return;
      }

      const commitMsg = "chore(clips): 本機更新球員影片片段定位資料";
      appendLog(`[步驟 4/4] 正在提交：${commitMsg}`);
      const commitRes = await runCommand("git", ["commit", "-m", commitMsg], {
        streamLogs: true,
      });
      if (commitRes.code !== 0) {
        setStepStatus("git_push", "error");
        throw new Error("git commit 失敗");
      }

      const shaRes = await runCommand("git", ["rev-parse", "--short", "HEAD"]);
      const shortSha = shaRes.code === 0 ? shaRes.stdout.trim() : "";

      appendLog("[步驟 4/4] 正在推送至 origin main...");
      const pushRes = await runCommand("git", ["push", "origin", "main"], {
        streamLogs: true,
      });
      if (pushRes.code !== 0) {
        setStepStatus("git_push", "error");
        throw new Error("git push origin main 失敗，請確認本機 Git 驗證與網路連線。");
      }

      setStepStatus("git_push", "done");
      state.status = "succeeded";
      state.step = null;
      state.commitSha = shortSha || null;
      state.finishedAt = new Date().toISOString();
      state.summary = shortSha
        ? `已完成影片片段更新並推送至 GitHub（Commit ${shortSha}），線上看板將自動部署。`
        : "已完成影片片段更新並推送至 GitHub，線上看板將自動部署。";
      appendLog(`[完成] ${state.summary}`);
      broadcastState();
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      if (msg === "TASK_CANCELLED" || cancelRequested) {
        if (state.step) {
          setStepStatus(state.step, "error");
        }
        state.status = "cancelled";
        state.step = null;
        state.finishedAt = new Date().toISOString();
        state.summary = "任務已由使用者手動終止。";
        appendLog("[已終止] 任務已停止執行。");
        broadcastState();
      } else {
        if (state.step) {
          setStepStatus(state.step, "error");
        }
        state.status = "failed";
        state.step = null;
        state.finishedAt = new Date().toISOString();
        state.summary = msg;
        appendLog(`[錯誤] ${msg}`);
        broadcastState();
      }
    }
  };

  return {
    name: "local-clips-update",
    apply(_config, env) {
      return env.command === "serve" && env.mode !== "test" && !process.env.VITEST;
    },
    configureServer(server) {
      const heartbeatTimer = setInterval(() => {
        for (const client of sseClients) {
          if (!client.writableEnded) {
            client.write(": ping\n\n");
          }
        }
      }, 15_000);
      heartbeatTimer.unref();

      server.httpServer?.on("close", () => {
        clearInterval(heartbeatTimer);
        if (activeChild) {
          cancelRequested = true;
          killChildTree(activeChild);
        }
        for (const client of sseClients) {
          if (!client.writableEnded) {
            client.end();
          }
        }
        sseClients.clear();
      });

      server.middlewares.use(API_PREFIX, async (req, res, next) => {
        if (!isLocalRequest(req)) {
          sendJson(res, 403, { error: "僅允許本機迴環位址存取" });
          return;
        }

        const rawUrl = req.url ?? "/";
        const pathOnly = rawUrl.split("?")[0] ?? "/";

        if (req.method === "GET" && pathOnly === "/status") {
          sendJson(res, 200, state);
          return;
        }

        if (req.method === "GET" && pathOnly === "/stream") {
          res.statusCode = 200;
          res.setHeader("Content-Type", "text/event-stream; charset=utf-8");
          res.setHeader("Cache-Control", "no-cache");
          res.setHeader("Connection", "keep-alive");
          res.flushHeaders?.();

          sseClients.add(res);
          res.write(`event: snapshot\ndata: ${JSON.stringify(state)}\n\n`);

          req.on("close", () => {
            sseClients.delete(res);
          });
          return;
        }

        if (req.method === "POST" && pathOnly === "/start") {
          if (state.status === "running") {
            sendJson(res, 409, { error: "已有影片片段更新任務正在執行中" });
            return;
          }
          try {
            const body = await readJsonBody(req);
            const params = validateStartParams(body);
            void runJob(params);
            sendJson(res, 202, { ok: true, commandPreview: buildCommandPreview(params) });
          } catch (err) {
            sendJson(res, 400, {
              error: err instanceof Error ? err.message : "請求參數驗證失敗",
            });
          }
          return;
        }

        if (req.method === "POST" && pathOnly === "/cancel") {
          if (state.status !== "running") {
            sendJson(res, 409, { error: "目前沒有執行中的任務" });
            return;
          }
          cancelRequested = true;
          if (activeChild) {
            killChildTree(activeChild);
          }
          sendJson(res, 202, { ok: true });
          return;
        }

        next();
      });
    },
  };
}
