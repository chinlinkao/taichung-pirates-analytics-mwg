/**
 * Tab 1：分隊戰情總覽視圖 (overview.ts)
 * - 顯示 5 張核心戰情 KPI 卡片
 * - 顯示可排序、支援橫向捲動與首欄固定的各隊戰績明細表
 */

import { createDataTable, type TableColumn } from "../components/data-table";
import { formatRate3 } from "../core/stats";
import type { OverviewSummary, TeamOverviewRow } from "../types/data";

export interface OverviewViewController {
  element: HTMLElement;
  update: (summary: OverviewSummary) => void;
}

interface KpiCardRefs {
  valueEl: HTMLElement;
  subEl: HTMLElement;
}

function createKpiCard(labelText: string): {
  card: HTMLElement;
  refs: KpiCardRefs;
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
  return { card, refs: { valueEl, subEl } };
}

const TEAM_OVERVIEW_COLUMNS: readonly TableColumn<TeamOverviewRow>[] = [
  {
    key: "teamName",
    label: "球隊名稱",
    type: "string",
    stickyRowHeader: true,
    renderCell: (r) => r.teamName,
  },
  {
    key: "year",
    label: "年度",
    type: "number",
    align: "end",
    sortValue: (r) => r.year,
    renderCell: (r) => String(r.year),
  },
  {
    key: "season",
    label: "賽季",
    type: "string",
    renderCell: (r) => r.season,
  },
  {
    key: "ageGroup",
    label: "層級",
    type: "string",
    renderCell: (r) => r.ageGroup,
  },
  {
    key: "division",
    label: "組別",
    type: "string",
    renderCell: (r) => r.division,
  },
  {
    key: "teamGroup",
    label: "分隊群組",
    type: "string",
    renderCell: (r) => r.teamGroup,
  },
  {
    key: "leagueTitle",
    label: "賽事名稱",
    type: "string",
    renderCell: (r) => r.leagueTitle,
  },
  {
    key: "rosterCount",
    label: "登錄人數",
    type: "number",
    align: "end",
    sortValue: (r) => r.rosterCount,
    renderCell: (r) => String(r.rosterCount),
  },
  {
    key: "games",
    label: "出賽",
    type: "number",
    align: "end",
    sortValue: (r) => r.games,
    renderCell: (r) => String(r.games),
  },
  {
    key: "wins",
    label: "勝",
    type: "number",
    align: "end",
    sortValue: (r) => r.wins,
    renderCell: (r) => String(r.wins),
  },
  {
    key: "losses",
    label: "敗",
    type: "number",
    align: "end",
    sortValue: (r) => r.losses,
    renderCell: (r) => String(r.losses),
  },
  {
    key: "ties",
    label: "和",
    type: "number",
    align: "end",
    sortValue: (r) => r.ties,
    renderCell: (r) => String(r.ties),
  },
  {
    key: "winRate",
    label: "勝率",
    type: "number",
    align: "end",
    sortValue: (r) => r.winRate,
    renderCell: (r) => formatRate3(r.winRate),
  },
  {
    key: "runsScored",
    label: "得分",
    type: "number",
    align: "end",
    sortValue: (r) => r.runsScored,
    renderCell: (r) => String(r.runsScored),
  },
  {
    key: "runsAllowed",
    label: "失分",
    type: "number",
    align: "end",
    sortValue: (r) => r.runsAllowed,
    renderCell: (r) => String(r.runsAllowed),
  },
  {
    key: "headCoach",
    label: "總教練",
    type: "string",
    renderCell: (r) => r.headCoach ?? "-",
  },
];

export function createOverviewView(): OverviewViewController {
  const section = document.createElement("section");
  section.className = "section-stack";
  section.setAttribute("aria-label", "分隊戰情總覽");

  const kpiHost = document.createElement("div");
  kpiHost.className = "kpi-container";

  const kpiGrid = document.createElement("div");
  kpiGrid.className = "kpi-grid kpi-grid--five";

  const kpiTeams = createKpiCard("符合參賽隊伍");
  const kpiMatches = createKpiCard("已完賽總場次");
  const kpiRecord = createKpiCard("累計勝敗和");
  const kpiWinRate = createKpiCard("團隊整體勝率");
  const kpiRuns = createKpiCard("總得分 / 失分");

  kpiGrid.append(
    kpiTeams.card,
    kpiMatches.card,
    kpiRecord.card,
    kpiWinRate.card,
    kpiRuns.card
  );
  kpiHost.append(kpiGrid);

  const table = createDataTable<TeamOverviewRow>({
    caption: "各聯賽參賽球隊戰績明細表（點擊表頭可排序）",
    columns: TEAM_OVERVIEW_COLUMNS,
    rows: [],
    emptyMessage: "目前篩選條件下無符合的參賽隊伍資料。",
  });

  section.append(kpiHost, table.element);

  return {
    element: section,
    update: (summary) => {
      kpiTeams.refs.valueEl.textContent = `${summary.teamCount} 隊`;
      kpiTeams.refs.subEl.textContent = "依目前篩選條件統計";

      kpiMatches.refs.valueEl.textContent = `${summary.totalMatches} 場`;
      kpiMatches.refs.subEl.textContent = "含海盜內戰各隊視角";

      kpiRecord.refs.valueEl.textContent = `${summary.wins}W - ${summary.losses}L - ${summary.ties}T`;
      kpiRecord.refs.subEl.textContent = "勝 / 敗 / 和";

      kpiWinRate.refs.valueEl.textContent = formatRate3(summary.winRate);
      kpiWinRate.refs.subEl.textContent =
        summary.totalMatches > 0 ? "依已完賽場次計算" : "尚無完賽場次";

      const diffPrefix = summary.runDiff > 0 ? "+" : "";
      kpiRuns.refs.valueEl.textContent = `${summary.runsScored} : ${summary.runsAllowed}`;
      kpiRuns.refs.subEl.textContent = `得失分差 ${diffPrefix}${summary.runDiff}`;

      table.updateRows(summary.teams);
    },
  };
}
