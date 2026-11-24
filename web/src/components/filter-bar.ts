/**
 * 全域篩選控制列元件 (filter-bar.ts)
 * - 桌機寬螢幕：顯示於頂部精簡篩選工具列
 * - 行動窄螢幕：收合至原生 <dialog closedby="any"> Modal 抽屜（含 Safari light-dismiss fallback）
 */

import type {
  AgeGroup,
  EraInnings,
  FilterState,
  PiratesDataset,
  Season,
  TeamGroup,
  TournamentType,
} from "../types/data";

type FilterOptionsMeta = PiratesDataset["meta"]["filter_options"];

export interface FilterBarOptions {
  filterOptions: FilterOptionsMeta;
  initialFilters: FilterState;
  initialEraInnings: EraInnings;
  onFiltersChange: (nextFilters: FilterState) => void;
  onEraChange: (nextEra: EraInnings) => void;
  onReset: () => void;
}

export interface FilterBarController {
  desktopElement: HTMLElement;
  drawerElement: HTMLDialogElement;
  openDrawer: () => void;
  closeDrawer: () => void;
  sync: (filters: FilterState, eraInnings: EraInnings, matchedTeamsCount: number) => void;
}

/** 計算目前有幾項維度非預設全選，供行動版篩選按鈕徽章顯示 */
export function countActiveFilterGroups(
  filters: FilterState,
  options: FilterOptionsMeta
): number {
  let count = 0;
  if (filters.ageGroups.length !== options.age_groups.length) count += 1;
  if (filters.teamGroups.length !== options.team_groups.length) count += 1;
  if (filters.years.length !== options.years.length) count += 1;
  if (filters.seasons.length !== options.seasons.length) count += 1;
  if (filters.tournamentTypes.length !== options.tournament_types.length) count += 1;
  return count;
}

interface CheckboxBinding<T extends string | number> {
  value: T;
  input: HTMLInputElement;
}

interface FilterSurfaceRefs {
  ageChecks: CheckboxBinding<AgeGroup>[];
  groupChecks: CheckboxBinding<TeamGroup>[];
  yearChecks: CheckboxBinding<number>[];
  seasonChecks: CheckboxBinding<Season>[];
  tourneyChecks: CheckboxBinding<TournamentType>[];
  eraRadios: Array<{ value: EraInnings; input: HTMLInputElement }>;
  matchCountText: HTMLElement;
}

