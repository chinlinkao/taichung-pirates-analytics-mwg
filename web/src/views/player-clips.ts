/**
 * Tab 5：球員影片集合視圖 (player-clips.ts)
 * - 複用 createPlayerCombobox（IME 安全 + 嚴格綁定 player_uid）搜尋任意球員
 * - 整理該球員每場出賽的打席、投球、守備 YouTube 秒數連結成可排序響應式表格
 * - 支援事件類型過濾、UTF-8 BOM CSV 匯出與 ?tab=clips&uid=... 分享連結
 * - 嚴格遵循 MWG 1.0 無障礙與響應式設計規範
 */

import {
  createDataTable,
  type DataTableController,
  type TableColumn,
} from "../components/data-table";
import {
  createPlayerCombobox,
  type PlayerComboboxController,
} from "../components/player-combobox";
import { downloadCsv } from "../core/csv";
import { getSelectablePlayers, type DatasetIndex } from "../core/filters";
import type {
  ClipType,
  PiratesDataset,
  PlayerClipItem,
  PlayerClipMatchRecord,
  PlayerClipsDataset,
  PlayerClipsRecord,
  SelectablePlayer,
} from "../types/data";

export interface PlayerClipsViewOptions {
  onSelectPlayer: (playerUid: string) => void;
  onRetryLoad?: () => void;
}

export interface PlayerClipsViewState {
  status: "idle" | "loading" | "ready" | "error";
  clipsDataset: PlayerClipsDataset | null;
  errorMessage: string | null;
}

export interface PlayerClipsViewController {
  element: HTMLElement;
  update: (
    dataset: PiratesDataset,
    teamIds: ReadonlySet<number>,
    selectedPlayerUid: string | null,
    index: DatasetIndex,
    clipsState: PlayerClipsViewState
  ) => void;
}

type ClipFilterMode = "all" | ClipType;

function createKpiCard(labelText: string): {
  card: HTMLElement;
  valueEl: HTMLElement;
  subEl: HTMLElement;
} {
  const card = document.createElement("article");
  card.className = "kpi-card";

  const labelEl = document.createElement("span");
  labelEl.className = "kpi-label";
  labelEl.textContent = labelText;

  const valueEl = document.createElement("strong");
  valueEl.className = "kpi-value";
  valueEl.textContent = "-";

  const subEl = document.createElement("span");
  subEl.className = "kpi-sub";

  card.append(labelEl, valueEl, subEl);
  return { card, valueEl, subEl };
}

function createPlayIconSvg(): SVGSVGElement {
  const svgNs = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(svgNs, "svg");
  svg.setAttribute("class", "ext-link-icon");
  svg.setAttribute("viewBox", "0 0 16 16");
  svg.setAttribute("width", "12");
  svg.setAttribute("height", "12");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");

  const path = document.createElementNS(svgNs, "path");
  path.setAttribute("fill", "currentColor");
  path.setAttribute("d", "M4.25 3.2v9.6L12.5 8 4.25 3.2Z");
  svg.append(path);
  return svg;
}

function formatConfidenceTitle(clip: PlayerClipItem): string {
  const methodLabel =
    clip.align_method === "dual_track"
      ? "雙軌定位（Scorebug + 語音逐字稿精修）"
      : clip.align_method === "manual"
        ? "人工微調校準"
        : clip.confidence === "high"
          ? "Scorebug 錨點精準鎖定"
          : clip.confidence === "medium"
            ? "Scorebug 出局區間推算"
            : "記分板缺漏區間推估（建議前後略微拖曳）";
  return `${clip.event_detail}\n開始秒數：${clip.time_display}（${methodLabel}）`;
}

