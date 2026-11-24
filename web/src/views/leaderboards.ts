/**
 * Tab 2：投打排行榜視圖 (leaderboards.ts)
 * - 支援「跨季生涯合併（依 player_uid）」與「單季分隊成績」切換
 * - 內建打者 / 投手子頁籤（ARIA tablist + 鍵盤左右鍵切換）
 * - 支援 Min PA / Min IP 滑桿與 UTF-8 BOM CSV 下載
 * - 投手 IP 排序嚴格綁定整數 outs，絕不以字串排序
 */

import {
  createDataTable,
  type DataTableController,
  type TableColumn,
} from "../components/data-table";
import { downloadCsv } from "../core/csv";
import {
  computeBattingLeaderboard,
  computePitchingLeaderboard,
  type DatasetIndex,
} from "../core/filters";
import { formatRate2, formatRate3 } from "../core/stats";
import type {
  BattingLeaderboardRow,
  EraInnings,
  LeaderboardMode,
  PiratesDataset,
  PitchingLeaderboardRow,
} from "../types/data";

export interface LeaderboardsViewOptions {
  onSelectPlayer: (playerUid: string) => void;
}

export interface LeaderboardsViewController {
  element: HTMLElement;
  update: (
    dataset: PiratesDataset,
    teamIds: ReadonlySet<number>,
    eraInnings: EraInnings,
    index: DatasetIndex
  ) => void;
}