export function createFilterBar(options: FilterBarOptions): FilterBarController {
  let currentFilters: FilterState = {
    ageGroups: [...options.initialFilters.ageGroups],
    teamGroups: [...options.initialFilters.teamGroups],
    years: [...options.initialFilters.years],
    seasons: [...options.initialFilters.seasons],
    tournamentTypes: [...options.initialFilters.tournamentTypes],
  };
  let currentEra: EraInnings = options.initialEraInnings;

  const surfaces: FilterSurfaceRefs[] = [];

  const syncAllSurfaces = (matchedTeamsCount?: number): void => {
    for (const s of surfaces) {
      const ageSet = new Set(currentFilters.ageGroups);
      for (const item of s.ageChecks) item.input.checked = ageSet.has(item.value);

      const groupSet = new Set(currentFilters.teamGroups);
      for (const item of s.groupChecks) item.input.checked = groupSet.has(item.value);

      const yearSet = new Set(currentFilters.years);
      for (const item of s.yearChecks) item.input.checked = yearSet.has(item.value);

      const seasonSet = new Set(currentFilters.seasons);
      for (const item of s.seasonChecks) item.input.checked = seasonSet.has(item.value);

      const tourneySet = new Set(currentFilters.tournamentTypes);
      for (const item of s.tourneyChecks) item.input.checked = tourneySet.has(item.value);

      for (const radio of s.eraRadios) {
        radio.input.checked = radio.value === currentEra;
      }

      if (matchedTeamsCount !== undefined) {
        s.matchCountText.textContent = `符合條件隊伍：${matchedTeamsCount} 隊`;
      }
    }
  };

  function buildCheckboxGroup<T extends string | number>(
    legendText: string,
    items: readonly T[],
    selected: readonly T[],
    onToggle: (nextSelected: T[]) => void
  ): { fieldset: HTMLFieldSetElement; bindings: CheckboxBinding<T>[] } {
    const fieldset = document.createElement("fieldset");
    fieldset.className = "filter-group";

    const legend = document.createElement("legend");
    legend.textContent = legendText;

    const chips = document.createElement("div");
    chips.className = "filter-chips";

    const bindings: CheckboxBinding<T>[] = [];
    const selectedSet = new Set(selected);

    for (const val of items) {
      const label = document.createElement("label");
      label.className = "filter-option";

      const input = document.createElement("input");
      input.type = "checkbox";
      input.checked = selectedSet.has(val);
      input.addEventListener("change", () => {
        const next = bindings
          .filter((b) => b.input.checked)
          .map((b) => b.value);
        onToggle(next);
      });

      const span = document.createElement("span");
      span.textContent = String(val);

      label.append(input, span);
      chips.append(label);
      bindings.push({ value: val, input });
    }

    fieldset.append(legend, chips);
    return { fieldset, bindings };
  }

  function buildFilterContent(surfacePrefix: string): HTMLElement {
    const container = document.createElement("div");
    container.className = "section-stack";

    const grid = document.createElement("div");
    grid.className = "filter-grid";

    const ageGroup = buildCheckboxGroup(
      "年齡層級",
      options.filterOptions.age_groups,
      currentFilters.ageGroups,
      (next) => {
        currentFilters = { ...currentFilters, ageGroups: next };
        syncAllSurfaces();
        options.onFiltersChange(currentFilters);
      }
    );

    const teamGroup = buildCheckboxGroup(
      "分隊群組",
      options.filterOptions.team_groups,
      currentFilters.teamGroups,
      (next) => {
        currentFilters = { ...currentFilters, teamGroups: next };
        syncAllSurfaces();
        options.onFiltersChange(currentFilters);
      }
    );

    const yearGroup = buildCheckboxGroup(
      "賽事年度",
      options.filterOptions.years,
      currentFilters.years,
      (next) => {
        currentFilters = { ...currentFilters, years: next };
        syncAllSurfaces();
        options.onFiltersChange(currentFilters);
      }
    );

    const seasonGroup = buildCheckboxGroup(
      "賽季",
      options.filterOptions.seasons,
      currentFilters.seasons,
      (next) => {
        currentFilters = { ...currentFilters, seasons: next };
        syncAllSurfaces();
        options.onFiltersChange(currentFilters);
      }
    );

    const tourneyGroup = buildCheckboxGroup(
      "賽制",
      options.filterOptions.tournament_types,
      currentFilters.tournamentTypes,
      (next) => {
        currentFilters = { ...currentFilters, tournamentTypes: next };
        syncAllSurfaces();
        options.onFiltersChange(currentFilters);
      }
    );

    grid.append(
      ageGroup.fieldset,
      teamGroup.fieldset,
      yearGroup.fieldset,
      seasonGroup.fieldset,
      tourneyGroup.fieldset
    );

    const footer = document.createElement("div");
    footer.className = "filter-footer";

    // ERA 7 局 / 9 局換算基準
    const eraSwitch = document.createElement("div");
    eraSwitch.className = "era-switch";
    eraSwitch.setAttribute("role", "radiogroup");
    eraSwitch.setAttribute("aria-label", "ERA 防禦率換算局數基準");

    const eraTitle = document.createElement("span");
    eraTitle.className = "era-switch-title";
    eraTitle.textContent = "ERA 基準：";
    eraSwitch.append(eraTitle);

    const eraRadios: Array<{ value: EraInnings; input: HTMLInputElement }> = [];
    const eraOptions: Array<{ value: EraInnings; label: string }> = [
      { value: 7, label: "7 局制（少棒標準）" },
      { value: 9, label: "9 局制（網站預設）" },
    ];

    for (const opt of eraOptions) {
      const label = document.createElement("label");
      label.className = "filter-option";

      const radio = document.createElement("input");
      radio.type = "radio";
      radio.name = `era-innings-${surfacePrefix}`;
      radio.value = String(opt.value);
      radio.checked = opt.value === currentEra;
      radio.addEventListener("change", () => {
        if (!radio.checked) return;
        currentEra = opt.value;
        syncAllSurfaces();
        options.onEraChange(opt.value);
      });

      const span = document.createElement("span");
      span.textContent = opt.label;
      label.append(radio, span);
      eraSwitch.append(label);
      eraRadios.push({ value: opt.value, input: radio });
    }

    const rightControls = document.createElement("div");
    rightControls.className = "toolbar-group";

    const matchCountText = document.createElement("span");
    matchCountText.className = "brand-subtitle";

    const resetBtn = document.createElement("button");
    resetBtn.type = "button";
    resetBtn.className = "btn";
    resetBtn.textContent = "重設篩選";
    resetBtn.addEventListener("click", () => {
      options.onReset();
    });

    rightControls.append(matchCountText, resetBtn);
    footer.append(eraSwitch, rightControls);

    container.append(grid, footer);

    surfaces.push({
      ageChecks: ageGroup.bindings,
      groupChecks: teamGroup.bindings,
      yearChecks: yearGroup.bindings,
      seasonChecks: seasonGroup.bindings,
      tourneyChecks: tourneyGroup.bindings,
      eraRadios,
      matchCountText,
    });

    return container;
  }

  // 1. 桌機常駐精簡篩選條
  const desktopSection = document.createElement("section");
  desktopSection.className = "filter-card desktop-filter-host";
  desktopSection.setAttribute("aria-label", "全域資料篩選條件");
  desktopSection.append(buildFilterContent("desktop"));

  // 2. 行動版原生 <dialog> 篩選抽屜（支援 light-dismiss）
  const drawer = document.createElement("dialog");
  drawer.className = "filter-drawer";
  drawer.setAttribute("closedby", "any");
  drawer.setAttribute("aria-labelledby", "filter-drawer-title");

  const drawerShell = document.createElement("div");
  drawerShell.className = "drawer-shell";

  const drawerHeader = document.createElement("div");
  drawerHeader.className = "drawer-header";

  const drawerTitle = document.createElement("h2");
  drawerTitle.id = "filter-drawer-title";
  drawerTitle.textContent = "篩選條件與 ERA 基準";

  const closeIconBtn = document.createElement("button");
  closeIconBtn.type = "button";
  closeIconBtn.className = "btn btn-ghost";
  closeIconBtn.setAttribute("aria-label", "關閉篩選視窗");
  closeIconBtn.textContent = "✕";
  closeIconBtn.addEventListener("click", () => drawer.close());

  drawerHeader.append(drawerTitle, closeIconBtn);

  const drawerBody = document.createElement("div");
  drawerBody.className = "drawer-body";
  drawerBody.append(buildFilterContent("mobile"));

  const drawerFooter = document.createElement("div");
  drawerFooter.className = "drawer-footer";

  const drawerResetBtn = document.createElement("button");
  drawerResetBtn.type = "button";
  drawerResetBtn.className = "btn";
  drawerResetBtn.textContent = "全部重設";
  drawerResetBtn.addEventListener("click", () => options.onReset());

  const applyCloseBtn = document.createElement("button");
  applyCloseBtn.type = "button";
  applyCloseBtn.className = "btn btn-primary";
  applyCloseBtn.textContent = "完成查看";
  applyCloseBtn.addEventListener("click", () => drawer.close());

  drawerFooter.append(drawerResetBtn, applyCloseBtn);
  drawerShell.append(drawerHeader, drawerBody, drawerFooter);
  drawer.append(drawerShell);

  // MWG light-dismiss-a-dialog 強制 Safari Fallback（無 closedBy 支援時以座標點擊判定外部關閉）
  if (!("closedBy" in HTMLDialogElement.prototype)) {
    drawer.addEventListener("click", (event) => {
      if (event.target !== drawer) {
        return;
      }
      const rect = drawer.getBoundingClientRect();
      const isInsideContent =
        rect.top <= event.clientY &&
        event.clientY <= rect.top + rect.height &&
        rect.left <= event.clientX &&
        event.clientX <= rect.left + rect.width;
      if (!isInsideContent) {
        drawer.close();
      }
    });
  }

  return {
    desktopElement: desktopSection,
    drawerElement: drawer,
    openDrawer: () => {
      if (!drawer.open) {
        drawer.showModal();
      }
    },
    closeDrawer: () => {
      if (drawer.open) {
        drawer.close();
      }
    },
    sync: (filters, eraInnings, matchedTeamsCount) => {
      currentFilters = {
        ageGroups: [...filters.ageGroups],
        teamGroups: [...filters.teamGroups],
        years: [...filters.years],
        seasons: [...filters.seasons],
        tournamentTypes: [...filters.tournamentTypes],
      };
      currentEra = eraInnings;
      syncAllSurfaces(matchedTeamsCount);
    },
  };
}