function renderClipListCell(
  clips: readonly PlayerClipItem[],
  clipType: ClipType
): Node {
  if (clips.length === 0) {
    return document.createTextNode("—");
  }

  const wrap = document.createElement("div");
  wrap.className = "clip-list";

  for (const clip of clips) {
    const a = document.createElement("a");
    a.className = `clip-pill clip-pill--${clipType}`;
    if (clip.confidence === "low") {
      a.classList.add("clip-pill--low");
    } else if (clip.align_method === "dual_track") {
      a.classList.add("clip-pill--dual");
    }
    a.href = clip.youtube_url;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    a.title = formatConfidenceTitle(clip);
    a.setAttribute(
      "aria-label",
      `${clip.half_label} ${clip.label_short}，從 ${clip.time_display} 開始播放（開啟新分頁）`
    );

    const textSpan = document.createElement("span");
    const dualMark = clip.align_method === "dual_track" ? " ✦" : "";
    textSpan.textContent = `${clip.half_label} ${clip.label_short} (${clip.time_display})${dualMark}`;

    a.append(createPlayIconSvg(), textSpan);
    wrap.append(a);
  }
  return wrap;
}

function renderMatchInfoCell(row: PlayerClipMatchRecord): Node {
  const wrap = document.createElement("div");
  wrap.className = "clip-match-cell";

  const scoreText =
    row.away_score !== null && row.home_score !== null
      ? `${row.away_score}:${row.home_score}`
      : "-";
  const titleEl = document.createElement("div");
  titleEl.className = "clip-match-title";
  titleEl.textContent = `${row.away_team} ${scoreText} ${row.home_team}`;

  const subRow = document.createElement("div");
  subRow.className = "clip-match-sub";
  const leagueSpan = document.createElement("span");
  leagueSpan.textContent = row.league_title;
  subRow.append(leagueSpan);

  if (row.pbp_url) {
    const pbpLink = document.createElement("a");
    pbpLink.className = "ext-link clip-pbp-link";
    pbpLink.href = row.pbp_url;
    pbpLink.target = "_blank";
    pbpLink.rel = "noopener noreferrer";
    pbpLink.textContent = "文字紀錄 ↗";
    subRow.append(pbpLink);
  }

  wrap.append(titleEl, subRow);
  return wrap;
}

function buildColumns(filterMode: ClipFilterMode): readonly TableColumn<PlayerClipMatchRecord>[] {
  const cols: TableColumn<PlayerClipMatchRecord>[] = [
    {
      key: "matchDate",
      label: "日期",
      type: "string",
      stickyRowHeader: true,
      sortValue: (r) => `${r.match_date_iso ?? ""} ${r.match_time ?? ""}`,
      renderCell: (r) =>
        r.match_time ? `${r.match_date_iso ?? "-"} ${r.match_time}` : (r.match_date_iso ?? "-"),
    },
    {
      key: "matchup",
      label: "賽事與對戰比數",
      type: "string",
      sortValue: (r) => `${r.away_team} ${r.home_team}`,
      renderCell: (r) => renderMatchInfoCell(r),
    },
    {
      key: "position",
      label: "守位",
      type: "string",
      sortValue: (r) => r.starting_position ?? "",
      renderCell: (r) => r.starting_position ?? (r.pitching_clips.length > 0 ? "投(後援)" : "-"),
    },
  ];

  if (filterMode === "all" || filterMode === "batting") {
    cols.push({
      key: "battingClips",
      label: "每打席影片連結",
      type: "number",
      sortValue: (r) => r.batting_clips.length,
      renderCell: (r) => renderClipListCell(r.batting_clips, "batting"),
    });
  }
  if (filterMode === "all" || filterMode === "pitching") {
    cols.push({
      key: "pitchingClips",
      label: "投球影片連結",
      type: "number",
      sortValue: (r) => r.pitching_clips.length,
      renderCell: (r) => renderClipListCell(r.pitching_clips, "pitching"),
    });
  }
  if (filterMode === "all" || filterMode === "fielding") {
    cols.push({
      key: "fieldingClips",
      label: "參與守備影片連結",
      type: "number",
      sortValue: (r) => r.fielding_clips.length,
      renderCell: (r) => renderClipListCell(r.fielding_clips, "fielding"),
    });
  }

  return cols;
}

