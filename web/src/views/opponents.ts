/**
 * Tab 4：賽程戰果與對手分析視圖 (opponents.ts)
 * - 原生下拉選單選擇特定對手或檢視全部對戰（含海盜內戰分隊）
 * - 4 張對戰統計 KPI 卡片
 * - 可排序對戰明細表含安全外部三連結（攻守數據 / 文字紀錄 / 戰況表）
 */

import { createDataTable, type TableColumn } from "../components/data-table";
import { formatRate3 } from "../core/stats";
import type { MatchViewRow, OpponentSummary } from "../types/data";

export interface OpponentsViewOptions {
  onSelectOpponent: (opponent: string | null) => void;
}

export interface OpponentsViewController {
  element: HTMLElement;
  update: (summary: OpponentSummary) => void;
}

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

function createExternalLinksCell(row: MatchViewRow): Node {
  const links: Array<{ label: string; url: string }> = [];
  if (row.boxscoreUrl) {
    links.push({ label: "攻守數據", url: row.boxscoreUrl });
  }
  if (row.pbpUrl) {
    links.push({ label: "文字紀錄", url: row.pbpUrl });
  }
  if (row.summaryUrl) {
    links.push({ label: "戰況表", url: row.summaryUrl });
  }

  if (links.length === 0) {
    return document.createTextNode("-");
  }

  const wrap = document.createElement("div");
  wrap.className = "ext-links";

  for (const item of links) {
    const a = document.createElement("a");
    a.className = "ext-link";
    a.href = item.url;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    a.textContent = `${item.label} ↗`;
    wrap.append(a);
  }
  return wrap;
}

function createOutcomeCell(row: MatchViewRow): Node {
  if (!row.outcome) {
    return document.createTextNode("未開賽");
  }
  const labelMap = { W: "勝", L: "敗", T: "和" } as const;
  const span = document.createElement("span");
  span.className = "outcome-badge";
  span.dataset.outcome = row.outcome;
  span.textContent = labelMap[row.outcome];
  return span;
}

const MATCH_COLUMNS: readonly TableColumn<MatchViewRow>[] = [
  {
    key: "matchDate",
    label: "日期",
    type: "string",
    stickyRowHeader: true,
    sortValue: (r) => r.matchDateIso ?? r.matchDate ?? "",
    renderCell: (r) => r.matchDateIso ?? r.matchDate ?? "-",
  },
  {
    key: "matchTime",
    label: "時間",
    type: "string",
    renderCell: (r) => r.matchTime ?? "-",
  },
  {
    key: "teamName",
    label: "我方球隊",
    type: "string",
    renderCell: (r) => r.teamName,
  },
  {
    key: "opponentTeam",
    label: "對手球隊",
    type: "string",
    renderCell: (r) => r.opponentTeam ?? "-",
  },
  {
    key: "pirateSide",
    label: "主/客",
    type: "string",
    renderCell: (r) =>
      r.pirateSide === "away" ? "客場" : r.pirateSide === "home" ? "主場" : "-",
  },
  {
    key: "score",
    label: "比分 (我:敵)",
    type: "number",
    align: "end",
    sortValue: (r) =>
      r.myScore !== null && r.oppScore !== null
        ? r.myScore - r.oppScore
        : null,
    renderCell: (r) =>
      r.myScore !== null && r.oppScore !== null
        ? `${r.myScore} : ${r.oppScore}`
        : "未開賽",
  },
  {
    key: "outcome",
    label: "勝負",
    type: "string",
    sortValue: (r) => r.outcome ?? "",
    renderCell: (r) => createOutcomeCell(r),
  },
  {
    key: "ageGroup",
    label: "層級",
    type: "string",
    renderCell: (r) => r.ageGroup,
  },
  {
    key: "leagueTitle",
    label: "賽事名稱",
    type: "string",
    renderCell: (r) => r.leagueTitle,
  },
  {
    key: "venue",
    label: "場地",
    type: "string",
    renderCell: (r) => r.venue ?? "-",
  },
  {
    key: "links",
    label: "官網戰況連結",
    type: "string",
    sortable: false,
    renderCell: (r) => createExternalLinksCell(r),
  },
];