export function createLeaderboardsView(
  options: LeaderboardsViewOptions
): LeaderboardsViewController {
  let mode: LeaderboardMode = "career";
  let subTab: "batting" | "pitching" = "batting";
  let minPa = 5;
  let minIp = 1;

  let latestDataset: PiratesDataset | null = null;
  let latestTeamIds: ReadonlySet<number> = new Set<number>();
  let latestEraInnings: EraInnings = 7;
  let latestIndex: DatasetIndex | null = null;

  let currentBattingRows: BattingLeaderboardRow[] = [];
  let currentPitchingRows: PitchingLeaderboardRow[] = [];

  let battingTable: DataTableController<BattingLeaderboardRow> | null = null;
  let battingTableMode: LeaderboardMode | null = null;

  let pitchingTable: DataTableController<PitchingLeaderboardRow> | null = null;
  let pitchingTableMode: LeaderboardMode | null = null;
  let pitchingTableEra: EraInnings | null = null;

  const section = document.createElement("section");
  section.className = "section-stack";
  section.setAttribute("aria-label", "投打攻守排行榜");

  // 頂部統計視角切換列
  const topToolbar = document.createElement("div");
  topToolbar.className = "toolbar-row";

  const modeGroup = document.createElement("div");
  modeGroup.className = "segmented-group";
  modeGroup.setAttribute("role", "group");
  modeGroup.setAttribute("aria-label", "統計視角切換");

  const careerBtn = document.createElement("button");
  careerBtn.type = "button";
  careerBtn.className = "segmented-btn";
  careerBtn.textContent = "跨季生涯合併";
  careerBtn.setAttribute("aria-pressed", "true");

  const seasonBtn = document.createElement("button");
  seasonBtn.type = "button";
  seasonBtn.className = "segmented-btn";
  seasonBtn.textContent = "單季分隊成績";
  seasonBtn.setAttribute("aria-pressed", "false");

  modeGroup.append(careerBtn, seasonBtn);
  topToolbar.append(modeGroup);

  // 子頁籤列：打者 / 投手
  const subTablist = document.createElement("div");
  subTablist.className = "sub-tablist";
  subTablist.setAttribute("role", "tablist");
  subTablist.setAttribute("aria-label", "打者與投手排行榜切換");

  const batTabBtn = document.createElement("button");
  batTabBtn.type = "button";
  batTabBtn.id = "subtab-batting-btn";
  batTabBtn.className = "sub-tab-btn";
  batTabBtn.setAttribute("role", "tab");
  batTabBtn.setAttribute("aria-selected", "true");
  batTabBtn.tabIndex = 0;
  batTabBtn.textContent = "⚾ 打者排行榜";

  const pitchTabBtn = document.createElement("button");
  pitchTabBtn.type = "button";
  pitchTabBtn.id = "subtab-pitching-btn";
  pitchTabBtn.className = "sub-tab-btn";
  pitchTabBtn.setAttribute("role", "tab");
  pitchTabBtn.setAttribute("aria-selected", "false");
  pitchTabBtn.tabIndex = -1;
  pitchTabBtn.textContent = "🎯 投手排行榜";

  subTablist.append(batTabBtn, pitchTabBtn);

  // 打者面板
  const batPanel = document.createElement("div");
  batPanel.className = "section-stack";
  batPanel.setAttribute("role", "tabpanel");
  batPanel.setAttribute("aria-labelledby", batTabBtn.id);

  const batControls = document.createElement("div");
  batControls.className = "toolbar-row";

  const batSliderWrap = document.createElement("label");
  batSliderWrap.className = "slider-field";
  const batSliderTitle = document.createElement("span");
  batSliderTitle.textContent = "最低打席門檻 (Min PA)：";
  const batRange = document.createElement("input");
  batRange.type = "range";
  batRange.min = "0";
  batRange.max = "50";
  batRange.step = "1";
  batRange.value = String(minPa);
  const batSliderVal = document.createElement("span");
  batSliderVal.className = "slider-value";
  batSliderVal.textContent = `${minPa} PA`;
  batSliderWrap.append(batSliderTitle, batRange, batSliderVal);

  const batRight = document.createElement("div");
  batRight.className = "toolbar-group";
  const batCountText = document.createElement("span");
  batCountText.className = "brand-subtitle";
  const batCsvBtn = document.createElement("button");
  batCsvBtn.type = "button";
  batCsvBtn.className = "btn";
  batCsvBtn.textContent = "📥 下載打者 CSV";
  batRight.append(batCountText, batCsvBtn);
  batControls.append(batSliderWrap, batRight);

  const batTableHost = document.createElement("div");
  batPanel.append(batControls, batTableHost);

  // 投手面板
  const pitchPanel = document.createElement("div");
  pitchPanel.className = "section-stack";
  pitchPanel.setAttribute("role", "tabpanel");
  pitchPanel.setAttribute("aria-labelledby", pitchTabBtn.id);
  pitchPanel.hidden = true;

  const pitchControls = document.createElement("div");
  pitchControls.className = "toolbar-row";

  const pitchSliderWrap = document.createElement("label");
  pitchSliderWrap.className = "slider-field";
  const pitchSliderTitle = document.createElement("span");
  pitchSliderTitle.textContent = "最低投球局數門檻 (Min IP)：";
  const pitchRange = document.createElement("input");
  pitchRange.type = "range";
  pitchRange.min = "0";
  pitchRange.max = "20";
  pitchRange.step = "1";
  pitchRange.value = String(minIp);
  const pitchSliderVal = document.createElement("span");
  pitchSliderVal.className = "slider-value";
  pitchSliderVal.textContent = `${minIp}.0 局`;
  pitchSliderWrap.append(pitchSliderTitle, pitchRange, pitchSliderVal);

  const pitchRight = document.createElement("div");
  pitchRight.className = "toolbar-group";
  const pitchCountText = document.createElement("span");
  pitchCountText.className = "brand-subtitle";
  const pitchCsvBtn = document.createElement("button");
  pitchCsvBtn.type = "button";
  pitchCsvBtn.className = "btn";
  pitchCsvBtn.textContent = "📥 下載投手 CSV";
  pitchRight.append(pitchCountText, pitchCsvBtn);
  pitchControls.append(pitchSliderWrap, pitchRight);

  const pitchTableHost = document.createElement("div");
  pitchPanel.append(pitchControls, pitchTableHost);

  section.append(topToolbar, subTablist, batPanel, pitchPanel);

  function createPlayerLinkButton(uid: string, name: string): HTMLButtonElement {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "link-btn";
    btn.textContent = name;
    btn.title = `查看 ${name} 的跨屆成長檔案`;
    btn.addEventListener("click", () => {
      options.onSelectPlayer(uid);
    });
    return btn;
  }

  function buildBattingColumns(currentMode: LeaderboardMode): TableColumn<BattingLeaderboardRow>[] {
    const baseCols: TableColumn<BattingLeaderboardRow>[] = [
      {
        key: "maskedName",
        label: "姓名",
        type: "string",
        stickyRowHeader: true,
        sortValue: (r) => r.maskedName,
        renderCell: (r) => createPlayerLinkButton(r.playerUid, r.maskedName),
      },
      {
        key: "birthYm",
        label: "出生年月",
        type: "string",
        renderCell: (r) => r.birthYm ?? "-",
      },
      {
        key: "jerseyNumber",
        label: "背號",
        type: "string",
        renderCell: (r) => r.jerseyNumber ?? "-",
      },
    ];

    if (currentMode === "career") {
      baseCols.push(
        {
          key: "ageGroups",
          label: "層級",
          type: "string",
          renderCell: (r) => r.ageGroups.join("/") || "-",
        },
        {
          key: "seasonsPlayed",
          label: "出賽隊數",
          type: "number",
          align: "end",
          sortValue: (r) => r.seasonsPlayed,
          renderCell: (r) => String(r.seasonsPlayed),
        }
      );
    } else {
      baseCols.push(
        {
          key: "teamName",
          label: "球隊",
          type: "string",
          renderCell: (r) => r.teamName ?? "-",
        },
        {
          key: "year",
          label: "年度",
          type: "number",
          align: "end",
          sortValue: (r) => r.year,
          renderCell: (r) => (r.year !== null ? String(r.year) : "-"),
        },
        {
          key: "season",
          label: "賽季",
          type: "string",
          renderCell: (r) => r.season ?? "-",
        },
        {
          key: "ageGroups",
          label: "層級",
          type: "string",
          renderCell: (r) => r.ageGroups[0] ?? "-",
        }
      );
    }

    baseCols.push(
      { key: "g", label: "G", type: "number", align: "end", sortValue: (r) => r.g, renderCell: (r) => String(r.g) },
      { key: "pa", label: "PA", type: "number", align: "end", sortValue: (r) => r.pa, renderCell: (r) => String(r.pa) },
      { key: "ab", label: "AB", type: "number", align: "end", sortValue: (r) => r.ab, renderCell: (r) => String(r.ab) },
      { key: "h", label: "H", type: "number", align: "end", sortValue: (r) => r.h, renderCell: (r) => String(r.h) },
      { key: "doubles", label: "2B", type: "number", align: "end", sortValue: (r) => r.doubles, renderCell: (r) => String(r.doubles) },
      { key: "triples", label: "3B", type: "number", align: "end", sortValue: (r) => r.triples, renderCell: (r) => String(r.triples) },
      { key: "hr", label: "HR", type: "number", align: "end", sortValue: (r) => r.hr, renderCell: (r) => String(r.hr) },
      { key: "rbi", label: "打點", type: "number", align: "end", sortValue: (r) => r.rbi, renderCell: (r) => String(r.rbi) },
      { key: "r", label: "得分", type: "number", align: "end", sortValue: (r) => r.r, renderCell: (r) => String(r.r) },
      { key: "bb", label: "四死", type: "number", align: "end", sortValue: (r) => r.bb, renderCell: (r) => String(r.bb) },
      { key: "so", label: "三振", type: "number", align: "end", sortValue: (r) => r.so, renderCell: (r) => String(r.so) },
      { key: "sbTop10", label: "盜壘*", type: "number", align: "end", sortValue: (r) => r.sbTop10, renderCell: (r) => String(r.sbTop10) },
      { key: "avg", label: "AVG", type: "number", align: "end", sortValue: (r) => r.avg, renderCell: (r) => formatRate3(r.avg) },
      { key: "obp", label: "OBP", type: "number", align: "end", sortValue: (r) => r.obp, renderCell: (r) => formatRate3(r.obp) },
      { key: "slg", label: "SLG", type: "number", align: "end", sortValue: (r) => r.slg, renderCell: (r) => formatRate3(r.slg) },
      { key: "ops", label: "OPS", type: "number", align: "end", sortValue: (r) => r.ops, renderCell: (r) => formatRate3(r.ops) }
    );

    return baseCols;
  }

  function buildPitchingColumns(
    currentMode: LeaderboardMode,
    eraInnings: EraInnings
  ): TableColumn<PitchingLeaderboardRow>[] {
    const baseCols: TableColumn<PitchingLeaderboardRow>[] = [
      {
        key: "maskedName",
        label: "姓名",
        type: "string",
        stickyRowHeader: true,
        sortValue: (r) => r.maskedName,
        renderCell: (r) => createPlayerLinkButton(r.playerUid, r.maskedName),
      },
      {
        key: "birthYm",
        label: "出生年月",
        type: "string",
        renderCell: (r) => r.birthYm ?? "-",
      },
      {
        key: "jerseyNumber",
        label: "背號",
        type: "string",
        renderCell: (r) => r.jerseyNumber ?? "-",
      },
    ];

    if (currentMode === "career") {
      baseCols.push(
        {
          key: "ageGroups",
          label: "層級",
          type: "string",
          renderCell: (r) => r.ageGroups.join("/") || "-",
        },
        {
          key: "seasonsPitched",
          label: "登板隊數",
          type: "number",
          align: "end",
          sortValue: (r) => r.seasonsPitched,
          renderCell: (r) => String(r.seasonsPitched),
        }
      );
    } else {
      baseCols.push(
        {
          key: "teamName",
          label: "球隊",
          type: "string",
          renderCell: (r) => r.teamName ?? "-",
        },
        {
          key: "year",
          label: "年度",
          type: "number",
          align: "end",
          sortValue: (r) => r.year,
          renderCell: (r) => (r.year !== null ? String(r.year) : "-"),
        },
        {
          key: "season",
          label: "賽季",
          type: "string",
          renderCell: (r) => r.season ?? "-",
        },
        {
          key: "ageGroups",
          label: "層級",
          type: "string",
          renderCell: (r) => r.ageGroups[0] ?? "-",
        }
      );
    }

    baseCols.push(
      { key: "w", label: "W", type: "number", align: "end", sortValue: (r) => r.w, renderCell: (r) => String(r.w) },
      { key: "l", label: "L", type: "number", align: "end", sortValue: (r) => r.l, renderCell: (r) => String(r.l) },
      { key: "sv", label: "SV", type: "number", align: "end", sortValue: (r) => r.sv, renderCell: (r) => String(r.sv) },
      { key: "g", label: "G", type: "number", align: "end", sortValue: (r) => r.g, renderCell: (r) => String(r.g) },
      // 鐵則：IP 欄位必須依整數 outs 排序，而非字串排序
      { key: "ip", label: "IP", type: "number", align: "end", sortValue: (r) => r.outs, renderCell: (r) => r.ipDisplay },
      { key: "era", label: `ERA (${eraInnings}局)`, type: "number", align: "end", sortValue: (r) => r.era, renderCell: (r) => formatRate2(r.era) },
      { key: "whip", label: "WHIP", type: "number", align: "end", sortValue: (r) => r.whip, renderCell: (r) => formatRate2(r.whip) },
      { key: "k7", label: "K/7", type: "number", align: "end", sortValue: (r) => r.k7, renderCell: (r) => formatRate2(r.k7) },
      { key: "h", label: "被安打", type: "number", align: "end", sortValue: (r) => r.h, renderCell: (r) => String(r.h) },
      { key: "bb", label: "四死球", type: "number", align: "end", sortValue: (r) => r.bb, renderCell: (r) => String(r.bb) },
      { key: "so", label: "三振", type: "number", align: "end", sortValue: (r) => r.so, renderCell: (r) => String(r.so) },
      { key: "r", label: "失分", type: "number", align: "end", sortValue: (r) => r.r, renderCell: (r) => String(r.r) },
      { key: "er", label: "責失", type: "number", align: "end", sortValue: (r) => r.er, renderCell: (r) => String(r.er) }
    );

    return baseCols;
  }

  function refreshTables(): void {
    if (!latestDataset || !latestIndex) {
      return;
    }

    // 1. 打者排行榜
    currentBattingRows = computeBattingLeaderboard(
      latestDataset,
      latestTeamIds,
      mode,
      minPa,
      latestIndex
    );
    batCountText.textContent = `符合條件：${currentBattingRows.length} 筆（點擊姓名查看成長曲線）`;

    if (!battingTable || battingTableMode !== mode) {
      battingTableMode = mode;
      battingTable = createDataTable<BattingLeaderboardRow>({
        caption:
          mode === "career"
            ? "打者跨季生涯合併排行榜（依 player_uid 歸戶）"
            : "打者單季分隊排行榜",
        columns: buildBattingColumns(mode),
        rows: currentBattingRows,
        emptyMessage: "目前篩選與最低打席條件下無符合的打者紀錄。",
      });
      batTableHost.replaceChildren(battingTable.element);
    } else {
      battingTable.updateRows(currentBattingRows);
    }

    // 2. 投手排行榜（嚴格排除 coach_pitch_888 且保留 0 局責失累加）
    currentPitchingRows = computePitchingLeaderboard(
      latestDataset,
      latestTeamIds,
      mode,
      minIp * 3,
      latestEraInnings,
      latestIndex
    );
    pitchCountText.textContent = `符合條件：${currentPitchingRows.length} 筆（已排除教練拋打）`;

    if (
      !pitchingTable ||
      pitchingTableMode !== mode ||
      pitchingTableEra !== latestEraInnings
    ) {
      pitchingTableMode = mode;
      pitchingTableEra = latestEraInnings;
      pitchingTable = createDataTable<PitchingLeaderboardRow>({
        caption:
          mode === "career"
            ? `投手跨季生涯合併排行榜（${latestEraInnings} 局制 ERA，依整數出局數累積）`
            : `投手單季分隊排行榜（${latestEraInnings} 局制 ERA）`,
        columns: buildPitchingColumns(mode, latestEraInnings),
        rows: currentPitchingRows,
        emptyMessage: "目前篩選與最低局數條件下無符合的投手紀錄。",
      });
      pitchTableHost.replaceChildren(pitchingTable.element);
    } else {
      pitchingTable.updateRows(currentPitchingRows);
    }
  }

  const setMode = (nextMode: LeaderboardMode): void => {
    if (nextMode === mode) return;
    mode = nextMode;
    careerBtn.setAttribute("aria-pressed", mode === "career" ? "true" : "false");
    seasonBtn.setAttribute("aria-pressed", mode === "season" ? "true" : "false");
    refreshTables();
  };

  careerBtn.addEventListener("click", () => setMode("career"));
  seasonBtn.addEventListener("click", () => setMode("season"));

  const setSubTab = (next: "batting" | "pitching", focusTab = false): void => {
    subTab = next;
    const isBat = next === "batting";
    batTabBtn.setAttribute("aria-selected", isBat ? "true" : "false");
    batTabBtn.tabIndex = isBat ? 0 : -1;
    pitchTabBtn.setAttribute("aria-selected", !isBat ? "true" : "false");
    pitchTabBtn.tabIndex = !isBat ? 0 : -1;
    batPanel.hidden = !isBat;
    pitchPanel.hidden = isBat;
    if (focusTab) {
      (isBat ? batTabBtn : pitchTabBtn).focus();
    }
  };

  batTabBtn.addEventListener("click", () => setSubTab("batting"));
  pitchTabBtn.addEventListener("click", () => setSubTab("pitching"));

  const handleSubTabKey = (event: KeyboardEvent): void => {
    if (event.key === "ArrowRight" || event.key === "ArrowLeft") {
      event.preventDefault();
      setSubTab(subTab === "batting" ? "pitching" : "batting", true);
    }
  };
  batTabBtn.addEventListener("keydown", handleSubTabKey);
  pitchTabBtn.addEventListener("keydown", handleSubTabKey);

  batRange.addEventListener("input", () => {
    minPa = Number(batRange.value);
    batSliderVal.textContent = `${minPa} PA`;
    refreshTables();
  });

  pitchRange.addEventListener("input", () => {
    minIp = Number(pitchRange.value);
    pitchSliderVal.textContent = `${minIp}.0 局`;
    refreshTables();
  });

  batCsvBtn.addEventListener("click", () => {
    const headers = [
      "player_uid",
      "姓名",
      "出生年月",
      "背號",
      ...(mode === "career" ? ["層級", "出賽隊數"] : ["球隊", "年度", "賽季", "層級"]),
      "G",
      "PA",
      "AB",
      "H",
      "2B",
      "3B",
      "HR",
      "打點",
      "得分",
      "四死",
      "三振",
      "盜壘(前十註記)",
      "AVG",
      "OBP",
      "SLG",
      "OPS",
    ];
    const rows = currentBattingRows.map((r) => [
      r.playerUid,
      r.maskedName,
      r.birthYm ?? "",
      r.jerseyNumber ?? "",
      ...(mode === "career"
        ? [r.ageGroups.join("/"), r.seasonsPlayed]
        : [r.teamName ?? "", r.year ?? "", r.season ?? "", r.ageGroups[0] ?? ""]),
      r.g,
      r.pa,
      r.ab,
      r.h,
      r.doubles,
      r.triples,
      r.hr,
      r.rbi,
      r.r,
      r.bb,
      r.so,
      r.sbTop10,
      formatRate3(r.avg),
      formatRate3(r.obp),
      formatRate3(r.slg),
      formatRate3(r.ops),
    ]);
    downloadCsv(`pirates_batting_${mode}.csv`, headers, rows);
  });

  pitchCsvBtn.addEventListener("click", () => {
    const headers = [
      "player_uid",
      "姓名",
      "出生年月",
      "背號",
      ...(mode === "career" ? ["層級", "登板隊數"] : ["球隊", "年度", "賽季", "層級"]),
      "W",
      "L",
      "SV",
      "G",
      "IP",
      "總出局數(outs)",
      `ERA(${latestEraInnings}局)`,
      "WHIP",
      "K/7",
      "被安打",
      "四死球",
      "三振",
      "失分",
      "責失",
    ];
    const rows = currentPitchingRows.map((r) => [
      r.playerUid,
      r.maskedName,
      r.birthYm ?? "",
      r.jerseyNumber ?? "",
      ...(mode === "career"
        ? [r.ageGroups.join("/"), r.seasonsPitched]
        : [r.teamName ?? "", r.year ?? "", r.season ?? "", r.ageGroups[0] ?? ""]),
      r.w,
      r.l,
      r.sv,
      r.g,
      r.ipDisplay,
      r.outs,
      formatRate2(r.era),
      formatRate2(r.whip),
      formatRate2(r.k7),
      r.h,
      r.bb,
      r.so,
      r.r,
      r.er,
    ]);
    downloadCsv(`pirates_pitching_${mode}.csv`, headers, rows);
  });

  return {
    element: section,
    update: (dataset, teamIds, eraInnings, index) => {
      latestDataset = dataset;
      latestTeamIds = teamIds;
      latestEraInnings = eraInnings;
      latestIndex = index;
      refreshTables();
    },
  };
}