export function createPlayerClipsView(
  options: PlayerClipsViewOptions
): PlayerClipsViewController {
  const section = document.createElement("section");
  section.className = "section-stack";
  section.setAttribute("aria-label", "球員影片集合");

  // 1. 頂部搜尋列與動作按鈕
  const topControls = document.createElement("div");
  topControls.className = "toolbar-row";

  const comboboxHost = document.createElement("div");
  comboboxHost.style.flex = "1 1 20rem";
  let combobox: PlayerComboboxController | null = null;

  const actionGroup = document.createElement("div");
  actionGroup.className = "toolbar-group";

  const exportBtn = document.createElement("button");
  exportBtn.type = "button";
  exportBtn.className = "btn";
  exportBtn.textContent = "📥 匯出 CSV";

  const copyBtn = document.createElement("button");
  copyBtn.type = "button";
  copyBtn.className = "btn btn-primary";
  copyBtn.textContent = "🔗 複製分享連結";

  const copyStatus = document.createElement("span");
  copyStatus.className = "brand-subtitle";
  copyStatus.setAttribute("role", "status");

  let copyTimer: number | null = null;
  copyBtn.addEventListener("click", async () => {
    const shareUrl = window.location.href;
    let copied = false;
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(shareUrl);
        copied = true;
      }
    } catch {
      copied = false;
    }

    copyStatus.textContent = copied
      ? "已複製球員影片集合網址！"
      : "請直接複製瀏覽器網址列分享";

    if (copyTimer !== null) {
      window.clearTimeout(copyTimer);
    }
    copyTimer = window.setTimeout(() => {
      copyStatus.textContent = "";
    }, 2600);
  });

  actionGroup.append(exportBtn, copyBtn, copyStatus);
  topControls.append(comboboxHost, actionGroup);

  // 2. 載入中／錯誤橫幅
  const statusBanner = document.createElement("div");
  statusBanner.className = "status-banner section-stack";
  statusBanner.hidden = true;

  // 3. 球員摘要與事件過濾列
  const profileBanner = document.createElement("article");
  profileBanner.className = "profile-banner";
  profileBanner.hidden = true;

  const profileHeaderRow = document.createElement("div");
  profileHeaderRow.className = "profile-header-row";
  const profileNameEl = document.createElement("h2");
  profileNameEl.className = "profile-name";
  const profileUidEl = document.createElement("span");
  profileUidEl.className = "profile-uid";
  profileHeaderRow.append(profileNameEl, profileUidEl);

  const legendEl = document.createElement("p");
  legendEl.className = "brand-subtitle";
  legendEl.textContent =
    "說明：✦ 表示已透過本機語音逐字稿雙軌精修；虛線框表示記分板該半局缺漏採區間推估。點擊任一膠囊將直接跳轉至對應 YouTube 開始秒數。";

  const filterRow = document.createElement("div");
  filterRow.className = "clip-filter-group";
  filterRow.setAttribute("role", "group");
  filterRow.setAttribute("aria-label", "影片片段類型篩選");

  let filterMode: ClipFilterMode = "all";
  const filterButtons = new Map<ClipFilterMode, HTMLButtonElement>();
  const filterDefs: ReadonlyArray<{ mode: ClipFilterMode; label: string }> = [
    { mode: "all", label: "全部片段" },
    { mode: "batting", label: "僅打席" },
    { mode: "pitching", label: "僅投球" },
    { mode: "fielding", label: "僅守備" },
  ];

  // 4. 摘要 KPI 卡片
  const kpiGrid = document.createElement("div");
  kpiGrid.className = "kpi-grid";
  kpiGrid.hidden = true;

  const kpiMatches = createKpiCard("收錄出賽場次");
  const kpiBatting = createKpiCard("打席影片數");
  const kpiPitching = createKpiCard("投球半局數");
  const kpiFielding = createKpiCard("守備與雙軌精修");
  kpiGrid.append(kpiMatches.card, kpiBatting.card, kpiPitching.card, kpiFielding.card);

  // 5. 無片段提示與快速體驗球員列
  const emptyPlayerBanner = document.createElement("div");
  emptyPlayerBanner.className = "status-banner section-stack";
  emptyPlayerBanner.hidden = true;

  // 6. 表格容器
  const tableHost = document.createElement("div");
  let clipsTable: DataTableController<PlayerClipMatchRecord> | null = null;
  let tableMode: ClipFilterMode | null = null;

  let currentPlayerRecord: PlayerClipsRecord | null = null;
  let currentVisibleMatches: PlayerClipMatchRecord[] = [];
  let lastRenderCtx: {
    dataset: PiratesDataset;
    teamIds: ReadonlySet<number>;
    selectedPlayerUid: string | null;
    index: DatasetIndex;
    clipsState: PlayerClipsViewState;
  } | null = null;

  const syncFilterButtons = (): void => {
    for (const [mode, btn] of filterButtons.entries()) {
      const pressed = mode === filterMode;
      btn.setAttribute("aria-pressed", pressed ? "true" : "false");
    }
  };

  for (const def of filterDefs) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "btn clip-filter-btn";
    btn.textContent = def.label;
    btn.setAttribute("aria-pressed", def.mode === filterMode ? "true" : "false");
    btn.addEventListener("click", () => {
      if (filterMode === def.mode) return;
      filterMode = def.mode;
      syncFilterButtons();
      if (lastRenderCtx) {
        renderView(
          lastRenderCtx.dataset,
          lastRenderCtx.teamIds,
          lastRenderCtx.selectedPlayerUid,
          lastRenderCtx.index,
          lastRenderCtx.clipsState
        );
      }
    });
    filterButtons.set(def.mode, btn);
    filterRow.append(btn);
  }

  profileBanner.append(profileHeaderRow, legendEl, filterRow);

  exportBtn.addEventListener("click", () => {
    if (!currentPlayerRecord || currentVisibleMatches.length === 0) return;
    const rows: Array<Array<string | number>> = [];
    for (const m of currentVisibleMatches) {
      const score =
        m.away_score !== null && m.home_score !== null
          ? `${m.away_score}:${m.home_score}`
          : "-";
      const matchup = `${m.away_team} ${score} ${m.home_team}`;
      const pos = m.starting_position ?? (m.pitching_clips.length > 0 ? "投(後援)" : "-");

      const pushClips = (typeLabel: string, list: readonly PlayerClipItem[]) => {
        for (const c of list) {
          rows.push([
            m.match_date_iso ?? "",
            m.match_time ?? "",
            m.league_title,
            m.team_name,
            matchup,
            pos,
            typeLabel,
            c.half_label,
            c.label_short,
            c.event_detail,
            c.start_sec,
            c.time_display,
            c.youtube_url,
            c.align_method,
            c.confidence,
          ]);
        }
      };

      if (filterMode === "all" || filterMode === "batting") {
        pushClips("打席", m.batting_clips);
      }
      if (filterMode === "all" || filterMode === "pitching") {
        pushClips("投球", m.pitching_clips);
      }
      if (filterMode === "all" || filterMode === "fielding") {
        pushClips("守備", m.fielding_clips);
      }
    }

    const safeUid = currentPlayerRecord.player_uid.replace(/[^\w\u4e00-\u9fff-]/g, "_");
    downloadCsv(
      `${safeUid}_影片集合.csv`,
      [
        "比賽日期",
        "時間",
        "聯賽賽事",
        "所屬隊伍",
        "對戰與比數",
        "守位",
        "片段類型",
        "半局",
        "精簡結果",
        "事件描述",
        "開始秒數",
        "時間標記",
        "YouTube連結",
        "定位方法",
        "信心度",
      ],
      rows
    );
  });

  section.append(
    topControls,
    statusBanner,
    profileBanner,
    kpiGrid,
    emptyPlayerBanner,
    tableHost
  );

  const renderView = (
    dataset: PiratesDataset,
    teamIds: ReadonlySet<number>,
    selectedPlayerUid: string | null,
    index: DatasetIndex,
    clipsState: PlayerClipsViewState
  ): void => {
    lastRenderCtx = { dataset, teamIds, selectedPlayerUid, index, clipsState };

    const allSelectable = getSelectablePlayers(dataset, teamIds, index);
    // 若已載入 clipsDataset，標註有影片的球員優先排在下拉清單前方
    const clipsMap = clipsState.clipsDataset?.players ?? {};
    const withClips: SelectablePlayer[] = [];
    const withoutClips: SelectablePlayer[] = [];
    for (const p of allSelectable) {
      if (clipsMap[p.playerUid]) {
        withClips.push(p);
      } else {
        withoutClips.push(p);
      }
    }
    const orderedPlayers: SelectablePlayer[] = [...withClips, ...withoutClips];

    const activeUid =
      selectedPlayerUid && orderedPlayers.some((p) => p.playerUid === selectedPlayerUid)
        ? selectedPlayerUid
        : (orderedPlayers.find((p) => Boolean(clipsMap[p.playerUid]))?.playerUid ??
          orderedPlayers[0]?.playerUid ??
          null);

    if (!combobox) {
      combobox = createPlayerCombobox({
        players: orderedPlayers,
        selectedPlayerUid: activeUid,
        label: "搜尋球員影片集合（已優先排列近期 2026-10+ 有出賽影片選手）",
        onSelect: options.onSelectPlayer,
      });
      comboboxHost.replaceChildren(combobox.element);
    } else {
      combobox.updatePlayers(orderedPlayers, activeUid);
    }

    if (clipsState.status === "loading" || clipsState.status === "idle") {
      statusBanner.hidden = false;
      statusBanner.setAttribute("role", "status");
      const p = document.createElement("p");
      p.textContent = "正在載入球員影片時間軸索引（2026-10 起賽事）...";
      statusBanner.replaceChildren(p);
      profileBanner.hidden = true;
      kpiGrid.hidden = true;
      emptyPlayerBanner.hidden = true;
      tableHost.hidden = true;
      exportBtn.disabled = true;
      return;
    }

    if (clipsState.status === "error" || !clipsState.clipsDataset) {
      statusBanner.hidden = false;
      statusBanner.setAttribute("role", "alert");
      const title = document.createElement("h2");
      title.textContent = "無法載入球員影片集合資料";
      const msg = document.createElement("p");
      msg.textContent = clipsState.errorMessage ?? "讀取靜態資料時發生錯誤。";
      const retryWrap = document.createElement("div");
      if (options.onRetryLoad) {
        const retryBtn = document.createElement("button");
        retryBtn.type = "button";
        retryBtn.className = "btn btn-primary";
        retryBtn.textContent = "重新載入";
        retryBtn.addEventListener("click", () => options.onRetryLoad?.());
        retryWrap.append(retryBtn);
      }
      statusBanner.replaceChildren(title, msg, retryWrap);
      profileBanner.hidden = true;
      kpiGrid.hidden = true;
      emptyPlayerBanner.hidden = true;
      tableHost.hidden = true;
      exportBtn.disabled = true;
      return;
    }

    statusBanner.hidden = true;
    const clipsDataset = clipsState.clipsDataset;
    const playerMeta = orderedPlayers.find((p) => p.playerUid === activeUid) ?? null;
    const playerRecord = activeUid ? (clipsDataset.players[activeUid] ?? null) : null;
    currentPlayerRecord = playerRecord;

    const sinceLabel = clipsDataset.meta.since_date ?? "2026-10-01";
    const untilLabel = clipsDataset.meta.until_date ?? "";

    // 依全域篩選條件 (teamIds) 過濾該球員的比賽
    const matchedGames = playerRecord
      ? playerRecord.matches.filter((m) => teamIds.has(m.team_id))
      : [];

    const visibleGames = matchedGames.filter((m) => {
      if (filterMode === "batting") return m.batting_clips.length > 0;
      if (filterMode === "pitching") return m.pitching_clips.length > 0;
      if (filterMode === "fielding") return m.fielding_clips.length > 0;
      return (
        m.batting_clips.length + m.pitching_clips.length + m.fielding_clips.length > 0
      );
    });
    currentVisibleMatches = visibleGames;

    profileBanner.hidden = false;
    profileNameEl.textContent = playerMeta
      ? `${playerMeta.maskedName}（${playerMeta.birthYm ?? "生日未知"}）`
      : "未選擇球員";
    profileUidEl.textContent = activeUid ?? "";

    if (!playerRecord || matchedGames.length === 0) {
      kpiGrid.hidden = true;
      tableHost.hidden = true;
      exportBtn.disabled = true;
      emptyPlayerBanner.hidden = false;

      const infoP = document.createElement("p");
      infoP.textContent =
        `目前試跑版本收錄範圍為 ${sinceLabel} ~ ${untilLabel}（共 ${clipsDataset.meta.counts.matches} 場含逐打席文字紀錄之賽事），` +
        "所選球員在此條件下尚無出賽影片紀錄。請調整篩選條件或點選下方近期有影片的球員體驗：";

      const quickRow = document.createElement("div");
      quickRow.className = "ext-links";
      const sampleUids = Object.values(clipsDataset.players)
        .sort((a, b) => b.total_matches - a.total_matches)
        .slice(0, 8);
      for (const sp of sampleUids) {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "btn";
        b.textContent = `${sp.masked_name} (${sp.total_matches}場)`;
        b.addEventListener("click", () => options.onSelectPlayer(sp.player_uid));
        quickRow.append(b);
      }

      emptyPlayerBanner.replaceChildren(infoP, quickRow);
      return;
    }

    emptyPlayerBanner.hidden = true;
    kpiGrid.hidden = false;
    tableHost.hidden = false;

    const batCount = matchedGames.reduce((acc, m) => acc + m.batting_clips.length, 0);
    const pitchCount = matchedGames.reduce((acc, m) => acc + m.pitching_clips.length, 0);
    const fieldCount = matchedGames.reduce((acc, m) => acc + m.fielding_clips.length, 0);
    const dualCount = matchedGames.reduce((acc, m) => {
      const all = [...m.batting_clips, ...m.pitching_clips, ...m.fielding_clips];
      return acc + all.filter((c) => c.align_method === "dual_track").length;
    }, 0);

    kpiMatches.valueEl.textContent = `${matchedGames.length} 場`;
    kpiMatches.subEl.textContent = `收錄範圍：${sinceLabel} 起`;

    kpiBatting.valueEl.textContent = `${batCount}`;
    kpiBatting.subEl.textContent = "含每打席開始秒數";

    kpiPitching.valueEl.textContent = `${pitchCount}`;
    kpiPitching.subEl.textContent = "投球登板半局數";

    kpiFielding.valueEl.textContent = `${fieldCount}`;
    kpiFielding.subEl.textContent = `全場 ✦ 雙軌語音精修 ${dualCount} 筆`;

    exportBtn.disabled = visibleGames.length === 0;

    if (!clipsTable || tableMode !== filterMode) {
      tableMode = filterMode;
      clipsTable = createDataTable<PlayerClipMatchRecord>({
        caption: `${playerRecord.masked_name} 出賽影片集合（${sinceLabel} ~ ${untilLabel}）`,
        columns: buildColumns(filterMode),
        rows: visibleGames,
        emptyMessage: "在目前類型篩選下無符合片段，請切換上方片段類型。",
        initialSort: { key: "matchDate", direction: "descending" },
      });
      tableHost.replaceChildren(clipsTable.element);
    } else {
      clipsTable.updateRows(visibleGames);
    }
  };

  return {
    element: section,
    update: renderView,
  };
}
