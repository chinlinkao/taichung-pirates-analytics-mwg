/**
 * Tab 3：球員跨屆成長檔案視圖 (player-profile.ts)
 * - 球員搜尋選單（IME 安全 + 嚴格綁定 player_uid）
 * - 一鍵複製分享連結（連動 ?tab=player&uid=...&era=...）
 * - 個人履歷摘要卡、歷屆打擊／投球趨勢雙折線圖、歷屆明細表
 */

import {
  createTrendChart,
  type TrendChartController,
} from "../components/charts";
import {
  createDataTable,
  type DataTableController,
  type TableColumn,
} from "../components/data-table";
import {
  createPlayerCombobox,
  type PlayerComboboxController,
} from "../components/player-combobox";
import {
  getPlayerHistory,
  getSelectablePlayers,
  type DatasetIndex,
  type PlayerSeasonBattingRow,
  type PlayerSeasonPitchingRow,
} from "../core/filters";
import {
  calcAvg,
  calcEra,
  calcK7,
  calcObp,
  calcOps,
  calcSlg,
  calcWhip,
  formatInnings,
  formatRate2,
  formatRate3,
} from "../core/stats";
import type { EraInnings, PiratesDataset } from "../types/data";

export interface PlayerProfileViewOptions {
  onSelectPlayer: (playerUid: string) => void;
  onOpenPlayerClips?: (playerUid: string) => void;
}

export interface PlayerProfileViewController {
  element: HTMLElement;
  update: (
    dataset: PiratesDataset,
    teamIds: ReadonlySet<number>,
    selectedPlayerUid: string | null,
    eraInnings: EraInnings,
    index: DatasetIndex
  ) => void;
  refreshTheme: () => void;
}

const BATTING_SEASON_COLUMNS: readonly TableColumn<PlayerSeasonBattingRow>[] = [
  { key: "teamName", label: "球隊", type: "string", stickyRowHeader: true, renderCell: (r) => r.teamName },
  { key: "year", label: "年度", type: "number", align: "end", sortValue: (r) => r.year, renderCell: (r) => String(r.year) },
  { key: "season", label: "賽季", type: "string", renderCell: (r) => r.season },
  { key: "ageGroup", label: "層級", type: "string", renderCell: (r) => r.ageGroup },
  { key: "jerseyNumber", label: "背號", type: "string", renderCell: (r) => r.jersey_number ?? "-" },
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
  { key: "sb", label: "盜壘*", type: "number", align: "end", sortValue: (r) => r.sb_top10 ?? 0, renderCell: (r) => String(r.sb_top10 ?? 0) },
  { key: "avg", label: "AVG", type: "number", align: "end", sortValue: (r) => r.avg, renderCell: (r) => formatRate3(r.avg) },
  { key: "obp", label: "OBP", type: "number", align: "end", sortValue: (r) => r.obp, renderCell: (r) => formatRate3(r.obp) },
  { key: "slg", label: "SLG", type: "number", align: "end", sortValue: (r) => r.slg, renderCell: (r) => formatRate3(r.slg) },
  { key: "ops", label: "OPS", type: "number", align: "end", sortValue: (r) => r.ops, renderCell: (r) => formatRate3(r.ops) },
];

function buildPitchingSeasonColumns(
  eraInnings: EraInnings
): TableColumn<PlayerSeasonPitchingRow>[] {
  return [
    { key: "teamName", label: "球隊", type: "string", stickyRowHeader: true, renderCell: (r) => r.teamName },
    { key: "year", label: "年度", type: "number", align: "end", sortValue: (r) => r.year, renderCell: (r) => String(r.year) },
    { key: "season", label: "賽季", type: "string", renderCell: (r) => r.season },
    { key: "ageGroup", label: "層級", type: "string", renderCell: (r) => r.ageGroup },
    { key: "jerseyNumber", label: "背號", type: "string", renderCell: (r) => r.jersey_number ?? "-" },
    { key: "w", label: "W", type: "number", align: "end", sortValue: (r) => r.w, renderCell: (r) => String(r.w) },
    { key: "l", label: "L", type: "number", align: "end", sortValue: (r) => r.l, renderCell: (r) => String(r.l) },
    { key: "sv", label: "SV", type: "number", align: "end", sortValue: (r) => r.sv, renderCell: (r) => String(r.sv) },
    { key: "g", label: "G", type: "number", align: "end", sortValue: (r) => r.g, renderCell: (r) => String(r.g) },
    { key: "ip", label: "IP", type: "number", align: "end", sortValue: (r) => r.outs, renderCell: (r) => r.ipDisplay },
    { key: "era", label: `ERA (${eraInnings}局)`, type: "number", align: "end", sortValue: (r) => r.era, renderCell: (r) => formatRate2(r.era) },
    { key: "whip", label: "WHIP", type: "number", align: "end", sortValue: (r) => r.whip, renderCell: (r) => formatRate2(r.whip) },
    { key: "k7", label: "K/7", type: "number", align: "end", sortValue: (r) => r.k7, renderCell: (r) => formatRate2(r.k7) },
    { key: "h", label: "被安打", type: "number", align: "end", sortValue: (r) => r.h, renderCell: (r) => String(r.h) },
    { key: "bb", label: "四死球", type: "number", align: "end", sortValue: (r) => r.bb, renderCell: (r) => String(r.bb) },
    { key: "so", label: "三振", type: "number", align: "end", sortValue: (r) => r.so, renderCell: (r) => String(r.so) },
    { key: "r", label: "失分", type: "number", align: "end", sortValue: (r) => r.r, renderCell: (r) => String(r.r) },
    { key: "er", label: "責失", type: "number", align: "end", sortValue: (r) => r.er, renderCell: (r) => String(r.er) },
  ];
}