export function createOpponentsView(
  options: OpponentsViewOptions
): OpponentsViewController {
  const section = document.createElement("section");
  section.className = "section-stack";
  section.setAttribute("aria-label", "賽程戰果與對手分析");

  // 1. 對手下拉選單
  const controlsRow = document.createElement("div");
  controlsRow.className = "toolbar-row";

  const selectWrap = document.createElement("div");
  selectWrap.className = "select-field";

  const selectLabel = document.createElement("label");
  selectLabel.className = "select-label";
  selectLabel.htmlFor = "opponent-select";
  selectLabel.textContent = "篩選對戰對手（含海盜內戰分隊）";

  const selectEl = document.createElement("select");
  selectEl.id = "opponent-select";
  selectEl.className = "select-input";
  selectEl.addEventListener("change", () => {
    const val = selectEl.value;
    options.onSelectOpponent(val === "" ? null : val);
  });

  selectWrap.append(selectLabel, selectEl);
  controlsRow.append(selectWrap);

  // 2. 4 張對戰統計 KPI 卡片
  const kpiHost = document.createElement("div");
  kpiHost.className = "kpi-container";

  const kpiGrid = document.createElement("div");
  kpiGrid.className = "kpi-grid kpi-grid--four";

  const kpiTotal = createKpiCard("對戰總場次");
  const kpiRecord = createKpiCard("對戰勝敗和");
  const kpiWinRate = createKpiCard("對戰勝率");
  const kpiRuns = createKpiCard("得分 / 失分");

  kpiGrid.append(kpiTotal.card, kpiRecord.card, kpiWinRate.card, kpiRuns.card);
  kpiHost.append(kpiGrid);

  // 3. 對戰賽程明細表
  const table = createDataTable<MatchViewRow>({
    caption: "賽程戰果與外部戰況連結明細表",
    columns: MATCH_COLUMNS,
    rows: [],
    emptyMessage: "目前篩選條件下無符合的賽程紀錄。",
  });

  section.append(controlsRow, kpiHost, table.element);

  return {
    element: section,
    update: (summary) => {
      // 重建對手下拉選項（確保反映目前篩選條件內的對手列表）
      const fragment = document.createDocumentFragment();
      const allOpt = document.createElement("option");
      allOpt.value = "";
      allOpt.textContent = `全部對手（共 ${summary.availableOpponents.length} 支球隊）`;
      fragment.append(allOpt);

      for (const opp of summary.availableOpponents) {
        const opt = document.createElement("option");
        opt.value = opp;
        opt.textContent = opp;
        fragment.append(opt);
      }

      if (
        summary.opponentName &&
        !summary.availableOpponents.includes(summary.opponentName)
      ) {
        const customOpt = document.createElement("option");
        customOpt.value = summary.opponentName;
        customOpt.textContent = `${summary.opponentName}（目前條件無賽程）`;
        fragment.append(customOpt);
      }

      selectEl.replaceChildren(fragment);
      selectEl.value = summary.opponentName ?? "";

      kpiTotal.valueEl.textContent = `${summary.totalMatches} 場`;
      kpiTotal.subEl.textContent = `已完賽 ${summary.completedMatches} 場`;

      kpiRecord.valueEl.textContent = `${summary.wins}W - ${summary.losses}L - ${summary.ties}T`;
      kpiRecord.subEl.textContent = summary.opponentName ?? "全部對手合計";

      kpiWinRate.valueEl.textContent = formatRate3(summary.winRate);
      kpiWinRate.subEl.textContent =
        summary.completedMatches > 0 ? "依已完賽場次計算" : "尚無完賽紀錄";

      const diff = summary.runsScored - summary.runsAllowed;
      const diffPrefix = diff > 0 ? "+" : "";
      kpiRuns.valueEl.textContent = `${summary.runsScored} : ${summary.runsAllowed}`;
      kpiRuns.subEl.textContent = `淨分 ${diffPrefix}${diff}`;

      table.updateRows(summary.matches);
    },
  };
}