export function createPlayerProfileView(
  options: PlayerProfileViewOptions
): PlayerProfileViewController {
  const section = document.createElement("section");
  section.className = "section-stack";
  section.setAttribute("aria-label", "球員跨屆成長檔案");

  // 1. 頂部搜尋列與分享按鈕
  const topControls = document.createElement("div");
  topControls.className = "toolbar-row";

  const comboboxHost = document.createElement("div");
  comboboxHost.style.flex = "1 1 20rem";
  let combobox: PlayerComboboxController | null = null;

  const shareGroup = document.createElement("div");
  shareGroup.className = "toolbar-group";

  let currentEffectiveUid: string | null = null;
  const clipsBtn = document.createElement("button");
  clipsBtn.type = "button";
  clipsBtn.className = "btn";
  clipsBtn.textContent = "🎬 查看影片集合";
  clipsBtn.hidden = !options.onOpenPlayerClips;
  clipsBtn.addEventListener("click", () => {
    if (currentEffectiveUid && options.onOpenPlayerClips) {
      options.onOpenPlayerClips(currentEffectiveUid);
    }
  });

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
      ? "已複製專屬分享網址！"
      : "請直接複製瀏覽器網址列分享";

    if (copyTimer !== null) {
      window.clearTimeout(copyTimer);
    }
    copyTimer = window.setTimeout(() => {
      copyStatus.textContent = "";
    }, 2600);
  });

  shareGroup.append(clipsBtn, copyBtn, copyStatus);
  topControls.append(comboboxHost, shareGroup);

  // 2. 球員履歷摘要卡
  const profileCard = document.createElement("article");
  profileCard.className = "profile-banner";

  const profileHeaderRow = document.createElement("div");
  profileHeaderRow.className = "profile-header-row";
  const profileNameEl = document.createElement("h2");
  profileNameEl.className = "profile-name";
  const profileUidEl = document.createElement("span");
  profileUidEl.className = "profile-uid";
  profileHeaderRow.append(profileNameEl, profileUidEl);

  const profileTagsEl = document.createElement("div");
  profileTagsEl.className = "profile-tags";

  const profileStatsLine = document.createElement("p");
  profileStatsLine.className = "brand-subtitle";

  profileCard.append(profileHeaderRow, profileTagsEl, profileStatsLine);

  const noDataBanner = document.createElement("p");
  noDataBanner.className = "status-banner";
  noDataBanner.hidden = true;

  // 3. 兩張歷屆成長趨勢圖
  const chartsContainer = document.createElement("div");
  chartsContainer.className = "charts-container";

  const chartsGrid = document.createElement("div");
  chartsGrid.className = "charts-grid";

  const batChartCard = document.createElement("article");
  batChartCard.className = "chart-card";
  const batChartTitle = document.createElement("h3");
  batChartTitle.className = "chart-title";
  batChartTitle.textContent = "📈 歷屆打擊成長趨勢（AVG / OBP / SLG / OPS）";
  const batCanvasWrap = document.createElement("div");
  batCanvasWrap.className = "chart-canvas-wrap";
  const batCanvas = document.createElement("canvas");
  batCanvas.setAttribute("aria-label", "歷屆打擊三圍與OPS成長折線圖");
  batCanvasWrap.append(batCanvas);
  batChartCard.append(batChartTitle, batCanvasWrap);

  const pitchChartCard = document.createElement("article");
  pitchChartCard.className = "chart-card";
  const pitchChartTitle = document.createElement("h3");
  pitchChartTitle.className = "chart-title";
  const pitchCanvasWrap = document.createElement("div");
  pitchCanvasWrap.className = "chart-canvas-wrap";
  const pitchCanvas = document.createElement("canvas");
  pitchCanvas.setAttribute("aria-label", "歷屆投球防禦率與WHIP趨勢折線圖");
  pitchCanvasWrap.append(pitchCanvas);
  pitchChartCard.append(pitchChartTitle, pitchCanvasWrap);

  chartsGrid.append(batChartCard, pitchChartCard);
  chartsContainer.append(chartsGrid);

  let batChart: TrendChartController | null = null;
  let pitchChart: TrendChartController | null = null;

  // 4. 歷屆打擊／投球明細表
  const battingTable = createDataTable<PlayerSeasonBattingRow>({
    caption: "歷屆打擊出賽明細表",
    columns: BATTING_SEASON_COLUMNS,
    rows: [],
    emptyMessage: "在目前篩選條件下無此球員的打擊紀錄。",
  });

  const pitchTableHost = document.createElement("div");
  let pitchingTable: DataTableController<PlayerSeasonPitchingRow> | null = null;
  let pitchingTableEra: EraInnings | null = null;

  section.append(
    topControls,
    profileCard,
    noDataBanner,
    chartsContainer,
    battingTable.element,
    pitchTableHost
  );

  function addTag(text: string): void {
    const pill = document.createElement("span");
    pill.className = "tag-pill";
    pill.textContent = text;
    profileTagsEl.append(pill);
  }

  return {
    element: section,
    refreshTheme: () => {
      batChart?.refreshTheme();
      pitchChart?.refreshTheme();
    },
    update: (dataset, teamIds, selectedPlayerUid, eraInnings, index) => {
      // Combobox 列出所有非教練球員以維持分享連結定位彈性
      const selectablePlayers = getSelectablePlayers(dataset, undefined, index);
      const effectiveUid =
        selectedPlayerUid &&
        selectablePlayers.some((p) => p.playerUid === selectedPlayerUid)
          ? selectedPlayerUid
          : selectablePlayers[0]?.playerUid ?? null;

      if (!combobox) {
        combobox = createPlayerCombobox({
          players: selectablePlayers,
          selectedPlayerUid: effectiveUid,
          onSelect: (uid) => options.onSelectPlayer(uid),
        });
        comboboxHost.replaceChildren(combobox.element);
      } else {
        combobox.updatePlayers(selectablePlayers, effectiveUid);
      }

      if (!effectiveUid) {
        currentEffectiveUid = null;
        clipsBtn.disabled = true;
        profileCard.hidden = true;
        chartsContainer.hidden = true;
        battingTable.element.hidden = true;
        pitchTableHost.hidden = true;
        noDataBanner.hidden = false;
        noDataBanner.textContent = "目前資料庫中尚無可顯示的球員資料。";
        return;
      }

      currentEffectiveUid = effectiveUid;
      clipsBtn.disabled = false;
      profileCard.hidden = false;
      const history = getPlayerHistory(
        dataset,
        effectiveUid,
        eraInnings,
        teamIds,
        index
      );
      const fullHistory = getPlayerHistory(
        dataset,
        effectiveUid,
        eraInnings,
        undefined,
        index
      );
      const player = history.player ?? fullHistory.player;

      profileNameEl.textContent = player ? player.masked_name : effectiveUid;
      profileUidEl.textContent = `UID: ${effectiveUid}`;

      profileTagsEl.replaceChildren();
      addTag(`出生年月：${player?.birth_ym ?? "未知"}`);
      addTag(
        `歷經層級：${fullHistory.ageGroups.join(" → ") || "未登錄"}`
      );
      if (fullHistory.jerseyNumbers.length > 0) {
        addTag(`曾穿背號：#${fullHistory.jerseyNumbers.join(" / #")}`);
      }
      if (fullHistory.positions.length > 0) {
        addTag(`登錄守位：${fullHistory.positions.join("、")}`);
      }

      // 計算目前篩選範圍內的生涯累計摘要
      const summaryParts: string[] = [];
      if (history.battingSeasons.length > 0) {
        let g = 0;
        let pa = 0;
        let ab = 0;
        let h = 0;
        let d = 0;
        let t = 0;
        let hr = 0;
        let bb = 0;
        for (const row of history.battingSeasons) {
          g += row.g;
          pa += row.pa;
          ab += row.ab;
          h += row.h;
          d += row.doubles;
          t += row.triples;
          hr += row.hr;
          bb += row.bb;
        }
        const avg = calcAvg(h, ab);
        const obp = calcObp(h, ab, bb);
        const slg = calcSlg(h, d, t, hr, ab);
        const ops = calcOps(h, d, t, hr, ab, bb);
        summaryParts.push(
          `打擊累計（${history.battingSeasons.length} 隊次）：${g}G / ${pa}PA / ${h}H / ${hr}HR，AVG ${formatRate3(avg)} / OBP ${formatRate3(obp)} / SLG ${formatRate3(slg)} / OPS ${formatRate3(ops)}`
        );
      }

      if (history.pitchingSeasons.length > 0) {
        let g = 0;
        let w = 0;
        let l = 0;
        let sv = 0;
        let outs = 0;
        let h = 0;
        let bb = 0;
        let so = 0;
        let er = 0;
        for (const row of history.pitchingSeasons) {
          g += row.g;
          w += row.w;
          l += row.l;
          sv += row.sv;
          outs += row.outs;
          h += row.h;
          bb += row.bb;
          so += row.so;
          er += row.er;
        }
        const era = calcEra(er, outs, eraInnings);
        const whip = calcWhip(h, bb, outs);
        const k7 = calcK7(so, outs);
        summaryParts.push(
          `投球累計（${history.pitchingSeasons.length} 隊次）：${g}G / ${formatInnings(outs)} IP / ${w}W-${l}L-${sv}SV，ERA(${eraInnings}局) ${formatRate2(era)} / WHIP ${formatRate2(whip)} / K/7 ${formatRate2(k7)}`
        );
      }

      if (summaryParts.length === 0) {
        profileStatsLine.textContent =
          "此球員在目前篩選條件下無攻守數據，請調整篩選範圍。";
        noDataBanner.hidden = false;
        noDataBanner.textContent =
          "目前篩選條件未包含此球員出賽的隊伍；全選篩選即可檢視完整歷屆成長紀錄。";
      } else {
        profileStatsLine.textContent = summaryParts.join(" ｜ ");
        noDataBanner.hidden = true;
      }

      // 更新打擊圖表
      const hasBat = history.battingSeasons.length > 0;
      batChartCard.hidden = !hasBat;
      if (hasBat) {
        const labels = history.battingSeasons.map(
          (r) => `${r.year}${r.season} (${r.ageGroup})`
        );
        const batChartConfig = {
          labels,
          yAxisTitle: "打擊率 / 攻擊指數",
          decimals: 3,
          series: [
            {
              label: "AVG",
              data: history.battingSeasons.map((r) => r.avg),
              colorLight: "#005fcc",
              colorDark: "#60a5fa",
            },
            {
              label: "OBP",
              data: history.battingSeasons.map((r) => r.obp),
              colorLight: "#116a3d",
              colorDark: "#4ade80",
            },
            {
              label: "SLG",
              data: history.battingSeasons.map((r) => r.slg),
              colorLight: "#c88a00",
              colorDark: "#f2be38",
            },
            {
              label: "OPS",
              data: history.battingSeasons.map((r) => r.ops),
              colorLight: "#b42318",
              colorDark: "#f87171",
            },
          ],
        };
        if (!batChart) {
          batChart = createTrendChart(batCanvas, batChartConfig);
        } else {
          batChart.update(batChartConfig);
        }
      }

      // 更新投球圖表
      const hasPitch = history.pitchingSeasons.length > 0;
      pitchChartCard.hidden = !hasPitch;
      pitchChartTitle.textContent = `🎯 歷屆投球表現趨勢（${eraInnings} 局制 ERA / WHIP / K/7）`;
      if (hasPitch) {
        const labels = history.pitchingSeasons.map(
          (r) => `${r.year}${r.season} (${r.ageGroup})`
        );
        const pitchChartConfig = {
          labels,
          yAxisTitle: "投手率值",
          decimals: 2,
          series: [
            {
              label: `ERA (${eraInnings}局)`,
              data: history.pitchingSeasons.map((r) => r.era),
              colorLight: "#b42318",
              colorDark: "#f87171",
            },
            {
              label: "WHIP",
              data: history.pitchingSeasons.map((r) => r.whip),
              colorLight: "#005fcc",
              colorDark: "#60a5fa",
            },
            {
              label: "K/7",
              data: history.pitchingSeasons.map((r) => r.k7),
              colorLight: "#116a3d",
              colorDark: "#4ade80",
            },
          ],
        };
        if (!pitchChart) {
          pitchChart = createTrendChart(pitchCanvas, pitchChartConfig);
        } else {
          pitchChart.update(pitchChartConfig);
        }
      }

      chartsContainer.hidden = !hasBat && !hasPitch;

      // 更新明細表
      battingTable.element.hidden = !hasBat;
      battingTable.updateRows(history.battingSeasons);

      pitchTableHost.hidden = !hasPitch;
      if (!pitchingTable || pitchingTableEra !== eraInnings) {
        pitchingTableEra = eraInnings;
        pitchingTable = createDataTable<PlayerSeasonPitchingRow>({
          caption: `歷屆投球登板明細表（${eraInnings} 局制 ERA）`,
          columns: buildPitchingSeasonColumns(eraInnings),
          rows: history.pitchingSeasons,
          emptyMessage: "在目前篩選條件下無此球員的投球紀錄。",
        });
        pitchTableHost.replaceChildren(pitchingTable.element);
      } else {
        pitchingTable.updateRows(history.pitchingSeasons);
      }
    },
  };
}
